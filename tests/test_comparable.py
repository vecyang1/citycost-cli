"""`comparable` must refuse before it can be wrong, and only when it should.

Everything graded here fires BEFORE a delta exists: a snapshot id checked
against the published list, two ids that resolve to one URL, a column label
that has to mean the same quantity in both headers, a row key that has to
identify one city in each table, and the fingerprint that catches one table
fetched twice. Each test comes in two directions — the degenerate input that
must be caught and the healthy input that must not be — because a guard that
cannot fire and a guard that always fires read identically from outside, and
the second gets muted within a week.

Two of these classes reach for `diffing.diff` rather than for `index_by_key`
directly. That is deliberate: a join that over-matches is not visible in the
index, it is visible as a *delta between two different cities*, and the
assertion has to be able to see the fabricated number.

Offline by construction. Nothing here touches numbeo.com: the transport is
either mocked or, where the property under test is "was this called at all",
replaced by a trap that raises at the moment the wrong call happens.
"""

import re
import shlex
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401  (must be first)
from ._tables import COL, RENT, _row, _table
from citycost import comparable, diffing, movers, rankings
from citycost.errors import CitycostError, LayoutChanged, SourceUnavailable
from citycost.parser import build_parser


class SnapshotIdsAreValidatedBeforeAnythingIsFetched(unittest.TestCase):
    """Numbeo serves an unrecognised `?title=` as the current table.

    So a typo'd `--from 2109` produces two identical tables and fires the
    upstream-drift alarm, sending the user to diagnose a source that is
    behaving correctly — while spending two requests against an address the
    source bans for seven days at a time.
    """

    PUBLISHED = ["2026-mid", "2026", "2025", "2019", "2014"]

    def _snaps(self):
        return mock.patch.object(rankings, "snapshots",
                                 return_value=list(self.PUBLISHED))

    def _never_fetch(self):
        # A stub returning a plausible table would hide *whether* the fetch
        # happened, which is the entire property under test.
        return mock.patch.object(
            rankings, "fetch",
            side_effect=AssertionError("a table was fetched before the "
                                       "snapshot ids were validated"))

    def test_an_unknown_from_id_is_refused_and_costs_no_request(self):
        with self._snaps(), self._never_fetch():
            with self.assertRaises(SourceUnavailable) as ctx:
                movers.compare_snapshots(frm="2109", to="current")
        self.assertIn("2109", ctx.exception.message)
        self.assertIn("cost-of-living", ctx.exception.message)
        self.assertIn("2019", ctx.exception.remedy)

    def test_an_unknown_to_id_is_refused_the_same_way(self):
        with self._snaps(), self._never_fetch():
            with self.assertRaises(SourceUnavailable) as ctx:
                movers.compare_snapshots(frm="2019", to="2027")
        self.assertIn("--to", ctx.exception.message)
        self.assertIn("2027", ctx.exception.message)

    def test_current_is_the_one_accepted_id_that_is_not_in_the_list(self):
        with self._snaps():
            res = comparable.resolve_snapshots("cost-of-living", "2019",
                                               "current")
        self.assertEqual((res["from"], res["to"]), ("2019", "current"))

    def test_every_published_id_is_accepted(self):
        """The healthy direction. A validator that refused a real id would be
        discovered by the user, not by this suite."""
        with self._snaps():
            for sid in self.PUBLISHED:
                with self.subTest(sid=sid):
                    res = comparable.resolve_snapshots("cost-of-living", sid,
                                                   "current")
                    self.assertEqual(res["from"], sid)

    def test_a_missing_from_is_refused_rather_than_defaulted(self):
        """A defaulted base is a base the reader did not choose and will not
        check — and every sign in the table depends on it."""
        with self._snaps(), self._never_fetch():
            with self.assertRaises(SourceUnavailable) as ctx:
                comparable.resolve_snapshots("cost-of-living", "")
        self.assertIn("--from", ctx.exception.message)

    def test_an_empty_snapshot_list_gets_its_own_sentence(self):
        """'this vertical publishes no history' and 'you typed an id that does
        not exist' have different fixes, so they must not share a message."""
        with mock.patch.object(rankings, "snapshots", return_value=[]), \
             self._never_fetch():
            with self.assertRaises(SourceUnavailable) as ctx:
                comparable.resolve_snapshots("cost-of-living", "2019")
        self.assertIn("no snapshot list", ctx.exception.message)
        self.assertNotIn("not a published snapshot", ctx.exception.message)

    def test_newest_and_oldest_come_from_the_ids_not_from_page_order(self):
        with mock.patch.object(rankings, "snapshots",
                               return_value=["2019", "2026-mid", "2026",
                                             "2014"]):
            res = comparable.resolve_snapshots("cost-of-living", "2019")
        self.assertEqual(res["newest"], "2026-mid")
        self.assertEqual(res["oldest"], "2014")


class TwoIdsThatResolveToOneUrlAreRefusedBeforeSpendingTwoRequests(
        unittest.TestCase):
    """A zero-delta answer produced by asking one question twice, which would
    otherwise be indistinguishable from upstream drift. A refusal that costs no
    request beats an alarm that costs two."""

    def _snaps(self):
        return mock.patch.object(rankings, "snapshots",
                                 return_value=["2026-mid", "2019"])

    def _never_fetch(self):
        return mock.patch.object(
            rankings, "fetch",
            side_effect=AssertionError("fetched despite one URL for two ids"))

    def test_current_against_current(self):
        with self._snaps(), self._never_fetch():
            with self.assertRaises(SourceUnavailable) as ctx:
                movers.compare_snapshots(frm="current", to="current")
        self.assertIn("same URL", ctx.exception.message)
        self.assertIn("rankings_current.jsp", ctx.exception.message)

    def test_the_same_archive_id_on_both_sides(self):
        with self._snaps(), self._never_fetch():
            with self.assertRaises(SourceUnavailable) as ctx:
                movers.compare_snapshots(frm="2019", to="2019")
        self.assertIn("title=2019", ctx.exception.message)

    def test_the_remedy_names_a_base_by_period_not_by_page_order(self):
        """`published[-1]` is the last element of a list whose order is the
        `<select>`'s. This module has `_snapshot_sort_key` precisely because
        that order is not guaranteed, so on an oldest-first page the sentence
        offering a BASE hands the reader the newest id there is."""
        with mock.patch.object(rankings, "snapshots",
                               return_value=["2014", "2019", "2026-mid"]):
            with self.assertRaises(SourceUnavailable) as ctx:
                comparable.resolve_snapshots("cost-of-living", "current",
                                             "current")
        self.assertIn("--from 2014", ctx.exception.remedy)
        self.assertNotIn("2026-mid", ctx.exception.remedy)

    def test_the_pair_the_remedy_offers_actually_resolves(self):
        """The decidable half of 'is this remedy any good': a suggestion that
        collides again is the same refusal wearing a suggestion."""
        with mock.patch.object(rankings, "snapshots",
                               return_value=["2014", "2019", "2026-mid"]):
            with self.assertRaises(SourceUnavailable) as ctx:
                comparable.resolve_snapshots("cost-of-living", "2014", "2014")
            pair = re.search(r"--from (\S+) --to ([\w-]+)",
                             ctx.exception.remedy)
            self.assertIsNotNone(pair, ctx.exception.remedy)
            res = comparable.resolve_snapshots("cost-of-living", pair.group(1),
                                           pair.group(2))
        self.assertNotEqual(res["url_from"], res["url_to"])

    def test_two_different_ids_are_not_refused(self):
        with self._snaps():
            res = comparable.resolve_snapshots("cost-of-living", "2019",
                                               "current")
        self.assertNotEqual(res["url_from"], res["url_to"])


class ColumnResolution(unittest.TestCase):
    """`rank --sort` takes the first substring hit because it sorts ONE table,
    where a wrong-but-single column is visibly labelled in the output. `movers`
    joins two, where taking the first hit in each header independently
    subtracts `Rent Index` from `Cost of Living Plus Rent Index` and renders
    the result exactly like a number that means something."""

    PLUS = "Cost of Living Plus Rent Index"

    def test_the_default_is_the_first_shared_column_in_the_to_header(self):
        a = _table([_row("X, Y", **{RENT: 1.0, COL: 2.0})], columns=[RENT, COL])
        b = _table([_row("X, Y", **{COL: 3.0, RENT: 4.0})], columns=[COL, RENT])
        self.assertEqual(comparable.resolve_column(a, b, None), COL)

    def test_a_column_only_the_older_table_has_is_never_the_default(self):
        a = _table([_row("X, Y", **{"Gone Index": 1.0, COL: 2.0})],
                   columns=["Gone Index", COL])
        b = _table([_row("X, Y", **{COL: 3.0})], columns=[COL])
        self.assertEqual(comparable.resolve_column(a, b, None), COL)

    def test_the_default_is_never_just_the_to_headers_first_column(self):
        """`to_cols[0]` and `shared[0]` are the same string on every table
        whose headers happen to line up, so the fixture has to disagree with
        itself or the intersection is never actually exercised."""
        a = _table([_row("X, Y", **{COL: 2.0})], columns=[COL])
        b = _table([_row("X, Y", **{"New Index": 1.0, COL: 3.0})],
                   columns=["New Index", COL])
        self.assertEqual(comparable.resolve_column(a, b, None), COL)

    def test_no_shared_column_at_all_is_a_layout_change_not_a_user_error(self):
        a = _table([_row("X, Y", **{"Old Index": 1.0})], columns=["Old Index"])
        b = _table([_row("X, Y", **{"New Index": 2.0})], columns=["New Index"])
        with self.assertRaises(LayoutChanged) as ctx:
            comparable.resolve_column(a, b, None)
        self.assertIn("Old Index", str(ctx.exception))
        self.assertIn("New Index", str(ctx.exception))

    def test_a_substring_matching_two_headers_in_one_table_is_refused(self):
        a = _table([_row("X, Y", **{RENT: 1.0, self.PLUS: 2.0})],
                   columns=[RENT, self.PLUS])
        b = _table([_row("X, Y", **{RENT: 3.0, self.PLUS: 4.0})],
                   columns=[RENT, self.PLUS])
        with self.assertRaises(SourceUnavailable) as ctx:
            comparable.resolve_column(a, b, "rent")
        self.assertIn(RENT, str(ctx.exception))
        self.assertIn(self.PLUS, str(ctx.exception))

    def test_a_substring_resolving_to_two_different_labels_is_refused(self):
        """The measured shape: the two headers each yield exactly one hit, and
        they are different quantities. Nothing downstream could notice."""
        a = _table([_row("X, Y", **{self.PLUS: 2.0})], columns=[self.PLUS])
        b = _table([_row("X, Y", **{RENT: 3.0})], columns=[RENT])
        with self.assertRaises(SourceUnavailable) as ctx:
            comparable.resolve_column(a, b, "rent")
        self.assertIn("unrelated series", str(ctx.exception))

    def test_a_column_that_vanished_from_the_newer_table_is_a_layout_change(self):
        a = _table([_row("X, Y", **{COL: 1.0, "Gone Index": 2.0})],
                   columns=[COL, "Gone Index"])
        b = _table([_row("X, Y", **{COL: 3.0})], columns=[COL])
        with self.assertRaises(LayoutChanged) as ctx:
            comparable.resolve_column(a, b, "Gone Index")
        self.assertIn("needs updating", ctx.exception.remedy)

    def test_a_column_only_the_newer_table_has_is_the_callers_error(self):
        """Diffing it would report N/A for all 396 cities — a page of absences
        that reads as 'Numbeo has no data for any of these'."""
        a = _table([_row("X, Y", **{COL: 1.0})], columns=[COL])
        b = _table([_row("X, Y", **{COL: 3.0, "New Index": 4.0})],
                   columns=[COL, "New Index"])
        with self.assertRaises(SourceUnavailable) as ctx:
            comparable.resolve_column(a, b, "New Index")
        self.assertIn(COL, ctx.exception.remedy)

    def test_a_column_in_neither_header_lists_both_headers(self):
        a = _table([_row("X, Y", **{COL: 1.0})], columns=[COL])
        b = _table([_row("X, Y", **{COL: 3.0})], columns=[COL])
        with self.assertRaises(SourceUnavailable) as ctx:
            comparable.resolve_column(a, b, "Pollution")
        self.assertIn("no header in either", ctx.exception.message)

    def test_the_full_label_wins_over_the_longer_header_containing_it(self):
        """The healthy direction: a user who typed the complete label has
        already disambiguated, and refusing them would be a refusal
        manufactured by the matcher."""
        cols = [self.PLUS, RENT]
        a = _table([_row("X, Y", **{self.PLUS: 1.0, RENT: 2.0})], columns=cols)
        b = _table([_row("X, Y", **{self.PLUS: 3.0, RENT: 4.0})], columns=cols)
        self.assertEqual(comparable.resolve_column(a, b, "Rent Index"), RENT)
        self.assertEqual(comparable.resolve_column(a, b, "rent index"), RENT)

    def test_resolution_never_falls_back_to_column_position(self):
        a = _table([_row("X, Y", **{"A": 1.0, COL: 2.0})], columns=["A", COL])
        b = _table([_row("X, Y", **{COL: 3.0, "B": 4.0})], columns=[COL, "B"])
        self.assertEqual(comparable.resolve_column(a, b, None), COL)
        with self.assertRaises(LayoutChanged):
            comparable.resolve_column(a, b, "A")


class TheJoinIsExactAndNeverGuesses(unittest.TestCase):
    """`_norm` collapses only what is notation and nothing that is identity.

    A join that guesses produces a *delta* between two different cities — a
    fabricated number — where a join that abstains produces a pair of visible
    absences.
    """

    def test_the_two_vancouvers_never_become_one_mover(self):
        a = _table([_row("Vancouver, Canada", **{COL: 70.0})])
        b = _table([_row("Vancouver, WA, United States", **{COL: 95.0})])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["only_in_from"], ["Vancouver, Canada"])
        self.assertEqual(out["only_in_to"], ["Vancouver, WA, United States"])

    def test_nang_is_never_paired_with_penang(self):
        a = _table([_row("Da Nang, Vietnam", **{COL: 40.0})])
        b = _table([_row("Penang, Malaysia", **{COL: 33.0})])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["joined"], 0)
        self.assertEqual(out["counts"]["moved"], 0)

    def test_two_portlands_stay_two_cities(self):
        a = _table([_row("Portland, OR, United States", **{COL: 80.0}),
                    _row("Portland, ME, United States", **{COL: 75.0})])
        b = _table([_row("Portland, OR, United States", **{COL: 82.0}),
                    _row("Portland, ME, United States", **{COL: 90.0})])
        out = diffing.diff(a, b, COL)
        deltas = {r["place"]: r["delta"] for r in out["movers"]}
        self.assertEqual(deltas["Portland, OR, United States"], 2.0)
        self.assertEqual(deltas["Portland, ME, United States"], 15.0)

    def test_a_rename_presents_as_a_pair_of_absences_and_is_not_repaired(self):
        """Kiev -> Kyiv. The failure direction is the safe one: the pair is
        visible, reported, and adjacent in the two lists where a human spots
        it. Fuzzy repair risks pairing two genuinely different cities."""
        a = _table([_row("Kiev, Ukraine", **{COL: 40.0})])
        b = _table([_row("Kyiv, Ukraine", **{COL: 44.0})])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["only_in_from"], ["Kiev, Ukraine"])
        self.assertEqual(out["only_in_to"], ["Kyiv, Ukraine"])

    def test_a_country_rename_also_presents_as_absences(self):
        a = _table([_row("Istanbul, Turkey", **{COL: 40.0})])
        b = _table([_row("Istanbul, Türkiye", **{COL: 44.0})])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["joined"], 0)

    def test_a_subdivision_appearing_is_a_rename_not_a_match(self):
        a = _table([_row("Vancouver, Canada", **{COL: 70.0})])
        b = _table([_row("Vancouver, BC, Canada", **{COL: 72.0})])
        self.assertEqual(diffing.diff(a, b, COL)["joined"], 0)

    def test_notation_only_differences_do_join(self):
        """The other half, and the reason `_norm` is the right normaliser
        rather than raw string equality."""
        cases = [("São Paulo, Brazil", "Sao Paulo, Brazil"),
                 ("St. John's, Newfoundland and Labrador, Canada",
                  "St Johns, Newfoundland and Labrador, Canada"),
                 ("Xi'an, China", "Xian, China")]
        for old, new in cases:
            with self.subTest(old=old):
                a = _table([_row(old, **{COL: 40.0})])
                b = _table([_row(new, **{COL: 44.0})])
                out = diffing.diff(a, b, COL)
                self.assertEqual(out["joined"], 1, out["only_in_from"])
                self.assertEqual(out["movers"][0]["delta"], 4.0)

    def test_join_key_has_exactly_one_owner(self):
        row = _row("São Paulo, Brazil", **{COL: 1.0})
        self.assertEqual(comparable.join_key(row),
                         rankings._norm(row["place"]))


class DuplicateKeysAreExcludedNotSilentlyDeduplicated(unittest.TestCase):
    """`{key: row for row in rows}` is last-wins, and page order is measured to
    be unstable across snapshots. The delta would then be computed between two
    different cities, and *which* two would change between runs."""

    DUPES = [_row("St. Petersburg, Russia", **{COL: 40.0}),
             _row("St Petersburg, Russia", **{COL: 60.0})]

    def test_a_colliding_pair_is_excluded_from_the_index(self):
        index, excluded = comparable.index_by_key(_table(self.DUPES))
        self.assertEqual(index, {})
        self.assertEqual(len(excluded), 1)
        self.assertEqual(sorted(excluded[0]["labels"]),
                         ["St Petersburg, Russia", "St. Petersburg, Russia"])

    def test_neither_of_them_becomes_a_mover(self):
        a = _table(self.DUPES)
        b = _table([_row("St. Petersburg, Russia", **{COL: 50.0})])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["counts"]["duplicate_key"], 2)
        self.assertEqual(out["duplicate_keys"][0]["side"], "from")

    def test_the_result_does_not_depend_on_page_order(self):
        """The whole point. Last-wins gives 10.0 one way and -10.0 the other,
        and page order is not stable between snapshots."""
        b = _table([_row("St. Petersburg, Russia", **{COL: 50.0})])
        forward = diffing.diff(_table(self.DUPES), b, COL)
        backward = diffing.diff(_table(list(reversed(self.DUPES))), b, COL)
        self.assertEqual(forward["counts"], backward["counts"])
        self.assertEqual(forward["movers"], backward["movers"])

    def test_an_excluded_row_is_reported_rather_than_dropped(self):
        a = _table(self.DUPES)
        b = _table([_row("Somewhere, Else", **{COL: 50.0})])
        out = diffing.diff(a, b, COL)
        places = [r["place"] for r in out["rows"]]
        self.assertIn("St. Petersburg, Russia", places)
        self.assertIn("St Petersburg, Russia", places)
        # Not `new` and not `delisted`: they were listed, twice.
        self.assertNotIn("St. Petersburg, Russia", out["only_in_from"])

    def test_a_label_that_normalises_to_nothing_cannot_join_another(self):
        """Two such rows would otherwise join to each other on the empty key,
        which is the over-matching failure with no name attached."""
        a = _table([_row("???", **{COL: 10.0})])
        b = _table([_row("!!!", **{COL: 99.0})])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["counts"]["unkeyable_label"], 2)


class TheWholeTableFingerprintGuard(unittest.TestCase):
    """If `?title=` stops being honoured upstream, both fetches return the
    current table, all ~550 cities join, and every delta is exactly 0.0 — a
    complete, plausible 'nothing moved in seven years' with no error
    anywhere."""

    T = _table([_row("A, X", **{COL: 1.0}), _row("B, Y", **{COL: 2.0})])

    def test_one_table_twice_is_caught(self):
        self.assertTrue(comparable.same_panel(self.T, self.T))

    def test_two_real_tables_are_not_caught(self):
        other = _table([_row("A, X", **{COL: 1.0}), _row("B, Y", **{COL: 9.9})])
        self.assertFalse(comparable.same_panel(self.T, other))

    def test_a_difference_in_any_column_is_enough(self):
        """The fingerprint is taken before column selection, so a guard cannot
        be narrowed by which column the caller happens to be diffing."""
        a = _table([_row("A, X", **{COL: 1.0, RENT: 5.0})])
        b = _table([_row("A, X", **{COL: 1.0, RENT: 6.0})])
        self.assertFalse(comparable.same_panel(a, b))

    def test_a_different_row_count_alone_is_enough(self):
        smaller = _table([_row("A, X", **{COL: 1.0})])
        self.assertFalse(comparable.same_panel(self.T, smaller))

    def test_two_empty_tables_are_not_this_failure(self):
        """They fingerprint identically because there is nothing to
        fingerprint. Naming that 'the snapshot parameter is ignored' sends the
        reader to the wrong subsystem; an empty panel has its own refusal."""
        empty = _table([], columns=[COL])
        self.assertFalse(comparable.same_panel(empty, empty))

    def test_it_delegates_rather_than_reimplementing(self):
        """Second member of a family whose first is
        `rankings._is_one_table_repeated`, and the family lesson is that a fix
        goes to one owner while the defect stays in the sibling. `create=True`
        is deliberately NOT passed: the owner must really exist, or this would
        pass against a module that has no such function.
        """
        other = _table([_row("A, X", **{COL: 9.9})])
        with mock.patch.object(rankings, "same_panel",
                               return_value=True) as delegate:
            self.assertTrue(comparable.same_panel(self.T, other))
        delegate.assert_called_once()

    def test_the_owner_itself_refuses_two_empty_tables(self):
        """Asserted on `rankings`, not on `movers`, because that is where the
        precondition now lives — and a test aimed at the wrapper would keep
        passing if the wrapper grew its own copy of the rule."""
        empty = _table([], columns=[COL])
        self.assertFalse(rankings.same_panel(empty, empty))


class EveryMultiSnapshotCommandRefusesARepeatedTable(unittest.TestCase):
    """The family rule made decidable.

    The `?title=` guard now has two implementations. A third multi-snapshot
    command added later will either register here or be visibly absent, instead
    of shipping as the one command with no refusal. Printing the count is what
    stops the selector silently narrowing to one.
    """

    T = _table([_row("A, X", **{COL: 1.0}), _row("B, Y", **{COL: 2.0})])
    OTHER = _table([_row("A, X", **{COL: 1.0})])

    def _point(self, snapshot, *, of, value):
        return {"snapshot": snapshot, "found": True, "place": "A, X",
                "rank": 1, "of": of, "metrics": {COL: value}}

    def _guards(self):
        """command -> (fires on one table repeated, silent on real history)."""
        return {
            "trend": (
                lambda: rankings._is_one_table_repeated(
                    [self._point(s, of=547, value=38.2)
                     for s in ("2026-mid", "2025", "2024")]),
                lambda: rankings._is_one_table_repeated(
                    [self._point("2026-mid", of=547, value=38.2),
                     self._point("2025", of=561, value=36.9),
                     self._point("2024", of=578, value=35.1)])),
            "movers": (
                lambda: comparable.same_panel(self.T, self.T),
                lambda: comparable.same_panel(self.T, self.OTHER)),
        }

    def test_each_registered_command_flags_one_table_fetched_twice(self):
        guards = self._guards()
        for name, (degenerate, _) in guards.items():
            with self.subTest(command=name):
                self.assertTrue(degenerate(), f"{name} accepted a repeated "
                                              f"table as history")
        self.assertGreaterEqual(len(guards), 2,
                                f"graded only {len(guards)} commands")

    def test_each_registered_command_stays_silent_on_real_history(self):
        """The half that rots. A predicate that fired on real data would be
        muted within a week."""
        guards = self._guards()
        for name, (_, healthy) in guards.items():
            with self.subTest(command=name):
                self.assertFalse(healthy(), f"{name} flagged real history")
        self.assertGreaterEqual(len(guards), 2,
                                f"graded only {len(guards)} commands")

    def test_movers_does_not_reuse_the_trend_predicate(self):
        """`_is_one_table_repeated` takes a series of per-city points, returns
        False below three found points by design, and reads `of` as its size
        discriminator. `movers` has exactly two tables."""
        self.assertFalse(rankings._is_one_table_repeated(
            [self._point("a", of=1, value=1.0),
             self._point("b", of=1, value=1.0)]))
        self.assertTrue(comparable.same_panel(self.T, self.T))


class EveryRemedyNamesACommandThatParses(unittest.TestCase):
    """The one decidable slice of 'is this error message any good'.

    Error text is the interface nothing asserts on: the handler is correct, the
    exit code is correct, the suite is green, and the sentence hands the reader
    a command that exits immediately.
    """

    def _refusals(self):
        out = []
        published = ["2026-mid", "2019", "2014"]
        with mock.patch.object(rankings, "snapshots", return_value=published):
            for kwargs in ({"frm": "2109"}, {"frm": "2019", "to": "nope"},
                           {"frm": "current", "to": "current"},
                           {"frm": "2019", "to": "2019"}, {"frm": ""}):
                with self.assertRaises(CitycostError) as ctx:
                    comparable.resolve_snapshots("cost-of-living", **kwargs)
                out.append(ctx.exception)
        with mock.patch.object(rankings, "snapshots", return_value=[]):
            with self.assertRaises(CitycostError) as ctx:
                comparable.resolve_snapshots("cost-of-living", "2019")
            out.append(ctx.exception)

        a = _table([_row("A, X", **{COL: 1.0, "Gone Index": 2.0})],
                   columns=[COL, "Gone Index"])
        b = _table([_row("A, X", **{COL: 3.0, "New Index": 4.0})],
                   columns=[COL, "New Index"])
        for requested in ("Gone Index", "New Index", "Pollution"):
            with self.assertRaises(CitycostError) as ctx:
                comparable.resolve_column(a, b, requested)
            out.append(ctx.exception)
        return out

    def test_every_command_in_every_refusal_parses(self):
        parser = build_parser()
        graded = []
        for exc in self._refusals():
            for command in re.findall(r"`(citycost [^`]+)`", str(exc)):
                graded.append(command)
                with self.subTest(command=command):
                    argv = shlex.split(command)[1:]
                    try:
                        parser.parse_args(argv)
                    except SystemExit:
                        self.fail(f"a remedy names a command that cannot run: "
                                  f"{command}")
        # The denominator, counted DISTINCTLY. `>= 5` occurrences was met by
        # one sentence repeated: eight hits of three strings, so the gate read
        # as "five remedies graded" while two of the three were one line copied
        # across refusals. Occurrences measure how often a remedy is reused;
        # only the distinct set measures how much of this module's error
        # surface has actually been through the parser.
        distinct = set(graded)
        self.assertGreaterEqual(
            len(distinct), 3,
            f"graded {len(distinct)} distinct commands over "
            f"{len(graded)} occurrences: {sorted(distinct)}")

    def test_every_refusal_carries_a_remedy(self):
        refusals = self._refusals()
        self.assertGreaterEqual(len(refusals), 9)
        for exc in refusals:
            with self.subTest(message=exc.message[:50]):
                self.assertTrue(exc.remedy.strip(), exc.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
