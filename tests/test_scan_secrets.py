"""The secret scanner, graded in both directions.

A scanner is two failure modes, not one. It can miss a real credential — and it
can flag things that are not credentials, which is worse in practice: a reader
who sees four findings and confirms all four are regexes stops reading the
output, and an ignored scanner protects nothing.

Every false positive below was produced by the first version of this scanner on
2026-08-25, against real files. Every true positive is synthetic and assembled
from fragments, so this file does not flag itself and does not need to be
excluded from the scan — excluding it would carve out the one file guaranteed
to accumulate credential-shaped strings.
"""

import pathlib
import re
import subprocess
import sys
import unittest

from . import _sandbox  # noqa: F401

REPO = pathlib.Path(__file__).resolve().parent.parent
SCANNER = REPO / "tools" / "scan_secrets.py"


def _patterns() -> dict:
    src = SCANNER.read_text(encoding="utf-8")
    ns: dict = {"re": re}
    exec(src[src.index("PATTERNS = {"):src.index("hits, graded")], ns)
    return ns["PATTERNS"]


#: Assembled, never written out, so this file stays clean under its own scan.
REAL = {
    "aws-key": "AKIA" + "IOSFODNN7EXAMPLE",
    "google-oauth": "GOCSPX" + "-" + "aBcD3fGh1JkLmN0pQrStUvWx",
    "github-token": "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "proxy-userinfo": "http://" + "ab12cd34" + ":" + "Xk9mQ2rTvB8nLp4w" + "@gw:823",
    "private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
}

#: Every one of these was a real finding from the first version of the scanner.
NOT_CREDENTIALS = {
    "regex source": 'm = re.match(r"https?://([^:]+):([^@]+)@(.+)", url)',
    "f-string template": 'return f"http://{user}:{password}@{host}:8080"',
    "literal placeholder": 'CUSTOM = "http://user:pass@proxy.example.com:1080"',
    "env example": "PROXY_URL=http://USERNAME:PASSWORD@gw.example.com:823",
    "docs placeholder": 'export TOKEN="your-token-here"',
}


class TestScannerCatchesRealSecrets(unittest.TestCase):
    def test_each_credential_shape_is_flagged(self):
        pats = _patterns()
        for name, sample in REAL.items():
            with self.subTest(kind=name):
                self.assertTrue(pats[name].search(sample),
                                f"{name} pattern missed its own shape")


class TestScannerDoesNotCryWolf(unittest.TestCase):
    def test_known_false_positives_stay_clean(self):
        pats = _patterns()
        for name, sample in NOT_CREDENTIALS.items():
            with self.subTest(case=name):
                hits = [k for k, p in pats.items() if p.search(sample)]
                self.assertEqual(hits, [], f"{name!r} flagged by {hits}")


class TestScannerEndToEnd(unittest.TestCase):
    def test_a_clean_tree_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCANNER), str(REPO)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CLEAN", r.stdout)

    def test_it_reports_its_own_denominator(self):
        """'CLEAN' over zero files is not a pass, it is an unasked question."""
        r = subprocess.run([sys.executable, str(SCANNER), str(REPO)],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"graded (\d+) files", r.stdout)
        self.assertIsNotNone(m, r.stdout)
        self.assertGreater(int(m.group(1)), 20)

    def test_it_never_prints_a_matching_line(self):
        """`grep -n` on a secret pattern is how a defensive sweep becomes the
        leak. The scanner must emit a verdict, never a body."""
        src = SCANNER.read_text(encoding="utf-8")
        self.assertNotIn("print(line", src)
        self.assertNotIn("m.group()", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
