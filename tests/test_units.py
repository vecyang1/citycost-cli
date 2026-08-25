"""Numbeo renders one page in two measurement systems. Both must read alike.

The bug this file exists to stop shipping again: `TARGETS` spelled the taxi row
"Taxi 1km" while Numbeo serves "Taxi 1 km (Standard Tariff)". The prefix
matched nothing, so that row was unreadable on **every metric-rendered page** —
which, from a metric country, is every page. The client warned honestly on each
run and the warning was read as upstream drift, because a single rendering
cannot show you what it is missing; only the other rendering can.

So the assertion here is not "these labels parse". It is **the two renderings
must yield the identical key set**, ranging over all of `TARGETS`, with the
count printed — a spelling that silently covers one system and not the other
then fails as a difference rather than as a plausible absence.

The labels below are the real spellings, captured 2026-08-25 by fetching one
city twice within the same minute through two exit IPs. The prices are
invented: Numbeo's figures are proprietary and are not redistributed here, and
a fixture does not need real ones to prove a label matches.
"""

import unittest

from . import _sandbox  # noqa: F401
from citycost import prices

#: key -> (metric label, imperial label). Identical where Numbeo does not vary.
LABELS = {
    "cheap_meal": ("Meal at an Inexpensive Restaurant",) * 2,
    "meal_for_two": ("Meal for Two at a Mid-Range Restaurant (Three Courses, "
                     "Without Drinks)",) * 2,
    "cappuccino": ("Cappuccino (Regular Size)",) * 2,
    "transport_pass": ("Monthly Public Transport Pass (Regular Price)",) * 2,
    "gasoline_l": ("Gasoline (1 Liter)",) * 2,
    "internet": ("Broadband Internet (Unlimited Data, 60 Mbps or Higher)",) * 2,
    "mobile": ("Mobile Phone Plan (Monthly, with Calls and 10GB+ Data)",) * 2,
    "gym": ("Monthly Fitness Club Membership",) * 2,
    "cinema": ("Cinema Ticket (International Release)",) * 2,
    "preschool": ("Private Full-Day Preschool or Kindergarten, Monthly Fee "
                  "per Child",) * 2,
    "intl_school": ("International Primary School, Annual Tuition per Child",) * 2,
    "rent_1br_center": ("1 Bedroom Apartment in City Centre",) * 2,
    "rent_1br_outside": ("1 Bedroom Apartment Outside of City Centre",) * 2,
    "rent_3br_center": ("3 Bedroom Apartment in City Centre",) * 2,
    "rent_3br_outside": ("3 Bedroom Apartment Outside of City Centre",) * 2,
    "net_salary": ("Average Monthly Net Salary (After Tax)",) * 2,
    "mortgage_rate_pct": ("Annual Mortgage Interest Rate (20-Year Fixed, in %)",) * 2,
    # The four that actually differ.
    "taxi_km": ("Taxi 1 km (Standard Tariff)",
                "Taxi 1 mile (Standard Tariff)"),
    "utilities": ("Basic Utilities for 85 m2 Apartment (Electricity, Heating, "
                  "Cooling, Water, Garbage)",
                  "Basic Utilities for 915 Square Feet Apartment (Electricity, "
                  "Heating, Cooling, Water, Garbage)"),
    "buy_sqm_center": ("Price per Square Meter to Buy Apartment in City Centre",
                       "Price per Square Feet to Buy Apartment in City Centre"),
    "buy_sqm_outside": ("Price per Square Meter to Buy Apartment Outside of Centre",
                        "Price per Square Feet to Buy Apartment Outside of Centre"),
}

#: Invented, and deliberately the same number for every row in both systems, so
#: any value that moves between the two renderings moved because of a unit
#: conversion and not because the fixture said so.
PRICE = "$100.00"


def page(system: str) -> str:
    idx = 0 if system == "metric" else 1
    rows = "\n".join(f"<tr><td>{v[idx]}</td><td>{PRICE}</td></tr>"
                     for v in LABELS.values())
    return ('<html><body>'
            '<a href="/cost-of-living/country_result.jsp?country=Vietnam">VN</a>'
            f'<table class="data_wide_table"><tr><th>H</th><th>Edit</th></tr>'
            f'{rows}</table></body></html>')


class MeasurementSystemParity(unittest.TestCase):

    def test_fixture_covers_every_target_key(self):
        """The denominator, stated. A key added to TARGETS and not to LABELS
        would otherwise make this whole file quietly stop testing it."""
        expected = set(prices.TARGETS.values())
        self.assertEqual(set(LABELS), expected,
                         "tests/test_units.py LABELS has drifted from "
                         "prices.TARGETS; add the two real spellings")
        self.assertEqual(len(expected), 21)

    def test_both_renderings_are_detected(self):
        self.assertEqual(
            prices._parse(page("metric"), "X", "USD")["measurement_system"],
            "metric")
        self.assertEqual(
            prices._parse(page("imperial"), "X", "USD")["measurement_system"],
            "imperial")

    def test_identical_key_set_from_either_rendering(self):
        m = prices._parse(page("metric"), "X", "USD")
        i = prices._parse(page("imperial"), "X", "USD")
        readable = lambda rec: {k for k, v in rec["values"].items()
                                if v is not None}
        self.assertEqual(readable(m), readable(i))
        self.assertEqual(readable(m), set(prices.TARGETS.values()))

    def test_neither_rendering_reports_a_missing_row(self):
        for system in ("metric", "imperial"):
            with self.subTest(system=system):
                rec = prices._parse(page(system), "X", "USD")
                self.assertEqual(prices.missing_report(rec),
                                 {"unmatched": [], "blank": []})

    def test_taxi_row_reads_under_metric(self):
        """The specific regression. Kept as its own named test so that if it
        breaks again the failure says what broke, not just 'a key set differs'."""
        rec = prices._parse(page("metric"), "X", "USD")
        self.assertIsNotNone(rec["values"]["taxi_km"])

    def test_only_scale_dependent_rows_are_converted(self):
        m = prices._parse(page("metric"), "X", "USD")
        i = prices._parse(page("imperial"), "X", "USD")
        moved = {k for k in m["values"]
                 if m["values"][k] != i["values"][k]}
        self.assertEqual(moved, set(prices.IMPERIAL_TO_METRIC),
                         "a row changed value between renderings without being "
                         "declared scale-dependent, or vice versa")

    def test_conversion_factors_match_the_measured_ground_truth(self):
        """Verified live 2026-08-25 against one city read twice: $1.01/mile and
        $0.63/km, $354.72/sq ft and $3818.15/m2 — the same prices, two scales.
        """
        i = prices._parse(page("imperial"), "X", "USD")["values"]
        self.assertAlmostEqual(i["taxi_km"], 100 / 1.60934, places=2)
        self.assertAlmostEqual(i["buy_sqm_center"], 100 * 10.7639, places=2)

    def test_units_source_keeps_what_arrived(self):
        i = prices._parse(page("imperial"), "X", "USD", units="source")
        self.assertAlmostEqual(i["values"]["taxi_km"], 100.0, places=2)
        self.assertEqual(i["measurement_system"], "imperial")

    def test_utilities_is_the_same_basket_in_both(self):
        """915 sq ft *is* 85 m2, so this row must NOT be converted — unlike
        price-per-area, where the two spellings differ by 10.76x."""
        m = prices._parse(page("metric"), "X", "USD")["values"]
        i = prices._parse(page("imperial"), "X", "USD")["values"]
        self.assertEqual(m["utilities"], i["utilities"])


class CacheFingerprint(unittest.TestCase):
    """The fingerprint must change when anything that shapes a cached payload
    changes. It covered only `TARGETS.values()`, so the taxi *prefix* fix
    changed nothing and stale entries parsed under the broken spelling were
    still served as current — the cache certifying the bug after the fix."""

    def _schema_with(self, targets=None, conversions=None):
        # Calls the shipped function rather than restating its formula: a copy
        # here would have to be edited alongside any real change, and would then
        # agree with it by construction instead of checking it.
        return prices.schema_for(
            prices.TARGETS if targets is None else targets,
            prices.IMPERIAL_TO_METRIC if conversions is None else conversions)

    def test_the_constant_is_derived_from_the_live_tables(self):
        self.assertEqual(self._schema_with(), prices.SCHEMA)

    def test_renaming_a_label_prefix_changes_the_fingerprint(self):
        broken = dict(prices.TARGETS)
        del broken["Taxi 1 km"]
        broken["Taxi 1km"] = "taxi_km"
        self.assertNotEqual(self._schema_with(targets=broken), prices.SCHEMA)

    def test_changing_a_conversion_factor_changes_the_fingerprint(self):
        other = dict(prices.IMPERIAL_TO_METRIC)
        other["taxi_km"] = (1.0, "per mile", "per km")
        self.assertNotEqual(self._schema_with(conversions=other), prices.SCHEMA)

    def test_adding_a_metric_changes_the_fingerprint(self):
        more = dict(prices.TARGETS)
        more["Something New"] = "something_new"
        self.assertNotEqual(self._schema_with(targets=more), prices.SCHEMA)


if __name__ == "__main__":
    unittest.main()
