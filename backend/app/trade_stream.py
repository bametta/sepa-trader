"""
Alpaca trade_updates WebSocket listener.

Augments — does NOT replace — the polling watchdog. On each fill / partial_fill
event we trigger the same reconciliation paths the polling cycle uses
(check_post_close + reconcile_db_vs_alpaca), so DB state catches up to broker
state in seconds instead of waiting for the next monitor tick.

Polling stays in place as the safety net: if the WS drops a message, the next
scheduled cycle still reconciles.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from alpaca.trading.stream import TradingStream

from .config import settings
from .database import SessionLocal, get_all_user_settings
from sqlalchemy import text

logger = logging.getLogger(__name__)

_streams: dict[str, TradingStream] = {}
_tasks: dict[str, asyncio.Task] = {}


def _resolve_creds(mode: str) -> Optional[tuple[str, str]]:
    """Return (key, secret) for `mode`, falling back to env if DB has none.
    Returns None if no usable credentials."""
    db = SessionLocal()
    try:
        admin_row = db.execute(
            text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
        ).fetchone()
        admin_uid = admin_row[0] if admin_row else None
        merged = get_all_user_settings(db, admin_uid) if admin_uid else {}
    finally:
        db.close()

    if mode == "paper":
        key = (merged.get("alpaca_paper_key") or settings.alpaca_paper_key or "").strip()
        sec = (merged.get("alpaca_paper_secret") or settings.alpaca_paper_secret or "").strip()
    else:
        key = (merged.get("alpaca_live_key") or settings.alpaca_live_key or "").strip()
        sec = (merged.get("alpaca_live_secret") or settings.alpaca_live_secret or "").strip()

    if not key or not sec:
        return None
    return key, sec


def _make_handler(mode: str):
    """Build an async handler bound to `mode` that reconciles on fill events."""

    async def handler(data) -> None:
        # data is a TradeUpdate pydantic model. .event is the lifecycle event.
        try:
            event = getattr(data, "event", None)
            order = getattr(data, "order", None)
            symbol = getattr(order, "symbol", None) if order else None
            logger.info("trade_stream[%s]: event=%s symbol=%s", mode, event, symbol)

            # Reconcile only on terminal/partial fill states. Other events
            # (new, accepted, canceled) don't change position state.
            if event not in ("fill", "partial_fill"):
                return

            # Run reconciliation off the WS coroutine so a slow DB call doesn't
            # block the next event. SessionLocal is sync, so use a thread.
            await asyncio.to_thread(_reconcile_sync, mode)
        except Exception as exc:
            logger.error("trade_stream[%s]: handler error — %s", mode, exc, exc_info=True)

    return handler


def _reconcile_sync(mode: str) -> None:
    """Same paths the polling watchdog uses. Safe to call repeatedly — both
    are idempotent."""
    db = SessionLocal()
    try:
        from .position_manager import check_post_close, reconcile_db_vs_alpaca
        check_post_close(db, mode=mode)
        try:
            reconcile_db_vs_alpaca(db, mode=mode)
        except Exception as exc:
            logger.error("trade_stream[%s]: reconcile failed — %s", mode, exc)
    finally:
        db.close()


async def _start_one(mode: str) -> None:
    creds = _resolve_creds(mode)
    if not creds:
        logger.info("trade_stream[%s]: no credentials — skipping WS start.", mode)
        return

    key, sec = creds
    stream = TradingStream(api_key=key, secret_key=sec, paper=(mode == "paper"))
    stream.subscribe_trade_updates(_make_handler(mode))
    _streams[mode] = stream

    logger.info("trade_stream[%s]: starting WebSocket listener…", mode)
    try:
        # _run_forever has its own reconnect loop on websockets exceptions.
        await stream._run_forever()
    except asyncio.CancelledError:
        logger.info("trade_stream[%s]: cancelled.", mode)
        try:
            await stream.close()
        except Exception:
            pass
        raise
    except Exception as exc:
        logger.error("trade_stream[%s]: terminated unexpectedly — %s", mode, exc, exc_info=True)


def start_trade_streams() -> None:
    """Spawn one background task per mode. Safe to call multiple times — won't
    duplicate running tasks."""
    loop = asyncio.get_event_loop()
    for mode in ("paper", "live"):
        existing = _tasks.get(mode)
        if existing and not existing.done():
            continue
        _tasks[mode] = loop.create_task(_start_one(mode), name=f"trade_stream_{mode}")


async def stop_trade_streams() -> None:
    for mode, task in list(_tasks.items()):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    _tasks.clear()
    _streams.clear()
