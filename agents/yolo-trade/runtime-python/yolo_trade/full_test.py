"""Read-only YoloTrade gates used before a full-test live installation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sqlite3

from .apple_upgrade import (
    LEGACY_YOLO_TRADE_BUNDLE_IDS,
    YOLO_TRADE_BUNDLE_ID,
    device_apps,
    validate_macos_bundle,
)
from .host_service import _paths, status as host_service_status


BLOCKING_ORDER_STATES = ("submitting", "unknown", "cancel_pending")


class FullTestPreflightError(RuntimeError):
    pass


def unresolved_order_summary(data_dir: Path) -> dict[str, object]:
    ledger = data_dir.expanduser().resolve() / "paper-orders.sqlite3"
    if not ledger.is_file() or ledger.is_symlink():
        return {"ledger_present": False, "blocking_count": 0, "states": {}}
    try:
        connection = sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT state, COUNT(*) FROM paper_orders "
                "WHERE state IN ('submitting','unknown','cancel_pending') GROUP BY state"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        raise FullTestPreflightError("无法只读核对 Paper 订单账本。") from None
    states = {state: count for state, count in rows}
    return {
        "ledger_present": True,
        "blocking_count": sum(states.values()),
        "states": {state: states[state] for state in BLOCKING_ORDER_STATES if state in states},
    }


def live_preflight(
    *,
    device: str,
    mac_destination: Path,
    home: Path | None = None,
    host_status_reader: Callable[[], dict] | None = None,
    app_reader=device_apps,
    mac_validator=validate_macos_bundle,
) -> dict[str, object]:
    host = host_status_reader() if host_status_reader is not None else host_service_status(home=home)
    health = host.get("host_health") if isinstance(host, dict) else None
    required_health = (
        isinstance(health, dict)
        and health.get("broker_state") == "connected_paper"
        and health.get("broker_ready") is True
        and health.get("data_complete") is True
        and health.get("environment") == "paper"
        and health.get("read_only") is False
        and health.get("execution_enabled") is True
    )
    if not isinstance(host, dict) or host.get("service_ready") is not True or not required_health:
        raise FullTestPreflightError("YoloTrade Host、Paper 执行配置或 Broker 数据尚未全部就绪。")

    orders = unresolved_order_summary(_paths(home)["data"])
    if orders["ledger_present"] is not True:
        raise FullTestPreflightError("Paper 订单账本不存在，不能证明升级前订单状态安全。")
    if orders["blocking_count"] != 0:
        raise FullTestPreflightError("存在 submitting、unknown 或 cancel_pending 订单，禁止升级或重启。")

    try:
        mac = mac_validator(mac_destination)
        installed_apps = app_reader(device)
    except Exception as error:
        raise FullTestPreflightError(f"读取已安装 App 状态失败：{type(error).__name__}") from None
    installed_ids = {item.bundle_id for item in installed_apps}
    if installed_ids.intersection(LEGACY_YOLO_TRADE_BUNDLE_IDS):
        raise FullTestPreflightError("iPhone 仍有旧身份 YoloTrade，不能作为普通覆盖升级。")
    current = next((item for item in installed_apps if item.bundle_id == YOLO_TRADE_BUNDLE_ID), None)
    if current is None:
        raise FullTestPreflightError("iPhone 未安装当前身份的 YoloTrade，不能执行升级测试。")
    return {
        "status": "passed",
        "host": {
            "service_ready": True,
            "agent_id": health.get("agent_id"),
            "broker_state": health.get("broker_state"),
            "broker_ready": True,
            "data_complete": True,
            "environment": "paper",
            "read_only": False,
            "execution_enabled": True,
        },
        "orders": orders,
        "macos": {
            "bundle_id": mac.bundle_id,
            "short_version": mac.short_version,
            "build_version": mac.build_version,
        },
        "ios": {
            "bundle_id": current.bundle_id,
            "short_version": current.short_version,
            "build_version": current.build_version,
            "legacy_identity_present": False,
        },
    }
