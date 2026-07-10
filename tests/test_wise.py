"""Wise connector tests — mapping, config, SCA signing; no API contact."""

from datetime import date
from decimal import Decimal

from kassensturz.config import WiseConnection, load_config
from kassensturz.connectors.wise import _sign_ott, statement_tx_to_transaction

CONFIG_YAML = """
wise:
  - name: wise
    private_key: /tmp/nope.pem
"""


def test_statement_mapping_card_debit():
    data = {
        "type": "DEBIT",
        "date": "2026-06-15T09:31:02.000Z",
        "amount": {"value": -23.5, "currency": "EUR"},
        "details": {
            "type": "CARD",
            "description": "Card transaction of 23.50 EUR issued by Coffee Corner",
            "merchant": {"name": "Coffee Corner"},
        },
        "runningBalance": {"value": 476.5, "currency": "EUR"},
    }
    t = statement_tx_to_transaction(data, "wise-EUR", "wise:wise")
    assert t.booking_date == date(2026, 6, 15)
    assert t.amount == Decimal("-23.5")
    assert t.currency == "EUR"
    assert t.counterparty == "Coffee Corner"
    assert t.booking_type == "DEBIT"
    assert t.account == "wise-EUR"


def test_statement_mapping_incoming_transfer():
    data = {
        "type": "CREDIT",
        "date": "2026-06-01T00:00:00.000Z",
        "amount": {"value": 1000, "currency": "USD"},
        "details": {"type": "DEPOSIT", "senderName": "ACME Corp"},
    }
    t = statement_tx_to_transaction(data, "wise-USD", "wise:wise")
    assert t.amount == Decimal("1000")
    assert t.currency == "USD"
    assert t.counterparty == "ACME Corp"


def test_config_and_labels(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(CONFIG_YAML, encoding="utf-8")
    cfg = load_config(f)
    conn = cfg.connection("wise")
    assert isinstance(conn, WiseConnection)
    assert conn.label_for("eur") == "wise-EUR"
    assert conn in cfg.all_connections


def test_sign_ott_roundtrip(tmp_path):
    """Signature must verify against the public half of the key."""
    import base64

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "key.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ott = "one-time-token-123"
    signature = base64.b64decode(_sign_ott(ott, str(pem)))
    key.public_key().verify(  # raises on mismatch
        signature, ott.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
