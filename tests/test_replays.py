"""app.replays 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import random
import unittest
from itertools import combinations

from app.compiler import START, CompileError, compile_commands
from app.replays import MAX_REPLAY_CANDIDATES, plan_replays


def cmd(cid, channel="m", anchor=START, offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def cand(cid, priority=0):
    return {"id": cid, "priority": priority}


def merge_intervals(intervals):
    """与核心逻辑分开书写的区间并集：重叠或首尾相接即合并。"""
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def brute_force(commands, candidates, lead_in, lead_out, budget):
    """枚举全部候选子集，按题面三目标求最优（与核心逻辑分开书写）。"""
    compiled = compile_commands([dict(c) for c in commands])
    starts = {e["id"]: e["start_ms"] for e in compiled["timeline"]}
    ends = {e["id"]: e["end_ms"] for e in compiled["timeline"]}
    by_id = {c["id"]: c for c in candidates}

    windows = {
        cid: (max(0, starts[cid] - lead_in), ends[cid] + lead_out)
        for cid in by_id
    }
    best_ids = None
    best_segments = []
    best_total = 0
    for r in range(len(by_id) + 1):
        for chosen in combinations(sorted(by_id), r):
            merged = merge_intervals([windows[cid] for cid in chosen])
            total = sum(hi - lo for lo, hi in merged)
            if total > budget:
                continue
            priority_sum = sum(by_id[cid]["priority"] for cid in chosen)
            key = (priority_sum, r)
            if (
                best_ids is None
                or key > (
                    sum(by_id[c]["priority"] for c in best_ids),
                    len(best_ids),
                )
                or (
                    key
                    == (
                        sum(by_id[c]["priority"] for c in best_ids),
                        len(best_ids),
                    )
                    and list(chosen) < best_ids
                )
            ):
                best_ids = list(chosen)
                best_segments = merged
                best_total = total
    return {
        "selected_ids": best_ids,
        "segments": [
            {"start_ms": lo, "end_ms": hi} for lo, hi in best_segments
        ],
        "total_duration_ms": best_total,
    }


class BasicPlanTests(unittest.TestCase):
    def test_window_padding_clamped_at_origin(self):
        # a [100, 200)：前留白 300 截断到 0，后留白 50 -> [0, 250)
        commands = [cmd("a", offset=100, duration=100)]
        result = plan_replays(commands, [cand("a")], 300, 50, 1000)
        self.assertEqual(result["selected_ids"], ["a"])
        self.assertEqual(result["segments"], [{"start_ms": 0, "end_ms": 250}])
        self.assertEqual(result["total_duration_ms"], 250)

    def test_overlapping_windows_across_channels_merge(self):
        # a [0, 100) on m, b [100, 200) on s；零留白下首尾相接，跨通道合并
        commands = [
            cmd("a", "m", START, 0, 100),
            cmd("b", "s", START, 100, 100),
        ]
        result = plan_replays(commands, [cand("a"), cand("b")], 0, 0, 1000)
        self.assertEqual(result["selected_ids"], ["a", "b"])
        self.assertEqual(result["segments"], [{"start_ms": 0, "end_ms": 200}])
        self.assertEqual(result["total_duration_ms"], 200)

    def test_overlapping_with_padding_merges(self):
        # a [0, 100), b [150, 200)：零留白下不接触；后留白 50 使两窗口相接
        commands = [
            cmd("a", "m", START, 0, 100),
            cmd("b", "m", START, 150, 50),
        ]
        result = plan_replays(commands, [cand("a"), cand("b")], 0, 50, 1000)
        self.assertEqual(result["segments"], [{"start_ms": 0, "end_ms": 250}])
        self.assertEqual(result["total_duration_ms"], 250)

    def test_separate_windows_stay_separate(self):
        commands = [
            cmd("a", "m", START, 0, 100),
            cmd("b", "s", START, 1000, 100),
        ]
        result = plan_replays(commands, [cand("a"), cand("b")], 0, 0, 1000)
        self.assertEqual(result["selected_ids"], ["a", "b"])
        self.assertEqual(
            result["segments"],
            [
                {"start_ms": 0, "end_ms": 100},
                {"start_ms": 1000, "end_ms": 1100},
            ],
        )
        self.assertEqual(result["total_duration_ms"], 200)

    def test_budget_drops_lower_priority(self):
        # a [0, 100)、b [1000, 1100) 两窗口分离，各耗 100；预算 100 只能取一条，
        # a 优先级 1 < b 优先级 5，故选 b
        commands = [
            cmd("a", "m", START, 0, 100),
            cmd("b", "s", START, 1000, 100),
        ]
        result = plan_replays(
            commands, [cand("a", 1), cand("b", 5)], 0, 0, 100
        )
        self.assertEqual(result["selected_ids"], ["b"])
        self.assertEqual(result["segments"], [{"start_ms": 1000, "end_ms": 1100}])
        self.assertEqual(result["total_duration_ms"], 100)

    def test_count_breaks_priority_tie(self):
        # a、b 窗口重叠，合并后仅 100，各优先级 2（同选优先级和 4、条数 2）；
        # c 独占合并段 100、优先级 4（条数 1）。预算 100 下优先级和相同，
        # 按第二目标取条数更多的 {a,b}。
        commands = [
            cmd("a", "m", START, 0, 80),    # [0, 80)
            cmd("b", "s", START, 20, 80),   # [20, 100)，与 a 合并为 [0, 100) 共 100
            cmd("c", "c", START, 1000, 100),
        ]
        result = plan_replays(
            commands,
            [cand("a", 2), cand("b", 2), cand("c", 4)],
            0,
            0,
            100,
        )
        self.assertEqual(result["selected_ids"], ["a", "b"])
        self.assertEqual(result["segments"], [{"start_ms": 0, "end_ms": 100}])
        self.assertEqual(result["total_duration_ms"], 100)

    def test_lexicographic_breaks_final_tie(self):
        # a、b 窗口分离、同为 100、优先级相同；预算 100 任取其一，取 id 较小的 a
        commands = [
            cmd("a", "m", START, 0, 100),
            cmd("b", "s", START, 1000, 100),
        ]
        result = plan_replays(
            commands, [cand("a", 2), cand("b", 2)], 0, 0, 100
        )
        self.assertEqual(result["selected_ids"], ["a"])
        self.assertEqual(result["segments"], [{"start_ms": 0, "end_ms": 100}])

    def test_overlap_makes_both_cheaper_than_sum(self):
        # a [0, 100)、b [50, 150) 零留白重叠，合并总时长 150 < 单独之和 200；
        # 预算 150 下两条都入选（优先级和最大）
        commands = [
            cmd("a", "m", START, 0, 100),
            cmd("b", "s", START, 50, 100),
        ]
        result = plan_replays(
            commands, [cand("a", 1), cand("b", 1)], 0, 0, 150
        )
        self.assertEqual(result["selected_ids"], ["a", "b"])
        self.assertEqual(result["segments"], [{"start_ms": 0, "end_ms": 150}])
        self.assertEqual(result["total_duration_ms"], 150)

    def test_empty_selection_when_budget_too_small(self):
        commands = [cmd("a", "m", START, 0, 100)]
        result = plan_replays(commands, [cand("a", 9)], 0, 0, 50)
        self.assertEqual(result["selected_ids"], [])
        self.assertEqual(result["segments"], [])
        self.assertEqual(result["total_duration_ms"], 0)

    def test_empty_candidates_always_ok(self):
        result = plan_replays([cmd("a")], [], 10, 20, 1)
        self.assertEqual(
            result,
            {"selected_ids": [], "segments": [], "total_duration_ms": 0},
        )

    def test_segments_sorted_by_start_with_anchor_resolved_times(self):
        # b 锚定 a 并带 100ms 偏移：a [0, 100)，b [200, 250)，窗口分离；
        # 候选输入顺序 (b, a) 不影响 selected_ids 升序与 segments 按起点排序
        commands = [
            cmd("a", "m", START, 0, 100),
            cmd("b", "s", "a", 100, 50),
        ]
        result = plan_replays(
            commands, [cand("b"), cand("a")], 0, 0, 1000
        )
        self.assertEqual(result["selected_ids"], ["a", "b"])
        self.assertEqual(
            result["segments"],
            [
                {"start_ms": 0, "end_ms": 100},
                {"start_ms": 200, "end_ms": 250},
            ],
        )


class ValidationTests(unittest.TestCase):
    def test_compile_errors_propagate(self):
        # 缺失锚点优先返回编排错误
        with self.assertRaises(CompileError) as ctx:
            plan_replays(
                [cmd("a", anchor="ghost")], [cand("a")], 0, 0, 100
            )
        self.assertEqual(ctx.exception.code, "MISSING_ANCHOR")

    def test_negative_lead_in(self):
        with self.assertRaises(CompileError) as ctx:
            plan_replays([cmd("a")], [cand("a")], -1, 0, 100)
        self.assertEqual(ctx.exception.code, "INVALID_REPLAY_PADDING")

    def test_negative_lead_out(self):
        with self.assertRaises(CompileError) as ctx:
            plan_replays([cmd("a")], [cand("a")], 0, -1, 100)
        self.assertEqual(ctx.exception.code, "INVALID_REPLAY_PADDING")

    def test_non_positive_budget(self):
        for budget in (0, -1):
            with self.assertRaises(CompileError) as ctx:
                plan_replays([cmd("a")], [cand("a")], 0, 0, budget)
            self.assertEqual(ctx.exception.code, "INVALID_REPLAY_BUDGET")

    def test_duplicate_candidate_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_replays([cmd("a")], [cand("a"), cand("a")], 0, 0, 100)
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        self.assertEqual(ctx.exception.details, {"duplicates": ["a"]})

    def test_empty_candidate_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_replays([cmd("a")], [cand("")], 0, 0, 100)
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_unknown_candidate_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_replays([cmd("a")], [cand("ghost")], 0, 0, 100)
        self.assertEqual(ctx.exception.code, "REPLAY_TARGET_NOT_FOUND")
        self.assertEqual(ctx.exception.details, {"ids": ["ghost"]})

    def test_negative_priority(self):
        with self.assertRaises(CompileError) as ctx:
            plan_replays([cmd("a")], [cand("a", -1)], 0, 0, 100)
        self.assertEqual(ctx.exception.code, "INVALID_PRIORITY")

    def test_too_many_candidates(self):
        commands = [cmd(f"c{i:02d}", duration=1) for i in range(13)]
        with self.assertRaises(CompileError) as ctx:
            plan_replays(
                commands, [cand(f"c{i:02d}") for i in range(13)], 0, 0, 10**9
            )
        self.assertEqual(ctx.exception.code, "TOO_MANY_REPLAY_CANDIDATES")
        self.assertEqual(
            ctx.exception.details,
            {"count": 13, "limit": MAX_REPLAY_CANDIDATES},
        )

    def test_inputs_not_mutated(self):
        commands = [
            {"id": "a", "channel": "m", "offset_ms": 0, "duration_ms": 100}
        ]
        candidates = [{"id": "a", "priority": 3}]
        commands_snapshot = [dict(c) for c in commands]
        candidates_snapshot = [dict(c) for c in candidates]
        plan_replays(commands, candidates, 10, 20, 1000)
        self.assertEqual(commands, commands_snapshot)
        self.assertEqual(candidates, candidates_snapshot)


class BruteForceFuzzTests(unittest.TestCase):
    def test_matches_brute_force(self):
        random.seed(20260926)
        for _ in range(4000):
            n_cmds = random.randint(1, 6)
            ids = [f"c{i}" for i in range(n_cmds)]
            commands = []
            for i in range(n_cmds):
                anchor = START if i == 0 or random.random() < 0.5 else random.choice(ids[:i])
                commands.append(
                    cmd(
                        ids[i],
                        random.choice(["m", "s"]),
                        anchor,
                        random.randint(0, 200),
                        random.randint(1, 120),
                    )
                )
            try:
                compiled = compile_commands([dict(c) for c in commands])
            except CompileError:
                continue
            known = [entry["id"] for entry in compiled["timeline"]]

            n_cand = random.randint(0, min(6, len(known)))
            chosen = random.sample(known, n_cand)
            candidates = [
                cand(cid, random.randint(0, 5)) for cid in chosen
            ]
            lead_in = random.randint(0, 80)
            lead_out = random.randint(0, 80)
            budget = random.choice([1, 50, 150, 10**6])

            result = plan_replays(
                [dict(c) for c in commands],
                [dict(c) for c in candidates],
                lead_in,
                lead_out,
                budget,
            )
            expected = brute_force(
                [dict(c) for c in commands],
                [dict(c) for c in candidates],
                lead_in,
                lead_out,
                budget,
            )
            self.assertEqual(result["selected_ids"], expected["selected_ids"])
            self.assertEqual(result["segments"], expected["segments"])
            self.assertEqual(
                result["total_duration_ms"], expected["total_duration_ms"]
            )


if __name__ == "__main__":
    unittest.main()
