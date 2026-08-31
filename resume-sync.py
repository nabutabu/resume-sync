#!/usr/bin/env python3
"""
resume-sync.py - Automate job-application resume tracking.

Polls Gmail (via the authenticated `composio` CLI) for applicant-tracking-system
(ATS) confirmation emails, then snapshots your current resume into a
`Company_Position` branch of your Resume repository and records the application
in applications.json.

All Gmail access goes through the `composio execute` subcommand, which reuses
your existing connected account - no separate OAuth setup.

Modes (can be combined):
  --snapshot    create/commit/push a `Company_Position` branch for each newly
                detected application whose resume is in the working tree.
  --reconcile   ledger-only: record applications that have no branch yet
                (uses the resume already committed on main as the source).
  --dry-run     print what would happen without touching git or the ledger.

Environment overrides:
  RESUME_REPO   path to the resume repository      (default /mnt/d/Documents/Resume)
  LEDGER_PATH   path to applications.json          (default ./applications.json)
  RESOLVE_URLS=1  fetch full email bodies to extract posting URLs for new apps

Observability (OpenTelemetry):
  Emits traces, metrics, and structured logs via the local OpenTelemetry
  Collector (see otel-config.yaml + docker-compose.yml). Enable by setting
  OTEL_CONFIG_FILE=/path/to/otel-config.yaml (and, optionally,
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318).

      OTEL_CONFIG_FILE        declarative SDK config (e.g. ./otel-config.yaml)
      OTEL_EXPORTER_OTLP_ENDPOINT   collector base URL (default http://localhost:4318)

  If OTEL_CONFIG_FILE is unset, or the OpenTelemetry packages are not
  installed, the script runs exactly as before (plain stdout logging) -- OTel
  is strictly optional and never a failure point for the cron.
"""

import argparse
import datetime
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.environ.get("LEDGER_PATH", os.path.join(SCRIPT_DIR, "applications.json"))
RESUME_REPO = os.environ.get("RESUME_REPO", "/mnt/d/Documents/Resume")
RESUME_FILES = ["NavyaSharma_Resume.docx", "NavyaSharma_Resume.pdf"]
BASE_REF = "origin/main"

ATS_DOMAINS = (
    "ashbyhq.com", "greenhouse-mail.io", "greenhouse.io", "myworkday.com",
    "dayforce.com", "teamtailor-mail.com", "smartrecruiters.com", "lever.co",
    "workable.com", "jobs.netflix.com", "careers.stripe.com",
    "recruiting.datadoghq.com", "oracle.com",
)

SUBJECT_TERMS = [
    "thank you for applying", "thanks for applying", "your application",
    "application received", "application submitted", "successful application",
    "thank you for your application", "thanks for your application",
    "application confirmation",
]

NOISE_TERMS = [
    "profile is popular", "waiting for your response", "want to connect",
    "connect on linkedin", "connect with", "reset your password",
    "two-factor", "oauth application", "rebate program",
    "security is coming", "added to your account",
]

REJECT_TERMS = [
    "decided not to move forward", "not moving forward",
    "no longer under consideration", "chosen to move forward with other candidates",
    "went in a direction", "will not be moving forward",
    "have chosen to move forward with candidates", "position has been filled",
    "incredibly difficult decision", "another candidate has been selected",
    "we are no longer", "no longer being considered", "not to proceed",
]

POSITIVE_TERMS = ["phone screen", "technical screen", "interview", "recruiter call"]
OFFER_TERMS = ["offer letter", "we would like to extend", "official offer"]

# ATS templates whose confirmation e-mails carry (company, role) text
BODY_COMPANY_PATTERNS = [
    # "application to/with X" / "apply to X" / "interest in (joining) X" -> X is the company
    r"(?:your )?application\s+(?:to|with)\s+(?P<comp>[A-Z0-9][A-Za-z0-9 .&'\-]{1,30})(?=\s|[,.!])",
    r"(?:apply|applied|applying)\s+to\s+(?:working at |work at |the )?(?P<comp>[A-Z0-9][A-Za-z0-9 .&'\-]{1,30})(?=\s|[,.!])",
    r"interest in\s+(?:joining\s+|the\s+)?(?P<comp>[A-Z0-9][A-Za-z0-9 .&'\-]{1,30})(?=\s|[,.!])",
    # "role of X at Y" / "X role at Y" / "for Y at X" -> X is the company
    r"(?:role|position|opening|opportunity)[^.!]{0,14}(?:at|with|for)\s+(?!our\b|us\b|a\b|an\b|the\s+team\b|the\s+role|the\s+position)(?P<comp>[A-Z0-9][A-Za-z0-9 .&'\-]{1,30})(?=\s|[,.!])",
]

# Position extraction, tried in order on the decoded email text.
POSITION_PATTERNS = [
    r"for the role of\s+(?P<pos>[^.!]{2,90}?)\s+(?:\([^)]*\)\s*)?(?:at\s+[A-Z]|\bposition\b|\.|,|$)",
    r"(?:applying|apply|application|applied)\s+(?:for|to)\s+(?:the\s+)?(?P<pos>[A-Za-z][^.!]{2,90}?)\s+role\b",
    r"(?:for the|for our|to the|to our)\s+(?P<pos>[A-Za-z][^.!]{2,90}?)\s+(?:role|position|opening)\b",
    r"(?:interest|interested)\s+in\s+the\s+(?P<pos>[A-Za-z][^.!]{2,90}?)\s+(?:role|position|opening)\b",
]

STATUS_CACHE = {}


# --------------------------------------------------------------------------- #
# OpenTelemetry (optional) bootstrap
# --------------------------------------------------------------------------- #
# OTel is strictly optional. If OTEL_CONFIG_FILE is set and the OpenTelemetry
# packages are importable, we load the declarative config and wire up traces,
# metrics, and OTel log records. Otherwise the script behaves exactly as before
# (plain stdout logging) and the cron never breaks because of OTel.

_OTEL_READY = False
_tracer = None
_meter = None


def _otel_modules():
    """Return True if the OpenTelemetry packages we need are importable."""
    try:
        import opentelemetry.configuration  # noqa: F401
        return True
    except Exception:
        return False


def _otel_logging_handler():
    """Best-effort import of the OTel LoggingHandler (path varies by version)."""
    from opentelemetry._logs import get_logger_provider
    provider = get_logger_provider()
    for mod_name in ("opentelemetry.sdk._logs._internal", "opentelemetry.sdk._logs"):
        try:
            mod = __import__(mod_name, fromlist=["LoggingHandler"])
            handler = mod.LoggingHandler(level=logging.INFO, logger_provider=provider)
            handler.setFormatter(logging.Formatter("%(message)s"))
            return handler
        except Exception:
            continue
    return None


def init_otel(config_file):
    """Load declarative OTel config and set global trace/meter providers.

    Called once at startup. Never raises: on any failure OTel stays disabled.
    """
    global _OTEL_READY, _tracer, _meter
    if not config_file or not os.path.exists(config_file):
        return
    if not _otel_modules():
        log("otel: packages not installed; telemetry disabled")
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.configuration import configure_sdk, load_config_file

        config = load_config_file(config_file)
        configure_sdk(config)
        # Wire Python's standard logging into the OTel logger provider so the
        # existing log() calls become OTel log records (as well as stdout).
        _install_otel_log_handler()
        _tracer = trace.get_tracer("resume-sync")
        _meter = metrics.get_meter("resume-sync")
        _OTEL_READY = True
        log("otel: telemetry enabled")
    except Exception as exc:  # pragma: no cover - defensive
        log(f"otel: init failed ({exc}); telemetry disabled")
        _OTEL_READY = False


def _install_otel_log_handler():
    handler = _otel_logging_handler()
    if handler is not None:
        # Attach to the "resume-sync" logger; its records are forwarded to OTel.
        logging.getLogger("resume-sync").addHandler(handler)


def otel_shutdown():
    """Flush and shut down OTel providers (call on exit). Never raises."""
    if not _OTEL_READY:
        return
    try:
        for getter in (lambda: getattr(_tracer, "provider", None),
                       lambda: getattr(_meter, "provider", None)):
            provider = getter()
            if provider is not None and hasattr(provider, "shutdown"):
                provider.shutdown()
    except Exception:  # pragma: no cover - defensive
        pass


def start_span(name, **attrs):
    """Start a span (no-op when OTel disabled)."""
    if not _OTEL_READY or not _tracer:
        return _Span()
    return _Span(_tracer.start_as_current_span(name, attributes=attrs or None))


class _Span:
    """Wraps an OTel span; safe no-op when telemetry is off."""
    def __init__(self, span=None):
        self._span = span
        self._ctx = None

    def __enter__(self):
        if self._span is not None:
            self._ctx = self._span.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._span is not None:
            self._span.__exit__(exc_type, exc, tb)
        return False

    def set_attribute(self, key, value):
        if self._span is not None:
            try:
                self._span.set_attribute(key, value)
            except Exception:
                pass


def increment(counter, amount=1, **attrs):
    """Increment a Counter by name (no-op when OTel disabled)."""
    if not _OTEL_READY or not _meter:
        return
    try:
        c = _meter.create_counter(counter)
        c.add(amount, attributes=attrs or None)
    except Exception:
        pass


def set_gauge(name, value, **attrs):
    """Set a gauge value by adding the delta to an UpDownCounter (no-op otherwise)."""
    if not _OTEL_READY or not _meter:
        return
    try:
        g = _meter.create_up_down_counter(name)
        g.add(value, attributes=attrs or None)
    except Exception:
        pass


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
    logging.getLogger("resume-sync").info(msg)


def sh(args, cwd=None, check=True, capture=True):
    """Run a command; returns (code, stdout, stderr)."""
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        env=dict(os.environ, PATH=f"{os.path.expanduser('~/.local/bin')}:{os.environ.get('PATH','')}"),
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"cmd failed ({proc.returncode}): {' '.join(args)}\n{proc.stderr.strip()}")
    return proc.returncode, proc.stdout, proc.stderr


def composio_execute(slug, data):
    returncode, out, err = sh(["composio", "execute", slug, "-d", json.dumps(data)], check=False)
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"composio {slug} returned non-JSON:\n{out[:500]}{err[:500]}")
    if not payload.get("successful"):
        raise RuntimeError(f"composio {slug} failed: {payload.get('error') or err[:500]}")
    return payload.get("data") or {}


def composio_fetch_page(query, page_token="", page_size=30):
    """Single Gmail fetch page via the CLI.

    NOTE: the composio CLI returns an empty list (no error) when max_results is
    too large for a large result set, so we keep pages modest and paginate via
    nextPageToken.
    """
    data = {"query": query, "max_results": page_size, "user_id": "me",
            "verbose": False, "include_payload": False}
    if page_token:
        data["page_token"] = page_token
    return composio_execute("GMAIL_FETCH_EMAILS", data)


def fetch_emails(query, page_size=30, max_pages=80):
    """Paginated Gmail fetch via CLI. Returns list of message dicts."""
    messages, token, pages = [], "", 0
    while True:
        result = composio_fetch_page(query, page_token=token, page_size=page_size)
        messages.extend(result.get("messages") or [])
        token = result.get("nextPageToken") or ""
        pages += 1
        if not token or pages >= max_pages:
            break
    return messages


def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return []
    try:
        with open(LEDGER_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"ledger unreadable at {LEDGER_PATH}: {exc}")


def save_ledger(entries):
    with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
        fh.write("\n")


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def words(s):
    return [w for w in re.findall(r"[A-Za-z0-9]+", s or "")]


def decode(text):
    return html.unescape(text or "")


def msg_date(msg, tz=datetime.timezone.utc):
    try:
        dt = datetime.datetime.fromisoformat(msg.get("messageTimestamp", "").replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.datetime.now(tz)
    return dt.astimezone(tz).date().isoformat()


# --------------------------------------------------------------------------- #
# classification & parsing
# --------------------------------------------------------------------------- #
def is_noise(msg):
    text = decode((msg.get("subject") or "") + " " + (msg.get("preview") or {}).get("body", ""))
    low = text.lower()
    if "linkedin" in (msg.get("sender") or "").lower():
        return True
    return any(t in low for t in NOISE_TERMS)


def is_application_email(msg):
    subject = (msg.get("subject") or "").lower()
    body = (msg.get("preview") or {}).get("body", "")
    text = subject + " " + body.lower()
    if subject.startswith("re:"):  # recruiter back-and-forth, not a new app
        return False
    keywords = ["applying to", "applying for", "application", "applied"]
    return any(k in text for k in keywords)


def infer_status(msg):
    text = decode((msg.get("subject") or "") + " " + (msg.get("preview") or {}).get("body", "")).lower()
    if any(t in text for t in REJECT_TERMS):
        return "rejected"
    if any(t in text for t in OFFER_TERMS):
        return "offer"
    if any(t in text for t in POSITIVE_TERMS):
        return "interview"
    return "applied"


_DEKOR = re.compile(
    r"\s*[-–]?\s*(Hiring Team|Talent Team|Talent Acquisition|Talent|Recruiting Team|Recruiting|"
    r"Recruitment|Careers|Career|Notifications|Notification|Job Alerts|Jobs|Found|Source|Team|"
    r"Corp|Inc|LLC|Ltd|Group|Technologies|Technology|Co|Careers Team|Staffing)\s*$",
    re.I,
)
_GREETING = re.compile(r"\s+(?:hi|hello|dear|thanks|thank you)\s+[A-Z]", re.I)
_GENERIC_LOCAL = re.compile(r"^(noreply|no-reply|donotreply|do_not_reply|otp|notifications|notification|hello|hi|admin|info|support|careers|jobs|job|recruiter|staffing|updates|mail|postmaster|notify)$", re.I)
_POS_STOP = re.compile(r"^(the|a|an|our|your|this|that|for|at|to|role|position|opening|job|following)$", re.I)
_SENT_START = re.compile(r"\s+(?:we|our|you|your|they|their|it|hi|hello|dear|thank|thanks|appreciate|please|this|that|and|for|in|the|a|an|has|have)\b", re.I)


def _clean_comp(s):
    s = html.unescape(s or "").strip().strip('"').strip()
    s = _DEKOR.sub("", s).strip()
    s = _GREETING.split(s, 1)[0].strip()
    s = re.split(r"\s+navya\b", s, flags=re.I)[0].strip()
    # cut at a trailing clause ("X and ...", "X. We ...") that is not part of the name
    s = re.split(r"\s+(?:and|then|but)\s+", s, 1)[0].strip()
    s = re.split(r"\s+\.(?:\s|$)", s, 1)[0].strip()
    s = s.rstrip(".!;:,").strip()
    return s


def _finalize_company(c):
    c = _clean_comp(c)
    if not c:
        return c
    if "@" in c:
        m = re.match(r"^[^@<>]*?<?([^@<>]*)@([A-Za-z0-9.\-]+)", c)
        if m:
            local, domain = m.group(1), m.group(2)
            if _GENERIC_LOCAL.search(local):
                host = re.sub(r"^(us\.|www\.|mail\.|app\.|jobs\.|careers\.|hiring\.|recruiting\.|talent\.|notifications\.)", "", domain.lower())
                labels = host.split(".")
                c = labels[-2] if len(labels) >= 2 else host
            else:
                c = local
            c = _clean_comp(c)
    c = _SENT_START.split(c, 1)[0].strip()
    c = re.split(r"\.\s+[A-Z]", c, 1)[0].strip()
    c = c.rstrip(".!;:,").strip()
    if (not c or _POS_STOP.fullmatch(c) or len(c) < 2
            or re.match(r"^(our|us|my|your|their|the|a|an)\b", c, re.I)):
        return ""
    return c


def sender_company(msg):
    """Best-effort company from the From header (display name, else email)."""
    sender = msg.get("sender") or ""
    # display name before the address
    name = sender.split("<")[0].strip().strip('"').strip()
    em = re.search(r"<([^@>]+)@([A-Za-z0-9.\-]+)>", sender)
    # Workday / rippling-style "<company>@myworkday.com" => company from local part
    if em:
        local, domain = em.group(1), em.group(2)
        if "workday" in domain or "rippling" in domain:
            cand = _finalize_company(re.sub(r"[\W_]+", " ", local))
            if cand:
                return cand
    if name and name.lower() not in ("", "workday notifications", "ashby bot"):
        cand = _finalize_company(name)
        if cand:
            return cand
    if em and not _GENERIC_LOCAL.search(local):
        cand = _finalize_company(re.sub(r"[\W_]+", " ", local))
        if cand:
            return cand
    # fallback: use the registrable email domain
    if em:
        host = em.group(2).lower()
        host = re.sub(r"^(us\.|www\.|mail\.|app\.|jobs\.|careers\.|hiring\.|recruiting\.|talent\.|notifications\.)", "", host)
        labels = host.split(".")
        cand = labels[-2] if len(labels) >= 2 else host
        cand = _finalize_company(cand)
        if cand:
            return cand
    return ""


def find_company_position(msg):
    text = decode((msg.get("subject") or "") + " " + (msg.get("preview") or {}).get("body", ""))
    text = text.split("\n")[0] + " " + text  # include subject + first line of body

    company, position = "", ""
    for pat in BODY_COMPANY_PATTERNS:
        m = re.search(pat, text, re.I)
        if m and m.group("comp"):
            company = _finalize_company(m.group("comp"))
            if company:
                break
    if not company:
        company = sender_company(msg)

    for pat in POSITION_PATTERNS:
        m = re.search(pat, text, re.I)
        if m and m.group("pos"):
            pos = re.sub(r"\s+", " ", m.group("pos")).strip()
            pos = re.sub(r"\s+(at\s+[A-Z].*|role|position|opening|here at.*)$", "", pos, flags=re.I).strip()
            pos = pos.rstrip(".,;:()").strip().strip("()")
            if len(pos) >= 4 and not _POS_STOP.match(pos) and pos.lower() != "at":
                position = pos
                break

    # Workday "R23920 Platform Engineer" / "for the following position: X"
    if not position:
        m = re.search(r"position[s]?\s*:\s*(?:R\d+\s*)?(?P<pos>[A-Za-z][A-Za-z0-9 ,&'\-/]{3,70})", text, re.I)
        if m:
            position = m.group("pos").strip()

    return company, position


def posting_url(msg):
    text = decode((msg.get("subject") or "") + " " + (msg.get("preview") or {}).get("body", ""))
    for url in re.findall(r"https?://[^\s<>\"')\]]+", text):
        url = url.rstrip(".,);")
        if re.search(r"(jobs|careers|posting|board|view|/job|/position)", url, re.I):
            return url
    m = re.search(r"https?://[^\s<>\"')\]]+", text)
    return m.group(0).rstrip(".,);") if m else ""


def branch_name(company, position):
    parts = words(company) or ["company"]
    if position:
        pos = re.sub(r"\([^)]*\)", "", position, flags=re.I)  # drop parentheticals
        pos = re.sub(r"\b(role|position|opening|job)\b.*$", "", pos, flags=re.I).strip()
        pw = words(pos)[:5]
        if pw:
            parts += ["_"] + pw
    return "_".join(parts)


# --------------------------------------------------------------------------- #
# git / resume-repo operations
# --------------------------------------------------------------------------- #
def git(args, check=True):
    return sh(["git", "-C", RESUME_REPO] + args, check=check)


def existing_branches():
    _, local, _ = git(["branch", "--format=%(refname:short)"], check=False)
    _, remote, _ = git(["branch", "-r", "--format=%(refname:short)"], check=False)
    return [b.replace("origin/", "") for b in (local + "\n" + remote).splitlines()
            if b and b != "HEAD"]


def find_existing_branch(company, position, branches, threshold=0.5):
    cn, pn = norm(company), [norm(t) for t in words(position)]
    best, best_score = None, -1.0
    for b in branches:
        bn = norm(b)
        if cn and cn not in bn:
            continue
        if not pn:
            score = 1.0
        else:
            matched = sum(1 for t in pn if t and t in bn)
            score = matched / len(pn)
        if score > best_score:
            best, best_score = b, score
    return best if best_score >= threshold else None


def restore_worktree(snapshot_dir):
    for f in RESUME_FILES:
        src = os.path.join(snapshot_dir, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(RESUME_REPO, f))


def snapshot_to_branch(entry):
    """Create a branch from origin/main holding the current working-tree resume."""
    branch = entry["branch"]
    # never clobber an existing branch (local or remote)
    branches = existing_branches()
    if branch in branches:
        log(f"  snapshot skipped: branch '{branch}' already exists")
        return False
    orig_head = "main"
    _, out, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if out.strip() and out.strip().lower() != "head":
        orig_head = out.strip()

    snapshot_dir = tempfile.mkdtemp(prefix="resume-snap-")
    with start_span("resume_sync.git_snapshot",
                    branch=branch,
                    company=entry["company"],
                    position=entry["position"] or "") as git_span:
        try:
            missing = [f for f in RESUME_FILES if os.path.exists(os.path.join(RESUME_REPO, f))]
            for f in missing:
                shutil.copy2(os.path.join(RESUME_REPO, f), os.path.join(snapshot_dir, f))

            git(["fetch", "origin", "--prune"])
            git(["checkout", "-B", branch, BASE_REF])
            restore_worktree(snapshot_dir)
            msg = f"Applied: {entry['company']} - {entry['position'] or entry['branch']}"
            git(["add", "--"] + RESUME_FILES)
            git(["commit", "-m", msg])
            pushed = True
            try:
                git(["push", "-u", "origin", branch])
            except RuntimeError:
                pushed = False
                log(f"  WARNING: branch '{branch}' committed but push failed - fix manually")
            git_span.set_attribute("pushed", pushed)
            log(f"  created + pushed branch '{branch}' ({msg})")
            return True
        finally:
            # always return to the original branch and restore the tailored files
            try:
                git(["checkout", orig_head], check=False)
            except Exception:
                git(["checkout", "main"], check=False)
            restore_worktree(snapshot_dir)
            shutil.rmtree(snapshot_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_query(since_days):
    after = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=since_days)).strftime("%Y/%m/%d")
    from_clause = "from:(" + " OR ".join(ATS_DOMAINS) + f") after:{after}"
    subjects = " OR ".join(f'"{t}"' for t in SUBJECT_TERMS)
    subject_clause = f"(subject:({subjects})) after:{after}"
    return f"({from_clause}) OR ({subject_clause})"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since-days", type=int, default=14, help="look back window")
    p.add_argument("--snapshot", action="store_true", help="branch+commit new applications")
    p.add_argument("--reconcile", action="store_true", help="ledger-only entries for apps without branches")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.snapshot and not args.reconcile:
        args.snapshot = True

    init_otel(os.environ.get("OTEL_CONFIG_FILE"))

    ledg_size = 0
    try:
        with start_span("resume_sync.run",
                        snapshot=args.snapshot,
                        reconcile=args.reconcile,
                        dry_run=args.dry_run,
                        since_days=args.since_days) as run_span:
            ledg_size = _run(args)
            run_span.set_attribute("outcome", "success")
    except SystemExit:
        raise
    except Exception as exc:
        log(f"ERROR: {exc}")
        increment("resume_sync.applications_skipped", company="", reason="unhandled_error")
        raise
    finally:
        set_gauge("resume_sync.ledger_size", ledg_size)
        otel_shutdown()


def _run(args):
    ledger = load_ledger()
    known_ids = {e["thread_id"] for e in ledger if e.get("thread_id")}
    known_msgids = {e["message_id"] for e in ledger if e.get("message_id")}
    branches = existing_branches()
    since = args.since_days if (args.snapshot or args.reconcile) else 14

    log(f"scanning gmail (last {since}d); ledger={len(ledger)} entries; mode snapshot={args.snapshot} reconcile={args.reconcile} dry_run={args.dry_run}")

    with start_span("resume_sync.gmail_fetch") as fetch_span:
        emails = fetch_emails(build_query(since))
        increment("resume_sync.emails_fetched", len(emails))
        fetch_span.set_attribute("emails_fetched", len(emails))
    log(f"  fetched {len(emails)} candidate emails")

    touched = 0
    for msg in emails:
        thread, mid = msg.get("threadId"), msg.get("messageId")
        if thread in known_ids or mid in known_msgids:
            continue
        if is_noise(msg) or not is_application_email(msg):
            increment("resume_sync.applications_skipped", company="", reason="not_application")
            continue
        company, position = find_company_position(msg)
        if not company:
            log(f"  SKIP (no company parsed): {msg.get('subject')}")
            increment("resume_sync.applications_skipped", company="", reason="no_company_parsed")
            continue
        status = infer_status(msg)
        proposed_branch = branch_name(company, position)
        match = find_existing_branch(company, position, branches) or (
            proposed_branch if proposed_branch in branches else None)

        with start_span("resume_sync.application_process",
                        company=company, position=position, status=status) as app_span:
            entry = {
                "company": company,
                "position": position,
                "applied_date": msg_date(msg),
                "url": posting_url(msg),
                "req_id": "",
                "branch": match,
                "source": "gmail",
                "resume_source": "main",
                "status": status,
                "ats": ((msg.get("sender") or "").split("@")[-1].rstrip(">")),
                "thread_id": thread,
                "message_id": mid,
            }

            if match:
                # first time a thread for this application shows up: record it;
                # later threads just refresh the status.
                prior = next((e for e in ledger if e.get("branch") == match), None)
                entry["branch"], entry["resume_source"] = match, "branch"
                if prior:
                    if prior["status"] != status:
                        prior["status"] = status
                        log(f"  updated status '{status}' for existing branch '{match}'")
                        touched += 1
                    else:
                        log(f"  already tracked (branch {match}): {company} - {position} [{status}]")
                    continue
                log(f"  [reconcile] recorded existing branch {match}: {company} - {position} [{status}]")
            elif args.snapshot:
                entry["branch"], entry["resume_source"] = proposed_branch, "worktree"
                if proposed_branch in branches:
                    log(f"  SKIP shadowed branch name '{proposed_branch}' for {company} - {position}; use --reconcile")
                    increment("resume_sync.applications_skipped", company=company, reason="shadowed_branch")
                    continue
                if args.dry_run:
                    log(f"  [dry-run] would create branch {proposed_branch} for {company} - {position}")
                    touched += 1
                    continue
                if not snapshot_to_branch(entry):
                    increment("resume_sync.applications_skipped", company=company, reason="snapshot_failed")
                    continue
            else:
                log(f"  [reconcile] ledger-only: {company} - {position}")
                if args.dry_run:
                    touched += 1
                    continue

            ledger.append(entry)
            touched += 1
            increment("resume_sync.applications_new", company=company, position=position)
            increment("resume_sync.applications_received", company=company, status=status)
            app_span.set_attribute("outcome", "recorded")

    if touched and not args.dry_run:
        save_ledger(sorted(ledger, key=lambda e: (e.get("applied_date") or "", e.get("company") or "")))
        log(f"saved ledger ({len(ledger)} entries, +{touched})")
    elif args.dry_run:
        log(f"dry-run: would add {touched} entries")

    return len(ledger)


if __name__ == "__main__":
    main()