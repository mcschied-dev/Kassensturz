"""SQLite-backed transaction store. One file, no server, append-only + dedup."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .model import Transaction, assign_seq

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
  hash TEXT PRIMARY KEY,
  booking_date TEXT NOT NULL,
  value_date TEXT,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  account TEXT NOT NULL,
  counterparty TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT '',
  booking_type TEXT NOT NULL DEFAULT '',
  category TEXT,
  source TEXT NOT NULL DEFAULT '',
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(booking_date);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);

CREATE TABLE IF NOT EXISTS balances (
  account TEXT NOT NULL,
  snapshot_date TEXT NOT NULL,
  balance_cents INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'EUR',
  source TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (account, snapshot_date)
);
"""


_DATE_LIKE = " AND booking_date LIKE ?"


def build_year_matrix(rows) -> tuple[list[str], list[dict]]:
    """Pivot summary_by_year rows → (years, [{name, cells{year:cents}, total}])."""
    years = sorted({r["year"] for r in rows})
    by_cat: dict[str, dict] = {}
    for r in rows:
        e = by_cat.setdefault(
            r["category"], {"name": r["category"], "cells": {}, "total": 0}
        )
        e["cells"][r["year"]] = r["total_cents"]
        e["total"] += r["total_cents"]
    return years, sorted(by_cat.values(), key=lambda e: e["total"])


def _period_filter(year: int | None, month: int | None) -> tuple[str, list[str]]:
    """SQL fragment + params restricting booking_date to a year/month."""
    if year and month:
        return _DATE_LIKE, [f"{year:04d}-{month:02d}-%"]
    if year:
        return _DATE_LIKE, [f"{year:04d}-%"]
    return "", []


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- writing -----------------------------------------------------------

    def add(self, txs: list[Transaction]) -> tuple[int, int]:
        """Insert transactions; returns (inserted, skipped_as_duplicate)."""
        txs = assign_seq(txs)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        inserted = 0
        for t in txs:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO transactions
                   (hash, booking_date, value_date, amount_cents, currency,
                    account, counterparty, purpose, booking_type, category,
                    source, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t.tx_hash(),
                    t.booking_date.isoformat(),
                    t.value_date.isoformat() if t.value_date else None,
                    t.amount_cents,
                    t.currency,
                    t.account,
                    t.counterparty,
                    t.purpose,
                    t.booking_type,
                    t.category,
                    t.source,
                    now,
                ),
            )
            inserted += cur.rowcount
        self.conn.commit()
        return inserted, len(txs) - inserted

    def record_balances(
        self, balances: list[tuple[str, str, int, str, str]]
    ) -> None:
        """Store balance snapshots: (account, iso_date, cents, currency, source)."""
        self.conn.executemany(
            """INSERT OR REPLACE INTO balances
               (account, snapshot_date, balance_cents, currency, source)
               VALUES (?, ?, ?, ?, ?)""",
            balances,
        )
        self.conn.commit()

    def latest_balances(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT account, snapshot_date, balance_cents, currency
               FROM balances b
               WHERE snapshot_date = (
                   SELECT MAX(snapshot_date) FROM balances WHERE account = b.account
               )
               ORDER BY balance_cents DESC"""
        ).fetchall()

    def adopt_categories(self, txs: list[Transaction]) -> int:
        """Fallback categorization from a bank/app CSV.

        Only fills rows that are still NULL — YAML rules always win.
        Returns the number of rows updated.
        """
        updated = 0
        for t in assign_seq(txs):
            if not t.category:
                continue
            cur = self.conn.execute(
                "UPDATE transactions SET category = ? "
                "WHERE hash = ? AND category IS NULL",
                (t.category, t.tx_hash()),
            )
            updated += cur.rowcount
        self.conn.commit()
        return updated

    def set_category(self, hashes: list[str], category: str | None) -> int:
        if not hashes:
            return 0
        qs = ",".join("?" * len(hashes))
        cur = self.conn.execute(
            f"UPDATE transactions SET category = ? WHERE hash IN ({qs})",
            [category, *hashes],
        )
        self.conn.commit()
        return cur.rowcount

    # -- reading -----------------------------------------------------------

    def query(
        self,
        account: str | None = None,
        year: int | None = None,
        month: int | None = None,
        category: str | None = None,
        uncategorized: bool = False,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM transactions WHERE 1=1"
        params: list = []
        if account:
            sql += " AND account = ?"
            params.append(account)
        frag, frag_params = _period_filter(year, month)
        sql += frag
        params.extend(frag_params)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if uncategorized:
            sql += " AND category IS NULL"
        sql += " ORDER BY booking_date DESC, hash"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def summary(
        self, year: int | None = None, month: int | None = None
    ) -> list[sqlite3.Row]:
        sql = """SELECT COALESCE(category, '(unkategorisiert)') AS category,
                        currency,
                        SUM(amount_cents) AS total_cents,
                        COUNT(*) AS n
                 FROM transactions WHERE 1=1"""
        params: list = []
        frag, frag_params = _period_filter(year, month)
        sql += frag
        params.extend(frag_params)
        sql += " GROUP BY category, currency ORDER BY total_cents"
        return self.conn.execute(sql, params).fetchall()

    def balances_at(self, stichtag: str) -> list[dict]:
        """Per-account balance at a historic date (ISO), computed backwards:
        latest snapshot minus all bookings after the Stichtag.

        status: 'ok'             — data reaches back past the Stichtag
                'nicht_existent' — account demonstrably opened later
                                   (tx sum equals today's balance)
                'unbekannt'      — history starts after the Stichtag but
                                   doesn't cover the account's full life
        """
        out: list[dict] = []
        for b in self.latest_balances():
            acc = b["account"]
            later = self.conn.execute(
                """SELECT COALESCE(SUM(amount_cents), 0) AS s FROM transactions
                   WHERE account = ? AND booking_date > ? AND booking_date <= ?""",
                (acc, stichtag, b["snapshot_date"]),
            ).fetchone()["s"]
            ever = self.conn.execute(
                """SELECT MIN(booking_date) AS first_tx,
                          COALESCE(SUM(amount_cents), 0) AS total
                   FROM transactions WHERE account = ?""",
                (acc,),
            ).fetchone()
            if ever["first_tx"] and ever["first_tx"] <= stichtag:
                status, cents = "ok", b["balance_cents"] - later
            elif ever["total"] == b["balance_cents"]:
                status, cents = "nicht_existent", 0
            else:
                status, cents = "unbekannt", None
            out.append(
                {
                    "account": acc,
                    "status": status,
                    "balance_cents": cents,
                    "currency": b["currency"],
                    "snapshot_date": b["snapshot_date"],
                    "data_from": ever["first_tx"],
                }
            )
        return out

    def summary_by_year(self) -> list[sqlite3.Row]:
        """Category sums per calendar year (for pivot views)."""
        return self.conn.execute(
            """SELECT COALESCE(category, '(unkategorisiert)') AS category,
                      substr(booking_date,1,4) AS year,
                      SUM(amount_cents) AS total_cents,
                      COUNT(*) AS n
               FROM transactions
               GROUP BY category, year
               ORDER BY category, year"""
        ).fetchall()

    def accounts_overview(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT account, currency, COUNT(*) AS n,
                      SUM(amount_cents) AS total_cents,
                      MIN(booking_date) AS first_date,
                      MAX(booking_date) AS last_date
               FROM transactions GROUP BY account, currency ORDER BY account"""
        ).fetchall()
