"""
Claude AI analyst — post-close evaluation and on-demand weekly pick review.
"""
import logging
from sqlalchemy import text
from sqlalchemy.orm import Session
from .database import get_setting

logger = logging.getLogger(__name__)


def _client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def analyze_picks(db: Session, picks: list[dict], closed_position: dict | None = None) -> str:
    """
    Send picks + optional closed-position context to Claude.
    Returns the analysis text. Raises ValueError if no API key is set.
    """
    api_key = get_setting(db, "claude_api_key", "")
    if not api_key:
        raise ValueError("Claude API key not configured in Settings.")

    model = get_setting(db, "claude_model", "claude-sonnet-4-5")

    parts = []
    if closed_position:
        parts.append(
            f"A position was just closed:\n"
            f"  Symbol: {closed_position['symbol']}\n"
            f"  Entry:  ${closed_position.get('entry_price') or 'N/A'}\n"
            f"  Reason: {closed_position.get('reason', 'position closed')}"
        )

    lines = []
    for i, p in enumerate(picks, 1):
        ep = p.get("entry_price") or 0
        sp = p.get("stop_price") or 0
        t1 = p.get("target1") or 0
        rr = round((t1 - ep) / (ep - sp), 2) if ep > sp > 0 and t1 > ep else "N/A"
        lines.append(
            f"{i}. {p['symbol']}  score={p.get('score','?')}/6  signal={p.get('signal','?')}"
            f"  entry=${ep:.2f}  stop=${sp:.2f}  t1=${t1:.2f}  R:R={rr}"
            f"  status={p.get('status','?')}  note: {p.get('rationale','')}"
        )
    parts.append("Current week's top picks:\n" + "\n".join(lines))

    parts.append(
        "You are a professional swing-trader assistant using Minervini SEPA criteria.\n"
        "For each PENDING pick above give a one-line recommendation: EXECUTE, WAIT, or SKIP "
        "with a brief reason (≤15 words). Consider score, R:R ratio, and signal quality.\n"
        "Output a numbered list only — no preamble."
    )

    resp = _client(api_key).messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
    )
    return resp.content[0].text


def log_analysis(db: Session, trigger: str, symbol: str | None, analysis_text: str, mode: str):
    db.execute(
        text("""
            INSERT INTO ai_analysis_log (trigger, symbol, analysis, mode)
            VALUES (:trigger, :symbol, :analysis, :mode)
        """),
        {"trigger": trigger, "symbol": symbol, "analysis": analysis_text, "mode": mode},
    )
    db.commit()


def get_latest_analyses(db: Session, limit: int = 20, mode: str | None = None) -> list[dict]:
    """Return recent analyses. If mode is provided, filters to that mode only."""
    if mode:
        rows = db.execute(
            text("""
                SELECT id, trigger, symbol, analysis, mode, created_at
                FROM ai_analysis_log
                WHERE mode = :mode
                ORDER BY created_at DESC
                LIMIT :l
            """),
            {"l": limit, "mode": mode},
        ).fetchall()
    else:
        rows = db.execute(
            text("""
                SELECT id, trigger, symbol, analysis, mode, created_at
                FROM ai_analysis_log
                ORDER BY created_at DESC
                LIMIT :l
            """),
            {"l": limit},
        ).fetchall()
    return [dict(r._mapping) for r in rows]