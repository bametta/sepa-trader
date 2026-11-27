"""
Historical validation for the IRA allocation rules.

Question answered: does the tactical layer (regime baseline, optionally with a per-ETF trend
overlay) beat a STATIC portfolio holding the same average equity share? Beating SPY or 60/40
is not the test - taking more risk would do that.

Data (extended mode, default): each ETF's returns are spliced to a long-history proxy before
the ETF existed, giving ~1999 onward (dot-com, GFC, COVID, 2022). Prices are total-return.
  VTI<-VTSMX  VXUS<-VGTSX  SCHD<-VIVAX  VNQ<-VGSIX  BND<-VBMFX  TIP<-VIPSX (from 2000-06)
  GLD<-GC=F gold futures (from 2000-08)  BIL<-^IRX T-bill yield. Before TIPS/gold data exist,
  TIP is replaced by BND and GLD by cash. QQQ and SPY have real history from 1999/1993.

Honest limits: the AI tilt and Dual Momentum are NOT backtested. The rules have no fitted
parameters, so rather than walk-forward optimisation this runs rolling-window win rates and a
sensitivity grid (SMA length, cadence, drift threshold, trading cost). Signals use data through
the decision day; trades fill the next trading day's close. Adjusted prices already include
fund expense ratios.
"""
import math
from datetime import datetime

import numpy as np
import pandas as pd

from .strategies import yf_client as yf
from .ira_data import EQUITY_BUCKETS, REFERENCE
from .ira_manager import BASELINES, DEFAULT_UNIVERSE, DEFENSIVE, normalize_weights

TRADING_DAYS = 252
WARMUP = 260  # trading days: longest SMA in the sensitivity grid is 250

PROXY = {"VTI": "VTSMX", "VXUS": "VGTSX", "SCHD": "VIVAX", "VNQ": "VGSIX",
         "BND": "VBMFX", "TIP": "VIPSX", "GLD": "GC=F", "BIL": "^IRX"}
SUBSTITUTE = {"TIP": "BND", "GLD": "BIL"}

STRESS = {
    "dotcom_2000_2002": ("2000-03-24", "2002-10-09"),
    "gfc_2007_2009": ("2007-10-09", "2009-03-09"),
    "covid_2020": ("2020-02-19", "2020-03-23"),
    "inflation_2022": ("2022-01-03", "2022-10-12"),
}

# Pre-specified bar a tactical strategy must clear against an equity-matched static mix.
CRITERIA = {"min_drawdown_improvement": 0.03, "max_cagr_giveup": 0.005,
            "min_stress_windows_better": 3, "min_sensitivity_pass_rate": 0.70}

GRID = {"sma": (150, 200, 250), "cadence": ("weekly", "monthly"),
        "threshold": (0.03, 0.05, 0.10), "cost_bps": (3.0, 10.0)}


# ── Data ──────────────────────────────────────────────────────────────────────

def _series(sym: str, start: str) -> pd.Series:
    days = (datetime.now() - datetime.fromisoformat(start)).days + 10
    df = yf.fetch_history(sym, days)
    if df is None or df.empty:
        raise ValueError(f"no price history for {sym}")
    s = df["Close"].astype(float)
    s.index = pd.to_datetime(s.index).normalize()
    return s[~s.index.duplicated(keep="last")].sort_index()


def _returns_on(index: pd.DatetimeIndex, s: pd.Series, is_yield: bool = False) -> pd.Series:
    aligned = s.reindex(index.union(s.index)).ffill().reindex(index)
    return (aligned / 100.0 / TRADING_DAYS) if is_yield else aligned.pct_change()


def load_market(universe: list[str], start: str, extended: bool):
    spy = _series("SPY", start)
    index = spy.index
    cols = list(dict.fromkeys(list(universe) + ["SPY"]))
    rets = {}
    for s in cols:
        r = _returns_on(index, _series(s, start))
        if extended and s in PROXY:
            r = r.combine_first(_returns_on(index, _series(PROXY[s], start), is_yield=(s == "BIL")))
        rets[s] = r
    df = pd.DataFrame(rets, index=index)
    if extended:
        for s, sub in SUBSTITUTE.items():
            if s in df and sub in df:
                df[s] = df[s].fillna(df[sub])
    first = df[universe].dropna().index[0]
    df = df.loc[first:].fillna(0.0)
    df.iloc[0] = 0.0
    vix_raw = _series("^VIX", start)
    vix = vix_raw.reindex(index.union(vix_raw.index)).ffill().reindex(df.index)
    return df, vix


# ── Signals / targets ─────────────────────────────────────────────────────────

def regime_array(px_spy: pd.Series, vix: pd.Series, sma_window: int) -> np.ndarray:
    sma = px_spy.rolling(sma_window).mean()
    above = (px_spy > sma).to_numpy()
    v = vix.to_numpy()
    out = np.full(len(px_spy), "neutral", dtype=object)
    out[above & (v < 30)] = "risk_on"
    out[(~above) & (v > 30)] = "risk_off"
    out[sma.isna().to_numpy()] = "neutral"
    return out


def _vec(weights: dict, cols: list[str]) -> np.ndarray:
    return np.array([weights.get(c, 0.0) for c in cols], dtype=float)


def build_regime_targets(px: pd.DataFrame, cols: list[str], universe: list[str],
                         sma_window: int, trend: bool, vix: pd.Series) -> np.ndarray:
    reg = regime_array(px["SPY"], vix, sma_window)
    base = {r: normalize_weights(BASELINES[r], universe) for r in BASELINES}
    sma = px[universe].rolling(sma_window).mean().to_numpy()
    price = px[universe].to_numpy()
    risky = [i for i, s in enumerate(universe) if s not in DEFENSIVE]
    cache: dict = {}
    out = np.zeros((len(px), len(cols)))
    for t in range(len(px)):
        r = reg[t]
        below = ()
        if trend:
            below = tuple(i for i in risky if base[r].get(universe[i], 0) > 0
                          and not np.isnan(sma[t, i]) and price[t, i] < sma[t, i])
        key = (r, below)
        if key not in cache:
            w = dict(base[r])
            freed = 0.0
            for i in below:
                freed += w[universe[i]] * 0.5
                w[universe[i]] *= 0.5
            if freed:
                w["BIL"] = w.get("BIL", 0.0) + freed
            n = normalize_weights(w, universe)
            cache[key] = _vec(n, cols)
        out[t] = cache[key]
    return out


def static_targets(weights, cols: list[str], T: int) -> np.ndarray:
    """Constant target every day. `weights` is a {symbol: weight} dict or an array aligned to cols."""
    v = np.asarray(weights, dtype=float) if isinstance(weights, np.ndarray) else _vec(weights, cols)
    return np.tile(v / v.sum(), (T, 1))


def matched_static_weights(equity_share: float) -> dict:
    e = min(max(equity_share, 0.0), 1.0)
    return {"VTI": 0.7 * e, "VXUS": 0.3 * e, "BND": 1 - e}


def equity_vector(cols: list[str]) -> np.ndarray:
    out = []
    for c in cols:
        exp = REFERENCE.get(c, {}).get("exposure", {"us_large": 1.0} if c == "SPY" else {})
        out.append(sum(v for b, v in exp.items() if b in EQUITY_BUCKETS))
    return np.array(out)


# ── Simulation ────────────────────────────────────────────────────────────────

def decision_mask(index: pd.DatetimeIndex, cadence: str) -> np.ndarray:
    pos = pd.Series(np.arange(len(index)), index=index)
    key = index.to_period("M") if cadence == "monthly" else index.to_period("W")
    mask = np.zeros(len(index), dtype=bool)
    mask[pos.groupby(key).max().to_numpy()] = True
    return mask


def simulate(rets: np.ndarray, targets: np.ndarray, start: int, decide: np.ndarray,
             threshold: float, cost_bps: float, eq_vec: np.ndarray):
    T = rets.shape[0]
    w = targets[start] / targets[start].sum()
    equity, pending = 1.0, None
    curve = np.empty(T - start)
    turnover = cost_total = eq_share = 0.0
    w_sum = np.zeros_like(w)
    for t in range(start, T):
        r = rets[t]
        equity *= 1.0 + float(w @ r)
        w = w * (1.0 + r)
        w /= w.sum()
        if pending is not None:
            turn = 0.5 * float(np.abs(pending - w).sum())
            cost = turn * 2.0 * cost_bps / 1e4
            equity *= 1.0 - cost
            turnover += turn
            cost_total += cost
            w, pending = pending, None
        if decide[t]:
            tgt = targets[t]
            if float(np.abs(tgt - w).max()) >= threshold:
                pending = tgt
        eq_share += float(w @ eq_vec)
        w_sum += w
        curve[t - start] = equity
    avg_w = w_sum / (T - start)
    return curve, turnover, cost_total, eq_share / (T - start), avg_w / avg_w.sum()


# ── Metrics ───────────────────────────────────────────────────────────────────

def _max_dd(values: np.ndarray) -> float:
    peak = np.maximum.accumulate(values)
    return float((values / peak - 1.0).min())


def metrics(curve: pd.Series, rf: pd.Series, turnover: float, cost: float) -> dict:
    rets = curve.pct_change().dropna()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
    sd = rets.std()
    ex = rets - rf.reindex(rets.index).fillna(0.0)
    mdd = _max_dd(curve.to_numpy())
    yearly = curve.groupby(curve.index.year).last()
    yr = yearly.pct_change()
    yr.iloc[0] = yearly.iloc[0] / curve.iloc[0] - 1
    return {
        "total_return": round(float(curve.iloc[-1] / curve.iloc[0] - 1), 4),
        "cagr": round(float(cagr), 4),
        "volatility": round(float(sd * math.sqrt(TRADING_DAYS)), 4),
        "sharpe": round(float(ex.mean() / sd * math.sqrt(TRADING_DAYS)), 2) if sd > 0 else None,
        "max_drawdown": round(mdd, 4),
        "calmar": round(float(cagr / abs(mdd)), 2) if mdd < 0 else None,
        "turnover_per_year": round(turnover / years, 3),
        "trading_cost_drag_per_year": round(cost / years, 5),
        "worst_year": {"year": int(yr.idxmin()), "return": round(float(yr.min()), 4)},
        "calendar_years": {int(y): round(float(v), 4) for y, v in yr.items()},
    }


def window_stats(curve: pd.Series, a: str, b: str) -> dict | None:
    seg = curve.loc[a:b]
    if len(seg) < 5:
        return None
    return {"return": round(float(seg.iloc[-1] / seg.iloc[0] - 1), 4),
            "max_drawdown": round(_max_dd(seg.to_numpy()), 4)}


def rolling_vs(strategy: pd.Series, static: pd.Series, years: int) -> dict:
    n = years * TRADING_DAYS
    ends = strategy.resample("ME").last().index
    ends = [d for d in ends if strategy.index.get_indexer([d], method="pad")[0] >= n]
    wins_ret = wins_dd = 0
    excess = []
    for d in ends:
        i = strategy.index.get_indexer([d], method="pad")[0]
        s, m = strategy.iloc[i - n:i + 1].to_numpy(), static.iloc[i - n:i + 1].to_numpy()
        rs, rm = s[-1] / s[0] - 1, m[-1] / m[0] - 1
        excess.append(rs - rm)
        wins_ret += rs > rm
        wins_dd += _max_dd(s) > _max_dd(m)  # shallower drawdown
    k = len(ends)
    if not k:
        return {"windows": 0}
    return {"windows": k, "beat_on_return": round(wins_ret / k, 3), "shallower_drawdown": round(wins_dd / k, 3),
            "median_excess_return": round(float(np.median(excess)), 4)}


# ── Orchestration ─────────────────────────────────────────────────────────────

def _clean(o):
    """Make the result JSON-safe: numpy scalars -> python, NaN/inf -> None."""
    if isinstance(o, dict):
        return {(int(k) if isinstance(k, np.integer) else k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    return o


def run_backtest(start: str | None = None, cadence: str = "weekly", threshold: float = 0.05,
                 cost_bps: float = 3.0, universe: list[str] | None = None,
                 extended: bool = True, sensitivity: bool = True) -> dict:
    universe = universe or DEFAULT_UNIVERSE
    start = start or ("1999-01-01" if extended else "2011-06-01")
    rets_df, vix = load_market(universe, start, extended)
    cols = list(rets_df.columns)
    px = (1.0 + rets_df).cumprod()
    rets = rets_df.to_numpy()
    dates = rets_df.index
    T = len(dates)
    if T < WARMUP + 2 * TRADING_DAYS:
        raise ValueError("not enough overlapping history for the universe")
    start_pos = WARMUP
    rf = rets_df["BIL"]
    eq_vec = equity_vector(cols)
    masks = {c: decision_mask(dates, c) for c in ("weekly", "monthly")}
    target_cache: dict = {}

    def regime_targets(sma, trend):
        k = (sma, trend)
        if k not in target_cache:
            target_cache[k] = build_regime_targets(px, cols, universe, sma, trend, vix)
        return target_cache[k]

    def run(targets, cad=cadence, thr=threshold, cost=cost_bps):
        return simulate(rets, targets, start_pos, masks[cad], thr, cost, eq_vec)

    idx = dates[start_pos:]
    benchmarks = {
        "benchmark_60_40": {"VTI": .6, "BND": .4},
        "benchmark_glide_75": {"VTI": .50, "VXUS": .25, "BND": .20, "BIL": .05},
        "benchmark_3fund": {"VTI": .45, "VXUS": .20, "BND": .35},
        "benchmark_spy": {"SPY": 1.0},
    }
    strategies = {"regime_baseline": (200, False), "regime_trend": (200, True)}

    results, curves, matched, matched_curves, simple, shares = {}, {}, {}, {}, {}, {}

    def record(name, targets):
        c, turn, cost, share, avg_w = run(targets)
        ser = pd.Series(c, index=idx)
        m = metrics(ser, rf, turn, cost)
        m["avg_equity_share"] = round(share, 3)
        m["avg_weights"] = {col: round(float(v), 3) for col, v in zip(cols, avg_w) if v > 0.0005}
        m["curve"] = [[d.date().isoformat(), round(float(v) * 100, 2)] for d, v in ser.resample("ME").last().items()]
        results[name], curves[name], shares[name] = m, ser, share
        return ser, share, avg_w

    for name, (sma, trend) in strategies.items():
        _, share, avg_w = record(name, regime_targets(sma, trend))
        # Primary control: a STATIC portfolio holding the strategy's time-average weight in every ETF.
        # Same sleeves (gold, TIPS, cash...) and same average equity, so any gap is timing, not asset mix.
        mc, turn, cost, _, _ = run(static_targets(avg_w, cols, T))
        mser = pd.Series(mc, index=idx)
        matched[name] = metrics(mser, rf, turn, cost)
        matched[name]["equity_share"] = round(share, 3)
        matched_curves[name] = mser
        # Secondary: simple stock/bond mix with the same equity share.
        sc, turn, cost, _, _ = run(static_targets(matched_static_weights(share), cols, T))
        simple[name] = metrics(pd.Series(sc, index=idx), rf, turn, cost)
    for name, w in benchmarks.items():
        record(name, static_targets(w, cols, T))

    stress = {}
    for label, (a, b) in STRESS.items():
        row = {}
        for name in list(strategies) + list(benchmarks):
            row[name] = window_stats(curves[name], a, b)
        for name in strategies:
            row[f"matched_static_for_{name}"] = window_stats(matched_curves[name], a, b)
        if any(v for v in row.values()):
            stress[label] = row

    rolling = {f"{y}y": {name: rolling_vs(curves[name], matched_curves[name], y) for name in strategies}
               for y in (3, 5)}

    sens = {"configs": 0, "by_strategy": {}}
    if sensitivity:
        for name, (_, trend) in strategies.items():
            passes = total = 0
            rows = []
            for sma in GRID["sma"]:
                tg = regime_targets(sma, trend)
                for cad in GRID["cadence"]:
                    for thr in GRID["threshold"]:
                        for cost in GRID["cost_bps"]:
                            c, _, _, _, avg_w = run(tg, cad, thr, cost)
                            mc, _, _, _, _ = run(static_targets(avg_w, cols, T), cad, thr, cost)
                            yrs = (idx[-1] - idx[0]).days / 365.25
                            cagr, mcagr = (c[-1] / c[0]) ** (1 / yrs) - 1, (mc[-1] / mc[0]) ** (1 / yrs) - 1
                            dd, mdd = _max_dd(c), _max_dd(mc)
                            ok = (dd - mdd >= CRITERIA["min_drawdown_improvement"]
                                  and cagr - mcagr >= -CRITERIA["max_cagr_giveup"])
                            passes += ok
                            total += 1
                            rows.append({"sma": sma, "cadence": cad, "threshold": thr, "cost_bps": cost,
                                         "cagr": round(float(cagr), 4), "max_dd": round(dd, 4),
                                         "vs_matched_cagr": round(float(cagr - mcagr), 4),
                                         "vs_matched_dd": round(dd - mdd, 4), "passes": bool(ok)})
            sens["by_strategy"][name] = {
                "pass_rate": round(passes / total, 3), "configs": total,
                "cagr_range": [min(r["cagr"] for r in rows), max(r["cagr"] for r in rows)],
                "max_dd_range": [min(r["max_dd"] for r in rows), max(r["max_dd"] for r in rows)],
                "grid": rows,
            }
            sens["configs"] = total

    verdict = {}
    for name in strategies:
        r, mm = results[name], matched[name]
        dd_gain = r["max_drawdown"] - mm["max_drawdown"]
        cagr_delta = r["cagr"] - mm["cagr"]
        better = sum(1 for lab, row in stress.items()
                     if row.get(name) and row.get(f"matched_static_for_{name}")
                     and row[name]["max_drawdown"] > row[f"matched_static_for_{name}"]["max_drawdown"])
        sens_rate = sens["by_strategy"].get(name, {}).get("pass_rate")
        checks = {
            "drawdown_improvement": dd_gain >= CRITERIA["min_drawdown_improvement"],
            "cagr_giveup_ok": cagr_delta >= -CRITERIA["max_cagr_giveup"],
            "stress_windows_better": better >= min(CRITERIA["min_stress_windows_better"], len(stress)),
            "robust_to_parameters": (sens_rate is not None and sens_rate >= CRITERIA["min_sensitivity_pass_rate"]),
        }
        verdict[name] = {"earns_its_place": all(checks.values()), "checks": checks,
                         "drawdown_vs_matched_static": round(dd_gain, 4), "cagr_vs_matched_static": round(cagr_delta, 4),
                         "stress_windows_with_shallower_drawdown": f"{better}/{len(stress)}",
                         "sensitivity_pass_rate": sens_rate}

    return _clean({
        "period": {"start": idx[0].date().isoformat(), "end": idx[-1].date().isoformat()},
        "data_mode": "extended (proxy-spliced)" if extended else "etf_only",
        "assumptions": {"cadence": cadence, "rebalance_threshold": threshold, "cost_bps_per_side": cost_bps,
                        "universe": universe, "criteria": CRITERIA, "grid": GRID,
                        "proxies": PROXY if extended else {}, "substitutes": SUBSTITUTE if extended else {}},
        "results": results, "matched_static": matched, "matched_simple_equity": simple,
        "stress": stress, "rolling": rolling,
        "sensitivity": sens, "verdict": verdict,
        "notes": [
            "The control is a STATIC portfolio holding the strategy's time-average weight in every ETF "
            "(same gold/TIPS/cash sleeves and equity share), so a gap reflects timing, not asset mix.",
            "AI tilt and Dual Momentum overlay are not backtested; this validates the rules-based core only.",
            "Proxy history (mutual funds, gold futures, T-bill yield) approximates the ETFs; before mid-2000 TIPS "
            "are replaced by BND and gold by cash.",
            "Rules have no fitted parameters, so robustness is shown via rolling windows and a sensitivity grid "
            "rather than walk-forward optimisation.",
            "Signals use data through the decision day; trades fill the next trading day's close.",
        ],
    })
