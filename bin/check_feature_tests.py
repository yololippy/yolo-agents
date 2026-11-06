#!/usr/bin/env python3
"""Require production changes in an explicit PR diff to include owning tests."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import re
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
        if name.endswith(".py") or name in ("check-feature-tests", "yolo-agents-full-test", "yolo-agents-pr-gate"):
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
        if not any(path.startswith(scope.test_prefixes) and path.endswith((".py", ".swift")) for path in eligible):
            missing.append(MissingTests(scope, tuple(sorted(production_paths))))
    return sorted(missing, key=lambda item: item.scope.name)


def pr_paths(base: str, head: str, diff_filter: str, *, root: Path = ROOT) -> tuple[str, ...]:
    """Inspect the entire frozen PR, independent of the checkout and index.

    Disable rename detection: a production rename includes its deleted origin,
    and deleting a test never counts as adding coverage in the destination.
    """
    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", sha) for sha in (base, head)):
        raise RuntimeError("PR 模式要求 --base 和 --head 使用完整 40 位提交 SHA。")
    for sha in (base, head):
        verified = subprocess.run(
            ["git", "cat-file", "-t", sha], cwd=root, capture_output=True, check=False,
        )
        if verified.returncode or verified.stdout.strip() != b"commit":
            raise RuntimeError("PR 提交不存在或不是 commit；请先 fetch 指定 base/head。")
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"--diff-filter={diff_filter}", "-z", f"{base}...{head}", "--"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("无法读取 PR base/head 差异。")
    return tuple(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="PR base 的完整 40 位 SHA（仅支持显式 PR 模式）")
    parser.add_argument("--head", required=True, help="PR head 的完整 40 位 SHA（不读取暂存区）")
    args = parser.parse_args(argv)
    try:
        changed = pr_paths(args.base, args.head, "ACDMT")
        non_deleted = pr_paths(args.base, args.head, "ACMT")
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
