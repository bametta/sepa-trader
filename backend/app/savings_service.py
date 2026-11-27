"""
Savings tracker service: live prices, the read-only Alpaca IRA account, look-through concentration
and the combined retirement projection. Planning aid only, not financial advice.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import numpy as np

from . import ira_planner, ira_stress, savings, savings_trades
from .database import get_user_setting

logger = logging.getLogger(__name__)

_PRICE_TTL = 15 * 60
_price_cache: dict = {}


def _price_one(symbol: str) -> float | None:
    hit = _price_cache.get(symbol)
    if hit and time.time() - hit[0] < _PRICE_TTL:
        return hit[1]
    try:
        from .strategies import yf_client as yf
        df = yf.fetch_history(symbol, 7)
        price = float(df["Close"].iloc[-1]) if df is not None and not df.empty else None
    except Exception as exc:
        logger.debug("savings price %s failed: %s", symbol, exc)
        price = None
    if price is not None:                      # never cache failures, so the next load retries
        _price_cache[symbol] = (time.time(), price)
    return price


def fetch_prices(symbols) -> dict:
    syms = sorted({s.upper() for s in symbols if s})
    if not syms:
        return {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(_price_one, syms))
    return {s: p for s, p in zip(syms, got) if p}


def alpaca_account(db, uid: int) -> dict | None:
    """The managed Alpaca IRA as a read-only account (None if not connected or unreachable)."""
    from . import ira_manager as ira
    if not ira.has_credentials(db, uid):
        return None
    try:
        snap = ira.get_snapshot(db, uid)
    except Exception as exc:
        logger.warning("savings: alpaca IRA unavailable: %s", exc)
        return None
    today = date.today()
    holdings = [{"id": None, "symbol": h["symbol"], "name": h["symbol"], "asset_class": "auto", "quantity": h["qty"],
                 "manual_value": h["market_value"], "as_of": today, "illiquid": False}
                for h in snap["holdings"] if h["market_value"] > 0]
    if snap["cash"] > 0.005:
        holdings.append({"id": None, "symbol": None, "name": "Cash", "asset_class": "cash", "quantity": None,
                         "manual_value": snap["cash"], "as_of": today, "illiquid": False})
    prof = ira_planner.get_profile(db, uid)
    annual = (ira_planner.contribution_limit(prof["age"]) if prof["contribute_max"]
              else (prof["planned_contribution"] or 0.0))
    return {"id": "alpaca-ira", "provider": "Alpaca", "name": "Roth IRA (managed by this app)",
            "account_type": "roth_ira", "annual_contribution": annual, "retirement": True,
            "read_only": True, "holdings": holdings}


def lookthrough_concentration(summary: dict) -> dict:
    """Best-effort single-stock exposure across all accounts from known ETFs' top-10 holdings."""
    try:
        from . import ira_data
        total = summary["total"]
        if not total:
            return {"available": False}
        weights: dict = {}
        for a in summary["accounts"]:
            for h in a["holdings"]:
                if h["symbol"] and h["symbol"] in ira_data.REFERENCE:
                    weights[h["symbol"]] = weights.get(h["symbol"], 0.0) + h["value"] / total
        lt = ira_data.lookthrough(weights)
        lt["coverage"] = round(sum(weights.values()), 3)
        return lt
    except Exception as exc:
        logger.debug("savings lookthrough failed: %s", exc)
        return {"available": False}


def get_state(db, uid: int, record: bool = True) -> dict:
    accounts = savings.load_accounts(db, uid)
    virtual = alpaca_account(db, uid)
    if virtual:
        accounts = [virtual] + accounts
    symbols = [h["symbol"] for a in accounts for h in a["holdings"] if h.get("symbol") and h.get("quantity") is not None]
    birth = ira_planner.get_profile(db, uid).get("birth_year")
    summary = savings.build_summary(accounts, fetch_prices(symbols), birth_year=birth)
    for a, src in zip(summary["accounts"], accounts):          # carry notes for the editor
        a["notes"] = src.get("notes", "")
    summary["lookthrough"] = lookthrough_concentration(summary)
    if record and summary["total"] > 0:
        try:
            savings.record_snapshot(db, uid, summary)
            savings.record_account_snapshots(db, uid, summary)
        except Exception as exc:
            logger.warning("savings snapshot failed: %s", exc)
            db.rollback()
    return summary


def changes(db, uid: int, days: int = 180) -> list[dict]:
    """Activity feed from snapshot diffs, labelled with account names."""
    names = {str(a["id"]): f"{a['provider']} · {a['name']}" for a in savings.load_accounts(db, uid)}
    names["alpaca-ira"] = "Alpaca · Roth IRA (managed by this app)"
    return [{**c, "account": names.get(c["account_key"], "Removed account")}
            for c in savings.load_changes(db, uid, days)]


def combined_plan(db, uid: int, summary: dict) -> dict:
    """Project the total of all retirement accounts using the age-based glidepath mix."""
    p = ira_planner.get_profile(db, uid)
    if p["age"] is None or not p["years_to_retire"] or p["years_to_retire"] < 1:
        return {"ready": False, "note": "Set your birth year and retirement age in Settings -> IRA Portfolio."}
    try:
        target = float(get_user_setting(db, "savings_total_target", "", uid) or 0) or None
    except ValueError:
        target = None
    years = p["years_to_retire"]
    balance = float(summary["retirement_total"])
    annual = sum(a["annual_contribution"] for a in summary["accounts"] if a["retirement"])
    glide = ira_planner.glidepath_equity(p["age"], p["retire_age"], p["risk"])
    mu, sigma = ira_planner.mix_stats(ira_planner.strategic_mix(glide))
    end = ira_planner._simulate(balance, annual, years, mu, sigma)
    p10, p50, p90 = (float(np.percentile(end, q)) for q in (10, 50, 90))
    out = {
        "ready": True, "balance": round(balance), "annual_contribution": round(annual), "years": years,
        "retire_age": p["retire_age"], "glidepath_equity": glide, "actual_equity_share": summary["equity_share"],
        "p10": round(p10), "p50": round(p50), "p90": round(p90), "target": round(target) if target else None,
        "assumptions": "Real (today's) dollars, long-run return assumptions, age-based glidepath mix, "
                       "constant yearly contributions across all retirement accounts.",
    }
    try:
        safe = ira_stress.withdrawal_test(max(p50, 1.0), p["retire_age"], p["risk"], 0.04)["safe_rate_90pct"]
    except Exception:
        safe = None
    if safe:
        out["safe_withdrawal_rate"] = safe
        out["income_at_safe_rate"] = {"p10": round(p10 * safe), "p50": round(p50 * safe), "p90": round(p90 * safe)}
    if target:
        out["prob_reach_target"] = round(float((end >= target).mean()), 3)
        if out["prob_reach_target"] < 0.8:
            lo, hi = 0.0, max(annual * 8, 100_000.0)
            for _ in range(18):
                mid = (lo + hi) / 2
                ok = float((ira_planner._simulate(balance, annual + mid, years, mu, sigma) >= target).mean()) >= 0.8
                lo, hi = (lo, mid) if ok else (mid, hi)
            out["extra_saving_for_80pct"] = round(hi)
    return out


# ── Import and manual-trade helper ────────────────────────────────────────────

def _own_account(db, uid: int, account_id: int) -> dict:
    for a in savings.load_accounts(db, uid):
        if a["id"] == account_id:
            return a
    raise ValueError("account_not_found")


def _valued(holdings: list[dict], prices: dict) -> list[dict]:
    out = []
    for h in holdings:
        sym = (h.get("symbol") or "").upper() or None
        val, _ = savings.value_holding(h, prices.get(sym) if sym else None)
        out.append({"key": sym or h["name"], "symbol": sym, "name": h["name"], "quantity": h.get("quantity"),
                    "value": val})
    return out


def import_positions(db, uid: int, account_id: int, text: str, apply: bool = False) -> dict:
    """Parse a Schwab positions export (or our own CSV), show what would change, and save only if `apply`."""
    from . import schwab_import
    account = _own_account(db, uid, account_id)
    if schwab_import.looks_like_schwab(text):
        parsed = schwab_import.parse_schwab_positions(text)
        source = "schwab"
    else:
        parsed = {"holdings": savings.parse_holdings_csv(text), "file_total": None, "warnings": [], "title": None}
        source = "csv"
    new = parsed["holdings"]
    symbols = [h["symbol"] for h in new + account["holdings"] if h.get("symbol") and h.get("quantity") is not None]
    prices = fetch_prices(symbols)
    before, after = _valued(account["holdings"], prices), _valued(new, prices)
    prev_total, new_total = sum(h["value"] for h in before), sum(h["value"] for h in after)
    diff = savings.diff_snapshots(before, after, prev_total, new_total) if before else \
        {"events": [{"type": "added", "key": h["key"], "name": h["name"], "quantity": h["quantity"],
                     "value": round(h["value"], 2)} for h in after], "total_change": round(new_total, 2),
         "traded_net": 0.0, "market_and_other": round(new_total, 2)}
    warnings = list(parsed["warnings"])
    if before and prev_total and abs(new_total - prev_total) / prev_total > 0.5:
        warnings.append(f"The new total (${new_total:,.0f}) differs from the stored total (${prev_total:,.0f}) by more "
                        "than 50%. Make sure this file belongs to this account.")
    result = {"source": source, "title": parsed.get("title"), "holdings": len(new), "file_total": parsed.get("file_total"),
              "parsed_total": round(new_total, 2), "stored_total": round(prev_total, 2), "warnings": warnings,
              "diff": diff, "applied": False}
    if apply:
        savings.replace_holdings(db, uid, account_id, new)
        result["applied"] = True
    return result


def saved_target_model(db, uid: int) -> str:
    return (get_user_setting(db, "savings_target_model", "", uid) or "").strip()


def default_target_for(db, uid: int) -> dict:
    p = ira_planner.get_profile(db, uid)
    if p["age"] is None:
        raise ValueError("Set your birth year and retirement age in Settings, or type a target model yourself.")
    return savings_trades.default_target(ira_planner.glidepath_equity(p["age"], p["retire_age"], p["risk"]))


def target_info(db, uid: int) -> dict:
    saved = saved_target_model(db, uid)
    try:
        default = savings_trades.format_target(default_target_for(db, uid))
    except ValueError:
        default = ""
    return {"saved": saved, "default": default, "effective": saved or default}


def trade_plan(db, uid: int, account_ids: list[int] | None, mode: str, target_text: str | None) -> dict:
    from . import savings_trades as st
    accounts = [a for a in savings.load_accounts(db, uid) if account_ids is None or a["id"] in account_ids]
    accounts = [a for a in accounts if savings.tax_bucket(a["account_type"]) in ("roth", "pre_tax")]
    if not accounts:
        raise ValueError("Pick at least one Roth or pre-tax retirement account that has holdings.")
    text = (target_text or "").strip() or saved_target_model(db, uid)
    target = st.parse_target_model(text) if text else default_target_for(db, uid)
    symbols = set(target) | {h["symbol"] for a in accounts for h in a["holdings"]
                             if h.get("symbol") and h.get("quantity") is not None}
    prices = fetch_prices(symbols)
    shaped = []
    for a in accounts:
        holdings = []
        for h in a["holdings"]:
            sym = (h.get("symbol") or "").upper() or None
            val, _ = savings.value_holding(h, prices.get(sym) if sym else None)
            holdings.append({**h, "symbol": sym, "value": val})
        shaped.append({"id": a["id"], "name": f"{a['provider']} · {a['name']}",
                       "tax": savings.tax_bucket(a["account_type"]), "holdings": holdings})
    plan = st.plan_trades(shaped, prices, target, mode=mode)
    plan["target_text"] = st.format_target(target)
    plan["target_source"] = "typed" if (target_text or "").strip() else ("saved" if text else "age-based default")
    plan["prices_as_of"] = "live quotes (may be delayed); use limit orders"
    return plan
