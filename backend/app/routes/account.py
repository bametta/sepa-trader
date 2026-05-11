import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..database import get_db, get_current_user, get_all_user_settings
from ..config import settings as global_settings
from .. import alpaca_client as alp
from ..utils import sf as _sf
from ..crypto import decrypt as _dec

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/account", tags=["account"])


def _resolve_alpaca_client(user_settings: dict, mode: str, is_admin: bool = False):
    """
    Pick Alpaca credentials for a user.
    Admins fall back to .env global keys so their existing account just works.
    Regular users must configure their own credentials.
    """
    if mode == "paper":
        key    = user_settings.get("alpaca_paper_key")
        secret = user_settings.get("alpaca_paper_secret")
        if is_admin:
            key    = key    or global_settings.alpaca_paper_key
            secret = secret or global_settings.alpaca_paper_secret
        paper = True
    else:
        key    = user_settings.get("alpaca_live_key")
        secret = user_settings.get("alpaca_live_secret")
        if is_admin:
            key    = key    or global_settings.alpaca_live_key
            secret = secret or global_settings.alpaca_live_secret
        paper = False
    if not key or not secret:
        raise HTTPException(status_code=400, detail="alpaca_credentials_missing")
    logger.info(
        "_resolve_alpaca_client: mode=%s paper=%s credentials=set",
        mode, paper,
    )
    return alp.get_client_for_keys(key, secret, paper)


def _fetch_account_data(client, name: str, mode: str) -> dict | None:
    """Fetch and normalise one Alpaca account. Returns None on any error."""
    try:
        acct    = client.get_account()
        equity  = _sf(acct.equity,      0.0)
        last_eq = _sf(acct.last_equity, 0.0)
        day_pnl = equity - last_eq

        non_marginable_bp = _sf(
            getattr(acct, "non_marginable_buying_power", None),
            _sf(acct.buying_power, 0.0),
        )

        # Total P&L (realized + unrealized since account inception).
        # Use the raw REST endpoint so this works across all alpaca-py versions.
        # GET /v2/account/portfolio/history?period=all → profit_loss[-1] = cumulative P&L
        total_pl = None
        try:
            history  = client.get("/account/portfolio/history", {"period": "all"})
            pl_list  = history.get("profit_loss") if isinstance(history, dict) else getattr(history, "profit_loss", None)
            if pl_list:
                total_pl = _sf(pl_list[-1], None)
        except Exception as hist_exc:
            logger.warning("_fetch_account_data(%s, %s): portfolio history failed: %s", name, mode, hist_exc)

        if total_pl is None:
            # Fallback: unrealized P&L from open positions (excludes already-closed trades)
            try:
                positions = client.get_all_positions()
                total_pl  = sum(_sf(getattr(p, "unrealized_pl", None), 0.0) for p in positions)
            except Exception as pos_exc:
                logger.warning("_fetch_account_data(%s, %s): positions fallback failed: %s", name, mode, pos_exc)
                total_pl = 0.0

        # Build a stable deposit key: "paper_main", "live_main", "live_dual_momentum" etc.
        slug = name.lower().replace(" ", "_")
        deposit_key = f"total_deposited_{mode}_{slug}"

        return {
            "name":              name,
            "mode":              mode,
            "deposit_key":       deposit_key,
            "portfolio_value":   _sf(acct.portfolio_value, 0.0),
            "cash":              _sf(acct.cash, 0.0),
            "buying_power":      _sf(acct.buying_power, 0.0),
            "non_marginable_bp": non_marginable_bp,
            "equity":            equity,
            "day_pnl":           day_pnl,
            "day_pnl_pct":       (day_pnl / last_eq * 100) if last_eq else 0.0,
            "unrealized_pl":     total_pl,
        }
    except Exception as exc:
        logger.warning("_fetch_account_data(%s, %s): %s", name, mode, exc)
        return None


@router.get("/overview")
def accounts_overview(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return all Alpaca accounts for this user, grouped by mode (paper / live).
    Includes the main account plus any strategy accounts that have dedicated
    API keys configured (currently: Dual Momentum).
    """
    uid          = current_user["id"]
    is_admin     = current_user["role"] == "admin"
    user_settings = get_all_user_settings(db, uid)

    # Strategy configs with potentially separate keys.
    # Each strategy has one row per trading_mode; load both and pick the
    # row matching the mode we're rendering inside the loop below.
    strategy_rows = db.execute(
        text("""
            SELECT strategy_name, trading_mode,
                   alpaca_paper_key, alpaca_paper_secret,
                   alpaca_live_key,  alpaca_live_secret
            FROM strategy_config
            WHERE user_id = :uid
        """),
        {"uid": uid},
    ).fetchall()

    # Pretty-print strategy names
    STRATEGY_LABELS = {
        "dual_momentum": "Dual Momentum",
    }

    result = {"paper": [], "live": []}

    for mode in ("paper", "live"):
        # ── Main account ──────────────────────────────────────────────────────
        try:
            main_client = _resolve_alpaca_client(user_settings, mode, is_admin)
            data = _fetch_account_data(main_client, "Main", mode)
            if data:
                data["total_deposited"] = float(user_settings.get(data["deposit_key"]) or 0)
                result[mode].append(data)
        except HTTPException:
            pass  # credentials not configured for this mode

        # ── Strategy accounts (only if they have DEDICATED keys) ──────────────
        for row in strategy_rows:
            strat_name, row_mode, pk, ps, lk, ls = row
            if row_mode != mode:
                continue
            if mode == "paper":
                enc_key, enc_secret = pk, ps
            else:
                enc_key, enc_secret = lk, ls

            # Stored credentials are encrypted at rest — decrypt before use.
            key    = _dec(enc_key or "")
            secret = _dec(enc_secret or "")

            # Skip if no dedicated keys or same as main account credentials
            main_key = user_settings.get(f"alpaca_{mode}_key", "")
            if not key or key == main_key:
                continue

            try:
                client = alp.get_client_for_keys(key, secret, mode == "paper")
                label  = STRATEGY_LABELS.get(strat_name, strat_name.replace("_", " ").title())
                data   = _fetch_account_data(client, label, mode)
                if data:
                    data["total_deposited"] = float(user_settings.get(data["deposit_key"]) or 0)
                    result[mode].append(data)
            except Exception as exc:
                logger.warning("accounts_overview: strategy %s [%s]: %s", strat_name, mode, exc)

    return result


@router.get("")
def account(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_settings = get_all_user_settings(db, current_user["id"])
    mode = user_settings.get("trading_mode", "paper")
    client = _resolve_alpaca_client(user_settings, mode, is_admin=current_user["role"] == "admin")
    acct        = client.get_account()
    equity      = _sf(acct.equity,      0.0)
    last_equity = _sf(acct.last_equity, 0.0)
    day_pnl     = equity - last_equity
    return {
        "mode":            mode,
        "portfolio_value": _sf(acct.portfolio_value, 0.0),
        "cash":            _sf(acct.cash, 0.0),
        "buying_power":    _sf(acct.buying_power, 0.0),
        "equity":          equity,
        "day_pnl":         day_pnl,
        "day_pnl_pct":     (day_pnl / last_equity * 100) if last_equity else 0.0,
    }


@router.patch("/deposits")
def update_total_deposited(
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save deposited capital for a specific account (keyed by deposit_key)."""
    from ..database import set_user_setting
    amount      = body.get("total_deposited")
    deposit_key = body.get("deposit_key", "total_deposited_paper_main")
    if amount is None or not isinstance(amount, (int, float)) or amount < 0:
        raise HTTPException(400, "total_deposited must be a non-negative number")
    # Validate key format to prevent arbitrary setting writes
    if not deposit_key.startswith("total_deposited_"):
        raise HTTPException(400, "invalid deposit_key")
    set_user_setting(db, deposit_key, str(float(amount)), current_user["id"])
    return {"deposit_key": deposit_key, "total_deposited": float(amount)}
