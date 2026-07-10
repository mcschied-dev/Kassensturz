"""Wise connector via the official Wise API.

Auth: personal API token (system keychain, `kassensturz pin <name>`).
Balances need only the token. Balance *statements* (transactions) are
SCA-protected: Wise answers 403 with an `x-2fa-approval` one-time token,
which must be signed with an RSA private key whose public half is
uploaded to the Wise account — then the same request is retried with
the signature. Without a configured key, sync still records balances
and skips transactions with a hint.

REALITY CHECK (PSD2): Wise's docs state that statement retrieval via
personal tokens is only supported for accounts based in US/CA/AU/NZ/
SG/MY. For EEA/UK accounts the SCA signature is REJECTED regardless of
a correct key — that is policy, not a bug. EEA users: balances work,
transactions come via the web-UI CSV export (`import-csv`).

Setup (once):
  1. wise.com → Settings → API tokens → create token (read only)
     → `kassensturz pin wise`
  2. openssl genrsa -out ~/.kassensturz/wise-private.pem 2048
     openssl rsa -pubout -in ~/.kassensturz/wise-private.pem -out wise-public.pem
     → upload wise-public.pem at wise.com (API tokens → Manage public keys)
     → config: `private_key: ~/.kassensturz/wise-private.pem`
"""

from __future__ import annotations

import base64
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import requests

from ..config import WiseConnection
from ..model import Transaction
from .csv_import import parse_date

logger = logging.getLogger(__name__)

TIMEOUT = 30


class WiseError(Exception):
    pass


def _sign_ott(ott: str, private_key_path: str) -> str:
    """Sign the one-time token: SHA256 + PKCS#1 v1.5, base64-encoded."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    pem = Path(private_key_path).expanduser().read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    signature = key.sign(ott.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def _get(session: requests.Session, conn: WiseConnection, path: str, **params):
    """GET with transparent SCA handling for 403 + x-2fa-approval."""
    url = f"{conn.api_url}{path}"
    r = session.get(url, params=params or None, timeout=TIMEOUT)
    if r.status_code == 403 and "x-2fa-approval" in r.headers:
        ott = r.headers["x-2fa-approval"]
        if not conn.private_key:
            raise WiseError(
                "Endpoint verlangt SCA-Signatur, aber in der Config ist kein "
                "private_key hinterlegt (siehe Docstring in connectors/wise.py)."
            )
        r = session.get(
            url,
            params=params or None,
            timeout=TIMEOUT,
            headers={
                "x-2fa-approval": ott,
                "X-Signature": _sign_ott(ott, conn.private_key),
            },
        )
    if r.status_code != 200:
        raise WiseError(f"Wise API {path}: HTTP {r.status_code} — {r.text[:200]}")
    return r.json()


def _profile_candidates(session: requests.Session, conn: WiseConnection) -> list[int]:
    """Configured profile first; otherwise personal, then the rest.

    Accounts often live on the business profile while the personal one is
    empty — sync_wise walks the candidates until it finds balances.
    """
    if conn.profile_id:
        return [conn.profile_id]
    profiles = _get(session, conn, "/v2/profiles")
    if not profiles:
        raise WiseError("Kein Wise-Profil gefunden — Token gültig?")
    personal = [p["id"] for p in profiles if p.get("type", "").upper() == "PERSONAL"]
    rest = [p["id"] for p in profiles if p["id"] not in personal]
    return [*personal, *rest]


def statement_tx_to_transaction(
    data: dict, account_label: str, source: str
) -> Transaction:
    """Map one Wise statement transaction to the canonical model."""
    details = data.get("details") or {}
    merchant = (details.get("merchant") or {}).get("name") or ""
    counterparty = (
        merchant
        or details.get("senderName")
        or details.get("recipient", {}).get("name", "")
        or ""
    )
    amount = data.get("amount") or {}
    return Transaction(
        booking_date=parse_date(str(data["date"])[:10]),
        amount=Decimal(str(amount.get("value", 0))),
        currency=amount.get("currency") or "EUR",
        account=account_label,
        counterparty=counterparty,
        purpose=details.get("description")
        or details.get("paymentReference")
        or "",
        booking_type=data.get("type", ""),
        source=source,
    )


def sync_wise(
    conn: WiseConnection, token: str, start: date, end: date
) -> tuple[list[Transaction], list[str], list[tuple[str, int, str]]]:
    """Fetch balances (always) + statements (if private_key configured).

    Returns (transactions, skipped_notes, balances) like the other connectors.
    """
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    profile = None
    raw_balances: list = []
    for candidate in _profile_candidates(session, conn):
        raw_balances = _get(
            session, conn, f"/v4/profiles/{candidate}/balances", types="STANDARD"
        )
        if raw_balances:
            profile = candidate
            break
    if profile is None:
        raise WiseError(
            "Kein Wise-Profil mit Guthaben-Konten gefunden — ggf. profile_id "
            "in der Config setzen."
        )

    balances: list[tuple[str, int, str]] = []
    txs: list[Transaction] = []
    skipped: list[str] = []

    for b in raw_balances:
        currency = b.get("currency") or "EUR"
        label = conn.label_for(currency)
        value = Decimal(str((b.get("amount") or {}).get("value", 0)))
        balances.append((label, int((value * 100).to_integral_value()), currency))

        if not conn.private_key:
            skipped.append(f"{label} (nur Saldo — kein private_key für Umsätze)")
            continue
        try:
            statement = _get(
                session,
                conn,
                f"/v1/profiles/{profile}/balance-statements/{b['id']}/statement.json",
                currency=currency,
                intervalStart=f"{start.isoformat()}T00:00:00.000Z",
                intervalEnd=f"{end.isoformat()}T23:59:59.999Z",
                type="COMPACT",
            )
        except WiseError as e:
            # Statements dürfen scheitern (z. B. SCA-Key noch nicht aktiv),
            # ohne die Saldo-Snapshots mitzureißen.
            logger.warning("Wise statement %s: %s", label, e)
            skipped.append(f"{label} (Umsätze: {e})")
            continue
        for t in statement.get("transactions", []):
            txs.append(statement_tx_to_transaction(t, label, f"wise:{conn.name}"))

    return txs, skipped, balances
