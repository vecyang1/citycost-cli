"""Numbeo's ranking pages — the bulk channel.

One request returns ~550 cities across 6 indices. Building the same answer from
per-city pages costs 550 requests, so the *shape of the question* decides the
channel: "how does X compare" is a rankings read, "what does X cost" is a city
read. Choosing wrong is not a style preference, it is two orders of magnitude.

Nothing here keys on column position. The seven verticals have between 4 and 11
columns and Numbeo renames them without notice, so every lookup goes through
the header row. A parser pinned to index 3 will one day report the pollution
index as the crime index and be believed.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json as _json
import re
import time

from .errors import LayoutChanged, SourceUnavailable
from .htmlparse import find_table, parse_tables, squash, text_of
from .net import cached_json, http_get, urlencode

BASE = "https://www.numbeo.com"
SCHEMA = "numbeo-rankings-2"

#: What a Numbeo snapshot id looks like. ONE object, used by the reader that
#: harvests the ids off the page's `<select>` and by the predicate that decides
#: whether an id may be pinned. Two literals for one grammar drift: a widened
#: pattern accepted by the reader and unknown to the predicate would change
#: behaviour with nothing going red.
SNAPSHOT_ID = re.compile(r"\d{4}(-mid)?")

#: How long after a named period ends before its table is treated as finished.
#: An ASSUMPTION about Numbeo's publication cadence, not a measurement — which
#: is why it is generous. Nothing observed distinguishes "frozen at
#: publication" from "still filling": DATA-STRATEGY records 2026-mid at 547
#: rows against `current`'s 558, and that single reading is equally consistent
#: with both stories.
PUBLICATION_CYCLE_DAYS = 180

#: Numbeo's oldest published snapshot, measured 2026-08-25 (cost-of-living and
#: property both start here). The upper bound on pinning is a date rule; this
#: is the lower one, and it exists because an id Numbeo never published is not
#: refused — an unrecognised `?title=` is answered with the CURRENT table.
#: Without this, `rank --snapshot 1999` would pin today's moving table forever
#: under an archival name.
EARLIEST_SNAPSHOT_YEAR = 2009

#: vertical -> URL path segment. The key is what a user types.
VERTICALS = {
    "cost-of-living": "cost-of-living",
    "quality-of-life": "quality-of-life",
    "crime": "crime",
    "health-care": "health-care",
    "pollution": "pollution",
    "traffic": "traffic",
    "property": "property-investment",
}

#: Region codes are UN M49. Read off Numbeo's own region links, not invented.
REGIONS = {
    "africa": "002", "america": "019", "asia": "142",
    "europe": "150", "oceania": "009",
}

_NUM = re.compile(r"^-?[\d,]+\.?\d*$")


def _to_number(text: str):
    """A ranking cell is a number or it is absent. It is never zero by default.

    Numbeo leaves a cell blank or writes '?' when it has nothing; reading that
    as 0.0 puts a city at the top of a 'cheapest' list on the strength of
    having no data at all.
    """
    t = squash(text)
    if not t or t in {"?", "-", "N/A", "n/a", "--"}:
        return None
    if not _NUM.match(t):
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def split_place(label: str) -> dict:
    """Split Numbeo's place label into city / subdivision / country.

    The country is the LAST comma-separated part, never the second. Measured
    forms this must survive, all taken from live pages:

        'Da Nang, Vietnam'                              -> Vietnam
        'Honolulu, HI, United States'                   -> United States
        'Hong Kong, Hong Kong (China)'                  -> Hong Kong (China)
        "St. John's, Newfoundland and Labrador, Canada" -> Canada
        'Pristina, Kosovo (Disputed Territory)'         -> Kosovo (Disputed Territory)
        'Rostov-on-Don (Rostov-na-donu), Russia'        -> Russia

    A `split(',')[1]` parse is correct for the first and wrong for four of the
    other five, while looking right in every test written from the first.
    """
    parts = [p.strip() for p in squash(label).split(",") if p.strip()]
    if not parts:
        return {"place": squash(label), "city": "", "subdivision": "", "country": ""}
    if len(parts) == 1:
        return {"place": parts[0], "city": parts[0], "subdivision": "",
                "country": ""}
    return {
        "place": ", ".join(parts),
        "city": parts[0],
        "subdivision": ", ".join(parts[1:-1]),
        "country": parts[-1],
    }


def _url(vertical: str, view: str, snapshot: str | None,
         region: str | None) -> str:
    seg = VERTICALS.get(vertical)
    if not seg:
        raise SourceUnavailable(
            f"unknown index '{vertical}'",
            "one of: " + ", ".join(sorted(VERTICALS)))
    # `current` is the absence of a title, not a title whose value is
    # "current". Numbeo answers an unrecognised `?title=` with the current
    # table, so sending it *worked* — while making one table reachable under
    # two cache keys, and while making "the URL carries a title" useless as a
    # test of whether a payload is an archive.
    title = None if snapshot == "current" else snapshot
    if view == "country":
        page, params = "rankings_by_country.jsp", {"title": title}
    elif view == "region":
        code = REGIONS.get((region or "").lower(), region)
        page, params = "region_rankings.jsp", {"title": title, "region": code}
    elif snapshot and snapshot != "current":
        page, params = "rankings.jsp", {"title": snapshot}
    else:
        page, params = "rankings_current.jsp", {}
    qs = urlencode(params)
    return f"{BASE}/{seg}/{page}" + (f"?{qs}" if qs else "")


def _parse(html: str, url: str) -> dict:
    table = find_table(html, table_id="t2")
    if table is None:
        # Distinguish "no such page" from "page changed shape": they have
        # different fixes and must not print the same sentence.
        body = text_of(html)
        if "Cannot find" in body or "no data" in body.lower():
            raise SourceUnavailable(
                f"{url} has no ranking table for that selection",
                "check the snapshot and region with `citycost snapshots`")
        ids = [t.id or f"class={sorted(t.classes)}" for t in parse_tables(html)]
        raise LayoutChanged(
            f"{url} loaded but table#t2 was absent (found: {ids[:6]})",
            "Numbeo changed its markup — this client needs updating")

    header = list(table.header)
    if not header:
        raise LayoutChanged(f"{url} table#t2 has no header row",
                            "Numbeo changed its markup — this client needs updating")

    # Column 0 is the rank cell, which Numbeo leaves EMPTY in the HTML and
    # fills in with DataTables JS. Reading it yields '' for every row, so rank
    # is computed here from the page's own order instead.
    place_i = table.column_index("City", "Country") or 1
    metric_cols = [(i, h) for i, h in enumerate(header)
                   if i > place_i and h.lower() not in ("rank",)]

    rows = []
    for order, cells in enumerate(table.rows, start=1):
        if len(cells) <= place_i:
            continue
        label = cells[place_i]
        if not label:
            continue
        rec = split_place(label)
        rec["rank"] = order
        metrics = {}
        for i, name in metric_cols:
            metrics[name] = _to_number(cells[i]) if i < len(cells) else None
        rec["metrics"] = metrics
        rows.append(rec)

    return {"url": url, "columns": [h for _, h in metric_cols], "rows": rows}


def is_archival(snapshot: str | None, *, now: float | None = None,
                moved: bool = False) -> bool:
    """May this snapshot be treated as a finished, unchanging artefact?

    Pure, and deliberately so — the deciding input is the snapshot id, which is
    already inside the cache key and therefore cannot be unavailable. The
    obvious alternative, asking `snapshots()` which id is newest, sits behind a
    network read; i.e. it is precisely the input that is missing during the
    seven-day address ban this whole feature exists to survive, and a missing
    list would then silently *grant* a pin to a newly published snapshot.

    Every unknown degrades toward MUTABLE, because the two errors are not
    symmetric. Under-pinning costs one request and, on a banned address,
    produces the existing loud 429 carrying the server's own deadline.
    Over-pinning produces a complete, plausible, internally consistent table
    that never expires and is protected from prune — a wrong number no future
    run can discover, which is the exact failure class every guard in this
    repository was written for, made permanent.

    So the rule is coarse on purpose: the whole calendar year the id names must
    have ended, plus one publication cycle. On 2026-08-25 that pins `2025-mid`
    and everything older, and refuses `2026` and `2026-mid` — the current
    half-year, which nothing has measured to be finished. The cost is two
    re-fetches per vertical out of 31.
    """
    if moved or not snapshot or snapshot == "current":
        return False
    if not SNAPSHOT_ID.fullmatch(str(snapshot)):
        return False
    try:
        year = int(str(snapshot)[:4])
    except ValueError:
        return False
    if year < EARLIEST_SNAPSHOT_YEAR:
        return False
    ended = _dt.datetime(year + 1, 1, 1, tzinfo=_dt.timezone.utc).timestamp()
    return (now if now is not None else time.time()) >= (
        ended + PUBLICATION_CYCLE_DAYS * 86400)


def panel_fingerprint(table: dict) -> str:
    """A stable digest of a whole parsed table: row count plus every
    (place, metrics) pair in page order.

    One owner, because two consumers ask the same question for opposite
    reasons. `fetch` compares it across time to falsify the claim that a pinned
    snapshot never moves; `movers` compares it across two snapshot ids to catch
    `?title=` being accepted and silently ignored. A second implementation
    would agree on the day it was written.
    """
    rows = (table or {}).get("rows") or []
    h = hashlib.sha256()
    # A table with no rows is not a table this can speak about: two empty
    # parses would otherwise fingerprint identically, and every consumer of
    # this digest reads "identical" as evidence of one specific upstream fault.
    if not rows:
        return ""
    h.update(str(len(rows)).encode())
    for r in rows:
        h.update(_json.dumps(
            [r.get("place"), sorted((r.get("metrics") or {}).items(),
                                    key=lambda kv: kv[0])],
            sort_keys=True, default=str).encode())
    return h.hexdigest()[:32]


def fetch(vertical: str = "cost-of-living", *, view: str = "city",
          snapshot: str | None = None, region: str | None = None,
          max_age: int = 21600) -> dict:
    """One ranking table.

    A titled snapshot old enough to be finished is written as an *archival*
    entry: no age clock, and out of reach of `prune_cache`. `max_age=0` still
    forces a live read of it — `doctor`'s whole contract is that no cache can
    answer it, and a conditionally-honoured 0 would reinstate the 1.2.0 defect
    where an hour-old cache reported a seven-day-banned address as `ok`.

    Every archival read also records a fingerprint of what it saw. That is what
    turns "this snapshot is immutable" from an assumption into a falsifiable
    claim: an id ever observed with two different contents is marked `_moved`
    and is permanently ineligible for pinning. A check that cannot fail is not
    a check, and this one costs no extra request.
    """
    url = _url(vertical, view, snapshot, region)
    arch = is_archival(snapshot)

    def _meta(payload, prior):
        fp = panel_fingerprint(payload)
        was = prior.get("fingerprint")
        moved = bool(was and was != fp) or bool(prior.get("moved"))
        if moved and arch:
            from . import render
            render.note(f"  ! {snapshot} was published as final and its "
                        f"contents changed ({was} -> {fp}); it will not be "
                        f"cached as an archive")
        return {"fingerprint": fp, "moved": moved, "snapshot": snapshot,
                "vertical": vertical, "url": url}

    payload, age, _ = cached_json(f"rank:{url}", SCHEMA, max_age,
                                  lambda: _parse(http_get(url), url),
                                  archival=arch, meta_hook=_meta)
    out = dict(payload or {})
    out["_age_s"] = age
    out["_archival"] = arch
    out["vertical"] = vertical
    out["view"] = view
    out["snapshot"] = snapshot or "current"
    out["region"] = region
    return out


def snapshots(vertical: str = "cost-of-living", *, max_age: int = 86400) -> list[str]:
    """The snapshot list, read off the page's own <select>.

    Hardcoding this list would be a second owner for a fact Numbeo already
    publishes, and would go stale on their schedule rather than ours.
    """
    seg = VERTICALS.get(vertical, vertical)
    url = f"{BASE}/{seg}/rankings.jsp"

    def produce():
        html = http_get(url)
        opts = re.findall(r'<option[^>]+value="([^"]+)"[^>]*>', html)
        # The page carries other <select>s (currency, display column); keep only
        # values that look like a Numbeo snapshot id.
        return [o for o in opts if SNAPSHOT_ID.fullmatch(o)]

    # `keep=True` without `archival`: the list is genuinely re-published, so it
    # expires on a clock — but it is also the only offline owner of "which
    # snapshots exist", so pruning it makes every pinned page unenumerable
    # during exactly the ban that made pinning worth doing.
    payload, _, _ = cached_json(f"snapshots:{url}", SCHEMA, max_age, produce,
                                keep=True)
    return list(payload or [])


def _norm(s: str) -> str:
    """NFKD, strip combining marks, lowercase, drop non-alphanumerics."""
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", s or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def find_place(table: dict, needle: str) -> list[dict]:
    """Every row matching `needle`, most specific match first — never just one.

    Returning a single "best" hit is what makes homonyms dangerous. Measured
    2026-08-25 across seven snapshots: **ten city-name keys resolve to two or
    more different cities**, and they co-exist in one snapshot with unstable
    order. The 2026-mid page lists ``Vancouver, WA, United States`` *before*
    ``Vancouver, Canada``; the 2025 page lists only ``Vancouver, Canada``. A
    lookup that took the first hit would splice a Washington suburb and a
    Canadian metro into one 17-year "trend" and nothing would look wrong.

    Substring matching is unsafe for the same reason: ``Nang`` matches
    ``Penang, Malaysia``, and ``York`` matches ``New York, NY, United States``.
    So substring is offered only as the last tier, and the caller is told how
    many candidates there were.
    """
    n = _norm(needle)
    if not n:
        return []
    full = [r for r in table["rows"] if _norm(r["place"]) == n]
    if full:
        return full
    city = [r for r in table["rows"] if _norm(r["city"]) == n]
    if city:
        return city
    return [r for r in table["rows"] if n in _norm(r["place"])]


def resolve_place(table: dict, needle: str) -> tuple[dict | None, list[dict]]:
    """`(chosen, candidates)`. Chosen is None when the answer is ambiguous.

    Ambiguity is returned rather than resolved, because the tie-break a
    computer would pick — first row, best rank, shortest name — is arbitrary,
    and an arbitrary choice presented as an answer is the failure this exists
    to prevent.
    """
    hits = find_place(table, needle)
    if len(hits) == 1:
        return hits[0], hits
    return None, hits


def trend(city: str, *, vertical: str = "cost-of-living", column: str | None = None,
          limit: int = 12, max_age: int = 86400) -> dict:
    """One city across successive snapshots, anchored to ONE full place label.

    The label is resolved once, from the first snapshot that contains the city,
    and every later snapshot is matched on that exact label. Re-resolving per
    snapshot is what lets a homonym swap in halfway through a series.

    Absence is preserved, never interpolated and never zeroed: Da Nang appears
    in 1 of 7 sampled snapshots (verified by raw text search of the saved HTML,
    not just by this parser), and a 0 on a cost index would say 'free' — a
    claim nobody made. Note also that snapshots are **not** monotonically
    growing (2022 carried 578 cities, 2026-mid carries 547), so an absence
    cannot be explained away as "the old table was smaller".
    """
    snaps = snapshots(vertical, max_age=max_age)[:limit]
    if not snaps:
        raise SourceUnavailable(
            f"no snapshot list found for '{vertical}'",
            "this vertical may be current-only; try `citycost rank` instead")

    series, columns, anchor, ambiguous = [], [], None, []
    for snap in snaps:
        try:
            table = fetch(vertical, snapshot=snap, max_age=max_age)
        except (SourceUnavailable, LayoutChanged) as exc:
            series.append({"snapshot": snap, "found": False,
                           "error": exc.message})
            continue
        columns = table["columns"] or columns

        if anchor is None:
            chosen, candidates = resolve_place(table, city)
            if chosen is None and candidates:
                ambiguous = [c["place"] for c in candidates]
                series.append({"snapshot": snap, "found": False,
                               "note": f"ambiguous: {len(candidates)} places match"})
                continue
            if chosen is None:
                series.append({"snapshot": snap, "found": False,
                               "note": "not ranked in this snapshot"})
                continue
            anchor = chosen["place"]

        hits = [r for r in table["rows"] if r["place"] == anchor]
        if not hits:
            series.append({"snapshot": snap, "found": False,
                           "note": "not ranked in this snapshot"})
            continue
        hit = hits[0]
        series.append({
            "snapshot": snap, "found": True, "place": hit["place"],
            "rank": hit["rank"], "of": len(table["rows"]),
            "metrics": hit["metrics"],
        })

    key = column or (columns[0] if columns else None)
    return {"city": city, "anchor": anchor, "ambiguous": ambiguous,
            "vertical": vertical, "column": key, "columns": columns,
            "series": series,
            "identical_across_snapshots": _is_one_table_repeated(series),
            "found_in": sum(1 for s in series if s.get("found")),
            "requested": len(snaps)}


def _is_one_table_repeated(series: list[dict]) -> bool:
    """True when every snapshot returned the *same* table — i.e. not history.

    `?title=` is a request, not a guarantee. A parameter can be accepted and
    silently **ignored** rather than rejected, and this one has no error path
    to fail into: the page would answer 200 with the current table for every
    snapshot, the series would come back complete with `found: True` on every
    point, and a flat line would be read as "this city is remarkably stable".
    Nothing raises, and the snapshot `<select>` this client reads its list
    from would still be on the page.

    Measured 2026-08-25, all seven verticals, oldest vs newest snapshot: every
    one honours it today (quality-of-life 95 rows in 2014 against 305 in
    2026-mid; cost-of-living 41 against 547). So this is an alarm for drift,
    not a description of the source as it stands.

    `of` is what makes the predicate safe to assert. A genuinely unchanging
    city still sits in tables of *different sizes* year to year, and Numbeo's
    snapshots are not monotonic — 2022 carried 578 cities, 2026-mid carries
    547. Identical metrics **and** an identical table size across three or
    more snapshots is the shape of one table fetched three times.
    """
    found = [s for s in series if s.get("found")]
    if len(found) < 3:
        return False
    fingerprints = {(tuple(sorted((s.get("metrics") or {}).items())), s.get("of"))
                    for s in found}
    return len(fingerprints) == 1
