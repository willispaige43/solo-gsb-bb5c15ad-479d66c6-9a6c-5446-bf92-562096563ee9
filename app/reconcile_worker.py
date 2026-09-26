"""POST /runs/reconcile 的单工作进程调度（随应用生命周期管理）。

设计要点
--------
* 应用启动时创建 ``max_workers=1`` 的 ``ProcessPoolExecutor``：对账计算全部
  移入唯一的工作进程执行，事件循环只负责等待结果，长对账期间 ``/health``
  等接口照常响应；应用关闭时随 lifespan 回收工作进程。
* 同时未结束的任务（**执行中 + 排队**）最多 2 个：满额时新请求立即失败
  （503 / ``RECONCILE_BUSY``），不提交计算。
* 任务被接纳即按 ``RECONCILE_TIMEOUT_SECONDS``（默认 10 秒）计时，排队时间
  也计入；到期按超时处理（504 / ``RECONCILE_TIMEOUT``）。
* 排队任务保存在应用层队列中，**尚未提交工作进程**：超时或请求取消时直接
  撤销并立即释放名额；已提交工作进程的任务无法中断，等它实际结束才释放
  名额，迟到的结果直接丢弃。
* 所有状态变更都发生在事件循环线程内（工作进程的完成回调经
  ``loop.call_soon_threadsafe`` 弹回），每个名额恰好释放一次，
  并发完成与取消不会重复释放。
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any

from .reconcile import reconcile_runs

#: 未结束任务（执行中 + 排队）的名额上限
MAX_INFLIGHT = 2

#: RECONCILE_TIMEOUT_SECONDS 缺省值（秒）
DEFAULT_TIMEOUT_SECONDS = 10.0


def reconcile_timeout_seconds() -> float:
    """读取 ``RECONCILE_TIMEOUT_SECONDS``；缺省、无法解析或非正数时回退到默认值。"""
    raw = os.environ.get("RECONCILE_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def _run_reconcile(payload: dict[str, Any]) -> dict[str, Any]:
    """工作进程入口：展开参数并执行对账（模块级函数，可跨进程 pickle）。"""
    return reconcile_runs(**payload)


def _warmup() -> None:
    """空操作：仅用于让执行器提前派生工作进程。"""
    return None


class _QueuedJob:
    """已接纳但尚未提交工作进程的排队任务。"""

    __slots__ = ("payload", "promoted")

    def __init__(self, payload: dict[str, Any], promoted: asyncio.Future) -> None:
        self.payload = payload
        # 被提升为执行中时，由完成回调把 concurrent Future 放进该 asyncio Future
        self.promoted = promoted


class ReconcileWorker:
    """单工作进程 + 「1 执行 + 1 排队」名额的对账执行器。

    除标注线程安全的方法外，所有方法都必须在持有该执行器的事件循环线程内
    调用；工作进程任务的完成回调统一弹回事件循环，保证状态单线程变更。
    """

    def __init__(self) -> None:
        self._executor = ProcessPoolExecutor(max_workers=1)
        self._running: Future | None = None
        self._queued: _QueuedJob | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def inflight(self) -> int:
        """当前未结束任务数（执行中 + 排队），主要用于测试与观测。"""
        return (self._running is not None) + (self._queued is not None)

    async def run(
        self, payload: dict[str, Any], timeout: float
    ) -> tuple[str, dict[str, Any] | None]:
        """占用一个名额执行对账，返回 ``("ok" | "busy" | "timeout", 结果)``。

        * 计算在工作进程内执行，等待不阻塞事件循环；
        * 业务异常（``CompileError`` 及其子类）随 future 结果原样回传，
          继续向上抛，由 HTTP 层转换为 422；
        * 请求被取消时 ``CancelledError`` 照常向上传播；
        * 无论成功、超时、取消还是业务异常，名额都恰好释放一次。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        kind, token = self._admit(payload, loop)
        if kind == "busy":
            return "busy", None

        if kind == "queued":
            job = token
            try:
                # shield：超时/取消只解除等待，不取消 promoted 本身，
                # 以便提升回调仍能安全地 set_result
                future = await asyncio.wait_for(
                    asyncio.shield(job.promoted),
                    max(0.0, deadline - loop.time()),
                )
            except asyncio.TimeoutError:
                self._revoke_queued(job)
                return "timeout", None
            except asyncio.CancelledError:
                self._revoke_queued(job)
                raise
        else:
            future = token

        try:
            result = await asyncio.wait_for(
                asyncio.wrap_future(future), max(0.0, deadline - loop.time())
            )
            return "ok", result
        except asyncio.TimeoutError:
            # 已提交工作进程的任务无法中断：名额由完成回调在实际结束时释放，
            # 迟到的结果无人读取，直接丢弃
            return "timeout", None

    def shutdown(self) -> None:
        """回收工作进程：撤销排队任务，等待执行中的任务结束。

        会阻塞调用线程，需经 ``asyncio.to_thread`` 调用以免阻塞事件循环。
        """
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    def warmup(self) -> None:
        """提前派生工作进程，避免首个对账请求承担 fork 开销。

        提交一个空操作并等待完成；会阻塞调用线程，需放到线程中执行。
        """
        self._executor.submit(_warmup).result()

    # ------------------------------------------------------------------
    # 以下方法仅在事件循环线程内执行（完成回调已弹回事件循环）
    # ------------------------------------------------------------------

    def _admit(
        self, payload: dict[str, Any], loop: asyncio.AbstractEventLoop
    ) -> tuple[str, Any]:
        """尝试接纳一个任务：直接执行 / 排队 / 满额拒绝（不提交计算）。"""
        self._loop = loop
        if self._closed:
            return "busy", None
        if self._running is None:
            return "running", self._submit(payload)
        if self._queued is None:
            job = _QueuedJob(payload, loop.create_future())
            self._queued = job
            return "queued", job
        return "busy", None

    def _submit(self, payload: dict[str, Any]) -> Future:
        """把对账计算提交给唯一的工作进程，并挂完成回调。"""
        future = self._executor.submit(_run_reconcile, payload)
        self._running = future
        future.add_done_callback(self._bounce_done)
        return future

    def _revoke_queued(self, job: _QueuedJob) -> None:
        """撤销仍在排队的任务并立即释放名额；已提升的无需动作。

        已提升的任务占用的是执行名额，由它自己的完成回调负责释放，
        因此这里与提升回调互斥，不会重复释放。
        """
        if self._queued is job:
            self._queued = None
            job.promoted.cancel()

    def _bounce_done(self, future: Future) -> None:
        """工作进程任务完成回调（执行器线程）：弹回事件循环统一处理。"""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._on_future_done, future)
        except RuntimeError:
            # 事件循环已关闭（应用退出中），名额随进程回收
            pass

    def _on_future_done(self, future: Future) -> None:
        """执行名额释放的唯一入口：任务实际结束才到这里，并提升排队任务。"""
        if self._running is not future:
            return
        self._running = None
        job, self._queued = self._queued, None
        if job is None:
            return
        if self._closed:
            job.promoted.cancel()
            return
        # 提升排队任务：排队槽位移交给执行槽位，名额总数不变
        promoted = self._submit(job.payload)
        job.promoted.set_result(promoted)


_worker: ReconcileWorker | None = None


def get_worker() -> ReconcileWorker:
    """取当前对账执行器；不存在或已关闭时（重新）创建。"""
    global _worker
    if _worker is None or _worker.closed:
        _worker = ReconcileWorker()
    return _worker


def startup_worker() -> ReconcileWorker:
    """应用启动：创建并返回执行器（工作进程由 warmup 提前派生）。"""
    return get_worker()


def shutdown_worker() -> None:
    """应用关闭：回收工作进程；阻塞调用线程，需放到线程中执行。"""
    global _worker
    worker, _worker = _worker, None
    if worker is not None:
        worker.shutdown()
