import logging
import httpx
from .config import settings

logger = logging.getLogger(__name__)

_warned_unconfigured = False


def _build_request(message: str, level: str) -> tuple[str, dict] | None:
    global _warned_unconfigured
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        if not _warned_unconfigured:
            logger.warning(
                "Telegram alerts disabled: bot token or chat_id not configured. "
                "Operator will not receive URGENT/OPPORTUNITY notifications."
            )
            _warned_unconfigured = True
        return None
    emoji = {"URGENT": "🚨", "OPPORTUNITY": "🟢", "INFO": "ℹ️"}.get(level, "📊")
    text  = f"{emoji} *SEPA Monitor*\n\n{message}"
    url   = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    data  = {"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "Markdown"}
    return url, data


async def send(message: str, level: str = "INFO") -> bool:
    req = _build_request(message, level)
    if req is None:
        return False
    url, data = req
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=data)
            if r.status_code != 200:
                logger.error("Telegram send failed [%s]: HTTP %s — %s", level, r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:
        logger.error("Telegram send exception [%s]: %s", level, exc)
        return False


def send_sync(message: str, level: str = "INFO") -> bool:
    """Synchronous variant — safe to call from inside async functions where
    asyncio.run() would deadlock against the running loop."""
    req = _build_request(message, level)
    if req is None:
        return False
    url, data = req
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(url, json=data)
            if r.status_code != 200:
                logger.error("Telegram send_sync failed [%s]: HTTP %s — %s", level, r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:
        logger.error("Telegram send_sync exception [%s]: %s", level, exc)
        return False


async def alert_stage2_lost(symbols: list[str], mode: str):
    syms = ", ".join(symbols)
    await send(
        f"*STAGE 2 LOST* [{mode.upper()}]\n\nSymbols: `{syms}`\n\nPositions closed automatically.",
        level="URGENT",
    )


async def alert_breakout(symbols: list[str], mode: str):
    syms = ", ".join(symbols)
    await send(
        f"*BREAKOUT DETECTED* [{mode.upper()}]\n\nSymbols: `{syms}`\n\nPositions sized in automatically.",
        level="OPPORTUNITY",
    )


async def alert_trade(
    action: str,
    symbol: str,
    qty: float,
    price: float,
    trigger: str,
    mode: str,
    ai_verdict: str | None = None,
    ai_reason: str | None = None,
):
    body = (
        f"*TRADE EXECUTED* [{mode.upper()}]\n\n"
        f"Action: `{action}`\nSymbol: `{symbol}`\nQty: `{qty}`\nPrice: `${price:.2f}`\nTrigger: `{trigger}`"
    )
    if ai_verdict:
        body += f"\n\n*AI gate:* `{ai_verdict}`"
        if ai_reason:
            body += f"\n_{ai_reason[:200]}_"
    await send(body, level="INFO")


def alert_trade_sync(
    action: str,
    symbol: str,
    qty: float,
    price: float,
    trigger: str,
    mode: str,
    ai_verdict: str | None = None,
    ai_reason: str | None = None,
) -> bool:
    """Sync trade alert for non-async call sites (monitor loop, position_manager)."""
    body = (
        f"*TRADE EXECUTED* [{mode.upper()}]\n\n"
        f"Action: `{action}`\nSymbol: `{symbol}`\nQty: `{qty}`\nPrice: `${price:.2f}`\nTrigger: `{trigger}`"
    )
    if ai_verdict:
        body += f"\n\n*AI gate:* `{ai_verdict}`"
        if ai_reason:
            body += f"\n_{ai_reason[:200]}_"
    return send_sync(body, level="INFO")


def alert_system_error_sync(context: str, error: str | Exception, level: str = "URGENT") -> bool:
    """Sync system-failure alert. Safe to call from any sync code path
    (scheduler jobs, monitor loop, screener except blocks).

    `context` is a short label like "Minervini screener" or "Monitor loop".
    `error` may be a string or Exception; rendered with type prefix when Exception.
    """
    if isinstance(error, Exception):
        body = f"{type(error).__name__}: {error}"
    else:
        body = str(error)
    body = body[:600]
    msg = f"*SYSTEM ERROR*\n\nContext: `{context}`\n\n```\n{body}\n```"
    return send_sync(msg, level=level)


async def alert_monitor_summary(portfolio: float, day_pnl: float, positions: int, mode: str, interval_minutes: int = 30):
    pnl_sign = "+" if day_pnl >= 0 else ""
    if interval_minutes < 60:
        freq = f"{interval_minutes}-min Check"
    elif interval_minutes == 60:
        freq = "Hourly Check"
    else:
        freq = f"{interval_minutes // 60}h Check"
    await send(
        f"*{freq}* [{mode.upper()}]\n\n"
        f"Portfolio: `${portfolio:,.2f}`\nDay P&L: `{pnl_sign}${day_pnl:,.2f}`\nPositions: `{positions}`",
        level="INFO",
    )
