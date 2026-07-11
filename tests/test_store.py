"""Store + model tests — dedup semantics are the critical part."""

from datetime import date
from decimal import Decimal

from kassensturz.model import Transaction, assign_seq, fmt_eur
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


def test_reimport_is_noop():
    store = Store(":memory:")
    batch = [tx(), tx(amount=Decimal("-20.00"))]
    assert store.add(batch) == (2, 0)
    assert store.add(batch) == (0, 2)  # same file again → all duplicates


def test_identical_bookings_both_kept():
    """Two real coffees, same day, same amount → both stored."""
    store = Store(":memory:")
    inserted, skipped = store.add([tx(), tx()])
    assert (inserted, skipped) == (2, 0)
    # and re-importing both still dedups
    assert store.add([tx(), tx()]) == (0, 2)


def test_assign_seq_deterministic():
    a, b = assign_seq([tx(), tx()])
    assert (a.seq, b.seq) == (0, 1)
    assert a.tx_hash() != b.tx_hash()


def test_amount_cents():
    assert tx(amount=Decimal("-54.30")).amount_cents == -5430
    assert tx(amount=Decimal("3500.00")).amount_cents == 350000


def test_query_filters():
    store = Store(":memory:")
    store.add(
        [
            tx(booking_date=date(2026, 5, 31), category=None),
            tx(booking_date=date(2026, 6, 1), amount=Decimal("-1.00")),
        ]
    )
    assert len(store.query(year=2026, month=6)) == 1
    assert len(store.query(year=2026)) == 2
    assert len(store.query(uncategorized=True)) == 2


def test_summary_and_set_category():
    store = Store(":memory:")
    store.add([tx(), tx(amount=Decimal("-5.00"), purpose="anders")])
    rows = store.query()
    store.set_category([rows[0]["hash"]], "Kaffee")
    summary = store.summary(year=2026)
    cats = {r["category"]: r["total_cents"] for r in summary}
    assert "Kaffee" in cats
    assert "(unkategorisiert)" in cats


def test_adopt_categories_only_fills_null():
    store = Store(":memory:")
    a = tx(purpose="a")
    b = tx(purpose="b")
    store.add([a, b])
    rows = store.query()
    # rule already categorized one row — adoption must not touch it
    store.set_category([r["hash"] for r in rows if r["purpose"] == "a"], "Regel")
    from dataclasses import replace

    adopted = store.adopt_categories(
        [replace(a, category="Bank-Kat"), replace(b, category="Bank-Kat")]
    )
    assert adopted == 1
    cats = {r["purpose"]: r["category"] for r in store.query()}
    assert cats == {"a": "Regel", "b": "Bank-Kat"}


def test_balances_snapshot_and_latest():
    store = Store(":memory:")
    store.record_balances([("dkb-giro", "2026-07-01", 100000, "EUR", "dkb")])
    store.record_balances(
        [
            ("dkb-giro", "2026-07-03", 120000, "EUR", "dkb"),
            ("dkb-tagesgeld", "2026-07-03", 54000000, "EUR", "dkb"),
        ]
    )
    # same account+date overwrites instead of duplicating
    store.record_balances([("dkb-giro", "2026-07-03", 125000, "EUR", "dkb")])
    latest = {r["account"]: r["balance_cents"] for r in store.latest_balances()}
    assert latest == {"dkb-giro": 125000, "dkb-tagesgeld": 54000000}


def test_balances_at_three_statuses():
    store = Store(":memory:")
    # "old" account: history reaches past the Stichtag
    store.add(
        [
            tx(account="alt", booking_date=date(2022, 3, 1), amount=Decimal("100.00")),
            tx(account="alt", booking_date=date(2024, 6, 1), amount=Decimal("-40.00")),
        ]
    )
    # "new" account: opened after Stichtag, fully covered (sum == balance)
    store.add(
        [tx(account="neu", booking_date=date(2024, 2, 1), amount=Decimal("500.00"))]
    )
    # "gap" account: history starts after Stichtag but doesn't explain balance
    store.add(
        [tx(account="luecke", booking_date=date(2024, 2, 1), amount=Decimal("10.00"))]
    )
    store.record_balances(
        [
            ("alt", "2026-07-03", 20000, "EUR", "t"),      # heute 200 €
            ("neu", "2026-07-03", 50000, "EUR", "t"),      # 500 € = Summe
            ("luecke", "2026-07-03", 99900, "EUR", "t"),   # 999 € ≠ 10 €
        ]
    )
    result = {r["account"]: r for r in store.balances_at("2023-05-05")}
    # alt: 200 € heute minus (-40 €) nach Stichtag = 240 €
    assert result["alt"]["status"] == "ok"
    assert result["alt"]["balance_cents"] == 24000
    assert result["neu"]["status"] == "nicht_existent"
    assert result["neu"]["balance_cents"] == 0
    assert result["luecke"]["status"] == "unbekannt"
    assert result["luecke"]["balance_cents"] is None


def test_consolidated_ledger_counts_detail_accounts():
    """Seit der Konsolidierung zählen Detail-Konten (paypal, mm-karte) im
    Cashflow mit — die Bank-Sammelbuchungen werden stattdessen über
    NEUTRAL_CATEGORIES im Report neutralisiert."""
    store = Store(":memory:")
    store.add(
        [
            tx(account="dkb", amount=Decimal("-10.00"), category="Shopping"),
            tx(account="paypal", amount=Decimal("-10.00"), category="Shopping"),
        ]
    )
    shopping = [r for r in store.summary() if r["category"] == "Shopping"]
    assert shopping[0]["total_cents"] == -2000  # beide zählen
    assert len(store.query(account="paypal")) == 1


def test_fmt_eur():
    assert fmt_eur(-123456) == "-1.234,56 €"
    assert fmt_eur(350000) == "3.500,00 €"
    assert fmt_eur(-5, "USD") == "-0,05 USD"
