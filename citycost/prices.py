"""One Numbeo city page: itemised real prices, converted by Numbeo, not by us.

The whole currency layer that used to live here is gone. Requesting
`?displayCurrency=USD` makes the server return every figure already converted,
which deleted three defect classes in one line:

  * the ambiguous ``¥`` (CNY vs JPY) behind a three-city allowlist, which
    reported **Fukuoka rent as $11,095 against a true $470** — measured
    2026-08-25, a 23.6x error in a tool used to decide where to live;
  * a greedy symbol regex that fused annotation glyphs onto the currency
    (``≈$45.00``) and discarded an otherwise valid number;
  * a hard dependency on an exchange-rate API that raised on *every* city,
    USD ones included, whenever it was down.

An allowlist of city names is only ever right for the cities somebody
remembered. Asking the source to render the unit you want is a read; deriving
it downstream from someone else's ambiguous rendering is inference.
"""

from __future__ import annotations

import hashlib
import re

from .errors import LayoutChanged, SourceUnavailable, ThinData, UnknownCity
from .htmlparse import find_table, squash, text_of
from .net import cached_json, http_get

BASE = "https://www.numbeo.com/cost-of-living/in"

#: Numbeo renders **imperial or metric depending on the client's geography**,
#: and there is no query parameter that pins it (measurementSystem, measurement,
#: units and displayMeasurement were all tested 2026-08-25 and all ignored; the
#: real control is a cookie set through measurement_settings.jsp).
#:
#: The same page therefore serves either
#:     "Basic Utilities for 915 Square Feet Apartment"   (US exit IP)
#:     "Basic Utilities for 85 m 2 Apartment"            (non-US exit IP)
#: and either
#:     "Price per Square Feet to Buy Apartment in City Centre"
#:     "Price per Square Meter to Buy Apartment in City Centre"
#:
#: Those two pairs are NOT the same kind of difference. 915 sq ft *is* 85 m², so
#: the utilities basket is identical and its value is directly comparable. Price
#: per square foot and per square metre differ by 10.76x, and a client that maps
#: both to one key silently mixes two scales — the exact defect this project
#: exists to avoid. Same for "Taxi 1 mile" vs "Taxi 1km".
#:
#: So: match both spellings, detect which system was served, normalise the
#: scale-dependent values to metric, and say in the output which system the
#: source used. Pinning was preferred and is not available; detecting works
#: whatever Numbeo changes next.
IMPERIAL_MARKERS = ("Square Feet", "1 lb", "1 mile", "(1 Pint)", "12 oz")

#: Keys that are NOT money. Rendering a 4.67% mortgage rate as "$4.7" is a
#: small thing that makes every number on the row suspect.
NON_MONEY = frozenset({"mortgage_rate_pct"})

#: Metric is this tool's canonical output, whatever the source served. Numbeo
#: decides by geography — a US exit IP gets square feet and pounds — so without
#: a fixed output unit the same command answers differently depending on where
#: the user sits, and two users comparing notes silently compare two scales.
#: `units="source"` opts out and keeps whatever arrived, labelled.
DEFAULT_UNITS = "metric"

#: key -> (factor, from_unit, to_unit) applied ONLY when imperial was served.
IMPERIAL_TO_METRIC = {
    "buy_sqm_center": (10.7639, "per sq ft", "per m2"),
    "buy_sqm_outside": (10.7639, "per sq ft", "per m2"),
    "taxi_km": (1 / 1.60934, "per mile", "per km"),
}

#: Label prefix -> short key. Matched as a **prefix**, never exactly, and every
#: unit-dependent row lists both spellings.
TARGETS: dict[str, str] = {
    "Meal at an Inexpensive Restaurant": "cheap_meal",
    "Meal for Two at a Mid-Range Restaurant": "meal_for_two",
    "Cappuccino": "cappuccino",
    "Monthly Public Transport Pass": "transport_pass",
    "Taxi 1 mile": "taxi_km",
    "Taxi 1km": "taxi_km",
    "Gasoline": "gasoline_l",
    "Basic Utilities for": "utilities",
    "Broadband Internet": "internet",
    "Mobile Phone Plan": "mobile",
    "Monthly Fitness Club Membership": "gym",
    "Fitness Club": "gym",
    "Cinema Ticket": "cinema",
    "Private Full-Day Preschool or Kindergarten": "preschool",
    "Preschool (or Kindergarten)": "preschool",
    "International Primary School": "intl_school",
    "1 Bedroom Apartment in City Centre": "rent_1br_center",
    "1 Bedroom Apartment Outside of City Centre": "rent_1br_outside",
    "3 Bedroom Apartment in City Centre": "rent_3br_center",
    "3 Bedrooms Apartment in City Centre": "rent_3br_center",
    "3 Bedroom Apartment Outside of City Centre": "rent_3br_outside",
    "3 Bedrooms Apartment Outside of City Centre": "rent_3br_outside",
    "Price per Square Feet to Buy Apartment in City Centre": "buy_sqm_center",
    "Price per Square Meter to Buy Apartment in City Centre": "buy_sqm_center",
    "Price per Square Feet to Buy Apartment Outside of Centre": "buy_sqm_outside",
    "Price per Square Meter to Buy Apartment Outside of Centre": "buy_sqm_outside",
    "Average Monthly Net Salary": "net_salary",
    "Annual Mortgage Interest Rate": "mortgage_rate_pct",
}

#: Human labels for output, kept beside TARGETS so a renamed key cannot drift
#: from its heading. Units are stated where a reader could otherwise guess.
LABELS: dict[str, str] = {
    "cheap_meal": "Cheap Meal", "meal_for_two": "Meal for Two",
    "cappuccino": "Cappuccino", "transport_pass": "Transport Pass",
    "taxi_km": "Taxi (per km)", "gasoline_l": "Gasoline (1L)",
    "utilities": "Utilities (85m2)", "internet": "Internet", "mobile": "Mobile",
    "gym": "Gym", "cinema": "Cinema", "preschool": "Preschool",
    "intl_school": "Intl School (/yr)",
    "rent_1br_center": "Rent 1BR Center", "rent_1br_outside": "Rent 1BR Outside",
    "rent_3br_center": "Rent 3BR Center", "rent_3br_outside": "Rent 3BR Outside",
    "buy_sqm_center": "Buy (per m2, centre)",
    "buy_sqm_outside": "Buy (per m2, outside)",
    "net_salary": "Avg Net Salary", "mortgage_rate_pct": "Mortgage Rate %",
}

SCHEMA = hashlib.sha256(
    ("|".join(sorted(set(TARGETS.values()))) + "|v3-units").encode()).hexdigest()[:12]

_NUM = re.compile(r"[\d,]+\.?\d*")


def parse_value(text: str):
    """The number in a price cell, or None.

    Currency is fixed by the request, so only digits matter. Numbeo renders
    ``?`` for a row nobody has priced — that is an absence, never a zero, and a
    zero here would put a city at the top of a 'cheapest' list on the strength
    of having no data.
    """
    m = _NUM.search(text or "")
    return float(m.group().replace(",", "")) if m else None


def missing_report(record: dict) -> dict[str, list[str]]:
    """Two absences with two different remedies, kept apart.

    ``unmatched`` means the row label drifted upstream and *this client* needs
    updating. ``blank`` means Numbeo rendered ``?`` and there is nothing to do.
    Printing one sentence for both is how a user spends an afternoon on the
    wrong problem.
    """
    if record.get("error"):
        return {"unmatched": [], "blank": []}
    vals = record.get("values") or {}
    seen = record.get("seen") or {}
    # De-duplicated: TARGETS holds two spellings for every unit-dependent row
    # (imperial and metric), so iterating its values reports `taxi_km` twice
    # and makes one missing row look like two.
    keys = set(TARGETS.values())
    return {
        "unmatched": sorted(k for k in keys if k not in seen),
        "blank": sorted(k for k in keys
                        if k in seen and vals.get(k) is None),
    }


def _parse(html: str, slug: str, currency: str,
           units: str = DEFAULT_UNITS) -> dict:
    body = text_of(html)
    if "Cannot find city id" in body:
        raise UnknownCity(
            f"Numbeo has no page for slug '{slug}'",
            "list the real spellings with `citycost find <Country>`, or search "
            "numbeo.com directly — a genuinely missing slug answers "
            "'Cannot find city id'")
    if "We don't have" in body:
        raise ThinData(
            f"Numbeo has a page for '{slug}' but not enough submitted prices",
            "pick a larger nearby city; this is not something a retry fixes")

    table = find_table(html, css_class="data_wide_table")
    if table is None:
        raise LayoutChanged(
            f"'{slug}' loaded but table.data_wide_table was absent",
            "Numbeo changed its markup — this client needs updating")

    labels_blob = " ".join(squash(c[0]) for c in table.rows if c)
    system = ("imperial" if any(m in labels_blob for m in IMPERIAL_MARKERS)
              else "metric")

    values, raw, seen, converted = {}, {}, {}, {}
    for cells in table.rows:
        if len(cells) < 2:
            continue
        label = squash(cells[0])
        for prefix, key in TARGETS.items():
            if key in seen:
                continue
            if label.startswith(prefix) or prefix in label:
                seen[key] = label
                raw[key] = squash(cells[1])
                values[key] = parse_value(cells[1])
                break

    # Normalise the scale-dependent rows so one key always means one unit,
    # whichever system the source happened to serve this request.
    if system == "imperial" and units == "metric":
        for key, (factor, frm, to) in IMPERIAL_TO_METRIC.items():
            if values.get(key) is not None:
                values[key] = round(values[key] * factor, 4)
                converted[key] = f"{frm} -> {to} (x{factor:.4f})"

    country = ""
    m = re.search(r"country=([A-Za-z%+\-']+)", html)
    if m:
        from urllib.parse import unquote_plus
        country = unquote_plus(m.group(1))

    return {"slug": slug, "country": country, "currency": currency,
            "measurement_system": system, "output_units": units,
            "converted": converted,
            "values": values, "raw": raw, "seen": seen}


def fetch(slug: str, *, currency: str = "USD", units: str = DEFAULT_UNITS,
          max_age: int = 3600) -> dict:
    """Prices for one city.

    `currency="LOCAL"` asks Numbeo for no currency conversion at all.
    `units="source"` keeps whichever measurement system Numbeo served instead of
    normalising to metric.
    """
    slug = slug.strip().replace(" ", "-")
    url = f"{BASE}/{slug}"
    if currency != "LOCAL":
        url += f"?displayCurrency={currency}"

    payload, age, cached = cached_json(
        f"prices:{slug.lower()}:{currency}:{units}", SCHEMA, max_age,
        lambda: _parse(http_get(url), slug, currency, units))
    out = dict(payload or {})
    out["_age_s"] = age
    out["_cached"] = cached
    out["url"] = url
    return out


def try_fetch(slug: str, **kw) -> dict:
    """Never raises. Returns the record, or one carrying `error`/`remedy`.

    Used by anything sweeping several cities, where one unknown slug must not
    abort the other nine.
    """
    try:
        return fetch(slug, **kw)
    except (UnknownCity, ThinData, LayoutChanged, SourceUnavailable) as exc:
        return {"slug": slug, "values": {}, "raw": {}, "seen": {},
                "error": exc.message, "remedy": exc.remedy,
                "kind": type(exc).__name__}
