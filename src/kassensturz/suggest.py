"""Kategorie-Vorschläge für unkategorisierte Buchungen — Stufen-Pipeline.

Reihenfolge (Advisor-Feedback 11.07.2026):
  1. Deterministische Regeln     → `categorize` (läuft VOR diesem Modul;
                                    dieses Modul sieht nur NULL-Zeilen)
  2. Historische Zuordnungen     → gleicher normalisierter Händler wurde
                                    bisher eindeutig kategorisiert
  3. Merchant-Normalisierung     → `normalize_merchant` (IBAN-/Kartennetz-
                                    Präfixe, Rechtsformen, Filialnummern weg)
  4. ML-Klassifikation           → TF-IDF-Zeichen-n-Gramme + LogReg, nur
                                    für Zeilen ohne historischen Treffer
  5. Menschliche Prüfung         → default werden Vorschläge NUR angezeigt;
                                    `--apply` ist ein bewusster Schritt
  6. Lernen aus Bestätigtem      → bestätigte Zeilen sind beim nächsten Lauf
                                    Historie (Stufe 2); häufige Händler werden
                                    als rules.yaml-Kandidaten ausgewiesen

CFO-Prinzip: nichts wird still gebucht; Konfidenz/Quelle stehen an jedem
Vorschlag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .store import Store

# Klassen mit weniger Beispielen taugen nicht als Trainingsziel (Rauschen,
# Sondereffekte) und würden die Konfidenzen verzerren.
MIN_CLASS_SIZE = 5

# Kategorien, die nie vorgeschlagen werden: Einmaleffekte und manuell
# gepflegte Verfahrens-Semantik (Unterhalt!) sind Regel-/Menschensache.
EXCLUDE_LABELS_LIKE = ("(Sondereffekt)",)

# Historische Zuordnung gilt nur, wenn der Händler bisher (fast) eindeutig
# einer Kategorie zugeordnet war.
HIST_MIN_ROWS = 2
HIST_MIN_PURITY = 0.9

_IBAN = re.compile(r"^[A-Z]{2}\d{20}")
_CARD_PREFIX = re.compile(r"^(sp|sq|fs|uzr|tst|pp)\s*[*.]\s*", re.I)
_NOISE_TOKENS = {
    "gmbh", "ag", "kg", "ohg", "vvag", "inc", "ltd", "ug", "co", "eg", "se",
    "sa", "gbr", "mbh", "e", "v", "und", "der", "die", "das", "the",
}


def normalize_merchant(counterparty: str) -> str:
    """Kanonischer Händler-Schlüssel: 'AMZN.Mktp.DE.XY123', 'SP Mrmarvis.De'
    und 'DE12…REWE Markt' fallen auf vergleichbare Kerne zusammen."""
    s = _IBAN.sub("", counterparty)
    s = _CARD_PREFIX.sub("", s.strip())
    s = re.sub(r"[^a-zäöüß ]", " ", s.lower())
    tokens = [t for t in s.split() if len(t) > 2 and t not in _NOISE_TOKENS]
    return " ".join(tokens[:2])


@dataclass
class Suggestion:
    tx_hash: str
    booking_date: str
    account: str
    amount_cents: int
    counterparty: str
    purpose: str
    category: str
    confidence: float
    source: str  # "historisch (n×)" | "ml"


def _bucket(amount_cents: int) -> str:
    cents = abs(amount_cents)
    if cents < 1_000:
        return "xs"
    if cents < 5_000:
        return "s"
    if cents < 20_000:
        return "m"
    if cents < 100_000:
        return "l"
    return "xl"


def _text(row) -> str:
    sign = "in" if row["amount_cents"] > 0 else "out"
    return (
        f"{row['counterparty']} {row['purpose']} "
        f"§{row['account']} §{_bucket(row['amount_cents'])} §{sign}"
    ).lower()


def _labeled(store: Store) -> list:
    rows = store.conn.execute(
        "SELECT counterparty, purpose, account, amount_cents, category "
        "FROM transactions WHERE category IS NOT NULL"
    ).fetchall()
    return [
        r for r in rows
        if not any(m in r["category"] for m in EXCLUDE_LABELS_LIKE)
    ]


def train(store: Store):
    """Trainiert Vektorisierer + Klassifikator; liefert (model, meta).

    meta enthält Holdout-Genauigkeit und Klassenzahl — die Genauigkeit
    gehört in jede Ausgabe, damit die Vorschläge einschätzbar bleiben.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import make_pipeline

    rows = _labeled(store)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    rows = [r for r in rows if counts[r["category"]] >= MIN_CLASS_SIZE]
    if len(rows) < 100:
        raise ValueError(
            f"nur {len(rows)} kategorisierte Buchungen — zu wenig Training")

    texts = [_text(r) for r in rows]
    labels = [r["category"] for r in rows]
    model = make_pipeline(
        TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2,
                        sublinear_tf=True),
        LogisticRegression(max_iter=2000, C=10.0),
    )
    x_tr, x_te, y_tr, y_te = train_test_split(
        texts, labels, test_size=0.1, random_state=42, stratify=labels)
    model.fit(x_tr, y_tr)
    accuracy = float(model.score(x_te, y_te))
    # fürs echte Vorhersagen auf allen Labels trainieren
    model.fit(texts, labels)
    meta = {
        "accuracy": accuracy,
        "n_train": len(rows),
        "n_classes": len(set(labels)),
    }
    return model, meta


def historical_index(store: Store) -> dict[str, tuple[str, int, float]]:
    """Stufe 2/3: normalisierter Händler → (Kategorie, n, Reinheit),
    sofern die bisherige Zuordnung (fast) eindeutig war."""
    stats: dict[str, dict[str, int]] = {}
    for r in _labeled(store):
        key = normalize_merchant(r["counterparty"])
        if key:
            stats.setdefault(key, {})
            stats[key][r["category"]] = stats[key].get(r["category"], 0) + 1
    index = {}
    for key, cats in stats.items():
        total = sum(cats.values())
        best_cat, best_n = max(cats.items(), key=lambda kv: kv[1])
        purity = best_n / total
        if total >= HIST_MIN_ROWS and purity >= HIST_MIN_PURITY:
            index[key] = (best_cat, total, purity)
    return index


def _make(r, category: str, confidence: float, source: str) -> Suggestion:
    return Suggestion(
        tx_hash=r["hash"], booking_date=r["booking_date"],
        account=r["account"], amount_cents=r["amount_cents"],
        counterparty=r["counterparty"], purpose=r["purpose"],
        category=category, confidence=confidence, source=source,
    )


def suggestions(store: Store, min_confidence: float = 0.5):
    """Vorschläge für alle unkategorisierten Buchungen ≥ min_confidence.

    Erst historische Zuordnung (deterministisch, präzise), ML nur für den
    Rest. Rückgabe: (Vorschläge, meta) — meta enthält Holdout-Genauigkeit
    des ML-Teils und die Trefferzahlen je Stufe.
    """
    rows = store.conn.execute(
        "SELECT hash, booking_date, account, amount_cents, counterparty, "
        "purpose FROM transactions WHERE category IS NULL "
        "ORDER BY booking_date"
    ).fetchall()
    model, meta = train(store)
    meta["n_hist"] = 0
    meta["n_ml"] = 0
    if not rows:
        return [], meta

    hist = historical_index(store)
    out: list[Suggestion] = []
    rest = []
    for r in rows:
        key = normalize_merchant(r["counterparty"])
        if key in hist:
            cat, n, purity = hist[key]
            out.append(_make(r, cat, purity, f"historisch ({n}×)"))
            meta["n_hist"] += 1
        else:
            rest.append(r)

    if rest:
        probs = model.predict_proba([_text(r) for r in rest])
        classes = model.classes_
        for r, p in zip(rest, probs):
            best = p.argmax()
            out.append(_make(r, str(classes[best]), float(p[best]), "ml"))
            meta["n_ml"] += 1

    out = [s for s in out if s.confidence >= min_confidence]
    out.sort(key=lambda s: (s.source == "ml", -s.confidence))
    return out, meta


def regel_konflikte(store: Store, ruleset, min_faelle: int = 3) -> list[dict]:
    """Wo widersprechen manuelle Kategorien den Regeln? (Lern-Loop Stufe 6b)

    Regeln gewinnen bei NEUEN Buchungen immer — wer denselben Händler
    wiederholt anders kategorisiert, will vermutlich die Regel ändern.
    Schwelle 3 Fälle, damit dokumentierte Einzel-Ausnahmen (Circula-Storno-
    Paar, Führerschein Paul) nicht als Konflikt aufschlagen.
    """
    from collections import defaultdict
    stats: dict[tuple, int] = defaultdict(int)
    beispiel: dict[tuple, str] = {}
    for r in store.conn.execute(
            "SELECT counterparty, purpose, booking_type, account, category "
            "FROM transactions WHERE category IS NOT NULL"):
        regel_kat = next(
            (rule.name for rule in ruleset.rules
             if rule.matches(r["counterparty"], r["purpose"],
                             r["booking_type"], r["account"])), None)
        if r["category"] in ("PayPal-Funding (neutral)", "Umbuchung",
                             "Kreditkarten-Abrechnung"):
            continue  # bewusste Neutralisierungen (PayPal-Funding-Zwillinge,
            #           Storno-Paare) — überwacht check_paypal_funding
        if regel_kat and regel_kat != r["category"]:
            key = (normalize_merchant(r["counterparty"]),
                   regel_kat, r["category"])
            stats[key] += 1
            beispiel.setdefault(key, r["counterparty"])
    return sorted(
        ({"haendler": beispiel[k], "kern": k[0], "regel": k[1],
          "ist": k[2], "n": n}
         for k, n in stats.items() if n >= min_faelle),
        key=lambda d: -d["n"])


def rule_candidates(items: list[Suggestion], min_rows: int = 3) -> list[tuple]:
    """Stufe 6: Händler, die oft genug bestätigt wurden, gehören als
    deterministische Regel in rules.yaml — Kandidatenliste dafür."""
    stats: dict[tuple[str, str], int] = {}
    for s in items:
        key = normalize_merchant(s.counterparty)
        if key:
            stats[(key, s.category)] = stats.get((key, s.category), 0) + 1
    return sorted(
        ((key, cat, n) for (key, cat), n in stats.items() if n >= min_rows),
        key=lambda t: -t[2],
    )


def apply(store: Store, items: list[Suggestion], min_confidence: float) -> int:
    """Setzt vorgeschlagene Kategorien ≥ min_confidence (bewusster Schritt!)."""
    n = 0
    with store.conn:
        for s in items:
            if s.confidence >= min_confidence:
                store.conn.execute(
                    "UPDATE transactions SET category = ? "
                    "WHERE hash = ? AND category IS NULL",
                    (s.category, s.tx_hash),
                )
                n += 1
    return n
