"""Core IRA safety logic: allocation limits, order planning, verification, one-time execution."""
import json
import random
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from tests._loader import load_manager

m = load_manager()
U = m.DEFAULT_UNIVERSE


class NormalizeWeightsTests(unittest.TestCase):
    def check(self, w):
        self.assertAlmostEqual(sum(w.values()), 1.0, places=2)
        self.assertLessEqual(max(w.values()), m.MAX_WEIGHT + 1e-6)
        self.assertGreaterEqual(sum(w[s] for s in w if s in m.DEFENSIVE), m.MIN_DEFENSIVE - 1e-3)
        self.assertGreaterEqual(min(w.values()), 0.0)

    def test_baselines_valid(self):
        for name, base in m.BASELINES.items():
            with self.subTest(regime=name):
                self.check(m.normalize_weights(base, U))

    def test_extreme_inputs(self):
        for raw in ({"QQQ": 1.0}, {"VTI": 1, "QQQ": 1}, {}, {"VTI": -5, "BND": 2}):
            with self.subTest(raw=raw):
                self.check(m.normalize_weights(raw, U))

    def test_random_inputs_never_break_limits(self):
        rng = random.Random(1)
        for _ in range(300):
            raw = {s: rng.random() * rng.choice([0, 1, 5]) for s in U}
            self.check(m.normalize_weights(raw, U))

    def test_unknown_symbols_ignored(self):
        w = m.normalize_weights({"VTI": 0.5, "FAKE": 0.5}, U)
        self.assertNotIn("FAKE", w)


def held(eq, target, scale=1.0):
    return [{"symbol": s, "market_value": eq * w * scale} for s, w in target.items()]


class PlanRebalanceTests(unittest.TestCase):
    T = m.normalize_weights(m.BASELINES["neutral"], U)

    def plan(self, **snap):
        return m.plan_rebalance(snap, self.T, 5.0, 0.02)

    def test_initial_funding_buys_every_target_position(self):
        p = self.plan(equity=10000, spendable=10000, holdings=[])
        self.assertEqual({o["symbol"] for o in p}, {s for s, w in self.T.items() if w > 0})
        self.assertLessEqual(sum(o["notional"] for o in p), 10000)

    def test_contribution_is_deployed_not_left_idle(self):
        p = self.plan(equity=10500, spendable=500, holdings=held(10000, self.T))
        self.assertTrue(p and all(o["side"] == "buy" for o in p))
        self.assertGreater(sum(o["notional"] for o in p), 250)

    def test_balanced_without_cash_does_nothing(self):
        self.assertEqual(self.plan(equity=10000, spendable=0, holdings=held(10000, self.T)), [])

    def test_zero_equity_does_nothing(self):
        self.assertEqual(self.plan(equity=0, spendable=0, holdings=[]), [])

    def test_overweight_position_is_sold_down(self):
        h = held(10000, self.T)
        h[0]["market_value"] += 3000
        p = self.plan(equity=13000, spendable=0, holdings=h)
        self.assertEqual([(o["side"], o["symbol"]) for o in p], [("sell", h[0]["symbol"])])

    def test_position_outside_target_is_fully_exited(self):
        h = held(10000, self.T) + [{"symbol": "XYZ", "market_value": 600}]
        p = self.plan(equity=10600, spendable=0, holdings=h)
        self.assertTrue(any(o["symbol"] == "XYZ" and o.get("full") for o in p))

    def test_never_spends_more_than_settled_cash(self):
        rng = random.Random(7)
        for _ in range(300):
            eq = rng.uniform(500, 200000)
            cash = rng.uniform(0, eq)
            weights = {s: rng.random() for s in self.T}
            tot = sum(weights.values())
            h = [{"symbol": s, "market_value": (eq - cash) * w / tot} for s, w in weights.items()]
            p = m.plan_rebalance({"equity": eq, "spendable": cash, "holdings": h}, self.T, 5.0, 0.02)
            buys = sum(o["notional"] for o in p if o["side"] == "buy")
            self.assertLessEqual(buys, cash + 0.05)
            self.assertTrue(all(o["notional"] > 0 for o in p))
            self.assertEqual(len({(o["symbol"], o["side"]) for o in p}), len(p))


class VerifyOrdersTests(unittest.TestCase):
    def client(self, statuses):
        c = MagicMock()
        c.get_order_by_id.side_effect = lambda oid: NS(status=statuses[oid], filled_qty="1", filled_avg_price="10")
        return c

    def test_filled_orders_have_no_problems(self):
        res = [{"side": "buy", "symbol": "VTI", "status": "submitted", "order_id": "a"}]
        m.verify_orders(self.client({"a": "OrderStatus.FILLED"}), res)
        self.assertEqual(res[0]["final_status"], "filled")
        self.assertEqual(m._problems(res), [])

    def test_rejected_cancelled_and_failed_are_flagged(self):
        res = [{"side": "buy", "symbol": "A", "status": "submitted", "order_id": "a"},
               {"side": "sell", "symbol": "B", "status": "submitted", "order_id": "b"},
               {"side": "buy", "symbol": "C", "status": "failed", "error": "boom"}]
        m.verify_orders(self.client({"a": "OrderStatus.REJECTED", "b": "canceled"}), res)
        self.assertEqual(len(m._problems(res)), 3)

    def test_unconfirmed_order_is_flagged_after_timeout(self):
        res = [{"side": "buy", "symbol": "A", "status": "submitted", "order_id": "a"}]
        m.verify_orders(self.client({"a": "new"}), res, timeout_s=0)
        self.assertEqual(len(m._problems(res)), 1)


class FakeDB:
    """Routes execute() by SQL text: SELECT returns the proposal row, UPDATE ... EXECUTING is the claim."""
    def __init__(self, row, claim_ok=True):
        self.row, self.claim_ok, self.updates = row, claim_ok, []

    def execute(self, sql, params=None):
        r = MagicMock()
        if "SELECT data" in sql:
            r.fetchone.return_value = self.row
        elif "'EXECUTING'" in sql:
            r.fetchone.return_value = (1,) if self.claim_ok else None
        else:
            self.updates.append((sql, params))
        return r

    def commit(self):
        pass


def proposal(**over):
    data = {"equity": 10000.0, "regime": "neutral", "gate": {"verdict": "APPROVE"},
            "plan": [{"side": "buy", "symbol": "VTI", "notional": 3000.0, "drift_pct": -30.0}]}
    data.update(over.pop("data", {}))
    base = dict(data=json.dumps(data), verdict="PROPOSED", executed=False, age=60.0)
    base.update(over)
    return (base["data"], base["verdict"], base["executed"], base["age"])


SNAP = {"equity": 10000.0, "spendable": 9800.0, "holdings": []}


class ExecuteProposalTests(unittest.TestCase):
    def run_exec(self, row, claim_ok=True, snap=None, market_open=True, cfg=None):
        db = FakeDB(row, claim_ok)
        with patch.object(m, "get_config", return_value={"halted": False, **(cfg or {})}), \
             patch.object(m, "get_snapshot", return_value=snap or SNAP), \
             patch.object(m, "_market_open", return_value=market_open), \
             patch.object(m, "_execute_and_verify",
                          return_value=([{"side": "buy", "symbol": "VTI", "status": "submitted",
                                          "final_status": "filled"}], True)) as ex:
            out = m.execute_proposal(db, 1, 99)
        return out, ex, db

    def fails(self, code, **kw):
        with self.assertRaises(ValueError) as cm:
            self.run_exec(**kw)
        self.assertIn(code, str(cm.exception))

    def test_happy_path_executes_once_and_records_result(self):
        out, ex, db = self.run_exec(proposal())
        ex.assert_called_once()
        self.assertEqual(out["final_status"], "EXECUTED")
        self.assertTrue(any("UPDATE" in s for s, _ in db.updates))

    def test_halted_blocks_execution(self):
        self.fails("ira_halted", row=proposal(), cfg={"halted": True})

    def test_missing_proposal(self):
        self.fails("proposal_not_found", row=None)

    def test_already_executed_or_not_proposed(self):
        self.fails("proposal_not_available", row=proposal(executed=True))
        self.fails("proposal_not_available", row=proposal(verdict="EXECUTED"))

    def test_expired_proposal(self):
        self.fails("proposal_expired", row=proposal(age=31 * 60))

    def test_unapproved_gate(self):
        self.fails("proposal_not_approved", row=proposal(data={"gate": {"verdict": "BLOCK"}}))

    def test_account_changed(self):
        self.fails("account_changed", row=proposal(), snap={**SNAP, "equity": 9000.0})

    def test_sell_without_matching_holding_is_rejected(self):
        sell = {"side": "sell", "symbol": "VTI", "notional": 2000.0}
        self.fails("account_changed", row=proposal(data={"plan": [sell]}))

    def test_market_closed(self):
        self.fails("market_closed", row=proposal(), market_open=False)

    def test_lost_claim_means_no_double_execution(self):
        db = FakeDB(proposal(), claim_ok=False)
        with patch.object(m, "get_config", return_value={"halted": False}), \
             patch.object(m, "get_snapshot", return_value=SNAP), \
             patch.object(m, "_market_open", return_value=True), \
             patch.object(m, "_execute_and_verify") as ex:
            with self.assertRaises(ValueError):
                m.execute_proposal(db, 1, 99)
        ex.assert_not_called()

    def test_buys_are_scaled_down_to_available_cash(self):
        captured = {}

        def fake_exec(db, uid, plan, regime):
            captured["plan"] = plan
            return ([], False)
        db = FakeDB(proposal())
        with patch.object(m, "get_config", return_value={"halted": False}), \
             patch.object(m, "get_snapshot", return_value={**SNAP, "spendable": 1500.0}), \
             patch.object(m, "_market_open", return_value=True), \
             patch.object(m, "_execute_and_verify", side_effect=fake_exec):
            m.execute_proposal(db, 1, 99)
        self.assertLessEqual(sum(o["notional"] for o in captured["plan"]), 1500.01)


class RegimeTests(unittest.TestCase):
    def regime(self, env):
        import sys
        import types
        pkg = types.ModuleType("app.strategies")
        pkg.__path__ = []
        me = types.ModuleType("app.strategies.market_env")
        dm = types.ModuleType("app.strategies.dual_momentum")
        self.dm_called = False

        def assess():
            if isinstance(env, Exception):
                raise env
            return {"environment": env}

        def evaluate():
            self.dm_called = True
            return {"recommended_symbol": "AGG"}
        me.assess, dm.evaluate = assess, evaluate
        sys.modules.update({"app.strategies": pkg, "app.strategies.market_env": me,
                            "app.strategies.dual_momentum": dm})
        return m._detect_regime()[0]

    def test_validated_mapping(self):
        for env, want in [("BULL", "risk_on"), ("BULL_VOLATILE", "risk_on"), ("BEAR", "risk_off"),
                          ("CORRECTION", "neutral"), ("TRANSITIONAL", "neutral"), ("UNKNOWN", "neutral")]:
            with self.subTest(env=env):
                self.assertEqual(self.regime(env), want)

    def test_data_failure_defaults_to_neutral(self):
        self.assertEqual(self.regime(RuntimeError("yahoo down")), "neutral")

    def test_dual_momentum_is_not_consulted(self):
        self.assertEqual(self.regime("BULL"), "risk_on")
        self.assertFalse(self.dm_called)


def stat(above):
    return {"above_200sma": above}


class TrendOverlayTests(unittest.TestCase):
    BASE = m.normalize_weights(m.BASELINES["risk_on"], U)

    def test_below_average_etfs_are_halved_and_moved_to_cash(self):
        out, flagged = m.apply_trend_overlay(self.BASE, {"QQQ": stat(False), "VTI": stat(True)}, U)
        self.assertEqual(flagged, ["QQQ"])
        self.assertAlmostEqual(out["QQQ"], self.BASE["QQQ"] * 0.5, places=3)
        self.assertAlmostEqual(out["BIL"], self.BASE["BIL"] + self.BASE["QQQ"] * 0.5, places=3)
        self.assertAlmostEqual(out["VTI"], self.BASE["VTI"], places=3)
        self.assertAlmostEqual(sum(out.values()), 1.0, places=3)

    def test_defensive_etfs_are_never_halved(self):
        stats = {s: stat(False) for s in U}
        out, flagged = m.apply_trend_overlay(self.BASE, stats, U)
        self.assertFalse(set(flagged) & m.DEFENSIVE)
        self.assertGreaterEqual(out["BND"], self.BASE["BND"] - 1e-3)

    def test_missing_or_unknown_data_leaves_weights_alone(self):
        out, flagged = m.apply_trend_overlay(self.BASE, {"QQQ": None, "VTI": {"above_200sma": None}}, U)
        self.assertEqual(flagged, [])
        self.assertEqual(out, m.normalize_weights(self.BASE, U))

    def test_overlay_never_raises_equity_share(self):
        rng = random.Random(3)
        for _ in range(100):
            stats = {s: stat(rng.random() < 0.5) for s in U}
            out, _ = m.apply_trend_overlay(self.BASE, stats, U)
            risky = lambda w: sum(v for s, v in w.items() if s not in m.DEFENSIVE)
            self.assertLessEqual(risky(out), risky(self.BASE) + 1e-3)
            self.assertAlmostEqual(sum(out.values()), 1.0, places=3)

    def test_no_cash_sleeve_in_universe_disables_overlay(self):
        uni = ["VTI", "QQQ"]
        base = {"VTI": 0.5, "QQQ": 0.5}
        out, flagged = m.apply_trend_overlay(base, {"QQQ": stat(False)}, uni)
        self.assertEqual((out, flagged), (base, []))


class CapFlaggedTests(unittest.TestCase):
    def test_ai_cannot_raise_a_flagged_etf_above_core(self):
        core = {"VTI": 0.4, "QQQ": 0.05, "BIL": 0.55}
        tilted = {"VTI": 0.4, "QQQ": 0.10, "BIL": 0.50}
        out = m.cap_flagged(tilted, core, ["QQQ"], ["VTI", "QQQ", "BIL"])
        self.assertAlmostEqual(out["QQQ"], 0.05)
        self.assertAlmostEqual(sum(out.values()), 1.0)

    def test_lower_weight_from_ai_is_kept(self):
        core = {"QQQ": 0.05, "BIL": 0.95}
        out = m.cap_flagged({"QQQ": 0.02, "BIL": 0.98}, core, ["QQQ"], ["QQQ", "BIL"])
        self.assertAlmostEqual(out["QQQ"], 0.02)


if __name__ == "__main__":
    unittest.main()
