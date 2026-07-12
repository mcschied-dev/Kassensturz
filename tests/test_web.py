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
