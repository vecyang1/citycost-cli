import unittest

from . import _sandbox  # noqa: F401  (must be first)
from citycost.htmlparse import find_table, parse_tables, squash, text_of

# Captured verbatim from https://www.numbeo.com/cost-of-living/rankings.jsp
# on 2026-08-25 — including the EMPTY first <td>, which is the whole point:
# Numbeo injects the rank with DataTables JS and it is absent from the HTML.
RANKING_HTML = """
<table id="t2" class="stripe row-border order-column compact">
<thead><tr>
  <th><div style="font-size: 80%;">Rank</div></th>
  <th><div style="font-size: 95%;">City</div></th>
  <th><div>Cost of Living Index</div></th>
  <th><div>Rent Index</div></th>
</tr></thead>
<tbody>
<tr style="width: 100%"><td></td>
  <td class="cityOrCountryInIndicesTable">Zurich, Switzerland</td>
  <td style="text-align: right">123.1</td><td style="text-align: right">74.5</td></tr>
<tr style="width: 100%"><td></td>
  <td class="cityOrCountryInIndicesTable">Honolulu, HI, United States</td>
  <td style="text-align: right">105.4</td><td style="text-align: right">63.9</td></tr>
<tr style="width: 100%"><td></td>
  <td class="cityOrCountryInIndicesTable">Hong Kong, Hong Kong (China)</td>
  <td style="text-align: right">76.4</td><td style="text-align: right">59.1</td></tr>
</tbody></table>
"""

PRICE_HTML = """
<table class="data_wide_table new_bar_table">
<tr><td>Meal at an Inexpensive Restaurant</td><td>$2.29</td></tr>
<tr><td>Basic Utilities for 85 m <sup>2</sup> Apartment (Electricity, Heating,
    Cooling, Water, Garbage)</td><td>$87.45</td></tr>
<tr><td>Monthly Public Transport Pass (Regular Price)</td><td>?</td></tr>
</table>
"""


class TestTableExtraction(unittest.TestCase):
    def test_header_labels_are_read_not_assumed(self):
        t = find_table(RANKING_HTML, table_id="t2")
        self.assertEqual(t.header,
                         ["Rank", "City", "Cost of Living Index", "Rent Index"])

    def test_header_row_is_not_counted_as_data(self):
        """A <th> row rendered as a city is how a column label becomes a place."""
        t = find_table(RANKING_HTML, table_id="t2")
        self.assertEqual(len(t.rows), 3)
        self.assertNotIn("City", [r[1] for r in t.rows])

    def test_first_cell_is_empty_because_rank_is_injected_by_javascript(self):
        t = find_table(RANKING_HTML, table_id="t2")
        self.assertEqual([r[0] for r in t.rows], ["", "", ""])

    def test_column_index_is_by_label_not_position(self):
        t = find_table(RANKING_HTML, table_id="t2")
        self.assertEqual(t.column_index("Rent Index"), 3)
        self.assertEqual(t.column_index("rent index"), 3)
        self.assertIsNone(t.column_index("Pollution Index"))

    def test_label_split_across_tags_is_squashed_to_one_line(self):
        """Numbeo splits the utilities label with a <sup> and newlines; a client
        matching the on-screen string must see one line, not three."""
        t = find_table(PRICE_HTML, css_class="data_wide_table")
        label = t.rows[1][0]
        self.assertTrue(label.startswith("Basic Utilities for"))
        self.assertNotIn("\n", label)

    def test_find_table_by_class_and_by_id(self):
        self.assertIsNotNone(find_table(PRICE_HTML, css_class="data_wide_table"))
        self.assertIsNone(find_table(PRICE_HTML, table_id="t2"))

    def test_missing_table_is_none_not_an_exception(self):
        self.assertIsNone(find_table("<p>nothing here</p>", table_id="t2"))

    def test_text_of_skips_script_so_a_marker_check_cannot_match_javascript(self):
        html = "<script>var s='Cannot find city id';</script><p>Prices in X</p>"
        self.assertNotIn("Cannot find city id", text_of(html))
        self.assertIn("Prices in X", text_of(html))

    def test_squash(self):
        self.assertEqual(squash("  a \n b\t c "), "a b c")

    def test_parse_tables_finds_every_table(self):
        self.assertEqual(len(parse_tables(RANKING_HTML + PRICE_HTML)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
