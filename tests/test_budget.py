import unittest

from . import _sandbox  # noqa: F401
from citycost import budget


def full(**over):
    v = {"rent_1br_outside": 300.0, "utilities": 80.0, "internet": 9.0,
         "mobile": 5.0, "transport_pass": 6.0, "cheap_meal": 2.0,
         "net_salary": 500.0}
    v.update(over)
    return v


class TestStrictness(unittest.TestCase):
    def test_complete_basket_totals(self):
        self.assertEqual(budget.monthly_budget(full()), 300 + 80 + 9 + 5 + 6 + 60)

    def test_any_missing_component_yields_none_never_a_short_sum(self):
        """A total that quietly omits utilities looks entirely reasonable."""
        for key, _ in budget.BASKET:
            v = full()
            del v[key]
            self.assertIsNone(budget.monthly_budget(v), key)

    def test_a_none_valued_component_is_missing_too(self):
        self.assertIsNone(budget.monthly_budget(full(utilities=None)))

    def test_breakdown_names_what_was_missing_so_the_caller_need_not_re_derive(self):
        b = budget.budget_breakdown(full(internet=None))
        self.assertIsNone(b["total"])
        self.assertEqual(b["missing"], ["internet"])
        self.assertIn("utilities", b["parts"])

    def test_zero_is_a_value_not_an_absence(self):
        """Somewhere a transport pass really is free; that is data, not a gap."""
        b = budget.budget_breakdown(full(transport_pass=0.0))
        self.assertEqual(b["missing"], [])
        self.assertIsNotNone(b["total"])


class TestDerived(unittest.TestCase):
    def test_savings_needs_both_halves(self):
        self.assertEqual(budget.savings(full(), 460.0), 40.0)
        self.assertIsNone(budget.savings(full(net_salary=None), 460.0))
        self.assertIsNone(budget.savings(full(), None))

    def test_rent_to_income(self):
        self.assertEqual(budget.rent_to_income(full()), 60.0)
        self.assertIsNone(budget.rent_to_income(full(net_salary=0)))


class TestDirection(unittest.TestCase):
    """One lower-is-better rule for every row marked the worst-paying city
    green — measured on Da Nang $355 beside Chiang Mai $605."""

    def test_costs_are_lower_is_better(self):
        for m in ("utilities", "rent_1br_outside", "budget", "rent_to_income",
                  "Cost of Living Index", "Pollution Index"):
            self.assertEqual(budget.best_of([500.0, 700.0, 4200.0], m), 500.0, m)

    def test_income_and_derived_income_rows_are_higher_is_better(self):
        for m in ("net_salary", "monthly_savings", "salary_budget_ratio",
                  "Safety Index", "Quality of Life Index"):
            self.assertEqual(budget.best_of([500.0, 700.0, 4200.0], m), 4200.0, m)

    def test_single_value_yields_no_best_so_nothing_is_marked(self):
        self.assertIsNone(budget.best_of([42.0], "utilities"))
        self.assertIsNone(budget.best_of([None, None], "utilities"))

    def test_is_better_agrees_with_best_of(self):
        self.assertTrue(budget.is_better("utilities", 1.0, 2.0))
        self.assertTrue(budget.is_better("net_salary", 2.0, 1.0))


class TestAnomalyControl(unittest.TestCase):
    """A control that has never fired is not a control."""

    HEALTHY = {"Tokyo": 0.81, "Fukuoka": 0.98, "Da-Nang": 0.83,
               "Chiang-Mai": 0.81, "Hanoi": 0.85}   # measured 2026-08-25

    def test_measured_healthy_ratios_raise_nothing(self):
        self.assertEqual(budget.ratio_anomalies(self.HEALTHY), {})

    def test_a_collapsed_budget_is_flagged(self):
        self.assertIn("X", budget.ratio_anomalies(dict(self.HEALTHY, X=0.28)))

    def test_a_currency_inflated_budget_is_flagged(self):
        """The Fukuoka yen defect would have surfaced here as roughly 20x."""
        self.assertIn("Y", budget.ratio_anomalies(dict(self.HEALTHY, Y=19.6)))

    def test_too_small_a_sample_gives_no_verdict_rather_than_a_clean_bill(self):
        self.assertEqual(budget.ratio_anomalies({"a": 0.8, "b": 9.9}), {})

    def test_none_ratios_are_skipped_not_counted_as_agreement(self):
        self.assertEqual(budget.ratio_anomalies(dict(self.HEALTHY, X=None)), {})

    def test_the_band_is_wide_enough_not_to_fire_on_a_genuinely_odd_city(self):
        """A city 40% off the median is unusual, not broken. A band that fires
        on healthy input gets muted, and then it protects nothing."""
        self.assertEqual(budget.ratio_anomalies(dict(self.HEALTHY, Z=0.85 * 1.4)),
                         {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
