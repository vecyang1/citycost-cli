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
# Re-exported: `fmt_age` is a pure formatter and belongs with the other
# formatters, but `net.fmt_age` is the name existing callers and tests use.
from .render import fmt_age  # noqa: F401
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
    """One call into the external fetcher; `fallback.run` owns retry from here.

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

#: Where a *pinned* entry lives. The retention decision (may I delete this?)
#: is carried by the PATH, never by a field inside the payload — because the
#: payload is schema-versioned and `_schema` is *designed* to change: bumping
#: it is how a parser fix invalidates stale entries on purpose. Putting the
#: keep-or-delete decision inside a versioned blob would make the one event the
#: system plans for the event that un-protects the archive. A path also
#: survives a file that no longer parses, and needs no cooperation from the
#: delete loop: `Path.glob("*.json")` is non-recursive, so the two functions
#: below already spare this directory without a line changing. Tests pin that
#: in both directions, so a future `rglob` "fix" goes red instead of silently
#: deleting a corpus that cannot be re-taken while the address is banned.
ARCHIVE_DIR = "keep"


def _cache_path(key: str, *, keep: bool = False) -> Path:
    import hashlib
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in key)[:80]
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    base = cache_dir() / ARCHIVE_DIR if keep else cache_dir()
    return base / f"{safe}.{digest}.json"


def _existing_path(key: str) -> Path | None:
    """Wherever the entry actually is. An entry can migrate between the two
    directories when its snapshot ages past the pin threshold, so a reader
    that looked in only one would report `absent` for a file it owns."""
    for keep in (True, False):
        p = _cache_path(key, keep=keep)
        if p.exists():
            return p
    return None


#: Did this process actually touch the network? `--fetch-mode never/always` is
#: a question *about the transport*, and a run answered entirely from disk has
#: tested none of it — which is how `--fetch-mode never` returned a confident
#: exit 0 while the address was banned for a week. Counted here because
#: `cached_json` is the one place that knows, and a second counter elsewhere
#: would be a second answer to the same question.
_READS = {"live": 0, "cached": 0}


def reads() -> dict:
    return dict(_READS)


def reset_reads() -> None:
    _READS.update(live=0, cached=0)


def cache_probe(key: str, schema: str, max_age: int, *,
                archival: bool = False) -> dict:
    """Can the cache answer this, and if not, why not — as a REASON, not a bool.

    One predicate, consulted by both the reader (`cached_json`) and by anything
    that wants to *plan* reads without performing them. Two implementations of
    "file exists AND schema matches AND age is acceptable" agree on the day
    they are written and diverge silently after; here the divergence would be
    denominated in requests against a source that bans by address for a week.

    States: `absent` · `unreadable` · `schema_mismatch` · `stale` · `fresh`.
    "You have 140 of 192" and "you have 192 files, all under the previous
    schema" cost 52 and 192 requests respectively, and a boolean makes them the
    same sentence.

    `max_age <= 0` is a distinct state meaning *read live*, not a very small
    number, and it is honoured for a pinned entry exactly as for any other.
    That rule is written once, here: a conditionally-honoured `--max-age 0`
    would recreate 1.3.0's accepted-and-silently-ignored defect one layer down,
    where nothing observes it. It is also the only non-destructive way to
    replace a poisoned pin.
    """
    path = _existing_path(key)
    if path is None:
        return {"state": "absent", "age_s": None, "path": None, "blob": None}
    try:
        age = int(time.time() - path.stat().st_mtime)
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"state": "unreadable", "age_s": None, "path": path, "blob": None}
    if not isinstance(blob, dict) or blob.get("_schema") != schema:
        return {"state": "schema_mismatch", "age_s": age, "path": path,
                "blob": blob if isinstance(blob, dict) else None}
    if archival and (blob.get("_meta") or {}).get("moved"):
        # An OBSERVATION overrules a caller's claim. The caller derives
        # `archival` from the id; this file records that the id was seen
        # holding two different tables, which disproves the claim the id was
        # making. Honoured here rather than at the caller because a rule
        # enforced in one of two readers is enforced in neither: the veto used
        # to survive exactly one write and then evaporate, since nothing on the
        # read path ever looked at it again.
        archival = False
    if max_age <= 0:
        return {"state": "stale", "age_s": age, "path": path, "blob": blob}
    if archival or age < max_age:
        return {"state": "fresh", "age_s": age, "path": path, "blob": blob}
    return {"state": "stale", "age_s": age, "path": path, "blob": blob}


def cached_json(key: str, schema: str, max_age: int, produce, *,
                archival: bool = False, keep: bool | None = None,
                meta_hook=None):
    """Return `(payload, age_seconds, from_cache)`.

    `produce()` is only called when `cache_probe` says the cache cannot answer.
    The age travels with the payload so a caller can print it — a figure whose
    age is unstated is a figure that will be read as current.

    Two axes, deliberately separate, because a payload can want one without the
    other. `archival=True` exempts the entry from the AGE CLOCK — may I serve
    this without asking? `keep=True` puts it where `prune_cache` will not
    remove it — may I delete this file? They default together because that is
    the common case, and they must be separable because the snapshot LIST is
    the case where they disagree: it is genuinely re-published (so it expires
    on a clock) and it is the only offline owner of the archive's denominator
    (so deleting it makes 192 pinned pages unenumerable, during exactly the ban
    that made them worth pinning). One flag setting both is the fusion this
    change exists to undo.

    `meta_hook(payload, prior_meta) -> dict` runs only on a live read, so a
    caller can record a fact about the *transition* — did a table we called
    immutable actually change — without opening the cache file itself, which
    would make it a second reader of the one file this function owns. A hook
    returning `{"moved": True}` vetoes the pin: an id observed with two
    different contents has disproved its own immutability, and no age rule may
    overrule an observation.
    """
    if keep is None:
        keep = archival
    probe = cache_probe(key, schema, max_age, archival=archival)
    if probe["state"] == "fresh":
        _READS["cached"] += 1
        return probe["blob"].get("payload"), probe["age_s"], True

    prior_meta = {}
    if isinstance(probe["blob"], dict):
        prior_meta = probe["blob"].get("_meta") or {}

    # Counted before the call, not after: a live attempt that *fails* still
    # exercised the transport, which is the question this counter answers.
    _READS["live"] += 1
    payload = produce()
    meta = {}
    if meta_hook is not None:
        meta = meta_hook(payload, prior_meta) or {}
    if meta.get("moved"):
        keep = False
    cache_write(key, schema, payload, meta=meta, keep=keep)
    return payload, 0, False


def cache_write(key: str, schema: str, payload, *, meta: dict | None = None,
                keep: bool = False) -> bool:
    """The one writer. Returns whether the write landed.

    Records `_fetched_at` because mtime is not a fact about the read: a corpus
    copied between machines or restored from backup arrives with every mtime
    set to today, so a 300-day-old figure reports `age 0s`.

    Swallowing the error is right for an ordinary read — an unwritable cache
    must not fail a read that already succeeded — and wrong for a caller whose
    entire product IS the write. A read-only home directory or a full disk
    would otherwise let a 192-request harvest report success having stored
    nothing, so the outcome is returned rather than only logged.
    """
    try:
        target = _cache_path(key, keep=keep)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"_schema": schema, "_fetched_at": int(time.time()),
                        "_meta": meta or {}, "payload": payload}),
            encoding="utf-8")
        # An entry that changed side must not be left behind in the other
        # directory: two copies of one key is two answers to one question, and
        # the stale one wins whenever it is the first found.
        other = _cache_path(key, keep=not keep)
        if other.exists():
            other.unlink()
        return True
    except OSError:
        return False


def cache_entries(*, keep: bool | None = None) -> list[Path]:
    """Every cache file, optionally only one side. Non-recursive on purpose per
    directory, so the two sets stay disjoint and countable."""
    d = cache_dir()
    out = []
    if keep in (None, False) and d.exists():
        out += sorted(d.glob("*.json"))
    if keep in (None, True) and (d / ARCHIVE_DIR).exists():
        out += sorted((d / ARCHIVE_DIR).glob("*.json"))
    return out


def cache_status() -> dict:
    """Facts about the corpus on disk, read-only, no network.

    Reports `mtime_skew` because mtime is not a fact about when a figure was
    read: a corpus copied between machines or restored from backup arrives with
    every mtime set to today, so a 300-day-old entry announces `age 0s` and a
    genuinely stale figure passes every freshness rule in the tool. Comparing
    it against the recorded `_fetched_at` is the only observer for that, and it
    costs a stat and a parse.
    """
    now = time.time()
    out = {"dir": str(cache_dir()), "ordinary": 0, "archived": 0, "bytes": 0,
           "unreadable": 0, "mtime_skew": 0, "oldest_fetch_s": None,
           "legacy": 0, "schemas": {}}
    for keep in (False, True):
        for f in cache_entries(keep=keep):
            out["archived" if keep else "ordinary"] += 1
            try:
                out["bytes"] += f.stat().st_size
                blob = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                out["unreadable"] += 1
                continue
            if not isinstance(blob, dict):
                out["unreadable"] += 1
                continue
            sch = blob.get("_schema") or "?"
            out["schemas"][sch] = out["schemas"].get(sch, 0) + 1
            fetched = blob.get("_fetched_at")
            if not isinstance(fetched, (int, float)):
                # Written before this field existed. Counted, because every
                # observer that reads `_fetched_at` is SILENT about these, and
                # silence reads as a clean bill.
                out["legacy"] += 1
            if isinstance(fetched, (int, float)):
                age = int(now - fetched)
                if out["oldest_fetch_s"] is None or age > out["oldest_fetch_s"]:
                    out["oldest_fetch_s"] = age
                try:
                    if abs(f.stat().st_mtime - fetched) > 3600:
                        out["mtime_skew"] += 1
                except OSError:
                    pass
    return out


def prune_cache(retention: int = CACHE_RETENTION) -> dict:
    """Remove expired ordinary entries. Reports its own denominator.

    Returning a bare count made `pruned 0 expired cache files` the same
    sentence for a clean cache and for a `CITYCOST_CACHE_DIR` pointing at an
    empty or misspelt directory — and the second one is a user reading a zero
    denominator as a clean bill.
    """
    d = cache_dir()
    now, removed, scanned = time.time(), 0, 0
    for f in d.glob("*.json") if d.exists() else []:
        scanned += 1
        try:
            if now - f.stat().st_mtime > retention:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return {"scanned": scanned, "removed": removed,
            "protected": len(cache_entries(keep=True)), "dir": str(d)}


def clear_cache(*, include_archive: bool = False) -> dict:
    """Delete cache entries. The archive is spared unless explicitly named.

    `--clear` is reached by someone who wants a clean read, not by someone who
    wants to destroy a corpus that took hundreds of throttled requests and
    cannot be re-taken while the address is banned. Those are different
    intentions and they get different flags.
    """
    d, n, kept = cache_dir(), 0, 0
    for f in d.glob("*.json") if d.exists() else []:
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    for f in cache_entries(keep=True):
        if include_archive:
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        else:
            kept += 1
    return {"removed": n, "protected": kept, "dir": str(d)}


def urlencode(params: dict) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return urllib.parse.urlencode(clean)
