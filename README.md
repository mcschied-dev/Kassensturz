# kassensturz

Local-first personal finance for German banks. Pull transactions via
FinTS/HBCI, the DKB web API, Wise and Trade Republic, consolidate them into
one ledger, categorize with YAML rules plus an ML suggestion pipeline, and
analyze everything through a CLI, a dark-mode web UI with interactive
charts, an XLSX workbook, and German LaTeX/PDF reports — all on your own
machine. No cloud, no third-party aggregator.

> **Kassensturz** *(German)* — counting up what's actually in the till.

## Features

### Getting data in

- **Multi-bank sync** (`sync`) — all configured connections in one run,
  isolated per bank, with a summary table:
  - **FinTS/HBCI** (Sparkassen and most German banks): PIN in the system
    keychain, client state persisted per connection so SCA/2FA is only
    triggered when the bank insists; decoupled pushTAN (confirm in the
    banking app, polled headless) and classic TAN supported.
  - **DKB** via the bank's web/REST API (`dkb-robo`) — no FinTS product ID
    needed; one app confirmation per run, captcha handled via Playwright.
  - **Wise** (official API) — balance snapshots for all currencies.
    (Transaction statements are blocked by PSD2 for EEA personal tokens;
    the SCA signing code is included for non-EEA accounts.)
  - **Trade Republic** (`pytr`) — depot market value + cash as a balance
    snapshot, positions valued via live ticker.
- **CSV import** (`import-csv`) with per-source profiles (DKB, Sparkasse
  CAMT, Outbank, PayPal activity export, Deutsche Bank credit card portal,
  generic). Re-importing overlapping files is a no-op (hash dedup).
- **Balance snapshots** for every account — assets are never invisible,
  even where only balances are available (`balance` for manual entry).

### One consolidated ledger

- Detail sources (e.g. a PayPal account or a credit card CSV) hold the
  real merchant-level bookings; the banks' settlement/funding rows are
  neutralized by category (`Umbuchung`, `Kreditkarten-Abrechnung`,
  `PayPal-Funding (neutral)`) so **every euro counts exactly once**, at
  the real merchant.
- PayPal funding legs are auto-neutralized during `categorize` by matching
  the exact amount against the PayPal detail account within a few days —
  including bank rows where the "PAYPAL" prefix was stripped to the bare
  merchant name.
- Internal transfers between your own accounts are paired automatically
  (opposite sign, same amount, few days apart).
- Every category change is recorded in an audit table (`category_log`) via
  an SQLite trigger — you can always answer "who changed this row, when?".

### Categorization

- **YAML rules** (`rules.yaml`, first match wins) on counterparty, purpose,
  booking type — optionally scoped to specific accounts (`account:`).
- **Bank category adoption** (`adopt-categories`) as a fallback for rows
  your rules didn't catch; rules always win.
- **Suggestion pipeline** (`suggest`) in stages: deterministic rules →
  historical merchant matching (normalized merchant keys, purity ≥ 90 %) →
  TF-IDF + logistic regression ML for the rest (holdout accuracy is always
  reported). Suggestions are shown by default; `--apply` is a deliberate
  step. Confirmed merchants are proposed as new deterministic rules.
- **Conflict detector** (`konflikte`) — finds merchants you repeatedly
  categorize differently than the rules would, so rules and habits
  converge instead of fighting.
- Categories are organized into **groups** (one level, e.g. "Wohnen",
  "Versicherungen"), editable in the web UI.

### Integrity — `check`

Run after every import/sync/rule change; exit code 1 on warnings:

- balance vs. transaction-sum reconciliation per account (to the cent, or
  a documented constant opening balance),
- neutralized settlements must be backed by a live detail source,
- internal transfers must net to ~0,
- uncategorized large inflows are flagged,
- monthly gaps in active accounts (export/sync holes),
- expense reimbursement matching (see below),
- PayPal funding legs: double-counted rows (non-neutral with a detail
  twin) **and** the reverse — neutralized rows without backing.

### Analysis & reports

- `summary` — category sums, optionally pivoted by year.
- `budget` — budget vs. actual per category (reimbursement-adjusted),
  driven by a `budget.yaml`.
- `forecast` — month-by-month projection from your own patterns: steady
  categories via 6-month median, lumpy ones (insurance, tax, car) via
  same-month-last-year.
- `spesen` — business travel expenses matched against employer
  reimbursements (per-receipt amount matching with grace period).
- `stichtag` — net-worth statement for a historic date, computed backwards
  from current balances (useful for legal/tax cut-off dates).
- `report` — German PDF report via LuaLaTeX (Work Sans, Swiss typographic
  canon): balances, income/expenses, category pivots, focus categories.
- `export-xlsx` — a full LibreOffice/Excel workbook: dashboard, contracts &
  subscriptions (auto-detected from recurring bookings, grouped, with
  merge logic for renamed/repriced contracts), expenses, budget, forecast,
  income tax projection, and one sheet per year with KPIs, monthly trend,
  anomaly drill-down and top merchants. Regenerate any time — the workbook
  is an artifact of the database, never hand-edited.

### Web UI (`web`)

- FastAPI + Jinja + Tailwind, dark mode, single-user login (PBKDF2 hash,
  signed session cookies; `web-password` to set).
- **Dashboard** with net worth, month KPIs and a *projected* month saldo:
  the expected salary run (configurable, or auto-derived as the median of
  recent salary months) is factored in until it actually books.
- **Bookings** with full-text/account/category/date filters, pagination,
  inline category dropdown (auto-save), and technical/neutral rows hidden
  by default.
- **Analysis** page with interactive ECharts: income vs. expenses (with
  toggles for one-off effects and what-if scenarios), saving/dissaving
  plus cumulative, capital *withdrawal* and capital *inflow* scenario
  projections (parameterized, multiple dated inflows), a financing
  calculator, and stacked category trends. Colorblind-safe palette,
  validated against the dark surface.
- **Contracts & subscriptions** and **settings** (category CRUD + groups,
  dashboard/salary settings).
- **Docker**: `docker build -t kassensturz .` then run with your data
  mounted (`-v ~/.kassensturz:/data -e KASSENSTURZ_WEB_PASSWORD=…`).
  Data never goes into the image; bank syncs with 2FA stay on the host.

## Quickstart (CSV only — no bank setup needed)

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/mcschied-dev/Kassensturz && cd Kassensturz
uv sync
uv run kassensturz --help

# 1. Export a CSV from your bank's web UI, then:
uv run kassensturz import-csv umsaetze.csv --profile dkb --account giro

# 2. Copy rules.example.yaml → rules.yaml, adapt to your merchants, then:
uv run kassensturz categorize --rules rules.yaml

# 3. Verify and look at the result:
uv run kassensturz check
uv run kassensturz summary --by-year
uv run kassensturz export-xlsx        # full workbook in reports/
```

The database lives at `~/.kassensturz/kassensturz.db` by default
(override with `--db` or `KASSENSTURZ_DB`).

## Bank sync setup

```bash
cp config.example.yaml ~/.kassensturz/config.yaml   # then edit
uv run kassensturz pin <connection>   # store PIN/token in the system keychain
uv run kassensturz sync               # all connections; or: sync -c dkb
uv run kassensturz accounts           # balances overview with EUR total
```

Per connector:

- **FinTS (Sparkasse & co.)** — you need your institute's FinTS endpoint
  URL (see comments in `config.example.yaml`) and a **registered FinTS
  product ID** (mandatory, free, via
  [Deutsche Kreditwirtschaft](https://www.fints.org/de/hersteller/produktregistrierung);
  typically approved within days). First sync triggers one app
  confirmation; afterwards the persisted client state avoids repeated SCA.
  Deeper history: `sync -c <name> --days 400` (triggers SCA once).
- **DKB** — username in the config, password via `pin dkb`; confirm each
  sync in the DKB app. No product ID needed.
- **Wise** — personal API token via `pin wise`; balances only for EEA.
- **Trade Republic** — phone number in the config, PIN via `pin tr`;
  2FA once, then the session is reused.

Until a product ID arrives (or for banks without usable APIs), the CSV
profiles cover the gap — including credit-card portals and PayPal's
activity export, which slot into the consolidated ledger as detail
sources.

## Rules

```yaml
categories:
  - name: Lebensmittel
    counterparty: ["rewe", "edeka", "aldi"]
  - name: Gehalt
    purpose: ["gehalt", "lohn"]
  - name: PayPal-Funding (neutral)
    counterparty: ["paypal"]
    account: ["^giro$"]        # optional: restrict a rule to accounts
transfers:
  category: Umbuchung
  max_days: 3
```

Patterns are case-insensitive regexes; first match wins, so order matters.
Comments in your `rules.yaml` double as documentation of one-off manual
decisions. `rules.yaml` is gitignored — your merchant list is personal
data.

## Web UI

```bash
uv run kassensturz web-password       # once
uv run kassensturz web                # http://127.0.0.1:8420
```

Or via Docker (UI only — keep syncs on the host, banking 2FA doesn't
belong in a container):

```bash
docker build -t kassensturz .
docker run -p 8420:8420 -v ~/.kassensturz:/data \
  -e KASSENSTURZ_WEB_PASSWORD=… kassensturz
```

## PDF reports

`report` needs a TeX Live with `latexmk` + LuaLaTeX and the Work Sans
font installed. Everything else (XLSX, web, CLI) has no TeX dependency.

## Design

```text
connectors (fints, dkb, wise, tr, csv)  →  canonical Transaction model
                                        →  SQLite (append-only + dedup + audit log)
                                        →  categorize (rules → transfers → funding twins)
                                        →  suggest (history → ML → human review)
                                        →  check (invariants, exit 1 on warnings)
                                        →  reports (PDF), workbook (XLSX), web UI
```

One canonical transaction shape; everything downstream only sees that.
Chat/AI integration is intentionally *not* built in — the CLI is the
interface, and an AI agent (e.g. a Claude Code skill) drives it the same
way it drives any other CLI.

## Data & security

- All data stays local (SQLite in your home directory).
- Bank credentials are **never** in config files — they live in the system
  keychain (`keyring`).
- Real data (database, `rules.yaml`, `config.yaml`, `budget.yaml`, CSV
  exports, reports) is gitignored; test fixtures are synthetic only.
  Keep it that way in PRs.
- The database is an analysis tool, not evidence — archive your original
  bank exports; imports are idempotent and reproducible.

## Development

```bash
uv run pytest -q        # test suite, no network, synthetic fixtures
```

## License

MIT
