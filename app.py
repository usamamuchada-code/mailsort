#!/usr/bin/env python3
"""
MailSort web app – runs on your own PC. Open http://localhost:5000

  • Upload a bulk scan PDF  → sorted, split, matched, emails drafted (runs in the background)
  • Upload / update the client database (CSV)
  • Review each batch, download the per-client PDFs, send the notification emails

Start:   python app.py            (first run: open Settings and enter your API key + email details)
Data:    everything is kept in ./data  (clients.csv, config.json, batches/<batch>/...)
"""
from __future__ import annotations

import csv, io, json, os, smtplib, socket, threading, urllib.error, uuid, datetime as dt
from email.message import EmailMessage
from pathlib import Path

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template_string, request,
                   send_from_directory, session, url_for)
from werkzeug.utils import secure_filename

import mailsort as ms

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")   # on Railway: the mounted volume, e.g. /data
BATCHES = DATA / "batches"
CLIENTS_CSV = DATA / "clients.csv"
CONFIG = DATA / "config.json"
for d in (DATA, BATCHES):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "mailsort-local")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3  # 2 GB uploads

# --- login: set MAILSORT_PASSWORD (and optionally MAILSORT_USER) to require a password on every page.
AUTH_USER = os.environ.get("MAILSORT_USER", "admin")
AUTH_PASS = os.environ.get("MAILSORT_PASSWORD", "")


app.permanent_session_lifetime = dt.timedelta(days=30)

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Startitup Mail Room – Sign in</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>*{box-sizing:border-box}body{font-family:'Hanken Grotesk',system-ui,sans-serif;margin:0;background:#fafafa;color:#111;
display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border:1px solid #e7e7e7;border-radius:14px;padding:36px;width:100%;max-width:400px;box-shadow:0 8px 30px rgba(0,0,0,.05)}
.logo{display:flex;justify-content:center;margin-bottom:6px}.logo img{height:40px}
h1{font-size:16px;font-weight:600;color:#777;text-align:center;margin:0 0 26px;letter-spacing:.03em}
label{display:block;font-size:13px;font-weight:600;margin:14px 0 6px}
input[type=text],input[type=password]{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:8px;font-size:15px}
input:focus{outline:2px solid #111;border-color:#111}
.btn{width:100%;margin-top:22px;background:#111;color:#fff;border:0;border-radius:999px;padding:12px;font-size:15px;font-weight:700;cursor:pointer}
.btn:hover{background:#333}.err{background:#fee2e2;border:1px solid #fca5a5;color:#991b1b;padding:10px 12px;border-radius:8px;font-size:14px;margin-bottom:8px}
.rem{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:14px;color:#444}
.foot{text-align:center;color:#9ca3af;font-size:12px;margin-top:22px}</style></head><body>
<form class="card" method="post" action="/login">
<div class="logo"><img src="data:image/png;base64,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" alt="Startitup"></div>
<h1>Mail Room – staff sign in</h1>
{% if error %}<div class="err">{{error}}</div>{% endif %}
<label>Username</label><input type="text" name="username" autofocus autocomplete="username">
<label>Password</label><input type="password" name="password" autocomplete="current-password">
<label class="rem"><input type="checkbox" name="remember" checked> Keep me signed in on this computer</label>
<button class="btn">Sign in</button>
<div class="foot">Startitup Business Address · authorised staff only</div>
</form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_PASS or session.get("auth"):
        return redirect("/")
    error = None
    if request.method == "POST":
        import time
        if request.form.get("username", "").strip() == AUTH_USER and request.form.get("password", "") == AUTH_PASS:
            session["auth"] = True
            session.permanent = bool(request.form.get("remember"))
            return redirect(request.args.get("next") or "/")
        time.sleep(1.5)  # slow down guessing
        error = "Wrong username or password."
    return render_template_string(LOGIN_PAGE, error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.before_request
def require_login():
    if not AUTH_PASS:
        return None  # local use – no password configured
    if request.path == "/login" or session.get("auth"):
        return None
    # legacy basic-auth still accepted (e.g. saved bookmarks/scripts)
    a = request.authorization
    if a and a.type == "basic" and a.username == AUTH_USER and a.password == AUTH_PASS:
        session["auth"] = True
        return None
    return redirect("/login?next=" + request.path)

JOBS: dict[str, dict] = {}  # batch_id -> {"msg", "frac", "done", "error"}

REQUIRED_COLS = ["client_id", "company_name", "contact_name", "email", "status", "package", "start_date", "reseller", "reseller_email", "kyc", "siu", "address"]
PACKAGES = ["Basic", "Standard", "Premium"]


# ----------------------------------------------------------------------------- config / clients

def load_config() -> dict:
    base = {"anthropic_api_key": "", "model": "claude-sonnet-4-5", "sender_name": "The Startitup Team",
            "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_user": "", "smtp_password": "",
            "from_email": "", "attach_pdfs": True}
    if CONFIG.exists():
        base.update(json.loads(CONFIG.read_text()))
    # environment variables (Railway "Variables") override anything saved in the Settings page
    env_map = {"anthropic_api_key": "ANTHROPIC_API_KEY", "model": "MAILSORT_MODEL", "sender_name": "SENDER_NAME",
               "smtp_host": "SMTP_HOST", "smtp_port": "SMTP_PORT", "smtp_user": "SMTP_USER",
               "smtp_password": "SMTP_PASSWORD", "from_email": "FROM_EMAIL"}
    for k, ev in env_map.items():
        if os.environ.get(ev):
            base[k] = os.environ[ev]
    return base


def save_config(cfg: dict):
    CONFIG.write_text(json.dumps(cfg, indent=1))


def read_clients() -> list[dict]:
    if not CLIENTS_CSV.exists():
        return []
    with open(CLIENTS_CSV, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        ms.apply_service_expiry(r)
    return rows


def write_clients(rows: list[dict]):
    with open(CLIENTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REQUIRED_COLS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def parse_client_upload(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("The file is empty.")
    # tolerate different header spellings
    alias = {"company": "company_name", "name": "company_name", "client": "company_name",
             "contact": "contact_name", "email_address": "email", "e-mail": "email",
             "id": "client_id", "account_status": "status", "subscription_status": "status",
             "plan": "package", "subscription": "package", "package_name": "package", "tier": "package",
             "start": "start_date", "service_start": "start_date", "date_joined": "start_date",
             "joined": "start_date", "renewal_date": "start_date", "subscription_start": "start_date",
             "reseller_name": "reseller", "partner": "reseller", "agent": "reseller", "introducer": "reseller",
             "kyc_done": "kyc", "kyc_status": "kyc", "kyc_completed": "kyc", "id_verified": "kyc",
             "partner_email": "reseller_email", "agent_email": "reseller_email",
             "unit": "siu", "unit_number": "siu", "siu_number": "siu", "office_number": "siu", "suite": "siu",
             "client_address": "address", "registered_address": "address", "full_address": "address"}
    out = []
    for r in rows:
        n = {}
        for k, v in r.items():
            key = (k or "").strip().lower().replace(" ", "_")
            key = alias.get(key, key)
            n[key] = (v or "").strip()
        if not n.get("company_name"):
            continue
        n.setdefault("client_id", ""); n.setdefault("contact_name", ""); n.setdefault("email", "")
        n["status"] = (n.get("status") or "active").lower()
        n["package"] = (n.get("package") or "").strip().capitalize()
        if n["package"] and n["package"] not in PACKAGES:
            n["package"] = ""
        n["siu"] = (n.get("siu") or "").strip().upper()
        kv = (n.get("kyc") or "").strip().lower()
        n["kyc"] = "yes" if kv in ("yes", "y", "done", "true", "1", "complete", "completed", "verified", "ok") \
            else ("no" if kv in ("no", "n", "pending", "false", "0", "incomplete", "not done", "outstanding") else "")
        n["reseller"] = (n.get("reseller") or "").strip()
        if n["reseller"].lower() in ("direct", "none", "-", "n/a"):
            n["reseller"] = ""
        n["start_date"] = n.get("start_date", "").strip()
        d = ms.parse_date(n["start_date"])
        if d:
            n["start_date"] = d.isoformat()   # store consistently as YYYY-MM-DD
        out.append(n)
    missing = [c for c in ("company_name", "email") if not any(x.get(c) for x in out)]
    if missing:
        raise ValueError(f"Could not find these columns: {', '.join(missing)}. "
                         f"Expected headers: {', '.join(REQUIRED_COLS)}")
    return out


# ----------------------------------------------------------------------------- batch processing

def batch_dir(bid: str) -> Path:
    p = (BATCHES / bid).resolve()
    if not str(p).startswith(str(BATCHES.resolve())):
        abort(404)
    return p


def load_batch(bid: str) -> dict | None:
    p = batch_dir(bid) / "batch.json"
    return json.loads(p.read_text()) if p.exists() else None


def save_batch(bid: str, data: dict):
    (batch_dir(bid) / "batch.json").write_text(json.dumps(data, indent=1, default=str))


def siu_blocked(b: dict, files: list[str]) -> list[str]:
    """Return letter ids among `files` that must not be downloaded (SIU office missing)."""
    return [L["letter_id"] for L in b["letters"] if L["file"] in files and not L.get("siu_ok", True)]


def mark_downloaded(bid: str, files: list[str], how: str):
    """Record that these letter files were downloaded (for the portal-upload workflow)."""
    b = load_batch(bid)
    if not b:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    for L in b["letters"]:
        if L["file"] in files:
            L["downloaded_at"] = now
            L["downloaded_how"] = how
    save_batch(bid, b)


def process_in_background(bid: str, pdf: Path, note: str):
    cfg = load_config()
    job = JOBS[bid]

    def status(msg, frac=None):
        job["msg"] = msg
        if frac is not None:
            job["frac"] = frac

    ms.TEMPLATES = load_templates()
    try:
        r = ms.run_batch(pdf, CLIENTS_CSV, batch_dir(bid), batch_tag=bid, sender_name=cfg["sender_name"],
                         api_key=cfg.get("anthropic_api_key") or None, model=cfg.get("model"), status=status)
        letters = []
        for L in r["letters"]:
            c = L.get("client") or {}
            letters.append({k: L[k] for k in ("letter_id", "pages", "recipient_company", "sender", "letter_type",
                                              "urgency", "summary", "file", "needs_review", "match_score", "siu_ok")} | {"address": L.get("address", ""), "in_package": L.get("in_package", True),
                              "unit": L.get("unit", ""), "matched_by": L.get("matched_by", ""), "unit_mismatch": L.get("unit_mismatch", False),
                              "match_state": L.get("match_state", "verified"), "match_note": L.get("match_note", ""),
                              "suggested_client": L.get("suggested_client", "")}
                           | {"client": {k: c.get(k, "") for k in REQUIRED_COLS} if c else None})
        emails = []
        for e in r["emails"]:
            missing = [L["letter_id"] for L in r["letters"] if L.get("client") and L["client"]["company_name"] == e["company"] and not L.get("siu_ok", True)]
            mismatches = [L["letter_id"] for L in r["letters"] if L.get("client") and L["client"]["company_name"] == e["company"] and L.get("unit_mismatch")]
            unverified = [L["letter_id"] for L in r["letters"] if L.get("client") and L["client"]["company_name"] == e["company"] and L.get("match_state") == "review"]
            if mismatches:
                e = {**e, "action": f"HOLD – UNIT MISMATCH on {', '.join(mismatches)} (check before sending!)"}
            elif unverified:
                e = {**e, "action": f"HOLD – match not verified on {', '.join(unverified)} (confirm the client first)"}
            pkg = next((L["client"].get("package", "") for L in r["letters"]
                        if L.get("client") and L["client"]["company_name"] == e["company"]), "")
            res = next((L["client"].get("reseller", "") for L in r["letters"]
                        if L.get("client") and L["client"]["company_name"] == e["company"]), "")
            emails.append({**e, "package": pkg, "reseller": res, "siu_missing": missing, "unit_mismatch_ids": mismatches,
                           "sent_at": None, "sent_to": None, "manual_sent_at": None})
        save_batch(bid, {"id": bid, "created": dt.datetime.now().isoformat(timespec="seconds"), "note": note,
                         "pdf": pdf.name, "pages": r["pages"], "mode": r["mode"], "summary": r["summary"],
                         "letters": letters, "emails": emails})
        job["done"] = True
    except Exception as e:  # surface the error in the UI
        job["error"] = f"{type(e).__name__}: {e}"
        job["done"] = True


# ----------------------------------------------------------------------------- editable email templates

TEMPLATES_FILE = DATA / "templates.json"

# key: (section, label, placeholder help, default)
TPL_DEFS = {
    "batch_subject": ("Daily post email (per client)", "Subject", "{n} {company}", None),
    "batch_body": ("Daily post email (per client)", "Body", "{contact} {company} {n} {date} {items} {urgent_note} {upgrade_note} {sender_name}", None),
    "urgent_note": ("Daily post email (per client)", "Urgent note (inserted when a letter is time-sensitive)", "none", None),
    "upgrade_note": ("Daily post email (per client)", "Package upgrade note (inserted when letters are outside the package)", "{n_excluded} {package}", None),
    "excluded_body": ("Daily post email (per client)", "Body when ALL letters are outside the package", "{contact} {company} {n_excluded} {package} {date} {sender_name}", None),
    "letter_subject": ("Single letter email", "Subject", "{company}", "New item of post received – {company}"),
    "letter_body": ("Single letter email", "Body", "{contact} {company} {date} {sender} {summary} {urgent_note} {sender_name}",
        "Dear {contact},\n\nWe have received an item of post for {company} ({date}). It has been scanned and is attached to this email.\n\n  From: {sender}\n  Summary: {summary}{urgent_note}\n\nYou can also view it in your client portal.\n\nKind regards,\n{sender_name}\n"),
    "issue_kyc": ("Issue paragraphs (used in issue & compliance emails)", "KYC incomplete", "{login_url}",
        "Identity verification (KYC) for your account is incomplete. Please log in to {login_url} and provide the required KYC documents."),
    "issue_siu": ("Issue paragraphs (used in issue & compliance emails)", "SIU office missing", "{n} {ids}",
        "{n} letter(s) arrived WITHOUT your SIU office in the address{ids}. Please update your registered address with all authorities, banks and vendors (HMRC, Companies House, suppliers, etc.) so that it always includes your SIU office."),
    "issue_renewal": ("Issue paragraphs (used in issue & compliance emails)", "Service expired / renewal", "{site}",
        "Your service period has expired and requires renewal. Please visit {site} and speak to our chat support to renew your subscription."),
    "issue_status": ("Issue paragraphs (used in issue & compliance emails)", "Account suspended / cancelled", "{status}",
        "Your account is currently marked as {status}. Please contact us to reactivate your service so we can continue handling your post."),
    "issue_package": ("Issue paragraphs (used in issue & compliance emails)", "Letters outside package", "{n} {package}",
        "{n} item(s) of post received are not covered by the {package} package (non-government mail). Upgrade your package to have all post scanned and emailed to you."),
    "issues_subject": ("Batch issues email (⚠ Report issues button)", "Subject", "{company}",
        "Action required – issues with your mail service ({company})"),
    "issues_body": ("Batch issues email (⚠ Report issues button)", "Body", "{contact} {date} {issues} {sender_name}",
        "Dear {contact},\n\nWhile processing your post today ({date}) we noticed the following issue(s) with your account or incoming mail that need your attention:\n\n{issues}\n\nPlease resolve the above, or reply to this email if you have any questions – we are happy to help.\n\nKind regards,\n{sender_name}\n"),
    "compliance_subject": ("Compliance notice (whole client base)", "Subject", "{company}",
        "IMPORTANT – action required on your business address service ({company})"),
    "compliance_body": ("Compliance notice (whole client base)", "Body", "{contact} {company} {issues} {site} {sender_name}",
        "Dear {contact},\n\nThis is an important notice regarding the business address service for {company}.\n\nYour incoming letters CANNOT be processed and forwarded until the following is resolved:\n\n{issues}\n\nOnce resolved, processing of your post will resume as normal. If you have any questions, please reply to this email or contact our support team via {site}.\n\nKind regards,\n{sender_name}\n"),
    "reseller_subject": ("Reseller renewal reminder", "Subject", "{n} {days}",
        "Renewals due within {days} days – {n} client(s)"),
    "reseller_body": ("Reseller renewal reminder", "Body", "{reseller} {lines} {portal} {sender_name}",
        "Dear {reseller},\n\nThe business-address service for the following client(s) of yours is due for renewal:\n\n{lines}\n\nPlease renew these subscriptions via {portal} before the due date to avoid any interruption to their mail service.\n\nKind regards,\n{sender_name}\n"),
}


def tpl_default(key: str) -> str:
    d = TPL_DEFS[key][3]
    return d if d is not None else ms.DEFAULTS[key]


def load_templates() -> dict:
    stored = {}
    try:
        stored = json.loads(TEMPLATES_FILE.read_text())
    except Exception:
        pass
    return {k: (stored.get(k) or tpl_default(k)) for k in TPL_DEFS}


def get_t(key: str) -> str:
    return load_templates()[key]


render = None  # set below after ms import is certain
render = ms.render


# ----------------------------------------------------------------------------- reseller renewal reminders

REMINDERS_FILE = DATA / "renewal_reminders.json"
REMIND_DAYS = 30
PORTAL_URL = os.environ.get("RESELLER_PORTAL_URL", "our Reseller Portal")


def load_reminders() -> dict:
    try:
        return json.loads(REMINDERS_FILE.read_text())
    except Exception:
        return {}


def save_reminders(d: dict):
    REMINDERS_FILE.write_text(json.dumps(d, indent=1))


def due_reseller_clients() -> list[dict]:
    """Reseller-assigned clients whose renewal is due within REMIND_DAYS days (or already passed)."""
    import datetime as _dt
    out = []
    today = _dt.date.today()
    for c in read_clients():
        if not (c.get("reseller") and c.get("_expiry")):
            continue
        expiry = _dt.date.fromisoformat(c["_expiry"])
        days_left = (expiry - today).days
        if days_left <= REMIND_DAYS:
            key = f"{c.get('client_id') or c['company_name']}|{c['_expiry']}"
            c["_days_left"] = days_left
            c["_rkey"] = key
            out.append(c)
    return out


def send_renewal_reminders(manual_reseller: str | None = None) -> tuple[int, list[str]]:
    """Group due clients by reseller and email each reseller once. Returns (sent, errors)."""
    cfg = load_config()
    reminders = load_reminders()
    due = [c for c in due_reseller_clients()
           if (manual_reseller is None and c["_rkey"] not in reminders) or
              (manual_reseller is not None and c["reseller"] == manual_reseller)]
    by_res: dict[str, list[dict]] = {}
    for c in due:
        by_res.setdefault(c["reseller"], []).append(c)
    sent, errors = 0, []
    for reseller, cs in by_res.items():
        to = next((c.get("reseller_email") for c in cs if c.get("reseller_email")), "")
        if not to:
            errors.append(f"{reseller}: no reseller_email in the client database – cannot send")
            continue
        def _left(c):
            return " (OVERDUE)" if c["_days_left"] < 0 else f" ({c['_days_left']} days)"
        lines = "\n".join(f"  - {c['company_name']} – renewal due {c['_expiry']}" + _left(c) for c in cs)
        fields = dict(reseller=reseller, lines=lines, portal=PORTAL_URL, days=REMIND_DAYS,
                      n=len(cs), sender_name=cfg["sender_name"])
        subject = render(get_t("reseller_subject"), **fields)
        body = render(get_t("reseller_body"), **fields)
        try:
            send_email(cfg, to, subject, body, [])
            now = dt.datetime.now().isoformat(timespec="seconds")
            for c in cs:
                reminders[c["_rkey"]] = {"sent_at": now, "to": to, "reseller": reseller}
            sent += len(cs)
        except Exception as ex:
            errors.append(f"{reseller}: {ex}")
    save_reminders(reminders)
    return sent, errors


_reminder_thread_started = False


def _reminder_loop():
    import time
    time.sleep(60)  # let the app settle after deploy
    while True:
        try:
            sent, errors = send_renewal_reminders()
            if sent or errors:
                print(f"[renewals] sent reminders for {sent} client(s); errors: {errors}", flush=True)
        except Exception as ex:
            print(f"[renewals] check failed: {ex}", flush=True)
        time.sleep(24 * 3600)


@app.before_request
def _start_reminder_thread():
    global _reminder_thread_started
    if not _reminder_thread_started:
        _reminder_thread_started = True
        threading.Thread(target=_reminder_loop, daemon=True).start()


# ----------------------------------------------------------------------------- compliance notices (whole client base)

NOTICES_FILE = DATA / "compliance_notices.json"
LOGIN_URL = "https://startitup.global/account/login"
SITE_URL = "www.startitup.global"


def siu_missing_by_company() -> dict:
    """company -> number of letters across ALL batches that arrived without the SIU office."""
    counts: dict[str, int] = {}
    if BATCHES.exists():
        for d in BATCHES.iterdir():
            b = load_batch(d.name) if d.is_dir() else None
            for L in (b or {}).get("letters", []):
                c = L.get("client")
                if c and not L.get("siu_ok", True):
                    counts[c["company_name"]] = counts.get(c["company_name"], 0) + 1
    return counts


def compliance_issues(c: dict, siu_counts: dict) -> list[str]:
    issues = []
    if (c.get("kyc") or "").lower() == "no":
        issues.append(render(get_t("issue_kyc"), login_url=LOGIN_URL))
    if (c.get("status") or "").lower() == "overdue":
        issues.append(render(get_t("issue_renewal"), site=SITE_URL))
    n = siu_counts.get(c.get("company_name", ""), 0)
    if n:
        issues.append(render(get_t("issue_siu"), n=n, ids=""))
    return issues


def compliance_list() -> list[dict]:
    siu_counts = siu_missing_by_company()
    notices = json.loads(NOTICES_FILE.read_text()) if NOTICES_FILE.exists() else {}
    out = []
    for c in read_clients():
        issues = compliance_issues(c, siu_counts)
        if issues:
            c["_issues"] = issues
            c["_last_notice"] = (notices.get(c["company_name"]) or {}).get("sent_at", "")[:16].replace("T", " ")
            out.append(c)
    return out


def compliance_email(c: dict, cfg: dict) -> tuple[str, str]:
    lines = "\n\n".join(f"{i}. {x}" for i, x in enumerate(c["_issues"], 1))
    fields = dict(company=c["company_name"], contact=c.get("contact_name") or "Client",
                  issues=lines, site=SITE_URL, sender_name=cfg["sender_name"])
    return render(get_t("compliance_subject"), **fields), render(get_t("compliance_body"), **fields)


def send_compliance(companies: list[str] | None = None) -> tuple[int, list[str]]:
    """Send the compliance notice to the given companies (or every affected client if None)."""
    cfg = load_config()
    notices = json.loads(NOTICES_FILE.read_text()) if NOTICES_FILE.exists() else {}
    sent, errors = 0, []
    for c in compliance_list():
        if companies is not None and c["company_name"] not in companies:
            continue
        if not c.get("email"):
            errors.append(f"{c['company_name']}: no email address on file")
            continue
        subject, body = compliance_email(c, cfg)
        try:
            send_email(cfg, c["email"], subject, body, [])
            notices[c["company_name"]] = {"sent_at": dt.datetime.now().isoformat(timespec="seconds"),
                                          "to": c["email"], "issues": len(c["_issues"])}
            sent += 1
        except Exception as ex:
            errors.append(f"{c['company_name']}: {ex}")
    NOTICES_FILE.write_text(json.dumps(notices, indent=1))
    return sent, errors


# ----------------------------------------------------------------------------- email sending

def _ipv4_only_getaddrinfo(*args, **kwargs):
    """Some hosts (e.g. Railway) resolve smtp.gmail.com to IPv6 but have no IPv6 route -> 'Network is unreachable'."""
    return [ai for ai in _real_getaddrinfo(*args, **kwargs) if ai[0] == socket.AF_INET]


_real_getaddrinfo = socket.getaddrinfo


def send_email(cfg: dict, to: str, subject: str, body: str, attachments: list[Path]) -> None:
    # Option A – Resend (HTTPS API, works everywhere). Set RESEND_API_KEY in Railway variables.
    if os.environ.get("RESEND_API_KEY"):
        return _send_via_resend(cfg, to, subject, body, attachments)
    # Option B – SMTP (Gmail app password etc.)
    if not (cfg.get("smtp_user") and cfg.get("smtp_password")):
        raise RuntimeError("Email is not configured – open Settings and enter SMTP details.")
    msg = EmailMessage()
    msg["From"] = cfg.get("from_email") or cfg["smtp_user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for p in attachments:
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    socket.getaddrinfo = _ipv4_only_getaddrinfo
    try:
        port = int(cfg["smtp_port"])
        if port == 465:
            with smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=60) as s:
                s.login(cfg["smtp_user"], cfg["smtp_password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["smtp_host"], port, timeout=60) as s:
                s.starttls()
                s.login(cfg["smtp_user"], cfg["smtp_password"])
                s.send_message(msg)
    except OSError as e:
        if getattr(e, "errno", None) in (101, 110, 111):
            raise RuntimeError(f"{e} – this host appears to block outgoing mail connections. "
                               "Add a RESEND_API_KEY variable (see DEPLOY_RAILWAY.md) to send over HTTPS instead.")
        raise
    finally:
        socket.getaddrinfo = _real_getaddrinfo


def _send_via_resend(cfg: dict, to: str, subject: str, body: str, attachments: list[Path]) -> None:
    import base64, urllib.request
    sender = cfg.get("from_email") or "onboarding@resend.dev"
    if cfg.get("sender_name"):
        sender = f"{cfg['sender_name']} <{sender}>"
    payload = {"from": sender, "to": [to], "subject": subject, "text": body,
               "attachments": [{"filename": p.name, "content": base64.b64encode(p.read_bytes()).decode()} for p in attachments]}
    req = urllib.request.Request("https://api.resend.com/emails", data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY'].strip()}",
                                          "Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "MailSort/1.0 (+https://github.com/usamamuchada-code/mailsort)"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Resend rejected the email ({e.code}): {e.read().decode()[:300]}")


def client_issues(b: dict, e: dict) -> list[str]:
    """Human-readable list of problems for this client, drawn from the batch + client record."""
    ls = [L for L in b["letters"] if L.get("client") and L["client"]["company_name"] == e["company"]]
    c = ls[0]["client"] if ls else {}
    issues = []
    missing = [L["letter_id"] for L in ls if not L.get("siu_ok", True)]
    if missing:
        issues.append(render(get_t("issue_siu"), n=len(missing), ids=" (" + ", ".join(missing) + ")"))
    status = (c.get("status") or e.get("status") or "").lower()
    if status == "overdue":
        issues.append(render(get_t("issue_renewal"), site=SITE_URL))
    elif status in ("suspended", "cancelled"):
        issues.append(render(get_t("issue_status"), status=status))
    if (c.get("kyc") or "").lower() == "no":
        issues.append(render(get_t("issue_kyc"), login_url=LOGIN_URL))
    excluded = [L["letter_id"] for L in ls if not L.get("in_package", True)]
    if excluded:
        pkg = c.get("package") or e.get("package") or "your"
        issues.append(render(get_t("issue_package"), n=len(excluded), package=pkg))
    return issues


def issues_email_parts(b: dict, e: dict, cfg: dict) -> tuple[str, str]:
    issues = client_issues(b, e)
    today = dt.date.today().strftime("%d/%m/%Y")
    lines = "\n\n".join(f"{i}. {x}" for i, x in enumerate(issues, 1))
    contact = next((L["client"].get("contact_name") for L in b["letters"]
                    if L.get("client") and L["client"]["company_name"] == e["company"]), "") or "Client"
    fields = dict(company=e["company"], contact=contact, date=today, issues=lines, sender_name=cfg["sender_name"])
    return render(get_t("issues_subject"), **fields), render(get_t("issues_body"), **fields)


def draft_parts(bdir: Path, e: dict) -> tuple[str, str]:
    """Return (subject, body) from the draft .txt written by mailsort."""
    txt = (bdir / e["file"]).read_text(encoding="utf-8")
    lines = txt.splitlines()
    subject, body_lines, in_body = "", [], False
    for ln in lines:
        if ln.startswith("# ACTION") or ln.startswith("To: "):
            continue
        if ln.startswith("Subject: "):
            subject = ln[9:]; in_body = True; continue
        if in_body:
            body_lines.append(ln)
    return subject, "\n".join(body_lines).strip() + "\n"


# ----------------------------------------------------------------------------- templates

BASE = """<!doctype html><html><head><meta charset="utf-8"><title>Startitup Mail Room</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--b:#111111;--bg:#fafafa;--card:#fff;--line:#e7e7e7;--warn:#fff4e5;--ok:#e8f7ee}
*{box-sizing:border-box}body{font-family:'Hanken Grotesk',system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--bg);color:#111}
header{background:#fff;border-bottom:1px solid var(--line);padding:12px 28px;display:flex;gap:28px;align-items:center}
header a{color:#555;text-decoration:none;font-weight:600}header a:hover{color:#111}
header a.brand{display:flex;align-items:center;gap:10px;color:#111}
header a.brand img{height:30px;display:block}header a.brand span{font-size:13px;font-weight:600;color:#777;border-left:1px solid var(--line);padding-left:10px;letter-spacing:.02em}
main{max-width:1200px;margin:28px auto;padding:0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:22px;margin-bottom:22px;overflow-x:auto}
h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}
td{overflow-wrap:anywhere;max-width:240px}
th{background:#f7f7f7;font-weight:700}tr.review{background:var(--warn)}tr.sent{background:var(--ok)}
.btn{display:inline-block;background:var(--b);color:#fff;border:0;border-radius:999px;padding:9px 18px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none}
.btn:hover{background:#333}.btn.secondary{background:#efefef;color:#111}.btn.secondary:hover{background:#e2e2e2}.btn.small{padding:5px 10px;font-size:13px}.btn:disabled{opacity:.5}
.btn{white-space:nowrap}
input[type=text],input[type=password],input[type=number],textarea{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}
.drop{border:2px dashed #94a3b8;border-radius:10px;padding:36px;text-align:center;background:#fafafa}
.drop.over{background:#eef2ff;border-color:var(--b)}
.flash{background:#fef3c7;border:1px solid #fcd34d;padding:10px 14px;border-radius:8px;margin-bottom:16px}
.muted{color:#6b7280;font-size:13px}.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;background:#e5e7eb;white-space:nowrap}
.pill.high{background:#fee2e2;color:#991b1b}.pill.active{background:#dcfce7;color:#166534}.pill.hold{background:#fee2e2;color:#991b1b}
progress{width:100%;height:14px}
.tiny{font-size:11px}
.btnrow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{display:inline-block;padding:5px 12px;border-radius:999px;border:1px solid var(--line);background:#fff;font-size:13px;font-weight:600;cursor:pointer;user-select:none}
.chip:hover{border-color:#999}.chip.on{background:#111;color:#fff;border-color:#111}
.clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.statgrid{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 4px}
.stat{background:#f7f7f7;border:1px solid var(--line);border-radius:10px;padding:8px 16px;font-size:12px;color:#6b7280;min-width:86px}
.stat b{font-size:19px;display:block;color:#111}
tr.drow>td{background:#fbfbfb;border-bottom:2px solid var(--line)}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px 22px;padding:6px 2px}
.dgrid>div>b{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af;margin-bottom:2px;font-weight:700}
details.flash summary{cursor:pointer;font-weight:700}
.filterbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.filterbar input{max-width:280px}
</style><script>
function siuWarn(ids){
  return confirm("WARNING: letter " + ids + " does NOT have the SIU office mentioned in the address. Please notify the customer that their post must carry the SIU office. Continue anyway?")
      && confirm("Second check – are you sure you want to proceed with " + ids + " despite the missing SIU office?");
}
</script></head><body>
<header><a class="brand" href="/"><img src="data:image/png;base64,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" alt="Startitup"><span>Mail Room</span></a>
<a href="/">Batches</a><a href="/history">Search &amp; History</a><a href="/clients">Client database</a><a href="/templates">Templates</a><a href="/audit">Audit</a><a href="/settings">Settings</a><a href="/logout" style="margin-left:auto">Sign out</a><span style="color:#c4c4c4;font-size:11px">v28w</span></header>
<main>{% with m = get_flashed_messages() %}{% for x in m %}<div class="flash">{{x}}</div>{% endfor %}{% endwith %}
{% block body %}{% endblock %}</main></body></html>"""

HOME = """{% extends "base" %}{% block body %}
<div class="card"><h1>Upload a bulk scan</h1>
{% if not clients %}<div class="flash">No client database yet – <a href="/clients">upload your client list</a> first.</div>{% endif %}
<form method="post" action="/upload" enctype="multipart/form-data" id="f">
<div class="drop" id="drop"><p><b>Drag the scanned PDF here</b> – or photos/images of letters (JPG, PNG); you can select several files and they become one batch, in order</p>
<input type="file" name="pdf" accept="application/pdf,image/jpeg,image/png,image/webp,image/tiff,.pdf,.jpg,.jpeg,.png,.webp,.tif,.tiff" multiple required id="file"><p class="muted" id="fname"></p></div>
<label>Note (optional, e.g. "Morning post 26 Aug")</label><input type="text" name="note">
<p><button class="btn" id="go" {% if not clients %}disabled{% endif %}>Upload &amp; sort</button>
<span class="muted">Sorting runs in the background – you can leave this page.</span></p></form></div>
<div class="card"><h2>Recent batches</h2>
{% if not batches %}<p class="muted">Nothing processed yet.</p>{% else %}
<table><tr><th>Batch</th><th>When</th><th>File</th><th>Result</th><th>Emails</th><th></th></tr>
{% for b in batches %}<tr><td>{{b.id}}</td><td>{{b.created}}</td><td>{{b.pdf}}<br><span class="muted">{{b.note}}</span></td>
<td>{% if b.error %}<span style="color:#b91c1c">Failed: {{b.error}}</span>{% elif b.summary %}{{b.summary}}{% else %}{{b.msg}}{% endif %}</td>
<td>{{b.sent}}/{{b.total_emails}} sent</td>
<td><a class="btn small secondary" href="/batch/{{b.id}}">Open</a>
<form method="post" action="/batch/{{b.id}}/delete" style="display:inline" onsubmit="if(!confirm('Delete batch {{b.id}} completely? Its split letters, drafts and history entries will be permanently removed. The client database and settings are NOT affected.'))return false; var pw=prompt('Enter the delete password:'); if(pw===null||pw==='')return false; this.pw.value=pw; return true;">
<input type="hidden" name="pw"><button class="btn small secondary" style="color:#991b1b">Delete</button></form></td></tr>{% endfor %}</table>{% endif %}</div>
<script>
const d=document.getElementById('drop'),i=document.getElementById('file'),n=document.getElementById('fname');
i.onchange=()=>{const fs=[...i.files]; n.textContent=fs.length?fs.map(f=>f.name).join(', ')+' ('+(fs.reduce((a,f)=>a+f.size,0)/1048576).toFixed(1)+' MB)':''};
d.ondragover=e=>{e.preventDefault();d.classList.add('over')};d.ondragleave=()=>d.classList.remove('over');
d.ondrop=e=>{e.preventDefault();d.classList.remove('over');i.files=e.dataTransfer.files;i.onchange()};
document.getElementById('f').onsubmit=()=>{document.getElementById('go').disabled=true;document.getElementById('go').textContent='Uploading…'};
</script>{% endblock %}"""

BATCH = """{% extends "base" %}{% block body %}
<div class="card"><h1>Batch {{bid}}</h1>
{% if not b %}<p id="msg">{{job.msg}}</p><progress value="{{job.frac}}" max="1" id="bar"></progress>
{% if job.error %}<p style="color:#b91c1c">{{job.error}}</p>{% else %}
<script>setInterval(async()=>{const r=await (await fetch('/api/job/{{bid}}')).json();
document.getElementById('msg').textContent=r.msg;document.getElementById('bar').value=r.frac;if(r.done)location.reload()},2000)</script>{% endif %}
{% else %}
<p>{{b.summary}} &middot; {{b.pages}} pages &middot; processed from <b>{{b.pdf}}</b> {{b.created}}
{% if b.mode!='ai' %}&middot; <span class="pill high">classified WITHOUT AI ({{b.mode}}) – check Settings</span>{% endif %}</p>
{% set nver = b.letters|selectattr("match_state","equalto","verified")|list|length %}
{% set nchk = b.letters|selectattr("match_state","equalto","review")|list|length %}
{% set nsentmail = b.emails|selectattr("sent_at")|list|length %}
{% set nlsent = b.letters|selectattr("emailed_at")|list|length %}
<div class="statgrid">
<div class="stat"><b>{{b.letters|length}}</b>letters</div>
<div class="stat"><b style="color:#166534">{{nver}}</b>verified</div>
<div class="stat"{% if nchk %} style="background:#fef3c7;border-color:#fcd34d"{% endif %}><b style="color:#92400e">{{nchk}}</b>need a check</div>
<div class="stat"><b>{{nsentmail}}</b>client emails sent</div>
<div class="stat"><b>{{nlsent}}</b>letters emailed singly</div>
</div>
{% set mm = b.letters|selectattr("unit_mismatch")|list %}{% if mm %}<details class="flash" style="background:#fee2e2;border-color:#dc2626"><summary>⚠ POSSIBLE WRONG-CLIENT MATCH on {{mm|length}} letter(s) – click for the list. Their emails are on HOLD.</summary>
{% for L in mm %}{{L.letter_id}} (letter shows unit {{L.unit}} but was name-matched to {{L.client.company_name}}, unit {{L.client.siu or "?"}}){% if not loop.last %}; {% endif %}{% endfor %}.
Open each letter and verify the correct client before sending anything.</details>{% endif %}
{% set missing = b.letters|rejectattr("siu_ok")|list %}{% if missing %}<details class="flash" style="background:#fee2e2;border-color:#fca5a5"><summary>SIU office missing on {{missing|length}} letter(s) – click for the list.</summary>
{% for L in missing %}{{L.letter_id}} ({{L.client.company_name if L.client else L.recipient_company or "unknown addressee"}}){% if not loop.last %}, {% endif %}{% endfor %}.
You will be warned before opening, downloading or sending these – please notify the customer(s) that their post must carry the SIU office in the address.</details>{% endif %}
{% set held = b.letters|rejectattr("emailed_at")|selectattr("match_state","equalto","review")|list %}
{% set ready = b.letters|rejectattr("emailed_at")|selectattr("rematched_at")|selectattr("match_state","equalto","verified")|list %}
{% if held or ready %}<div class="flash" style="background:#fef3c7;border-color:#fbbf24">
{% if held %}<b>{{held|length}} letter(s) are on hold / not verified.</b> If you have updated the client database since this batch was sorted (e.g. added the missing company), re-check them – nothing is emailed by this step:
<form method="post" action="/batch/{{bid}}/rematch" style="display:inline"><button class="btn small">🔄 Re-check held letters against current client list</button></form>{% endif %}
{% if ready %}<br><b>{{ready|length}} re-checked letter(s) are now verified and ready:</b>
{% for L in ready %}{{L.letter_id}} → {{L.client.company_name}}{% if not loop.last %}, {% endif %}{% endfor %}.
<form method="post" action="/batch/{{bid}}/send_held" style="display:inline" onsubmit="return confirm('Email {{ready|length}} re-checked letter(s) to their verified clients now?')"><button class="btn small">📧 Send {{ready|length}} re-checked letter(s)</button></form>{% endif %}
</div>{% endif %}
<p><a class="btn secondary small" href="/batch/{{bid}}/file/manifest.csv">Download manifest.csv</a>
<a class="btn secondary small" href="/batch/{{bid}}/zip">Download all letters (zip)</a>
<form method="post" action="/batch/{{bid}}/learn" style="display:inline" onsubmit="return confirm('Fill in MISSING unit numbers and addresses in the client database, using what the AI read on the VERIFIED letters in this batch? Existing values are never changed, and nothing is learned from held or unverified letters.')">
<button class="btn secondary small">📥 Learn units &amp; addresses from verified letters</button></form></p>
<p class="muted tiny">“Learn” copies the unit number and address from each verified letter into the client database wherever those fields are still blank – so matching gets safer with every batch, without typing 1,500 addresses by hand.</p></div>

<div class="card"><h2>Client emails</h2>
<table><tr><th>Client</th><th>Email</th><th>Status</th><th>Letters</th><th>Action</th><th>Sent</th><th></th></tr>
{% for e in b.emails %}<tr class="{{'sent' if e.sent_at or e.manual_sent_at else ('review' if not e.action.startswith('SEND') else '')}}">
<td>{{e.company}}{% if e.reseller %}<br><span class="pill" style="background:#e0e7ff;color:#3730a3;font-size:11px">{{e.reseller}}</span>{% endif %}</td><td>{{e.email}}</td><td><span class="pill {{'active' if e.status=='active' else 'hold'}}">{{e.status}}</span>{% if e.package %} <span class="pill">{{e.package}}</span>{% endif %}</td>
<td>{{e.letters}}</td><td>{{e.action}}{% if e.siu_missing %}<br><span class="pill high">⚠ SIU missing on {{e.siu_missing|join(", ")}}</span>{% endif %}</td><td>{% if e.sent_at %}{{e.sent_at|replace("T"," ")}} <span class="muted">(emailed)</span>{% elif e.manual_sent_at %}{{e.manual_sent_at|replace("T"," ")}} <span class="muted">(marked by staff)</span>{% else %}–{% endif %}</td>
<td><div class="btnrow"><a class="btn small secondary" href="/batch/{{bid}}/client_zip/{{loop.index0}}"{% if e.siu_missing %} onclick="return siuWarn('{{e.siu_missing|join(", ")}}')"{% endif %}>PDFs</a> <a class="btn small secondary" href="/batch/{{bid}}/email/{{loop.index0}}">Preview</a>
<form method="post" action="/batch/{{bid}}/send/{{loop.index0}}" style="display:inline" onsubmit="return {% if e.siu_missing %}siuWarn('{{e.siu_missing|join(", ")}}') && {% endif %}confirm('Send to {{e.email}}?')">
<button class="btn small" {% if not e.email %}disabled{% endif %}>{{'Re-send' if e.sent_at else 'Send'}}</button></form>
<form method="post" action="/batch/{{bid}}/mark_sent/{{loop.index0}}" style="display:inline">
<button class="btn small secondary">{{'Unmark' if e.manual_sent_at else 'Mark as sent'}}</button></form>
{% if e.n_issues %}<a class="btn small" style="background:#b91c1c" href="/batch/{{bid}}/issues/{{loop.index0}}">⚠ Report issues ({{e.n_issues}})</a>
{% if e.issues_sent_at %}<span class="muted" style="font-size:11px">sent {{e.issues_sent_at|replace("T"," ")}}</span>{% endif %}{% endif %}</div></td></tr>{% endfor %}</table>
<p style="margin-top:14px"><form method="post" action="/batch/{{bid}}/send_all" onsubmit="return confirm('Send to every ACTIVE client that has not been emailed yet?')">
<button class="btn">Send all active &amp; unsent</button></form></p></div>

<div class="card"><h2>Letters ({{b.letters|length}})</h2>
<p class="muted">Each row shows the essentials – press <b>Details</b> on a row for addresses, tracking and full review notes.
Orange rows need a human check, green rows are downloaded. Downloaded: {{b.letters|selectattr("downloaded_at")|list|length}} of {{b.letters|length}} · opened (viewed only): {{b.letters|rejectattr("downloaded_at")|selectattr("opened_at")|list|length}} · untouched: {{b.letters|rejectattr("downloaded_at")|rejectattr("opened_at")|list|length}}.</p>
<div class="filterbar">
<input type="text" id="lsearch" placeholder="Search letters – company, sender, address…" oninput="applyf()">
<span class="chip on" data-m="all" onclick="setf('all')">All</span>
<span class="chip" data-m="check" onclick="setf('check')">Needs check</span>
<span class="chip" data-m="held" onclick="setf('held')">No match</span>
<span class="chip" data-m="matched" onclick="setf('matched')">Matched</span>
<span class="chip" data-m="verified" onclick="setf('verified')">Verified</span>
<span class="chip" data-m="sent" onclick="setf('sent')">Emailed</span>
<span class="chip" data-m="siu" onclick="setf('siu')">SIU missing</span>
<span class="muted tiny" id="fcount"></span>
</div>
<table><tr><th>ID</th><th>Addressee (as printed)</th><th>Matched client</th><th>Status</th><th>Sender</th><th>Summary</th><th>Actions</th><th></th></tr>
{% for L in b.letters %}<tr class="lrow {{'review' if L.needs_review else ('sent' if L.downloaded_at else '')}}" data-id="{{L.letter_id}}" data-st="{% if L.match_state == 'review' %}check {% endif %}{% if not L.client %}held {% endif %}{% if L.client %}matched {% endif %}{% if L.client and L.match_state == 'verified' %}verified {% endif %}{% if L.emailed_at %}sent {% endif %}{% if not L.siu_ok %}siu {% endif %}">
<td><b>{{L.letter_id}}</b><br><span class="muted tiny">{{L.pages|length}} page{{'s' if L.pages|length != 1 else ''}} · p.{{L.pages|join("-")}}</span></td>
<td>{{L.recipient_company or "—"}}{% if L.unit %}<br><span class="muted tiny">letter unit {{L.unit}}</span>{% endif %}</td>
<td>{% if L.client %}{{L.client.company_name}}
<span class="muted">{% if L.matched_by == "unit" %}unit ✓{% elif L.matched_by == "ai-verified" %}AI ✓{% elif L.matched_by == "staff" %}staff ✓{% else %}{{(L.match_score*100)|round|int}}%{% endif %}</span>
{% if L.client.siu %}<br><span class="muted tiny">client unit {{L.client.siu}}</span>{% endif %}
{% if L.unit_mismatch %}<br><span class="pill high">⚠ UNIT MISMATCH – letter is unit {{L.unit}}</span>{% endif %}
{% else %}<b>— no match —</b>{% endif %}
{% if L.match_state == "review" or not L.client %}<div style="background:#fef9c3;border:1px solid #fde047;border-radius:8px;padding:6px;margin-top:6px">
<span class="pill high">NEEDS VERIFICATION</span>{% if L.match_note %}<br><span class="muted tiny">{{L.match_note}}</span>{% endif %}
{% if L.suggested_client %}<br><span class="muted tiny">AI suggests: {{L.suggested_client}}</span>{% endif %}
<form method="post" action="/batch/{{bid}}/assign/{{L.letter_id}}" style="margin-top:4px" onsubmit="return confirm('Confirm this letter belongs to the selected client? It will then be sendable.')">
<select name="company" style="max-width:180px;font-size:12px;padding:4px;border:1px solid #cbd5e1;border-radius:6px">
{% for name in all_clients %}<option value="{{name}}" {% if name == (L.suggested_client or (L.client.company_name if L.client else "")) %}selected{% endif %}>{{name}}</option>{% endfor %}
<option value="__unmatch__">— not a client / do not send —</option></select>
<button class="btn small">Confirm</button></form></div>{% endif %}</td>
<td><div style="display:flex;flex-wrap:wrap;gap:4px;max-width:150px">
{% if L.client and L.match_state == "verified" %}<span class="pill active">verified</span>{% elif not L.client %}<span class="pill high">HELD</span>{% else %}<span class="pill high">needs check</span>{% endif %}
{% if L.urgency == "high" %}<span class="pill high">urgent</span>{% endif %}
{% if not L.siu_ok %}<span class="pill high">SIU missing</span>{% endif %}
{% if L.client and L.client.kyc == "no" %}<span class="pill high">KYC pending</span>{% endif %}
{% if not L.in_package %}<span class="pill high">not in package</span>{% endif %}
{% if L.emailed_at %}<span class="pill active">✉ emailed</span>{% endif %}
{% if L.downloaded_at %}<span class="pill active">✔ downloaded</span>{% endif %}
</div></td>
<td>{{L.sender}}<br><span class="muted tiny">{{L.letter_type|replace("_"," ")}}</span></td>
<td><div class="clamp" style="max-width:240px">{{L.summary}}</div></td>
<td><div class="btnrow">
{% if L.siu_ok %}<a class="btn small secondary" href="/batch/{{bid}}/file/{{L.file}}" target="_blank">Open</a><a class="btn small secondary" href="/batch/{{bid}}/download/{{L.file}}">Download</a>{% else %}<a class="btn small secondary" href="/batch/{{bid}}/file/{{L.file}}" target="_blank" onclick="return siuWarn('{{L.letter_id}}')">Open</a><a class="btn small secondary" href="/batch/{{bid}}/download/{{L.file}}" onclick="return siuWarn('{{L.letter_id}}')">Download</a>{% endif %}
{% if L.client and L.client.email %}{% if L.match_state == "review" %}<span class="muted tiny">verify match first</span>{% else %}<form method="post" action="/batch/{{bid}}/send_letter/{{L.letter_id}}" style="display:inline" onsubmit="return {% if L.unit_mismatch %}confirm('DANGER: this letter shows unit {{L.unit}} but is matched to {{L.client.company_name}} (unit {{L.client.siu or "?"}}). Sending to the wrong client exposes confidential post. Are you CERTAIN this is the right client?') && {% endif %}{% if not L.siu_ok %}siuWarn('{{L.letter_id}}') && {% endif %}{% if not L.in_package %}confirm('NOTE: this letter is NOT covered by {{L.client.package or "the client"}}\'s package (non-government mail). Send it anyway?') && {% endif %}confirm('Email this letter to {{L.client.email}}?')">
<button class="btn small">{{'Re-send' if L.emailed_at else 'Send'}}</button></form>{% endif %}{% elif not L.client %}<span class="muted tiny">no client</span>{% endif %}
{% if not loop.first %}<form method="post" action="/batch/{{bid}}/merge/{{L.letter_id}}" style="display:inline" onsubmit="return confirm('Merge {{L.letter_id}} into the letter above? They will become ONE PDF. Use this when one document was wrongly split in two.')">
<button class="btn small secondary" title="This letter is really a continuation of the one above">Merge ↑</button></form>{% endif %}
</div>{% if L.emailed_at %}<span class="muted tiny">✉ {{L.emailed_at|replace("T"," ")}}</span>{% endif %}</td>
<td><button type="button" class="btn small secondary" onclick="tgl('det-{{L.letter_id}}')">Details</button></td></tr>
<tr class="drow" id="det-{{L.letter_id}}" hidden><td colspan="8"><div class="dgrid">
<div><b>Address on letter (AI read)</b>{% if L.address %}{{L.address}}{% else %}<span class="muted">—</span>{% endif %}</div>
<div><b>Address on file (CSV)</b>{% if L.client and L.client.address %}{{L.client.address}}{% elif L.client %}<span class="pill high" style="font-size:11px">no address in CSV</span>{% else %}<span class="muted">—</span>{% endif %}</div>
<div><b>Reseller / Direct</b>{% if L.client %}{% if L.client.reseller %}<span class="pill" style="background:#e0e7ff;color:#3730a3">{{L.client.reseller}}</span>{% else %}<span class="pill">Direct</span>{% endif %}{% else %}<span class="muted">—</span>{% endif %}</div>
<div><b>KYC</b>{% if L.client %}{% if L.client.kyc == "yes" %}<span class="pill active">✔ done</span>{% elif L.client.kyc == "no" %}<span class="pill high">pending</span>{% else %}<span class="muted">?</span>{% endif %}{% else %}<span class="muted">—</span>{% endif %}</div>
<div><b>Urgency</b><span class="pill {{L.urgency}}">{{L.urgency}}</span></div>
<div><b>SIU office on letter</b>{% if L.siu_ok %}<span class="pill active">✔ present</span>{% else %}<span class="pill high">MISSING</span>{% endif %}</div>
<div><b>Tracking</b>{% if L.downloaded_at %}<span class="pill active">✔ downloaded {{L.downloaded_at|replace("T"," ")}}</span>{% elif L.opened_at %}<span class="pill">👁 opened ×{{L.opens or 1}} · {{L.opened_at|replace("T"," ")}}</span>{% else %}<span class="muted">not opened yet</span>{% endif %}</div>
{% if L.match_note %}<div style="grid-column:1/-1"><b>Match note</b>{{L.match_note}}</div>{% endif %}
<div style="grid-column:1/-1"><b>Full summary</b>{{L.summary}}</div>
<div style="grid-column:1/-1"><b>Review notes</b>{% if L.needs_review %}{{L.needs_review}}{% else %}<span class="muted">nothing to check</span>{% endif %}</div>
</div></td></tr>{% endfor %}</table></div>
<script>
function tgl(id){var r=document.getElementById(id);r.hidden=!r.hidden}
var MODE='all';
function applyf(){
 var q=(document.getElementById('lsearch').value||'').toLowerCase();
 var shown=0,total=0;
 document.querySelectorAll('tr.lrow').forEach(function(r){
  total++;
  var d=document.getElementById('det-'+r.dataset.id);
  var okm=(MODE==='all')||((' '+r.dataset.st+' ').indexOf(' '+MODE+' ')>=0);
  var okq=!q||((r.textContent+(d?d.textContent:'')).toLowerCase().indexOf(q)>=0);
  var show=okm&&okq;
  r.style.display=show?'':'none';
  if(d){d.style.display=show?'':'none';if(!show){d.hidden=true}}
  if(show){shown++}
 });
 document.getElementById('fcount').textContent=(shown===total)?'':('showing '+shown+' of '+total);
 document.querySelectorAll('.chip[data-m]').forEach(function(c){c.classList.toggle('on',c.dataset.m===MODE)});
}
function setf(m){MODE=m;applyf()}
</script>
{% endif %}{% endblock %}"""

EMAIL = """{% extends "base" %}{% block body %}<div class="card"><h1>Email preview – {{e.company}}</h1>
<p><b>To:</b> {{e.email}} &nbsp; <b>Subject:</b> {{subject}}</p><pre style="white-space:pre-wrap;background:#f9fafb;padding:14px;border-radius:8px">{{body}}</pre>
<p><b>Attachments:</b> {% for a in atts %}{{a}}{% if not loop.last %}, {% endif %}{% endfor %}</p>
<p><a class="btn secondary" href="/batch/{{bid}}">Back</a>
<form method="post" action="/batch/{{bid}}/send/{{i}}" style="display:inline" onsubmit="return confirm('Send to {{e.email}}?')"><button class="btn">Send now</button></form></p></div>{% endblock %}"""

ISSUES = """{% extends "base" %}{% block body %}<div class="card"><h1>Issues email – {{e.company}}</h1>
<p><b>To:</b> {{e.email or "— no email on file —"}} &nbsp; <b>Subject:</b> {{subject}}</p>
<pre style="white-space:pre-wrap;background:#f9fafb;padding:14px;border-radius:8px">{{body}}</pre>
<p><a class="btn secondary" href="/batch/{{bid}}">Back</a>
<form method="post" action="/batch/{{bid}}/issues_send/{{i}}" style="display:inline" onsubmit="return confirm('Send this issues email to {{e.email}}?')">
<button class="btn" {% if not e.email %}disabled{% endif %}>Send issues email</button></form>
{% if e.issues_sent_at %}<span class="muted">Already sent {{e.issues_sent_at|replace("T"," ")}} – sending again is allowed.</span>{% endif %}</p></div>{% endblock %}"""

COMPLIANCE = """{% extends "base" %}{% block body %}
<div class="card"><h1>Compliance notices</h1>
<p class="muted">Every client in the database with an outstanding issue – KYC pending, service expired, or letters received
without the SIU office (from all processed batches). The email warns that their post cannot be processed until resolved.
"Last sent" shows when they were last notified, so you can avoid repeat emails.</p>
{% if not affected %}<p><b>No clients currently have outstanding issues. 🎉</b></p>{% else %}
<form method="post" action="/clients/compliance_send" onsubmit="return confirm('Send the compliance notice email to ALL {{affected|length}} affected client(s)?') && confirm('Second check – this emails {{affected|length}} customers at once. Continue?')">
<p><button class="btn" style="background:#b91c1c">⚠ Send to all {{affected|length}} affected clients</button></p></form>
<table><tr><th>Client</th><th>Email</th><th>Issues</th><th>Last sent</th><th></th></tr>
{% for c in affected %}<tr>
<td>{{c.company_name}}{% if c.reseller %}<br><span class="pill" style="background:#e0e7ff;color:#3730a3;font-size:11px">{{c.reseller}}</span>{% endif %}</td>
<td>{{c.email or "— none —"}}</td>
<td style="font-size:13px">{% for i in c._issues %}• {{i[:110]}}{% if i|length > 110 %}…{% endif %}<br>{% endfor %}</td>
<td>{% if c._last_notice %}<span class="pill active">{{c._last_notice}}</span>{% else %}<span class="muted">never</span>{% endif %}</td>
<td><details><summary class="btn small secondary" style="list-style:none;cursor:pointer">Preview</summary>
<pre style="white-space:pre-wrap;background:#f9fafb;padding:10px;border-radius:8px;font-size:12px;max-width:520px">{{c._body}}</pre></details>
<form method="post" action="/clients/compliance_send" onsubmit="return confirm('Send the compliance notice to {{c.email}}?')">
<input type="hidden" name="company" value="{{c.company_name}}"><button class="btn small" {% if not c.email %}disabled{% endif %}>Send</button></form></td></tr>{% endfor %}</table>{% endif %}
<p><a class="btn secondary" href="/clients">Back to client database</a></p></div>{% endblock %}"""

CLIENTS = """{% extends "base" %}{% block body %}
<div class="card"><h1>Client database</h1>
<p class="muted">Upload a CSV exported from your portal – it <b>replaces</b> the current list. Only <b>active</b> clients are emailed automatically.</p>
<details style="margin-bottom:12px"><summary class="muted" style="cursor:pointer;font-weight:600">What each CSV column means (click to expand)</summary>
<p class="muted">Headers: <code>client_id, company_name, contact_name, email, status, package, start_date, reseller, reseller_email, kyc, siu, address</code>.
package = Basic / Standard / Premium. start_date = when the client's service year began, e.g. 2026-03-01 or 01/03/2026 –
an <b>active</b> client is automatically treated as <b>overdue</b> once a year has passed; update the date when they renew.
reseller = the reseller/partner this address is assigned through; leave blank for a direct client.
kyc = yes/no (aliases: done/pending) – whether identity checks are complete.
<b>siu</b> = the client's unique unit/SIU number, e.g. A80 – STRONGLY recommended: letters are matched by this number first,
and a letter whose unit number conflicts with the matched client is held instead of sent.
<b>address</b> = the client's full registered address as it should appear on their post – used by the AI to verify matches.
Letters whose address does not reliably match any client are HELD as unmatched, never sent on a guess.
status = active / overdue / suspended / cancelled.</p></details>
<form method="post" action="/clients/upload" enctype="multipart/form-data"><input type="file" name="csv" accept=".csv,text/csv" required>
<button class="btn">Upload / update database</button> <a class="btn secondary" href="/clients/sample">Download sample CSV</a>
<a class="btn" style="background:#b91c1c" href="/clients/compliance">⚠ Compliance notices</a></form>
<p class="muted">{{clients|length}} clients on file{% if mtime %} · last updated {{mtime}}{% endif %} · <a href="/clients/download">download current CSV</a></p>
{% set bad = clients|selectattr("start_date")|rejectattr("_expiry")|list %}{% if bad %}
<div class="flash" style="background:#fee2e2;border-color:#fca5a5"><b>{{bad|length}} client(s) have a start date the system cannot read</b> (marked ⚠ below) – their renewal due date and automatic overdue cannot be calculated. Please use dates like 2026-03-01 or 01/03/2026 and re-upload.</div>{% endif %}</div>
<div class="card"><h2>Add or edit one client</h2>
<form method="post" action="/clients/save"><div style="display:grid;grid-template-columns:repeat(11,1fr);gap:10px">
<div><label>Client ID</label><input type="text" name="client_id"></div><div><label>Company name *</label><input type="text" name="company_name" required></div>
<div><label>Contact</label><input type="text" name="contact_name"></div><div><label>Email</label><input type="text" name="email"></div>
<div><label>Status</label><input type="text" name="status" value="active"></div>
<div><label>Package</label><select name="package" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px">
<option value="">—</option><option>Basic</option><option>Standard</option><option>Premium</option></select></div>
<div><label>Service start</label><input type="date" name="start_date"></div>
<div><label>Reseller (blank = direct)</label><input type="text" name="reseller"></div>
<div><label>Unit / SIU no.</label><input type="text" name="siu" placeholder="A80"></div>
<div><label>Registered address</label><input type="text" name="address" placeholder="SIU A80, 71-75 ..."></div>
<div><label>KYC done</label><select name="kyc" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px">
<option value="">—</option><option value="yes">Yes</option><option value="no">No</option></select></div></div>
<p><button class="btn">Save</button> <span class="muted">Matching company name (case-insensitive) is updated; otherwise added.</span></p></form></div>
{% if due %}<div class="card"><h2>Upcoming reseller renewals (within 30 days)</h2>
<p class="muted">Resellers are emailed automatically once per renewal – the app checks every day. Use "Send now" to send (or re-send) a reseller's reminder immediately. Reminders are only for reseller-assigned clients; direct clients are not affected.</p>
<table><tr><th>Reseller</th><th>Client</th><th>Renewal due</th><th>Days left</th><th>Reminder</th><th></th></tr>
{% for c in due %}<tr{% if c._days_left < 0 %} style="background:#fee2e2"{% endif %}>
<td><span class="pill" style="background:#e0e7ff;color:#3730a3">{{c.reseller}}</span>{% if not c.reseller_email %} <span class="pill high">no reseller email!</span>{% endif %}</td>
<td>{{c.company_name}}</td><td>{{c._expiry}}</td><td>{% if c._days_left < 0 %}<span class="pill high">overdue</span>{% else %}{{c._days_left}}{% endif %}</td>
<td>{% if c._reminded %}<span class="pill active">✔ {{c._reminded}}</span>{% else %}<span class="muted">not yet</span>{% endif %}</td>
<td><form method="post" action="/clients/remind/{{c.reseller}}" onsubmit="return confirm('Email {{c.reseller}} about all their clients due within 30 days?')"><button class="btn small">Send now</button></form></td></tr>{% endfor %}</table></div>{% endif %}
<div class="card"><h2>Clients</h2>
<div class="filterbar"><input type="text" id="csearch" placeholder="Search clients – name, unit, email, reseller…" oninput="cfilt()">
<span class="muted tiny" id="ccount"></span></div>
<table><tr><th>ID</th><th>Company</th><th>Unit</th><th>Address</th><th>Contact</th><th>Email</th><th>Status</th><th>Package</th><th>Reseller / Direct</th><th>KYC</th><th>Service start</th><th>Renewal due</th></tr>
{% for c in clients %}<tr class="crow"><td>{{c.client_id}}</td><td>{{c.company_name}}</td><td>{% if c.siu %}<span class="pill">{{c.siu}}</span>{% else %}<span class="muted">—</span>{% endif %}</td>
<td style="font-size:12px;max-width:180px">{{c.address or "—"}}</td><td>{{c.contact_name}}</td><td>{{c.email}}</td>
<td><span class="pill {{'active' if c.status=='active' else 'hold'}}">{{c.status}}</span>{% if c._auto_overdue %}<br><span class="muted" style="font-size:11px">auto – year ended</span>{% endif %}</td>
<td>{% if c.package %}<span class="pill">{{c.package}}</span>{% else %}<span class="muted">—</span>{% endif %}</td>
<td>{% if c.reseller %}<span class="pill" style="background:#e0e7ff;color:#3730a3">{{c.reseller}}</span>{% else %}<span class="pill">Direct</span>{% endif %}</td>
<td>{% if c.kyc == "yes" %}<span class="pill active">✔ done</span>{% elif c.kyc == "no" %}<span class="pill high">pending</span>{% else %}<span class="muted">—</span>{% endif %}</td>
<td>{% if c.start_date and not c._expiry %}<span class="pill high" title="This date could not be understood – use YYYY-MM-DD or DD/MM/YYYY">⚠ {{c.start_date}}</span>{% else %}{{c.start_date or "—"}}{% endif %}</td>
<td>{% if c._expiry %}{{c._expiry}}{% if c._auto_overdue %} <span class="pill high">passed</span>{% endif %}{% else %}<span class="muted">—</span>{% endif %}</td></tr>{% endfor %}</table></div>
<script>
function cfilt(){
 var q=(document.getElementById('csearch').value||'').toLowerCase();
 var shown=0,total=0;
 document.querySelectorAll('tr.crow').forEach(function(r){
  total++;
  var show=!q||r.textContent.toLowerCase().indexOf(q)>=0;
  r.style.display=show?'':'none';
  if(show){shown++}
 });
 document.getElementById('ccount').textContent=(shown===total)?(total+' clients'):('showing '+shown+' of '+total);
}
document.addEventListener('DOMContentLoaded',cfilt);
</script>{% endblock %}"""

AUDIT = """{% extends "base" %}{% block body %}<div class="card"><h1>System audit</h1>
<p class="muted">Automatic safety and data-quality checks across the client database and every processed batch.
Run this after uploading a new client list and after any incident. Red items need action.</p>
{% if a.critical %}{% for c in a.critical %}<div class="flash" style="background:#fee2e2;border-color:#dc2626"><b>⚠ {{c}}</b></div>{% endfor %}
{% else %}<div class="flash" style="background:#e8f7ee;border-color:#86efac"><b>No critical problems found.</b></div>{% endif %}

<h2>Client database ({{a.n_clients}} clients)</h2>
<table>
<tr><td>Missing <b>unit/SIU number</b> (weakens match safety)</td><td>{{a.missing_siu|length}}</td><td style="font-size:12px">{{a.missing_siu[:12]|join(", ")}}{% if a.missing_siu|length > 12 %} …{% endif %}</td></tr>
<tr><td>Missing <b>registered address</b> (weakens AI verification)</td><td>{{a.missing_address|length}}</td><td style="font-size:12px">{{a.missing_address[:12]|join(", ")}}{% if a.missing_address|length > 12 %} …{% endif %}</td></tr>
<tr><td>Missing email</td><td>{{a.missing_email|length}}</td><td style="font-size:12px">{{a.missing_email[:12]|join(", ")}}</td></tr>
<tr><td>Missing start date</td><td>{{a.missing_start|length}}</td><td></td></tr>
</table>
{% if a.similar_names %}<h2>Dangerously similar client names</h2>
<p class="muted">These pairs can be confused by senders and matching – make sure BOTH have unit numbers and addresses.</p>
<ul style="font-size:14px">{% for s in a.similar_names %}<li>{{s}}</li>{% endfor %}</ul>{% endif %}

<h2>Matching method across all letters</h2>
<table><tr><th>Method</th><th>Letters</th></tr>
{% for m, n in a.match_dist.items() %}<tr><td>{{ {"unit":"Unit number (safest)","ai-verified":"AI-verified","name":"Name match","staff":"Staff-confirmed","unmatched":"Unmatched / held"}.get(m, m) }}</td><td>{{n}}</td></tr>{% endfor %}</table>

{% if a.risky_sent %}<h2 style="color:#b91c1c">Letters emailed while flagged (historical)</h2>
<ul style="font-size:13px">{% for r in a.risky_sent %}<li>{{r}}</li>{% endfor %}</ul>{% endif %}

<h2>Batches</h2>
<table><tr><th>Batch</th><th>Letters</th><th>No address read</th><th>No unit found</th><th>Currently held</th><th>Mode</th></tr>
{% for s in a.batch_stats|reverse %}<tr {% if s.heuristic %}style="background:#fee2e2"{% endif %}>
<td>{{s.id}}</td><td>{{s.n}}</td><td>{{s.no_addr}}</td><td>{{s.no_unit}}</td><td>{{s.held}}</td><td>{{"NO AI!" if s.heuristic else "AI"}}</td></tr>{% endfor %}</table>
{% if a.dup_scans %}<h2>Possible duplicate uploads</h2><ul>{% for d in a.dup_scans %}<li>{{d}}</li>{% endfor %}</ul>{% endif %}
</div>{% endblock %}"""

TEMPLATES_PAGE = """{% extends "base" %}{% block body %}<div class="card"><h1>Email templates</h1>
<p class="muted">Every email the system sends, editable in one place. Words in curly brackets like <code>{company}</code>
are filled in automatically when the email is sent – keep them exactly as written (you can move or remove them, but don't
change the spelling inside the brackets). Changes take effect immediately for all future emails.</p>
<form method="post">
{% for section, items in sections.items() %}<h2 style="margin-top:26px">{{section}}</h2>
{% for key, label, ph, value, is_subject in items %}
<label>{{label}} <span class="muted" style="font-weight:400">– available: {{ph}}</span></label>
{% if is_subject %}<input type="text" name="{{key}}" value="{{value}}">{% else %}<textarea name="{{key}}" rows="{{5 if value|length < 400 else 9}}" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:13px;font-family:ui-monospace,monospace">{{value}}</textarea>{% endif %}
{% endfor %}{% endfor %}
<p style="margin-top:20px"><button class="btn">Save all templates</button>
<button class="btn secondary" name="reset_all" value="1" onclick="return confirm('Reset ALL templates to their original wording? Your edits will be lost.')">Reset all to defaults</button></p>
</form></div>{% endblock %}"""

SETTINGS = """{% extends "base" %}{% block body %}<div class="card"><h1>Settings</h1><form method="post">
<h2>AI</h2><label>Anthropic API key</label><input type="password" name="anthropic_api_key" value="{{c.anthropic_api_key}}">
<label>Model</label><input type="text" name="model" value="{{c.model}}">
<h2 style="margin-top:24px">Email sending (SMTP)</h2>
<p class="muted">For Gmail / Google Workspace: host smtp.gmail.com, port 587, your address as user, and an <b>App Password</b> (Google Account → Security → 2-Step Verification → App passwords). Works the same for Outlook (smtp.office365.com).</p>
<label>Sender name (signature)</label><input type="text" name="sender_name" value="{{c.sender_name}}">
<label>From address</label><input type="text" name="from_email" value="{{c.from_email}}">
<div style="display:grid;grid-template-columns:2fr 1fr;gap:10px"><div><label>SMTP host</label><input type="text" name="smtp_host" value="{{c.smtp_host}}"></div>
<div><label>Port</label><input type="number" name="smtp_port" value="{{c.smtp_port}}"></div></div>
<label>SMTP username</label><input type="text" name="smtp_user" value="{{c.smtp_user}}">
<label>SMTP password / app password</label><input type="password" name="smtp_password" value="{{c.smtp_password}}">
<label><input type="checkbox" name="attach_pdfs" {% if c.attach_pdfs %}checked{% endif %}> Attach the letter PDFs to the email</label>
<p><button class="btn">Save settings</button> <a class="btn secondary" href="/settings/test_email">Send a test email to myself</a></p></form></div>{% endblock %}"""

HISTORY = """{% extends "base" %}{% block body %}
<div class="card"><h1>Search &amp; History</h1>
<form method="get" style="display:flex;gap:10px;align-items:center;margin-bottom:14px">
<input type="text" name="q" value="{{q}}" placeholder="Search letters: company, address, sender, summary…" style="max-width:420px">
<button class="btn small">Search</button>{% if q %}<a class="btn small secondary" href="/history">Clear</a>{% endif %}
<span class="muted">{% if q %}{{found|length}} letter(s) match "{{q}}" · {% endif %}{{downloaded|length}} letter(s) downloaded · {{sent|length}} email(s) sent · {{batches|length}} batches in total</span></form>
{% if q %}<h2>Letters matching "{{q}}"</h2>
{% if not found %}<p class="muted">No letters match. The search looks at client name, addressee, address on the letter, sender and summary, across every batch. (Addresses exist only on batches processed after the address feature was added.)</p>{% else %}
<table><tr><th>Received</th><th>Batch</th><th>Client / addressee</th><th>Address on letter (AI read)</th><th>Address on file (CSV)</th><th>Sender</th><th>Summary</th><th>Length</th><th>SIU</th><th>PDF</th></tr>
{% for L in found %}<tr{% if not L.siu_ok %} style="background:#fee2e2"{% endif %}>
<td>{{L.created|replace("T"," ")}}</td><td><a href="/batch/{{L.batch}}">{{L.batch}}</a></td>
<td>{{L.client.company_name if L.client else L.recipient_company or "—"}}{% if L.client and L.client.reseller %}<br><span class="pill" style="background:#e0e7ff;color:#3730a3;font-size:11px">{{L.client.reseller}}</span>{% endif %}</td>
<td style="font-size:12px">{{L.address or "—"}}</td>
<td style="font-size:12px">{% if L.client and L.client.address %}{{L.client.address}}{% elif L.client %}<span class="pill high" style="font-size:11px">no address in CSV</span>{% else %}—{% endif %}</td>
<td>{{L.sender}}</td><td style="font-size:13px">{{L.summary}}</td>
<td>{{L.pages|length}} pg<br><span class="muted" style="font-size:11px">p.{{L.pages|join("-")}}</span></td>
<td>{% if L.siu_ok %}<span class="pill active">✔</span>{% else %}<span class="pill high">MISSING</span>{% endif %}</td>
<td><a href="/batch/{{L.batch}}/file/{{L.file}}" target="_blank"{% if not L.siu_ok %} onclick="return siuWarn('{{L.letter_id}}')"{% endif %}>open</a> · <a href="/batch/{{L.batch}}/download/{{L.file}}"{% if not L.siu_ok %} onclick="return siuWarn('{{L.letter_id}}')"{% endif %}>download</a></td></tr>{% endfor %}</table>{% endif %}{% endif %}
<h2>Letters downloaded</h2>
{% if not downloaded %}<p class="muted">No letters downloaded yet.</p>{% else %}
<table><tr><th>Downloaded</th><th>Client</th><th>Letter</th><th>Address on letter (AI read)</th><th>Address on file (CSV)</th><th>Sender</th><th>Length</th><th>Batch</th><th></th></tr>
{% for d in downloaded %}<tr><td>{{d.downloaded_at|replace("T"," ")}}</td>
<td>{% if d.client %}{{d.client.company_name}}{% elif d.recipient_company %}{{d.recipient_company}}<br><span class="pill high" style="font-size:11px">not in client database</span>{% else %}<span class="muted">unknown addressee</span>{% endif %}</td>
<td>{{d.letter_id}} · {{d.summary}}</td>
<td style="font-size:12px">{{d.address or "—"}}</td>
<td style="font-size:12px">{% if d.client and d.client.address %}{{d.client.address}}{% elif d.client %}<span class="pill high" style="font-size:11px">no address in CSV</span>{% else %}—{% endif %}</td>
<td>{{d.sender}}</td>
<td>{{d.pages|length}} page{{'s' if d.pages|length != 1 else ''}}<br><span class="muted" style="font-size:11px">scan p.{{d.pages|join("-")}}</span></td><td>{{d.batch}}</td>
<td><a class="btn small secondary" href="/batch/{{d.batch}}/download/{{d.file}}">Download again</a></td></tr>{% endfor %}</table>{% endif %}
<h2 style="margin-top:24px">Emails sent</h2>
{% if not sent %}<p class="muted">No emails sent yet.</p>{% else %}
<table><tr><th>Sent</th><th>How</th><th>Client</th><th>To</th><th>Letters</th><th>Batch</th><th></th></tr>
{% for s in sent %}<tr><td>{{s.when|replace("T"," ")}}</td><td>{% if s.how=='emailed' %}<span class="pill active">emailed</span>{% else %}<span class="pill">marked by staff</span>{% endif %}</td><td>{{s.company}}</td><td>{{s.sent_to or "—"}}</td><td>{{s.letters}}</td>
<td>{{s.batch}}<br><span class="muted">{{s.note}}</span></td><td><a class="btn small secondary" href="/batch/{{s.batch}}">Open batch</a></td></tr>{% endfor %}</table>{% endif %}</div>
<div class="card"><h2>All batches</h2>
<table><tr><th>Batch</th><th>Processed</th><th>File</th><th>Result</th><th>Letters downloaded</th><th>Opened only</th><th>Emails sent</th><th></th></tr>
{% for b in batches %}<tr><td>{{b.id}}</td><td>{{b.created|replace("T"," ")}}</td><td>{{b.pdf}}<br><span class="muted">{{b.note}}</span></td>
<td>{{b.summary}}</td><td>{{b.dl}} / {{b.nletters}}</td><td>{{b.opened}}</td><td>{{b.sent}} / {{b.total}}</td><td><a class="btn small secondary" href="/batch/{{b.id}}">Open</a></td></tr>{% endfor %}</table></div>{% endblock %}"""

app.jinja_loader = type("L", (), {"get_source": lambda self, env, name: (
    {"base": BASE, "home": HOME, "batch": BATCH, "email": EMAIL, "clients": CLIENTS, "settings": SETTINGS,
     "history": HISTORY, "issues": ISSUES, "compliance": COMPLIANCE, "templates": TEMPLATES_PAGE,
     "audit": AUDIT}[name], name, lambda: True)})()


# ----------------------------------------------------------------------------- routes

@app.route("/")
def home():
    batches = []
    for p in sorted(BATCHES.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        b = load_batch(p.name) or {}
        job = JOBS.get(p.name, {})
        batches.append({"id": p.name, "created": b.get("created", ""), "pdf": b.get("pdf", job.get("pdf", "")),
                        "note": b.get("note", job.get("note", "")), "summary": b.get("summary"),
                        "msg": job.get("msg", "processing…"), "error": job.get("error"),
                        "sent": sum(1 for e in b.get("emails", []) if e.get("sent_at") or e.get("manual_sent_at")),
                        "total_emails": len(b.get("emails", []))})
    return render_template_string(HOME, clients=read_clients(), batches=batches[:50])


@app.get("/history")
def history():
    q = (request.args.get("q") or "").strip().lower()
    batches, sent, downloaded, found = [], [], [], []
    for p in sorted(BATCHES.iterdir(), reverse=True):
        b = load_batch(p.name) if p.is_dir() else None
        if not b:
            continue
        emails, letters = b.get("emails", []), b.get("letters", [])
        batches.append({"id": b["id"], "created": b.get("created", ""), "pdf": b.get("pdf", ""), "note": b.get("note", ""),
                        "summary": b.get("summary", ""), "sent": sum(1 for e in emails if e.get("sent_at") or e.get("manual_sent_at")), "total": len(emails),
                        "dl": sum(1 for L in letters if L.get("downloaded_at")), "nletters": len(letters),
                        "opened": sum(1 for L in letters if L.get("opened_at") and not L.get("downloaded_at"))})
        for L in letters:
            name = (L.get("client") or {}).get("company_name", "")
            if L.get("downloaded_at") and (not q or q in name.lower() or q in (L.get("sender") or "").lower()):
                downloaded.append({**L, "batch": b["id"]})
            if q:
                hay = " ".join([name, (L.get("client") or {}).get("reseller") or "", L.get("recipient_company") or "",
                                L.get("address") or "", L.get("sender") or "", L.get("summary") or ""]).lower()
                if q in hay:
                    found.append({**L, "batch": b["id"], "created": b.get("created", "")})
        for e in emails:
            when = e.get("sent_at") or e.get("manual_sent_at")
            if when and (not q or q in e["company"].lower() or q in (e.get("sent_to") or "").lower()):
                sent.append({**e, "when": when, "how": "emailed" if e.get("sent_at") else "manual",
                             "batch": b["id"], "note": b.get("note", "")})
    sent.sort(key=lambda s: s["when"], reverse=True)
    downloaded.sort(key=lambda d: d["downloaded_at"], reverse=True)
    found.sort(key=lambda L: L["created"], reverse=True)
    return render_template_string(HISTORY, batches=batches, sent=sent, downloaded=downloaded, found=found[:200], q=q)


IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp")


def build_input_pdf(files, bdir: Path) -> Path:
    """Combine uploaded PDFs and images (in selection order) into a single PDF to process."""
    first_name = secure_filename(files[0].filename) or "upload"
    if len(files) == 1 and first_name.lower().endswith(".pdf"):
        pdf = bdir / first_name
        files[0].save(pdf)
        return pdf
    out = ms.fitz.open()
    for f in files:
        name = (f.filename or "").lower()
        data = f.read()
        if name.endswith(".pdf"):
            src = ms.fitz.open(stream=data, filetype="pdf")
            out.insert_pdf(src); src.close()
        elif name.endswith(IMG_EXT):
            img = ms.fitz.open(stream=data, filetype=name.rsplit(".", 1)[-1])
            rect = img[0].rect
            imgpdf = ms.fitz.open("pdf", img.convert_to_pdf()); img.close()
            # scale page to A4 portrait proportions if the image is portrait, else keep its own ratio
            page = out.new_page(width=595, height=max(595 * rect.height / max(rect.width, 1), 200))
            page.show_pdf_page(page.rect, imgpdf, 0)
            imgpdf.close()
        # silently skip anything else
    pdf = bdir / (Path(first_name).stem + "_combined.pdf")
    out.save(pdf); out.close()
    return pdf


@app.post("/upload")
def upload():
    files = [f for f in request.files.getlist("pdf") if f and f.filename]
    ok = [f for f in files if f.filename.lower().endswith((".pdf",) + IMG_EXT)]
    if not ok:
        flash("Please choose a PDF or image files (JPG, PNG)."); return redirect("/")
    if not CLIENTS_CSV.exists():
        flash("Upload the client database first."); return redirect("/clients")
    bid = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    bdir = batch_dir(bid); bdir.mkdir(parents=True)
    try:
        pdf = build_input_pdf(ok, bdir)
    except Exception as ex:
        flash(f"Could not read the uploaded file(s): {ex}"); return redirect("/")
    JOBS[bid] = {"msg": "Queued…", "frac": 0.0, "done": False, "error": None, "pdf": pdf.name,
                 "note": request.form.get("note", "")}
    threading.Thread(target=process_in_background, args=(bid, pdf, request.form.get("note", "")), daemon=True).start()
    return redirect(f"/batch/{bid}")


@app.get("/api/job/<bid>")
def api_job(bid):
    return jsonify(JOBS.get(bid, {"msg": "unknown", "frac": 0, "done": True, "error": "No such job"}))


@app.get("/batch/<bid>")
def batch(bid):
    b = load_batch(bid)
    all_clients = sorted(c["company_name"] for c in read_clients())
    if b:
        for e in b["emails"]:
            e["n_issues"] = len(client_issues(b, e))
    job = JOBS.get(bid, {"msg": "Not found (was the app restarted mid-run?)", "frac": 0, "error": None})
    return render_template_string(BATCH, bid=bid, b=b, job=job, all_clients=all_clients)


@app.get("/batch/<bid>/file/<path:rel>")
def batch_file(bid, rel):
    if rel.startswith("letters/"):
        b = load_batch(bid)
        if b:
            now = dt.datetime.now().isoformat(timespec="seconds")
            for L in b["letters"]:
                if L["file"] == rel:
                    L["opened_at"] = now
                    L["opens"] = int(L.get("opens") or 0) + 1
            save_batch(bid, b)
    return send_from_directory(batch_dir(bid), rel)


@app.get("/batch/<bid>/download/<path:rel>")
def batch_download(bid, rel):
    b = load_batch(bid)
    if b and siu_blocked(b, [rel]):
        flash("Warning: this letter does NOT have the SIU office in the address – remember to notify the customer.")
    mark_downloaded(bid, [rel], "single")
    return send_from_directory(batch_dir(bid), rel, as_attachment=True)


@app.get("/batch/<bid>/client_zip/<int:i>")
def client_zip(bid, i):
    import zipfile, tempfile
    b = load_batch(bid) or abort(404)
    e = b["emails"][i]
    bdir = batch_dir(bid)
    files = [bdir / L["file"] for L in b["letters"] if L["client"] and L["client"]["company_name"] == e["company"]]
    blocked = siu_blocked(b, [str(f.relative_to(bdir)) for f in files])
    if blocked:
        flash(f"Warning for {e['company']}: SIU office missing on {', '.join(blocked)} – remember to notify the customer.")
    mark_downloaded(bid, [str(f.relative_to(bdir)) for f in files], "client")
    if len(files) == 1:
        return send_from_directory(files[0].parent, files[0].name, as_attachment=True)
    tmp = Path(tempfile.gettempdir()) / f"mailsort_{bid}_{i}.zip"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.name)
    return send_from_directory(tmp.parent, tmp.name, as_attachment=True, download_name=f"{ms.slug(e['company'])}_{bid}.zip")


@app.get("/batch/<bid>/zip")
def batch_zip(bid):
    import shutil, tempfile
    bdir = batch_dir(bid)
    b = load_batch(bid)
    ok = (b or {}).get("letters", [])
    bad = [L["letter_id"] for L in ok if not L.get("siu_ok", True)]
    if bad:
        flash(f"Warning: SIU office missing on {', '.join(bad)} (included in the zip) – remember to notify the customer(s).")
    mark_downloaded(bid, [L["file"] for L in ok], "all")
    import zipfile
    tmp = Path(tempfile.gettempdir()) / f"mailsort_{bid}.zip"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for L in ok:
            z.write(bdir / L["file"], L["file"].replace("letters/", "", 1))
        if (bdir / "manifest.csv").exists():
            z.write(bdir / "manifest.csv", "manifest.csv")
    return send_from_directory(tmp.parent, tmp.name, as_attachment=True, download_name=f"{bid}_letters.zip")
    shutil.make_archive(str(tmp), "zip", bdir / "letters")
    return send_from_directory(tmp.parent, tmp.name + ".zip", as_attachment=True, download_name=f"{bid}_letters.zip")


@app.get("/batch/<bid>/email/<int:i>")
def email_preview(bid, i):
    b = load_batch(bid) or abort(404)
    e = b["emails"][i]
    subject, body = draft_parts(batch_dir(bid), e)
    atts = [Path(L["file"]).name for L in b["letters"] if L["client"] and L["client"]["company_name"] == e["company"] and L.get("in_package", True)]
    return render_template_string(EMAIL, bid=bid, i=i, e=e, subject=subject, body=body, atts=atts)


def _send_one(bid: str, b: dict, i: int, cfg: dict):
    e = b["emails"][i]
    # SAFETY GATE: confidential post must never go out on an unverified or conflicting match.
    bad = [L["letter_id"] for L in b["letters"]
           if L.get("client") and L["client"]["company_name"] == e["company"]
           and (L.get("unit_mismatch") or L.get("match_state") == "review")]
    if bad:
        raise RuntimeError(f"BLOCKED – letter(s) {', '.join(bad)} are not verified to belong to "
                           f"{e['company']}. Confirm each match on the letter row first.")
    bdir = batch_dir(bid)
    subject, body = draft_parts(bdir, e)
    atts = [bdir / L["file"] for L in b["letters"]
            if L["client"] and L["client"]["company_name"] == e["company"] and L.get("in_package", True)
            and not L.get("unit_mismatch") and L.get("match_state") != "review"
            and not L.get("emailed_at")] \
        if cfg.get("attach_pdfs") else []
    send_email(cfg, e["email"], subject, body, atts)
    e["sent_at"] = dt.datetime.now().isoformat(timespec="seconds"); e["sent_to"] = e["email"]


@app.post("/batch/<bid>/send/<int:i>")
def send_one(bid, i):
    b = load_batch(bid) or abort(404)
    try:
        _send_one(bid, b, i, load_config()); save_batch(bid, b)
        flash(f"Sent to {b['emails'][i]['email']}.")
    except Exception as ex:
        flash(f"Could not send: {ex}")
    return redirect(f"/batch/{bid}")


@app.get("/batch/<bid>/issues/<int:i>")
def issues_preview(bid, i):
    b = load_batch(bid) or abort(404)
    e = b["emails"][i]
    subject, body = issues_email_parts(b, e, load_config())
    return render_template_string(ISSUES, bid=bid, i=i, e=e, subject=subject, body=body,
                                  n_issues=len(client_issues(b, e)))


@app.post("/batch/<bid>/issues_send/<int:i>")
def issues_send(bid, i):
    b = load_batch(bid) or abort(404)
    e = b["emails"][i]
    if not e.get("email"):
        flash("This client has no email address on file.")
        return redirect(f"/batch/{bid}")
    cfg = load_config()
    subject, body = issues_email_parts(b, e, cfg)
    try:
        send_email(cfg, e["email"], subject, body, [])
        e["issues_sent_at"] = dt.datetime.now().isoformat(timespec="seconds")
        save_batch(bid, b)
        flash(f"Issues email sent to {e['email']}.")
    except Exception as ex:
        flash(f"Could not send issues email: {ex}")
    return redirect(f"/batch/{bid}")


@app.post("/batch/<bid>/mark_sent/<int:i>")
def mark_sent(bid, i):
    b = load_batch(bid) or abort(404)
    e = b["emails"][i]
    if e.get("manual_sent_at"):
        e["manual_sent_at"] = None
        flash(f"{e['company']} unmarked.")
    else:
        e["manual_sent_at"] = dt.datetime.now().isoformat(timespec="seconds")
        flash(f"{e['company']} marked as sent by staff.")
    save_batch(bid, b)
    return redirect(f"/batch/{bid}")


@app.post("/batch/<bid>/merge/<lid>")
def merge_letter(bid, lid):
    """Merge letter <lid> into the letter above it (they are one document split in two)."""
    b = load_batch(bid) or abort(404)
    idx = next((i for i, x in enumerate(b["letters"]) if x["letter_id"] == lid), None)
    if idx is None or idx == 0:
        flash("Cannot merge the first letter – there is nothing above it.")
        return redirect(f"/batch/{bid}")
    bdir = batch_dir(bid)
    prev, cur = b["letters"][idx - 1], b["letters"][idx]
    src_pdf = bdir / b["pdf"]
    if not src_pdf.exists():
        flash("Original scan file no longer exists – cannot rebuild the PDF.")
        return redirect(f"/batch/{bid}")
    pages = sorted(set(prev["pages"] + cur["pages"]))
    doc = ms.fitz.open(src_pdf)
    new = ms.fitz.open()
    for pg in pages:
        new.insert_pdf(doc, from_page=pg - 1, to_page=pg - 1)
    new.save(bdir / prev["file"])
    new.close(); doc.close()
    try:
        (bdir / cur["file"]).unlink(missing_ok=True)
    except OSError:
        pass
    prev["pages"] = pages
    prev["summary"] = (prev.get("summary") or "")
    prev["siu_ok"] = prev.get("siu_ok", True) or cur.get("siu_ok", False)
    if cur.get("match_state") == "review" or cur.get("unit_mismatch"):
        prev["match_state"] = "review"
        prev["match_note"] = "merged with an unverified letter – re-confirm the client"
    b["letters"].pop(idx)
    # refresh the per-client email letter counts
    for e in b["emails"]:
        e["letters"] = sum(1 for L in b["letters"] if L.get("client") and L["client"]["company_name"] == e["company"])
    b["summary"] = f"{len(b['letters'])} letters after merge – {b.get('summary','')}".split(" – ")[0] + " (" + b.get("pdf", "") + ")"
    save_batch(bid, b)
    flash(f"{lid} merged into {prev['letter_id']} – it is now one PDF of pages {'-'.join(map(str, pages))}.")
    return redirect(f"/batch/{bid}")


def _base_action(e: dict) -> str:
    status = (e.get("status") or "").lower()
    return "SEND" if status in ("active",) else f"HOLD – account status is '{status or 'unknown'}'"


def recompute_email_flags(b: dict):
    for e in b["emails"]:
        ls = [L for L in b["letters"] if L.get("client") and L["client"]["company_name"] == e["company"]]
        e["letters"] = len(ls)
        mismatches = [L["letter_id"] for L in ls if L.get("unit_mismatch")]
        unverified = [L["letter_id"] for L in ls if L.get("match_state") == "review"]
        e["unit_mismatch_ids"] = mismatches
        if ls and all(L.get("emailed_at") for L in ls) and not e["sent_at"] and not e.get("manual_sent_at"):
            e["action"] = "DONE – every letter already emailed individually"
            continue
        if mismatches:
            e["action"] = f"HOLD – UNIT MISMATCH on {', '.join(mismatches)} (check before sending!)"
        elif unverified:
            e["action"] = f"HOLD – match not verified on {', '.join(unverified)} (confirm the client first)"
        elif e["action"].startswith("HOLD – UNIT MISMATCH") or e["action"].startswith("HOLD – match not verified"):
            e["action"] = _base_action(e)


@app.post("/batch/<bid>/assign/<lid>")
def assign_letter(bid, lid):
    b = load_batch(bid) or abort(404)
    L = next((x for x in b["letters"] if x["letter_id"] == lid), None) or abort(404)
    company = (request.form.get("company") or "").strip()
    if company == "__unmatch__":
        L["client"] = None
        L["match_state"] = "review"
        L["match_score"] = 0.0
        flash(f"{lid} set to unmatched.")
    else:
        c = next((x for x in read_clients() if x["company_name"] == company), None)
        if not c:
            flash("Client not found in the database.")
            return redirect(f"/batch/{bid}")
        L["client"] = {k: c.get(k, "") for k in REQUIRED_COLS}
        L["match_state"] = "verified"
        L["match_note"] = "confirmed by staff"
        L["matched_by"] = "staff"
        L["match_score"] = 1.0
        L["unit_mismatch"] = False
        L["needs_review"] = "; ".join(x for x in (L.get("needs_review") or "").split("; ")
                                      if x and "MATCH NOT VERIFIED" not in x and "UNIT MISMATCH" not in x and "no client match" not in x and "fuzzy match" not in x)
        notes, _ = learn_from_verified(b, only_letter_id=lid)
        extra = (" " + "; ".join(notes) + " (learned from the letter – existing values are never changed).") if notes else ""
        flash(f"{lid} confirmed as {company}.{extra}")
    recompute_email_flags(b)
    save_batch(bid, b)
    return redirect(f"/batch/{bid}")


def _send_letter_email(bid: str, L: dict, cfg: dict):
    """Email ONE letter to its verified client. Raises on failure. SAFETY: refuses unverified matches."""
    c = L.get("client")
    if not c or not c.get("email"):
        raise RuntimeError("no matched client email")
    if L.get("match_state") == "review" or L.get("unit_mismatch"):
        raise RuntimeError("match not verified")
    today = dt.date.today().strftime("%d/%m/%Y")
    fields = dict(company=c["company_name"], contact=c.get("contact_name") or "Client", date=today,
                  sender=L.get("sender") or "unknown sender", summary=L.get("summary", ""),
                  urgent_note=("\n\n  This item looks time-sensitive – please review it promptly."
                               if L.get("urgency") == "high" else ""),
                  sender_name=cfg["sender_name"])
    subject = render(get_t("letter_subject"), **fields)
    body = render(get_t("letter_body"), **fields)
    atts = [batch_dir(bid) / L["file"]] if cfg.get("attach_pdfs") else []
    send_email(cfg, c["email"], subject, body, atts)
    L["emailed_at"] = dt.datetime.now().isoformat(timespec="seconds")
    L["emailed_to"] = c["email"]


@app.post("/batch/<bid>/send_letter/<lid>")
def send_letter(bid, lid):
    b = load_batch(bid) or abort(404)
    L = next((x for x in b["letters"] if x["letter_id"] == lid), None) or abort(404)
    c = L.get("client")
    if not c or not c.get("email"):
        flash("This letter has no matched client email – add the client first.")
        return redirect(f"/batch/{bid}")
    if L.get("match_state") == "review" or L.get("unit_mismatch"):
        flash(f"{lid} cannot be sent – its client match is not verified. Confirm the correct client on the letter row first.")
        return redirect(f"/batch/{bid}")
    try:
        _send_letter_email(bid, L, load_config())
        save_batch(bid, b)
        flash(f"Letter {lid} sent to {c['email']}.")
    except Exception as ex:
        flash(f"Could not send {lid}: {ex}")
    return redirect(f"/batch/{bid}")


@app.post("/batch/<bid>/rematch")
def rematch_batch(bid):
    """Re-check every held / unverified letter against the CURRENT client database.
    Use after updating the CSV: letters held because their company was missing can now match."""
    b = load_batch(bid) or abort(404)
    clients = ms.load_clients(CLIENTS_CSV)
    if not clients:
        flash("The client database is empty – upload your CSV first.")
        return redirect(f"/batch/{bid}")
    held = [L for L in b["letters"]
            if not L.get("emailed_at")
            and L.get("matched_by") != "staff"
            and (not L.get("client") or L.get("match_state") == "review")]
    if not held:
        flash("No held or unverified letters to re-check in this batch.")
        return redirect(f"/batch/{bid}")
    cfg = load_config()
    ms.match_letters(held, clients, use_ai=bool(cfg.get("anthropic_api_key")),
                     api_key=cfg.get("anthropic_api_key") or None, model=cfg.get("model") or None)
    now = dt.datetime.now().isoformat(timespec="seconds")
    verified, suggested, still_held = 0, 0, 0
    for L in held:
        if L.get("client"):
            L["client"] = {k: L["client"].get(k, "") for k in REQUIRED_COLS}
        if L.get("match_state") == "verified" and L.get("client"):
            L["rematched_at"] = now
            verified += 1
        elif L.get("client") or L.get("suggested_client"):
            suggested += 1
        else:
            still_held += 1
    recompute_email_flags(b)
    save_batch(bid, b)
    flash(f"Re-checked {len(held)} held letter(s) against the current client list: "
          f"{verified} now verified (ready to send), {suggested} have a possible match for staff to confirm, "
          f"{still_held} still have no match. Nothing has been emailed yet.")
    return redirect(f"/batch/{bid}")


def _clean_letter_address(L: dict) -> str:
    """The address as printed on the letter, with the leading company-name line removed."""
    addr = (L.get("address") or "").strip()
    name = (L.get("recipient_company") or "").strip()
    if addr and name:
        parts = [p.strip() for p in addr.split(",")]
        if parts and ms.normalise_name(parts[0]) == ms.normalise_name(name):
            addr = ", ".join(parts[1:]).strip()
    return addr


def learn_from_verified(b: dict, only_letter_id: str | None = None):
    """Fill BLANK siu / address fields in the client database from this batch's VERIFIED letters.
    SAFETY: never overwrites an existing value, never learns from an unverified or mismatched
    letter, and never creates a duplicate unit number. Returns (notes, changed)."""
    rows = read_clients()
    by_name = {(r.get("company_name") or "").strip().lower(): r for r in rows}
    units_in_use = {}
    for r in rows:
        u = ms.normalise_unit(r.get("siu") or "")
        if u:
            units_in_use.setdefault(u, (r.get("company_name") or "").strip())
    notes, changed = [], False
    for L in b["letters"]:
        if only_letter_id and L["letter_id"] != only_letter_id:
            continue
        c = L.get("client")
        if not c or L.get("match_state") != "verified" or L.get("unit_mismatch"):
            continue
        row = by_name.get((c.get("company_name") or "").strip().lower())
        if not row:
            continue
        u = ms.normalise_unit(L.get("unit") or "")
        if u and not (row.get("siu") or "").strip():
            owner = units_in_use.get(u)
            if owner and owner.lower() != (row.get("company_name") or "").strip().lower():
                notes.append(f"unit {u} NOT saved for {row['company_name']} ({L['letter_id']}) – it is already recorded for {owner}")
            else:
                row["siu"] = u
                units_in_use[u] = (row.get("company_name") or "").strip()
                changed = True
                notes.append(f"{row['company_name']}: unit set to {u}")
        addr = _clean_letter_address(L)
        if addr and not (row.get("address") or "").strip():
            row["address"] = addr
            changed = True
            notes.append(f"{row['company_name']}: address saved")
        for k in ("siu", "address"):
            if not (c.get(k) or "").strip() and (row.get(k) or "").strip():
                c[k] = row[k]
    if changed:
        write_clients(rows)
    return notes, changed


@app.post("/batch/<bid>/learn")
def learn_batch(bid):
    b = load_batch(bid) or abort(404)
    notes, changed = learn_from_verified(b)
    if changed:
        save_batch(bid, b)
    units = [n for n in notes if ": unit set" in n]
    addrs = [n for n in notes if ": address saved" in n]
    warns = [n for n in notes if "NOT saved" in n]
    if notes:
        msg = (f"Learned from this batch's verified letters: {len(units)} unit number(s) and "
               f"{len(addrs)} address(es) added to the client database. Existing values were not touched.")
        if warns:
            msg += " ⚠ " + "; ".join(warns[:5]) + ("; …" if len(warns) > 5 else "")
    else:
        msg = "Nothing to learn – every verified letter's client already has a unit and address on file."
    flash(msg)
    return redirect(f"/batch/{bid}")


@app.post("/batch/<bid>/send_held")
def send_held(bid):
    """Send every re-checked letter that is now VERIFIED (after a CSV update + re-check).
    Unverified, out-of-package, non-active or already-sent letters are never touched."""
    b = load_batch(bid) or abort(404)
    cfg = load_config()
    ok, skipped, failed = 0, [], []
    for L in b["letters"]:
        if not L.get("rematched_at") or L.get("emailed_at"):
            continue
        c = L.get("client")
        if not c or L.get("match_state") != "verified" or L.get("unit_mismatch"):
            continue
        if not c.get("email"):
            skipped.append(f"{L['letter_id']} (no email on file)"); continue
        if (c.get("status") or "").lower() not in ms.STATUS_OK:
            skipped.append(f"{L['letter_id']} (status {c.get('status')})"); continue
        if not L.get("in_package", True):
            skipped.append(f"{L['letter_id']} (not in {c.get('package') or 'their'} package)"); continue
        try:
            _send_letter_email(bid, L, cfg); ok += 1
        except Exception as ex:
            failed.append(f"{L['letter_id']}: {ex}")
    save_batch(bid, b)
    msg = f"Sent {ok} re-checked letter(s)."
    if skipped: msg += " Skipped: " + "; ".join(skipped) + "."
    if failed: msg += " Failed: " + "; ".join(failed) + "."
    flash(msg)
    return redirect(f"/batch/{bid}")


@app.post("/batch/<bid>/send_all")
def send_all(bid):
    b = load_batch(bid) or abort(404)
    cfg, ok, fail = load_config(), 0, []
    for i, e in enumerate(b["emails"]):
        if e["sent_at"] or e.get("manual_sent_at") or not e["action"].startswith("SEND") or not e["email"]:
            continue
        try:
            _send_one(bid, b, i, cfg); ok += 1
        except Exception as ex:
            fail.append(f"{e['company']}: {ex}")
    save_batch(bid, b)
    flash(f"Sent {ok} email(s)." + (" Failed: " + "; ".join(fail) if fail else ""))
    return redirect(f"/batch/{bid}")


DELETE_PASSWORD = os.environ.get("DELETE_PASSWORD", "askusama")


@app.post("/batch/<bid>/delete")
def delete_batch(bid):
    import shutil
    if request.form.get("pw", "") != DELETE_PASSWORD:
        flash("Wrong delete password – batch NOT deleted.")
        return redirect("/")
    bdir = batch_dir(bid)
    if not bdir.exists():
        abort(404)
    shutil.rmtree(bdir)
    JOBS.pop(bid, None)
    flash(f"Batch {bid} deleted – its letters, emails and history entries are gone.")
    return redirect("/")


@app.get("/clients")
def clients():
    mt = dt.datetime.fromtimestamp(CLIENTS_CSV.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if CLIENTS_CSV.exists() else ""
    reminders = load_reminders()
    due = due_reseller_clients()
    for c in due:
        r = reminders.get(c["_rkey"])
        c["_reminded"] = (r or {}).get("sent_at", "")[:10] if r else ""
    return render_template_string(CLIENTS, clients=read_clients(), mtime=mt, due=due)


@app.post("/clients/upload")
def clients_upload():
    f = request.files.get("csv")
    try:
        raw = f.read()
        rows = parse_client_upload(raw)
        write_clients(rows)
        msg = f"Client database updated – {len(rows)} clients."
        raw_rows = sum(1 for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
                       if any((v or "").strip() for v in r.values()))
        skipped = raw_rows - len(rows)
        if skipped > 0:
            msg += (f" ⚠ {skipped} row(s) were SKIPPED because they have no company_name – those clients are "
                    f"NOT in the database and their letters can never be matched. For personal/sole-trader "
                    f"clients, put the person's name in the company_name column and re-upload.")
        no_siu = sum(1 for r in rows if not (r.get("siu") or "").strip())
        no_addr = sum(1 for r in rows if not (r.get("address") or "").strip())
        if no_siu or no_addr:
            parts = []
            if no_siu: parts.append(f"{no_siu} of {len(rows)} clients have NO siu (unit/office number)")
            if no_addr: parts.append(f"{no_addr} have NO address")
            msg += (" ⚠ IMPORTANT: " + " and ".join(parts) + ". The unit number and registered address are "
                    "MailSort's strongest proof of who a letter belongs to – without them, matching falls back "
                    "on company names alone and far more letters will be HELD for manual review. Please add "
                    "'siu' and 'address' columns to your CSV.")
        flash(msg)
    except Exception as ex:
        flash(f"Could not read the file: {ex}")
    return redirect("/clients")


@app.post("/clients/remind/<path:reseller>")
def remind_reseller(reseller):
    sent, errors = send_renewal_reminders(manual_reseller=reseller)
    if errors:
        flash("Problem: " + "; ".join(errors))
    elif sent:
        flash(f"Renewal reminder sent to {reseller} covering {sent} client(s).")
    else:
        flash(f"No clients of {reseller} are within {REMIND_DAYS} days of renewal.")
    return redirect("/clients")


@app.get("/clients/compliance")
def compliance_page():
    cfg = load_config()
    affected = compliance_list()
    for c in affected:
        c["_subject"], c["_body"] = compliance_email(c, cfg)
    return render_template_string(COMPLIANCE, affected=affected)


@app.post("/clients/compliance_send")
def compliance_send():
    company = request.form.get("company") or None
    sent, errors = send_compliance([company] if company else None)
    msg = f"Compliance notice sent to {sent} client(s)."
    if errors:
        msg += " Problems: " + "; ".join(errors[:5]) + ("…" if len(errors) > 5 else "")
    flash(msg)
    return redirect("/clients/compliance")


@app.post("/clients/save")
def clients_save():
    rows = read_clients()
    new = {k: request.form.get(k, "").strip() for k in REQUIRED_COLS}
    new["status"] = (new["status"] or "active").lower()
    new["package"] = (new.get("package") or "").capitalize()
    if new["package"] not in PACKAGES:
        new["package"] = ""
    d = ms.parse_date(new.get("start_date", ""))
    new["start_date"] = d.isoformat() if d else ""
    if new.get("kyc") not in ("yes", "no"):
        new["kyc"] = ""
    new["siu"] = (new.get("siu") or "").strip().upper()
    key = ms.normalise_name(new["company_name"])
    for r in rows:
        if ms.normalise_name(r["company_name"]) == key:
            r.update({k: v for k, v in new.items() if v or k == "status"}); break
    else:
        rows.append(new)
    write_clients(rows); flash(f"Saved {new['company_name']}.")
    return redirect("/clients")


SAMPLE_CSV = """client_id,company_name,contact_name,email,status,package,start_date,reseller,reseller_email,kyc,siu,address
C001,Bluebird Consulting Ltd,Sarah Khan,sarah@bluebirdconsulting.co.uk,active,Premium,2026-03-01,,,yes,A80,"SIU A80, 71-75 Shelton Street, Covent Garden, London WC2H 9JQ"
C002,Northgate Logistics Limited,Tom Reid,tom@northgatelogistics.com,active,Standard,2026-01-15,FormationsHub Ltd,accounts@formationshub.com,yes,A81,"SIU A81, 71-75 Shelton Street, Covent Garden, London WC2H 9JQ"
C003,Pixel & Pine Studio Ltd,Amira Osei,hello@pixelandpine.co.uk,active,Basic,2025-06-10,FormationsHub Ltd,accounts@formationshub.com,no,A82,"SIU A82, 71-75 Shelton Street, Covent Garden, London WC2H 9JQ"
C004,Harrow Property Ventures LLP,James Whitfield,james@harrowpv.com,cancelled,Standard,2024-11-20,,,yes,A83,"SIU A83, 71-75 Shelton Street, Covent Garden, London WC2H 9JQ"
C005,Greenleaf Nutrition Ltd,Priya Nair,priya@greenleafnutrition.com,active,Premium,2026-05-05,BizStart Agency,hello@bizstart.agency,no,A84,"SIU A84, 71-75 Shelton Street, Covent Garden, London WC2H 9JQ"
"""


@app.get("/clients/sample")
def clients_sample():
    return Response(SAMPLE_CSV, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=startitup_clients_sample.csv"})


@app.get("/clients/download")
def clients_download():
    return send_from_directory(DATA, "clients.csv", as_attachment=True)


def run_audit() -> dict:
    """Data-quality and safety audit across the client database and every batch."""
    import collections
    a: dict = {"client_issues": [], "critical": [], "batch_stats": [], "risky_sent": [],
               "match_dist": collections.Counter(), "dup_scans": []}
    clients = read_clients()
    a["n_clients"] = len(clients)
    a["missing_email"] = [c["company_name"] for c in clients if not c.get("email")]
    a["missing_siu"] = [c["company_name"] for c in clients if not c.get("siu")]
    a["missing_address"] = [c["company_name"] for c in clients if not c.get("address")]
    a["missing_start"] = [c["company_name"] for c in clients if not c.get("start_date")]
    units = collections.Counter(c.get("siu") for c in clients if c.get("siu"))
    a["dup_units"] = {u: [c["company_name"] for c in clients if c.get("siu") == u]
                      for u, n in units.items() if n > 1}
    if a["dup_units"]:
        a["critical"].append(f"Duplicate unit numbers in client database: "
                             + "; ".join(f"{u} → {', '.join(v)}" for u, v in a["dup_units"].items())
                             + ". Unit-based matching is DISABLED for these units until fixed.")
    # near-duplicate company names (ambiguity risk)
    norms = [(ms.normalise_name(c["company_name"]), c["company_name"]) for c in clients]
    import difflib as dl
    seen = set()
    a["similar_names"] = []
    for i in range(len(norms)):
        for j in range(i + 1, len(norms)):
            if norms[i][0] and dl.SequenceMatcher(None, norms[i][0], norms[j][0]).ratio() >= 0.78:
                key = tuple(sorted((norms[i][1], norms[j][1])))
                if key not in seen:
                    seen.add(key)
                    a["similar_names"].append(f"{key[0]}  ↔  {key[1]}")
    # batches
    scan_names = collections.Counter()
    if BATCHES.exists():
        for d in sorted(BATCHES.iterdir()):
            b = load_batch(d.name) if d.is_dir() else None
            if not b:
                continue
            ls = b.get("letters", [])
            n = len(ls)
            no_addr = sum(1 for L in ls if not L.get("address"))
            no_unit = sum(1 for L in ls if not L.get("unit"))
            held = sum(1 for L in ls if L.get("match_state") == "review" or L.get("unit_mismatch"))
            for L in ls:
                a["match_dist"][L.get("matched_by") or ("unmatched" if not L.get("client") else "?")] += 1
            scan_names[b.get("pdf", "")] += 1
            heur = b.get("mode") not in ("ai", "file")
            a["batch_stats"].append({"id": b["id"], "n": n, "no_addr": no_addr, "no_unit": no_unit,
                                     "held": held, "heuristic": heur})
            if heur and n:
                a["critical"].append(f"Batch {b['id']} was processed WITHOUT AI (heuristic mode) – its matching is unreliable; check the API key and re-upload that scan.")
            # historical risk: anything actually EMAILED while flagged (pre-lock era)
            sent_companies = {e["company"] for e in b.get("emails", []) if e.get("sent_at")}
            for L in ls:
                flagged = L.get("unit_mismatch") or L.get("match_state") == "review"
                emailed = bool(L.get("emailed_at")) or (L.get("client") and L["client"]["company_name"] in sent_companies)
                if flagged and emailed:
                    a["risky_sent"].append(f"{b['id']} / {L['letter_id']} – sent to "
                                           f"{(L.get('client') or {}).get('company_name', '?')} while flagged "
                                           f"({'unit mismatch' if L.get('unit_mismatch') else 'match not verified'}) – REVIEW MANUALLY")
    a["dup_scans"] = [f"{name} uploaded {n}×" for name, n in scan_names.items() if name and n > 1]
    if a["risky_sent"]:
        a["critical"].append(f"{len(a['risky_sent'])} letter(s) were EMAILED while their match was flagged (before the sending locks) – listed below; verify each went to the right client.")
    return a


@app.get("/audit")
def audit_page():
    return render_template_string(AUDIT, a=run_audit())


@app.route("/templates", methods=["GET", "POST"])
def templates_page():
    if request.method == "POST":
        if request.form.get("reset_all"):
            try:
                TEMPLATES_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            flash("All templates reset to their original wording.")
            return redirect("/templates")
        stored = {}
        for k in TPL_DEFS:
            v = (request.form.get(k) or "").replace("\r\n", "\n")
            if v.strip() and v != tpl_default(k):
                stored[k] = v
        TEMPLATES_FILE.write_text(json.dumps(stored, indent=1))
        flash("Templates saved – all future emails will use the new wording.")
        return redirect("/templates")
    cur = load_templates()
    sections: dict = {}
    for k, (section, label, ph, _d) in TPL_DEFS.items():
        sections.setdefault(section, []).append((k, label, ph, cur[k], k.endswith("_subject") or k == "batch_subject"))
    return render_template_string(TEMPLATES_PAGE, sections=sections)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = load_config()
    if request.method == "POST":
        for k in cfg:
            if k == "attach_pdfs":
                cfg[k] = bool(request.form.get(k))
            elif k in request.form:
                cfg[k] = request.form.get(k).strip()
        save_config(cfg); flash("Settings saved.")
        return redirect("/settings")
    return render_template_string(SETTINGS, c=cfg)


@app.get("/settings/test_email")
def test_email():
    cfg = load_config()
    # deliver the test to a real inbox: TEST_EMAIL_TO variable, else the SMTP username (usually the admin's
    # personal address), else the from-address as a last resort
    to = os.environ.get("TEST_EMAIL_TO") or cfg.get("smtp_user") or cfg.get("from_email")
    try:
        send_email(cfg, to, "MailSort test email",
                   f"If you can read this, MailSort can send email.\nSent from: {cfg.get('from_email')}\n", [])
        flash(f"Test email sent to {to} – check that inbox and look at the FROM address.")
    except Exception as ex:
        flash(f"Test failed: {ex}")
    return redirect("/settings")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"MailSort running – open http://localhost:{port}")
    app.run(host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1", port=port, debug=False, threaded=True)
