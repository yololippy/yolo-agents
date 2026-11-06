"""Offline semantic tests for the two-round deterministic PR runner."""
from pathlib import Path
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

    def test_engine_validates_all_groups_before_spawning_ui_then_summarizes(self):
        source = Path(self.temporary.name) / "source"
        source.mkdir()
        run_dir = Path(self.temporary.name) / "run"
        for name in ("p0", "mac", "simulator", "ui"):
            (run_dir / name).mkdir(parents=True)
        events = []

        class Gate:
            GROUPS = ("p0", "mac", "simulator")
            def prepare(self, *_args): events.append("prepare"); return {"run_dir": str(run_dir)}
            def load_manifest(self, *_args): return ({"run_id": "1" * 32}, b"key")
            def read_json(self, path): return {"status": "passed", "path": str(path)}
            def validate_group(self, _report, group, *_args): events.append("validate-" + group)
            def summary(self, *_args): events.append("summary"); return {"merge_ready": True}
            def cleanup(self, *_args): events.append("cleanup")

        class Process:
            returncode = 0
            def poll(self): return self.returncode
            def wait(self, timeout=None): return self.returncode

        engine = runner.Engine(FakeGitHub(candidate()))
        commands = []
        def spawn(command, _cwd, _log):
            commands.append(command); events.append("spawn-ui" if "test_yolo_trade_pr_ui.py" in " ".join(command) else "spawn-group")
            return Process()
        engine._source = Mock(return_value=source)
        engine._spawn = spawn
        with patch.object(runner, "load_gate", return_value=Gate()), patch.object(runner, "git", return_value=""):
            result = engine.run(self.config, candidate(), Path(self.temporary.name) / "attempt")
        self.assertTrue(result["merge_ready"])
        self.assertEqual(events.count("spawn-group"), 3)
        self.assertLess(events.index("validate-simulator"), events.index("spawn-ui"))
        self.assertLess(events.index("spawn-ui"), events.index("summary"))
        self.assertEqual(events[-1], "cleanup")

    def test_metadata_change_is_superseded_before_execution_continues(self):
        github = FakeGitHub(candidate("c" * 40))
        engine = runner.Engine(github)
        with self.assertRaisesRegex(runner.Superseded, "pull_request_changed"):
            engine._current(candidate("b" * 40))

if __name__ == "__main__":
    unittest.main()
