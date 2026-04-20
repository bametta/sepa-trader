from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..database import get_db, get_setting
from .. import alpaca_client as alp

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("/open")
def open_orders(db: Session = Depends(get_db)):
    mode   = get_setting(db, "trading_mode", "paper")
    orders = alp.get_open_orders(mode)
    return [
        {
            "id":     str(o.id),
            "symbol": o.symbol,
            "side":   str(o.side),
            "qty":    float(o.qty or 0),
            "status": str(o.status),
            "type":   str(o.type),
            "submitted_at": str(o.submitted_at),
        }
        for o in orders
    ]


@router.get("/history")
def trade_history(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT symbol, action, qty, price, trigger, mode, created_at FROM trade_log ORDER BY created_at DESC LIMIT :l"),
        {"l": limit},
    ).fetchall()
    return [
        {"symbol": r[0], "action": r[1], "qty": float(r[2]), "price": float(r[3]),
         "trigger": r[4], "mode": r[5], "timestamp": str(r[6])}
        for r in rows
    ]


@router.get("/alpaca-history")
def alpaca_order_history(limit: int = 100, db: Session = Depends(get_db)):
    """Full Alpaca order history — all statuses (filled, cancelled, expired, etc.)."""
    mode   = get_setting(db, "trading_mode", "paper")
    orders = alp.get_all_orders(mode, limit=limit)
    return [
        {
            "id":           str(o.id),
            "symbol":       o.symbol,
            "side":         str(o.side).replace("OrderSide.", ""),
            "qty":          float(o.qty or 0),
            "filled_qty":   float(o.filled_qty or 0),
            "filled_avg":   float(o.filled_avg_price or 0) if o.filled_avg_price else None,
            "status":       str(o.status).replace("OrderStatus.", ""),
            "type":         str(o.type).replace("OrderType.", ""),
            "submitted_at": str(o.submitted_at) if o.submitted_at else None,
            "filled_at":    str(o.filled_at) if o.filled_at else None,
        }
        for o in orders
    ]
