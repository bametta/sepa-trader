"""
IRA AI gate — final review before a rebalance may execute.

Deterministic checks run first and can only BLOCK; the AI review can also only
BLOCK, never loosen a hard check. If the AI is unavailable the verdict is
UNAVAILABLE, and execution is refused (fail-closed: this is a long-horizon
real-money account, unlike the swing gate which fails open).
"""
import json
import logging
import re

from . import ira_data
from .database import get_user_setting

logger = logging.getLogger(__name__)

DEFAULTS = {"ira_max_expense_pct": 0.25, "ira_max_turnover_pct": 30.0, "ira_max_stock_pct": 8.0}


def _cfg(db, uid) -> dict:
    out = {}
    for k, d in DEFAULTS.items():
        try:
            out[k] = float(get_user_setting(db, k, str(d), uid) or d)
        except ValueError:
            out[k] = d
    return out


def _extract_json(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def review(db, user_id: int, snapshot: dict, tgt: dict, plan: list[dict],
           planner: dict | None = None) -> dict:
    cfg = _cfg(db, user_id)
    equity = snapshot["equity"] or 0.0
    target = tgt["target"]
    held_syms = {h["symbol"] for h in snapshot["holdings"]}
    syms = sorted(set(target) | held_syms | {o["symbol"] for o in plan})
    stats = ira_data.market_stats(syms)

    held_w = {h["symbol"]: h["weight"] for h in snapshot["holdings"]}
    metrics = {
        "blended_expense_pct": round(ira_data.blended_expense_ratio(target), 3),
        "current_expense_pct": round(ira_data.blended_expense_ratio(held_w), 3) if held_w else None,
        "bucket_exposure": ira_data.bucket_exposure(target),
        "cheaper_alternatives": ira_data.cheaper_alternatives(target),
        "lookthrough": ira_data.lookthrough(target),
        "market_stats": stats,
    }

    sells = sum(o["notional"] for o in plan if o["side"] == "sell")
    invested = sum(h["market_value"] for h in snapshot["holdings"])
    initial_deploy = equity > 0 and invested < 0.05 * equity
    metrics["turnover_pct"] = round(100 * sells / equity, 1) if equity else 0.0
    metrics["initial_deployment"] = initial_deploy

    checks: list[dict] = []

    def add(code, severity, detail):
        checks.append({"code": code, "severity": severity, "detail": detail})

    needed = [s for s, w in target.items() if w > 0] + [o["symbol"] for o in plan]
    bad = sorted({s for s in needed if not stats.get(s) or stats[s]["stale"]})
    if bad:
        add("DATA_STALE_OR_MISSING", "block", f"No fresh price data for: {', '.join(bad)}")
    if metrics["blended_expense_pct"] > cfg["ira_max_expense_pct"]:
        add("EXPENSE_CAP", "block",
            f"Blended expense {metrics['blended_expense_pct']}% > cap {cfg['ira_max_expense_pct']}%")
    if not initial_deploy and metrics["turnover_pct"] > cfg["ira_max_turnover_pct"]:
        add("TURNOVER_CAP", "block",
            f"One-way sell turnover {metrics['turnover_pct']}% of equity > cap {cfg['ira_max_turnover_pct']}%")
    top = (metrics["lookthrough"]["top_stocks"] or [{}])[0]
    if top and top.get("lookthrough_pct", 0) > cfg["ira_max_stock_pct"]:
        add("STOCK_CONCENTRATION", "block",
            f"{top['symbol']} look-through {top['lookthrough_pct']}% > cap {cfg['ira_max_stock_pct']}%")
    for p in metrics["lookthrough"]["pair_overlap"]:
        if p["top10_overlap_pct"] >= 25:
            add("FUND_OVERLAP", "warn",
                f"{p['a']}/{p['b']} top-10 holdings overlap {p['top10_overlap_pct']}%")
    for alt in metrics["cheaper_alternatives"]:
        add("CHEAPER_ALTERNATIVE", "info",
            f"{alt['symbol']} ({alt['expense_ratio']}%) -> {alt['alt']} ({alt['alt_expense_ratio']}%)")
    risk = [s for s in target if target[s] > 0 and s not in ("BND", "AGG", "TIP", "BIL") and stats.get(s)]
    below = [s for s in risk if stats[s]["above_200sma"] is False]
    if risk and len(below) / len(risk) >= 0.7:
        add("MARKET_STRESS", "warn", f"{len(below)}/{len(risk)} risk ETFs are below their 200-day average")

    profile_line = "Investor profile: not set (age/retirement goal unknown)."
    if planner and planner.get("glidepath_equity") is not None:
        gap = planner.get("glidepath_gap")
        profile_line = (f"Investor profile: age {planner['profile']['age']}, retire at "
                        f"{planner['profile']['retire_age']}, risk {planner['profile']['risk']}; "
                        f"age-appropriate equity share ~{planner['glidepath_equity']:.0%}, "
                        f"target equity share {planner.get('tactical_equity')}.")
        if gap is not None and abs(gap) > 0.15:
            add("GLIDEPATH_GAP", "warn",
                f"Target equity share is {gap:+.0%} vs the age/risk glidepath ({planner['glidepath_equity']:.0%})")
    metrics["planner"] = {k: planner.get(k) for k in ("glidepath_equity", "tactical_equity", "glidepath_gap")} if planner else None

    hard = [c for c in checks if c["severity"] == "block"]
    result = {"verdict": None, "reason": "", "checks": checks, "metrics": metrics, "ai": None, "limits": cfg}

    if not plan:
        result.update(verdict="NO_TRADES", reason="No trades proposed.")
        return result
    if hard:
        result.update(verdict="BLOCK", reason="; ".join(c["detail"] for c in hard))
        return result

    from .claude_analyst import _call_ai
    compact = {s: {k: v[k] for k in ("ret_1m", "ret_3m", "ret_12m", "vol_20d", "pct_off_52w_high", "above_200sma")}
               for s, v in stats.items() if v}
    prompt = (
        "You are the final risk gate for a long-horizon Roth IRA that rebalances ETFs. "
        "Hard checks (data freshness, expense cap, turnover cap, concentration) already passed. "
        "Decide whether this rebalance should execute now.\n"
        "BLOCK only if the plan is clearly harmful for a long-horizon investor: e.g. it sells into a "
        "panic that the rules likely misread, creates needless churn, or the data looks wrong. "
        "Do not block just because markets are down.\n\n"
        f"{profile_line}\n"
        f"Regime: {tgt['regime']}  Trend overlay halved: {tgt.get('trend_flagged', [])}\n"
        f"Equity: {equity:.0f}  Initial deployment: {initial_deploy}\n"
        f"Plan: {json.dumps([{k: o[k] for k in ('side','symbol','notional','drift_pct')} for o in plan])}\n"
        f"Target: {json.dumps(target)}\n"
        f"Blended expense: {metrics['blended_expense_pct']}%  Turnover: {metrics['turnover_pct']}%\n"
        f"Look-through top stocks: {json.dumps(metrics['lookthrough']['top_stocks'][:5])}\n"
        f"Fund overlap: {json.dumps(metrics['lookthrough']['pair_overlap'][:3])}\n"
        f"Warnings: {json.dumps([c['detail'] for c in checks if c['severity'] == 'warn'])}\n"
        f"Live ETF stats: {json.dumps(compact)}\n\n"
        'Reply with JSON only: {"verdict": "APPROVE" or "BLOCK", "reason": "1-3 sentences", "concerns": ["..."]}'
    )
    try:
        raw = _call_ai(db, prompt, 1500, user_id)
    except Exception as exc:
        logger.error("ira gate AI call failed: %s", exc)
        result.update(verdict="UNAVAILABLE", reason=f"AI gate unavailable ({str(exc)[:80]}); not executing.")
        return result
    data = _extract_json(raw) if raw else None
    if not data or str(data.get("verdict", "")).upper() not in ("APPROVE", "BLOCK"):
        result.update(verdict="UNAVAILABLE", reason="AI gate returned no usable verdict; not executing.")
        return result
    result["ai"] = {"verdict": str(data["verdict"]).upper(), "reason": str(data.get("reason", ""))[:600],
                    "concerns": [str(c)[:200] for c in (data.get("concerns") or [])][:5]}
    result.update(verdict=result["ai"]["verdict"], reason=result["ai"]["reason"])
    return result


def format_analysis(gate: dict) -> str:
    lines = [f"Blended expense: {gate['metrics']['blended_expense_pct']}%  "
             f"Turnover: {gate['metrics']['turnover_pct']}%"]
    lines += [f"[{c['severity'].upper()}] {c['code']}: {c['detail']}" for c in gate["checks"]]
    if gate.get("ai"):
        lines += ["AI concerns: " + "; ".join(gate["ai"]["concerns"])] if gate["ai"]["concerns"] else []
    return "\n".join(lines)
