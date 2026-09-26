"""POST /runs/reconcile 工作进程调度（单工作进程 + 名额 + 超时）的 HTTP 级测试。

通过 ``unittest.mock.patch`` 替换 ``app.reconcile_worker.reconcile_runs`` 注入
慢速对账函数：工作进程在首次提交时才从主进程 fork，子进程会继承补丁。
运行方式：``python -m unittest discover -s tests``。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from fastapi.testclient import TestClient

import app.reconcile_worker as worker_mod
from app.main import app

MARKER = os.path.join(
    tempfile.gettempdir(), f"reconcile_worker_test_{os.getpid()}.log"
)
SLOW_SECONDS = 2.0


def slow_reconcile(commands, records, tolerance_ms, miss_penalty, extra_penalty):
    """测试用慢速对账：在标记文件记录一次调用后睡眠，返回固定的合法结果。"""
    with open(MARKER, "a", encoding="utf-8") as fh:
        fh.write("x\n")
    time.sleep(SLOW_SECONDS)
    return {
        "matches": [],
        "missed_plan_ids": [],
        "extra_actual_ids": [],
        "total_penalty": 0,
    }


def payload():
    return {
        "commands": [{"id": "a", "channel": "main", "duration_ms": 1000}],
        "records": [
            {
                "id": "r1",
                "channel": "main",
                "actual_start_ms": 0,
                "actual_end_ms": 1000,
            }
        ],
        "tolerance_ms": 100,
        "miss_penalty": 1000,
        "extra_penalty": 1000,
    }


def payload_kwargs():
    """直接驱动 ReconcileWorker.run 时使用的关键字参数形式。"""
    body = payload()
    return {
        "commands": body["commands"],
        "records": body["records"],
        "tolerance_ms": body["tolerance_ms"],
        "miss_penalty": body["miss_penalty"],
        "extra_penalty": body["extra_penalty"],
    }


def marker_count() -> int:
    if not os.path.exists(MARKER):
        return 0
    with open(MARKER, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


class SlowReconcileTests(unittest.TestCase):
    """注入慢速对账函数，验证名额、超时与事件循环不被阻塞。"""

    def setUp(self):
        if os.path.exists(MARKER):
            os.remove(MARKER)
        self._old_timeout = os.environ.get("RECONCILE_TIMEOUT_SECONDS")
        self.addCleanup(self._restore_timeout)
        patcher = mock.patch.object(worker_mod, "reconcile_runs", slow_reconcile)
        self.addCleanup(patcher.stop)
        patcher.start()
        # 每个用例独立的客户端上下文：lifespan 会新建执行器，
        # 工作进程在首次提交时 fork，从而继承上面的补丁
        self._client_ctx = TestClient(app)
        self.client = self._client_ctx.__enter__()
        self.addCleanup(self._client_ctx.__exit__, None, None, None)

    def _restore_timeout(self):
        if self._old_timeout is None:
            os.environ.pop("RECONCILE_TIMEOUT_SECONDS", None)
        else:
            os.environ["RECONCILE_TIMEOUT_SECONDS"] = self._old_timeout

    def set_timeout(self, seconds):
        os.environ["RECONCILE_TIMEOUT_SECONDS"] = str(seconds)

    def test_health_responsive_during_long_reconcile(self):
        self.set_timeout(30)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.client.post, "/runs/reconcile", json=payload())
            time.sleep(0.5)  # 等对账进入工作进程执行
            start = time.monotonic()
            health = self.client.get("/health")
            elapsed = time.monotonic() - start
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json(), {"status": "ok"})
            self.assertLess(elapsed, 1.0)  # 长对账期间健康检查不被阻塞
            response = pending.result(timeout=15)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_penalty"], 0)
        self.assertEqual(marker_count(), 1)

    def test_busy_returns_503_without_submitting(self):
        self.set_timeout(30)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.client.post, "/runs/reconcile", json=payload())
            second = pool.submit(self.client.post, "/runs/reconcile", json=payload())
            time.sleep(0.5)  # 1 执行 + 1 排队，两个名额占满
            start = time.monotonic()
            third = self.client.post("/runs/reconcile", json=payload())
            elapsed = time.monotonic() - start
            self.assertEqual(third.status_code, 503)
            self.assertEqual(third.json()["code"], "RECONCILE_BUSY")
            self.assertLess(elapsed, 1.0)  # 满额立即拒绝
            self.assertEqual(first.result(timeout=15).status_code, 200)
            self.assertEqual(second.result(timeout=15).status_code, 200)
        # 第三个请求未提交计算：慢速函数只被调用了 2 次
        self.assertEqual(marker_count(), 2)
        self.assertEqual(worker_mod.get_worker().inflight, 0)

    def test_timeout_504_and_slot_released_only_after_actual_end(self):
        self.set_timeout(1)
        start = time.monotonic()
        response = self.client.post("/runs/reconcile", json=payload())
        elapsed = time.monotonic() - start
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["code"], "RECONCILE_TIMEOUT")
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, SLOW_SECONDS)  # 到期即返回，不等计算结束
        # 任务已提交工作进程，须等实际结束才释放名额：此刻仍占 1 个
        self.assertEqual(worker_mod.get_worker().inflight, 1)
        # 等慢速任务实际结束（迟到结果丢弃），名额随后释放
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and worker_mod.get_worker().inflight:
            time.sleep(0.05)
        self.assertEqual(worker_mod.get_worker().inflight, 0)
        self.assertEqual(marker_count(), 1)

    def test_queued_task_revoked_on_timeout_and_slot_freed_immediately(self):
        self.set_timeout(30)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.client.post, "/runs/reconcile", json=payload())
            time.sleep(0.5)  # 第一个进入执行
            self.set_timeout(1)  # 第二个任务排队 1 秒即超时
            start = time.monotonic()
            queued = self.client.post("/runs/reconcile", json=payload())
            elapsed = time.monotonic() - start
            self.assertEqual(queued.status_code, 504)
            self.assertEqual(queued.json()["code"], "RECONCILE_TIMEOUT")
            self.assertLess(elapsed, SLOW_SECONDS)
            # 排队任务被撤销并立即释放名额：只剩执行中的第一个
            self.assertEqual(worker_mod.get_worker().inflight, 1)
            self.set_timeout(30)
            self.assertEqual(first.result(timeout=15).status_code, 200)
        # 被撤销的排队任务从未提交工作进程：慢速函数只被调用 1 次
        self.assertEqual(marker_count(), 1)
        self.assertEqual(worker_mod.get_worker().inflight, 0)

    def test_queued_task_is_promoted_after_running_finishes(self):
        self.set_timeout(30)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.client.post, "/runs/reconcile", json=payload())
            time.sleep(0.5)
            start = time.monotonic()
            second = pool.submit(self.client.post, "/runs/reconcile", json=payload())
            # 第一个结束后第二个被提升执行，两者都成功
            self.assertEqual(first.result(timeout=15).status_code, 200)
            response = second.result(timeout=15)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total_penalty"], 0)
            self.assertGreaterEqual(time.monotonic() - start, SLOW_SECONDS)
        self.assertEqual(marker_count(), 2)
        self.assertEqual(worker_mod.get_worker().inflight, 0)


class WorkerCancellationTests(unittest.IsolatedAsyncioTestCase):
    """直接驱动执行器：请求取消时，排队任务撤销并立即释放名额，
    执行中任务的等待被取消后名额等实际结束才释放。"""

    async def test_cancel_queued_and_running_waits(self):
        if os.path.exists(MARKER):
            os.remove(MARKER)
        with mock.patch.object(worker_mod, "reconcile_runs", slow_reconcile):
            worker = worker_mod.ReconcileWorker()
            try:
                first = asyncio.create_task(worker.run(payload_kwargs(), 30))
                await asyncio.sleep(0.5)
                self.assertEqual(worker.inflight, 1)

                queued = asyncio.create_task(worker.run(payload_kwargs(), 30))
                await asyncio.sleep(0.2)
                self.assertEqual(worker.inflight, 2)

                # 取消排队任务的等待：任务被撤销，名额立即释放
                queued.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await queued
                self.assertEqual(worker.inflight, 1)

                # 取消执行中任务的等待：任务无法中断，名额等实际结束才释放
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertEqual(worker.inflight, 1)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and worker.inflight:
                    await asyncio.sleep(0.05)
                self.assertEqual(worker.inflight, 0)
            finally:
                await asyncio.get_running_loop().run_in_executor(None, worker.shutdown)
        # 排队任务被撤销未执行，执行中任务跑完：慢速函数只被调用 1 次
        self.assertEqual(marker_count(), 1)


class RealReconcileTests(unittest.TestCase):
    """不注入补丁：真实计算经工作进程往返，结果与 422 内容保持不变。"""

    def setUp(self):
        self._client_ctx = TestClient(app)
        self.client = self._client_ctx.__enter__()
        self.addCleanup(self._client_ctx.__exit__, None, None, None)

    def test_success_result_unchanged(self):
        response = self.client.post(
            "/runs/reconcile",
            json={
                "commands": [
                    {"id": "a", "channel": "main", "offset_ms": 0, "duration_ms": 1000},
                    {"id": "b", "channel": "main", "anchor": "a", "offset_ms": 0, "duration_ms": 500},
                    {"id": "c", "channel": "sub", "offset_ms": 0, "duration_ms": 800},
                ],
                "records": [
                    {"id": "r1", "channel": "main", "actual_start_ms": 10, "actual_end_ms": 1020},
                    {"id": "r2", "channel": "main", "actual_start_ms": 1010, "actual_end_ms": 1490},
                    {"id": "r3", "channel": "sub", "actual_start_ms": 0, "actual_end_ms": 900},
                ],
                "tolerance_ms": 50,
                "miss_penalty": 200,
                "extra_penalty": 300,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_penalty"], 550)
        self.assertEqual(body["missed_plan_ids"], ["c"])
        self.assertEqual(body["extra_actual_ids"], ["r3"])
        self.assertEqual(
            [(m["plan_id"], m["actual_id"]) for m in body["matches"]],
            [("a", "r1"), ("b", "r2")],
        )

    def test_business_422_preserved(self):
        response = self.client.post(
            "/runs/reconcile",
            json={
                "commands": [],
                "records": [
                    {"id": "r1", "channel": "main", "actual_start_ms": 900, "actual_end_ms": 500}
                ],
                "tolerance_ms": 50,
                "miss_penalty": 200,
                "extra_penalty": 300,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json(),
            {
                "code": "INVALID_INTERVAL",
                "message": "实播区间非法：actual_start_ms 不得为负，"
                "且 actual_end_ms 必须大于 actual_start_ms",
                "details": {"ids": ["r1"]},
            },
        )

    def test_plan_side_422_preserved(self):
        response = self.client.post(
            "/runs/reconcile",
            json={
                "commands": [
                    {"id": "a", "channel": "main", "anchor": "b", "duration_ms": 100},
                    {"id": "b", "channel": "main", "anchor": "a", "duration_ms": 100},
                ],
                "records": [],
                "tolerance_ms": 0,
                "miss_penalty": 1,
                "extra_penalty": 1,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "DEPENDENCY_CYCLE")


if __name__ == "__main__":
    unittest.main()
