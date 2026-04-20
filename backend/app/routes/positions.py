from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..database import get_db, get_setting
from .. import alpaca_client as alp
from ..sepa_analyzer import analyze
import time

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("")
def positions(db: Session = Depends(get_db)):
    mode = get_setting(db, "trading_mode", "paper")
    raw  = alp.get_positions(mode)

    if not raw:
        return []

    symbols = [p.symbol for p in raw]

    # DISTINCT ON returns the most recent weekly_plan row per symbol —
    # current week if it exists, otherwise falls back to the latest historical entry.
    plan_rows = db.execute(
        text("""
            SELECT DISTINCT ON (symbol)
                symbol, stop_price, target1, target2, week_start
            FROM weekly_plan
            WHERE symbol = ANY(:syms)
              AND mode = :mode
            ORDER BY symbol, week_start DESC
        """),
        {"syms": symbols, "mode": mode},
    ).fetchall()

    plan_map = {
        r[0]: {
            "stop_price": float(r[1]) if r[1] else None,
            "target1":    float(r[2]) if r[2] else None,
            "target2":    float(r[3]) if r[3] else None,
            "plan_week":  str(r[4])   if r[4] else None,
        }
        for r in plan_rows
    }

    out = []
    for p in raw:
        signal_data = analyze(p.symbol)
        plan        = plan_map.get(p.symbol, {})
        out.append({
            "symbol":          p.symbol,
            "qty":             float(p.qty),
            "entry_price":     float(p.avg_entry_price),
            "current_price":   float(p.current_price),
            "market_value":    float(p.market_value),
            "unrealized_pl":   float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc) * 100,
            "signal":          signal_data.get("signal"),
            "score":           signal_data.get("score"),
            "ema20":           signal_data.get("ema20"),
            "ema50":           signal_data.get("ema50"),
            "week52_high":     signal_data.get("week52_high"),
            "week52_low":      signal_data.get("week52_low"),
            "stop_price":      plan.get("stop_price"),
            "target1":         plan.get("target1"),
            "target2":         plan.get("target2"),
            "plan_week":       plan.get("plan_week"),
            "mode":            mode,
        })
    return out


@router.delete("/{symbol}")
def close(symbol: str, db: Session = Depends(get_db)):
    mode = get_setting(db, "trading_mode", "paper")
    alp.close_position(symbol.upper(), mode)
    return {"status": "closed", "symbol": symbol.upper()}


def _upsert_plan_exits(db: Session, symbol: str, stop: float, target: float, mode: str):
    """Shared helper — upsert stop/target into the current week's plan for a given mode."""
    existing = db.execute(
        text("""
            SELECT id FROM weekly_plan
            WHERE symbol = :sym
              AND mode = :mode
              AND week_start = (
                  SELECT MAX(week_start) FROM weekly_plan WHERE mode = :mode
              )
        """),
        {"sym": symbol, "mode": mode},
    ).fetchone()

    if existing:
        db.execute(
            text("""
                UPDATE weekly_plan
                SET stop_price = :stop, target1 = :target
                WHERE symbol = :sym
                  AND mode = :mode
                  AND week_start = (
                      SELECT MAX(week_start) FROM weekly_plan WHERE mode = :mode
                  )
            """),
            {"stop": stop, "target": target, "sym": symbol, "mode": mode},
        )
    else:
        db.execute(
            text("""
                INSERT INTO weekly_plan
                    (week_start, symbol, rank, score, entry_price, stop_price, target1, status, mode)
                VALUES (
                    (SELECT COALESCE(MAX(week_start), CURRENT_DATE)
                     FROM weekly_plan WHERE mode = :mode),
                    :sym, 99, 0, 0, :stop, :target, 'EXECUTED', :mode
                )
            """),
            {"sym": symbol, "stop": stop, "target": target, "mode": mode},
        )
    db.commit()


@router.patch("/{symbol}/exits")
def set_exit_levels(
    symbol: str,
    stop: float,
    target: float,
    db: Session = Depends(get_db),
):
    """
    Save stop_price and target1 to the current mode's weekly plan.
    The exit guard will detect the change on the next monitor cycle and
    replace the existing OCO with the updated levels.
    """
    symbol = symbol.upper()
    mode   = get_setting(db, "trading_mode", "paper")
    _upsert_plan_exits(db, symbol, stop, target, mode)
    return {"status": "ok", "symbol": symbol, "stop": stop, "target": target, "mode": mode}


@router.post("/{symbol}/place-exits")
def place_exits_now(
    symbol: str,
    stop: float,
    target: float,
    db: Session = Depends(get_db),
):
    """
    Save levels to the current mode's weekly plan AND immediately replace
    any existing exit orders on Alpaca with a fresh OCO at the new levels.

    Cancels ALL open sell orders for the symbol first (including existing OCOs)
    so Alpaca doesn't reject the new order as an oversell.
    """
    symbol = symbol.upper()
    mode   = get_setting(db, "trading_mode", "paper")

    _upsert_plan_exits(db, symbol, stop, target, mode)

    # Confirm position still open in the correct account
    positions = alp.get_positions(mode)
    pos = next((p for p in positions if p.symbol == symbol), None)
    if not pos:
        return {"status": "error", "detail": f"No open {mode} position found for {symbol}"}

    qty = float(pos.qty)

    # Cancel ALL open sell orders — including any existing OCO.
    # Without this, Alpaca rejects the new OCO as an oversell.
    try:
        cancelled = alp.cancel_symbol_exit_orders(symbol, mode)
        if cancelled:
            time.sleep(0.6)   # let Alpaca fully process cancellations
    except Exception as exc:
        # Non-fatal — attempt the new OCO anyway
        pass

    alp.place_oca_exit(symbol, qty, stop, target, mode)
    return {
        "status": "ok",
        "symbol": symbol,
        "qty":    qty,
        "stop":   stop,
        "target": target,
        "mode":   mode,
    }