import unittest
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import rankings
from citycost.errors import LayoutChanged, SourceUnavailable
from .test_htmlparse import RANKING_HTML


class TestSplitPlace(unittest.TestCase):
    """The country is the LAST comma part, never the second.

    Every string below was taken from a live ranking page. A `split(',')[1]`
    parse is right for the first and wrong for four of the other five, while
    looking correct in any test written from the first alone.
    """

    CASES = [
        ("Da Nang, Vietnam", "Da Nang", "", "Vietnam"),
        ("Honolulu, HI, United States", "Honolulu", "HI", "United States"),
        ("Hong Kong, Hong Kong (China)", "Hong Kong", "", "Hong Kong (China)"),
        ("St. John's, Newfoundland and Labrador, Canada",
         "St. John's", "Newfoundland and Labrador", "Canada"),
        ("Pristina, Kosovo (Disputed Territory)",
         "Pristina", "", "Kosovo (Disputed Territory)"),
        ("Rostov-on-Don (Rostov-na-donu), Russia",
         "Rostov-on-Don (Rostov-na-donu)", "", "Russia"),
        ("Xi'an, China", "Xi'an", "", "China"),
    ]

    def test_every_observed_form(self):
        for label, city, sub, country in self.CASES:
            got = rankings.split_place(label)
            self.assertEqual(got["city"], city, label)
            self.assertEqual(got["subdivision"], sub, label)
            self.assertEqual(got["country"], country, label)

    def test_single_part_label_has_no_country_rather_than_a_wrong_one(self):
        got = rankings.split_place("Singapore")
        self.assertEqual(got["city"], "Singapore")
        self.assertEqual(got["country"], "")


class TestNumericCells(unittest.TestCase):
    def test_absent_markers_are_none_not_zero(self):
        """A 0 on a cost index means 'free', which is a claim nobody made — and
        it sorts the city straight to the top of a 'cheapest' list."""
        for marker in ("", "?", "-", "--", "N/A", "n/a", " "):
            self.assertIsNone(rankings._to_number(marker), repr(marker))

    def test_real_numbers_parse(self):
        self.assertEqual(rankings._to_number("123.1"), 123.1)
        self.assertEqual(rankings._to_number("1,234.5"), 1234.5)
        self.assertEqual(rankings._to_number("-3.2"), -3.2)

    def test_non_numeric_text_is_none_not_a_partial_number(self):
        self.assertIsNone(rankings._to_number("about 40"))
        self.assertIsNone(rankings._to_number("40-100"))


class TestParse(unittest.TestCase):
    def test_rank_is_computed_from_page_order_since_html_leaves_it_empty(self):
        out = rankings._parse(RANKING_HTML, "u")
        self.assertEqual([r["rank"] for r in out["rows"]], [1, 2, 3])

    def test_columns_come_from_the_header_not_from_a_hardcoded_list(self):
        out = rankings._parse(RANKING_HTML, "u")
        self.assertEqual(out["columns"], ["Cost of Living Index", "Rent Index"])

    def test_metrics_are_keyed_by_label(self):
        out = rankings._parse(RANKING_HTML, "u")
        self.assertEqual(out["rows"][0]["metrics"]["Cost of Living Index"], 123.1)

    def test_a_page_without_the_table_is_a_layout_change_not_a_missing_city(self):
        with self.assertRaises(LayoutChanged) as ctx:
            rankings._parse("<html><table id='other'></table></html>", "u")
        self.assertIn("client needs updating", ctx.exception.remedy)


class TestUrlBuilding(unittest.TestCase):
    def test_current_view_uses_the_current_page(self):
        self.assertTrue(rankings._url("cost-of-living", "city", None, None)
                        .endswith("/cost-of-living/rankings_current.jsp"))

    def test_snapshot_switches_to_the_historical_page(self):
        u = rankings._url("cost-of-living", "city", "2020", None)
        self.assertIn("rankings.jsp?title=2020", u)

    def test_region_name_resolves_to_the_un_code_numbeo_publishes(self):
        self.assertIn("region=142",
                      rankings._url("cost-of-living", "region", "2026-mid", "asia"))

    def test_country_view(self):
        self.assertIn("rankings_by_country.jsp",
                      rankings._url("crime", "country", None, None))

    def test_unknown_vertical_lists_the_valid_ones_rather_than_just_refusing(self):
        with self.assertRaises(SourceUnavailable) as ctx:
            rankings._url("cost-of-livng", "city", None, None)
        self.assertIn("cost-of-living", ctx.exception.remedy)


class TestFindPlace(unittest.TestCase):
    def setUp(self):
        self.table = rankings._parse(RANKING_HTML, "u")

    def test_exact_then_fuzzy(self):
        self.assertEqual(len(rankings.find_place(self.table, "Zurich")), 1)
        self.assertEqual(len(rankings.find_place(self.table, "zurich")), 1)
        self.assertEqual(len(rankings.find_place(self.table, "hong kong")), 1)

    def test_absent_city_returns_empty_not_a_false_match(self):
        self.assertEqual(rankings.find_place(self.table, "Zzzz Nowhere"), [])

    def test_empty_needle_matches_nothing_rather_than_everything(self):
        self.assertEqual(rankings.find_place(self.table, "  "), [])


class TestTrendPreservesAbsence(unittest.TestCase):
    def test_a_city_missing_from_a_snapshot_is_unknown_not_zero(self):
        with mock.patch.object(rankings, "snapshots",
                               return_value=["2026", "2010"]), \
             mock.patch.object(rankings, "fetch",
                               return_value=rankings._parse(RANKING_HTML, "u")):
            out = rankings.trend("Zzzz Nowhere", limit=2)
        self.assertEqual(out["found_in"], 0)
        self.assertEqual(out["requested"], 2)
        for s in out["series"]:
            self.assertFalse(s["found"])
            self.assertNotIn("metrics", s)

    def test_a_present_city_carries_its_rank_and_the_field_size(self):
        with mock.patch.object(rankings, "snapshots", return_value=["2026"]), \
             mock.patch.object(rankings, "fetch",
                               return_value=rankings._parse(RANKING_HTML, "u")):
            out = rankings.trend("Zurich", limit=1)
        s = out["series"][0]
        self.assertTrue(s["found"])
        self.assertEqual((s["rank"], s["of"]), (1, 3))


if __name__ == "__main__":
    unittest.main(verbosity=2)


HOMONYM_HTML = """
<table id="t2"><thead><tr><th>Rank</th><th>City</th>
<th><div>Health Care<br/>Exp. Index</div></th></tr></thead><tbody>
<tr><td></td><td>Vancouver, WA, United States</td><td>69.2</td></tr>
<tr><td></td><td>Vancouver, Canada</td><td>68.5</td></tr>
<tr><td></td><td>Penang, Malaysia</td><td>33.6</td></tr>
<tr><td></td><td>Da Nang, Vietnam</td><td>27.2</td></tr>
</tbody></table>
"""


class TestHomonymSafety(unittest.TestCase):
    """Measured 2026-08-25 across seven snapshots: ten city-name keys resolve
    to two or more different cities, co-existing in one snapshot with unstable
    order. 2026-mid lists Vancouver-WA *before* Vancouver-BC; 2025 has only
    Vancouver-BC. Taking the first hit splices them into one 'trend'."""

    def setUp(self):
        self.table = rankings._parse(HOMONYM_HTML, "u")

    def test_an_ambiguous_name_returns_every_candidate(self):
        hits = rankings.find_place(self.table, "Vancouver")
        self.assertEqual(len(hits), 2)

    def test_resolve_refuses_to_choose_rather_than_choosing_arbitrarily(self):
        chosen, candidates = rankings.resolve_place(self.table, "Vancouver")
        self.assertIsNone(chosen)
        self.assertEqual(len(candidates), 2)

    def test_a_full_label_disambiguates(self):
        chosen, _ = rankings.resolve_place(self.table, "Vancouver, Canada")
        self.assertEqual(chosen["place"], "Vancouver, Canada")

    def test_exact_city_match_beats_substring_so_da_nang_is_not_penang(self):
        """'Nang' is a substring of 'Penang'. An exact city tier must win."""
        hits = rankings.find_place(self.table, "Da Nang")
        self.assertEqual([h["place"] for h in hits], ["Da Nang, Vietnam"])

    def test_substring_remains_available_but_only_as_the_last_tier(self):
        hits = rankings.find_place(self.table, "Nang")
        self.assertEqual(len(hits), 2)   # Penang AND Da Nang — reported, not hidden

    def test_trend_reports_ambiguity_instead_of_producing_a_spliced_series(self):
        with mock.patch.object(rankings, "snapshots", return_value=["2026", "2025"]), \
             mock.patch.object(rankings, "fetch",
                               return_value=rankings._parse(HOMONYM_HTML, "u")):
            out = rankings.trend("Vancouver", limit=2)
        self.assertTrue(out["ambiguous"])
        self.assertEqual(out["found_in"], 0)

    def test_trend_anchors_on_one_full_label_for_the_whole_series(self):
        with mock.patch.object(rankings, "snapshots", return_value=["2026", "2025"]), \
             mock.patch.object(rankings, "fetch",
                               return_value=rankings._parse(HOMONYM_HTML, "u")):
            out = rankings.trend("Vancouver, Canada", limit=2)
        self.assertEqual(out["anchor"], "Vancouver, Canada")
        self.assertEqual(out["found_in"], 2)
        for s in out["series"]:
            self.assertEqual(s["place"], "Vancouver, Canada")


class TestBrTagInHeader(unittest.TestCase):
    def test_a_br_inside_a_header_becomes_a_space_not_a_join(self):
        """Numbeo writes '<div>Health Care<br/>Exp. Index</div>'. Naive text
        extraction yields 'Health CareExp. Index' with no space, and a client
        keying on the on-screen label then never matches."""
        t = rankings._parse(HOMONYM_HTML, "u")
        self.assertEqual(t["columns"], ["Health Care Exp. Index"])

    def test_the_column_is_addressable_by_its_visible_label(self):
        from citycost.htmlparse import find_table
        tbl = find_table(HOMONYM_HTML, table_id="t2")
        self.assertEqual(tbl.column_index("Health Care Exp. Index"), 2)
