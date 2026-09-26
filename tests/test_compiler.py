"""app.compiler 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import copy
import unittest

from app.compiler import START, CompileError, compile_commands, preview_change


def cmd(cid, channel, anchor=START, offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def ids_of(timeline):
    return [entry["id"] for entry in timeline]


class TimelineTests(unittest.TestCase):
    def test_empty_input(self):
        result = compile_commands([])
        self.assertEqual(result, {"timeline": [], "conflicts": []})

    def test_single_start_item_and_defaults(self):
        result = compile_commands([{"id": "a", "channel": "cam", "duration_ms": 500}])
        self.assertEqual(len(result["timeline"]), 1)
        entry = result["timeline"][0]
        self.assertEqual(entry["start_ms"], 0)
        self.assertEqual(entry["end_ms"], 500)

    def test_chain_relative_to_anchor_end(self):
        # a: [0, 1000)；b 相对 a 结束时刻 +200，时长 300 -> [1200, 1500)
        result = compile_commands(
            [
                cmd("a", "cam", START, 0, 1000),
                cmd("b", "cam", "a", 200, 300),
            ]
        )
        by_id = {e["id"]: e for e in result["timeline"]}
        self.assertEqual((by_id["a"]["start_ms"], by_id["a"]["end_ms"]), (0, 1000))
        self.assertEqual((by_id["b"]["start_ms"], by_id["b"]["end_ms"]), (1200, 1500))

    def test_negative_offset_keeps_start_non_negative(self):
        # a: [1000, 2000)；b 相对 a 结束 -1500 -> [500, 600)
        result = compile_commands(
            [
                cmd("a", "cam", START, 1000, 1000),
                cmd("b", "cam", "a", -1500, 100),
            ]
        )
        by_id = {e["id"] for e in result["timeline"]}
        self.assertEqual(by_id, {"a", "b"})
        entries = {e["id"]: e for e in result["timeline"]}
        self.assertEqual(entries["b"]["start_ms"], 500)

    def test_sorted_by_start_then_id(self):
        # b 先开始；c 与 a 同起点，按 id 排序
        result = compile_commands(
            [
                cmd("a", "c1", START, 100, 100),
                cmd("b", "c1", START, 0, 100),
                cmd("c", "c1", START, 100, 100),
            ]
        )
        self.assertEqual(ids_of(result["timeline"]), ["b", "a", "c"])


class ConflictTests(unittest.TestCase):
    def test_touching_intervals_do_not_conflict(self):
        # a [0,1000)、b [1000,1200) 首尾相接
        result = compile_commands(
            [cmd("a", "cam", START, 0, 1000), cmd("b", "cam", "a", 0, 200)]
        )
        self.assertEqual(result["conflicts"], [])

    def test_overlap_pair_ordered_by_id_within_and_between_pairs(self):
        # c 与 a 重叠（c<a 字典序，对内应为 a,c）
        items = [
            cmd("a", "cam", START, 0, 1000),
            cmd("b", "cam", START, 2000, 100),
            cmd("c", "cam", START, 500, 100),
        ]
        result = compile_commands(items)
        pairs = [(c["a"], c["b"]) for c in result["conflicts"]]
        self.assertEqual(pairs, [("a", "c")])

    def test_multiple_pairs_sorted(self):
        # a[0,1000) b[100,200) c[150,300)：重叠对 (a,b)(a,c)(b,c)
        result = compile_commands(
            [
                cmd("a", "cam", START, 0, 1000),
                cmd("b", "cam", START, 100, 100),
                cmd("c", "cam", START, 150, 150),
            ]
        )
        pairs = [(c["a"], c["b"]) for c in result["conflicts"]]
        self.assertEqual(pairs, [("a", "b"), ("a", "c"), ("b", "c")])

    def test_different_channels_never_conflict(self):
        result = compile_commands(
            [
                cmd("a", "cam1", START, 0, 1000),
                cmd("b", "cam2", START, 0, 1000),
            ]
        )
        self.assertEqual(result["conflicts"], [])

    def test_conflict_payload_intervals_and_channel(self):
        result = compile_commands(
            [
                cmd("a", "cam", START, 0, 100),
                cmd("b", "cam", START, 50, 100),
            ]
        )
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["channel"], "cam")
        self.assertEqual(conflict["a_interval"], [0, 100])
        self.assertEqual(conflict["b_interval"], [50, 150])

    def test_break_early_when_reaching_non_overlapping_later_start(self):
        # a[0,100)，其后 b[100,200) 相接，c[100,200) 与 b 重叠、与 a 不重叠
        result = compile_commands(
            [
                cmd("a", "cam", START, 0, 100),
                cmd("b", "cam", START, 100, 100),
                cmd("c", "cam", START, 100, 100),
            ]
        )
        pairs = [(c["a"], c["b"]) for c in result["conflicts"]]
        self.assertEqual(pairs, [("b", "c")])


class ErrorTests(unittest.TestCase):
    def test_duplicate_id(self):
        with self.assertRaises(CompileError) as ctx:
            compile_commands(
                [cmd("a", "cam"), cmd("b", "cam"), cmd("a", "cam")]
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        self.assertEqual(ctx.exception.details["duplicates"], ["a"])

    def test_reserved_and_empty_id(self):
        with self.assertRaises(CompileError) as ctx:
            compile_commands([cmd("START", "cam")])
        self.assertEqual(ctx.exception.code, "INVALID_ID")
        with self.assertRaises(CompileError) as ctx:
            compile_commands([cmd("", "cam")])
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_empty_channel(self):
        with self.assertRaises(CompileError) as ctx:
            compile_commands([cmd("a", "")])
        self.assertEqual(ctx.exception.code, "INVALID_CHANNEL")

    def test_non_positive_duration(self):
        for bad in (0, -1):
            with self.assertRaises(CompileError) as ctx:
                compile_commands([cmd("a", "cam", duration=bad)])
            self.assertEqual(ctx.exception.code, "INVALID_DURATION")

    def test_missing_anchor(self):
        with self.assertRaises(CompileError) as ctx:
            compile_commands([cmd("a", "cam", anchor="ghost")])
        self.assertEqual(ctx.exception.code, "MISSING_ANCHOR")
        self.assertEqual(ctx.exception.details["ids"], ["a"])
        self.assertEqual(ctx.exception.details["anchors"], ["ghost"])

    def test_self_dependency_cycle(self):
        with self.assertRaises(CompileError) as ctx:
            compile_commands([cmd("a", "cam", anchor="a")])
        self.assertEqual(ctx.exception.code, "DEPENDENCY_CYCLE")
        self.assertEqual(ctx.exception.details["cycle"], ["a", "a"])

    def test_two_node_cycle(self):
        with self.assertRaises(CompileError) as ctx:
            compile_commands(
                [cmd("a", "cam", anchor="b"), cmd("b", "cam", anchor="a")]
            )
        self.assertEqual(ctx.exception.code, "DEPENDENCY_CYCLE")
        cycle = ctx.exception.details["cycle"]
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"a", "b"})

    def test_cycle_with_unrelated_prefix_already_resolved(self):
        # a -> b 是已解析的独立前缀链；x -> z -> y -> x 构成环
        items = [
            cmd("a", "cam", START, 0, 10),
            cmd("b", "cam", "a", 0, 10),
            cmd("x", "cam", anchor="z", duration=10),
            cmd("y", "cam", anchor="x", duration=10),
            cmd("z", "cam", anchor="y", duration=10),
        ]
        with self.assertRaises(CompileError) as ctx:
            compile_commands(items)
        self.assertEqual(ctx.exception.code, "DEPENDENCY_CYCLE")
        cycle = ctx.exception.details["cycle"]
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"x", "y", "z"})

    def test_negative_start_from_start_anchor(self):
        with self.assertRaises(CompileError) as ctx:
            compile_commands([cmd("a", "cam", START, -10, 100)])
        self.assertEqual(ctx.exception.code, "NEGATIVE_START")

    def test_negative_start_through_chain(self):
        # a [100, 200)；b 相对 a 结束 -300 -> -100，非法
        with self.assertRaises(CompileError) as ctx:
            compile_commands(
                [
                    cmd("a", "cam", START, 100, 100),
                    cmd("b", "cam", "a", -300, 10),
                ]
            )
        self.assertEqual(ctx.exception.code, "NEGATIVE_START")
        self.assertEqual(ctx.exception.details["id"], "b")
        self.assertEqual(ctx.exception.details["start_ms"], -100)

    def test_negative_start_reported_only_after_cycle_check(self):
        # 存在环时优先报环，避免对环上节点产生无意义时间
        with self.assertRaises(CompileError) as ctx:
            compile_commands(
                [
                    cmd("a", "cam", anchor="b", offset=-10, duration=10),
                    cmd("b", "cam", anchor="a", offset=-10, duration=10),
                ]
            )
        self.assertEqual(ctx.exception.code, "DEPENDENCY_CYCLE")


class PreviewTests(unittest.TestCase):
    def sample(self):
        return [
            cmd("a", "cam", START, 0, 100),    # [0, 100)
            cmd("b", "cam", "a", 0, 100),      # [100, 200) 依赖 a，传导
            cmd("c", "cam", START, 150, 100),  # [150, 250)
        ]

    def test_offset_shift_changes_target_and_dependents_sorted(self):
        items = self.sample()
        result = preview_change(items, {"id": "a", "offset_ms": 50})
        # a -> [50,150)；b 锚定 a 结束 -> [150,250)；c 不变
        self.assertEqual(result["changed_ids"], ["a", "b"])

    def test_duration_change_propagates_to_dependents(self):
        items = self.sample()
        result = preview_change(items, {"id": "a", "duration_ms": 200})
        # a -> [0,200)；b -> [200,300)；c 仍为 [150,250)
        self.assertEqual(result["changed_ids"], ["a", "b"])
        # a 延长后与 c 新重叠（携带改动后区间）；(b,c) 仍重叠，身份不变
        self.assertEqual(len(result["added_conflicts"]), 1)
        pair = result["added_conflicts"][0]
        self.assertEqual((pair["channel"], pair["a"], pair["b"]), ("cam", "a", "c"))
        self.assertEqual(pair["a_interval"], [0, 200])
        self.assertEqual(pair["b_interval"], [150, 250])
        self.assertEqual(result["removed_conflicts"], [])

    def test_no_change_fields_kept_intact(self):
        # offset 与 duration 均未变：changed 为空、冲突无增减
        items = self.sample()
        result = preview_change(items, {"id": "c", "offset_ms": 150})
        self.assertEqual(result["changed_ids"], [])
        self.assertEqual(result["added_conflicts"], [])
        self.assertEqual(result["removed_conflicts"], [])

    def test_added_conflict_uses_after_interval_and_is_sorted(self):
        # 原冲突仅 (b,c)；a 右移 60 后新增 (a,b)(a,c)，(b,c) 仍在
        result = preview_change(self.sample(), {"id": "a", "offset_ms": 60})
        # a[60,160) b[160,260) c[150,250) -> (a,c) 重叠；a,b 相接不冲突；b,c 重叠
        added = [(c["channel"], c["a"], c["b"]) for c in result["added_conflicts"]]
        self.assertEqual(added, [("cam", "a", "c")])
        pair = result["added_conflicts"][0]
        self.assertEqual(pair["a_interval"], [60, 160])
        self.assertEqual(pair["b_interval"], [150, 250])
        self.assertEqual(result["removed_conflicts"], [])

    def test_removed_conflict_uses_before_interval(self):
        # 原冲突 (b,c)：c 推迟到 300 后与 b 不再重叠，携带原区间
        result = preview_change(self.sample(), {"id": "c", "offset_ms": 300})
        self.assertEqual(len(result["removed_conflicts"]), 1)
        pair = result["removed_conflicts"][0]
        self.assertEqual((pair["channel"], pair["a"], pair["b"]), ("cam", "b", "c"))
        self.assertEqual(pair["a_interval"], [100, 200])
        self.assertEqual(pair["b_interval"], [150, 250])
        self.assertEqual(result["added_conflicts"], [])
        self.assertEqual(result["changed_ids"], ["c"])

    def test_added_and_removed_across_channels_sorted_by_a_b_channel(self):
        items = [
            cmd("a", "x", START, 0, 100),
            cmd("b", "x", START, 50, 100),   # 与 a 冲突
            cmd("p", "y", START, 0, 100),
            cmd("q", "y", START, 200, 100),  # 与 p 不冲突
        ]
        # a 推迟到 150：(x,a,b) 消失；p 不动... 改 p 的 duration 到 250 使 (y,p,q) 新增
        result = preview_change(items, {"id": "a", "offset_ms": 150})
        removed = [
            (c["a"], c["b"], c["channel"]) for c in result["removed_conflicts"]
        ]
        self.assertEqual(removed, [("a", "b", "x")])
        self.assertEqual(result["removed_conflicts"][0]["b_interval"], [50, 150])

        # 反向：p 延长 -> 新增 (y,p,q)，携带新区间
        result2 = preview_change(items, {"id": "p", "duration_ms": 250})
        added = [(c["a"], c["b"], c["channel"]) for c in result2["added_conflicts"]]
        self.assertEqual(added, [("p", "q", "y")])
        self.assertEqual(result2["added_conflicts"][0]["a_interval"], [0, 250])
        self.assertEqual(result2["added_conflicts"][0]["b_interval"], [200, 300])

    def test_does_not_mutate_original_request_data(self):
        items = self.sample()
        snapshot = copy.deepcopy(items)
        preview_change(items, {"id": "a", "offset_ms": 50, "duration_ms": 50})
        self.assertEqual(items, snapshot)

    def test_does_not_mutate_original_when_fields_omitted(self):
        # 缺省 anchor / offset_ms 的口令经编译后不应被回写 setdefault
        minimal = [{"id": "a", "channel": "cam", "duration_ms": 100}]
        snapshot = copy.deepcopy(minimal)
        result = preview_change(minimal, {"id": "a", "duration_ms": 200})
        self.assertEqual(result["changed_ids"], ["a"])
        self.assertEqual(minimal, snapshot)

    def test_target_id_missing(self):
        with self.assertRaises(CompileError) as ctx:
            preview_change(self.sample(), {"id": "ghost", "offset_ms": 1})
        self.assertEqual(ctx.exception.code, "CHANGE_TARGET_NOT_FOUND")
        self.assertEqual(ctx.exception.details["id"], "ghost")

    def test_patched_duration_invalid_reuses_validation(self):
        with self.assertRaises(CompileError) as ctx:
            preview_change(self.sample(), {"id": "a", "duration_ms": 0})
        self.assertEqual(ctx.exception.code, "INVALID_DURATION")
        self.assertEqual(ctx.exception.details["ids"], ["a"])

    def test_patched_negative_start_reuses_validation(self):
        with self.assertRaises(CompileError) as ctx:
            preview_change(self.sample(), {"id": "a", "offset_ms": -1})
        self.assertEqual(ctx.exception.code, "NEGATIVE_START")

    def test_original_errors_surfaced_first(self):
        with self.assertRaises(CompileError) as ctx:
            preview_change(
                [cmd("a", "cam"), cmd("a", "cam")],
                {"id": "a", "offset_ms": 1},
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")

    def test_change_only_overrides_given_fields(self):
        items = [
            cmd("a", "cam", START, 10, 100),  # [10, 110)
            cmd("b", "cam", "a", 5, 100),     # [115, 215)
        ]
        # 只改 offset_ms：duration 保留 100，channel/anchor 保留
        result = preview_change(items, {"id": "a", "offset_ms": 20})
        self.assertEqual(result["changed_ids"], ["a", "b"])
        pair_added = result["added_conflicts"]
        self.assertEqual(pair_added, [])


class LongChainTests(unittest.TestCase):
    def test_deep_chain_does_not_recurse(self):
        # 迭代 DFS 应能处理远超默认递归深度的依赖链
        n = 5000
        items = [cmd("c0", "cam", START, 0, 1)]
        for i in range(1, n):
            items.append(cmd(f"c{i}", "cam", f"c{i-1}", 0, 1))
        result = compile_commands(items)
        self.assertEqual(len(result["timeline"]), n)
        last = result["timeline"][-1]
        self.assertEqual(last["id"], f"c{n-1}")
        self.assertEqual(last["start_ms"], n - 1)


if __name__ == "__main__":
    unittest.main()
