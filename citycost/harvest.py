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
planner** (`harvest_plan._target_key`, `run` — the number printed and the
number spent come from one predicate over one target list), **one harvest at a
time** (`_lock` — `net._last_hit` is per-process, so two harvests are two
unpaced agents, the measured cause of the ban), and **stop at the first
refusal** (`_fetch_one` — the block is address-level and does not slide). The
documented alternative, a shell loop over `citycost rank --snapshot <id>`, has
none of them: 192 separate processes reset the throttle 192 times and hammer at
full speed.

Deliberately absent: an `--out`/`--export` destination (192 parsed Numbeo
tables in a directory the user names is a redistributed dataset, whatever the
flag was for), a `--source` flag (city pages are the 558x-wrong channel and
nomads.com states it is not a bulk source), and a manifest (a second owner of
"what has been harvested", drifting from the cache the first time an entry is
pruned). The cache files are the record; resumability reads them back through
the probe the plan used.

**Three files, cut by the question each answers rather than by where the page
broke.** THIS one owns spending a plan: the lock, the transport loop, the abort
rules, and the accounting of what a run actually did — everything that is only
knowable by sending a request. `harvest_plan.py` owns everything answerable
without sending one: the cache keys, the target list, the partition, the
denominator, the pacing floor, the read-backs, the repeated-table alarm.
`harvest_report.py` owns saying it, and refusing to say a report whose columns
do not add up. A reader chasing "why did it stop at item 1 of 177" is in the
right file here; "why does it say 192" is `harvest_plan.py` and "why is that
line worded like that" is `harvest_report.py`.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time

from . import fallback, net, rankings, render
from .errors import CitycostError, LayoutChanged, SourceUnavailable

# -- re-exports ------------------------------------------------------------
#
# `panel.py`, `parser.py` and this project's tests reach every one of these
# through `harvest.`, and none of them moves because the module split is not
# their business. A re-export is NOT a second owner: each name below is DEFINED
# in exactly one place — the module it is imported from — so editing that
# definition edits every reader of it, and there is no copy here to drift. What
# would be a second owner is a wrapper restating a body, and there is none.
from .harvest_plan import (  # noqa: F401
    REPEAT_ALARM, _distinct_fingerprints, _distinct_on_disk, _distinct_tables,
    _fingerprint_of, _list_key, _outstanding, _pace_for, _partition,
    _signature_default, _snapshot_ids, _stored, _target_key, _targets_for,
    plan,
)
from .harvest_report import (  # noqa: F401
    EXCLUDED_CURRENT, PROBE_STATES, _failure_record, _hms, _note_distinct,
    _note_repeated, _note_run_warnings, exit_code, render_plan, render_report,
    verify_partition,
)

#: `rankings._parse` raises this when upstream answers "no such selection" — a
#: successful, permanent answer about that URL rather than a failure. It is
#: prose, which `errors.py` exists to discourage, and there is no status to key
#: on instead: this and a dead socket both arrive as
#: `SourceUnavailable(status=None)`. So a test raises the real exception out of
#: `rankings._parse` and asserts this still matches — reword it upstream and
#: that goes red, rather than every dead snapshot silently becoming a failure
#: no rerun can clear, which makes exit 0 unreachable.
EMPTY_MARKER = "has no ranking table for that selection"

#: Name of the exclusive lock, inside `net.cache_dir()` — the process-shared,
#: `CITYCOST_CACHE_DIR`-overridable location the test sandbox already pins.
LOCK_NAME = "harvest.lock"


class HarvestAborted(CitycostError):
    """The run stopped on purpose and the corpus is incomplete. Carries the
    partial report, so an abort does not throw away the record of what did
    succeed and is now on disk."""

    def __init__(self, message: str, remedy: str = "",
                 report: dict | None = None) -> None:
        super().__init__(message, remedy)
        self.report = report or {}


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


def resolve(report: dict, *, now: float | None = None) -> dict:
    """Spend exactly the list requests the plan named, then plan again.

    Fetches no tables. `plan()` is structurally zero-network and `run()`
    refuses an unknown denominator, so the first `harvest` on any machine — and
    every one a day after the last, since a snapshot list expires on a clock —
    ended in seven `citycost snapshots` commands: seven processes, each
    resetting the per-process throttle, which is the shell-loop shape the
    module docstring warns against. This is that step in one paced process,
    under the same refusals as the run: `--fetch-mode always` is refused before
    request one, a blocked status stops at that list, and a rerouted read
    refuses to continue. The number spent is `report["list_requests"]`, which
    the plan already printed as the cost of learning.

    Measured 2026-09-02, the first live harvest: all seven lists had expired.
    """
    if not isinstance(report, dict) or "verticals" not in report:
        raise CitycostError(
            "harvest.resolve() takes the report harvest.plan() returned",
            "call plan() first")
    if report.get("executed"):
        raise CitycostError(
            "this plan has already been executed",
            "call plan() again: a spent report states what the cache held "
            "before the run, not what it holds now")
    wanted = [r["vertical"] for r in report["verticals"]]
    unknown = list(report.get("unknown_verticals") or [])
    # Refused up front regardless of whether anything is unknown, so `resolve`
    # honours the "same refusals as the run" it promises — `run()` refuses
    # `always` unconditionally, and a `--resolve` that quietly succeeded under
    # it would be the one path that does not.
    if fallback.settings().mode == "always":
        raise CitycostError(
            "harvest refuses --fetch-mode always",
            "the external fetcher is a repair, not a licence to sweep harder. "
            "Run this from a network that is not blocked")
    if not unknown:
        return plan(wanted, max_age=report["max_age"],
                    list_max_age=report["list_max_age"], now=now)
    with _lock():
        for i, vertical in enumerate(unknown, 1):
            events_before = len(fallback.events())
            try:
                rankings.snapshots(vertical, max_age=0)
            except SourceUnavailable as exc:
                # The same partial state is on disk whichever failure this is —
                # a rerun skips the lists already learned — so both branches say
                # so. Reporting it only for the address-level block left the
                # commoner failure (a transient 500, a URLError) under-informing
                # about the identical cache.
                learned = (f"; {i - 1} list(s) were learned and are cached"
                           if i > 1 else "")
                if fallback.is_blocked(exc):
                    # Address-level and it does not slide: the next list
                    # request spends the address for whoever uses it next.
                    raise CitycostError(
                        f"{exc.message} — resolving stopped at list {i} of "
                        f"{len(unknown)} ({vertical}){learned}",
                        exc.remedy) from exc
                raise CitycostError(
                    f"could not learn the snapshot list for {vertical} "
                    f"(list {i} of {len(unknown)}){learned}: {exc.message}",
                    exc.remedy) from exc
            if len(fallback.events()) > events_before:
                raise CitycostError(
                    f"the snapshot list for {vertical} was rerouted through "
                    f"the external fetcher (list {i} of {len(unknown)})",
                    "the reroute is a repair, not a licence to sweep harder. "
                    "Run the harvest from a network that is not blocked")
    return plan(wanted, max_age=report["max_age"],
                list_max_age=report["list_max_age"], now=now)


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
    # An aborting request has no outcome to be counted under — `_fetch_one`
    # raises only after the transport has answered — so `ok + failed + empty`
    # is one short of what was spent on both abort paths. Carried as its own
    # field rather than folded in: a consumer subtracting the outcomes from
    # `attempted` would get the right number and no idea what it meant.
    attempted = done + st["unresolved"]
    stored = _stored(st["landed"], report["max_age"])
    out = {
        **report, "executed": True, "planned": st["total"],
        "attempted": attempted, "requests_sent": attempted,
        "unresolved": st["unresolved"],
        "live_reads": net.reads()["live"] - st["live0"],
        "fetched": st["ok"], "stored": stored, "ok": st["ok"],
        "failed": st["failed"], "empty": st["empty"],
        "failures": list(st["failures"]), "empties": list(st["empties"]),
        "distinct": dict(st["distinct"]), "aborted": None,
        "rerun_requests": _outstanding(report),
        "elapsed_s": round(time.time() - st["started"], 1), **extra,
    }
    # The union of two readings, because neither contains the other. The disk
    # holds tables this run did not fetch — the case that made the alarm
    # forget — and this run holds tables that never reached the disk, which is
    # a corpus that inspects clean while the requests were spent on one table
    # served five times.
    on_disk = _distinct_on_disk(report.get("verticals") or [],
                                report["max_age"])
    out["distinct_on_disk"] = on_disk
    repeated = sorted({v for v, d in st["distinct"].items() if d["repeated"]}
                      | {v for v, d in on_disk.items() if d["repeated"]})
    out["repeated_verticals"] = repeated
    out["complete"] = bool(
        out["aborted"] is None and st["failed"] == 0 and not repeated
        and done == st["total"] and stored == st["ok"]
        and report.get("denominator") is not None)
    return out


def _run_locked(report: dict) -> dict:
    st = {"ok": 0, "failed": 0, "empty": 0, "unresolved": 0, "failures": [],
          "empties": [], "distinct": {}, "landed": [], "started": time.time(),
          "live0": net.reads()["live"]}
    st["total"] = sum(1 for r in report["verticals"] for t in r["targets"]
                      if t["state"] != "fresh")
    index, target = 0, None

    def stop(exc: _Abort) -> HarvestAborted:
        # Built here and raised at the call site, so control flow reads as
        # control flow: a helper that always raises leaves the next editor
        # working out whether the line after it runs.
        partial = _progress(report, st, aborted={
            "reason": exc.reason, "at_item": index, "of": st["total"],
            "vertical": (target or {}).get("vertical"),
            "snapshot": (target or {}).get("snapshot")})
        _note_run_warnings(partial)
        out = HarvestAborted(
            f"{exc.message} — harvest stopped at item {index} of {st['total']}; "
            f"{st['ok']} item(s) are cached and a rerun will skip them",
            exc.remedy, report=partial)
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
                # Counted before the report is built: the transport already
                # answered this one, so leaving it out reports fewer requests
                # than were spent against an address that bans by address.
                st["unresolved"] += 1
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
            # Computed here and merely SAID over there: the alarm keeps one
            # owner in `harvest_plan`, and the run keeps ownership of its own
            # state rather than handing `st` to a renderer to write into.
            st["distinct"][row["vertical"]] = d = _distinct_tables(tables)
            _note_distinct(row["vertical"], d)

    out = _progress(report, st)
    _note_run_warnings(out)
    return out
