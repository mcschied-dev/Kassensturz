"""Categorization + transfer matching tests."""

from datetime import date
from decimal import Decimal

from kassensturz.categorize import (apply_rules, load_rules, match_transfers,
                                    neutralize_funding_legs)
from kassensturz.model import Transaction
from kassensturz.store import Store

RULES_YAML = """
categories:
  # account grenzt ein: dieselbe "paypal"-Zeile ist auf mm-birgit eine
  # echte Ausgabe (Partnerkarte ohne eigenes PayPal-Detailkonto)
  - name: PayPal-Funding (neutral)
    counterparty: ["paypal"]
    account: ["^dkb", "^paypal$"]
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


def test_account_scope_grenzt_regel_ein(tmp_path):
    store = Store(":memory:")
    store.add(
        [
            tx(account="dkb-3527", counterparty="PayPal (Europe) S.a.r.l."),
            tx(account="mm-birgit", counterparty="Paypal *Ebay De"),
        ]
    )
    counts = apply_rules(store, make_ruleset(tmp_path))
    # dkb-Leg neutralisiert, Birgits Karten-Kauf bleibt unkategorisiert
    assert counts == {"PayPal-Funding (neutral)": 1}
    offen = store.query(uncategorized=True)
    assert [r["account"] for r in offen] == ["mm-birgit"]


def test_neutralize_funding_legs_nur_mit_namensgleichem_zwilling():
    store = Store(":memory:")
    store.add(
        [
            # gekürztes DKB-Leg + betrags-/namensgleiche paypal-Zeile → neutral
            tx(account="dkb-3527", counterparty="Apple / iTunes",
               amount=Decimal("-9.99")),
            tx(account="paypal", counterparty="Apple Services",
               amount=Decimal("-9.99"), booking_date=date(2026, 5, 30)),
            # gleicher Betrag, anderer Händler → Zufalls-Zwilling, bleibt offen
            tx(account="dkb-3527", counterparty="Amazon",
               amount=Decimal("-9.99")),
            # fremdes Konto (Partnerkarte) wird nie angefasst
            tx(account="mm-birgit", counterparty="Apple / iTunes",
               amount=Decimal("-9.99")),
        ]
    )
    assert neutralize_funding_legs(store) == 1
    neutral = store.query(category="PayPal-Funding (neutral)")
    assert [r["account"] for r in neutral] == ["dkb-3527"]
    assert neutral[0]["counterparty"] == "Apple / iTunes"


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
