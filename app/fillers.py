"""空档素材填充规划核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 现有口令沿用 ``POST /compile`` 的口令数组，先用同一个编排器解析并校验每条口令的
  起止毫秒；取目标通道口令的占用区间，裁剪到 ``[0, horizon_end_ms)`` 并求并集，
  其补集即为该通道的半开空档（至多 8 个，超出报错）。
* 每条素材（``id``、正整数 ``duration_ms``、非负整数 ``priority``）最多使用一次，
  在全部空档间**联合分配**：同一空档内素材时长之和不得超过空档长度。
* 目标依次为：① 填充总时长最大；② 并列时素材 ``priority`` 总和最大；③ 再并列时
  「按空档时间顺序、空档内按 id 升序展开」的素材 id 序列字典序最小。
* 各空档内素材按 id 升序从空档起点紧贴排布；返回每条入选素材的起止时刻与
  各空档填充后的剩余时长。

实现要点
--------
素材至多 12 条、空档至多 8 个，按空档逐层做子集动态规划：``dp[mask]`` 表示前若干
空档恰好装下素材集合 ``mask`` 时的最小序列编码。同一 ``mask`` 的时长与优先级总和
固定，故状态内只需比较序列；把 id 排名 + 1 作为 ``n + 1`` 进制的位值（位值 0 留作
末尾补齐的哨兵），「id 序列字典序最小」即转化为整数编码比较，无需保存与比较列表。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .compiler import CompileError, compile_commands

# 一次填充规划最多允许的素材条数
MAX_FILLERS = 12
# 视界内最多允许的空档个数
MAX_GAPS = 8


class FillerError(CompileError):
    """空档填充规划失败的业务异常；复用 CompileError 的 422 转换通道。"""


def plan_fillers(
    commands: list[dict[str, Any]],
    channel: str,
    horizon_end_ms: int,
    fillers: list[dict[str, Any]],
) -> dict[str, Any]:
    """在 ``[0, horizon_end_ms)`` 的空档内联合分配素材，求三目标最优的填充方案。

    不会修改传入的 ``commands`` / ``fillers``。

    :raises CompileError: 口令编排非法（重复 id、依赖环、负起点等，与 /compile 相同）。
    :raises FillerError: 目标通道为空、horizon 非正、素材非法（空 id、重复 id、
        超过 12 条、非正时长、负优先级）或空档超过 8 个时抛出，
        ``code`` 可用于程序化区分错误类型。
    """
    # 1) 复用编排器解析并校验现有口令（深拷贝，setdefault 等不会回写调用方数据；
    #    填充规划只需解析后的时间线，跳过 O(n²) 的冲突对枚举）
    compiled = compile_commands(deepcopy(commands), with_conflicts=False)

    # 2) 目标参数校验
    if channel == "":
        raise FillerError(
            "INVALID_CHANNEL", "目标通道名不能为空", {"channel": channel}
        )
    if horizon_end_ms <= 0:
        raise FillerError(
            "INVALID_HORIZON",
            "horizon_end_ms 必须为正数",
            {"horizon_end_ms": horizon_end_ms},
        )

    # 3) 素材校验，得到按 id 升序的 (id, duration_ms, priority) 列表
    items = _validate_fillers(fillers)

    # 4) 目标通道占用区间裁剪到视界内并求并集，补集即为半开空档（按时间升序）
    occupied = [
        (entry["start_ms"], min(entry["end_ms"], horizon_end_ms))
        for entry in compiled["timeline"]
        if entry["channel"] == channel and entry["start_ms"] < horizon_end_ms
    ]
    gaps = _find_gaps(occupied, horizon_end_ms)
    if len(gaps) > MAX_GAPS:
        raise FillerError(
            "TOO_MANY_GAPS",
            f"通道 {channel} 在 [0, {horizon_end_ms}) 内的空档数 "
            f"{len(gaps)} 超过上限 {MAX_GAPS}",
            {"channel": channel, "count": len(gaps), "limit": MAX_GAPS},
        )

    # 5) 多空档联合分配（素材 <= 12、空档 <= 8：逐空档子集动态规划）
    placements, remaining = _allocate(items, gaps)
    return {
        "placements": placements,
        "gaps": [
            {"start_ms": start, "end_ms": end, "remaining_ms": rem}
            for (start, end), rem in zip(gaps, remaining)
        ],
    }


def _validate_fillers(
    fillers: list[dict[str, Any]],
) -> list[tuple[str, int, int]]:
    """校验素材列表，返回按 id 升序排列的 (id, duration_ms, priority) 列表。"""
    # 1) 空 id
    empty_ids = [filler["id"] for filler in fillers if filler["id"] == ""]
    if empty_ids:
        raise FillerError(
            "INVALID_ID", "素材 id 不能为空", {"ids": empty_ids}
        )

    # 2) 重复 id
    seen: set[str] = set()
    duplicates: list[str] = []
    for filler in fillers:
        fid = filler["id"]
        if fid in seen and fid not in duplicates:
            duplicates.append(fid)
        seen.add(fid)
    if duplicates:
        raise FillerError(
            "DUPLICATE_ID",
            f"存在重复的素材 id: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    # 3) 条数上限
    if len(fillers) > MAX_FILLERS:
        raise FillerError(
            "TOO_MANY_FILLERS",
            f"最多 {MAX_FILLERS} 条素材参与一次填充规划，当前 {len(fillers)} 条",
            {"count": len(fillers), "limit": MAX_FILLERS},
        )

    # 4) 时长必须为正整数
    bad_duration = sorted(
        filler["id"] for filler in fillers if filler["duration_ms"] <= 0
    )
    if bad_duration:
        raise FillerError(
            "INVALID_DURATION",
            "素材 duration_ms 必须为正数",
            {"ids": bad_duration},
        )

    # 5) 优先级必须为非负整数
    bad_priority = sorted(
        filler["id"] for filler in fillers if filler["priority"] < 0
    )
    if bad_priority:
        raise FillerError(
            "INVALID_PRIORITY",
            "素材 priority 必须为非负整数",
            {"ids": bad_priority},
        )

    return sorted(
        (
            (filler["id"], filler["duration_ms"], filler["priority"])
            for filler in fillers
        ),
        key=lambda item: item[0],
    )


def _find_gaps(
    occupied: list[tuple[int, int]], horizon_end_ms: int
) -> list[tuple[int, int]]:
    """占用区间（已裁剪到视界内）求并集，返回 ``[0, horizon_end_ms)`` 内的半开空档。"""
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in sorted(occupied):
        if end <= cursor:
            continue  # 被并集中已有区间完全覆盖
        if start > cursor:
            gaps.append((cursor, start))
        cursor = end
    if cursor < horizon_end_ms:
        gaps.append((cursor, horizon_end_ms))
    return gaps


def _allocate(
    items: list[tuple[str, int, int]], gaps: list[tuple[int, int]]
) -> tuple[list[dict[str, Any]], list[int]]:
    """联合分配并返回 (placements, 各空档剩余时长)。

    ``items`` 已按 id 升序（下标即字典序排名）。空档内素材子集确定后，其排布
    顺序固定为 id 升序，故「按空档顺序展开的 id 序列」只取决于各空档的子集划分。
    """
    n = len(items)
    caps = [end - start for start, end in gaps]
    if n == 0:
        return [], list(caps)

    durations = [duration for _, duration, _ in items]
    priorities = [priority for _, _, priority in items]
    total_duration = sum(durations)

    # 捷径：某空档单独装得下全部素材时，全选且全部放入该空档即三目标同时最优
    # （时长、优先级总和已达上界；序列为全量 id 升序，是字典序下界）
    for g, cap in enumerate(caps):
        if cap >= total_duration:
            return _place_all(items, gaps, g)

    base = n + 1
    size = 1 << n
    pow_base = [1] * (n + 1)
    for k in range(1, n + 1):
        pow_base[k] = pow_base[k - 1] * base

    # 每个素材子集的时长和、优先级和、元素个数与序列编码（子集内 id 升序）
    popcount = [0] * size
    sub_dur = [0] * size
    sub_pri = [0] * size
    sub_code = [0] * size
    for mask in range(1, size):
        low = mask & -mask
        i = low.bit_length() - 1
        rest = mask ^ low
        popcount[mask] = popcount[rest] + 1
        sub_dur[mask] = sub_dur[rest] + durations[i]
        sub_pri[mask] = sub_pri[rest] + priorities[i]
        # 最低位对应最小 id，即子集序列的首元素（编码最高位）
        sub_code[mask] = (i + 1) * pow_base[popcount[rest]] + sub_code[rest]
    # shift[mask]：把该子集序列接到已有序列末尾时，已有编码需要左移的倍率
    shift = [pow_base[popcount[mask]] for mask in range(size)]

    # dp[mask] = 前 g 个空档恰好装下素材集合 mask 时的最小序列编码；
    # 同一 mask 的时长 / 优先级总和固定，故状态内只需比较编码。
    # 以大于任何合法编码（< base^n）的哨兵 INF 表示不可达，省去内层循环的空值判断
    INF = 1 << 62
    full = size - 1
    dp = [INF] * size
    dp[0] = 0
    parents: list[list[int]] = []
    for cap in caps:
        # 该空档容量下的可行子集标记（含空子集：跳过该空档）
        feasible = [sub_dur[mask] <= cap for mask in range(size)]
        next_dp = [INF] * size
        parent = [-1] * size
        for mask in range(size):
            code = dp[mask]
            if code == INF:
                continue
            # 空子集：该空档不装任何素材
            if code < next_dp[mask]:
                next_dp[mask] = code
                parent[mask] = 0
            # 枚举补集的非空子集作为本空档装入的素材（全体 (mask, sub) 对共 3^n 次）
            comp = full ^ mask
            sub = comp
            while sub:
                if feasible[sub]:
                    merged = mask | sub
                    cand = code * shift[sub] + sub_code[sub]
                    if cand < next_dp[merged]:
                        next_dp[merged] = cand
                        parent[merged] = sub
                sub = (sub - 1) & comp
        dp = next_dp
        parents.append(parent)

    # 最终裁决：先 (时长和, 优先级和) 最大，再末尾补哨兵后的完整编码最小
    best_mask = 0
    best_key: tuple[int, int, int] | None = None
    for mask in range(size):
        code = dp[mask]
        if code == INF:
            continue
        key = (
            sub_dur[mask],
            sub_pri[mask],
            -(code * pow_base[n - popcount[mask]]),
        )
        if best_key is None or key > best_key:
            best_key = key
            best_mask = mask

    # 回溯得到每个空档装入的素材子集
    chosen = [0] * len(gaps)
    rest = best_mask
    for g in range(len(gaps) - 1, -1, -1):
        sub = parents[g][rest]
        chosen[g] = sub
        rest ^= sub

    return _build_placements(items, gaps, chosen)


def _place_all(
    items: list[tuple[str, int, int]], gaps: list[tuple[int, int]], target: int
) -> tuple[list[dict[str, Any]], list[int]]:
    """全部素材放入同一个空档（容量足够时的捷径），序列即全量 id 升序。"""
    placements: list[dict[str, Any]] = []
    remaining = [end - start for start, end in gaps]
    cursor = gaps[target][0]
    used = 0
    for fid, duration, _ in items:
        placements.append(
            {"id": fid, "start_ms": cursor, "end_ms": cursor + duration}
        )
        cursor += duration
        used += duration
    remaining[target] -= used
    return placements, remaining


def _build_placements(
    items: list[tuple[str, int, int]],
    gaps: list[tuple[int, int]],
    chosen: list[int],
) -> tuple[list[dict[str, Any]], list[int]]:
    """按各空档的素材子集生成紧贴排布（空档内按 id 升序）与各空档剩余时长。"""
    n = len(items)
    placements: list[dict[str, Any]] = []
    remaining: list[int] = []
    for g, (start, end) in enumerate(gaps):
        cursor = start
        used = 0
        sub = chosen[g]
        for i in range(n):
            if sub >> i & 1:
                fid, duration, _ = items[i]
                placements.append(
                    {"id": fid, "start_ms": cursor, "end_ms": cursor + duration}
                )
                cursor += duration
                used += duration
        remaining.append((end - start) - used)
    return placements, remaining
