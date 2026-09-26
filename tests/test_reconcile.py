"""app.reconcile 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import copy
import unittest

from app.compiler import CompileError
from app.reconcile import ReconcileError, reconcile_runs


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


def reconcile(commands, records, tolerance=100, miss=1000, extra=1000):
    return reconcile_runs(commands, records, tolerance, miss, extra)


def pair_ids(result):
    return [(m["plan_id"], m["actual_id"]) for m in result["matches"]]


class BasicTests(unittest.TestCase):
    def test_empty_input(self):
        result = reconcile([], [])
        self.assertEqual(
            result,
            {
                "matches": [],
                "missed_plan_ids": [],
                "extra_actual_ids": [],
                "total_penalty": 0,
            },
        )

    def test_exact_match_zero_penalty(self):
        result = reconcile(
            [cmd("a", "main", duration=1000)],
            [rec("r1", "main", 0, 1000)],
        )
        self.assertEqual(result["total_penalty"], 0)
        self.assertEqual(len(result["matches"]), 1)
        match = result["matches"][0]
        self.assertEqual(match["plan_id"], "a")
        self.assertEqual(match["actual_id"], "r1")
        self.assertEqual(match["channel"], "main")
        self.assertEqual(match["planned_interval"], [0, 1000])
        self.assertEqual(match["actual_interval"], [0, 1000])
        self.assertEqual(match["start_deviation_ms"], 0)
        self.assertEqual(match["end_deviation_ms"], 0)
        self.assertEqual(result["missed_plan_ids"], [])
        self.assertEqual(result["extra_actual_ids"], [])

    def test_signed_deviations_but_absolute_cost(self):
        # 实播比计划晚 30ms 开始、早 20ms 结束：带符号偏差 30 / -20，代价 30 + 20 = 50
        result = reconcile(
            [cmd("a", "main", offset=1000, duration=1000)],
            [rec("r1", "main", 1030, 1980)],
        )
        match = result["matches"][0]
        self.assertEqual(match["start_deviation_ms"], 30)
        self.assertEqual(match["end_deviation_ms"], -20)
        self.assertEqual(result["total_penalty"], 50)

    def test_anchor_resolution_is_reused(self):
        # a [0,1000)；b 锚定 a 结束时刻 +200 -> [1200,1500)
        result = reconcile(
            [
                cmd("a", "main", duration=1000),
                cmd("b", "main", anchor="a", offset=200, duration=300),
            ],
            [rec("r1", "main", 0, 1000), rec("r2", "main", 1200, 1500)],
        )
        self.assertEqual(result["total_penalty"], 0)
        self.assertEqual(pair_ids(result), [("a", "r1"), ("b", "r2")])

    def test_does_not_mutate_input(self):
        # 故意省略 anchor / offset_ms（/compile 会做 setdefault），调用后入参不应被改动
        commands = [{"id": "a", "channel": "main", "duration_ms": 1000}]
        records = [rec("r1", "main", 0, 1000)]
        snapshot = copy.deepcopy((commands, records))
        reconcile(commands, records)
        self.assertEqual((commands, records), snapshot)


class MatchingTests(unittest.TestCase):
    def test_tolerance_blocks_match(self):
        # 终点偏差 200 超过容差 100：不可匹配，按漏播 + 额外播出计
        result = reconcile_runs(
            [cmd("a", "main", duration=1000)],
            [rec("r1", "main", 0, 1200)],
            100,
            30,
            40,
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["missed_plan_ids"], ["a"])
        self.assertEqual(result["extra_actual_ids"], ["r1"])
        self.assertEqual(result["total_penalty"], 70)

    def test_deviation_equal_to_tolerance_is_allowed(self):
        # 起点偏差恰好等于容差（“不超过”含等号）：允许匹配，代价 100 + 50 = 150
        result = reconcile_runs(
            [cmd("a", "main", duration=1000)],
            [rec("r1", "main", 100, 1050)],
            100,
            5000,
            5000,
        )
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["total_penalty"], 150)

    def test_both_deviations_must_be_within_tolerance(self):
        # 起点偏差在容差内、终点偏差超出容差：仍不可匹配
        result = reconcile_runs(
            [cmd("a", "main", duration=1000)],
            [rec("r1", "main", 10, 1200)],
            100,
            5000,
            5000,
        )
        self.assertEqual(result["matches"], [])

    def test_channels_are_isolated(self):
        # 区间完全相同但不同通道：不能跨通道匹配
        result = reconcile(
            [cmd("a", "main", duration=1000)],
            [rec("r1", "sub", 0, 1000)],
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["missed_plan_ids"], ["a"])
        self.assertEqual(result["extra_actual_ids"], ["r1"])
        self.assertEqual(result["total_penalty"], 2000)

    def test_records_sorted_by_start_then_id(self):
        # 实播记录乱序提交，仍按 (起点, id) 排序后保序匹配
        result = reconcile(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=200, duration=100),
            ],
            [rec("q2", "main", 200, 300), rec("q1", "main", 0, 100)],
            tolerance=0,
        )
        self.assertEqual(pair_ids(result), [("a", "q1"), ("b", "q2")])
        self.assertEqual(result["total_penalty"], 0)

    def test_finds_global_optimum_not_greedy(self):
        # 全部匹配代价 190；只匹配 a-y，再各计一次罚分只需 20
        # a [0,100)、b [200,300)；排序后实播 y [0,100)、x [150,160)，容差 200
        result = reconcile_runs(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=200, duration=100),
            ],
            [rec("x", "main", 150, 160), rec("y", "main", 0, 100)],
            200,
            10,
            10,
        )
        self.assertEqual(pair_ids(result), [("a", "y")])
        self.assertEqual(result["missed_plan_ids"], ["b"])
        self.assertEqual(result["extra_actual_ids"], ["x"])
        self.assertEqual(result["total_penalty"], 20)

    def test_only_missed_plans(self):
        result = reconcile_runs(
            [cmd("a", "main", duration=100)], [], 0, 5, 9
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["missed_plan_ids"], ["a"])
        self.assertEqual(result["extra_actual_ids"], [])
        self.assertEqual(result["total_penalty"], 5)

    def test_only_extra_records(self):
        result = reconcile_runs(
            [], [rec("r1", "main", 0, 100)], 0, 5, 9
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["missed_plan_ids"], [])
        self.assertEqual(result["extra_actual_ids"], ["r1"])
        self.assertEqual(result["total_penalty"], 9)

    def test_multi_channel_aggregation(self):
        # main：a-r1 偏差 10+20=30；sub：r3 终点偏差 100 超容差，c 漏播、r3 额外
        result = reconcile_runs(
            [
                cmd("a", "main", duration=1000),
                cmd("c", "sub", duration=800),
            ],
            [
                rec("r1", "main", 10, 1020),
                rec("r2", "sub", 0, 900),
            ],
            50,
            200,
            300,
        )
        self.assertEqual(pair_ids(result), [("a", "r1")])
        self.assertEqual(result["missed_plan_ids"], ["c"])
        self.assertEqual(result["extra_actual_ids"], ["r2"])
        self.assertEqual(result["total_penalty"], 30 + 200 + 300)


class TieBreakTests(unittest.TestCase):
    def test_tie_prefers_more_matches(self):
        # p1 [0,100)、p2 [200,400)；q1 [0,100)、q2 [250,450)，容差 50，罚分各 50。
        # 全部匹配：0 + 100 = 100（2 对）；只匹配 p1：0 + 50 + 50 = 100（1 对）。
        # 罚分相同，取匹配数更多者
        result = reconcile_runs(
            [
                cmd("p1", "main", offset=0, duration=100),
                cmd("p2", "main", offset=200, duration=200),
            ],
            [rec("q1", "main", 0, 100), rec("q2", "main", 250, 450)],
            50,
            50,
            50,
        )
        self.assertEqual(pair_ids(result), [("p1", "q1"), ("p2", "q2")])
        self.assertEqual(result["missed_plan_ids"], [])
        self.assertEqual(result["extra_actual_ids"], [])
        self.assertEqual(result["total_penalty"], 100)

    def test_tie_prefers_lexicographically_smaller_actual_id(self):
        # 两条实播区间完全相同：匹配哪条罚分都一样，取 id 对字典序更小者
        result = reconcile_runs(
            [cmd("a", "main", duration=100)],
            [rec("q2", "main", 0, 100), rec("q1", "main", 0, 100)],
            0,
            100,
            100,
        )
        self.assertEqual(pair_ids(result), [("a", "q1")])
        self.assertEqual(result["missed_plan_ids"], [])
        self.assertEqual(result["extra_actual_ids"], ["q2"])
        self.assertEqual(result["total_penalty"], 100)

    def test_tie_prefers_lexicographically_smaller_plan_id(self):
        # 两个同起点计划都能匹配唯一实播：取 (计划 id, 实播 id) 更小者
        result = reconcile_runs(
            [
                cmd("pb", "main", duration=100),
                cmd("pa", "main", duration=100),
            ],
            [rec("q", "main", 0, 100)],
            0,
            100,
            100,
        )
        self.assertEqual(pair_ids(result), [("pa", "q")])
        self.assertEqual(result["missed_plan_ids"], ["pb"])
        self.assertEqual(result["extra_actual_ids"], [])
        self.assertEqual(result["total_penalty"], 100)

    def test_zero_penalties_still_match_exact_records(self):
        # 罚分为 0 时，完全一致（代价 0）与互不匹配（罚分 0）同分，取匹配数更多者
        result = reconcile_runs(
            [cmd("a", "main", duration=100)],
            [rec("r1", "main", 0, 100)],
            0,
            0,
            0,
        )
        self.assertEqual(pair_ids(result), [("a", "r1")])
        self.assertEqual(result["total_penalty"], 0)

    def test_zero_penalties_skip_costly_match(self):
        # 罚分为 0 时，带偏差的匹配代价 30 高于互不匹配（0），选择不匹配
        result = reconcile_runs(
            [cmd("a", "main", duration=100)],
            [rec("r1", "main", 10, 120)],
            100,
            0,
            0,
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["missed_plan_ids"], ["a"])
        self.assertEqual(result["extra_actual_ids"], ["r1"])
        self.assertEqual(result["total_penalty"], 0)


class ValidationTests(unittest.TestCase):
    def test_invalid_interval_end_before_start(self):
        with self.assertRaises(ReconcileError) as ctx:
            reconcile(
                [cmd("a", "main")],
                [rec("r1", "main", 500, 400)],
            )
        self.assertEqual(ctx.exception.code, "INVALID_INTERVAL")
        self.assertEqual(ctx.exception.details["ids"], ["r1"])

    def test_invalid_interval_zero_length(self):
        with self.assertRaises(ReconcileError) as ctx:
            reconcile([], [rec("r1", "main", 500, 500)])
        self.assertEqual(ctx.exception.code, "INVALID_INTERVAL")

    def test_invalid_interval_negative_start(self):
        with self.assertRaises(ReconcileError) as ctx:
            reconcile([], [rec("r1", "main", -1, 500)])
        self.assertEqual(ctx.exception.code, "INVALID_INTERVAL")

    def test_duplicate_record_id(self):
        with self.assertRaises(ReconcileError) as ctx:
            reconcile(
                [],
                [rec("r1", "main", 0, 100), rec("r1", "main", 0, 100)],
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        self.assertEqual(ctx.exception.details["duplicates"], ["r1"])

    def test_empty_record_id(self):
        with self.assertRaises(ReconcileError) as ctx:
            reconcile([], [rec("", "main", 0, 100)])
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_empty_record_channel(self):
        with self.assertRaises(ReconcileError) as ctx:
            reconcile([], [rec("r1", "", 0, 100)])
        self.assertEqual(ctx.exception.code, "INVALID_CHANNEL")

    def test_negative_tolerance_or_penalty(self):
        for tolerance, miss, extra in ((-1, 0, 0), (0, -1, 0), (0, 0, -1)):
            with self.assertRaises(ReconcileError) as ctx:
                reconcile_runs([], [], tolerance, miss, extra)
            self.assertEqual(ctx.exception.code, "INVALID_REQUEST")

    def test_plan_errors_propagate(self):
        # 计划侧沿用 /compile 的校验与错误码
        with self.assertRaises(CompileError) as ctx:
            reconcile(
                [cmd("a", "main", anchor="b"), cmd("b", "main", anchor="a")],
                [],
            )
        self.assertEqual(ctx.exception.code, "DEPENDENCY_CYCLE")

    def test_plan_checked_before_records(self):
        # 计划与实播同时非法时，先报计划侧错误
        with self.assertRaises(CompileError) as ctx:
            reconcile(
                [cmd("a", "main", duration=0)],
                [rec("r1", "main", 500, 400)],
            )
        self.assertEqual(ctx.exception.code, "INVALID_DURATION")


if __name__ == "__main__":
    unittest.main()
