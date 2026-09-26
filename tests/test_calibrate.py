"""app.calibrate 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import copy
import random
import unittest

from app.calibrate import CalibrateError, _best_offset, calibrate_runs
from app.compiler import CompileError
from app.reconcile import ReconcileError


def cmd(cid, channel, anchor="START", offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def rec(rid, channel, start, end):
    return {
        "id": rid,
        "channel": channel,
        "actual_start_ms": start,
        "actual_end_ms": end,
    }


def calibrate(commands, records, tolerance=100):
    return calibrate_runs(commands, records, tolerance)


def channel_result(result, channel):
    for entry in result["channels"]:
        if entry["channel"] == channel:
            return entry
    raise AssertionError(f"通道 {channel} 不在结果中: {result}")


class BasicTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(calibrate([], []), {"channels": []})

    def test_exact_record_zero_offset(self):
        result = calibrate(
            [cmd("a", "main", duration=1000)],
            [rec("a", "main", 0, 1000)],
        )
        self.assertEqual(
            result["channels"],
            [
                {
                    "channel": "main",
                    "status": "calibrated",
                    "offset_ms": 0,
                    "consistent_ids": ["a"],
                    "anomalous_ids": [],
                }
            ],
        )

    def test_uniform_shift_is_recovered(self):
        # 两条记录起点偏差 20、终点偏差 40：容差 10 时公共整数偏移只有 30
        result = calibrate_runs(
            [
                cmd("a", "main", duration=1000),
                cmd("b", "main", anchor="a", duration=500),
            ],
            [rec("a", "main", 20, 1040), rec("b", "main", 1020, 1540)],
            10,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["status"], "calibrated")
        self.assertEqual(entry["offset_ms"], 30)
        self.assertEqual(entry["consistent_ids"], ["a", "b"])
        self.assertEqual(entry["anomalous_ids"], [])

    def test_anchor_resolution_is_reused(self):
        # b 锚定 a 结束时刻 +200 -> 计划 [1200, 1500)
        result = calibrate_runs(
            [
                cmd("a", "main", duration=1000),
                cmd("b", "main", anchor="a", offset=200, duration=300),
            ],
            [rec("a", "main", 0, 1000), rec("b", "main", 1200, 1500)],
            0,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["offset_ms"], 0)
        self.assertEqual(entry["consistent_ids"], ["a", "b"])

    def test_channels_without_records_are_omitted(self):
        result = calibrate(
            [cmd("a", "main"), cmd("c", "sub")],
            [rec("a", "main", 0, 1000)],
        )
        self.assertEqual([entry["channel"] for entry in result["channels"]], ["main"])

    def test_channels_sorted_by_name(self):
        result = calibrate(
            [cmd("a", "sub"), cmd("b", "main")],
            [rec("b", "main", 0, 1000), rec("a", "sub", 0, 1000)],
        )
        self.assertEqual(
            [entry["channel"] for entry in result["channels"]], ["main", "sub"]
        )

    def test_does_not_mutate_input(self):
        # 故意省略 anchor / offset_ms（/compile 会做 setdefault），调用后入参不应被改动
        commands = [{"id": "a", "channel": "main", "duration_ms": 1000}]
        records = [rec("a", "main", 0, 1000)]
        snapshot = copy.deepcopy((commands, records))
        calibrate(commands, records)
        self.assertEqual((commands, records), snapshot)


class OffsetSelectionTests(unittest.TestCase):
    def test_double_ended_tolerance_required(self):
        # 起点偏差 0、终点偏差 100，容差 50：单端在容差内不算，双端同时满足才算一致
        result = calibrate_runs(
            [cmd("a", "main", duration=1000)],
            [rec("a", "main", 0, 1100)],
            50,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["status"], "calibrated")
        self.assertEqual(entry["offset_ms"], 50)
        self.assertEqual(entry["consistent_ids"], ["a"])

    def test_deviation_equal_to_tolerance_is_consistent(self):
        # 偏差恰好等于容差（“不超过”含等号）：偏移 0 即可让双端都压线落入容差
        result = calibrate_runs(
            [cmd("a", "main", duration=1000)],
            [rec("a", "main", 100, 1100)],
            100,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["offset_ms"], 0)
        self.assertEqual(entry["consistent_ids"], ["a"])

    def test_majority_offset_wins_and_outlier_is_anomalous(self):
        # a、b 一致偏移约 +30，d 只有 0 附近可行：最优偏移 30，d 计为异常
        result = calibrate_runs(
            [
                cmd("a", "main", offset=0, duration=1000),
                cmd("b", "main", anchor="a", duration=500),
                cmd("d", "main", anchor="b", duration=500),
            ],
            [
                rec("a", "main", 30, 1030),
                rec("b", "main", 1040, 1540),
                rec("d", "main", 1500, 2000),
            ],
            10,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["offset_ms"], 30)
        self.assertEqual(entry["consistent_ids"], ["a", "b"])
        self.assertEqual(entry["anomalous_ids"], ["d"])

    def test_tie_prefers_smallest_absolute_offset(self):
        # 单条记录偏移区间 [20, 40]：整段一致数相同，取绝对值最小的 20
        result = calibrate_runs(
            [cmd("a", "main", duration=1000)],
            [rec("a", "main", 30, 1030)],
            10,
        )
        self.assertEqual(channel_result(result, "main")["offset_ms"], 20)

    def test_tie_prefers_negative_when_absolute_values_equal(self):
        # 两条记录分别只支持 +20 与 -20 附近：一致数、绝对值都并列，取数值较小者
        result = calibrate_runs(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=200, duration=100),
            ],
            [rec("a", "main", 30, 130), rec("b", "main", 170, 270)],
            10,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["offset_ms"], -20)
        self.assertEqual(entry["consistent_ids"], ["b"])
        self.assertEqual(entry["anomalous_ids"], ["a"])

    def test_zero_tolerance_requires_exact_shift(self):
        result = calibrate_runs(
            [cmd("a", "main", duration=1000)],
            [rec("a", "main", 7, 1007)],
            0,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["offset_ms"], 7)
        self.assertEqual(entry["consistent_ids"], ["a"])

    def test_unsatisfiable_record_marks_channel_uncalibratable(self):
        # 实播时长比计划长 100，双端容差 2*15=30 之内无任何整数偏移可满足
        result = calibrate_runs(
            [cmd("a", "main", duration=800)],
            [rec("a", "main", 0, 900)],
            15,
        )
        self.assertEqual(
            result["channels"],
            [
                {
                    "channel": "main",
                    "status": "uncalibratable",
                    "offset_ms": None,
                    "consistent_ids": [],
                    "anomalous_ids": ["a"],
                }
            ],
        )

    def test_unsatisfiable_record_counts_as_anomalous_when_others_calibrate(self):
        # a 可校准（偏移 0），b 时长偏差过大不可满足：通道仍可校准，b 计为异常
        result = calibrate_runs(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=200, duration=100),
            ],
            [rec("a", "main", 0, 100), rec("b", "main", 200, 400)],
            10,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["status"], "calibrated")
        self.assertEqual(entry["offset_ms"], 0)
        self.assertEqual(entry["consistent_ids"], ["a"])
        self.assertEqual(entry["anomalous_ids"], ["b"])

    def test_channels_are_estimated_independently(self):
        result = calibrate_runs(
            [cmd("a", "main", duration=1000), cmd("c", "sub", duration=800)],
            [rec("a", "main", 30, 1030), rec("c", "sub", 0, 900)],
            10,
        )
        main = channel_result(result, "main")
        self.assertEqual(main["status"], "calibrated")
        self.assertEqual(main["offset_ms"], 20)
        sub = channel_result(result, "sub")
        self.assertEqual(sub["status"], "uncalibratable")
        self.assertEqual(sub["offset_ms"], None)
        self.assertEqual(sub["anomalous_ids"], ["c"])

    def test_ids_sorted_in_output(self):
        # 记录乱序提交，一致 / 异常 id 列表仍按 id 升序
        result = calibrate_runs(
            [
                cmd("b", "main", offset=0, duration=100),
                cmd("a", "main", offset=200, duration=100),
            ],
            [rec("a", "main", 210, 310), rec("b", "main", 10, 110)],
            5,
        )
        entry = channel_result(result, "main")
        self.assertEqual(entry["offset_ms"], 5)
        self.assertEqual(entry["consistent_ids"], ["a", "b"])
        self.assertEqual(entry["anomalous_ids"], [])


class BestOffsetFuzzTests(unittest.TestCase):
    def test_best_offset_matches_brute_force(self):
        # 随机候选偏移区间，与逐点暴力枚举（一致数最多、|o| 最小、o 最小）对照
        rng = random.Random(20260925)
        for _ in range(500):
            intervals = []
            for _ in range(rng.randint(0, 6)):
                lo = rng.randint(-50, 50)
                hi = lo + rng.randint(0, 30)
                intervals.append((lo, hi))
            self.assertEqual(
                _best_offset(intervals),
                _brute_best_offset(intervals),
                f"intervals={intervals}",
            )


def _brute_best_offset(intervals):
    """逐点枚举的参照实现：一致数最多，并列取 |o| 最小、再取 o 较小者。"""
    best = None
    best_key = None
    for offset in range(-200, 201):
        count = sum(1 for lo, hi in intervals if lo <= offset <= hi)
        if count == 0:
            continue
        key = (-count, abs(offset), offset)
        if best_key is None or key < best_key:
            best_key = key
            best = offset
    return best


class ValidationTests(unittest.TestCase):
    def test_record_id_not_in_plan(self):
        with self.assertRaises(CalibrateError) as ctx:
            calibrate([cmd("a", "main")], [rec("x", "main", 0, 1000)])
        self.assertEqual(ctx.exception.code, "CALIBRATION_TARGET_NOT_FOUND")
        self.assertEqual(ctx.exception.details["ids"], ["x"])

    def test_channel_mismatch(self):
        with self.assertRaises(CalibrateError) as ctx:
            calibrate([cmd("a", "main")], [rec("a", "sub", 0, 1000)])
        self.assertEqual(ctx.exception.code, "CHANNEL_MISMATCH")
        self.assertEqual(ctx.exception.details["ids"], ["a"])
        self.assertEqual(
            ctx.exception.details["mismatches"],
            [{"id": "a", "plan_channel": "main", "record_channel": "sub"}],
        )

    def test_unknown_id_checked_before_channel_mismatch(self):
        with self.assertRaises(CalibrateError) as ctx:
            calibrate(
                [cmd("a", "main")],
                [rec("a", "sub", 0, 1000), rec("x", "main", 0, 1000)],
            )
        self.assertEqual(ctx.exception.code, "CALIBRATION_TARGET_NOT_FOUND")

    def test_negative_tolerance(self):
        with self.assertRaises(CalibrateError) as ctx:
            calibrate_runs([], [], -1)
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")

    def test_record_field_validation_is_reused(self):
        # 与 /runs/reconcile 相同的实播记录校验：重复 id、空 id、空通道、非法区间
        with self.assertRaises(ReconcileError) as ctx:
            calibrate(
                [cmd("a", "main")],
                [rec("a", "main", 0, 1000), rec("a", "main", 0, 1000)],
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")

        with self.assertRaises(ReconcileError) as ctx:
            calibrate([], [rec("", "main", 0, 100)])
        self.assertEqual(ctx.exception.code, "INVALID_ID")

        with self.assertRaises(ReconcileError) as ctx:
            calibrate([], [rec("r1", "", 0, 100)])
        self.assertEqual(ctx.exception.code, "INVALID_CHANNEL")

        with self.assertRaises(ReconcileError) as ctx:
            calibrate([], [rec("r1", "main", 500, 400)])
        self.assertEqual(ctx.exception.code, "INVALID_INTERVAL")

    def test_plan_errors_propagate(self):
        # 计划侧沿用 /compile 的校验与错误码
        with self.assertRaises(CompileError) as ctx:
            calibrate(
                [cmd("a", "main", anchor="b"), cmd("b", "main", anchor="a")],
                [],
            )
        self.assertEqual(ctx.exception.code, "DEPENDENCY_CYCLE")

    def test_plan_checked_before_records(self):
        # 计划与实播同时非法时，先报计划侧错误
        with self.assertRaises(CompileError) as ctx:
            calibrate(
                [cmd("a", "main", duration=0)],
                [rec("a", "main", 500, 400)],
            )
        self.assertEqual(ctx.exception.code, "INVALID_DURATION")


if __name__ == "__main__":
    unittest.main()
