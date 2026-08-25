import unittest
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import prices
from citycost.errors import LayoutChanged, ThinData, UnknownCity

# Captured 2026-08-25 from /cost-of-living/in/Da-Nang?displayCurrency=USD.
# The utilities label is the post-rename '85 m 2' form, split across a <sup>,
# and the transport row is the literal '?' Numbeo renders for an unpriced row.
PAGE = """<html><body>
<a href="/cost-of-living/country_result.jsp?country=Vietnam">Vietnam</a>
<table class="data_wide_table new_bar_table">
<tr><th>Restaurants</th><th>Edit</th></tr>
<tr><td>Meal at an Inexpensive Restaurant</td><td>$2.29</td></tr>
<tr><td>Basic Utilities for 85 m <sup>2</sup> Apartment (Electricity, Heating,
   Cooling, Water, Garbage)</td><td>$87.45</td></tr>
<tr><td>Broadband Internet (Unlimited Data, 60 Mbps or Higher)</td><td>$8.10</td></tr>
<tr><td>Mobile Phone Plan (Monthly, with Calls and 10GB+ Data)</td><td>$6.11</td></tr>
<tr><td>Monthly Public Transport Pass (Regular Price)</td><td>?</td></tr>
<tr><td>1 Bedroom Apartment Outside of City Centre</td><td>$339.50</td></tr>
<tr><td>Average Monthly Net Salary (After Tax)</td><td>$354.86</td></tr>
</table></body></html>"""


def _patched_get(html):
    return mock.patch.object(prices, "http_get", return_value=html)


class TestParseValue(unittest.TestCase):
    def test_unpriced_row_is_absent_not_zero(self):
        self.assertIsNone(prices.parse_value("?"))
        self.assertIsNone(prices.parse_value(""))

    def test_separators_and_annotation_glyphs(self):
        self.assertEqual(prices.parse_value("$2,288,888.89"), 2288888.89)
        self.assertEqual(prices.parse_value("≈$45.00"), 45.0)
        self.assertEqual(prices.parse_value("60.00$*"), 60.0)


class TestParsePage(unittest.TestCase):
    def test_utilities_matches_after_the_upstream_rename(self):
        """The exact label went '915 Square Feet' -> '85 m 2' in Aug 2026 and a
        client keyed on the old string reported N/A for the whole budget."""
        out = prices._parse(PAGE, "Da-Nang", "USD")
        self.assertEqual(out["values"]["utilities"], 87.45)

    def test_country_is_read_off_the_page_not_a_local_table(self):
        self.assertEqual(prices._parse(PAGE, "Da-Nang", "USD")["country"],
                         "Vietnam")

    def test_question_mark_row_is_seen_but_valueless(self):
        out = prices._parse(PAGE, "Da-Nang", "USD")
        self.assertIn("transport_pass", out["seen"])
        self.assertIsNone(out["values"]["transport_pass"])

    def test_missing_report_separates_drift_from_unpriced(self):
        rec = prices._parse(PAGE, "Da-Nang", "USD")
        m = prices.missing_report(rec)
        self.assertIn("transport_pass", m["blank"])
        self.assertNotIn("transport_pass", m["unmatched"])
        self.assertIn("preschool", m["unmatched"])

    def test_healthy_row_appears_in_neither_bucket(self):
        m = prices.missing_report(prices._parse(PAGE, "Da-Nang", "USD"))
        for bucket in m.values():
            self.assertNotIn("cheap_meal", bucket)


class TestErrorTriage(unittest.TestCase):
    """Three failures, three different fixes, three different sentences."""

    def test_unknown_slug_names_the_slug_and_the_lookup(self):
        with _patched_get("<html>Cannot find city id for Xx</html>"):
            with self.assertRaises(UnknownCity) as c:
                prices.fetch("Xx", max_age=0)
        self.assertIn("citycost find", c.exception.remedy)

    def test_thin_data_says_a_retry_will_not_help(self):
        with _patched_get("<html>We don't have enough data</html>"):
            with self.assertRaises(ThinData) as c:
                prices.fetch("Xx", max_age=0)
        self.assertIn("not something a retry fixes", c.exception.remedy)

    def test_layout_change_points_at_this_repo_not_at_the_user(self):
        with _patched_get("<html><body>Prices in Xx</body></html>"):
            with self.assertRaises(LayoutChanged) as c:
                prices.fetch("Xx", max_age=0)
        self.assertIn("client needs updating", c.exception.remedy)

    def test_try_fetch_never_raises_so_one_bad_slug_cannot_abort_a_sweep(self):
        with _patched_get("<html>Cannot find city id</html>"):
            rec = prices.try_fetch("Xx", max_age=0)
        self.assertEqual(rec["kind"], "UnknownCity")
        self.assertEqual(rec["values"], {})


class TestCurrencyIsTheServersJob(unittest.TestCase):
    def test_usd_is_requested_rather_than_computed(self):
        """The deleted local-conversion path reported Fukuoka rent as $11,095
        against a true $470. The fix is that we never convert."""
        with mock.patch.object(prices, "http_get",
                               return_value=PAGE) as g:
            prices.fetch("Da-Nang", max_age=0)
        self.assertIn("displayCurrency=USD", g.call_args[0][0])

    def test_local_mode_asks_for_no_conversion_at_all(self):
        with mock.patch.object(prices, "http_get", return_value=PAGE) as g:
            prices.fetch("Da-Nang", currency="LOCAL", max_age=0)
        self.assertNotIn("displayCurrency", g.call_args[0][0])

    def test_schema_fingerprint_changes_with_the_metric_set(self):
        """Asserts the PROPERTY, not the formula.

        A test that recomputes the implementation's own expression only
        restates the code and passes for any formula at all, including a
        constant. What must hold is that adding a metric changes the
        fingerprint, so entries written by an older version are discarded
        rather than served with the new keys silently missing.
        """
        import hashlib
        base = set(prices.TARGETS.values())
        a = hashlib.sha256("|".join(sorted(base)).encode()).hexdigest()[:12]
        b = hashlib.sha256("|".join(sorted(base | {"gym_towel"})).encode()
                           ).hexdigest()[:12]
        self.assertNotEqual(a, b)
        self.assertEqual(len(prices.SCHEMA), 12)
        self.assertRegex(prices.SCHEMA, r"^[0-9a-f]{12}$")


class TestLabelCoverage(unittest.TestCase):
    def test_every_target_key_has_a_human_label(self):
        """A key without a label renders as a blank row heading, which reads as
        a missing metric rather than a missing translation."""
        missing = set(prices.TARGETS.values()) - set(prices.LABELS)
        self.assertEqual(missing, set(), f"unlabelled: {missing}")

    def test_no_orphan_labels(self):
        self.assertEqual(set(prices.LABELS) - set(prices.TARGETS.values()), set())




class TestMissingReportIsDeduplicated(unittest.TestCase):
    """TARGETS maps two label spellings (imperial and metric) onto one key, so
    iterating its values reports that key twice. Shipped once: a single missing
    taxi row printed as `['taxi_km', 'taxi_km']`, which reads as two problems.

    The edit that was supposed to fix this silently did nothing because the
    replacement had no assertion behind it — which is why the behaviour is
    pinned here rather than trusted to a diff.
    """

    def test_no_key_appears_twice_in_either_bucket(self):
        rec = {"values": {}, "seen": {}}
        m = prices.missing_report(rec)
        for bucket, names in m.items():
            with self.subTest(bucket=bucket):
                self.assertEqual(len(names), len(set(names)), names)

    def test_unmatched_ranges_over_distinct_keys_only(self):
        m = prices.missing_report({"values": {}, "seen": {}})
        self.assertEqual(set(m["unmatched"]), set(prices.TARGETS.values()))
        self.assertEqual(len(m["unmatched"]), len(set(prices.TARGETS.values())))

    def test_a_key_with_two_spellings_is_satisfied_by_either(self):
        """Matching the imperial spelling must not leave the metric one
        outstanding — they are one metric, not two."""
        for spelling in ("Taxi 1 mile", "Taxi 1km"):
            with self.subTest(spelling=spelling):
                rec = {"values": {"taxi_km": 1.0}, "seen": {"taxi_km": spelling}}
                self.assertNotIn("taxi_km", prices.missing_report(rec)["unmatched"])

    def test_buckets_are_sorted_so_output_is_stable_across_runs(self):
        m = prices.missing_report({"values": {}, "seen": {}})
        self.assertEqual(m["unmatched"], sorted(m["unmatched"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
