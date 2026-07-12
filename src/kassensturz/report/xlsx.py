"""XLSX-Workbook (LibreOffice-tauglich): Dashboard + ein Sheet pro Jahr.

Erzeugt den Bericht IMMER frisch aus der Datenbank — das Workbook ist ein
Artefakt, keine Datenhaltung. Nach Sync/Import einfach neu generieren.
Konsolidierungs-Semantik wie im LaTeX-Report: NEUTRAL_CATEGORIES zählen
nicht, "(Sondereffekt)"-Kategorien laufen als eigene Zeile.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from statistics import median, pstdev

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..store import Store
from .build import NEUTRAL_CATEGORIES, SPECIAL_MARKER

EUR_FMT = '#,##0.00 "€"'
PCT_FMT = "0.0%"

INK = "1A1A1A"
GREY = "F2F0EB"  # warmes Hellgrau, Schweizer Kanon
LINE = Side(style="thin", color="D9D5CC")

F_TITLE = Font(size=16, bold=True, color=INK)
F_H2 = Font(size=11, bold=True, color=INK)
F_KPI = Font(size=13, bold=True, color=INK)
F_DIM = Font(size=9, color="777777")
FILL_HEAD = PatternFill("solid", fgColor=GREY)

MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
          "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]

# konsolidiert = ohne interne/neutrale Buchungen; Sondereffekte separat.
# Eigene Konstanten ohne Quotes → als SQL-Literale sicher einbettbar.
_NEUTRAL_SQL = ",".join(f"'{c}'" for c in NEUTRAL_CATEGORIES)
_BASE = (
    f" AND COALESCE(category,'') NOT IN ({_NEUTRAL_SQL})"
    f" AND COALESCE(category,'') NOT LIKE '%{SPECIAL_MARKER}%'"
)

# Orts-Zuordnung: erst eindeutige Kategorien (Wohnungen), dann Städtenamen
# in Empfänger/Zweck (Karten-/Debitumsätze tragen die Stadt im Text).
# Nicht Zuordenbares bleibt ehrlich "überregional / online".
ORT_GP = "Göppingen"
ORT_R = "Regensburg"
ORT_SONST = "überregional / online"
_GP_WORDS = ("goeppingen", "göppingen", "goppingen", "uhingen", "filstal",
             "eislingen", "faurndau", "dornierstrasse",
             # Umland + Göppinger Leben laut Marc: Staufers Markthalle
             # Rechberghausen; Mercedes-Leasing = Birgits Auto in Göppingen
             "rechberghausen", "mercedes-benz leasing", "mercedes.benz leasing")
_R_WORDS = ("regensburg", "heinkelstr", "obertraubling", "neutraubling",
            "bad abbach", "mintraching")
# Unterhalt Birgit zählt zu Göppingen (sie wohnt dort — das Geld
# finanziert den Göppinger Haushalt), ebenso Marcs "Birgit"-Ausgaben
_GP_CATS = ("Wohnen Göppingen", "Birgit", "Unterhalt Birgit")
_R_CATS = ("Miete Heinkelstrasse", "Einrichtung Heinkelstrasse")


def _like_any(words: tuple[str, ...]) -> str:
    return " OR ".join(
        f"lower(counterparty || ' ' || purpose) LIKE '%{w}%'" for w in words
    )


_ORT_EXPR = f"""CASE
  WHEN category IN ({",".join(f"'{c}'" for c in _GP_CATS)}) THEN '{ORT_GP}'
  WHEN category IN ({",".join(f"'{c}'" for c in _R_CATS)}) THEN '{ORT_R}'
  WHEN {_like_any(_GP_WORDS)} THEN '{ORT_GP}'
  WHEN {_like_any(_R_WORDS)} THEN '{ORT_R}'
  ELSE '{ORT_SONST}'
END"""
ORTE = (ORT_GP, ORT_R, ORT_SONST)


def _q(store: Store, sql: str, params: tuple = ()) -> list:
    return store.conn.execute(sql, params).fetchall()


def _income_expense(store: Store, year: str, month: str | None = None) -> tuple[int, int]:
    """Einnahmen/Ausgaben nach KATEGORIE-NETTO (wie der LaTeX-Report):
    Retouren mindern die Ausgaben ihrer Kategorie, statt als "Einkommen"
    zu erscheinen — eine zurückgeschickte Hose ist kein Gehalt."""
    frag, params = "", [year]
    if month:
        frag = " AND substr(booking_date,6,2)=?"
        params.append(month)
    rows = _q(
        store,
        "SELECT COALESCE(category,'(unkategorisiert)') c, SUM(amount_cents) s "
        f"FROM transactions WHERE substr(booking_date,1,4)=? {frag} {_BASE} "
        "GROUP BY c",
        tuple(params),
    )
    inc = sum(r["s"] for r in rows if r["s"] > 0)
    exp = sum(r["s"] for r in rows if r["s"] < 0)
    return inc, exp


def _years(store: Store, first: str = "2022") -> list[str]:
    return [
        r["y"]
        for r in _q(
            store,
            "SELECT DISTINCT substr(booking_date,1,4) AS y FROM transactions "
            "WHERE y >= ? ORDER BY y",
            (first,),
        )
    ]


def _section(ws, row: int, title: str) -> int:
    c = ws.cell(row=row, column=2, value=title)
    c.font = F_H2
    c.border = Border(bottom=Side(style="medium", color=INK))
    return row + 1


def _table_header(ws, row: int, col: int, labels: list[str]) -> None:
    for i, label in enumerate(labels):
        c = ws.cell(row=row, column=col + i, value=label)
        c.font = Font(size=9, bold=True, color=INK)
        c.fill = FILL_HEAD
        c.border = Border(top=LINE, bottom=LINE, left=LINE, right=LINE)


def _cell(ws, row, col, value, fmt=None, bold=False):
    c = ws.cell(row=row, column=col, value=value)
    c.border = Border(bottom=LINE)
    if fmt:
        c.number_format = fmt
    if bold:
        c.font = Font(bold=True)
    return c


def _widths(ws, spec: dict[int, int]) -> None:
    for col, w in spec.items():
        ws.column_dimensions[get_column_letter(col)].width = w


F_DETAIL = Font(size=9, color="555555")

# Drilldown-Zeilen pro Kategorie (eingeklappt); darüber wird abgeschnitten
# und der Rest als Sammelzeile ausgewiesen — kein stilles Kappen.
DETAIL_LIMIT = 150


def _detail_row(ws, row: int, values: list[tuple[int, object, str | None]]) -> None:
    """Eine eingeklappte Drilldown-Zeile (outline level 1, hidden)."""
    for col, value, fmt in values:
        c = ws.cell(row=row, column=col, value=value)
        c.font = F_DETAIL
        if fmt:
            c.number_format = fmt
    ws.row_dimensions[row].outline_level = 1
    ws.row_dimensions[row].hidden = True


def _kpis(ws, row: int, pairs: list[tuple[str, float | int | str]]) -> int:
    for i, (label, value) in enumerate(pairs):
        col = 2 + i * 2
        lab = ws.cell(row=row, column=col, value=label)
        lab.font = F_DIM
        val = ws.cell(row=row + 1, column=col, value=value)
        val.font = F_KPI
        if isinstance(value, (int, float)):
            val.number_format = EUR_FMT
    return row + 3


def _dashboard(store: Store, ws, years: list[str]) -> None:
    ws.title = "Dashboard"
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.outlinePr.summaryBelow = False  # ⊞ an der Summenzeile
    _widths(ws, {1: 2, 2: 26, 3: 14, 4: 14, 5: 14, 6: 14, 7: 14, 8: 14})

    ws.cell(row=2, column=2, value="Kassensturz — Finanzbericht").font = F_TITLE
    ws.cell(row=3, column=2,
            value=f"Stand {date.today().strftime('%d.%m.%Y')} · konsolidiertes "
                  f"Hauptbuch (Karten- und PayPal-Einzelumsätze, Sammelbuchungen "
                  f"neutralisiert) · Sondereffekte separat").font = F_DIM

    # Vermögen
    row = _section(ws, 5, "Vermögen (letzter Sync)")
    balances = store.latest_balances()
    total = sum(b["balance_cents"] for b in balances if b["currency"] == "EUR")
    row = _kpis(ws, row + 1, [("Gesamtvermögen (EUR-Konten)", total / 100)])
    _table_header(ws, row, 2, ["Konto", "Stand", "Datum"])
    for b in balances:
        row += 1
        _cell(ws, row, 2, b["account"])
        _cell(ws, row, 3, b["balance_cents"] / 100,
              EUR_FMT if b["currency"] == "EUR" else f'#,##0.00 "{b["currency"]}"')
        _cell(ws, row, 4, b["snapshot_date"])

    # Jahresübersicht
    row = _section(ws, row + 3, "Jahresübersicht (konsolidiert)")
    row += 1
    _table_header(ws, row, 2,
                  ["Jahr", "Einnahmen", "Ausgaben", "Saldo", "Sondereffekte"])
    for y in years:
        inc, exp = _income_expense(store, y)
        special = _q(
            store,
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions "
            f"WHERE substr(booking_date,1,4)=? AND category LIKE '%{SPECIAL_MARKER}%'",
            (y,),
        )[0][0]
        row += 1
        _cell(ws, row, 2, int(y), bold=True)
        _cell(ws, row, 3, inc / 100, EUR_FMT)
        _cell(ws, row, 4, exp / 100, EUR_FMT)
        _cell(ws, row, 5, (inc + exp) / 100, EUR_FMT, bold=True)
        _cell(ws, row, 6, special / 100, EUR_FMT)

    # Top-Kategorien über alle Jahre (Ausgaben), Jahre als Spalten
    row = _section(ws, row + 3, "Ausgaben-Kategorien × Jahre (Top 15)")
    row += 1
    cats = [
        r["category"]
        for r in _q(
            store,
            "SELECT category, SUM(amount_cents) s FROM transactions "
            f"WHERE amount_cents<0 AND category IS NOT NULL {_BASE} "
            "AND substr(booking_date,1,4)>=? "
            "GROUP BY category ORDER BY s LIMIT 15",
            (years[0],),
        )
    ]
    _table_header(ws, row, 2, ["Kategorie", *years, "Gesamt"])
    for cat in cats:
        by_year = {
            r["y"]: r["s"]
            for r in _q(
                store,
                "SELECT substr(booking_date,1,4) y, SUM(amount_cents) s "
                "FROM transactions WHERE category=? AND amount_cents<0 "
                "GROUP BY y",
                (cat,),
            )
        }
        row += 1
        _cell(ws, row, 2, cat)
        for i, y in enumerate(years):
            _cell(ws, row, 3 + i, by_year.get(y, 0) / 100, EUR_FMT)
        _cell(ws, row, 3 + len(years),
              sum(by_year.get(y, 0) for y in years) / 100, EUR_FMT, bold=True)
        # Drilldown: Händler dieser Kategorie über alle Jahre
        for d in _q(
            store,
            "SELECT COALESCE(NULLIF(counterparty,''),'(ohne Empfänger)') cp,"
            "       COUNT(*) n, SUM(amount_cents) s "
            "FROM transactions WHERE category=? AND amount_cents<0 "
            "GROUP BY lower(cp) ORDER BY s LIMIT ?",
            (cat, DETAIL_LIMIT),
        ):
            row += 1
            _detail_row(ws, row, [
                (2, f"    {d['cp'][:34]}", None),
                (3, d["n"], None),
                (3 + len(years), d["s"] / 100, EUR_FMT),
            ])

    # Ausgaben nach Ort × Jahre (Göppingen vs. Regensburg)
    row = _section(ws, row + 3, "Ausgaben nach Ort × Jahre")
    row += 1
    _table_header(ws, row, 2, ["Ort", *years, "Gesamt"])
    ort_by_year = {
        (r["ort"], r["y"]): r["s"]
        for r in _q(
            store,
            f"SELECT {_ORT_EXPR} ort, substr(booking_date,1,4) y, "
            "       SUM(amount_cents) s "
            f"FROM transactions WHERE amount_cents<0 {_BASE} "
            "AND substr(booking_date,1,4)>=? GROUP BY ort, y",
            (years[0],),
        )
    }
    for ort in ORTE:
        row += 1
        _cell(ws, row, 2, ort)
        for i, y in enumerate(years):
            _cell(ws, row, 3 + i, ort_by_year.get((ort, y), 0) / 100, EUR_FMT)
        _cell(ws, row, 3 + len(years),
              sum(ort_by_year.get((ort, y), 0) for y in years) / 100,
              EUR_FMT, bold=True)
    row += 1
    _cell(ws, row, 2, "Gesamt", bold=True)
    for i, y in enumerate(years):
        _cell(ws, row, 3 + i,
              sum(ort_by_year.get((o, y), 0) for o in ORTE) / 100, EUR_FMT, bold=True)
    _cell(ws, row, 3 + len(years),
          sum(ort_by_year.values()) / 100, EUR_FMT, bold=True)

    ws.freeze_panes = "A2"


def _year_sheet(store: Store, ws, year: str) -> None:
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.outlinePr.summaryBelow = False
    _widths(ws, {1: 2, 2: 12, 3: 14, 4: 14, 5: 14, 6: 4,
                 7: 34, 8: 14, 9: 10, 10: 4, 11: 12, 12: 34, 13: 18, 14: 14})

    ws.cell(row=2, column=2, value=f"Jahresbericht {year}").font = F_TITLE

    inc, exp = _income_expense(store, year)
    n = _q(
        store,
        f"SELECT COUNT(*) FROM transactions WHERE substr(booking_date,1,4)=? {_BASE}",
        (year,),
    )[0][0]
    special = _q(
        store,
        "SELECT COALESCE(SUM(amount_cents),0) FROM transactions "
        f"WHERE substr(booking_date,1,4)=? AND category LIKE '%{SPECIAL_MARKER}%'",
        (year,),
    )[0][0]
    # Spesen-Glättung: Reise-Verauslagung und Emproof-Erstattung liegen in
    # verschiedenen Monaten — "Saldo bereinigt" nimmt BEIDE Seiten raus und
    # zeigt den echten Konsum-Saldo. Auch Circula-Einzüge werden per
    # DKB-Erstattung ausgeglichen (Juni 2026: −1.071 → +1.071 am 17.06.);
    # das technische Storno-Paar 07/2026 ist als Umbuchung neutralisiert.
    from ..check import _spesen_candidates, _spesen_reimbursements
    kand_m: dict[str, int] = {}
    erst_m: dict[str, int] = {}
    for e in _spesen_candidates(store):
        if e["booking_date"].startswith(year):
            kand_m[e["booking_date"][5:7]] = (
                kand_m.get(e["booking_date"][5:7], 0) + e["amount_cents"])
    for r_ in _spesen_reimbursements(store):
        if r_["booking_date"].startswith(year):
            erst_m[r_["booking_date"][5:7]] = (
                erst_m.get(r_["booking_date"][5:7], 0) + r_["amount_cents"])
    spesen_glatt = bool(kand_m or erst_m)
    adj_year = sum(erst_m.values()) + sum(kand_m.values())

    kpis = [
        ("Einnahmen", inc / 100),
        ("Ausgaben", exp / 100),
        ("Saldo laufend", (inc + exp) / 100),
        ("Sondereffekte", special / 100),
        ("Buchungen", n),
    ]
    if spesen_glatt:
        kpis.append(("Saldo bereinigt (o. Spesen)", (inc + exp - adj_year) / 100))
    row = _kpis(ws, 4, kpis)

    # Monatsverlauf
    row = _section(ws, row + 1, "Monatsverlauf")
    row += 1
    head = row
    header = ["Monat", "Einnahmen", "Ausgaben", "Saldo"]
    if spesen_glatt:
        header.append("Saldo bereinigt")
    _table_header(ws, head, 2, header)
    for m in range(1, 13):
        mm = f"{m:02d}"
        i, e = _income_expense(store, year, mm)
        row += 1
        _cell(ws, row, 2, MONTHS[m - 1])
        _cell(ws, row, 3, i / 100, EUR_FMT)
        _cell(ws, row, 4, -e / 100, EUR_FMT)  # Ausgaben positiv fürs Chart
        _cell(ws, row, 5, (i + e) / 100, EUR_FMT)
        if spesen_glatt:
            adj = i + e - erst_m.get(mm, 0) - kand_m.get(mm, 0)
            _cell(ws, row, 6, adj / 100, EUR_FMT)

    chart = BarChart()
    chart.type = "col"
    chart.title = f"Einnahmen vs. Ausgaben {year}"
    chart.height, chart.width = 8, 16
    chart.add_data(Reference(ws, min_col=3, max_col=4, min_row=head, max_row=row),
                   titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=2, min_row=head + 1, max_row=row))
    chart.gapWidth = 60
    chart.legend.position = "b"
    ws.add_chart(chart, f"{'H' if spesen_glatt else 'G'}{head}")

    # Saldo-Treiber je Monat — NUR ANOMALIEN (Marcs Wunsch): Fixkosten
    # (Miete, PKV, Gehalt, Unterhalt …) stecken im "Normalmonat" = Median
    # der 12 Vormonate je Kategorie; gezeigt wird, was den Monat davon
    # wegbewegt. Jahresrechnungen (Kfz-Versicherung, Kfz-Steuer) erscheinen
    # bewusst als Anomalie — sie erklären ja den Ausreißer.
    ANOMALIE_CENTS = 20_000

    # Emproof-Spesen komplett ausklammern (Verauslagung UND Erstattung) —
    # erstattete Reisekosten sind kein Saldo-Treiber, nur Timing. Auch
    # Circula-Einzüge (per DKB-Erstattung ausgeglichen); das Storno-Paar
    # 07/2026 ist ohnehin als Umbuchung neutral.
    from ..check import SPESEN_EXCLUDE, SPESEN_PATTERNS, SPESEN_SINCE
    pat = list(SPESEN_PATTERNS)
    kand_like = " OR ".join(
        "lower(counterparty) LIKE ? OR lower(purpose) LIKE ?" for _ in pat)
    kand_excl = " AND ".join(
        "lower(counterparty) NOT LIKE ?" for _ in SPESEN_EXCLUDE)
    ohne_spesen = (
        " AND COALESCE(category,'') != 'Spesen'"
        f" AND NOT (booking_date >= '{SPESEN_SINCE}' AND amount_cents < 0"
        f" AND ({kand_like}) AND {kand_excl})"
    )
    spesen_params = [f"%{p}%" for p in pat for _ in range(2)] + \
                    [f"%{p}%" for p in SPESEN_EXCLUDE]

    hist: dict[tuple[str, str], int] = {}
    for r in _q(
        store,
        "SELECT substr(booking_date,1,7) ym,"
        "       COALESCE(category,'(unkategorisiert)') c, SUM(amount_cents) s "
        f"FROM transactions WHERE substr(booking_date,1,4) = ? {_BASE} "
        f"{ohne_spesen} GROUP BY ym, c",
        (year, *spesen_params),
    ):
        hist[(r["ym"], r["c"])] = r["s"]
    yms = sorted({ym for ym, _ in hist})
    all_cats = sorted({c for _, c in hist})

    row = _section(ws, row + 3, "Saldo-Treiber je Monat (nur Anomalien)")
    ws.cell(row=row, column=2,
            value="Fixkosten laufen im Normalmonat (Median der übrigen "
                  "Monate des Jahres je Kategorie) mit; Emproof-Spesen und "
                  "-Erstattungen sind ausgeklammert — gezeigt werden nur "
                  f"Abweichungen ≥ {ANOMALIE_CENTS // 100} €; ⊞ öffnet die "
                  "größten Einzelposten").font = F_DIM
    row += 1
    _table_header(ws, row, 2,
                  ["Monat / Kategorie", "Ist", "Normalmonat", "Abweichung"])
    for m in range(1, 13):
        ym = f"{year}-{m:02d}"
        if not any((ym, c) in hist for c in all_cats):
            continue
        andere = [y2 for y2 in yms if y2 != ym]
        med = {
            c: (median([hist.get((y2, c), 0) for y2 in andere])
                if andere else 0)
            for c in all_cats
        }
        cur = {c: hist.get((ym, c), 0) for c in all_cats}
        saldo_m, normal = sum(cur.values()), sum(med.values())
        row += 1
        for col, val, fmt, b in ((2, MONTHS[m - 1], None, True),
                                 (3, saldo_m / 100, EUR_FMT, True),
                                 (4, normal / 100, EUR_FMT, False),
                                 (5, (saldo_m - normal) / 100, EUR_FMT, True)):
            c_ = _cell(ws, row, col, val, fmt, bold=b)
            c_.fill = FILL_HEAD
        for c in sorted(all_cats, key=lambda c: cur[c] - med[c]):
            delta = cur[c] - med[c]
            if abs(delta) < ANOMALIE_CENTS:
                continue
            row += 1
            _detail_row(ws, row, [
                (2, c, None),
                (3, cur[c] / 100, EUR_FMT),
                (4, med[c] / 100, EUR_FMT),
                (5, delta / 100, EUR_FMT),
            ])
            if delta < 0:  # größte Einzelposten hinter der Ausgaben-Anomalie
                cat_filter = ("category IS NULL" if c == "(unkategorisiert)"
                              else "category=?")
                params = (ym,) if c == "(unkategorisiert)" else (ym, c)
                for p in _q(
                    store,
                    "SELECT booking_date, counterparty, amount_cents "
                    f"FROM transactions WHERE substr(booking_date,1,7)=? "
                    f"AND {cat_filter} {_BASE} {ohne_spesen} "
                    "AND amount_cents<0 ORDER BY amount_cents LIMIT 3",
                    (*params, *spesen_params),
                ):
                    row += 1
                    _detail_row(ws, row, [
                        (2, f"    {p['booking_date'][5:]}  "
                            f"{(p['counterparty'] or '')[:34]}", None),
                        (3, p["amount_cents"] / 100, EUR_FMT),
                    ])
                    ws.row_dimensions[row].outline_level = 2

    # Top-Ausgaben-Kategorien
    row = _section(ws, row + 3, "Wichtigste Ausgaben-Kategorien")
    row += 1
    _table_header(ws, row, 2, ["Kategorie", "Betrag", "Anteil"])
    for r in _q(
        store,
        "SELECT COALESCE(category,'(unkategorisiert)') c, SUM(amount_cents) s "
        f"FROM transactions WHERE substr(booking_date,1,4)=? AND amount_cents<0 "
        f"{_BASE} GROUP BY c ORDER BY s LIMIT 15",
        (year,),
    ):
        row += 1
        _cell(ws, row, 2, r["c"])
        _cell(ws, row, 3, r["s"] / 100, EUR_FMT)
        _cell(ws, row, 4, (r["s"] / exp) if exp else 0, PCT_FMT)
        # Drilldown: Einzelbuchungen der Kategorie (⊞ in LibreOffice)
        cat_filter = (
            "category IS NULL" if r["c"] == "(unkategorisiert)" else "category=?"
        )
        params: tuple = (year,) if r["c"] == "(unkategorisiert)" else (year, r["c"])
        details = _q(
            store,
            "SELECT booking_date, counterparty, amount_cents FROM transactions "
            f"WHERE substr(booking_date,1,4)=? AND {cat_filter} AND amount_cents<0 "
            f"ORDER BY amount_cents LIMIT {DETAIL_LIMIT + 1}",
            params,
        )
        for d in details[:DETAIL_LIMIT]:
            row += 1
            _detail_row(ws, row, [
                (2, d["booking_date"][5:], None),  # MM-TT reicht im Jahresblatt
                (3, d["amount_cents"] / 100, EUR_FMT),
                (4, (d["counterparty"] or "")[:45], None),
            ])
        if len(details) > DETAIL_LIMIT:
            rest = _q(
                store,
                "SELECT COUNT(*) n, SUM(amount_cents) s FROM transactions "
                f"WHERE substr(booking_date,1,4)=? AND {cat_filter} "
                "AND amount_cents<0",
                params,
            )[0]
            row += 1
            _detail_row(ws, row, [
                (2, f"… weitere {rest['n'] - DETAIL_LIMIT} Buchungen", None),
                (3, (rest["s"] - sum(d["amount_cents"]
                                     for d in details[:DETAIL_LIMIT])) / 100,
                 EUR_FMT),
            ])

    # Einnahmen nach Kategorie — Netto-Sicht wie die KPI: nur Kategorien
    # mit positivem Jahressaldo (Retouren mindern ihre Ausgaben-Kategorie
    # und sind bewusst KEINE Einnahme); Drilldown auf Einzelbuchungen
    row = _section(ws, row + 3, "Einnahmen nach Kategorie")
    row += 1
    _table_header(ws, row, 2, ["Kategorie", "Betrag", "Anteil"])
    for r in _q(
        store,
        "SELECT COALESCE(category,'(unkategorisiert)') c, SUM(amount_cents) s "
        f"FROM transactions WHERE substr(booking_date,1,4)=? {_BASE} "
        "GROUP BY c HAVING s > 0 ORDER BY s DESC",
        (year,),
    ):
        row += 1
        _cell(ws, row, 2, r["c"], bold=True)
        _cell(ws, row, 3, r["s"] / 100, EUR_FMT, bold=True)
        _cell(ws, row, 4, (r["s"] / inc) if inc else 0, PCT_FMT)
        cat_filter = (
            "category IS NULL" if r["c"] == "(unkategorisiert)" else "category=?"
        )
        params = (year,) if r["c"] == "(unkategorisiert)" else (year, r["c"])
        details = _q(
            store,
            "SELECT booking_date, counterparty, amount_cents FROM transactions "
            f"WHERE substr(booking_date,1,4)=? AND {cat_filter} {_BASE} "
            f"ORDER BY amount_cents DESC LIMIT {DETAIL_LIMIT + 1}",
            params,
        )
        for d in details[:DETAIL_LIMIT]:
            row += 1
            _detail_row(ws, row, [
                (2, d["booking_date"][5:], None),
                (3, d["amount_cents"] / 100, EUR_FMT),
                (4, (d["counterparty"] or "")[:45], None),
            ])
        if len(details) > DETAIL_LIMIT:
            shown = sum(d["amount_cents"] for d in details[:DETAIL_LIMIT])
            row += 1
            _detail_row(ws, row, [
                (2, "… weitere Buchungen", None),
                (3, (r["s"] - shown) / 100, EUR_FMT),
            ])
    row += 1
    _cell(ws, row, 2, "Gesamt", bold=True)
    _cell(ws, row, 3, inc / 100, EUR_FMT, bold=True)
    _cell(ws, row, 4, 1.0 if inc else 0, PCT_FMT)

    # Ausgaben nach Ort: Göppingen vs. Regensburg, zweistufiges Drilldown
    # (Ort → Kategorien → Einzelbuchungen)
    row = _section(ws, row + 3, "Ausgaben nach Ort")
    row += 1
    _table_header(ws, row, 2, ["Ort", "Betrag", "Anteil"])
    for ort in ORTE:
        s_ort = _q(
            store,
            "SELECT COALESCE(SUM(amount_cents),0) s FROM transactions "
            f"WHERE substr(booking_date,1,4)=? AND amount_cents<0 {_BASE} "
            f"AND {_ORT_EXPR}=?",
            (year, ort),
        )[0]["s"]
        row += 1
        _cell(ws, row, 2, ort, bold=True)
        _cell(ws, row, 3, s_ort / 100, EUR_FMT, bold=True)
        _cell(ws, row, 4, (s_ort / exp) if exp else 0, PCT_FMT)
        for cat_row in _q(
            store,
            "SELECT COALESCE(category,'(unkategorisiert)') c, SUM(amount_cents) s "
            f"FROM transactions WHERE substr(booking_date,1,4)=? AND amount_cents<0 "
            f"{_BASE} AND {_ORT_EXPR}=? GROUP BY c ORDER BY s",
            (year, ort),
        ):
            row += 1
            _detail_row(ws, row, [
                (2, f"  {cat_row['c']}", None),
                (3, cat_row["s"] / 100, EUR_FMT),
            ])
            cat_filter = (
                "category IS NULL" if cat_row["c"] == "(unkategorisiert)"
                else "category=?"
            )
            params = ((year, ort) if cat_row["c"] == "(unkategorisiert)"
                      else (year, ort, cat_row["c"]))
            details = _q(
                store,
                "SELECT booking_date, counterparty, amount_cents FROM transactions "
                f"WHERE substr(booking_date,1,4)=? AND amount_cents<0 {_BASE} "
                f"AND {_ORT_EXPR}=? AND {cat_filter} "
                f"ORDER BY amount_cents LIMIT {DETAIL_LIMIT + 1}",
                params,
            )
            for d in details[:DETAIL_LIMIT]:
                row += 1
                _detail_row(ws, row, [
                    (2, f"      {d['booking_date'][5:]}", None),
                    (3, d["amount_cents"] / 100, EUR_FMT),
                    (4, (d["counterparty"] or "")[:45], None),
                ])
                ws.row_dimensions[row].outline_level = 2
            if len(details) > DETAIL_LIMIT:
                shown = sum(d["amount_cents"] for d in details[:DETAIL_LIMIT])
                row += 1
                _detail_row(ws, row, [
                    (2, "      … weitere Buchungen", None),
                    (3, (cat_row["s"] - shown) / 100, EUR_FMT),
                ])
                ws.row_dimensions[row].outline_level = 2
    row += 1
    _cell(ws, row, 2, "Gesamt", bold=True)
    _cell(ws, row, 3, exp / 100, EUR_FMT, bold=True)
    _cell(ws, row, 4, 1.0 if exp else 0, PCT_FMT)

    # Größte Einzelposten + Top-Händler nebeneinander
    top_row = _section(ws, row + 3, "Größte Einzelposten (Ausgaben)")
    _table_header(ws, top_row + 1, 2, ["Datum", "Empfänger", "Kategorie", "Betrag"])
    r2 = top_row + 1
    _widths(ws, {3: 34})
    for r in _q(
        store,
        "SELECT booking_date, counterparty, category, amount_cents "
        f"FROM transactions WHERE substr(booking_date,1,4)=? AND amount_cents<0 "
        f"{_BASE} ORDER BY amount_cents LIMIT 20",
        (year,),
    ):
        r2 += 1
        _cell(ws, r2, 2, r["booking_date"])
        _cell(ws, r2, 3, (r["counterparty"] or "")[:40])
        _cell(ws, r2, 4, r["category"] or "—")
        _cell(ws, r2, 5, r["amount_cents"] / 100, EUR_FMT)

    hdr = ws.cell(row=top_row, column=7, value="Top-Händler (aggregiert)")
    hdr.font = F_H2
    hdr.border = Border(bottom=Side(style="medium", color=INK))
    _table_header(ws, top_row + 1, 7, ["Händler", "Zahlungen", "Summe"])
    r3 = top_row + 1
    for r in _q(
        store,
        "SELECT counterparty cp, COUNT(*) n, SUM(amount_cents) s "
        f"FROM transactions WHERE substr(booking_date,1,4)=? AND amount_cents<0 "
        f"{_BASE} AND counterparty != '' "
        "GROUP BY lower(counterparty) ORDER BY s LIMIT 15",
        (year,),
    ):
        r3 += 1
        _cell(ws, r3, 7, r["cp"][:38])
        _cell(ws, r3, 8, r["n"])
        _cell(ws, r3, 9, r["s"] / 100, EUR_FMT)

    ws.freeze_panes = "A2"


# --- Verträge & Abos: wiederkehrende Zahlungen automatisch erkennen ---------
#
# Gruppierung nach (Anbieter-Schlüssel, Betrag) — so werden mehrere
# Google-/Microsoft-Abos mit unterschiedlichen Beträgen einzeln erkannt.
# Beitrags-/Namenswechsel desselben Vertrags (Hallesche zieht um, FinTS
# klebt die IBAN vor den Namen) werden anschließend zu EINER Zeile gemergt;
# Versicherungen werden zusätzlich kategorie-basiert erfasst, weil
# Jahresverträge mit Beitragsanpassung selten 3× denselben Betrag zeigen.

_TURNUS = (  # (min Tage, max Tage, Label, Zahlungen pro Jahr)
    (24, 38, "monatlich", 12),
    (52, 75, "2-monatlich", 6),
    (80, 105, "quartalsweise", 4),
    (165, 205, "halbjährlich", 2),
    (330, 420, "jährlich", 1),
)

# exakt 20 Ziffern (deutsche IBAN) — {16,30} wäre gierig und frisst die
# führende "1" von "1+1 Mail + Media"/"1blu" mit
_IBAN_PREFIX = r"^[A-Z]{2}\d{20}"
_KEY_NOISE = {  # Füllwörter + Rechtsformen, die keinen Anbieter benennen
    "die", "der", "das", "und", "the",
    "gmbh", "ag", "kg", "ohg", "vvag", "inc", "ltd", "ug", "co", "eg",
    "se", "sa", "aktiengesellschaft", "limited",
}


def _abo_key(counterparty: str) -> str:
    """Anbieter-Schlüssel: IBAN-Präfix, Rechtsform und Füllwörter weg,
    erstes Namens-Token behalten — 'HALLESCHE Reinsburgstrasse 10',
    'Hallesche' und 'DE24…HALLESCHE' fallen so zusammen."""
    s = re.sub(_IBAN_PREFIX, "", counterparty)
    s = re.sub(r"[^a-zäöüß ]", " ", s.lower())
    for t in s.split():
        if len(t) > 2 and t not in _KEY_NOISE:
            return t
    return ""


def _abo_subkey(counterparty: str) -> str:
    """Feinerer Schlüssel (zwei Namens-Tokens) — trennt parallele Verträge
    desselben Anbieters trotz variierender Schreibweisen ('Google Payment
    Ireland Ltd.'/'… Limited' vs. 'Google')."""
    s = re.sub(_IBAN_PREFIX, "", counterparty)
    s = re.sub(r"[^a-zäöüß ]", " ", s.lower())
    tokens = [t for t in s.split() if len(t) > 2 and t not in _KEY_NOISE]
    return " ".join(tokens[:2])


def _anbieter(counterparty: str) -> str:
    return re.sub(_IBAN_PREFIX, "", counterparty).strip()[:38]


def _aktiv(today: date, last: date, med: float) -> bool:
    # 1,35 Perioden + Karenz: eine ausgesetzte Monatsrate beendet kein Abo,
    # eine >1,35 Jahre ausbleibende Jahresrechnung schon
    return (today - last).days <= max(med * 1.35, med + 35)


def _turnus_of(med: float, tolerant: bool = False) -> tuple | None:
    """Turnus zum Median-Abstand; tolerant = weitere Bänder (Versicherungen
    verschieben gern den Jahresbeginn, 325 Tage sind noch 'jährlich')."""
    lo, hi = (0.85, 1.1) if tolerant else (1.0, 1.0)
    return next((t for t in _TURNUS if t[0] * lo <= med <= t[1] * hi), None)


def _regular_line(key: str, rows: list, amount: int, today: date) -> dict | None:
    """Prüft eine Zahlungsreihe auf Abo-Regelmäßigkeit → Zeile oder None."""
    if len(rows) < 3:
        return None
    dates = [date.fromisoformat(r["booking_date"]) for r in rows]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    med = median(gaps)
    turnus = _turnus_of(med)
    # Abos sind regelmäßig — unruhige Intervalle sind Einkaufsmuster
    if turnus is None or (len(gaps) > 1 and pstdev(gaps) > 0.35 * med):
        return None
    return {
        "key": key,
        "anbieter": _anbieter(rows[-1]["counterparty"]),
        "kategorie": rows[-1]["c"],
        "turnus": turnus[2],
        "betrag": -amount,
        "jahr": -amount * turnus[3],
        "seit": dates[0].isoformat(),
        "letzte": dates[-1].isoformat(),
        "n": len(rows),
        "aktiv": _aktiv(today, dates[-1], med),
    }


def _merge_vertraege(lines: list[dict]) -> list[dict]:
    """Gleicher Anbieter + Kategorie, zeitlich nacheinander = derselbe
    Vertrag mit Beitrags-/Namenswechsel → eine Zeile (Betrag = aktuell).
    Deutliche Überschneidung = parallele Verträge, bleiben getrennt."""
    by: dict[tuple[str, str], list[dict]] = {}
    for line in lines:
        by.setdefault((line["key"], line["kategorie"]), []).append(line)
    out = []
    for group in by.values():
        group.sort(key=lambda a: a["seit"])
        merged = [group[0]]
        for cur in group[1:]:
            prev = merged[-1]
            overlap = (date.fromisoformat(prev["letzte"])
                       - date.fromisoformat(cur["seit"])).days
            if overlap <= 45:
                cur["seit"] = prev["seit"]
                cur["letzte"] = max(cur["letzte"], prev["letzte"])
                cur["n"] += prev["n"]
                merged[-1] = cur
            else:
                merged.append(cur)
        out.extend(merged)
    return out


# Mehrere Verträge beim selben Versicherer (HUK: 2× Kfz + Motorrad) tragen
# die Vertragsnummer im Zweck ("667/865989-L, R-XI 197") — daran splitten,
# sonst verschmilzt alles zu EINER "unregelmäßig"-Sammelzeile (Marcs
# HUK-Befund 12.07.2026). Kennzeichen dient als sprechender Namenszusatz.
_VERTRAG_NR = re.compile(r"\b(\d{2,4}/\d{5,9}(?:-[A-Z0-9])?)\b")
_KENNZEICHEN = re.compile(
    r"\b\d{2,4}/\d{5,9}(?:-[A-Z0-9])?\s*,\s*"
    r"([A-Za-zÄÖÜäöü]{1,3}-[A-Za-z]{1,2}\s?\d{1,4})")


def _versicherungen(store: Store, today: date) -> list[dict]:
    """Versicherungen laufen KOMPLETT kategorie-basiert (nicht über die
    Betrags-Gruppierung): Jahresverträge mit Beitragsanpassung (Concordia,
    Rhion, HUK …) haben selten 3× denselben Betrag, und ein frisch
    abgeschlossener Vertrag (Stuttgarter) hat erst eine Zahlung."""
    groups: dict[tuple[str, str, str], list] = {}
    # Rücklastschrift-Gutschriften tragen den EIGENEN Namen als Absender
    # (nicht den Versicherer) — Zuordnung läuft über die Vertragsnummer im
    # Zweck; gesammelt je Nummer, verrechnet unten gegen die Belastungen
    ruecklaeufer: dict[str, list] = {}
    for r in _q(
        store,
        "SELECT counterparty, purpose, COALESCE(category,'(unkategorisiert)') c,"
        "       booking_date, amount_cents "
        f"FROM transactions WHERE amount_cents != 0 {_BASE} "
        "AND category LIKE '%ersicherung%' ORDER BY booking_date",
    ):
        m = _VERTRAG_NR.search(r["purpose"] or "")
        nr = m.group(1) if m else ""
        if r["amount_cents"] > 0:
            if nr and "lastschrift" in (r["purpose"] or "").lower():
                ruecklaeufer.setdefault(nr, []).append(r)
            continue
        key = _abo_key(r["counterparty"])
        if key:
            groups.setdefault((key, r["c"], nr), []).append(r)

    lines = []
    for (key, cat, nr), rows in groups.items():
        # Storno-Paare verrechnen: geplatzte Lastschrift + Rücklastschrift
        # (gleiche Vertragsnummer, gleicher Betrag, ≤ 35 Tage) heben sich
        # auf — sonst zählt der Rückläufer als zweiter Jahresbeitrag
        for gut in ruecklaeufer.get(nr, []):
            gd = date.fromisoformat(gut["booking_date"])
            for i, r in enumerate(rows):
                if (r["amount_cents"] == -gut["amount_cents"] and
                        abs((gd - date.fromisoformat(r["booking_date"])).days) <= 35):
                    del rows[i]
                    break
        if not rows:
            continue
        dates = [date.fromisoformat(r["booking_date"]) for r in rows]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        # Einzelzahlung = frischer Vertrag → als Jahresbeitrag annehmen
        med = median(gaps) if gaps else 365.0
        amounts = [-r["amount_cents"] for r in rows]
        # Turnus × Betrag nur hochrechnen, wenn die Beträge homogen sind —
        # HUK bündelt mehrere Kfz-Verträge unter einem Namen, da wäre
        # "letzte Jahresrechnung × 4" grober Unsinn
        homogen = pstdev(amounts) <= 0.2 * (sum(amounts) / len(amounts))
        turnus = _turnus_of(med, tolerant=True) if homogen else None
        betrag = amounts[-1]
        aktiv = _aktiv(today, dates[-1], med)
        if turnus:
            label, jahr = turnus[2], betrag * turnus[3]
        else:
            # unregelmäßig: der Median-Abstand ist bei gemischten Beträgen
            # bedeutungslos (Erst-+Folgebeitrag 14 Tage auseinander machten
            # die HUK R-MS fälschlich "beendet") → festes Jahresfenster+Kulanz
            aktiv = (today - dates[-1]).days <= 410
            # Summe der letzten 365 Tage als Jahreskosten
            end = today if aktiv else dates[-1]
            cutoff = end.toordinal() - 365
            jahr = sum(a for a, d in zip(amounts, dates)
                       if cutoff < d.toordinal() <= end.toordinal())
            label = "unregelmäßig"
        anbieter = _anbieter(rows[-1]["counterparty"])
        if nr:
            mk = _KENNZEICHEN.search(rows[-1]["purpose"] or "")
            anbieter = f"{anbieter} · {mk.group(1) if mk else nr}"
        elif any(k == key and c == cat and n for (k, c, n) in groups):
            # Restzahlungen ohne Nummer neben nummerierten Verträgen
            # (QR-/Sonderzahlungen) ehrlich kennzeichnen statt verschmelzen
            anbieter = f"{anbieter} · ohne Vertragsnr."
        lines.append({
            "key": f"{key}·{nr}" if nr else key,
            "anbieter": anbieter,
            "kategorie": cat,
            "turnus": label,
            "betrag": betrag,
            "jahr": jahr,
            "seit": dates[0].isoformat(),
            "letzte": dates[-1].isoformat(),
            "n": len(rows),
            "aktiv": aktiv,
        })
    return lines


def _sparplaene(store: Store, today: date) -> list[dict]:
    """Sparpläne aufs eigene Depot laufen als Umbuchung (neutral — das Geld
    bleibt Vermögen, keine Ausgabe) und fielen deshalb aus der Übersicht.
    Marcs 1.000-€-TR-Sparplan soll trotzdem als Vertrag sichtbar sein."""
    groups: dict[tuple[str, int], list] = {}
    for r in _q(
        store,
        "SELECT counterparty, 'Vermögensaufbau (Umbuchung)' c,"
        "       booking_date, amount_cents "
        "FROM transactions WHERE amount_cents < 0 AND category = 'Umbuchung' "
        "AND lower(counterparty) LIKE '%trade republic%' ORDER BY booking_date",
    ):
        key = _abo_key(r["counterparty"])
        if key:
            groups.setdefault((key, r["amount_cents"]), []).append(r)
    return [line for (key, amount), rows in groups.items()
            if (line := _regular_line(key, rows, amount, today))]


def _detect_abos(store: Store, today: date) -> list[dict]:
    groups: dict[tuple[str, int], list] = {}
    for r in _q(
        store,
        "SELECT counterparty, COALESCE(category,'(unkategorisiert)') c,"
        "       booking_date, amount_cents "
        f"FROM transactions WHERE amount_cents < 0 {_BASE} "
        "AND COALESCE(category,'') NOT LIKE '%ersicherung%' "
        "ORDER BY booking_date",
    ):
        key = _abo_key(r["counterparty"])
        if key:
            groups.setdefault((key, r["amount_cents"]), []).append(r)

    abos = []
    for (key, amount), rows in groups.items():
        line = _regular_line(key, rows, amount, today)
        if line:
            abos.append(line)
            continue
        # zwei parallele Verträge gleichen Anbieters und Betrags (z. B.
        # Google-Abos) machen die Reihe unruhig → je Feinschlüssel prüfen
        sub: dict[str, list] = {}
        for r in rows:
            sub.setdefault(_abo_subkey(r["counterparty"]), []).append(r)
        if len(sub) > 1:
            abos.extend(
                line for srows in sub.values()
                if (line := _regular_line(key, srows, amount, today))
            )

    abos += _versicherungen(store, today)
    abos += _sparplaene(store, today)
    # Merge zum Schluss über alle Pässe — so wird auch die Sparplan-
    # Erhöhung (600 → 1.000) zu einer Linie
    abos = _merge_vertraege(abos)
    abos.sort(key=lambda a: (not a["aktiv"], -a["jahr"]))
    return abos


# Pivot-Gruppen für die Vertragsübersicht: Gruppenzeile mit Summe,
# Einzelverträge zum Ausklappen darunter (Marcs Wunsch, 11.07.2026)
_WOHNEN_CATS = ("Miete Heinkelstrasse", "Wohnen & Nebenkosten",
                "Wohnen Göppingen", "Strom", "Wohnen und Garten")
_FAMILIE_CATS = ("Unterhalt Birgit", "Unterhalt Paul", "Familie",
                 "Unterstützung Mutter")
# Sparpläne sind Vermögensaufbau, keine Kosten → eigene Gruppe, die aus
# den Kosten-KPIs herausgehalten wird
GRUPPE_SPAREN = "Vermögensaufbau"


def lade_gruppen(store: Store) -> dict[str, str]:
    """Kategorie → Gruppe aus der DB (Einstellungen-Seite pflegt sie)."""
    return {r[0]: r[1] for r in store.conn.execute(
        "SELECT name, gruppe FROM categories WHERE gruppe IS NOT NULL")}


def _abo_gruppe(kategorie: str, mapping: dict | None = None) -> str:
    if mapping and mapping.get(kategorie):
        return mapping[kategorie]
    # Heuristik-Fallback (leere DBs, synthetische Kategorien)
    if kategorie in ("Sparplan Romy", "Vermögensaufbau (Umbuchung)"):
        return GRUPPE_SPAREN
    if "ersicherung" in kategorie.lower():
        return "Versicherungen"
    if kategorie in _WOHNEN_CATS:
        return "Wohnen & Miete"
    if kategorie in _FAMILIE_CATS:
        return "Unterhalt & Familie"
    if kategorie in ("Abos & Dienste", "Medien"):
        return "Abos & Medien"
    if kategorie in ("Bankgebühren", "Bank & Kredit"):
        return "Bank & Gebühren"
    return "Sonstiges"


def _abos_sheet(store: Store, ws) -> None:
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.outlinePr.summaryBelow = False  # ⊞ an der Gruppenzeile
    _widths(ws, {1: 2, 2: 36, 3: 22, 4: 16, 5: 12, 6: 13, 7: 11, 8: 11, 9: 10})
    ws.cell(row=2, column=2, value="Verträge & Abos").font = F_TITLE
    ws.cell(row=3, column=2,
            value="Gruppen ausklappen (⊞ links) für die Einzelverträge · "
                  "automatisch erkannt: regelmäßige Zahlungen je Anbieter; "
                  "Beitrags-/Namenswechsel desselben Vertrags zu einer Zeile "
                  "zusammengeführt (Betrag = aktuell); Versicherungen "
                  "kategorie-basiert, auch bei wechselnden Beträgen").font = F_DIM

    today = date.today()
    abos = _detect_abos(store, today)
    mapping = lade_gruppen(store)
    aktive = [a for a in abos if a["aktiv"]]
    spar_jahr = sum(a["jahr"] for a in aktive
                    if _abo_gruppe(a["kategorie"], mapping) == GRUPPE_SPAREN)
    kosten_jahr = sum(a["jahr"] for a in aktive) - spar_jahr
    row = _kpis(ws, 5, [
        ("Aktive Verträge/Abos", len(aktive)),
        ("Kosten pro Jahr (aktiv)", kosten_jahr / 100),
        ("davon monatlich gebunden",
         sum(a["betrag"] for a in aktive if a["turnus"] == "monatlich"
             and _abo_gruppe(a["kategorie"], mapping) != GRUPPE_SPAREN) / 100),
        ("Sparpläne pro Jahr", spar_jahr / 100),
    ])
    _table_header(ws, row, 2, ["Gruppe / Anbieter", "Kategorie", "Turnus",
                               "Betrag", "Kosten/Jahr", "seit", "letzte",
                               "Status"])

    gruppen: dict[str, list[dict]] = {}
    for a in abos:
        gruppen.setdefault(_abo_gruppe(a["kategorie"], mapping), []).append(a)

    # Gruppen nach aktiven Jahreskosten, Verträge darin aktiv zuerst
    for label, items in sorted(
        gruppen.items(),
        key=lambda kv: -sum(a["jahr"] for a in kv[1] if a["aktiv"]),
    ):
        aktive = [a for a in items if a["aktiv"]]
        row += 1
        g = _cell(ws, row, 2, label, bold=True)
        g.fill = FILL_HEAD
        n = _cell(ws, row, 4,
                  f"{len(aktive)} aktiv"
                  + (f", {len(items) - len(aktive)} beendet"
                     if len(items) > len(aktive) else ""))
        n.font = F_DIM
        n.fill = FILL_HEAD
        s = _cell(ws, row, 6, sum(a["jahr"] for a in aktive) / 100,
                  EUR_FMT, bold=True)
        s.fill = FILL_HEAD
        for col in (3, 5, 7, 8, 9):
            ws.cell(row=row, column=col).fill = FILL_HEAD
        for a in sorted(items, key=lambda a: (not a["aktiv"], -a["jahr"])):
            row += 1
            _detail_row(ws, row, [
                (2, a["anbieter"], None),
                (3, a["kategorie"], None),
                (4, f"{a['turnus']} ({a['n']}×)", None),
                (5, a["betrag"] / 100, EUR_FMT),
                (6, a["jahr"] / 100, EUR_FMT),
                (7, a["seit"], None),
                (8, a["letzte"], None),
                (9, "aktiv" if a["aktiv"] else "beendet", None),
            ])
            ws.cell(row=row, column=9).font = Font(
                size=9, bold=a["aktiv"],
                color="1A7F37" if a["aktiv"] else "999999")
    ws.freeze_panes = "A2"


def _spesen_sheet(store: Store, ws) -> None:
    """Spesen-Status (Emproof/Circula): Monatsübersicht + Belege ohne
    Einzel-Match. WICHTIG fürs Lesen: Sammelreports erstatten pauschal —
    Belege ohne 1:1-Match sind dadurch meist trotzdem bezahlt; die Kennzahl
    „finanziell offen" verrechnet den Sammelreport-Überhang."""
    from ..check import SPESEN_SINCE, match_spesen

    ws.sheet_view.showGridLines = False
    _widths(ws, {1: 2, 2: 12, 3: 10, 4: 13, 5: 13, 6: 44, 7: 14})
    ws.cell(row=2, column=2, value="Spesen (Emproof / Circula)").font = F_TITLE
    ws.cell(row=3, column=2,
            value=f"Reise-/Spesenkandidaten seit {SPESEN_SINCE} · Matching "
                  "beleggenau (gleicher Betrag, 0–90 Tage); Sammelreports "
                  "(Travel Expenses) erstatten pauschal und decken Belege "
                  "ohne Einzel-Match").font = F_DIM

    result = match_spesen(store)
    offen_fin = max(0, result["unmatched_total"] - result["surplus"])
    row = _kpis(ws, 5, [
        ("Erstattet gesamt", result["reimbursed_total"] / 100),
        ("Belege ohne Einzel-Match", result["unmatched_total"] / 100),
        ("Sammelreport-Überhang", result["surplus"] / 100),
        ("Finanziell offen", offen_fin / 100),
    ])

    # Monatstabelle
    months: dict[str, dict] = {}
    for e in result["expenses"]:
        mkey = e["booking_date"][:7]
        months.setdefault(mkey, {"n": 0, "s": 0, "o": 0})
        months[mkey]["n"] += 1
        months[mkey]["s"] += e["amount_cents"]
    for e in result["unmatched"]:
        months[e["booking_date"][:7]]["o"] += e["amount_cents"]
    _table_header(ws, row, 2, ["Monat", "Belege", "Summe", "ohne Match"])
    for mkey in sorted(months):
        row += 1
        _cell(ws, row, 2, mkey)
        _cell(ws, row, 3, months[mkey]["n"])
        _cell(ws, row, 4, -months[mkey]["s"] / 100, EUR_FMT)
        _cell(ws, row, 5, -months[mkey]["o"] / 100, EUR_FMT)

    # Beleg-Liste (nur > Bagatelle und älter als Kulanzfrist — wie `check`)
    row = _section(ws, row + 3, "Belege ohne Einzel-Match")
    ws.cell(row=row, column=6,
            value="rechnerisch durch Sammelreports gedeckt"
            if offen_fin == 0 else
            "NICHT voll gedeckt — in Circula einreichen!").font = F_DIM
    row += 1
    _table_header(ws, row, 2, ["Datum", "", "Betrag", "", "Händler", "Konto"])
    for e in result["unmatched"]:
        row += 1
        _cell(ws, row, 2, e["booking_date"])
        _cell(ws, row, 4, -e["amount_cents"] / 100, EUR_FMT)
        _cell(ws, row, 6, (e["counterparty"] or "")[:42])
        _cell(ws, row, 7, e["account"])
    ws.freeze_panes = "A2"


def _budget_sheet(store: Store, ws, budget_path: Path) -> None:
    """Budget vs. spesenbereinigtes Ist (Steuergröße: Δ YTD ab Budget-Start)."""
    from ..budget import report

    today = date.today()
    r = report(store, budget_path, f"{today.year}", today.month)
    ws.sheet_view.showGridLines = False
    _widths(ws, {1: 2, 2: 30, 3: 12, 4: 12, 5: 12, 6: 12, 7: 12})
    ws.cell(row=2, column=2, value="Budget").font = F_TITLE
    ws.cell(row=3, column=2,
            value=f"ab Budget-Start, Stand {r['monat']} über {r['n_monate']} "
                  "Monat(e) · Ist ist spesenbereinigt; Sparpläne zählen "
                  "nicht (Vermögensaufbau) · Jahres-/Quartalsposten machen "
                  "Monate klumpig — steuere nach Δ YTD").font = F_DIM
    row = _kpis(ws, 5, [
        ("Budget pro Monat", r["summe_budget"] / 100),
        ("Einkommensreferenz", r["einkommen"] / 100),
        ("Plan-Saldo/Monat", r["plan_saldo"] / 100),
        ("Ist lfd. Monat", r["monat_ist"] / 100),
    ])
    _table_header(ws, row, 2, ["Kategorie", "Budget", "Ist Monat", "Δ Monat",
                               "Ist YTD", "Δ YTD"])
    for z in r["zeilen"]:
        if not z["budget"] and not z["ytd_ist"]:
            continue
        row += 1
        _cell(ws, row, 2, z["kategorie"])
        _cell(ws, row, 3, z["budget"] / 100, EUR_FMT)
        _cell(ws, row, 4, z["monat_ist"] / 100, EUR_FMT)
        _cell(ws, row, 5, z["monat_delta"] / 100, EUR_FMT)
        _cell(ws, row, 6, z["ytd_ist"] / 100, EUR_FMT)
        c = _cell(ws, row, 7, z["ytd_delta"] / 100, EUR_FMT, bold=True)
        c.font = Font(bold=True,
                      color="B42318" if z["ytd_delta"] < 0 else "1A7F37")
    row += 1
    _cell(ws, row, 2, "GESAMT", bold=True)
    _cell(ws, row, 3, r["summe_budget"] / 100, EUR_FMT, bold=True)
    _cell(ws, row, 4, r["monat_ist"] / 100, EUR_FMT, bold=True)
    _cell(ws, row, 6, r["ytd_ist"] / 100, EUR_FMT, bold=True)
    _cell(ws, row, 7, (r["ytd_budget"] - r["ytd_ist"]) / 100, EUR_FMT, bold=True)
    ws.freeze_panes = "A2"


def _forecast_sheet(store: Store, ws) -> None:
    """Kategorien × Monate bis Ende nächsten Jahres; stetig = Median,
    klumpig = Vorjahresmonat (Jahres-Saisonalität)."""
    from ..forecast import _ym_shift, build_forecast

    today = date.today()
    last_full = _ym_shift(f"{today.year:04d}-{today.month:02d}", -1)
    until = f"{today.year + 1}-12"
    f = build_forecast(store, last_full, until)
    monate = f["monate"]

    ws.sheet_view.showGridLines = False
    _widths(ws, {1: 2, 2: 28, 3: 24})
    for i in range(len(monate) + 1):
        _widths(ws, {4 + i: 10})
    ws.cell(row=2, column=2, value="Forecast").font = F_TITLE
    ws.cell(row=3, column=2,
            value=f"Prognose {monate[0]} – {until} · stetige Kategorien = "
                  "6-Monats-Median, klumpige = Vorjahresmonat (Saisonalität "
                  "aus 2025/26: Versicherungs-Jahresrechnungen, ESt-VZ, "
                  "Kfz-Steuer). Annahmen: Unterhalt Birgit läuft unverändert "
                  "weiter, Mercedes-Leasing unbefristet, keine Erhöhungen, "
                  "keine Sondereffekte (Kiteworks 2027 fehlt bewusst), "
                  "ohne Spesen/Sparpläne.").font = F_DIM

    j1 = monate[0][:4]
    jahres_saldi = {
        jahr: sum(f["saldi"][ym] for ym in monate if ym.startswith(jahr))
        for jahr in sorted({ym[:4] for ym in monate})
    }
    kpis = [(f"Saldo {j} (Rest)" if j == j1 else f"Saldo {j}", s / 100)
            for j, s in jahres_saldi.items()]
    kpis.append(("Entsparen bis " + until, sum(jahres_saldi.values()) / 100))
    row = _kpis(ws, 5, kpis)

    _table_header(ws, row, 2, ["Kategorie", "Regel"]
                  + [m[2:] for m in monate])
    # Kategorien: Einnahmen zuerst, dann Ausgaben nach Jahressumme
    def _summe(c):
        return sum(f["werte"].get((ym, c), 0) for ym in monate)
    for c in sorted(f["kategorien"], key=lambda c: -_summe(c)):
        row += 1
        _cell(ws, row, 2, c, bold=_summe(c) > 0)
        _cell(ws, row, 3, f["kategorien"][c])
        for i, ym in enumerate(monate):
            v = f["werte"].get((ym, c), 0)
            if v:
                _cell(ws, row, 4 + i, v / 100, EUR_FMT)
    row += 1
    _cell(ws, row, 2, "SALDO", bold=True)
    for i, ym in enumerate(monate):
        c_ = _cell(ws, row, 4 + i, f["saldi"][ym] / 100, EUR_FMT, bold=True)
        c_.font = Font(bold=True,
                       color="B42318" if f["saldi"][ym] < 0 else "1A7F37")
    row += 1
    kum = 0
    _cell(ws, row, 2, "kumuliert", bold=True)
    for i, ym in enumerate(monate):
        kum += f["saldi"][ym]
        _cell(ws, row, 4 + i, kum / 100, EUR_FMT)
    ws.freeze_panes = "D2"


def _est_sheet(store: Store, ws, lohn_folder: Path, basis_kv_pv: float) -> None:
    """ESt-Projektion des laufenden Jahres aus den Gehaltsabrechnungen."""
    from ..est import projektion

    jahr = f"{date.today().year}"
    p = projektion(store, lohn_folder, jahr, basis_kv_pv)
    ws.sheet_view.showGridLines = False
    _widths(ws, {1: 2, 2: 44, 3: 14, 4: 3, 5: 60})
    ws.cell(row=2, column=2, value=f"ESt-Projektion {jahr}").font = F_TITLE
    ws.cell(row=3, column=2,
            value=f"Quelle: {p['lohn']['quelle']} (YTD {p['lohn']['monate_ytd']} "
                  f"Monate, Rest hochgerechnet) · NÄHERUNG mit vorläufigem "
                  f"Tarif {jahr}, Einzelveranlagung, WK-Pauschbetrag — "
                  "verbindlich rechnet die neo-Kanzlei").font = F_DIM
    row = _kpis(ws, 5, [
        ("Erwartete Steuerschuld", p["schuld"]),
        ("Bereits geleistet (inkl. offener VZ)", p["geleistet"]),
        ("Erwartete ERSTATTUNG" if p["saldo"] >= 0 else "Erwartete NACHZAHLUNG",
         abs(p["saldo"])),
    ])

    zeilen = [
        ("Jahresbrutto (Gehalt, 12 Monate)", p["brutto_jahr"], ""),
        ("− Werbungskosten (Pauschbetrag)", -p["wk"],
         "tatsächliche WK (Reisen vor Jobstart etc.) können mehr sein"),
        ("− Rentenversicherung AN-Anteil", -p["vorsorge_rv"], "voll abziehbar"),
        ("− Basis-KV/PV nach AG-Zuschuss", -p["vorsorge_kv"],
         f"Basis {p['basis_kv_pv']:,.2f} − AG-Zuschuss {p['ag_zuschuss']:,.2f}"),
        ("− Sonderausgaben-Pauschbetrag", -p["sa_pausch"], ""),
        ("= zu versteuerndes Einkommen (zvE)", p["zve"], ""),
        (f"Einkommensteuer (Tarif {jahr})", p["est"],
         "Grenzsteuersatz 42 %"),
        ("+ Solidaritätszuschlag", p["soli"], ""),
        ("= erwartete Steuerschuld", p["schuld"], ""),
        ("", None, ""),
        ("Lohnsteuer einbehalten (proj. 12 Mon.)", p["lst_jahr"], ""),
        ("Soli einbehalten (proj. 12 Mon.)", p["soli_lst_jahr"], ""),
        (f"ESt-VZ gezahlt ({p['vz_n']} Quartale)", p["vz_gezahlt"],
         "Freistaat Bayern, 1.189,50/Quartal"),
        ("ESt-VZ offen (10.09. + 10.12.)", p["vz_offen"], ""),
        ("= geleistet gesamt", p["geleistet"], ""),
        ("", None, ""),
        ("ERGEBNIS (+ = Erstattung)", p["saldo"], ""),
    ]
    _table_header(ws, row, 2, ["Position", "Betrag", "", "Hinweis"])
    for text, val, hint in zeilen:
        row += 1
        if val is None:
            continue
        _cell(ws, row, 2, text, bold=text.startswith(("=", "ERGEBNIS")))
        _cell(ws, row, 3, val, EUR_FMT, bold=text.startswith(("=", "ERGEBNIS")))
        if hint:
            ws.cell(row=row, column=5, value=hint).font = F_DIM

    row += 2
    for hinweis in (
        "Nicht eingerechnet (verbessern das Ergebnis): Kinderfreibeträge "
        "Romy + Paul (Günstigerprüfung, grob +1–2k), tatsächliche "
        "Werbungskosten über Pauschbetrag.",
        "Option Realsplitting §10 Abs. 1a: Unterhalt an Birgit bis 13.805 € "
        "als Sonderausgaben (≈ +5,8k bei 42 %) — braucht ihre Zustimmung "
        "(Anlage U), im Streit unrealistisch; via Herlitze/Benny prüfen.",
        "Kapitalerträge (DKB-Zinsen ~5,5k) sind abgeltungsbesteuert an der "
        "Quelle — bei 42 % Grenzsteuersatz keine Günstigerprüfung sinnvoll.",
        "VSOP/Hurdle-Zuteilung 06/2026: Dry-Income-Struktur, keine Steuer "
        "bei Zuteilung (lt. Benny-Struktur) — bei Exit neu rechnen.",
    ):
        ws.cell(row=row, column=2, value="• " + hinweis).font = F_DIM
        row += 1
    ws.freeze_panes = "A2"


def build_workbook(store: Store, out_path: Path, first_year: str = "2022") -> Path:
    years = _years(store, first_year)
    if not years:
        raise ValueError(f"keine Buchungen ab {first_year} in der Datenbank")
    wb = Workbook()
    _dashboard(store, wb.active, years)
    _abos_sheet(store, wb.create_sheet("Verträge & Abos"))
    from ..check import match_spesen
    if match_spesen(store)["expenses"]:
        _spesen_sheet(store, wb.create_sheet("Spesen"))
    budget_path = Path("budget.yaml")
    if budget_path.exists():
        _budget_sheet(store, wb.create_sheet("Budget"), budget_path)
    _forecast_sheet(store, wb.create_sheet("Forecast"))
    lohn_ordner = sorted(Path("csv").glob("Lohndokumente*"))
    if lohn_ordner and budget_path.exists():
        import yaml
        basis = yaml.safe_load(budget_path.read_text(encoding="utf-8")).get(
            "est_basis_kv_pv", 0)
        _est_sheet(store, wb.create_sheet("ESt"), lohn_ordner[-1], basis)
    for y in years:
        _year_sheet(store, wb.create_sheet(y), y)
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path
