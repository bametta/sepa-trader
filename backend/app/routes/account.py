from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db, get_current_user, get_all_user_settings
from ..config import settings as global_settings
from .. import alpaca_client as alp

router = APIRouter(prefix="/api/account", tags=["account"])


def _resolve_alpaca_client(user_settings: dict, mode: str):
    """Pick user-specific Alpaca keys, falling back to global .env values."""
    if mode == "paper":
        key    = user_settings.get("alpaca_paper_key")    or global_settings.alpaca_paper_key
        secret = user_settings.get("alpaca_paper_secret") or global_settings.alpaca_paper_secret
        paper  = True
    else:
        key    = user_settings.get("alpaca_live_key")    or global_settings.alpaca_live_key
        secret = user_settings.get("alpaca_live_secret") or global_settings.alpaca_live_secret
        paper  = False
    if not key or not secret:
        raise HTTPException(status_code=400, detail="alpaca_credentials_missing")
    return alp.get_client_for_keys(key, secret, paper)


@router.get("")
def account(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_settings = get_all_user_settings(db, current_user["id"])
    mode = user_settings.get("trading_mode", "paper")
    client = _resolve_alpaca_client(user_settings, mode)
    acct = client.get_account()
    equity     = float(acct.equity)
    last_equity = float(acct.last_equity)
    return {
        "mode":           mode,
        "portfolio_value": float(acct.portfolio_value),
        "cash":           float(acct.cash),
        "buying_power":   float(acct.buying_power),
        "equity":         equity,
        "day_pnl":        equity - last_equity,
        "day_pnl_pct":    (equity - last_equity) / last_equity * 100 if last_equity else 0,
    }
