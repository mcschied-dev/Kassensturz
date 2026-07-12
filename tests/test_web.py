"""Weboberfläche: Login-Guard, Seiten, Filter, Export."""

from datetime import date
from decimal import Decimal

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kassensturz.model import Transaction  # noqa: E402
from kassensturz.store import Store  # noqa: E402
from kassensturz.web import auth  # noqa: E402
from kassensturz.web.app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "web_auth.json")
    auth.set_password("test123")
    db = tmp_path / "test.db"
    store = Store(db)
    store.add([Transaction(
        booking_date=date(2026, 7, 1), amount=Decimal("-42.00"),
        currency="EUR", account="test", counterparty="REWE Regensburg",
        purpose="Einkauf", category="Lebensmittel")])
    store.record_balances([("test", "2026-07-01", 100000, "EUR", "t")])
    monkeypatch.chdir(tmp_path)  # kein budget.yaml/csv aus dem Projekt ziehen
    return TestClient(create_app(db))


def test_guard_leitet_auf_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_und_seiten(client):
    assert client.post("/login", data={"password": "falsch"}).status_code == 401
    r = client.post("/login", data={"password": "test123"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/").status_code == 200
    b = client.get("/buchungen", params={"q": "REWE"})
    assert b.status_code == 200
    assert "REWE Regensburg" in b.text
    leer = client.get("/buchungen", params={"q": "gibtsnicht"})
    assert "Keine Buchungen" in leer.text
    assert client.get("/vertraege").status_code == 200
    assert client.get("/analyse").status_code == 200


def test_dashboard_gehaltslauf_projektion(client):
    client.post("/login", data={"password": "test123"})
    # ohne Einstellung und ohne Gehalts-Historie: keine Projektion
    assert "ohne Gehaltslauf" not in client.get("/").text
    # Betrag setzen → Kachel zeigt PROJIZIERT groß, Ist klein darunter
    r = client.post("/einstellungen/dashboard",
                    data={"gehalt_erwartet": "5000",
                          "gehalt_kategorie": "Gehalt"},
                    follow_redirects=True)
    assert "Gespeichert" in r.text
    d = client.get("/").text
    assert "4.958,00" in d                      # projiziert (−42 + 5.000)
    assert "ohne Gehaltslauf" in d and "ausstehend" in d and "5.000,00" in d


def test_login_backoff_nach_fehlversuchen(client):
    for _ in range(5):
        assert client.post(
            "/login", data={"password": "falsch"}).status_code == 401
    # ab dem 5. Fehler greift die IP-Sperre — auch das RICHTIGE Passwort
    # muss jetzt warten (sonst wäre der Backoff wirkungslos)
    assert client.post(
        "/login", data={"password": "test123"}).status_code == 429


def test_origin_check_blockt_fremde_posts(client):
    client.post("/login", data={"password": "test123"})
    # schreibender Request mit fremdem Origin → 403 (CSRF-Härtung);
    # eigener Origin und Requests ohne Origin-Header laufen normal
    r = client.post("/buchungen/kategorie",
                    data={"tx_hash": "x", "kategorie": "y"},
                    headers={"origin": "https://boese-seite.example"})
    assert r.status_code == 403
    r = client.post("/buchungen/kategorie",
                    data={"tx_hash": "gibtsnicht", "kategorie": "y"},
                    headers={"origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_provider_verwaltung(client, tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# Kopf-Kommentar bleibt erhalten\n"
        "dkb:\n"
        "  - name: dkb\n"
        '    username: "user_p"   # Login-Kommentar\n',
        encoding="utf-8")
    monkeypatch.setenv("KASSENSTURZ_CONFIG", str(cfg))
    client.post("/login", data={"password": "test123"})

    seite = client.get("/einstellungen/provider").text
    assert "dkb" in seite and "Neue Verbindung" in seite

    # anlegen (traderepublic) + Namens-Kollision abgewiesen
    r = client.post("/einstellungen/provider/anlegen",
                    data={"typ": "traderepublic",
                          "text": 'name: tr\nphone: "+49151000"'},
                    follow_redirects=True)
    assert "Gespeichert" in r.text and "name: tr" in r.text
    r = client.post("/einstellungen/provider/anlegen",
                    data={"typ": "dkb", "text": "name: tr\nusername: x"},
                    follow_redirects=True)
    assert "schon vergeben" in r.text

    # modify: Kommentare der Datei überleben den Round-Trip
    client.post("/einstellungen/provider/speichern",
                data={"typ": "dkb", "name": "dkb",
                      "text": 'name: dkb\nusername: "neu_p"'})
    inhalt = cfg.read_text(encoding="utf-8")
    assert "Kopf-Kommentar bleibt erhalten" in inhalt
    assert "neu_p" in inhalt
    assert cfg.with_suffix(".yaml.bak").exists()

    # Validierung: Pflichtfeld fehlt → Fehler, Datei unverändert
    r = client.post("/einstellungen/provider/speichern",
                    data={"typ": "traderepublic", "name": "tr",
                          "text": "name: tr"},  # phone fehlt
                    follow_redirects=True)
    assert "Validierung fehlgeschlagen" in r.text
    assert "+49151000" in cfg.read_text(encoding="utf-8")

    # delete
    client.post("/einstellungen/provider/loeschen",
                data={"typ": "traderepublic", "name": "tr"})
    assert "traderepublic" not in cfg.read_text(encoding="utf-8")


def test_spesen_seite(client):
    client.post("/login", data={"password": "test123"})
    r = client.get("/spesen")
    assert r.status_code == 200
    assert "Angefallen" in r.text and "Beglichen" in r.text
    assert 'name="monat"' in r.text and "PDF erzeugen" in r.text


def test_provider_csv_upload(client, tmp_path):
    client.post("/login", data={"password": "test123"})
    seite = client.get("/einstellungen/provider").text
    assert "CSV-Import" in seite and "csvPreset('paypal', 'paypal')" in seite

    csv_inhalt = (b"date,amount,counterparty,purpose\n"
                  b"2026-07-01,-12.34,Testshop,Upload-Kauf\n"
                  b"2026-07-02,-5.00,Testshop,Upload-Kauf 2\n")
    r = client.post(
        "/einstellungen/provider/upload",
        files={"datei": ("export.csv", csv_inhalt, "text/csv")},
        data={"profil": "generic", "konto": "upload-test"},
        follow_redirects=True)
    assert "2 neu, 0 Duplikate" in r.text
    # Datei liegt im Projekt-csv/, Buchung ist in der DB sichtbar
    assert list((tmp_path / "csv").glob("upload_*export.csv"))
    assert "Testshop" in client.get(
        "/buchungen", params={"konto": "upload-test"}).text
    # Re-Upload = reine Dubletten (Dedup), kein Doppelimport
    r = client.post(
        "/einstellungen/provider/upload",
        files={"datei": ("export.csv", csv_inhalt, "text/csv")},
        data={"profil": "generic", "konto": "upload-test"},
        follow_redirects=True)
    assert "0 neu, 2 Duplikate" in r.text
    # kaputte Eingaben: falsches Profil / falsche Endung
    r = client.post(
        "/einstellungen/provider/upload",
        files={"datei": ("export.csv", csv_inhalt, "text/csv")},
        data={"profil": "gibtsnicht", "konto": "x"}, follow_redirects=True)
    assert "unbekanntes Profil" in r.text
    r = client.post(
        "/einstellungen/provider/upload",
        files={"datei": ("export.xlsx", b"x", "application/octet-stream")},
        data={"profil": "generic", "konto": "x"}, follow_redirects=True)
    assert "nur .csv" in r.text


def test_kontenrundruf_endpoints(client, monkeypatch):
    from kassensturz.web import app as webapp

    client.post("/login", data={"password": "test123"})
    d = client.get("/").text
    assert 'id="sync-btn"' in d and "Kontenrundruf" in d

    # Worker mocken — der echte würde Banken abrufen
    def fake_worker(db_path, projekt):
        webapp.SYNC_STATE.update(running=False, rc=0,
                                 fertig="12.07.2026 16:00", log="$ ok")
    monkeypatch.setattr(webapp, "_sync_worker", fake_worker)
    monkeypatch.setitem(webapp.SYNC_STATE, "running", False)

    assert client.post("/sync/start").json()["running"] is True
    import time
    for _ in range(50):  # Thread beendet sich sofort (fake)
        s = client.get("/sync/status").json()
        if not s["running"]:
            break
        time.sleep(0.05)
    assert s["rc"] == 0 and s["log"] == "$ ok"
    # Doppelstart-Guard
    monkeypatch.setitem(webapp.SYNC_STATE, "running", True)
    assert client.post("/sync/start").json().get("hinweis") == "läuft bereits"
    monkeypatch.setitem(webapp.SYNC_STATE, "running", False)


def test_neutrale_default_ausgeblendet(client):
    client.post("/login", data={"password": "test123"})
    # Funding-Leg (neutral) in die Test-DB legen
    import re
    tx_hash = re.search(r'name="tx_hash" value="([^"]+)"',
                        client.get("/buchungen").text).group(1)
    client.post("/buchungen/kategorie",
                data={"tx_hash": tx_hash,
                      "kategorie": "PayPal-Funding (neutral)",
                      "zurueck": "/buchungen"})
    sel = "selected>PayPal-Funding (neutral)"
    assert sel not in client.get("/buchungen").text          # default: weg
    assert sel in client.get("/buchungen?neutrale=1").text   # Checkbox: da
    assert sel in client.get(                                # expliziter Filter: da
        "/buchungen", params={"kategorie": "PayPal-Funding (neutral)"}).text


def test_xlsx_export(client):
    client.post("/login", data={"password": "test123"})
    r = client.get("/export/xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats")


def test_kategorien_verwaltung(client, tmp_path):
    client.post("/login", data={"password": "test123"})
    # anlegen → taucht im Buchungs-Dropdown auf
    client.post("/einstellungen/kategorien/anlegen", data={"name": "Testkat"})
    seite = client.get("/einstellungen/kategorien")
    assert "Testkat" in seite.text
    assert client.get("/buchungen").text.count("Testkat") >= 1
    # Buchung umkategorisieren (hash aus der Seite ziehen)
    import re
    tx_hash = re.search(r'name="tx_hash" value="([^"]+)"',
                        client.get("/buchungen").text).group(1)
    client.post("/buchungen/kategorie",
                data={"tx_hash": tx_hash, "kategorie": "Testkat",
                      "zurueck": "/buchungen"})
    assert "Testkat" in client.get(
        "/buchungen", params={"kategorie": "Testkat"}).text
    # umbenennen + löschen
    client.post("/einstellungen/kategorien/umbenennen",
                data={"alt": "Testkat", "neu": "Testkat2"})
    client.post("/einstellungen/kategorien/loeschen",
                data={"name": "Testkat2"})
    s2 = client.get("/einstellungen/kategorien").text
    assert "Testkat2" not in s2.replace("gelöscht", "")
