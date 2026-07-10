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
