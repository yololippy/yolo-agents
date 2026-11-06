#!/usr/bin/env python3
"""Deterministic local runner for trusted yolo-agents pull requests.

The runner polls GitHub's public REST API, reserves at most two attempts per
pull request before executing anything, and reuses the existing frozen PR gate
and real YoloTrade UI runner. It never repairs code or merges a pull request.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid


MAX_ATTEMPTS = 2
TOTAL_DEADLINE_SECONDS = 1800
MONITOR_INTERVAL_SECONDS = 60
CONFIG_KEYS = frozenset({
    "repository", "trusted_author", "root", "python", "product",
    "credentials_data_dir", "state_dir", "pull_requests",
})
SHA = re.compile(r"[0-9a-f]{40}\Z")
BRANCH = re.compile(r"(?:feat|fix|refactor|test|docs)/[A-Za-z0-9_.\-/]+\Z")
LABEL = "com.yolo-agents.pr-runner"


class RunnerError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Superseded(RunnerError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise RunnerError("unsafe_state_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RunnerError("unsafe_state_directory")
    return path.resolve()


def read_json(path: Path, *, private: bool = False) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RunnerError("invalid_json_file")
    metadata = path.stat()
    if private and (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise RunnerError("unsafe_private_file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise RunnerError("invalid_json_file") from None
    if not isinstance(value, dict):
        raise RunnerError("invalid_json_file")
    return value


def atomic_json(path: Path, value: dict) -> None:
    private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_scan(state_dir: Path):
    path = private_directory(state_dir) / "scan.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunnerError("scan_already_running") from None
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class Config:
    repository: str
    trusted_author: str
    root: Path
    python: Path
    product: str
    credentials_data_dir: Path
    state_dir: Path
    pull_requests: tuple[int, ...]


def load_config(path: Path) -> Config:
    value = read_json(path, private=True)
    if set(value) != CONFIG_KEYS:
        raise RunnerError("invalid_config_fields")
    if not isinstance(value["repository"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise RunnerError("invalid_repository")
    if not isinstance(value["trusted_author"], str) or not re.fullmatch(r"[A-Za-z0-9-]+", value["trusted_author"]):
        raise RunnerError("invalid_trusted_author")
    requests = value["pull_requests"]
    if (not isinstance(requests, list) or any(type(item) is not int or item <= 0 for item in requests)
            or len(set(requests)) != len(requests)):
        raise RunnerError("invalid_pull_requests")
    paths = {}
    for key in ("root", "python", "credentials_data_dir", "state_dir"):
        item = value[key]
        if not isinstance(item, str) or not Path(item).is_absolute():
            raise RunnerError("config_paths_must_be_absolute")
        paths[key] = Path(item).resolve()
    if value["product"] != "yolo-trade":
        raise RunnerError("unsupported_product")
    if not paths["root"].is_dir() or not paths["credentials_data_dir"].is_dir():
        raise RunnerError("configured_directory_missing")
    if not paths["python"].is_file() or not os.access(paths["python"], os.X_OK):
        raise RunnerError("configured_python_unavailable")
    if paths["state_dir"].is_relative_to(paths["root"]):
        raise RunnerError("state_directory_inside_candidate_checkout")
    private_directory(paths["state_dir"])
    return Config(value["repository"], value["trusted_author"], paths["root"], paths["python"],
                  value["product"], paths["credentials_data_dir"], paths["state_dir"], tuple(requests))


@dataclass(frozen=True)
class Candidate:
    number: int
    author: str
    repository: str
    head_repository: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    state: str
    draft: bool

    def metadata(self) -> dict:
        return {
            "repository": self.repository, "pr_number": self.number,
            "base_ref": self.base_ref, "base_sha": self.base_sha,
            "head_ref": self.head_ref, "head_sha": self.head_sha,
            "state": self.state, "draft": self.draft,
        }


def candidate_from_github(value: dict) -> Candidate:
    try:
        candidate = Candidate(
            number=value["number"], author=value["user"]["login"],
            repository=value["base"]["repo"]["full_name"],
            head_repository=value["head"]["repo"]["full_name"],
            base_ref=value["base"]["ref"], base_sha=value["base"]["sha"],
            head_ref=value["head"]["ref"], head_sha=value["head"]["sha"],
            state=value["state"], draft=value["draft"],
        )
    except (KeyError, TypeError):
        raise RunnerError("invalid_github_response") from None
    if (type(candidate.number) is not int or candidate.number <= 0
            or any(not isinstance(item, str) for item in (
                candidate.author, candidate.repository, candidate.head_repository,
                candidate.base_ref, candidate.base_sha, candidate.head_ref, candidate.head_sha, candidate.state))
            or type(candidate.draft) is not bool or not SHA.fullmatch(candidate.base_sha)
            or not SHA.fullmatch(candidate.head_sha)):
        raise RunnerError("invalid_github_response")
    return candidate


class GitHub:
    def __init__(self, repository: str, *, opener=urlopen):
        self.repository = repository
        self.opener = opener

    def _get(self, suffix: str):
        url = "https://api.github.com/repos/" + quote(self.repository, safe="/") + suffix
        request = Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "yolo-agents-pr-runner",
        })
        try:
            with self.opener(request, timeout=20) as response:
                value = json.load(response)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise RunnerError("github_unavailable") from None
        return value

    def pull_request(self, number: int) -> Candidate:
        return candidate_from_github(self._get(f"/pulls/{number}"))

    def pull_requests(self, selected: tuple[int, ...]) -> list[Candidate]:
        if selected:
            return [self.pull_request(number) for number in selected]
        value = self._get("/pulls?state=open&per_page=100&sort=updated&direction=desc")
        if not isinstance(value, list):
            raise RunnerError("invalid_github_response")
        return [candidate_from_github(item) for item in value]


def trusted(candidate: Candidate, config: Config) -> bool:
    return (
        candidate.repository.lower() == config.repository.lower()
        and candidate.head_repository.lower() == config.repository.lower()
        and candidate.author == config.trusted_author
        and candidate.base_ref == "master"
        and candidate.state == "open"
        and candidate.draft is False
        and BRANCH.fullmatch(candidate.head_ref) is not None
    )


def git(root: Path, *arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(["git", *arguments], cwd=root, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if result.returncode:
        raise RunnerError("git_operation_failed")
    return result.stdout.decode("utf-8").strip()


def load_gate(source: Path):
    path = source / "bin/yolo_agents_pr_gate.py"
    name = "yolo_agents_pr_gate_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RunnerError("gate_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise RunnerError("gate_unavailable") from None
    return module


def process_status(processes: list[subprocess.Popen]) -> bool:
    return all(process.poll() is not None for process in processes)


def stop_processes(processes: list[subprocess.Popen], first_signal: int = signal.SIGINT) -> None:
    active = [process for process in processes if process.poll() is None]
    for process in active:
        try:
            os.killpg(process.pid, first_signal)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while active and time.monotonic() < deadline:
        active = [process for process in active if process.poll() is None]
        if active:
            time.sleep(0.1)
    for process in active:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for process in processes:
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass


class Engine:
    def __init__(self, github: GitHub, *, clock=time.monotonic, sleep=time.sleep):
        self.github = github
        self.clock = clock
        self.sleep = sleep
        self.active: list[subprocess.Popen] = []

    def _current(self, expected: Candidate) -> None:
        current = self.github.pull_request(expected.number)
        if current.metadata() != expected.metadata():
            raise Superseded("pull_request_changed")

    def _wait(self, processes: list[subprocess.Popen], expected: Candidate, deadline: float) -> None:
        next_check = self.clock() + MONITOR_INTERVAL_SECONDS
        try:
            while not process_status(processes):
                current = self.clock()
                if current >= deadline:
                    raise RunnerError("attempt_deadline_exceeded")
                if current >= next_check:
                    self._current(expected)
                    next_check = current + MONITOR_INTERVAL_SECONDS
                self.sleep(0.2)
        except (RunnerError, KeyboardInterrupt):
            stop_processes(processes)
            raise

    @staticmethod
    def _spawn(command: list[str], cwd: Path, log: Path) -> subprocess.Popen:
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        output = os.fdopen(descriptor, "wb")
        try:
            process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        except OSError:
            output.close()
            raise RunnerError("command_unavailable") from None
        output.close()
        return process

    def _source(self, config: Config, candidate: Candidate, attempt_dir: Path) -> Path:
        git(config.root, "fetch", "origin", "master", f"refs/pull/{candidate.number}/head:refs/pull/{candidate.number}/head")
        if (git(config.root, "rev-parse", "refs/remotes/origin/master") != candidate.base_sha
                or git(config.root, "rev-parse", f"refs/pull/{candidate.number}/head") != candidate.head_sha):
            raise Superseded("fetched_revision_changed")
        source = attempt_dir / "source"
        git(config.root, "worktree", "add", "--detach", str(source), candidate.head_sha)
        if git(source, "status", "--porcelain=v1", "--untracked-files=all"):
            raise RunnerError("source_worktree_not_clean")
        return source

    def run(self, config: Config, candidate: Candidate, attempt_dir: Path) -> dict:
        deadline = self.clock() + TOTAL_DEADLINE_SECONDS
        private_directory(attempt_dir)
        metadata_path = attempt_dir / "metadata.json"
        atomic_json(metadata_path, candidate.metadata())
        source = None
        gate = None
        run_dir = None
        result = {"status": "failed", "reason": "attempt_failed", "merge_ready": False}
        try:
            self._current(candidate)
            source = self._source(config, candidate, attempt_dir)
            gate = load_gate(source)
            prepared = gate.prepare(source, metadata_path, config.product)
            run_dir = Path(prepared["run_dir"])
            commands = [
                [str(source / "bin/yolo-agents-pr-gate"), "--root", str(source), "run-group",
                 "--run-dir", str(run_dir), "--group", group]
                for group in gate.GROUPS
            ]
            logs = attempt_dir / "controller-logs"
            private_directory(logs)
            self.active = [self._spawn(command, source, logs / f"{group}.log")
                           for command, group in zip(commands, gate.GROUPS)]
            self._wait(self.active, candidate, deadline)
            self.active = []
            manifest, key = gate.load_manifest(run_dir, source)
            for group in gate.GROUPS:
                report = gate.read_json(run_dir / group / "report.json")
                gate.validate_group(report, group, manifest, run_dir, key)
            self._current(candidate)
            ui_command = [
                str(config.python), str(source / "bin/test_yolo_trade_pr_ui.py"),
                "--run-dir", str(run_dir / "ui"), "--run-id", manifest["run_id"],
                "--head-sha", candidate.head_sha, "--base-sha", candidate.base_sha,
                "--python", str(config.python), "--credentials-data-dir", str(config.credentials_data_dir),
            ]
            self.active = [self._spawn(ui_command, source, logs / "ui.log")]
            self._wait(self.active, candidate, deadline)
            ui_code = self.active[0].returncode
            self.active = []
            if ui_code != 0:
                report = gate.read_json(run_dir / "ui/report.json")
                status = report.get("status") if report.get("status") in ("failed", "blocked") else "failed"
                reason = next((item.get("reason") for item in report.get("checks", [])
                               if isinstance(item, dict) and item.get("status") in ("failed", "blocked")), None)
                raise RunnerError(reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]+", reason) else status)
            current = self.github.pull_request(candidate.number)
            if current.metadata() != candidate.metadata():
                raise Superseded("pull_request_changed")
            git(config.root, "fetch", "origin", "master", f"refs/pull/{candidate.number}/head:refs/pull/{candidate.number}/head")
            atomic_json(metadata_path, current.metadata())
            summary = gate.summary(source, run_dir, metadata_path, run_dir / "ui/report.json")
            result = {
                "status": "passed" if summary.get("merge_ready") is True else "failed",
                "reason": "passed" if summary.get("merge_ready") is True else "summary_not_ready",
                "merge_ready": summary.get("merge_ready") is True,
                "run_dir": str(run_dir),
            }
        except Superseded as error:
            result = {"status": "superseded", "reason": error.reason, "merge_ready": False}
        except RunnerError as error:
            result = {"status": "failed", "reason": error.reason, "merge_ready": False}
        except Exception:
            result = {"status": "failed", "reason": "attempt_failed", "merge_ready": False}
        finally:
            if self.active:
                stop_processes(self.active)
                self.active = []
            if gate is not None and run_dir is not None:
                try:
                    gate.cleanup(source, run_dir)
                except Exception:
                    result["cleanup"] = "incomplete"
            if source is not None and source.exists():
                try:
                    if git(source, "status", "--porcelain=v1", "--untracked-files=all"):
                        result["cleanup"] = "incomplete"
                    else:
                        git(config.root, "worktree", "remove", str(source))
                except RunnerError:
                    result["cleanup"] = "incomplete"
        return result


def ledger_path(config: Config, number: int) -> Path:
    return config.state_dir / f"pr-{number}.json"


def load_ledger(config: Config, candidate: Candidate) -> dict:
    path = ledger_path(config, candidate.number)
    if not path.exists():
        return {"schema_version": 1, "repository": config.repository, "pr_number": candidate.number,
                "attempts": [], "status": "new", "updated_at": now()}
    value = read_json(path, private=True)
    if (value.get("schema_version") != 1 or value.get("repository") != config.repository
            or value.get("pr_number") != candidate.number or not isinstance(value.get("attempts"), list)
            or len(value["attempts"]) > MAX_ATTEMPTS):
        raise RunnerError("invalid_ledger")
    return value


def reserve(config: Config, candidate: Candidate, ledger: dict) -> tuple[dict, dict] | None:
    attempts = ledger["attempts"]
    if any(item.get("head_sha") == candidate.head_sha for item in attempts if isinstance(item, dict)):
        return None
    if len(attempts) >= MAX_ATTEMPTS:
        ledger.update(status="exhausted", updated_at=now())
        atomic_json(ledger_path(config, candidate.number), ledger)
        return None
    number = len(attempts) + 1
    attempt = {
        "number": number, "head_sha": candidate.head_sha, "base_sha": candidate.base_sha,
        "reserved_at": now(), "finished_at": None, "status": "running",
        "reason": "reserved", "merge_ready": False,
    }
    attempts.append(attempt)
    ledger.update(status="running", updated_at=now())
    atomic_json(ledger_path(config, candidate.number), ledger)
    return ledger, attempt


def scan(config: Config, *, github: GitHub | None = None,
         engine_factory: Callable[[GitHub], Engine] = Engine) -> dict:
    github = github or GitHub(config.repository)
    results = []
    with exclusive_scan(config.state_dir):
        candidates = github.pull_requests(config.pull_requests)
        for candidate in candidates:
            if not trusted(candidate, config):
                results.append({"pr_number": candidate.number, "status": "rejected"})
                continue
            ledger = load_ledger(config, candidate)
            reserved = reserve(config, candidate, ledger)
            if reserved is None:
                results.append({"pr_number": candidate.number, "status": ledger["status"]})
                continue
            ledger, attempt = reserved
            attempt_dir = private_directory(config.state_dir / "attempts" / f"pr-{candidate.number}-round-{attempt['number']}")
            engine = engine_factory(github)
            try:
                outcome = engine.run(config, candidate, attempt_dir)
            except KeyboardInterrupt:
                outcome = {"status": "interrupted", "reason": "controller_interrupted", "merge_ready": False}
            attempt.update({
                "finished_at": now(), "status": outcome.get("status", "failed"),
                "reason": outcome.get("reason", "attempt_failed"),
                "merge_ready": outcome.get("merge_ready") is True,
            })
            if isinstance(outcome.get("run_dir"), str):
                attempt["run_dir"] = outcome["run_dir"]
            if outcome.get("cleanup") == "incomplete":
                attempt["cleanup"] = "incomplete"
            if attempt["merge_ready"]:
                ledger["status"] = "passed"
            elif len(ledger["attempts"]) >= MAX_ATTEMPTS:
                ledger["status"] = "exhausted"
            else:
                ledger["status"] = "awaiting_new_head"
            ledger["updated_at"] = now()
            atomic_json(ledger_path(config, candidate.number), ledger)
            results.append({"pr_number": candidate.number, "round": attempt["number"],
                            "status": attempt["status"], "reason": attempt["reason"],
                            "merge_ready": attempt["merge_ready"], "ledger_status": ledger["status"]})
    return {"status": "completed", "results": results}


def status(config: Config) -> dict:
    ledgers = []
    if config.pull_requests:
        numbers = config.pull_requests
    else:
        numbers = tuple(sorted(int(path.stem.removeprefix("pr-")) for path in config.state_dir.glob("pr-*.json")
                               if path.stem.removeprefix("pr-").isdigit()))
    for number in numbers:
        path = ledger_path(config, number)
        ledgers.append(read_json(path, private=True) if path.exists() else {
            "schema_version": 1, "repository": config.repository, "pr_number": number,
            "attempts": [], "status": "new",
        })
    return {"status": "ok", "repository": config.repository, "pull_requests": ledgers}


def launch_agent_payload(config_path: Path, script: Path, python: Path, state_dir: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script), "scan", "--config", str(config_path)],
        "RunAtLoad": True,
        "StartInterval": 120,
        "ProcessType": "Background",
        "StandardOutPath": str(state_dir / "launch-agent.log"),
        "StandardErrorPath": str(state_dir / "launch-agent-error.log"),
    }


def install_launch_agent(config_path: Path, config: Config, *, home: Path | None = None,
                         run=subprocess.run) -> dict:
    home = home or Path.home()
    destination = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    payload = plistlib.dumps(launch_agent_payload(config_path.resolve(), script, config.python, config.state_dir), sort_keys=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
    os.replace(temporary, destination)
    destination.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    run(["/bin/launchctl", "bootout", domain, str(destination)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    loaded = run(["/bin/launchctl", "bootstrap", domain, str(destination)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if loaded.returncode:
        raise RunnerError("launch_agent_install_failed")
    return {"status": "installed", "label": LABEL, "plist": str(destination)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("scan", "status", "install-launch-agent"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.action == "scan":
            previous_term = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda _signum, _frame: (_ for _ in ()).throw(KeyboardInterrupt()))
            value = scan(config)
            signal.signal(signal.SIGTERM, previous_term)
        elif args.action == "status":
            value = status(config)
        else:
            value = install_launch_agent(args.config, config)
    except RunnerError as error:
        value = {"status": "blocked", "reason": error.reason}
    except KeyboardInterrupt:
        value = {"status": "interrupted", "reason": "controller_interrupted"}
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0 if value["status"] in ("completed", "ok", "installed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
