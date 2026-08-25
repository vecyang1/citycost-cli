"""Secret scan that reports a VERDICT, not a body.

`grep -n` prints the line containing the value, which is how a defensive sweep
becomes the leak. This prints file + count only. Patterns are assembled from
fragments so the scanner does not flag its own source and does not need to
exclude itself — excluding it would carve out the one file guaranteed to
accumulate credential-shaped strings.
"""
import re, subprocess, sys, pathlib

files = subprocess.run(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
    capture_output=True, text=True, cwd=sys.argv[1]).stdout.split()

PATTERNS = {
    "aws-key":        re.compile("AKIA" + r"[0-9A-Z]{16}"),
    "google-oauth":   re.compile("GOCSPX" + r"-[A-Za-z0-9_\-]{20,}"),
    "google-api":     re.compile("AIza" + r"[0-9A-Za-z_\-]{35}"),
    "github-token":   re.compile(r"gh[pousr]" + "_" + r"[A-Za-z0-9]{36,}"),
    "slack-token":    re.compile("xox" + r"[abprs]-[A-Za-z0-9\-]{10,}"),
    "openai-key":     re.compile("sk" + r"-[A-Za-z0-9]{32,}"),
    "private-key":    re.compile("-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----"),
    "assigned-secret": re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|token|credential)\b\s*[:=]\s*"
        r"['\"][A-Za-z0-9/+=_\-]{16,}['\"]"),
    # user:pass in a URL, EXCLUDING the three forms that are not credentials:
    # a regex source (`([^:]+):([^@]+)@`), an f-string or format template
    # (`{user}:{pass}@`), and a literal placeholder (`user:pass@`). A scanner
    # that flags those trains its readers to ignore it, and an ignored scanner
    # protects nothing — measured 2026-08-25, when this pattern produced four
    # findings and all four were regexes, templates or the word "pass".
    "proxy-userinfo": re.compile(
        r"(?i)://(?!\{|\$|<|%|\(|\[)"
        r"(?!user[s:]|username|your|example|placeholder|changeme|foo|test|"
        r"my[_-]?user|abc)"
        r"[A-Za-z0-9._~-]{3,}:"
        r"(?!\{|\$|<|%|\(|\[)"
        r"(?!pass[:@]|passwd|password|your|example|placeholder|changeme|"
        r"secret[:@]|hunter2|xxx)"
        r"[A-Za-z0-9._~%-]{8,}@"),
    "home-path":      re.compile(r"/Users/[a-z0-9._-]{3,}/"),
    "personal-email": re.compile(r"[A-Za-z0-9._%+-]+@(gmail|foxmail|qq|outlook)\.com"),
}
hits, graded = {}, 0
for rel in files:
    fp = pathlib.Path(sys.argv[1]) / rel
    if not fp.is_file() or fp.stat().st_size > 3_000_000:
        continue
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    graded += 1
    for name, pat in PATTERNS.items():
        n = len(pat.findall(text))
        if n:
            hits.setdefault(name, []).append((rel, n))
print(f"graded {graded} files that git would accept")
if not hits:
    print("CLEAN — no credential-shaped strings")
    sys.exit(0)
for name, rows in sorted(hits.items()):
    print(f"  {name}:")
    for rel, n in rows:
        print(f"      {rel}  x{n}")
sys.exit(1)
