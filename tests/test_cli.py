import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import cli, prices, rankings
from .test_htmlparse import RANKING_HTML
from .test_prices import PAGE


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestParser(unittest.TestCase):
    def test_every_subcommand_is_reachable(self):
        p = cli.build_parser()
        for cmd in ("discover", "compare", "rank", "trend", "snapshots",
                    "find", "city", "meetups", "doctor", "cache"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(p.parse_args([cmd] + _min_args(cmd)))

    def test_no_subcommand_is_an_error_not_a_silent_success(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])


def _min_args(cmd):
    return {"compare": ["Prague"], "trend": ["Prague"], "find": ["Vietnam"],
            "city": ["prague-czech-republic"]}.get(cmd, [])


class TestCompareOutput(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(prices, "http_get", return_value=PAGE)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_json_is_pipe_clean_while_notes_go_to_stderr(self):
        code, out, err = run(["compare", "Da-Nang", "--json", "--max-age", "0"])
        self.assertEqual(code, 0)
        data = json.loads(out)          # raises if a note leaked into stdout
        self.assertEqual(data[0]["slug"], "Da-Nang")
        self.assertIn("Da-Nang", err)

    def test_an_unpriced_component_makes_the_budget_null_not_a_short_sum(self):
        _, out, err = run(["compare", "Da-Nang", "--json", "--max-age", "0"])
        rec = json.loads(out)[0]
        self.assertIsNone(rec["monthly_budget_usd"])
        self.assertIn("transport_pass", rec["missing"])
        self.assertIn("NOTE", err)

    def test_csv_keeps_declared_columns_even_when_empty(self):
        _, out, _ = run(["compare", "Da-Nang", "--csv", "--max-age", "0"])
        header = out.splitlines()[0]
        for col in ("budget", "savings", "control_local", "ratio", "missing"):
            self.assertIn(col, header)

    def test_markdown_mode_emits_a_table(self):
        _, out, _ = run(["compare", "Da-Nang", "--md", "--max-age", "0"])
        self.assertIn("|---", out)

    def test_an_unknown_slug_exits_zero_but_says_so_on_stderr(self):
        """A sweep must not abort because one of ten names was mistyped."""
        with mock.patch.object(prices, "http_get",
                               return_value="<html>Cannot find city id</html>"):
            code, out, err = run(["compare", "Xx", "--json", "--max-age", "0"])
        self.assertEqual(code, 0)
        self.assertIn("Cannot find", err + json.loads(out)[0]["error"])


class TestRankOutput(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(rankings, "http_get",
                                       return_value=RANKING_HTML)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_json_carries_the_columns_it_actually_read(self):
        code, out, _ = run(["rank", "--json", "--max-age", "0"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["columns"],
                         ["Cost of Living Index", "Rent Index"])

    def test_top_truncates_and_the_note_says_so(self):
        _, out, err = run(["rank", "--top", "2", "--max-age", "0"])
        self.assertIn("2 of 3 rows", err)

    def test_match_filters(self):
        _, out, _ = run(["rank", "--match", "zurich", "--json", "--max-age", "0"])
        self.assertEqual(json.loads(out)["count"], 1)

    def test_an_unmatched_sort_column_warns_instead_of_silently_not_sorting(self):
        _, _, err = run(["rank", "--sort", "nonsense", "--max-age", "0"])
        self.assertIn("no column matched", err)

    def test_csv_includes_every_index_column(self):
        _, out, _ = run(["rank", "--csv", "--max-age", "0"])
        self.assertIn("Cost of Living Index", out.splitlines()[0])


class TestExitCodes(unittest.TestCase):
    def test_a_source_failure_is_exit_2_with_a_remedy(self):
        from citycost.errors import SourceUnavailable
        with mock.patch.object(rankings, "http_get",
                               side_effect=SourceUnavailable("down", "wait")):
            code, _, err = run(["rank", "--max-age", "0"])
        self.assertEqual(code, 2)
        self.assertIn("wait", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
