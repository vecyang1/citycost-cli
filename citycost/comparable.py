"""`comparable` — may these two tables be compared at all, and between what?

The question here is AGREEMENT, and every refusal in this file fires before a
delta exists: two snapshot ids that must name two different published tables,
one column label that must mean the same quantity in both headers, one row key
that must identify one city in each table. Nothing here subtracts anything, and
nothing here has an opinion about what a movement means.

A reader looking for "why was my `--from`, my `--column`, or my city refused"
opens this file. A reader looking for "why is this delta `None`" or "why did
`partial_drift` fire" opens `diffing`, which owns the arithmetic and the
observers that read it. `movers` owns the command that calls both.

`same_panel` is here rather than beside those observers on purpose. It asks the
question `resolve_snapshots` already asked of the two URLs — are these two ids
one table? — one layer later, after the fetch, and it answers without choosing
a column and without subtracting anything. The observers that DO need the
deltas (a column frozen while its neighbours moved, a panel-wide common-mode
shift) cannot be computed until the subtraction has happened, so they live with
it.

The only thing this file fetches is the published snapshot list, and only to
check an id against it — which is the whole point: a typo costs one cached read
here instead of two live requests and a wrong diagnosis downstream.
"""

from __future__ import annotations

from . import rankings
from .errors import LayoutChanged, SourceUnavailable

#: The statuses for rows the join had to refuse — produced HERE, by
#: `index_by_key`, which is why they live beside it rather than beside
#: `diffing.STATUSES`. Counted alongside those statuses and deliberately not
#: numbered in prose: that tuple grew by one the day the arithmetic's overflow
#: reason was counted, and a comment that says "a seventh" is a fact about a
#: length nobody updates.
#:
#: Not `new` and not `delisted` —
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

    # Ordered by the period each id NAMES, never by the order the `<select>`
    # happened to list them in — that is what `_snapshot_sort_key` is for. A
    # remedy built from `published[-1]` offers the reader a base, and on an
    # oldest-first page the id it hands them is the newest one there is.
    newest = max(published, key=_snapshot_sort_key)
    oldest = min(published, key=_snapshot_sort_key)

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
            f"name two different ids, e.g. "
            f"`citycost movers --from {oldest} --to current "
            f"--index {vertical}`; the published ids: "
            f"`citycost snapshots --index {vertical}`")

    return {"vertical": vertical, "view": view, "region": region,
            "from": frm, "to": to, "url_from": url_from, "url_to": url_to,
            "published": published, "newest": newest, "oldest": oldest}


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
