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

import re

from .errors import LayoutChanged, SourceUnavailable
from .htmlparse import find_table, parse_tables, squash, text_of
from .net import cached_json, http_get, urlencode

BASE = "https://www.numbeo.com"
SCHEMA = "numbeo-rankings-2"

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
    if view == "country":
        page, params = "rankings_by_country.jsp", {"title": snapshot}
    elif view == "region":
        code = REGIONS.get((region or "").lower(), region)
        page, params = "region_rankings.jsp", {"title": snapshot, "region": code}
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


def fetch(vertical: str = "cost-of-living", *, view: str = "city",
          snapshot: str | None = None, region: str | None = None,
          max_age: int = 21600) -> dict:
    """One ranking table. Cached longer than city prices: an index snapshot is
    a published artefact that does not move between requests."""
    url = _url(vertical, view, snapshot, region)
    payload, age, _ = cached_json(f"rank:{url}", SCHEMA, max_age,
                                  lambda: _parse(http_get(url), url))
    out = dict(payload or {})
    out["_age_s"] = age
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
        return [o for o in opts if re.fullmatch(r"\d{4}(-mid)?", o)]

    payload, _, _ = cached_json(f"snapshots:{url}", SCHEMA, max_age, produce)
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
