"""app.reconcile_worker 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。

覆盖：成功结果与业务错误的搬运、满额拒收、排队 / 执行中超时的名额释放语义、
取消语义、迟到结果丢弃、事件循环不被计算阻塞、随生命周期回收。
"""

from __future__ import annotations

import asyncio
import time
import unittest

from app.reconcile import reconcile_runs
from app.reconcile_worker import ReconcileTimeout, ReconcileWorker


def make_payload(n, tolerance=2):
    """构造 n 计划 × n 实播的单通道对账负载（n≈450 时计算约 1 秒）。"""
    return {
        "commands": [
            {
                "id": f"c{i}",
                "channel": "main",
                "anchor": "START",
                "offset_ms": i * 10,
                "duration_ms": 5,
            }
            for i in range(n)
        ],
        "records": [
            {
                "id": f"r{i}",
                "channel": "main",
                "actual_start_ms": i * 10,
                "actual_end_ms": i * 10 + 5,
            }
            for i in range(n)
        ],
        "tolerance_ms": tolerance,
        "miss_penalty": 100,
        "extra_penalty": 100,
    }


SMALL = make_payload(2)
SLOW = make_payload(450)
LONG_TIMEOUT = 60.0


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = ReconcileWorker()
        await self.worker.start()

    async def asyncTearDown(self):
        await self.worker.stop()

    async def wait_done(self, future):
        return await asyncio.wait_for(future, timeout=30)

    async def submit_when_slot_available(self, payload=SMALL, tries=600):
        """轮询直至出现空闲名额（用于等待被弃置任务实际结束）。"""
        for _ in range(tries):
            future = self.worker.submit(payload, LONG_TIMEOUT)
            if future is not None:
                return future
            await asyncio.sleep(0.05)
        self.fail("等待名额释放超时")

    async def test_success_result_matches_direct_call(self):
        future = self.worker.submit(SMALL, LONG_TIMEOUT)
        self.assertIsNotNone(future)
        outcome = await self.wait_done(future)
        self.assertEqual(outcome[0], "ok")
        self.assertEqual(outcome[1], reconcile_runs(**SMALL))

    async def test_business_error_carried_back(self):
        payload = make_payload(1)
        payload["records"] = [
            {
                "id": "r1",
                "channel": "main",
                "actual_start_ms": 500,
                "actual_end_ms": 400,
            }
        ]
        future = self.worker.submit(payload, LONG_TIMEOUT)
        outcome = await self.wait_done(future)
        self.assertEqual(outcome[0], "error")
        self.assertEqual(outcome[1], "INVALID_INTERVAL")
        self.assertEqual(outcome[3], {"ids": ["r1"]})

    async def test_busy_limit_rejects_third_task(self):
        f1 = self.worker.submit(SLOW, LONG_TIMEOUT)  # 执行中
        f2 = self.worker.submit(SMALL, LONG_TIMEOUT)  # 排队
        self.assertIsNotNone(f1)
        self.assertIsNotNone(f2)
        # 满额：立即拒收，不提交计算
        self.assertIsNone(self.worker.submit(SMALL, LONG_TIMEOUT))
        self.assertEqual((await self.wait_done(f1))[0], "ok")
        self.assertEqual((await self.wait_done(f2))[0], "ok")
        # 名额释放后可再接纳
        f3 = self.worker.submit(SMALL, LONG_TIMEOUT)
        self.assertIsNotNone(f3)
        self.assertEqual((await self.wait_done(f3))[0], "ok")

    async def test_queued_timeout_releases_slot_immediately(self):
        f1 = self.worker.submit(SLOW, LONG_TIMEOUT)  # 执行中（慢）
        f2 = self.worker.submit(SMALL, timeout_sec=0.2)  # 排队，很快到时
        with self.assertRaises(ReconcileTimeout):
            await self.wait_done(f2)
        # 排队任务撤销后名额立即释放：慢任务仍在执行也能再接纳一个
        f3 = self.worker.submit(SMALL, LONG_TIMEOUT)
        self.assertIsNotNone(f3)
        self.assertEqual((await self.wait_done(f1))[0], "ok")
        self.assertEqual((await self.wait_done(f3))[0], "ok")

    async def test_running_timeout_holds_slot_until_actual_end(self):
        f1 = self.worker.submit(SLOW, timeout_sec=0.2)  # 执行中超时
        with self.assertRaises(ReconcileTimeout):
            await self.wait_done(f1)
        # 名额保留到实际结束：此时只能再接纳 1 个
        f2 = self.worker.submit(SMALL, LONG_TIMEOUT)
        self.assertIsNotNone(f2)
        self.assertIsNone(self.worker.submit(SMALL, LONG_TIMEOUT))
        # 慢任务实际结束后名额释放（迟到结果已被丢弃，不报错）
        f3 = await self.submit_when_slot_available()
        self.assertEqual((await self.wait_done(f2))[0], "ok")
        self.assertEqual((await self.wait_done(f3))[0], "ok")

    async def test_cancel_queued_releases_slot_immediately(self):
        f1 = self.worker.submit(SLOW, LONG_TIMEOUT)
        f2 = self.worker.submit(SMALL, LONG_TIMEOUT)
        f2.cancel()
        self.worker.cancel(f2)
        # 排队任务撤销、名额立即释放
        f3 = self.worker.submit(SMALL, LONG_TIMEOUT)
        self.assertIsNotNone(f3)
        self.assertIsNone(self.worker.submit(SMALL, LONG_TIMEOUT))
        self.assertEqual((await self.wait_done(f1))[0], "ok")
        self.assertEqual((await self.wait_done(f3))[0], "ok")

    async def test_cancel_running_abandons_and_discards_late_result(self):
        f1 = self.worker.submit(SLOW, LONG_TIMEOUT)
        await asyncio.sleep(0.1)  # 确保已进入执行
        f1.cancel()
        self.worker.cancel(f1)
        # 执行中任务弃置：名额保留，只能再接纳 1 个
        f2 = self.worker.submit(SMALL, LONG_TIMEOUT)
        self.assertIsNotNone(f2)
        self.assertIsNone(self.worker.submit(SMALL, LONG_TIMEOUT))
        # 实际结束后名额释放、迟到结果丢弃不报错
        f3 = await self.submit_when_slot_available()
        self.assertEqual((await self.wait_done(f2))[0], "ok")
        self.assertEqual((await self.wait_done(f3))[0], "ok")

    async def test_event_loop_not_blocked_during_compute(self):
        future = self.worker.submit(SLOW, LONG_TIMEOUT)
        ticks = 0
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline and not future.done():
            await asyncio.sleep(0.02)
            ticks += 1
        # 计算期间事件循环持续响应（若被阻塞，ticks 会远小于该值）
        self.assertGreaterEqual(ticks, 10)
        self.assertEqual((await self.wait_done(future))[0], "ok")

    async def test_stop_waits_for_running_task(self):
        self.worker.submit(SLOW, LONG_TIMEOUT)
        started = time.monotonic()
        await self.worker.stop()
        elapsed = time.monotonic() - started
        # 等执行中任务实际结束才回收（计算约 1 秒）
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertIsNone(self.worker._process)

    async def test_stop_without_tasks(self):
        await self.worker.stop()
        self.assertIsNone(self.worker._process)
        # 重复停止安全
        await self.worker.stop()


if __name__ == "__main__":
    unittest.main()
