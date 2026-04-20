"""
DD fetcher using stockanalysis.com — parses __NEXT_DATA__ JSON embedded in the page.
Works from residential IPs; datacenter IPs may receive 403 (expected during CI/dev).
Rate-limit protection is handled by the caller via DB caching (7-day TTL).
"""
import re
import json
import time
import logging
import httpx

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Consensus text → numeric scale (matches yfinance recommendationMean range)
_CONSENSUS_NUM = {
    "Strong Buy": 1.0, "Buy": 2.0, "Moderate Buy": 2.0,
    "Hold": 3.0, "Neutral": 3.0,
    "Moderate Sell": 4.0, "Underperform": 4.0,
    "Sell": 5.0, "Strong Sell": 5.0,
}
_NUM_CSS = {
    1.0: "text-emerald-400", 2.0: "text-emerald-300",
    3.0: "text-yellow-400",  4.0: "text-orange-400", 5.0: "text-red-400",
}


def _first(*vals, cast=None):
    """Return the first non-None value; optionally cast it."""
    for v in vals:
        if v is not None:
            return cast(v) if cast else v
    return None


def _pct(v):
    """Convert a value to a decimal fraction if it looks like a whole-percent."""
    if v is None:
        return None
    v = float(v)
    # stockanalysis sometimes returns margins as whole numbers (44.1) instead of decimals (0.441)
    return v / 100 if abs(v) > 1.5 else v


def _extract(nd: dict, symbol: str) -> dict:
    """Parse the __NEXT_DATA__ dict and return a DD record."""
    pp = nd.get("props", {}).get("pageProps", {})

    # ── company info ──────────────────────────────────────────────────────────
    # Try several known locations for company info
    info = (pp.get("info") or pp.get("stockInfo") or
            (pp.get("data") or {}).get("info") or {})

    name     = _first(info.get("name"), info.get("n"), default=symbol)
    sector   = info.get("sector") or info.get("se") or ""
    industry = info.get("industry") or info.get("ind") or ""
    desc     = (info.get("description") or info.get("desc") or "")[:500]

    # ── key stats ─────────────────────────────────────────────────────────────
    stats = (pp.get("stats") or pp.get("stockStats") or
             (pp.get("data") or {}).get("stats") or
             pp.get("overview") or {})

    # Fallback: scan top-level pageProps values for a dict that has financial keys
    if not stats:
        for v in pp.values():
            if isinstance(v, dict) and any(k in v for k in ("pe", "marketCap", "eps", "mc")):
                stats = v
                break

    mktcap   = _first(stats.get("marketCap"), stats.get("mktCap"), stats.get("mc"),
                       info.get("marketCap"), cast=float)
    pe       = _first(stats.get("pe"), stats.get("trailingPE"), stats.get("peTTM"), cast=float)
    fpe      = _first(stats.get("fpe"), stats.get("forwardPE"), stats.get("forwardPeRatio"), cast=float)
    eps      = _first(stats.get("eps"), stats.get("epsTTM"), stats.get("trailingEps"), cast=float)

    rev_g    = _pct(_first(stats.get("revenueGrowth"), stats.get("revGrowth"), stats.get("revenueGrowthYoy")))
    earn_g   = _pct(_first(stats.get("earningsGrowth"), stats.get("epsGrowth"), stats.get("netIncomeGrowth")))
    gross_m  = _pct(_first(stats.get("grossMargin"), stats.get("gm")))
    net_m    = _pct(_first(stats.get("netMargin"), stats.get("profitMargin"), stats.get("nm")))
    roe      = _pct(_first(stats.get("roe"), stats.get("returnOnEquity")))
    de       = _first(stats.get("debtEquity"), stats.get("debtToEquity"), stats.get("de"), cast=float)

    # ── analyst ratings ───────────────────────────────────────────────────────
    ratings      = pp.get("ratings") or pp.get("analystRatings") or {}
    consensus    = (ratings.get("rating") or ratings.get("consensus") or
                    ratings.get("analystRating") or "")
    analyst_cnt  = _first(ratings.get("count"), ratings.get("numAnalysts"),
                           ratings.get("numberOfAnalystOpinions"), cast=int)
    analyst_tgt  = _first(ratings.get("targetPrice"), ratings.get("priceTarget"), cast=float)

    rating_num = _CONSENSUS_NUM.get(consensus)

    if not name and mktcap is None and pe is None:
        raise ValueError(f"No usable data found in __NEXT_DATA__ for {symbol}. "
                         "Keys available: " + str(list(pp.keys())))

    return {
        "symbol":          symbol,
        "name":            name,
        "sector":          sector,
        "industry":        industry,
        "market_cap":      mktcap,
        "pe_ttm":          pe,
        "forward_pe":      fpe,
        "eps_ttm":         eps,
        "revenue_growth":  rev_g,
        "earnings_growth": earn_g,
        "gross_margin":    gross_m,
        "net_margin":      net_m,
        "roe":             roe,
        "debt_to_equity":  de,
        "analyst_rating":  rating_num,
        "analyst_label":   consensus or "N/A",
        "analyst_css":     _NUM_CSS.get(rating_num, "text-slate-500"),
        "analyst_count":   analyst_cnt,
        "analyst_target":  analyst_tgt,
        "description":     desc,
        "error":           None,
    }


def fetch_dd(symbol: str) -> dict:
    """Fetch DD from stockanalysis.com. Never raises — returns {error} on failure."""
    url = f"https://stockanalysis.com/stocks/{symbol.lower()}/"
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        resp.raise_for_status()

        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text, re.DOTALL,
        )
        if not m:
            logger.warning("DD %s: __NEXT_DATA__ not found in page.", symbol)
            return {"symbol": symbol, "error": "__NEXT_DATA__ not found — site layout may have changed."}

        nd = json.loads(m.group(1))
        return _extract(nd, symbol)

    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        logger.warning("DD %s: HTTP %s", symbol, code)
        msg = ("Blocked by Cloudflare — this endpoint must run on a residential IP."
               if code == 403 else f"HTTP {code}")
        return {"symbol": symbol, "error": msg}
    except Exception as exc:
        logger.warning("DD fetch failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "error": str(exc)[:200]}


def fetch_dd_batch(symbols: list[str]) -> list[dict]:
    """Fetch DD for each symbol sequentially with a 2 s throttle."""
    results = []
    for i, sym in enumerate(symbols):
        if i > 0:
            time.sleep(2)
        results.append(fetch_dd(sym))
    return results
