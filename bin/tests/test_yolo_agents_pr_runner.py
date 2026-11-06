"""Offline semantic tests for the two-round deterministic PR runner."""
from pathlib import Path
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yolo_agents_pr_runner as runner
import yolo_agents_pr_gate as gate


BASE = "a" * 40


def candidate(head="b" * 40, *, author="owner", head_repository="owner/repo", number=3):
    return runner.Candidate(number, author, "owner/repo", head_repository, "master", BASE,
                            "feat/example", head, "open", False)


class FakeGitHub:
    def __init__(self, current):
        self.current = current

    def pull_requests(self, selected):
        return [self.current] if not selected or self.current.number in selected else []

    def pull_request(self, number):
        assert number == self.current.number
        return self.current


class FakeEngine:
    def __init__(self, github, outcomes, calls):
        self.github = github
        self.outcomes = outcomes
        self.calls = calls

    def run(self, config, current, attempt_dir):
        self.calls.append((current.head_sha, attempt_dir.name))
        return self.outcomes.pop(0)


class ControlledProcess:
    def __init__(self, group):
        self.group = group
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class ControlledRun:
    """Release fake processes explicitly; validate real signed group reports."""

    def __init__(self, config, root, *, failed_group=None, report_fault=None,
                 apple_order=("mac", "simulator"), spawn_failure=None):
        self.config = config
        self.root = Path(tempfile.mkdtemp(dir=root))
        self.source = self.root / "source"
        self.source.mkdir()
        self.run_dir = self.root / "run"
        self.events = []
        self.commands = []
        self.processes = {}
        self.failed_group = failed_group
        self.report_fault = report_fault
        self.spawn_failure = spawn_failure
        self.completions = iter(("p0", *apple_order, "ui"))
        self.elapsed = 0
        self.key = b"k" * 32
        self.manifest = {"run_id": "1" * 32, "product": config.product,
                         "python": str(config.python), "metadata": candidate().metadata()}
        self.github = FakeGitHub(candidate())
        self.engine = runner.Engine(self.github, clock=lambda: self.elapsed, sleep=self.release)
        self.engine._source = Mock(return_value=self.source)
        self.engine._spawn = self.spawn
        self.gate = Mock(
            prepare=Mock(return_value={"run_dir": str(self.run_dir)}),
            load_manifest=Mock(return_value=(self.manifest, self.key)),
            read_json=Mock(side_effect=gate.read_json),
            validate_group=Mock(side_effect=self.validate),
            summary=Mock(side_effect=self.summary),
            cleanup=Mock(side_effect=lambda *_args: self.events.append(("cleanup",))),
        )

    def spawn(self, command, _cwd, _log):
        group = command[command.index("--group") + 1] if "--group" in command else "ui"
        if group == self.spawn_failure:
            raise runner.RunnerError("command_unavailable")
        self.commands.append(command)
        self.events.append(("spawn", group))
        process = ControlledProcess(group)
        self.processes[group] = process
        return process

    def release(self, _seconds):
        active = tuple(name for name, process in self.processes.items() if process.poll() is None)
        self.events.append(("wait", active))
        group = next(self.completions)
        process = self.processes[group]
        if process.poll() is not None:
            raise AssertionError("process already completed")
        self.elapsed += 1
        if group != "ui" and self.report_fault != (group, "missing"):
            self.write_report(group)
        process.returncode = 1 if group == self.failed_group else 0
        self.events.append(("complete", group))

    def write_report(self, group):
        checks = []
        for check in gate.checks_for(group, self.manifest, self.run_dir):
            log = f"{group}/logs/{check.name}.log"
            path = self.run_dir / log
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"controlled check output\n")
            checks.append({
                "name": check.name, "command": list(check.command), "status": "passed",
                "exit_code": 0, "reason": "", "timeout_seconds": check.timeout_seconds,
                "log": log, "log_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        report = gate.signed(dict(
            gate.identity(self.manifest), group=group,
            status="failed" if group == self.failed_group else "passed",
            dry_run=False, before_clean=True, after_clean=True, checks=checks,
        ), self.key)
        if self.report_fault == (group, "signature"):
            report["signature"] = "invalid private fixture detail"
        gate.write_json(self.run_dir / group / "report.json", report)

    def validate(self, report, group, manifest, run_dir, key):
        self.events.append(("validate", group))
        if self.processes[group].poll() is None:
            raise AssertionError("validation before process completion")
        gate.validate_group(report, group, manifest, run_dir, key)
        self.events.append(("verified", group))

    def summary(self, *_args):
        self.events.append(("summary",))
        return {"merge_ready": True}

    def stop(self, processes):
        for process in processes:
            if process.poll() is None:
                process.returncode = -1
                self.events.append(("stop", process.group))

    def run(self):
        with (patch.object(runner, "load_gate", return_value=self.gate),
              patch.object(runner, "git", return_value=""),
              patch.object(runner, "stop_processes", side_effect=self.stop)):
            return self.engine.run(self.config, candidate(), self.root / "attempt")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        checkout = root / "checkout"
        credentials = root / "credentials"
        state = root / "state"
        checkout.mkdir(); credentials.mkdir(); state.mkdir(mode=0o700)
        python = root / "python"
        python.symlink_to(sys.executable)
        self.config = runner.Config("owner/repo", "owner", checkout, python, "yolo-trade",
                                    credentials, state, (3,))

    def factory(self, outcomes, calls):
        return lambda github: FakeEngine(github, outcomes, calls)

    def test_same_head_is_deduplicated_and_new_heads_consume_exactly_two_rounds(self):
        github = FakeGitHub(candidate())
        calls = []
        outcomes = [
            {"status": "failed", "reason": "round_one", "merge_ready": False},
            {"status": "failed", "reason": "round_two", "merge_ready": False},
        ]
        first = runner.scan(self.config, github=github, engine_factory=self.factory(outcomes, calls))
        duplicate = runner.scan(self.config, github=github, engine_factory=self.factory(outcomes, calls))
        github.current = candidate("c" * 40)
        second = runner.scan(self.config, github=github, engine_factory=self.factory(outcomes, calls))
        github.current = candidate("d" * 40)
        exhausted = runner.scan(self.config, github=github, engine_factory=self.factory(outcomes, calls))
        self.assertEqual([item[0] for item in calls], ["b" * 40, "c" * 40])
        self.assertEqual(first["results"][0]["round"], 1)
        self.assertEqual(duplicate["results"][0]["status"], "awaiting_new_head")
        self.assertEqual(second["results"][0]["round"], 2)
        self.assertEqual(exhausted["results"][0]["status"], "exhausted")
        ledger = runner.read_json(runner.ledger_path(self.config, 3), private=True)
        self.assertEqual(len(ledger["attempts"]), runner.MAX_ATTEMPTS)
        self.assertEqual(ledger["status"], "exhausted")

    def test_interrupted_reserved_round_is_not_refunded_after_restart(self):
        github = FakeGitHub(candidate())
        calls = []
        runner.scan(self.config, github=github, engine_factory=self.factory([
            {"status": "interrupted", "reason": "controller_interrupted", "merge_ready": False},
        ], calls))
        reloaded = runner.Config(**self.config.__dict__)
        runner.scan(reloaded, github=github, engine_factory=self.factory([], calls))
        github.current = candidate("c" * 40)
        runner.scan(reloaded, github=github, engine_factory=self.factory([
            {"status": "passed", "reason": "passed", "merge_ready": True},
        ], calls))
        self.assertEqual(len(calls), 2)
        ledger = runner.read_json(runner.ledger_path(self.config, 3), private=True)
        self.assertEqual([item["status"] for item in ledger["attempts"]], ["interrupted", "passed"])
        self.assertEqual(ledger["status"], "passed")

    def test_fork_untrusted_author_draft_and_wrong_branch_are_rejected_without_execution(self):
        variants = [
            candidate(head_repository="fork/repo"),
            candidate(author="stranger"),
            runner.Candidate(3, "owner", "owner/repo", "owner/repo", "master", BASE,
                             "topic/example", "b" * 40, "open", False),
            runner.Candidate(3, "owner", "owner/repo", "owner/repo", "master", BASE,
                             "feat/example", "b" * 40, "open", True),
        ]
        for current in variants:
            with self.subTest(current=current):
                calls = []
                result = runner.scan(self.config, github=FakeGitHub(current),
                                     engine_factory=self.factory([], calls))
                self.assertEqual(result["results"][0]["status"], "rejected")
                self.assertEqual(calls, [])

    def test_scan_lock_prevents_double_reservation(self):
        with runner.exclusive_scan(self.config.state_dir):
            with self.assertRaisesRegex(runner.RunnerError, "scan_already_running"):
                runner.scan(self.config, github=FakeGitHub(candidate()),
                            engine_factory=self.factory([], []))
        self.assertFalse(runner.ledger_path(self.config, 3).exists())

    def test_status_is_read_only_and_never_starts_an_engine(self):
        before = set(self.config.state_dir.iterdir())
        value = runner.status(self.config)
        after = set(self.config.state_dir.iterdir())
        self.assertEqual(before, after)
        self.assertEqual(value["pull_requests"][0]["status"], "new")

    def test_atomic_ledger_is_private(self):
        path = self.config.state_dir / "fixture.json"
        runner.atomic_json(path, {"value": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"value": 1})

    def test_config_has_fixed_schema_and_cannot_raise_attempt_limit(self):
        path = Path(self.temporary.name) / "config.json"
        value = {
            "repository": "owner/repo", "trusted_author": "owner",
            "root": str(self.config.root), "python": str(self.config.python),
            "product": "yolo-trade", "credentials_data_dir": str(self.config.credentials_data_dir),
            "state_dir": str(self.config.state_dir), "pull_requests": [3],
        }
        path.write_text(json.dumps(value)); path.chmod(0o600)
        self.assertEqual(runner.load_config(path).pull_requests, (3,))
        path.write_text(json.dumps({**value, "max_attempts": 3}))
        with self.assertRaisesRegex(runner.RunnerError, "invalid_config_fields"):
            runner.load_config(path)

    def test_launch_agent_runs_scan_every_120_seconds(self):
        path = Path(self.temporary.name) / "config.json"
        payload = runner.launch_agent_payload(path, Path("/repo/bin/runner"), self.config.python,
                                              self.config.state_dir)
        self.assertEqual(payload["StartInterval"], 120)
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["ProgramArguments"][-3:], ["scan", "--config", str(path)])
        encoded = plistlib.loads(plistlib.dumps(payload))
        self.assertEqual(encoded["Label"], runner.LABEL)

    def test_github_shape_rejects_missing_or_non_sha_fields_without_leaking_payload(self):
        with self.assertRaisesRegex(runner.RunnerError, "invalid_github_response"):
            runner.candidate_from_github({"private": "fixture-secret"})
        raw = {
            "number": 3, "user": {"login": "owner"}, "state": "open", "draft": False,
            "base": {"ref": "master", "sha": BASE, "repo": {"full_name": "owner/repo"}},
            "head": {"ref": "feat/example", "sha": "short", "repo": {"full_name": "owner/repo"}},
        }
        with self.assertRaisesRegex(runner.RunnerError, "invalid_github_response") as caught:
            runner.candidate_from_github(raw)
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_engine_verifies_p0_then_runs_apple_groups_together_before_ui(self):
        for apple_order in (("mac", "simulator"), ("simulator", "mac")):
            with self.subTest(apple_order=apple_order):
                controlled = ControlledRun(self.config, self.temporary.name, apple_order=apple_order)
                result = controlled.run()
                self.assertTrue(result["merge_ready"], (result, controlled.events))
                self.assertEqual(controlled.events, [
                    ("spawn", "p0"), ("wait", ("p0",)), ("complete", "p0"),
                    ("validate", "p0"), ("verified", "p0"),
                    ("spawn", "mac"), ("spawn", "simulator"),
                    ("wait", ("mac", "simulator")), ("complete", apple_order[0]),
                    ("wait", (apple_order[1],)), ("complete", apple_order[1]),
                    ("validate", "mac"), ("verified", "mac"),
                    ("validate", "simulator"), ("verified", "simulator"),
                    ("spawn", "ui"), ("wait", ("ui",)), ("complete", "ui"),
                    ("summary",), ("cleanup",),
                ])
                self.assertEqual(controlled.engine.active, [])
                ui_command = controlled.commands[-1]
                self.assertEqual(ui_command[0], str(self.config.python))
                self.assertEqual(ui_command[1], str(controlled.source / "bin/test_yolo_trade_pr_ui.py"))
                for option, expected in (
                    ("--run-dir", str(controlled.run_dir / "ui")),
                    ("--run-id", controlled.manifest["run_id"]),
                    ("--head-sha", candidate().head_sha), ("--base-sha", BASE),
                    ("--python", str(self.config.python)),
                    ("--credentials-data-dir", str(self.config.credentials_data_dir)),
                ):
                    self.assertEqual(ui_command[ui_command.index(option) + 1], expected)

    def test_failed_signed_p0_blocks_all_apple_groups_and_ui(self):
        controlled = ControlledRun(self.config, self.temporary.name, failed_group="p0")
        result = controlled.run()
        self.assertEqual(result, {"status": "failed", "reason": "p0_failed", "merge_ready": False})
        self.assertEqual(controlled.events, [
            ("spawn", "p0"), ("wait", ("p0",)), ("complete", "p0"),
            ("validate", "p0"), ("cleanup",),
        ])
        controlled.gate.summary.assert_not_called()

    def test_failed_signed_apple_group_waits_for_both_and_blocks_ui(self):
        for failed_group in ("mac", "simulator"):
            with self.subTest(failed_group=failed_group):
                controlled = ControlledRun(self.config, self.temporary.name, failed_group=failed_group)
                result = controlled.run()
                self.assertEqual(result, {
                    "status": "failed", "reason": f"{failed_group}_failed", "merge_ready": False,
                })
                self.assertEqual([event for event in controlled.events if event[0] == "spawn"],
                                 [("spawn", "p0"), ("spawn", "mac"), ("spawn", "simulator")])
                self.assertIn(("wait", ("mac", "simulator")), controlled.events)
                for group in ("mac", "simulator"):
                    self.assertLess(controlled.events.index(("complete", group)),
                                    controlled.events.index(("validate", "mac")))
                self.assertEqual(controlled.events[-1], ("cleanup",))
                controlled.gate.summary.assert_not_called()

    def test_missing_or_invalidly_signed_report_blocks_later_stages(self):
        for group in ("p0", "mac", "simulator"):
            for fault in ("missing", "signature"):
                with self.subTest(group=group, fault=fault):
                    controlled = ControlledRun(self.config, self.temporary.name, report_fault=(group, fault))
                    result = controlled.run()
                    self.assertEqual(result, {
                        "status": "failed", "reason": f"{group}_failed" if fault == "signature" else "attempt_failed",
                        "merge_ready": False,
                    })
                    expected = [("spawn", "p0")]
                    if group != "p0":
                        expected += [("spawn", "mac"), ("spawn", "simulator")]
                    self.assertEqual([event for event in controlled.events if event[0] == "spawn"], expected)
                    self.assertNotIn(("verified", group), controlled.events)
                    self.assertEqual(controlled.events[-1], ("cleanup",))
                    controlled.gate.summary.assert_not_called()

    def test_second_apple_spawn_failure_cleans_up_the_first_process(self):
        controlled = ControlledRun(self.config, self.temporary.name, spawn_failure="simulator")
        result = controlled.run()
        self.assertEqual(result, {
            "status": "failed", "reason": "command_unavailable", "merge_ready": False,
        })
        self.assertEqual(controlled.events[-3:], [("spawn", "mac"), ("stop", "mac"), ("cleanup",)])
        self.assertNotIn(("spawn", "ui"), controlled.events)
        self.assertEqual(controlled.engine.active, [])

    def test_metadata_change_or_deadline_between_stages_blocks_next_stage(self):
        for boundary in ("p0", "simulator"):
            for cause in ("metadata", "deadline"):
                with self.subTest(boundary=boundary, cause=cause):
                    controlled = ControlledRun(self.config, self.temporary.name)

                    def validate(report, group, manifest, run_dir, key):
                        controlled.validate(report, group, manifest, run_dir, key)
                        if group == boundary:
                            if cause == "metadata":
                                controlled.github.current = candidate("c" * 40)
                            else:
                                controlled.elapsed = runner.TOTAL_DEADLINE_SECONDS

                    controlled.gate.validate_group.side_effect = validate
                    result = controlled.run()
                    self.assertEqual(result, {
                        "status": "superseded" if cause == "metadata" else "failed",
                        "reason": "pull_request_changed" if cause == "metadata" else "attempt_deadline_exceeded",
                        "merge_ready": False,
                    })
                    expected = [("spawn", "p0")]
                    if boundary == "simulator":
                        expected += [("spawn", "mac"), ("spawn", "simulator")]
                    self.assertEqual([event for event in controlled.events if event[0] == "spawn"], expected)
                    self.assertEqual(controlled.events[-1], ("cleanup",))

    def test_metadata_change_or_deadline_during_apple_wait_stops_remaining_process(self):
        for cause in ("metadata", "deadline"):
            with self.subTest(cause=cause):
                controlled = ControlledRun(self.config, self.temporary.name)

                def release(seconds):
                    controlled.release(seconds)
                    if controlled.events[-1] == ("complete", "mac"):
                        if cause == "metadata":
                            controlled.github.current = candidate("c" * 40)
                            controlled.elapsed += runner.MONITOR_INTERVAL_SECONDS
                        else:
                            controlled.elapsed = runner.TOTAL_DEADLINE_SECONDS

                controlled.engine.sleep = release
                result = controlled.run()
                self.assertEqual(result, {
                    "status": "superseded" if cause == "metadata" else "failed",
                    "reason": "pull_request_changed" if cause == "metadata" else "attempt_deadline_exceeded",
                    "merge_ready": False,
                })
                self.assertEqual(controlled.events[-2:], [("stop", "simulator"), ("cleanup",)])
                self.assertNotIn(("validate", "mac"), controlled.events)
                self.assertNotIn(("spawn", "ui"), controlled.events)
                self.assertEqual(controlled.engine.active, [])

    def test_metadata_change_is_superseded_before_execution_continues(self):
        github = FakeGitHub(candidate("c" * 40))
        engine = runner.Engine(github)
        with self.assertRaisesRegex(runner.Superseded, "pull_request_changed"):
            engine._current(candidate("b" * 40))

if __name__ == "__main__":
    unittest.main()
