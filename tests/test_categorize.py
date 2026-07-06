"""Categorization + transfer matching tests."""

from datetime import date
from decimal import Decimal

from kassensturz.categorize import apply_rules, load_rules, match_transfers
from kassensturz.model import Transaction
from kassensturz.store import Store

RULES_YAML = """
categories:
  - name: Lebensmittel
    counterparty: ["rewe", "edeka"]
  - name: Gehalt
    purpose: ["gehalt"]
transfers:
  category: Umbuchung
  max_days: 3
"""


def tx(**kwargs) -> Transaction:
    defaults = dict(
        booking_date=date(2026, 6, 1),
        amount=Decimal("-10.00"),
        currency="EUR",
        account="a",
        counterparty="",
        purpose="",
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def make_ruleset(tmp_path):
    f = tmp_path / "rules.yaml"
    f.write_text(RULES_YAML, encoding="utf-8")
    return load_rules(f)


def test_apply_rules_first_match_wins(tmp_path):
    store = Store(":memory:")
    store.add(
        [
            tx(counterparty="REWE Markt GmbH", purpose="REWE SAGT DANKE"),
            tx(counterparty="ACME GmbH", purpose="GEHALT 06/2026", amount=Decimal("3500.00")),
            tx(counterparty="Unbekannt", purpose="???", amount=Decimal("-1.00")),
        ]
    )
    counts = apply_rules(store, make_ruleset(tmp_path))
    assert counts == {"Lebensmittel": 1, "Gehalt": 1}
    assert len(store.query(uncategorized=True)) == 1


def test_transfer_matching(tmp_path):
    store = Store(":memory:")
    store.add(
        [
            # internal transfer dkb → spk, 1 day apart
            tx(account="dkb", amount=Decimal("-500.00"), purpose="Übertrag"),
            tx(
                account="spk",
                amount=Decimal("500.00"),
                purpose="Übertrag",
                booking_date=date(2026, 6, 2),
            ),
            # same amount but SAME account → not a transfer
            tx(account="dkb", amount=Decimal("-500.00"), purpose="anderes"),
        ]
    )
    ruleset = make_ruleset(tmp_path)
    assert match_transfers(store, ruleset) == 2
    umbuchungen = store.query(category="Umbuchung")
    assert {r["account"] for r in umbuchungen} == {"dkb", "spk"}


def test_transfer_outside_window_not_matched(tmp_path):
    store = Store(":memory:")
    store.add(
        [
            tx(account="dkb", amount=Decimal("-500.00")),
            tx(
                account="spk",
                amount=Decimal("500.00"),
                booking_date=date(2026, 6, 10),  # 9 days later
            ),
        ]
    )
    assert match_transfers(store, make_ruleset(tmp_path)) == 0
