"""
UK Tax tab (v44a) – year-end UK filings for clients.

  Companies House (filed DIRECT with our presenter account – XML Gateway, same account as UK Formations):
    * Dormant company accounts (s480, FRS 105 / FRS 102 1A layout)      – form "Accounts" (iXBRL document)
    * Micro-entity accounts (FRS 105)                                     – form "Accounts" (iXBRL document)
    * Small company accounts – filleted balance sheet (FRS 102 s1A)       – form "Accounts" (iXBRL document)
    * Confirmation statement (CS01) for companies and LLPs                – form "ConfirmationStatement" v1-3
  HMRC (prepared + tracked here, filed with your current HMRC software until we have HMRC filing credentials):
    * Corporation Tax return CT600 – computation (19% / marginal relief / 25%), due dates, stages
    * Partnership return SA800 – profit split between partners, due dates, stages
    * Self Assessment SA100 – income tax + Class 4 estimate, due dates, payments on account, stages

Self-contained add-on: app.py only imports this file and calls register(app, load_config, send_email).
Uses the gateway code of formations.py (envelope, presenter credentials, shared submission-number counter).

Secrets – Railway → Variables only (the repo is public):
  CH_PRESENTER_ID / CH_PRESENTER_CODE, CH_TEST_PRESENTER_ID / CH_TEST_PRESENTER_CODE  (already set for UK Formations)
  CH_API_KEY        Companies House REST API key – "Load from Companies House" (name, due dates, directors, SIC)
  UKTAX_NO_POLL=1   switches off the 10-minute Companies House status check for this tab
"""
from __future__ import annotations

import base64, calendar, csv, datetime as dt, html, io, json, os, re, threading, time, urllib.parse, urllib.request, uuid, zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_DOWN
from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template_string, request, send_file, Response

import formations as fm

try:
    from lxml import etree
except Exception:  # pragma: no cover
    etree = None

VERSION = "v44a"
ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "uktax"
SETTINGS_FILE = OUT / "settings.json"
CS_XSD = "ConfirmationStatement-v1-3.xsd"
LOC_CS = fm.NS_CH + " http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/" + CS_XSD

_LOCK = threading.RLock()
_CFG = {"fn": None}
_SEND = {"fn": None}
_POLL = {"started": False}

bp = Blueprint("uktax", __name__)

SETTINGS_DEFAULT = {
    "taxonomy": "2025",             # FRC taxonomy year used in the accounts (2024 / 2025 / 2026)
    "accounts_live_approved": "",   # CH has tested + approved our software for "Accounts"
    "cs_live_approved": "",         # CH has tested + approved our software for "ConfirmationStatement"
    "notify_email": "accounting@startitup.global",
    "firm_name": "Startitup Global",
    "firm_email": "accounting@startitup.global",
}

KINDS = {"company": "Limited company", "llp": "LLP", "partnership": "Partnership (SA800)", "individual": "Individual (SA100)"}

FILING_TYPES = {
    "ACC_DORMANT": {"label": "Dormant accounts", "auth": "CH", "group": "ch", "for": ("company",)},
    "ACC_MICRO": {"label": "Micro-entity accounts", "auth": "CH", "group": "ch", "for": ("company",)},
    "ACC_SMALL": {"label": "Small company accounts (filleted)", "auth": "CH", "group": "ch", "for": ("company",)},
    "CS01": {"label": "Confirmation statement (CS01)", "auth": "CH", "group": "ch", "for": ("company", "llp")},
    "CT600": {"label": "Corporation Tax return (CT600)", "auth": "HMRC", "group": "ct", "for": ("company",)},
    "SA800": {"label": "Partnership return (SA800)", "auth": "HMRC", "group": "sa800", "for": ("partnership", "llp")},
    "SA100": {"label": "Self Assessment (SA100)", "auth": "HMRC", "group": "sa100", "for": ("individual",)},
}
GROUPS = {"due": "Due soon", "ch": "Companies House", "ct": "Corporation Tax", "sa800": "Partnerships (SA800)",
          "sa100": "Self Assessment (SA100)", "all": "All filings", "clients": "Clients"}

CH_STATUS = {"draft": "Draft", "submitted": "Sent – waiting", "pending": "With Companies House", "parked": "Parked by CH",
             "accepted": "Accepted", "rejected": "Rejected", "error": "Error – not sent", "filed_elsewhere": "Filed elsewhere",
             "not_needed": "Not needed"}
HMRC_STAGES = {"info_requested": "Info requested", "info_received": "Info received", "prepared": "Prepared",
               "sent_to_client": "Sent to client to approve", "approved": "Client approved", "filed": "Filed with HMRC",
               "accepted": "HMRC accepted", "not_needed": "Not needed"}
DONE = {"accepted", "filed", "filed_elsewhere", "not_needed"}


# ----------------------------------------------------------------------------- small helpers

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


def D(v, default="0") -> Decimal:
    v = str(v if v is not None else "").replace(",", "").replace("£", "").strip()
    if v.startswith("(") and v.endswith(")"):
        v = "-" + v[1:-1]
    try:
        return Decimal(v) if v not in ("", "-") else Decimal(default)
    except InvalidOperation:
        return Decimal(default)


def whole(d: Decimal) -> Decimal:
    return d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def money(d, dp=0) -> str:
    d = D(d)
    q = Decimal("1") if dp == 0 else Decimal("0.01")
    d = d.quantize(q, rounding=ROUND_HALF_UP)
    s = f"{abs(d):,.{dp}f}"
    return f"({s})" if d < 0 else s


def pdate(v) -> dt.date | None:
    if isinstance(v, dt.date):
        return v
    v = (v or "").strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(v, f).date()
        except ValueError:
            pass
    return None


def iso(d) -> str:
    d = pdate(d)
    return d.isoformat() if d else ""


def long_date(d) -> str:
    d = pdate(d)
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


def add_months(d: dt.date, n: int, keep_month_end=True) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    last = calendar.monthrange(y, m)[1]
    if keep_month_end and d.day == calendar.monthrange(d.year, d.month)[1]:
        return dt.date(y, m, last)
    return dt.date(y, m, min(d.day, last))


def tax_year_end(ty: str) -> dt.date | None:
    """'2025-26' -> 5 April 2026"""
    mm = re.match(r"^(\d{4})", ty or "")
    return dt.date(int(mm.group(1)) + 1, 4, 5) if mm else None


def current_sa_year(today=None) -> str:
    """The tax year whose return is being prepared now (ended last 5 April)."""
    today = today or dt.date.today()
    y = today.year - 1 if today >= dt.date(today.year, 4, 6) else today.year - 2
    return f"{y}-{str(y + 1)[2:]}"


def is_llp_number(n: str) -> bool:
    return (n or "").upper()[:2] in ("OC", "SO", "NC")


def company_type_code(n: str) -> str:
    n = (n or "").upper()
    for p in ("OC", "SO", "NC", "SC", "NI"):
        if n.startswith(p):
            return p
    return "R" if n.startswith("R") else "EW"


def number_digits(n: str) -> str:
    return re.sub(r"\D", "", n or "")


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


# ----------------------------------------------------------------------------- storage

def cdir() -> Path:
    p = OUT / "clients"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _chk(cid: str):
    if not re.fullmatch(r"[0-9a-f]{12}", cid or ""):
        abort(404)


def load_client(cid: str) -> dict:
    _chk(cid)
    p = cdir() / f"{cid}.json"
    if not p.exists():
        abort(404)
    return json.loads(p.read_text())


def save_client(c: dict):
    p = cdir() / f"{c['id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=2))
    tmp.replace(p)


def list_clients() -> list[dict]:
    out = []
    for p in cdir().glob("*.json"):
        try:
            c = json.loads(p.read_text())
            if isinstance(c, dict) and c.get("id") and isinstance(c.get("filings"), list):
                out.append(c)
        except Exception:
            pass
    out.sort(key=lambda c: (c.get("name") or "").lower())
    return out


def fdir(c: dict, f: dict) -> Path:
    p = OUT / "files" / c["id"] / f["id"]
    p.mkdir(parents=True, exist_ok=True)
    return p


def log(f: dict, text: str):
    f.setdefault("log", []).append({"t": dt.datetime.now().isoformat(timespec="seconds"), "text": text})


def get_filing(c: dict, fid: str) -> dict:
    for f in c["filings"]:
        if f["id"] == fid:
            return f
    abort(404)


def new_client(kind: str) -> dict:
    return {"id": uuid.uuid4().hex[:12], "kind": kind if kind in KINDS else "company", "name": "", "company_number": "",
            "auth_code": "", "utr": "", "ni_number": "", "email": "", "phone": "", "contact": "", "address": "",
            "registered_email": "", "incorporated": "", "ard": "", "accounts_next_start": "", "accounts_next_end": "",
            "accounts_due": "", "cs_next_date": "", "cs_due": "", "sic": "", "officers": [], "partners": [],
            "ch_status": "", "notes": "", "created": dt.datetime.now().isoformat(timespec="seconds"), "filings": []}


# ----------------------------------------------------------------------------- Companies House REST API

def ch_get(path: str) -> dict:
    key = os.environ.get("CH_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CH_API_KEY is not set in Railway → Variables.")
    req = urllib.request.Request("https://api.company-information.service.gov.uk" + path)
    req.add_header("Authorization", "Basic " + base64.b64encode((key + ":").encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def officer_name(raw: str) -> dict:
    """'SMITH, John Paul' -> forename/surname"""
    if "," in raw:
        sur, fore = raw.split(",", 1)
        return {"surname": sur.strip().title(), "forename": fore.strip().title()}
    parts = raw.split()
    return {"surname": parts[-1].title() if parts else raw, "forename": " ".join(parts[:-1]).title()}


def apply_ch_profile(c: dict, co: dict, officers: list[dict]):
    c["company_number"] = co.get("company_number", c.get("company_number", ""))
    c["name"] = co.get("company_name") or c.get("name", "")
    c["ch_status"] = co.get("company_status", "")
    c["incorporated"] = co.get("date_of_creation", c.get("incorporated", ""))
    if (co.get("type") or "").startswith("llp") or is_llp_number(c["company_number"]):
        c["kind"] = "llp"
    acc = co.get("accounts") or {}
    ard = acc.get("accounting_reference_date") or {}
    if ard.get("day") and ard.get("month"):
        c["ard"] = f"{int(ard['day']):02d}/{int(ard['month']):02d}"
    c["accounts_next_end"] = acc.get("next_made_up_to") or (acc.get("next_accounts") or {}).get("period_end_on", "")
    c["accounts_next_start"] = (acc.get("next_accounts") or {}).get("period_start_on", "")
    if not c["accounts_next_start"]:
        last = pdate((acc.get("last_accounts") or {}).get("made_up_to"))
        c["accounts_next_start"] = iso(last + dt.timedelta(days=1)) if last else c["incorporated"]
    c["accounts_due"] = acc.get("next_due") or (acc.get("next_accounts") or {}).get("due_on", "")
    cs = co.get("confirmation_statement") or {}
    c["cs_next_date"] = cs.get("next_made_up_to", "")
    c["cs_due"] = cs.get("next_due", "")
    c["sic"] = ", ".join(co.get("sic_codes") or [])
    ro = co.get("registered_office_address") or {}
    c["address"] = ", ".join(x for x in (ro.get("premises"), ro.get("address_line_1"), ro.get("address_line_2"),
                                         ro.get("locality"), ro.get("postal_code")) if x)
    offs = []
    for o in officers:
        if o.get("resigned_on"):
            continue
        role = o.get("officer_role", "")
        if role in ("director", "llp-member", "llp-designated-member", "corporate-director", "secretary"):
            nm = officer_name(o.get("name", ""))
            nm["role"] = role
            offs.append(nm)
    if offs:
        c["officers"] = offs
    c["ch_loaded"] = dt.datetime.now().isoformat(timespec="seconds")


def load_from_ch(c: dict) -> str:
    n = (c.get("company_number") or "").strip().upper()
    if not n:
        return "Enter the company number first."
    if n.isdigit():
        n = n.zfill(8)
    try:
        co = ch_get(f"/company/{urllib.parse.quote(n)}")
        try:
            offs = ch_get(f"/company/{urllib.parse.quote(n)}/officers?items_per_page=100").get("items", [])
        except Exception:
            offs = []
    except Exception as ex:
        return f"Companies House lookup failed: {ex}"
    apply_ch_profile(c, co, offs)
    return f"Loaded {c['name']} from Companies House."


# ----------------------------------------------------------------------------- deadlines + filings

def accounts_due(c: dict, period_end: dt.date | None) -> dt.date | None:
    if not period_end:
        return None
    ch_end, ch_due = pdate(c.get("accounts_next_end")), pdate(c.get("accounts_due"))
    if ch_end == period_end and ch_due:
        return ch_due
    inc = pdate(c.get("incorporated"))
    if inc and (period_end - inc).days < 550:  # first accounts: 21 months after incorporation (or 3m after ARD if later)
        return max(add_months(inc, 21, keep_month_end=False), add_months(period_end, 3))
    return add_months(period_end, 9)


def new_filing(c: dict, ftype: str) -> dict:
    f = {"id": uuid.uuid4().hex[:8], "type": ftype, "created": dt.datetime.now().isoformat(timespec="seconds"),
         "status": "draft" if FILING_TYPES[ftype]["auth"] == "CH" else "info_requested", "d": {}, "log": []}
    d = f["d"]
    if ftype.startswith("ACC_"):
        d.update(start=c.get("accounts_next_start", ""), end=c.get("accounts_next_end", ""),
                 approved=dt.date.today().isoformat(), employees="0", first_year="",
                 signer=(c.get("officers") or [{}])[0].get("forename", "") + " " + (c.get("officers") or [{}])[0].get("surname", ""),
                 trading_status="never" if ftype == "ACC_DORMANT" else "")
        for k in ("fixed", "cash", "debtors", "unpaid_capital", "cred1", "cred2", "provisions", "share_capital", "other_reserves"):
            d[k] = ""
            d["py_" + k] = ""
    elif ftype == "CS01":
        d.update(review=c.get("cs_next_date", ""), sic_change="", sic=c.get("sic", ""), email_change="",
                 registered_email=c.get("registered_email", ""), capital_change="", currency="GBP",
                 classes=[{"cls": "ORDINARY", "num": "", "nominal": "1", "unpaid": "0",
                           "particulars": fm.DEFAULT_PARTICULARS}],
                 holders=[{"name": "", "corporate": "", "cls": "ORDINARY", "num": "", "transfer_date": "", "transfer_num": ""}])
    elif ftype == "CT600":
        d.update(start=c.get("accounts_next_start", ""), end=c.get("accounts_next_end", ""), associated="0",
                 profit="", depreciation="", disallowable="", other_addbacks="", capital_allowances="", other_deductions="",
                 losses_bf="", income_other="", ct_paid="")
    elif ftype == "SA800":
        y = current_sa_year()
        d.update(tax_year=y, period_start=f"{y[:4]}-04-06", period_end=f"{int(y[:4]) + 1}-04-05", turnover="", expenses="",
                 profit="", other_income="", partners=[{"name": p.get("name", ""), "utr": p.get("utr", ""), "share": p.get("share", "")}
                                                       for p in (c.get("partners") or [])] or [{"name": "", "utr": "", "share": ""}])
    elif ftype == "SA100":
        d.update(tax_year=current_sa_year(), employment="", paye="", self_employment="", property="", savings="",
                 dividends="", pension_gross="", other="", poa_paid="", student_loan="")
    refresh_due(c, f)
    return f


def period_label(f: dict) -> str:
    d = f.get("d", {})
    if f["type"] in ("SA100", "SA800"):
        return d.get("tax_year", "")
    if f["type"] == "CS01":
        return "Review " + (pdate(d.get("review")) and pdate(d.get("review")).strftime("%d/%m/%Y") or "?")
    s, e = pdate(d.get("start")), pdate(d.get("end"))
    return (s.strftime("%d/%m/%Y") if s else "?") + " – " + (e.strftime("%d/%m/%Y") if e else "?")


def refresh_due(c: dict, f: dict):
    d, t = f["d"], f["type"]
    due, extra = None, []
    if t.startswith("ACC_"):
        due = accounts_due(c, pdate(d.get("end")))
    elif t == "CS01":
        r = pdate(d.get("review"))
        due = r + dt.timedelta(days=14) if r else None
    elif t == "CT600":
        e = pdate(d.get("end"))
        if e:
            due = add_months(e, 12)
            extra.append(("Tax payment", add_months(e, 9, keep_month_end=False) + dt.timedelta(days=1)))
    elif t in ("SA100", "SA800"):
        ye = tax_year_end(d.get("tax_year"))
        if ye:
            due = dt.date(ye.year + 1, 1, 31)
            extra.append(("Paper deadline", dt.date(ye.year, 10, 31)))
            if t == "SA100":
                extra.append(("Balancing payment + 1st payment on account", due))
                extra.append(("2nd payment on account", dt.date(ye.year + 1, 7, 31)))
    f["due"] = due.isoformat() if due else ""
    f["other_dates"] = [[k, v.isoformat()] for k, v in extra]


def days_left(f: dict) -> int | None:
    d = pdate(f.get("due"))
    return (d - dt.date.today()).days if d else None


def is_done(f: dict) -> bool:
    return f.get("status") in DONE


def all_rows() -> list[dict]:
    rows = []
    for c in list_clients():
        for f in c["filings"]:
            rows.append({"c": c, "f": f, "days": days_left(f), "group": FILING_TYPES[f["type"]]["group"]})
    rows.sort(key=lambda r: (is_done(r["f"]), r["f"].get("due") or "9999"))
    return rows


def alert_count() -> int:
    n = 0
    for r in all_rows():
        f = r["f"]
        if f.get("status") in ("rejected", "error", "parked"):
            n += 1
        elif not is_done(f) and r["days"] is not None and r["days"] <= 21:
            n += 1
    return n


def suggested_filings(c: dict) -> list[str]:
    base = {"company": ["ACC_MICRO", "CS01", "CT600"], "llp": ["CS01", "SA800"], "partnership": ["SA800"],
            "individual": ["SA100"]}[c["kind"]]
    open_types = {("ACC" if f["type"].startswith("ACC_") else f["type"]) for f in c["filings"] if not is_done(f)}
    return [t for t in base if ("ACC" if t.startswith("ACC_") else t) not in open_types]


# ----------------------------------------------------------------------------- accounts (iXBRL)

ACC_LINES = [  # key, label, sign (+ asset / - liability)
    ("unpaid_capital", "Called up share capital not paid", 1),
    ("fixed", "Fixed assets", 1),
    ("debtors", "Debtors", 1),
    ("cash", "Cash at bank and in hand", 1),
    ("cred1", "Creditors: amounts falling due within one year", -1),
    ("cred2", "Creditors: amounts falling due after more than one year", -1),
    ("provisions", "Provisions for liabilities", -1),
    ("share_capital", "Called up share capital", 0),
]


def acc_figures(d: dict, prefix="") -> dict:
    g = {k: whole(D(d.get(prefix + k))) for k, _, _ in ACC_LINES}
    g["other_reserves"] = whole(D(d.get(prefix + "other_reserves")))
    g["current"] = g["debtors"] + g["cash"]
    g["ncl"] = g["current"] - g["cred1"]
    g["talcl"] = g["unpaid_capital"] + g["fixed"] + g["ncl"]
    g["net"] = g["talcl"] - g["cred2"] - g["provisions"]
    g["reserves"] = g["net"] - g["share_capital"] - g["other_reserves"]   # profit and loss account balances the sheet
    g["equity"] = g["net"]
    return g


def has_prior(d: dict) -> bool:
    return not d.get("first_year") and any(str(d.get("py_" + k, "")).strip() for k, _, _ in ACC_LINES)


def acc_checks(c: dict, f: dict) -> tuple[list[str], list[str]]:
    d, t = f["d"], f["type"]
    errs, warns = [], []
    if c["kind"] != "company":
        errs.append("Accounts filing here is for limited companies only (LLP accounts are not built yet).")
    if not c.get("company_number"):
        errs.append("Company number is missing.")
    if not re.fullmatch(r"[A-Za-z0-9]{6,8}", c.get("auth_code") or ""):
        errs.append("Company authentication code (6–8 characters, from the company's Companies House letter) is missing.")
    s, e, ap = pdate(d.get("start")), pdate(d.get("end")), pdate(d.get("approved"))
    if not (s and e) or s >= e:
        errs.append("Period start and end dates are needed (start before end).")
    elif (e - s).days > 549:
        errs.append("The period is longer than 18 months.")
    if e and e > dt.date.today():
        errs.append("The period has not ended yet.")
    if not ap:
        errs.append("Date the directors approved the accounts is needed.")
    elif e and ap <= e:
        errs.append("Approval date must be after the balance sheet date.")
    elif ap > dt.date.today():
        errs.append("Approval date cannot be in the future.")
    if not (d.get("signer") or "").strip():
        errs.append("Name of the director signing the balance sheet is needed.")
    g = acc_figures(d)
    if g["share_capital"] <= 0:
        warns.append("Called up share capital is 0 – check the number of shares issued (usually £1 or £100).")
    if t == "ACC_DORMANT":
        if g["fixed"] or g["cred2"] or g["provisions"]:
            warns.append("A dormant company normally has no fixed assets, long-term creditors or provisions – check.")
        if g["reserves"] != 0:
            warns.append(f"Profit and loss reserve is {money(g['reserves'])} – a company that has been dormant since it was "
                         "formed has none. If it traded in the past choose 'Dormant, traded in the past'.")
        if d.get("trading_status") == "never" and g["reserves"] != 0:
            errs.append("'Never traded' but the balance sheet shows profit or loss reserves – change the trading status or figures.")
    if not d.get("first_year") and not has_prior(d):
        warns.append("No comparative (previous year) figures – tick 'First accounts' if this is the first period.")
    if t != "ACC_DORMANT" and not str(d.get("employees", "")).strip():
        errs.append("Average number of employees (including directors) is required.")
    return errs, warns


def taxonomy_ns(year: str) -> dict:
    y = year if year in ("2024", "2025", "2026") else "2025"
    v = f"{y}-01-01"
    return {"entry": f"https://xbrl.frc.org.uk/FRS-102/{v}/FRS-102-{v}.xsd",
            "core": f"http://xbrl.frc.org.uk/fr/{v}/core", "bus": f"http://xbrl.frc.org.uk/cd/{v}/business",
            "direp": f"http://xbrl.frc.org.uk/reports/{v}/direp"}


def build_ixbrl(c: dict, f: dict, s: dict) -> str:
    d, t = f["d"], f["type"]
    ns = taxonomy_ns(s.get("taxonomy", "2025"))
    num = (c.get("company_number") or "").upper()
    s_, e_ = pdate(d.get("start")), pdate(d.get("end"))
    ap = pdate(d.get("approved"))
    prior = has_prior(d)
    pe = s_ - dt.timedelta(days=1) if s_ else None
    ps = add_months(s_, -12) if s_ else None
    g, gp = acc_figures(d), acc_figures(d, "py_")
    dormant = t == "ACC_DORMANT"
    micro = t in ("ACC_MICRO", "ACC_DORMANT")
    name = c.get("name", "")
    signer = (d.get("signer") or "").strip()
    std_member = "bus:Micro-entities" if micro else "bus:SmallEntities"

    ctx = []

    def context(cid, period, dims=()):
        dims_xml = "".join(f'<xbrldi:explicitMember dimension="{dm}">{mb}</xbrldi:explicitMember>' for dm, mb in dims)
        seg = f"<xbrli:segment>{dims_xml}</xbrli:segment>" if dims else ""
        per = (f"<xbrli:instant>{period}</xbrli:instant>" if isinstance(period, str)
               else f"<xbrli:startDate>{period[0]}</xbrli:startDate><xbrli:endDate>{period[1]}</xbrli:endDate>")
        ctx.append(f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">'
                   f'{esc(num)}</xbrli:identifier>{seg}</xbrli:entity><xbrli:period>{per}</xbrli:period></xbrli:context>')

    cy = (s_.isoformat(), e_.isoformat())
    context("cy", cy)
    context("cyi", e_.isoformat())
    context("cy_std", cy, [("bus:AccountingStandardsDimension", std_member)])
    context("cy_type", cy, [("bus:AccountsTypeDimension", "bus:FullAccounts")])
    context("cy_status", cy, [("bus:AccountsStatusDimension", "bus:AuditExempt-NoAccountantsReport")])
    context("cy_dir1", cy, [("bus:EntityOfficersDimension", "bus:Director1")])
    if dormant:
        context("cy_trading", cy, [("bus:EntityTradingStatusDimension", "bus:EntityHasNeverTraded" if d.get("trading_status") == "never"
                                     else "bus:EntityNoLongerTradingButTradedInPast")])
    for p, inst in (("cyi", e_.isoformat()),) + ((("pyi", pe.isoformat()),) if prior else ()):
        context(p + "_w1", inst, [("core:MaturitiesOrExpirationPeriodsDimension", "core:WithinOneYear")])
        context(p + "_a1", inst, [("core:MaturitiesOrExpirationPeriodsDimension", "core:AfterOneYear")])
        context(p + "_sc", inst, [("core:EquityClassesDimension", "core:ShareCapital")])
        context(p + "_re", inst, [("core:EquityClassesDimension", "core:RetainedEarningsAccumulatedLosses")])
        context(p + "_or", inst, [("core:EquityClassesDimension", "core:OtherReservesSubtotal")])
    if prior:
        context("pyi", pe.isoformat())
        context("py", (ps.isoformat(), pe.isoformat()))

    def nf(concept, ctxid, val: Decimal, show_zero_dash=True):
        sign = ' sign="-"' if val < 0 else ""
        txt = f"{abs(val):,.0f}"
        if val == 0 and show_zero_dash:
            return (f'<ix:nonFraction name="{concept}" contextRef="{ctxid}" unitRef="GBP" decimals="0" '
                    f'format="ixt:fixed-zero">-</ix:nonFraction>')
        inner = (f'<ix:nonFraction name="{concept}" contextRef="{ctxid}" unitRef="GBP" decimals="0" scale="0" '
                 f'format="ixt:num-dot-decimal"{sign}>{txt}</ix:nonFraction>')
        return f"({inner})" if val < 0 else inner

    def nn(concept, ctxid, text_):
        return f'<ix:nonNumeric name="{concept}" contextRef="{ctxid}">{text_}</ix:nonNumeric>'

    def ixdate(concept, ctxid, d0: dt.date):
        return (f'<ix:nonNumeric name="{concept}" contextRef="{ctxid}" format="ixt:date-day-monthname-year-en">'
                f'{d0.day} {d0.strftime("%B %Y")}</ix:nonNumeric>')

    rows = []

    def line(label, key, concept, ctx_c, ctx_p, cls="", always=False, note=""):
        cv, pv = g[key], gp[key]
        if not always and cv == 0 and (not prior or pv == 0):
            return
        rows.append(f'<tr class="{cls}"><td>{esc(label)}</td><td class="n">{note}</td>'
                    f'<td class="v">{nf(concept, ctx_c, cv)}</td>'
                    + (f'<td class="v">{nf(concept, ctx_p, pv)}</td>' if prior else "") + "</tr>")

    line("Called up share capital not paid", "unpaid_capital", "core:CalledUpShareCapitalNotPaidNotExpressedAsCurrentAsset", "cyi", "pyi")
    line("Fixed assets", "fixed", "core:FixedAssets", "cyi", "pyi")
    rows.append('<tr><td colspan="4" class="h">Current assets</td></tr>')
    line("Debtors", "debtors", "core:Debtors", "cyi", "pyi")
    line("Cash at bank and in hand", "cash", "core:CashBankOnHand", "cyi", "pyi")
    line("Total current assets", "current", "core:CurrentAssets", "cyi", "pyi", "sub", always=True)
    line("Creditors: amounts falling due within one year", "cred1", "core:Creditors", "cyi_w1", "pyi_w1")
    line("Net current assets (liabilities)", "ncl", "core:NetCurrentAssetsLiabilities", "cyi", "pyi", "sub", always=True)
    line("Total assets less current liabilities", "talcl", "core:TotalAssetsLessCurrentLiabilities", "cyi", "pyi", "sub", always=True)
    line("Creditors: amounts falling due after more than one year", "cred2", "core:Creditors", "cyi_a1", "pyi_a1")
    line("Provisions for liabilities", "provisions", "core:ProvisionsForLiabilitiesBalanceSheetSubtotal", "cyi", "pyi")
    line("Net assets (liabilities)", "net", "core:NetAssetsLiabilities", "cyi", "pyi", "tot", always=True)
    rows.append('<tr><td colspan="4" class="h">Capital and reserves</td></tr>')
    line("Called up share capital", "share_capital", "core:Equity", "cyi_sc", "pyi_sc", always=True)
    line("Other reserves", "other_reserves", "core:Equity", "cyi_or", "pyi_or")
    line("Profit and loss account", "reserves", "core:Equity", "cyi_re", "pyi_re", always=not dormant)
    line("Shareholders' funds", "equity", "core:Equity", "cyi", "pyi", "tot", always=True)

    if dormant:
        audit = nn("direp:StatementThatCompanyEntitledToExemptionFromAuditUnderSection480CompaniesAct2006RelatingToDormantCompanies", "cy",
                   f"For the {'period' if not prior else 'year'} ending {long_date(e_)} the company was entitled to exemption from audit "
                   "under section 480 of the Companies Act 2006 relating to dormant companies.")
    else:
        audit = nn("direp:StatementThatCompanyEntitledToExemptionFromAuditUnderSection477CompaniesAct2006RelatingToSmallCompanies", "cy",
                   f"For the {'period' if not prior else 'year'} ending {long_date(e_)} the company was entitled to exemption from audit "
                   "under section 477 of the Companies Act 2006 relating to small companies.")
    members = nn("direp:StatementThatMembersHaveNotRequiredCompanyToObtainAnAudit", "cy",
                 "The members have not required the company to obtain an audit in accordance with section 476 of the Companies Act 2006.")
    resp = nn("direp:StatementThatDirectorsAcknowledgeTheirResponsibilitiesUnderCompaniesAct", "cy",
              "The directors acknowledge their responsibilities for complying with the requirements of the Act with respect to "
              "accounting records and the preparation of accounts.")
    regime = nn("direp:StatementThatAccountsHaveBeenPreparedInAccordanceWithProvisionsSmallCompaniesRegime", "cy",
                ("These accounts have been prepared in accordance with the provisions applicable to companies subject to the "
                 "small companies regime" + (" and in accordance with the micro-entity provisions." if micro else ".")))
    filleted = "" if micro else ("<p>In accordance with section 444 of the Companies Act 2006 the profit and loss account "
                                 "has not been delivered to the Registrar of Companies.</p>")
    notes = []
    if not dormant:
        emp = whole(D(d.get("employees")))
        notes.append(f'<p><b>Average number of employees</b><br/>The average number of persons employed by the company '
                     f'(including directors) during the {"year" if prior else "period"} was '
                     f'<ix:nonFraction name="core:AverageNumberEmployeesDuringPeriod" contextRef="cy" unitRef="pure" decimals="0" '
                     f'format="ixt:num-dot-decimal">{emp}</ix:nonFraction>.</p>')
        if not micro:
            notes.append("<p><b>Accounting policies</b><br/>These financial statements have been prepared in accordance with "
                         "FRS 102 Section 1A 'Small Entities'. They are prepared under the historical cost convention and are "
                         "presented in sterling, rounded to the nearest pound.</p>")
    if dormant:
        notes.append("<p>The company was dormant throughout the period and has not traded"
                     + (" during the period." if d.get("trading_status") != "never" else " since incorporation.") + "</p>")
    period_head = f'<th class="v">{e_.year}<br/>£</th>' + (f'<th class="v">{pe.year if pe.year != e_.year else pe.strftime("%d/%m/%Y")}<br/>£</th>' if prior else "")

    hidden = "".join([
        nn("bus:EntityDormantTruefalse", "cy", "true" if dormant else "false"),
        nn("bus:AccountingStandardsApplied", "cy_std", ""),
        nn("bus:AccountsType", "cy_type", ""),
        nn("bus:AccountsStatusAuditedOrUnaudited", "cy_status", ""),
        nn("bus:EntityTradingStatus", "cy_trading", "") if dormant else "",
        nn("bus:NameEntityOfficer", "cy_dir1", esc(signer)),
    ])
    title = f"{esc(name)} – Annual accounts {long_date(e_)}"
    doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
 xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12" xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:core="{ns['core']}" xmlns:bus="{ns['bus']}" xmlns:direp="{ns['direp']}" xml:lang="en">
<head><meta http-equiv="Content-Type" content="text/html; charset=UTF-8"/><title>{title}</title>
<style type="text/css">body{{font-family:Arial,Helvetica,sans-serif;font-size:11pt;max-width:760px;margin:30px auto;color:#000}}
h1{{font-size:16pt;margin:0}}h2{{font-size:13pt;margin:22px 0 6px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:3px 4px}}
td.v,th.v{{text-align:right;width:110px}}td.n{{width:40px}}tr.sub td.v{{border-top:1px solid #000}}tr.tot td.v{{border-top:1px solid #000;border-bottom:3px double #000;font-weight:bold}}
td.h{{font-weight:bold;padding-top:10px}}.cover{{text-align:center;margin:60px 0 80px}}.pb{{page-break-before:always}}</style></head>
<body>
<div style="display:none"><ix:header><ix:hidden>{hidden}</ix:hidden>
<ix:references><link:schemaRef xlink:type="simple" xlink:href="{ns['entry']}"/></ix:references>
<ix:resources>{''.join(ctx)}<xbrli:unit id="GBP"><xbrli:measure>iso4217:GBP</xbrli:measure></xbrli:unit><xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit></ix:resources>
</ix:header></div>
<div class="cover"><p>Registered number: {nn("bus:UKCompaniesHouseRegisteredNumber", "cy", esc(num))}</p>
<h1>{nn("bus:EntityCurrentLegalOrRegisteredName", "cy", esc(name))}</h1>
<p>{'Unaudited ' if True else ''}{'Dormant ' if dormant else ''}{'Micro-entity ' if micro and not dormant else ''}{'Filleted ' if not micro else ''}Financial Statements</p>
<p>for the {'year' if prior else 'period'} {'from ' + ixdate("bus:StartDateForPeriodCoveredByReport", "cy", s_) + ' to '}{ixdate("bus:EndDateForPeriodCoveredByReport", "cy", e_)}</p></div>
<div class="pb"><h2>{esc(name)}<br/>Balance sheet as at {ixdate("bus:BalanceSheetDate", "cy", e_)}</h2>
<table><tr><th></th><th></th>{period_head}</tr>{''.join(rows)}</table>
<p>{audit}</p><p>{members}</p><p>{resp}</p><p>{regime}</p>{filleted}
<p>The financial statements were approved and authorised for issue by the board on
{ixdate("core:DateAuthorisationFinancialStatementsForIssue", "cy", ap)} and were signed on its behalf by:</p>
<p>{nn("core:DirectorSigningFinancialStatements", "cy_dir1", esc(signer))}<br/>Director</p>
<p>Registered number: {esc(num)}</p></div>
{('<div class="pb"><h2>Notes to the financial statements</h2>' + ''.join(notes) + '</div>') if notes else ''}
</body></html>"""
    return doc


def ixbrl_problems(doc: str) -> list[str]:
    out = []
    if etree is None:
        return ["lxml is not installed."]
    try:
        root = etree.fromstring(doc.encode("utf-8"))
    except Exception as ex:
        return [f"Accounts document is not well-formed XHTML: {ex}"]
    ixns = "http://www.xbrl.org/2013/inlineXBRL"
    ids = {e.get("id") for e in root.iter("{http://www.xbrl.org/2003/instance}context")}
    for e in root.iter(f"{{{ixns}}}nonFraction", f"{{{ixns}}}nonNumeric"):
        if e.get("contextRef") not in ids:
            out.append(f"{e.get('name')} uses a missing context {e.get('contextRef')}")
    names = {e.get("name") for e in root.iter(f"{{{ixns}}}nonNumeric")}
    for req in ("bus:UKCompaniesHouseRegisteredNumber", "bus:EntityCurrentLegalOrRegisteredName", "bus:BalanceSheetDate",
                "core:DateAuthorisationFinancialStatementsForIssue", "core:DirectorSigningFinancialStatements"):
        if req not in names:
            out.append(f"Required tag {req} missing")
    return out


# ----------------------------------------------------------------------------- confirmation statement

def cs_checks(c: dict, f: dict) -> tuple[list[str], list[str]]:
    d = f["d"]
    errs, warns = [], []
    if not c.get("company_number"):
        errs.append("Company number is missing.")
    if not re.fullmatch(r"[A-Za-z0-9]{6,8}", c.get("auth_code") or ""):
        errs.append("Company authentication code (6–8 characters) is missing.")
    r = pdate(d.get("review"))
    if not r:
        errs.append("Confirmation (review) date is needed.")
    elif r > dt.date.today():
        errs.append("The confirmation date cannot be in the future – wait until that date.")
    nd = pdate(c.get("cs_next_date"))
    if r and nd and r > nd:
        errs.append(f"Confirmation date is after the date it was due ({nd.strftime('%d/%m/%Y')}) – use {nd.strftime('%d/%m/%Y')} or earlier.")
    if d.get("sic_change"):
        codes = [x.strip() for x in re.split(r"[,\s]+", d.get("sic", "")) if x.strip()]
        if not codes or len(codes) > 4 or any(not re.fullmatch(r"\d{4,5}", x) for x in codes):
            errs.append("SIC codes: 1 to 4 codes of 4–5 digits each.")
    if d.get("email_change") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", d.get("registered_email", "")):
        errs.append("Registered email address is not valid.")
    if not d.get("email_change"):
        warns.append("No registered email address is being sent. Every company must have one – tick 'Give / change the "
                     "registered email address' if it has never been provided.")
    if d.get("capital_change"):
        if c["kind"] == "llp":
            errs.append("LLPs have no share capital – untick the capital change.")
        cls = [x for x in d.get("classes", []) if x.get("cls")]
        if not cls:
            errs.append("Statement of capital: add at least one share class.")
        for x in cls:
            if D(x.get("num")) <= 0 or D(x.get("nominal")) <= 0:
                errs.append(f"Share class {x['cls']}: number of shares and nominal value per share must be more than 0.")
        hold = [h for h in d.get("holders", []) if (h.get("name") or "").strip()]
        if not hold:
            errs.append("Shareholders: list every shareholder (a full list is required when there is a change).")
        for x in cls:
            tot = sum((D(h.get("num")) for h in hold if (h.get("cls") or "").upper() == x["cls"].upper()), Decimal(0))
            if tot != D(x.get("num")):
                warns.append(f"Shareholders hold {tot} {x['cls']} shares but the statement of capital says {x.get('num')}.")
    return errs, warns


def build_cs(c: dict, f: dict):
    d = f["d"]
    E = fm.E
    cs = etree.Element(f"{{{fm.NS_CH}}}ConfirmationStatement", nsmap={None: fm.NS_CH, "xsi": fm.NS_XSI})
    cs.set(f"{{{fm.NS_XSI}}}schemaLocation", LOC_CS)
    E(cs, "ReviewDate", iso(d.get("review")))
    if d.get("sic_change"):
        sc = E(cs, "SICCodes")
        for code in [x.strip() for x in re.split(r"[,\s]+", d.get("sic", "")) if x.strip()][:4]:
            E(sc, "SICCode", code)
    if d.get("capital_change") and c["kind"] != "llp":
        cls = [x for x in d.get("classes", []) if x.get("cls")]
        soc = E(cs, "StatementOfCapital")
        cap = E(soc, "Capital")
        tot_n = sum((D(x["num"]) for x in cls), Decimal(0))
        tot_v = sum((D(x["num"]) * D(x["nominal"]) for x in cls), Decimal(0))
        tot_u = sum((D(x["num"]) * D(x.get("unpaid")) for x in cls), Decimal(0))
        E(cap, "TotalAmountUnpaid", fm.fmt_dec(tot_u))
        E(cap, "TotalNumberOfIssuedShares", fm.fmt_dec(tot_n))
        E(cap, "ShareCurrency", (d.get("currency") or "GBP").upper())
        E(cap, "TotalAggregateNominalValue", fm.fmt_dec(tot_v))
        for x in cls:
            sh = E(cap, "Shares")
            E(sh, "ShareClass", x["cls"].upper())
            E(sh, "PrescribedParticulars", (x.get("particulars") or fm.DEFAULT_PARTICULARS).strip())
            E(sh, "NumShares", fm.fmt_dec(D(x["num"])))
            E(sh, "AggregateNominalValue", fm.fmt_dec(D(x["num"]) * D(x["nominal"])))
        hold = [h for h in d.get("holders", []) if (h.get("name") or "").strip()]
        for h in hold:
            sh = E(cs, "Shareholdings")
            E(sh, "ShareClass", (h.get("cls") or "ORDINARY").upper())
            E(sh, "NumberHeld", fm.fmt_dec(D(h.get("num"))))
            if pdate(h.get("transfer_date")) and D(h.get("transfer_num")) > 0:
                tr = E(sh, "Transfers")
                E(tr, "DateOfTransfer", iso(h["transfer_date"]))
                E(tr, "NumberSharesTransferred", fm.fmt_dec(D(h["transfer_num"])))
            who = E(sh, "Shareholders")
            nm = E(who, "Name")
            if h.get("corporate"):
                E(nm, "AmalgamatedName", h["name"].strip()[:160])
            else:
                parts = h["name"].strip().split()
                E(nm, "Surname", parts[-1].upper())
                if len(parts) > 1:
                    E(nm, "Forename", " ".join(parts[:-1])[:50])
    if d.get("email_change") and d.get("registered_email"):
        E(cs, "RegisteredEmailAddress", d["registered_email"].strip())
    E(cs, "AcceptLawfulPurposeStatement", "true")
    E(cs, "StateConfirmation", "true")
    return cs


_CS_SCHEMA: dict = {}


def cs_schema():
    if "x" not in _CS_SCHEMA:
        fm.schemas()  # extracts ch_schemas.zip (baseTypes etc.)
        src = ROOT / CS_XSD
        if not src.exists():
            raise RuntimeError(f"{CS_XSD} is missing – upload it to the main folder of the GitHub repo.")
        dst = fm.SCHEMA_DIR / CS_XSD
        dst.write_bytes(src.read_bytes())
        _CS_SCHEMA["x"] = etree.XMLSchema(etree.parse(str(dst)))
    return _CS_SCHEMA["x"]


# ----------------------------------------------------------------------------- gateway (shared with UK Formations)

def form_submission(c: dict, f: dict, subno: str, s_fm: dict):
    E, H = fm.E, fm.NS_HDR
    fs = etree.Element(f"{{{H}}}FormSubmission", nsmap={None: H, "xsi": fm.NS_XSI})
    fs.set(f"{{{fm.NS_XSI}}}schemaLocation", fm.LOC_FS)
    h = E(fs, "FormHeader", ns=H)
    E(h, "CompanyNumber", number_digits(c["company_number"]), H)
    E(h, "CompanyType", company_type_code(c["company_number"]), H)
    E(h, "CompanyName", re.sub(r"\s+", " ", c.get("name", "").strip()).upper(), H)
    E(h, "CompanyAuthenticationCode", (c.get("auth_code") or "").strip(), H)
    E(h, "PackageReference", (s_fm.get("package_reference") or "0012").strip(), H)
    E(h, "FormIdentifier", "ConfirmationStatement" if f["type"] == "CS01" else "Accounts", H)
    E(h, "SubmissionNumber", subno, H)
    if s_fm.get("contact_name") and s_fm.get("contact_number"):
        E(h, "ContactName", s_fm["contact_name"].strip(), H)
        E(h, "ContactNumber", s_fm["contact_number"].strip()[:25], H)
    E(h, "CustomerReference", (c["id"][:8] + f["id"]).upper()[:25], H)
    E(fs, "DateSigned", dt.date.today().isoformat(), H)
    form = E(fs, "Form", ns=H)
    if f["type"] == "CS01":
        form.append(build_cs(c, f))
    else:
        doc = build_ixbrl(c, f, load_settings())
        el = E(fs, "Document", ns=H)
        E(el, "Data", base64.b64encode(doc.encode("utf-8")).decode(), H)
        E(el, "Date", dt.date.today().isoformat(), H)
        E(el, "Filename", "accounts.html", H)
        E(el, "ContentType", "application/xml", H)
        E(el, "Category", "ACCOUNTS", H)
    return fs


def schema_problems(fs, ftype: str) -> list[str]:
    out = []
    sc = fm.schemas()
    shell = etree.fromstring(etree.tostring(fs))
    form = shell.find(f"{{{fm.NS_HDR}}}Form")
    inner = list(form)
    for ch in inner:
        form.remove(ch)
    if not sc["fs"].validate(shell):
        out += ["Form header: " + fm._explain(e) for e in sc["fs"].error_log]
    if ftype == "CS01":
        x = cs_schema()
        csel = fs.find(f"{{{fm.NS_HDR}}}Form/{{{fm.NS_CH}}}ConfirmationStatement")
        if not x.validate(etree.fromstring(etree.tostring(csel))):
            out += ["Confirmation statement: " + fm._explain(e) for e in x.error_log]
    return out


def checks(c: dict, f: dict) -> tuple[list[str], list[str]]:
    if f["type"] == "CS01":
        return cs_checks(c, f)
    if f["type"].startswith("ACC_"):
        e, w = acc_checks(c, f)
        if not e:
            e += ixbrl_problems(build_ixbrl(c, f, load_settings()))
        return e, w
    return [], []


def submit(c: dict, f: dict, mode: str) -> tuple[bool, str]:
    errs, _ = checks(c, f)
    if errs:
        return False, "Fix the problems shown first."
    pid, code = fm.creds(mode)
    if not (pid and code):
        return False, "Presenter ID / code not set – add CH_PRESENTER_ID and CH_PRESENTER_CODE in Railway → Variables."
    s = load_settings()
    key = "cs_live_approved" if f["type"] == "CS01" else "accounts_live_approved"
    if mode == "live" and not s.get(key):
        return False, ("LIVE filing is locked until Companies House has approved the software for this form "
                       "(tick it in UK Tax → Settings).")
    with fm._LOCK:                       # one counter for every Companies House submission we make
        s_fm = fm.load_settings()
        subno = fm.next_submission_number(s_fm)
        fm.save_settings(s_fm)
    fs = form_submission(c, f, subno, s_fm)
    probs = schema_problems(fs, f["type"])
    if probs:
        return False, "Schema check failed: " + "; ".join(probs[:5])
    cls = "ConfirmationStatement" if f["type"] == "CS01" else "Accounts"
    xml = fm.envelope(cls, fs, mode)
    d = fdir(c, f)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (d / f"sent_{stamp}_{subno}.xml").write_bytes(fm.masked(re.sub(rb"(<(?:\w+:)?Data>)[^<]{200,}(</)", rb"\1[base64 accounts]\2", xml)))
    try:
        raw = fm.post_gateway(xml, timeout=90)
    except Exception as ex:
        f["status"] = "error"
        log(f, f"Could not reach Companies House ({mode.upper()}): {ex}")
        return False, f"Could not reach Companies House: {ex}"
    (d / f"reply_{stamp}_{subno}.xml").write_bytes(raw)
    try:
        resp = etree.fromstring(raw)
    except Exception:
        f["status"] = "error"
        log(f, "Companies House sent a reply that is not XML.")
        return False, "Companies House sent an unreadable reply (saved on the filing)."
    gerr = fm.gateway_errors(resp)
    if fm.text(resp, "Qualifier") == "error" or gerr:
        f["status"] = "error"
        log(f, f"{mode.upper()} submission {subno} REFUSED by the gateway: " + "; ".join(gerr or ["(no reason given)"]))
        return False, "Refused by the Companies House gateway: " + "; ".join(gerr or ["no reason given"])
    f.update(status="submitted", mode=mode, submission_number=subno, submitted_at=dt.datetime.now().isoformat(timespec="seconds"),
             rejections=[], examiner="")
    f.setdefault("submissions", []).append({"no": subno, "mode": mode, "t": f["submitted_at"]})
    log(f, f"Sent to Companies House ({mode.upper()}), submission number {subno}."
        + (" TEST – nothing is really filed." if mode == "test" else ""))
    return True, f"Sent ({mode.upper()}). Submission number {subno}."


def poll(c: dict, f: dict) -> str:
    if not f.get("submission_number") or f.get("status") not in ("submitted", "pending", "parked"):
        return "Nothing to check."
    mode = f.get("mode") or "test"
    pid, _ = fm.creds(mode)
    body = etree.Element(f"{{{fm.NS_CH}}}GetSubmissionStatus", nsmap={None: fm.NS_CH})
    fm.E(body, "SubmissionNumber", f["submission_number"])
    fm.E(body, "PresenterID", pid)
    try:
        resp = etree.fromstring(fm.post_gateway(fm.envelope("GetSubmissionStatus", body, mode)))
    except Exception as ex:
        return f"Could not reach Companies House: {ex}"
    gerr = fm.gateway_errors(resp)
    if gerr:
        return "Gateway error: " + "; ".join(gerr)
    f["last_poll"] = dt.datetime.now().isoformat(timespec="seconds")
    st = [x for x in fm.find_all(resp, "Status") if fm.text(x, "SubmissionNumber").upper() == f["submission_number"].upper()]
    if not st:
        return "No status yet – Companies House has not processed it."
    x = st[-1]
    code = fm.text(x, "StatusCode")
    rej = [f"{fm.text(r, 'RejectCode')}: {fm.text(r, 'Description')}" for r in fm.find_all(x, "Reject")]
    ex_ = fm.find1(x, "Examiner")
    examiner = (" – ".join(v for v in (fm.text(ex_, "Telephone"), fm.text(ex_, "Comment")) if v)) if ex_ is not None else ""
    if code == "ACCEPT":
        if f.get("status") != "accepted":
            f["status"] = "accepted"
            f["accepted_at"] = dt.datetime.now().isoformat(timespec="seconds")
            log(f, f"ACCEPTED by Companies House ({mode.upper()}).")
            notify(c, f, "accepted")
        return "Accepted."
    if code == "REJECT":
        if f.get("status") != "rejected":
            f.update(status="rejected", rejections=rej, examiner=examiner)
            log(f, "REJECTED: " + "; ".join(rej or ["no reason given"]) + (f" Examiner: {examiner}" if examiner else ""))
            notify(c, f, "rejected")
        return "Rejected – see the reasons."
    if code in ("PENDING", "PARKED"):
        if f.get("status") != code.lower():
            f["status"] = code.lower()
            log(f, f"Companies House status: {code}." + (f" Examiner: {examiner}" if examiner else ""))
        return f"Status: {code}."
    return f"Status: {code or 'unknown'}."


def notify(c: dict, f: dict, what: str):
    to = (load_settings().get("notify_email") or "").strip()
    if not to or not _SEND["fn"]:
        return
    lab = FILING_TYPES[f["type"]]["label"]
    if what == "accepted":
        subj = f"ACCEPTED: {lab} – {c.get('name')}"
        body = f"Companies House accepted the {lab} for {c.get('name')} ({c.get('company_number')}), {period_label(f)}."
    else:
        subj = f"REJECTED: {lab} – {c.get('name')}"
        body = (f"Companies House rejected the {lab} for {c.get('name')} ({c.get('company_number')}).\n\nReasons:\n- "
                + "\n- ".join(f.get("rejections") or ["(none given)"]) + f"\n\nExaminer: {f.get('examiner', '')}\n\n"
                "Correct it in the Mail Room → UK Tax and send again.")
    try:
        _SEND["fn"](_cfg(), to, subj, body, [])
    except Exception as ex:
        log(f, f"Notification email failed: {ex}")


def _poll_loop():
    while True:
        time.sleep(600)
        try:
            for c0 in list_clients():
                for f0 in c0["filings"]:
                    if f0.get("status") in ("submitted", "pending", "parked"):
                        with _LOCK:
                            c = load_client(c0["id"])
                            f = get_filing(c, f0["id"])
                            poll(c, f)
                            save_client(c)
        except Exception as ex:  # never kill the thread
            print(f"[uktax] poll error: {ex}", flush=True)


# ----------------------------------------------------------------------------- HMRC computations

def ct_computation(f: dict) -> dict:
    d = f["d"]
    s, e = pdate(d.get("start")), pdate(d.get("end"))
    days = (e - s).days + 1 if s and e else 365
    profit = D(d.get("profit"))
    add = D(d.get("depreciation")) + D(d.get("disallowable")) + D(d.get("other_addbacks"))
    ded = D(d.get("capital_allowances")) + D(d.get("other_deductions"))
    trading = profit + add - ded
    losses_bf = max(D(d.get("losses_bf")), Decimal(0))
    used = min(losses_bf, max(trading, Decimal(0)))
    trading_after = trading - used
    other = D(d.get("income_other"))
    ttp = max(trading_after, Decimal(0)) + max(other, Decimal(0))
    ttp = ttp.quantize(Decimal("1"), rounding=ROUND_DOWN)
    assoc = int(D(d.get("associated")))
    year_days = 366 if (s and e and any(calendar.isleap(y) and s <= dt.date(y, 2, 29) <= e for y in range(s.year, e.year + 1))) else 365
    frac = Decimal(days) / Decimal(year_days)
    lower = (Decimal(50000) / (assoc + 1) * frac).quantize(Decimal("0.01"))
    upper = (Decimal(250000) / (assoc + 1) * frac).quantize(Decimal("0.01"))
    if ttp <= lower:
        rate_note, tax, mr = "Small profits rate 19%", ttp * Decimal("0.19"), Decimal(0)
    elif ttp >= upper:
        rate_note, tax, mr = "Main rate 25%", ttp * Decimal("0.25"), Decimal(0)
    else:
        mr = Decimal(3) / Decimal(200) * (upper - ttp)
        rate_note, tax = "Main rate 25% less marginal relief", ttp * Decimal("0.25") - mr
    tax = tax.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    loss_cf = losses_bf - used + (-trading if trading < 0 else Decimal(0))
    return {"days": days, "profit": profit, "add": add, "ded": ded, "trading": trading, "losses_used": used,
            "ttp": ttp, "lower": lower, "upper": upper, "mr": mr.quantize(Decimal("0.01")), "rate_note": rate_note,
            "tax": tax, "loss_cf": loss_cf, "balance": tax - D(d.get("ct_paid")), "short": days < 365,
            "long": days > 366}


SA_RATES = {  # tax year -> rates (England, Wales and Northern Ireland)
    "2024-25": {"pa": 12570, "basic": 37700, "add_from": 125140, "div": (Decimal("0.0875"), Decimal("0.3375"), Decimal("0.3935"))},
    "2025-26": {"pa": 12570, "basic": 37700, "add_from": 125140, "div": (Decimal("0.0875"), Decimal("0.3375"), Decimal("0.3935"))},
    "2026-27": {"pa": 12570, "basic": 37700, "add_from": 125140, "div": (Decimal("0.1075"), Decimal("0.3575"), Decimal("0.3935"))},
}


def sa_estimate(f: dict) -> dict | None:
    d = f["d"]
    r = SA_RATES.get(d.get("tax_year"))
    if not r:
        return None
    emp, se, prop = D(d.get("employment")), D(d.get("self_employment")), D(d.get("property"))
    oth = D(d.get("other"))
    sav, div = max(D(d.get("savings")), Decimal(0)), max(D(d.get("dividends")), Decimal(0))
    pens = max(D(d.get("pension_gross")), Decimal(0))       # relief at source: extends the basic + higher bands
    non_sav = max(emp + max(se, Decimal(0)) + max(prop, Decimal(0)) + oth, Decimal(0))
    total = non_sav + sav + div
    adj_net = total - pens
    pa = Decimal(r["pa"])
    if adj_net > 100000:
        pa = max(Decimal(0), pa - ((adj_net - 100000) / 2).quantize(Decimal("1"), rounding=ROUND_DOWN))
    basic = Decimal(r["basic"]) + pens
    higher_top = Decimal(r["add_from"]) - Decimal(r["pa"]) + pens   # band above which additional rate applies (taxable income)
    # allocate the personal allowance: non-savings, then savings, then dividends
    left = pa
    ns_t = max(non_sav - left, Decimal(0)); left = max(left - non_sav, Decimal(0))
    sv_t = max(sav - left, Decimal(0)); left = max(left - sav, Decimal(0))
    dv_t = max(div - left, Decimal(0))
    used = Decimal(0)
    tax = Decimal(0)
    lines = []

    def band_tax(amount, rates, label, zero_first=Decimal(0)):
        nonlocal used, tax
        z = min(zero_first, amount)
        used += z
        amount -= z
        if z:
            lines.append((f"{label} at 0%", z, Decimal(0)))
        for top, rate, nm in ((basic, rates[0], "basic"), (higher_top, rates[1], "higher"), (Decimal("1e15"), rates[2], "additional")):
            room = max(top - used, Decimal(0))
            part = min(amount, room)
            if part > 0:
                t = (part * rate).quantize(Decimal("0.01"))
                lines.append((f"{label} at {rate * 100:.4g}% ({nm})", part, t))
                tax += t
                used += part
                amount -= part
    band_tax(ns_t, (Decimal("0.20"), Decimal("0.40"), Decimal("0.45")), "Earnings / profits")
    start_rate = max(Decimal(5000) - ns_t, Decimal(0))
    taxable_all = ns_t + sv_t + dv_t
    psa = Decimal(0) if taxable_all > higher_top else (Decimal(500) if taxable_all > basic else Decimal(1000))
    band_tax(sv_t, (Decimal("0.20"), Decimal("0.40"), Decimal("0.45")), "Savings", zero_first=start_rate + psa)
    band_tax(dv_t, r["div"], "Dividends", zero_first=Decimal(500))
    lower_l, upper_l = Decimal(12570), Decimal(50270)
    c4 = Decimal(0)
    if se > lower_l:
        c4 = (min(se, upper_l) - lower_l) * Decimal("0.06") + max(se - upper_l, Decimal(0)) * Decimal("0.02")
    c4 = c4.quantize(Decimal("0.01"))
    sl = D(d.get("student_loan"))
    total_due = tax + c4 + sl - D(d.get("paye"))
    poa = (total_due / 2).quantize(Decimal("0.01")) if total_due > 1000 and D(d.get("paye")) < (tax + c4) * Decimal("0.8") else Decimal(0)
    return {"pa": pa, "income": total, "lines": lines, "income_tax": tax, "class4": c4, "student_loan": sl,
            "paye": D(d.get("paye")), "due": total_due, "poa": poa, "balancing": total_due - D(d.get("poa_paid")),
            "rates_year": d.get("tax_year")}


def sa800_split(f: dict) -> dict:
    d = f["d"]
    profit = D(d.get("profit")) if str(d.get("profit", "")).strip() else D(d.get("turnover")) - D(d.get("expenses"))
    ps = [p for p in d.get("partners", []) if (p.get("name") or "").strip()]
    tot_share = sum((D(p.get("share")) for p in ps), Decimal(0))
    rows = []
    for p in ps:
        sh = D(p.get("share"))
        rows.append({"name": p["name"], "utr": p.get("utr", ""), "share": sh,
                     "amount": (profit * sh / tot_share).quantize(Decimal("1")) if tot_share else Decimal(0)})
    return {"profit": profit, "rows": rows, "total_share": tot_share}


# ----------------------------------------------------------------------------- routes

def _ctx(**kw):
    kw.update(VERSION=VERSION, KINDS=KINDS, FT=FILING_TYPES, GROUPS=GROUPS, CH_STATUS=CH_STATUS, HMRC_STAGES=HMRC_STAGES,
              DONE=DONE, money=money, period_label=period_label, days_left=days_left, is_done=is_done)
    return kw


@bp.get("/uktax")
def home():
    tab = request.args.get("tab", "due")
    if tab not in GROUPS:
        tab = "due"
    rows = all_rows()
    if tab == "due":
        rows = [r for r in rows if not is_done(r["f"]) and (r["days"] is None or r["days"] <= 60
                                                             or r["f"].get("status") in ("rejected", "error", "parked"))]
    elif tab not in ("all", "clients"):
        rows = [r for r in rows if r["group"] == tab]
    live_id, live_code = fm.creds("live")
    return render_template_string(HOME, **_ctx(tab=tab, rows=rows, clients=list_clients(), s=load_settings(),
                                               live_set=bool(live_id and live_code), api_key=bool(os.environ.get("CH_API_KEY")),
                                               fm_s=fm.load_settings()))


@bp.post("/uktax/settings")
def settings_save():
    s = load_settings()
    for k in ("taxonomy", "notify_email", "firm_name", "firm_email"):
        if k in request.form:
            s[k] = request.form.get(k, "").strip()
    s["accounts_live_approved"] = "1" if request.form.get("accounts_live_approved") else ""
    s["cs_live_approved"] = "1" if request.form.get("cs_live_approved") else ""
    save_settings(s)
    flash("UK Tax settings saved.")
    return redirect("/uktax?tab=clients")


@bp.post("/uktax/new")
def new():
    c = new_client(request.form.get("kind", "company"))
    c["name"] = request.form.get("name", "").strip()
    c["company_number"] = request.form.get("company_number", "").strip().upper()
    msg = ""
    if c["company_number"] and c["kind"] in ("company", "llp"):
        msg = load_from_ch(c)
    if not c["name"]:
        c["name"] = c["company_number"] or "(new client)"
    save_client(c)
    if msg:
        flash(msg)
    return redirect(f"/uktax/c/{c['id']}")


@bp.get("/uktax/c/<cid>")
def client_page(cid):
    c = load_client(cid)
    return render_template_string(CLIENT, **_ctx(c=c, suggested=suggested_filings(c), api_key=bool(os.environ.get("CH_API_KEY"))))


@bp.post("/uktax/c/<cid>/save")
def client_save(cid):
    with _LOCK:
        c = load_client(cid)
        for k in ("name", "company_number", "auth_code", "utr", "ni_number", "email", "phone", "contact", "address",
                  "registered_email", "incorporated", "accounts_next_start", "accounts_next_end", "accounts_due",
                  "cs_next_date", "cs_due", "sic", "notes"):
            if k in request.form:
                v = request.form.get(k, "").strip()
                c[k] = v.upper() if k in ("company_number", "ni_number") else v
        if request.form.get("kind") in KINDS:
            c["kind"] = request.form["kind"]
        if c["kind"] in ("partnership", "llp"):
            ps = []
            for i in range(int(request.form.get("np", "0") or 0)):
                nm = request.form.get(f"p{i}_name", "").strip()
                if nm:
                    ps.append({"name": nm, "utr": request.form.get(f"p{i}_utr", "").strip(),
                               "share": request.form.get(f"p{i}_share", "").strip()})
            c["partners"] = ps
        for f in c["filings"]:
            refresh_due(c, f)
        save_client(c)
    flash("Saved.")
    return redirect(f"/uktax/c/{cid}")


@bp.post("/uktax/c/<cid>/ch")
def client_ch(cid):
    with _LOCK:
        c = load_client(cid)
        msg = load_from_ch(c)
        for f in c["filings"]:
            refresh_due(c, f)
        save_client(c)
    flash(msg)
    return redirect(f"/uktax/c/{cid}")


@bp.post("/uktax/c/<cid>/add")
def add_filing(cid):
    t = request.form.get("type", "")
    if t not in FILING_TYPES:
        abort(400)
    with _LOCK:
        c = load_client(cid)
        if c["kind"] not in FILING_TYPES[t]["for"]:
            flash(f"{FILING_TYPES[t]['label']} is not for a {KINDS[c['kind']].lower()}.")
            return redirect(f"/uktax/c/{cid}")
        f = new_filing(c, t)
        c["filings"].append(f)
        save_client(c)
    return redirect(f"/uktax/c/{cid}/f/{f['id']}")


@bp.post("/uktax/c/<cid>/delete")
def client_delete(cid):
    if request.form.get("password", "") != os.environ.get("DELETE_PASSWORD", "askusama"):  # same as the rest of the Mail Room
        flash("⚠ Wrong delete password.")
        return redirect(f"/uktax/c/{cid}")
    c = load_client(cid)
    if any(f.get("status") in ("submitted", "pending", "parked", "accepted", "filed") for f in c["filings"]):
        flash("⚠ This client has filings that were sent or filed – it cannot be deleted (keep the record).")
        return redirect(f"/uktax/c/{cid}")
    (cdir() / f"{cid}.json").unlink(missing_ok=True)
    flash("Client deleted.")
    return redirect("/uktax?tab=clients")


@bp.get("/uktax/c/<cid>/f/<fid>")
def filing_page(cid, fid):
    c = load_client(cid)
    f = get_filing(c, fid)
    errs, warns = checks(c, f)
    extra = {}
    if f["type"].startswith("ACC_"):
        extra["g"], extra["gp"], extra["prior"] = acc_figures(f["d"]), acc_figures(f["d"], "py_"), has_prior(f["d"])
        extra["LINES"] = ACC_LINES
    elif f["type"] == "CT600":
        extra["ct"] = ct_computation(f)
    elif f["type"] == "SA100":
        extra["sa"] = sa_estimate(f)
        extra["years"] = list(SA_RATES)
    elif f["type"] == "SA800":
        extra["sp"] = sa800_split(f)
    files = sorted(p.name for p in fdir(c, f).glob("*")) if f["type"] in ("CS01",) or f["type"].startswith("ACC_") else []
    s = load_settings()
    key = "cs_live_approved" if f["type"] == "CS01" else "accounts_live_approved"
    return render_template_string(FILING, **_ctx(c=c, f=f, d=f["d"], errs=errs, warns=warns, files=files,
                                                 live_ok=bool(all(fm.creds("live")) and s.get(key)),
                                                 creds_ok=bool(all(fm.creds("test"))), **extra))


def _form_into(f: dict):
    d = f["d"]
    form = request.form
    for k, v in form.items():
        if k.startswith("d_"):
            d[k[2:]] = v.strip()
    for flag in ("first_year", "sic_change", "email_change", "capital_change"):
        if f"present_{flag}" in form:
            d[flag] = "1" if form.get(f"d_{flag}") else ""
    if f["type"] == "CS01":
        d["classes"] = [{"cls": form.get(f"c{i}_cls", "").strip().upper(), "num": form.get(f"c{i}_num", "").strip(),
                         "nominal": form.get(f"c{i}_nominal", "").strip(), "unpaid": form.get(f"c{i}_unpaid", "0").strip(),
                         "particulars": form.get(f"c{i}_particulars", "").strip()}
                        for i in range(int(form.get("nc", "0") or 0)) if form.get(f"c{i}_cls", "").strip()]
        d["holders"] = [{"name": form.get(f"h{i}_name", "").strip(), "corporate": "1" if form.get(f"h{i}_corporate") else "",
                         "cls": form.get(f"h{i}_cls", "ORDINARY").strip().upper(), "num": form.get(f"h{i}_num", "").strip(),
                         "transfer_date": form.get(f"h{i}_transfer_date", "").strip(),
                         "transfer_num": form.get(f"h{i}_transfer_num", "").strip()}
                        for i in range(int(form.get("nh", "0") or 0)) if form.get(f"h{i}_name", "").strip()]
    if f["type"] == "SA800":
        d["partners"] = [{"name": form.get(f"p{i}_name", "").strip(), "utr": form.get(f"p{i}_utr", "").strip(),
                          "share": form.get(f"p{i}_share", "").strip()}
                         for i in range(int(form.get("np", "0") or 0)) if form.get(f"p{i}_name", "").strip()]


@bp.post("/uktax/c/<cid>/f/<fid>/save")
def filing_save(cid, fid):
    with _LOCK:
        c = load_client(cid)
        f = get_filing(c, fid)
        if f["status"] in ("submitted", "pending", "parked", "accepted") and FILING_TYPES[f["type"]]["auth"] == "CH":
            flash("Already sent to Companies House – it cannot be changed.")
            return redirect(f"/uktax/c/{cid}/f/{fid}")
        _form_into(f)
        st = request.form.get("status")
        if st and (st in HMRC_STAGES or st in ("filed_elsewhere", "not_needed", "draft")):
            if st != f.get("status"):
                log(f, f"Stage changed to: {HMRC_STAGES.get(st) or CH_STATUS.get(st, st)}")
            f["status"] = st
        for k in ("filed_date", "reference", "notes"):
            if k in request.form:
                f[k] = request.form.get(k, "").strip()
        refresh_due(c, f)
        save_client(c)
    flash("Saved.")
    return redirect(f"/uktax/c/{cid}/f/{fid}")


@bp.get("/uktax/c/<cid>/f/<fid>/accounts.html")
def accounts_html(cid, fid):
    c = load_client(cid)
    f = get_filing(c, fid)
    if not f["type"].startswith("ACC_"):
        abort(404)
    try:
        doc = build_ixbrl(c, f, load_settings())
    except Exception as ex:
        return Response(f"Fill in the period dates first ({ex}).", mimetype="text/plain")
    if request.args.get("download"):
        return send_file(io.BytesIO(doc.encode("utf-8")), mimetype="application/xhtml+xml", as_attachment=True,
                         download_name=f"{re.sub(r'[^A-Za-z0-9]+', '_', c.get('name', 'accounts'))}_{iso(f['d'].get('end'))}.xhtml")
    return Response(doc, mimetype="application/xhtml+xml")


@bp.get("/uktax/c/<cid>/f/<fid>/xml")
def xml_preview(cid, fid):
    c = load_client(cid)
    f = get_filing(c, fid)
    if FILING_TYPES[f["type"]]["auth"] != "CH":
        abort(404)
    errs, _ = checks(c, f)
    if errs:
        return Response("Fix these first:\n- " + "\n- ".join(errs), mimetype="text/plain")
    fs = form_submission(c, f, "SU0000", fm.load_settings())
    probs = schema_problems(fs, f["type"])
    for d_ in fs.iter(f"{{{fm.NS_HDR}}}Data"):
        d_.text = f"[{len(d_.text or '')} characters of base64 iXBRL accounts]"
    for a_ in fs.iter(f"{{{fm.NS_HDR}}}CompanyAuthenticationCode"):
        a_.text = "******"
    head = ("<!-- Official schema check: " + ("PASSED" if not probs else "FAILED: " + "; ".join(probs)) + " -->\n").encode()
    return Response(head + etree.tostring(fs, pretty_print=True, encoding="UTF-8"), mimetype="text/plain")


@bp.post("/uktax/c/<cid>/f/<fid>/submit")
def submit_route(cid, fid):
    mode = request.form.get("mode", "test")
    if mode == "live" and request.form.get("confirm_live") != "LIVE":
        flash("Type LIVE to confirm a real filing.")
        return redirect(f"/uktax/c/{cid}/f/{fid}")
    if not request.form.get("approved"):
        flash("Tick that the directors / client approved this filing.")
        return redirect(f"/uktax/c/{cid}/f/{fid}")
    with _LOCK:
        c = load_client(cid)
        f = get_filing(c, fid)
        if f.get("status") in ("submitted", "pending", "parked", "accepted"):
            flash("Already sent.")
            return redirect(f"/uktax/c/{cid}/f/{fid}")
        ok, msg = submit(c, f, mode)
        save_client(c)
    flash(msg)
    return redirect(f"/uktax/c/{cid}/f/{fid}")


@bp.post("/uktax/c/<cid>/f/<fid>/poll")
def poll_route(cid, fid):
    with _LOCK:
        c = load_client(cid)
        f = get_filing(c, fid)
        msg = poll(c, f)
        save_client(c)
    flash(msg)
    return redirect(f"/uktax/c/{cid}/f/{fid}")


@bp.post("/uktax/c/<cid>/f/<fid>/reopen")
def reopen(cid, fid):
    with _LOCK:
        c = load_client(cid)
        f = get_filing(c, fid)
        if f.get("status") in ("rejected", "error"):
            f["status"] = "draft"
            log(f, "Re-opened for correction.")
            save_client(c)
    return redirect(f"/uktax/c/{cid}/f/{fid}")


@bp.post("/uktax/c/<cid>/f/<fid>/remove")
def remove_filing(cid, fid):
    with _LOCK:
        c = load_client(cid)
        f = get_filing(c, fid)
        if f.get("status") in ("submitted", "pending", "parked", "accepted", "filed", "approved"):
            flash("This filing was sent / filed – it is kept as a record. Set it to 'Not needed' instead.")
            return redirect(f"/uktax/c/{cid}/f/{fid}")
        c["filings"] = [x for x in c["filings"] if x["id"] != fid]
        save_client(c)
    flash("Filing removed.")
    return redirect(f"/uktax/c/{cid}")


@bp.get("/uktax/c/<cid>/f/<fid>/download/<name>")
def download(cid, fid, name):
    c = load_client(cid)
    f = get_filing(c, fid)
    p = fdir(c, f) / name
    if "/" in name or ".." in name or not p.exists():
        abort(404)
    return send_file(p, as_attachment=True)


CHECKLIST = {
    "CT600": ["Bank statements for the whole accounting period", "Sales invoices / income records",
              "Purchase invoices and receipts", "Details of any assets bought or sold (date, cost)",
              "Loans to or from directors and the balance at the year end", "Amounts owed to the company and owed by it at the year end",
              "Dividends paid during the period"],
    "SA100": ["P60 / P45 and any P11D from employment", "Self-employment income and expenses for the tax year",
              "Rental income and expenses (if you let property)", "Bank / building society interest received",
              "Dividends received", "Personal pension contributions (gross) and Gift Aid donations",
              "Your UTR and National Insurance number (if we do not have them)"],
    "SA800": ["Partnership bank statements for the accounting period", "Income and expense records",
              "Each partner's name, address, UTR and NI number", "Profit-sharing ratio and any changes during the year",
              "Assets bought or sold"],
}


@bp.post("/uktax/c/<cid>/f/<fid>/request_info")
def request_info(cid, fid):
    with _LOCK:
        c = load_client(cid)
        f = get_filing(c, fid)
        to = (c.get("email") or "").strip()
        if not to or not _SEND["fn"]:
            flash("No client email address on the client record.")
            return redirect(f"/uktax/c/{cid}/f/{fid}")
        s = load_settings()
        lab = FILING_TYPES[f["type"]]["label"]
        items = CHECKLIST.get(f["type"], [])
        due = pdate(f.get("due"))
        body = (f"Dear {c.get('contact') or c.get('name')},\n\nWe are preparing your {lab} for {period_label(f)}"
                + (f" (deadline {due.strftime('%d/%m/%Y')})" if due else "") + ". Please send us:\n\n- " + "\n- ".join(items)
                + f"\n\nYou can reply to this email with the documents attached.\n\nKind regards\n{s.get('firm_name')}\n{s.get('firm_email')}")
        try:
            _SEND["fn"](_cfg(), to, f"Information needed for your {lab} – {c.get('name')}", body, [])
        except Exception as ex:
            flash(f"Email failed: {ex}")
            return redirect(f"/uktax/c/{cid}/f/{fid}")
        f["status"] = "info_requested" if f["status"] in ("info_requested",) else f["status"]
        f["info_requested_at"] = dt.datetime.now().isoformat(timespec="seconds")
        log(f, f"Information checklist emailed to {to}.")
        save_client(c)
    flash(f"Checklist emailed to {to}.")
    return redirect(f"/uktax/c/{cid}/f/{fid}")


@bp.get("/uktax/export.csv")
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Client", "Type", "Company no.", "UTR", "Filing", "Period / year", "Due", "Status", "Submission no.",
                "Filed date", "Reference"])
    for r in all_rows():
        c, f = r["c"], r["f"]
        due = pdate(f.get("due"))
        w.writerow([c.get("name"), KINDS.get(c["kind"]), c.get("company_number"), c.get("utr"), FILING_TYPES[f["type"]]["label"],
                    period_label(f), due.strftime("%d/%m/%Y") if due else "",
                    HMRC_STAGES.get(f["status"]) or CH_STATUS.get(f["status"], f["status"]), f.get("submission_number", ""),
                    f.get("filed_date", ""), f.get("reference", "")])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=uk_tax_filings.csv"})


def register(app, cfg_fn=None, send_fn=None):
    _CFG["fn"] = cfg_fn
    _SEND["fn"] = send_fn
    OUT.mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)
    if not _POLL["started"] and os.environ.get("UKTAX_NO_POLL") != "1":
        _POLL["started"] = True
        threading.Thread(target=_poll_loop, daemon=True).start()

    @app.context_processor
    def _uktax_badge():
        try:
            return {"uktax_alerts": alert_count()}
        except Exception:
            return {"uktax_alerts": 0}


# ----------------------------------------------------------------------------- templates

STYLE = """<style>
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 14px}.tabs a{padding:8px 14px;border-radius:8px;background:#f1f5f9;color:#111;text-decoration:none;font-weight:600;font-size:14px}
.tabs a.on{background:#111;color:#fff}
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:4px 16px}
table.t{width:100%;border-collapse:collapse;font-size:14px}table.t th,table.t td{border-bottom:1px solid var(--line,#e5e7eb);padding:7px 6px;text-align:left;vertical-align:top}
table.t td.r,table.t th.r{text-align:right}table.t tr.tot td{font-weight:700;border-top:2px solid #111}
.pill{display:inline-block;border-radius:10px;padding:2px 9px;font-size:12px;font-weight:700;background:#e5e7eb}
.pill.late{background:#fee2e2;color:#991b1b}.pill.soon{background:#ffedd5;color:#9a3412}.pill.ok{background:#dcfce7;color:#166534}
.pill.ch{background:#dbeafe;color:#1e40af}.pill.hmrc{background:#ede9fe;color:#5b21b6}
.why{font-size:12px;color:#555}.okb{background:#e8f7ee;border:1px solid #86efac;padding:10px 14px;border-radius:8px;margin:10px 0}
.warn{background:#fef9c3;border:1px solid #fde047;padding:10px 14px;border-radius:8px;margin:10px 0}
.err{background:#fee2e2;border:1px solid #fca5a5;padding:10px 14px;border-radius:8px;margin:10px 0}
select,input[type=date]{padding:7px;border:1px solid #cbd5e1;border-radius:6px;font-family:inherit}
input.num{text-align:right}td input[type=text]{width:100%;box-sizing:border-box}
</style>"""

DUE_CELL = r"""{% macro due(f) %}{% set dl = days_left(f) %}{% if f.due %}{{f.due|ukdate}}{% if not is_done(f) and dl is not none %}<br>{% if dl < 0 %}<span class="pill late">{{-dl}} days late</span>{% elif dl <= 21 %}<span class="pill soon">{{dl}} days</span>{% elif dl <= 60 %}<span class="pill">{{dl}} days</span>{% endif %}{% endif %}{% else %}–{% endif %}{% endmacro %}
{% macro stage(f) %}<span class="pill {{'ok' if f.status in DONE else ('late' if f.status in ('rejected','error') else '')}}">{{ HMRC_STAGES.get(f.status) or CH_STATUS.get(f.status, f.status) }}</span>{% if f.mode %} <span class="why">{{f.mode|upper}}</span>{% endif %}{% endmacro %}"""

HOME = r"""{% extends "base" %}{% block body %}""" + STYLE + DUE_CELL + r"""
<div class="card"><h1 style="margin-bottom:4px">UK Tax <span class="muted" style="font-size:15px">– accounts, confirmation statements, Corporation Tax and Self Assessment</span></h1>
<p class="muted" style="margin-top:0">Companies House filings (accounts and confirmation statements) are sent <b>directly</b> with our presenter account.
HMRC returns (CT600, SA800, SA100) are prepared and tracked here and filed with your HMRC software for now.</p>
<div class="btnrow">
<span class="pill {{ 'ok' if live_set else 'late' }}">Presenter account {{ 'set' if live_set else 'NOT set' }}</span>
<span class="pill {{ 'ok' if s.accounts_live_approved else '' }}">Accounts: {{ 'LIVE approved' if s.accounts_live_approved else 'TEST only (awaiting CH approval)' }}</span>
<span class="pill {{ 'ok' if s.cs_live_approved else '' }}">CS01: {{ 'LIVE approved' if s.cs_live_approved else 'TEST only (awaiting CH approval)' }}</span>
<span class="pill {{ 'ok' if api_key else '' }}">Companies House lookup {{ 'on' if api_key else 'off (no CH_API_KEY)' }}</span></div>
<div class="tabs" style="margin-top:12px">{% for k, v in GROUPS.items() %}<a href="/uktax?tab={{k}}" class="{{'on' if tab==k}}">{{v}}</a>{% endfor %}
<a href="/uktax/export.csv" style="margin-left:auto">⬇ Export (CSV)</a></div>
{% if tab != 'clients' %}
<table class="t"><tr><th>Client</th><th>Filing</th><th>Period / year</th><th>Due</th><th>Status</th><th></th></tr>
{% for r in rows %}<tr><td><b>{{r.c.name}}</b><br><span class="why">{{KINDS[r.c.kind]}}{% if r.c.company_number %} · {{r.c.company_number}}{% endif %}{% if r.c.utr %} · UTR {{r.c.utr}}{% endif %}</span></td>
<td><span class="pill {{'ch' if FT[r.f.type].auth=='CH' else 'hmrc'}}">{{FT[r.f.type].auth}}</span> {{FT[r.f.type].label}}
{% if r.f.status=='rejected' and r.f.rejections %}<div class="why" style="color:#b91c1c">⚠ {{r.f.rejections[0]}}</div>{% endif %}</td>
<td>{{period_label(r.f)}}</td><td>{{due(r.f)}}</td><td>{{stage(r.f)}}</td>
<td><a class="btn small" href="/uktax/c/{{r.c.id}}/f/{{r.f.id}}">Open</a></td></tr>
{% else %}<tr><td colspan="6" class="muted">Nothing here. Add a client under "Clients", then add its filings.</td></tr>{% endfor %}</table>
{% else %}
<div class="fgrid" style="align-items:start">
<form method="post" action="/uktax/new" class="card" style="margin:0"><h3 style="margin-top:0">🏢 Add a company / LLP</h3>
<input type="hidden" name="kind" value="company">
<label>Company number</label><input type="text" name="company_number" placeholder="e.g. 12345678 or OC123456" required>
<p class="why">Name, accounts dates, confirmation statement date, directors and SIC codes are loaded from Companies House.</p>
<button class="btn">Add and load from Companies House</button></form>
<form method="post" action="/uktax/new" class="card" style="margin:0"><h3 style="margin-top:0">👤 Add an individual / partnership</h3>
<label>Type</label><select name="kind" style="width:100%"><option value="individual">Individual – Self Assessment (SA100)</option><option value="partnership">Partnership – SA800</option></select>
<label>Name</label><input type="text" name="name" required>
<button class="btn" style="margin-top:8px">Add</button></form></div>
<h3>Clients ({{clients|length}})</h3>
<table class="t"><tr><th>Client</th><th>Type</th><th>Number / UTR</th><th>Open filings</th><th></th></tr>
{% for c in clients %}<tr><td><b>{{c.name}}</b>{% if c.ch_status and c.ch_status!='active' %} <span class="pill late">{{c.ch_status}}</span>{% endif %}</td><td>{{KINDS[c.kind]}}</td>
<td>{{c.company_number}}{% if c.utr %}<br><span class="why">UTR {{c.utr}}</span>{% endif %}</td>
<td>{% for f in c.filings if not is_done(f) %}{{FT[f.type].label.split(' (')[0]}}{% if not loop.last %} · {% endif %}{% else %}<span class="why">none</span>{% endfor %}</td>
<td><a class="btn small" href="/uktax/c/{{c.id}}">Open</a></td></tr>{% else %}<tr><td colspan="5" class="muted">No clients yet.</td></tr>{% endfor %}</table>
<details style="margin-top:18px"><summary><b>Settings</b></summary>
<form method="post" action="/uktax/settings"><div class="fgrid">
<div><label>FRC taxonomy year (accounts)</label><select name="taxonomy" style="width:100%">{% for y in ('2024','2025','2026') %}<option {{'selected' if s.taxonomy==y}}>{{y}}</option>{% endfor %}</select></div>
<div><label>Notify email (accepted / rejected)</label><input type="text" name="notify_email" value="{{s.notify_email}}"></div>
<div><label>Firm name (client emails)</label><input type="text" name="firm_name" value="{{s.firm_name}}"></div>
<div><label>Firm email (client emails)</label><input type="text" name="firm_email" value="{{s.firm_email}}"></div></div>
<label><input type="checkbox" name="accounts_live_approved" {{'checked' if s.accounts_live_approved}}> Companies House has approved our software for <b>Accounts</b> (unlocks LIVE)</label><br>
<label><input type="checkbox" name="cs_live_approved" {{'checked' if s.cs_live_approved}}> Companies House has approved our software for <b>Confirmation statements</b> (unlocks LIVE)</label>
<p class="why">Presenter ID, package reference ({{fm_s.package_reference}}) and the submission-number counter ({{fm_s.next_submission}}) are shared with UK Formations → Settings. {{VERSION}}</p>
<button class="btn">Save settings</button></form></details>
{% endif %}</div>
{% endblock %}"""

CLIENT = r"""{% extends "base" %}{% block body %}""" + STYLE + DUE_CELL + r"""
<div class="card"><p><a href="/uktax?tab=clients">← UK Tax</a></p>
<h1 style="margin-bottom:2px">{{c.name}}</h1><p class="muted" style="margin-top:0">{{KINDS[c.kind]}}{% if c.company_number %} · {{c.company_number}}{% endif %}{% if c.ch_status %} · {{c.ch_status}}{% endif %}{% if c.ch_loaded %} · loaded from CH {{c.ch_loaded|ukdate}}{% endif %}</p>
{% if c.kind in ('company','llp') %}<form method="post" action="/uktax/c/{{c.id}}/ch" style="display:inline"><button class="btn small secondary" {{'disabled' if not api_key}}>↻ Refresh from Companies House</button></form>{% endif %}

<h2 id="filings">Filings</h2>
<table class="t"><tr><th>Filing</th><th>Period / year</th><th>Due</th><th>Status</th><th></th></tr>
{% for f in c.filings|sort(attribute='created', reverse=True) %}<tr><td><span class="pill {{'ch' if FT[f.type].auth=='CH' else 'hmrc'}}">{{FT[f.type].auth}}</span> {{FT[f.type].label}}</td>
<td>{{period_label(f)}}</td><td>{{due(f)}}</td><td>{{stage(f)}}</td><td><a class="btn small" href="/uktax/c/{{c.id}}/f/{{f.id}}">Open</a></td></tr>
{% else %}<tr><td colspan="5" class="muted">No filings yet – add one below.</td></tr>{% endfor %}</table>
<form method="post" action="/uktax/c/{{c.id}}/add" class="btnrow" style="margin-top:10px">
<select name="type">{% for k, t in FT.items() if c.kind in t['for'] %}<option value="{{k}}" {{'selected' if suggested and k==suggested[0]}}>{{t.label}}</option>{% endfor %}</select>
<button class="btn">+ Add filing</button>{% if suggested %}<span class="why">Suggested: {% for t in suggested %}{{FT[t].label}}{% if not loop.last %}, {% endif %}{% endfor %}</span>{% endif %}</form>
</div>

<div class="card"><h2>Client details</h2>
<form method="post" action="/uktax/c/{{c.id}}/save"><div class="fgrid">
<div><label>Name</label><input type="text" name="name" value="{{c.name}}"></div>
<div><label>Type</label><select name="kind" style="width:100%">{% for k, v in KINDS.items() %}<option value="{{k}}" {{'selected' if c.kind==k}}>{{v}}</option>{% endfor %}</select></div>
{% if c.kind in ('company','llp') %}
<div><label>Company number</label><input type="text" name="company_number" value="{{c.company_number}}"></div>
<div><label>Company authentication code</label><input type="password" name="auth_code" value="{{c.auth_code}}" autocomplete="off" placeholder="6 characters – needed to file">
<div class="why">From the letter Companies House sent the company. Needed for accounts and CS01.</div></div>
<div><label>Incorporated</label><input type="date" name="incorporated" value="{{c.incorporated}}"></div>
<div><label>Next accounts period start</label><input type="date" name="accounts_next_start" value="{{c.accounts_next_start}}"></div>
<div><label>Next accounts made up to</label><input type="date" name="accounts_next_end" value="{{c.accounts_next_end}}"></div>
<div><label>Accounts due (CH)</label><input type="date" name="accounts_due" value="{{c.accounts_due}}"></div>
<div><label>Next confirmation date</label><input type="date" name="cs_next_date" value="{{c.cs_next_date}}"></div>
<div><label>Confirmation statement due</label><input type="date" name="cs_due" value="{{c.cs_due}}"></div>
<div><label>SIC codes</label><input type="text" name="sic" value="{{c.sic}}"></div>
<div><label>Registered email address</label><input type="text" name="registered_email" value="{{c.registered_email}}"></div>
{% endif %}
<div><label>UTR</label><input type="text" name="utr" value="{{c.utr}}"></div>
{% if c.kind == 'individual' %}<div><label>NI number</label><input type="text" name="ni_number" value="{{c.ni_number}}"></div>{% endif %}
<div><label>Contact name</label><input type="text" name="contact" value="{{c.contact}}"></div>
<div><label>Client email</label><input type="text" name="email" value="{{c.email}}"></div>
<div><label>Phone</label><input type="text" name="phone" value="{{c.phone}}"></div>
<div style="grid-column:1/-1"><label>Address</label><input type="text" name="address" value="{{c.address}}"></div>
</div>
{% if c.officers %}<p class="why">Officers (CH): {% for o in c.officers %}{{o.forename}} {{o.surname}} ({{o.role}}){% if not loop.last %} · {% endif %}{% endfor %}</p>{% endif %}
{% if c.kind in ('partnership','llp') %}<h3>Partners / members</h3>
<table class="t"><tr><th>Name</th><th>UTR</th><th>Profit share %</th></tr>
{% set ps = c.partners + [{}, {}] %}<input type="hidden" name="np" value="{{ps|length}}">
{% for p in ps %}<tr><td><input type="text" name="p{{loop.index0}}_name" value="{{p.name}}"></td><td><input type="text" name="p{{loop.index0}}_utr" value="{{p.utr}}"></td><td><input type="text" class="num" name="p{{loop.index0}}_share" value="{{p.share}}"></td></tr>{% endfor %}</table>{% endif %}
<label>Notes</label><textarea name="notes" rows="3" style="width:100%">{{c.notes}}</textarea>
<button class="btn" style="margin-top:8px">Save details</button></form>
<details style="margin-top:14px"><summary class="why">Delete client</summary>
<form method="post" action="/uktax/c/{{c.id}}/delete" onsubmit="return confirm('Delete this client and its filings?')"><input type="password" name="password" placeholder="delete password"> <button class="btn small secondary">Delete</button></form></details>
</div>{% endblock %}"""

FILING = r"""{% extends "base" %}{% block body %}""" + STYLE + DUE_CELL + r"""
{% set ch = FT[f.type].auth == 'CH' %}{% set locked = ch and f.status in ('submitted','pending','parked','accepted') %}
<div class="card"><p><a href="/uktax/c/{{c.id}}">← {{c.name}}</a></p>
<h1 style="margin-bottom:2px"><span class="pill {{'ch' if ch else 'hmrc'}}">{{FT[f.type].auth}}</span> {{FT[f.type].label}}</h1>
<p class="muted" style="margin-top:0">{{c.name}}{% if c.company_number %} · {{c.company_number}}{% endif %} · {{period_label(f)}} · due {{due(f)}} · {{stage(f)}}</p>
{% if f.other_dates %}<p class="why">{% for k, v in f.other_dates %}{{k}}: <b>{{v|ukdate}}</b>{% if not loop.last %} · {% endif %}{% endfor %}</p>{% endif %}
{% if f.status=='rejected' %}<div class="err"><b>Rejected by Companies House</b><ul>{% for r in f.rejections %}<li>{{r}}</li>{% endfor %}</ul>{% if f.examiner %}Examiner: {{f.examiner}}{% endif %}
<form method="post" action="/uktax/c/{{c.id}}/f/{{f.id}}/reopen"><button class="btn small">Re-open to correct</button></form></div>{% endif %}
{% if f.status=='error' %}<div class="err">Not sent – {{ (f.log|last).text if f.log }} <form method="post" action="/uktax/c/{{c.id}}/f/{{f.id}}/reopen" style="display:inline"><button class="btn small">Re-open</button></form></div>{% endif %}
{% if errs and not locked %}<div class="err"><b>To fix before filing:</b><ul>{% for e in errs %}<li>{{e}}</li>{% endfor %}</ul></div>{% endif %}
{% if warns and not locked %}<div class="warn"><b>Please check:</b><ul>{% for e in warns %}<li>{{e}}</li>{% endfor %}</ul></div>{% endif %}
</div>

<form method="post" action="/uktax/c/{{c.id}}/f/{{f.id}}/save"><fieldset style="border:0;padding:0;margin:0" {{'disabled' if locked}}>
{% if f.type.startswith('ACC_') %}
<div class="card"><h2>Accounts</h2><div class="fgrid">
<div><label>Period start</label><input type="date" name="d_start" value="{{d.start}}"></div>
<div><label>Balance sheet date (period end)</label><input type="date" name="d_end" value="{{d.end}}"></div>
<div><label>Approved by the directors on</label><input type="date" name="d_approved" value="{{d.approved}}"></div>
<div><label>Director signing</label><input type="text" name="d_signer" value="{{d.signer}}" list="officers">
<datalist id="officers">{% for o in c.officers %}<option value="{{o.forename}} {{o.surname}}">{% endfor %}</datalist></div>
{% if f.type!='ACC_DORMANT' %}<div><label>Average employees (incl. directors)</label><input type="text" class="num" name="d_employees" value="{{d.employees}}"></div>{% else %}
<div><label>Trading status</label><select name="d_trading_status" style="width:100%"><option value="never" {{'selected' if d.trading_status=='never'}}>Dormant – has never traded</option><option value="past" {{'selected' if d.trading_status!='never'}}>Dormant – traded in the past</option></select></div>{% endif %}
</div>
<input type="hidden" name="present_first_year" value="1"><label><input type="checkbox" name="d_first_year" {{'checked' if d.first_year}}> First accounts (no comparative figures)</label>
<h3>Balance sheet <span class="why">whole pounds – enter liabilities as positive numbers</span></h3>
<table class="t"><tr><th></th><th class="r">This period</th>{% if not d.first_year %}<th class="r">Previous period</th>{% endif %}</tr>
{% for k, lab, sg in LINES %}<tr><td>{{lab}}</td><td><input type="text" class="num" name="d_{{k}}" value="{{d[k]}}"></td>{% if not d.first_year %}<td><input type="text" class="num" name="d_py_{{k}}" value="{{d['py_'+k]}}"></td>{% endif %}</tr>{% endfor %}
<tr><td>Other reserves</td><td><input type="text" class="num" name="d_other_reserves" value="{{d.other_reserves}}"></td>{% if not d.first_year %}<td><input type="text" class="num" name="d_py_other_reserves" value="{{d.py_other_reserves}}"></td>{% endif %}</tr>
<tr><td><b>Net current assets</b></td><td class="r">{{money(g.ncl)}}</td>{% if not d.first_year %}<td class="r">{{money(gp.ncl)}}</td>{% endif %}</tr>
<tr class="tot"><td>Net assets = shareholders' funds</td><td class="r">{{money(g.net)}}</td>{% if not d.first_year %}<td class="r">{{money(gp.net)}}</td>{% endif %}</tr>
<tr><td>Profit and loss account (balancing figure)</td><td class="r">{{money(g.reserves)}}</td>{% if not d.first_year %}<td class="r">{{money(gp.reserves)}}</td>{% endif %}</tr></table>
<p class="btnrow"><a class="btn small secondary" href="/uktax/c/{{c.id}}/f/{{f.id}}/accounts.html" target="_blank">👁 View accounts</a>
<a class="btn small secondary" href="/uktax/c/{{c.id}}/f/{{f.id}}/accounts.html?download=1">⬇ Download iXBRL</a></p></div>
{% elif f.type=='CS01' %}
<div class="card"><h2>Confirmation statement</h2><div class="fgrid">
<div><label>Confirmation date (made up to)</label><input type="date" name="d_review" value="{{d.review}}"><div class="why">Due date per CH: {{c.cs_next_date|ukdate if c.cs_next_date else '–'}}</div></div></div>
<p class="why">Default is <b>"no changes"</b> – just the date, the lawful purpose statement and the confirmation. Tick a box below only when something changed.</p>
<input type="hidden" name="present_sic_change" value="1"><label><input type="checkbox" name="d_sic_change" {{'checked' if d.sic_change}}> SIC codes changed</label>
<input type="text" name="d_sic" value="{{d.sic}}" placeholder="e.g. 62020, 70229">
<input type="hidden" name="present_email_change" value="1"><label style="margin-top:8px"><input type="checkbox" name="d_email_change" {{'checked' if d.email_change}}> Give / change the registered email address</label>
<input type="text" name="d_registered_email" value="{{d.registered_email}}">
{% if c.kind!='llp' %}
<input type="hidden" name="present_capital_change" value="1"><label style="margin-top:8px"><input type="checkbox" name="d_capital_change" {{'checked' if d.capital_change}}> Share capital or shareholders changed (full list required)</label>
<table class="t"><tr><th>Share class</th><th>Number issued</th><th>Nominal value each (£)</th><th>Unpaid each (£)</th><th>Rights (prescribed particulars)</th></tr>
{% set cls = d.classes + [{}] %}<input type="hidden" name="nc" value="{{cls|length}}">
{% for x in cls %}<tr><td><input type="text" name="c{{loop.index0}}_cls" value="{{x.cls}}"></td><td><input type="text" class="num" name="c{{loop.index0}}_num" value="{{x.num}}"></td>
<td><input type="text" class="num" name="c{{loop.index0}}_nominal" value="{{x.nominal}}"></td><td><input type="text" class="num" name="c{{loop.index0}}_unpaid" value="{{x.unpaid}}"></td>
<td><input type="text" name="c{{loop.index0}}_particulars" value="{{x.particulars}}"></td></tr>{% endfor %}</table>
<table class="t"><tr><th>Shareholder name</th><th>Company?</th><th>Class</th><th>Shares held</th><th>Transfer date</th><th>Shares transferred</th></tr>
{% set hs = d.holders + [{}, {}] %}<input type="hidden" name="nh" value="{{hs|length}}">
{% for h in hs %}<tr><td><input type="text" name="h{{loop.index0}}_name" value="{{h.name}}"></td><td><input type="checkbox" name="h{{loop.index0}}_corporate" {{'checked' if h.corporate}}></td>
<td><input type="text" name="h{{loop.index0}}_cls" value="{{h.cls or 'ORDINARY'}}"></td><td><input type="text" class="num" name="h{{loop.index0}}_num" value="{{h.num}}"></td>
<td><input type="date" name="h{{loop.index0}}_transfer_date" value="{{h.transfer_date}}"></td><td><input type="text" class="num" name="h{{loop.index0}}_transfer_num" value="{{h.transfer_num}}"></td></tr>{% endfor %}</table>
{% endif %}
<p class="why">Sent with every statement: "The company confirms that its intended future activities are lawful" and that all information required to be delivered has been delivered.
Officers, PSCs and registered office changes are filed separately (Company forms tab).</p></div>
{% elif f.type=='CT600' %}
<div class="card"><h2>Corporation Tax computation</h2><div class="fgrid">
<div><label>Period start</label><input type="date" name="d_start" value="{{d.start}}"></div>
<div><label>Period end</label><input type="date" name="d_end" value="{{d.end}}"></div>
<div><label>Associated companies</label><input type="text" class="num" name="d_associated" value="{{d.associated}}"></div></div>
{% if ct.long %}<div class="warn">The period is longer than 12 months – a CT600 can cover at most 12 months. Split it into two returns.</div>{% endif %}
<table class="t">
{% for k, lab in [('profit','Profit (loss) before tax per accounts'),('depreciation','Add: depreciation / amortisation'),('disallowable','Add: disallowable expenses (entertaining, fines, private use…)'),('other_addbacks','Add: other'),('capital_allowances','Less: capital allowances (AIA etc.)'),('other_deductions','Less: other deductions'),('losses_bf','Trading losses brought forward'),('income_other','Other taxable income (interest, property, gains)'),('ct_paid','Corporation Tax already paid')] %}
<tr><td>{{lab}}</td><td style="width:180px"><input type="text" class="num" name="d_{{k}}" value="{{d[k]}}"></td></tr>{% endfor %}
<tr><td>Adjusted trading profit (loss)</td><td class="r">{{money(ct.trading)}}</td></tr>
<tr><td>Losses used</td><td class="r">{{money(ct.losses_used)}}</td></tr>
<tr class="tot"><td>Taxable total profits</td><td class="r">£{{money(ct.ttp)}}</td></tr>
<tr><td>Limits for {{ct.days}} days ({{d.associated or 0}} associated): lower £{{money(ct.lower)}} · upper £{{money(ct.upper)}}</td><td class="r">{{ct.rate_note}}</td></tr>
{% if ct.mr %}<tr><td>Marginal relief (3/200)</td><td class="r">{{money(ct.mr, 2)}}</td></tr>{% endif %}
<tr class="tot"><td>Corporation Tax payable</td><td class="r">£{{money(ct.tax, 2)}}</td></tr>
<tr><td>Balance to pay</td><td class="r">£{{money(ct.balance, 2)}}</td></tr>
{% if ct.loss_cf > 0 %}<tr><td>Trading losses carried forward</td><td class="r">£{{money(ct.loss_cf)}}</td></tr>{% endif %}</table>
<p class="why">Rates for periods from 1 April 2023: 19% up to the lower limit, 25% above the upper limit, marginal relief between. Limits are divided by
(1 + associated companies) and reduced for short periods. Check the figures before entering them in your HMRC filing software.</p></div>
{% elif f.type=='SA100' %}
<div class="card"><h2>Self Assessment figures</h2><div class="fgrid">
<div><label>Tax year</label><select name="d_tax_year" style="width:100%">{% for y in years %}<option {{'selected' if d.tax_year==y}}>{{y}}</option>{% endfor %}</select></div></div>
<table class="t">
{% for k, lab in [('employment','Employment income (P60 pay + taxable benefits)'),('paye','Tax deducted under PAYE'),('self_employment','Self-employment profit (after expenses)'),('property','UK property profit'),('savings','Bank / building society interest'),('dividends','Dividends'),('other','Other taxable income'),('pension_gross','Personal pension contributions (gross) + Gift Aid (gross)'),('student_loan','Student loan repayment due (from SA calculation)'),('poa_paid','Payments on account already made for this year')] %}
<tr><td>{{lab}}</td><td style="width:180px"><input type="text" class="num" name="d_{{k}}" value="{{d[k]}}"></td></tr>{% endfor %}</table>
{% if sa %}<h3>Estimate ({{sa.rates_year}}, England / Wales / NI rates)</h3><table class="t">
<tr><td>Total income</td><td class="r">{{money(sa.income)}}</td></tr><tr><td>Personal allowance</td><td class="r">{{money(sa.pa)}}</td></tr>
{% for lab, amt, t in sa.lines %}<tr><td>{{lab}} on £{{money(amt)}}</td><td class="r">{{money(t, 2)}}</td></tr>{% endfor %}
<tr><td>Income tax</td><td class="r">{{money(sa.income_tax, 2)}}</td></tr><tr><td>Class 4 National Insurance</td><td class="r">{{money(sa.class4, 2)}}</td></tr>
{% if sa.student_loan %}<tr><td>Student loan</td><td class="r">{{money(sa.student_loan, 2)}}</td></tr>{% endif %}
<tr><td>Less PAYE</td><td class="r">({{money(sa.paye, 2)}})</td></tr>
<tr class="tot"><td>Tax due for the year</td><td class="r">£{{money(sa.due, 2)}}</td></tr>
<tr><td>Less payments on account made</td><td class="r">({{money(d.poa_paid or 0, 2)}})</td></tr>
<tr class="tot"><td>Balancing payment</td><td class="r">£{{money(sa.balancing, 2)}}</td></tr>
{% if sa.poa %}<tr><td>Payments on account for next year (each, 31 Jan and 31 Jul)</td><td class="r">£{{money(sa.poa, 2)}}</td></tr>{% endif %}</table>
<p class="why">An estimate for checking only – Scottish rates, the High Income Child Benefit Charge, capital gains, marriage allowance and foreign income are not included.</p>{% endif %}</div>
{% elif f.type=='SA800' %}
<div class="card"><h2>Partnership return figures</h2><div class="fgrid">
<div><label>Tax year</label><input type="text" name="d_tax_year" value="{{d.tax_year}}" placeholder="2025-26"></div>
<div><label>Accounting period start</label><input type="date" name="d_period_start" value="{{d.period_start}}"></div>
<div><label>Accounting period end</label><input type="date" name="d_period_end" value="{{d.period_end}}"></div>
<div><label>Turnover</label><input type="text" class="num" name="d_turnover" value="{{d.turnover}}"></div>
<div><label>Allowable expenses</label><input type="text" class="num" name="d_expenses" value="{{d.expenses}}"></div>
<div><label>Taxable profit (leave blank = turnover − expenses)</label><input type="text" class="num" name="d_profit" value="{{d.profit}}"></div></div>
<h3>Profit split</h3><table class="t"><tr><th>Partner</th><th>UTR</th><th class="r">Share %</th><th class="r">Profit</th></tr>
{% set ps = d.partners + [{}] %}<input type="hidden" name="np" value="{{ps|length}}">
{% for p in ps %}<tr><td><input type="text" name="p{{loop.index0}}_name" value="{{p.name}}"></td><td><input type="text" name="p{{loop.index0}}_utr" value="{{p.utr}}"></td>
<td><input type="text" class="num" name="p{{loop.index0}}_share" value="{{p.share}}"></td><td class="r">{% for r in sp.rows if r.name==p.name %}{{money(r.amount)}}{% endfor %}</td></tr>{% endfor %}
<tr class="tot"><td>Total</td><td></td><td class="r">{{sp.total_share}}</td><td class="r">{{money(sp.profit)}}</td></tr></table>
{% if sp.rows and sp.total_share != 100 %}<div class="warn">Shares add up to {{sp.total_share}}%, not 100%.</div>{% endif %}
<p class="why">Each partner also needs their own SA100 with the partnership pages – add them as individuals.</p></div>
{% endif %}

<div class="card"><h2>Progress</h2><div class="fgrid">
{% if not ch %}<div><label>Stage</label><select name="status" style="width:100%">{% for k, v in HMRC_STAGES.items() %}<option value="{{k}}" {{'selected' if f.status==k}}>{{v}}</option>{% endfor %}</select></div>
<div><label>Date filed</label><input type="date" name="filed_date" value="{{f.filed_date}}"></div>
<div><label>HMRC submission reference / IRmark</label><input type="text" name="reference" value="{{f.reference}}"></div>
{% else %}{% if f.status in ('draft','filed_elsewhere','not_needed') %}<div><label>Status</label><select name="status" style="width:100%">
{% for k in ('draft','filed_elsewhere','not_needed') %}<option value="{{k}}" {{'selected' if f.status==k}}>{{CH_STATUS[k]}}</option>{% endfor %}</select><div class="why">"Filed elsewhere" if it was filed with other software / WebFiling.</div></div>{% endif %}{% endif %}
</div><label>Notes</label><textarea name="notes" rows="2" style="width:100%">{{f.notes}}</textarea>
<button class="btn" style="margin-top:8px">💾 Save</button></fieldset></form>
{% if not ch %}<form method="post" action="/uktax/c/{{c.id}}/f/{{f.id}}/request_info" style="margin-top:8px" onsubmit="return confirm('Email the information checklist to {{c.email or 'the client'}}?')">
<button class="btn small secondary" {{'disabled' if not c.email}}>✉ Email the client the information checklist</button>{% if not c.email %} <span class="why">add the client email first</span>{% endif %}{% if f.info_requested_at %} <span class="why">last sent {{f.info_requested_at|ukdate}}</span>{% endif %}</form>{% endif %}
</div>

{% if ch %}<div class="card"><h2>File with Companies House</h2>
{% if f.submission_number %}<p>Submission <b>{{f.submission_number}}</b> ({{(f.mode or '')|upper}}) sent {{f.submitted_at|ukdate}}{% if f.last_poll %} · last checked {{f.last_poll|ukdate}}{% endif %}</p>{% endif %}
{% if f.status in ('submitted','pending','parked') %}<form method="post" action="/uktax/c/{{c.id}}/f/{{f.id}}/poll"><button class="btn small">↻ Check status now</button> <span class="why">checked automatically every 10 minutes</span></form>{% endif %}
{% if f.status in ('draft',) %}
<p><a class="btn small secondary" href="/uktax/c/{{c.id}}/f/{{f.id}}/xml" target="_blank">Preview XML + official schema check</a></p>
<form method="post" action="/uktax/c/{{c.id}}/f/{{f.id}}/submit">
<label><input type="checkbox" name="approved"> The {{ 'directors approved these accounts' if f.type.startswith('ACC_') else 'company confirmed these details' }} and asked us to file</label>
<div class="btnrow" style="margin-top:8px"><button class="btn secondary" name="mode" value="test" {{'disabled' if errs or not creds_ok}}>Send TEST (nothing is filed)</button>
<input type="text" name="confirm_live" placeholder="type LIVE" style="width:110px"><button class="btn" name="mode" value="live" {{'disabled' if errs or not live_ok}}>File LIVE with Companies House</button></div>
{% if not live_ok %}<p class="why">LIVE is locked until Companies House approves our software for this form (UK Tax → Clients → Settings).</p>{% endif %}</form>{% endif %}
{% if files %}<p class="why">Files: {% for n in files %}<a href="/uktax/c/{{c.id}}/f/{{f.id}}/download/{{n}}">{{n}}</a>{% if not loop.last %} · {% endif %}{% endfor %}</p>{% endif %}
</div>{% endif %}

<div class="card"><h2>History</h2>{% for l in f.log|reverse %}<div class="why">{{l.t|ukdate}} – {{l.text}}</div>{% else %}<p class="why">Nothing yet.</p>{% endfor %}
{% if f.status not in ('submitted','pending','parked','accepted','filed','approved') %}<form method="post" action="/uktax/c/{{c.id}}/f/{{f.id}}/remove" onsubmit="return confirm('Remove this filing?')" style="margin-top:10px"><button class="btn small secondary">Remove filing</button></form>{% endif %}
</div>{% endblock %}"""
