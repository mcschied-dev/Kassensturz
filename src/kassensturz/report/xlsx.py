"""XLSX-Workbook (LibreOffice-tauglich): Dashboard + ein Sheet pro Jahr.

Erzeugt den Bericht IMMER frisch aus der Datenbank — das Workbook ist ein
Artefakt, keine Datenhaltung. Nach Sync/Import einfach neu generieren.
Konsolidierungs-Semantik wie im LaTeX-Report: NEUTRAL_CATEGORIES zählen
nicht, "(Sondereffekt)"-Kategorien laufen als eigene Zeile.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
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
    row = _kpis(ws, 4, [
        ("Einnahmen", inc / 100),
        ("Ausgaben", exp / 100),
        ("Saldo laufend", (inc + exp) / 100),
        ("Sondereffekte", special / 100),
        ("Buchungen", n),
    ])

    # Monatsverlauf
    row = _section(ws, row + 1, "Monatsverlauf")
    row += 1
    head = row
    _table_header(ws, row, 2, ["Monat", "Einnahmen", "Ausgaben", "Saldo"])
    for m in range(1, 13):
        i, e = _income_expense(store, year, f"{m:02d}")
        row += 1
        _cell(ws, row, 2, MONTHS[m - 1])
        _cell(ws, row, 3, i / 100, EUR_FMT)
        _cell(ws, row, 4, -e / 100, EUR_FMT)  # Ausgaben positiv fürs Chart
        _cell(ws, row, 5, (i + e) / 100, EUR_FMT)

    chart = BarChart()
    chart.type = "col"
    chart.title = f"Einnahmen vs. Ausgaben {year}"
    chart.height, chart.width = 8, 16
    chart.add_data(Reference(ws, min_col=3, max_col=4, min_row=head, max_row=row),
                   titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=2, min_row=head + 1, max_row=row))
    chart.gapWidth = 60
    chart.legend.position = "b"
    ws.add_chart(chart, f"G{head}")

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
# Gruppierung nach (normalisiertem Anbieter, Betrag) — so werden mehrere
# Apple-/Google-Abos mit unterschiedlichen Beträgen einzeln erkannt und
# Preiserhöhungen als "alte Linie beendet / neue aktiv" sichtbar.

_TURNUS = (  # (min Tage, max Tage, Label, Zahlungen pro Jahr)
    (24, 38, "monatlich", 12),
    (52, 75, "2-monatlich", 6),
    (80, 105, "quartalsweise", 4),
    (165, 205, "halbjährlich", 2),
    (330, 420, "jährlich", 1),
)


def _abo_key(counterparty: str) -> str:
    import re as _re

    s = _re.sub(r"[^a-zäöüß ]", " ", counterparty.lower())
    tokens = [t for t in s.split() if len(t) > 2][:2]
    return " ".join(tokens)


def _detect_abos(store: Store, today: date) -> list[dict]:
    from statistics import median, pstdev

    groups: dict[tuple[str, int], list] = {}
    for r in _q(
        store,
        "SELECT counterparty, COALESCE(category,'(unkategorisiert)') c,"
        "       booking_date, amount_cents "
        f"FROM transactions WHERE amount_cents < 0 {_BASE} "
        "ORDER BY booking_date",
    ):
        key = _abo_key(r["counterparty"])
        if key:
            groups.setdefault((key, r["amount_cents"]), []).append(r)

    abos = []
    for (_key, amount), rows in groups.items():
        if len(rows) < 3:
            continue
        dates = [date.fromisoformat(r["booking_date"]) for r in rows]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        med = median(gaps)
        turnus = next((t for t in _TURNUS if t[0] <= med <= t[1]), None)
        # Abos sind regelmäßig — unruhige Intervalle sind Einkaufsmuster
        if turnus is None or (len(gaps) > 1 and pstdev(gaps) > 0.35 * med):
            continue
        active = (today - dates[-1]).days <= med * 1.8
        import re as _re

        # FinTS klebt die IBAN vor den Namen — fürs Auge entfernen
        anbieter = _re.sub(r"^DE\d{20}", "", rows[-1]["counterparty"]).strip()
        abos.append({
            "anbieter": anbieter[:38],
            "kategorie": rows[-1]["c"],
            "turnus": turnus[2],
            "betrag": -amount,
            "jahr": -amount * turnus[3],
            "seit": dates[0].isoformat(),
            "letzte": dates[-1].isoformat(),
            "n": len(rows),
            "aktiv": active,
        })
    abos.sort(key=lambda a: (not a["aktiv"], -a["jahr"]))
    return abos


def _abos_sheet(store: Store, ws) -> None:
    ws.sheet_view.showGridLines = False
    _widths(ws, {1: 2, 2: 36, 3: 22, 4: 13, 5: 12, 6: 13, 7: 11, 8: 11, 9: 10})
    ws.cell(row=2, column=2, value="Verträge & Abos").font = F_TITLE
    ws.cell(row=3, column=2,
            value="automatisch erkannt: ≥3 Zahlungen, gleicher Betrag, "
                  "regelmäßiger Rhythmus — kein Anspruch auf Vollständigkeit "
                  "bei schwankenden Beträgen (z. B. verbrauchsabhängige "
                  "Tarife)").font = F_DIM

    today = date.today()
    abos = _detect_abos(store, today)
    aktiv_jahr = sum(a["jahr"] for a in abos if a["aktiv"])
    row = _kpis(ws, 5, [
        ("Aktive Verträge/Abos", sum(1 for a in abos if a["aktiv"])),
        ("Kosten pro Jahr (aktiv)", aktiv_jahr / 100),
        ("davon monatlich gebunden",
         sum(a["betrag"] for a in abos if a["aktiv"] and a["turnus"] == "monatlich")
         / 100),
    ])
    _table_header(ws, row, 2, ["Anbieter", "Kategorie", "Turnus", "Betrag",
                               "Kosten/Jahr", "seit", "letzte", "Status"])
    for a in abos:
        row += 1
        _cell(ws, row, 2, a["anbieter"], bold=a["aktiv"])
        _cell(ws, row, 3, a["kategorie"])
        _cell(ws, row, 4, f"{a['turnus']} ({a['n']}×)")
        _cell(ws, row, 5, a["betrag"] / 100, EUR_FMT)
        _cell(ws, row, 6, a["jahr"] / 100, EUR_FMT, bold=a["aktiv"])
        _cell(ws, row, 7, a["seit"])
        _cell(ws, row, 8, a["letzte"])
        c = _cell(ws, row, 9, "aktiv" if a["aktiv"] else "beendet")
        c.font = Font(size=9, bold=a["aktiv"],
                      color="1A7F37" if a["aktiv"] else "999999")
    ws.freeze_panes = "A2"


def build_workbook(store: Store, out_path: Path, first_year: str = "2022") -> Path:
    years = _years(store, first_year)
    if not years:
        raise ValueError(f"keine Buchungen ab {first_year} in der Datenbank")
    wb = Workbook()
    _dashboard(store, wb.active, years)
    _abos_sheet(store, wb.create_sheet("Verträge & Abos"))
    for y in years:
        _year_sheet(store, wb.create_sheet(y), y)
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path
