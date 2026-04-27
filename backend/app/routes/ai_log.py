from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db, get_current_user, get_user_setting

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/pre-trade-log")
def pre_trade_log(
    limit: int = 100,
    symbol: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Recent pre-trade AI gate verdicts, scoped to the user's active mode.

    The `analysis` field contains the full verbatim AI response (VERDICT/
    REASON/WARNINGS plus any tape/news/sizing context that was prepended).
    """
    mode = get_user_setting(db, "trading_mode", "paper", current_user["id"])
    params: dict = {
        "uid": current_user["id"],
        "mode": mode,
        "l": min(max(limit, 1), 500),
    }
    where_extra = ""
    if symbol:
        where_extra = " AND symbol = :sym"
        params["sym"] = symbol.upper()

    # Show rows scoped to this user OR with user_id=NULL (legacy/unscoped
    # writes from internal callers — Monday open, slot refill, post-close).
    # The mode filter still prevents cross-mode bleed.
    rows = db.execute(
        text(f"""
            SELECT id, trigger, symbol, analysis, mode, created_at
            FROM ai_analysis_log
            WHERE (user_id = :uid OR user_id IS NULL)
              AND mode = :mode
              AND trigger LIKE 'pre_trade_%'
              {where_extra}
            ORDER BY created_at DESC
            LIMIT :l
        """),
        params,
    ).fetchall()

    out = []
    for r in rows:
        analysis = r[3] or ""
        verdict = ""
        reason = ""
        for line in analysis.splitlines():
            if line.startswith("VERDICT:") and not verdict:
                verdict = line.split(":", 1)[1].strip()
            elif line.startswith("REASON:") and not reason:
                reason = line.split(":", 1)[1].strip()
            if verdict and reason:
                break
        out.append({
            "id":         r[0],
            "trigger":    r[1],
            "symbol":     r[2],
            "verdict":    verdict,
            "reason":     reason,
            "analysis":   analysis,
            "mode":       r[4],
            "created_at": str(r[5]),
        })
    return out
