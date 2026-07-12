"""Rule-based categorization + transfer matching.

Rules live in a YAML file (see rules.example.yaml). First match wins,
so order matters. Transfer matching pairs opposite-sign bookings of the
same amount between two *own* accounts within a few days and marks both
as internal transfers — without this, every cashflow statistic is wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .store import Store

DEFAULT_TRANSFER_CATEGORY = "Umbuchung"
DEFAULT_TRANSFER_MAX_DAYS = 3


@dataclass
class Rule:
    name: str
    counterparty: list[re.Pattern] = field(default_factory=list)
    purpose: list[re.Pattern] = field(default_factory=list)
    booking_type: list[re.Pattern] = field(default_factory=list)
    # account ist eine EINSCHRÄNKUNG, kein Treffer-Kriterium: ist die Liste
    # gesetzt, greift die Regel nur auf diesen Konten. Nötig geworden, weil
    # die PayPal-Neutralisierung sonst auch Birgits Partnerkarten-Käufe
    # ("Paypal *Ebay" auf mm-birgit) aus dem Cashflow radierte.
    account: list[re.Pattern] = field(default_factory=list)
    # ab ist ebenfalls eine EINSCHRÄNKUNG: Regel gilt erst ab diesem
    # Buchungsdatum (ISO). Nötig für Ära-Wechsel — die Emproof-Regeln
    # (Geschäftsreise etc.) gelten ab 2026, davor fällt dieselbe Buchung
    # auf die späteren Alltags-Regeln (Mobilität …) durch.
    ab: str | None = None

    def matches(self, counterparty: str, purpose: str, booking_type: str,
                account: str = "", booking_date: str = "") -> bool:
        if self.account and not any(p.search(account) for p in self.account):
            return False
        if self.ab and booking_date and booking_date < self.ab:
            return False
        return (
            any(p.search(counterparty) for p in self.counterparty)
            or any(p.search(purpose) for p in self.purpose)
            or any(p.search(booking_type) for p in self.booking_type)
        )


@dataclass
class RuleSet:
    rules: list[Rule]
    transfer_category: str = DEFAULT_TRANSFER_CATEGORY
    transfer_max_days: int = DEFAULT_TRANSFER_MAX_DAYS


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def load_rules(path: str | Path) -> RuleSet:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules = [
        Rule(
            name=entry["name"],
            counterparty=_compile(entry.get("counterparty", [])),
            purpose=_compile(entry.get("purpose", [])),
            booking_type=_compile(entry.get("booking_type", [])),
            account=_compile(entry.get("account", [])),
            ab=str(entry["ab"]) if entry.get("ab") else None,
        )
        for entry in data.get("categories", [])
    ]
    transfers = data.get("transfers") or {}
    return RuleSet(
        rules=rules,
        transfer_category=transfers.get("category", DEFAULT_TRANSFER_CATEGORY),
        transfer_max_days=int(transfers.get("max_days", DEFAULT_TRANSFER_MAX_DAYS)),
    )


def apply_rules(store: Store, ruleset: RuleSet) -> dict[str, int]:
    """Categorize all uncategorized transactions. Returns {category: count}."""
    counts: dict[str, int] = {}
    by_category: dict[str, list[str]] = {}
    for row in store.query(uncategorized=True):
        for rule in ruleset.rules:
            if rule.matches(row["counterparty"], row["purpose"],
                            row["booking_type"], row["account"],
                            row["booking_date"]):
                by_category.setdefault(rule.name, []).append(row["hash"])
                break
    for category, hashes in by_category.items():
        counts[category] = store.set_category(hashes, category)
    return counts


# PayPal-Funding-Legs, denen die DKB seit 03/2026 den "PAYPAL"-Präfix
# wegkürzt ("Apple / iTunes" statt "PAYPAL..APPLE"), erkennt keine Text-
# regel mehr — nur der Daten-Zwilling: gleiche Summe binnen weniger Tage
# im paypal-Detailkonto, Händlername passt. Ohne diesen Schritt landen
# neue Legs bei Regeln/ML als echte Ausgabe (12.07.2026 dreimal passiert)
# und zählen doppelt, bis `check` Alarm schlägt.
FUNDING_LEG_ACCOUNTS = ("dkb-3527", "ksk-giro", "mm-karte")
FUNDING_LEG_DAYS = 4
FUNDING_CATEGORY = "PayPal-Funding (neutral)"


def neutralize_funding_legs(store: Store) -> int:
    """Deterministische Stufe vor den Textregeln: unkategorisierte Bank-
    Belastung + betrags- und namensgleiche paypal-Zeile (±4 Tage) = Funding-
    Leg → neutral. Läuft VOR apply_rules, sonst fängt z. B. die
    'Abos & Dienste'-Regel ein Spotify-Leg als Ausgabe."""
    rows = store.conn.execute(
        "SELECT d.hash h, d.counterparty dc, p.counterparty pc "
        "FROM transactions d JOIN transactions p ON p.account = 'paypal' "
        "  AND p.amount_cents = d.amount_cents "
        "  AND ABS(julianday(p.booking_date) - julianday(d.booking_date)) <= ? "
        f"WHERE d.account IN ({','.join('?' * len(FUNDING_LEG_ACCOUNTS))}) "
        "  AND d.category IS NULL AND d.amount_cents < 0",
        (FUNDING_LEG_DAYS, *FUNDING_LEG_ACCOUNTS),
    ).fetchall()
    hashes = []
    for r in rows:
        # Namensabgleich gegen Zufalls-Zwillinge (Amazon 9,99 ≠ Spotify 9,99)
        token = r["dc"].lower().split("/")[0].split(",")[0].strip()
        if token and token in r["pc"].lower():
            hashes.append(r["h"])
    return store.set_category(sorted(set(hashes)), FUNDING_CATEGORY)


def match_transfers(store: Store, ruleset: RuleSet) -> int:
    """Pair internal transfers between own accounts; returns rows marked."""
    rows = store.query(uncategorized=True)
    used: set[str] = set()
    matched: list[str] = []

    # index positive legs by (amount, currency)
    positives: dict[tuple[int, str], list] = {}
    for r in rows:
        if r["amount_cents"] > 0:
            positives.setdefault((r["amount_cents"], r["currency"]), []).append(r)

    for r in rows:
        if r["amount_cents"] >= 0 or r["hash"] in used:
            continue
        candidates = positives.get((-r["amount_cents"], r["currency"]), [])
        r_date = date.fromisoformat(r["booking_date"])
        for c in candidates:
            if c["hash"] in used or c["account"] == r["account"]:
                continue
            delta = abs((date.fromisoformat(c["booking_date"]) - r_date).days)
            if delta <= ruleset.transfer_max_days:
                used.update((r["hash"], c["hash"]))
                matched.extend((r["hash"], c["hash"]))
                break

    return store.set_category(matched, ruleset.transfer_category)
