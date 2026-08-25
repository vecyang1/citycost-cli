"""What to do when the polite path is refused.

Numbeo answers **HTTP 429** to ordinary reading once it has decided you are a
crawler, and the decision long outlives the load that caused it. Measured
2026-08-25: the concurrent readers stopped at ~09:25 +0700 and a single probe
from the same IP was still refused at 10:39 — over an hour later, and still
refused when measurement ended, so the real duration is unknown and longer.

"Wait and retry" is therefore not a remedy, it is a description of the rest of
the afternoon. The only thing measured to restore service is arriving from
somewhere else.

This module owns that fallback, and owns it in one place, because the two
temptations either side of it are both bad:

* **Vendor a proxy.** Then this repository holds a credential, and a public
  repository that holds a credential is a leak with a delay on it.
* **Do nothing.** Then every caller invents its own way around a 429, which is
  how a credential ends up copied into three projects that should never have
  held one.

Instead: an *external fetcher* named by configuration. citycost runs a command,
takes stdout as the body, and requires exit 0. That is the whole contract. This
repository ships no proxy code and no credentials and cannot leak what it never
holds; anyone with a fetcher — a proxy CLI, a corporate egress, a cache, a
friend's VPS — plugs it in without this project growing a dependency.

**Direct first, always.** The fetcher is a fallback, not a transport. A run that
is not blocked never touches it and never spends anyone's bandwidth, and the
switch is announced on stderr rather than absorbed, because a client that
silently routes around a rate limit is a client whose owner never learns their
sweep is too aggressive.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .errors import SourceUnavailable

#: Shell template containing ``{url}``; stdout is the response body.
GET_CMD_ENV = "CITYCOST_FETCH_CMD"
#: Same, for POST. The request body arrives on the command's **stdin**, which
#: keeps a JSON-RPC payload out of the process table and out of shell quoting.
POST_CMD_ENV = "CITYCOST_POST_CMD"
#: ``auto`` (default) direct first, fetcher only when blocked · ``always``
#: fetcher only · ``never`` direct only, even when that means failing.
MODE_ENV = "CITYCOST_FETCH_MODE"
#: Overridable so a test run can never read the developer's real configuration.
CONFIG_ENV = "CITYCOST_CONFIG"

MODES = ("auto", "always", "never")
DEFAULT_MODE = "auto"

#: Statuses that mean *this client is refused*, as opposed to *this URL is
#: wrong*. Only these justify spending someone's bandwidth: a 404 through a
#: residential proxy is the same 404, bought.
BLOCKED_STATUSES = frozenset({403, 429, 503})

_OWNED_KEYS = (GET_CMD_ENV, POST_CMD_ENV, MODE_ENV)


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV)
                or Path.home() / ".config" / "citycost" / "fetch.conf")


def _read_config() -> tuple[dict[str, str], str]:
    """Return ``(settings, status)``.

    ``status`` is one of ``missing`` / ``ok`` / ``foreign`` / ``unreadable``.
    ``foreign`` matters: a file existing at a path is not the same as a file
    that is *ours*. A config that declares none of our keys is somebody else's,
    and reporting "not configured" without saying which file was opened makes
    "you have no config" and "your config lost" the same sentence.
    """
    path = config_path()
    if not path.exists():
        return {}, "missing"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}, "unreadable"
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _OWNED_KEYS:
            found[key] = _unquote(value.strip())
    return found, ("ok" if found else "foreign")


def _unquote(value: str) -> str:
    """Strip a *matched* pair of surrounding quotes, and nothing else.

    `.strip('"').strip("'")` looks equivalent and is not: it eats a trailing
    quote that belongs to the value. A fetch command legitimately ends in one —
    `--header 'Content-Type: application/json'` — and losing it leaves an
    unbalanced quote that shlex then refuses, so the escape hatch fails at the
    only moment it is ever used.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


class Settings:
    """Resolved transport configuration, and where each part came from."""

    def __init__(self) -> None:
        file_cfg, self.config_status = _read_config()
        self.config_file = str(config_path())

        def resolve(key: str) -> tuple[str, str]:
            env = os.environ.get(key)
            if env:
                return env, "env"
            if file_cfg.get(key):
                return file_cfg[key], "config"
            return "", "unset"

        self.get_cmd, self.get_source = resolve(GET_CMD_ENV)
        self.post_cmd, self.post_source = resolve(POST_CMD_ENV)
        mode, self.mode_source = resolve(MODE_ENV)
        self.mode = (mode or DEFAULT_MODE).lower()
        if self.mode not in MODES:
            raise SourceUnavailable(
                f"{MODE_ENV}={mode!r} is not one of {', '.join(MODES)}",
                f"set {MODE_ENV} to auto, always, or never")

    @property
    def available(self) -> bool:
        return bool(self.get_cmd)

    def cmd_for(self, method: str) -> str:
        return self.post_cmd if method.upper() == "POST" else self.get_cmd


def settings() -> Settings:
    return Settings()


# -- events ----------------------------------------------------------------
#
# A fallback that leaves no trace is a fallback nobody audits. These are read
# by `--json` output so a machine consumer can see the figure did not come
# down the ordinary path, and printed on stderr so a human sees it immediately.

_EVENTS: list[dict] = []


def record(url: str, status: int | None, method: str) -> None:
    _EVENTS.append({"url": url, "blocked_status": status, "method": method})


def events() -> list[dict]:
    return list(_EVENTS)


def reset() -> None:
    _EVENTS.clear()


# -- the fetcher -----------------------------------------------------------

def is_blocked(exc: SourceUnavailable) -> bool:
    return getattr(exc, "status", None) in BLOCKED_STATUSES


def run(url: str, template: str, timeout: int, *,
        data: bytes | None = None, method: str = "GET") -> str:
    """Run the external fetcher and return the body. Exit 0 or raise."""
    if "{url}" not in template:
        raise SourceUnavailable(
            f"the external fetch command must contain the literal {{url}} "
            f"placeholder; got {template!r}",
            f"e.g. {GET_CMD_ENV}='my-fetcher {{url}} --raw'")
    argv = [part.replace("{url}", url) for part in shlex.split(template)]
    # `input=None` does not mean "no stdin" — it means *inherit the caller's*,
    # so a fetcher that reads stdin blocks on the terminal until the timeout
    # expires and then reports itself as slow. Measured: four GET tests went
    # from 0.2s to 45s each that way, and the same hang would have hit anyone
    # piping into citycost. A GET has no body; say so explicitly.
    stream = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
    try:
        proc = subprocess.run(argv, capture_output=True,
                              timeout=timeout + 15, **stream)
    except FileNotFoundError as exc:
        raise SourceUnavailable(
            f"external {method} fetcher not found: {argv[0]}",
            f"fix the command in {config_path()} or {GET_CMD_ENV}, or set "
            f"{MODE_ENV}=never to stay on the direct path") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceUnavailable(
            f"external {method} fetcher timed out on {url}",
            "raise --timeout, or set CITYCOST_FETCH_MODE=never") from exc
    if proc.returncode != 0:
        # A non-zero exit carrying a body is the dangerous case: a 429 page is
        # ~20 KB of plausible HTML and would parse as content if trusted. The
        # contract is exit 0 on 2xx only, precisely so this branch can refuse.
        err = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise SourceUnavailable(
            f"external {method} fetcher exited {proc.returncode} for {url}: {err}",
            "the fetcher must exit 0 only on a 2xx response")
    if not proc.stdout:
        raise SourceUnavailable(
            f"external {method} fetcher returned an empty body for {url}",
            "it exited 0 with nothing on stdout; check its --raw flag")
    return proc.stdout.decode("utf-8-sig", errors="replace")
