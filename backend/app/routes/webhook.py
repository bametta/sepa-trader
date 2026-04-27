import hmac
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..database import get_db, get_setting
from ..trader import _log_signal, _log_trade, _size_position, _gate, _get_weekly_plan_exits
from .. import alpaca_client as alp
from .. import telegram_alerts as tg

router = APIRouter(prefix="/api/webhook", tags=["webhook"])


class TVAlert(BaseModel):
    symbol: str
    signal: str          # BREAKOUT | PULLBACK_EMA20 | PULLBACK_EMA50 | STAGE2_WATCH | NO_SETUP
    price: float
    volume: float = 0
    score: int = 0
    secret: str = ""
    # TradingView's native sector/industry labels. Send via Pine alert template
    # using `{{syminfo.sector}}` and `{{syminfo.industry}}`. Used for sector
    # exclusion before any sizing/AI/order work runs.
    sector: str = ""
    industry: str = ""


@router.post("/tradingview")
async def tradingview(alert: TVAlert, db: Session = Depends(get_db)):
    # Validate webhook secret — must be configured; empty secret blocks ALL requests
    webhook_secret = get_setting(db, "webhook_secret", "")
    if not webhook_secret:
        raise HTTPException(status_code=403, detail="Webhook secret not configured")
    if not hmac.compare_digest(alert.secret, webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    mode = get_setting(db, "trading_mode", "paper")
    # Use mode-specific auto_execute — live is fail-safe (default off)
    if mode == "live":
        auto_execute = get_setting(db, "live_auto_execute", "false").lower() == "true"
    else:
        auto_execute = get_setting(db, "paper_auto_execute", get_setting(db, "auto_execute", "true")).lower() == "true"
    risk_pct     = float(get_setting(db, "risk_pct", "2.0"))
    stop_pct     = float(get_setting(db, "stop_loss_pct", "8.0"))

    symbol = alert.symbol.upper().replace("NASDAQ:", "").replace("NYSE:", "").replace("AMEX:", "")
    signal = alert.signal.upper()

    # Log the incoming signal
    _log_signal(db, symbol, signal, alert.score, alert.price, mode)

    action_taken = None

    try:
        clock = alp.get_clock(mode)
        market_open = clock.is_open
    except Exception:
        market_open = False

    if auto_execute and market_open:
        positions   = {p.symbol: p for p in alp.get_positions(mode)}
        acct        = alp.get_account(mode)
        portfolio   = float(acct.portfolio_value)
        max_pos     = int(get_setting(db, "max_positions", "10"))

        if signal == "BREAKOUT" and symbol not in positions:
            # Sector exclusion — uses the TV-native sector label sent by the
            # alert. Resolves the configured exclusion list (which may use
            # GICS-style names) into TV sector names via _resolve_excluded.
            # Falls back to the RS strategy's default exclusion list when
            # no `tv_excluded_sectors` setting is present, so existing
            # operators get sane defaults without configuring anything.
            tv_sector = (alert.sector or "").strip().lower()
            if tv_sector:
                try:
                    from ..rs_screener import _resolve_excluded, _DEFAULT_EXCLUDED_SECTORS
                    raw = get_setting(
                        db, "tv_excluded_sectors",
                        get_setting(db, "rs_excluded_sectors", ",".join(_DEFAULT_EXCLUDED_SECTORS)),
                    )
                    excluded = _resolve_excluded([s for s in (raw or "").split(",") if s.strip()])
                    if tv_sector in excluded:
                        action_taken = f"BLOCKED_SECTOR: {alert.sector}"
                        # Skip the rest of the BREAKOUT branch entirely.
                        signal = "_SECTOR_BLOCKED"
                except Exception:
                    pass

        if signal == "BREAKOUT" and symbol not in positions:
            if len(positions) < max_pos:
                stop, target = _get_weekly_plan_exits(db, symbol, mode)
                qty = _size_position(portfolio, alert.price, risk_pct, stop_pct, stop_price=stop)

                # Apply min_cash_pct buffer (mirrors fill_open_slots) so a TV
                # breakout cannot push cash below the configured floor.
                try:
                    from ..position_manager import _settled_funds_available
                    min_cash_pct = float(get_setting(db, "min_cash_pct", "10.0") or "10.0")
                    avail        = _settled_funds_available(acct, portfolio, min_cash_pct, 0.0)
                    if avail > 0 and alert.price > 0:
                        qty = min(qty, int(avail / alert.price))
                    else:
                        qty = 0
                except Exception:
                    pass

                if qty >= 1:
                    if not _gate(db, symbol, qty, alert.price, stop, target, "TV_BREAKOUT", mode):
                        action_taken = "BLOCKED_BY_AI"
                    else:
                        # Hard pre-submit cash guard (TV market buy goes direct
                        # to place_market_buy, bypassing _place_entry's check).
                        try:
                            live_cash  = float(getattr(alp.get_account(mode), "cash", 0) or 0)
                            worst_cost = qty * alert.price * 1.01
                        except Exception:
                            live_cash, worst_cost = 0.0, float("inf")
                        if worst_cost > live_cash:
                            action_taken = (
                                f"BLOCKED_CASH_GUARD: worst-case ${worst_cost:.2f} "
                                f"> settled cash ${live_cash:.2f}"
                            )
                        else:
                            try:
                                alp.place_market_buy(symbol, qty, mode)
                                _log_trade(db, symbol, "BUY", qty, alert.price, "TV_BREAKOUT", mode)
                                action_taken = f"BUY {qty} shares"
                                from ..claude_analyst import get_latest_pre_trade
                                v, r = get_latest_pre_trade(db, symbol, mode)
                                asyncio.create_task(tg.alert_trade(
                                    "BUY", symbol, qty, alert.price, "TV_BREAKOUT", mode,
                                    ai_verdict=v, ai_reason=r,
                                ))
                            except Exception as e:
                                action_taken = f"BUY_FAILED: {e}"

        elif signal == "NO_SETUP" and symbol in positions:
            try:
                qty = float(positions[symbol].qty)
                alp.close_position(symbol, mode)
                _log_trade(db, symbol, "SELL", qty, alert.price, "TV_STAGE2_LOST", mode)
                action_taken = f"SELL {qty} shares"
                asyncio.create_task(tg.alert_trade("SELL", symbol, qty, alert.price, "TV_STAGE2_LOST", mode))
            except Exception as e:
                action_taken = f"SELL_FAILED: {e}"
    else:
        action_taken = "market_closed" if not market_open else "auto_execute_off"

    # Telegram notification for all signals
    asyncio.create_task(tg.send(
        f"*TradingView Alert* [{mode.upper()}]\n\n"
        f"Symbol: `{symbol}`\nSignal: `{signal}`\nPrice: `${alert.price:.2f}`\n"
        f"Score: `{alert.score}/8`\nAction: `{action_taken or 'none'}`",
        level="OPPORTUNITY" if signal == "BREAKOUT" else "URGENT" if signal == "NO_SETUP" else "INFO"
    ))

    return {
        "status":       "received",
        "symbol":       symbol,
        "signal":       signal,
        "action_taken": action_taken,
        "mode":         mode,
    }
