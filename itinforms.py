"""
ITIN tab (v32a) – IRS Form W-7 + Form W-7 (COA) Certificate of Accuracy, prepared by Startitup Global Inc as a
Certifying Acceptance Agent (CAA).

Self-contained add-on: app.py only imports this file and calls register(app, load_config).
Nothing in the mail-sorting or Company forms tabs is touched.

  /itin                      -> upload passport, AI pre-fills, staff complete the W-7, pick reason boxes, generate
  /itin/scan     (POST)      -> JSON: details read from the passport scan (AI vision)
  /itin/generate (POST)      -> fills fw7.pdf (+ fw7coa.pdf), stores them under DATA/itin/<job>/
  /itin/job/<job>            -> preview + download one by one / all as ZIP
  /itin/caa      (POST)      -> saves our Acceptance Agent details (name, title, EIN, PTIN, office code ...)

Templates live in ./itin_templates (official IRS PDFs, used exactly as published – only fields are filled):
  fw7.pdf, fw7coa.pdf
"""
from __future__ import annotations

import base64, datetime as dt, io, json, os, re, threading, uuid, zipfile
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, render_template_string, request, send_file, session

ROOT = Path(__file__).parent
TPL_DIR = ROOT / "itin_templates"
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "itin"
LOG_FILE = OUT / "log.json"
CAA_FILE = OUT / "caa.json"
_LOCK = threading.Lock()
_CFG = {"fn": None}

# Our Acceptance Agent details – defaults, editable (and saved) on the ITIN page.
CAA_DEFAULT = {"responsible_party": "Moaz Ali Siddiqui", "name_title": "Moaz Ali Siddiqui, COO",
               "company": "Startitup Global Inc", "ein": "42-2709229", "ptin": "P03481333", "ptin_expiry": "2026-12-31",
               "office_code": "00000001", "agreement_date": "2026-09-29", "phone": "406-605-9190", "fax": "806-615-2858"}
# Source: CAA agreement Sec. 3.02 (EIN 422709229), AA approval letter 5614C (07 Oct 2026, responsible party),
# PTIN confirmation (Moaz Siddiqui P03481333). Office code 00000001 (Active, effective 29/09/2026) from the AA
# Application Summary on the Registered User Portal (tracking 20260729144741339273, Customer ID 15331555).

REASONS = {   # W-7 "Reason you're submitting Form W-7" boxes
    "a": "Nonresident alien required to get ITIN to claim tax treaty benefit",
    "b": "Nonresident alien filing a U.S. federal tax return",
    "c": "U.S. resident alien (based on days present in the United States) filing a U.S. federal tax return",
    "d": "Dependent of U.S. citizen/resident alien",
    "e": "Spouse of U.S. citizen/resident alien",
    "f": "Nonresident alien student, professor, or researcher filing a U.S. federal tax return or claiming an exception",
    "g": "Dependent/spouse of a nonresident alien holding a U.S. visa",
    "h": "Other (see instructions)",
}

ID_TYPES = {"passport": "Passport", "dl": "Driver's license/State ID", "uscis": "USCIS documentation", "other": "Other"}

bp = Blueprint("itinforms", __name__)


# ----------------------------------------------------------------------------- settings / log

def _cfg() -> dict:
    try:
        return _CFG["fn"]() if _CFG["fn"] else {}
    except Exception:
        return {}


def load_caa() -> dict:
    d = dict(CAA_DEFAULT)
    try:
        d.update(json.loads(CAA_FILE.read_text()))
    except Exception:
        pass
    return d


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


# ----------------------------------------------------------------------------- passport reading (AI vision)

SCAN_SYSTEM = """You read identity documents (mostly passports) for a US IRS Certifying Acceptance Agent preparing Form W-7.
Read ONLY what is printed on the document. Never guess or invent. If a value is not visible, return "".
Use the visual zone and cross-check with the machine-readable zone (MRZ) where present; if they disagree, prefer the
visual zone and say so in "notes".
Return ONLY a JSON object with these keys:
{"document_type": "passport" | "national ID" | "driver's license" | "other",
 "surname": "", "given_names": "",            // exactly as printed, Latin letters
 "first_name": "", "middle_name": "",          // split given_names: first word = first_name, rest = middle_name
 "date_of_birth": "YYYY-MM-DD", "sex": "M" | "F" | "",
 "nationality": "",                            // country name in English, e.g. "Pakistan" (not the code)
 "place_of_birth": "",                         // as printed, e.g. "KARACHI, PAK" or "LONDON"
 "birth_city": "", "birth_country": "",        // split place_of_birth; birth_country in English ONLY if printed or
                                               // the printed place is unambiguous (e.g. "KARACHI, PAK" -> "Pakistan")
 "document_number": "", "issuing_country": "", // issuing country/authority in English
 "issue_date": "YYYY-MM-DD", "expiry_date": "YYYY-MM-DD",
 "mrz_checked": true | false, "notes": ""}"""


def _to_images(data: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Return [(media_type, bytes)] – PDFs are rendered page by page (max 2 pages)."""
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        out = []
        for i, page in enumerate(doc):
            if i >= 2:
                break
            out.append(("image/png", page.get_pixmap(dpi=200).tobytes("png")))
        return out
    mt = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mt = "image/webp"
    # keep the request small: shrink very large photos
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if max(im.size) > 2400 or len(data) > 4_500_000:
            im.thumbnail((2400, 2400))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=88)
            return [("image/jpeg", buf.getvalue())]
    except Exception:
        pass
    return [(mt, data)]


def scan_passport(data: bytes, filename: str) -> dict:
    cfg = _cfg()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic API key is set (Settings page or ANTHROPIC_API_KEY) – type the details by hand.")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(b).decode()}}
               for mt, b in _to_images(data, filename)]
    content.append({"type": "text", "text": "Read this identity document and return the JSON."})
    resp = client.messages.create(model=cfg.get("model") or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5"),
                                  max_tokens=800, system=SCAN_SYSTEM, messages=[{"role": "user", "content": content}])
    raw = resp.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError("The AI reply could not be read – type the details by hand.")
    d = json.loads(m.group(0))
    return {k: (v if isinstance(v, bool) else str(v or "").strip()) for k, v in d.items()}


# ----------------------------------------------------------------------------- PDF filling

# FIELD MAPS – filled in from the official PDFs (see w7_values / coa_values below)

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _mdY(iso: str) -> str:
    """'2026-10-18' -> '10182026' for the W-7 comb date boxes (slashes are printed on the form)."""
    try:
        return dt.date.fromisoformat(iso).strftime("%m%d%Y")
    except (TypeError, ValueError):
        return ""


def w7_values(a: dict, caa: dict) -> dict:
    """a = the applicant dict built from the page. Keys = last part of the IRS field name."""
    v = {
        # application type
        "c1_1[0]": a["app_type"] == "new", "c1_1[1]": a["app_type"] == "renew",
        # reasons a..h  (c1_2 .. c1_9)
        **{f"c1_{i}[0]": (r in a["reasons"]) for i, r in zip(range(2, 10), "abcdefgh")},
        "f1_01[0]": a["rel_relationship"] if "d" in a["reasons"] else "",
        "f1_02[0]": a["us_person_name"] if ({"d", "e"} & set(a["reasons"])) else "",
        "f1_03[0]": a["us_person_tin"] if ({"d", "e"} & set(a["reasons"])) else "",
        "f1_04[0]": a["reason_h_text"] if "h" in a["reasons"] else "",
        "f1_05[0]": a["treaty_country"] if ({"a", "f"} & set(a["reasons"])) else "",
        "f1_06[0]": a["treaty_article"] if ({"a", "f"} & set(a["reasons"])) else "",
        # 1a / 1b names
        "f1_07[0]": a["first"], "f1_08[0]": a["middle"], "f1_09[0]": a["last"],
        "f1_10[0]": a["birth_first"], "f1_11[0]": a["birth_middle"], "f1_12[0]": a["birth_last"],
        # 2 mailing, 3 foreign address
        "f1_13[0]": a["mail_1"], "f1_14[0]": a["mail_2"], "f1_15[0]": a["foreign_1"], "f1_16[0]": a["foreign_2"],
        # 4 birth, 5 sex
        "f1_17[0]": _mdY(a["dob"]), "f1_18[0]": a["birth_country"], "f1_19[0]": a["birth_city"],
        "c1_10[0]": a["sex"] == "M", "c1_10[1]": a["sex"] == "F",
        # 6a-6c
        "f1_20[0]": a["citizenship"], "f1_21[0]": a["foreign_tin"], "f1_22[0]": a["visa"],
        # 6d identification document
        "c1_11[0]": a["id_type"] == "passport", "c1_11[1]": a["id_type"] == "dl",
        "c1_11[2]": a["id_type"] == "uscis", "c1_11[3]": a["id_type"] == "other",
        "f1_23[0]": a["id_other"] if a["id_type"] == "other" else "",
        "f1_24[0]": a["id_issued_by"], "f1_25[0]": a["id_number"], "f1_26[0]": _mdY(a["id_expiry"]),
        "f1_27[0]": _mdY(a["us_entry"]),
        # 6e / 6f previous ITIN / IRSN
        "c1_12[0]": a["prev"] != "yes", "c1_12[1]": a["prev"] == "yes",
        "f1_34[0]": a["prev_first"] if a["prev"] == "yes" else "", "f1_35[0]": a["prev_middle"] if a["prev"] == "yes" else "",
        "f1_36[0]": a["prev_last"] if a["prev"] == "yes" else "",
        # 6g
        "f1_37[0]": a["college"], "f1_38[0]": a["college_city"], "f1_39[0]": a["stay"],
        # signature block (signatures/dates are left blank – signed by hand)
        "f1_40[0]": a["phone"], "f1_41[0]": a["delegate_name"],
        "c1_13[0]": a["delegate_rel"] == "parent", "c1_13[1]": a["delegate_rel"] == "poa",
        "c1_13[2]": a["delegate_rel"] == "guardian",
        # Acceptance Agent's use only
        "f1_42[0]": caa["phone"], "f1_43[0]": caa["fax"], "f1_44[0]": caa["name_title"], "f1_45[0]": caa["company"],
        "f1_46[0]": caa["ein"], "f1_47[0]": caa["ptin"], "f1_48[0]": caa["office_code"],
    }
    itin, irsn = _digits(a["prev_itin"]), _digits(a["prev_irsn"])
    if a["prev"] == "yes":
        v.update({"f1_28[0]": itin[0:3], "f1_29[0]": itin[3:5], "f1_30[0]": itin[5:9],
                  "f1_31[0]": irsn[0:3], "f1_32[0]": irsn[3:5], "f1_33[0]": irsn[5:9]})
    return v


# W-7-COA supporting documentation rows: key -> (label, row number, has a Foreign Status box)
COA_ROWS = {
    "passport": ("Passport (stand-alone document)", 1, True),
    "national_id": ("National Identification Card", 2, True),
    "us_dl": ("United States Drivers License", 3, False),
    "birth_cert": ("Civil Birth Certificate", 4, True),
    "medical": ("Medical Records (dependents under 6)", 5, True),
    "foreign_dl": ("Foreign Drivers License", 6, False),
    "us_state_id": ("United States State Identification Card", 7, False),
    "voter": ("Foreign Voters Registration Card", 8, True),
    "us_military": ("United States Military Identification Card", 9, False),
    "foreign_military": ("Foreign Military Identification Card", 10, True),
    "school": ("School Records (dependents under 24)", 11, True),
    "visa": ("Visa issued by U.S. Department of State", 12, True),
    "uscis": ("USCIS Photo Identification", 13, True),
}


def coa_values(a: dict, caa: dict) -> dict:
    v = {"theUndersigned[0]": caa["responsible_party"], "businessName[0]": caa["company"],
         "applicantsName[0]": " ".join(x for x in (a["first"], a["middle"], a["last"]) if x),
         "acceptanceAgentEIN[0]": caa["ein"], "agentCode[0]": caa["office_code"], "agentPTIN[0]": caa["ptin"],
         "signatureResponsibleParty[0]": "", "dateResponsibleParty[0]": ""}
    try:
        ad = dt.date.fromisoformat(caa.get("agreement_date") or "")
        v.update({"datedMonth[0]": f"{ad.month:02d}", "datedDay[0]": f"{ad.day:02d}", "datedYear[0]": f"{ad.year % 100:02d}"})
    except ValueError:
        pass
    for key, (_lbl, row, has_fs) in COA_ROWS.items():
        v[f"Row{row}[0].identity[0]"] = key in a["coa_identity"]
        v[f"Row{row}[0].foreignStatus[0]"] = has_fs and key in a["coa_foreign"]
    return v


def fill_irs(template: Path, values: dict, keep_pages: int | None = None) -> bytes:
    """Fill an IRS form. values are keyed by the END of the full field name (e.g. 'f1_07[0]' or
    'Row1[0].identity[0]'), so the long 'topmostSubform[0].Page1[0]...' prefix does not matter."""
    import pymupdf
    doc = pymupdf.open(template)
    if keep_pages:
        doc.select(list(range(keep_pages)))
    used = set()
    for page in doc:
        for w in page.widgets():
            key = next((k for k in values if w.field_name == k or w.field_name.endswith("." + k)), None)
            if key is None:
                continue
            v = values[key]
            if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
                w.field_value = w.on_state() if v else "Off"
            else:
                v = "" if v is None else str(v)
                if w.text_maxlen and len(v) > w.text_maxlen:
                    v = v[: w.text_maxlen]
                w.field_value = v
                if len(v) > 1 and not w.text_maxlen:
                    w.text_fontsize = 0          # auto-size so long entries are never cut off
            w.update()
            used.add(key)
    missing = set(values) - used
    if missing:
        raise RuntimeError(f"{template.name} has changed – fields not found: {', '.join(sorted(missing)[:8])}")
    buf = io.BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    doc.close()
    return buf.getvalue()


def id_copy_pdf(data: bytes, filename: str) -> bytes:
    """The uploaded ID scan as a PDF (images are placed on an A4/Letter page)."""
    import pymupdf
    if data[:4] == b"%PDF":
        return data
    img = pymupdf.open(stream=data, filetype=(filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"))
    rect = img[0].rect
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    scale = min(540 / rect.width, 720 / rect.height, 1.5)
    w, h = rect.width * scale, rect.height * scale
    page.insert_image(pymupdf.Rect(36, 36, 36 + w, 36 + h), stream=data)
    buf = io.BytesIO()
    pdf.save(buf, deflate=True)
    return buf.getvalue()


# ----------------------------------------------------------------------------- tracking: sign -> post -> IRS decision

IRS_ADDR = {   # Form W-7 mailing addresses (IRS "How do I apply for an ITIN?")
    "usps": ("U.S. Postal Service", ["Internal Revenue Service", "Austin Service Center", "ITIN Operation",
                                     "P.O. Box 149342", "Austin, TX 78714-9342"]),
    "courier": ("Private delivery service (FedEx / UPS / DHL)", ["Internal Revenue Service", "ITIN Operation",
                 "Mail Stop 6090-AUSC", "3651 S. Interregional, Highway 35", "Austin, TX 78741-0000"]),
}
IRS_STATUS_PHONE = "512-433-5966"     # W-7 status line for acceptance agents (IRS letter 5614C)

STAGES = {"prepared": "Forms prepared – waiting for signatures", "ready": "Signed – ready to post",
          "posted": "Posted – waiting for the IRS", "suspended": "Suspended – IRS asked for more",
          "itin": "ITIN issued", "rejected": "Rejected", "withdrawn": "Withdrawn / cancelled"}
CLOSED = {"itin", "rejected", "withdrawn"}
OUTCOMES = {"": "Still waiting", "itin": "ITIN issued (CP565)", "suspended": "Suspended – more information requested (CP566)",
            "rejected": "Rejected (CP567)", "withdrawn": "Withdrawn / cancelled by us"}
T_BOOL = ("consent", "applicant_signed", "rp_signed", "label_done", "enc_w7", "enc_coa", "enc_id", "enc_support")
T_TEXT = ("consent_date", "applicant_signed_date", "rp_signed_date", "post_method", "carrier", "tracking_no",
          "posted_date", "outcome", "outcome_date", "itin_number", "outcome_notes", "notes")


def _d(s):
    try:
        return dt.date.fromisoformat(s or "")
    except (TypeError, ValueError):
        return None


def support_doc(a: dict) -> str:
    """What has to go in the envelope besides W-7, COA and ID copies."""
    r = set(a.get("reasons") or [])
    if "h" in r:
        return f"Documents supporting the exception claimed ({a.get('reason_h_text') or 'box h'})"
    if r & {"b", "c", "d", "e", "f", "g"}:
        return "The applicant's U.S. federal tax return (W-7 goes on top, return attached behind it)"
    return "Supporting documents for the reason ticked"


def expected_weeks(posted: dt.date) -> int:
    """IRS: allow 7 weeks; 9-11 weeks from 15 Jan to 30 Apr or from abroad. We plan on ~2 months, 11 in peak season."""
    peak = (1, 15) <= (posted.month, posted.day) <= (4, 30)
    return 11 if peak else 8


def track_info(meta: dict, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    t = meta.get("track") or {}
    signed = bool(t.get("consent") and t.get("applicant_signed") and t.get("rp_signed"))
    posted = _d(t.get("posted_date")) if t.get("label_done") else None
    out = t.get("outcome") or ""
    if out in ("itin", "rejected", "withdrawn", "suspended"):
        stage = out
    elif posted:
        stage = "posted"
    elif signed:
        stage = "ready"
    else:
        stage = "prepared"
    info = {"stage": stage, "stage_label": STAGES[stage], "signed": signed, "posted": posted, "expected": None,
            "days": None, "overdue": False, "alert": "", "closed": stage in CLOSED}
    if posted:
        info["expected"] = posted + dt.timedelta(weeks=expected_weeks(posted))
        info["days"] = (today - posted).days
    if stage == "posted" and today > info["expected"]:
        info["overdue"] = True
        info["alert"] = (f"No IRS decision after {info['days']} days (expected by {info['expected'].strftime('%d/%m/%Y')}) "
                         f"– call the IRS W-7 status line for acceptance agents on {IRS_STATUS_PHONE}.")
    elif stage == "suspended":
        info["alert"] = "IRS suspended the application – reply with the requested documents within the deadline on the letter."
    elif stage == "ready":
        since = _d(t.get("applicant_signed_date")) or _d(t.get("rp_signed_date"))
        if since and (today - since).days > 5:
            info["alert"] = f"Signed {(today - since).days} days ago but not posted yet."
    elif stage == "prepared":
        made = _d((meta.get("when") or "")[:10])
        if made and (today - made).days > 14:
            info["alert"] = f"Forms prepared {(today - made).days} days ago – still waiting for signatures."
    return info


def ptin_alert(caa: dict, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    exp = _d(caa.get("ptin_expiry"))
    if not caa.get("ptin") or not exp:
        return ""
    left = (exp - today).days
    if left < 0:
        return (f"PTIN {caa['ptin']} EXPIRED on {exp.strftime('%d/%m/%Y')} – renew it at rpr.irs.gov before signing any "
                f"more W-7s, then update the expiry date below.")
    if left <= 75:   # the IRS opens PTIN renewals for next year in mid-October
        return (f"PTIN {caa['ptin']} ({caa.get('responsible_party') or 'responsible party'}) expires on "
                f"{exp.strftime('%d/%m/%Y')} – {left} days left. Renew for {exp.year + 1} at rpr.irs.gov "
                f"(Tax Pro PTIN System), then change the PTIN expiry date in our Acceptance Agent details.")
    return ""


def all_jobs() -> list[dict]:
    out = []
    for x in _load_log():
        f = OUT / x["job"] / "job.json"
        try:
            m = json.loads(f.read_text())
        except Exception:
            continue
        m["_t"] = track_info(m)
        out.append(m)
    return out


_ALERT_CACHE = {"at": 0.0, "n": 0}


def alert_count() -> int:
    import time
    if time.time() - _ALERT_CACHE["at"] > 60:
        try:
            n = sum(1 for m in all_jobs() if m["_t"]["alert"]) + (1 if ptin_alert(load_caa()) else 0)
        except Exception:
            n = 0
        _ALERT_CACHE.update(at=time.time(), n=n)
    return _ALERT_CACHE["n"]


# ----------------------------------------------------------------------------- routes

TEXT_KEYS = ("app_type", "rel_relationship", "us_person_name", "us_person_tin", "reason_h_text", "treaty_country",
             "treaty_article", "first", "middle", "last", "birth_first", "birth_middle", "birth_last", "mail_1", "mail_2",
             "foreign_1", "foreign_2", "dob", "birth_country", "birth_city", "sex", "citizenship", "foreign_tin", "visa",
             "id_type", "id_other", "id_issued_by", "id_number", "id_expiry", "us_entry", "prev", "prev_itin", "prev_irsn",
             "prev_first", "prev_middle", "prev_last", "college", "college_city", "stay", "phone", "delegate_name",
             "delegate_rel", "client_ref")


def applicant_from_form(f) -> tuple[dict, list[str]]:
    a = {k: (f.get(k) or "").strip() for k in TEXT_KEYS}
    a["reasons"] = [r for r in REASONS if f.get(f"reason_{r}")]
    a["coa_identity"] = [k for k in COA_ROWS if f.get(f"coa_id_{k}")]
    a["coa_foreign"] = [k for k, row in COA_ROWS.items() if row[2] and f.get(f"coa_fs_{k}")]
    r, e = set(a["reasons"]), []
    if a["app_type"] not in ("new", "renew"):
        e.append("Choose the application type (new ITIN or renewal).")
    if not r:
        e.append("Tick at least one reason for submitting Form W-7 (boxes a–h).")
    if "a" in r and "h" not in r:
        e.append("Box a needs box h ticked too (with the exception, e.g. \"Exception 2(a)\") – see the W-7 instructions.")
    if r & {"a", "f"} and "a" in r and not (a["treaty_country"] and a["treaty_article"]):
        e.append("Box a: enter the treaty country and treaty article number.")
    if "d" in r and not a["rel_relationship"]:
        e.append("Box d: enter the relationship to the U.S. citizen/resident alien.")
    if r & {"d", "e"} and not (a["us_person_name"] and a["us_person_tin"]):
        e.append("Box d/e: enter the name and SSN/ITIN of the U.S. citizen/resident alien.")
    if "h" in r and not a["reason_h_text"]:
        e.append("Box h: enter the exception or reason (e.g. \"Exception 1(d)\").")
    if not a["first"] or not a["last"]:
        e.append("Line 1a: first name and last name are required.")
    if not a["mail_1"] or not a["mail_2"]:
        e.append("Line 2: both lines of the mailing address are required.")
    if not a["dob"]:
        e.append("Line 4: date of birth is required.")
    if not a["birth_country"]:
        e.append("Line 4: country of birth is required.")
    if a["sex"] not in ("M", "F"):
        e.append("Line 5: choose male or female.")
    if not a["citizenship"]:
        e.append("Line 6a: country of citizenship is required.")
    if a["id_type"] not in ID_TYPES:
        e.append("Line 6d: choose the identification document type.")
    if not a["id_issued_by"] or not a["id_number"]:
        e.append("Line 6d: 'Issued by' and document number are required.")
    if a["id_type"] == "other" and not a["id_other"]:
        e.append("Line 6d: describe the 'Other' document.")
    if a["prev"] == "yes" and not (_digits(a["prev_itin"]) or _digits(a["prev_irsn"])):
        e.append("Line 6f: enter the previous ITIN and/or IRSN (or tick No/Don't know).")
    if a["app_type"] == "renew" and not _digits(a["prev_itin"]):
        e.append("Renewal: enter the existing ITIN on line 6f (and tick Yes on 6e).")
    if not a["coa_identity"] or not a["coa_foreign"]:
        e.append("Certificate of Accuracy: tick the documents you reviewed for both Identity and Foreign Status.")
    return a, e


@bp.get("/itin")
def itin_home():
    missing = [t for t in ("fw7.pdf", "fw7coa.pdf") if not (TPL_DIR / t).exists()]
    caa = load_caa()
    caa_missing = [lbl for k, lbl in (("responsible_party", "responsible party"), ("name_title", "name and title"),
                                      ("ein", "EIN"), ("office_code", "office code"), ("agreement_date", "agreement date"))
                   if not caa.get(k)]
    jobs = all_jobs()
    return render_template_string(PAGE, reasons=REASONS, id_types=ID_TYPES, coa_rows=COA_ROWS, caa=caa,
                                  caa_missing=caa_missing, missing=missing, log=_load_log()[:40],
                                  open_jobs=[m for m in jobs if not m["_t"]["closed"]],
                                  done_jobs=[m for m in jobs if m["_t"]["closed"]][:40], stages=STAGES,
                                  ptin_msg=ptin_alert(caa),
                                  has_ai=bool(_cfg().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")))


@bp.post("/itin/caa")
def itin_caa():
    d = {k: (request.form.get(k) or "").strip() for k in CAA_DEFAULT}
    with _LOCK:
        OUT.mkdir(parents=True, exist_ok=True)
        CAA_FILE.write_text(json.dumps(d, indent=1))
    _ALERT_CACHE["at"] = 0
    flash("Acceptance Agent details saved.")
    return redirect("/itin")


@bp.post("/itin/scan")
def itin_scan():
    up = request.files.get("id_file")
    if not up or not up.filename:
        return jsonify(ok=False, error="Choose the passport scan first.")
    data = up.read()
    if len(data) > 15_000_000:
        return jsonify(ok=False, error="File is larger than 15 MB – please use a smaller scan.")
    try:
        return jsonify(ok=True, data=scan_passport(data, up.filename))
    except Exception as ex:
        return jsonify(ok=False, error=f"Could not read the document: {ex}")


@bp.post("/itin/generate")
def itin_generate():
    a, errs = applicant_from_form(request.form)
    caa = load_caa()
    if not caa.get("responsible_party") or not caa.get("ein") or not caa.get("office_code"):
        errs.append("Fill in and save our Acceptance Agent details first (responsible party, EIN, office code).")
    if errs:
        for e in errs:
            flash("⚠ " + e)
        return render_template_string(PAGE, reasons=REASONS, id_types=ID_TYPES, coa_rows=COA_ROWS, caa=caa, caa_missing=[],
                                      missing=[], log=_load_log()[:40], prefill=a,
                                      has_ai=bool(_cfg().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")))
    job = uuid.uuid4().hex[:12]
    d = OUT / job
    d.mkdir(parents=True, exist_ok=True)
    tag = _safe(f"{a['last']}_{a['first']}")
    files = []
    try:
        (d / f"W-7_{tag}.pdf").write_bytes(fill_irs(TPL_DIR / "fw7.pdf", w7_values(a, caa)))
        files.append({"file": f"W-7_{tag}.pdf", "label": "Form W-7 – Application for ITIN"})
        (d / f"W-7-COA_{tag}.pdf").write_bytes(fill_irs(TPL_DIR / "fw7coa.pdf", coa_values(a, caa), keep_pages=1))
        files.append({"file": f"W-7-COA_{tag}.pdf", "label": "Form W-7-COA – Certificate of Accuracy"})
        up = request.files.get("id_file")
        if up and up.filename:
            raw = up.read()
            if raw:
                (d / f"ID_copy_{tag}.pdf").write_bytes(id_copy_pdf(raw, up.filename))
                files.append({"file": f"ID_copy_{tag}.pdf", "label": "Copy of identity document (attach to COA)"})
    except Exception as ex:
        flash(f"⚠ Could not create the forms: {ex}")
        return redirect("/itin")
    name = " ".join(x for x in (a["first"], a["middle"], a["last"]) if x)
    meta = {"job": job, "when": dt.datetime.now().isoformat(timespec="seconds"), "applicant": name, "a": a,
            "staff": session.get("user") or os.environ.get("MAILSORT_USER", "admin"), "files": files,
            "caa": {k: caa.get(k, "") for k in ("company", "responsible_party", "office_code")}}
    (d / "job.json").write_text(json.dumps(meta, indent=1))
    _append_log({"job": job, "when": meta["when"], "applicant": name, "reasons": a["reasons"], "app_type": a["app_type"],
                 "client_ref": a["client_ref"], "staff": meta["staff"], "nfiles": len(files)})
    return redirect(f"/itin/job/{job}")


@bp.get("/itin/job/<job>")
def itin_job(job):
    d = _job_dir(job)
    m = json.loads((d / "job.json").read_text())
    return render_template_string(JOB_PAGE, m=m, t=m.get("track") or {}, ti=track_info(m), reasons=REASONS,
                                  addr=IRS_ADDR, outcomes=OUTCOMES, support=support_doc(m["a"]), phone=IRS_STATUS_PHONE,
                                  today=dt.date.today().isoformat(), ptin_msg=ptin_alert(load_caa()), stages=STAGES)


@bp.post("/itin/job/<job>/track")
def itin_track(job):
    d = _job_dir(job)
    f = request.form
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        old = m.get("track") or {}
        before = track_info(m)["stage"]
        t = {k: bool(f.get(k)) for k in T_BOOL}
        t.update({k: (f.get(k) or "").strip() for k in T_TEXT})
        errs = []
        for flag, date_k, what in (("consent", "consent_date", "client consent"), ("applicant_signed", "applicant_signed_date",
                                   "applicant's signature"), ("rp_signed", "rp_signed_date", "responsible party's signature")):
            if t[flag] and not _d(t[date_k]):
                errs.append(f"Enter the date of the {what}.")
        signed = t["consent"] and t["applicant_signed"] and t["rp_signed"]
        if (t["label_done"] or t["posted_date"]) and not signed:
            errs.append("Posting is blocked until the client consent, the applicant's ink signature and our responsible "
                        "party's signature are all ticked.")
            t["label_done"], t["posted_date"] = False, ""
        if t["posted_date"] and not t["label_done"]:
            errs.append("Tick \"Postage label generated\" before entering the posting date.")
            t["posted_date"] = ""
        if t["label_done"] and not t["posted_date"]:
            errs.append("Enter the date the envelope was posted – the 2-month IRS reminder starts from it.")
            t["label_done"] = False
        if t["label_done"] and not all(t[k] for k in ("enc_w7", "enc_coa", "enc_id", "enc_support")):
            errs.append("Tick every item in the envelope checklist before posting.")
            t["label_done"], t["posted_date"] = False, ""
        if t["outcome"] and t["outcome"] != "withdrawn" and not _d(t["posted_date"]):
            errs.append("An IRS outcome can only be recorded after the application has been posted.")
            t["outcome"] = ""
        if t["outcome"] == "itin" and not re.fullmatch(r"9\d{2}-?\d{2}-?\d{4}", t["itin_number"]):
            errs.append("Enter the ITIN issued (9XX-XX-XXXX) from the IRS letter.")
        if t["post_method"] not in IRS_ADDR:
            t["post_method"] = "courier"
        hist = m.get("history") or []
        m["track"] = t
        after = track_info(m)["stage"]
        if after != before:
            hist.append({"when": dt.datetime.now().isoformat(timespec="seconds"), "stage": after,
                         "staff": session.get("user") or os.environ.get("MAILSORT_USER", "admin")})
        m["history"] = hist
        (d / "job.json").write_text(json.dumps(m, indent=1))
    _ALERT_CACHE["at"] = 0
    for e in errs:
        flash("⚠ " + e)
    if not errs:
        flash("Progress saved – " + STAGES[after] + ".")
    return redirect(f"/itin/job/{job}#track")


@bp.get("/itin/job/<job>/file/<path:fname>")
def itin_file(job, fname):
    d = _job_dir(job)
    meta = json.loads((d / "job.json").read_text())
    if fname not in {x["file"] for x in meta["files"]}:
        abort(404)
    return send_file(d / fname, mimetype="application/pdf", as_attachment=request.args.get("inline") != "1",
                     download_name=fname)


@bp.get("/itin/job/<job>/zip")
def itin_zip(job):
    d = _job_dir(job)
    meta = json.loads((d / "job.json").read_text())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for x in meta["files"]:
            z.write(d / x["file"], x["file"])
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"ITIN_{_safe(meta['applicant'])}.zip")


def register(app, cfg_fn=None):
    _CFG["fn"] = cfg_fn
    OUT.mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)

    @app.context_processor
    def _itin_badge():          # red number next to "ITIN" in the menu: overdue/stuck applications + PTIN renewal
        try:
            return {"itin_alerts": alert_count()}
        except Exception:
            return {"itin_alerts": 0}


# ----------------------------------------------------------------------------- pages

PAGE = r"""{% extends "base" %}{% block body %}{% set p = prefill or {} %}
<style>
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:4px 18px}
input[type=date],select{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;font-family:inherit}
.tick{display:flex;gap:10px;align-items:flex-start;font-weight:600;font-size:14px;margin:8px 0;cursor:pointer}
.tick input{margin-top:3px;width:16px;height:16px;flex:0 0 16px}
.ok{background:#e8f7ee;border:1px solid #86efac;padding:10px 14px;border-radius:8px;margin:10px 0}
.bad{background:#fee2e2;border:1px solid #fca5a5;color:#991b1b;padding:10px 14px;border-radius:8px;margin:10px 0}
.warn{background:#fef9c3;border:1px solid #fde047;padding:10px 14px;border-radius:8px;margin:10px 0}
.step{display:inline-flex;width:24px;height:24px;border-radius:50%;background:#111;color:#fff;font-size:13px;align-items:center;justify-content:center;margin-right:8px}
.box{background:#f7f7f7;border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:8px 0}
.extra{margin:4px 0 10px 26px}.hide{display:none}
.ln{display:block;margin-top:16px;font-weight:700;color:#555;font-size:12px}
.radios label{display:inline-flex;gap:6px;align-items:center;margin-right:18px;font-weight:600;font-size:14px}
.radios input{width:16px;height:16px}
table.coa td,table.coa th{text-align:center}table.coa td:first-child,table.coa th:first-child{text-align:left}
table.coa input{width:16px;height:16px}
</style>
<div class="card"><h1>ITIN application (Form W-7)</h1>
<p class="muted">Prepares IRS Form W-7 and the Form W-7-COA Certificate of Accuracy for Startitup Global Inc as a Certifying
Acceptance Agent. Upload the applicant's passport, let the AI read it, then check and complete the rest. The official IRS
forms are used as published – only the fields are filled. Signatures and signing dates are left blank to sign by hand.</p>
{% if missing %}<div class="bad">Template file(s) missing from the <b>itin_templates</b> folder on GitHub: {{ missing|join(', ') }}</div>{% endif %}
{% if caa_missing %}<div class="bad">Our Acceptance Agent details are incomplete ({{ caa_missing|join(', ') }}) – fill them in below and press Save.</div>{% endif %}
{% if ptin_msg %}<div class="bad">⏰ <b>PTIN renewal:</b> {{ptin_msg}}</div>{% endif %}
{% if not has_ai %}<div class="warn">No Anthropic API key is set, so passport reading is off – type the details by hand.</div>{% endif %}
</div>

<div class="card"><details {% if caa_missing %}open{% endif %}><summary style="cursor:pointer;font-weight:700;font-size:16px">Our Acceptance Agent details (printed on every W-7 and COA)</summary>
<form method="post" action="/itin/caa"><div class="fgrid" style="margin-top:10px">
 <div><label>Responsible party (COA "The undersigned")</label><input type="text" name="responsible_party" value="{{caa.responsible_party}}"></div>
 <div><label>W-7 Name and title (e.g. "Usama Muchada, Responsible Party")</label><input type="text" name="name_title" value="{{caa.name_title}}"></div>
 <div><label>Name of company</label><input type="text" name="company" value="{{caa.company}}"></div>
 <div><label>EIN (e.g. 12-3456789)</label><input type="text" name="ein" value="{{caa.ein}}"></div>
 <div><label>Office code (Registered User Portal → Acceptance Agent application summary)</label><input type="text" name="office_code" value="{{caa.office_code}}"></div>
 <div><label>PTIN (optional)</label><input type="text" name="ptin" value="{{caa.ptin}}"></div>
 <div><label>PTIN valid until (renew every year – reminder 75 days before)</label><input type="date" name="ptin_expiry" value="{{caa.ptin_expiry}}"></div>
 <div><label>Agreement approved date (portal → Agreement Summary)</label><input type="date" name="agreement_date" value="{{caa.agreement_date}}"></div>
 <div><label>Phone</label><input type="text" name="phone" value="{{caa.phone}}"></div>
 <div><label>Fax (optional)</label><input type="text" name="fax" value="{{caa.fax}}"></div>
</div><div style="margin-top:12px"><button class="btn" type="submit">Save Acceptance Agent details</button></div></form></details></div>

<form method="post" action="/itin/generate" enctype="multipart/form-data" id="wf" onsubmit="return beforeSubmit()">
<div class="card"><h2><span class="step">1</span>Identity document</h2>
<div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
 <div style="flex:1 1 280px"><label>Passport scan (photo page – JPG, PNG or PDF)</label><input type="file" name="id_file" id="id_file" accept="image/*,application/pdf"></div>
 <button type="button" class="btn" id="scanbtn" onclick="scan()" {% if not has_ai %}disabled{% endif %}>Read with AI</button>
</div>
{% if prefill %}<div class="warn">Please choose the passport scan again – browsers clear the file after a correction.</div>{% endif %}
<div id="scanmsg"></div>
<p class="muted" style="margin:6px 0 0">The scan is also saved with the job as "Copy of identity document" to send with the COA.</p>
</div>

<div class="card"><h2><span class="step">2</span>Application type &amp; reason</h2>
<div class="radios"><label><input type="radio" name="app_type" value="new" {% if p.app_type != 'renew' %}checked{% endif %}> Apply for a new ITIN</label>
<label><input type="radio" name="app_type" value="renew" {% if p.app_type == 'renew' %}checked{% endif %}> Renew an existing ITIN</label></div>
<p class="muted" style="margin:12px 0 0">Tick every reason that applies to this client.</p>
{% for k, lbl in reasons.items() %}<label class="tick"><input type="checkbox" name="reason_{{k}}" value="1" id="r_{{k}}" onchange="showExtras()" {% if k in (p.reasons or []) %}checked{% endif %}> <span><b>{{k}}</b> &nbsp;{{lbl}}</span></label>
 {% if k == 'a' %}<div class="extra hide" id="x_af"><div class="fgrid">
   <div><label>Treaty country (a / f)</label><input type="text" name="treaty_country" value="{{p.treaty_country}}"></div>
   <div><label>Treaty article number</label><input type="text" name="treaty_article" value="{{p.treaty_article}}"></div></div>
   <p class="muted" style="margin:2px 0">Box a also needs box h ticked with the exception (it is ticked for you).</p></div>{% endif %}
 {% if k == 'e' %}<div class="extra hide" id="x_de"><div class="fgrid">
   <div id="x_d"><label>Box d: relationship to U.S. citizen/resident alien</label><input type="text" name="rel_relationship" value="{{p.rel_relationship}}" placeholder="e.g. Son"></div>
   <div><label>Name of U.S. citizen/resident alien</label><input type="text" name="us_person_name" value="{{p.us_person_name}}"></div>
   <div><label>Their SSN/ITIN</label><input type="text" name="us_person_tin" value="{{p.us_person_tin}}" placeholder="123-45-6789"></div></div></div>{% endif %}
 {% if k == 'h' %}<div class="extra hide" id="x_h"><div class="fgrid">
   <div><label>Box h: exception / reason</label><input type="text" name="reason_h_text" list="exlist" value="{{p.reason_h_text}}" placeholder="e.g. Exception 1(d)"></div></div>
   <datalist id="exlist">{% for x in ['Exception 1(a)','Exception 1(b)','Exception 1(c)','Exception 1(d)','Exception 1(e)','Exception 2(a)','Exception 2(b)','Exception 2(c)','Exception 2(d)','Exception 3','Exception 4','Exception 5'] %}<option value="{{x}}">{% endfor %}</datalist></div>{% endif %}
{% endfor %}
<div class="fgrid" style="margin-top:8px"><div><label>Client / portal reference (optional, for our log)</label><input type="text" name="client_ref" value="{{p.client_ref}}"></div></div>
</div>

<div class="card"><h2><span class="step">3</span>Applicant</h2>
<span class="ln">1a Name</span>
<div class="fgrid"><div><label>First name</label><input type="text" name="first" id="first" value="{{p.first}}"></div>
<div><label>Middle name</label><input type="text" name="middle" id="middle" value="{{p.middle}}"></div>
<div><label>Last name</label><input type="text" name="last" id="last" value="{{p.last}}"></div></div>
<details {% if p.birth_last %}open{% endif %}><summary style="cursor:pointer;margin:8px 0" class="muted">1b Name at birth (only if different)</summary>
<div class="fgrid"><div><label>First name</label><input type="text" name="birth_first" value="{{p.birth_first}}"></div>
<div><label>Middle name</label><input type="text" name="birth_middle" value="{{p.birth_middle}}"></div>
<div><label>Last name</label><input type="text" name="birth_last" value="{{p.birth_last}}"></div></div></details>

<span class="ln">2 Mailing address (where the IRS sends the ITIN letter)</span>
<div class="fgrid"><div><label>Street address, apartment number</label><input type="text" name="mail_1" value="{{p.mail_1}}"></div>
<div><label>City, state/province, country and postcode</label><input type="text" name="mail_2" value="{{p.mail_2}}"></div></div>
<span class="ln">3 Foreign (non-U.S.) address – only if different from line 2 (no P.O. box)</span>
<div class="fgrid"><div><label>Street address, apartment number</label><input type="text" name="foreign_1" value="{{p.foreign_1}}"></div>
<div><label>City, state/province, country and postcode</label><input type="text" name="foreign_2" value="{{p.foreign_2}}"></div></div>

<span class="ln">4 Birth information &nbsp; 5 Sex</span>
<div class="fgrid"><div><label>Date of birth</label><input type="date" name="dob" id="dob" value="{{p.dob}}"></div>
<div><label>Country of birth</label><input type="text" name="birth_country" id="birth_country" value="{{p.birth_country}}"></div>
<div><label>City and state or province (optional)</label><input type="text" name="birth_city" id="birth_city" value="{{p.birth_city}}"></div>
<div><label>Sex</label><div class="radios" style="padding-top:8px"><label><input type="radio" name="sex" value="M" id="sex_M" {% if p.sex == 'M' %}checked{% endif %}> Male</label><label><input type="radio" name="sex" value="F" id="sex_F" {% if p.sex == 'F' %}checked{% endif %}> Female</label></div></div></div>

<span class="ln">6a–6c Other information</span>
<div class="fgrid"><div><label>6a Country(ies) of citizenship</label><input type="text" name="citizenship" id="citizenship" value="{{p.citizenship}}"></div>
<div><label>6b Foreign tax I.D. number (if any)</label><input type="text" name="foreign_tin" value="{{p.foreign_tin}}"></div>
<div><label>6c U.S. visa type, number, expiry (if any)</label><input type="text" name="visa" value="{{p.visa}}" placeholder="leave blank if none"></div></div>

<span class="ln">6d Identification document submitted</span>
<div class="fgrid"><div><label>Document type</label><select name="id_type" id="id_type" onchange="syncCoa()">{% for k, lbl in id_types.items() %}<option value="{{k}}" {% if (p.id_type or 'passport') == k %}selected{% endif %}>{{lbl}}</option>{% endfor %}</select></div>
<div id="x_other" class="hide"><label>"Other" – describe</label><input type="text" name="id_other" value="{{p.id_other}}"></div>
<div><label>Issued by (country)</label><input type="text" name="id_issued_by" id="id_issued_by" value="{{p.id_issued_by}}"></div>
<div><label>Document number</label><input type="text" name="id_number" id="id_number" value="{{p.id_number}}"></div>
<div><label>Expiry date</label><input type="date" name="id_expiry" id="id_expiry" value="{{p.id_expiry}}"></div>
<div><label>Date of entry into the U.S. (if any)</label><input type="date" name="us_entry" value="{{p.us_entry}}"></div></div>

<span class="ln">6e / 6f Previous ITIN or IRSN</span>
<div class="radios"><label><input type="radio" name="prev" value="no" onchange="showExtras()" {% if p.prev != 'yes' %}checked{% endif %}> No / Don't know</label>
<label><input type="radio" name="prev" value="yes" id="prev_yes" onchange="showExtras()" {% if p.prev == 'yes' %}checked{% endif %}> Yes</label></div>
<div id="x_prev" class="hide"><div class="fgrid">
<div><label>ITIN</label><input type="text" name="prev_itin" value="{{p.prev_itin}}" placeholder="9XX-XX-XXXX"></div>
<div><label>IRSN</label><input type="text" name="prev_irsn" value="{{p.prev_irsn}}"></div>
<div><label>Issued under first name</label><input type="text" name="prev_first" value="{{p.prev_first}}"></div>
<div><label>Middle name</label><input type="text" name="prev_middle" value="{{p.prev_middle}}"></div>
<div><label>Last name</label><input type="text" name="prev_last" value="{{p.prev_last}}"></div></div></div>

<span class="ln">6g College/university or company</span>
<div class="fgrid"><div><label>Name of college/university or company</label><input type="text" name="college" value="{{p.college}}"></div>
<div><label>City and state</label><input type="text" name="college_city" value="{{p.college_city}}"></div>
<div><label>Length of stay</label><input type="text" name="stay" value="{{p.stay}}"></div></div>

<span class="ln">Sign here</span>
<div class="fgrid"><div><label>Applicant's phone number</label><input type="text" name="phone" value="{{p.phone}}"></div>
<div><label>Name of delegate (only if signing for the applicant)</label><input type="text" name="delegate_name" value="{{p.delegate_name}}"></div>
<div><label>Delegate's relationship</label><select name="delegate_rel"><option value="">—</option>
{% for k, lbl in [('parent','Parent'),('poa','Power of attorney'),('guardian','Court-appointed guardian')] %}<option value="{{k}}" {% if p.delegate_rel == k %}selected{% endif %}>{{lbl}}</option>{% endfor %}</select></div></div>
</div>

<div class="card"><h2><span class="step">4</span>Certificate of Accuracy – documents you reviewed</h2>
<p class="muted" style="margin-top:0">A passport alone proves both identity and foreign status. Without a passport, tick two or more documents.</p>
<table class="coa"><tr><th>Supporting documentation</th><th style="width:90px">Identity</th><th style="width:110px">Foreign status</th></tr>
{% for k, r in coa_rows.items() %}<tr><td>{{r[0]}}</td><td><input type="checkbox" name="coa_id_{{k}}" id="cid_{{k}}" value="1" {% if k in (p.coa_identity or []) %}checked{% endif %}></td>
<td>{% if r[2] %}<input type="checkbox" name="coa_fs_{{k}}" id="cfs_{{k}}" value="1" {% if k in (p.coa_foreign or []) %}checked{% endif %}>{% else %}<span class="muted">—</span>{% endif %}</td></tr>{% endfor %}</table>
<div style="margin-top:18px"><button class="btn" type="submit">Create W-7 + Certificate of Accuracy</button></div>
</div>
</form>

{% macro jobrow(x) %}{% set ti = x._t %}<tr{% if ti.alert %} style="background:#fff7ed"{% endif %}><td>{{x.applicant}}<br><span class="muted">{{x.a.client_ref or ''}} {{x.when|ukdate}}</span></td>
<td><b>{{ti.stage_label}}</b>{% if ti.alert %}<br><span style="color:#b45309">⚠ {{ti.alert}}</span>{% endif %}</td>
<td>{{ ti.posted.strftime('%d/%m/%Y') if ti.posted else '—' }}{% if ti.days is not none and not ti.closed %}<br><span class="muted">{{ti.days}} days ago</span>{% endif %}</td>
<td>{% if x.track and x.track.outcome == 'itin' %}ITIN {{x.track.itin_number}}{% elif ti.expected %}{{ti.expected.strftime('%d/%m/%Y')}}{% else %}—{% endif %}</td>
<td><a class="btn small secondary" href="/itin/job/{{x.job}}#track">Open</a></td></tr>{% endmacro %}
<div class="card" id="progress"><h2>Applications in progress ({{open_jobs|length}})</h2>
{% if open_jobs %}<table><tr><th>Applicant</th><th>Stage</th><th>Posted to IRS</th><th>IRS decision expected</th><th></th></tr>
{% for x in open_jobs %}{{ jobrow(x) }}{% endfor %}</table>
<p class="muted">IRS timing: about 2 months from posting (8 weeks; 11 weeks if posted between 15 Jan and 30 Apr). Rows turn orange when something needs action.</p>
{% else %}<p class="muted">Nothing in progress.</p>{% endif %}</div>
{% if done_jobs %}<div class="card"><details><summary style="cursor:pointer;font-weight:700">Completed / closed ({{done_jobs|length}})</summary>
<table style="margin-top:10px"><tr><th>Applicant</th><th>Outcome</th><th>Posted</th><th>Result</th><th></th></tr>
{% for x in done_jobs %}{{ jobrow(x) }}{% endfor %}</table></details></div>{% endif %}

<script>
function $(id){return document.getElementById(id)}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')}
function on(id){return $(id) && $(id).checked}
function showExtras(){
  if(on('r_a') && !on('r_h')){ $('r_h').checked = true }
  $('x_af').classList.toggle('hide', !(on('r_a')||on('r_f')));
  $('x_de').classList.toggle('hide', !(on('r_d')||on('r_e')));
  $('x_d').classList.toggle('hide', !on('r_d'));
  $('x_h').classList.toggle('hide', !on('r_h'));
  $('x_prev').classList.toggle('hide', !on('prev_yes'));
  $('x_other').classList.toggle('hide', $('id_type').value!='other');
}
function syncCoa(){
  var pp = $('id_type').value=='passport';
  if(pp){ $('cid_passport').checked = true; $('cfs_passport').checked = true }
  showExtras();
}
function setv(id, v){ if(v && $(id)) $(id).value = v }
function scan(){
  var f = $('id_file').files[0], msg = $('scanmsg');
  if(!f){ msg.innerHTML = '<div class="bad">Choose the passport scan first.</div>'; return }
  var fd = new FormData(); fd.append('id_file', f);
  $('scanbtn').disabled = true; msg.innerHTML = '<p class="muted">Reading the document… (10–20 seconds)</p>';
  fetch('/itin/scan', {method:'POST', body:fd}).then(function(r){return r.json()}).then(function(r){
    $('scanbtn').disabled = false;
    if(!r.ok){ msg.innerHTML = '<div class="bad">'+esc(r.error)+'</div>'; return }
    var d = r.data;
    setv('first', d.first_name); setv('middle', d.middle_name); setv('last', d.surname);
    setv('dob', d.date_of_birth); setv('birth_country', d.birth_country); setv('birth_city', d.birth_city);
    setv('citizenship', d.nationality); setv('id_number', d.document_number); setv('id_issued_by', d.issuing_country);
    setv('id_expiry', d.expiry_date);
    if(d.sex=='M'||d.sex=='F') $('sex_'+d.sex).checked = true;
    if(/passport/i.test(d.document_type||'')){ $('id_type').value = 'passport'; syncCoa() }
    var w = [];
    if(d.expiry_date && d.expiry_date < new Date().toISOString().slice(0,10)) w.push('⚠ The document is EXPIRED ('+esc(d.expiry_date)+'). The IRS only accepts current documents.');
    if(!d.birth_country) w.push('Country of birth is not printed – ask the client and type it in.');
    if(d.notes) w.push('AI note: '+esc(d.notes));
    msg.innerHTML = '<div class="ok">Read <b>'+esc([d.first_name,d.middle_name,d.surname].filter(Boolean).join(' '))+'</b> – '
      + esc(d.document_type||'document')+' '+esc(d.document_number)+(d.mrz_checked?' (checked against the MRZ)':'')+'. Check every field against the scan.</div>'
      + (w.length ? '<div class="warn">'+w.join('<br>')+'</div>' : '');
  }).catch(function(e){ $('scanbtn').disabled = false; msg.innerHTML = '<div class="bad">Reading failed: '+esc(e)+'</div>' });
}
function beforeSubmit(){
  var p = [];
  if(!document.querySelector('[name^=reason_]:checked')) p.push('Tick at least one reason (a–h).');
  if(!$('first').value.trim() || !$('last').value.trim()) p.push('Enter the applicant\'s first and last name.');
  if(p.length){ alert(p.join('\n')); return false }
  return true;
}
{% if not prefill %}syncCoa();{% else %}showExtras();{% endif %}
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
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:4px 18px}
input[type=date],select{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;font-family:inherit}
.tick{display:flex;gap:10px;align-items:flex-start;font-weight:600;font-size:14px;margin:10px 0 4px;cursor:pointer}
.tick input{margin-top:3px;width:16px;height:16px;flex:0 0 16px}
.sub{margin:0 0 8px 26px;max-width:260px}
.bad{background:#fee2e2;border:1px solid #fca5a5;color:#991b1b;padding:10px 14px;border-radius:8px;margin:10px 0}
.warn{background:#fef9c3;border:1px solid #fde047;padding:10px 14px;border-radius:8px;margin:10px 0}
.okb{background:#e8f7ee;border:1px solid #86efac;padding:10px 14px;border-radius:8px;margin:10px 0}
.steps{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 4px}
.steps span{flex:1 1 150px;padding:8px 10px;border-radius:8px;background:#f1f5f9;color:#64748b;font-size:13px;font-weight:600}
.steps span.done{background:#dcfce7;color:#166534}.steps span.now{background:#111;color:#fff}
.tsec{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
.tsec h3{margin:0 0 4px;font-size:15px}
.addr{font-family:ui-monospace,Menlo,Consolas,monospace;background:#f7f7f7;border:1px dashed #94a3b8;border-radius:8px;padding:10px 14px;line-height:1.5;display:inline-block;margin:6px 0}
</style>
<div class="card"><h1>{{m.applicant}} <span class="muted" style="font-size:15px">ITIN – {{ 'renewal' if m.a.app_type == 'renew' else 'new application' }}</span></h1>
<div class="dgrid">
 <div><b>Created</b>{{m.when|ukdate}} by {{m.staff}}</div>
 <div><b>Reasons</b>{% for r in m.a.reasons %}{{r}} – {{reasons[r]}}{% if r == 'h' %} ({{m.a.reason_h_text}}){% endif %}<br>{% endfor %}</div>
 <div><b>Document</b>{{m.a.id_type|upper}} {{m.a.id_number}} ({{m.a.id_issued_by}}), expires {{m.a.id_expiry|ukdate}}</div>
 {% if m.a.client_ref %}<div><b>Reference</b>{{m.a.client_ref}}</div>{% endif %}
</div>
<div class="btnrow" style="margin-top:14px"><a class="btn" href="/itin/job/{{m.job}}/zip">⬇ Download all ({{m.files|length}}) as ZIP</a>
<a class="btn secondary" href="/itin">New application</a></div>
<p class="muted">Before sending to the IRS: check every field against the documents; the applicant signs and dates the W-7;
our responsible party signs and dates the Acceptance Agent box and the Certificate of Accuracy; attach the copy of the
identity document and the tax return (or exception documents) as required by the reason boxes ticked.</p></div>

{% set order = ['prepared','ready','posted','itin'] %}{% set cur = order.index(ti.stage) if ti.stage in order else (2 if ti.stage == 'suspended' else 3) %}
<div class="card" id="track"><h2>Progress tracker</h2>
{% if ptin_msg %}<div class="bad">⏰ <b>PTIN renewal:</b> {{ptin_msg}}</div>{% endif %}
<div class="steps">{% for k, lbl in [('prepared','1 · Forms prepared'),('ready','2 · Signed'),('posted','3 · Posted to IRS'),('itin','4 · IRS decision')] %}
<span class="{{ 'now' if loop.index0 == cur else ('done' if loop.index0 < cur else '') }}">{{lbl}}</span>{% endfor %}</div>
<p style="margin:6px 0"><b>Current stage:</b> {{ti.stage_label}}{% if ti.expected and not ti.closed %} · IRS decision expected by <b>{{ti.expected.strftime('%d/%m/%Y')}}</b> ({{ti.days}} days since posting){% endif %}</p>
{% if ti.alert %}<div class="warn">⚠ {{ti.alert}}</div>{% endif %}
{% if ti.stage == 'itin' %}<div class="okb">✔ ITIN issued: <b>{{t.itin_number}}</b>{% if t.outcome_date %} on {{t.outcome_date|ukdate}}{% endif %}.</div>{% endif %}

<form method="post" action="/itin/job/{{m.job}}/track">
<div class="tsec"><h3>A. Client consent &amp; signatures</h3>
<div class="warn" style="margin:6px 0 10px"><b>The W-7 must be signed by hand in ink.</b> Form W-7 is not on the IRS list of forms that accept electronic
or typed signatures, so a digital signature (or just the name typed in) gets the application suspended or rejected. Post or courier the printed
W-7 to the client, they sign and date it in ink and send the original back; we post that original to the IRS. Moaz signs the Acceptance
Agent box on the W-7 and the Certificate of Accuracy in ink too.</div>
<label class="tick"><input type="checkbox" name="consent" value="1" {% if t.consent %}checked{% endif %}> Client has signed our ITIN application / engagement form, giving consent for Startitup Global Inc (Certifying Acceptance Agent) to prepare and submit their Form W-7 and to receive copies of their ITIN notices</label>
<div class="sub"><label>Date client signed our application</label><input type="date" name="consent_date" value="{{t.consent_date}}"></div>
<label class="tick"><input type="checkbox" name="applicant_signed" value="1" {% if t.applicant_signed %}checked{% endif %}> Applicant has signed and dated the W-7 by hand in ink – the ORIGINAL signed form is with us</label>
<div class="sub"><label>Date applicant signed the W-7</label><input type="date" name="applicant_signed_date" value="{{t.applicant_signed_date}}"></div>
<label class="tick"><input type="checkbox" name="rp_signed" value="1" {% if t.rp_signed %}checked{% endif %}> {{m.caa.responsible_party or 'Responsible party'}} has signed in ink the W-7 Acceptance Agent box and the Certificate of Accuracy</label>
<div class="sub"><label>Date signed</label><input type="date" name="rp_signed_date" value="{{t.rp_signed_date}}"></div>
</div>

<div class="tsec"><h3>B. Post to the IRS</h3>
<p class="muted" style="margin:2px 0 8px">The system will not accept posting until all three boxes in section A are ticked.</p>
<div class="fgrid"><div><label>Sending by</label><select name="post_method" id="pm" onchange="showAddr()">{% for k, v in addr.items() %}<option value="{{k}}" {% if (t.post_method or 'courier') == k %}selected{% endif %}>{{v[0]}}</option>{% endfor %}</select></div></div>
{% for k, v in addr.items() %}<div class="addr" id="addr_{{k}}" style="display:none">{{ v[1]|join('<br>')|safe }}</div>{% endfor %}
<div><button type="button" class="btn small secondary" onclick="copyAddr()">Copy address</button> <span class="muted" id="cpmsg"></span></div>
<p style="margin:12px 0 2px"><b>Envelope checklist</b></p>
<label class="tick"><input type="checkbox" name="enc_w7" value="1" {% if t.enc_w7 %}checked{% endif %}> Original Form W-7 signed in ink by the applicant and by us</label>
<label class="tick"><input type="checkbox" name="enc_coa" value="1" {% if t.enc_coa %}checked{% endif %}> Form W-7-COA Certificate of Accuracy, signed and dated</label>
<label class="tick"><input type="checkbox" name="enc_id" value="1" {% if t.enc_id %}checked{% endif %}> Copies of the identity documents we reviewed (passport photo page etc.)</label>
<label class="tick"><input type="checkbox" name="enc_support" value="1" {% if t.enc_support %}checked{% endif %}> {{support}}</label>
<label class="tick" style="margin-top:14px"><input type="checkbox" name="label_done" value="1" {% if t.label_done %}checked{% endif %}> Postage label generated and envelope sent</label>
<div class="fgrid" style="margin-left:26px"><div><label>Carrier</label><input type="text" name="carrier" value="{{t.carrier}}" placeholder="e.g. FedEx, UPS, USPS Certified Mail"></div>
<div><label>Tracking number</label><input type="text" name="tracking_no" value="{{t.tracking_no}}"></div>
<div><label>Date posted (starts the 2-month reminder)</label><input type="date" name="posted_date" value="{{t.posted_date}}"></div></div>

</div>

<div class="tsec"><h3>C. IRS decision</h3>
<p class="muted" style="margin:2px 0 8px">The IRS takes about 2 months (8 weeks; up to 11 weeks between 15 January and 30 April). Status line for acceptance agents: {{phone}}.</p>
<div class="fgrid"><div><label>Outcome</label><select name="outcome">{% for k, v in outcomes.items() %}<option value="{{k}}" {% if (t.outcome or '') == k %}selected{% endif %}>{{v}}</option>{% endfor %}</select></div>
<div><label>Date of IRS letter</label><input type="date" name="outcome_date" value="{{t.outcome_date}}"></div>
<div><label>ITIN issued</label><input type="text" name="itin_number" value="{{t.itin_number}}" placeholder="9XX-XX-XXXX"></div></div>
<label>Notes (what the IRS asked for, calls made, etc.)</label><textarea name="outcome_notes" rows="2" style="width:100%">{{t.outcome_notes}}</textarea>
</div>
<input type="hidden" name="notes" value="{{t.notes}}">
<button class="btn" type="submit">Save progress</button>
</form>
{% if m.history %}<details style="margin-top:12px"><summary class="muted" style="cursor:pointer">History</summary><ul>{% for h in m.history %}<li>{{h.when|ukdate}} – {{stages.get(h.stage, h.stage)}} ({{h.staff}})</li>{% endfor %}</ul></details>{% endif %}
</div>

<div class="doclist">
<div>{% for x in m.files %}<div class="ditem{% if loop.first %} on{% endif %}" data-f="{{x.file}}" onclick="show(this)">
<b>{{x.label}}</b><span class="muted">{{x.file}}</span>
<div style="margin-top:6px"><a class="btn small secondary" href="/itin/job/{{m.job}}/file/{{x.file}}" onclick="event.stopPropagation()">⬇ Download</a></div></div>{% endfor %}</div>
<div id="pv"></div>
</div>
<script>
var base = '/itin/job/{{m.job}}/file/';
function show(el){
  document.querySelectorAll('.ditem').forEach(function(d){d.classList.remove('on')}); el.classList.add('on');
  document.getElementById('pv').innerHTML = '<iframe src="'+base+encodeURIComponent(el.getAttribute('data-f'))+'?inline=1"></iframe>';
}
var first = document.querySelector('.ditem'); if(first) show(first);
function showAddr(){ var v=document.getElementById('pm').value; document.querySelectorAll('[id^=addr_]').forEach(function(e){e.style.display = e.id=='addr_'+v ? 'inline-block' : 'none'}) }
function copyAddr(){ var v=document.getElementById('pm').value, el=document.getElementById('addr_'+v);
  navigator.clipboard.writeText(el.innerText).then(function(){document.getElementById('cpmsg').textContent='Copied'}) }
showAddr();
</script>
{% endblock %}"""
