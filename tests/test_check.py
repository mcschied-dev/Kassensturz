"""Sanity-check invariants — the court-proofness layer."""

from datetime import date
from decimal import Decimal

from kassensturz.check import (
    check_balance_reconciliation,
    check_monthly_gaps,
    check_settlement_backing,
    check_transfer_residual,
    check_uncategorized_income,
)
from kassensturz.model import Transaction
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


def levels(findings, check_name):
    return [f.level for f in findings if f.check == check_name]


def test_balance_reconciliation_exact_and_opening():
    store = Store(":memory:")
    store.add([tx(account="voll", amount=Decimal("100.00"))])
    store.add([tx(account="alt", amount=Decimal("50.00"))])
    store.record_balances(
        [
            ("voll", "2026-07-01", 10000, "EUR", "t"),   # == Summe → ok
            ("alt", "2026-07-01", 20000, "EUR", "t"),    # 150 € Anfangssaldo → info
            ("depot", "2026-07-01", 99900, "EUR", "t"),  # nur Saldo → info
        ]
    )
    f = check_balance_reconciliation(store)
    msgs = {m.message.split(":")[0]: m.level for m in f}
    assert msgs["voll"] == "ok"
    assert msgs["alt"] == "info"
    assert msgs["depot"] == "info"


def test_settlement_backing_flags_stale_detail():
    store = Store(":memory:")
    # settlements keep arriving, detail source stopped months earlier
    store.add(
        [
            tx(booking_date=date(2026, 6, 15), category="Kreditkarten-Abrechnung"),
            tx(account="mm-karte", booking_date=date(2026, 2, 1)),
        ]
    )
    f = check_settlement_backing(store, today=date(2026, 7, 1))
    assert levels(f, "deckung") == ["warn"]
    # fresh detail → ok
    store.add([tx(account="mm-karte", booking_date=date(2026, 6, 10))])
    f = check_settlement_backing(store, today=date(2026, 7, 1))
    assert levels(f, "deckung") == ["ok"]


def test_settlement_without_any_detail_is_warned():
    store = Store(":memory:")
    store.add([tx(category="PayPal-Funding (neutral)")])
    f = check_settlement_backing(store)
    assert levels(f, "deckung") == ["warn"]


def test_transfer_residual():
    store = Store(":memory:")
    store.add(
        [
            tx(amount=Decimal("-500.00"), category="Umbuchung", account="a"),
            tx(amount=Decimal("500.00"), category="Umbuchung", account="b"),
            # external flow (no counter-booking in DB) must not trip the check
            tx(
                amount=Decimal("-5000.00"),
                category="Umbuchung",
                counterparty="Trade Republic",
            ),
        ]
    )
    assert levels(check_transfer_residual(store), "umbuchung") == ["ok"]
    store.add([tx(amount=Decimal("-2000.00"), category="Umbuchung", purpose="solo")])
    assert levels(check_transfer_residual(store), "umbuchung") == ["warn"]


def test_uncategorized_income():
    store = Store(":memory:")
    store.add([tx(amount=Decimal("300.00"), category=None)])
    assert levels(check_uncategorized_income(store), "einnahmen") == ["ok"]
    store.add([tx(amount=Decimal("9000.00"), category=None, purpose="wo her?")])
    assert levels(check_uncategorized_income(store), "einnahmen") == ["warn"]


def test_spesen_matching_and_grace():
    from kassensturz.check import check_spesen, match_spesen

    store = Store(":memory:")
    store.add(
        [
            # exakt erstatteter Beleg
            tx(
                account="mm-karte",
                booking_date=date(2026, 3, 2),
                amount=Decimal("-220.00"),
                counterparty="Stayery Bochum",
            ),
            tx(
                account="dkb",
                booking_date=date(2026, 3, 10),
                amount=Decimal("220.00"),
                counterparty="emproof GmbH",
                purpose="RNr. 110-26030056",
                category="Spesen",
            ),
            # vergessener Beleg, älter als Kulanzfrist
            tx(
                account="mm-karte",
                booking_date=date(2026, 4, 1),
                amount=Decimal("-333.00"),
                counterparty="Mercure Bochum City",
            ),
            # frischer Beleg — noch in der Kulanzfrist, keine Warnung
            tx(
                account="mm-karte",
                booking_date=date(2026, 6, 28),
                amount=Decimal("-99.00"),
                counterparty="DB Vertrieb GmbH",
            ),
        ]
    )
    result = match_spesen(store, today=date(2026, 7, 1))
    assert result["unmatched_total"] == 33300  # nur der Mercure-Beleg
    assert result["surplus"] == 0
    assert levels(check_spesen(store, today=date(2026, 7, 1)), "spesen") == ["warn"]
    # Sammelreport deckt den offenen Betrag → ok
    store.add(
        [
            tx(
                account="dkb",
                booking_date=date(2026, 5, 30),
                amount=Decimal("400.00"),
                counterparty="emproof GmbH",
                purpose="Travel Expenses sequenz-id 42",
                category="Spesen",
            )
        ]
    )
    assert levels(check_spesen(store, today=date(2026, 7, 1)), "spesen") == ["ok"]


def test_monthly_gaps():
    store = Store(":memory:")
    # 100+ bookings in Jan..Mar with April missing, May present → warn
    batch = [
        tx(account="giro", booking_date=date(2026, m, d))
        for m in (1, 2, 3)
        for d in range(1, 28)
    ] + [tx(account="giro", booking_date=date(2026, 5, 2))]
    store.add(batch)
    f = check_monthly_gaps(store, min_rows=50)
    assert levels(f, "lücken") == ["warn"]
    assert "2026-04" in f[0].message
    store.add([tx(account="giro", booking_date=date(2026, 4, 10))])
    assert levels(check_monthly_gaps(store, min_rows=50), "lücken") == ["ok"]


def test_paypal_neutral_deckung():
    from kassensturz.check import check_paypal_neutral_deckung
    store = Store(":memory:")
    store.add(
        [
            # gedeckt: Später-zahlen-Rate, Kauf 30 Tage früher im paypal-Konto
            tx(account="dkb-3527", counterparty="PayPal - Später zahlen",
               amount=Decimal("-461.70"), booking_date=date(2026, 3, 6),
               category="PayPal-Funding (neutral)"),
            tx(account="paypal", counterparty="Galaxus Deutschland GmbH",
               amount=Decimal("-461.70"), booking_date=date(2026, 2, 3)),
            # exempt: PayPals eigene Einzugszeile (Sammel-Lastschrift)
            tx(account="ksk-giro", counterparty="PayPal (Europe) S.a r.l.",
               amount=Decimal("-728.85"), booking_date=date(2024, 11, 14),
               category="PayPal-Funding (neutral)"),
        ]
    )
    assert levels(check_paypal_neutral_deckung(store), "paypal-deckung") == ["ok"]

    # echte Ausgabe fälschlich neutralisiert (kein Zwilling, Händler-Leg) → warn
    store.add(
        [
            tx(account="mm-birgit", counterparty="Paypal *Ebay De",
               amount=Decimal("-827.98"), booking_date=date(2021, 11, 10),
               category="PayPal-Funding (neutral)"),
        ]
    )
    f = check_paypal_neutral_deckung(store)
    assert levels(f, "paypal-deckung") == ["warn"]
    assert "827.98" in f[0].message


def test_category_log_trigger():
    store = Store(":memory:")
    store.add([tx(counterparty="Netflix")])
    h = store.query()[0]["hash"]
    store.set_category([h], "Medien")
    store.set_category([h], "Medien")  # unverändert → kein Log-Eintrag
    store.set_category([h], "PayPal-Funding (neutral)")
    log = store.conn.execute(
        "SELECT alt, neu FROM category_log ORDER BY id").fetchall()
    assert [(r["alt"], r["neu"]) for r in log] == [
        (None, "Medien"), ("Medien", "PayPal-Funding (neutral)")]
