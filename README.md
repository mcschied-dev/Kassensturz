# kassensturz

Local-first personal finance reporting for German banks. Pull transactions
via FinTS/HBCI (DKB, Sparkasse) and Trade Republic, categorize capital
flows with simple YAML rules, and generate German LaTeX/PDF reports —
all on your own machine. No cloud, no third-party aggregator.

> **Kassensturz** *(German)* — counting up what's actually in the till.

## Status

Early. Working today:

- **FinTS/HBCI sync** (`sync`) — pull transactions directly from your bank
  (Sparkasse, ...). PINs live in the system keychain, client state is
  persisted per connection so SCA is only triggered when the bank insists.
  Decoupled TAN (pushTAN: confirm in the app) and classic TAN supported.
- **DKB sync** via the bank's web/REST API (`dkb-robo`) — checking accounts
  and credit cards, no FinTS product ID required; confirm login in the
  DKB app.
- **CSV import** for DKB ("neues Banking" export), Sparkasse (CAMT-CSV),
  and a generic format — with encoding/format quirks handled
- **SQLite store** with safe dedup (re-importing overlapping exports is a no-op)
- **Rule-based categorization** (YAML, first match wins)
- **Transfer matching** — opposite-sign bookings between your own accounts
  are paired as internal transfers, so cashflow stats aren't polluted
- **CLI**: `sync`, `pin`, `import-csv`, `list`, `categorize`, `summary`,
  `accounts`, `export-csv`

Planned (in order): Trade Republic via `pytr` · LaTeX report templates
(German) · XLSX export · AI-agent skill.

### FinTS setup

```bash
cp config.example.yaml ~/.kassensturz/config.yaml   # then edit
kassensturz pin spk        # store PIN in the system keychain
kassensturz sync           # first run picks the TAN mechanism interactively
```

You need your institute's FinTS endpoint URL (see comments in
`config.example.yaml`) and a **registered FinTS product ID** — mandatory,
no fallback exists. Registration is free via
[Deutsche Kreditwirtschaft](https://www.fints.org/de/hersteller/produktregistrierung);
until it arrives, use your bank's CSV export with `import-csv`.

## Quickstart

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run kassensturz --help

# 1. Export CSV from your bank's web UI, then:
uv run kassensturz import-csv umsaetze.csv --profile dkb --account dkb-giro

# 2. Copy rules.example.yaml → rules.yaml, adapt, then:
uv run kassensturz categorize --rules rules.yaml

# 3. Look at the result:
uv run kassensturz summary --year 2026 --month 6
uv run kassensturz export-csv out.csv        # for Excel & friends
```

The database lives at `~/.kassensturz/kassensturz.db` by default
(override with `--db` or `KASSENSTURZ_DB`).

## Design

```text
connectors (csv, fints, pytr)  →  canonical Transaction model
                               →  SQLite (append-only + dedup)
                               →  categorize (YAML rules + transfer matching)
                               →  reports (LaTeX/PDF), exports (CSV/XLSX)
```

One canonical transaction shape; everything downstream only sees that.
Chat/AI integration is intentionally *not* built in — the CLI is the
interface, and an AI agent (e.g. a Claude Code skill) drives it, the same
way it drives any other CLI.

## Security

- All data stays local (SQLite file in your home directory).
- Bank credentials are **never** stored in config files — FinTS sync will
  use the system keychain (`keyring`).
- Test fixtures contain only synthetic data. Keep it that way in PRs.

## License

MIT
