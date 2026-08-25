"""`movers` — a panel-wide diff of ONE index column between TWO snapshots.

`rank` reads one table, `trend` reads one city across many; this reads two and
asks which of the ~400 cities in both moved. Two bulk reads answer it — "diff
every consecutive pair" would be thirty requests against a source that bans
this address for seven days at a time.

Everything in this command exists because its characteristic failure is a
*complete, well-formed, catastrophically wrong* table rather than an error —
`?title=` silently ignored so every delta is 0.0, a collapsed join answering
from a 3% sample, a column that means two different things in the two
snapshots. Each has an observer, and each observer says which wrong answer it
is there to prevent.

No verdict about the DATA is rendered here and none should be rendered
downstream. The sign's desirability depends on the column AND the reader — a
rent index rising is bad for a renter and good for a landlord, a salary index
falling is bad, `Gross Rental Yield` has no single answer — and this repository
has already shipped one "lower is better" rule that marked the worst-paying
city as best. `verdict()` below judges the RUN, never the cities.

WHAT THIS FILE OWNS: the command's outside. What a run fetches and what that
costs (`compare_snapshots`), what the caller receives back (`order_movers`,
`present`), and what the exit code claims (`verdict`). The two questions inside
a run have a file each, and every name of theirs a caller reaches through
`movers` is re-exported below — their private helpers stay where they are
defined — so no caller has to know which file holds what:

  `comparable`  may these two tables be compared, and between exactly what? —
                snapshot ids, column label, row keys, and the whole-table
                fingerprint. Every refusal that fires before a delta exists.
  `diffing`     what does the comparison say, and can the movement be believed?
                — the arithmetic, the join, the statistics, and the observers
                that read the deltas back.
"""

from __future__ import annotations

from . import net, rankings
# Imported for use AND re-exported: `citycost/panel.py`, `citycost/parser.py`,
# the README and this command's tests all reach these names through `movers`,
# and moving a definition must not move the name a caller already imports.
# Re-exporting is not a second owner — each name below is DEFINED in exactly
# one place, the file it is imported from, and that is where it is edited.
from .comparable import (EXCLUDED_STATUSES, _snapshot_sort_key, index_by_key,
                         join_key, resolve_column, resolve_snapshots,
                         same_panel)
from .diffing import (BASIS, DELTA_REASONS, DISTINCT_DELTA_FLOOR,
                      DISTINCT_DELTA_MIN_SAMPLE, FROZEN_COLUMN_MIN_SAMPLE,
                      METRIC_ABSENT_IN_BOTH, METRIC_ABSENT_IN_FROM,
                      METRIC_ABSENT_IN_TO, MIN_OVERLAP, NOT_FINITE,
                      PRESENTATION_KEYS, REBASE_DIRECTION_SHARE,
                      REBASE_MIN_SAMPLE, ROW_KEYS, STATUSES, abs_change, diff,
                      pct_change)
from .errors import SourceUnavailable
from .net import DEFAULT_MAX_AGE

#: Example names beside a count on stderr. One owner, so the number the note
#: promises and the number it prints cannot disagree.
SAMPLE_N = 5

#: Text and markdown truncate by default; machine formats never do. A default
#: that silently truncated `--json` would make a consumer record "396 cities
#: compared, 20 moved" — the 1.2.0 `--full`-shaping-`raw` defect, reworn.
DEFAULT_TOP_TEXT = 20

ORDERS = ("abs", "pct")
DIRECTIONS = ("both", "rising", "falling")

#: Declared explicitly so a column survives in the header even when every value
#: is empty, and so `status` can never be dropped: without it an empty delta
#: cell means three different things (new, delisted, or `?` in one snapshot) —
#: the absent-is-not-zero rule re-losing at the CSV boundary.
CSV_COLUMNS = ["place", "city", "country", "status", "reason",
               "value_from", "value_to", "delta", "pct",
               "rank_from", "of_from", "rank_to", "of_to"]


# ------------------------------------------------------- ordering, framing --

def order_movers(rows, order: str = "abs", direction: str = "both") -> list:
    """Filter by direction, then sort by magnitude, descending, both ways.

    Pure, and one owner, so the text path, the `--md` path and the `--json`
    path cannot drift into three orderings.

    `abs` is the default because each ordering has a failure mode and only one
    is visible on screen. A percentage is inflated by a small base, and the
    small-base end of a Numbeo index is where the sample is thinnest — so
    `--order pct` puts measurement noise at the top *by construction*, and it
    looks dramatic. The absolute ordering understates a low-magnitude column
    like `Gross Rental Yield`, which the reader can see because the numbers on
    screen are small. A row whose sort figure is `None` sinks to the bottom
    rather than sorting as 0: a city with no percentage (non-positive base) is
    not a city that moved 0%.
    """
    if order not in ORDERS:
        raise SourceUnavailable(f"unknown --order '{order}'",
                                f"one of: {', '.join(ORDERS)}")
    if direction not in DIRECTIONS:
        raise SourceUnavailable(f"unknown direction '{direction}'",
                                f"one of: {', '.join(DIRECTIONS)}")
    if direction == "rising":
        rows = [r for r in rows if (r.get("delta") or 0) > 0]
    elif direction == "falling":
        rows = [r for r in rows if (r.get("delta") or 0) < 0]
    key = "pct" if order == "pct" else "delta"
    return sorted(rows,
                  key=lambda r: (r.get(key) is None, -abs(r.get(key) or 0.0),
                                 r.get("place") or ""))


def present(payload: dict, *, order: str = "abs", direction: str = "both",
            top=None) -> dict:
    """Order, filter, truncate — and record all three counts in one place.

    `joined` → `matched_direction` → `shown` is what lets a consumer see
    truncation without trusting a flag it did not set. `top=None` is the only
    correct default for `--json` and `--csv`: a default `--top 20` on a machine
    format would make a consumer record "396 cities compared, 20 moved".
    """
    ordered = order_movers(payload.get("movers") or [], order, direction)
    shown = ordered if top is None else ordered[:max(0, int(top))]
    return dict(payload, movers=shown, order=order, direction=direction,
                matched_direction=len(ordered), shown=len(shown),
                truncated=len(shown) < len(ordered))


# ------------------------------------------------------------------ verdict --

def verdict(payload: dict) -> dict:
    """`{"exit", "reasons", "warnings"}` — the refusals, as data.

    Here rather than in `cmd_movers` because every one is a correctness
    predicate over the payload, and a predicate reachable only through argparse
    is one no test exercises directly; cli.py owns the wording and returns
    `verdict(payload)["exit"]`, the same split `trend` uses. `warnings` are
    separate because a panel-wide rebase happens to healthy data, and a warning
    that fires on healthy input is muted within a week.
    """
    reasons = []
    drift = payload.get("drift")
    if drift == "same_published_table":
        # A usage error — two ids naming one published table. Different cause,
        # different fix, so a different code from the drift alarm below, which
        # means "the command ran and refuses to present its answer".
        reasons.append(("same_published_table", 2))
    elif drift == "snapshot_parameter_ignored":
        reasons.append(("snapshot_parameter_ignored", 1))
    elif drift == "identical_tables_cause_unknown":
        # Identical tables with no third published id to probe. Exit 1, the
        # "ran and refuses to present its answer" code, rather than the usage
        # code: 2 would assert the ids are at fault, and that is the half of
        # the question this run could not answer.
        reasons.append(("identical_tables_cause_unknown", 1))
    if (payload.get("join") or {}).get("below_floor"):
        reasons.append(("join_below_floor", 1))
    if (payload.get("stats") or {}).get("with_metric_in_both") == 0:
        # "Nothing was compared" and "nothing moved" render identically — an
        # empty table — and they have opposite meanings.
        reasons.append(("nothing_was_compared", 1))
    for flag in payload.get("partial_drift") or []:
        reasons.append((flag, 1))

    warnings = []
    if payload.get("rebase_suspected"):
        warnings.append("rebase_suspected")
    return {"exit": max([code for _, code in reasons], default=0),
            "reasons": [name for name, _ in reasons], "warnings": warnings}


# ------------------------------------------------------------- orchestrator --

def _reads_since(before: dict) -> dict:
    """What this call actually spent, as a DIFFERENCE against `net.reads()`.

    `net`'s counters are process-global and every other command adds to them,
    so an absolute reading here would report the session rather than the run.
    Live and cached stay separate for the reason `net` separates them: "three
    reads" is the same number whether the address was reachable or the answer
    came off disk, and only one of those two runs tested anything.
    """
    now = net.reads()
    spent = {kind: now[kind] - before.get(kind, 0) for kind in now}
    return dict(spent, total=sum(spent.values()))


def compare_snapshots(vertical: str = "cost-of-living", *, frm,
                      to: str = "current", column=None, view: str = "city",
                      region=None, max_age: int = DEFAULT_MAX_AGE) -> dict:
    """Fetch both tables and diff them. THREE reads, or four.

        1  the snapshot list (cached at 86400; usually zero)
        2  the --from table
        3  the --to table
        4  the oldest published id that is NEITHER side — ONLY when the first
           two came back identical, i.e. on the already-degenerate path

    It must never iterate the snapshot list: "diff every consecutive pair"
    would be thirty requests against a source that bans this address for seven
    days on volume, for a question two reads answer. The only function here
    that spends one, which is why `same_panel` stays pure.

    The `reads` figure in the payload is the difference `net.reads()` measured
    across this call, never the list above restated as arithmetic: the list is
    what this function INTENDS, and the intention is identical on a run that
    went to the network and a run answered entirely from disk — which is the
    one distinction anybody reads that number for."""
    reads_before = net.reads()
    res = resolve_snapshots(vertical, frm, to, view=view, region=region,
                            max_age=max_age)
    published = res["published"]
    # No per-side TTL is computed here. `rankings.fetch` derives pin
    # eligibility from the id via `rankings.is_archival` and exempts a pinned
    # entry from the age clock entirely, so a second rule in this module would
    # be a second ANSWER — and the two disagreed: "not the newest published id"
    # called 2026 an archive while "its year has ended plus a publication
    # cycle" does not.
    ma_from = ma_to = max_age

    t_from = rankings.fetch(vertical, view=view, snapshot=res["from"],
                            region=region, max_age=ma_from)
    t_to = rankings.fetch(vertical, view=view, snapshot=res["to"],
                          region=region, max_age=ma_to)

    identical = same_panel(t_from, t_to)
    drift, probe = None, None
    if identical:
        # ONE confirmation probe, only here. `--from 2026-mid --to current` may
        # legitimately name one published table under two names, and an alarm
        # firing on that healthy input gets muted — the same reason
        # `_is_one_table_repeated` needed `of`. The OLDEST id is the target
        # because `?title=` can be honoured for recent ids and ignored for old
        # ones, and only the oldest distinguishes that.
        #
        # The oldest id that is NEITHER SIDE, though. Probing `--from` or
        # `--to` re-fetches a table already known to equal the other, so
        # `same_panel(t_old, t_to)` is True by construction and the verdict is
        # `snapshot_parameter_ignored` however the source behaved. Measured
        # with 2014 and 2019 serving one published table: `--from 2026-mid --to
        # 2019` said `same_published_table` and `--from 2014 --to 2019` said
        # the opposite about that same fact. A read-back that cannot
        # distinguish the two outcomes is not a measurement.
        candidates = [s for s in published
                      if s not in (res["from"], res["to"])]
        if candidates:
            target = min(candidates, key=_snapshot_sort_key)
            t_old = rankings.fetch(
                vertical, view=view, snapshot=target, region=region,
                max_age=max_age)
            drift = ("snapshot_parameter_ignored" if same_panel(t_old, t_to)
                     else "same_published_table")
            probe = {"available": True, "snapshot": target,
                     "rows": len(t_old.get("rows") or []),
                     "url": t_old.get("url"),
                     "identical_to_to": drift == "snapshot_parameter_ignored"}
        else:
            # Every published id is one of the two sides, so nothing here can
            # tell the two causes apart. The tables ARE identical and every
            # delta below is 0, so the run still refuses — but the two causes
            # have opposite fixes (pick different ids / this client is broken),
            # and naming one unobserved sends the reader to the wrong
            # subsystem. Same key set as the probed branch: a `probe` whose
            # shape depends on the answer is a second trap for the reader
            # inspecting a degenerate run.
            drift = "identical_tables_cause_unknown"
            probe = {"available": False, "snapshot": None, "rows": None,
                     "url": None, "identical_to_to": None}

    resolved_column = resolve_column(t_from, t_to, column)
    payload = diff(t_from, t_to, resolved_column)

    def side(table, snapshot, url, used):
        return {"snapshot": snapshot, "url": table.get("url") or url,
                "age_s": table.get("_age_s"), "archival": table.get("_archival"),
                "rows": len(table.get("rows") or []), "max_age_used": used}

    return dict(
        payload, vertical=vertical, view=view, region=region,
        # Both ids echoed with their ages and row counts. Transposing --from
        # and --to inverts every sign and leaves the table otherwise perfect,
        # so the only defence is that the reader is always told which is which.
        **{"from": side(t_from, res["from"], res["url_from"], ma_from),
           "to": side(t_to, res["to"], res["url_to"], ma_to)},
        identical_tables=identical, drift=drift, probe=probe,
        published=published, reads=_reads_since(reads_before))
