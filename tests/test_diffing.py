"""`diffing` must never invent a number, and must doubt the ones it produces.

The arithmetic half and the observer half are graded in one file because they
are one question: every flag below is computed FROM the deltas above it, and a
fixture that exercises one exercises the other. A city with `?` in 2019 read as
0, a column frozen while its neighbour moved, 396 deltas taking three distinct
values — each is a complete, well-formed, plausible answer rather than an
error, so each is asserted in both directions.

`movers.verdict` appears in several assertions. That is the point of them: a
drift flag nobody turns into an exit code is a flag a caller never sees, and
the two halves of that claim live in two files.

Offline by construction — every table here is a fixture, and the last class
builds one from the markup Numbeo actually serves rather than from this file's
idea of it.
"""

import unittest

from . import _sandbox  # noqa: F401  (must be first)
from ._tables import COL, RENT, _html, _row, _table
from citycost import comparable, diffing, movers, rankings


class ChangeArithmeticNeverInventsANumber(unittest.TestCase):
    def test_a_real_pair(self):
        self.assertEqual(diffing.abs_change(25.0, 49.5), (24.5, None))
        value, reason = diffing.pct_change(25.0, 49.5)
        self.assertAlmostEqual(value, 98.0)
        self.assertIsNone(reason)

    def test_each_absence_gets_its_own_reason(self):
        self.assertEqual(diffing.abs_change(None, 40.0),
                         (None, "metric_absent_in_from"))
        self.assertEqual(diffing.abs_change(40.0, None),
                         (None, "metric_absent_in_to"))
        self.assertEqual(diffing.abs_change(None, None),
                         (None, "metric_absent_in_both"))
        self.assertEqual(diffing.pct_change(None, 40.0),
                         (None, "metric_absent_in_from"))

    def test_a_missing_base_is_never_read_as_zero(self):
        """A city with `?` in 2019 and 40.0 today, read as 0 -> 40.0, is +inf%
        and tops the movers list on the strength of having had no data."""
        value, reason = diffing.pct_change(None, 40.0)
        self.assertIsNone(value)
        self.assertNotEqual(value, 0)
        self.assertEqual(reason, "metric_absent_in_from")

    def test_a_non_positive_base_is_refused_not_divided_by(self):
        for base in (0.0, -1.0):
            with self.subTest(base=base):
                value, reason = diffing.pct_change(base, 10.0)
                self.assertIsNone(value)
                self.assertEqual(reason, "base_not_positive")

    def test_no_result_is_ever_inf_or_nan(self):
        """`json.dump` writes a non-finite float as `Infinity`, which is not
        JSON — so an overflow would make the degenerate run the one that
        crashes a consumer's parser."""
        for a, b in ((0.0, 10.0), (-1.0, 10.0), (1e-300, 1e300)):
            with self.subTest(a=a, b=b):
                value, reason = diffing.pct_change(a, b)
                if value is None:
                    self.assertIsNotNone(reason)
                    continue
                self.assertTrue(float("-inf") < value < float("inf"))
                self.assertEqual(value, value)   # NaN != NaN
        self.assertEqual(diffing.pct_change(1e-300, 1e300)[1], "not_finite")

    def test_the_delta_survives_a_base_that_has_no_percentage(self):
        """Absolute change is still knowable at a base of 0; only the ratio is
        not. Returning None for both would lose a fact we hold."""
        a = _table([_row("X, Y", **{COL: 0.0})])
        b = _table([_row("X, Y", **{COL: 10.0})])
        row = diffing.diff(a, b, COL)["movers"][0]
        self.assertEqual(row["delta"], 10.0)
        self.assertIsNone(row["pct"])
        self.assertEqual(row["reason"], "base_not_positive")


class EveryReasonTheArithmeticProducesIsACountedStatus(unittest.TestCase):
    """`_join_rows` promotes a delta reason straight to a per-city status and
    counts it against a dict pre-seeded from a hand-written tuple.

    A reason the arithmetic can produce and that tuple does not list arrives as
    `KeyError: 'not_finite'` — and a `KeyError` is not a `CitycostError`, so
    `main()` does not catch it and the user gets a traceback instead of a row.
    """

    def test_an_overflowing_delta_is_counted_rather_than_crashing(self):
        a = _table([_row("A, X", **{COL: 1e308})])
        b = _table([_row("A, X", **{COL: -1e308})])
        self.assertEqual(diffing.abs_change(1e308, -1e308),
                         (None, "not_finite"))
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["counts"]["not_finite"], 1)
        self.assertEqual(out["movers"], [])
        self.assertEqual(out["rows"][0]["status"], "not_finite")

    def test_the_counted_set_and_the_arithmetic_share_one_vocabulary(self):
        """Both directions, because they fail at different moments: a reason
        the arithmetic produces and nothing counts is the crash above, and a
        reason declared but unproducible is a bucket that can never fill —
        which reads as coverage of a case nobody handles."""
        probes = ((None, None), (None, 1.0), (1.0, None), (1.0, 2.0),
                  (1e308, -1e308), (-1e308, 1e308))
        produced = {reason for reason in
                    (diffing.abs_change(a, b)[1] for a, b in probes)
                    if reason is not None}
        self.assertEqual(produced, set(diffing.DELTA_REASONS),
                         f"graded {len(produced)} reasons against "
                         f"{len(diffing.DELTA_REASONS)} declared")
        counts = diffing.diff(
            _table([_row("A, X", **{COL: 1.0})]),
            _table([_row("A, X", **{COL: 2.0})]), COL)["counts"]
        for reason in diffing.DELTA_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, counts)

    def test_the_status_tuple_is_derived_from_the_reasons_not_retyped(self):
        """Two lists kept in step by hand agree on the day they are written.
        `STATUSES` has to CONTAIN the arithmetic's vocabulary, not merely
        happen to."""
        for reason in diffing.DELTA_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, diffing.STATUSES)


class EachStatusIsItsOwnFact(unittest.TestCase):
    """'Numbeo did not list this city in 2019' and 'it was listed and the cell
    was `?`' are different facts with different fixes, and neither is 'it
    moved'. Not named for the count of them: the tuple grew by one the day the
    arithmetic's overflow reason was counted, and a class called
    `SixStatuses...` would then have been a name asserting the wrong number."""

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
        self.out = diffing.diff(self.a, self.b, COL)

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
                          diffing.STATUSES + comparable.EXCLUDED_STATUSES)

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
        self.out = diffing.diff(a, b, COL)

    def test_a_mover_row_has_exactly_the_declared_key_set(self):
        for row in self.out["movers"]:
            self.assertEqual(set(row), set(diffing.ROW_KEYS))

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


class TheSubtlerDriftSignatures(unittest.TestCase):
    """A parameter can be honoured for part of a page. The whole-table
    fingerprint differs, `same_panel` passes, and the answer is still wrong."""

    FLAG = "diffed_column_did_not_move_while_another_did"

    def _frozen_beside_a_mover(self, n):
        """`n` comparable cities, every one of them frozen in the diffed
        column, while the control column moves in all of them."""
        a = _table([_row(f"C{i:02d}, X", **{COL: 10.0, RENT: 5.0})
                    for i in range(n)])
        b = _table([_row(f"C{i:02d}, X", **{COL: 10.0, RENT: 6.0 + i})
                    for i in range(n)])
        return diffing.diff(a, b, COL)

    def test_a_diffed_column_frozen_while_another_moves_is_flagged(self):
        out = self._frozen_beside_a_mover(diffing.FROZEN_COLUMN_MIN_SAMPLE)
        self.assertEqual(out["stats"]["zero_delta_share"], 1.0)
        self.assertIn(self.FLAG, out["partial_drift"])
        self.assertEqual(movers.verdict(out)["exit"], 1)

    def test_two_frozen_cities_beside_a_moving_column_is_healthy_input(self):
        """Measured: two cities, one comparable and genuinely unchanged, fired
        `partial_drift` and exited 1 on data with nothing wrong with it. Flag
        (b) has been gated at a minimum sample since it was written, for this
        exact reason, and flag (a) sat beside it with none — an alarm that
        fires on healthy input is muted within a week, and it takes the
        degenerate case with it."""
        out = self._frozen_beside_a_mover(2)
        self.assertEqual(out["stats"]["zero_delta_share"], 1.0)
        self.assertEqual(out["partial_drift"], [])
        self.assertEqual(movers.verdict(out)["exit"], 0)

    def test_the_floor_is_a_boundary_not_a_direction(self):
        """Both sides of it, in one test, because a gate asserted from one side
        only is satisfied by a constant."""
        floor = diffing.FROZEN_COLUMN_MIN_SAMPLE
        self.assertGreater(floor, 1, "a floor of 1 gates nothing")
        for n, expected in ((floor - 1, False), (floor, True)):
            with self.subTest(comparable=n):
                out = self._frozen_beside_a_mover(n)
                self.assertEqual(out["stats"]["with_metric_in_both"], n)
                self.assertEqual(self.FLAG in out["partial_drift"], expected)

    def test_the_same_panel_with_the_control_also_frozen_is_not_double_reported(
            self):
        """That is the whole-panel signature, which `same_panel` owns. Two
        guards with one message is how a reader learns to read one of them as
        noise."""
        a = _table([_row(f"C{i}, X", **{COL: 10.0, RENT: 5.0})
                    for i in range(6)])
        b = _table([_row(f"C{i}, X", **{COL: 10.0, RENT: 5.0})
                    for i in range(5)])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["partial_drift"], [])

    def _control(self, a, b, column=COL):
        """The control verdict itself, three-valued.

        Asserted here rather than through `partial_drift`, where the only
        consumer is `control_moves is True`: through that one predicate `None`
        and `False` produce an identical empty flag list, so a test named for
        telling them apart could not observe the distinction it claims and
        passed whichever way the code went."""
        from_index, _ = comparable.index_by_key(a)
        to_index, _ = comparable.index_by_key(b)
        joined = [k for k in to_index if k in from_index]
        shared = [c for c in b["columns"] if c in a["columns"]]
        return diffing._control_moves(from_index, to_index, joined, shared,
                                     column)

    def test_an_unreadable_control_is_not_read_as_a_still_control(self):
        """`?` in every control cell is no evidence at all. Reading it as "the
        control did not move either" would route the reader to the whole-panel
        signature `same_panel` owns; reading it as "it moved" would fire flag
        (a) on a column nobody could read."""
        a = _table([_row(f"C{i:02d}, X", **{COL: 10.0, RENT: None})
                    for i in range(12)])
        b = _table([_row(f"C{i:02d}, X", **{COL: 10.0, RENT: None})
                    for i in range(12)])
        self.assertIsNone(self._control(a, b))
        self.assertEqual(diffing.diff(a, b, COL)["partial_drift"], [])

    def test_a_still_control_is_false_and_a_moving_one_is_true(self):
        """The two states the one above must not be confused with. `False` is
        a measurement — every comparable control cell was read and none
        moved — and only `assertIs` tells it from `None`."""
        keys = {COL: 10.0, RENT: 5.0}
        a = _table([_row(f"C{i:02d}, X", **keys) for i in range(12)])
        still = _table([_row(f"C{i:02d}, X", **keys) for i in range(12)])
        moved = _table([_row(f"C{i:02d}, X", **{COL: 10.0, RENT: 6.0 + i})
                        for i in range(12)])
        self.assertIs(self._control(a, still), False)
        self.assertIs(self._control(a, moved), True)

    def test_every_shared_column_is_a_control_not_only_the_first(self):
        """The control was `next(c for c in shared_columns if c != column)` —
        exactly one, whichever the `--to` header happens to list first. When
        that one is `?` across the panel, a third column moving is invisible
        and the alarm does not fire on the very input it exists for.

        Measured on this fixture: `Dead Index` is the first shared column that
        is not the diffed one, so the old control read `None` (no evidence)
        while `Rent Index` moved in every city."""
        n = diffing.FROZEN_COLUMN_MIN_SAMPLE
        a = _table([_row(f"C{i:02d}, X",
                         **{COL: 10.0, "Dead Index": None, RENT: 5.0})
                    for i in range(n)])
        b = _table([_row(f"C{i:02d}, X",
                         **{COL: 10.0, "Dead Index": None, RENT: 6.0 + i})
                    for i in range(n)])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["shared_columns"], [COL, "Dead Index", RENT])
        self.assertIs(self._control(a, b), True)
        self.assertIn(self.FLAG, out["partial_drift"])
        self.assertEqual(movers.verdict(out)["exit"], 1)

    def test_a_refreshed_table_gives_near_zero_deltas_that_equality_misses(self):
        """Deltas that are near-zero but not exactly zero pass every equality
        test. Real history over 396 cities produces hundreds of distinct
        deltas; a repeated table produces one."""
        a = _table([_row(f"C{i}, X", **{COL: 10.0 + i}) for i in range(60)])
        b = _table([_row(f"C{i}, X", **{COL: 10.1 + i}) for i in range(60)])
        out = diffing.diff(a, b, COL)
        self.assertNotEqual(out["stats"]["zero_delta_share"], 1.0)
        self.assertEqual(out["stats"]["distinct_delta_values"], 1)
        self.assertIn("too_few_distinct_deltas", out["partial_drift"])

    def test_real_history_over_the_same_panel_is_not_flagged(self):
        a = _table([_row(f"C{i}, X", **{COL: 10.0 + i}) for i in range(60)])
        b = _table([_row(f"C{i}, X", **{COL: 10.0 + i * 1.37}) for i in range(60)])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["partial_drift"], [])
        self.assertGreater(out["stats"]["distinct_delta_values"],
                           diffing.DISTINCT_DELTA_FLOOR)

    def test_a_thin_column_is_not_mistaken_for_a_repeated_table(self):
        """396 joined cities of which three carry the column is a sparse
        column. Firing there is a warning on healthy input."""
        rows_a = [_row(f"C{i}, X", **{COL: (10.0 if i < 3 else None)})
                  for i in range(60)]
        rows_b = [_row(f"C{i}, X", **{COL: (12.0 if i < 3 else None)})
                  for i in range(60)]
        out = diffing.diff(_table(rows_a), _table(rows_b), COL)
        self.assertEqual(out["stats"]["with_metric_in_both"], 3)
        self.assertEqual(out["partial_drift"], [])

    def test_a_panel_wide_common_mode_shift_is_reported_as_a_rebase(self):
        """A redefinition of a same-named column presents exactly as 400
        cities moving together, and nothing else in the payload reveals it."""
        a = _table([_row(f"C{i}, X", **{COL: 10.0 + i}) for i in range(20)])
        b = _table([_row(f"C{i}, X", **{COL: 20.0 + i}) for i in range(20)])
        out = diffing.diff(a, b, COL)
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
        out = diffing.diff(_table(rows_a), _table(rows_b), COL)
        self.assertFalse(out["rebase_suspected"])

    def test_a_share_over_an_empty_denominator_is_unknown_not_zero(self):
        a = _table([_row("A, X", **{COL: None})])
        b = _table([_row("A, X", **{COL: None})])
        stats = diffing.diff(a, b, COL)["stats"]
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
        out = diffing.diff(a, b, COL)
        self.assertTrue(out["join"]["below_floor"])
        self.assertEqual(movers.verdict(out)["exit"], 1)
        self.assertIn("join_below_floor", movers.verdict(out)["reasons"])

    def test_a_healthy_overlap_is_not_refused(self):
        a = _table([_row(f"C{i}, X", **{COL: 10.0}) for i in range(20)])
        b = _table([_row(f"C{i}, X", **{COL: 12.0}) for i in range(19)])
        out = diffing.diff(a, b, COL)
        self.assertFalse(out["join"]["below_floor"])
        self.assertGreater(out["join"]["overlap_pct_of_smaller"], 90.0)

    def test_the_overlap_figure_and_both_row_counts_are_always_present(self):
        """Reported on every run, healthy or not: the denominator is the only
        thing that makes 'Medellin moved most' a bounded claim."""
        a = _table([_row("A, X", **{COL: 1.0})])
        b = _table([_row("A, X", **{COL: 2.0})])
        join = diffing.diff(a, b, COL)["join"]
        self.assertEqual(join["from_rows"], 1)
        self.assertEqual(join["to_rows"], 1)
        self.assertEqual(join["overlap_pct_of_smaller"], 100.0)
        self.assertEqual(join["floor_pct"], 50.0)

    def test_an_empty_side_reports_an_unknown_overlap_not_a_zero_one(self):
        out = diffing.diff(_table([], columns=[COL]),
                          _table([_row("A, X", **{COL: 1.0})]), COL)
        self.assertIsNone(out["join"]["overlap_pct_of_smaller"])
        self.assertFalse(out["join"]["below_floor"])


class NothingComparedIsNotNothingMoved(unittest.TestCase):
    """Zero comparable cities and zero movers render identically — an empty
    table — and they have opposite meanings."""

    def test_a_joined_panel_with_no_comparable_cell_is_refused(self):
        a = _table([_row(f"C{i}, X", **{COL: None}) for i in range(10)])
        b = _table([_row(f"C{i}, X", **{COL: 5.0}) for i in range(10)])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["joined"], 10)
        self.assertEqual(out["stats"]["with_metric_in_both"], 0)
        self.assertIn("nothing_was_compared", movers.verdict(out)["reasons"])
        self.assertEqual(movers.verdict(out)["exit"], 1)

    def test_a_panel_where_nothing_moved_is_a_different_answer(self):
        a = _table([_row(f"C{i}, X", **{COL: 5.0}) for i in range(10)])
        b = _table([_row(f"C{i}, X", **{COL: 5.0}) for i in range(9)])
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["stats"]["with_metric_in_both"], 9)
        self.assertNotIn("nothing_was_compared", movers.verdict(out)["reasons"])

    def test_a_zero_delta_city_stays_in_the_table(self):
        """Filtering zero deltas out as 'did not move' would delete precisely
        the evidence the drift guards read."""
        a = _table([_row("A, X", **{COL: 5.0})])
        b = _table([_row("A, X", **{COL: 5.0}), _row("B, Y", **{COL: 1.0})])
        out = diffing.diff(a, b, COL)
        self.assertEqual([r["delta"] for r in out["movers"]], [0.0])


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
        column = comparable.resolve_column(a, b, None)
        self.assertEqual(column, COL)
        out = movers.present(diffing.diff(a, b, column), top=None)
        by_place = {r["place"]: r for r in out["movers"]}
        self.assertAlmostEqual(by_place["Medellin, Colombia"]["delta"], 18.3)
        self.assertAlmostEqual(by_place["Medellin, Colombia"]["pct"], 61.0)
        self.assertAlmostEqual(by_place["Osaka, Japan"]["delta"], -33.9)
        self.assertEqual(out["only_in_from"], ["Ghosttown, Nowhere"])
        self.assertEqual(out["only_in_to"], ["Newcity, Delta"])

    def test_an_unpriced_cell_in_real_markup_stays_an_absence(self):
        a = rankings._parse(_html([("Nodata, Alpha", ("?", 1.0))]), "a")
        b = rankings._parse(_html([("Nodata, Alpha", (40.0, 2.0))]), "b")
        out = diffing.diff(a, b, COL)
        self.assertEqual(out["counts"]["metric_absent_in_from"], 1)
        self.assertEqual(out["movers"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
