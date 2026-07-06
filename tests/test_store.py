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


def test_fmt_eur():
    assert fmt_eur(-123456) == "-1.234,56 €"
    assert fmt_eur(350000) == "3.500,00 €"
    assert fmt_eur(-5, "USD") == "-0,05 USD"
