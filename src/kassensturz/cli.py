"""kassensturz CLI — typer app.

Chat integration works exactly like the spark CLI: an AI agent (or you)
calls these commands; all data stays in a local SQLite file.
"""

from __future__ import annotations

import csv as csv_module
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .categorize import apply_rules, load_rules, match_transfers
from .config import DEFAULT_CONFIG, load_config
from .connectors.csv_import import PROFILES, import_csv
from .model import fmt_eur
from .store import Store, build_year_matrix

app = typer.Typer(
    name="kassensturz",
    help="Local-first personal finance reporting for German banks.",
    no_args_is_help=True,
)
console = Console()

DEFAULT_DB = Path(
    os.environ.get(
        "KASSENSTURZ_DB", Path.home() / ".kassensturz" / "kassensturz.db"
    )
)

DbOption = typer.Option(DEFAULT_DB, "--db", help="Path to the SQLite database.")


def _store(db: Path) -> Store:
    return Store(db)


@app.command()
def version() -> None:
    """Print version."""
    console.print(f"kassensturz {__version__}")


def _secret_cmd(connection: str, delete: bool, show: bool) -> None:
    from .connectors import fints_client

    if delete:
        fints_client.delete_pin(connection)
        console.print(f"[yellow]Zugangsdaten für '{connection}' gelöscht.[/yellow]")
        return
    value = typer.prompt(
        f"PIN/Passwort für '{connection}'", hide_input=not show
    )
    fints_client.set_pin(connection, value)
    console.print(
        f"[green]Zugangsdaten für '{connection}' im Schlüsselbund gespeichert.[/green]"
    )


ShowOption = typer.Option(
    False, "--show", help="Show the input while typing (default: hidden)."
)


@app.command()
def pin(
    connection: str = typer.Argument(..., help="Connection name from config.yaml."),
    delete: bool = typer.Option(False, "--delete", help="Remove the stored secret."),
    show: bool = ShowOption,
) -> None:
    """Store (or remove) a banking PIN/password in the system keychain."""
    _secret_cmd(connection, delete, show)


@app.command("password", hidden=True)
def password(
    connection: str = typer.Argument(..., help="Connection name from config.yaml."),
    delete: bool = typer.Option(False, "--delete", help="Remove the stored secret."),
    show: bool = ShowOption,
) -> None:
    """Alias for `pin` — DKB & friends use a password, not a PIN."""
    _secret_cmd(connection, delete, show)


class TanRequired(Exception):
    """TAN/2FA nötig, aber keine interaktive Eingabe möglich (Rundruf)."""


def _sync_error_hint(e: Exception, conn_name: str) -> str:
    msg = str(e).lower()
    if "locked" in msg:
        return "Zugang gesperrt — Webbanking entsperren, sonst 24 h warten"
    if isinstance(e, TanRequired):
        return f"2FA nötig → interaktiv: kassensturz sync -c {conn_name}"
    return type(e).__name__


def _print_rundruf_table(results: list[tuple]) -> None:
    table = Table(title="Kontenrundruf")
    for col in ("Bank", "Status", "Neu", "Duplikate", "Salden", "Hinweis"):
        table.add_column(col)
    for name, status, neu, dup, salden, hint in results:
        color = "green" if status == "✓" else "red"
        table.add_row(
            name, f"[{color}]{status}[/{color}]", str(neu), str(dup), salden, hint
        )
    console.print(table)


def _tan_callback(response) -> str:
    """Interactive TAN entry; decoupled = confirm in app, then Enter."""
    try:
        if getattr(response, "decoupled", False):
            typer.prompt(
                f"→ In der Banking-App bestätigen: {response.challenge or 'Freigabe'}"
                " — danach Enter",
                default="",
                show_default=False,
            )
            return ""
        return typer.prompt(f"TAN ({response.challenge or 'siehe Gerät'})")
    except (EOFError, typer.Abort) as e:
        raise TanRequired(
            "TAN-Eingabe erforderlich — bitte einmal interaktiv im Terminal "
            "ausführen (kassensturz sync -c <name>)."
        ) from e


def _get_pin_or_prompt(name: str) -> str:
    from .connectors import fints_client

    stored = fints_client.get_pin(name)
    if stored is not None:
        return stored
    console.print(
        f"[yellow]Keine Zugangsdaten für '{name}' im Schlüsselbund"
        f" — `kassensturz pin {name}`[/yellow]"
    )
    try:
        return typer.prompt(f"PIN/Passwort für '{name}'", hide_input=True)
    except (EOFError, typer.Abort) as e:
        raise TanRequired(
            f"Keine Zugangsdaten für '{name}' — `kassensturz pin {name}` ausführen."
        ) from e


def _sync_one(conn, conn_pin: str, start, end):
    """Dispatch one connection to the right connector."""
    from .config import DkbConnection, TradeRepublicConnection, WiseConnection
    from .connectors import fints_client
    from .connectors.dkb import sync_dkb
    from .connectors.traderepublic import sync_traderepublic
    from .connectors.wise import sync_wise

    if isinstance(conn, DkbConnection):
        console.print("  [dim]→ Anmeldung in der DKB-App bestätigen …[/dim]")
        return sync_dkb(conn, conn_pin, start, end)
    if isinstance(conn, WiseConnection):
        return sync_wise(conn, conn_pin, start, end)
    if isinstance(conn, TradeRepublicConnection):
        console.print("  [dim]→ Trade Republic: ggf. 2FA-Code aus App/SMS …[/dim]")
        return sync_traderepublic(conn, conn_pin, start, end)
    return fints_client.sync_connection(conn, conn_pin, start, end, _tan_callback)


def _require_product_id(conn) -> None:
    from .config import FintsConnection

    if isinstance(conn, FintsConnection) and not conn.product_id:
        console.print(
            f"[red]'{conn.name}': product_id fehlt in der Config.[/red]\n"
            "FinTS verlangt eine bei der Deutschen Kreditwirtschaft "
            "registrierte Produkt-ID (kostenlos):\n"
            "  https://www.fints.org/de/hersteller/produktregistrierung\n"
            "Bis sie da ist: CSV-Export + `kassensturz import-csv` nutzen."
        )
        raise typer.Exit(1)


@app.command()
def sync(
    connection: Optional[str] = typer.Option(
        None, "--connection", "-c", help="Only this connection (default: all)."
    ),
    days: int = typer.Option(
        80, "--days", "-d", help="How many days back (>90 usually triggers SCA)."
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", help=f"Config file (default: {DEFAULT_CONFIG})."
    ),
    db: Path = DbOption,
) -> None:
    """Sync transactions from the bank (FinTS/HBCI or DKB web API)."""
    from datetime import date, timedelta

    config = load_config(config_path)
    conns = [config.connection(connection)] if connection else config.all_connections
    if not conns:
        console.print(
            f"[red]Keine Bankverbindungen konfiguriert.[/red] "
            f"Siehe config.example.yaml → {DEFAULT_CONFIG}"
        )
        raise typer.Exit(1)

    store = _store(db)
    start = date.today() - timedelta(days=days)
    today = date.today().isoformat()
    results: list[tuple] = []  # (name, status, neu, dup, salden, hinweis)
    for conn in conns:
        console.print(f"[cyan]Sync {conn.name}[/cyan] (ab {start.isoformat()}) …")
        try:
            _require_product_id(conn)
            txs, skipped, balances = _sync_one(
                conn, _get_pin_or_prompt(conn.name), start, date.today()
            )
        except Exception as e:  # noqa: BLE001 — Banken werfen bunt; isolieren
            console.print(f"  [red]fehlgeschlagen:[/red] {str(e)[:160]}")
            results.append((conn.name, "✗", 0, 0, "", _sync_error_hint(e, conn.name)))
            continue
        inserted, dup = store.add(txs)
        store.record_balances(
            [(label, today, cents, cur, conn.name) for label, cents, cur in balances]
        )
        for number in skipped:
            console.print(f"  [dim]übersprungen: {number}[/dim]")
        salden = " · ".join(fmt_eur(c, cur) for _l, c, cur in balances)
        results.append((conn.name, "✓", inserted, dup, salden, ""))

    _print_rundruf_table(results)
    if all(r[1] == "✗" for r in results):
        raise typer.Exit(1)


@app.command("import-csv")
def import_csv_cmd(
    path: Path = typer.Argument(..., exists=True, help="CSV export from your bank."),
    profile: str = typer.Option(
        ..., "--profile", "-p", help=f"Format profile: {', '.join(PROFILES)}"
    ),
    account: str = typer.Option(
        ..., "--account", "-a", help='Label for the own account, e.g. "dkb-giro".'
    ),
    db: Path = DbOption,
) -> None:
    """Import a bank CSV export. Re-importing overlapping files is safe (dedup)."""
    from dataclasses import replace

    # canonical flow: rows enter uncategorized; YAML rules categorize,
    # bank categories only via the explicit `adopt-categories` command
    txs = [replace(t, category=None) for t in import_csv(path, profile, account)]
    store = _store(db)
    inserted, skipped = store.add(txs)
    console.print(
        f"[green]{inserted} importiert[/green], "
        f"[yellow]{skipped} Duplikate übersprungen[/yellow] "
        f"({path.name} → {account})"
    )


@app.command("list")
def list_cmd(
    account: Optional[str] = typer.Option(None, "--account", "-a"),
    year: Optional[int] = typer.Option(None, "--year", "-y"),
    month: Optional[int] = typer.Option(None, "--month", "-m"),
    category: Optional[str] = typer.Option(None, "--category", "-c"),
    uncategorized: bool = typer.Option(False, "--uncategorized", "-u"),
    limit: int = typer.Option(50, "--limit", "-n"),
    db: Path = DbOption,
) -> None:
    """List transactions (newest first)."""
    rows = _store(db).query(
        account=account,
        year=year,
        month=month,
        category=category,
        uncategorized=uncategorized,
        limit=limit,
    )
    table = Table(title=f"{len(rows)} Transaktionen")
    for col in ("Datum", "Konto", "Gegenpartei", "Zweck", "Kategorie"):
        table.add_column(col)
    table.add_column("Betrag", justify="right")
    for r in rows:
        table.add_row(
            r["booking_date"],
            r["account"],
            r["counterparty"][:32],
            r["purpose"][:40],
            r["category"] or "[dim]—[/dim]",
            fmt_eur(r["amount_cents"], r["currency"]),
        )
    console.print(table)


@app.command("adopt-categories")
def adopt_categories_cmd(
    path: Path = typer.Argument(..., exists=True, help="CSV mit Kategorie-Spalte."),
    profile: str = typer.Option(..., "--profile", "-p"),
    account: str = typer.Option(..., "--account", "-a"),
    db: Path = DbOption,
) -> None:
    """Bank-/App-Kategorien als Fallback übernehmen (nur für unkategorisierte)."""
    txs = import_csv(path, profile, account)
    updated = _store(db).adopt_categories(txs)
    with_cat = sum(1 for t in txs if t.category)
    console.print(
        f"[green]{updated} Buchungen kategorisiert[/green] "
        f"({with_cat} Kategorien in {path.name}; Regeln behalten Vorrang)"
    )


@app.command()
def categorize(
    rules: Path = typer.Option(
        Path("rules.yaml"), "--rules", "-r", exists=True, help="YAML rule file."
    ),
    transfers: bool = typer.Option(
        True, help="Also pair internal transfers between own accounts."
    ),
    db: Path = DbOption,
) -> None:
    """Apply category rules to all uncategorized transactions."""
    store = _store(db)
    ruleset = load_rules(rules)
    n_transfers = match_transfers(store, ruleset) if transfers else 0
    counts = apply_rules(store, ruleset)
    if n_transfers:
        console.print(f"[cyan]{ruleset.transfer_category}[/cyan]: {n_transfers}")
    for cat, n in sorted(counts.items()):
        console.print(f"[cyan]{cat}[/cyan]: {n}")
    remaining = len(store.query(uncategorized=True))
    console.print(f"[dim]{remaining} unkategorisiert[/dim]")


@app.command()
def check(
    db: Path = DbOption,
) -> None:
    """Sanity-Check: Salden, Deckung der Neutral-Buchungen, Lücken.

    Nach jedem Import/Sync ausführen — Exit-Code 1 bei Warnungen.
    """
    from .check import run_all

    store = _store(db)
    findings = run_all(store)
    icons = {"ok": "[green]✓[/green]", "info": "[cyan]i[/cyan]", "warn": "[red]![/red]"}
    for f in findings:
        console.print(f"{icons[f.level]} [dim]{f.check}[/dim] {f.message}")
    warns = sum(1 for f in findings if f.level == "warn")
    if warns:
        console.print(f"[red]{warns} Warnung(en) — Zahlen sind so NICHT belastbar.[/red]")
        raise typer.Exit(1)
    console.print("[green]Alle Invarianten halten.[/green]")


@app.command()
def summary(
    year: Optional[int] = typer.Option(None, "--year", "-y"),
    month: Optional[int] = typer.Option(None, "--month", "-m"),
    by_year: bool = typer.Option(
        False, "--by-year", help="Kategorien × Jahre als Pivot-Tabelle."
    ),
    db: Path = DbOption,
) -> None:
    """Sum per category (optionally for one year/month, or pivoted by year)."""
    store = _store(db)
    if by_year:
        years, rows = build_year_matrix(store.summary_by_year())
        table = Table(title="Kassensturz — Kategorien nach Jahren (EUR)")
        table.add_column("Kategorie")
        for y in years:
            table.add_column(y, justify="right")
        table.add_column("Gesamt", justify="right")
        for row in rows:
            cells = [
                fmt_eur(row["cells"][y]).removesuffix(" €") if y in row["cells"] else ""
                for y in years
            ]
            table.add_row(
                row["name"], *cells, fmt_eur(row["total"]).removesuffix(" €")
            )
        console.print(table)
        return

    if year and month:
        period = f"{year}-{month:02d}"
    elif year:
        period = str(year)
    else:
        period = "gesamt"
    table = Table(title=f"Kassensturz {period}")
    table.add_column("Kategorie")
    table.add_column("Buchungen", justify="right")
    table.add_column("Summe", justify="right")
    for r in _store(db).summary(year=year, month=month):
        table.add_row(
            r["category"], str(r["n"]), fmt_eur(r["total_cents"], r["currency"])
        )
    console.print(table)


@app.command()
def balance(
    account: str = typer.Argument(..., help='Kontolabel, z. B. "ksk-giro".'),
    amount: str = typer.Argument(..., help='Kontostand, z. B. "12.345,67".'),
    snapshot_date: Optional[str] = typer.Option(
        None, "--date", help="YYYY-MM-DD (Default: heute)."
    ),
    currency: str = typer.Option("EUR", "--currency"),
    db: Path = DbOption,
) -> None:
    """Kontostand manuell erfassen — für CSV-Konten ohne Sync (z. B. KSK)."""
    from datetime import date

    from .connectors.csv_import import parse_german_decimal

    cents = int((parse_german_decimal(amount) * 100).to_integral_value())
    day = snapshot_date or date.today().isoformat()
    _store(db).record_balances([(account, day, cents, currency, "manual")])
    console.print(f"[green]{account}: {fmt_eur(cents, currency)} per {day}[/green]")


@app.command()
def accounts(db: Path = DbOption) -> None:
    """Overview of all imported accounts and latest known balances."""
    store = _store(db)
    table = Table(title="Konten")
    for col in ("Konto", "Buchungen", "Von", "Bis"):
        table.add_column(col)
    table.add_column("Umsatzsumme (importiert)", justify="right")
    for r in store.accounts_overview():
        table.add_row(
            r["account"],
            str(r["n"]),
            r["first_date"],
            r["last_date"],
            fmt_eur(r["total_cents"], r["currency"]),
        )
    console.print(table)

    balances = store.latest_balances()
    if balances:
        btable = Table(title="Kontostände (letzter Sync)")
        btable.add_column("Konto")
        btable.add_column("Stand vom")
        btable.add_column("Saldo", justify="right")
        total = 0
        for b in balances:
            btable.add_row(
                b["account"],
                b["snapshot_date"],
                fmt_eur(b["balance_cents"], b["currency"]),
            )
            if b["currency"] == "EUR":
                total += b["balance_cents"]
        btable.add_row("[bold]Gesamt (EUR)[/bold]", "", f"[bold]{fmt_eur(total)}[/bold]")
        console.print(btable)


@app.command()
def report(
    year: Optional[int] = typer.Option(None, "--year", "-y"),
    month: Optional[int] = typer.Option(None, "--month", "-m"),
    focus: Optional[list[str]] = typer.Option(
        None, "--focus", "-f",
        help="Kategorie(n) mit eigener Jahres-Historie im Bericht. Mehrfach nutzbar.",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="PDF-Zielpfad (Default: reports/…)."
    ),
    db: Path = DbOption,
) -> None:
    """Deutschen PDF-Bericht erzeugen (LuaLaTeX, Work Sans)."""
    from .report.build import build_pdf, collect, render

    store = _store(db)
    data = collect(store, year=year, month=month, focus=focus)
    if out is None:
        if year and month:
            stem = f"kassensturz-{year}-{month:02d}"
        elif year:
            stem = f"kassensturz-{year}"
        else:
            stem = "kassensturz-gesamt"
        out = Path("reports") / f"{stem}.pdf"
    pdf = build_pdf(render(data), out)
    console.print(f"[green]Bericht: {pdf}[/green]")


@app.command()
def stichtag(
    datum: str = typer.Argument(..., help="Stichtag YYYY-MM-DD, z. B. Trennungstag."),
    label: Optional[str] = typer.Option(
        None, "--label", "-l", help='Bezeichnung, z. B. "Trennungsvermögen".'
    ),
    pdf: bool = typer.Option(False, "--pdf", help="Zusätzlich PDF-Bericht erzeugen."),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    db: Path = DbOption,
) -> None:
    """Vermögensübersicht zu einem historischen Stichtag (rückgerechnet)."""
    from datetime import date as _date

    _date.fromisoformat(datum)  # fail early on bad input
    store = _store(db)
    from .report.build import build_pdf, collect_stichtag, render_stichtag

    data = collect_stichtag(store, datum, label)
    table = Table(title=f"Vermögen zum {data['stichtag_de']}"
                  + (f" — {label}" if label else ""))
    table.add_column("Konto")
    table.add_column("Saldo", justify="right")
    table.add_column("Datenbasis")
    for r in data["rows"]:
        if r["status"] == "ok":
            table.add_row(
                r["account"],
                fmt_eur(r["balance_cents"], r["currency"]),
                f"Umsätze ab {r['data_from']}",
            )
        elif r["status"] == "nicht_existent":
            table.add_row(r["account"], "—", "Konto bestand noch nicht")
        else:
            table.add_row(r["account"], "unbekannt", f"Umsätze erst ab {r['data_from']}")
    table.add_row("[bold]Gesamt (EUR)[/bold]", f"[bold]{fmt_eur(data['total_cents'])}[/bold]", "")
    console.print(table)

    if pdf:
        if out is None:
            out = Path("reports") / f"stichtag-{datum}.pdf"
        pdf_path = build_pdf(render_stichtag(data), out)
        console.print(f"[green]Bericht: {pdf_path}[/green]")


@app.command("export-csv")
def export_csv_cmd(
    out: Path = typer.Argument(..., help="Output CSV path (UTF-8, comma-separated)."),
    year: Optional[int] = typer.Option(None, "--year", "-y"),
    db: Path = DbOption,
) -> None:
    """Export canonical transactions as CSV (for Excel & friends)."""
    rows = _store(db).query(year=year)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv_module.writer(f)
        writer.writerow(
            [
                "booking_date",
                "value_date",
                "amount",
                "currency",
                "account",
                "counterparty",
                "purpose",
                "booking_type",
                "category",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["booking_date"],
                    r["value_date"] or "",
                    f"{r['amount_cents'] / 100:.2f}",
                    r["currency"],
                    r["account"],
                    r["counterparty"],
                    r["purpose"],
                    r["booking_type"],
                    r["category"] or "",
                ]
            )
    console.print(f"[green]{len(rows)} Transaktionen → {out}[/green]")


if __name__ == "__main__":
    app()
