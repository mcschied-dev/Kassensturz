"""Sanity checks: sums and balances must reconcile.

The consolidated ledger rests on invariants (detail accounts replace the
banks' settlement bookings, internal transfers net out, every asset has a
balance snapshot). Each check returns Findings instead of printing, so the
CLI can render them and scripts/tests can assert on them. Run after every
import, sync, or rule change — a broken invariant means the cashflow
statistics are silently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .store import Store

# Neutralized settlement categories and the detail account that must back
# them. If settlements keep arriving but the detail source stops, expenses
# silently vanish from the consolidated view.
SETTLEMENT_BACKING = (
    ("Kreditkarten-Abrechnung", "mm-karte"),
    ("PayPal-Funding (neutral)", "paypal"),
)

# Settlements may lag the covered spending by roughly one card cycle.
STALE_DAYS = 40

# Internal transfers must net out apart from flows to accounts that hold
# balances but no transactions (Depot, Wise) — those purposes/counterparties
# are excluded from the residual.
EXTERNAL_TRANSFER_MARKERS = ("trade republic", "wise")

UNCATEGORIZED_INCOME_CENTS = 50_000


@dataclass
class Finding:
    level: str  # "ok" | "info" | "warn"
    check: str
    message: str


def check_balance_reconciliation(store: Store) -> list[Finding]:
    """Latest balance vs. transaction sum per account.

    diff == 0        → history covers the account's whole life (beweisbar)
    diff == constant → implied opening balance before the history starts;
                       legitimate, but it must be documented and stable.
    """
    out: list[Finding] = []
    sums = {
        r["account"]: r["s"]
        for r in store.conn.execute(
            "SELECT account, SUM(amount_cents) AS s FROM transactions GROUP BY account"
        )
    }
    for b in store.latest_balances():
        if b["account"] not in sums:  # balance-only assets (Depot, Wise)
            out.append(
                Finding("info", "abstimmung",
                        f"{b['account']}: nur Saldo-Snapshot, keine Umsätze (by design)")
            )
            continue
        diff = b["balance_cents"] - sums[b["account"]]
        if diff == 0:
            out.append(
                Finding("ok", "abstimmung",
                        f"{b['account']}: Saldo == Buchungssumme (auf den Cent)")
            )
        else:
            out.append(
                Finding("info", "abstimmung",
                        f"{b['account']}: Differenz {diff/100:+.2f} € = implizierter "
                        f"Anfangssaldo vor Historienbeginn — muss konstant bleiben")
            )
    for account in sums:
        if account not in {b["account"] for b in store.latest_balances()}:
            out.append(
                Finding("info", "abstimmung",
                        f"{account}: Umsätze ohne Saldo-Snapshot (Detail-Quelle)")
            )
    return out


def check_settlement_backing(store: Store, today: date | None = None) -> list[Finding]:
    """Neutralized settlements need a live detail source behind them."""
    out: list[Finding] = []
    today = today or date.today()
    for category, detail_account in SETTLEMENT_BACKING:
        last_settle = store.conn.execute(
            "SELECT MAX(booking_date) AS d FROM transactions WHERE category = ?",
            (category,),
        ).fetchone()["d"]
        last_detail = store.conn.execute(
            "SELECT MAX(booking_date) AS d FROM transactions WHERE account = ?",
            (detail_account,),
        ).fetchone()["d"]
        if not last_settle:
            continue
        if not last_detail:
            out.append(
                Finding("warn", "deckung",
                        f"{category} neutralisiert, aber Detail-Konto "
                        f"{detail_account} ist leer — Ausgaben fehlen!")
            )
            continue
        lag = date.fromisoformat(last_settle) - date.fromisoformat(last_detail)
        if lag > timedelta(days=STALE_DAYS):
            out.append(
                Finding("warn", "deckung",
                        f"{category} bis {last_settle} neutralisiert, aber "
                        f"{detail_account}-Detail endet {last_detail} "
                        f"({lag.days} Tage Lücke) → neuen Export importieren, "
                        f"sonst fehlen diese Ausgaben")
            )
        else:
            out.append(
                Finding("ok", "deckung",
                        f"{category}: Detail {detail_account} aktuell "
                        f"(bis {last_detail})")
            )
    return out


def check_transfer_residual(store: Store, tolerance_cents: int = 100_000) -> list[Finding]:
    """Internal transfers must net to ~0 once external flows are excluded."""
    like = " OR ".join(
        "lower(counterparty) LIKE ? OR lower(purpose) LIKE ?"
        for _ in EXTERNAL_TRANSFER_MARKERS
    )
    params = [f"%{m}%" for m in EXTERNAL_TRANSFER_MARKERS for _ in range(2)]
    row = store.conn.execute(
        f"""SELECT COALESCE(SUM(amount_cents),0) AS s, COUNT(*) AS n
            FROM transactions WHERE category = 'Umbuchung' AND NOT ({like})""",
        params,
    ).fetchone()
    if abs(row["s"]) > tolerance_cents:
        return [Finding("warn", "umbuchung",
                        f"Interne Umbuchungen saldieren auf {row['s']/100:+.2f} € "
                        f"({row['n']} Buchungen) — unpaarige Transfers suchen")]
    return [Finding("ok", "umbuchung",
                    f"Interne Umbuchungen saldieren auf {row['s']/100:+.2f} € "
                    f"({row['n']} Buchungen)")]


def check_uncategorized_income(store: Store) -> list[Finding]:
    """Big unexplained inflows are the worst kind of gap in a Zugewinn case."""
    rows = store.conn.execute(
        """SELECT booking_date, account, counterparty, amount_cents
           FROM transactions WHERE category IS NULL AND amount_cents >= ?
           ORDER BY amount_cents DESC""",
        (UNCATEGORIZED_INCOME_CENTS,),
    ).fetchall()
    if not rows:
        return [Finding("ok", "einnahmen",
                        "keine unkategorisierte Einnahme über "
                        f"{UNCATEGORIZED_INCOME_CENTS/100:.0f} €")]
    return [
        Finding("warn", "einnahmen",
                f"{r['booking_date']} {r['account']}: {r['amount_cents']/100:+.2f} € "
                f"von '{r['counterparty']}' unkategorisiert — erklären!")
        for r in rows
    ]


def check_monthly_gaps(store: Store, min_rows: int = 100) -> list[Finding]:
    """Months without a single booking inside an otherwise active account's
    history usually mean a hole in an export, not a quiet month. Accounts
    with few bookings (Tagesgeld: quarterly interest) are skipped."""
    out: list[Finding] = []
    accounts = store.conn.execute(
        """SELECT account, COUNT(*) AS n,
                  substr(MIN(booking_date),1,7) AS first,
                  substr(MAX(booking_date),1,7) AS last
           FROM transactions GROUP BY account HAVING n >= ?""",
        (min_rows,),
    ).fetchall()
    for acc in accounts:
        present = {
            r["m"]
            for r in store.conn.execute(
                """SELECT DISTINCT substr(booking_date,1,7) AS m
                   FROM transactions WHERE account = ?""",
                (acc["account"],),
            )
        }
        missing: list[str] = []
        y, m = (int(x) for x in acc["first"].split("-"))
        while f"{y:04d}-{m:02d}" < acc["last"]:
            m += 1
            if m == 13:
                y, m = y + 1, 1
            month = f"{y:04d}-{m:02d}"
            if month <= acc["last"] and month not in present:
                missing.append(month)
        if missing:
            out.append(
                Finding("warn", "lücken",
                        f"{acc['account']}: keine Buchungen in {', '.join(missing)} "
                        f"— Export-/Sync-Loch prüfen")
            )
        else:
            out.append(
                Finding("ok", "lücken",
                        f"{acc['account']}: lückenlos {acc['first']} … {acc['last']}")
            )
    return out


def run_all(store: Store, today: date | None = None) -> list[Finding]:
    return [
        *check_balance_reconciliation(store),
        *check_settlement_backing(store, today=today),
        *check_transfer_residual(store),
        *check_uncategorized_income(store),
        *check_monthly_gaps(store),
    ]
