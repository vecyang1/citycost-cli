import json
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import discover
from citycost.errors import SourceUnavailable

# Captured 2026-08-25 from a real tools/call on https://nomads.com/mcp.
SEARCH_RESULT = {
    "content": [{"type": "text", "text": json.dumps({
        "total_matching": 12, "returned": 2,
        "cities": [
            {"slug": "pokhara-nepal", "name": "Pokhara", "country": "Nepal",
             "region": "Asia", "overall_score": 3.63,
             "cost_for_nomad_usd_per_month": 1039},
            {"slug": "nha-trang-vietnam", "name": "Nha Trang",
             "country": "Vietnam", "region": "Asia", "overall_score": 3.58,
             "cost_for_nomad_usd_per_month": 1184},
        ],
        "attribution": "Data from Nomads.com — https://nomads.com",
    })}]
}


class TestArgumentBuilding(unittest.TestCase):
    def test_unset_filters_are_omitted_not_sent_as_null(self):
        """A stated null is a different question from an absent key, and some
        servers answer it, correctly, with nothing."""
        with mock.patch.object(discover, "_call_tool",
                               return_value={"cities": []}) as call:
            discover.search_cities(region="Asia", max_age=0)
        args = call.call_args[0][1]
        self.assertEqual(set(args), {"region", "limit"})
        self.assertNotIn("country", args)

    def test_a_false_or_zero_filter_is_kept_because_it_is_an_answer(self):
        with mock.patch.object(discover, "_call_tool",
                               return_value={"cities": []}) as call:
            discover.search_cities(min_safety=0, max_age=0)
        self.assertIn("min_safety", call.call_args[0][1])

    def test_a_limit_above_the_endpoint_cap_is_refused_not_silently_clamped(self):
        """A result labelled 500 that contains 100 is worse than a refusal."""
        with self.assertRaises(SourceUnavailable) as c:
            discover.search_cities(limit=500)
        self.assertIn("citycost rank", c.exception.remedy)


class TestResultUnwrapping(unittest.TestCase):
    def test_the_text_block_is_parsed_to_an_object(self):
        with mock.patch.object(discover, "_rpc", return_value=SEARCH_RESULT):
            out = discover.search_cities(region="Asia", max_age=0)
        self.assertEqual(out["total_matching"], 12)
        self.assertEqual(len(out["cities"]), 2)

    def test_truncation_is_preserved_so_a_page_cannot_pose_as_the_whole_set(self):
        with mock.patch.object(discover, "_rpc", return_value=SEARCH_RESULT):
            out = discover.search_cities(region="Asia", max_age=0)
        self.assertLess(out["returned"], out["total_matching"])

    def test_is_error_is_raised_rather_than_returned_as_data(self):
        with mock.patch.object(discover, "_rpc", return_value={
                "isError": True,
                "content": [{"type": "text", "text": "no such tool"}]}):
            with self.assertRaises(SourceUnavailable):
                discover.search_cities(max_age=0)

    def test_an_empty_content_block_is_an_error_not_an_empty_result(self):
        """Zero cities and a broken response must not look the same."""
        with mock.patch.object(discover, "_rpc", return_value={"content": []}):
            with self.assertRaises(SourceUnavailable):
                discover.search_cities(max_age=0)

    def test_a_jsonrpc_error_object_is_surfaced(self):
        with mock.patch.object(discover, "http_post_json",
                               return_value={"error": {"code": -32601,
                                                       "message": "no method"}}):
            with self.assertRaises(SourceUnavailable) as c:
                discover.server_info()
        self.assertIn("-32601", c.exception.message)


class TestSlugBridge(unittest.TestCase):
    """nomads uses 'da-nang-vietnam', Numbeo uses 'Da-Nang', and neither
    publishes a mapping — so this is a guess the caller verifies by fetching,
    not a table that would be wrong for every city nobody has hit yet."""

    def test_name_is_preferred_over_the_slug(self):
        self.assertEqual(
            discover.numbeo_slug_for({"name": "Da Nang", "slug": "da-nang-vietnam",
                                      "country": "Vietnam"}),
            "Da-Nang")

    def test_falls_back_to_the_slug_with_the_country_suffix_removed(self):
        self.assertEqual(
            discover.numbeo_slug_for({"slug": "chiang-mai-thailand",
                                      "country": "Thailand"}),
            "Chiang-Mai")

    def test_apostrophes_are_dropped_the_way_numbeo_writes_them(self):
        self.assertEqual(discover.numbeo_slug_for({"name": "Xi'an"}), "Xian")

    def test_an_empty_city_yields_an_empty_guess_rather_than_a_wrong_one(self):
        self.assertEqual(discover.numbeo_slug_for({}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
