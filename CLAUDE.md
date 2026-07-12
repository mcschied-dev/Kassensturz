# CLAUDE.md — working notes for AI-assisted development

## Ground rules

- Run everything through uv from the repo root: `uv run kassensturz …`,
  `uv run pytest -q`.
- After ANY change touching data, categorization, or the ledger model:
  `uv run kassensturz check` must pass (exit 1 on warnings). The numbers
  feed real-world decisions — treat them as audit-grade.
- The XLSX workbook is an artifact of the database — regenerate with
  `export-xlsx`, never edit by hand.
- Real data is gitignored (`*.db`, `rules.yaml`, `config.yaml`,
  `budget.yaml`, `csv/`, `reports/`, `state/`). Before any commit,
  `git ls-files` must not show any of them. Test fixtures stay synthetic.

## Architecture in one line

connectors → canonical `Transaction` → SQLite (hash dedup +
`category_log` audit trigger) → categorize (transfer matching →
PayPal-funding-twin neutralization → YAML rules) → suggest (history → ML
→ human) → `check` invariants → reports / XLSX / web.

## Conventions & pitfalls

- **rules.yaml**: first match wins — order matters. `account:` restricts
  a rule to accounts (regex list); it exists because a blanket "paypal"
  rule once neutralized real spending on a partner card. Comments in the
  user's rules file document one-off decisions — never rewrite wholesale.
- **Renaming a category? grep first** — category names are referenced as
  constants in `check.py`, `budget.py`, `forecast.py`, `report/xlsx.py`
  and in tests.
- **Neutral categories** (`Umbuchung`, `Kreditkarten-Abrechnung`,
  `PayPal-Funding (neutral)`) keep settlement rows out of ALL statistics,
  but the rows must stay in the DB — balance reconciliation depends on
  them. Never neutralize a bank row without a matching detail-account
  twin; `check` guards both directions (`paypal-funding`,
  `paypal-deckung`).
- "PayPal – Später zahlen" repayments cover purchases up to ~1 month
  earlier — the merchant row counts at purchase date, the repayment is
  neutral financing cash-flow.
- **Web tests**: point `KASSENSTURZ_HOME` at a temp dir so a real
  `web_auth.json` doesn't interfere; the login form field is `password`.
- Money is integer cents everywhere; `fmt_eur` for display.
- German UI strings; English README and code identifiers.
- Web scenario features are named generically (capital withdrawal /
  inflow) — semantics live in the user's local parameters, not in code.

## Pre-OSS-release cleanup (known debt)

- Strip remaining personal context from docstrings/comments (expense
  module wording, place-word lists in `report/xlsx.py`, one chart
  subtitle in `analyse.html`, CSV-upload preset buttons in
  `einstellungen_provider.html`).
- Make `WEGFALL_KATEGORIE` (`web/app.py`) configurable.
- Replace the Tailwind Play CDN with a standalone build.
