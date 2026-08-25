"""HTTP with an honest cache.

Two rules this module exists to enforce:

1. **Decode explicitly.** `urllib` hands back bytes and a header that may not
   declare a charset. Guessing produces a body whose first field name carries a
   BOM welded to it, which then reads as "that column is empty" forever.

2. **A cache is a cost saver, never the answer.** Everything served reports its
   own age, anything past `max_age` is refetched, and the payload is stamped
   with a schema fingerprint so an entry written by an older version is
   discarded rather than silently missing its new keys.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from .errors import SourceUnavailable

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_AGE = 3600          # a figure older than this is refetched
CACHE_RETENTION = 7 * 86400     # files older than this are pruned


def cache_dir() -> Path:
    """Overridable so a test run can never touch the developer's real cache."""
    return Path(os.environ.get("CITYCOST_CACHE_DIR")
                or Path.home() / ".cache" / "citycost")


def fmt_age(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _decode(raw: bytes, content_type: str) -> str:
    """utf-8-sig first: it strips a BOM if present and is plain utf-8 if not.

    Only fall back to a declared charset, then to a lenient utf-8. Never to the
    library's ISO-8859-1 default, which is what turns a leading BOM into three
    visible characters glued onto the first header cell.
    """
    for enc in ("utf-8-sig",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    declared = ""
    if "charset=" in content_type.lower():
        declared = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
    for enc in (declared, "utf-8", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def _unwrap(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    if "gzip" in enc:
        return gzip.decompress(raw)
    if "deflate" in enc:
        return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


#: Transient failures are normal and are not the same as a broken endpoint.
#: Measured 2026-08-25: under concurrent load the first connect to numbeo.com
#: timed out and the immediate retry succeeded in 1.2s. A client that dies on
#: the first blip reports "unreachable" about a site that is up.
RETRIES = 4
BACKOFF = (0.0, 1.5, 4.0, 9.0)

#: Minimum gap between requests to the same host, seconds.
#:
#: Not politeness theatre — measured 2026-08-25: six concurrent agents reading
#: numbeo.com earned an HTTP 429 within about two minutes. A free public source
#: that a client can get itself blocked from is a source that stops working for
#: everyone who installs this. The gap is per-process and per-host, so a normal
#: interactive run never notices it and a sweep paces itself.
#:
#: Routing around a rate limit with residential proxies was considered and
#: rejected as the default: it costs money, it is a poor citizen on a site
#: whose robots.txt permits ordinary reading, and it hides the signal that a
#: sweep is too aggressive instead of fixing it.
MIN_INTERVAL = {"www.numbeo.com": 1.1, "nomads.com": 1.5}
DEFAULT_INTERVAL = 0.4
_last_hit: dict[str, float] = {}


def _throttle(url: str) -> None:
    host = urllib.parse.urlsplit(url).netloc
    gap = MIN_INTERVAL.get(host, DEFAULT_INTERVAL)
    prev = _last_hit.get(host)
    if prev is not None:
        wait = gap - (time.monotonic() - prev)
        if wait > 0:
            time.sleep(wait)
    _last_hit[host] = time.monotonic()


def _with_retry(fn, *, what: str):
    last = None
    for attempt in range(RETRIES):
        if BACKOFF[attempt]:
            time.sleep(BACKOFF[attempt])
        try:
            return fn()
        except SourceUnavailable as exc:
            # A rate limit or a 4xx is an answer, not a blip: retrying a 429
            # makes the rate limit worse and retrying a 404 never helps.
            if "429" in exc.message or "HTTP 4" in exc.message:
                raise
            last = exc
    raise SourceUnavailable(
        f"{what} failed after {RETRIES} attempts: {last.message if last else ''}",
        last.remedy if last else "check network connectivity")


#: An external fetcher, for the case where the polite path is genuinely blocked.
#:
#: ``CITYCOST_FETCH_CMD`` is a shell template containing ``{url}``. citycost runs
#: it, takes stdout as the response body, and requires exit 0. That is the whole
#: contract — which is the point: this repository ships **no** proxy code and
#: **no** credentials, and cannot leak what it never holds. Anyone who already
#: has a fetcher (a proxy CLI, a corporate egress, a cache) plugs it in without
#: this project growing a dependency or a secret.
#:
#:     export CITYCOST_FETCH_CMD='my-fetcher {url} --raw'
#:
#: Deliberately opt-in. Making it the default would spend someone's money by
#: surprise and would hide the signal that a sweep is too aggressive, instead of
#: fixing it — and a tool that routes around a rate limit by default is a worse
#: citizen than one that waits.
FETCH_CMD_ENV = "CITYCOST_FETCH_CMD"


def _external_fetch(url: str, template: str, timeout: int) -> str:
    import shlex
    import subprocess
    if "{url}" not in template:
        raise SourceUnavailable(
            f"{FETCH_CMD_ENV} must contain the literal {{url}} placeholder",
            f"e.g. {FETCH_CMD_ENV}='my-fetcher {{url}} --raw'")
    argv = [part.replace("{url}", url) for part in shlex.split(template)]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout + 15)
    except FileNotFoundError as exc:
        raise SourceUnavailable(f"{FETCH_CMD_ENV} command not found: {argv[0]}",
                                f"check {FETCH_CMD_ENV}, or unset it to use the "
                                f"built-in fetcher") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceUnavailable(f"{FETCH_CMD_ENV} timed out on {url}",
                                "raise the timeout or unset the variable") from exc
    if proc.returncode != 0:
        # A non-zero exit with a body is the dangerous case: a 429 page is
        # ~20 KB of plausible HTML and would parse as content if trusted.
        err = proc.stderr.decode("utf-8", "replace")[:200]
        raise SourceUnavailable(
            f"{FETCH_CMD_ENV} exited {proc.returncode} for {url}: {err}",
            "the external fetcher failed; it must exit 0 only on a 2xx")
    return _decode(proc.stdout, "")


def http_get(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    template = os.environ.get(FETCH_CMD_ENV)
    if template:
        return _with_retry(
            lambda: _external_fetch(url, template, timeout), what=url)
    return _with_retry(lambda: _http_get_once(url, timeout=timeout), what=url)


def _http_get_once(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    _throttle(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _unwrap(resp.read(), resp.headers.get("Content-Encoding", ""))
            return _decode(raw, resp.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as exc:
        # 429 and 403 have different remedies and must not read alike.
        if exc.code == 429:
            raise SourceUnavailable(
                f"{url} rate-limited (HTTP 429)",
                "wait and retry; lower concurrency, or widen --max-age so "
                "cached figures are reused") from exc
        raise SourceUnavailable(
            f"{url} returned HTTP {exc.code}",
            "check the URL is still valid; the site may have moved or blocked "
            "this client") from exc
    except urllib.error.URLError as exc:
        raise SourceUnavailable(f"{url} unreachable: {exc.reason}",
                                "check network connectivity") from exc


def http_post_json(url: str, payload: dict, *, timeout: int = DEFAULT_TIMEOUT,
                   headers: dict | None = None) -> dict:
    return _with_retry(
        lambda: _http_post_json_once(url, payload, timeout=timeout,
                                     headers=headers),
        what=url)


def _http_post_json_once(url: str, payload: dict, *,
                         timeout: int = DEFAULT_TIMEOUT,
                         headers: dict | None = None) -> dict:
    _throttle(url)
    body = json.dumps(payload).encode("utf-8")
    hdrs = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _unwrap(resp.read(), resp.headers.get("Content-Encoding", ""))
            text = _decode(raw, resp.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code == 429:
            raise SourceUnavailable(
                f"{url} rate-limited (HTTP 429) {detail}".strip(),
                "this endpoint is explicitly not a bulk source; slow down") from exc
        raise SourceUnavailable(f"{url} returned HTTP {exc.code} {detail}".strip(),
                                "check the endpoint and payload shape") from exc
    except urllib.error.URLError as exc:
        raise SourceUnavailable(f"{url} unreachable: {exc.reason}",
                                "check network connectivity") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceUnavailable(f"{url} returned non-JSON: {text[:160]}",
                                "the endpoint may have changed shape") from exc


# -- cache -----------------------------------------------------------------

def _cache_path(key: str) -> Path:
    import hashlib
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in key)[:80]
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return cache_dir() / f"{safe}.{digest}.json"


def cached_json(key: str, schema: str, max_age: int, produce):
    """Return `(payload, age_seconds, from_cache)`.

    `produce()` is only called when the cache misses, is stale, or was written
    under a different schema. The age travels with the payload so a caller can
    print it — a figure whose age is unstated is a figure that will be read as
    current.
    """
    path = _cache_path(key)
    if max_age > 0 and path.exists():
        age = int(time.time() - path.stat().st_mtime)
        if age < max_age:
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                blob = None
            if isinstance(blob, dict) and blob.get("_schema") == schema:
                return blob.get("payload"), age, True

    payload = produce()
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(
            json.dumps({"_schema": schema, "payload": payload}),
            encoding="utf-8")
    except OSError:
        pass  # an unwritable cache must not fail a read that already succeeded
    return payload, 0, False


def prune_cache(retention: int = CACHE_RETENTION) -> int:
    d = cache_dir()
    if not d.exists():
        return 0
    now, removed = time.time(), 0
    for f in d.glob("*.json"):
        try:
            if now - f.stat().st_mtime > retention:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def clear_cache() -> int:
    d = cache_dir()
    if not d.exists():
        return 0
    n = 0
    for f in d.glob("*.json"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def urlencode(params: dict) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return urllib.parse.urlencode(clean)
