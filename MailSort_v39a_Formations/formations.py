"""MailSort add-on: UK company FORMATIONS tab (/formation)  –  v39a

Turns a dashboard order (pasted text + passport / proof-of-address files) into a Companies House
"copy-paste pack" that a data-entry operator can type into the Companies House "Register a company"
service without having to think:

  1. Paste the order text from dashboard.startitup.global (attachments are downloaded from the links,
     or uploaded by hand).
  2. AI reads every passport and proof of address (name, date of birth, nationality, town of birth,
     passport number, father's name, address split into Companies House boxes).
  3. Checks: name ending, sensitive words, same-name search (CH_API_KEY), names vs passports, verified
     last name vs passport, residency vs proof of address, shares = 100 %, PSC bands, expiry, age.
  4. A reviewer settles the red flags ONCE (on screen) and ticks "Reviewed".
  5. Company mailbox created on our domain through the cPanel API (registered email address) and the
     inbox is readable inside the tab (security codes are picked out automatically).
  6. Authenticator-app (TOTP) codes for GOV.UK One Login and Government Gateway – no phone needed.
  7. Everything is saved in ONE folder named after the company email:  /data/formations/<email>/
     (order, documents renamed per director, extracted data, sign-in details, filing pack PDF, outline).

app.py only needs:   import formations; formations.register(app, load_config)
The "Formations" menu link is added automatically (no change to the page header in app.py).

Railway variables (optional – the same values can be typed on the Settings page of this tab):
  CPANEL_HOST   e.g. server123.web-hosting.com      CPANEL_USER   cPanel username
  CPANEL_TOKEN  cPanel > Security > Manage API Tokens
  CH_API_KEY    (already set for Company forms) – used for the same-name check
"""
from __future__ import annotations

import base64, datetime as dt, email as _email, hashlib, hmac, html, imaplib, io, json, os, re, secrets, shutil
import ssl, string, struct, threading, time, urllib.error, urllib.parse, urllib.request, uuid, zipfile
from difflib import SequenceMatcher
from email.header import decode_header, make_header
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, render_template_string, request, send_file

VERSION = "v39a"
ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "formations"
SETTINGS_FILE = OUT / "settings.json"
LOG_FILE = OUT / "log.json"
_LOCK = threading.Lock()
_CFG = {"fn": None}
bp = Blueprint("formations", __name__)

SETTINGS_DEFAULT = {
    "mail_domain": "",                 # e.g. startitupmail.co.uk – mailboxes are created here
    "cpanel_host": "", "cpanel_user": "", "cpanel_token": "",
    "webmail_url": "",                 # blank = https://webmail.<mail_domain>
    "imap_host": "",                   # blank = mail.<mail_domain>
    "mail_quota_mb": "250",
    "name_ending": "LIMITED",
    "occupation": "Company Director",
    "shares_total": "100", "share_value": "1", "share_class": "ORDINARY", "currency": "GBP",
    "prescribed": ("Each share is entitled to one vote in any circumstances. Each share is entitled to share equally "
                   "in dividend payments or any other distribution. Each share is entitled to participate in a "
                   "distribution arising from a winding up of the company."),
    "ro_building": "SIU Offices", "ro_street": "4-6 Greatorex Street", "ro_town": "London",
    "ro_region": "", "ro_postcode": "E1 5NF", "ro_country": "United Kingdom", "ro_jurisdiction": "England and Wales",
}

NATIONALITY = {"india": "Indian", "pakistan": "Pakistani", "bangladesh": "Bangladeshi", "united kingdom": "British",
               "uk": "British", "britain": "British", "nigeria": "Nigerian", "saudi arabia": "Saudi Arabian",
               "united arab emirates": "Emirati", "uae": "Emirati", "sri lanka": "Sri Lankan", "nepal": "Nepalese",
               "china": "Chinese", "united states": "American", "usa": "American", "canada": "Canadian",
               "egypt": "Egyptian", "ghana": "Ghanaian", "kenya": "Kenyan", "turkey": "Turkish", "qatar": "Qatari",
               "oman": "Omani", "kuwait": "Kuwaiti", "bahrain": "Bahraini", "malaysia": "Malaysian",
               "philippines": "Filipino", "indonesia": "Indonesian", "morocco": "Moroccan", "afghanistan": "Afghan",
               "iran": "Iranian", "iraq": "Iraqi", "jordan": "Jordanian", "lebanon": "Lebanese", "germany": "German",
               "france": "French", "italy": "Italian", "spain": "Spanish", "poland": "Polish", "romania": "Romanian",
               "ireland": "Irish", "australia": "Australian", "south africa": "South African", "vietnam": "Vietnamese"}

# Company, Limited Liability Partnership and Business (Names and Trading Disclosures) Regulations 2015 /
# Sensitive Words Regulations 2014 – common words that need approval or a supporting letter. Warning only.
SENSITIVE = ["accredit", "accreditation", "accredited", "accrediting", "assurance", "assurer", "assurers", "association",
             "authority", "board", "charitable", "charity", "charter", "chartered", "chemist", "chemistry",
             "contact lens", "co-operative", "cooperative", "council", "dental", "dentistry", "department",
             "federation", "foundation", "friendly society", "fund", "government", "health centre", "health service",
             "health visitor", "his majesty", "her majesty", "industrial and provident society", "institute",
             "institution", "insurance", "insurer", "judicial", "king", "licensing", "medical centre", "midwife",
             "midwifery", "ministry", "nhs", "nurse", "nursing", "oversight", "patent", "patentee", "police",
             "polytechnic", "post office", "prince", "princess", "queen", "reassurance", "reinsurance",
             "reinsurer", "register", "registered", "royal", "royalty", "sheffield", "society", "special school",
             "standards", "stock exchange", "trade union", "trust", "university", "england", "english", "wales",
             "welsh", "scotland", "scottish", "ireland", "irish", "northern ireland", "british", "great britain",
             "united kingdom", "national", "bank", "banking", "audit", "auditor"]


# ----------------------------------------------------------------------------- small helpers

def _cfg() -> dict:
    try:
        return _CFG["fn"]() if _CFG["fn"] else {}
    except Exception:
        return {}


def load_settings() -> dict:
    s = dict(SETTINGS_DEFAULT)
    try:
        s.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text()).items() if v not in (None,)})
    except Exception:
        pass
    for k, env in (("cpanel_host", "CPANEL_HOST"), ("cpanel_user", "CPANEL_USER"), ("cpanel_token", "CPANEL_TOKEN"),
                   ("mail_domain", "MAIL_DOMAIN")):
        if not s.get(k) and os.environ.get(env):
            s[k] = os.environ[env].strip()
    if not s.get("webmail_url") and s.get("mail_domain"):
        s["webmail_url"] = f"https://webmail.{s['mail_domain']}"
    if not s.get("imap_host") and s.get("mail_domain"):
        s["imap_host"] = f"mail.{s['mail_domain']}"
    return s


def _now() -> str:
    return dt.datetime.now().strftime("%d/%m/%Y %H:%M")


def _toks(s: str) -> list[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", (s or "").upper()) if t]


def _title(s: str) -> str:
    """CH accepts any case – we keep UPPER CASE for names, like the passport (no thinking for the operator)."""
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


def _ddmmyyyy(iso: str) -> str:
    try:
        return dt.date.fromisoformat(iso).strftime("%d/%m/%Y")
    except Exception:
        return iso or ""


def _job_dir(job: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9@._+-]{3,120}", job or "") or job.startswith(".") or ".." in job:
        abort(404)
    d = OUT / job
    if not d.is_dir():
        abort(404)
    return d


def load_meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text())


def save_meta(d: Path, m: dict):
    m["updated"] = _now()
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False))
    tmp.replace(d / "meta.json")


def _log(entry: dict):
    with _LOCK:
        OUT.mkdir(parents=True, exist_ok=True)
        try:
            log = json.loads(LOG_FILE.read_text())
        except Exception:
            log = []
        log.insert(0, {"at": _now(), **entry})
        LOG_FILE.write_text(json.dumps(log[:3000], indent=1))


def gen_password(n: int = 16) -> str:
    """Strong but copy-safe: letters, digits and a few symbols that never break copy/paste or URLs."""
    sym = "!#%*+-=?@"
    while True:
        p = "".join(secrets.choice(string.ascii_letters + string.digits + sym) for _ in range(n))
        if (any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p)
                and any(c in sym for c in p) and p[0].isalpha()):
            return p


# ----------------------------------------------------------------------------- 1. order text parser

LABELS = [  # (key, regex that the label line starts with) – order as on dashboard.startitup.global
    ("customer_name", r"customer name"), ("customer_email", r"email$"), ("phone", r"business phone"),
    ("name1", r"business / company name suggestion 0?1"), ("name2", r"business / company name suggestion 0?2"),
    ("name3", r"business / company name suggestion 0?3"), ("ro", r"registered office address"),
    ("sic", r"please select nature of business"), ("activity", r"a brief description"),
    ("structure", r"please select company director structure"), ("id_docs", r"government issued photo id"),
    ("poa_docs", r"bank statement, utility bills"), ("directors", r"provide list of directors"),
    ("codes", r"directors personal code"), ("residency", r"please all director residency|director residency"),
    ("nationality", r"list directors nationality"), ("shares", r"please list director shareholding"),
]
DOC_RE = re.compile(r"\.(pdf|jpe?g|png|webp|heic|gif|bmp|tiff?)\b", re.I)


def _split_numbered(text: str) -> list[str]:
    parts = re.split(r"(?:^|\s)\d{1,2}\s*[.)]\s*|\n|;", text or "")
    return [p.strip(" ,-") for p in parts if p and p.strip(" ,-")]


def _name_value(item: str) -> tuple[str, str]:
    """'MUHAMMED ASHRAF PULIYODAN - India' / 'NAME -50%' / 'Kevin 45%' / 'Kevin British' -> (name, value)."""
    m = re.match(r"(.+?)\s*[-–:]\s*(.+)$", item)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"(.+?)\s+(\d+(?:\.\d+)?\s*%)$", item)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    w = item.rsplit(" ", 1)
    return (w[0], w[1]) if len(w) == 2 else (item, "")


def parse_sic(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"(\d{5})\s*[-–]?\s*([^,;\n\d]*)", text or ""):
        out.append({"code": m.group(1), "desc": m.group(2).strip(" ,.-")})
    return out[:4]


def parse_order(text: str) -> dict:
    lines = [l.rstrip() for l in (text or "").replace("\r", "").split("\n")]
    found: dict[str, list[str]] = {}
    cur = None
    for raw in lines:
        l = raw.strip()
        if not l:
            continue
        key = next((k for k, rx in LABELS if re.match(rx, l, re.I) and k not in found), None)
        if key:
            cur = key
            found[cur] = []
            continue
        if cur:
            found[cur].append(l)
    val = {k: "\n".join(v).strip() for k, v in found.items()}
    for k, v in val.items():
        if v.lower() in ("empty response", "n/a", "na", "-", "none"):
            val[k] = ""
    o = {k: val.get(k, "") for k, _ in LABELS}
    o["names"] = [n for n in (val.get("name1", ""), val.get("name2", ""), val.get("name3", "")) if n]
    o["sic_list"] = parse_sic(val.get("sic", ""))
    # attachments: markdown links  [📎 file.pdf](https://...)  or bare urls / file names
    docs = []
    for key, kind in (("id_docs", "identity"), ("poa_docs", "address")):
        for l in found.get(key, []):
            for m in re.finditer(r"\[(?:📎\s*)?([^\]]+)\]\((https?://[^)\s]+)\)", l):
                docs.append({"name": m.group(1).strip(), "url": m.group(2), "hint": kind})
            if not re.search(r"\]\(", l):
                for u in re.findall(r"https?://\S+", l):
                    docs.append({"name": urllib.parse.unquote(u.rsplit("/", 1)[-1]), "url": u, "hint": kind})
                if not re.search(r"https?://", l) and DOC_RE.search(l):
                    docs.append({"name": l.replace("📎", "").strip(), "url": "", "hint": kind})
    o["docs"] = docs
    # directors and per-director values
    names = _split_numbered(val.get("directors", ""))
    res = dict(_name_value(x) for x in _split_numbered(val.get("residency", "")))
    nat = dict(_name_value(x) for x in _split_numbered(val.get("nationality", "")))
    shr = dict(_name_value(x) for x in _split_numbered(val.get("shares", "")))
    codes = dict(_name_value(x) for x in _split_numbered(val.get("codes", "")))

    def pick(dct, name):
        best, score = "", 0.0
        for k, v in dct.items():
            s = SequenceMatcher(None, _title(k), _title(name)).ratio()
            if s > score:
                best, score = v, s
        return best if score >= 0.6 else ""

    o["directors_list"] = []
    for n in names:
        pct = re.sub(r"[^\d.]", "", pick(shr, n))
        code = re.search(r"[A-Z0-9]{3}-[A-Z0-9]{4}-[A-Z0-9]{4}", pick(codes, n).upper() or "")
        o["directors_list"].append({"full_name": _title(n), "residency": pick(res, n), "nationality_order": pick(nat, n),
                                    "share_pct": pct, "personal_code": code.group(0) if code else ""})
    return o


# ----------------------------------------------------------------------------- 2. documents (download + AI read)

SCAN_SYSTEM = """You read identity and address documents for a UK company formation agent who must fill in the
Companies House "Register a company" service. Read ONLY what is printed. Never guess or invent; unknown = "".
Dates as YYYY-MM-DD. Return ONLY one JSON object.

First decide "doc_kind": "passport" | "national_id" | "driving_licence" | "bank_statement" | "utility_bill" | "other".

For passports / ID cards (check the visual zone against the MRZ; Indian passports print the father, mother, spouse
and address on the last page – read them if that page is included):
{"doc_kind": "", "surname": "", "given_names": "",   // exactly as printed. If the Surname line is EMPTY write ""
 "surname_empty": true|false,                        // true when the document has no surname (MRZ starts IND<<<NAME)
 "date_of_birth": "", "sex": "M"|"F"|"", "nationality": "",   // nationality as printed e.g. "INDIAN"
 "place_of_birth": "", "town_of_birth": "",          // town only, e.g. "MANJERI"
 "document_number": "", "issuing_country": "", "issue_date": "", "expiry_date": "", "place_of_issue": "",
 "father_name": "", "mother_name": "", "spouse_name": "",
 "address": {"premises": "", "street": "", "locality": "", "town": "", "region": "", "postcode": "", "country": ""},
 "mrz_ok": true|false, "notes": ""}

For proofs of address (bank statement, utility bill):
{"doc_kind": "", "name_on_document": "", "issuer": "", "document_date": "",   // statement end date / bill date
 "address": {"premises": "", "street": "", "locality": "", "town": "", "region": "", "postcode": "", "country": ""},
 "phone": "", "email": "", "notes": ""}

Address boxes follow Companies House: premises = house/flat/building name or number; street = road; locality =
area/village; town = post town/city; region = state/county; postcode = PIN/ZIP/postcode; country in English
(e.g. "India", "Saudi Arabia"). Use the country printed on the document; a PIN code with Kerala means India."""


def _to_images(data: bytes, filename: str, max_pages: int = 3) -> list[tuple[str, bytes]]:
    if data[:4] == b"%PDF" or (filename or "").lower().endswith(".pdf"):
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        return [("image/png", p.get_pixmap(dpi=170).tobytes("png")) for i, p in enumerate(doc) if i < max_pages]
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    im.thumbnail((2400, 2400))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return [("image/jpeg", buf.getvalue())]


def _pdf_text(data: bytes) -> str:
    try:
        import pymupdf
        return "\n".join(p.get_text() for p in pymupdf.open(stream=data, filetype="pdf"))[:6000]
    except Exception:
        return ""


def scan_doc(data: bytes, filename: str) -> dict:
    cfg = _cfg()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic API key (MailSort Settings or ANTHROPIC_API_KEY) – type details by hand.")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(b).decode()}}
               for mt, b in _to_images(data, filename)]
    txt = _pdf_text(data) if data[:4] == b"%PDF" else ""
    content.append({"type": "text", "text": f"File name: {filename}\n"
                    + (f"Text layer of the PDF (use it to read exact spellings):\n{txt}\n" if txt.strip() else "")
                    + "Read this document and return the JSON."})
    resp = client.messages.create(model=cfg.get("model") or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5"),
                                  max_tokens=1200, system=SCAN_SYSTEM, messages=[{"role": "user", "content": content}])
    raw = resp.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError("AI reply could not be read")
    return json.loads(m.group(0))


def fetch_url(url: str) -> bytes:
    if not re.match(r"https://([a-z0-9-]+\.)*startitup\.global/", url or "", re.I):
        raise RuntimeError("only links on startitup.global are downloaded automatically")
    req = urllib.request.Request(url, headers={"User-Agent": "MailSort-Formations/1.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = r.read(25_000_000)
    if len(data) < 200:
        raise RuntimeError("file is empty")
    return data


def _doc_person_score(doc: dict, director: dict) -> float:
    dn = set(_toks(director["full_name"]))
    if not dn:
        return 0.0
    s = doc.get("scan") or {}
    nm = " ".join([s.get("given_names", ""), s.get("surname", ""), s.get("name_on_document", "")])
    t = set(_toks(nm))
    score = len(t & dn) / max(1, len(t)) if t else 0.0
    fn = set(_toks(doc.get("orig", "")))       # file name hint e.g. "1792493312_ashraf Passport.pdf"
    if fn & dn:
        score += 0.3
    return score


def assign_docs(m: dict):
    """Attach each scanned document to the director it belongs to (best name match, >= 0.5)."""
    for d in m["directors"]:
        d["passport_doc"], d["poa_doc"] = "", ""
    for doc in m["docs"]:
        s = doc.get("scan") or {}
        kind = "passport" if s.get("doc_kind") in ("passport", "national_id", "driving_licence") else (
            "poa" if s.get("doc_kind") in ("bank_statement", "utility_bill") else
            ("passport" if doc.get("hint") == "identity" else "poa"))
        doc["kind"] = kind
        best, sc = None, 0.0
        for d in m["directors"]:
            x = _doc_person_score(doc, d)
            if x > sc:
                best, sc = d, x
        doc["director"] = best["full_name"] if best and sc >= 0.5 else ""
        if best and sc >= 0.5 and not best.get(kind + "_doc"):
            best[kind + "_doc"] = doc["file"]


def rename_docs(d: Path, m: dict):
    """Files are kept per director: <NAME>_passport.pdf / <NAME>_proof_of_address.pdf."""
    for doc in m["docs"]:
        if not doc.get("director") or doc.get("renamed"):
            continue
        ext = Path(doc["file"]).suffix.lower() or ".pdf"
        base = re.sub(r"[^A-Z0-9]+", "_", doc["director"])[:40] + ("_passport" if doc["kind"] == "passport" else "_proof_of_address")
        new, i = base + ext, 2
        while (d / "documents" / new).exists():
            new, i = f"{base}_{i}{ext}", i + 1
        (d / "documents" / doc["file"]).rename(d / "documents" / new)
        for dd in m["directors"]:
            for k in ("passport_doc", "poa_doc"):
                if dd.get(k) == doc["file"]:
                    dd[k] = new
        doc["file"], doc["renamed"] = new, True


# ----------------------------------------------------------------------------- 3. derive Companies House values

def _doc(m: dict, fname: str) -> dict:
    return next((x.get("scan") or {} for x in m["docs"] if x["file"] == fname), {}) if fname else {}


def _addr_ok(a: dict) -> bool:
    return bool(a and (a.get("premises") or a.get("street")) and a.get("country"))


def derive(m: dict, force: bool = False):
    """Fill every director's Companies House fields from order + documents, then re-apply the reviewer's
    typed corrections (d['edit'])."""
    st = load_settings()
    for d in m["directors"]:
        p = _doc(m, d.get("passport_doc"))
        poa = _doc(m, d.get("poa_doc"))
        full = d["full_name"]
        sur, giv = _title(p.get("surname")), _title(p.get("given_names"))
        vlast = _title(d.get("verified_last_name"))
        if vlast:                                   # match the identity Companies House verified – exactly
            surname = vlast
            rest = [t for t in _toks(f"{giv} {sur}" if (giv or sur) else full) if t not in set(_toks(vlast))]
            fn = rest
        elif sur:
            surname, fn = sur, giv.split()
        elif giv:                                   # passport with NO surname – see flags
            parts = giv.split()
            surname, fn = parts[-1], parts[:-1]
        else:
            parts = full.split()
            surname, fn = (parts[-1] if parts else ""), parts[:-1]
        d["first_name"] = fn[0] if fn else ""
        d["middle_names"] = " ".join(fn[1:])
        d["surname"] = surname
        d["title"] = {"M": "MR", "F": "MS"}.get(p.get("sex", ""), "")
        d["dob"] = p.get("date_of_birth", "")
        nat = (p.get("nationality") or d.get("nationality_order") or "").strip()
        d["nationality"] = NATIONALITY.get(nat.lower(), nat.title())
        if nat.upper() == "INDIAN":
            d["nationality"] = "Indian"
        d["occupation"] = d.get("occupation") or st["occupation"]
        d["town_of_birth"] = _title(p.get("town_of_birth") or (p.get("place_of_birth", "").split(",")[0]))
        d["passport_no"] = p.get("document_number", "")
        d["passport_expiry"] = p.get("expiry_date", "")
        d["father_first"] = _title((p.get("father_name") or "").split(" ")[0]) if p.get("father_name") else ""
        # residential address: proof of address wins only when it agrees with the stated residency
        pa, qa = p.get("address") or {}, poa.get("address") or {}
        stated = (d.get("residency") or "").strip().lower()
        choice = d.get("addr_choice", "")
        if not choice:
            if _addr_ok(qa) and (not stated or qa.get("country", "").lower() == stated):
                choice = "poa"
            elif _addr_ok(qa) and _addr_ok(pa) and qa.get("country", "").lower() != stated and pa.get("country", "").lower() == stated:
                choice = ""             # conflict -> reviewer decides
            elif _addr_ok(qa):
                choice = "poa"
            elif _addr_ok(pa):
                choice = "passport"
        d["addr_auto"] = choice
        src = qa if choice == "poa" else pa if choice == "passport" else {}
        d["res"] = {k: _title(src.get(k, "")) for k in ("premises", "street", "locality", "town", "region", "postcode")}
        d["res"]["country"] = (src.get("country") or "").title()
        d["residence_country"] = d["res"]["country"] or (d.get("residency") or "").title()
        for k, v in (d.get("edit") or {}).items():       # reviewer's typed corrections always win
            if k.startswith("res."):
                d["res"][k[4:]] = v
            else:
                d[k] = v
        if "residence_country" not in (d.get("edit") or {}) and d["res"].get("country"):
            d["residence_country"] = d["res"]["country"]


def pct_band(p: float) -> str:
    if p > 75:
        return "75% or more"
    if p > 50:
        return "More than 50% but less than 75%"
    if p > 25:
        return "More than 25% but not more than 50%"
    return ""


def share_split(m: dict) -> list[dict]:
    st = load_settings()
    total = int(float(m["company"].get("shares_total") or st["shares_total"]))
    out, given = [], 0
    for i, d in enumerate(m["directors"]):
        p = float(d.get("share_pct") or 0)
        n = round(total * p / 100)
        if i == len(m["directors"]) - 1 and abs(sum(float(x.get("share_pct") or 0) for x in m["directors"]) - 100) < 0.01:
            n = total - given
        given += n
        out.append({"name": d["full_name"], "pct": p, "shares": n})
    return out


def checks(m: dict) -> list[dict]:
    """Every problem a learned person would have spotted. level: red = must be settled before filing."""
    f = []
    add = lambda lvl, txt, key="": f.append({"level": lvl, "text": txt, "key": key or hashlib.md5(txt.encode()).hexdigest()[:8]})
    c = m["company"]
    names = m["order"].get("names", [])
    if len({_title(n) for n in names}) <= 1 and names:
        add("amber", "All 3 company-name choices are the same – if Companies House refuses it there is no back-up. Ask the customer for a 2nd choice.", "names-same")
    for w in SENSITIVE:
        if re.search(r"\b" + re.escape(w) + r"\b", c["name"], re.I):
            add("red", f"Company name contains the sensitive word “{w.upper()}” – needs Companies House approval/supporting letter, or change the name.", "sens-" + w)
    if c.get("name_check"):
        add("red" if c["name_check"].startswith("TAKEN") else "green", "Name search: " + c["name_check"], "namecheck")
    sic = c.get("sic") or []
    if not sic:
        add("red", "No SIC code.", "sic0")
    for s in sic:
        if not re.fullmatch(r"\d{5}", s["code"]):
            add("red", f"SIC “{s['code']}” is not a 5-digit code.", "sicbad")
    if len(m["directors"]) == 0:
        add("red", "No directors found in the order.", "nodir")
    tot = sum(float(d.get("share_pct") or 0) for d in m["directors"])
    if abs(tot - 100) > 0.01:
        add("red", f"Shareholding adds up to {tot:g}% – must be 100%.", "pct")
    if not m.get("mailbox", {}).get("email"):
        add("amber", "Company email (registered email address) not created yet – step 3.", "mail")
    today = dt.date.today()
    for d in m["directors"]:
        n = d["full_name"]
        if not d.get("passport_doc"):
            add("red", f"{n}: no passport/ID matched to this director.", "pp-" + n)
            continue
        p = _doc(m, d["passport_doc"])
        pn = set(_toks(f"{p.get('given_names', '')} {p.get('surname', '')}"))
        if pn and pn != set(_toks(n)):
            add("red", f"{n}: name in the order does not match the passport ({_title(p.get('given_names'))} / {_title(p.get('surname')) or 'no surname'}).", "ppname-" + n)
        if p.get("surname_empty") or not p.get("surname"):
            add("amber", f"{n}: the passport has NO surname (all names are given names). Use the split that matches the identity Companies House verified – paste the personal-code email (step 2).", "nosur-" + n)
        vl = d.get("verified_last_name")
        if vl and p.get("surname") and set(_toks(vl)) != set(_toks(p.get("surname"))):
            add("red", f"{n}: Companies House verified the last name as “{vl}” but the passport surname is “{p.get('surname')}”. The filing uses the verified last name – check the personal code is accepted.", "vl-" + n)
        if vl and not d.get("first_name"):
            add("amber", f"{n}: verified last name is the whole name “{vl}”, so First name is blank. If the Companies House screen insists on a first name, stop and ask the reviewer – do not invent one.", "fnblank-" + n)
        try:
            if p.get("expiry_date") and dt.date.fromisoformat(p["expiry_date"]) < today:
                add("red", f"{n}: passport expired on {_ddmmyyyy(p['expiry_date'])}.", "exp-" + n)
            if p.get("date_of_birth"):
                b = dt.date.fromisoformat(p["date_of_birth"])
                if (today - b).days < 16 * 365.25:
                    add("red", f"{n}: under 16 – cannot be a director.", "age-" + n)
        except ValueError:
            add("amber", f"{n}: a date on the passport could not be read – type it on the review page.", "date-" + n)
        if not d.get("poa_doc"):
            add("amber", f"{n}: no proof of address matched to this director.", "poa-" + n)
        else:
            q = _doc(m, d["poa_doc"])
            qa = (q.get("address") or {})
            stated = (d.get("residency") or "").strip()
            if qa.get("country") and stated and qa["country"].lower() != stated.lower():
                pa = (p.get("address") or {})
                add("red", f"{n}: order says residency “{stated}” but the proof of address is in “{qa['country']}”"
                    + (f" (passport address: {pa.get('country')})" if pa.get("country") else "")
                    + ". Choose the usual residential address on the review page (and confirm with the customer).", "res-" + n)
            try:
                if q.get("document_date") and (today - dt.date.fromisoformat(q["document_date"])).days > 92:
                    add("amber", f"{n}: proof of address is older than 3 months ({_ddmmyyyy(q['document_date'])}).", "poaold-" + n)
            except ValueError:
                pass
            qn = set(_toks(q.get("name_on_document")))
            if qn and not qn <= set(_toks(n)):
                add("amber", f"{n}: name on proof of address is “{q.get('name_on_document')}”.", "poaname-" + n)
        if not d.get("addr_choice") and not d.get("addr_auto"):
            add("red", f"{n}: residential address not decided.", "addr-" + n)
        if not d.get("personal_code"):
            add("red", f"{n}: no Companies House personal code yet (identity verification). Paste the verification email in step 2.", "code-" + n)
        if d.get("nationality_order") and d.get("nationality") and d["nationality_order"].lower() in NATIONALITY and \
                NATIONALITY[d["nationality_order"].lower()] != d["nationality"]:
            add("amber", f"{n}: order nationality “{d['nationality_order']}” vs passport “{d['nationality']}”.", "nat-" + n)
    return f


def unresolved_red(m: dict) -> list[dict]:
    ack = set(m.get("ack", []))
    return [x for x in checks(m) if x["level"] == "red" and x["key"] not in ack]


# ----------------------------------------------------------------------------- 4. the copy-paste pack

def _addr_rows(prefix: str, a: dict) -> list:
    return [(f"{prefix} – Property name or number", a.get("premises", "")),
            (f"{prefix} – Address line 1 (street)", a.get("street", "")),
            (f"{prefix} – Address line 2 (area)", a.get("locality", "")),
            (f"{prefix} – Town or city", a.get("town", "")),
            (f"{prefix} – County / State / Region", a.get("region", "")),
            (f"{prefix} – Postcode", a.get("postcode", "")),
            (f"{prefix} – Country", a.get("country", ""))]


def build_pack(m: dict) -> list[dict]:
    """Sections in the order the Companies House 'Register a company' service asks."""
    st = load_settings()
    c, mb, secs = m["company"], m.get("mailbox", {}), []
    S = lambda title, rows, note="": secs.append({"title": title, "rows": [r for r in rows if r is not None], "note": note})
    ro = {"premises": c.get("ro_building", st["ro_building"]), "street": c.get("ro_street", st["ro_street"]),
          "locality": "", "town": c.get("ro_town", st["ro_town"]), "region": c.get("ro_region", st["ro_region"]),
          "postcode": c.get("ro_postcode", st["ro_postcode"]), "country": c.get("ro_country", st["ro_country"])}
    S("A. Sign in (GOV.UK One Login for this company)", [
        ("Email address", mb.get("email", "⚠ create the mailbox first (step 3)")),
        ("One Login password", m.get("creds", {}).get("onelogin_password", "")),
        ("Email security code", "Press “Get latest code” on the job page – it reads the company inbox"),
        ("2-step: choose", "Authenticator app (NOT text message – text codes only work for UK mobiles)"),
        ("Authenticator secret", "Copy the secret key shown by GOV.UK into step 4 on the job page – the 6-digit code appears there"),
    ], "One account per company, owned by the company email.")
    S("B. Company name", [
        ("Company name (without ending)", c["name"]),
        ("Name ending", c.get("ending") or st["name_ending"]),
        ("Full name will be", f"{c['name']} {c.get('ending') or st['name_ending']}"),
        ("Company type", "Private company limited by shares"),
    ])
    S("C. Registered office", [("Where is the registered office (jurisdiction)", c.get("ro_jurisdiction", st["ro_jurisdiction"]))]
      + _addr_rows("Registered office", ro))
    S("D. Registered email address", [("Registered email address", mb.get("email", "⚠ not created yet"))],
      "Companies House does not publish it – it is our company mailbox.")
    S("E. Nature of business (SIC)", [(f"SIC code {i + 1}", f"{s['code']}", s.get("desc", "")) for i, s in enumerate(c.get("sic", []))])
    S("F. Articles", [("Articles of association", "Model articles (no changes)")])
    for i, d in enumerate(m["directors"], 1):
        a = d.get("res") or {}
        dob = d.get("dob", "")
        try:
            b = dt.date.fromisoformat(dob)
            dd, mm, yy = f"{b.day:02d}", f"{b.month:02d}", str(b.year)
        except ValueError:
            dd = mm = yy = "⚠"
        rows = [("Personal code (identity verification)", d.get("personal_code") or "⚠ MISSING"),
                ("Title", d.get("title", "")), ("First name", d.get("first_name", "")),
                ("Middle name(s)", d.get("middle_names", "")), ("Last name", d.get("surname", "")),
                ("Former names", "None"),
                ("Date of birth – Day", dd), ("Date of birth – Month", mm), ("Date of birth – Year", yy),
                ("Nationality", d.get("nationality", "")),
                ("Country / state of usual residence", d.get("residence_country", "")),
                ("Occupation", d.get("occupation", "")),
                ("Correspondence (service) address", "Same as registered office address"),
                ] + _addr_rows("Home (residential) address", a) + [
                ("Section 243 exemption", "No")]
        if not d.get("addr_choice") and not d.get("addr_auto"):
            rows = [(r[0], "⚠ DECISION NEEDED – see review page") if r[0].startswith("Home") else r for r in rows]
        S(f"G{i}. Director {i}: {d['full_name']}", rows)
    sp = share_split(m)
    st_total = int(float(c.get("shares_total") or st["shares_total"]))
    val = c.get("share_value") or st["share_value"]
    S("H. Share capital (statement of capital)", [
        ("Currency", c.get("currency") or st["currency"]), ("Class of shares", c.get("share_class") or st["share_class"]),
        ("Total number of shares", str(st_total)), ("Value of each share (£)", val),
        ("Amount paid per share (£)", val), ("Amount unpaid per share (£)", "0"),
        ("Total aggregate value (£)", f"{st_total * float(val):g}"),
        ("Prescribed particulars (rights attached)", c.get("prescribed") or st["prescribed"])])
    for i, (d, s) in enumerate(zip(m["directors"], sp), 1):
        S(f"I{i}. Shareholder / subscriber {i}: {d['full_name']}", [
            ("Shareholder type", "Individual"), ("First name(s)", " ".join(x for x in (d.get('first_name'), d.get('middle_names')) if x)),
            ("Last name", d.get("surname", "")), ("Class", c.get("share_class") or st["share_class"]),
            ("Number of shares", str(s["shares"])), ("Address", "Same as registered office address"),
            ("Security answer – Town of birth (first 3 letters)", (d.get("town_of_birth") or "")[:3]),
            ("Security answer – Passport number (last 3)", (d.get("passport_no") or "")[-3:]),
            ("Security answer – Father's first name (first 3 letters)", (d.get("father_first") or "")[:3]),
        ], "The 3 security answers are only needed if the screen asks for personal information for the subscriber.")
    for i, (d, s) in enumerate(zip(m["directors"], sp), 1):
        band = pct_band(s["pct"])
        if not band:
            continue
        S(f"J{i}. Person with significant control {i}: {d['full_name']}", [
            ("Is this person also a director?", "Yes – same details as Director " + str(i)),
            ("Personal code", d.get("personal_code") or "⚠ MISSING"),
            ("Ownership of shares", band), ("Ownership of voting rights", band),
            ("Right to appoint or remove the majority of directors", "No" if s["pct"] <= 50 else "Yes"),
            ("Significant influence or control", "No")])
    S("K. Statement of compliance / submit", [
        ("Who is filing", "Agent on behalf of all subscribers – STARTITUP ACSP"),
        ("Lawful purpose statement", "Tick – confirmed"), ("Statement of compliance", "Tick – confirmed"),
        ("Fee", "Pay by company card"),
        ("Government Gateway user ID (created at the end)", m.get("creds", {}).get("gateway_id") or "Write it on the job page (step 4) when shown"),
        ("Government Gateway password", m.get("creds", {}).get("gateway_password", ""))])
    return secs


# ----------------------------------------------------------------------------- 5. mailbox (cPanel UAPI) + inbox (IMAP)

def _local_part(company: str) -> str:
    stop = {"LIMITED", "LTD", "THE", "AND", "OF", "UK", "GLOBAL", "SERVICES", "SOLUTIONS", "BUSINESS", "GROUP", "COMPANY"}
    w = [t.lower() for t in _toks(company) if t not in stop] or [t.lower() for t in _toks(company)]
    return ("".join(w[:2]) or "company")[:24]


def cpanel(func: str, **params) -> dict:
    st = load_settings()
    if not (st["cpanel_host"] and st["cpanel_user"] and st["cpanel_token"]):
        raise RuntimeError("cPanel is not set up – fill in host, user and API token on the Formations settings page.")
    host = st["cpanel_host"].replace("https://", "").replace("http://", "").strip("/")
    if ":" not in host:
        host += ":2083"
    url = f"https://{host}/execute/Email/{func}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"cpanel {st['cpanel_user']}:{st['cpanel_token']}",
                                               "User-Agent": "MailSort-Formations/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        res = json.loads(r.read().decode())
    if not res.get("status"):
        raise RuntimeError("cPanel: " + "; ".join(res.get("errors") or ["unknown error"]))
    return res


def create_mailbox(company: str, wanted: str = "") -> tuple[str, str]:
    st = load_settings()
    dom = st["mail_domain"]
    if not dom:
        raise RuntimeError("Mail domain is not set (Formations settings).")
    existing = set()
    try:
        for x in cpanel("list_pops", domain=dom).get("data") or []:
            existing.add((x.get("email") or "").lower())
    except Exception:
        pass
    base = re.sub(r"[^a-z0-9.-]", "", (wanted or _local_part(company)).lower()) or "company"
    lp, i = base, 2
    while f"{lp}@{dom}" in existing:
        lp, i = f"{base}{i}", i + 1
    pw = gen_password()
    cpanel("add_pop", email=lp, domain=dom, password=pw, quota=st.get("mail_quota_mb") or "250", skip_update_db=1)
    return f"{lp}@{dom}", pw


CODE_RE = re.compile(r"(?<![\d-])(\d{6})(?![\d-])")


def _dec(s) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _body_text(msg) -> str:
    parts = []
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        ct = p.get_content_type()
        if ct in ("text/plain", "text/html") and not p.get_filename():
            try:
                t = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ct == "text/html":
                t = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script).*?</\1>", " ", t)))
            parts.append(t)
            if ct == "text/plain":
                break
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def read_inbox(email_addr: str, password: str, n: int = 12) -> list[dict]:
    st = load_settings()
    host = st["imap_host"] or ("mail." + email_addr.split("@", 1)[1])
    M = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context())
    try:
        M.login(email_addr, password)
        M.select("INBOX", readonly=True)
        typ, data = M.search(None, "ALL")
        ids = (data[0].split() if data and data[0] else [])[-n:]
        out = []
        for i in reversed(ids):
            typ, msgdata = M.fetch(i, "(RFC822)")
            msg = _email.message_from_bytes(msgdata[0][1])
            body = _body_text(msg)
            sub = _dec(msg.get("Subject"))
            code = ""
            for mm in CODE_RE.finditer(sub + " " + body):
                ctx = (sub + " " + body)[max(0, mm.start() - 120):mm.start()].lower()
                if "code" in ctx or "security" in ctx or "verification" in ctx or "access" in ctx:
                    code = mm.group(1)
                    break
            out.append({"from": _dec(msg.get("From")), "subject": sub, "date": msg.get("Date", ""),
                        "code": code, "snippet": body[:400]})
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ----------------------------------------------------------------------------- 6. authenticator app (TOTP, RFC 6238)

def _b32(secret: str) -> bytes:
    s = re.sub(r"[^A-Z2-7]", "", (secret or "").upper())
    return base64.b32decode(s + "=" * (-len(s) % 8))


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> tuple[str, int]:
    t = int((at if at is not None else time.time()) // step)
    h = hmac.new(_b32(secret), struct.pack(">Q", t), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    left = step - int((at if at is not None else time.time()) % step)
    return f"{code:0{digits}d}", left


def parse_secret(text: str) -> str:
    """Accepts the plain key GOV.UK shows ('Enter this secret key') or an otpauth:// link."""
    t = (text or "").strip()
    m = re.search(r"secret=([A-Za-z2-7=]+)", t)
    s = (m.group(1) if m else re.sub(r"\s+", "", t)).upper().rstrip("=")
    if not re.fullmatch(r"[A-Z2-7]{16,128}", s):
        raise ValueError("That does not look like an authenticator secret key (letters A–Z and digits 2–7).")
    _b32(s)
    return s


# ----------------------------------------------------------------------------- 7. personal-code emails

def parse_personal_codes(text: str) -> list[dict]:
    """'Your last name: Abdul Gafoor ... Your Companies House personal code: CCH-2L4N-2223' (email or WhatsApp copy).
    Several can be pasted together."""
    out = []
    t = text or ""
    for m in re.finditer(r"last name:\s*([^\n]+?)\s*(?:\n|your companies house personal code)", t, re.I):
        rest = t[m.end() - 40:]
        c = re.search(r"personal code:?\s*\**\s*([A-Z0-9]{3}-[A-Z0-9]{4}-[A-Z0-9]{4})", rest, re.I)
        if c:
            out.append({"last_name": _title(m.group(1)), "code": c.group(1).upper()})
    if not out:
        for c in re.findall(r"\b([A-Z0-9]{3}-[A-Z0-9]{4}-[A-Z0-9]{4})\b", t.upper()):
            out.append({"last_name": "", "code": c})
    return out


def apply_codes(m: dict, found: list[dict]) -> list[str]:
    msgs = []
    for f in found:
        best, sc = None, 0.0
        for d in m["directors"]:
            if f["last_name"]:
                lt, dt_ = set(_toks(f["last_name"])), set(_toks(d["full_name"]))
                s = len(lt & dt_) / max(1, len(lt))
            else:
                s = 0.0
            if s > sc:
                best, sc = d, s
        if best and sc >= 0.99:
            best["personal_code"], best["verified_last_name"] = f["code"], f["last_name"]
            msgs.append(f"{f['code']} → {best['full_name']} (verified last name: {f['last_name']})")
        else:
            msgs.append(f"{f['code']} (last name '{f['last_name']}') – no director matched; enter it by hand")
    return msgs


# ----------------------------------------------------------------------------- 8. outputs (pack PDF, outline, zip)

def _story_pdf(html_doc: str) -> bytes:
    import pymupdf
    buf = io.BytesIO()
    story = pymupdf.Story(html=html_doc, user_css="body{font-family:sans-serif;font-size:9.5pt} h1{font-size:15pt} "
                          "h2{font-size:11pt;margin:10px 0 3px;background:#eee;padding:3px} p.r{margin:2px 0} "
                          ".k{color:#555} .w{color:#b00}")
    writer = pymupdf.DocumentWriter(buf)
    rect = pymupdf.paper_rect("a4")
    where = rect + (36, 36, -36, -36)
    more = 1
    while more:
        dev = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def pack_html(m: dict, for_pdf: bool = False) -> str:
    e = html.escape
    out = [f"<h1>Companies House filing pack – {e(m['company']['name'])} {e(m['company'].get('ending', ''))}</h1>",
           f"<p>Folder / company email: <b>{e(m.get('mailbox', {}).get('email', m['job']))}</b> · prepared {e(_now())}"
           f" · {'REVIEWED by ' + e(m.get('reviewed_by', '')) if m.get('reviewed') else '<span class=w>NOT REVIEWED</span>'}</p>"]
    for s in build_pack(m):
        out.append(f"<h2>{e(s['title'])}</h2>")
        for r in s["rows"]:
            v = str(r[1]) if r[1] else "(leave empty)"
            out.append(f"<p class=r><span class=k>{e(r[0])}:</span> <b>{e(v)}</b>{(' <i>(' + e(r[2]) + ')</i>') if len(r) > 2 and r[2] else ''}</p>")
        out.append(f"<p><i>{e(s['note'])}</i></p>" if s.get("note") else "")
    return "".join(out)


def outline_text(m: dict) -> str:
    """Same layout as the team's 'Company Outline' – but complete (DOB, home address, codes, login)."""
    c, mb, cr = m["company"], m.get("mailbox", {}), m.get("creds", {})
    st = load_settings()
    ro = f"{c.get('ro_building', st['ro_building'])}, {c.get('ro_street', st['ro_street'])}, {c.get('ro_town', st['ro_town'])}, {c.get('ro_postcode', st['ro_postcode'])}, {c.get('ro_country', st['ro_country'])}"
    L = ["COMPANY OUTLINE", f"Company Name: {c['name']} {c.get('ending', '')}", f"Registered Office Address: {ro}", ""]
    ords = ["1st", "2nd", "3rd", "4th", "5th", "6th"]
    for i, d in enumerate(m["directors"]):
        a = d.get("res") or {}
        home = ", ".join(x for x in (a.get("premises"), a.get("street"), a.get("locality"), a.get("town"),
                                     a.get("region"), a.get("postcode"), a.get("country")) if x)
        L += [f"{ords[i] if i < 6 else str(i + 1) + 'th'} Director's Full Name: {d['full_name']}",
              f"   CH name split: First: {d.get('first_name', '')} | Middle: {d.get('middle_names', '')} | Last: {d.get('surname', '')}",
              f"DOB: {_ddmmyyyy(d.get('dob', ''))}", f"Home Address: {home or 'DECISION NEEDED'}",
              f"Correspondence Address: {ro}", f"Director's Nationality: {d.get('nationality', '')}",
              f"Director's Residency: {d.get('residence_country', '')}", f"Occupation: {d.get('occupation', '')}",
              f"Personal Code: {d.get('personal_code') or 'MISSING'}", ""]
    L.append("Shareholding:")
    for i, s in enumerate(share_split(m), 1):
        L.append(f"{i}-{s['name']} - {s['pct']:g}%  ({s['shares']} shares)")
    L += ["SIC Code:"] + [f"{s['code']} {s.get('desc', '')}" for s in c.get("sic", [])]
    val = c.get("share_value") or st["share_value"]
    L += [f"Share Capital – {c.get('shares_total') or st['shares_total']} Share @ £{val}"]
    for i, s in enumerate(share_split(m), 1):
        L.append(f"{i}-{s['name']} – {s['shares'] * float(val):g} GBP")
    L += ["", f"Login Link: {load_settings().get('webmail_url', '')}", f"Email: {mb.get('email', '')}",
          f"Pass: {mb.get('password', '')}", f"GOV.UK One Login password: {cr.get('onelogin_password', '')}",
          f"Government Gateway user ID: {cr.get('gateway_id', '')}", f"Government Gateway password: {cr.get('gateway_password', '')}",
          f"Company number: {m.get('company_number', '')}"]
    return "\n".join(L)


def write_outputs(d: Path, m: dict):
    """Refresh every file in the folder (called after each change)."""
    (d / "order.txt").write_text(m.get("order_text", ""))
    (d / "extracted.json").write_text(json.dumps({"order": m["order"], "documents": m["docs"]}, indent=1, ensure_ascii=False))
    (d / "Company_Outline.txt").write_text(outline_text(m))
    cr = m.get("creds", {})
    mb = m.get("mailbox", {})
    (d / "sign_in_details.txt").write_text("\n".join([
        f"Company: {m['company']['name']} {m['company'].get('ending', '')}",
        f"Company email: {mb.get('email', '')}", f"Email password: {mb.get('password', '')}",
        f"Webmail: {load_settings().get('webmail_url', '')}",
        f"GOV.UK One Login email: {mb.get('email', '')}", f"GOV.UK One Login password: {cr.get('onelogin_password', '')}",
        f"GOV.UK One Login authenticator secret: {cr.get('onelogin_totp', '')}",
        f"Government Gateway user ID: {cr.get('gateway_id', '')}", f"Government Gateway password: {cr.get('gateway_password', '')}",
        f"Government Gateway authenticator secret: {cr.get('gateway_totp', '')}",
        f"Companies House company number: {m.get('company_number', '')}",
        f"Companies House authentication code: {m.get('auth_code', '')}", ""]))
    try:
        (d / "CH_filing_pack.pdf").write_bytes(_story_pdf(pack_html(m, True)))
    except Exception as ex:
        print(f"[formations] pack pdf failed: {ex}", flush=True)


def process_job(job: str):
    """Background: download attachments, AI-read every document, match to directors, derive, write files."""
    d = OUT / job
    m = load_meta(d)
    m["scan_state"], m["scan_msg"] = "running", "Downloading attachments…"
    save_meta(d, m)
    for doc in m["docs"]:
        if not doc.get("file") and doc.get("url"):
            try:
                data = fetch_url(doc["url"])
                fn = re.sub(r"[^A-Za-z0-9._-]+", "_", doc["name"])[-80:]
                (d / "documents" / fn).write_bytes(data)
                doc["file"], doc["orig"] = fn, doc["name"]
            except Exception as ex:
                doc["error"] = f"download failed: {ex} – upload this file by hand"
    for i, doc in enumerate(m["docs"]):
        if doc.get("file") and not doc.get("scan"):
            m["scan_msg"] = f"AI reading document {i + 1} of {len(m['docs'])}: {doc['file']}"
            save_meta(d, m)
            try:
                doc["scan"] = scan_doc((d / "documents" / doc["file"]).read_bytes(), doc.get("orig") or doc["file"])
                doc.pop("error", None)
            except Exception as ex:
                doc["error"] = f"AI read failed: {ex}"
    assign_docs(m)
    rename_docs(d, m)
    derive(m)
    m["scan_state"], m["scan_msg"] = "done", f"Read {sum(1 for x in m['docs'] if x.get('scan'))} of {len(m['docs'])} documents."
    save_meta(d, m)
    write_outputs(d, m)


def name_check(name: str) -> str:
    key = os.environ.get("CH_API_KEY", "").strip()
    if not key:
        return ""
    q = urllib.parse.quote(name)
    req = urllib.request.Request(f"https://api.company-information.service.gov.uk/search/companies?q={q}&items_per_page=20",
                                 headers={"Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode()})
    with urllib.request.urlopen(req, timeout=20) as r:
        items = json.loads(r.read().decode()).get("items", [])
    norm = lambda s: re.sub(r"\b(LIMITED|LTD|PLC|LLP|CO|COMPANY|AND|THE|UK|SERVICES?)\b|[^A-Z0-9]", "", (s or "").upper()
                            .replace("&", " AND "))
    same = [i for i in items if norm(i.get("title")) == norm(name) and i.get("company_status") not in ("dissolved",)]
    if same:
        return "TAKEN – " + "; ".join(f"{i['title']} ({i.get('company_number')}, {i.get('company_status')})" for i in same[:3])
    return "no company with the same name found on the register (final check is at submission)"


def new_job(text: str, uploads: list) -> str:
    o = parse_order(text)
    st = load_settings()
    job = "pending-" + uuid.uuid4().hex[:8]
    d = OUT / job
    (d / "documents").mkdir(parents=True)
    raw = _title(o["names"][0] if o["names"] else o.get("customer_name", ""))
    ending = st["name_ending"]
    mm = re.search(r"\s+(LIMITED|LTD\.?)$", raw)
    if mm:
        raw, ending = raw[:mm.start()], "LTD" if mm.group(1).startswith("LTD") else "LIMITED"
    ro = st
    m = {"job": job, "created": _now(), "order_text": text, "order": o, "status": "new",
         "company": {"name": raw, "ending": ending, "sic": o["sic_list"], "activity": o.get("activity", ""),
                     **{k: ro[k] for k in ("ro_building", "ro_street", "ro_town", "ro_region", "ro_postcode", "ro_country", "ro_jurisdiction")},
                     "shares_total": st["shares_total"], "share_value": st["share_value"], "share_class": st["share_class"],
                     "currency": st["currency"], "prescribed": st["prescribed"]},
         "directors": [dict(x) for x in o["directors_list"]], "docs": [dict(x) for x in o["docs"]],
         "mailbox": {}, "creds": {"onelogin_password": gen_password(), "gateway_password": gen_password(14)},
         "ack": [], "history": [f"{_now()} order pasted"]}
    for f in uploads:
        if not f or not f.filename:
            continue
        fn = re.sub(r"[^A-Za-z0-9._-]+", "_", f.filename)[-80:]
        f.save(d / "documents" / fn)
        hit = next((x for x in m["docs"] if not x.get("file") and _toks(x["name"]) == _toks(f.filename)), None)
        if hit:
            hit.update({"file": fn, "orig": f.filename})
        else:
            m["docs"].append({"name": f.filename, "url": "", "hint": "", "file": fn, "orig": f.filename})
    try:
        m["company"]["name_check"] = name_check(raw)
    except Exception as ex:
        m["company"]["name_check"] = ""
        m["history"].append(f"name search failed: {ex}")
    save_meta(d, m)
    write_outputs(d, m)
    _log({"job": job, "event": "new order", "company": raw})
    threading.Thread(target=process_job, args=(job,), daemon=True).start()
    return job


def rename_folder(d: Path, m: dict, email_addr: str) -> Path:
    """The folder is named after the company email once the mailbox exists."""
    target = OUT / email_addr.lower()
    if target.exists() and target != d:
        target = OUT / (email_addr.lower() + "-" + uuid.uuid4().hex[:4])
    d.rename(target)
    m["job"] = target.name
    save_meta(target, m)
    return target


# ============================================================================= routes

def all_jobs() -> list[dict]:
    out = []
    if OUT.is_dir():
        for d in OUT.iterdir():
            if d.is_dir() and (d / "meta.json").exists():
                try:
                    m = load_meta(d)
                    out.append({"job": d.name, "name": f"{m['company']['name']} {m['company'].get('ending', '')}",
                                "created": m.get("created", ""), "status": m.get("status", "new"),
                                "email": m.get("mailbox", {}).get("email", ""), "reviewed": m.get("reviewed"),
                                "red": len(unresolved_red(m)), "number": m.get("company_number", ""),
                                "mtime": (d / "meta.json").stat().st_mtime})
                except Exception:
                    pass
    return sorted(out, key=lambda x: -x["mtime"])


@bp.get("/formation")
def f_home():
    return render_template_string(HOME, jobs=all_jobs(), st=load_settings(), v=VERSION)


@bp.route("/formation/settings", methods=["GET", "POST"])
def f_settings():
    st = load_settings()
    if request.method == "POST":
        new = {k: request.form.get(k, "").strip() for k in SETTINGS_DEFAULT}
        if not new.get("cpanel_token"):
            new["cpanel_token"] = st.get("cpanel_token", "") if not os.environ.get("CPANEL_TOKEN") else ""
        OUT.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(new, indent=1))
        flash("Formation settings saved.")
        return redirect("/formation/settings")
    return render_template_string(SETTINGS_PAGE, st=st, env_token=bool(os.environ.get("CPANEL_TOKEN")))


@bp.post("/formation/settings/test")
def f_settings_test():
    try:
        st = load_settings()
        res = cpanel("list_pops", domain=st["mail_domain"])
        flash(f"cPanel OK – {len(res.get('data') or [])} mailboxes on {st['mail_domain']}.")
    except Exception as ex:
        flash(f"cPanel test failed: {ex}")
    return redirect("/formation/settings")


@bp.post("/formation/new")
def f_new():
    text = request.form.get("order_text", "")
    if not text.strip():
        flash("Paste the order text first.")
        return redirect("/formation")
    job = new_job(text, request.files.getlist("files"))
    return redirect(f"/formation/{job}")


@bp.get("/formation/<job>")
def f_job(job):
    d = _job_dir(job)
    m = load_meta(d)
    st = load_settings()
    flags = checks(m)
    ack = set(m.get("ack", []))
    files = sorted(p.name for p in d.iterdir() if p.is_file() and p.name not in ("meta.json",) and not p.name.endswith(".tmp"))
    docs_by = {x["file"]: x for x in m["docs"] if x.get("file")}
    return render_template_string(JOB, m=m, job=d.name, flags=flags, ack=ack, st=st, files=files, docs_by=docs_by,
                                  red=unresolved_red(m), share=share_split(m), doc=lambda f: _doc(m, f))


@bp.post("/formation/<job>/save")
def f_save(job):
    d = _job_dir(job)
    m = load_meta(d)
    f = request.form
    c = m["company"]
    c["name"] = _title(f.get("c_name", c["name"]))
    c["ending"] = f.get("c_ending", c.get("ending", "LIMITED"))
    for k in ("ro_building", "ro_street", "ro_town", "ro_region", "ro_postcode", "ro_country", "ro_jurisdiction",
              "shares_total", "share_value", "share_class", "currency"):
        if k in f:
            c[k] = f.get(k, "").strip()
    sic = []
    for i in range(4):
        code = re.sub(r"\D", "", f.get(f"sic{i}", ""))
        if code:
            sic.append({"code": code, "desc": f.get(f"sicd{i}", "").strip()})
    c["sic"] = sic
    FIELDS = ("title", "first_name", "middle_names", "surname", "dob", "nationality", "residence_country", "occupation",
              "personal_code", "town_of_birth", "passport_no", "father_first")
    for i, dd in enumerate(m["directors"]):
        dd["share_pct"] = f.get(f"d{i}_share_pct", dd.get("share_pct", "")).strip()
        if f.get(f"d{i}_addr_choice") in ("passport", "poa", "manual"):
            dd["addr_choice"] = f.get(f"d{i}_addr_choice")
        before = {k: dd.get(k, "") for k in FIELDS}
        before.update({"res." + k: (dd.get("res") or {}).get(k, "") for k in ("premises", "street", "locality", "town", "region", "postcode", "country")})
        edit = dict(dd.get("edit") or {})
        for k in list(before):
            key = f"d{i}_{k.replace('.', '_')}"
            if key in f:
                val = f.get(key, "").strip()
                if k != "dob" and not k.startswith("res.country") and k not in ("nationality", "residence_country", "occupation"):
                    val = _title(val) if k not in ("personal_code",) else val.upper()
                if val != before[k]:
                    edit[k] = val
        if f.get(f"d{i}_addr_choice") in ("passport", "poa"):      # picking a document address clears typed address edits
            edit = {k: v for k, v in edit.items() if not k.startswith("res.")}
        dd["edit"] = edit
    derive(m)
    m["history"].append(f"{_now()} review page saved")
    save_meta(d, m)
    write_outputs(d, m)
    flash("Saved – the pack has been rebuilt.")
    return redirect(f"/formation/{d.name}#review")


@bp.post("/formation/<job>/reset/<int:i>")
def f_reset(job, i):
    d = _job_dir(job)
    m = load_meta(d)
    if 0 <= i < len(m["directors"]):
        m["directors"][i]["edit"] = {}
        m["directors"][i].pop("addr_choice", None)
        derive(m)
        save_meta(d, m)
        write_outputs(d, m)
        flash("Typed corrections removed – values are back to what the documents say.")
    return redirect(f"/formation/{d.name}#review")


@bp.post("/formation/<job>/ack")
def f_ack(job):
    d = _job_dir(job)
    m = load_meta(d)
    m["ack"] = request.form.getlist("ack")
    m["ack_note"] = request.form.get("ack_note", "").strip()
    m["history"].append(f"{_now()} flags settled: {len(m['ack'])} ({m['ack_note']})")
    save_meta(d, m)
    return redirect(f"/formation/{d.name}#flags")


@bp.post("/formation/<job>/codes")
def f_codes(job):
    d = _job_dir(job)
    m = load_meta(d)
    found = parse_personal_codes(request.form.get("codes_text", ""))
    if not found:
        flash("No personal code (format ABC-1234-ABCD) found in the pasted text.")
        return redirect(f"/formation/{d.name}#codes")
    msgs = apply_codes(m, found)
    derive(m)
    m["history"].append(f"{_now()} personal codes: " + " | ".join(msgs))
    save_meta(d, m)
    write_outputs(d, m)
    flash("Personal codes: " + " | ".join(msgs))
    return redirect(f"/formation/{d.name}#codes")


@bp.post("/formation/<job>/mailbox")
def f_mailbox(job):
    d = _job_dir(job)
    m = load_meta(d)
    if m.get("mailbox", {}).get("email") and request.form.get("action") != "manual":
        flash("This company already has a mailbox.")
        return redirect(f"/formation/{d.name}#mail")
    try:
        if request.form.get("action") == "manual":
            em, pw = request.form.get("email", "").strip().lower(), request.form.get("password", "").strip()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", em) or not pw:
                raise RuntimeError("enter a full email address and its password")
        else:
            em, pw = create_mailbox(m["company"]["name"], request.form.get("local", "").strip())
        m["mailbox"] = {"email": em, "password": pw, "created": _now(),
                        "how": "manual" if request.form.get("action") == "manual" else "cPanel"}
        m["history"].append(f"{_now()} mailbox {em} ({m['mailbox']['how']})")
        save_meta(d, m)
        if d.name.startswith("pending-"):
            d = rename_folder(d, m, em)
        write_outputs(d, m)
        _log({"job": d.name, "event": "mailbox", "email": em})
        flash(f"Mailbox ready: {em}. The folder is now named after it.")
    except Exception as ex:
        flash(f"Mailbox not created: {ex}")
    return redirect(f"/formation/{d.name}#mail")


@bp.get("/formation/<job>/inbox")
def f_inbox(job):
    d = _job_dir(job)
    mb = load_meta(d).get("mailbox", {})
    if not mb.get("email"):
        return jsonify({"error": "no mailbox yet"})
    try:
        return jsonify({"items": read_inbox(mb["email"], mb["password"])})
    except Exception as ex:
        return jsonify({"error": f"could not read the inbox: {ex}"})


@bp.post("/formation/<job>/creds")
def f_creds(job):
    d = _job_dir(job)
    m = load_meta(d)
    cr = m.setdefault("creds", {})
    try:
        for k in ("onelogin_totp", "gateway_totp"):
            v = request.form.get(k, "").strip()
            if v:
                cr[k] = parse_secret(v)
        for k in ("onelogin_password", "gateway_id", "gateway_password"):
            if k in request.form:
                cr[k] = request.form.get(k, "").strip()
        m["history"].append(f"{_now()} sign-in details updated")
        save_meta(d, m)
        write_outputs(d, m)
        flash("Sign-in details saved.")
    except ValueError as ex:
        flash(str(ex))
    return redirect(f"/formation/{d.name}#signin")


@bp.get("/formation/<job>/totp/<which>")
def f_totp(job, which):
    d = _job_dir(job)
    sec = load_meta(d).get("creds", {}).get({"onelogin": "onelogin_totp", "gateway": "gateway_totp"}.get(which, ""), "")
    if not sec:
        return jsonify({"error": "no secret saved"})
    code, left = totp(sec)
    nxt, _ = totp(sec, time.time() + 30)
    return jsonify({"code": code, "left": left, "next": nxt})


@bp.post("/formation/<job>/reviewed")
def f_reviewed(job):
    d = _job_dir(job)
    m = load_meta(d)
    if request.form.get("undo"):
        m["reviewed"] = False
        m["status"] = "new"
    else:
        red = unresolved_red(m)
        who = request.form.get("who", "").strip()
        if red:
            flash(f"{len(red)} red flag(s) still open – fix them or tick them as settled first.")
            return redirect(f"/formation/{d.name}#flags")
        if not who:
            flash("Type your name as reviewer.")
            return redirect(f"/formation/{d.name}#flags")
        m.update({"reviewed": True, "reviewed_by": who, "reviewed_at": _now(), "status": "ready"})
    m["history"].append(f"{_now()} reviewed={m['reviewed']} {m.get('reviewed_by', '')}")
    save_meta(d, m)
    write_outputs(d, m)
    return redirect(f"/formation/{d.name}")


@bp.post("/formation/<job>/filed")
def f_filed(job):
    d = _job_dir(job)
    m = load_meta(d)
    for k in ("company_number", "auth_code", "filed_date", "filed_by", "submission_ref"):
        m[k] = request.form.get(k, "").strip()
    m["status"] = "incorporated" if m.get("company_number") else ("submitted" if m.get("filed_date") else m.get("status"))
    m["history"].append(f"{_now()} filing recorded: {m['status']} {m.get('company_number', '')}")
    save_meta(d, m)
    write_outputs(d, m)
    flash("Saved.")
    return redirect(f"/formation/{d.name}#filed")


@bp.post("/formation/<job>/upload")
def f_upload(job):
    d = _job_dir(job)
    m = load_meta(d)
    n = 0
    for f in request.files.getlist("files"):
        if f and f.filename:
            fn = re.sub(r"[^A-Za-z0-9._-]+", "_", f.filename)[-80:]
            f.save(d / "documents" / fn)
            hit = next((x for x in m["docs"] if not x.get("file") and _toks(x["name"]) == _toks(f.filename)), None)
            if hit:
                hit.update({"file": fn, "orig": f.filename})
                hit.pop("error", None)
            else:
                m["docs"].append({"name": f.filename, "url": "", "hint": request.form.get("hint", ""), "file": fn, "orig": f.filename})
            n += 1
    save_meta(d, m)
    threading.Thread(target=process_job, args=(d.name,), daemon=True).start()
    flash(f"{n} file(s) added – reading them now.")
    return redirect(f"/formation/{d.name}")


@bp.post("/formation/<job>/rescan")
def f_rescan(job):
    d = _job_dir(job)
    m = load_meta(d)
    for x in m["docs"]:
        x.pop("scan", None)
    save_meta(d, m)
    threading.Thread(target=process_job, args=(d.name,), daemon=True).start()
    return redirect(f"/formation/{d.name}")


@bp.get("/formation/<job>/status")
def f_status(job):
    m = load_meta(_job_dir(job))
    return jsonify({"state": m.get("scan_state"), "msg": m.get("scan_msg")})


@bp.get("/formation/<job>/pack")
def f_pack(job):
    d = _job_dir(job)
    m = load_meta(d)
    return render_template_string(PACK, m=m, job=d.name, secs=build_pack(m), red=unresolved_red(m))


@bp.get("/formation/<job>/file/<path:name>")
def f_file(job, name):
    d = _job_dir(job)
    p = (d / name).resolve()
    if not str(p).startswith(str(d.resolve()) + os.sep) or not p.is_file() or p.name == "meta.json":
        abort(404)
    return send_file(p, as_attachment=request.args.get("dl") == "1")


@bp.get("/formation/<job>/zip")
def f_zip(job):
    d = _job_dir(job)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in d.rglob("*"):
            if p.is_file() and p.name not in ("meta.json",) and not p.name.endswith(".tmp"):
                z.write(p, f"{d.name}/{p.relative_to(d)}")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{d.name}.zip", mimetype="application/zip")


def register(app, cfg_fn=None):
    _CFG["fn"] = cfg_fn
    OUT.mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)

    @app.after_request
    def _formations_menu(resp):          # add "Formations" to the top menu without editing app.py's header
        try:
            if resp.mimetype == "text/html" and not resp.direct_passthrough:
                body = resp.get_data(as_text=True)
                anchor = '<a href="/forms">Company forms</a>'
                if anchor in body and 'href="/formation"' not in body:
                    resp.set_data(body.replace(anchor, anchor + '<a href="/formation">Formations</a>', 1))
        except Exception:
            pass
        return resp


# ============================================================================= pages

CSS = """<style>
.fx .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:4px 14px}
.fx label{font-size:12px;color:#555;font-weight:700;display:block;margin-top:6px}
.fx input[type=text],.fx input[type=date],.fx select,.fx textarea{width:100%;padding:7px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;font-family:inherit}
.fx .red{background:#fee2e2;border:1px solid #fca5a5;color:#7f1d1d;padding:8px 12px;border-radius:8px;margin:6px 0}
.fx .amber{background:#fef9c3;border:1px solid #fde047;padding:8px 12px;border-radius:8px;margin:6px 0}
.fx .green{background:#e8f7ee;border:1px solid #86efac;padding:8px 12px;border-radius:8px;margin:6px 0}
.fx .done{opacity:.55;text-decoration:line-through}
.fx .step{display:inline-flex;width:26px;height:26px;border-radius:50%;background:#111;color:#fff;font-size:13px;align-items:center;justify-content:center;margin-right:8px}
.fx .pill{display:inline-block;border-radius:10px;padding:2px 9px;font-size:12px;font-weight:700}
.fx .p-new{background:#e5e7eb}.fx .p-ready{background:#dcfce7;color:#166534}.fx .p-incorporated{background:#dbeafe;color:#1e40af}.fx .p-submitted{background:#fef9c3}
.fx table.t{width:100%;border-collapse:collapse}.fx table.t td,.fx table.t th{border-bottom:1px solid #eee;padding:6px;text-align:left;vertical-align:top;font-size:14px}
.fx .code{font:700 34px/1 ui-monospace,monospace;letter-spacing:4px}
.fx .muted{color:#666;font-size:13px}
.fx .dir{border:1px solid #e5e7eb;border-radius:10px;padding:10px 14px;margin:10px 0;background:#fff}
.fx .edited{background:#fff7ed!important;border-color:#fdba74!important}
</style>"""

HOME = r"""{% extends "base" %}{% block body %}""" + CSS + r"""<div class="fx">
<div class="card"><h1>UK company formations <span class="muted">{{v}}</span></h1>
<p class="muted">Paste a new-company order from the dashboard. The system downloads the passports and proofs of address,
reads them, checks everything, creates the company mailbox and builds a Companies House copy-paste pack.
<a href="/formation/settings">Settings (mail server, defaults)</a>{% if not st.mail_domain %} – <b style="color:#b91c1c">mail domain not set yet</b>{% endif %}</p>
<form method="post" action="/formation/new" enctype="multipart/form-data">
<label>Order text (copy everything from “Customer Name” to the shareholding)</label>
<textarea name="order_text" rows="12" placeholder="Customer Name&#10;KADASSERI SANU BUSINESS SOLUTIONS&#10;Email&#10;..."></textarea>
<label>Files (only if the links in the order cannot be downloaded) – passports and proofs of address</label>
<input type="file" name="files" multiple>
<p><button class="btn">Create formation job</button></p></form></div>
<div class="card"><h2>Jobs (folder = company email)</h2>
<table class="t"><tr><th>Company</th><th>Folder / email</th><th>Status</th><th>Open flags</th><th>Created</th></tr>
{% for j in jobs %}<tr><td><a href="/formation/{{j.job}}">{{j.name}}</a>{% if j.number %} · {{j.number}}{% endif %}</td><td>{{j.job}}</td>
<td><span class="pill p-{{j.status}}">{{j.status}}</span>{% if j.reviewed %} ✓ reviewed{% endif %}</td>
<td>{% if j.red %}<b style="color:#b91c1c">{{j.red}} red</b>{% else %}–{% endif %}</td><td>{{j.created}}</td></tr>
{% else %}<tr><td colspan=5 class="muted">No formations yet.</td></tr>{% endfor %}</table></div></div>
{% endblock %}"""

SETTINGS_PAGE = r"""{% extends "base" %}{% block body %}""" + CSS + r"""<div class="fx"><div class="card"><h1>Formation settings</h1>
<form method="post"><h3>Company mailboxes (cPanel)</h3><div class="grid">
<div><label>Mail domain (mailboxes are created as name@domain)</label><input type="text" name="mail_domain" value="{{st.mail_domain}}"></div>
<div><label>cPanel host (e.g. server123.web-hosting.com)</label><input type="text" name="cpanel_host" value="{{st.cpanel_host}}"></div>
<div><label>cPanel username</label><input type="text" name="cpanel_user" value="{{st.cpanel_user}}"></div>
<div><label>cPanel API token {% if env_token %}(set in Railway – leave blank){% elif st.cpanel_token %}(saved – leave blank to keep){% endif %}</label><input type="text" name="cpanel_token" value="" autocomplete="off"></div>
<div><label>Webmail link (blank = https://webmail.&lt;domain&gt;)</label><input type="text" name="webmail_url" value="{{st.webmail_url}}"></div>
<div><label>IMAP server (blank = mail.&lt;domain&gt;)</label><input type="text" name="imap_host" value="{{st.imap_host}}"></div>
<div><label>Mailbox size (MB)</label><input type="text" name="mail_quota_mb" value="{{st.mail_quota_mb}}"></div></div>
<h3>Defaults for every company</h3><div class="grid">
<div><label>Name ending</label><select name="name_ending">{% for e in ["LIMITED","LTD"] %}<option {{'selected' if st.name_ending==e}}>{{e}}</option>{% endfor %}</select></div>
<div><label>Director occupation</label><input type="text" name="occupation" value="{{st.occupation}}"></div>
<div><label>Total shares</label><input type="text" name="shares_total" value="{{st.shares_total}}"></div>
<div><label>Value per share (£)</label><input type="text" name="share_value" value="{{st.share_value}}"></div>
<div><label>Share class</label><input type="text" name="share_class" value="{{st.share_class}}"></div>
<div><label>Currency</label><input type="text" name="currency" value="{{st.currency}}"></div>
<div><label>Registered office – building</label><input type="text" name="ro_building" value="{{st.ro_building}}"></div>
<div><label>Registered office – street</label><input type="text" name="ro_street" value="{{st.ro_street}}"></div>
<div><label>Town</label><input type="text" name="ro_town" value="{{st.ro_town}}"></div>
<div><label>County / region</label><input type="text" name="ro_region" value="{{st.ro_region}}"></div>
<div><label>Postcode</label><input type="text" name="ro_postcode" value="{{st.ro_postcode}}"></div>
<div><label>Country</label><input type="text" name="ro_country" value="{{st.ro_country}}"></div>
<div><label>Jurisdiction</label><input type="text" name="ro_jurisdiction" value="{{st.ro_jurisdiction}}"></div></div>
<label>Prescribed particulars (share rights)</label><textarea name="prescribed" rows="3">{{st.prescribed}}</textarea>
<p><button class="btn">Save settings</button></p></form>
<form method="post" action="/formation/settings/test"><button class="btn" style="background:#475569">Test cPanel connection</button></form>
</div></div>{% endblock %}"""

JOB = r"""{% extends "base" %}{% block body %}""" + CSS + r"""<div class="fx">
<div class="card"><h1>{{m.company.name}} {{m.company.ending}} <span class="pill p-{{m.status}}">{{m.status}}</span></h1>
<p class="muted">Folder: <b>{{job}}</b> · created {{m.created}} · customer {{m.order.customer_name}} &lt;{{m.order.customer_email}}&gt; {{m.order.phone}}</p>
{% if m.scan_state == 'running' %}<div class="amber" id="scanbox">⏳ <span id="scanmsg">{{m.scan_msg}}</span></div>
<script>setInterval(()=>fetch('/formation/{{job}}/status').then(r=>r.json()).then(j=>{if(j.state!='running')location.reload();else document.getElementById('scanmsg').textContent=j.msg}),3000)</script>
{% elif m.scan_msg %}<p class="muted">{{m.scan_msg}}</p>{% endif %}
<p><a class="btn" href="/formation/{{job}}/pack" target="_blank" style="background:{{'#15803d' if m.reviewed else '#64748b'}}">📋 Open copy-paste pack (for data entry)</a>
<a class="btn" href="/formation/{{job}}/file/CH_filing_pack.pdf" target="_blank" style="background:#475569">PDF pack</a>
<a class="btn" href="/formation/{{job}}/zip" style="background:#475569">⬇ Folder ZIP</a></p>
{% if not m.reviewed %}<div class="red"><b>Not reviewed yet.</b> A reviewer settles the red items below once; after that anyone can file from the pack.</div>
{% else %}<div class="green">✓ Reviewed by {{m.reviewed_by}} on {{m.reviewed_at}} – ready to file.</div>{% endif %}</div>

<div class="card" id="flags"><h2><span class="step">1</span>Checks</h2>
<form method="post" action="/formation/{{job}}/ack">
{% for f in flags %}<div class="{{f.level}} {{'done' if f.key in ack}}">{% if f.level=='red' %}<label style="display:inline;font-size:14px;color:inherit"><input type="checkbox" name="ack" value="{{f.key}}" {{'checked' if f.key in ack}}> settled</label> · {% endif %}{{f.text}}</div>
{% else %}<div class="green">No problems found.</div>{% endfor %}
<label>Note on how red items were settled (e.g. “customer confirmed lives in Jeddah”)</label><input type="text" name="ack_note" value="{{m.ack_note or ''}}">
<p><button class="btn" style="background:#475569">Save settled ticks</button></p></form>
<form method="post" action="/formation/{{job}}/reviewed">{% if m.reviewed %}<input type="hidden" name="undo" value="1"><button class="btn" style="background:#b91c1c">Undo “reviewed”</button>
{% else %}<label>Reviewer name</label><input type="text" name="who" style="max-width:260px"> <button class="btn" {{'disabled' if red}}>✓ Mark reviewed – ready to file</button>{% if red %} <span class="muted">{{red|length}} red item(s) open</span>{% endif %}{% endif %}</form></div>

<div class="card" id="codes"><h2><span class="step">2</span>Companies House personal codes</h2>
<p class="muted">Paste the “STARTITUP ACSP has told Companies House they have verified your identity” email(s) or the WhatsApp copy – several at once is fine.
The code goes to the right director and the <b>verified last name</b> is used in the filing so the code is accepted.</p>
<table class="t">{% for d in m.directors %}<tr><td>{{d.full_name}}</td><td><b>{{d.personal_code or '—'}}</b></td><td class="muted">{% if d.verified_last_name %}verified last name: {{d.verified_last_name}}{% endif %}</td></tr>{% endfor %}</table>
<form method="post" action="/formation/{{job}}/codes"><textarea name="codes_text" rows="4" placeholder="Your last name: Abdul Gafoor&#10;Your Companies House personal code: CCH-2L4N-2223"></textarea>
<p><button class="btn">Read codes</button></p></form></div>

<div class="card" id="mail"><h2><span class="step">3</span>Company email (registered email address + One Login)</h2>
{% if m.mailbox.email %}<div class="green">📧 <b>{{m.mailbox.email}}</b> · password <code>{{m.mailbox.password}}</code> · <a href="{{st.webmail_url}}" target="_blank">open webmail</a> ({{m.mailbox.how}}, {{m.mailbox.created}})</div>
<p><button class="btn" onclick="inbox();return false">📥 Get latest codes from the inbox</button> <span class="muted">reads the newest emails (GOV.UK, Government Gateway) and picks out the security code</span></p>
<div id="inbox"></div>
<script>function inbox(){const b=document.getElementById('inbox');b.innerHTML='Reading inbox…';fetch('/formation/{{job}}/inbox').then(r=>r.json()).then(j=>{if(j.error){b.innerHTML='<div class=red>'+j.error+'</div>';return}
if(!j.items.length){b.innerHTML='<div class=amber>Inbox is empty – wait a minute and try again.</div>';return}
b.innerHTML='<table class=t>'+j.items.map(x=>'<tr><td>'+(x.code?'<span class=code onclick="navigator.clipboard.writeText(\''+x.code+'\')" title="click to copy">'+x.code+'</span>':'')+'</td><td><b>'+esc(x.subject)+'</b><br><span class=muted>'+esc(x.from)+' · '+esc(x.date)+'</span><br><span class=muted>'+esc(x.snippet.slice(0,220))+'</span></td></tr>').join('')+'</table>'})}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}</script>
{% else %}
<form method="post" action="/formation/{{job}}/mailbox"><input type="hidden" name="action" value="create">
<div class="grid"><div><label>Mailbox name (blank = from company name)</label><input type="text" name="local" placeholder="kadasserisanu"></div>
<div><label>Domain</label><input type="text" value="@{{st.mail_domain or 'set in settings'}}" disabled></div></div>
<p><button class="btn" {{'disabled' if not st.mail_domain}}>Create mailbox on our server</button></p></form>
<details><summary class="muted">…or record a mailbox made by hand</summary><form method="post" action="/formation/{{job}}/mailbox"><input type="hidden" name="action" value="manual">
<div class="grid"><div><label>Email</label><input type="text" name="email"></div><div><label>Password</label><input type="text" name="password"></div></div><p><button class="btn" style="background:#475569">Save</button></p></form></details>
{% endif %}</div>

<div class="card" id="signin"><h2><span class="step">4</span>Sign-in details &amp; authenticator codes (no phone needed)</h2>
<p class="muted">When GOV.UK One Login / Government Gateway asks how to get security codes, choose <b>Authenticator app</b>, click “I cannot scan the QR code”,
copy the <b>secret key</b> and paste it below – the live 6-digit code then shows here for anyone in the team.</p>
<form method="post" action="/formation/{{job}}/creds"><div class="grid">
<div><label>GOV.UK One Login password</label><input type="text" name="onelogin_password" value="{{m.creds.onelogin_password}}"></div>
<div><label>One Login authenticator secret {% if m.creds.onelogin_totp %}(saved){% endif %}</label><input type="text" name="onelogin_totp" placeholder="paste secret key"></div>
<div><label>Government Gateway user ID</label><input type="text" name="gateway_id" value="{{m.creds.gateway_id or ''}}"></div>
<div><label>Government Gateway password</label><input type="text" name="gateway_password" value="{{m.creds.gateway_password}}"></div>
<div><label>Gateway authenticator secret {% if m.creds.gateway_totp %}(saved){% endif %}</label><input type="text" name="gateway_totp" placeholder="paste secret key"></div></div>
<p><button class="btn">Save sign-in details</button></p></form>
<div class="grid">{% for w,lbl in [('onelogin','GOV.UK One Login'),('gateway','Government Gateway')] %}{% if m.creds[w+'_totp'] %}
<div class="dir"><b>{{lbl}}</b><div class="code" id="c-{{w}}" onclick="navigator.clipboard.writeText(this.textContent)" title="click to copy">······</div><span class="muted" id="l-{{w}}"></span></div>{% endif %}{% endfor %}</div>
<script>function tick(w){fetch('/formation/{{job}}/totp/'+w).then(r=>r.json()).then(j=>{if(j.code){document.getElementById('c-'+w).textContent=j.code;document.getElementById('l-'+w).textContent='changes in '+j.left+'s · next '+j.next}})}
{% for w in ['onelogin','gateway'] %}{% if m.creds[w+'_totp'] %}tick('{{w}}');setInterval(()=>tick('{{w}}'),1000*5);{% endif %}{% endfor %}</script></div>

<div class="card" id="review"><h2>Review &amp; correct (reviewer only)</h2>
<form method="post" action="/formation/{{job}}/save">
<div class="grid"><div><label>Company name (without ending)</label><input type="text" name="c_name" value="{{m.company.name}}"></div>
<div><label>Ending</label><select name="c_ending">{% for e in ["LIMITED","LTD"] %}<option {{'selected' if m.company.ending==e}}>{{e}}</option>{% endfor %}</select></div>
<div><label>Total shares</label><input type="text" name="shares_total" value="{{m.company.shares_total}}"></div>
<div><label>Value per share (£)</label><input type="text" name="share_value" value="{{m.company.share_value}}"></div></div>
<div class="grid">{% for i in range(4) %}{% set s = m.company.sic[i] if m.company.sic|length > i else {} %}
<div><label>SIC {{i+1}}</label><input type="text" name="sic{{i}}" value="{{s.code or ''}}"><input type="text" name="sicd{{i}}" value="{{s.desc or ''}}" placeholder="description"></div>{% endfor %}</div>
<p class="muted">Registered office: {{m.company.ro_building}}, {{m.company.ro_street}}, {{m.company.ro_town}} {{m.company.ro_postcode}} ({{m.company.ro_jurisdiction}}). Order said: {{m.order.ro}}</p>
{% for d in m.directors %}{% set i = loop.index0 %}{% set e = d.edit or {} %}<div class="dir"><h3>Director {{loop.index}}: {{d.full_name}} <span class="muted">({{d.share_pct}}% · order residency {{d.residency}} · order nationality {{d.nationality_order}})</span></h3>
<p class="muted">Passport: {% if d.passport_doc %}<a href="/formation/{{job}}/file/documents/{{d.passport_doc}}" target="_blank">{{d.passport_doc}}</a>{% else %}<b>none</b>{% endif %} ·
Proof of address: {% if d.poa_doc %}<a href="/formation/{{job}}/file/documents/{{d.poa_doc}}" target="_blank">{{d.poa_doc}}</a>{% else %}<b>none</b>{% endif %}</p>
{% set pa = (doc(d.passport_doc).address or {}) %}{% set qa = (doc(d.poa_doc).address or {}) %}
<label>Usual residential address – which one?</label>
<div style="font-size:14px">{% if pa.country %}<label style="display:block;font-weight:400;color:#111"><input type="radio" name="d{{i}}_addr_choice" value="passport" {{'checked' if (d.addr_choice or d.addr_auto)=='passport'}}> Passport address: {{[pa.premises,pa.street,pa.locality,pa.town,pa.region,pa.postcode,pa.country]|select|join(', ')}}</label>{% endif %}
{% if qa.country %}<label style="display:block;font-weight:400;color:#111"><input type="radio" name="d{{i}}_addr_choice" value="poa" {{'checked' if (d.addr_choice or d.addr_auto)=='poa'}}> Proof-of-address document: {{[qa.premises,qa.street,qa.locality,qa.town,qa.region,qa.postcode,qa.country]|select|join(', ')}}</label>{% endif %}
<label style="display:block;font-weight:400;color:#111"><input type="radio" name="d{{i}}_addr_choice" value="manual" {{'checked' if d.addr_choice=='manual'}}> Typed below</label></div>
<div class="grid">{% for k,lbl in [('title','Title'),('first_name','First name'),('middle_names','Middle name(s)'),('surname','Last name'),('dob','Date of birth (YYYY-MM-DD)'),('nationality','Nationality'),('residence_country','Country of residence'),('occupation','Occupation'),('personal_code','Personal code'),('town_of_birth','Town of birth'),('passport_no','Passport no.'),('father_first',"Father's first name")] %}
<div><label>{{lbl}}</label><input type="text" name="d{{i}}_{{k}}" value="{{d[k] or ''}}" class="{{'edited' if k in e}}"></div>{% endfor %}
<div><label>Shareholding %</label><input type="text" name="d{{i}}_share_pct" value="{{d.share_pct}}"></div></div>
<div class="grid">{% for k,lbl in [('premises','Home – property name/no.'),('street','Home – street'),('locality','Home – area'),('town','Home – town'),('region','Home – state/region'),('postcode','Home – postcode'),('country','Home – country')] %}
<div><label>{{lbl}}</label><input type="text" name="d{{i}}_res_{{k}}" value="{{(d.res or {})[k] or ''}}" class="{{'edited' if ('res.'+k) in e}}"></div>{% endfor %}</div>
{% if e %}<p class="muted">Orange = typed by reviewer (overrides the documents). <button class="btn" style="background:#475569;padding:3px 10px" formaction="/formation/{{job}}/reset/{{i}}">Remove typed corrections</button></p>{% endif %}
</div>{% endfor %}
<p><button class="btn">Save and rebuild pack</button></p></form></div>

<div class="card"><h2>Documents</h2><table class="t">{% for x in m.docs %}<tr><td>{% if x.file %}<a href="/formation/{{job}}/file/documents/{{x.file}}" target="_blank">{{x.file}}</a>{% else %}{{x.name}}{% endif %}</td>
<td>{{(x.scan or {}).doc_kind or x.hint}}</td><td>{{x.director or '<b style="color:#b91c1c">not matched</b>'|safe}}</td><td class="muted">{{x.error or ''}}{{(x.scan or {}).notes or ''}}</td></tr>{% endfor %}</table>
<form method="post" action="/formation/{{job}}/upload" enctype="multipart/form-data" style="margin-top:8px"><input type="file" name="files" multiple> <button class="btn" style="background:#475569">Add files</button></form>
<form method="post" action="/formation/{{job}}/rescan" style="margin-top:6px"><button class="btn" style="background:#475569">Read all documents again</button></form>
<h3>Folder contents ({{job}})</h3><p class="muted">{% for f in files %}<a href="/formation/{{job}}/file/{{f}}" target="_blank">{{f}}</a> · {% endfor %}documents/</p></div>

<div class="card" id="filed"><h2><span class="step">5</span>After filing</h2><form method="post" action="/formation/{{job}}/filed"><div class="grid">
<div><label>Submitted on</label><input type="text" name="filed_date" value="{{m.filed_date or ''}}"></div><div><label>Submitted by</label><input type="text" name="filed_by" value="{{m.filed_by or ''}}"></div>
<div><label>Submission reference</label><input type="text" name="submission_ref" value="{{m.submission_ref or ''}}"></div>
<div><label>Company number</label><input type="text" name="company_number" value="{{m.company_number or ''}}"></div>
<div><label>Companies House authentication code</label><input type="text" name="auth_code" value="{{m.auth_code or ''}}"></div></div>
<p><button class="btn">Save</button></p></form>
<details><summary class="muted">History</summary><pre style="white-space:pre-wrap;font-size:12px">{{m.history|join('\n')}}</pre></details></div>
</div>{% endblock %}"""

PACK = r"""<!doctype html><html><head><meta charset="utf-8"><title>CH pack – {{m.company.name}}</title>
<style>body{font-family:system-ui,sans-serif;margin:0;background:#f4f4f5;color:#111}main{max-width:900px;margin:0 auto;padding:16px}
h1{font-size:20px}h2{font-size:16px;margin:0 0 8px}.sec{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;margin:12px 0}
.sec.ok{opacity:.5}.row{display:grid;grid-template-columns:38% 1fr;gap:10px;padding:7px 0;border-bottom:1px solid #f1f1f1;align-items:center}
.k{color:#555;font-size:14px}.v{font:600 16px ui-monospace,monospace;cursor:pointer;padding:5px 8px;border-radius:6px;background:#f8fafc;border:1px dashed #cbd5e1;word-break:break-word}
.v:hover{background:#e0f2fe}.v.copied{background:#dcfce7;border-color:#22c55e}.v.empty{color:#aaa;font-weight:400;cursor:default;border-style:none;background:none}
.v.warn{background:#fee2e2;color:#991b1b;border-color:#fca5a5}.note{font-size:13px;color:#666;margin-top:6px}.d{font-size:12px;color:#888;font-weight:400}
.ban{padding:10px 14px;border-radius:8px;margin:8px 0}.r{background:#fee2e2;color:#7f1d1d}.g{background:#dcfce7}
.chk{float:right;font-size:13px}</style></head><body><main>
<h1>Companies House filing pack – {{m.company.name}} {{m.company.ending}}</h1>
<p>Company email / folder: <b>{{m.mailbox.email or job}}</b>. Click any value to copy it, then paste it into the matching Companies House box. Leave empty rows empty. Tick each section when done.</p>
{% if not m.reviewed %}<div class="ban r"><b>STOP – not reviewed yet.</b> Do not file until a reviewer marks this job “reviewed”{% if red %} ({{red|length}} red item(s) open){% endif %}.</div>
{% else %}<div class="ban g">✓ Reviewed by {{m.reviewed_by}} – go ahead.</div>{% endif %}
{% for s in secs %}<div class="sec"><label class="chk"><input type="checkbox" onchange="this.closest('.sec').classList.toggle('ok',this.checked)"> done</label><h2>{{s.title}}</h2>
{% for r in s.rows %}{% set v = r[1]|string %}<div class="row"><div class="k">{{r[0]}}</div>
<div class="v {{'empty' if not v}} {{'warn' if '⚠' in v}}" {% if v and '⚠' not in v %}onclick="cp(this)"{% endif %}>{{v or '(leave empty)'}}{% if r|length > 2 and r[2] %} <span class="d">{{r[2]}}</span>{% endif %}</div></div>{% endfor %}
{% if s.note %}<div class="note">{{s.note}}</div>{% endif %}</div>{% endfor %}
<script>function cp(el){const t=el.childNodes[0].textContent.trim();navigator.clipboard.writeText(t);el.classList.add('copied');setTimeout(()=>el.classList.remove('copied'),1200)}</script>
</main></body></html>"""
