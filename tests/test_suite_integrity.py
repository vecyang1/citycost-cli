"""The suite grading itself.

CI ran `unittest discover -s tests` and reported **"Ran 7 tests ... FAILED
(errors=7)"** — seven load errors wearing the shape of seven tests, while the
real suite is 111. Locally the same suite ran green under a different
invocation. Two ways to run one suite, two answers, and the one nobody ran was
the one that mattered.

So the suite states its own denominator, and a module that stops loading, or a
file that stops being discovered, goes red instead of quietly shrinking the
count.
"""

import pathlib
import re
import unittest

from . import _sandbox  # noqa: F401

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO = TESTS_DIR.parent

#: Raise deliberately when adding tests. A count that only ever moves down
#: without anyone noticing is the failure this file exists to prevent.
MIN_TESTS = 465


class TestSuiteIntegrity(unittest.TestCase):
    def test_every_test_module_actually_loads(self):
        """A module that fails to import is reported by unittest as a *passing-
        shaped* `_FailedTest`, so a broken import can look like a small suite
        rather than a broken one."""
        suite = unittest.defaultTestLoader.discover(
            start_dir=str(TESTS_DIR), top_level_dir=str(REPO))
        broken = [str(t) for t in _flatten(suite)
                  if type(t).__name__ == "_FailedTest"]
        self.assertEqual(broken, [], f"modules failed to import: {broken}")

    def test_the_suite_is_at_least_as_large_as_it_was(self):
        suite = unittest.defaultTestLoader.discover(
            start_dir=str(TESTS_DIR), top_level_dir=str(REPO))
        n = len(list(_flatten(suite)))
        self.assertGreaterEqual(
            n, MIN_TESTS,
            f"suite shrank to {n}; expected at least {MIN_TESTS}. Either a "
            f"module stopped loading or tests were removed.")

    def test_every_test_file_on_disk_is_reachable_by_discovery(self):
        """A file named test_*.py that discovery never reaches contributes
        nothing and looks exactly like coverage."""
        on_disk = {p.stem for p in TESTS_DIR.glob("test_*.py")}
        suite = unittest.defaultTestLoader.discover(
            start_dir=str(TESTS_DIR), top_level_dir=str(REPO))
        loaded = {type(t).__module__.rsplit(".", 1)[-1] for t in _flatten(suite)}
        self.assertEqual(on_disk - loaded, set(),
                         f"never discovered: {on_disk - loaded}")

    def test_each_module_is_runnable_on_its_own_the_documented_way(self):
        """These modules use relative imports, so `python tests/x.py` cannot
        work — only `python -m tests.x` can. Asserted so the entry block is not
        advertising a route that fails."""
        import subprocess
        import sys
        # Excluding this module is not laziness: running it as a subprocess
        # would re-run this very test, which spawns again, forever. A guard
        # that recurses is a guard that never reports.
        me = pathlib.Path(__file__).stem
        for p in sorted(TESTS_DIR.glob("test_*.py")):
            if p.stem == me:
                continue
            with self.subTest(module=p.stem):
                r = subprocess.run(
                    [sys.executable, "-m", f"tests.{p.stem}"],
                    cwd=REPO, capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0,
                                 f"python -m tests.{p.stem} failed:\n"
                                 f"{r.stderr[-600:]}")

    def test_nothing_is_defined_below_the_unittest_main_entry_block(self):
        """`unittest.main()` calls sys.exit(), so anything defined after it is
        unreachable under direct invocation — and appending with `>>` always
        lands there."""
        offenders = []
        for p in TESTS_DIR.glob("test_*.py"):
            lines = p.read_text(encoding="utf-8").splitlines()
            entry = next((i for i, l in enumerate(lines)
                          if l.startswith('if __name__')), None)
            if entry is None:
                continue
            after = [l for l in lines[entry + 1:]
                     if l.startswith(("def ", "class "))]
            if after:
                offenders.append(f"{p.name}: {after}")
        self.assertEqual(offenders, [], f"unreachable definitions: {offenders}")


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


class RepoIntegrity(unittest.TestCase):
    """Predicates about the repository that are decidable, so they are tests
    rather than paragraphs somebody has to remember."""

    def test_the_version_has_exactly_one_owner(self):
        """`__version__` said 1.1.0 while pyproject said 1.0.0, so `pipx
        install .` produced a wheel labelled with the older number and the CLI
        it installed disagreed with it. Two literals for one fact agree on the
        day they are written."""
        text = (pathlib.Path(__file__).resolve().parents[1]
                / "pyproject.toml").read_text(encoding="utf-8")
        # Scoped to [project]: `[tool.setuptools.dynamic]` legitimately holds a
        # line starting `version =`, and a regex over the whole file matches it
        # and fails on the correct configuration. tomllib would be cleaner and
        # is 3.11+, while this suite still runs on 3.10.
        block, inside = [], False
        for line in text.splitlines():
            if line.strip().startswith("["):
                inside = line.strip() == "[project]"
                continue
            if inside:
                block.append(line)
        project = "\n".join(block)
        self.assertIn('dynamic = ["version"]', project)
        self.assertIsNone(re.search(r"(?m)^version\s*=", project),
                          "[project] declares a literal version again")


if __name__ == "__main__":
    unittest.main(verbosity=2)
