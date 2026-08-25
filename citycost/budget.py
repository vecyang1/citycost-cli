"""Turning prices into a monthly figure, and knowing when that figure is wrong.

Two ideas carry this module.

**Strictness.** A budget missing one component returns ``None``, never a short
sum. A total that quietly omits utilities looks entirely reasonable and is read
as fact; ``N/A`` beside a stderr line naming the missing row is worse to look
at and infinitely better to act on.

**An independent control.** Our frugal basket runs a stable *fraction* of
nomads.com's local figure. Stability is the signal: the two baskets answer
different questions, so their ratio is not 1 and should never be forced to 1 —
but it is consistent, and a city that breaks rank is a scrape to re-read rather
than a bargain discovered.
"""

from __future__ import annotations

from statistics import median

#: component -> multiplier. Everything here recurs monthly; a one-off purchase
#: has no business in a monthly figure.
BASKET: tuple[tuple[str, int], ...] = (
    ("rent_1br_outside", 1),
    ("utilities", 1),
    ("internet", 1),
    ("mobile", 1),
    ("transport_pass", 1),
    ("cheap_meal", 30),
)

FAMILY_EXTRAS: tuple[tuple[str, int], ...] = (
    ("preschool", 1),
)

#: Metrics where a HIGHER number is the good one. Getting this wrong marked the
#: worst-paying city as best — measured: Da Nang's $355 salary flagged green
#: beside Chiang Mai's $605. The derived rows are where it was regressed a
#: second time, so they are listed explicitly rather than inferred.
HIGHER_IS_BETTER = frozenset({
    "net_salary", "Avg Net Salary", "Local Purchasing Power Index",
    "Purchasing Power Index", "Safety Index", "Health Care Index",
    "Climate Index", "Quality of Life Index", "Affordability Index",
    "Gross Rental Yield City Centre", "Gross Rental Yield Outside of Centre",
    "salary_budget_ratio", "monthly_savings",
})


def monthly_budget(values: dict, *, basket=BASKET) -> float | None:
    """Strict: any missing or None component yields None."""
    total = 0.0
    for key, mult in basket:
        v = values.get(key)
        if v is None:
            return None
        total += float(v) * mult
    return total


def budget_breakdown(values: dict, *, basket=BASKET) -> dict:
    """The same sum, plus what it is made of and what was missing.

    Returning the missing list beside the total is what lets a caller say *why*
    a budget is N/A without re-deriving it.
    """
    parts, missing = {}, []
    for key, mult in basket:
        v = values.get(key)
        if v is None:
            missing.append(key)
        else:
            parts[key] = float(v) * mult
    return {
        "total": None if missing else round(sum(parts.values()), 2),
        "parts": parts,
        "missing": missing,
    }


def savings(values: dict, budget: float | None) -> float | None:
    salary = values.get("net_salary")
    if salary is None or budget is None:
        return None
    return round(float(salary) - budget, 2)


def rent_to_income(values: dict) -> float | None:
    rent, salary = values.get("rent_1br_outside"), values.get("net_salary")
    if rent is None or not salary:
        return None
    return round(float(rent) / float(salary) * 100, 1)


def is_better(metric: str, a: float, b: float) -> bool:
    """True when `a` is the better value for `metric`."""
    return a > b if metric in HIGHER_IS_BETTER else a < b


def best_of(values: list, metric: str):
    """The good value for this metric, or None when there is nothing to compare.

    Refusing a verdict on a single value is deliberate: marking the only city
    green tells a reader a comparison happened.
    """
    nums = [v for v in values if v is not None]
    if len(nums) < 2:
        return None
    return max(nums) if metric in HIGHER_IS_BETTER else min(nums)


#: How far a city's ours/theirs ratio may stray from the group median before it
#: is called out. Wide on purpose: the two baskets genuinely differ by country,
#: and a band that fires on healthy input is a band that gets muted.
ANOMALY_LOW, ANOMALY_HIGH = 0.6, 1.6

#: Below this many comparable cities there is no median worth trusting, so the
#: check returns *no verdict* rather than a clean bill. "Nothing flagged" and
#: "nothing could be checked" are different answers.
MIN_SAMPLE = 3


def ratio_anomalies(ratios: dict[str, float | None]) -> dict[str, float]:
    """Cities whose ratio-to-the-median breaks rank, as {city: deviation}."""
    vals = sorted(v for v in ratios.values() if v is not None)
    if len(vals) < MIN_SAMPLE:
        return {}
    mid = median(vals)
    if not mid:
        return {}
    out = {}
    for city, r in ratios.items():
        if r is None:
            continue
        dev = r / mid
        if not ANOMALY_LOW <= dev <= ANOMALY_HIGH:
            out[city] = round(dev, 3)
    return out
