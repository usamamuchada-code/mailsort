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
                   send_from_directory, url_for)
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


@app.before_request
def require_login():
    if not AUTH_PASS:
        return None  # local use – no password configured
    a = request.authorization
    if a and a.type == "basic" and a.username == AUTH_USER and a.password == AUTH_PASS:
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="MailSort"'})

JOBS: dict[str, dict] = {}  # batch_id -> {"msg", "frac", "done", "error"}

REQUIRED_COLS = ["client_id", "company_name", "contact_name", "email", "status", "package"]
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
        return list(csv.DictReader(f))


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
             "plan": "package", "subscription": "package", "package_name": "package", "tier": "package"}
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

    try:
        r = ms.run_batch(pdf, CLIENTS_CSV, batch_dir(bid), batch_tag=bid, sender_name=cfg["sender_name"],
                         api_key=cfg.get("anthropic_api_key") or None, model=cfg.get("model"), status=status)
        letters = []
        for L in r["letters"]:
            c = L.get("client") or {}
            letters.append({k: L[k] for k in ("letter_id", "pages", "recipient_company", "sender", "letter_type",
                                              "urgency", "summary", "file", "needs_review", "match_score", "siu_ok")} | {"address": L.get("address", "")}
                           | {"client": {k: c.get(k, "") for k in REQUIRED_COLS} if c else None})
        emails = []
        for e in r["emails"]:
            missing = [L["letter_id"] for L in r["letters"] if L.get("client") and L["client"]["company_name"] == e["company"] and not L.get("siu_ok", True)]
            if missing:
                e = {**e, "action": f"HOLD – SIU office missing on {', '.join(missing)}"}
            pkg = next((L["client"].get("package", "") for L in r["letters"]
                        if L.get("client") and L["client"]["company_name"] == e["company"]), "")
            emails.append({**e, "package": pkg, "sent_at": None, "sent_to": None, "manual_sent_at": None})
        save_batch(bid, {"id": bid, "created": dt.datetime.now().isoformat(timespec="seconds"), "note": note,
                         "pdf": pdf.name, "pages": r["pages"], "mode": r["mode"], "summary": r["summary"],
                         "letters": letters, "emails": emails})
        job["done"] = True
    except Exception as e:  # surface the error in the UI
        job["error"] = f"{type(e).__name__}: {e}"
        job["done"] = True


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
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:22px;margin-bottom:22px}
h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}
th{background:#f7f7f7;font-weight:700}tr.review{background:var(--warn)}tr.sent{background:var(--ok)}
.btn{display:inline-block;background:var(--b);color:#fff;border:0;border-radius:999px;padding:9px 18px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none}
.btn:hover{background:#333}.btn.secondary{background:#efefef;color:#111}.btn.secondary:hover{background:#e2e2e2}.btn.small{padding:5px 10px;font-size:13px}.btn:disabled{opacity:.5}
input[type=text],input[type=password],input[type=number],textarea{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}
.drop{border:2px dashed #94a3b8;border-radius:10px;padding:36px;text-align:center;background:#fafafa}
.drop.over{background:#eef2ff;border-color:var(--b)}
.flash{background:#fef3c7;border:1px solid #fcd34d;padding:10px 14px;border-radius:8px;margin-bottom:16px}
.muted{color:#6b7280;font-size:13px}.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;background:#e5e7eb}
.pill.high{background:#fee2e2;color:#991b1b}.pill.active{background:#dcfce7;color:#166534}.pill.hold{background:#fee2e2;color:#991b1b}
progress{width:100%;height:14px}
</style></head><body>
<header><a class="brand" href="/"><img src="data:image/png;base64,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" alt="Startitup"><span>Mail Room</span></a>
<a href="/">Batches</a><a href="/history">History</a><a href="/clients">Client database</a><a href="/settings">Settings</a></header>
<main>{% with m = get_flashed_messages() %}{% for x in m %}<div class="flash">{{x}}</div>{% endfor %}{% endwith %}
{% block body %}{% endblock %}</main></body></html>"""

HOME = """{% extends "base" %}{% block body %}
<div class="card"><h1>Upload a bulk scan</h1>
{% if not clients %}<div class="flash">No client database yet – <a href="/clients">upload your client list</a> first.</div>{% endif %}
<form method="post" action="/upload" enctype="multipart/form-data" id="f">
<div class="drop" id="drop"><p><b>Drag the scanned PDF here</b> or</p>
<input type="file" name="pdf" accept="application/pdf" required id="file"><p class="muted" id="fname"></p></div>
<label>Note (optional, e.g. "Morning post 26 Aug")</label><input type="text" name="note">
<p><button class="btn" id="go" {% if not clients %}disabled{% endif %}>Upload &amp; sort</button>
<span class="muted">Sorting runs in the background – you can leave this page.</span></p></form></div>
<div class="card"><h2>Recent batches</h2>
{% if not batches %}<p class="muted">Nothing processed yet.</p>{% else %}
<table><tr><th>Batch</th><th>When</th><th>File</th><th>Result</th><th>Emails</th><th></th></tr>
{% for b in batches %}<tr><td>{{b.id}}</td><td>{{b.created}}</td><td>{{b.pdf}}<br><span class="muted">{{b.note}}</span></td>
<td>{% if b.error %}<span style="color:#b91c1c">Failed: {{b.error}}</span>{% elif b.summary %}{{b.summary}}{% else %}{{b.msg}}{% endif %}</td>
<td>{{b.sent}}/{{b.total_emails}} sent</td>
<td><a class="btn small secondary" href="/batch/{{b.id}}">Open</a></td></tr>{% endfor %}</table>{% endif %}</div>
<script>
const d=document.getElementById('drop'),i=document.getElementById('file'),n=document.getElementById('fname');
i.onchange=()=>n.textContent=i.files[0]?i.files[0].name+' ('+(i.files[0].size/1048576).toFixed(1)+' MB)':'';
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
{% set missing = b.letters|rejectattr("siu_ok")|list %}{% if missing %}<div class="flash" style="background:#fee2e2;border-color:#fca5a5"><b>SIU office missing on {{missing|length}} letter(s):</b>
{% for L in missing %}{{L.letter_id}} ({{L.client.company_name if L.client else L.recipient_company or "unknown addressee"}}){% if not loop.last %}, {% endif %}{% endfor %}.
These cannot be opened, downloaded or emailed – please notify the customer(s) that their post must carry the SIU office in the address.</div>{% endif %}
<p><a class="btn secondary small" href="/batch/{{bid}}/file/manifest.csv">Download manifest.csv</a>
<a class="btn secondary small" href="/batch/{{bid}}/zip">Download all letters (zip)</a></p></div>

<div class="card"><h2>Client emails</h2>
<table><tr><th>Client</th><th>Email</th><th>Status</th><th>Letters</th><th>Action</th><th>Sent</th><th></th></tr>
{% for e in b.emails %}<tr class="{{'sent' if e.sent_at or e.manual_sent_at else ('review' if not e.action.startswith('SEND') else '')}}">
<td>{{e.company}}</td><td>{{e.email}}</td><td><span class="pill {{'active' if e.status=='active' else 'hold'}}">{{e.status}}</span>{% if e.package %} <span class="pill">{{e.package}}</span>{% endif %}</td>
<td>{{e.letters}}</td><td>{{e.action}}</td><td>{% if e.sent_at %}{{e.sent_at|replace("T"," ")}} <span class="muted">(emailed)</span>{% elif e.manual_sent_at %}{{e.manual_sent_at|replace("T"," ")}} <span class="muted">(marked by staff)</span>{% else %}–{% endif %}</td>
<td>{% if 'SIU' in e.action %}<span class="btn small secondary" style="opacity:.45" title="SIU office missing">PDFs</span>{% else %}<a class="btn small secondary" href="/batch/{{bid}}/client_zip/{{loop.index0}}">PDFs</a>{% endif %} <a class="btn small secondary" href="/batch/{{bid}}/email/{{loop.index0}}">Preview</a>
<form method="post" action="/batch/{{bid}}/send/{{loop.index0}}" style="display:inline" onsubmit="return confirm('Send to {{e.email}}?')">
<button class="btn small" {% if not e.email or 'SIU' in e.action %}disabled{% endif %}>{{'Re-send' if e.sent_at else 'Send'}}</button></form>
<form method="post" action="/batch/{{bid}}/mark_sent/{{loop.index0}}" style="display:inline">
<button class="btn small secondary">{{'Unmark' if e.manual_sent_at else 'Mark as sent'}}</button></form></td></tr>{% endfor %}</table>
<p style="margin-top:14px"><form method="post" action="/batch/{{bid}}/send_all" onsubmit="return confirm('Send to every ACTIVE client that has not been emailed yet?')">
<button class="btn">Send all active &amp; unsent</button></form></p></div>

<div class="card"><h2>Letters ({{b.letters|length}})</h2><p class="muted">Orange rows need a human check. Downloaded: {{b.letters|selectattr("downloaded_at")|list|length}} of {{b.letters|length}} · opened (viewed only): {{b.letters|rejectattr("downloaded_at")|selectattr("opened_at")|list|length}} · untouched: {{b.letters|rejectattr("downloaded_at")|rejectattr("opened_at")|list|length}}. Green rows are downloaded – e.g. for manual upload to the portal.</p>
<table><tr><th>ID</th><th>Pages</th><th>Addressee (as printed)</th><th>Matched client</th><th>Sender</th><th>Type</th><th>Urgency</th><th>Summary</th><th>SIU</th><th>PDF</th><th>Downloaded</th><th>Review</th></tr>
{% for L in b.letters %}<tr class="{{'review' if L.needs_review else ('sent' if L.downloaded_at else '')}}"{% if not L.siu_ok %} style="background:#fee2e2"{% endif %}><td>{{L.letter_id}}</td><td>{{L.pages|join('-')}}</td>
<td>{{L.recipient_company}}{% if L.address %}<br><span class="muted" style="font-size:12px">{{L.address}}</span>{% endif %}</td><td>{% if L.client %}{{L.client.company_name}} <span class="muted">{{(L.match_score*100)|round|int}}%</span>{% else %}<b>— no match —</b>{% endif %}</td>
<td>{{L.sender}}</td><td>{{L.letter_type}}</td><td><span class="pill {{L.urgency}}">{{L.urgency}}</span></td><td>{{L.summary}}</td>
<td>{% if L.siu_ok %}<span class="pill active">✔</span>{% else %}<span class="pill high">MISSING</span>{% endif %}</td>
<td>{% if L.siu_ok %}<a href="/batch/{{bid}}/file/{{L.file}}" target="_blank">open</a> · <a href="/batch/{{bid}}/download/{{L.file}}">download</a>{% else %}<span class="pill high">blocked</span>{% endif %}</td>
<td>{% if L.downloaded_at %}<span class="pill active">✔ downloaded {{L.downloaded_at|replace("T"," ")}}</span>
{% elif L.opened_at %}<span class="pill">👁 opened ×{{L.opens or 1}} · {{L.opened_at|replace("T"," ")}}</span>
{% else %}<span class="muted">not yet</span>{% endif %}</td><td>{{L.needs_review}}</td></tr>{% endfor %}</table></div>
{% endif %}{% endblock %}"""

EMAIL = """{% extends "base" %}{% block body %}<div class="card"><h1>Email preview – {{e.company}}</h1>
<p><b>To:</b> {{e.email}} &nbsp; <b>Subject:</b> {{subject}}</p><pre style="white-space:pre-wrap;background:#f9fafb;padding:14px;border-radius:8px">{{body}}</pre>
<p><b>Attachments:</b> {% for a in atts %}{{a}}{% if not loop.last %}, {% endif %}{% endfor %}</p>
<p><a class="btn secondary" href="/batch/{{bid}}">Back</a>
<form method="post" action="/batch/{{bid}}/send/{{i}}" style="display:inline" onsubmit="return confirm('Send to {{e.email}}?')"><button class="btn">Send now</button></form></p></div>{% endblock %}"""

CLIENTS = """{% extends "base" %}{% block body %}
<div class="card"><h1>Client database</h1>
<p class="muted">Upload a CSV exported from your portal. It <b>replaces</b> the current list. Headers: <code>client_id, company_name, contact_name, email, status, package</code>
(package = Basic / Standard / Premium; columns named "plan" or "subscription" are understood too)
(status = active / overdue / suspended / cancelled). Only <b>active</b> clients are emailed automatically.</p>
<form method="post" action="/clients/upload" enctype="multipart/form-data"><input type="file" name="csv" accept=".csv,text/csv" required>
<button class="btn">Upload / update database</button></form>
<p class="muted">{{clients|length}} clients on file{% if mtime %} · last updated {{mtime}}{% endif %} · <a href="/clients/download">download current CSV</a></p></div>
<div class="card"><h2>Add or edit one client</h2>
<form method="post" action="/clients/save"><div style="display:grid;grid-template-columns:repeat(6,1fr);gap:10px">
<div><label>Client ID</label><input type="text" name="client_id"></div><div><label>Company name *</label><input type="text" name="company_name" required></div>
<div><label>Contact</label><input type="text" name="contact_name"></div><div><label>Email</label><input type="text" name="email"></div>
<div><label>Status</label><input type="text" name="status" value="active"></div>
<div><label>Package</label><select name="package" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px">
<option value="">—</option><option>Basic</option><option>Standard</option><option>Premium</option></select></div></div>
<p><button class="btn">Save</button> <span class="muted">Matching company name (case-insensitive) is updated; otherwise added.</span></p></form></div>
<div class="card"><h2>Clients</h2><table><tr><th>ID</th><th>Company</th><th>Contact</th><th>Email</th><th>Status</th><th>Package</th></tr>
{% for c in clients %}<tr><td>{{c.client_id}}</td><td>{{c.company_name}}</td><td>{{c.contact_name}}</td><td>{{c.email}}</td>
<td><span class="pill {{'active' if c.status=='active' else 'hold'}}">{{c.status}}</span></td>
<td>{% if c.package %}<span class="pill">{{c.package}}</span>{% else %}<span class="muted">—</span>{% endif %}</td></tr>{% endfor %}</table></div>{% endblock %}"""

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
<div class="card"><h1>History</h1>
<form method="get" style="display:flex;gap:10px;align-items:center;margin-bottom:14px">
<input type="text" name="q" value="{{q}}" placeholder="Filter by client name or email…" style="max-width:360px">
<button class="btn small">Search</button>{% if q %}<a class="btn small secondary" href="/history">Clear</a>{% endif %}
<span class="muted">{{downloaded|length}} letter(s) downloaded · {{sent|length}} email(s) sent{% if q %} matching "{{q}}"{% endif %} · {{batches|length}} batches in total</span></form>
<h2>Letters downloaded</h2>
{% if not downloaded %}<p class="muted">No letters downloaded yet.</p>{% else %}
<table><tr><th>Downloaded</th><th>Client</th><th>Letter</th><th>Sender</th><th>Pages</th><th>Batch</th><th></th></tr>
{% for d in downloaded %}<tr><td>{{d.downloaded_at|replace("T"," ")}}</td><td>{{d.client.company_name if d.client else "— unmatched —"}}</td>
<td>{{d.letter_id}} · {{d.summary}}</td><td>{{d.sender}}</td><td>{{d.pages|join("-")}}</td><td>{{d.batch}}</td>
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
     "history": HISTORY}[name], name, lambda: True)})()


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
    batches, sent, downloaded = [], [], []
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
        for e in emails:
            when = e.get("sent_at") or e.get("manual_sent_at")
            if when and (not q or q in e["company"].lower() or q in (e.get("sent_to") or "").lower()):
                sent.append({**e, "when": when, "how": "emailed" if e.get("sent_at") else "manual",
                             "batch": b["id"], "note": b.get("note", "")})
    sent.sort(key=lambda s: s["when"], reverse=True)
    downloaded.sort(key=lambda d: d["downloaded_at"], reverse=True)
    return render_template_string(HISTORY, batches=batches, sent=sent, downloaded=downloaded, q=q)


@app.post("/upload")
def upload():
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        flash("Please choose a PDF file."); return redirect("/")
    if not CLIENTS_CSV.exists():
        flash("Upload the client database first."); return redirect("/clients")
    bid = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    bdir = batch_dir(bid); bdir.mkdir(parents=True)
    pdf = bdir / secure_filename(f.filename)
    f.save(pdf)
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
    job = JOBS.get(bid, {"msg": "Not found (was the app restarted mid-run?)", "frac": 0, "error": None})
    return render_template_string(BATCH, bid=bid, b=b, job=job)


@app.get("/batch/<bid>/file/<path:rel>")
def batch_file(bid, rel):
    if rel.startswith("letters/"):
        b = load_batch(bid)
        if b and siu_blocked(b, [rel]):
            flash("Blocked: SIU office is missing from the address on this letter – it cannot be opened or downloaded. Please notify the customer.")
            return redirect(f"/batch/{bid}")
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
        flash("Download blocked: SIU office is missing from the address on this letter. Please notify the customer.")
        return redirect(f"/batch/{bid}")
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
        flash(f"Download blocked for {e['company']}: SIU office is missing on {', '.join(blocked)}. Please notify the customer.")
        return redirect(f"/batch/{bid}")
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
    ok = [L for L in (b or {}).get("letters", []) if L.get("siu_ok", True)]
    bad = [L["letter_id"] for L in (b or {}).get("letters", []) if not L.get("siu_ok", True)]
    if bad:
        flash(f"Zip created WITHOUT {', '.join(bad)} – SIU office missing on those letters. Please notify the customer(s).")
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
    atts = [Path(L["file"]).name for L in b["letters"] if L["client"] and L["client"]["company_name"] == e["company"]]
    return render_template_string(EMAIL, bid=bid, i=i, e=e, subject=subject, body=body, atts=atts)


def _send_one(bid: str, b: dict, i: int, cfg: dict):
    e = b["emails"][i]
    if "SIU" in e.get("action", ""):
        raise RuntimeError(f"{e['action']} – notify the customer instead of sending.")
    bdir = batch_dir(bid)
    subject, body = draft_parts(bdir, e)
    atts = [bdir / L["file"] for L in b["letters"] if L["client"] and L["client"]["company_name"] == e["company"]] \
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


@app.get("/clients")
def clients():
    mt = dt.datetime.fromtimestamp(CLIENTS_CSV.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if CLIENTS_CSV.exists() else ""
    return render_template_string(CLIENTS, clients=read_clients(), mtime=mt)


@app.post("/clients/upload")
def clients_upload():
    f = request.files.get("csv")
    try:
        rows = parse_client_upload(f.read())
        write_clients(rows)
        flash(f"Client database updated – {len(rows)} clients.")
    except Exception as ex:
        flash(f"Could not read the file: {ex}")
    return redirect("/clients")


@app.post("/clients/save")
def clients_save():
    rows = read_clients()
    new = {k: request.form.get(k, "").strip() for k in REQUIRED_COLS}
    new["status"] = (new["status"] or "active").lower()
    new["package"] = (new.get("package") or "").capitalize()
    if new["package"] not in PACKAGES:
        new["package"] = ""
    key = ms.normalise_name(new["company_name"])
    for r in rows:
        if ms.normalise_name(r["company_name"]) == key:
            r.update({k: v for k, v in new.items() if v or k == "status"}); break
    else:
        rows.append(new)
    write_clients(rows); flash(f"Saved {new['company_name']}.")
    return redirect("/clients")


@app.get("/clients/download")
def clients_download():
    return send_from_directory(DATA, "clients.csv", as_attachment=True)


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
    try:
        send_email(cfg, cfg.get("from_email") or cfg["smtp_user"], "MailSort test email",
                   "If you can read this, MailSort can send email.\n", [])
        flash("Test email sent – check your inbox.")
    except Exception as ex:
        flash(f"Test failed: {ex}")
    return redirect("/settings")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"MailSort running – open http://localhost:{port}")
    app.run(host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1", port=port, debug=False, threaded=True)
