"""app.rebalance 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import copy
import unittest

from app.rebalance import RebalanceError, rebalance_schedule


def seg(cid, planned, minimum, fixed=None, actual_end=None):
    s = {
        "id": cid,
        "planned_duration_sec": planned,
        "min_duration_sec": minimum,
    }
    if fixed is not None:
        s["fixed_start_at_sec"] = fixed
    if actual_end is not None:
        s["actual_end_at_sec"] = actual_end
    return s


def by_id(result):
    return {s["id"]: s for s in result["segments"]}


class BasicTests(unittest.TestCase):
    def test_empty_schedule(self):
        result = rebalance_schedule([])
        self.assertEqual(result, {"segments": [], "fixed_points": []})

    def test_no_fixed_point_keeps_planned_durations(self):
        result = rebalance_schedule(
            [seg("a", 600, 300, actual_end=620), seg("b", 400, 200)]
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["adjusted_duration_sec"], 600)
        self.assertEqual(entries["b"]["adjusted_duration_sec"], 400)
        self.assertEqual(result["fixed_points"], [])

    def test_finished_flag_and_untouched_finished_segment(self):
        result = rebalance_schedule(
            [
                seg("a", 600, 300, actual_end=660),
                seg("b", 900, 600),
                seg("news", 300, 300, fixed=1800),
            ]
        )
        entries = by_id(result)
        self.assertTrue(entries["a"]["finished"])
        self.assertEqual(entries["a"]["adjusted_duration_sec"], 600)
        self.assertEqual(entries["a"]["compressed_sec"], 0)
        self.assertFalse(entries["b"]["finished"])

    def test_does_not_mutate_input(self):
        segments = [
            seg("a", 600, 300, actual_end=660),
            seg("b", 900, 600),
            seg("news", 300, 300, fixed=1800),
        ]
        snapshot = copy.deepcopy(segments)
        rebalance_schedule(segments)
        self.assertEqual(segments, snapshot)


class AllocationTests(unittest.TestCase):
    def test_delay_split_proportionally_by_margin(self):
        # opening 实际 660 结束（晚 60s）；窗口可用 1800-660=1140，
        # 计划 900+600=1500，延误 360；余量 300/300，各摊 180
        result = rebalance_schedule(
            [
                seg("opening", 600, 300, actual_end=660),
                seg("interview", 900, 600),
                seg("music", 600, 300),
                seg("news", 300, 300, fixed=1800),
            ]
        )
        entries = by_id(result)
        self.assertEqual(entries["interview"]["adjusted_duration_sec"], 720)
        self.assertEqual(entries["music"]["adjusted_duration_sec"], 420)
        self.assertEqual(entries["interview"]["compressed_sec"], 180)
        self.assertEqual(entries["music"]["compressed_sec"], 180)
        self.assertEqual(
            result["fixed_points"],
            [
                {
                    "segment_id": "news",
                    "fixed_start_at_sec": 1800,
                    "absorbed_sec": 360,
                    "remaining_sec": 0,
                }
            ],
        )

    def test_remainder_allocated_by_fraction_descending(self):
        # 延误 4，余量 [6,3,1]：精确值 2.4 / 1.2 / 0.4，下整 [2,1,0] 余 1 秒；
        # 小数部分 0.4(a) = 0.4(c) > 0.2(b)，a 与 c 同分按节目顺序，补给 a
        result = rebalance_schedule(
            [
                seg("a", 106, 100),
                seg("b", 103, 100),
                seg("c", 101, 100),
                seg("news", 50, 50, fixed=306),
            ]
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["compressed_sec"], 3)
        self.assertEqual(entries["b"]["compressed_sec"], 1)
        self.assertEqual(entries["c"]["compressed_sec"], 0)
        self.assertEqual(entries["a"]["adjusted_duration_sec"], 103)

    def test_remainder_tie_broken_by_program_order(self):
        # 延误 7，余量 [3,3,3]：精确值均为 2.33，下整各 2 余 1 秒；
        # 小数部分相同，按节目顺序补给最靠前的 a
        result = rebalance_schedule(
            [
                seg("a", 103, 100),
                seg("b", 103, 100),
                seg("c", 103, 100),
                seg("news", 50, 50, fixed=302),
            ]
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["compressed_sec"], 3)
        self.assertEqual(entries["b"]["compressed_sec"], 2)
        self.assertEqual(entries["c"]["compressed_sec"], 2)

    def test_never_compressed_below_min_duration(self):
        # 延误 1，余量 [1,1]：同分按顺序补给 a，a 恰好压到最短时长
        result = rebalance_schedule(
            [
                seg("a", 101, 100),
                seg("b", 101, 100),
                seg("news", 50, 50, fixed=201),
            ]
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["adjusted_duration_sec"], 100)
        self.assertEqual(entries["b"]["adjusted_duration_sec"], 101)

    def test_zero_margin_segment_gets_no_compression(self):
        # a 余量为 0，延误 50 全部由 b 吸收
        result = rebalance_schedule(
            [
                seg("a", 100, 100),
                seg("b", 200, 100),
                seg("news", 50, 50, fixed=250),
            ]
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["adjusted_duration_sec"], 100)
        self.assertEqual(entries["b"]["adjusted_duration_sec"], 150)


class WindowTests(unittest.TestCase):
    def test_slack_reported_as_remaining_sec(self):
        # opening 提前 100s 结束（500 < 计划 600）；可用 1300，计划 700
        result = rebalance_schedule(
            [
                seg("opening", 600, 300, actual_end=500),
                seg("a", 400, 300),
                seg("b", 300, 200),
                seg("news", 300, 300, fixed=1800),
            ]
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["adjusted_duration_sec"], 400)
        self.assertEqual(entries["b"]["adjusted_duration_sec"], 300)
        self.assertEqual(
            result["fixed_points"],
            [
                {
                    "segment_id": "news",
                    "fixed_start_at_sec": 1800,
                    "absorbed_sec": 0,
                    "remaining_sec": 600,
                }
            ],
        )

    def test_fixed_segment_compressed_within_own_window(self):
        # 固定段 a（1000 开场）自身也参与其窗口的压缩：
        # 窗口 [1000, 2000)，计划 800+400=1200，延误 200，余量 300/300 各摊 100
        result = rebalance_schedule(
            [
                seg("a", 800, 500, fixed=1000),
                seg("b", 400, 100),
                seg("c", 100, 100, fixed=2000),
            ]
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["adjusted_duration_sec"], 700)
        self.assertEqual(entries["b"]["adjusted_duration_sec"], 300)
        self.assertEqual(
            result["fixed_points"],
            [
                {
                    "segment_id": "c",
                    "fixed_start_at_sec": 2000,
                    "absorbed_sec": 200,
                    "remaining_sec": 0,
                }
            ],
        )

    def test_tail_after_last_fixed_point_keeps_planned(self):
        result = rebalance_schedule(
            [
                seg("a", 100, 100, fixed=0),
                seg("b", 100, 50, fixed=100),
                seg("c", 500, 400),
            ]
        )
        entries = by_id(result)
        self.assertEqual(entries["b"]["adjusted_duration_sec"], 100)
        self.assertEqual(entries["c"]["adjusted_duration_sec"], 500)
        # 只有 b 是「前面有窗口」的固定点
        self.assertEqual(
            result["fixed_points"],
            [
                {
                    "segment_id": "b",
                    "fixed_start_at_sec": 100,
                    "absorbed_sec": 0,
                    "remaining_sec": 0,
                }
            ],
        )

    def test_first_segment_fixed_has_no_pre_window(self):
        result = rebalance_schedule(
            [
                seg("a", 600, 300, fixed=3600),
                seg("b", 400, 200),
                seg("c", 100, 100, fixed=5000),
            ]
        )
        # 窗口 [3600, 5000)：计划 1000，可用 1400，富余 400
        self.assertEqual(
            result["fixed_points"],
            [
                {
                    "segment_id": "c",
                    "fixed_start_at_sec": 5000,
                    "absorbed_sec": 0,
                    "remaining_sec": 400,
                }
            ],
        )

    def test_multiple_windows_are_independent(self):
        result = rebalance_schedule(
            [
                seg("opening", 200, 100, actual_end=100),
                seg("a", 200, 100),
                seg("f1", 100, 100, fixed=400),
                seg("b", 600, 300),
                seg("f2", 100, 100, fixed=1000),
            ]
        )
        # 窗口 1：可用 300，计划 200，富余 100
        # 窗口 2：[400, 1000)，计划 100+600=700，延误 100，f1 余量 0，全摊给 b
        entries = by_id(result)
        self.assertEqual(entries["a"]["adjusted_duration_sec"], 200)
        self.assertEqual(entries["f1"]["adjusted_duration_sec"], 100)
        self.assertEqual(entries["b"]["adjusted_duration_sec"], 500)
        self.assertEqual(
            result["fixed_points"],
            [
                {
                    "segment_id": "f1",
                    "fixed_start_at_sec": 400,
                    "absorbed_sec": 0,
                    "remaining_sec": 100,
                },
                {
                    "segment_id": "f2",
                    "fixed_start_at_sec": 1000,
                    "absorbed_sec": 100,
                    "remaining_sec": 0,
                },
            ],
        )


class InsufficientMarginTests(unittest.TestCase):
    def test_insufficient_margin_reports_unrecoverable_sec(self):
        # opening 拖到 1700；可用 100，计划 500，延误 400，余量仅 50
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule(
                [
                    seg("opening", 600, 600, actual_end=1700),
                    seg("interview", 500, 450),
                    seg("news", 300, 300, fixed=1800),
                ]
            )
        exc = ctx.exception
        self.assertEqual(exc.code, "INSUFFICIENT_MARGIN")
        self.assertEqual(exc.details["unrecoverable_sec"], 350)
        self.assertEqual(exc.details["required_compression_sec"], 400)
        self.assertEqual(exc.details["available_margin_sec"], 50)
        self.assertEqual(
            exc.details["fixed_point"],
            {"segment_id": "news", "fixed_start_at_sec": 1800},
        )

    def test_finished_overshoot_without_pending_segments(self):
        # 已结束段实际结束时刻越过固定点，窗口内没有可压缩的段
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule(
                [
                    seg("a", 600, 600, actual_end=1900),
                    seg("news", 300, 300, fixed=1800),
                ]
            )
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_MARGIN")
        self.assertEqual(ctx.exception.details["unrecoverable_sec"], 100)

    def test_first_infeasible_window_is_reported(self):
        # 两个窗口都不足：报节目顺序更靠前的固定点
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule(
                [
                    seg("a", 600, 500),
                    seg("f1", 100, 100, fixed=100),
                    seg("b", 600, 500),
                    seg("f2", 100, 100, fixed=300),
                ]
            )
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_MARGIN")
        self.assertEqual(ctx.exception.details["fixed_point"]["segment_id"], "f1")


class ValidationTests(unittest.TestCase):
    def test_duplicate_id(self):
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule([seg("a", 100, 50), seg("a", 100, 50)])
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        self.assertEqual(ctx.exception.details["duplicates"], ["a"])

    def test_empty_id(self):
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule([seg("", 100, 50)])
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_planned_below_min(self):
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule([seg("a", 100, 200)])
        self.assertEqual(ctx.exception.code, "INVALID_DURATION")
        self.assertEqual(ctx.exception.details["ids"], ["a"])

    def test_non_positive_durations(self):
        for kwargs in ({"planned": 0, "minimum": 0}, {"planned": 100, "minimum": -1}):
            with self.assertRaises(RebalanceError) as ctx:
                rebalance_schedule(
                    [seg("a", kwargs["planned"], kwargs["minimum"])]
                )
            self.assertEqual(ctx.exception.code, "INVALID_DURATION")

    def test_negative_fixed_start(self):
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule([seg("a", 100, 50, fixed=-10)])
        self.assertEqual(ctx.exception.code, "INVALID_FIXED_START")

    def test_fixed_starts_must_be_strictly_increasing(self):
        for t2 in (90, 100):  # 倒退与相等都非法
            with self.assertRaises(RebalanceError) as ctx:
                rebalance_schedule(
                    [
                        seg("a", 100, 50, fixed=100),
                        seg("b", 100, 50, fixed=t2),
                    ]
                )
            self.assertEqual(ctx.exception.code, "INVALID_FIXED_START")
            self.assertEqual(ctx.exception.details["ids"], ["b"])

    def test_negative_actual_end(self):
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule([seg("a", 100, 50, actual_end=-1)])
        self.assertEqual(ctx.exception.code, "INVALID_ACTUAL_END")

    def test_finished_segments_must_form_prefix(self):
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule(
                [
                    seg("a", 100, 50, actual_end=100),
                    seg("b", 100, 50),
                    seg("c", 100, 50, actual_end=300),
                ]
            )
        self.assertEqual(ctx.exception.code, "INVALID_ACTUAL_END")
        self.assertEqual(ctx.exception.details["ids"], ["c"])

    def test_actual_ends_must_be_non_decreasing(self):
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule(
                [
                    seg("a", 100, 50, actual_end=200),
                    seg("b", 100, 50, actual_end=150),
                ]
            )
        self.assertEqual(ctx.exception.code, "INVALID_ACTUAL_END")
        self.assertEqual(ctx.exception.details["ids"], ["b"])

    def test_finished_fixed_segment_cannot_end_before_its_fixed_start(self):
        with self.assertRaises(RebalanceError) as ctx:
            rebalance_schedule(
                [seg("a", 100, 50, fixed=1000, actual_end=900)]
            )
        self.assertEqual(ctx.exception.code, "INVALID_ACTUAL_END")
        self.assertEqual(ctx.exception.details["ids"], ["a"])


if __name__ == "__main__":
    unittest.main()
