"""app.fillers 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import random
import unittest
from itertools import product

from app.compiler import START, CompileError, compile_commands
from app.fillers import plan_fillers


def cmd(cid, channel, anchor=START, offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def filler(fid, duration, priority=0):
    return {"id": fid, "duration_ms": duration, "priority": priority}


def gaps_of(commands, channel, horizon):
    """测试侧的独立空档计算（与核心逻辑实现分开书写，用于交叉验证）。"""
    compiled = compile_commands([dict(c) for c in commands])
    occupied = [
        (e["start_ms"], min(e["end_ms"], horizon))
        for e in compiled["timeline"]
        if e["channel"] == channel and e["start_ms"] < horizon
    ]
    gaps = []
    cursor = 0
    for start, end in sorted(occupied):
        if end <= cursor:
            continue
        if start > cursor:
            gaps.append((cursor, start))
        cursor = end
    if cursor < horizon:
        gaps.append((cursor, horizon))
    return gaps


class GapExtractionTests(unittest.TestCase):
    def test_occupied_clipped_to_horizon_and_merged(self):
        # main: a [0,1000)、b [1500,2500)、c [2600,3600)；视界 3000 把 c 裁到 [2600,3000)
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("b", "main", "a", 500, 1000),
            cmd("c", "main", START, 2600, 1000),
            cmd("d", "sub", START, 100, 500),
        ]
        result = plan_fillers(commands, "main", 3000, [])
        self.assertEqual(result["placements"], [])
        self.assertEqual(
            result["gaps"],
            [
                {"start_ms": 1000, "end_ms": 1500, "remaining_ms": 500},
                {"start_ms": 2500, "end_ms": 2600, "remaining_ms": 100},
            ],
        )

    def test_other_channel_ignored(self):
        commands = [cmd("a", "main", START, 0, 100), cmd("d", "sub", START, 0, 100)]
        result = plan_fillers(commands, "main", 300, [])
        self.assertEqual(
            result["gaps"],
            [{"start_ms": 100, "end_ms": 300, "remaining_ms": 200}],
        )
        # sub 通道的占用不影响 main；换到 sub 通道同理
        result = plan_fillers(commands, "sub", 300, [])
        self.assertEqual(
            result["gaps"],
            [{"start_ms": 100, "end_ms": 300, "remaining_ms": 200}],
        )

    def test_touching_intervals_merge_into_one_occupied_run(self):
        # a [0,1000)、b 锚定 a [1000,1500)：首尾相接，并集连续，中间不产生空档
        commands = [cmd("a", "main", START, 0, 1000), cmd("b", "main", "a", 0, 500)]
        result = plan_fillers(commands, "main", 2000, [])
        self.assertEqual(
            result["gaps"],
            [{"start_ms": 1500, "end_ms": 2000, "remaining_ms": 500}],
        )

    def test_full_coverage_leaves_no_gap(self):
        commands = [cmd("a", "main", START, 0, 1000)]
        result = plan_fillers(commands, "main", 1000, [filler("x", 10)])
        self.assertEqual(result["placements"], [])
        self.assertEqual(result["gaps"], [])

    def test_empty_commands_single_gap(self):
        result = plan_fillers([], "main", 500, [])
        self.assertEqual(
            result,
            {
                "placements": [],
                "gaps": [{"start_ms": 0, "end_ms": 500, "remaining_ms": 500}],
            },
        )


class AllocationTests(unittest.TestCase):
    def test_joint_allocation_across_gaps(self):
        # 空档 [0,5) 与 [6,12)：{a}|{b,c} 是唯一装下全部素材的划分
        commands = [cmd("p", "main", START, 5, 1)]
        fillers = [filler("a", 4, 1), filler("b", 3, 2), filler("c", 3, 0)]
        result = plan_fillers(commands, "main", 12, fillers)
        self.assertEqual(
            result["placements"],
            [
                {"id": "a", "start_ms": 0, "end_ms": 4},
                {"id": "b", "start_ms": 6, "end_ms": 9},
                {"id": "c", "start_ms": 9, "end_ms": 12},
            ],
        )
        self.assertEqual(
            result["gaps"],
            [
                {"start_ms": 0, "end_ms": 5, "remaining_ms": 1},
                {"start_ms": 6, "end_ms": 12, "remaining_ms": 0},
            ],
        )

    def test_priority_breaks_duration_tie(self):
        # 单空档容量 5：a、b 时长同为 5，选 priority 更大的 b
        result = plan_fillers([], "main", 5, [filler("a", 5, 10), filler("b", 5, 20)])
        self.assertEqual(
            result["placements"], [{"id": "b", "start_ms": 0, "end_ms": 5}]
        )
        self.assertEqual(
            result["gaps"], [{"start_ms": 0, "end_ms": 5, "remaining_ms": 0}]
        )

    def test_duration_dominates_priority(self):
        # 填充总时长优先：a 时长 8、priority 0 仍胜过时长 5、priority 100 的 b
        result = plan_fillers([], "main", 8, [filler("a", 8, 0), filler("b", 5, 100)])
        self.assertEqual(
            result["placements"], [{"id": "a", "start_ms": 0, "end_ms": 8}]
        )

    def test_lexicographic_breaks_tie(self):
        # 两个容量 6 的空档：{a}|{b} 与 {b}|{a} 时长、优先级都相同，
        # 序列 [a, b] 字典序更小 -> a 进靠前的空档
        commands = [cmd("p", "main", START, 6, 1)]
        result = plan_fillers(
            commands, "main", 13, [filler("a", 6), filler("b", 6)]
        )
        self.assertEqual(
            result["placements"],
            [
                {"id": "a", "start_ms": 0, "end_ms": 6},
                {"id": "b", "start_ms": 7, "end_ms": 13},
            ],
        )
        self.assertEqual(
            result["gaps"],
            [
                {"start_ms": 0, "end_ms": 6, "remaining_ms": 0},
                {"start_ms": 7, "end_ms": 13, "remaining_ms": 0},
            ],
        )

    def test_id_order_within_gap_regardless_of_input_order(self):
        # 输入顺序 c、a、b，空档内仍按 id 升序从空档起点紧贴排布
        fillers = [filler("c", 2), filler("a", 3), filler("b", 4)]
        result = plan_fillers([], "main", 10, fillers)
        self.assertEqual(
            result["placements"],
            [
                {"id": "a", "start_ms": 0, "end_ms": 3},
                {"id": "b", "start_ms": 3, "end_ms": 7},
                {"id": "c", "start_ms": 7, "end_ms": 9},
            ],
        )
        self.assertEqual(
            result["gaps"], [{"start_ms": 0, "end_ms": 10, "remaining_ms": 1}]
        )

    def test_each_filler_used_at_most_once(self):
        # 两个空档都装得下 a，但 a 只能使用一次
        commands = [cmd("p", "main", START, 5, 1)]
        result = plan_fillers(commands, "main", 11, [filler("a", 5)])
        self.assertEqual(
            result["placements"], [{"id": "a", "start_ms": 0, "end_ms": 5}]
        )
        self.assertEqual(
            result["gaps"],
            [
                {"start_ms": 0, "end_ms": 5, "remaining_ms": 0},
                {"start_ms": 6, "end_ms": 11, "remaining_ms": 5},
            ],
        )

    def test_no_filler_fits_any_gap(self):
        result = plan_fillers([], "main", 50, [filler("a", 100)])
        self.assertEqual(result["placements"], [])
        self.assertEqual(
            result["gaps"], [{"start_ms": 0, "end_ms": 50, "remaining_ms": 50}]
        )

    def test_empty_fillers(self):
        commands = [cmd("p", "main", START, 5, 1)]
        result = plan_fillers(commands, "main", 11, [])
        self.assertEqual(result["placements"], [])
        self.assertEqual(
            result["gaps"],
            [
                {"start_ms": 0, "end_ms": 5, "remaining_ms": 5},
                {"start_ms": 6, "end_ms": 11, "remaining_ms": 5},
            ],
        )


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.commands = [cmd("a", "main", START, 0, 100)]
        self.fillers = [filler("f", 10)]

    def test_compile_errors_propagate_first(self):
        with self.assertRaises(CompileError) as ctx:
            plan_fillers([cmd("a", "main", "ghost")], "main", 100, self.fillers)
        self.assertEqual(ctx.exception.code, "MISSING_ANCHOR")

        with self.assertRaises(CompileError) as ctx:
            plan_fillers(
                [cmd("a", "main", START, -10, 5)], "main", 100, self.fillers
            )
        self.assertEqual(ctx.exception.code, "NEGATIVE_START")

    def test_empty_channel(self):
        with self.assertRaises(CompileError) as ctx:
            plan_fillers(self.commands, "", 100, self.fillers)
        self.assertEqual(ctx.exception.code, "INVALID_CHANNEL")

    def test_non_positive_horizon(self):
        for horizon in (0, -5):
            with self.assertRaises(CompileError) as ctx:
                plan_fillers(self.commands, "main", horizon, self.fillers)
            self.assertEqual(ctx.exception.code, "INVALID_HORIZON")

    def test_duplicate_filler_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_fillers(
                self.commands, "main", 100, [filler("f", 1), filler("f", 2)]
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        self.assertEqual(ctx.exception.details["duplicates"], ["f"])

    def test_empty_filler_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_fillers(self.commands, "main", 100, [filler("", 1)])
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_non_positive_duration(self):
        for duration in (0, -3):
            with self.assertRaises(CompileError) as ctx:
                plan_fillers(
                    self.commands, "main", 100, [filler("f", duration)]
                )
            self.assertEqual(ctx.exception.code, "INVALID_DURATION")

    def test_negative_priority(self):
        with self.assertRaises(CompileError) as ctx:
            plan_fillers(self.commands, "main", 100, [filler("f", 1, -1)])
        self.assertEqual(ctx.exception.code, "INVALID_PRIORITY")

    def test_too_many_fillers(self):
        fillers = [filler(f"f{i}", 1) for i in range(13)]
        with self.assertRaises(CompileError) as ctx:
            plan_fillers(self.commands, "main", 100, fillers)
        self.assertEqual(ctx.exception.code, "TOO_MANY_FILLERS")
        self.assertEqual(ctx.exception.details["count"], 13)

    def test_too_many_gaps(self):
        # 8 条口令 [1,2)、[3,4)、…、[15,16) 把 [0,20) 切成 9 个空档
        commands = [cmd(f"p{i}", "main", START, 2 * i + 1, 1) for i in range(8)]
        with self.assertRaises(CompileError) as ctx:
            plan_fillers(commands, "main", 20, self.fillers)
        self.assertEqual(ctx.exception.code, "TOO_MANY_GAPS")
        self.assertEqual(ctx.exception.details["count"], 9)

    def test_inputs_not_mutated(self):
        import copy

        commands = [cmd("p", "main", START, 5, 1)]
        fillers = [filler("b", 3, 1), filler("a", 4, 2)]
        commands_snapshot = copy.deepcopy(commands)
        fillers_snapshot = copy.deepcopy(fillers)
        plan_fillers(commands, "main", 12, fillers)
        self.assertEqual(commands, commands_snapshot)
        self.assertEqual(fillers, fillers_snapshot)


class BruteForceFuzzTests(unittest.TestCase):
    """随机输入下与逐分配方案暴力枚举结果逐案对照（仅测试代码枚举，核心逻辑用 DP）。"""

    def test_matches_brute_force(self):
        random.seed(20260925)
        for trial in range(250):
            n_cmds = random.randint(0, 4)
            ids = [f"c{i}" for i in range(n_cmds)]
            commands = []
            for i, cid in enumerate(ids):
                anchor = (
                    START
                    if i == 0 or random.random() < 0.5
                    else random.choice(ids[:i])
                )
                commands.append(
                    cmd(
                        cid,
                        random.choice(["m", "s"]),
                        anchor,
                        random.randint(0, 200),
                        random.randint(1, 120),
                    )
                )
            try:
                compile_commands([dict(x) for x in commands])
            except CompileError:
                continue

            horizon = random.randint(1, 400)
            channel = random.choice(["m", "s", "x"])
            fillers = [
                filler(f"f{i}", random.randint(1, 60), random.randint(0, 5))
                for i in range(random.randint(0, 5))
            ]
            gaps = gaps_of(commands, channel, horizon)
            self.assertLessEqual(len(gaps), 8)

            result = plan_fillers(commands, channel, horizon, fillers)
            if len(gaps) > 3:
                # 暴力枚举规模随空档数指数增长，空档较多时仅校验排布合法性
                self._assert_legality(result, gaps, fillers)
                continue

            best = self._brute_force(fillers, gaps)
            (dur, pri), seq = best
            placements = result["placements"]
            self.assertEqual([p["id"] for p in placements], seq, trial)
            self.assertEqual(
                sum(p["end_ms"] - p["start_ms"] for p in placements), dur, trial
            )
            pri_of = {f["id"]: f["priority"] for f in fillers}
            self.assertEqual(
                sum(pri_of[p["id"]] for p in placements), pri, trial
            )
            self._assert_legality(result, gaps, fillers)

    @staticmethod
    def _brute_force(fillers, gaps):
        """枚举每条素材的空档归属（含不选用），返回 ((时长, 优先级), 最小 id 序列)。"""
        n = len(fillers)
        g = len(gaps)
        best = None
        for assign in product(range(g + 1), repeat=n):
            used = [0] * g
            dur = pri = 0
            for i, slot in enumerate(assign):
                if slot < g:
                    used[slot] += fillers[i]["duration_ms"]
                    dur += fillers[i]["duration_ms"]
                    pri += fillers[i]["priority"]
            if any(used[k] > gaps[k][1] - gaps[k][0] for k in range(g)):
                continue
            seq = []
            for k in range(g):
                seq.extend(
                    sorted(fillers[i]["id"] for i in range(n) if assign[i] == k)
                )
            key = (dur, pri)
            if best is None or key > best[0] or (key == best[0] and seq < best[1]):
                best = (key, seq)
        return best

    def _assert_legality(self, result, gaps, fillers):
        """校验排布合法性：空档内紧贴、id 升序、不越界、剩余时长正确。"""
        dur_of = {f["id"]: f["duration_ms"] for f in fillers}
        placements = result["placements"]
        self.assertEqual(len(result["gaps"]), len(gaps))
        pos = 0
        for gap_info, (gs, ge) in zip(result["gaps"], gaps):
            self.assertEqual((gap_info["start_ms"], gap_info["end_ms"]), (gs, ge))
            cursor = gs
            last_id = None
            while pos < len(placements) and placements[pos]["start_ms"] < ge:
                placement = placements[pos]
                self.assertEqual(placement["start_ms"], cursor)
                self.assertEqual(
                    placement["end_ms"] - placement["start_ms"],
                    dur_of[placement["id"]],
                )
                if last_id is not None:
                    self.assertLess(last_id, placement["id"])
                last_id = placement["id"]
                cursor = placement["end_ms"]
                pos += 1
            self.assertEqual(gap_info["remaining_ms"], ge - cursor)
        self.assertEqual(pos, len(placements))


if __name__ == "__main__":
    unittest.main()
