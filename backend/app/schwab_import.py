"""
Schwab positions CSV importer (the "Export" on a Schwab positions page, including PCRAs).

Tolerant by design: finds the header row wherever it sits (Schwab prepends a title line), maps columns by
name, ignores columns it doesn't know, and handles "$1,234.56", "(12.5)", "--" and "N/A". It reads only; it
never contacts Schwab. If a real export doesn't parse, the error says what it was looking for.
"""
import csv
import io
import re
from datetime import date

SYNONYMS = {
    "symbol": ("symbol",),
    "name": ("description", "name", "security name"),
    "quantity": ("quantity", "qty", "shares"),
    "value": ("market value", "mkt val (market value)", "current value", "value"),
    "type": ("security type", "asset type", "type"),
}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-/]{0,11}$")
TOTAL_LABELS = ("account total", "totals", "total")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def money(raw) -> float | None:
    s = (raw or "").strip().replace("$", "").replace(",", "")
    if s in ("", "--", "-", "n/a", "N/A"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if negative else v


def _find_header(rows: list[list[str]]) -> tuple[int, dict]:
    for i, row in enumerate(rows):
        cells = [_norm(c) for c in row]
        if "symbol" not in cells:
            continue
        mapping = {}
        for key, names in SYNONYMS.items():
            for j, c in enumerate(cells):
                if c in names and key not in mapping:
                    mapping[key] = j
        if "symbol" in mapping and ("quantity" in mapping or "value" in mapping):
            return i, mapping
    raise ValueError("could not find a header row with a 'Symbol' column plus 'Quantity' or 'Market Value'. "
                     "Is this Schwab's positions export? Send the first few lines (without account numbers) "
                     "and I'll adapt the importer.")


def parse_schwab_positions(raw: str, today: date | None = None) -> dict:
    """Returns {holdings, file_total, parsed_total, warnings, title}. Raises ValueError if unreadable."""
    today = today or date.today()
    text = (raw or "").strip().lstrip("﻿")
    if not text:
        raise ValueError("the file is empty")
    rows = [r for r in csv.reader(io.StringIO(text))]
    header_idx, col = _find_header(rows)
    title = next((" ".join(r).strip() for r in rows[:header_idx] if any(c.strip() for c in r)), None)

    def cell(row, key):
        j = col.get(key)
        return row[j].strip() if j is not None and j < len(row) else ""

    holdings, warnings, file_total = [], [], None
    for row in rows[header_idx + 1:]:
        if not any(c.strip() for c in row):
            continue
        sym_raw = cell(row, "symbol")
        low = _norm(sym_raw)
        value = money(cell(row, "value"))
        if low in TOTAL_LABELS:
            file_total = value if value is not None else file_total
            continue
        name = cell(row, "name") or sym_raw
        sec_type = _norm(cell(row, "type"))
        if low.startswith("cash"):
            if value:
                holdings.append({"symbol": None, "name": sym_raw or "Cash", "quantity": None,
                                 "manual_value": value, "asset_class": "cash", "illiquid": False, "as_of": today})
            continue
        symbol = sym_raw.upper()
        if not SYMBOL_RE.match(symbol):
            if sym_raw:
                warnings.append(f"skipped a row with an unreadable symbol: '{sym_raw[:20]}'")
            continue
        qty = money(cell(row, "quantity"))
        if qty is not None and qty <= 0:
            warnings.append(f"skipped {symbol}: quantity {qty} is not a long position")
            continue
        if qty is None and value is None:
            warnings.append(f"skipped {symbol}: no quantity and no market value")
            continue
        is_cash_like = "money market" in sec_type or "money market" in _norm(name) or sec_type == "cash"
        holdings.append({"symbol": symbol, "name": name[:120], "quantity": qty, "manual_value": value,
                         "asset_class": "cash" if is_cash_like else "auto", "illiquid": False, "as_of": today})

    if not holdings:
        raise ValueError("found the header but no holdings below it")
    parsed_total = round(sum(h["manual_value"] or 0.0 for h in holdings), 2)
    if file_total and parsed_total and abs(parsed_total - file_total) / file_total > 0.01:
        warnings.append(f"holdings add up to ${parsed_total:,.2f} but the file's account total is "
                        f"${file_total:,.2f}; some rows may have been skipped")
    return {"holdings": holdings, "file_total": file_total, "parsed_total": parsed_total,
            "warnings": warnings, "title": title}


def looks_like_schwab(raw: str) -> bool:
    low = (raw or "").lower()
    return "market value" in low or "positions for account" in low
