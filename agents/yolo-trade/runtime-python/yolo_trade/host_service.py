"""Install the YoloTrade Host as a private per-user macOS LaunchAgent."""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile

from .ibkr_config import IBKRConfigurationError, load_ibkr_config
from .product import AGENT_ID, DEFAULT_PORT


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
    if config.read_only is not False or config.execution_enabled is not True:
        raise HostServiceError("常驻 Host 必须保留 read_only=false、execution_enabled=true 的 Paper 执行配置。")
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


def _atomic_bytes(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _snapshot(path: Path) -> tuple[bytes, int] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise HostServiceError("Host 配置或 LaunchAgent 路径不是普通文件。")
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_bytes(path, snapshot[0], snapshot[1])


def launch_agent_payload(paths: dict[str, Path], wrapper: Path) -> dict:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(wrapper), "--mode", "chatgpt", "--host", "127.0.0.1", "--port", str(DEFAULT_PORT),
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
    previous_config = _snapshot(paths["config"])
    previous_plist = _snapshot(paths["plist"])
    payload = plistlib.dumps(launch_agent_payload(paths, wrapper), sort_keys=True)
    domain = f"gui/{os.getuid()}"
    bootout = subprocess.run(
        ["/bin/launchctl", "bootout", domain, str(paths["plist"])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    previously_loaded = bootout.returncode == 0
    try:
        _atomic_json(paths["config"], config)
        _private_directory(paths["logs"])
        _atomic_bytes(paths["plist"], payload, 0o644)
        result = subprocess.run(
            ["/bin/launchctl", "bootstrap", domain, str(paths["plist"])],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode != 0:
            raise HostServiceError("Host LaunchAgent 加载失败。")
    except Exception as error:
        subprocess.run(
            ["/bin/launchctl", "bootout", domain, str(paths["plist"])],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        try:
            _restore(paths["config"], previous_config)
            _restore(paths["plist"], previous_plist)
            if previously_loaded and previous_plist is not None:
                restored = subprocess.run(
                    ["/bin/launchctl", "bootstrap", domain, str(paths["plist"])],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
                if restored.returncode != 0:
                    raise HostServiceError("Host 升级失败，旧文件已恢复但旧服务未能重新加载。")
        except HostServiceError:
            raise
        except Exception:
            raise HostServiceError("Host 升级失败，旧配置恢复也失败。") from None
        if isinstance(error, HostServiceError):
            raise HostServiceError("Host 升级失败；旧配置和服务已恢复。") from None
        raise HostServiceError("Host 安装失败；旧配置和服务已恢复。") from None
    return paths["plist"]


def uninstall(*, home: Path | None = None) -> None:
    paths = _paths(home)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["/bin/launchctl", "bootout", domain, str(paths["plist"])],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    paths["plist"].unlink(missing_ok=True)


def listener_process_ids(port: int = DEFAULT_PORT, *, runner=subprocess.run) -> tuple[int, ...]:
    result = runner(
        ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, text=True,
    )
    if result.returncode != 0:
        return ()
    values = set()
    for line in result.stdout.splitlines():
        try:
            identifier = int(line.strip())
        except ValueError:
            continue
        if identifier > 0:
            values.add(identifier)
    return tuple(sorted(values))


def public_host_health(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                       timeout: float = 0.5) -> dict | None:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", "/v1/health")
        response = connection.getresponse()
        if response.status != 200:
            return None
        payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        broker = payload.get("broker") if isinstance(payload.get("broker"), dict) else {}
        configuration = broker.get("configuration") if isinstance(broker.get("configuration"), dict) else {}
        return {
            "status": payload.get("status") if isinstance(payload.get("status"), str) else None,
            "agent_id": payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None,
            "agent_mode": payload.get("agent_mode") if isinstance(payload.get("agent_mode"), str) else None,
            "broker_state": broker.get("state") if isinstance(broker.get("state"), str) else None,
            "broker_ready": broker.get("ready") is True,
            "data_complete": broker.get("data_complete") is True,
            "environment": configuration.get("environment") if isinstance(configuration.get("environment"), str) else None,
            "read_only": configuration.get("read_only") if type(configuration.get("read_only")) is bool else None,
            "execution_enabled": configuration.get("execution_enabled") if type(configuration.get("execution_enabled")) is bool else None,
        }
    except (OSError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def status(*, home: Path | None = None, runner=subprocess.run,
           health_reader=public_host_health) -> dict:
    paths = _paths(home)
    result = runner(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    try:
        config = validate_paper_config(paths["config"])
    except HostServiceError:
        config = None
    listeners = listener_process_ids(runner=runner)
    health = health_reader()
    identity_verified = isinstance(health, dict) and health.get("agent_id") == AGENT_ID and health.get("status") == "ok"
    return {
        "launch_agent_loaded": result.returncode == 0,
        "configuration_present": paths["config"].is_file(),
        "configuration_valid": config is not None,
        "paper_execution_configured": config is not None,
        "listener_count": len(listeners),
        "single_listener": len(listeners) == 1,
        "host_identity_verified": identity_verified,
        "host_health": health,
        "service_ready": result.returncode == 0 and config is not None and len(listeners) == 1 and identity_verified,
        "port": DEFAULT_PORT,
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
