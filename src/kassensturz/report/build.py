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
NEUTRAL_CATEGORIES = ("Umbuchung",)

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
            [latexmk, "-lualatex", "-interaction=nonstopmode", "-halt-on-error",
             tex_file.name],
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
