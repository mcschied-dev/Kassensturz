"""DKB connector via dkb-robo (the bank's web/REST API).

Why not FinTS for DKB? This route needs no DK-registered product ID —
dkb-robo talks to the same REST endpoints the DKB web banking uses.
Login requires confirming once in the DKB app (2FA); the library polls
until the user approves. Password comes from the system keychain,
shared with the FinTS connector (`kassensturz pin <name>`).

Covered: checking accounts ("account") and credit cards ("creditcard").
Depots are skipped — portfolio positions aren't transactions.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from ..config import DkbConnection
from ..model import Transaction
from .csv_import import parse_date

logger = logging.getLogger(__name__)

SYNCABLE_TYPES = ("account", "creditcard")


def dkb_tx_to_transaction(
    data: dict, account_type: str, account_label: str, source: str
) -> Transaction:
    """Map one dkb-robo transaction dict to the canonical model."""
    if account_type == "creditcard":
        counterparty = ""
        purpose = data.get("text") or ""
        booking_type = ""
    else:
        counterparty = data.get("peer") or ""
        purpose = data.get("reasonforpayment") or ""
        booking_type = data.get("postingtext") or ""

    raw_vdate = data.get("vdate")
    return Transaction(
        booking_date=parse_date(str(data.get("bdate") or data.get("date"))),
        amount=Decimal(str(data["amount"])),
        currency=data.get("currencycode") or "EUR",
        account=account_label,
        counterparty=counterparty,
        purpose=purpose,
        value_date=parse_date(str(raw_vdate)) if raw_vdate else None,
        booking_type=booking_type,
        source=source,
    )


def extract_balances(
    account_dic: dict, conn: DkbConnection
) -> list[tuple[str, int, str]]:
    """(label, balance_cents, currency) for EVERY account dkb-robo reports.

    Balances are recorded even for accounts excluded from transaction sync
    (depot, unmapped) — assets must never be invisible. Unmapped accounts
    get the auto label.
    """
    out: list[tuple[str, int, str]] = []
    for _idx, acc in sorted(account_dic.items()):
        amount = acc.get("amount")
        if amount in (None, ""):
            continue
        number = acc.get("iban") or acc.get("account") or acc.get("id") or ""
        label = conn.label_for(number) or f"{conn.name}-{number[-4:]}"
        cents = int((Decimal(str(amount)) * 100).to_integral_value())
        out.append((label, cents, acc.get("currencycode") or "EUR"))
    return out


def sync_dkb(
    conn: DkbConnection, password: str, start: date, end: date
) -> tuple[list[Transaction], list[str], list[tuple[str, int, str]]]:
    """Fetch transactions + balance snapshots for one DKB connection.

    Returns (transactions, skipped, balances) — skipped = account numbers
    present at the bank but not selected (unmapped or unsupported type).
    Blocks during login until the user confirms in the DKB app.
    """
    from dkb_robo import DKBRobo  # heavy import, keep it local

    txs: list[Transaction] = []
    skipped: list[str] = []
    with DKBRobo(dkb_user=conn.username, dkb_password=password) as dkb:
        balances = extract_balances(dkb.account_dic, conn)
        for _idx, acc in sorted(dkb.account_dic.items()):
            number = acc.get("iban") or acc.get("account") or acc.get("id") or ""
            acc_type = acc.get("type", "")
            if acc_type not in SYNCABLE_TYPES:
                logger.info("skipping %s (%s)", number, acc_type)
                skipped.append(f"{number} ({acc_type})")
                continue
            label = conn.label_for(number)
            if label is None:
                skipped.append(number)
                continue
            raw = dkb.get_transactions(
                acc["transactions"],
                acc_type,
                start.strftime("%d.%m.%Y"),
                end.strftime("%d.%m.%Y"),
            )
            for t in raw:
                txs.append(
                    dkb_tx_to_transaction(t, acc_type, label, f"dkb:{conn.name}")
                )
    return txs, skipped, balances
