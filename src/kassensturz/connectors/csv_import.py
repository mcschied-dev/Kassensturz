"""CSV import with per-bank profiles.

German bank exports are messy: semicolon-separated, decimal comma,
dd.mm.yy dates, cp1252 or utf-8-sig encodings, and metadata lines above
the actual header. Each profile describes one bank's export format; the
header row is located by searching for the profile's required columns.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..model import Transaction

DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d")


def parse_german_decimal(raw: str) -> Decimal:
    """'-1.234,56 €' → Decimal('-1234.56'). Also accepts '1234.56'."""
    s = raw.strip().replace("−", "-")  # unicode minus
    s = re.sub(r"[^\d,.\-+]", "", s)  # strip currency symbols/spaces
    if not s:
        raise InvalidOperation(f"empty amount: {raw!r}")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return Decimal(s)


def parse_date(raw: str) -> date:
    s = raw.strip().strip('"')
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {raw!r}")


@dataclass
class CsvProfile:
    name: str
    date_col: str
    amount_col: str
    purpose_col: str
    counterparty_col: str | None = None
    # (col if amount < 0, col if amount >= 0) — DKB splits payer/payee
    counterparty_by_sign: tuple[str, str] | None = None
    value_date_col: str | None = None
    booking_type_col: str | None = None
    currency_col: str | None = None
    # bank/app's own category column — used by `adopt-categories` as a
    # fallback for rows the YAML rules leave uncategorized
    category_col: str | None = None
    # rows whose booking_type is in this set are dropped (PayPal: internal
    # funding legs, authorizations, FX conversions → avoid double counting)
    exclude_types: tuple[str, ...] = ()
    # rows whose counterparty contains any of these substrings are dropped
    # (PayPal: "PayPal (Europe)" = interne Einzugs-/Settlement-Buchungen)
    exclude_counterparty_contains: tuple[str, ...] = ()
    currency: str = "EUR"
    delimiter: str = ";"
    encodings: tuple[str, ...] = ("utf-8-sig", "cp1252")

    @property
    def required_cols(self) -> list[str]:
        return [self.date_col, self.amount_col, self.purpose_col]


PROFILES: dict[str, CsvProfile] = {
    # DKB "neues Banking" export (since 2023)
    "dkb": CsvProfile(
        name="dkb",
        date_col="Buchungsdatum",
        value_date_col="Wertstellung",
        amount_col="Betrag (€)",
        purpose_col="Verwendungszweck",
        counterparty_by_sign=("Zahlungsempfänger*in", "Zahlungspflichtige*r"),
        booking_type_col="Umsatztyp",
    ),
    # Sparkasse CAMT-CSV export
    "sparkasse": CsvProfile(
        name="sparkasse",
        date_col="Buchungstag",
        value_date_col="Valutadatum",
        amount_col="Betrag",
        purpose_col="Verwendungszweck",
        counterparty_col="Beguenstigter/Zahlungspflichtiger",
        booking_type_col="Buchungstext",
        currency_col="Waehrung",
        category_col="Kategorie",  # only present in the categorized export
    ),
    # PayPal Aktivitäten-Export (deutsch). ENTWURF — an echtem Export
    # verifizieren: Spaltennamen/Trennzeichen variieren je PayPal-Version.
    # "Brutto" = Transaktionsbetrag; nur Zahlungen importieren, Bank-
    # Aufbuchungen ("Allgemeine Aufbuchung") sind Gegenbuchung zur
    # SEPA-Abbuchung → sonst Doppelzählung.
    "paypal": CsvProfile(
        name="paypal",
        date_col="Datum",
        amount_col="Brutto",
        purpose_col="Artikelbezeichnung",
        counterparty_col="Name",
        currency_col="Währung",
        booking_type_col="Typ",
        delimiter=",",
        encodings=("utf-8-sig", "cp1252"),
        # nur echte Zahlungen/Rückzahlungen behalten; interne Legs raus
        exclude_types=(
            "Allgemeine Autorisierung",
            "Stornierung der Autorisierung",
            "Einbehaltung für offene Autorisierung",
            "Rückbuchung allgemeiner Einbehaltung",
            "Allgemeine Gutschrift auf Kreditkarte",
            "Allgemeine Abbuchung von Kreditkarte",
            "Bankgutschrift auf PayPal-Konto",
            "Allgemeine Währungsumrechnung",
        ),
        exclude_counterparty_contains=("PayPal (Europe)",),
    ),
    # Outbank app export (multi-year history, all German banks)
    "outbank": CsvProfile(
        name="outbank",
        date_col="Datum",
        value_date_col="Valuta",
        amount_col="Betrag",
        purpose_col="Zweck",
        counterparty_col="Name",
        booking_type_col="Buchungstext",
        currency_col="Währung",
        category_col="Kategorie",
        encodings=("utf-8-sig",),
    ),
    # Minimal generic format: date,amount,counterparty,purpose[,currency]
    "generic": CsvProfile(
        name="generic",
        date_col="date",
        amount_col="amount",
        purpose_col="purpose",
        counterparty_col="counterparty",
        currency_col="currency",
        delimiter=",",
        encodings=("utf-8-sig",),
    ),
}


def _read_text(path: Path, encodings: tuple[str, ...]) -> str:
    last_err: Exception | None = None
    for enc in encodings:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError as e:  # pragma: no cover - depends on input
            last_err = e
    raise last_err  # type: ignore[misc]


def _find_header(lines: list[str], profile: CsvProfile) -> int:
    """Index of the line that contains all required columns."""
    for i, line in enumerate(lines):
        cols = next(csv.reader([line], delimiter=profile.delimiter), [])
        cols = [c.strip() for c in cols]
        if all(rc in cols for rc in profile.required_cols):
            return i
    raise ValueError(
        f"profile '{profile.name}': header row with columns "
        f"{profile.required_cols} not found"
    )


def _counterparty(row: dict[str, str], profile: CsvProfile, amount: Decimal) -> str:
    if profile.counterparty_by_sign:
        neg_col, pos_col = profile.counterparty_by_sign
        return row.get(neg_col if amount < 0 else pos_col, "")
    return row.get(profile.counterparty_col or "", "")


def _row_to_transaction(
    row: dict[str, str], profile: CsvProfile, account: str
) -> Transaction | None:
    raw_amount = row.get(profile.amount_col, "")
    raw_date = row.get(profile.date_col, "")
    if not raw_amount or not raw_date:
        return None  # trailing/summary lines
    booking_type = row.get(profile.booking_type_col or "", "").strip()
    if booking_type in profile.exclude_types:
        return None  # internal/funding legs (PayPal double entry)
    amount = parse_german_decimal(raw_amount)
    counterparty = _counterparty(row, profile, amount)
    cp_lower = counterparty.lower()
    if any(s.lower() in cp_lower for s in profile.exclude_counterparty_contains):
        return None  # internal PayPal settlement, not a merchant payment

    value_date = None
    if profile.value_date_col and row.get(profile.value_date_col):
        value_date = parse_date(row[profile.value_date_col])

    currency = row.get(profile.currency_col or "") or profile.currency

    return Transaction(
        booking_date=parse_date(raw_date),
        amount=amount,
        currency=currency,
        account=account,
        counterparty=counterparty,
        purpose=row.get(profile.purpose_col, ""),
        value_date=value_date,
        booking_type=booking_type,
        category=row.get(profile.category_col or "") or None,
        source=f"csv:{profile.name}",
    )


def import_csv(path: str | Path, profile_name: str, account: str) -> list[Transaction]:
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ValueError(
            f"unknown profile '{profile_name}' (available: {', '.join(PROFILES)})"
        )
    lines = _read_text(Path(path), profile.encodings).splitlines()
    header_idx = _find_header(lines, profile)
    reader = csv.DictReader(
        io.StringIO("\n".join(lines[header_idx:])), delimiter=profile.delimiter
    )
    txs = []
    for raw_row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw_row.items()}
        tx = _row_to_transaction(row, profile, account)
        if tx is not None:
            txs.append(tx)
    return txs
