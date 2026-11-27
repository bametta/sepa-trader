"""
Savings tracker: one place for every account across providers (401k, IRA, self-directed IRA,
taxable...). Manual-first and credential-free: holdings are entered or pasted as CSV. A holding is
either a ticker with a quantity (priced live) or a manual value with an as-of date (core funds,
real estate, notes). Pure functions do the valuation and aggregation; a thin DB layer persists.

Planning aid only, not financial advice.
"""
import csv
import io
from datetime import date, timedelta

from sqlalchemy import text

from .ira_data import EQUITY_BUCKETS, REFERENCE

ACCOUNT_TYPES = {
    "401k": "pre_tax", "roth_401k": "roth", "ira": "pre_tax", "roth_ira": "roth",
    "sdira": "pre_tax", "roth_sdira": "roth", "hsa": "hsa", "taxable": "taxable", "other": "other",
}
CLASSES = ("us_large", "us_midsmall", "intl_dev", "intl_em", "reit", "bonds_agg", "bonds_tips", "gold", "cash",
           "company_stock", "crypto", "alt_real_estate", "alt_private", "alt_other", "other")
EQUITY_CLASSES = set(EQUITY_BUCKETS) | {"company_stock"}
ILLIQUID_DEFAULT = {"alt_real_estate", "alt_private", "alt_other"}

STALE_DAYS = {"liquid": 90, "illiquid": 180}
LIMITS = {"position_pct": 0.25, "company_stock_pct": 0.10, "illiquid_pct": 0.25}
MAX_HOLDINGS = 500
MAX_CSV_CHARS = 200_000
CSV_COLUMNS = ("symbol", "name", "quantity", "value", "asset_class", "illiquid", "as_of")


# ── Parsing / validation ──────────────────────────────────────────────────────

def _num(raw, field: str, line: int) -> float | None:
    raw = (raw or "").strip().replace(",", "").replace("$", "")
    if raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"line {line}: {field} '{raw}' is not a number")
    if v < 0 or v > 1e10:
        raise ValueError(f"line {line}: {field} must be between 0 and 10,000,000,000")
    return v


def parse_holdings_csv(raw: str, today: date | None = None) -> list[dict]:
    """CSV with a header row using any of: symbol,name,quantity,value,asset_class,illiquid,as_of.
    All-or-nothing: raises ValueError listing every problem."""
    today = today or date.today()
    if len(raw) > MAX_CSV_CHARS:
        raise ValueError("CSV is too large")
    rows = [r for r in csv.reader(io.StringIO(raw.strip())) if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    unknown = [c for c in header if c not in CSV_COLUMNS]
    if unknown:
        raise ValueError(f"unknown column(s): {', '.join(unknown)}. Allowed: {', '.join(CSV_COLUMNS)}")
    if "symbol" not in header and "name" not in header:
        raise ValueError("the header must include 'symbol' or 'name'")
    if len(rows) - 1 > MAX_HOLDINGS:
        raise ValueError(f"too many holdings (max {MAX_HOLDINGS})")
    out, errors = [], []
    for i, row in enumerate(rows[1:], start=2):
        cell = {h: (row[j].strip() if j < len(row) else "") for j, h in enumerate(header)}
        try:
            symbol = cell.get("symbol", "").upper() or None
            name = cell.get("name", "") or symbol
            if not name:
                raise ValueError(f"line {i}: needs a symbol or a name")
            qty, val = _num(cell.get("quantity"), "quantity", i), _num(cell.get("value"), "value", i)
            if qty is None and val is None:
                raise ValueError(f"line {i}: needs a quantity or a value")
            if qty is not None and not symbol:
                raise ValueError(f"line {i}: a quantity needs a symbol to price it (or use value)")
            cls = (cell.get("asset_class") or "auto").lower()
            if cls != "auto" and cls not in CLASSES:
                raise ValueError(f"line {i}: asset_class '{cls}' must be 'auto' or one of {', '.join(CLASSES)}")
            ill_raw = (cell.get("illiquid") or "").lower()
            illiquid = ill_raw in ("1", "true", "yes", "y") or (not ill_raw and cls in ILLIQUID_DEFAULT)
            as_of = date.fromisoformat(cell["as_of"]) if cell.get("as_of") else today
            if as_of > today:
                raise ValueError(f"line {i}: as_of cannot be in the future")
            out.append({"symbol": symbol, "name": name[:120], "quantity": qty, "manual_value": val,
                        "asset_class": cls, "illiquid": illiquid, "as_of": as_of})
        except ValueError as exc:
            errors.append(str(exc) if str(exc).startswith("line") else f"line {i}: {exc}")
    if errors:
        raise ValueError("; ".join(errors[:12]) + (f" (+{len(errors) - 12} more)" if len(errors) > 12 else ""))
    return out


def holdings_to_csv(holdings: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for h in holdings:
        w.writerow([h.get("symbol") or "", h["name"], "" if h.get("quantity") is None else h["quantity"],
                    "" if h.get("manual_value") is None else h["manual_value"], h.get("asset_class", "auto"),
                    "true" if h.get("illiquid") else "", h["as_of"].isoformat() if h.get("as_of") else ""])
    return buf.getvalue()


CONTRIBUTION_SOURCES = ("employee", "employer")


def validate_account(provider: str, name: str, account_type: str, annual_contribution,
                     contribution_source="employee") -> dict:
    provider, name = (provider or "").strip(), (name or "").strip()
    if not (1 <= len(provider) <= 60) or not (1 <= len(name) <= 100):
        raise ValueError("provider (1-60 chars) and name (1-100 chars) are required")
    if account_type not in ACCOUNT_TYPES:
        raise ValueError(f"account_type must be one of: {', '.join(ACCOUNT_TYPES)}")
    if contribution_source not in CONTRIBUTION_SOURCES:
        raise ValueError(f"contribution_source must be one of: {', '.join(CONTRIBUTION_SOURCES)}")
    try:
        contrib = float(annual_contribution or 0)
    except (TypeError, ValueError):
        raise ValueError("annual_contribution must be a number")
    if not (0 <= contrib <= 10_000_000):
        raise ValueError("annual_contribution must be between 0 and 10,000,000")
    return {"provider": provider, "name": name, "account_type": account_type, "annual_contribution": contrib,
            "contribution_source": contribution_source}


# 401(k) employee elective-deferral limits (from memory; verify at irs.gov each year):
# year -> (base, catch-up at 50+, catch-up at 60-63 which replaces the 50+ amount)
DEFERRAL_LIMITS = {2025: (23500.0, 7500.0, 11250.0), 2026: (24500.0, 8000.0, 11250.0)}
K401_TYPES = ("401k", "roth_401k")


def deferral_limit(year: int, birth_year: int | None) -> dict:
    base, catch, super_catch = DEFERRAL_LIMITS.get(year) or DEFERRAL_LIMITS[max(DEFERRAL_LIMITS)]
    age = (year - birth_year) if birth_year else None
    if age is not None and 60 <= age <= 63:
        add, label = super_catch, "catch-up for ages 60-63"
    elif age is not None and age >= 50:
        add, label = catch, "catch-up for age 50+"
    else:
        add, label = 0.0, None
    return {"year": year, "limit": base + add, "catch_up": add, "catch_up_label": label,
            "estimated": year not in DEFERRAL_LIMITS}


def plan_deferrals(accounts: list[dict], year: int, birth_year: int | None) -> list[dict]:
    """Per provider (= per plan): your own contributions across its 401(k)/Roth 401(k) accounts against the
    employee deferral limit, which pre-tax and Roth deferrals share. Employer-funded accounts (the match)
    don't count toward that limit."""
    plans: dict[str, dict] = {}
    for a in accounts:
        if a["account_type"] not in K401_TYPES or a["annual_contribution"] <= 0:
            continue
        p = plans.setdefault(a["provider"], {"employee": 0.0, "employer": 0.0})
        p["employer" if a.get("contribution_source") == "employer" else "employee"] += a["annual_contribution"]
    out = []
    for provider, p in plans.items():
        lim = deferral_limit(year, birth_year)
        out.append({"provider": provider, **lim, "employee_planned": round(p["employee"], 2),
                    "employer_planned": round(p["employer"], 2),
                    "over_by": round(max(0.0, p["employee"] - lim["limit"]), 2),
                    "room_left": round(max(0.0, lim["limit"] - p["employee"]), 2)})
    return out


# ── Valuation / aggregation (pure) ────────────────────────────────────────────

def value_holding(h: dict, price: float | None) -> tuple[float, str]:
    if h.get("quantity") is not None and price:
        return float(h["quantity"]) * float(price), "live"
    if h.get("manual_value") is not None:
        return float(h["manual_value"]), "manual"
    return 0.0, "missing"


def exposure(h: dict) -> dict:
    cls = h.get("asset_class") or "auto"
    if cls != "auto":
        return {cls: 1.0}
    ref = REFERENCE.get((h.get("symbol") or "").upper())
    return dict(ref["exposure"]) if ref else {"unclassified": 1.0}


def is_single_name(h: dict) -> bool:
    """True for holdings where concentration is a real risk (a single company, coin or unknown ticker),
    not for diversified funds. Heuristic: known ETFs and 5-letter mutual-fund tickers ending in X are funds;
    manual core funds with an explicit class are funds; employer stock and crypto are single names."""
    cls = h.get("asset_class") or "auto"
    if cls in ("company_stock", "crypto"):
        return True
    sym = (h.get("symbol") or "").upper()
    if cls != "auto" or not sym:
        return False
    if sym in REFERENCE or (len(sym) == 5 and sym.endswith("X")):
        return False
    return True


def tax_bucket(account_type: str) -> str:
    return ACCOUNT_TYPES.get(account_type, "other")


def _add(d: dict, k, v):
    d[k] = d.get(k, 0.0) + v


def build_summary(accounts: list[dict], prices: dict, today: date | None = None,
                  birth_year: int | None = None) -> dict:
    """`accounts`: [{id, provider, name, account_type, retirement, annual_contribution, contribution_source,
    read_only, holdings: [...]}]. `prices`: {SYMBOL: price}. Returns totals, breakdowns and warnings."""
    today = today or date.today()
    total = retirement_total = illiquid_total = 0.0
    by_provider, by_tax, by_class, positions = {}, {}, {}, {}
    out_accounts, warnings = [], []
    stale, missing, unclassified = [], [], 0.0

    for a in accounts:
        acct_value = 0.0
        rows = []
        for h in a["holdings"]:
            sym = (h.get("symbol") or "").upper() or None
            val, source = value_holding(h, prices.get(sym) if sym else None)
            exp = exposure(h)
            acct_value += val
            for cls, frac in exp.items():
                _add(by_class, cls, val * frac)
            if "unclassified" in exp:
                unclassified += val
            illiquid = bool(h.get("illiquid"))
            if illiquid:
                illiquid_total += val
            key = sym or h["name"]
            p = positions.setdefault(key, {"symbol": sym, "name": h["name"], "value": 0.0, "single": False})
            p["value"] += val
            p["single"] = p["single"] or is_single_name(h)
            as_of = h.get("as_of")
            age = (today - as_of).days if as_of else None
            is_stale = (source == "manual" and age is not None
                        and age > STALE_DAYS["illiquid" if illiquid else "liquid"])
            if is_stale:
                stale.append(f"{a['provider']} / {h['name']} (valued {as_of.isoformat()})")
            if source == "missing":
                missing.append(f"{a['provider']} / {h['name']}")
            rows.append({**{k: h.get(k) for k in ("id", "symbol", "name", "quantity", "manual_value", "asset_class",
                                                  "illiquid")},
                         "as_of": as_of.isoformat() if as_of else None, "value": round(val, 2), "source": source,
                         "price": prices.get(sym) if sym else None, "stale": is_stale})
        total += acct_value
        if a.get("retirement", True):
            retirement_total += acct_value
        _add(by_provider, a["provider"], acct_value)
        _add(by_tax, tax_bucket(a["account_type"]), acct_value)
        for r in rows:
            r["pct_of_account"] = round(r["value"] / acct_value, 4) if acct_value else 0.0
        contrib = float(a.get("annual_contribution") or 0)
        source = a.get("contribution_source") or "employee"
        out_accounts.append({
            "id": a["id"], "provider": a["provider"], "name": a["name"], "account_type": a["account_type"],
            "tax": tax_bucket(a["account_type"]), "retirement": a.get("retirement", True),
            "annual_contribution": contrib, "contribution_source": source, "read_only": bool(a.get("read_only")),
            "value": round(acct_value, 2), "holdings": sorted(rows, key=lambda r: -r["value"]),
        })

    deferrals = plan_deferrals(out_accounts, today.year, birth_year)
    for d in deferrals:
        if d["over_by"] > 0:
            warnings.append({"code": "DEFERRAL_OVER_LIMIT", "severity": "warn", "detail": (
                f"{d['provider']}: your planned contributions of ${d['employee_planned']:,.0f} are over the "
                f"{d['year']} employee deferral limit of ${d['limit']:,.0f} by ${d['over_by']:,.0f}. Excess "
                "deferrals are taxed twice unless corrected, so confirm with your plan. "
                "(Limits are from memory; verify them.)")})

    pct = lambda v: round(v / total, 4) if total else 0.0
    equity = sum(v for c, v in by_class.items() if c in EQUITY_CLASSES)
    top = sorted(positions.values(), key=lambda p: -p["value"])[:10]
    top = [{**p, "value": round(p["value"], 2), "pct": pct(p["value"])} for p in top]

    if not accounts:
        warnings.append({"code": "NO_ACCOUNTS", "severity": "info", "detail": "Add an account to start tracking."})
    if stale:
        warnings.append({"code": "STALE_VALUATION", "severity": "warn",
                         "detail": "Manual values are out of date: " + "; ".join(stale[:6])})
    if missing:
        warnings.append({"code": "MISSING_VALUE", "severity": "warn",
                         "detail": "No price or value for: " + "; ".join(missing[:6])})
    if unclassified > 0.005 * (total or 1):
        warnings.append({"code": "UNCLASSIFIED", "severity": "warn",
                         "detail": f"${unclassified:,.0f} is in holdings without an asset class, so allocation is understated."})
    for p in top[:3]:
        if p["pct"] > LIMITS["position_pct"] and p["single"]:
            warnings.append({"code": "CONCENTRATION", "severity": "warn",
                             "detail": f"{p['name']} is {p['pct']:.0%} of everything you track."})
    if pct(by_class.get("company_stock", 0.0)) > LIMITS["company_stock_pct"]:
        warnings.append({"code": "COMPANY_STOCK", "severity": "warn",
                         "detail": f"Employer stock is {pct(by_class['company_stock']):.0%} of your total."})
    if retirement_total and illiquid_total / retirement_total > LIMITS["illiquid_pct"]:
        warnings.append({"code": "ILLIQUID_HIGH", "severity": "warn",
                         "detail": f"Illiquid assets are {illiquid_total / retirement_total:.0%} of retirement savings."})

    return {
        "as_of": today.isoformat(), "total": round(total, 2), "retirement_total": round(retirement_total, 2),
        "accounts": out_accounts,
        "by_provider": {k: round(v, 2) for k, v in sorted(by_provider.items(), key=lambda kv: -kv[1])},
        "by_tax": {k: round(v, 2) for k, v in sorted(by_tax.items(), key=lambda kv: -kv[1])},
        "by_class": {k: round(v, 2) for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])},
        "equity_share": pct(equity), "illiquid_share": pct(illiquid_total),
        "positions": top, "warnings": warnings, "deferrals": deferrals,
    }


# ── Database ──────────────────────────────────────────────────────────────────

def _holding_row(r) -> dict:
    return {"id": r[0], "account_id": r[1], "symbol": r[2], "name": r[3], "asset_class": r[4],
            "quantity": float(r[5]) if r[5] is not None else None,
            "manual_value": float(r[6]) if r[6] is not None else None, "as_of": r[7], "illiquid": r[8]}


def load_accounts(db, uid: int) -> list[dict]:
    accts = db.execute(
        text("""SELECT id, provider, name, account_type, annual_contribution, retirement, notes, contribution_source
                FROM savings_accounts WHERE user_id = :u AND is_active ORDER BY provider, name"""),
        {"u": uid}).fetchall()
    ids = [a[0] for a in accts]
    holds: dict[int, list] = {i: [] for i in ids}
    if ids:
        for r in db.execute(
            text("""SELECT id, account_id, symbol, name, asset_class, quantity, manual_value, as_of, illiquid
                    FROM savings_holdings WHERE account_id = ANY(:ids) ORDER BY id"""), {"ids": ids}).fetchall():
            holds[r[1]].append(_holding_row(r))
    return [{"id": a[0], "provider": a[1], "name": a[2], "account_type": a[3],
             "annual_contribution": float(a[4] or 0), "retirement": bool(a[5]), "notes": a[6] or "",
             "contribution_source": a[7] or "employee", "holdings": holds[a[0]]} for a in accts]


def create_account(db, uid: int, provider, name, account_type, annual_contribution=0, retirement=True, notes="",
                   contribution_source="employee") -> int:
    v = validate_account(provider, name, account_type, annual_contribution, contribution_source)
    new_id = db.execute(
        text("""INSERT INTO savings_accounts
                (user_id, provider, name, account_type, annual_contribution, contribution_source, retirement, notes)
                VALUES (:u, :p, :n, :t, :c, :s, :r, :no) RETURNING id"""),
        {"u": uid, "p": v["provider"], "n": v["name"], "t": v["account_type"], "c": v["annual_contribution"],
         "s": v["contribution_source"], "r": bool(retirement), "no": (notes or "")[:500]}).scalar()
    db.commit()
    return new_id


def update_account(db, uid: int, account_id: int, **fields) -> bool:
    cur = db.execute(text("""SELECT provider, name, account_type, annual_contribution, retirement, notes,
                                    contribution_source
                             FROM savings_accounts WHERE id = :id AND user_id = :u AND is_active"""),
                     {"id": account_id, "u": uid}).fetchone()
    if not cur:
        return False
    merged = {"provider": cur[0], "name": cur[1], "account_type": cur[2], "annual_contribution": cur[3],
              "retirement": cur[4], "notes": cur[5] or "", "contribution_source": cur[6] or "employee"}
    merged.update({k: v for k, v in fields.items() if v is not None})
    v = validate_account(merged["provider"], merged["name"], merged["account_type"], merged["annual_contribution"],
                         merged["contribution_source"])
    db.execute(text("""UPDATE savings_accounts SET provider=:p, name=:n, account_type=:t, annual_contribution=:c,
                       contribution_source=:s, retirement=:r, notes=:no WHERE id = :id AND user_id = :u"""),
               {"p": v["provider"], "n": v["name"], "t": v["account_type"], "c": v["annual_contribution"],
                "s": v["contribution_source"], "r": bool(merged["retirement"]), "no": str(merged["notes"])[:500],
                "id": account_id, "u": uid})
    db.commit()
    return True


def delete_account(db, uid: int, account_id: int) -> bool:
    n = db.execute(text("DELETE FROM savings_accounts WHERE id = :id AND user_id = :u"),
                   {"id": account_id, "u": uid}).rowcount
    db.commit()
    return bool(n)


def owns_account(db, uid: int, account_id: int) -> bool:
    return db.execute(text("SELECT 1 FROM savings_accounts WHERE id = :id AND user_id = :u AND is_active"),
                      {"id": account_id, "u": uid}).fetchone() is not None


def replace_holdings(db, uid: int, account_id: int, holdings: list[dict]) -> int:
    """Atomically replace an account's holdings (all-or-nothing)."""
    if not owns_account(db, uid, account_id):
        raise ValueError("account_not_found")
    if len(holdings) > MAX_HOLDINGS:
        raise ValueError(f"too many holdings (max {MAX_HOLDINGS})")
    try:
        db.execute(text("DELETE FROM savings_holdings WHERE account_id = :a"), {"a": account_id})
        for h in holdings:
            db.execute(
                text("""INSERT INTO savings_holdings
                        (account_id, symbol, name, asset_class, quantity, manual_value, as_of, illiquid)
                        VALUES (:a, :s, :n, :c, :q, :v, :d, :i)"""),
                {"a": account_id, "s": h.get("symbol"), "n": h["name"], "c": h.get("asset_class", "auto"),
                 "q": h.get("quantity"), "v": h.get("manual_value"), "d": h.get("as_of") or date.today(),
                 "i": bool(h.get("illiquid"))})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return len(holdings)


def record_snapshot(db, uid: int, summary: dict) -> None:
    import json
    db.execute(
        text("""INSERT INTO savings_history (user_id, as_of, total, breakdown)
                VALUES (:u, :d, :t, CAST(:b AS jsonb))
                ON CONFLICT (user_id, as_of) DO UPDATE SET total = :t, breakdown = CAST(:b AS jsonb)"""),
        {"u": uid, "d": date.fromisoformat(summary["as_of"]), "t": summary["total"],
         "b": json.dumps({"by_provider": summary["by_provider"], "by_tax": summary["by_tax"],
                          "retirement_total": summary["retirement_total"]})})
    db.commit()


# ── Change tracking: dated per-account snapshots, diffed to show what you did ──

def _snapshot_holdings(account: dict) -> list[dict]:
    return [{"key": h.get("symbol") or h["name"], "symbol": h.get("symbol"), "name": h["name"],
             "quantity": h.get("quantity"), "value": h["value"]} for h in account["holdings"]]


def diff_snapshots(prev: list[dict], cur: list[dict], prev_value: float, cur_value: float) -> dict:
    """Compare two holdings snapshots of one account. Holdings priced by quantity show buys/sells;
    manual-value holdings show value updates. `market_and_other` is whatever trading doesn't explain."""
    pk, ck = {h["key"]: h for h in prev}, {h["key"]: h for h in cur}
    events, traded_net = [], 0.0
    for key, h in ck.items():
        p = pk.get(key)
        if p is None:
            kind = "bought" if h.get("quantity") is not None else "added"
            events.append({"type": "added", "key": key, "name": h["name"], "quantity": h.get("quantity"),
                           "value": round(h["value"], 2)})
            if kind == "bought":
                traded_net += h["value"]
            continue
        if h.get("quantity") is not None and p.get("quantity") is not None:
            dq = h["quantity"] - p["quantity"]
            if abs(dq) > 1e-6 * max(1.0, abs(p["quantity"])):
                price = (h["value"] / h["quantity"]) if h["quantity"] else (p["value"] / p["quantity"])
                value = abs(dq) * price
                events.append({"type": "bought" if dq > 0 else "sold", "key": key, "name": h["name"],
                               "quantity_change": round(dq, 6), "value": round(value, 2)})
                traded_net += value if dq > 0 else -value
        elif abs(h["value"] - p["value"]) >= 0.01 and (h.get("quantity") is None or p.get("quantity") is None):
            events.append({"type": "value_update", "key": key, "name": h["name"],
                           "from": round(p["value"], 2), "to": round(h["value"], 2),
                           "change": round(h["value"] - p["value"], 2)})
    for key, p in pk.items():
        if key not in ck:
            events.append({"type": "removed", "key": key, "name": p["name"], "value": round(p["value"], 2)})
            if p.get("quantity") is not None:
                traded_net -= p["value"]
    total_change = cur_value - prev_value
    return {"events": events, "total_change": round(total_change, 2), "traded_net": round(traded_net, 2),
            "market_and_other": round(total_change - traded_net, 2)}


def record_account_snapshots(db, uid: int, summary: dict) -> None:
    import json
    day = date.fromisoformat(summary["as_of"])
    for a in summary["accounts"]:
        db.execute(
            text("""INSERT INTO savings_snapshots (user_id, account_key, as_of, value, holdings)
                    VALUES (:u, :k, :d, :v, CAST(:h AS jsonb))
                    ON CONFLICT (user_id, account_key, as_of) DO UPDATE SET value = :v, holdings = CAST(:h AS jsonb)"""),
            {"u": uid, "k": str(a["id"]), "d": day, "v": a["value"], "h": json.dumps(_snapshot_holdings(a))})
    db.commit()


def load_changes(db, uid: int, days: int = 180, limit: int = 100) -> list[dict]:
    """Per-account differences between consecutive snapshots that show real activity, newest first."""
    import json
    since = date.today() - timedelta(days=days)
    rows = db.execute(
        text("""SELECT account_key, as_of, value, holdings FROM savings_snapshots
                WHERE user_id = :u AND as_of >= :s ORDER BY account_key, as_of"""),
        {"u": uid, "s": since}).fetchall()
    out, prev_by_key = [], {}
    for key, as_of, value, holdings in rows:
        hold = holdings if isinstance(holdings, list) else json.loads(holdings)
        prev = prev_by_key.get(key)
        if prev:
            d = diff_snapshots(prev["holdings"], hold, prev["value"], float(value))
            if d["events"]:
                out.append({"account_key": key, "from": prev["as_of"].isoformat(), "to": as_of.isoformat(), **d})
        prev_by_key[key] = {"as_of": as_of, "value": float(value), "holdings": hold}
    return sorted(out, key=lambda c: c["to"], reverse=True)[:limit]


def load_history(db, uid: int, limit: int = 400) -> list[dict]:
    rows = db.execute(text("""SELECT as_of, total FROM savings_history WHERE user_id = :u
                              ORDER BY as_of DESC LIMIT :l"""), {"u": uid, "l": limit}).fetchall()
    return [{"as_of": r[0].isoformat(), "total": float(r[1])} for r in reversed(rows)]
