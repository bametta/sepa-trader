"""
Savings tracker routes: every account across providers in one place (manual-first, no credentials).

GET    /api/savings/summary                      totals, breakdowns, warnings (+ daily snapshot)
GET    /api/savings/plan                         combined retirement projection
GET    /api/savings/history                      daily total history
POST   /api/savings/accounts                     create an account
PATCH  /api/savings/accounts/{id}                edit an account
DELETE /api/savings/accounts/{id}                delete an account and its holdings
GET    /api/savings/accounts/{id}/holdings.csv   holdings as editable CSV
PUT    /api/savings/accounts/{id}/holdings       replace holdings from CSV text (all-or-nothing)
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, get_current_user
from .. import savings, savings_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/savings", tags=["savings"])


class AccountBody(BaseModel):
    provider: str
    name: str
    account_type: str
    annual_contribution: float = 0.0
    contribution_source: str = "employee"      # "employee" or "employer" (e.g. the match account)
    max_deferral: bool = False                 # your own 401(k): follow the IRS deferral limit each year
    retirement: bool = True
    notes: str = ""


class AccountPatch(BaseModel):
    provider: str | None = None
    name: str | None = None
    account_type: str | None = None
    annual_contribution: float | None = None
    contribution_source: str | None = None
    max_deferral: bool | None = None
    retirement: bool | None = None
    notes: str | None = None


class HoldingsBody(BaseModel):
    csv: str


@router.get("/summary")
def summary(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return savings_service.get_state(db, current_user["id"])


@router.get("/plan")
def plan(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = current_user["id"]
    return savings_service.combined_plan(db, uid, savings_service.get_state(db, uid, record=False))


@router.get("/changes")
def changes(days: int = 180, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """What changed between daily snapshots: buys, sells, new/removed holdings, manual value updates."""
    return savings_service.changes(db, current_user["id"], max(7, min(730, days)))


@router.get("/history")
def history(limit: int = 400, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return savings.load_history(db, current_user["id"], max(1, min(2000, limit)))


@router.post("/accounts")
def create_account(body: AccountBody, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        new_id = savings.create_account(db, current_user["id"], body.provider, body.name, body.account_type,
                                        body.annual_contribution, body.retirement, body.notes, body.contribution_source,
                                        body.max_deferral)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": new_id}


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, body: AccountPatch, current_user: dict = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    try:
        ok = savings.update_account(db, current_user["id"], account_id, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, "account_not_found")
    return {"status": "updated"}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if not savings.delete_account(db, current_user["id"], account_id):
        raise HTTPException(404, "account_not_found")
    return {"status": "deleted"}


@router.get("/accounts/{account_id}/holdings.csv")
def holdings_csv(account_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    for a in savings.load_accounts(db, current_user["id"]):
        if a["id"] == account_id:
            return {"csv": savings.holdings_to_csv(a["holdings"]), "columns": list(savings.CSV_COLUMNS),
                    "asset_classes": list(savings.CLASSES)}
    raise HTTPException(404, "account_not_found")


@router.put("/accounts/{account_id}/holdings")
def put_holdings(account_id: int, body: HoldingsBody, current_user: dict = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    try:
        holdings = savings.parse_holdings_csv(body.csv)
        n = savings.replace_holdings(db, current_user["id"], account_id, holdings)
    except ValueError as exc:
        if str(exc) == "account_not_found":
            raise HTTPException(404, "account_not_found")
        raise HTTPException(400, str(exc))
    return {"status": "saved", "holdings": n}


# ── Import a positions export; manual-trade helper ────────────────────────────

class ImportBody(BaseModel):
    text: str
    apply: bool = False        # false = preview only (nothing saved)
    account_key: str | None = None   # which account to import when the file holds several


class TradePlanBody(BaseModel):
    account_ids: list[int] | None = None
    mode: str = "cash_only"    # "cash_only" (buys only) or "full" (also sells)
    target: str | None = None  # e.g. "VTI:50, VXUS:25, BND:25"; blank = saved model, else age-based default


@router.post("/accounts/{account_id}/import")
def import_positions(account_id: int, body: ImportBody, current_user: dict = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Parse a Schwab positions export (or this app's CSV). Preview shows what changed; apply saves it."""
    if len(body.text) > savings.MAX_CSV_CHARS:
        raise HTTPException(400, "file is too large")
    try:
        return savings_service.import_positions(db, current_user["id"], account_id, body.text, body.apply,
                                                body.account_key)
    except ValueError as exc:
        if str(exc) == "account_not_found":
            raise HTTPException(404, "account_not_found")
        raise HTTPException(400, str(exc))


@router.get("/target-model")
def target_model(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return savings_service.target_info(db, current_user["id"])


@router.post("/trade-plan")
def trade_plan(body: TradePlanBody, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """A worksheet of whole-share buys/sells per account. It places nothing."""
    try:
        return savings_service.trade_plan(db, current_user["id"], body.account_ids, body.mode, body.target)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
