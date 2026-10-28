"""Trading-owned background loop; HTTP threads only read copied health snapshots."""

import asyncio
from concurrent.futures import CancelledError as FutureCancelled, TimeoutError as FutureTimeout
from copy import deepcopy
import threading

from .ibkr import IBKRAdapter, IBKROperationError, _timeout


class IBKRBrokerLifecycle:
    """Own one fresh adapter and one explicit paper-only connection attempt.

    Start does not wait for Gateway or prevent chat startup. The event loop
    remains running after connect completes so SDK network/events stay alive.
    A lifecycle is single-use; restart the Host to apply a different profile.
    """

    def __init__(self, broker: IBKRAdapter, *, poll_interval=0.1):
        if not isinstance(broker, IBKRAdapter):
            raise TypeError("broker must be an IBKRAdapter")
        if not 0 < poll_interval <= 1:
            raise ValueError("poll_interval must be greater than 0 and at most 1")
        self._broker = broker
        self._poll_interval = poll_interval
        self._guard = threading.Lock()
        self._snapshot = broker.health()
        self._status = "not_started"
        self._error = None
        self._thread = None
        self._loop = None
        self._stop = None
        self._closing = threading.Event()
        self._paper_snapshot = broker.paper_order_snapshot()
        self._orders_snapshot = []
        self._events_snapshot = []
        self._requests = set()

    def health(self):
        with self._guard:
            health = deepcopy(self._snapshot)
            health["lifecycle"] = {
                "state": self._status,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "error": deepcopy(self._error),
            }
            health["health_stale"] = self._status == "failed"
            return health

    def _publish(self, *, status=None):
        # This is called only on the SDK-owning event-loop thread.
        health = self._broker.health()
        paper = self._broker.paper_order_snapshot()
        orders, events = self._broker.orders_snapshot(), self._broker.events_snapshot()
        with self._guard:
            self._snapshot = health
            self._paper_snapshot, self._orders_snapshot, self._events_snapshot = paper, orders, events
            if status is not None:
                self._status = status

    def _failure(self, code, message):
        # Exception strings may contain account data; keep diagnostics fixed.
        with self._guard:
            self._status = "failed"
            self._error = {"code": code, "message": message}

    def start(self):
        with self._guard:
            if self._closing.is_set() or self._status != "not_started":
                return False
            config = self._broker.config
            if config.environment != "paper":
                self._status = "inactive"
                self._snapshot["state"] = "automatic_connection_blocked"
                self._snapshot["blockers"].append({
                    "code": "paper_only_auto_connect", "message": "Host 自动连接仅开放显式 paper 环境，live 不发起连接。"})
                return False
            if config.account is None:
                self._status = "inactive"
                return False
            self._status = "starting"
            self._thread = threading.Thread(target=self._thread_main, name="ibkr-paper", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._status = "failed"
                self._error = {"code": "broker_thread_failed", "message": "IBKR 后台线程未能启动；聊天仍可使用。"}
                return False
        return True

    async def _connect_once(self):
        try:
            await self._broker.connect()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failure("broker_connect_failed", "IBKR 连接任务失败，请检查 Gateway；聊天仍可使用。")
        finally:
            self._publish()

    async def _run(self):
        loop, stop = asyncio.get_running_loop(), asyncio.Event()
        with self._guard:
            self._loop, self._stop = loop, stop
        connection = None
        try:
            if not self._closing.is_set():
                self._publish(status="running")
                connection = asyncio.create_task(self._connect_once(), name="ibkr-initial-connect")
            while not self._closing.is_set() and not stop.is_set():
                self._publish()
                try:
                    await asyncio.wait_for(stop.wait(), self._poll_interval)
                except TimeoutError:
                    pass
        finally:
            self._publish(status="stopping")
            if connection is not None:
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)
            requests = tuple(self._requests)
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            await self._broker.disconnect()
            self._publish()
            if self._broker.health()["connected"]:
                self._failure("broker_disconnect_failed", "IBKR 连接关闭无法确认，请检查 Gateway 和 Host 进程。")
            else:
                with self._guard:
                    self._status = "stopped"

    def paper_order_snapshot(self):
        with self._guard:
            value = deepcopy(self._paper_snapshot)
            value["health_stale"] = self._status != "running" or self._closing.is_set()
            if value["health_stale"]:
                value["execution_enabled"] = False
            return value

    def orders_snapshot(self):
        with self._guard:
            return deepcopy(self._orders_snapshot)

    def events_snapshot(self, after=0):
        if type(after) is not int or after < 0:
            raise IBKROperationError("invalid_cursor", "事件游标必须为非负整数。")
        with self._guard:
            return deepcopy([event for event in self._events_snapshot if event["seq"] > after])

    async def _request(self, method, args, kwargs):
        task = asyncio.current_task()
        self._requests.add(task)
        try:
            if self._closing.is_set():
                raise IBKROperationError("broker_closing", "Broker 正在关闭，不能执行新请求。")
            return await getattr(self._broker, method)(*args, **kwargs)
        finally:
            self._requests.discard(task)
            self._publish()

    def _call(self, method, args, kwargs, timeout):
        timeout = _timeout(timeout)
        with self._guard:
            loop, worker = self._loop, self._thread
            if self._status != "running" or self._closing.is_set() or loop is None:
                raise IBKROperationError("broker_not_running", "Broker 循环尚未就绪或已关闭。")
        if worker is threading.current_thread():
            raise IBKROperationError("facade_on_owner_thread", "同步门面不能阻塞 Broker 所属线程。")
        coroutine = self._request(method, deepcopy(args), {**deepcopy(kwargs), "timeout": timeout})
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()
            raise IBKROperationError("broker_not_running", "Broker 循环已关闭。") from None
        try:
            return future.result(timeout=timeout + 1)
        except (FutureTimeout, FutureCancelled):
            future.cancel()
            if method in ("submit_paper_order", "cancel_paper_order"):
                return {"order_ref": kwargs["order_ref"], "status": "unknown",
                        "broker_order_id": kwargs.get("broker_order_id"), "filled_quantity": 0}
            raise IBKROperationError("preview_timeout", "预览超时，不能提交订单。") from None

    def preview_paper_order(self, intent, *, order_ref, context, timeout=5.0):
        return self._call("preview_paper_order", (intent,), {"order_ref": order_ref, "context": context}, timeout)

    def submit_paper_order(self, intent, *, order_ref, preview, context, timeout=5.0):
        return self._call("submit_paper_order", (intent,),
                          {"order_ref": order_ref, "preview": preview, "context": context}, timeout)

    def cancel_paper_order(self, *, order_ref, broker_order_id, context, timeout=5.0):
        return self._call("cancel_paper_order", (),
                          {"order_ref": order_ref, "broker_order_id": broker_order_id, "context": context}, timeout)

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception:
            self._failure("broker_lifecycle_failed", "IBKR 后台循环异常，连接状态可能已过期；聊天仍可使用。")
        finally:
            with self._guard:
                self._loop, self._stop = None, None

    def close(self, timeout=5.0):
        """Cancel pending connect, disconnect on its owner loop, then join thread.

        A timeout/failure is reported rather than pretending resources closed.
        No cancellation or SDK methods run directly on an HTTP/Host thread.
        """
        self._closing.set()
        with self._guard:
            loop, stop, worker = self._loop, self._stop, self._thread
            if worker is None:
                self._status = "stopped"
                return True
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass  # Owner loop has already closed.
        if worker is threading.current_thread():
            return False
        worker.join(timeout)
        if worker.is_alive():
            self._failure("broker_shutdown_timeout", "IBKR 后台线程尚未退出，不能确认连接已清理。")
            return False
        with self._guard:
            return self._status == "stopped"
