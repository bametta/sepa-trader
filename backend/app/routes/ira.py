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
    proposal_id: int | None = None


@router.post("/rebalance")
def ira_rebalance(body: RebalanceBody, current_user: dict = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """Preview (default) or execute a previously previewed proposal.

    Manual execution never recomputes the plan: it runs exactly the gated proposal the user
    previewed, after re-validating it against the live account.
    """
    uid = current_user["id"]
    _need_creds(db, uid)
    try:
        if body.execute:
            if body.proposal_id is None:
                raise HTTPException(400, "proposal_id_required: preview first, then confirm that proposal")
            return ira.execute_proposal(db, uid, body.proposal_id)
        return ira.run_rebalance(db, uid, execute=False, force=body.force)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        logger.exception("ira rebalance failed")
        raise HTTPException(502, f"IRA rebalance error: {str(exc)[:200]}")


class HaltBody(BaseModel):
    halted: bool


@router.post("/halt")
def ira_halt(body: HaltBody, current_user: dict = Depends(get_current_user),
             db: Session = Depends(get_db)):
    """Kill switch: while halted, no IRA orders are placed (manual or scheduled)."""
    from ..database import set_user_setting
    set_user_setting(db, "ira_halt", "true" if body.halted else "false", current_user["id"])
    ira._alert(f"IRA trading {'HALTED' if body.halted else 'RESUMED'} via kill switch.", "URGENT" if body.halted else "INFO")
    return {"halted": body.halted}


@router.get("/history")
def ira_history(limit: int = 20, current_user: dict = Depends(get_current_user),
                db: Session = Depends(get_db)):
    return ira.get_history(db, current_user["id"], max(1, min(100, limit)))


@router.get("/plan")
def ira_plan(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Retirement projection + contribution recommendation from the user's profile."""
    from .. import ira_planner, ira_data
    uid = current_user["id"]
    balance, tactical = 0.0, None
    if ira.has_credentials(db, uid):
        try:
            snap = ira.get_snapshot(db, uid)
            balance = snap["equity"]
            if snap["holdings"]:
                tactical = ira_planner.equity_share_of(ira_data.bucket_exposure(
                    {h["symbol"]: h["weight"] for h in snap["holdings"]}))
        except Exception as exc:
            logger.warning("ira plan: snapshot failed: %s", exc)
    return {**ira_planner.build_plan(db, uid, balance, tactical), "balance": balance}


@router.get("/reference")
def ira_reference(current_user: dict = Depends(get_current_user)):
    """Fund reference table (expense ratio, category, exposures) plus live expense-ratio check."""
    from .. import ira_data
    rows = []
    for sym, ref in ira_data.REFERENCE.items():
        live = ira_data.fetch_live_expense_ratio(sym)
        rows.append({"symbol": sym, **{k: ref[k] for k in ("name", "category", "expense_ratio", "exposure")},
                     "alt": ref.get("alt"), "live_expense_ratio": live})
    return {"as_of": ira_data.REFERENCE_AS_OF, "funds": rows}


class BacktestBody(BaseModel):
    cadence: str = "weekly"
    threshold: float = 0.05
    cost_bps: float = 3.0
    extended: bool = True        # splice ETFs to long-history proxies (back to ~1999)
    sensitivity: bool = True     # run the parameter grid (adds ~20s)


_backtest_cache: dict = {}


@router.post("/backtest")
def ira_backtest_run(body: BacktestBody, current_user: dict = Depends(get_current_user)):
    import time
    from .. import ira_backtest
    if body.cadence not in ("weekly", "monthly"):
        raise HTTPException(400, "cadence must be weekly or monthly")
    if not (0.01 <= body.threshold <= 0.25) or not (0 <= body.cost_bps <= 50):
        raise HTTPException(400, "threshold must be 0.01-0.25 and cost_bps 0-50")
    key = (body.cadence, round(body.threshold, 4), round(body.cost_bps, 2), body.extended, body.sensitivity)
    hit = _backtest_cache.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    try:
        res = ira_backtest.run_backtest(cadence=body.cadence, threshold=body.threshold, cost_bps=body.cost_bps,
                                        extended=body.extended, sensitivity=body.sensitivity)
    except Exception as exc:
        logger.exception("ira backtest failed")
        raise HTTPException(502, f"Backtest failed: {str(exc)[:200]}")
    _backtest_cache[key] = (time.time(), res)
    return res


# ── Contributions ledger and stress tests ─────────────────────────────────────

from datetime import date as _date


class ContribBody(BaseModel):
    amount: float
    contributed_on: _date | None = None
    tax_year: int | None = None
    note: str = ""


@router.get("/contributions")
def ira_contributions(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    from .. import ira_contributions as ic
    return ic.summary(db, current_user["id"])


@router.post("/contributions")
def ira_add_contribution(body: ContribBody, current_user: dict = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    from .. import ira_contributions as ic
    uid = current_user["id"]
    on = body.contributed_on or _date.today()
    try:
        ic.add_contribution(db, uid, body.amount, on, body.tax_year or on.year, body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    summary = ic.summary(db, uid)
    over = [y for y in summary["years"] if y["excess"] > 0]
    if over:
        ira._alert("IRA contribution limit exceeded for " + ", ".join(
            f"{y['year']} (by ${y['excess']:,.2f})" for y in over) + ". Excess is taxed 6% per year until withdrawn.",
            "URGENT")
    return summary


@router.delete("/contributions/{cid}")
def ira_delete_contribution(cid: int, current_user: dict = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    from .. import ira_contributions as ic
    if not ic.delete_contribution(db, current_user["id"], cid):
        raise HTTPException(404, "contribution_not_found")
    return ic.summary(db, current_user["id"])


@router.get("/stress/historical")
def ira_stress_historical(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Replay real 1996+ return sequences against the plan (needs market data; cached 24h)."""
    from .. import ira_planner, ira_stress
    uid = current_user["id"]
    balance = 0.0
    if ira.has_credentials(db, uid):
        try:
            balance = ira.get_snapshot(db, uid)["equity"]
        except Exception as exc:
            logger.warning("ira stress: snapshot failed: %s", exc)
    plan = ira_planner.build_plan(db, uid, balance)
    if not plan.get("ready") or not plan.get("stress"):
        raise HTTPException(400, "plan_not_ready: set birth year, retirement age and a goal in Settings first.")
    return ira_stress.historical_replay(
        plan["glidepath_equity"], plan["profile"]["years_to_retire"],
        plan["stress"]["contribution_assumed"], balance, plan["target"]["amount"])
