"""
ITIN tab (v35a) – IRS Form W-7 + Form W-7 (COA) Certificate of Accuracy, prepared by Startitup Global Inc as a
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
if not (TPL_DIR / "fw7.pdf").exists() and (ROOT / "fw7.pdf").exists():
    TPL_DIR = ROOT                      # PDFs uploaded to the main folder of the repo instead – use them there
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "itin"
LOG_FILE = OUT / "log.json"
CAA_FILE = OUT / "caa.json"
_LOCK = threading.Lock()
_CFG = {"fn": None}

# Our Acceptance Agent details – defaults, editable (and saved) on the ITIN page.
CAA_DEFAULT = {"responsible_party": "Moaz Ali Siddiqui", "name_title": "Moaz Ali Siddiqui, COO",
               "company": "Startitup Global Inc", "ein": "42-2709229", "ptin": "P03481333", "ptin_expiry": "2026-12-31",
               "office_code": "00000001", "agreement_date": "2026-09-29", "phone": "406-605-9190", "fax": "806-615-2858",
               "notify_email": "accounting@startitup.global",
               "our_mail_1": "c/o Startitup Global Inc, 5904 Jessamine St, Ste A15", "our_mail_2": "Houston, TX 77081, USA"}
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
 "address_line1": "", "address_line2": "",    // only if an address is printed (e.g. national ID card): street;
                                               // then city, state/province, country, postcode
 "mrz_checked": true | false, "notes": ""}"""

_JSON_RULE = "Read ONLY what is printed. Never guess. Unknown values are \"\". Dates as YYYY-MM-DD. Return ONLY one JSON object.\n"
SCAN_PROMPTS = {
    "identity": SCAN_SYSTEM,
    "address": _JSON_RULE + """This is a proof of address (utility bill, bank statement, tenancy, tax letter). Return:
{"name_on_document": "", "address_line1": "",   // house/flat number and street (and area)
 "address_line2": "",                            // city, state/province, country and postcode in one line, country in English
 "document_type": "", "document_date": "", "notes": ""}""",
    "visa": _JSON_RULE + """This is a U.S. visa, I-20, I-94 record or passport entry stamp. Return:
{"name": "", "visa_type": "",        // e.g. B1/B2, F-1, H-1B
 "visa_number": "",                  // the visa foil number (red number) if shown
 "visa_expiry": "", "entry_date": "",// most recent date of entry into the United States, if shown
 "notes": ""}""",
    "tax_return": _JSON_RULE + """This is a U.S. federal tax return (e.g. Form 1040-NR or 1040). Return:
{"form": "", "tax_year": "", "names": [""],   // every taxpayer/spouse name as printed on the return
 "notes": ""}                                   // say if any signature or page seems missing""",
    "exception": _JSON_RULE + """This document supports an ITIN exception (bank letter, payer/withholding agent letter, partnership or LLC
agreement, employment letter, mortgage or property sale papers, Form 8288). Return:
{"document_type": "", "issuer": "", "name_of_applicant": "", "date": "",
 "suggested_exception": "",   // one of: Exception 1(a), Exception 1(b), Exception 1(c), Exception 1(d), Exception 2(a),
                              // Exception 2(b), Exception 2(c), Exception 3, Exception 4, Exception 5, T.D. 9363 – or ""
 "is_original_on_letterhead": true | false, "is_signed": true | false, "notes": ""}""",
    "us_person": _JSON_RULE + """This is a U.S. social security card, ITIN letter or similar for a U.S. citizen/resident. Return:
{"name": "", "ssn_or_itin": "", "notes": ""}  // number formatted 123-45-6789""",
    "prev_itin": _JSON_RULE + """This is an IRS letter assigning an ITIN (e.g. CP565) or showing an existing ITIN. Return:
{"itin": "", "first_name": "", "middle_name": "", "last_name": "", "letter_date": "", "notes": ""}  // itin as 9XX-XX-XXXX""",
}


def _norm_image(data: bytes) -> bytes:
    """Any photo format (WebP from WhatsApp, HEIC from iPhone if pillow-heif is installed, PNG, BMP, TIFF, GIF)
    -> upright RGB JPEG. PDFs are returned unchanged."""
    if data[:4] == b"%PDF":
        return data
    from PIL import Image, ImageOps
    try:
        import pillow_heif                      # optional – iPhone HEIC photos
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, "white")
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    im = im.convert("RGB")
    if max(im.size) > 3000:
        im.thumbnail((3000, 3000))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


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
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        try:
            data = _norm_image(data)            # WebP / HEIC / BMP ... -> JPEG
        except Exception:
            pass
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


def scan_passport(data: bytes, filename: str, kind: str = "identity") -> dict:
    cfg = _cfg()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic API key is set (Settings page or ANTHROPIC_API_KEY) – type the details by hand.")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(b).decode()}}
               for mt, b in _to_images(data, filename)]
    content.append({"type": "text", "text": "Read this document and return the JSON."})
    resp = client.messages.create(model=cfg.get("model") or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5"),
                                  max_tokens=900, system=SCAN_PROMPTS.get(kind, SCAN_SYSTEM),
                                  messages=[{"role": "user", "content": content}])
    raw = resp.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError("The AI reply could not be read – type the details by hand.")
    d = json.loads(m.group(0))
    return {k: (v if isinstance(v, (bool, list)) else str(v or "").strip()) for k, v in d.items()}


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
    # IRS W-7 instructions: "enter N/A in any section that doesn't apply – don't leave any section blank"
    if not a["foreign_1"] and not a["foreign_2"]:          # line 3 = home address abroad; usually same as line 2
        v["f1_15[0]"], v["f1_16[0]"] = a["mail_1"], a["mail_2"]
    for k in ("f1_21[0]", "f1_22[0]", "f1_37[0]", "f1_38[0]", "f1_39[0]"):     # 6b, 6c, 6g
        if not (v.get(k) or "").strip():
            v[k] = "N/A"
    return v


def w7_overlays(a: dict) -> list[tuple[str, str]]:
    """Text written over a field that is too small for it (comb date boxes)."""
    return [] if a.get("us_entry") else [("f1_27[0]", "Never entered the U.S.")]


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


def fill_irs(template: Path, values: dict, keep_pages: int | None = None, overlays=None) -> bytes:
    """Fill an IRS form. values are keyed by the END of the full field name (e.g. 'f1_07[0]' or
    'Row1[0].identity[0]'), so the long 'topmostSubform[0].Page1[0]...' prefix does not matter."""
    import pymupdf
    doc = pymupdf.open(template)
    if keep_pages:
        doc.select(list(range(keep_pages)))
    used, ticks = set(), []
    for page in doc:
        for w in page.widgets():
            key = next((k for k in values if w.field_name == k or w.field_name.endswith("." + k)), None)
            if key is None:
                continue
            v = values[key]
            if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
                # draw our own tick (vector lines) – some PDF viewers show the form's font glyph as a star
                w.field_value = "Off"
                if v:
                    ticks.append((page.number, w.rect))
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
    for pno, r in ticks:
        pg = doc[pno]
        s_ = min(r.width, r.height)
        x, y = r.x0 + (r.width - s_) / 2, r.y0 + (r.height - s_) / 2
        pg.draw_polyline([(x + s_ * .18, y + s_ * .55), (x + s_ * .42, y + s_ * .80), (x + s_ * .85, y + s_ * .18)],
                         color=(0, 0, 0), width=max(0.9, s_ * .13), overlay=True)
    for key, text in overlays or []:
        for page in doc:
            for w in page.widgets():
                if w.field_name.endswith("." + key):
                    r = w.rect
                    page.draw_rect(r, color=None, fill=(1, 1, 1), overlay=True)   # cover the printed "/  /"
                    page.insert_text((r.x0 + 1, r.y1 - 2), text, fontsize=5.5, fontname="helv", color=(0, 0, 0.55))
    buf = io.BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    doc.close()
    return buf.getvalue()


AGENT_FIELDS = ("f1_42[0]", "f1_43[0]", "f1_44[0]", "f1_45[0]", "f1_46[0]", "f1_47[0]", "f1_48[0]")   # W-7 Acceptance Agent box


def customer_copy_pdf(a: dict, caa: dict) -> bytes:
    """W-7 for the client's own records: our Acceptance Agent details (phone, fax, name, company, EIN, PTIN,
    office code) blanked and covered, the form flattened so it can't be edited, and a CUSTOMER COPY watermark."""
    import pymupdf
    v = w7_values(a, caa)
    hide = list(AGENT_FIELDS)
    if a.get("mail_choice") == "ours":           # line 2 is our office address – withhold it too
        hide += ["f1_13[0]", "f1_14[0]"]
    for k in hide:
        v[k] = ""
    doc = pymupdf.open(stream=fill_irs(TPL_DIR / "fw7.pdf", v, overlays=w7_overlays(a)), filetype="pdf")
    page = doc[0]
    rects = [w.rect for w in page.widgets() if any(w.field_name.endswith("." + k) for k in hide)]
    try:
        doc.bake()                               # form fields -> plain page content (not editable)
    except Exception:
        pass
    for r in rects:                              # grey "withheld" boxes over our details
        page.draw_rect(r, color=(0.55, 0.55, 0.55), fill=(0.86, 0.86, 0.86), width=0.4, overlay=True)
        page.insert_textbox(r, "WITHHELD", fontsize=6, fontname="helv", color=(0.35, 0.35, 0.35), align=1)
    for pg in doc:
        W, H = pg.rect.width, pg.rect.height
        for y in (H * 0.40, H * 0.74):
            pg.insert_text((W * 0.14, y), "CUSTOMER COPY", fontsize=54, fontname="hebo", color=(0.85, 0.12, 0.12),
                           fill_opacity=0.16, stroke_opacity=0.16, overlay=True,
                           morph=(pymupdf.Point(W * 0.14, y), pymupdf.Matrix(-28)))
        pg.insert_textbox(pymupdf.Rect(30, 8, W - 30, 30),
                          "CUSTOMER COPY - for your records only. NOT for submission to the IRS. Acceptance Agent details withheld.",
                          fontsize=8, fontname="hebo", color=(0.75, 0.1, 0.1), align=1, overlay=True)
    buf = io.BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    return buf.getvalue()


def id_copy_pdf(data: bytes, filename: str) -> bytes:
    """The uploaded ID scan as a PDF (images are placed on an A4/Letter page)."""
    import pymupdf
    if data[:4] == b"%PDF":
        return data
    data = _norm_image(data)                    # WhatsApp .webp, iPhone .heic, sideways photos etc.
    img = pymupdf.open(stream=data, filetype="jpg")
    rect = img[0].rect
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    scale = min(540 / rect.width, 720 / rect.height, 1.5)
    w, h = rect.width * scale, rect.height * scale
    page.insert_image(pymupdf.Rect(36, 36, 36 + w, 36 + h), stream=data)
    buf = io.BytesIO()
    pdf.save(buf, deflate=True)
    return buf.getvalue()


# ----------------------------------------------------------------------------- supporting documents

# key: (label, what it is / tips, AI reader kind or None, COA row it proves or None)
DOC_TYPES = {
    "engagement": ("Our ITIN application / engagement form – signed by the client",
                   "Client's consent for us to prepare and submit the W-7", None, None),
    "passport": ("Passport – photo page (plus US visa pages if any)",
                 "Fills name, date of birth, sex, citizenship, place of birth and passport details", "identity", "passport"),
    "national_id": ("National ID card (e.g. CNIC) – front and back",
                    "Second ID when there is no passport; can also fill the home address", "identity", "national_id"),
    "birth_cert": ("Civil birth certificate", "Needed for children under 18 without a passport", "identity", "birth_cert"),
    "other_id": ("Other ID – driving licence, voter card, military ID",
                 "Extra ID when there is no passport (tick the right COA row yourself)", "identity", None),
    "address": ("Proof of address – utility bill or bank statement (last 3 months)",
                "Fills line 2 mailing address", "address", None),
    "visa": ("US visa / I-20 / I-94 / US entry stamp", "Fills 6c visa and the date of entry", "visa", "visa"),
    "tax_return": ("US federal tax return (e.g. Form 1040-NR) – goes behind the W-7",
                   "AI checks the names match the passport", "tax_return", None),
    "exception": ("Exception documents – bank / payer letter, partnership agreement, mortgage or property papers",
                  "AI suggests which exception (box h) applies", "exception", None),
    "us_person": ("US spouse / parent – SSN card or ITIN letter", "Fills name and SSN/ITIN for boxes d and e", "us_person", None),
    "residency": ("Dependant's US residency proof – US school or medical record, US ID, US bank statement",
                  "Required by the IRS for dependants (box d)", None, None),
    "prev_itin": ("Old ITIN letter (CP565) – renewals or previous ITIN", "Fills line 6e/6f", "prev_itin", None),
    "poa": ("Form 2848 power of attorney / court guardianship papers", "Needed when someone signs for the applicant", None, None),
    "signed_w7": ("Signed W-7 and COA – scan for our records", "Upload after signing", None, None),
    "other": ("Anything else", "Any other supporting document", None, None),
}
LEVELS = {"req": "Required", "cond": "Required for this application", "opt": "Optional", "na": "Not needed"}


def doc_rules(a: dict) -> dict:
    """key -> (level, why). Mirrors docRules() in the page script – keep both in step."""
    r = set(a.get("reasons") or [])
    pp = (a.get("id_type") or "passport") == "passport"
    ret = r & set("bcdefg")
    out = {
        "engagement": ("na", "Not needed – manual filing") if a.get("manual") else ("req", "Needed before we post"),
        "passport": ("req", "Proves identity and foreign status on its own") if pp else ("opt", ""),
        "national_id": ("opt", "") if pp else ("cond", "No passport – at least two IDs, one with a photo"),
        "birth_cert": ("cond", "Dependant without a passport") if (not pp and "d" in r) else ("opt", ""),
        "other_id": ("opt", "" if pp else "Use as the second ID if needed"),
        "address": ("opt", "Saves typing the address"),
        "visa": ("cond", "Boxes f and g need the US visa") if r & {"f", "g"} else ("opt", ""),
        "tax_return": (("na", "Exception claimed in box h instead") if "h" in r else
                       ("opt", "Not needed for a first-time (new) application – add it if there is one")
                       if ret and a.get("app_type") != "renew" else
                       ("cond", "Boxes b–g need the tax return") if ret else ("opt", "")),
        "exception": ("cond", "Box h – the IRS needs proof of the exception") if "h" in r else ("na", ""),
        "us_person": ("opt", "Fills the US person's name and number") if r & {"d", "e"} else ("na", ""),
        "residency": ("cond", "Box d – dependants must prove US residency") if "d" in r else ("na", ""),
        "prev_itin": ("opt", "Has / had an ITIN") if (a.get("app_type") == "renew" or a.get("prev") == "yes") else ("na", ""),
        "poa": ("cond", "Someone else signs for the applicant") if a.get("delegate_rel") in ("poa", "guardian") else ("na", ""),
        "signed_w7": ("opt", "Upload after signing"),
        "other": ("opt", ""),
    }
    return out


def uploaded_kinds(meta: dict) -> dict:
    out = {}
    for x in meta.get("files", []):
        if x.get("kind"):
            out.setdefault(x["kind"], []).append(x)
    return out


def missing_required(meta: dict) -> list[str]:
    up = uploaded_kinds(meta)
    return [DOC_TYPES[k][0] for k, (lvl, _w) in doc_rules(meta["a"]).items() if lvl in ("req", "cond") and k not in up]


def save_docs(d: Path, tag: str, files_list: list, getter) -> int:
    """Save every uploaded document as PDF in the job folder. getter(key) -> list of FileStorage."""
    n = 0
    for key, (label, *_rest) in DOC_TYPES.items():
        for up in getter(key):
            if not up or not up.filename:
                continue
            raw = up.read()
            if not raw:
                continue
            i = 1 + sum(1 for x in files_list if x.get("kind") == key)
            fname = f"{key}_{tag}_{i}.pdf"
            try:
                (d / fname).write_bytes(id_copy_pdf(raw, up.filename))
            except Exception:
                raise RuntimeError(f"\"{up.filename}\" could not be read – upload it as a PDF, JPG, PNG or WebP photo.")
            files_list.append({"file": fname, "label": label.split(" – ")[0] + (f" ({i})" if i > 1 else ""),
                               "kind": key, "orig": up.filename[:120]})
            n += 1
    return n


# ----------------------------------------------------------------------------- tracking: sign -> post -> IRS decision

IRS_ADDR = {   # Form W-7 mailing addresses (IRS "How do I apply for an ITIN?")
    "usps": ("U.S. Postal Service", ["Internal Revenue Service", "Austin Service Center", "ITIN Operation",
                                     "P.O. Box 149342", "Austin, TX 78714-9342"]),
    "courier": ("Private delivery service (FedEx / UPS / DHL)", ["Internal Revenue Service", "ITIN Operation",
                 "Mail Stop 6090-AUSC", "3651 S. Interregional, Highway 35", "Austin, TX 78741-0000"]),
}
IRS_STATUS_PHONE = "512-433-5966"     # W-7 status line for acceptance agents (IRS letter 5614C)

STAGES = {"kyc_sent": "KYC link sent – waiting for the client", "kyc_received": "KYC received – review and create the W-7",
          "prepared": "Forms prepared – waiting for signatures", "ready": "Signed – ready to post",
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
        if a.get("app_type") != "renew":
            return "The applicant's U.S. federal tax return, if there is one (W-7 on top, return behind it)"
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
    st = meta.get("status") or ""
    if st in ("kyc_sent", "kyc_received") and out != "withdrawn":
        k = meta.get("kyc") or {}
        info = {"stage": st, "stage_label": STAGES[st], "signed": False, "posted": None, "expected": None, "days": None,
                "overdue": False, "alert": "", "closed": False}
        if st == "kyc_received":
            info["alert"] = "Client has submitted the KYC – review it and create the W-7."
        else:
            exp, made = _d(k.get("expires")), _d((k.get("created") or "")[:10])
            if exp and exp < today:
                info["alert"] = "KYC link has expired – extend it and remind the client."
            elif made and (today - made).days >= 7:
                info["alert"] = f"Client hasn't completed the KYC after {(today - made).days} days – send a reminder."
        return info
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


def use_our_mail(a: dict, caa: dict | None = None) -> dict:
    """Line 2 = our office (c/o Startitup). Line 3 must then hold the client's full foreign street address
    (IRS W-7 instructions, line 3 caution: no c/o address on line 2 if line 3 is just a country)."""
    caa = caa or load_caa()
    a["mail_choice"] = "ours"
    a["mail_1"], a["mail_2"] = caa.get("our_mail_1", ""), caa.get("our_mail_2", "")
    return a


def applicant_from_form(f) -> tuple[dict, list[str]]:
    a = {k: (f.get(k) or "").strip() for k in TEXT_KEYS}
    a["mail_choice"] = "ours" if f.get("mail_choice") == "ours" else "client"
    if a["mail_choice"] == "ours":
        use_our_mail(a)
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
        e.append("Line 2: both lines of the mailing address are required." if a["mail_choice"] != "ours" else
                 "Line 2: our mailing address is not set – fill it in under \"Our Acceptance Agent details\" and press Save.")
    if a["mail_choice"] == "ours" and not (a["foreign_1"] and a["foreign_2"]):
        e.append("Line 3: when the IRS letters go to our address, enter the client's full foreign home address "
                 "(street and city/country) on line 3 – otherwise the IRS may reject the application.")
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


MANUAL_HOW = {"in_person": "In person – originals seen", "certified": "Certified copies received",
              "video": "Video call + originals seen"}


def manual_from_form(f, errs: list) -> dict | None:
    """Manual filing (v38a): KYC and signatures collected offline by staff – no client KYC / e-sign link."""
    if not f.get("manual"):
        return None
    man = {k: (f.get(f"m_{k}") or "").strip() for k in
           ("kyc_date", "kyc_by", "kyc_how", "applicant_date", "rp_date")}
    man["confirm"] = bool(f.get("m_confirm"))
    if man["kyc_how"] not in MANUAL_HOW:
        man["kyc_how"] = "in_person"
    today = dt.date.today()
    for k, lbl, need in (("kyc_date", "the date the KYC / ID check was done", True),
                         ("applicant_date", "the date the applicant signed the W-7", False),
                         ("rp_date", "the date the responsible party signed", False)):
        v = _d(man[k])
        if need and not man[k]:
            errs.append(f"Manual filing: enter {lbl}.")
        elif man[k] and not v:
            errs.append(f"Manual filing: {lbl} is not a valid date.")
        elif v and v > today:
            errs.append(f"Manual filing: {lbl} can't be in the future.")
    if not man["kyc_by"]:
        errs.append("Manual filing: enter who did the KYC / ID check.")
    if not man["confirm"]:
        errs.append("Manual filing: tick the confirmation that the ID originals were seen.")
    return man


def apply_manual(meta: dict, man: dict):
    """Record the offline KYC / signatures on the application and tick tracker section A to match."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    staff = meta.get("staff") or "admin"
    meta["manual"] = dict(man, marked_by=staff, when=now)
    meta.setdefault("a", {})["manual"] = man      # doc_rules(): no engagement form needed for manual filing
    t = meta.get("track") or {}
    before = track_info(meta)["stage"] if meta.get("track") else ""
    t.update(consent=True, consent_date=man["kyc_date"] or t.get("consent_date", ""))   # consent taken at the KYC meeting
    if man.get("applicant_date"):
        t.update(applicant_signed=True, applicant_signed_date=man["applicant_date"])
    if man.get("rp_date"):
        t.update(rp_signed=True, rp_signed_date=man["rp_date"])
    for k in T_BOOL:
        t.setdefault(k, False)
    for k in T_TEXT:
        t.setdefault(k, "")
    if t.get("post_method") not in IRS_ADDR:
        t["post_method"] = "courier"
    meta["track"] = t
    meta["status"] = ""                          # out of kyc_sent / kyc_received if it came from a KYC draft
    hist = meta.setdefault("history", [])
    hist.append({"when": now, "stage": "manual filing – KYC & signatures done offline", "staff": staff})
    after = track_info(meta)["stage"]
    if after != before and after != "prepared":
        hist.append({"when": now, "stage": after, "staff": staff})


@bp.post("/itin/job/<job>/manual")
def itin_manual(job):
    """Switch an existing application to manual filing (e.g. a KYC link the client never completed)."""
    d = _job_dir(job)
    errs: list = []
    man = manual_from_form(request.form, errs)
    if not man:
        flash("⚠ Tick \"Manual filing\" first.")
        return redirect(f"/itin/job/{job}#manualcard")
    if errs:
        for e in errs:
            flash("⚠ " + e)
        return redirect(f"/itin/job/{job}#manualcard")
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        m["staff"] = m.get("staff") or session.get("user") or "admin"
        apply_manual(m, man)
        (d / "job.json").write_text(json.dumps(m, indent=1))
    _ALERT_CACHE["at"] = 0
    flash("✍️ Manual filing recorded – " + track_info(m)["stage_label"] + ".")
    return redirect(f"/itin/job/{job}#track")


@bp.get("/itin")
def itin_home():
    missing = [t for t in ("fw7.pdf", "fw7coa.pdf") if not (TPL_DIR / t).exists()]
    caa = load_caa()
    caa_missing = [lbl for k, lbl in (("responsible_party", "responsible party"), ("name_title", "name and title"),
                                      ("ein", "EIN"), ("office_code", "office code"), ("agreement_date", "agreement date"))
                   if not caa.get(k)]
    jobs = all_jobs()
    return render_template_string(PAGE, manual_pref=request.args.get("manual") == "1", reasons=REASONS, id_types=ID_TYPES, coa_rows=COA_ROWS, doc_types=DOC_TYPES, levels=LEVELS, eng=load_engage(), kyc_reasons=KYC_REASONS, caa=caa,
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
    up = request.files.get("file") or request.files.get("id_file")
    key = request.form.get("doc") or "passport"
    kind = (DOC_TYPES.get(key) or (None, None, "identity"))[2] or "identity"
    if not up or not up.filename:
        return jsonify(ok=False, error="Choose the file first.")
    data = up.read()
    if len(data) > 15_000_000:
        return jsonify(ok=False, error="File is larger than 15 MB – please use a smaller scan.")
    try:
        return jsonify(ok=True, doc=key, kind=kind, data=scan_passport(data, up.filename, kind))
    except Exception as ex:
        return jsonify(ok=False, error=f"Could not read the document: {ex}")


@bp.post("/itin/generate")
def itin_generate():
    a, errs = applicant_from_form(request.form)
    man = manual_from_form(request.form, errs)
    if man:
        a["manual"] = man
    caa = load_caa()
    if not caa.get("responsible_party") or not caa.get("ein") or not caa.get("office_code"):
        errs.append("Fill in and save our Acceptance Agent details first (responsible party, EIN, office code).")
    from_job = request.form.get("from_job") or ""
    prev_meta = None
    if from_job:
        prev_meta = json.loads((_job_dir(from_job) / "job.json").read_text())
    have = set(uploaded_kinds(prev_meta)) if prev_meta else set()
    has = lambda k: k in have or any(f and f.filename for f in request.files.getlist(f"doc_{k}"))
    if a["id_type"] == "passport" and not has("passport"):
        errs.append("Upload the passport (Documents, step 1).")
    if a["id_type"] != "passport" and sum(has(k) for k in ("national_id", "birth_cert", "other_id", "visa")) < 2:
        errs.append("No passport: upload at least two identity documents (e.g. national ID card + birth certificate).")
    if errs:
        for e in errs:
            flash("⚠ " + e)
        return render_template_string(PAGE, reasons=REASONS, id_types=ID_TYPES, coa_rows=COA_ROWS, doc_types=DOC_TYPES, levels=LEVELS, eng=load_engage(), kyc_reasons=KYC_REASONS, caa=caa, caa_missing=[],
                                      missing=[], log=_load_log()[:40], prefill=a, from_job=from_job,
                                      existing=uploaded_kinds(prev_meta) if prev_meta else {}, review=prev_meta,
                                      has_ai=bool(_cfg().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")))
    job = from_job or uuid.uuid4().hex[:12]
    d = OUT / job
    d.mkdir(parents=True, exist_ok=True)
    tag = _safe(f"{a['last']}_{a['first']}")
    files = []
    if prev_meta:
        for x in prev_meta.get("files", []):
            if x.get("kind"):
                files.append(x)                      # documents the client uploaded stay with the application
            else:
                (d / x["file"]).unlink(missing_ok=True)   # old W-7 / COA are recreated below
    try:
        (d / f"W-7_{tag}.pdf").write_bytes(fill_irs(TPL_DIR / "fw7.pdf", w7_values(a, caa), overlays=w7_overlays(a)))
        files.append({"file": f"W-7_{tag}.pdf", "label": "Form W-7 – Application for ITIN"})
        (d / f"W-7-COA_{tag}.pdf").write_bytes(fill_irs(TPL_DIR / "fw7coa.pdf", coa_values(a, caa), keep_pages=1))
        files.append({"file": f"W-7-COA_{tag}.pdf", "label": "Form W-7-COA – Certificate of Accuracy"})
        save_docs(d, tag, files, lambda k: request.files.getlist(f"doc_{k}"))
        files.sort(key=lambda x: 1 if x.get("kind") else 0)    # W-7 and COA first, then the documents
    except Exception as ex:
        flash(f"⚠ Could not create the forms: {ex}")
        return redirect("/itin")
    name = " ".join(x for x in (a["first"], a["middle"], a["last"]) if x)
    meta = {"job": job, "when": dt.datetime.now().isoformat(timespec="seconds"), "applicant": name, "a": a,
            "staff": session.get("user") or os.environ.get("MAILSORT_USER", "admin"), "files": files,
            "caa": {k: caa.get(k, "") for k in ("company", "responsible_party", "office_code")}}
    if prev_meta:   # keep the KYC, engagement and tracker history of a client-submitted application
        for k in ("kyc", "engage", "track", "history"):
            if prev_meta.get(k):
                meta[k] = prev_meta[k]
        meta["when"] = prev_meta.get("when", meta["when"])
        meta.setdefault("history", []).append({"when": dt.datetime.now().isoformat(timespec="seconds"), "stage": "prepared",
                                               "staff": meta["staff"]})
    if man:
        apply_manual(meta, man)
    (d / "job.json").write_text(json.dumps(meta, indent=1))
    if not prev_meta:
        _append_log({"job": job, "when": meta["when"], "applicant": name, "reasons": a["reasons"], "app_type": a["app_type"],
                     "client_ref": a["client_ref"], "staff": meta["staff"], "nfiles": len(files)})
    else:
        with _LOCK:
            log = _load_log()
            for x in log:
                if x["job"] == job:
                    x.update(applicant=name, reasons=a["reasons"], app_type=a["app_type"], nfiles=len(files))
            LOG_FILE.write_text(json.dumps(log, indent=1))
    _ALERT_CACHE["at"] = 0
    if man:
        ti = track_info(meta)
        flash("✍️ Manual filing recorded – " + ti["stage_label"] + ".")
        if man.get("applicant_date"):
            flash("⚠ Post the W-7 the applicant actually signed in ink. If they signed a blank or hand-filled W-7, print this one and get it signed again.")
    return redirect(f"/itin/job/{job}")


@bp.get("/itin/job/<job>")
def itin_job(job):
    d = _job_dir(job)
    m = json.loads((d / "job.json").read_text())
    return render_template_string(JOB_PAGE, m=m, t=m.get("track") or {}, ti=track_info(m), reasons=REASONS,
                                  doc_types=DOC_TYPES, rules=doc_rules(m["a"]), levels=LEVELS, up=uploaded_kinds(m),
                                  missing_docs=missing_required(m), missing_docs_keys=[], eng=load_engage(),
                                  base_url=(request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0] + "://" + request.host,
                                  addr=IRS_ADDR, outcomes=OUTCOMES, support=support_doc(m["a"]), phone=IRS_STATUS_PHONE,
                                  today=dt.date.today().isoformat(), ptin_msg=ptin_alert(load_caa()), stages=STAGES)


@bp.post("/itin/job/<job>/upload")
def itin_upload(job):
    d = _job_dir(job)
    key = request.form.get("doc") or ""
    if key not in DOC_TYPES:
        flash("⚠ Choose which document you are uploading.")
        return redirect(f"/itin/job/{job}#docs")
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        tag = _safe(f"{m['a']['last']}_{m['a']['first']}")
        try:
            n = save_docs(d, tag, m["files"], lambda k: request.files.getlist("files") if k == key else [])
        except Exception as ex:
            flash(f"⚠ {ex}")
            return redirect(f"/itin/job/{job}#docs")
        (d / "job.json").write_text(json.dumps(m, indent=1))
    _ALERT_CACHE["at"] = 0
    flash(f"{n} file(s) added – {DOC_TYPES[key][0].split(' – ')[0]}." if n else "⚠ No file was chosen.")
    return redirect(f"/itin/job/{job}#docs")


@bp.post("/itin/job/<job>/remove")
def itin_remove(job):
    d = _job_dir(job)
    fname = request.form.get("file") or ""
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        hit = next((x for x in m["files"] if x["file"] == fname and x.get("kind")), None)   # forms can't be removed
        if hit:
            m["files"].remove(hit)
            (d / fname).unlink(missing_ok=True)
            (d / "job.json").write_text(json.dumps(m, indent=1))
    flash("Document removed." if hit else "⚠ That file can't be removed.")
    return redirect(f"/itin/job/{job}#docs")


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
        miss = missing_required(m)
        if t["label_done"] and miss:
            errs.append("Posting is blocked – upload the required documents first: " + "; ".join(x.split(" – ")[0] for x in miss) + ".")
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


@bp.get("/itin/job/<job>/customer-copy.pdf")
def itin_customer_copy(job):
    d = _job_dir(job)
    m = json.loads((d / "job.json").read_text())
    if m.get("status") in ("kyc_sent", "kyc_received"):
        abort(404)
    pdf = customer_copy_pdf(m["a"], load_caa())
    return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=request.args.get("inline") != "1",
                     download_name=f"W-7_customer_copy_{_safe(m['applicant'])}.pdf")


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


def register(app, cfg_fn=None, send_fn=None):
    _CFG["fn"] = cfg_fn
    _SEND["fn"] = send_fn
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
.doctab td{vertical-align:top}.doctab input[type=file]{font-size:12px;width:100%}
.lvl{display:inline-block;border-radius:10px;padding:2px 9px;font-size:12px;font-weight:700;white-space:nowrap}
.lvl.req{background:#fee2e2;color:#991b1b}.lvl.cond{background:#ffedd5;color:#9a3412}.lvl.opt{background:#e5e7eb;color:#374151}.lvl.na{background:#f3f4f6;color:#9ca3af}
.hint{background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:8px 12px;margin:6px 0 10px;font-size:13px;line-height:1.5}
.ln{display:block;margin-top:16px;font-weight:700;color:#555;font-size:12px}
.radios label{display:inline-flex;gap:6px;align-items:center;margin-right:18px;font-weight:600;font-size:14px}
.radios input{width:16px;height:16px}
table.coa td,table.coa th{text-align:center}table.coa td:first-child,table.coa th:first-child{text-align:left}
table.coa input{width:16px;height:16px}
tr.manualonly{display:none}
</style>
<div class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center"><h1 style="margin:0">ITIN application (Form W-7)</h1>
<div><a class="btn" href="/itin/guide">📘 Staff guide</a> <a class="btn secondary" href="/itin/guide#ask">What to ask the client</a></div></div>
<p class="muted">Prepares IRS Form W-7 and the Form W-7-COA Certificate of Accuracy for Startitup Global Inc as a Certifying
Acceptance Agent. Upload the applicant's passport, let the AI read it, then check and complete the rest. The official IRS
forms are used as published – only the fields are filled. Signatures and signing dates are left blank to sign by hand.</p>
{% if missing %}<div class="bad">Template file(s) missing from the <b>itin_templates</b> folder on GitHub: {{ missing|join(', ') }}</div>{% endif %}
{% if caa_missing %}<div class="bad">Our Acceptance Agent details are incomplete ({{ caa_missing|join(', ') }}) – fill them in below and press Save.</div>{% endif %}
{% if ptin_msg %}<div class="bad">⏰ <b>PTIN renewal:</b> {{ptin_msg}}</div>{% endif %}
{% if not has_ai %}<div class="warn">No Anthropic API key is set, so passport reading is off – type the details by hand.</div>{% endif %}
</div>

{% if review %}{% set k = review.kyc or {} %}{% set ai = k.ai or {} %}
<div class="card" style="border:2px solid #2563eb"><h2 style="margin-top:0">Reviewing the KYC from {{review.applicant}}</h2>
<p>The client's answers and documents are filled in below. <b>Check every field against the documents</b>, choose the reason box(es), then press <b>Create W-7</b> at the bottom.
The documents the client uploaded stay with the application – you don't need to upload them again.</p>
{% if ai.status == 'running' %}<div class="warn">⏳ The AI is still reading the documents – <a href="/itin/review/{{from_job}}">refresh in a minute</a> to see what it filled in.</div>{% endif %}
{% if ai.filled %}<div class="ok">AI filled: {{ ai.filled|join('; ') }}.</div>{% endif %}
{% if ai.warnings %}<div class="warn"><b>Check these:</b><ul style="margin:6px 0 0">{% for w in ai.warnings %}<li>{{w}}</li>{% endfor %}</ul></div>{% endif %}
{% set ans = k.answers or {} %}<p class="muted" style="margin-bottom:0"><b>Client said they need an ITIN because:</b>
{% for key, txt, box in kyc_reasons if ans.get('reason_' ~ key) %}<br>• {{txt}} → suggested box <b>{{box}}</b>{% endfor %}
{% if ans.other_reason %}<br>• "{{ans.other_reason}}"{% endif %}
{% if ans.had_itin == 'yes' %}<br>• Had an ITIN before: {{ans.old_itin or 'number not given'}} – consider <b>Renew</b>.{% endif %}</p>
<p style="margin-bottom:0"><a class="btn small secondary" href="/itin/job/{{from_job}}">← Back to the application</a></p></div>
{% else %}
<div class="card" id="kyc"><h2 style="margin-top:0">📨 New client – send a KYC link</h2>
<p class="muted" style="margin-top:0">The client gets one link to fill in their details, upload their documents and sign our engagement form on their phone.
When they submit, the application appears below as <b>"KYC received"</b> with everything filled in by AI – you check it and create the W-7.</p>
<form method="post" action="/itin/kyc/new"><div class="fgrid">
<div><label>Client first name</label><input type="text" name="first" required></div>
<div><label>Client last name</label><input type="text" name="last" required></div>
<div><label>Client email</label><input type="email" name="email" placeholder="to email the link"></div>
<div><label>WhatsApp number (optional)</label><input type="text" name="whatsapp" placeholder="+92 300 1234567"></div>
<div><label>Fee on the engagement form</label><input type="text" name="fee" value="{{eng.default_fee}}" placeholder="e.g. USD 250" required></div>
<div><label>Our reference (optional)</label><input type="text" name="client_ref"></div></div>
<label class="tick"><input type="checkbox" name="send_email" value="1" checked> Email the link to the client now</label>
<button class="btn" type="submit">Create KYC link</button></form></div>
<div class="card" id="manualstart" style="border:2px solid #16a34a"><h2 style="margin-top:0">✍️ Manual filing – KYC &amp; signatures already done offline</h2>
<p class="muted" style="margin-top:0">Use this when you have met the client (or checked their originals) yourself and collected the signatures on paper – no KYC link is sent.
Fill in the form below as normal, upload the documents, then complete <b>step 5 – Manual filing</b>. No engagement form is needed.
The tracker ticks client consent (and the signatures, if you enter their dates) automatically.</p>
<div class="btnrow"><a class="btn" href="/itin?manual=1#docs" onclick="if(window.setManual){setManual(true);document.getElementById('docs').scrollIntoView();return false}">Start a manual application ↓</a></div></div>
{% endif %}

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
 <div><label>Email us when a client submits KYC</label><input type="text" name="notify_email" value="{{caa.notify_email}}"></div>
 <div><label>Our mailing address for W-7 line 2 – street</label><input type="text" name="our_mail_1" value="{{caa.our_mail_1}}"></div>
 <div><label>Our mailing address for W-7 line 2 – city, state, ZIP, country</label><input type="text" name="our_mail_2" value="{{caa.our_mail_2}}"></div>
</div><div style="margin-top:12px"><button class="btn" type="submit">Save Acceptance Agent details</button></div></form></details></div>

{% if not review %}<div class="card" id="engagement"><details><summary style="cursor:pointer;font-weight:700;font-size:16px">Client engagement form wording (signed online by clients)</summary>
<p class="muted">This is the text clients see and sign from the link on each application. {fee} is replaced by the fee entered for that client. Keep the numbered headings – a blank line separates sections and lines starting with "- " become bullet points.</p>
<form method="post" action="/itin/engagement-terms">
<div class="fgrid"><div><label>Title</label><input type="text" name="title" value="{{eng.title}}"></div>
<div><label>Default fee (pre-filled on each application)</label><input type="text" name="default_fee" value="{{eng.default_fee}}" placeholder="e.g. USD 250"></div></div>
<label>Terms</label><textarea name="terms" rows="18" style="width:100%;font-size:13px">{{eng.terms}}</textarea>
<div style="margin-top:10px"><button class="btn" type="submit">Save wording</button></div></form>
<form method="post" action="/itin/engagement-terms/reset" onsubmit="return confirm('Replace the wording with the standard text?')" style="margin-top:8px"><button class="btn small secondary">Reset to standard text</button></form>
</details></div>{% endif %}

<form method="post" action="/itin/generate" enctype="multipart/form-data" id="wf" onsubmit="return beforeSubmit()">
<input type="hidden" name="from_job" value="{{from_job or ''}}">{% set ex = existing or {} %}
<div class="card" id="docs"><h2><span class="step">1</span>Documents</h2>
<p class="hint">Before you start: send the client the <a href="/itin/guide#ask">information request</a>. Upload each document with its own button.
The <b>Needed?</b> column changes as you tick the reason boxes below. Press <b>Read with AI</b> to fill the form from that document – then check every field.
Scans: colour, all four corners visible, PDF/JPG/PNG. Documents can also be added later on the application page. <a href="/itin/guide#docs">More</a></p>
{% if prefill and not from_job %}<div class="warn">Please choose the files again – browsers clear uploads after a correction.</div>{% endif %}
<table class="doctab"><tr><th>Document</th><th style="width:190px">Needed?</th><th style="width:250px">Upload</th><th style="width:120px"></th></tr>
{% for k, d in doc_types.items() %}<tr id="row_{{k}}" {% if k == 'signed_w7' %}class="manualonly"{% endif %} data-has="{{ '1' if ex.get(k) else '' }}"><td><b>{{d[0]}}</b><br><span class="muted">{{d[1]}}</span>
{% if ex.get(k) %}<div style="color:#166534;font-size:13px;margin-top:4px">✔ Already uploaded: {% for x in ex[k] %}<a href="/itin/job/{{from_job}}/file/{{x.file}}?inline=1" target="_blank">{{x.orig or x.file}}</a>{% if not loop.last %}, {% endif %}{% endfor %}</div>{% endif %}<div id="msg_{{k}}"></div></td>
<td><span class="lvl" id="lvl_{{k}}"></span><br><span class="muted" style="font-size:12px" id="why_{{k}}"></span></td>
<td><input type="file" name="doc_{{k}}" id="f_{{k}}" accept="image/*,application/pdf" {% if k in ('exception','other','residency','tax_return') %}multiple{% endif %} onchange="picked('{{k}}')"></td>
<td>{% if d[2] %}<button type="button" class="btn small" id="ai_{{k}}" onclick="readDoc('{{k}}')" {% if not has_ai %}disabled title="No Anthropic API key set"{% endif %}>Read with AI</button>{% endif %}</td></tr>{% endfor %}
</table>
<p class="muted" style="margin:6px 0 0"><span class="lvl req">Required</span> always needed · <span class="lvl cond">Required for this application</span> needed because of the boxes ticked ·
<span class="lvl opt">Optional</span> helps fill the form · <span class="lvl na">Not needed</span>. Posting to the IRS is blocked until every required document is uploaded.</p>
</div>

<div class="card"><h2><span class="step">2</span>Application type &amp; reason</h2>
<p class="hint"><b>Most clients:</b> they live abroad and must file a US tax return (e.g. 1040-NR for their US company) → tick <b>b</b> only and attach the return.
Spouse of a US person → <b>e</b>. No tax return but a bank/payer letter → <b>h</b> with the exception. "Only for Stripe/PayPal/bank" is not a reason by itself.
<a href="/itin/guide#reason">Which box for which client</a> · <a href="/itin/guide#exceptions">Exceptions list</a></p>
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
<p class="hint">Copy names exactly as on the passport. Leave a field blank if it doesn't apply – the form prints <b>N/A</b> for 6b, 6c and 6g, copies line 2 into line 3 when line 3 is blank,
and prints "Never entered the U.S." when there is no entry date. <a href="/itin/guide#fields">Field-by-field help</a></p>
<span class="ln">1a Name</span>
<div class="fgrid"><div><label>First name</label><input type="text" name="first" id="first" value="{{p.first}}"></div>
<div><label>Middle name</label><input type="text" name="middle" id="middle" value="{{p.middle}}"></div>
<div><label>Last name</label><input type="text" name="last" id="last" value="{{p.last}}"></div></div>
<details {% if p.birth_last %}open{% endif %}><summary style="cursor:pointer;margin:8px 0" class="muted">1b Name at birth (only if different)</summary>
<div class="fgrid"><div><label>First name</label><input type="text" name="birth_first" value="{{p.birth_first}}"></div>
<div><label>Middle name</label><input type="text" name="birth_middle" value="{{p.birth_middle}}"></div>
<div><label>Last name</label><input type="text" name="birth_last" value="{{p.birth_last}}"></div></div></details>

<span class="ln">2 Mailing address (where the IRS sends the ITIN letter)</span>
<div class="radios" style="margin:4px 0 6px"><label><input type="radio" name="mail_choice" value="client" id="mc_client" {% if p.mail_choice != 'ours' %}checked{% endif %} onchange="mailChoice()"> Customer's own mailing address</label>
<label><input type="radio" name="mail_choice" value="ours" id="mc_ours" {% if p.mail_choice == 'ours' %}checked{% endif %} onchange="mailChoice()"> Our address ({{ caa.our_mail_1 if caa else '' }}, {{ caa.our_mail_2 if caa else '' }})</label></div>
<div class="fgrid"><div><label>Street address, apartment number</label><input type="text" name="mail_1" id="mail_1" value="{{p.mail_1}}"></div>
<div><label>City, state/province, country and postcode</label><input type="text" name="mail_2" id="mail_2" value="{{p.mail_2}}"></div></div>
<div class="warn" id="ours_note" style="display:none;margin-top:6px">IRS letters (ITIN notice, returned documents) will come to our office. <b>Line 3 must have the client's full foreign home address</b> – street and city/country, not just the country – or the IRS may reject it.</div>
<span class="ln">3 Foreign (non-U.S.) home address – leave blank if the same as line 2 (it is copied). Must be filled if line 2 is a US address or our address. No P.O. box.</span>
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
<div><label>Date of entry into the U.S. (blank = never entered)</label><input type="date" name="us_entry" value="{{p.us_entry}}"></div></div>

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
<p class="hint">Tick only documents our responsible party has seen as originals or certified copies. For dependants we can only certify a passport or birth certificate. <a href="/itin/guide#docs">More</a></p>
<p class="muted" style="margin-top:0">A passport alone proves both identity and foreign status. Without a passport, tick two or more documents.</p>
<table class="coa"><tr><th>Supporting documentation</th><th style="width:90px">Identity</th><th style="width:110px">Foreign status</th></tr>
{% for k, r in coa_rows.items() %}<tr><td>{{r[0]}}</td><td><input type="checkbox" name="coa_id_{{k}}" id="cid_{{k}}" value="1" {% if k in (p.coa_identity or []) %}checked{% endif %}></td>
<td>{% if r[2] %}<input type="checkbox" name="coa_fs_{{k}}" id="cfs_{{k}}" value="1" {% if k in (p.coa_foreign or []) %}checked{% endif %}>{% else %}<span class="muted">—</span>{% endif %}</td></tr>{% endfor %}</table>
</div>

{% set pm = p.manual or {} %}
<div class="card" id="manual"><h2><span class="step">5</span>Manual filing – KYC &amp; signatures done offline <span class="muted" style="font-size:13px;font-weight:400">(skip for the normal KYC-link route)</span></h2>
<label class="tick"><input type="checkbox" name="manual" id="manual_on" value="1" {% if pm or manual_pref %}checked{% endif %} onchange="setManual(this.checked)">
This is a manual filing – I checked the client's ID myself and collected the signatures on paper (no KYC link)</label>
<div id="manual_box" style="display:none">
<div class="fgrid">
<div><label>KYC / ID check done on</label><input type="date" name="m_kyc_date" value="{{pm.kyc_date}}"></div>
<div><label>KYC done by (staff name)</label><input type="text" name="m_kyc_by" value="{{pm.kyc_by or (caa.responsible_party if caa else '')}}"></div>
<div><label>How the ID was checked</label><select name="m_kyc_how">{% for k, lbl in [('in_person','In person – originals seen'),('certified','Certified copies received'),('video','Video call + originals seen')] %}<option value="{{k}}" {% if pm.kyc_how == k %}selected{% endif %}>{{lbl}}</option>{% endfor %}</select></div>
<div><label>Applicant signed the W-7 in ink on <span class="muted">(blank if not yet)</span></label><input type="date" name="m_applicant_date" value="{{pm.applicant_date}}"></div>
<div><label>{{ (caa.responsible_party if caa else '') or 'Responsible party' }} signed agent box + COA on <span class="muted">(blank if not yet)</span></label><input type="date" name="m_rp_date" value="{{pm.rp_date}}"></div>
</div>
<div class="warn" style="margin-top:10px"><b>The applicant must sign the completed W-7 that we post.</b> A signature on a blank W-7, or on a different version, is not valid.
If the client hasn't signed this filled-in W-7 yet, leave that date blank – print the W-7 after pressing Create, get it signed in ink, then tick it in the tracker.
In step 1, upload the signed W-7 scan if you have it. No engagement form is needed for manual filing.</div>
<label class="tick"><input type="checkbox" name="m_confirm" value="1" {% if pm.confirm %}checked{% endif %}> I confirm our responsible party saw the ID originals (or certified copies); KYC notes and ID copies are kept on file for 3 years (CAA agreement 6.04)</label>
</div></div>

<div class="card"><button class="btn" type="submit">Create W-7 + Certificate of Accuracy</button></div>
</form>

{% macro jobrow(x) %}{% set ti = x._t %}<tr{% if ti.alert %} style="background:#fff7ed"{% endif %}><td>{{x.applicant}}<br><span class="muted">{{x.a.client_ref or ''}} {{x.when|ukdate}}</span></td>
<td><b>{{ti.stage_label}}</b>{% if ti.alert %}<br><span style="color:#b45309">⚠ {{ti.alert}}</span>{% endif %}</td>
<td>{{ ti.posted.strftime('%d/%m/%Y') if ti.posted else '—' }}{% if ti.days is not none and not ti.closed %}<br><span class="muted">{{ti.days}} days ago</span>{% endif %}</td>
<td>{% if x.track and x.track.outcome == 'itin' %}ITIN {{x.track.itin_number}}{% elif ti.expected %}{{ti.expected.strftime('%d/%m/%Y')}}{% else %}—{% endif %}</td>
<td><a class="btn small secondary" href="/itin/job/{{x.job}}#track">Open</a></td></tr>{% endmacro %}
{% if not review %}<div class="card" id="progress"><h2>Applications in progress ({{open_jobs|length}})</h2>
{% if open_jobs %}<table><tr><th>Applicant</th><th>Stage</th><th>Posted to IRS</th><th>IRS decision expected</th><th></th></tr>
{% for x in open_jobs %}{{ jobrow(x) }}{% endfor %}</table>
<p class="muted">IRS timing: about 2 months from posting (8 weeks; 11 weeks if posted between 15 Jan and 30 Apr). Rows turn orange when something needs action.</p>
{% else %}<p class="muted">Nothing in progress.</p>{% endif %}</div>
{% if done_jobs %}<div class="card"><details><summary style="cursor:pointer;font-weight:700">Completed / closed ({{done_jobs|length}})</summary>
<table style="margin-top:10px"><tr><th>Applicant</th><th>Outcome</th><th>Posted</th><th>Result</th><th></th></tr>
{% for x in done_jobs %}{{ jobrow(x) }}{% endfor %}</table></details></div>{% endif %}{% endif %}

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
var LEVELS = {{ levels|tojson }};
var COAMAP = {passport:'passport', national_id:'national_id', birth_cert:'birth_cert', visa:'visa'};
function docRules(){   // mirrors doc_rules() in itinforms.py
  var r={}; 'abcdefgh'.split('').forEach(function(k){ r[k]=on('r_'+k) });
  var pp = $('id_type').value=='passport', ret = r.b||r.c||r.d||r.e||r.f||r.g;
  var at = document.querySelector('[name=app_type]:checked'), renew = at && at.value=='renew';
  var del = document.querySelector('[name=delegate_rel]').value;
  return {
    engagement: on('manual_on') ? ['na','Not needed – manual filing'] : ['req','Needed before we post'],
    passport: pp ? ['req','Proves identity and foreign status on its own'] : ['opt',''],
    national_id: pp ? ['opt',''] : ['cond','No passport – at least two IDs, one with a photo'],
    birth_cert: (!pp && r.d) ? ['cond','Dependant without a passport'] : ['opt',''],
    other_id: ['opt', pp ? '' : 'Use as the second ID if needed'],
    address: ['opt','Saves typing the address'],
    visa: (r.f||r.g) ? ['cond','Boxes f and g need the US visa'] : ['opt',''],
    tax_return: r.h ? ['na','Exception claimed in box h instead'] : (ret ? (renew ? ['cond','Boxes b–g need the tax return'] : ['opt','Not needed for a first-time (new) application – add it if there is one']) : ['opt','']),
    exception: r.h ? ['cond','Box h – the IRS needs proof of the exception'] : ['na',''],
    us_person: (r.d||r.e) ? ['opt',"Fills the US person's name and number"] : ['na',''],
    residency: r.d ? ['cond','Box d – dependants must prove US residency'] : ['na',''],
    prev_itin: (renew || on('prev_yes')) ? ['opt','Has / had an ITIN'] : ['na',''],
    poa: (del=='poa'||del=='guardian') ? ['cond','Someone else signs for the applicant'] : ['na',''],
    signed_w7: on('manual_on') ? ['opt','Scan of the W-7 / COA signed in ink, if you have it'] : ['opt','Upload after signing'],
    other: ['opt','']
  };
}
var OUR_MAIL = [{{ (caa.our_mail_1 if caa else '')|tojson }}, {{ (caa.our_mail_2 if caa else '')|tojson }}], _clientMail = null;
function mailChoice(){
  var ours = on('mc_ours'), m1 = $('mail_1'), m2 = $('mail_2'), f1 = document.querySelector('[name=foreign_1]'), f2 = document.querySelector('[name=foreign_2]');
  if(!m1) return;
  if(ours){
    if(m1.value && m1.value != OUR_MAIL[0]){ _clientMail = [m1.value, m2.value];
      if(!f1.value && !f2.value){ f1.value = m1.value; f2.value = m2.value } }   // client's address moves to line 3
    m1.value = OUR_MAIL[0]; m2.value = OUR_MAIL[1];
  } else if(m1.value == OUR_MAIL[0]){
    m1.value = _clientMail ? _clientMail[0] : ''; m2.value = _clientMail ? _clientMail[1] : '';
  }
  m1.readOnly = m2.readOnly = ours; m1.style.background = m2.style.background = ours ? '#f1f5f9' : '';
  $('ours_note').style.display = ours ? '' : 'none';
}
function setManual(v){
  if($('manual_on')) $('manual_on').checked = v;
  if($('manual_box')) $('manual_box').style.display = v ? '' : 'none';
  document.querySelectorAll('.manualonly').forEach(function(r){ r.style.display = v ? 'table-row' : 'none' });
  if(window.paintDocs) paintDocs();
}
function paintDocs(){
  var R = docRules();
  for(var k in R){ var el=$('lvl_'+k); if(!el) continue;
    el.className='lvl '+R[k][0]; el.textContent=LEVELS[R[k][0]]; $('why_'+k).textContent=R[k][1];
    $('row_'+k).style.opacity = (R[k][0]=='na' && !($('f_'+k).files||[]).length && !$('row_'+k).dataset.has) ? 0.5 : 1 }
}
function picked(k){
  var c = COAMAP[k];
  if(c && $('f_'+k).files.length){ if($('cid_'+c)) $('cid_'+c).checked = true; if($('cfs_'+c)) $('cfs_'+c).checked = true }
  $('msg_'+k).innerHTML = $('f_'+k).files.length ? '<span class="muted">✔ '+$('f_'+k).files.length+' file(s) chosen'+($('ai_'+k)?' – press Read with AI to fill the form':'')+'</span>' : '';
  paintDocs();
}
function setv(id, v){ if(v && $(id)) $(id).value = v }
function usd(iso){ return /^\d{4}-\d{2}-\d{2}$/.test(iso||'') ? iso.slice(5,7)+'/'+iso.slice(8,10)+'/'+iso.slice(0,4) : (iso||'') }
function readDoc(k){
  var f = $('f_'+k).files[0], msg = $('msg_'+k);
  if(!f){ msg.innerHTML = '<div class="bad">Choose the file first.</div>'; return }
  var fd = new FormData(); fd.append('file', f); fd.append('doc', k);
  $('ai_'+k).disabled = true; msg.innerHTML = '<p class="muted">Reading… (10–20 seconds)</p>';
  fetch('/itin/scan', {method:'POST', body:fd}).then(function(r){return r.json()}).then(function(r){
    $('ai_'+k).disabled = false;
    if(!r.ok){ msg.innerHTML = '<div class="bad">'+esc(r.error)+'</div>'; return }
    var d = r.data, ok = [], w = [];
    if(r.kind=='identity'){
      var hasPass = ($('f_passport').files||[]).length > 0;
      if(k=='passport' || !$('first').value){
        setv('first', d.first_name); setv('middle', d.middle_name); setv('last', d.surname);
        setv('dob', d.date_of_birth); setv('birth_country', d.birth_country); setv('birth_city', d.birth_city);
        setv('citizenship', d.nationality);
        if(d.sex=='M'||d.sex=='F') $('sex_'+d.sex).checked = true;
        ok.push('name, date of birth, sex, birth place, citizenship');
      }
      if(k=='passport' || (!hasPass && !$('id_number').value)){
        setv('id_number', d.document_number); setv('id_issued_by', d.issuing_country); setv('id_expiry', d.expiry_date);
        if(k=='passport'){ $('id_type').value='passport'; syncCoa() }
        else { $('id_type').value='other'; setv('id_other', d.document_type || 'National ID card'); showExtras() }
        ok.push('6d document details');
      }
      if(d.address_line1 && !document.querySelector('[name=mail_1]').value){
        document.querySelector('[name=mail_1]').value = d.address_line1; document.querySelector('[name=mail_2]').value = d.address_line2||'';
        ok.push('line 2 address');
      }
      if(d.expiry_date && d.expiry_date < new Date().toISOString().slice(0,10)) w.push('⚠ This document is EXPIRED ('+esc(d.expiry_date)+') – the IRS only accepts current documents.');
      if(k=='passport' && !d.birth_country) w.push('Country of birth is not printed – ask the client and type it in.');
    } else if(r.kind=='address'){
      if(d.address_line1){ if(on('mc_ours')){ document.querySelector('[name=foreign_1]').value = d.address_line1; document.querySelector('[name=foreign_2]').value = d.address_line2||''; ok.push('line 3 address') }
        else { document.querySelector('[name=mail_1]').value = d.address_line1; document.querySelector('[name=mail_2]').value = d.address_line2||''; ok.push('line 2 address') } }
      var nm = ($('last').value||'').toUpperCase();
      if(nm && d.name_on_document && d.name_on_document.toUpperCase().indexOf(nm) < 0) w.push('Name on the document ("'+esc(d.name_on_document)+'") does not match the applicant – check it is the applicant\'s address.');
    } else if(r.kind=='visa'){
      var v = [d.visa_type, d.visa_number, d.visa_expiry ? 'exp. '+usd(d.visa_expiry) : ''].filter(Boolean).join(', ');
      if(v){ document.querySelector('[name=visa]').value = v; ok.push('6c visa') }
      if(d.entry_date){ document.querySelector('[name=us_entry]').value = d.entry_date; ok.push('date of entry') }
    } else if(r.kind=='tax_return'){
      var first=($('first').value||'').toUpperCase(), last=($('last').value||'').toUpperCase(), names=(d.names||[]).map(function(x){return String(x).toUpperCase()});
      var match = last && names.some(function(n){ return n.indexOf(last)>=0 && (!first || n.indexOf(first)>=0) });
      ok.push((d.form||'Return')+' '+(d.tax_year||'')+' read');
      if(!last) w.push('Fill the applicant name first, then read the return again to check the names match.');
      else if(!match) w.push('⚠ The name on the return ('+esc(names.join(' / '))+') does not match the applicant – the IRS rejects mismatched names.');
      else ok.push('names match the applicant');
    } else if(r.kind=='exception'){
      if(d.suggested_exception){ if(!on('r_h')){ $('r_h').checked = true }
        var h = document.querySelector('[name=reason_h_text]'); if(!h.value) h.value = d.suggested_exception; showExtras(); ok.push('box h → '+d.suggested_exception+' (check it)') }
      if(d.is_original_on_letterhead===false) w.push('The IRS needs the ORIGINAL letter on letterhead – a copy is not enough.');
      if(d.is_signed===false) w.push('The letter does not look signed.');
    } else if(r.kind=='us_person'){
      var a=document.querySelector('[name=us_person_name]'), b=document.querySelector('[name=us_person_tin]');
      if(d.name) a.value=d.name; if(d.ssn_or_itin) b.value=d.ssn_or_itin; ok.push('US person name and number');
    } else if(r.kind=='prev_itin'){
      $('prev_yes').checked = true; showExtras();
      ['prev_itin','prev_first','prev_middle','prev_last'].forEach(function(n, i){ var val=[d.itin,d.first_name,d.middle_name,d.last_name][i]; if(val) document.querySelector('[name='+n+']').value = val });
      ok.push('6e/6f previous ITIN');
    }
    if(d.notes) w.push('AI note: '+esc(d.notes));
    msg.innerHTML = '<div class="ok">Filled: '+esc(ok.join('; ')||'nothing new')+'. Check every field against the document.</div>' + (w.length ? '<div class="warn">'+w.join('<br>')+'</div>' : '');
    paintDocs();
  }).catch(function(e){ $('ai_'+k).disabled = false; msg.innerHTML = '<div class="bad">Reading failed: '+esc(e)+'</div>' });
}
function beforeSubmit(){
  var p = [];
  if(!document.querySelector('[name^=reason_]:checked')) p.push('Tick at least one reason (a–h).');
  if(!$('first').value.trim() || !$('last').value.trim()) p.push('Enter the applicant\'s first and last name.');
  if($('id_type').value=='passport' && !$('f_passport').files.length && !$('row_passport').dataset.has) p.push('Upload the passport (Documents, step 1).');
  if(on('manual_on')){
    var f = function(n){ var e=document.querySelector('[name='+n+']'); return e ? e.value.trim() : '' };
    if(!f('m_kyc_date')) p.push('Manual filing: enter the KYC / ID check date.');
    if(!f('m_kyc_by')) p.push('Manual filing: enter who did the KYC.');
    if(!document.querySelector('[name=m_confirm]').checked) p.push('Manual filing: tick the confirmation box.');
  }
  if(p.length){ alert(p.join('\n')); return false }
  return true;
}
{% if not prefill %}syncCoa();{% else %}showExtras();{% endif %}
$('wf').addEventListener('change', paintDocs); setManual(on('manual_on')); mailChoice();
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
.lvl{display:inline-block;border-radius:10px;padding:2px 9px;font-size:12px;font-weight:700;white-space:nowrap}
.lvl.req{background:#fee2e2;color:#991b1b}.lvl.cond{background:#ffedd5;color:#9a3412}.lvl.opt{background:#e5e7eb;color:#374151}.lvl.na{background:#f3f4f6;color:#9ca3af}
.addr{font-family:ui-monospace,Menlo,Consolas,monospace;background:#f7f7f7;border:1px dashed #94a3b8;border-radius:8px;padding:10px 14px;line-height:1.5;display:inline-block;margin:6px 0}
</style>
<div class="card"><h1>{{m.applicant}} <span class="muted" style="font-size:15px">ITIN – {{ 'renewal' if m.a.app_type == 'renew' else 'new application' }}</span>{% if m.manual %} <span style="background:#dcfce7;color:#166534;border-radius:6px;padding:2px 8px;font-size:13px;vertical-align:middle">✍️ Manual filing</span>{% endif %}</h1>
<div class="dgrid">
 <div><b>Created</b>{{m.when|ukdate}} by {{m.staff}}</div>
 <div><b>Reasons</b>{% for r in m.a.reasons %}{{r}} – {{reasons[r]}}{% if r == 'h' %} ({{m.a.reason_h_text}}){% endif %}<br>{% endfor %}</div>
 <div><b>Document</b>{{m.a.id_type|upper}} {{m.a.id_number}} ({{m.a.id_issued_by}}), expires {{m.a.id_expiry|ukdate}}</div>
 {% if m.a.client_ref %}<div><b>Reference</b>{{m.a.client_ref}}</div>{% endif %}
</div>
<div class="btnrow" style="margin-top:14px"><a class="btn" href="/itin/job/{{m.job}}/zip">⬇ Download all ({{m.files|length}}) as ZIP</a>
{% set w7f = [] %}{% for x in m.files if x.file.startswith('W-7_') %}{% if w7f.append(x) %}{% endif %}{% endfor %}{% if w7f %}<a class="btn secondary" href="/itin/job/{{m.job}}/file/{{w7f[0].file}}?inline=1" target="_blank">🖨 Print W-7 (for signing / IRS)</a>
<a class="btn secondary" href="/itin/job/{{m.job}}/customer-copy.pdf?inline=1" target="_blank">🧾 Customer copy (our details masked)</a>{% endif %}
<a class="btn secondary" href="/itin">New application</a></div>
<p class="muted">Before sending to the IRS: check every field against the documents; the applicant signs and dates the W-7;
our responsible party signs and dates the Acceptance Agent box and the Certificate of Accuracy; attach the copy of the
identity document and the tax return (or exception documents) as required by the reason boxes ticked.</p></div>

{% set draft = m.status in ('kyc_sent', 'kyc_received') %}{% set k = m.kyc or {} %}
{% if k.token %}<div class="card" id="kyc"><h2 style="margin-top:0">Client KYC</h2>
{% if m.status == 'kyc_received' %}{% set ai = k.ai or {} %}
<div class="okb">✔ The client submitted the KYC on {{k.submitted|ukdate}} and signed the engagement form.</div>
{% if ai.status == 'running' %}<div class="warn">⏳ AI is reading the documents – refresh in a minute.</div>{% endif %}
{% if ai.warnings %}<div class="warn"><b>Check these:</b><ul style="margin:6px 0 0">{% for w in ai.warnings %}<li>{{w}}</li>{% endfor %}</ul></div>{% endif %}
<div class="btnrow"><a class="btn" href="/itin/review/{{m.job}}">Review answers &amp; create the W-7 →</a>
<form method="post" action="/itin/job/{{m.job}}/kyc/reopen" style="display:inline" onsubmit="return confirm('Let the client edit and submit again?')"><button class="btn secondary">Let the client edit again</button></form></div>
{% elif m.status == 'kyc_sent' %}{% set link = base_url ~ '/sign/kyc/' ~ k.token %}
<p>Waiting for <b>{{m.applicant}}</b> to fill in the form. Link created {{k.created|ukdate}}, works until <b>{{k.expires|ukdate}}</b>.{% if k.saved %} The client last saved their progress on {{k.saved|ukdate}}.{% endif %}</p>
<div style="display:flex;gap:8px;flex-wrap:wrap"><input type="text" id="klink" value="{{link}}" readonly style="flex:1 1 320px">
<button type="button" class="btn small secondary" onclick="navigator.clipboard.writeText(document.getElementById('klink').value).then(function(){document.getElementById('kcp').textContent='Copied'})">Copy</button>
<a class="btn small secondary" target="_blank" href="https://wa.me/{{ (k.whatsapp or '')|replace('+','')|replace(' ','') }}?text={{ ('Hello ' ~ m.a.first ~ ', please complete your ITIN application with Startitup Global here (about 10 minutes, works on your phone): ' ~ link)|urlencode }}">WhatsApp</a>
<span class="muted" id="kcp"></span></div>
<div class="btnrow" style="margin-top:10px"><form method="post" action="/itin/job/{{m.job}}/kyc/email" style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
<div><label>Email</label><input type="email" name="email" value="{{k.email}}"></div><button class="btn small">Email the link / reminder</button></form>
<form method="post" action="/itin/job/{{m.job}}/kyc/extend"><button class="btn small secondary">Extend the link {{30}} days</button></form></div>
{% else %}<p class="muted" style="margin:0">Created from the client's KYC (submitted {{k.submitted|ukdate}}). <a href="/itin/review/{{m.job}}">Edit the W-7 again</a></p>{% endif %}
</div>{% endif %}

{% if not draft %}
{% set order = ['prepared','ready','posted','itin'] %}{% set cur = order.index(ti.stage) if ti.stage in order else (2 if ti.stage == 'suspended' else 3) %}
<div class="card" id="track"><h2>Progress tracker <a class="btn small secondary" href="/itin/guide" style="float:right">📘 Staff guide</a></h2>
{% if ptin_msg %}<div class="bad">⏰ <b>PTIN renewal:</b> {{ptin_msg}}</div>{% endif %}
<div class="steps">{% for k, lbl in [('prepared','1 · Forms prepared'),('ready','2 · Signed'),('posted','3 · Posted to IRS'),('itin','4 · IRS decision')] %}
<span class="{{ 'now' if loop.index0 == cur else ('done' if loop.index0 < cur else '') }}">{{lbl}}</span>{% endfor %}</div>
<p style="margin:6px 0"><b>Current stage:</b> {{ti.stage_label}}{% if ti.expected and not ti.closed %} · IRS decision expected by <b>{{ti.expected.strftime('%d/%m/%Y')}}</b> ({{ti.days}} days since posting){% endif %}</p>
{% if ti.alert %}<div class="warn">⚠ {{ti.alert}}</div>{% endif %}
{% if missing_docs and not ti.closed %}<div class="bad">📎 <b>Documents still missing</b> (posting is blocked until uploaded): {% for x in missing_docs %}{{ x.split(' – ')[0] }}{% if not loop.last %}; {% endif %}{% endfor %}. <a href="#docs">Upload them</a></div>{% endif %}
{% if ti.stage == 'itin' %}<div class="okb">✔ ITIN issued: <b>{{t.itin_number}}</b>{% if t.outcome_date %} on {{t.outcome_date|ukdate}}{% endif %}.</div>{% endif %}

{% set e = m.engage or {} %}
<div class="tsec" style="background:#fafafa"><h3>{{ "Manual filing" if m.manual and not e.signed else "Client engagement form" }} <a class="muted" style="font-size:13px;font-weight:400" href="/itin/guide#sign">guide</a></h3>
{% if m.manual and not e.signed %}{% set mn = m.manual or {} %}
<div class="okb">✍️ <b>Manual filing</b> – no engagement form needed. KYC / ID checked on <b>{{mn.kyc_date|ukdate}}</b> by {{mn.kyc_by}} – {{ {'in_person':'in person, originals seen','certified':'certified copies','video':'video call + originals seen'}.get(mn.kyc_how, mn.kyc_how) }}.
Client consent in section A was ticked from the KYC date.</div>
{% elif e.signed %}<div class="okb">✔ Signed online by <b>{{e.typed_name}}</b> on {{e.when_local}} – <a href="/itin/job/{{m.job}}/file/{{e.file}}?inline=1" target="_blank">view signed PDF</a>. Consent in section A has been ticked automatically.</div>
{% else %}
<p class="muted" style="margin:2px 0 8px">Send the client a link – they check their details, read our terms, tick the declarations and sign with their finger or mouse.
The signed PDF is saved here and section A "client consent" ticks itself. (This is our own form, so an electronic signature is fine – unlike the W-7.)</p>
<form method="post" action="/itin/job/{{m.job}}/engage" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
<div style="flex:0 1 220px"><label>Fee shown on the form</label><input type="text" name="fee" value="{{ e.fee or eng.default_fee }}" placeholder="e.g. USD 250"></div>
<button class="btn" type="submit">{{ 'Update fee' if e.token else 'Create signing link' }}</button>
<a class="btn secondary" href="/itin/job/{{m.job}}/engage/blank.pdf" target="_blank">Download to sign on paper</a></form>
{% if e.token %}{% set link = base_url ~ '/sign/itin/' ~ e.token %}
<div style="margin-top:10px"><label>Signing link for the client</label>
<div style="display:flex;gap:8px;flex-wrap:wrap"><input type="text" id="slink" value="{{link}}" readonly style="flex:1 1 320px">
<button type="button" class="btn small secondary" onclick="navigator.clipboard.writeText(document.getElementById('slink').value).then(function(){document.getElementById('scp').textContent='Copied'})">Copy</button>
<a class="btn small secondary" target="_blank" href="https://wa.me/?text={{ ('Hello ' ~ m.a.first ~ ', please review and sign your ITIN engagement form with Startitup Global here: ' ~ link)|urlencode }}">WhatsApp</a>
<a class="btn small secondary" href="mailto:?subject={{ 'Your ITIN engagement form – Startitup Global'|urlencode }}&body={{ ('Hello ' ~ m.a.first ~ ',\n\nPlease review and sign your ITIN engagement form here:\n' ~ link ~ '\n\nThank you,\nStartitup Global')|urlencode }}">Email</a>
<span class="muted" id="scp"></span></div>
<p class="muted" style="margin:4px 0 0">Link created {{e.created|ukdate}} · fee {{e.fee}} · waiting for the client to sign.</p></div>{% endif %}
{% endif %}</div>

{% if not m.manual and not e.signed and not ti.closed %}<details class="tsec" id="manualcard"><summary style="cursor:pointer;font-weight:700">✍️ Switch to manual filing (KYC &amp; signatures done offline)</summary>
<form method="post" action="/itin/job/{{m.job}}/manual" style="margin-top:8px"><input type="hidden" name="manual" value="1">
<div class="fgrid">
<div><label>KYC / ID check done on</label><input type="date" name="m_kyc_date"></div>
<div><label>KYC done by (staff name)</label><input type="text" name="m_kyc_by" value="{{m.caa.responsible_party}}"></div>
<div><label>How the ID was checked</label><select name="m_kyc_how"><option value="in_person">In person – originals seen</option><option value="certified">Certified copies received</option><option value="video">Video call + originals seen</option></select></div>
<div><label>Applicant signed this W-7 in ink on (blank if not yet)</label><input type="date" name="m_applicant_date"></div>
<div><label>{{m.caa.responsible_party or 'Responsible party'}} signed on (blank if not yet)</label><input type="date" name="m_rp_date"></div></div>
<label class="tick"><input type="checkbox" name="m_confirm" value="1"> I confirm our responsible party saw the ID originals (or certified copies)</label>
<button class="btn" type="submit">Record manual filing</button></form></details>{% endif %}

<form method="post" action="/itin/job/{{m.job}}/track">
<div class="tsec"><h3>A. Client consent &amp; signatures <a class="muted" style="font-size:13px;font-weight:400" href="/itin/guide#sign">guide</a></h3>
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

<div class="tsec"><h3>B. Post to the IRS <a class="muted" style="font-size:13px;font-weight:400" href="/itin/guide#post">guide</a></h3>
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

<div class="tsec"><h3>C. IRS decision <a class="muted" style="font-size:13px;font-weight:400" href="/itin/guide#after">guide</a></h3>
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

{% endif %}
<div class="card" id="docs"><h2>Documents <a class="btn small secondary" href="/itin/guide#docs" style="float:right">guide</a></h2>
<table><tr><th>Document</th><th style="width:200px">Needed?</th><th>Uploaded</th></tr>
{% for k, d in doc_types.items() %}{% set lv = rules[k][0] %}{% if lv != 'na' or up.get(k) %}
<tr><td><b>{{d[0]}}</b>{% if rules[k][1] %}<br><span class="muted" style="font-size:12px">{{rules[k][1]}}</span>{% endif %}</td>
<td><span class="lvl {{lv}}">{{levels[lv]}}</span></td>
<td>{% if up.get(k) %}{% for x in up[k] %}<div>✔ <a href="/itin/job/{{m.job}}/file/{{x.file}}?inline=1" target="_blank">{{x.orig or x.file}}</a>
<form method="post" action="/itin/job/{{m.job}}/remove" style="display:inline" onsubmit="return confirm('Remove this document?')"><input type="hidden" name="file" value="{{x.file}}"><button class="btn small secondary" style="padding:1px 8px">remove</button></form></div>{% endfor %}
{% elif lv in ('req','cond') %}<span style="color:#b91c1c;font-weight:600">✖ Missing</span>{% else %}<span class="muted">—</span>{% endif %}</td></tr>{% endif %}{% endfor %}</table>
<form method="post" action="/itin/job/{{m.job}}/upload" enctype="multipart/form-data" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-top:12px">
<div style="flex:1 1 280px"><label>Add a document</label><select name="doc"><option value="">Choose which document…</option>
{% for k, d in doc_types.items() %}<option value="{{k}}" {% if k in missing_docs_keys %}style="font-weight:700"{% endif %}>{{d[0].split(' – ')[0]}}{% if rules[k][0] in ('req','cond') and not up.get(k) %} (missing){% endif %}</option>{% endfor %}</select></div>
<div style="flex:1 1 240px"><label>File(s)</label><input type="file" name="files" multiple accept="image/*,application/pdf"></div>
<button class="btn" type="submit">Upload</button></form>
<p class="muted" style="margin-top:8px">Uploaded documents are saved as PDFs with this application, included in the ZIP, and kept as our CAA record.</p>
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


# ----------------------------------------------------------------------------- staff guide

CUSTOMER_MESSAGE = """Hello {name},

To apply for your ITIN (US tax number) we need a few things from you. Please reply with:

1. A clear colour photo or scan of your passport photo page (the passport must still be valid). No passport? Send your national ID card (front and back) and birth certificate.
2. Your home address (house, street, city, postcode, country) - this is where the IRS posts your ITIN letter. A recent utility bill or bank statement showing it helps.
3. Your mobile number.
4. Why you need the ITIN - for example: to file a US tax return for your US company, you are the husband/wife of a US person, you receive income from a US payer, or you are buying/selling US property.
5. Have you ever had an ITIN or any US tax number before? If yes, please send the number.
6. Your tax number in your own country, if you have one (e.g. NTN in Pakistan, UTR in the UK).
7. Have you ever been to the USA? If yes, the date you last entered and your visa type.
8. Your US company name and the state it is registered in (if you have one).

Next steps: we prepare the form, you sign it by hand with a pen (we cannot accept a typed or digital signature) and send the original back to us by courier. We then send everything to the IRS. The IRS usually replies in about 2 months.

Thank you,
Startitup Global"""


@bp.get("/itin/guide")
def itin_guide():
    return render_template_string(GUIDE, msg=CUSTOMER_MESSAGE.replace("{name}", "[client name]"), addr=IRS_ADDR,
                                  phone=IRS_STATUS_PHONE, caa=load_caa())


GUIDE = r"""{% extends "base" %}{% block body %}
<style>
.g h2{margin-top:6px}.g h3{margin:18px 0 6px;font-size:16px}
.g table{width:100%;border-collapse:collapse;margin:8px 0}.g td,.g th{border:1px solid var(--line);padding:8px 10px;vertical-align:top;font-size:14px;text-align:left}
.g th{background:#f3f4f6}.g ol li,.g ul li{margin:4px 0;line-height:1.5}
.tip{background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px 14px;margin:10px 0}
.stop{background:#fee2e2;border:1px solid #fca5a5;border-radius:8px;padding:10px 14px;margin:10px 0}
.toc a{display:inline-block;margin:3px 10px 3px 0}
pre.msg{white-space:pre-wrap;background:#f7f7f7;border:1px solid var(--line);border-radius:8px;padding:14px;font-family:inherit;font-size:14px}
</style>
<div class="card g"><h1>ITIN staff guide</h1>
<p class="muted">How to handle an ITIN (Form W-7) application from the first message to the ITIN letter. Based on the IRS Instructions for Form W-7 (Rev. December 2024).
If a case does not fit this guide, ask {{caa.responsible_party or 'the responsible party'}} before sending anything to the client or the IRS.</p>
<div class="toc"><b>Jump to:</b> <a href="#flow">1. The whole process</a><a href="#ask">2. What to ask the client</a><a href="#reason">3. Which reason box</a>
<a href="#exceptions">4. Exceptions (box h)</a><a href="#fields">5. Filling each field</a><a href="#docs">6. Documents &amp; COA</a><a href="#sign">7. Signing</a>
<a href="#post">8. Posting</a><a href="#after">9. After posting</a><a href="#renew">10. Renewals</a><a href="#dont">11. Common mistakes</a></div>
<p><a class="btn" href="/itin">← Back to ITIN applications</a></p></div>

<div class="card g" id="flow"><h2>1. The whole process</h2>
<table><tr><th style="width:40px">#</th><th>Step</th><th>Who</th><th>In the system</th></tr>
<tr><td>1</td><td>Agree the fee, then on the ITIN page use <b>New client – send a KYC link</b> (enter name, email, fee). The client fills in their details, uploads their documents and signs our engagement form on their phone. The link works for 30 days and they can save and come back.</td><td>Staff → client</td><td>ITIN → New client</td></tr>
<tr><td>1b</td><td>When the client submits, you get an email and the application shows <b>KYC received</b> (orange row, red badge). AI has read the documents and filled the fields, and lists anything to check.</td><td>System</td><td>Applications in progress</td></tr>
<tr><td>2</td><td>Open the application → <b>Review answers &amp; create the W-7</b>. Read the AI warnings. Work out the reason box(es) (section 3) – the client's answers suggest one and which documents the IRS needs with the W-7 (tax return, or exception documents).</td><td>Staff</td><td>—</td></tr>
<tr><td>3</td><td>Upload each document with its own button (the <b>Needed?</b> column shows what is required for the boxes ticked), press <b>Read with AI</b> on each, check every field, complete the rest, tick the COA documents, press <b>Create</b>. Missing documents can be added later on the application page.</td><td>Staff</td><td>ITIN → new application</td></tr>
<tr><td>4</td><td>Check the PDFs carefully. Send the W-7 to the client to sign by hand in ink and return the original.</td><td>Staff → client</td><td>Tracker A</td></tr>
<tr><td>5</td><td>Responsible party verifies the passport, signs the Acceptance Agent box on the W-7 and the Certificate of Accuracy.</td><td>{{caa.responsible_party or 'Responsible party'}}</td><td>Tracker A</td></tr>
<tr><td>6</td><td>Put the envelope together (checklist), generate the postage label, post it, enter carrier, tracking number and date.</td><td>Staff</td><td>Tracker B</td></tr>
<tr><td>7</td><td>Wait about 2 months. If the row turns orange, call the IRS status line. Record the outcome and the ITIN, tell the client.</td><td>Staff</td><td>Tracker C</td></tr></table>
</div>

<div class="card g" id="ask"><h2>2. What to ask the client</h2>
<p><b>Best:</b> send the client a <b>KYC link</b> from the ITIN page – it asks these questions for you, takes the documents and gets the engagement form signed. Clients who prefer to reply by message: copy this text and send it by WhatsApp or email.</p>
<button class="btn small secondary" onclick="navigator.clipboard.writeText(document.getElementById('cmsg').innerText).then(function(){document.getElementById('cpd').textContent=' Copied'})">Copy message</button><span id="cpd" class="muted"></span>
<pre class="msg" id="cmsg">{{msg}}</pre>
<div class="tip"><b>When the client answers question 4 ("why do you need an ITIN?")</b> use section 3 to choose the box. If the answer is only
"for Stripe / PayPal / Amazon / a bank account", that is <b>not</b> a reason on its own – see the red box in section 3.</div>
</div>

<div class="card g" id="reason"><h2>3. Which reason box to tick</h2>
<p>Find the client's situation. Tick only the boxes shown. Then attach what the last column says.</p>
<table><tr><th>Client's situation</th><th>Tick</th><th>Extra fields</th><th>What must go with the W-7</th></tr>
<tr><td><b>Most common:</b> lives outside the US and has to file a US tax return (e.g. Form 1040-NR for US business income, or to claim a refund of US tax withheld)</td><td><b>b</b></td><td>Line 3 foreign address must be complete</td><td>The completed US tax return. W-7 goes on top, return behind it.</td></tr>
<tr><td>Husband/wife of a US citizen or US resident, filing a joint US return</td><td><b>e</b></td><td>Name + SSN/ITIN of the US spouse</td><td>The joint tax return</td></tr>
<tr><td>Child or other dependant of a US citizen/resident</td><td><b>d</b></td><td>Relationship (e.g. "Son"), name + SSN/ITIN of the US person, date of entry into the US</td><td>The tax return + proof the dependant lives in the US. We can only verify a <b>passport or birth certificate</b> for dependants.</td></tr>
<tr><td>Lives in the US because of days spent there (substantial presence) but can't get an SSN</td><td><b>c</b></td><td>Date of entry into the US</td><td>The tax return</td></tr>
<tr><td>Spouse/child of someone holding a US visa (e.g. H-1B, L-1)</td><td><b>g</b></td><td>Date of entry into the US</td><td>Tax return + copy of the visa</td></tr>
<tr><td>Student, professor or researcher in the US</td><td><b>f</b> (+ <b>h</b> if claiming Exception 2)</td><td>Visa (6c), date of entry, line 6g school + city/state + length of stay</td><td>Tax return, or the Exception 2 documents. Passport must show a valid US visa.</td></tr>
<tr><td>No tax return, but claiming a tax treaty benefit (e.g. reduced withholding on royalties or dividends)</td><td><b>a</b> + <b>h</b></td><td>Treaty country + article number; box h "Exception 1(d)" or "Exception 2(…)"</td><td>The exception documents (section 4)</td></tr>
<tr><td>No tax return, but a US bank / partnership / payer / mortgage lender / property buyer needs the ITIN</td><td><b>h</b> only</td><td>Exception number on the box h line</td><td>The exception documents (section 4)</td></tr>
<tr><td>Already had an ITIN that expired</td><td>Tick <b>Renew</b>, the same reason box as above, 6e = <b>Yes</b></td><td>Old ITIN in 6f</td><td>The tax return (renewals are only for people filing a return)</td></tr></table>
<div class="stop"><b>Stop and check:</b> "I need an ITIN for Stripe / PayPal / Amazon / to open a bank account" is not an IRS reason by itself.
Ask whether they have a US tax return to file (then box b), or an original letter from a US bank or payer (then box h with Exception 1).
If neither, the IRS will reject it – do not submit, explain this to the client and speak to {{caa.responsible_party or 'the responsible party'}}.</div>
<div class="tip">Boxes b, c, d, e, f and g always need a US tax return attached <b>unless</b> an exception applies (then also tick h). Box a always needs h too – the system ticks it for you.</div>
</div>

<div class="card g" id="exceptions"><h2>4. Exceptions – what to write on the box h line and what to attach</h2>
<p>Only use these when there is <b>no tax return</b>. Choose the text from the list in the system (box h field).</p>
<table><tr><th>Write on box h</th><th>Who it is for</th><th>Document needed (originals/certified)</th></tr>
<tr><td>Exception 1(a)</td><td>Partner in a US partnership / multi-member LLC that invests in the US and has income subject to withholding</td><td>Copy of the partnership or LLC agreement showing the entity's EIN and that the client is a partner</td></tr>
<tr><td>Exception 1(b)</td><td>Has an interest-bearing US <b>business</b> bank account with income effectively connected to a US business</td><td>Original signed letter from the bank on its letterhead</td></tr>
<tr><td>Exception 1(c)</td><td>Resident alien with an interest-bearing personal US bank account</td><td>Original signed letter from the bank on its letterhead</td></tr>
<tr><td>Exception 1(d)</td><td>Receives pensions, annuities, rent, royalties, dividends etc. and the payer (withholding agent) needs the ITIN</td><td>Original letter/document from the payer on its letterhead (or the contract)</td></tr>
<tr><td>Exception 2(a)</td><td>Wages / fees / honoraria with a treaty claim (files Form 8233 with the payer)</td><td>Original employment letter, contract or speaking-engagement letter</td></tr>
<tr><td>Exception 2(b) / 2(c)</td><td>Scholarships, fellowships, grants (with / without treaty)</td><td>Letter from the university, passport with US visa, SSA or DSO/RO letter (see IRS instructions)</td></tr>
<tr><td>Exception 3</td><td>Has a home mortgage on US property</td><td>Evidence of the mortgage loan (e.g. contract of sale)</td></tr>
<tr><td>Exception 4</td><td>Buying/selling US real estate where FIRPTA withholding applies</td><td>Form 8288 / 8288-A / 8288-B + sales contract or closing statement</td></tr>
<tr><td>Exception 5, T.D. 9363</td><td>Non-US officer of a foreign company who must e-file under T.D. 9363</td><td>Original signed letter from the employer on company letterhead</td></tr></table>
<div class="tip">Exception 1 can't be used if the client also has to file a US tax return – then use the return (box b) instead.</div>
</div>

<div class="card g" id="fields"><h2>5. Filling each field in the system</h2>
<table><tr><th style="width:24%">Field</th><th>What to enter</th><th style="width:26%">If it doesn't apply</th></tr>
<tr><td>Passport scan → Read with AI</td><td>Photo page in colour, all four corners visible. Then <b>check every field the AI filled</b> against the passport.</td><td>No AI? Type it in.</td></tr>
<tr><td>Application type</td><td>New, unless the client already had an ITIN that expired (Renew).</td><td>—</td></tr>
<tr><td>1a Name</td><td>Exactly as on the passport. Same spelling as on the tax return.</td><td>No middle name → leave blank</td></tr>
<tr><td>1b Name at birth</td><td>Only if different from the passport (e.g. maiden name).</td><td>Leave closed/blank</td></tr>
<tr><td>2 Mailing address</td><td>Where the IRS posts the ITIN letter – usually the client's home address. P.O. box only if no street delivery.</td><td>Required</td></tr>
<tr><td>3 Foreign address</td><td>The client's permanent home address outside the US. <b>Leave blank if it is the same as line 2</b> – the system copies line 2. If line 2 is a US address, you must enter the foreign address. No P.O. box.</td><td>Copied from line 2</td></tr>
<tr><td>4 Date of birth / country / city</td><td>From the passport. If the passport shows only a city, ask the client for the country.</td><td>City optional</td></tr>
<tr><td>5 Sex</td><td>From the passport.</td><td>—</td></tr>
<tr><td>6a Citizenship</td><td>Country name in full (e.g. Pakistan, not PAK).</td><td>Required</td></tr>
<tr><td>6b Foreign tax ID</td><td>Their own country's tax number (e.g. NTN, UTR).</td><td>Leave blank → printed as N/A</td></tr>
<tr><td>6c US visa</td><td>Type, number, expiry – only US visas.</td><td>Leave blank → N/A</td></tr>
<tr><td>6d Document</td><td>Passport (normally). Issued by = country. Number + expiry from the passport. <b>Date of entry into the US</b> from the last US entry stamp.</td><td>Never been to the US → leave the date blank; the form prints "Never entered the U.S."</td></tr>
<tr><td>6e / 6f Previous ITIN</td><td>Yes only if they ever had an ITIN or IRSN – enter the number and the name it was issued under.</td><td>No / Don't know</td></tr>
<tr><td>6g College or company</td><td>Students: school, city/state, length of stay. Business: the US company name, city/state, length of stay.</td><td>Leave blank → N/A</td></tr>
<tr><td>Phone</td><td>Client's phone with country code.</td><td>—</td></tr>
<tr><td>Delegate</td><td>Only if someone signs for the applicant: parent/guardian of a child under 18, or a person holding Form 2848 power of attorney (attach it).</td><td>Leave blank</td></tr>
<tr><td>Client reference</td><td>Our portal / invoice reference so we can find it later.</td><td>Optional</td></tr></table>
</div>

<div class="card g" id="docs"><h2>6. Documents and the Certificate of Accuracy (COA)</h2>
<ol><li>The client shows the <b>original passport</b> (or a copy certified by the issuing agency/embassy) to {{caa.responsible_party or 'the responsible party'}}. It must be valid on the day we post.</li>
<li>In the system tick <b>Passport – Identity</b> and <b>Passport – Foreign status</b> (ticked automatically when 6d is Passport).</li>
<li>No passport? At least <b>two</b> documents are needed, one with a photo – e.g. national ID card + birth certificate. Tick each one under the right column.</li>
<li>Dependants: we can only certify a <b>passport or birth certificate</b>. Any other dependant document must be the original or a certified copy sent to the IRS.</li>
<li>We keep copies of everything we checked – every document uploaded is saved as a PDF with the application – and return originals to the client straight away.</li>
<li>Posting is blocked until every document marked <b>Required</b> or <b>Required for this application</b> is uploaded.</li></ol>
</div>

<div class="card g" id="sign"><h2>7. Signing</h2>
<div class="stop"><b>Ink only.</b> The W-7 and the COA are not on the IRS list of forms that accept electronic signatures. A typed name, a pasted image or a
DocuSign-type signature will get the application suspended or rejected.</div>
<ol><li>Our engagement form: press <b>Create signing link</b> on the application, send it by WhatsApp or email – the client signs on their phone and the signed PDF is saved automatically. Clients who prefer paper: "Download to sign on paper", then upload the signed scan as "engagement form".</li>
<li>Print the W-7 and send it to the client (courier or post). The client signs and dates the "Signature of applicant" box with a pen and posts the <b>original</b> back.</li>
<li>Under 18: a parent or court-appointed guardian signs and is entered as Delegate. Someone else signing for an adult needs Form 2848 attached.</li>
<li>{{caa.responsible_party or 'The responsible party'}} signs and dates the Acceptance Agent box on the W-7 and the COA.</li>
<li>Tick the three boxes in <b>Tracker A</b> with the dates. Posting is blocked until all three are ticked.</li></ol>
<p><b>Manual filing</b> (KYC and signatures done offline, no link): on the ITIN page press <b>Start a manual application</b>, fill the W-7 as normal, upload the
documents (no engagement form needed), and complete step 5 with the KYC and signing dates. Tracker A ticks itself from those dates.
An existing application (e.g. a KYC link the client never finished) can be switched with <b>Switch to manual filing</b> on its page.</p>
</div>

<div class="card g" id="post"><h2>8. Posting to the IRS</h2>
<table><tr><th>Courier (recommended – tracked)</th><th>US Postal Service</th></tr>
<tr><td>{{ addr['courier'][1]|join('<br>')|safe }}</td><td>{{ addr['usps'][1]|join('<br>')|safe }}</td></tr></table>
<p><b>Envelope, in this order:</b> signed W-7 on top → tax return (or exception documents) → W-7-COA → copies of the documents we verified.</p>
<p>Generate the label, post it, then in <b>Tracker B</b> tick the checklist and "Postage label generated", and enter carrier, tracking number and date posted.
The 2-month reminder starts from that date.</p>
</div>

<div class="card g" id="after"><h2>9. After posting</h2>
<table><tr><th>What happens</th><th>What to do</th></tr>
<tr><td>Nothing for about 2 months (8 weeks; 11 weeks if posted 15 Jan – 30 Apr)</td><td>Nothing. The row stays white.</td></tr>
<tr><td>Row turns orange (past the expected date)</td><td>Call the IRS W-7 status line for acceptance agents: <b>{{phone}}</b>. Write what they said in Tracker C notes.</td></tr>
<tr><td>IRS letter CP565 – ITIN issued</td><td>Tracker C → "ITIN issued", enter the ITIN and letter date. Send the client their ITIN.</td></tr>
<tr><td>IRS letter CP566 – suspended (more information needed)</td><td>Tracker C → "Suspended". Send the requested documents before the deadline on the letter.</td></tr>
<tr><td>IRS letter CP567 – rejected</td><td>Tracker C → "Rejected", note the reason, tell {{caa.responsible_party or 'the responsible party'}} before contacting the client.</td></tr></table>
</div>

<div class="card g" id="renew"><h2>10. Renewals and our PTIN</h2>
<ul><li>An ITIN expires if it is not used on a US tax return for 3 years in a row. Renew only when the client is filing a return – tick <b>Renew</b>, 6e Yes, old ITIN in 6f, attach the return.</li>
<li>Our responsible party's PTIN ({{caa.ptin}}) must be renewed every year (renewals open mid-October). The system shows a red reminder 75 days before {{caa.ptin_expiry|ukdate if caa.ptin_expiry else 'expiry'}}; after renewing, update "PTIN valid until" in our Acceptance Agent details.</li></ul>
</div>

<div class="card g" id="dont"><h2>11. Common mistakes that get applications rejected</h2>
<ul><li>No tax return attached for boxes b–g (and no exception claimed).</li>
<li>Box a ticked without box h and the treaty country/article.</li>
<li>Name spelt differently from the passport or the tax return.</li>
<li>Passport expired by the time we post.</li>
<li>Typed or digital signature, or the applicant forgot to date it.</li>
<li>Line 3 foreign address missing for box b.</li>
<li>Exception claimed without the original bank/payer letter.</li></ul>
<p><a class="btn" href="/itin">← Back to ITIN applications</a></p></div>
{% endblock %}"""


# ----------------------------------------------------------------------------- client engagement form (signed online)

ENGAGE_TERMS_FILE = OUT / "engagement_terms.json"
TOKENS_FILE = OUT / "sign_tokens.json"

ENGAGE_DEFAULT = {
    "default_fee": "",
    "title": "ITIN Application – Client Engagement and Authorisation",
    "terms": """1. Who we are
Startitup Global Inc (trading as Startitup Global), 5904 Jessamine Street, Suite 15, Houston, TX 77081, USA, is an IRS Certifying Acceptance Agent (CAA). "We" means Startitup Global Inc. "You" means the applicant named below.

2. What we will do
We will check your identity documents, prepare IRS Form W-7 (Application for IRS Individual Taxpayer Identification Number) and Form W-7-COA (Certificate of Accuracy), and send your application to the IRS. Preparing a US tax return is not part of this engagement unless we agree it separately in writing.

3. Fee
Our fee for this application is {fee}. It is payable before we send your application to the IRS. Once your application has been posted to the IRS the fee is not refundable, whatever the IRS decides.

4. What you agree to do
- Give us true, complete and up-to-date information.
- Give us genuine, current identity documents (originals or copies certified by the issuing agency).
- Sign Form W-7 by hand in ink and return the original to us – the IRS does not accept an electronic signature on Form W-7.
- Tell us straight away about any change to your details, and reply promptly if the IRS asks for more information.

5. No guarantee
Only the IRS can issue an ITIN. The IRS usually takes about 7 weeks, and up to 11 weeks between January and April or for applications from abroad. We cannot guarantee that an ITIN will be issued or when.

6. Your authority
You authorise us to act as your Certifying Acceptance Agent for this application, to deal with the IRS ITIN Program about it, and to receive copies of IRS notices about your application.

7. Your documents and personal data
We use your information only to provide this service and to meet our obligations to the IRS and under the law. We share it only with the IRS. As required by our IRS Acceptance Agent agreement, we keep copies of your Form W-7, supporting documents and IRS notices for three calendar years after the year your application is mailed to the IRS, and then delete them securely.

8. Electronic signature
You agree to sign this engagement electronically, and that your electronic signature has the same effect as a handwritten one.

9. Ending this engagement
You can end this engagement in writing at any time before we post your application; we will then return your documents. If you end it after we have started work, the fee for work done may be charged."""}

DECLARATIONS = [
    "The information I have given is true, complete and about me.",
    "The identity documents I have provided are genuine and current.",
    "I have read and agree to the terms above, including the fee.",
    "I understand I must still sign the IRS Form W-7 by hand in ink and return the original.",
    "I agree to sign this engagement electronically.",
]


def _sig_ok(sig: str) -> bool:
    """True if the drawn signature has real ink in it (not an empty box)."""
    if not sig.startswith("data:image/png;base64,") or len(sig) > 3_000_000:
        return False
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(sig.split(",", 1)[1]))).convert("RGBA")
        im.thumbnail((400, 400))
        return sum(1 for px in im.getdata() if px[3] > 40) >= 40
    except Exception:
        return False


def load_engage() -> dict:
    d = dict(ENGAGE_DEFAULT)
    try:
        d.update(json.loads(ENGAGE_TERMS_FILE.read_text()))
    except Exception:
        pass
    return d


def _tokens() -> dict:
    try:
        return json.loads(TOKENS_FILE.read_text())
    except Exception:
        return {}


def _job_for_token(token: str):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        abort(404)
    job = _tokens().get(token)
    if not job:
        abort(404)
    d = _job_dir(job)
    return d, json.loads((d / "job.json").read_text())


def _mask(s: str) -> str:
    s = s or ""
    return ("•" * max(0, len(s) - 4)) + s[-4:] if len(s) > 4 else s


def engagement_pdf(m: dict, eng: dict, fee: str, signed: dict | None = None) -> bytes:
    """The engagement as a PDF. signed=None → blank copy for signing on paper."""
    import hashlib, html as H
    import pymupdf
    a = m["a"]
    name = " ".join(x for x in (a["first"], a["middle"], a["last"]) if x)
    terms = eng["terms"].replace("{fee}", fee or "as agreed with us in writing")
    body = []
    for block in terms.split("\n\n"):
        lines = block.strip().split("\n")
        if not lines or not lines[0]:
            continue
        body.append(f"<h3>{H.escape(lines[0])}</h3>")
        bullets = [l[2:] for l in lines[1:] if l.startswith("- ")]
        paras = [l for l in lines[1:] if not l.startswith("- ")]
        body += [f"<p>{H.escape(p)}</p>" for p in paras]
        if bullets:
            body.append("<ul>" + "".join(f"<li>{H.escape(b)}</li>" for b in bullets) + "</ul>")
    rows = [("Applicant", name), ("Date of birth", a.get("dob") and dt.date.fromisoformat(a["dob"]).strftime("%d/%m/%Y") or ""),
            ("Passport / ID number", a.get("id_number", "")), ("Address", ", ".join(x for x in (a.get("mail_1"), a.get("mail_2")) if x)),
            ("Phone", (signed or {}).get("phone") or a.get("phone", "")), ("Email", (signed or {}).get("email", "")),
            ("Our reference", a.get("client_ref") or m["job"])]
    tbl = "".join(f"<tr><td class='k'>{H.escape(k)}</td><td>{H.escape(v or '')}</td></tr>" for k, v in rows)
    tick = "&#9745;" if signed else "&#9744;"
    decl = "".join(f"<p>{tick} {H.escape(x)}</p>" for x in DECLARATIONS)
    if signed:
        sig = (f"<p><img src='sig.png' width='220'/></p><p><b>Signed by:</b> {H.escape(signed['typed_name'])}<br/>"
               f"<b>Date:</b> {H.escape(signed['when_local'])}</p>")
        digest = hashlib.sha256(terms.encode()).hexdigest()
        audit = (f"<h3>Electronic signature record</h3><p class='s'>Signed online at {H.escape(signed['when_utc'])} UTC · "
                 f"IP address {H.escape(signed['ip'])} · Device: {H.escape(signed['ua'][:160])} · Link ID {H.escape(signed['token'][:8])} · "
                 f"Terms fingerprint (SHA-256) {digest[:32]}</p>")
    else:
        sig = ("<p>Signature: ______________________________________</p><p>Full name: ______________________________________</p>"
               "<p>Date: ____ / ____ / ________</p>")
        audit = ""
    html = f"""<style>body{{font-family:sans-serif;font-size:10pt;line-height:1.4}} h1{{font-size:15pt;margin:0 0 4px}}
h3{{font-size:10.5pt;margin:10px 0 2px}} p{{margin:2px 0 4px}} td{{padding:3px 6px;border:1px solid #bbb;font-size:9.5pt}}
td.k{{width:150px;background:#f2f2f2;font-weight:bold}} .s{{font-size:8pt;color:#444}} .hd{{font-size:9pt;color:#555}}</style>
<p class='hd'><b>Startitup Global Inc</b> · IRS Certifying Acceptance Agent · 5904 Jessamine Street, Suite 15, Houston, TX 77081 · +1 406-605-9190</p>
<h1>{H.escape(eng['title'])}</h1>
<table>{tbl}</table>{''.join(body)}<h3>Declarations</h3>{decl}<h3>Signature</h3>{sig}{audit}"""
    arch = pymupdf.Archive()
    if signed:
        arch.add(signed["sig_png"], "sig.png")
    story = pymupdf.Story(html=html, archive=arch)
    buf = io.BytesIO()
    w = pymupdf.DocumentWriter(buf)
    page_rect, where = pymupdf.paper_rect("letter"), pymupdf.paper_rect("letter") + (50, 50, -50, -50)
    more = 1
    while more:
        dev = w.begin_page(page_rect)
        more, _ = story.place(where)
        story.draw(dev)
        w.end_page()
    w.close()
    return buf.getvalue()


@bp.post("/itin/engagement-terms")
def itin_engage_terms():
    d = {k: (request.form.get(k) or "").strip() for k in ("default_fee", "title", "terms")}
    if not d["terms"]:
        d["terms"] = ENGAGE_DEFAULT["terms"]
    with _LOCK:
        OUT.mkdir(parents=True, exist_ok=True)
        ENGAGE_TERMS_FILE.write_text(json.dumps(d, indent=1))
    flash("Engagement form wording saved.")
    return redirect("/itin#engagement")


@bp.post("/itin/engagement-terms/reset")
def itin_engage_reset():
    ENGAGE_TERMS_FILE.unlink(missing_ok=True)
    flash("Engagement form wording reset to the standard text.")
    return redirect("/itin#engagement")


@bp.post("/itin/job/<job>/engage")
def itin_engage_link(job):
    d = _job_dir(job)
    fee = (request.form.get("fee") or "").strip()
    if not fee:
        flash("⚠ Enter the fee to show on the engagement form (e.g. USD 250).")
        return redirect(f"/itin/job/{job}#track")
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        e = m.get("engage") or {}
        if e.get("signed"):
            flash("⚠ The client has already signed the engagement form.")
            return redirect(f"/itin/job/{job}#track")
        tok = e.get("token") or uuid.uuid4().hex
        e.update({"token": tok, "fee": fee, "created": dt.datetime.now().isoformat(timespec="seconds")})
        m["engage"] = e
        (d / "job.json").write_text(json.dumps(m, indent=1))
        t = _tokens()
        t[tok] = job
        TOKENS_FILE.write_text(json.dumps(t, indent=1))
    flash("Signing link ready – copy it and send it to the client.")
    return redirect(f"/itin/job/{job}#track")


@bp.get("/itin/job/<job>/engage/blank.pdf")
def itin_engage_blank(job):
    d = _job_dir(job)
    m = json.loads((d / "job.json").read_text())
    fee = (m.get("engage") or {}).get("fee") or request.args.get("fee") or load_engage().get("default_fee", "")
    return send_file(io.BytesIO(engagement_pdf(m, load_engage(), fee)), mimetype="application/pdf",
                     download_name=f"ITIN_engagement_{_safe(m['applicant'])}_to_sign.pdf")


# --- public signing page (no login – protected by the 32-character link) ---

@bp.get("/sign/itin/<token>")
def sign_page(token):
    d, m = _job_for_token(token)
    e = m.get("engage") or {}
    eng = load_engage()
    return render_template_string(SIGN_PAGE, m=m, a=m["a"], e=e, eng=eng, token=token, decl=DECLARATIONS,
                                  terms=eng["terms"].replace("{fee}", e.get("fee") or "as agreed with us in writing"),
                                  masked=_mask(m["a"].get("id_number", "")), err="")


@bp.post("/sign/itin/<token>")
def sign_submit(token):
    d, m = _job_for_token(token)
    e = m.get("engage") or {}
    if e.get("signed"):
        return redirect(f"/sign/itin/{token}")
    f = request.form
    typed = (f.get("typed_name") or "").strip()
    sig = f.get("sig") or ""
    errs = []
    if len(typed) < 3:
        errs.append("Type your full name.")
    if not all(f.get(f"decl_{i}") for i in range(len(DECLARATIONS))):
        errs.append("Tick every declaration.")
    if not _sig_ok(sig):
        errs.append("Draw your signature in the box.")
    eng = load_engage()
    if errs:
        return render_template_string(SIGN_PAGE, m=m, a=m["a"], e=e, eng=eng, token=token, decl=DECLARATIONS,
                                      terms=eng["terms"].replace("{fee}", e.get("fee") or "as agreed with us in writing"),
                                      masked=_mask(m["a"].get("id_number", "")), err=" ".join(errs), form=f)
    now = dt.datetime.now(dt.timezone.utc)
    signed = {"typed_name": typed, "email": (f.get("email") or "").strip()[:120], "phone": (f.get("phone") or "").strip()[:40],
              "when_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "when_local": now.strftime("%d/%m/%Y"),
              "ip": (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip(),
              "ua": request.headers.get("User-Agent", ""), "token": token,
              "sig_png": base64.b64decode(sig.split(",", 1)[1])}
    pdf = engagement_pdf(m, eng, e.get("fee", ""), signed)
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        tag = _safe(f"{m['a']['last']}_{m['a']['first']}")
        fname = f"engagement_{tag}_signed.pdf"
        (d / fname).write_bytes(pdf)
        m["files"] = [x for x in m["files"] if x["file"] != fname] + [
            {"file": fname, "label": "Engagement form (signed online)", "kind": "engagement", "orig": "Signed online"}]
        e = m.get("engage") or {}
        e.update({k: v for k, v in signed.items() if k != "sig_png"}, signed=True, file=fname)
        m["engage"] = e
        t = m.get("track") or {}
        before = track_info(m)["stage"]
        t["consent"], t["consent_date"] = True, now.date().isoformat()
        m["track"] = t
        after = track_info(m)["stage"]
        hist = m.get("history") or []
        hist.append({"when": dt.datetime.now().isoformat(timespec="seconds"), "stage": "engagement signed online",
                     "staff": "client"})
        if after != before:
            hist.append({"when": dt.datetime.now().isoformat(timespec="seconds"), "stage": after, "staff": "client"})
        m["history"] = hist
        (d / "job.json").write_text(json.dumps(m, indent=1))
    _ALERT_CACHE["at"] = 0
    return redirect(f"/sign/itin/{token}")


@bp.get("/sign/itin/<token>/copy.pdf")
def sign_copy(token):
    d, m = _job_for_token(token)
    e = m.get("engage") or {}
    if not e.get("signed"):
        abort(404)
    return send_file(d / e["file"], mimetype="application/pdf", download_name="ITIN_engagement_signed.pdf")


SIGN_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ITIN engagement – Startitup Global</title>
<style>
:root{--ink:#111;--muted:#666;--line:#e5e7eb}
*{box-sizing:border-box}body{margin:0;background:#f5f5f4;color:var(--ink);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:16px}.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:20px;margin:14px 0}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 10px}.muted{color:var(--muted);font-size:14px}
table{width:100%;border-collapse:collapse}td{border-bottom:1px solid var(--line);padding:7px 4px;vertical-align:top;font-size:15px}td.k{color:var(--muted);width:40%}
.terms{white-space:pre-wrap;font-size:14.5px;max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:14px;background:#fafafa}
label.d{display:flex;gap:10px;align-items:flex-start;margin:10px 0;font-size:15px}label.d input{width:22px;height:22px;flex:0 0 22px;margin-top:1px}
input[type=text],input[type=email],input[type=tel]{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:9px;font-size:16px}
canvas{width:100%;height:180px;border:2px dashed #94a3b8;border-radius:10px;background:#fff;touch-action:none}
.btn{display:inline-block;background:#111;color:#fff;border:0;border-radius:10px;padding:13px 20px;font-size:16px;font-weight:600;cursor:pointer;text-decoration:none}
.btn.sec{background:#e5e7eb;color:#111}.err{background:#fee2e2;color:#991b1b;border-radius:10px;padding:12px}.ok{background:#dcfce7;color:#14532d;border-radius:10px;padding:14px}
.brand{font-weight:800;font-size:18px}
</style></head><body><div class="wrap">
<div class="brand">Startitup Global</div><div class="muted">IRS Certifying Acceptance Agent · Houston, Texas</div>
{% if e.signed %}
<div class="card"><h1>Thank you, {{e.typed_name}}</h1>
<div class="ok">✔ Your ITIN engagement form was signed on {{e.when_local}}. We have received it – there is nothing more to do on this page.</div>
<p><b>Next step:</b> we will send you your Form W-7 to sign by hand with a pen. Please sign, date and return the original to us as soon as you receive it.</p>
<p><a class="btn sec" href="/sign/itin/{{token}}/copy.pdf">Download your signed copy (PDF)</a></p></div>
{% else %}
<div class="card"><h1>{{eng.title}}</h1><p class="muted">Please check your details, read the terms, tick the boxes and sign at the bottom. It takes about 3 minutes.</p>
{% if err %}<div class="err">{{err}}</div>{% endif %}
<h2>Your details</h2><table>
<tr><td class="k">Name</td><td>{{ [a.first, a.middle, a.last]|select|join(' ') }}</td></tr>
<tr><td class="k">Date of birth</td><td>{{ a.dob|ukdate if a.dob else '' }}</td></tr>
<tr><td class="k">Passport / ID number</td><td>{{masked}}</td></tr>
<tr><td class="k">Address</td><td>{{ [a.mail_1, a.mail_2]|select|join(', ') }}</td></tr>
<tr><td class="k">Fee</td><td><b>{{e.fee}}</b></td></tr></table>
<p class="muted">Something wrong? Don't sign – reply to the message we sent you and we'll correct it.</p></div>
<form method="post" id="sf" onsubmit="return go()">
<div class="card"><h2>Terms</h2><div class="terms">{{terms}}</div></div>
<div class="card"><h2>Declarations</h2>
{% for x in decl %}<label class="d"><input type="checkbox" name="decl_{{loop.index0}}" value="1" {% if form and form.get('decl_'~loop.index0) %}checked{% endif %}> {{x}}</label>{% endfor %}</div>
<div class="card"><h2>Your contact details</h2>
<label>Email</label><input type="email" name="email" value="{{ form.email if form else '' }}" autocomplete="email">
<div style="height:10px"></div><label>Phone (with country code)</label><input type="tel" name="phone" value="{{ form.phone if form else a.phone }}" autocomplete="tel"></div>
<div class="card"><h2>Sign</h2>
<label>Type your full name</label><input type="text" name="typed_name" value="{{ form.typed_name if form else '' }}" autocomplete="name">
<div style="height:12px"></div><label>Draw your signature with your finger or mouse</label>
<canvas id="pad"></canvas><div style="margin-top:8px"><button type="button" class="btn sec" onclick="clr()">Clear</button></div>
<input type="hidden" name="sig" id="sig">
<p class="muted">By pressing "Sign and submit" you sign this engagement electronically. Your signature, the date and time, your IP address and device are recorded.</p>
<button class="btn" type="submit">Sign and submit</button></div></form>
<script>
var c=document.getElementById('pad'), x=c.getContext('2d'), drawing=false, drawn=0;
function size(){ var r=c.getBoundingClientRect(), d=window.devicePixelRatio||1; c.width=r.width*d; c.height=r.height*d; x.scale(d,d); x.lineWidth=2.4; x.lineCap='round'; x.lineJoin='round'; x.strokeStyle='#0b1f66' }
size();
function pt(e){ var r=c.getBoundingClientRect(), t=e.touches?e.touches[0]:e; return [t.clientX-r.left, t.clientY-r.top] }
function down(e){ e.preventDefault(); drawing=true; var p=pt(e); x.beginPath(); x.moveTo(p[0],p[1]) }
function move(e){ if(!drawing) return; e.preventDefault(); var p=pt(e); x.lineTo(p[0],p[1]); x.stroke(); drawn++ }
function up(){ drawing=false }
c.addEventListener('mousedown',down); c.addEventListener('mousemove',move); window.addEventListener('mouseup',up);
c.addEventListener('touchstart',down,{passive:false}); c.addEventListener('touchmove',move,{passive:false}); c.addEventListener('touchend',up);
function clr(){ x.clearRect(0,0,c.width,c.height); drawn=0 }
function go(){
  if(drawn < 8){ alert('Please draw your signature in the box.'); return false }
  if(document.querySelectorAll('[name^=decl_]:checked').length < {{decl|length}}){ alert('Please tick every declaration.'); return false }
  document.getElementById('sig').value = c.toDataURL('image/png'); return true }
</script>
{% endif %}
<p class="muted" style="text-align:center;margin-top:20px">Startitup Global Inc · 5904 Jessamine Street, Suite 15, Houston, TX 77081 · +1 406-605-9190</p>
</div></body></html>"""


# ----------------------------------------------------------------------------- KYC client portal (one link: details + documents + engagement)

KYC_TOKENS_FILE = OUT / "kyc_tokens.json"
KYC_DAYS = 30
_SEND = {"fn": None}

# what the client ticks -> W-7 box we suggest to staff
KYC_REASONS = [
    ("tax_return", "I need to file a US tax return (for example for my US company / LLC income, or to claim back US tax)", "b"),
    ("spouse", "I am married to a US citizen or green-card holder and we file a joint US tax return", "e"),
    ("dependant", "I am the child or relative (dependant) of a US citizen or resident who claims me on their US tax return", "d"),
    ("us_resident", "I live in the USA but cannot get a Social Security Number", "c"),
    ("student", "I am a student, professor or researcher in the USA", "f"),
    ("visa_family", "I am the husband/wife or child of someone working or studying in the USA on a visa", "g"),
    ("payer", "A US bank, payer, partnership or company has asked me for an ITIN", "h"),
    ("property", "I am buying or selling US property, or I have a US mortgage", "h"),
    ("other", "Something else", "h"),
]
# documents the client can upload: key (same as DOC_TYPES) -> client-facing label
KYC_DOCS = {
    "passport": "Passport – the photo page (and any US visa page)",
    "national_id": "National ID card – front and back (only if you have no passport)",
    "birth_cert": "Birth certificate (only if you have no passport) – or your US visa if you have one",
    "address": "Proof of address – a utility bill or bank statement from the last 3 months",
    "visa": "US visa, I-20, I-94 or US entry stamp",
    "tax_return": "Your US tax return (if it is already prepared)",
    "exception": "Letter from the US bank / payer / partnership, or the property or mortgage papers",
    "us_person": "Your US spouse's or parent's Social Security card or ITIN letter",
    "residency": "Proof the dependant lives in the USA – US school or medical record, US ID or US bank statement",
    "prev_itin": "Your old ITIN letter from the IRS (if you had an ITIN before)",
    "other": "Anything else you think we need",
}
KYC_FIELDS = ("first", "middle", "last", "dob", "sex", "birth_country", "birth_city", "citizenship", "email", "phone",
              "home_1", "home_2", "mail_1", "mail_2", "other_reason", "spouse_name", "spouse_tin", "dep_rel", "dep_name",
              "dep_tin", "school", "school_city", "stay", "had_itin", "old_itin", "been_us", "entry_date", "visa_type",
              "visa_number", "visa_expiry", "home_tax_id", "us_company", "us_company_state")


def blank_a() -> dict:
    a = {k: "" for k in TEXT_KEYS}
    a.update(reasons=[], coa_identity=[], coa_foreign=[], app_type="new", prev="no", id_type="passport")
    return a


def _kyc_tokens() -> dict:
    try:
        return json.loads(KYC_TOKENS_FILE.read_text())
    except Exception:
        return {}


def _kyc_job(token: str):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        abort(404)
    job = _kyc_tokens().get(token)
    if not job:
        abort(404)
    d = _job_dir(job)
    return d, json.loads((d / "job.json").read_text())


def answers_to_a(ans: dict, a: dict, kinds: set) -> dict:
    """Turn the client's KYC answers into the W-7 applicant dict (staff check it before creating the W-7)."""
    g = lambda k: (ans.get(k) or "").strip()
    a = dict(a)
    for k in ("first", "middle", "last", "dob", "sex", "birth_country", "birth_city", "citizenship", "phone"):
        a[k] = g(k) or a.get(k, "")
    a["mail_1"], a["mail_2"] = (g("mail_1"), g("mail_2")) if ans.get("mail_diff") else (g("home_1"), g("home_2"))
    a["foreign_1"], a["foreign_2"] = (g("home_1"), g("home_2")) if ans.get("mail_diff") else ("", "")
    a["mail_choice"] = "client"
    if ans.get("mail_ours"):                      # client asked for letters to come to our office
        use_our_mail(a)
        a["foreign_1"], a["foreign_2"] = g("home_1"), g("home_2")
    boxes = []
    for key, _t, box in KYC_REASONS:
        if ans.get(f"reason_{key}") and box not in boxes:
            boxes.append(box)
    a["reasons"] = sorted(boxes)
    a["us_person_name"] = g("spouse_name") or g("dep_name")
    a["us_person_tin"] = g("spouse_tin") or g("dep_tin")
    a["rel_relationship"] = g("dep_rel")
    a["foreign_tin"] = g("home_tax_id")
    if g("visa_type") or g("visa_number"):
        exp = g("visa_expiry")
        a["visa"] = ", ".join(x for x in (g("visa_type"), g("visa_number"),
                                          ("exp. " + dt.date.fromisoformat(exp).strftime("%m/%d/%Y")) if _d(exp) else "") if x)
    a["us_entry"] = g("entry_date") if g("been_us") == "yes" else ""
    if g("school"):
        a["college"], a["college_city"], a["stay"] = g("school"), g("school_city"), g("stay")
    elif g("us_company"):
        a["college"], a["college_city"], a["stay"] = g("us_company"), g("us_company_state"), g("stay")
    if g("had_itin") == "yes":
        a["prev"], a["prev_itin"], a["prev_first"], a["prev_last"] = "yes", g("old_itin"), a["first"], a["last"]
    a["id_type"] = "other" if ans.get("no_passport") else "passport"
    if a["id_type"] == "other":
        a["id_other"] = "National ID card" if "national_id" in kinds else (a.get("id_other") or "")
    coa_map = {"passport": "passport", "national_id": "national_id", "birth_cert": "birth_cert", "visa": "visa"}
    a["coa_identity"] = [coa_map[k] for k in coa_map if k in kinds]
    a["coa_foreign"] = [coa_map[k] for k in coa_map if k in kinds and COA_ROWS[coa_map[k]][2]]
    a["client_ref"] = a.get("client_ref") or "KYC"
    return a


def kyc_ai(job: str):
    """Background: read every uploaded document with AI and fill/check the applicant record."""
    d = OUT / job
    try:
        m = json.loads((d / "job.json").read_text())
    except Exception:
        return
    a, warn, filled = dict(m["a"]), [], []
    order = ["passport", "national_id", "birth_cert", "address", "visa", "prev_itin", "us_person", "tax_return", "exception"]
    for kind in order:
        for x in [f for f in m.get("files", []) if f.get("kind") == kind][:1]:
            ai_kind = DOC_TYPES[kind][2]
            try:
                r = scan_passport((d / x["file"]).read_bytes(), x["file"], ai_kind)
            except Exception as ex:
                warn.append(f"{DOC_TYPES[kind][0].split(' – ')[0]}: AI could not read it ({ex}). Check it by eye.")
                continue
            if ai_kind == "identity" and (kind == "passport" or a.get("id_type") != "passport"):
                for fld, src in (("first", "first_name"), ("middle", "middle_name"), ("last", "surname")):
                    v = r.get(src) or ""
                    if v and a.get(fld) and v.strip().upper() != a[fld].strip().upper():
                        warn.append(f"Client typed {fld} name \"{a[fld]}\" but the {kind.replace('_', ' ')} says \"{v}\" – "
                                    f"the document version has been used (the W-7 must match the ID exactly).")
                    if v:
                        a[fld] = v
                for fld, src in (("dob", "date_of_birth"), ("citizenship", "nationality"), ("birth_country", "birth_country"),
                                 ("birth_city", "birth_city")):
                    if r.get(src):
                        if a.get(fld) and fld == "dob" and a[fld] != r[src]:
                            warn.append(f"Date of birth typed {a[fld]} but the document says {r[src]} – document used.")
                        a[fld] = r[src]
                if r.get("sex") in ("M", "F"):
                    a["sex"] = r["sex"]
                a["id_number"], a["id_issued_by"], a["id_expiry"] = r.get("document_number", ""), r.get("issuing_country", ""), r.get("expiry_date", "")
                if kind != "passport":
                    a["id_type"], a["id_other"] = "other", r.get("document_type") or a.get("id_other") or "National ID card"
                if _d(r.get("expiry_date")) and _d(r["expiry_date"]) < dt.date.today():
                    warn.append(f"⚠ The {kind.replace('_', ' ')} EXPIRED on {r['expiry_date']} – ask the client for a current document.")
                filled.append(f"identity details from the {kind.replace('_', ' ')}")
            elif ai_kind == "identity":
                pass
            elif ai_kind == "address":
                ak = "foreign" if a.get("mail_choice") == "ours" else "mail"   # our office on line 2 -> compare line 3
                if not a.get(f"{ak}_1") and r.get("address_line1"):
                    a[f"{ak}_1"], a[f"{ak}_2"] = r["address_line1"], r.get("address_line2", "")
                    filled.append("address")
                elif r.get("address_line1"):
                    stop = {"HOUSE", "FLAT", "STREET", "ST", "ROAD", "RD", "NO", "BLOCK", "APT", "APARTMENT", "SUITE", "UNIT"}
                    w1 = set(re.sub(r"\W+", " ", a.get(f"{ak}_1", "").upper()).split()) - stop
                    w2 = set(re.sub(r"\W+", " ", r["address_line1"].upper()).split()) - stop
                    if w1 and w2 and len(w1 & w2) < max(1, min(len(w1), len(w2)) // 2):
                        warn.append(f"Proof of address shows \"{r['address_line1']}, {r.get('address_line2', '')}\" – different from the address the client typed. Check which is right.")
                if r.get("name_on_document") and a.get("last") and a["last"].upper() not in r["name_on_document"].upper():
                    warn.append(f"Proof of address is in the name of \"{r['name_on_document']}\", not the applicant.")
            elif ai_kind == "visa":
                if not a.get("visa") and (r.get("visa_type") or r.get("visa_number")):
                    a["visa"] = ", ".join(x for x in (r.get("visa_type"), r.get("visa_number"),
                                                      ("exp. " + _d(r["visa_expiry"]).strftime("%m/%d/%Y")) if _d(r.get("visa_expiry")) else "") if x)
                    filled.append("visa")
                if not a.get("us_entry") and _d(r.get("entry_date")):
                    a["us_entry"] = r["entry_date"]
                    filled.append("date of entry")
            elif ai_kind == "prev_itin":
                if r.get("itin"):
                    a["prev"], a["prev_itin"] = "yes", a.get("prev_itin") or r["itin"]
                    a["prev_first"], a["prev_last"] = r.get("first_name") or a.get("prev_first", ""), r.get("last_name") or a.get("prev_last", "")
                    filled.append("previous ITIN")
            elif ai_kind == "us_person":
                a["us_person_name"] = a.get("us_person_name") or r.get("name", "")
                a["us_person_tin"] = a.get("us_person_tin") or r.get("ssn_or_itin", "")
            elif ai_kind == "tax_return":
                names = [str(n).upper() for n in (r.get("names") or [])]
                if a.get("last") and not any(a["last"].upper() in n for n in names):
                    warn.append(f"⚠ Name on the tax return ({' / '.join(names) or 'not read'}) does not match the applicant.")
            elif ai_kind == "exception":
                if r.get("suggested_exception") and not a.get("reason_h_text"):
                    a["reason_h_text"] = r["suggested_exception"]
                    if "h" not in a["reasons"]:
                        a["reasons"] = sorted(set(a["reasons"]) | {"h"})
                    filled.append(f"box h → {r['suggested_exception']}")
                if r.get("is_original_on_letterhead") is False:
                    warn.append("The exception letter looks like a copy – the IRS needs the original on letterhead.")
            if r.get("notes"):
                warn.append(f"AI note ({kind.replace('_', ' ')}): {r['notes']}")
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        m["a"] = a
        m["applicant"] = " ".join(x for x in (a["first"], a["middle"], a["last"]) if x) or m.get("applicant", "")
        m.setdefault("kyc", {})["ai"] = {"status": "done", "when": dt.datetime.now().isoformat(timespec="seconds"),
                                         "warnings": warn, "filled": filled}
        (d / "job.json").write_text(json.dumps(m, indent=1))


def _base_url() -> str:
    return (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0] + "://" + request.host


def _kyc_email_body(first: str, link: str, expires: str) -> str:
    return (f"Hello {first},\n\nThank you for choosing Startitup Global for your ITIN (US tax number) application.\n\n"
            f"Please open this secure link, fill in your details, upload your documents and sign our engagement form. "
            f"It takes about 10 minutes and works on your phone:\n\n{link}\n\n"
            f"You can save and come back later. The link works until {expires}.\n\n"
            f"Please have ready: your passport (photo page), your home address, and any US documents you have "
            f"(US visa, US tax return, old ITIN letter).\n\nThank you,\nStartitup Global\n"
            f"IRS Certifying Acceptance Agent · +1 406-605-9190")


def _send_mail(to: str, subject: str, body: str) -> str:
    """'' on success, otherwise the error text."""
    if not _SEND["fn"]:
        return "email sending is not available"
    try:
        _SEND["fn"](_cfg(), to, subject, body, [])
        return ""
    except Exception as ex:
        return str(ex)


@bp.post("/itin/kyc/new")
def itin_kyc_new():
    f = request.form
    first, last = (f.get("first") or "").strip(), (f.get("last") or "").strip()
    email, fee = (f.get("email") or "").strip(), (f.get("fee") or "").strip()
    if not first or not last:
        flash("⚠ Enter the client's first and last name.")
        return redirect("/itin#kyc")
    if not fee:
        flash("⚠ Enter the fee for the engagement form (e.g. USD 250).")
        return redirect("/itin#kyc")
    if f.get("send_email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("⚠ Enter a valid email address, or untick \"Email the link\".")
        return redirect("/itin#kyc")
    job, tok = uuid.uuid4().hex[:12], uuid.uuid4().hex
    d = OUT / job
    d.mkdir(parents=True, exist_ok=True)
    a = blank_a()
    a.update(first=first, last=last, client_ref=(f.get("client_ref") or "").strip())
    exp = (dt.date.today() + dt.timedelta(days=KYC_DAYS)).isoformat()
    now = dt.datetime.now().isoformat(timespec="seconds")
    staff = session.get("user") or os.environ.get("MAILSORT_USER", "admin")
    m = {"job": job, "when": now, "applicant": f"{first} {last}", "a": a, "status": "kyc_sent", "staff": staff, "files": [],
         "kyc": {"token": tok, "created": now, "expires": exp, "email": email, "whatsapp": (f.get("whatsapp") or "").strip(),
                 "answers": {"first": first, "last": last, "email": email}},
         "engage": {"fee": fee}, "history": [{"when": now, "stage": "kyc_sent", "staff": staff}],
         "caa": {k: load_caa().get(k, "") for k in ("company", "responsible_party", "office_code")}}
    with _LOCK:
        (d / "job.json").write_text(json.dumps(m, indent=1))
        t = _kyc_tokens()
        t[tok] = job
        KYC_TOKENS_FILE.write_text(json.dumps(t, indent=1))
    _append_log({"job": job, "when": now, "applicant": f"{first} {last}", "reasons": [], "app_type": "new",
                 "client_ref": a["client_ref"], "staff": staff, "nfiles": 0})
    link = f"{_base_url()}/sign/kyc/{tok}"
    if f.get("send_email"):
        err = _send_mail(email, "Your ITIN application – Startitup Global",
                         _kyc_email_body(first, link, dt.date.fromisoformat(exp).strftime("%d/%m/%Y")))
        flash(f"⚠ The link was created but the email could not be sent: {err}. Copy the link and send it yourself." if err
              else f"KYC link emailed to {email}.")
    else:
        flash("KYC link created – copy it or send it by WhatsApp.")
    _ALERT_CACHE["at"] = 0
    return redirect(f"/itin/job/{job}")


@bp.post("/itin/job/<job>/kyc/<action>")
def itin_kyc_action(job, action):
    d = _job_dir(job)
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        k = m.get("kyc") or {}
        if not k.get("token"):
            abort(404)
        if action == "extend":
            k["expires"] = (dt.date.today() + dt.timedelta(days=KYC_DAYS)).isoformat()
            msg = f"Link extended to {_d(k['expires']).strftime('%d/%m/%Y')}."
        elif action == "reopen":
            k["submitted"] = ""
            m["status"] = "kyc_sent"
            k["expires"] = max(k.get("expires", ""), (dt.date.today() + dt.timedelta(days=7)).isoformat())
            msg = "The client can edit and submit again."
        elif action == "email":
            to = (request.form.get("email") or k.get("email") or "").strip()
            err = _send_mail(to, "Reminder: your ITIN application – Startitup Global",
                             _kyc_email_body(m["a"].get("first") or "", f"{_base_url()}/sign/kyc/{k['token']}",
                                             _d(k["expires"]).strftime("%d/%m/%Y")))
            k["email"] = to
            msg = f"⚠ Email not sent: {err}" if err else f"Link emailed to {to}."
        else:
            abort(404)
        m["kyc"] = k
        (d / "job.json").write_text(json.dumps(m, indent=1))
    _ALERT_CACHE["at"] = 0
    flash(msg)
    return redirect(f"/itin/job/{job}#kyc")


@bp.get("/itin/review/<job>")
def itin_review(job):
    d = _job_dir(job)
    m = json.loads((d / "job.json").read_text())
    caa = load_caa()
    return render_template_string(PAGE, reasons=REASONS, id_types=ID_TYPES, coa_rows=COA_ROWS, doc_types=DOC_TYPES,
                                  levels=LEVELS, eng=load_engage(), kyc_reasons=KYC_REASONS, caa=caa, caa_missing=[], missing=[], log=[],
                                  open_jobs=[], done_jobs=[], stages=STAGES, ptin_msg="", prefill=m["a"], from_job=job,
                                  existing=uploaded_kinds(m), review=m,
                                  has_ai=bool(_cfg().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")))


# --- client side (no login, /sign/ prefix) ---

def _kyc_render(m, token, err="", saved=False):
    k = m.get("kyc") or {}
    eng = load_engage()
    fee = (m.get("engage") or {}).get("fee", "")
    return render_template_string(KYC_PAGE, m=m, k=k, ans=k.get("answers") or {}, token=token, err=err, saved=saved,
                                  reasons=KYC_REASONS, docs=KYC_DOCS, up=uploaded_kinds(m), decl=DECLARATIONS, eng=eng,
                                  fee=fee, terms=eng["terms"].replace("{fee}", fee or "as agreed with us in writing"),
                                  our_mail=", ".join(x for x in (load_caa().get("our_mail_1", "").replace("c/o ", ""), load_caa().get("our_mail_2", "")) if x),
                                  expired=bool(_d(k.get("expires")) and _d(k["expires"]) < dt.date.today()))


@bp.get("/sign/kyc/<token>")
def kyc_page(token):
    d, m = _kyc_job(token)
    return _kyc_render(m, token, saved=request.args.get("saved") == "1")


@bp.post("/sign/kyc/<token>")
def kyc_submit(token):
    d, m = _kyc_job(token)
    k = m.get("kyc") or {}
    if k.get("submitted"):
        return redirect(f"/sign/kyc/{token}")
    if _d(k.get("expires")) and _d(k["expires"]) < dt.date.today():
        return _kyc_render(m, token)
    f = request.form
    ans = {fld: (f.get(fld) or "").strip()[:300] for fld in KYC_FIELDS}
    for key, _t, _b in KYC_REASONS:
        ans[f"reason_{key}"] = bool(f.get(f"reason_{key}"))
    ans["mail_diff"], ans["no_passport"] = bool(f.get("mail_diff")), bool(f.get("no_passport"))
    ans["mail_ours"] = bool(f.get("mail_ours"))
    if ans["mail_ours"]:
        ans["mail_diff"] = False
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        tag = _safe(f"{ans['last'] or m['a']['last']}_{ans['first'] or m['a']['first']}")
        try:
            save_docs(d, tag, m["files"], lambda key: request.files.getlist(f"doc_{key}") if key in KYC_DOCS else [])
        except Exception as ex:
            (d / "job.json").write_text(json.dumps(m, indent=1))
            return _kyc_render(m, token, err=str(ex))
        m["kyc"]["answers"] = ans
        m["kyc"]["saved"] = dt.datetime.now().isoformat(timespec="seconds")
        (d / "job.json").write_text(json.dumps(m, indent=1))
    if f.get("action") == "save":
        return redirect(f"/sign/kyc/{token}?saved=1")
    # --- final submit: check and sign
    kinds = set(uploaded_kinds(m))
    errs = []
    for fld, lbl in (("first", "first name"), ("last", "last name"), ("dob", "date of birth"), ("citizenship", "citizenship"),
                     ("birth_country", "country of birth"), ("home_1", "home address"), ("home_2", "town / country of your address"),
                     ("phone", "phone number")):
        if not ans[fld]:
            errs.append(f"Please enter your {lbl}.")
    if ans["mail_diff"] and not (ans["mail_1"] and ans["mail_2"]):
        errs.append("Please enter your mailing address, or untick \"send my letters to a different address\".")
    if not any(ans[f"reason_{key}"] for key, _t, _b in KYC_REASONS):
        errs.append("Please tick why you need an ITIN.")
    if ans["reason_other"] and not ans["other_reason"]:
        errs.append("Please tell us briefly why you need an ITIN.")
    if not ans["no_passport"] and "passport" not in kinds:
        errs.append("Please upload your passport photo page.")
    if ans["no_passport"] and len(kinds & {"national_id", "birth_cert", "visa"}) < 2:
        errs.append("Without a passport please upload two documents, e.g. national ID card and birth certificate.")
    if (ans["reason_payer"] or ans["reason_property"]) and "exception" not in kinds:
        errs.append("Please upload the letter from the US bank / payer, or the property / mortgage papers.")
    if ans["reason_dependant"] and "residency" not in kinds:
        errs.append("Please upload proof that the dependant lives in the USA.")
    typed, sig = (f.get("typed_name") or "").strip(), f.get("sig") or ""
    if len(typed) < 3:
        errs.append("Please type your full name to sign.")
    if not all(f.get(f"decl_{i}") for i in range(len(DECLARATIONS))):
        errs.append("Please tick every declaration.")
    if not _sig_ok(sig):
        errs.append("Please draw your signature in the box.")
    if errs:
        return _kyc_render(m, token, err=" ".join(errs))
    now = dt.datetime.now(dt.timezone.utc)
    with _LOCK:
        m = json.loads((d / "job.json").read_text())
        m["a"] = answers_to_a(ans, m["a"], kinds)
        m["applicant"] = " ".join(x for x in (m["a"]["first"], m["a"]["middle"], m["a"]["last"]) if x)
        signed = {"typed_name": typed, "email": ans["email"], "phone": ans["phone"], "when_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
                  "when_local": now.strftime("%d/%m/%Y"),
                  "ip": (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip(),
                  "ua": request.headers.get("User-Agent", ""), "token": token,
                  "sig_png": base64.b64decode(sig.split(",", 1)[1])}
        eng = load_engage()
        e = m.get("engage") or {}
        pdf = engagement_pdf(m, eng, e.get("fee", ""), signed)
        tag = _safe(f"{m['a']['last']}_{m['a']['first']}")
        fname = f"engagement_{tag}_signed.pdf"
        (d / fname).write_bytes(pdf)
        m["files"] = [x for x in m["files"] if x["file"] != fname] + [
            {"file": fname, "label": "Engagement form (signed online)", "kind": "engagement", "orig": "Signed online"}]
        e.update({kk: v for kk, v in signed.items() if kk != "sig_png"}, signed=True, file=fname)
        m["engage"] = e
        t = m.get("track") or {}
        t["consent"], t["consent_date"] = True, now.date().isoformat()
        m["track"] = t
        m["status"] = "kyc_received"
        m["kyc"]["submitted"] = dt.datetime.now().isoformat(timespec="seconds")
        m["kyc"]["ai"] = {"status": "running"}
        m.setdefault("history", []).append({"when": m["kyc"]["submitted"], "stage": "kyc_received", "staff": "client"})
        (d / "job.json").write_text(json.dumps(m, indent=1))
    job = m["job"]
    if _cfg().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"):
        threading.Thread(target=kyc_ai, args=(job,), daemon=True).start()
    else:
        with _LOCK:
            m = json.loads((d / "job.json").read_text())
            m["kyc"]["ai"] = {"status": "off", "warnings": ["AI reading is off (no Anthropic API key) – check the documents by eye."], "filled": []}
            (d / "job.json").write_text(json.dumps(m, indent=1))
    notify = load_caa().get("notify_email")
    if notify:
        _send_mail(notify, f"New ITIN KYC submitted – {m['applicant']}",
                   f"{m['applicant']} has completed the ITIN KYC form and signed the engagement form.\n\n"
                   f"Review it and create the W-7: {_base_url()}/itin/job/{job}\n")
    _ALERT_CACHE["at"] = 0
    return redirect(f"/sign/kyc/{token}")


KYC_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ITIN application – Startitup Global</title>
<style>
:root{--ink:#111;--muted:#666;--line:#e5e7eb}
*{box-sizing:border-box}body{margin:0;background:#f5f5f4;color:var(--ink);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:16px}.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:20px;margin:14px 0}
h1{font-size:22px;margin:0 0 6px}h2{font-size:18px;margin:0 0 4px}.muted{color:var(--muted);font-size:14px}
label{display:block;font-weight:600;font-size:14px;margin:12px 0 4px}.req:after{content:" *";color:#b91c1c}
input[type=text],input[type=email],input[type=tel],input[type=date],select{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:9px;font-size:16px;background:#fff}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}@media(max-width:560px){.row2{grid-template-columns:1fr}}
label.c{display:flex;gap:10px;align-items:flex-start;font-weight:500;font-size:15px;margin:10px 0;cursor:pointer}label.c input{width:22px;height:22px;flex:0 0 22px;margin-top:1px}
.radio{display:flex;gap:18px;flex-wrap:wrap}.radio label{display:flex;gap:6px;align-items:center;font-weight:500;margin:6px 0}.radio input{width:20px;height:20px}
.doc{border:1px solid var(--line);border-radius:10px;padding:12px;margin:10px 0}.doc b{display:block}.doc input[type=file]{margin-top:8px;font-size:14px;width:100%}
.tag{display:inline-block;font-size:12px;font-weight:700;border-radius:9px;padding:1px 8px;margin-left:6px}.tag.r{background:#fee2e2;color:#991b1b}.tag.o{background:#e5e7eb;color:#374151}
.have{color:#166534;font-size:14px;margin-top:4px}.sub{margin-left:32px;border-left:3px solid #e5e7eb;padding-left:12px}.hide{display:none}
.terms{white-space:pre-wrap;font-size:14px;max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:14px;background:#fafafa}
canvas{width:100%;height:170px;border:2px dashed #94a3b8;border-radius:10px;background:#fff;touch-action:none}
.btn{display:inline-block;background:#111;color:#fff;border:0;border-radius:10px;padding:13px 20px;font-size:16px;font-weight:600;cursor:pointer;text-decoration:none}
.btn.sec{background:#e5e7eb;color:#111}.err{background:#fee2e2;color:#991b1b;border-radius:10px;padding:12px;margin:10px 0}.ok{background:#dcfce7;color:#14532d;border-radius:10px;padding:14px}
.step{display:inline-flex;width:26px;height:26px;border-radius:50%;background:#111;color:#fff;font-size:14px;align-items:center;justify-content:center;margin-right:8px}
.brand{font-weight:800;font-size:18px}
</style></head><body><div class="wrap">
<div class="brand">Startitup Global</div><div class="muted">IRS Certifying Acceptance Agent · Houston, Texas</div>
{% if k.submitted %}
<div class="card"><h1>Thank you{% if ans.first %}, {{ans.first}}{% endif %}</h1>
<div class="ok">✔ We have received your details, documents and signed engagement form ({{k.submitted|ukdate}}).</div>
<p><b>What happens next:</b></p><ol><li>We check everything and prepare your Form W-7.</li><li>We send you the W-7 to <b>sign by hand with a pen</b> – please sign, date and return the original to us.</li>
<li>We send your application to the IRS. The IRS usually replies in about 2 months, by post to your address.</li></ol>
{% if (m.engage or {}).signed %}<p><a class="btn sec" href="/sign/kyc/{{token}}/copy.pdf">Download your signed engagement form</a></p>{% endif %}
<p class="muted">Questions? Reply to our message or call +1 406-605-9190.</p></div>
{% elif expired %}
<div class="card"><h1>This link has expired</h1><p>Please contact us and we will send you a new link. +1 406-605-9190</p></div>
{% else %}
<div class="card"><h1>Your ITIN application</h1>
<p class="muted" style="margin:0">About 10 minutes. Fields marked <span style="color:#b91c1c">*</span> are needed. You can press <b>Save and finish later</b> at any time and come back to this link{% if k.expires %} until {{k.expires|ukdate}}{% endif %}.</p>
{% if saved %}<div class="ok" style="margin-top:10px">✔ Saved. You can close this page and come back to the same link later.</div>{% endif %}
{% if err %}<div class="err">{{err}}</div>{% endif %}</div>

<form method="post" enctype="multipart/form-data" id="kf" onsubmit="return go()">
<input type="hidden" name="action" id="action" value="submit">

<div class="card"><h2><span class="step">1</span>About you</h2><p class="muted" style="margin:0">Write your name exactly as it is in your passport.</p>
<div class="row2"><div><label class="req">First name</label><input type="text" name="first" value="{{ans.first}}" autocomplete="given-name"></div>
<div><label>Middle name</label><input type="text" name="middle" value="{{ans.middle}}"></div></div>
<label class="req">Last name (surname)</label><input type="text" name="last" value="{{ans.last}}" autocomplete="family-name">
<div class="row2"><div><label class="req">Date of birth</label><input type="date" name="dob" value="{{ans.dob}}"></div>
<div><label>Sex (as in your passport)</label><div class="radio"><label><input type="radio" name="sex" value="M" {% if ans.sex=='M' %}checked{% endif %}> Male</label><label><input type="radio" name="sex" value="F" {% if ans.sex=='F' %}checked{% endif %}> Female</label></div></div></div>
<div class="row2"><div><label class="req">Country of birth</label><input type="text" name="birth_country" value="{{ans.birth_country}}"></div>
<div><label>City of birth</label><input type="text" name="birth_city" value="{{ans.birth_city}}"></div></div>
<label class="req">Citizenship (country)</label><input type="text" name="citizenship" value="{{ans.citizenship}}">
<div class="row2"><div><label class="req">Mobile number (with country code)</label><input type="tel" name="phone" value="{{ans.phone}}" placeholder="+92 300 1234567" autocomplete="tel"></div>
<div><label>Email</label><input type="email" name="email" value="{{ans.email}}" autocomplete="email"></div></div></div>

<div class="card"><h2><span class="step">2</span>Your home address</h2><p class="muted" style="margin:0">Where you normally live – your full street address. The IRS posts your ITIN letter here unless you choose another address below.</p>
<label class="req">House / flat number and street</label><input type="text" name="home_1" value="{{ans.home_1}}" autocomplete="address-line1">
<label class="req">Town / city, state or province, postcode and country</label><input type="text" name="home_2" value="{{ans.home_2}}" placeholder="Karachi, Sindh 75600, Pakistan">
<label class="c"><input type="checkbox" name="mail_ours" value="1" id="mail_ours" {% if ans.mail_ours %}checked{% endif %} onchange="if(this.checked)$('mail_diff').checked=false"> Send my IRS letters to Startitup Global's US office ({{our_mail}}) – we scan and email them to you</label>
<label class="c"><input type="checkbox" name="mail_diff" value="1" id="mail_diff" {% if ans.mail_diff %}checked{% endif %} onchange="if(this.checked)$('mail_ours').checked=false"> Send my IRS letters to a different address of my own</label>
<div id="x_mail" class="sub hide"><label>Street address</label><input type="text" name="mail_1" value="{{ans.mail_1}}">
<label>Town / city, state or province, postcode and country</label><input type="text" name="mail_2" value="{{ans.mail_2}}">
<p class="muted">Use your own address – not a company "care of" address or a private mailbox.</p></div></div>

<div class="card"><h2><span class="step">3</span>Why do you need an ITIN?</h2><p class="muted" style="margin:0">Tick all that apply. Not sure? Tick "Something else" and explain – we'll work it out.</p>
{% for key, txt, box in reasons %}<label class="c"><input type="checkbox" name="reason_{{key}}" value="1" id="r_{{key}}" {% if ans.get('reason_'~key) %}checked{% endif %}> {{txt}}</label>
{% if key == 'spouse' %}<div id="x_spouse" class="sub hide"><div class="row2"><div><label>Your US spouse's full name</label><input type="text" name="spouse_name" value="{{ans.spouse_name}}"></div>
<div><label>Their SSN or ITIN</label><input type="text" name="spouse_tin" value="{{ans.spouse_tin}}" placeholder="123-45-6789"></div></div></div>{% endif %}
{% if key == 'dependant' %}<div id="x_dependant" class="sub hide"><label>Your relationship to them (e.g. son, daughter, parent)</label><input type="text" name="dep_rel" value="{{ans.dep_rel}}">
<div class="row2"><div><label>Their full name</label><input type="text" name="dep_name" value="{{ans.dep_name}}"></div><div><label>Their SSN or ITIN</label><input type="text" name="dep_tin" value="{{ans.dep_tin}}"></div></div></div>{% endif %}
{% if key == 'student' %}<div id="x_student" class="sub hide"><label>College / university</label><input type="text" name="school" value="{{ans.school}}">
<div class="row2"><div><label>City and state</label><input type="text" name="school_city" value="{{ans.school_city}}"></div><div><label>How long you will stay</label><input type="text" name="stay" value="{{ans.stay}}" placeholder="e.g. 2 years"></div></div></div>{% endif %}
{% if key == 'other' %}<div id="x_other" class="sub hide"><label>Please explain briefly</label><input type="text" name="other_reason" value="{{ans.other_reason}}"></div>{% endif %}
{% endfor %}</div>

<div class="card"><h2><span class="step">4</span>A few more questions</h2>
<label>Have you ever had an ITIN or any US tax number before?</label>
<div class="radio">{% for v, l in [('no','No'),('yes','Yes'),('unsure',"I don't know")] %}<label><input type="radio" name="had_itin" value="{{v}}" {% if (ans.had_itin or 'no')==v %}checked{% endif %}> {{l}}</label>{% endfor %}</div>
<div id="x_itin" class="sub hide"><label>Your old ITIN (if you know it)</label><input type="text" name="old_itin" value="{{ans.old_itin}}" placeholder="9XX-XX-XXXX"></div>
<label>Have you ever been to the USA?</label>
<div class="radio">{% for v, l in [('no','No'),('yes','Yes')] %}<label><input type="radio" name="been_us" value="{{v}}" {% if (ans.been_us or 'no')==v %}checked{% endif %}> {{l}}</label>{% endfor %}</div>
<div id="x_us" class="sub hide"><label>Date you last entered the USA</label><input type="date" name="entry_date" value="{{ans.entry_date}}">
<div class="row2"><div><label>US visa type (e.g. B1/B2)</label><input type="text" name="visa_type" value="{{ans.visa_type}}"></div><div><label>Visa number</label><input type="text" name="visa_number" value="{{ans.visa_number}}"></div></div>
<label>Visa expiry date</label><input type="date" name="visa_expiry" value="{{ans.visa_expiry}}"></div>
<label>Your tax number in your own country (if you have one)</label><input type="text" name="home_tax_id" value="{{ans.home_tax_id}}" placeholder="e.g. NTN in Pakistan, UTR in the UK">
<div class="row2"><div><label>Your US company name (if any)</label><input type="text" name="us_company" value="{{ans.us_company}}"></div>
<div><label>State it is registered in</label><input type="text" name="us_company_state" value="{{ans.us_company_state}}" placeholder="e.g. Wyoming"></div></div></div>

<div class="card"><h2><span class="step">5</span>Upload your documents</h2>
<p class="muted" style="margin:0">Take clear colour photos with all four corners showing, or upload PDFs. Only the documents for your answers are shown.</p>
<label class="c"><input type="checkbox" name="no_passport" value="1" id="no_passport" {% if ans.no_passport %}checked{% endif %}> I don't have a valid passport</label>
{% for key, lbl in docs.items() %}<div class="doc" id="d_{{key}}"><b>{{lbl}}<span class="tag" id="t_{{key}}"></span></b>
{% if up.get(key) %}<div class="have">✔ Already uploaded: {% for x in up[key] %}{{x.orig}}{% if not loop.last %}, {% endif %}{% endfor %} – add more only if needed</div>{% endif %}
<input type="file" name="doc_{{key}}" accept="image/*,application/pdf" {% if key in ('other','exception','residency','tax_return','national_id') %}multiple{% endif %}></div>{% endfor %}
</div>

<div class="card"><h2><span class="step">6</span>Our engagement form – read and sign</h2>
<p class="muted" style="margin:0">Fee: <b>{{fee}}</b>. This is our agreement with you. You will sign the IRS Form W-7 separately, by hand, later.</p>
<div class="terms" style="margin-top:10px">{{terms}}</div>
{% for x in decl %}<label class="c"><input type="checkbox" name="decl_{{loop.index0}}" value="1"> {{x}}</label>{% endfor %}
<label class="req">Type your full name</label><input type="text" name="typed_name" autocomplete="name">
<label class="req">Draw your signature with your finger or mouse</label><canvas id="pad"></canvas>
<div style="margin-top:8px"><button type="button" class="btn sec" onclick="clr()">Clear signature</button></div>
<input type="hidden" name="sig" id="sig">
<p class="muted">By pressing "Submit" you sign electronically. The date and time, your IP address and device are recorded.</p>
<div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn" type="submit" onclick="document.getElementById('action').value='submit'">Submit</button>
<button class="btn sec" type="submit" onclick="document.getElementById('action').value='save'">Save and finish later</button></div></div>
</form>
<script>
function $(i){return document.getElementById(i)} function on(i){return $(i)&&$(i).checked}
function val(n){var e=document.querySelector('[name='+n+']:checked'); return e?e.value:''}
function show(){
  $('x_mail').classList.toggle('hide', !on('mail_diff'));
  ['spouse','dependant','student','other'].forEach(function(k){ $('x_'+k).classList.toggle('hide', !on('r_'+k)) });
  $('x_itin').classList.toggle('hide', val('had_itin')!='yes'); $('x_us').classList.toggle('hide', val('been_us')!='yes');
  var np=on('no_passport'), need={
    passport:[!np, !np], national_id:[np, np], birth_cert:[np, np], address:[true,false],
    visa:[val('been_us')=='yes'||on('r_student')||on('r_visa_family'), on('r_student')||on('r_visa_family')],
    tax_return:[on('r_tax_return')||on('r_spouse')||on('r_dependant'), false],
    exception:[on('r_payer')||on('r_property'), on('r_payer')||on('r_property')],
    us_person:[on('r_spouse')||on('r_dependant'), false], residency:[on('r_dependant'), on('r_dependant')],
    prev_itin:[val('had_itin')=='yes', false], other:[true,false]};
  for(var k in need){ var d=$('d_'+k); if(!d) continue; var has=d.querySelector('.have');
    d.classList.toggle('hide', !need[k][0] && !has); var t=$('t_'+k); t.className='tag '+(need[k][1]?'r':'o'); t.textContent=need[k][1]?'Needed':'Optional' }
}
document.getElementById('kf').addEventListener('change', show); show();
var c=$('pad'), x=c.getContext('2d'), drawing=false, drawn=0;
function size(){ var r=c.getBoundingClientRect(), d=window.devicePixelRatio||1; c.width=r.width*d; c.height=r.height*d; x.scale(d,d); x.lineWidth=2.4; x.lineCap='round'; x.lineJoin='round'; x.strokeStyle='#0b1f66' }
size();
function pt(e){ var r=c.getBoundingClientRect(), t=e.touches?e.touches[0]:e; return [t.clientX-r.left, t.clientY-r.top] }
function down(e){ e.preventDefault(); drawing=true; var p=pt(e); x.beginPath(); x.moveTo(p[0],p[1]) }
function move(e){ if(!drawing) return; e.preventDefault(); var p=pt(e); x.lineTo(p[0],p[1]); x.stroke(); drawn++ }
function up(){ drawing=false }
c.addEventListener('mousedown',down); c.addEventListener('mousemove',move); window.addEventListener('mouseup',up);
c.addEventListener('touchstart',down,{passive:false}); c.addEventListener('touchmove',move,{passive:false}); c.addEventListener('touchend',up);
function clr(){ x.clearRect(0,0,c.width,c.height); drawn=0 }
function go(){
  if($('action').value=='save') return true;
  if(drawn < 8){ alert('Please draw your signature in the box (or press "Save and finish later").'); return false }
  $('sig').value = c.toDataURL('image/png'); return true }
</script>
{% endif %}
<p class="muted" style="text-align:center;margin-top:20px">Startitup Global Inc · 5904 Jessamine Street, Suite 15, Houston, TX 77081 · +1 406-605-9190</p>
</div></body></html>"""


@bp.get("/sign/kyc/<token>/copy.pdf")
def kyc_copy(token):
    d, m = _kyc_job(token)
    e = m.get("engage") or {}
    if not e.get("signed"):
        abort(404)
    return send_file(d / e["file"], mimetype="application/pdf", download_name="ITIN_engagement_signed.pdf")
