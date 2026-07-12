"""Monatsbudget: Plan je Kategorie vs. spesenbereinigtes Ist.

budget.yaml (gitignored, Marcs echte Zahlen) definiert Monatsbudgets je
Kategorie plus eine Einkommensreferenz. Das Ist ist konsistent mit der
"Saldo bereinigt"-Sicht: Emproof-Spesenkandidaten, Erstattungen, neutrale
Kategorien und Sondereffekte zählen nicht — gemessen wird der Konsum.
Jahres-/Quartalsposten (Kfz-Versicherung, ESt-VZ) machen einzelne Monate
klumpig; die ehrliche Steuergröße ist deshalb der YTD-Vergleich.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .check import SPESEN_EXCLUDE, SPESEN_PATTERNS
from .store import Store

NEUTRAL = ("Umbuchung", "Kreditkarten-Abrechnung", "PayPal-Funding (neutral)",
           "Spesen",
           # Vermögensaufbau ist kein Konsum — gehört nicht ins Budget
           "Sparplan Romy")


def load_budget(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    budgets = {k: int(round(v * 100)) for k, v in data["budgets"].items()}
    return (budgets, int(round(data.get("einkommen_referenz", 0) * 100)),
            str(data.get("start", "0000-01")))


def _ist(store: Store, year: str) -> dict[tuple[str, str], int]:
    """(Monat, Kategorie) → Konsum-Cents (negativ), spesenbereinigt."""
    like = " OR ".join(
        "lower(counterparty) LIKE ? OR lower(purpose) LIKE ?"
        for _ in SPESEN_PATTERNS)
    excl = " AND ".join(
        "lower(counterparty) NOT LIKE ?" for _ in SPESEN_EXCLUDE)
    neutral = ",".join(f"'{c}'" for c in NEUTRAL)
    params = [year] + [f"%{p}%" for p in SPESEN_PATTERNS for _ in range(2)] \
        + [f"%{p}%" for p in SPESEN_EXCLUDE]
    return {
        (r["ym"], r["c"]): r["s"]
        for r in store.conn.execute(
            "SELECT substr(booking_date,1,7) ym,"
            "       COALESCE(category,'(unkategorisiert)') c,"
            "       SUM(amount_cents) s FROM transactions "
            "WHERE substr(booking_date,1,4) = ? AND amount_cents < 0 "
            f"AND COALESCE(category,'') NOT IN ({neutral}) "
            "AND COALESCE(category,'') NOT LIKE '%(Sondereffekt)%' "
            f"AND NOT (({like}) AND {excl}) "
            "GROUP BY ym, c",
            params,
        )
    }


def report(store: Store, budget_path: Path, year: str, month: int) -> dict:
    """Budgetstand: Monat + YTD je Kategorie, sortiert nach YTD-Delta."""
    budgets, einkommen, start = load_budget(budget_path)
    ist = _ist(store, year)
    ym = f"{year}-{month:02d}"
    # YTD zählt erst ab Budget-Start — Vergangenheit wird nicht rückwirkend
    # am neuen Budget gemessen
    ist = {k: v for k, v in ist.items() if k[0] >= start}
    yms = sorted({k[0] for k in ist if k[0] <= ym})
    n_monate = len([y for y in yms if y <= ym]) or 1
    cats = sorted(set(budgets) | {c for _, c in ist})
    zeilen = []
    for c in cats:
        b = budgets.get(c, 0)
        m_ist = -ist.get((ym, c), 0)
        ytd_ist = -sum(v for (y, cc), v in ist.items() if cc == c and y <= ym)
        ytd_budget = b * n_monate
        zeilen.append({
            "kategorie": c, "budget": b, "monat_ist": m_ist,
            "monat_delta": b - m_ist,
            "ytd_ist": ytd_ist, "ytd_budget": ytd_budget,
            "ytd_delta": ytd_budget - ytd_ist,
        })
    zeilen.sort(key=lambda z: z["ytd_delta"])
    summe_budget = sum(budgets.values())
    return {
        "zeilen": zeilen, "monat": ym, "n_monate": n_monate,
        "summe_budget": summe_budget,
        "einkommen": einkommen,
        "plan_saldo": einkommen - summe_budget,
        "monat_ist": sum(z["monat_ist"] for z in zeilen),
        "ytd_ist": sum(z["ytd_ist"] for z in zeilen),
        "ytd_budget": summe_budget * n_monate,
    }
