"""CSV import tests — all fixture data is synthetic."""

from datetime import date
from decimal import Decimal

import pytest

from kassensturz.connectors.csv_import import (
    import_csv,
    parse_german_decimal,
)

DKB_SAMPLE = '''"Girokonto";"DE00120300000000000000"
""
"Buchungsdatum";"Wertstellung";"Status";"Zahlungspflichtige*r";"Zahlungsempfänger*in";"Verwendungszweck";"Umsatztyp";"IBAN";"Betrag (€)";"Gläubiger-ID";"Mandatsreferenz";"Kundenreferenz"
"03.06.2026";"03.06.2026";"Gebucht";"Max Muster";"REWE Markt GmbH";"REWE SAGT DANKE 44440001";"Ausgang";"DE00111111110000000000";"-54,30";"";"";""
"01.06.2026";"01.06.2026";"Gebucht";"ACME GmbH";"Max Muster";"GEHALT 06/2026";"Eingang";"DE00222222220000000000";"3.500,00";"";"";""
'''

SPARKASSE_SAMPLE = '''"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";"Glaeubiger ID";"Mandatsreferenz";"Kundenreferenz (End-to-End)";"Sammlerreferenz";"Lastschrift Ursprungsbetrag";"Auslagenersatz Ruecklastschrift";"Beguenstigter/Zahlungspflichtiger";"Kontonummer/IBAN";"BIC (SWIFT-Code)";"Betrag";"Waehrung";"Info"
"DE00700000000000000000";"02.06.26";"02.06.26";"LASTSCHRIFT";"Miete Juni";"DE00ZZZ00000000000";"M-001";"";"";"";"";"Hausverwaltung Muster";"DE00333333330000000000";"XXXXDEYY";"-1.200,00";"EUR";"Umsatz gebucht"
'''

GENERIC_SAMPLE = """date,amount,counterparty,purpose,currency
2026-06-05,-10.50,Coffee Corner,Cappuccino,EUR
"""


def test_parse_german_decimal():
    assert parse_german_decimal("-1.234,56 €") == Decimal("-1234.56")
    assert parse_german_decimal("3.500,00") == Decimal("3500.00")
    assert parse_german_decimal("−54,30") == Decimal("-54.30")  # unicode minus
    assert parse_german_decimal("1234.56") == Decimal("1234.56")


def test_dkb_import(tmp_path):
    f = tmp_path / "dkb.csv"
    f.write_text(DKB_SAMPLE, encoding="utf-8")
    txs = import_csv(f, "dkb", account="dkb-giro")
    assert len(txs) == 2
    out, incoming = txs[0], txs[1]
    assert out.booking_date == date(2026, 6, 3)
    assert out.amount == Decimal("-54.30")
    # negative amount → counterparty is the payee
    assert out.counterparty == "REWE Markt GmbH"
    assert incoming.amount == Decimal("3500.00")
    # positive amount → counterparty is the payer
    assert incoming.counterparty == "ACME GmbH"
    assert incoming.booking_type == "Eingang"
    assert out.account == "dkb-giro"


def test_sparkasse_import_cp1252(tmp_path):
    f = tmp_path / "spk.csv"
    f.write_bytes(SPARKASSE_SAMPLE.encode("cp1252"))
    txs = import_csv(f, "sparkasse", account="spk-giro")
    assert len(txs) == 1
    t = txs[0]
    assert t.booking_date == date(2026, 6, 2)
    assert t.amount == Decimal("-1200.00")
    assert t.counterparty == "Hausverwaltung Muster"
    assert t.booking_type == "LASTSCHRIFT"
    assert t.currency == "EUR"


def test_generic_import(tmp_path):
    f = tmp_path / "generic.csv"
    f.write_text(GENERIC_SAMPLE, encoding="utf-8")
    txs = import_csv(f, "generic", account="misc")
    assert len(txs) == 1
    assert txs[0].amount == Decimal("-10.50")
    assert txs[0].counterparty == "Coffee Corner"


def test_unknown_profile(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("a;b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown profile"):
        import_csv(f, "nope", account="x")
