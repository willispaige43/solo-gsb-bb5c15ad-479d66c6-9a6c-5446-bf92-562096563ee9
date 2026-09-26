"""时钟偏移校准核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 计划侧是与 ``POST /compile`` 相同的口令数组，复用 ``app.compiler`` 的锚点解析
  得到每条口令的计划起止毫秒；实播记录复用 ``app.reconcile`` 的字段级校验
  （重复 id、空 id、空通道、区间非法）。
* 实播记录与计划口令按 **id 一一对应**：记录 id 必须出现在计划中，且通道
  必须与该口令一致，否则返回结构化 422。
* 每个通道独立估计 **一个整数时钟偏移** ``offset_ms``：一条记录仅当
  「计划起点 + 偏移」与「计划终点 + 偏移」同时落入实播起止的
  ``±tolerance_ms`` 范围内（双端容差）时，才对该偏移计为一致。
* 偏移选择 **一致记录数最多** 的整数；并列时先取 **绝对值最小** 者，
  再取 **数值较小** 者。
* 某通道所有记录都无法被任何整数偏移满足双端容差时，该通道标为
  ``uncalibratable``（无法校准），``offset_ms`` 为 ``None``，
  全部记录计入异常。
"""

from __future__ import annotations

from typing import Any

from .compiler import CompileError, compile_commands
from .reconcile import validate_records


class CalibrateError(CompileError):
    """时钟校准失败的业务异常；复用 CompileError 的 422 转换通道。"""


def calibrate_runs(
    commands: list[dict[str, Any]],
    records: list[dict[str, Any]],
    tolerance_ms: int,
) -> dict[str, Any]:
    """逐通道估计整数时钟偏移，并按估计结果划分一致 / 异常记录。

    不会修改传入的 ``commands`` / ``records``。

    :raises CompileError: 计划口令非法（重复 id、依赖环等，与 /compile 相同）。
    :raises ReconcileError: 实播记录字段级非法（重复 id、空 id、空通道、
        区间非法），与 /runs/reconcile 相同。
    :raises CalibrateError: 容差为负、记录 id 未出现在计划中、或记录通道与
        计划口令不一致时抛出，``code`` 可用于程序化区分错误类型。
    """
    if tolerance_ms < 0:
        raise CalibrateError(
            "INVALID_REQUEST",
            "tolerance_ms 必须是非负数",
            {"tolerance_ms": tolerance_ms},
        )

    # 1) 计划侧：复用 /compile 的锚点解析（拷贝一份，避免 setdefault 改动调用方数据；
    #    校准只使用时间线，跳过 O(n²) 的冲突对枚举）
    timeline = compile_commands(
        [dict(item) for item in commands], with_conflicts=False
    )["timeline"]
    plan_by_id = {entry["id"]: entry for entry in timeline}

    # 2) 实播记录字段级校验（与 /runs/reconcile 相同）
    validate_records(records)

    # 3) 记录 id 必须出现在计划中，且通道与计划口令一致
    unknown = sorted(record["id"] for record in records if record["id"] not in plan_by_id)
    if unknown:
        raise CalibrateError(
            "CALIBRATION_TARGET_NOT_FOUND",
            f"实播记录 id 未出现在计划口令中: {', '.join(unknown)}",
            {"ids": unknown},
        )
    mismatched = sorted(
        record["id"]
        for record in records
        if record["channel"] != plan_by_id[record["id"]]["channel"]
    )
    if mismatched:
        record_channel = {record["id"]: record["channel"] for record in records}
        raise CalibrateError(
            "CHANNEL_MISMATCH",
            f"实播记录通道与计划口令通道不一致: {', '.join(mismatched)}",
            {
                "ids": mismatched,
                "mismatches": [
                    {
                        "id": rid,
                        "plan_channel": plan_by_id[rid]["channel"],
                        "record_channel": record_channel[rid],
                    }
                    for rid in mismatched
                ],
            },
        )

    # 4) 每条记录换算成「可计为一致」的整数偏移闭区间：
    #    |actual_start - (plan_start + o)| <= tol 且 |actual_end - (plan_end + o)| <= tol
    #    即 o ∈ [start_dev - tol, start_dev + tol] ∩ [end_dev - tol, end_dev + tol]
    intervals_by_channel: dict[str, list[tuple[str, int, int]]] = {}
    for record in records:
        plan = plan_by_id[record["id"]]
        start_dev = record["actual_start_ms"] - plan["start_ms"]
        end_dev = record["actual_end_ms"] - plan["end_ms"]
        lo = max(start_dev, end_dev) - tolerance_ms
        hi = min(start_dev, end_dev) + tolerance_ms
        intervals_by_channel.setdefault(record["channel"], []).append(
            (record["id"], lo, hi)
        )

    # 5) 各通道独立选择最优偏移，划分一致 / 异常记录
    channels: list[dict[str, Any]] = []
    for channel in sorted(intervals_by_channel):
        entries = intervals_by_channel[channel]
        offset = _best_offset([(lo, hi) for _, lo, hi in entries if lo <= hi])
        if offset is None:
            # 没有任何记录能被某个整数偏移满足双端容差：无法校准
            channels.append(
                {
                    "channel": channel,
                    "status": "uncalibratable",
                    "offset_ms": None,
                    "consistent_ids": [],
                    "anomalous_ids": sorted(rid for rid, _, _ in entries),
                }
            )
            continue
        consistent = sorted(
            rid for rid, lo, hi in entries if lo <= offset <= hi
        )
        anomalous = sorted(
            rid for rid, lo, hi in entries if not lo <= offset <= hi
        )
        channels.append(
            {
                "channel": channel,
                "status": "calibrated",
                "offset_ms": offset,
                "consistent_ids": consistent,
                "anomalous_ids": anomalous,
            }
        )

    return {"channels": channels}


def _best_offset(intervals: list[tuple[int, int]]) -> int | None:
    """在整数偏移中选被最多闭区间覆盖者；并列先取绝对值最小，再取数值较小者。

    ``intervals`` 为若干闭区间 ``(lo, hi)``（``lo <= hi``）；没有可用区间时
    返回 ``None``。按事件点扫描，不逐毫秒枚举。
    """
    events: list[tuple[int, int]] = []
    for lo, hi in intervals:
        events.append((lo, 1))
        events.append((hi + 1, -1))
    if not events:
        return None
    events.sort()

    best_count = 0
    best_offset: int | None = None
    count = 0
    i = 0
    n = len(events)
    while i < n:
        pos = events[i][0]
        while i < n and events[i][0] == pos:
            count += events[i][1]
            i += 1
        # 偏移区间 [pos, 下一事件点 - 1] 内覆盖数恒为 count
        seg_lo = pos
        seg_hi = events[i][0] - 1 if i < n else None
        if count < best_count:
            continue
        # 段内取 (|o|, o) 最小的代表点
        if seg_lo <= 0 and (seg_hi is None or seg_hi >= 0):
            candidate = 0
        elif seg_hi is not None and seg_hi < 0:
            candidate = seg_hi
        else:
            candidate = seg_lo
        if (
            count > best_count
            or (abs(candidate), candidate) < (abs(best_offset), best_offset)
        ):
            best_count = count
            best_offset = candidate
    return best_offset
