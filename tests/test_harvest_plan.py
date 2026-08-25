"""`citycost/harvest_plan.py` — the number the user consents to.

Everything on that side has to be answerable with the network unplugged, so
these tests grade the plan against the FILES: the denominator and what makes it
unknown, the partition and the states a probe can come back with, the pacing
floor, and the one owner of the freshness rule. The subject is `plan()` and the
target list under it. What happens when a plan is spent is `test_harvest.py`;
how any of it is worded is `test_harvest_report.py`.

The first test here is the load-bearing one, and it is why this side is its own
file: a plan that quietly went and found out would be spending the consent it
was about to ask for. It is asserted with traps on both transport names and on
both fetching functions, not with a call count.
"""

import contextlib
import io
import json
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401  (must be first)
from ._harvest_case import ARCHIVE_IDS, NOW, HarvestCase, distinct_pages
from citycost import harvest, net, rankings
from citycost.errors import CitycostError


class TestPlanIsReadOnly(HarvestCase):
    def test_plan_reaches_the_network_by_no_path_at_all(self):
        """The plan states the cost of a decision the user has not made yet.
        A plan that quietly went and found out would be spending the consent it
        was about to ask for."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        traps = {
            "fetch": AssertionError("plan() fetched a table"),
            "snapshots": AssertionError("plan() fetched a snapshot list"),
        }
        # autospec, so the stubs keep the real signatures the planner reads its
        # freshness rule from — a bare MagicMock would fail this test for a
        # reason that has nothing to do with the network.
        before = net.reads()["live"]
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                self.transport_trap("plan() reached the transport"))
            for name, boom in traps.items():
                stack.enter_context(mock.patch.object(
                    rankings, name, autospec=True, side_effect=boom))
            report = harvest.plan(now=NOW)
        self.assertEqual(report["requests_sent"], 0)
        self.assertFalse(report["executed"])
        self.assertEqual(net.reads()["live"], before)

    def test_one_row_per_vertical_always_and_the_count_is_printed(self):
        """A vertical whose list failed must not be dropped from the table: a
        smaller total looks complete, and no row says anything is missing."""
        self.seed_list("crime", ARCHIVE_IDS)
        report = harvest.plan(now=NOW)
        self.assertEqual(report["rows"], len(rankings.VERTICALS))
        self.assertEqual(len(report["verticals"]), len(rankings.VERTICALS))
        err = self.stderr_of(harvest.render_plan, report, color=False)
        self.assertIn(f"{len(rankings.VERTICALS)} rows", err)

    def test_a_vertical_with_no_cached_list_is_unknown_not_zero(self):
        report = harvest.plan(now=NOW)
        self.assertIsNone(report["denominator"])
        self.assertEqual(sorted(report["unknown_verticals"]),
                         sorted(rankings.VERTICALS))
        for row in report["verticals"]:
            self.assertIsNone(row["denominator"])
            self.assertEqual(row["list_request_cost"], 1)
        # The plan is not free either: with no cached lists the real ceiling is
        # 7 requests more than the corpus size.
        self.assertEqual(report["list_requests"], len(rankings.VERTICALS))

    def test_a_first_run_still_states_a_number_to_consent_to(self):
        """The consent number was missing for exactly the person who has never
        run this. `to_fetch` is None whenever any denominator is unknown, and
        `list_requests` is non-zero only in that same case, so
        `to_fetch + list_requests` was None every time it mattered — and
        `render_plan` printed no "to fetch:" line at all. A floor stated as a
        floor is a number a user can consent to; `None` is not.
        """
        report = harvest.plan(now=NOW)
        self.assertIsNone(report["to_fetch"])
        self.assertEqual(report["requests_planned"], len(rankings.VERTICALS))
        self.assertEqual(report["requests_planned_state"], "floor")
        self.assertEqual(report["to_fetch_known"], 0)
        self.assertIsNotNone(report["pacing_floor_s"])
        err = self.stderr_of(harvest.render_plan, report, color=False)
        self.assertIn(f"to fetch: at least {len(rankings.VERTICALS)}", err)
        self.assertIn("to learn the unknown lists", err)

    def test_a_known_denominator_states_the_number_as_exact(self):
        """The other direction, which is the half that rots: a plan that hedged
        every number would teach the user to ignore the hedge on the run where
        it is real."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        self.assertEqual(report["requests_planned"], 3)
        self.assertEqual(report["requests_planned_state"], "exact")
        self.assertEqual(report["to_fetch_known"], report["to_fetch"])
        err = self.stderr_of(harvest.render_plan, report, color=False)
        self.assertIn("to fetch: 3 request(s)", err)
        self.assertNotIn("at least", err)
        self.assertNotIn("to learn the unknown lists", err)

    def test_a_plan_whose_request_count_does_not_add_up_refuses_to_print(self):
        """`requests_planned` is the one number the user actually consents to,
        and it is a sum of two others. A sum nobody checks is a number that
        agrees with its parts on the day it was written."""
        report = harvest.plan(now=NOW)
        broken = json.loads(json.dumps(report))
        broken["list_requests"] -= 1
        with self.assertRaises(CitycostError):
            harvest.verify_partition(broken)
        missing = json.loads(json.dumps(report))
        del missing["to_fetch_known"]
        with self.assertRaises(CitycostError):
            harvest.verify_partition(missing)

    def test_an_unknown_denominator_prints_the_word_not_a_blank_cell(self):
        buf, sink = io.StringIO(), io.StringIO()
        report = harvest.plan(now=NOW)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(sink):
            harvest.render_plan(report, color=False)
        self.assertIn("unknown", buf.getvalue())

    def test_the_denominator_follows_the_cached_list_rather_than_a_constant(self):
        """Numbeo publishes 2027 one day. A plan carrying its own number would
        ask for consent to a total that is no longer the total."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        self.assertEqual(self.plan_one()["denominator"], 3)
        self.seed_list("cost-of-living", ARCHIVE_IDS + ["2017"])
        self.assertEqual(self.plan_one()["denominator"], 4)

    def test_an_empty_cached_list_is_unknown_not_a_complete_zero(self):
        """`0 of 0` reads as 'this vertical is complete', which is a
        completeness claim over an empty denominator — the check that cannot
        return false."""
        with mock.patch.object(rankings, "http_get",
                               return_value="<html><select></select></html>"):
            rankings.snapshots("cost-of-living", max_age=0)
        report = self.plan_one()
        self.assertIsNone(report["denominator"])
        self.assertEqual(report["unknown_verticals"], ["cost-of-living"])

    def test_the_partition_sums_to_the_denominator(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        self.seed_table("cost-of-living", "2020")
        report = self.plan_one()
        row = report["verticals"][0]
        self.assertEqual(sum(row["states"].values()), row["denominator"])
        self.assertEqual(row["already_cached"] + row["to_fetch"],
                         row["denominator"])
        self.assertEqual(row["states"]["fresh"], 1)
        self.assertEqual(row["states"]["absent"], 2)

    def test_a_report_whose_columns_do_not_add_refuses_to_print(self):
        """A partition that does not sum has quietly dropped a category, and a
        dropped category is the difference between 52 requests and 192."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        broken = json.loads(json.dumps(report))
        broken["verticals"][0]["states"]["absent"] -= 1
        with self.assertRaises(CitycostError):
            harvest.verify_partition(broken)
        with self.assertRaises(CitycostError):
            self.stderr_of(harvest.render_plan, broken, color=False)

    def test_a_schema_bump_is_its_own_column_not_cached_and_not_absent(self):
        """The repo has already bumped `rankings.SCHEMA` once. 'You have 3
        cached' and 'you have 3 files, all under the previous schema' cost 0
        and 3 requests, and a boolean makes them the same sentence."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        # Written by an older version of this client, which is how the event
        # actually arrives: the list stays readable and the tables do not.
        with mock.patch.object(rankings, "SCHEMA", "numbeo-rankings-1"):
            for snap in ARCHIVE_IDS:
                self.seed_table("cost-of-living", snap)
        report = self.plan_one()
        row = report["verticals"][0]
        self.assertEqual(row["states"]["schema_mismatch"], 3)
        self.assertEqual(row["states"]["fresh"], 0)
        self.assertEqual(row["to_fetch"], 3)

    def test_current_is_excluded_from_the_subject_set_and_said_so_out_loud(self):
        """`rankings_current.jsp` is a different object from `?title=<id>` —
        558 rows against 547 — and it is the one URL in the family that
        legitimately moves. Silently omitting it leaves a user who harvested
        'everything' believing a stale `rank` is covered."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        targets = report["verticals"][0]["targets"]
        self.assertEqual([t["snapshot"] for t in targets], ARCHIVE_IDS)
        # Not `assertNotIn("rankings_current.jsp", t["url"])` over the targets:
        # `current` is filtered out by `rankings.snapshots` and again by
        # `_snapshot_ids` before `_targets_for` can see it, so that predicate
        # has no input that makes it false. What can actually change is the
        # pattern both filters share — widen it to accept a non-numeric
        # selection and `current` walks in through both.
        self.assertIsNone(rankings.SNAPSHOT_ID.fullmatch("current"))
        err = self.stderr_of(harvest.render_plan, report, color=False)
        self.assertIn("NOT harvested: current", err)

    def test_a_cached_list_entry_that_is_not_a_snapshot_id_is_narrowed_out_loud(self):
        """The list on disk outlives the client that wrote it, which is the one
        way an unrecognised entry arrives: `rankings.snapshots` applies this
        exact pattern before caching, so no CURRENT writer can produce one.
        A file left by a client whose pattern was wider can, and a silent
        narrowing there is a denominator that merely looks smaller — the plan
        then reports a complete corpus over snapshots it never harvested and
        nobody ever missed.

        Seeded through the real writer FIRST, so this stays a round trip: if
        `_list_key` ever drifted from the key `rankings` writes, the correct
        list would answer the plan and this goes red rather than passing
        against a file only the test can find.
        """
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        net.cache_write(
            harvest._list_key("cost-of-living"), rankings.SCHEMA,
            ["2020", "current", "2019", "rankings_current.jsp", "2018"],
            keep=True)
        report = self.plan_one()
        row = report["verticals"][0]
        self.assertEqual(row["denominator"], 3)
        self.assertEqual(row["unrecognised_ids"], 2)
        self.assertEqual([t["snapshot"] for t in row["targets"]], ARCHIVE_IDS)
        err = self.stderr_of(harvest.render_plan, report, color=False)
        self.assertIn("2 cached list entr", err)
        self.assertIn("narrowed out", err)

    def test_a_clean_cached_list_says_nothing_about_narrowing(self):
        """The direction that rots: a note printed on every healthy plan is a
        note nobody reads on the plan where it is real."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        row = self.plan_one()["verticals"][0]
        self.assertNotIn("unrecognised_ids", row)
        self.assertNotIn(
            "narrowed out",
            self.stderr_of(harvest.render_plan, self.plan_one(), color=False))

    def test_the_current_period_is_reported_as_unpinnable(self):
        """`is_archival` refuses 2026 and 2026-mid on this date, so two per
        vertical are harvested and then expire on the ordinary clock. A plan
        that did not say so would be promising a corpus it is not building."""
        self.seed_list("cost-of-living", ["2026-mid", "2026", "2025"])
        row = self.plan_one()["verticals"][0]
        self.assertEqual(row["pinned"], 1)
        self.assertEqual(sorted(row["unpinned_ids"]), ["2026", "2026-mid"])

    def test_max_age_zero_means_nothing_on_disk_may_answer(self):
        """`--max-age 0` is a distinct state meaning *read live*, and it is
        honoured for a pinned entry exactly as for any other — the only
        non-destructive way to replace a poisoned pin."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        for snap in ARCHIVE_IDS:
            self.seed_table("cost-of-living", snap)
        self.assertEqual(self.plan_one()["to_fetch"], 0)
        self.assertEqual(self.plan_one(max_age=0)["to_fetch"], 3)

    def test_the_pacing_floor_is_read_off_net_rather_than_restated(self):
        """The pacing floor is half of what the user consents to: 192 x 1.1s is
        three and a half minutes of sleep before any response time. A literal
        here would stop tracking the throttle the moment somebody tuned it."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        base = self.plan_one()["pacing_floor_s"]
        with mock.patch.dict(net.MIN_INTERVAL, {"www.numbeo.com": 5.0}):
            tuned = self.plan_one()
        self.assertEqual(base, round(3 * 1.1, 1))
        self.assertEqual(tuned["pacing_floor_s"], 15.0)
        self.assertEqual(tuned["min_interval_s"], 5.0)

    def test_a_repeated_index_is_one_vertical_not_two(self):
        """`--index` is `action="append"` with a `choices=` list, which accepts
        a repeat happily. Measured: `--index crime --index crime` over a
        3-snapshot vertical planned a denominator of 6, 6 to fetch and a 6.6s
        pacing floor, then sent 3 real requests while reporting 6 planned. The
        consent number and the spent number came apart in the direction that
        makes a sweep look larger on paper than the source ever saw — which is
        the direction that gets believed."""
        self.seed_list("crime", ARCHIVE_IDS)
        report = harvest.plan(["crime", "crime"], now=NOW)
        self.assertEqual(report["rows"], 1)
        self.assertEqual([r["vertical"] for r in report["verticals"]], ["crime"])
        self.assertEqual(report["denominator"], 3)
        self.assertEqual(report["to_fetch"], 3)
        self.assertEqual(report["requests_planned"], 3)
        self.assertEqual(report["pacing_floor_s"], round(3 * 1.1, 1))
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)) as get:
            with contextlib.redirect_stderr(io.StringIO()):
                out = harvest.run(report)
        self.assertEqual((get.call_count, out["requests_sent"]), (3, 3))

    def test_an_unknown_vertical_is_refused_by_the_planner_too(self):
        """argparse `choices` guards the CLI, but `plan()` is a public entry
        point and this check can therefore actually fire."""
        with self.assertRaises(CitycostError) as ctx:
            harvest.plan(["nomads.com"], now=NOW)
        self.assertIn("cost-of-living", ctx.exception.remedy)


class TestTtlHasOneOwner(HarvestCase):
    def test_the_freshness_rule_comes_from_the_function_that_will_apply_it(self):
        self.assertEqual(
            self.plan_one()["max_age"],
            harvest._signature_default(rankings.fetch, "max_age"))

    def test_a_signature_that_stops_carrying_a_default_fails_loudly(self):
        """Substituting a shorter TTL silently would make the plan over-report
        by one request per vertical, which is a wrong consent number."""
        def no_default(vertical="cost-of-living", *, max_age="soon"):
            raise AssertionError("not called")
        with self.assertRaises(CitycostError):
            harvest._signature_default(no_default, "max_age")



if __name__ == "__main__":
    unittest.main(verbosity=2)
