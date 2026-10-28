#!/usr/bin/env python3
"""Exercise real local hosts and HTTP isolation; never authorize OAuth or connect IBKR.

Run with the repository .venv Python. All state and pairing credentials are temporary.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def environment():
    result = {k: v for k, v in os.environ.items() if not k.startswith("YOLO_AGENTS_")}
    result["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in (
        "packages/core-python", "agents/trading-agent/runtime-python", "agents/b-agent/runtime-python"))
    result["PYTHONUNBUFFERED"] = "1"
    return result


class Host:
    def __init__(self, module, folder, mode):
        self.proc = subprocess.Popen([sys.executable, "-m", module, "--mode", mode,
            "--host", "127.0.0.1", "--port", "0", "--data-dir", str(folder)],
            cwd=ROOT, env=environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.codes = {}
        lines = queue.Queue()
        def read():
            for line in self.proc.stdout:
                lines.put(line)
            lines.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            while True:
                line = lines.get(timeout=15)
                if line is None:
                    raise RuntimeError("Fixture host exited before becoming ready")
                if " pairing code " in line:
                    self.codes[line.split()[0]] = line.rsplit(": ", 1)[1].strip()
                match = re.match(r"(http://127\.0\.0\.1:\d+) \|", line)
                if match:
                    self.url = match[1]
                    break
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.reader.join(timeout=2)
        self.proc.stdout.close()

    def request(self, path, *, token=None, data=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(self.url + "/v1/" + path,
            data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def pair(self, platform):
        status, result = self.request("devices/pair", data={"pairing_code": self.codes[platform],
            "device_name": "isolated-test", "platform": platform})
        if status != 201:
            raise RuntimeError("Fixture pairing failed")
        return result["token"]


class AgentIsolationTests(unittest.TestCase):
    def test_two_hosts_preserve_history_and_isolate_provider_accounts(self):
        with tempfile.TemporaryDirectory(prefix="yolo-provider-isolation-") as folder:
            trading, other = Path(folder) / "trading", Path(folder) / "b"
            with ExitStack() as stack:
                first = stack.enter_context(Host("yolo_trading", trading, "demo"))
                second = stack.enter_context(Host("yolo_b_agent", other, "demo"))
                first_token, second_token = first.pair("macos"), second.pair("macos")
                a, b = first.request("health")[1], second.request("health")[1]
                self.assertEqual(a["agent_id"], "trading-agent")
                self.assertEqual(b["agent_id"], "b-agent")
                self.assertNotEqual(a["server_id"], b["server_id"])
                self.assertEqual(second.request("conversations", token=first_token)[0], 401)
                status, created = first.request("conversations", token=first_token, data={"title": "keep-local-history"})
                self.assertEqual(status, 201)
                conversation_id = created["conversation"]["id"]
                self.assertEqual(second.request("conversations", token=second_token)[1]["conversations"], [])
                self.assertEqual(second.request("orders", token=second_token)[0], 404)
            # Same owned data survives an explicit mode switch; no Codex token is imported.
            with ExitStack() as stack:
                first = stack.enter_context(Host("yolo_trading", trading, "chatgpt"))
                second = stack.enter_context(Host("yolo_b_agent", other, "chatgpt"))
                for host, agent_id in ((first, "trading-agent"), (second, "b-agent")):
                    mac = host.pair("macos")
                    ios = host.pair("ios")
                    status, result = host.request("account", token=mac)
                    self.assertEqual(status, 200)
                    self.assertEqual(result["account"]["agent_id"], agent_id)
                    self.assertFalse(result["account"]["status"] in ("authenticated", "logged_in"))
                    self.assertEqual(host.request("account", token=ios)[0], 403)
                    self.assertEqual(host.request("account/logout", token=ios, data={})[0], 403)
                    self.assertEqual(host.request("models", token=ios)[0], 200)
                    self.assertNotIn("access_token", json.dumps(result))
                    self.assertNotIn("refresh_token", json.dumps(result))
                # Construct two real SDK OAuth flows without opening a browser or sending credentials.
                a_login = first.request("account/login", token=first_token, data={})
                b_login = second.request("account/login", token=second_token, data={})
                self.assertEqual(a_login[0], 200)
                self.assertEqual(b_login[0], 200)
                a_url, b_url = (urlsplit(reply[1]["auth_url"]) for reply in (a_login, b_login))
                self.assertEqual(a_url.hostname, "auth.openai.com")
                self.assertEqual(b_url.hostname, "auth.openai.com")
                a_query, b_query = parse_qs(a_url.query), parse_qs(b_url.query)
                self.assertEqual(a_query["agent_name_hint"], ["Trading Agent"])
                self.assertEqual(b_query["agent_name_hint"], ["B Agent"])
                self.assertNotEqual(a_query["state"], b_query["state"])
                self.assertNotEqual(a_query["redirect_uri"], b_query["redirect_uri"])
                self.assertEqual(first.request("account/login/cancel", token=first_token, data={})[0], 200)
                self.assertEqual(second.request("account", token=second_token)[1]["account"]["status"], "login_pending")
                cancelled = second.request("account/login/cancel", token=second_token, data={})
                self.assertEqual(cancelled[0], 200)
                self.assertEqual(cancelled[1]["account"]["error"]["code"], "login_cancelled")
                self.assertIsInstance(cancelled[1]["account"]["error"]["message"], str)
                rows = first.request("conversations", token=first_token)[1]["conversations"]
                self.assertEqual([row["id"] for row in rows], [conversation_id])
                self.assertEqual(second.request("conversations", token=second_token)[1]["conversations"], [])
            # Cross-agent ownership must fail before creating a trading ledger in B's directory.
            existing = sorted(p.name for p in other.iterdir())
            attempted = subprocess.run([sys.executable, "-m", "yolo_trading", "--mode", "demo", "--port", "0",
                "--data-dir", str(other)], cwd=ROOT, env=environment(), capture_output=True, timeout=10)
            self.assertNotEqual(attempted.returncode, 0)
            self.assertEqual(sorted(p.name for p in other.iterdir()), existing)
            self.assertFalse((other / "paper-orders.sqlite3").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
