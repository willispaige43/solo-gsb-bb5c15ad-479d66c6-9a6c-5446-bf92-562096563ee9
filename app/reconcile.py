"""实播对账核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 计划侧是与 ``POST /compile`` 相同的口令数组，复用 ``app.compiler`` 的锚点解析
  得到每条口令的计划起止毫秒。
* 实播记录携带唯一 ``id``、``channel`` 与实际起止毫秒；起点为负、或结束时刻
  不大于起点的区间非法。
* 每个通道独立做 **保持顺序的一对一匹配**：通道内计划项与实播记录分别按
  ``(起点, id)`` 排序后，匹配对在下标意义上单调递增（类似序列比对）。
* 目标：最小化「匹配对起止偏差绝对值之和 + 漏播罚分 × 漏播计划数
  + 额外罚分 × 额外实播数」；只有起点偏差与终点偏差都不超过容差的同通道项
  才允许匹配。
* 同分时依次取：匹配对数更多者；匹配 id 对 ``(计划 id, 实播 id)`` 序列
  （按匹配顺序）字典序更小者。
"""

from __future__ import annotations

from typing import Any

from .compiler import CompileError, compile_commands

# 通道内单条记录统一为 (起点, 终点, id) 元组，排序键即 (起点, id)
PlanItem = tuple[int, int, str]


class ReconcileError(CompileError):
    """实播对账失败的业务异常；复用 CompileError 的 422 转换通道。"""


def reconcile_runs(
    commands: list[dict[str, Any]],
    records: list[dict[str, Any]],
    tolerance_ms: int,
    miss_penalty: int,
    extra_penalty: int,
) -> dict[str, Any]:
    """对计划口令与实播记录做对账，返回匹配对、漏播/额外 id 与总罚分。

    不会修改传入的 ``commands`` / ``records``。

    :raises CompileError: 计划口令非法（重复 id、依赖环等，与 /compile 相同）。
    :raises ReconcileError: 容差或罚分为负、实播记录重复 id / 空 id / 空通道 /
        区间非法时抛出，``code`` 可用于程序化区分错误类型。
    """
    if tolerance_ms < 0 or miss_penalty < 0 or extra_penalty < 0:
        raise ReconcileError(
            "INVALID_REQUEST",
            "tolerance_ms、miss_penalty、extra_penalty 都必须是非负数",
            {
                "tolerance_ms": tolerance_ms,
                "miss_penalty": miss_penalty,
                "extra_penalty": extra_penalty,
            },
        )

    # 1) 计划侧：复用 /compile 的锚点解析（拷贝一份，避免 setdefault 改动调用方数据；
    #    对账只使用时间线，跳过 O(n²) 的冲突对枚举）
    timeline = compile_commands(
        [dict(item) for item in commands], with_conflicts=False
    )["timeline"]

    # 2) 实播记录校验
    validate_records(records)

    # 3) 按通道分组；timeline 已按 (start_ms, id) 排序，过滤后通道内保持有序
    plans_by_channel: dict[str, list[PlanItem]] = {}
    for entry in timeline:
        plans_by_channel.setdefault(entry["channel"], []).append(
            (entry["start_ms"], entry["end_ms"], entry["id"])
        )

    actuals_by_channel: dict[str, list[PlanItem]] = {}
    for record in records:
        actuals_by_channel.setdefault(record["channel"], []).append(
            (record["actual_start_ms"], record["actual_end_ms"], record["id"])
        )
    for channel_records in actuals_by_channel.values():
        channel_records.sort(key=lambda item: (item[0], item[2]))

    # 4) 各通道独立求解保序最优匹配，汇总结果
    matches: list[dict[str, Any]] = []
    missed: list[str] = []
    extras: list[str] = []
    total_penalty = 0
    for channel in sorted(plans_by_channel.keys() | actuals_by_channel.keys()):
        pairs, missed_ids, extra_ids, cost = _match_channel(
            plans_by_channel.get(channel, []),
            actuals_by_channel.get(channel, []),
            tolerance_ms,
            miss_penalty,
            extra_penalty,
        )
        total_penalty += cost
        missed.extend(missed_ids)
        extras.extend(extra_ids)
        for (p_start, p_end, p_id), (a_start, a_end, a_id) in pairs:
            matches.append(
                {
                    "plan_id": p_id,
                    "actual_id": a_id,
                    "channel": channel,
                    "planned_interval": [p_start, p_end],
                    "actual_interval": [a_start, a_end],
                    "start_deviation_ms": a_start - p_start,
                    "end_deviation_ms": a_end - p_end,
                }
            )

    matches.sort(key=lambda item: (item["plan_id"], item["actual_id"]))
    return {
        "matches": matches,
        "missed_plan_ids": sorted(missed),
        "extra_actual_ids": sorted(extras),
        "total_penalty": total_penalty,
    }


def validate_records(records: list[dict[str, Any]]) -> None:
    """实播记录的字段级校验，非法输入抛出 ``ReconcileError``。

    与 ``/runs/reconcile``、``/runs/calibrate`` 共用同一套校验：重复 id、
    空 id、空通道、区间非法（起点为负或结束时刻不大于起点）。
    """
    # 1) 重复 id
    seen: set[str] = set()
    duplicates: list[str] = []
    for record in records:
        rid = record["id"]
        if rid in seen and rid not in duplicates:
            duplicates.append(rid)
        seen.add(rid)
    if duplicates:
        raise ReconcileError(
            "DUPLICATE_ID",
            f"存在重复的实播记录 id: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    # 2) 空 id / 空通道
    empty_ids = [record["id"] for record in records if record["id"] == ""]
    if empty_ids:
        raise ReconcileError("INVALID_ID", "实播记录 id 不能为空", {"ids": empty_ids})

    empty_channels = [record["id"] for record in records if record["channel"] == ""]
    if empty_channels:
        raise ReconcileError(
            "INVALID_CHANNEL", "通道名不能为空", {"ids": sorted(empty_channels)}
        )

    # 3) 区间：起点不得为负，结束时刻必须大于起点
    bad_interval = [
        record["id"]
        for record in records
        if record["actual_start_ms"] < 0
        or record["actual_end_ms"] <= record["actual_start_ms"]
    ]
    if bad_interval:
        raise ReconcileError(
            "INVALID_INTERVAL",
            "实播区间非法：actual_start_ms 不得为负，"
            "且 actual_end_ms 必须大于 actual_start_ms",
            {"ids": bad_interval},
        )


def _match_channel(
    plans: list[PlanItem],
    actuals: list[PlanItem],
    tolerance_ms: int,
    miss_penalty: int,
    extra_penalty: int,
) -> tuple[list[tuple[PlanItem, PlanItem]], list[str], list[str], int]:
    """单通道的保序最优匹配。

    ``plans`` / ``actuals`` 均已按 ``(起点, id)`` 排序。
    返回 ``(匹配对列表, 漏播计划 id 列表, 额外实播 id 列表, 通道罚分)``，
    匹配对按匹配顺序（即计划起点升序）排列。
    """
    m, n = len(plans), len(actuals)

    # 后缀 DP：cost[i][j] 为 plans[i:] 与 actuals[j:] 对账的最小罚分，
    # cnt[i][j] 为取得最小罚分时的最大匹配对数
    cost = [[0] * (n + 1) for _ in range(m + 1)]
    cnt = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        cost[i][n] = miss_penalty * (m - i)
    for j in range(n - 1, -1, -1):
        cost[m][j] = extra_penalty * (n - j)
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            # 三种转移：plans[i] 漏播 / actuals[j] 额外播出 / 二者匹配
            best_cost = miss_penalty + cost[i + 1][j]
            best_cnt = cnt[i + 1][j]
            cand_cost = extra_penalty + cost[i][j + 1]
            if (cand_cost, -cnt[i][j + 1]) < (best_cost, -best_cnt):
                best_cost, best_cnt = cand_cost, cnt[i][j + 1]
            pair_cost = _pair_cost(plans[i], actuals[j], tolerance_ms)
            if pair_cost is not None:
                cand_cost = pair_cost + cost[i + 1][j + 1]
                cand_cnt = 1 + cnt[i + 1][j + 1]
                if (cand_cost, -cand_cnt) < (best_cost, -best_cnt):
                    best_cost, best_cnt = cand_cost, cand_cnt
            cost[i][j] = best_cost
            cnt[i][j] = best_cnt

    def take_match(i: int, j: int) -> bool:
        """匹配 (i, j) 是否与 (i, j) 处的最优解一致。"""
        pair_cost = _pair_cost(plans[i], actuals[j], tolerance_ms)
        return (
            pair_cost is not None
            and cost[i][j] == pair_cost + cost[i + 1][j + 1]
            and cnt[i][j] == 1 + cnt[i + 1][j + 1]
        )

    def skip_plan(i: int, j: int) -> bool:
        """漏播 plans[i] 是否与 (i, j) 处的最优解一致。"""
        return (
            cost[i][j] == miss_penalty + cost[i + 1][j]
            and cnt[i][j] == cnt[i + 1][j]
        )

    def skip_actual(i: int, j: int) -> bool:
        """额外播出 actuals[j] 是否与 (i, j) 处的最优解一致。"""
        return (
            cost[i][j] == extra_penalty + cost[i][j + 1]
            and cnt[i][j] == cnt[i][j + 1]
        )

    # first[i][j]：从 (i, j) 出发沿最优转移能发出的第一对 (计划 id, 实播 id)，
    # 用于在罚分、匹配数都相同的解之间取 id 对序列字典序最小者；None 表示后面没有匹配
    first: list[list[tuple[str, str] | None]] = [
        [None] * (n + 1) for _ in range(m + 1)
    ]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            best_pair: tuple[str, str] | None = None
            if skip_plan(i, j):
                best_pair = first[i + 1][j]
            if skip_actual(i, j):
                best_pair = _min_pair(best_pair, first[i][j + 1])
            if take_match(i, j):
                best_pair = _min_pair(best_pair, (plans[i][2], actuals[j][2]))
            first[i][j] = best_pair

    # 沿最优转移走一遍：匹配 / 漏播 / 额外播出。
    # 同一 id 对只能由唯一的匹配转移发出，因此每步三个分支至多一个成立
    pairs: list[tuple[PlanItem, PlanItem]] = []
    missed: list[str] = []
    extras: list[str] = []
    i = j = 0
    while i < m and j < n:
        if take_match(i, j) and first[i][j] == (plans[i][2], actuals[j][2]):
            pairs.append((plans[i], actuals[j]))
            i += 1
            j += 1
        elif skip_plan(i, j) and first[i + 1][j] == first[i][j]:
            missed.append(plans[i][2])
            i += 1
        else:  # skip_actual(i, j) 必然成立且 first[i][j + 1] == first[i][j]
            extras.append(actuals[j][2])
            j += 1
    missed.extend(item[2] for item in plans[i:])
    extras.extend(item[2] for item in actuals[j:])
    return pairs, missed, extras, cost[0][0]


def _pair_cost(plan: PlanItem, actual: PlanItem, tolerance_ms: int) -> int | None:
    """匹配代价（起止偏差绝对值之和）；任一偏差超过容差时返回 None（不可匹配）。"""
    dev_start = abs(plan[0] - actual[0])
    dev_end = abs(plan[1] - actual[1])
    if dev_start > tolerance_ms or dev_end > tolerance_ms:
        return None
    return dev_start + dev_end


def _min_pair(
    a: tuple[str, str] | None, b: tuple[str, str] | None
) -> tuple[str, str] | None:
    """两个候选 id 对取字典序较小者，None 视为无穷大。"""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
