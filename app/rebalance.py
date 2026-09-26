"""节目单延误分摊核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 节目段按提交顺序播出，每段有计划时长 ``planned_duration_sec`` 与最短时长
  ``min_duration_sec``；可压缩余量 = 计划时长 - 最短时长。
* 部分段落携带不可移动的固定开场时刻 ``fixed_start_at_sec``（固定点）。固定点把
  节目单切成若干窗口：每个窗口内的段落必须在窗口末尾的固定点时刻之前播完；
  最后一个固定点之后的段落没有窗口约束，保持计划时长。
* 已结束段提交实际结束时刻 ``actual_end_at_sec``，它决定窗口内剩余未播段的可用
  时间：可用时间 = 固定点时刻 - 窗口内最后一个已结束段的实际结束时刻。
* 窗口内未播段计划总时长超出可用时间时，超出部分（延误）按各段可压缩余量比例
  分摊压缩；整数秒余数按小数部分从大到小、同分按节目顺序分配；任何段不得压缩到
  最短时长以下。可用时间有富余时不拉伸任何段，富余秒数作为固定点前剩余秒数返回。
* 余量不足（延误超过可压缩余量总和）时抛出 ``INSUFFICIENT_MARGIN``，携带无法
  追回的秒数，由 HTTP 层统一转换为 422。
"""

from __future__ import annotations

from typing import Any

from .compiler import CompileError


class RebalanceError(CompileError):
    """延误分摊失败的业务异常；复用 CompileError 的 422 转换通道。"""


def rebalance_schedule(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """对节目单做延误分摊，返回调整后的各段时长与固定点前剩余秒数。

    不会修改传入的 ``segments``。

    :raises RebalanceError: 输入非法（重复 id、时长非法、固定点乱序、实际结束时刻
        不一致等）或可压缩余量不足时抛出，``code`` 可用于程序化区分错误类型。
    """
    _validate(segments)

    n = len(segments)
    fixed_idx = [
        i for i, s in enumerate(segments) if s.get("fixed_start_at_sec") is not None
    ]

    # 窗口划分：每个窗口 [lo, hi) 以固定点时刻 w_end 收尾（None 表示后面没有固定点）
    windows: list[tuple[int, int, int | None]] = []
    first_fixed = fixed_idx[0] if fixed_idx else n
    if 0 < first_fixed < n:
        windows.append((0, first_fixed, segments[first_fixed]["fixed_start_at_sec"]))
    for order, i in enumerate(fixed_idx):
        j = fixed_idx[order + 1] if order + 1 < len(fixed_idx) else n
        w_end = segments[j]["fixed_start_at_sec"] if j < n else None
        windows.append((i, j, w_end))

    adjusted: dict[int, int] = {}  # 段下标 -> 分摊后时长（仅未播段会被写入）
    fixed_points: list[dict[str, Any]] = []

    for lo, hi, w_end in windows:
        # 窗口起点：首段为固定点时用固定时刻，否则用时间原点 0；
        # 随后被窗口内已结束段的实际结束时刻依次推进
        first = segments[lo]
        pos = (
            first["fixed_start_at_sec"]
            if first.get("fixed_start_at_sec") is not None
            else 0
        )
        idx = lo
        while idx < hi and segments[idx].get("actual_end_at_sec") is not None:
            pos = segments[idx]["actual_end_at_sec"]
            idx += 1

        pending = list(range(idx, hi))
        planned = [segments[i]["planned_duration_sec"] for i in pending]

        if w_end is None:
            # 末尾没有固定点：无需压缩，也没有剩余秒数可汇报
            for i, p in zip(pending, planned):
                adjusted[i] = p
            continue

        need = sum(planned)
        avail = w_end - pos
        target = segments[hi]  # w_end 对应的固定点段（w_end 非 None 时 hi < n）

        if need <= avail:
            # 时间够用：不压缩，富余作为固定点前剩余秒数
            for i, p in zip(pending, planned):
                adjusted[i] = p
            fixed_points.append(
                {
                    "segment_id": target["id"],
                    "fixed_start_at_sec": w_end,
                    "absorbed_sec": 0,
                    "remaining_sec": avail - need,
                }
            )
            continue

        delay = need - avail
        margins = [
            segments[i]["planned_duration_sec"] - segments[i]["min_duration_sec"]
            for i in pending
        ]
        total_margin = sum(margins)
        if delay > total_margin:
            raise RebalanceError(
                "INSUFFICIENT_MARGIN",
                f"固定点 {target['id']}（{w_end}s）前可压缩余量不足，"
                f"仍有 {delay - total_margin} 秒无法追回",
                {
                    "unrecoverable_sec": delay - total_margin,
                    "required_compression_sec": delay,
                    "available_margin_sec": total_margin,
                    "fixed_point": {
                        "segment_id": target["id"],
                        "fixed_start_at_sec": w_end,
                    },
                },
            )

        allocations = _allocate(delay, margins)
        for k, i in enumerate(pending):
            adjusted[i] = planned[k] - allocations[k]
        fixed_points.append(
            {
                "segment_id": target["id"],
                "fixed_start_at_sec": w_end,
                "absorbed_sec": delay,
                "remaining_sec": 0,
            }
        )

    return {
        "segments": [
            {
                "id": s["id"],
                "planned_duration_sec": s["planned_duration_sec"],
                "adjusted_duration_sec": adjusted.get(i, s["planned_duration_sec"]),
                "compressed_sec": s["planned_duration_sec"]
                - adjusted.get(i, s["planned_duration_sec"]),
                "finished": s.get("actual_end_at_sec") is not None,
            }
            for i, s in enumerate(segments)
        ],
        "fixed_points": fixed_points,
    }


def _allocate(total: int, margins: list[int]) -> list[int]:
    """把 ``total`` 秒按余量比例分摊到各段，返回每段应压缩的秒数。

    先按 ``total * margin / 余量总和`` 向下取整，剩余秒数按小数部分从大到小、
    同分按下标（节目顺序）从小到大各补 1 秒。由于调用方保证
    ``total <= 余量总和``，分摊结果不会超过任何段的余量，即不会把任何段压到
    最短时长以下；余量为 0 的段小数部分为 0，永远不会被分到余数。
    """
    base = sum(margins)
    floors: list[int] = []
    frac_nums: list[int] = []  # 小数部分分子（公共分母为 base，可直接比较）
    for margin in margins:
        quotient, remainder = divmod(total * margin, base)
        floors.append(quotient)
        frac_nums.append(remainder)
    leftover = total - sum(floors)
    order = sorted(range(len(margins)), key=lambda k: (-frac_nums[k], k))
    allocations = list(floors)
    for k in order[:leftover]:
        allocations[k] += 1
    return allocations


def _validate(segments: list[dict[str, Any]]) -> None:
    """字段级与跨段一致性校验，非法输入抛出 ``RebalanceError``。"""
    # 1) 重复 id
    seen: set[str] = set()
    duplicates: list[str] = []
    for s in segments:
        cid = s["id"]
        if cid in seen and cid not in duplicates:
            duplicates.append(cid)
        seen.add(cid)
    if duplicates:
        raise RebalanceError(
            "DUPLICATE_ID",
            f"存在重复的节目段 id: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    # 2) 空 id
    empty_ids = [s["id"] for s in segments if s["id"] == ""]
    if empty_ids:
        raise RebalanceError("INVALID_ID", "节目段 id 不能为空", {"ids": empty_ids})

    # 3) 时长：计划时长与最短时长必须为正，且计划时长不得短于最短时长
    bad_duration = [
        s["id"]
        for s in segments
        if s["min_duration_sec"] <= 0
        or s["planned_duration_sec"] <= 0
        or s["planned_duration_sec"] < s["min_duration_sec"]
    ]
    if bad_duration:
        raise RebalanceError(
            "INVALID_DURATION",
            "planned_duration_sec 与 min_duration_sec 必须为正数，"
            "且计划时长不得短于最短时长",
            {"ids": bad_duration},
        )

    # 4) 固定开场时刻：非负，且按节目顺序严格递增
    bad_fixed = [
        s["id"]
        for s in segments
        if s.get("fixed_start_at_sec") is not None and s["fixed_start_at_sec"] < 0
    ]
    if bad_fixed:
        raise RebalanceError(
            "INVALID_FIXED_START", "固定开场时刻不得为负数", {"ids": bad_fixed}
        )
    fixed_seq = [
        (s["id"], s["fixed_start_at_sec"])
        for s in segments
        if s.get("fixed_start_at_sec") is not None
    ]
    not_increasing = [
        fixed_seq[k + 1][0]
        for k in range(len(fixed_seq) - 1)
        if fixed_seq[k + 1][1] <= fixed_seq[k][1]
    ]
    if not_increasing:
        raise RebalanceError(
            "INVALID_FIXED_START",
            "固定开场时刻必须按节目顺序严格递增",
            {"ids": not_increasing},
        )

    # 5) 实际结束时刻：非负；已结束段必须构成节目单的连续前缀，且结束时刻单调不减
    bad_actual = [
        s["id"]
        for s in segments
        if s.get("actual_end_at_sec") is not None and s["actual_end_at_sec"] < 0
    ]
    if bad_actual:
        raise RebalanceError(
            "INVALID_ACTUAL_END", "实际结束时刻不得为负数", {"ids": bad_actual}
        )

    seen_unfinished = False
    not_prefix = []
    for s in segments:
        if s.get("actual_end_at_sec") is None:
            seen_unfinished = True
        elif seen_unfinished:
            not_prefix.append(s["id"])
    if not_prefix:
        raise RebalanceError(
            "INVALID_ACTUAL_END",
            "已结束段必须是节目单的连续前缀（未播段之后不能再出现已结束段）",
            {"ids": not_prefix},
        )

    finished = [s for s in segments if s.get("actual_end_at_sec") is not None]
    decreasing = [
        finished[k + 1]["id"]
        for k in range(len(finished) - 1)
        if finished[k + 1]["actual_end_at_sec"] < finished[k]["actual_end_at_sec"]
    ]
    if decreasing:
        raise RebalanceError(
            "INVALID_ACTUAL_END",
            "已结束段的实际结束时刻必须按节目顺序单调不减",
            {"ids": decreasing},
        )

    # 6) 已结束的固定点段：实际结束时刻不得早于其固定开场时刻
    ended_before_start = [
        s["id"]
        for s in finished
        if s.get("fixed_start_at_sec") is not None
        and s["actual_end_at_sec"] < s["fixed_start_at_sec"]
    ]
    if ended_before_start:
        raise RebalanceError(
            "INVALID_ACTUAL_END",
            "已结束的固定点段实际结束时刻不得早于其固定开场时刻",
            {"ids": ended_before_start},
        )
