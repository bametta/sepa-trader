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
MIN_DEPLOY_USD   = 25.0   # idle settled cash below this isn't worth deploying
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
    try:
        tilt = float(g("ira_tilt_max_pp", "5"))
    except ValueError:
        tilt = 5.0
    return {
        "enabled":       g("ira_enabled", "false").lower() == "true",
        "halted":        g("ira_halt", "false").lower() == "true",
        "trend_overlay": g("ira_trend_overlay", "true").lower() != "false",
        "tilt_pp":       max(0.0, min(10.0, tilt)),
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
    """Regime from SPY vs its 200-day SMA and VIX only, the exact rules validated in ira_backtest:
    BULL/BULL_VOLATILE -> risk_on, BEAR -> risk_off, anything else (CORRECTION, TRANSITIONAL,
    data failure) -> neutral. Dual Momentum is deliberately excluded because it was never backtested.
    """
    info: dict = {}
    regime = "neutral"
    try:
        from .strategies.market_env import assess
        env = assess() or {}
        info["market_env"] = env
        label = str(env.get("environment") or "").upper()
        if label in ("BULL", "BULL_VOLATILE"):
            regime = "risk_on"
        elif label == "BEAR":
            regime = "risk_off"
    except Exception as exc:
        logger.warning("ira regime: market_env failed: %s", exc)
    return regime, info


def cap_flagged(tilted: dict, core: dict, flagged: list[str], universe: list[str]) -> dict:
    """The AI may not undo the trend overlay: flagged ETFs never exceed their core weight.
    Any weight removed is parked in cash so the total is unchanged."""
    park = next((s for s in ("BIL", "BND", "TIP") if s in universe), None)
    out = dict(tilted)
    for sym in flagged:
        excess = out.get(sym, 0.0) - core.get(sym, 0.0)
        if excess > 0 and park:
            out[sym] -= excess
            out[park] = out.get(park, 0.0) + excess
    return out


def apply_trend_overlay(weights: dict, stats: dict, universe: list[str]) -> tuple[dict, list[str]]:
    """Halve any risk ETF trading below its own 200-day average and park the freed weight in cash.

    Mirrors ira_backtest.build_regime_targets(trend=True), the variant that passed validation.
    ETFs with no live data are left alone (the AI gate blocks stale data separately).
    Returns (normalized weights, list of flagged symbols).
    """
    park = next((s for s in ("BIL", "BND", "TIP") if s in universe), None)
    if park is None:
        return dict(weights), []
    out, flagged, freed = dict(weights), [], 0.0
    for sym, w in weights.items():
        if sym in DEFENSIVE or w <= 0:
            continue
        st = stats.get(sym)
        if st and st.get("above_200sma") is False:
            out[sym] = w * 0.5
            freed += w * 0.5
            flagged.append(sym)
    if freed:
        out[park] = out.get(park, 0.0) + freed
    return normalize_weights(out, universe), flagged


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
               context: dict, holdings: list[dict], tilt: float = 0.05) -> tuple[dict, str]:
    """Ask the AI to tilt the baseline by at most ±`tilt` per ETF. Returns (weights, rationale).
    Falls back to the baseline on any AI problem; tilt <= 0 disables the AI allocation step."""
    if tilt <= 0:
        return baseline, "AI tilt disabled; rules-based glidepath core only."
    from .claude_analyst import _call_ai
    prompt = (
        "You are the portfolio manager for a long-horizon Roth IRA (ETF-only, no leverage, "
        "no shorting, rebalanced weekly). Goal: long-term growth with controlled drawdowns.\n\n"
        f"Detected regime: {regime}\n"
        f"Context: {json.dumps({k: v for k, v in context.items() if k not in ('etf_market', 'profile_line')}, default=str)[:2000]}\n"
        f"Live ETF stats (returns, 20d vol, % off 52w high, above 200d SMA): {json.dumps(context.get('etf_market', {}))}\n"
        f"{context.get('profile_line', '')}\n"
        f"Current holdings: {json.dumps([{k: h[k] for k in ('symbol','weight','unrealized_plpc')} for h in holdings], default=str)}\n"
        f"Rules-based core weights (regime baseline after the validated trend overlay): {json.dumps(baseline)}\n"
        "The trend overlay already halved any ETF below its 200-day average. Do NOT reverse it: never raise an "
        "ETF listed under trend_overlay.halved_etfs_below_200d_sma above its core weight.\n\n"
        f"You may tilt the baseline by at most ±{tilt * 100:.0f} percentage points per ETF. Use ONLY the tickers "
        "in the baseline. Prefer stability — do not churn without a clear reason.\n"
        'Reply with JSON only: {"weights": {"TICKER": 0.xx, ...}, "rationale": "2-4 sentences"}'
    )
    try:
        raw = _call_ai(db, prompt, 1500, user_id)
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
        bounded[s] = min(base_w + tilt, max(base_w - tilt, p))
    return bounded, str(data.get("rationale", ""))[:1000]


def build_target(db: Session, user_id: int, holdings: list[dict] | None = None) -> dict:
    cfg = get_config(db, user_id)
    regime, ctx = _detect_regime()
    stats: dict = {}
    try:
        from . import ira_data, ira_planner
        stats = ira_data.market_stats(cfg["universe"])
        ctx["etf_market"] = {s: {k: v[k] for k in ("ret_1m", "ret_3m", "ret_12m", "vol_20d",
                                                    "pct_off_52w_high", "above_200sma")}
                             for s, v in stats.items() if v}
        prof = ira_planner.get_profile(db, user_id)
        if prof["age"] is not None:
            ctx["profile_line"] = (
                f"Investor: age {prof['age']}, retire at {prof['retire_age']}, risk {prof['risk']}; "
                f"age-appropriate equity share ~{ira_planner.glidepath_equity(prof['age'], prof['retire_age'], prof['risk']):.0%}.")
    except Exception as exc:
        logger.warning("ira live context failed: %s", exc)
    baseline = normalize_weights(BASELINES[regime], cfg["universe"])
    core, flagged = baseline, []
    if cfg["trend_overlay"]:
        core, flagged = apply_trend_overlay(baseline, stats, cfg["universe"])
    ctx["trend_overlay"] = ({"applied": True, "halved_etfs_below_200d_sma": flagged} if cfg["trend_overlay"]
                            else {"applied": False})
    tilted, rationale = _ai_adjust(db, user_id, core, regime, ctx, holdings or [], cfg["tilt_pp"] / 100.0)
    tilted = cap_flagged(tilted, core, flagged, cfg["universe"])
    target = normalize_weights(tilted, cfg["universe"])
    return {"regime": regime, "baseline": baseline, "core": core, "trend_flagged": flagged,
            "target": target, "rationale": rationale}


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

    # Deploy idle settled cash (initial funding, contributions) into the most underweight
    # ETFs even when each shortfall is under the drift threshold. Buy-only, keeps the cash buffer.
    free = budget - equity * cash_buffer
    if free >= MIN_DEPLOY_USD:
        selling = {o["symbol"] for o in sells}
        ordered = {o["symbol"]: o for o in capped}
        short = {}
        for sym, tgt_w in target.items():
            if tgt_w <= 0 or sym in selling:
                continue
            gap = investable * tgt_w - held.get(sym, {}).get("market_value", 0.0) \
                  - ordered.get(sym, {}).get("notional", 0.0)
            if gap >= MIN_TRADE_USD:
                short[sym] = gap
        total = sum(short.values())
        for sym, gap in sorted(short.items(), key=lambda kv: -kv[1]):
            amt = round(gap * min(1.0, free / total), 2)
            if amt < MIN_TRADE_USD:
                continue
            if sym in ordered:
                ordered[sym]["notional"] = round(ordered[sym]["notional"] + amt, 2)
                ordered[sym]["capped"] = False
            else:
                drift = (held.get(sym, {}).get("market_value", 0.0) / equity - target[sym]) * 100.0
                capped.append({"symbol": sym, "side": "buy", "notional": amt,
                               "drift_pct": round(drift, 2), "cash_deploy": True})
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


def _alert(msg: str, level: str = "INFO") -> None:
    try:
        from . import telegram_alerts as tg
        tg.send_sync(msg, level=level)
    except Exception as exc:
        logger.warning("ira alert failed: %s", exc)


_BAD_FINAL = ("canceled", "cancelled", "rejected", "expired")


def verify_orders(client, results: list[dict], timeout_s: int = 25) -> list[dict]:
    """Poll submitted orders until filled/cancelled/rejected or timeout; annotate each result."""
    import time
    deadline = time.time() + timeout_s
    pending = [r for r in results if r.get("status") == "submitted" and r.get("order_id")]
    while pending:
        still = []
        for r in pending:
            try:
                o = client.get_order_by_id(r["order_id"])
            except Exception as exc:
                r["verify_error"] = str(exc)[:120]
                still.append(r)
                continue
            st = str(getattr(o, "status", "")).lower().split(".")[-1]
            r["final_status"] = st
            r["filled_qty"] = _sf(getattr(o, "filled_qty", None), 0.0)
            r["filled_avg_price"] = _sf(getattr(o, "filled_avg_price", None), None)
            if st != "filled" and st not in _BAD_FINAL:
                still.append(r)
        pending = still
        if not pending or time.time() >= deadline:
            break
        time.sleep(2)
    return results


def _problems(results: list[dict]) -> list[str]:
    out = []
    for r in results:
        if r.get("status") == "failed":
            out.append(f"{r['side']} {r['symbol']} failed: {r.get('error', '')}")
        elif r.get("final_status") in _BAD_FINAL:
            out.append(f"{r['side']} {r['symbol']} {r['final_status']}")
        elif r.get("status") == "submitted" and r.get("final_status") != "filled":
            out.append(f"{r['side']} {r['symbol']} not confirmed filled ({r.get('final_status', 'unknown')})")
    return out


def _execute_and_verify(db: Session, user_id: int, plan: list[dict], regime: str) -> tuple[list[dict], bool]:
    results = execute_plan(db, user_id, plan)
    verify_orders(alp.get_client(MODE), results)
    problems = _problems(results)
    if problems:
        _alert("IRA rebalance needs attention:\n" + "\n".join(problems[:8]), "URGENT")
    else:
        _alert(f"IRA rebalance filled: {len(results)} order(s), regime {regime}.")
    return results, any(r["status"] == "submitted" for r in results)


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

    from . import ira_data, ira_gate, ira_planner
    try:
        planner = ira_planner.build_plan(
            db, user_id, snap["equity"],
            tactical_equity=ira_planner.equity_share_of(ira_data.bucket_exposure(tgt["target"])),
        )
    except Exception as exc:
        logger.warning("ira planner failed: %s", exc)
        planner = None
    gate = ira_gate.review(db, user_id, snap, tgt, plan, planner)
    if plan:
        try:
            from .claude_analyst import log_pre_trade
            log_pre_trade(db, "IRA", "IRA_REBALANCE", gate["verdict"], gate["reason"],
                          ira_gate.format_analysis(gate), MODE, user_id)
        except Exception as exc:
            logger.warning("ira gate log failed: %s", exc)

    results: list[dict] = []
    executed = False
    note = ""
    if plan and execute:
        if cfg["halted"]:
            note = "IRA trading is HALTED (kill switch) — plan saved, not executed."
        elif gate["verdict"] != "APPROVE":
            note = f"AI gate {gate['verdict']}: {gate['reason']} — plan saved, not executed."
        elif not _market_open(alp.get_client(MODE)):
            note = "Market closed — plan saved, not executed."
        else:
            results, executed = _execute_and_verify(db, user_id, plan, tgt["regime"])
        if not executed:
            _alert(f"IRA scheduled rebalance NOT executed: {note}", "INFO")
    elif snap["equity"] <= 0:
        note = "IRA account has no funds yet — nothing to allocate."
    elif not plan:
        note = "Portfolio within drift threshold — no trades needed."

    payload = {
        "regime": tgt["regime"], "baseline": tgt["baseline"], "target": tgt["target"],
        "plan": plan, "results": results, "equity": snap["equity"],
        "spendable": snap["spendable"], "note": note, "gate": gate,
        "ran_at": datetime.utcnow().isoformat(),
    }
    approved = bool(plan) and gate["verdict"] == "APPROVE" and not cfg["halted"]
    verdict = "EXECUTED" if executed else ("PROPOSED" if approved else (gate["verdict"] if plan else "HOLD"))
    proposal_id = db.execute(
        text("""
            INSERT INTO strategy_signals
                (user_id, strategy_name, action, data, reasoning, ai_verdict, ai_reasoning, mode, executed)
            VALUES (:uid, :name, :action, CAST(:data AS jsonb), :reasoning, :verdict, :ai, :mode, :ex)
            RETURNING id
        """),
        {"uid": user_id, "name": STRATEGY,
         "action": "REBALANCE" if plan else "HOLD",
         "data": json.dumps(payload, default=str),
         "reasoning": f"Regime: {tgt['regime']}. {len(plan)} trade(s) proposed.",
         "verdict": verdict, "ai": tgt["rationale"], "mode": MODE, "ex": executed},
    ).scalar()
    db.commit()
    return {**payload, "executed": executed, "rationale": tgt["rationale"], "planner": planner,
            "proposal_id": proposal_id if approved else None, "halted": cfg["halted"]}


MAX_PROPOSAL_AGE_S = 30 * 60


def execute_proposal(db: Session, user_id: int, proposal_id: int) -> dict:
    """Execute exactly the plan the user previewed, after re-validating it against the live account.

    Raises ValueError with a short code when the proposal can't safely run. The row is claimed
    atomically first, so a double-click or retry can never submit the orders twice.
    """
    cfg = get_config(db, user_id)
    if cfg["halted"]:
        raise ValueError("ira_halted: trading is halted (kill switch)")
    row = db.execute(
        text("""SELECT data, ai_verdict, executed,
                       EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_s
                FROM strategy_signals
                WHERE id = :id AND user_id = :uid AND strategy_name = :n"""),
        {"id": proposal_id, "uid": user_id, "n": STRATEGY},
    ).fetchone()
    if not row:
        raise ValueError("proposal_not_found")
    data, verdict, was_executed, age_s = row
    if isinstance(data, str):
        data = json.loads(data)
    if was_executed or verdict != "PROPOSED":
        raise ValueError(f"proposal_not_available: status {verdict}")
    if age_s > MAX_PROPOSAL_AGE_S:
        raise ValueError("proposal_expired: preview again (proposals expire after 30 minutes)")
    if (data.get("gate") or {}).get("verdict") != "APPROVE":
        raise ValueError("proposal_not_approved: the AI gate did not approve this plan")

    snap = get_snapshot(db, user_id)
    prev_eq = data.get("equity") or 0.0
    if prev_eq <= 0 or abs(snap["equity"] - prev_eq) / prev_eq > 0.03:
        raise ValueError("account_changed: equity moved more than 3% since the preview; preview again")
    held = {h["symbol"]: h["market_value"] for h in snap["holdings"]}
    plan = [dict(o) for o in data.get("plan", [])]
    for o in plan:
        if o["side"] == "sell" and held.get(o["symbol"], 0.0) < 0.9 * o["notional"]:
            raise ValueError(f"account_changed: {o['symbol']} holding no longer matches the preview")
    buys = [o for o in plan if o["side"] == "buy"]
    total_buy = sum(o["notional"] for o in buys)
    if total_buy > snap["spendable"] + 0.01:
        scale = snap["spendable"] / total_buy if total_buy else 0.0
        for o in buys:
            o["notional"] = round(o["notional"] * scale, 2)
        plan = [o for o in plan if o["side"] == "sell" or o["notional"] >= MIN_TRADE_USD]
    if not plan:
        raise ValueError("nothing_to_execute")
    if not _market_open(alp.get_client(MODE)):
        raise ValueError("market_closed: orders can only be placed while the market is open")

    claimed = db.execute(
        text("""UPDATE strategy_signals SET ai_verdict = 'EXECUTING'
                WHERE id = :id AND user_id = :uid AND ai_verdict = 'PROPOSED' RETURNING id"""),
        {"id": proposal_id, "uid": user_id},
    ).fetchone()
    db.commit()
    if not claimed:
        raise ValueError("proposal_not_available: already executing or executed")

    try:
        results, executed = _execute_and_verify(db, user_id, plan, data.get("regime", ""))
    except Exception as exc:
        db.execute(text("UPDATE strategy_signals SET ai_verdict = 'FAILED' WHERE id = :id"), {"id": proposal_id})
        db.commit()
        _alert(f"IRA rebalance crashed mid-execution: {str(exc)[:200]}. Check Alpaca before retrying.", "URGENT")
        raise
    data.update({"plan": plan, "results": results, "executed_at": datetime.utcnow().isoformat()})
    final = "EXECUTED" if executed and not _problems(results) else ("PARTIAL" if executed else "FAILED")
    db.execute(
        text("""UPDATE strategy_signals SET data = CAST(:d AS jsonb), ai_verdict = :v, executed = :ex
                WHERE id = :id"""),
        {"d": json.dumps(data, default=str), "v": final, "ex": executed, "id": proposal_id},
    )
    db.commit()
    return {**data, "executed": executed, "final_status": final, "problems": _problems(results)}


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
