"""
AI Strategist — the central decision engine across all strategies.

Role:
  1. Receives the market environment assessment and all active strategy signals.
  2. Calls the user's configured AI provider with full context.
  3. Returns a single actionable decision: which strategy to follow,
     what to hold, and whether to execute now.

Fails open — if no API key is configured the strategist returns the
highest-fit strategy's signal based on the static STRATEGY_FIT table.
"""
import logging
from sqlalchemy.orm import Session
from .market_env import STRATEGY_FIT
from ..database import get_user_setting
from ..claude_analyst import _call_ai

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
You are the AI portfolio manager for a systematic multi-strategy trading system.
Your job is to synthesize the market regime and strategy signals into ONE final decision.

Respond in EXACTLY this format — no other text:
DECISION: <EXECUTE|HOLD|WAIT>
STRATEGY: <dual_momentum|sepa|none>
SYMBOL: <ticker or NONE>
RISK: <LOW|MEDIUM|HIGH>
REASONING: <one sentence, ≤25 words>

═══ MARKET ENVIRONMENT ═══
Regime:          {environment}
Description:     {description}
SPY price:       ${spy_price} ({spy_vs_200})
VIX:             {vix}
SPY 20d return:  {spy_20d}%

═══ ACTIVE STRATEGY SIGNALS ═══
{strategy_signals_block}

═══ CURRENT PORTFOLIO ═══
{portfolio_block}

═══ DECISION RULES ═══
• EXECUTE: Signal is clear, market supports it, risk is acceptable.
• HOLD:    Already in the correct position — no change needed.
• WAIT:    Signal is weak, market is mixed, or risk is elevated.
• Use NONE for STRATEGY when WAIT/HOLD with no active trade.
• Prefer SEPA in BULL markets, Dual Momentum in all regimes for stability.
• Never recommend executing when VIX > 35.
"""


def _format_strategy_signals(signals: list[dict]) -> str:
    if not signals:
        return "  (no strategy signals available)"
    lines = []
    for s in signals:
        name   = s.get("strategy_name", "?")
        symbol = s.get("recommended_symbol", "?")
        action = s.get("action", "?")
        reason = s.get("reasoning", "")[:120]
        lines.append(f"  [{name}]  → {action} {symbol}\n    {reason}")
    return "\n".join(lines)


def _format_portfolio(portfolio: dict) -> str:
    if not portfolio:
        return "  No open positions"
    lines = []
    for sym, info in portfolio.items():
        pnl = info.get("unrealized_pl", 0)
        lines.append(f"  {sym}: {info.get('qty', '?')} shares  P&L ${pnl:+.2f}")
    return "\n".join(lines) if lines else "  No open positions"


def _default_decision(market_env: dict, signals: list[dict]) -> dict:
    """Fallback when no AI key is configured — use static fit table."""
    env    = market_env.get("environment", "UNKNOWN")
    scores = STRATEGY_FIT.get(env, STRATEGY_FIT["UNKNOWN"])

    # Pick the best fitting active strategy from the signals provided
    active_names = {s["strategy_name"] for s in signals}
    if not active_names:
        return {
            "decision": "WAIT",
            "strategy": "none",
            "symbol":   None,
            "risk":     "MEDIUM",
            "reasoning": f"No active strategies configured for regime {env}.",
            "ai_used":  False,
        }

    best = max(active_names, key=lambda n: scores.get(n, 0))
    sig  = next((s for s in signals if s["strategy_name"] == best), None)

    return {
        "decision": "EXECUTE" if sig and sig.get("action") != "HOLD" else "HOLD",
        "strategy": best,
        "symbol":   sig.get("recommended_symbol") if sig else None,
        "risk":     "MEDIUM",
        "reasoning": f"Rule-based: best fit for {env} regime is {best}.",
        "ai_used":  False,
    }


def _parse_ai_response(text: str) -> dict:
    result = {
        "decision": "WAIT",
        "strategy": "none",
        "symbol":   None,
        "risk":     "MEDIUM",
        "reasoning": "",
        "ai_used":  True,
    }
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("DECISION:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("EXECUTE", "HOLD", "WAIT"):
                result["decision"] = v
        elif line.startswith("STRATEGY:"):
            result["strategy"] = line.split(":", 1)[1].strip().lower()
        elif line.startswith("SYMBOL:"):
            s = line.split(":", 1)[1].strip().upper()
            result["symbol"] = None if s == "NONE" else s
        elif line.startswith("RISK:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("LOW", "MEDIUM", "HIGH"):
                result["risk"] = v
        elif line.startswith("REASONING:"):
            result["reasoning"] = line.split(":", 1)[1].strip()
    return result


def decide(
    db: Session,
    market_env: dict,
    strategy_signals: list[dict],
    portfolio: dict,
    user_id: int = None,
) -> dict:
    """
    Main entry point for the AI strategist.

    Parameters
    ----------
    market_env       : output of market_env.assess()
    strategy_signals : list of strategy signal dicts from the DB
    portfolio        : {symbol: {qty, unrealized_pl}} of current positions
    user_id          : for per-user AI credentials

    Returns
    -------
    {decision, strategy, symbol, risk, reasoning, ai_used}
    """
    # Fail-open: if no API key, use rule-based fallback
    if not get_user_setting(db, "ai_api_key", "", user_id):
        logger.info("ai_strategist: no API key — using rule-based fallback")
        return _default_decision(market_env, strategy_signals)

    env = market_env.get("environment", "UNKNOWN")
    spy_price  = market_env.get("spy_price")
    spy_200sma = market_env.get("spy_200sma")
    spy_vs_200 = (
        f"{'above' if market_env.get('spy_above_200') else 'below'} 200SMA (${spy_200sma})"
        if spy_price and spy_200sma else "data unavailable"
    )

    prompt = _PROMPT_TEMPLATE.format(
        environment             = env,
        description             = market_env.get("description", ""),
        spy_price               = spy_price or "N/A",
        spy_vs_200              = spy_vs_200,
        vix                     = market_env.get("vix", "N/A"),
        spy_20d                 = market_env.get("spy_20d_return", "N/A"),
        strategy_signals_block  = _format_strategy_signals(strategy_signals),
        portfolio_block         = _format_portfolio(portfolio),
    )

    try:
        text = _call_ai(db, prompt, max_tokens=256, user_id=user_id)
        if text is None:
            return _default_decision(market_env, strategy_signals)
        result = _parse_ai_response(text.strip())
        logger.info(
            "ai_strategist: %s → %s %s (risk=%s)",
            env, result["decision"], result["symbol"] or "—", result["risk"],
        )
        return result
    except Exception as exc:
        logger.error("ai_strategist: AI call failed (%s) — falling back", exc)
        return _default_decision(market_env, strategy_signals)
