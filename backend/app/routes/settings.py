from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import (
    get_db, get_current_user,
    get_all_user_settings, set_user_setting,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

EDITABLE_KEYS = {
    "trading_mode", "auto_execute", "risk_pct", "stop_loss_pct", "max_positions",
    "watchlist", "webhook_secret",
    "screener_universe",
    "screener_price_min", "screener_price_max", "screener_top_n",
    "screener_min_score", "screener_vol_surge_pct", "screener_ema20_pct", "screener_ema50_pct",
    "screener_auto_run", "screener_schedule_day", "screener_schedule_time",
    "tv_username", "tv_password",
    "claude_api_key", "claude_model",
    "alpaca_paper_key", "alpaca_paper_secret",
    "alpaca_live_key",  "alpaca_live_secret",
}


@router.get("")
def get_all(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return merged settings: global defaults overlaid with user-specific overrides."""
    return get_all_user_settings(db, current_user["id"])


class SettingUpdate(BaseModel):
    value: str


@router.patch("/{key}")
def update(
    key: str,
    body: SettingUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if key not in EDITABLE_KEYS:
        raise HTTPException(400, f"Key '{key}' is not editable")
    if key == "trading_mode" and body.value not in ("paper", "live"):
        raise HTTPException(400, "trading_mode must be 'paper' or 'live'")
    set_user_setting(db, key, body.value, current_user["id"])
    return {"key": key, "value": body.value}
