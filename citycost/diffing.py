"""`diffing` — the panel-wide subtraction, and every reason to doubt it.

`comparable` has already agreed that these are two tables and not one, that the
requested column means the same quantity in both headers, and which row pairs
with which. What is left is this file: for each joined city, what changed — and
then, over the whole panel, whether that change is history or an artefact of
how the two tables were fetched.

Those two halves are one question, not two, and that is why they share a file:
every observer below reads the deltas the arithmetic above it produced. A
column whose every delta is exactly 0.0 while a neighbouring column moved, and
a panel whose 396 deltas take three distinct values, are both invisible until
the subtraction has happened. The one guard that needs no delta — the
whole-table fingerprint — is in `comparable` with the rest of the refusals that
fire before a number exists.

A reader looking for "why is this delta `None`", "why did `partial_drift`
fire", "what exactly is in a row", or "what does `stats` mean" opens this file.
A reader looking for what a run fetched, what it cost, or what its exit code
claims opens `movers`.

Nothing here does I/O. Pure, so that all six per-city statuses are reachable
from a fixture and the text, `--md`, `--csv` and `--json` paths cannot drift
into four answers.
"""

from __future__ import annotations

import math
from statistics import median

from .comparable import EXCLUDED_STATUSES, index_by_key

#: The join must cover at least this share of the SMALLER table or the answer
#: is refused. Measured 2026-08-25, cost-of-living 2019 vs current: 433 rows
#: against 558 with 396 joining — 91%. That is what healthy looks like, so the
#: next person to tune this floor can see the headroom they are spending.
MIN_OVERLAP = 0.5

#: Below this many DISTINCT delta values, over a panel big enough to have
#: opinions, the column is not history: real history over 396 cities produces
#: hundreds of distinct deltas, one table fetched twice produces one. Counted
#: over cities COMPARABLE in both, not merely joined — 396 joined of which three
#: carry the column is sparse, and firing there is a warning on healthy input.
DISTINCT_DELTA_FLOOR = 3
DISTINCT_DELTA_MIN_SAMPLE = 50

#: The frozen-column flag needs a floor for the same reason its neighbour does,
#: and shipped without one. Measured: two cities, one of them comparable and
#: genuinely unchanged while another column moved, fired the alarm and exited
#: 1 on data with nothing wrong with it. Ten rather than
#: `DISTINCT_DELTA_MIN_SAMPLE`, because "every one of N is EXACTLY 0.0 while a
#: neighbouring column moved" becomes improbable far faster than "N deltas take
#: three distinct values" — the same size `REBASE_MIN_SAMPLE` picked for the
#: same judgement about when agreement stops being ordinary.
FROZEN_COLUMN_MIN_SAMPLE = 10

#: A whole panel moving one way is the signature of a rebase or a redefined
#: same-named column, not of four hundred cities agreeing. The minimum sample
#: exists because four cities moving together is ordinary, and a warning that
#: fires on ordinary input is muted within a week.
REBASE_DIRECTION_SHARE = 0.90
REBASE_MIN_SAMPLE = 10

#: Carried in every output shape, because a caveat that lives only in prose is
#: a caveat a machine consumer never receives.
BASIS = ("Numbeo index columns are rebased per snapshot against that "
         "snapshot's own reference city. A mover moved relative to that "
         "reference and to the rest of the panel — not necessarily in "
         "absolute price.")

#: Everything `movers.present()` fills in, seeded null by `diff()` from this
#: same tuple so the key set is identical on every path. It lives here rather
#: than beside `present()` because `diff()` is the only code that READS it —
#: `present()` writes those keys by name. `matched_direction` and
#: `shown` were seeded and `truncated` was not, which made
#: `'truncated' in compare_snapshots(...)` False — on a documented public entry
#: point, and precisely on the runs that refuse before presenting, which are
#: the runs a consumer inspects. One owner, so a fourth key added to `present`
#: is either seeded here or visibly absent from the tuple.
PRESENTATION_KEYS = ("order", "direction", "matched_direction", "shown",
                     "truncated")

#: The exact key set of a row, asserted in tests, because the field that must
#: NOT be here is the interesting one: a `rank_delta` across panels of 433 and
#: 558 rows mixes the city's movement with the panel's 29% growth in one
#: number, in an unknown ratio — and needs no interpretation, so it is trusted.
ROW_KEYS = frozenset({
    "place", "city", "country", "key", "status", "reason",
    "value_from", "value_to", "delta", "pct",
    "rank_from", "of_from", "rank_to", "of_to",
})

#: Every reason `abs_change` returns INSTEAD of a delta, named once and
#: referenced by the arithmetic itself. `_join_rows` promotes each one straight
#: to a per-city status and counts it against a pre-seeded dict, so a reason
#: the arithmetic can produce and this tuple does not list arrives as
#: `KeyError: 'not_finite'` — and a KeyError is not a `CitycostError`, so
#: `main()` does not catch it and the user gets a traceback where a row
#: belonged. Two tuples kept in step by hand agree on the day they are written;
#: `STATUSES` is built from this one so they cannot drift apart.
METRIC_ABSENT_IN_FROM = "metric_absent_in_from"
METRIC_ABSENT_IN_TO = "metric_absent_in_to"
METRIC_ABSENT_IN_BOTH = "metric_absent_in_both"
NOT_FINITE = "not_finite"
DELTA_REASONS = (METRIC_ABSENT_IN_FROM, METRIC_ABSENT_IN_TO,
                 METRIC_ABSENT_IN_BOTH, NOT_FINITE)

#: Statuses that describe a comparison, not a fault in this client. "Not listed
#: in 2019", "listed but the cell was `?`", "listed today only", "the
#: subtraction overflowed" and "actually comparable" have five meanings and no
#: shared sentence.
STATUSES = ("moved",) + DELTA_REASONS + ("new", "delisted")


# ------------------------------------------------------------- arithmetic --

def abs_change(a, b) -> tuple[float | None, str | None]:
    """`(points, reason)`. Never 0 as a stand-in for an unknown.

    `_to_number` already yields `None` for a `?` cell, and this is where that
    discipline is kept or lost: a city with `?` in 2019 and 40.0 today, read as
    0 → 40.0, tops the movers list on the strength of having had no data.
    """
    if a is None and b is None:
        return None, METRIC_ABSENT_IN_BOTH
    if a is None:
        return None, METRIC_ABSENT_IN_FROM
    if b is None:
        return None, METRIC_ABSENT_IN_TO
    return _finite(float(b) - float(a))


def _finite(value) -> tuple[float | None, str | None]:
    """Unreachable from a Numbeo index (0 to ~250) and kept anyway, because
    `json.dump` writes a non-finite float as `Infinity`, which is not JSON, and
    the one run worth catching must not be the run that crashes a parser."""
    return (value, None) if math.isfinite(value) else (None, NOT_FINITE)


def pct_change(a, b) -> tuple[float | None, str | None]:
    """`(percent, reason)`. Never 0, never inf, never NaN.

    A non-positive base is refused rather than divided by: at `a == 0` the
    answer is unbounded and at `a < 0` the sign silently inverts, so the city
    would be reported as having moved the other way.
    """
    if a is None and b is None:
        return None, METRIC_ABSENT_IN_BOTH
    if a is None:
        return None, METRIC_ABSENT_IN_FROM
    if b is None:
        return None, METRIC_ABSENT_IN_TO
    if float(a) <= 0:
        return None, "base_not_positive"
    return _finite((float(b) - float(a)) / float(a) * 100.0)


# -------------------------------------------------------------------- diff --

def _mover_row(row, *, key, status, of_from, of_to, reason=None,
               value_from=None, value_to=None, delta=None, pct=None,
               rank_from=None, rank_to=None) -> dict:
    """Every row carries every field, including the ones it has no value for: a
    key that vanishes when data is missing lets `new` and `moved` be told apart
    by shape rather than by the status that says so."""
    return {"place": row.get("place"), "city": row.get("city"),
            "country": row.get("country"), "key": key, "status": status,
            "reason": reason, "value_from": value_from, "value_to": value_to,
            "delta": delta, "pct": pct,
            "rank_from": rank_from, "of_from": of_from,
            "rank_to": rank_to, "of_to": of_to}


def _share(hits: int, total: int):
    """A share over an empty denominator is unknown, not 0.0 — and 0.0 here
    would read as "nothing was zero", which is the opposite claim."""
    return None if not total else hits / total


def _join_rows(from_index, to_index, excluded, column, of_from, of_to):
    """`(rows, joined_keys, counts, duplicate_keys)` over the UNION, because the
    one-sided cities are the only observer for a broken key: 37 delisted and
    162 new is healthy, and a symmetric explosion in those two counts is a
    collapsed join seen from outside."""
    frame = {"of_from": of_from, "of_to": of_to}
    rows, joined = [], []
    counts = {s: 0 for s in STATUSES + EXCLUDED_STATUSES}

    for key, r_to in to_index.items():
        r_from = from_index.get(key)
        if r_from is None:
            counts["new"] += 1
            rows.append(_mover_row(
                r_to, key=key, status="new", rank_to=r_to.get("rank"),
                value_to=(r_to.get("metrics") or {}).get(column), **frame))
            continue
        joined.append(key)
        a = (r_from.get("metrics") or {}).get(column)
        b = (r_to.get("metrics") or {}).get(column)
        delta, delta_reason = abs_change(a, b)
        pct, pct_reason = pct_change(a, b)
        status = "moved" if delta_reason is None else delta_reason
        counts[status] += 1
        rows.append(_mover_row(
            r_to, key=key, status=status,
            # Only ever the reason a *present* pair still has no percentage.
            # Repeating the status as a reason would be one absence wearing two
            # sentences.
            reason=(pct_reason if status == "moved" and pct is None else None),
            value_from=a, value_to=b, delta=delta, pct=pct,
            rank_from=r_from.get("rank"), rank_to=r_to.get("rank"), **frame))

    for key, r_from in from_index.items():
        if key in to_index:
            continue
        counts["delisted"] += 1
        rows.append(_mover_row(
            r_from, key=key, status="delisted", rank_from=r_from.get("rank"),
            value_from=(r_from.get("metrics") or {}).get(column), **frame))

    duplicate_keys = []
    for side, items in excluded:
        for item in items:
            counts[item["reason"]] += len(item["rows"])
            duplicate_keys.append({"side": side, "key": item["key"],
                                   "reason": item["reason"],
                                   "labels": item["labels"]})
            rows += [_mover_row(r, key=item["key"], status=item["reason"],
                                reason=f"excluded_from_{side}_snapshot",
                                **frame) for r in item["rows"]]
    return rows, joined, counts, duplicate_keys


def _statistics(movers_rows: list) -> dict:
    """The free observers, always computed. `median_delta` and
    `same_direction_share` exist because a redefined same-named column presents
    exactly as a panel-wide common-mode shift, and nothing else reveals it."""
    deltas = [r["delta"] for r in movers_rows]
    pcts = [r["pct"] for r in movers_rows if r["pct"] is not None]
    zeros = sum(1 for d in deltas if d == 0.0)
    up = sum(1 for d in deltas if d > 0)
    down = sum(1 for d in deltas if d < 0)
    return {
        "with_metric_in_both": len(deltas),
        # Rounded only for counting: index cells carry one decimal, so 1e-6 is
        # far below the source's own resolution and far above float noise.
        "distinct_delta_values": len({round(d, 6) for d in deltas}),
        "zero_delta_share": _share(zeros, len(deltas)),
        "same_direction_share": _share(max(up, down), up + down),
        "median_delta": median(deltas) if deltas else None,
        "median_pct": median(pcts) if pcts else None,
        "rising": up, "falling": down, "unchanged": zeros,
    }


def diff(from_table: dict, to_table: dict, column: str) -> dict:
    """Pure. Two parsed tables in, one structure out, no I/O and no rendering.

    Pure so that all six per-city statuses are reachable from a fixture, and so
    the text, `--md`, `--csv` and `--json` paths cannot drift into four
    answers. `moved` means *comparable*, not *nonzero*: a city whose delta is
    exactly 0.0 stays in the table, because filtering zero deltas out as "did
    not move" would delete precisely the evidence the drift guards read.
    """
    from_index, from_excluded = index_by_key(from_table)
    to_index, to_excluded = index_by_key(to_table)
    of_from = len((from_table or {}).get("rows") or [])
    of_to = len((to_table or {}).get("rows") or [])
    from_cols = list((from_table or {}).get("columns") or [])
    to_cols = list((to_table or {}).get("columns") or [])
    shared_columns = [c for c in to_cols if c in from_cols]

    rows, joined_keys, counts, duplicate_keys = _join_rows(
        from_index, to_index,
        (("from", from_excluded), ("to", to_excluded)),
        column, of_from, of_to)
    movers = [r for r in rows if r["status"] == "moved"]
    stats = _statistics(movers)
    joined = len(joined_keys)

    smaller = min(of_from, of_to)
    join = {
        "from_rows": of_from, "to_rows": of_to,
        "overlap_pct_of_smaller": (100.0 * joined / smaller) if smaller else None,
        "floor_pct": 100.0 * MIN_OVERLAP,
        "below_floor": bool(smaller) and joined < MIN_OVERLAP * smaller,
    }

    return {
        "column": column,
        "shared_columns": shared_columns,
        "columns": {"from": from_cols, "to": to_cols, "shared": shared_columns},
        "basis": BASIS,
        "joined": joined,
        # Filled in by `present()`. Null from the start so the key set is
        # identical on every path: a key that appears only sometimes is its own
        # trap for a consumer. Seeded from `PRESENTATION_KEYS` rather than
        # listed again here — the key this file forgot to list is the key that
        # was missing.
        **{key: None for key in PRESENTATION_KEYS},
        "movers": movers,
        "rows": rows,
        "only_in_to": [r["place"] for r in rows if r["status"] == "new"],
        "only_in_from": [r["place"] for r in rows if r["status"] == "delisted"],
        "duplicate_keys": duplicate_keys,
        "counts": counts,
        "join": join,
        "stats": stats,
        "partial_drift": _partial_drift(stats, from_index, to_index,
                                        joined_keys, shared_columns, column),
        "rebase_suspected": bool(
            stats["same_direction_share"] is not None
            and stats["rising"] + stats["falling"] >= REBASE_MIN_SAMPLE
            and stats["same_direction_share"] >= REBASE_DIRECTION_SHARE),
    }


def _control_moves(from_index, to_index, joined_keys, shared_columns, column):
    """Did ANY shared column other than the diffed one move? THREE-VALUED.

    `True` — at least one control cell moved. `False` — every comparable
    control cell was read and none of them moved. `None` — nothing comparable
    to read, which is no evidence at all and must never be read as "it did not
    move": that is a different claim, about a different subsystem.

    EVERY other shared column, not the first one the `--to` header lists. A
    single-column control is a control chosen by header order: measured on a
    panel where the first non-diffed shared column was `?` in every row while a
    third column moved in every row, the answer was `None` and flag (a) stayed
    silent on precisely the input it exists for. A column that cannot be read
    is not evidence that nothing moved, and with one control the two are the
    same answer.
    """
    comparable = False
    for control in (c for c in shared_columns if c != column):
        for key in joined_keys:
            delta, _ = abs_change(
                (from_index[key].get("metrics") or {}).get(control),
                (to_index[key].get("metrics") or {}).get(control))
            if delta is None:
                continue
            comparable = True
            if delta != 0.0:
                # One moving cell settles it; the rest of the scan cannot
                # change the answer and this runs over ~400 cities × n columns.
                return True
    # `False` only once something was actually read. Collapsing this into a
    # bare `False` would answer "no control moved" for a panel where no control
    # could be read, which is the one confusion this function exists to avoid.
    return False if comparable else None


def _partial_drift(stats, from_index, to_index, joined_keys,
                   shared_columns, column) -> list:
    """The two drift signatures a whole-table fingerprint cannot see.

    (a) `?title=` honoured for the rest of the page while the *diffed* column
        falls back to current values: the fingerprints differ, `same_panel`
        passes, and every delta the user asked for is exactly zero. The control
        is every other shared column — if any of them moves while this one is
        100% zeros over a panel big enough to have an opinion, one column is
        not history.
    (b) A refreshed rather than archived table, where deltas are near-zero but
        not exactly zero, so no equality test catches it. Real history over 396
        cities produces hundreds of distinct deltas; a repeat produces one.

    The control is three-valued and only `True` fires (a). `False` — nothing in
    the control moved either — is the whole-panel signature `same_panel` owns.
    `None` — no comparable values there — is no evidence at all and must not be
    read as "it did not move". At `DISTINCT_DELTA_MIN_SAMPLE` comparable cities
    or more, (b) fires regardless of the control, closing the small-sample gap.

    Both flags carry a minimum sample. Two cities frozen while a neighbouring
    column moved is ordinary, and an alarm that fires on ordinary input is
    muted within a week — taking the degenerate case it was written for with
    it.
    """
    flags = []
    total = stats["with_metric_in_both"]
    if not total:
        return flags

    control_moves = _control_moves(from_index, to_index, joined_keys,
                                   shared_columns, column)

    if (total >= FROZEN_COLUMN_MIN_SAMPLE
            and stats["zero_delta_share"] == 1.0 and control_moves is True):
        flags.append("diffed_column_did_not_move_while_another_did")
    if (total >= DISTINCT_DELTA_MIN_SAMPLE
            and stats["distinct_delta_values"] <= DISTINCT_DELTA_FLOOR):
        flags.append("too_few_distinct_deltas")
    return flags
