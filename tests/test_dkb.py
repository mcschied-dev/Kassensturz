"""DKB connector tests — mapping and config only; no bank contact."""

from datetime import date
from decimal import Decimal

from kassensturz.config import DkbConnection, load_config
from kassensturz.connectors.dkb import dkb_tx_to_transaction, extract_balances

CONFIG_YAML = """
dkb:
  - name: dkb
    username: marc_dkb
    accounts:
      DE00120300000000000000: dkb-giro
"""


def test_checking_account_mapping():
    data = {
        "bdate": "2026-06-15",
        "vdate": "2026-06-16",
        "amount": -42.5,
        "currencycode": "EUR",
        "peer": "REWE Markt GmbH",
        "reasonforpayment": "REWE SAGT DANKE",
        "postingtext": "Kartenzahlung",
    }
    t = dkb_tx_to_transaction(data, "account", "dkb-giro", "dkb:dkb")
    assert t.booking_date == date(2026, 6, 15)
    assert t.value_date == date(2026, 6, 16)
    assert t.amount == Decimal("-42.5")
    assert t.counterparty == "REWE Markt GmbH"
    assert t.purpose == "REWE SAGT DANKE"
    assert t.booking_type == "Kartenzahlung"
    assert t.source == "dkb:dkb"


def test_checking_account_german_dates():
    """dkb-robo has emitted dd.mm.yyyy historically — both must parse."""
    data = {"bdate": "15.06.2026", "amount": 10, "currencycode": "EUR"}
    t = dkb_tx_to_transaction(data, "account", "x", "s")
    assert t.booking_date == date(2026, 6, 15)
    assert t.value_date is None


def test_creditcard_mapping():
    data = {
        "bdate": "2026-06-10",
        "amount": -12.9,
        "currencycode": "EUR",
        "text": "NETFLIX.COM",
    }
    t = dkb_tx_to_transaction(data, "creditcard", "dkb-visa", "dkb:dkb")
    assert t.purpose == "NETFLIX.COM"
    assert t.counterparty == ""
    assert t.amount == Decimal("-12.9")


def test_config_dkb_section(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(CONFIG_YAML, encoding="utf-8")
    cfg = load_config(f)
    assert len(cfg.dkb) == 1
    conn = cfg.connection("dkb")
    assert isinstance(conn, DkbConnection)
    assert conn.username == "marc_dkb"
    assert conn.label_for("DE00120300000000000000") == "dkb-giro"
    assert conn.label_for("4748________1234") is None  # unmapped card → skip
    assert cfg.all_connections == [*cfg.fints, *cfg.dkb]


def test_dkb_auto_labels():
    conn = DkbConnection(name="dkb", username="u")
    assert conn.label_for("DE00120300000000004711") == "dkb-4711"
    assert conn.label_for("4748________1234") == "dkb-1234"


def test_extract_balances_covers_all_account_types():
    """Depot & unmapped accounts must still report balances."""
    conn = DkbConnection(
        name="dkb", username="u",
        accounts={"DE00120300000000003527": "dkb-giro"},
    )
    account_dic = {
        0: {"iban": "DE00120300000000003527", "type": "account",
            "amount": 3375.98, "currencycode": "EUR"},
        1: {"iban": "DE00120300000000006735", "type": "account",  # Tagesgeld, unmapped
            "amount": 540000.00, "currencycode": "EUR"},
        2: {"id": "9876", "type": "depot", "amount": "12345.67",
            "currencycode": "EUR"},
        3: {"iban": "X", "type": "account", "amount": None},  # no balance → skip
    }
    balances = extract_balances(account_dic, conn)
    assert ("dkb-giro", 337598, "EUR") in balances
    assert ("dkb-6735", 54000000, "EUR") in balances  # auto label despite mapping
    assert ("dkb-9876", 1234567, "EUR") in balances   # depot included
    assert len(balances) == 3
