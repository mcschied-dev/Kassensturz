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
from .connectors.csv_import import PROFILES, import_csv
from .model import fmt_eur
from .store import Store

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
    txs = import_csv(path, profile, account)
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
def summary(
    year: Optional[int] = typer.Option(None, "--year", "-y"),
    month: Optional[int] = typer.Option(None, "--month", "-m"),
    db: Path = DbOption,
) -> None:
    """Sum per category (optionally for one year/month)."""
    period = f"{year}" + (f"-{month:02d}" if month else "") if year else "gesamt"
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
def accounts(db: Path = DbOption) -> None:
    """Overview of all imported accounts."""
    table = Table(title="Konten")
    for col in ("Konto", "Buchungen", "Von", "Bis"):
        table.add_column(col)
    table.add_column("Saldo (importiert)", justify="right")
    for r in _store(db).accounts_overview():
        table.add_row(
            r["account"],
            str(r["n"]),
            r["first_date"],
            r["last_date"],
            fmt_eur(r["total_cents"], r["currency"]),
        )
    console.print(table)


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
