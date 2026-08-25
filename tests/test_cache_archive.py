"""The archive: what may be kept forever, and what may be served without asking.

Two axes that used to be one number. RETENTION (may I delete this file?) is now
decided by the file's location on disk; FRESHNESS (may I serve it without
asking the network?) by a pure predicate re-derived at every read. They were
fused in `st_mtime`, which is why an immutable 2019 archive expired on the same
clock as `current` — and why the command a person runs to *look* at the cache
deleted part of it.

Both directions are asserted throughout. A test that only proves the archive
survives `prune_cache` still passes on a build where prune deletes nothing at
all, and a test that only proves a pin is honoured still passes on a build
where the cache is never re-read.
"""

import datetime as dt
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import net, rankings

NOW = dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc).timestamp()


class TestIsArchival(unittest.TestCase):
    """Every unknown must degrade toward MUTABLE.

    The two errors are not symmetric. Under-pinning costs one request and, on a
    banned address, produces the existing loud 429 carrying the server's own
    deadline. Over-pinning produces a complete, plausible table that never
    expires and is protected from prune — a wrong number no future run can
    discover.
    """

    PINNED = ["2025", "2025-mid", "2019", "2014", "2009"]
    REFUSED = [None, "", "current", "2026", "2026-mid", "2027", "lol",
               "20261", "201", "2019-end", "2019 ", " 2019", "title=2019"]

    def test_finished_periods_are_pinned(self):
        for snap in self.PINNED:
            with self.subTest(snap=snap):
                self.assertTrue(rankings.is_archival(snap, now=NOW))

    def test_every_unknown_and_unfinished_id_is_refused(self):
        for snap in self.REFUSED:
            with self.subTest(snap=snap):
                self.assertFalse(rankings.is_archival(snap, now=NOW))

    def test_the_current_half_year_is_not_pinned(self):
        """2026-mid is the trap. Nothing measured distinguishes 'frozen at
        publication' from 'still filling' — DATA-STRATEGY records it at 547
        rows against current's 558, which is equally consistent with both
        stories. Pinning a still-forming table freezes half a year of partial
        data forever, and the only tell would be a large age, which is exactly
        the signal an archive teaches the reader to ignore."""
        self.assertFalse(rankings.is_archival("2026-mid", now=NOW))
        # ...and it becomes pinnable once its year has ended plus a cycle.
        later = dt.datetime(2027, 8, 1, tzinfo=dt.timezone.utc).timestamp()
        self.assertTrue(rankings.is_archival("2026-mid", now=later))

    def test_the_grace_period_is_the_thing_that_moves_the_boundary(self):
        """Asserted against the constant rather than a hardcoded date, so
        changing the cadence assumption cannot silently change behaviour
        without this failing."""
        just_after_2025_ended = dt.datetime(
            2026, 1, 2, tzinfo=dt.timezone.utc).timestamp()
        self.assertFalse(rankings.is_archival("2025", now=just_after_2025_ended))
        past_grace = just_after_2025_ended + (
            rankings.PUBLICATION_CYCLE_DAYS * 86400)
        self.assertTrue(rankings.is_archival("2025", now=past_grace))

    def test_an_observed_move_overrules_every_age_rule(self):
        """`moved` is the falsifying observation. Without it, 'this snapshot is
        immutable' is a check that cannot fail."""
        self.assertTrue(rankings.is_archival("2019", now=NOW))
        self.assertFalse(rankings.is_archival("2019", now=NOW, moved=True))

    def test_one_grammar_object_for_a_snapshot_id(self):
        """The list reader and the pin predicate must not hold two literals for
        one grammar: a widened pattern accepted by the reader and unknown to the
        predicate would change behaviour with nothing going red."""
        self.assertTrue(rankings.SNAPSHOT_ID.fullmatch("2026-mid"))
        self.assertIsNone(rankings.SNAPSHOT_ID.fullmatch("current"))
        src = Path(rankings.__file__).read_text(encoding="utf-8")
        self.assertEqual(src.count(r'r"\d{4}(-mid)?"'), 1,
                         "the snapshot-id pattern is written more than once")


class TestArchivalCacheBehaviour(unittest.TestCase):
    def setUp(self):
        self.key = f"rank:test:{time.time()}"

    def test_an_archival_entry_ignores_the_age_clock(self):
        calls = []
        net.cached_json(self.key, "v1", 3600,
                        lambda: calls.append(1) or {"a": 1}, archival=True)
        # Age it far past any TTL the CLI could pass.
        path = net._existing_path(self.key)
        os.utime(path, (time.time() - 400 * 86400,) * 2)
        payload, age, cached = net.cached_json(
            self.key, "v1", 3600, lambda: calls.append(1) or {"a": 2},
            archival=True)
        self.assertTrue(cached)
        self.assertEqual(payload, {"a": 1})
        self.assertGreater(age, 86400)
        self.assertEqual(len(calls), 1)

    def test_an_ordinary_entry_of_the_same_age_is_refetched(self):
        """The control. Without it the test above passes on a build where the
        cache is simply never expired for anything."""
        calls = []
        net.cached_json(self.key, "v1", 3600,
                        lambda: calls.append(1) or {"a": 1})
        os.utime(net._existing_path(self.key), (time.time() - 400 * 86400,) * 2)
        _, _, cached = net.cached_json(self.key, "v1", 3600,
                                       lambda: calls.append(1) or {"a": 2})
        self.assertFalse(cached)
        self.assertEqual(len(calls), 2)

    def test_max_age_zero_still_reaches_the_network_for_a_pinned_entry(self):
        """Asserted on the CALL, not on the returned value.

        A value-returning stub makes a wrong order look like a right one: if
        `produce` were skipped and the pinned payload returned, an assertion on
        the value would still pass whenever the two happen to match. `doctor`'s
        entire contract is that no cache can answer it, and 1.3.0 is a whole
        release about a parameter accepted and silently ignored.
        """
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1}, archival=True)
        produce = mock.Mock(return_value={"a": 2})
        net.cached_json(self.key, "v1", 0, produce, archival=True)
        produce.assert_called_once()

    def test_max_age_nonzero_does_not_reach_the_network_for_a_pinned_entry(self):
        """The other direction, so the test above cannot pass on a build that
        simply always calls produce."""
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1}, archival=True)
        trap = mock.Mock(side_effect=AssertionError(
            "produce() was called for a fresh pinned entry"))
        net.cached_json(self.key, "v1", 3600, trap, archival=True)
        trap.assert_not_called()

    def test_a_pinned_entry_lives_under_keep_and_an_ordinary_one_does_not(self):
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1}, archival=True)
        self.assertEqual(net._existing_path(self.key).parent.name,
                         net.ARCHIVE_DIR)
        other = self.key + ":ordinary"
        net.cached_json(other, "v1", 3600, lambda: {"a": 1})
        self.assertEqual(net._existing_path(other).parent,
                         net.cache_dir())

    def test_an_entry_that_changes_side_leaves_no_duplicate_behind(self):
        """Two copies of one key is two answers to one question, and the stale
        one wins whenever it is the first found."""
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1})
        ordinary = net._cache_path(self.key)
        self.assertTrue(ordinary.exists())
        net.cached_json(self.key, "v1", 0, lambda: {"a": 2}, archival=True)
        self.assertFalse(ordinary.exists())
        self.assertTrue(net._cache_path(self.key, archival=True).exists())


class TestRetentionSparesTheArchive(unittest.TestCase):
    """Pinned in BOTH directions, so a future `rglob` "fix" goes red instead of
    silently deleting a corpus that took hundreds of throttled requests and
    cannot be re-taken while the address is banned."""

    def setUp(self):
        self.pinned = f"rank:pinned:{time.time()}"
        self.plain = f"rank:plain:{time.time()}"
        net.cached_json(self.pinned, "v1", 3600, lambda: {"a": 1},
                        archival=True)
        net.cached_json(self.plain, "v1", 3600, lambda: {"a": 1})
        old = time.time() - (net.CACHE_RETENTION + 86400)
        for k in (self.pinned, self.plain):
            os.utime(net._existing_path(k), (old, old))

    def test_prune_removes_the_expired_ordinary_entry(self):
        res = net.prune_cache()
        self.assertFalse(net._cache_path(self.plain).exists())
        self.assertGreaterEqual(res["removed"], 1)

    def test_prune_spares_the_expired_pinned_entry(self):
        net.prune_cache()
        self.assertTrue(net._cache_path(self.pinned, archival=True).exists())

    def test_prune_reports_its_own_denominator(self):
        """`pruned 0 expired cache files` was the same sentence for a clean
        cache and for a CITYCOST_CACHE_DIR pointing at an empty or misspelt
        directory — and the second is a user reading a zero denominator as a
        clean bill."""
        res = net.prune_cache()
        self.assertIn("scanned", res)
        self.assertIn("protected", res)
        self.assertEqual(res["dir"], str(net.cache_dir()))
        self.assertGreaterEqual(res["scanned"], 1)

    def test_prune_on_an_empty_directory_reports_a_zero_denominator(self):
        with mock.patch.dict(os.environ,
                             {"CITYCOST_CACHE_DIR": "/nonexistent/citycost"}):
            res = net.prune_cache()
        self.assertEqual(res["scanned"], 0)
        self.assertEqual(res["removed"], 0)
        self.assertIn("nonexistent", res["dir"])

    def test_clear_spares_the_archive_unless_it_is_named(self):
        res = net.clear_cache()
        self.assertTrue(net._cache_path(self.pinned, archival=True).exists())
        self.assertGreaterEqual(res["protected"], 1)

    def test_clear_include_archive_does_remove_it(self):
        net.clear_cache(include_archive=True)
        self.assertFalse(net._cache_path(self.pinned, archival=True).exists())


class TestCacheProbe(unittest.TestCase):
    """One predicate, and it answers with a REASON. 'You have 140 of 192' and
    'you have 192 files, all under the previous schema' cost 52 and 192
    requests respectively, and a boolean makes them the same sentence."""

    def setUp(self):
        self.key = f"probe:{time.time()}"

    def test_absent(self):
        self.assertEqual(net.cache_probe(self.key, "v1", 3600)["state"],
                         "absent")

    def test_fresh_then_stale(self):
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1})
        self.assertEqual(net.cache_probe(self.key, "v1", 3600)["state"],
                         "fresh")
        os.utime(net._existing_path(self.key), (time.time() - 7200,) * 2)
        self.assertEqual(net.cache_probe(self.key, "v1", 3600)["state"],
                         "stale")

    def test_schema_mismatch_is_not_absent(self):
        """A schema bump is a normal parser fix — this repo has shipped one —
        and it invalidates every entry at once. Reported as its own state
        because 'missing' and 'present but unservable' have different costs."""
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1})
        self.assertEqual(net.cache_probe(self.key, "v2", 3600)["state"],
                         "schema_mismatch")

    def test_unreadable_is_not_absent(self):
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1})
        net._existing_path(self.key).write_text("{not json", encoding="utf-8")
        self.assertEqual(net.cache_probe(self.key, "v1", 3600)["state"],
                         "unreadable")

    def test_max_age_zero_is_read_live_not_a_small_number(self):
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1}, archival=True)
        self.assertEqual(net.cache_probe(self.key, "v1", 0,
                                         archival=True)["state"], "stale")

    def test_probing_never_counts_as_a_read(self):
        """`net.reads()` answers 'did this process touch the network'. A plan
        that inflated it would make `--fetch-mode`'s own warning lie."""
        net.cached_json(self.key, "v1", 3600, lambda: {"a": 1})
        net.reset_reads()
        net.cache_probe(self.key, "v1", 3600)
        self.assertEqual(net.reads(), {"live": 0, "cached": 0})


class TestFetchRecordsTheFalsifyingObservation(unittest.TestCase):
    TABLE_A = {"url": "u", "columns": ["Cost of Living Index"],
               "rows": [{"place": "Da Nang, Vietnam",
                         "metrics": {"Cost of Living Index": 30.0}}]}
    TABLE_B = {"url": "u", "columns": ["Cost of Living Index"],
               "rows": [{"place": "Da Nang, Vietnam",
                         "metrics": {"Cost of Living Index": 31.0}}]}

    def test_fingerprint_changes_with_content_and_not_with_identity(self):
        self.assertEqual(rankings.panel_fingerprint(self.TABLE_A),
                         rankings.panel_fingerprint(dict(self.TABLE_A)))
        self.assertNotEqual(rankings.panel_fingerprint(self.TABLE_A),
                            rankings.panel_fingerprint(self.TABLE_B))

    def test_fingerprint_is_sensitive_to_row_count(self):
        """Table size is what makes the repeated-table check safe to assert: a
        genuinely unchanging city still sits in tables of different sizes,
        because Numbeo's snapshots are not monotonic."""
        shorter = {**self.TABLE_A, "rows": []}
        self.assertNotEqual(rankings.panel_fingerprint(self.TABLE_A),
                            rankings.panel_fingerprint(shorter))

    def test_a_snapshot_that_moves_is_permanently_unpinned(self):
        with mock.patch.object(rankings, "_parse",
                               side_effect=[self.TABLE_A, self.TABLE_B]), \
             mock.patch.object(rankings, "http_get", return_value="<html>"):
            first = rankings.fetch("cost-of-living", snapshot="2019")
            self.assertTrue(first["_archival"])
            key = f"rank:{rankings._url('cost-of-living', 'city', '2019', None)}"
            self.assertTrue(net._cache_path(key, archival=True).exists())
            # Force a live re-read; the content has changed underneath us.
            rankings.fetch("cost-of-living", snapshot="2019", max_age=0)

        blob = json.loads(net._existing_path(key).read_text(encoding="utf-8"))
        self.assertTrue(blob["_meta"]["moved"])
        # ...and having moved, it is no longer stored as an archive.
        self.assertFalse(net._cache_path(key, archival=True).exists())
        self.assertTrue(net._cache_path(key).exists())


class TestCurrentIsNotATitle(unittest.TestCase):
    """`current` is the absence of a title, not a title whose value is
    "current". Numbeo answers an unrecognised `?title=` with the current table,
    so sending it *worked* — while making one table reachable under two cache
    keys, and while making "the URL carries a title" useless as a test of
    whether a payload is an archive."""

    def test_no_view_sends_a_literal_current_title(self):
        for view in ("city", "country", "region"):
            with self.subTest(view=view):
                url = rankings._url("cost-of-living", view, "current", "asia")
                self.assertNotIn("title=current", url)

    def test_current_and_unspecified_resolve_to_one_cache_key(self):
        for view in ("city", "country"):
            with self.subTest(view=view):
                self.assertEqual(
                    rankings._url("cost-of-living", view, "current", None),
                    rankings._url("cost-of-living", view, None, None))

    def test_a_real_snapshot_still_carries_its_title(self):
        for view in ("city", "country", "region"):
            with self.subTest(view=view):
                self.assertIn(
                    "title=2019",
                    rankings._url("cost-of-living", view, "2019", "asia"))


class TestCacheStatus(unittest.TestCase):
    def test_mtime_skew_is_reported(self):
        """mtime is not a fact about when a figure was read. A corpus copied
        between machines or restored from backup arrives with every mtime set
        to today, so a 300-day-old entry announces `age 0s` and a genuinely
        stale figure passes every freshness rule in the tool."""
        key = f"skew:{time.time()}"
        net.cached_json(key, "v1", 3600, lambda: {"a": 1})
        path = net._existing_path(key)
        blob = json.loads(path.read_text(encoding="utf-8"))
        blob["_fetched_at"] = int(time.time() - 300 * 86400)
        path.write_text(json.dumps(blob), encoding="utf-8")
        st = net.cache_status()
        self.assertGreaterEqual(st["mtime_skew"], 1)
        self.assertGreaterEqual(st["oldest_fetch_s"], 299 * 86400)

    def test_status_reads_nothing_from_the_network(self):
        net.reset_reads()
        net.cache_status()
        self.assertEqual(net.reads()["live"], 0)


if __name__ == "__main__":
    unittest.main()
