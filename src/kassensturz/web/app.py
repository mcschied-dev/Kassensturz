"""kassensturz Web — FastAPI + Jinja + Tailwind (Catalyst-artiger Look).

Sicht auf die lokale SQLite plus XLSX-Export. Schreibend nur die
Kategorien-Pflege (Inline-Dropdown in den Buchungen, Einstellungen-Seite) —
Importe/Regeln/Checks bleiben in der CLI (gerichtsfeste Pipeline).
Start: `kassensturz web` oder Docker (siehe Dockerfile).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from ..model import fmt_eur
from ..store import Store
from . import auth

TEMPLATES = Path(__file__).parent / "templates"
PAGE_SIZE = 100

NEUTRAL = ("Umbuchung", "Kreditkarten-Abrechnung", "PayPal-Funding (neutral)")


# Kontenrundruf aus dem Dashboard: läuft als Hintergrund-Thread über die
# CLI (gleicher Code-Pfad wie das Terminal — Keychain, Trust-State,
# DKB-App-Freigabe). Ein Lauf zur Zeit; Status wird vom Dashboard gepollt.
SYNC_STATE: dict = {"running": False, "rc": None, "fertig": None, "log": ""}
_SYNC_LOCK = threading.Lock()
_SYNC_LOG_MAX = 6_000


def _sync_worker(db_path: Path, projekt: Path) -> None:
    ks = Path(sys.executable).parent / "kassensturz"
    schritte = [["sync"]]
    if (projekt / "rules.yaml").exists():
        schritte.append(["categorize", "--rules", "rules.yaml"])
    schritte.append(["check"])
    rc = 0
    try:
        for args in schritte:
            SYNC_STATE["log"] += f"\n$ kassensturz {' '.join(args)}\n"
            p = subprocess.run(
                [str(ks), *args, "--db", str(db_path)],
                cwd=projekt, capture_output=True, text=True, timeout=900)
            SYNC_STATE["log"] = (SYNC_STATE["log"] + p.stdout
                                 + (p.stderr if p.returncode else "")
                                 )[-_SYNC_LOG_MAX:]
            rc = p.returncode  # der letzte Schritt ist `check` → Invarianten
    except Exception as e:  # Timeout, fehlendes Binary, …
        SYNC_STATE["log"] = (SYNC_STATE["log"] + f"\nFEHLER: {e}")[-_SYNC_LOG_MAX:]
        rc = 1
    fertig = datetime.now().strftime("%d.%m.%Y %H:%M")
    SYNC_STATE.update(running=False, rc=rc, fertig=fertig)
    try:  # letzter Lauf übersteht Neustarts (Dashboard-Anzeige)
        Store(db_path).setting_set("letzter_rundruf", f"{fertig}|{rc}")
    except Exception:
        pass


def _gehalt_einstellungen(s, ym: str) -> tuple[str, int]:
    """(Gehalts-Kategorie, erwarteter Gehaltslauf in Cents) fürs Dashboard.

    Betrag aus den Einstellungen; ohne gesetzten Wert der Median der
    positiven Monatssummen der Kategorie aus den letzten 3 vollen Monaten
    (datengetrieben statt hartkodiert — OSS-tauglich).
    """
    kat = s.setting_get("gehalt_kategorie", "Gehalt")
    roh = s.setting_get("gehalt_erwartet", "").replace(",", ".").strip()
    try:
        return kat, int(round(float(roh) * 100))
    except ValueError:
        pass
    vals = sorted(r["v"] for r in s.conn.execute(
        """SELECT SUM(amount_cents) v FROM transactions
           WHERE category = ? AND amount_cents > 0
             AND substr(booking_date,1,7) < ?
           GROUP BY substr(booking_date,1,7)
           ORDER BY substr(booking_date,1,7) DESC LIMIT 3""",
        (kat, ym)).fetchall())
    return kat, (vals[len(vals) // 2] if vals else 0)


def _alle_kategorien(s) -> list[str]:
    """Kategorien aus Buchungen UND der Registry (manuell angelegte)."""
    return [r[0] for r in s.conn.execute(
        "SELECT DISTINCT COALESCE(category,'(unkategorisiert)') AS n "
        "FROM transactions UNION SELECT name FROM categories ORDER BY 1")]


def create_app(db_path: Path) -> FastAPI:
    # EINE geteilte Verbindung für die ganze Web-App statt einer pro
    # Request (Review 12.07.: Stores wurden nie geschlossen → Deskriptor-/
    # Lock-Leck). Alle Handler sind async und laufen im Event-Loop-Thread,
    # SQLite-Zugriffe sind damit serialisiert. ACHTUNG: deshalb hier NIE
    # sync-Routen (def statt async def) einführen — die liefen im
    # Threadpool. Der Sync-Worker-Thread nutzt bewusst einen eigenen Store.
    web_store = Store(db_path, check_same_thread=False)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        web_store.close()

    app = FastAPI(title="kassensturz", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    # Tailwind + ECharts sind gevendort (kein CDN): local-first heißt
    # offline-fähig und keine Skripte von fremden Hosts (SRI-Problem entfällt)
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(
        directory=str(Path(__file__).parent / "static")), name="static")
    # Projektordner (rules.yaml) = Startverzeichnis von `kassensturz web`
    projekt = Path.cwd()
    templates.env.filters["eur"] = lambda cents: fmt_eur(int(cents))
    signer = URLSafeSerializer(auth.secret_key(), salt="session")

    def store() -> Store:
        return web_store

    def logged_in(request: Request) -> bool:
        cookie = request.cookies.get("session")
        if not cookie:
            return False
        try:
            return signer.loads(cookie).get("ok") is True
        except BadSignature:
            return False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # CSRF-Härtung: schreibende Requests müssen — wenn der Browser
        # einen Origin-Header mitschickt — vom eigenen Host kommen.
        # (Zweite Verteidigungslinie neben SameSite=strict am Cookie.)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if origin and urlparse(origin).netloc != host:
                return PlainTextResponse("Origin-Prüfung fehlgeschlagen",
                                         status_code=403)
        if (request.url.path not in ("/login",)
                and not request.url.path.startswith("/static/")
                and not logged_in(request)):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # ---------- Auth ----------
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return templates.TemplateResponse(
            request, "login.html", {"fehler": None})

    # Brute-Force-Bremse: PBKDF2 (300k Iterationen) macht jeden Versuch
    # teuer, zusätzlich exponentieller Backoff je Client-IP ab dem 5. Fehler
    login_fails: dict[str, tuple[int, float]] = {}
    LOGIN_FREIVERSUCHE = 4

    @app.post("/login")
    async def login(request: Request, password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        fails, gesperrt_bis = login_fails.get(ip, (0, 0.0))
        if time.time() < gesperrt_bis:
            return PlainTextResponse(
                "Zu viele Fehlversuche — bitte kurz warten.", status_code=429)
        if auth.verify(password):
            login_fails.pop(ip, None)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("session", signer.dumps({"ok": True}),
                            httponly=True, samesite="strict",
                            max_age=86400 * 14)
            return resp
        fails += 1
        sperre = (min(30 * 2 ** (fails - LOGIN_FREIVERSUCHE), 900)
                  if fails > LOGIN_FREIVERSUCHE else 0)
        login_fails[ip] = (fails, time.time() + sperre)
        return templates.TemplateResponse(
            request, "login.html", {"fehler": "Passwort stimmt nicht."},
            status_code=401)

    @app.post("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("session")
        return resp

    # ---------- Seiten ----------
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        s = store()
        balances = [dict(b) for b in s.latest_balances()]
        vermoegen = sum(b["balance_cents"] for b in balances
                        if b["currency"] == "EUR")
        ym = f"{date.today():%Y-%m}"
        neutral = ",".join(f"'{c}'" for c in NEUTRAL)
        monat = s.conn.execute(
            f"""SELECT COALESCE(SUM(CASE WHEN amount_cents>0 THEN amount_cents END),0) ein,
                       COALESCE(SUM(CASE WHEN amount_cents<0 THEN amount_cents END),0) aus
                FROM transactions WHERE substr(booking_date,1,7)=?
                AND COALESCE(category,'') NOT IN ({neutral})
                AND COALESCE(category,'') NOT LIKE '%(Sondereffekt)%'""",
            (ym,)).fetchone()
        letzte = s.conn.execute(
            "SELECT booking_date, counterparty, category, amount_cents "
            "FROM transactions ORDER BY booking_date DESC, rowid DESC LIMIT 8"
        ).fetchall()
        # Gehaltslauf-Projektion: das Gehalt bucht erst zum Monatsende —
        # ohne sie sähe der laufende Monat fast immer negativ aus. Betrag/
        # Kategorie kommen aus Einstellungen → Dashboard; ohne gesetzten
        # Betrag gilt der Median der letzten Monate der Gehalts-Kategorie.
        gehalt_kat, erwartet = _gehalt_einstellungen(s, ym)
        gebucht = s.conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) v FROM transactions "
            "WHERE category = ? AND amount_cents > 0 "
            "AND substr(booking_date,1,7) = ?", (gehalt_kat, ym)).fetchone()["v"]
        ausstehend = erwartet if gebucht == 0 else 0
        saldo = monat["ein"] + monat["aus"]
        # Zuflussdatum fürs Gehalt: gebucht → echtes Datum; ausstehend →
        # typischer Buchungstag (Median der letzten Gehaltsläufe)
        raw = s.setting_get("letzter_rundruf", "")
        letzter_rundruf, letzter_rc = (raw.split("|") + [""])[:2] if raw else ("", "")
        # Hero-Daten: 12 Monats-Saldi als Balken (Pixel serverseitig skaliert),
        # Entsparen-Schnitt der letzten 6 vollen Monate, Verträge-Jahreskosten
        saldi12 = [dict(r) for r in s.conn.execute(
            f"""SELECT substr(booking_date,1,7) ym, SUM(amount_cents) v
                FROM transactions
                WHERE COALESCE(category,'') NOT IN ({neutral})
                  AND COALESCE(category,'') NOT LIKE '%(Sondereffekt)%'
                GROUP BY ym ORDER BY ym DESC LIMIT 12""")][::-1]
        maxv = max((abs(r["v"]) for r in saldi12), default=1) or 1
        balken = [{"ym": r["ym"], "px": max(1, round(abs(r["v"]) * 88 / maxv)),
                   "pos": r["v"] >= 0, "offen": r["ym"] == ym}
                  for r in saldi12]
        volle = [r["v"] for r in saldi12 if r["ym"] != ym][-6:]
        schnitt6 = round(sum(volle) / len(volle)) if volle else 0
        from ..report.build import collect_vertraege
        vertraege = collect_vertraege(s)
        gehalt_datum, gehalt_tag = None, None
        if gebucht:
            gehalt_datum = s.conn.execute(
                "SELECT MAX(booking_date) d FROM transactions WHERE category=? "
                "AND amount_cents > 0 AND substr(booking_date,1,7) = ?",
                (gehalt_kat, ym)).fetchone()["d"]
        elif ausstehend:
            tage = sorted(int(r["d"][8:10]) for r in s.conn.execute(
                "SELECT booking_date d FROM transactions WHERE category=? "
                "AND amount_cents > 0 AND substr(booking_date,1,7) < ? "
                "ORDER BY booking_date DESC LIMIT 3", (gehalt_kat, ym)))
            gehalt_tag = tage[len(tage) // 2] if tage else None
        return templates.TemplateResponse(request, "dashboard.html", {
            "aktiv": "dashboard", "vermoegen": vermoegen,
            "balances": balances, "ym": ym,
            "einnahmen": monat["ein"], "ausgaben": monat["aus"],
            "saldo": saldo, "letzte": letzte,
            "ausstehend": ausstehend, "projiziert": saldo + ausstehend,
            "gehalt_gebucht": gebucht > 0, "gehalt_summe": gebucht,
            "gehalt_datum": gehalt_datum, "gehalt_tag": gehalt_tag,
            "letzter_rundruf": letzter_rundruf, "letzter_rc": letzter_rc,
            "balken": balken, "schnitt6": schnitt6,
            "kosten_jahr": vertraege["kosten_jahr"],
            "sparen_jahr": vertraege["sparen_jahr"],
            "n_tx": s.conn.execute(
                "SELECT COUNT(*) FROM transactions").fetchone()[0],
        })

    @app.post("/sync/start")
    async def sync_start():
        with _SYNC_LOCK:
            if SYNC_STATE["running"]:
                return {"running": True, "hinweis": "läuft bereits"}
            SYNC_STATE.update(running=True, rc=None, log="")
        threading.Thread(target=_sync_worker, args=(db_path, projekt),
                         daemon=True).start()
        return {"running": True}

    @app.get("/sync/status")
    async def sync_status():
        return {k: SYNC_STATE[k] for k in ("running", "rc", "fertig", "log")}

    @app.get("/buchungen", response_class=HTMLResponse)
    async def buchungen(request: Request, q: str = "", konto: str = "",
                        kategorie: str = "", von: str = "", bis: str = "",
                        neutrale: int = 0, seite: int = 1):
        s = store()
        wo, params = ["1=1"], []
        if q:
            wo.append("(counterparty LIKE ? OR purpose LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if konto:
            wo.append("account = ?")
            params.append(konto)
        if kategorie:
            if kategorie == "(unkategorisiert)":
                wo.append("category IS NULL")
            else:
                wo.append("category = ?")
                params.append(kategorie)
        # Neutrale/technische Zeilen (Funding-Legs, Settlements, Umbuchungen)
        # sind Belege für die Saldenabstimmung, keine aufgelösten Positionen —
        # default ausgeblendet. Wer explizit nach einer neutralen Kategorie
        # filtert, will sie natürlich sehen.
        if not neutrale and kategorie not in NEUTRAL:
            wo.append("COALESCE(category,'') NOT IN ({})".format(
                ",".join("?" * len(NEUTRAL))))
            params += list(NEUTRAL)
        if von:
            wo.append("booking_date >= ?")
            params.append(von)
        if bis:
            wo.append("booking_date <= ?")
            params.append(bis)
        where = " AND ".join(wo)
        agg = s.conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) s "
            f"FROM transactions WHERE {where}", params).fetchone()
        rows = s.conn.execute(
            f"SELECT hash, booking_date, account, counterparty, purpose, "
            f"category, amount_cents FROM transactions WHERE {where} "
            "ORDER BY booking_date DESC, rowid DESC LIMIT ? OFFSET ?",
            [*params, PAGE_SIZE, (seite - 1) * PAGE_SIZE]).fetchall()
        konten = [r[0] for r in s.conn.execute(
            "SELECT DISTINCT account FROM transactions ORDER BY 1")]
        kategorien = _alle_kategorien(s)
        from ..report.xlsx import lade_gruppen
        gmap = lade_gruppen(s)
        kat_gruppen: dict[str, list[str]] = {}
        for k in kategorien:
            if k != "(unkategorisiert)":
                kat_gruppen.setdefault(gmap.get(k, "Sonstiges"), []).append(k)
        return templates.TemplateResponse(request, "buchungen.html", {
            "aktiv": "buchungen", "rows": rows, "n": agg["n"],
            "summe": agg["s"], "seite": seite, "seiten":
            max(1, -(-agg["n"] // PAGE_SIZE)),
            "q": q, "konto": konto, "kategorie": kategorie,
            "von": von, "bis": bis, "neutrale": neutrale, "konten": konten,
            "kategorien": kategorien,
            "kat_gruppen": dict(sorted(kat_gruppen.items())),
        })

    @app.get("/analyse", response_class=HTMLResponse)
    async def analyse(request: Request, entnahme: float = 300_000,
                      kosten: float = 25_000, wirksam: str = "2027-01",
                      zufluss: list[float] = Query(default=[100_000]),
                      zufluss_ab: list[str] = Query(default=["2027-01"])):
        from collections import defaultdict
        s = store()
        # Szenario „wegfallende laufende Zahlung": Kategorie kommt aus den
        # Einstellungen (Tab Dashboard) — kein Personenbezug im Code
        wegfall_kategorie = s.setting_get("wegfall_kategorie", "")
        neutral = ",".join(f"'{c}'" for c in NEUTRAL)
        rows = s.conn.execute(
            f"""SELECT substr(booking_date,1,7) ym,
                       COALESCE(category,'(unkategorisiert)') c,
                       SUM(amount_cents) v FROM transactions
                WHERE booking_date >= '2022-01-01'
                AND COALESCE(category,'') NOT IN ({neutral})
                AND COALESCE(category,'') NOT LIKE '%(Sondereffekt)%'
                GROUP BY ym, c""").fetchall()
        per: dict[str, dict[str, int]] = defaultdict(dict)
        for r in rows:
            per[r["ym"]][r["c"]] = r["v"]
        monate = sorted(per)
        # Kategorie-Netto-Semantik wie die Reports: Retouren mindern ihre
        # Kategorie, statt als Einnahme zu zählen
        ein = [round(sum(v for v in per[m].values() if v > 0) / 100, 2)
               for m in monate]
        aus = [round(-sum(v for v in per[m].values() if v < 0) / 100, 2)
               for m in monate]
        saldo = [round(e - a, 2) for e, a in zip(ein, aus)]

        # Zuschaltbare Serien für die Checkboxen: Sondereffekte (Exit,
        # Abfindung — oben bewusst ausgefiltert) und die Historie der im
        # Szenario wegfallenden Zahlung (steckt in `aus` mit drin)
        sonder = s.conn.execute(
            """SELECT substr(booking_date,1,7) ym,
                      SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) p,
                      SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END) n
               FROM transactions WHERE category LIKE '%(Sondereffekt)%'
               GROUP BY ym""").fetchall()
        s_pos = {r["ym"]: r["p"] for r in sonder}
        s_neg = {r["ym"]: r["n"] for r in sonder}
        sonder_ein = [round(s_pos.get(m, 0) / 100, 2) for m in monate]
        sonder_aus = [round(s_neg.get(m, 0) / 100, 2) for m in monate]
        wegfall_hist = [
            round(-min(0, per[m].get(wegfall_kategorie, 0)) / 100, 2)
            for m in monate]

        # Top-6-Ausgabenkategorien der letzten 24 Monate, Rest gebündelt
        tot: dict[str, int] = defaultdict(int)
        for m in monate[-24:]:
            for c, v in per[m].items():
                if v < 0:
                    tot[c] += -v
        top = [c for c, _ in sorted(tot.items(), key=lambda kv: -kv[1])[:6]]
        kat_serien = [
            {"name": c, "werte": [
                round(-min(0, per[m].get(c, 0)) / 100, 2) for m in monate]}
            for c in top]
        kat_serien.append({"name": "Übrige", "werte": [
            round(sum(-v for c, v in per[m].items()
                      if v < 0 and c not in top) / 100, 2) for m in monate]})

        # Sparen/Entsparen = bereinigter Saldo (Spesen-Verauslagung und
        # -Erstattung beidseitig geglättet, ab 2026); kumuliert seit 2022
        from ..check import _spesen_candidates, _spesen_reimbursements
        adj: dict[str, int] = defaultdict(int)
        for e in _spesen_candidates(s):
            adj[e["booking_date"][:7]] += e["amount_cents"]
        for r_ in _spesen_reimbursements(s):
            adj[r_["booking_date"][:7]] += r_["amount_cents"]
        bereinigt = [round(sa - adj.get(m, 0) / 100, 2)
                     for m, sa in zip(monate, saldo)]
        kum, kumuliert = 0.0, []
        for b in bereinigt:
            kum = round(kum + b, 2)
            kumuliert.append(kum)
        # --- Szenario „Kapitalentnahme" ---
        # Forecast bis Ende übernächsten Jahres; Szenario: im Wirksam-Monat
        # fließen Entnahme + Nebenkosten ab, danach entfällt die laufende
        # Zahlung und die Zinsen sinken anteilig (~1 % p. a. Tagesgeld).
        from ..forecast import _ym_shift, build_forecast
        heute = date.today()
        last_full = _ym_shift(f"{heute:%Y-%m}", -1)
        fc = build_forecast(s, last_full, f"{heute.year + 2}-12")
        verm0 = sum(b["balance_cents"] for b in s.latest_balances()
                    if b["currency"] == "EUR") / 100
        zins_delta = (entnahme + kosten) * 0.01 / 12
        wegfall_fc = [fc["werte"].get((m, wegfall_kategorie), 0) / 100
                      for m in fc["monate"]]
        basis_pfad, szen_pfad = [], []
        v_basis = v_szen = verm0
        for i, m in enumerate(fc["monate"]):
            ms = fc["saldi"][m] / 100
            v_basis += ms
            v_szen += ms
            if m == wirksam:
                v_szen -= entnahme + kosten
            if m >= wirksam:
                v_szen += -wegfall_fc[i] - zins_delta  # laufende Zahlung entfällt
            basis_pfad.append(round(v_basis, 2))
            szen_pfad.append(round(v_szen, 2))

        # Spiegel-Szenario „Kapitalzuflüsse": MEHRERE Sondereffekt-Eingänge
        # (Exit-Tranche, Erstattung, Verkauf …), je Betrag + Stichtag; das UI
        # ergänzt Zeilen per „+ weitere". Ab jedem Stichtag wächst der
        # Zinsgewinn ~1 % p. a. (Tagesgeld-Logik wie beim Entnahme-Szenario).
        ereignisse = [(m_, b) for m_, b in zip(zufluss_ab, zufluss) if b > 0]
        zufluss_pfad, v_zu, zins_zu = [], verm0, 0.0
        for m in fc["monate"]:
            v_zu += fc["saldi"][m] / 100
            for em, eb in ereignisse:
                if em == m:
                    v_zu += eb
                    zins_zu += eb * 0.01 / 12
            v_zu += zins_zu
            zufluss_pfad.append(round(v_zu, 2))
        zufluss_summe = sum(b for _, b in ereignisse)
        zufluss_zins = round(zufluss_summe * 0.01 / 12, 2)

        return templates.TemplateResponse(request, "analyse.html", {
            "aktiv": "analyse", "monate": monate, "ein": ein, "aus": aus,
            "saldo": saldo, "kat_serien": kat_serien,
            "bereinigt": bereinigt, "kumuliert": kumuliert,
            "sonder_ein": sonder_ein, "sonder_aus": sonder_aus,
            "wegfall_hist": wegfall_hist, "zins_delta": round(zins_delta, 2),
            "fc_monate": fc["monate"], "basis_pfad": basis_pfad,
            "szen_pfad": szen_pfad, "entnahme": entnahme,
            "kosten": kosten, "wirksam": wirksam,
            "zufluss_pfad": zufluss_pfad, "ereignisse": ereignisse,
            "zufluss_summe": zufluss_summe, "zufluss_zins": zufluss_zins,
            "verm0": verm0,
        })

    @app.get("/vertraege", response_class=HTMLResponse)
    async def vertraege(request: Request):
        from ..report.xlsx import _abo_gruppe, _detect_abos, lade_gruppen
        s_ = store()
        abos = _detect_abos(s_, date.today())
        mapping = lade_gruppen(s_)
        gruppen: dict[str, list] = {}
        for a in abos:
            gruppen.setdefault(_abo_gruppe(a["kategorie"], mapping), []).append(a)
        reihenfolge = sorted(
            gruppen.items(),
            key=lambda kv: -sum(x["jahr"] for x in kv[1] if x["aktiv"]))
        from ..report.xlsx import GRUPPE_SPAREN
        sparen = sum(a["jahr"] for g, es in reihenfolge if g == GRUPPE_SPAREN
                     for a in es if a["aktiv"])
        aktiv_jahr = sum(a["jahr"] for a in abos if a["aktiv"])
        return templates.TemplateResponse(request, "vertraege.html", {
            "aktiv": "vertraege", "gruppen": reihenfolge,
            "kosten_jahr": aktiv_jahr - sparen, "sparen_jahr": sparen,
        })

    @app.get("/spesen", response_class=HTMLResponse)
    async def spesen_seite(request: Request, monat: str = ""):
        from ..report.build import collect_spesen_monat, spesen_monate
        s = store()
        monate = spesen_monate(s)
        if monat not in monate:
            monat = monate[-1] if monate else f"{date.today():%Y-%m}"
        return templates.TemplateResponse(request, "spesen.html", {
            "aktiv": "spesen", "monate": monate,
            **collect_spesen_monat(s, monat),
        })

    @app.post("/spesen/akzeptieren")
    async def spesen_akzeptieren(tx_hash: str = Form(...),
                                 monat: str = Form("")):
        """Toggle: Beleg abhaken (nicht eingereicht / Differenz akzeptiert)
        bzw. wieder öffnen — zählt dann nicht mehr als offen."""
        s = store()
        with s.conn:
            cur = s.conn.execute(
                "DELETE FROM spesen_akzeptiert WHERE tx_hash = ?", (tx_hash,))
            if cur.rowcount == 0:
                s.conn.execute(
                    "INSERT INTO spesen_akzeptiert (tx_hash) VALUES (?)",
                    (tx_hash,))
        return RedirectResponse("/spesen?monat=" + quote(monat),
                                status_code=303)

    @app.get("/spesen/pdf")
    async def spesen_pdf(monat: str = ""):
        from ..report.build import (
            build_pdf,
            collect_spesen_monat,
            render_spesen,
            spesen_monate,
        )
        s = store()
        monate = spesen_monate(s)
        if monat not in monate:
            monat = monate[-1] if monate else f"{date.today():%Y-%m}"
        try:
            pdf = build_pdf(
                render_spesen(collect_spesen_monat(s, monat)),
                Path(tempfile.mkdtemp(prefix="kassensturz-")) / "spesen.pdf")
        except RuntimeError as e:
            return PlainTextResponse(f"PDF-Erzeugung fehlgeschlagen: {e}",
                                     status_code=503)
        return FileResponse(pdf, media_type="application/pdf",
                            filename=f"spesen-{monat}.pdf")

    @app.get("/vertraege/pdf")
    async def vertraege_pdf(inaktive: int = 0):
        from ..report.build import build_pdf, collect_vertraege, render_vertraege
        s = store()
        data = collect_vertraege(s, mit_inaktiven=bool(inaktive))
        try:
            pdf = build_pdf(
                render_vertraege(data),
                Path(tempfile.mkdtemp(prefix="kassensturz-")) / "vertraege.pdf")
        except RuntimeError as e:
            # kein TeX (z. B. Docker-Image) oder Kompilierfehler —
            # klare Meldung statt 500er-Traceback
            return PlainTextResponse(f"PDF-Erzeugung fehlgeschlagen: {e}",
                                     status_code=503)
        return FileResponse(
            pdf, media_type="application/pdf",
            filename=f"vertraege-{date.today()}.pdf")

    @app.post("/buchungen/kategorie")
    async def kategorie_setzen(tx_hash: str = Form(...),
                               kategorie: str = Form(""),
                               zurueck: str = Form("/buchungen")):
        s = store()
        s.conn.execute(
            "UPDATE transactions SET category = ? WHERE hash = ?",
            (kategorie or None, tx_hash))
        s.conn.commit()
        if not zurueck.startswith("/buchungen"):
            zurueck = "/buchungen"
        return RedirectResponse(zurueck, status_code=303)

    @app.get("/einstellungen/kategorien", response_class=HTMLResponse)
    async def kategorien_seite(request: Request, meldung: str = ""):
        s = store()
        zeilen = s.conn.execute(
            """SELECT k.name,
                      COALESCE(c.gruppe, 'Sonstiges') gruppe,
                      (SELECT COUNT(*) FROM transactions WHERE category=k.name) n,
                      (SELECT COALESCE(SUM(amount_cents),0) FROM transactions
                       WHERE category=k.name) summe
               FROM (SELECT DISTINCT category AS name FROM transactions
                     WHERE category IS NOT NULL
                     UNION SELECT name FROM categories) k
               LEFT JOIN categories c ON c.name = k.name
               ORDER BY gruppe COLLATE NOCASE, k.name COLLATE NOCASE""").fetchall()
        gruppen_namen = sorted({z["gruppe"] for z in zeilen} | {"Sonstiges"})
        unkat = s.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE category IS NULL"
        ).fetchone()[0]
        konflikte = []
        if Path("rules.yaml").exists():
            from ..categorize import load_rules
            from ..suggest import regel_konflikte
            konflikte = regel_konflikte(s, load_rules(Path("rules.yaml")))[:8]
        return templates.TemplateResponse(request, "kategorien.html", {
            "aktiv": "einstellungen", "zeilen": zeilen, "unkat": unkat,
            "meldung": meldung, "gruppen_namen": gruppen_namen,
            "konflikte": konflikte})

    @app.get("/einstellungen/dashboard", response_class=HTMLResponse)
    async def dashboard_einstellungen(request: Request, meldung: str = ""):
        s = store()
        ym = f"{date.today():%Y-%m}"
        gehalt_kat, erwartet = _gehalt_einstellungen(s, ym)
        return templates.TemplateResponse(
            request, "einstellungen_dashboard.html", {
                "aktiv": "einstellungen", "meldung": meldung,
                "gehalt_kategorie": gehalt_kat,
                "gehalt_erwartet": s.setting_get("gehalt_erwartet", ""),
                "wegfall_kategorie": s.setting_get("wegfall_kategorie", ""),
                "effektiv": erwartet,
                "kategorien": [k for k in _alle_kategorien(s)
                               if k != "(unkategorisiert)"],
            })

    @app.get("/einstellungen/provider", response_class=HTMLResponse)
    async def provider_seite(request: Request, meldung: str = "",
                             fehler: str = ""):
        from ..connectors.csv_import import PROFILES
        from . import providercfg
        return templates.TemplateResponse(
            request, "einstellungen_provider.html", {
                "aktiv": "einstellungen", "meldung": meldung,
                "fehler": fehler, "verbindungen": providercfg.liste(),
                "typen": providercfg.TYPEN, "vorlagen": providercfg.VORLAGEN,
                "pfad": str(providercfg.config_pfad()),
                "csv_profile": sorted(PROFILES),
            })

    def _provider_redirect(fehler: str | None):
        ziel = ("/einstellungen/provider?fehler=" + quote(fehler)
                if fehler else "/einstellungen/provider?meldung=Gespeichert.")
        return RedirectResponse(ziel, status_code=303)

    @app.post("/einstellungen/provider/speichern")
    async def provider_speichern(typ: str = Form(...), name: str = Form(...),
                                 text: str = Form(...)):
        from . import providercfg
        return _provider_redirect(providercfg.speichern(typ, name, text))

    @app.post("/einstellungen/provider/loeschen")
    async def provider_loeschen(typ: str = Form(...), name: str = Form(...)):
        from . import providercfg
        return _provider_redirect(providercfg.loeschen(typ, name))

    @app.post("/einstellungen/provider/anlegen")
    async def provider_anlegen(typ: str = Form(...), text: str = Form(...)):
        from . import providercfg
        return _provider_redirect(providercfg.anlegen(typ, text))

    @app.post("/einstellungen/provider/upload")
    async def provider_upload(datei: UploadFile = File(...),
                              profil: str = Form(...),
                              konto: str = Form(...)):
        """CSV-Detail-Quellen (PayPal, Miles&More …): Datei → csv/ →
        Import → Kategorisierung → check, wie der CLI-Workflow."""
        from ..connectors.csv_import import PROFILES, import_csv
        if profil not in PROFILES:
            return _provider_redirect(f"unbekanntes Profil '{profil}'")
        if not konto.strip():
            return _provider_redirect("Konto-Label fehlt")
        name = Path(datei.filename or "upload.csv").name
        if not name.lower().endswith(".csv"):
            return _provider_redirect(f"'{name}': nur .csv-Dateien")
        ziel = projekt / "csv" / f"upload_{date.today()}_{name}"
        ziel.parent.mkdir(exist_ok=True)
        ziel.write_bytes(await datei.read())
        s = store()
        try:
            neu, dubletten = s.add(import_csv(ziel, profil, konto.strip()))
        except Exception as e:
            return _provider_redirect(f"Import fehlgeschlagen ({name}): {e}")
        from ..categorize import (
            apply_rules,
            load_rules,
            match_transfers,
            neutralize_funding_legs,
        )
        n_kat = 0
        if (projekt / "rules.yaml").exists():
            ruleset = load_rules(projekt / "rules.yaml")
            n_kat += match_transfers(s, ruleset)
            n_kat += neutralize_funding_legs(s)
            n_kat += sum(apply_rules(s, ruleset).values())
        else:
            n_kat += neutralize_funding_legs(s)
        from ..check import run_all
        warnungen = [f for f in run_all(s) if f.level == "warn"]
        meldung = (f"{name}: {neu} neu, {dubletten} Duplikate, "
                   f"{n_kat} kategorisiert · "
                   + ("check: Alle Invarianten halten ✓" if not warnungen
                      else f"⚠ {len(warnungen)} check-Warnung(en), z. B.: "
                           f"{warnungen[0].message[:140]}"))
        return RedirectResponse(
            "/einstellungen/provider?meldung=" + quote(meldung),
            status_code=303)

    @app.post("/einstellungen/dashboard")
    async def dashboard_einstellungen_speichern(
            gehalt_erwartet: str = Form(""),
            gehalt_kategorie: str = Form("Gehalt"),
            wegfall_kategorie: str = Form("")):
        s = store()
        wert = gehalt_erwartet.replace(",", ".").strip()
        try:
            float(wert)
        except ValueError:
            wert = ""  # leer = automatisch (Median der letzten Monate)
        s.setting_set("gehalt_erwartet", wert)
        s.setting_set("gehalt_kategorie", gehalt_kategorie or "Gehalt")
        s.setting_set("wegfall_kategorie", wegfall_kategorie)
        return RedirectResponse(
            "/einstellungen/dashboard?meldung=Gespeichert.", status_code=303)

    @app.post("/einstellungen/kategorien/gruppe")
    async def kategorie_gruppe(name: str = Form(...), gruppe: str = Form(...)):
        s = store()
        s.conn.execute(
            "INSERT INTO categories (name, gruppe) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET gruppe = excluded.gruppe",
            (name, gruppe.strip() or None))
        s.conn.commit()
        return RedirectResponse("/einstellungen/kategorien", status_code=303)

    @app.post("/einstellungen/kategorien/anlegen")
    async def kategorie_anlegen(name: str = Form(...)):
        s = store()
        name = name.strip()
        if name:
            s.conn.execute(
                "INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
            s.conn.commit()
        return RedirectResponse(
            f"/einstellungen/kategorien?meldung=„{name}“ angelegt",
            status_code=303)

    @app.post("/einstellungen/kategorien/umbenennen")
    async def kategorie_umbenennen(alt: str = Form(...), neu: str = Form(...)):
        s = store()
        neu = neu.strip()
        if neu and neu != alt:
            n = s.conn.execute(
                "UPDATE transactions SET category = ? WHERE category = ?",
                (neu, alt)).rowcount
            s.conn.execute("DELETE FROM categories WHERE name = ?", (alt,))
            s.conn.execute(
                "INSERT OR IGNORE INTO categories (name) VALUES (?)", (neu,))
            s.conn.commit()
            meldung = f"„{alt}“ → „{neu}“ ({n} Buchungen)"
        else:
            meldung = "nichts geändert"
        return RedirectResponse(
            f"/einstellungen/kategorien?meldung={meldung}", status_code=303)

    @app.post("/einstellungen/kategorien/loeschen")
    async def kategorie_loeschen(name: str = Form(...)):
        s = store()
        n = s.conn.execute(
            "UPDATE transactions SET category = NULL WHERE category = ?",
            (name,)).rowcount
        s.conn.execute("DELETE FROM categories WHERE name = ?", (name,))
        s.conn.commit()
        return RedirectResponse(
            f"/einstellungen/kategorien?meldung=„{name}“ gelöscht — "
            f"{n} Buchungen jetzt unkategorisiert", status_code=303)

    @app.get("/export/xlsx")
    async def export_xlsx():
        from ..report.xlsx import build_workbook
        out = Path(tempfile.mkdtemp()) / "kassensturz.xlsx"
        build_workbook(store(), out)
        return FileResponse(
            out, filename=f"kassensturz-{date.today()}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument"
                       ".spreadsheetml.sheet")

    return app


def run(db_path: Path, host: str, port: int) -> None:
    import uvicorn
    uvicorn.run(create_app(db_path), host=host, port=port)
