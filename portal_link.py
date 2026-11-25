"""
Link to the Startitup client portal (startitup-portal on Railway).

Two Railway variables, the same key on both sides:
  PORTAL_URL      e.g. https://startitup-portal-production.up.railway.app
  PORTAL_API_KEY  a long random value; the portal has it as MAILSORT_API_KEY

1. EIN – the portal sends us the client's signed SS-4 (the client e-signs it in the portal):
     POST /api/ein/jobs        (Bearer PORTAL_API_KEY) JSON: ref (portal order id), legal_name, client_email,
                               fax_to, file_name, pdf_b64, callback_url
     GET  /api/ein/jobs/<id>   status, EIN, faxes
     GET  /api/ein/jobs/<id>/cp575
     GET  /api/ein/settings    our applicant fax/phone (printed on the portal's SS-4) and whether Telnyx is ready
   We make it an ordinary EIN application (it shows on the EIN Filing tab), fax it with our cover sheet, re-fax
   weekly, read the EIN from the IRS fax – all as for our own applications – and tell the portal:
     FAX_COMPLETED  when the first fax is delivered    -> portal ticks "EIN application sent"
     INCOMING_FAX   with the EIN, the order and the letter -> portal ticks "EIN received" and files the letter
   The portal's callback_url is its fax webhook (/webhooks/fax/<secret>).

2. Letters – when a sorted letter is emailed to its verified client, it is also uploaded to that client's
   portal Letters folder (POST <PORTAL_URL>/api/letters). Clients without an address service in the portal are
   skipped (the portal answers 404); nothing else changes in the mail room.
"""
from __future__ import annotations

import base64, datetime as dt, hmac, json, os, threading, time, urllib.error, urllib.request, uuid
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_file

bp = Blueprint("portal_link", __name__)
_STARTED = {"sync": False}
_LOCK = threading.Lock()


def portal_url() -> str:
    return (os.environ.get("PORTAL_URL") or "").strip().rstrip("/")


def api_key() -> str:
    return (os.environ.get("PORTAL_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(api_key())


def _authorised() -> bool:
    key = api_key()
    got = request.headers.get("Authorization", "")
    return bool(key) and got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip(), key)


def _ein():
    import einforms
    return einforms


# ----------------------------------------------------------------------------- API for the portal

@bp.before_request
def _check_key():
    if not _authorised():
        return jsonify(error="Unauthorised"), 401
    return None


@bp.get("/api/ein/settings")
def api_settings():
    e = _ein()
    s = e.load_settings()
    return jsonify(applicant_fax=e.e164(s.get("applicant_fax")), applicant_phone=e.e164(s.get("applicant_phone")),
                   fax_ready=e.fax_ready())


def _portal_jobs():
    e = _ein()
    out = []
    if e.OUT.is_dir():
        for d in e.OUT.iterdir():
            if (d / "meta.json").is_file():
                try:
                    m = e.load_meta(d)
                except Exception:
                    continue
                if m.get("portal"):
                    out.append((d, m))
    return out


@bp.post("/api/ein/jobs")
def api_new_job():
    e = _ein()
    j = request.get_json(silent=True) or {}
    legal_name = str(j.get("legal_name") or "").strip()[:120]
    try:
        pdf = base64.b64decode(str(j.get("pdf_b64") or ""), validate=True)
    except Exception:
        pdf = b""
    if not legal_name or pdf[:5] != b"%PDF-":
        return jsonify(error="legal_name and a PDF (pdf_b64) are needed"), 400
    fax_to = e.e164(str(j.get("fax_to") or "")) or e.e164(e.IRS_FAX["intl"][0])
    ref = str(j.get("ref") or "")[:64]
    callback = str(j.get("callback_url") or "")[:500]
    if callback and not callback.startswith("https://"):
        return jsonify(error="callback_url must be https"), 400
    with _LOCK:
        # The same order sent twice (a retry): give back the application we already have.
        if ref:
            for d, m in _portal_jobs():
                if m["portal"].get("ref") == ref and m.get("status") not in ("cancelled", "rejected"):
                    if callback:
                        m["portal"]["callback"] = callback
                        e.save_meta(d, m)
                    return jsonify(id=m["id"], status=m.get("status"), existing=True)
        s = e.load_settings()
        job = uuid.uuid4().hex[:12]
        d = e.OUT / job
        (d / "docs").mkdir(parents=True, exist_ok=True)
        (d / "out").mkdir(exist_ok=True)
        now = dt.datetime.now().isoformat(timespec="seconds")
        a = e.blank_app(s)
        a.update(legal_name=legal_name, fax_to=fax_to, sig_mode="client", sig_ok="1",
                 client_email=str(j.get("client_email") or "")[:200],
                 notes="Prepared and e-signed by the client in the Startitup portal.")
        tag = e._safe(legal_name)
        files = [{"file": f"SS-4_{tag}_signed.pdf", "label": "Form SS-4 – signed by the client in the portal"},
                 {"file": f"Fax_cover_{tag}.pdf", "label": "Fax cover sheet"},
                 {"file": f"Fax_pack_{tag}.pdf", "label": "Fax pack (cover + SS-4) – send this to the IRS"}]
        cover = e.fax_cover(a, s)
        (d / "out" / files[0]["file"]).write_bytes(pdf)
        (d / "out" / files[1]["file"]).write_bytes(cover)
        import pymupdf
        pack = pymupdf.open(stream=cover, filetype="pdf")
        pack.insert_pdf(pymupdf.open(stream=pdf, filetype="pdf"))
        pack.save(d / "out" / files[2]["file"], garbage=3, deflate=True)
        m = {"id": job, "created": now, "status": "prepared", "status_at": now, "a": a, "members": [], "docs": [],
             "files": files, "portal": {"ref": ref, "callback": callback, "sent": {}},
             "history": [{"t": now, "status": "prepared", "note": "SS-4 received from the portal (signed by the client)"}]}
        e.save_meta(d, m)
    _fax_first(d)
    m = e.load_meta(d)
    return jsonify(id=job, status=m.get("status"))


def _fax_first(d: Path):
    """Faxes a portal application now, if Telnyx is set up. A failure stays on the EIN tab and is retried."""
    e = _ein()
    if not e.fax_ready():
        return
    m = e.load_meta(d)
    try:
        e.send_job_fax(d, m, "first")
    except e.FaxError as ex:
        print(f"[portal] fax {m['id']}: {ex}", flush=True)
    e.save_meta(d, m)


@bp.get("/api/ein/jobs/<job>")
def api_job(job):
    e = _ein()
    d = e._job_dir(job)
    m = e.load_meta(d)
    return jsonify(id=m["id"], status=m.get("status"), ein=m.get("ein"), ref=(m.get("portal") or {}).get("ref"),
                   faxes=[{k: x.get(k) for k in ("n", "kind", "t", "status", "error", "pages")} for x in m.get("faxes", [])])


@bp.get("/api/ein/jobs/<job>/cp575")
def api_cp575(job):
    e = _ein()
    d = e._job_dir(job)
    m = e.load_meta(d)
    if not m.get("cp575") or not (d / "docs" / m["cp575"]).is_file():
        abort(404)
    return send_file(d / "docs" / m["cp575"], mimetype="application/pdf")


# ----------------------------------------------------------------------------- telling the portal

def _post_json(url: str, body: dict, timeout: int = 30) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as ex:
        return ex.code


def sync_once():
    """Sends the portal what changed on its applications. Safe to run often: each event is sent once."""
    e = _ein()
    for d, m in _portal_jobs():
        p = m["portal"]
        sent = p.setdefault("sent", {})
        cb = p.get("callback")
        changed = False
        faxes = m.get("faxes", [])
        # A first fax that failed: try again after an hour, up to 3 times (the team sees it on the EIN tab).
        if faxes and all(x.get("status") == "failed" for x in faxes) and len(faxes) < 3 and not m.get("ein") \
                and e._mins_since(faxes[0]["t"]) >= 60 and e.fax_ready():
            try:
                e.send_job_fax(d, m, "first")
            except e.FaxError as ex:
                print(f"[portal] re-fax {m['id']}: {ex}", flush=True)
            changed = True
        if not cb:
            if changed:
                e.save_meta(d, m)
            continue
        delivered = next((x for x in reversed(faxes) if x.get("status") == "delivered"), None)
        if delivered and not sent.get("delivered"):
            st = _post_json(cb, {"event": "FAX_COMPLETED",
                                 "fax": {"id": f"mailsort-{m['id']}", "direction": "OUTBOUND", "status": "COMPLETED",
                                         "numberOfPages": delivered.get("pages") or 2}})
            if 200 <= st < 300:
                sent["delivered"] = dt.datetime.now().isoformat(timespec="seconds")
                changed = True
        if m.get("ein") and not sent.get("ein"):
            letter = d / "docs" / m["cp575"] if m.get("cp575") else None
            body = {"event": "INCOMING_FAX", "fax": {"id": f"mailsort-{m['id']}-ein", "direction": "INBOUND", "from": "IRS"},
                    "ein": m["ein"], "case_id": p.get("ref") or ""}
            if letter and letter.is_file():
                body["file"] = base64.b64encode(letter.read_bytes()).decode()
            st = _post_json(cb, body, timeout=60)
            if 200 <= st < 300:
                sent["ein"] = dt.datetime.now().isoformat(timespec="seconds")
                m.setdefault("history", []).insert(0, {"t": sent["ein"], "status": m.get("status"),
                                                       "note": "EIN sent to the client portal"})
                changed = True
        if changed:
            e.save_meta(d, m)


def _sync_loop():
    time.sleep(60)
    while True:
        try:
            sync_once()
        except Exception as ex:
            print(f"[portal] sync: {ex}", flush=True)
        time.sleep(5 * 60)


# ----------------------------------------------------------------------------- letters into the portal

def push_letter(pdf_path: Path, client_email: str, sender: str = "", received: str = "") -> str:
    """Uploads one sorted letter to the client's portal Letters folder. Returns 'ok', 'off', 'no address service' or an error."""
    if not (portal_url() and api_key()):
        return "off"
    if not client_email or not pdf_path.is_file():
        return "no file or email"
    bnd = "----ms" + uuid.uuid4().hex
    parts = []
    for k, v in (("client_email", client_email), ("sender", sender or ""), ("received", received or "")):
        if v:
            parts.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{pdf_path.name}\"\r\n"
                 f"Content-Type: application/pdf\r\n\r\n".encode() + pdf_path.read_bytes() + b"\r\n")
    parts.append(f"--{bnd}--\r\n".encode())
    req = urllib.request.Request(portal_url() + "/api/letters", data=b"".join(parts), method="POST",
                                 headers={"Authorization": f"Bearer {api_key()}",
                                          "Content-Type": f"multipart/form-data; boundary={bnd}"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return "ok"
    except urllib.error.HTTPError as ex:
        return "no address service" if ex.code == 404 else f"portal error {ex.code}"
    except Exception as ex:
        return f"portal not reached: {ex}"


def push_letters(bdir: Path, letters: list[dict], client_email: str):
    """After an email went to the client: put the same letters in their portal. Records the result on each letter."""
    for L in letters:
        if L.get("portal_at") or not L.get("file"):
            continue
        r = push_letter(bdir / L["file"], client_email, L.get("sender") or "", dt.date.today().isoformat())
        if r == "off":
            return
        L["portal"] = r
        if r == "ok":
            L["portal_at"] = dt.datetime.now().isoformat(timespec="seconds")


def register(app):
    app.register_blueprint(bp)
    if enabled() and not _STARTED["sync"] and os.environ.get("EIN_NO_POLL") != "1":
        _STARTED["sync"] = True
        threading.Thread(target=_sync_loop, daemon=True).start()
