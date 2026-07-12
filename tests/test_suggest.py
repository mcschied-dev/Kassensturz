"""ML-Kategorievorschläge: lernt Muster aus gelabelten Buchungen."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from kassensturz.model import Transaction
from kassensturz.store import Store
from kassensturz.suggest import suggestions

pytest.importorskip("sklearn")


def tx(i: int, counterparty: str, category: str | None,
       amount: str = "-10.00") -> Transaction:
    return Transaction(
        booking_date=date(2024, 1, 1) + timedelta(days=i % 700),
        amount=Decimal(amount),
        currency="EUR",
        account="test",
        counterparty=counterparty,
        purpose=f"kauf {i}",
        category=category,
    )


def test_suggest_erkennt_textvarianten():
    store = Store(":memory:")
    txs = []
    i = 0
    # Trainingsdaten: zwei klar getrennte Kategorien
    for name in ("REWE Regensburg", "EDEKA Markt", "Lidl Filiale 12"):
        for _ in range(40):
            txs.append(tx(i, name, "Lebensmittel"))
            i += 1
    for name in ("Amazon.de", "AMZN Mktp DE", "Zalando SE"):
        for _ in range(40):
            txs.append(tx(i, name, "Shopping"))
            i += 1
    # Unkategorisiert: Textvarianten der bekannten Händler
    txs.append(tx(i, "REWE Markt GmbH Goeppingen", None))
    i += 1
    txs.append(tx(i, "AMZN.Mktp.DE.XY123", None))
    i += 1
    store.add(txs)

    items, meta = suggestions(store, min_confidence=0.5)
    assert meta["n_train"] == 240
    by_cp = {s.counterparty: s for s in items}
    # "AMZN Mktp DE" normalisiert auf denselben Kern → Stufe 2 (historisch)
    amzn = by_cp["AMZN.Mktp.DE.XY123"]
    assert amzn.category == "Shopping"
    assert amzn.source.startswith("historisch")
    # anderer Kern ("rewe markt" vs. "rewe regensburg") → Stufe 4 (ML)
    rewe = by_cp["REWE Markt GmbH Goeppingen"]
    assert rewe.category == "Lebensmittel"
    assert rewe.source == "ml"
