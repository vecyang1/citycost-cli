"""`movers` must be able to prove it diffed two different tables.

This file grades the command's outside: what a run fetches and what it spends,
what the caller receives back after ordering and truncation, and what the exit
code claims. The two questions inside a run are graded beside their own
modules — `test_comparable` for every refusal that fires before a delta exists,
`test_diffing` for the arithmetic and the observers that read it back.

Every failure this command guards produces a *complete, well-formed, plausible*
answer rather than an error, so each test below comes in two directions: the
degenerate input that must be caught, and the healthy input that must not be —
because a guard that cannot fire and a guard that always fires read identically
from outside, and the second gets muted within a week.

Offline by construction. Nothing here touches numbeo.com: the transport is
either mocked or, where the property under test is "was this called at all",
replaced by a trap that raises at the moment the wrong call happens.
"""

import datetime as dt
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401  (must be first)
from ._tables import COL, _html, _row, _table
from citycost import comparable, diffing, movers, net, rankings
from citycost.errors import SourceUnavailable


class TheDriftProbeSeparatesTwoCauses(unittest.TestCase):
    """Identical tables have two causes with opposite fixes, and one symptom.

    `?title=` ignored upstream (fix: this client, and every delta is 0 for a
    reason unrelated to these cities) versus two ids that genuinely name one
    published table (fix: pick different ids). The probe against the OLDEST
    published snapshot is what tells them apart: if that is identical too, the
    parameter is not being honoured at all.
    """

    PUBLISHED = ["2026-mid", "2019", "2014"]
    CURRENT = _table([_row("A, X", **{COL: 2.0}), _row("B, Y", **{COL: 4.0})])
    OLD = _table([_row("A, X", **{COL: 1.0}), _row("B, Y", **{COL: 3.0})])

    def _run(self, tables, **kwargs):
        with mock.patch.object(rankings, "snapshots",
                               return_value=list(self.PUBLISHED)), \
             mock.patch.object(rankings, "fetch",
                               side_effect=list(tables)) as fetch:
            payload = movers.compare_snapshots(frm=kwargs.pop("frm", "2019"),
                                               to=kwargs.pop("to", "current"),
                                               **kwargs)
        return payload, fetch

    def test_identical_tables_and_an_identical_oldest_is_upstream_drift(self):
        payload, fetch = self._run([self.CURRENT, self.CURRENT, self.CURRENT])
        self.assertTrue(payload["identical_tables"])
        self.assertEqual(payload["drift"], "snapshot_parameter_ignored")
        self.assertEqual(movers.verdict(payload)["exit"], 1)
        self.assertEqual(fetch.call_count, 3)

    def test_identical_tables_but_a_different_oldest_is_a_usage_error(self):
        payload, _ = self._run([self.CURRENT, self.CURRENT, self.OLD])
        self.assertEqual(payload["drift"], "same_published_table")
        self.assertEqual(movers.verdict(payload)["exit"], 2)

    def test_the_two_causes_do_not_share_an_exit_code(self):
        drift, _ = self._run([self.CURRENT, self.CURRENT, self.CURRENT])
        usage, _ = self._run([self.CURRENT, self.CURRENT, self.OLD])
        self.assertNotEqual(movers.verdict(drift)["exit"],
                            movers.verdict(usage)["exit"])

    def test_the_degenerate_payload_is_still_complete(self):
        """The run worth catching must not be the run that crashes somebody's
        parser."""
        payload, _ = self._run([self.CURRENT, self.CURRENT, self.CURRENT])
        for key in ("movers", "rows", "joined", "counts", "join", "stats",
                    "basis", "from", "to", "probe"):
            self.assertIn(key, payload)
        self.assertEqual(payload["probe"]["snapshot"], "2014")

    def test_a_healthy_run_never_spends_the_probe(self):
        payload, fetch = self._run([self.OLD, self.CURRENT])
        self.assertFalse(payload["identical_tables"])
        self.assertIsNone(payload["drift"])
        self.assertIsNone(payload["probe"])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(movers.verdict(payload)["exit"], 0)

    def test_the_documented_entry_point_carries_the_presentation_keys(self):
        """`compare_snapshots` is what the README and the skill call, and
        `'truncated' in compare_snapshots(...)` was False until `present()`
        ran — i.e. on exactly the degenerate runs a consumer inspects most."""
        payload, _ = self._run([self.OLD, self.CURRENT])
        for key in movers.PRESENTATION_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, payload)

    def test_the_read_count_is_measured_rather_than_asserted_against_itself(
            self):
        """`reads` was assembled from literals — `{"tables": 2, "total": 3 +
        p}` — while `net.reads()` is the one thing in this repo that counts a
        read. The old assertion compared the literal to itself and was
        therefore incapable of noticing the two disagreeing, which they did.

        Run through the real cache layer with only the transport mocked,
        because a mock at `rankings.fetch` is exactly what hides the question:
        it makes both the counter and the payload say nothing, agreeably."""
        net.clear_cache(include_archive=True)
        old = _html([("Aville, Xland", (10.0, 1.0))])
        new = _html([("Aville, Xland", (20.0, 2.0)),
                     ("Bville, Yland", (30.0, 3.0))])

        def fake_get(url, **kw):
            return old if "title=2019" in url else new

        with mock.patch.object(rankings, "snapshots",
                               return_value=list(self.PUBLISHED)), \
             mock.patch.object(rankings, "http_get", side_effect=fake_get):
            net.reset_reads()
            payload = movers.compare_snapshots(frm="2019", to="current",
                                               max_age=3600)
            counter = net.reads()
            self.assertEqual(counter["live"], 2, "two tables, both live")
            self.assertEqual(payload["reads"]["live"], counter["live"])
            self.assertEqual(payload["reads"]["cached"], counter["cached"])
            self.assertEqual(payload["reads"]["total"],
                             counter["live"] + counter["cached"])

            # The half that catches a figure hardcoded to the healthy shape: a
            # second run answers from disk, so a truthful counter MOVES.
            net.reset_reads()
            again = movers.compare_snapshots(frm="2019", to="current",
                                             max_age=3600)
            self.assertEqual(again["reads"]["live"], 0)
            self.assertEqual(again["reads"]["cached"], net.reads()["cached"])
            self.assertNotEqual(again["reads"], payload["reads"])
        net.clear_cache(include_archive=True)

    def test_the_snapshot_list_is_never_iterated(self):
        """'Diff every consecutive pair' would be thirty requests against a
        source that bans this address for seven days on volume."""
        many = [f"20{n:02d}" for n in range(9, 27)]
        with mock.patch.object(rankings, "snapshots", return_value=many), \
             mock.patch.object(rankings, "fetch",
                               side_effect=[self.OLD, self.CURRENT]) as fetch:
            movers.compare_snapshots(frm="2019", to="current")
        self.assertEqual(fetch.call_count, 2)

    def test_the_probe_targets_the_oldest_because_drift_can_be_partial(self):
        """`?title=` honoured on recent ids and ignored on old ones is caught
        only by a probe that targets the oldest."""
        seen = []

        def fake(vertical, **kw):
            seen.append(kw.get("snapshot"))
            return self.CURRENT
        with mock.patch.object(rankings, "snapshots",
                               return_value=list(self.PUBLISHED)), \
             mock.patch.object(rankings, "fetch", side_effect=fake):
            movers.compare_snapshots(frm="2019", to="current")
        self.assertEqual(seen[-1], "2014")


class TheProbeMustBeAbleToDISAGREE(unittest.TestCase):
    """A probe that re-fetches one of the two sides answers by construction.

    When the oldest published id IS `--from` or `--to`, the probe fetches a
    table it already knows equals the other one, so `same_panel(t_old, t_to)`
    is True whatever the source did and the verdict is
    `snapshot_parameter_ignored` no matter what. Measured with 2014 and 2019
    serving one published table while 2026-mid genuinely differs: `--from 2014
    --to 2019` blamed this client's `?title=` handling and exited 1, for a
    source that was distinguishing ids perfectly well.

    A read-back that returns the same value whichever way the world went is not
    a measurement. Better to say the probe is unavailable than to return a
    verdict it cannot support.
    """

    PUBLISHED = ["2026-mid", "2019", "2014"]
    ONE = _table([_row("A, X", **{COL: 1.0}), _row("B, Y", **{COL: 2.0})])
    OTHER = _table([_row("A, X", **{COL: 9.0}), _row("B, Y", **{COL: 8.0})])

    def _run(self, frm, to, world, published=None):
        seen = []

        def fake(vertical, **kw):
            seen.append(kw.get("snapshot"))
            return world(kw.get("snapshot"))

        with mock.patch.object(rankings, "snapshots",
                               return_value=list(published or self.PUBLISHED)), \
             mock.patch.object(rankings, "fetch", side_effect=fake):
            return movers.compare_snapshots(frm=frm, to=to), seen

    def _two_ids_one_table(self, snapshot):
        """2014 and 2019 serve one published table; 2026-mid and `current` do
        not. A source that is behaving."""
        return self.ONE if snapshot in ("2014", "2019") else self.OTHER

    def test_a_third_id_is_probed_rather_than_one_of_the_two_sides(self):
        payload, seen = self._run("2014", "2019", self._two_ids_one_table)
        self.assertTrue(payload["identical_tables"])
        self.assertNotIn(seen[-1], ("2014", "2019"),
                         "the probe re-fetched a side it already held")
        self.assertEqual(payload["probe"]["snapshot"], "2026-mid")
        self.assertEqual(payload["drift"], "same_published_table")
        self.assertEqual(movers.verdict(payload)["exit"], 2)

    def test_one_fact_gets_one_verdict_whichever_side_holds_the_oldest_id(self):
        """The measured contradiction, as a test: the same world read twice,
        differing only in which side happens to be the oldest published id."""
        a, _ = self._run("2014", "2019", self._two_ids_one_table)
        b, _ = self._run("2019", "2014", self._two_ids_one_table)
        # Named, not merely compared: the broken version agreed with itself
        # perfectly — it said `snapshot_parameter_ignored` both ways round, and
        # an assertion that only checks the two agree passes on that.
        self.assertEqual([a["drift"], b["drift"]],
                         ["same_published_table"] * 2)
        self.assertEqual(movers.verdict(a)["exit"], movers.verdict(b)["exit"])

    def test_the_upstream_drift_verdict_still_fires_when_every_id_agrees(self):
        """The direction that must not be lost while fixing the other: when
        `?title=` really is ignored, the third id serves the same table too."""
        payload, seen = self._run("2014", "2019", lambda _snapshot: self.ONE)
        self.assertEqual(seen[-1], "2026-mid")
        self.assertEqual(payload["drift"], "snapshot_parameter_ignored")
        self.assertTrue(payload["probe"]["identical_to_to"])
        self.assertEqual(movers.verdict(payload)["exit"], 1)

    def test_with_no_third_id_the_probe_is_unavailable_not_guessed(self):
        """Every published id is one of the two sides, so nothing can tell the
        two causes apart. The tables ARE identical — every delta below is 0 —
        so the run still refuses; what it must not do is name a cause it did
        not observe, since the two causes have opposite fixes."""
        payload, seen = self._run("2014", "2019", lambda _snapshot: self.ONE,
                                  published=["2019", "2014"])
        self.assertEqual(seen, ["2014", "2019"], "a third fetch was spent")
        self.assertFalse(payload["probe"]["available"])
        self.assertIsNone(payload["probe"]["snapshot"])
        self.assertIsNone(payload["probe"]["identical_to_to"])
        self.assertEqual(payload["drift"], "identical_tables_cause_unknown")
        self.assertEqual(movers.verdict(payload)["exit"], 1)
        self.assertIn("identical_tables_cause_unknown",
                      movers.verdict(payload)["reasons"])

    def test_the_probe_key_set_does_not_depend_on_the_answer(self):
        """A key that appears only on some paths is its own trap, and `probe`
        is read by the renderer."""
        probed, _ = self._run("2014", "2019", self._two_ids_one_table)
        blind, _ = self._run("2014", "2019", lambda _s: self.ONE,
                             published=["2019", "2014"])
        self.assertEqual(set(probed["probe"]), set(blind["probe"]))
        self.assertGreaterEqual(len(probed["probe"]), 5,
                                f"graded {len(probed['probe'])} probe keys")


class OrderingAndTruncation(unittest.TestCase):
    def setUp(self):
        a = _table([_row("Rising, X", **{COL: 25.0}),
                    _row("Falling, Y", **{COL: 88.5}),
                    _row("Flat, Z", **{COL: 40.0}),
                    _row("Zerobase, Q", **{COL: 0.0})])
        b = _table([_row("Rising, X", **{COL: 49.5}),
                    _row("Falling, Y", **{COL: 54.6}),
                    _row("Flat, Z", **{COL: 40.0}),
                    _row("Zerobase, Q", **{COL: 30.0})])
        self.out = diffing.diff(a, b, COL)

    def test_the_default_order_is_absolute_points_in_both_directions(self):
        """25.0 -> 49.5 is +24.5 points and +98.0%; 88.5 -> 54.6 is -33.9 and
        -38.3%. Neither ordering is wrong, and the absolute one's failure is
        the one the reader can see on screen."""
        order = [r["place"] for r in movers.order_movers(self.out["movers"])]
        self.assertEqual(order,
                         ["Falling, Y", "Zerobase, Q", "Rising, X", "Flat, Z"])

    def test_order_pct_reorders_them(self):
        order = [r["place"]
                 for r in movers.order_movers(self.out["movers"], "pct")]
        self.assertEqual(order[0], "Rising, X")

    def test_both_figures_are_present_in_every_row_whichever_one_sorts(self):
        """Emitting both always is what makes the choice auditable rather than
        a hidden editorial decision."""
        for order in movers.ORDERS:
            for row in movers.order_movers(self.out["movers"], order):
                with self.subTest(order=order, place=row["place"]):
                    self.assertIn("delta", row)
                    self.assertIn("pct", row)

    def test_a_row_with_no_percentage_sinks_rather_than_sorting_as_zero(self):
        """Zerobase moved +30 points, more than anything else, and has no
        percentage. Sorting it as 0% would bury a real mover; sorting it as a
        number it does not have would invent one."""
        order = [r["place"]
                 for r in movers.order_movers(self.out["movers"], "pct")]
        self.assertEqual(order[-1], "Zerobase, Q")
        by_abs = [r["place"] for r in movers.order_movers(self.out["movers"])]
        self.assertEqual(by_abs[0], "Falling, Y")
        self.assertIn("Zerobase, Q", by_abs[:3])

    def test_an_absent_percentage_is_not_a_zero_percentage(self):
        """The sharper half of the test above. A row sorted as 0% lands beside
        the genuinely flat rows, and the alphabetical tie-break then hides the
        difference — so the names have to disagree with the ordering for the
        assertion to be able to see it."""
        a = _table([_row("Azerobase, Q", **{COL: 0.0}),
                    _row("Zflat, X", **{COL: 40.0})])
        b = _table([_row("Azerobase, Q", **{COL: 30.0}),
                    _row("Zflat, X", **{COL: 40.0})])
        out = diffing.diff(a, b, COL)
        order = [r["place"] for r in movers.order_movers(out["movers"], "pct")]
        self.assertEqual(order, ["Zflat, X", "Azerobase, Q"])

    def test_the_direction_filters_describe_the_index_not_the_reader(self):
        rising = movers.order_movers(self.out["movers"], "abs", "rising")
        falling = movers.order_movers(self.out["movers"], "abs", "falling")
        self.assertEqual({r["place"] for r in rising},
                         {"Rising, X", "Zerobase, Q"})
        self.assertEqual({r["place"] for r in falling}, {"Falling, Y"})

    def test_an_unknown_order_or_direction_is_refused(self):
        for kwargs in ({"order": "sideways"}, {"direction": "up"}):
            with self.subTest(**kwargs):
                with self.assertRaises(SourceUnavailable):
                    movers.order_movers(self.out["movers"], **kwargs)

    def test_no_top_means_no_truncation_and_the_counts_say_so(self):
        """A default `--top 20` on a machine format would make a consumer
        record '396 cities compared, 20 moved'."""
        shown = movers.present(self.out, top=None)
        self.assertEqual(shown["shown"], shown["matched_direction"])
        self.assertEqual(len(shown["movers"]), 4)
        self.assertFalse(shown["truncated"])

    def test_no_top_never_truncates_a_panel_larger_than_the_text_default(self):
        """Four movers cannot tell `no limit` from `limit 20`. The panel has to
        be bigger than the default for the assertion to range over anything."""
        a = _table([_row(f"C{i:02d}, X", **{COL: 10.0 + i}) for i in range(30)])
        b = _table([_row(f"C{i:02d}, X", **{COL: 10.0 + i * 2})
                    for i in range(30)])
        shown = movers.present(diffing.diff(a, b, COL), top=None)
        self.assertGreater(shown["matched_direction"], movers.DEFAULT_TOP_TEXT)
        self.assertEqual(shown["shown"], shown["matched_direction"])
        self.assertFalse(shown["truncated"])

    def test_an_explicit_top_truncates_and_declares_all_three_counts(self):
        shown = movers.present(self.out, top=2)
        self.assertEqual((shown["joined"], shown["matched_direction"],
                          shown["shown"]), (4, 4, 2))
        self.assertTrue(shown["truncated"])

    def test_the_direction_count_is_not_the_join_count(self):
        shown = movers.present(self.out, direction="falling", top=None)
        self.assertEqual(shown["joined"], 4)
        self.assertEqual(shown["matched_direction"], 1)
        self.assertEqual(shown["shown"], 1)

    def test_presenting_adds_no_key_the_payload_did_not_already_carry(self):
        """A key that appears only on some paths is its own trap: a consumer
        reading `shown` would get a KeyError on exactly the runs that refused.

        The set difference IS the assertion, rather than a hand-listed subset
        of the keys — the previous version named three of them and the one that
        was missing was the fourth. A gate whose subject list is retyped can
        only ever grade what somebody remembered."""
        presented = movers.present(self.out, top=1)
        self.assertGreaterEqual(len(presented), 20,
                                f"graded {len(presented)} payload keys")
        self.assertEqual(set(presented) - set(self.out), set(),
                         "these keys exist only after present() ran")

    def test_the_keys_present_fills_are_declared_and_seeded_null(self):
        for key in movers.PRESENTATION_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, self.out)
                self.assertIsNone(self.out[key])
        self.assertGreaterEqual(len(movers.PRESENTATION_KEYS), 3,
                                f"graded {len(movers.PRESENTATION_KEYS)} keys")

    def test_present_does_not_mutate_the_payload_it_was_given(self):
        before = len(self.out["movers"])
        movers.present(self.out, top=1)
        self.assertEqual(len(self.out["movers"]), before)
        self.assertIsNone(self.out["shown"])


class MaxAgeZeroForcesALiveReadOfBothSides(unittest.TestCase):
    """If the archive side ignores `--max-age 0`, the flag documented as
    'forces live' silently means 'forces half of it live', and a user checking
    whether a seven-day ban has lifted reads a green run off disk."""

    PUBLISHED = ["2026-mid", "2019", "2014"]

    def test_pin_eligibility_has_exactly_one_owner(self):
        """`movers` computes no TTL of its own.

        It used to: `side_max_age`/`is_archive_id` reused a table for 30 days
        when its id was "not the newest published". `rankings.is_archival` says
        "its named year has ended plus a publication cycle". Two owners for one
        fact, and they disagreed — 2026 is not the newest published id, so the
        table for the first half of the year we are IN was an archive to one of
        them and not to the other. The rule kept is the conservative one, and
        this asserts the other is gone rather than merely unused.

        Ranged over all three modules this command is now split across, not
        just the one holding `compare_snapshots`. A gate whose name is general
        and whose selector is one file passes the day the thing it forbids is
        moved into a sibling.
        """
        graded = (movers, comparable, diffing)
        for module in graded:
            for gone in ("side_max_age", "is_archive_id", "ARCHIVE_MAX_AGE"):
                with self.subTest(module=module.__name__, name=gone):
                    self.assertFalse(hasattr(module, gone))
        self.assertEqual(len(graded), 3, f"graded {len(graded)} modules")

    def test_no_prose_about_a_per_side_ttl_outlived_the_helpers(self):
        """The helpers went and four lines of `#:` prose about a published
        snapshot being reusable "far longer than `current`'s" stayed, directly
        above `DISTINCT_DELTA_FLOOR` — so the next reader concludes that
        constant is a cache TTL and tunes a drift threshold to change caching.
        The code was right; the paragraph above it described a rule this module
        no longer owns.

        `DISTINCT_DELTA_FLOOR` now lives in `diffing`, so a gate that still
        read only `movers.py` would grade the file the prose is no longer next
        to. All three modules, and the count is printed so a fourth added later
        is either covered or visibly absent."""
        import pathlib
        graded = (movers, comparable, diffing)
        for module in graded:
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for orphan in (
                    "reused far longer", "CACHE_RETENTION",
                    "Subordinate, always, to an explicit `--max-age 0`"):
                with self.subTest(module=module.__name__, orphan=orphan):
                    # `assertFalse`, not `assertNotIn`: the container here is
                    # the whole module, and a failure that prints 25KB of
                    # source buries the one phrase the reader has to find.
                    self.assertFalse(orphan in source,
                                     f"orphaned per-side TTL prose survives "
                                     f"in {module.__name__}: {orphan!r}")
        self.assertEqual(len(graded), 3, f"graded {len(graded)} modules")

    def test_the_surviving_owner_still_refuses_the_unfinished_ids(self):
        """The property the removed helpers existed for, asserted against the
        owner that kept it. 2026-mid was measured at 547 rows against
        `current`'s 558, which is equally consistent with 'frozen' and 'still
        filling'."""
        now = dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc).timestamp()
        self.assertTrue(rankings.is_archival("2019", now=now))
        self.assertFalse(rankings.is_archival("2026-mid", now=now))
        self.assertFalse(rankings.is_archival("current", now=now))

    def test_the_live_read_counter_confirms_both_sides_went_out(self):
        """Asserted through the real cache layer rather than on the argument,
        because `net.reads()['live']` is what main()'s `--fetch-mode` warning
        depends on, and a mock at `rankings.fetch` would hide the whole
        question."""
        net.clear_cache(include_archive=True)
        old_html = _html([("Aville, Xland", (10.0, 1.0))])
        new_html = _html([("Aville, Xland", (20.0, 2.0)),
                          ("Bville, Yland", (30.0, 3.0))])

        def fake_get(url, **kw):
            return old_html if "title=2019" in url else new_html

        with mock.patch.object(rankings, "snapshots",
                               return_value=list(self.PUBLISHED)), \
             mock.patch.object(rankings, "http_get", side_effect=fake_get):
            net.reset_reads()
            movers.compare_snapshots(frm="2019", to="current", max_age=3600)
            self.assertEqual(net.reads()["live"], 2, "first run must be live")

            net.reset_reads()
            movers.compare_snapshots(frm="2019", to="current", max_age=3600)
            self.assertEqual(net.reads()["live"], 0, "second run is cached")

            net.reset_reads()
            movers.compare_snapshots(frm="2019", to="current", max_age=0)
            self.assertEqual(net.reads()["live"], 2,
                             "--max-age 0 must reach the archive side too")
        net.clear_cache(include_archive=True)


class MoversRendersNoVerdict(unittest.TestCase):
    """This repository has already shipped one 'lower is better' rule applied
    to every column and marked the worst-paying city as best. Here the sign's
    desirability depends on the column AND on the reader: a rent index rising
    is bad for a renter and good for a landlord, a crime index falling is good,
    a salary index falling is bad, and `Gross Rental Yield` has no single
    answer."""

    def test_the_module_does_not_consume_the_higher_is_better_set(self):
        """All three modules the command is split across, because the rule is
        about the command and not about one file. A gate named for the whole
        and pointed at one part passes the day somebody moves the offending
        line into a sibling."""
        import pathlib
        graded = (movers, comparable, diffing)
        for module in graded:
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for banned in ("HIGHER_IS_BETTER", "mark_best", "import budget",
                           "from .budget", "from . import budget"):
                with self.subTest(module=module.__name__, banned=banned):
                    self.assertNotIn(banned, source.replace(
                        "budget.py owns", "").replace(
                            "HIGHER_IS_BETTER` verdicts", ""))
        self.assertEqual(len(graded), 3, f"graded {len(graded)} modules")

    def test_no_row_carries_a_better_or_worse_field(self):
        a = _table([_row("A, X", **{COL: 10.0})])
        b = _table([_row("A, X", **{COL: 20.0})])
        for row in diffing.diff(a, b, COL)["rows"]:
            for key in row:
                self.assertNotIn("best", key)
                self.assertNotIn("better", key)
                self.assertNotIn("worse", key)

    def test_the_basis_caveat_travels_in_the_payload_not_only_in_prose(self):
        """A caveat only in prose is a caveat a machine consumer never
        receives, and without it the output reads as 'Medellin got 61% more
        expensive' — a claim about prices this data does not make."""
        a = _table([_row("A, X", **{COL: 10.0})])
        b = _table([_row("A, X", **{COL: 20.0})])
        basis = diffing.diff(a, b, COL)["basis"]
        self.assertIn("rebased", basis)
        self.assertIn("not necessarily in absolute price", basis)


if __name__ == "__main__":
    unittest.main(verbosity=2)
