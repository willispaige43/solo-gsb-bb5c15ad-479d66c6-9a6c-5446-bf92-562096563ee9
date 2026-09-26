"""``POST /runs/reconcile`` 的专属单工作进程执行器（仅依赖标准库）。

设计要点
--------
* 计算在**独立子进程**中执行（spawn 启动，随应用生命周期创建与回收），
  事件循环只负责排队、计时与收发结果；长对账期间 ``/health`` 等接口照常响应。
* 单工作进程**串行**执行任务；未结束任务（执行中 + 排队中）上限为
  ``MAX_INFLIGHT``，满额时 ``submit`` 返回 ``None``，由 HTTP 层转成 503，
  不提交计算。
* 每个被接纳的任务从**接纳时刻**起算超时（排队时间计入）。超时或客户端取消时：
  仍在排队的任务直接撤销并立即释放名额，永远不会被执行；已在执行的任务无法
  打断，名额保留到其**实际结束**才释放，迟到的结果直接丢弃。
* 所有状态迁移（接纳 / 派发 / 完成 / 超时 / 取消 / 释放）只发生在事件循环
  线程内，同一任务的名额只会释放一次；子进程只负责执行与回报，是否派发的
  决策权始终在父进程，因此排队任务被撤销后不存在「已被取走执行」的竞态。

进程间消息
----------
父 → 子（任务队列）：``("run", task_id, payload)`` / ``("stop",)``。
子 → 父（结果队列）：``("free",)`` 空闲信号、``("done", task_id, outcome)``
任务结束、``("down",)`` 退出前最后一条消息。
``outcome`` 为 ``("ok", result)`` / ``("error", code, message, details)`` /
``("crash", 摘要)``。
"""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
from collections import deque
from typing import Any

from .compiler import CompileError
from .reconcile import reconcile_runs

# 未结束任务（执行中 + 排队中）上限
MAX_INFLIGHT = 2


class ReconcileTimeout(Exception):
    """对账任务在时限（含排队时间）内未完成。"""


def _worker_main(task_queue: multiprocessing.Queue, result_queue: multiprocessing.Queue) -> None:
    """子进程入口：串行消费任务，执行前后各发一次空闲信号向父进程要任务。"""
    result_queue.put(("free",))
    while True:
        message = task_queue.get()
        if message[0] == "stop":
            break
        _, task_id, payload = message
        try:
            outcome = ("ok", reconcile_runs(**payload))
        except CompileError as exc:
            # 业务错误原样搬运 code / message / details，由父进程还原为 422
            outcome = ("error", exc.code, exc.message, exc.details)
        except BaseException as exc:  # 业务逻辑之外的异常不拖垮工作进程
            outcome = ("crash", f"{type(exc).__name__}: {exc}")
        result_queue.put(("done", task_id, outcome))
        result_queue.put(("free",))
    result_queue.put(("down",))


class ReconcileWorker:
    """应用生命周期管理的单工作进程执行器（详见模块 docstring）。"""

    MAX_INFLIGHT = MAX_INFLIGHT
    _START_TIMEOUT_SEC = 30.0
    _STOP_TIMEOUT_SEC = 10.0
    _PUMP_JOIN_TIMEOUT_SEC = 2.0
    _POLL_INTERVAL_SEC = 0.05

    def __init__(self) -> None:
        self._ctx = multiprocessing.get_context("spawn")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._process: multiprocessing.Process | None = None
        self._task_queue: multiprocessing.Queue | None = None
        self._result_queue: multiprocessing.Queue | None = None
        self._pump_thread: threading.Thread | None = None
        self._ready: asyncio.Event | None = None
        self._tasks: dict[int, dict[str, Any]] = {}
        self._queue: deque[int] = deque()
        self._next_task_id = 0
        self._worker_busy = False
        self._started = False

    # ------------------------------------------------------------------
    # 生命周期（随应用启动 / 关闭调用）
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动工作进程并等待其就绪（确认子进程已成功导入并贯通队列）。"""
        self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        self._task_queue = self._ctx.Queue()
        self._result_queue = self._ctx.Queue()
        self._process = self._ctx.Process(
            target=_worker_main,
            args=(self._task_queue, self._result_queue),
            name="reconcile-worker",
            daemon=True,
        )
        self._process.start()
        self._pump_thread = threading.Thread(
            target=self._pump_results, name="reconcile-result-pump", daemon=True
        )
        self._pump_thread.start()
        deadline = self._loop.time() + self._START_TIMEOUT_SEC
        while not self._ready.is_set():
            if not self._process.is_alive():
                raise RuntimeError(
                    f"对账工作进程启动即退出（exitcode={self._process.exitcode}）"
                )
            if self._loop.time() >= deadline:
                raise TimeoutError("等待对账工作进程就绪超时")
            await asyncio.sleep(self._POLL_INTERVAL_SEC)
        self._started = True

    async def stop(self) -> None:
        """回收工作进程：撤销排队任务，等执行中任务实际结束后让子进程退出。"""
        if not self._started:
            return
        self._started = False  # 不再接纳新任务、不再派发
        self._queue.clear()
        for record in self._tasks.values():
            record["state"] = "closed"
            record["timeout_handle"].cancel()
        process = self._process
        if process is not None:
            try:
                self._task_queue.put_nowait(("stop",))
            except Exception:
                pass
            # 子进程跑完当前任务后读到停止标记自行退出；逾期强制结束
            await asyncio.to_thread(process.join, self._STOP_TIMEOUT_SEC)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, self._STOP_TIMEOUT_SEC)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, self._STOP_TIMEOUT_SEC)
            process.close()
            self._process = None
        # 子进程退出前会发 ("down",)，泵线程读到后自行结束；异常退出时
        # 泵线程为 daemon，不阻碍进程收尾
        if self._pump_thread is not None:
            await asyncio.to_thread(self._pump_thread.join, self._PUMP_JOIN_TIMEOUT_SEC)
            self._pump_thread = None
        for queue in (self._task_queue, self._result_queue):
            if queue is not None:
                queue.close()
        self._task_queue = self._result_queue = None

    # ------------------------------------------------------------------
    # 任务接纳 / 取消（仅事件循环线程调用）
    # ------------------------------------------------------------------

    def submit(self, payload: dict[str, Any], timeout_sec: float) -> asyncio.Future | None:
        """接纳一个对账任务；满额（未结束任务达上限）时返回 ``None``。

        返回的 Future 以 ``("ok", result)`` / ``("error", code, message,
        details)`` / ``("crash", 摘要)`` 形式完成；超时以 ``ReconcileTimeout``
        异常完成。排队时间计入 ``timeout_sec``。
        """
        if not self._started or len(self._tasks) >= self.MAX_INFLIGHT:
            return None
        self._next_task_id += 1
        task_id = self._next_task_id
        future = self._loop.create_future()
        self._tasks[task_id] = {
            "payload": payload,
            "future": future,
            "state": "queued",
            "timeout_handle": self._loop.call_later(
                timeout_sec, self._on_timeout, task_id
            ),
        }
        self._queue.append(task_id)
        self._dispatch()
        return future

    def cancel(self, future: asyncio.Future) -> None:
        """客户端断开等导致的取消：与超时相同的撤销 / 弃置语义。"""
        for task_id, record in self._tasks.items():
            if record["future"] is future:
                self._release_or_abandon(task_id)
                return

    # ------------------------------------------------------------------
    # 内部状态迁移（仅事件循环线程执行）
    # ------------------------------------------------------------------

    def _dispatch(self) -> None:
        """工作进程空闲且队列非空时，把队首任务派发给子进程。"""
        if not self._started or self._worker_busy or not self._queue:
            return
        task_id = self._queue.popleft()
        record = self._tasks[task_id]
        record["state"] = "running"
        self._worker_busy = True
        try:
            self._task_queue.put_nowait(("run", task_id, record["payload"]))
        except Exception:
            # 队列写入失败（如管道已关闭）：任务退回队首，等待下次派发
            record["state"] = "queued"
            self._worker_busy = False
            self._queue.appendleft(task_id)

    def _release_or_abandon(self, task_id: int) -> None:
        """超时 / 取消共用：排队任务撤销并立即释放名额；执行中任务弃置、保留名额。"""
        record = self._tasks.get(task_id)
        if record is None:
            return
        record["timeout_handle"].cancel()
        if record["state"] == "queued":
            # 任务尚未派发（父进程持有唯一派发权），直接撤销，永远不会被执行
            self._tasks.pop(task_id)
            try:
                self._queue.remove(task_id)
            except ValueError:
                pass
            record["state"] = "expired"
        elif record["state"] == "running":
            # 无法打断子进程内的计算：名额保留到 ("done", ...) 到达才释放
            record["state"] = "abandoned"

    def _on_timeout(self, task_id: int) -> None:
        """任务到时（自接纳起算，含排队时间）。"""
        record = self._tasks.get(task_id)
        if record is None:
            return
        self._release_or_abandon(task_id)
        future = record["future"]
        if not future.done():
            future.set_exception(ReconcileTimeout())

    def _on_done(self, task_id: int, outcome: tuple) -> None:
        """任务实际结束：释放名额；已超时 / 取消的任务结果直接丢弃。"""
        record = self._tasks.pop(task_id, None)
        if record is None:
            return  # 重复或未知完成通知：名额只释放一次
        record["timeout_handle"].cancel()
        if record["state"] == "running":
            future = record["future"]
            if not future.done():
                future.set_result(outcome)
        # 其余状态（abandoned / closed）：迟到结果丢弃

    def _on_message(self, message: tuple) -> None:
        """处理子进程消息（由泵线程经 call_soon_threadsafe 转发到事件循环）。"""
        kind = message[0]
        if kind == "free":
            if not self._started and self._ready is not None:
                self._ready.set()  # 首次空闲信号 = 子进程已就绪
                return
            self._worker_busy = False
            self._dispatch()
        elif kind == "done":
            _, task_id, outcome = message
            self._on_done(task_id, outcome)
        # ("down",)：子进程已按序退出，无需处理

    # ------------------------------------------------------------------
    # 泵线程：阻塞读结果队列，不占用事件循环
    # ------------------------------------------------------------------

    def _pump_results(self) -> None:
        """把结果队列的消息逐条转发到事件循环线程处理。"""
        while True:
            try:
                message = self._result_queue.get()
            except (EOFError, OSError):
                break
            loop = self._loop
            if loop is None:
                break
            try:
                loop.call_soon_threadsafe(self._on_message, message)
            except RuntimeError:  # 事件循环已关闭
                break
            if message[0] == "down":
                break
