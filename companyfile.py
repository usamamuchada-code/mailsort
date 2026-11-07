"""
MailSort v42a – Company files (one file/folder per company, built from the mail room).

Self-contained add-on: app.py only imports this file and calls register(app, load_batch, read_clients).

  /companies                 -> every client with the three key documents ticked / missing
  /company?name=<company>    -> one company's file: checklist + every letter ever received + ZIP
  /company/zip?name=...      -> all VERIFIED letters of that company in one ZIP
  /company/letterkind (POST) -> staff confirm / correct what a letter is
  /company/manual    (POST)  -> staff tick a document received outside the mail room (or undo)

The three key documents:
  ch_auth_code     Companies House – Authentication Code
  hmrc_utr         HMRC – Corporation Tax UTR letter
  hmrc_activation  HMRC – UTR / online services activation code

How a document gets ticked
  • AI read the letter as that document (new batches, v42a+)          → ticked automatically
  • old letters: the scanned text looks like that document              → "possible – check" (amber),
    one click Confirm / Not this
  • staff set a letter's type, or tick "received outside the mail room" → ticked
SAFETY: only letters whose client match is VERIFIED (and has no unit mismatch) count towards a
company's file – a letter still waiting for a match check is listed separately and never ticks.
Nothing here changes batches, letters or emails: it only reads them. Staff decisions are kept in
DATA/companyfile/overrides.json; a cache of the letter list in DATA/companyfile/index.json.
"""
from __future__ import annotations

import datetime as dt, io, json, os, re, threading, zipfile
from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template_string, request, send_file

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
BATCHES = DATA / "batches"
OUT = DATA / "companyfile"
INDEX_FILE = OUT / "index.json"
OVR_FILE = OUT / "overrides.json"
INDEX_VERSION = 1

DOCS = [  # key, label, sender, icon
    ("ch_auth_code", "Authentication Code", "Companies House", "🔑"),
    ("hmrc_utr", "UTR letter", "HMRC", "🧾"),
    ("hmrc_activation", "UTR activation code", "HMRC", "🔓"),
]
DOC_KEYS = [d[0] for d in DOCS]
DOC_LABEL = {d[0]: d[1] for d in DOCS}

bp = Blueprint("companyfile", __name__)
_lock = threading.Lock()
_cache: dict = {"index": None, "alerts": 0}
_hooks: dict = {}


# ----------------------------------------------------------------------------- helpers

def ckey(name: str) -> str:
    """Company key = the exact company name, trimmed and lower-cased (same rule the mail room uses)."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def load_overrides() -> dict:
    o = _read_json(OVR_FILE, {})
    o.setdefault("letters", {})   # "bid/lid" -> {kind: <doc key>|"none", by, when}
    o.setdefault("manual", {})    # company key -> {doc key: {date, note, by, when}}
    return o


def text_kind(text: str) -> str:
    """Rough check on the scanned text, for letters the AI did not label (old batches).
    Only ever gives a POSSIBLE match – staff confirm it with one click."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    if not t:
        return ""
    hmrc = ("hmrc" in t) or ("hm revenue" in t) or ("revenue & customs" in t) or ("revenue and customs" in t)
    reminder = re.search(r"notice to (deliver|file)|penalt|overdue|reminder|confirmation statement is due|accounts are due", t)
    if hmrc and re.search(r"activation\s*code", t) and not re.search(r"activation code (has )?expired", t):
        return "hmrc_activation"
    if ("companies house" in t) and re.search(r"authenti[cs]ation\s*code", t) and not reminder:
        if re.search(r"(your|the|company)\s+(company\s+)?authenti[cs]ation\s*code\s*(is|:|-)", t) \
                or re.search(r"authenti[cs]ation\s*code\s*[:\-]?\s*[a-z0-9]{6}\b", t):
            return "ch_auth_code"
    if hmrc and "corporation tax" in t and not reminder and \
            re.search(r"unique\s*tax\s*payer\s*reference|\butr\b|ct41g", t) and \
            re.search(r"ct41g|new compan|(company'?s?|your)\s+(corporation tax\s+)?(unique\s*tax\s*payer\s*reference|utr)\s*(is|:|-)|"
                      r"(unique\s*tax\s*payer\s*reference|utr)\s*(is|:|-)?\s*\d{5}\s?\d{5}", t):
        return "hmrc_utr"
    return ""


# ----------------------------------------------------------------------------- index (cached letter list)

def _batch_entries(bdir: Path) -> list[dict]:
    b = _read_json(bdir / "batch.json", None)
    if not b:
        return []
    pages = {}
    pj = bdir / "pages.json"
    if pj.exists():
        for pg in _read_json(pj, []):
            try:
                pages[int(pg.get("page"))] = pg.get("text") or ""
            except Exception:
                pass
    bundle = {}
    for e in b.get("emails", []):
        when = e.get("sent_at") or e.get("manual_sent_at")
        if when:
            bundle[ckey(e.get("company", ""))] = when
    out = []
    for L in b.get("letters", []):
        c = L.get("client") or {}
        name = c.get("company_name", "") if c else ""
        if L.get("unit_mismatch"):
            state = "review"
        elif not c:
            state = "held"
        else:
            state = "verified" if L.get("match_state", "verified") == "verified" else "review"
        text = " ".join(pages.get(p, "") for p in (L.get("pages") or []))
        tk = text_kind(" ".join([text, L.get("sender") or "", L.get("summary") or ""]))
        out.append({
            "bid": b.get("id") or bdir.name, "lid": L.get("letter_id"), "file": L.get("file", ""),
            "created": b.get("created", ""), "npages": len(L.get("pages") or []),
            "sender": L.get("sender", ""), "letter_type": L.get("letter_type", ""), "summary": L.get("summary", ""),
            "urgency": L.get("urgency", ""), "recipient": L.get("recipient_company", ""), "address": L.get("address", ""),
            "unit": L.get("unit", ""), "company": name, "key": ckey(name) if name else "",
            "suggested": L.get("suggested_client", ""), "state": state,
            "emailed_at": L.get("emailed_at"), "downloaded_at": L.get("downloaded_at"),
            "bundle_sent_at": bundle.get(ckey(name)) if name and state == "verified" else None,
            "siu_ok": L.get("siu_ok", True), "ai_kind": L.get("doc_kind") or "", "text_kind": tk,
        })
    return out


def get_index(rebuild: bool = True) -> dict:
    """{bid: {"mtime": m, "letters": [...]}} – only batches whose batch.json changed are re-read."""
    with _lock:
        idx = _cache["index"]
        if idx is None:
            idx = _read_json(INDEX_FILE, {})
            if idx.get("_v") != INDEX_VERSION:
                idx = {"_v": INDEX_VERSION, "batches": {}}
        if not rebuild:
            return idx
        changed = False
        seen = set()
        if BATCHES.exists():
            for bdir in BATCHES.iterdir():
                bj = bdir / "batch.json"
                if not bdir.is_dir() or not bj.exists():
                    continue
                seen.add(bdir.name)
                m = bj.stat().st_mtime
                cur = idx["batches"].get(bdir.name)
                if not cur or cur.get("mtime") != m:
                    idx["batches"][bdir.name] = {"mtime": m, "letters": _batch_entries(bdir)}
                    changed = True
        for gone in [k for k in idx["batches"] if k not in seen]:
            idx["batches"].pop(gone)
            changed = True
        if changed:
            try:
                _write_json(INDEX_FILE, idx)
            except Exception as ex:
                print(f"[companyfile] index not saved: {ex}", flush=True)
        _cache["index"] = idx
        return idx


def all_letters(idx: dict) -> list[dict]:
    out = []
    for v in idx.get("batches", {}).values():
        out.extend(v.get("letters", []))
    out.sort(key=lambda e: (e.get("created") or "", e.get("lid") or ""), reverse=True)
    return out


def effective_kind(e: dict, ovr: dict) -> tuple[str, str]:
    """(kind, how) – how: staff | ai | possible | ''."""
    o = ovr["letters"].get(f"{e['bid']}/{e['lid']}")
    if o:
        k = o.get("kind")
        return ("", "staff") if k == "none" else (k, "staff")
    if e.get("ai_kind") in DOC_KEYS:
        return e["ai_kind"], "ai"
    if e.get("text_kind") in DOC_KEYS:
        return e["text_kind"], "possible"
    return "", ""


def sent_info(e: dict) -> tuple[str, str]:
    if e.get("emailed_at"):
        return e["emailed_at"], "emailed"
    if e.get("bundle_sent_at"):
        return e["bundle_sent_at"], "sent with batch email"
    if e.get("downloaded_at"):
        return e["downloaded_at"], "downloaded for portal"
    return "", ""


def company_status(key: str, letters: list[dict], ovr: dict) -> dict:
    """Checklist for one company. letters = that company's entries (any state)."""
    res = {}
    manual = ovr["manual"].get(key, {})
    for d in DOC_KEYS:
        got, poss, waiting = [], [], []
        for e in letters:
            k, how = effective_kind(e, ovr)
            if k != d:
                continue
            if e["state"] != "verified":
                waiting.append(e)
            elif how == "possible":
                poss.append(e)
            else:
                got.append({**e, "how": how})
        st = {"doc": d, "letters": sorted(got, key=lambda x: x.get("created") or ""), "possible": poss,
              "waiting": waiting, "manual": manual.get(d)}
        if got:
            first = st["letters"][0]
            st["state"] = "received"
            st["received"] = first.get("created")
            sent = [sent_info(x) for x in got if sent_info(x)[0]]
            st["sent"] = sorted(sent)[0] if sent else None
        elif st["manual"]:
            st["state"] = "manual"
            st["received"] = st["manual"].get("date")
            st["sent"] = None
        elif poss:
            st["state"] = "possible"
        else:
            st["state"] = "missing"
        res[d] = st
    return res


def build_overview():
    idx = get_index()
    ovr = load_overrides()
    letters = all_letters(idx)
    by_key: dict[str, list] = {}
    for e in letters:
        if e.get("key"):
            by_key.setdefault(e["key"], []).append(e)
    clients = [c for c in (_hooks["read_clients"]() or []) if (c.get("company_name") or "").strip()]
    rows, seen = [], set()
    for c in clients:
        k = ckey(c["company_name"])
        if k in seen:
            continue
        seen.add(k)
        rows.append((c, by_key.get(k, [])))
    for k, ls in by_key.items():   # companies on letters but no longer in the client database
        if k not in seen:
            rows.append(({"company_name": ls[0]["company"], "_gone": True}, ls))
    out = []
    alerts = 0
    for c, ls in rows:
        k = ckey(c["company_name"])
        st = company_status(k, ls, ovr)
        ver = [e for e in ls if e["state"] == "verified"]
        nposs = sum(1 for d in DOC_KEYS if st[d]["state"] == "possible")
        alerts += 1 if nposs else 0
        out.append({"c": c, "key": k, "st": st, "n": len(ver), "nwait": len(ls) - len(ver),
                    "last": max((e.get("created") or "" for e in ver), default=""),
                    "have": sum(1 for d in DOC_KEYS if st[d]["state"] in ("received", "manual")), "nposs": nposs})
    _cache["alerts"] = alerts
    return out


def _start_days(c: dict):
    try:
        from mailsort import parse_date
        d = parse_date((c.get("start_date") or "")[:10])
        return (dt.date.today() - d).days if d else None
    except Exception:
        return None


def _staff() -> str:
    return (request.form.get("staff") or "").strip()[:60] or os.environ.get("MAILSORT_USER", "staff")


# ----------------------------------------------------------------------------- routes

@bp.get("/companies")
def companies():
    rows = build_overview()
    for r in rows:
        r["days"] = _start_days(r["c"])
    rows.sort(key=lambda r: (-(r["nposs"] > 0), r["days"] if r["days"] is not None else 10 ** 6, r["c"]["company_name"].lower()))
    tot = len(rows)
    complete = sum(1 for r in rows if r["have"] == 3)
    poss = sum(1 for r in rows if r["nposs"])
    return render_template_string(LIST, rows=rows, docs=DOCS, tot=tot, complete=complete, poss=poss)


@bp.get("/company")
def company():
    name = (request.args.get("name") or "").strip()
    if not name:
        return redirect("/companies")
    k = ckey(name)
    idx = get_index()
    ovr = load_overrides()
    ls = [e for e in all_letters(idx) if e.get("key") == k]
    c = next((x for x in _hooks["read_clients"]() if ckey(x.get("company_name", "")) == k), None)
    if not c and not ls:
        flash(f"No company called '{name}' in the client database or in any batch.")
        return redirect("/companies")
    c = c or {"company_name": ls[0]["company"], "_gone": True}
    st = company_status(k, ls, ovr)
    for e in ls:
        e["kind"], e["how"] = effective_kind(e, ovr)
        e["sent"] = sent_info(e)
    ver = [e for e in ls if e["state"] == "verified"]
    wait = [e for e in ls if e["state"] != "verified"]
    return render_template_string(COMPANY, c=c, st=st, docs=DOCS, ver=ver, wait=wait, label=DOC_LABEL,
                                  today=dt.date.today().isoformat(), days=_start_days(c))


@bp.get("/company/zip")
def company_zip():
    name = (request.args.get("name") or "").strip()
    k = ckey(name)
    ls = [e for e in all_letters(get_index()) if e.get("key") == k and e["state"] == "verified"]
    if not ls:
        flash("No verified letters for this company yet.")
        return redirect(f"/company?name={name}")
    ovr = load_overrides()
    buf = io.BytesIO()
    safe = re.sub(r"[^A-Za-z0-9 _.-]", "", ls[0]["company"]).strip() or "company"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for e in ls:
            p = (BATCHES / e["bid"] / e["file"]).resolve()
            if not str(p).startswith(str(BATCHES.resolve())) or not p.exists():
                continue
            kind, how = effective_kind(e, ovr)
            day = (e.get("created") or "")[:10]
            folder = "Key documents/" if kind and how != "possible" else ""
            z.write(p, f"{safe}/{folder}{day}_{e['lid']}_{p.name}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe} - company file {dt.date.today().strftime('%d-%m-%Y')}.zip")


@bp.post("/company/letterkind")
def letterkind():
    bid, lid = request.form.get("bid", ""), request.form.get("lid", "")
    kind = request.form.get("kind", "")
    name = request.form.get("name", "")
    if not re.fullmatch(r"[\w.-]+", bid or "") or not re.fullmatch(r"[\w.-]+", lid or ""):
        abort(400)
    ovr = load_overrides()
    key = f"{bid}/{lid}"
    if kind == "auto":
        ovr["letters"].pop(key, None)
        msg = f"{lid}: back to automatic detection."
    elif kind == "none" or kind in DOC_KEYS:
        ovr["letters"][key] = {"kind": kind, "by": _staff(), "when": _now()}
        msg = f"{lid}: marked as {'not a key document' if kind == 'none' else DOC_LABEL[kind]}."
    else:
        abort(400)
    _write_json(OVR_FILE, ovr)
    flash(msg)
    return redirect(f"/company?name={_q(name)}")


@bp.post("/company/manual")
def manual():
    name = (request.form.get("name") or "").strip()
    doc = request.form.get("doc", "")
    if doc not in DOC_KEYS or not name:
        abort(400)
    k = ckey(name)
    ovr = load_overrides()
    if request.form.get("action") == "clear":
        ovr["manual"].get(k, {}).pop(doc, None)
        flash(f"{DOC_LABEL[doc]}: manual tick removed.")
    else:
        ovr["manual"].setdefault(k, {})[doc] = {"date": request.form.get("date") or dt.date.today().isoformat(),
                                                "note": (request.form.get("note") or "").strip()[:300],
                                                "by": _staff(), "when": _now()}
        flash(f"{DOC_LABEL[doc]} ticked as received outside the mail room.")
    _write_json(OVR_FILE, ovr)
    return redirect(f"/company?name={_q(name)}")


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s or "", safe="")


def register(app, load_batch=None, read_clients=None):
    OUT.mkdir(parents=True, exist_ok=True)
    _hooks["read_clients"] = read_clients or (lambda: [])
    app.register_blueprint(bp)

    @app.context_processor
    def _cf_alerts():
        return {"cf_alerts": _cache.get("alerts", 0)}

    def _warm():
        try:
            build_overview()
        except Exception as ex:
            print(f"[companyfile] first index build failed: {ex}", flush=True)
    threading.Thread(target=_warm, daemon=True).start()


# ----------------------------------------------------------------------------- pages

STYLE = """<style>
.dk{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:99px;font-size:12px;font-weight:600;white-space:nowrap}
.dk.ok{background:#dcfce7;color:#166534}.dk.man{background:#e0f2fe;color:#075985}.dk.pos{background:#fef3c7;color:#92400e}
.dk.no{background:#f1f5f9;color:#64748b}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.chip{border:1px solid var(--line);background:#fff;border-radius:99px;padding:5px 12px;font-size:13px;cursor:pointer;font-weight:600}
.chip.on{background:#111;color:#fff;border-color:#111}
.docs{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.doc{border:1px solid var(--line);border-radius:10px;padding:14px 16px}.doc h3{margin:0 0 6px;font-size:16px}
.doc.received,.doc.manual{border-color:#86efac;background:#f0fdf4}.doc.possible{border-color:#fcd34d;background:#fffbeb}
.doc.missing{background:#f8fafc}
.big{font-size:22px;margin-right:6px}.tiny{font-size:12px}
.mini input,.mini select{padding:5px 6px;font-size:12px;border:1px solid #cbd5e1;border-radius:6px;width:auto}
.kv{display:grid;grid-template-columns:140px 1fr;gap:4px 12px;font-size:14px}.kv b{color:#555;font-weight:600}
</style>"""

LIST = """{% extends "base" %}{% block body %}""" + STYLE + """
<div class="card"><h1>Company files</h1>
<p class="muted">One file per company, built from every sorted letter. The three key documents tick themselves off when
they arrive. Only letters with a <b>verified</b> client match count. Amber = the letter looks like one of them – open the
company and press Confirm.</p>
<div style="display:flex;gap:10px;flex-wrap:wrap;margin:8px 0">
<span class="pill">{{tot}} companies</span><span class="pill active">{{complete}} have all 3</span>
{% if poss %}<span class="pill" style="background:#fef3c7;color:#92400e">{{poss}} to check</span>{% endif %}</div>
<div class="chips" id="chips">
<button class="chip on" data-f="all">All</button><button class="chip" data-f="missing">Missing any</button>
<button class="chip" data-f="check">To check</button><button class="chip" data-f="complete">All 3 received</button>
<button class="chip" data-f="new">Started in last 90 days</button>
{% for k,l,s,i in docs %}<button class="chip" data-f="no-{{k}}">No {{l}}</button>{% endfor %}</div>
<input type="text" id="q" placeholder="Search company name, unit or email…" style="max-width:420px">
<p class="muted tiny" id="cnt"></p>
<table id="t"><tr><th>Company</th><th>Unit</th><th>Start</th>{% for k,l,s,i in docs %}<th>{{i}} {{l}}</th>{% endfor %}<th>Letters</th><th>Last letter</th><th></th></tr>
{% for r in rows %}{% set c = r.c %}
<tr data-s="{{(c.company_name ~ ' ' ~ (c.siu or '') ~ ' ' ~ (c.email or ''))|lower}}" data-f="{% if r.have < 3 %}missing {% endif %}{% if r.have == 3 %}complete {% endif %}{% if r.nposs %}check {% endif %}{% if r.days is not none and r.days <= 90 %}new {% endif %}{% for k,l,s,i in docs %}{% if r.st[k].state not in ('received','manual') %}no-{{k}} {% endif %}{% endfor %}">
<td><a href="/company?name={{c.company_name|urlencode}}" style="color:#111;font-weight:600">{{c.company_name}}</a>
{% if c._gone %}<br><span class="pill high tiny">not in client database</span>{% elif c.status and c.status != 'active' %}<br><span class="pill high tiny">{{c.status}}</span>{% endif %}</td>
<td>{{c.siu or "—"}}</td><td class="tiny">{{c.start_date|ukdate if c.start_date else "—"}}</td>
{% for k,l,s,i in docs %}{% set d = r.st[k] %}<td>
{% if d.state == 'received' %}<span class="dk ok">✓ {{(d.received or "")[:10]|ukdate}}</span>
{% elif d.state == 'manual' %}<span class="dk man" title="ticked by staff">✓ {{(d.received or "")[:10]|ukdate}}</span>
{% elif d.state == 'possible' %}<span class="dk pos">? check</span>
{% else %}<span class="dk no">—</span>{% endif %}</td>{% endfor %}
<td>{{r.n}}{% if r.nwait %} <span class="muted tiny">+{{r.nwait}} awaiting check</span>{% endif %}</td>
<td class="tiny">{{r.last[:10]|ukdate if r.last else "—"}}</td>
<td><a class="btn small secondary" href="/company?name={{c.company_name|urlencode}}">Open file</a></td></tr>{% endfor %}</table></div>
<script>
const rows=[...document.querySelectorAll('#t tr[data-s]')];let f='all';
function run(){const q=document.getElementById('q').value.trim().toLowerCase();let n=0;
rows.forEach(r=>{const ok=(f==='all'||(' '+r.dataset.f).includes(' '+f+' '))&&(!q||r.dataset.s.includes(q));r.style.display=ok?'':'none';if(ok)n++});
document.getElementById('cnt').textContent=n+' shown';}
document.querySelectorAll('#chips .chip').forEach(b=>b.onclick=()=>{document.querySelectorAll('#chips .chip').forEach(x=>x.classList.remove('on'));b.classList.add('on');f=b.dataset.f;run()});
document.getElementById('q').oninput=run;run();
</script>{% endblock %}"""

COMPANY = """{% extends "base" %}{% block body %}""" + STYLE + """
<div class="card"><div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start">
<div><h1 style="margin-bottom:6px">{{c.company_name}}</h1>
{% if c._gone %}<span class="pill high">not in the client database any more</span>{% endif %}</div>
<div class="btnrow"><a class="btn secondary small" href="/companies">← All companies</a>
{% if ver %}<a class="btn small" href="/company/zip?name={{c.company_name|urlencode}}">⬇ Download company folder (ZIP)</a>{% endif %}</div></div>
<div class="kv" style="margin-top:12px">
<b>Unit (SIU)</b><span>{{c.siu or "—"}}</span><b>Address on file</b><span>{{c.address or "—"}}</span>
<b>Email</b><span>{{c.email or "—"}}</span><b>Contact</b><span>{{c.contact_name or "—"}}</span>
<b>Status / package</b><span>{{c.status or "—"}}{% if c.package %} · {{c.package}}{% endif %}</span>
<b>Start date</b><span>{{c.start_date|ukdate if c.start_date else "—"}}{% if days is not none %} <span class="muted">({{days}} days ago)</span>{% endif %}</span>
{% if c.reseller %}<b>Reseller</b><span>{{c.reseller}}</span>{% endif %}</div></div>

<div class="card"><h2>Key documents</h2><div class="docs">
{% for k,l,s,i in docs %}{% set d = st[k] %}<div class="doc {{d.state}}">
<h3><span class="big">{{i}}</span>{{l}}</h3><div class="muted tiny" style="margin-bottom:8px">from {{s}}</div>
{% if d.state == 'received' %}<div><span class="dk ok">✓ Received {{(d.received or "")[:10]|ukdate}}</span></div>
<div class="tiny" style="margin-top:6px">{% if d.sent %}Sent to client: <b>{{d.sent[0]|ukdate}}</b> ({{d.sent[1]}}){% else %}<span style="color:#b45309;font-weight:600">Not sent to the client yet</span>{% endif %}</div>
{% for e in d.letters %}<div class="tiny" style="margin-top:6px">{{e.lid}} · batch {{e.created|ukdate}} · {{'AI' if e.how=='ai' else 'staff'}}
· <a href="/batch/{{e.bid}}/file/{{e.file}}" target="_blank">open</a> · <a href="/batch/{{e.bid}}">batch</a></div>{% endfor %}
{% elif d.state == 'manual' %}<div><span class="dk man">✓ Received {{d.received|ukdate}} – outside the mail room</span></div>
<div class="tiny" style="margin-top:6px">by {{d.manual.by}} on {{d.manual.when|ukdate}}{% if d.manual.note %} – {{d.manual.note}}{% endif %}</div>
<form method="post" action="/company/manual" style="margin-top:8px" onsubmit="return confirm('Remove this manual tick?')">
<input type="hidden" name="name" value="{{c.company_name}}"><input type="hidden" name="doc" value="{{k}}"><input type="hidden" name="action" value="clear">
<button class="btn small secondary">Undo tick</button></form>
{% elif d.state == 'possible' %}<div><span class="dk pos">? Possible – please check</span></div>
{% for e in d.possible %}<div class="tiny" style="margin-top:8px;border-top:1px dashed #fcd34d;padding-top:6px">
<b>{{e.lid}}</b> · {{e.created|ukdate}} · {{e.sender}}<br>{{e.summary}}<br>
<a href="/batch/{{e.bid}}/file/{{e.file}}" target="_blank">Open the letter</a>
<form method="post" action="/company/letterkind" style="display:inline-flex;gap:4px;margin-left:6px" class="mini">
<input type="hidden" name="bid" value="{{e.bid}}"><input type="hidden" name="lid" value="{{e.lid}}"><input type="hidden" name="name" value="{{c.company_name}}">
<input type="text" name="staff" placeholder="your name" style="width:90px">
<button class="btn small" name="kind" value="{{k}}">✓ Confirm</button><button class="btn small secondary" name="kind" value="none">Not this</button></form></div>{% endfor %}
{% else %}<div><span class="dk no">Not received yet</span></div>
{% if days is not none %}<div class="tiny muted" style="margin-top:4px">{{days}} days since the client started</div>{% endif %}
<details style="margin-top:8px"><summary class="tiny" style="cursor:pointer">Received outside the mail room? Tick it here</summary>
<form method="post" action="/company/manual" class="mini" style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px">
<input type="hidden" name="name" value="{{c.company_name}}"><input type="hidden" name="doc" value="{{k}}">
<input type="date" name="date" value="{{today}}"><input type="text" name="note" placeholder="note (e.g. client had it)" style="width:170px">
<input type="text" name="staff" placeholder="your name" style="width:90px"><button class="btn small">Tick received</button></form></details>{% endif %}
{% if d.waiting %}<div class="tiny" style="margin-top:8px;color:#92400e">⚠ {{d.waiting|length}} matching letter(s) still waiting for the client match check in their batch:
{% for e in d.waiting %}<a href="/batch/{{e.bid}}">{{e.lid}} ({{e.created|ukdate}})</a> {% endfor %}</div>{% endif %}
</div>{% endfor %}</div></div>

<div class="card"><h2>All letters received ({{ver|length}})</h2>
{% if not ver %}<p class="muted">No verified letters for this company yet.</p>{% else %}
<table><tr><th>Received</th><th>Letter</th><th>Sender</th><th>Summary</th><th>Key document</th><th>Sent to client</th><th></th></tr>
{% for e in ver %}<tr>
<td class="tiny">{{e.created|ukdate}}</td><td><b>{{e.lid}}</b><br><span class="muted tiny">{{e.npages}} page{{'s' if e.npages != 1 else ''}}</span></td>
<td>{{e.sender or "—"}}<br><span class="muted tiny">{{e.letter_type|replace("_"," ")}}{% if e.urgency == 'high' %} · <span style="color:#b91c1c">urgent</span>{% endif %}</span></td>
<td style="max-width:300px">{{e.summary}}{% if not e.siu_ok %}<br><span class="pill high tiny">SIU missing on letter</span>{% endif %}</td>
<td>{% if e.kind and e.how != 'possible' %}<span class="dk ok">{{label[e.kind]}}</span><br><span class="muted tiny">{{'AI' if e.how=='ai' else 'staff'}}</span>
{% elif e.kind %}<span class="dk pos">{{label[e.kind]}}?</span>{% else %}<span class="muted">—</span>{% endif %}
<form method="post" action="/company/letterkind" class="mini" style="margin-top:4px">
<input type="hidden" name="bid" value="{{e.bid}}"><input type="hidden" name="lid" value="{{e.lid}}"><input type="hidden" name="name" value="{{c.company_name}}">
<select name="kind" onchange="this.form.submit()"><option value="">change…</option>
{% for k,l,s,i in docs %}<option value="{{k}}">{{l}}</option>{% endfor %}<option value="none">not a key document</option><option value="auto">automatic</option></select></form></td>
<td class="tiny">{% if e.sent[0] %}<span class="dk ok">{{e.sent[0]|ukdate}}</span><br><span class="muted">{{e.sent[1]}}</span>{% else %}<span class="dk no">not yet</span>{% endif %}</td>
<td><div class="btnrow"><a class="btn small secondary" href="/batch/{{e.bid}}/file/{{e.file}}" target="_blank">Open</a><a class="btn small secondary" href="/batch/{{e.bid}}">Batch</a></div></td></tr>{% endfor %}</table>{% endif %}</div>

{% if wait %}<div class="card"><h2>Waiting for a match check ({{wait|length}})</h2>
<p class="muted">These letters were matched to this company but the match is not verified yet. They do not count towards the file
until a member of staff confirms them in the batch.</p>
<table><tr><th>Batch date</th><th>Letter</th><th>Printed name / address</th><th>Sender</th><th></th></tr>
{% for e in wait %}<tr><td class="tiny">{{e.created|ukdate}}</td><td><b>{{e.lid}}</b></td><td class="tiny">{{e.recipient}}<br>{{e.address}}</td>
<td>{{e.sender}}</td><td><a class="btn small secondary" href="/batch/{{e.bid}}">Check in batch</a></td></tr>{% endfor %}</table></div>{% endif %}
{% endblock %}"""
