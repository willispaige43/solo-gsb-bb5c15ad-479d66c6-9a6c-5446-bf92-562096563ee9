"""app.merge 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import unittest

from app.compiler import START, CompileError
from app.merge import merge_versions


def cmd(cid, channel, anchor=START, offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def ids_of(result):
    return [item["id"] for item in result["commands"]]


def by_id(result):
    return {item["id"]: item for item in result["commands"]}


class CleanMergeTests(unittest.TestCase):
    def test_identical_versions_keep_base(self):
        base = [cmd("a", "main"), cmd("b", "main", "a", 0, 100)]
        result = merge_versions(base, [dict(x) for x in base], [dict(x) for x in base])
        self.assertEqual(result["status"], "merged")
        self.assertEqual(ids_of(result), ["a", "b"])
        self.assertEqual(by_id(result)["b"]["anchor"], "a")

    def test_single_sided_modifications_are_adopted(self):
        base = [cmd("a", "main", duration=100), cmd("b", "main", duration=200)]
        left = [cmd("a", "alt", duration=100), cmd("b", "main", duration=200)]
        right = [cmd("a", "main", duration=100), cmd("b", "main", duration=300)]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "merged")
        merged = by_id(result)
        # left 改 a 的 channel、right 改 b 的 duration_ms：各自单侧改动直接采纳
        self.assertEqual(merged["a"]["channel"], "alt")
        self.assertEqual(merged["b"]["duration_ms"], 300)

    def test_different_fields_of_same_command_merge_together(self):
        # 两侧分别修改同一条口令的不同字段：合并到同一口令
        base = [cmd("a", "main", duration=100)]
        left = [cmd("a", "alt", duration=100)]
        right = [cmd("a", "main", duration=200)]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "merged")
        a = by_id(result)["a"]
        self.assertEqual(a["channel"], "alt")
        self.assertEqual(a["duration_ms"], 200)
        self.assertEqual(a["anchor"], START)
        self.assertEqual(a["offset_ms"], 0)

    def test_both_sides_change_to_same_value_is_adopted(self):
        base = [cmd("a", "main", duration=100)]
        left = [cmd("a", "main", duration=250)]
        right = [cmd("a", "main", duration=250)]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(by_id(result)["a"]["duration_ms"], 250)

    def test_single_sided_add_is_adopted_and_sorted_by_id(self):
        base = [cmd("b", "main")]
        left = [cmd("a", "main"), cmd("b", "main")]
        right = [cmd("b", "main")]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(ids_of(result), ["a", "b"])

    def test_single_sided_delete_is_adopted(self):
        base = [cmd("a", "main"), cmd("b", "main")]
        left = [cmd("b", "main")]
        right = [cmd("a", "main"), cmd("b", "main")]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(ids_of(result), ["b"])

    def test_both_sides_delete_is_adopted(self):
        base = [cmd("a", "main")]
        result = merge_versions(base, [], [])
        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["commands"], [])

    def test_both_sides_add_identical_command_is_adopted(self):
        result = merge_versions([], [cmd("x", "main", offset=5, duration=10)],
                                [cmd("x", "main", offset=5, duration=10)])
        self.assertEqual(result["status"], "merged")
        self.assertEqual(ids_of(result), ["x"])

    def test_defaults_are_filled_before_merge(self):
        # base 省略 anchor/offset；left 显式给默认值视为未改动
        base = [{"id": "a", "channel": "main", "duration_ms": 100}]
        left = [cmd("a", "main", START, 0, 200)]
        right = [{"id": "a", "channel": "alt", "duration_ms": 100}]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "merged")
        a = by_id(result)["a"]
        self.assertEqual(a["channel"], "alt")
        self.assertEqual(a["duration_ms"], 200)
        self.assertEqual(a["anchor"], START)
        self.assertEqual(a["offset_ms"], 0)

    def test_merge_result_compiles_timeline_and_conflicts(self):
        # a[0,1000) 与 c[800,1300) 同通道冲突；left 加 b（锚定 a），right 加 c
        base = [cmd("a", "main")]
        left = [cmd("a", "main"), cmd("b", "main", "a", 0, 500)]
        right = [cmd("a", "main"), cmd("c", "main", "a", -200, 500)]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(ids_of(result), ["a", "b", "c"])
        timeline = {e["id"]: (e["start_ms"], e["end_ms"]) for e in result["timeline"]}
        self.assertEqual(timeline["a"], (0, 1000))
        self.assertEqual(timeline["b"], (1000, 1500))
        self.assertEqual(timeline["c"], (800, 1300))
        conflicts = {(c["a"], c["b"]) for c in result["conflicts"]}
        self.assertEqual(conflicts, {("a", "c"), ("b", "c")})

    def test_inputs_are_not_mutated(self):
        base = [{"id": "a", "channel": "main", "duration_ms": 100}]
        left = [dict(base[0])]
        right = [dict(base[0])]
        merge_versions(base, left, right)
        self.assertNotIn("anchor", base[0])
        self.assertNotIn("offset_ms", base[0])


class DivergenceTests(unittest.TestCase):
    def test_both_sides_change_same_field_differently(self):
        base = [cmd("a", "main", duration=100)]
        left = [cmd("a", "alt", duration=100)]
        right = [cmd("a", "sub", duration=100)]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "diverged")
        self.assertNotIn("commands", result)
        self.assertEqual(len(result["divergences"]), 1)
        d = result["divergences"][0]
        self.assertEqual(
            d,
            {
                "id": "a",
                "field": "channel",
                "kind": "MODIFY_MODIFY",
                "base_value": "main",
                "left_value": "alt",
                "right_value": "sub",
            },
        )

    def test_divergences_sorted_by_id_then_field(self):
        base = [
            cmd("a", "main", duration=100),
            cmd("b", "main", duration=100),
            cmd("c", "main", duration=100),
        ]
        # c：offset_ms 与 duration_ms 两个分歧；a：channel 分歧；b：anchor 分歧
        left = [
            cmd("a", "alt", duration=100),
            cmd("b", "main", anchor="a", duration=100),
            cmd("c", "main", START, 10, 200),
        ]
        right = [
            cmd("a", "sub", duration=100),
            cmd("b", "main", anchor="c", duration=100),
            cmd("c", "main", START, 20, 300),
        ]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "diverged")
        keys = [(d["id"], d["field"]) for d in result["divergences"]]
        self.assertEqual(
            keys,
            [
                ("a", "channel"),
                ("b", "anchor"),
                ("c", "duration_ms"),
                ("c", "offset_ms"),
            ],
        )
        c_off = next(
            d for d in result["divergences"]
            if d["id"] == "c" and d["field"] == "offset_ms"
        )
        self.assertEqual((c_off["base_value"], c_off["left_value"], c_off["right_value"]),
                         (0, 10, 20))

    def test_delete_vs_modify_records_each_modified_field(self):
        base = [cmd("a", "main", "a", 0, 100)]
        left = []  # left 删除 a
        right = [cmd("a", "sub", "a", 5, 200)]  # right 改了 channel/offset/duration
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "diverged")
        divergences = result["divergences"]
        self.assertEqual([d["kind"] for d in divergences],
                         ["DELETE_MODIFY"] * 3)
        self.assertEqual([d["field"] for d in divergences],
                         ["channel", "duration_ms", "offset_ms"])
        for d in divergences:
            self.assertEqual(d["base_value"], {"channel": "main",
                                                "duration_ms": 100,
                                                "offset_ms": 0}[d["field"]])
            self.assertIsNone(d["left_value"])
        self.assertEqual(
            {d["field"]: d["right_value"] for d in divergences},
            {"channel": "sub", "duration_ms": 200, "offset_ms": 5},
        )

    def test_delete_vs_no_change_is_clean(self):
        # 一侧删除，另一侧保留且未改动：直接采纳删除
        base = [cmd("a", "main"), cmd("b", "main")]
        left = [cmd("b", "main")]
        right = [cmd("a", "main"), cmd("b", "main")]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(ids_of(result), ["b"])

    def test_both_add_same_id_different_content_records_divergence(self):
        left = [cmd("n", "main", START, 0, 100)]
        right = [cmd("n", "main", START, 0, 200)]
        result = merge_versions([], left, right)
        self.assertEqual(result["status"], "diverged")
        divergences = result["divergences"]
        self.assertEqual(len(divergences), 1)
        d = divergences[0]
        self.assertEqual(d["id"], "n")
        self.assertEqual(d["field"], "duration_ms")
        self.assertEqual(d["kind"], "ADD_ADD")
        self.assertIsNone(d["base_value"])
        self.assertEqual((d["left_value"], d["right_value"]), (100, 200))

    def test_both_add_multiple_field_differences(self):
        left = [cmd("n", "main", START, 0, 100)]
        right = [cmd("n", "alt", START, 5, 100)]
        result = merge_versions([], left, right)
        self.assertEqual(result["status"], "diverged")
        self.assertEqual(
            [(d["field"], d["kind"]) for d in result["divergences"]],
            [("channel", "ADD_ADD"), ("offset_ms", "ADD_ADD")],
        )

    def test_any_divergence_blocks_compile_result(self):
        # 有分歧时即使其它口令单侧新增也不返回合并口令
        base = [cmd("a", "main", duration=100)]
        left = [cmd("a", "alt", duration=100), cmd("z", "main")]
        right = [cmd("a", "sub", duration=100)]
        result = merge_versions(base, left, right)
        self.assertEqual(result["status"], "diverged")
        self.assertEqual([d["id"] for d in result["divergences"]], ["a"])
        self.assertEqual(set(result), {"status", "divergences"})


class ValidationPassthroughTests(unittest.TestCase):
    def _assert_code(self, func, code):
        with self.assertRaises(CompileError) as ctx:
            func()
        self.assertEqual(ctx.exception.code, code)

    def test_duplicate_id_within_a_version(self):
        dup = [cmd("a", "main"), cmd("a", "sub")]
        self._assert_code(lambda: merge_versions(dup, [], []), "DUPLICATE_ID")
        self._assert_code(lambda: merge_versions([], dup, []), "DUPLICATE_ID")
        self._assert_code(lambda: merge_versions([], [], dup), "DUPLICATE_ID")

    def test_missing_anchor_after_merge(self):
        # left 单侧新增 b，锚定不存在的 x；无分歧，编排校验返回 MISSING_ANCHOR
        base = [cmd("a", "main")]
        left = [cmd("a", "main"), cmd("b", "main", anchor="x")]
        right = [cmd("a", "main")]
        self._assert_code(lambda: merge_versions(base, left, right), "MISSING_ANCHOR")

    def test_dependency_cycle_after_merge(self):
        # left 单侧把 a 改锚到 b、right 单侧把 b 改锚到 a（不同字段不同口令），
        # 合本身无分歧，编排校验时成环
        base = [cmd("a", "main"), cmd("b", "main")]
        left = [cmd("a", "main", anchor="b"), cmd("b", "main")]
        right = [cmd("a", "main"), cmd("b", "main", anchor="a")]
        self._assert_code(lambda: merge_versions(base, left, right), "DEPENDENCY_CYCLE")

    def test_negative_start_after_merge(self):
        # 单侧把锚点时长缩短导致负起点
        base = [cmd("a", "main", START, 0, 100), cmd("b", "main", "a", -50, 100)]
        left = [cmd("a", "main", START, 0, 100), cmd("b", "main", "a", -50, 100)]
        right = [cmd("a", "main", START, 0, 10), cmd("b", "main", "a", -50, 100)]
        self._assert_code(lambda: merge_versions(base, left, right), "NEGATIVE_START")


if __name__ == "__main__":
    unittest.main()
