"""`citycost harvest` — saying the numbers, or refusing to say them.

The third question this command asks, and the smallest: *may this report be
printed, and in what words?* `harvest_plan.py` decides what a sweep would cost
and `harvest.py` spends it; neither of them owns a sentence. Open this file for
the wording of a line, the order of a column, the shape a failure takes in the
payload, or the grade a finished report gets (`exit_code`). Open
`harvest_plan.py` for where a number came from, and `harvest.py` for what was
spent to get it.

Nothing here reads the corpus or reaches the transport. Every function takes a
report dict and returns a verdict or writes to stderr, which is the seam's
point rather than an accident: a renderer that could go and check would be a
second owner of the numbers it is displaying, and two owners of one number are
two numbers, both printed as facts. It is also why this is the LOWEST of the
three layers — `plan()` calls `verify_partition` at construction so that no
consumer is ever handed an unsound report, and the module that verifies must
not be able to import the module it verifies.
"""

from __future__ import annotations

from . import net, render
from .errors import CitycostError

# -- vocabulary ------------------------------------------------------------

#: The states `net.cache_probe` can return, in the order the plan prints them.
#: A second owner of that vocabulary is tolerable only because the partition
#: counts whatever the probe actually says and merely *displays* it through
#: this tuple — a state added to `net` lands in the totals and in the to-fetch
#: column rather than being silently dropped.
PROBE_STATES = ("fresh", "stale", "schema_mismatch", "unreadable", "absent")

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


# -- soundness -------------------------------------------------------------

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
    if "requests_planned" in report:
        # Compared against its parts rather than trusted: this is the number
        # printed as the thing being consented to, and a sum nobody checks
        # agrees with its parts on the day it was written. A missing part is
        # checked as None rather than defaulted to 0 — a plan that lost a
        # component states a cost it did not compute.
        parts = (report.get("to_fetch_known"), report.get("list_requests"))
        check("requests planned", report["requests_planned"],
              sum(parts) if None not in parts else None)



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


def _hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


# -- notes on stderr -------------------------------------------------------

def _note_repeated(report: dict) -> None:
    """Name every vertical whose tables repeat, from whichever reading saw it.

    Called by the plan AND by the run summary, because the alarm outlives the
    run that raised it: exit 1 with nothing on stderr explaining why is an exit
    code that gets ignored, and the case this exists for is precisely the one
    where no single fetch batch held enough evidence to say anything.
    """
    for vertical in report.get("repeated_verticals") or []:
        disk = (report.get("distinct_on_disk") or {}).get(vertical) or {}
        # The disk reading is preferred and cannot be assumed: a run whose
        # writes never landed has a poisoned batch and a clean corpus, and
        # keying on the disk there would announce "0 of the 0 tables".
        d = disk if disk.get("repeated") else (
            (report.get("distinct") or {}).get(vertical) or {})
        where = "already on disk" if d is disk else "fetched by this run"
        render.note(f"  ! {vertical}: {d.get('max_repeat')} of the "
                    f"{d.get('of')} tables {where} are IDENTICAL. That is one "
                    f"table under {d.get('max_repeat')} snapshot ids, which "
                    f"every later `trend` reads offline as a flat line.")
        render.note("    -> `?title=` was ignored upstream for those reads. "
                    "Verify with `citycost rank --snapshot 2014 --top 3` "
                    "against `--snapshot current`, then re-read them with "
                    "--max-age 0, which is the one non-destructive way to "
                    "replace a poisoned pin")


def _note_run_warnings(out: dict) -> None:
    """The two checks on the run itself, at one owner because both abort paths
    need them too.

    They used to sit after the target loop, which every abort raises out of —
    so on the one path where an unexplained live read or an unwritten table is
    most expensive, the check that would have named it never ran, and a warning
    that cannot be reached on the path it was written for is not a warning.
    """
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


def _note_distinct(vertical: str, d: dict) -> None:
    """Say what a run's own fingerprints found, for one vertical.

    Takes the summary `harvest_plan._distinct_tables` computed rather than the
    tables, so the alarm keeps one owner and one threshold: a renderer that
    re-derived it would be a second answer to "is this corpus repeating",
    printed beside an exit code computed from the first.
    """
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


# -- the two tables a user reads -------------------------------------------

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
    for row in report["verticals"]:
        narrowed = row.get("unrecognised_ids")
        if not narrowed:
            continue
        # The count existed and reached no reader, which is the same as not
        # existing: a silently narrowed list is a denominator that merely looks
        # smaller, and "3 of 3" over a list that named 5 reads as complete.
        render.note(f"  ! {row['vertical']}: {narrowed} cached list entr"
                    f"{'y' if narrowed == 1 else 'ies'} name a selection this "
                    f"client does not recognise as a snapshot id and were "
                    f"narrowed out — they are snapshots that would never be "
                    f"harvested and never missed. Refresh the list with "
                    f"`citycost snapshots --index <vertical>`")
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
    if report["requests_planned"]:
        # Keyed on `requests_planned`, not on `to_fetch`: `to_fetch` is None on
        # the first run, so this whole line — the consent number, the pacing
        # floor, and the list-request clause below it — printed for everyone
        # except the person who had never run the command.
        floor = report["requests_planned_state"] == "floor"
        render.note(
            f"  to fetch: {'at least ' if floor else ''}"
            f"{report['requests_planned']} request(s)"
            + (f" — {report['to_fetch_known']} table(s) counted + "
               f"{report['list_requests']} to learn the unknown lists, which "
               f"then name an unknown number more"
               if report["list_requests"] else "")
            + f" · pacing floor {report['requests_planned']} x "
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
    _note_repeated(report)
    if not report["executed"]:
        render.note("  plan only — nothing was fetched and no request was "
                    "sent (add --execute)")


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
    _note_repeated(report)
    if not report.get("complete"):
        render.note("  ! this corpus is INCOMPLETE — a later `trend` reports "
                    "the missing snapshots as 'not ranked in this snapshot', "
                    "an absence that reads as 'not in that table'")


# -- the grade the shell reads ---------------------------------------------

def exit_code(report: dict) -> int:
    """0 = everything planned succeeded or nothing was needed · 1 = a failure,
    a table that did not land on disk, or a repeated-table alarm — which is
    graded on a plan-only report too, because it describes the FILES rather
    than the run that noticed them · 2 = refused or aborted.

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
    if report.get("repeated_verticals"):
        # Graded before `executed`, because a repeated corpus is a fact about
        # the FILES rather than about the run that noticed them: a plan-only
        # run over the same files used to report 0, so the alarm was cleared by
        # doing nothing at all.
        return 1
    if not report.get("executed"):
        return 0
    if report.get("failed"):
        return 1
    if report.get("stored") is not None and report["stored"] < report.get("ok", 0):
        return 1
    return 0
