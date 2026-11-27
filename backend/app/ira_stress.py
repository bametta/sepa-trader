"""
Sequence-of-returns stress tests for the retirement plan (real, inflation-adjusted dollars).

- scenario_projection: deterministic crash/lost-decade/stagflation paths on the accumulation phase
- withdrawal_test:     retirement phase, Monte Carlo success rate, a bad-first-years path, and the
                       highest withdrawal rate that still succeeds 90% of the time
- historical_replay:   real market return sequences (1996+) replayed against the plan; needs network

Planning aid only, not financial advice. Scenario shocks are illustrative, not forecasts.
"""
import time

import numpy as np

from .ira_planner import ASSUMPTIONS, glidepath_equity, mix_stats, strategic_mix

SAFE_RATE_CONFIDENCE = 0.90

# Per-year REAL return overrides by asset class. A scenario is {year_offset: {class: return}}, where a
# negative offset counts back from the final year of the phase (-1 = last year).
SCENARIOS = {
    "crash_two_years_before_retirement": {"label": "Equity -35% two years before retirement",
                                          "shocks": {-2: {"equity": -0.35, "bonds": 0.03, "gold": 0.05, "cash": 0.01}}},
    "crash_final_year": {"label": "Equity -40% in the final year",
                         "shocks": {-1: {"equity": -0.40, "bonds": 0.02, "gold": 0.05, "cash": 0.01}}},
    "lost_decade_early": {"label": "Zero real equity return for the first 10 years",
                          "shocks": {i: {"equity": 0.0} for i in range(10)}},
    "stagflation_midway": {"label": "2022-style: stocks and bonds both fall, midway",
                           "shocks": {"mid": {"equity": -0.20, "bonds": -0.13},
                                      "mid+1": {"equity": -0.05, "bonds": -0.05}}},
}


def _year_returns(mix: dict, overrides: dict | None) -> float:
    base = {c: ASSUMPTIONS[c][0] for c in mix}
    if overrides:
        base.update({c: r for c, r in overrides.items() if c in base})
    return sum(mix[c] * base[c] for c in mix)


def _resolve(key, years: int) -> int | None:
    if key == "mid":
        return years // 2
    if key == "mid+1":
        return years // 2 + 1
    idx = key if key >= 0 else years + key
    return idx if 0 <= idx < years else None


def project(balance: float, annual, years: int, mix: dict, shocks: dict | None = None) -> float:
    """`annual` is a constant yearly contribution or a per-year sequence of length `years`."""
    by_year = {}
    for key, over in (shocks or {}).items():
        idx = _resolve(key, years)
        if idx is not None:
            by_year[idx] = over
    per_year = np.broadcast_to(np.asarray(annual, dtype=float), (years,))
    bal = float(balance)
    for y in range(years):
        bal = (bal + per_year[y]) * (1 + _year_returns(mix, by_year.get(y)))
    return bal


def scenario_projection(balance: float, annual, years: int, mix: dict, target: float | None) -> list[dict]:
    expected = project(balance, annual, years, mix)
    rows = [{"key": "expected", "label": "Expected returns, no shock", "final": round(expected),
             "vs_expected": 0.0, "pct_of_target": round(expected / target, 3) if target else None,
             "meets_target": bool(expected >= target) if target else None}]
    for key, sc in SCENARIOS.items():
        final = project(balance, annual, years, mix, sc["shocks"])
        rows.append({"key": key, "label": sc["label"], "final": round(final),
                     "vs_expected": round(final / expected - 1, 3) if expected else None,
                     "pct_of_target": round(final / target, 3) if target else None,
                     "meets_target": bool(final >= target) if target else None})
    return rows


def _success(z: np.ndarray, target: float, rate: float, mu: float, sigma: float) -> tuple[float, np.ndarray]:
    log_mu = np.log(1 + mu) - 0.5 * (sigma / (1 + mu)) ** 2
    log_sd = sigma / (1 + mu)
    bal = np.full(z.shape[0], float(target))
    alive = np.ones(z.shape[0], dtype=bool)
    spend = target * rate
    for y in range(z.shape[1]):
        bal = bal - spend                      # withdraw at the start of each year (conservative)
        alive &= bal > 0
        bal = np.maximum(bal, 0.0) * np.exp(log_mu + log_sd * z[:, y])
    return float(alive.mean()), bal


def withdrawal_test(target: float, retire_age: int, risk: str, rate: float = 0.04,
                    horizon: int = 30, n: int = 4000, seed: int = 11) -> dict:
    """How long a retirement pot lasts at a constant real withdrawal, using the age-appropriate retirement mix."""
    mix = strategic_mix(glidepath_equity(retire_age, retire_age, risk))
    mu, sigma = mix_stats(mix)
    z = np.random.default_rng(seed).standard_normal((n, horizon))
    prob, end_bal = _success(z, target, rate, mu, sigma)

    lo, hi = 0.02, 0.08                          # highest rate with >= 90% success (common random numbers)
    if _success(z, target, lo, mu, sigma)[0] < SAFE_RATE_CONFIDENCE:
        safe = None
    else:
        for _ in range(16):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if _success(z, target, mid, mu, sigma)[0] >= SAFE_RATE_CONFIDENCE else (lo, mid)
        safe = lo

    bad, survived = float(target), horizon       # deterministic: equity -30%, -15% in the first two years
    for y in range(horizon):
        bad -= target * rate
        if bad <= 0:
            survived, bad = y, 0.0
            break
        over = {0: {"equity": -0.30}, 1: {"equity": -0.15}}.get(y)
        bad *= 1 + _year_returns(mix, over)
    return {
        "retirement_mix": {k: round(v, 3) for k, v in mix.items()},
        "expected_real_return": round(mu, 4), "volatility": round(sigma, 4),
        "withdrawal_rate": rate, "annual_spend": round(target * rate), "horizon_years": horizon,
        "success_probability": round(prob, 3),
        "median_ending_balance": round(float(np.median(end_bal))),
        "safe_rate_90pct": round(safe, 4) if safe is not None else None,
        "bad_start": {"description": "Equity -30% then -15% in the first two years of retirement",
                      "years_funded": survived, "ending_balance": round(bad)},
    }


# ── Historical replay (network) ───────────────────────────────────────────────

_cache: dict = {}
INFLATION = 0.025  # constant assumption used to deflate nominal history (~US average since 1996)


def _monthly_returns():
    import pandas as pd
    from .strategies import yf_client as yf

    def month_end(sym):
        df = yf.fetch_history(sym, 11500)
        if df is None or df.empty:
            raise ValueError(f"no history for {sym}")
        s = df["Close"].astype(float)
        s.index = pd.to_datetime(s.index).normalize()
        return s[~s.index.duplicated(keep="last")].resample("ME").last()

    us, intl, bond = (month_end(s).pct_change() for s in ("VTSMX", "VGTSX", "VBMFX"))
    irx = month_end("^IRX") / 100.0 / 12.0
    return pd.concat({"us": us, "intl": intl, "bond": bond, "cash": irx}, axis=1).dropna()


def historical_replay(equity_share: float, years: int, annual: float, balance: float,
                      target: float | None) -> dict:
    key = (round(equity_share, 2), years, round(annual), round(balance), round(target or 0))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 24 * 3600:
        return hit[1]
    try:
        df = _monthly_returns()
    except Exception as exc:
        return {"available": False, "reason": f"market history unavailable: {str(exc)[:120]}"}
    rest = 1 - equity_share
    port = equity_share * (0.7 * df["us"] + 0.3 * df["intl"]) + rest * 0.85 * df["bond"] + rest * 0.15 * df["cash"]
    real = (1 + port) / (1 + INFLATION) ** (1 / 12) - 1
    r = real.to_numpy()
    n = len(r)
    used = min(years, n // 12 - 1)
    months = used * 12
    if months < 24:
        return {"available": False, "reason": "not enough history"}
    contrib = annual / 12.0
    finals, starts = [], []
    for s in range(0, n - months + 1):
        bal = float(balance)
        for m in range(months):
            bal = (bal + contrib) * (1 + r[s + m])
        finals.append(bal)
        starts.append(real.index[s])
    f = np.array(finals)
    worst = int(np.argmin(f))
    out = {
        "available": True, "window_years_used": used, "requested_years": years,
        "history": f"{real.index[0].date()} to {real.index[-1].date()}", "windows": len(f),
        "worst": {"final": round(float(f.min())), "starting": starts[worst].date().isoformat()},
        "p10": round(float(np.percentile(f, 10))), "median": round(float(np.median(f))),
        "best": round(float(f.max())),
        "share_reaching_target": round(float((f >= target).mean()), 3) if target else None,
        "note": ("Overlapping windows from ~30 years of history are not independent samples, and returns are "
                 f"deflated at a constant {INFLATION:.1%} inflation. Use as a sanity check, not a forecast."
                 + (f" Window shortened from {years} to {used} years because history is limited." if used < years else "")),
    }
    _cache[key] = (time.time(), out)
    return out
