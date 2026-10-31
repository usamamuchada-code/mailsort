"""
EIN Filing tab (v40f – Articles only, Telnyx fax) – IRS Form SS-4 (Rev. 12-2025), prepared by Startitup Global Inc and faxed to the IRS.

Self-contained add-on: app.py only imports this file and calls register(app, load_config, send_email).
Nothing in the mail-sorting, Company forms, ITIN or Formations tabs is touched.

  /ein                         -> list / tracker of EIN applications + "New EIN application"
  /ein/new                     -> creates an empty application and opens it
  /ein/<job>                   -> the application: company documents, AI read, every SS-4 line (all editable), generate
  /ein/<job>/upload   (POST)   -> add company documents (Certificate/Articles of Formation, Operating Agreement, passports ...)
  /ein/<job>/read     (POST)   -> JSON: AI reads every uploaded document and suggests a value for every SS-4 line
  /ein/<job>/save     (POST)   -> saves the form (action=generate also builds SS-4 + fax cover + fax pack)
  /ein/<job>/track    (POST)   -> tracker: status, EIN, CP 575 upload (AI reads the EIN), email EIN to client
  /ein/<job>/file/<name>       -> preview / download one output file
  /ein/settings       (POST)   -> applicant phone & fax defaults (no Third Party Designee is used)

Template: fss4.pdf (official IRS PDF, used exactly as published – only the fields are filled) in ./ein_templates
or in the main folder of the repo.

Signature: the applicant's signature is the typed name of one of the members, printed in italics on the
signature line, with the date. Staff must tick that the member has approved the form and this typed signature.
"""
from __future__ import annotations

import base64, datetime as dt, io, json, os, re, threading, uuid
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, render_template_string, request, send_file

ROOT = Path(__file__).parent
TPL = ROOT / "ein_templates" / "fss4.pdf"
if not TPL.exists():
    TPL = ROOT / "fss4.pdf"
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "ein"
SETTINGS_FILE = OUT / "settings.json"
_LOCK = threading.Lock()
_CFG = {"fn": None}
_SEND = {"fn": None}

bp = Blueprint("einforms", __name__)

# ----------------------------------------------------------------------------- defaults (editable on the page)

SETTINGS_DEFAULT = {
    "applicant_phone": "+14066059190",          # printed on every SS-4: "Applicant's telephone number"
    "applicant_fax": "+18066152858",            # printed on every SS-4: "Applicant's fax number"
    "sender_company": "Startitup Global Inc",
    "notify_email": "accounting@startitup.global",
    "telnyx_api_key": "",                       # better: Railway variable TELNYX_API_KEY
    "telnyx_connection_id": "",                 # Telnyx Fax Application ID (Mission Control → Fax → Programmable Fax)
    "fax_from": "",                             # our Telnyx fax number the faxes are sent from (E.164)
    "followup_days": "7",                       # re-fax the IRS every N days until the EIN arrives
    "followup_max": "8",                        # stop after this many follow-ups
    "read_inbox": "1",                          # read incoming faxes for the EIN (AI) and close the application
}

# IRS fax numbers (SS-4 instructions Rev. 12-2025 "Where to file / Fax-TIN")
IRS_FAX = {
    "intl": ("304-707-9471", "No legal residence / principal place of business in the US – faxing from outside the US"),
    "intl_us": ("855-215-1627", "No legal residence / principal place of business in the US – faxing from inside the US"),
    "us": ("855-641-6935", "Principal place of business in the US (50 states / DC)"),
}

ENTITY = {   # line 9a: key -> (label, checkbox index)
    "sole": ("Sole proprietor (SSN)", 0), "estate": ("Estate", 1), "partnership": ("Partnership", 2),
    "plan": ("Plan administrator", 3), "corp": ("Corporation (form number)", 4), "trust": ("Trust", 5),
    "psc": ("Personal service corporation", 6), "military": ("Military/National Guard", 7),
    "stategov": ("State/local government", 8), "church": ("Church", 9), "coop": ("Farmers' cooperative", 10),
    "fedgov": ("Federal government", 11), "nonprofit": ("Other nonprofit organization", 12), "remic": ("REMIC", 13),
    "tribal": ("Indian tribal government/enterprise", 14), "other": ("Other (specify)", 15),
}
REASON = {   # line 10: key -> (label, checkbox index)
    "started": ("Started new business (specify type)", 0), "changed": ("Changed type of organization", 1),
    "purchased": ("Purchased going business", 2), "hired": ("Hired employees", 3), "trust": ("Created a trust", 4),
    "withholding": ("Compliance with IRS withholding regulations", 5), "pension": ("Created a pension plan", 6),
    "other": ("Other (specify)", 7), "banking": ("Banking purpose (specify)", 8),
}
ACTIVITY = {  # line 16: key -> (label, checkbox index)
    "health": ("Health care & social assistance", 0), "wholesale_agent": ("Wholesale – agent/broker", 1),
    "construction": ("Construction", 2), "rental": ("Rental & leasing", 3), "transport": ("Transportation & warehousing", 4),
    "accommodation": ("Accommodation & food service", 5), "wholesale_other": ("Wholesale – other", 6), "retail": ("Retail", 7),
    "realestate": ("Real estate", 8), "manufacturing": ("Manufacturing", 9), "finance": ("Finance & insurance", 10),
    "other": ("Other (specify)", 11),
}
PRESETS = {
    "foreign_smllc": "Single-member LLC – foreign owner (Form 5472)",
    "us_smllc": "Single-member LLC – US owner",
    "mmllc": "Multi-member LLC – partnership (US and/or foreign members)",
    "llc_corp": "LLC taxed as a corporation (Form 8832 / 2553 election)",
    "ccorp": "Corporation (C corp, Form 1120)",
    "custom": "Custom – I'll set every line myself",
}
STATUS = {"draft": "Draft", "prepared": "SS-4 prepared", "faxed": "Faxed to IRS", "ein": "EIN received",
          "rejected": "Rejected by IRS", "cancelled": "Cancelled"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
US_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
             "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
             "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
             "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
             "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
             "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
             "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
             "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
             "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
             "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}

# Every line of the SS-4 we keep for an application (all editable on the page)
FIELDS = ["legal_name", "trade_name", "care_of", "mail_addr", "mail_city", "street_addr", "street_city",
          "county_state", "rp_name", "rp_tin", "is_llc", "llc_members", "llc_us", "entity", "sole_ssn", "estate_ssn",
          "plan_tin", "corp_form", "trust_tin", "nonprofit", "entity_other", "gen", "inc_state", "inc_country",
          "reason", "started_type", "changed_to", "trust_type", "pension_type", "reason_other", "banking_purpose",
          "start_date", "fy_close", "emp_ag", "emp_house", "emp_other", "form944", "first_wages", "activity",
          "activity_other", "products", "prior_ein", "prior_ein_no", "use_designee", "designee_name",
          "designee_phone", "designee_fax", "designee_address", "signer", "signer_title", "sign_date",
          "applicant_phone", "applicant_fax", "sig_mode", "sig_ok", "fax_to", "client_email", "preset", "notes"]
CHECKS = {"form944", "use_designee", "sig_ok"}


# ----------------------------------------------------------------------------- storage

def _cfg() -> dict:
    try:
        return _CFG["fn"]() if _CFG["fn"] else {}
    except Exception:
        return {}


def load_settings() -> dict:
    s = dict(SETTINGS_DEFAULT)
    try:
        s.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text()).items() if k in SETTINGS_DEFAULT})
    except Exception:
        pass
    return s


def _job_dir(job: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job or ""):
        abort(404)
    d = OUT / job
    if not (d / "meta.json").is_file():
        abort(404)
    return d


def load_meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text())


def save_meta(d: Path, m: dict):
    m["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    with _LOCK:
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(m, indent=1))
        tmp.replace(d / "meta.json")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:40] or "X"


def blank_app(s: dict) -> dict:
    a = {k: "" for k in FIELDS}
    a.update({"preset": "foreign_smllc", "fy_close": "December", "emp_ag": "-0-", "emp_house": "-0-", "emp_other": "-0-",
              "first_wages": "N/A", "prior_ein": "no", "use_designee": "", "sig_mode": "typed",
              "fax_to": IRS_FAX["intl"][0], "applicant_phone": s["applicant_phone"], "applicant_fax": s["applicant_fax"],
              "sign_date": dt.date.today().isoformat()})
    apply_preset(a, "foreign_smllc")
    return a


def apply_preset(a: dict, p: str):
    """Sets lines 8a-8c, 9a, 10, 7b and the signer's title for the usual cases (IRS SS-4 instructions Rev. 12-2025)."""
    a["preset"] = p
    if p != "foreign_smllc":
        if a.get("entity_other", "").startswith("Foreign-owned"):
            a["entity_other"] = ""
        if a.get("reason_other", "").startswith("Foreign-owned"):
            a["reason_other"] = ""
    if p == "foreign_smllc":
        a.update(is_llc="yes", llc_members="1", llc_us="yes", entity="other",
                 entity_other="Foreign-owned U.S. disregarded entity-Form 5472", reason="other",
                 reason_other="Foreign-owned U.S. disregarded entity filing Form 5472", signer_title="Sole Member")
        if not a.get("rp_tin"):
            a["rp_tin"] = "Foreign"
    elif p == "us_smllc":
        # SS-4 instructions 12/2025: single-member LLC = disregarded entity -> 9a "Other" + "Disregarded entity"
        a.update(is_llc="yes", llc_members="1", llc_us="yes", entity="other", entity_other="Disregarded entity",
                 reason="started", signer_title="Sole Member")
        if a.get("rp_tin", "").lower() == "foreign":
            a["rp_tin"] = ""
    elif p == "mmllc":
        a.update(is_llc="yes", llc_us="yes", entity="partnership", reason="started", signer_title="Member")
        if not a.get("llc_members") or a.get("llc_members") == "1":
            a["llc_members"] = "2"
    elif p == "llc_corp":
        # LLC that elected corporate tax treatment: 9a Corporation + form number (1120 or 1120-S)
        a.update(is_llc="yes", llc_us="yes", entity="corp", corp_form=a.get("corp_form") or "1120", reason="started",
                 signer_title="Managing Member")
        if not a.get("llc_members"):
            a["llc_members"] = "1"
    elif p == "ccorp":
        a.update(is_llc="no", llc_members="", llc_us="", entity="corp", corp_form="1120", reason="started",
                 signer_title="President")


def all_jobs() -> list[dict]:
    rows = []
    if OUT.is_dir():
        for d in OUT.iterdir():
            if (d / "meta.json").is_file():
                try:
                    m = load_meta(d)
                    m["_alert"] = job_alert(m)
                    rows.append(m)
                except Exception:
                    pass
    rows.sort(key=lambda m: m.get("updated", ""), reverse=True)
    return rows


def _bdays(since: str) -> int:
    try:
        d0 = dt.date.fromisoformat(since[:10])
    except Exception:
        return 0
    n, d = 0, d0
    while d < dt.date.today():
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def job_alert(m: dict) -> str:
    st, t = m.get("status"), m.get("status_at", "")
    fx = m.get("faxes") or []
    if fx and fx[0].get("status") == "failed" and st not in ("ein", "cancelled"):
        n = next((i for i, x in enumerate(fx) if x.get("status") != "failed"), len(fx))
        if all(x.get("status") == "failed" for x in fx):
            return f"Fax to the IRS failed ({fx[0].get('error') or 'unknown'}) – press Send again."
        return (f"Last fax failed ({fx[0].get('error') or 'unknown'})"
                + (" – 3 failures in a row, automatic follow-ups paused. Press Re-request when ready." if n >= 3
                   else " – it will be retried automatically within the hour, or press Re-request."))
    ok = [x for x in fx if x.get("status") != "failed"]
    if ok and st == "faxed":
        t = ok[-1]["t"]
    if st == "prepared" and _bdays(t) >= 2:
        return "SS-4 prepared but not faxed yet"
    if st == "faxed" and _bdays(t) > 4:
        return f"Faxed {_bdays(t)} business days ago – no EIN yet (IRS aims for 4). Re-fax or call 267-941-1099."
    return ""


def alert_count() -> int:
    return sum(1 for m in all_jobs() if m.get("_alert"))


def set_status(m: dict, st: str, note: str = ""):
    if st not in STATUS:
        return
    if m.get("status") != st:
        m["status"] = st
        m["status_at"] = dt.datetime.now().isoformat(timespec="seconds")
        m.setdefault("history", []).insert(0, {"t": m["status_at"], "status": st, "note": note})


# ----------------------------------------------------------------------------- AI reading of company documents

def _to_images(data: bytes, filename: str, max_pages: int = 4) -> list[tuple[str, bytes]]:
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        return [("image/png", doc[i].get_pixmap(dpi=130).tobytes("png")) for i in range(min(max_pages, len(doc)))]
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    im.thumbnail((2200, 2200))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return [("image/jpeg", buf.getvalue())]


def _ai_json(system: str, images: list[tuple[str, bytes]], text: str, max_tokens: int = 2500) -> dict:
    cfg = _cfg()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic API key is set (Settings page or ANTHROPIC_API_KEY) – type the details by hand.")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(b).decode()}}
               for mt, b in images]
    content.append({"type": "text", "text": text})
    resp = client.messages.create(model=cfg.get("model") or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5"),
                                  max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}])
    raw = resp.content[0].text.strip()
    mm = re.search(r"\{.*\}", raw, re.S)
    if not mm:
        raise RuntimeError("The AI reply could not be read – type the details by hand.")
    return json.loads(mm.group(0))


READ_SYSTEM = """You read company formation documents (usually only the state-issued Articles of Organization / Certificate of
Formation) for a US tax agent preparing IRS Form SS-4 (EIN application).
The documents can be: Certificate/Articles of Formation or Organization, Articles of Incorporation, Operating Agreement,
Statement of Information / annual report, registered agent receipt, members' passports or ID cards, proof of address.
Read ONLY what is printed. Never invent. Unknown values are "". Dates as YYYY-MM-DD. Return ONLY one JSON object:
{"legal_name": "",                 // exact company name incl. LLC / Inc. as on the formation certificate
 "trade_name": "",                 // DBA only if printed
 "entity_kind": "LLC" | "Corporation" | "Partnership" | "Other" | "",
 "state_of_formation": "",         // US state, full name e.g. "Wyoming"; or the foreign country
 "formation_date": "",             // filing / effective date of the certificate or articles
 "principal_address": "",          // principal office / mailing address of the company as printed (one line)
 "registered_agent_address": "",   // registered agent / registered office address (one line)
 "members": [{"name": "", "role": "",          // e.g. "Member", "Managing Member", "Manager", "Director", "President"
              "type": "person" | "company",    // "company" when the member/owner is itself a company or other entity
              "ownership_pct": "",             // e.g. "100" if printed
              "address": "",                   // residential address if printed (one line)
              "country": "",                   // country of residence / nationality, short English name
              "us_person": true | false | null, // true only if a US address, SSN or US citizenship is shown
              "tin": ""}],                     // SSN/ITIN only if printed
 "business_purpose": "",           // business activity / purpose clause, short
 "products_services": "",          // principal products or services if stated
 "fiscal_year_end_month": "",      // e.g. "December" if stated
 "county": "",                     // county of the principal US address – you MAY work this out from a US city/ZIP
 "operated_from_country": "",      // city and country where the business is actually run/managed, ONLY if a document
                                   // (e.g. the order form / questionnaire or operating agreement) says so
 "notes": ""}                      // anything the preparer should check (conflicts between documents, unreadable parts)
List every member/owner (people AND companies); for a corporation also its directors/officers. Use the operating
agreement for ownership if present. If a member is a company, ALSO add the individual(s) shown as controlling it
(director, manager, authorised signatory) with "type": "person" and role "Controls <company name>".
Do not list the registered agent or a filing service/organizer company as a member. An ORGANIZER or INCORPORATOR who
signed the filing is NOT a member unless the document expressly calls them a member/manager/owner – leave them out.
A passport or ID card gives a member's exact name, nationality and (if printed) address – merge it with the same
person from the other documents instead of listing them twice."""


def _addr_split(s: str) -> tuple[str, str]:
    """'1309 Coffeen Ave Ste 1200, Sheridan, WY 82801, USA' -> ('1309 Coffeen Ave Ste 1200', 'Sheridan, WY 82801')"""
    s = re.sub(r"\s+", " ", s or "").strip().strip(",")
    s = re.sub(r",?\s*(USA|U\.S\.A\.?|United States( of America)?)$", "", s, flags=re.I).strip().strip(",")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 3:
        return ", ".join(parts[:-2]), ", ".join(parts[-2:])
    if len(parts) == 2:
        return parts[0], parts[1]
    return s, ""


def _state_abbr(name: str) -> str:
    n = (name or "").strip().lower()
    for k, v in US_STATES.items():
        if n in (k.lower(), v.lower()):
            return k
    return ""


def _member_kind(p: dict) -> str:
    t = str(p.get("type") or "").lower()
    role = str(p.get("role") or "").lower()
    if "control" in role or (t == "person" and re.search(r"\b(director|officer|signatory)\b", role)
                             and "member" not in role):
        return "controller"
    if t in ("company", "entity", "corporation") or re.search(r"\b(ltd|limited|llc|inc|corp|gmbh|fze|fzco|plc|pvt|s\.?a\.?|b\.?v\.?)\b\.?$",
                                                            str(p.get("name") or "").lower()):
        return "company"
    return "person"


def is_us_location(s: str) -> bool:
    s = s or ""
    if re.search(r"\b(united states|usa|u\.s\.a?\.?)\b", s, re.I):
        return True
    return bool(re.search(r"\b(" + "|".join(US_STATES) + r")\b", s)) or \
        any(v.lower() in s.lower() for v in US_STATES.values())


def app_warnings(a: dict, members: list[dict]) -> list[str]:
    """Non-blocking consistency checks shown above the SS-4 lines."""
    w = []
    loc = a.get("county_state", "")
    us_loc = is_us_location(loc) if loc else None
    if us_loc is True and a.get("fax_to") != IRS_FAX["us"][0]:
        w.append(f"Line 6 shows a US location ({loc}), so the company has a US principal place of business – the IRS "
                 f"instructions say to fax {IRS_FAX['us'][0]}, not {a.get('fax_to')}. If the business is really run "
                 "from abroad, put the city and country on line 6 instead.")
    if us_loc is False and a.get("fax_to") == IRS_FAX["us"][0]:
        w.append(f"Line 6 is outside the US ({loc}) – fax {IRS_FAX['intl'][0]} (or {IRS_FAX['intl_us'][0]} if "
                 "faxing from inside the US).")
    owners = [x for x in members if x.get("kind", "person") in ("person", "company")]
    people = [x for x in members if x.get("kind", "person") in ("person", "controller")]
    if a.get("is_llc") == "yes" and owners and a.get("llc_members") and a["llc_members"].isdigit() \
            and int(a["llc_members"]) != len(owners):
        w.append(f"Line 8b says {a['llc_members']} member(s) but {len(owners)} member(s) are listed in section 3.")
    if a.get("preset") in ("foreign_smllc", "us_smllc") and len(owners) > 1:
        w.append("More than one member is listed – a single-member preset is selected. A multi-member LLC is a "
                 "partnership by default (choose \"Multi-member LLC\").")
    if a.get("preset") == "mmllc" and len(owners) == 1:
        w.append("Only one member is listed – a single-member LLC is a disregarded entity, not a partnership.")
    if any(x.get("kind") == "company" for x in owners) and not people:
        w.append("A member is a company – add the individual who controls it (Type: \"Controls a company member\"). "
                 "The responsible party on line 7a must be a person, not a company.")
    if a.get("preset") == "foreign_smllc" and owners and owners[0].get("country", "").lower() in (
            "united states", "usa", "us", "u.s."):
        w.append("The single member looks like a US person – choose \"Single-member LLC – US owner\" "
                 "(no Form 5472 wording) and enter their SSN/ITIN on line 7b.")
    if a.get("rp_tin", "").lower() == "foreign" and any(x.get("country", "").lower() in ("united states", "usa", "us")
                                                         for x in people if x.get("name") == a.get("rp_name")):
        w.append("Line 7b says Foreign but the responsible party looks like a US person – enter their SSN or ITIN.")
    return w


def hand_todo(a: dict, members: list[dict]) -> list[str]:
    """What the Articles of Organization did not give us – shown as a short checklist."""
    t = []
    if not [x for x in members if x.get("kind", "person") in ("person", "controller")]:
        t.append("Section 3 – the member(s): full name as on the passport, and country "
                 "(Articles usually don't name the members)")
    if not a.get("rp_name"):
        t.append("Line 7a – responsible party (the member who owns/controls the company)")
    if a.get("rp_name") and not a.get("rp_tin"):
        t.append("Line 7b – SSN/ITIN of the responsible party, or \"Foreign\"")
    if not a.get("mail_addr"):
        t.append("Lines 4a–4b – mailing address where the IRS should send letters")
    if not a.get("county_state"):
        t.append("Line 6 – where the business is really run (US county & state, or city & country)")
    if not a.get("activity"):
        t.append("Line 16 – principal activity")
    if not a.get("products"):
        t.append("Line 17 – what the company sells / does")
    if not a.get("start_date"):
        t.append("Line 11 – date started (usually the filing date on the Articles)")
    return t


def suggestions_from_ai(ai: dict, a: dict) -> tuple[dict, list[dict], str]:
    """Turns the AI reading into SS-4 values. Returns (values, members, preset)."""
    members = []
    for p in ai.get("members") or []:
        if isinstance(p, dict) and str(p.get("name") or "").strip():
            members.append({"name": str(p.get("name")).strip(), "role": str(p.get("role") or "").strip(),
                            "kind": _member_kind(p),
                            "country": str(p.get("country") or "").strip(), "pct": str(p.get("ownership_pct") or "").strip(),
                            "us": p.get("us_person"), "tin": str(p.get("tin") or "").strip(),
                            "address": str(p.get("address") or "").strip()})
    kind = str(ai.get("entity_kind") or "").lower()
    owners = [m for m in members if m["kind"] in ("person", "company")]
    people = [m for m in members if m["kind"] in ("person", "controller")]
    n = len(owners)
    any_us = any(m["us"] is True for m in owners)
    if kind == "corporation":
        preset = "ccorp"
    elif kind == "llc" or "llc" in str(ai.get("legal_name") or "").lower():
        preset = "mmllc" if n > 1 else ("us_smllc" if any_us else "foreign_smllc")
    else:
        preset = a.get("preset") or "custom"
    v = dict(a)
    apply_preset(v, preset)
    v["legal_name"] = str(ai.get("legal_name") or "").strip()
    v["trade_name"] = str(ai.get("trade_name") or "").strip()
    principal = str(ai.get("principal_address") or "").strip()
    addr = principal or str(ai.get("registered_agent_address") or "").strip()
    if addr:
        v["mail_addr"], v["mail_city"] = _addr_split(addr)
    st = str(ai.get("state_of_formation") or "").strip()
    county = str(ai.get("county") or "").strip()
    ab = _state_abbr(st)
    m_st = re.search(r"\b([A-Z]{2})\s+\d{5}", v.get("mail_city", ""))
    st_for_county = (m_st.group(1) if m_st else "") or ab
    run_from = str(ai.get("operated_from_country") or "").strip()
    v["county_state"] = ""
    if run_from and not is_us_location(run_from) and run_from.lower() not in ("us", "u.s."):
        v["county_state"] = run_from                 # business run from abroad -> city/country (staff add the city)
    elif county and principal:                       # only from a real principal office, never the registered agent
        if "county" not in county.lower() and "parish" not in county.lower():
            county += " County"
        v["county_state"] = f"{county}, {st_for_county}" if st_for_county else county
    if v["county_state"]:
        v["fax_to"] = IRS_FAX["us"][0] if is_us_location(v["county_state"]) else IRS_FAX["intl"][0]
    if people:
        # responsible party must be an individual: managing member > any person member > controller of a company member
        lead = next((m for m in people if m["kind"] == "person" and "manag" in m["role"].lower()), None) or \
            next((m for m in people if m["kind"] == "person"), None) or people[0]
        v["rp_name"] = lead["name"]
        v["rp_tin"] = lead["tin"] or ("" if lead["us"] is True else "Foreign")
        v["signer"] = lead["name"]
        if lead["kind"] == "person" and "manag" in lead["role"].lower() and preset in ("mmllc", "llc_corp"):
            v["signer_title"] = "Managing Member"
        if lead["kind"] == "controller":
            co = next((m["name"] for m in owners if m["kind"] == "company"), "")
            v["signer_title"] = f"Director of {co}, Sole Member" if (co and n == 1) else (f"Director of {co}, Member" if co else "Director")
    if preset in ("mmllc", "llc_corp") and n:
        v["llc_members"] = str(n)
    if preset in ("ccorp", "llc_corp"):
        v["inc_state"] = st if ab else ""
        v["inc_country"] = "" if ab else st
    v["start_date"] = str(ai.get("formation_date") or "").strip()[:10]
    if ai.get("fiscal_year_end_month") in MONTHS:
        v["fy_close"] = ai["fiscal_year_end_month"]
    purpose = str(ai.get("business_purpose") or "").strip()
    prod = str(ai.get("products_services") or "").strip()
    if prod or purpose:
        v["products"] = (prod or purpose)[:90]
    if preset != "foreign_smllc" and purpose:
        v["started_type"] = purpose[:40]
    vals = {k: v[k] for k in FIELDS if k in v and str(v.get(k, "")) != "" and k not in
            ("designee_name", "designee_phone", "designee_fax", "designee_address", "applicant_phone",
             "applicant_fax", "sig_mode", "use_designee", "sign_date", "client_email", "notes", "sig_ok")}
    return vals, members, preset


def read_docs(d: Path, m: dict) -> dict:
    images = []
    for doc in m.get("docs", []):
        p = d / "docs" / doc["file"]
        if p.is_file():
            try:
                images += _to_images(p.read_bytes(), doc["file"], 4)
            except Exception:
                pass
    if not images:
        raise RuntimeError("Upload the company documents first.")
    images = images[:16]
    ai = _ai_json(READ_SYSTEM, images, "Read these documents and return the JSON.")
    vals, members, preset = suggestions_from_ai(ai, m.get("a", {}))
    return {"ai": ai, "values": vals, "members": members, "preset": preset,
            "notes": str(ai.get("notes") or ""), "t": dt.datetime.now().isoformat(timespec="seconds")}


CP575_SYSTEM = """This is an IRS notice (usually CP 575 or 147C) assigning an Employer Identification Number.
Return ONLY JSON: {"ein": "XX-XXXXXXX", "legal_name": "", "notice": "", "date": "YYYY-MM-DD", "notes": ""}.
Read only what is printed; "" when not visible."""


# ----------------------------------------------------------------------------- PDF

def _mdy(iso: str) -> str:
    try:
        return dt.date.fromisoformat((iso or "")[:10]).strftime("%m/%d/%Y")
    except Exception:
        return iso or ""


def ss4_values(a: dict) -> dict:
    v = {"f1_2[0]": a["legal_name"], "f1_3[0]": a["trade_name"], "f1_4[0]": a["care_of"],
         "f1_5[0]": a["mail_addr"], "f1_6[0]": a["mail_city"], "f1_7[0]": a["street_addr"], "f1_8[0]": a["street_city"],
         "f1_9[0]": a["county_state"], "f1_10[0]": a["rp_name"], "f1_11[0]": a["rp_tin"][:11]}
    if a["is_llc"] in ("yes", "no"):
        v["c1_1[0]" if a["is_llc"] == "yes" else "c1_1[1]"] = True
    if a["is_llc"] == "yes":
        v["f1_12[0]"] = a["llc_members"]
        if a["llc_us"] in ("yes", "no"):
            v["c1_2[0]" if a["llc_us"] == "yes" else "c1_2[1]"] = True
    if a["entity"] in ENTITY:
        v[f"c1_3[{ENTITY[a['entity']][1]}]"] = True
        extra = {"sole": ("f1_13[0]", "sole_ssn"), "estate": ("f1_14[0]", "estate_ssn"), "plan": ("f1_15[0]", "plan_tin"),
                 "corp": ("f1_16[0]", "corp_form"), "trust": ("f1_17[0]", "trust_tin"),
                 "nonprofit": ("f1_18[0]", "nonprofit"), "other": ("f1_19[0]", "entity_other")}.get(a["entity"])
        if extra:
            v[extra[0]] = a[extra[1]]
    v["f1_20[0]"] = a["gen"]
    v["f1_21[0]"], v["f1_22[0]"] = a["inc_state"], a["inc_country"]
    if a["reason"] in REASON:
        v[f"c1_4[{REASON[a['reason']][1]}]"] = True
        extra = {"started": ("f1_26[0]", "started_type"), "changed": ("f1_27[0]", "changed_to"),
                 "trust": ("f1_28[0]", "trust_type"), "pension": ("f1_29[0]", "pension_type"),
                 "other": ("f1_30[0]", "reason_other"), "banking": ("f1_24[0]", "banking_purpose")}.get(a["reason"])
        if extra:
            v[extra[0]] = a[extra[1]]
    v["f1_31[0]"] = _mdy(a["start_date"])
    v["f1_32[0]"] = a["fy_close"]
    v["f1_33[0]"], v["f1_34[0]"], v["f1_35[0]"] = a["emp_ag"], a["emp_house"], a["emp_other"]
    if a["form944"]:
        v["c1_5[0]"] = True
    v["f1_36[0]"] = a["first_wages"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a["first_wages"] or "") is None else _mdy(a["first_wages"])
    if a["activity"] in ACTIVITY:
        v[f"c1_6[{ACTIVITY[a['activity']][1]}]"] = True
    if a["activity"] == "other":
        v["f1_37[0]"] = a["activity_other"]
    v["f1_38[0]"] = a["products"]
    if a["prior_ein"] in ("yes", "no"):
        v["c1_7[0]" if a["prior_ein"] == "yes" else "c1_7[1]"] = True
    if a["prior_ein"] == "yes":
        v["f1_39[0]"] = a["prior_ein_no"][:10]
    # Third Party Designee section is left blank (v40b – no designee used)
    nt = ", ".join(x for x in (a["signer"], a["signer_title"]) if x)
    v["f1_44[0]"] = nt
    v["f1_45[0]"], v["f1_46[0]"] = a["applicant_phone"], a["applicant_fax"]
    return v


SIG_RECT = (88, 736, 326, 757)     # signature line (page 1, between "Signature" and "Date")
DATE_POS = (352, 754)


def fill_ss4(a: dict, signed: bool) -> bytes:
    import pymupdf
    doc = pymupdf.open(TPL)
    doc.select([0])
    values = ss4_values(a)
    used, ticks = set(), []
    page = doc[0]
    for w in page.widgets():
        key = w.field_name.split(".")[-1]
        if key not in values:
            continue
        val = values[key]
        if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
            w.field_value = "Off"
            if val:
                ticks.append(w.rect)
        else:
            val = "" if val is None else str(val)
            if w.text_maxlen and len(val) > w.text_maxlen:
                val = val[: w.text_maxlen]
            w.field_value = val
            if len(val) > 1 and not w.text_maxlen:
                w.text_fontsize = 0
        w.update()
        used.add(key)
    missing = set(values) - used
    if missing:
        raise RuntimeError(f"fss4.pdf has changed – fields not found: {', '.join(sorted(missing)[:8])}")
    for r in ticks:
        s_ = min(r.width, r.height)
        x, y = r.x0 + (r.width - s_) / 2, r.y0 + (r.height - s_) / 2
        page.draw_polyline([(x + s_ * .18, y + s_ * .55), (x + s_ * .42, y + s_ * .80), (x + s_ * .85, y + s_ * .18)],
                           color=(0, 0, 0), width=max(0.9, s_ * .13), overlay=True)
    if signed and a.get("signer"):
        name = a["signer"]
        fs = 14
        while fs > 8 and pymupdf.get_text_length(name, fontname="tiit", fontsize=fs) > SIG_RECT[2] - SIG_RECT[0]:
            fs -= 0.5
        page.insert_text((SIG_RECT[0], SIG_RECT[3] - 3), name, fontname="tiit", fontsize=fs, color=(0.05, 0.1, 0.45))
        page.insert_text(DATE_POS, _mdy(a.get("sign_date") or dt.date.today().isoformat()), fontname="helv",
                         fontsize=9, color=(0, 0, 0))
    doc.bake()                       # flatten so the fax machine / any viewer shows exactly the same
    buf = io.BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    doc.close()
    return buf.getvalue()


def fax_cover(a: dict, s: dict) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    pg = doc.new_page(width=612, height=792)
    y = 80
    pg.insert_text((60, y), "FAX", fontname="hebo", fontsize=30)
    pg.insert_text((60, y + 26), s["sender_company"], fontname="helv", fontsize=12, color=(.35, .35, .35))
    y += 70
    rows = [("To", "Internal Revenue Service - EIN Operation" if a["fax_to"] == IRS_FAX["us"][0]
             else "Internal Revenue Service - Attn: EIN International Operation"),
            ("To fax", a["fax_to"]),
            ("From", f"{a['legal_name']} (applicant)"),
            ("Return fax", a["applicant_fax"]),
            ("Phone", a["applicant_phone"]),
            ("Date", dt.date.today().strftime("%m/%d/%Y")),
            ("Pages", "2 (including this cover sheet)"),
            ("Re", f"Form SS-4 - Application for Employer Identification Number - {a['legal_name']}")]
    for k, v in rows:
        pg.insert_text((60, y), k + ":", fontname="hebo", fontsize=11)
        pg.insert_textbox(pymupdf.Rect(150, y - 11, 552, y + 30), v, fontname="helv", fontsize=11)
        y += 30 if len(v) < 70 else 44
    pg.draw_line((60, y), (552, y), color=(.7, .7, .7))
    y += 24
    msg = (f"Please find attached Form SS-4 for the above entity. Please fax the EIN to the applicant's fax number "
           f"{a['applicant_fax']}.\n\nThank you.")
    pg.insert_textbox(pymupdf.Rect(60, y, 552, y + 160), msg, fontname="helv", fontsize=11)
    pg.insert_textbox(pymupdf.Rect(60, 700, 552, 760),
                      "CONFIDENTIAL: this fax contains taxpayer information intended only for the Internal Revenue "
                      "Service. If you received it in error, please notify the sender and destroy it.",
                      fontname="helv", fontsize=8, color=(.4, .4, .4))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_files(d: Path, m: dict, s: dict) -> list[dict]:
    import pymupdf
    a = m["a"]
    tag = _safe(a["legal_name"])
    signed = a["sig_mode"] == "typed" and bool(a["sig_ok"]) and bool(a["signer"])
    ss4 = fill_ss4(a, signed)
    cover = fax_cover(a, s)
    out = d / "out"
    out.mkdir(exist_ok=True)
    for f in out.iterdir():
        f.unlink()
    files = [{"file": f"SS-4_{tag}{'_signed' if signed else ''}.pdf",
              "label": "Form SS-4" + (" – signed (typed signature)" if signed else " – unsigned, to sign by hand")},
             {"file": f"Fax_cover_{tag}.pdf", "label": "Fax cover sheet"},
             {"file": f"Fax_pack_{tag}.pdf", "label": "Fax pack (cover + SS-4) – send this to the IRS"}]
    (out / files[0]["file"]).write_bytes(ss4)
    (out / files[1]["file"]).write_bytes(cover)
    pack = pymupdf.open(stream=cover, filetype="pdf")
    pack.insert_pdf(pymupdf.open(stream=ss4, filetype="pdf"))
    pack.save(out / files[2]["file"], garbage=3, deflate=True)
    return files


# ----------------------------------------------------------------------------- form handling

def form_to_app(f, a_old: dict) -> tuple[dict, list[dict]]:
    a = {}
    for k in FIELDS:
        a[k] = ("1" if f.get(k) else "") if k in CHECKS else (f.get(k, a_old.get(k, "")) or "").strip()
    members = []
    for i in range(8):
        nm = (f.get(f"m_name_{i}") or "").strip()
        if nm:
            members.append({"name": nm, "role": (f.get(f"m_role_{i}") or "").strip(),
                            "kind": (f.get(f"m_kind_{i}") or "person"),
                            "country": (f.get(f"m_country_{i}") or "").strip(),
                            "pct": (f.get(f"m_pct_{i}") or "").strip()})
    return a, members


def check_app(a: dict, members: list[dict]) -> list[str]:
    e = []
    need = [("legal_name", "Line 1 – legal name"), ("mail_addr", "Line 4a – mailing address"),
            ("mail_city", "Line 4b – city, state, ZIP"), ("county_state", "Line 6 – county and state"),
            ("rp_name", "Line 7a – responsible party"), ("rp_tin", "Line 7b – SSN/ITIN or \"Foreign\""),
            ("entity", "Line 9a – type of entity"), ("reason", "Line 10 – reason for applying"),
            ("start_date", "Line 11 – date business started"), ("fy_close", "Line 12 – closing month"),
            ("activity", "Line 16 – principal activity"), ("signer", "Name of the person signing"),
            ("applicant_phone", "Applicant's telephone number")]
    e += [f"{lbl} is empty." for k, lbl in need if not a.get(k)]
    if a["rp_tin"] and a["rp_tin"].lower() != "foreign" and not re.fullmatch(r"9?\d{2}-?\d{2}-?\d{4}|\d{3}-\d{2}-\d{4}", a["rp_tin"]):
        e.append("Line 7b must be an SSN/ITIN (XXX-XX-XXXX) or the word \"Foreign\".")
    if a["is_llc"] == "yes" and not re.fullmatch(r"\d+", a["llc_members"] or ""):
        e.append("Line 8b – enter the number of LLC members.")
    if a["entity"] == "other" and not a["entity_other"]:
        e.append("Line 9a – specify the 'Other' entity type.")
    if a["entity"] == "corp" and not a["corp_form"]:
        e.append("Line 9a – enter the corporation's form number (usually 1120).")
    if a["entity"] == "corp" and not (a["inc_state"] or a["inc_country"]):
        e.append("Line 9b – state or foreign country of incorporation.")
    if a["reason"] == "other" and not a["reason_other"]:
        e.append("Line 10 – specify the 'Other' reason.")
    if a["activity"] == "other" and not a["activity_other"]:
        e.append("Line 16 – specify the 'Other' activity.")
    if a["prior_ein"] == "yes" and not a["prior_ein_no"]:
        e.append("Line 18 – enter the previous EIN.")
    if a["sig_mode"] == "typed" and a["signer"] and not a["sig_ok"]:
        e.append("Tick the box confirming the member has approved the SS-4 and the typed signature "
                 "(or choose \"Leave unsigned\").")
    people = [x["name"] for x in members if x.get("kind", "person") in ("person", "controller")]
    if a["signer"] and members and a["signer"] not in people:
        e.append("The signer must be a person listed in section 3 (not a company).")
    if a["rp_name"] and members and a["rp_name"] not in people:
        e.append("Line 7a – the responsible party must be an individual listed in section 3, not a company.")
    return e


# ----------------------------------------------------------------------------- routes

@bp.get("/ein")
def ein_home():
    jobs = all_jobs()
    for m in jobs:
        m["_age"] = ageing(m)
    return render_template_string(LIST_PAGE, jobs=jobs, STATUS=STATUS, s=load_settings(), missing=not TPL.exists(),
                                  fp_on=fax_ready(), fp_env=bool(os.environ.get("TELNYX_API_KEY")), poll=_POLL,
                                  unmatched=_load_json(UNMATCHED_FILE, []), mdy=_mdy, e164=e164,
                                  open_jobs=[m for m in jobs if m.get("status") in ("faxed", "prepared") and not m.get("ein")])


@bp.post("/ein/settings")
def ein_settings():
    s = load_settings()
    for k in SETTINGS_DEFAULT:
        if k == "telnyx_api_key" and not request.form.get(k, "").strip():
            continue                                  # blank password box = keep the saved token
        if k == "read_inbox" and "fax_form" in request.form:
            s[k] = "1" if request.form.get(k) else ""
        elif k in request.form:
            s[k] = request.form[k].strip()
    for k in ("followup_days", "followup_max"):
        if not re.fullmatch(r"\d{1,2}", s.get(k, "")) or int(s[k]) < (1 if k == "followup_days" else 0):
            s[k] = SETTINGS_DEFAULT[k]
    OUT.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=1))
    flash("EIN settings saved – they are used for new applications.")
    return redirect("/ein")


@bp.get("/ein/new")
def ein_new():
    job = uuid.uuid4().hex[:12]
    d = OUT / job
    (d / "docs").mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().isoformat(timespec="seconds")
    m = {"id": job, "created": now, "status": "draft", "status_at": now, "a": blank_app(load_settings()),
         "members": [], "docs": [], "files": [], "history": [{"t": now, "status": "draft", "note": "created"}]}
    save_meta(d, m)
    return redirect(f"/ein/{job}")


@bp.get("/ein/<job>")
def ein_job(job):
    d = _job_dir(job)
    m = load_meta(d)
    has_ai = bool(_cfg().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))
    return render_template_string(JOB_PAGE, m=m, a=m["a"], job=job, ENTITY=ENTITY, REASON=REASON, ACTIVITY=ACTIVITY,
                                  PRESETS=PRESETS, MONTHS=MONTHS, IRS_FAX=IRS_FAX, STATUS=STATUS, has_ai=has_ai,
                                  errors=m.pop("_errors", None), alert=job_alert(m), mdy=_mdy,
                                  ag=ageing(m), fp_on=fax_ready(), KINDS=FOLLOWUP_KINDS, s=load_settings(), e164=e164,
                                  warns=app_warnings(m["a"], m.get("members", [])),
                                  todo=hand_todo(m["a"], m.get("members", [])),
                                  poll=_POLL)


@bp.post("/ein/<job>/upload")
def ein_upload(job):
    d = _job_dir(job)
    m = load_meta(d)
    (d / "docs").mkdir(exist_ok=True)
    n = 0
    for f in request.files.getlist("docs"):
        if not f or not f.filename:
            continue
        data = f.read()
        if not data:
            continue
        name = f"{len(m['docs']) + 1:02d}_{_safe(Path(f.filename).stem)}{Path(f.filename).suffix.lower()[:6]}"
        (d / "docs" / name).write_bytes(data)
        m["docs"].append({"file": name, "orig": f.filename, "t": dt.datetime.now().isoformat(timespec="seconds")})
        n += 1
    save_meta(d, m)
    flash(f"{n} document(s) added." + (" Press “Read with AI” to fill the form." if n else ""))
    return redirect(f"/ein/{job}#docs")


@bp.post("/ein/<job>/doc/<int:i>/remove")
def ein_doc_remove(job, i):
    d = _job_dir(job)
    m = load_meta(d)
    if 0 <= i < len(m["docs"]):
        p = d / "docs" / m["docs"][i]["file"]
        if p.is_file():
            p.unlink()
        m["docs"].pop(i)
        save_meta(d, m)
    return redirect(f"/ein/{job}#docs")


@bp.get("/ein/<job>/doc/<int:i>")
def ein_doc(job, i):
    d = _job_dir(job)
    m = load_meta(d)
    if not 0 <= i < len(m["docs"]):
        abort(404)
    return send_file(d / "docs" / m["docs"][i]["file"], download_name=m["docs"][i]["orig"])


@bp.post("/ein/<job>/read")
def ein_read(job):
    d = _job_dir(job)
    m = load_meta(d)
    try:
        r = read_docs(d, m)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400
    m["ai"] = r
    save_meta(d, m)
    return jsonify({"values": r["values"], "members": r["members"], "preset": r["preset"], "notes": r["notes"]})


@bp.post("/ein/<job>/save")
def ein_save(job):
    d = _job_dir(job)
    m = load_meta(d)
    a, members = form_to_app(request.form, m["a"])
    old = m["a"]
    m["a"], m["members"] = a, members
    if request.form.get("action") == "generate":
        errs = check_app(a, members)
        if not TPL.exists():
            errs.append("fss4.pdf is missing from the repo (main folder or ein_templates/).")
        if errs:
            save_meta(d, m)
            for e in errs:
                flash("⚠ " + e)
            return redirect(f"/ein/{job}")
        try:
            m["files"] = build_files(d, m, load_settings())
        except Exception as ex:
            save_meta(d, m)
            flash(f"Could not create the SS-4: {ex}")
            return redirect(f"/ein/{job}")
        if a["sig_mode"] == "typed" and a["sig_ok"] and (old.get("signer") != a["signer"] or not old.get("sig_ok")):
            m.setdefault("history", []).insert(0, {"t": dt.datetime.now().isoformat(timespec="seconds"),
                                                   "status": m.get("status"),
                                                   "note": f"Typed signature of {a['signer']} confirmed as approved by the member"})
        if m.get("status") in ("draft", "prepared"):
            set_status(m, "prepared", "SS-4 created")
        save_meta(d, m)
        flash("SS-4, fax cover and fax pack created – see “Files” below.")
        return redirect(f"/ein/{job}#files")
    save_meta(d, m)
    flash("Saved.")
    return redirect(f"/ein/{job}")


@bp.get("/ein/<job>/file/<name>")
def ein_file(job, name):
    d = _job_dir(job)
    p = d / "out" / Path(name).name
    if not p.is_file():
        abort(404)
    return send_file(p, as_attachment=request.args.get("dl") == "1", download_name=p.name)


@bp.post("/ein/<job>/track")
def ein_track(job):
    d = _job_dir(job)
    m = load_meta(d)
    st = request.form.get("status") or m.get("status")
    ein = (request.form.get("ein") or "").strip()
    note = (request.form.get("note") or "").strip()
    f = request.files.get("cp575")
    if f and f.filename:
        data = f.read()
        name = "CP575_" + _safe(m["a"].get("legal_name")) + (Path(f.filename).suffix.lower()[:6] or ".pdf")
        (d / "docs").mkdir(exist_ok=True)
        (d / "docs" / name).write_bytes(data)
        m["cp575"] = name
        if not ein:
            try:
                r = _ai_json(CP575_SYSTEM, _to_images(data, f.filename, 2), "Return the JSON.", 600)
                ein = str(r.get("ein") or "").strip()
                if ein:
                    flash(f"AI read the EIN from the IRS letter: {ein} – please check it against the letter.")
            except Exception as ex:
                flash(f"Could not read the EIN from the letter ({ex}) – type it in.")
    if ein:
        digits = re.sub(r"\D", "", ein)
        if len(digits) != 9:
            flash("The EIN must have 9 digits (XX-XXXXXXX).")
        else:
            m["ein"] = f"{digits[:2]}-{digits[2:]}"
            st = "ein"
    m["a"]["client_email"] = (request.form.get("client_email") or m["a"].get("client_email") or "").strip()
    if st in STATUS:
        set_status(m, st, note)
    elif note:
        m.setdefault("history", []).insert(0, {"t": dt.datetime.now().isoformat(timespec="seconds"),
                                               "status": m.get("status"), "note": note})
    if request.form.get("email_client") and m.get("ein"):
        to = m["a"].get("client_email")
        if not to:
            flash("Enter the client's email first.")
        elif not _SEND["fn"]:
            flash("Email is not available.")
        else:
            att = [d / "docs" / m["cp575"]] if m.get("cp575") and (d / "docs" / m["cp575"]).is_file() else []
            body = (f"Dear {m['a'].get('signer') or 'Client'},\n\nThe IRS has issued the Employer Identification Number "
                    f"(EIN) for {m['a']['legal_name']}:\n\n    EIN: {m['ein']}\n\n"
                    + ("The IRS confirmation letter is attached. " if att else "")
                    + "The IRS will also post the original confirmation letter (CP 575) to the company's mailing address. "
                      "Please keep it safe – banks and payment providers usually ask for it.\n\nKind regards,\n"
                    + load_settings()["sender_company"])
            try:
                _SEND["fn"](_cfg(), to, f"EIN issued – {m['a']['legal_name']}", body, att)
                m.setdefault("history", []).insert(0, {"t": dt.datetime.now().isoformat(timespec="seconds"),
                                                       "status": m.get("status"), "note": f"EIN emailed to {to}"})
                flash(f"EIN emailed to {to}.")
            except Exception as ex:
                flash(f"Email failed: {ex}")
    save_meta(d, m)
    return redirect(f"/ein/{job}#track")


@bp.get("/ein/<job>/cp575")
def ein_cp575(job):
    d = _job_dir(job)
    m = load_meta(d)
    if not m.get("cp575"):
        abort(404)
    return send_file(d / "docs" / m["cp575"])


@bp.post("/ein/<job>/delete")
def ein_delete(job):
    d = _job_dir(job)
    m = load_meta(d)
    if (request.form.get("confirm") or "").strip().upper() != "DELETE":
        flash("Type DELETE to delete the application.")
        return redirect(f"/ein/{job}")
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    flash(f"Deleted EIN application for {m['a'].get('legal_name') or 'untitled company'}.")
    return redirect("/ein")


# ----------------------------------------------------------------------------- Telnyx Programmable Fax – send, follow up, read replies
# REST v2 (developers.telnyx.com), pay as you go. Auth: "Authorization: Bearer <API key>" (Mission Control → API Keys).
#   POST /v2/faxes  multipart: connection_id (Fax Application id), to, from (our Telnyx number, E.164), quality, contents (PDF)
#        -> 202 {"data": Fax}       Fax.status: queued, media.processing, media.processed, initiated, originated, sending,
#                                   delivered, failed (failure_reason), receiving, received
#   GET  /v2/faxes/{id}                                     -> {"data": Fax}
#   GET  /v2/faxes?filter[direction][eq]=inbound&filter[created_at][gte]=ISO&page[size]=50&page[number]=n  -> {"data":[...],"meta"}
#   POST /v2/faxes/{id}/actions/refresh                     -> refreshes an inbound fax's media_url (signed link, short-lived)
# Webhooks (Fax Application webhook URL) -> POST /ein/telnyx/webhook only "nudges" a status check; nothing in the payload is trusted.
TELNYX_API = "https://api.telnyx.com/v2"
SEEN_FILE = OUT / "fax_inbox_seen.json"
UNMATCHED_FILE = OUT / "fax_inbox_unmatched.json"
_POLL = {"started": False, "last": "", "error": "", "running": False}


class FaxError(RuntimeError):
    pass


def fax_key() -> str:
    return (os.environ.get("TELNYX_API_KEY") or load_settings().get("telnyx_api_key") or "").strip()


def fax_ready() -> bool:
    s = load_settings()
    return bool(fax_key() and s.get("telnyx_connection_id") and e164(s.get("fax_from")))


def e164(num: str) -> str:
    """'304-707-9471' / '+1 806-615-2858' / '18066152858' -> '+13047079471'"""
    d = re.sub(r"\D", "", num or "")
    if len(d) == 10:
        d = "1" + d
    return "+" + d if len(d) >= 8 else ""


def _tx(method: str, path: str, *, json_body=None, fields=None, file=None, params=None, timeout=90) -> dict:
    import urllib.request, urllib.parse, urllib.error
    key = fax_key()
    if not key:
        raise FaxError("Telnyx is not connected – add the Telnyx API key (Railway variable TELNYX_API_KEY or EIN Settings).")
    url = TELNYX_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif fields is not None:
        bnd = "----ein" + uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in fields.items():
            body.write(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        if file:
            fname, content = file
            body.write(f"--{bnd}\r\nContent-Disposition: form-data; name=\"contents\"; filename=\"{fname}\"\r\n"
                       f"Content-Type: application/pdf\r\n\r\n".encode())
            body.write(content)
            body.write(b"\r\n")
        body.write(f"--{bnd}--\r\n".encode())
        data = body.getvalue()
        headers["Content-Type"] = f"multipart/form-data; boundary={bnd}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as ex:
        txt = ex.read().decode("utf-8", "replace")[:500]
        try:
            e = (json.loads(txt).get("errors") or [{}])[0]
            txt = " – ".join(x for x in (e.get("title"), e.get("detail")) if x) or txt
        except Exception:
            pass
        if ex.code in (401, 403):
            txt = f"Telnyx refused the API key ({txt})."
        raise FaxError(f"Telnyx error {ex.code}: {txt}")
    except Exception as ex:
        raise FaxError(f"Could not reach Telnyx: {ex}")
    return json.loads(raw.decode() or "{}")


def tx_send(pdf: bytes, filename: str, to: str, s: dict) -> str:
    to_ = e164(to)
    if not to_:
        raise FaxError(f"'{to}' is not a valid fax number.")
    r = _tx("POST", "/faxes", fields={"connection_id": s.get("telnyx_connection_id", ""), "to": to_,
                                      "from": e164(s.get("fax_from")), "quality": "high"},
            file=(filename, pdf))
    fid = str((r.get("data") or {}).get("id") or "")
    if not fid:
        raise FaxError(f"Telnyx did not return a fax id: {str(r)[:200]}")
    return fid


def tx_get(fid: str) -> dict:
    return _tx("GET", f"/faxes/{fid}").get("data") or {}


def tx_list_inbound(after: dt.datetime) -> list[dict]:
    out, page = [], 1
    while page <= 10:
        r = _tx("GET", "/faxes", params={"filter[direction][eq]": "inbound",
                                         "filter[created_at][gte]": after.astimezone(dt.timezone.utc).isoformat(),
                                         "page[size]": 50, "page[number]": page})
        out += r.get("data") or []
        meta = r.get("meta") or {}
        if page >= int(meta.get("total_pages") or 1):
            break
        page += 1
    return out


def tx_inbound_pdf(fid: str) -> bytes:
    import urllib.request
    url = tx_get(fid).get("media_url")
    for attempt in range(2):
        if url:
            try:
                with urllib.request.urlopen(url, timeout=120) as r:   # signed S3 link – no auth header
                    return r.read()
            except Exception:
                pass
        _tx("POST", f"/faxes/{fid}/actions/refresh", json_body={})
        url = tx_get(fid).get("media_url")
    raise FaxError("Could not download the incoming fax (media link expired).")


FOLLOWUP_KINDS = {"first": "First fax", "auto": "Weekly follow-up (automatic)", "manual": "Re-request (button)"}


def followup_cover(a: dict, s: dict, first: str, n: int) -> bytes:
    """Cover letter for a follow-up: says clearly it is NOT a new application, so the IRS does not issue a 2nd EIN."""
    import pymupdf
    doc = pymupdf.open()
    pg = doc.new_page(width=612, height=792)
    pg.insert_text((60, 80), "FAX - FOLLOW-UP REQUEST", fontname="hebo", fontsize=22)
    pg.insert_text((60, 104), f"Request no. {n + 1} - please do NOT assign a second EIN", fontname="hebo", fontsize=12,
                   color=(.7, 0, 0))
    y = 150
    rows = [("To", "Internal Revenue Service - Attn: EIN International Operation" if a["fax_to"] != IRS_FAX["us"][0]
             else "Internal Revenue Service - EIN Operation"),
            ("To fax", a["fax_to"]), ("From", f"{a['legal_name']} (applicant)"),
            ("Return fax", a["applicant_fax"]), ("Phone", a["applicant_phone"]),
            ("Date", dt.date.today().strftime("%m/%d/%Y")), ("Pages", "2 (including this cover sheet)"),
            ("Re", f"Form SS-4 originally faxed on {first} - {a['legal_name']}")]
    for k, v in rows:
        pg.insert_text((60, y), k + ":", fontname="hebo", fontsize=11)
        pg.insert_textbox(pymupdf.Rect(150, y - 11, 552, y + 30), v, fontname="helv", fontsize=11)
        y += 30
    pg.draw_line((60, y), (552, y), color=(.7, .7, .7))
    msg = (f"We faxed the attached Form SS-4 for {a['legal_name']} on {first} and have not yet received the "
           f"Employer Identification Number.\n\n"
           "This is a FOLLOW-UP to that application, not a new application. If an EIN has already been assigned, "
           f"please fax the EIN (or the CP 575 notice) to {a['applicant_fax']} and do not assign a second number. "
           "If the application has not been processed, please process the attached copy.\n\n"
           f"Responsible party: {a['rp_name']}\n\nThank you.")
    pg.insert_textbox(pymupdf.Rect(60, y + 20, 552, y + 300), msg, fontname="helv", fontsize=11)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _ss4_file(d: Path, m: dict) -> Path | None:
    f = next((x["file"] for x in m.get("files", []) if x["file"].startswith("SS-4_")), None)
    return d / "out" / f if f and (d / "out" / f).is_file() else None


def send_job_fax(d: Path, m: dict, kind: str) -> dict:
    """Sends the fax pack (first) or a follow-up cover + SS-4 (auto/manual). Records it in m['faxes']."""
    import pymupdf
    s = load_settings()
    a = m["a"]
    ss4 = _ss4_file(d, m)
    if not ss4:
        raise FaxError("Create the SS-4 first.")
    if a.get("sig_mode") == "typed" and not ("_signed" in ss4.name):
        raise FaxError("The SS-4 is not signed – tick the member's approval and create it again.")
    if a.get("sig_mode") == "none":
        raise FaxError("This SS-4 is set to be signed by hand – fax the hand-signed copy yourself.")
    faxes = m.setdefault("faxes", [])
    sent_before = [x for x in faxes if x.get("status") != "failed"]
    if kind == "first" or not sent_before:
        kind = "first"
        pack = (d / "out" / next(x["file"] for x in m["files"] if x["file"].startswith("Fax_pack_"))).read_bytes()
    else:
        first = _mdy(next(x for x in reversed(faxes) if x.get("status") != "failed")["t"][:10])
        cov = followup_cover(a, s, first, len(sent_before))
        p = pymupdf.open(stream=cov, filetype="pdf")
        p.insert_pdf(pymupdf.open(ss4))
        pack = p.tobytes(garbage=3, deflate=True)
        fn = f"Followup_{len(sent_before)}_{_safe(a['legal_name'])}.pdf"
        (d / "out" / fn).write_bytes(pack)
    tag = f"EIN-{m['id']}-{len(faxes) + 1} {a['legal_name']}"[:120]
    entry = {"n": len(faxes) + 1, "kind": kind, "t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "to": a["fax_to"], "tag": tag, "status": "queued", "fax_id": "", "error": ""}
    try:
        entry["fax_id"] = tx_send(pack, f"SS-4_{_safe(a['legal_name'])}.pdf", a["fax_to"], s)
    except FaxError as ex:
        entry.update(status="failed", error=str(ex))
        faxes.insert(0, entry)
        raise
    faxes.insert(0, entry)
    if m.get("status") in ("draft", "prepared", "faxed"):
        set_status(m, "faxed", f"{FOLLOWUP_KINDS[kind]} sent to {a['fax_to']}")
    m.setdefault("history", []).insert(0, {"t": dt.datetime.now().isoformat(timespec="seconds"), "status": m["status"],
                                           "note": f"{FOLLOWUP_KINDS[kind]} queued (Telnyx) to {a['fax_to']}"})
    return entry




def _mins_since(iso: str) -> float:
    try:
        t = dt.datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 60
    except Exception:
        return 1e9


def _days_since(iso: str) -> int:
    try:
        t = dt.datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - t).days
    except Exception:
        return 0


def ageing(m: dict) -> dict:
    """Days since the first successful fax, days since the last one and when the next follow-up is due."""
    s = load_settings()
    ok = [x for x in m.get("faxes", []) if x.get("status") not in ("failed",)]
    if not ok:
        return {}
    first, last = ok[-1], ok[0]
    every = int(s.get("followup_days") or 7)
    due = None
    if m.get("status") == "faxed" and m.get("auto_followup", True) and \
            sum(1 for x in ok if x["kind"] != "first") < int(s.get("followup_max") or 8):
        try:
            due = (dt.datetime.fromisoformat(last["t"]) + dt.timedelta(days=every)).date()
        except Exception:
            due = None
    return {"first": first["t"], "days": _days_since(first["t"]), "last_days": _days_since(last["t"]),
            "bdays": _bdays(first["t"]), "next": due.isoformat() if due else "", "count": len(ok)}


TX_DONE = {"delivered": "delivered", "failed": "failed"}


def poll_once():
    """Every 15 minutes (and on every Telnyx webhook): update fax statuses, send weekly follow-ups, read incoming faxes."""
    if not fax_ready():
        return
    if _POLL["running"]:
        return
    _POLL["running"] = True
    try:
        _poll_inner()
    finally:
        _POLL["running"] = False


def _poll_inner():
    s = load_settings()
    dirs = [d for d in (OUT.iterdir() if OUT.is_dir() else []) if (d / "meta.json").is_file()]
    # 1. status of faxes we sent
    for d in dirs:
        try:
            m = load_meta(d)
        except Exception:
            continue
        changed = False
        for x in m.get("faxes", []):
            if x.get("status") not in ("queued", "sending") or not x.get("fax_id"):
                continue
            try:
                f = tx_get(x["fax_id"])
            except FaxError as ex:
                print(f"[ein] fax status {x['fax_id']}: {ex}", flush=True)
                continue
            st = str(f.get("status") or "")
            if st == "delivered":
                x["status"] = "delivered"
                x["pages"] = f.get("page_count") or x.get("pages")
                note = f"Fax {x['n']} delivered to the IRS"
            elif st == "failed":
                x["status"], x["error"] = "failed", str(f.get("failure_reason") or "failed").replace("_", " ")
                note = f"Fax {x['n']} FAILED: {x['error']}"
            else:
                if x["status"] != "sending":
                    x["status"], changed = "sending", True
                continue
            m.setdefault("history", []).insert(0, {"t": dt.datetime.now().isoformat(timespec="seconds"),
                                                   "status": m.get("status"), "note": note})
            changed = True
        if changed:
            save_meta(d, m)
    # 2. weekly follow-ups
    for d in dirs:
        try:
            m = load_meta(d)
            ag = ageing(m)
            fx = m.get("faxes", [])
            recent_fail = fx and fx[0].get("status") == "failed" and _mins_since(fx[0]["t"]) < 60
            fails_in_row = next((i for i, x in enumerate(fx) if x.get("status") != "failed"), len(fx))
            if ag.get("next") and ag["next"] <= dt.date.today().isoformat() and not m.get("ein") \
                    and not any(x.get("status") in ("queued", "sending") for x in fx) \
                    and not recent_fail and fails_in_row < 3:      # 3 failures in a row -> stop and alert staff
                try:
                    send_job_fax(d, m, "auto")
                except FaxError as ex:
                    m.setdefault("history", []).insert(0, {"t": dt.datetime.now().isoformat(timespec="seconds"),
                                                           "status": m.get("status"), "note": f"Weekly follow-up failed: {ex}"})
                save_meta(d, m)
        except Exception as ex:
            print(f"[ein] follow-up {d.name}: {ex}", flush=True)
    # 3. incoming faxes -> EIN
    if s.get("read_inbox", "1") and (_cfg().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")):
        open_jobs = []
        for d in dirs:
            try:
                m = load_meta(d)
            except Exception:
                continue
            if m.get("status") == "faxed" and not m.get("ein"):
                open_jobs.append((d, m))
        times = [dt.datetime.fromisoformat(x["t"]) for _, m in open_jobs for x in m.get("faxes", [])
                 if x.get("status") != "failed"]
        if times:
            read_inbox(min(times) - dt.timedelta(hours=1), open_jobs)


def _norm_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(r"\b(llc|l l c|inc|incorporated|corp|corporation|ltd|limited|co|company|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def read_inbox(since: dt.datetime, open_jobs: list):
    seen = set(_load_json(SEEN_FILE, []))
    unmatched = _load_json(UNMATCHED_FILE, [])
    for r in tx_list_inbound(since):
        fid = str(r.get("id") or "")
        if not fid or fid in seen:
            continue
        if str(r.get("status") or "") != "received":
            if str(r.get("status") or "") == "failed":
                seen.add(fid)
            continue                      # still arriving – look again next time
        seen.add(fid)
        try:
            pdf = tx_inbound_pdf(fid)
            ai = _ai_json(CP575_SYSTEM, _to_images(pdf, "fax.pdf", 3), "Return the JSON.", 600)
        except Exception as ex:
            print(f"[ein] inbox fax {fid}: {ex}", flush=True)
            seen.discard(fid)             # try again next time
            continue
        ein = re.sub(r"\D", "", str(ai.get("ein") or ""))
        if len(ein) != 9:
            continue                      # not an EIN letter
        ein = f"{ein[:2]}-{ein[2:]}"
        nm = _norm_name(ai.get("legal_name"))
        hit = [(d, m) for d, m in open_jobs if nm and _norm_name(m["a"].get("legal_name")) == nm]
        if len(hit) == 1:
            d, m = hit[0]
            name = f"CP575_{_safe(m['a']['legal_name'])}.pdf"
            (d / "docs").mkdir(exist_ok=True)
            (d / "docs" / name).write_bytes(pdf)
            m = load_meta(d)
            m.update(ein=ein, cp575=name)
            set_status(m, "ein", f"EIN {ein} received by fax – read automatically")
            save_meta(d, m)
            _notify(f"EIN received – {m['a']['legal_name']}: {ein}",
                    f"The IRS faxed the EIN for {m['a']['legal_name']}: {ein}\nFrom: {r.get('from')}\n\n"
                    f"Weekly follow-ups have stopped. Open the application to check the letter and email the client.")
        else:
            (OUT / "inbox").mkdir(parents=True, exist_ok=True)
            (OUT / "inbox" / f"{fid}.pdf").write_bytes(pdf)
            unmatched.insert(0, {"fax_id": fid, "t": str(r.get("created_at") or ""), "from": str(r.get("from") or ""),
                                 "ein": ein, "legal_name": str(ai.get("legal_name") or "")})
            _notify(f"EIN fax received – not matched: {ai.get('legal_name')}",
                    f"An EIN letter arrived by fax for '{ai.get('legal_name')}' (EIN {ein}) but it could not be matched "
                    f"to one open application. Open the EIN Filing page to assign it.")
    SEEN_FILE.write_text(json.dumps(sorted(seen)))
    UNMATCHED_FILE.write_text(json.dumps(unmatched[:200], indent=1))


def _notify(subject: str, body: str):
    to = load_settings().get("notify_email")
    if to and _SEND["fn"]:
        try:
            _SEND["fn"](_cfg(), to, subject, body, [])
        except Exception as ex:
            print(f"[ein] notify failed: {ex}", flush=True)


def _poll_loop():
    import time
    time.sleep(30)
    while True:
        try:
            poll_once()
            _POLL.update(last=dt.datetime.now().isoformat(timespec="seconds"), error="")
        except Exception as ex:
            _POLL.update(last=dt.datetime.now().isoformat(timespec="seconds"), error=str(ex))
            print(f"[ein] fax poll: {ex}", flush=True)
        time.sleep(15 * 60)


@bp.post("/ein/<job>/fax")
def ein_fax(job):
    d = _job_dir(job)
    m = load_meta(d)
    kind = request.form.get("kind", "first")
    if kind == "first" and any(x.get("status") != "failed" for x in m.get("faxes", [])):
        kind = "manual"
    if kind == "first" and not request.form.get("confirm_first"):
        flash("Tick the box to confirm the SS-4 has been checked before sending it to the IRS.")
        return redirect(f"/ein/{job}#fax")
    try:
        e = send_job_fax(d, m, kind)
        flash(f"{FOLLOWUP_KINDS[kind]} queued to {e['to']}. Status updates automatically "
              f"(or press “Check status now”).")
    except FaxError as ex:
        flash(f"Fax not sent: {ex}")
    save_meta(d, m)
    return redirect(f"/ein/{job}#fax")


@bp.post("/ein/<job>/autofollow")
def ein_autofollow(job):
    d = _job_dir(job)
    m = load_meta(d)
    m["auto_followup"] = bool(request.form.get("on"))
    save_meta(d, m)
    flash("Weekly follow-ups " + ("ON." if m["auto_followup"] else "OFF for this application."))
    return redirect(f"/ein/{job}#fax")


@bp.post("/ein/fax/poll")
def ein_poll_now():
    try:
        if not fax_ready():
            raise FaxError("Telnyx is not set up yet (API key, Fax Application ID and fax number).")
        poll_once()
        _POLL.update(last=dt.datetime.now().isoformat(timespec="seconds"), error="")
        flash("Checked – fax statuses, follow-ups and incoming faxes are up to date.")
    except Exception as ex:
        flash(f"Check failed: {ex}")
    return redirect(request.form.get("back") or "/ein")


@bp.post("/ein/fax/test")
def ein_fax_test():
    s = load_settings()
    try:
        app_id = s.get("telnyx_connection_id")
        if not app_id:
            raise FaxError("Enter the Fax Application ID in Settings.")
        r = _tx("GET", f"/fax_applications/{app_id}")
        name = (r.get("data") or {}).get("application_name") or app_id
        flash(f"Telnyx connected ✓ – Fax Application “{name}”, sending from {e164(s.get('fax_from')) or '(no number set)'}.")
    except Exception as ex:
        flash(f"Telnyx test failed: {ex}")
    return redirect("/ein")


@bp.post("/ein/telnyx/webhook")
def ein_telnyx_webhook():
    """Telnyx fax events. The payload is NOT trusted – it only triggers a status check through the API."""
    if fax_ready() and not _POLL["running"]:
        threading.Thread(target=lambda: (poll_once(), _POLL.update(last=dt.datetime.now().isoformat(timespec="seconds"))),
                         daemon=True).start()
    return jsonify({"ok": True})


@bp.get("/ein/inbox/<fax_id>")
def ein_inbox_file(fax_id):
    p = OUT / "inbox" / f"{Path(fax_id).name}.pdf"
    if not p.is_file():
        abort(404)
    return send_file(p, mimetype="application/pdf")


@bp.post("/ein/inbox/<fax_id>/assign")
def ein_inbox_assign(fax_id):
    unmatched = _load_json(UNMATCHED_FILE, [])
    item = next((x for x in unmatched if x["fax_id"] == fax_id), None)
    p = OUT / "inbox" / f"{Path(fax_id).name}.pdf"
    job = request.form.get("job", "")
    if not item or not p.is_file():
        abort(404)
    if job == "_ignore":
        unmatched = [x for x in unmatched if x["fax_id"] != fax_id]
        UNMATCHED_FILE.write_text(json.dumps(unmatched, indent=1))
        return redirect("/ein")
    d = _job_dir(job)
    m = load_meta(d)
    name = f"CP575_{_safe(m['a']['legal_name'])}.pdf"
    (d / "docs").mkdir(exist_ok=True)
    (d / "docs" / name).write_bytes(p.read_bytes())
    m.update(ein=item["ein"], cp575=name)
    set_status(m, "ein", f"EIN {item['ein']} from incoming fax assigned by staff")
    save_meta(d, m)
    UNMATCHED_FILE.write_text(json.dumps([x for x in unmatched if x["fax_id"] != fax_id], indent=1))
    flash(f"EIN {item['ein']} saved to {m['a']['legal_name']}.")
    return redirect(f"/ein/{job}#track")


def register(app, cfg_fn=None, send_fn=None):
    _CFG["fn"] = cfg_fn
    _SEND["fn"] = send_fn
    OUT.mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)
    if not _POLL["started"] and os.environ.get("EIN_NO_POLL") != "1":
        _POLL["started"] = True
        threading.Thread(target=_poll_loop, daemon=True).start()

    @app.context_processor
    def _ein_badge():
        try:
            return {"ein_alerts": alert_count()}
        except Exception:
            return {"ein_alerts": 0}


# ----------------------------------------------------------------------------- pages

LIST_PAGE = r"""{% extends "base" %}{% block body %}
<div class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
<h1 style="margin:0">EIN Filing (Form SS-4)</h1><a class="btn" href="/ein/new">+ New EIN application</a></div>
<p class="muted">Upload the company documents, let the AI fill every line of Form SS-4, check or change anything by hand,
then create the SS-4 with the member's typed signature and a fax pack for the IRS. Track each application until the EIN arrives.</p>
{% if missing %}<div class="flash">fss4.pdf is missing – upload it to the main folder of the GitHub repo.</div>{% endif %}
</div>
<div class="card"><h2>Applications</h2>
{% if not jobs %}<p class="muted">No EIN applications yet.</p>{% else %}
<table><tr><th>Company</th><th>Signer</th><th>Status</th><th>EIN</th><th>Faxed</th><th>Next follow-up</th><th></th></tr>
{% for m in jobs %}<tr{% if m._alert %} class="review"{% endif %}>
<td><b>{{ m.a.legal_name or '(not filled in yet)' }}</b>{% if m._alert %}<div class="muted" style="color:#b91c1c">⚠ {{m._alert}}</div>{% endif %}</td>
<td>{{ m.a.signer }}</td><td><span class="pill{% if m.status=='ein' %} active{% elif m.status=='rejected' %} hold{% endif %}">{{ STATUS.get(m.status, m.status) }}</span></td>
<td>{{ m.ein or '' }}</td>
<td>{% if m._age %}{{ m._age.days }} day{{ '' if m._age.days==1 else 's' }} ago<div class="muted">{{ m._age.count }} fax{{ '' if m._age.count==1 else 'es' }}</div>{% else %}<span class="muted">–</span>{% endif %}</td>
<td>{% if m._age and m._age.next and not m.ein %}{{ m._age.next|ukdate }}{% else %}<span class="muted">–</span>{% endif %}</td>
<td><a class="btn small secondary" href="/ein/{{m.id}}">Open</a></td></tr>{% endfor %}
</table>{% endif %}</div>
{% if unmatched %}<div class="card" style="border:2px solid #f59e0b"><h2>Incoming EIN faxes not matched to an application</h2>
<table><tr><th>Received</th><th>From</th><th>Company on the letter</th><th>EIN</th><th>Assign to</th></tr>
{% for u in unmatched %}<tr><td>{{u.t|ukdate}}</td><td>{{u.from}}</td><td><a href="/ein/inbox/{{u.fax_id}}" target="_blank">{{u.legal_name or '(not read)'}}</a></td><td>{{u.ein}}</td>
<td><form method="post" action="/ein/inbox/{{u.fax_id}}/assign" style="display:flex;gap:6px"><select name="job" style="padding:6px">
{% for j in open_jobs %}<option value="{{j.id}}">{{j.a.legal_name}}</option>{% endfor %}<option value="_ignore">– not ours / ignore –</option></select>
<button class="btn small">Save</button></form></td></tr>{% endfor %}</table></div>{% endif %}
<div class="card"><h2>Fax (Telnyx)</h2>
{% if fp_on %}<p>✅ Connected{% if fp_env %} (API key from the Railway variable TELNYX_API_KEY){% endif %}. Faxes go out from <b>{{s.fax_from}}</b>.
The system checks fax delivery (instantly through the Telnyx webhook, and every 15 minutes), re-faxes the IRS every <b>{{s.followup_days}} days</b> until the EIN arrives (max {{s.followup_max}} follow-ups){% if s.read_inbox %}, and reads incoming faxes for the EIN{% endif %}.</p>
{% if s.applicant_fax and e164(s.applicant_fax) != e164(s.fax_from) %}<div class="flash">⚠ The applicant's fax on the SS-4 is <b>{{s.applicant_fax}}</b>, but your Telnyx number is <b>{{s.fax_from}}</b>. The IRS faxes the EIN to the SS-4 number, so it will only be picked up automatically if you change the applicant's fax to the Telnyx number (Settings below) or port {{s.applicant_fax}} to Telnyx.</div>{% endif %}
<p class="muted">Last check: {{ poll.last|ukdate if poll.last else 'not yet' }}{% if poll.error %} – <span style="color:#b91c1c">{{poll.error}}</span>{% endif %}</p>
<div class="btnrow"><form method="post" action="/ein/fax/poll"><button class="btn small">Check now</button></form>
<form method="post" action="/ein/fax/test"><button class="btn small secondary">Test connection</button></form></div>
{% else %}<div class="flash">Fax sending is not set up yet. You need a Telnyx API key (Railway variable <b>TELNYX_API_KEY</b>, or Settings below),
the Fax Application ID and your Telnyx fax number – see the setup steps you were given. Until then, download the fax pack and fax it by hand.</div>{% endif %}
</div>
<div class="card"><details><summary style="cursor:pointer;font-weight:700">Settings – applicant phone/fax and Telnyx fax</summary>
<form method="post" action="/ein/settings"><p class="muted">Used as the starting values for every new application (each application can still be changed).</p>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:0 18px">
<div><label>Applicant's telephone number</label><input type="text" name="applicant_phone" value="{{s.applicant_phone}}"></div>
<div><label>Applicant's fax number</label><input type="text" name="applicant_fax" value="{{s.applicant_fax}}"></div>
<div><label>Our company (fax cover)</label><input type="text" name="sender_company" value="{{s.sender_company}}"></div>
<div><label>Telnyx API key</label><input type="password" name="telnyx_api_key" placeholder="{{ 'saved – leave blank to keep' if s.telnyx_api_key else ('set in Railway' if fp_env else 'paste API key') }}" autocomplete="off"></div>
<div><label>Telnyx Fax Application ID</label><input type="text" name="telnyx_connection_id" value="{{s.telnyx_connection_id}}"></div>
<div><label>Our Telnyx fax number (send from)</label><input type="text" name="fax_from" value="{{s.fax_from}}" placeholder="+1..."></div>
<div><label>Re-fax the IRS every … days</label><input type="number" name="followup_days" value="{{s.followup_days}}" min="1" max="60"></div>
<div><label>Maximum follow-ups</label><input type="number" name="followup_max" value="{{s.followup_max}}" min="0" max="52"></div>
<div><label>Email when an EIN fax arrives</label><input type="text" name="notify_email" value="{{s.notify_email}}"></div>
</div><input type="hidden" name="fax_form" value="1">
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="read_inbox" {% if s.read_inbox %}checked{% endif %} style="width:16px;height:16px"> Read incoming faxes with AI and close the application when the EIN arrives</label><p><button class="btn">Save settings</button></p></form></details></div>
{% endblock %}"""

JOB_PAGE = r"""{% extends "base" %}{% block body %}
<style>
.g2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:0 18px}
.g3{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0 18px}
select,input[type=date]{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;font-family:inherit;background:#fff}
.ln{display:inline-block;background:#111;color:#fff;border-radius:5px;padding:0 6px;font-size:11px;margin-right:6px}
.hint{font-size:12px;color:#6b7280;margin-top:3px}
.ai{background:#eff6ff !important;border-color:#60a5fa !important}
.radios label{display:inline-flex;gap:6px;align-items:center;margin-right:16px;font-weight:600}
.radios input{width:16px;height:16px}
.tick{display:flex;gap:10px;align-items:flex-start;font-weight:600;font-size:14px;margin:10px 0;cursor:pointer}
.tick input{margin-top:3px;width:16px;height:16px;flex:0 0 16px}
.sigprev{font-family:'Times New Roman',Times,serif;font-style:italic;font-size:26px;color:#13206e;border-bottom:1px solid #999;padding:4px 10px;min-height:44px;max-width:420px}
.box{background:#f7f7f7;border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:8px 0}
.err{background:#fee2e2;border:1px solid #fca5a5;color:#991b1b;padding:10px 14px;border-radius:8px;margin:10px 0}
.okb{background:#e8f7ee;border:1px solid #86efac;padding:10px 14px;border-radius:8px;margin:10px 0}
.sug td,.sug th{font-size:13px;padding:4px 8px}
.hide{display:none}
table.mem input{padding:6px}
</style>
<div class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
<h1 style="margin:0">EIN application – {{ a.legal_name or 'new company' }}</h1>
<div><span class="pill">{{ STATUS.get(m.status, m.status) }}</span>{% if m.ein %} <span class="pill active">EIN {{m.ein}}</span>{% endif %}
<a class="btn small secondary" href="/ein">← All EIN applications</a></div></div>
{% if alert %}<div class="err">⚠ {{alert}}</div>{% endif %}
</div>

<div class="card" id="docs"><h2>1. Company documents</h2>
<p class="muted">Upload the <b>Articles of Organization</b> (Certificate of Formation / Articles of Incorporation) the state issued
at incorporation – PDF or photo. The AI fills everything it can from it; anything the Articles don't show is listed below
the form as <b>“Fill by hand”</b>.</p>
{% if m.docs %}<table><tr><th>Document</th><th>Added</th><th></th></tr>
{% for x in m.docs %}<tr><td><a href="/ein/{{job}}/doc/{{loop.index0}}" target="_blank">{{x.orig}}</a></td><td>{{x.t|ukdate}}</td>
<td><form method="post" action="/ein/{{job}}/doc/{{loop.index0}}/remove" onsubmit="return confirm('Remove this document?')"><button class="btn small secondary">Remove</button></form></td></tr>{% endfor %}</table>{% endif %}
<form method="post" action="/ein/{{job}}/upload" enctype="multipart/form-data" style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<input type="file" name="docs" multiple accept=".pdf,image/*" required> <button class="btn secondary">Upload</button></form>
<div style="margin-top:14px">
<button class="btn" type="button" id="readbtn" onclick="readAI()" {% if not m.docs or not has_ai %}disabled{% endif %}>✨ Read with AI and fill the form</button>
{% if not has_ai %}<span class="muted"> – no Anthropic API key set, type the details by hand.</span>{% endif %}
<span id="readmsg" class="muted"></span></div>
<div id="sugbox" class="hide"></div>
</div>

<form method="post" action="/ein/{{job}}/save" id="ss4">
<div class="card"><h2>2. Type of company <span class="muted" style="font-weight:400">– sets lines 8a–8c, 9a, 10 and the signer's title</span></h2>
<select name="preset" id="preset" onchange="applyPreset(this.value)">
{% for k,v in PRESETS.items() %}<option value="{{k}}" {% if a.preset==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<div class="hint">Everything below can still be changed by hand.</div></div>

<div class="card"><h2>3. Members</h2>
<p class="muted">Filled from the documents. The signer and the responsible party (line 7a) are picked from the <b>people</b> in this list.
If a member is a company, also add the person who controls it. Add or correct names by hand.</p>
<table class="mem"><tr><th>Full name</th><th>Type</th><th>Role</th><th>Country</th><th>% owned</th></tr>
{% for i in range(8) %}{% set x = m.members[i] if i < m.members|length else {} %}
<tr class="mrow{% if i >= (m.members|length + 1) and i > 1 %} hide{% endif %}"><td><input type="text" name="m_name_{{i}}" value="{{x.name}}" oninput="syncMembers()"></td>
<td><select name="m_kind_{{i}}" onchange="syncMembers()" style="padding:6px">{% for k,v in [('person','Person – member'),('company','Company – member'),('controller','Person – controls a company member')] %}<option value="{{k}}" {% if (x.kind or 'person')==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select></td>
<td><input type="text" name="m_role_{{i}}" value="{{x.role}}" placeholder="Member"></td>
<td><input type="text" name="m_country_{{i}}" value="{{x.country}}"></td>
<td><input type="text" name="m_pct_{{i}}" value="{{x.pct}}" style="max-width:80px"></td></tr>{% endfor %}</table>
<button type="button" class="btn small secondary" onclick="addRow()" style="margin-top:8px">+ Add member</button>
</div>

<div class="card"><h2>4. Form SS-4 – every line</h2>
{% if todo %}<div class="box" style="border-color:#f59e0b;background:#fffbeb"><b>Fill by hand</b> (not on the Articles):
<ul style="margin:6px 0 0 18px;padding:0">{% for t in todo %}<li>{{t}}</li>{% endfor %}</ul></div>{% endif %}
{% for w in warns %}<div class="flash">⚠ {{w}}</div>{% endfor %}
<div class="g2">
<div><label><span class="ln">1</span>Legal name of entity</label><input type="text" name="legal_name" value="{{a.legal_name}}"></div>
<div><label><span class="ln">2</span>Trade name (if different)</label><input type="text" name="trade_name" value="{{a.trade_name}}"></div>
<div><label><span class="ln">3</span>Executor, administrator, trustee, "care of" name</label><input type="text" name="care_of" value="{{a.care_of}}"></div>
</div>
<div class="g2">
<div><label><span class="ln">4a</span>Mailing address (room, apt., suite no. and street, or P.O. box)</label><input type="text" name="mail_addr" value="{{a.mail_addr}}"></div>
<div><label><span class="ln">4b</span>City, state, and ZIP code (if foreign, see instructions)</label><input type="text" name="mail_city" value="{{a.mail_city}}"></div>
<div><label><span class="ln">5a</span>Street address (if different) – no P.O. box</label><input type="text" name="street_addr" value="{{a.street_addr}}"></div>
<div><label><span class="ln">5b</span>City, state, and ZIP code (if different)</label><input type="text" name="street_city" value="{{a.street_city}}"></div>
</div>
<div class="box" style="font-size:13px">Quick fill 4a/4b + 6:
<button type="button" class="btn small secondary" onclick="addr('8 The Green, Ste R','Dover, DE 19901','Kent County, DE')">Delaware – 8 The Green</button>
<button type="button" class="btn small secondary" onclick="addr('5904 Jessamine St, Ste A15','Houston, TX 77081','Harris County, TX')">Houston office</button>
<button type="button" class="btn small secondary" onclick="addr('140 E Broadway Ave, Suite 25, #804','Jackson, WY 83001','Teton County, WY')">Wyoming – Jackson office</button></div>
<div class="g2">
<div><label><span class="ln">6</span>County and state where principal business is located</label><input type="text" name="county_state" value="{{a.county_state}}" onchange="faxForLine6()">
<div class="hint">The company's real physical location. US (e.g. "Teton County, WY") → IRS fax 855-641-6935. Run from abroad → city and country (e.g. "Dubai, United Arab Emirates") → fax 304-707-9471. The fax number in section 6 follows this automatically.</div></div>
<div><label><span class="ln">7a</span>Name of responsible party</label><select name="rp_name" id="rp_name" data-v="{{a.rp_name}}" onchange="this.dataset.v=this.value"></select>
<div class="hint">Must be an individual – usually the (managing) member.</div></div>
<div><label><span class="ln">7b</span>SSN, ITIN, or EIN</label><input type="text" name="rp_tin" value="{{a.rp_tin}}" maxlength="11">
<div class="hint">Type <b>Foreign</b> if the responsible party has no SSN/ITIN.</div></div>
</div>
<div class="g3">
<div><label><span class="ln">8a</span>Is this application for an LLC?</label><div class="radios">
<label><input type="radio" name="is_llc" value="yes" {% if a.is_llc=='yes' %}checked{% endif %}>Yes</label>
<label><input type="radio" name="is_llc" value="no" {% if a.is_llc=='no' %}checked{% endif %}>No</label></div></div>
<div><label><span class="ln">8b</span>Number of LLC members</label><input type="text" name="llc_members" value="{{a.llc_members}}"></div>
<div><label><span class="ln">8c</span>LLC organized in the United States?</label><div class="radios">
<label><input type="radio" name="llc_us" value="yes" {% if a.llc_us=='yes' %}checked{% endif %}>Yes</label>
<label><input type="radio" name="llc_us" value="no" {% if a.llc_us=='no' %}checked{% endif %}>No</label></div></div>
</div>
<div class="g2">
<div><label><span class="ln">9a</span>Type of entity</label><select name="entity" onchange="showX()">
<option value="">– choose –</option>{% for k,v in ENTITY.items() %}<option value="{{k}}" {% if a.entity==k %}selected{% endif %}>{{v[0]}}</option>{% endfor %}</select></div>
<div class="x x-sole"><label>Sole proprietor – SSN</label><input type="text" name="sole_ssn" value="{{a.sole_ssn}}"></div>
<div class="x x-estate"><label>Estate – SSN of decedent</label><input type="text" name="estate_ssn" value="{{a.estate_ssn}}"></div>
<div class="x x-plan"><label>Plan administrator – TIN</label><input type="text" name="plan_tin" value="{{a.plan_tin}}"></div>
<div class="x x-corp"><label>Corporation – form number to be filed</label><input type="text" name="corp_form" value="{{a.corp_form}}"></div>
<div class="x x-trust"><label>Trust – TIN of grantor</label><input type="text" name="trust_tin" value="{{a.trust_tin}}"></div>
<div class="x x-nonprofit"><label>Nonprofit – specify</label><input type="text" name="nonprofit" value="{{a.nonprofit}}"></div>
<div class="x x-other"><label>Other – specify</label><input type="text" name="entity_other" value="{{a.entity_other}}"></div>
<div><label>Group Exemption Number (GEN), if any</label><input type="text" name="gen" value="{{a.gen}}"></div>
<div><label><span class="ln">9b</span>If a corporation – state</label><input type="text" name="inc_state" value="{{a.inc_state}}"></div>
<div><label>…or foreign country</label><input type="text" name="inc_country" value="{{a.inc_country}}"></div>
</div>
<div class="g2">
<div><label><span class="ln">10</span>Reason for applying</label><select name="reason" onchange="showX()">
<option value="">– choose –</option>{% for k,v in REASON.items() %}<option value="{{k}}" {% if a.reason==k %}selected{% endif %}>{{v[0]}}</option>{% endfor %}</select></div>
<div class="r r-started"><label>Started new business – type</label><input type="text" name="started_type" value="{{a.started_type}}"></div>
<div class="r r-changed"><label>Changed type – new type</label><input type="text" name="changed_to" value="{{a.changed_to}}"></div>
<div class="r r-trust"><label>Trust – type</label><input type="text" name="trust_type" value="{{a.trust_type}}"></div>
<div class="r r-pension"><label>Pension plan – type</label><input type="text" name="pension_type" value="{{a.pension_type}}"></div>
<div class="r r-other"><label>Other – specify</label><input type="text" name="reason_other" value="{{a.reason_other}}"></div>
<div class="r r-banking"><label>Banking purpose – specify</label><input type="text" name="banking_purpose" value="{{a.banking_purpose}}"></div>
</div>
<div class="g3">
<div><label><span class="ln">11</span>Date business started or acquired</label><input type="date" name="start_date" value="{{a.start_date}}">
<div class="hint">Printed as MM/DD/YYYY. Usually the formation date.</div></div>
<div><label><span class="ln">12</span>Closing month of accounting year</label><select name="fy_close">{% for x in MONTHS %}<option {% if a.fy_close==x %}selected{% endif %}>{{x}}</option>{% endfor %}</select></div>
<div><label><span class="ln">13</span>Employees – Agricultural</label><input type="text" name="emp_ag" value="{{a.emp_ag}}"></div>
<div><label>Household</label><input type="text" name="emp_house" value="{{a.emp_house}}"></div>
<div><label>Other</label><input type="text" name="emp_other" value="{{a.emp_other}}"></div>
<div><label><span class="ln">15</span>First date wages paid</label><input type="text" name="first_wages" value="{{a.first_wages}}"><div class="hint">"N/A" if no employees.</div></div>
</div>
<label class="tick"><input type="checkbox" name="form944" {% if a.form944 %}checked{% endif %}><span><span class="ln">14</span>File Form 944 annually instead of Form 941 (employment tax liability $1,000 or less)</span></label>
<div class="g2">
<div><label><span class="ln">16</span>Principal activity</label><select name="activity" onchange="showX()">
<option value="">– choose –</option>{% for k,v in ACTIVITY.items() %}<option value="{{k}}" {% if a.activity==k %}selected{% endif %}>{{v[0]}}</option>{% endfor %}</select></div>
<div class="act act-other"><label>Other – specify</label><input type="text" name="activity_other" value="{{a.activity_other}}" placeholder="e.g. Consulting, E-commerce"></div>
<div><label><span class="ln">17</span>Principal line of merchandise / products / services</label><input type="text" name="products" value="{{a.products}}"></div>
</div>
<div class="g3">
<div><label><span class="ln">18</span>Has the applicant entity applied for an EIN before?</label><div class="radios">
<label><input type="radio" name="prior_ein" value="yes" {% if a.prior_ein=='yes' %}checked{% endif %} onchange="showX()">Yes</label>
<label><input type="radio" name="prior_ein" value="no" {% if a.prior_ein=='no' %}checked{% endif %} onchange="showX()">No</label></div></div>
<div id="prior_box"><label>Previous EIN</label><input type="text" name="prior_ein_no" value="{{a.prior_ein_no}}"></div>
</div>
</div>

<div class="card"><h2>5. Applicant's signature</h2>
<div class="g2">
<div><label>Member signing</label><select name="signer" id="signer" data-v="{{a.signer}}" onchange="this.dataset.v=this.value;sigPrev()"></select></div>
<div><label>Title</label><input type="text" name="signer_title" value="{{a.signer_title}}"><div class="hint">Printed as "Name and title": <i>Name, Title</i></div></div>
<div><label>Date signed</label><input type="date" name="sign_date" value="{{a.sign_date}}"></div>
<div><label>Applicant's telephone number</label><input type="text" name="applicant_phone" value="{{a.applicant_phone}}"></div>
<div><label>Applicant's fax number</label><input type="text" name="applicant_fax" value="{{a.applicant_fax}}"></div>
</div>
<div class="radios" style="margin-top:14px">
<label><input type="radio" name="sig_mode" value="typed" {% if a.sig_mode!='none' %}checked{% endif %} onchange="sigPrev()">Typed signature (member's name in italics)</label>
<label><input type="radio" name="sig_mode" value="none" {% if a.sig_mode=='none' %}checked{% endif %} onchange="sigPrev()">Leave unsigned (sign by hand)</label></div>
<div id="sigbox"><label>Signature as it will appear on the SS-4</label><div class="sigprev" id="sigprev"></div>
<label class="tick"><input type="checkbox" name="sig_ok" {% if a.sig_ok %}checked{% endif %}><span>The member named above has reviewed this SS-4 and authorised us to sign it with their typed name.</span></label></div>
</div>

<div class="card"><h2>6. Fax to the IRS</h2>
<select name="fax_to">{% for k,v in IRS_FAX.items() %}<option value="{{v[0]}}" {% if a.fax_to==v[0] %}selected{% endif %}>{{v[0]}} – {{v[1]}}</option>{% endfor %}</select>
<div class="g2"><div><label>Client's email (for sending the EIN)</label><input type="text" name="client_email" value="{{a.client_email}}"></div>
<div><label>Internal notes</label><input type="text" name="notes" value="{{a.notes}}"></div></div>
<p style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap">
<button class="btn" name="action" value="generate">Create SS-4 + fax pack</button>
<button class="btn secondary" name="action" value="save">Save draft</button></p>
</div>
</form>

{% if m.files %}<div class="card" id="files"><h2>Files</h2>
<table>{% for f in m.files %}<tr><td>{{f.label}}</td><td><a class="btn small secondary" href="/ein/{{job}}/file/{{f.file}}" target="_blank">Preview</a>
<a class="btn small" href="/ein/{{job}}/file/{{f.file}}?dl=1">Download</a></td></tr>{% endfor %}</table>
<iframe src="/ein/{{job}}/file/{{m.files[0].file}}" style="width:100%;height:820px;border:1px solid var(--line);border-radius:8px;margin-top:12px"></iframe>
</div>{% endif %}

{% if m.files %}<div class="card" id="fax"><h2>Send to the IRS by fax</h2>
{% if not fp_on %}<div class="flash">Fax sending is not set up – see the Fax box on the <a href="/ein">EIN Filing page</a>. You can still download the fax pack above.</div>
{% elif e164(a.applicant_fax) != e164(s.fax_from) %}<div class="flash">⚠ The applicant's fax on this SS-4 ({{a.applicant_fax}}) is not your Telnyx number ({{s.fax_from}}) – the IRS's EIN fax will not be picked up automatically. Change it in section 5 and create the SS-4 again, or upload the IRS letter in the Tracker when it arrives.</div>{% endif %}
{% if ag %}<div class="statgrid">
<div class="stat"><b>{{ag.days}}</b>days since first fax</div><div class="stat"><b>{{ag.bdays}}</b>business days</div>
<div class="stat"><b>{{ag.count}}</b>fax{{'' if ag.count==1 else 'es'}} sent</div>
<div class="stat"><b>{{ ag.next|ukdate if ag.next and not m.ein else '–' }}</b>next follow-up</div></div>{% endif %}
{% set okfax = (m.faxes or [])|rejectattr('status','equalto','failed')|list %}
{% if not okfax %}
<form method="post" action="/ein/{{job}}/fax"><input type="hidden" name="kind" value="first">
<label class="tick"><input type="checkbox" name="confirm_first"><span>I have checked the SS-4 – send the fax pack (cover + signed SS-4) to the IRS on <b>{{a.fax_to}}</b>.</span></label>
<button class="btn" {% if not fp_on %}disabled{% endif %}>📠 Send to IRS by fax</button></form>
{% elif not m.ein %}
<div class="btnrow">
<form method="post" action="/ein/{{job}}/fax" onsubmit="return confirm('Send a follow-up request to the IRS now? It goes with a cover letter saying it is NOT a new application.')"><input type="hidden" name="kind" value="manual">
<button class="btn" {% if not fp_on %}disabled{% endif %}>🔁 Re-request EIN now</button></form>
<form method="post" action="/ein/fax/poll"><input type="hidden" name="back" value="/ein/{{job}}#fax"><button class="btn secondary">Check status now</button></form>
<form method="post" action="/ein/{{job}}/autofollow"><input type="hidden" name="on" value="{{ '1' if m.auto_followup == false else '' }}">
<button class="btn secondary">{{ 'Turn weekly follow-ups ON' if m.auto_followup == false else 'Turn weekly follow-ups OFF' }}</button></form></div>
<p class="muted">Weekly follow-ups are {{ 'OFF' if m.auto_followup == false else 'ON' }}: every {{s.followup_days}} days without an EIN the system re-faxes the IRS with a follow-up cover letter
("follow-up, not a new application – do not assign a second EIN") plus the SS-4, up to {{s.followup_max}} times.</p>
{% endif %}
{% if m.faxes %}<table style="margin-top:12px"><tr><th>#</th><th>Type</th><th>Sent</th><th>To</th><th>Status</th><th></th></tr>
{% for x in m.faxes %}<tr{% if x.status=='failed' %} class="review"{% endif %}><td>{{x.n}}</td><td>{{KINDS.get(x.kind, x.kind)}}</td><td>{{x.t|ukdate}}</td><td>{{x.to}}</td>
<td>{% if x.status=='delivered' %}<span class="pill active">Delivered{% if x.pages %} · {{x.pages}} pages{% endif %}</span>{% elif x.status=='failed' %}<span class="pill hold">Failed</span> <span class="muted">{{x.error}}</span>{% else %}<span class="pill">{{ 'Queued' if x.status=='queued' else 'Sending' }}</span>{% endif %}</td>
<td class="muted" style="font-size:11px">{{x.fax_id}}</td></tr>{% endfor %}</table>{% endif %}
</div>{% endif %}

<div class="card" id="track"><h2>Tracker</h2>
<form method="post" action="/ein/{{job}}/track" enctype="multipart/form-data">
<div class="g2">
<div><label>Status</label><select name="status">{% for k,v in STATUS.items() %}<option value="{{k}}" {% if m.status==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select></div>
<div><label>EIN</label><input type="text" name="ein" value="{{m.ein or ''}}" placeholder="XX-XXXXXXX"></div>
<div><label>IRS letter (CP 575 / fax reply) – AI reads the EIN</label><input type="file" name="cp575" accept=".pdf,image/*">
{% if m.cp575 %}<div class="hint"><a href="/ein/{{job}}/cp575" target="_blank">View uploaded letter</a></div>{% endif %}</div>
<div><label>Client's email</label><input type="text" name="client_email" value="{{a.client_email}}"></div>
<div><label>Note</label><input type="text" name="note" placeholder="e.g. faxed 10:30, confirmation received"></div>
</div>
<label class="tick"><input type="checkbox" name="email_client"><span>Email the EIN (and the IRS letter) to the client</span></label>
<button class="btn">Update tracker</button></form>
{% if m.history %}<table style="margin-top:14px"><tr><th>When</th><th>Status</th><th>Note</th></tr>
{% for h in m.history %}<tr><td>{{h.t|ukdate}}</td><td>{{STATUS.get(h.status, h.status)}}</td><td>{{h.note}}</td></tr>{% endfor %}</table>{% endif %}
</div>

<div class="card"><details><summary style="cursor:pointer;font-weight:700;color:#b91c1c">Delete this application</summary>
<form method="post" action="/ein/{{job}}/delete" style="margin-top:10px">Type DELETE: <input type="text" name="confirm" style="max-width:160px">
<button class="btn small">Delete</button></form></details></div>

<script>
const F = document.getElementById('ss4');
const PRESET_VALUES = {
 foreign_smllc:{is_llc:'yes',llc_members:'1',llc_us:'yes',entity:'other',entity_other:'Foreign-owned U.S. disregarded entity-Form 5472',reason:'other',reason_other:'Foreign-owned U.S. disregarded entity filing Form 5472',signer_title:'Sole Member'},
 us_smllc:{is_llc:'yes',llc_members:'1',llc_us:'yes',entity:'other',entity_other:'Disregarded entity',reason:'started',reason_other:'',signer_title:'Sole Member'},
 llc_corp:{is_llc:'yes',llc_us:'yes',entity:'corp',corp_form:'1120',reason:'started',entity_other:'',reason_other:'',signer_title:'Managing Member'},
 mmllc:{is_llc:'yes',llc_us:'yes',entity:'partnership',reason:'started',entity_other:'',reason_other:'',signer_title:'Member'},
 ccorp:{is_llc:'no',llc_members:'',entity:'corp',corp_form:'1120',reason:'started',entity_other:'',reason_other:'',signer_title:'President'},
 custom:{}};
function setv(k, v, mark){
  const els = F.querySelectorAll('[name="'+k+'"]'); if(!els.length) return;
  els.forEach(el=>{
    if(el.type==='radio'){ el.checked = (el.value===v); }
    else if(el.type==='checkbox'){ el.checked = !!v; }
    else if(el.tagName==='SELECT' && (k==='rp_name'||k==='signer')){ el.dataset.v = v; }
    else { el.value = v; }
    if(mark && el.type!=='radio' && el.type!=='checkbox'){ el.classList.add('ai'); }
  });
}
function getv(k){ const el = F.querySelector('[name="'+k+'"]:checked') || F.querySelector('[name="'+k+'"]:not([type=radio])'); return el ? (el.type==='checkbox' ? (el.checked?'1':'') : (el.tagName==='SELECT' && el.dataset.v !== undefined ? (el.dataset.v || el.value) : el.value)) : ''; }
function applyPreset(p){
  const v = PRESET_VALUES[p]||{};
  Object.keys(v).forEach(k=>setv(k, v[k]));
  const tin = F.querySelector('[name=rp_tin]');
  if(p==='foreign_smllc' && !tin.value) tin.value='Foreign';
  if(p==='us_smllc' && tin.value.toLowerCase()==='foreign') tin.value='';
  if(p==='mmllc'){ F.querySelector('[name=llc_members]').value = String(Math.max(ownerCount(),2)); }
  if(p==='llc_corp'){ F.querySelector('[name=llc_members]').value = String(Math.max(ownerCount(),1)); }
  showX(); sigPrev();
}
function showX(){
  const e = getv('entity'), r = getv('reason'), act = getv('activity');
  document.querySelectorAll('.x').forEach(el=>el.classList.toggle('hide', !el.classList.contains('x-'+e)));
  document.querySelectorAll('.r').forEach(el=>el.classList.toggle('hide', !el.classList.contains('r-'+r)));
  document.querySelectorAll('.act').forEach(el=>el.classList.toggle('hide', !el.classList.contains('act-'+act)));
  document.getElementById('prior_box').classList.toggle('hide', getv('prior_ein')!=='yes');
}
function memberNames(){ return [...document.querySelectorAll('[name^=m_name_]')].filter(x=>{const k=F.querySelector('[name=m_kind_'+x.name.split('_')[2]+']'); return !k || k.value!=='company';}).map(x=>x.value.trim()).filter(Boolean); }
function ownerCount(){ return [...document.querySelectorAll('[name^=m_name_]')].filter(x=>{const k=F.querySelector('[name=m_kind_'+x.name.split('_')[2]+']'); return x.value.trim() && (!k || k.value!=='controller');}).length; }
function fillSelect(id){
  const s = document.getElementById(id), cur = s.dataset.v || s.value || '';
  const names = memberNames(); if(cur && !names.includes(cur)) names.unshift(cur);
  s.innerHTML = '<option value="">– choose a member –</option>' + names.map(n=>'<option'+(n===cur?' selected':'')+'>'+n.replace(/</g,'&lt;')+'</option>').join('');
  if(cur){ s.value = cur; } s.dataset.v = s.value;
}
function syncMembers(){ fillSelect('rp_name'); fillSelect('signer'); sigPrev(); }
function addRow(){ const h = document.querySelector('tr.mrow.hide'); if(h) h.classList.remove('hide'); }
function sigPrev(){
  const typed = getv('sig_mode')!=='none';
  document.getElementById('sigbox').classList.toggle('hide', !typed);
  document.getElementById('sigprev').textContent = document.getElementById('signer').value || '';
}
function addr(a, c, county){ setv('mail_addr', a); setv('mail_city', c); setv('county_state', county); faxForLine6(); }
const US_ST = /\b(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b/, US_C = /united states|\busa\b/i;
function faxForLine6(){
  const v = (F.querySelector('[name=county_state]')||{}).value || ''; if(!v) return;
  const sel = F.querySelector('[name=fax_to]'); if(!sel) return;
  sel.value = (US_ST.test(v) || US_C.test(v)) ? '{{IRS_FAX.us[0]}}' : '{{IRS_FAX.intl[0]}}';
}
function esc(s){ return String(s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
let LAST = null, SKIP = [];
function useOne(i, b){ const [k,c,v] = SKIP[i]; setv(k, v, true); syncMembers(); showX(); b.disabled = true; }
async function readAI(){
  const b = document.getElementById('readbtn'), msg = document.getElementById('readmsg');
  b.disabled = true; msg.textContent = ' Reading the documents… (up to a minute)';
  try{
    const r = await fetch('/ein/{{job}}/read', {method:'POST'}); const j = await r.json();
    if(!r.ok || j.error) throw new Error(j.error || 'failed');
    LAST = j; useAll(true);
    msg.textContent = ' Done – blue boxes were filled from the documents. Check them, and change anything by hand.';
  }catch(e){ msg.textContent = ' ' + e.message; }
  b.disabled = false;
}
function useAll(onlyEmpty){
  const j = LAST; if(!j) return;
  // members first (signer / responsible party come from them)
  const rows = document.querySelectorAll('tr.mrow');
  if(!onlyEmpty || !memberNames().length){
    (j.members||[]).slice(0,8).forEach((mm,i)=>{ rows[i].classList.remove('hide');
      rows[i].querySelector('[name=m_name_'+i+']').value = mm.name||''; rows[i].querySelector('[name=m_role_'+i+']').value = mm.role||'';
      rows[i].querySelector('[name=m_kind_'+i+']').value = mm.kind||'person';
      rows[i].querySelector('[name=m_country_'+i+']').value = mm.country||''; rows[i].querySelector('[name=m_pct_'+i+']').value = mm.pct||''; });
  }
  if(!onlyEmpty || F.preset.value==='foreign_smllc') { F.preset.value = j.preset; }
  const skipped = [];
  Object.entries(j.values||{}).forEach(([k,v])=>{
    const cur = getv(k);
    if(onlyEmpty && cur && cur!==v && !['is_llc','llc_members','llc_us','entity','entity_other','reason','reason_other','signer_title','preset','emp_ag','emp_house','emp_other','first_wages','prior_ein','fy_close','fax_to'].includes(k)){ skipped.push([k,cur,v]); return; }
    setv(k, v, true);
  });
  syncMembers(); showX(); faxForLine6();
  const box = document.getElementById('sugbox');
  let h = '';
  if(j.notes) h += '<div class="box"><b>AI notes:</b> '+esc(j.notes)+'</div>';
  if(skipped.length){
    h += '<div class="box"><b>These boxes already had something typed in, so they were not changed.</b> Use the AI value?<table class="sug"><tr><th>Field</th><th>Now</th><th>From documents</th><th></th></tr>';
    SKIP = skipped;
    skipped.forEach(([k,c,v],i)=>{ h += '<tr><td>'+esc(k)+'</td><td>'+esc(c)+'</td><td>'+esc(v)+'</td><td><button type="button" class="btn small secondary" onclick="useOne('+i+',this)">Use</button></td></tr>'; });
    h += '</table><button type="button" class="btn small" onclick="useAll(false)">Use all AI values</button></div>';
  }
  box.innerHTML = h; box.classList.toggle('hide', !h);
}
syncMembers(); showX(); sigPrev();
</script>
{% endblock %}"""
