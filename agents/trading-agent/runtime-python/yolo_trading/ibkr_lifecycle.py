"""Trading-owned background loop; HTTP threads only read copied health snapshots."""

import asyncio
from copy import deepcopy
import threading

from .ibkr import IBKRAdapter


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
        with self._guard:
            self._snapshot = health
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
            await self._broker.disconnect()
            self._publish()
            if self._broker.health()["connected"]:
                self._failure("broker_disconnect_failed", "IBKR 连接关闭无法确认，请检查 Gateway 和 Host 进程。")
            else:
                with self._guard:
                    self._status = "stopped"

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
