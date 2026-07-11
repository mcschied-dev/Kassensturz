"""XLSX-Export: Struktur- und Semantik-Smoke-Test."""

from datetime import date
from decimal import Decimal

from openpyxl import load_workbook

from kassensturz.model import Transaction
from kassensturz.report.xlsx import build_workbook
from kassensturz.store import Store


def tx(**kwargs) -> Transaction:
    defaults = dict(
        booking_date=date(2024, 6, 1),
        amount=Decimal("-10.00"),
        currency="EUR",
        account="test",
        counterparty="Shop",
        purpose="Kauf",
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def test_workbook_sheets_and_consolidation(tmp_path):
    store = Store(":memory:")
    store.add(
        [
            tx(booking_date=date(2024, 3, 1), amount=Decimal("-100.00"),
               category="Shopping"),
            # Retoure: mindert die Shopping-Ausgaben, ist KEIN Einkommen
            tx(booking_date=date(2024, 3, 9), amount=Decimal("30.00"),
               category="Shopping", purpose="retoure"),
            tx(booking_date=date(2024, 4, 1), amount=Decimal("3000.00"),
               category="Gehalt", purpose="lohn"),
            # neutral & Sondereffekt dürfen NICHT in Einnahmen/Ausgaben zählen
            tx(booking_date=date(2024, 5, 1), amount=Decimal("-500.00"),
               category="Kreditkarten-Abrechnung"),
            tx(booking_date=date(2024, 5, 2), amount=Decimal("9999.00"),
               category="Exit (Sondereffekt)"),
            tx(booking_date=date(2025, 1, 5), amount=Decimal("-40.00"),
               category=None, purpose="tail"),
        ]
    )
    store.record_balances([("test", "2025-06-01", 123456, "EUR", "t")])
    out = build_workbook(store, tmp_path / "wb.xlsx", first_year="2024")
    wb = load_workbook(out)
    assert wb.sheetnames == ["Dashboard", "2024", "2025"]
    ws = wb["2024"]
    kpis = {ws.cell(row=4, column=2 + i * 2).value:
            ws.cell(row=5, column=2 + i * 2).value for i in range(5)}
    assert kpis["Einnahmen"] == 3000.0         # Retoure zählt NICHT als Einnahme
    assert kpis["Ausgaben"] == -70.0           # Shopping netto, ohne Neutrale
    assert kpis["Sondereffekte"] == 9999.0     # separat ausgewiesen
    # unkategorisierte Zeilen zählen mit (kein NULL-Trap)
    ws25 = wb["2025"]
    kpis25 = {ws25.cell(row=4, column=2 + i * 2).value:
              ws25.cell(row=5, column=2 + i * 2).value for i in range(5)}
    assert kpis25["Ausgaben"] == -40.0
