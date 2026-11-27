"""Savings service: price fetching/caching and the combined retirement projection (needs numpy + pandas)."""
import types
import unittest
from datetime import date
from unittest.mock import MagicMock

try:
    import numpy  # noqa: F401
    import pandas as pd
except ImportError:
    numpy = pd = None

from tests._loader import load, sql_stub

SETTINGS = {}


@unittest.skipIf(numpy is None, "numpy/pandas not installed")
class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_db = types.ModuleType("app.database")
        fake_db.get_user_setting = lambda db, k, d="", u=None: SETTINGS.get(k, d)
        data = types.ModuleType("app.ira_data")
        data.EQUITY_BUCKETS = {"us_large", "us_midsmall", "intl_dev", "intl_em", "reit"}
        data.REFERENCE = {}
        base = {**sql_stub(), "app.database": fake_db, "app.ira_data": data}
        load("ira_planner", base)
        load("ira_stress", base)
        load("savings", base)
        cls.svc = load("savings_service", base)

    def setUp(self):
        SETTINGS.clear()
        SETTINGS.update(ira_birth_year=str(date.today().year - 38), ira_retire_age="65", ira_risk="moderate")
        self.svc._price_cache.clear()

    def summary(self, balance=100000, accounts=None, equity=0.6):
        accounts = accounts if accounts is not None else [{"annual_contribution": 20000, "retirement": True}]
        return {"retirement_total": balance, "accounts": accounts, "equity_share": equity}

    # ── prices ────────────────────────────────────────────────────────────────
    def test_fetch_prices_dedupes_uppercases_and_drops_failures(self):
        calls = []

        def fake(sym):
            calls.append(sym)
            return None if sym == "BAD" else 10.0
        orig = self.svc._price_one
        self.svc._price_one = fake
        try:
            out = self.svc.fetch_prices(["vti", "VTI", "bad", None, ""])
        finally:
            self.svc._price_one = orig
        self.assertEqual(out, {"VTI": 10.0})
        self.assertEqual(sorted(calls), ["BAD", "VTI"])

    def test_failed_price_lookups_are_not_cached_but_successes_are(self):
        import sys
        fetches = []
        frames = [pd.DataFrame(), pd.DataFrame({"Close": [99.0, 101.0]})]

        def fetch_history(sym, days):
            fetches.append(sym)
            return frames[min(len(fetches) - 1, 1)]
        pkg = types.ModuleType("app.strategies")
        pkg.__path__ = []
        yf = types.ModuleType("app.strategies.yf_client")
        yf.fetch_history = fetch_history
        saved = {k: sys.modules.get(k) for k in ("app.strategies", "app.strategies.yf_client")}
        sys.modules.update({"app.strategies": pkg, "app.strategies.yf_client": yf})
        try:
            self.assertIsNone(self.svc._price_one("ZZZ"))     # empty frame -> None, not cached
            self.assertEqual(self.svc._price_one("ZZZ"), 101.0)
            self.assertEqual(self.svc._price_one("ZZZ"), 101.0)  # cached: no third fetch
        finally:
            for k, v in saved.items():
                sys.modules.pop(k, None) if v is None else sys.modules.__setitem__(k, v)
        self.assertEqual(len(fetches), 2)

    # ── combined projection ───────────────────────────────────────────────────
    def plan(self, summary=None):
        return self.svc.combined_plan(MagicMock(), 1, summary or self.summary())

    def test_not_ready_without_age(self):
        SETTINGS.pop("ira_birth_year")
        self.assertFalse(self.plan()["ready"])

    def test_more_saving_means_more_money(self):
        low = self.plan(self.summary(accounts=[{"annual_contribution": 5000, "retirement": True}]))
        high = self.plan(self.summary(accounts=[{"annual_contribution": 30000, "retirement": True}]))
        self.assertGreater(high["p50"], low["p50"])
        self.assertLess(low["p10"], low["p50"])
        self.assertLess(low["p50"], low["p90"])

    def test_non_retirement_accounts_do_not_add_contributions(self):
        accts = [{"annual_contribution": 10000, "retirement": True}, {"annual_contribution": 50000, "retirement": False}]
        self.assertEqual(self.plan(self.summary(accounts=accts))["annual_contribution"], 10000)

    def test_balance_alone_grows_with_no_contributions(self):
        p = self.plan(self.summary(balance=200000, accounts=[]))
        self.assertGreater(p["p50"], 200000)
        self.assertEqual(p["annual_contribution"], 0)

    def test_income_uses_the_safe_withdrawal_rate(self):
        p = self.plan()
        self.assertIn("income_at_safe_rate", p)
        self.assertAlmostEqual(p["income_at_safe_rate"]["p50"], p["p50"] * p["safe_withdrawal_rate"], delta=2)

    def test_target_gives_probability_and_gap(self):
        SETTINGS["savings_total_target"] = "50000000"          # unreachable
        p = self.plan()
        self.assertLess(p["prob_reach_target"], 0.05)
        self.assertGreater(p["extra_saving_for_80pct"], 0)

    def test_reachable_target_needs_no_extra_saving(self):
        SETTINGS["savings_total_target"] = "100000"
        p = self.plan()
        self.assertGreater(p["prob_reach_target"], 0.8)
        self.assertNotIn("extra_saving_for_80pct", p)

    def test_bad_target_setting_is_ignored(self):
        SETTINGS["savings_total_target"] = "lots"
        self.assertIsNone(self.plan()["target"])


if __name__ == "__main__":
    unittest.main()
