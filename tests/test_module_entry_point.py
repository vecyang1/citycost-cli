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
from citycost import __version__


class ModuleEntryPointRuns(unittest.TestCase):

    def _run(self, *args):
        return subprocess.run([sys.executable, "-m", "citycost", *args],
                              capture_output=True, text=True, timeout=30)

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
