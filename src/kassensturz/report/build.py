"""German LaTeX/PDF reports.

Data comes straight from the Store; the template is Jinja2 with
LaTeX-friendly delimiters (((var))) / ((* block *)) so backslashes and
braces in TeX never fight the templating. Compilation uses latexmk with
LuaLaTeX (fontspec + variable-font Work Sans, falling back gracefully
if the font is missing is the template's job, not ours).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from ..model import fmt_eur
from ..store import Store, build_year_matrix

TEMPLATE_DIR = Path(__file__).parent / "templates"

_TEX_REPLACEMENTS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def tex_escape(value) -> str:
    s = str(value)
    return "".join(_TEX_REPLACEMENTS.get(ch, ch) for ch in s)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        block_start_string="((*",
        block_end_string="*))",
        variable_start_string="(((",
        variable_end_string=")))",
        comment_start_string="((=",
        comment_end_string="=))",
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["tex"] = tex_escape
    env.filters["eur"] = fmt_eur
    # bare German number, no currency suffix — for dense pivot tables
    env.filters["betrag"] = lambda cents: fmt_eur(cents).removesuffix(" €")
    return env


@dataclass
class ReportData:
    period_label: str
    created: str
    balances: list[dict] = field(default_factory=list)
    balance_total_cents: int = 0
    income_cents: int = 0
    expense_cents: int = 0
    special_cents: int = 0  # categories marked "(Sondereffekt)"
    categories: list[dict] = field(default_factory=list)
    # year pivot (full-period reports only): columns + rows
    years: list[str] = field(default_factory=list)
    year_rows: list[dict] = field(default_factory=list)
    focus: list[dict] = field(default_factory=list)  # {name, years:[{...}], total}
    uncategorized_count: int = 0
    tx_count: int = 0


# Categories that are money moving between own pockets, not income/expense.
# Kreditkarten-Abrechnung + PayPal-Funding sind seit der Konsolidierung
# neutral: die echten Käufe stehen itemisiert in mm-karte bzw. paypal.
NEUTRAL_CATEGORIES = (
    "Umbuchung",
    "Kreditkarten-Abrechnung",
    "PayPal-Funding (neutral)",
)

# Name marker for one-off effects (exits, inheritances, ...) that must not
# distort the running income/expense picture — reported on their own line.
SPECIAL_MARKER = "Sondereffekt"


def _period_label(year: int | None, month: int | None) -> str:
    if year and month:
        return f"{month:02d}/{year}"
    if year:
        return str(year)
    return "Gesamtzeitraum"


def _add_balances(store: Store, data: ReportData) -> None:
    for b in store.latest_balances():
        data.balances.append(
            {
                "account": b["account"],
                "date": b["snapshot_date"],
                "cents": b["balance_cents"],
                "currency": b["currency"],
            }
        )
        if b["currency"] == "EUR":
            data.balance_total_cents += b["balance_cents"]


def _add_summary(
    store: Store, data: ReportData, year: int | None, month: int | None
) -> None:
    for r in store.summary(year=year, month=month):
        data.categories.append(
            {
                "name": r["category"],
                "n": r["n"],
                "cents": r["total_cents"],
                "currency": r["currency"],
            }
        )
        data.tx_count += r["n"]
        if r["category"] == "(unkategorisiert)":
            data.uncategorized_count += r["n"]
        if r["category"] in NEUTRAL_CATEGORIES:
            continue
        if SPECIAL_MARKER in r["category"]:
            data.special_cents += r["total_cents"]
        elif r["total_cents"] >= 0:
            data.income_cents += r["total_cents"]
        else:
            data.expense_cents += r["total_cents"]


def _add_focus_histories(store: Store, data: ReportData, focus: list[str]) -> None:
    for name in focus:
        rows = store.conn.execute(
            """SELECT substr(booking_date,1,4) AS year,
                      COUNT(*) AS n, SUM(amount_cents) AS cents
               FROM transactions WHERE category = ?
               GROUP BY year ORDER BY year""",
            (name,),
        ).fetchall()
        if not rows:
            continue
        data.focus.append(
            {
                "name": name,
                "years": [
                    {"year": r["year"], "n": r["n"], "cents": r["cents"]}
                    for r in rows
                ],
                "total_cents": sum(r["cents"] for r in rows),
                "total_n": sum(r["n"] for r in rows),
            }
        )


def collect(
    store: Store,
    year: int | None = None,
    month: int | None = None,
    focus: list[str] | None = None,
) -> ReportData:
    data = ReportData(
        period_label=_period_label(year, month),
        created=date.today().strftime("%d.%m.%Y"),
    )
    _add_balances(store, data)
    _add_summary(store, data, year, month)
    if not year and not month:
        data.years, data.year_rows = build_year_matrix(store.summary_by_year())
    _add_focus_histories(store, data, focus or [])
    return data


def render(data: ReportData) -> str:
    return _env().get_template("bericht.tex.j2").render(d=data)


def collect_stichtag(store: Store, stichtag: str, label: str | None = None) -> dict:
    """Data for the historic net-worth report (see Store.balances_at)."""
    rows = store.balances_at(stichtag)
    y, m, d = stichtag.split("-")
    return {
        "stichtag": stichtag,
        "stichtag_de": f"{d}.{m}.{y}",
        "label": label,
        "created": date.today().strftime("%d.%m.%Y"),
        "rows": rows,
        "total_cents": sum(
            r["balance_cents"]
            for r in rows
            if r["status"] == "ok" and r["currency"] == "EUR"
        ),
        "snapshot_date": max((r["snapshot_date"] for r in rows), default="—"),
    }


def render_stichtag(data: dict) -> str:
    return _env().get_template("stichtag.tex.j2").render(d=data)


def collect_vertraege(store: Store, mit_inaktiven: bool = False,
                      today: date | None = None) -> dict:
    """Verträge & Abos gruppiert fürs PDF — Datenbasis wie Web-Seite/XLSX."""
    from .xlsx import GRUPPE_SPAREN, _abo_gruppe, _detect_abos, lade_gruppen
    abos = _detect_abos(store, today or date.today())
    if not mit_inaktiven:
        abos = [a for a in abos if a["aktiv"]]
    mapping = lade_gruppen(store)
    gruppen: dict[str, list] = {}
    for a in abos:
        gruppen.setdefault(_abo_gruppe(a["kategorie"], mapping), []).append(a)
    sortiert = sorted(
        gruppen.items(),
        key=lambda kv: -sum(x["jahr"] for x in kv[1] if x["aktiv"]))
    # Sparpläne sind Vermögensaufbau, keine Kosten — im Kopf getrennt
    # ausweisen (gleiche Semantik wie das XLSX-Verträge-Sheet)
    sparen = sum(a["jahr"] for name, es in sortiert if name == GRUPPE_SPAREN
                 for a in es if a["aktiv"])
    return {
        "created": (today or date.today()).strftime("%d.%m.%Y"),
        "inaktive": mit_inaktiven,
        "aktiv_n": sum(1 for a in abos if a["aktiv"]),
        "kosten_jahr": sum(a["jahr"] for a in abos if a["aktiv"]) - sparen,
        "sparen_jahr": sparen,
        "gruppen": [
            {
                "name": name,
                "aktiv_n": sum(1 for a in eintraege if a["aktiv"]),
                "jahr": sum(a["jahr"] for a in eintraege if a["aktiv"]),
                "eintraege": sorted(
                    eintraege, key=lambda a: (not a["aktiv"], -a["jahr"])),
            }
            for name, eintraege in sortiert
        ],
    }


def render_vertraege(data: dict) -> str:
    return _env().get_template("vertraege.tex.j2").render(d=data)


def spesen_monate(store: Store) -> list[str]:
    """Monate mit Spesen-Aktivität (Kandidaten oder Erstattungen)."""
    from ..check import _spesen_candidates, _spesen_reimbursements
    monate = {e["booking_date"][:7] for e in _spesen_candidates(store)}
    monate |= {r["booking_date"][:7] for r in _spesen_reimbursements(store)}
    return sorted(monate)


def collect_spesen_monat(store: Store, monat: str,
                         today: date | None = None) -> dict:
    """Monatssicht angefallen vs. beglichen — Basis für Web-Reiter + PDF.

    Das Matching läuft global (Erstattungen dürfen bis 90 Tage nach dem
    Beleg liegen, also monatsübergreifend); gefiltert wird nur die Anzeige.
    """
    from ..check import _spesen_reimbursements, match_spesen
    result = match_spesen(store, today=today)
    belege = [e for e in result["expenses"]
              if e["booking_date"][:7] == monat]
    beglichen = [e for e in belege if e["matched"]]
    akzeptiert = [e for e in belege if e.get("akzeptiert")]
    offen = [e for e in belege
             if not e["matched"] and not e.get("akzeptiert")]
    eingaenge = [r for r in _spesen_reimbursements(store)
                 if r["booking_date"][:7] == monat]
    return {
        "monat": monat,
        "created": (today or date.today()).strftime("%d.%m.%Y"),
        "belege": belege,
        "angefallen": -sum(e["amount_cents"] for e in belege),
        "beglichen": -sum(e["amount_cents"] for e in beglichen),
        "offen": -sum(e["amount_cents"] for e in offen),
        "n_offen": len(offen),
        "akzeptiert_summe": -sum(e["amount_cents"] for e in akzeptiert),
        "n_akzeptiert": len(akzeptiert),
        "eingaenge": eingaenge,
        "eingang_summe": sum(r["amount_cents"] for r in eingaenge),
        "surplus": result["surplus"],  # Sammelreport-Überhang (global)
    }


def render_spesen(data: dict) -> str:
    return _env().get_template("spesen.tex.j2").render(d=data)


def build_pdf(tex_source: str, out_path: Path) -> Path:
    """Compile TeX → PDF via latexmk/LuaLaTeX; returns the PDF path."""
    latexmk = shutil.which("latexmk")
    if latexmk is None:
        raise RuntimeError(
            "latexmk not found — install TeX Live (or MacTeX) to build PDFs."
        )
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kassensturz-tex-") as tmp:
        tex_file = Path(tmp) / "bericht.tex"
        tex_file.write_text(tex_source, encoding="utf-8")
        result = subprocess.run(
            [latexmk, "-lualatex", "-no-shell-escape",
             "-interaction=nonstopmode", "-halt-on-error", tex_file.name],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        pdf = Path(tmp) / "bericht.pdf"
        if result.returncode != 0 or not pdf.exists():
            tail = "\n".join(result.stdout.splitlines()[-30:])
            raise RuntimeError(f"latexmk failed:\n{tail}")
        shutil.copy2(pdf, out_path)
    return out_path


def collect_cfo(store: Store, today: date | None = None) -> dict:
    """CFO-Monatsbericht: Executive Summary, Vermögensbilanz, Cashflow,
    Integritäts-Prüfungen — Family-Office-Dreiklang auf dem Hauptbuch."""
    import re as _re

    from ..check import run_all
    today = today or date.today()
    ym = f"{today:%Y-%m}"
    monat_de = ("Januar Februar März April Mai Juni Juli August September "
                "Oktober November Dezember").split()[today.month - 1]
    neutral = ",".join(f"'{c}'" for c in NEUTRAL_CATEGORIES)
    basis = (f"COALESCE(category,'') NOT IN ({neutral}) "
             "AND COALESCE(category,'') NOT LIKE '%(Sondereffekt)%'")

    # Vermögen: Stichtag & Monatsbeginn (rückgerechnet)
    ende_rows = [b for b in store.latest_balances() if b["currency"] == "EUR"]
    verm_ende = sum(b["balance_cents"] for b in ende_rows)
    anfang_map = {r["account"]: r for r in
                  store.balances_at(f"{ym}-01")}
    konten, verm_anfang = [], 0
    for b in sorted(ende_rows, key=lambda x: -x["balance_cents"]):
        a = anfang_map.get(b["account"])
        ok = a and a["status"] == "ok"
        anfang = a["balance_cents"] if ok else b["balance_cents"]
        verm_anfang += anfang
        konten.append({
            "account": b["account"],
            "hinweis": "" if ok else "nur Saldo-Snapshot",
            "anfang": anfang, "ende": b["balance_cents"],
            "delta": b["balance_cents"] - anfang,
        })

    # Monats-KPIs + 12-Monats-Saldi
    saldi = [dict(r) for r in store.conn.execute(
        f"""SELECT substr(booking_date,1,7) ym,
               SUM(CASE WHEN amount_cents>0 THEN amount_cents ELSE 0 END) ein,
               SUM(CASE WHEN amount_cents<0 THEN amount_cents ELSE 0 END) aus
            FROM transactions WHERE {basis}
            GROUP BY 1 ORDER BY 1 DESC LIMIT 12""")][::-1]
    maxv = max((abs(r["ein"] + r["aus"]) for r in saldi), default=1) or 1
    balken = [{"ym": r["ym"], "pos": r["ein"] + r["aus"] >= 0,
               "mm": max(1, round(abs(r["ein"] + r["aus"]) * 24 / maxv)),
               "offen": r["ym"] == ym} for r in saldi]
    m = next((r for r in saldi if r["ym"] == ym), {"ein": 0, "aus": 0})
    volle = [r for r in saldi if r["ym"] != ym][-6:]
    ausgaben_avg6 = (round(sum(-r["aus"] for r in volle) / len(volle))
                     if volle else 0)

    # Gehalts-Projektion (Median der letzten 3 Läufe)
    kat = store.setting_get("gehalt_kategorie", "Gehalt")
    roh = store.setting_get("gehalt_erwartet", "").replace(",", ".").strip()
    try:
        erwartet = int(round(float(roh) * 100))
    except ValueError:
        vals = sorted(r[0] for r in store.conn.execute(
            """SELECT SUM(amount_cents) FROM transactions
               WHERE category = ? AND amount_cents > 0
                 AND substr(booking_date,1,7) < ?
               GROUP BY substr(booking_date,1,7)
               ORDER BY substr(booking_date,1,7) DESC LIMIT 3""",
            (kat, ym)).fetchall())
        erwartet = vals[len(vals) // 2] if vals else 0
    gebucht = store.conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM transactions "
        "WHERE category = ? AND amount_cents > 0 "
        "AND substr(booking_date,1,7) = ?", (kat, ym)).fetchone()[0]
    ausstehend = erwartet if gebucht == 0 else 0
    tage = sorted(int(r[0][8:10]) for r in store.conn.execute(
        "SELECT booking_date FROM transactions WHERE category = ? "
        "AND amount_cents > 0 ORDER BY booking_date DESC LIMIT 3", (kat,)))
    gehalt_tag = (f"~{tage[len(tage)//2]:02d}.{today.month:02d}."
                  if tage and ausstehend else "—")

    # Cashflow-Tabelle (letzte 4 Monate) + Top-Bewegungen
    cashflow = [{"ym": r["ym"], "ein": r["ein"], "aus": r["aus"],
                 "saldo": r["ein"] + r["aus"], "offen": r["ym"] == ym}
                for r in saldi[-4:]]
    aufl = _re.compile(r"[\w.\- ]*-Auflösung:\s*(.+)$")
    top = []
    for r in store.conn.execute(
            f"""SELECT booking_date, counterparty, purpose, amount_cents
                FROM transactions WHERE {basis} AND amount_cents < 0
                AND substr(booking_date,1,7) = ?
                ORDER BY amount_cents LIMIT 5""", (ym,)):
        mm_ = aufl.search(r["purpose"] or "")
        # IBAN-Präfixe der FinTS-Texte verschandeln den Bericht
        cp = _re.sub(r"^[A-Z]{2}\d{20}", "", r["counterparty"]).strip()
        text = (f"{mm_.group(1).strip()} · via {cp}" if mm_ else cp)
        top.append({"datum": f"{r['booking_date'][8:10]}.{r['booking_date'][5:7]}.",
                    "text": text, "betrag": r["amount_cents"]})

    # Integrität + Quellen + Spesen + Verträge
    pruefungen = [{"level": f.level, "check": f.check, "message": f.message}
                  for f in run_all(store, today=today)]
    quellen = []
    for r in store.conn.execute(
            """SELECT account, COUNT(*) n, MIN(booking_date) von,
                      MAX(booking_date) bis,
                      GROUP_CONCAT(DISTINCT substr(source,1,instr(source,':')-1)) q
               FROM transactions GROUP BY account ORDER BY account"""):
        quellen.append({"account": r["account"], "n": r["n"],
                        "von": r["von"], "bis": r["bis"],
                        "quelle": r["q"] or "—"})
    sp = collect_spesen_monat(store, ym, today=today)
    v = collect_vertraege(store, today=today)
    n_monat = store.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE substr(booking_date,1,7)=?",
        (ym,)).fetchone()[0]

    saldo = m["ein"] + m["aus"]
    warns = [p for p in pruefungen if p["level"] == "warn"]
    schnitt6 = (round(sum(r["ein"] + r["aus"] for r in volle) / len(volle))
                if volle else 0)
    kernaussagen = [
        f"Monatssaldo projiziert {fmt_eur(saldo + ausstehend)} — der "
        f"Gehaltslauf ({fmt_eur(erwartet)}) wird {gehalt_tag} erwartet.",
        f"Durchschnittssaldo der letzten sechs vollen Monate: "
        f"{fmt_eur(schnitt6)}/Monat.",
        (f"Größte Einzelbewegung im Monat: {top[0]['text'][:60]} "
         f"({fmt_eur(top[0]['betrag'])})." if top else
         "Keine Ausgaben im Berichtsmonat."),
        ("Alle Integritäts-Prüfungen bestanden — Salden bankbestätigt, "
         "keine offenen Abstimmungsposten."
         if not warns else
         f"{len(warns)} Prüfung(en) mit Warnung — Details Seite 3."),
    ]

    return {
        "monat_de": f"{monat_de} {today.year}", "monat_kurz": f"{today.month:02d}/{today.year}",
        "created": today.strftime("%d.%m.%Y"),
        "datenstand": max((b["snapshot_date"] for b in ende_rows), default="—"),
        "verm_ende": verm_ende, "verm_anfang": verm_anfang,
        "verm_delta": verm_ende - verm_anfang,
        "n_monat": n_monat, "n_konten": len(ende_rows),
        "saldo": saldo, "saldo_proj": saldo + ausstehend,
        "ein_proj": m["ein"] + ausstehend, "ausgaben": -m["aus"],
        "ausgaben_avg6": ausgaben_avg6, "gehalt_tag": gehalt_tag,
        "kosten_jahr": v["kosten_jahr"], "sparen_jahr": v["sparen_jahr"],
        "balken": balken, "kernaussagen": kernaussagen,
        "konten": konten, "cashflow": cashflow, "top_bewegungen": top,
        "sp_angefallen": sp["angefallen"], "sp_beglichen": sp["beglichen"],
        "sp_offen": sp["offen"],
        "pruefungen": pruefungen, "quellen": quellen,
    }


def render_cfo(data: dict) -> str:
    return _env().get_template("cfo.tex.j2").render(d=data)
