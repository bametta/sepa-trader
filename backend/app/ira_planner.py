"""
IRA retirement planner: age/goal profile -> glidepath, Monte Carlo projection,
and contribution recommendations. All figures are in today's (real) dollars.

Planning aid only, not financial advice. Return/volatility inputs are long-run
assumptions (ASSUMPTIONS below), not forecasts, and IRS figures change yearly.
"""
from datetime import date

import numpy as np

from .database import get_user_setting

# Roth IRA limits - verify each year at irs.gov.
LIMITS = {"year": 2026, "base": 7500.0, "catch_up_50": 1100.0,
          "note": "Roth eligibility phases out with income (2026: roughly $153k-$168k single, "
                  "$242k-$252k joint). Confirm current figures at irs.gov."}

# Long-run REAL return / volatility assumptions per asset class.
ASSUMPTIONS = {
    "equity": (0.050, 0.16),
    "bonds":  (0.015, 0.055),
    "gold":   (0.010, 0.15),
    "cash":   (0.005, 0.01),
}
_CORR = {("equity", "bonds"): 0.10, ("equity", "gold"): 0.05, ("bonds", "gold"): 0.20}
SUCCESS_TARGET = 0.80
EQUITY_BUCKETS = {"us_large", "us_midsmall", "intl_dev", "intl_em", "reit"}


def get_profile(db, uid: int) -> dict:
    g = lambda k, d="": get_user_setting(db, k, d, uid)

    def num(k):
        try:
            v = float(g(k) or 0)
            return v if v > 0 else None
        except ValueError:
            return None

    birth = num("ira_birth_year")
    risk = (g("ira_risk", "moderate") or "moderate").lower()
    retire = int(num("ira_retire_age") or 65)
    age = (date.today().year - int(birth)) if birth else None
    return {
        "age": age, "birth_year": int(birth) if birth else None,
        "retire_age": retire, "years_to_retire": (retire - age) if age is not None else None,
        "risk": risk if risk in ("conservative", "moderate", "aggressive") else "moderate",
        "target_amount": num("ira_target_amount"), "target_income": num("ira_target_income"),
        "planned_contribution": num("ira_annual_contribution"), "annual_income": num("ira_annual_income"),
    }


def contribution_limit(age: int | None) -> float:
    return LIMITS["base"] + (LIMITS["catch_up_50"] if age is not None and age >= 50 else 0.0)


def glidepath_equity(age: int, retire_age: int, risk: str) -> float:
    """Rule-of-thumb strategic equity share: ~(110 - age), tilted by risk, de-risked near retirement."""
    base = 110 - age + {"conservative": -10, "moderate": 0, "aggressive": 10}[risk]
    if retire_age - age <= 5:
        base -= 10
    return round(min(0.95, max(0.25, base / 100)), 2)


def strategic_mix(equity: float) -> dict:
    """Strategic asset-class mix for a given equity share."""
    rest = 1 - equity
    return {"equity": equity, "bonds": rest * 0.75, "gold": rest * 0.10 if rest > 0.2 else 0.0,
            "cash": rest - rest * 0.75 - (rest * 0.10 if rest > 0.2 else 0.0)}


def mix_stats(mix: dict) -> tuple[float, float]:
    """Expected real return and volatility of an asset-class mix."""
    classes = list(mix)
    mu = sum(mix[c] * ASSUMPTIONS[c][0] for c in classes)
    var = 0.0
    for a in classes:
        for b in classes:
            rho = 1.0 if a == b else _CORR.get((a, b)) or _CORR.get((b, a)) or 0.0
            var += mix[a] * mix[b] * ASSUMPTIONS[a][1] * ASSUMPTIONS[b][1] * rho
    return mu, var ** 0.5


def equity_share_of(bucket_exposure: dict) -> float:
    return round(sum(v for b, v in bucket_exposure.items() if b in EQUITY_BUCKETS), 3)


def _simulate(balance: float, annual: float, years: int, mu: float, sigma: float,
              n: int = 4000, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    log_mu = np.log(1 + mu) - 0.5 * (sigma / (1 + mu)) ** 2
    log_sd = sigma / (1 + mu)
    bal = np.full(n, float(balance))
    for _ in range(years):
        bal = (bal + annual) * np.exp(rng.normal(log_mu, log_sd, n))
    return bal


def _prob(target, balance, annual, years, mu, sigma) -> float:
    return float((_simulate(balance, annual, years, mu, sigma) >= target).mean())


def build_plan(db, uid: int, balance: float, tactical_equity: float | None = None) -> dict:
    p = get_profile(db, uid)
    out = {"profile": p, "limits": LIMITS, "assumptions": {k: {"real_return": v[0], "volatility": v[1]}
                                                          for k, v in ASSUMPTIONS.items()},
           "ready": False, "missing": []}
    if p["age"] is None:
        out["missing"].append("ira_birth_year")
    if not (p["target_amount"] or p["target_income"]):
        out["missing"].append("ira_target_amount or ira_target_income")
    if p["age"] is None or p["years_to_retire"] is None or p["years_to_retire"] < 1:
        out["note"] = "Set your birth year and a retirement age after today to enable planning."
        return out

    age, years = p["age"], p["years_to_retire"]
    limit = contribution_limit(age)
    glide = glidepath_equity(age, p["retire_age"], p["risk"])
    mix = strategic_mix(glide)
    mu, sigma = mix_stats(mix)
    out.update(contribution_limit=limit, glidepath_equity=glide, strategic_mix={k: round(v, 3) for k, v in mix.items()},
               expected_real_return=round(mu, 4), expected_volatility=round(sigma, 4))
    if tactical_equity is not None:
        out["tactical_equity"] = tactical_equity
        out["glidepath_gap"] = round(tactical_equity - glide, 3)

    target = p["target_amount"] or (p["target_income"] / 0.04 if p["target_income"] else None)
    out["target"] = {"amount": round(target) if target else None,
                     "basis": "target_amount" if p["target_amount"] else ("income / 4% withdrawal rule" if target else None)}

    def scenario(annual):
        end = _simulate(balance, annual, years, mu, sigma)
        s = {"annual": round(annual), "monthly": round(annual / 12),
             "pct_of_limit": round(annual / limit, 3),
             "p10": round(float(np.percentile(end, 10))), "p50": round(float(np.percentile(end, 50))),
             "p90": round(float(np.percentile(end, 90)))}
        if p["annual_income"]:
            s["pct_of_income"] = round(annual / p["annual_income"], 4)
        if target:
            s["prob_reach_target"] = round(float((end >= target).mean()), 3)
        return s

    levels = sorted({round(limit * f) for f in (0.25, 0.5, 0.75, 1.0)} |
                    ({round(p["planned_contribution"])} if p["planned_contribution"] else set()))
    out["scenarios"] = [scenario(a) for a in levels]

    if target:
        if _prob(target, balance, 0, years, mu, sigma) >= SUCCESS_TARGET:
            rec, kind = 0.0, "already_on_track"
        elif _prob(target, balance, limit, years, mu, sigma) >= SUCCESS_TARGET:
            lo, hi = 0.0, limit
            for _ in range(18):
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if _prob(target, balance, mid, years, mu, sigma) >= SUCCESS_TARGET else (mid, hi)
            rec, kind = hi, "within_ira_limit"
        else:
            rec, kind = limit, "limit_not_enough"
        r = {"kind": kind, "success_threshold": SUCCESS_TARGET, "annual": round(rec),
             "monthly": round(rec / 12), "pct_of_limit": round(rec / limit, 3)}
        if p["annual_income"]:
            r["pct_of_income"] = round(rec / p["annual_income"], 4)
        if kind == "limit_not_enough":
            r["prob_at_limit"] = round(_prob(target, balance, limit, years, mu, sigma), 3)
            lo, hi = limit, limit * 8
            for _ in range(18):
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if _prob(target, balance, mid, years, mu, sigma) >= SUCCESS_TARGET else (mid, hi)
            r["total_annual_saving_needed"] = round(hi)
            r["extra_outside_ira"] = round(hi - limit)
            r["note"] = ("Maxing the IRA is not enough for an 80% chance. Close the gap with savings in a "
                         "401(k)/taxable account, a later retirement age, or a lower target.")
        out["recommendation"] = r
        try:
            from . import ira_stress
            assumed = p["planned_contribution"] if p["planned_contribution"] else r["annual"]
            out["stress"] = {
                "contribution_assumed": round(assumed),
                "scenarios": ira_stress.scenario_projection(balance, assumed, years, mix, target),
                "withdrawal": ira_stress.withdrawal_test(target, p["retire_age"], p["risk"], 0.04),
            }
        except Exception as exc:  # stress output is supplementary; never break the plan
            out["stress"] = {"error": str(exc)[:120]}
    out["ready"] = True
    out["missing"] = [m for m in out["missing"] if not (m.startswith("ira_target") and target)]
    out["disclaimer"] = ("Planning aid, not financial advice. Real (inflation-adjusted) dollars; "
                         "returns are long-run assumptions, not forecasts.")
    return out
