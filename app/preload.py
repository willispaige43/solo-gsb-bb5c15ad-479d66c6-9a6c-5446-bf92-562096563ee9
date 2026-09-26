"""素材预载规划核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 现有口令沿用 ``POST /compile`` 的口令数组，先用同一个编排器解析并校验每条口令的
  起止毫秒；每条素材通过 ``command_id`` 关联一条现有口令，**该口令解析出的起点**
  即素材加载的截止时刻（须在该时刻前完成，允许恰好完成）。
* 至多 12 条素材，字段：``id``、``command_id``、``ready_ms``（最早可开始时刻，
  非负）、``load_ms``（串行加载时长，正数）、``priority``（非负整数）。
  素材 id 与关联口令均不得重复。
* 单台加载器从时刻 0 起**串行、不可中断**地工作：素材只能在 ``ready_ms`` 之后
  开始（未就绪时空转等待），且必须在关联口令起点之前完成；不满足的素材可跳过。
* 目标依次为：① 按时加载条数最多；② 并列时入选素材 ``priority`` 总和最大；
  ③ 再并列时「入选 id 升序列表」字典序最小；④ 同一入选集合取总完工时刻最早
  的加载顺序；⑤ 完工时刻仍相同时取「加载 id 顺序」字典序最小。

实现要点
--------
素材至多 12 条，按 id 升序编号后用位掩码做子集动态规划：

1. ``earliest[mask]``：从时刻 0 起串行加载 ``mask`` 中全部素材（不可跳过）时，
   「每条素材均不晚于其截止时刻完工」的排列所能达到的最早总完工时刻；不可行
   记为 INF。枚举排在最后的素材 i：前段取其最早可行完工时刻，i 在
   ``max(前段完工, ready_i)`` 时刻开始且不晚于 ``deadline_i`` 完工。
2. 对全部可行子集按 (条数, priority 总和, 入选 id 升序列表) 裁决；列表字典序
   比较与空档填充一致，转化为 ``n + 1`` 进制的整数编码（位值 0 留作末尾补齐
   的哨兵）。
3. 入选集合确定后逐位置恢复加载顺序：每个位置枚举放在当前时刻之后的候选素材，
   用一张「给定起始时刻 t 的子集最早可行完工」DP 求「候选 + 剩余集合」的总完工
   时刻，先取总完工最早的候选、并列取 id 最小者，从而同时满足目标 ④、⑤。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .compiler import CompileError, compile_commands

# 一次预载规划最多允许的素材条数
MAX_PRELOAD_ASSETS = 12

# 不可行状态的哨兵完工时刻（真实时刻远小于它）
INF = 1 << 62


class PreloadError(CompileError):
    """素材预载规划失败的业务异常；复用 CompileError 的 422 转换通道。"""


def plan_preload(
    commands: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """求按时加载条数最多（并列优先级、字典序裁决）的串行预载方案。

    不会修改传入的 ``commands`` / ``assets``。

    :raises CompileError: 口令编排非法（重复 id、依赖环、负起点等，与 /compile 相同）。
    :raises PreloadError: 素材非法（空 id、重复 id、超过 12 条、负 ready_ms、
        非正 load_ms、负 priority、重复关联口令、关联口令不存在）时抛出，
        ``code`` 可用于程序化区分错误类型。
    """
    # 1) 复用编排器解析并校验现有口令（深拷贝，setdefault 等不会回写调用方数据；
    #    预载只需要口令起点，跳过 O(n²) 的冲突对枚举）
    compiled = compile_commands(deepcopy(commands), with_conflicts=False)
    starts = {entry["id"]: entry["start_ms"] for entry in compiled["timeline"]}

    # 2) 校验素材列表，得到按 id 升序排列的预载素材
    items = _validate_assets(assets, starts)

    n = len(items)
    size = 1 << n
    base = n + 1
    loads = [item["load_ms"] for item in items]
    readies = [item["ready_ms"] for item in items]
    deadlines = [starts[item["command_id"]] for item in items]

    # 3) 子集最早可行完工表：
    #    earliest[mask] = 从 0 时刻起串行加载 mask 全部素材、且每条素材都按时
    #    完工的排列的最小总完工时刻；不可行为 INF。枚举最后加载的素材 i，
    #    前段越早对 i 越有利（ready 只是下界），故取前段最早可行完工时刻即可。
    earliest = [INF] * size
    earliest[0] = 0
    for mask in range(1, size):
        best = INF
        bits = mask
        while bits:
            low = bits & -bits
            i = low.bit_length() - 1
            prev = earliest[mask ^ low]
            if prev < INF:
                finish_i = max(prev, readies[i]) + loads[i]
                if finish_i <= deadlines[i] and finish_i < best:
                    best = finish_i
            bits ^= low
        earliest[mask] = best

    # 4) 入选集合裁决：条数最多 -> priority 总和最大 -> 入选 id 升序列表字典序最小
    priorities = [item["priority"] for item in items]
    popcount = [0] * size
    sub_pri = [0] * size
    for mask in range(1, size):
        low = mask & -mask
        i = low.bit_length() - 1
        rest = mask ^ low
        popcount[mask] = popcount[rest] + 1
        sub_pri[mask] = sub_pri[rest] + priorities[i]

    pow_base = [1] * (n + 1)
    for k in range(1, n + 1):
        pow_base[k] = pow_base[k - 1] * base
    # sub_code[mask]：子集按 id 升序列表的 (n+1) 进制编码（最低位 id 占最高位），
    # 末尾补位值 0 后直接比较整数即比较列表字典序
    sub_code = [0] * size
    for mask in range(1, size):
        low = mask & -mask
        i = low.bit_length() - 1
        rest = mask ^ low
        sub_code[mask] = (i + 1) * pow_base[popcount[rest]] + sub_code[rest]

    best_mask = 0
    best_key: tuple[int, int, int] | None = None
    for mask in range(size):
        if earliest[mask] == INF:
            continue
        key = (
            popcount[mask],
            sub_pri[mask],
            -(sub_code[mask] * pow_base[n - popcount[mask]]),
        )
        if best_key is None or key > best_key:
            best_key = key
            best_mask = mask

    # 5) 恢复该集合的最优加载顺序：最早总完工优先，并列时加载 id 顺序字典序最小
    order = _restore_order(best_mask, loads, readies, deadlines)

    # 6) 按加载顺序计算每条入选素材的实际起止时刻
    placed: list[dict[str, Any]] = []
    cursor = 0
    for i in order:
        start = max(cursor, readies[i])
        end = start + loads[i]
        placed.append({"id": items[i]["id"], "start_ms": start, "end_ms": end})
        cursor = end

    chosen = {items[i]["id"] for i in order}
    skipped_ids = sorted(item["id"] for item in items if item["id"] not in chosen)
    return {"loads": placed, "skipped_ids": skipped_ids}


def _validate_assets(
    assets: list[dict[str, Any]], starts: dict[str, int]
) -> list[dict[str, Any]]:
    """校验素材列表，返回按 id 升序排列的素材 dict 列表。"""
    # 1) 空 id
    empty_ids = [asset["id"] for asset in assets if asset["id"] == ""]
    if empty_ids:
        raise PreloadError(
            "INVALID_ID", "素材 id 不能为空", {"ids": empty_ids}
        )

    # 2) 重复 id
    seen: set[str] = set()
    duplicates: list[str] = []
    for asset in assets:
        aid = asset["id"]
        if aid in seen and aid not in duplicates:
            duplicates.append(aid)
        seen.add(aid)
    if duplicates:
        raise PreloadError(
            "DUPLICATE_ID",
            f"存在重复的素材 id: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    # 3) 条数上限
    if len(assets) > MAX_PRELOAD_ASSETS:
        raise PreloadError(
            "TOO_MANY_PRELOAD_ASSETS",
            f"最多 {MAX_PRELOAD_ASSETS} 条素材参与一次预载规划，"
            f"当前 {len(assets)} 条",
            {"count": len(assets), "limit": MAX_PRELOAD_ASSETS},
        )

    # 4) ready_ms 必须为非负整数
    bad_ready = sorted(asset["id"] for asset in assets if asset["ready_ms"] < 0)
    if bad_ready:
        raise PreloadError(
            "INVALID_READY_MS",
            "素材 ready_ms 必须为非负整数",
            {"ids": bad_ready},
        )

    # 5) load_ms 必须为正整数
    bad_load = sorted(asset["id"] for asset in assets if asset["load_ms"] <= 0)
    if bad_load:
        raise PreloadError(
            "INVALID_DURATION",
            "素材 load_ms 必须为正数",
            {"ids": bad_load},
        )

    # 6) priority 必须为非负整数
    bad_priority = sorted(
        asset["id"] for asset in assets if asset["priority"] < 0
    )
    if bad_priority:
        raise PreloadError(
            "INVALID_PRIORITY",
            "素材 priority 必须为非负整数",
            {"ids": bad_priority},
        )

    # 7) 关联口令不能为空
    empty_command_ids = sorted(
        {asset["id"] for asset in assets if asset["command_id"] == ""}
    )
    if empty_command_ids:
        raise PreloadError(
            "INVALID_ID",
            "素材的 command_id 不能为空",
            {"ids": empty_command_ids},
        )

    # 8) 关联口令不得重复
    linked: dict[str, str] = {}
    duplicate_commands: list[str] = []
    for asset in assets:
        command_id = asset["command_id"]
        if command_id in linked:
            if command_id not in duplicate_commands:
                duplicate_commands.append(command_id)
            continue
        linked[command_id] = asset["id"]
    if duplicate_commands:
        raise PreloadError(
            "DUPLICATE_COMMAND_LINK",
            f"存在被多条素材关联的口令 id: {', '.join(sorted(duplicate_commands))}",
            {"command_ids": sorted(duplicate_commands)},
        )

    # 9) 关联口令必须存在
    unknown = sorted(command_id for command_id in linked if command_id not in starts)
    if unknown:
        raise PreloadError(
            "PRELOAD_TARGET_NOT_FOUND",
            f"素材关联了不存在的口令 id: {', '.join(unknown)}",
            {"command_ids": unknown},
        )

    return sorted(assets, key=lambda asset: asset["id"])


def _restore_order(
    chosen: int,
    loads: list[int],
    readies: list[int],
    deadlines: list[int],
) -> list[int]:
    """恢复入选集合的最优加载顺序（最早总完工优先，并列时加载 id 顺序字典序最小）。

    逐位置处理：枚举放在当前时刻 ``cursor`` 之后的候选素材 i（其自身须按时
    完工），用 ``_earliest_finish`` 求剩余素材自 i 完工时刻起的最早可行总完工
    时刻；先取总完工时刻最小的候选，并列时取 id 最小者。入选集合经全局可行性
    表确认可行，故每个位置必有候选。
    """
    order: list[int] = []
    remaining = chosen
    cursor = 0
    while remaining:
        best_finish = INF
        best_i = -1
        bits = remaining
        while bits:
            low = bits & -bits
            i = low.bit_length() - 1
            finish_i = max(cursor, readies[i]) + loads[i]
            if finish_i <= deadlines[i]:
                tail = _earliest_finish(
                    remaining ^ low, finish_i, loads, readies, deadlines
                )
                if tail < best_finish or (tail == best_finish and i < best_i):
                    best_finish = tail
                    best_i = i
            bits ^= low
        assert best_i >= 0
        order.append(best_i)
        remaining ^= 1 << best_i
        cursor = max(cursor, readies[best_i]) + loads[best_i]
    return order


def _earliest_finish(
    mask: int,
    at: int,
    loads: list[int],
    readies: list[int],
    deadlines: list[int],
) -> int:
    """加载器从时刻 ``at`` 起串行加载 ``mask`` 全部素材时的最早可行总完工时刻。

    不可行（某素材在任何排列下都无法在截止时刻前完工）时返回 INF。
    子集 DP：``finish[sub]`` 为从 ``at`` 起加载子集 ``sub`` 的最早可行完工时刻，
    枚举最后加载的素材 i，要求 i 不晚于 ``deadlines[i]`` 完工。空集立即完工。
    """
    finish = [INF] * (mask + 1)
    finish[0] = at
    for sub in range(1, mask + 1):
        if sub & ~mask:
            continue
        best = INF
        bits = sub
        while bits:
            low = bits & -bits
            i = low.bit_length() - 1
            prev = finish[sub ^ low]
            if prev < INF:
                end_i = max(prev, readies[i]) + loads[i]
                if end_i <= deadlines[i] and end_i < best:
                    best = end_i
            bits ^= low
        finish[sub] = best
    return finish[mask]
