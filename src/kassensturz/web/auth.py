"""Single-User-Auth für die Weboberfläche.

Passwort-Hash (PBKDF2) liegt in ~/.kassensturz/web_auth.json — gesetzt per
`kassensturz web-password` oder (Docker) per Env KASSENSTURZ_WEB_PASSWORD.
Sessions sind signierte Cookies (itsdangerous), kein Server-State.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

AUTH_FILE = Path(
    os.environ.get("KASSENSTURZ_HOME", Path.home() / ".kassensturz")
) / "web_auth.json"
ITERATIONS = 300_000


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), ITERATIONS
    ).hex()


def set_password(password: str) -> None:
    salt = secrets.token_hex(16)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps({
        "salt": salt,
        "hash": _hash(password, salt),
        "secret": secrets.token_hex(32),  # signiert die Session-Cookies
    }))
    AUTH_FILE.chmod(0o600)


def _load() -> dict | None:
    # Env-Passwort (Docker) gewinnt und erzeugt die Datei bei Bedarf
    env_pw = os.environ.get("KASSENSTURZ_WEB_PASSWORD")
    if env_pw and not AUTH_FILE.exists():
        set_password(env_pw)
    if not AUTH_FILE.exists():
        return None
    return json.loads(AUTH_FILE.read_text())


def verify(password: str) -> bool:
    data = _load()
    if not data:
        return False
    return secrets.compare_digest(_hash(password, data["salt"]), data["hash"])


def secret_key() -> str:
    data = _load()
    if not data:
        raise RuntimeError(
            "Kein Web-Passwort gesetzt — `kassensturz web-password` ausführen "
            "oder KASSENSTURZ_WEB_PASSWORD setzen.")
    return data["secret"]
