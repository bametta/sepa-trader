"""
Due-diligence data fetcher for the weekly top-10 picks.
Uses yfinance .info — one call per symbol, sequential with small throttle.
Safe for <=15 symbols; takes ~5-10s total.
"""
import time
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

_RATING_LABEL = {
    (1.0, 1.5): ("Strong Buy",  "text-emerald-400"),
    (1.5, 2.5): ("Buy",         "text-emerald-300"),
    (2.5, 3.5): ("Hold",        "text-yellow-400"),
    (3.5, 4.5): ("Underperform","text-orange-400"),
    (4.5, 5.1): ("Sell",        "text-red-400"),
}


def _rating_meta(mean: float | None) -> dict:
    if mean is None:
        return {"label": "N/A", "css": "text-slate-500"}
    for (lo, hi), (label, css) in _RATING_LABEL.items():
        if lo <= mean < hi:
            return {"label": label, "css": css}
    return {"label": "N/A", "css": "text-slate-500"}


def fetch_dd(symbol: str) -> dict:
    """Return a DD dict for one symbol. Never raises — returns error key on failure."""
    try:
        info = yf.Ticker(symbol).info
        if not info or info.get("trailingPE") is None and info.get("marketCap") is None:
            # yfinance returned an empty/stub dict (rate-limited or unknown symbol)
            return {"symbol": symbol, "error": "No data returned — try again later."}

        rating_mean = info.get("recommendationMean")
        return {
            "symbol":          symbol,
            "name":            info.get("longName") or info.get("shortName") or symbol,
            "sector":          info.get("sector")   or "",
            "industry":        info.get("industry") or "",
            "market_cap":      info.get("marketCap"),
            "pe_ttm":          info.get("trailingPE"),
            "forward_pe":      info.get("forwardPE"),
            "eps_ttm":         info.get("trailingEps"),
            "revenue_growth":  info.get("revenueGrowth"),    # quarterly YoY decimal
            "earnings_growth": info.get("earningsGrowth"),   # quarterly YoY decimal
            "gross_margin":    info.get("grossMargins"),
            "net_margin":      info.get("profitMargins"),
            "roe":             info.get("returnOnEquity"),
            "debt_to_equity":  info.get("debtToEquity"),
            "analyst_rating":  rating_mean,
            "analyst_label":   _rating_meta(rating_mean)["label"],
            "analyst_css":     _rating_meta(rating_mean)["css"],
            "analyst_count":   info.get("numberOfAnalystOpinions"),
            "description":     (info.get("longBusinessSummary") or "")[:500],
            "error":           None,
        }
    except Exception as exc:
        logger.warning("DD fetch failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "error": str(exc)[:200]}


def fetch_dd_batch(symbols: list[str]) -> list[dict]:
    """Fetch DD for all symbols sequentially with light throttle."""
    results = []
    for i, sym in enumerate(symbols):
        if i > 0:
            time.sleep(0.5)
        results.append(fetch_dd(sym))
    return results
