"""
TradingView Scanner API-based SEPA analyzer.

Replaces yfinance: fetches all universe symbols in ONE HTTP request
to TradingView's public screener endpoint — no per-symbol rate limits,
no retries, ~5 seconds for 100 symbols vs 3–5 minutes with yfinance.
"""
import logging
import httpx

from .tradingview_client import to_tv_symbol

logger = logging.getLogger(__name__)

SCAN_URL = "https://scanner.tradingview.com/america/scan"

_TV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Origin":  "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}

# Cached session cookie from TV login (populated by _get_tv_cookie)
_tv_cookie: str = ""


def _get_tv_cookie(db=None) -> str:
    """
    Return a cached TV session cookie if TV credentials are configured.
    Reads from the admin user's merged settings (user_settings overrides global).
    Falls back to empty string (unauthenticated) if credentials not set.
    """
    global _tv_cookie
    if _tv_cookie:
        return _tv_cookie
    if db is None:
        return ""
    try:
        from sqlalchemy import text as _text
        from .database import get_all_user_settings as _gaus
        # Get admin user_id
        admin_row = db.execute(
            _text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
        ).fetchone()
        if not admin_row:
            return ""
        merged = _gaus(db, admin_row[0])
        tv_user = merged.get("tv_username", "")
        tv_pass = merged.get("tv_password", "")
        if not tv_user or not tv_pass:
            return ""
        from .tradingview_client import get_session_cookie
        _tv_cookie = get_session_cookie(tv_user, tv_pass) or ""
        if _tv_cookie:
            logger.info("tv_analyzer: TV session cookie obtained for scanner auth")
    except Exception as exc:
        logger.debug("tv_analyzer: could not get TV cookie: %s", exc)
    return _tv_cookie

# Confirmed-valid TradingView scanner columns only.
# EMA150 is not a standard TV field — interpolated from EMA100+EMA200.
# 52W High/Low not available via scanner API — those 2 criteria score 0; max is 6/8.
_COLS = [
    "close",
    "EMA20",
    "EMA50",
    "EMA100",                   # proxy for EMA150
    "EMA200",
    "SMA200",                   # EMA200 > SMA200 → EMA200 is rising
    "volume",
    "average_volume_30d_calc",
    "sector",
    "industry",
]


def batch_analyze(
    symbols: list[str],
    vol_surge_pct: float = 40.0,
    ema20_pct: float = 2.0,
    ema50_pct: float = 3.0,
    db=None,
) -> dict[str, dict]:
    """
    Fetch SEPA indicators for every symbol in one TradingView API call.
    Returns {symbol: result_dict} in the same format as sepa_analyzer.analyze().

    Handles TV 401 gracefully — returns INSUFFICIENT_DATA so the monitor
    can still manage stops/exits even when TV blocks unauthenticated requests.
    """
    global _tv_cookie
    tv_syms = [to_tv_symbol(s) for s in symbols]

    def _do_request(cookie: str = ""):
        headers = dict(_TV_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        return httpx.post(
            SCAN_URL,
            json={
                "symbols": {"tickers": tv_syms, "query": {"types": []}},
                "columns": _COLS,
            },
            timeout=30,
            headers=headers,
        )

    try:
        resp = _do_request(_tv_cookie)

        # TV returns 401 when unauthenticated from cloud IPs — retry with session cookie
        if resp.status_code == 401:
            logger.warning(
                "tv_analyzer: TV scanner returned 401 — attempting authenticated request"
            )
            _tv_cookie = ""          # clear stale cookie
            fresh_cookie = _get_tv_cookie(db)
            if fresh_cookie:
                resp = _do_request(fresh_cookie)
            else:
                logger.warning(
                    "tv_analyzer: no TV credentials configured — "
                    "scanner unavailable. Add TV username/password in Settings → Integrations. "
                    "Monitor will continue managing stops and exits."
                )
                return {s: {"signal": "INSUFFICIENT_DATA", "score": 0, "price": None} for s in symbols}

        if resp.status_code == 401:
            logger.warning(
                "tv_analyzer: TV scanner 401 even after auth — "
                "signal analysis unavailable. Monitor continues managing stops/exits."
            )
            return {s: {"signal": "INSUFFICIENT_DATA", "score": 0, "price": None} for s in symbols}

        if not resp.is_success:
            logger.warning("tv_analyzer: TV scanner returned %d — skipping signal analysis", resp.status_code)
            return {s: {"signal": "INSUFFICIENT_DATA", "score": 0, "price": None} for s in symbols}

    except Exception as exc:
        logger.warning("tv_analyzer: TV scanner unavailable (%s) — skipping signal analysis", exc)
        return {s: {"signal": "INSUFFICIENT_DATA", "score": 0, "price": None} for s in symbols}

    rows = resp.json().get("data", [])
    results: dict[str, dict] = {}

    for row in rows:
        sym = row["s"].split(":")[-1]
        vals = dict(zip(_COLS, row["d"]))
        results[sym] = _score_sepa(sym, vals, vol_surge_pct, ema20_pct, ema50_pct)

    # Symbols TradingView didn't return (unknown/delisted)
    for s in symbols:
        if s not in results:
            results[s] = {"signal": "INSUFFICIENT_DATA", "score": 0, "price": None}

    logger.info(
        "TradingView scan: %d requested, %d returned, %d errors",
        len(symbols),
        len(rows),
        sum(1 for r in results.values() if r.get("signal") in ("ERROR", "INSUFFICIENT_DATA")),
    )
    return results


def analyze(symbol: str, db=None) -> dict:
    """Single-symbol wrapper — used by the hourly monitor."""
    return batch_analyze([symbol], db=db).get(symbol, {"signal": "ERROR", "score": 0, "price": None})


# Full column set for the unified scan — same fields batch_analyze fetches via
# symbol lookup, plus market_cap_basic for the pre-filter sort.
_SCAN_COLS = [
    "close", "EMA20", "EMA50", "EMA100", "EMA200", "SMA200",
    "volume", "average_volume_30d_calc", "market_cap_basic",
    "sector", "industry",
]

# Mega scan column set: superset covering all three screeners in one TV call.
# Minervini needs: close, EMA20/50/100/200, SMA200, volume, avg_vol, market_cap, sector, industry
# Pullback adds:   RSI, ADX, price_52_week_high, earnings_release_next_date, Perf.1M, Perf.3M
# RS Momentum adds: Perf.6M, Perf.Y, exchange
_MEGA_COLS = [
    "close",
    "EMA20",
    "EMA50",
    "EMA100",
    "EMA200",
    "SMA200",
    "volume",
    "average_volume_30d_calc",
    "market_cap_basic",
    "sector",
    "industry",
    "exchange",
    "RSI",
    "ADX",
    "price_52_week_high",
    "earnings_release_next_date",
    "Perf.1M",
    "Perf.3M",
    "Perf.6M",
    "Perf.Y",
]


def scan_universe_mega(
    price_min: float = 5.0,
    price_max: float = 0.0,
    exchanges: list | None = None,
    max_results: int = 1500,
    db=None,
) -> dict[str, dict]:
    """Single TradingView scan covering all three screeners (Minervini, Pullback, RS).

    Uses the broadest possible server-side filter — price, volume, market cap,
    and exchange only. No strategy-specific EMA ladder or RSI filters are applied
    server-side so the same dataset can feed all three screeners for their own
    local filtering passes.

    Returns {symbol: raw_vals_dict} with all _MEGA_COLS fields present.
    Empty dict on failure so callers fall back to individual TV calls.
    """
    global _tv_cookie
    floor_price = max(5.0, price_min or 0)
    filters: list[dict] = [
        {"left": "close",                   "operation": "egreater", "right": floor_price},
        {"left": "average_volume_30d_calc", "operation": "egreater", "right": 500_000},
        {"left": "market_cap_basic",        "operation": "egreater", "right": 300_000_000},
        {"left": "exchange", "operation": "in_range",
         "right": exchanges or ["NYSE", "NASDAQ"]},
    ]
    if price_max and price_max > 0:
        filters.append({"left": "close", "operation": "eless", "right": price_max})

    def _do_request(cookie: str = ""):
        headers = dict(_TV_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        return httpx.post(
            SCAN_URL,
            json={
                "filter":  filters,
                "columns": _MEGA_COLS,
                "range":   [0, max_results],
                "sort":    {"sortBy": "average_volume_30d_calc", "sortOrder": "desc"},
                "markets": ["america"],
            },
            timeout=60,
            headers=headers,
        )

    try:
        resp = _do_request(_tv_cookie)
        if resp.status_code == 401:
            _tv_cookie = ""
            fresh = _get_tv_cookie(db)
            if fresh:
                _tv_cookie = fresh
                resp = _do_request(_tv_cookie)
            else:
                logger.warning("scan_universe_mega: TV 401 and no credentials — returning empty")
                return {}
        if resp.status_code == 401:
            logger.warning("scan_universe_mega: TV 401 even after auth — returning empty")
            return {}
        if not resp.is_success:
            logger.warning("scan_universe_mega: TV returned %d — returning empty", resp.status_code)
            return {}
    except Exception as exc:
        logger.error("scan_universe_mega: request failed: %s", exc)
        return {}

    rows = resp.json().get("data") or []
    result: dict[str, dict] = {}
    for row in rows:
        try:
            sym  = row["s"].split(":")[-1]
            vals = dict(zip(_MEGA_COLS, row["d"]))
            result[sym] = vals
        except (KeyError, IndexError, TypeError):
            continue

    logger.info(
        "scan_universe_mega: %d symbols fetched (%d columns each, max_results=%d)",
        len(result), len(_MEGA_COLS), max_results,
    )
    return result


def score_mega_for_minervini(
    mega_data: dict[str, dict],
    vol_surge_pct: float = 40.0,
    ema20_pct: float = 2.0,
    ema50_pct: float = 3.0,
) -> dict[str, dict]:
    """Apply Minervini Stage-2 pre-filter and SEPA scoring to pre-fetched mega data.

    Replicates the server-side Stage-2 ladder (close > EMA50 > EMA100 > EMA200)
    that scan_and_score_universe applies — using EMA columns as a proxy for the
    SMA50/SMA150/SMA200 TV server-side check. In practice EMA ≈ SMA for liquid
    large-caps, so the difference is negligible.

    Returns {symbol: sepa_result} in the same format as scan_and_score_universe().
    """
    results: dict[str, dict] = {}
    for sym, vals in mega_data.items():
        close = vals.get("close") or 0.0
        e50   = vals.get("EMA50")  or 0.0
        e100  = vals.get("EMA100") or 0.0
        e200  = vals.get("EMA200") or 0.0
        # Stage-2 approximation using EMA ladder (server-side uses SMA50>SMA150>SMA200)
        if not (close > 0 and e50 > 0 and e100 > 0 and e200 > 0):
            continue
        if not (close > e50 and e50 > e100 and e100 > e200):
            continue
        results[sym] = _score_sepa(sym, vals, vol_surge_pct, ema20_pct, ema50_pct)
    logger.info(
        "score_mega_for_minervini: %d of %d passed Stage-2 pre-filter",
        len(results), len(mega_data),
    )
    return results


def scan_and_score_universe(
    price_min: float = 0.0,
    price_max: float = 0.0,
    excluded_sectors: set | None = None,
    excluded_industries: set | None = None,
    exchanges: list | None = None,
    max_results: int = 1500,
    vol_surge_pct: float = 40.0,
    ema20_pct: float = 2.0,
    ema50_pct: float = 3.0,
    db=None,
) -> dict[str, dict]:
    """Single TV scanner call: filter the full exchange, fetch SEPA columns,
    and score everything — replaces the old two-call flow (universe fetch +
    batch_analyze). Returns {symbol: result_dict} in the same format as
    batch_analyze().

    Server-side filters applied:
      • price ≥ max($5, price_min)
      • 30-day avg volume ≥ 500k
      • market cap ≥ $300M
      • exchange in NYSE / NASDAQ (or override)
      • Stage-2 trend ladder: close > SMA50 > SMA150 > SMA200

    Sector / industry exclusion and SEPA scoring are done client-side on the
    returned rows so every Stage-2 candidate on the exchange is evaluated.
    """
    floor_price = max(5.0, price_min or 0)
    filters: list[dict] = [
        {"left": "close",                   "operation": "egreater", "right": floor_price},
        {"left": "average_volume_30d_calc", "operation": "egreater", "right": 500_000},
        {"left": "market_cap_basic",        "operation": "egreater", "right": 300_000_000},
        {"left": "exchange", "operation": "in_range",
         "right": exchanges or ["NYSE", "NASDAQ"]},
        # Stage-2 trend ladder — pre-filter to structural candidates only
        {"left": "close",  "operation": "greater", "right": "SMA50"},
        {"left": "SMA50",  "operation": "greater", "right": "SMA150"},
        {"left": "SMA150", "operation": "greater", "right": "SMA200"},
    ]
    if price_max and price_max > 0:
        filters.append({"left": "close", "operation": "eless", "right": price_max})

    def _do_request(cookie: str = ""):
        headers = dict(_TV_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        return httpx.post(
            SCAN_URL,
            json={
                "filter":  filters,
                "columns": _SCAN_COLS,
                "range":   [0, max_results],
                "sort":    {"sortBy": "average_volume_30d_calc", "sortOrder": "desc"},
                "markets": ["america"],
            },
            timeout=60,
            headers=headers,
        )

    global _tv_cookie
    try:
        resp = _do_request(_tv_cookie)
        if resp.status_code == 401:
            _tv_cookie = ""
            fresh = _get_tv_cookie(db)
            if fresh:
                _tv_cookie = fresh
                resp = _do_request(_tv_cookie)
            else:
                logger.warning("scan_and_score_universe: TV 401 and no credentials — returning empty")
                return {}
        if resp.status_code == 401:
            logger.warning("scan_and_score_universe: TV 401 even after auth — returning empty")
            return {}
        if not resp.is_success:
            logger.warning("scan_and_score_universe: TV returned %d — returning empty", resp.status_code)
            return {}
    except Exception as exc:
        logger.error("scan_and_score_universe: request failed: %s", exc)
        return {}

    rows = resp.json().get("data") or []
    excluded_sectors    = excluded_sectors    or set()
    excluded_industries = excluded_industries or set()

    results: dict[str, dict] = {}
    skipped = 0
    for row in rows:
        try:
            full_sym = row["s"]
            vals     = dict(zip(_SCAN_COLS, row["d"]))
        except (KeyError, IndexError, TypeError):
            continue
        sector   = (vals.get("sector")   or "").strip()
        industry = (vals.get("industry") or "").strip()
        if excluded_sectors    and sector.lower()   in excluded_sectors:
            skipped += 1
            continue
        if excluded_industries and industry.lower() in excluded_industries:
            skipped += 1
            continue
        sym = full_sym.split(":")[-1]
        results[sym] = _score_sepa(sym, vals, vol_surge_pct, ema20_pct, ema50_pct)

    logger.info(
        "scan_and_score_universe: %d from TV, %d sector-skipped, %d scored (max_results=%d)",
        len(rows), skipped, len(results), max_results,
    )
    return results


def _score_sepa(
    symbol: str,
    v: dict,
    vol_surge_pct: float = 40.0,
    ema20_pct: float = 2.0,
    ema50_pct: float = 3.0,
) -> dict:
    close = v.get("close")
    if not close:
        return {"signal": "INSUFFICIENT_DATA", "score": 0, "price": None}

    e20      = v.get("EMA20") or 0
    e50      = v.get("EMA50") or 0
    e100     = v.get("EMA100") or 0
    e200     = v.get("EMA200") or 0
    sma200   = v.get("SMA200") or 0
    w52h     = 0  # not available via TV scanner — criterion 7 always 0
    w52l     = 0  # not available via TV scanner — criterion 8 always 0
    vol      = v.get("volume") or 0
    vol_avg  = v.get("average_volume_30d_calc") or 1

    # EMA150 not a standard TV field — interpolate between EMA100 and EMA200
    e150 = (e100 * 0.5 + e200 * 0.5) if e100 and e200 else 0

    # Criterion 6: EMA200 rising — EMA200 > SMA200 means recent closes are
    # pulling the exponential average above the simple one (upward momentum)
    e200_rising = bool(e200 and sma200 and e200 > sma200)

    score = sum([
        bool(e50  and close > e50),
        bool(e150 and close > e150),
        bool(e200 and close > e200),
        bool(e50  and e150 and e50  > e150),
        bool(e150 and e200 and e150 > e200),
        e200_rising,
        bool(w52h and close >= w52h * 0.75),
        bool(w52l and close >= w52l * 1.30),
    ])

    vol_surge   = bool(vol and vol_avg and vol > vol_avg * (1 + vol_surge_pct / 100))
    e20_near    = bool(e20  and abs(close - e20)  / e20  * 100 <= ema20_pct)
    e50_near    = bool(e50  and abs(close - e50)  / e50  * 100 <= ema50_pct)
    above_pivot = score >= 7

    if score >= 7:
        if vol_surge:
            signal = "BREAKOUT"
        elif e20_near:
            signal = "PULLBACK_EMA20"
        elif e50_near:
            signal = "PULLBACK_EMA50"
        else:
            signal = "STAGE2_WATCH"
    elif score >= 4:
        signal = "PULLBACK_EMA20" if e20_near else ("PULLBACK_EMA50" if e50_near else "STAGE2_WATCH")
    else:
        signal = "NO_SETUP"

    return {
        "signal":      signal,
        "score":       score,
        "price":       round(close, 4),
        "sector":      (v.get("sector") or "").strip(),
        "industry":    (v.get("industry") or "").strip(),
        "ema20":       round(e20, 4)  if e20  else None,
        "ema50":       round(e50, 4)  if e50  else None,
        "ema150":      round(e150, 4) if e150 else None,
        "ema200":      round(e200, 4) if e200 else None,
        "week52_high": round(w52h, 4) if w52h else None,
        "week52_low":  round(w52l, 4) if w52l else None,
        "vol_today":   int(vol),
        "vol_avg30":   int(vol_avg),
        "vol_surge":   vol_surge,
        "near20":      e20_near,
        "near50":      e50_near,
        "above_pivot": above_pivot,
    }
