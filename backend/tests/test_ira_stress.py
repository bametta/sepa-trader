"""Sequence-of-returns stress math. Needs numpy (and pandas for the replay test)."""
import types
import unittest

try:
    import numpy as np
    import pandas as pd
except ImportError:
    np = pd = None

from tests._loader import load, sql_stub

fake_db = types.ModuleType("app.database")
fake_db.get_user_setting = lambda db, k, d="", u=None: d


@unittest.skipIf(np is None, "numpy/pandas not installed")
class StressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.planner = load("ira_planner", {**sql_stub(), "app.database": fake_db})
        cls.s = load("ira_stress")
        cls.mix = cls.planner.strategic_mix(0.75)

    def test_zero_shock_projection_matches_annuity_formula(self):
        r = self.planner.ASSUMPTIONS["cash"][0]
        got = self.s.project(0.0, 1000.0, 10, {"cash": 1.0})
        want = 1000.0 * ((1 + r) ** 10 - 1) / r * (1 + r)
        self.assertAlmostEqual(got, want, places=4)

    def test_projection_accepts_a_per_year_contribution_path(self):
        const = self.s.project(0.0, 7500.0, 10, self.mix)
        same = self.s.project(0.0, [7500.0] * 10, 10, self.mix)
        more = self.s.project(0.0, [7500.0] * 5 + [8600.0] * 5, 10, self.mix)
        self.assertAlmostEqual(const, same, places=6)
        self.assertGreater(more, const)
        rows = self.s.scenario_projection(0.0, [7500.0] * 10, 10, self.mix, 100_000)
        self.assertEqual(len(rows), 5)

    def test_every_shock_scenario_ends_below_expected(self):
        rows = self.s.scenario_projection(10000.0, 7500.0, 30, self.mix, 1_000_000)
        base = rows[0]["final"]
        for row in rows[1:]:
            with self.subTest(row["key"]):
                self.assertLess(row["final"], base)
                self.assertLess(row["vs_expected"], 0)

    def test_scenarios_report_target_coverage(self):
        rows = self.s.scenario_projection(0.0, 7500.0, 30, self.mix, 200_000)
        self.assertTrue(all(r["meets_target"] in (True, False) for r in rows))
        rows = self.s.scenario_projection(0.0, 7500.0, 30, self.mix, None)
        self.assertTrue(all(r["pct_of_target"] is None for r in rows))

    def test_short_horizons_do_not_crash(self):
        rows = self.s.scenario_projection(1000.0, 500.0, 1, self.mix, 10_000)
        self.assertEqual(len(rows), 5)

    def test_withdrawal_success_falls_as_the_rate_rises(self):
        probs = [self.s.withdrawal_test(1_000_000, 65, "moderate", r)["success_probability"]
                 for r in (0.03, 0.04, 0.05, 0.07)]
        self.assertEqual(probs, sorted(probs, reverse=True))
        self.assertGreater(probs[0], probs[-1])

    def test_safe_rate_actually_achieves_the_confidence_level(self):
        res = self.s.withdrawal_test(1_000_000, 65, "moderate", 0.04)
        safe = res["safe_rate_90pct"]
        self.assertIsNotNone(safe)
        self.assertTrue(0.02 <= safe <= 0.08)
        again = self.s.withdrawal_test(1_000_000, 65, "moderate", safe)
        self.assertGreaterEqual(again["success_probability"], 0.895)

    def test_results_are_reproducible(self):
        a = self.s.withdrawal_test(500_000, 62, "conservative", 0.04)
        b = self.s.withdrawal_test(500_000, 62, "conservative", 0.04)
        self.assertEqual(a, b)

    def test_high_withdrawal_rate_runs_out_in_the_bad_start_path(self):
        res = self.s.withdrawal_test(1_000_000, 65, "moderate", 0.12)
        self.assertLess(res["bad_start"]["years_funded"], 30)
        self.assertEqual(res["bad_start"]["ending_balance"], 0)

    def test_replay_without_network_degrades_gracefully(self):
        orig = self.s._monthly_returns
        self.s._cache.clear()
        self.s._monthly_returns = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        try:
            out = self.s.historical_replay(0.7, 20, 5000.0, 0.0, 300_000)
        finally:
            self.s._monthly_returns = orig
        self.assertFalse(out["available"])

    def test_replay_finds_the_worst_start_in_a_synthetic_history(self):
        idx = pd.date_range("1996-05-31", periods=360, freq="ME")
        us = pd.Series(0.006, index=idx)
        us.iloc[24:48] = -0.03                                  # a synthetic two-year crash early on
        df = pd.DataFrame({"us": us, "intl": us, "bond": 0.002, "cash": 0.002}, index=idx)
        orig = self.s._monthly_returns
        self.s._cache.clear()
        self.s._monthly_returns = lambda: df
        try:
            out = self.s.historical_replay(0.8, 10, 6000.0, 0.0, 100_000)
        finally:
            self.s._monthly_returns = orig
            self.s._cache.clear()
        self.assertTrue(out["available"])
        self.assertLess(out["worst"]["final"], out["median"])
        self.assertLessEqual(out["median"], out["best"])
        self.assertTrue(out["worst"]["starting"] < "2000-01-01")  # windows containing the crash start early


if __name__ == "__main__":
    unittest.main()
