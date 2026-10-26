"""Explicit, immutable IBKR configuration; importing this module never connects."""

from dataclasses import dataclass, field, fields
import hashlib
import ipaddress
import json
import math
import os
from typing import Mapping


ENV_PREFIX = "YOLO_AGENTS_TRADING_IBKR_"
DEFAULT_PORTS = {"paper": 4002, "live": 4001}


class IBKRConfigurationError(ValueError):
    """A safe error code/message without echoing supplied values or credentials."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self):
        return {"code": self.code, "message": str(self)}


def _integer(value, name):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            return int(value)
        except ValueError:
            pass
    raise IBKRConfigurationError("invalid_" + name, name + " 必须为整数。")


def _boolean(value, name):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise IBKRConfigurationError("invalid_" + name, name + " 必须明确为 true 或 false。")


@dataclass(frozen=True)
class IBKRConfig:
    environment: str
    host: str = "127.0.0.1"
    port: int | None = None
    client_id: int = 20
    read_only: bool = True
    execution_enabled: bool = False
    account: str | None = field(default=None, repr=False)
    connect_timeout: float = 5.0
    _sources: tuple = field(default=(), repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.environment, str) or self.environment not in DEFAULT_PORTS:
            raise IBKRConfigurationError("environment_required", "必须显式选择 environment=paper 或 live。")
        if not isinstance(self.host, str):
            raise IBKRConfigurationError("invalid_host", "host 必须是本机回环地址。")
        host = "127.0.0.1" if self.host == "localhost" else self.host
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise IBKRConfigurationError("invalid_host", "第一阶段仅支持数值回环地址或 localhost。") from None
        if not address.is_loopback or "%" in host:
            raise IBKRConfigurationError("invalid_host", "第一阶段 Gateway 必须运行在本机回环地址。")
        port = DEFAULT_PORTS[self.environment] if self.port is None else _integer(self.port, "port")
        client_id = _integer(self.client_id, "client_id")
        if not 1 <= port <= 65535:
            raise IBKRConfigurationError("invalid_port", "port 必须在 1 到 65535 之间。")
        if not 1 <= client_id <= 2147483647:
            raise IBKRConfigurationError("invalid_client_id", "client_id 必须为正整数；第一阶段禁止自动绑定订单的 client ID 0。")
        read_only = _boolean(self.read_only, "read_only")
        execution_enabled = _boolean(self.execution_enabled, "execution_enabled")
        if execution_enabled:
            raise IBKRConfigurationError("execution_not_supported", "IBKR 第一阶段仅支持只读，paper/live 均禁止开启执行。")
        if not read_only:
            raise IBKRConfigurationError("read_only_required", "IBKR 第一阶段要求 read_only=true。")
        if self.account is not None and (
            not isinstance(self.account, str) or not self.account or len(self.account) > 64
            or not self.account.isascii() or not self.account.isalnum()
        ):
            raise IBKRConfigurationError("invalid_account", "account 必须是显式账户标识；不要填写用户名、密码或其他凭据。")
        try:
            if isinstance(self.connect_timeout, bool):
                raise ValueError
            timeout = float(self.connect_timeout)
            if not math.isfinite(timeout) or not 0 < timeout <= 60:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise IBKRConfigurationError("invalid_connect_timeout", "connect_timeout 必须大于 0 且不超过 60 秒。") from None
        for key, value in {"host": str(address), "port": port, "client_id": client_id,
                           "read_only": read_only, "execution_enabled": execution_enabled,
                           "connect_timeout": timeout}.items():
            object.__setattr__(self, key, value)

    @property
    def config_id(self):
        values = {item.name: getattr(self, item.name) for item in fields(self) if not item.name.startswith("_")}
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def audit(self):
        """Account ID is omitted; config_id still changes when account changes."""
        return {"config_id": self.config_id, "environment": self.environment,
                "host": self.host, "port": self.port, "client_id": self.client_id,
                "read_only": self.read_only, "execution_enabled": self.execution_enabled,
                "execution_supported": False, "account_configured": self.account is not None,
                "connect_timeout": self.connect_timeout, "sources": dict(self._sources)}


def load_ibkr_config(base: Mapping | None = None, *, environ: Mapping | None = None,
                     overrides: Mapping | None = None) -> IBKRConfig:
    """Defaults < base JSON < prefixed environment < explicit overrides.

    Any layer that changes environment discards earlier profile values. Thus a
    live selection cannot silently inherit a paper account or custom endpoint.
    No value, port or account prefix is used to guess the actual broker mode.
    """
    allowed = {item.name for item in fields(IBKRConfig) if not item.name.startswith("_")}
    env = os.environ if environ is None else environ
    env_values = {key[len(ENV_PREFIX):].lower(): value for key, value in env.items()
                  if isinstance(key, str) and key.startswith(ENV_PREFIX)}
    values, sources = {}, {}
    for label, layer in (("base", base), ("environment", env_values), ("override", overrides)):
        if layer is None:
            continue
        if not isinstance(layer, Mapping) or any(key not in allowed for key in layer):
            raise IBKRConfigurationError("unknown_configuration_field", "配置包含不支持的字段；此接口不接收登录凭据。")
        if "environment" in layer and layer["environment"] != values.get("environment"):
            values.clear()
            sources.clear()
        values.update(layer)
        sources.update({key: label for key in layer})
    if "environment" not in values:
        raise IBKRConfigurationError("environment_required", "必须显式选择 environment=paper 或 live。")
    for key in allowed:
        sources.setdefault(key, "default")
    if values.get("port") is None:
        sources["port"] = "environment_default"
    return IBKRConfig(**values, _sources=tuple(sorted(sources.items())))
