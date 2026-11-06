#!/usr/bin/env python3
"""Require staged production changes to include tests in the same code scope."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TestScope:
    name: str
    test_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class MissingTests:
    scope: TestScope
    production_paths: tuple[str, ...]


def _parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def _under(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return parts[:len(prefix)] == prefix


def scope_for_path(path: str) -> TestScope | None:
    """Map behavior-bearing repository files to their owning automated tests."""
    parts = _parts(path)
    if not parts or "Tests" in parts or "tests" in parts:
        return None

    if _under(parts, ("packages", "core-python", "yolo_agents")) and path.endswith(".py"):
        return TestScope("packages/core-python", ("packages/core-python/tests/",))

    if len(parts) >= 4 and parts[0] == "agents" and parts[2] == "runtime-python":
        agent = parts[1]
        if path.endswith((".py", ".json")):
            return TestScope(
                f"agents/{agent}/runtime-python",
                (f"agents/{agent}/runtime-python/tests/",),
            )

    if len(parts) >= 3 and parts[0] == "packages":
        package = parts[1]
        if (parts[2] == "Sources" and path.endswith(".swift")) or parts[2] == "Package.swift":
            return TestScope(f"packages/{package}", (f"packages/{package}/Tests/",))

    if len(parts) >= 4 and parts[0] == "apps" and parts[2] == "client-swift":
        agent = parts[1]
        if (parts[3] == "Sources" and path.endswith(".swift")) or parts[3] == "Package.swift":
            return TestScope(
                f"apps/{agent}/client-swift",
                (f"apps/{agent}/client-swift/Tests/",),
            )

    if (len(parts) >= 5 and parts[0] == "apps" and parts[2] in ("ios", "macos")
            and parts[3] == "Sources" and path.endswith(".swift")):
        agent = parts[1]
        return TestScope(
            f"apps/{agent}/platform-entry",
            (f"apps/{agent}/client-swift/Tests/",),
        )

    if len(parts) == 2 and parts[0] == "bin" and not parts[1].startswith("test-"):
        name = parts[1]
        if name.startswith(("yolo-trade-", "yolo-ibkr-", "install-ibc-")):
            return TestScope(
                "YoloTrade command entrypoints",
                ("agents/yolo-trade/runtime-python/tests/",),
            )
        if name.endswith(".py") or name in ("check-feature-tests", "yolo-agents-full-test"):
            return TestScope("repository command tooling", ("bin/tests/",))

    return None


def missing_test_scopes(
    changed_paths: Iterable[str],
    eligible_test_paths: Iterable[str] | None = None,
) -> list[MissingTests]:
    """Return each changed production scope lacking a non-deleted test change."""
    changed = tuple(dict.fromkeys(changed_paths))
    eligible = tuple(dict.fromkeys(changed if eligible_test_paths is None else eligible_test_paths))
    grouped: dict[TestScope, list[str]] = {}
    for path in changed:
        scope = scope_for_path(path)
        if scope is not None:
            grouped.setdefault(scope, []).append(path)

    missing = []
    for scope, production_paths in grouped.items():
        if not any(path.startswith(scope.test_prefixes) for path in eligible):
            missing.append(MissingTests(scope, tuple(sorted(production_paths))))
    return sorted(missing, key=lambda item: item.scope.name)


def _staged_paths(diff_filter: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--no-renames", f"--diff-filter={diff_filter}", "-z"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("无法读取暂存区。")
    return tuple(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def main() -> int:
    try:
        changed = _staged_paths("ACDMR")
        non_deleted = _staged_paths("ACMR")
    except (RuntimeError, UnicodeError) as error:
        print(f"功能测试门禁失败：{error}", file=sys.stderr)
        return 2

    missing = missing_test_scopes(changed, non_deleted)
    if missing:
        print("功能测试门禁失败：以下生产代码改动没有同范围的测试代码改动：", file=sys.stderr)
        for item in missing:
            print(f"- {item.scope.name}", file=sys.stderr)
            for path in item.production_paths:
                print(f"  生产代码：{path}", file=sys.stderr)
            print(f"  需要测试：{' 或 '.join(item.scope.test_prefixes)}", file=sys.stderr)
        return 1

    scopes = {scope_for_path(path) for path in changed}
    count = len(scopes - {None})
    print(f"功能测试门禁通过：已核对 {count} 个生产代码范围。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
