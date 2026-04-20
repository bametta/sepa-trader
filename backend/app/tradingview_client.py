"""
Unofficial TradingView watchlist API client.

Authenticates with username/password, then creates or replaces
the 'weekly_picks' watchlist with the screener's top 10 symbols.

Note: uses TradingView's internal REST API which may change without notice.
2FA is not supported — use an account without two-factor authentication.
"""
import logging
import httpx

logger = logging.getLogger(__name__)

TV_BASE        = "https://www.tradingview.com"
SIGNIN_URL     = f"{TV_BASE}/accounts/signin/"
WATCHLIST_API  = f"{TV_BASE}/api/v1/symbols_list/watchlists/"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Stocks in our universe that trade on NYSE (everything else defaults to NASDAQ)
_NYSE = {
    "JPM", "BAC", "GS", "MS", "V", "MA", "WFC", "BX", "AXP", "SPGI",
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "TMO", "ISRG", "REGN", "VRTX",
    "ORCL", "NOW", "WMT", "HD", "NKE", "MCD", "TJX", "DECK", "ONON",
    "XOM", "CVX", "COP", "SLB", "CAT", "DE", "HON", "LMT", "RTX", "GE",
    "UNP", "CSX", "DIS", "ENPH", "FSLR", "BRK.B",
}


def to_tv_symbol(symbol: str) -> str:
    """Return exchange-prefixed symbol for TradingView (e.g. NASDAQ:AAPL)."""
    exchange = "NYSE" if symbol.upper() in _NYSE else "NASDAQ"
    return f"{exchange}:{symbol.upper()}"


def _headers(csrf: str = "") -> dict:
    h = {
        "User-Agent":  _UA,
        "Referer":     TV_BASE + "/",
        "Accept":      "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    if csrf:
        h["X-CSRFToken"] = csrf
    return h


def _signin(username: str, password: str) -> tuple[dict, str]:
    """
    Authenticate and return (cookies_dict, csrf_token).
    Raises RuntimeError on failure.
    """
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        # Visit homepage first to get initial CSRF cookie
        client.get(TV_BASE + "/", headers={"User-Agent": _UA})
        csrf = client.cookies.get("csrftoken", "")

        resp = client.post(
            SIGNIN_URL,
            data={"username": username, "password": password, "remember_me": "on"},
            headers=_headers(csrf),
        )

        if resp.status_code not in (200, 204):
            raise RuntimeError(f"Signin HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("error"):
                raise RuntimeError(f"Signin rejected: {body['error']}")
        except ValueError:
            pass  # non-JSON response is OK if status was 200

        csrf = client.cookies.get("csrftoken", csrf)
        return dict(client.cookies), csrf


def _list_watchlists(cookies: dict, csrf: str) -> list:
    with httpx.Client(cookies=cookies, timeout=30) as client:
        resp = client.get(WATCHLIST_API, headers=_headers(csrf))
        resp.raise_for_status()
        data = resp.json()
        # API may return {"payload": [...]} or a bare list
        return data.get("payload", data) if isinstance(data, dict) else data


def _create_watchlist(cookies: dict, csrf: str, name: str, tv_symbols: list[str]) -> dict:
    payload = {
        "name": name,
        "symbols": {"content": [{"symbol": s} for s in tv_symbols]},
    }
    with httpx.Client(cookies=cookies, timeout=30) as client:
        resp = client.post(WATCHLIST_API, json=payload, headers=_headers(csrf))
        resp.raise_for_status()
        return resp.json()


def _update_watchlist(cookies: dict, csrf: str, wl_id: str, name: str, tv_symbols: list[str]) -> dict:
    payload = {
        "name": name,
        "symbols": {"content": [{"symbol": s} for s in tv_symbols]},
    }
    with httpx.Client(cookies=cookies, timeout=30) as client:
        resp = client.put(
            f"{WATCHLIST_API}{wl_id}/",
            json=payload,
            headers=_headers(csrf),
        )
        resp.raise_for_status()
        return resp.json()


def update_weekly_picks(
    username: str,
    password: str,
    symbols: list[str],
    watchlist_name: str = "weekly_picks",
) -> dict:
    """
    Create or replace the named TradingView watchlist with the given symbols.

    Returns {"ok": True, "action": "created"|"updated", "count": N}
    on success, or {"ok": False, "error": "..."} on failure.
    """
    try:
        cookies, csrf = _signin(username, password)
        tv_syms = [to_tv_symbol(s) for s in symbols]

        watchlists = _list_watchlists(cookies, csrf)
        existing = next((w for w in watchlists if w.get("name") == watchlist_name), None)

        if existing:
            _update_watchlist(cookies, csrf, str(existing["id"]), watchlist_name, tv_syms)
            action = "updated"
        else:
            _create_watchlist(cookies, csrf, watchlist_name, tv_syms)
            action = "created"

        logger.info(
            "TradingView watchlist '%s' %s with %d symbols: %s",
            watchlist_name, action, len(tv_syms), tv_syms,
        )
        return {"ok": True, "action": action, "count": len(tv_syms)}

    except Exception as exc:
        logger.error("TradingView watchlist sync failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
