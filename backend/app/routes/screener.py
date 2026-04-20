import traceback
import logging

from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..database import get_db, SessionLocal, get_setting, set_setting

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/screener", tags=["screener"])


@router.get("/weekly-plan")
def get_weekly_plan(db: Session = Depends(get_db)):
    rows = db.execute(
        text("""
            SELECT week_start, symbol, rank, score, signal,
                   entry_price, stop_price, target1, target2,
                   position_size, risk_amount, rationale, status, mode, created_at
            FROM weekly_plan
            WHERE week_start = (SELECT MAX(week_start) FROM weekly_plan)
            ORDER BY rank ASC
        """)
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/status")
def get_screener_status(db: Session = Depends(get_db)):
    """Return current screener state and last-run summary."""
    return {
        "status":          get_setting(db, "screener_status",   "idle"),
        "error":           get_setting(db, "screener_error",    ""),
        "last_run_summary":get_setting(db, "screener_last_run", ""),
        "count":           int(get_setting(db, "screener_count", "0") or "0"),
    }


@router.get("/dd")
def get_weekly_dd(db: Session = Depends(get_db)):
    """Fetch due-diligence data for every symbol in the current week's plan."""
    rows = db.execute(
        text("""
            SELECT symbol FROM weekly_plan
            WHERE week_start = (SELECT MAX(week_start) FROM weekly_plan)
            ORDER BY rank ASC
        """)
    ).fetchall()
    symbols = [r[0] for r in rows]
    if not symbols:
        return []
    from ..dd_fetcher import fetch_dd_batch
    return fetch_dd_batch(symbols)


@router.get("/history")
def get_plan_history(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT DISTINCT week_start, COUNT(*) as cnt FROM weekly_plan GROUP BY week_start ORDER BY week_start DESC LIMIT 12")
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/weekly-plan/{week_start}")
def get_plan_for_week(week_start: str, db: Session = Depends(get_db)):
    rows = db.execute(
        text("""
            SELECT week_start, symbol, rank, score, signal,
                   entry_price, stop_price, target1, target2,
                   position_size, risk_amount, rationale, status, mode, created_at
            FROM weekly_plan WHERE week_start = :w ORDER BY rank ASC
        """),
        {"w": week_start},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/run")
def trigger_screener(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Start the screener in the background. Poll GET /status to track progress."""
    set_setting(db, "screener_status", "running")
    set_setting(db, "screener_error",  "")

    def _run():
        db2 = SessionLocal()
        try:
            from ..screener import run_screener
            results = run_screener(db2)
            set_setting(db2, "screener_status", "done")
            set_setting(db2, "screener_count",  str(len(results)))
        except Exception as exc:
            err_msg = f"{exc}\n{traceback.format_exc()}"
            log.error("Screener background task failed:\n%s", err_msg)
            db3 = SessionLocal()
            try:
                set_setting(db3, "screener_status", "error")
                set_setting(db3, "screener_error",  str(exc)[:500])
            finally:
                db3.close()
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return {"status": "running"}


@router.post("/sync-tradingview")
def sync_tradingview(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    tv_user = get_setting(db, "tv_username", "")
    tv_pass = get_setting(db, "tv_password", "")
    if not tv_user or not tv_pass:
        from fastapi import HTTPException
        raise HTTPException(400, "TradingView credentials not configured in Settings.")

    rows = db.execute(
        text("""
            SELECT symbol FROM weekly_plan
            WHERE week_start = (SELECT MAX(week_start) FROM weekly_plan)
            ORDER BY rank ASC
        """)
    ).fetchall()
    symbols = [r[0] for r in rows]
    if not symbols:
        from fastapi import HTTPException
        raise HTTPException(404, "No weekly plan found. Run the screener first.")

    def _sync():
        from ..tradingview_client import update_weekly_picks
        result = update_weekly_picks(tv_user, tv_pass, symbols)
        if result["ok"]:
            log.info("TV sync: weekly_picks %s (%d symbols).", result["action"], result["count"])
        else:
            log.error("TV sync failed: %s", result["error"])

    background_tasks.add_task(_sync)
    return {"status": "sync_started", "symbols": symbols,
            "message": f"Syncing {len(symbols)} symbols to TradingView weekly_picks."}


@router.get("/analysis")
def get_analyses(limit: int = 20, db: Session = Depends(get_db)):
    """Return recent Claude AI analyses."""
    from ..claude_analyst import get_latest_analyses
    return get_latest_analyses(db, limit=limit)


@router.post("/analysis/run")
def trigger_analysis(db: Session = Depends(get_db)):
    """Manually trigger a Claude analysis of the current week's picks."""
    from ..claude_analyst import analyze_picks, log_analysis
    mode = db.execute(text("SELECT value FROM settings WHERE key='trading_mode'")).scalar() or "paper"
    picks_rows = db.execute(
        text("""
            SELECT symbol, score, signal, entry_price, stop_price, target1, status, rationale
            FROM weekly_plan
            WHERE week_start = (SELECT MAX(week_start) FROM weekly_plan)
            ORDER BY rank ASC
        """)
    ).fetchall()
    if not picks_rows:
        from fastapi import HTTPException
        raise HTTPException(404, "No weekly plan found.")
    picks = [dict(r._mapping) for r in picks_rows]
    try:
        analysis = analyze_picks(db, picks)
        log_analysis(db, "manual", None, analysis, mode)
        return {"analysis": analysis}
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(400, str(exc))


@router.patch("/weekly-plan/{symbol}/status")
def update_plan_status(symbol: str, body: dict, db: Session = Depends(get_db)):
    status = body.get("status", "PENDING")
    if status not in ("PENDING", "EXECUTED", "PARTIAL", "SKIPPED"):
        from fastapi import HTTPException
        raise HTTPException(400, "Invalid status")
    db.execute(
        text("""
            UPDATE weekly_plan SET status = :s
            WHERE symbol = :sym
              AND week_start = (SELECT MAX(week_start) FROM weekly_plan)
        """),
        {"s": status, "sym": symbol.upper()},
    )
    db.commit()
    return {"symbol": symbol, "status": status}
