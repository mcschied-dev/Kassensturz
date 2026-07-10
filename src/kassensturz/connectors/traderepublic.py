"""Trade Republic connector via pytr (unofficial API).

Scope: a balance snapshot (cash + portfolio market value). TR has no
"account transactions" in the bank sense — the money flow is the
savings-plan debit on the funding account (already categorized there,
e.g. "Sparplan Romy" on the DKB side). What TR adds to kassensturz is
the depot's market value in the net-worth overview.

Login is interactive on first run (2FA code via the TR app or SMS);
pytr then caches the web session under ~/.pytr, so later syncs resume
without 2FA. PIN comes from the system keychain (`kassensturz pin tr`).

Valuation reuses pytr's own Portfolio class (subscribes to instrument
tickers), so we don't reimplement the fragile websocket dance.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from ..config import TradeRepublicConnection
from ..model import Transaction

logger = logging.getLogger(__name__)


def summarize_portfolio(cash: list[dict], positions: list[dict]) -> dict:
    """Aggregate pytr Portfolio data → totals per currency.

    cash: pytr `cash` response, e.g. [{'currencyId':'EUR','amount':12.3}]
    positions: pytr portfolio positions, each with a computed 'netValue'.
    Returns {currency: total_cents} combining cash + invested value.
    """
    totals: dict[str, Decimal] = {}
    for c in cash or []:
        cur = c.get("currencyId") or "EUR"
        totals[cur] = totals.get(cur, Decimal(0)) + Decimal(str(c.get("amount", 0)))
    for pos in positions or []:
        cur = pos.get("currency") or "EUR"
        net = pos.get("netValue")
        if net is None:
            continue
        totals[cur] = totals.get(cur, Decimal(0)) + Decimal(str(net))
    return {
        cur: int((val * 100).to_integral_value()) for cur, val in totals.items()
    }


# TR retired the old portfolio topics (portfolio, compactPortfolio). The
# current one groups positions under categories. Positions carry netSize +
# averageBuyIn but no market value, so we value each via a ticker lookup.
_PORTFOLIO_TOPICS = ("compactPortfolioByType", "compactPortfolio", "portfolio")
_DEFAULT_EXCHANGE = "LSX"  # Lang & Schwarz — priced for ~all TR instruments


def _extract_positions(response: dict) -> list[dict]:
    """Positions list out of whatever the portfolio topic returned."""
    if not isinstance(response, dict):
        return []
    if isinstance(response.get("categories"), list):
        out: list[dict] = []
        for cat in response["categories"]:
            out.extend(cat.get("positions", []) or [])
        return out
    for key in ("positions", "holdings"):
        if isinstance(response.get(key), list):
            return response[key]
    return []


def _ticker_price(response: dict) -> Decimal | None:
    if isinstance(response, dict) and isinstance(response.get("last"), dict):
        try:
            return Decimal(str(response["last"]["price"]))
        except Exception:  # noqa: BLE001
            return None
    return None


def _login(phone: str, pin: str):
    """TradeRepublicApi with cookie caching — 2FA/WAF only on first run."""
    from pytr.api import TradeRepublicApi

    tr = TradeRepublicApi(phone_no=phone, pin=pin, save_cookies=True)
    if tr.resume_websession():
        return tr
    countdown = tr.initiate_weblogin()  # WAF via Playwright, then 2FA
    print(
        f"→ Trade Republic: 2FA-Code aus der App eingeben "
        f"(leer = SMS, {int(countdown)}s):"
    )
    code = input("Code: ").strip()
    if not code:
        tr.resend_weblogin()
        code = input("SMS-Code: ").strip()
    tr.complete_weblogin(code)
    return tr


async def _one_topic(tr, payload):
    """Subscribe to one topic, return its response, or None on rejection.

    pytr's recv() *raises* (sub_id, subscription, response) when TR answers
    with an error (e.g. a retired topic), so we catch that and move on.
    """
    import asyncio

    sub_id = await tr.subscribe(payload)
    try:
        _sid, _sub, response = await asyncio.wait_for(tr.recv(), 15)
        return response
    except Exception as e:  # noqa: BLE001 — error subscriptions raise here
        logger.info("TR topic %s abgelehnt: %s", payload, e)
        return None
    finally:
        try:
            await tr.unsubscribe(sub_id)
        except Exception:  # noqa: BLE001
            pass


async def _value_positions(tr, positions: list[dict]) -> None:
    """Fill each position's 'netValue' via a ticker lookup (netSize × price).

    Falls back to cost basis (netSize × averageBuyIn) when no live price is
    available, so a flaky ticker never drops a holding from the total.
    """
    for pos in positions:
        net_size = Decimal(str(pos.get("netSize") or 0))
        isin = pos.get("isin")
        price = None
        if isin:
            tick = await _one_topic(
                tr, {"type": "ticker", "id": f"{isin}.{_DEFAULT_EXCHANGE}"}
            )
            price = _ticker_price(tick)
        if price is None:
            price = Decimal(str(pos.get("averageBuyIn") or 0))
        pos["netValue"] = float(net_size * price)
        pos["currency"] = "EUR"


async def _collect(tr):
    """Fetch cash + valued portfolio positions from the current API."""
    cash_resp = await _one_topic(tr, {"type": "cash"})
    cash = (
        cash_resp
        if isinstance(cash_resp, list)
        else (cash_resp or {}).get("cash", [])
    )

    positions: list = []
    for topic in _PORTFOLIO_TOPICS:
        response = await _one_topic(tr, {"type": topic})
        positions = _extract_positions(response)
        if positions:
            break
    await _value_positions(tr, positions)
    return cash, positions


def _fetch_portfolio(phone: str, pin: str):
    import asyncio

    tr = _login(phone, pin)
    return asyncio.run(_collect(tr))


def sync_traderepublic(
    conn: TradeRepublicConnection, pin: str, start: date, end: date
) -> tuple[list[Transaction], list[str], list[tuple[str, int, str]]]:
    """Fetch a TR portfolio balance snapshot.

    Returns (transactions=[], notes, balances) like the other connectors.
    start/end are unused (snapshot only); kept for a uniform signature.
    """
    cash, positions = _fetch_portfolio(conn.phone, pin)
    totals = summarize_portfolio(cash, positions)
    balances = [(conn.label, cents, cur) for cur, cents in totals.items()]
    note = (
        f"{conn.label}: {len(positions)} Positionen zu Marktkursen bewertet "
        "(Depotwert + Cash; Umsätze laufen über das Verrechnungskonto)"
    )
    return [], [note], balances
