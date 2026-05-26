"""
Post-Earnings Announcement Drift (PEAD) screener.

Detects stocks that gapped up ≥5% on ≥1.5× volume within the last
1-5 trading days and are still consolidating near the gap-day close
(not reversed, not extended).  No earnings API required — the gap-up
itself on elevated volume is the earnings-beat proxy.

Entry logic
-----------
  Gap day  : the triggering gap-up session (earnings catalyst)
  Entry    : current price (must be within 5% of gap-day close)
  Stop     : gap-day low × 0.99  — if the gap fills, the thesis is dead
  T1       : entry + 2R          — 50% exit, stop → breakeven
  T2       : entry + 3R          — trail with EMA20, exit before next earnings

Screening criteria
------------------
  • Gap-up ≥ 5%  (open vs prior close)
  • Volume on gap day ≥ 1.5× 50-day average
  • Gap occurred within the last LOOKBACK_DAYS trading days
  • Current price within 5% of gap-day close  (not extended or reversed)
  • Current price > gap-day low               (gap not fully filled)
  • Stage 2 structure: price > EMA50 > EMA150 (EMA200 is preferred but
    not required — some momentum stocks in early Stage 2 lag the 200MA)
  • R:R ≥ 1.5  (gap-anchored stop is tight; 1.5× is conservative)
  • SEPA score ≥ 3 (basic quality floor — not a junk stock)

Results are saved to weekly_plan as screener_type='pead', signal='PEAD_DRIFT'.
The existing fill_open_slots, pre-trade gate, T1 exit, and trailing-stop
machinery all apply unchanged.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Config (defaults — overridden by user settings in DB) ─────────────────────

LOOKBACK_DAYS    = 5     # how many recent trading sessions to inspect for gaps
MIN_GAP_PCT      = 0.05  # 5% minimum open-vs-prev-close gap
MIN_VOLUME_RATIO = 1.5   # gap-day volume vs 50d average
MAX_DRIFT_PCT    = 0.05  # current price must be within 5% of gap-day close
MIN_RR           = 1.5   # minimum R:R ratio (gap stop is structural — tighter)
MIN_SEPA_SCORE   = 3     # skip clear junk; full SEPA bar (≥5) not required
STOP_BUFFER      = 0.99  # stop = gap_day_low × STOP_BUFFER


def _load_pead_settings(db: Session, user_id: int) -> dict:
    """Read user-configurable PEAD thresholds from the settings table."""
    from .database import get_user_setting as _gs

    def _f(key: str, default: float) -> float:
        try:
            return float(_gs(db, key, str(default), user_id) or default)
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int) -> int:
        try:
            return int(float(_gs(db, key, str(default), user_id) or default))
        except (TypeError, ValueError):
            return default

    return {
        "lookback_days":    _i("pead_lookback_days",   LOOKBACK_DAYS),
        "min_gap_pct":      _f("pead_gap_min_pct",     MIN_GAP_PCT * 100) / 100,
        "min_vol_ratio":    _f("pead_vol_ratio_min",   MIN_VOLUME_RATIO),
        "max_drift_pct":    _f("pead_max_drift_pct",   MAX_DRIFT_PCT * 100) / 100,
        "min_rr":           _f("pead_min_rr",          MIN_RR),
        "min_sepa_score":   _i("pead_min_sepa_score",  MIN_SEPA_SCORE),
        "top_n":            _i("pead_top_n",            10),
        "enabled":          _gs(db, "pead_screener_enabled", "true", user_id) not in ("false", "0", ""),
    }


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan_pead_opportunities(
    db: Session,
    mode: str,
    user_id: int = 1,
    lookback_days: int | None = None,
) -> list[dict]:
    """
    Scan the combined universe (DEFAULT_UNIVERSE + current weekly plan)
    for PEAD setups. Returns a list of qualifying pick dicts.

    Each dict contains:
      symbol, entry_price, stop_price, target1, target2,
      gap_date, gap_pct, volume_ratio, rr, score, signal, rationale
    """
    cfg = _load_pead_settings(db, user_id)

    if not cfg["enabled"]:
        logger.info("PEAD screener disabled for user %s — skipping.", user_id)
        return []

    effective_lookback = lookback_days if lookback_days is not None else cfg["lookback_days"]

    universe = _build_universe(db, mode)
    logger.info("PEAD scan: checking %d symbols for earnings gaps [%s]", len(universe), mode)

    picks: list[dict] = []
    skipped_stage2  = 0
    skipped_no_gap  = 0
    skipped_rr      = 0
    skipped_drift   = 0
    errors          = 0

    for sym in universe:
        try:
            pick = _evaluate_symbol(
                sym, db, effective_lookback,
                min_gap_pct=cfg["min_gap_pct"],
                min_vol_ratio=cfg["min_vol_ratio"],
                max_drift_pct=cfg["max_drift_pct"],
            )
            if pick is None:
                skipped_no_gap += 1
                continue

            # Stage 2 check via TradingView analyzer
            score, stage2_ok = _check_stage2(sym, pick["entry_price"], db)
            if not stage2_ok:
                skipped_stage2 += 1
                logger.debug("PEAD: %s — failed Stage 2 check", sym)
                continue

            pick["score"] = score
            if score < cfg["min_sepa_score"]:
                skipped_stage2 += 1
                logger.debug("PEAD: %s — SEPA score %d < %d", sym, score, cfg["min_sepa_score"])
                continue

            # Validate R:R
            if pick.get("rr", 0) < cfg["min_rr"]:
                skipped_rr += 1
                logger.debug("PEAD: %s — R:R %.2f < %.1f", sym, pick["rr"], cfg["min_rr"])
                continue

            logger.info(
                "PEAD candidate: %s  gap=+%.1f%%  vol=%.1f×  price=$%.2f  "
                "stop=$%.2f  T1=$%.2f  T2=$%.2f  R:R=%.1f  score=%d [%s]",
                sym, pick["gap_pct"] * 100, pick["volume_ratio"],
                pick["entry_price"], pick["stop_price"],
                pick["target1"], pick["target2"], pick["rr"], score, mode,
            )
            picks.append(pick)

        except Exception as exc:
            errors += 1
            logger.debug("PEAD: error evaluating %s: %s", sym, exc)

    logger.info(
        "PEAD scan complete [%s]: %d candidates | "
        "skipped no-gap=%d stage2=%d rr=%d drift=%d errors=%d",
        mode, len(picks), skipped_no_gap, skipped_stage2,
        skipped_rr, skipped_drift, errors,
    )

    # Sort by R:R descending and cap at top_n
    picks.sort(key=lambda p: p.get("rr", 0), reverse=True)
    picks = picks[: cfg["top_n"]]

    if picks:
        _save_pead_picks(db, picks, mode)

    return picks


# ── Symbol evaluation ─────────────────────────────────────────────────────────

def _evaluate_symbol(
    sym: str,
    db: Session,
    lookback_days: int,
    min_gap_pct: float = MIN_GAP_PCT,
    min_vol_ratio: float = MIN_VOLUME_RATIO,
    max_drift_pct: float = MAX_DRIFT_PCT,
) -> Optional[dict]:
    """
    Fetch OHLCV and check for a qualifying PEAD gap.
    Returns a pick dict or None if no valid setup found.
    """
    from .strategies.yf_client import fetch_ohlcv
    df = fetch_ohlcv(sym, period_days=70)

    if df is None or df.empty:
        return None
    if len(df) < 15:
        return None

    # Need Open and Volume columns
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in df.columns:
            return None

    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) < 15:
        return None

    # 50-day average volume from the historical baseline (exclude recent window)
    baseline_vol = df["Volume"].iloc[:-lookback_days]
    if len(baseline_vol) < 10:
        return None
    avg_vol_50d = float(baseline_vol.tail(50).mean())
    if avg_vol_50d <= 0:
        return None

    # Scan the most recent `lookback_days` sessions for a gap-up
    recent   = df.iloc[-lookback_days:]   # candidate gap window
    gap_row  = None
    gap_idx  = None

    for i in range(len(recent)):
        bar_idx = len(df) - lookback_days + i
        if bar_idx < 1:
            continue
        prev_close = float(df["Close"].iloc[bar_idx - 1])
        day_open   = float(recent["Open"].iloc[i])
        day_vol    = float(recent["Volume"].iloc[i])

        if prev_close <= 0:
            continue

        gap_pct    = (day_open - prev_close) / prev_close
        vol_ratio  = day_vol / avg_vol_50d

        if gap_pct >= min_gap_pct and vol_ratio >= min_vol_ratio:
            # Take the most recent qualifying gap
            gap_row = recent.iloc[i]
            gap_idx = i
            _gap_pct   = gap_pct
            _vol_ratio = vol_ratio

    if gap_row is None:
        return None

    gap_day_low   = float(gap_row["Low"])
    gap_day_close = float(gap_row["Close"])
    current_price = float(df["Close"].iloc[-1])

    # Gap must not be filled — current price above the gap-day low
    if current_price <= gap_day_low:
        return None

    # Current price must not be more than MAX_DRIFT_PCT from the gap-day close
    # (either direction: too extended above = chasing; below gap close = reversing)
    drift_from_close = (current_price - gap_day_close) / gap_day_close
    if abs(drift_from_close) > max_drift_pct:
        return None

    # Compute exits anchored to the gap-day low
    stop    = round(gap_day_low * STOP_BUFFER, 2)
    entry   = round(current_price, 2)
    risk    = entry - stop
    if risk <= 0:
        return None

    target1 = round(entry + 2 * risk, 2)   # 2R
    target2 = round(entry + 3 * risk, 2)   # 3R
    rr      = round((target1 - entry) / risk, 2)

    # Infer gap date from DataFrame index
    try:
        gap_date_ts = df.index[-lookback_days + gap_idx]
        gap_date_str = pd.Timestamp(gap_date_ts).strftime("%Y-%m-%d")
    except Exception:
        gap_date_str = "unknown"

    return {
        "symbol":       sym,
        "entry_price":  entry,
        "stop_price":   stop,
        "target1":      target1,
        "target2":      target2,
        "gap_date":     gap_date_str,
        "gap_pct":      round(_gap_pct, 4),
        "volume_ratio": round(_vol_ratio, 2),
        "gap_day_low":  round(gap_day_low, 2),
        "gap_day_close": round(gap_day_close, 2),
        "rr":           rr,
        "score":        0,     # filled in by caller after Stage 2 check
        "signal":       "PEAD_DRIFT",
        "rationale":    (
            f"Gap +{_gap_pct*100:.1f}% on {_vol_ratio:.1f}× volume ({gap_date_str}). "
            f"PEAD setup: stop below gap-day low ${gap_day_low:.2f}, "
            f"T1 at 2R ${target1:.2f}, T2 at 3R ${target2:.2f}."
        ),
    }


# ── Stage 2 check ─────────────────────────────────────────────────────────────

def _check_stage2(sym: str, current_price: float, db: Session) -> tuple[int, bool]:
    """
    Verify Stage 2 structure using TradingView EMAs.
    Returns (sepa_score, is_stage2_ok).

    Requirements:
      • price > EMA50
      • EMA50 > EMA150  (full Stage 2 ladder preferred)
      • EMA150 > EMA200 is a bonus but not required (early Stage 2 stocks)
    Falls open on error — if TV data is unavailable, don't block the scan.
    """
    try:
        from .tv_analyzer import analyze as _tv_analyze
        tech = _tv_analyze(sym, db=db) or {}

        ema50  = float(tech.get("ema50")  or 0)
        ema150 = float(tech.get("ema150") or 0)
        ema200 = float(tech.get("ema200") or 0)
        score  = int(tech.get("score")    or 0)
        px     = float(tech.get("price")  or current_price)

        if ema50 <= 0 or ema150 <= 0:
            # Insufficient data — fail open, let pre-trade gate decide
            return score, True

        stage2 = (
            px     > ema50   # price above 50MA
            and ema50  > ema150  # 50MA above 150MA (Stage 2 ladder)
        )
        return score, stage2

    except Exception as exc:
        logger.debug("PEAD: Stage 2 check failed for %s: %s — passing through", sym, exc)
        return 0, True   # fail open


# ── Universe builder ──────────────────────────────────────────────────────────

def _build_universe(db: Session, mode: str) -> list[str]:
    """
    Build the scan universe by combining:
    1. DEFAULT_UNIVERSE — the fixed quality watchlist
    2. Current + prior week's weekly_plan symbols (already vetted quality stocks)

    Deduplicates and caps at 400 symbols for runtime safety.
    """
    symbols: set[str] = set()

    # 1. Static default universe
    try:
        from .screener import DEFAULT_UNIVERSE
        symbols.update(DEFAULT_UNIVERSE)
    except Exception as exc:
        logger.warning("PEAD: could not load DEFAULT_UNIVERSE: %s", exc)

    # 2. Recent weekly plan symbols (last 2 weeks, any status)
    try:
        rows = db.execute(
            text("""
                SELECT DISTINCT symbol FROM weekly_plan
                WHERE mode = :mode
                  AND week_start >= CURRENT_DATE - INTERVAL '14 days'
            """),
            {"mode": mode},
        ).fetchall()
        symbols.update(r[0] for r in rows if r[0])
    except Exception as exc:
        logger.debug("PEAD: weekly_plan universe lookup failed: %s", exc)

    # Remove any symbols already in a PEAD PENDING pick this week
    # (don't re-add what we already added last night)
    try:
        existing = {
            r[0] for r in db.execute(
                text("""
                    SELECT symbol FROM weekly_plan
                    WHERE mode = :mode
                      AND screener_type = 'pead'
                      AND status = 'PENDING'
                      AND week_start >= CURRENT_DATE - INTERVAL '7 days'
                """),
                {"mode": mode},
            ).fetchall()
        }
        symbols -= existing
    except Exception:
        pass

    result = sorted(symbols)[:400]
    return result


# ── DB persistence ────────────────────────────────────────────────────────────

def _save_pead_picks(db: Session, picks: list[dict], mode: str) -> None:
    """
    Upsert PEAD picks into weekly_plan.
    Uses the current Monday as week_start so they appear in this week's plan.
    Existing PENDING rows for the same symbol are skipped (don't overwrite).
    Already-executed symbols are also skipped.
    """
    from datetime import date as _date
    today     = _date.today()
    weekday   = today.weekday()   # 0=Mon, 6=Sun
    monday    = today - timedelta(days=weekday)

    inserted = 0
    skipped  = 0

    for i, pick in enumerate(picks):
        sym = pick["symbol"]
        try:
            # Skip if already in plan this week as any status
            existing = db.execute(
                text("""
                    SELECT status FROM weekly_plan
                    WHERE symbol   = :sym
                      AND mode     = :mode
                      AND week_start = :ws
                """),
                {"sym": sym, "mode": mode, "ws": monday},
            ).fetchone()

            if existing:
                skipped += 1
                continue

            # Find the current max rank to append below existing picks
            max_rank_row = db.execute(
                text("""
                    SELECT COALESCE(MAX(rank), 0) FROM weekly_plan
                    WHERE mode = :mode AND week_start = :ws
                """),
                {"mode": mode, "ws": monday},
            ).fetchone()
            base_rank = int(max_rank_row[0]) if max_rank_row else 0

            db.execute(
                text("""
                    INSERT INTO weekly_plan
                      (week_start, mode, symbol, rank, status,
                       entry_price, stop_price, target1, target2,
                       screener_type, signal, rationale)
                    VALUES
                      (:ws, :mode, :sym, :rank, 'PENDING',
                       :entry, :stop, :t1, :t2,
                       'pead', :signal, :rationale)
                """),
                {
                    "ws":       monday,
                    "mode":     mode,
                    "sym":      sym,
                    "rank":     base_rank + i + 1,
                    "entry":    pick["entry_price"],
                    "stop":     pick["stop_price"],
                    "t1":       pick["target1"],
                    "t2":       pick["target2"],
                    "signal":   pick["signal"],
                    "rationale": pick["rationale"][:500],
                },
            )
            db.commit()
            inserted += 1
            logger.info(
                "PEAD: saved %s to weekly_plan [%s] "
                "entry=$%.2f stop=$%.2f T1=$%.2f T2=$%.2f R:R=%.1f",
                sym, mode, pick["entry_price"], pick["stop_price"],
                pick["target1"], pick["target2"], pick["rr"],
            )

        except Exception as exc:
            db.rollback()
            logger.error("PEAD: failed to save %s: %s", sym, exc)

    logger.info(
        "PEAD save complete [%s]: %d inserted, %d skipped (already in plan)",
        mode, inserted, skipped,
    )


# ── Earnings proximity helper (re-exported for claude_analyst) ────────────────

def get_days_since_earnings(symbol: str, db: Session) -> Optional[int]:
    """
    Return the number of calendar days since the last earnings report,
    or None if unknown.  Uses the earnings_cache table (populated by
    pullback screener) — avoids a live network call at gate time.

    Logic: if the cached next_earnings date is in the past, the difference
    from today is how many days ago the report happened.  If next_earnings
    is in the future or unknown, returns None.
    """
    try:
        from .pullback_screener import _get_cached_earnings
        next_earn = _get_cached_earnings(db, symbol)
        if next_earn is None:
            return None
        days_until = (next_earn - date.today()).days
        if days_until < 0:
            # next_earnings is in the past → that's the last report date
            return abs(days_until)
        return None
    except Exception as exc:
        logger.debug("get_days_since_earnings: failed for %s: %s", symbol, exc)
        return None
