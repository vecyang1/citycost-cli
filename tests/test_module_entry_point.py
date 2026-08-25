"""`python -m citycost` must work, and must be the same program as the script.

Two entry points is the shape that rots quietly: the console script is what
everyone runs, so a broken `-m` is discovered by the one person who could not
run the console script — the reader the README sends to `pipx ensurepath`,
who is already having the bad day this is supposed to shorten.

`--version` is the right probe because argparse handles it before any command
function is reached, so this stays hermetic: no network, no cache, no config.
Running it as a subprocess rather than importing `citycost.__main__` is the
point — importing proves the file parses, and the failure being guarded is
that the module is *absent from the package*, which an import inside the
source tree cannot see.
"""

import subprocess
import sys
import unittest

from . import _sandbox  # noqa: F401
import pathlib
import re

import citycost
from citycost import __version__


class ModuleEntryPointRuns(unittest.TestCase):

    def _run(self, *args):
        return subprocess.run([sys.executable, "-m", "citycost", *args],
                              capture_output=True, text=True, timeout=30)

    def test_the_imported_version_matches_the_source_TEXT(self):
        """The one reading that a stale bytecode cache cannot fake.

        Every other version check here compares two values that both came from
        an import, so they agree on the wrong number together. Measured
        2026-08-26: bumping `1.3.1` to `1.4.0` is a same-LENGTH edit, and
        CPython decides a `.pyc` is stale from the source's mtime in whole
        seconds plus its size — both unchanged. `python -m citycost --version`
        printed 1.3.1 from a file that said 1.4.0, and `-B` does not help
        because it disables WRITING bytecode, not reading it.

        Reading the file as text is the independent witness. It also fails when
        somebody edits the constant without the release, which is the same
        question asked from the other end.
        """
        src = pathlib.Path(citycost.__file__).read_text(encoding="utf-8")
        declared = re.search(r'__version__\s*=\s*"([^"]+)"', src)
        self.assertIsNotNone(declared, "no __version__ literal in __init__.py")
        self.assertEqual(declared.group(1), __version__,
                         "the imported version disagrees with the source text "
                         "— clear __pycache__ and re-run before believing "
                         "anything else in this suite")

    def test_dash_m_reports_the_version(self):
        proc = self._run("--version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(__version__, proc.stdout + proc.stderr)

    def test_dash_m_reports_the_same_version_the_package_declares(self):
        """The drift this closes is not hypothetical.

        On 2026-08-25 a fix was verified against `citycost` on PATH, which was
        a pipx build one version behind the source tree. Both answered
        `citycost 1.1.1`, so the run looked like evidence and was not. An
        invocation that cannot pick a different copy is the cheap defence.
        """
        proc = self._run("--version")
        self.assertIn(__version__, proc.stdout + proc.stderr)

    def test_dash_m_exits_nonzero_on_a_bad_subcommand(self):
        """A entry point that cannot fail has not been shown to run."""
        proc = self._run("no-such-command")
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
