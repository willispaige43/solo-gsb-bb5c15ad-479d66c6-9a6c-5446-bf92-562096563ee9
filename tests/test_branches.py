"""app.branches 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

from copy import deepcopy

import unittest

from app.branches import MAX_SWITCHES, BranchError, select_branch
from app.compiler import START, CompileError, compile_commands


def cmd(cid, channel, anchor=START, offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def switch(name, current):
    return {"name": name, "current": current}


def cond(cid, **requires):
    return {"id": cid, "requires": requires}


def times_of(result):
    return {
        entry["id"]: (entry["start_ms"], entry["end_ms"])
        for entry in result["timeline"]
    }


class BasicSelectionTests(unittest.TestCase):
    def test_no_switches_single_combination_all_enabled(self):
        # 无开关时只有 1 个组合：全部口令启用
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("b", "main", "a", 0, 500),
        ]
        result = select_branch(commands, [], [])
        self.assertEqual(result["values"], {})
        self.assertEqual(result["flipped"], [])
        self.assertEqual(
            [entry["id"] for entry in result["timeline"]], ["a", "b"]
        )

    def test_unconditional_commands_always_enabled(self):
        # 未设置条件的口令在所有组合中都启用；当前取值已安全时翻转数为 0
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("b", "main", "a", 0, 500),
        ]
        result = select_branch(commands, [switch("s", False)], [])
        self.assertEqual(result["values"], {"s": False})
        self.assertEqual(result["flipped"], [])
        self.assertEqual(times_of(result), {"a": (0, 1000), "b": (1000, 1500)})

    def test_condition_gates_command_and_drops_it_from_timeline(self):
        # x 仅在 s=true 时启用，但与 a 冲突 -> 只能翻转为 false 将其禁用
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("x", "main", START, 200, 500),  # [200, 700) 与 a 重叠
        ]
        result = select_branch(
            commands, [switch("s", True)], [cond("x", s=True)]
        )
        self.assertEqual(result["values"], {"s": False})
        self.assertEqual(result["flipped"], ["s"])
        self.assertEqual(times_of(result), {"a": (0, 1000)})

    def test_empty_requires_means_always_enabled(self):
        commands = [cmd("x", "main", START, 0, 100)]
        result = select_branch(
            commands, [switch("s", True)], [cond("x")]
        )
        self.assertEqual(result["values"], {"s": True})
        self.assertEqual(result["flipped"], [])
        self.assertEqual(times_of(result), {"x": (0, 100)})

    def test_condition_requiring_false(self):
        # 条件要求 false：s=false 时 x 启用，与 a 冲突 -> 只能保持 s=true
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("x", "main", START, 200, 500),
        ]
        result = select_branch(
            commands, [switch("s", True)], [cond("x", s=False)]
        )
        self.assertEqual(result["values"], {"s": True})
        self.assertEqual(result["flipped"], [])
        self.assertEqual(times_of(result), {"a": (0, 1000)})

    def test_empty_commands_and_empty_conditions(self):
        result = select_branch([], [switch("s", True)], [])
        self.assertEqual(result["values"], {"s": True})
        self.assertEqual(result["flipped"], [])
        self.assertEqual(result["timeline"], [])


class AnchorClosureTests(unittest.TestCase):
    def test_enabled_command_with_disabled_anchor_invalidates_combination(self):
        # y 无条件启用且锚定 x；s=false 时 x 被禁用 -> 该组合无效，只能选 s=true
        commands = [
            cmd("x", "main", START, 0, 100),
            cmd("y", "sub", "x", 0, 100),
        ]
        result = select_branch(
            commands, [switch("s", False)], [cond("x", s=True)]
        )
        self.assertEqual(result["values"], {"s": True})
        self.assertEqual(result["flipped"], ["s"])
        self.assertEqual(times_of(result), {"x": (0, 100), "y": (100, 200)})

    def test_disabled_anchor_chain_invalidates_regardless_of_channel(self):
        # 跨通道同样检查锚点闭合：z 锚定 y、y 锚定 x，x 禁用则整链无效
        commands = [
            cmd("x", "main", START, 0, 100),
            cmd("y", "sub", "x", 0, 100),
            cmd("z", "third", "y", 0, 100),
        ]
        result = select_branch(
            commands,
            [switch("s", False)],
            [cond("x", s=True), cond("y", s=True)],
        )
        self.assertEqual(result["values"], {"s": True})
        self.assertEqual(
            times_of(result),
            {"x": (0, 100), "y": (100, 200), "z": (200, 300)},
        )

    def test_anchor_on_start_always_available(self):
        commands = [cmd("a", "main", START, 0, 100)]
        result = select_branch(commands, [switch("s", True)], [])
        self.assertEqual(result["values"], {"s": True})
        self.assertEqual(times_of(result), {"a": (0, 100)})


class ConflictExclusionTests(unittest.TestCase):
    def test_touching_intervals_are_safe(self):
        # x 与 a 首尾相接（a 的 end 等于 x 的 start），不算冲突
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("x", "main", "a", 0, 500),  # [1000, 1500)
        ]
        result = select_branch(
            commands, [switch("s", True)], [cond("x", s=True)]
        )
        self.assertEqual(result["values"], {"s": True})
        self.assertEqual(result["flipped"], [])
        self.assertEqual(times_of(result), {"a": (0, 1000), "x": (1000, 1500)})

    def test_cross_channel_overlap_is_safe(self):
        # 不同通道的时间重叠互不影响
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("x", "sub", START, 200, 500),
        ]
        result = select_branch(
            commands, [switch("s", True)], [cond("x", s=True)]
        )
        self.assertEqual(result["values"], {"s": True})
        self.assertEqual(result["flipped"], [])

    def test_only_max_disable_combination_safe(self):
        # w 无条件启用且与 x、y 都重叠：只有同时禁用 x、y 的组合安全
        commands = [
            cmd("w", "main", START, 0, 100),
            cmd("x", "main", START, 0, 100),
            cmd("y", "main", START, 0, 100),
        ]
        switches = [switch("s1", True), switch("s2", True)]
        conditions = [cond("x", s1=True), cond("y", s2=True)]
        result = select_branch(commands, switches, conditions)
        self.assertEqual(result["values"], {"s1": False, "s2": False})
        self.assertEqual(result["flipped"], ["s1", "s2"])
        self.assertEqual(times_of(result), {"w": (0, 100)})


class SelectionObjectiveTests(unittest.TestCase):
    def test_min_flips_wins(self):
        # (T,F) 翻转 1 次且安全；(F,F) 翻转 2 次也安全 -> 选翻转少的 (T,F)
        commands = [
            cmd("x", "main", START, 0, 100),
            cmd("y", "main", START, 200, 100),
            cmd("w", "main", START, 250, 100),  # 只与 y 重叠
        ]
        switches = [switch("s1", True), switch("s2", True)]
        conditions = [cond("x", s1=True), cond("y", s2=True)]
        result = select_branch(commands, switches, conditions)
        self.assertEqual(result["values"], {"s1": True, "s2": False})
        self.assertEqual(result["flipped"], ["s2"])
        self.assertEqual(times_of(result), {"x": (0, 100), "w": (250, 350)})

    def test_tie_break_false_before_true_in_input_order(self):
        # (T,F) 与 (F,T) 同为 1 次翻转的安全组合；按输入顺序 false < true，
        # (F, T) 字典序更小 -> 翻转 s1、保留 s2
        commands = [
            cmd("x", "main", START, 0, 100),
            cmd("y", "main", START, 0, 100),
        ]
        switches = [switch("s1", True), switch("s2", True)]
        conditions = [cond("x", s1=True), cond("y", s2=True)]
        result = select_branch(commands, switches, conditions)
        self.assertEqual(result["values"], {"s1": False, "s2": True})
        self.assertEqual(result["flipped"], ["s1"])
        self.assertEqual(times_of(result), {"y": (0, 100)})

    def test_flipped_names_follow_switch_input_order(self):
        # 开关输入顺序为 [zeta, alpha]，flipped 也按该顺序（而非按名字典序）
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("x", "main", START, 200, 500),
            cmd("y", "main", START, 200, 500),
        ]
        switches = [switch("zeta", True), switch("alpha", True)]
        conditions = [cond("x", zeta=True), cond("y", alpha=True)]
        result = select_branch(commands, switches, conditions)
        self.assertEqual(result["values"], {"zeta": False, "alpha": False})
        self.assertEqual(result["flipped"], ["zeta", "alpha"])
        self.assertEqual(list(result["values"]), ["zeta", "alpha"])

    def test_eight_switches_full_enumeration(self):
        # 8 个开关共 256 个组合：x 要求全部开关为 true，但与 a 冲突，
        # 故恰有 1 个组合不安全；1 次翻转的 8 个组合并列，取字典序最小者
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("x", "main", START, 200, 500),
        ]
        switches = [switch(f"s{i}", True) for i in range(MAX_SWITCHES)]
        conditions = [cond("x", **{f"s{i}": True for i in range(MAX_SWITCHES)})]
        result = select_branch(commands, switches, conditions)
        expected = {f"s{i}": True for i in range(MAX_SWITCHES)}
        expected["s0"] = False
        self.assertEqual(result["values"], expected)
        self.assertEqual(result["flipped"], ["s0"])
        self.assertEqual(times_of(result), {"a": (0, 1000)})


class ErrorTests(unittest.TestCase):
    def test_branch_error_is_compile_error(self):
        # HTTP 层依赖 CompileError 统一转换为 422
        self.assertTrue(issubclass(BranchError, CompileError))

    def test_no_safe_combination(self):
        # a、b 无条件启用且在 main 重叠，唯一组合不安全
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("b", "main", START, 500, 1000),
        ]
        with self.assertRaises(BranchError) as ctx:
            select_branch(commands, [], [])
        self.assertEqual(ctx.exception.code, "NO_SAFE_COMBINATION")
        self.assertEqual(ctx.exception.details, {"combinations": 1})

    def test_no_safe_combination_details_count_all_combinations(self):
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("b", "main", START, 500, 1000),
        ]
        with self.assertRaises(BranchError) as ctx:
            select_branch(commands, [switch("s", True)], [])
        self.assertEqual(ctx.exception.code, "NO_SAFE_COMBINATION")
        self.assertEqual(ctx.exception.details, {"combinations": 2})

    def test_unknown_switch_in_condition(self):
        commands = [cmd("a", "main", START, 0, 100)]
        with self.assertRaises(BranchError) as ctx:
            select_branch(
                commands, [switch("s", True)], [cond("a", ghost=True)]
            )
        self.assertEqual(ctx.exception.code, "UNKNOWN_SWITCH")
        self.assertEqual(ctx.exception.details, {"switches": ["ghost"]})

    def test_unknown_command_id_in_condition(self):
        commands = [cmd("a", "main", START, 0, 100)]
        with self.assertRaises(BranchError) as ctx:
            select_branch(
                commands, [switch("s", True)], [cond("ghost", s=True)]
            )
        self.assertEqual(ctx.exception.code, "CONDITION_TARGET_NOT_FOUND")
        self.assertEqual(ctx.exception.details, {"ids": ["ghost"]})

    def test_unknown_command_id_reported_before_unknown_switch(self):
        commands = [cmd("a", "main", START, 0, 100)]
        with self.assertRaises(BranchError) as ctx:
            select_branch(
                commands, [switch("s", True)], [cond("ghost", ghost_sw=True)]
            )
        self.assertEqual(ctx.exception.code, "CONDITION_TARGET_NOT_FOUND")

    def test_duplicate_switch_name(self):
        commands = [cmd("a", "main", START, 0, 100)]
        with self.assertRaises(BranchError) as ctx:
            select_branch(commands, [switch("s", True), switch("s", False)], [])
        self.assertEqual(ctx.exception.code, "DUPLICATE_SWITCH")
        self.assertEqual(ctx.exception.details, {"duplicates": ["s"]})

    def test_empty_switch_name(self):
        commands = [cmd("a", "main", START, 0, 100)]
        with self.assertRaises(BranchError) as ctx:
            select_branch(commands, [switch("", True)], [])
        self.assertEqual(ctx.exception.code, "INVALID_SWITCH")

    def test_too_many_switches(self):
        commands = [cmd("a", "main", START, 0, 100)]
        switches = [switch(f"s{i}", True) for i in range(MAX_SWITCHES + 1)]
        with self.assertRaises(BranchError) as ctx:
            select_branch(commands, switches, [])
        self.assertEqual(ctx.exception.code, "TOO_MANY_SWITCHES")
        self.assertEqual(
            ctx.exception.details,
            {"count": MAX_SWITCHES + 1, "limit": MAX_SWITCHES},
        )

    def test_duplicate_condition_id(self):
        commands = [cmd("a", "main", START, 0, 100)]
        with self.assertRaises(BranchError) as ctx:
            select_branch(
                commands,
                [switch("s", True)],
                [cond("a", s=True), cond("a", s=False)],
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        self.assertEqual(ctx.exception.details, {"duplicates": ["a"]})

    def test_empty_condition_id(self):
        commands = [cmd("a", "main", START, 0, 100)]
        with self.assertRaises(BranchError) as ctx:
            select_branch(commands, [switch("s", True)], [cond("", s=True)])
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_compile_errors_take_priority(self):
        # 口令编排非法时优先返回编排错误码（即便开关本身也非法）
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("a", "main", START, 0, 100),
        ]
        with self.assertRaises(CompileError) as ctx:
            select_branch(commands, [switch("", True)], [])
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")

    def test_missing_anchor_propagates(self):
        commands = [cmd("a", "main", "ghost", 0, 100)]
        with self.assertRaises(CompileError) as ctx:
            select_branch(commands, [], [])
        self.assertEqual(ctx.exception.code, "MISSING_ANCHOR")


class RobustnessTests(unittest.TestCase):
    def test_input_not_mutated(self):
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("x", "main", START, 200, 500),
        ]
        switches = [switch("s", True)]
        conditions = [cond("x", s=True)]
        snapshot = deepcopy((commands, switches, conditions))
        select_branch(commands, switches, conditions)
        self.assertEqual((commands, switches, conditions), snapshot)

    def test_timeline_matches_compile_of_enabled_subset(self):
        # 最终时间线应与「对启用口令子集直接调用编排器」的结果一致
        commands = [
            cmd("a", "main", START, 0, 1000),
            cmd("b", "main", "a", 0, 500),
            cmd("x", "main", START, 200, 500),
            cmd("y", "sub", "x", 0, 300),
        ]
        switches = [switch("show_x", True), switch("show_y", True)]
        conditions = [cond("x", show_x=True), cond("y", show_y=True)]
        result = select_branch(commands, switches, conditions)
        self.assertEqual(
            result["values"], {"show_x": False, "show_y": False}
        )
        expected = compile_commands(
            [cmd("a", "main", START, 0, 1000), cmd("b", "main", "a", 0, 500)]
        )
        self.assertEqual(result["timeline"], expected["timeline"])

    def test_timeline_entry_fields_complete(self):
        commands = [cmd("a", "main", START, 0, 1000)]
        result = select_branch(commands, [], [])
        self.assertEqual(
            result["timeline"][0],
            {
                "id": "a",
                "channel": "main",
                "anchor": "START",
                "offset_ms": 0,
                "duration_ms": 1000,
                "start_ms": 0,
                "end_ms": 1000,
            },
        )


if __name__ == "__main__":
    unittest.main()
