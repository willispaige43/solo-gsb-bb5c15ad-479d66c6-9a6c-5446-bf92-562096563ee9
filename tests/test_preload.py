"""app.preload 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import random
import unittest
from itertools import combinations, permutations

from app.compiler import START, CompileError, compile_commands
from app.preload import MAX_PRELOAD_ASSETS, plan_preload


def cmd(cid, channel="m", anchor=START, offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def asset(aid, command_id, ready=0, load=10, priority=0):
    return {
        "id": aid,
        "command_id": command_id,
        "ready_ms": ready,
        "load_ms": load,
        "priority": priority,
    }


def brute_force(commands, assets):
    """枚举入选子集与全部排列，按题面五层目标求最优方案（与核心逻辑分开书写）。"""
    compiled = compile_commands([dict(c) for c in commands])
    starts = {entry["id"]: entry["start_ms"] for entry in compiled["timeline"]}
    by_id = {item["id"]: item for item in assets}

    best = None
    for r in range(len(assets), -1, -1):
        size_r: list[tuple] = []
        for chosen_ids in combinations(sorted(by_id), r):
            best_order = None
            for perm in permutations([by_id[i] for i in chosen_ids]):
                cursor = 0
                loads = []
                ok = True
                for item in perm:
                    start = max(cursor, item["ready_ms"])
                    end = start + item["load_ms"]
                    if end > starts[item["command_id"]]:
                        ok = False
                        break
                    loads.append(
                        {"id": item["id"], "start_ms": start, "end_ms": end}
                    )
                    cursor = end
                if not ok:
                    continue
                order_ids = [entry["id"] for entry in loads]
                if best_order is None or (cursor, order_ids) < (
                    best_order[0],
                    best_order[1],
                ):
                    best_order = (cursor, order_ids, loads)
            if best_order is not None:
                priority_sum = sum(
                    by_id[i]["priority"] for i in chosen_ids
                )
                size_r.append((priority_sum, list(chosen_ids), best_order))
        if size_r:
            size_r.sort(key=lambda entry: (-entry[0], entry[1]))
            _, chosen_list, best_order = size_r[0]
            skipped = sorted(set(by_id) - set(chosen_list))
            best = {"loads": best_order[2], "skipped_ids": skipped}
            break
    return best


class BasicPlanTests(unittest.TestCase):
    def setUp(self):
        # a [1000,2000)、b [2000,2500)、c [2500,3000)；起点即关联素材的截止时刻
        self.commands = [
            cmd("a", offset=1000, duration=1000),
            cmd("b", anchor="a", duration=500),
            cmd("c", anchor="b", duration=500),
        ]

    def test_serial_chain_with_wait(self):
        # x 0-400（截止 a 起点 1000）；y 就绪 500、需 600（截止 b 起点 2000）；
        # z 需 500（截止 c 起点 2500）。两种全选排列中 x,z,y 总完工 1500
        # （z 400-900，等 y 就绪后 900-1500）早于 x,y,z 的 1600
        # （空转到 500 才开始 y），故最早完工排列为 x,z,y。
        assets = [
            asset("x", "a", 0, 400, 1),
            asset("y", "b", 500, 600, 2),
            asset("z", "c", 0, 500, 3),
        ]
        result = plan_preload(self.commands, assets)
        self.assertEqual(
            result["loads"],
            [
                {"id": "x", "start_ms": 0, "end_ms": 400},
                {"id": "z", "start_ms": 400, "end_ms": 900},
                {"id": "y", "start_ms": 900, "end_ms": 1500},
            ],
        )
        self.assertEqual(result["skipped_ids"], [])

    def test_first_moment_wait_for_ready(self):
        # 加载器从 0 起空转，直到唯一素材就绪
        c = cmd("a", offset=100, duration=50)  # 起点 100
        result = plan_preload([c], [asset("x", "a", ready=50, load=50)])
        self.assertEqual(
            result["loads"], [{"id": "x", "start_ms": 50, "end_ms": 100}]
        )

    def test_finish_exactly_at_command_start_is_on_time(self):
        c = cmd("a", offset=100, duration=50)
        result = plan_preload([c], [asset("x", "a", ready=0, load=100)])
        self.assertEqual(
            result["loads"], [{"id": "x", "start_ms": 0, "end_ms": 100}]
        )

    def test_finish_one_past_deadline_skipped(self):
        c = cmd("a", offset=100, duration=50)
        result = plan_preload([c], [asset("x", "a", ready=0, load=101)])
        self.assertEqual(result["loads"], [])
        self.assertEqual(result["skipped_ids"], ["x"])

    def test_ready_after_deadline_skipped(self):
        c = cmd("a", offset=100, duration=50)
        result = plan_preload([c], [asset("x", "a", ready=101, load=1)])
        self.assertEqual(result["skipped_ids"], ["x"])

    def test_empty_assets(self):
        self.assertEqual(
            plan_preload(self.commands, []),
            {"loads": [], "skipped_ids": []},
        )

    def test_empty_commands_with_assets_is_target_not_found(self):
        with self.assertRaises(CompileError) as ctx:
            plan_preload([], [asset("x", "a")])
        self.assertEqual(ctx.exception.code, "PRELOAD_TARGET_NOT_FOUND")

    def test_skip_one_to_keep_more(self):
        # 四条口令起点：b=500、c=1000、d=1500、e=1500（d、e 不同通道，同起点）。
        # 短素材 y、z、w 各 500ms 首尾相接恰好全部按时（0-500、500-1000、
        # 1000-1500）；长素材 x 关联 e（截止 1500，需 1000ms），带上 x 至多还能
        # 再带 1 条短素材（共 2 条），故最优跳过 x、按时加载 3 条。
        commands = [
            cmd("b", channel="m", offset=500, duration=100),
            cmd("c", channel="m", offset=1000, duration=100),
            cmd("d", channel="m", offset=1500, duration=100),
            cmd("e", channel="s", offset=1500, duration=100),
        ]
        assets = [
            asset("x", "e", ready=0, load=1000, priority=0),  # 截止 1500
            asset("y", "b", ready=0, load=500, priority=0),   # 0-500，截止 500
            asset("z", "c", ready=0, load=500, priority=0),   # 500-1000，截止 1000
            asset("w", "d", ready=0, load=500, priority=0),   # 1000-1500，截止 1500
        ]
        result = plan_preload(commands, assets)
        self.assertEqual(
            [entry["id"] for entry in result["loads"]], ["y", "z", "w"]
        )
        self.assertEqual(result["skipped_ids"], ["x"])

    def test_priority_breaks_count_tie(self):
        # 两条口令起点同为 200，素材各需 200ms，串行加载器至多按时完成 1 条
        # （第二条 400 才完工，超过共同截止时刻 200）；条数并列（都是 1），
        # 选 priority 更大的 hi。
        commands = [
            cmd("a", channel="m", offset=200, duration=100),
            cmd("b", channel="s", offset=200, duration=100),
        ]
        assets = [
            asset("lo", "a", 0, 200, 1),
            asset("hi", "b", 0, 200, 9),
        ]
        result = plan_preload(commands, assets)
        self.assertEqual(
            result["loads"], [{"id": "hi", "start_ms": 0, "end_ms": 200}]
        )
        self.assertEqual(result["skipped_ids"], ["lo"])

    def test_chosen_id_list_lex_breaks_priority_tie(self):
        # 条数、优先级总和均相同：入选 id 升序列表 [a, z] 与 [b, c]，取前者。
        # 四条口令起点分别为 1000（a、c，不同通道）与 2000（b、d，不同通道）；
        # 素材与口令一一关联。预载不看通道，通道仅用于编排示例自洽。
        commands = [
            cmd("a", channel="m", offset=1000, duration=100),
            cmd("b", channel="m", offset=2000, duration=100),
            cmd("c", channel="s", offset=1000, duration=100),
            cmd("d", channel="s", offset=2000, duration=100),
        ]
        assets = [
            asset("a", "a", 0, 1000, 0),  # 0-1000，截止 1000
            asset("z", "b", 0, 1000, 0),  # 1000-2000，截止 2000
            asset("b", "c", 0, 500, 0),   # 0-500，截止 1000
            asset("c", "d", 0, 1500, 0),  # 500-2000，截止 2000
        ]
        result = plan_preload(commands, assets)
        self.assertEqual(
            [entry["id"] for entry in result["loads"]], ["a", "z"]
        )
        self.assertEqual(result["skipped_ids"], ["b", "c"])

    def test_earliest_finish_tiebreak_orders_by_id(self):
        # 同一入选集合两种顺序总完工相同时取加载 id 顺序字典序最小。
        # a、b 起点同为 1000（不同通道），素材各加载 100ms、均 0 时刻就绪。
        commands = [
            cmd("a", channel="m", offset=1000, duration=100),
            cmd("b", channel="s", offset=1000, duration=100),
        ]
        assets = [
            asset("p", "a", ready=0, load=100),
            asset("q", "b", ready=0, load=100),
        ]
        result = plan_preload(commands, assets)
        self.assertEqual(
            [entry["id"] for entry in result["loads"]], ["p", "q"]
        )

    def test_loads_returned_in_loading_order_not_id_order(self):
        # 就绪时刻迫使 id 较大的素材先加载：q 就绪 0、p 就绪 50，
        # q(0-300) 后 p 300-600 完工 600；p 先则 50-350、q 350-650 完工 650，
        # 取总完工更早的 q、p。
        commands = [
            cmd("a", channel="m", offset=1000, duration=100),
            cmd("b", channel="s", offset=1000, duration=100),
        ]
        assets = [
            asset("p", "a", ready=50, load=300),
            asset("q", "b", ready=0, load=300),
        ]
        result = plan_preload(commands, assets)
        self.assertEqual(
            result["loads"],
            [
                {"id": "q", "start_ms": 0, "end_ms": 300},
                {"id": "p", "start_ms": 300, "end_ms": 600},
            ],
        )


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.commands = [cmd("a", offset=1000, duration=100)]

    def test_compile_errors_propagate_first(self):
        with self.assertRaises(CompileError) as ctx:
            plan_preload([cmd("a", anchor="ghost", offset=100)], [asset("x", "a")])
        self.assertEqual(ctx.exception.code, "MISSING_ANCHOR")
        with self.assertRaises(CompileError) as ctx:
            plan_preload([cmd("a", offset=-10, duration=5)], [asset("x", "a")])
        self.assertEqual(ctx.exception.code, "NEGATIVE_START")

    def test_empty_asset_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_preload(self.commands, [asset("", "a")])
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_empty_command_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_preload(self.commands, [asset("x", "")])
        self.assertEqual(ctx.exception.code, "INVALID_ID")
        self.assertEqual(ctx.exception.details["ids"], ["x"])

    def test_duplicate_asset_id(self):
        with self.assertRaises(CompileError) as ctx:
            plan_preload(
                self.commands,
                [asset("x", "a"), asset("x", "a")],
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")

    def test_too_many_assets(self):
        commands = [cmd(f"c{i}", offset=1000 + 100 * i, duration=100) for i in range(13)]
        assets = [asset(f"a{i}", f"c{i}") for i in range(13)]
        with self.assertRaises(CompileError) as ctx:
            plan_preload(commands, assets)
        self.assertEqual(ctx.exception.code, "TOO_MANY_PRELOAD_ASSETS")
        self.assertEqual(ctx.exception.details["limit"], MAX_PRELOAD_ASSETS)

    def test_negative_ready(self):
        with self.assertRaises(CompileError) as ctx:
            plan_preload(self.commands, [asset("x", "a", ready=-1)])
        self.assertEqual(ctx.exception.code, "INVALID_READY_MS")

    def test_non_positive_load(self):
        for load in (0, -3):
            with self.assertRaises(CompileError) as ctx:
                plan_preload(self.commands, [asset("x", "a", load=load)])
            self.assertEqual(ctx.exception.code, "INVALID_DURATION")

    def test_negative_priority(self):
        with self.assertRaises(CompileError) as ctx:
            plan_preload(self.commands, [asset("x", "a", priority=-1)])
        self.assertEqual(ctx.exception.code, "INVALID_PRIORITY")

    def test_duplicate_command_link(self):
        commands = [cmd("a", offset=1000, duration=100),
                    cmd("b", offset=2000, duration=100)]
        assets = [
            asset("x", "a", load=1),
            asset("y", "a", load=1),
        ]
        with self.assertRaises(CompileError) as ctx:
            plan_preload(commands, assets)
        self.assertEqual(ctx.exception.code, "DUPLICATE_COMMAND_LINK")
        self.assertEqual(ctx.exception.details["command_ids"], ["a"])

    def test_unknown_command(self):
        with self.assertRaises(CompileError) as ctx:
            plan_preload(self.commands, [asset("x", "ghost")])
        self.assertEqual(ctx.exception.code, "PRELOAD_TARGET_NOT_FOUND")
        self.assertEqual(ctx.exception.details["command_ids"], ["ghost"])

    def test_inputs_not_mutated(self):
        import copy

        commands = [dict(c) for c in self.commands]
        assets = [dict(asset("x", "a", ready=5, load=10, priority=2))]
        cmd_snapshot = copy.deepcopy(commands)
        asset_snapshot = copy.deepcopy(assets)
        plan_preload(commands, assets)
        self.assertEqual(commands, cmd_snapshot)
        self.assertEqual(assets, asset_snapshot)


class BruteForceFuzzTests(unittest.TestCase):
    """随机输入下与「子集 × 全排列」暴力枚举逐案对照。"""

    def test_matches_brute_force(self):
        random.seed(20260926)
        for trial in range(2000):
            n_cmds = random.randint(0, 5)
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
                        random.randint(1, 150),
                    )
                )
            try:
                compile_commands([dict(c) for c in commands])
            except CompileError:
                continue

            n_assets = random.randint(0, min(5, n_cmds))
            chosen_commands = random.sample(ids, n_assets)
            assets = [
                asset(
                    f"a{i}",
                    cid,
                    ready=random.randint(0, 200),
                    load=random.randint(1, 150),
                    priority=random.randint(0, 5),
                )
                for i, cid in enumerate(chosen_commands)
            ]

            result = plan_preload(
                [dict(c) for c in commands], [dict(a) for a in assets]
            )
            expected = brute_force(commands, assets)
            self.assertEqual(result, expected, trial)


if __name__ == "__main__":
    unittest.main()
