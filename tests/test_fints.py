"""FinTS connector tests — mapping and config only; no bank contact."""

from datetime import date
from decimal import Decimal

import pytest

from kassensturz.config import FintsConnection, load_config
from kassensturz.connectors.fints_client import mt940_to_transaction

CONFIG_YAML = """
fints:
  - name: spk
    blz: 75050000
    username: 1234567890
    endpoint: https://banking-by5.s-fints-pt-by.de/fints30
    accounts:
      DE00750500000000000000: spk-giro
  - name: dkb
    blz: "12030000"
    username: "user"
    endpoint: https://banking-dkb.s-fints-pt-dkb.de/fints30
    product_id: ABCDEF
"""


class FakeAmount:
    def __init__(self, amount: str, currency: str = "EUR"):
        self.amount = Decimal(amount)
        self.currency = currency


def test_mt940_mapping():
    data = {
        "date": date(2026, 6, 15),
        "entry_date": date(2026, 6, 16),
        "amount": FakeAmount("-42.50"),
        "applicant_name": "REWE Markt GmbH",
        "purpose": "REWE SAGT DANKE",
        "posting_text": "KARTENZAHLUNG",
    }
    t = mt940_to_transaction(data, "spk-giro", "fints:spk")
    assert t.booking_date == date(2026, 6, 15)
    assert t.value_date == date(2026, 6, 16)
    assert t.amount == Decimal("-42.50")
    assert t.currency == "EUR"
    assert t.counterparty == "REWE Markt GmbH"
    assert t.booking_type == "KARTENZAHLUNG"
    assert t.account == "spk-giro"
    assert t.source == "fints:spk"


def test_mt940_mapping_sparse_fields():
    """Banks omit fields freely — mapping must not blow up."""
    data = {"date": date(2026, 6, 1), "amount": FakeAmount("10.00")}
    t = mt940_to_transaction(data, "x", "fints:spk")
    assert t.counterparty == ""
    assert t.purpose == ""
    assert t.value_date is None


def test_mt940_additional_purpose_appended():
    data = {
        "date": date(2026, 6, 1),
        "amount": FakeAmount("-1.00"),
        "purpose": "Teil eins",
        "additional_purpose": "Teil zwei",
    }
    assert mt940_to_transaction(data, "x", "s").purpose == "Teil eins Teil zwei"


def test_load_config(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(CONFIG_YAML, encoding="utf-8")
    cfg = load_config(f)
    assert len(cfg.fints) == 2
    spk = cfg.connection("spk")
    assert spk.blz == "75050000"  # int in yaml → str
    assert spk.username == "1234567890"
    assert spk.accounts == {"DE00750500000000000000": "spk-giro"}
    assert cfg.connection("dkb").product_id == "ABCDEF"
    with pytest.raises(KeyError):
        cfg.connection("nope")


def test_load_config_missing_file(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert cfg.fints == []


def test_label_for():
    explicit = FintsConnection(
        name="spk", blz="1", username="u", endpoint="e",
        accounts={"DE00SELECTED": "spk-giro"},
    )
    assert explicit.label_for("DE00SELECTED") == "spk-giro"
    assert explicit.label_for("DE00OTHER") is None  # not mapped → skip

    auto = FintsConnection(name="spk", blz="1", username="u", endpoint="e")
    assert auto.label_for("DE0075050000000000001234") == "spk-1234"
