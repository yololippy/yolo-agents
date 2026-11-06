"""Isolated old-to-new Apple upgrade exercises for the full-test workflow."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from .apple_upgrade import (
    AppleUpgradeError,
    DEFAULT_DEVELOPER_DIR,
    YOLO_TRADE_BUNDLE_ID,
    bundle_metadata,
    replace_macos_app,
    validate_ios_bundle,
)


class UpgradeTestError(RuntimeError):
    pass


def tree_fingerprint(root: Path) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root)).encode()
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
    return digest.hexdigest()


def bundle_executable_fingerprint(app: Path, *, platform: str) -> str:
    metadata = bundle_metadata(app, platform=platform)
    executable = app / ("Contents/MacOS" if platform == "macos" else "") / metadata.executable
    digest = hashlib.sha256()
    with executable.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def exercise_macos_upgrade(baseline: Path, candidate: Path, workspace: Path, *,
                           run=subprocess.run) -> dict[str, object]:
    destination = workspace / "Applications/YoloTrade.app"
    backup_root = workspace / "backups"
    first = replace_macos_app(baseline, destination, backup_root, run=run)
    sentinel = destination / "Contents/Resources/full-test-old-version-sentinel"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("old version residue", encoding="utf-8")
    second = replace_macos_app(candidate, destination, backup_root, run=run)
    if sentinel.exists():
        raise UpgradeTestError("Mac 升级把旧版资源合并进了候选 App。")
    if tree_fingerprint(destination) != tree_fingerprint(candidate):
        raise UpgradeTestError("Mac 升级后的 App 与候选产物不一致。")
    return {
        "status": "passed",
        "baseline_version": first.metadata.short_version,
        "candidate_version": second.metadata.short_version,
        "backup_created": second.backup is not None,
    }


def _capture(command: list[str], *, run=subprocess.run, message: str) -> bytes:
    try:
        result = run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError:
        raise UpgradeTestError(message) from None
    if result.returncode != 0:
        raise UpgradeTestError(message)
    return result.stdout


def _simctl() -> list[str]:
    developer = Path(os.environ.get("DEVELOPER_DIR", str(DEFAULT_DEVELOPER_DIR)))
    return [str(developer / "usr/bin/simctl")]


def _version_key(value: object) -> tuple[int, ...]:
    if not isinstance(value, str):
        return (0, 0, 0)
    parts = [int(part) for part in re.findall(r"\d+", value)[:3]]
    return tuple((parts + [0, 0, 0])[:3])


def select_simulator(run=subprocess.run) -> tuple[str, str]:
    try:
        runtimes = json.loads(_capture(_simctl() + ["list", "runtimes", "-j"], run=run,
                                       message="无法读取 iOS Simulator runtime。"))
        device_types = json.loads(_capture(_simctl() + ["list", "devicetypes", "-j"], run=run,
                                           message="无法读取 iPhone Simulator 类型。"))
        available = [item for item in runtimes["runtimes"]
                     if item.get("isAvailable") is True and "iOS" in item.get("identifier", "")]
        phones = [item for item in device_types["devicetypes"]
                  if item.get("productFamily") == "iPhone"
                  and isinstance(item.get("identifier"), str)]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise UpgradeTestError("Simulator 清单格式无效。") from None
    if not available or not phones:
        raise UpgradeTestError("没有可用的 iOS runtime 或 iPhone Simulator 类型。")
    runtime = max(available, key=lambda item: _version_key(item.get("version")))
    runtime_version = _version_key(runtime.get("version"))
    compatible = [
        item for item in phones
        if _version_key(item.get("minRuntimeVersionString")) <= runtime_version
        <= _version_key(item.get("maxRuntimeVersionString", "65535.255.255"))
    ]
    if not compatible:
        raise UpgradeTestError("没有与最新 iOS runtime 兼容的 iPhone Simulator 类型。")
    device_type = max(
        compatible,
        key=lambda item: (_version_key(item.get("minRuntimeVersionString")), item["identifier"]),
    )
    return runtime["identifier"], device_type["identifier"]


def exercise_ios_simulator_upgrade(baseline: Path, candidate: Path, workspace: Path, *,
                                   run=subprocess.run) -> dict[str, object]:
    try:
        baseline_metadata = validate_ios_bundle(baseline, run=run)
        candidate_metadata = validate_ios_bundle(candidate, run=run)
    except AppleUpgradeError as error:
        raise UpgradeTestError(str(error)) from None
    runtime, device_type = select_simulator(run=run)
    name = f"YoloAgents Full Test {uuid4().hex[:8]}"
    device = _capture(_simctl() + ["create", name, device_type, runtime], run=run,
                      message="无法创建隔离 iPhone Simulator。").decode().strip()
    if not device:
        raise UpgradeTestError("创建 Simulator 后没有获得设备标识。")
    marker_name = ".yolo-agents-full-test-preserved"
    try:
        _capture(_simctl() + ["boot", device], run=run, message="无法启动隔离 iPhone Simulator。")
        _capture(_simctl() + ["bootstatus", device, "-b"], run=run, message="隔离 iPhone Simulator 未完成启动。")
        _capture(_simctl() + ["install", device, str(baseline.resolve())], run=run,
                 message="无法在隔离 Simulator 安装旧版 App。")
        data_before = Path(_capture(
            _simctl() + ["get_app_container", device, YOLO_TRADE_BUNDLE_ID, "data"], run=run,
            message="无法取得旧版 App 数据容器。",
        ).decode().strip())
        if not data_before.is_dir():
            raise UpgradeTestError("旧版 App 数据容器不存在。")
        (data_before / marker_name).write_text("preserve across upgrade", encoding="utf-8")
        _capture(_simctl() + ["install", device, str(candidate.resolve())], run=run,
                 message="无法在隔离 Simulator 覆盖安装候选 App。")
        data_after = Path(_capture(
            _simctl() + ["get_app_container", device, YOLO_TRADE_BUNDLE_ID, "data"], run=run,
            message="无法取得升级后的 App 数据容器。",
        ).decode().strip())
        app_after = Path(_capture(
            _simctl() + ["get_app_container", device, YOLO_TRADE_BUNDLE_ID, "app"], run=run,
            message="无法取得升级后的 App 包。",
        ).decode().strip())
        if (data_after / marker_name).read_text(encoding="utf-8") != "preserve across upgrade":
            raise UpgradeTestError("iOS 覆盖升级没有保留既有数据容器。")
        installed = bundle_metadata(app_after, platform="ios")
        if (installed.bundle_id != candidate_metadata.bundle_id
                or installed.short_version != candidate_metadata.short_version
                or installed.build_version != candidate_metadata.build_version):
            raise UpgradeTestError("Simulator 中的升级结果不是候选版本。")
        if bundle_executable_fingerprint(app_after, platform="ios") != bundle_executable_fingerprint(
            candidate, platform="ios",
        ):
            raise UpgradeTestError("Simulator 中的主程序不是候选产物。")
        return {
            "status": "passed",
            "baseline_version": baseline_metadata.short_version,
            "candidate_version": candidate_metadata.short_version,
            "data_preserved": True,
            "data_container_reused": data_before == data_after,
        }
    except (OSError, UnicodeError) as error:
        raise UpgradeTestError("读取隔离升级结果失败。") from error
    finally:
        run(_simctl() + ["shutdown", device], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        run(_simctl() + ["delete", device], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
