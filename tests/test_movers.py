"""`movers` must be able to prove it diffed two different tables.

Every failure this module guards produces a *complete, well-formed, plausible*
answer rather than an error, so each test below comes in two directions: the
degenerate input that must be caught, and the healthy input that must not be —
because a guard that cannot fire and a guard that always fires read identically
from outside, and the second gets muted within a week.

Offline by construction. Nothing here touches numbeo.com: the transport is
either mocked or, where the property under test is "was this called at all",
replaced by a trap that raises at the moment the wrong call happens.
"""

import datetime as dt
import re
import shlex
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401  (must be first)
from citycost import movers, net, rankings
from citycost.errors import CitycostError, LayoutChanged, SourceUnavailable
from citycost.parser import build_parser

COL = "Cost of Living Index"
RENT = "Rent Index"


def _row(place, **metrics):
    rec = rankings.split_place(place)
    rec["rank"] = 0
    rec["metrics"] = dict(metrics)
    return rec


def _table(rows, columns=None, url="u"):
    ranked = []
    for i, r in enumerate(rows, start=1):
        rr = dict(r)
        rr["rank"] = i
        ranked.append(rr)
    cols = columns if columns is not None else (
        list(rows[0]["metrics"]) if rows else [])
    return {"url": url, "columns": list(cols), "rows": ranked}


def _html(rows, columns=(COL, RENT)):
    """A real ranking page, built the way Numbeo builds one.

    Including the EMPTY first `<td>` — the rank cell Numbeo fills with
    DataTables JS. A fixture written from what the response *means* rather than
    from what it *is* tests the parser against its author's mental model.
    """
    head = "".join(f"<th><div>{c}</div></th>" for c in columns)
    body = "".join(
        "<tr><td></td>"
        f'<td class="cityOrCountryInIndicesTable">{place}</td>'
        + "".join(f'<td style="text-align: right">{v}</td>' for v in values)
        + "</tr>"
        for place, values in rows)
    return (f'<table id="t2" class="stripe"><thead><tr>'
            f'<th><div>Rank</div></th><th><div>City</div></th>{head}'
            f"</tr></thead><tbody>{body}</tbody></table>")


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
            res = movers.resolve_snapshots("cost-of-living", "2019", "current")
        self.assertEqual((res["from"], res["to"]), ("2019", "current"))

    def test_every_published_id_is_accepted(self):
        """The healthy direction. A validator that refused a real id would be
        discovered by the user, not by this suite."""
        with self._snaps():
            for sid in self.PUBLISHED:
                with self.subTest(sid=sid):
                    res = movers.resolve_snapshots("cost-of-living", sid,
                                                   "current")
                    self.assertEqual(res["from"], sid)

    def test_a_missing_from_is_refused_rather_than_defaulted(self):
        """A defaulted base is a base the reader did not choose and will not
        check — and every sign in the table depends on it."""
        with self._snaps(), self._never_fetch():
            with self.assertRaises(SourceUnavailable) as ctx:
                movers.resolve_snapshots("cost-of-living", "")
        self.assertIn("--from", ctx.exception.message)

    def test_an_empty_snapshot_list_gets_its_own_sentence(self):
        """'this vertical publishes no history' and 'you typed an id that does
        not exist' have different fixes, so they must not share a message."""
        with mock.patch.object(rankings, "snapshots", return_value=[]), \
             self._never_fetch():
            with self.assertRaises(SourceUnavailable) as ctx:
                movers.resolve_snapshots("cost-of-living", "2019")
        self.assertIn("no snapshot list", ctx.exception.message)
        self.assertNotIn("not a published snapshot", ctx.exception.message)

    def test_newest_and_oldest_come_from_the_ids_not_from_page_order(self):
        with mock.patch.object(rankings, "snapshots",
                               return_value=["2019", "2026-mid", "2026",
                                             "2014"]):
            res = movers.resolve_snapshots("cost-of-living", "2019")
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

    def test_two_different_ids_are_not_refused(self):
        with self._snaps():
            res = movers.resolve_snapshots("cost-of-living", "2019", "current")
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
        self.assertEqual(movers.resolve_column(a, b, None), COL)

    def test_a_column_only_the_older_table_has_is_never_the_default(self):
        a = _table([_row("X, Y", **{"Gone Index": 1.0, COL: 2.0})],
                   columns=["Gone Index", COL])
        b = _table([_row("X, Y", **{COL: 3.0})], columns=[COL])
        self.assertEqual(movers.resolve_column(a, b, None), COL)

    def test_the_default_is_never_just_the_to_headers_first_column(self):
        """`to_cols[0]` and `shared[0]` are the same string on every table
        whose headers happen to line up, so the fixture has to disagree with
        itself or the intersection is never actually exercised."""
        a = _table([_row("X, Y", **{COL: 2.0})], columns=[COL])
        b = _table([_row("X, Y", **{"New Index": 1.0, COL: 3.0})],
                   columns=["New Index", COL])
        self.assertEqual(movers.resolve_column(a, b, None), COL)

    def test_no_shared_column_at_all_is_a_layout_change_not_a_user_error(self):
        a = _table([_row("X, Y", **{"Old Index": 1.0})], columns=["Old Index"])
        b = _table([_row("X, Y", **{"New Index": 2.0})], columns=["New Index"])
        with self.assertRaises(LayoutChanged) as ctx:
            movers.resolve_column(a, b, None)
        self.assertIn("Old Index", str(ctx.exception))
        self.assertIn("New Index", str(ctx.exception))

    def test_a_substring_matching_two_headers_in_one_table_is_refused(self):
        a = _table([_row("X, Y", **{RENT: 1.0, self.PLUS: 2.0})],
                   columns=[RENT, self.PLUS])
        b = _table([_row("X, Y", **{RENT: 3.0, self.PLUS: 4.0})],
                   columns=[RENT, self.PLUS])
        with self.assertRaises(SourceUnavailable) as ctx:
            movers.resolve_column(a, b, "rent")
        self.assertIn(RENT, str(ctx.exception))
        self.assertIn(self.PLUS, str(ctx.exception))

    def test_a_substring_resolving_to_two_different_labels_is_refused(self):
        """The measured shape: the two headers each yield exactly one hit, and
        they are different quantities. Nothing downstream could notice."""
        a = _table([_row("X, Y", **{self.PLUS: 2.0})], columns=[self.PLUS])
        b = _table([_row("X, Y", **{RENT: 3.0})], columns=[RENT])
        with self.assertRaises(SourceUnavailable) as ctx:
            movers.resolve_column(a, b, "rent")
        self.assertIn("unrelated series", str(ctx.exception))

    def test_a_column_that_vanished_from_the_newer_table_is_a_layout_change(self):
        a = _table([_row("X, Y", **{COL: 1.0, "Gone Index": 2.0})],
                   columns=[COL, "Gone Index"])
        b = _table([_row("X, Y", **{COL: 3.0})], columns=[COL])
        with self.assertRaises(LayoutChanged) as ctx:
            movers.resolve_column(a, b, "Gone Index")
        self.assertIn("needs updating", ctx.exception.remedy)

    def test_a_column_only_the_newer_table_has_is_the_callers_error(self):
        """Diffing it would report N/A for all 396 cities — a page of absences
        that reads as 'Numbeo has no data for any of these'."""
        a = _table([_row("X, Y", **{COL: 1.0})], columns=[COL])
        b = _table([_row("X, Y", **{COL: 3.0, "New Index": 4.0})],
                   columns=[COL, "New Index"])
        with self.assertRaises(SourceUnavailable) as ctx:
            movers.resolve_column(a, b, "New Index")
        self.assertIn(COL, ctx.exception.remedy)

    def test_a_column_in_neither_header_lists_both_headers(self):
        a = _table([_row("X, Y", **{COL: 1.0})], columns=[COL])
        b = _table([_row("X, Y", **{COL: 3.0})], columns=[COL])
        with self.assertRaises(SourceUnavailable) as ctx:
            movers.resolve_column(a, b, "Pollution")
        self.assertIn("no header in either", ctx.exception.message)

    def test_the_full_label_wins_over_the_longer_header_containing_it(self):
        """The healthy direction: a user who typed the complete label has
        already disambiguated, and refusing them would be a refusal
        manufactured by the matcher."""
        cols = [self.PLUS, RENT]
        a = _table([_row("X, Y", **{self.PLUS: 1.0, RENT: 2.0})], columns=cols)
        b = _table([_row("X, Y", **{self.PLUS: 3.0, RENT: 4.0})], columns=cols)
        self.assertEqual(movers.resolve_column(a, b, "Rent Index"), RENT)
        self.assertEqual(movers.resolve_column(a, b, "rent index"), RENT)

    def test_resolution_never_falls_back_to_column_position(self):
        a = _table([_row("X, Y", **{"A": 1.0, COL: 2.0})], columns=["A", COL])
        b = _table([_row("X, Y", **{COL: 3.0, "B": 4.0})], columns=[COL, "B"])
        self.assertEqual(movers.resolve_column(a, b, None), COL)
        with self.assertRaises(LayoutChanged):
            movers.resolve_column(a, b, "A")


class TheJoinIsExactAndNeverGuesses(unittest.TestCase):
    """`_norm` collapses only what is notation and nothing that is identity.

    A join that guesses produces a *delta* between two different cities — a
    fabricated number — where a join that abstains produces a pair of visible
    absences.
    """

    def test_the_two_vancouvers_never_become_one_mover(self):
        a = _table([_row("Vancouver, Canada", **{COL: 70.0})])
        b = _table([_row("Vancouver, WA, United States", **{COL: 95.0})])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["only_in_from"], ["Vancouver, Canada"])
        self.assertEqual(out["only_in_to"], ["Vancouver, WA, United States"])

    def test_nang_is_never_paired_with_penang(self):
        a = _table([_row("Da Nang, Vietnam", **{COL: 40.0})])
        b = _table([_row("Penang, Malaysia", **{COL: 33.0})])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["joined"], 0)
        self.assertEqual(out["counts"]["moved"], 0)

    def test_two_portlands_stay_two_cities(self):
        a = _table([_row("Portland, OR, United States", **{COL: 80.0}),
                    _row("Portland, ME, United States", **{COL: 75.0})])
        b = _table([_row("Portland, OR, United States", **{COL: 82.0}),
                    _row("Portland, ME, United States", **{COL: 90.0})])
        out = movers.diff(a, b, COL)
        deltas = {r["place"]: r["delta"] for r in out["movers"]}
        self.assertEqual(deltas["Portland, OR, United States"], 2.0)
        self.assertEqual(deltas["Portland, ME, United States"], 15.0)

    def test_a_rename_presents_as_a_pair_of_absences_and_is_not_repaired(self):
        """Kiev -> Kyiv. The failure direction is the safe one: the pair is
        visible, reported, and adjacent in the two lists where a human spots
        it. Fuzzy repair risks pairing two genuinely different cities."""
        a = _table([_row("Kiev, Ukraine", **{COL: 40.0})])
        b = _table([_row("Kyiv, Ukraine", **{COL: 44.0})])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["only_in_from"], ["Kiev, Ukraine"])
        self.assertEqual(out["only_in_to"], ["Kyiv, Ukraine"])

    def test_a_country_rename_also_presents_as_absences(self):
        a = _table([_row("Istanbul, Turkey", **{COL: 40.0})])
        b = _table([_row("Istanbul, Türkiye", **{COL: 44.0})])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["joined"], 0)

    def test_a_subdivision_appearing_is_a_rename_not_a_match(self):
        a = _table([_row("Vancouver, Canada", **{COL: 70.0})])
        b = _table([_row("Vancouver, BC, Canada", **{COL: 72.0})])
        self.assertEqual(movers.diff(a, b, COL)["joined"], 0)

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
                out = movers.diff(a, b, COL)
                self.assertEqual(out["joined"], 1, out["only_in_from"])
                self.assertEqual(out["movers"][0]["delta"], 4.0)

    def test_join_key_has_exactly_one_owner(self):
        row = _row("São Paulo, Brazil", **{COL: 1.0})
        self.assertEqual(movers.join_key(row), rankings._norm(row["place"]))


class DuplicateKeysAreExcludedNotSilentlyDeduplicated(unittest.TestCase):
    """`{key: row for row in rows}` is last-wins, and page order is measured to
    be unstable across snapshots. The delta would then be computed between two
    different cities, and *which* two would change between runs."""

    DUPES = [_row("St. Petersburg, Russia", **{COL: 40.0}),
             _row("St Petersburg, Russia", **{COL: 60.0})]

    def test_a_colliding_pair_is_excluded_from_the_index(self):
        index, excluded = movers.index_by_key(_table(self.DUPES))
        self.assertEqual(index, {})
        self.assertEqual(len(excluded), 1)
        self.assertEqual(sorted(excluded[0]["labels"]),
                         ["St Petersburg, Russia", "St. Petersburg, Russia"])

    def test_neither_of_them_becomes_a_mover(self):
        a = _table(self.DUPES)
        b = _table([_row("St. Petersburg, Russia", **{COL: 50.0})])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["counts"]["duplicate_key"], 2)
        self.assertEqual(out["duplicate_keys"][0]["side"], "from")

    def test_the_result_does_not_depend_on_page_order(self):
        """The whole point. Last-wins gives 10.0 one way and -10.0 the other,
        and page order is not stable between snapshots."""
        b = _table([_row("St. Petersburg, Russia", **{COL: 50.0})])
        forward = movers.diff(_table(self.DUPES), b, COL)
        backward = movers.diff(_table(list(reversed(self.DUPES))), b, COL)
        self.assertEqual(forward["counts"], backward["counts"])
        self.assertEqual(forward["movers"], backward["movers"])

    def test_an_excluded_row_is_reported_rather_than_dropped(self):
        a = _table(self.DUPES)
        b = _table([_row("Somewhere, Else", **{COL: 50.0})])
        out = movers.diff(a, b, COL)
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
        out = movers.diff(a, b, COL)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["counts"]["unkeyable_label"], 2)


class ChangeArithmeticNeverInventsANumber(unittest.TestCase):
    def test_a_real_pair(self):
        self.assertEqual(movers.abs_change(25.0, 49.5), (24.5, None))
        value, reason = movers.pct_change(25.0, 49.5)
        self.assertAlmostEqual(value, 98.0)
        self.assertIsNone(reason)

    def test_each_absence_gets_its_own_reason(self):
        self.assertEqual(movers.abs_change(None, 40.0),
                         (None, "metric_absent_in_from"))
        self.assertEqual(movers.abs_change(40.0, None),
                         (None, "metric_absent_in_to"))
        self.assertEqual(movers.abs_change(None, None),
                         (None, "metric_absent_in_both"))
        self.assertEqual(movers.pct_change(None, 40.0),
                         (None, "metric_absent_in_from"))

    def test_a_missing_base_is_never_read_as_zero(self):
        """A city with `?` in 2019 and 40.0 today, read as 0 -> 40.0, is +inf%
        and tops the movers list on the strength of having had no data."""
        value, reason = movers.pct_change(None, 40.0)
        self.assertIsNone(value)
        self.assertNotEqual(value, 0)
        self.assertEqual(reason, "metric_absent_in_from")

    def test_a_non_positive_base_is_refused_not_divided_by(self):
        for base in (0.0, -1.0):
            with self.subTest(base=base):
                value, reason = movers.pct_change(base, 10.0)
                self.assertIsNone(value)
                self.assertEqual(reason, "base_not_positive")

    def test_no_result_is_ever_inf_or_nan(self):
        """`json.dump` writes a non-finite float as `Infinity`, which is not
        JSON — so an overflow would make the degenerate run the one that
        crashes a consumer's parser."""
        for a, b in ((0.0, 10.0), (-1.0, 10.0), (1e-300, 1e300)):
            with self.subTest(a=a, b=b):
                value, reason = movers.pct_change(a, b)
                if value is None:
                    self.assertIsNotNone(reason)
                    continue
                self.assertTrue(float("-inf") < value < float("inf"))
                self.assertEqual(value, value)   # NaN != NaN
        self.assertEqual(movers.pct_change(1e-300, 1e300)[1], "not_finite")

    def test_the_delta_survives_a_base_that_has_no_percentage(self):
        """Absolute change is still knowable at a base of 0; only the ratio is
        not. Returning None for both would lose a fact we hold."""
        a = _table([_row("X, Y", **{COL: 0.0})])
        b = _table([_row("X, Y", **{COL: 10.0})])
        row = movers.diff(a, b, COL)["movers"][0]
        self.assertEqual(row["delta"], 10.0)
        self.assertIsNone(row["pct"])
        self.assertEqual(row["reason"], "base_not_positive")


class SixStatusesWithSixMeanings(unittest.TestCase):
    """'Numbeo did not list this city in 2019' and 'it was listed and the cell
    was `?`' are different facts with different fixes, and neither is 'it
    moved'."""

    def setUp(self):
        self.a = _table([
            _row("Medellin, Colombia", **{COL: 30.0}),
            _row("Osaka, Japan", **{COL: 88.5}),
            _row("Ghosttown, Nowhere", **{COL: 40.0}),
            _row("Nodata, Alpha", **{COL: None}),
            _row("Wasdata, Beta", **{COL: 40.0}),
            _row("Never, Gamma", **{COL: None}),
        ])
        self.b = _table([
            _row("Medellin, Colombia", **{COL: 48.3}),
            _row("Osaka, Japan", **{COL: 54.6}),
            _row("Newcity, Delta", **{COL: 55.0}),
            _row("Nodata, Alpha", **{COL: 40.0}),
            _row("Wasdata, Beta", **{COL: None}),
            _row("Never, Gamma", **{COL: None}),
        ])
        self.out = movers.diff(self.a, self.b, COL)

    def test_every_status_has_its_own_bucket(self):
        self.assertEqual(
            {k: v for k, v in self.out["counts"].items() if v},
            {"moved": 2, "metric_absent_in_from": 1, "metric_absent_in_to": 1,
             "metric_absent_in_both": 1, "new": 1, "delisted": 1})

    def test_the_statuses_are_mutually_exclusive(self):
        joined_and_union = [r["status"] for r in self.out["rows"]]
        self.assertEqual(len(joined_and_union), len(self.out["rows"]))
        for row in self.out["rows"]:
            self.assertIn(row["status"],
                          movers.STATUSES + movers.EXCLUDED_STATUSES)

    def test_a_new_city_carries_no_delta_of_any_kind(self):
        """Putting a `new` city in the table with any delta would make
        'appeared' and 'moved' the same event."""
        new = [r for r in self.out["rows"] if r["status"] == "new"][0]
        self.assertIsNone(new["delta"])
        self.assertIsNone(new["pct"])
        self.assertIsNone(new["value_from"])
        self.assertNotIn(new, self.out["movers"])

    def test_a_delisted_city_carries_no_delta_either(self):
        gone = [r for r in self.out["rows"] if r["status"] == "delisted"][0]
        self.assertIsNone(gone["delta"])
        self.assertIsNone(gone["pct"])
        self.assertIsNone(gone["rank_to"])
        self.assertNotIn(gone, self.out["movers"])

    def test_both_one_sided_lists_are_reported_unconditionally(self):
        """A symmetric explosion in these two counts is the ONLY observer for a
        broken join, so there is no flag that suppresses them."""
        self.assertEqual(self.out["only_in_to"], ["Newcity, Delta"])
        self.assertEqual(self.out["only_in_from"], ["Ghosttown, Nowhere"])

    def test_the_union_row_set_covers_every_city_in_either_table(self):
        union = {r["place"] for r in self.a["rows"]} | {
            r["place"] for r in self.b["rows"]}
        self.assertEqual({r["place"] for r in self.out["rows"]}, union)

    def test_every_union_row_has_the_columns_csv_declares(self):
        for row in self.out["rows"]:
            for col in movers.CSV_COLUMNS:
                self.assertIn(col, row)


class RankIsAPairOfFractionsNeverADelta(unittest.TestCase):
    """The panel grew from 433 to 558 rows and snapshot sizes are not
    monotonic, so a rank delta mixes the city's movement with the panel's
    growth in one number, in an unknown ratio — and it is the number a reader
    trusts most, because it needs no interpretation."""

    def setUp(self):
        a = _table([_row(f"C{i}, X", **{COL: 10.0 + i}) for i in range(4)])
        b = _table([_row(f"C{i}, X", **{COL: 20.0 + i}) for i in range(6)])
        self.out = movers.diff(a, b, COL)

    def test_a_mover_row_has_exactly_the_declared_key_set(self):
        for row in self.out["movers"]:
            self.assertEqual(set(row), set(movers.ROW_KEYS))

    def test_the_four_rank_fields_are_separate_and_carry_both_denominators(self):
        row = self.out["movers"][0]
        self.assertEqual((row["of_from"], row["of_to"]), (4, 6))
        self.assertIsNotNone(row["rank_from"])
        self.assertIsNotNone(row["rank_to"])

    def test_no_rank_delta_key_exists_anywhere_in_the_payload(self):
        found = []

        def walk(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    if "rank_delta" in str(k):
                        found.append(f"{path}.{k}")
                    walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")

        walk(self.out)
        self.assertEqual(found, [])


class TheWholeTableFingerprintGuard(unittest.TestCase):
    """If `?title=` stops being honoured upstream, both fetches return the
    current table, all ~550 cities join, and every delta is exactly 0.0 — a
    complete, plausible 'nothing moved in seven years' with no error
    anywhere."""

    T = _table([_row("A, X", **{COL: 1.0}), _row("B, Y", **{COL: 2.0})])

    def test_one_table_twice_is_caught(self):
        self.assertTrue(movers.same_panel(self.T, self.T))

    def test_two_real_tables_are_not_caught(self):
        other = _table([_row("A, X", **{COL: 1.0}), _row("B, Y", **{COL: 9.9})])
        self.assertFalse(movers.same_panel(self.T, other))

    def test_a_difference_in_any_column_is_enough(self):
        """The fingerprint is taken before column selection, so a guard cannot
        be narrowed by which column the caller happens to be diffing."""
        a = _table([_row("A, X", **{COL: 1.0, RENT: 5.0})])
        b = _table([_row("A, X", **{COL: 1.0, RENT: 6.0})])
        self.assertFalse(movers.same_panel(a, b))

    def test_a_different_row_count_alone_is_enough(self):
        smaller = _table([_row("A, X", **{COL: 1.0})])
        self.assertFalse(movers.same_panel(self.T, smaller))

    def test_two_empty_tables_are_not_this_failure(self):
        """They fingerprint identically because there is nothing to
        fingerprint. Naming that 'the snapshot parameter is ignored' sends the
        reader to the wrong subsystem; an empty panel has its own refusal."""
        empty = _table([], columns=[COL])
        self.assertFalse(movers.same_panel(empty, empty))

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
            self.assertTrue(movers.same_panel(self.T, other))
        delegate.assert_called_once()

    def test_the_owner_itself_refuses_two_empty_tables(self):
        """Asserted on `rankings`, not on `movers`, because that is where the
        precondition now lives — and a test aimed at the wrapper would keep
        passing if the wrapper grew its own copy of the rule."""
        empty = _table([], columns=[COL])
        self.assertFalse(rankings.same_panel(empty, empty))

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

    def test_a_healthy_run_costs_exactly_three_reads(self):
        payload, _ = self._run([self.OLD, self.CURRENT])
        self.assertEqual(payload["reads"],
                         {"snapshot_list": 1, "tables": 2, "probe": 0,
                          "total": 3})

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


class TheSubtlerDriftSignatures(unittest.TestCase):
    """A parameter can be honoured for part of a page. The whole-table
    fingerprint differs, `same_panel` passes, and the answer is still wrong."""

    def test_a_diffed_column_frozen_while_another_moves_is_flagged(self):
        a = _table([_row(f"C{i}, X", **{COL: 10.0, RENT: 5.0})
                    for i in range(6)])
        b = _table([_row(f"C{i}, X", **{COL: 10.0, RENT: 6.0 + i})
                    for i in range(6)])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["stats"]["zero_delta_share"], 1.0)
        self.assertIn("diffed_column_did_not_move_while_another_did",
                      out["partial_drift"])
        self.assertEqual(movers.verdict(out)["exit"], 1)

    def test_the_same_panel_with_the_control_also_frozen_is_not_double_reported(
            self):
        """That is the whole-panel signature, which `same_panel` owns. Two
        guards with one message is how a reader learns to read one of them as
        noise."""
        a = _table([_row(f"C{i}, X", **{COL: 10.0, RENT: 5.0})
                    for i in range(6)])
        b = _table([_row(f"C{i}, X", **{COL: 10.0, RENT: 5.0})
                    for i in range(5)])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["partial_drift"], [])

    def test_an_unreadable_control_is_not_read_as_a_still_control(self):
        a = _table([_row(f"C{i}, X", **{COL: 10.0, RENT: None})
                    for i in range(6)])
        b = _table([_row(f"C{i}, X", **{COL: 10.0, RENT: None})
                    for i in range(6)])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["partial_drift"], [])

    def test_a_refreshed_table_gives_near_zero_deltas_that_equality_misses(self):
        """Deltas that are near-zero but not exactly zero pass every equality
        test. Real history over 396 cities produces hundreds of distinct
        deltas; a repeated table produces one."""
        a = _table([_row(f"C{i}, X", **{COL: 10.0 + i}) for i in range(60)])
        b = _table([_row(f"C{i}, X", **{COL: 10.1 + i}) for i in range(60)])
        out = movers.diff(a, b, COL)
        self.assertNotEqual(out["stats"]["zero_delta_share"], 1.0)
        self.assertEqual(out["stats"]["distinct_delta_values"], 1)
        self.assertIn("too_few_distinct_deltas", out["partial_drift"])

    def test_real_history_over_the_same_panel_is_not_flagged(self):
        a = _table([_row(f"C{i}, X", **{COL: 10.0 + i}) for i in range(60)])
        b = _table([_row(f"C{i}, X", **{COL: 10.0 + i * 1.37}) for i in range(60)])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["partial_drift"], [])
        self.assertGreater(out["stats"]["distinct_delta_values"],
                           movers.DISTINCT_DELTA_FLOOR)

    def test_a_thin_column_is_not_mistaken_for_a_repeated_table(self):
        """396 joined cities of which three carry the column is a sparse
        column. Firing there is a warning on healthy input."""
        rows_a = [_row(f"C{i}, X", **{COL: (10.0 if i < 3 else None)})
                  for i in range(60)]
        rows_b = [_row(f"C{i}, X", **{COL: (12.0 if i < 3 else None)})
                  for i in range(60)]
        out = movers.diff(_table(rows_a), _table(rows_b), COL)
        self.assertEqual(out["stats"]["with_metric_in_both"], 3)
        self.assertEqual(out["partial_drift"], [])

    def test_a_panel_wide_common_mode_shift_is_reported_as_a_rebase(self):
        """A redefinition of a same-named column presents exactly as 400
        cities moving together, and nothing else in the payload reveals it."""
        a = _table([_row(f"C{i}, X", **{COL: 10.0 + i}) for i in range(20)])
        b = _table([_row(f"C{i}, X", **{COL: 20.0 + i}) for i in range(20)])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["stats"]["same_direction_share"], 1.0)
        self.assertTrue(out["rebase_suspected"])
        # A warning, not a refusal: real inflation does this, and a warning
        # that refuses on healthy data gets routed around.
        self.assertEqual(movers.verdict(out)["exit"], 0)
        self.assertIn("rebase_suspected", movers.verdict(out)["warnings"])

    def test_a_mixed_panel_is_not_reported_as_a_rebase(self):
        rows_a = [_row(f"C{i}, X", **{COL: 10.0 + i}) for i in range(20)]
        rows_b = [_row(f"C{i}, X", **{COL: 10.0 + i + (5 if i % 2 else -5)})
                  for i in range(20)]
        out = movers.diff(_table(rows_a), _table(rows_b), COL)
        self.assertFalse(out["rebase_suspected"])

    def test_a_share_over_an_empty_denominator_is_unknown_not_zero(self):
        a = _table([_row("A, X", **{COL: None})])
        b = _table([_row("A, X", **{COL: None})])
        stats = movers.diff(a, b, COL)["stats"]
        self.assertIsNone(stats["zero_delta_share"])
        self.assertIsNone(stats["same_direction_share"])
        self.assertIsNone(stats["median_delta"])


class TheOverlapFloor(unittest.TestCase):
    """Measured healthy overlap is 396/433 = 91% of the smaller table. A
    collapsed join returns a short, sorted, entirely plausible movers list
    computed from a 3% sample, and nothing in the rows themselves looks
    wrong."""

    def test_a_collapsed_join_is_refused(self):
        a = _table([_row(f"C{i}, X", **{COL: 10.0}) for i in range(20)])
        b = _table([_row(f"D{i}, X", **{COL: 12.0}) for i in range(18)]
                   + [_row(f"C{i}, X", **{COL: 12.0}) for i in range(2)])
        out = movers.diff(a, b, COL)
        self.assertTrue(out["join"]["below_floor"])
        self.assertEqual(movers.verdict(out)["exit"], 1)
        self.assertIn("join_below_floor", movers.verdict(out)["reasons"])

    def test_a_healthy_overlap_is_not_refused(self):
        a = _table([_row(f"C{i}, X", **{COL: 10.0}) for i in range(20)])
        b = _table([_row(f"C{i}, X", **{COL: 12.0}) for i in range(19)])
        out = movers.diff(a, b, COL)
        self.assertFalse(out["join"]["below_floor"])
        self.assertGreater(out["join"]["overlap_pct_of_smaller"], 90.0)

    def test_the_overlap_figure_and_both_row_counts_are_always_present(self):
        """Reported on every run, healthy or not: the denominator is the only
        thing that makes 'Medellin moved most' a bounded claim."""
        a = _table([_row("A, X", **{COL: 1.0})])
        b = _table([_row("A, X", **{COL: 2.0})])
        join = movers.diff(a, b, COL)["join"]
        self.assertEqual(join["from_rows"], 1)
        self.assertEqual(join["to_rows"], 1)
        self.assertEqual(join["overlap_pct_of_smaller"], 100.0)
        self.assertEqual(join["floor_pct"], 50.0)

    def test_an_empty_side_reports_an_unknown_overlap_not_a_zero_one(self):
        out = movers.diff(_table([], columns=[COL]),
                          _table([_row("A, X", **{COL: 1.0})]), COL)
        self.assertIsNone(out["join"]["overlap_pct_of_smaller"])
        self.assertFalse(out["join"]["below_floor"])


class NothingComparedIsNotNothingMoved(unittest.TestCase):
    """Zero comparable cities and zero movers render identically — an empty
    table — and they have opposite meanings."""

    def test_a_joined_panel_with_no_comparable_cell_is_refused(self):
        a = _table([_row(f"C{i}, X", **{COL: None}) for i in range(10)])
        b = _table([_row(f"C{i}, X", **{COL: 5.0}) for i in range(10)])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["joined"], 10)
        self.assertEqual(out["stats"]["with_metric_in_both"], 0)
        self.assertIn("nothing_was_compared", movers.verdict(out)["reasons"])
        self.assertEqual(movers.verdict(out)["exit"], 1)

    def test_a_panel_where_nothing_moved_is_a_different_answer(self):
        a = _table([_row(f"C{i}, X", **{COL: 5.0}) for i in range(10)])
        b = _table([_row(f"C{i}, X", **{COL: 5.0}) for i in range(9)])
        out = movers.diff(a, b, COL)
        self.assertEqual(out["stats"]["with_metric_in_both"], 9)
        self.assertNotIn("nothing_was_compared", movers.verdict(out)["reasons"])

    def test_a_zero_delta_city_stays_in_the_table(self):
        """Filtering zero deltas out as 'did not move' would delete precisely
        the evidence the drift guards read."""
        a = _table([_row("A, X", **{COL: 5.0})])
        b = _table([_row("A, X", **{COL: 5.0}), _row("B, Y", **{COL: 1.0})])
        out = movers.diff(a, b, COL)
        self.assertEqual([r["delta"] for r in out["movers"]], [0.0])


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
        self.out = movers.diff(a, b, COL)

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
        out = movers.diff(a, b, COL)
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
        shown = movers.present(movers.diff(a, b, COL), top=None)
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

    def test_the_three_counts_exist_before_present_runs(self):
        """A key that appears only on some paths is its own trap: a consumer
        reading `shown` would get a KeyError on exactly the runs that refused."""
        for key in ("joined", "matched_direction", "shown"):
            self.assertIn(key, self.out)

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
        """
        self.assertFalse(hasattr(movers, "side_max_age"))
        self.assertFalse(hasattr(movers, "is_archive_id"))
        self.assertFalse(hasattr(movers, "ARCHIVE_MAX_AGE"))

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


class TheFixtureIsNotEasierThanTheRealPage(unittest.TestCase):
    """Every table above is built as a dict. This one goes through the real
    parser from real markup — including the empty rank `<td>` Numbeo fills with
    JavaScript — so the diff is exercised against the shape production hands
    it, not against this file's mental model of one."""

    def test_the_whole_diff_runs_on_parsed_html(self):
        a = rankings._parse(_html([("Medellin, Colombia", (30.0, 10.0)),
                                   ("Osaka, Japan", (88.5, 40.0)),
                                   ("Ghosttown, Nowhere", (50.0, 20.0))]), "a")
        b = rankings._parse(_html([("Medellin, Colombia", (48.3, 12.0)),
                                   ("Osaka, Japan", (54.6, 22.0)),
                                   ("Newcity, Delta", (60.0, 30.0))]), "b")
        column = movers.resolve_column(a, b, None)
        self.assertEqual(column, COL)
        out = movers.present(movers.diff(a, b, column), top=None)
        by_place = {r["place"]: r for r in out["movers"]}
        self.assertAlmostEqual(by_place["Medellin, Colombia"]["delta"], 18.3)
        self.assertAlmostEqual(by_place["Medellin, Colombia"]["pct"], 61.0)
        self.assertAlmostEqual(by_place["Osaka, Japan"]["delta"], -33.9)
        self.assertEqual(out["only_in_from"], ["Ghosttown, Nowhere"])
        self.assertEqual(out["only_in_to"], ["Newcity, Delta"])

    def test_an_unpriced_cell_in_real_markup_stays_an_absence(self):
        a = rankings._parse(_html([("Nodata, Alpha", ("?", 1.0))]), "a")
        b = rankings._parse(_html([("Nodata, Alpha", (40.0, 2.0))]), "b")
        out = movers.diff(a, b, COL)
        self.assertEqual(out["counts"]["metric_absent_in_from"], 1)
        self.assertEqual(out["movers"], [])


class MoversRendersNoVerdict(unittest.TestCase):
    """This repository has already shipped one 'lower is better' rule applied
    to every column and marked the worst-paying city as best. Here the sign's
    desirability depends on the column AND on the reader: a rent index rising
    is bad for a renter and good for a landlord, a crime index falling is good,
    a salary index falling is bad, and `Gross Rental Yield` has no single
    answer."""

    def test_the_module_does_not_consume_the_higher_is_better_set(self):
        import pathlib
        source = (pathlib.Path(movers.__file__)).read_text(encoding="utf-8")
        for banned in ("HIGHER_IS_BETTER", "mark_best", "import budget",
                       "from .budget", "from . import budget"):
            self.assertNotIn(banned, source.replace(
                "budget.py owns", "").replace("HIGHER_IS_BETTER` verdicts", ""))

    def test_no_row_carries_a_better_or_worse_field(self):
        a = _table([_row("A, X", **{COL: 10.0})])
        b = _table([_row("A, X", **{COL: 20.0})])
        for row in movers.diff(a, b, COL)["rows"]:
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
        basis = movers.diff(a, b, COL)["basis"]
        self.assertIn("rebased", basis)
        self.assertIn("not necessarily in absolute price", basis)


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
                    movers.resolve_snapshots("cost-of-living", **kwargs)
                out.append(ctx.exception)
        with mock.patch.object(rankings, "snapshots", return_value=[]):
            with self.assertRaises(CitycostError) as ctx:
                movers.resolve_snapshots("cost-of-living", "2019")
            out.append(ctx.exception)

        a = _table([_row("A, X", **{COL: 1.0, "Gone Index": 2.0})],
                   columns=[COL, "Gone Index"])
        b = _table([_row("A, X", **{COL: 3.0, "New Index": 4.0})],
                   columns=[COL, "New Index"])
        for requested in ("Gone Index", "New Index", "Pollution"):
            with self.assertRaises(CitycostError) as ctx:
                movers.resolve_column(a, b, requested)
            out.append(ctx.exception)
        return out

    def test_every_command_in_every_refusal_parses(self):
        parser = build_parser()
        graded = 0
        for exc in self._refusals():
            for command in re.findall(r"`(citycost [^`]+)`", str(exc)):
                graded += 1
                with self.subTest(command=command):
                    argv = shlex.split(command)[1:]
                    try:
                        parser.parse_args(argv)
                    except SystemExit:
                        self.fail(f"a remedy names a command that cannot run: "
                                  f"{command}")
        # The denominator. A selector that stopped finding commands would
        # otherwise pass this loop by ranging over nothing.
        self.assertGreaterEqual(graded, 5, f"graded only {graded} commands")

    def test_every_refusal_carries_a_remedy(self):
        refusals = self._refusals()
        self.assertGreaterEqual(len(refusals), 9)
        for exc in refusals:
            with self.subTest(message=exc.message[:50]):
                self.assertTrue(exc.remedy.strip(), exc.message)


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
                lambda: movers.same_panel(self.T, self.T),
                lambda: movers.same_panel(self.T, self.OTHER)),
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
        self.assertTrue(movers.same_panel(self.T, self.T))


if __name__ == "__main__":
    unittest.main(verbosity=2)
