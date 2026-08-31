# resume-sync

Automate job-application resume tracking.

`resume-sync.py` polls Gmail (through the authenticated `composio` CLI) for
applicant-tracking-system (ATS) confirmation emails, then snapshots your current
resume into a `Company_Position` branch of your Resume repository and records
each application in `applications.json`.

## What it does

- **Detects applications** – Scans Gmail for ATS confirmation emails from common
  platforms (Workday, Greenhouse, Ashby, Lever, SmartRecruiters, etc.).
- **Parses company & role** – Extracts the company and position from the email
  subject/body with best-effort heuristics.
- **Snapshots your resume** – For each new application (`--snapshot`), creates a
  `Company_Position` branch holding the resume as it was when you applied.
- **Classifies status** – Infers whether an email is an application, an
  interview invite, a rejection, or an offer.
- **Maintains a ledger** – Records every application (company, role, date, URL,
  status, branch, Gmail IDs) in `applications.json`.
- **Reconciles** – `--reconcile` records ledger entries for applications that
  don't yet have a branch.

## Usage

```bash
# Snapshot each new application to a Company_Position branch + update the ledger
python3 resume-sync.py --snapshot --reconcile --since-days 21

# Dry-run: show what would happen without touching git or the ledger
python3 resume-sync.py --snapshot --dry-run
```

All Gmail access goes through the `composio execute` subcommand, reusing your
existing connected account — no separate OAuth setup.

### Environment overrides

| Variable | Purpose | Default |
|----------|---------|---------|
| `RESUME_REPO` | path to the resume repository | `/mnt/d/Documents/Resume` |
| `LEDGER_PATH` | path to `applications.json` | `./applications.json` |
| `RESOLVE_URLS=1` | fetch full email bodies to extract posting URLs | off |

`sync.sh` is a thin wrapper that adds a single-instance lock and sane PATH /
logging, then runs the script with a default 21-day lookback window.

## Observability (OpenTelemetry)

`resume-sync` emits OpenTelemetry **traces**, **metrics**, and **structured
logs** to a local OpenTelemetry Collector. This is entirely optional and
off-by-default: if the OTel packages are not installed and/or `OTEL_CONFIG_FILE`
is unset, the script runs exactly as before (plain stdout logging).

### Setup

```bash
# 1. Install the OTel dependencies (in a venv to keep system Python clean)
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. Start the local collector (receives OTLP/gRPC :4317, OTLP/HTTP :4318)
docker compose up -d

# 3. Run via the wrapper (sets OTEL_CONFIG_FILE automatically)
./sync.sh
```

### What is emitted

- **Traces** — one root span per run (`resume_sync.run`) with child spans for the
  Gmail fetch (`resume_sync.gmail_fetch`), each processed application
  (`resume_sync.application_process`), and branch snapshots
  (`resume_sync.git_snapshot`).
- **Metrics** —
  - `resume_sync.emails_fetched` (counter)
  - `resume_sync.applications_new` (counter)
  - `resume_sync.applications_skipped` (counter, with `reason`)
  - `resume_sync.applications_received` (counter, broken down by `status`:
    applied / rejected / interview / offer)
  - `resume_sync.ledger_size` (gauge)
- **Logs** — the existing `log(...)` lines are forwarded as OTel log records
  (in addition to stdout), carrying attributes like company/position/status.

### Configuration

- `otel-config.yaml` — declarative SDK config (declarative via
  `opentelemetry.configuration`). Uses synchronous exporters/flush so nothing is
  lost when the short-lived cron process exits.
- `otel-collector-config.yaml` + `docker-compose.yml` — local collector. By
  default it prints telemetry to the console via the `debug` exporter so you can
  verify locally. The config has commented-out hooks (`otlphttp/tempo`,
  `prometheusremotewrite`, `otlphttp/backend`) to wire a real backend later —
  e.g. a local Grafana + Tempo dashboard that reads straight from this
  collector's OTLP endpoint, or Prometheus.

### Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `OTEL_CONFIG_FILE` | path to the declarative SDK config | (unset → OTel disabled) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | collector base URL | `http://localhost:4318` |

Set `OTEL_CONFIG_FILE` to disable observability — just leave it unset.

## Notes

- `applications.json` is **not** committed to this repository — it contains
  personal application records and is excluded via `.gitignore`.
- This project was **AI / vibe-coded** with assistance from an AI coding agent.
