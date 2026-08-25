"""`citycost harvest` — the plan must cost what the run spends, and the run
must stop when the source says no.

Every test here is offline. The transport is patched at `rankings.http_get`,
which is the seam the whole module is built around: harvest calls
`rankings.fetch` and nothing lower, so a test that has to reach past that seam
is a test reporting that the design broke.

Where the property under test is "this must NOT happen" — no request before a
refusal, no fetcher call, no request from `plan()` at all — the stub is an
`AssertionError` side effect rather than a plausible return value. A stub that
returns something hides both ordering and whether it was called; a trap fails
at the moment the wrong thing occurs.
"""

import contextlib
import datetime
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401  (must be first)
from citycost import fallback, harvest, net, rankings
from citycost.errors import CitycostError, LayoutChanged, SourceUnavailable

#: A fixed clock, so which ids may be PINNED is a property of the test rather
#: than of the day it runs. On this date `rankings.is_archival` pins 2025-mid
#: and older and refuses 2026 / 2026-mid — the current half-year, which nothing
#: has measured to be finished.
NOW = datetime.datetime(2026, 8, 25, tzinfo=datetime.timezone.utc).timestamp()

ARCHIVE_IDS = ["2020", "2019", "2018"]


def _snapshot_page(ids) -> str:
    opts = "".join(f'<option value="{i}">{i}</option>' for i in ids)
    return ("<html><body><select name='title'>" + opts +
            "</select><table id='t2'></table></body></html>")


def _table_page(places) -> str:
    """A ranking table whose CONTENT varies with `places`, so two snapshots can
    be told apart by fingerprint rather than only by row count — two snapshots
    can legitimately carry the same number of cities."""
    rows = "".join(
        f"<tr><td></td><td>{p}</td><td>{v:.1f}</td><td>{v / 2:.1f}</td></tr>"
        for p, v in places)
    return (
        "<table id='t2'><thead><tr><th><div>Rank</div></th>"
        "<th><div>City</div></th><th><div>Cost of Living Index</div></th>"
        "<th><div>Rent Index</div></th></tr></thead><tbody>"
        + rows + "</tbody></table>")


ONE_TABLE = _table_page([("Zurich, Switzerland", 123.1),
                         ("Da Nang, Vietnam", 32.4)])

#: What Numbeo serves for a selection it has no table for. `rankings._parse`
#: turns this into a SourceUnavailable that is a *permanent answer about that
#: URL*, not a failure.
NO_TABLE_PAGE = "<html><body><p>Cannot find that selection</p></body></html>"


def distinct_pages(n: int) -> list:
    """n tables that genuinely differ — the normal shape. Measured,
    cost-of-living runs 41 rows in 2009 against 547 in 2026-mid."""
    return [_table_page([(f"City {i}, Country", 100.0 + i)] * (i + 1))
            for i in range(1, n + 1)]


class HarvestCase(unittest.TestCase):
    """Each test gets its own cache directory.

    `_sandbox` pins one for the whole process, which is what keeps the suite
    off the developer's real corpus; this narrows it again per test so a
    resume/rerun test cannot be answered by a neighbour's files.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="citycost-harvest-", dir=_sandbox.SANDBOX)
        self._env = mock.patch.dict(os.environ, {"CITYCOST_CACHE_DIR": self._dir})
        self._env.start()
        self.addCleanup(self._env.stop)
        fallback.reset()
        self.addCleanup(fallback.reset)
        net.reset_reads()
        self.addCleanup(net.reset_reads)

    # -- seeding, always through the module that owns the cache key ---------
    #
    # Nothing here writes a cache file directly. `harvest._target_key` and
    # `harvest._list_key` restate a fact `rankings` owns, and that is the one
    # place this module can be silently wrong: a changed key prefix would make
    # the plan report an empty corpus while 192 files sit on disk, costing 192
    # requests against a source that bans by address. Seeding through the real
    # writer makes every test in this file a round trip, so the drift goes red
    # here rather than on somebody's rate limit.

    def seed_list(self, vertical, ids):
        with mock.patch.object(rankings, "http_get",
                               return_value=_snapshot_page(ids)):
            got = rankings.snapshots(vertical, max_age=0)
        self.assertEqual(got, list(ids))

    def seed_table(self, vertical, snapshot, html=ONE_TABLE):
        with mock.patch.object(rankings, "http_get", return_value=html):
            rankings.fetch(vertical, snapshot=snapshot, max_age=0)

    def stderr_of(self, fn, *a, **kw):
        buf, sink = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(sink):
            fn(*a, **kw)
        return buf.getvalue()

    def plan_one(self, vertical="cost-of-living", **kw):
        kw.setdefault("now", NOW)
        return harvest.plan([vertical], **kw)


class TestPlanIsReadOnly(HarvestCase):
    def test_plan_reaches_the_network_by_no_path_at_all(self):
        """The plan states the cost of a decision the user has not made yet.
        A plan that quietly went and found out would be spending the consent it
        was about to ask for."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        traps = {
            "http_get": AssertionError("plan() reached the transport"),
            "fetch": AssertionError("plan() fetched a table"),
            "snapshots": AssertionError("plan() fetched a snapshot list"),
        }
        # autospec, so the stubs keep the real signatures the planner reads its
        # freshness rule from — a bare MagicMock would fail this test for a
        # reason that has nothing to do with the network.
        before = net.reads()["live"]
        with contextlib.ExitStack() as stack:
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
        for t in targets:
            self.assertNotIn("rankings_current.jsp", t["url"])
        err = self.stderr_of(harvest.render_plan, report, color=False)
        self.assertIn("NOT harvested: current", err)

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


class TestRun(HarvestCase):
    def _seeded(self, ids=ARCHIVE_IDS, vertical="cost-of-living"):
        self.seed_list(vertical, ids)
        return self.plan_one(vertical)

    def test_the_run_sends_exactly_what_the_plan_costed(self):
        report = self._seeded()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)) as get:
            with contextlib.redirect_stderr(io.StringIO()):
                out = harvest.run(report)
        self.assertEqual(report["to_fetch"], 3)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(out["requests_sent"], 3)
        self.assertEqual((out["ok"], out["failed"], out["empty"]), (3, 0, 0))
        self.assertTrue(out["executed"])
        self.assertTrue(out["complete"])
        self.assertEqual(harvest.exit_code(out), 0)

    def test_ok_failed_and_empty_sum_to_attempted(self):
        report = self._seeded(["2020", "2019", "2018", "2017"])
        a, b = distinct_pages(2)
        pages = [a, NO_TABLE_PAGE,
                 SourceUnavailable("nope", "", status=404), b]
        with mock.patch.object(rankings, "http_get", side_effect=pages):
            out = self.stderr_run(report)
        self.assertEqual(out["ok"] + out["failed"] + out["empty"],
                         out["attempted"])
        self.assertEqual((out["ok"], out["failed"], out["empty"]), (2, 1, 1))

    def stderr_run(self, report):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = harvest.run(report)
        self._stderr = buf.getvalue()
        return out

    def test_a_rerun_over_a_complete_corpus_sends_nothing_and_states_the_age(self):
        """A rerun that printed a success line would read as a fresh harvest,
        so the user believes the data is minutes old when it is three weeks
        old."""
        report = self._seeded()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)):
            with contextlib.redirect_stderr(io.StringIO()):
                harvest.run(report)
        second = self.plan_one()
        self.assertEqual(second["to_fetch"], 0)
        self.assertEqual(second["already_cached"], 3)
        err = self.stderr_of(harvest.render_plan, second, color=False)
        self.assertIn("already cached: 3", err)
        with mock.patch.object(
                rankings, "http_get",
                side_effect=AssertionError("a cached corpus was refetched")):
            out = self.stderr_run(second)
        self.assertEqual((out["fetched"], out["requests_sent"]), (0, 0))
        self.assertEqual(out["already_cached"], 3)
        self.assertTrue(out["executed"])
        self.assertTrue(out["complete"])

    def test_a_transient_failure_is_named_when_it_happens_and_again_at_the_end(self):
        """Batching the failure list to the end loses it entirely when the run
        is killed at request 150 — and a five-minute foreground command over a
        ban-prone source will be killed."""
        report = self._seeded()
        a, b = distinct_pages(2)
        pages = [a, SourceUnavailable("gone", "check it", status=404), b]
        with mock.patch.object(rankings, "http_get", side_effect=pages):
            out = self.stderr_run(report)
        self.assertEqual(out["failed"], 1)
        self.assertFalse(out["complete"])
        self.assertEqual(harvest.exit_code(out), 1)
        self.assertIn("2019", self._stderr)
        fail = out["failures"][0]
        self.assertEqual((fail["vertical"], fail["snapshot"]),
                         ("cost-of-living", "2019"))
        self.assertIn("rankings.jsp?title=2019", fail["url"])
        # 404 is an HTTP status and 4 is a fetcher exit code; one field for
        # both makes the first reader to compare them get a plausible answer.
        self.assertEqual((fail["status"], fail["exit_code"]), (404, None))
        summary = self.stderr_of(harvest.render_report, out)
        self.assertIn("2019", summary)
        self.assertIn("rerun costs 1", summary)

    def test_an_absent_upstream_table_is_not_a_failure_and_still_exits_zero(self):
        """Counting it as a failure means every rerun retries the same dead ids
        forever and the command can never reach exit 0 — within a week exit 1
        reads as normal, and a real failure is indistinguishable from it."""
        report = self._seeded()
        a, b = distinct_pages(2)
        pages = [a, NO_TABLE_PAGE, b]
        with mock.patch.object(rankings, "http_get", side_effect=pages):
            out = self.stderr_run(report)
        self.assertEqual((out["empty"], out["failed"]), (1, 0))
        self.assertEqual(harvest.exit_code(out), 0)
        again = self.plan_one()
        self.assertEqual(again["to_fetch"], 1)      # asked again, not retired
        with mock.patch.object(rankings, "http_get", return_value=NO_TABLE_PAGE):
            out2 = self.stderr_run(again)
        self.assertEqual(harvest.exit_code(out2), 0)

    def test_the_empty_marker_still_matches_what_rankings_actually_raises(self):
        """This module keys on prose, which `errors.py` exists to discourage —
        there is no status to key on, because a dead socket and 'no such
        selection' both arrive as SourceUnavailable(status=None). So the
        coupling is pinned to the real raise site: reword the sentence upstream
        and this goes red, instead of every dead snapshot silently becoming a
        failure that no rerun can clear."""
        with self.assertRaises(SourceUnavailable) as ctx:
            rankings._parse(NO_TABLE_PAGE, "u")
        self.assertTrue(harvest._is_empty(ctx.exception))
        self.assertFalse(harvest._is_empty(
            SourceUnavailable("u unreachable: timed out", "check network")))

    def test_live_reads_are_graded_against_the_items_the_plan_costed(self):
        """The transport is the one place that knows how many live reads
        happened. A run that made more of them than it attempted items has a
        second path to the network the plan never costed."""
        report = self._seeded()

        def greedy(*a, **kw):
            net._READS["live"] += 2
            return rankings._parse(ONE_TABLE, "u")

        with mock.patch.object(rankings, "fetch", side_effect=greedy):
            out = self.stderr_run(report)
        self.assertGreater(out["live_reads"], out["attempted"])
        self.assertIn("harvest sent requests its plan did not cost",
                      self._stderr)


class TestRefusals(HarvestCase):
    def test_a_blocked_status_aborts_the_whole_run_at_that_item(self):
        """Numbeo's block is address-level and seven days and does not slide,
        so continuing would send up to 191 more requests into a wall and spend
        the address on behalf of the next user. Asserted on the CALL COUNT: the
        message is prose, the count is the property."""
        self.seed_list("cost-of-living", ["2020", "2019", "2018", "2017"])
        report = self.plan_one()
        a, b = distinct_pages(2)
        pages = [a, b,
                 SourceUnavailable("refused (HTTP 429)", "use another network",
                                   status=429),
                 AssertionError("harvest kept going after a 429")]
        with mock.patch.object(rankings, "http_get", side_effect=pages) as get:
            with self.assertRaises(harvest.HarvestAborted) as ctx:
                with contextlib.redirect_stderr(io.StringIO()):
                    harvest.run(report)
        self.assertEqual(get.call_count, 3)
        partial = ctx.exception.report
        self.assertEqual(partial["aborted"]["reason"], "blocked")
        self.assertEqual((partial["aborted"]["at_item"], partial["aborted"]["of"]),
                         (3, 4))
        self.assertFalse(partial["complete"])
        self.assertEqual(harvest.exit_code(partial), 2)
        self.assertIn("2 item(s) are cached", ctx.exception.message)

    def test_a_layout_change_aborts_rather_than_repeating_one_bug_180_times(self):
        """The fix is in this repo, so the remaining requests cannot succeed:
        pure waste against a rate-limited source, and a failure list that says
        one thing 180 times buries the one thing."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        pages = [distinct_pages(1)[0],
                 "<html><table id='other'></table></html>",
                 AssertionError("harvest kept going after a layout change")]
        with mock.patch.object(rankings, "http_get", side_effect=pages) as get:
            with self.assertRaises(harvest.HarvestAborted) as ctx:
                with contextlib.redirect_stderr(io.StringIO()):
                    harvest.run(report)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(ctx.exception.report["aborted"]["reason"],
                         "layout_changed")
        self.assertEqual(ctx.exception.report["failed"], 0)

    def test_a_mid_run_reroute_aborts_the_sweep(self):
        """A sweep every request of which is rerouted is a sweep the origin has
        already refused, and 192 stderr lines is not a signal anybody reads.
        Observed through `fallback.events()` — the log `net._via_fetcher`
        appends to — so this fires in `auto` too, where the reroute is the thing
        that happens rather than the thing that was asked for."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        calls = {"n": 0}

        def maybe_rerouted(url, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                fallback.record(url, 429, "GET")
            return ONE_TABLE

        with mock.patch.object(rankings, "http_get", side_effect=maybe_rerouted):
            with self.assertRaises(harvest.HarvestAborted) as ctx:
                with contextlib.redirect_stderr(io.StringIO()):
                    harvest.run(report)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(ctx.exception.report["aborted"]["reason"], "rerouted")
        # The item that was rerouted still counts as fetched and cached: it is
        # on disk, and a rerun that refetched it would spend the egress twice.
        self.assertEqual(ctx.exception.report["ok"], 2)

    def test_fetch_mode_always_is_refused_before_request_one(self):
        """Refused up front rather than mid-run, and asserted with traps rather
        than with a call count: the property is that NOTHING is called."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        with mock.patch.dict(os.environ, {"CITYCOST_FETCH_MODE": "always"}), \
             mock.patch.object(rankings, "http_get",
                               side_effect=AssertionError("sent a request")), \
             mock.patch.object(fallback, "run",
                               side_effect=AssertionError("used the fetcher")):
            with self.assertRaises(CitycostError) as ctx:
                harvest.run(report)
        self.assertIn("repair, not a licence", ctx.exception.remedy)
        self.assertNotIsInstance(ctx.exception, harvest.HarvestAborted)

    def test_execute_refuses_while_any_denominator_is_unknown(self):
        """Harvesting the verticals that resolved and reporting a total is a
        partial result passing as complete, and the total is the number the
        user consented to."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = harvest.plan(now=NOW)            # six lists still uncached
        with mock.patch.object(rankings, "http_get",
                               side_effect=AssertionError("sent a request")):
            with self.assertRaises(CitycostError) as ctx:
                harvest.run(report)
        self.assertIn("denominator is unknown", ctx.exception.message)

    def test_a_spent_plan_cannot_be_run_twice(self):
        report = self._plan_and_run()
        with self.assertRaises(CitycostError):
            harvest.run(report)

    def test_run_refuses_something_that_is_not_a_plan(self):
        with self.assertRaises(CitycostError):
            harvest.run({"to_fetch": 3})

    def _plan_and_run(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)):
            with contextlib.redirect_stderr(io.StringIO()):
                return harvest.run(report)


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

    def test_the_fingerprint_reads_content_not_only_row_count(self):
        """Two snapshots can carry the same NUMBER of cities and different
        cities. Keying on the count alone would call those one table."""
        a = _table_page([("Zurich, Switzerland", 123.1)])
        b = _table_page([("Hanoi, Vietnam", 41.2)])
        self.assertNotEqual(rankings.panel_fingerprint(rankings._parse(a, "u")),
                            rankings.panel_fingerprint(rankings._parse(b, "u")))
        self.assertFalse(harvest._distinct_tables(
            [rankings._parse(a, "u"), rankings._parse(b, "u")])["repeated"])


class TestTheWriteIsReadBack(HarvestCase):
    def test_a_run_that_fetched_but_stored_nothing_is_not_a_success(self):
        """`net.cache_write` returns whether the write landed, but a successful
        fetch is still only the writer's report of its own work. A read-only
        home directory would otherwise let a 192-request harvest report success
        having kept nothing — and those requests are not repeatable while the
        address is banned."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)), \
             mock.patch.object(net, "cache_write", return_value=False):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                out = harvest.run(report)
        self.assertEqual((out["ok"], out["stored"]), (3, 0))
        self.assertFalse(out["complete"])
        self.assertEqual(harvest.exit_code(out), 1)
        self.assertIn("NOT on disk", buf.getvalue())

    def test_a_healthy_run_reads_back_everything_it_fetched(self):
        """The direction that rots: a warning that fires on healthy input gets
        muted, so the same run must come back clean."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        buf = io.StringIO()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)), \
             contextlib.redirect_stderr(buf):
            out = harvest.run(report)
        self.assertEqual((out["ok"], out["stored"]), (3, 3))
        self.assertNotIn("NOT on disk", buf.getvalue())


class TestLock(HarvestCase):
    def _write_lock(self, **holder):
        path = net.cache_dir() / harvest.LOCK_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(holder), encoding="utf-8")
        return path

    def _prepared(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        return self.plan_one()

    def test_a_second_harvest_is_refused_naming_the_holder(self):
        """Two concurrent harvests are not paced relative to each other at all:
        `net._last_hit` is per-process. They are two agents fanning out, which
        is the measured cause of the seven-day ban."""
        import socket
        report = self._prepared()
        self._write_lock(pid=os.getpid(), host=socket.gethostname(),
                         started_at=1756000000)
        with mock.patch.object(rankings, "http_get",
                               side_effect=AssertionError("sent a request")):
            with self.assertRaises(CitycostError) as ctx:
                harvest.run(report)
        self.assertIn(str(os.getpid()), ctx.exception.message)
        self.assertIn("remove", ctx.exception.remedy)

    def test_a_lock_whose_process_is_gone_does_not_wedge_the_command(self):
        """A kill skips `finally`. Recovering from the stale holder is what
        stops that from making the command permanently unusable — and it covers
        SIGKILL, which no signal handler can."""
        import socket
        report = self._prepared()
        self._write_lock(pid=self._dead_pid(), host=socket.gethostname(),
                         started_at=1756000000)
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)):
            with contextlib.redirect_stderr(io.StringIO()):
                out = harvest.run(report)
        self.assertEqual(out["ok"], 3)

    def test_a_lock_from_another_host_is_never_stolen(self):
        """A pid table is not a fact about a different machine, and cannot-tell
        must not be read as dead."""
        report = self._prepared()
        self._write_lock(pid=1, host="some-other-host", started_at=1756000000)
        with self.assertRaises(CitycostError):
            harvest.run(report)

    def test_the_lock_is_released_when_the_run_aborts(self):
        report = self._prepared()
        err = SourceUnavailable("refused (HTTP 429)", "wait", status=429)
        with mock.patch.object(rankings, "http_get", side_effect=err):
            with self.assertRaises(harvest.HarvestAborted):
                with contextlib.redirect_stderr(io.StringIO()):
                    harvest.run(report)
        self.assertFalse((net.cache_dir() / harvest.LOCK_NAME).exists())

    @staticmethod
    def _dead_pid() -> int:
        for candidate in range(99999, 40000, -1):
            try:
                os.kill(candidate, 0)
            except ProcessLookupError:
                return candidate
            except OSError:
                continue
        raise unittest.SkipTest("no free pid to impersonate")


class TestNoSecondRecord(HarvestCase):
    def test_a_run_writes_cache_entries_and_nothing_else(self):
        """A manifest is a second owner of 'what has been harvested'. It drifts
        from the cache the first time an entry is pruned or hand-deleted, after
        which the plan reports from the manifest and the run spends against the
        cache — and both look authoritative."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)):
            with contextlib.redirect_stderr(io.StringIO()):
                harvest.run(report)
        root = pathlib.Path(net.cache_dir())
        on_disk = {f.resolve() for f in root.rglob("*") if f.is_file()}
        accounted = {f.resolve() for f in net.cache_entries()}
        # Asserted against `net`'s own inventory rather than a path this test
        # rebuilds: the question is whether harvest left anything `net` does not
        # know about, and a second path builder here would answer a different
        # question convincingly.
        self.assertEqual(len(accounted), 4)      # 1 snapshot list + 3 tables
        self.assertEqual(on_disk - accounted, set())
        self.assertFalse((root / harvest.LOCK_NAME).exists())


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
