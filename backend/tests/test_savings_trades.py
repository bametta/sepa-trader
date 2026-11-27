"""Manual-trade helper: target parsing, whole shares, cash limits, no-sell mode and asset location."""
import random
import types
import unittest

from tests._loader import load, sql_stub

fake_data = types.ModuleType("app.ira_data")
fake_data.EQUITY_BUCKETS = {"us_large", "us_midsmall", "intl_dev", "intl_em", "reit"}
fake_data.REFERENCE = {
    "VTI": {"exposure": {"us_large": 0.75, "us_midsmall": 0.25}},
    "VXUS": {"exposure": {"intl_dev": 0.7, "intl_em": 0.3}},
    "QQQ": {"exposure": {"us_large": 1.0}},
    "BND": {"exposure": {"bonds_agg": 1.0}},
    "TIP": {"exposure": {"bonds_tips": 1.0}},
}
load("savings", {**sql_stub(), "app.ira_data": fake_data})
st = load("savings_trades")

PRICES = {"VTI": 250.0, "VXUS": 80.0, "BND": 70.0, "QQQ": 650.0, "TIP": 105.0}
TARGET = {"VTI": 0.50, "VXUS": 0.25, "BND": 0.25}


def acct(aid, tax, positions=(), cash=0.0, name=None):
    holdings = [{"symbol": s, "quantity": q, "value": q * PRICES[s], "asset_class": "auto", "name": s}
                for s, q in positions]
    if cash:
        holdings.append({"symbol": None, "quantity": None, "value": cash, "asset_class": "cash", "name": "Cash"})
    return {"id": aid, "name": name or f"acct{aid}", "tax": tax, "holdings": holdings}


def trades(plan, aid):
    return {(t["symbol"], t["side"]): t["shares"] for a in plan["accounts"] if a["id"] == aid for t in a["trades"]}


class TargetModelTests(unittest.TestCase):
    def test_parse_percent_and_fraction_forms(self):
        self.assertEqual(st.parse_target_model("VTI:50, VXUS:25, BND:25"), {"VTI": 0.5, "VXUS": 0.25, "BND": 0.25})
        self.assertEqual(st.parse_target_model("vti=0.6\nbnd=0.4"), {"VTI": 0.6, "BND": 0.4})
        self.assertEqual(st.parse_target_model("VTI:50%; BND:50%"), {"VTI": 0.5, "BND": 0.5})

    def test_rejects_bad_models(self):
        for bad in ("", "VTI", "VTI:abc", "VTI:50, VTI:50", "VTI:0, BND:100", "VTI:60, BND:60", "VTI:50, BND:40",
                    "!!!:100", ",".join(f"A{i}:{100 / 13:.3f}" for i in range(13))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                st.parse_target_model(bad)

    def test_default_target_follows_the_equity_share(self):
        t = st.default_target(0.82)
        self.assertAlmostEqual(sum(t.values()), 1.0)
        self.assertAlmostEqual(t["VTI"] + t["VXUS"], 0.82)
        self.assertAlmostEqual(t["BND"], 0.18)
        self.assertNotIn("BND", st.default_target(1.0))

    def test_format_round_trip(self):
        self.assertEqual(st.parse_target_model(st.format_target(TARGET)), TARGET)


class TechCapTests(unittest.TestCase):
    TGT = {"VTI": 0.40, "QQQ": 0.30, "BND": 0.30}

    def qqq_shares(self, plan):
        return sum(t["shares"] for a in plan["accounts"] for t in a["trades"] if t["symbol"] == "QQQ")

    def test_no_tech_buys_when_already_over_the_limit_elsewhere(self):
        plan = st.plan_trades([acct(1, "roth", cash=10000)], PRICES, self.TGT, outside_total=100000, outside_tech=35000)
        self.assertEqual(self.qqq_shares(plan), 0)
        self.assertTrue(any("Held back" in n and "QQQ" in n for n in plan["notes"]))
        spent = sum(t["value"] for t in plan["accounts"][0]["trades"])
        self.assertGreater(spent, 2000)                                    # cash still goes to the other funds

    def test_partial_buy_up_to_the_limit(self):
        # everything = 110k; 30% = 33k; 32k tech already -> room $1,000 -> 1 share of QQQ at $650
        plan = st.plan_trades([acct(1, "roth", cash=10000)], PRICES, self.TGT, outside_total=100000, outside_tech=32000)
        self.assertEqual(self.qqq_shares(plan), 1)

    def test_tech_total_never_exceeds_limit_after_trades(self):
        for out_tech in (0, 10000, 29000, 31000, 40000):
            with self.subTest(out_tech=out_tech):
                plan = st.plan_trades([acct(1, "roth", cash=10000), acct(2, "pre_tax", cash=5000)], PRICES, self.TGT,
                                      outside_total=85000, outside_tech=out_tech)
                bought = self.qqq_shares(plan) * PRICES["QQQ"]
                self.assertLessEqual(bought, max(0.0, 0.30 * 100000 - out_tech) + 1e-6)

    def test_unaffected_without_tech_in_the_target(self):
        plan = st.plan_trades([acct(1, "roth", cash=10000)], PRICES, TARGET, outside_total=50000, outside_tech=45000)
        self.assertFalse(any("Held back" in n for n in plan["notes"]))
        self.assertGreater(sum(t["value"] for t in plan["accounts"][0]["trades"]), 9000)

    def test_small_tech_target_with_nothing_elsewhere_is_fine(self):
        plan = st.plan_trades([acct(1, "roth", cash=10000)], PRICES, {"VTI": 0.8, "QQQ": 0.2})
        self.assertGreaterEqual(self.qqq_shares(plan), 1)


class DipAllowanceTests(unittest.TestCase):
    TGT = {"VTI": 0.40, "QQQ": 0.30, "BND": 0.30}

    def qqq(self, plan):
        return sum(t["shares"] * t["price"] for a in plan["accounts"] for t in a["trades"] if t["symbol"] == "QQQ")

    def run_plan(self, out_tech, dips, cash=10000, out_total=100000):
        return st.plan_trades([acct(1, "roth", cash=cash)], PRICES, self.TGT, outside_total=out_total,
                              outside_tech=out_tech, dip_off_high=dips)

    def test_dip_allows_buying_above_the_normal_cap(self):
        # tech already 35% (over 30%): without a dip nothing; in a 15% dip some is bought
        none = self.run_plan(38500, {})
        dip = self.run_plan(38500, {"QQQ": -0.15})
        self.assertEqual(self.qqq(none), 0)
        self.assertGreater(self.qqq(dip), 0)
        self.assertTrue(any(t.get("dip") for t in dip["accounts"][0]["trades"] if t["symbol"] == "QQQ"))
        self.assertTrue(any("Dip buys" in n for n in dip["notes"]))

    def test_dip_buying_limited_to_a_quarter_of_the_cash(self):
        dip = self.run_plan(38500, {"QQQ": -0.15}, cash=10000)
        self.assertLessEqual(self.qqq(dip), 0.25 * 10000 + 1e-6)

    def test_never_beyond_the_hard_ceiling(self):
        for out_tech in (20000, 38000, 41000, 50000):
            with self.subTest(out_tech=out_tech):
                plan = self.run_plan(out_tech, {"QQQ": -0.30}, cash=50000, out_total=100000)
                self.assertLessEqual(out_tech + self.qqq(plan), max(0.40 * 150000, out_tech) + 1e-6)

    def test_shallow_pullback_is_not_a_dip(self):
        plan = self.run_plan(38500, {"QQQ": -0.05})
        self.assertEqual(self.qqq(plan), 0)

    def test_no_dip_data_means_no_extra_buying(self):
        self.assertEqual(self.qqq(self.run_plan(38500, None)), 0)

    def test_under_the_normal_cap_nothing_is_labelled_a_dip(self):
        plan = self.run_plan(10000, {"QQQ": -0.20})
        self.assertGreater(self.qqq(plan), 0)
        self.assertFalse(any(t.get("dip") for t in plan["accounts"][0]["trades"]))


class NoOverweightBuysTests(unittest.TestCase):
    def test_cash_only_skips_a_fund_that_is_over_target_across_accounts(self):
        # VTI is $25k of $30k (83%) against a 50% target; the Roth's cash must not buy more of it
        accts = [acct(1, "pre_tax", positions=[("VTI", 100)]), acct(2, "roth", cash=5000)]
        plan = st.plan_trades(accts, PRICES, TARGET, mode="cash_only")
        bought = {(t["symbol"]) for a in plan["accounts"] for t in a["trades"] if t["side"] == "buy"}
        self.assertNotIn("VTI", bought)
        self.assertTrue({"VXUS", "BND"} & bought)

    def test_underweight_funds_are_still_bought(self):
        accts = [acct(1, "pre_tax", positions=[("VTI", 10)]), acct(2, "roth", cash=20000)]
        plan = st.plan_trades(accts, PRICES, TARGET, mode="cash_only")
        bought = {t["symbol"] for a in plan["accounts"] for t in a["trades"] if t["side"] == "buy"}
        self.assertEqual(bought, {"VTI", "VXUS", "BND"})

    def test_never_pushes_a_fund_past_its_target(self):
        accts = [acct(1, "pre_tax", positions=[("VTI", 55), ("BND", 30)]), acct(2, "roth", cash=8000)]
        plan = st.plan_trades(accts, PRICES, TARGET, mode="cash_only")
        total = plan["total"]
        for s, w in plan["after"].items():
            if plan["before"].get(s, 0.0) < TARGET.get(s, 0.0):
                self.assertLessEqual(w, TARGET[s] + 0.5 * PRICES[s] / total + 1e-6)


class CashOnlyTests(unittest.TestCase):
    def test_new_cash_is_deployed_in_whole_shares_without_overspending(self):
        plan = st.plan_trades([acct(1, "roth", cash=10000)], PRICES, TARGET)
        a = plan["accounts"][0]
        self.assertTrue(all(t["side"] == "buy" and isinstance(t["shares"], int) and t["shares"] >= 1 for t in a["trades"]))
        spent = sum(t["shares"] * t["price"] for t in a["trades"])
        self.assertLessEqual(spent, 10000 + 1e-6)
        self.assertAlmostEqual(a["cash_after"], 10000 - spent, places=1)
        self.assertLess(a["cash_after"], 250)                              # leftover below the priciest target fund
        self.assertLess(plan["max_drift_after"], plan["max_drift_before"])

    def test_cash_only_never_sells(self):
        plan = st.plan_trades([acct(1, "roth", positions=[("QQQ", 20)], cash=3000)], PRICES, TARGET)
        self.assertTrue(all(t["side"] == "buy" for t in plan["accounts"][0]["trades"]))
        self.assertTrue(any("QQQ" in n and "left alone" in n for n in plan["notes"]))

    def test_underweight_funds_get_the_money(self):
        held = [("VTI", 100), ("VXUS", 100), ("BND", 0)]
        plan = st.plan_trades([acct(1, "roth", positions=held[:2], cash=5000)], PRICES, TARGET)
        t = trades(plan, 1)
        self.assertIn(("BND", "buy"), t)
        self.assertNotIn(("VTI", "sell"), t)

    def test_tiny_cash_makes_no_trades(self):
        plan = st.plan_trades([acct(1, "roth", positions=[("VTI", 10)], cash=20)], PRICES, TARGET)
        self.assertEqual(plan["accounts"][0]["trades"], [])
        self.assertTrue(any("Nothing to do" in n for n in plan["notes"]))


class LocationTests(unittest.TestCase):
    def test_bonds_go_to_the_pretax_account_and_growth_to_the_roth(self):
        accounts = [acct(1, "roth", cash=30000, name="Roth"), acct(2, "pre_tax", cash=10000, name="Match")]
        plan = st.plan_trades(accounts, PRICES, TARGET)
        roth, match = trades(plan, 1), trades(plan, 2)
        self.assertIn(("BND", "buy"), match)                      # 25% of $40k = $10k fits exactly in the match account
        self.assertNotIn(("BND", "buy"), roth)
        self.assertIn(("VTI", "buy"), roth)
        self.assertIn(("VXUS", "buy"), roth)

    def test_location_beats_fund_size(self):
        """Bonds are the biggest target here; without location logic they'd land in the Roth first."""
        target = {"BND": 0.5, "VTI": 0.5}
        accounts = [acct(1, "roth", cash=20000, name="Roth"), acct(2, "pre_tax", cash=20000, name="Match")]
        plan = st.plan_trades(accounts, PRICES, target)
        self.assertIn(("BND", "buy"), trades(plan, 2))
        self.assertNotIn(("BND", "buy"), trades(plan, 1))
        self.assertIn(("VTI", "buy"), trades(plan, 1))
        self.assertNotIn(("VTI", "buy"), trades(plan, 2))

    def test_overflow_goes_where_there_is_room(self):
        accounts = [acct(1, "roth", cash=10000), acct(2, "pre_tax", cash=30000)]       # bonds target ($10k) < pretax room
        plan = st.plan_trades(accounts, PRICES, TARGET)
        match = trades(plan, 2)
        self.assertIn(("BND", "buy"), match)
        self.assertTrue(any(k[0] in ("VTI", "VXUS") for k in match))                    # remaining pre-tax room holds equities

    def test_each_account_only_spends_its_own_cash(self):
        accounts = [acct(1, "roth", cash=1000), acct(2, "pre_tax", cash=500)]
        plan = st.plan_trades(accounts, PRICES, TARGET)
        for a in plan["accounts"]:
            spent = sum(t["shares"] * t["price"] for t in a["trades"] if t["side"] == "buy")
            self.assertLessEqual(spent, a["cash_before"] + 1e-6)

    def test_only_roth_and_pretax_accounts_are_planned(self):
        with self.assertRaises(ValueError):
            st.plan_trades([{"id": 1, "name": "x", "tax": "taxable", "holdings": []}], PRICES, TARGET)


class FullRebalanceTests(unittest.TestCase):
    def test_sells_overweight_and_buys_underweight_within_one_account(self):
        plan = st.plan_trades([acct(1, "roth", positions=[("VTI", 160), ("BND", 5)], cash=0)], PRICES, TARGET, mode="full")
        t = trades(plan, 1)
        self.assertIn(("VTI", "sell"), t)
        self.assertIn(("BND", "buy"), t)
        self.assertLess(plan["max_drift_after"], plan["max_drift_before"])
        a = plan["accounts"][0]
        self.assertGreaterEqual(a["cash_after"], 0)

    def test_off_target_funds_are_fully_sold(self):
        plan = st.plan_trades([acct(1, "roth", positions=[("QQQ", 10), ("VTI", 20)], cash=0)], PRICES, TARGET, mode="full")
        self.assertEqual(trades(plan, 1).get(("QQQ", "sell")), 10)

    def test_balanced_portfolio_needs_no_trades(self):
        held = [("VTI", 80), ("VXUS", 125), ("BND", 143)]      # ~ 50/25/25 of ~$40k
        plan = st.plan_trades([acct(1, "roth", positions=held)], PRICES, TARGET, mode="full")
        self.assertEqual(plan["accounts"][0]["trades"], [])

    def test_unpriced_target_funds_are_dropped_with_a_note(self):
        plan = st.plan_trades([acct(1, "roth", cash=5000)], {k: v for k, v in PRICES.items() if k != "VXUS"}, TARGET)
        self.assertTrue(any("VXUS" in n for n in plan["notes"]))
        self.assertNotIn("VXUS", plan["target"])

    def test_non_tradable_holdings_are_excluded_not_traded(self):
        a = acct(1, "pre_tax", cash=1000)
        a["holdings"].append({"symbol": None, "quantity": None, "value": 50000, "asset_class": "us_large", "name": "Core Fund"})
        plan = st.plan_trades([a], PRICES, TARGET)
        self.assertEqual(plan["excluded"][0]["name"], "Core Fund")
        self.assertEqual(plan["total"], 1000)


class PropertyTests(unittest.TestCase):
    def random_case(self, rng):
        accounts = []
        for aid in range(1, rng.randint(2, 3) + 1):
            held = [(s, rng.randint(0, 120)) for s in rng.sample(list(PRICES), rng.randint(0, 4))]
            accounts.append(acct(aid, rng.choice(["roth", "pre_tax"]), positions=held, cash=rng.choice([0, 300, 5000, 20000])))
        return accounts

    def test_invariants_hold_on_random_portfolios(self):
        rng = random.Random(11)
        for _ in range(300):
            accounts = self.random_case(rng)
            if sum(a["holdings"][i]["value"] for a in accounts for i in range(len(a["holdings"]))) <= 0:
                continue
            for mode in ("cash_only", "full"):
                plan = st.plan_trades(accounts, PRICES, TARGET, mode=mode)
                for p, a in zip(plan["accounts"], accounts):
                    self.assertGreaterEqual(p["cash_after"], -1e-6)
                    self.assertTrue(all(isinstance(t["shares"], int) and t["shares"] >= 1 for t in p["trades"]))
                    held = {h["symbol"]: h["quantity"] for h in a["holdings"] if h["symbol"]}
                    for t in p["trades"]:
                        if t["side"] == "sell":
                            self.assertEqual(mode, "full")
                            self.assertLessEqual(t["shares"], held.get(t["symbol"], 0))
                    spend = sum(t["shares"] * t["price"] for t in p["trades"] if t["side"] == "buy")
                    proceeds = sum(t["shares"] * t["price"] for t in p["trades"] if t["side"] == "sell")
                    self.assertLessEqual(spend, p["cash_before"] + proceeds + 1e-6)
                self.assertAlmostEqual(sum(plan["after"].values()) + sum(a["cash_after"] for a in plan["accounts"]) / plan["total"], 1.0, places=2)

    def test_cash_deployment_never_increases_drift(self):
        rng = random.Random(5)
        for _ in range(200):
            accounts = self.random_case(rng)
            if not any(h["asset_class"] == "cash" for a in accounts for h in a["holdings"]):
                continue
            plan = st.plan_trades(accounts, PRICES, TARGET, mode="cash_only")
            self.assertLessEqual(plan["max_drift_after"], plan["max_drift_before"] + 0.02)


if __name__ == "__main__":
    unittest.main()
