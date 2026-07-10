"""Trade Republic connector tests — aggregation + config; no API contact."""

from kassensturz.config import TradeRepublicConnection, load_config
from kassensturz.connectors.traderepublic import summarize_portfolio

CONFIG_YAML = """
traderepublic:
  - name: tr
    phone: "+4915112345678"
"""


def test_summarize_cash_and_positions():
    cash = [{"currencyId": "EUR", "amount": 12.34}]
    positions = [
        {"instrumentId": "IE00B4L5Y983", "netValue": 1500.00, "currency": "EUR"},
        {"instrumentId": "US0000000000", "netValue": 250.50},  # currency defaults EUR
    ]
    totals = summarize_portfolio(cash, positions)
    assert totals == {"EUR": 176284}  # (12.34 + 1500 + 250.50) * 100


def test_summarize_skips_unvalued_positions():
    # a position whose ticker didn't resolve (no netValue) must not crash
    positions = [{"instrumentId": "X", "netValue": None}, {"instrumentId": "Y"}]
    assert summarize_portfolio([], positions) == {}


def test_summarize_multi_currency():
    cash = [{"currencyId": "EUR", "amount": 100}, {"currencyId": "USD", "amount": 50}]
    totals = summarize_portfolio(cash, [])
    assert totals == {"EUR": 10000, "USD": 5000}


def test_config_and_label(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(CONFIG_YAML, encoding="utf-8")
    cfg = load_config(f)
    conn = cfg.connection("tr")
    assert isinstance(conn, TradeRepublicConnection)
    assert conn.phone == "+4915112345678"
    assert conn.label == "tr-depot"
    assert conn in cfg.all_connections
