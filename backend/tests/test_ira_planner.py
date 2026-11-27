"""Retirement planner math. Needs numpy (skipped where it isn't installed)."""
import types
import unittest
from datetime import date

try:
    import numpy  # noqa: F401
except ImportError:
    numpy = None

from tests._loader import load, sql_stub

SETTINGS = {}
fake_db = types.ModuleType("app.database")
fake_db.get_user_setting = lambda db, k, d="", u=None: SETTINGS.get(k, d)


@unittest.skipIf(numpy is None, "numpy not installed")
class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = load("ira_planner", {**sql_stub(), "app.database": fake_db})

    def setUp(self):
        SETTINGS.clear()
        y = date.today().year
        SETTINGS.update(ira_birth_year=str(y - 40), ira_retire_age="65", ira_target_amount="500000")

    def test_not_ready_without_profile(self):
        SETTINGS.clear()
        plan = self.p.build_plan(None, 1, 0.0)
        self.assertFalse(plan["ready"])
        self.assertIn("ira_birth_year", plan["missing"])

    def test_glidepath_gets_more_conservative_with_age_and_risk(self):
        g = self.p.glidepath_equity
        self.assertGreater(g(30, 65, "moderate"), g(50, 65, "moderate"))
        self.assertGreater(g(40, 65, "aggressive"), g(40, 65, "moderate"))
        self.assertGreater(g(40, 65, "moderate"), g(40, 65, "conservative"))
        for age in range(20, 70):
            self.assertTrue(0.25 <= g(age, 65, "moderate") <= 0.95)

    def test_catch_up_applies_from_age_50(self):
        self.assertLess(self.p.contribution_limit(49), self.p.contribution_limit(50))

    def test_success_probability_rises_with_contribution(self):
        plan = self.p.build_plan(None, 1, 10000.0)
        probs = [s["prob_reach_target"] for s in plan["scenarios"]]
        self.assertEqual(probs, sorted(probs))

    def test_recommendation_meets_the_confidence_threshold(self):
        plan = self.p.build_plan(None, 1, 10000.0)
        rec = plan["recommendation"]
        if rec["kind"] == "within_ira_limit":
            mu, sigma = plan["expected_real_return"], plan["expected_volatility"]
            prob = self.p._prob(plan["target"]["amount"], 10000.0, rec["annual"],
                                plan["profile"]["years_to_retire"], mu, sigma)
            self.assertGreaterEqual(prob, 0.79)

    def test_unreachable_goal_reports_gap_outside_the_ira(self):
        SETTINGS["ira_target_amount"] = "5000000"
        rec = self.p.build_plan(None, 1, 0.0)["recommendation"]
        self.assertEqual(rec["kind"], "limit_not_enough")
        self.assertGreater(rec["extra_outside_ira"], 0)

    def test_income_goal_uses_four_percent_rule(self):
        SETTINGS.pop("ira_target_amount")
        SETTINGS["ira_target_income"] = "30000"
        self.assertEqual(self.p.build_plan(None, 1, 0.0)["target"]["amount"], 750000)

    def test_contribution_path_adds_catch_up_from_age_50(self):
        path = self.p.contribution_path(45, 10)               # ages 45..54
        self.assertEqual(list(path[:5]), [7500.0] * 5)
        self.assertEqual(list(path[5:]), [8600.0] * 5)

    def test_per_year_array_matches_the_constant_case(self):
        const = self.p._simulate(1000.0, 7500.0, 12, 0.04, 0.12)
        arr = self.p._simulate(1000.0, numpy.full(12, 7500.0), 12, 0.04, 0.12)
        numpy.testing.assert_allclose(const, arr)

    def test_max_every_year_mode_includes_the_catch_up(self):
        SETTINGS["ira_contribute_max"] = "true"
        SETTINGS["ira_birth_year"] = str(date.today().year - 38)
        SETTINGS["ira_target_amount"] = "1000000"
        plan = self.p.build_plan(None, 1, 0.0)
        mm = plan["max_mode"]
        self.assertEqual((mm["first_year"], mm["final_year"]), (7500, 8600))
        self.assertGreater(mm["catch_up_years"], 0)
        flat_years = 27
        self.assertGreater(mm["total_contributed"], 7500 * flat_years)
        row = [s for s in plan["scenarios"] if s.get("max_every_year")]
        self.assertEqual(len(row), 1)
        self.assertEqual(plan["stress"]["contribution_assumed"], 7500)
        self.assertGreater(plan["stress"]["contribution_avg"], 7500)
        self.assertTrue(plan["stress"]["max_every_year"])

    def test_max_mode_beats_flat_limit_and_is_off_by_default(self):
        SETTINGS["ira_target_amount"] = "1000000"
        SETTINGS["ira_birth_year"] = str(date.today().year - 38)
        flat = self.p.build_plan(None, 1, 0.0)
        self.assertNotIn("max_mode", flat)
        SETTINGS["ira_contribute_max"] = "true"
        maxed = self.p.build_plan(None, 1, 0.0)
        top = lambda p: max(s["p50"] for s in p["scenarios"])
        self.assertGreater(top(maxed), top(flat))

    def test_extra_saving_outside_the_ira_is_reported_on_top_of_the_max_path(self):
        SETTINGS["ira_contribute_max"] = "true"
        SETTINGS["ira_target_amount"] = "5000000"
        rec = self.p.build_plan(None, 1, 0.0)["recommendation"]
        self.assertEqual(rec["kind"], "limit_not_enough")
        self.assertGreater(rec["extra_outside_ira"], 0)
        self.assertEqual(rec["total_annual_saving_needed"], 7500 + rec["extra_outside_ira"])

    def test_retirement_in_the_past_is_not_ready(self):
        SETTINGS["ira_retire_age"] = "30"
        self.assertFalse(self.p.build_plan(None, 1, 0.0)["ready"])


if __name__ == "__main__":
    unittest.main()
