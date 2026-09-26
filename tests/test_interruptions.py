"""app.interruptions 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import random
import unittest

from app.compiler import START, CompileError, compile_commands
from app.interruptions import plan_interruption


def cmd(cid, channel, anchor=START, offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def ids_of(result):
    return [entry["id"] for entry in result["timeline"]]


class BasicPlanningTests(unittest.TestCase):
    def setUp(self):
        # a: main [0,1000)；b: main 锚定 a [1000,1500)；c: sub [0,1000)
        self.commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("b", "main", "a", 0, 500),
            cmd("c", "sub", START, 0, 1000),
        ]

    def test_picks_gap_start_with_single_cancel_over_earlier_double(self):
        # s=800: [800,1000) 只与 a 重叠，撤销闭包 {a, b}（b 是 a 的锚点后继）
        # s=1000: [1000,1200) 与 a 首尾相接、只与 b 重叠 -> 仅撤 b（1 条）最优
        result = plan_interruption(self.commands, "main", 200, 0, 1499, ["a", "b"])
        self.assertEqual((result["start_ms"], result["cancelled_ids"]), (1000, ["b"]))
        # 剩余时间线：b 被撤，a、c 保留且内容/顺序与 /compile 一致
        self.assertEqual(ids_of(result), ["a", "c"])
        self.assertEqual(
            result["timeline"],
            [
                e for e in compile_commands(self.commands)["timeline"] if e["id"] != "b"
            ],
        )

    def test_clear_window_returns_earliest_start_with_empty_cancellation(self):
        # s=1500 起插播与所有 main 口令首尾相接/无交集
        result = plan_interruption(self.commands, "main", 200, 1500, 2000, ["a", "b"])
        self.assertEqual(result["start_ms"], 1500)
        self.assertEqual(result["cancelled_ids"], [])
        self.assertEqual(ids_of(result), ["a", "c", "b"])

    def test_single_point_window_overlapping_only_anchor(self):
        result = plan_interruption(self.commands, "main", 200, 800, 800, ["a", "b"])
        self.assertEqual(result["start_ms"], 800)
        self.assertEqual(result["cancelled_ids"], ["a", "b"])

    def test_half_open_touching_does_not_overlap(self):
        # s=1000 与 a 的 end=1000 首尾相接，不算重叠
        result = plan_interruption(self.commands, "main", 200, 1000, 1000, ["a", "b"])
        self.assertEqual(result["cancelled_ids"], ["b"])

    def test_intersection_includes_commands_starting_after_interruption(self):
        # a [0,100)，插播 [0,300)：口令在插播开始后才结束前都算重叠
        commands = [cmd("p", "main", START, 0, 100)]
        result = plan_interruption(commands, "main", 300, 0, 99, ["p"])
        self.assertEqual(result["start_ms"], 0)
        self.assertEqual(result["cancelled_ids"], ["p"])
        # s=100: [100,400) 与 p 首尾相接，无需撤销
        result = plan_interruption(commands, "main", 300, 0, 1000, ["p"])
        self.assertEqual(result["start_ms"], 100)
        self.assertEqual(result["cancelled_ids"], [])

    def test_other_channel_ignored(self):
        # 插播 sub：与 c 重叠；main 的 a、b 不参与。c [0,1000) 不可撤销
        # （c 不在 cancellable_ids 中），窗口 [0,499] 内任意起点都重叠 -> 无可行起点
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(self.commands, "sub", 500, 0, 499, ["a", "b"])
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_INTERRUPTION")
        self.assertEqual(ctx.exception.details["blocking_ids"], ["c"])
        # 放开到 s=1000：与 c 首尾相接，无需撤销
        result = plan_interruption(self.commands, "sub", 500, 0, 1000, ["a", "b"])
        self.assertEqual(result["start_ms"], 1000)
        self.assertEqual(result["cancelled_ids"], [])
        # c 允许撤销时，窗口内最早起点 0 撤 c；a、b 在 main 不受影响
        result = plan_interruption(self.commands, "sub", 500, 0, 499, ["a", "b", "c"])
        self.assertEqual(result["start_ms"], 0)
        self.assertEqual(result["cancelled_ids"], ["c"])
        self.assertEqual(ids_of(result), ["a", "b"])
        # 通道上没有任何口令时窗口起点即可插播
        result = plan_interruption(self.commands, "void", 500, 0, 999, [])
        self.assertEqual(result["start_ms"], 0)

    def test_unknown_channel_with_no_commands(self):
        result = plan_interruption([], "brand-new", 10, 5, 50, [])
        self.assertEqual(
            result, {"start_ms": 5, "cancelled_ids": [], "timeline": []}
        )

    def test_window_single_point_closed_both_ends(self):
        # 闭区间右端 latest 也可取：s=1500 可行
        result = plan_interruption(self.commands, "main", 200, 1500, 1500, ["a", "b"])
        self.assertEqual(result["start_ms"], 1500)


class SuccessorClosureTests(unittest.TestCase):
    def test_cancel_pulls_all_anchor_successors_recursively_and_cross_channel(self):
        # a(main) <- b(main) <- x(sub)；s=1000 与 b 重叠，必须递归撤掉 x
        commands = [
            cmd("a", "main"),
            cmd("b", "main", "a", 0, 500),
            cmd("x", "sub", "b", 0, 100),
        ]
        result = plan_interruption(commands, "main", 200, 1000, 1000, ["a", "b", "x"])
        self.assertEqual(result["cancelled_ids"], ["b", "x"])

    def test_branching_subtree_pulled_when_anchor_cancelled(self):
        # a(main) 有两个跨通道后继 b、c；撤 a 必须连带撤 b、c
        commands = [
            cmd("a", "main", START, 10, 100),
            cmd("b", "sub", "a", 0, 10),
            cmd("c", "sub", "a", 5, 10),
        ]
        # a [10,110)，插播 50ms；s=110 起无重叠
        result = plan_interruption(commands, "main", 50, 0, 1000, ["a", "b", "c"])
        self.assertEqual(result["start_ms"], 110)
        self.assertEqual(result["cancelled_ids"], [])
        result = plan_interruption(commands, "main", 50, 0, 109, ["a", "b", "c"])
        self.assertEqual(result["cancelled_ids"], ["a", "b", "c"])

    def test_non_cancellable_descendant_blocks_even_if_overlap_command_is_cancellable(self):
        # b 自身不可撤销：s=800 通过 a 的后继闭包触及 b；s=1000 直接触及 b
        commands = [cmd("a", "main"), cmd("b", "main", "a", 0, 500)]
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(commands, "main", 200, 0, 1499, ["a"])
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_INTERRUPTION")
        self.assertEqual(ctx.exception.details["reason"], "UNREVOCABLE_OVERLAP")
        self.assertEqual(ctx.exception.details["blocking_ids"], ["b"])
        # 越过 b 的 end=1500 后可行，且无需撤销
        result = plan_interruption(commands, "main", 200, 0, 2000, ["a"])
        self.assertEqual((result["start_ms"], result["cancelled_ids"]), (1500, []))

    def test_non_cancellable_cross_channel_descendant_blocks(self):
        commands = [
            cmd("a", "main"),
            cmd("b", "main", "a", 0, 500),
            cmd("x", "sub", "b", 0, 100),
        ]
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(commands, "main", 200, 1000, 1499, ["a", "b"])
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_INTERRUPTION")
        self.assertIn("x", ctx.exception.details["blocking_ids"])

    def test_command_not_anchoring_cancelled_command_survives(self):
        # d 在 sub 通道独立锚定 START：撤 main 的 b 不影响 d
        commands = [
            cmd("a", "main"),
            cmd("b", "main", "a", 0, 500),
            cmd("d", "sub", START, 1000, 500),
        ]
        result = plan_interruption(commands, "main", 200, 1000, 1000, ["a", "b", "d"])
        self.assertEqual(result["cancelled_ids"], ["b"])
        self.assertEqual(ids_of(result), ["a", "d"])

    def test_independent_same_channel_command_outside_interval_survives(self):
        # d 独立锚定 START 且与 b 同通道，但时间上不与插播重叠（首尾相接）
        commands = [
            cmd("a", "main"),
            cmd("b", "main", "a", 0, 500),
            cmd("d", "main", START, 1200, 100),
        ]
        result = plan_interruption(commands, "main", 200, 1000, 1000, ["a", "b", "d"])
        self.assertEqual(result["cancelled_ids"], ["b"])
        self.assertEqual(ids_of(result), ["a", "d"])


class TieBreakTests(unittest.TestCase):
    def test_equal_cancel_count_picks_earliest_start(self):
        # p [0,100)、q [200,300)，插播 100ms：
        # 撤 p 的起点范围与撤 q 的起点范围各撤 1 条；s=100 处 [100,200) 空档撤 0 条
        commands = [
            cmd("p", "main", START, 0, 100),
            cmd("q", "main", START, 200, 100),
        ]
        result = plan_interruption(commands, "main", 100, 0, 1000, ["p", "q"])
        self.assertEqual(result["start_ms"], 100)
        self.assertEqual(result["cancelled_ids"], [])

    def test_tie_between_two_single_cancel_segments(self):
        # 不留空档：p[0,100)、q[100,200) 首尾相接，插播 100ms 始终恰好与 1 条重叠
        # （s=0 与 p、s=100 与 q、s>=200 无重叠）。窗口限定 [0,199]：
        # 撤 1 条的最早起点是 0（撤 p）
        commands = [
            cmd("p", "main", START, 0, 100),
            cmd("q", "main", START, 100, 100),
        ]
        result = plan_interruption(commands, "main", 100, 0, 199, ["p", "q"])
        self.assertEqual(result["start_ms"], 0)
        self.assertEqual(result["cancelled_ids"], ["p"])

    def test_no_feasible_start_in_window_but_feasible_outside(self):
        # 窗口完全落在 a 的重叠区且 a 不可撤销；窗口外本可以避开
        commands = [cmd("a", "main", START, 100, 100)]  # [100,200)
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(commands, "main", 50, 100, 199, [])
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_INTERRUPTION")
        # s=200 可行，但不在窗口内
        result = plan_interruption(commands, "main", 50, 200, 300, [])
        self.assertEqual(result["start_ms"], 200)
        self.assertEqual(result["cancelled_ids"], [])


class NoPerMillisecondEnumerationTests(unittest.TestCase):
    def test_huge_integer_window_resolves_instantly(self):
        commands = [
            cmd("a", "main", START, 10**12, 10**9),
        ]
        # a 在 10^12 才开始：窗口起点 0 处插播 [0, 10^9) 与其无交集，
        # 最早可行点即 earliest=0，且无需任何撤销（不逐毫秒扫描到 10^12）
        result = plan_interruption(
            commands, "main", 10**9, 0, 2**53, ["a"]
        )
        self.assertEqual(result["start_ms"], 0)
        self.assertEqual(result["cancelled_ids"], [])

    def test_deep_chain_does_not_recurse_or_enumerate(self):
        N = 5000
        commands = [cmd("z0", "main", START, 0, 10)]
        for k in range(1, N):
            commands.append(cmd(f"z{k}", "main", f"z{k-1}", 0, 10))
        ids = [c["id"] for c in commands]
        result = plan_interruption(commands, "main", 5, 0, 10**12, ids)
        self.assertEqual(result["cancelled_ids"], [])
        self.assertEqual(result["start_ms"], 10 * N)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.commands = [cmd("a", "main"), cmd("b", "main", "a", 0, 500)]

    def test_compile_errors_propagate_first(self):
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(
                [cmd("a", "main", "ghost")], "main", 10, 0, 10, ["a"]
            )
        self.assertEqual(ctx.exception.code, "MISSING_ANCHOR")

        with self.assertRaises(CompileError) as ctx:
            plan_interruption(
                [cmd("a", "main", START, -10, 5)], "main", 10, 0, 10, ["a"]
            )
        self.assertEqual(ctx.exception.code, "NEGATIVE_START")

    def test_non_positive_duration(self):
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(self.commands, "main", 0, 0, 10, ["a"])
        self.assertEqual(ctx.exception.code, "INVALID_DURATION")

    def test_empty_channel(self):
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(self.commands, "", 10, 0, 10, ["a"])
        self.assertEqual(ctx.exception.code, "INVALID_CHANNEL")

    def test_inverted_window(self):
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(self.commands, "main", 10, 20, 10, ["a"])
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")

    def test_negative_window_bounds(self):
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(self.commands, "main", 10, -1, 10, ["a"])
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")

    def test_unknown_cancellable_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(self.commands, "main", 10, 0, 10, ["a", "zzz"])
        self.assertEqual(ctx.exception.code, "INTERRUPTION_TARGET_NOT_FOUND")
        self.assertEqual(ctx.exception.details["ids"], ["zzz"])

    def test_duplicate_cancellable_ids(self):
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(self.commands, "main", 10, 0, 10, ["a", "a"])
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")

    def test_empty_cancellable_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_interruption(self.commands, "main", 10, 0, 10, [""])
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_inputs_not_mutated(self):
        import copy

        snapshot = copy.deepcopy(self.commands)
        canc = ["a", "b"]
        canc_snapshot = list(canc)
        plan_interruption(self.commands, "main", 200, 0, 1000, canc)
        self.assertEqual(self.commands, snapshot)
        self.assertEqual(canc, canc_snapshot)


class BruteForceFuzzTests(unittest.TestCase):
    """随机编排下与逐毫秒暴力枚举结果逐案对照（仅测试代码枚举，核心逻辑不枚举）。"""

    def test_matches_brute_force(self):
        random.seed(20260925)
        for trial in range(400):
            n = random.randint(0, 8)
            ids = [f"c{i}" for i in range(n)]
            commands = []
            for i, cid in enumerate(ids):
                anchor = (
                    START
                    if i == 0 or random.random() < 0.4
                    else random.choice(ids[:i])
                )
                commands.append(
                    cmd(
                        cid,
                        random.choice(["m", "s"]),
                        anchor,
                        random.randint(-50, 200),
                        random.randint(1, 120),
                    )
                )
            try:
                compiled = compile_commands([dict(x) for x in commands])
            except CompileError:
                continue
            entries = {e["id"]: e for e in compiled["timeline"]}

            children = {cid: set() for cid in entries}
            for cid, entry in entries.items():
                if entry["anchor"] != START:
                    children[entry["anchor"]].add(cid)

            def closure(roots):
                seen: set[str] = set()
                stack = list(roots)
                while stack:
                    node = stack.pop()
                    if node in seen:
                        continue
                    seen.add(node)
                    stack.extend(children[node])
                return seen

            channel = random.choice(["m", "s", "x"])
            duration = random.randint(1, 100)
            lo = random.randint(0, 300)
            hi = lo + random.randint(0, 300)
            cancellable = set(
                random.sample(ids, random.randint(0, len(ids)))
            )

            best = None
            for start in range(lo, hi + 1):
                roots = [
                    cid
                    for cid, entry in entries.items()
                    if entry["channel"] == channel
                    and entry["start_ms"] < start + duration
                    and start < entry["end_ms"]
                ]
                clo = closure(roots)
                if clo - cancellable:
                    continue
                candidate = (len(clo), start)
                if best is None or candidate < best:
                    best = candidate

            try:
                result = plan_interruption(
                    commands,
                    channel,
                    duration,
                    lo,
                    hi,
                    sorted(cancellable),
                )
            except CompileError as exc:
                self.assertEqual(exc.code, "NO_FEASIBLE_INTERRUPTION")
                self.assertIsNone(best, trial)
                continue

            self.assertIsNotNone(best, trial)
            self.assertEqual(
                (len(result["cancelled_ids"]), result["start_ms"]),
                best,
                trial,
            )
            start = result["start_ms"]
            roots = [
                cid
                for cid, entry in entries.items()
                if entry["channel"] == channel
                and entry["start_ms"] < start + duration
                and start < entry["end_ms"]
            ]
            self.assertEqual(set(result["cancelled_ids"]), closure(roots))
            self.assertEqual(
                result["cancelled_ids"], sorted(result["cancelled_ids"])
            )
            self.assertEqual(
                ids_of(result),
                [
                    e["id"]
                    for e in compiled["timeline"]
                    if e["id"] not in closure(roots)
                ],
            )


if __name__ == "__main__":
    unittest.main()
