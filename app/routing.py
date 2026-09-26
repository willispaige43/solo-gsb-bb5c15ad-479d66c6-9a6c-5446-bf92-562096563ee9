"""通道改派核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 请求沿用 ``POST /compile`` 的口令数组，先用同一个编排器解析并校验每条口令的
  起止毫秒；改派只允许更换 ``channel``，**不得改变任何起止时刻**。
* 每个在 ``backups`` 中按 id 列出的口令，可以落到它的原通道或备用通道之一；
  未列出的口令只能留在原通道（原通道始终可选）。最多 12 条口令可拥有备用通道。
* 半开区间 ``[start_ms, end_ms)`` 时间重叠的口令不得分到同一通道，首尾相接
  （前者 end 等于后者 start）可以共用；不同通道互不影响。
* 在所有无冲突方案中全局选择**改派口令数最少**者；并列时取「按口令 id 排序后的
  通道名序列」字典序最小者。返回改派后的 timeline（仅 channel 可能变化）与
  实际改派的口令 id 列表。
"""

from __future__ import annotations

from bisect import bisect_left
from copy import deepcopy
from typing import Any

from .compiler import CompileError, compile_commands

# 最多允许多少条口令携带备用通道
MAX_BACKUP_TARGETS = 12


class RoutingError(CompileError):
    """通道改派失败的业务异常；复用 CompileError 的 422 转换通道。"""


def assign_routing(
    commands: list[dict[str, Any]],
    backups: list[dict[str, Any]],
) -> dict[str, Any]:
    """在不改变起止时刻的前提下求改派口令最少的无冲突通道方案。

    不会修改传入的 ``commands`` / ``backups``。

    :raises CompileError: 口令编排非法（重复 id、依赖环、负起点等，与 /compile 相同）。
    :raises RoutingError: 备用通道声明非法（未知 id、空串、重复通道、超出条数上限）
        或不存在可行方案时抛出，``code`` 可用于程序化区分错误类型。
    """
    # 1) 复用编排器解析并校验时间（深拷贝，setdefault 等不会回写调用方数据；
    #    改派只使用时间线，跳过 O(n²) 的冲突对枚举）
    compiled = compile_commands(deepcopy(commands), with_conflicts=False)
    entries = {entry["id"]: entry for entry in compiled["timeline"]}

    # 2) 校验备用通道声明，得到 id -> 备用通道列表
    backup_map = _validate_backups(backups, entries)

    movable_ids = sorted(backup_map)
    fixed_ids = [entry["id"] for entry in compiled["timeline"] if entry["id"] not in backup_map]

    # 3) 未列出的口令固定在原通道：若它们在同通道已经时间重叠，则任何方案都无解；
    #    同时构建各通道已占区间表（区间互不重叠，starts 与 ends 均单调递增）
    occupancy = _build_fixed_occupancy(entries, fixed_ids)

    # 4) 可移动口令的候选通道 = 原通道 ∪ 备用通道，剔除被固定口令占用的通道
    n = len(movable_ids)
    originals = [entries[cid]["channel"] for cid in movable_ids]
    intervals = [
        (entries[cid]["start_ms"], entries[cid]["end_ms"]) for cid in movable_ids
    ]
    domains: list[frozenset[str]] = []
    unassignable: list[str] = []
    for i, cid in enumerate(movable_ids):
        start, end = intervals[i]
        candidates = {originals[i]} | set(backup_map[cid])
        available = frozenset(
            ch
            for ch in candidates
            if not _channel_blocked(occupancy, ch, start, end)
        )
        domains.append(available)
        if not available:
            unassignable.append(cid)
    if unassignable:
        raise RoutingError(
            "NO_FEASIBLE_ROUTING",
            "存在原通道与全部备用通道都被时间重叠口令占用的口令，不存在无冲突改派方案",
            {"reason": "NO_AVAILABLE_CHANNEL", "ids": sorted(unassignable)},
        )

    # 5) 可移动口令之间的时间重叠图；同一条边的两个端点不能落到同一通道
    adjacency: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        si, ei = intervals[i]
        for j in range(i + 1, n):
            sj, ej = intervals[j]
            if si < ej and sj < ei:  # 半开区间重叠；首尾相接不算
                adjacency[i].add(j)
                adjacency[j].add(i)

    chosen: dict[int, str] = {}
    if n > 0:
        # 6) 一次分支限界搜索：最小化改派数，并列取按 id 排序通道序列字典序最小
        chosen = _solve(domains, originals, adjacency, intervals)
        if chosen is None:
            raise RoutingError(
                "NO_FEASIBLE_ROUTING",
                "不存在满足时间重叠约束的无冲突通道改派方案",
                {"reason": "INSUFFICIENT_CHANNEL_CAPACITY"},
            )

    # 7) 组装结果：timeline 仅 channel 可能变化，排序与起止时刻完全沿用编排结果
    new_channels = {movable_ids[i]: channel for i, channel in chosen.items()}
    timeline: list[dict[str, Any]] = []
    for entry in compiled["timeline"]:
        new_entry = dict(entry)
        if entry["id"] in new_channels:
            new_entry["channel"] = new_channels[entry["id"]]
        timeline.append(new_entry)

    changed_ids = sorted(
        cid for cid in new_channels if new_channels[cid] != entries[cid]["channel"]
    )
    return {"timeline": timeline, "changed_ids": changed_ids}


def _validate_backups(
    backups: list[dict[str, Any]], entries: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    """校验备用通道声明并返回去重保留顺序的 id -> 备用通道列表。"""
    # 1) 空 id
    empty_ids = [spec["id"] for spec in backups if spec["id"] == ""]
    if empty_ids:
        raise RoutingError(
            "INVALID_ID",
            "备用通道声明的口令 id 不能为空",
            {"ids": empty_ids},
        )

    # 2) 重复声明同一 id
    declared: dict[str, list[str]] = {}
    duplicate_ids: list[str] = []
    for spec in backups:
        cid = spec["id"]
        if cid in declared:
            if cid not in duplicate_ids:
                duplicate_ids.append(cid)
            continue
        declared[cid] = list(spec["backup_channels"])
    if duplicate_ids:
        raise RoutingError(
            "DUPLICATE_ID",
            f"存在重复的改派口令声明: {', '.join(sorted(duplicate_ids))}",
            {"duplicates": sorted(duplicate_ids)},
        )

    # 3) 未知 id（不存在于口令数组）
    unknown = sorted(cid for cid in declared if cid not in entries)
    if unknown:
        raise RoutingError(
            "ROUTING_TARGET_NOT_FOUND",
            f"备用通道声明引用了不存在的口令 id: {', '.join(unknown)}",
            {"ids": unknown},
        )

    # 4) 最多 12 条口令可携带备用通道
    if len(declared) > MAX_BACKUP_TARGETS:
        raise RoutingError(
            "TOO_MANY_BACKUP_TARGETS",
            f"最多 {MAX_BACKUP_TARGETS} 条口令可携带备用通道，"
            f"当前声明了 {len(declared)} 条",
            {"count": len(declared), "limit": MAX_BACKUP_TARGETS},
        )

    # 5) 备用通道名不能为空串
    empty_channels = sorted(
        cid for cid, channels in declared.items() if any(ch == "" for ch in channels)
    )
    if empty_channels:
        raise RoutingError(
            "INVALID_CHANNEL",
            "备用通道名不能为空",
            {"ids": empty_channels},
        )

    # 6) 同一条声明内备用通道不得重复（含与始终可选的原通道重复：原通道无需列出）
    duplicated_channels = sorted(
        cid
        for cid, channels in declared.items()
        if len(channels) != len(set(channels))
    )
    original_listed = sorted(
        cid
        for cid, channels in declared.items()
        if entries[cid]["channel"] in channels
    )
    duplicated = sorted(set(duplicated_channels) | set(original_listed))
    if duplicated:
        raise RoutingError(
            "DUPLICATE_BACKUP_CHANNEL",
            "同一条口令的备用通道列表中存在重复通道名"
            "（原通道始终可选，不应再列入备用通道）",
            {"ids": duplicated},
        )

    return declared


def _build_fixed_occupancy(
    entries: dict[str, dict[str, Any]], fixed_ids: list[str]
) -> dict[str, tuple[list[int], list[int]]]:
    """汇总不可改派口令在各原通道上的已占区间；固定口令自身重叠即无可行方案。"""
    grouped: dict[str, list[tuple[int, int]]] = {}
    for cid in fixed_ids:
        entry = entries[cid]
        grouped.setdefault(entry["channel"], []).append(
            (entry["start_ms"], entry["end_ms"])
        )

    occupancy: dict[str, tuple[list[int], list[int]]] = {}
    for channel, intervals in grouped.items():
        intervals.sort()
        starts = [interval[0] for interval in intervals]
        ends = [interval[1] for interval in intervals]
        # 固定口令无法挪走：同通道一旦重叠（首尾相接除外），任何方案都无法消除
        max_end = ends[0]
        for k in range(1, len(intervals)):
            if starts[k] < max_end:
                raise RoutingError(
                    "NO_FEASIBLE_ROUTING",
                    f"通道 {channel} 上不可改派的口令之间存在时间重叠，无可行改派方案",
                    {"reason": "FIXED_OVERLAP", "channel": channel},
                )
            if ends[k] > max_end:
                max_end = ends[k]
        occupancy[channel] = (starts, ends)
    return occupancy


def _channel_blocked(
    occupancy: dict[str, tuple[list[int], list[int]]],
    channel: str,
    start: int,
    end: int,
) -> bool:
    """半开区间 [start, end) 是否与该通道上已占的固定区间重叠。"""
    occupied = occupancy.get(channel)
    if occupied is None:
        return False
    starts, ends = occupied
    # 最后一个起点 < end 的固定区间；区间互不重叠，只需检查它的结束时刻
    i = bisect_left(starts, end) - 1
    return i >= 0 and ends[i] > start


def _solve(
    domains: list[frozenset[str]],
    originals: list[str],
    adjacency: list[set[int]],
    intervals: list[tuple[int, int]],
) -> dict[int, str] | None:
    """按下标顺序做深度优先搜索，返回全局最优通道分配；不可行返回 ``None``。

    目标按优先级：① 改派口令（所选通道 != 原通道）数最少；② 在改派数相同的
    方案中，按口令下标（id 升序）的通道名序列字典序最小。

    下标顺序赋值、通道按名字典序升序试探，因此找到第一个完整可行解时，它必然
    是当前预算下字典序最小的方案；预算（允许的改派数上限）从 0 起逐年抬升，
    首个可行预算给出的就是全局最优。

    剪枝与表示（可移动口令至多 12 个）：

    * 通道全局按名字典序编号，候选通道集合用位掩码表示；
    * 每个通道维护已占用它的下标集合（12 位整数），失败状态以
      ``(下一位置, 各通道占用表, 剩余预算)`` 记忆化，与赋值路径无关、可跨路径共享；
    * 前向检查：位置一旦发现某未赋值口令候选通道全部被重叠邻居占用即回溯；
      原通道已失效的口令数超过剩余预算时同样回溯；
    * 同原通道组的区间团下界：两两时间重叠且原通道相同的口令至多一个能保留，
      其余必须改派（区间团深度按起点覆盖数 O(g²) 计算）。
    """
    n = len(originals)

    # 全局通道集合：所有候选通道 ∪ 各口令原通道
    # （原通道可能被固定口令完全占用、不在任何候选集中，但编号时仍需包含）
    all_channels = sorted(set(originals) | set().union(*domains))
    index_of = {name: k for k, name in enumerate(all_channels)}
    m = len(all_channels)
    initial_dom = [
        sum(1 << index_of[name] for name in dom) for dom in domains
    ]
    original_bit = [1 << index_of[name] for name in originals]
    adj = [0] * n
    for i in range(n):
        for j in adjacency[i]:
            adj[i] |= 1 << j

    # 同原通道（通道编号 -> 下标列表），团下界只在这些组内才有意义
    groups_by_channel: dict[int, list[int]] = {}
    for i, bit in enumerate(original_bit):
        groups_by_channel.setdefault(bit.bit_length() - 1, []).append(i)

    def clique_move_lower_bound(pos: int) -> int:
        """位置 pos 起未赋值口令中，同原通道组所需的最小改派数下界。

        时间重叠图是区间图，团数等于同一点上的最大区间覆盖数（半开区间：起点
        计入、终点不计入）。同原通道的一个 k 团至多 1 个能留在原通道，其余
        k-1 个必须改派；若已赋值前缀中已有同原通道口令覆盖某个最大覆盖点，
        则该团 1 个也留不下，需要 k 个改派。
        """
        bound = 0
        for channel_k, members in groups_by_channel.items():
            pending = [i for i in members if i >= pos]
            if not pending:
                continue
            # 扫描每个起点处的覆盖深度，记录取到最大深度的代表点
            starts = sorted(intervals[i][0] for i in pending)
            max_depth = 0
            deepest_points: list[int] = []
            for point in starts:
                depth = sum(
                    1
                    for i in pending
                    if intervals[i][0] <= point < intervals[i][1]
                )
                if depth > max_depth:
                    max_depth = depth
                    deepest_points = [point]
                elif depth == max_depth:
                    deepest_points.append(point)
            keep = 1
            for h in members:
                if h >= pos or assigned_mask[h] != original_bit[h]:
                    continue
                hs, he = intervals[h]
                if any(hs <= point < he for point in deepest_points):
                    keep = 0
                    break
            bound = max(bound, max_depth - keep)
        return bound

    # 每轮预算的可变状态
    assigned_mask = [0] * n       # assigned_mask[i]：下标 i 已选通道的单位掩码
    occupancy = [0] * m           # occupancy[k]：占用通道 k 的下标集合
    solution: list[int] | None = None
    failed: set[tuple[Any, ...]] = set()

    def dfs(pos: int, remaining: int) -> bool:
        nonlocal solution
        if pos == n:
            solution = list(assigned_mask)
            return True

        key = (pos, tuple(occupancy), remaining)
        if key in failed:
            return False

        # 前向检查 + 必改派计数
        must_move = 0
        assigned_prefix_mask = (1 << pos) - 1
        for i in range(pos, n):
            used = 0
            holders = adj[i] & assigned_prefix_mask
            while holders:
                low = holders & -holders
                h = low.bit_length() - 1
                holders ^= low
                used |= assigned_mask[h]
            if initial_dom[i] & ~used == 0:
                failed.add(key)
                return False
            if not (initial_dom[i] & ~used & original_bit[i]):
                must_move += 1
        if must_move > remaining:
            failed.add(key)
            return False

        # 同原通道团下界
        if clique_move_lower_bound(pos) > remaining:
            failed.add(key)
            return False

        # 当前位置被已赋值重叠邻居占用的通道
        used_here = 0
        holders = adj[pos] & assigned_prefix_mask
        while holders:
            low = holders & -holders
            h = low.bit_length() - 1
            holders ^= low
            used_here |= assigned_mask[h]
        candidates = initial_dom[pos] & ~used_here

        while candidates:
            low = candidates & -candidates
            k = low.bit_length() - 1
            candidates ^= low
            cost = 0 if low == original_bit[pos] else 1
            if cost > remaining:
                continue

            assigned_mask[pos] = low
            old = occupancy[k]
            occupancy[k] = old | (1 << pos)
            if dfs(pos + 1, remaining - cost):
                return True
            occupancy[k] = old
            assigned_mask[pos] = 0

        failed.add(key)
        return False

    for budget in range(n + 1):
        failed.clear()
        occupancy = [0] * m
        for i in range(n):
            assigned_mask[i] = 0
        solution = None
        if dfs(0, budget):
            assert solution is not None
            return {
                i: all_channels[mask.bit_length() - 1]
                for i, mask in enumerate(solution)
            }
    return None
