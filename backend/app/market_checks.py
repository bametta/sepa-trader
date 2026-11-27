"""
Pre-trade market checks — earnings proximity and volume confirmation.

Both checks are injected into the AI gate prompt as structured context blocks.
Both fail open on any error: a data outage never blocks a trade.

Earnings
--------
Uses the same earnings_cache DB table as the pullback screener so a lookup
done at screener time is reused at gate time — no duplicate Yahoo Finance call.

Volume
------
Uses yf_client.fetch_ohlcv() (same Yahoo Finance session as the rest of the
system) to compute a 50-day average volume baseline. Compares yesterday's
completed volume (or the most recent bar) so early-morning gate calls don't
see a partial intraday bar. Ratio is passed to the AI along with explicit
thresholds; the AI weighs it against the overall setup quality.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# ── Earnings proximity ────────────────────────────────────────────────────────

def get_earnings_proximity(symbol: str, db=None) -> dict:
    """
    Return days until the next earnings release for `symbol`.

    Result dict:
    {
      "earnings_date":    str | None,   # ISO date string
      "days_to_earnings": int | None,   # calendar days; None = unknown
      "trading_days":     int | None,   # approx trading days (×5/7)
      "risk_level":       "low"|"medium"|"high"|"unknown",
      "summary":          str,
    }
    Fails open — returns risk_level="unknown" on any error.
    """
    try:
        from .pullback_screener import _get_cached_earnings
        next_earn = _get_cached_earnings(db, symbol)
    except Exception as exc:
        logger.debug("market_checks: earnings lookup failed for %s: %s", symbol, exc)
        next_earn = None

    today = date.today()

    if next_earn is None:
        return {
            "earnings_date":    None,
            "days_to_earnings": None,
            "days_since":       None,
            "trading_days":     None,
            "risk_level":       "unknown",
            "summary":          "Next earnings date unknown — treat as elevated risk.",
        }

    cal_days  = (next_earn - today).days
    # Positive = days until next earnings; negative = days since last earnings
    days_since = abs(cal_days) if cal_days < 0 else None
    trad_days  = max(0, int(cal_days * 5 / 7))

    if trad_days <= 5:
        risk = "high"
    elif trad_days <= 10:
        risk = "medium"
    else:
        risk = "low"

    if cal_days < 0:
        summary = f"Earnings date {next_earn} appears to be in the past — may be stale data."
        risk    = "unknown"
    elif trad_days <= 5:
        summary = (
            f"⚠ EARNINGS IN {trad_days} TRADING DAY(S) ({next_earn}). "
            f"Holding through earnings exposes the position to gap risk that stops cannot protect against."
        )
    elif trad_days <= 10:
        summary = (
            f"Earnings approaching: {trad_days} trading days away ({next_earn}). "
            f"Position will be open near the event — monitor closely."
        )
    else:
        summary = f"Next earnings: {next_earn} ({trad_days} trading days away) — sufficient runway."

    return {
        "earnings_date":    str(next_earn),
        "days_to_earnings": cal_days,
        "days_since":       days_since,
        "trading_days":     trad_days,
        "risk_level":       risk,
        "summary":          summary,
    }


def format_earnings_for_prompt(symbol: str, info: dict) -> str:
    if not info:
        return ""
    return (
        f"\n--- EARNINGS PROXIMITY — {symbol} ---\n"
        f"{info['summary']}\n"
    )


# ── Volume confirmation ───────────────────────────────────────────────────────

def get_volume_confirmation(symbol: str) -> dict:
    """
    Compare yesterday's completed volume against the 50-day average.

    Uses the most recent completed trading day so early-morning gate calls
    (Monday 9:35 open) don't see a tiny partial intraday bar.

    Result dict:
    {
      "avg_50d_volume":   int,
      "recent_volume":    int,    # most recent complete session
      "ratio":            float,  # recent / avg_50d
      "signal":           "strong"|"above_avg"|"average"|"below_avg"|"weak"|"unknown",
      "summary":          str,
    }
    Fails open — returns signal="unknown" on any error.
    """
    try:
        from .strategies.yf_client import fetch_ohlcv
        df = fetch_ohlcv(symbol, period_days=60)
    except Exception as exc:
        logger.debug("market_checks: OHLCV fetch failed for %s: %s", symbol, exc)
        return _unknown_volume()

    if df is None or df.empty or "Volume" not in df.columns:
        return _unknown_volume()

    df = df.dropna(subset=["Volume"])
    if len(df) < 10:
        return _unknown_volume()

    # Use all but the most recent bar as the 50-day baseline,
    # then the most recent bar as "recent" volume.
    baseline = df.iloc[:-1]["Volume"]   # exclude today/latest partial bar
    if len(baseline) < 5:
        return _unknown_volume()

    avg_vol    = int(baseline.tail(50).mean())
    recent_vol = int(df["Volume"].iloc[-1])

    if avg_vol <= 0:
        return _unknown_volume()

    ratio = recent_vol / avg_vol

    if ratio >= 2.0:
        signal  = "strong"
        note    = f"Volume {ratio:.1f}× average — strong institutional participation. Bullish confirmation."
    elif ratio >= 1.5:
        signal  = "above_avg"
        note    = f"Volume {ratio:.1f}× average — above-average interest. Positive signal."
    elif ratio >= 0.8:
        signal  = "average"
        note    = f"Volume {ratio:.1f}× average — within normal range."
    elif ratio >= 0.5:
        signal  = "below_avg"
        note    = f"Volume {ratio:.1f}× average — below average. Low conviction signal."
    else:
        signal  = "weak"
        note    = f"Volume {ratio:.1f}× average — very thin. Breakout lacks institutional backing."

    summary = (
        f"Recent vol: {recent_vol:,}  |  50d avg: {avg_vol:,}  |  Ratio: {ratio:.2f}×\n"
        f"{note}"
    )

    return {
        "avg_50d_volume": avg_vol,
        "recent_volume":  recent_vol,
        "ratio":          round(ratio, 2),
        "signal":         signal,
        "summary":        summary,
    }


def _unknown_volume() -> dict:
    return {
        "avg_50d_volume": 0,
        "recent_volume":  0,
        "ratio":          0.0,
        "signal":         "unknown",
        "summary":        "Volume data unavailable.",
    }


def format_volume_for_prompt(symbol: str, info: dict) -> str:
    if not info or info.get("signal") == "unknown":
        return ""
    return (
        f"\n--- VOLUME CONFIRMATION — {symbol} ---\n"
        f"{info['summary']}\n"
    )
