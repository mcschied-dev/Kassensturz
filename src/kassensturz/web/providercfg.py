"""Bankverbindungen (config.yaml) aus der Web-UI pflegen.

Die Datei enthält dokumentierte Entscheidungen als Kommentare (z. B. warum
M&M-FinTS deaktiviert ist) — deshalb ruamel.yaml im Round-Trip-Modus statt
yaml.dump, und vor jedem Schreiben ein .bak. Jede Änderung wird erst gegen
load_config() validiert (Temp-Datei), bevor sie die echte Config ersetzt.
Secrets gehören NICHT hierher — PINs/Token bleiben im Schlüsselbund.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from pathlib import Path

from ruamel.yaml import YAML

from ..config import load_config

TYPEN = ("fints", "dkb", "wise", "traderepublic")

# Startpunkte fürs Anlegen — Pflichtfelder je Typ vorgezeichnet
VORLAGEN = {
    "fints": (
        'name: neu            # kurzer Handle, auch Schlüsselbund-Key\n'
        'blz: "12345678"\n'
        'username: "..."\n'
        'endpoint: https://banking-xyz.example/fints30\n'
        'product_id: "..."    # DK-registrierte FinTS-Produkt-ID\n'
        '# accounts:\n'
        '#   DE00123456780000000000: mein-giro   # IBAN → Label\n'
    ),
    "dkb": 'name: neu\nusername: "..."   # DKB-Webbanking-Login\n',
    "wise": 'name: neu\n# profile_id: 12345678   # sonst Auto-Discovery\n',
    "traderepublic": 'name: neu\nphone: "+49151..."\n',
}


def config_pfad() -> Path:
    return Path(os.environ.get(
        "KASSENSTURZ_CONFIG", Path.home() / ".kassensturz" / "config.yaml"))


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _lesen():
    pfad = config_pfad()
    if not pfad.exists():
        return {}
    return _yaml().load(pfad.read_text(encoding="utf-8")) or {}


def _dump(obj) -> str:
    buf = io.StringIO()
    _yaml().dump(obj, buf)
    return buf.getvalue()


def _schreiben(daten) -> str | None:
    """Validieren → .bak → atomar ersetzen. Rückgabe: Fehlertext oder None."""
    pfad = config_pfad()
    with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        _yaml().dump(daten, tmp)
        tmp_pfad = Path(tmp.name)
    try:
        load_config(tmp_pfad)  # wirft bei fehlenden Pflichtfeldern
    except Exception as e:
        tmp_pfad.unlink(missing_ok=True)
        return f"Validierung fehlgeschlagen: {e}"
    if pfad.exists():
        shutil.copy2(pfad, pfad.with_suffix(".yaml.bak"))
    pfad.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(tmp_pfad, pfad)
    return None


def liste() -> list[dict]:
    """Alle Verbindungen als {typ, name, text} — text = YAML des Eintrags."""
    daten = _lesen()
    out = []
    for typ in TYPEN:
        for eintrag in daten.get(typ) or []:
            out.append({
                "typ": typ,
                "name": str(eintrag.get("name", "?")),
                "text": _dump(eintrag),
            })
    return out


def _parse_eintrag(text: str):
    eintrag = _yaml().load(text)
    if not isinstance(eintrag, dict) or not eintrag.get("name"):
        raise ValueError("Eintrag muss ein YAML-Mapping mit `name:` sein")
    return eintrag


def speichern(typ: str, name: str, text: str) -> str | None:
    if typ not in TYPEN:
        return f"unbekannter Typ '{typ}'"
    try:
        neu = _parse_eintrag(text)
    except Exception as e:
        return f"YAML-Fehler: {e}"
    daten = _lesen()
    eintraege = daten.get(typ) or []
    for i, e in enumerate(eintraege):
        if str(e.get("name")) == name:
            eintraege[i] = neu
            return _schreiben(daten)
    return f"Verbindung '{name}' ({typ}) nicht gefunden"


def loeschen(typ: str, name: str) -> str | None:
    daten = _lesen()
    eintraege = daten.get(typ) or []
    for i, e in enumerate(eintraege):
        if str(e.get("name")) == name:
            del eintraege[i]
            if not eintraege:  # leere Liste raus, Kommentare der Sektion bleiben
                del daten[typ]
            return _schreiben(daten)
    return f"Verbindung '{name}' ({typ}) nicht gefunden"


def anlegen(typ: str, text: str) -> str | None:
    if typ not in TYPEN:
        return f"unbekannter Typ '{typ}'"
    try:
        neu = _parse_eintrag(text)
    except Exception as e:
        return f"YAML-Fehler: {e}"
    daten = _lesen()
    vergeben = {str(e.get("name"))
                for t in TYPEN for e in (daten.get(t) or [])}
    if str(neu["name"]) in vergeben:
        return (f"Name '{neu['name']}' ist schon vergeben — er ist zugleich "
                f"der Schlüsselbund-Key und muss eindeutig sein")
    daten.setdefault(typ, []).append(neu)
    return _schreiben(daten)
