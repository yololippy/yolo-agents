import json
from pathlib import Path
import tempfile
import unittest

from yolo_trade.apple_upgrade import BundleMetadata, DeviceApp
from yolo_trade.full_test import FullTestPreflightError, live_preflight, unresolved_order_summary
from yolo_trade.host_service import _paths


def ready_host():
    return {
        "service_ready": True,
        "host_health": {
            "status": "ok",
            "agent_id": "yolo-trade",
            "broker_state": "connected_paper",
            "broker_ready": True,
            "data_complete": True,
            "environment": "paper",
            "read_only": False,
            "execution_enabled": True,
        },
    }


def create_ledger(home: Path, state: str | None = None) -> None:
    import sqlite3
    ledger = _paths(home)["data"] / "paper-orders.sqlite3"
    ledger.parent.mkdir(parents=True)
    connection = sqlite3.connect(ledger)
    try:
        connection.execute("CREATE TABLE paper_orders (state TEXT NOT NULL)")
        if state is not None:
            connection.execute("INSERT INTO paper_orders VALUES (?)", (state,))
        connection.commit()
    finally:
        connection.close()


class FullTestPreflightTests(unittest.TestCase):
    def test_order_summary_reads_only_blocking_states_without_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            create_ledger(home, "unknown")
            summary = unresolved_order_summary(_paths(home)["data"])
        self.assertEqual(summary["blocking_count"], 1)
        self.assertEqual(summary["states"], {"unknown": 1})
        self.assertNotIn("order", json.dumps(summary))

    def test_live_preflight_passes_with_ready_host_current_apps_and_empty_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            create_ledger(home)
            result = live_preflight(
                device="private-device-id",
                mac_destination=home / "YoloTrade.app",
                home=home,
                host_status_reader=ready_host,
                app_reader=lambda _device: [DeviceApp("com.yoloagents.yolotrade", "1.0", "4")],
                mac_validator=lambda _path: BundleMetadata("com.yoloagents.yolotrade", "1.0", "4", "YoloTradeMac"),
            )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["orders"]["blocking_count"], 0)
        self.assertNotIn("private-device-id", json.dumps(result))

    def test_live_preflight_blocks_unresolved_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            create_ledger(home, "cancel_pending")
            with self.assertRaises(FullTestPreflightError):
                live_preflight(
                    device="private-device-id", mac_destination=home / "YoloTrade.app", home=home,
                    host_status_reader=ready_host,
                )

    def test_live_preflight_blocks_legacy_ios_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            create_ledger(home)
            with self.assertRaises(FullTestPreflightError):
                live_preflight(
                    device="private-device-id",
                    mac_destination=home / "YoloTrade.app",
                    home=home,
                    host_status_reader=ready_host,
                    app_reader=lambda _device: [
                        DeviceApp("com.yoloagents.yolotrade", "1.0", "4"),
                        DeviceApp("com.yoloagents.tradingagent", "1.0", "1"),
                    ],
                    mac_validator=lambda _path: BundleMetadata(
                        "com.yoloagents.yolotrade", "1.0", "4", "YoloTradeMac",
                    ),
                )

    def test_live_preflight_blocks_degraded_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            create_ledger(home)
            host = ready_host()
            host["host_health"]["data_complete"] = False
            with self.assertRaises(FullTestPreflightError):
                live_preflight(
                    device="private-device-id", mac_destination=home / "YoloTrade.app", home=home,
                    host_status_reader=lambda: host,
                )


if __name__ == "__main__":
    unittest.main()
