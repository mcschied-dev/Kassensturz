"""Canonical transaction model.

Every connector (FinTS, CSV import, Trade Republic, ...) normalizes into
this one shape. The store, categorizer, and reports only ever see this.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Transaction:
    booking_date: date
    amount: Decimal
    currency: str
    account: str  # label of *our own* account, e.g. "dkb-giro"
    counterparty: str
    purpose: str
    value_date: date | None = None
    booking_type: str = ""  # Buchungstext / Umsatztyp
    category: str | None = None
    source: str = ""  # connector that produced this row, e.g. "csv:dkb"
    seq: int = 0  # disambiguates otherwise-identical transactions in a batch

    def tx_hash(self) -> str:
        """Stable dedup key. Re-importing an overlapping export is a no-op."""
        raw = "|".join(
            [
                self.booking_date.isoformat(),
                str(self.amount),
                self.currency,
                self.account,
                self.counterparty.strip().lower(),
                self.purpose.strip().lower(),
                str(self.seq),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def amount_cents(self) -> int:
        return int((self.amount * 100).to_integral_value())


def assign_seq(txs: list[Transaction]) -> list[Transaction]:
    """Number identical transactions within one batch (0, 1, 2, ...).

    Two genuinely identical bookings on the same day (e.g. two coffees)
    get distinct hashes; re-importing the same file still deduplicates,
    because the numbering is deterministic in file order.
    """
    seen: dict[tuple, int] = {}
    out: list[Transaction] = []
    for t in txs:
        key = (
            t.booking_date,
            str(t.amount),
            t.currency,
            t.account,
            t.counterparty.strip().lower(),
            t.purpose.strip().lower(),
        )
        n = seen.get(key, 0)
        seen[key] = n + 1
        out.append(replace(t, seq=n))
    return out


def fmt_eur(cents: int, currency: str = "EUR") -> str:
    """German number formatting: 1.234,56 €"""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    whole, frac = divmod(cents, 100)
    whole_str = f"{whole:,}".replace(",", ".")
    symbol = "€" if currency == "EUR" else currency
    return f"{sign}{whole_str},{frac:02d} {symbol}"
