"""Install the YoloTrade Host as a private per-user macOS LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

from .ibkr_config import IBKRConfigurationError, load_ibkr_config


LAUNCH_AGENT_LABEL = "com.yolo-agents.yolo-trade-host"


class HostServiceError(RuntimeError):
    pass


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists() and (candidate / "bin").is_dir():
            return candidate
    raise HostServiceError("无法定位 yolo-agents 仓库。")


def _paths(home: Path | None = None) -> dict[str, Path]:
    home = (home or Path.home()).expanduser().resolve()
    data = home / "Library" / "Application Support" / "YoloAgents" / "yolo-trade"
    return {
        "data": data,
        "config": data / "ibkr.json",
        "logs": home / "Library" / "Logs" / "YoloAgents" / "yolo-trade",
        "plist": home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist",
    }


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def validate_paper_config(path: Path) -> dict:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
        config = load_ibkr_config(value, environ={})
    except (OSError, UnicodeError, json.JSONDecodeError, IBKRConfigurationError):
        raise HostServiceError("IBKR 配置必须是可读取且完整的 JSON；不得包含登录凭据。") from None
    if config.environment != "paper" or config.account is None:
        raise HostServiceError("常驻 Host 只接受显式 Paper 环境和显式目标账户。")
    # Persist the exact validated product profile, not environment-derived values.
    return {
        "environment": config.environment,
        "host": config.host,
        "port": config.port,
        "client_id": config.client_id,
        "read_only": config.read_only,
        "execution_enabled": config.execution_enabled,
        "account": config.account,
        "connect_timeout": config.connect_timeout,
    }


def _atomic_json(path: Path, payload: dict) -> None:
    _private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix="ibkr.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def launch_agent_payload(paths: dict[str, Path], wrapper: Path) -> dict:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(wrapper), "--mode", "chatgpt", "--host", "127.0.0.1", "--port", "8765",
            "--data-dir", str(paths["data"]), "--ibkr-config", str(paths["config"]),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(paths["logs"] / "host.log"),
        "StandardErrorPath": str(paths["logs"] / "host.log"),
    }


def install(source_config: Path, *, home: Path | None = None) -> Path:
    paths = _paths(home)
    wrapper = _repo_root() / "bin" / "yolo-trade-host"
    if not wrapper.is_file():
        raise HostServiceError("YoloTrade Host 启动脚本不存在。")
    config = validate_paper_config(source_config)
    _atomic_json(paths["config"], config)
    _private_directory(paths["logs"])
    paths["plist"].parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"{LAUNCH_AGENT_LABEL}.", suffix=".plist", dir=paths["plist"].parent,
    )
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(launch_agent_payload(paths, wrapper), handle, sort_keys=True)
        os.replace(temporary, paths["plist"])
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    domain = f"gui/{os.getuid()}"
    subprocess.run(["/bin/launchctl", "bootout", domain, str(paths["plist"])],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    result = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(paths["plist"])],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        raise HostServiceError("Host LaunchAgent 已写入但加载失败。")
    return paths["plist"]


def uninstall(*, home: Path | None = None) -> None:
    paths = _paths(home)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["/bin/launchctl", "bootout", domain, str(paths["plist"])],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    paths["plist"].unlink(missing_ok=True)


def status(*, home: Path | None = None) -> dict:
    paths = _paths(home)
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return {
        "launch_agent_loaded": result.returncode == 0,
        "configuration_present": paths["config"].is_file(),
        "port": 8765,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="YoloTrade Host LaunchAgent")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install-launch-agent")
    install_parser.add_argument("--ibkr-config", type=Path, required=True)
    commands.add_parser("uninstall-launch-agent")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.command == "install-launch-agent":
            print(f"Host LaunchAgent 已安装：{install(args.ibkr_config)}")
        elif args.command == "uninstall-launch-agent":
            uninstall()
            print("Host LaunchAgent 已卸载；IBKR 配置和产品数据保留。")
        else:
            print(json.dumps(status(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except HostServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
