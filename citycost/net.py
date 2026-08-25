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

from . import fallback, render
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


def retry_after(headers) -> str:
    """Turn a `Retry-After` header into something a human can act on.

    Read the server before measuring the server. Numbeo's 429 states exactly
    when the block lifts — `Retry-After: Tue, 1 Sep 2026 08:00:00 +0200`, a
    **seven day** ban — and this client spent an afternoon probing to conclude
    "duration unknown, at least an hour" while the answer was in every one of
    those responses. The measurement was not wrong, it was unnecessary; the
    remedy built on it ("wait and retry") was wrong.

    Both RFC forms occur: an HTTP-date and a delta in seconds.
    """
    raw = (headers.get("Retry-After") or "").strip() if headers else ""
    if not raw:
        return ""
    import datetime
    import email.utils
    when = None
    if raw.isdigit():
        when = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=int(raw)))
    else:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return f"server says Retry-After: {raw}"
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    left = when - datetime.datetime.now(datetime.timezone.utc)
    secs = left.total_seconds()
    if secs <= 0:
        return f"blocked until {when.isoformat(timespec='minutes')} (now past)"
    span = (f"{secs / 86400:.1f} days" if secs >= 86400
            else f"{secs / 3600:.1f} hours" if secs >= 3600
            else f"{secs / 60:.0f} minutes")
    return f"blocked until {when.isoformat(timespec='minutes')} — {span} away"


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
            # A 4xx is an answer, not a blip: retrying a 429 makes the rate
            # limit worse and retrying a 404 never helps. Keyed on the status,
            # never on the message — see errors.SourceUnavailable.
            if exc.status is not None and 400 <= exc.status < 500:
                raise
            last = exc
    # Carry the status through. Losing it here means a 503 that survived four
    # attempts arrives at the fallback decision as "no status", so `is_blocked`
    # says no and the escape hatch never opens for the one failure mode most
    # likely to need it.
    raise SourceUnavailable(
        f"{what} failed after {RETRIES} attempts: {last.message if last else ''}",
        last.remedy if last else "check network connectivity",
        status=last.status if last else None)


def _via_fetcher(url: str, cfg, timeout: int, *, data: bytes | None,
                 method: str, blocked: int | None) -> str:
    """One attempt through the external fetcher. Deliberately not retried.

    Every attempt here costs somebody bandwidth, and the fetcher has its own
    retry policy; wrapping it in ours would multiply two backoffs together and
    bill for the product.
    """
    # No "is it configured" guard here on purpose: `_fetch` refuses before it
    # ever calls this, and a second check that cannot fire is a guard a reader
    # trusts and a mutation test grades as covered.
    template = cfg.cmd_for(method)
    fallback.record(url, blocked, method)
    _throttle(url)          # a proxy exit is still an IP somebody else shares
    return fallback.run(url, template, timeout, data=data, method=method)


def _fetch(url: str, *, timeout: int, data: bytes | None, method: str,
           direct):
    """Direct first; the fetcher only when the direct path is *refused*.

    The three modes differ in one decision only, which is why they live here
    and not scattered across call sites:

    ``never``   direct only, and a 429 is a failure the caller must see.
    ``auto``    direct, then the fetcher on 403/429/503. The default.
    ``always``  the fetcher only — for when you already know you are blocked
                and would rather not spend a minute proving it again.
    """
    cfg = fallback.settings()
    if cfg.mode == "always":
        if not cfg.cmd_for(method):
            raise SourceUnavailable(
                f"{fallback.MODE_ENV}=always but no {method} fetch command is "
                f"configured",
                f"set one in {fallback.config_path()}, or use "
                f"{fallback.MODE_ENV}=auto")
        return _via_fetcher(url, cfg, timeout, data=data, method=method,
                            blocked=None)
    try:
        return _with_retry(direct, what=url)
    except SourceUnavailable as exc:
        if cfg.mode == "never" or not fallback.is_blocked(exc):
            raise
        if not cfg.cmd_for(method):
            # Blocked with nothing to fall back to: say how to get one rather
            # than repeating "wait and retry", which was measured to still be
            # wrong an hour after the load stopped. Name the variable for
            # *this* verb —
            # a GET-only configuration is the common half-installed state, and
            # pointing its owner at the GET variable they already set is the
            # kind of right-answer-to-the-wrong-question that stops a reader
            # before the line that would have fixed it.
            var = (fallback.POST_CMD_ENV if method == "POST"
                   else fallback.GET_CMD_ENV)
            how = (" (the request body arrives on the command's stdin)"
                   if method == "POST" else "")
            exc.remedy = (
                f"{exc.remedy}; or configure an external {method} fetcher: "
                f"set {var}{how} in {fallback.config_path()} "
                f"— `citycost doctor` reports what is wired")
            raise
        # Print the exception's own message rather than rebuilding a summary:
        # it already carries the server's stated deadline, and a second,
        # shorter sentence about the same event is how the two drift apart.
        # "spends its bandwidth" was dropped once a fetcher could be free —
        # the client does not know what the configured command costs, and
        # asserting a cost it cannot see is the kind of confident detail a
        # reader believes.
        render.note(f"  ! {exc.message}\n"
                    f"    -> rerouting through the configured fetcher")
        return _via_fetcher(url, cfg, timeout, data=data, method=method,
                            blocked=exc.status)


def http_get(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    return _fetch(url, timeout=timeout, data=None, method="GET",
                  direct=lambda: _http_get_once(url, timeout=timeout))


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
            until = retry_after(exc.headers)
            long_block = "days" in until or "hours" in until
            raise SourceUnavailable(
                f"{url} refused this client (HTTP 429)"
                + (f" — {until}" if until else ""),
                # The remedy depends on the number the server gave, because
                # "wait" and "you cannot wait" are different instructions and
                # only the header knows which one applies.
                ("this is an address-level block, not a pause: a real browser "
                 "on this network is refused too. Use another network or "
                 "configure an external fetcher (citycost doctor); do not run "
                 "concurrent agents against this host"
                 if long_block else
                 "lower concurrency, widen --max-age so cached figures are "
                 "reused, or configure an external fetcher (citycost doctor)"),
                status=429) from exc
        raise SourceUnavailable(
            f"{url} returned HTTP {exc.code}",
            "check the URL is still valid; the site may have moved or blocked "
            "this client", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise SourceUnavailable(f"{url} unreachable: {exc.reason}",
                                "check network connectivity") from exc


def http_post_json(url: str, payload: dict, *, timeout: int = DEFAULT_TIMEOUT,
                   headers: dict | None = None) -> dict:
    """POST JSON, with the same direct-then-fetcher policy as `http_get`.

    A GET-only fallback would be worse than none: the half that works hides
    that the other half never runs, and the caller discovers its discovery
    command is dead only when it is already blocked.
    """
    text = _fetch(
        url, timeout=timeout, data=json.dumps(payload).encode("utf-8"),
        method="POST",
        direct=lambda: _http_post_json_once(url, payload, timeout=timeout,
                                            headers=headers))
    if isinstance(text, dict):          # the direct path already parsed it
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceUnavailable(
            f"{url} returned non-JSON through the external fetcher: "
            f"{text[:160]}",
            "the fetcher must print the response body and nothing else "
            "(the --raw flag on most)") from exc


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
            until = retry_after(exc.headers)
            raise SourceUnavailable(
                f"{url} rate-limited (HTTP 429)"
                + (f" — {until}" if until else "") + f" {detail}".rstrip(),
                "this endpoint is explicitly not a bulk source; slow down, or "
                "configure an external fetcher (citycost doctor)",
                status=429) from exc
        raise SourceUnavailable(f"{url} returned HTTP {exc.code} {detail}".strip(),
                                "check the endpoint and payload shape",
                                status=exc.code) from exc
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
