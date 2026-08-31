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

## Notes

- `applications.json` is **not** committed to this repository — it contains
  personal application records and is excluded via `.gitignore`.
- This project was **AI / vibe-coded** with assistance from an AI coding agent.
