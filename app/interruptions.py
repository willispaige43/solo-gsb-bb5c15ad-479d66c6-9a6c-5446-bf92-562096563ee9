"""插播起点规划核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 现有口令沿用 ``POST /compile`` 的口令数组，先用同一个编排器解析并校验每条口令的
  起止毫秒；插播本身是一条落在指定 ``channel`` 上、半开区间
  ``[start_ms, start_ms + duration_ms)`` 的新口令。
* ``start_ms`` 必须落在闭区间 ``[earliest_start_ms, latest_start_ms]`` 内的整数
  毫秒点上。
* 对某个起点 s，同通道内凡与插播半开区间重叠（``cmd.start < s + duration`` 且
  ``s < cmd.end``，首尾相接不算）的现有口令必须撤销；撤销还要递归包含其**全部
  锚点后继**（直接或间接以该口令为锚点的口令，跨通道同样递归）。
* 若该撤销闭包中包含任何不在 ``cancellable_ids`` 内的不可撤销口令，则此起点
  不可行。
* 在所有可行起点中选择**撤销条数最少**者；撤销条数并列时取**起点最早**者。
  闭包状态只在现有口令的“开始重叠 / 结束重叠”事件时刻改变，因此只评估这些
  事件点与窗口边界，整体 O(n log n)，绝不逐毫秒枚举。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .compiler import START, CompileError, compile_commands

# 可撤销口令叶子在“阻塞最大值”聚合中的哨兵（命中计数恒非负）
_NEG_INF = -(10**15)


class InterruptionError(CompileError):
    """插播规划失败的业务异常；复用 CompileError 的 422 转换通道。"""


def plan_interruption(
    commands: list[dict[str, Any]],
    channel: str,
    duration_ms: int,
    earliest_start_ms: int,
    latest_start_ms: int,
    cancellable_ids: list[str],
) -> dict[str, Any]:
    """在给定起点窗口内求撤销口令最少、并列起点最早的插播方案。

    不会修改传入的 ``commands`` / ``cancellable_ids``。

    :raises CompileError: 口令编排非法（重复 id、依赖环、负起点等，与 /compile 相同）。
    :raises InterruptionError: 插播参数非法（非正时长、空通道、起点窗口非法）、
        ``cancellable_ids`` 引用未知口令，或窗口内不存在可行起点时抛出，
        ``code`` 可用于程序化区分错误类型。
    """
    # 1) 复用编排器解析并校验现有口令（深拷贝，setdefault 等不会回写调用方数据；
    #    插播规划只需解析后的时间线，跳过 O(n²) 的冲突对枚举）
    compiled = compile_commands(deepcopy(commands), with_conflicts=False)
    entries = compiled["timeline"]
    by_id = {entry["id"]: entry for entry in entries}

    # 2) 插播参数校验
    if duration_ms <= 0:
        raise InterruptionError(
            "INVALID_DURATION",
            "插播 duration_ms 必须为正数",
            {"duration_ms": duration_ms},
        )
    if earliest_start_ms < 0 or latest_start_ms < 0:
        raise InterruptionError(
            "INVALID_REQUEST",
            "earliest_start_ms 与 latest_start_ms 都不得为负",
            {
                "earliest_start_ms": earliest_start_ms,
                "latest_start_ms": latest_start_ms,
            },
        )
    if latest_start_ms < earliest_start_ms:
        raise InterruptionError(
            "INVALID_REQUEST",
            "latest_start_ms 不得小于 earliest_start_ms",
            {
                "earliest_start_ms": earliest_start_ms,
                "latest_start_ms": latest_start_ms,
            },
        )
    if channel == "":
        raise InterruptionError(
            "INVALID_CHANNEL", "插播目标通道名不能为空", {"channel": channel}
        )

    # 3) cancellable_ids 校验：空 id / 重复 id / 未知 id
    cancellable = set(_validate_cancellable_ids(cancellable_ids, by_id))

    # 4) 以锚点关系建森林（START 为虚拟根），求每棵子树的前序遍历连续区间。
    #    口令 u 的全部锚点后继恰好是其在森林中的后代，对应欧拉序上的一段连续区间。
    children: dict[str, list[str]] = {START: []}
    for entry in entries:
        children.setdefault(entry["anchor"], []).append(entry["id"])
        children.setdefault(entry["id"], [])
    order, subtree_lo, subtree_hi = _euler_ranges(children)
    n = len(order)

    target_entries = [entry for entry in entries if entry["channel"] == channel]

    # 5) 事件扫描：同通道口令 [a, b) 与插播 [s, s+D) 重叠的整数起点范围为
    #    s ∈ [a-D+1, b-1]，故其后代区间在 x=a-D+1 处 +1、x=b 处 -1。
    tree = _CoverageTree(order, cancellable)
    events: list[tuple[int, int, int, int]] = []
    for entry in target_entries:
        cid = entry["id"]
        lo, hi = subtree_lo[cid], subtree_hi[cid]
        activate_at = entry["start_ms"] - duration_ms + 1
        deactivate_at = entry["end_ms"]
        events.append((activate_at, lo, hi, 1))
        events.append((deactivate_at, lo, hi, -1))
    events.sort(key=lambda event: event[0])

    best = _sweep(
        tree,
        events,
        window_lo=earliest_start_ms,
        window_hi=latest_start_ms,
        total=n,
    )

    # 6) 窗口内没有任何可行起点
    if best is None:
        blocking_ids = _blocking_ids(
            target_entries,
            order,
            subtree_lo,
            subtree_hi,
            cancellable,
            earliest_start_ms,
            latest_start_ms,
            duration_ms,
        )
        raise InterruptionError(
            "NO_FEASIBLE_INTERRUPTION",
            f"通道 {channel} 在闭区间 [{earliest_start_ms}, {latest_start_ms}] "
            "内不存在可行插播起点：撤销闭包中包含不可撤销口令",
            {
                "reason": "UNREVOCABLE_OVERLAP",
                "channel": channel,
                "duration_ms": duration_ms,
                "earliest_start_ms": earliest_start_ms,
                "latest_start_ms": latest_start_ms,
                "blocking_ids": blocking_ids,
            },
        )

    _, start_ms = best

    # 7) 对选定起点重算撤销闭包（此时闭包中的口令必然全部可撤销），按 id 排序
    cancelled_ids = _closure_at(
        target_entries,
        start_ms,
        duration_ms,
        order,
        subtree_lo,
        subtree_hi,
    )

    cancelled_set = set(cancelled_ids)
    timeline = [dict(entry) for entry in entries if entry["id"] not in cancelled_set]
    return {
        "start_ms": start_ms,
        "cancelled_ids": sorted(cancelled_ids),
        "timeline": timeline,
    }


def _validate_cancellable_ids(
    cancellable_ids: list[str], by_id: dict[str, dict[str, Any]]
) -> list[str]:
    """校验可撤销 id 列表并返回去重后的列表。"""
    empty_ids = [cid for cid in cancellable_ids if cid == ""]
    if empty_ids:
        raise InterruptionError(
            "INVALID_ID",
            "cancellable_ids 中的口令 id 不能为空",
            {"ids": empty_ids},
        )

    seen: set[str] = set()
    duplicates: list[str] = []
    for cid in cancellable_ids:
        if cid in seen and cid not in duplicates:
            duplicates.append(cid)
        seen.add(cid)
    if duplicates:
        raise InterruptionError(
            "DUPLICATE_ID",
            f"cancellable_ids 中存在重复的口令 id: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    unknown = sorted(cid for cid in cancellable_ids if cid not in by_id)
    if unknown:
        raise InterruptionError(
            "INTERRUPTION_TARGET_NOT_FOUND",
            f"cancellable_ids 引用了不存在的口令 id: {', '.join(unknown)}",
            {"ids": unknown},
        )

    return list(seen)


def _euler_ranges(
    children: dict[str, list[str]]
) -> tuple[list[str], dict[str, int], dict[str, int]]:
    """迭代 DFS 求前序欧拉序及每个节点（含 START）所含口令叶子的半开区间。

    虚拟根 START 不占位置；其区间覆盖全部口令 ``[0, n)``。
    """
    order: list[str] = []
    lo: dict[str, int] = {}
    hi: dict[str, int] = {}

    stack: list[tuple[str, int]] = [(START, 0)]
    while stack:
        node, state = stack.pop()
        if state == 0:
            if node != START:
                lo[node] = len(order)
                order.append(node)
            stack.append((node, 1))
            # 逆序压栈以按 id 字典序访问（结果仅用于确定性，与正确性无关）
            for child in sorted(children.get(node, ()), reverse=True):
                stack.append((child, 0))
        else:
            hi[node] = len(order)

    return order, lo, hi


class _CoverageTree:
    """对欧拉序区间做区间加法的懒惰线段树，同时维护两路聚合。

    * 全部叶子：``hit`` 为当前口令被多少个“与插播重叠的同通道祖先”覆盖，
      ``hit > 0`` 即在撤销闭包中；根上记录全局最小 ``hit`` 与其叶子数，
      ``hit == 0`` 的叶子即无需撤销的口令，由此得到撤销条数。
    * 仅不可撤销叶子参与的最大值聚合（可撤销叶子以 -∞ 填充）：根最大值
      ``> 0`` 表示闭包中存在不可撤销口令，当前起点不可行。

    两路聚合随同一组区间加法同步更新，因此只需一个懒惰标记。
    """

    def __init__(self, order: list[str], cancellable: set[str]):
        self._n = len(order)
        size = max(4 * self._n, 4)
        self._min = [0] * size
        self._min_count = [0] * size
        self._max_blocked = [0] * size
        self._lazy = [0] * size
        if self._n > 0:
            self._non_cancellable = [cid not in cancellable for cid in order]
            self._build(1, 0, self._n)

    def _build(self, node: int, left: int, right: int) -> None:
        if right - left == 1:
            self._min_count[node] = 1
            self._max_blocked[node] = (
                0 if self._non_cancellable[left] else _NEG_INF
            )
            return
        mid = (left + right) // 2
        self._build(node * 2, left, mid)
        self._build(node * 2 + 1, mid, right)
        self._pull(node)

    def _pull(self, node: int) -> None:
        lc, rc = node * 2, node * 2 + 1
        left_min, right_min = self._min[lc], self._min[rc]
        self._min[node] = min(left_min, right_min)
        count = 0
        if left_min == self._min[node]:
            count += self._min_count[lc]
        if right_min == self._min[node]:
            count += self._min_count[rc]
        self._min_count[node] = count
        self._max_blocked[node] = max(
            self._max_blocked[lc], self._max_blocked[rc]
        )

    def _apply(self, node: int, delta: int) -> None:
        self._min[node] += delta
        self._lazy[node] += delta
        if self._max_blocked[node] != _NEG_INF:
            self._max_blocked[node] += delta

    def _push(self, node: int) -> None:
        delta = self._lazy[node]
        if delta:
            self._apply(node * 2, delta)
            self._apply(node * 2 + 1, delta)
            self._lazy[node] = 0

    def add(
        self,
        qlo: int,
        qhi: int,
        delta: int,
        node: int = 1,
        left: int | None = None,
        right: int | None = None,
    ) -> None:
        """对 ``[qlo, qhi)`` 区间累加覆盖计数。"""
        if left is None:
            left = 0
        if right is None:
            right = self._n
        if qhi <= left or right <= qlo:
            return
        if qlo <= left and right <= qhi:
            self._apply(node, delta)
            return
        self._push(node)
        mid = (left + right) // 2
        self.add(qlo, qhi, delta, node * 2, left, mid)
        self.add(qlo, qhi, delta, node * 2 + 1, mid, right)
        self._pull(node)

    def feasible(self) -> bool:
        """当前覆盖状态下该起点是否可行（闭包中无不可撤销口令）。"""
        return self._n == 0 or self._max_blocked[1] <= 0

    def cancelled_count(self) -> int:
        """当前覆盖状态下撤销闭包的口令条数。"""
        if self._n == 0:
            return 0
        return self._n - (self._min_count[1] if self._min[1] == 0 else 0)


def _sweep(
    tree: _CoverageTree,
    events: list[tuple[int, int, int, int]],
    window_lo: int,
    window_hi: int,
    total: int,
) -> tuple[int, int] | None:
    """在事件点之间扫描恒定状态区间，返回 ``(撤销条数, 起点)`` 最优解。

    闭包状态只在事件坐标处改变：坐标 x 处的事件从起点 x 起生效（失活事件
    在 x 处即解除重叠，激活事件在 x 处即开始重叠）。先把 ``x < window_lo`` 的
    事件全部落入（含 x 为负的激活事件——起点窗口非负，这些口令在窗口起点处
    已经重叠），再逐段评估：状态在 ``[cur, next_x)`` 内恒定，可行时段内最早
    整数点 cur 即本段最优；随后应用 next_x 处事件并把 cur 推进到 next_x。
    扫描按起点递增，撤销条数相同时自然取最早起点。
    """
    i = 0
    event_count = len(events)

    while i < event_count and events[i][0] < window_lo:
        _, lo, hi, delta = events[i]
        tree.add(lo, hi, delta)
        i += 1

    window_end = window_hi + 1  # 窗口整数点的半开右端
    cur = window_lo
    best: tuple[int, int] | None = None

    while cur < window_end:
        next_x = events[i][0] if i < event_count else None
        seg_end = window_end if next_x is None else min(window_end, next_x)

        # [cur, seg_end) 内所有整数毫秒点状态相同；可行时最早点 cur 即本段最优
        if cur < seg_end and tree.feasible():
            candidate = (tree.cancelled_count() if total else 0, cur)
            if best is None or candidate < best:
                best = candidate

        if next_x is None or next_x > window_hi:
            break

        # 应用 next_x 处的全部事件，新状态自 next_x 起生效
        while i < event_count and events[i][0] == next_x:
            _, lo, hi, delta = events[i]
            tree.add(lo, hi, delta)
            i += 1
        cur = next_x

    return best


def _mark_subtrees(
    order: list[str],
    subtree_lo: dict[str, int],
    subtree_hi: dict[str, int],
    selected: list[str],
) -> set[str]:
    """在欧拉序上用差分数组标记 selected 口令的全部子树，返回被标记的口令集合。"""
    diff = [0] * (len(order) + 1)
    for cid in selected:
        lo, hi = subtree_lo[cid], subtree_hi[cid]
        diff[lo] += 1
        diff[hi] -= 1

    marked: set[str] = set()
    running = 0
    for idx, cid in enumerate(order):
        running += diff[idx]
        if running > 0:
            marked.add(cid)
    return marked


def _overlaps(entry: dict[str, Any], start_ms: int, duration_ms: int) -> bool:
    """半开区间 [entry.start, entry.end) 与 [start_ms, start_ms+duration_ms) 是否重叠。"""
    return entry["start_ms"] < start_ms + duration_ms and start_ms < entry["end_ms"]


def _closure_at(
    target_entries: list[dict[str, Any]],
    start_ms: int,
    duration_ms: int,
    order: list[str],
    subtree_lo: dict[str, int],
    subtree_hi: dict[str, int],
) -> list[str]:
    """指定起点处与插播重叠的同通道口令及其全部锚点后继（撤销闭包）。"""
    roots = [
        entry["id"]
        for entry in target_entries
        if _overlaps(entry, start_ms, duration_ms)
    ]
    return sorted(_mark_subtrees(order, subtree_lo, subtree_hi, roots))


def _blocking_ids(
    target_entries: list[dict[str, Any]],
    order: list[str],
    subtree_lo: dict[str, int],
    subtree_hi: dict[str, int],
    cancellable: set[str],
    earliest_start_ms: int,
    latest_start_ms: int,
    duration_ms: int,
) -> list[str]:
    """窗口内任一可行起点的闭包中可能出现的不可撤销口令 id（用于错误详情）。

    同通道口令 [a, b) 与插播在 s ∈ [L, R] 内存在重叠，当且仅当
    ``a < R + D`` 且 ``b > L``；取这些口令子树之并，再筛出不可撤销口令。
    """
    roots = [
        entry["id"]
        for entry in target_entries
        if entry["start_ms"] < latest_start_ms + duration_ms
        and entry["end_ms"] > earliest_start_ms
    ]
    union_closure = _mark_subtrees(order, subtree_lo, subtree_hi, roots)
    return sorted(cid for cid in union_closure if cid not in cancellable)
