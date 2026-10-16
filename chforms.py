"""
Company forms tab (v30a) – Companies House change forms + SIU proof-of-address letters.

Self-contained add-on: app.py only imports this file and calls register(app).
Nothing in the mail-sorting system is touched.

  /forms                    -> enter company number, look it up, pick documents, generate
  /forms/lookup?number=...  -> JSON: company name, active directors, active individual PSCs (Companies House API)
  /forms/generate  (POST)   -> fills the templates, stores them under DATA/chforms/<job>/
  /forms/job/<job>          -> preview + download one by one / all as ZIP

Templates live in ./chforms_templates (used exactly as supplied – only the client fields are filled):
  AD01.pdf, CH01.pdf, PSC04.pdf, Letter_Registered_Office.docx, Letter_Director.docx, Letter_PSC.docx

Railway variable needed for the automatic lookup:  CH_API_KEY  (free key from developer.company-information.service.gov.uk)
"""
from __future__ import annotations

import base64, datetime as dt, io, json, os, re, threading, urllib.error, urllib.parse, urllib.request, uuid, zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from flask import Blueprint, abort, flash, jsonify, redirect, render_template_string, request, send_file, session

ROOT = Path(__file__).parent
TPL_DIR = ROOT / "chforms_templates"
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "chforms"
LOG_FILE = OUT / "log.json"
_LOCK = threading.Lock()

CH_API = "https://api.company-information.service.gov.uk"

# Our SIU Offices address – the default "new address". Staff can still edit every line on screen.
SIU = {"building_name": "SIU Offices", "premise": "4-6", "street": "Greatorex Street",
       "town": "London", "county": "", "postcode": "E1 5NF", "country": "United Kingdom"}

DOCS = {   # key: (label shown to staff, template file, group)
    "ad01": ("AD01 – Change of registered office address", "AD01.pdf", "Registered office"),
    "ro_letter": ("Proof of Registered Office Address letter", "Letter_Registered_Office.docx", "Registered office"),
    "ch01": ("CH01 – Change of director's details", "CH01.pdf", "Directors"),
    "dir_letter": ("Proof of Director's Correspondence Address letter", "Letter_Director.docx", "Directors"),
    "psc04": ("PSC04 – Change of PSC details", "PSC04.pdf", "PSCs"),
    "psc_letter": ("Proof of PSC Correspondence Address letter", "Letter_PSC.docx", "PSCs"),
}

bp = Blueprint("chforms", __name__)


# ----------------------------------------------------------------------------- Companies House lookup

def _ch_get(path: str):
    key = os.environ.get("CH_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CH_API_KEY is not set in Railway Variables – automatic lookup is off. "
                           "You can still type the names in by hand.")
    req = urllib.request.Request(CH_API + path, headers={
        "Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode(),
        "User-Agent": "StartitupMailRoom/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return None
        if ex.code == 401:
            raise RuntimeError("Companies House rejected the API key (401). Check CH_API_KEY in Railway Variables.")
        if ex.code == 429:
            raise RuntimeError("Companies House rate limit reached – wait a minute and try again.")
        raise RuntimeError(f"Companies House error {ex.code}")
    except urllib.error.URLError as ex:
        raise RuntimeError(f"Could not reach Companies House ({ex.reason})")


def norm_number(s: str) -> str:
    s = re.sub(r"\s+", "", (s or "").upper())
    if s.isdigit() and len(s) < 8:
        s = s.zfill(8)          # 1234567 -> 01234567
    return s


def split_officer_name(name: str) -> tuple[str, str]:
    """'DAWOOD, Faizan Ahmed' -> ('FAIZAN AHMED', 'DAWOOD')"""
    if "," in name:
        sur, fore = name.split(",", 1)
        fore = fore.strip().strip(",")
        return fore.upper(), sur.strip().upper()
    parts = name.split()
    return " ".join(parts[:-1]).upper(), (parts[-1] if parts else "").upper()


def lookup_company(number: str) -> dict:
    prof = _ch_get(f"/company/{urllib.parse.quote(number)}")
    if not prof:
        return {"ok": False, "error": f"No company found on Companies House with number {number}."}
    res = {"ok": True, "number": prof.get("company_number", number), "name": prof.get("company_name", ""),
           "status": prof.get("company_status", ""), "type": prof.get("type", ""),
           "jurisdiction": prof.get("jurisdiction", ""), "ro": ", ".join(
               v for v in [(prof.get("registered_office_address") or {}).get(k, "") for k in
                           ("care_of", "premises", "address_line_1", "address_line_2", "locality", "region", "postal_code")] if v),
           "directors": [], "pscs": [], "skipped": []}
    offs = _ch_get(f"/company/{urllib.parse.quote(number)}/officers?items_per_page=100") or {}
    for o in offs.get("items", []):
        if o.get("resigned_on"):
            continue
        role = o.get("officer_role", "")
        if role in ("director", "nominee-director"):
            fore, sur = split_officer_name(o.get("name", ""))
            dob = o.get("date_of_birth") or {}
            res["directors"].append({"forenames": fore, "surname": sur, "title": "",
                                     "dob_month": dob.get("month", ""), "dob_year": dob.get("year", "")})
        elif "director" in role:
            res["skipped"].append(f"{o.get('name')} ({role.replace('-', ' ')}) – CH01 is only for individual directors (use CH02).")
    try:
        pscs = _ch_get(f"/company/{urllib.parse.quote(number)}/persons-with-significant-control?items_per_page=100") or {}
    except RuntimeError as ex:
        pscs = {}
        res["skipped"].append(f"PSC list could not be read: {ex}")
    for p in pscs.get("items", []):
        if p.get("ceased_on") or p.get("ceased"):
            continue
        kind = p.get("kind", "")
        if kind == "individual-person-with-significant-control":
            ne = p.get("name_elements") or {}
            fore = " ".join(x for x in [ne.get("forename", ""), ne.get("middle_name", "")] if x).upper()
            sur = (ne.get("surname") or "").upper()
            if not sur:
                fore, sur = split_officer_name(p.get("name", ""))
            dob = p.get("date_of_birth") or {}
            res["pscs"].append({"forenames": fore, "surname": sur, "title": (ne.get("title") or "").upper(),
                                "dob_month": dob.get("month", ""), "dob_year": dob.get("year", "")})
        elif kind:
            res["skipped"].append(f"{p.get('name')} ({kind.replace('-', ' ')}) – PSC04 is only for individuals (use PSC05/PSC06).")
    return res


# ----------------------------------------------------------------------------- PDF form filling (AD01 / CH01 / PSC04)

def _boxes(prefix_start: int, value: str, n: int) -> dict:
    """Spread a value over n single-character boxes: Text Field <start> ... <start+n-1>."""
    v = (value or "").upper()[:n]
    return {f"Text Field {prefix_start + i}": (v[i] if i < len(v) else "") for i in range(n)}


def _boxes_list(nums: list[int], value: str) -> dict:
    v = (value or "").upper()
    return {f"Text Field {n}": (v[i] if i < len(v) else "") for i, n in enumerate(nums)}


def _split_name(name: str, width: int = 42) -> tuple[str, str]:
    name = (name or "").upper()
    if len(name) <= width:
        return name, ""
    cut = name.rfind(" ", 0, width)
    cut = cut if cut > 0 else width
    return name[:cut].strip(), name[cut:].strip()


def _ddmmyyyy(d: dt.date) -> str:
    return d.strftime("%d%m%Y")


def _mmyyyy(m, y) -> str:
    try:
        return f"{int(m):02d}{int(y):04d}"
    except (TypeError, ValueError):
        return ""


def _company(num: str, name: str) -> dict:
    n1, n2 = _split_name(name)
    f = _boxes(1, num, 8)
    f.update({"Text Field 9": n1, "Text Field 10": n2})
    return f


def _presenter(nums: dict, c: dict, addr: dict, contact: str) -> dict:
    """Presenter information box: contact = authenticating director, company = client, address = new address."""
    f = {f"Text Field {nums['contact']}": contact, f"Text Field {nums['company']}": c["name"],
         f"Text Field {nums['addr'][0]}": addr["building"], f"Text Field {nums['addr'][1]}": addr["street"],
         f"Text Field {nums['addr'][2]}": addr["street2"], f"Text Field {nums['town']}": addr["town"],
         f"Text Field {nums['county']}": addr["county"], f"Text Field {nums['country']}": addr["country"]}
    f.update(_boxes(nums["postcode"], addr["postcode"], 8))
    return f


PRESENTER = {
    "AD01": {"contact": 26, "company": 27, "addr": (29, 30, 31), "town": 32, "county": 33, "postcode": 34, "country": 42},
    "CH01": {"contact": 62, "company": 63, "addr": (65, 66, 67), "town": 68, "county": 69, "postcode": 70, "country": 78},
    "PSC04": {"contact": 70, "company": 71, "addr": (73, 74, 75), "town": 76, "county": 77, "postcode": 78, "country": 86},
}


def ad01_values(c: dict, addr: dict, auth: str) -> dict:
    f = _company(c["number"], c["name"])
    f.update({"Text Field 11": addr["building"], "Text Field 12": addr["street"], "Text Field 13": addr["street2"],
              "Text Field 14": addr["town"], "Text Field 15": addr["county"], "Text Field 24": addr["country"],
              "Text Field 25": auth, "Check Box 1": True})
    f.update(_boxes(16, addr["postcode"], 8))
    f.update(_presenter(PRESENTER["AD01"], c, addr, auth))
    return f


def ch01_values(c: dict, person: dict, addr: dict, change: dt.date, auth: str, no_ura_change: bool) -> dict:
    f = _company(c["number"], c["name"])
    f.update(_boxes_list([11, 12, 13, 14, 15, 16], _mmyyyy(person.get("dob_month"), person.get("dob_year"))))
    f.update({"Text Field 17": person.get("title", ""), "Text Field 18": person["forenames"], "Text Field 19": person["surname"]})
    f.update(_boxes(20, _ddmmyyyy(change), 8))
    f.update({"Text Field 31": addr["building"], "Text Field 32": addr["street"], "Text Field 33": addr["street2"],
              "Text Field 34": addr["town"], "Text Field 35": addr["county"], "Text Field 44": addr["country"],
              "Check Box 1": bool(no_ura_change), "Text Field 61": auth})
    f.update(_boxes(36, addr["postcode"], 8))
    f.update(_presenter(PRESENTER["CH01"], c, addr, auth))
    return f


def psc04_values(c: dict, person: dict, addr: dict, change: dt.date, auth: str) -> dict:
    f = _company(c["number"], c["name"])
    f.update(_boxes_list([11, 12, 13, 14, 15, 16], _mmyyyy(person.get("dob_month"), person.get("dob_year"))))
    f.update({"Text Field 17": person.get("title", ""), "Text Field 18": person["forenames"], "Text Field 19": person["surname"]})
    f.update(_boxes(20, _ddmmyyyy(change), 8))
    f.update({"Text Field 31": addr["building"], "Text Field 32": addr["street"], "Text Field 33": addr["street2"],
              "Text Field 34": addr["town"], "Text Field 35": addr["county"], "Text Field 44": addr["country"],
              "Text Field 69": auth})
    f.update(_boxes(36, addr["postcode"], 8))
    f.update(_boxes(61, _ddmmyyyy(change), 8))      # section 10 – date of confirmation
    f.update(_presenter(PRESENTER["PSC04"], c, addr, auth))
    return f


def fill_pdf(template: Path, values: dict) -> bytes:
    import pymupdf  # already installed for MailSort (pymupdf>=1.24)
    doc = pymupdf.open(template)
    done = set()
    for page in doc:
        for w in page.widgets():
            if w.field_name not in values:
                continue
            v = values[w.field_name]
            if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX,):
                w.field_value = w.on_state() if v else "Off"
            else:
                v = (v or "").upper()
                w.field_value = v
                if len(v) > 1 and w.rect.width > 40:          # long lines: shrink text so nothing is cut off
                    w.text_fontsize = 0
            w.update()
            done.add(w.field_name)
    buf = io.BytesIO()
    doc.save(buf, garbage=0, deflate=True)
    doc.close()
    return buf.getvalue()


# ----------------------------------------------------------------------------- Word letter filling

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]
_T = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|<w:t\s*/>")


def _suffix(day: int) -> str:
    return "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _ptext(p: str) -> str:
    return "".join(m.group(1) or "" for m in _T.finditer(p))


def _set_texts(fragment: str, newfn) -> str:
    """Rewrite each <w:t> inside fragment: newfn(index, old_text) -> new text (None = keep)."""
    i = [-1]

    def rep(m):
        i[0] += 1
        old = m.group(1) or ""
        new = newfn(i[0], old)
        if new is None:
            return m.group(0)
        return f'<w:t xml:space="preserve">{escape(new)}</w:t>'
    return _T.sub(rep, fragment)


def _after_first_br(p: str, fn) -> str:
    k = p.find("<w:br/>")
    if k < 0:
        return p
    return p[:k] + _set_texts(p[k:], fn)


def fill_letter(template: Path, *, issued: dt.date, company: str, number: str, lines: list[str],
                person: str = "", account_id: str = "") -> bytes:
    zin = zipfile.ZipFile(template)
    xml = zin.read("word/document.xml").decode("utf-8")
    hits = set()

    def para(p_match):
        p = p_match.group(0)
        t = _ptext(p).replace("’", "'")
        if t.startswith("Date Issued:"):
            hits.add("date")
            olds = [m.group(1) or "" for m in _T.finditer(p[p.find("<w:br/>"):])]
            suf = next((j for j, o in enumerate(olds) if o.strip() in ("st", "nd", "rd", "th")), None)

            def fn(j, old):
                if suf is not None and j < suf:
                    return f"{issued.day:02d}" if j == 0 else ""   # house style: 09th
                if j == suf:
                    return _suffix(issued.day)
                s = old.strip()
                if s in _MONTHS:
                    return old.replace(s, _MONTHS[issued.month - 1])
                if re.fullmatch(r"\d{4}", s):
                    return old.replace(s, str(issued.year))
                return None
            return _after_first_br(p, fn)
        if re.match(r"^(Directors' Name|PSC Name):", t):
            hits.add("person")
            return _after_first_br(p, lambda j, o: person.upper() if j == 0 else "")
        if t.startswith("Company Name:"):
            hits.add("company")
            return _after_first_br(p, lambda j, o: company.upper() if j == 0 else "")
        if "Company Number:" in t:
            hits.add("number")
            return _after_first_br(p, lambda j, o: number.upper() if j == 0 else "")
        if t.startswith("Registered Office Address:"):
            hits.add("address")
            return _after_first_br(p, lambda j, o: (lines[j] + (" " if o.endswith(" ") else "")) if j < len(lines) else "")
        if t.strip().startswith("Client Account ID:") and account_id:
            hits.add("account")
            runs = re.findall(r"<w:r[ >].*?</w:r>", p, re.S)
            rpr = re.search(r"<w:rPr>.*?</w:rPr>", runs[-1], re.S) if runs else None
            new = f'<w:r>{rpr.group(0) if rpr else ""}<w:t xml:space="preserve"> {escape(account_id)}</w:t></w:r>'
            return p[: p.rfind("</w:p>")] + new + "</w:p>"
        return p

    xml = re.sub(r"<w:p[ >].*?</w:p>", para, xml, flags=re.S)
    need = {"date", "company", "number", "address"} | ({"person"} if person else set())
    if need - hits:
        raise RuntimeError(f"Letter template {template.name} has changed – could not find: {', '.join(sorted(need - hits))}")
    # footer: the template's date (e.g. 10/06/2026) becomes the issue date
    new_parts = {"word/document.xml": xml}
    fdate = issued.strftime("%d/%m/%Y")
    for name in zin.namelist():
        if re.fullmatch(r"word/footer\d*\.xml", name):
            fx = zin.read(name).decode("utf-8")

            def fpara(m):
                p = m.group(0)
                if re.fullmatch(r"\s*\d{1,2}/\d{1,2}/\d{4,5}\s*", _ptext(p)):
                    return _set_texts(p, lambda j, o: fdate if j == 0 else "")
                return p
            new_parts[name] = re.sub(r"<w:p[ >].*?</w:p>", fpara, fx, flags=re.S)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = new_parts[item.filename].encode("utf-8") if item.filename in new_parts else zin.read(item.filename)
            zout.writestr(item, data)
    return out.getvalue()


_LO_LOCK = threading.Lock()


def _render_copy(docx: bytes, squeeze: float) -> bytes:
    """Copy of the letter used ONLY for the PDF conversion.
    - "Calibri Body"/"Arial MT" are Word aliases: name the real fonts so the converter keeps the template spacing.
    - squeeze < 1 shrinks the EMPTY spacer lines (no text, no picture) so the QR code stays on page 1 when the
      converter spaces lines a touch wider than Word."""
    zi, buf = zipfile.ZipFile(io.BytesIO(docx)), io.BytesIO()

    def spacer(m):
        p = m.group(0)
        if _ptext(p).strip() or "<w:drawing" in p or "<w:pict" in p or "<w:sectPr" in p:
            return p
        return re.sub(r'(<w:sz(?:Cs)? w:val=")(\d+)(")', lambda q: f"{q.group(1)}{max(2, int(int(q.group(2)) * squeeze))}{q.group(3)}", p)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zo:
        for it in zi.infolist():
            b = zi.read(it.filename)
            if it.filename.endswith(".xml"):
                b = b.replace(b'"Calibri Body"', b'"Calibri"').replace(b'"Arial MT"', b'"Arial"')
                if it.filename == "word/document.xml" and squeeze < 1:
                    x = b.decode("utf-8")
                    body = x.find("<w:body>")
                    x = x[:body] + re.sub(r"<w:p[ >].*?</w:p>", spacer, x[body:], flags=re.S)
                    b = x.encode("utf-8")
            zo.writestr(it, b)
    return buf.getvalue()


def docx_to_pdf(docx: bytes) -> bytes | None:
    """Convert a filled letter to PDF with LibreOffice (installed in the Docker image). None if unavailable.
    Letters are one page; if the converter spills onto a 2nd page, retry with tighter spacer lines."""
    import shutil, subprocess, tempfile
    import pymupdf
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return None
    best = None
    with _LO_LOCK, tempfile.TemporaryDirectory() as tmp:
        for squeeze in (1.0, 0.6, 0.35, 0.15):
            src = Path(tmp) / "letter.docx"
            src.write_bytes(_render_copy(docx, squeeze))
            try:
                subprocess.run([exe, f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--norestore",
                                "--convert-to", "pdf", "--outdir", tmp, str(src)],
                               check=True, timeout=180, capture_output=True, env={**os.environ, "HOME": tmp})
            except Exception as ex:
                print(f"[chforms] PDF conversion failed: {ex}", flush=True)
                return best
            pdf = Path(tmp) / "letter.pdf"
            if not pdf.exists():
                return best
            best = pdf.read_bytes()
            if pymupdf.open(stream=best).page_count == 1:
                return best
    return best


# ----------------------------------------------------------------------------- jobs / log

def _load_log() -> list:
    try:
        return json.loads(LOG_FILE.read_text())
    except Exception:
        return []


def _append_log(entry: dict):
    with _LOCK:
        OUT.mkdir(parents=True, exist_ok=True)
        log = _load_log()
        log.insert(0, entry)
        LOG_FILE.write_text(json.dumps(log[:2000], indent=1))


def _job_dir(job: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job or ""):
        abort(404)
    d = OUT / job
    if not d.is_dir():
        abort(404)
    return d


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:40] or "X"


def _people(prefix: str) -> list[dict]:
    out = []
    for i in range(50):
        if not request.form.get(f"{prefix}_sel_{i}"):
            continue
        p = {k: (request.form.get(f"{prefix}_{k}_{i}") or "").strip()
             for k in ("title", "forenames", "surname", "dob_month", "dob_year")}
        if p["surname"]:
            out.append(p)
    return out


# ----------------------------------------------------------------------------- routes

@bp.get("/forms")
def forms_home():
    missing = [t for _, t, _g in DOCS.values() if not (TPL_DIR / t).exists()]
    return render_template_string(PAGE, siu=SIU, docs=DOCS, log=_load_log()[:40], missing=missing,
                                  has_key=bool(os.environ.get("CH_API_KEY", "").strip()),
                                  today=dt.date.today().isoformat())


@bp.get("/forms/lookup")
def forms_lookup():
    num = norm_number(request.args.get("number", ""))
    if not re.fullmatch(r"[A-Z0-9]{8}", num):
        return jsonify(ok=False, error="A company number has 8 characters, e.g. 16016243 or SC123456.")
    try:
        return jsonify(lookup_company(num))
    except RuntimeError as ex:
        return jsonify(ok=False, error=str(ex), manual=True)


@bp.post("/forms/generate")
def forms_generate():
    f = request.form
    num = norm_number(f.get("number", ""))
    name = (f.get("company_name") or "").strip()
    docs = [k for k in DOCS if f.get(f"doc_{k}")]
    ref = (f.get("portal_ref") or "").strip()
    errs = []
    if not re.fullmatch(r"[A-Z0-9]{8}", num):
        errs.append("Enter a valid 8-character company number.")
    if not name:
        errs.append("Company name is missing – press Look up first (or type it in).")
    if not docs:
        errs.append("Tick at least one document.")
    if not f.get("portal_confirm"):
        errs.append("Tick the box to confirm the client requested this through the portal.")
    if not ref:
        errs.append("Enter the portal reference.")
    directors, pscs = _people("dir"), _people("psc")
    need_dir = "ch01" in docs or "dir_letter" in docs
    need_psc = "psc04" in docs or "psc_letter" in docs
    if need_dir and not directors:
        errs.append("A director document is ticked but no director is selected.")
    if need_psc and not pscs:
        errs.append("A PSC document is ticked but no PSC is selected.")
    auth = (f.get("auth_name") or "").strip().upper()
    if not auth and any(k in docs for k in ("ad01", "ch01", "psc04")):
        errs.append("Enter the name of the director authenticating the forms.")
    try:
        change = dt.date.fromisoformat(f.get("change_date") or "")
        issued = dt.date.fromisoformat(f.get("issue_date") or "")
    except ValueError:
        errs.append("Dates are not valid.")
        change = issued = dt.date.today()
    unit = (f.get("unit") or "").strip()
    addr = {k: (f.get("a_" + k) or "").strip() for k in ("building", "street", "street2", "town", "county", "postcode", "country")}
    letter_lines = [(f.get(f"l_{i}") or "").strip() for i in range(4)]
    if not addr["building"] or not addr["street"] or not addr["town"] or not addr["postcode"]:
        errs.append("The new address needs building, street, post town and postcode.")
    if errs:
        for e in errs:
            flash("⚠ " + e)
        return redirect("/forms")

    company = {"number": num, "name": name}
    job = uuid.uuid4().hex[:12]
    d = OUT / job
    d.mkdir(parents=True, exist_ok=True)
    files, warn = [], []
    acct = (f.get("account_id") or "").strip()
    try:
        def save(fname, data, kind, label):
            if kind == "docx":                       # letters are delivered as PDF
                pdf = docx_to_pdf(data)
                if pdf:
                    fname, data, kind = fname[:-5] + ".pdf", pdf, "pdf"
                else:
                    warn.append(fname)
            (d / fname).write_bytes(data)
            files.append({"file": fname, "kind": kind, "label": label})

        if "ad01" in docs:
            save(f"{num}_AD01.pdf", fill_pdf(TPL_DIR / "AD01.pdf", ad01_values(company, addr, auth)), "pdf",
                 "AD01 – Change of registered office address")
        if "ro_letter" in docs:
            save(f"{num}_Proof_of_Registered_Office_Address.docx",
                 fill_letter(TPL_DIR / "Letter_Registered_Office.docx", issued=issued, company=name, number=num,
                             lines=letter_lines, account_id=acct), "docx", "Proof of Registered Office Address")
        if need_dir:
            for p in directors:
                full = f"{p['forenames']} {p['surname']}".strip()
                tag = _safe(p["surname"] + "_" + p["forenames"].split(" ")[0] if p["forenames"] else p["surname"])
                if "ch01" in docs:
                  save(f"{num}_CH01_{tag}.pdf",
                     fill_pdf(TPL_DIR / "CH01.pdf", ch01_values(company, p, addr, change, auth, bool(f.get("no_ura_change")))),
                     "pdf", f"CH01 – Change of director's details – {full}")
                if "dir_letter" in docs:
                  save(f"{num}_Proof_of_Directors_Correspondence_Address_{tag}.docx",
                     fill_letter(TPL_DIR / "Letter_Director.docx", issued=issued, company=name, number=num,
                                 lines=letter_lines, person=full, account_id=acct), "docx",
                     f"Proof of Director's Correspondence Address – {full}")
        if need_psc:
            for p in pscs:
                full = f"{p['forenames']} {p['surname']}".strip()
                tag = _safe(p["surname"] + "_" + p["forenames"].split(" ")[0] if p["forenames"] else p["surname"])
                if "psc04" in docs:
                  save(f"{num}_PSC04_{tag}.pdf", fill_pdf(TPL_DIR / "PSC04.pdf", psc04_values(company, p, addr, change, auth)),
                     "pdf", f"PSC04 – Change of PSC details – {full}")
                if "psc_letter" in docs:
                  save(f"{num}_Proof_of_PSC_Correspondence_Address_{tag}.docx",
                     fill_letter(TPL_DIR / "Letter_PSC.docx", issued=issued, company=name, number=num,
                                 lines=letter_lines, person=full, account_id=acct), "docx",
                     f"Proof of PSC Correspondence Address – {full}")
    except Exception as ex:
        flash(f"⚠ Could not create the documents: {ex}")
        return redirect("/forms")

    meta = {"job": job, "when": dt.datetime.now().isoformat(timespec="seconds"), "number": num, "company": name,
            "docs": docs, "portal_ref": ref, "unit": unit, "address": addr, "letter_lines": letter_lines,
            "change_date": change.isoformat(), "issue_date": issued.isoformat(), "auth": auth,
            "directors": [f"{p['forenames']} {p['surname']}" for p in directors] if need_dir else [],
            "pscs": [f"{p['forenames']} {p['surname']}" for p in pscs] if need_psc else [],
            "account_id": acct, "staff": session.get("user") or os.environ.get("MAILSORT_USER", "admin"),
            "files": files}
    (d / "job.json").write_text(json.dumps(meta, indent=1))
    if warn:
        flash("⚠ PDF conversion is not available on the server yet, so these letters are in Word format: " + ", ".join(warn))
    _append_log({k: meta[k] for k in ("job", "when", "number", "company", "docs", "portal_ref", "unit", "staff")} |
                {"nfiles": len(files)})
    return redirect(f"/forms/job/{job}")


@bp.get("/forms/job/<job>")
def forms_job(job):
    d = _job_dir(job)
    meta = json.loads((d / "job.json").read_text())
    return render_template_string(JOB_PAGE, m=meta, docs=DOCS)


@bp.get("/forms/job/<job>/file/<path:fname>")
def forms_file(job, fname):
    d = _job_dir(job)
    meta = json.loads((d / "job.json").read_text())
    if fname not in {x["file"] for x in meta["files"]}:
        abort(404)
    inline = request.args.get("inline") == "1"
    mt = "application/pdf" if fname.endswith(".pdf") else \
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return send_file(d / fname, mimetype=mt, as_attachment=not inline, download_name=fname)


@bp.get("/forms/job/<job>/zip")
def forms_zip(job):
    d = _job_dir(job)
    meta = json.loads((d / "job.json").read_text())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for x in meta["files"]:
            z.write(d / x["file"], x["file"])
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{meta['number']}_{_safe(meta['company'])}_forms.zip")


def register(app):
    OUT.mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)


# ----------------------------------------------------------------------------- pages

PAGE = r"""{% extends "base" %}{% block body %}
<style>
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:4px 18px}
input[type=date],select{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;font-family:inherit}
.tick{display:flex;gap:10px;align-items:flex-start;font-weight:600;font-size:14px;margin:8px 0;cursor:pointer}
.tick input{margin-top:3px;width:16px;height:16px}
.ppl td input{padding:5px 6px;font-size:13px}.ppl td{max-width:none}
.ok{background:#e8f7ee;border:1px solid #86efac;padding:10px 14px;border-radius:8px;margin:10px 0}
.bad{background:#fee2e2;border:1px solid #fca5a5;color:#991b1b;padding:10px 14px;border-radius:8px;margin:10px 0}
.step{display:inline-flex;width:24px;height:24px;border-radius:50%;background:#111;color:#fff;font-size:13px;align-items:center;justify-content:center;margin-right:8px}
.addrprev{background:#f7f7f7;border:1px solid var(--line);border-radius:8px;padding:10px 14px;font-size:13px;line-height:1.5}
</style>
<div class="card"><h1>Company forms</h1>
<p class="muted">Creates Companies House change forms (AD01, CH01, PSC04) and our proof-of-address letters, filled from the
Companies House register. Templates are used exactly as supplied – only the client details are filled in.</p>
{% if missing %}<div class="bad">Template file(s) missing from the <b>chforms_templates</b> folder on GitHub: {{ missing|join(', ') }}</div>{% endif %}
{% if not has_key %}<div class="bad">Automatic lookup is off: add the variable <b>CH_API_KEY</b> in Railway (see setup guide). Until then, type the names in by hand.</div>{% endif %}
</div>

<form method="post" action="/forms/generate" id="ff" onsubmit="return beforeSubmit()">
<div class="card"><h2><span class="step">1</span>Client company</h2>
<div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
 <div style="flex:0 0 220px"><label>Company number</label><input type="text" name="number" id="num" placeholder="e.g. 16016243" autocomplete="off"></div>
 <button type="button" class="btn" onclick="lookup()" id="lkbtn">Look up on Companies House</button>
</div>
<div id="lkmsg"></div>
<div class="fgrid"><div><label>Company name (from Companies House)</label><input type="text" name="company_name" id="cname"></div>
<div><label>Current registered office (Companies House)</label><input type="text" id="cro" readonly style="background:#f7f7f7"></div></div>

<h2 style="margin-top:20px">Directors <span class="muted">(tick who needs a CH01 + letter)</span></h2>
<table class="ppl" id="dirs"><tr><th style="width:40px"></th><th>Title</th><th>Forename(s)</th><th>Surname</th><th>Birth month</th><th>Birth year</th></tr></table>
<button type="button" class="btn small secondary" style="margin-top:6px" onclick="addRow('dir',{})">+ Add director by hand</button>

<h2 style="margin-top:20px">PSCs <span class="muted">(individuals – tick who needs a PSC04 + letter)</span></h2>
<table class="ppl" id="pscs"><tr><th style="width:40px"></th><th>Title</th><th>Forename(s)</th><th>Surname</th><th>Birth month</th><th>Birth year</th></tr></table>
<button type="button" class="btn small secondary" style="margin-top:6px" onclick="addRow('psc',{})">+ Add PSC by hand</button>
<div id="skipped"></div>
</div>

<div class="card"><h2><span class="step">2</span>New address &amp; documents</h2>
<div class="fgrid"><div><label>Unit number (optional)</label><input type="text" name="unit" id="unit" placeholder="e.g. A80" oninput="buildAddr()"></div></div>
<details style="margin-top:12px"><summary style="cursor:pointer;font-weight:600">Address lines used (SIU Offices – click to edit)</summary>
<div class="fgrid">
 <div><label>Forms: Building name/number</label><input type="text" name="a_building" id="a_building"></div>
 <div><label>Forms: Street</label><input type="text" name="a_street" id="a_street"></div>
 <div><label>Forms: Street line 2</label><input type="text" name="a_street2" id="a_street2"></div>
 <div><label>Forms: Post town</label><input type="text" name="a_town" id="a_town"></div>
 <div><label>Forms: County/Region</label><input type="text" name="a_county" id="a_county"></div>
 <div><label>Forms: Postcode</label><input type="text" name="a_postcode" id="a_postcode"></div>
 <div><label>Forms: Country</label><input type="text" name="a_country" id="a_country"></div>
</div>
<div class="fgrid">
 <div><label>Letters: line 1</label><input type="text" name="l_0" id="l_0"></div>
 <div><label>Letters: line 2</label><input type="text" name="l_1" id="l_1"></div>
 <div><label>Letters: line 3</label><input type="text" name="l_2" id="l_2"></div>
 <div><label>Letters: line 4</label><input type="text" name="l_3" id="l_3"></div>
</div></details>
<div class="addrprev" id="addrprev" style="margin-top:10px"></div>

<h2 style="margin-top:20px">Documents needed</h2>
<p class="muted" style="margin-top:0">Tick each document you need – forms and letters can be chosen separately.</p>
<div class="fgrid">{% for g in ["Registered office", "Directors", "PSCs"] %}<div class="addrprev" style="margin-bottom:8px"><b>{{g}}</b>
{% for k, d in docs.items() if d[2] == g %}<label class="tick"><input type="checkbox" name="doc_{{k}}" value="1" id="doc_{{k}}"> {{d[0]}}</label>{% endfor %}</div>{% endfor %}</div>

<div class="fgrid" style="margin-top:8px">
 <div><label>Date of change (CH01 / PSC04)</label><input type="date" name="change_date" value="{{today}}"></div>
 <div><label>Letter "Date Issued"</label><input type="date" name="issue_date" value="{{today}}"></div>
 <div><label>Director authenticating the forms</label><input type="text" name="auth_name" id="auth" list="authlist" placeholder="filled after look-up"><datalist id="authlist"></datalist></div>
 <div><label>Client Account ID on letters (optional)</label><input type="text" name="account_id"></div>
</div>
<label class="tick"><input type="checkbox" name="no_ura_change" value="1" checked> CH01: tick "I confirm that there has been no change in the director's residential address"</label>
</div>

<div class="card"><h2><span class="step">3</span>Client request</h2>
<label class="tick"><input type="checkbox" name="portal_confirm" value="1" id="pconf"> I confirm the client asked for this through our portal.</label>
<div class="fgrid"><div><label>Portal reference</label><input type="text" name="portal_ref" id="pref" placeholder="e.g. REQ-10452"></div></div>
<div style="margin-top:18px"><button class="btn" type="submit">Create documents</button></div>
</div>
</form>

{% if log %}<div class="card"><h2>Recently created</h2>
<table><tr><th>Created</th><th>Company</th><th>Documents</th><th>Unit</th><th>Portal ref</th><th>By</th><th></th></tr>
{% for x in log %}<tr><td>{{x.when|ukdate}}</td><td>{{x.company}}<br><span class="muted">{{x.number}}</span></td>
<td>{{ x.docs|join(', ')|replace('ro_letter','RO letter')|replace('dir_letter','director letter')|replace('psc_letter','PSC letter')|upper }} <span class="muted">({{x.nfiles}} files)</span></td><td>{{x.unit or '—'}}</td><td>{{x.portal_ref}}</td><td>{{x.staff}}</td>
<td><a class="btn small secondary" href="/forms/job/{{x.job}}">Open</a></td></tr>{% endfor %}</table></div>{% endif %}

<script>
var SIU = {{ siu|tojson }};
var n = {dir:0, psc:0};
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')}
function addRow(kind, p){
  var i = n[kind]++, t = document.getElementById(kind=='dir'?'dirs':'pscs');
  var tr = document.createElement('tr');
  tr.innerHTML = '<td><input type="checkbox" name="'+kind+'_sel_'+i+'" value="1" checked style="width:16px;height:16px"></td>'
    + ['title','forenames','surname','dob_month','dob_year'].map(function(k){
        return '<td><input type="text" name="'+kind+'_'+k+'_'+i+'" value="'+esc(p[k])+'"'+(k=='title'?' style="width:70px"':'')
          +(k.indexOf('dob')==0?' style="width:80px"':'')+'></td>'}).join('');
  t.appendChild(tr);
}
function clearRows(id){var t=document.getElementById(id); while(t.rows.length>1) t.deleteRow(1);}
function lookup(){
  var num = document.getElementById('num').value.trim(), msg = document.getElementById('lkmsg');
  if(!num){msg.innerHTML='<div class="bad">Enter a company number first.</div>';return}
  document.getElementById('lkbtn').disabled = true; msg.innerHTML = '<p class="muted">Looking up '+esc(num)+' on Companies House…</p>';
  fetch('/forms/lookup?number='+encodeURIComponent(num)).then(function(r){return r.json()}).then(function(d){
    document.getElementById('lkbtn').disabled = false;
    if(!d.ok){msg.innerHTML='<div class="bad">'+esc(d.error)+'</div>';return}
    document.getElementById('num').value = d.number;
    document.getElementById('cname').value = d.name;
    document.getElementById('cro').value = d.ro;
    clearRows('dirs'); clearRows('pscs'); n = {dir:0, psc:0};
    d.directors.forEach(function(p){addRow('dir',p)}); d.pscs.forEach(function(p){addRow('psc',p)});
    var dl = document.getElementById('authlist'); dl.innerHTML='';
    d.directors.forEach(function(p){var o=document.createElement('option'); o.value=(p.forenames+' '+p.surname).trim(); dl.appendChild(o)});
    if(d.directors.length) document.getElementById('auth').value = (d.directors[0].forenames+' '+d.directors[0].surname).trim();
    var warn = (d.status && d.status!='active') ? '<div class="bad">⚠ Company status on Companies House is <b>'+esc(d.status)+'</b>.</div>' : '';
    msg.innerHTML = warn + '<div class="ok">Found <b>'+esc(d.name)+'</b> – '+d.directors.length+' active director(s), '+d.pscs.length+' individual PSC(s). Check the names below before creating.</div>';
    document.getElementById('skipped').innerHTML = d.skipped.length ? '<div class="flash">'+d.skipped.map(esc).join('<br>')+'</div>' : '';
  }).catch(function(e){document.getElementById('lkbtn').disabled=false; msg.innerHTML='<div class="bad">Look-up failed: '+esc(e)+'</div>'});
}
document.getElementById('num').addEventListener('keydown', function(e){ if(e.key=='Enter'){e.preventDefault(); lookup()} });
function buildAddr(){
  var u = document.getElementById('unit').value.trim().replace(/^unit\s*/i,'');
  var U = u ? 'Unit '+u : '';
  var v = {a_building: (U?U+', ':'')+SIU.premise, a_street: SIU.street, a_street2:'', a_town: SIU.town, a_county: SIU.county,
           a_postcode: SIU.postcode, a_country: SIU.country,
           l_0: (U?U+', ':'')+SIU.building_name, l_1: SIU.premise+' '+SIU.street, l_2: SIU.town, l_3: SIU.postcode};
  for(var k in v) document.getElementById(k).value = v[k];
  showAddr();
}
function showAddr(){
  function g(k){return document.getElementById(k).value}
  document.getElementById('addrprev').innerHTML = '<b>Forms:</b> '+esc([g('a_building'),g('a_street'),g('a_street2'),g('a_town'),g('a_county'),g('a_postcode'),g('a_country')].filter(Boolean).join(', '))
    + '<br><b>Letters:</b> '+esc([g('l_0'),g('l_1'),g('l_2'),g('l_3')].filter(Boolean).join(' / '));
}
document.querySelectorAll('[id^=a_],[id^=l_]').forEach(function(el){el.addEventListener('input', showAddr)});
buildAddr();
function beforeSubmit(){
  var p = [];
  if(!document.getElementById('cname').value.trim()) p.push('Look up the company first.');
  if(!document.querySelector('[name^=doc_]:checked')) p.push('Tick at least one document.');
  if(!document.getElementById('pconf').checked) p.push('Confirm the client asked for this through the portal.');
  if(!document.getElementById('pref').value.trim()) p.push('Enter the portal reference.');
  if(p.length){alert(p.join('\n')); return false}
  return true;
}
</script>
{% endblock %}"""

JOB_PAGE = r"""{% extends "base" %}{% block body %}
<style>
.doclist{display:grid;grid-template-columns:320px 1fr;gap:18px}
@media(max-width:900px){.doclist{grid-template-columns:1fr}}
.ditem{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:8px;cursor:pointer;background:#fff}
.ditem:hover{border-color:#999}.ditem.on{border-color:#111;box-shadow:0 0 0 1px #111}
.ditem b{display:block;font-size:14px}.ditem .muted{font-size:12px;word-break:break-all}
#pv{border:1px solid var(--line);border-radius:8px;min-height:780px;background:#f3f3f3;overflow:auto}
#pv iframe{width:100%;height:800px;border:0;display:block}
#docxbox .docx-wrapper{background:#f3f3f3;padding:16px}
</style>
<div class="card"><h1>{{m.company}} <span class="muted" style="font-size:15px">{{m.number}}</span></h1>
<div class="dgrid">
 <div><b>Created</b>{{m.when|ukdate}} by {{m.staff}}</div>
 <div><b>Portal reference</b>{{m.portal_ref}}</div>
 <div><b>New address (forms)</b>{{ [m.address.building, m.address.street, m.address.street2, m.address.town, m.address.county, m.address.postcode, m.address.country]|select|join(', ') }}</div>
 <div><b>Date of change / issued</b>{{m.change_date|ukdate}} / {{m.issue_date|ukdate}}</div>
 {% if m.directors %}<div><b>Directors</b>{{m.directors|join(', ')}}</div>{% endif %}
 {% if m.pscs %}<div><b>PSCs</b>{{m.pscs|join(', ')}}</div>{% endif %}
</div>
<div class="btnrow" style="margin-top:14px"><a class="btn" href="/forms/job/{{m.job}}/zip">⬇ Download all ({{m.files|length}}) as ZIP</a>
<a class="btn secondary" href="/forms">New request</a></div>
<p class="muted">Check every document in the preview before filing with Companies House or sending to the client.</p></div>

<div class="doclist">
<div>{% for x in m.files %}<div class="ditem{% if loop.first %} on{% endif %}" data-f="{{x.file}}" data-k="{{x.kind}}" onclick="show(this)">
<b>{{x.label}}</b><span class="muted">{{x.file}}</span>
<div style="margin-top:6px"><a class="btn small secondary" href="/forms/job/{{m.job}}/file/{{x.file}}" onclick="event.stopPropagation()">⬇ Download</a></div></div>{% endfor %}</div>
<div id="pv"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/docx-preview@0.3.5/dist/docx-preview.min.js"></script>
<script>
var base = '/forms/job/{{m.job}}/file/';
function show(el){
  document.querySelectorAll('.ditem').forEach(function(d){d.classList.remove('on')}); el.classList.add('on');
  var f = el.getAttribute('data-f'), pv = document.getElementById('pv');
  if(el.getAttribute('data-k')=='pdf'){ pv.innerHTML = '<iframe src="'+base+encodeURIComponent(f)+'?inline=1"></iframe>'; return }
  pv.innerHTML = '<div id="docxbox"><p class="muted" style="padding:16px">Loading preview…</p></div>';
  fetch(base+encodeURIComponent(f)+'?inline=1').then(function(r){return r.blob()}).then(function(b){
    if(!window.docx){ throw 'preview library could not load' }
    var box = document.getElementById('docxbox'); box.innerHTML='';
    return docx.renderAsync(b, box, null, {inWrapper:true, ignoreWidth:false, ignoreHeight:false, renderHeaders:true, renderFooters:true});
  }).catch(function(e){ pv.innerHTML = '<p style="padding:16px">Preview not available in this browser ('+e+'). Use Download to open it in Word.</p>' });
}
var first = document.querySelector('.ditem'); if(first) show(first);
</script>
{% endblock %}"""
