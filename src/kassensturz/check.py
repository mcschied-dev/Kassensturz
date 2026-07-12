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
        # Kalendermonate können trotz Loch besetzt sein (je 1 Zeile am
        # Rand) — deshalb zusätzlich der größte Tagesabstand zwischen
        # aufeinanderfolgenden Buchungen der letzten ~13 Monate.
        gap_row = store.conn.execute(
            """SELECT MAX(julianday(booking_date) - julianday(prev)) AS g,
                      COUNT(*) AS n
               FROM (SELECT booking_date,
                            LAG(booking_date) OVER (ORDER BY booking_date) AS prev
                     FROM (SELECT DISTINCT booking_date FROM transactions
                           WHERE account = ?
                             AND booking_date >= date(
                                   (SELECT MAX(booking_date) FROM transactions
                                    WHERE account = ?), '-400 days')))""",
            (acc["account"], acc["account"]),
        ).fetchone()
        # nur bei dichter Nutzung aussagekräftig — eine selten genutzte
        # Partnerkarte hat legitime Wochen ohne Umsatz
        max_gap = int(gap_row["g"] or 0) if gap_row["n"] >= 60 else 0
        if missing:
            out.append(
                Finding("warn", "lücken",
                        f"{acc['account']}: keine Buchungen in {', '.join(missing)} "
                        f"— Export-/Sync-Loch prüfen")
            )
        elif max_gap > 28:
            out.append(
                Finding("warn", "lücken",
                        f"{acc['account']}: {max_gap} Tage ohne Buchung im letzten "
                        f"Jahr — fehlende Abrechnung/Export prüfen")
            )
        else:
            out.append(
                Finding("ok", "lücken",
                        f"{acc['account']}: lückenlos {acc['first']} … {acc['last']}")
            )
    return out


# --- Spesen-Erstattung (Emproof/Circula) -------------------------------------
#
# Marc reicht Reisebelege in Circula ein; Emproof erstattet auf die DKB —
# belegweise ("RNr. … RDat. …", Betrag == Beleg) oder als Sammelreport
# ("Travel Expenses … sequenz-id …"). Der Check matcht Reise-Ausgaben gegen
# Erstattungen und meldet vergessene Einreichungen.

SPESEN_SINCE = "2026-01-01"  # Emproof-Arbeitsvertrag ab 01.01.2026

# was als Reise-/Spesenkandidat gilt (Substring, lowercase, gegen
# counterparty UND purpose geprüft — Kartenzeilen tragen die Stadt im Text)
SPESEN_PATTERNS = (
    "db vertrieb", "db fernverkehr", "deutsche bahn", "vrr ",
    "stayery", "hotel", "mercure", "motel one", "8710 mo ",
    "mo berl", "mo bonn",  # Motel-One-Kartentexte ("5500 MO BERL UPPER WST")
    "kantine.wtf", "lufthan", "flughafen", "muc airport", "rentalcars",
    "record go", "circula",
    "bochum", "bremen",
)

# Verpflegung unterwegs ist PRIVAT (dafür gibt es Pauschalen, keine
# Einzelbelege — Marc, 11.07.2026); Duty-free am Flughafen ebenso.
# ("_" = LIKE-Wildcard: trifft Bäckerei/B.ckerei/B?ckerei-Kartentexte)
SPESEN_EXCLUDE = ("b_ckerei", "baecker", "rewe", "hannelores", "zettle",
                  "airport retail")

# so lange darf eine Ausgabe unerstattet bleiben, bevor gewarnt wird
# (Einreichung + Monatsende-Zahllauf)
SPESEN_GRACE_DAYS = 45

# Bagatellen nicht anmahnen
SPESEN_MIN_CENTS = 1_000


def _spesen_candidates(store: Store) -> list[dict]:
    like = " OR ".join(
        "lower(counterparty) LIKE ? OR lower(purpose) LIKE ?"
        for _ in SPESEN_PATTERNS
    )
    excl = " AND ".join(
        "lower(counterparty) NOT LIKE ?" for _ in SPESEN_EXCLUDE
    )
    params = [f"%{p}%" for p in SPESEN_PATTERNS for _ in range(2)]
    excl_params = [f"%{p}%" for p in SPESEN_EXCLUDE]
    return [
        dict(r)
        for r in store.conn.execute(
            f"""SELECT booking_date, account, counterparty, purpose, amount_cents
                FROM transactions
                WHERE amount_cents < 0 AND booking_date >= ?
                  AND account != 'mm-birgit'
                  AND COALESCE(category,'') NOT IN
                      ('Umbuchung','Kreditkarten-Abrechnung',
                       'PayPal-Funding (neutral)','Bankgebühren')
                  AND ({like})
                  AND {excl}
                ORDER BY booking_date""",
            [SPESEN_SINCE, *params, *excl_params],
        )
    ]


def _spesen_reimbursements(store: Store) -> list[dict]:
    # ab SPESEN_SINCE: die Kategorie enthält auch DRACOON-Erstattungen
    # (2022–2024, per Regel-Bereinigung 12.07.2026) — die gehören nicht in
    # die Emproof-Abstimmung, sonst bläht sich der Sammelreport-Überhang
    return [
        dict(r)
        for r in store.conn.execute(
            """SELECT booking_date, purpose, amount_cents
               FROM transactions
               WHERE category = 'Spesen' AND amount_cents > 0
                 AND booking_date >= ?
               ORDER BY booking_date""",
            (SPESEN_SINCE,),
        )
    ]


def match_spesen(store: Store, today: date | None = None) -> dict:
    """Greedy Beleg-Matching: gleicher Betrag, Erstattung 0–90 Tage nach
    der Ausgabe. Sammelreports bleiben als Erstattungs-Überhang stehen und
    decken rechnerisch die ältesten unmatchten Ausgaben."""
    today = today or date.today()
    expenses = _spesen_candidates(store)
    reimbs = _spesen_reimbursements(store)
    open_reimbs = list(reimbs)
    for e in expenses:
        for r in open_reimbs:
            gap = (date.fromisoformat(r["booking_date"])
                   - date.fromisoformat(e["booking_date"])).days
            if r["amount_cents"] == -e["amount_cents"] and 0 <= gap <= 90:
                e["matched"] = r["booking_date"]
                open_reimbs.remove(r)
                break
        else:
            e["matched"] = None
    unmatched = [
        e for e in expenses
        if e["matched"] is None
        and -e["amount_cents"] >= SPESEN_MIN_CENTS
        and (today - date.fromisoformat(e["booking_date"])).days > SPESEN_GRACE_DAYS
    ]
    surplus = sum(r["amount_cents"] for r in open_reimbs)  # Sammelreports etc.
    return {
        "expenses": expenses,
        "reimbursed_total": sum(r["amount_cents"] for r in reimbs),
        "unmatched": unmatched,
        "unmatched_total": -sum(e["amount_cents"] for e in unmatched),
        "surplus": surplus,
    }


def check_spesen(store: Store, today: date | None = None) -> list[Finding]:
    result = match_spesen(store, today=today)
    if not result["expenses"]:
        return []
    gap = result["unmatched_total"] - result["surplus"]
    if gap > 0:
        return [Finding(
            "warn", "spesen",
            f"{len(result['unmatched'])} Reisebelege ohne passende Emproof-"
            f"Erstattung ({result['unmatched_total']/100:.2f} €; nach Abzug des "
            f"Sammelreport-Überhangs fehlen {gap/100:.2f} €) — `kassensturz "
            f"spesen` zeigt die Belege")]
    return [Finding(
        "ok", "spesen",
        f"Reisespesen seit {SPESEN_SINCE} gedeckt (erstattet "
        f"{result['reimbursed_total']/100:.2f} €, Überhang aus Sammelreports "
        f"{result['surplus']/100:.2f} € ≥ offene {result['unmatched_total']/100:.2f} €)")]


# Seit 03/2026 kürzt die DKB PayPal-Kartenbelastungen auf den reinen
# Händlernamen ("Google" statt "PAYPAL..GOOGLE") — solche Funding-Legs
# rutschen an der Neutralisierungs-Regel vorbei und zählen doppelt.
FUNDING_TWIN_SINCE = "2026-03-01"
FUNDING_TWIN_DAYS = 4


def check_paypal_funding_twins(store: Store) -> list[Finding]:
    """Nicht-neutrale Bankbuchung + PayPal-Zeile mit gleichem Betrag binnen
    weniger Tage, deren Händlername den Bank-Empfänger enthält = mutmaßlich
    doppelt gezähltes Funding-Leg → warnen (Kategorie-Fix nötig)."""
    rows = store.conn.execute(
        "SELECT d.booking_date dd, d.amount_cents a, d.counterparty dc,"
        "       p.counterparty pc "
        "FROM transactions d JOIN transactions p ON p.account = 'paypal' "
        "  AND p.amount_cents = d.amount_cents "
        "  AND ABS(julianday(p.booking_date) - julianday(d.booking_date)) <= ? "
        "WHERE d.account != 'paypal' AND d.amount_cents < 0 "
        "  AND d.booking_date >= ? "
        "  AND COALESCE(d.category,'') NOT IN "
        "      ('PayPal-Funding (neutral)', 'Umbuchung', 'Kreditkarten-Abrechnung')",
        (FUNDING_TWIN_DAYS, FUNDING_TWIN_SINCE),
    ).fetchall()
    suspects = {}
    for r in rows:
        # Namensabgleich filtert Zufalls-Zwillinge (Amazon 9,99 ≠ Spotify 9,99)
        token = r["dc"].lower().split("/")[0].split(",")[0].strip()
        if token and token in r["pc"].lower():
            suspects[(r["dd"], r["a"], r["dc"])] = None
    if suspects:
        total = -sum(a for _, a, _ in suspects) / 100
        beispiele = ", ".join(f"{d} {c}" for d, _, c in list(suspects)[:3])
        return [Finding(
            "warn", "paypal-funding",
            f"{len(suspects)} Bankbuchungen sehen wie unneutralisierte "
            f"PayPal-Funding-Legs aus ({total:.2f} € doppelt gezählt; z. B. "
            f"{beispiele}) — Kategorie auf 'PayPal-Funding (neutral)' setzen")]
    return [Finding("ok", "paypal-funding",
                    "keine doppelt gezählten PayPal-Funding-Legs")]


# Gegenrichtung zur Zwillings-Prüfung: eine neutralisierte Zeile OHNE
# Deckung im paypal-Detailkonto radiert echtes Geld aus dem Cashflow
# (so verschwanden 2.980 € von Birgits Partnerkarte, Forensik 12.07.2026).
# Ausgenommen sind PayPals eigene Einzugs-/Kreditzeilen: "Später zahlen"-
# Raten decken Käufe bis ~1 Monat zuvor (Händlerzeile zählt am Kaufdatum),
# deshalb das lange Rückschau-Fenster für alles andere.
NEUTRAL_SETTLEMENT_MARKERS = ("paypal (europe)", "paypal europe",
                              "spaeter.zahlen", "später zahlen")
NEUTRAL_TWIN_LOOKBACK_DAYS = 100


def check_paypal_neutral_deckung(store: Store) -> list[Finding]:
    """Jede 'PayPal-Funding (neutral)'-Bankzeile braucht entweder einen
    betragsgleichen paypal-Zwilling (Kauf bis 100 Tage zuvor, Lastschrift
    bis 4 Tage danach) oder muss eine PayPal-eigene Sammel-/Kreditzeile
    sein — sonst wurde vermutlich eine echte Ausgabe neutralisiert."""
    rows = store.conn.execute(
        """SELECT d.booking_date dd, d.account acc, d.amount_cents a,
                  d.counterparty dc, d.purpose dp,
                  EXISTS (SELECT 1 FROM transactions p
                          WHERE p.account = 'paypal'
                            AND p.amount_cents = d.amount_cents
                            AND julianday(d.booking_date)
                                - julianday(p.booking_date)
                                BETWEEN -4 AND ?) AS gedeckt
           FROM transactions d
           WHERE d.category = 'PayPal-Funding (neutral)'
             AND d.account != 'paypal'""",
        (NEUTRAL_TWIN_LOOKBACK_DAYS,),
    ).fetchall()
    offen = [
        r for r in rows
        if not r["gedeckt"]
        and not any(m in (r["dc"] + " " + r["dp"]).lower()
                    for m in NEUTRAL_SETTLEMENT_MARKERS)
    ]
    if offen:
        total = -sum(r["a"] for r in offen) / 100
        beispiele = ", ".join(
            f"{r['dd']} {r['acc']} {r['dc'][:25]}" for r in offen[:3])
        return [Finding(
            "warn", "paypal-deckung",
            f"{len(offen)} neutralisierte Zeilen ohne paypal-Zwilling "
            f"({total:.2f} € aus dem Cashflow radiert; z. B. {beispiele}) "
            f"— echte Ausgabe? Kategorie korrigieren")]
    return [Finding("ok", "paypal-deckung",
                    f"alle {len(rows)} neutralisierten PayPal-Funding-Zeilen "
                    f"gedeckt oder PayPal-eigene Einzugszeilen")]


def run_all(store: Store, today: date | None = None) -> list[Finding]:
    return [
        *check_balance_reconciliation(store),
        *check_settlement_backing(store, today=today),
        *check_transfer_residual(store),
        *check_uncategorized_income(store),
        *check_monthly_gaps(store),
        *check_spesen(store, today=today),
        *check_paypal_funding_twins(store),
        *check_paypal_neutral_deckung(store),
    ]
