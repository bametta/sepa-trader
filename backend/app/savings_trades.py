"""
Manual-trade helper: exactly what to buy or sell in each account so the accounts together move toward a
target allocation, in whole shares, using only each account's own cash and holdings.

It never places an order. You place the trades yourself, so treat the output as a worksheet and check your
plan's rules (fees, trading restrictions, settlement) first. Planning aid only, not financial advice.

Asset location: the target is for all selected accounts combined, then split across accounts so bonds and cash
sit in the pre-tax account(s) and growth assets in the Roth account(s) where capacity allows (tax-free growth
is worth most in the Roth). It never moves money between accounts, so each account keeps its own total.
"""
import math
import re

from .savings import LIMITS, TECH_THEME, exposure

PRETAX_CLASSES = {"bonds_agg", "bonds_tips", "cash"}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
MAX_TARGET_SYMBOLS = 12
TAX_GROUPS = {"roth": "roth", "pre_tax": "pre_tax"}


# ── Target model ──────────────────────────────────────────────────────────────

def parse_target_model(text: str) -> dict:
    """'VTI:50, VXUS:25, BND:25' (percent or fractions that add to ~100 or ~1) -> normalised weights."""
    parts = [p.strip() for p in re.split(r"[,\n;]+", text or "") if p.strip()]
    if not parts:
        raise ValueError("target model is empty")
    if len(parts) > MAX_TARGET_SYMBOLS:
        raise ValueError(f"at most {MAX_TARGET_SYMBOLS} funds")
    weights = {}
    for p in parts:
        m = re.match(r"^([A-Za-z0-9.\-]+)\s*[:=]\s*([0-9]*\.?[0-9]+)\s*%?$", p)
        if not m:
            raise ValueError(f"can't read '{p}'. Use SYMBOL:percent, for example VTI:50")
        sym, w = m.group(1).upper(), float(m.group(2))
        if not SYMBOL_RE.match(sym):
            raise ValueError(f"'{sym}' is not a valid ticker")
        if w <= 0:
            raise ValueError(f"{sym} needs a weight above zero")
        if sym in weights:
            raise ValueError(f"{sym} is listed twice")
        weights[sym] = w
    total = sum(weights.values())
    if not (0.99 <= total <= 1.01 or 99.0 <= total <= 101.0):
        raise ValueError(f"weights add up to {total:g}; they should add to 100 (or 1)")
    return {s: w / total for s, w in weights.items()}


def default_target(equity_share: float) -> dict:
    """Plain three-fund default from the age-based equity share: US 70% / international 30% of equity."""
    e = min(max(equity_share, 0.0), 1.0)
    out = {"VTI": 0.7 * e, "VXUS": 0.3 * e, "BND": 1.0 - e}
    return {s: w for s, w in out.items() if w > 0.0005}


def format_target(weights: dict) -> str:
    return ", ".join(f"{s}:{w * 100:.1f}".rstrip("0").rstrip(".") for s, w in
                     sorted(weights.items(), key=lambda kv: -kv[1]))


# ── Planning ──────────────────────────────────────────────────────────────────

def _prefers_pretax(symbol: str) -> bool:
    exp = exposure({"symbol": symbol, "asset_class": "auto"})
    top = max(exp, key=exp.get)
    return top in PRETAX_CLASSES


def _split_account(acct: dict, prices: dict):
    """Tradable positions (symbol + quantity + price) and cash; anything else is excluded."""
    pos, cash, excluded = {}, 0.0, []
    for h in acct["holdings"]:
        sym = (h.get("symbol") or "").upper() or None
        if h.get("asset_class") == "cash":
            cash += float(h.get("value") or 0.0)
        elif sym and h.get("quantity") is not None and prices.get(sym):
            pos[sym] = pos.get(sym, 0.0) + float(h["quantity"])
        else:
            excluded.append({"account": acct["name"], "name": h.get("name") or sym,
                             "value": round(float(h.get("value") or 0.0), 2)})
    return pos, cash, excluded


def _assign_targets(accounts, target_dollars, capacity):
    """Split each symbol's target dollars across accounts by tax location. Returns {account_id: {symbol: $}}."""
    remaining = dict(capacity)
    assigned = {a["id"]: {} for a in accounts}
    pretax_ids = [a["id"] for a in accounts if a["tax"] == "pre_tax"]
    roth_ids = [a["id"] for a in accounts if a["tax"] == "roth"]
    order = lambda s: (0 if _prefers_pretax(s) else 1, -target_dollars[s])
    for sym in sorted(target_dollars, key=order):
        need = target_dollars[sym]
        prefer = (pretax_ids + roth_ids) if _prefers_pretax(sym) else (roth_ids + pretax_ids)
        for aid in prefer:
            if need <= 1e-9:
                break
            put = min(need, remaining[aid])
            if put > 0:
                assigned[aid][sym] = assigned[aid].get(sym, 0.0) + put
                remaining[aid] -= put
                need -= put
    return assigned


def plan_trades(accounts: list[dict], prices: dict, target: dict, mode: str = "cash_only",
                min_trade: float = 25.0, drift_threshold: float = 0.03, outside_total: float = 0.0,
                outside_tech: float = 0.0, tech_limit: float | None = None) -> dict:
    """accounts: [{id, name, tax('roth'|'pre_tax'), holdings:[{symbol, quantity, value, asset_class, name}]}].
    mode 'cash_only' buys with each account's cash and never sells; 'full' also sells overweight holdings.
    Buys of tech/semiconductor/AI funds are capped so those funds stay within `tech_limit` (default 30%) of
    everything tracked: the selected accounts plus `outside_total` dollars elsewhere, of which `outside_tech`
    is already in tech funds. Cash that can't go to tech goes to the other funds, or stays in cash."""
    limit = LIMITS["tech_theme_pct"] if tech_limit is None else tech_limit
    if mode not in ("cash_only", "full"):
        raise ValueError("mode must be 'cash_only' or 'full'")
    accounts = [a for a in accounts if a["tax"] in TAX_GROUPS]
    if not accounts:
        raise ValueError("pick at least one Roth or pre-tax retirement account")
    prices = {s.upper(): p for s, p in prices.items() if p}
    notes, excluded = [], []
    unpriced = sorted(s for s in target if s not in prices)
    if unpriced:
        notes.append(f"No price for {', '.join(unpriced)}, so they were dropped from the target.")
    target = {s: w for s, w in target.items() if s in prices}
    if not target:
        raise ValueError("none of the target funds have a price")
    total_w = sum(target.values())
    target = {s: w / total_w for s, w in target.items()}

    state = {}
    for a in accounts:
        pos, cash, ex = _split_account(a, prices)
        excluded += ex
        state[a["id"]] = {"pos": pos, "cash": cash}
    cap = {a["id"]: sum(q * prices[s] for s, q in state[a["id"]]["pos"].items()) + state[a["id"]]["cash"]
           for a in accounts}
    total = sum(cap.values())
    if total <= 0:
        raise ValueError("these accounts hold no tradable positions or cash")
    held_syms = {s for st in state.values() for s in st["pos"]}
    off_target = sorted(held_syms - set(target))
    if off_target:
        notes.append(f"Held but not in the target model: {', '.join(off_target)}. "
                     + ("They will be sold." if mode == "full" else "They are left alone in cash-only mode."))

    target_dollars = {s: w * total for s, w in target.items()}
    assigned = _assign_targets(accounts, target_dollars, cap)

    def weights_now(sellmap=None, buymap=None):
        value = {}
        for aid, st in state.items():
            for s, q in st["pos"].items():
                value[s] = value.get(s, 0.0) + q * prices[s]
        return {s: v / total for s, v in value.items()}

    before = weights_now()
    results = []
    final_pos = {aid: dict(st["pos"]) for aid, st in state.items()}
    final_cash = {}
    held_back: dict[str, int] = {}

    def tech_room() -> float:
        """Dollars of tech-theme funds that can still be added across everything tracked."""
        inside = sum(q * prices[s] for pos in final_pos.values() for s, q in pos.items() if s in TECH_THEME)
        return limit * (total + outside_total) - outside_tech - inside
    for a in accounts:
        aid, st = a["id"], state[a["id"]]
        price = lambda s: prices[s]
        have = lambda s: st["pos"].get(s, 0.0) * prices.get(s, 0.0)
        goal = assigned[aid]
        trades, cash = [], st["cash"]
        if mode == "full":
            for s in sorted(set(st["pos"]) | set(goal)):
                delta = goal.get(s, 0.0) - have(s)
                if delta < 0 and (-delta >= max(min_trade, 0.005 * cap[aid]) or s not in target):
                    shares = min(math.floor(st["pos"].get(s, 0.0)), round(-delta / price(s)))
                    if s not in target:
                        shares = math.floor(st["pos"].get(s, 0.0))
                    if shares >= 1:
                        trades.append({"symbol": s, "side": "sell", "shares": int(shares), "price": round(price(s), 2),
                                       "value": round(shares * price(s), 2)})
                        final_pos[aid][s] = st["pos"][s] - shares
                        cash += shares * price(s)
        need = {s: goal.get(s, 0.0) - final_pos[aid].get(s, 0.0) * price(s) for s in goal}
        need = {s: v for s, v in need.items() if v >= max(min_trade, 0.0)}
        budget, total_need = cash, sum(need.values())
        for s in sorted(need, key=lambda x: -need[x]):
            alloc = need[s] if total_need <= budget else budget * need[s] / total_need
            shares = math.floor(alloc / price(s))
            if s in TECH_THEME and shares >= 1:
                allowed = max(0, math.floor(max(tech_room(), 0.0) / price(s)))
                if shares > allowed:
                    held_back[s] = held_back.get(s, 0) + shares - allowed
                    shares = allowed
            if shares >= 1:
                cost = shares * price(s)
                trades.append({"symbol": s, "side": "buy", "shares": int(shares), "price": round(price(s), 2),
                               "value": round(cost, 2)})
                final_pos[aid][s] = final_pos[aid].get(s, 0.0) + shares
                cash -= cost
        for _ in range(50):                              # use leftover cash, most underweight first
            gaps = {s: goal.get(s, 0.0) - final_pos[aid].get(s, 0.0) * price(s) for s in goal}
            room = tech_room()
            affordable = [s for s in gaps if gaps[s] >= 0.5 * price(s) and price(s) <= cash + 1e-9
                          and (s not in TECH_THEME or price(s) <= room)]
            if not affordable:
                break
            s = max(affordable, key=lambda x: gaps[x])
            trades.append({"symbol": s, "side": "buy", "shares": 1, "price": round(price(s), 2),
                           "value": round(price(s), 2)})
            final_pos[aid][s] = final_pos[aid].get(s, 0.0) + 1
            cash -= price(s)
        merged = {}
        for t in trades:                                  # combine repeated lines for the same symbol and side
            k = (t["symbol"], t["side"])
            if k in merged:
                merged[k]["shares"] += t["shares"]
                merged[k]["value"] = round(merged[k]["shares"] * t["price"], 2)
            else:
                merged[k] = dict(t)
        trades = sorted(merged.values(), key=lambda t: (t["side"] != "sell", -t["value"]))
        final_cash[aid] = cash
        results.append({"id": aid, "name": a["name"], "tax": a["tax"], "value": round(cap[aid], 2),
                        "cash_before": round(st["cash"], 2), "cash_after": round(max(cash, 0.0), 2), "trades": trades})

    after_val = {}
    for aid, pos in final_pos.items():
        for s, q in pos.items():
            if q > 0:
                after_val[s] = after_val.get(s, 0.0) + q * prices[s]
    after = {s: v / total for s, v in after_val.items()}
    drift = lambda w: max([abs(w.get(s, 0.0) - target.get(s, 0.0)) for s in set(w) | set(target)] or [0.0])
    if held_back:
        now = (outside_tech + sum(q * prices[s] for st in state.values() for s, q in st["pos"].items()
                                  if s in TECH_THEME)) / (total + outside_total)
        notes.append(f"Held back {', '.join(f'{n} share(s) of {s}' for s, n in sorted(held_back.items()))}: "
                     f"tech/semiconductor/AI funds are {now:.0%} of everything you track (limit {limit:.0%}). "
                     "That cash went to your other target funds or stays in cash.")
    no_trades = all(not r["trades"] for r in results)
    if no_trades:
        notes.append("Nothing to do: cash is below the smallest useful trade or you're within the target.")
    elif mode == "full" and drift(before) < drift_threshold and not any(r["cash_before"] > min_trade for r in results):
        notes.append(f"Your allocation is already within {drift_threshold:.0%} of the target; trading is optional.")
    return {
        "mode": mode, "total": round(total, 2), "target": {s: round(w, 4) for s, w in target.items()},
        "before": {s: round(w, 4) for s, w in sorted(before.items(), key=lambda kv: -kv[1])},
        "after": {s: round(w, 4) for s, w in sorted(after.items(), key=lambda kv: -kv[1])},
        "max_drift_before": round(drift(before), 4), "max_drift_after": round(drift(after), 4),
        "accounts": results, "excluded": excluded, "notes": notes,
    }
