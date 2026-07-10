"""Sparkasse decoupled-pushTAN quirk: HKSYN system-id sync must be skippable."""

from kassensturz.config import FintsConnection, load_config
from kassensturz.connectors.fints_client import (
    _SkipSyncPinTanClient,
)

CONFIG_YAML = """
fints:
  - name: spk
    blz: "61050000"
    username: "KSKSchiederGP"
    endpoint: https://banking-bw3.s-fints-pt-bw.de/fints30
    product_id: "ABCDEF"
    skip_system_id_sync: true
"""


def test_config_carries_skip_flag(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(CONFIG_YAML, encoding="utf-8")
    conn = load_config(f).connection("spk")
    assert isinstance(conn, FintsConnection)
    assert conn.skip_system_id_sync is True


def test_default_skip_flag_is_false():
    conn = FintsConnection(name="x", blz="1", username="u", endpoint="e")
    assert conn.skip_system_id_sync is False


def test_skip_client_ensure_system_id_is_noop():
    # _ensure_system_id must not raise / not send HKSYN; a bare object with
    # the method bound is enough to prove the override does nothing.
    class Fake(_SkipSyncPinTanClient):
        def __init__(self):  # bypass network-touching __init__
            pass

    assert Fake()._ensure_system_id() is None
