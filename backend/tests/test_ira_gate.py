"""IRA gate: hard checks always win, the AI can only block, and any AI problem fails closed."""
import types
import unittest
from unittest.mock import MagicMock

from tests._loader import load, sql_stub

STATE = {}


def fresh(stale=False):
    return {"price": 100.0, "ret_1m": 0.0, "ret_3m": 0.0, "ret_12m": 0.1, "vol_20d": 0.15,
            "pct_off_52w_high": -0.05, "above_200sma": True, "as_of": "2026-11-27", "stale": stale}


fake_data = types.ModuleType("app.ira_data")
fake_data.market_stats = lambda syms: {s: STATE["stats"].get(s, fresh()) if STATE["stats"] is not None else None for s in syms}
fake_data.blended_expense_ratio = lambda w: STATE["er"]
fake_data.bucket_exposure = lambda w: {}
fake_data.cheaper_alternatives = lambda w: []
fake_data.lookthrough = lambda w: {"available": True, "top_stocks": [{"symbol": "AAPL", "lookthrough_pct": STATE["stock"]}],
                                   "pair_overlap": []}

fake_ai = types.ModuleType("app.claude_analyst")
fake_ai._call_ai = lambda db, prompt, max_tokens, user_id=None: STATE["ai"]()

fake_db = types.ModuleType("app.database")
fake_db.get_user_setting = lambda db, k, d="", u=None: d

gate = load("ira_gate", {**sql_stub(), "app.ira_data": fake_data, "app.claude_analyst": fake_ai,
                         "app.database": fake_db})

TARGET = {"VTI": 0.6, "BND": 0.4}
TGT = {"regime": "neutral", "target": TARGET}
BUY = [{"side": "buy", "symbol": "VTI", "notional": 6000.0, "drift_pct": -60.0}]
SELL = [{"side": "sell", "symbol": "VTI", "notional": 4000.0, "drift_pct": 40.0, "full": False}]
EMPTY = {"equity": 10000.0, "holdings": []}
INVESTED = {"equity": 10000.0, "holdings": [{"symbol": "VTI", "market_value": 9000.0, "weight": 0.9}]}


def approve():
    return '{"verdict": "APPROVE", "reason": "fine", "concerns": []}'


class GateTests(unittest.TestCase):
    def setUp(self):
        STATE.update(stats={}, er=0.05, stock=2.0, ai=approve)
        self.ai_calls = 0
        base = STATE["ai"]

        def counting():
            self.ai_calls += 1
            return STATE["ai_impl"]()
        STATE["ai_impl"] = base
        STATE["ai"] = counting

    def review(self, snap=EMPTY, plan=BUY):
        return gate.review(MagicMock(), 1, snap, TGT, plan, None)

    def test_no_trades_skips_ai(self):
        r = self.review(plan=[])
        self.assertEqual(r["verdict"], "NO_TRADES")
        self.assertEqual(self.ai_calls, 0)

    def test_ai_approval_passes(self):
        r = self.review()
        self.assertEqual(r["verdict"], "APPROVE")
        self.assertEqual(self.ai_calls, 1)

    def test_ai_block_blocks(self):
        STATE["ai_impl"] = lambda: '{"verdict": "BLOCK", "reason": "panic sell", "concerns": ["x"]}'
        self.assertEqual(self.review()["verdict"], "BLOCK")

    def test_stale_or_missing_prices_block_without_asking_ai(self):
        STATE["stats"] = {"VTI": fresh(stale=True)}
        r = self.review()
        self.assertEqual((r["verdict"], self.ai_calls), ("BLOCK", 0))
        STATE["stats"] = None
        self.assertEqual(self.review()["verdict"], "BLOCK")

    def test_expense_cap_blocks_even_if_ai_would_approve(self):
        STATE["er"] = 0.60
        r = self.review()
        self.assertEqual((r["verdict"], self.ai_calls), ("BLOCK", 0))
        self.assertIn("EXPENSE_CAP", [c["code"] for c in r["checks"]])

    def test_stock_concentration_blocks(self):
        STATE["stock"] = 12.0
        self.assertEqual(self.review()["verdict"], "BLOCK")

    def test_turnover_cap_blocks_normal_runs(self):
        r = self.review(snap=INVESTED, plan=SELL)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("TURNOVER_CAP", [c["code"] for c in r["checks"]])

    def test_initial_deployment_is_exempt_from_turnover_cap(self):
        self.assertTrue(self.review()["metrics"]["initial_deployment"])
        self.assertEqual(self.review()["verdict"], "APPROVE")

    def test_ai_error_fails_closed(self):
        def boom():
            raise RuntimeError("provider down")
        STATE["ai_impl"] = boom
        self.assertEqual(self.review()["verdict"], "UNAVAILABLE")

    def test_unusable_ai_output_fails_closed(self):
        for bad in ("", "I think this is fine!", '{"verdict": "MAYBE"}', "{not json}"):
            with self.subTest(bad=bad):
                STATE["ai_impl"] = lambda b=bad: b
                self.assertEqual(self.review()["verdict"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
