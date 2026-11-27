"""
Historical backtest for the IRA allocation rules.

Compares, on dividend-adjusted (total-return) prices:
  - regime_baseline : regime from SPY 200-day SMA + VIX (same rules as market_env)
  - regime_trend    : regime_baseline + halve any risk ETF trading below its own 200-day SMA
  - benchmarks      : 60/40 (VTI/BND), 3-fund (VTI/VXUS/BND), SPY buy-and-hold

Honest limits: the AI tilt and Dual Momentum overlay are NOT backtested (no point-in-time
AI output exists), and the universe only has full history from ~2012 (one bull-dominated
regime). Adjusted prices already include each fund's expense ratio, so it isn't deducted
again; trading costs are modelled separately. Signals use data through the decision day's
close and trades fill at the NEXT day's close.
"""
import math
from datetime import datetime

import pandas as pd

from .strategies import yf_client as yf
from .ira_manager import BASELINES, DEFAULT_UNIVERSE, DEFENSIVE, normalize_weights

TRADING_DAYS = 252
WARMUP = 210  # trading days for 200-day SMAs


def load_prices(symbols, start: str = "2011-06-01") -> pd.DataFrame:
    days = (datetime.now() - datetime.fromisoformat(start)).days
    cols = {}
    for s in symbols:
        df = yf.fetch_history(s, days)
        if df is None or df.empty:
            raise ValueError(f"no price history for {s}")
        ser = df["Close"].astype(float)
        ser.index = pd.to_datetime(ser.index).normalize()
        cols[s] = ser[~ser.index.duplicated(keep="last")]
    return pd.DataFrame(cols).sort_index().ffill()


def regime_series(spy: pd.Series, vix: pd.Series) -> pd.Series:
    """BULL/BULL_VOLATILE -> risk_on, BEAR -> risk_off, else neutral (market_env rules)."""
    sma = spy.rolling(200).mean()
    above = spy > sma
    vix = vix.reindex(spy.index).ffill()
    out = pd.Series("neutral", index=spy.index)
    out[above & (vix < 30)] = "risk_on"
    out[(~above) & (vix > 30)] = "risk_off"
    out[sma.isna()] = "neutral"
    return out


def _normalized(weights: dict, universe: list[str]) -> pd.Series:
    n = normalize_weights(weights, universe)
    return pd.Series({s: n.get(s, 0.0) for s in universe})


def make_regime_target(px, regimes, universe, trend: bool):
    sma200 = px[universe].rolling(200).mean()

    def target(d) -> pd.Series:
        w = dict(BASELINES[regimes.loc[d]])
        if trend:
            freed = 0.0
            for s in universe:
                if s in DEFENSIVE or w.get(s, 0) <= 0:
                    continue
                sma = sma200.loc[d, s]
                if pd.notna(sma) and px.loc[d, s] < sma:
                    freed += w[s] * 0.5
                    w[s] *= 0.5
            w["BIL"] = w.get("BIL", 0.0) + freed
        return _normalized(w, universe)

    return target


def make_static_target(weights: dict, universe: list[str]):
    fixed = pd.Series({s: weights.get(s, 0.0) for s in universe})
    fixed = fixed / fixed.sum()
    return lambda d: fixed


def _decision_dates(index: pd.DatetimeIndex, cadence: str) -> set:
    s = pd.Series(index, index=index)
    key = s.dt.to_period("M") if cadence == "monthly" else s.dt.to_period("W")
    return set(s.groupby(key).last())


def simulate(px, target_fn, start_pos: int, cadence="weekly", threshold=0.05, cost_bps=3.0):
    cols = list(px.columns)
    rets = px.pct_change().fillna(0.0)
    dates = px.index
    decide = _decision_dates(dates, cadence)

    w = target_fn(dates[start_pos]).reindex(cols).fillna(0.0)
    equity, curve, turnover_total, cost_total, pending = 1.0, {}, 0.0, 0.0, None
    for i in range(start_pos, len(dates)):
        d = dates[i]
        r = rets.loc[d]
        gross = float((w * r).sum())
        w = w * (1 + r)
        w = w / w.sum()
        equity *= 1 + gross
        if pending is not None:
            turn = float((pending - w).abs().sum()) / 2.0
            cost = turn * 2 * cost_bps / 1e4  # buys + sells each pay the half-spread/slippage
            equity *= 1 - cost
            turnover_total += turn
            cost_total += cost
            w, pending = pending, None
        if d in decide:
            tgt = target_fn(d).reindex(cols).fillna(0.0)
            if float((tgt - w).abs().max()) >= threshold:
                pending = tgt
        curve[d] = equity
    return pd.Series(curve), turnover_total, cost_total


def _metrics(curve: pd.Series, rf: pd.Series, turnover: float, cost: float) -> dict:
    rets = curve.pct_change().dropna()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
    vol = float(rets.std() * math.sqrt(TRADING_DAYS))
    ex = rets - rf.reindex(rets.index).fillna(0.0)
    sharpe = float(ex.mean() / rets.std() * math.sqrt(TRADING_DAYS)) if rets.std() > 0 else None
    dd = curve / curve.cummax() - 1
    yearly = curve.groupby(curve.index.year).last().pct_change()
    yearly.iloc[0] = curve.groupby(curve.index.year).last().iloc[0] / curve.iloc[0] - 1
    return {
        "total_return": round(float(curve.iloc[-1] / curve.iloc[0] - 1), 4),
        "cagr": round(float(cagr), 4),
        "volatility": round(vol, 4),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown": round(float(dd.min()), 4),
        "calmar": round(float(cagr / abs(dd.min())), 2) if dd.min() < 0 else None,
        "turnover_per_year": round(turnover / years, 3),
        "trading_cost_drag_per_year": round(cost / years, 5),
        "worst_year": {"year": int(yearly.idxmin()), "return": round(float(yearly.min()), 4)},
        "calendar_years": {int(y): round(float(v), 4) for y, v in yearly.items()},
    }


def run_backtest(start: str = "2011-06-01", cadence: str = "weekly",
                 threshold: float = 0.05, cost_bps: float = 3.0,
                 universe: list[str] | None = None) -> dict:
    universe = universe or DEFAULT_UNIVERSE
    px = load_prices(sorted(set(universe) | {"SPY", "BIL"}), start)
    vix = load_prices(["^VIX"], start)["^VIX"]
    regimes = regime_series(px["SPY"], vix)

    first_full = px[universe].dropna().index[0]
    start_pos = max(px.index.get_loc(first_full) + WARMUP, WARMUP)
    if start_pos >= len(px) - TRADING_DAYS:
        raise ValueError("not enough overlapping history for the universe")

    rf = px["BIL"].pct_change().fillna(0.0)
    strategies = {
        "regime_baseline": make_regime_target(px, regimes, universe, trend=False),
        "regime_trend":    make_regime_target(px, regimes, universe, trend=True),
        "benchmark_60_40": make_static_target({"VTI": .6, "BND": .4}, list(px.columns)),
        "benchmark_3fund": make_static_target({"VTI": .45, "VXUS": .20, "BND": .35}, list(px.columns)),
        "benchmark_spy":   make_static_target({"SPY": 1.0}, list(px.columns)),
    }
    results = {}
    for name, fn in strategies.items():
        curve, turn, cost = simulate(px, fn, start_pos, cadence, threshold, cost_bps)
        m = _metrics(curve, rf, turn, cost)
        monthly = curve.resample("ME").last()
        m["curve"] = [[d.date().isoformat(), round(float(v) * 100, 2)] for d, v in monthly.items()]
        results[name] = m

    reg_share = regimes.iloc[start_pos:].value_counts(normalize=True).round(3).to_dict()
    return {
        "period": {"start": px.index[start_pos].date().isoformat(), "end": px.index[-1].date().isoformat()},
        "assumptions": {"cadence": cadence, "rebalance_threshold": threshold, "cost_bps_per_side": cost_bps,
                        "universe": universe, "regime_days_share": reg_share},
        "results": results,
        "notes": [
            "AI tilt and Dual Momentum overlay are not backtested; this validates the rules-based core only.",
            "Prices are dividend-adjusted, so fund expense ratios are already reflected in returns.",
            "History covers ~2012 onward: mostly a bull market, with 2018, 2020 and 2022 as the main stress tests.",
            "Signals use data through the decision day; trades fill the next trading day's close.",
        ],
    }
