"""FinTS/HBCI connector (Sparkasse, DKB, ...) built on python-fints.

Design notes:

- PINs come from the system keychain (`keyring`), never from config.
- Client state (BPD/UPD, TAN mechanism, dialog state) is persisted per
  connection under ~/.kassensturz/state/. This avoids re-triggering SCA
  on every sync — banks only demand a fresh TAN when they must.
- TAN entry is a callback so the connector stays UI-free. Decoupled TAN
  (pushTAN: confirm in the bank's app) is signalled by `response.decoupled`;
  the callback returns "" after the user confirmed.
- Fetching more than ~90 days of history usually triggers SCA (PSD2), so
  the default sync window stays below that.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

import keyring
from fints.client import FinTS3PinTanClient, NeedTANResponse

from ..config import FintsConnection
from ..model import Transaction

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "kassensturz-fints"
DEFAULT_STATE_DIR = Path.home() / ".kassensturz" / "state"


class ProductIdMissing(Exception):
    """FinTS requires a DK-registered product ID; none configured."""


class _SkipSyncPinTanClient(FinTS3PinTanClient):
    """PIN/TAN client that skips the HKSYN NEW_SYSTEM_ID sync.

    Some Sparkassen reject that sync under decoupled pushTAN (return code
    3905 "keine Challenge erzeugt" → 9010). Without it, python-fints
    obtains the BPD/TAN mechanisms via a plain dialog init, which these
    banks accept. Only used when the connection opts in.
    """

    def _ensure_system_id(self):  # noqa: D401 — deliberate no-op
        return

# Called with a NeedTANResponse; returns the TAN ("" for decoupled after
# the user confirmed in the banking app).
TanCallback = Callable[[NeedTANResponse], str]


# -- PIN handling (system keychain) -----------------------------------------


def get_pin(connection_name: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, connection_name)


def set_pin(connection_name: str, pin: str) -> None:
    keyring.set_password(KEYRING_SERVICE, connection_name, pin)


def delete_pin(connection_name: str) -> None:
    keyring.delete_password(KEYRING_SERVICE, connection_name)


# -- client state persistence ------------------------------------------------


def _state_file(state_dir: Path, name: str) -> Path:
    return state_dir / f"{name}.fints"


def _load_state(state_dir: Path, name: str) -> bytes | None:
    f = _state_file(state_dir, name)
    return f.read_bytes() if f.exists() else None


def _save_state(state_dir: Path, name: str, data: bytes) -> None:
    """Atomically write the (secret) client state with 0600 from the start."""
    import os

    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    target = _state_file(state_dir, name)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


# -- mapping -----------------------------------------------------------------


def mt940_to_transaction(data: dict, account_label: str, source: str) -> Transaction:
    """Map one mt940 transaction data dict to the canonical model."""
    amount = data["amount"]  # mt940.models.Amount
    purpose = data.get("purpose") or ""
    extra = data.get("additional_purpose") or ""
    if extra and extra not in purpose:
        purpose = f"{purpose} {extra}".strip()
    return Transaction(
        booking_date=data["date"],
        amount=Decimal(str(amount.amount)),
        currency=amount.currency or "EUR",
        account=account_label,
        counterparty=data.get("applicant_name") or "",
        purpose=purpose,
        value_date=data.get("entry_date") or data.get("guessed_entry_date"),
        booking_type=data.get("posting_text") or "",
        source=source,
    )


# -- sync --------------------------------------------------------------------


def _resolve_tan(client: FinTS3PinTanClient, result, tan_callback: TanCallback):
    """If `result` demands a TAN, run the callback loop until resolved."""
    while isinstance(result, NeedTANResponse):
        tan = tan_callback(result)
        result = client.send_tan(result, tan)
    return result


def _bootstrap(client: FinTS3PinTanClient, conn: FintsConnection) -> None:
    """Pick TAN mechanism + medium without the interactive CLI helper.

    python-fints' minimal_interactive_cli_bootstrap chokes on Sparkasse
    decoupled pushTAN: fetching the TAN-medium name (HKTAB) expects a
    challenge the bank never issues for decoupled ("3905 keine Challenge
    erzeugt" → 9010). Here we set the mechanism and, when required, the
    medium name — preferring an explicit config value (Deutsche Bank
    BestSign refuses the HKTAB query, so the name must come from config).
    """
    from fints.formals import DescriptionRequired

    client.fetch_tan_mechanisms()
    mechanisms = client.get_tan_mechanisms()
    if conn.tan_mechanism and conn.tan_mechanism in mechanisms:
        client.set_tan_mechanism(conn.tan_mechanism)
    elif not client.get_current_tan_mechanism() and mechanisms:
        client.set_tan_mechanism(next(iter(mechanisms)))

    mechanism = client.get_current_tan_mechanism()
    if getattr(mechanism, "description_required", None) != DescriptionRequired.MUST:
        return
    # Explicit config wins — the value python-fints stores is just the name.
    if conn.tan_medium:
        client.selected_tan_medium = conn.tan_medium
        return
    try:
        _usage, media = client.get_tan_media()
    except Exception as e:  # noqa: BLE001 — HKTAB rejection is non-fatal
        logger.warning("TAN-Medium konnte nicht geladen werden: %s", e)
        return
    names = [m for m in media if getattr(m, "tan_medium_name", None)]
    if names:
        client.set_tan_medium(names[0])


def _fetch_account(client, acc, label, conn_name, start, end, tan_callback):
    """Balance + transactions for one SEPA account. Raises on bank rejection."""
    balance_tuple = None
    balance = _resolve_tan(client, client.get_balance(acc), tan_callback)
    if balance is not None and getattr(balance, "amount", None) is not None:
        cents = int((balance.amount.amount * 100).to_integral_value())
        balance_tuple = (label, cents, balance.amount.currency or "EUR")
    result = _resolve_tan(
        client, client.get_transactions(acc, start, end), tan_callback
    )
    txs = [
        mt940_to_transaction(t.data, label, f"fints:{conn_name}") for t in result
    ]
    return balance_tuple, txs


def sync_connection(
    conn: FintsConnection,
    pin: str,
    start: date,
    end: date,
    tan_callback: TanCallback,
    state_dir: Path = DEFAULT_STATE_DIR,
) -> tuple[list[Transaction], list[str], list[tuple[str, int, str]]]:
    """Fetch transactions for all selected accounts of one connection.

    Returns (transactions, skipped_ibans, balances) — skipped = present at
    the bank but not mapped in config (only when an explicit mapping
    exists); balances = (label, cents, currency) snapshots via HKSAL.
    """
    if not conn.product_id:
        raise ProductIdMissing(
            f"Verbindung '{conn.name}': product_id fehlt. FinTS verlangt eine "
            "bei der Deutschen Kreditwirtschaft registrierte Produkt-ID "
            "(kostenlos): https://www.fints.org/de/hersteller/produktregistrierung — "
            "danach in der Config unter product_id eintragen."
        )
    from_data = _load_state(state_dir, conn.name)
    client_cls = (
        _SkipSyncPinTanClient
        if getattr(conn, "skip_system_id_sync", False)
        else FinTS3PinTanClient
    )
    client = client_cls(
        conn.blz,
        conn.username,
        pin,
        conn.endpoint,
        product_id=conn.product_id,
        from_data=from_data,
    )
    if from_data is None:
        # First contact: fetch BPD, pick TAN mechanism (decoupled-safe).
        _bootstrap(client, conn)

    txs: list[Transaction] = []
    skipped: list[str] = []
    balances: list[tuple[str, int, str]] = []
    with client:
        if client.init_tan_response:
            _resolve_tan(client, client.init_tan_response, tan_callback)

        # Persist IMMEDIATELY after login: the bank has just stored this
        # device as trusted — losing that state (e.g. because a later
        # account fetch fails) forces a fresh SCA on every run.
        _save_state(state_dir, conn.name, client.deconstruct(including_private=True))

        accounts = client.get_sepa_accounts()
        for acc in accounts:
            label = conn.label_for(acc.iban or "")
            if label is None:
                skipped.append(acc.iban or "?")
                continue
            # Per-account isolation: some accounts (Sparkonten, Altprodukte)
            # reject HKSAL/HKKAZ with 9370 "keine Auftragsberechtigung" —
            # that must not kill the whole sync.
            try:
                acc_balance, acc_txs = _fetch_account(
                    client, acc, label, conn.name, start, end, tan_callback
                )
            except Exception as e:  # noqa: BLE001 — bankseitige Ablehnung je Konto
                logger.warning("Konto %s übersprungen: %s", label, e)
                skipped.append(f"{label} (Bank lehnt Abruf ab: {type(e).__name__})")
                continue
            if acc_balance:
                balances.append(acc_balance)
            txs.extend(acc_txs)

    _save_state(state_dir, conn.name, client.deconstruct(including_private=True))
    return txs, skipped, balances
