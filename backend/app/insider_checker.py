"""
SEC EDGAR Form 4 insider-transaction fetcher.

Used by the AI pre-trade gate to detect significant insider selling before a
position is opened. Fails open on any network/parse error — missing data
never blocks a trade.

Architecture
------------
1. Look up the company's SEC CIK from the tickers JSON (cached 24 h).
2. Fetch the submissions JSON for that CIK (cached 6 h per symbol).
3. Identify Form 4 filings filed in the last `days` calendar days.
4. Fetch and parse the primary XML document for up to MAX_FILINGS filings.
5. Return a structured summary for injection into the AI prompt.

Rate limits
-----------
SEC EDGAR allows ~10 requests/second; we stay well below that.
All outbound requests include a proper User-Agent as required by SEC policy.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

# SEC requires a User-Agent with contact info
_UA = "sepa-trader/1.0 (contact: njohbillie@gmail.com)"
_HEADERS = {"User-Agent": _UA, "Accept-Encoding": "gzip"}
_TIMEOUT = 8.0       # seconds per request
_MAX_FILINGS = 5     # max Form 4 docs to parse (keeps gate latency low)

# ── In-memory caches ─────────────────────────────────────────────────────────
# Ticker → CIK map: refreshed once every 24 h (the file rarely changes)
_ticker_cache: dict = {}          # {ticker: cik_str}
_ticker_cache_ts: datetime | None = None
_TICKER_CACHE_TTL = timedelta(hours=24)

# Per-symbol insider result: {symbol: (fetched_at, result_dict)}
_result_cache: dict[str, tuple[datetime, dict]] = {}
_RESULT_CACHE_TTL = timedelta(hours=6)


# ── CIK lookup ────────────────────────────────────────────────────────────────

def _get_cik(symbol: str) -> str | None:
    """Return the zero-padded 10-digit CIK string for a ticker, or None."""
    global _ticker_cache, _ticker_cache_ts

    now = datetime.now(timezone.utc)
    if _ticker_cache_ts is None or (now - _ticker_cache_ts) > _TICKER_CACHE_TTL:
        try:
            resp = httpx.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers=_HEADERS,
                timeout=_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
            raw = resp.json()
            _ticker_cache = {
                v["ticker"].upper(): f"{v['cik_str']:0>10}"
                for v in raw.values()
            }
            _ticker_cache_ts = now
        except Exception as exc:
            logger.debug("insider_checker: CIK map fetch failed: %s", exc)
            return None

    return _ticker_cache.get(symbol.upper())


# ── Form 4 XML parsing ───────────────────────────────────────────────────────

def _tag(el: ET.Element, tag: str) -> str:
    """Extract text from a child tag, stripping namespace prefixes."""
    for child in el.iter():
        if child.tag.split("}")[-1] == tag and child.text:
            return child.text.strip()
    return ""


def _parse_form4(xml_text: str, filed_date: str) -> list[dict]:
    """
    Parse a Form 4 XML document.
    Returns a list of transaction dicts:
      {
        owner_name: str, is_director: bool, is_officer: bool,
        title: str, code: "A"|"D", shares: float, price: float,
        value: float, date: str, filed: str,
      }
    """
    transactions = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.debug("insider_checker: XML parse error: %s", exc)
        return []

    # Owner info (may be multiple reporters, but Form 4 is usually one per doc)
    owners = []
    for rpt in root.iter():
        if rpt.tag.split("}")[-1] == "reportingOwner":
            name  = _tag(rpt, "rptOwnerName") or _tag(rpt, "reportingOwnerName") or "Unknown"
            is_dir = _tag(rpt, "isDirector") == "1"
            is_off = _tag(rpt, "isOfficer") == "1"
            title  = _tag(rpt, "officerTitle")
            owners.append({"name": name, "is_director": is_dir, "is_officer": is_off, "title": title})

    owner = owners[0] if owners else {"name": "Unknown", "is_director": False, "is_officer": False, "title": ""}

    # Non-derivative transactions (common stock, RSUs that converted, etc.)
    for tbl in root.iter():
        if tbl.tag.split("}")[-1] == "nonDerivativeTransaction":
            code   = _tag(tbl, "transactionAcquiredDisposedCode").upper()
            if code not in ("A", "D"):
                continue
            try:
                shares = float(_tag(tbl, "transactionShares") or 0)
                price  = float(_tag(tbl, "transactionPricePerShare") or 0)
            except ValueError:
                continue
            date = _tag(tbl, "transactionDate") or filed_date
            transactions.append({
                **owner,
                "code":   code,
                "shares": shares,
                "price":  price,
                "value":  round(shares * price, 2),
                "date":   date,
                "filed":  filed_date,
            })

    return transactions


# ── Main fetcher ─────────────────────────────────────────────────────────────

def fetch_insider_activity(symbol: str, days: int = 30) -> dict:
    """
    Fetch Form 4 insider activity for `symbol` over the last `days` calendar days.

    Returns:
    {
      "sellers": [{"name", "role", "shares", "value", "date"}],
      "buyers":  [...],
      "total_sold_value":   float,   # USD value disposed
      "total_bought_value": float,
      "unique_sellers":     int,
      "cluster_sell":       bool,    # ≥2 distinct insiders sold
      "summary":            str,     # human-readable block for AI prompt
    }
    Returns {} on any error (fails open).
    """
    # ── Cache check ───────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    if symbol in _result_cache:
        cached_at, cached_result = _result_cache[symbol]
        if (now - cached_at) < _RESULT_CACHE_TTL:
            return cached_result

    try:
        result = _fetch_insider_activity_uncached(symbol, days, now)
    except Exception as exc:
        logger.warning("insider_checker: unexpected error for %s: %s — failing open", symbol, exc)
        result = {}

    _result_cache[symbol] = (now, result)
    return result


def _fetch_insider_activity_uncached(symbol: str, days: int, now: datetime) -> dict:
    # 1. CIK lookup
    cik = _get_cik(symbol)
    if not cik:
        logger.debug("insider_checker: no CIK found for %s", symbol)
        return {}

    # 2. Submissions JSON — lists all recent filings in reverse chronological order
    sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        resp = httpx.get(sub_url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        sub  = resp.json()
    except Exception as exc:
        logger.debug("insider_checker: submissions fetch failed for %s: %s", symbol, exc)
        return {}

    recent   = sub.get("filings", {}).get("recent", {})
    forms    = recent.get("form", [])
    dates    = recent.get("filingDate", [])
    accnums  = recent.get("accessionNumber", [])
    docs     = recent.get("primaryDocument", [])

    cutoff = now - timedelta(days=days)

    # 3. Find Form 4 filings within the window
    filing_tuples = []
    for form, date_str, accn, doc in zip(forms, dates, accnums, docs):
        if form != "4":
            continue
        try:
            filed_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if filed_dt < cutoff:
            break   # list is in reverse-chron order; older filings follow
        filing_tuples.append((date_str, accn, doc))
        if len(filing_tuples) >= _MAX_FILINGS:
            break

    if not filing_tuples:
        result = _empty_result("No Form 4 filings in the last 30 days.")
        return result

    # 4. Fetch and parse each Form 4 XML
    all_txns: list[dict] = []
    for filed_date, accn, primary_doc in filing_tuples:
        accn_path = accn.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_path}/{primary_doc}"
        try:
            r = httpx.get(
                url, headers={**_HEADERS, "Accept": "text/xml,application/xml,*/*"},
                timeout=_TIMEOUT, follow_redirects=True,
            )
            r.raise_for_status()
            txns = _parse_form4(r.text, filed_date)
            all_txns.extend(txns)
        except Exception as exc:
            logger.debug("insider_checker: filing fetch failed %s: %s", url, exc)

    if not all_txns:
        return _empty_result("Form 4 filings found but no parseable transactions.")

    # 5. Aggregate
    sellers: list[dict] = []
    buyers:  list[dict] = []
    seller_names: set[str] = set()
    buyer_names:  set[str] = set()

    for t in all_txns:
        role = t.get("title") or ("Director" if t["is_director"] else "Officer" if t["is_officer"] else "Insider")
        entry = {
            "name":   t["name"],
            "role":   role,
            "shares": int(t["shares"]),
            "value":  t["value"],
            "date":   t["date"],
        }
        if t["code"] == "D":
            sellers.append(entry)
            seller_names.add(t["name"])
        else:
            buyers.append(entry)
            buyer_names.add(t["name"])

    total_sold   = sum(s["value"] for s in sellers)
    total_bought = sum(b["value"] for b in buyers)
    unique_sellers = len(seller_names)
    cluster_sell   = unique_sellers >= 2

    summary = _build_summary(sellers, buyers, total_sold, total_bought, unique_sellers, cluster_sell, days)

    return {
        "sellers":           sellers,
        "buyers":            buyers,
        "total_sold_value":   round(total_sold, 2),
        "total_bought_value": round(total_bought, 2),
        "unique_sellers":     unique_sellers,
        "cluster_sell":       cluster_sell,
        "summary":            summary,
    }


def _empty_result(note: str) -> dict:
    return {
        "sellers":            [],
        "buyers":             [],
        "total_sold_value":   0.0,
        "total_bought_value": 0.0,
        "unique_sellers":     0,
        "cluster_sell":       False,
        "summary":            note,
    }


def _build_summary(sellers, buyers, total_sold, total_bought, unique_sellers, cluster_sell, days) -> str:
    lines = []

    if sellers:
        lines.append(
            f"SELLING: {unique_sellers} insider(s) disposed "
            f"~${total_sold:,.0f} in the last {days} days."
        )
        for s in sellers[:3]:   # cap at 3 for prompt brevity
            lines.append(f"  • {s['name']} ({s['role']}): {s['shares']:,} shares @ ~${s['value']:,.0f} on {s['date']}")
        if len(sellers) > 3:
            lines.append(f"  • ... and {len(sellers) - 3} more disposal transaction(s)")
        if cluster_sell:
            lines.append("  ⚠ CLUSTER SELL: multiple distinct insiders selling in the same window.")

    if buyers:
        lines.append(
            f"BUYING: {len(set(b['name'] for b in buyers))} insider(s) acquired "
            f"~${total_bought:,.0f} in the last {days} days."
        )
        for b in buyers[:2]:
            lines.append(f"  • {b['name']} ({b['role']}): {b['shares']:,} shares on {b['date']}")

    if not sellers and not buyers:
        lines.append("No significant insider transactions in the last 30 days.")

    return "\n".join(lines)


def format_for_prompt(symbol: str, activity: dict) -> str:
    """Format insider activity for injection into the AI gate prompt."""
    if not activity:
        return ""
    summary = activity.get("summary", "")
    if not summary:
        return ""
    return f"\n--- INSIDER ACTIVITY — {symbol} (last 30 days, SEC Form 4) ---\n{summary}\n"
