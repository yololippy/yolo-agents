"""Reusable local host process for a Yolo Agents instance."""

import argparse
import fcntl
import ipaddress
import os
from pathlib import Path
import signal
import ssl
import sys

from .api import ChatHTTPServer
from .auth import DeviceAuth
from .service import ChatService
from .identity import ensure_agent_identity, validate_agent_id


def create_parser(description: str | None = None, *, agent_name: str = "base", app_name: str | None = None,
                  default_port: int = 8765) -> argparse.ArgumentParser:
    validate_agent_id(agent_name)
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--data-dir", type=Path,
                        default=Path.home() / "Library/Application Support/YoloAgents" / agent_name)
    parser.add_argument("--adopt-existing-data", action="store_true", help="明确接管未标记的旧数据目录")
    parser.add_argument("--mode", choices=["demo", "pydantic", "codex", "chatgpt"], default="demo")
    parser.add_argument("--model", default=os.environ.get("YOLO_AGENTS_MODEL"))
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    return parser


def validate_host_arguments(args, parser) -> None:
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be provided together")
    try:
        loopback = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not args.tls_cert:
        parser.error("binding outside loopback requires --tls-cert and --tls-key")
    if args.mode == "pydantic" and not args.model:
        parser.error("--model or YOLO_AGENTS_MODEL is required for pydantic mode")


def prepare_agent_directory(data_dir, agent_name="base", app_name=None, *, adopt_existing=False):
    """Check/claim identity before an Agent creates private side-effecting services.

    run_host repeats this check while holding the single-instance lock. The
    caller must not start a broker before this function succeeds.
    """
    directory = Path(data_dir).expanduser().resolve()
    legacy = (Path.home() / "Library/Application Support/YoloAgents").resolve()
    if agent_name == "b-agent" and directory == legacy and (legacy / "app.sqlite3").exists():
        raise RuntimeError("B Agent cannot adopt the legacy Trading Agent directory.")
    if (agent_name == "trading-agent" and directory == legacy / agent_name
            and (legacy / "app.sqlite3").exists() and not (directory / ".agent-owner.json").exists()):
        raise RuntimeError("Legacy Trading chat exists. Explicitly use the old --data-dir with --adopt-existing-data, or migrate it before using the new default.")
    return ensure_agent_identity(directory, agent_name, adopt_existing=adopt_existing)


def run_host(args, *, extension=None, agent_name: str = "base", app_name: str | None = None) -> int:
    """Run one configured agent host with an optional agent-owned API extension."""
    args.data_dir = args.data_dir.expanduser().resolve()
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (args.data_dir / "server.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("another server is using this data directory") from None
    try:
        prepare_agent_directory(args.data_dir, agent_name, app_name, adopt_existing=getattr(args, "adopt_existing_data", False))
    except BaseException:
        lock.close()
        raise
    from .agent_runtime import create_runtime
    from .store import Store

    store = Store(args.data_dir / "app.sqlite3")
    try:
        runtime = create_runtime(args.mode, args.model, args.data_dir, store, agent_name=agent_name,
                                 app_name=app_name or agent_name)
    except (ImportError, RuntimeError, ValueError) as exc:
        print(f"Agent initialization failed ({type(exc).__name__}); check dependencies and model settings.", file=sys.stderr)
        store.close()
        lock.close()
        return 1
    auth = DeviceAuth(args.data_dir / "devices.sqlite3")
    service = ChatService(store, runtime)
    service.agent_id, service.app_name = agent_name, app_name or agent_name
    service.recover_interrupted()
    server = ChatHTTPServer((args.host, args.port), service, auth, extension=extension)
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    for platform in ("macos", "ios"):
        print(f"{platform} pairing code (one use, expires in 10 minutes): {auth.issue_pairing_code(platform)}", flush=True)
    scheme = "https" if args.tls_cert else "http"
    print(f"{scheme}://{args.host}:{server.server_address[1]} | agent={agent_name}:{runtime.mode}", flush=True)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()
        runtime.close()
        store.close()
        lock.close()
    return 0
