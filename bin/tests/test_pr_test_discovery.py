"""The repository test command must collect the UI runner's regression suite."""

from pathlib import Path
import subprocess
import sys
import unittest


class RepositoryDiscoveryTests(unittest.TestCase):
    def test_discovery_survives_command_directory_preceding_test_directory(self):
        root = Path(__file__).resolve().parents[2]
        program = """
import pathlib, sys, unittest
root = pathlib.Path(sys.argv[1])
# Other tooling tests import command modules from bin before discovery finishes.
sys.path.insert(0, str(root / 'bin'))
suite = unittest.TestLoader().discover(str(root / 'bin/tests'), pattern='test_*.py')
def cases(node):
    if isinstance(node, unittest.TestSuite):
        for child in node:
            yield from cases(child)
    else:
        yield node
items = list(cases(suite))
errors = [item for item in items if isinstance(item, unittest.loader._FailedTest)]
if errors:
    raise AssertionError('repository test collection contains import failures')
assert any(item.id().startswith('test_pr_ui_runner.') for item in items)
"""
        result = subprocess.run(
            [sys.executable, "-c", program, str(root)], cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
