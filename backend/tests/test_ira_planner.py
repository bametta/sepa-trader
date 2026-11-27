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

    def test_retirement_in_the_past_is_not_ready(self):
        SETTINGS["ira_retire_age"] = "30"
        self.assertFalse(self.p.build_plan(None, 1, 0.0)["ready"])


if __name__ == "__main__":
    unittest.main()
