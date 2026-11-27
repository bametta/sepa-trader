"""Backtest engine mechanics on synthetic data (no network). Needs numpy + pandas."""
import types
import unittest
from unittest.mock import MagicMock

try:
    import numpy as np
    import pandas as pd
except ImportError:
    np = pd = None

from tests._loader import load_manager, load


def _load_backtest():
    load_manager()
    strategies = types.ModuleType("app.strategies")
    strategies.__path__ = []
    strategies.yf_client = MagicMock()
    data = types.ModuleType("app.ira_data")
    data.EQUITY_BUCKETS = {"us_large", "us_midsmall", "intl_dev", "intl_em", "reit"}
    data.REFERENCE = {"VTI": {"exposure": {"us_large": 1.0}}, "BND": {"exposure": {"bonds_agg": 1.0}},
                      "BIL": {"exposure": {"cash": 1.0}}}
    return load("ira_backtest", {"app.strategies": strategies, "app.strategies.yf_client": strategies.yf_client,
                                 "app.ira_data": data})


@unittest.skipIf(np is None, "numpy/pandas not installed")
class BacktestEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = _load_backtest()

    def sim(self, rets, targets, decide=None, threshold=0.05, cost=0.0, start=0):
        rets, targets = np.array(rets, float), np.array(targets, float)
        decide = np.ones(len(rets), bool) if decide is None else np.array(decide, bool)
        return self.b.simulate(rets, targets, start, decide, threshold, cost, np.array([1.0, 0.0]))

    def test_static_without_trades_is_buy_and_hold(self):
        rets = [[0.0, 0.0], [0.10, 0.0], [0.10, 0.0]]
        tg = [[0.5, 0.5]] * 3
        curve, turn, cost, _, _ = self.sim(rets, tg, threshold=9.9)
        self.assertAlmostEqual(curve[-1], 0.5 * 1.1 * 1.1 + 0.5, places=9)
        self.assertEqual(turn, 0.0)

    def test_trade_executes_the_day_after_the_signal(self):
        # Decision on day 1 to move 100% into asset A; A jumps +10% on day 2. The move fills at the
        # close of day 2, so the portfolio must NOT capture day 2's jump.
        rets = [[0, 0], [0, 0], [0.10, 0.0], [0.0, 0.0]]
        tg = [[0, 1], [1, 0], [1, 0], [1, 0]]
        decide = [False, True, False, False]
        curve, *_ = self.sim(rets, tg, decide=decide)
        self.assertAlmostEqual(curve[-1], 1.0, places=9)

    def test_costs_reduce_final_equity(self):
        rng = np.random.default_rng(0)
        rets = rng.normal(0.0003, 0.01, (400, 2))
        tg = np.tile([[0.6, 0.4]], (400, 1))
        tg[::10] = [0.2, 0.8]
        free = self.sim(rets, tg, threshold=0.01, cost=0.0)
        paid = self.sim(rets, tg, threshold=0.01, cost=25.0)
        self.assertLess(paid[0][-1], free[0][-1])
        self.assertGreater(paid[2], 0.0)

    def test_drift_threshold_suppresses_small_trades(self):
        rets = [[0.0, 0.0]] * 5
        tg = [[0.52, 0.48]] * 5
        tg[0] = [0.5, 0.5]
        _, turn, *_ = self.sim(rets, tg, threshold=0.05, start=0)
        self.assertEqual(turn, 0.0)

    def test_average_weights_returned_and_normalised(self):
        curve, _, _, share, avg_w = self.sim([[0.0, 0.0]] * 4, [[0.7, 0.3]] * 4)
        self.assertAlmostEqual(avg_w.sum(), 1.0)
        self.assertAlmostEqual(share, 0.7, places=6)

    def test_decision_mask_picks_last_trading_day_of_each_week(self):
        idx = pd.bdate_range("2024-01-01", periods=15)
        mask = self.b.decision_mask(idx, "weekly")
        self.assertEqual([d.weekday() for d in idx[mask]], [4, 4, 4])
        self.assertEqual(self.b.decision_mask(idx, "monthly").sum(), 1)

    def test_regime_rules(self):
        idx = pd.bdate_range("2020-01-01", periods=260)
        up = pd.Series(np.linspace(100, 200, 260), index=idx)
        down = pd.Series(np.linspace(200, 100, 260), index=idx)
        calm, stress = pd.Series(15.0, index=idx), pd.Series(40.0, index=idx)
        self.assertEqual(self.b.regime_array(up, calm, 200)[-1], "risk_on")
        self.assertEqual(self.b.regime_array(down, stress, 200)[-1], "risk_off")
        self.assertEqual(self.b.regime_array(down, calm, 200)[-1], "neutral")   # correction caps at neutral
        self.assertEqual(self.b.regime_array(up, stress, 200)[-1], "neutral")
        self.assertEqual(self.b.regime_array(up, calm, 200)[0], "neutral")      # no SMA yet

    def test_trend_overlay_halves_broken_etf_and_moves_weight_to_cash(self):
        universe = list(self.b.DEFAULT_UNIVERSE)
        idx = pd.bdate_range("2020-01-01", periods=260)
        px = pd.DataFrame({s: np.linspace(100, 200, 260) for s in universe + ["SPY"]}, index=idx)
        px["QQQ"] = np.linspace(200, 100, 260)  # downtrend: below its own SMA
        vix = pd.Series(15.0, index=idx)
        cols = universe + ["SPY"]
        plain = self.b.build_regime_targets(px, cols, universe, 200, False, vix)[-1]
        trend = self.b.build_regime_targets(px, cols, universe, 200, True, vix)[-1]
        q, bil = cols.index("QQQ"), cols.index("BIL")
        self.assertLess(trend[q], plain[q])
        self.assertGreater(trend[bil], plain[bil])
        self.assertAlmostEqual(trend.sum(), 1.0, places=6)
        self.assertAlmostEqual(plain.sum(), 1.0, places=6)

    def test_live_overlay_matches_the_validated_backtest_exactly(self):
        """What runs live must be what was validated: same weights from the same market state."""
        import sys
        live = sys.modules["app.ira_manager"]
        universe = list(self.b.DEFAULT_UNIVERSE)
        cols = universe + ["SPY"]
        idx = pd.bdate_range("2020-01-01", periods=300)
        rng = np.random.default_rng(5)
        for trial in range(30):
            drift = {s: rng.choice([-0.002, 0.002]) for s in cols}
            px = pd.DataFrame({s: 100 * np.exp(np.cumsum(rng.normal(drift[s], 0.004, 300))) for s in cols}, index=idx)
            px["SPY"] = np.linspace(100, 200, 300)          # calm uptrend -> risk_on
            vix = pd.Series(15.0, index=idx)
            bt_row = self.b.build_regime_targets(px, cols, universe, 200, True, vix)[-1]
            stats = {s: {"above_200sma": bool(px[s].iloc[-1] > px[s].tail(200).mean())} for s in universe}
            base = live.normalize_weights(live.BASELINES["risk_on"], universe)
            weights, _ = live.apply_trend_overlay(base, stats, universe)
            live_row = np.array([weights.get(c, 0.0) for c in cols])
            np.testing.assert_allclose(live_row, bt_row, atol=1e-9, err_msg=f"trial {trial}")

    def test_metrics_max_drawdown(self):
        idx = pd.bdate_range("2020-01-01", periods=5)
        curve = pd.Series([1.0, 1.2, 0.6, 0.9, 1.0], index=idx)
        m = self.b.metrics(curve, pd.Series(0.0, index=idx), 0.0, 0.0)
        self.assertAlmostEqual(m["max_drawdown"], -0.5)

    def test_rolling_window_ties_are_not_wins(self):
        idx = pd.bdate_range("2015-01-01", periods=1500)
        s = pd.Series(np.linspace(1, 2, 1500), index=idx)
        r = self.b.rolling_vs(s, s.copy(), 2)
        self.assertEqual(r["beat_on_return"], 0.0)
        self.assertEqual(r["shallower_drawdown"], 0.0)

    def test_clean_makes_results_json_safe(self):
        import json
        out = self.b._clean({"a": np.float64("nan"), "b": np.bool_(True), "c": np.int64(3), 2020: [np.float32(1.5)]})
        json.dumps(out)
        self.assertIsNone(out["a"])
        self.assertIs(out["b"], True)

    def test_matched_static_weights_sum_to_one(self):
        w = self.b.matched_static_weights(0.74)
        self.assertAlmostEqual(sum(w.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
