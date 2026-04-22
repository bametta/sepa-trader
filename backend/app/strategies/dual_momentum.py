"""
Dual Momentum — Gary Antonacci's Global Equity Momentum (GEM)

Algorithm:
  Step 1 — Absolute momentum:
    If SPY 12-month return > BIL (T-bill proxy) → positive absolute momentum;
    proceed to Step 2.  Otherwise → hold AGG (aggregate bonds, defensive).

  Step 2 — Relative momentum:
    Compare SPY vs EFA (international equities).
    Hold whichever has the higher 12-month return.

Rebalance: monthly (or on demand).
Universe: SPY, EFA, AGG, BIL
"""
import logging
from datetime import datetime

from .yf_client import fetch_history, get_current_price

logger = logging.getLogger(__name__)

ASSETS = {
    "SPY": "US Equities (S&P 500)",
    "EFA": "International Equities (MSCI EAFE)",
    "AGG": "US Aggregate Bonds",
    "BIL": "Short-term T-Bills (cash proxy)",
}

DEFAULT_LOOKBACK_MONTHS = 12


def _fetch_momentum(symbol: str, months: int) -> float:
    """
    Total price return over *months* calendar months.
    Returns 0.0 on any data error.
    """
    # Request slightly more days than needed to guarantee enough trading days
    period_days = int(months * 31.5) + 15
    try:
        hist = fetch_history(symbol, period_days=period_days)
        if len(hist) < 20:
            logger.warning("dual_momentum: only %d rows for %s (need 20)", len(hist), symbol)
            return 0.0
        first = float(hist["Close"].iloc[0])
        last  = float(hist["Close"].iloc[-1])
        if first == 0:
            return 0.0
        return (last / first) - 1.0
    except Exception as exc:
        logger.error("dual_momentum: failed to fetch momentum for %s: %s", symbol, exc)
        return 0.0


def evaluate(lookback_months: int = DEFAULT_LOOKBACK_MONTHS) -> dict:
    """
    Run the GEM algorithm.

    Returns a dict with:
      recommended_symbol  — SPY | EFA | AGG
      asset_class         — human-readable name
      momentum            — {symbol: float} for all four assets
      prices              — {symbol: float} current prices
      reasoning           — plain-English explanation
      lookback_months     — months used
      evaluated_at        — ISO timestamp
    """
    logger.info("dual_momentum: fetching %d-month momentum for all assets…", lookback_months)

    momentum = {sym: _fetch_momentum(sym, lookback_months) for sym in ASSETS}
    prices   = {sym: get_current_price(sym) for sym in ASSETS}

    spy_mom = momentum["SPY"]
    efa_mom = momentum["EFA"]
    bil_mom = momentum["BIL"]

    # ── GEM decision tree ─────────────────────────────────────────────────────
    if spy_mom > bil_mom:
        if spy_mom >= efa_mom:
            recommended = "SPY"
            reasoning = (
                f"Absolute momentum positive: US equities ({spy_mom:+.1%}) beat T-bills "
                f"({bil_mom:+.1%}). Relative momentum: US ({spy_mom:+.1%}) leads "
                f"international ({efa_mom:+.1%}). → Hold SPY."
            )
        else:
            recommended = "EFA"
            reasoning = (
                f"Absolute momentum positive: equities beat T-bills ({bil_mom:+.1%}). "
                f"Relative momentum: international ({efa_mom:+.1%}) leads US "
                f"({spy_mom:+.1%}). → Hold EFA."
            )
    else:
        recommended = "AGG"
        reasoning = (
            f"Absolute momentum negative: US equities ({spy_mom:+.1%}) below T-bills "
            f"({bil_mom:+.1%}). Defensive posture. → Hold AGG (bonds)."
        )

    return {
        "recommended_symbol": recommended,
        "asset_class":        ASSETS[recommended],
        "momentum":           {k: round(v, 4) for k, v in momentum.items()},
        "prices":             {k: round(v, 2) for k, v in prices.items()},
        "reasoning":          reasoning,
        "lookback_months":    lookback_months,
        "evaluated_at":       datetime.utcnow().isoformat(),
    }
