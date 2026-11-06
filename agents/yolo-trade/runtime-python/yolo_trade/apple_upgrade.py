"""Validated Apple app replacement for YoloTrade full tests and installs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
from uuid import uuid4


YOLO_TRADE_BUNDLE_ID = "com.yoloagents.yolotrade"
DEFAULT_DEVELOPER_DIR = Path("/Applications/Xcode.app/Contents/Developer")


class AppleUpgradeError(RuntimeError):
    pass


@dataclass(frozen=True)
class BundleMetadata:
    bundle_id: str
    short_version: str | None
    build_version: str | None
    executable: str


@dataclass(frozen=True)
class MacReplacement:
    installed: str
    backup: str | None
    metadata: BundleMetadata


@dataclass(frozen=True)
class DeviceApp:
    bundle_id: str
    short_version: str | None
    build_version: str | None


@dataclass(frozen=True)
class IOSReplacement:
    replaced_existing: bool
    metadata: BundleMetadata


def _regular_directory(path: Path, label: str) -> Path:
    path = path.expanduser()
    if path.is_symlink() or not path.is_dir():
        raise AppleUpgradeError(f"{label}必须是普通 App 目录。")
    return path.resolve()


def bundle_metadata(path: Path, *, platform: str = "macos") -> BundleMetadata:
    app = _regular_directory(path, "App")
    plist = app / ("Contents/Info.plist" if platform == "macos" else "Info.plist")
    try:
        value = plistlib.loads(plist.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise AppleUpgradeError("App 的 Info.plist 不可读取。") from None
    bundle_id = value.get("CFBundleIdentifier")
    executable = value.get("CFBundleExecutable")
    if not isinstance(bundle_id, str) or not bundle_id or not isinstance(executable, str) or not executable:
        raise AppleUpgradeError("App 缺少有效的 Bundle ID 或可执行文件名。")
    executable_path = app / ("Contents/MacOS" if platform == "macos" else "") / executable
    if executable_path.is_symlink() or not executable_path.is_file():
        raise AppleUpgradeError("App 的主可执行文件不存在或不安全。")
    return BundleMetadata(
        bundle_id=bundle_id,
        short_version=value.get("CFBundleShortVersionString") if isinstance(value.get("CFBundleShortVersionString"), str) else None,
        build_version=value.get("CFBundleVersion") if isinstance(value.get("CFBundleVersion"), str) else None,
        executable=executable,
    )


def _run_checked(command: list[str], *, run=subprocess.run, message: str) -> None:
    try:
        result = run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        raise AppleUpgradeError(message) from None
    if result.returncode != 0:
        raise AppleUpgradeError(message)


def _developer_tool(name: str) -> str:
    developer = Path(os.environ.get("DEVELOPER_DIR", str(DEFAULT_DEVELOPER_DIR)))
    return str(developer / "usr" / "bin" / name)


def validate_macos_bundle(path: Path, *, expected_bundle_id: str = YOLO_TRADE_BUNDLE_ID,
                          run=subprocess.run) -> BundleMetadata:
    metadata = bundle_metadata(path, platform="macos")
    if metadata.bundle_id != expected_bundle_id:
        raise AppleUpgradeError("候选 App 的 Bundle ID 与 YoloTrade 不一致，不能作为普通升级安装。")
    _run_checked(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path.resolve())],
        run=run, message="候选 App 签名验证失败。",
    )
    return metadata


def validate_ios_bundle(path: Path, *, expected_bundle_id: str = YOLO_TRADE_BUNDLE_ID,
                        run=subprocess.run) -> BundleMetadata:
    metadata = bundle_metadata(path, platform="ios")
    if metadata.bundle_id != expected_bundle_id:
        raise AppleUpgradeError("候选 iOS App 的 Bundle ID 与 YoloTrade 不一致，不能作为普通升级安装。")
    _run_checked(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path.resolve())],
        run=run, message="候选 iOS App 签名验证失败。",
    )
    return metadata


def device_apps(device: str, *, run=subprocess.run) -> list[DeviceApp]:
    descriptor, output = tempfile.mkstemp(prefix="yolo-device-apps.", suffix=".json")
    os.close(descriptor)
    try:
        _run_checked([
            _developer_tool("devicectl"), "device", "info", "apps", "--device", device,
            "--json-output", output, "--quiet",
        ], run=run, message="无法读取 iPhone 已安装 App。")
        try:
            payload = json.loads(Path(output).read_text(encoding="utf-8"))
            rows = payload["result"]["apps"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            raise AppleUpgradeError("iPhone App 列表格式无效。") from None
        if not isinstance(rows, list):
            raise AppleUpgradeError("iPhone App 列表格式无效。")
        result = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("bundleIdentifier"), str):
                raise AppleUpgradeError("iPhone App 列表包含无效记录。")
            result.append(DeviceApp(
                row["bundleIdentifier"],
                row.get("version") if isinstance(row.get("version"), str) else None,
                row.get("bundleVersion") if isinstance(row.get("bundleVersion"), str) else None,
            ))
        return result
    finally:
        Path(output).unlink(missing_ok=True)


def _install_ios_bundle(candidate: Path, device: str, *, run=subprocess.run) -> None:
    descriptor, output = tempfile.mkstemp(prefix="yolo-device-install.", suffix=".json")
    os.close(descriptor)
    try:
        _run_checked([
            _developer_tool("devicectl"), "device", "install", "app", "--device", device,
            str(candidate), "--json-output", output, "--quiet",
        ], run=run, message="iPhone App 安装失败。")
    finally:
        Path(output).unlink(missing_ok=True)


def replace_ios_app(candidate: Path, device: str, *, expected_bundle_id: str = YOLO_TRADE_BUNDLE_ID,
                    legacy_bundle_ids: tuple[str, ...] = ("com.yoloagents.tradingagent",),
                    require_existing: bool = True, rollback_candidate: Path | None = None,
                    run=subprocess.run) -> IOSReplacement:
    candidate = _regular_directory(candidate, "候选 iOS App")
    metadata = validate_ios_bundle(candidate, expected_bundle_id=expected_bundle_id, run=run)
    installed = device_apps(device, run=run)
    installed_ids = {item.bundle_id for item in installed}
    legacy = sorted(installed_ids.intersection(legacy_bundle_ids))
    if legacy:
        raise AppleUpgradeError("iPhone 仍安装旧身份 App；这是身份迁移，不能作为普通覆盖升级。")
    existing = expected_bundle_id in installed_ids
    if require_existing and not existing:
        raise AppleUpgradeError("iPhone 未安装当前身份的 YoloTrade，不能执行升级替换测试。")
    rollback = None
    if rollback_candidate is not None:
        rollback = _regular_directory(rollback_candidate, "回滚 iOS App")
        validate_ios_bundle(rollback, expected_bundle_id=expected_bundle_id, run=run)
    _install_ios_bundle(candidate, device, run=run)
    final = next((item for item in device_apps(device, run=run) if item.bundle_id == expected_bundle_id), None)
    matches = final is not None
    if metadata.short_version is not None:
        matches = matches and final.short_version == metadata.short_version
    if metadata.build_version is not None:
        matches = matches and final.build_version == metadata.build_version
    if not matches:
        if rollback is not None:
            _install_ios_bundle(rollback, device, run=run)
        raise AppleUpgradeError("iPhone 安装后的版本校验失败。")
    return IOSReplacement(existing, metadata)


def _copy_macos_bundle(source: Path, destination: Path, *, run=subprocess.run) -> None:
    _run_checked(
        ["/usr/bin/ditto", "--rsrc", "--extattr", str(source), str(destination)],
        run=run, message="无法复制候选 App。",
    )


def replace_macos_app(candidate: Path, destination: Path, backup_root: Path, *,
                      expected_bundle_id: str = YOLO_TRADE_BUNDLE_ID,
                      run=subprocess.run, replace=os.replace) -> MacReplacement:
    candidate = _regular_directory(candidate, "候选 App")
    destination = destination.expanduser().absolute()
    backup_root = backup_root.expanduser().absolute()
    if destination.is_symlink() or backup_root.is_symlink():
        raise AppleUpgradeError("安装路径或备份路径不能是符号链接。")
    candidate_metadata = validate_macos_bundle(candidate, expected_bundle_id=expected_bundle_id, run=run)
    if destination.exists():
        current = bundle_metadata(destination, platform="macos")
        if current.bundle_id != expected_bundle_id:
            raise AppleUpgradeError("安装位置已有另一个应用，不能覆盖。")
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_root.chmod(0o700)
    nonce = uuid4().hex
    staging = destination.parent / f".{destination.name}.installing-{nonce}"
    backup = backup_root / f"{destination.stem}-before-{nonce}.app" if destination.exists() else None
    failed = backup_root / f"{destination.stem}-failed-{nonce}.app"
    if staging.exists() or (backup is not None and backup.exists()) or failed.exists():
        raise AppleUpgradeError("安装临时路径发生冲突。")
    try:
        _copy_macos_bundle(candidate, staging, run=run)
        validate_macos_bundle(staging, expected_bundle_id=expected_bundle_id, run=run)
        if backup is not None:
            replace(destination, backup)
        try:
            replace(staging, destination)
            installed = validate_macos_bundle(destination, expected_bundle_id=expected_bundle_id, run=run)
        except Exception:
            if destination.exists():
                replace(destination, failed)
            if backup is not None and backup.exists():
                replace(backup, destination)
            raise
        return MacReplacement(str(destination), str(backup) if backup is not None else None, installed)
    except AppleUpgradeError:
        raise
    except Exception:
        raise AppleUpgradeError("App 替换失败；旧版本已恢复或保留。") from None
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def restore_macos_app(backup: Path, destination: Path, backup_root: Path, *,
                      expected_bundle_id: str = YOLO_TRADE_BUNDLE_ID,
                      run=subprocess.run, replace=os.replace) -> MacReplacement:
    backup = _regular_directory(backup, "备份 App")
    destination = destination.expanduser().absolute()
    backup_root = backup_root.expanduser().absolute()
    metadata = validate_macos_bundle(backup, expected_bundle_id=expected_bundle_id, run=run)
    displaced = None
    if destination.exists():
        current = bundle_metadata(destination, platform="macos")
        if current.bundle_id != expected_bundle_id:
            raise AppleUpgradeError("安装位置已有另一个应用，不能恢复覆盖。")
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        displaced = backup_root / f"{destination.stem}-replaced-{uuid4().hex}.app"
        replace(destination, displaced)
    try:
        replace(backup, destination)
    except Exception:
        if displaced is not None and displaced.exists():
            replace(displaced, destination)
        raise AppleUpgradeError("恢复旧版 App 失败。") from None
    validate_macos_bundle(destination, expected_bundle_id=expected_bundle_id, run=run)
    return MacReplacement(str(destination), str(displaced) if displaced is not None else None, metadata)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install-macos")
    install.add_argument("--candidate", type=Path, required=True)
    install.add_argument("--destination", type=Path, required=True)
    install.add_argument("--backup-root", type=Path, required=True)
    restore = commands.add_parser("restore-macos")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--backup-root", type=Path, required=True)
    ios = commands.add_parser("install-ios")
    ios.add_argument("--candidate", type=Path, required=True)
    ios.add_argument("--device", required=True)
    ios.add_argument("--rollback-candidate", type=Path)
    ios.add_argument("--allow-fresh-install", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "install-macos":
            result = replace_macos_app(args.candidate, args.destination, args.backup_root)
        elif args.command == "restore-macos":
            result = restore_macos_app(args.backup, args.destination, args.backup_root)
        else:
            result = replace_ios_app(
                args.candidate, args.device, rollback_candidate=args.rollback_candidate,
                require_existing=not args.allow_fresh_install,
            )
        print(json.dumps({**asdict(result), "metadata": asdict(result.metadata)}, ensure_ascii=False, sort_keys=True))
        return 0
    except AppleUpgradeError as error:
        print(str(error), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
