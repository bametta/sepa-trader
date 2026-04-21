from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db, require_admin, get_setting
from ..auth import hash_password
import secrets

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── User management ───────────────────────────────────────────────────────────

@router.get("/users")
def list_users(
    _: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
            SELECT id, email, username, role, is_active, totp_enabled, created_at, last_login
            FROM users
            ORDER BY created_at ASC
        """)
    ).fetchall()
    return [
        {
            "id":           r[0],
            "email":        r[1],
            "username":     r[2],
            "role":         r[3],
            "is_active":    r[4],
            "totp_enabled": r[5],
            "created_at":   str(r[6]),
            "last_login":   str(r[7]) if r[7] else None,
        }
        for r in rows
    ]


class UserUpdate(BaseModel):
    role:      str | None = None
    is_active: bool | None = None


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdate,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if body.role and body.role not in ("user", "admin"):
        raise HTTPException(400, "role must be 'user' or 'admin'")

    if body.role is not None:
        # Prevent removing the last admin
        if body.role != "admin":
            admin_count = db.execute(
                text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = true")
            ).scalar()
            if admin_count <= 1 and current_admin["id"] == user_id:
                raise HTTPException(400, "Cannot demote the only active admin")
        db.execute(
            text("UPDATE users SET role = :r WHERE id = :id"),
            {"r": body.role, "id": user_id},
        )

    if body.is_active is not None:
        db.execute(
            text("UPDATE users SET is_active = :a WHERE id = :id"),
            {"a": body.is_active, "id": user_id},
        )

    db.commit()
    return {"status": "updated", "user_id": user_id}


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    _: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Generate a temporary password for a user. Show it once — user must change it."""
    temp_password = secrets.token_urlsafe(10)
    db.execute(
        text("UPDATE users SET password_hash = :pw WHERE id = :id"),
        {"pw": hash_password(temp_password), "id": user_id},
    )
    db.commit()
    return {"temp_password": temp_password}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user_id == current_admin["id"]:
        raise HTTPException(400, "Cannot delete your own account")

    # Reassign their data to the admin before deletion
    admin_id = current_admin["id"]
    for table in ("weekly_plan", "trade_log", "signal_log", "ai_analysis_log"):
        db.execute(
            text(f"UPDATE {table} SET user_id = :admin WHERE user_id = :uid"),
            {"admin": admin_id, "uid": user_id},
        )

    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    db.commit()
    return {"status": "deleted", "user_id": user_id}


# ── App health ────────────────────────────────────────────────────────────────

@router.get("/health")
def app_health(
    _: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from ..scheduler import scheduler

    # DB check
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        db_ok = False

    # Scheduler jobs
    jobs = [
        {
            "id":       j.id,
            "next_run": str(j.next_run_time) if j.next_run_time else None,
        }
        for j in scheduler.get_jobs()
    ]

    # Recent trade activity
    recent_trades = db.execute(
        text("""
            SELECT COUNT(*) FROM trade_log
            WHERE created_at > NOW() - INTERVAL '7 days'
        """)
    ).scalar()

    user_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()

    screener_status = get_setting(db, "screener_status", "idle")
    last_monitor    = get_setting(db, "screener_last_auto_run", "never")

    return {
        "db":               "ok" if db_ok else "error",
        "scheduler_running": scheduler.running,
        "jobs":             jobs,
        "user_count":       user_count,
        "trades_last_7d":   recent_trades,
        "screener_status":  screener_status,
        "last_screener_run": last_monitor,
    }
