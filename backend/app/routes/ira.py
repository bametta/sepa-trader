"""
IRA Portfolio Manager routes.

GET  /api/ira/status      config + connection state + holdings snapshot
GET  /api/ira/target      current AI target allocation + drift per holding
POST /api/ira/rebalance   {execute: bool, force: bool} — plan (and optionally run) a rebalance
GET  /api/ira/history     past proposals / executions
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, get_current_user
from .. import ira_manager as ira

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ira", tags=["ira"])


def _need_creds(db: Session, uid: int):
    if not ira.has_credentials(db, uid):
        raise HTTPException(400, "ira_credentials_missing: add your Alpaca IRA keys in Settings.")


@router.get("/status")
def ira_status(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = current_user["id"]
    cfg = ira.get_config(db, uid)
    if not ira.has_credentials(db, uid):
        return {"connected": False, "config": cfg, "snapshot": None}
    try:
        snap = ira.get_snapshot(db, uid)
    except Exception as exc:
        logger.error("ira status failed: %s", exc)
        raise HTTPException(502, f"Alpaca IRA error: {str(exc)[:200]}")
    return {"connected": True, "config": cfg, "snapshot": snap}


@router.get("/target")
def ira_target(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = current_user["id"]
    _need_creds(db, uid)
    try:
        snap = ira.get_snapshot(db, uid)
        tgt  = ira.build_target(db, uid, snap["holdings"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"IRA target error: {str(exc)[:200]}")
    held = {h["symbol"]: h["weight"] for h in snap["holdings"]}
    rows = [
        {"symbol": s,
         "target_weight": tgt["target"].get(s, 0.0),
         "current_weight": held.get(s, 0.0),
         "drift_pct": round((held.get(s, 0.0) - tgt["target"].get(s, 0.0)) * 100, 2)}
        for s in sorted(set(held) | set(tgt["target"]))
    ]
    return {**tgt, "rows": rows, "equity": snap["equity"]}


class RebalanceBody(BaseModel):
    execute: bool = False
    force: bool = False


@router.post("/rebalance")
def ira_rebalance(body: RebalanceBody, current_user: dict = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    uid = current_user["id"]
    _need_creds(db, uid)
    try:
        return ira.run_rebalance(db, uid, execute=body.execute, force=body.force)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("ira rebalance failed")
        raise HTTPException(502, f"IRA rebalance error: {str(exc)[:200]}")


@router.get("/history")
def ira_history(limit: int = 20, current_user: dict = Depends(get_current_user),
                db: Session = Depends(get_db)):
    return ira.get_history(db, current_user["id"], max(1, min(100, limit)))
