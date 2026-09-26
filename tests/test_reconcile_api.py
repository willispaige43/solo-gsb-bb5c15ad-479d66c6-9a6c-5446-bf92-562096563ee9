"""POST /runs/reconcile 的端到端测试（真实 uvicorn 服务 + httpx 客户端）。

需要安装 fastapi / uvicorn / httpx（见 requirements.txt）；未安装时整文件跳过，
不影响纯标准库环境下运行其余单元测试。
"""

from __future__ import annotations

import os
import socket
import threading
import time
import unittest

try:
    import httpx
    import uvicorn

    from app.main import app
    from app.reconcile import reconcile_runs

    HAVE_SERVER = True
except ImportError:  # pragma: no cover - 纯标准库环境
    HAVE_SERVER = False


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


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Server:
    """在后台线程中运行真实 uvicorn 服务（含应用生命周期）。"""

    def __init__(self):
        self.port = _free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("uvicorn 启动超时")
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=30)


def _post_in_thread(url, payload, results, key):
    results[key] = httpx.post(url, json=payload, timeout=60)


@unittest.skipUnless(HAVE_SERVER, "需要安装 fastapi / uvicorn / httpx")
class ReconcileApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._server = _Server()
        cls.base = cls._server.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._server.__exit__()

    def post_reconcile(self, payload, timeout=60):
        return httpx.post(f"{self.base}/runs/reconcile", json=payload, timeout=timeout)

    def test_success_result_unchanged(self):
        resp = self.post_reconcile(SMALL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), reconcile_runs(**SMALL))

    def test_business_422_preserved(self):
        payload = make_payload(1)
        payload["records"] = [
            {
                "id": "r1",
                "channel": "main",
                "actual_start_ms": 900,
                "actual_end_ms": 500,
            }
        ]
        resp = self.post_reconcile(payload)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(
            resp.json(),
            {
                "code": "INVALID_INTERVAL",
                "message": "实播区间非法：actual_start_ms 不得为负，"
                "且 actual_end_ms 必须大于 actual_start_ms",
                "details": {"ids": ["r1"]},
            },
        )

    def test_validation_422_preserved(self):
        resp = self.post_reconcile({"commands": [], "records": []})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "INVALID_REQUEST")

    def test_busy_returns_503_without_submitting(self):
        results: dict[str, httpx.Response] = {}
        url = f"{self.base}/runs/reconcile"
        t1 = threading.Thread(
            target=_post_in_thread, args=(url, SLOW, results, "first")
        )
        t2 = threading.Thread(
            target=_post_in_thread, args=(url, SLOW, results, "second")
        )
        t1.start()
        time.sleep(0.3)  # 第一个请求已进入执行
        t2.start()
        time.sleep(0.3)  # 第二个请求已排队，两个名额占满
        resp = self.post_reconcile(SMALL)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["code"], "RECONCILE_BUSY")
        t1.join()
        t2.join()
        self.assertEqual(results["first"].status_code, 200)
        self.assertEqual(results["second"].status_code, 200)
        # 名额释放后立即可用
        self.assertEqual(self.post_reconcile(SMALL).status_code, 200)

    def test_health_responds_during_long_reconcile(self):
        results: dict[str, httpx.Response] = {}
        url = f"{self.base}/runs/reconcile"
        thread = threading.Thread(
            target=_post_in_thread, args=(url, SLOW, results, "slow")
        )
        thread.start()
        time.sleep(0.3)  # 慢对账已进入执行
        try:
            for _ in range(3):
                started = time.monotonic()
                resp = httpx.get(f"{self.base}/health", timeout=5)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"status": "ok"})
                self.assertLess(time.monotonic() - started, 1.0)
                time.sleep(0.2)
        finally:
            thread.join()
        self.assertEqual(results["slow"].status_code, 200)

    def test_timeout_returns_504_and_slot_released_after_actual_end(self):
        os.environ["RECONCILE_TIMEOUT_SECONDS"] = "0.3"
        try:
            started = time.monotonic()
            resp = self.post_reconcile(SLOW)
            self.assertEqual(resp.status_code, 504)
            self.assertEqual(resp.json()["code"], "RECONCILE_TIMEOUT")
            self.assertLess(time.monotonic() - started, 5)
        finally:
            del os.environ["RECONCILE_TIMEOUT_SECONDS"]
        # 超时任务仍在执行（名额保留）：再接纳 1 个后即满额
        results: dict[str, httpx.Response] = {}
        url = f"{self.base}/runs/reconcile"
        thread = threading.Thread(
            target=_post_in_thread, args=(url, SMALL, results, "queued")
        )
        thread.start()
        time.sleep(0.2)  # 排队任务已接纳
        self.assertEqual(self.post_reconcile(SMALL).status_code, 503)
        thread.join()
        # 执行中的任务实际结束后名额才释放：排队任务最终成功（迟到结果已丢弃）
        self.assertEqual(results["queued"].status_code, 200)
        self.assertEqual(self.post_reconcile(SMALL).status_code, 200)

    def test_queued_time_counts_toward_timeout(self):
        # 慢请求按默认超时（10s）接纳并进入执行
        results: dict[str, httpx.Response] = {}
        url = f"{self.base}/runs/reconcile"
        thread = threading.Thread(
            target=_post_in_thread, args=(url, SLOW, results, "slow")
        )
        thread.start()
        time.sleep(0.3)  # 慢请求已进入执行，下一个只能排队
        os.environ["RECONCILE_TIMEOUT_SECONDS"] = "0.4"
        try:
            resp = self.post_reconcile(SMALL)
            # 排队时间计入超时：排队 0.4s 后到期返回 504
            self.assertEqual(resp.status_code, 504)
            self.assertEqual(resp.json()["code"], "RECONCILE_TIMEOUT")
        finally:
            del os.environ["RECONCILE_TIMEOUT_SECONDS"]
        thread.join()
        # 排队任务已被撤销（不会执行）；慢请求不受影响，正常完成
        self.assertEqual(results["slow"].status_code, 200)
        self.assertEqual(self.post_reconcile(SMALL).status_code, 200)

    def test_client_disconnect_revokes_queued_and_abandons_running(self):
        url = f"{self.base}/runs/reconcile"
        # 慢请求进入执行（计算约 2 秒）
        results: dict[str, httpx.Response] = {}
        slow_thread = threading.Thread(
            target=_post_in_thread, args=(url, make_payload(600), results, "slow")
        )
        slow_thread.start()
        time.sleep(0.3)
        # 排队请求的客户端在排队期间断开（httpx 读超时即关闭连接）
        with self.assertRaises(httpx.ReadTimeout):
            httpx.post(url, json=SMALL, timeout=0.5)
        time.sleep(0.3)  # 服务端已感知断连并撤销排队任务
        # 排队任务的名额已释放：新请求被接纳（排队）；再一个则满额 503
        follow_up: dict[str, httpx.Response] = {}
        follow_thread = threading.Thread(
            target=_post_in_thread, args=(url, SMALL, follow_up, "follow")
        )
        follow_thread.start()
        time.sleep(0.3)
        self.assertEqual(self.post_reconcile(SMALL).status_code, 503)
        # 断连的执行中任务名额保留到实际结束：随后排队任务正常完成
        slow_thread.join()
        follow_thread.join()
        self.assertEqual(follow_up["follow"].status_code, 200)
        self.assertEqual(self.post_reconcile(SMALL).status_code, 200)


if __name__ == "__main__":
    unittest.main()
