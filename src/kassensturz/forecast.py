"""Monats-Forecast bis Ende nächsten Jahres — aus den eigenen Mustern.

Zwei Prognose-Regeln je Kategorie (Konsum-Sicht, konsistent mit Budget/
"Saldo bereinigt": Emproof-Spesen beidseitig raus, Neutrales und
Sondereffekte raus, Sparpläne separat):

- STETIG (Miete, Gehalt, Unterhalt, Lebensmittel …): Median der letzten
  6 vollen Monate wird fortgeschrieben.
- KLUMPIG (Versicherung, Steuer, Kfz — unruhige Monatsreihen): Jahres-
  Saisonalität — Prognose(Monat) = Wert desselben Kalendermonats ein Jahr
  zuvor (Ist oder, für späte 2027-Monate, bereits prognostiziert). So
  landen HUK-Dezember/Januar, Kfz-Steuer, ESt-Vorauszahlungen (10.03./
  10.06./10.09./10.12.) automatisch im richtigen Monat.

Annahmen sind Annahmen: Unterhalt Birgit läuft unverändert weiter,
Mercedes-Leasing unbefristet, keine Sondereffekte (Kiteworks 2027 fehlt
bewusst), keine Beitrags-/Mieterhöhungen. Anpassungen → budget.yaml/Chat.
"""

from __future__ import annotations

from statistics import median, pstdev

from .check import SPESEN_EXCLUDE, SPESEN_PATTERNS
from .store import Store

SKIP = ("Umbuchung", "Kreditkarten-Abrechnung", "PayPal-Funding (neutral)",
        "Spesen", "Sparplan Romy")

STETIG_FENSTER = 6


def _ym_shift(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _history(store: Store) -> dict[tuple[str, str], int]:
    """(Monat, Kategorie) → Netto-Cents, spesenbereinigt (beide Seiten)."""
    like = " OR ".join(
        "lower(counterparty) LIKE ? OR lower(purpose) LIKE ?"
        for _ in SPESEN_PATTERNS)
    excl = " AND ".join(
        "lower(counterparty) NOT LIKE ?" for _ in SPESEN_EXCLUDE)
    skip = ",".join(f"'{c}'" for c in SKIP)
    params = [f"%{p}%" for p in SPESEN_PATTERNS for _ in range(2)] \
        + [f"%{p}%" for p in SPESEN_EXCLUDE]
    return {
        (r["ym"], r["c"]): r["s"]
        for r in store.conn.execute(
            "SELECT substr(booking_date,1,7) ym,"
            "       COALESCE(category,'(unkategorisiert)') c,"
            "       SUM(amount_cents) s FROM transactions "
            f"WHERE COALESCE(category,'') NOT IN ({skip}) "
            "AND COALESCE(category,'') NOT LIKE '%(Sondereffekt)%' "
            f"AND NOT (amount_cents < 0 AND ({like}) AND {excl}) "
            "GROUP BY ym, c",
            params,
        )
    }


def build_forecast(store: Store, last_full_month: str, until: str) -> dict:
    """Prognose je Kategorie/Monat von last_full_month+1 bis `until`."""
    hist = _history(store)
    cats = sorted({c for _, c in hist})
    fenster12 = [_ym_shift(last_full_month, -i) for i in range(11, -1, -1)]
    fenster6 = fenster12[-STETIG_FENSTER:]

    regeln: dict[str, str] = {}
    basis: dict[str, int] = {}
    for c in cats:
        reihe12 = [hist.get((ym, c), 0) for ym in fenster12]
        if not any(reihe12):
            continue  # in den letzten 12 Monaten tot → nicht fortschreiben
        reihe6 = [hist.get((ym, c), 0) for ym in fenster6]
        med6 = median(reihe6)
        mean6 = sum(reihe6) / len(reihe6)
        cv6 = pstdev(reihe6) / abs(mean6) if mean6 else 99
        spitze12 = max(abs(v) for v in reihe12)
        # STETIG hat Vorrang (Regimewechsel! 2025 war jobfrei — ein
        # 12-Monats-CV würde das Gehalt fälschlich "saisonal" machen):
        # dicht + ruhig in den letzten 6 Monaten UND kein Jahres-Spike
        # außerhalb des Fensters (Versicherungs-Dezember, Steuer-Quartale).
        dicht = sum(1 for v in reihe6 if v) >= STETIG_FENSTER - 1
        if dicht and cv6 <= 0.5 and med6 and spitze12 <= 2 * abs(med6):
            regeln[c] = "stetig (6-Monats-Median)"
            basis[c] = int(med6)
        else:
            regeln[c] = "saisonal (Vorjahresmonat)"

    werte: dict[tuple[str, str], int] = {}  # Prognosewerte
    monate = []
    ym = _ym_shift(last_full_month, 1)
    while ym <= until:
        monate.append(ym)
        for c in regeln:
            if regeln[c].startswith("stetig"):
                werte[(ym, c)] = basis[c]
            else:
                ref = _ym_shift(ym, -12)
                werte[(ym, c)] = werte.get((ref, c), hist.get((ref, c), 0))
        ym = _ym_shift(ym, 1)

    saldi = {ym: sum(werte.get((ym, c), 0) for c in regeln) for ym in monate}
    return {
        "monate": monate,
        "kategorien": regeln,
        "werte": werte,
        "saldi": saldi,
        "hist": hist,
        "fenster12": fenster12,
    }
