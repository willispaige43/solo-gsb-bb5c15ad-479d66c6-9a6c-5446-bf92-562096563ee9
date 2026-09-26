"""回放窗口预算规划核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 现有口令沿用 ``POST /compile`` 的口令数组，先用同一个编排器解析并校验每条口令的
  起止毫秒；候选口令由其 ``id`` 指定，并各自携带一个非负整数 ``priority``。
* 每个候选口令生成一条**全局录制窗口**
  ``[max(0, start_ms - lead_in_ms), end_ms + lead_out_ms)``，
  即口令区间向前 / 向后各扩一段非负留白（前留白受时间原点 0 截断）。
* 全部候选窗口**跨通道**求并集：半开区间重叠或首尾相接（前一条结束时刻等于
  后一条起点）即合并为同一片段；合并后的总时长才计入预算。
* 在合并总时长不超过正数 ``budget_ms`` 的全部候选子集中，依次取
  ① 入选 ``priority`` 总和最大；② 入选条数最多；③ 入选 id 升序列表字典序最小。
  空集合始终可行（总时长 0）。

实现要点
--------
候选至多 12 条，按 id 升序编号后用位掩码枚举全部子集（至多 4096 个），逐子集
合并窗口并计算合并总时长；第三目标的 id 列表字典序与空档填充 / 素材预载一致，
转化为 ``n + 1`` 进制的整数编码（位值 0 留作末尾补齐的哨兵）比较。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .compiler import CompileError, compile_commands

# 一次回放规划最多允许的候选条数
MAX_REPLAY_CANDIDATES = 12


class ReplayError(CompileError):
    """回放窗口预算规划失败的业务异常；复用 CompileError 的 422 转换通道。"""


def plan_replays(
    commands: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    lead_in_ms: int,
    lead_out_ms: int,
    budget_ms: int,
) -> dict[str, Any]:
    """在预算内求优先级总和最大（并列条数、字典序裁决）的回放候选集合。

    不会修改传入的 ``commands`` / ``candidates``。

    :raises CompileError: 口令编排非法（重复 id、依赖环、负起点等，与 /compile 相同）。
    :raises ReplayError: 留白为负、预算非正、候选非法（空 id、重复 id、超过 12 条、
        负优先级、引用不存在的口令 id）时抛出，``code`` 可用于程序化区分错误类型。
    """
    # 1) 复用编排器解析并校验现有口令（深拷贝，setdefault 等不会回写调用方数据；
    #    回放规划只需解析后的起止时刻，跳过 O(n²) 的冲突对枚举）
    compiled = compile_commands(deepcopy(commands), with_conflicts=False)
    starts = {entry["id"]: entry["start_ms"] for entry in compiled["timeline"]}
    ends = {entry["id"]: entry["end_ms"] for entry in compiled["timeline"]}

    # 2) 留白与预算参数校验
    if lead_in_ms < 0 or lead_out_ms < 0:
        raise ReplayError(
            "INVALID_REPLAY_PADDING",
            "前留白 lead_in_ms 与后留白 lead_out_ms 必须为非负整数",
            {"lead_in_ms": lead_in_ms, "lead_out_ms": lead_out_ms},
        )
    if budget_ms <= 0:
        raise ReplayError(
            "INVALID_REPLAY_BUDGET",
            "回放时长预算 budget_ms 必须为正数",
            {"budget_ms": budget_ms},
        )

    # 3) 候选列表校验，得到按 id 升序排列的 (id, priority) 列表
    items = _validate_candidates(candidates, starts)

    n = len(items)
    # 4) 每个候选一条全局录制窗口：[max(0, 起点-前留白), 终点+后留白)
    windows = [
        (max(0, starts[cid] - lead_in_ms), ends[cid] + lead_out_ms)
        for cid, _ in items
    ]

    # 5) 位掩码枚举全部子集：跨通道合并窗口，合并总时长不超预算才可行
    size = 1 << n
    base = n + 1
    popcount = [0] * size
    sub_pri = [0] * size
    sub_code = [0] * size
    pow_base = [1] * (n + 1)
    for k in range(1, n + 1):
        pow_base[k] = pow_base[k - 1] * base

    priorities = [priority for _, priority in items]
    for mask in range(1, size):
        low = mask & -mask
        i = low.bit_length() - 1
        rest = mask ^ low
        popcount[mask] = popcount[rest] + 1
        sub_pri[mask] = sub_pri[rest] + priorities[i]
        # 最低位对应最小 id，即子集序列的首元素（编码最高位）
        sub_code[mask] = (i + 1) * pow_base[popcount[rest]] + sub_code[rest]

    best_mask = 0
    best_key: tuple[int, int, int] | None = None
    best_segments: list[tuple[int, int]] = []
    best_total = 0
    for mask in range(size):
        intervals = [
            windows[i] for i in range(n) if (mask >> i) & 1
        ]
        merged = _merge_intervals(intervals)
        total = sum(end - start for start, end in merged)
        if total > budget_ms:
            continue
        # 优先级总和最大 -> 入选条数最多 -> 入选 id 升序列表字典序最小
        key = (
            sub_pri[mask],
            popcount[mask],
            -(sub_code[mask] * pow_base[n - popcount[mask]]),
        )
        if best_key is None or key > best_key:
            best_key = key
            best_mask = mask
            best_segments = merged
            best_total = total

    return {
        "selected_ids": [items[i][0] for i in range(n) if (best_mask >> i) & 1],
        "segments": [
            {"start_ms": start, "end_ms": end} for start, end in best_segments
        ],
        "total_duration_ms": best_total,
    }


def _validate_candidates(
    candidates: list[dict[str, Any]], starts: dict[str, int]
) -> list[tuple[str, int]]:
    """校验候选列表，返回按 id 升序排列的 (id, priority) 列表。"""
    # 1) 空 id
    empty_ids = [cand["id"] for cand in candidates if cand["id"] == ""]
    if empty_ids:
        raise ReplayError(
            "INVALID_ID", "候选口令 id 不能为空", {"ids": empty_ids}
        )

    # 2) 重复 id
    seen: set[str] = set()
    duplicates: list[str] = []
    for cand in candidates:
        cid = cand["id"]
        if cid in seen and cid not in duplicates:
            duplicates.append(cid)
        seen.add(cid)
    if duplicates:
        raise ReplayError(
            "DUPLICATE_ID",
            f"存在重复的候选口令 id: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    # 3) 条数上限
    if len(candidates) > MAX_REPLAY_CANDIDATES:
        raise ReplayError(
            "TOO_MANY_REPLAY_CANDIDATES",
            f"最多 {MAX_REPLAY_CANDIDATES} 条候选口令参与一次回放规划，"
            f"当前 {len(candidates)} 条",
            {"count": len(candidates), "limit": MAX_REPLAY_CANDIDATES},
        )

    # 4) priority 必须为非负整数
    bad_priority = sorted(
        cand["id"] for cand in candidates if cand["priority"] < 0
    )
    if bad_priority:
        raise ReplayError(
            "INVALID_PRIORITY",
            "候选口令 priority 必须为非负整数",
            {"ids": bad_priority},
        )

    # 5) 候选 id 必须存在于现有口令中
    unknown = sorted(cand["id"] for cand in candidates if cand["id"] not in starts)
    if unknown:
        raise ReplayError(
            "REPLAY_TARGET_NOT_FOUND",
            f"候选口令 id 不存在于现有口令中: {', '.join(unknown)}",
            {"ids": unknown},
        )

    return sorted((cand["id"], cand["priority"]) for cand in candidates)


def _merge_intervals(
    intervals: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """半开区间求并集：重叠或首尾相接（lo <= 上一条 hi）即合并，按起点升序返回。"""
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            if hi > merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    return merged
