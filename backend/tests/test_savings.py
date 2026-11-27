"""Savings tracker: CSV parsing, valuation, aggregation, warnings and the DB layer."""
import types
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

from tests._loader import load, sql_stub

fake_data = types.ModuleType("app.ira_data")
fake_data.EQUITY_BUCKETS = {"us_large", "us_midsmall", "intl_dev", "intl_em", "reit"}
fake_data.REFERENCE = {
    "VTI": {"exposure": {"us_large": 0.75, "us_midsmall": 0.25}},
    "BND": {"exposure": {"bonds_agg": 1.0}},
    "VXUS": {"exposure": {"intl_dev": 0.7, "intl_em": 0.3}},
}
sv = load("savings", {**sql_stub(), "app.ira_data": fake_data})
TODAY = date(2026, 6, 1)


def acct(provider="Principal", atype="401k", holdings=(), retirement=True, aid=1, annual=0):
    return {"id": aid, "provider": provider, "name": f"{provider} {atype}", "account_type": atype,
            "retirement": retirement, "annual_contribution": annual, "holdings": list(holdings)}


def hold(name="X", symbol=None, qty=None, val=None, cls="auto", illiquid=False, as_of=TODAY):
    return {"id": None, "symbol": symbol, "name": name, "quantity": qty, "manual_value": val,
            "asset_class": cls, "illiquid": illiquid, "as_of": as_of}


class CsvTests(unittest.TestCase):
    def parse(self, text):
        return sv.parse_holdings_csv(text, today=TODAY)

    def test_symbol_and_quantity(self):
        h = self.parse("symbol,quantity\nvti,120.5\nBND,10")
        self.assertEqual([(x["symbol"], x["quantity"]) for x in h], [("VTI", 120.5), ("BND", 10.0)])
        self.assertEqual(h[0]["asset_class"], "auto")

    def test_name_value_and_class(self):
        h = self.parse("name,value,asset_class,as_of\nPrincipal LifeTime 2050,48250.10,us_large,2026-05-01")
        self.assertEqual(h[0]["manual_value"], 48250.10)
        self.assertEqual(h[0]["as_of"], date(2026, 5, 1))
        self.assertIsNone(h[0]["symbol"])

    def test_money_formatting_is_tolerated(self):
        h = self.parse('name,value\nNote,"$125,000.50"')
        self.assertEqual(h[0]["manual_value"], 125000.50)

    def test_alt_assets_default_to_illiquid(self):
        h = self.parse("name,value,asset_class\nRental,250000,alt_real_estate")
        self.assertTrue(h[0]["illiquid"])
        h = self.parse("name,value,asset_class,illiquid\nRental,250000,alt_real_estate,no")
        self.assertFalse(h[0]["illiquid"])

    def test_blank_lines_ignored_and_empty_input_ok(self):
        self.assertEqual(len(self.parse("symbol,quantity\n\nVTI,1\n\n")), 1)
        self.assertEqual(self.parse("   "), [])

    def test_errors_are_reported_per_line_and_nothing_is_returned(self):
        with self.assertRaises(ValueError) as cm:
            self.parse("symbol,quantity,value\nVTI,abc,\nBND,,\n,5,\nVXUS,-3,")
        msg = str(cm.exception)
        for needle in ("line 2", "line 3", "line 4", "line 5"):
            self.assertIn(needle, msg)

    def test_header_problems(self):
        with self.assertRaises(ValueError):
            self.parse("quantity,value\n1,2")
        with self.assertRaises(ValueError):
            self.parse("symbol,quantity,secret\nVTI,1,x")

    def test_rejected_shapes(self):
        for bad in ("name,quantity\nFund,5",                       # quantity needs a symbol
                    "symbol,quantity,asset_class\nVTI,1,stocks",   # unknown class
                    "name,value,as_of\nX,1,2030-01-01",            # future as_of
                    "name,value\nX,99999999999"):                  # absurd amount
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.parse(bad)

    def test_size_limits(self):
        with self.assertRaises(ValueError):
            self.parse("name,value\n" + "a,1\n" * (sv.MAX_HOLDINGS + 1))
        with self.assertRaises(ValueError):
            self.parse("x" * (sv.MAX_CSV_CHARS + 1))

    def test_round_trip(self):
        original = [hold("Fund A", "VTI", qty=10.0), hold("Rental", None, val=250000.0, cls="alt_real_estate",
                                                         illiquid=True)]
        back = self.parse(sv.holdings_to_csv(original))
        self.assertEqual([(h["symbol"], h["name"], h["quantity"], h["manual_value"], h["illiquid"]) for h in back],
                         [("VTI", "Fund A", 10.0, None, False), (None, "Rental", None, 250000.0, True)])


class AccountValidationTests(unittest.TestCase):
    def test_valid(self):
        v = sv.validate_account(" Fidelity ", "Rollover IRA", "ira", 6000)
        self.assertEqual((v["provider"], v["annual_contribution"]), ("Fidelity", 6000.0))

    def test_invalid(self):
        for args in (("", "n", "ira", 0), ("p", "", "ira", 0), ("p", "n", "crypto_wallet", 0),
                     ("p", "n", "ira", -1), ("p", "n", "ira", "abc"), ("p" * 61, "n", "ira", 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                sv.validate_account(*args)

    def test_tax_buckets(self):
        self.assertEqual([sv.tax_bucket(t) for t in ("401k", "roth_401k", "sdira", "roth_sdira", "taxable", "hsa")],
                         ["pre_tax", "roth", "pre_tax", "roth", "taxable", "hsa"])


class ValuationTests(unittest.TestCase):
    def test_live_manual_missing(self):
        self.assertEqual(sv.value_holding(hold(symbol="VTI", qty=10), 100.0), (1000.0, "live"))
        self.assertEqual(sv.value_holding(hold(symbol="FUND", qty=10, val=900.0), None), (900.0, "manual"))
        self.assertEqual(sv.value_holding(hold(symbol="FUND", qty=10), None), (0.0, "missing"))
        self.assertEqual(sv.value_holding(hold(val=5.0), 100.0), (5.0, "manual"))

    def test_exposure(self):
        self.assertEqual(sv.exposure(hold(symbol="VTI", qty=1)), {"us_large": 0.75, "us_midsmall": 0.25})
        self.assertEqual(sv.exposure(hold(symbol="MYSTERY", qty=1)), {"unclassified": 1.0})
        self.assertEqual(sv.exposure(hold(symbol="VTI", qty=1, cls="bonds_agg")), {"bonds_agg": 1.0})


class SummaryTests(unittest.TestCase):
    def build(self, accounts, prices=None):
        return sv.build_summary(accounts, prices or {}, TODAY)

    def codes(self, s):
        return {w["code"] for w in s["warnings"]}

    def test_totals_and_breakdowns_across_providers(self):
        s = self.build([
            acct("Principal", "401k", [hold("Core Fund", val=60000, cls="us_large"),
                                       hold("Bond Fund", val=20000, cls="bonds_agg")], aid=1),
            acct("Fidelity", "roth_ira", [hold("VTI", "VTI", qty=100)], aid=2),
            acct("Brokerage", "taxable", [hold("Cash", val=10000, cls="cash")], aid=3, retirement=False),
        ], {"VTI": 200.0})
        self.assertEqual(s["total"], 110000)
        self.assertEqual(s["retirement_total"], 100000)
        self.assertEqual(s["by_provider"], {"Principal": 80000, "Fidelity": 20000, "Brokerage": 10000})
        self.assertEqual(s["by_tax"], {"pre_tax": 80000, "roth": 20000, "taxable": 10000})
        self.assertAlmostEqual(s["by_class"]["us_large"], 60000 + 15000)       # look-through of VTI
        self.assertAlmostEqual(s["by_class"]["us_midsmall"], 5000)
        self.assertAlmostEqual(s["equity_share"], 80000 / 110000, places=3)

    def test_same_symbol_in_two_accounts_is_one_position(self):
        s = self.build([acct("A", "401k", [hold("VTI", "VTI", qty=10)], aid=1),
                        acct("B", "ira", [hold("VTI", "VTI", qty=30)], aid=2)], {"VTI": 100.0})
        self.assertEqual(len(s["positions"]), 1)
        self.assertEqual(s["positions"][0]["value"], 4000)

    def test_account_percentages_sum_to_one(self):
        s = self.build([acct("A", "ira", [hold("a", val=30, cls="cash"), hold("b", val=70, cls="cash")])])
        self.assertAlmostEqual(sum(h["pct_of_account"] for h in s["accounts"][0]["holdings"]), 1.0, places=3)

    def test_empty_and_zero_value_never_divide_by_zero(self):
        s = self.build([])
        self.assertEqual((s["total"], s["equity_share"]), (0, 0.0))
        self.assertIn("NO_ACCOUNTS", self.codes(s))
        s = self.build([acct("A", "ira", [hold("nothing", symbol="Z", qty=5)])])
        self.assertIn("MISSING_VALUE", self.codes(s))
        self.assertEqual(s["total"], 0)

    def test_stale_manual_values(self):
        old = TODAY - timedelta(days=100)
        s = self.build([acct("A", "401k", [hold("Fund", val=1000, cls="us_large", as_of=old)])])
        self.assertIn("STALE_VALUATION", self.codes(s))
        # live-priced holdings are never stale, and illiquid assets get a longer window
        s = self.build([acct("A", "401k", [hold("VTI", "VTI", qty=1, as_of=old)])], {"VTI": 100.0})
        self.assertNotIn("STALE_VALUATION", self.codes(s))
        s = self.build([acct("A", "sdira", [hold("House", val=1000, cls="alt_real_estate", illiquid=True, as_of=old)])])
        self.assertNotIn("STALE_VALUATION", self.codes(s))
        s = self.build([acct("A", "sdira", [hold("House", val=1000, cls="alt_real_estate", illiquid=True,
                                                 as_of=TODAY - timedelta(days=200))])])
        self.assertIn("STALE_VALUATION", self.codes(s))

    def test_unclassified_warning(self):
        s = self.build([acct("A", "401k", [hold("Mystery", "ZZZZ", val=5000)])])
        self.assertIn("UNCLASSIFIED", self.codes(s))
        self.assertEqual(s["by_class"]["unclassified"], 5000)

    def test_concentration_flags_single_names_but_not_broad_market_funds(self):
        s = self.build([acct("A", "401k", [hold("Acme", "ACME", val=60000), hold("Cash", val=40000, cls="cash")])])
        self.assertIn("CONCENTRATION", self.codes(s))
        s = self.build([acct("A", "401k", [hold("VTI", "VTI", qty=100)])], {"VTI": 100.0})
        self.assertNotIn("CONCENTRATION", self.codes(s))

    def test_diversified_funds_are_not_flagged_as_concentration(self):
        core = self.build([acct("Principal", "401k", [hold("LifeTime 2055", val=90000, cls="us_large"),
                                                      hold("Cash", val=10000, cls="cash")])])
        self.assertNotIn("CONCENTRATION", self.codes(core))
        mutual = self.build([acct("Fidelity", "ira", [hold("FXAIX", "FXAIX", qty=100),
                                                      hold("Cash", val=1000, cls="cash")])], {"FXAIX": 200.0})
        self.assertNotIn("CONCENTRATION", self.codes(mutual))

    def test_single_names_crypto_and_employer_stock_are_flagged(self):
        for h in (hold("Coin", "BTC", val=70000, cls="crypto"), hold("Employer", val=70000, cls="company_stock"),
                  hold("Mystery Inc", "MYST", val=70000)):
            with self.subTest(h=h["name"]):
                s = self.build([acct("A", "taxable", [h, hold("Cash", val=30000, cls="cash")])])
                self.assertIn("CONCENTRATION", self.codes(s))

    def test_real_estate_is_covered_by_the_illiquid_warning_not_concentration(self):
        s = self.build([acct("ET", "sdira", [hold("House", val=80000, cls="alt_real_estate", illiquid=True),
                                             hold("Cash", val=20000, cls="cash")])])
        self.assertNotIn("CONCENTRATION", self.codes(s))
        self.assertIn("ILLIQUID_HIGH", self.codes(s))

    def test_employer_stock_and_illiquid_limits(self):
        s = self.build([acct("A", "401k", [hold("Employer", val=20000, cls="company_stock"),
                                           hold("Bonds", val=80000, cls="bonds_agg")])])
        self.assertIn("COMPANY_STOCK", self.codes(s))
        s = self.build([acct("EquityTrust", "sdira", [hold("Rental", val=40000, cls="alt_real_estate", illiquid=True),
                                                      hold("Cash", val=60000, cls="cash")])])
        self.assertIn("ILLIQUID_HIGH", self.codes(s))
        self.assertAlmostEqual(s["illiquid_share"], 0.4)

    def test_non_retirement_illiquid_does_not_distort_retirement_share(self):
        s = self.build([acct("A", "401k", [hold("Cash", val=100000, cls="cash")]),
                        acct("B", "taxable", [hold("Land", val=50000, cls="alt_real_estate", illiquid=True)],
                             aid=2, retirement=False)])
        self.assertEqual(s["retirement_total"], 100000)


def pcra(name, atype, annual, source, val=1000.0, provider="Schwab (Principal plan)", aid=None):
    return {"id": aid or hash(name) % 1000, "provider": provider, "name": name, "account_type": atype,
            "retirement": True, "annual_contribution": annual, "contribution_source": source,
            "holdings": [hold("Cash", val=val, cls="cash")]}


class DeferralTests(unittest.TestCase):
    """Two PCRAs in one plan: Roth deferrals (you) and the employer match (employer)."""

    def test_limits_by_age_and_year(self):
        self.assertEqual(sv.deferral_limit(2026, 1990)["limit"], 24500)
        self.assertEqual(sv.deferral_limit(2026, 1976)["limit"], 24500 + 8000)         # 50
        self.assertEqual(sv.deferral_limit(2026, 1965)["limit"], 24500 + 11250)        # 61: super catch-up
        self.assertEqual(sv.deferral_limit(2026, 1960)["limit"], 24500 + 8000)         # 66: back to regular
        self.assertTrue(sv.deferral_limit(2031, 1990)["estimated"])

    def summary(self, accounts, birth=1988):
        return sv.build_summary(accounts, {}, TODAY, birth_year=birth)

    def test_employer_match_does_not_count_toward_the_deferral_limit(self):
        s = self.summary([pcra("PCRA Roth", "roth_401k", 20000, "employee"),
                          pcra("PCRA Match", "401k", 5000, "employer")])
        d = s["deferrals"][0]
        self.assertEqual((d["employee_planned"], d["employer_planned"]), (20000, 5000))
        self.assertEqual((d["over_by"], d["room_left"]), (0, 4500))
        self.assertNotIn("DEFERRAL_OVER_LIMIT", {w["code"] for w in s["warnings"]})

    def test_employee_deferrals_across_roth_and_pretax_are_summed(self):
        s = self.summary([pcra("PCRA Roth", "roth_401k", 20000, "employee"),
                          pcra("Pre-tax", "401k", 6000, "employee", aid=2)])
        d = s["deferrals"][0]
        self.assertEqual(d["employee_planned"], 26000)
        self.assertEqual(d["over_by"], 1500)
        self.assertIn("DEFERRAL_OVER_LIMIT", {w["code"] for w in s["warnings"]})

    def test_treating_the_whole_25k_as_your_own_is_flagged(self):
        s = self.summary([pcra("PCRA Roth", "roth_401k", 25000, "employee")])
        self.assertEqual(s["deferrals"][0]["over_by"], 500)

    def test_separate_plans_are_checked_separately_and_iras_are_ignored(self):
        s = self.summary([pcra("Roth", "roth_401k", 20000, "employee", provider="Plan A"),
                          pcra("Roth", "roth_401k", 20000, "employee", provider="Plan B", aid=2),
                          pcra("IRA", "roth_ira", 7000, "employee", provider="Plan A", aid=3)])
        self.assertEqual(sorted(d["provider"] for d in s["deferrals"]), ["Plan A", "Plan B"])
        self.assertTrue(all(d["employee_planned"] == 20000 for d in s["deferrals"]))

    def test_catch_up_age_raises_the_limit(self):
        s = self.summary([pcra("Roth", "roth_401k", 30000, "employee")], birth=1970)    # 56 in 2026
        self.assertEqual(s["deferrals"][0]["limit"], 32500)
        self.assertEqual(s["deferrals"][0]["over_by"], 0)

    def test_two_pcras_roll_up_by_tax_treatment(self):
        s = self.summary([pcra("PCRA Roth", "roth_401k", 20000, "employee", val=30000),
                          pcra("PCRA Match", "401k", 5000, "employer", val=10000, aid=2)])
        self.assertEqual(s["by_tax"], {"roth": 30000, "pre_tax": 10000})

    def test_contribution_source_is_validated(self):
        with self.assertRaises(ValueError):
            sv.validate_account("P", "n", "401k", 100, "boss")
        self.assertEqual(sv.validate_account("P", "n", "401k", 100, "employer")["contribution_source"], "employer")


class MaxDeferralTests(unittest.TestCase):
    """'Max the IRS limit every year' follows the limit (with catch-up) instead of a typed amount."""

    def summary(self, accounts, birth=1988):
        return sv.build_summary(accounts, {}, TODAY, birth_year=birth)

    def roth(self, **kw):
        a = pcra("PCRA Roth", "roth_401k", kw.pop("annual", 0), "employee", val=1000.0)
        a["max_deferral"] = kw.pop("max_deferral", True)
        a.update(kw)
        return a

    def test_amount_follows_the_limit_not_the_typed_value(self):
        s = self.summary([self.roth(annual=1)])
        self.assertEqual(s["accounts"][0]["annual_contribution"], 24500)
        self.assertTrue(s["accounts"][0]["max_deferral"])
        d = s["deferrals"][0]
        self.assertEqual((d["employee_planned"], d["over_by"], d["room_left"]), (24500, 0, 0))

    def test_catch_up_ages_raise_the_amount(self):
        self.assertEqual(self.summary([self.roth()], birth=1970)["accounts"][0]["annual_contribution"], 32500)   # 56
        self.assertEqual(self.summary([self.roth()], birth=1965)["accounts"][0]["annual_contribution"], 35750)   # 61

    def test_unset_flag_keeps_the_typed_amount(self):
        s = self.summary([self.roth(annual=20000, max_deferral=False)])
        self.assertEqual(s["accounts"][0]["annual_contribution"], 20000)

    def test_match_account_amount_is_independent_of_the_max_account(self):
        s = self.summary([self.roth(),
                          pcra("PCRA Match", "401k", 5000, "employer", val=500.0, aid=2)])
        d = s["deferrals"][0]
        self.assertEqual((d["employee_planned"], d["employer_planned"]), (24500, 5000))
        self.assertEqual(d["over_by"], 0)

    def test_only_own_401k_accounts_may_follow_the_limit(self):
        for atype, source in (("roth_ira", "employee"), ("ira", "employee"), ("401k", "employer"), ("taxable", "employee")):
            with self.subTest(atype=atype, source=source), self.assertRaises(ValueError):
                sv.validate_account("P", "n", atype, 0, source, True)
        self.assertTrue(sv.validate_account("P", "n", "roth_401k", 0, "employee", True)["max_deferral"])
        self.assertTrue(sv.validate_account("P", "n", "401k", 0, "employee", True)["max_deferral"])

    def test_flag_is_saved_and_updated(self):
        db = FakeDB()
        sv.create_account(db, 1, "Schwab", "PCRA", "roth_401k", 0, max_deferral=True)
        self.assertTrue(db.calls[-1][1]["md"])
        db = FakeDB(account_row=("Schwab", "PCRA", "roth_401k", 0, True, "", "employee", False))
        sv.update_account(db, 1, 5, max_deferral=True)
        update = [c for c in db.calls if "UPDATE savings_accounts" in c[0]][0][1]
        self.assertTrue(update["md"])
        with self.assertRaises(ValueError):                                        # can't switch type away while maxing
            sv.update_account(FakeDB(account_row=("S", "P", "roth_401k", 0, True, "", "employee", True)), 1, 5,
                              account_type="taxable")


class ManualValueTests(unittest.TestCase):
    """Quick-update table for hand-valued holdings (Equity Trust real estate, notes, core funds)."""

    def clean(self, updates, today=TODAY):
        return sv.validate_value_updates(updates, today)

    def test_valid_updates_are_cleaned_and_dated(self):
        out = self.clean([{"holding_id": "7", "value": "250000.456", "as_of": "2026-05-20"},
                          {"holding_id": 8, "value": 0}])
        self.assertEqual(out[0], {"holding_id": 7, "value": 250000.46, "as_of": date(2026, 5, 20)})
        self.assertEqual(out[1]["as_of"], TODAY)                        # default is today
        self.assertEqual(out[1]["value"], 0.0)                          # a written-off asset is allowed

    def test_bad_updates_are_rejected(self):
        bad = ([], [{"value": 1}], [{"holding_id": 1}], [{"holding_id": 1, "value": "abc"}],
               [{"holding_id": 1, "value": -1}], [{"holding_id": 1, "value": float("nan")}],
               [{"holding_id": 1, "value": 1e11}], [{"holding_id": 1, "value": 1, "as_of": "next week"}],
               [{"holding_id": 1, "value": 1, "as_of": "2026-06-02"}],
               [{"holding_id": 1, "value": 1}, {"holding_id": 1, "value": 2}])
        for updates in bad:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.clean(updates)

    def test_batch_size_is_capped(self):
        with self.assertRaises(ValueError):
            self.clean([{"holding_id": i, "value": 1} for i in range(sv.MAX_HOLDINGS + 1)])


class ValueDB:
    """Wraps FakeDB (defined below) so holding updates report a configurable number of affected rows."""
    def __init__(self, rowcounts=(1,), **kw):
        self._db = FakeDB(**kw)
        self.rowcounts = list(rowcounts)

    def __getattr__(self, name):
        return getattr(self._db, name)

    def execute(self, sql, params=None):
        r = self._db.execute(sql, params)
        if "UPDATE savings_holdings" in sql:
            r.rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
        return r

    def commit(self):
        self._db.commit()

    def rollback(self):
        self._db.rollback()


class ManualValueDbTests(unittest.TestCase):
    UPDATES = [{"holding_id": 11, "value": 160000, "as_of": "2026-05-30"}, {"holding_id": 12, "value": 26000}]

    def test_all_updates_commit_together(self):
        db = ValueDB(rowcounts=[1, 1])
        self.assertEqual(sv.update_manual_values(db, 1, 5, self.UPDATES, TODAY), 2)
        self.assertEqual((db.commits, db.rollbacks), (1, 0))
        ups = [c for c in db.calls if "UPDATE savings_holdings" in c[0]]
        self.assertEqual([(c[1]["h"], c[1]["v"], c[1]["a"]) for c in ups], [(11, 160000.0, 5), (12, 26000.0, 5)])
        self.assertEqual(ups[0][1]["d"], date(2026, 5, 30))

    def test_one_missing_holding_rolls_everything_back(self):
        db = ValueDB(rowcounts=[1, 0])
        with self.assertRaises(ValueError) as cm:
            sv.update_manual_values(db, 1, 5, self.UPDATES, TODAY)
        self.assertIn("holding 12", str(cm.exception))
        self.assertEqual((db.commits, db.rollbacks), (0, 1))

    def test_updates_only_touch_hand_valued_holdings_in_that_account(self):
        db = ValueDB()
        sv.update_manual_values(db, 1, 5, [{"holding_id": 11, "value": 1}], TODAY)
        sql = [c[0] for c in db.calls if "UPDATE savings_holdings" in c[0]][0]
        self.assertIn("account_id = :a", sql)
        self.assertIn("quantity IS NULL", sql)

    def test_other_users_accounts_are_refused_before_any_write(self):
        db = ValueDB(owns=False)
        with self.assertRaises(ValueError) as cm:
            sv.update_manual_values(db, 1, 5, self.UPDATES, TODAY)
        self.assertEqual(str(cm.exception), "account_not_found")
        self.assertFalse(any("UPDATE savings_holdings" in c[0] for c in db.calls))

    def test_invalid_input_never_reaches_the_database(self):
        db = ValueDB()
        with self.assertRaises(ValueError):
            sv.update_manual_values(db, 1, 5, [{"holding_id": 1, "value": -5}], TODAY)
        self.assertEqual(db.calls, [])


class SnapshotDiffTests(unittest.TestCase):
    def snap(self, *items):
        return [{"key": k, "symbol": k if q is not None else None, "name": k, "quantity": q, "value": v}
                for k, q, v in items]

    def test_buy_sell_new_and_removed(self):
        prev = self.snap(("VTI", 10, 2000), ("BND", 20, 1400), ("OLD", 5, 500))
        cur = self.snap(("VTI", 15, 3000), ("BND", 10, 700), ("NEW", 4, 400))
        d = sv.diff_snapshots(prev, cur, 3900, 4100)
        kinds = {e["key"]: e["type"] for e in d["events"]}
        self.assertEqual(kinds, {"VTI": "bought", "BND": "sold", "NEW": "added", "OLD": "removed"})
        vti = [e for e in d["events"] if e["key"] == "VTI"][0]
        self.assertEqual((vti["quantity_change"], vti["value"]), (5, 1000.0))      # 5 shares at $200
        self.assertAlmostEqual(d["traded_net"], 1000 - 700 + 400 - 500)
        self.assertAlmostEqual(d["market_and_other"], 200 - d["traded_net"])

    def test_pure_price_moves_make_no_events(self):
        d = sv.diff_snapshots(self.snap(("VTI", 10, 2000)), self.snap(("VTI", 10, 2100)), 2000, 2100)
        self.assertEqual(d["events"], [])
        self.assertEqual((d["traded_net"], d["market_and_other"]), (0, 100))

    def test_manual_value_updates_are_reported_as_updates(self):
        d = sv.diff_snapshots(self.snap(("Core Fund", None, 50000)), self.snap(("Core Fund", None, 52500)), 50000, 52500)
        self.assertEqual(d["events"][0]["type"], "value_update")
        self.assertEqual(d["events"][0]["change"], 2500)
        self.assertEqual(d["traded_net"], 0)

    def test_tiny_float_noise_in_quantity_is_ignored(self):
        d = sv.diff_snapshots(self.snap(("VTI", 10.0, 2000)), self.snap(("VTI", 10.0000000001, 2000)), 2000, 2000)
        self.assertEqual(d["events"], [])


class ChangeFeedTests(unittest.TestCase):
    def test_load_changes_diffs_consecutive_snapshots_per_account(self):
        import json
        rows = [("1", date(2026, 5, 1), 1000.0, [{"key": "VTI", "symbol": "VTI", "name": "VTI", "quantity": 5, "value": 1000}]),
                ("1", date(2026, 5, 2), 1010.0, [{"key": "VTI", "symbol": "VTI", "name": "VTI", "quantity": 5, "value": 1010}]),
                ("1", date(2026, 5, 3), 2010.0, [{"key": "VTI", "symbol": "VTI", "name": "VTI", "quantity": 10, "value": 2010}]),
                ("2", date(2026, 5, 1), 500.0, json.dumps([{"key": "X", "symbol": None, "name": "X", "quantity": None, "value": 500}])),
                ("2", date(2026, 5, 3), 700.0, json.dumps([{"key": "X", "symbol": None, "name": "X", "quantity": None, "value": 700}]))]
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = rows
        out = sv.load_changes(db, 1, days=3650)
        self.assertEqual([(c["account_key"], c["to"]) for c in out], [("1", "2026-05-03"), ("2", "2026-05-03")])
        self.assertEqual(out[0]["events"][0]["type"], "bought")
        self.assertEqual(out[1]["events"][0]["type"], "value_update")

    def test_record_account_snapshots_writes_one_row_per_account_and_commits_once(self):
        db = FakeDB()
        summary = {"as_of": "2026-06-01", "accounts": [
            {"id": 1, "value": 100.0, "holdings": [{"symbol": "VTI", "name": "VTI", "quantity": 1, "value": 100.0}]},
            {"id": "alpaca-ira", "value": 0.0, "holdings": []}]}
        sv.record_account_snapshots(db, 1, summary)
        inserts = [c for c in db.calls if "INSERT INTO savings_snapshots" in c[0]]
        self.assertEqual([c[1]["k"] for c in inserts], ["1", "alpaca-ira"])
        self.assertEqual(db.commits, 1)


class FakeDB:
    """Records SQL; branches on SELECT text so ownership and merging can be tested."""
    def __init__(self, owns=True, account_row=None, fail_on_insert=False):
        self.owns, self.account_row, self.fail, self.calls, self.commits, self.rollbacks = owns, account_row, fail_on_insert, [], 0, 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        r = MagicMock()
        if "SELECT 1 FROM savings_accounts" in sql:
            r.fetchone.return_value = (1,) if self.owns else None
        elif "SELECT provider, name, account_type" in sql:
            r.fetchone.return_value = self.account_row
        elif "INSERT INTO savings_holdings" in sql and self.fail:
            raise RuntimeError("boom")
        elif "RETURNING id" in sql:
            r.scalar.return_value = 42
        return r

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class DbLayerTests(unittest.TestCase):
    def test_create_validates_before_touching_the_database(self):
        db = FakeDB()
        with self.assertRaises(ValueError):
            sv.create_account(db, 1, "", "x", "ira")
        self.assertEqual(db.calls, [])
        self.assertEqual(sv.create_account(db, 1, "Fidelity", "IRA", "ira", 6000), 42)
        self.assertEqual(db.commits, 1)

    def test_replace_holdings_requires_ownership(self):
        db = FakeDB(owns=False)
        with self.assertRaises(ValueError):
            sv.replace_holdings(db, 1, 5, [{"name": "x", "manual_value": 1}])
        self.assertFalse(any("DELETE" in c[0] for c in db.calls))

    def test_replace_holdings_rolls_back_on_failure(self):
        db = FakeDB(fail_on_insert=True)
        with self.assertRaises(RuntimeError):
            sv.replace_holdings(db, 1, 5, [{"name": "x", "manual_value": 1}])
        self.assertEqual((db.commits, db.rollbacks), (0, 1))

    def test_replace_holdings_inserts_all_and_commits_once(self):
        db = FakeDB()
        n = sv.replace_holdings(db, 1, 5, [{"name": "a", "manual_value": 1}, {"name": "b", "manual_value": 2}])
        self.assertEqual((n, db.commits), (2, 1))
        self.assertEqual(sum("INSERT INTO savings_holdings" in c[0] for c in db.calls), 2)

    def test_update_merges_and_validates(self):
        db = FakeDB(account_row=("Fidelity", "IRA", "ira", 100, True, "", "employee", False))
        self.assertTrue(sv.update_account(db, 1, 5, annual_contribution=500))
        update = [c for c in db.calls if "UPDATE savings_accounts" in c[0]][0][1]
        self.assertEqual((update["p"], update["c"]), ("Fidelity", 500.0))
        with self.assertRaises(ValueError):
            sv.update_account(FakeDB(account_row=("F", "I", "ira", 1, True, "", "employee", False)), 1, 5,
                              account_type="nope")
        self.assertFalse(sv.update_account(FakeDB(account_row=None), 1, 5, name="x"))

    def test_every_query_is_scoped_to_the_user(self):
        db = FakeDB(account_row=("F", "I", "ira", 1, True, "", "employee", False))
        sv.update_account(db, 7, 5, name="x")
        sv.delete_account(db, 7, 5)
        for sql, params in db.calls:
            if "savings_accounts" in sql and "INSERT" not in sql:
                self.assertEqual(params.get("u"), 7, sql)


if __name__ == "__main__":
    unittest.main()
