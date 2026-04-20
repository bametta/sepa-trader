from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..database import get_db, get_setting
from .. import alpaca_client as alp
from ..sepa_analyzer import analyze

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
            ORDER BY symbol, week_start DESC
        """),
        {"syms": symbols},
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
        })
    return out


@router.delete("/{symbol}")
def close(symbol: str, db: Session = Depends(get_db)):
    mode = get_setting(db, "trading_mode", "paper")
    alp.close_position(symbol.upper(), mode)
    return {"status": "closed", "symbol": symbol.upper()}


@router.patch("/{symbol}/exits")
def set_exit_levels(
    symbol: str,
    stop: float,
    target: float,
    db: Session = Depends(get_db),
):
    """
    Save stop_price and target1 to the current week's plan.
    Inserts a stub row if no row exists for this symbol this week.
    Exit guard will place the OCO on the next monitor cycle.
    """
    symbol = symbol.upper()

    existing = db.execute(
        text("""
            SELECT id FROM weekly_plan
            WHERE symbol = :sym
              AND week_start = (SELECT MAX(week_start) FROM weekly_plan)
        """),
        {"sym": symbol},
    ).fetchone()

    if existing:
        db.execute(
            text("""
                UPDATE weekly_plan
                SET stop_price = :stop, target1 = :target
                WHERE symbol = :sym
                  AND week_start = (SELECT MAX(week_start) FROM weekly_plan)
            """),
            {"stop": stop, "target": target, "sym": symbol},
        )
    else:
        db.execute(
            text("""
                INSERT INTO weekly_plan
                    (week_start, symbol, rank, score, entry_price, stop_price, target1, status, mode)
                VALUES (
                    (SELECT COALESCE(MAX(week_start), CURRENT_DATE) FROM weekly_plan),
                    :sym, 99, 0, 0, :stop, :target, 'EXECUTED',
                    (SELECT value FROM settings WHERE key = 'trading_mode' LIMIT 1)
                )
            """),
            {"sym": symbol, "stop": stop, "target": target},
        )

    db.commit()
    return {"status": "ok", "symbol": symbol, "stop": stop, "target": target}


@router.post("/{symbol}/place-exits")
def place_exits_now(
    symbol: str,
    stop: float,
    target: float,
    db: Session = Depends(get_db),
):
    """
    Save levels to weekly_plan AND immediately place a live OCO on Alpaca.
    Cancels any orphaned standalone sell orders first.
    Used by the 'Place Now' path in PositionCard.
    """
    symbol = symbol.upper()
    mode   = get_setting(db, "trading_mode", "paper")

    # Persist levels so exit guard stays in sync
    existing = db.execute(
        text("""
            SELECT id FROM weekly_plan
            WHERE symbol = :sym
              AND week_start = (SELECT MAX(week_start) FROM weekly_plan)
        """),
        {"sym": symbol},
    ).fetchone()

    if existing:
        db.execute(
            text("""
                UPDATE weekly_plan
                SET stop_price = :stop, target1 = :target
                WHERE symbol = :sym
                  AND week_start = (SELECT MAX(week_start) FROM weekly_plan)
            """),
            {"stop": stop, "target": target, "sym": symbol},
        )
    else:
        db.execute(
            text("""
                INSERT INTO weekly_plan
                    (week_start, symbol, rank, score, entry_price, stop_price, target1, status, mode)
                VALUES (
                    (SELECT COALESCE(MAX(week_start), CURRENT_DATE) FROM weekly_plan),
                    :sym, 99, 0, 0, :stop, :target, 'EXECUTED',
                    (SELECT value FROM settings WHERE key = 'trading_mode' LIMIT 1)
                )
            """),
            {"sym": symbol, "stop": stop, "target": target},
        )
    db.commit()

    # Confirm position is still open
    positions = alp.get_positions(mode)
    pos = next((p for p in positions if p.symbol == symbol), None)
    if not pos:
        return {"status": "error", "detail": f"No open position found for {symbol}"}

    qty = float(pos.qty)

    # Cancel orphaned standalone sell orders before placing OCO
    try:
        open_orders = alp.get_open_orders_by_symbol(mode)
        client      = alp.get_client(mode)
        for o in open_orders.get(symbol, []):
            side        = str(getattr(o, 'side', '') or '').lower()
            order_class = str(getattr(o, 'order_class', '') or '').lower()
            is_oco      = any(kw in order_class for kw in ('oco', 'bracket', 'oto'))
            if 'sell' in side and not is_oco:
                client.cancel_order_by_id(str(o.id))
    except Exception:
        pass

    alp.place_oca_exit(symbol, qty, stop, target, mode)
    return {"status": "ok", "symbol": symbol, "qty": qty, "stop": stop, "target": target}