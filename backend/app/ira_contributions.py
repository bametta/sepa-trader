"""
Roth IRA contribution ledger and limit tracking.

Manual ledger (the app can't verify how Alpaca reports IRA deposits until the account has
activity). Tracks each tax year against the IRS limit, including the age-50 catch-up and an
income phase-out estimate. Prior-year contributions are allowed until April 15.

IRS figures are from memory and change yearly: verify at irs.gov before relying on them.
Phase-out uses MAGI, which is not the same as the 'annual income' setting.
"""
import math
from datetime import date, datetime

from sqlalchemy import text

from .database import get_user_setting

# tax year -> (base limit, age-50 catch-up)
LIMITS = {2025: (7000.0, 1000.0), 2026: (7500.0, 1100.0)}
# tax year -> filing status -> (phase-out start, phase-out end) of MAGI
PHASEOUT = {2025: {"single": (150000.0, 165000.0), "joint": (236000.0, 246000.0)},
            2026: {"single": (153000.0, 168000.0), "joint": (242000.0, 252000.0)}}
DEADLINE_MONTH_DAY = (4, 15)
EXCISE_RATE = 0.06
VERIFY_NOTE = "IRS limits and income phase-outs are approximate; confirm current figures at irs.gov."


def _known(table: dict, year: int):
    """Table entry for `year`, falling back to the latest known year (flagged as an estimate)."""
    if year in table:
        return table[year], False
    return table[max(table)], True


def limit_for(year: int, birth_year: int | None) -> dict:
    (base, catch), estimated = _known(LIMITS, year)
    age_end = (year - birth_year) if birth_year else None
    catch_ok = age_end is not None and age_end >= 50
    return {"year": year, "base": base, "catch_up": catch if catch_ok else 0.0,
            "limit": base + (catch if catch_ok else 0.0), "estimated": estimated,
            "catch_up_eligible": catch_ok}


def phaseout_limit(limit: float, magi: float | None, status: str | None, year: int) -> tuple[float, str | None]:
    """Reduced Roth limit for a given MAGI. Returns (allowed, note)."""
    if not magi or status not in ("single", "joint"):
        return limit, None
    rng, _ = _known(PHASEOUT, year)
    lo, hi = rng[status]
    if magi <= lo:
        return limit, None
    if magi >= hi:
        return 0.0, f"MAGI ${magi:,.0f} is above the ${hi:,.0f} phase-out end: direct Roth contributions not allowed."
    reduced = limit * (hi - magi) / (hi - lo)
    reduced = math.ceil(reduced / 10.0) * 10.0          # IRS rounds up to the next $10
    reduced = max(reduced, 200.0) if reduced > 0 else 0.0  # $200 floor while still in range
    return min(limit, reduced), f"MAGI ${magi:,.0f} is in the ${lo:,.0f}-${hi:,.0f} phase-out: limit reduced to ${reduced:,.0f}."


def contribution_deadline(year: int) -> date:
    return date(year + 1, *DEADLINE_MONTH_DAY)


def open_tax_years(today: date) -> list[int]:
    """Tax years that can still receive contributions: this year, plus last year until April 15."""
    years = [today.year]
    if today <= date(today.year, *DEADLINE_MONTH_DAY):
        years.append(today.year - 1)
    return sorted(years, reverse=True)


def summarize_year(year: int, today: date, birth_year: int | None, magi: float | None,
                   status: str | None, contributed: float) -> dict:
    lim = limit_for(year, birth_year)
    allowed, phase_note = phaseout_limit(lim["limit"], magi, status, year)
    remaining = max(0.0, allowed - contributed)
    excess = max(0.0, contributed - allowed)
    deadline = contribution_deadline(year)               # April 15 of the following year
    days_to_deadline = (deadline - today).days
    months_to_deadline = max(1, math.ceil(days_to_deadline / 30.44)) if days_to_deadline > 0 else 0
    year_end = date(year, 12, 31)
    months_to_year_end = max(1, math.ceil((year_end - today).days / 30.44)) if today < year_end else 0
    out = {
        **lim, "allowed": allowed, "phase_out_note": phase_note, "contributed": round(contributed, 2),
        "remaining": round(remaining, 2), "excess": round(excess, 2),
        "excess_tax_estimate": round(excess * EXCISE_RATE, 2),
        "deadline": deadline.isoformat(), "days_to_deadline": days_to_deadline,
        "monthly_to_fill_by_year_end": (round(remaining / months_to_year_end, 2)
                                        if remaining and months_to_year_end else None),
        "monthly_to_fill_by_deadline": (round(remaining / months_to_deadline, 2)
                                        if remaining and months_to_deadline else None),
    }
    if year == today.year:
        elapsed = min(1.0, ((today - date(year, 1, 1)).days + 1) / 365.0)
        expected = allowed * elapsed
        out["expected_by_today"] = round(expected, 2)
        out["behind_by"] = round(max(0.0, expected - contributed), 2)
        out["pace"] = "maxed" if remaining <= 0 else ("on_pace" if contributed + 0.5 >= expected else "behind")
    if excess > 0:
        out["warning"] = (f"Contributions exceed the limit by ${excess:,.2f}. Excess is taxed 6% per year until "
                          f"withdrawn (about ${excess * EXCISE_RATE:,.2f}/yr). Withdraw the excess and its "
                          "earnings before the filing deadline and talk to a tax professional.")
    return out


def reminder_text(summary: dict, today: date) -> str | None:
    """Monthly 'are you on track to max the IRA?' message, or None when nothing is open."""
    open_years = set(open_tax_years(today))
    lines = []
    for y in summary["years"]:
        if y["year"] not in open_years or y["remaining"] <= 0:
            continue
        if y["year"] == today.year and y.get("monthly_to_fill_by_year_end"):
            when = f"about ${y['monthly_to_fill_by_year_end']:,.0f}/month fills it by Dec 31"
        else:
            when = f"deadline {y['deadline']}"
        status = {"behind": " (behind pace)", "on_pace": " (on pace)"}.get(y.get("pace"), "")
        lines.append(f"{y['year']}: ${y['contributed']:,.0f} of ${y['allowed']:,.0f} contributed, "
                     f"${y['remaining']:,.0f} left, {when}{status}.")
    return ("IRA contribution check:\n" + "\n".join(lines)) if lines else None


# ── Ledger (database) ─────────────────────────────────────────────────────────

def validate(amount, contributed_on: date, tax_year: int, today: date) -> None:
    if not (0 < amount <= 50000):
        raise ValueError("amount must be between $0.01 and $50,000")
    if contributed_on > today:
        raise ValueError("contribution date cannot be in the future")
    if tax_year not in (contributed_on.year, contributed_on.year - 1):
        raise ValueError("tax year must be the contribution year or the prior year")
    if tax_year == contributed_on.year - 1 and contributed_on > date(contributed_on.year, *DEADLINE_MONTH_DAY):
        raise ValueError("prior-year contributions must be made by April 15")


def add_contribution(db, uid: int, amount: float, contributed_on: date, tax_year: int,
                     note: str = "", today: date | None = None) -> int:
    validate(amount, contributed_on, tax_year, today or date.today())
    row = db.execute(
        text("""INSERT INTO ira_contributions (user_id, tax_year, amount, contributed_on, note)
                VALUES (:u, :y, :a, :d, :n) RETURNING id"""),
        {"u": uid, "y": tax_year, "a": round(float(amount), 2), "d": contributed_on, "n": (note or "")[:200]},
    ).scalar()
    db.commit()
    return row


def delete_contribution(db, uid: int, cid: int) -> bool:
    n = db.execute(text("DELETE FROM ira_contributions WHERE id = :id AND user_id = :u"),
                   {"id": cid, "u": uid}).rowcount
    db.commit()
    return bool(n)


def list_contributions(db, uid: int) -> list[dict]:
    rows = db.execute(
        text("""SELECT id, tax_year, amount, contributed_on, note FROM ira_contributions
                WHERE user_id = :u ORDER BY contributed_on DESC, id DESC"""), {"u": uid}).fetchall()
    return [{"id": r[0], "tax_year": r[1], "amount": float(r[2]), "contributed_on": r[3].isoformat(), "note": r[4] or ""}
            for r in rows]


def summary(db, uid: int, today: date | None = None) -> dict:
    today = today or date.today()
    g = lambda k: get_user_setting(db, k, "", uid)

    def num(v):
        try:
            x = float(v)
            return x if x > 0 else None
        except (TypeError, ValueError):
            return None

    birth = num(g("ira_birth_year"))
    magi = num(g("ira_magi")) or num(g("ira_annual_income"))
    status = (g("ira_filing_status") or "").lower() or None
    ledger = list_contributions(db, uid)
    by_year: dict[int, float] = {}
    for c in ledger:
        by_year[c["tax_year"]] = by_year.get(c["tax_year"], 0.0) + c["amount"]
    years = sorted(set(open_tax_years(today)) | set(by_year), reverse=True)
    return {
        "today": today.isoformat(),
        "max_every_year": (g("ira_contribute_max") or "").lower() == "true",
        "years": [summarize_year(y, today, int(birth) if birth else None, magi, status, by_year.get(y, 0.0))
                  for y in years],
        "ledger": ledger,
        "inputs": {"birth_year": int(birth) if birth else None, "magi_used": magi, "filing_status": status,
                   "magi_note": "Uses 'ira_magi' if set, otherwise 'annual income' as a proxy for MAGI."},
        "note": VERIFY_NOTE,
    }
