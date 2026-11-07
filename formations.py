"""
UK Formations tab (v41a) – incorporate UK companies and LLPs straight to Companies House by software filing
(XML Gateway, form "CompanyIncorporation" – schema CompanyIncorporation-v3-8, FormSubmission-v2-11).

Self-contained add-on: app.py only imports this file and calls register(app, load_config, send_email).
Nothing in the mail-sorting, Company forms, ITIN or EIN tabs is touched.

  /formations                    -> list / tracker of incorporations + settings
  /formations/new                -> new incorporation
  /formations/<job>              -> the incorporation: company, people (directors / secretary / LLP members / PSCs /
                                    subscribers / guarantors), shares, documents, checks, submit, status, certificate
  /formations/<job>/save (POST)  -> JSON save of the whole form
  /formations/<job>/check        -> JSON: plain-English checks + official schema check of the XML
  /formations/<job>/submit       -> sends the incorporation to Companies House (TEST or LIVE)
  /formations/<job>/poll         -> asks Companies House for the status now
  /formations/<job>/upload       -> articles PDF / name-approval letter / ID documents for the AI read
  /formations/<job>/read         -> JSON: AI reads passports / order form and suggests people + company details
  /formations/<job>/namecheck    -> JSON: similar names on the register (CH REST API, uses CH_API_KEY)

Secrets – NEVER in the code (the GitHub repo is public). Railway → Variables:
  CH_PRESENTER_ID / CH_PRESENTER_CODE            live presenter account (fee-bearing filings need a credit account)
  CH_TEST_PRESENTER_ID / CH_TEST_PRESENTER_CODE  XML Gateway TEST account (if not set, TEST mode uses the live ones)
  CH_GATEWAY_URL                                 optional override (default https://xmlgw.companieshouse.gov.uk/v1-0/xmlgw/Gateway)
  CH_API_KEY                                     already set for the Company forms tab – used for the name check
  FORMATIONS_NO_POLL=1                           switches off the 10-minute background status check

The official XSD files are in ch_schemas.zip (repo root). Every filing is checked against them before it is sent.
"""
from __future__ import annotations

import base64, datetime as dt, hashlib, io, json, os, re, threading, time, urllib.error, urllib.parse, urllib.request, uuid, zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, render_template_string, request, send_file

try:
    from lxml import etree
except Exception:  # pragma: no cover – the tab shows a clear message instead
    etree = None

VERSION = "v41a"
ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "formations"
SETTINGS_FILE = OUT / "settings.json"
SCHEMA_ZIP = ROOT / "ch_schemas.zip"
SCHEMA_DIR = OUT / "schemas"
GATEWAY = os.environ.get("CH_GATEWAY_URL") or "https://xmlgw.companieshouse.gov.uk/v1-0/xmlgw/Gateway"

NS_ENV = "http://www.govtalk.gov.uk/CM/envelope"
NS_HDR = "http://xmlgw.companieshouse.gov.uk/Header"
NS_CH = "http://xmlgw.companieshouse.gov.uk"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
LOC_INC = NS_CH + " http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/CompanyIncorporation-v3-8.xsd"
LOC_FS = NS_HDR + " http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/FormSubmission-v2-11.xsd"
LOC_ENV = NS_ENV + " http://xmlgw.companieshouse.gov.uk/v2-1/schema/Egov_ch-v2-0.xsd"

_LOCK = threading.RLock()
_CFG = {"fn": None}
_SEND = {"fn": None}
_POLL = {"started": False}

bp = Blueprint("formations", __name__)

# ----------------------------------------------------------------------------- defaults

SIU_RO = {"premise": "SIU OFFICE", "street": "4-6", "thoroughfare": "Greatorex Street", "post_town": "London",
          "county": "", "postcode": "E1 5NF", "country": "GB-ENG"}

SETTINGS_DEFAULT = {
    "mode": "test",                 # test | live
    "live_approved": "",            # staff tick: Companies House has approved our software for CompanyIncorporation
    "package_reference": "0012",    # 0012 = Companies House test package reference; use the one CH gives for live
    "next_submission": "1",         # running number for the 6-character SubmissionNumber
    "agent_company": "",            # the firm that holds the presenter account (Authoriser = Agent)
    "agent_forename": "Muhammad",
    "agent_surname": "Usama",
    "agent_premise": SIU_RO["premise"], "agent_street": SIU_RO["street"], "agent_thoroughfare": SIU_RO["thoroughfare"],
    "agent_post_town": SIU_RO["post_town"], "agent_postcode": SIU_RO["postcode"], "agent_country": "GB-ENG",
    "contact_name": "", "contact_number": "",
    "notify_email": "accounting@startitup.global",
    "registered_email_default": "",  # used when a company has no registered email address of its own
}

COMPANY_TYPES = {
    "BYSHR": "Private company limited by shares",
    "BYGUAR": "Private company limited by guarantee",
    "BYGUAREXEMPT": "Private company limited by guarantee – exempt from using 'Limited'",
    "PLC": "Public limited company (PLC)",
    "LLP": "Limited liability partnership (LLP)",
    "LLPDES": "LLP – every member is a designated member",
}
JURISDICTIONS = {"EW": "England and Wales", "WA": "Wales", "SC": "Scotland", "NI": "Northern Ireland"}
ARTICLES = {"model": "Model articles (no document needed)", "amended": "Model articles with amendments (upload PDF)",
            "bespoke": "Bespoke articles (upload PDF)"}
MISMATCH = {"": "– names match ID –", "PREFERRED_NAME": "Preferred name", "LEGALLY_CHANGED": "Name legally changed",
            "TRANSLATION_OR_DIFF_CONV": "Translation / different naming convention",
            "REGISTER_ERROR": "Error on the register", "NOT_SAY": "Prefer not to say"}
NOC_COMPANY = {
    "OWNERSHIPOFSHARES_25TO50PERCENT": "Shares 25–50%", "OWNERSHIPOFSHARES_50TO75PERCENT": "Shares 50–75%",
    "OWNERSHIPOFSHARES_75TO100PERCENT": "Shares 75–100%",
    "VOTINGRIGHTS_25TO50PERCENT": "Votes 25–50%", "VOTINGRIGHTS_50TO75PERCENT": "Votes 50–75%",
    "VOTINGRIGHTS_75TO100PERCENT": "Votes 75–100%",
    "RIGHTTOAPPOINTANDREMOVEDIRECTORS": "Right to appoint/remove directors",
    "SIGINFLUENCECONTROL": "Significant influence or control",
}
NOC_LLP = {
    "RIGHTTOSHARESURPLUSASSETS_25TO50PERCENT": "Surplus assets 25–50%",
    "RIGHTTOSHARESURPLUSASSETS_50TO75PERCENT": "Surplus assets 50–75%",
    "RIGHTTOSHARESURPLUSASSETS_75TO100PERCENT": "Surplus assets 75–100%",
    "VOTINGRIGHTS_25TO50PERCENT": "Votes 25–50%", "VOTINGRIGHTS_50TO75PERCENT": "Votes 50–75%",
    "VOTINGRIGHTS_75TO100PERCENT": "Votes 75–100%",
    "RIGHTTOAPPOINTANDREMOVEMEMBERS": "Right to appoint/remove members",
    "SIGINFLUENCECONTROL": "Significant influence or control",
}
# the schema also allows _AS_TRUST and _AS_FIRM variants of every nature of control
for _d in (NOC_COMPANY, NOC_LLP):
    for _k, _v in list(_d.items()):
        _d[_k + "_AS_TRUST"] = _v + " (as trustee)"
        _d[_k + "_AS_FIRM"] = _v + " (as member of a firm)"

DEFAULT_PARTICULARS = ("Each share carries one vote at general meetings, equal rights to dividends and to participate in a "
                       "distribution on winding up. The shares are not redeemable.")

STATUS = {"draft": "Draft", "submitted": "Sent – waiting", "pending": "With Companies House", "parked": "Parked by CH",
          "accepted": "INCORPORATED", "rejected": "Rejected", "error": "Not sent – error"}

# Countries the schema accepts as codes; anything else goes in <OtherForeignCountry>.
CODE_COUNTRIES = {"USA", "IRL", "DEU", "FRA", "ITA", "ESP", "PRT", "NLD", "POL", "BEL", "NOR", "SWE", "DNK", "AUS", "NZL",
                  "CAN", "ZAF", "AUT", "HRV", "CYP", "CZE", "EST", "HUN", "GRC", "LTU", "GBR", "GB-ENG", "GB-WLS",
                  "GB-SCT", "GB-NIR"}
COUNTRY_NAMES = {
    "united kingdom": "GBR", "uk": "GBR", "great britain": "GBR", "britain": "GBR", "gb": "GBR",
    "england": "GB-ENG", "wales": "GB-WLS", "scotland": "GB-SCT", "northern ireland": "GB-NIR",
    "united states": "USA", "united states of america": "USA", "us": "USA", "america": "USA",
    "ireland": "IRL", "republic of ireland": "IRL", "germany": "DEU", "france": "FRA", "italy": "ITA", "spain": "ESP",
    "portugal": "PRT", "netherlands": "NLD", "holland": "NLD", "poland": "POL", "belgium": "BEL", "norway": "NOR",
    "sweden": "SWE", "denmark": "DNK", "australia": "AUS", "new zealand": "NZL", "canada": "CAN",
    "south africa": "ZAF", "austria": "AUT", "croatia": "HRV", "cyprus": "CYP", "czech republic": "CZE",
    "czechia": "CZE", "estonia": "EST", "hungary": "HUN", "greece": "GRC", "lithuania": "LTU",
}
RO_COUNTRY_FOR = {"EW": {"GB-ENG", "GB-WLS", "GBR"}, "WA": {"GB-WLS", "GBR"}, "SC": {"GB-SCT", "GBR"},
                  "NI": {"GB-NIR", "GBR"}}


def country_code(v: str) -> tuple[str, str]:
    """-> ("code", value) for a schema country code, or ("other", name) for <OtherForeignCountry>."""
    s = (v or "").strip()
    if s.upper() in CODE_COUNTRIES:
        return "code", s.upper()
    c = COUNTRY_NAMES.get(s.lower().rstrip("."))
    if c:
        return "code", c
    return "other", s


# ----------------------------------------------------------------------------- storage

def _cfg() -> dict:
    try:
        return _CFG["fn"]() if _CFG["fn"] else {}
    except Exception:
        return {}


def load_settings() -> dict:
    s = dict(SETTINGS_DEFAULT)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except Exception:
        pass
    return s


def save_settings(s: dict):
    OUT.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def creds(mode: str) -> tuple[str, str]:
    if mode == "test":
        tid, tcode = os.environ.get("CH_TEST_PRESENTER_ID", "").strip(), os.environ.get("CH_TEST_PRESENTER_CODE", "").strip()
        if tid and tcode:
            return tid, tcode
    return os.environ.get("CH_PRESENTER_ID", "").strip(), os.environ.get("CH_PRESENTER_CODE", "").strip()


def job_dir(job: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job or ""):
        abort(404)
    return OUT / job


def load_job(job: str) -> dict:
    p = job_dir(job) / "meta.json"
    if not p.exists():
        abort(404)
    return json.loads(p.read_text())


def save_job(m: dict):
    d = OUT / m["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(m, indent=2))
    tmp.replace(d / "meta.json")


def list_jobs() -> list[dict]:
    out = []
    if OUT.exists():
        for p in OUT.glob("*/meta.json"):
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                pass
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def log(m: dict, text: str):
    m.setdefault("log", []).append({"t": dt.datetime.now().isoformat(timespec="seconds"), "text": text})


def new_person() -> dict:
    return {"id": uuid.uuid4().hex[:8], "kind": "person", "title": "", "forename": "", "other_forenames": "",
            "surname": "", "dob": "", "nationality": "", "residence_country": "United Kingdom",
            "service_same_ro": True, "service": {}, "res_same_service": False, "res": {},
            "personal_code": "", "name_mismatch": "", "former_names": "",
            "corp_name": "", "corp_addr": {}, "reg_uk": True, "reg_no": "", "place_registered": "", "law": "",
            "legal_form": "", "country_or_state": "",
            "roles": {"director": False, "secretary": False, "member": False, "designated": False, "psc": False,
                      "subscriber": False, "guarantor": False},
            "consent": False, "psc_consent": False, "noc": [], "shares": [], "guarantee": "1"}


def new_job() -> dict:
    s = load_settings()
    return {"id": uuid.uuid4().hex[:12], "created": dt.datetime.now().isoformat(timespec="seconds"),
            "status": "draft", "mode": "", "submission_number": "", "company_number": "", "log": [],
            "docs": {}, "uploads": [], "cert": "",
            "a": {"company_name": "", "company_type": "BYSHR", "cic": False, "jurisdiction": "EW", "ro": dict(SIU_RO),
                  "articles": "model", "restricted_articles": False, "same_day": False, "same_name": False,
                  "name_authorisation": False, "reject_reference": "", "sic": [""],
                  "registered_email": s.get("registered_email_default", ""), "client_email": "", "client_name": "",
                  "no_psc": False, "share_classes": [{"cls": "ORDINARY", "currency": "GBP", "nominal": "1",
                                                       "particulars": DEFAULT_PARTICULARS}],
                  "people": [new_person()]}}


# ----------------------------------------------------------------------------- helpers

def dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else default).replace(",", "").replace("£", "").strip())
    except InvalidOperation:
        return Decimal("NaN")


def fmt_dec(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s if s not in ("-0",) else "0"


def is_llp(a: dict) -> bool:
    return a.get("company_type") in ("LLP", "LLPDES")


def has_shares(a: dict) -> bool:
    return a.get("company_type") in ("BYSHR", "PLC")


def is_guar(a: dict) -> bool:
    return a.get("company_type") in ("BYGUAR", "BYGUAREXEMPT")


def pname(p: dict) -> str:
    if p.get("kind") == "corporate":
        return p.get("corp_name") or "(company)"
    if p.get("kind") == "legal":
        return p.get("corp_name") or "(legal person)"
    return " ".join(x for x in (p.get("forename"), p.get("other_forenames"), p.get("surname")) if x) or "(person)"


def norm_name(n: str) -> str:
    n = (n or "").upper().replace("&", " AND ").replace("+", " AND ")
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    n = re.sub(r"\b(LIMITED|LTD|PLC|LLP|PUBLIC LIMITED COMPANY|LIMITED LIABILITY PARTNERSHIP|COMMUNITY INTEREST COMPANY|"
               r"CIC|CYF|CYFYNGEDIG|THE|COMPANY|CO|UK|SERVICES?|GROUP|HOLDINGS?)\b", " ", n)
    return re.sub(r"\s+", "", n)


def share_rows(a: dict) -> tuple[list, dict]:
    """Allotments per subscriber and the statement of capital computed from them."""
    classes = {(c.get("cls") or "").strip().upper(): c for c in a.get("share_classes", []) if (c.get("cls") or "").strip()}
    rows = []
    for p in a.get("people", []):
        if not p.get("roles", {}).get("subscriber"):
            continue
        for sh in p.get("shares", []):
            cls = (sh.get("cls") or "").strip().upper()
            c = classes.get(cls, {})
            nominal = dec(c.get("nominal"), "1")
            paid = dec(sh.get("paid"), str(nominal)) if sh.get("paid") not in (None, "") else nominal
            unpaid = dec(sh.get("unpaid"), "0")
            rows.append({"person": p, "cls": cls, "num": int(dec(sh.get("num"), "0")) if dec(sh.get("num"), "0").is_finite() else 0,
                         "nominal": nominal, "paid": paid, "unpaid": unpaid,
                         "currency": (c.get("currency") or "GBP").upper(), "particulars": c.get("particulars") or ""})
    cap = {}
    for r in rows:
        cur = cap.setdefault(r["currency"], {"unpaid": Decimal(0), "num": 0, "agg": Decimal(0), "classes": {}})
        cur["unpaid"] += r["unpaid"] * r["num"]
        cur["num"] += r["num"]
        cur["agg"] += r["nominal"] * r["num"]
        k = cur["classes"].setdefault(r["cls"], {"num": 0, "agg": Decimal(0), "particulars": r["particulars"]})
        k["num"] += r["num"]
        k["agg"] += r["nominal"] * r["num"]
    return rows, cap


# ----------------------------------------------------------------------------- plain-English checks

CHARS_OK = re.compile(r"^[-,.:; 0-9A-Za-z&@$£¥€'\"«»‘’“”?!/\\()\[\]{}<>*=#%+À-ÖØ-öø-ſƒǺ-ǿẀ-ẅỲỳ]*$")


def precheck(m: dict, s: dict) -> tuple[list[str], list[str]]:
    a = m["a"]
    errs, warns = [], []
    ct, name = a.get("company_type"), (a.get("company_name") or "").strip()
    up = name.upper()
    if not name:
        errs.append("Company name is empty.")
    elif not CHARS_OK.match(name):
        errs.append("Company name has characters Companies House does not accept.")
    if name:
        if ct in ("BYSHR", "BYGUAR") and not a.get("cic") and not re.search(r"\b(LIMITED|LTD\.?|CYFYNGEDIG|CYF\.?)$", up):
            errs.append("A private limited company name must end with LIMITED or LTD.")
        if ct == "PLC" and not re.search(r"\b(PUBLIC LIMITED COMPANY|PLC\.?|CWMNI CYHOEDDUS CYFYNGEDIG|CCC\.?)$", up):
            errs.append("A PLC name must end with PLC or PUBLIC LIMITED COMPANY.")
        if ct in ("LLP", "LLPDES") and not re.search(r"\b(LLP|LIMITED LIABILITY PARTNERSHIP|PAC|PARTNERIAETH ATEBOLRWYDD CYFYNGEDIG)$", up):
            errs.append("An LLP name must end with LLP or LIMITED LIABILITY PARTNERSHIP.")
        if a.get("cic") and not re.search(r"\b(CIC|COMMUNITY INTEREST COMPANY|CBC|CWMNI BUDDIANT CYMUNEDOL)$", up):
            errs.append("A CIC name must end with CIC or COMMUNITY INTEREST COMPANY.")
    ro = a.get("ro") or {}
    for k, label in (("premise", "building name/number"), ("post_town", "town"), ("postcode", "postcode")):
        if not (ro.get(k) or "").strip():
            errs.append(f"Registered office: {label} is missing.")
    if (ro.get("country") or "") not in RO_COUNTRY_FOR.get(a.get("jurisdiction"), set()):
        errs.append(f"Registered office country does not match the jurisdiction ({JURISDICTIONS.get(a.get('jurisdiction'))}).")
    if not (a.get("registered_email") or "").strip() or "@" not in a.get("registered_email", ""):
        errs.append("Registered email address is missing (required for every new company/LLP).")
    if not is_llp(a):
        sic = [x.strip() for x in a.get("sic", []) if x and x.strip()]
        if not sic:
            errs.append("Add at least one SIC code.")
        for x in sic:
            if not re.fullmatch(r"\d{5}", x):
                errs.append(f"SIC code '{x}' must be 5 digits.")
        if len(sic) > 4:
            errs.append("Maximum 4 SIC codes.")
        if a.get("articles") in ("amended", "bespoke") and not m.get("docs", {}).get("ARTS"):
            errs.append("Upload the articles PDF (amended or bespoke articles were chosen).")
    if a.get("name_authorisation") and not m.get("docs", {}).get("SUPPNAMEAUTH"):
        errs.append("Upload the name approval letter (sensitive word approval was ticked).")
    if a.get("same_name") and not m.get("docs", {}).get("SUPPEXISTNAME"):
        errs.append("Upload the 'same as existing name' consent letter.")
    if a.get("cic") and not m.get("docs", {}).get("CIC36"):
        errs.append("Upload the CIC36 form (community interest company).")
    rr = (a.get("reject_reference") or "").strip()
    if rr and not (8 <= len(rr) <= 9):
        errs.append("Reject reference must be 8 or 9 characters.")

    people = a.get("people", [])
    dirs = [p for p in people if p["roles"].get("director") and not is_llp(a)]
    secs = [p for p in people if p["roles"].get("secretary") and not is_llp(a)]
    mems = [p for p in people if p["roles"].get("member") and is_llp(a)]
    pscs = [p for p in people if p["roles"].get("psc")]
    subs = [p for p in people if p["roles"].get("subscriber") and has_shares(a)]
    guars = [p for p in people if p["roles"].get("guarantor") and is_guar(a)]
    if is_llp(a):
        if len(mems) < 2:
            errs.append("An LLP needs at least 2 members.")
        if a.get("company_type") == "LLP" and sum(1 for p in mems if p["roles"].get("designated")) < 2:
            errs.append("An LLP needs at least 2 designated members (or choose 'every member is designated').")
    else:
        if not dirs:
            errs.append("Add at least one director.")
        if not any(p.get("kind") == "person" for p in dirs):
            errs.append("At least one director must be an individual (not a company).")
        if ct == "PLC" and len(dirs) < 2:
            errs.append("A PLC needs at least 2 directors.")
        if ct == "PLC" and not secs:
            errs.append("A PLC must have a company secretary.")
    if has_shares(a) and not subs:
        errs.append("Add at least one subscriber (shareholder) with shares.")
    if is_guar(a) and not guars:
        errs.append("Add at least one guarantor (member) with the amount guaranteed.")
    if not pscs and not a.get("no_psc"):
        errs.append("Add the PSCs, or tick 'There is no person with significant control'.")
    if pscs and a.get("no_psc"):
        errs.append("PSCs are listed but 'no PSC' is also ticked – choose one.")

    today = dt.date.today()
    for p in people:
        r = p.get("roles", {})
        active = [k for k in ("director", "secretary", "member", "psc", "subscriber", "guarantor") if r.get(k)]
        if not active:
            continue
        who = pname(p)
        if p.get("kind") == "person":
            if not (p.get("surname") or "").strip():
                errs.append(f"{who}: surname is missing.")
            for f in ("forename", "other_forenames", "surname", "nationality"):
                if p.get(f) and not CHARS_OK.match(p[f]):
                    errs.append(f"{who}: '{p[f]}' has characters Companies House does not accept.")
            needs_dob = r.get("director") and not is_llp(a) or r.get("member") and is_llp(a) or r.get("psc")
            if needs_dob:
                try:
                    dob = dt.date.fromisoformat(p.get("dob") or "")
                    age = (today - dob).days / 365.25
                    if (r.get("director") or r.get("member")) and age < 16:
                        errs.append(f"{who}: directors/LLP members must be at least 16.")
                except ValueError:
                    errs.append(f"{who}: date of birth is missing or not a date.")
                if not (p.get("residence_country") or "").strip():
                    errs.append(f"{who}: country of residence is missing.")
            if (r.get("director") and not is_llp(a) or r.get("psc")) and not (p.get("nationality") or "").strip():
                errs.append(f"{who}: nationality is missing.")
            code = re.sub(r"[\s-]", "", p.get("personal_code") or "")
            if (r.get("director") and not is_llp(a)) or (r.get("member") and is_llp(a)):
                if len(code) != 11:
                    errs.append(f"{who}: Companies House personal code must be 11 characters (identity verification "
                                f"is required for every director / LLP member).")
            elif r.get("psc") and code and len(code) != 11:
                errs.append(f"{who}: PSC personal code must be 11 characters (or leave it blank – the PSC then has "
                            f"14 days after incorporation to verify).")
            elif r.get("psc") and not code:
                warns.append(f"{who}: no personal code for this PSC – they must verify within 14 days of incorporation.")
            for key, label in (("service", "service address"), ("res", "home address")):
                if key == "service" and p.get("service_same_ro"):
                    continue
                if key == "res" and (p.get("res_same_service") or not needs_dob):
                    continue
                ad = p.get(key) or {}
                if not (ad.get("premise") and ad.get("post_town") and ad.get("country")):
                    errs.append(f"{who}: {label} needs building, town and country.")
        else:
            if not (p.get("corp_name") or "").strip():
                errs.append("A corporate person has no name.")
            ad = p.get("corp_addr") or {}
            if not (ad.get("premise") and ad.get("post_town") and ad.get("country")):
                errs.append(f"{who}: registered address needs building, town and country.")
            if p.get("kind") == "corporate" and (r.get("director") or r.get("secretary") or r.get("member")):
                if p.get("reg_uk") and not p.get("reg_no"):
                    errs.append(f"{who}: UK registration number is missing.")
                if not p.get("reg_uk") and not (p.get("law") and p.get("legal_form")):
                    errs.append(f"{who}: governing law and legal form are needed for a non-UK company.")
            if r.get("psc") and not (p.get("law") and p.get("legal_form")):
                errs.append(f"{who}: governing law and legal form are needed for a corporate PSC.")
        if (r.get("director") or r.get("secretary") or r.get("member")) and not p.get("consent"):
            errs.append(f"{who}: tick that they have consented to act.")
        if r.get("psc"):
            if not p.get("noc"):
                errs.append(f"{who}: choose at least one nature of control.")
            if p.get("kind") == "person" and not p.get("psc_consent"):
                errs.append(f"{who}: tick that the PSC has confirmed their details (PSC consent statement).")
        if r.get("subscriber") and has_shares(a):
            if not p.get("shares"):
                errs.append(f"{who}: subscriber has no shares.")
        if r.get("guarantor") and is_guar(a) and not (p.get("guarantee") or "").strip():
            errs.append(f"{who}: amount guaranteed is missing.")

    if has_shares(a):
        rows, cap = share_rows(a)
        names = {(c.get("cls") or "").strip().upper() for c in a.get("share_classes", [])}
        for c in a.get("share_classes", []):
            if not (c.get("particulars") or "").strip():
                errs.append(f"Share class {c.get('cls')}: prescribed particulars (rights) are missing.")
            nv = dec(c.get("nominal"))
            if not nv.is_finite() or nv <= 0:
                errs.append(f"Share class {c.get('cls')}: nominal value per share must be above 0.")
        for r_ in rows:
            if r_["cls"] not in names:
                errs.append(f"{pname(r_['person'])}: share class '{r_['cls']}' is not in the share classes list.")
            if r_["num"] <= 0:
                errs.append(f"{pname(r_['person'])}: number of shares must be at least 1.")
            if not (r_["paid"].is_finite() and r_["unpaid"].is_finite()) or r_["paid"] < 0 or r_["unpaid"] < 0:
                errs.append(f"{pname(r_['person'])}: paid / unpaid amounts are not valid numbers.")
            elif r_["paid"] + r_["unpaid"] < r_["nominal"]:
                warns.append(f"{pname(r_['person'])}: paid + unpaid per share is less than the nominal value.")
        if ct == "PLC":
            gbp = cap.get("GBP", {}).get("agg", Decimal(0))
            if gbp < 50000:
                errs.append("A PLC needs allotted share capital of at least £50,000 nominal.")

    if not (s.get("agent_company") or "").strip():
        errs.append("Settings: enter the agent firm name (the firm that holds the presenter account).")
    if a.get("same_day"):
        warns.append("Same-day incorporation costs more (£156 instead of £100) – must reach Companies House by 3pm.")
    return errs, warns


# ----------------------------------------------------------------------------- XML building (CompanyIncorporation-v3-8)

def E(parent, tag, text=None, ns=NS_CH):
    el = etree.SubElement(parent, f"{{{ns}}}{tag}")
    if text is not None:
        el.text = str(text)
    return el


def _addr(parent, tag, ad: dict, kind="base", ns=NS_CH):
    """kind: 'ro' (ROAddressType), 'company' (CompanyAddressType), 'base' (AddressBaseType)."""
    x = E(parent, tag, ns=ns)
    E(x, "Premise", (ad.get("premise") or "").strip(), ns)
    for k, t in (("street", "Street"), ("thoroughfare", "Thoroughfare")):
        if (ad.get(k) or "").strip():
            E(x, t, ad[k].strip(), ns)
    E(x, "PostTown", (ad.get("post_town") or "").strip(), ns)
    if (ad.get("county") or "").strip():
        E(x, "County", ad["county"].strip(), ns)
    if kind == "ro":
        E(x, "Country", ad.get("country") or "GB-ENG", ns)
        E(x, "Postcode", (ad.get("postcode") or "").strip().upper(), ns)
        return x
    how, val = country_code(ad.get("country"))
    E(x, "Country" if how == "code" else "OtherForeignCountry", val, ns)
    pc = (ad.get("postcode") or "").strip()
    if pc:
        E(x, "Postcode", pc.upper() if how == "code" and val.startswith("GB") else pc, ns)
    return x


def _names(x, p, with_title=True):
    if with_title and (p.get("title") or "").strip():
        E(x, "Title", p["title"].strip())
    if (p.get("forename") or "").strip():
        E(x, "Forename", p["forename"].strip())
    if (p.get("other_forenames") or "").strip():
        E(x, "OtherForenames", p["other_forenames"].strip())
    E(x, "Surname", (p.get("surname") or "").strip())


def _service(x, p):
    sa = E(x, "ServiceAddress")
    if p.get("service_same_ro"):
        E(sa, "SameAsRegisteredOffice", "true")
    else:
        _addr(sa, "Address", p.get("service") or {}, "company")


def _residential(x, p, a=None):
    ra = E(x, "ResidentialAddress")
    if p.get("res_same_service") and not p.get("service_same_ro"):
        E(ra, "SameAsServiceAddress", "true")
    elif p.get("res_same_service") and a is not None:   # lives at the registered office address
        _addr(ra, "Address", _person_address_for_subscriber(a, p), "base")
    else:
        _addr(ra, "Address", p.get("res") or {}, "base")


def _verification(x, p):
    v = E(x, "VerificationDetails")
    E(v, "CompaniesHousePersonalCode", re.sub(r"[\s-]", "", p.get("personal_code") or "").upper())
    st = E(v, "VerificationStatements")
    E(st, "VerificationStatementForIndividual", "INDIVIDUAL_VERIFIED")
    if p.get("name_mismatch"):
        E(v, "NameMismatchReason", p["name_mismatch"])


def _previous(x, p):
    for line in (p.get("former_names") or "").split(";"):
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit(" ", 1)
        pn = E(x, "PreviousNames")
        if len(parts) == 2:
            E(pn, "Forename", parts[0])
            E(pn, "Surname", parts[1])
        else:
            E(pn, "Surname", parts[0])


def _corp_officer(x, p):
    c = E(x, "Corporate")
    E(c, "CorporateName", p.get("corp_name", "").strip())
    _addr(c, "Address", p.get("corp_addr") or {}, "company")
    ci = E(c, "CompanyIdentification")
    if p.get("reg_uk"):
        E(E(ci, "UK"), "RegistrationNumber", (p.get("reg_no") or "").strip())
    else:
        nu = E(ci, "NonUK")
        if (p.get("place_registered") or "").strip():
            E(nu, "PlaceRegistered", p["place_registered"].strip())
        if (p.get("reg_no") or "").strip():
            E(nu, "RegistrationNumber", p["reg_no"].strip())
        E(nu, "LawGoverned", (p.get("law") or "").strip())
        E(nu, "LegalForm", (p.get("legal_form") or "").strip())


def _person_address_for_subscriber(a, p) -> dict:
    if p.get("kind") != "person":
        return p.get("corp_addr") or {}
    if p.get("service_same_ro"):
        ro = dict(a.get("ro") or {})
        ro["country"] = {"GB-ENG": "GB-ENG", "GB-WLS": "GB-WLS", "GB-SCT": "GB-SCT", "GB-NIR": "GB-NIR"}.get(ro.get("country"), "GBR")
        return ro
    return p.get("service") or {}


def _member_name(x, p):
    if p.get("kind") == "person":
        pe = E(x, "Person")
        fn = " ".join(v.strip() for v in (p.get("forename"), p.get("other_forenames")) if v and v.strip())
        if fn:
            E(pe, "Forename", fn[:50])
        E(pe, "Surname", (p.get("surname") or "").strip())
    else:
        co = E(x, "Corporate")
        E(co, "CorporateName", (p.get("corp_name") or "").strip())


def build_incorporation(m: dict, s: dict):
    a = m["a"]
    ct = a["company_type"]
    root = etree.Element(f"{{{NS_CH}}}CompanyIncorporation", nsmap={None: NS_CH})
    root.set(f"{{{NS_XSI}}}schemaLocation", LOC_INC)
    E(root, "CompanyType", ct)
    if a.get("cic") and not is_llp(a):
        E(root, "Cic", "true")
    E(root, "CountryOfIncorporation", a.get("jurisdiction") or "EW")
    _addr(root, "RegisteredOfficeAddress", a.get("ro") or {}, "ro")
    E(root, "DataMemorandum", "false" if is_llp(a) else "true")
    if not is_llp(a):
        fam = {"BYSHR": "BYSHR", "PLC": "PLC", "BYGUAR": "BYGUAR", "BYGUAREXEMPT": "BYGUAR"}[ct]
        art = a.get("articles") or "model"
        if art == "bespoke":
            code = "BESPOKE"
        elif art == "amended":
            code = {"BYSHR": "BYSHAREAMEND", "PLC": "PLCAMEND", "BYGUAR": "BYGUARAMEND"}[fam]
        else:
            code = fam + "MODEL"
        E(root, "Articles", code)
        E(root, "RestrictedArticles", "true" if a.get("restricted_articles") else "false")

    people = a.get("people", [])
    for p in people:
        r = p.get("roles", {})
        roles = []
        if is_llp(a):
            if r.get("member"):
                roles.append("Member")
        else:
            if r.get("director"):
                roles.append("Director")
            if r.get("secretary"):
                roles.append("Secretary")
        for role in roles:
            ap = E(root, "Appointment")
            E(ap, "ConsentToAct", "true" if p.get("consent") else "false")
            ro_ = E(ap, role)
            if role == "Member":
                E(ro_, "DesignatedInd", "true" if (r.get("designated") or ct == "LLPDES") else "false")
            if p.get("kind") == "corporate":
                _corp_officer(ro_, p)
                continue
            pe = E(ro_, "Person")
            _names(pe, p)
            _service(pe, p)
            if role == "Secretary":
                _previous(pe, p)
                continue
            E(pe, "DOB", p.get("dob"))
            if role == "Director":
                E(pe, "Nationality", (p.get("nationality") or "").strip())
                if (p.get("residence_country") or "").strip():
                    E(pe, "CountryOfResidence", p["residence_country"].strip())
                _previous(pe, p)
            else:  # LLP member
                E(pe, "CountryOfResidence", (p.get("residence_country") or "").strip())
                _previous(pe, p)
            _residential(pe, p, a)
            _verification(pe, p)

    pscs = E(root, "PSCs")
    psc_people = [p for p in people if p.get("roles", {}).get("psc")]
    if a.get("no_psc") or not psc_people:
        E(pscs, "NoPSCStatement", "NO_INDIVIDUAL_OR_ENTITY_WITH_SIGNFICANT_CONTROL")
    else:
        valid = NOC_LLP if is_llp(a) else NOC_COMPANY
        for p in psc_people:
            n = E(E(pscs, "PSC"), "PSCNotification")
            if p.get("kind") == "person":
                ind = E(n, "Individual")
                _names(ind, p)
                _service(ind, p)
                E(ind, "DOB", p.get("dob"))
                E(ind, "Nationality", (p.get("nationality") or "").strip())
                E(ind, "CountryOfResidence", (p.get("residence_country") or "").strip())
                _residential(ind, p, a)
                if re.sub(r"[\s-]", "", p.get("personal_code") or ""):
                    _verification(ind, p)
                E(ind, "ConsentStatement", "true" if p.get("psc_consent") else "false")
            elif p.get("kind") == "corporate":
                co = E(n, "Corporate")
                E(co, "CorporateName", (p.get("corp_name") or "").strip())
                _addr(co, "Address", p.get("corp_addr") or {}, "company")
                ci = E(co, "PSCCompanyIdentification")
                if (p.get("place_registered") or "").strip():
                    E(ci, "PSCPlaceRegistered", p["place_registered"].strip())
                if (p.get("reg_no") or "").strip():
                    E(ci, "PSCRegistrationNumber", p["reg_no"].strip())
                E(ci, "LawGoverned", (p.get("law") or "").strip())
                E(ci, "LegalForm", (p.get("legal_form") or "").strip())
                if (p.get("country_or_state") or "").strip():
                    E(ci, "CountryOrState", p["country_or_state"].strip())
            else:
                lp = E(n, "LegalPerson")
                E(lp, "LegalPersonName", (p.get("corp_name") or "").strip())
                _addr(lp, "Address", p.get("corp_addr") or {}, "company")
                li = E(lp, "LegalPersonIdentification")
                E(li, "LawGoverned", (p.get("law") or "").strip())
                E(li, "LegalForm", (p.get("legal_form") or "").strip())
            nc = E(n, "LLPNatureOfControls" if is_llp(a) else "NatureOfControls")
            for k in p.get("noc", []):
                if k in valid:
                    E(nc, "NatureOfControl", k)

    if has_shares(a):
        rows, cap = share_rows(a)
        soc = E(root, "StatementOfCapital")
        for cur, c in cap.items():
            cx = E(soc, "Capital")
            E(cx, "TotalAmountUnpaid", fmt_dec(c["unpaid"]))
            E(cx, "TotalNumberOfIssuedShares", c["num"])
            E(cx, "ShareCurrency", cur)
            E(cx, "TotalAggregateNominalValue", fmt_dec(c["agg"]))
            for cls, k in c["classes"].items():
                sx = E(cx, "Shares")
                E(sx, "ShareClass", cls)
                E(sx, "PrescribedParticulars", k["particulars"].strip())
                E(sx, "NumShares", k["num"])
                E(sx, "AggregateNominalValue", fmt_dec(k["agg"]))
        for p in people:
            if not p.get("roles", {}).get("subscriber"):
                continue
            sb = E(root, "Subscribers")
            _member_name(sb, p)
            _addr(sb, "Address", _person_address_for_subscriber(a, p), "base")
            E(E(sb, "Authentication"), "MemorandumPersonalAuthentication", "SUBSCRIBER_AGREES_NAME_USED_TO_AUTHENTICATE")
            for r_ in rows:
                if r_["person"] is not p:
                    continue
                sx = E(sb, "Shares")
                E(sx, "ShareClass", r_["cls"])
                E(sx, "NumShares", r_["num"])
                E(sx, "AmountPaidDuePerShare", fmt_dec(r_["paid"]))
                E(sx, "AmountUnpaidPerShare", fmt_dec(r_["unpaid"]))
                E(sx, "ShareCurrency", r_["currency"])
                E(sx, "ShareValue", fmt_dec(r_["nominal"]))
            E(sb, "MemorandumStatement", "Each subscriber to this memorandum of association wishes to form a company "
              "under the Companies Act 2006 and agrees to become a member of the company and to take at least one share.")
    if is_guar(a):
        for p in people:
            if not p.get("roles", {}).get("guarantor"):
                continue
            g = E(root, "Guarantors")
            _member_name(g, p)
            _addr(g, "Address", _person_address_for_subscriber(a, p), "base")
            E(E(g, "Authentication"), "MemorandumPersonalAuthentication", "GUARANTOR_AGREES_NAME_USED_TO_AUTHENTICATE")
            E(g, "AmountGuaranteed", (p.get("guarantee") or "1").strip())
            E(g, "MemorandumStatement", "Each subscriber to this memorandum of association wishes to form a company "
              "under the Companies Act 2006 and agrees to become a member of the company.")

    au = E(E(root, "Authoriser"), "Agent")
    co = E(au, "Corporate")
    if s.get("agent_forename") and s.get("agent_surname"):
        E(co, "Forename", s["agent_forename"].strip())
        E(co, "Surname", s["agent_surname"].strip())
    E(co, "CorporateName", (s.get("agent_company") or "").strip())
    E(E(au, "Authentication"), "AuthoriserPersonalAuthentication", "AUTHORISER_AGREES_NAME_USED_TO_AUTHENTICATE")
    _addr(au, "Address", {"premise": s.get("agent_premise"), "street": s.get("agent_street"),
                          "thoroughfare": s.get("agent_thoroughfare"), "post_town": s.get("agent_post_town"),
                          "postcode": s.get("agent_postcode"), "country": s.get("agent_country") or "GB-ENG"}, "base")
    E(root, "SameDay", "true" if a.get("same_day") else "false")
    if a.get("same_name"):
        E(root, "SameName", "true")
    if a.get("name_authorisation"):
        E(root, "NameAuthorisation", "true")
    if (a.get("reject_reference") or "").strip():
        E(root, "RejectReference", a["reject_reference"].strip())
    if not is_llp(a):
        sic = [x.strip() for x in a.get("sic", []) if x and x.strip()]
        if sic:
            sx = E(root, "SICCodes")
            for c in sic[:4]:
                E(sx, "SICCode", c)
    E(root, "RegisteredEmailAddress", (a.get("registered_email") or "").strip())
    if not is_llp(a):
        E(root, "AcceptLawfulPurposeStatement", "true")
    return root


DOC_LABEL = {"ARTS": "Articles of association (PDF)", "SUPPNAMEAUTH": "Name approval letter (sensitive words)",
             "SUPPEXISTNAME": "Consent to use the same name", "CIC36": "CIC36 form"}


def build_form_submission(m: dict, s: dict, subno: str):
    a = m["a"]
    fs = etree.Element(f"{{{NS_HDR}}}FormSubmission", nsmap={None: NS_HDR, "xsi": NS_XSI})
    fs.set(f"{{{NS_XSI}}}schemaLocation", LOC_FS)
    h = E(fs, "FormHeader", ns=NS_HDR)
    E(h, "CompanyName", re.sub(r"\s+", " ", a.get("company_name", "").strip()).upper(), NS_HDR)
    E(h, "PackageReference", (s.get("package_reference") or "0012").strip(), NS_HDR)
    E(h, "FormIdentifier", "CompanyIncorporation", NS_HDR)
    E(h, "SubmissionNumber", subno, NS_HDR)
    if s.get("contact_name") and s.get("contact_number"):
        E(h, "ContactName", s["contact_name"].strip(), NS_HDR)
        E(h, "ContactNumber", s["contact_number"].strip()[:25], NS_HDR)
    E(h, "CustomerReference", m["id"], NS_HDR)
    E(fs, "DateSigned", dt.date.today().isoformat(), NS_HDR)
    E(fs, "Form", ns=NS_HDR).append(build_incorporation(m, s))
    d = job_dir(m["id"]) / "docs"
    for cat in ("ARTS", "SUPPNAMEAUTH", "SUPPEXISTNAME", "CIC36"):
        fn = m.get("docs", {}).get(cat)
        if not fn:
            continue
        if cat == "ARTS" and (is_llp(a) or a.get("articles") == "model"):
            continue
        if cat == "SUPPNAMEAUTH" and not a.get("name_authorisation"):
            continue
        if cat == "SUPPEXISTNAME" and not a.get("same_name"):
            continue
        if cat == "CIC36" and not a.get("cic"):
            continue
        doc = E(fs, "Document", ns=NS_HDR)
        E(doc, "Data", base64.b64encode((d / fn).read_bytes()).decode(), NS_HDR)
        E(doc, "Date", dt.date.today().isoformat(), NS_HDR)
        E(doc, "Filename", (cat.lower() + ".pdf")[:32], NS_HDR)
        E(doc, "ContentType", "application/pdf", NS_HDR)
        E(doc, "Category", cat, NS_HDR)
    return fs


def md5(v: str) -> str:
    return hashlib.md5(v.encode("utf-8")).hexdigest().lower()


def envelope(cls: str, body_el, mode: str) -> bytes:
    pid, code = creds(mode)
    g = etree.Element(f"{{{NS_ENV}}}GovTalkMessage", nsmap={None: NS_ENV, "xsi": NS_XSI})
    g.set(f"{{{NS_XSI}}}schemaLocation", LOC_ENV)
    E(g, "EnvelopeVersion", "1.0", NS_ENV)
    h = E(g, "Header", ns=NS_ENV)
    md_ = E(h, "MessageDetails", ns=NS_ENV)
    E(md_, "Class", cls, NS_ENV)
    E(md_, "Qualifier", "request", NS_ENV)
    E(md_, "TransactionID", uuid.uuid4().hex.upper(), NS_ENV)
    if mode == "test":
        E(md_, "GatewayTest", "1", NS_ENV)
    ida = E(E(h, "SenderDetails", ns=NS_ENV), "IDAuthentication", ns=NS_ENV)
    E(ida, "SenderID", md5(pid), NS_ENV)
    au = E(ida, "Authentication", ns=NS_ENV)
    E(au, "Method", "clear", NS_ENV)
    E(au, "Value", md5(code), NS_ENV)
    E(E(g, "GovTalkDetails", ns=NS_ENV), "Keys", ns=NS_ENV)
    E(g, "Body", ns=NS_ENV).append(body_el)
    return etree.tostring(g, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def masked(xml: bytes) -> bytes:
    return re.sub(rb"(<(SenderID|Value)>)[^<]*(</)", rb"\1********\3", xml)


# ----------------------------------------------------------------------------- official schema check

_SCHEMAS: dict = {}


def schemas() -> dict:
    if _SCHEMAS:
        return _SCHEMAS
    if etree is None:
        raise RuntimeError("lxml is not installed – add 'lxml' to requirements.txt.")
    if not SCHEMA_ZIP.exists():
        raise RuntimeError("ch_schemas.zip is missing – upload it to the main folder of the GitHub repo.")
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(SCHEMA_ZIP) as z:
        z.extractall(SCHEMA_DIR)
    for key, fn in (("inc", "CompanyIncorporation-v3-8.xsd"), ("fs", "FormSubmission-v2-11.xsd"),
                    ("env", "Egov_ch-v2-0.xsd")):
        _SCHEMAS[key] = etree.XMLSchema(etree.parse(str(SCHEMA_DIR / fn)))
    return _SCHEMAS


def _explain(err) -> str:
    msg = re.sub(r"\{http[^}]*\}", "", str(err.message))
    mm = re.search(r"Element '(\w+)': \[facet 'pattern'\] The value '([^']*)'", msg)
    if mm:
        msg = f"{mm.group(1)} '{mm.group(2)}' contains a character Companies House does not accept (use plain letters)"
    mm = re.search(r"Element '(\w+)': \[facet '(minLength|maxLength|length)'\] The value has a length of '(\d+)'.*?'(\d+)'", msg)
    if mm:
        msg = f"{mm.group(1)} is {mm.group(3)} characters – the {'minimum' if mm.group(2)=='minLength' else 'maximum'} is {mm.group(4)}"
    path = re.sub(r"\{http[^}]*\}", "", err.path or "")
    path = re.sub(r"/(CompanyIncorporation|FormSubmission|GovTalkMessage)/?", "", path, count=1)
    return f"{path}: {msg}" if path and "*" not in path else msg


def schema_check(fs) -> list[str]:
    sc = schemas()
    out = []
    inc = fs.find(f"{{{NS_HDR}}}Form/{{{NS_CH}}}CompanyIncorporation")
    if not sc["inc"].validate(etree.fromstring(etree.tostring(inc))):
        out += ["Incorporation: " + _explain(e) for e in sc["inc"].error_log]
    shell = etree.fromstring(etree.tostring(fs))
    form = shell.find(f"{{{NS_HDR}}}Form")
    for ch in list(form):
        form.remove(ch)
    if not sc["fs"].validate(shell):
        out += ["Form header: " + _explain(e) for e in sc["fs"].error_log]
    return out


# ----------------------------------------------------------------------------- gateway

def post_gateway(xml: bytes, timeout=60) -> bytes:
    req = urllib.request.Request(GATEWAY, data=xml, headers={"Content-Type": "text/xml; charset=UTF-8",
                                                             "User-Agent": "StartitupMailRoom/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _lname(el) -> str:
    return etree.QName(el).localname


def find_all(root, name):
    return [e for e in root.iter() if isinstance(e.tag, str) and _lname(e) == name]


def find1(root, name):
    r = find_all(root, name)
    return r[0] if r else None


def text(root, name, default=""):
    e = find1(root, name)
    return (e.text or "").strip() if e is not None and e.text else default


def gateway_errors(resp) -> list[str]:
    out = []
    for e in find_all(resp, "Error"):
        t = " ".join(x for x in (text(e, "Number"), text(e, "Text")) if x)
        loc = text(e, "Location")
        out.append(t + (f" (at {loc})" if loc else ""))
    return out


def next_submission_number(s: dict) -> str:
    n = int(s.get("next_submission") or 1)
    s["next_submission"] = str(n + 1)
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    v, out = n, ""
    while v:
        v, r = divmod(v, 36)
        out = digits[r] + out
    return "SU" + out.rjust(4, "0")[-4:]


def submit(m: dict, mode: str) -> tuple[bool, str]:
    s = load_settings()
    errs, _ = precheck(m, s)
    if errs:
        return False, "Fix the problems shown first."
    pid, code = creds(mode)
    if not (pid and code):
        return False, ("Presenter ID / code not set. Add CH_PRESENTER_ID and CH_PRESENTER_CODE in Railway → Variables"
                       + (" (or CH_TEST_PRESENTER_ID / CH_TEST_PRESENTER_CODE)." if mode == "test" else "."))
    if mode == "live" and not s.get("live_approved"):
        return False, "LIVE filing is locked until Companies House has approved the software (tick it in Settings)."
    with _LOCK:
        s = load_settings()
        subno = next_submission_number(s)
        save_settings(s)
    fs = build_form_submission(m, s, subno)
    probs = schema_check(fs)
    if probs:
        return False, "Schema check failed: " + "; ".join(probs[:5])
    xml = envelope("CompanyIncorporation", fs, mode)
    d = job_dir(m["id"]) / "out"
    d.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (d / f"sent_{stamp}_{subno}.xml").write_bytes(masked(xml))
    try:
        raw = post_gateway(xml)
    except Exception as ex:
        m["status"] = "error"
        log(m, f"Could not reach Companies House ({mode.upper()}): {ex}")
        return False, f"Could not reach Companies House: {ex}"
    (d / f"reply_{stamp}_{subno}.xml").write_bytes(raw)
    try:
        resp = etree.fromstring(raw)
    except Exception:
        m["status"] = "error"
        log(m, "Companies House sent a reply that is not XML.")
        return False, "Companies House sent an unreadable reply (saved on the job)."
    q = text(resp, "Qualifier")
    errs = gateway_errors(resp)
    if q == "error" or errs:
        m["status"] = "error"
        log(m, f"{mode.upper()} submission {subno} REFUSED by the gateway: " + "; ".join(errs or ["(no reason given)"]))
        return False, "Refused by the Companies House gateway: " + "; ".join(errs or ["no reason given"])
    m.update(status="submitted", mode=mode, submission_number=subno,
             submitted_at=dt.datetime.now().isoformat(timespec="seconds"), rejections=[], examiner="")
    m.setdefault("submissions", []).append({"no": subno, "mode": mode, "t": m["submitted_at"]})
    log(m, f"Sent to Companies House ({mode.upper()}), submission number {subno}. "
           + ("TEST – no fee, no real company." if mode == "test" else "LIVE – fee charged to the credit account."))
    return True, f"Sent ({mode.upper()}). Submission number {subno}."


def poll(m: dict) -> str:
    if not m.get("submission_number") or m.get("status") not in ("submitted", "pending", "parked", "accepted"):
        return "Nothing to check."
    mode = m.get("mode") or "test"
    pid, _ = creds(mode)
    body = etree.Element(f"{{{NS_CH}}}GetSubmissionStatus", nsmap={None: NS_CH})
    E(body, "SubmissionNumber", m["submission_number"])
    E(body, "PresenterID", pid)
    try:
        raw = post_gateway(envelope("GetSubmissionStatus", body, mode))
        resp = etree.fromstring(raw)
    except Exception as ex:
        return f"Could not reach Companies House: {ex}"
    errs = gateway_errors(resp)
    if errs:
        return "Gateway error: " + "; ".join(errs)
    m["last_poll"] = dt.datetime.now().isoformat(timespec="seconds")
    st = [x for x in find_all(resp, "Status") if text(x, "SubmissionNumber").upper() == m["submission_number"].upper()]
    if not st:
        return "No status yet – Companies House has not processed it."
    x = st[-1]
    code = text(x, "StatusCode")
    rej = [f"{text(r, 'RejectCode')}: {text(r, 'Description')}" for r in find_all(x, "Reject")]
    ex_ = find1(x, "Examiner")
    examiner = (" – ".join(v for v in (text(ex_, "Telephone"), text(ex_, "Comment")) if v)) if ex_ is not None else ""
    if code == "ACCEPT":
        m["company_number"] = text(x, "CompanyNumber") or m.get("company_number", "")
        inc = find1(x, "IncorporationDetails")
        if inc is not None:
            m["incorporation_date"] = text(inc, "IncorporationDate")
            m["auth_code"] = text(inc, "AuthenticationCode")
            m["doc_key"] = text(inc, "DocRequestKey")
        if m.get("status") != "accepted":
            m["status"] = "accepted"
            log(m, f"ACCEPTED – company number {m.get('company_number')}, incorporated {m.get('incorporation_date', '')}.")
            notify(m, "accepted")
        if m.get("doc_key") and not m.get("cert"):
            msg = fetch_certificate(m)
            log(m, msg)
        return f"Accepted – company number {m.get('company_number')}."
    if code == "REJECT":
        if m.get("status") != "rejected":
            m.update(status="rejected", rejections=rej, examiner=examiner,
                     reject_reference=text(x, "RejectReference"))
            if m.get("reject_reference"):
                m["a"]["reject_reference"] = m["reject_reference"]
            log(m, "REJECTED: " + "; ".join(rej or ["no reason given"]) + (f" Examiner: {examiner}" if examiner else ""))
            notify(m, "rejected")
        return "Rejected – see the reasons."
    if code in ("PENDING", "PARKED"):
        new = code.lower()
        if m.get("status") != new:
            m["status"] = new
            log(m, f"Companies House status: {code}." + (f" Examiner: {examiner}" if examiner else ""))
        m["examiner"] = examiner
        return f"Status: {code}."
    if code == "INTERNAL_FAILURE":
        log(m, "Companies House reported an INTERNAL_FAILURE – contact them quoting the submission number.")
        return "Companies House internal failure."
    return f"Status: {code or 'unknown'}."


def fetch_certificate(m: dict) -> str:
    body = etree.Element(f"{{{NS_CH}}}GetDocument", nsmap={None: NS_CH})
    E(body, "DocRequestKey", m["doc_key"])
    try:
        resp = etree.fromstring(post_gateway(envelope("GetDocument", body, m.get("mode") or "test"), timeout=120))
    except Exception as ex:
        return f"Certificate not downloaded yet: {ex}"
    data = find1(resp, "DocumentData")
    if data is None or not (data.text or "").strip():
        errs = gateway_errors(resp)
        return "Certificate not available yet" + (": " + "; ".join(errs) if errs else ".")
    d = job_dir(m["id"]) / "out"
    d.mkdir(parents=True, exist_ok=True)
    fn = f"Certificate_of_Incorporation_{m.get('company_number') or m['id']}.pdf"
    (d / fn).write_bytes(base64.b64decode(data.text))
    m["cert"] = fn
    return "Certificate of incorporation downloaded."


def notify(m: dict, what: str):
    s = load_settings()
    to = (s.get("notify_email") or "").strip()
    if not to or not _SEND["fn"]:
        return
    a = m["a"]
    if what == "accepted":
        subj = f"INCORPORATED: {a.get('company_name')} – {m.get('company_number')}"
        body = (f"{a.get('company_name')} has been incorporated by Companies House ({(m.get('mode') or '').upper()}).\n\n"
                f"Company number: {m.get('company_number')}\nIncorporation date: {m.get('incorporation_date', '')}\n"
                f"Authentication code: {m.get('auth_code', '')}\n\nOpen the Mail Room → UK Formations for the certificate.")
    else:
        subj = f"REJECTED: {a.get('company_name')} – Companies House incorporation"
        body = (f"Companies House rejected the incorporation of {a.get('company_name')}.\n\nReasons:\n- "
                + "\n- ".join(m.get("rejections") or ["(none given)"])
                + f"\n\nReject reference: {m.get('reject_reference', '')}\nExaminer: {m.get('examiner', '')}\n\n"
                  "Correct it in the Mail Room → UK Formations and send again (the reject reference is filled in "
                  "automatically so no new fee is charged within the time limit).")
    try:
        _SEND["fn"](_cfg(), to, subj, body, [])
    except Exception as ex:
        log(m, f"Notification email failed: {ex}")


def alert_count() -> int:
    return sum(1 for m in list_jobs() if m.get("status") in ("rejected", "error", "parked"))


def _poll_loop():
    while True:
        time.sleep(600)
        try:
            for m0 in list_jobs():
                if m0.get("status") in ("submitted", "pending", "parked") or (
                        m0.get("status") == "accepted" and m0.get("doc_key") and not m0.get("cert")):
                    with _LOCK:
                        m = load_job(m0["id"])
                        poll(m)
                        save_job(m)
        except Exception as ex:  # never kill the thread
            print(f"[formations] poll error: {ex}", flush=True)


# ----------------------------------------------------------------------------- AI read (passports / order form)

READ_SYSTEM = """You read documents for a UK company formation agent preparing a Companies House incorporation.
Documents can be: passports, ID cards, driving licences, proof of address, the customer's order form / questionnaire,
emails with the requested company details. Read ONLY what is printed or written. Never invent. Unknown values are "".
Dates as YYYY-MM-DD. Return ONLY one JSON object:
{"company_name": "", "company_type": "BYSHR" | "BYGUAR" | "PLC" | "LLP" | "",
 "sic_codes": [""], "business_activity": "", "registered_email": "",
 "people": [{"title": "", "forename": "", "other_forenames": "", "surname": "",   // names exactly as on the passport
             "dob": "", "nationality": "",          // nationality as an English adjective, e.g. "British", "Pakistani"
             "residence_country": "",               // country where they live, e.g. "United Kingdom", "Pakistan"
             "home_address": {"premise": "", "street": "", "post_town": "", "county": "", "postcode": "", "country": ""},
             "roles": ["director" | "secretary" | "member" | "psc" | "subscriber" | "guarantor"],
             "shares": "",                          // number of shares if stated
             "ownership_pct": "",                   // e.g. "100" if stated
             "personal_code": ""}],                 // Companies House personal code (11 characters) only if written
 "notes": ""}                                       // anything to check: unreadable parts, conflicts, missing data
"premise" is the house/flat/building name or number. Merge the same person found in several documents into one entry."""


def _images(path: Path) -> list[tuple[str, bytes]]:
    data = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        try:
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            out = []
            for i, page in enumerate(doc):
                if i >= 4:
                    break
                out.append(("image/jpeg", page.get_pixmap(dpi=130).tobytes("jpeg")))
            return out
        except Exception:
            return []
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    im.thumbnail((2000, 2000))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return [("image/jpeg", buf.getvalue())]


def ai_read(m: dict) -> dict:
    cfg = _cfg()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic API key is set – type the details by hand.")
    imgs = []
    d = job_dir(m["id"]) / "docs"
    for u in m.get("uploads", []):
        imgs += _images(d / u["file"])
    imgs = imgs[:16]
    if not imgs:
        raise RuntimeError("Upload passports / the order form first.")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(b).decode()}}
               for mt, b in imgs]
    content.append({"type": "text", "text": "Read these documents and return the JSON."})
    resp = client.messages.create(model=cfg.get("model") or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5"),
                                  max_tokens=3000, system=READ_SYSTEM, messages=[{"role": "user", "content": content}])
    raw = resp.content[0].text.strip()
    mm = re.search(r"\{.*\}", raw, re.S)
    if not mm:
        raise RuntimeError("The AI reply could not be read – type the details by hand.")
    return json.loads(mm.group(0))


# ----------------------------------------------------------------------------- name check (CH REST API)

def name_check(name: str) -> dict:
    key = os.environ.get("CH_API_KEY", "").strip()
    if not key:
        return {"error": "CH_API_KEY is not set – check the name on find-and-update.company-information.service.gov.uk"}
    q = urllib.parse.urlencode({"q": name, "items_per_page": 20})
    req = urllib.request.Request("https://api.company-information.service.gov.uk/search/companies?" + q)
    req.add_header("Authorization", "Basic " + base64.b64encode((key + ":").encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            items = json.loads(r.read()).get("items", [])
    except Exception as ex:
        return {"error": f"Name search failed: {ex}"}
    want = norm_name(name)
    out = []
    for it in items:
        t = it.get("title", "")
        out.append({"title": t, "number": it.get("company_number", ""), "status": it.get("company_status", ""),
                    "same": norm_name(t) == want and bool(want)})
    return {"items": out, "clash": any(x["same"] for x in out)}


# ----------------------------------------------------------------------------- routes

def _ctx(**kw):
    s = load_settings()
    kw.setdefault("s", s)
    kw.update(STATUS=STATUS, VERSION=VERSION, COMPANY_TYPES=COMPANY_TYPES)
    return kw


@bp.get("/formations")
def list_page():
    s = load_settings()
    live_id, live_code = creds("live")
    t_id = os.environ.get("CH_TEST_PRESENTER_ID", "").strip()
    return render_template_string(LIST_PAGE, **_ctx(jobs=list_jobs(), live_set=bool(live_id and live_code),
                                                    test_set=bool(t_id), lxml_ok=etree is not None,
                                                    schemas_ok=SCHEMA_ZIP.exists(), gateway=GATEWAY,
                                                    api_key=bool(os.environ.get("CH_API_KEY"))))


@bp.post("/formations/settings")
def settings_save():
    s = load_settings()
    for k in SETTINGS_DEFAULT:
        if k in ("live_approved",):
            continue
        if k in request.form:
            s[k] = request.form.get(k, "").strip()
    s["live_approved"] = "1" if request.form.get("live_approved") else ""
    if s.get("mode") not in ("test", "live"):
        s["mode"] = "test"
    save_settings(s)
    flash("Formation settings saved.")
    return redirect("/formations")


@bp.route("/formations/new", methods=["GET", "POST"])
def new():
    m = new_job()
    save_job(m)
    return redirect(f"/formations/{m['id']}")


@bp.get("/formations/<job>")
def job_page(job):
    m = load_job(job)
    s = load_settings()
    errs, warns = precheck(m, s)
    has_ai = bool(_cfg().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))
    rows, cap = share_rows(m["a"]) if has_shares(m["a"]) else ([], {})
    files = sorted(p.name for p in (job_dir(job) / "out").glob("*")) if (job_dir(job) / "out").exists() else []
    return render_template_string(JOB_PAGE, **_ctx(m=m, a=m["a"], errs=errs, warns=warns, has_ai=has_ai,
                                                   cap={k: {"num": v["num"], "agg": fmt_dec(v["agg"]),
                                                            "unpaid": fmt_dec(v["unpaid"])} for k, v in cap.items()},
                                                   files=files, JUR=JURISDICTIONS, ART=ARTICLES, MISMATCH=MISMATCH,
                                                   NOC_COMPANY=NOC_COMPANY, NOC_LLP=NOC_LLP, DOC_LABEL=DOC_LABEL,
                                                   live_ok=bool(all(creds("live")) and s.get("live_approved")),
                                                   creds_ok=bool(all(creds("test")))))


@bp.post("/formations/<job>/save")
def save(job):
    with _LOCK:
        m = load_job(job)
        if m.get("status") in ("submitted", "pending", "parked", "accepted"):
            return jsonify(ok=False, error="This incorporation has been sent – it can no longer be changed.")
        a = request.get_json(force=True, silent=True)
        if not isinstance(a, dict) or "people" not in a:
            return jsonify(ok=False, error="Nothing to save.")
        for p in a.get("people", []):
            base = new_person()
            base.update(p)
            p.clear()
            p.update(base)
        m["a"] = a
        save_job(m)
    errs, warns = precheck(m, load_settings())
    return jsonify(ok=True, errors=errs, warnings=warns)


@bp.get("/formations/<job>/check")
def check(job):
    m = load_job(job)
    s = load_settings()
    errs, warns = precheck(m, s)
    schema = []
    if not errs:
        try:
            schema = schema_check(build_form_submission(m, s, "TEST01"))
        except Exception as ex:
            schema = [f"Schema check could not run: {ex}"]
    return jsonify(errors=errs, warnings=warns, schema=schema, ok=not errs and not schema)


@bp.get("/formations/<job>/xml")
def xml_preview(job):
    m = load_job(job)
    s = load_settings()
    fs = build_form_submission(m, s, m.get("submission_number") or "PREVIEW"[:6])
    for d in fs.iter(f"{{{NS_HDR}}}Data"):
        d.text = f"[{len(d.text or '')} characters of base64 PDF]"
    return send_file(io.BytesIO(etree.tostring(fs, pretty_print=True, encoding="UTF-8", xml_declaration=True)),
                     mimetype="text/plain", download_name="incorporation_preview.xml")


@bp.post("/formations/<job>/submit")
def submit_route(job):
    mode = request.form.get("mode", "test")
    if mode == "live" and request.form.get("confirm_live") != "LIVE":
        flash("Type LIVE to confirm a real (fee-paying) filing.")
        return redirect(f"/formations/{job}")
    if not request.form.get("approved"):
        flash("Tick that the details were approved by the client and every personal code has been checked.")
        return redirect(f"/formations/{job}")
    with _LOCK:
        m = load_job(job)
        if m.get("status") in ("submitted", "pending", "parked", "accepted"):
            flash("Already sent.")
            return redirect(f"/formations/{job}")
        ok, msg = submit(m, mode)
        save_job(m)
    flash(msg)
    return redirect(f"/formations/{job}")


@bp.post("/formations/<job>/poll")
def poll_route(job):
    with _LOCK:
        m = load_job(job)
        msg = poll(m)
        if m.get("status") == "accepted" and m.get("doc_key") and not m.get("cert"):
            msg += " " + fetch_certificate(m)
        save_job(m)
    flash(msg)
    return redirect(f"/formations/{job}")


@bp.post("/formations/<job>/reopen")
def reopen(job):
    with _LOCK:
        m = load_job(job)
        if m.get("status") in ("rejected", "error"):
            m["status"] = "draft"
            log(m, "Re-opened for correction.")
            save_job(m)
    return redirect(f"/formations/{job}")


@bp.post("/formations/<job>/upload")
def upload(job):
    m = load_job(job)
    cat = request.form.get("cat", "ID")
    d = job_dir(job) / "docs"
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if cat in DOC_LABEL:
            if ext != ".pdf":
                flash(f"{DOC_LABEL[cat]} must be a PDF.")
                continue
            fn = f"{cat}.pdf"
            f.save(d / fn)
            m.setdefault("docs", {})[cat] = fn
        else:
            if ext not in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic"):
                continue
            fn = f"id_{uuid.uuid4().hex[:6]}{ext}"
            f.save(d / fn)
            m.setdefault("uploads", []).append({"file": fn, "name": f.filename[:80]})
        n += 1
    with _LOCK:
        mm = load_job(job)
        mm["docs"], mm["uploads"] = m.get("docs", {}), m.get("uploads", [])
        save_job(mm)
    flash(f"{n} file(s) uploaded.")
    return redirect(f"/formations/{job}#docs")


@bp.post("/formations/<job>/docdel/<cat>")
def doc_delete(job, cat):
    with _LOCK:
        m = load_job(job)
        if cat in m.get("docs", {}):
            m["docs"].pop(cat)
        else:
            m["uploads"] = [u for u in m.get("uploads", []) if u["file"] != cat]
        save_job(m)
    return redirect(f"/formations/{job}#docs")


@bp.post("/formations/<job>/read")
def read(job):
    m = load_job(job)
    try:
        return jsonify(ok=True, data=ai_read(m))
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@bp.get("/formations/<job>/namecheck")
def namecheck(job):
    return jsonify(name_check(request.args.get("name", "")))


@bp.get("/formations/<job>/file/<name>")
def file(job, name):
    p = job_dir(job) / "out" / Path(name).name
    if not p.exists():
        p = job_dir(job) / "docs" / Path(name).name
    if not p.exists():
        abort(404)
    return send_file(p, as_attachment=request.args.get("dl") == "1")


@bp.post("/formations/<job>/email")
def email_client(job):
    m = load_job(job)
    to = (request.form.get("to") or m["a"].get("client_email") or "").strip()
    if not to or not m.get("company_number"):
        flash("No client email or not incorporated yet.")
        return redirect(f"/formations/{job}")
    a = m["a"]
    att = [job_dir(job) / "out" / m["cert"]] if m.get("cert") else []
    body = (f"Dear {a.get('client_name') or 'client'},\n\nGood news – {a.get('company_name')} has been incorporated at "
            f"Companies House.\n\nCompany number: {m['company_number']}\nDate of incorporation: "
            f"{m.get('incorporation_date', '')}\nCompany authentication code: {m.get('auth_code', '')}\n"
            "(keep the authentication code safe – it is needed to file for the company online)\n\n"
            + ("The certificate of incorporation is attached.\n\n" if att else "")
            + "Kind regards,\nThe Startitup Team")
    try:
        _SEND["fn"](_cfg(), to, f"{a.get('company_name')} – incorporated ({m['company_number']})", body, att)
        with _LOCK:
            mm = load_job(job)
            log(mm, f"Incorporation email sent to {to}.")
            mm["client_emailed"] = dt.datetime.now().isoformat(timespec="seconds")
            save_job(mm)
        flash(f"Email sent to {to}.")
    except Exception as ex:
        flash(f"Email failed: {ex}")
    return redirect(f"/formations/{job}")


@bp.post("/formations/<job>/delete")
def delete(job):
    if request.form.get("confirm") != "DELETE":
        flash("Type DELETE to remove this incorporation.")
        return redirect(f"/formations/{job}")
    import shutil
    m = load_job(job)
    if m.get("status") in ("submitted", "pending", "parked"):
        flash("It is with Companies House – wait for the result before deleting.")
        return redirect(f"/formations/{job}")
    shutil.rmtree(job_dir(job), ignore_errors=True)
    flash("Deleted.")
    return redirect("/formations")


def register(app, cfg_fn=None, send_fn=None):
    _CFG["fn"] = cfg_fn
    _SEND["fn"] = send_fn
    OUT.mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)
    if not _POLL["started"] and os.environ.get("FORMATIONS_NO_POLL") != "1":
        _POLL["started"] = True
        threading.Thread(target=_poll_loop, daemon=True).start()

    @app.context_processor
    def _formations_badge():
        try:
            return {"formation_alerts": alert_count()}
        except Exception:
            return {"formation_alerts": 0}


# ----------------------------------------------------------------------------- pages

LIST_PAGE = r"""{% extends "base" %}{% block body %}
<div class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
<h1 style="margin:0">UK Formations <span class="muted" style="font-weight:400">– incorporate at Companies House by software filing</span></h1>
<form method="post" action="/formations/new"><button class="btn">+ New incorporation</button></form></div>
<p class="muted">Enter the company once, check it, and send it straight to Companies House. The status is checked every
10 minutes; when it is accepted the certificate of incorporation is downloaded automatically.</p>
{% if not lxml_ok %}<div class="flash">lxml is not installed – add the line <b>lxml&gt;=5</b> to requirements.txt in GitHub.</div>{% endif %}
{% if not schemas_ok %}<div class="flash">ch_schemas.zip is missing – upload it to the main folder of the GitHub repo.</div>{% endif %}
{% if not live_set %}<div class="flash">Presenter ID / code are not set yet. Add <b>CH_PRESENTER_ID</b> and <b>CH_PRESENTER_CODE</b> in Railway → Variables (never in GitHub – the repo is public).</div>{% endif %}
<div class="btnrow" style="margin-top:6px">
<span class="pill {{ 'active' if s.mode=='live' else 'hold' }}">Mode: {{ 'LIVE – real companies, fees charged' if s.mode=='live' else 'TEST – nothing is really incorporated' }}</span>
<span class="pill {{ 'active' if live_set else 'hold' }}">Live presenter account {{ 'set' if live_set else 'NOT set' }}</span>
<span class="pill {{ 'active' if test_set else '' }}">Test account {{ 'set' if test_set else 'not set (test uses the live ID)' }}</span>
<span class="pill {{ 'active' if s.live_approved else '' }}">{{ 'CH approved our software' if s.live_approved else 'Awaiting CH software approval' }}</span>
<span class="pill {{ 'active' if api_key else '' }}">Name check {{ 'on' if api_key else 'off (no CH_API_KEY)' }}</span>
</div></div>

<div class="card"><h2>Incorporations</h2>
{% if not jobs %}<p class="muted">None yet.</p>{% else %}
<table><tr><th>Company</th><th>Type</th><th>Status</th><th>Company no.</th><th>Sent</th><th></th></tr>
{% for m in jobs %}<tr{% if m.status in ('rejected','error','parked') %} class="review"{% elif m.status=='accepted' %} class="sent"{% endif %}>
<td><b>{{ m.a.company_name or '(no name yet)' }}</b>{% if m.status=='rejected' and m.rejections %}<div class="muted" style="color:#b91c1c">⚠ {{ m.rejections[0] }}</div>{% endif %}</td>
<td>{{ m.a.company_type }}</td>
<td><span class="pill{% if m.status=='accepted' %} active{% elif m.status in ('rejected','error') %} hold{% endif %}">{{ STATUS.get(m.status, m.status) }}</span>{% if m.mode %} <span class="muted tiny">{{ m.mode|upper }}</span>{% endif %}</td>
<td>{{ m.company_number or '' }}</td>
<td>{{ (m.submitted_at or '')|ukdate }}</td>
<td><a class="btn small secondary" href="/formations/{{m.id}}">Open</a></td></tr>{% endfor %}
</table>{% endif %}</div>

<div class="card"><h2>Settings</h2>
<form method="post" action="/formations/settings">
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 16px">
<div><label>Filing mode</label><select name="mode" style="padding:8px;width:100%">
<option value="test" {{ 'selected' if s.mode!='live' }}>TEST (GatewayTest = 1, no fee)</option>
<option value="live" {{ 'selected' if s.mode=='live' }}>LIVE (real incorporation, fee)</option></select></div>
<div><label>Package reference</label><input type="text" name="package_reference" value="{{s.package_reference}}">
<div class="muted tiny">0012 for testing. Use the one Companies House gives you when they approve the software.</div></div>
<div><label>Next submission number (counter)</label><input type="text" name="next_submission" value="{{s.next_submission}}"></div>
<div><label>Notify email (accepted / rejected)</label><input type="text" name="notify_email" value="{{s.notify_email}}"></div>
<div><label>Default registered email address</label><input type="text" name="registered_email_default" value="{{s.registered_email_default}}"></div>
</div>
<h2 style="margin-top:18px">Agent (signs every incorporation as "Authoriser – Agent")</h2>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:0 16px">
<div><label>Agent firm name (holds the presenter account)</label><input type="text" name="agent_company" value="{{s.agent_company}}" placeholder="e.g. Startitup Global Ltd"></div>
<div><label>Agent forename</label><input type="text" name="agent_forename" value="{{s.agent_forename}}"></div>
<div><label>Agent surname</label><input type="text" name="agent_surname" value="{{s.agent_surname}}"></div>
<div><label>Building</label><input type="text" name="agent_premise" value="{{s.agent_premise}}"></div>
<div><label>Address line 1</label><input type="text" name="agent_street" value="{{s.agent_street}}"></div>
<div><label>Address line 2</label><input type="text" name="agent_thoroughfare" value="{{s.agent_thoroughfare}}"></div>
<div><label>Town</label><input type="text" name="agent_post_town" value="{{s.agent_post_town}}"></div>
<div><label>Postcode</label><input type="text" name="agent_postcode" value="{{s.agent_postcode}}"></div>
<div><label>Country code</label><input type="text" name="agent_country" value="{{s.agent_country}}"></div>
<div><label>Contact name for CH (optional)</label><input type="text" name="contact_name" value="{{s.contact_name}}"></div>
<div><label>Contact phone for CH (optional)</label><input type="text" name="contact_number" value="{{s.contact_number}}"></div>
</div>
<label style="margin-top:16px"><input type="checkbox" name="live_approved" {{ 'checked' if s.live_approved }}> Companies House has tested and approved this software for incorporations (unlocks LIVE filing)</label>
<p class="muted tiny">Gateway: {{gateway}} · {{VERSION}}</p>
<button class="btn">Save settings</button></form></div>
{% endblock %}
"""
JOB_PAGE = r"""{% extends "base" %}{% block body %}
<style>
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:0 14px}
.fx select,.fx input[type=date]{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;background:#fff}
.person{border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0;background:#fcfcfc}
.person h3{margin:0;font-size:16px}
.roles{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.roles label{display:inline-flex;gap:5px;align-items:center;margin:0;padding:5px 11px;border:1px solid var(--line);border-radius:99px;background:#fff;cursor:pointer;font-weight:600}
.roles label.on{background:#111;color:#fff;border-color:#111}
.sub{border-left:3px solid #e5e7eb;padding-left:12px;margin-top:12px}
.sub h4{margin:6px 0 0;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:#555}
.chk{display:flex;gap:6px;align-items:flex-start;font-weight:600;margin:10px 0 0}
.chk input{margin-top:3px}
.noc{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:2px 10px}
.noc label{font-weight:400;margin:2px 0;display:flex;gap:6px}
.errs li{color:#b91c1c;margin:3px 0}.warns li{color:#92400e;margin:3px 0}
.savebar{position:sticky;top:0;z-index:5;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.lock input,.lock select,.lock textarea{pointer-events:none;background:#f3f4f6}
table.mini td,table.mini th{padding:5px}
</style>
{% set locked = m.status in ('submitted','pending','parked','accepted') %}
<div class="card">
<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
<h1 style="margin:0">{{ a.company_name or 'New incorporation' }}</h1>
<a class="btn small secondary" href="/formations">← All incorporations</a></div>
<div class="btnrow" style="margin-top:10px">
<span class="pill{% if m.status=='accepted' %} active{% elif m.status in ('rejected','error') %} hold{% endif %}" style="font-size:14px;padding:4px 12px">{{ STATUS.get(m.status, m.status) }}</span>
{% if m.mode %}<span class="pill">{{ m.mode|upper }}</span>{% endif %}
{% if m.submission_number %}<span class="muted">Submission no. <b>{{ m.submission_number }}</b></span>{% endif %}
{% if m.last_poll %}<span class="muted">· last checked {{ m.last_poll|ukdate }}</span>{% endif %}
</div>
{% if m.status=='accepted' %}
<div style="margin-top:14px;padding:14px;background:var(--ok);border-radius:8px">
<b>Company number: {{ m.company_number }}</b> · incorporated {{ (m.incorporation_date or '')|ukdate }} · authentication code <b>{{ m.auth_code }}</b>
<div class="btnrow" style="margin-top:10px">
{% if m.cert %}<a class="btn small" href="/formations/{{m.id}}/file/{{m.cert}}" target="_blank">Certificate of incorporation</a>{% else %}<span class="muted">Certificate not downloaded yet – press "Check status now".</span>{% endif %}
<form method="post" action="/formations/{{m.id}}/email" style="display:flex;gap:6px"><input type="text" name="to" value="{{a.client_email}}" placeholder="client email" style="width:240px">
<button class="btn small secondary">Email company number{% if m.cert %} + certificate{% endif %} to client</button></form>
{% if m.client_emailed %}<span class="muted">sent {{ m.client_emailed|ukdate }}</span>{% endif %}</div></div>{% endif %}
{% if m.status=='rejected' %}
<div style="margin-top:14px;padding:14px;background:#fee2e2;border-radius:8px"><b>Rejected by Companies House</b>
<ul>{% for r in m.rejections or [] %}<li>{{r}}</li>{% endfor %}</ul>
{% if m.examiner %}<div class="muted">Examiner: {{m.examiner}}</div>{% endif %}
{% if m.reject_reference %}<div>Reject reference <b>{{m.reject_reference}}</b> – it is filled in automatically when you send again, so no second fee is charged (within the time limit).</div>{% endif %}
<form method="post" action="/formations/{{m.id}}/reopen" style="margin-top:8px"><button class="btn small">Correct and send again</button></form></div>{% endif %}
{% if m.status=='error' %}<form method="post" action="/formations/{{m.id}}/reopen" style="margin-top:10px"><span class="muted">The last attempt did not reach Companies House or was refused by the gateway – see the history. </span><button class="btn small secondary">Edit again</button></form>{% endif %}
{% if m.status in ('submitted','pending','parked','accepted') %}
<form method="post" action="/formations/{{m.id}}/poll" style="margin-top:10px"><button class="btn small secondary">Check status now</button></form>{% endif %}
</div>

<div class="savebar"><button class="btn small" id="saveBtn" type="button" {{ 'disabled' if locked }}>Save</button>
<span id="saveMsg" class="muted">{{ 'Sent to Companies House – read only.' if locked else 'Changes save automatically.' }}</span>
<span style="margin-left:auto" id="probCount"></span></div>

<div id="form" class="fx{{ ' lock' if locked }}">
<div class="card"><h2>1. Company</h2>
<div class="grid">
<div style="grid-column:span 2"><label>Proposed company name</label><div style="display:flex;gap:6px"><input type="text" data-p="company_name" id="cname"><button class="btn small secondary" type="button" onclick="nameCheck()">Check name</button></div>
<div id="nameRes" class="muted"></div></div>
<div><label>Company type</label><select data-p="company_type" data-r="1">{% for k,v in COMPANY_TYPES.items() %}<option value="{{k}}">{{v}}</option>{% endfor %}</select></div>
<div><label>Registered in</label><select data-p="jurisdiction" data-r="1">{% for k,v in JUR.items() %}<option value="{{k}}">{{v}}</option>{% endfor %}</select></div>
</div>
<div class="grid">
<div><label>Registered email address (not public)</label><input type="text" data-p="registered_email"></div>
<div><label>Client name (for the email)</label><input type="text" data-p="client_name"></div>
<div><label>Client email</label><input type="text" data-p="client_email"></div>
</div>
<div id="companyExtra"></div>
<h2 style="margin-top:18px">Registered office</h2>
<div id="roBox"></div>
<p class="muted tiny">Default: SIU OFFICE, 4-6, Greatorex Street, London, E1 5NF (as entered on Companies House for all clients). Add the unit in "Building" if the client has one.</p>
</div>

<div class="card" id="docs"><h2>2. Documents</h2>
<div class="grid">
<div><b>Passports / ID / order form</b> <span class="muted">(for the AI read – not sent to Companies House)</span>
<ul>{% for u in m.uploads or [] %}<li><a href="/formations/{{m.id}}/file/{{u.file}}" target="_blank">{{u.name}}</a>
<form method="post" action="/formations/{{m.id}}/docdel/{{u.file}}" style="display:inline"><button class="btn small secondary" {{ 'disabled' if locked }}>×</button></form></li>{% endfor %}</ul>
<form method="post" action="/formations/{{m.id}}/upload" enctype="multipart/form-data" class="btnrow"><input type="hidden" name="cat" value="ID"><input type="file" name="files" multiple accept=".pdf,.jpg,.jpeg,.png,.webp"><button class="btn small secondary">Upload</button></form>
{% if has_ai %}<button class="btn small" type="button" style="margin-top:8px" onclick="aiRead()" {{ 'disabled' if locked or not m.uploads }}>Read with AI → fill the form</button>{% else %}<p class="muted">No Anthropic API key – type the details by hand.</p>{% endif %}
<div id="aiMsg" class="muted"></div></div>
<div><b>Documents sent to Companies House</b> <span class="muted">(PDF only, only when needed)</span>
<table class="mini">{% for k,lab in DOC_LABEL.items() %}<tr><td>{{lab}}</td><td>{% if m.docs.get(k) %}<a href="/formations/{{m.id}}/file/{{m.docs[k]}}" target="_blank">view</a>
<form method="post" action="/formations/{{m.id}}/docdel/{{k}}" style="display:inline"><button class="btn small secondary" {{ 'disabled' if locked }}>×</button></form>{% else %}
<form method="post" action="/formations/{{m.id}}/upload" enctype="multipart/form-data" style="display:flex;gap:4px"><input type="hidden" name="cat" value="{{k}}"><input type="file" name="files" accept=".pdf" style="width:170px"><button class="btn small secondary" {{ 'disabled' if locked }}>Upload</button></form>{% endif %}</td></tr>{% endfor %}</table>
</div></div></div>

<div class="card" id="sharesCard"><h2>3. Share classes</h2><div id="classes"></div>
<button class="btn small secondary" type="button" onclick="addClass()">+ Share class</button>
<div id="capSummary" class="muted" style="margin-top:10px"></div></div>

<div class="card"><h2 id="peopleTitle">4. People</h2>
<p class="muted">Add each person or company once and tick every role they have. Directors and LLP members need their 11-character Companies House personal code (identity verification).</p>
<div id="people"></div>
<div class="btnrow"><button class="btn small" type="button" onclick="addPerson('person')">+ Individual</button>
<button class="btn small secondary" type="button" onclick="addPerson('corporate')">+ Company / corporate body</button>
<button class="btn small secondary" type="button" onclick="addPerson('legal')">+ Other legal person (PSC only)</button></div>
<label class="chk" style="margin-top:16px"><input type="checkbox" data-p="no_psc" data-r="1"> There is no person with significant control (no PSC statement)</label>
</div>
</div>

<div class="card"><h2>5. Check and send</h2>
<div id="probs">{% if errs %}<ul class="errs">{% for e in errs %}<li>{{e}}</li>{% endfor %}</ul>{% endif %}{% if warns %}<ul class="warns">{% for w in warns %}<li>{{w}}</li>{% endfor %}</ul>{% endif %}</div>
<div class="btnrow"><button class="btn secondary" type="button" onclick="fullCheck()">Run full check (official Companies House schema)</button>
<a class="btn secondary" href="/formations/{{m.id}}/xml" target="_blank">Preview the XML</a></div>
<div id="checkRes" style="margin-top:10px"></div>
{% if not locked %}
<form method="post" action="/formations/{{m.id}}/submit" style="margin-top:16px;padding:14px;border:1px solid var(--line);border-radius:10px" onsubmit="return beforeSend(this)">
<label class="chk"><input type="checkbox" name="approved"> The client approved every detail, all directors/members consented to act, and I checked each personal code against the verified name.</label>
<div class="btnrow" style="margin-top:12px">
<button class="btn" name="mode" value="test" {{ 'disabled' if not creds_ok }}>Send as TEST</button>
{% if s.mode=='live' %}<input type="text" name="confirm_live" placeholder="type LIVE" style="width:110px">
<button class="btn" name="mode" value="live" style="background:#b91c1c" {{ 'disabled' if not live_ok }}>Send LIVE – £{{ '156' if a.same_day else '100' }} fee</button>{% endif %}
</div>
{% if not creds_ok %}<p class="muted">Presenter ID / code are not set in Railway → Variables.</p>{% endif %}
{% if s.mode=='live' and not live_ok %}<p class="muted">LIVE is locked until "Companies House has approved this software" is ticked in Settings.</p>{% endif %}
{% if s.mode!='live' %}<p class="muted">Settings mode is TEST. Switch to LIVE on the UK Formations page once Companies House has approved the software.</p>{% endif %}
</form>{% endif %}
</div>

<div class="card"><h2>History</h2>
{% if m.log %}<table class="mini">{% for l in m.log|reverse %}<tr><td style="white-space:nowrap">{{ l.t|ukdate }}</td><td>{{ l.text }}</td></tr>{% endfor %}</table>{% else %}<p class="muted">Nothing yet.</p>{% endif %}
{% if files %}<p class="muted" style="margin-top:10px">Files: {% for f in files %}<a href="/formations/{{m.id}}/file/{{f}}" target="_blank">{{f}}</a>{{ ', ' if not loop.last }}{% endfor %}</p>{% endif %}
{% if m.status not in ('submitted','pending','parked') %}
<form method="post" action="/formations/{{m.id}}/delete" style="margin-top:14px;display:flex;gap:6px"><input type="text" name="confirm" placeholder="type DELETE" style="width:140px"><button class="btn small secondary">Delete</button></form>{% endif %}
</div>

<script>
const JOB = {{ m.id|tojson }};
const LOCKED = {{ locked|tojson }};
let A = {{ a|tojson }};
const MISMATCH = {{ MISMATCH|tojson }};
const NOC_COMPANY = {{ NOC_COMPANY|tojson }};
const NOC_LLP = {{ NOC_LLP|tojson }};
const ART = {{ ART|tojson }};
const SIU = {premise:"SIU OFFICE",street:"4-6",thoroughfare:"Greatorex Street",post_town:"London",county:"",postcode:"E1 5NF",country:"GB-ENG"};
const COUNTRIES = ["United Kingdom","England","Wales","Scotland","Northern Ireland","Ireland","United States","Pakistan","India","Bangladesh","United Arab Emirates","Saudi Arabia","Qatar","Nigeria","Ghana","Kenya","South Africa","Canada","Australia","New Zealand","Germany","France","Italy","Spain","Portugal","Netherlands","Belgium","Poland","Romania","Bulgaria","Turkey","China","Hong Kong","Singapore","Malaysia","Sri Lanka","Egypt","Morocco"];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const isLLP = () => ["LLP","LLPDES"].includes(A.company_type);
const hasShares = () => ["BYSHR","PLC"].includes(A.company_type);
const isGuar = () => ["BYGUAR","BYGUAREXEMPT"].includes(A.company_type);
function getP(path){return path.split(".").reduce((o,k)=>o==null?undefined:o[k],A);}
function setP(path,v){const ks=path.split(".");let o=A;for(let i=0;i<ks.length-1;i++){if(o[ks[i]]==null||typeof o[ks[i]]!=="object")o[ks[i]]={};o=o[ks[i]];}o[ks[ks.length-1]]=v;}
function inp(path,label,o={}){const v=getP(path)??"";const t=o.type||"text";const r=o.r?' data-r="1"':"";
  const list=o.list?' list="countries"':"";const ph=o.ph?` placeholder="${esc(o.ph)}"`:"";
  return `<div${o.span?' style="grid-column:span '+o.span+'"':''}><label>${label}</label><input type="${t}" data-p="${path}" value="${esc(v)}"${r}${list}${ph}></div>`;}
function sel(path,label,opts,o={}){const v=getP(path)??"";
  return `<div><label>${label}</label><select data-p="${path}"${o.r?' data-r="1"':''}>${Object.entries(opts).map(([k,t])=>`<option value="${esc(k)}"${k==v?" selected":""}>${esc(t)}</option>`).join("")}</select></div>`;}
function chk(path,label,o={}){return `<label class="chk"><input type="checkbox" data-p="${path}"${getP(path)?" checked":""}${o.r?' data-r="1"':''}> ${label}</label>`;}
function addrBox(path,o={}){const ro=o.ro;
  let h=`<div class="grid">${inp(path+".premise","Building name / number")}${inp(path+".street","Address line 1")}${inp(path+".thoroughfare","Address line 2")}${inp(path+".post_town","Town / city")}${inp(path+".county","County / region")}${inp(path+".postcode","Postcode")}`;
  if(ro){h+=sel(path+".country","Country",{"GB-ENG":"England","GB-WLS":"Wales","GB-SCT":"Scotland","GB-NIR":"Northern Ireland","GBR":"United Kingdom"});}
  else h+=inp(path+".country","Country",{list:1});
  return h+"</div>";}

function renderCompany(){
  let h="";
  if(!isLLP()){
    h+=`<div class="grid">${sel("articles","Articles of association",ART,{r:1})}`;
    h+=`<div><label>SIC codes (up to 4)</label><div style="display:flex;gap:4px">${[0,1,2,3].map(i=>`<input type="text" data-p="sic.${i}" value="${esc((A.sic||[])[i]||"")}" placeholder="${i?"":"e.g. 62020"}" style="width:25%">`).join("")}</div></div></div>`;
    h+=chk("restricted_articles","The articles contain restrictions on amendment (entrenched provisions)");
    h+=chk("cic","Community interest company (CIC) – upload CIC36",{r:1});
  }
  h+=chk("same_day","Same-day incorporation (£156, must reach CH by 3pm)");
  h+=chk("name_authorisation","The name contains a sensitive word – approval letter uploaded");
  h+=chk("same_name","The name is the 'same as' an existing name – consent letter uploaded");
  h+=`<div class="grid">${inp("reject_reference","Reject reference (only when re-sending a rejected filing)")}</div>`;
  document.getElementById("companyExtra").innerHTML=h;
  if(!A.ro) A.ro=Object.assign({},SIU);
  document.getElementById("roBox").innerHTML=addrBox("ro",{ro:1})+`<button class="btn small secondary" type="button" style="margin-top:8px" onclick="A.ro=Object.assign({},SIU);renderAll();queueSave()">Use SIU OFFICE address</button>`;
}

function renderClasses(){
  const card=document.getElementById("sharesCard");card.style.display=hasShares()?"":"none";
  if(!hasShares())return;
  A.share_classes=A.share_classes||[];
  document.getElementById("classes").innerHTML=A.share_classes.map((c,i)=>`<div class="person"><div class="grid">
    ${inp(`share_classes.${i}.cls`,"Class name",{r:1})}${inp(`share_classes.${i}.currency`,"Currency",{r:1})}${inp(`share_classes.${i}.nominal`,"Nominal value per share",{r:1})}</div>
    <label>Prescribed particulars (rights attached)</label><textarea data-p="share_classes.${i}.particulars" rows="2">${esc(c.particulars)}</textarea>
    ${A.share_classes.length>1?`<button class="btn small secondary" type="button" style="margin-top:6px" onclick="A.share_classes.splice(${i},1);renderAll();queueSave()">Remove class</button>`:""}</div>`).join("");
  // capital summary
  const cap={};(A.people||[]).forEach(p=>{if(!(p.roles||{}).subscriber)return;(p.shares||[]).forEach(s=>{
    const c=A.share_classes.find(x=>(x.cls||"").trim().toUpperCase()==(s.cls||"").trim().toUpperCase())||{};
    const cur=(c.currency||"GBP").toUpperCase();const n=parseInt(s.num||0)||0;const nv=parseFloat(c.nominal||0)||0;
    cap[cur]=cap[cur]||{n:0,agg:0};cap[cur].n+=n;cap[cur].agg+=n*nv;});});
  document.getElementById("capSummary").innerHTML="Statement of capital (worked out from the subscribers): "+(Object.keys(cap).length?Object.entries(cap).map(([k,v])=>`<b>${v.n}</b> shares, total nominal <b>${k} ${v.agg.toFixed(2).replace(/\.00$/,"")}</b>`).join(" · "):"no shares allotted yet");
}

function roleList(){
  if(isLLP()) return [["member","LLP member"],["designated","Designated member"],["psc","PSC"]];
  const r=[["director","Director"],["secretary","Secretary"],["psc","PSC"]];
  if(hasShares()) r.push(["subscriber","Subscriber (shareholder)"]);
  if(isGuar()) r.push(["guarantor","Guarantor (member)"]);
  return r;
}

function renderPeople(){
  document.getElementById("peopleTitle").textContent=isLLP()?"4. Members and PSCs":"4. Officers, PSCs and "+(isGuar()?"guarantors":"subscribers");
  const box=document.getElementById("people");A.people=A.people||[];
  box.innerHTML=A.people.map((p,i)=>personHtml(p,i)).join("")||'<p class="muted">No one added yet.</p>';
}

function personHtml(p,i){
  const b=`people.${i}`;const r=p.roles||{};const ind=p.kind==="person";
  const officer=(isLLP()?r.member:(r.director||r.secretary));
  const needDob=ind&&((isLLP()?r.member:r.director)||r.psc);
  let h=`<div class="person"><div style="display:flex;justify-content:space-between;gap:8px;align-items:center"><h3>${esc(personName(p))} <span class="muted" style="font-weight:400">${p.kind==="person"?"individual":p.kind==="corporate"?"company":"legal person"}</span></h3>
    <button class="btn small secondary" type="button" onclick="if(confirm('Remove this person?')){A.people.splice(${i},1);renderAll();queueSave()}">Remove</button></div>
    <div class="roles">${roleList().filter(([k])=>!(p.kind==="legal"&&k!=="psc")).map(([k,t])=>`<label class="${r[k]?"on":""}"><input type="checkbox" data-p="${b}.roles.${k}" data-r="1"${r[k]?" checked":""}> ${t}</label>`).join("")}</div>`;
  if(ind){
    h+=`<div class="grid">${inp(b+".title","Title")}${inp(b+".forename","First name",{r:0})}${inp(b+".other_forenames","Middle names")}${inp(b+".surname","Surname")}</div>`;
    if(needDob||r.psc||(r.director&&!isLLP())) h+=`<div class="grid">${inp(b+".dob","Date of birth",{type:"date"})}${(!isLLP()&&(r.director||r.psc))||r.psc?inp(b+".nationality","Nationality",{ph:"e.g. British"}):""}${inp(b+".residence_country","Country of residence",{list:1})}</div>`;
    if(r.director||r.secretary||r.member) h+=`<div class="grid">${inp(b+".former_names","Former names (optional, 'First Surname; …')",{span:2})}</div>`;
    if(officer||r.psc){
      h+=`<div class="sub"><h4>Identity verification</h4><div class="grid">${inp(b+".personal_code","Companies House personal code (11 characters)"+(r.psc&&!officer?" – optional for a PSC":""),{ph:"e.g. ABC12345678"})}${sel(b+".name_mismatch","Name differs from the verified ID?",MISMATCH)}</div></div>`;
    }
    h+=`<div class="sub"><h4>Correspondence (service) address – public</h4>${chk(b+".service_same_ro","Same as the registered office",{r:1})}`;
    if(!p.service_same_ro) h+=addrBox(b+".service");
    h+=`</div>`;
    if(needDob){
      h+=`<div class="sub"><h4>Home address – not public</h4>${chk(b+".res_same_service",p.service_same_ro?"Lives at the registered office address":"Same as the correspondence address",{r:1})}`;
      if(!p.res_same_service) h+=addrBox(b+".res");
      h+=`</div>`;
    }
  } else {
    h+=`<div class="grid">${inp(b+".corp_name",p.kind==="legal"?"Name of the legal person":"Company name",{span:2})}</div>`;
    h+=`<div class="sub"><h4>Registered / principal office address</h4>${addrBox(b+".corp_addr")}</div>`;
    h+=`<div class="sub"><h4>Identification</h4>`;
    if(p.kind==="corporate"&&officer) h+=chk(b+".reg_uk","Registered in the UK (Companies House)",{r:1});
    h+=`<div class="grid">`;
    if(p.kind==="corporate"){h+=inp(b+".reg_no","Registration number");h+=inp(b+".place_registered","Register / place registered");}
    if(p.kind==="legal"||!(officer&&p.reg_uk)||r.psc){h+=inp(b+".law","Governing law",{ph:"e.g. Companies Act 2006"})+inp(b+".legal_form","Legal form",{ph:"e.g. Private limited company"});}
    if(p.kind==="corporate"&&r.psc) h+=inp(b+".country_or_state","Country / state registered");
    h+=`</div></div>`;
  }
  if(officer) h+=chk(b+".consent",(isLLP()?"Consents to act as LLP member":r.director&&r.secretary?"Consents to act as director and secretary":r.director?"Consents to act as director":"Consents to act as secretary"));
  if(r.psc){
    const N=isLLP()?NOC_LLP:NOC_COMPANY;const cur=p.noc||[];
    h+=`<div class="sub"><h4>PSC – nature of control</h4><div class="noc">${Object.entries(N).filter(([k])=>!/_AS_(TRUST|FIRM)$/.test(k)||cur.includes(k)||p.show_more).map(([k,t])=>`<label><input type="checkbox" data-noc="${i}" value="${k}"${cur.includes(k)?" checked":""}> ${esc(t)}</label>`).join("")}</div>
      ${p.show_more?"":`<a href="#" class="muted" onclick="A.people[${i}].show_more=1;renderAll();return false">show 'as trustee' / 'as member of a firm' options</a>`}`;
    if(ind) h+=chk(b+".psc_consent","The PSC has confirmed these details are correct (PSC consent statement)");
    h+=`</div>`;
  }
  if(r.subscriber&&hasShares()){
    p.shares=p.shares||[];
    h+=`<div class="sub"><h4>Shares taken on formation</h4><table class="mini"><tr><th>Class</th><th>Number</th><th>Paid per share</th><th>Unpaid per share</th><th></th></tr>`+
      p.shares.map((s,j)=>`<tr><td><select data-p="${b}.shares.${j}.cls" data-r="1">${(A.share_classes||[]).map(c=>`<option${(c.cls||"").toUpperCase()==(s.cls||"").toUpperCase()?" selected":""}>${esc(c.cls)}</option>`).join("")}</select></td>
      <td><input type="number" min="1" data-p="${b}.shares.${j}.num" data-r="1" value="${esc(s.num)}" style="width:110px"></td>
      <td><input type="text" data-p="${b}.shares.${j}.paid" value="${esc(s.paid)}" placeholder="= nominal" style="width:110px"></td>
      <td><input type="text" data-p="${b}.shares.${j}.unpaid" value="${esc(s.unpaid)}" placeholder="0" style="width:110px"></td>
      <td><button class="btn small secondary" type="button" onclick="A.people[${i}].shares.splice(${j},1);renderAll();queueSave()">×</button></td></tr>`).join("")+
      `</table><button class="btn small secondary" type="button" style="margin-top:6px" onclick="addShares(${i})">+ Shares</button></div>`;
  }
  if(r.guarantor&&isGuar()) h+=`<div class="sub"><h4>Guarantee</h4><div class="grid">${inp(b+".guarantee","Amount guaranteed (£)")}</div></div>`;
  return h+"</div>";
}
function personName(p){if(p.kind!=="person")return p.corp_name||"(new company)";return [p.forename,p.other_forenames,p.surname].filter(Boolean).join(" ")||"(new person)";}
function blankPerson(kind){return {id:Math.random().toString(16).slice(2,10),kind,title:"",forename:"",other_forenames:"",surname:"",dob:"",nationality:"",residence_country:"United Kingdom",service_same_ro:kind==="person",service:{},res_same_service:false,res:{},personal_code:"",name_mismatch:"",former_names:"",corp_name:"",corp_addr:{},reg_uk:kind==="corporate",reg_no:"",place_registered:"",law:"",legal_form:"",country_or_state:"",roles:{director:false,secretary:false,member:false,designated:false,psc:false,subscriber:false,guarantor:false},consent:false,psc_consent:false,noc:[],shares:[],guarantee:"1"};}
function addPerson(kind){const p=blankPerson(kind);if(kind==="legal")p.roles.psc=true;A.people.push(p);renderAll();queueSave();}
function addClass(){A.share_classes.push({cls:"",currency:"GBP",nominal:"1",particulars:""});renderAll();queueSave();}
function addShares(i){const c=(A.share_classes[0]||{}).cls||"ORDINARY";A.people[i].shares.push({cls:c,num:"",paid:"",unpaid:""});renderAll();queueSave();}

function renderAll(){
  const y=window.scrollY;
  renderCompany();renderClasses();renderPeople();
  document.querySelectorAll("#form [data-p]").forEach(el=>{const v=getP(el.dataset.p);
    if(el.type==="checkbox")el.checked=!!v;else if(el.tagName==="SELECT"){if(v!=null&&v!=="")el.value=v;}else if(el.tagName==="TEXTAREA"){el.value=v??"";}else el.value=v??"";
    if(LOCKED)el.disabled=true;});
  window.scrollTo(0,y);
}
document.getElementById("form").addEventListener("input",onEdit);
document.getElementById("form").addEventListener("change",onEdit);
function onEdit(e){const el=e.target;
  if(el.dataset.noc!==undefined){const p=A.people[+el.dataset.noc];p.noc=p.noc||[];if(el.checked){if(!p.noc.includes(el.value))p.noc.push(el.value);}else p.noc=p.noc.filter(x=>x!==el.value);queueSave();return;}
  if(!el.dataset.p)return;
  let v=el.type==="checkbox"?el.checked:el.value;
  setP(el.dataset.p,v);
  if(el.dataset.p.startsWith("sic.")){A.sic=(A.sic||[]).map(x=>x||"");}
  if(el.dataset.r&&e.type==="change")renderAll();
  else if(el.dataset.r&&el.type==="checkbox")renderAll();
  queueSave();}
let tmr=null;
function queueSave(){if(LOCKED)return;document.getElementById("saveMsg").textContent="Saving…";clearTimeout(tmr);tmr=setTimeout(saveNow,700);}
async function saveNow(){if(LOCKED)return;try{const r=await fetch(`/formations/${JOB}/save`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(A)});const j=await r.json();
  if(!j.ok){document.getElementById("saveMsg").textContent=j.error||"Not saved";return;}
  document.getElementById("saveMsg").textContent="Saved "+new Date().toLocaleTimeString("en-GB");showProbs(j.errors,j.warnings);}catch(e){document.getElementById("saveMsg").textContent="Not saved – "+e;}}
document.getElementById("saveBtn").onclick=saveNow;
function showProbs(errs,warns){
  document.getElementById("probs").innerHTML=(errs.length?`<ul class="errs">${errs.map(e=>`<li>${esc(e)}</li>`).join("")}</ul>`:'<p style="color:#166534;font-weight:600">✓ No problems found in the details.</p>')+(warns.length?`<ul class="warns">${warns.map(e=>`<li>${esc(e)}</li>`).join("")}</ul>`:"");
  document.getElementById("probCount").innerHTML=errs.length?`<span class="pill hold">${errs.length} problem${errs.length>1?"s":""} to fix</span>`:'<span class="pill active">Ready to check</span>';}
async function fullCheck(){await saveNow();const box=document.getElementById("checkRes");box.textContent="Checking…";
  const j=await (await fetch(`/formations/${JOB}/check`)).json();
  if(j.errors.length){box.innerHTML='<p class="muted">Fix the problems listed above first.</p>';return;}
  box.innerHTML=j.schema.length?`<div class="flash"><b>The official schema check found:</b><ul>${j.schema.map(e=>`<li>${esc(e)}</li>`).join("")}</ul></div>`:'<p style="color:#166534;font-weight:700">✓ Passed the official Companies House schema check – ready to send.</p>';}
async function nameCheck(){const n=A.company_name||"";const box=document.getElementById("nameRes");if(!n){box.textContent="Type a name first.";return;}box.textContent="Searching the register…";
  const j=await (await fetch(`/formations/${JOB}/namecheck?name=${encodeURIComponent(n)}`)).json();
  if(j.error){box.textContent=j.error;return;}
  box.innerHTML=(j.clash?'<b style="color:#b91c1c">⚠ A company with the same name (ignoring Ltd, punctuation, "the" etc.) already exists – Companies House will reject it.</b>':'<b style="color:#166534">No identical name found.</b> Similar names may still be refused as "too like" – check the list.')+
    (j.items.length?"<br>"+j.items.slice(0,8).map(x=>`${x.same?"<b>":""}${esc(x.title)} (${esc(x.number)}, ${esc(x.status)})${x.same?"</b>":""}`).join("<br>"):"");}
async function aiRead(){const box=document.getElementById("aiMsg");box.textContent="Reading the documents… (up to a minute)";
  const j=await (await fetch(`/formations/${JOB}/read`,{method:"POST"})).json();
  if(!j.ok){box.textContent=j.error;return;}
  const d=j.data;let n=0;
  if(d.company_name&&!A.company_name){A.company_name=d.company_name;n++;}
  if(d.company_type&&!A.people.some(p=>p.surname||p.corp_name)){A.company_type=d.company_type;}
  if(d.registered_email&&!A.registered_email)A.registered_email=d.registered_email;
  if((d.sic_codes||[]).filter(Boolean).length&&!(A.sic||[]).filter(Boolean).length)A.sic=d.sic_codes.filter(Boolean).slice(0,4);
  A.people=A.people.filter(p=>p.surname||p.corp_name);
  (d.people||[]).forEach(x=>{const ex=A.people.find(p=>(p.surname||"").toUpperCase()===(x.surname||"").toUpperCase()&&(p.forename||"").toUpperCase()===(x.forename||"").toUpperCase());
    const p=ex||blankPerson("person");["title","forename","other_forenames","surname","dob","nationality","residence_country","personal_code"].forEach(k=>{if(x[k]&&!p[k])p[k]=x[k];});
    if(x.residence_country&&p.residence_country==="United Kingdom"&&!ex)p.residence_country=x.residence_country;
    const ha=x.home_address||{};if(Object.values(ha).some(Boolean)&&!Object.values(p.res||{}).some(Boolean)){p.res=Object.assign({},ha);p.res_same_service=false;}
    (x.roles||[]).forEach(rk=>{if(rk in p.roles)p.roles[rk]=true;});
    if(p.roles.subscriber&&x.shares&&!p.shares.length)p.shares.push({cls:(A.share_classes[0]||{}).cls||"ORDINARY",num:String(x.shares).replace(/\D/g,""),paid:"",unpaid:""});
    if(p.roles.psc&&x.ownership_pct&&!p.noc.length){const v=parseFloat(x.ownership_pct);const band=v>75?"75TO100":v>50?"50TO75":v>=25?"25TO50":"";
      if(band){p.noc.push(isLLP()?`RIGHTTOSHARESURPLUSASSETS_${band}PERCENT`:`OWNERSHIPOFSHARES_${band}PERCENT`);p.noc.push(`VOTINGRIGHTS_${band}PERCENT`);}}
    if(!ex)A.people.push(p);n++;});
  if(!A.people.length)A.people.push(blankPerson("person"));
  renderAll();await saveNow();
  box.innerHTML=`Filled ${n} item(s) – <b>check everything</b> (blue boxes are not used; the AI only fills empty fields).`+(d.notes?`<br>AI notes: ${esc(d.notes)}`:"");}
function beforeSend(f){const mode=(document.activeElement||{}).value;if(mode==="live")return confirm("This is a REAL incorporation. The fee is charged to the credit account. Continue?");return true;}
document.body.insertAdjacentHTML("beforeend",`<datalist id="countries">${COUNTRIES.map(c=>`<option value="${c}">`).join("")}</datalist>`);
renderAll();
showProbs({{ errs|tojson }},{{ warns|tojson }});
</script>
{% endblock %}
"""
