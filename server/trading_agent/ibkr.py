"""Read-only IBKR lifecycle and offline configuration inspection.

Import, construction and health() never open sockets. An integrating service
must explicitly await connect(); this module exposes no order operations.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import importlib
import importlib.util
import json
from pathlib import Path

from .ibkr_config import IBKRConfig, IBKRConfigurationError, load_ibkr_config


class IBKRAdapter:
    def __init__(self, config: IBKRConfig, *, client_factory=None):
        if not isinstance(config, IBKRConfig):
            raise TypeError("config must be an IBKRConfig")
        self._config = config
        self._client_factory = client_factory
        self._client = None
        self._lock = asyncio.Lock()
        self._state = "disconnected"
        self._error = None
        self._attempted = False
        self._subscriptions = []
        self._revision = 1
        self._last_change = None

    @property
    def config(self):
        return self._config

    def _dependency_available(self):
        if self._client_factory is not None:
            return True
        try:
            return importlib.util.find_spec("ib_async") is not None
        except (ImportError, ValueError):
            return False

    def health(self):
        available = self._dependency_available()
        blockers = []
        if not available:
            blockers.append({"code": "dependency_missing", "message": "未安装可选依赖 ib_async。"})
        if self._config.account is None:
            blockers.append({"code": "account_required", "message": "连接前需显式配置目标账户 account。"})
        state = self._state
        if state == "disconnected" and blockers:
            state = "dependency_missing" if not available else "configuration_incomplete"
        if self._client is not None and not self._client.isConnected() and state not in ("connecting", "disconnected"):
            state = "gateway_unavailable"
        return {"state": state, "configuration": self._config.audit(),
                "configuration_revision": self._revision, "last_configuration_change": self._last_change,
                "dependency_available": available, "test_client": self._client_factory is not None,
                "connection_attempted": self._attempted,
                "connected": bool(self._client is not None and self._client.isConnected()),
                "environment_verified": False, "data_complete": False,
                "execution_enabled": False, "blockers": blockers, "error": self._error}

    def _fail(self, state, code, message):
        self._state, self._error = state, {"code": code, "message": message}

    def _disconnect_client(self):
        client = self._client
        if client is not None:
            try:
                client.disconnect()
                if client.isConnected():
                    raise ConnectionError
            except Exception:
                self._fail("disconnect_failed", "disconnect_failed", "无法确认原 Gateway 连接已关闭，配置切换已阻止。")
                return False
        for event, handler in self._subscriptions:
            event.disconnect(handler)
        self._subscriptions.clear()
        self._client = None
        return True

    def _on_disconnected(self, *args):
        self._fail("gateway_unavailable", "connection_lost", "Gateway 连接已断开，需恢复后重新对账。")

    def _on_error(self, request_id, error_code, error_text, *args):
        # Never publish/log broker error_text: it can contain account details.
        if error_code in (1100, 1300, 2110):
            self._fail("broker_unavailable", "broker_connection_lost", "Gateway 与 IBKR 的连接异常，当前数据不可视为最新。")
        elif error_code in (1101, 1102):
            self._fail("reconciliation_required", "reconciliation_required", "券商连接恢复，需重新核对账户、挂单、成交和订阅。")
        elif error_code == 10197:
            self._fail("session_conflict", "session_conflict", "存在竞争登录会话，请检查 Gateway 登录状态。")

    async def connect(self):
        async with self._lock:
            if self._client is not None:
                # An existing unhealthy connection requires explicit disconnect
                # and re-reconciliation, never a silent configuration fallback.
                return self.health()
            if self._config.account is None or not self._dependency_available():
                return self.health()
            self._state, self._error, self._attempted = "connecting", None, True
            try:
                factory = self._client_factory
                if factory is None:
                    try:
                        factory = importlib.import_module("ib_async").IB
                    except (ImportError, AttributeError):
                        self._fail("dependency_unavailable", "dependency_unavailable", "ib_async 无法加载，请核对可选依赖及版本。")
                        return self.health()
                client = factory()
                self._client = client
                client.RaiseRequestErrors = True
                for name, handler in (("disconnectedEvent", self._on_disconnected), ("errorEvent", self._on_error)):
                    event = getattr(client, name)
                    event.connect(handler)
                    self._subscriptions.append((event, handler))
                await asyncio.wait_for(client.connectAsync(
                    self._config.host, self._config.port, clientId=self._config.client_id,
                    timeout=self._config.connect_timeout, readonly=True,
                    account=self._config.account, raiseSyncErrors=True,
                ), timeout=self._config.connect_timeout)
                if not client.isConnected():
                    raise ConnectionError
                if self._config.account not in client.managedAccounts():
                    if self._disconnect_client():
                        self._fail("account_mismatch", "account_mismatch", "Gateway 返回的账户不包含配置目标；已断开，未自动选择其他账户。")
                elif self._state == "connecting":
                    self._state = "connected_read_only"
            except asyncio.CancelledError:
                if self._disconnect_client():
                    self._fail("disconnected", "connection_cancelled", "连接已取消。")
                raise
            except Exception:
                if self._disconnect_client():
                    self._fail("gateway_unavailable", "connection_failed", "无法完成 Gateway 连接；请检查进程、登录/API 配置、端口及 client ID。")
            return self.health()

    async def disconnect(self):
        async with self._lock:
            if self._disconnect_client():
                self._state, self._error = "disconnected", None
            return self.health()

    async def reconfigure(self, config: IBKRConfig):
        if not isinstance(config, IBKRConfig):
            raise TypeError("config must be an IBKRConfig")
        async with self._lock:
            previous = self._config.config_id
            if not self._disconnect_client():
                return self.health()
            self._config = config
            self._revision += 1
            self._state, self._error, self._attempted = "disconnected", None, False
            self._last_change = {"from_config_id": previous, "to_config_id": config.config_id,
                                 "changed_at": datetime.now(timezone.utc).isoformat()}
            # Switching is offline and never reconnects or retries an old action.
            return self.health()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect IBKR configuration without connecting to Gateway.")
    parser.add_argument("--config", type=Path, help="JSON config; no username/password fields")
    parser.add_argument("--environment", choices=("paper", "live"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--client-id", type=int)
    parser.add_argument("--account")
    parser.add_argument("--read-only", choices=("true", "false"))
    parser.add_argument("--execution-enabled", choices=("true", "false"))
    parser.add_argument("--connect-timeout", type=float)
    args = parser.parse_args(argv)
    try:
        base = json.loads(args.config.read_text()) if args.config else None
        overrides = {key: value for key, value in vars(args).items() if key != "config" and value is not None}
        config = load_ibkr_config(base, overrides=overrides)
        health = IBKRAdapter(config).health()
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(json.dumps({"state": "configuration_error", "error": {"code": "config_file_unreadable", "message": "无法读取配置 JSON 文件。"}}, ensure_ascii=False))
        return 2
    except IBKRConfigurationError as exc:
        print(json.dumps({"state": "configuration_error", "error": exc.as_dict()}, ensure_ascii=False))
        return 2
    print(json.dumps(health, ensure_ascii=False))
    return 1 if health["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
