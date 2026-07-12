"""XLSX-Export: Struktur- und Semantik-Smoke-Test."""

from datetime import date
from decimal import Decimal

from openpyxl import load_workbook

from kassensturz.model import Transaction
from kassensturz.report.xlsx import _detect_abos, build_workbook
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


def test_workbook_sheets_and_consolidation(tmp_path, monkeypatch):
    # neutrales cwd: sonst zieht build_workbook Marcs echtes budget.yaml
    monkeypatch.chdir(tmp_path)
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
            # aktives Abo — sonst bleiben Verträge-KPI-Zweige unausgeführt
            # (genau so schlüpfte der mapping-NameError an den Tests vorbei)
            *[tx(booking_date=date(2025, m, 3), amount=Decimal("-9.99"),
                 counterparty="Netflix", category="Abos & Dienste")
              for m in range(1, 7)],
        ]
    )
    store.record_balances([("test", "2025-06-01", 123456, "EUR", "t")])
    out = build_workbook(store, tmp_path / "wb.xlsx", first_year="2024")
    wb = load_workbook(out)
    assert wb.sheetnames == ["Dashboard", "Verträge & Abos", "Forecast",
                             "2024", "2025"]
    ws = wb["2024"]
    kpis = {ws.cell(row=4, column=2 + i * 2).value:
            ws.cell(row=5, column=2 + i * 2).value for i in range(5)}
    assert kpis["Einnahmen"] == 3000.0         # Retoure zählt NICHT als Einnahme
    assert kpis["Ausgaben"] == -70.0           # Shopping netto, ohne Neutrale
    assert kpis["Sondereffekte"] == 9999.0     # separat ausgewiesen
    # unkategorisierte Zeilen zählen mit (kein NULL-Trap); −40 tail
    # + 6 × −9,99 Netflix = −99,94
    ws25 = wb["2025"]
    kpis25 = {ws25.cell(row=4, column=2 + i * 2).value:
              ws25.cell(row=5, column=2 + i * 2).value for i in range(5)}
    assert kpis25["Ausgaben"] == -99.94


def test_abo_alias_rebranding_und_robuster_rhythmus():
    """Rebranding (jumpers → A.I. Fitness): Alias-Einstellung verkettet die
    Zahlungsreihe über den Namenswechsel; ein ausgelassener Monat und
    Doppel-Abbuchungen am selben Tag zerlegen ein Abo nicht mehr."""
    store = Store(":memory:")
    txs = []
    # alter Name, monatlich — mit Doppelbuchung (2 Mitgliedschaften) und
    # einem ausgelassenen Monat (2025-01 fehlt)
    for m in (7, 8, 9, 10, 11, 12):
        txs.append(tx(booking_date=date(2024, m, 1), amount=Decimal("-29.90"),
                      counterparty="jumpers fitness GmbH", category="Sport & Hobby"))
        txs.append(tx(booking_date=date(2024, m, 1), amount=Decimal("-29.90"),
                      counterparty="jumpers fitness GmbH", category="Sport & Hobby",
                      purpose="zweite Mitgliedschaft"))
    for m in (2, 3, 4, 5):
        txs.append(tx(booking_date=date(2025, m, 1), amount=Decimal("-29.90"),
                      counterparty="jumpers fitness GmbH", category="Sport & Hobby"))
    # neuer Name führt die Reihe fort
    for m in (6, 7, 8, 9, 10):
        txs.append(tx(booking_date=date(2025, m, 1), amount=Decimal("-29.90"),
                      counterparty="A.I. Fitness Sued GmbH", category="Sport & Hobby"))
    store.add(txs)
    store.setting_set("abo_aliase", "jumpers=fitness")

    fit = [a for a in _detect_abos(store, today=date(2025, 10, 20))
           if a["key"] == "fitness"]
    assert len(fit) == 1
    assert fit[0]["aktiv"]
    assert fit[0]["anbieter"] == "A.I. Fitness Sued GmbH"  # aktueller Name
    assert fit[0]["seit"] == "2024-07-01"                  # Historie erhalten
    assert fit[0]["turnus"] == "monatlich"


def test_versicherungen_splitten_nach_vertragsnummer():
    """Mehrere Verträge beim selben Versicherer (HUK: 2× Kfz + Motorrad)
    dürfen nicht zu einer Sammelzeile verschmelzen — Split über die
    Vertragsnummer im Zweck, Kennzeichen als Namenszusatz."""
    store = Store(":memory:")
    txs = []
    for jahr in (2024, 2025, 2026):
        txs.append(tx(booking_date=date(jahr, 5, 23), amount=Decimal("-1832.20"),
                      counterparty="HUK-COBURG", category="Versicherung",
                      purpose=f"Kraftfahrt-Versicherung 667/865989-L, R-XI 197, Folgebeitrag {jahr}"))
        txs.append(tx(booking_date=date(jahr, 3, 1), amount=Decimal("-198.21"),
                      counterparty="HUK-COBURG", category="Versicherung",
                      purpose=f"Kraftfahrt-Versicherung 663/158628-B, Gp-M 975 {jahr}"))
    # geplatzte Lastschrift + Rücklastschrift (trägt den EIGENEN Namen!)
    # + erfolgreicher Wiedereinzug: das Storno-Paar hebt sich auf, nur
    # der Wiedereinzug zählt als Jahresbeitrag
    txs.append(tx(booking_date=date(2025, 12, 19), amount=Decimal("-1011.42"),
                  counterparty="HUK-COBURG", category="Versicherung",
                  purpose="Kraftfahrt-Versicherung 668/139640-W, R-MS 573, Folgebeitrag"))
    txs.append(tx(booking_date=date(2025, 12, 22), amount=Decimal("1011.42"),
                  counterparty="SCHIEDER MARC", category="Versicherung",
                  purpose="RUECKLASTSCHRIFT Sonstige Gruende HUK24, KRAFTFAHRT-VERSICHERUNG 668/139640-W"))
    txs.append(tx(booking_date=date(2026, 1, 2), amount=Decimal("-1011.42"),
                  counterparty="HUK-COBURG", category="Versicherung",
                  purpose="Kraftfahrt-Versicherung 668/139640-W, R-MS 573"))
    store.add(txs)

    huk = [a for a in _detect_abos(store, today=date(2026, 7, 12))
           if "HUK" in a["anbieter"]]
    assert len(huk) == 3
    namen = {a["anbieter"] for a in huk}
    assert any("R-XI 197" in n for n in namen)
    assert any("Gp-M 975" in n for n in namen)
    assert any("R-MS 573" in n for n in namen)
    assert all(a["aktiv"] for a in huk)
    rxi = next(a for a in huk if "R-XI" in a["anbieter"])
    assert rxi["turnus"] == "jährlich" and rxi["jahr"] == 183220
    rms = next(a for a in huk if "R-MS" in a["anbieter"])
    assert rms["jahr"] == 101142  # Rückläufer verrechnet, nicht verdoppelt


def test_abos_merge_namensvarianten_und_finden_jahresversicherungen():
    store = Store(":memory:")
    txs = []
    # Hallesche: Namensvariante wechselt (Umzug/FinTS-IBAN-Präfix) und der
    # Beitrag steigt — trotzdem EIN Vertrag, EINE Zeile, Betrag = aktuell
    for m in range(1, 13):
        txs.append(tx(booking_date=date(2024, m, 1), amount=Decimal("-80.00"),
                      counterparty="HALLESCHE Reinsburgstrasse 10",
                      category="Versicherung"))
    for m in range(1, 7):
        txs.append(tx(booking_date=date(2025, m, 1), amount=Decimal("-88.00"),
                      counterparty="DE24600501010001114445HALLESCHE",
                      category="Versicherung"))
    # Concordia: jährlich mit Beitragsanpassung → nie 3× derselbe Betrag,
    # muss über die Kategorie trotzdem auftauchen
    txs.append(tx(booking_date=date(2024, 3, 14), amount=Decimal("-277.20"),
                  counterparty="Concordia", category="Versicherung"))
    txs.append(tx(booking_date=date(2025, 2, 2), amount=Decimal("-264.00"),
                  counterparty="Concordia", category="Versicherung"))
    store.add(txs)

    abos = _detect_abos(store, today=date(2025, 7, 1))

    hallesche = [a for a in abos if "hallesche" in a["anbieter"].lower()]
    assert len(hallesche) == 1
    assert hallesche[0]["aktiv"]
    assert hallesche[0]["betrag"] == 8800
    assert hallesche[0]["seit"] == "2024-01-01"
    assert hallesche[0]["anbieter"] == "HALLESCHE"  # IBAN-Präfix entfernt

    concordia = [a for a in abos if a["anbieter"] == "Concordia"]
    assert len(concordia) == 1
    assert concordia[0]["turnus"] == "jährlich"
    assert concordia[0]["betrag"] == 26400
    assert concordia[0]["aktiv"]
