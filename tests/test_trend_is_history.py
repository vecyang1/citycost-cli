"""`trend` must be able to tell history from one table fetched N times.

The failure this guards is the only one in the ranking family with no error
path to fall into. Every other way `?title=` can go wrong is loud: an unknown
vertical raises, a missing table raises, an empty snapshot list raises. But a
snapshot parameter that is *accepted and ignored* answers 200 with the current
table every time — complete series, `found: True` on every point, a flat line,
and a reader who concludes the city is stable.

Measured 2026-08-25 through the tool's own transport: all seven verticals
honour `?title=` today (oldest vs newest snapshot differed for every one). So
these tests are an alarm for drift, and the healthy-input direction below is
the half that actually rots — a predicate that fires on real data would be
muted within a week.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import cli, rankings


def _point(snapshot, *, of, value, found=True):
    return {"snapshot": snapshot, "found": found, "place": "Da Nang, Vietnam",
            "rank": 7, "of": of, "metrics": {"Cost of Living Index": value}}


class OneTableRepeatedIsNotHistory(unittest.TestCase):
    """The predicate, both directions. Positive first, because a guard that
    cannot fire and a guard that always fires read identically from outside."""

    def test_three_identical_snapshots_are_flagged(self):
        series = [_point(s, of=547, value=38.2)
                  for s in ("2026-mid", "2025", "2024")]
        self.assertTrue(rankings._is_one_table_repeated(series))

    def test_real_history_is_not_flagged(self):
        # Values move and, decisively, so does the table size.
        series = [_point("2026-mid", of=547, value=38.2),
                  _point("2025", of=561, value=36.9),
                  _point("2024", of=578, value=35.1)]
        self.assertFalse(rankings._is_one_table_repeated(series))

    def test_a_genuinely_unchanging_city_is_not_flagged(self):
        """The false positive worth designing against.

        An index really can print the same figure twice. What it cannot do is
        print it inside three tables of identical size — Numbeo's snapshots
        are not monotonic (2022 carried 578 cities, 2026-mid carries 547), so
        `of` is the field that separates a stable city from a repeated table.
        """
        series = [_point("2026-mid", of=547, value=38.2),
                  _point("2025", of=561, value=38.2),
                  _point("2024", of=578, value=38.2)]
        self.assertFalse(rankings._is_one_table_repeated(series))

    def test_same_table_size_alone_is_not_enough(self):
        """Two snapshots can legitimately carry the same number of cities."""
        series = [_point("2026-mid", of=547, value=38.2),
                  _point("2025", of=547, value=36.9),
                  _point("2024", of=547, value=35.1)]
        self.assertFalse(rankings._is_one_table_repeated(series))

    def test_two_points_are_not_enough_evidence(self):
        """Thin data must not be read as drift.

        A city ranked in only two snapshots is common and says nothing about
        the transport. Firing here would put the warning on ordinary output,
        and a warning that fires on healthy input gets muted.
        """
        series = [_point("2026-mid", of=547, value=38.2),
                  _point("2025", of=547, value=38.2),
                  {"snapshot": "2024", "found": False, "note": "not ranked"}]
        self.assertFalse(rankings._is_one_table_repeated(series))

    def test_absences_are_ignored_not_counted_as_matching(self):
        """`found: False` points carry no metrics; treating their empty dict
        as a fingerprint would make any three-absence run look degenerate."""
        series = [{"snapshot": s, "found": False, "note": "not ranked"}
                  for s in ("2026-mid", "2025", "2024", "2023")]
        self.assertFalse(rankings._is_one_table_repeated(series))


class TrendRefusesToPresentARepeatedTableAsHistory(unittest.TestCase):
    """The wiring: the predicate has to reach the exit code and the operator.

    Asserted at the CLI rather than on the dict, because the property under
    test is *whether the reader is told* — a flag nobody renders is the same
    as no flag.
    """

    FLAT = {
        "city": "Da-Nang", "anchor": "Da Nang, Vietnam", "ambiguous": [],
        "vertical": "cost-of-living", "column": "Cost of Living Index",
        "columns": ["Cost of Living Index"],
        "series": [_point(s, of=547, value=38.2)
                   for s in ("2026-mid", "2025", "2024")],
        "identical_across_snapshots": True, "found_in": 3, "requested": 3,
    }

    def _run(self, payload, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rankings, "trend", return_value=payload):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_text_output_exits_nonzero_and_says_why(self):
        code, _, err = self._run(self.FLAT, ["trend", "Da-Nang"])
        self.assertEqual(code, 1)
        self.assertIn("IDENTICAL", err)
        self.assertIn("title=", err)

    def test_json_output_exits_nonzero_but_stays_pipe_clean(self):
        """The warning is stderr and the exit code; stdout stays parseable.

        A consumer that pipes `--json` must still get JSON on a degenerate
        run — the alternative is that the one case worth catching is the case
        their parser crashes on, and a crash gets reported as a bug in them.
        """
        code, out, err = self._run(self.FLAT, ["trend", "Da-Nang", "--json"])
        self.assertEqual(code, 1)
        parsed = json.loads(out)
        self.assertTrue(parsed["identical_across_snapshots"])
        self.assertIn("IDENTICAL", err)

    def test_healthy_history_exits_zero_and_warns_nothing(self):
        healthy = dict(self.FLAT, identical_across_snapshots=False,
                       series=[_point("2026-mid", of=547, value=38.2),
                               _point("2025", of=561, value=36.9),
                               _point("2024", of=578, value=35.1)])
        code, _, err = self._run(healthy, ["trend", "Da-Nang"])
        self.assertEqual(code, 0)
        self.assertNotIn("IDENTICAL", err)


class TheFlagIsActuallyComputedByTrend(unittest.TestCase):
    """The two classes above test the predicate and the rendering separately,
    and a mock sits between them. This one closes the seam: `trend()` itself
    must put the key in its return value, or both of them pass over a field
    production never sets.
    """

    def test_trend_returns_the_key(self):
        table = {"url": "u", "columns": ["Cost of Living Index"],
                 "rows": [{"place": "Da Nang, Vietnam", "city": "Da Nang",
                           "subdivision": "", "country": "Vietnam", "rank": 7,
                           "metrics": {"Cost of Living Index": 38.2}}]}
        with mock.patch.object(rankings, "snapshots",
                               return_value=["2026-mid", "2025", "2024"]), \
             mock.patch.object(rankings, "fetch", return_value=table):
            data = rankings.trend("Da-Nang", limit=3)
        self.assertIn("identical_across_snapshots", data)
        # Three snapshots, one table object: the degenerate case by
        # construction, which is what makes this a live wire rather than a
        # key-presence check.
        self.assertTrue(data["identical_across_snapshots"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
