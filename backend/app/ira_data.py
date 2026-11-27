"""
IRA reference data and live market inputs.

- REFERENCE: static fund facts (expense ratio, category, look-through buckets).
  Expense ratios are approximate and can be cross-checked live via
  fetch_live_expense_ratio(); verify against issuer sites before relying on them.
- Live: price/momentum stats, Yahoo top-10 holdings (for overlap / look-through
  single-stock exposure), all cached in-process.
"""
import logging
import math
import time
from datetime import datetime, timezone

from .strategies import yf_client as yf

logger = logging.getLogger(__name__)

REFERENCE_AS_OF = "2025 (approximate - confirm with issuer before relying)"

# expense_ratio is a percent (0.03 == 0.03%). exposure buckets sum to ~1.0.
REFERENCE: dict[str, dict] = {
    "VTI":  {"name": "Vanguard Total Stock Market", "category": "US total market",   "expense_ratio": 0.03,
             "exposure": {"us_large": 0.73, "us_midsmall": 0.25, "reit": 0.02}},
    "VOO":  {"name": "Vanguard S&P 500",            "category": "US large cap",      "expense_ratio": 0.03,
             "exposure": {"us_large": 1.0}},
    "VXUS": {"name": "Vanguard Total Intl ex-US",   "category": "International",     "expense_ratio": 0.05,
             "exposure": {"intl_dev": 0.72, "intl_em": 0.28}},
    "QQQ":  {"name": "Invesco QQQ (Nasdaq-100)",    "category": "US large growth",   "expense_ratio": 0.18,
             "exposure": {"us_large": 1.0}, "alt": {"symbol": "QQQM", "expense_ratio": 0.15}},
    "SCHD": {"name": "Schwab US Dividend Equity",   "category": "US dividend/value", "expense_ratio": 0.06,
             "exposure": {"us_large": 0.78, "us_midsmall": 0.22}},
    "VNQ":  {"name": "Vanguard Real Estate",        "category": "US REITs",          "expense_ratio": 0.13,
             "exposure": {"reit": 1.0}},
    "BND":  {"name": "Vanguard Total Bond Market",  "category": "US agg bonds",      "expense_ratio": 0.03,
             "exposure": {"bonds_agg": 1.0}},
    "AGG":  {"name": "iShares Core US Aggregate",   "category": "US agg bonds",      "expense_ratio": 0.03,
             "exposure": {"bonds_agg": 1.0}},
    "TIP":  {"name": "iShares TIPS Bond",           "category": "TIPS",              "expense_ratio": 0.18,
             "exposure": {"bonds_tips": 1.0}, "alt": {"symbol": "SCHP", "expense_ratio": 0.03}},
    "GLD":  {"name": "SPDR Gold Shares",            "category": "Gold",              "expense_ratio": 0.40,
             "exposure": {"gold": 1.0}, "alt": {"symbol": "GLDM", "expense_ratio": 0.10}},
    "IAU":  {"name": "iShares Gold Trust",          "category": "Gold",              "expense_ratio": 0.25,
             "exposure": {"gold": 1.0}},
    "BIL":  {"name": "SPDR 1-3 Month T-Bill",       "category": "Cash / T-bills",    "expense_ratio": 0.14,
             "exposure": {"cash": 1.0}, "alt": {"symbol": "SGOV", "expense_ratio": 0.09}},
    # Common ETFs held elsewhere. Used by the Savings tab to classify holdings; approximate figures.
    "QQQM": {"name": "Invesco Nasdaq 100",          "category": "US large growth",   "expense_ratio": 0.15,
             "exposure": {"us_large": 1.0}},
    "SMH":  {"name": "VanEck Semiconductor",        "category": "Semiconductors",    "expense_ratio": 0.35,
             "exposure": {"us_large": 0.8, "intl_dev": 0.2}},
    "SOXX": {"name": "iShares Semiconductor",       "category": "Semiconductors",    "expense_ratio": 0.35,
             "exposure": {"us_large": 0.9, "intl_dev": 0.1}},
    "BOTZ": {"name": "Global X Robotics & AI",      "category": "Robotics / AI",     "expense_ratio": 0.68,
             "exposure": {"us_large": 0.5, "intl_dev": 0.5}},
    "DGRO": {"name": "iShares Core Dividend Growth", "category": "US dividend growth", "expense_ratio": 0.08,
             "exposure": {"us_large": 0.8, "us_midsmall": 0.2}},
    "VIG":  {"name": "Vanguard Dividend Appreciation", "category": "US dividend growth", "expense_ratio": 0.05,
             "exposure": {"us_large": 0.85, "us_midsmall": 0.15}},
    "VYM":  {"name": "Vanguard High Dividend Yield", "category": "US dividend/value", "expense_ratio": 0.06,
             "exposure": {"us_large": 0.8, "us_midsmall": 0.2}},
    "IVV":  {"name": "iShares Core S&P 500",        "category": "US large cap",      "expense_ratio": 0.03,
             "exposure": {"us_large": 1.0}},
    "SPY":  {"name": "SPDR S&P 500",                "category": "US large cap",      "expense_ratio": 0.09,
             "exposure": {"us_large": 1.0}},
    "SCHB": {"name": "Schwab US Broad Market",      "category": "US total market",   "expense_ratio": 0.03,
             "exposure": {"us_large": 0.73, "us_midsmall": 0.27}},
    "SCHG": {"name": "Schwab US Large-Cap Growth",  "category": "US large growth",   "expense_ratio": 0.04,
             "exposure": {"us_large": 1.0}},
    "VUG":  {"name": "Vanguard Growth",             "category": "US large growth",   "expense_ratio": 0.04,
             "exposure": {"us_large": 1.0}},
}

EQUITY_BUCKETS = {"us_large", "us_midsmall", "intl_dev", "intl_em", "reit"}

_TTL_MARKET = 15 * 60
_TTL_HOLDINGS = 24 * 3600
_cache: dict = {}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


# ── Reference-derived portfolio metrics ───────────────────────────────────────

def blended_expense_ratio(weights: dict) -> float:
    """Weighted expense ratio in percent (0.10 == 0.10%/yr)."""
    total = sum(weights.values()) or 1.0
    return sum((w / total) * REFERENCE.get(s, {}).get("expense_ratio", 0.0)
               for s, w in weights.items())


def bucket_exposure(weights: dict) -> dict:
    out: dict = {}
    for s, w in weights.items():
        for b, x in REFERENCE.get(s, {}).get("exposure", {}).items():
            out[b] = out.get(b, 0.0) + w * x
    return {b: round(v, 4) for b, v in sorted(out.items(), key=lambda kv: -kv[1])}


def cheaper_alternatives(weights: dict) -> list[dict]:
    """Held ETFs that have a materially cheaper like-for-like alternative."""
    out = []
    for s, w in weights.items():
        ref = REFERENCE.get(s, {})
        alt = ref.get("alt")
        if w > 0 and alt and alt["expense_ratio"] < ref["expense_ratio"] - 0.02:
            out.append({"symbol": s, "weight": round(w, 4), "expense_ratio": ref["expense_ratio"],
                        "alt": alt["symbol"], "alt_expense_ratio": alt["expense_ratio"],
                        "annual_saving_bps_on_position": round((ref["expense_ratio"] - alt["expense_ratio"]) * 100 * w, 2)})
    return out


# ── Live: Yahoo quoteSummary (expense ratio, top holdings) ────────────────────

def _quote_summary(symbol: str, modules: str) -> dict | None:
    try:
        sess = yf._get_session()
        crumb = yf._get_crumb()
        r = sess.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
            params={"modules": modules, "crumb": crumb}, timeout=20,
        )
        r.raise_for_status()
        res = (r.json().get("quoteSummary") or {}).get("result")
        return res[0] if res else None
    except Exception as exc:
        logger.debug("ira_data: quoteSummary %s failed: %s", symbol, exc)
        return None


def fetch_live_expense_ratio(symbol: str) -> float | None:
    """Percent, or None if Yahoo doesn't expose it."""
    def _go():
        d = _quote_summary(symbol, "fundProfile")
        fees = ((d or {}).get("fundProfile") or {}).get("feesExpensesInvestment") or {}
        for k in ("annualReportExpenseRatio", "netExpRatio"):
            raw = (fees.get(k) or {}).get("raw")
            if raw:
                return round(float(raw) * 100, 4)
        return None
    return _cached(("er", symbol), _TTL_HOLDINGS, _go)


def fetch_top_holdings(symbol: str) -> dict:
    """{ticker: weight_fraction} for the fund's top holdings (usually top 10)."""
    def _go():
        d = _quote_summary(symbol, "topHoldings")
        out = {}
        for h in ((d or {}).get("topHoldings") or {}).get("holdings") or []:
            sym = h.get("symbol")
            raw = (h.get("holdingPercent") or {}).get("raw")
            if sym and raw:
                out[sym] = float(raw)
        return out
    return _cached(("hold", symbol), _TTL_HOLDINGS, _go)


def lookthrough(weights: dict) -> dict:
    """Overlap and single-stock exposure from live top-10 holdings.

    Top-10 only, so stock exposure is a lower bound and pair overlap is the
    overlap of the funds' top holdings, not their full portfolios.
    """
    holdings = {s: fetch_top_holdings(s) for s, w in weights.items()
                if w > 0 and set(REFERENCE.get(s, {}).get("exposure", {})) & EQUITY_BUCKETS}
    holdings = {s: h for s, h in holdings.items() if h}
    stock: dict = {}
    for s, h in holdings.items():
        for t, hw in h.items():
            stock[t] = stock.get(t, 0.0) + weights[s] * hw
    pairs = []
    syms = sorted(holdings)
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            ov = sum(min(holdings[a][t], holdings[b][t]) for t in holdings[a] if t in holdings[b])
            if ov > 0:
                pairs.append({"a": a, "b": b, "top10_overlap_pct": round(ov * 100, 1)})
    return {
        "available": bool(holdings),
        "funds_covered": syms,
        "top_stocks": [{"symbol": t, "lookthrough_pct": round(v * 100, 2)}
                       for t, v in sorted(stock.items(), key=lambda kv: -kv[1])[:8]],
        "pair_overlap": sorted(pairs, key=lambda p: -p["top10_overlap_pct"]),
    }


# ── Live: price / momentum stats ──────────────────────────────────────────────

def _stats_one(symbol: str) -> dict | None:
    df = yf.fetch_history(symbol, 420)
    if df is None or df.empty or len(df) < 60:
        return None
    c = df["Close"].astype(float)
    last = float(c.iloc[-1])

    def ret(n):
        return round(float(last / c.iloc[-1 - n] - 1), 4) if len(c) > n else None

    asof = c.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - asof).days
    sma200 = float(c.tail(200).mean()) if len(c) >= 200 else None
    vol = float(c.pct_change().tail(20).std() * math.sqrt(252))
    return {
        "price": round(last, 2),
        "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126), "ret_12m": ret(252),
        "vol_20d": round(vol, 4) if vol == vol else None,
        "pct_off_52w_high": round(float(last / c.tail(252).max() - 1), 4),
        "above_200sma": (last > sma200) if sma200 else None,
        "as_of": asof.date().isoformat(),
        "stale": age_days > 5,
    }


def market_stats(symbols) -> dict:
    """Live stats per symbol; value is None when data couldn't be fetched."""
    return {s: _cached(("mkt", s), _TTL_MARKET, lambda s=s: _stats_one(s)) for s in symbols}
