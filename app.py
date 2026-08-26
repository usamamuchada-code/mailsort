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

import csv, io, json, os, smtplib, threading, uuid, datetime as dt
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

REQUIRED_COLS = ["client_id", "company_name", "contact_name", "email", "status"]


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
             "id": "client_id", "account_status": "status", "subscription_status": "status"}
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
                                              "urgency", "summary", "file", "needs_review", "match_score")}
                           | {"client": {k: c.get(k, "") for k in REQUIRED_COLS} if c else None})
        emails = [{**e, "sent_at": None, "sent_to": None} for e in r["emails"]]
        save_batch(bid, {"id": bid, "created": dt.datetime.now().isoformat(timespec="seconds"), "note": note,
                         "pdf": pdf.name, "pages": r["pages"], "mode": r["mode"], "summary": r["summary"],
                         "letters": letters, "emails": emails})
        job["done"] = True
    except Exception as e:  # surface the error in the UI
        job["error"] = f"{type(e).__name__}: {e}"
        job["done"] = True


# ----------------------------------------------------------------------------- email sending

def send_email(cfg: dict, to: str, subject: str, body: str, attachments: list[Path]) -> None:
    if not (cfg.get("smtp_user") and cfg.get("smtp_password")):
        raise RuntimeError("Email is not configured – open Settings and enter SMTP details.")
    msg = EmailMessage()
    msg["From"] = cfg.get("from_email") or cfg["smtp_user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for p in attachments:
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=60) as s:
        s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.send_message(msg)


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

BASE = """<!doctype html><html><head><meta charset="utf-8"><title>MailSort</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--b:#1d4ed8;--bg:#f6f7fb;--card:#fff;--line:#e5e7eb;--warn:#fff4e5;--ok:#e8f7ee}
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--bg);color:#1f2937}
header{background:#111827;color:#fff;padding:14px 28px;display:flex;gap:28px;align-items:center}
header a{color:#d1d5db;text-decoration:none;font-weight:500}header a.brand{color:#fff;font-size:18px;font-weight:700}
main{max-width:1200px;margin:28px auto;padding:0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:22px;margin-bottom:22px}
h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}
th{background:#f9fafb;font-weight:600}tr.review{background:var(--warn)}tr.sent{background:var(--ok)}
.btn{display:inline-block;background:var(--b);color:#fff;border:0;border-radius:7px;padding:9px 16px;font-size:14px;cursor:pointer;text-decoration:none}
.btn.secondary{background:#e5e7eb;color:#111}.btn.small{padding:5px 10px;font-size:13px}.btn:disabled{opacity:.5}
input[type=text],input[type=password],input[type=number],textarea{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}
.drop{border:2px dashed #94a3b8;border-radius:10px;padding:36px;text-align:center;background:#fafafa}
.drop.over{background:#eef2ff;border-color:var(--b)}
.flash{background:#fef3c7;border:1px solid #fcd34d;padding:10px 14px;border-radius:8px;margin-bottom:16px}
.muted{color:#6b7280;font-size:13px}.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;background:#e5e7eb}
.pill.high{background:#fee2e2;color:#991b1b}.pill.active{background:#dcfce7;color:#166534}.pill.hold{background:#fee2e2;color:#991b1b}
progress{width:100%;height:14px}
</style></head><body>
<header><a class="brand" href="/">MailSort</a><a href="/">Batches</a><a href="/clients">Client database</a><a href="/settings">Settings</a></header>
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
<p><a class="btn secondary small" href="/batch/{{bid}}/file/manifest.csv">Download manifest.csv</a>
<a class="btn secondary small" href="/batch/{{bid}}/zip">Download all letters (zip)</a></p></div>

<div class="card"><h2>Client emails</h2>
<table><tr><th>Client</th><th>Email</th><th>Status</th><th>Letters</th><th>Action</th><th>Sent</th><th></th></tr>
{% for e in b.emails %}<tr class="{{'sent' if e.sent_at else ('review' if not e.action.startswith('SEND') else '')}}">
<td>{{e.company}}</td><td>{{e.email}}</td><td><span class="pill {{'active' if e.status=='active' else 'hold'}}">{{e.status}}</span></td>
<td>{{e.letters}}</td><td>{{e.action}}</td><td>{{e.sent_at or '–'}}</td>
<td><a class="btn small secondary" href="/batch/{{bid}}/email/{{loop.index0}}">Preview</a>
<form method="post" action="/batch/{{bid}}/send/{{loop.index0}}" style="display:inline" onsubmit="return confirm('Send to {{e.email}}?')">
<button class="btn small" {% if not e.email %}disabled{% endif %}>{{'Re-send' if e.sent_at else 'Send'}}</button></form></td></tr>{% endfor %}</table>
<p style="margin-top:14px"><form method="post" action="/batch/{{bid}}/send_all" onsubmit="return confirm('Send to every ACTIVE client that has not been emailed yet?')">
<button class="btn">Send all active &amp; unsent</button></form></p></div>

<div class="card"><h2>Letters ({{b.letters|length}})</h2><p class="muted">Orange rows need a human check.</p>
<table><tr><th>ID</th><th>Pages</th><th>Addressee (as printed)</th><th>Matched client</th><th>Sender</th><th>Type</th><th>Urgency</th><th>Summary</th><th>PDF</th><th>Review</th></tr>
{% for L in b.letters %}<tr class="{{'review' if L.needs_review else ''}}"><td>{{L.letter_id}}</td><td>{{L.pages|join('-')}}</td>
<td>{{L.recipient_company}}</td><td>{% if L.client %}{{L.client.company_name}} <span class="muted">{{(L.match_score*100)|round|int}}%</span>{% else %}<b>— no match —</b>{% endif %}</td>
<td>{{L.sender}}</td><td>{{L.letter_type}}</td><td><span class="pill {{L.urgency}}">{{L.urgency}}</span></td><td>{{L.summary}}</td>
<td><a href="/batch/{{bid}}/file/{{L.file}}" target="_blank">open</a></td><td>{{L.needs_review}}</td></tr>{% endfor %}</table></div>
{% endif %}{% endblock %}"""

EMAIL = """{% extends "base" %}{% block body %}<div class="card"><h1>Email preview – {{e.company}}</h1>
<p><b>To:</b> {{e.email}} &nbsp; <b>Subject:</b> {{subject}}</p><pre style="white-space:pre-wrap;background:#f9fafb;padding:14px;border-radius:8px">{{body}}</pre>
<p><b>Attachments:</b> {% for a in atts %}{{a}}{% if not loop.last %}, {% endif %}{% endfor %}</p>
<p><a class="btn secondary" href="/batch/{{bid}}">Back</a>
<form method="post" action="/batch/{{bid}}/send/{{i}}" style="display:inline" onsubmit="return confirm('Send to {{e.email}}?')"><button class="btn">Send now</button></form></p></div>{% endblock %}"""

CLIENTS = """{% extends "base" %}{% block body %}
<div class="card"><h1>Client database</h1>
<p class="muted">Upload a CSV exported from your portal. It <b>replaces</b> the current list. Headers: <code>client_id, company_name, contact_name, email, status</code>
(status = active / overdue / suspended / cancelled). Only <b>active</b> clients are emailed automatically.</p>
<form method="post" action="/clients/upload" enctype="multipart/form-data"><input type="file" name="csv" accept=".csv,text/csv" required>
<button class="btn">Upload / update database</button></form>
<p class="muted">{{clients|length}} clients on file{% if mtime %} · last updated {{mtime}}{% endif %} · <a href="/clients/download">download current CSV</a></p></div>
<div class="card"><h2>Add or edit one client</h2>
<form method="post" action="/clients/save"><div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px">
<div><label>Client ID</label><input type="text" name="client_id"></div><div><label>Company name *</label><input type="text" name="company_name" required></div>
<div><label>Contact</label><input type="text" name="contact_name"></div><div><label>Email</label><input type="text" name="email"></div>
<div><label>Status</label><input type="text" name="status" value="active"></div></div>
<p><button class="btn">Save</button> <span class="muted">Matching company name (case-insensitive) is updated; otherwise added.</span></p></form></div>
<div class="card"><h2>Clients</h2><table><tr><th>ID</th><th>Company</th><th>Contact</th><th>Email</th><th>Status</th></tr>
{% for c in clients %}<tr><td>{{c.client_id}}</td><td>{{c.company_name}}</td><td>{{c.contact_name}}</td><td>{{c.email}}</td>
<td><span class="pill {{'active' if c.status=='active' else 'hold'}}">{{c.status}}</span></td></tr>{% endfor %}</table></div>{% endblock %}"""

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

app.jinja_loader = type("L", (), {"get_source": lambda self, env, name: (
    {"base": BASE, "home": HOME, "batch": BATCH, "email": EMAIL, "clients": CLIENTS, "settings": SETTINGS}[name], name, lambda: True)})()


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
                        "sent": sum(1 for e in b.get("emails", []) if e.get("sent_at")),
                        "total_emails": len(b.get("emails", []))})
    return render_template_string(HOME, clients=read_clients(), batches=batches[:50])


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
    return send_from_directory(batch_dir(bid), rel)


@app.get("/batch/<bid>/zip")
def batch_zip(bid):
    import shutil, tempfile
    bdir = batch_dir(bid)
    tmp = Path(tempfile.gettempdir()) / f"mailsort_{bid}"
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


@app.post("/batch/<bid>/send_all")
def send_all(bid):
    b = load_batch(bid) or abort(404)
    cfg, ok, fail = load_config(), 0, []
    for i, e in enumerate(b["emails"]):
        if e["sent_at"] or not e["action"].startswith("SEND") or not e["email"]:
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
