"""
Broker positions CSV importer (Schwab, Fidelity and similar "Download/Export positions" files).

Tolerant by design: finds the header row wherever it sits, maps columns by name, ignores columns it doesn't
know, ignores footer/disclaimer lines, and handles "$1,234.56", "+$12.34", "(12.5)", "--" and "N/A".
Files that contain several accounts (Fidelity's "Account Number" column) are grouped, and you pick one.
It only reads a file you give it and never contacts a broker. If a real export doesn't parse, the error says
what it was looking for.
"""
import csv
import io
import re
from datetime import date

# Deliberately no bare "type": in Fidelity's file that column means Cash/Margin account, not security type.
SYNONYMS = {
    "symbol": ("symbol", "ticker symbol", "ticker"),
    "name": ("description", "name", "security name"),
    "quantity": ("quantity", "qty", "shares", "units"),
    "value": ("market value", "mkt val (market value)", "current value", "asset value", "value"),
    "type": ("security type", "asset type"),
    "acct_number": ("account number", "account #", "acct number"),
    "acct_name": ("account name", "account nickname"),
}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-/]{0,11}$")
TOTAL_LABELS = ("account total", "totals", "total")
CASH_WORDS = ("money market", "cash reserves", "held in")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def money(raw) -> float | None:
    s = (raw or "").strip().replace("$", "").replace(",", "").replace("+", "")
    if s in ("", "--", "-", "n/a", "N/A"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if negative else v


def _fuzzy(key: str, cell: str) -> bool:
    """Headers like Schwab's 'Qty (Quantity)' and 'Mkt Val (Market Value)' carry the plain name in brackets."""
    if key == "quantity":
        return "quantity" in cell or cell.startswith("qty")
    if key == "value":
        return "market value" in cell or "mkt val" in cell
    return False


def _read_rows(text: str) -> list[list[str]]:
    """CSV rows. Pasted spreadsheet cells are tab-separated, real exports are comma-separated."""
    delim = "\t" if text.count("\t") > text.count(",") else ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim)]


DATE_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")


def _file_date(lines: list[str], today: date) -> date:
    """The 'as of' date printed above the header (mm-dd-yyyy or mm/dd/yyyy); today if absent or not plausible."""
    for ln in lines:
        m = DATE_RE.search(ln)
        if m:
            try:
                d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                continue
            if d <= today:
                return d
    return today


def _untickered_class(asset_type: str, description: str) -> tuple[str, bool]:
    """(asset_class, illiquid) for an asset with no ticker, from the custodian's Asset Type text (Equity Trust:
    Digital Currency, Brokerage Accounts, Limited Liability Company, real estate, notes...). Unknown types stay
    unclassified so the app asks you to classify them instead of guessing."""
    t = _norm(f"{asset_type} {description}")
    if re.search(r"\b(cash|money market)\b", t):
        return "cash", False
    if re.search(r"digital currenc|crypto|bitcoin|ethereum", t):
        return "crypto", False
    if re.search(r"real estate|property|rental|\bland\b|mortgage|trust deed|\blien\b", t):
        return "alt_real_estate", True
    if re.search(r"limited liability|\bllc\b|\bl\.l\.c|partnership|\blp\b|private|placement|syndication|"
                 r"promissory|\bnote\b|business", t):
        return "alt_private", True
    if re.search(r"brokerage account", t):
        return "auto", False                  # money held at another broker: value is real, its mix is unknown
    return "alt_other", True


def _find_header(rows: list[list[str]]) -> tuple[int, dict]:
    for i, row in enumerate(rows):
        cells = [_norm(c) for c in row]
        if not any(c in SYNONYMS["symbol"] for c in cells):
            continue
        mapping = {}
        for key, names in SYNONYMS.items():
            for j, c in enumerate(cells):
                if key not in mapping and (c in names or _fuzzy(key, c)):
                    mapping[key] = j
        if "symbol" in mapping and ("quantity" in mapping or "value" in mapping):
            return i, mapping
    raise ValueError("could not find a header row with a 'Symbol' column plus 'Quantity' or 'Market Value' / "
                     "'Current Value'. Is this a positions export? Send the first few lines (without account "
                     "numbers) and I'll adapt the importer.")


def looks_like_broker_export(raw: str) -> bool:
    """A positions file from a broker, as opposed to this app's own CSV (which has an asset_class column)."""
    first = next((ln for ln in (raw or "").splitlines() if ln.strip()), "").lower()
    if "asset_class" in first:
        return False
    try:
        _find_header(_read_rows((raw or "").strip().lstrip("﻿")))
        return True
    except ValueError:
        return False


def _label(number: str, name: str) -> str:
    tail = f"...{re.sub(r'[^0-9A-Za-z]', '', number)[-4:]}" if number else ""
    return " ".join(p for p in (name, tail) if p) or "Account"


def parse_positions(raw: str, today: date | None = None, account_key: str | None = None) -> dict:
    """Returns {holdings, file_total, parsed_total, warnings, title, groups, needs_choice}.
    If the file holds several accounts and `account_key` is not given, needs_choice is True and holdings is []."""
    today = today or date.today()
    text = (raw or "").strip().lstrip("﻿")
    if not text:
        raise ValueError("the file is empty")
    rows = _read_rows(text)
    header_idx, col = _find_header(rows)
    title = next((" ".join(r).strip() for r in rows[:header_idx] if sum(1 for c in r if c.strip()) == 1), None)
    # Ticker-less assets (real estate, LLCs, digital currency...) are only accepted from exports whose symbol column
    # is called "Ticker Symbol" (Equity Trust style); Schwab/Fidelity rows with an empty symbol stay skipped.
    untickered_ok = _norm(rows[header_idx][col["symbol"]]) in ("ticker symbol", "ticker")
    # Those hand-valued rows are dated by the 'as of' date printed on the file; other brokers keep using today.
    as_of = _file_date([" ".join(r) for r in rows[:header_idx]], today) if untickered_ok else today

    def cell(row, key):
        j = col.get(key)
        return row[j].strip() if j is not None and j < len(row) else ""

    groups: dict[str, dict] = {}
    for row in rows[header_idx + 1:]:
        if sum(1 for c in row if c.strip()) < 2:          # blank lines and one-cell footer/disclaimer lines
            continue
        number, acct_name = cell(row, "acct_number"), cell(row, "acct_name")
        key = number or acct_name or ""
        g = groups.setdefault(key, {"key": key, "label": _label(number, acct_name), "holdings": [], "warnings": [],
                                    "file_total": None})
        sym_raw = cell(row, "symbol")
        low = _norm(sym_raw)
        value = money(cell(row, "value"))
        if low in TOTAL_LABELS:
            g["file_total"] = value if value is not None else g["file_total"]
            continue
        if untickered_ok and not sym_raw:
            desc, raw_type = cell(row, "name"), cell(row, "type")
            label = _norm(desc or raw_type)
            if label in TOTAL_LABELS or label.startswith("total") or _norm(raw_type) in TOTAL_LABELS:
                g["file_total"] = value if value is not None else g["file_total"]
                continue
            if value is None or value <= 0:
                if desc or raw_type:
                    g["warnings"].append(f"skipped '{(desc or raw_type)[:40]}': no positive value")
                continue
            cls, illiquid = _untickered_class(raw_type, desc)
            g["holdings"].append({"symbol": None, "name": (desc or raw_type)[:120], "quantity": None,
                                  "manual_value": value, "asset_class": cls, "illiquid": illiquid, "as_of": as_of})
            continue
        name = cell(row, "name") or sym_raw
        sec_type = _norm(cell(row, "type"))
        if low == "pending activity":
            g["warnings"].append(f"skipped 'Pending Activity' ({value if value is not None else 'no value'}): "
                                 "pending trades aren't holdings yet")
            continue
        core_cash = sym_raw.endswith("*")                  # e.g. Fidelity's SPAXX**
        symbol = sym_raw.rstrip("*").upper()
        if low.startswith("cash") and " " in low:          # Schwab's "Cash & Cash Investments" row
            if value:
                g["holdings"].append({"symbol": None, "name": sym_raw or "Cash", "quantity": None,
                                      "manual_value": value, "asset_class": "cash", "illiquid": False,
                                      "as_of": as_of})
            continue
        if not SYMBOL_RE.match(symbol):
            if sym_raw:
                g["warnings"].append(f"skipped a row with an unreadable symbol: '{sym_raw[:20]}'")
            continue
        qty = money(cell(row, "quantity"))
        if qty is not None and qty <= 0:
            g["warnings"].append(f"skipped {symbol}: quantity {qty} is not a long position")
            continue
        if qty is None and value is None:
            g["warnings"].append(f"skipped {symbol}: no quantity and no market value")
            continue
        low_name = _norm(name)
        is_cash = (core_cash or "money market" in sec_type or sec_type == "cash"
                   or any(w in low_name for w in CASH_WORDS))
        g["holdings"].append({"symbol": symbol, "name": name[:120], "quantity": qty, "manual_value": value,
                              "asset_class": "cash" if is_cash else "auto", "illiquid": False, "as_of": as_of})

    groups = {k: g for k, g in groups.items() if g["holdings"] or g["file_total"]}
    if not groups:
        raise ValueError("found the header but no holdings below it")
    summary = [{"key": k, "label": g["label"], "holdings": len(g["holdings"]),
                "total": round(sum(h["manual_value"] or 0.0 for h in g["holdings"]), 2)} for k, g in groups.items()]
    if len(groups) > 1 and account_key is None:
        return {"holdings": [], "file_total": None, "parsed_total": 0.0, "warnings": [], "title": title,
                "groups": summary, "needs_choice": True}
    if account_key is None:
        account_key = next(iter(groups))
    if account_key not in groups:
        raise ValueError("that account is not in the file")
    g = groups[account_key]
    holdings, warnings = g["holdings"], list(g["warnings"])
    if not holdings:
        raise ValueError("no holdings found for that account")
    parsed_total = round(sum(h["manual_value"] or 0.0 for h in holdings), 2)
    file_total = g["file_total"]
    if file_total and parsed_total and abs(parsed_total - file_total) / file_total > 0.01:
        warnings.append(f"holdings add up to ${parsed_total:,.2f} but the file's account total is "
                        f"${file_total:,.2f}; some rows may have been skipped")
    return {"holdings": holdings, "file_total": file_total, "parsed_total": parsed_total, "warnings": warnings,
            "title": title or g["label"], "groups": summary, "needs_choice": False}
