"""Paper-only IB Gateway process supervision through IBC on macOS.

The supervisor owns no trading decisions and never calls the broker API. It
only starts IBC when Gateway is absent. Credentials stay in macOS Keychain;
IBC receives an owner-only config file without credentials in argv or env.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict, dataclass
import getpass
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Protocol


KEYCHAIN_ACCOUNT = "paper"
KEYCHAIN_USERNAME_SERVICE = "com.yolo-agents.ibkr.paper.username"
KEYCHAIN_PASSWORD_SERVICE = "com.yolo-agents.ibkr.paper.password"
LAUNCH_AGENT_LABEL = "com.yolo-agents.ibkr-gateway"
GATEWAY_PROCESS_PATTERN = (
    r"IB Gateway 10\.50\.app/Contents/MacOS/JavaApplicationStub|"
    r"ibcalpha\.ibc\.IbcGateway"
)


class GatewaySupervisorError(RuntimeError):
    """Fixed, non-secret operational failure."""


class KeychainBackend(Protocol):
    def exists(self, service: str, account: str) -> bool: ...
    def read(self, service: str, account: str) -> str: ...
    def write(self, service: str, account: str, value: str) -> None: ...
    def delete(self, service: str, account: str) -> None: ...


class MacOSKeychain:
    """Small Security.framework adapter; secrets never enter process argv."""

    _NOT_FOUND = -25300

    def __init__(self):
        if sys.platform != "darwin":
            raise GatewaySupervisorError("Keychain 凭据仅支持 macOS。")
        self._security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self._core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._configure_functions()

    def _configure_functions(self) -> None:
        uint32 = ctypes.c_uint32
        void_pointer = ctypes.c_void_p
        char_pointer = ctypes.c_char_p
        self._security.SecKeychainFindGenericPassword.argtypes = [
            void_pointer, uint32, char_pointer, uint32, char_pointer,
            ctypes.POINTER(uint32), ctypes.POINTER(void_pointer), ctypes.POINTER(void_pointer),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            void_pointer, uint32, char_pointer, uint32, char_pointer,
            uint32, void_pointer, ctypes.POINTER(void_pointer),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
            void_pointer, void_pointer, uint32, void_pointer,
        ]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [void_pointer]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [void_pointer, void_pointer]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._core_foundation.CFRelease.argtypes = [void_pointer]
        self._core_foundation.CFRelease.restype = None

    @staticmethod
    def _identity(service: str, account: str) -> tuple[bytes, bytes]:
        return service.encode("utf-8"), account.encode("utf-8")

    def _find_item(self, service: str, account: str) -> tuple[int, ctypes.c_void_p]:
        service_bytes, account_bytes = self._identity(service, account)
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            None,
            None,
            ctypes.byref(item),
        )
        return status, item

    def exists(self, service: str, account: str) -> bool:
        status, item = self._find_item(service, account)
        if item.value:
            self._core_foundation.CFRelease(item)
        if status == 0:
            return True
        if status == self._NOT_FOUND:
            return False
        raise GatewaySupervisorError("无法读取 macOS Keychain 状态。")

    def read(self, service: str, account: str) -> str:
        service_bytes, account_bytes = self._identity(service, account)
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        try:
            if status == self._NOT_FOUND:
                raise GatewaySupervisorError("IBKR Paper 凭据不存在。")
            if status != 0 or not data.value:
                raise GatewaySupervisorError("IBKR Paper 凭据当前不可访问。")
            value = ctypes.string_at(data, length.value).decode("utf-8")
            _validate_secret(value)
            return value
        finally:
            if data.value:
                self._security.SecKeychainItemFreeContent(None, data)
            if item.value:
                self._core_foundation.CFRelease(item)

    def write(self, service: str, account: str, value: str) -> None:
        _validate_secret(value)
        secret = value.encode("utf-8")
        buffer = ctypes.create_string_buffer(secret)
        status, item = self._find_item(service, account)
        try:
            if status == 0:
                result = self._security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(secret), ctypes.cast(buffer, ctypes.c_void_p)
                )
            elif status == self._NOT_FOUND:
                service_bytes, account_bytes = self._identity(service, account)
                created = ctypes.c_void_p()
                result = self._security.SecKeychainAddGenericPassword(
                    None,
                    len(service_bytes),
                    service_bytes,
                    len(account_bytes),
                    account_bytes,
                    len(secret),
                    ctypes.cast(buffer, ctypes.c_void_p),
                    ctypes.byref(created),
                )
                if created.value:
                    self._core_foundation.CFRelease(created)
            else:
                raise GatewaySupervisorError("无法检查 macOS Keychain 项。")
            if result != 0:
                raise GatewaySupervisorError("写入 macOS Keychain 失败。")
        finally:
            if item.value:
                self._core_foundation.CFRelease(item)

    def delete(self, service: str, account: str) -> None:
        status, item = self._find_item(service, account)
        try:
            if status == self._NOT_FOUND:
                return
            if status != 0 or not item.value:
                raise GatewaySupervisorError("无法检查 macOS Keychain 项。")
            if self._security.SecKeychainItemDelete(item) != 0:
                raise GatewaySupervisorError("删除 macOS Keychain 项失败。")
        finally:
            if item.value:
                self._core_foundation.CFRelease(item)


@dataclass(frozen=True)
class SupervisorPaths:
    home: Path
    ibc_path: Path
    gateway_path: Path
    cache_dir: Path
    state_dir: Path
    log_dir: Path
    runtime_config: Path
    launch_agent: Path

    @classmethod
    def defaults(cls, home: Path | None = None) -> "SupervisorPaths":
        home = (home or Path.home()).expanduser().resolve()
        app_support = home / "Library" / "Application Support" / "YoloAgents"
        cache_dir = home / "Library" / "Caches" / "YoloAgents" / "ibkr-gateway"
        state_dir = app_support / "ibkr-gateway"
        log_dir = home / "Library" / "Logs" / "YoloAgents" / "ibkr-gateway"
        return cls(
            home=home,
            ibc_path=app_support / "IBC" / "current",
            gateway_path=home / "Applications",
            cache_dir=cache_dir,
            state_dir=state_dir,
            log_dir=log_dir,
            runtime_config=cache_dir / "config.runtime.ini",
            launch_agent=home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist",
        )


@dataclass(frozen=True)
class GatewayStatus:
    state: str
    port_open: bool
    gateway_process_running: bool
    credentials_ready: bool
    ibc_installed: bool
    launch_agent_loaded: bool
    paper_only: bool = True
    host: str = "127.0.0.1"
    port: int = 4002


class KeychainCredentials:
    """Read Paper credentials without ever accepting them as CLI arguments."""

    def __init__(self, backend: KeychainBackend | None = None):
        self._backend = backend or MacOSKeychain()

    def exists(self, service: str) -> bool:
        return self._backend.exists(service, KEYCHAIN_ACCOUNT)

    def ready(self) -> bool:
        return self.exists(KEYCHAIN_USERNAME_SERVICE) and self.exists(KEYCHAIN_PASSWORD_SERVICE)

    def read(self, service: str) -> str:
        value = self._backend.read(service, KEYCHAIN_ACCOUNT)
        _validate_secret(value)
        return value

    def pair(self) -> tuple[str, str]:
        return self.read(KEYCHAIN_USERNAME_SERVICE), self.read(KEYCHAIN_PASSWORD_SERVICE)

    def set_interactive(self) -> None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise GatewaySupervisorError("凭据录入必须在交互式终端中执行。")
        username = getpass.getpass("1/3 IBKR Paper 用户名（输入不会显示）：")
        password = getpass.getpass("2/3 IBKR Paper 密码（输入不会显示）：")
        confirmation = getpass.getpass("3/3 再次输入密码确认（输入不会显示）：")
        _validate_secret(username)
        _validate_secret(password)
        if password != confirmation:
            raise GatewaySupervisorError("两次密码输入不一致；Keychain 未写入。")
        entries = (
            (KEYCHAIN_USERNAME_SERVICE, username),
            (KEYCHAIN_PASSWORD_SERVICE, password),
        )
        try:
            for service, value in entries:
                self._backend.write(service, KEYCHAIN_ACCOUNT, value)
        except Exception:
            self.delete()
            raise

    def delete(self) -> None:
        for service in (KEYCHAIN_USERNAME_SERVICE, KEYCHAIN_PASSWORD_SERVICE):
            self._backend.delete(service, KEYCHAIN_ACCOUNT)


def _validate_secret(value: str) -> None:
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise GatewaySupervisorError("Keychain 凭据为空或包含不支持的换行字符。")


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def render_ibc_config(template: Path, destination: Path, username: str, password: str) -> None:
    """Render the vendor template atomically with only safe Paper overrides."""
    _validate_secret(username)
    _validate_secret(password)
    if not template.is_file():
        raise GatewaySupervisorError("IBC 配置模板不存在；请先安装 IBC。")
    replacements = {
        "IbLoginId": username,
        "IbPassword": password,
        "ReloginAfterSecondFactorAuthenticationTimeout": "no",
        "ExitAfterSecondFactorAuthenticationTimeout": "no",
        "TradingMode": "paper",
        "AcceptNonBrokerageAccountWarning": "yes",
        "ExistingSessionDetectedAction": "manual",
        "ReadOnlyLogin": "no",
        "ReadOnlyApi": "",
        "AutoLogoffTime": "",
        "AutoRestartTime": "06:15 AM",
        "ColdRestartTime": "14:15",
        "ClosedownAt": "",
        "AcceptIncomingConnectionAction": "reject",
    }
    seen: set[str] = set()
    output: list[str] = []
    for line in template.read_text(encoding="utf-8").splitlines(keepends=True):
        key = line.split("=", 1)[0] if "=" in line else None
        if key in replacements:
            if key in seen:
                raise GatewaySupervisorError("IBC 配置模板包含重复关键字段。")
            ending = "\n" if line.endswith("\n") else ""
            output.append(f"{key}={replacements[key]}{ending}")
            seen.add(key)
        else:
            output.append(line)
    if seen != set(replacements):
        raise GatewaySupervisorError("IBC 配置模板缺少必要字段，拒绝生成运行配置。")
    _ensure_private_directory(destination.parent)
    descriptor, temporary = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=destination.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.writelines(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def port_is_open(host: str = "127.0.0.1", port: int = 4002, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gateway_process_running(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    result = runner(
        ["/usr/bin/pgrep", "-f", GATEWAY_PROCESS_PATTERN],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def launch_agent_loaded(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    result = runner(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def classify_status(
    *, is_port_open: bool, is_gateway_running: bool, credentials_ready: bool,
    ibc_installed: bool, is_launch_agent_loaded: bool,
) -> GatewayStatus:
    if is_port_open:
        state = "gateway_ready"
    elif is_gateway_running:
        state = "authentication_required"
    elif not credentials_ready:
        state = "credentials_missing"
    elif not ibc_installed:
        state = "ibc_missing"
    else:
        state = "gateway_stopped"
    return GatewayStatus(
        state=state,
        port_open=is_port_open,
        gateway_process_running=is_gateway_running,
        credentials_ready=credentials_ready,
        ibc_installed=ibc_installed,
        launch_agent_loaded=is_launch_agent_loaded,
    )


def current_status(paths: SupervisorPaths, credentials: KeychainCredentials | None = None) -> GatewayStatus:
    credentials = credentials or KeychainCredentials()
    return classify_status(
        is_port_open=port_is_open(),
        is_gateway_running=gateway_process_running(),
        credentials_ready=credentials.ready(),
        ibc_installed=(paths.ibc_path / "config.ini").is_file()
        and (paths.ibc_path / "scripts" / "ibcstart.sh").is_file(),
        is_launch_agent_loaded=launch_agent_loaded(),
    )


def ibc_command(paths: SupervisorPaths) -> list[str]:
    """Return argv containing paths and Paper mode, never credentials."""
    return [
        str(paths.ibc_path / "scripts" / "ibcstart.sh"),
        "10.50", "-g", f"--tws-path={paths.gateway_path}", "--tws-settings-path=",
        f"--ibc-path={paths.ibc_path}", f"--ibc-ini={paths.runtime_config}",
        "--user=", "--pw=", "--fix-user=", "--fix-pw=", "--java-path=",
        "--mode=paper", "--on2fatimeout=exit",
    ]


def _redact_ibc_output(stream, destination: Path, secrets: tuple[str, ...]) -> None:
    """Copy IBC output without ever persisting credential values or fields."""
    secret_bytes = tuple(value.encode("utf-8") for value in secrets if value)
    sensitive_fields = (b"IbLoginId", b"IbPassword", b"FIXLoginId", b"FIXPassword")
    _ensure_private_directory(destination.parent)
    destination.touch(mode=0o600, exist_ok=True)
    destination.chmod(0o600)
    try:
        with destination.open("ab", buffering=0) as log:
            while True:
                raw = stream.readline()
                if not raw:
                    break
                stripped = raw.lstrip()
                if any(stripped.startswith(field + b"=") for field in sensitive_fields):
                    indentation = raw[: len(raw) - len(stripped)]
                    field = stripped.split(b"=", 1)[0]
                    ending = b"\n" if raw.endswith(b"\n") else b""
                    raw = indentation + field + b"=<redacted>" + ending
                else:
                    for secret in secret_bytes:
                        raw = raw.replace(secret, b"<redacted>")
                log.write(raw)
    finally:
        stream.close()


def launch_ibc(paths: SupervisorPaths, credentials: KeychainCredentials) -> subprocess.Popen:
    username, password = credentials.pair()
    render_ibc_config(paths.ibc_path / "config.ini", paths.runtime_config, username, password)
    _ensure_private_directory(paths.log_dir)
    process = subprocess.Popen(
        ibc_command(paths), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
    )
    if process.stdout is None:
        process.terminate()
        raise GatewaySupervisorError("无法建立 IBC 脱敏日志管道。")
    threading.Thread(
        target=_redact_ibc_output,
        args=(process.stdout, paths.log_dir / "supervisor-ibc.log", (username, password)),
        name="ibc-log-redactor",
        daemon=True,
    ).start()
    return process


def _notify(title: str, message: str) -> None:
    script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
    subprocess.run(
        ["/usr/bin/osascript", "-e", script, title, message],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def _write_status(paths: SupervisorPaths, status: GatewayStatus) -> None:
    _ensure_private_directory(paths.state_dir)
    payload = {**asdict(status), "updated_at": int(time.time())}
    descriptor, temporary = tempfile.mkstemp(prefix="status.", suffix=".tmp", dir=paths.state_dir)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, paths.state_dir / "status.json")
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def supervise(paths: SupervisorPaths, *, poll_interval: float = 5.0) -> int:
    credentials = KeychainCredentials()
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_state: str | None = None
    next_launch_at = 0.0
    backoff = 30.0
    child: subprocess.Popen | None = None
    while not stopping:
        status = current_status(paths, credentials)
        _write_status(paths, status)
        if status.state != last_state:
            if status.state == "authentication_required":
                _notify("IBKR Gateway 需要认证", "请在 IBKR Mobile 完成 2FA，或检查 Mac 上的登录窗口。")
            elif status.state == "credentials_missing":
                _notify("IBKR 自动登录尚未启用", "请在终端运行 yolo-ibkr-gateway credentials-set。")
            elif status.state == "gateway_ready":
                _notify("IBKR Gateway 已连接", "Paper Gateway 端口 4002 已恢复；交易服务将重新对账。")
                backoff = 30.0
            last_state = status.state

        if child is not None and child.poll() is not None:
            child = None
            next_launch_at = time.monotonic() + backoff
            backoff = min(backoff * 2, 900.0)

        if status.state == "gateway_stopped" and child is None and time.monotonic() >= next_launch_at:
            try:
                child = launch_ibc(paths, credentials)
                next_launch_at = time.monotonic() + 30.0
            except Exception:
                _notify("IBKR Gateway 启动失败", "IBC 未能启动；请运行状态检查并查看本机日志。")
                next_launch_at = time.monotonic() + backoff
                backoff = min(backoff * 2, 900.0)
        time.sleep(poll_interval)
    return 0


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists() and (candidate / "bin").is_dir():
            return candidate
    raise GatewaySupervisorError("无法定位 yolo-agents 仓库。")


def install_launch_agent(paths: SupervisorPaths) -> None:
    wrapper = _repo_root() / "bin" / "yolo-ibkr-gateway"
    if not wrapper.is_file():
        raise GatewaySupervisorError("Gateway Supervisor 启动脚本不存在。")
    _ensure_private_directory(paths.log_dir)
    paths.launch_agent.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(wrapper), "supervise"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(paths.log_dir / "launch-agent.log"),
        "StandardErrorPath": str(paths.log_dir / "launch-agent.log"),
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"{LAUNCH_AGENT_LABEL}.", suffix=".plist", dir=paths.launch_agent.parent
    )
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
        os.replace(temporary, paths.launch_agent)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["/bin/launchctl", "bootout", domain, str(paths.launch_agent)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    result = subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(paths.launch_agent)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode != 0:
        raise GatewaySupervisorError("LaunchAgent 安装完成但加载失败。")


def uninstall_launch_agent(paths: SupervisorPaths) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["/bin/launchctl", "bootout", domain, str(paths.launch_agent)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    paths.launch_agent.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Yolo Agents Paper IB Gateway supervisor")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status", help="显示脱敏运行状态")
    subcommands.add_parser("credentials-set", help="交互式写入 Paper 凭据到 macOS Keychain")
    subcommands.add_parser("credentials-delete", help="删除 Keychain 中的 Paper 凭据")
    subcommands.add_parser("install-launch-agent", help="安装并加载用户级 LaunchAgent")
    subcommands.add_parser("uninstall-launch-agent", help="卸载 LaunchAgent；不结束现有 Gateway")
    supervise_parser = subcommands.add_parser("supervise", help=argparse.SUPPRESS)
    supervise_parser.add_argument("--poll-interval", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = SupervisorPaths.defaults()
    try:
        if args.command == "status":
            print(json.dumps(asdict(current_status(paths)), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "credentials-set":
            KeychainCredentials().set_interactive()
            print("IBKR Paper 用户名和密码已写入 macOS Keychain。")
        elif args.command == "credentials-delete":
            KeychainCredentials().delete()
            paths.runtime_config.unlink(missing_ok=True)
            print("IBKR Paper Keychain 凭据和运行配置已删除。")
        elif args.command == "install-launch-agent":
            install_launch_agent(paths)
            print(f"LaunchAgent 已安装：{paths.launch_agent}")
        elif args.command == "uninstall-launch-agent":
            uninstall_launch_agent(paths)
            print("LaunchAgent 已卸载；现有 Gateway 进程未被结束。")
        elif args.command == "supervise":
            if not 1.0 <= args.poll_interval <= 60.0:
                raise GatewaySupervisorError("轮询间隔必须在 1 到 60 秒之间。")
            return supervise(paths, poll_interval=args.poll_interval)
        return 0
    except GatewaySupervisorError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
