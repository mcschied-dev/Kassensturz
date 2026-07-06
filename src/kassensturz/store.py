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
"""


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
        if year and month:
            sql += " AND booking_date LIKE ?"
            params.append(f"{year:04d}-{month:02d}-%")
        elif year:
            sql += " AND booking_date LIKE ?"
            params.append(f"{year:04d}-%")
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
        if year and month:
            sql += " AND booking_date LIKE ?"
            params.append(f"{year:04d}-{month:02d}-%")
        elif year:
            sql += " AND booking_date LIKE ?"
            params.append(f"{year:04d}-%")
        sql += " GROUP BY category, currency ORDER BY total_cents"
        return self.conn.execute(sql, params).fetchall()

    def accounts_overview(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT account, currency, COUNT(*) AS n,
                      SUM(amount_cents) AS total_cents,
                      MIN(booking_date) AS first_date,
                      MAX(booking_date) AS last_date
               FROM transactions GROUP BY account, currency ORDER BY account"""
        ).fetchall()
