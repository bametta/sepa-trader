"""
IRA Portfolio Manager — long-term, AI-guided ETF allocation for an Alpaca Roth IRA.

Completely separate from the SEPA swing system: own credentials (mode="ira"),
no stops/brackets, weekly drift-based rebalancing, cash-account safe
(never spends unsettled funds, never shorts, never uses margin).

Flow:  baseline (rules + Dual Momentum)  →  AI adjustment (bounded)  →
       validate/normalize  →  diff vs holdings  →  sells then buys.
"""
import json
import logging
import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import alpaca_client as alp
from .database import get_user_setting, get_all_user_settings
from .utils import sf as _sf

logger = logging.getLogger(__name__)

STRATEGY = "ira_manager"
MODE     = "ira"

DEFAULT_UNIVERSE = ["VTI", "VXUS", "QQQ", "SCHD", "VNQ", "BND", "TIP", "GLD", "BIL"]

MAX_WEIGHT       = 0.40   # no single ETF above 40%
MIN_DEFENSIVE    = 0.05   # always keep >=5% in BND/TIP/BIL
MIN_TRADE_USD    = 5.0
DEFENSIVE        = {"BND", "TIP", "BIL"}

# Rules-based baselines by regime (weights sum to 1.0)
BASELINES = {
    "risk_on": {"VTI": 0.35, "VXUS": 0.15, "QQQ": 0.15, "SCHD": 0.10,
                "VNQ": 0.05, "BND": 0.10, "TIP": 0.00, "GLD": 0.05, "BIL": 0.05},
    "neutral": {"VTI": 0.30, "VXUS": 0.15, "QQQ": 0.05, "SCHD": 0.10,
                "VNQ": 0.05, "BND": 0.20, "TIP": 0.05, "GLD": 0.05, "BIL": 0.05},
    "risk_off": {"VTI": 0.15, "VXUS": 0.05, "QQQ": 0.00, "SCHD": 0.10,
                 "VNQ": 0.00, "BND": 0.30, "TIP": 0.10, "GLD": 0.10, "BIL": 0.20},
}


# ── Config / client ───────────────────────────────────────────────────────────

def get_config(db: Session, user_id: int) -> dict:
    g = lambda k, d: get_user_setting(db, k, d, user_id)
    uni = [s.strip().upper() for s in g("ira_etf_universe", "").split(",") if s.strip()]
    try:
        threshold = float(g("ira_rebalance_threshold_pct", "5"))
    except ValueError:
        threshold = 5.0
    try:
        cash_buf = float(g("ira_cash_buffer_pct", "2"))
    except ValueError:
        cash_buf = 2.0
    try:
        day = int(g("ira_rebalance_day", "4"))
    except ValueError:
        day = 4
    return {
        "enabled":       g("ira_enabled", "false").lower() == "true",
        "universe":      uni or DEFAULT_UNIVERSE,
        "threshold_pct": max(1.0, min(25.0, threshold)),
        "cash_buffer":   max(0.0, min(20.0, cash_buf)) / 100.0,
        "rebalance_day": day if 0 <= day <= 6 else 4,
    }


def configure_client(db: Session, user_id: int) -> None:
    merged = get_all_user_settings(db, user_id)
    alp.configure_from_db_settings(merged, MODE, is_admin=False)


def has_credentials(db: Session, user_id: int) -> bool:
    merged = get_all_user_settings(db, user_id)
    return bool((merged.get("alpaca_ira_key") or "").strip()
                and (merged.get("alpaca_ira_secret") or "").strip())


# ── Portfolio snapshot ────────────────────────────────────────────────────────

def get_snapshot(db: Session, user_id: int) -> dict:
    configure_client(db, user_id)
    client  = alp.get_client(MODE)
    account = client.get_account()
    equity  = _sf(account.equity, 0.0) or _sf(account.portfolio_value, 0.0)
    cash    = _sf(account.cash, 0.0)
    # Cash accounts: only settled funds are spendable.
    spendable = _sf(getattr(account, "non_marginable_buying_power", None), None)
    if spendable is None:
        spendable = cash
    spendable = max(0.0, min(spendable, cash))

    holdings = []
    for p in client.get_all_positions():
        mv = _sf(p.market_value, 0.0)
        holdings.append({
            "symbol":        p.symbol,
            "qty":           _sf(p.qty, 0.0),
            "market_value":  mv,
            "weight":        (mv / equity) if equity > 0 else 0.0,
            "avg_entry":     _sf(p.avg_entry_price, 0.0),
            "current_price": _sf(p.current_price, 0.0),
            "unrealized_pl": _sf(p.unrealized_pl, 0.0),
            "unrealized_plpc": (_sf(p.unrealized_plpc, 0.0) or 0.0) * 100,
        })
    return {
        "equity":    equity,
        "cash":      cash,
        "spendable": spendable,
        "holdings":  sorted(holdings, key=lambda h: -h["market_value"]),
        "status":    str(getattr(account, "status", "")),
    }


# ── Allocation engine ─────────────────────────────────────────────────────────

def _detect_regime() -> tuple[str, dict]:
    """Map market environment + Dual Momentum into a coarse regime label.

    SPY below its 200-day SMA (CORRECTION) caps the regime at neutral, so a
    Dual Momentum SPY/EFA pick can't promote it to risk-on.
    """
    info: dict = {}
    regime = "neutral"
    capped = False
    try:
        from .strategies.market_env import assess
        env = assess() or {}
        info["market_env"] = env
        label = str(env.get("environment") or "").upper()
        if label in ("BULL", "BULL_VOLATILE"):
            regime = "risk_on"
        elif label == "BEAR":
            regime = "risk_off"
        elif label == "CORRECTION":
            capped = True
    except Exception as exc:
        logger.warning("ira regime: market_env failed: %s", exc)
    try:
        from .strategies.dual_momentum import evaluate
        dm = evaluate() or {}
        info["dual_momentum"] = {k: dm.get(k) for k in ("recommended_symbol", "reasoning")}
        rec = dm.get("recommended_symbol")
        if rec in ("AGG", "BIL"):
            regime = "risk_off"
        elif rec in ("SPY", "EFA") and regime == "neutral" and not capped:
            regime = "risk_on"
    except Exception as exc:
        logger.warning("ira regime: dual momentum failed: %s", exc)
    return regime, info


def normalize_weights(weights: dict, universe: list[str]) -> dict:
    """Clamp to universe, cap each weight, enforce defensive floor, sum to 1."""
    w = {s: max(0.0, float(weights.get(s, 0.0) or 0.0)) for s in universe}
    if sum(w.values()) <= 0:
        w = {s: 1.0 / len(universe) for s in universe}
    for _ in range(10):
        total = sum(w.values())
        w = {s: v / total for s, v in w.items()}
        over = {s for s, v in w.items() if v > MAX_WEIGHT + 1e-9}
        if not over:
            break
        excess = sum(w[s] - MAX_WEIGHT for s in over)
        for s in over:
            w[s] = MAX_WEIGHT
        room = [s for s in w if s not in over and w[s] < MAX_WEIGHT]
        if not room:
            break
        room_total = sum(w[s] for s in room)
        for s in room:
            share = (w[s] / room_total) if room_total > 0 else 1.0 / len(room)
            w[s] = min(MAX_WEIGHT, w[s] + excess * share)
    defensive = [s for s in w if s in DEFENSIVE]
    deficit = MIN_DEFENSIVE - sum(w[s] for s in defensive)
    if defensive and deficit > 0:
        risky = [s for s in w if s not in DEFENSIVE and w[s] > 0]
        risky_total = sum(w[s] for s in risky)
        if risky_total > 0:
            for s in risky:
                w[s] -= deficit * (w[s] / risky_total)
        w[defensive[0]] += deficit
    total = sum(w.values()) or 1.0
    return {s: round(v / total, 4) for s, v in w.items()}


def _ai_adjust(db: Session, user_id: int, baseline: dict, regime: str,
               context: dict, holdings: list[dict]) -> tuple[dict, str]:
    """Ask the AI to tilt the baseline. Returns (weights, rationale). Fail-open to baseline."""
    from .claude_analyst import _call_ai
    prompt = (
        "You are the portfolio manager for a long-horizon Roth IRA (ETF-only, no leverage, "
        "no shorting, rebalanced weekly). Goal: long-term growth with controlled drawdowns.\n\n"
        f"Detected regime: {regime}\n"
        f"Context: {json.dumps(context, default=str)[:2500]}\n"
        f"Current holdings: {json.dumps([{k: h[k] for k in ('symbol','weight','unrealized_plpc')} for h in holdings], default=str)}\n"
        f"Rules-based baseline weights: {json.dumps(baseline)}\n\n"
        "You may tilt the baseline by at most ±10 percentage points per ETF. Use ONLY the tickers "
        "in the baseline. Prefer stability — do not churn without a clear reason.\n"
        'Reply with JSON only: {"weights": {"TICKER": 0.xx, ...}, "rationale": "2-4 sentences"}'
    )
    try:
        raw = _call_ai(db, prompt, 700, user_id)
    except Exception as exc:
        logger.error("ira AI call failed: %s", exc)
        return baseline, f"AI unavailable ({str(exc)[:80]}); using rules-based baseline."
    if not raw:
        return baseline, "No AI provider configured; using rules-based baseline."
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return baseline, "AI response unparseable; using rules-based baseline."
    try:
        data = json.loads(m.group(0))
        proposed = {s.upper(): float(v) for s, v in (data.get("weights") or {}).items()}
    except Exception:
        return baseline, "AI response unparseable; using rules-based baseline."
    bounded = {}
    for s, base_w in baseline.items():
        p = proposed.get(s, base_w)
        bounded[s] = min(base_w + 0.10, max(base_w - 0.10, p))
    return bounded, str(data.get("rationale", ""))[:1000]


def build_target(db: Session, user_id: int, holdings: list[dict] | None = None) -> dict:
    cfg = get_config(db, user_id)
    regime, ctx = _detect_regime()
    baseline = normalize_weights(BASELINES[regime], cfg["universe"])
    tilted, rationale = _ai_adjust(db, user_id, baseline, regime, ctx, holdings or [])
    target = normalize_weights(tilted, cfg["universe"])
    return {"regime": regime, "baseline": baseline, "target": target, "rationale": rationale}


# ── Rebalance planning / execution ────────────────────────────────────────────

def plan_rebalance(snapshot: dict, target: dict, threshold_pct: float,
                   cash_buffer: float) -> list[dict]:
    """Return sells (first) then buys. Buys are capped to settled cash."""
    equity = snapshot["equity"]
    if equity <= 0:
        return []
    investable = equity * (1.0 - cash_buffer)
    held = {h["symbol"]: h for h in snapshot["holdings"]}
    orders: list[dict] = []

    for sym in sorted(set(held) | set(target)):
        cur_val = held.get(sym, {}).get("market_value", 0.0)
        cur_w   = cur_val / equity
        tgt_w   = target.get(sym, 0.0)
        drift   = (cur_w - tgt_w) * 100.0
        tgt_val = investable * tgt_w
        delta   = tgt_val - cur_val
        if abs(delta) < MIN_TRADE_USD:
            continue
        if sym not in target:
            orders.append({"symbol": sym, "side": "sell", "notional": round(cur_val, 2),
                           "full": True, "drift_pct": round(drift, 2)})
        elif abs(drift) >= threshold_pct:
            if delta < 0:
                orders.append({"symbol": sym, "side": "sell", "notional": round(-delta, 2),
                               "full": False, "drift_pct": round(drift, 2)})
            else:
                orders.append({"symbol": sym, "side": "buy", "notional": round(delta, 2),
                               "drift_pct": round(drift, 2)})

    sells = [o for o in orders if o["side"] == "sell"]
    buys  = sorted((o for o in orders if o["side"] == "buy"), key=lambda o: -o["notional"])
    budget = snapshot["spendable"]
    capped = []
    for o in buys:
        amt = min(o["notional"], budget)
        if amt >= MIN_TRADE_USD:
            capped.append({**o, "notional": round(amt, 2),
                           "capped": amt < o["notional"] - 0.01})
            budget -= amt
    return sells + capped


def execute_plan(db: Session, user_id: int, orders: list[dict]) -> list[dict]:
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    configure_client(db, user_id)
    client = alp.get_client(MODE)
    results = []
    for o in orders:
        try:
            if o["side"] == "sell" and o.get("full"):
                resp = client.close_position(o["symbol"])
            else:
                resp = client.submit_order(MarketOrderRequest(
                    symbol=o["symbol"],
                    notional=round(o["notional"], 2),
                    side=OrderSide.SELL if o["side"] == "sell" else OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                ))
            results.append({**o, "status": "submitted", "order_id": str(getattr(resp, "id", ""))})
        except Exception as exc:
            logger.error("ira order %s %s failed: %s", o["side"], o["symbol"], exc)
            results.append({**o, "status": "failed", "error": str(exc)[:200]})
    return results


def _market_open(client) -> bool:
    try:
        return bool(client.get_clock().is_open)
    except Exception:
        return False


def run_rebalance(db: Session, user_id: int, execute: bool = False, force: bool = False) -> dict:
    """Evaluate (and optionally execute) a rebalance. Always logs the proposal."""
    if not has_credentials(db, user_id):
        raise ValueError("ira_credentials_missing")
    cfg  = get_config(db, user_id)
    snap = get_snapshot(db, user_id)
    tgt  = build_target(db, user_id, snap["holdings"])
    threshold = 0.0 if force else cfg["threshold_pct"]
    plan = plan_rebalance(snap, tgt["target"], threshold, cfg["cash_buffer"])

    results: list[dict] = []
    executed = False
    note = ""
    if plan and execute:
        if not _market_open(alp.get_client(MODE)):
            note = "Market closed — plan saved, not executed."
        else:
            results = execute_plan(db, user_id, plan)
            executed = any(r["status"] == "submitted" for r in results)
    elif snap["equity"] <= 0:
        note = "IRA account has no funds yet — nothing to allocate."
    elif not plan:
        note = "Portfolio within drift threshold — no trades needed."

    payload = {
        "regime": tgt["regime"], "baseline": tgt["baseline"], "target": tgt["target"],
        "plan": plan, "results": results, "equity": snap["equity"],
        "spendable": snap["spendable"], "note": note,
        "ran_at": datetime.utcnow().isoformat(),
    }
    db.execute(
        text("""
            INSERT INTO strategy_signals
                (user_id, strategy_name, action, data, reasoning, ai_verdict, ai_reasoning, mode, executed)
            VALUES (:uid, :name, :action, CAST(:data AS jsonb), :reasoning, :verdict, :ai, :mode, :ex)
        """),
        {"uid": user_id, "name": STRATEGY,
         "action": "REBALANCE" if plan else "HOLD",
         "data": json.dumps(payload, default=str),
         "reasoning": f"Regime: {tgt['regime']}. {len(plan)} trade(s) proposed.",
         "verdict": "EXECUTED" if executed else ("PROPOSED" if plan else "HOLD"),
         "ai": tgt["rationale"], "mode": MODE, "ex": executed},
    )
    db.commit()
    if executed:
        try:
            from . import telegram_alerts as tg
            tg.send_sync(f"IRA rebalance submitted: {len(results)} order(s), regime {tgt['regime']}.")
        except Exception:
            pass
    return {**payload, "executed": executed, "rationale": tgt["rationale"]}


def get_history(db: Session, user_id: int, limit: int = 20) -> list[dict]:
    rows = db.execute(
        text("""SELECT id, action, data, reasoning, ai_verdict, ai_reasoning, executed, created_at
                FROM strategy_signals
                WHERE user_id = :uid AND strategy_name = :name
                ORDER BY created_at DESC LIMIT :l"""),
        {"uid": user_id, "name": STRATEGY, "l": limit},
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping)
        if isinstance(d.get("data"), str):
            d["data"] = json.loads(d["data"])
        out.append(d)
    return out
