"""`movers` — a panel-wide diff of ONE index column between TWO snapshots.

`rank` reads one table, `trend` reads one city across many; this reads two and
asks which of the ~400 cities in both moved. Two bulk reads answer it — "diff
every consecutive pair" would be thirty requests against a source that bans
this address for seven days at a time.

Everything here exists because this command's characteristic failure is a
*complete, well-formed, catastrophically wrong* table rather than an error —
`?title=` silently ignored so every delta is 0.0, a collapsed join answering
from a 3% sample, a column that means two different things in the two
snapshots. Each has an observer below, and each observer says which wrong
answer it is there to prevent.

No verdict is rendered here and none should be rendered downstream. The sign's
desirability depends on the column AND the reader — a rent index rising is bad
for a renter and good for a landlord, a salary index falling is bad, `Gross
Rental Yield` has no single answer — and this repository has already shipped one
"lower is better" rule that marked the worst-paying city as best.
"""

from __future__ import annotations

import math
from statistics import median

from . import rankings
from .errors import LayoutChanged, SourceUnavailable
from .net import DEFAULT_MAX_AGE

#: The join must cover at least this share of the SMALLER table or the answer
#: is refused. Measured 2026-08-25, cost-of-living 2019 vs current: 433 rows
#: against 558 with 396 joining — 91%. That is what healthy looks like, so the
#: next person to tune this floor can see the headroom they are spending.
MIN_OVERLAP = 0.5

#: Example names beside a count on stderr. One owner, so the number the note
#: promises and the number it prints cannot disagree.
SAMPLE_N = 5

#: Text and markdown truncate by default; machine formats never do. A default
#: that silently truncated `--json` would make a consumer record "396 cities
#: compared, 20 moved" — the 1.2.0 `--full`-shaping-`raw` defect, reworn.
DEFAULT_TOP_TEXT = 20

#: A published snapshot that is not the newest cannot change, so its table may
#: be reused far longer than `current`'s. Capped in practice by
#: `net.CACHE_RETENTION` (7 days) for ids too recent for `rankings.is_archival`
#: to pin. Subordinate, always, to an explicit `--max-age 0`.

#: Below this many DISTINCT delta values, over a panel big enough to have
#: opinions, the column is not history: real history over 396 cities produces
#: hundreds of distinct deltas, one table fetched twice produces one. Counted
#: over cities COMPARABLE in both, not merely joined — 396 joined of which three
#: carry the column is sparse, and firing there is a warning on healthy input.
DISTINCT_DELTA_FLOOR = 3
DISTINCT_DELTA_MIN_SAMPLE = 50

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

ORDERS = ("abs", "pct")
DIRECTIONS = ("both", "rising", "falling")

#: The exact key set of a row, asserted in tests, because the field that must
#: NOT be here is the interesting one: a `rank_delta` across panels of 433 and
#: 558 rows mixes the city's movement with the panel's 29% growth in one
#: number, in an unknown ratio — and needs no interpretation, so it is trusted.
ROW_KEYS = frozenset({
    "place", "city", "country", "key", "status", "reason",
    "value_from", "value_to", "delta", "pct",
    "rank_from", "of_from", "rank_to", "of_to",
})

#: Declared explicitly so a column survives in the header even when every value
#: is empty, and so `status` can never be dropped: without it an empty delta
#: cell means three different things (new, delisted, or `?` in one snapshot) —
#: the absent-is-not-zero rule re-losing at the CSV boundary.
CSV_COLUMNS = ["place", "city", "country", "status", "reason",
               "value_from", "value_to", "delta", "pct",
               "rank_from", "of_from", "rank_to", "of_to"]

#: Statuses that describe a city, not a fault in this client. Six, because "not
#: listed in 2019", "listed but the cell was `?`", "listed today only" and
#: "actually comparable" have four meanings and no shared sentence.
STATUSES = ("moved", "metric_absent_in_from", "metric_absent_in_to",
            "metric_absent_in_both", "new", "delisted")

#: A seventh, for rows the join had to refuse. Not `new` and not `delisted` —
#: they were listed, twice, or under a label that normalises to nothing — and
#: calling them either would be a wrong message rather than a missing one.
EXCLUDED_STATUSES = ("duplicate_key", "unkeyable_label")


# ------------------------------------------------------------ snapshot ids --

def _snapshot_sort_key(sid) -> tuple[int, int]:
    """Order snapshot ids by the period they name, not by page order: "newest"
    and "oldest" are load-bearing — the newest decides which side may be cached
    for a month, the oldest is the confirmation probe's target — and
    `snapshots()` returns whatever order the `<select>` happens to be in."""
    text = str(sid or "")
    if not rankings.SNAPSHOT_ID.fullmatch(text):
        return (-1, 0)
    return (int(text[:4]), 1 if text.endswith("-mid") else 0)


def resolve_snapshots(vertical: str, frm, to="current", *, view: str = "city",
                      region=None, max_age: int = 86400) -> dict:
    """Validate both ids and both URLs BEFORE either table is fetched.

    Numbeo does not refuse an unrecognised `?title=`; it serves the current
    table. So a typo'd `--from 2109` produces two identical tables and fires
    the upstream-drift alarm, sending the user to diagnose a source behaving
    correctly. Two causes of one symptom must not print one sentence, and the
    cheap one is the keystroke. Costs one request, usually zero.
    """
    frm = "current" if frm is None else str(frm).strip()
    to = "current" if to is None else str(to).strip()
    if not frm:
        # Deliberately not defaulted to "five snapshots back" or anything else:
        # a base the reader did not choose is a base the reader will not check,
        # and every sign in the table depends on it.
        raise SourceUnavailable(
            f"no --from snapshot given for '{vertical}'",
            "name the base explicitly, e.g. --from 2019 --to current; the "
            f"published ids: `citycost snapshots --index {vertical}`")

    published = list(rankings.snapshots(vertical, max_age=max_age) or [])
    if not published:
        raise SourceUnavailable(
            f"no snapshot list found for '{vertical}'",
            "this vertical may be current-only, and a diff needs two "
            "published tables; try `citycost rank` instead")

    known = set(published) | {"current"}
    for side, value in (("--from", frm), ("--to", to)):
        if value not in known:
            raise SourceUnavailable(
                f"{side} '{value}' is not a published snapshot of '{vertical}'",
                f"'current' or one of: {', '.join(published)} (re-read them "
                f"with `citycost snapshots --index {vertical}`)")

    url_from = rankings._url(vertical, view, frm, region)
    url_to = rankings._url(vertical, view, to, region)
    if url_from == url_to:
        # A refusal that costs no request is strictly better than an alarm that
        # costs two, and this would otherwise arrive as the drift alarm — the
        # same sentence for a source that is behaving perfectly.
        raise SourceUnavailable(
            f"--from '{frm}' and --to '{to}' resolve to the same URL "
            f"({url_from}); every delta would be 0 because it is one question "
            f"asked twice",
            f"name two different ids, e.g. --from {published[-1]} --to "
            f"current; the published ids: "
            f"`citycost snapshots --index {vertical}`")

    return {"vertical": vertical, "view": view, "region": region,
            "from": frm, "to": to, "url_from": url_from, "url_to": url_to,
            "published": published,
            "newest": max(published, key=_snapshot_sort_key),
            "oldest": min(published, key=_snapshot_sort_key)}


# ----------------------------------------------------------------- columns --

def _flat(s) -> str:
    return " ".join(str(s or "").split()).lower()


def _match_column(headers, requested) -> list[str]:
    """Every header matching `requested`. Exact label first, substring after.

    The two tiers are `find_place`'s, for `find_place`'s reason: a user who
    typed the complete label has already disambiguated, and refusing them
    because their exact label is also a substring of a longer one would be a
    refusal manufactured by the matcher.
    """
    want = _flat(requested)
    if not want:
        return []
    exact = [h for h in headers if _flat(h) == want]
    if exact:
        return exact
    return [h for h in headers if want in _flat(h)]


def resolve_column(from_table: dict, to_table: dict, requested=None) -> str:
    """The one column both snapshots agree on, or a refusal naming both headers.

    There is no per-vertical table of "the headline column" here and must not
    be: the seven verticals carry 4 to 11 columns and Numbeo renames them
    without notice, so a remembered map is an allowlist only ever right for the
    names somebody remembered — the shape of the currency allowlist that
    reported Fukuoka rent as $11,095 against a true $470.

    `--column` is a substring as `rank --sort` is, and stricter on purpose:
    `rank` sorts ONE table, where a wrong-but-single column is visibly labelled
    in the output, while `movers` joins two, where taking the first hit in each
    header independently subtracts `Rent Index` from `Cost of Living Plus Rent
    Index` whenever the header order differs. Same syntax, different rigour.
    """
    from_cols = list((from_table or {}).get("columns") or [])
    to_cols = list((to_table or {}).get("columns") or [])
    shared = [c for c in to_cols if c in from_cols]
    detail = (f"--from header: {from_cols} · --to header: {to_cols} · "
              f"present in both: {shared}")

    if requested is None or not str(requested).strip():
        if not shared:
            # Not a caller error: the two published headers have no label in
            # common, which is a fact about the source and a fix in this repo.
            raise LayoutChanged(
                f"the two snapshots share no column label, so there is "
                f"nothing to diff. {detail}",
                "Numbeo renamed its columns between these snapshots — this "
                "client needs updating, or pick two closer snapshots")
        # `--to` supplies the ordering because its vocabulary is the one `rank`
        # prints today, so the default column is the one the reader has seen.
        return shared[0]

    from_hits = _match_column(from_cols, requested)
    to_hits = _match_column(to_cols, requested)

    # One shape for every refusal, so a branch added later cannot forget to
    # print the two headers a reader needs in order to pick again.
    def refuse(cls, why, remedy):
        raise cls(f"--column '{requested}' {why}. {detail}", remedy)

    pick = f"pick a column present in both: {shared}"
    if len(from_hits) > 1 or len(to_hits) > 1:
        refuse(SourceUnavailable,
               f"matches more than one header (--from: {from_hits} · --to: "
               f"{to_hits}), and a diff of two different quantities is a "
               f"number with no meaning",
               "give the full column label as printed by `citycost rank`")
    if from_hits and not to_hits:
        refuse(LayoutChanged,
               f"resolves to '{from_hits[0]}' in the older snapshot and to "
               f"nothing in the newer one",
               "Numbeo dropped or renamed that column between these snapshots "
               "— this client needs updating, or diff a shared label")
    if to_hits and not from_hits:
        refuse(SourceUnavailable,
               f"resolves to '{to_hits[0]}' in the newer snapshot and to "
               f"nothing in the older one, so every city would read N/A — "
               f"which reads as 'Numbeo has no data'", pick)
    if not from_hits:
        refuse(SourceUnavailable, "matches no header in either snapshot", pick)
    if from_hits[0] != to_hits[0]:
        refuse(SourceUnavailable,
               f"resolves to '{from_hits[0]}' in the older snapshot and "
               f"'{to_hits[0]}' in the newer one, and subtracting those is "
               f"arithmetic on two unrelated series", pick)
    return from_hits[0]


# -------------------------------------------------------------------- join --

def join_key(row) -> str:
    """`rankings._norm` of the FULL place label — nothing else, ever.

    The full comma-joined label including subdivision and country, because
    every shorter key over-matches: `_norm("city")` collapses `Vancouver, WA,
    United States` into `Vancouver, Canada`, `(city, country)` still collapses
    `Portland, OR` with `Portland, ME`, and `find_place`'s substring tier pairs
    `Nang` with `Penang`. Here that produces a *delta* rather than a visible
    ambiguity — a fabricated number instead of a refusal. `_norm` is reused
    rather than reimplemented so there is one owner.

    STRICT BUT NOT STABLE, deliberately. It does not survive an upstream
    rename: `Kiev`→`Kyiv`, `Turkey`→`Türkiye`, a subdivision appearing
    (`Vancouver, Canada`→`Vancouver, BC, Canada`), a parenthetical alternate
    changing. Every such case presents as one delisting plus one new entry and
    must NOT be repaired by fuzzy matching: a rename costs a pair of absences,
    visible and adjacent in the two lists where a human spots them, while a
    fuzzy repair risks pairing two different cities and inventing a delta.
    Given ten known homonym keys, abstaining beats guessing.
    """
    return rankings._norm((row or {}).get("place") or "")


def index_by_key(table: dict) -> tuple[dict, list]:
    """`(index, excluded)`. Two values, because one cannot express the exclusion.

    `{key: row for row in rows}` is last-wins, and page order is measured to be
    unstable across snapshots — 2026-mid lists `Vancouver, WA` before
    `Vancouver, Canada`, 2025 lists only `Vancouver, Canada` — so a colliding
    pair would contribute a delta between two different cities, and *which* two
    would change between runs. `_norm` drops punctuation and spacing, so `St.
    Petersburg` and `St Petersburg` are one key; a label normalising to nothing
    is excluded under its own name, or two such rows join on the empty key.
    """
    seen: dict[str, list] = {}
    unkeyable = []
    for row in (table or {}).get("rows") or []:
        key = join_key(row)
        if not key:
            unkeyable.append(row)
            continue
        seen.setdefault(key, []).append(row)

    index, excluded = {}, []
    for key, rows in seen.items():
        if len(rows) == 1:
            index[key] = rows[0]
        else:
            excluded.append({"key": key, "reason": "duplicate_key",
                             "labels": [r.get("place") for r in rows],
                             "rows": rows})
    for row in unkeyable:
        excluded.append({"key": "", "reason": "unkeyable_label",
                         "labels": [row.get("place")], "rows": [row]})
    return index, excluded


# ------------------------------------------------------------- arithmetic --

def abs_change(a, b) -> tuple[float | None, str | None]:
    """`(points, reason)`. Never 0 as a stand-in for an unknown.

    `_to_number` already yields `None` for a `?` cell, and this is where that
    discipline is kept or lost: a city with `?` in 2019 and 40.0 today, read as
    0 → 40.0, tops the movers list on the strength of having had no data.
    """
    if a is None and b is None:
        return None, "metric_absent_in_both"
    if a is None:
        return None, "metric_absent_in_from"
    if b is None:
        return None, "metric_absent_in_to"
    return _finite(float(b) - float(a))


def _finite(value) -> tuple[float | None, str | None]:
    """Unreachable from a Numbeo index (0 to ~250) and kept anyway, because
    `json.dump` writes a non-finite float as `Infinity`, which is not JSON, and
    the one run worth catching must not be the run that crashes a parser."""
    return (value, None) if math.isfinite(value) else (None, "not_finite")


def pct_change(a, b) -> tuple[float | None, str | None]:
    """`(percent, reason)`. Never 0, never inf, never NaN.

    A non-positive base is refused rather than divided by: at `a == 0` the
    answer is unbounded and at `a < 0` the sign silently inverts, so the city
    would be reported as having moved the other way.
    """
    if a is None and b is None:
        return None, "metric_absent_in_both"
    if a is None:
        return None, "metric_absent_in_from"
    if b is None:
        return None, "metric_absent_in_to"
    if float(a) <= 0:
        return None, "base_not_positive"
    return _finite((float(b) - float(a)) / float(a) * 100.0)


# ----------------------------------------------------- the degenerate case --

def same_panel(a: dict, b: dict) -> bool:
    """Are these two fetches the SAME table? The one guard with no error to
    fall into.

    If `?title=` stops being honoured upstream, both fetches return the current
    table, all ~550 cities join, every delta is exactly 0.0 and every
    percentage 0.0% — a complete, well-formed "nothing moved in seven years"
    with no error anywhere.

    One line, because the owner is `rankings.same_panel`: this is a fact about
    ranking tables, not about diffs, and a copy here would be the second
    implementation whose whole family lesson is that a fix goes to one owner
    while the defect stays in the sibling.

    The fingerprint is compared before any column selection, filtering or
    ordering, so no display flag can narrow the guard. No size safety valve is
    needed — the false positive `_is_one_table_repeated` was built against (a
    genuinely unchanging *city*) has no analogue, because 550 cities printing
    identical values in every column across two years is not a real panel.

    Two EMPTY tables are not this failure — naming that "the snapshot
    parameter is ignored" sends the reader to the wrong subsystem, and an empty
    panel already has its own refusal. `rankings.same_panel` owns that too.
    """
    if not ((a or {}).get("rows") and (b or {}).get("rows")):
        return False
    return rankings.same_panel(a, b)


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
        # trap for a consumer.
        "matched_direction": None,
        "shown": None,
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


def _partial_drift(stats, from_index, to_index, joined_keys,
                   shared_columns, column) -> list:
    """The two drift signatures a whole-table fingerprint cannot see.

    (a) `?title=` honoured for the rest of the page while the *diffed* column
        falls back to current values: the fingerprints differ, `same_panel`
        passes, and every delta the user asked for is exactly zero. The control
        is any other shared column — if it moves while this one is 100% zeros,
        one column is not history.
    (b) A refreshed rather than archived table, where deltas are near-zero but
        not exactly zero, so no equality test catches it. Real history over 396
        cities produces hundreds of distinct deltas; a repeat produces one.

    The control is three-valued and only `True` fires (a). `False` — nothing in
    the control moved either — is the whole-panel signature `same_panel` owns.
    `None` — no comparable values there — is no evidence at all and must not be
    read as "it did not move". At `DISTINCT_DELTA_MIN_SAMPLE` comparable cities
    or more, (b) fires regardless of the control, closing the small-sample gap.
    """
    flags = []
    total = stats["with_metric_in_both"]
    if not total:
        return flags

    control = next((c for c in shared_columns if c != column), None)
    control_moves = None
    if control is not None:
        control_deltas = []
        for key in joined_keys:
            d, _ = abs_change(
                (from_index[key].get("metrics") or {}).get(control),
                (to_index[key].get("metrics") or {}).get(control))
            if d is not None:
                control_deltas.append(d)
        if control_deltas:
            control_moves = any(d != 0.0 for d in control_deltas)

    if stats["zero_delta_share"] == 1.0 and control_moves is True:
        flags.append("diffed_column_did_not_move_while_another_did")
    if (total >= DISTINCT_DELTA_MIN_SAMPLE
            and stats["distinct_delta_values"] <= DISTINCT_DELTA_FLOOR):
        flags.append("too_few_distinct_deltas")
    return flags


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

def compare_snapshots(vertical: str = "cost-of-living", *, frm,
                      to: str = "current", column=None, view: str = "city",
                      region=None, max_age: int = DEFAULT_MAX_AGE) -> dict:
    """Fetch both tables and diff them. THREE requests, or four.

        1  the snapshot list (cached at 86400; usually zero)
        2  the --from table
        3  the --to table
        4  the oldest published table — ONLY when the first two came back
           identical, i.e. on the already-degenerate path

    It must never iterate the snapshot list: "diff every consecutive pair"
    would be thirty requests against a source that bans this address for seven
    days on volume, for a question two reads answer. The only function here
    that spends one, which is why `same_panel` stays pure."""
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
    drift, probe, probe_reads = None, None, 0
    if identical:
        # ONE confirmation probe, only here. `--from 2026-mid --to current` may
        # legitimately name one published table under two names, and an alarm
        # firing on that healthy input gets muted — the same reason
        # `_is_one_table_repeated` needed `of`. The oldest id is the target
        # because `?title=` can be honoured for recent ids and ignored for old
        # ones, and only the oldest distinguishes that.
        oldest = res["oldest"]
        t_old = rankings.fetch(
            vertical, view=view, snapshot=oldest, region=region,
            max_age=max_age)
        probe_reads = 0 if oldest in (res["from"], res["to"]) else 1
        drift = ("snapshot_parameter_ignored" if same_panel(t_old, t_to)
                 else "same_published_table")
        probe = {"snapshot": oldest, "rows": len(t_old.get("rows") or []),
                 "url": t_old.get("url"),
                 "identical_to_to": drift == "snapshot_parameter_ignored"}

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
        published=published,
        reads={"snapshot_list": 1, "tables": 2, "probe": probe_reads,
               "total": 3 + probe_reads})
