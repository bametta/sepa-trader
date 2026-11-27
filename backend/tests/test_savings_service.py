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

    def test_accounts_that_follow_the_irs_limit_get_catch_up_in_the_projection(self):
        SETTINGS["ira_birth_year"] = str(date.today().year - 45)        # catch-up starts in 5 years
        flat = self.plan(self.summary(accounts=[{"annual_contribution": 24500, "retirement": True}]))
        maxed = self.plan(self.summary(accounts=[{"annual_contribution": 24500, "retirement": True,
                                                  "max_deferral": True}]))
        self.assertEqual(flat["annual_contribution"], maxed["annual_contribution"])      # same first year
        self.assertGreater(maxed["avg_annual_contribution"], flat["avg_annual_contribution"])
        self.assertGreater(maxed["p50"], flat["p50"])

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

    # ── import + manual-trade helper ──────────────────────────────────────────
    SCHWAB = ('"Positions for account PCRA ...1234"\n"Symbol","Description","Quantity","Market Value","Security Type"\n'
              '"VTI","TOTAL MARKET ETF","12","$3,000.00","ETFs"\n"BND","BOND ETF","10","$700.00","ETFs"\n'
              '"Cash & Cash Investments","--","--","$500.00","Cash"\n"Account Total","--","--","$4,200.00","--"\n')

    def patched(self, accounts, prices):
        svc, saved = self.svc, {}
        saved["load"], saved["replace"], saved["prices"] = svc.savings.load_accounts, svc.savings.replace_holdings, svc.fetch_prices
        self.replaced = []
        svc.savings.load_accounts = lambda db, uid: accounts
        svc.savings.replace_holdings = lambda db, uid, aid, h: self.replaced.append((aid, h)) or len(h)
        svc.fetch_prices = lambda syms: {s: p for s, p in prices.items() if s in set(syms)}
        self.addCleanup(lambda: (setattr(svc.savings, "load_accounts", saved["load"]),
                                 setattr(svc.savings, "replace_holdings", saved["replace"]),
                                 setattr(svc, "fetch_prices", saved["prices"])))

    def acct(self, aid=1, atype="roth_401k", holdings=()):
        return {"id": aid, "provider": "Schwab", "name": f"PCRA {aid}", "account_type": atype,
                "annual_contribution": 0, "retirement": True, "contribution_source": "employee",
                "holdings": list(holdings)}

    def held(self, symbol, qty):
        return {"id": 1, "symbol": symbol, "name": symbol, "quantity": qty, "manual_value": None,
                "asset_class": "auto", "illiquid": False, "as_of": date.today()}

    def test_import_preview_saves_nothing_and_apply_saves(self):
        self.patched([self.acct(holdings=[self.held("VTI", 10)])], {"VTI": 250.0, "BND": 70.0})
        prev = self.svc.import_positions(MagicMock(), 1, 1, self.SCHWAB, apply=False)
        self.assertFalse(prev["applied"])
        self.assertEqual(self.replaced, [])
        kinds = {e["key"]: e["type"] for e in prev["diff"]["events"]}
        self.assertEqual(kinds["VTI"], "bought")                      # 10 -> 12 shares
        self.assertEqual(kinds["BND"], "added")
        done = self.svc.import_positions(MagicMock(), 1, 1, self.SCHWAB, apply=True)
        self.assertTrue(done["applied"])
        self.assertEqual(len(self.replaced), 1)
        self.assertEqual(self.replaced[0][0], 1)
        self.assertEqual(len(self.replaced[0][1]), 3)                 # VTI, BND, cash

    def test_import_into_an_empty_account_lists_everything_as_new(self):
        self.patched([self.acct()], {"VTI": 250.0, "BND": 70.0})
        out = self.svc.import_positions(MagicMock(), 1, 1, self.SCHWAB)
        self.assertEqual(len(out["diff"]["events"]), 3)

    def test_import_warns_when_the_file_looks_like_a_different_account(self):
        self.patched([self.acct(holdings=[self.held("VTI", 1000)])], {"VTI": 250.0, "BND": 70.0})
        out = self.svc.import_positions(MagicMock(), 1, 1, self.SCHWAB)
        self.assertTrue(any("50%" in w for w in out["warnings"]))

    FIDELITY = ("Account Number,Account Name,Symbol,Description,Quantity,Current Value,Type\n"
                "Z111,Rollover IRA,VOO,S&P 500 ETF,20,\"$13,000.00\",Cash\n"
                "Z111,Rollover IRA,SPAXX**,HELD IN MONEY MARKET,,$500.00,Cash\n"
                "Z222,Joint Brokerage,AAPL,APPLE,10,\"$3,000.00\",Margin\n"
                "\n\"Date downloaded 11/27/2026\"\n")

    def test_fidelity_file_with_several_accounts_asks_which_one_and_saves_nothing(self):
        self.patched([self.acct(holdings=[])], {"VOO": 650.0, "AAPL": 300.0})
        first = self.svc.import_positions(MagicMock(), 1, 1, self.FIDELITY, apply=True)
        self.assertTrue(first["needs_choice"])
        self.assertFalse(first["applied"])
        self.assertEqual(self.replaced, [])
        self.assertEqual({c["key"] for c in first["choices"]}, {"Z111", "Z222"})
        self.assertNotIn("diff", first)

    def test_choosing_the_account_previews_then_saves_only_its_holdings(self):
        self.patched([self.acct(holdings=[])], {"VOO": 650.0, "AAPL": 300.0})
        prev = self.svc.import_positions(MagicMock(), 1, 1, self.FIDELITY, apply=False, account_key="Z111")
        self.assertEqual((prev["source"], prev["holdings"]), ("broker", 2))
        done = self.svc.import_positions(MagicMock(), 1, 1, self.FIDELITY, apply=True, account_key="Z111")
        self.assertTrue(done["applied"])
        saved = sorted(h["symbol"] for h in self.replaced[0][1])
        self.assertEqual(saved, ["SPAXX", "VOO"])

    def test_import_accepts_this_apps_own_csv_and_rejects_foreign_accounts(self):
        self.patched([self.acct()], {"VTI": 250.0})
        out = self.svc.import_positions(MagicMock(), 1, 1, "symbol,quantity\nVTI,4\n")
        self.assertEqual((out["source"], out["holdings"]), ("csv", 1))
        with self.assertRaises(ValueError):
            self.svc.import_positions(MagicMock(), 1, 99, self.SCHWAB)

    def test_trade_plan_uses_typed_then_saved_then_default_target(self):
        accounts = [self.acct(1, "roth_401k", [
            self.held("VTI", 10), {"id": 2, "symbol": None, "name": "Cash", "quantity": None, "manual_value": 5000.0,
                                  "asset_class": "cash", "illiquid": False, "as_of": date.today()}])]
        self.patched(accounts, {"VTI": 250.0, "BND": 70.0, "VXUS": 80.0})
        typed = self.svc.trade_plan(MagicMock(), 1, None, "cash_only", "VTI:60, BND:40")
        self.assertEqual(typed["target_source"], "typed")
        SETTINGS["savings_target_model"] = "VTI:50, VXUS:50"
        saved = self.svc.trade_plan(MagicMock(), 1, None, "cash_only", None)
        self.assertEqual(saved["target_source"], "saved")
        SETTINGS.pop("savings_target_model")
        default = self.svc.trade_plan(MagicMock(), 1, None, "cash_only", None)
        self.assertEqual(default["target_source"], "age-based default")
        self.assertIn("VTI", default["target"])

    def test_trade_plan_ignores_taxable_accounts_and_needs_a_target(self):
        self.patched([self.acct(1, "taxable", [self.held("VTI", 10)])], {"VTI": 250.0, "BND": 70.0})
        with self.assertRaises(ValueError):
            self.svc.trade_plan(MagicMock(), 1, None, "cash_only", "VTI:100")
        self.patched([self.acct(1, "roth_401k", [self.held("VTI", 10)])], {"VTI": 250.0})
        SETTINGS.pop("ira_birth_year")
        with self.assertRaises(ValueError):
            self.svc.trade_plan(MagicMock(), 1, None, "cash_only", None)

    def test_bad_target_setting_is_ignored(self):
        SETTINGS["savings_total_target"] = "lots"
        self.assertIsNone(self.plan()["target"])


if __name__ == "__main__":
    unittest.main()
