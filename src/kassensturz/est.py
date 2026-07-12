"""ESt-Projektion aus den Gehaltsabrechnungen (DATEV Brutto/Netto-PDFs).

Liest die Lohndokumente (csv/Lohndokumente_*/Brutto-Netto-Abrechnung
YYYY MM *.pdf), nimmt die Jahreswerte der NEUESTEN Abrechnung + deren
Monatswerte zur Hochrechnung auf 12 Monate und stellt die erwartete
Einkommensteuer der bereits einbehaltenen Lohnsteuer plus den
ESt-Vorauszahlungen (Kategorie Steuer, Freistaat/Finanzkasse) gegenüber.

NÄHERUNG, KEINE STEUERBERATUNG: Tarif 2026 nach den (vorläufigen)
§32a-Eckwerten, Werbungskosten nur Pauschbetrag, Kapitalerträge gelten
als abgeltungsbesteuert, Kinderfreibeträge/Realsplitting nur als Hinweis.
Verbindlich rechnet die neo-Kanzlei (Benny/Hofmann).

DATEV-Zahlformat im Textlayer: Punkte sind Tausendertrenner, die letzten
zwei Ziffern sind Cents ohne Komma ("4.91691" = 4.916,91).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .store import Store

# §32a EStG, Eckwerte VZ 2026 (vorläufig lt. Steuerfortentwicklungsgesetz)
GRUNDFREIBETRAG = 12_348
ZONE1_ENDE = 17_799
ZONE2_ENDE = 68_480
ZONE3_ENDE = 277_825
WK_PAUSCHBETRAG = 1_230
SA_PAUSCHBETRAG = 36
SOLI_SATZ = 0.055


def tarif_2026(zve: float) -> float:
    """Grundtarif (Einzelveranlagung), gerundete Näherung."""
    if zve <= GRUNDFREIBETRAG:
        return 0.0
    if zve <= ZONE1_ENDE:
        y = (zve - GRUNDFREIBETRAG) / 10_000
        return (932.30 * y + 1_400) * y
    if zve <= ZONE2_ENDE:
        z = (zve - ZONE1_ENDE) / 10_000
        return (176.64 * z + 2_397) * z + 1_015.13
    if zve <= ZONE3_ENDE:
        return 0.42 * zve - 10_911.92
    return 0.45 * zve - 19_246.67


def _num(s: str) -> float:
    """DATEV-Steuerblock: '4.91691' → 4916.91, '78585' → 785.85.
    Netto-Block nutzt normales Format: '5.177,95' → 5177.95."""
    s = s.strip()
    if "," in s:
        return float(s.replace(".", "").replace(",", "."))
    return int(s.replace(".", "")) / 100


def _text(pdf: Path) -> str:
    return subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True, text=True, check=True,
    ).stdout


def parse_lohnordner(folder: Path) -> dict:
    """Neueste Abrechnung: Jahres- (Verdienstbescheinigung) + Monatswerte;
    AG-Zuschüsse zur privaten KV/PV über alle vorhandenen Monate summiert."""
    pdfs = sorted(folder.glob("Brutto-Netto-Abrechnung*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"keine Abrechnungen in {folder}")
    ag_zuschuss_ytd = 0.0
    for p in pdfs:
        t = _text(p)
        for m in re.finditer(
                r"AG-Ant\. priv\.\s?(?:Kranken|Pflege)vers\.\s+([\d.,]+)", t):
            ag_zuschuss_ytd += _num(m.group(1))
    t = _text(pdfs[-1])
    monat = int(re.search(r"Abrechnung .*?für \w+ \d{4}.*", t) and
                re.search(r"(\d{4}) (\d{2}) ", pdfs[-1].name).group(2))

    def _ytd(label: str) -> float:
        m = re.search(rf"{label}\s+([\d.]+)", t)
        return _num(m.group(1)) if m else 0.0

    # Monatszeile Steuer folgt auf den Blockheader "…Steuerrechtliche Abzüge"
    st = re.search(
        r"Steuerrechtliche Abzüge\s*\nL\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", t)
    return {
        "monate_ytd": monat,
        "brutto_ytd": _ytd("Gesamt-Brutto"),
        "lst_ytd": _ytd("Lohnsteuer"),
        "soli_ytd": _ytd(r"Solidaritätszuschlag"),
        "rv_ytd": _ytd("RV-Beitrag"),
        "av_ytd": _ytd("AV-Beitrag"),
        "brutto_monat": _num(st.group(1)) if st else 0.0,
        "lst_monat": _num(st.group(2)) if st else 0.0,
        "soli_monat": _num(st.group(3)) if st else 0.0,  # KiSt leer (konfessionslos)
        "ag_zuschuss_ytd": ag_zuschuss_ytd,
        "quelle": pdfs[-1].name,
    }


def vorauszahlungen(store: Store, jahr: str) -> tuple[float, int]:
    """(gezahlte ESt-VZ des Jahres, Anzahl Zahlungen) — Freistaat/Finanzkasse."""
    r = store.conn.execute(
        "SELECT COALESCE(SUM(-amount_cents),0) s, COUNT(*) n FROM transactions "
        "WHERE category='Steuer' AND amount_cents<0 "
        "AND substr(booking_date,1,4)=? "
        "AND (lower(counterparty) LIKE '%freistaat%' "
        "     OR lower(counterparty) LIKE '%finanzkasse%' "
        "     OR lower(counterparty) LIKE '%finanzamt%' OR counterparty LIKE 'FK %')",
        (jahr,),
    ).fetchone()
    return r["s"] / 100, r["n"]


def projektion(store: Store, folder: Path, jahr: str,
               basis_kv_pv_jahr: float) -> dict:
    lohn = parse_lohnordner(folder)
    n = lohn["monate_ytd"]
    rest = 12 - n
    brutto_jahr = lohn["brutto_ytd"] + rest * lohn["brutto_monat"]
    lst_jahr = lohn["lst_ytd"] + rest * lohn["lst_monat"]
    soli_lst_jahr = lohn["soli_ytd"] + rest * lohn["soli_monat"]
    rv_jahr = lohn["rv_ytd"] / n * 12
    ag_zuschuss_jahr = lohn["ag_zuschuss_ytd"] / n * 12

    wk = WK_PAUSCHBETRAG
    vorsorge_rv = rv_jahr  # AN-Anteil voll abziehbar (AG-Anteil steuerfrei)
    vorsorge_kv = max(0.0, basis_kv_pv_jahr - ag_zuschuss_jahr)
    zve = brutto_jahr - wk - vorsorge_rv - vorsorge_kv - SA_PAUSCHBETRAG

    est = round(tarif_2026(zve))
    soli = round(est * SOLI_SATZ, 2)  # bei dieser Höhe voll über Freigrenze
    schuld = est + soli

    vz_gezahlt, vz_n = vorauszahlungen(store, jahr)
    vz_quartal = vz_gezahlt / vz_n if vz_n else 0.0
    vz_offen = max(0, 4 - vz_n) * vz_quartal
    geleistet = lst_jahr + soli_lst_jahr + vz_gezahlt + vz_offen

    return {
        "lohn": lohn, "jahr": jahr,
        "brutto_jahr": brutto_jahr, "wk": wk,
        "vorsorge_rv": vorsorge_rv, "vorsorge_kv": vorsorge_kv,
        "basis_kv_pv": basis_kv_pv_jahr, "ag_zuschuss": ag_zuschuss_jahr,
        "sa_pausch": SA_PAUSCHBETRAG, "zve": zve,
        "est": est, "soli": soli, "schuld": schuld,
        "lst_jahr": lst_jahr, "soli_lst_jahr": soli_lst_jahr,
        "vz_gezahlt": vz_gezahlt, "vz_offen": vz_offen, "vz_n": vz_n,
        "geleistet": geleistet,
        "saldo": geleistet - schuld,  # >0 = Erstattung erwartet
    }
