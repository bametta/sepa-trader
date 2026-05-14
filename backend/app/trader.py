"""
Auto-execution engine: fires trades based on SEPA signals.
- Every monitor cycle (interval = monitor_interval_minutes, default 30):
  trailing stop adjustment + signal evaluation
- Pre-trade AI gate runs before every buy order
- Exit guard ensures every position has an OCO at all times,
  and replaces existing OCOs when the plan's stop/target has changed
- Live accounts under $10K use small-account position limits automatically
"""
import asyncio
import logging
from datetime import datetime
import pytz
from sqlalchemy import text
from sqlalchemy.orm import Session
from . import alpaca_client as alp
from .sepa_analyzer import analyze
from .database import get_setting, set_setting, get_all_user_settings
from . import telegram_alerts as tg

_ET = pytz.timezone("America/New_York")

logger = logging.getLogger(__name__)

# Symbols blocked by PDT protection this session (cleared at market open).
# Prevents the monitor from spamming PDT alerts every cycle when there is
# genuinely nothing we can do until the next trading day.
_pdt_blocked: set[tuple[str, str]] = set()  # (symbol, mode)

# Symbols that have already triggered a size-mismatch alert this session.
# Cleared at market open so a fresh alert fires the next day if still wrong.
_size_mismatch_alerted: set[tuple[str, str]] = set()  # (symbol, mode)


def _size_position(
    portfolio_value: float,
    price: float,
    risk_pct: float,
    stop_pct: float,
    stop_price: float = 0.0,
) -> float:
    risk_dollars  = portfolio_value * (risk_pct / 100)
    stop_distance = (price - stop_price) if stop_price > 0 and price > stop_price else price * (stop_pct / 100)
    if stop_distance <= 0:
        return 0
    return risk_dollars / stop_distance


def _effective_max_positions(db: Session, mode: str) -> int:
    """
    For live accounts under $10K, cap max_positions at 3 regardless of settings.
    Paper accounts always use the settings value.
    """
    configured = int(get_setting(db, "max_positions", "10"))
    if mode != "live":
        return configured
    try:
        from .database import get_live_account_limits
        acct   = alp.get_account(mode)
        limits = get_live_account_limits(float(acct.portfolio_value))
        cap    = limits.get("max_positions")
        if cap is not None:
            effective = min(configured, cap)
            if effective != configured:
                logger.info(
                    "Live account %s: max_positions capped at %d (settings=%d)",
                    limits.get("tier", ""), effective, configured,
                )
            return effective
    except Exception as exc:
        logger.warning("_effective_max_positions: could not fetch account — using settings: %s", exc)
    return configured


def _get_weekly_plan_exits(db: Session, symbol: str, mode: str) -> tuple[float, float, float]:
    """Return (stop_price, target1, target2) — most recent EXECUTED plan row.
    target2 is used as the OCO target when t1 hasn't been taken yet, so the
    software-managed T1 partial exit can fire before Alpaca's OCO does."""
    row = db.execute(
        text("""
            SELECT stop_price, target1, target2
            FROM weekly_plan
            WHERE symbol = :sym
              AND mode = :mode
              AND status = 'EXECUTED'
            ORDER BY week_start DESC
            LIMIT 1
        """),
        {"sym": symbol, "mode": mode},
    ).fetchone()
    if not row:
        return 0.0, 0.0, 0.0
    return float(row[0] or 0), float(row[1] or 0), float(row[2] or 0)


def _compute_fresh_exits(db: Session, symbol: str, current_price: float) -> tuple[float, float, str]:
    """Pure compute: derive (stop, target, basis) from current price + EMAs.
    No DB persistence. Used for slot-fill re-evaluation where we want fresh
    levels but write through the existing weekly_plan row.
    Returns (0, 0, "") on failure.
    """
    try:
        from .tv_analyzer import analyze as _analyze
        tech = _analyze(symbol, db=db) or {}
    except Exception as exc:
        logger.warning("_compute_fresh_exits: analyze(%s) failed: %s", symbol, exc)
        tech = {}

    px_now = float(tech.get("price") or current_price or 0)
    if px_now <= 0:
        return 0.0, 0.0, ""

    ema20 = float(tech.get("ema20") or 0)
    ema50 = float(tech.get("ema50") or 0)
    try:
        stop_pct = float(get_setting(db, "stop_loss_pct", "8.0") or "8.0")
        rr       = float(get_setting(db, "default_rr", "2.5") or "2.5")
    except (TypeError, ValueError):
        stop_pct, rr = 8.0, 2.5

    stop_floor, stop_ceiling = round(px_now * 0.85, 2), round(px_now * 0.96, 2)
    fallback = round(px_now * (1 - stop_pct / 100), 2)

    chosen, basis = 0.0, "fallback"
    if ema20 > 0:
        gap = (px_now - ema20) / px_now * 100
        if 1.0 <= gap <= 6.0:
            chosen, basis = round(ema20 * 0.99, 2), "EMA20"
    if chosen <= 0 and ema50 > 0:
        gap = (px_now - ema50) / px_now * 100
        if 2.0 <= gap <= 12.0:
            chosen, basis = round(ema50 * 0.99, 2), "EMA50"
    if chosen <= 0:
        chosen = fallback

    chosen = max(stop_floor, min(chosen, stop_ceiling))
    target = round(px_now + (px_now - chosen) * rr, 2)
    return chosen, target, basis


def _derive_fresh_plan(
    db: Session, symbol: str, mode: str, entry_price: float, current_price: float,
    user_id: int | None = None,
) -> tuple[float, float]:
    """For an open position with no weekly_plan row, derive a fresh stop/target
    from CURRENT price action — not from the original entry — and persist it
    so subsequent cycles read the same plan.

    Why current-price-anchored: a stale "default" of entry × (1−stop_pct) is
    obsolete the moment the stock has moved meaningfully off the entry. If the
    position is up 30%, we should ratchet the stop to lock in some gain
    (structural EMA20/EMA50 below current), not anchor to the original entry.

    Stop selection (in priority order):
      1. EMA20 if it's 1–6% below current → tightest structural support
      2. EMA50 if it's 2–12% below current → wider structural support
      3. current × (1 − stop_pct%) fallback (hard floor)
    Stop is clamped to [current × 0.85, current × 0.96] to prevent both
    catastrophic-wide and whipsaw-tight stops.

    Target = current + (current − stop) × default_rr (default 2.5R).
    Persists as a weekly_plan row with rank=99, status='EXECUTED' so it
    coexists with screener-driven plans.
    """
    try:
        from .tv_analyzer import analyze as _analyze
        tech = _analyze(symbol, db=db) or {}
    except Exception as exc:
        logger.warning("_derive_fresh_plan: analyze(%s) failed: %s", symbol, exc)
        tech = {}

    px_now = float(tech.get("price") or current_price or entry_price or 0)
    if px_now <= 0:
        return 0.0, 0.0

    ema20 = float(tech.get("ema20") or 0)
    ema50 = float(tech.get("ema50") or 0)

    try:
        stop_pct = float(get_setting(db, "stop_loss_pct", "8.0") or "8.0")
        rr       = float(get_setting(db, "default_rr", "2.5") or "2.5")
    except (TypeError, ValueError):
        stop_pct, rr = 8.0, 2.5

    stop_floor   = round(px_now * 0.85, 2)
    stop_ceiling = round(px_now * 0.96, 2)
    fallback     = round(px_now * (1 - stop_pct / 100), 2)

    chosen_stop = 0.0
    basis = "fallback"
    if ema20 > 0:
        gap_pct = (px_now - ema20) / px_now * 100
        if 1.0 <= gap_pct <= 6.0:
            chosen_stop = round(ema20 * 0.99, 2)
            basis = "EMA20"
    if chosen_stop <= 0 and ema50 > 0:
        gap_pct = (px_now - ema50) / px_now * 100
        if 2.0 <= gap_pct <= 12.0:
            chosen_stop = round(ema50 * 0.99, 2)
            basis = "EMA50"
    if chosen_stop <= 0:
        chosen_stop = fallback

    chosen_stop = max(stop_floor, min(chosen_stop, stop_ceiling))

    # Entry-price floor: if the position is meaningfully in profit (>3% above
    # entry), never place a stop below the purchase price.  A pre-market runner
    # should at minimum exit at breakeven, not at a structural level that was
    # valid at the original entry but is now a loss relative to cost basis.
    # Only enforced when the stock is clearly up — flat/losing positions keep
    # their structural stop so the trade has room to work.
    if entry_price > 0 and px_now > entry_price * 1.03:
        chosen_stop = max(chosen_stop, round(entry_price, 2))

    t1 = round(px_now + (px_now - chosen_stop) * rr, 2)        # 2.5R default
    t2 = round(px_now + (px_now - chosen_stop) * rr * 1.5, 2)  # ~3.75R — 50% further than T1

    # Persist so we don't re-derive every monitor cycle
    try:
        db.execute(
            text("""
                INSERT INTO weekly_plan
                    (week_start, symbol, rank, score, entry_price, stop_price,
                     target1, target2, status, mode, user_id)
                VALUES (
                    COALESCE(
                        (SELECT MAX(week_start) FROM weekly_plan
                         WHERE mode = :mode
                           AND (:uid IS NULL OR user_id = :uid)),
                        CURRENT_DATE
                    ),
                    :sym, 99, 0, :entry, :stop, :t1, :t2, 'EXECUTED', :mode, :uid
                )
            """),
            {"sym": symbol, "entry": entry_price or px_now,
             "stop": chosen_stop, "t1": t1, "t2": t2, "mode": mode, "uid": user_id},
        )
        db.commit()
    except Exception as exc:
        logger.warning("_derive_fresh_plan: persist(%s) failed: %s", symbol, exc)

    logger.warning(
        "Exit guard: %s reanalyzed (no plan) — basis=%s px=$%.2f stop=$%.2f "
        "t1=$%.2f t2=$%.2f (R:R=%.1f) [%s]",
        symbol, basis, px_now, chosen_stop, t1, t2, rr, mode,
    )
    return chosen_stop, t1


def _get_current_stop_price(orders: list) -> float | None:
    """Extract the active stop price from open OCO/bracket orders.

    Alpaca returns OCO siblings as separate top-level orders in get_orders()
    responses — the "held" stop leg has no order_class set, only order_type=stop
    (or stop_limit). We try the structured OCO parent first, then fall back to
    scanning standalone stop-type sell orders so the price-match check works
    even when .legs is empty in the list response.
    """
    # Pass 1: structured OCO parent with populated .legs
    for o in orders:
        order_class = str(getattr(o, 'order_class', '') or '').lower()
        side        = str(getattr(o, 'side',        '') or '').lower()
        if 'sell' not in side:
            continue
        if any(kw in order_class for kw in ('oco', 'bracket', 'oto')):
            legs = getattr(o, 'legs', None) or []
            for leg in legs:
                order_type = str(getattr(leg, 'type', '') or '').lower()
                if 'stop' in order_type:
                    sp = getattr(leg, 'stop_price', None)
                    if sp is not None:
                        return float(sp)
            sp = getattr(o, 'stop_price', None)
            if sp is not None:
                return float(sp)
    # Pass 2: standalone stop-type sell order (OCO "held" sibling returned
    # by Alpaca as a separate top-level entry with no order_class)
    for o in orders:
        side       = str(getattr(o, 'side',       '') or '').lower()
        order_type = str(getattr(o, 'order_type', '') or getattr(o, 'type', '') or '').lower()
        if 'sell' in side and 'stop' in order_type:
            sp = getattr(o, 'stop_price', None)
            if sp is not None:
                return float(sp)
    return None


def _get_current_target_price(orders: list) -> float | None:
    """Extract the active target (limit) price from open OCO/bracket orders.

    Same two-pass strategy as _get_current_stop_price: try the structured OCO
    parent first, then fall back to standalone limit-type sell orders.
    """
    # Pass 1: structured OCO parent with populated .legs
    for o in orders:
        order_class = str(getattr(o, 'order_class', '') or '').lower()
        side        = str(getattr(o, 'side',        '') or '').lower()
        if 'sell' not in side:
            continue
        if any(kw in order_class for kw in ('oco', 'bracket', 'oto')):
            lp = getattr(o, 'limit_price', None)
            if lp is not None:
                return float(lp)
            legs = getattr(o, 'legs', None) or []
            for leg in legs:
                order_type = str(getattr(leg, 'type', '') or '').lower()
                if 'limit' in order_type:
                    lp = getattr(leg, 'limit_price', None)
                    if lp is not None:
                        return float(lp)
    # Pass 2: standalone limit-type sell order (OCO "new" sibling)
    for o in orders:
        side       = str(getattr(o, 'side',       '') or '').lower()
        order_type = str(getattr(o, 'order_type', '') or getattr(o, 'type', '') or '').lower()
        if 'sell' in side and 'limit' in order_type and 'stop' not in order_type:
            lp = getattr(o, 'limit_price', None)
            if lp is not None:
                return float(lp)
    return None


# ── Trailing stop tiers (Apex v2) ────────────────────────────────────────────
# Only applied when position is green — never touches red positions.
# Tier 1 — gain >= 1R : move stop to breakeven (entry)
# Tier 2+ — gain >= 2R: EMA20 × 0.99 structural trailing (tracks momentum);
#                        falls back to entry + 1R when EMA20 is unavailable.
# Stop only ever moves UP — never down.

_MIN_STOP_IMPROVEMENT_PCT = 0.005


def _compute_new_stop(
    entry: float,
    original_stop: float,
    current_price: float,
    ema20: float = 0.0,
) -> float | None:
    """Compute the new stop level for a green position.

    Tier 1 (>=1R): move to breakeven.
    Tier 2+ (>=2R): EMA20 × 0.99 structural trailing when EMA20 > entry;
                    falls back to entry + 1R when EMA20 is below entry or
                    unavailable (e.g. TV API failure).
    """
    R = entry - original_stop
    if R <= 0:
        return None
    gain_r = (current_price - entry) / R

    if gain_r >= 2.0:
        if ema20 > 0 and ema20 > entry:
            new_stop = round(ema20 * 0.99, 2)     # structural: just below EMA20
        else:
            new_stop = round(entry + R, 2)          # fallback: lock in 1R profit
    elif gain_r >= 1.0:
        new_stop = round(entry, 2)                  # breakeven
    else:
        return None

    max_stop = round(current_price * 0.995, 2)
    return min(new_stop, max_stop)


def _get_plan_stop_for_trailing(
    db: Session, symbol: str, mode: str,
) -> tuple[float, float, float]:
    """Return (r_ref_stop, target, current_plan_stop) for trailing-stop logic.

    r_ref_stop      — stop used for R calculation. After a T1 partial exit the
                      plan stop moves to breakeven; we keep the original
                      structural stop so gain_r stays meaningful.
    target          — current target1 in the plan.
    current_plan_stop — the live stop_price (may be BE after T1).
    """
    row = db.execute(
        text("""
            SELECT stop_price, target1, original_stop, t1_taken
            FROM weekly_plan
            WHERE symbol = :sym AND mode = :mode
              AND status = 'EXECUTED'
            ORDER BY week_start DESC LIMIT 1
        """),
        {"sym": symbol, "mode": mode},
    ).fetchone()
    if not row:
        return 0.0, 0.0, 0.0
    current_stop = float(row[0] or 0)
    target       = float(row[1] or 0)
    orig_stop    = float(row[2] or 0)
    t1_taken     = bool(row[3])
    # If T1 was already taken, original_stop holds the pre-BE structural level
    r_ref = orig_stop if (t1_taken and orig_stop > 0) else current_stop
    return r_ref, target, current_stop


def _adjust_trailing_stops(
    db: Session,
    positions: list,
    open_orders_by_symbol: dict,
    mode: str,
):
    """
    Ratchet stops upward for green positions. Red positions untouched.
    Updates weekly_plan.stop_price so exit guard stays in sync.
    """
    for pos in positions:
        sym           = pos.symbol
        current_price = float(pos.current_price)
        entry         = float(pos.avg_entry_price)
        qty           = float(pos.qty)

        if current_price <= entry:
            continue  # red or flat — never touch

        r_ref_stop, target, current_plan_stop = _get_plan_stop_for_trailing(db, sym, mode)
        if r_ref_stop <= 0 or target <= 0:
            logger.debug("Trailing stop: %s has no plan exits — skipping.", sym)
            continue

        # Fetch EMA20 for positions at >= 2R gain so we can use structural trailing
        R      = entry - r_ref_stop
        gain_r = (current_price - entry) / R if R > 0 else 0.0
        ema20  = 0.0
        if R > 0 and gain_r >= 2.0:
            try:
                from .tv_analyzer import analyze as _tv_analyze
                tech  = _tv_analyze(sym) or {}
                ema20 = float(tech.get("ema20") or 0)
            except Exception as _ema_exc:
                logger.debug("Trailing stop: EMA20 fetch failed for %s: %s — fallback", sym, _ema_exc)

        new_stop = _compute_new_stop(entry, r_ref_stop, current_price, ema20)
        if new_stop is None:
            continue  # gain < 1R

        # Cap the new stop just below the take-profit so Alpaca never rejects
        # an OCO whose stop sits above its limit.
        if target > 0:
            target_cap = round(target * 0.999, 2)
            if new_stop >= target_cap:
                logger.debug(
                    "Trailing stop: %s new=$%.2f would breach target cap $%.2f — clamping.",
                    sym, new_stop, target_cap,
                )
                new_stop = target_cap

        live_stop         = _get_current_stop_price(open_orders_by_symbol.get(sym, []))
        effective_current = live_stop if live_stop else current_plan_stop

        if new_stop <= effective_current * (1 + _MIN_STOP_IMPROVEMENT_PCT):
            logger.debug(
                "Trailing stop: %s new=$%.2f vs current=$%.2f — no update needed.",
                sym, new_stop, effective_current,
            )
            continue

        logger.info(
            "Trailing stop: %s  gain=%.1fR  price=$%.2f  old=$%.2f → new=$%.2f  "
            "target=$%.2f [%s]",
            sym, gain_r, current_price, effective_current, new_stop, target, mode,
        )

        try:
            alp.replace_oca_exit(sym, qty, new_stop, target, mode)

            db.execute(
                text("""
                    UPDATE weekly_plan
                    SET stop_price = :stop
                    WHERE symbol = :sym AND mode = :mode
                      AND week_start = (
                          SELECT MAX(week_start) FROM weekly_plan
                          WHERE symbol = :sym AND mode = :mode
                      )
                """),
                {"stop": new_stop, "sym": sym, "mode": mode},
            )
            db.commit()
            logger.info(
                "Trailing stop updated: %s $%.2f → $%.2f (gain=%.1fR) [%s]",
                sym, effective_current, new_stop, gain_r, mode,
            )
        except Exception as exc:
            logger.error("Trailing stop update failed for %s: %s", sym, exc)
            # The replace cancelled the old OCO before placing the new one,
            # so a placement failure leaves the position naked until the next
            # monitor cycle. Alert immediately.
            try:
                tg.alert_system_error_sync(
                    f"NAKED POSITION [{mode}] {sym} — trailing-stop replace failed",
                    exc,
                )
            except Exception:
                pass


# ── Apex Pillar 2: Software-managed T1 partial exit ──────────────────────────
# Bypasses Alpaca's bracket-stacking limitation. When price hits target1 (2R),
# sell 50% of the position at market, move the stop to breakeven, and let
# the remaining 50% run to target2 (3R) with an EMA20-anchored trailing stop.

_t1_alerted: set = set()   # session-level dedup; prevents repeat Telegram alerts


def _check_t1_partial_exit(
    db: Session,
    positions: list,
    open_orders_by_symbol: dict,
    mode: str,
) -> None:
    """Fire a 50% market sell when price >= target1 and t1 hasn't been taken yet.

    After the partial exit:
    - OCO is replaced: remaining shares, stop = breakeven (entry), target = target2.
    - weekly_plan: t1_taken=TRUE, original_stop saved, stop_price → entry, target1 → target2.
    """
    for pos in positions:
        sym   = pos.symbol
        qty   = int(float(pos.qty))
        price = float(pos.current_price)
        entry = float(pos.avg_entry_price)

        if qty < 2:          # need at least 2 shares to split
            continue
        if price <= entry:   # only fire on green positions
            continue

        try:
            plan = db.execute(
                text("""
                    SELECT target1, target2, entry_price, stop_price, t1_taken
                    FROM weekly_plan
                    WHERE symbol = :sym AND mode = :mode
                      AND status = 'EXECUTED'
                    ORDER BY week_start DESC LIMIT 1
                """),
                {"sym": sym, "mode": mode},
            ).fetchone()
        except Exception as exc:
            logger.warning("T1 check: DB fetch failed for %s: %s", sym, exc)
            continue

        if not plan or plan[4]:   # missing plan or t1 already taken
            continue

        t1          = float(plan[0] or 0)
        t2          = float(plan[1] or 0)
        plan_entry  = float(plan[2] or entry)
        orig_stop   = float(plan[3] or 0)   # structural stop before we overwrite

        if t1 <= 0 or price < t1:
            continue

        # ── T1 hit ─────────────────────────────────────────────────────────
        sell_qty   = max(1, qty // 2)
        remain_qty = qty - sell_qty
        be_stop    = round(plan_entry, 2)    # breakeven = our fill entry
        exit_tgt   = t2 if t2 > 0 else round(price * 1.04, 2)   # fallback 4%

        logger.info(
            "T1 PARTIAL EXIT [%s]: %s price=$%.2f hit T1=$%.2f — "
            "selling %d/%d shares | stop→BE $%.2f | target→T2 $%.2f",
            mode, sym, price, t1, sell_qty, qty, be_stop, exit_tgt,
        )

        # Cancel the existing OCO BEFORE placing the market sell.
        # Without this, the OCO target leg (at T1 price) fires concurrently
        # and sells the full position alongside our half-share market sell.
        try:
            cancelled = alp.cancel_symbol_exit_orders(sym, mode)
            if cancelled:
                cleared = alp.wait_for_orders_cancelled(
                    sym, mode, timeout=6.0, poll_interval=0.5, order_ids=cancelled
                )
                if not cleared:
                    alp.cancel_symbol_exit_orders(sym, mode)  # second sweep
                    import time as _t; _t.sleep(1.5)
                    logger.warning(
                        "T1 partial exit: %s OCO cancel still settling — proceeding [%s]",
                        sym, mode,
                    )
        except Exception as exc:
            logger.warning("T1 partial exit: OCO cancel failed for %s: %s — proceeding", sym, exc)

        # Sell half
        try:
            alp.place_market_sell(sym, sell_qty, mode)
            _log_trade(db, sym, "SELL", sell_qty, price, "T1_PARTIAL", mode)
        except Exception as exc:
            logger.error("T1 partial exit: sell failed for %s: %s", sym, exc)
            continue

        # Place fresh OCO for the remaining half: stop = BE, target = T2
        if remain_qty > 0:
            import time as _t; _t.sleep(1.0)   # let the market sell settle before placing exits
            try:
                alp.replace_oca_exit(sym, remain_qty, be_stop, exit_tgt, mode)
            except Exception as exc:
                logger.error(
                    "T1 partial exit: OCO replace failed for %s "
                    "(remain=%d stop=$%.2f tgt=$%.2f): %s",
                    sym, remain_qty, be_stop, exit_tgt, exc,
                )

        # Persist: save original_stop, mark t1_taken, move stop to BE, update target
        try:
            db.execute(
                text("""
                    UPDATE weekly_plan
                    SET t1_taken      = TRUE,
                        original_stop = :orig,
                        stop_price    = :be,
                        target1       = :t2
                    WHERE symbol = :sym AND mode = :mode
                      AND status = 'EXECUTED'
                      AND week_start = (
                          SELECT MAX(week_start) FROM weekly_plan
                          WHERE symbol = :sym AND mode = :mode
                            AND status = 'EXECUTED'
                      )
                """),
                {"orig": orig_stop, "be": be_stop, "t2": exit_tgt,
                 "sym": sym, "mode": mode},
            )
            db.commit()
        except Exception as exc:
            logger.error("T1 partial exit: DB update failed for %s: %s", sym, exc)
            db.rollback()

        # Alert (once per position per session)
        if (sym, mode) not in _t1_alerted:
            _t1_alerted.add((sym, mode))
            try:
                tg.send_sync(
                    f"*T1 PARTIAL EXIT* [{mode.upper()}] {sym}\n\n"
                    f"Sold `{sell_qty}` shares @ `${price:.2f}` (T1 = `${t1:.2f}`)\n"
                    f"Remaining: `{remain_qty}` shares | Stop → BE `${be_stop:.2f}` | "
                    f"Target → T2 `${exit_tgt:.2f}`\n"
                    f"🎯 Banked 2R on half — zero cost-basis on remainder",
                    level="INFO",
                )
            except Exception:
                pass


# ── Apex Pillar 4: Time stop ──────────────────────────────────────────────────
# Positions that drift flat or negative for too many trading days are dead
# money — close them to recycle buying power into fresh setups.

def _check_time_stops(
    db: Session,
    positions: list,
    mode: str,
) -> None:
    """Close positions that have been open >= time_stop_days without sufficient
    gain. Sends a Telegram alert before closing so the trade can be overridden.

    Settings:
      time_stop_days        — trading days threshold (default 10; 0 = disabled)
      time_stop_max_gain_pct — max unrealized gain % below which time stop fires
                               (default 2.0; positions above this are left alone)
    """
    try:
        ts_days = int(get_setting(db, "time_stop_days",        "10")  or "10")
        ts_gain = float(get_setting(db, "time_stop_max_gain_pct", "2.0") or "2.0")
    except (TypeError, ValueError):
        ts_days, ts_gain = 10, 2.0

    if ts_days <= 0:
        return   # feature disabled

    now = datetime.now(_ET)

    for pos in positions:
        sym           = pos.symbol
        qty           = float(pos.qty)
        current_price = float(pos.current_price)
        avg_entry     = float(pos.avg_entry_price)

        try:
            row = db.execute(
                text("""
                    SELECT created_at FROM trade_log
                    WHERE symbol = :sym AND mode = :mode AND action = 'BUY'
                    ORDER BY created_at DESC LIMIT 1
                """),
                {"sym": sym, "mode": mode},
            ).fetchone()
        except Exception as exc:
            logger.warning("Time stop: DB fetch failed for %s: %s", sym, exc)
            continue

        if not row:
            continue

        entry_ts = row[0]
        if entry_ts.tzinfo is None:
            entry_ts = pytz.utc.localize(entry_ts)

        calendar_days = (now.astimezone(pytz.utc) - entry_ts).days
        trading_days  = int(calendar_days * 5 / 7)   # approx 5 market days / 7 cal days

        if trading_days < ts_days:
            continue

        unrealized_pct = (current_price - avg_entry) / avg_entry * 100 if avg_entry > 0 else 0.0

        if unrealized_pct >= ts_gain:
            continue   # position is running — let it ride

        logger.info(
            "TIME STOP [%s]: %s — %d trading days, unrealized=%.1f%% < %.1f%% threshold — closing",
            mode, sym, trading_days, unrealized_pct, ts_gain,
        )

        try:
            tg.send_sync(
                f"*TIME STOP* [{mode.upper()}] {sym}\n\n"
                f"Open `{trading_days}` trading days | "
                f"Unrealized: `{unrealized_pct:+.1f}%` (threshold `<{ts_gain:+.1f}%`)\n"
                f"🕐 Closing — recycling buying power into fresh setups",
                level="WARN",
            )
        except Exception:
            pass

        try:
            # Cancel existing OCO first — shares held_for_orders block a
            # market close ("insufficient qty available").
            _cancelled = alp.cancel_symbol_exit_orders(sym, mode)
            if _cancelled:
                alp.wait_for_orders_cancelled(
                    sym, mode, timeout=8.0, poll_interval=0.5, order_ids=_cancelled
                )
            alp.close_position(sym, mode)
            _log_trade(db, sym, "SELL", qty, current_price, "TIME_STOP", mode)
        except Exception as exc:
            logger.error("Time stop: close failed for %s: %s", sym, exc)


# ── Exit guard ────────────────────────────────────────────────────────────────

# Minimum relative move before an OCO is cancelled and replaced.
# Flat-dollar thresholds cause constant churn at 1-min intervals because
# EMA20/EMA50 drift a few cents every tick.  0.5% means a $50 stop must
# move at least $0.25 before we touch the bracket — eliminating noise
# cancellations that were eating 10s timeouts per position per cycle.
def _price_changed(current: float, plan: float) -> bool:
    """Return True only when prices differ by > 0.5% — avoids OCO churn."""
    if current is None or current <= 0 or plan <= 0:
        return True
    return abs(current - plan) / max(current, plan) > 0.005


def _ensure_exit_orders(
    db: Session,
    positions: list,
    open_orders_by_symbol: dict,
    mode: str,
    user_id: int | None = None,
):
    """
    For every live position:
      - If no OCO exists: cancel orphaned orders and place a fresh OCO
      - If an OCO exists but stop/target differ from the plan: replace it
      - If an OCO exists and prices match the plan: leave it alone

    This ensures that edits made via 'Set Stop / Target' (Auto mode) take
    effect on the next monitor cycle without requiring manual intervention.
    """
    client = alp.get_client(mode)

    # Cancel exit orders for symbols that have no live position (e.g., user
    # closed manually, or a previous close left dangling legs). Otherwise the
    # orphan OCO can fire against a fresh re-entry, prematurely closing the
    # new position.
    held_symbols = {p.symbol for p in positions}
    for sym in list(open_orders_by_symbol.keys()):
        if sym in held_symbols:
            continue
        for o in open_orders_by_symbol.get(sym, []):
            side = str(getattr(o, "side", "") or "").lower()
            if "sell" not in side:
                continue
            try:
                client.cancel_order_by_id(str(o.id))
                logger.info(
                    "Exit guard: cancelled orphan exit %s for %s (no live position) [%s]",
                    o.id, sym, mode,
                )
            except Exception as exc:
                logger.warning(
                    "Exit guard: could not cancel orphan %s for %s: %s",
                    o.id, sym, exc,
                )

    for pos in positions:
        sym = pos.symbol
        qty = int(float(pos.qty))
        if qty <= 0:
            continue

        # Skip silently if Alpaca's PDT protection already blocked all exits
        # for this symbol today. The monitor will succeed tomorrow once the
        # buy is no longer a same-day trade. _pdt_blocked is cleared at open.
        if (sym, mode) in _pdt_blocked:
            logger.debug(
                "Exit guard: %s skipped — PDT-blocked this session [%s]", sym, mode
            )
            continue

        stop, t1, t2 = _get_weekly_plan_exits(db, sym, mode)
        sym_orders = open_orders_by_symbol.get(sym, [])

        # ── Position-size mismatch alert ─────────────────────────────────
        # If the actual position is >20% larger than the plan intended, something
        # went wrong (e.g. double entry). Alert once per session per symbol so
        # the monitor doesn't spam. Cleared at market open each day.
        try:
            planned_qty_row = db.execute(
                text("""
                    SELECT position_size FROM weekly_plan
                    WHERE symbol = :sym AND mode = :mode
                    ORDER BY week_start DESC LIMIT 1
                """),
                {"sym": sym, "mode": mode},
            ).fetchone()
            planned_qty = int(float(planned_qty_row[0] or 0)) if planned_qty_row else 0
            if planned_qty > 0 and qty > planned_qty * 1.2:
                logger.warning(
                    "Exit guard: %s position size mismatch — actual=%d planned=%d (%.0f%% over) [%s]",
                    sym, qty, planned_qty, (qty / planned_qty - 1) * 100, mode,
                )
                if (sym, mode) not in _size_mismatch_alerted:
                    _size_mismatch_alerted.add((sym, mode))
                    try:
                        tg.alert_system_error_sync(
                            f"⚠️ SIZE MISMATCH [{mode}] {sym}\n"
                            f"Position is {qty} shares but plan expected {planned_qty} "
                            f"({(qty / planned_qty - 1) * 100:.0f}% over). "
                            f"Possible double entry — review and adjust manually.",
                            RuntimeError(f"actual={qty} planned={planned_qty}"),
                        )
                    except Exception:
                        pass
        except Exception as _size_exc:
            logger.debug("Exit guard: size-mismatch check failed for %s: %s", sym, _size_exc)

        # ── In-flight BUY guard ──────────────────────────────────────────
        # If an entry order is still partially filling, pos.qty may grow.
        # Defer all exit work; next tick reconciles once the BUY settles.
        buy_in_flight = any(
            'buy' in str(getattr(o, 'side', '') or '').lower()
            and str(getattr(o, 'status', '') or '').lower() in (
                'new', 'accepted', 'pending_new', 'partially_filled', 'held'
            )
            for o in sym_orders
        )
        if buy_in_flight:
            logger.info("Exit guard: %s has in-flight BUY — deferring [%s]", sym, mode)
            continue

        # ── Check T1 status before any exit derivation ───────────────────
        # If T1 has already been taken, the stop is intentionally at breakeven
        # (entry price). Re-deriving would overwrite the BE stop with a new
        # structural level that could be below entry — always preserve it.
        try:
            t1_row = db.execute(
                text("""
                    SELECT t1_taken FROM weekly_plan
                    WHERE symbol = :sym AND mode = :mode
                      AND status = 'EXECUTED'
                    ORDER BY week_start DESC LIMIT 1
                """),
                {"sym": sym, "mode": mode},
            ).fetchone()
            t1_taken = bool(t1_row[0]) if t1_row else False
        except Exception:
            t1_taken = False

        # ── Derive exits only when the plan is missing ───────────────────
        # When a plan already exists (stop > 0, t1 > 0), trust the DB
        # values — they are maintained by _adjust_trailing_stops (trailing
        # ratchet) and run_monday_open (fresh open prices).
        #
        # Computing fresh EMA exits every minute and comparing against the
        # live OCO caused constant 0.5%-drift cancel-replace cycles that
        # blocked the monitor for 4-10s per position per cycle.
        #
        # Fresh exits are only derived here when there is genuinely NO plan
        # (e.g. position entered manually, or outside the screener flow).
        # t1_taken positions are always skipped — BE stop must be preserved.
        if not t1_taken and (stop <= 0 or t1 <= 0):
            entry_price   = float(getattr(pos, "avg_entry_price", 0) or 0)
            current_price = float(getattr(pos, "current_price", 0) or entry_price)
            if entry_price <= 0 and current_price <= 0:
                logger.warning("Exit guard: %s no anchor price — cannot derive plan [%s]", sym, mode)
                continue
            stop, t1 = _derive_fresh_plan(
                db, sym, mode, entry_price, current_price, user_id=user_id
            )
            t2 = 0.0   # no T2 on freshly derived plans
            if stop <= 0 or t1 <= 0:
                logger.warning(
                    "Exit guard: %s plan derivation returned invalid prices "
                    "(stop=%.2f t1=%.2f) — skipping [%s]", sym, stop, t1, mode,
                )
                continue

        # ── Resolve the OCO target price ─────────────────────────────────
        # Key design decision: the OCO bracket targets T2 (not T1) so that
        # the software-managed T1 partial exit can fire via the monitor cycle
        # before Alpaca's OCO does. T1 is detected by price comparison each
        # cycle; the OCO only fires if price blows through T1 all the way to
        # T2 without the monitor catching it — in that case a full exit at T2
        # is still a profitable outcome.
        #
        # After t1 is taken, the remaining half runs with stop=BE and the
        # OCO target reverts to t1 (now repurposed as "remaining target").
        if t1_taken:
            # t1 column was overwritten with t2 value after the partial exit
            oco_target = t1
        elif t2 > 0:
            oco_target = t2
        else:
            # No T2 set — fall back to T1 (old behaviour, no partial exit)
            oco_target = t1

        # ── Inspect current coverage ─────────────────────────────────────
        # Alpaca's status=open only returns the "new" (working) leg of an OCO.
        # The "held" stop sibling is invisible to open-order queries.
        #
        # Coverage model:
        #   OCO path  — find a sell limit order with order_class=oco at the
        #               correct qty and target price. Alpaca's OCO contract
        #               guarantees the stop sibling exists; verify its price
        #               from .legs if populated, or from plan_stop (DB truth).
        #   Two-order path — find explicit stop + limit sell legs (legacy).
        sell_legs = [o for o in sym_orders if 'sell' in str(getattr(o, 'side', '') or '').lower()]

        # Detect OCO parent: a limit-type sell with order_class containing 'oco'
        oco_parent = None
        for o in sell_legs:
            oclass = str(getattr(o, 'order_class', '') or '').lower()
            otype  = str(getattr(o, 'order_type', '') or getattr(o, 'type', '') or '').lower()
            if 'oco' in oclass or 'bracket' in oclass:
                if 'limit' in otype and 'stop' not in otype:
                    oco_parent = o
                    break

        if oco_parent is not None:
            # OCO path: one order visible (limit=new), stop guaranteed by Alpaca
            try:
                oco_qty = int(float(getattr(oco_parent, 'qty', 0) or 0))
            except (TypeError, ValueError):
                oco_qty = 0

            current_target = _get_current_target_price(sym_orders)

            # Try to read stop price from .legs (populated in some responses)
            current_stop = None
            for leg in (getattr(oco_parent, 'legs', None) or []):
                leg_type = str(getattr(leg, 'type', '') or '').lower()
                if 'stop' in leg_type:
                    sp = getattr(leg, 'stop_price', None)
                    if sp is not None:
                        current_stop = float(sp)
                        break
            # If legs not populated, trust DB stop as ground truth (we set it)
            if current_stop is None:
                current_stop = stop

            target_ok = (current_target is not None
                         and not _price_changed(current_target, oco_target))
            stop_ok   = not _price_changed(current_stop, stop)
            qty_ok    = (oco_qty == qty)

            if qty_ok and stop_ok and target_ok:
                logger.debug("Exit guard: %s OCO coverage correct — no action.", sym)
                continue

        else:
            # Legacy two-order path (explicit stop + limit sell orders)
            has_stop_leg = has_limit_leg = False
            stop_qty = limit_qty = 0
            for o in sell_legs:
                otype = str(getattr(o, 'order_type', '') or getattr(o, 'type', '') or '').lower()
                try:
                    o_qty = int(float(getattr(o, 'qty', 0) or 0))
                except (TypeError, ValueError):
                    o_qty = 0
                if 'stop' in otype:
                    has_stop_leg = True
                    stop_qty = o_qty
                elif 'limit' in otype:
                    has_limit_leg = True
                    limit_qty = o_qty

            current_stop   = _get_current_stop_price(sym_orders)
            current_target = _get_current_target_price(sym_orders)
            prices_match = (
                current_stop is not None
                and current_target is not None
                and not _price_changed(current_stop, stop)
                and not _price_changed(current_target, oco_target)
            )
            if (has_stop_leg and has_limit_leg
                    and stop_qty == qty and limit_qty == qty
                    and len(sell_legs) == 2 and prices_match):
                logger.debug("Exit guard: %s two-order coverage correct — no action.", sym)
                continue

        # ── Serialized replace: cancel all sells, wait, place fresh ──────
        # Never stack a new OCO on top of existing sells. Alpaca paper can
        # silently drop the stop sibling if reservable qty is already
        # pledged by a prior order — leaving the position with a target
        # leg and no stop. Cancelling first eliminates the race.
        #
        # Alpaca paper is slow to cascade-cancel OCO siblings (held leg
        # can linger >10s after parent is cancelled). Strategy:
        #   1. Cancel all sell orders (4s wait).
        #   2. If still not clear: second cancel sweep + 1s grace, then
        #      proceed anyway.
        #   3. If placement fails with "insufficient qty" (40310000),
        #      the OLD OCO is still active → position is protected.
        #      Log and retry on the next cycle — not a hard failure.
        cancelled = alp.cancel_symbol_exit_orders(sym, mode)
        if cancelled:
            cleared = alp.wait_for_orders_cancelled(
                sym, mode, timeout=4.0, poll_interval=0.5, order_ids=cancelled
            )
            if not cleared:
                # Second sweep: cancel again to catch any newly-visible siblings
                alp.cancel_symbol_exit_orders(sym, mode)
                import time as _t; _t.sleep(1.0)
                logger.warning(
                    "Exit guard: %s cancel still settling — proceeding optimistically [%s]",
                    sym, mode,
                )

        try:
            parent = alp.place_oca_exit(sym, qty, stop, oco_target, mode)
            logger.info(
                "Exit guard: placed OCO for %s qty=%d stop=$%.2f target=$%.2f [%s] (T1=$%.2f)",
                sym, qty, stop, oco_target, mode, t1,
            )

            # Verify the parent order's legs directly — Alpaca's OCO holds
            # one sibling, so re-querying status=open can hide the held leg.
            has_l, has_s = alp.verify_oca_parent(parent)
            if not has_s:
                # Stop is the parent — should never be missing. If it is,
                # the position is genuinely naked.
                logger.error(
                    "Exit guard: %s OCO parent has NO stop leg (limit=%s) [%s]",
                    sym, has_l, mode,
                )
                try:
                    tg.alert_system_error_sync(
                        f"NAKED POSITION [{mode}] {sym} — OCO submitted without stop",
                        RuntimeError(f"limit={has_l} stop={has_s}"),
                    )
                except Exception:
                    pass
            elif not has_l:
                # Stop landed but Alpaca dropped the take-profit sibling.
                # Position is protected on the downside; just warn.
                logger.warning(
                    "Exit guard: %s OCO has stop but no take-profit leg [%s]",
                    sym, mode,
                )
        except Exception as exc:
            exc_str = str(exc)
            # Pledged-qty race (40310000): the cancelled OCO's held leg hasn't
            # fully released its qty reservation yet.
            #
            # IMPORTANT: the cancel DID go through — the old OCO is GONE.
            # "position protected, retrying next cycle" is WRONG here because
            # we could be naked for up to 30 minutes. Instead: wait 3s for the
            # reservation to clear, then retry placement once. If the retry also
            # fails, place a stop-market as emergency protection.
            if "40310000" in exc_str or "insufficient qty" in exc_str.lower():
                logger.warning(
                    "Exit guard: %s OCO blocked (40310000 qty race) — waiting 3s and retrying [%s]",
                    sym, mode,
                )
                import time as _t; _t.sleep(3.0)
                try:
                    parent = alp.place_oca_exit(sym, qty, stop, oco_target, mode)
                    logger.info(
                        "Exit guard: %s OCO retry succeeded — qty=%d stop=$%.2f target=$%.2f [%s]",
                        sym, qty, stop, oco_target, mode,
                    )
                except Exception as retry_exc:
                    # Retry also failed — place stop-market as emergency protection
                    logger.error(
                        "Exit guard: %s OCO retry failed (%s) — placing emergency stop-market [%s]",
                        sym, retry_exc, mode,
                    )
                    try:
                        alp.place_stop_loss_sell(sym, qty, stop, mode)
                        logger.info(
                            "Exit guard: %s emergency stop-market placed at $%.2f [%s]",
                            sym, stop, mode,
                        )
                        try:
                            tg.alert_system_error_sync(
                                f"⚠️ OCO RETRY FAILED [{mode}] {sym}\n"
                                f"OCO qty race persisted after retry. Emergency stop-market "
                                f"placed at ${stop:.2f}. OCO will be set on next cycle.",
                                retry_exc,
                            )
                        except Exception:
                            pass
                    except Exception as stop_exc:
                        logger.error(
                            "Exit guard: %s emergency stop-market also failed: %s [%s]",
                            sym, stop_exc, mode,
                        )
                        try:
                            tg.alert_system_error_sync(
                                f"🚨 NAKED POSITION [{mode}] {sym}\n"
                                f"OCO + emergency stop both failed after 40310000 race.\n"
                                f"Position is UNPROTECTED. Intervene manually.",
                                stop_exc,
                            )
                        except Exception:
                            pass
                continue
            # PDT (pattern day trading) protection blocks OCO orders — and even
            # plain stop orders — placed the same day as a buy in margin accounts
            # under $25k. Nothing can be placed today; try stop-market as last
            # resort, and if that also fails, park the symbol until tomorrow.
            elif "40310100" in exc_str or "pattern day trad" in exc_str.lower():
                logger.warning(
                    "Exit guard: OCO for %s blocked by PDT — trying stop-market fallback [%s]",
                    sym, mode,
                )
                stop_placed = False
                try:
                    alp.place_stop_loss_sell(sym, qty, stop, mode)
                    stop_placed = True
                    logger.info(
                        "Exit guard: PDT fallback — stop-market sell placed for %s "
                        "qty=%d stop=$%.2f [%s]",
                        sym, qty, stop, mode,
                    )
                    try:
                        tg.alert_system_error_sync(
                            f"⚠️ PDT PROTECTION [{mode}] {sym}\n"
                            f"OCO blocked by Alpaca's pattern-day-trading rule.\n"
                            f"✅ Stop-market placed at ${stop:.2f} (no take-profit today).\n"
                            f"OCO will be set automatically at tomorrow's open.\n"
                            f"Tip: Deposit to reach $25k+ equity to lift PDT restrictions.",
                            exc,
                        )
                    except Exception:
                        pass
                except Exception as stop_exc:
                    if "40310100" in str(stop_exc) or "pattern day trad" in str(stop_exc).lower():
                        # Alpaca is blocking even plain stop orders today.
                        # Park this symbol; the monitor will place exits tomorrow.
                        _pdt_blocked.add((sym, mode))
                        logger.error(
                            "Exit guard: %s — ALL exits blocked by PDT today; "
                            "parked until next open [%s]",
                            sym, mode,
                        )
                        try:
                            tg.alert_system_error_sync(
                                f"⚠️ PDT PROTECTION [{mode}] {sym}\n"
                                f"Alpaca is blocking ALL sell orders today (same-day buy).\n"
                                f"🔒 Position unprotected until tomorrow's market open.\n"
                                f"Bot will auto-place OCO at next open. Monitor manually.\n"
                                f"Tip: Deposit to reach $25k+ equity to lift PDT restrictions.",
                                stop_exc,
                            )
                        except Exception:
                            pass
                    else:
                        logger.error(
                            "Exit guard: PDT stop-market fallback failed for %s: %s [%s]",
                            sym, stop_exc, mode,
                        )
                        try:
                            tg.alert_system_error_sync(
                                f"NAKED POSITION [{mode}] {sym} — OCO + stop-market both failed",
                                stop_exc,
                            )
                        except Exception:
                            pass
            else:
                logger.error("Exit guard: OCO placement failed for %s: %s", sym, exc)
                try:
                    tg.alert_system_error_sync(
                        f"NAKED POSITION [{mode}] {sym} — OCO placement failed",
                        exc,
                    )
                except Exception:
                    pass
        continue


# ── Tape context helper ───────────────────────────────────────────────────────

def _get_tape_context(db: Session, user_id: int | None) -> dict | None:
    """
    Return today's cached tape verdict (no LLM call).
    Returns None if no cache row exists yet for today.
    """
    import json as _json
    from datetime import date
    from sqlalchemy import text as _text

    if user_id is None:
        return None
    try:
        today = date.today().isoformat()
        row = db.execute(
            _text("""
                SELECT signals, verdict, summary, key_risk
                FROM market_tape_cache
                WHERE user_id = :uid AND cache_date = :d
            """),
            {"uid": user_id, "d": today},
        ).fetchone()
        if not row:
            return None
        signals = _json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        return {
            "condition": row[1],
            "summary":   row[2],
            "key_risk":  row[3],
            "signals":   signals,
        }
    except Exception as exc:
        logger.debug("_get_tape_context failed (non-fatal): %s", exc)
        return None


# ── Pre-trade gate ────────────────────────────────────────────────────────────

def _gate(
    db: Session,
    symbol: str,
    qty: float,
    entry: float,
    stop: float,
    target: float,
    trigger: str,
    mode: str,
    user_id: int = None,
) -> bool:
    """Pre-trade AI gate. Returns True if order should proceed. Fails closed."""
    # ── Manual kill-switch: block_new_entries ────────────────────────────
    try:
        from .database import get_user_setting
        _uid_bne = user_id
        if _uid_bne is None:
            try:
                _r = db.execute(text("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")).fetchone()
                _uid_bne = _r[0] if _r else None
            except Exception:
                pass
        if (get_user_setting(db, "block_new_entries", "false", user_id=_uid_bne) or "false").lower() == "true":
            logger.warning(
                "Pre-trade gate: block_new_entries=true — hard blocking %s [%s]",
                symbol, mode,
            )
            return False
    except Exception as _bne_exc:
        logger.warning("Pre-trade gate: block_new_entries check failed (%s) — proceeding", _bne_exc)

    # ── Hard tape block: unfavorable market = no new entries ─────────────
    try:
        if user_id is None:
            try:
                row = db.execute(text("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")).fetchone()
                if row:
                    _uid_for_tape = row[0]
                else:
                    _uid_for_tape = None
            except Exception:
                _uid_for_tape = None
        else:
            _uid_for_tape = user_id
        tape = _get_tape_context(db, _uid_for_tape)
        if tape and tape.get("condition", "").lower() == "unfavorable":
            logger.warning(
                "Pre-trade gate: tape=UNFAVORABLE — hard blocking %s [%s] "
                "(no new entries on crash days)", symbol, mode,
            )
            return False
    except Exception as _tape_exc:
        logger.debug("Pre-trade gate: tape hard-block check failed (%s) — proceeding", _tape_exc)

    try:
        from .claude_analyst import pre_trade_analysis, log_pre_trade, get_stored_weekly_plan_analysis
        # Internal callers (Monday open, slot refill, post-close, TV) don't
        # carry a user context. Fall back to the admin uid so the gate log
        # row is scoped correctly and the AI Gate tab can display it.
        if user_id is None:
            try:
                row = db.execute(text("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")).fetchone()
                if row:
                    user_id = row[0]
            except Exception:
                pass
        stored       = get_stored_weekly_plan_analysis(db, symbol, mode)
        acct         = alp.get_account_for_user(db, user_id, mode)
        portfolio    = float(acct.portfolio_value)
        cash         = float(acct.cash)
        buying_power = float(acct.buying_power)

        # Fetch today's tape from cache (no extra LLM call; cache may be empty)
        tape_context = _get_tape_context(db, user_id)

        result = pre_trade_analysis(
            db=db, symbol=symbol, side="BUY", qty=qty,
            entry_price=entry, stop_price=stop, target_price=target,
            trigger=trigger, portfolio_value=portfolio,
            cash=cash, buying_power=buying_power, mode=mode,
            user_id=user_id, tape_context=tape_context,
            stored_analysis=stored,
        )
        log_pre_trade(
            db, symbol, trigger,
            result["verdict"], result["reason"], result["analysis"], mode,
            user_id=user_id,
        )

        if not result["proceed"]:
            logger.warning("Pre-trade gate BLOCKED %s [%s]: %s", symbol, trigger, result["reason"])
            return False

        # Treat WARN as a block by default. WARN means borderline R:R, sizing
        # near limits, weekly-plan WAIT carryover, or cash-at-floor — exactly
        # the cases where slippage or repricing pushes the trade out of spec.
        # Override with setting `block_on_warn=false` if a more permissive
        # posture is wanted.
        block_on_warn = (get_setting(db, "block_on_warn", "true") or "true").lower() == "true"
        if block_on_warn and result["verdict"] == "WARN":
            logger.warning(
                "Pre-trade gate BLOCKED %s [%s] on WARN: %s",
                symbol, trigger, result["reason"],
            )
            return False

        if result["warnings"]:
            logger.warning("Pre-trade gate WARNED %s [%s]: %s", symbol, trigger, ", ".join(result["warnings"]))
        logger.info("Pre-trade gate PASSED %s [%s]: %s", symbol, trigger, result["reason"])
        return True

    except Exception as exc:
        logger.error("Pre-trade gate error for %s: %s — BLOCKING (fail-closed).", symbol, exc)
        try:
            tg.alert_system_error_sync(f"Pre-trade gate {symbol} [{mode}]", exc, level="URGENT")
        except Exception:
            pass
        return False


# ── Partial-fill reconciliation ───────────────────────────────────────────────

def _reconcile_partial_fills(db: Session, positions, mode: str) -> None:
    """Sync stale planned qty against actual filled qty.

    `weekly_plan.position_size` is set at screener time and `trade_log.qty`
    captures the requested size. Neither is updated when a bracket entry
    partial-fills, so reporting drifts from reality. Bracket exit legs
    auto-resize to filled qty in Alpaca, so this is a reporting-fix, not
    a risk-fix — but stale numbers also leak into the AI gate's portfolio
    context and the cash-buffer calculation on the next monitor cycle.
    """
    from sqlalchemy import text as _text

    for pos in positions:
        sym       = pos.symbol
        actual_qty = float(getattr(pos, "qty", 0) or 0)
        if actual_qty <= 0:
            continue

        try:
            # Latest weekly_plan row for this symbol+mode
            wp = db.execute(
                _text("""
                    SELECT id, position_size, entry_price
                    FROM weekly_plan
                    WHERE symbol = :sym AND mode = :mode
                      AND week_start = (
                          SELECT MAX(week_start) FROM weekly_plan WHERE mode = :mode
                      )
                """),
                {"sym": sym, "mode": mode},
            ).fetchone()
            if not wp or wp[1] is None:
                continue

            planned = int(wp[1])
            if planned <= 0 or planned == int(actual_qty):
                continue

            # Decide whether this is a "fresh" partial fill worth alerting on.
            # If the most-recent BUY trade_log entry is older than 2 hours, the
            # drift is historical state we're cleaning up — sync silently.
            tl = db.execute(
                _text("""
                    SELECT id, EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_sec
                    FROM trade_log
                    WHERE symbol = :sym AND mode = :mode AND action = 'BUY'
                    ORDER BY created_at DESC
                    LIMIT 1
                """),
                {"sym": sym, "mode": mode},
            ).fetchone()
            is_fresh = bool(tl and tl[1] is not None and float(tl[1]) < 7200)

            db.execute(
                _text("UPDATE weekly_plan SET position_size = :q WHERE id = :id"),
                {"q": int(actual_qty), "id": wp[0]},
            )
            if tl:
                db.execute(
                    _text("UPDATE trade_log SET qty = :q WHERE id = :id"),
                    {"q": actual_qty, "id": tl[0]},
                )
            db.commit()

            diff_pct = abs(actual_qty - planned) / planned * 100
            logger.info(
                "Reconciled %s [%s]: planned=%d actual=%.0f (%.1f%% diff)",
                sym, mode, planned, actual_qty, diff_pct,
            )
            # Alert on material partial fill — but only when fresh.
            # Older drift is historical state being cleaned up (e.g. first
            # monitor cycle after this reconciler shipped) — sync silently.
            if diff_pct >= 10 and is_fresh:
                try:
                    tg.alert_system_error_sync(
                        f"Partial fill reconciled [{mode}] {sym}",
                        f"planned={planned}, filled={int(actual_qty)} ({diff_pct:.0f}% short)",
                        level="INFO",
                    )
                except Exception:
                    pass

        except Exception as exc:
            db.rollback()
            logger.warning("Partial-fill reconcile failed for %s: %s", sym, exc)


# ── Market-open position sync (Apex) ─────────────────────────────────────────

def run_position_sync(db: Session, mode: str, user_id: int | None = None) -> dict:
    """
    Lightweight stop-management cycle that runs at market open (9:31 ET) every
    trading day — independently of the configurable monitor interval.

    Runs ONLY the stop-management steps (T1 partial exit → trailing stops →
    exit guard → time stops). Does NOT do signal evaluation, slot fills, or
    post-close buys — those remain in the full monitor cycle to avoid
    interfering with Monday's run_monday_open at 9:35 ET.

    The purpose is to ensure existing positions have up-to-date Apex exits
    from the very first print, not up to 30 minutes later when the regular
    monitor interval first fires.
    """
    try:
        clock = alp.get_clock(mode)
        if not clock.is_open:
            logger.info("Position sync [%s]: market not open — skipping.", mode)
            return {"status": "skipped", "reason": "market_closed"}
    except Exception as exc:
        logger.warning("Position sync [%s]: clock check failed (%s) — skipping.", mode, exc)
        return {"status": "skipped", "reason": str(exc)}

    try:
        positions = alp.get_positions_for_user(db, user_id, mode)
    except Exception as exc:
        logger.error("Position sync [%s]: cannot fetch positions — %s", mode, exc)
        return {"status": "error", "error": str(exc)}

    if not positions:
        logger.info("Position sync [%s]: no open positions — nothing to sync.", mode)
        return {"status": "ok", "positions": 0}

    logger.info(
        "Position sync [%s]: syncing stop-management for %d open position(s) at market open.",
        mode, len(positions),
    )

    try:
        _reconcile_partial_fills(db, positions, mode)
    except Exception as exc:
        logger.warning("Position sync [%s]: reconcile failed — %s", mode, exc)

    try:
        open_orders = alp.get_open_orders_by_symbol_for_user(db, user_id, mode)

        _check_t1_partial_exit(db, positions, open_orders, mode)

        # Re-fetch after potential partial sell
        positions   = alp.get_positions_for_user(db, user_id, mode)
        open_orders = alp.get_open_orders_by_symbol_for_user(db, user_id, mode)

        _adjust_trailing_stops(db, positions, open_orders, mode)

        open_orders = alp.get_open_orders_by_symbol_for_user(db, user_id, mode)

        _ensure_exit_orders(db, positions, open_orders, mode, user_id=user_id)

    except Exception as exc:
        logger.error("Position sync [%s]: stop-management failed — %s", mode, exc)
        try:
            from . import telegram_alerts as tg
            tg.alert_system_error_sync(f"Market-open position sync [{mode}]", exc)
        except Exception:
            pass
        return {"status": "error", "error": str(exc)}

    try:
        _check_time_stops(db, positions, mode)
    except Exception as exc:
        logger.error("Position sync [%s]: time-stop check failed — %s", mode, exc)

    logger.info("Position sync [%s]: done.", mode)
    return {"status": "ok", "positions": len(positions)}


# ── Main monitor ──────────────────────────────────────────────────────────────

async def run_monitor(db: Session, user_id: int | None = None, mode: str | None = None):
    """
    mode — if passed explicitly (by scheduler for parallel runs) the monitor
    operates in that exact mode and does not read trading_mode from settings.
    If None, falls back to reading trading_mode from user/global settings.
    """
    if user_id:
        from .database import get_all_user_settings as _gaus
        _s = _gaus(db, user_id)
        if mode is None:
            mode = _s.get("trading_mode", "paper")
        # Per-mode auto_execute flags — live mode is FAIL-SAFE (defaults off):
        #   paper_auto_execute: default "true"
        #   live_auto_execute:  default "false" — must be EXPLICITLY set to "true"
        # Live mode never inherits a "true" default even if the key is absent.
        if mode == "live":
            auto_execute = _s.get("live_auto_execute", "false").lower() == "true"
        else:
            auto_execute = _s.get("paper_auto_execute", _s.get("auto_execute", "true")).lower() == "true"
        risk_pct         = float(_s.get("risk_pct", "2.0") or "2.0")
        stop_pct         = float(_s.get("stop_loss_pct", "8.0") or "8.0")
        interval_minutes = int(_s.get("monitor_interval_minutes", "30") or "30")
        try:
            alp.configure_from_db_settings(_s, mode, is_admin=True)
        except ValueError as _creds_err:
            logger.warning("run_monitor [%s]: credential error — %s", mode, _creds_err)
            return {"status": "error", "error": str(_creds_err)}
    else:
        if mode is None:
            mode = get_setting(db, "trading_mode", "paper")
        if mode == "live":
            auto_execute = get_setting(db, "live_auto_execute",  "false").lower() == "true"
        else:
            auto_execute = get_setting(db, "paper_auto_execute", get_setting(db, "auto_execute", "true")).lower() == "true"
        risk_pct         = float(get_setting(db, "risk_pct", "2.0"))
        stop_pct         = float(get_setting(db, "stop_loss_pct", "8.0"))
        interval_minutes = int(get_setting(db, "monitor_interval_minutes", "30") or "30")

    if mode == "live" and auto_execute:
        logger.warning(
            "LIVE AUTO-EXECUTE IS ENABLED — monitor will place real-money orders [user=%s]",
            user_id,
        )

    try:
        clock       = alp.get_clock(mode)
        market_open = clock.is_open

        # Clear per-session alert trackers at each market open so stale flags
        # don't suppress alerts that should fire again the next trading day.
        if market_open and (_pdt_blocked or _size_mismatch_alerted):
            if _pdt_blocked:
                logger.info(
                    "run_monitor [%s]: market open — clearing %d PDT-blocked symbol(s): %s",
                    mode, len(_pdt_blocked),
                    [s for s, m in _pdt_blocked if m == mode],
                )
                _pdt_blocked.clear()
            if _size_mismatch_alerted:
                logger.info(
                    "run_monitor [%s]: market open — clearing %d size-mismatch alert(s)",
                    mode, len(_size_mismatch_alerted),
                )
                _size_mismatch_alerted.clear()

        acct         = alp.get_account_for_user(db, user_id, mode)
        positions    = alp.get_positions_for_user(db, user_id, mode)
        portfolio    = float(acct.portfolio_value)
        cash         = float(acct.cash)
        buying_power = float(acct.buying_power)
        day_pnl      = float(acct.equity) - float(acct.last_equity)

        if market_open and positions:
            # Step 0: Reconcile partial fills before any stop/target sizing.
            # Exit orders are sized off pos.qty (already correct), but
            # weekly_plan.position_size and the BUY trade_log entry are
            # frozen at submit-time, so partial fills leave them stale.
            _reconcile_partial_fills(db, positions, mode)

            try:
                open_orders_by_symbol = alp.get_open_orders_by_symbol_for_user(db, user_id, mode)

                # Step 1a: T1 partial exit (Apex)
                # If price >= target1 and position hasn't been halved yet,
                # sell 50 %, move OCO stop to breakeven, target → T2.
                _check_t1_partial_exit(db, positions, open_orders_by_symbol, mode)

                # Re-fetch positions after potential partial sell
                positions             = alp.get_positions_for_user(db, user_id, mode)
                open_orders_by_symbol = alp.get_open_orders_by_symbol_for_user(db, user_id, mode)

                # Step 1b: Trailing stop adjustment (Apex)
                # Green positions get stops ratcheted: 1R→BE, 2R+→EMA20×0.99.
                # Red positions are untouched.
                _adjust_trailing_stops(db, positions, open_orders_by_symbol, mode)

                # Re-fetch after potential cancel+replace from trailing stops
                open_orders_by_symbol = alp.get_open_orders_by_symbol_for_user(db, user_id, mode)

                # Step 2: Exit guard
                # Ensures every position has an active OCO.
                # Replaces existing OCOs when plan stop/target has changed.
                _ensure_exit_orders(db, positions, open_orders_by_symbol, mode, user_id=user_id)

            except Exception as exc:
                logger.error("Stop management cycle failed: %s", exc)
                try:
                    tg.alert_system_error_sync(f"Monitor stop-mgmt cycle [{mode}]", exc)
                except Exception:
                    pass

            # Step 2b: Time stop (Apex) — runs outside the inner try so a
            # trailing-stop failure doesn't suppress dead-money exits.
            try:
                _check_time_stops(db, positions, mode)
            except Exception as exc:
                logger.error("Time stop cycle failed: %s", exc)

        # Step 3: Signal evaluation
        stage2_lost   = []
        new_breakouts = []
        results       = []

        for pos in positions:
            sym    = pos.symbol
            qty    = float(pos.qty)
            result = analyze(sym, db=db)
            signal = result.get("signal", "ERROR")

            _log_signal(db, sym, signal, result.get("score", 0), result.get("price"), mode)

            if signal == "NO_SETUP":
                stage2_lost.append(sym)
                if auto_execute and market_open:
                    try:
                        alp.close_position(sym, mode)
                        _log_trade(db, sym, "SELL", qty, result.get("price") or 0, "STAGE2_LOST", mode)
                    except Exception as e:
                        results.append({"sym": sym, "action": "SELL_FAILED", "error": str(e)})
            elif signal == "BREAKOUT":
                new_breakouts.append(sym)

            results.append({"sym": sym, "signal": signal})

        # Daily-drawdown kill-switch — block all NEW entries (slot fill +
        # watchlist) when day_pnl breaches the configured threshold. Existing
        # exits/trailing stops still run; we only stop adding risk.
        # Setting `daily_drawdown_halt_pct` (default 5.0); 0 disables the check.
        # Halt latches for the ET calendar day in `drawdown_halt_<mode>_date`
        # so a mid-day restart doesn't forget that we already tripped.
        entries_halted = False
        try:
            halt_pct = float(get_setting(db, "daily_drawdown_halt_pct", "5.0") or "0")
        except (TypeError, ValueError):
            halt_pct = 5.0

        today_et   = datetime.now(_ET).strftime("%Y-%m-%d")
        halt_key   = f"drawdown_halt_{mode}_date"
        prior_halt = get_setting(db, halt_key, "")

        if halt_pct > 0 and prior_halt == today_et:
            entries_halted = True
            logger.warning(
                "DAILY DRAWDOWN HALT [%s]: previously tripped today (%s) — entries still blocked.",
                mode, today_et,
            )

        last_eq = float(getattr(acct, "last_equity", 0) or 0)
        if not entries_halted and halt_pct > 0 and last_eq > 0:
            day_pnl_pct = day_pnl / last_eq * 100
            if day_pnl_pct <= -halt_pct:
                entries_halted = True
                try:
                    set_setting(db, halt_key, today_et)
                    db.commit()
                except Exception:
                    db.rollback()
                logger.warning(
                    "DAILY DRAWDOWN HALT [%s]: day_pnl=%.2f%% breached -%.2f%% threshold — "
                    "blocking new entries (exits still run).",
                    mode, day_pnl_pct, halt_pct,
                )
                try:
                    tg.send_sync(
                        f"*DAILY DRAWDOWN HALT* [{mode.upper()}]\n\n"
                        f"Day P&L: `{day_pnl_pct:.2f}%` (threshold `-{halt_pct:.2f}%`)\n"
                        f"New entries blocked for the rest of the session. Exits still run.",
                        level="URGENT",
                    )
                except Exception:
                    pass

        # Step 4: Weekly-plan slot fill — buys PENDING picks when capacity exists.
        # This is the primary entry mechanism (screener picks → weekly_plan → orders).
        if auto_execute and market_open and not entries_halted:
            try:
                from .position_manager import fill_open_slots
                fill_open_slots(
                    db=db, mode=mode, portfolio=portfolio,
                    cash=cash, buying_power=buying_power,
                    risk_pct=risk_pct, stop_pct=stop_pct,
                    positions=positions, user_id=user_id,
                )
                # Re-fetch positions so watchlist step has fresh state
                positions = alp.get_positions_for_user(db, user_id, mode)
            except Exception as exc:
                logger.error("fill_open_slots failed: %s", exc)

        # Step 5: Watchlist breakout entries (manual watchlist, not screener picks)
        held_symbols = {p.symbol for p in positions}
        watchlist    = _get_watchlist(db, user_id)
        max_pos      = _effective_max_positions(db, mode)

        if auto_execute and market_open and not entries_halted and len(positions) < max_pos:
            for sym in watchlist:
                if sym in held_symbols:
                    continue
                result = analyze(sym, db=db)
                signal = result.get("signal")
                _log_signal(db, sym, signal, result.get("score", 0), result.get("price"), mode)

                if signal == "BREAKOUT" and result.get("price"):
                    price               = result["price"]
                    stop, _t1, _t2 = _get_weekly_plan_exits(db, sym, mode)
                    qty                 = _size_position(portfolio, price, risk_pct, stop_pct, stop_price=stop)

                    # Apply min_cash_pct buffer — risk-based sizing alone doesn't
                    # respect the cash floor, so a watchlist breakout could push
                    # cash below the configured minimum (mirrors fill_open_slots).
                    try:
                        from .position_manager import _settled_funds_available
                        min_cash_pct = float(get_setting(db, "min_cash_pct", "10.0") or "10.0")
                        avail        = _settled_funds_available(acct, portfolio, min_cash_pct, 0.0)
                        if avail > 0 and price > 0:
                            qty = min(qty, int(avail / price))
                        else:
                            qty = 0
                    except Exception as _exc:
                        logger.warning("Watchlist cash-buffer check failed for %s: %s", sym, _exc)

                    if qty >= 1:
                        if not _gate(db, sym, qty, price, stop, target, "BREAKOUT", mode, user_id=user_id):
                            results.append({"sym": sym, "action": "BLOCKED_BY_AI"})
                            continue
                        order_placed = False
                        try:
                            from .position_manager import _place_entry as _pm_place_entry
                            order_desc = _pm_place_entry(db, sym, qty, price, stop, target, "BREAKOUT", mode, "minervini")
                            order_placed = True
                            logger.info("Watchlist buy %s qty=%.0f — %s [%s]", sym, qty, order_desc, mode)
                            _log_trade(db, sym, "BUY", qty, price, "BREAKOUT", mode)
                            new_breakouts.append(sym)
                            held_symbols.add(sym)
                            try:
                                from .claude_analyst import get_latest_pre_trade
                                v, r = get_latest_pre_trade(db, sym, mode, user_id=user_id)
                                tg.alert_trade_sync(
                                    "BUY", sym, qty, price, "BREAKOUT", mode,
                                    ai_verdict=v, ai_reason=r,
                                )
                            except Exception:
                                pass
                        except Exception as e:
                            if order_placed:
                                logger.error(
                                    "Watchlist buy %s [%s]: ORDER PLACED but trade_log failed: %s",
                                    sym, mode, e,
                                )
                                try:
                                    tg.alert_system_error_sync(
                                        f"UNTRACKED POSITION [{mode}] {sym} qty={qty} — watchlist order placed, log failed",
                                        e, level="URGENT",
                                    )
                                except Exception:
                                    pass
                            results.append({"sym": sym, "action": "BUY_FAILED", "error": str(e)})

        if stage2_lost:
            asyncio.create_task(tg.alert_stage2_lost(stage2_lost, mode))
        if new_breakouts:
            asyncio.create_task(tg.alert_breakout(new_breakouts, mode))

        asyncio.create_task(tg.alert_monitor_summary(portfolio, day_pnl, len(positions), mode, interval_minutes))

        return {
            "status":        "ok",
            "mode":          mode,
            "market_open":   market_open,
            "portfolio":     portfolio,
            "day_pnl":       day_pnl,
            "stage2_lost":   stage2_lost,
            "new_breakouts": new_breakouts,
            "results":       results,
        }

    except Exception as exc:
        logger.exception("Monitor [%s] top-level failure", mode)
        try:
            tg.alert_system_error_sync(f"Monitor crashed [{mode}]", exc)
        except Exception:
            pass
        return {"status": "error", "error": str(exc)}


def _get_watchlist(db: Session, user_id: int | None = None) -> list[str]:
    """Return the manual watchlist, preferring user_settings over global settings."""
    raw = None
    if user_id:
        from .database import get_all_user_settings as _gaus
        raw = _gaus(db, user_id).get("watchlist", "")
    if not raw:
        row = db.execute(text("SELECT value FROM settings WHERE key = 'watchlist'")).fetchone()
        raw = row[0] if row else ""
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _log_signal(db: Session, symbol: str, signal: str, score: int, price, mode: str):
    db.execute(
        text("INSERT INTO signal_log (symbol, signal, score, price, mode) VALUES (:s,:sig,:sc,:p,:m)"),
        {"s": symbol, "sig": signal, "sc": score, "p": price, "m": mode},
    )
    db.commit()


def _log_trade(db: Session, symbol: str, action: str, qty: float, price: float, trigger: str, mode: str):
    db.execute(
        text("INSERT INTO trade_log (symbol, action, qty, price, trigger, mode) VALUES (:s,:a,:q,:p,:t,:m)"),
        {"s": symbol, "a": action, "q": qty, "p": price, "t": trigger, "m": mode},
    )
    db.commit()