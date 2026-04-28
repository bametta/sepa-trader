from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopLimitOrderRequest,
    GetOrdersRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from .config import settings
import logging
import time

logger = logging.getLogger(__name__)

_clients: dict[str, TradingClient] = {}


def get_client(mode: str = "paper") -> TradingClient:
    """Global client using .env credentials — used by scheduler/background jobs only."""
    if mode not in _clients:
        if mode == "paper":
            _clients[mode] = TradingClient(
                api_key=(settings.alpaca_paper_key or "").strip(),
                secret_key=(settings.alpaca_paper_secret or "").strip(),
                paper=True,
            )
        else:
            _clients[mode] = TradingClient(
                api_key=(settings.alpaca_live_key or "").strip(),
                secret_key=(settings.alpaca_live_secret or "").strip(),
                paper=False,
            )
    return _clients[mode]


def get_client_for_keys(api_key: str, secret_key: str, paper: bool) -> TradingClient:
    """Create a TradingClient from explicit credentials (per-user API requests).
    Strips surrounding whitespace so copy-paste artefacts don't cause 401s.
    """
    return TradingClient(
        api_key=api_key.strip(),
        secret_key=secret_key.strip(),
        paper=paper,
    )


def configure_from_db_settings(merged: dict, mode: str, is_admin: bool = True) -> None:
    """
    Update the global cached client for `mode` using credentials from the merged
    user+global settings dict (as returned by get_all_user_settings).

    For admin users, falls back to .env credentials when DB credentials are absent
    — the same logic used by the account route's _resolve_alpaca_client.

    Call this at the start of run_monitor so the live client uses DB-stored keys
    (saved via Settings panel) rather than the .env-file keys which may be empty.
    """
    if mode == "paper":
        key    = (merged.get("alpaca_paper_key") or "").strip()
        secret = (merged.get("alpaca_paper_secret") or "").strip()
        if is_admin:
            key    = key    or (settings.alpaca_paper_key or "").strip()
            secret = secret or (settings.alpaca_paper_secret or "").strip()
        paper = True
    else:
        key    = (merged.get("alpaca_live_key") or "").strip()
        secret = (merged.get("alpaca_live_secret") or "").strip()
        if is_admin:
            key    = key    or (settings.alpaca_live_key or "").strip()
            secret = secret or (settings.alpaca_live_secret or "").strip()
        paper = False

    if not key or not secret:
        raise ValueError(f"No Alpaca credentials configured for {mode} mode")

    logger.info("configure_from_db_settings: updating %s client (credentials set)", mode)
    _clients[mode] = TradingClient(api_key=key, secret_key=secret, paper=paper)


def get_account(mode: str = "paper"):
    return get_client(mode).get_account()


def get_positions(mode: str = "paper"):
    return get_client(mode).get_all_positions()


def _get_user_client(db, user_id: int | None, mode: str) -> TradingClient | None:
    """Resolve a TradingClient from DB-stored credentials for a specific user.
    Returns None if user_id is None or no credentials are found (caller falls
    back to the global env-based client).
    """
    if not user_id:
        return None
    from .database import get_user_setting as _gus
    is_admin = db.execute(
        __import__("sqlalchemy").text("SELECT role FROM users WHERE id = :id"),
        {"id": user_id},
    ).scalar() == "admin"
    if mode == "paper":
        key    = (_gus(db, "alpaca_paper_key",    "", user_id) or "").strip()
        secret = (_gus(db, "alpaca_paper_secret", "", user_id) or "").strip()
        if is_admin:
            key    = key    or (settings.alpaca_paper_key    or "").strip()
            secret = secret or (settings.alpaca_paper_secret or "").strip()
        paper = True
    else:
        key    = (_gus(db, "alpaca_live_key",    "", user_id) or "").strip()
        secret = (_gus(db, "alpaca_live_secret", "", user_id) or "").strip()
        if is_admin:
            key    = key    or (settings.alpaca_live_key    or "").strip()
            secret = secret or (settings.alpaca_live_secret or "").strip()
        paper = False
    if not key or not secret:
        return None
    return get_client_for_keys(key, secret, paper)


def get_positions_for_user(db, user_id: int | None, mode: str = "paper"):
    """Fetch positions using DB-stored credentials for a specific user.
    Falls back to the global client if no DB creds are found.
    """
    client = _get_user_client(db, user_id, mode)
    return (client or get_client(mode)).get_all_positions()


def get_account_for_user(db, user_id: int | None, mode: str = "paper"):
    """Fetch account using DB-stored credentials for a specific user.
    Falls back to the global client if no DB creds are found.
    """
    client = _get_user_client(db, user_id, mode)
    return (client or get_client(mode)).get_account()


def get_open_orders_by_symbol_for_user(db, user_id: int | None, mode: str = "paper") -> dict[str, list]:
    """Open orders keyed by symbol, using user-scoped credentials."""
    client = _get_user_client(db, user_id, mode) or get_client(mode)
    orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    result: dict[str, list] = {}
    for o in orders:
        result.setdefault(o.symbol, []).append(o)
    return result


def get_open_orders(mode: str = "paper"):
    return get_client(mode).get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))


def get_open_orders_by_symbol(mode: str = "paper") -> dict[str, list]:
    """Return all open orders keyed by symbol for quick lookup."""
    orders = get_client(mode).get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    result: dict[str, list] = {}
    for o in orders:
        result.setdefault(o.symbol, []).append(o)
    return result


def get_all_orders(mode: str = "paper", limit: int = 100):
    return get_client(mode).get_orders(
        GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
    )


def find_recent_fill(mode: str, symbol: str, side: str, days: int = 30):
    """Return the most-recent FILLED order for (symbol, side) within `days`,
    or None if none found. Used to reconstruct SELL fills that were executed
    by Alpaca-side bracket OCOs (stop / take-profit) which the bot never
    submitted itself and therefore never logged.
    """
    from datetime import datetime, timedelta, timezone
    side_enum = OrderSide.SELL if side.upper() == "SELL" else OrderSide.BUY
    after = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        orders = get_client(mode).get_orders(
            GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                side=side_enum,
                after=after,
                limit=100,
            )
        )
    except Exception:
        return None
    fills = [o for o in (orders or []) if getattr(o, "status", None) and str(o.status).lower().endswith("filled") and getattr(o, "filled_at", None)]
    if not fills:
        return None
    fills.sort(key=lambda o: o.filled_at, reverse=True)
    return fills[0]


def find_recent_fills(mode: str, symbol: str, side: str, days: int = 30) -> list:
    """Return ALL filled orders for (symbol, side) within `days`, sorted oldest
    → newest. Used by _log_alpaca_side_sell to reconstruct multi-leg exits
    (T1 then T2) so each fill becomes its own trade_log SELL row instead of
    one row at the most-recent fill price.
    """
    from datetime import datetime, timedelta, timezone
    side_enum = OrderSide.SELL if side.upper() == "SELL" else OrderSide.BUY
    after = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        orders = get_client(mode).get_orders(
            GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                side=side_enum,
                after=after,
                limit=100,
            )
        )
    except Exception:
        return []
    fills = [
        o for o in (orders or [])
        if getattr(o, "status", None)
           and str(o.status).lower().endswith("filled")
           and getattr(o, "filled_at", None)
    ]
    fills.sort(key=lambda o: o.filled_at)
    return fills


def find_position_close_activity(mode: str, symbol: str, days: int = 90) -> list:
    """Query /account/activities for non-trade dispositions of `symbol` —
    mergers (MA), symbol/name changes (SC, NC), reorgs (REORG), cash-in-lieu
    (CIL), ACATS transfers, and stock splits (SPLIT).

    Used as a fallback when a position is closed on Alpaca but no SELL order
    exists. Returns list of dicts with at least {qty, price, timestamp,
    activity_type} so trade_log can record what happened to the shares.

    NOTE: Alpaca rejects the entire request with `invalid activity type` if
    ANY listed type is unrecognized — only include types from Alpaca's
    documented enum. `NRC` was previously listed and broke this whole call.
    """
    from datetime import datetime, timedelta, timezone
    after = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    activity_types = "MA,SC,NC,REORG,CIL,ACATC,ACATS,SPLIT"
    try:
        raw = get_client(mode).get(
            "/account/activities",
            data={"activity_types": activity_types, "after": after},
        )
    except Exception as exc:
        logger.warning("find_position_close_activity[%s] %s: %s", mode, symbol, exc)
        return []

    out: list = []
    for a in (raw or []):
        if not isinstance(a, dict):
            continue
        if (a.get("symbol") or "").upper() != symbol.upper():
            continue
        try:
            qty = abs(float(a.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0.0
        try:
            price = float(a.get("per_share_amount") or a.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if qty <= 0:
            continue
        out.append({
            "qty":           qty,
            "price":         price,
            "timestamp":     a.get("date") or a.get("transaction_time"),
            "activity_type": a.get("activity_type") or "CORP_ACTION",
            "description":   a.get("description") or "",
        })
    return out


def get_clock(mode: str = "paper"):
    return get_client(mode).get_clock()


def place_market_buy(symbol: str, qty: float, mode: str = "paper"):
    """Simple market buy with no exit legs. GTC so it survives past market close."""
    req = MarketOrderRequest(
        symbol=symbol,
        qty=round(qty, 0),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
    )
    return get_client(mode).submit_order(req)


def place_limit_buy(symbol: str, qty: float, limit_price: float, mode: str = "paper"):
    """DAY limit buy with no exit legs. Cancels automatically if not filled today."""
    req = LimitOrderRequest(
        symbol=symbol,
        qty=round(qty, 0),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    return get_client(mode).submit_order(req)


def place_market_sell(symbol: str, qty: float, mode: str = "paper"):
    """Simple market sell. GTC so it survives past market close."""
    req = MarketOrderRequest(
        symbol=symbol,
        qty=round(qty, 0),
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
    )
    return get_client(mode).submit_order(req)


def place_bracket_buy(
    symbol: str,
    qty: float,
    stop_price: float,
    target_price: float,
    mode: str = "paper",
):
    """
    Market buy with attached stop-loss and take-profit legs (bracket/OCA).
    Entry leg: DAY (required by Alpaca for market bracket entries).
    Stop and target legs: GTC — remain active until one fills or position is closed.
    Use this for NEW entries only. For existing positions use place_oca_exit().
    """
    if qty <= 0 or stop_price <= 0 or target_price <= 0:
        raise ValueError(f"place_bracket_buy {symbol}: invalid qty/stop/target ({qty}/{stop_price}/{target_price})")
    if target_price <= stop_price:
        raise ValueError(f"place_bracket_buy {symbol}: target ${target_price:.2f} must exceed stop ${stop_price:.2f}")
    req = MarketOrderRequest(
        symbol=symbol,
        qty=round(qty, 0),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        take_profit=TakeProfitRequest(limit_price=round(target_price, 2)),
    )
    return get_client(mode).submit_order(req)


def place_limit_bracket_buy(
    symbol: str,
    qty: float,
    entry_price: float,
    stop_price: float,
    target_price: float,
    slippage_pct: float = 0.5,
    mode: str = "paper",
):
    """
    DAY limit buy with attached stop-loss and take-profit bracket.
    Entry fills only up to entry_price × (1 + slippage_pct/100).
    If not filled by end of day, Alpaca cancels automatically.
    Use for pullback-to-MA entries where price is already near the target level.
    """
    if qty <= 0 or entry_price <= 0 or stop_price <= 0 or target_price <= 0:
        raise ValueError(
            f"place_limit_bracket_buy {symbol}: invalid qty/entry/stop/target "
            f"({qty}/{entry_price}/{stop_price}/{target_price})"
        )
    if stop_price >= entry_price:
        raise ValueError(f"place_limit_bracket_buy {symbol}: stop ${stop_price:.2f} must be below entry ${entry_price:.2f}")
    if target_price <= entry_price:
        raise ValueError(f"place_limit_bracket_buy {symbol}: target ${target_price:.2f} must exceed entry ${entry_price:.2f}")
    limit_price = round(entry_price * (1 + slippage_pct / 100), 2)
    req = LimitOrderRequest(
        symbol=symbol,
        qty=round(qty, 0),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        take_profit=TakeProfitRequest(limit_price=round(target_price, 2)),
    )
    return get_client(mode).submit_order(req)


def place_stop_limit_buy(
    symbol: str,
    qty: float,
    stop_price: float,
    slippage_pct: float = 1.0,
    mode: str = "paper",
):
    """
    DAY stop-limit buy — for breakout entries.
    Activates only when stock trades at or above stop_price (confirms the breakout),
    then fills up to stop_price × (1 + slippage_pct/100).
    Alpaca does not support brackets on stop-limit entries; the monitor will add
    OCO exits on the next cycle after the entry fills.
    """
    limit_price = round(stop_price * (1 + slippage_pct / 100), 2)
    req = StopLimitOrderRequest(
        symbol=symbol,
        qty=round(qty, 0),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        stop_price=round(stop_price, 2),
        limit_price=limit_price,
    )
    return get_client(mode).submit_order(req)


def place_oca_exit(
    symbol: str,
    qty: float,
    stop_price: float,
    target_price: float,
    mode: str = "paper",
):
    """
    Place a single OCO (One-Cancels-Other) sell order for an existing position.
    When one leg fills, Alpaca automatically cancels the other.

    Alpaca's API rejects OCO unless BOTH `stop_loss.stop_price` and
    `take_profit.limit_price` are provided (error 40010001), so both kwargs
    are required regardless of parent type. The parent is a LimitOrderRequest
    where the parent's limit_price doubles as the take-profit working price —
    the take_profit kwarg is the API-mandated sibling spec.
    """
    req = LimitOrderRequest(
        symbol=symbol,
        qty=round(qty, 0),
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        limit_price=round(target_price, 2),
        order_class=OrderClass.OCO,
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        take_profit=TakeProfitRequest(limit_price=round(target_price, 2)),
    )
    return get_client(mode).submit_order(req)


def verify_oca_parent(parent) -> tuple[bool, bool]:
    """Verify that an OCO parent Order returned from submit_order has both
    a stop and a limit (take-profit) child. Returns (has_limit, has_stop).

    Parent layout (see place_oca_exit): parent itself is the stop_limit, and
    the target lives in `.legs` as the take_profit sibling. We inspect .legs
    directly because Alpaca's "held" sibling status doesn't reliably appear
    in status=open queries."""
    if parent is None:
        return False, False
    parent_type = str(getattr(parent, "order_type", "") or getattr(parent, "type", "") or "").lower()
    has_stop  = "stop" in parent_type
    has_limit = "limit" in parent_type and "stop" not in parent_type  # parent is stop_limit, not pure limit
    for leg in (getattr(parent, "legs", None) or []):
        leg_type = str(getattr(leg, "order_type", "") or getattr(leg, "type", "") or "").lower()
        if "stop" in leg_type:
            has_stop = True
        elif "limit" in leg_type:
            has_limit = True
    return has_limit, has_stop


def cancel_symbol_exit_orders(symbol: str, mode: str = "paper") -> list[str]:
    """
    Cancel all open sell orders for a symbol (OCO, bracket, or standalone).
    Returns list of cancelled order IDs.
    """
    client      = get_client(mode)
    open_orders = get_open_orders_by_symbol(mode)
    cancelled   = []

    for o in open_orders.get(symbol, []):
        side = str(getattr(o, 'side', '') or '').lower()
        if 'sell' in side:
            try:
                client.cancel_order_by_id(str(o.id))
                cancelled.append(str(o.id))
                logger.debug("Cancelled exit order %s for %s [%s]", o.id, symbol, mode)
            except Exception as exc:
                logger.warning("Could not cancel order %s for %s: %s", o.id, symbol, exc)

    return cancelled


def wait_for_orders_cancelled(
    symbol: str,
    mode: str = "paper",
    timeout: float = 15.0,
    poll_interval: float = 0.4,
) -> bool:
    """
    Poll until no open sell orders remain for a symbol, or timeout elapses.
    Returns True if orders are cleared, False if timeout hit.
    Used after cancel_symbol_exit_orders() to ensure Alpaca has fully
    processed the cancellation before placing a replacement OCO.
    """
    elapsed = 0.0
    while elapsed < timeout:
        open_orders   = get_open_orders_by_symbol(mode)
        symbol_orders = open_orders.get(symbol, [])
        sell_orders   = [
            o for o in symbol_orders
            if 'sell' in str(getattr(o, 'side', '') or '').lower()
        ]
        if not sell_orders:
            return True
        time.sleep(poll_interval)
        elapsed += poll_interval

    logger.warning(
        "wait_for_orders_cancelled: timeout after %.1fs — sell orders still open for %s",
        timeout, symbol,
    )
    return False


def replace_oca_exit(
    symbol: str,
    qty: float,
    new_stop: float,
    target_price: float,
    mode: str = "paper",
):
    """
    Cancel existing exit orders for a symbol and place a fresh OCO with
    updated stop/target prices. Used by the trailing stop logic and the
    exit guard when plan prices change.

    Polls until cancellation is confirmed before placing the new order —
    avoids Alpaca rejecting the replacement as an oversell.
    """
    cancelled = cancel_symbol_exit_orders(symbol, mode)
    if cancelled:
        cleared = wait_for_orders_cancelled(symbol, mode, timeout=6.0, poll_interval=0.4)
        if not cleared:
            logger.warning(
                "replace_oca_exit: proceeding despite timeout — "
                "cancellation may not be fully settled for %s", symbol,
            )

    # CRITICAL: cancel succeeded; if the replacement place fails, the position
    # is naked until the next monitor cycle. Retry the placement once before
    # giving up so transient API errors don't strand a position.
    last_exc = None
    for attempt in (1, 2):
        try:
            return place_oca_exit(symbol, qty, new_stop, target_price, mode)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "replace_oca_exit: place attempt %d failed for %s: %s",
                attempt, symbol, exc,
            )
            time.sleep(0.5)
    # Both attempts failed — surface to caller so it can re-raise/alert. The
    # caller's existing `except` block catches and fires the NAKED POSITION
    # telegram alert.
    raise last_exc


def close_position(symbol: str, mode: str = "paper"):
    """
    Flatten a position and cancel all open orders for the symbol.
    Alpaca's close_position() handles cancelling attached bracket/OCA legs automatically.
    """
    return get_client(mode).close_position(symbol)