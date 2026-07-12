"""Report tests — data collection and TeX rendering (no compilation)."""

from datetime import date
from decimal import Decimal

from kassensturz.model import Transaction
from kassensturz.report.build import collect, render, tex_escape
from kassensturz.store import Store


def tx(**kwargs) -> Transaction:
    defaults = dict(
        booking_date=date(2026, 6, 1),
        amount=Decimal("-10.00"),
        currency="EUR",
        account="test",
        counterparty="Shop",
        purpose="Kauf",
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def make_store() -> Store:
    store = Store(":memory:")
    store.add(
        [
            tx(amount=Decimal("3500.00"), category="Gehalt", purpose="g1"),
            tx(amount=Decimal("-1200.00"), category="Miete & Co", purpose="m1"),
            tx(amount=Decimal("-500.00"), category="Umbuchung", purpose="u1"),
            tx(
                amount=Decimal("-2000.00"),
                category="Unterhalt Birgit",
                booking_date=date(2025, 5, 1),
                purpose="a1",
            ),
            tx(amount=Decimal("-2500.00"), category="Unterhalt Birgit", purpose="a2"),
        ]
    )
    store.record_balances([("dkb-4357", "2026-07-03", 54143889, "EUR", "dkb")])
    return store


def test_tex_escape():
    assert tex_escape("A&B 5% _x") == r"A\&B 5\% \_x"
    assert tex_escape("Müller GmbH") == "Müller GmbH"  # unicode untouched


def test_collect_excludes_umbuchung_from_totals():
    d = collect(make_store())
    assert d.income_cents == 350000
    assert d.expense_cents == -570000  # Miete + 2× Unterhalt, ohne Umbuchung


def test_sondereffekt_excluded_from_running_totals():
    store = make_store()
    store.add(
        [
            tx(
                amount=Decimal("838317.87"),
                category="DRACOON Exit (Sondereffekt)",
                counterparty="LUTZ / ABEL",
                purpose="Exit Tantieme Dracoon",
                booking_date=date(2024, 1, 17),
            )
        ]
    )
    d = collect(store)
    assert d.income_cents == 350000  # unverändert — Exit zählt nicht als Einkommen
    assert d.special_cents == 83831787
    tex = render(d)
    assert "Sondereffekte" in tex
    assert "Saldo inkl" in tex


def test_collect_focus_history():
    d = collect(make_store(), focus=["Unterhalt Birgit", "GibtEsNicht"])
    assert len(d.focus) == 1
    f = d.focus[0]
    assert [y["year"] for y in f["years"]] == ["2025", "2026"]
    assert f["total_cents"] == -450000


def test_year_matrix_in_full_period_report():
    d = collect(make_store())
    assert d.years == ["2025", "2026"]
    birgit = next(r for r in d.year_rows if r["name"] == "Unterhalt Birgit")
    assert birgit["cells"] == {"2025": -200000, "2026": -250000}
    assert birgit["total"] == -450000
    assert "Kategorien nach Jahren" in render(d)
    # month-scoped report keeps the flat table instead
    d_month = collect(make_store(), year=2026, month=6)
    assert d_month.year_rows == []
    assert "Kategorien nach Jahren" not in render(d_month)


def test_render_produces_valid_looking_tex():
    d = collect(make_store(), focus=["Unterhalt Birgit"])
    tex = render(d)
    assert tex.startswith("%")
    assert r"\begin{document}" in tex
    assert "Miete \\& Co" in tex  # escaping applied
    assert "541.438,89" in tex  # balance rendered
    assert "Unterhalt Birgit — Historie" in tex
    assert "(((" not in tex  # no unrendered placeholders


def test_vertraege_pdf_daten_und_rendering():
    """collect_vertraege trennt Kosten von Sparplänen; Template rendert
    ohne TeX-Sonderzeichen-Unfälle (kein latexmk nötig)."""
    from datetime import date
    from decimal import Decimal

    from kassensturz.model import Transaction
    from kassensturz.report.build import collect_vertraege, render_vertraege
    from kassensturz.store import Store

    store = Store(":memory:")
    txs = []
    for m in range(1, 8):
        txs.append(Transaction(
            booking_date=date(2026, m, 3), amount=Decimal("-9.99"),
            currency="EUR", account="giro", counterparty="Netflix & Co KG",
            purpose="abo", category="Abos & Dienste"))
        txs.append(Transaction(
            booking_date=date(2026, m, 28), amount=Decimal("-600.00"),
            currency="EUR", account="giro", counterparty="Trade Republic",
            purpose="sparplan", category="Umbuchung"))
    # beendetes Abo: taucht nur mit mit_inaktiven auf
    for m in range(1, 7):
        txs.append(Transaction(
            booking_date=date(2024, m, 3), amount=Decimal("-5.00"),
            currency="EUR", account="giro", counterparty="Altvertrag",
            purpose="abo", category="Abos & Dienste"))
    store.add(txs)

    nur_aktiv = collect_vertraege(store, today=date(2026, 7, 15))
    assert nur_aktiv["kosten_jahr"] == 9_99 * 12
    assert nur_aktiv["sparen_jahr"] == 600_00 * 12  # getrennt, keine Kosten
    assert all(a["aktiv"] for g in nur_aktiv["gruppen"] for a in g["eintraege"])

    alle = collect_vertraege(store, mit_inaktiven=True, today=date(2026, 7, 15))
    anbieter = [a["anbieter"] for g in alle["gruppen"] for a in g["eintraege"]]
    assert "Altvertrag" in anbieter

    tex = render_vertraege(alle)
    assert "\\documentclass" in tex
    assert "Netflix \\& Co KG" in tex  # tex-escaped
    assert "beendet" in tex


def test_spesen_monatsbericht_daten_und_rendering():
    from datetime import date
    from decimal import Decimal

    from kassensturz.model import Transaction
    from kassensturz.report.build import (
        collect_spesen_monat,
        render_spesen,
        spesen_monate,
    )
    from kassensturz.store import Store

    store = Store(":memory:")
    store.add([
        Transaction(booking_date=date(2026, 6, 3), amount=Decimal("-220.00"),
                    currency="EUR", account="mm-karte",
                    counterparty="Stayery Bochum", purpose="hotel"),
        Transaction(booking_date=date(2026, 6, 20), amount=Decimal("220.00"),
                    currency="EUR", account="dkb",
                    counterparty="Arbeitgeber GmbH",
                    purpose="RNr. 110-26060099", category="Spesen"),
        Transaction(booking_date=date(2026, 6, 25), amount=Decimal("-99.00"),
                    currency="EUR", account="dkb",
                    counterparty="DB Vertrieb GmbH", purpose="fahrkarte"),
    ])
    assert spesen_monate(store) == ["2026-06"]
    d = collect_spesen_monat(store, "2026-06", today=date(2026, 7, 13))
    assert d["angefallen"] == 319_00
    assert d["beglichen"] == 220_00
    assert d["offen"] == 99_00 and d["n_offen"] == 1
    assert d["eingang_summe"] == 220_00
    tex = render_spesen(d)
    assert "\\documentclass" in tex and "Stayery Bochum" in tex
    assert "beglichen" in tex and "offen" in tex


def test_cfo_bericht_daten_und_rendering():
    from datetime import date
    from decimal import Decimal

    from kassensturz.model import Transaction
    from kassensturz.report.build import collect_cfo, render_cfo
    from kassensturz.store import Store

    store = Store(":memory:")
    store.add([
        Transaction(booking_date=date(2026, 6, 28), amount=Decimal("3000.00"),
                    currency="EUR", account="giro", counterparty="AG GmbH",
                    purpose="Gehalt", category="Gehalt"),
        Transaction(booking_date=date(2026, 7, 2), amount=Decimal("-500.00"),
                    currency="EUR", account="giro",
                    counterparty="DE00123456780000000000Vermieter AG",
                    purpose="Miete", category="Wohnen & Nebenkosten"),
    ])
    store.record_balances([("giro", "2026-07-10", 250000, "EUR", "t")])
    d = collect_cfo(store, today=date(2026, 7, 12))
    assert d["verm_ende"] == 250000
    assert d["ausgaben"] == 50000
    assert len(d["kernaussagen"]) == 4
    tex = render_cfo(d)
    assert "CFO-Bericht" in tex and "Vermögensbilanz" in tex
    assert "Vermieter AG" in tex and "DE0012345678" not in tex  # IBAN weg
    assert "Datenintegrität" in tex
