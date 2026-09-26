"""app.routing 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import time
import unittest

from app.compiler import CompileError, START
from app.routing import MAX_BACKUP_TARGETS, RoutingError, assign_routing


def cmd(cid, channel, anchor=START, offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def channels_of(result):
    return {entry["id"]: entry["channel"] for entry in result["timeline"]}


def times_of(result):
    return {
        entry["id"]: (entry["start_ms"], entry["end_ms"])
        for entry in result["timeline"]
    }


class AssignBasicTests(unittest.TestCase):
    def test_no_backups_returns_original_timeline(self):
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 100, 100),  # 与 a 相接，不冲突
        ]
        result = assign_routing(commands, [])
        self.assertEqual(channels_of(result), {"a": "main", "b": "main"})
        self.assertEqual(result["changed_ids"], [])
        self.assertEqual(
            [(e["id"], e["start_ms"], e["end_ms"]) for e in result["timeline"]],
            [("a", 0, 100), ("b", 100, 200)],
        )

    def test_touching_intervals_may_share_channel(self):
        # a[0,100) b[100,200) 首尾相接，即便有备用通道也无需改派
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 100, 100),
        ]
        backups = [{"id": "a", "backup_channels": ["alt"]}, {"id": "b", "backup_channels": ["alt"]}]
        result = assign_routing(commands, backups)
        self.assertEqual(result["changed_ids"], [])
        self.assertEqual(channels_of(result), {"a": "main", "b": "main"})

    def test_moves_minimum_count_to_resolve_overlap(self):
        # a[0,100) 与 b[50,150) 在 main 重叠：至少改派一条。
        # 1-move 方案按 id 的通道序列：a 留下 [main, alt] vs b 留下 [alt, main]，
        # 通道名直接按字典序比较，[alt, main] 更小 -> a 改派
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 50, 100),
        ]
        backups = [
            {"id": "a", "backup_channels": ["alt"]},
            {"id": "b", "backup_channels": ["alt"]},
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(result["changed_ids"], ["a"])
        self.assertEqual(channels_of(result), {"a": "alt", "b": "main"})

    def test_only_listed_command_can_move(self):
        # 重叠对中只有 a 有备用通道，但 a 与 b 同 id 字典序更小；b 未列出不能动，
        # 因此必须改派 a
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 50, 100),
        ]
        backups = [{"id": "a", "backup_channels": ["alt"]}]
        result = assign_routing(commands, backups)
        self.assertEqual(result["changed_ids"], ["a"])
        self.assertEqual(channels_of(result), {"a": "alt", "b": "main"})

    def test_original_channel_always_available_even_if_not_listed(self):
        # 备用列表只给 alt；a 可以选择留在 main（与 c 不冲突时应留下，改派数为 0）
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("c", "sub", START, 0, 100),
        ]
        backups = [{"id": "a", "backup_channels": ["alt"]}]
        result = assign_routing(commands, backups)
        self.assertEqual(result["changed_ids"], [])
        self.assertEqual(channels_of(result)["a"], "main")

    def test_times_never_change_and_timeline_keeps_compile_order(self):
        commands = [
            cmd("a", "main", START, 100, 100),
            cmd("b", "main", "a", -50, 100),  # [150,250) 与 a[100,200) 重叠
        ]
        original = {e["id"]: (e["start_ms"], e["end_ms"]) for e in __import__(
            "app.compiler", fromlist=["compile_commands"]
        ).compile_commands([dict(c) for c in commands])["timeline"]}
        result = assign_routing(commands, [{"id": "b", "backup_channels": ["alt"]}])
        self.assertEqual(times_of(result), original)
        self.assertEqual([e["id"] for e in result["timeline"]], ["a", "b"])
        # 其他字段也保持不变
        entry_b = result["timeline"][1]
        self.assertEqual(entry_b["anchor"], "a")
        self.assertEqual(entry_b["offset_ms"], -50)
        self.assertEqual(entry_b["duration_ms"], 100)


class TieBreakTests(unittest.TestCase):
    def test_tie_break_lexicographic_channel_sequence_sorted_by_id(self):
        # 固定口令 f 占住 main；a、b 相互重叠且都与 f 重叠，二者都不能留 main，
        # 只能取 alt / backup 且必须互不相同：改派数恒为 2。
        # 序列 [alt, backup] < [backup, alt] -> a=alt, b=backup
        commands = [
            cmd("f", "main", START, 0, 1000),
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 50, 100),
        ]
        backups = [
            {"id": "b", "backup_channels": ["alt", "backup"]},
            {"id": "a", "backup_channels": ["alt", "backup"]},
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(
            channels_of(result),
            {"f": "main", "a": "alt", "b": "backup"},
        )
        self.assertEqual(result["changed_ids"], ["a", "b"])

    def test_tie_break_considers_original_channel_in_sequence(self):
        # a、b 重叠，a 备选 {zzz}，b 备选 {aaa}：各改 1 条，
        # 留下方案：(main, aaa) 序列 [main, aaa] vs (zzz, main) 序列 [zzz, main]
        # [main, ...] 更小 -> b 改派
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 50, 100),
        ]
        backups = [
            {"id": "a", "backup_channels": ["zzz"]},
            {"id": "b", "backup_channels": ["aaa"]},
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(channels_of(result), {"a": "main", "b": "aaa"})
        self.assertEqual(result["changed_ids"], ["b"])

    def test_tie_break_sequence_ordered_by_id_not_input_order(self):
        # 输入顺序 b 在前，裁决仍按 id 排序：a 备选 aaa，b 备选 zzz ->
        # (main, zzz) vs (aaa, main)，[aaa, main] < [main, zzz] -> a 改派
        commands = [
            cmd("b", "main", START, 50, 100),
            cmd("a", "main", START, 0, 100),
        ]
        backups = [
            {"id": "b", "backup_channels": ["zzz"]},
            {"id": "a", "backup_channels": ["aaa"]},
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(channels_of(result), {"a": "aaa", "b": "main"})

    def test_lexicographic_minimum_among_equal_move_counts(self):
        # 三者两两重叠（同一时间窗），全部 main；a/b 共享备选 alt，a 另有专属 aaa，
        # c 有专属 ccc。main 只能留 1 条，剩余两条需落在互不相同的备选通道，
        # 最小改派数为 2。全部 2-move 方案（按 id 的通道序列）：
        #   a 留 main: b=alt,c=ccc -> [main, alt, ccc]
        #   b 留 main: a=aaa,c=ccc -> [aaa, main, ccc]
        #   c 留 main: a=aaa,b=alt -> [aaa, alt, main]
        # 字典序最小为 [aaa, alt, main] -> c 留 main，a=aaa、b=alt
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 10, 100),
            cmd("c", "main", START, 20, 100),
        ]
        backups = [
            {"id": "a", "backup_channels": ["aaa", "alt"]},
            {"id": "b", "backup_channels": ["alt"]},
            {"id": "c", "backup_channels": ["ccc"]},
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(
            channels_of(result), {"a": "aaa", "b": "alt", "c": "main"}
        )
        self.assertEqual(result["changed_ids"], ["a", "b"])

    def test_tie_break_smallest_id_stays_when_original_name_is_lex_smallest(self):
        # 原通道名 aaa 比备选通道名 main 更小：1-move 方案
        # [aaa, main]（a 留）< [main, aaa]（b 留）-> b 改派
        commands = [
            cmd("a", "aaa", START, 0, 100),
            cmd("b", "aaa", START, 50, 100),
        ]
        backups = [
            {"id": "a", "backup_channels": ["main"]},
            {"id": "b", "backup_channels": ["main"]},
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(channels_of(result), {"a": "aaa", "b": "main"})
        self.assertEqual(result["changed_ids"], ["b"])

    def test_assigned_channels_must_not_conflict_with_each_other(self):
        # f 固定在 main 与三者均重叠，故三者都不能留 main；备选通道各不同，
        # 需要 3 个不同通道，按 id 字典序取 a=aaa、b=bbb、c=ccc
        commands = [
            cmd("f", "main", START, 0, 100),
            cmd("a", "main", START, 10, 10),
            cmd("b", "main", START, 20, 10),
            cmd("c", "main", START, 30, 10),
        ]
        backups = [
            {"id": "c", "backup_channels": ["ccc"]},
            {"id": "a", "backup_channels": ["aaa"]},
            {"id": "b", "backup_channels": ["bbb"]},
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(
            channels_of(result),
            {"f": "main", "a": "aaa", "b": "bbb", "c": "ccc"},
        )


class FixedCommandTests(unittest.TestCase):
    def test_fixed_command_blocks_original_and_backup(self):
        # f 固定在 main，与 a 时间重叠；a 备选 blocked（该通道也有固定口令 g 重叠），
        # 只有 free 可用 -> a 改派 free
        commands = [
            cmd("f", "main", START, 0, 100),
            cmd("g", "blocked", START, 0, 100),
            cmd("a", "main", START, 50, 100),
        ]
        backups = [{"id": "a", "backup_channels": ["blocked", "free"]}]
        result = assign_routing(commands, backups)
        self.assertEqual(channels_of(result)["a"], "free")
        self.assertEqual(result["changed_ids"], ["a"])

    def test_touching_fixed_interval_does_not_block(self):
        # f [0,100) 固定 main；a [100,200) 与 f 首尾相接，可以留在 main
        commands = [
            cmd("f", "main", START, 0, 100),
            cmd("a", "main", START, 100, 100),
        ]
        backups = [{"id": "a", "backup_channels": ["alt"]}]
        result = assign_routing(commands, backups)
        self.assertEqual(channels_of(result)["a"], "main")
        self.assertEqual(result["changed_ids"], [])

    def test_two_fixed_commands_overlapping_is_infeasible(self):
        commands = [
            cmd("f1", "main", START, 0, 100),
            cmd("f2", "main", START, 50, 100),
        ]
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(commands, [])
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_ROUTING")
        self.assertEqual(ctx.exception.details["reason"], "FIXED_OVERLAP")

    def test_empty_backup_channel_list_at_core_means_original_only(self):
        # 核心层收到空列表：等价于只允许原通道；原通道被固定口令占用 -> 无解。
        # HTTP 层由 Pydantic min_length=1 提前以 INVALID_REQUEST 拦截。
        commands = [
            cmd("f", "main", START, 0, 100),
            cmd("a", "main", START, 50, 100),
        ]
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(commands, [{"id": "a", "backup_channels": []}])
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_ROUTING")
        self.assertEqual(ctx.exception.details["reason"], "NO_AVAILABLE_CHANNEL")

    def test_every_candidate_blocked_is_infeasible(self):
        commands = [
            cmd("f", "main", START, 0, 100),
            cmd("g", "alt", START, 0, 100),
            cmd("a", "main", START, 50, 100),
        ]
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(commands, [{"id": "a", "backup_channels": ["alt"]}])
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_ROUTING")
        self.assertEqual(ctx.exception.details["reason"], "NO_AVAILABLE_CHANNEL")
        self.assertEqual(ctx.exception.details["ids"], ["a"])

    def test_chained_moves_preferred_by_global_minimum(self):
        # a[0,100)、b[50,150)、c[120,220) 全在 main：
        # 冲突对 (a,b)、(b,c)，a 与 c 不冲突。改派 b 一条即可全部消解。
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 50, 100),
            cmd("c", "main", START, 120, 100),
        ]
        backups = [
            {"id": spec, "backup_channels": ["alt"]} for spec in ("a", "b", "c")
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(result["changed_ids"], ["b"])
        self.assertEqual(
            channels_of(result), {"a": "main", "b": "alt", "c": "main"}
        )


class ValidationTests(unittest.TestCase):
    def test_unknown_id(self):
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(
                [cmd("a", "main")],
                [{"id": "ghost", "backup_channels": ["alt"]}],
            )
        self.assertEqual(ctx.exception.code, "ROUTING_TARGET_NOT_FOUND")
        self.assertEqual(ctx.exception.details["ids"], ["ghost"])

    def test_empty_id(self):
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(
                [cmd("a", "main")],
                [{"id": "", "backup_channels": ["alt"]}],
            )
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_duplicate_id_declarations(self):
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(
                [cmd("a", "main")],
                [
                    {"id": "a", "backup_channels": ["x"]},
                    {"id": "a", "backup_channels": ["y"]},
                ],
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        self.assertEqual(ctx.exception.details["duplicates"], ["a"])

    def test_empty_backup_channel_name(self):
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(
                [cmd("a", "main")],
                [{"id": "a", "backup_channels": ["alt", ""]}],
            )
        self.assertEqual(ctx.exception.code, "INVALID_CHANNEL")

    def test_duplicate_backup_channel_name(self):
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(
                [cmd("a", "main")],
                [{"id": "a", "backup_channels": ["alt", "alt"]}],
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_BACKUP_CHANNEL")
        self.assertEqual(ctx.exception.details["ids"], ["a"])

    def test_more_than_twelve_targets(self):
        commands = [cmd(f"c{i:02d}", "main", START, i, 1) for i in range(13)]
        backups = [
            {"id": f"c{i:02d}", "backup_channels": ["alt"]} for i in range(13)
        ]
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(commands, backups)
        self.assertEqual(ctx.exception.code, "TOO_MANY_BACKUP_TARGETS")
        self.assertEqual(ctx.exception.details["count"], 13)
        self.assertEqual(ctx.exception.details["limit"], MAX_BACKUP_TARGETS)

    def test_exactly_twelve_targets_allowed(self):
        commands = [cmd(f"c{i:02d}", "main", START, i * 100, 10) for i in range(12)]
        backups = [
            {"id": f"c{i:02d}", "backup_channels": ["alt"]} for i in range(12)
        ]
        result = assign_routing(commands, backups)
        self.assertEqual(result["changed_ids"], [])

    def test_original_channel_duplicated_in_backup_list_is_rejected(self):
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(
                [cmd("a", "main")],
                [{"id": "a", "backup_channels": ["main", "alt"]}],
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_BACKUP_CHANNEL")

    def test_compile_errors_are_reused(self):
        # 重复 id / 依赖环 / 负起点等编排错误沿用 /compile 的错误码
        with self.assertRaises(CompileError) as ctx:
            assign_routing([cmd("a", "main"), cmd("a", "main")], [])
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        with self.assertRaises(CompileError) as ctx:
            assign_routing([cmd("a", "main", START, -1, 10)], [])
        self.assertEqual(ctx.exception.code, "NEGATIVE_START")

    def test_does_not_mutate_inputs(self):
        import copy

        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 50, 100),
        ]
        backups = [
            {"id": "a", "backup_channels": ["alt"]},
            {"id": "b", "backup_channels": ["alt"]},
        ]
        cmd_snapshot = copy.deepcopy(commands)
        backup_snapshot = copy.deepcopy(backups)
        assign_routing(commands, backups)
        self.assertEqual(commands, cmd_snapshot)
        self.assertEqual(backups, backup_snapshot)


class InfeasibleTests(unittest.TestCase):
    def test_clique_needs_more_distinct_channels_than_available(self):
        # a、b、c 两两重叠，候选通道只有 main 与 alt（a/b/c 备选均为 alt）：
        # 2 个通道放不下 3 个两两重叠的口令
        commands = [
            cmd("a", "main", START, 0, 100),
            cmd("b", "main", START, 10, 100),
            cmd("c", "main", START, 20, 100),
        ]
        backups = [
            {"id": spec, "backup_channels": ["alt"]} for spec in ("a", "b", "c")
        ]
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(commands, backups)
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_ROUTING")
        self.assertEqual(ctx.exception.details["reason"], "INSUFFICIENT_CHANNEL_CAPACITY")

    def test_twelve_clique_with_two_channels_each_is_fast_and_infeasible(self):
        # 12 条口令两两重叠，原通道均为 main，备选通道只有共享的 alt：
        # 2 个通道放不下 12 个两两重叠的口令 -> 无解；验证剪枝与性能（须很快失败）
        commands = [cmd("a", "main", START, 0, 1000)]
        for i in range(1, 12):
            commands.append(cmd(f"a{i:02d}", "main", START, i, 1000))
        ids_ = ["a"] + [f"a{i:02d}" for i in range(1, 12)]
        backups = [
            {"id": cid, "backup_channels": ["alt"]} for cid in ids_
        ]
        started = time.monotonic()
        with self.assertRaises(RoutingError) as ctx:
            assign_routing(commands, backups)
        elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_ROUTING")
        self.assertLess(elapsed, 5.0)

    def test_twelve_clique_feasible_with_enough_alt_channels(self):
        # 12 条两两重叠；每人有各自独立的备选通道：恰好 1 留 main、其余改派。
        # 通道名直接按字典序比较（alt0 < ... < alt10 < main），lex-min 方案中
        # 最小编号的 a 取最小通道名 alt0……留 main 的是 id 最大的 a11。
        commands = [cmd("a", "main", START, 0, 100)]
        for i in range(1, 12):
            commands.append(cmd(f"a{i:02d}", "main", START, i, 100))
        ids_ = ["a"] + [f"a{i:02d}" for i in range(1, 12)]
        backups = [
            {"id": cid, "backup_channels": [f"alt{i}"]}
            for i, cid in enumerate(ids_)
        ]
        started = time.monotonic()
        result = assign_routing(commands, backups)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        chosen = channels_of(result)
        # lex-min 序列把最大通道名 main 放到最后一个 id：a11 留 main
        self.assertEqual(chosen["a"], "alt0")
        self.assertEqual(chosen["a11"], "main")
        self.assertEqual(set(result["changed_ids"]), set(ids_) - {"a11"})


if __name__ == "__main__":
    unittest.main()
