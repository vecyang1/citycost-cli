"""`citycost/harvest_report.py` — what the user is told, and the grade the
shell reads.

The repeated-table alarm is computed in `harvest_plan` and is worth nothing
until it reaches somebody. It has failed twice by being true and unsaid: once
by ranging only over the tables a run had just fetched, so a second run over
the same five poisoned files reported clean and exited 0, and once by holding
a finding no printed line carried. So it is graded here from the reader's
end — on the `IDENTICAL` sentence and on `exit_code` — rather than on the
arithmetic that produced it, because the arithmetic was right both times.

And the report's own shape: `executed` and `requests_sent` stated rather than
inferred, a `complete` that is None rather than False when nothing measured it,
and a payload that survives `--json`.
"""

import contextlib
import io
import json
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401  (must be first)
from ._harvest_case import (
    ARCHIVE_IDS, NOW, ONE_TABLE, HarvestCase, _table_page, distinct_pages)
from citycost import harvest, net, rankings


class TestDistinctTables(HarvestCase):
    """The repository's signature defect at harvest scale.

    If `?title=` is ever accepted and silently ignored, harvest stores one
    table under N snapshot ids, reports N successes and N files of the right
    size — and every later `trend` reads the flat line from cache, offline,
    forever. The harvest holds the whole vertical in hand at once, which is
    strictly more evidence than `trend` ever has.
    """

    def _run_with(self, ids, pages):
        self.seed_list("cost-of-living", ids)
        report = self.plan_one()
        buf = io.StringIO()
        with mock.patch.object(rankings, "http_get", side_effect=pages), \
             contextlib.redirect_stderr(buf):
            out = harvest.run(report)
        return out, buf.getvalue()

    def test_one_table_returned_for_every_snapshot_fails_the_run(self):
        ids = ["2020", "2019", "2018", "2017", "2016"]
        out, err = self._run_with(ids, [ONE_TABLE] * 5)
        d = out["distinct"]["cost-of-living"]
        self.assertEqual((d["distinct"], d["of"]), (1, 5))
        self.assertTrue(d["repeated"])
        self.assertEqual(out["repeated_verticals"], ["cost-of-living"])
        self.assertEqual(harvest.exit_code(out), 1)
        self.assertFalse(out["complete"])
        self.assertIn("IDENTICAL", err)
        self.assertIn("rank --snapshot 2014", err)

    def test_tables_that_genuinely_differ_do_not_flag(self):
        """The false-positive direction is the half that rots. Measured, this
        is the normal shape: cost-of-living runs 41 rows in 2009 against 547 in
        2026-mid."""
        ids = ["2020", "2019", "2018", "2017", "2016"]
        out, err = self._run_with(ids, distinct_pages(5))
        d = out["distinct"]["cost-of-living"]
        self.assertEqual((d["distinct"], d["of"]), (5, 5))
        self.assertFalse(d["repeated"])
        self.assertEqual(harvest.exit_code(out), 0)
        self.assertNotIn("IDENTICAL", err)

    def test_two_identical_tables_are_not_enough_evidence(self):
        """Two snapshots can legitimately carry the same table; three is the
        floor `rankings` already uses for the same reason."""
        out, _ = self._run_with(["2020", "2019"], [ONE_TABLE] * 2)
        self.assertFalse(out["distinct"]["cost-of-living"]["repeated"])
        self.assertEqual(harvest.exit_code(out), 0)

    def test_tables_with_no_rows_are_not_evidence_of_sameness(self):
        """`rankings.panel_fingerprint` returns "" for a table with no rows,
        precisely so two empty parses do not read as identical. Honouring that
        matters in the direction that hurts: a vertical whose snapshots
        legitimately parse empty would otherwise trip the alarm and exit 1
        forever, which is the standing-red gate that teaches a reader to ignore
        it."""
        ids = ["2020", "2019", "2018"]
        out, err = self._run_with(ids, [_table_page([])] * 3)
        d = out["distinct"]["cost-of-living"]
        self.assertEqual((d["unfingerprintable"], d["distinct"]), (3, 0))
        self.assertFalse(d["repeated"])
        self.assertEqual(harvest.exit_code(out), 0)
        self.assertIn("parsed with no rows", err)

    def test_a_poisoned_corpus_is_reported_by_every_later_run(self):
        """Measured: run 1 said IDENTICAL and exited 1; run 2 over the same
        five files said nothing and exited 0. The alarm ranged over the tables
        THIS run fetched, and a run that fetches nothing fetches no evidence —
        so the finding was a fact about a run, while the thing it warns about
        is the corpus every later `trend` reads offline, forever."""
        ids = ["2020", "2019", "2018", "2017", "2016"]
        out, _ = self._run_with(ids, [ONE_TABLE] * 5)
        self.assertEqual(out["repeated_verticals"], ["cost-of-living"])

        second = self.plan_one()
        self.assertEqual(second["to_fetch"], 0)
        self.assertEqual(second["repeated_verticals"], ["cost-of-living"])
        self.assertEqual(second["distinct_on_disk"]["cost-of-living"]
                         ["max_repeat"], 5)
        self.assertEqual(harvest.exit_code(second), 1)
        self.assertIn("IDENTICAL",
                      self.stderr_of(harvest.render_plan, second, color=False))

        with self.transport_trap("refetched a corpus that was already cached"):
            with contextlib.redirect_stderr(io.StringIO()):
                out2 = harvest.run(second)
        self.assertEqual((out2["fetched"], out2["requests_sent"]), (0, 0))
        self.assertEqual(out2["repeated_verticals"], ["cost-of-living"])
        self.assertEqual(harvest.exit_code(out2), 1)

    def test_a_healthy_corpus_stays_quiet_on_every_later_run(self):
        """The false-positive direction, which is the half that rots: an alarm
        that fires on a corpus which is merely finished gets muted, and then
        the run where it is real is muted too."""
        ids = ["2020", "2019", "2018", "2017", "2016"]
        self._run_with(ids, distinct_pages(5))
        second = self.plan_one()
        self.assertEqual(second["repeated_verticals"], [])
        self.assertEqual(second["distinct_on_disk"]["cost-of-living"]
                         ["distinct"], 5)
        self.assertEqual(harvest.exit_code(second), 0)
        self.assertNotIn("IDENTICAL",
                         self.stderr_of(harvest.render_plan, second, color=False))

    def test_the_alarm_reads_the_corpus_not_only_this_runs_fetches(self):
        """Three identical tables, one of which was already cached: the plan
        sees one table on disk and the run fetches two, so neither half holds
        three of anything and both report clean. The corpus is what a later
        `trend` reads, so the corpus is what has to be graded."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        self.seed_table("cost-of-living", "2020", ONE_TABLE)
        report = self.plan_one()
        self.assertEqual(report["repeated_verticals"], [])
        with mock.patch.object(rankings, "http_get", return_value=ONE_TABLE):
            with contextlib.redirect_stderr(io.StringIO()):
                out = harvest.run(report)
        self.assertEqual(out["distinct"]["cost-of-living"]["max_repeat"], 2)
        self.assertEqual(out["repeated_verticals"], ["cost-of-living"])
        self.assertEqual(harvest.exit_code(out), 1)
        # Exit 1 with nothing on stderr saying why is an exit 1 nobody acts on,
        # and neither half of this run printed an alarm: the streamed note saw
        # two identical tables, which is not evidence.
        summary = self.stderr_of(harvest.render_report, out)
        self.assertIn("IDENTICAL", summary)
        self.assertIn("already on disk", summary)

    def test_a_repeat_the_run_saw_still_counts_when_nothing_reached_disk(self):
        """The other half of the union, and why the run's own finding is not
        replaced by the disk's. A write that never landed leaves the corpus
        clean by inspection while the requests were spent on one table served
        five times."""
        self.seed_list("cost-of-living", ["2020", "2019", "2018", "2017", "2016"])
        report = self.plan_one()
        with mock.patch.object(rankings, "http_get",
                               side_effect=[ONE_TABLE] * 5), \
             mock.patch.object(net, "cache_write", return_value=False):
            with contextlib.redirect_stderr(io.StringIO()):
                out = harvest.run(report)
        self.assertEqual(out["distinct_on_disk"]["cost-of-living"]["of"], 0)
        self.assertEqual(out["repeated_verticals"], ["cost-of-living"])
        self.assertEqual(harvest.exit_code(out), 1)
        # Named from the reading that actually saw it: the disk has nothing to
        # report, so a summary keyed on the disk would announce 0 of 0.
        self.assertIn("fetched by this run",
                      self.stderr_of(harvest.render_report, out))

    def test_the_fingerprint_reads_content_not_only_row_count(self):
        """Two snapshots can carry the same NUMBER of cities and different
        cities. Keying on the count alone would call those one table."""
        a = _table_page([("Zurich, Switzerland", 123.1)])
        b = _table_page([("Hanoi, Vietnam", 41.2)])
        self.assertNotEqual(rankings.panel_fingerprint(rankings._parse(a, "u")),
                            rankings.panel_fingerprint(rankings._parse(b, "u")))
        self.assertFalse(harvest._distinct_tables(
            [rankings._parse(a, "u"), rankings._parse(b, "u")])["repeated"])



class TestReportShape(HarvestCase):
    def test_a_plan_only_run_is_structurally_distinguishable_from_a_harvest(self):
        """A bare exit 0 is what an agent scripting `citycost harvest &&
        next-step` reads as 'the corpus is here'. A consumer reading `executed`
        gets the right answer without inferring anything."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        self.assertIs(report["executed"], False)
        self.assertEqual(report["requests_sent"], 0)
        self.assertEqual(harvest.exit_code(report), 0)
        self.assertIn("plan only",
                      self.stderr_of(harvest.render_plan, report, color=False))

    def test_the_whole_report_survives_a_json_round_trip(self):
        """`--json` is the agent-facing surface; a payload carrying something
        json cannot express would fail at the consumer, not here."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        self.assertEqual(json.loads(json.dumps(report))["denominator"], 3)

    def test_completeness_is_unknown_rather_than_false_when_it_cannot_be_read(self):
        """Missing inputs to a verdict are not evidence for the negative
        verdict: with no denominator, 'incomplete' is a claim nobody measured."""
        self.assertIsNone(harvest.plan(now=NOW)["complete"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
