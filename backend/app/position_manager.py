"""
Position manager: Monday open fills + post-close slot refill with optional Claude analysis.
"""
import logging
from sqlalchemy import text
from sqlalchemy.orm import Session
from .database import get_setting, set_setting
from . import alpaca_client as alp

logger = logging.getLogger(__name__)


def _size_qty(portfolio: float, entry: float, stop: float, risk_pct: float, stop_pct: float) -> float:
    stop_dollar = (entry - stop) if stop > 0 else entry * (stop_pct / 100)
    if stop_dollar <= 0:
        return 0
    return (portfolio * risk_pct / 100) / stop_dollar


def run_monday_open(db: Session):
    """
    Called every Monday at 9:35 ET. Fills available position slots from the
    current week's PENDING picks for the active mode. Respects max_positions.
    """
    mode      = get_setting(db, "trading_mode", "paper")
    auto_exec = get_setting(db, "auto_execute", "true").lower() == "true"
    if not auto_exec:
        logger.info("Monday open: auto_execute off — skipping.")
        return

    max_pos = int(get_setting(db, "max_positions", "10"))

    try:
        positions = alp.get_positions(mode)
    except Exception as exc:
        logger.error("Monday open: could not fetch positions: %s", exc)
        return

    slots = max_pos - len(positions)
    if slots <= 0:
        logger.info("Monday open: portfolio full (%d/%d). No buys.", len(positions), max_pos)
        return

    rows = db.execute(
        text("""
            SELECT symbol, entry_price, stop_price, target1
            FROM weekly_plan
            WHERE week_start = (
                SELECT MAX(week_start) FROM weekly_plan WHERE mode = :mode
            )
              AND mode = :mode
              AND status = 'PENDING'
            ORDER BY rank ASC
            LIMIT :slots
        """),
        {"slots": slots, "mode": mode},
    ).fetchall()

    if not rows:
        logger.info("Monday open: no PENDING picks for mode=%s.", mode)
        return

    try:
        acct = alp.get_account(mode)
        portfolio = float(acct.portfolio_value)
    except Exception as exc:
        logger.error("Monday open: could not fetch account: %s", exc)
        return

    risk_pct = float(get_setting(db, "risk_pct", "2.0"))
    stop_pct = float(get_setting(db, "stop_loss_pct", "8.0"))
    held = {p.symbol for p in positions}

    for row in rows:
        sym    = row[0]
        entry  = float(row[1] or 0)
        stop   = float(row[2] or 0)
        target = float(row[3] or 0)

        if sym in held or entry <= 0:
            continue

        qty = _size_qty(portfolio, entry, stop, risk_pct, stop_pct)
        if qty < 1:
            logger.info("Monday open: skipping %s — position size < 1 share.", sym)
            continue

        try:
            if stop > 0 and target > 0:
                alp.place_bracket_buy(sym, qty, stop, target, mode)
                logger.info(
                    "Monday open: bracket buy %s qty=%.0f entry=~$%.2f stop=$%.2f target=$%.2f",
                    sym, qty, entry, stop, target,
                )
            else:
                alp.place_market_buy(sym, qty, mode)
                logger.info(
                    "Monday open: market buy %s qty=%.0f @ ~$%.2f (no bracket — missing stop/target)",
                    sym, qty, entry,
                )

            db.execute(
                text("""
                    UPDATE weekly_plan SET status = 'EXECUTED'
                    WHERE symbol = :sym
                      AND mode = :mode
                      AND week_start = (
                          SELECT MAX(week_start) FROM weekly_plan WHERE mode = :mode
                      )
                """),
                {"sym": sym, "mode": mode},
            )
            db.execute(
                text("""
                    INSERT INTO trade_log (symbol, action, qty, price, trigger, mode)
                    VALUES (:s, 'BUY', :q, :p, 'MONDAY_OPEN', :m)
                """),
                {"s": sym, "q": qty, "p": entry, "m": mode},
            )
            db.commit()
            held.add(sym)

        except Exception as exc:
            logger.error("Monday open: buy failed for %s: %s", sym, exc)


def check_post_close(db: Session):
    """
    Called in each monitor cycle. Compares current positions against the saved
    mode-scoped snapshot to detect newly closed positions, then:
      1. Runs Claude analysis (if API key configured)
      2. Auto-executes the next PENDING pick into the freed slot (if auto_execute)
    """
    mode = get_setting(db, "trading_mode", "paper")

    try:
        current = {p.symbol for p in alp.get_positions(mode)}
    except Exception as exc:
        logger.error("check_post_close: cannot fetch positions: %s", exc)
        return

    # Mode-scoped snapshot key — paper and live never share state
    snapshot_key = f"positions_snapshot_{mode}"
    snap_row = db.execute(
        text("SELECT value FROM settings WHERE key = :k"),
        {"k": snapshot_key},
    ).fetchone()
    prev = set(snap_row[0].split(",")) if snap_row and snap_row[0] else set()

    set_setting(db, snapshot_key, ",".join(sorted(current)))
    db.commit()

    closed = prev - current
    if not closed:
        return

    logger.info("[%s] Detected closed positions: %s", mode, closed)
    api_key   = get_setting(db, "claude_api_key", "")
    auto_exec = get_setting(db, "auto_execute", "true").lower() == "true"
    max_pos   = int(get_setting(db, "max_positions", "10"))

    for sym in closed:
        if api_key:
            _run_claude_analysis(db, sym, mode)

        if auto_exec and len(current) < max_pos:
            _execute_next_pick(db, mode, current)
            try:
                current = {p.symbol for p in alp.get_positions(mode)}
            except Exception:
                pass


def _run_claude_analysis(db: Session, closed_sym: str, mode: str):
    try:
        from .claude_analyst import analyze_picks, log_analysis

        picks_rows = db.execute(
            text("""
                SELECT symbol, score, signal, entry_price, stop_price, target1, status, rationale
                FROM weekly_plan
                WHERE week_start = (
                    SELECT MAX(week_start) FROM weekly_plan WHERE mode = :mode
                )
                  AND mode = :mode
                ORDER BY rank ASC
            """),
            {"mode": mode},
        ).fetchall()

        picks = [dict(r._mapping) for r in picks_rows]

        entry_row = db.execute(
            text("""
                SELECT price FROM trade_log
                WHERE symbol = :s AND action = 'BUY' AND mode = :mode
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"s": closed_sym, "mode": mode},
        ).fetchone()

        closed_ctx = {
            "symbol":      closed_sym,
            "entry_price": float(entry_row[0]) if entry_row else None,
            "reason":      "position closed (stop hit or target reached)",
        }

        analysis = analyze_picks(db, picks, closed_position=closed_ctx)
        log_analysis(db, "post_close", closed_sym, analysis, mode)
        logger.info("Claude analysis saved for post-close of %s [%s].", closed_sym, mode)

    except Exception as exc:
        logger.warning("Claude analysis failed for %s: %s", closed_sym, exc)


def _execute_next_pick(db: Session, mode: str, held: set):
    row = db.execute(
        text("""
            SELECT symbol, entry_price, stop_price, target1
            FROM weekly_plan
            WHERE week_start = (
                SELECT MAX(week_start) FROM weekly_plan WHERE mode = :mode
            )
              AND mode = :mode
              AND status = 'PENDING'
            ORDER BY rank ASC
            LIMIT 1
        """),
        {"mode": mode},
    ).fetchone()

    if not row:
        logger.info("Post-close: no PENDING picks left for mode=%s.", mode)
        return

    sym    = row[0]
    entry  = float(row[1] or 0)
    stop   = float(row[2] or 0)
    target = float(row[3] or 0)

    if sym in held or entry <= 0:
        return

    try:
        acct = alp.get_account(mode)
        portfolio = float(acct.portfolio_value)
    except Exception as exc:
        logger.error("Post-close: account fetch failed: %s", exc)
        return

    risk_pct = float(get_setting(db, "risk_pct", "2.0"))
    stop_pct = float(get_setting(db, "stop_loss_pct", "8.0"))
    qty = _size_qty(portfolio, entry, stop, risk_pct, stop_pct)
    if qty < 1:
        return

    try:
        if stop > 0 and target > 0:
            alp.place_bracket_buy(sym, qty, stop, target, mode)
            logger.info(
                "Post-close auto-buy: bracket %s qty=%.0f stop=$%.2f target=$%.2f [%s]",
                sym, qty, stop, target, mode,
            )
        else:
            alp.place_market_buy(sym, qty, mode)
            logger.info(
                "Post-close auto-buy: market %s qty=%.0f (no bracket — missing stop/target) [%s]",
                sym, qty, mode,
            )

        db.execute(
            text("""
                UPDATE weekly_plan SET status = 'EXECUTED'
                WHERE symbol = :sym
                  AND mode = :mode
                  AND week_start = (
                      SELECT MAX(week_start) FROM weekly_plan WHERE mode = :mode
                  )
            """),
            {"sym": sym, "mode": mode},
        )
        db.execute(
            text("""
                INSERT INTO trade_log (symbol, action, qty, price, trigger, mode)
                VALUES (:s, 'BUY', :q, :p, 'POST_CLOSE', :m)
            """),
            {"s": sym, "q": qty, "p": entry, "m": mode},
        )
        db.commit()

    except Exception as exc:
        logger.error("Post-close auto-buy failed for %s: %s", sym, exc)