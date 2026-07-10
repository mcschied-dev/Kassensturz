"""Configuration for bank connections.

Lives at ~/.kassensturz/config.yaml (override: KASSENSTURZ_CONFIG or
--config). Contains NO secrets — PINs go into the system keychain via
`kassensturz pin`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(
    os.environ.get("KASSENSTURZ_CONFIG", Path.home() / ".kassensturz" / "config.yaml")
)


def _label_for(conn_name: str, accounts: dict[str, str], number: str) -> str | None:
    """Label for an IBAN/card number; None = account not selected for sync.

    Empty mapping = sync everything, auto-labeled "<name>-<last 4 digits>".
    """
    if not accounts:
        return f"{conn_name}-{number[-4:]}"
    return accounts.get(number)


@dataclass
class FintsConnection:
    name: str  # short handle, e.g. "spk" — also the keychain key
    blz: str
    username: str
    endpoint: str  # the institute's FinTS 3.0 URL
    product_id: str | None = None  # DK-registered FinTS product ID
    accounts: dict[str, str] = field(default_factory=dict)  # IBAN → label
    # Sparkassen quirk: some reject the HKSYN NEW_SYSTEM_ID sync under
    # decoupled pushTAN (3905 "keine Challenge" → 9010). Skipping it lets
    # python-fints fetch the BPD via a plain dialog init instead.
    skip_system_id_sync: bool = False

    def label_for(self, iban: str) -> str | None:
        return _label_for(self.name, self.accounts, iban)


@dataclass
class DkbConnection:
    """DKB via its web/REST API (dkb-robo) — no FinTS product ID needed."""

    name: str  # short handle, e.g. "dkb" — also the keychain key
    username: str  # DKB web banking login
    accounts: dict[str, str] = field(default_factory=dict)  # IBAN/card → label

    def label_for(self, number: str) -> str | None:
        return _label_for(self.name, self.accounts, number)


@dataclass
class WiseConnection:
    """Wise via its official API — personal token (keychain) + optional
    RSA private key for SCA-protected statement endpoints."""

    name: str  # short handle, e.g. "wise" — also the keychain key
    private_key: str | None = None  # path to RSA private key PEM (for statements)
    profile_id: int | None = None  # auto-discovered (personal profile) if unset
    api_url: str = "https://api.wise.com"

    def label_for(self, currency: str) -> str:
        return f"{self.name}-{currency.upper()}"


@dataclass
class TradeRepublicConnection:
    """Trade Republic via pytr — phone number in config, PIN in keychain.
    Scope: portfolio + cash balance snapshot (transactions already show up
    on the funding account, e.g. the DKB savings-plan debits)."""

    name: str  # short handle, e.g. "tr" — also the keychain key
    phone: str  # +49… login number

    @property
    def label(self) -> str:
        return f"{self.name}-depot"


@dataclass
class Config:
    fints: list[FintsConnection] = field(default_factory=list)
    dkb: list[DkbConnection] = field(default_factory=list)
    wise: list[WiseConnection] = field(default_factory=list)
    traderepublic: list[TradeRepublicConnection] = field(default_factory=list)

    @property
    def all_connections(self):
        return [*self.fints, *self.dkb, *self.wise, *self.traderepublic]

    def connection(self, name: str) -> FintsConnection | DkbConnection:
        for c in self.all_connections:
            if c.name == name:
                return c
        raise KeyError(
            f"connection '{name}' not in config (available: "
            f"{', '.join(c.name for c in self.all_connections) or 'none'})"
        )


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.exists():
        return Config()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fints = [
        FintsConnection(
            name=entry["name"],
            blz=str(entry["blz"]),
            username=str(entry["username"]),
            endpoint=entry["endpoint"],
            product_id=entry.get("product_id"),
            accounts=dict(entry.get("accounts") or {}),
            skip_system_id_sync=bool(entry.get("skip_system_id_sync", False)),
        )
        for entry in (data.get("fints") or [])
    ]
    dkb = [
        DkbConnection(
            name=entry["name"],
            username=str(entry["username"]),
            accounts=dict(entry.get("accounts") or {}),
        )
        for entry in (data.get("dkb") or [])
    ]
    wise = [
        WiseConnection(
            name=entry["name"],
            private_key=entry.get("private_key"),
            profile_id=entry.get("profile_id"),
            api_url=entry.get("api_url", "https://api.wise.com"),
        )
        for entry in (data.get("wise") or [])
    ]
    traderepublic = [
        TradeRepublicConnection(name=entry["name"], phone=str(entry["phone"]))
        for entry in (data.get("traderepublic") or [])
    ]
    return Config(fints=fints, dkb=dkb, wise=wise, traderepublic=traderepublic)
