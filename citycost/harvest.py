"""`citycost harvest` — the sweep this tool already performs, given a plan.

`citycost trend Prague --snapshots 31` issues 32 requests today with no plan,
no consent number, no rule for stopping, and a TTL that throws the result away
before the next session — so three verticals explored across four sessions in
one day cost 364 requests for 88 distinct, immutable objects. The question is
not "should citycost sweep" but "should the sweep it already performs have a
plan, a number, an abort rule, and a result that outlives the hour".

Four properties separate that from a second seven-day ban, and each is argued
at the guard that implements it: **one transport** (`_fetch_one` — everything
goes through `rankings.fetch` and nothing lower, so the 1.1s pace, the
never-retry-a-429 rule and the reroute decision keep one owner each), **one
planner** (`_target_key`, `run` — the number printed and the number spent come
from one predicate over one target list), **one harvest at a time** (`_lock` —
`net._last_hit` is per-process, so two harvests are two unpaced agents, the
measured cause of the ban), and **stop at the first refusal** (`_fetch_one` —
the block is address-level and does not slide). The documented alternative, a
shell loop over `citycost rank --snapshot <id>`, has none of them: 192 separate
processes reset the throttle 192 times and hammer at full speed.

Deliberately absent: an `--out`/`--export` destination (192 parsed Numbeo
tables in a directory the user names is a redistributed dataset, whatever the
flag was for), a `--source` flag (city pages are the 558x-wrong channel and
nomads.com states it is not a bulk source), and a manifest (a second owner of
"what has been harvested", drifting from the cache the first time an entry is
pruned). The cache files are the record; resumability reads them back through
the probe the plan used.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import socket
import time
import urllib.parse

from . import fallback, net, rankings, render
from .errors import CitycostError, LayoutChanged, SourceUnavailable

#: The states `net.cache_probe` can return, in the order the plan prints them.
#: A second owner of that vocabulary is tolerable only because the partition
#: counts whatever the probe actually says and merely *displays* it through
#: this tuple — a state added to `net` lands in the totals and in the to-fetch
#: column rather than being silently dropped.
PROBE_STATES = ("fresh", "stale", "schema_mismatch", "unreadable", "absent")

#: `rankings._parse` raises this when upstream answers "no such selection" — a
#: successful, permanent answer about that URL rather than a failure. It is
#: prose, which `errors.py` exists to discourage, and there is no status to key
#: on instead: this and a dead socket both arrive as
#: `SourceUnavailable(status=None)`. So a test raises the real exception out of
#: `rankings._parse` and asserts this still matches — reword it upstream and
#: that goes red, rather than every dead snapshot silently becoming a failure
#: no rerun can clear, which makes exit 0 unreachable.
EMPTY_MARKER = "has no ranking table for that selection"

#: Why `current` is not a subject. Stated in the payload rather than only in
#: the printed line, so a `--json` consumer sees the exclusion too: a user who
#: harvested "everything" and then reads a stale `rank` has been told nothing.
EXCLUDED_CURRENT = {
    "subject": "current",
    "reason": "rankings_current.jsp is a different object from ?title=<id> "
              "(558 rows against 547 for 2026-mid) and the one URL in the "
              "family that legitimately moves — folding it in is how a "
              "one-time command becomes a weekly cron",
    "instead": "citycost rank --index <vertical>",
}

#: Name of the exclusive lock, inside `net.cache_dir()` — the process-shared,
#: `CITYCOST_CACHE_DIR`-overridable location the test sandbox already pins.
LOCK_NAME = "harvest.lock"

#: Identical fingerprints below this many snapshots is not evidence: two
#: snapshots can legitimately carry the same table, and `rankings` uses the
#: same floor for the same reason.
REPEAT_ALARM = 3


class HarvestAborted(CitycostError):
    """The run stopped on purpose and the corpus is incomplete. Carries the
    partial report, so an abort does not throw away the record of what did
    succeed and is now on disk."""

    def __init__(self, message: str, remedy: str = "",
                 report: dict | None = None) -> None:
        super().__init__(message, remedy)
        self.report = report or {}


# -- targets ---------------------------------------------------------------

def _signature_default(func, name: str) -> int:
    """The TTL for a kind of read belongs to the function that performs it.
    Restating `21600` and `86400` here would be two more literals for facts
    `rankings` already owns, agreeing on the day they were written. A signature
    that stops carrying one fails loudly rather than quietly substituting a
    shorter TTL, which would make the plan over-report."""
    param = inspect.signature(func).parameters.get(name)
    default = inspect.Parameter.empty if param is None else param.default
    if isinstance(default, bool) or not isinstance(default, int):
        raise CitycostError(
            f"{getattr(func, '__name__', func)}({name}=) does not carry an "
            f"integer default ({'absent' if param is None else repr(default)})"
            f", so a plan cannot state the freshness rule the fetch applies",
            "this client needs updating")
    return default


def _target_key(url: str) -> str:
    """The cache key `rankings.fetch` writes a ranking table under.

    The one fact this module restates about another, and load-bearing: get it
    wrong and the plan reports an empty corpus while 192 files sit on disk.
    Guarded by a round trip rather than by care — fetch with a stubbed
    transport, then assert `plan()` sees `fresh` — so a changed key prefix goes
    red instead of silently costing 192 requests.
    """
    return f"rank:{url}"


def _list_key(vertical: str) -> str:
    """Likewise for `rankings.snapshots`, and guarded by the same round trip."""
    seg = rankings.VERTICALS.get(vertical, vertical)
    return f"snapshots:{rankings.BASE}/{seg}/rankings.jsp"


def _pace_for(url: str) -> float:
    """The per-host floor, read off `net.MIN_INTERVAL` rather than restated.

    Naming 1.1 here would make the consent number stop tracking the throttle
    the moment somebody tuned it, and the floor is half of what the user agrees
    to: 192 x 1.1s is three and a half minutes of sleep before any response
    time, and a user who does not know that kills the run at ninety seconds.
    """
    host = urllib.parse.urlsplit(url).netloc
    return float(net.MIN_INTERVAL.get(host, net.DEFAULT_INTERVAL))


def _snapshot_ids(vertical: str, list_max_age: int) -> tuple[list, dict]:
    """The vertical's snapshot list, read from cache ONLY — never fetched.

    `ids` is None when the denominator is unknown, which differs from `[]` in
    the way this repository is about: a vertical whose list could not be read
    contributes 0 to a naive total, so the plan under-reports and the run
    over-spends. An empty list is unknown too — `0 of 0` reads as "complete",
    a check that cannot return false.
    """
    probe = net.cache_probe(_list_key(vertical), rankings.SCHEMA, list_max_age)
    info = {"list_state": probe["state"], "list_age_s": probe["age_s"]}
    if probe["state"] != "fresh":
        info["denominator_source"] = f"snapshot list {probe['state']}"
        return None, info
    payload = (probe["blob"] or {}).get("payload")
    if not isinstance(payload, list):
        info["denominator_source"] = "cached snapshot list is not a list"
        return None, info
    ids = [str(v) for v in payload if rankings.SNAPSHOT_ID.fullmatch(str(v))]
    if not ids:
        info["denominator_source"] = "cached snapshot list is empty"
        return None, info
    if len(ids) != len(payload):
        # Loud rather than quietly narrower: the difference is snapshots that
        # would never be harvested and never missed.
        info["unrecognised_ids"] = len(payload) - len(ids)
    info["denominator_source"] = "cached snapshot list"
    return ids, info


def _targets_for(vertical: str, ids: list, max_age: int, now: float | None):
    """One row per snapshot id, in the order Numbeo publishes them.

    Publication order is kept rather than sorted, so a run that aborts part-way
    leaves a prefix the user can predict. `current` cannot appear here: the
    `<select>` carries titled ids only — see `EXCLUDED_CURRENT`.
    """
    out = []
    for snap in ids:
        url = rankings._url(vertical, "city", snap, None)
        archival = rankings.is_archival(snap, now=now)
        probe = net.cache_probe(_target_key(url), rankings.SCHEMA, max_age,
                                archival=archival)
        out.append({
            "vertical": vertical, "snapshot": snap, "url": url,
            "state": probe["state"], "age_s": probe["age_s"],
            # Whether an id may be PINNED (no age clock, out of reach of prune)
            # is `rankings.is_archival`'s call, not ours; on 2026-08-25 it
            # refuses the current half-year, so two ids per vertical expire on
            # the ordinary clock. Saying so is a corpus rather than a belief.
            "pinned": archival,
        })
    return out


# -- plan ------------------------------------------------------------------

def plan(verticals=None, *, max_age: int | None = None,
         list_max_age: int | None = None, now: float | None = None) -> dict:
    """What a harvest would cost. Read-only, and structurally zero-network.

    Nothing here can reach the transport: the snapshot lists are read out of
    `net.cache_probe`'s returned blob rather than through `rankings.snapshots`,
    which would fetch on a miss. That is why a vertical with no cached list
    reports `unknown` and states the one request it would take to learn — a
    plan that went and found out would spend the consent it was about to ask
    for.
    """
    if max_age is None:
        max_age = _signature_default(rankings.fetch, "max_age")
    if list_max_age is None:
        list_max_age = _signature_default(rankings.snapshots, "max_age")

    wanted = sorted(rankings.VERTICALS) if verticals is None else list(verticals)
    unknown_names = [v for v in wanted if v not in rankings.VERTICALS]
    if unknown_names:
        raise CitycostError(
            f"unknown index {', '.join(sorted(unknown_names))}",
            "one of: " + ", ".join(sorted(rankings.VERTICALS)))

    rows, totals = [], dict.fromkeys(PROBE_STATES, 0)
    unknown, ages = [], []
    for vertical in wanted:
        ids, info = _snapshot_ids(vertical, list_max_age)
        if ids is None:
            unknown.append(vertical)
            # `list_request_cost` is stated because the plan is not free
            # either: with no cached lists the real ceiling is 199, not 192.
            rows.append({
                "vertical": vertical, "denominator": None, "targets": [],
                "denominator_state": "unknown", "snapshots": [],
                "states": dict.fromkeys(PROBE_STATES, 0), "to_fetch": None,
                "already_cached": None, "pinned": None, "unpinned_ids": [],
                "list_request_cost": 1, **info})
            continue
        targets = _targets_for(vertical, ids, max_age, now)
        states = _partition(targets)
        for k, v in states.items():
            totals[k] = totals.get(k, 0) + v
        ages += [t["age_s"] for t in targets
                 if t["state"] == "fresh" and t["age_s"] is not None]
        rows.append({
            "vertical": vertical, "denominator": len(targets),
            "denominator_state": "known", "snapshots": list(ids),
            "targets": targets, "states": states, "list_request_cost": 0,
            "to_fetch": len(targets) - states.get("fresh", 0),
            "already_cached": states.get("fresh", 0),
            "pinned": sum(1 for t in targets if t["pinned"]),
            "unpinned_ids": [t["snapshot"] for t in targets if not t["pinned"]],
            **info})

    known = [r for r in rows if r["denominator_state"] == "known"]
    denominator = sum(r["denominator"] for r in known) if not unknown else None
    to_fetch = sum(r["to_fetch"] for r in known) if not unknown else None
    list_requests = sum(r["list_request_cost"] for r in rows)
    pace = _pace_for(rankings._url("cost-of-living", "city", "2020", None))

    report = {
        # `rows` is printed with the plan so a selector that silently narrows
        # later shows up as a count that dropped rather than as a table that
        # still looks complete. `executed` and `requests_sent` are structural
        # rather than inferred: a consumer scripting `citycost harvest &&
        # next-step` reads a bare exit 0 as "the corpus is here", and a missing
        # key is not the same evidence as a stated `false`.
        "verticals": rows, "rows": len(rows),
        "verticals_available": len(rankings.VERTICALS),
        "scope": "all" if len(rows) == len(rankings.VERTICALS) else "subset",
        "denominator": denominator, "unknown_verticals": unknown,
        "denominator_state": "unknown" if unknown else "known",
        "states": totals, "already_cached": totals.get("fresh", 0),
        "to_fetch": to_fetch, "list_requests": list_requests,
        "requests_planned": (None if to_fetch is None
                             else to_fetch + list_requests),
        "pacing_floor_s": None if to_fetch is None else round(to_fetch * pace, 1),
        "min_interval_s": pace, "pacing_source": "net.MIN_INTERVAL",
        "cached_age_min_s": min(ages) if ages else None,
        "cached_age_max_s": max(ages) if ages else None,
        "pinned": sum(r["pinned"] or 0 for r in known),
        "unpinned": sum(len(r["unpinned_ids"]) for r in known),
        "excluded": [EXCLUDED_CURRENT], "max_age": max_age,
        "list_max_age": list_max_age, "schema": rankings.SCHEMA,
        "executed": False, "requests_sent": 0,
        "complete": None if unknown else (to_fetch == 0),
    }
    verify_partition(report)
    return report


def _partition(targets: list) -> dict:
    """Count by probe state — see `PROBE_STATES` for why an unheard-of one is
    still counted."""
    counts = dict.fromkeys(PROBE_STATES, 0)
    for t in targets:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    return counts


def verify_partition(report: dict) -> None:
    """A report whose columns do not add must fail loudly rather than print.
    "You have 140 of 192" and "you have 192 files, all under the previous
    schema" cost 52 and 192 requests; a partition keeps those two sentences
    apart, and one that does not sum has dropped a category. Called at
    construction so no consumer is handed an unsound report, and again before
    rendering, since a report can also arrive from a JSON round trip.
    """
    def check(what, got, want):
        if got != want:
            raise CitycostError(
                f"harvest plan does not add up: {what} is {got}, not {want}",
                "this client needs updating — a plan whose columns disagree "
                "cannot state a cost")

    for row in report.get("verticals") or []:
        if row.get("denominator_state") != "known":
            continue
        total = sum((row.get("states") or {}).values())
        fresh = (row.get("states") or {}).get("fresh", 0)
        check(f"{row['vertical']} classified", total, row["denominator"])
        # Compared value-for-value rather than as a sum: `or 0` on either side
        # would let a None pass as a zero, which is the failure this checks for.
        check(f"{row['vertical']} cached", row.get("already_cached"), fresh)
        check(f"{row['vertical']} to-fetch", row.get("to_fetch"), total - fresh)
    if report.get("denominator") is not None:
        check("total classified", sum(report.get("states", {}).values()),
              report["denominator"])


def _hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def render_plan(report: dict, *, color: bool = True) -> None:
    """The table a user consents to. Numbers on stdout, reasons on stderr."""
    verify_partition(report)
    headers = ["Vertical", "Total", "Cached", "Stale", "Wrong-schema",
               "Unreadable", "Absent", "To fetch", "Pinned"]

    def cells(name, states, total, to_fetch, pinned, *, counted=True):
        # `unknown` in the cell, never blank: a blank cell and an empty result
        # are indistinguishable, and one of them is a lie. The TOTAL row still
        # shows the counts it does have, rather than dashes that would hide a
        # partially readable corpus behind one unknown vertical.
        cols = ([str(states.get(k, 0)) for k in PROBE_STATES] if counted
                else ["—"] * len(PROBE_STATES))
        return ([name, "unknown" if total is None else str(total)] + cols
                + ["unknown" if to_fetch is None else str(to_fetch),
                   "—" if pinned is None else str(pinned)])

    body = [cells(r["vertical"], r.get("states") or {}, r["denominator"],
                  r["to_fetch"], r["pinned"],
                  counted=r["denominator_state"] == "known")
            for r in report["verticals"]]
    t = report.get("states") or {}
    body.append(cells("TOTAL", t, report["denominator"], report["to_fetch"],
                      report.get("pinned", 0)))
    print(render.text_table(headers, body, color=color))

    render.note(f"  {report['rows']} rows · one per vertical "
                f"({report['scope']}, {report['verticals_available']} exist) · "
                f"schema {report['schema']}")
    excluded = report["excluded"][0]
    render.note(f"  NOT harvested: {excluded['subject']} — {excluded['reason']}")
    render.note(f"    -> read it live with `{excluded['instead']}`")
    extra = sorted(set(t) - set(PROBE_STATES))
    if extra:
        render.note(f"  ! cache states this client does not display: {extra} — "
                    f"counted as to-fetch, which is the safe direction")
    if report["unknown_verticals"]:
        render.note(
            f"  ! denominator UNKNOWN for {', '.join(report['unknown_verticals'])}"
            f": the snapshot list is its only owner and is not cached. "
            f"{report['list_requests']} request(s) would learn it "
            f"(`citycost snapshots --index <vertical>`).")
        render.note("    -> --execute refuses while any denominator is unknown: "
                    "harvesting the verticals that resolved and reporting a "
                    "total is a partial result passing as complete")
    if report["already_cached"]:
        lo, hi = report["cached_age_min_s"], report["cached_age_max_s"]
        render.note(f"  already cached: {report['already_cached']} · age "
                    f"{net.fmt_age(lo or 0)}–{net.fmt_age(hi or 0)}")
    if report["max_age"] <= 0:
        render.note("  --max-age 0: nothing on disk may answer, so every target "
                    "is read live — including entries already pinned")
    if report["to_fetch"]:
        render.note(
            f"  to fetch: {report['to_fetch']} request(s)"
            + (f" + {report['list_requests']} to learn the unknown lists"
               if report["list_requests"] else "")
            + f" · pacing floor {report['to_fetch']} x "
              f"{report['min_interval_s']}s = "
              f"{_hms(report['pacing_floor_s'])} of sleep alone, before any "
              f"response time ({report['pacing_source']})")
    if report.get("unpinned"):
        render.note(f"  {report['pinned']} of "
                    f"{report['pinned'] + report['unpinned']} targets will be "
                    f"PINNED as archives (no age clock, out of reach of prune)"
                    f"; the other {report['unpinned']} name the current period, "
                    f"which nothing has measured to be finished, so they expire "
                    f"on the ordinary clock")
    if not report["executed"]:
        render.note("  plan only — nothing was fetched and no request was "
                    "sent (add --execute)")


# -- lock ------------------------------------------------------------------

def _holder_is_alive(holder: dict):
    """True / False / None, where None means *cannot tell* — which must not be
    read as dead. A lock written by another machine sharing this cache
    directory names a pid that means nothing here."""
    if not isinstance(holder, dict) or holder.get("host") != socket.gethostname():
        return None
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:                                   # pragma: no cover
        return None
    return True


@contextlib.contextmanager
def _lock():
    """Exclusive for the duration of a run; a second harvest is refused.

    `net._last_hit` is per-process, so two concurrent harvests are not paced
    relative to each other at all — they are two agents fanning out, the
    measured cause of the ban. Released in `finally`, and a kill that skips
    `finally` is recovered by the stale-holder rule rather than by a signal
    handler, which cannot cover SIGKILL and would leave the same hole while
    looking closed.
    """
    path = net.cache_dir() / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                          "started_at": int(time.time())})
    for attempt in (0, 1):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                holder = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                holder = None
            alive = _holder_is_alive(holder)
            if alive is False and attempt == 0:
                # The holder is gone. Reclaiming is what keeps a killed run
                # from wedging the command permanently.
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(
                (holder or {}).get("started_at", 0)))
            who = (f"pid {holder.get('pid')} on {holder.get('host')} since "
                   f"{started}" if isinstance(holder, dict)
                   else "an unreadable lock file")
            raise CitycostError(
                f"another harvest holds the lock ({who})",
                f"wait for it to finish, or remove {path} if that process is "
                f"gone — two harvests against one address are two unpaced "
                f"agents, which is what earns the block")
        else:
            os.write(fd, payload.encode("utf-8"))
            os.close(fd)
            break
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


# -- run -------------------------------------------------------------------

def _is_empty(exc: SourceUnavailable) -> bool:
    """A permanent answer about that URL rather than a failure; counting it as
    one makes exit 0 unreachable forever. See `EMPTY_MARKER`."""
    return EMPTY_MARKER in (getattr(exc, "message", "") or "")


def _distinct_tables(tables: list) -> dict:
    """Cross-snapshot distinctness for one vertical, over the tables just
    fetched.

    The repository's signature defect at harvest scale. `?title=` is a request,
    not a guarantee, and it has no error path to fall into: if it is ever
    accepted and ignored, harvest stores N copies of the current table and
    reports N successes and N files of the right size, after which every later
    `trend` reads the flat line from cache, offline, forever. The harvest holds
    the whole vertical at once — strictly more evidence than `trend` ever has.

    Keyed on parsed rows, not row count alone, because two snapshots can carry
    the same number of cities. Not shared with
    `rankings._is_one_table_repeated`: that ranges over one city's series and
    keys on `(metrics, of)`, this over whole tables — one function would
    silently narrow one of them.
    """
    counts: dict = {}
    blank = 0
    for t in tables:
        fp = rankings.panel_fingerprint(t)
        if not fp:
            # `panel_fingerprint` returns "" for a table with no rows, exactly
            # so that two empty parses do not read as identical. Honouring that
            # here matters in the direction that hurts: a vertical whose
            # snapshots legitimately parse empty would otherwise trip the
            # repeat alarm and exit 1 forever, which is the standing-red gate
            # that teaches a reader to ignore it.
            blank += 1
            continue
        counts[fp] = counts.get(fp, 0) + 1
    top = max(counts.values()) if counts else 0
    return {"of": len(tables), "distinct": len(counts), "max_repeat": top,
            "unfingerprintable": blank, "repeated": top >= REPEAT_ALARM}


def _stored(targets: list, max_age: int) -> int:
    """How many of the tables this run fetched are actually ON DISK.

    A successful fetch is the writer's report of its own work; this is the
    read-back. A read-only home directory or a full disk would otherwise let a
    192-request harvest report success having stored nothing — the one failure
    this command cannot survive, since the requests are not repeatable while
    the address is banned. The predicate is *present and under the current
    schema* (`fresh` OR `stale`): whether an entry may be SERVED depends on a
    TTL, and keying on `fresh` would call a corpus lost after `--max-age 0`.
    """
    return sum(1 for t in targets
               if net.cache_probe(_target_key(t["url"]), rankings.SCHEMA,
                                  max_age, archival=t["pinned"])["state"]
               in ("fresh", "stale"))


def _failure_record(target: dict, exc) -> dict:
    return {
        "vertical": target["vertical"], "snapshot": target["snapshot"],
        "url": target["url"],
        # `status` and `exit_code` stay separate because 4 is not an HTTP
        # status and 403 is not an exit code: the first reader to compare one
        # against the other's vocabulary gets a plausible answer.
        "status": getattr(exc, "status", None),
        "exit_code": getattr(exc, "exit_code", None),
        "message": getattr(exc, "message", str(exc)),
        "remedy": getattr(exc, "remedy", ""),
    }


def run(report: dict) -> dict:
    """Execute a plan. One loop, one transport, stopping at the first refusal
    — and it takes the report `plan()` returned rather than re-deriving it."""
    if not isinstance(report, dict) or "verticals" not in report:
        raise CitycostError(
            "harvest.run() takes the report harvest.plan() returned",
            "call plan() first — re-deriving the targets here is how the number "
            "consented to and the number spent come apart")
    if report.get("executed"):
        raise CitycostError(
            "this plan has already been executed",
            "call plan() again: a report states what the cache held at one "
            "moment, and re-running a spent one would refetch what it fetched")
    if report.get("unknown_verticals"):
        raise CitycostError(
            "refusing to harvest: the denominator is unknown for "
            + ", ".join(report["unknown_verticals"]),
            "run `citycost snapshots --index <vertical>` for each first — "
            "harvesting the verticals that resolved and reporting a total is a "
            "partial result passing as complete, and that total is the number "
            "you consented to")
    verify_partition(report)

    if fallback.settings().mode == "always":
        # Refused before request 1, not mid-run: a sweep every request of which
        # is rerouted is one the origin has already refused, and routing 192 of
        # them through a borrowed or paid egress removes the only feedback
        # signal that the volume was wrong.
        raise CitycostError(
            "harvest refuses --fetch-mode always",
            "the external fetcher is a repair, not a licence to sweep harder. "
            "Run this from a network that is not blocked; `auto` still reroutes "
            "a single blocked read, and harvest stops at the first one")

    with _lock():
        return _run_locked(report)


class _Abort(Exception):
    """Internal: a reason to stop, raised where it is detected and converted to
    `HarvestAborted` where the partial report exists, so the detection site
    cannot forget to attach the record of what got cached."""

    def __init__(self, reason: str, message: str, remedy: str) -> None:
        super().__init__(message)
        self.reason, self.message, self.remedy = reason, message, remedy


def _fetch_one(target: dict, max_age: int):
    """`(outcome, table, exc)` for one target; the two abort classes raise. The
    clause order is the policy: a layout change and a blocked status stop the
    run, everything else is per-item."""
    try:
        table = rankings.fetch(target["vertical"], view="city",
                               snapshot=target["snapshot"], max_age=max_age)
    except LayoutChanged as exc:
        # Not a per-item failure: the fix is in this repo, so the remaining
        # requests produce the same parse failure over and over — pure waste
        # against a source that bans, and a list saying one thing 180 times.
        raise _Abort("layout_changed", exc.message, exc.remedy) from exc
    except SourceUnavailable as exc:
        if fallback.is_blocked(exc):
            # The whole run, not this item and not this vertical: the block is
            # address-level and does not slide, so continuing spends the
            # address on behalf of whoever uses it next.
            raise _Abort("blocked", exc.message, exc.remedy) from exc
        return ("empty" if _is_empty(exc) else "failed"), None, exc
    return "ok", table, None


def _progress(report: dict, st: dict, **extra) -> dict:
    """The report as it stands. One builder, so the partial report an abort
    publishes carries the same fields as a completed one — a consumer forced to
    branch on which kind it got will read one of them wrong."""
    done = st["ok"] + st["failed"] + st["empty"]
    stored = _stored(st["landed"], report["max_age"])
    out = {
        **report, "executed": True, "planned": st["total"], "attempted": done,
        "requests_sent": done, "live_reads": net.reads()["live"] - st["live0"],
        "fetched": st["ok"], "stored": stored, "ok": st["ok"],
        "failed": st["failed"], "empty": st["empty"],
        "failures": list(st["failures"]), "empties": list(st["empties"]),
        "distinct": dict(st["distinct"]), "aborted": None,
        "rerun_requests": st["total"] - done + st["failed"],
        "elapsed_s": round(time.time() - st["started"], 1), **extra,
    }
    repeated = [v for v, d in st["distinct"].items() if d["repeated"]]
    out["repeated_verticals"] = repeated
    out["complete"] = bool(
        out["aborted"] is None and st["failed"] == 0 and not repeated
        and done == st["total"] and stored == st["ok"]
        and report.get("denominator") is not None)
    return out


def _note_distinct(vertical: str, tables: list, st: dict) -> None:
    d = _distinct_tables(tables)
    st["distinct"][vertical] = d
    blank = (f" · {d['unfingerprintable']} parsed with no rows, incomparable"
             if d["unfingerprintable"] else "")
    render.note(f"  {vertical}: fetched {d['of']} · distinct tables "
                f"{d['distinct']} of {d['of'] - d['unfingerprintable']}{blank}")
    if not d["repeated"]:
        return
    render.note(f"  ! {vertical}: {d['max_repeat']} fetched snapshots returned "
                f"an IDENTICAL table. That is not a stable index; that is one "
                f"table fetched {d['max_repeat']} times, now cached under "
                f"{d['max_repeat']} snapshot ids where every later `trend` "
                f"reads it offline.")
    render.note("    -> `?title=` is being ignored upstream. Verify with "
                "`citycost rank --snapshot 2014 --top 3` against "
                "`--snapshot current`.")


def _run_locked(report: dict) -> dict:
    st = {"ok": 0, "failed": 0, "empty": 0, "failures": [], "empties": [],
          "distinct": {}, "landed": [], "started": time.time(),
          "live0": net.reads()["live"]}
    st["total"] = sum(1 for r in report["verticals"] for t in r["targets"]
                      if t["state"] != "fresh")
    index, target = 0, None

    def stop(exc: _Abort) -> HarvestAborted:
        # Built here and raised at the call site, so control flow reads as
        # control flow: a helper that always raises leaves the next editor
        # working out whether the line after it runs.
        out = HarvestAborted(
            f"{exc.message} — harvest stopped at item {index} of {st['total']}; "
            f"{st['ok']} item(s) are cached and a rerun will skip them",
            exc.remedy, report=_progress(report, st, aborted={
                "reason": exc.reason, "at_item": index, "of": st["total"],
                "vertical": (target or {}).get("vertical"),
                "snapshot": (target or {}).get("snapshot")}))
        out.__cause__ = exc
        return out

    for row in report["verticals"]:
        tables = []
        for target in [t for t in row["targets"] if t["state"] != "fresh"]:
            index += 1
            events_before = len(fallback.events())
            try:
                outcome, table, exc = _fetch_one(target, report["max_age"])
            except _Abort as abort:
                raise stop(abort)
            if outcome == "ok":
                st["ok"] += 1
                tables.append(table)
                st["landed"].append(target)
            elif outcome == "empty":
                st["empty"] += 1
                st["empties"].append(_failure_record(target, exc))
                render.note(f"  · {target['vertical']} {target['snapshot']}: "
                            f"upstream has no table for this selection (not a "
                            f"failure; a rerun asks again)")
            else:
                st["failed"] += 1
                st["failures"].append(_failure_record(target, exc))
                # Streamed as it happens rather than batched to the end: a
                # five-minute foreground command over a ban-prone source will
                # be killed, and a batched list is lost entirely when it is.
                render.note(f"  ! {target['vertical']} {target['snapshot']} "
                            f"FAILED ({exc.status or 'no status'}): "
                            f"{exc.message}")
            if len(fallback.events()) > events_before:
                # Checked after the item is counted, whatever its outcome: the
                # egress was spent either way and the item is already on disk.
                raise stop(_Abort(
                    "rerouted",
                    "a request was rerouted through the external fetcher",
                    "the reroute is a repair, not a licence to sweep harder. "
                    "Run the harvest from a network that is not blocked"))
        target = None
        if tables:
            _note_distinct(row["vertical"], tables, st)

    out = _progress(report, st)
    if out["stored"] < out["ok"]:
        render.note(f"  ! {out['ok'] - out['stored']} of {out['ok']} fetched "
                    f"table(s) are NOT on disk — the requests were spent and "
                    f"nothing was kept. Check that {net.cache_dir()} is "
                    f"writable before rerunning")
    live = out["live_reads"]
    if live > out["attempted"]:
        # The denominator check on the run itself: the transport is the one
        # place that knows how many live reads happened, and a run that made
        # more than it attempted items has a second path to the network that
        # the plan never costed.
        render.note(f"  ! {live} live reads for {out['attempted']} planned "
                    f"items — harvest sent requests its plan did not cost; "
                    f"please report it")
    elif live < out["attempted"]:
        render.note(f"  {out['attempted'] - live} item(s) were answered from "
                    f"cache after the plan was made")
    return out


def exit_code(report: dict) -> int:
    """0 = everything planned succeeded or nothing was needed · 1 = completed
    with a failure, a repeated-table alarm, or a table that did not land on
    disk · 2 = refused or aborted.

    This inverts `compare`, where one city failing among several still exits 0.
    `compare`'s product is per-city answers that are independently useful with
    N/A cells the reader sees; harvest's product is not data at all but a CLAIM
    ABOUT COMPLETENESS, and one that is 94% true is a false claim with a
    plausible shape. The usual objection — non-zero for 1-of-N trains people to
    ignore it — does not apply: the rerun costs exactly the outstanding
    requests, and `empty` is excluded so the run can reach 0 and stay there.
    """
    if report.get("aborted"):
        return 2
    if not report.get("executed"):
        return 0
    if report.get("failed") or report.get("repeated_verticals"):
        return 1
    if report.get("stored") is not None and report["stored"] < report.get("ok", 0):
        return 1
    return 0


def render_report(report: dict) -> None:
    """The summary, after the run. Failures are named twice — streamed as they
    happen and listed again here — because the streamed copy survives a kill
    and this one survives a scrollback."""
    render.note(
        f"  harvest: planned {report.get('planned')} · fetched "
        f"{report.get('fetched')} · failed {report.get('failed')} · empty "
        f"{report.get('empty')} · stored {report.get('stored')} · already "
        f"cached {report.get('already_cached')} · of a denominator of "
        f"{report.get('denominator')} · {report.get('elapsed_s')}s")
    for f in report.get("failures") or []:
        render.note(f"  ! {f['vertical']} {f['snapshot']}: {f['message']}")
        if f.get("remedy"):
            render.note(f"    -> {f['remedy']}")
    if report.get("empty"):
        render.note(f"  {report['empty']} snapshot(s) have no table upstream: a "
                    f"permanent answer, not a failure, excluded from the code")
    if report.get("rerun_requests"):
        render.note(f"  rerun costs {report['rerun_requests']} request(s)")
    if not report.get("complete"):
        render.note("  ! this corpus is INCOMPLETE — a later `trend` reports "
                    "the missing snapshots as 'not ranked in this snapshot', "
                    "an absence that reads as 'not in that table'")
