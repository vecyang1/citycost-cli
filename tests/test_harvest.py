"""`citycost/harvest.py` — the plan must cost what the run spends, and the run
must stop when the source says no.

The sweep itself: one transport, one harvest at a time, and the four ways it
refuses. These are the tests that go red if harvest sends a request its plan
did not cost, keeps going past an address-level block, sweeps while another
harvest holds the lock, treats the external fetcher as a licence to sweep
harder, or reports a table as harvested that never reached the disk.

Where a number in the plan came from is `test_harvest_plan.py`; how a number is
finally said is `test_harvest_report.py`.
"""

import contextlib
import io
import json
import os
import pathlib
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401  (must be first)
from ._harvest_case import (
    ARCHIVE_IDS, NO_TABLE_PAGE, NOW, ONE_TABLE, HarvestCase, _snapshot_page,
    distinct_pages)
from citycost import fallback, harvest, net, rankings
from citycost.errors import CitycostError, SourceUnavailable


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
        # The direction that rots: an estimate that hedged on a corpus which is
        # actually finished would be re-spent by anyone who believed it.
        self.assertEqual(out["rerun_requests"], 0)
        self.assertEqual(self.plan_one()["to_fetch"], 0)

    def test_rerun_requests_is_what_the_next_plan_actually_costs(self):
        """Measured: the report said a rerun costs 1 request and the very next
        plan said 2. An `empty` outcome never reaches `cache_write`, so the
        next plan counts it as absent again — and the rerun figure is the
        number a user budgets by against a source that bans by address, so two
        owners for it is two answers, both printed as facts."""
        report = self._seeded(["2020", "2019", "2018", "2017"])
        a, b = distinct_pages(2)
        pages = [a, NO_TABLE_PAGE,
                 SourceUnavailable("gone", "check it", status=404), b]
        with mock.patch.object(rankings, "http_get", side_effect=pages):
            out = self.stderr_run(report)
        self.assertEqual((out["ok"], out["failed"], out["empty"]), (2, 1, 1))
        self.assertEqual(out["rerun_requests"], 2)
        self.assertEqual(out["rerun_requests"], self.plan_one()["to_fetch"])

    def test_a_rerun_estimate_counts_what_did_not_land_on_disk(self):
        """`ok` is the writer's report of its own work. A table that was
        fetched and not stored is absent to the next plan, so an estimate keyed
        on outcomes under-reports by exactly the requests the user is least
        able to repeat."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)), \
             mock.patch.object(net, "cache_write", return_value=False):
            out = self.stderr_run(report)
        self.assertEqual((out["ok"], out["stored"]), (3, 0))
        self.assertEqual(out["rerun_requests"], 3)
        self.assertEqual(out["rerun_requests"], self.plan_one()["to_fetch"])

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
        with self.transport_trap("a cached corpus was refetched"):
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

    def test_the_request_that_triggered_the_abort_is_counted(self):
        """`_fetch_one` raises only after the transport has answered, so the
        aborting request was sent and belongs in the total. Measured: 4
        targets, a 429 at item 3 — three real calls, a report saying two.
        Under-counting requests against a source that bans BY ADDRESS is the
        wrong direction to be wrong in, and it also made the run's own
        live-read check accuse the transport of a request the report had
        simply dropped.
        """
        self.seed_list("cost-of-living", ["2020", "2019", "2018", "2017"])
        report = self.plan_one()
        a, b = distinct_pages(2)
        pages = [a, b, SourceUnavailable("refused (HTTP 429)",
                                         "use another network", status=429)]
        with mock.patch.object(rankings, "http_get", side_effect=pages) as get:
            with self.assertRaises(harvest.HarvestAborted) as ctx:
                with contextlib.redirect_stderr(io.StringIO()):
                    harvest.run(report)
        partial = ctx.exception.report
        self.assertEqual(get.call_count, 3)
        self.assertEqual(partial["requests_sent"], get.call_count)
        self.assertEqual(partial["attempted"], 3)
        # Stated rather than inferred: the item has no outcome, and a consumer
        # that subtracted ok+failed+empty from attempted would be guessing why.
        self.assertEqual(partial["unresolved"], 1)
        self.assertEqual(partial["ok"] + partial["failed"] + partial["empty"], 2)
        self.assertEqual(partial["live_reads"], partial["requests_sent"])

    def test_the_live_read_check_is_evaluated_on_the_abort_path_too(self):
        """Both aborts raise out of the target loop, and the one denominator
        check on the run itself sat after it — so on the single path where a
        second, uncosted route to the network matters most, nothing graded it.
        """
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        calls = {"n": 0}

        def greedy(*a, **kw):
            calls["n"] += 1
            net._READS["live"] += 2
            if calls["n"] == 2:
                raise SourceUnavailable("refused (HTTP 429)",
                                        "use another network", status=429)
            return rankings._parse(ONE_TABLE, "u")

        buf = io.StringIO()
        with mock.patch.object(rankings, "fetch", side_effect=greedy), \
             contextlib.redirect_stderr(buf):
            with self.assertRaises(harvest.HarvestAborted) as ctx:
                harvest.run(report)
        partial = ctx.exception.report
        self.assertGreater(partial["live_reads"], partial["attempted"])
        self.assertIn("harvest sent requests its plan did not cost",
                      buf.getvalue())

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
             mock.patch.object(fallback, "run",
                               side_effect=AssertionError("used the fetcher")), \
             self.transport_trap("sent a request under --fetch-mode always"):
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
        with self.transport_trap("swept with an unknown denominator"):
            with self.assertRaises(CitycostError) as ctx:
                harvest.run(report)
        self.assertIn("denominator is unknown", ctx.exception.message)

    def test_a_spent_plan_cannot_be_run_twice(self):
        report = self._plan_and_run()
        with self.transport_trap("re-ran a spent plan"):
            with self.assertRaises(CitycostError):
                harvest.run(report)

    def test_run_refuses_something_that_is_not_a_plan(self):
        with self.transport_trap("ran something that was never planned"):
            with self.assertRaises(CitycostError):
                harvest.run({"to_fetch": 3})

    def _plan_and_run(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)):
            with contextlib.redirect_stderr(io.StringIO()):
                return harvest.run(report)



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
        with self.transport_trap("swept while another harvest held the lock"):
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
        with self.transport_trap("stole a lock held by another machine"):
            with self.assertRaises(CitycostError):
                harvest.run(report)

    def test_the_foreign_host_refusal_is_the_only_thing_stopping_the_run(self):
        """Perform the regression, so the trap above is evidence rather than
        decoration.

        Read cannot-tell as dead and the lock is stolen: the loop runs, and
        because it runs against a banned address it is thrown out by a 429 —
        a `HarvestAborted`, which is a `CitycostError`, which is what the test
        above asserts. Measured: one request sent, one pass reported.
        """
        report = self._prepared()
        self._write_lock(pid=1, host="some-other-host", started_at=1756000000)
        with mock.patch.object(harvest, "_holder_is_alive", return_value=False):
            with self.transport_trap("stole a lock held by another machine"):
                with self.assertRaises(AssertionError) as ctx:
                    harvest.run(report)
        self.assertIn("stole a lock held by another machine", str(ctx.exception))

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




class TestResolve(HarvestCase):
    """`--resolve` spends exactly the list requests the plan named — never a
    table — so every denominator becomes known in one paced process. Measured
    2026-09-02: the first live harvest began with all seven lists expired, the
    plan said `unknown` seven times, and the remedy was seven commands."""

    def _lists_only(self, ids=ARCHIVE_IDS):
        """A transport that answers snapshot-list pages and TRAPS table pages:
        a resolve that fetched a table would be a harvest without consent."""
        def answer(url, **kw):
            if "?title=" in url or "rankings_current" in url:
                raise AssertionError(f"resolve fetched a TABLE: {url}")
            return _snapshot_page(ids)
        return answer

    def test_resolve_sends_exactly_the_list_requests_and_the_plan_becomes_known(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        before = harvest.plan(now=NOW)
        self.assertEqual(len(before["unknown_verticals"]), 6)
        self.assertEqual(before["list_requests"], 6)
        with mock.patch.object(rankings, "http_get",
                               side_effect=self._lists_only()) as get:
            with contextlib.redirect_stderr(io.StringIO()):
                after = harvest.resolve(before, now=NOW)
        self.assertEqual(get.call_count, 6)
        self.assertEqual(after["unknown_verticals"], [])
        self.assertEqual(after["denominator"], 7 * len(ARCHIVE_IDS))
        self.assertEqual(after["list_requests"], 0)
        self.assertEqual(after["requests_planned_state"], "exact")
        self.assertFalse(after["executed"])
        self.assertEqual(after["requests_sent"], 0)

    def test_resolve_with_every_denominator_known_sends_nothing(self):
        for v in sorted(rankings.VERTICALS):
            self.seed_list(v, ARCHIVE_IDS)
        before = harvest.plan(now=NOW)
        with self.transport_trap("resolve sent a request with nothing unknown"):
            after = harvest.resolve(before, now=NOW)
        self.assertEqual(after["denominator"], before["denominator"])
        self.assertEqual(after["unknown_verticals"], [])

    def test_resolve_keeps_the_scope_it_was_given(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        before = harvest.plan(["cost-of-living", "crime"], now=NOW)
        with mock.patch.object(rankings, "http_get",
                               side_effect=self._lists_only()) as get:
            with contextlib.redirect_stderr(io.StringIO()):
                after = harvest.resolve(before, now=NOW)
        self.assertEqual(get.call_count, 1)
        self.assertEqual([r["vertical"] for r in after["verticals"]],
                         ["cost-of-living", "crime"])
        self.assertEqual(after["scope"], "subset")

    def test_resolve_stops_at_the_first_refusal(self):
        """The block is address-level and does not slide; the second list
        request after a 429 spends the address on behalf of whoever uses it
        next."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        before = harvest.plan(now=NOW)
        calls = {"n": 0}

        def refused(url, **kw):
            calls["n"] += 1
            raise SourceUnavailable(f"{url} refused this client (HTTP 429)",
                                    "use another network", status=429)

        with mock.patch.object(rankings, "http_get", side_effect=refused):
            with self.assertRaises(CitycostError) as ctx:
                harvest.resolve(before, now=NOW)
        self.assertEqual(calls["n"], 1)
        self.assertIn("list 1 of 6", ctx.exception.message)
        self.assertNotIsInstance(ctx.exception, harvest.HarvestAborted)

    def test_a_rerouted_list_read_refuses_to_continue(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        before = harvest.plan(now=NOW)
        calls = {"n": 0}

        def maybe_rerouted(url, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                fallback.record(url, 429, "GET")
            return _snapshot_page(ARCHIVE_IDS)

        with mock.patch.object(rankings, "http_get", side_effect=maybe_rerouted):
            with self.assertRaises(CitycostError) as ctx:
                with contextlib.redirect_stderr(io.StringIO()):
                    harvest.resolve(before, now=NOW)
        self.assertEqual(calls["n"], 2)
        self.assertIn("repair, not a licence", ctx.exception.remedy)

    def test_resolve_under_fetch_mode_always_is_refused_before_request_one(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        before = harvest.plan(now=NOW)
        with mock.patch.dict(os.environ, {"CITYCOST_FETCH_MODE": "always"}), \
             self.transport_trap("resolve sent a request under always"):
            with self.assertRaises(CitycostError) as ctx:
                harvest.resolve(before, now=NOW)
        self.assertIn("repair, not a licence", ctx.exception.remedy)

    def test_resolve_refuses_a_spent_plan_and_a_non_plan(self):
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        report = self.plan_one()
        with mock.patch.object(rankings, "http_get",
                               side_effect=distinct_pages(3)):
            with contextlib.redirect_stderr(io.StringIO()):
                spent = harvest.run(report)
        with self.transport_trap("resolved a spent plan"):
            with self.assertRaises(CitycostError):
                harvest.resolve(spent, now=NOW)
            with self.assertRaises(CitycostError):
                harvest.resolve({"not": "a plan"}, now=NOW)

    def test_the_command_resolves_then_prints_the_known_plan(self):
        from citycost import cli
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rankings, "http_get",
                               side_effect=self._lists_only()) as get:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["harvest", "--resolve",
                               "--index", "cost-of-living", "--index", "crime"])
        self.assertEqual(rc, 0)
        self.assertEqual(get.call_count, 1)          # only crime was unknown
        self.assertNotIn("unknown", out.getvalue())
        self.assertIn("learned 1 snapshot list", err.getvalue())
        self.assertIn("plan only", err.getvalue())

    def test_resolve_and_execute_together_spend_the_plan_that_was_printed(self):
        from citycost import cli
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        pages = iter(distinct_pages(2 * len(ARCHIVE_IDS)))

        def answer(url, **kw):
            return next(pages) if "?title=" in url else _snapshot_page(ARCHIVE_IDS)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rankings, "http_get", side_effect=answer) as get:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["harvest", "--resolve", "--execute",
                               "--index", "cost-of-living", "--index", "crime"])
        self.assertEqual(rc, 0)
        self.assertEqual(get.call_count, 1 + 2 * len(ARCHIVE_IDS))
        self.assertIn("harvest: planned 6", err.getvalue())
        # The plan the user saw is the plan that was spent: no `unknown` cell
        # reached stdout before the run began.
        self.assertNotIn("unknown", out.getvalue())



class TestResolveFailureReporting(HarvestCase):
    """The findings a code review raised: an ordinary failure must disclose the
    partial cache the block path already discloses, and the two `cmd_harvest`
    notes must be exercised through the real entry point rather than trusted."""

    def test_an_ordinary_failure_names_the_lists_already_learned(self):
        """A non-blocked SourceUnavailable (a transient 500, a URLError) leaves
        the same partial state on disk as a 429 does — a rerun skips what was
        learned — so it must say so too. It reported nothing before this."""
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        before = harvest.plan(now=NOW)
        self.assertEqual(before["unknown_verticals"][0], "crime")
        calls = {"n": 0}

        def answer(url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _snapshot_page(ARCHIVE_IDS)       # crime list learned
            raise SourceUnavailable("t/ returned HTTP 500", "retry later",
                                    status=500)          # health-care 500s

        with mock.patch.object(rankings, "http_get", side_effect=answer), \
             mock.patch.object(net.time, "sleep"):
            with self.assertRaises(CitycostError) as ctx:
                harvest.resolve(before, now=NOW)
        self.assertNotIsInstance(ctx.exception, harvest.HarvestAborted)
        self.assertIn("could not learn the snapshot list for health-care",
                      ctx.exception.message)
        self.assertIn("list 2 of 6", ctx.exception.message)
        # The disclosure the block path always had, now on this path too.
        self.assertIn("1 list(s) were learned and are cached",
                      ctx.exception.message)

    def test_resolve_under_always_refuses_even_with_nothing_unknown(self):
        """`run()` refuses `--fetch-mode always` unconditionally; `resolve`
        used to skip that check on its all-known early return, so it was the one
        path that quietly succeeded under a mode the tool refuses."""
        for v in sorted(rankings.VERTICALS):
            self.seed_list(v, ARCHIVE_IDS)
        before = harvest.plan(now=NOW)
        self.assertEqual(before["unknown_verticals"], [])
        with mock.patch.dict(os.environ, {"CITYCOST_FETCH_MODE": "always"}), \
             self.transport_trap("resolve ran under always with nothing unknown"):
            with self.assertRaises(CitycostError) as ctx:
                harvest.resolve(before, now=NOW)
        self.assertIn("repair, not a licence", ctx.exception.remedy)

    def test_the_all_known_note_prints_through_the_command(self):
        from citycost import cli
        for v in sorted(rankings.VERTICALS):
            self.seed_list(v, ARCHIVE_IDS)
        out, err = io.StringIO(), io.StringIO()
        with self.transport_trap("sent a request when nothing was unknown"):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["harvest", "--resolve"])
        self.assertEqual(rc, 0)
        self.assertIn("every denominator was already known", err.getvalue())

    def test_a_list_that_resolves_empty_is_reported_as_still_unknown(self):
        """A list page whose <select> carries no snapshot id caches as empty,
        so the denominator stays unknown after a successful fetch. The command
        must say so rather than imply the plan is now complete."""
        from citycost import cli
        self.seed_list("cost-of-living", ARCHIVE_IDS)
        empty = ("<html><body><select name='title'>"
                 "<option value='USD'>USD</option></select>"
                 "<table id='t2'></table></body></html>")

        def answer(url, **kw):
            return empty if "rankings.jsp" in url and "?title=" not in url \
                else _snapshot_page(ARCHIVE_IDS)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rankings, "http_get", side_effect=answer), \
             mock.patch.object(net.time, "sleep"):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["harvest", "--resolve"])
        self.assertIn("still unknown after resolving", err.getvalue())
        self.assertIn("learned 0 snapshot list", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
