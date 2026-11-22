"""
Tracker tab (v46a) – one place for every client company's deadlines: UK, US, other countries and Self Assessment.

  /tracker                    -> Deadlines: overdue / due in 7 / due in 30 days, filters, status + note per deadline
  /tracker/companies          -> every company: Companies House status, next accounts / CS / VAT, Shopify, WhatsApp group
  /tracker/c/<cid>            -> one company: details (edit), its deadlines, reminder drafts, own extra tasks, history
  /tracker/new                -> add a company by hand
  /tracker/import             -> bring in the tracker workbook (.xlsx downloaded from the Claude Sheet) or a CSV
  /tracker/export.csv         -> every open deadline as CSV
  /tracker/refresh   POST     -> Companies House refresh of all UK companies (background)
  /tracker/shopify   POST     -> Shopify customer check of all active companies (background)
  /tracker/settings           -> reminder wording, CS order link, reseller e-mail map for the Shopify check

How deadlines are worked out (nothing is typed twice):
  UK company   Accounts + confirmation statement due dates from Companies House (CH_API_KEY).
               CT payment = period end + 9 months + 1 day, CT600 = period end + 12 months (only if we do ACC/CT).
               VAT return = 1 month + 7 days after each quarter end, from the VAT stagger set on the company.
  US LLC       State annual filing from the state's rule (fixed date, or the formation anniversary),
               Form 5472 + pro forma 1120 every 15 April.
  Individual   SA return + balancing payment 31 Jan, 2nd payment on account 31 Jul.
  Any company  Extra tasks added by staff on the company page.
A status (To do / In progress / Waiting on client / Done / Not our job) is stored per company + deadline + due date,
so when the next period's date comes round the deadline starts again as "To do".

SAFETY: nothing is sent automatically. A reminder e-mail only goes when staff press "E-mail reminder" and confirm.
Data: DATA/tracker/tracker.json (the Railway volume – never in this public repo).

Railway → Variables (optional):
  CH_API_KEY                       Companies House REST key (already used by UK Tax)
  SHOPIFY_STORE                    e.g. your-store.myshopify.com
  SHOPIFY_ADMIN_TOKEN              Admin API access token (custom app, scope read_customers)
  TRACKER_NO_AUTO=1                switches off the daily automatic Companies House + Shopify refresh
"""
from __future__ import annotations

import base64, calendar, csv, datetime as dt, io, json, os, re, threading, time, urllib.error, urllib.request, uuid
from pathlib import Path

from flask import Blueprint, Response, abort, flash, jsonify, redirect, render_template_string, request

VERSION = "v46a"
ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "tracker"
STORE = OUT / "tracker.json"

bp = Blueprint("tracker", __name__)
_LOCK = threading.RLock()
_CFG = {"fn": None}
_SEND = {"fn": None}
_BG = {"ch": {"running": False, "msg": ""}, "shopify": {"running": False, "msg": ""}}
_AUTO = {"started": False}

STATUSES = ["To do", "In progress", "Waiting on client", "Done / filed", "Not our job"]
CLOSED = ("Done / filed", "Not our job")
JURIS = {"UK": "UK company", "US": "US company", "OTHER": "Other country", "SA": "Individual (Self Assessment)"}
VAT_STAGGERS = ["", "Monthly", "Mar/Jun/Sep/Dec", "Jan/Apr/Jul/Oct", "Feb/May/Aug/Nov"]
SHOPIFY_STATES = ["", "Found – company", "Found – via client/reseller", "Not in Shopify", "Own company"]

KINDS = {
    "ACC": "Annual accounts (Companies House)",
    "CS": "Confirmation statement",
    "CTPAY": "Corporation Tax payment",
    "CT600": "CT600 return",
    "VAT": "VAT return",
    "STATE": "State annual filing",
    "F5472": "Form 5472 + pro forma 1120",
    "SA": "SA return + balancing payment",
    "POA": "2nd payment on account",
    "TASK": "Task",
}

# state -> (label, rule) ; rule: ("fixed", month, day) | ("anniv_first",) | ("anniv_end",) | ("anniv",) | ("biennial_odd", m, d) | None
US_RULES = {
    "Montana": ("Annual report", ("fixed", 4, 15)),
    "Wyoming": ("Annual report – 1st day of anniversary month", ("anniv_first",)),
    "Delaware": ("LLC annual tax ($300)", ("fixed", 6, 1)),
    "Florida": ("Annual report", ("fixed", 5, 1)),
    "Texas": ("Franchise tax / PIR", ("fixed", 5, 15)),
    "Michigan": ("Annual statement", ("fixed", 2, 15)),
    "Iowa": ("Biennial report (odd years)", ("biennial_odd", 4, 1)),
    "Kentucky": ("Annual report", ("fixed", 6, 30)),
    "California": ("Franchise tax ($800) + Statement of Information", ("fixed", 4, 15)),
    "New Jersey": ("Annual report – end of anniversary month", ("anniv_end",)),
    "Oregon": ("Annual report – anniversary date", ("anniv",)),
    "New Mexico": ("No annual report for LLCs", None),
    "Missouri": ("No annual report for LLCs", None),
    "Ohio": ("No annual report for LLCs", None),
}

SETTINGS_DEFAULT = {
    "cs_link": "https://startitup.global/products/confirmation-statement-3",
    "sign_off": "Team Startitup",
    "remind_days": 30,
    "reseller_map": "",     # typed on the Tracker settings page – client e-mails never go in this public repo
}


# ----------------------------------------------------------------------------- storage

def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _today() -> dt.date:
    return dt.date.today()


def load() -> dict:
    with _LOCK:
        if STORE.exists():
            try:
                d = json.loads(STORE.read_text("utf-8"))
            except Exception:
                d = {}
        else:
            d = {}
        d.setdefault("companies", {})
        d.setdefault("status", {})
        d.setdefault("settings", {})
        d.setdefault("log", [])
        for k, v in SETTINGS_DEFAULT.items():
            d["settings"].setdefault(k, v)
        return d


def save(d: dict):
    with _LOCK:
        OUT.mkdir(parents=True, exist_ok=True)
        d["log"] = d.get("log", [])[-400:]
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, default=str), "utf-8")
        tmp.replace(STORE)


def _log(d: dict, msg: str, cid: str = ""):
    d.setdefault("log", []).append({"at": _now(), "cid": cid, "msg": msg})


def _norm(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\b(limited|ltd|llc|l\.l\.c|inc|plc|llp|co|company|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def find_company(d: dict, name: str, number: str = "") -> str | None:
    number = (number or "").strip().lstrip("'").upper()
    n = _norm(name)
    for cid, c in d["companies"].items():
        if number and c.get("number", "").upper() == number:
            return cid
    for cid, c in d["companies"].items():
        if n and _norm(c.get("name", "")) == n:
            return cid
    return None


# ----------------------------------------------------------------------------- dates

def pdate(v) -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            return None
    for f in ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def add_months(d: dt.date, m: int) -> dt.date:
    y = d.year + (d.month - 1 + m) // 12
    mo = (d.month - 1 + m) % 12 + 1
    return dt.date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def eom(y: int, m: int) -> dt.date:
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def next_vat_due(stagger: str, today: dt.date) -> tuple[dt.date, dt.date] | None:
    """(quarter end, due date) of the next VAT return not yet past its due date. Due = 1 month + 7 days after period end."""
    if not stagger:
        return None
    months = list(range(1, 13)) if stagger == "Monthly" else \
        {"Mar/Jun/Sep/Dec": [3, 6, 9, 12], "Jan/Apr/Jul/Oct": [1, 4, 7, 10], "Feb/May/Aug/Nov": [2, 5, 8, 11]}.get(stagger)
    if not months:
        return None
    for back in range(-14, 14):
        m0 = add_months(today.replace(day=1), back)
        if m0.month not in months:
            continue
        pe = eom(m0.year, m0.month)
        nxt = add_months(pe.replace(day=1), 1)
        due = eom(nxt.year, nxt.month) + dt.timedelta(days=7)
        if due >= today:
            return pe, due
    return None


def next_on_or_after(month: int, day: int, today: dt.date) -> dt.date:
    d = dt.date(today.year, month, min(day, calendar.monthrange(today.year, month)[1]))
    return d if d >= today - dt.timedelta(days=60) else dt.date(today.year + 1, month, min(day, calendar.monthrange(today.year + 1, month)[1]))


def us_state_due(state: str, formed: dt.date | None, today: dt.date) -> tuple[str, dt.date | None] | None:
    r = US_RULES.get(state)
    if not r:
        return ("State filing – check rule for " + (state or "unknown state"), None)
    label, rule = r
    if rule is None:
        return None
    kind = rule[0]
    if kind == "fixed":
        due = next_on_or_after(rule[1], rule[2], today)
        if formed and due.year <= formed.year:   # first report is the year after formation
            due = due.replace(year=formed.year + 1)
        return label, due
    if kind == "biennial_odd":
        due = next_on_or_after(rule[1], rule[2], today)
        if due.year % 2 == 0:
            due = due.replace(year=due.year + 1)
        return label, due
    if not formed:
        return label + " (add the formation date)", None
    y = max(today.year - (1 if today.month * 100 + today.day < formed.month * 100 + formed.day - 100 else 0), formed.year + 1)
    for yy in (y - 1, y, y + 1):
        if yy <= formed.year:
            continue
        if kind == "anniv_first":
            due = dt.date(yy, formed.month, 1)
        elif kind == "anniv_end":
            due = eom(yy, formed.month)
        else:
            due = dt.date(yy, formed.month, min(formed.day, calendar.monthrange(yy, formed.month)[1]))
        if due >= today - dt.timedelta(days=60):
            return label, due
    return label, None


def deadlines(c: dict, today: dt.date | None = None) -> list[dict]:
    """Every deadline of one company (generated + its own extra tasks)."""
    today = today or _today()
    out = []
    j = c.get("jurisdiction", "UK")
    svc = (c.get("services") or "").upper()

    def add(kind, due, note="", label=None):
        out.append({"kind": kind, "label": label or KINDS[kind], "due": due.isoformat() if due else "", "note": note})

    if not c.get("active", True):
        return []
    if j == "UK":
        ch = c.get("ch") or {}
        acc_due, made_up = pdate(ch.get("accounts_due") or c.get("accounts_due")), pdate(ch.get("made_up_to") or c.get("made_up_to"))
        cs_due = pdate(ch.get("cs_due") or c.get("cs_due"))
        if acc_due:
            add("ACC", acc_due, f"Period to {made_up:%d %b %Y}" if made_up else "")
        if cs_due:
            add("CS", cs_due)
        if made_up and (not svc or "ACC" in svc or "CT" in svc):
            add("CTPAY", add_months(made_up, 9) + dt.timedelta(days=1), f"Period to {made_up:%d %b %Y}")
            add("CT600", add_months(made_up, 12), f"Period to {made_up:%d %b %Y}")
        v = next_vat_due(c.get("vat", ""), today)
        if v:
            add("VAT", v[1], f"Period to {v[0]:%d %b %Y}")
    elif j == "US":
        s = us_state_due(c.get("state", ""), pdate(c.get("formed")), today)
        if s:
            out.append({"kind": "STATE", "label": s[0], "due": s[1].isoformat() if s[1] else "", "note": c.get("state", "")})
        add("F5472", next_on_or_after(4, 15, today), "Extendable to 15 Oct with Form 7004")
    elif j == "SA":
        add("SA", next_on_or_after(1, 31, today))
        add("POA", next_on_or_after(7, 31, today), "Only if payments on account apply")
    for t in c.get("tasks", []):
        if t.get("label"):
            out.append({"kind": "TASK", "label": t["label"], "due": t.get("due", ""), "note": t.get("note", ""), "tid": t.get("id")})
    return out


def tkey(cid: str, t: dict) -> str:
    return f"{cid}|{t['kind']}|{t.get('tid') or ''}|{t['due']}"


def all_rows(d: dict, today: dt.date | None = None) -> list[dict]:
    today = today or _today()
    rows = []
    for cid, c in d["companies"].items():
        for t in deadlines(c, today):
            k = tkey(cid, t)
            st = d["status"].get(k, {})
            due = pdate(t["due"])
            days = (due - today).days if due else None
            status = st.get("status", "To do")
            if days is None:
                alert = "no date"
            elif status in CLOSED:
                alert = "closed"
            elif days < 0:
                alert = "overdue"
            elif days <= 7:
                alert = "7"
            elif days <= 30:
                alert = "30"
            else:
                alert = "later"
            rows.append({**t, "key": k, "cid": cid, "company": c.get("name", ""), "jurisdiction": c.get("jurisdiction", ""),
                         "group": c.get("group", ""), "wa": c.get("wa_group", ""), "status": status,
                         "snote": st.get("note", ""), "updated": st.get("at", ""), "days": days, "alert": alert})
    rows.sort(key=lambda r: (r["due"] or "9999", r["company"].lower()))
    return rows


def alert_count() -> int:
    try:
        return sum(1 for r in all_rows(load()) if r["alert"] in ("overdue", "7"))
    except Exception:
        return 0


# ----------------------------------------------------------------------------- reminders

def reminder_text(r: dict, s: dict) -> str:
    due = pdate(r["due"])
    when = f"{due:%d %b %Y}" if due else "soon"
    late = r.get("days") is not None and r["days"] < 0
    head = f"Hi, this is a reminder from {s.get('sign_off') or 'Team Startitup'}. {r['company']}: {r['label']} "
    head += f"fell due on {when}." if late else f"is due on {when}."
    k = r["kind"]
    if k == "CS":
        body = (" Please confirm whether there are any changes to directors, shareholders, registered office or business "
                f"activity, and place the confirmation statement order here so we can file it on time: {s.get('cs_link')}")
    elif k in ("ACC", "CTPAY", "CT600"):
        body = " Please send us your bank statements, sales and purchase records for the period so we can prepare and file on time."
    elif k == "VAT":
        body = " Please send your sales and purchase invoices and bank statements for the quarter so we can prepare the VAT return."
    elif k in ("STATE", "F5472"):
        body = " Please confirm the company details are unchanged and send the year's bank statements so we can complete the filing."
    elif k in ("SA", "POA"):
        body = " Please send your income details for the year so we can complete your Self Assessment on time."
    else:
        body = " Please get in touch with us about this."
    return head + body + " Thank you."


# ----------------------------------------------------------------------------- Companies House

def ch_get(path: str) -> dict:
    key = os.environ.get("CH_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CH_API_KEY is not set in Railway → Variables.")
    req = urllib.request.Request("https://api.company-information.service.gov.uk" + path)
    req.add_header("Authorization", "Basic " + base64.b64encode((key + ":").encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def ch_refresh_one(c: dict) -> str:
    num = (c.get("number") or "").strip().upper()
    if not num:
        return "no company number"
    try:
        j = ch_get("/company/" + num)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            c["ch"] = {**(c.get("ch") or {}), "status": "not found", "refreshed": _now()}
            return "not found"
        raise
    acc, cs = j.get("accounts") or {}, j.get("confirmation_statement") or {}
    nxt = acc.get("next_accounts") or {}
    status = (j.get("company_status") or "").replace("-", " ")
    if j.get("company_status_detail"):
        status += " – " + j["company_status_detail"].replace("-", " ")
    c["ch"] = {"status": status, "name": j.get("company_name", ""), "incorporated": j.get("date_of_creation", ""),
               "accounts_due": acc.get("next_due") or nxt.get("due_on", ""), "made_up_to": acc.get("next_made_up_to") or nxt.get("period_end_on", ""),
               "accounts_overdue": bool(acc.get("overdue")), "cs_due": cs.get("next_due", ""), "cs_overdue": bool(cs.get("overdue")),
               "refreshed": _now()}
    if j.get("company_status") in ("dissolved", "converted-closed"):
        c["active"] = False
    return status


def _ch_job():
    st = _BG["ch"]
    try:
        d = load()
        ids = [cid for cid, c in d["companies"].items() if c.get("jurisdiction") == "UK" and c.get("number")]
        done = errs = 0
        for i, cid in enumerate(ids):
            st["msg"] = f"Companies House: {i + 1} of {len(ids)}"
            try:
                with _LOCK:
                    d2 = load()
                    c = d2["companies"].get(cid)
                    if c:
                        ch_refresh_one(c)
                        save(d2)
                done += 1
            except Exception as e:
                errs += 1
                if "CH_API_KEY" in str(e):
                    st["msg"] = str(e)
                    return
            time.sleep(0.6)     # CH allows 600 requests / 5 minutes
        with _LOCK:
            d2 = load()
            d2["ch_last"] = _now()
            _log(d2, f"Companies House refresh: {done} updated, {errs} errors")
            save(d2)
        st["msg"] = f"Companies House refresh finished: {done} updated, {errs} errors."
    finally:
        st["running"] = False


# ----------------------------------------------------------------------------- Shopify

GENERIC = set(("ltd limited llc uk global services service the and of group holdings holding international company co inc corp "
               "private pte trading solutions consultancy consulting enterprises enterprise ventures technologies technology "
               "tech digital media management partners prime plc associates london alpha airy alin allin astra brand builder "
               "business central clear cloud construction design designs electronics ecommerce exclusive features finds flick "
               "fluent fortune garment grow hello heaven house imran industrial interior insulation commercial falcon dreamy "
               "feather graf hamari dalton james kinetic laura lovemy mainline malak malik maroon marte matcha medicine midway "
               "mobile modena muhammad mughal naeem nadeem nexa noble ocean outsource pizza primes quality rides rocket safety "
               "select seven simb shaikh sonic squad stax strand strong sultan swiss timeless trade ultimate unity universal "
               "victory viking violet vision volta wave wealth xpert zahid zamzam reborn phantom startitup arshad sarwar "
               "advance best smart royal elite united home care quick total star city world premier golden gold apex nova "
               "north south east west build first green").split())


def shop_terms(name: str) -> list[str]:
    w = [x for x in re.findall(r"[a-z0-9]+", (name or "").lower().replace("'", "")) if x not in
         set("ltd limited llc uk global services service the and of group holdings holding international company co inc "
             "corp private pte trading solutions consultancy consulting enterprises enterprise ventures technologies "
             "technology tech digital media management partners prime plc associates london".split())]
    t = []
    if len(w) >= 2:
        t.append(w[0] + w[1])
    if w and len(w[0]) >= 5 and w[0] not in GENERIC:
        t.append(w[0])
    if not t and w:
        t.append("".join(w))
    return [x for x in t if len(x) >= 4]


def shopify_gql(query: str, variables: dict) -> dict:
    store = os.environ.get("SHOPIFY_STORE", "").strip().replace("https://", "").strip("/")
    tok = os.environ.get("SHOPIFY_ADMIN_TOKEN", "").strip()
    if not store or not tok:
        raise RuntimeError("SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN are not set in Railway → Variables.")
    req = urllib.request.Request(f"https://{store}/admin/api/2025-01/graphql.json",
                                 data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "X-Shopify-Access-Token": tok})
    with urllib.request.urlopen(req, timeout=40) as r:
        j = json.loads(r.read())
    if j.get("errors"):
        raise RuntimeError(str(j["errors"])[:300])
    return j["data"]


def shopify_search(queries: list[str]) -> list[dict]:
    hits = {}
    for i in range(0, len(queries), 4):
        part = queries[i:i + 4]
        q = "query(" + ",".join(f"$q{k}:String" for k in range(len(part))) + "){" + "".join(
            f"b{k}:customers(first:100,query:$q{k}){{nodes{{displayName defaultEmailAddress{{emailAddress}} numberOfOrders}}}}"
            for k in range(len(part))) + "}"
        data = shopify_gql(q, {f"q{k}": v for k, v in enumerate(part)})
        for b in data.values():
            for n in b["nodes"]:
                e = ((n.get("defaultEmailAddress") or {}).get("emailAddress") or "").strip()
                hits[(n.get("displayName") or "", e.lower())] = {"name": n.get("displayName") or "", "email": e,
                                                                "orders": int(n.get("numberOfOrders") or 0)}
        time.sleep(0.5)
    return list(hits.values())


def _reseller_map(s: dict) -> dict:
    m = {}
    for line in (s.get("reseller_map") or "").splitlines():
        if "=" in line:
            a, b = line.rsplit("=", 1)
            m[a.strip().lower()] = b.strip().lower()
    return m


def shopify_match(c: dict, hits: list[dict], rmap: dict) -> tuple[str, str]:
    g = c.get("group", "") or ""
    if g.lower().startswith("own") or "usama muchada" in g.lower():
        return "Own company", ""
    terms = shop_terms(c.get("name", ""))
    for h in hits:
        if "@" not in h["email"]:
            continue
        loc, dom = h["email"].split("@", 1)
        blob = re.sub(r"[^a-z0-9]", "", loc) + " " + re.sub(r"[^a-z0-9]", "", dom.split(".")[0]) + " " + re.sub(r"[^a-z0-9]", "", h["name"].lower())
        if any(t in blob for t in terms):
            return "Found – company", f"{h['name']} <{h['email']}> ({h['orders']} orders)"
    e = rmap.get(g.lower())
    if e:
        for h in hits:
            if h["email"].lower() == e:
                return "Found – via client/reseller", f"{h['name']} <{h['email']}> ({h['orders']} orders)"
    person = re.sub(r"\(.*?\)", "", g).strip().lower()
    if len(person.split()) >= 2 and not person.startswith(("direct", "reseller", "ad01", "usa ", "friend")):
        w = person.split()[:2]
        for h in hits:
            if all(x in h["name"].lower() for x in w):
                return "Found – via client/reseller", f"{h['name']} <{h['email']}> ({h['orders']} orders)"
    return "Not in Shopify", ""


def _shopify_job():
    st = _BG["shopify"]
    try:
        d = load()
        act = {cid: c for cid, c in d["companies"].items() if c.get("active", True) and c.get("jurisdiction") != "SA"}
        rmap = _reseller_map(d["settings"])
        terms = sorted({t for c in act.values() for t in shop_terms(c.get("name", ""))})
        queries = [" OR ".join(terms[i:i + 30]) for i in range(0, len(terms), 30)]
        people = sorted({re.sub(r"\(.*?\)", "", c.get("group", "")).strip().lower() for c in act.values()})
        people = [p for p in people if len(p.split()) >= 2 and not p.startswith(("direct", "reseller", "own", "ad01", "usa ", "friend"))]
        queries += [" OR ".join("(" + " ".join(p.split()[:2]) + ")" for p in people[i:i + 16]) for i in range(0, len(people), 16)]
        queries += [" OR ".join(sorted({e.split("@")[0] for e in rmap.values() if "@" in e}))] if rmap else []
        st["msg"] = f"Shopify: searching ({len(queries)} searches)…"
        hits = shopify_search(queries)
        with _LOCK:
            d2 = load()
            changed = 0
            for cid in act:
                c = d2["companies"].get(cid)
                if not c:
                    continue
                s, via = shopify_match(c, hits, rmap)
                if c.get("shopify_manual"):
                    continue    # staff set it by hand – keep
                if (c.get("shopify_status"), c.get("shopify_match")) != (s, via):
                    changed += 1
                c["shopify_status"], c["shopify_match"], c["shopify_checked"] = s, via, _today().isoformat()
            d2["shopify_last"] = _now()
            _log(d2, f"Shopify check: {len(act)} companies, {changed} changed, {len(hits)} customers matched a search")
            save(d2)
        st["msg"] = f"Shopify check finished: {len(act)} companies checked, {changed} changed."
    except Exception as e:
        st["msg"] = f"Shopify check stopped: {e}"
    finally:
        st["running"] = False


def _start(job: str):
    st = _BG[job]
    if st["running"]:
        return False
    st["running"], st["msg"] = True, "Starting…"
    threading.Thread(target=_ch_job if job == "ch" else _shopify_job, daemon=True).start()
    return True


def _auto_loop():
    time.sleep(90)
    while True:
        try:
            d = load()
            today = _today().isoformat()
            if os.environ.get("CH_API_KEY") and (d.get("ch_last") or "")[:10] != today:
                _start("ch")
            while _BG["ch"]["running"]:
                time.sleep(20)
            if os.environ.get("SHOPIFY_ADMIN_TOKEN") and (d.get("shopify_last") or "")[:10] != today:
                _start("shopify")
        except Exception as e:  # pragma: no cover
            print(f"[tracker] auto refresh: {e}", flush=True)
        time.sleep(3600)


# ----------------------------------------------------------------------------- import

def _cells(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    head = [str(h or "").strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not r or not any(v not in (None, "") for v in r):
            continue
        out.append({head[i]: r[i] for i in range(min(len(head), len(r))) if head[i]})
    return out


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _upsert(d: dict, name: str, number: str, jur: str, **fields) -> tuple[str, bool]:
    cid = find_company(d, name, number)
    new = cid is None
    if new:
        cid = uuid.uuid4().hex[:10]
        d["companies"][cid] = {"id": cid, "name": name, "jurisdiction": jur, "active": True, "tasks": [], "created": _now()}
    c = d["companies"][cid]
    if number:
        c["number"] = number
    for k, v in fields.items():
        if v not in (None, ""):
            c[k] = v
    return cid, new


def import_workbook(raw: bytes) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    names = {ws.title.lower(): ws for ws in wb.worksheets}
    rep = {"new": 0, "updated": 0, "statuses": 0, "sheets": []}
    with _LOCK:
        d = load()

        def count(new):
            rep["new" if new else "updated"] += 1

        if "uk companies" in names:
            rep["sheets"].append("UK Companies")
            for r in _cells(names["uk companies"]):
                name = _s(r.get("Company"))
                if not name:
                    continue
                st = _s(r.get("Companies House status"))
                vat = _s(r.get("VAT quarters (pick)"))
                cid, new = _upsert(d, name, _s(r.get("Co. number")).lstrip("'"), "UK", group=_s(r.get("Client / group")),
                                   services=_s(r.get("Work seen in WorkDrive")), vat=vat if vat in VAT_STAGGERS else "")
                c = d["companies"][cid]
                c.setdefault("ch", {})
                c["ch"].update({k: v for k, v in {"status": st, "accounts_due": str(pdate(r.get("Accounts due")) or ""),
                                                  "made_up_to": str(pdate(r.get("Next accounts made up to")) or ""),
                                                  "cs_due": str(pdate(r.get("Confirmation statement due")) or ""),
                                                  "incorporated": str(pdate(r.get("Incorporated")) or "")}.items() if v})
                if st and not st.lower().startswith("active"):
                    c["active"] = False
                count(new)
        if "us companies" in names:
            rep["sheets"].append("US Companies")
            for r in _cells(names["us companies"]):
                name = _s(r.get("Company"))
                if not name:
                    continue
                state = _s(r.get("State")).replace("—", "")
                cid, new = _upsert(d, name, "", "US", group=_s(r.get("Owner / client")), state=state,
                                   services=_s(r.get("Work seen in WorkDrive")), notes=_s(r.get("Notes")))
                count(new)
        if "other countries" in names:
            rep["sheets"].append("Other Countries")
            for r in _cells(names["other countries"]):
                name = _s(r.get("Company / matter"))
                if not name:
                    continue
                cid, new = _upsert(d, name, "", "OTHER", country=_s(r.get("Country")), group=_s(r.get("Folder / client")),
                                   notes=_s(r.get("Work / what to check")))
                count(new)
        if "self assessment" in names:
            rep["sheets"].append("Self Assessment")
            for r in _cells(names["self assessment"]):
                name = _s(r.get("Client"))
                if not name:
                    continue
                cid, new = _upsert(d, name, "", "SA", group="Self Assessment", notes=_s(r.get("Notes")))
                count(new)
        if "whatsapp map" in names:
            rep["sheets"].append("WhatsApp Map")
            for r in _cells(names["whatsapp map"]):
                cid = find_company(d, _s(r.get("Company / client (as on Deadlines)")))
                if cid and _s(r.get("WhatsApp group")):
                    d["companies"][cid]["wa_group"] = _s(r.get("WhatsApp group"))
        if "shopify check" in names:
            rep["sheets"].append("Shopify Check")
            for r in _cells(names["shopify check"]):
                cid = find_company(d, _s(r.get("Company")))
                if cid:
                    c = d["companies"][cid]
                    st = _s(r.get("Shopify status")).split(" (")[0]
                    c["shopify_status"] = st if st in SHOPIFY_STATES else _s(r.get("Shopify status"))
                    c["shopify_match"] = _s(r.get("Shopify customer matched"))
                    c["shopify_checked"] = str(pdate(r.get("Checked on")) or "")
        if "deadlines" in names:      # bring over statuses + notes typed on the old sheet
            rep["sheets"].append("Deadlines")
            label_kind = [("annual accounts", "ACC"), ("confirmation statement", "CS"), ("corporation tax payment", "CTPAY"),
                          ("ct600", "CT600"), ("vat", "VAT"), ("form 5472", "F5472"), ("sa return", "SA"),
                          ("payment on account", "POA")]
            for r in _cells(names["deadlines"]):
                cid = find_company(d, _s(r.get("Company / client")))
                st = _s(r.get("Status"))
                note = _s(r.get("Notes"))
                if not cid or st in ("", "To do"):      # notes on "To do" rows were the sheet's own auto notes
                    continue
                what = _s(r.get("What is due")).lower()
                kind = next((k for lbl, k in label_kind if lbl in what), "STATE" if d["companies"][cid].get("jurisdiction") == "US" else None)
                due = pdate(r.get("Due date"))
                if not kind:
                    continue
                for t in deadlines(d["companies"][cid]):
                    if t["kind"] == kind and (not due or t["due"] == due.isoformat()):
                        d["status"][tkey(cid, t)] = {"status": st if st in STATUSES else "To do", "note": note, "at": _now(),
                                                     "by": "import"}
                        rep["statuses"] += 1
                        break
        _log(d, f"Import: {rep['new']} new, {rep['updated']} updated, {rep['statuses']} statuses ({', '.join(rep['sheets'])})")
        save(d)
    return rep


def import_csv(raw: bytes) -> dict:
    text = raw.decode("utf-8-sig", errors="replace")
    rep = {"new": 0, "updated": 0, "statuses": 0, "sheets": ["CSV"]}
    with _LOCK:
        d = load()
        for r in csv.DictReader(io.StringIO(text)):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            name = r.get("company") or r.get("company_name") or r.get("name")
            if not name:
                continue
            j = (r.get("jurisdiction") or "UK").upper()
            j = "OTHER" if j.startswith("OTHER") else ("SA" if j.startswith("SA") or j.startswith("IND") else ("US" if j.startswith("US") else "UK"))
            _, new = _upsert(d, name, r.get("number") or r.get("company_number") or "", j, group=r.get("group") or r.get("client", ""),
                             state=r.get("state", ""), vat=r.get("vat", "") if r.get("vat", "") in VAT_STAGGERS else "",
                             services=r.get("services", ""), wa_group=r.get("whatsapp", ""), email=r.get("email", ""))
            rep["new" if new else "updated"] += 1
        _log(d, f"CSV import: {rep['new']} new, {rep['updated']} updated")
        save(d)
    return rep


# ----------------------------------------------------------------------------- routes

def _ctx(**kw):
    kw.update(VERSION=VERSION, STATUSES=STATUSES, CLOSED=CLOSED, JURIS=JURIS, VAT_STAGGERS=VAT_STAGGERS, KINDS=KINDS,
              SHOPIFY_STATES=SHOPIFY_STATES, US_STATES=sorted(US_RULES), bg=_BG, fmt=_fmt)
    return kw


def _fmt(v) -> str:
    d = pdate(v)
    return f"{d:%d %b %Y}" if d else (v or "–")


@bp.route("/tracker")
def home():
    d = load()
    rows = all_rows(d)
    a = request.args
    win = a.get("win", "30")
    q = (a.get("q") or "").lower().strip()
    jur, grp, kind, st = a.get("j", ""), a.get("g", ""), a.get("k", ""), a.get("s", "open")
    counts = {x: sum(1 for r in rows if r["alert"] == x) for x in ("overdue", "7", "30", "no date")}
    counts["waiting"] = sum(1 for r in rows if r["status"] == "Waiting on client")

    def keep(r):
        if jur and r["jurisdiction"] != jur:
            return False
        if grp and r["group"] != grp:
            return False
        if kind and r["kind"] != kind:
            return False
        if st == "open" and r["status"] in CLOSED:
            return False
        if st not in ("open", "all", "") and r["status"] != st:
            return False
        if q and q not in (r["company"] + " " + r["group"] + " " + r["label"] + " " + r["snote"]).lower():
            return False
        if win == "overdue":
            return r["alert"] == "overdue"
        if win in ("7", "30", "90"):
            return r["days"] is not None and r["days"] <= int(win)
        if win == "nodate":
            return r["alert"] == "no date"
        return True

    shown = [r for r in rows if keep(r)]
    groups = sorted({c.get("group", "") for c in d["companies"].values() if c.get("group")})
    return render_template_string(HOME, **_ctx(rows=shown[:800], total=len(shown), counts=counts, a=a, win=win, st=st,
                                               groups=groups, ncomp=len(d["companies"]), s=d["settings"], d=d))


@bp.post("/tracker/status")
def set_status():
    k = request.form.get("key", "")
    if k.count("|") != 3:
        abort(400)
    with _LOCK:
        d = load()
        cur = d["status"].get(k, {})
        new = {"status": request.form.get("status") or cur.get("status", "To do"),
               "note": request.form.get("note", cur.get("note", "")), "at": _now()}
        if new["status"] not in STATUSES:
            abort(400)
        d["status"][k] = new
        cid = k.split("|")[0]
        if cur.get("status") != new["status"]:
            _log(d, f"{k.split('|')[1]} {k.split('|')[3]}: {cur.get('status', 'To do')} → {new['status']}", cid)
        save(d)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, at=new["at"])
    return redirect(request.referrer or "/tracker")


@bp.route("/tracker/companies")
def companies():
    d = load()
    a = request.args
    q = (a.get("q") or "").lower().strip()
    jur, shop, act = a.get("j", ""), a.get("shop", ""), a.get("act", "1")
    rows = all_rows(d)
    nxt = {}
    for r in rows:
        if r["status"] not in CLOSED and r["due"]:
            nxt.setdefault(r["cid"], r)
    out = []
    for cid, c in d["companies"].items():
        if act == "1" and not c.get("active", True):
            continue
        if act == "0" and c.get("active", True):
            continue
        if jur and c.get("jurisdiction") != jur:
            continue
        if shop and (c.get("shopify_status") or "") != shop:
            continue
        if q and q not in " ".join(str(c.get(k, "")) for k in ("name", "number", "group", "wa_group", "state", "country", "notes")).lower():
            continue
        out.append({**c, "next": nxt.get(cid)})
    out.sort(key=lambda c: (c.get("jurisdiction", ""), c.get("name", "").lower()))
    sc = {}
    for c in d["companies"].values():
        if c.get("active", True) and c.get("jurisdiction") != "SA":
            sc[c.get("shopify_status") or "Not checked"] = sc.get(c.get("shopify_status") or "Not checked", 0) + 1
    return render_template_string(COMPANIES, **_ctx(rows=out, a=a, sc=sc, d=d))


@bp.route("/tracker/c/<cid>", methods=["GET", "POST"])
def company(cid):
    with _LOCK:
        d = load()
        c = d["companies"].get(cid) or abort(404)
        if request.method == "POST":
            f = request.form
            act = f.get("action", "save")
            if act == "save":
                for k in ("name", "number", "group", "state", "country", "services", "vat", "wa_group", "email", "notes", "formed"):
                    if k in f:
                        c[k] = f.get(k, "").strip()
                c["jurisdiction"] = f.get("jurisdiction") if f.get("jurisdiction") in JURIS else c.get("jurisdiction", "UK")
                c["active"] = bool(f.get("active"))
                if f.get("shopify_status") != c.get("shopify_status", ""):
                    c["shopify_status"], c["shopify_manual"], c["shopify_checked"] = f.get("shopify_status", ""), True, _today().isoformat()
                if c["jurisdiction"] == "UK" and not (c.get("ch") or {}).get("refreshed"):
                    for k in ("accounts_due", "made_up_to", "cs_due"):
                        if f.get(k):
                            c.setdefault("ch", {})[k] = f.get(k)
                _log(d, "Company details saved", cid)
                flash("Saved.")
            elif act == "task":
                if f.get("label", "").strip():
                    c.setdefault("tasks", []).append({"id": uuid.uuid4().hex[:8], "label": f["label"].strip(), "due": f.get("due", ""),
                                                      "note": f.get("note", "").strip()})
                    _log(d, f"Task added: {f['label'].strip()}", cid)
            elif act == "deltask":
                c["tasks"] = [t for t in c.get("tasks", []) if t.get("id") != f.get("tid")]
            elif act == "ch":
                try:
                    flash("Companies House: " + ch_refresh_one(c))
                except Exception as e:
                    flash(f"Companies House refresh failed: {e}")
            save(d)
            return redirect(f"/tracker/c/{cid}")
    rows = [r for r in all_rows(d) if r["cid"] == cid]
    s = d["settings"]
    for r in rows:
        r["reminder"] = reminder_text(r, s)
    hist = [h for h in d.get("log", []) if h.get("cid") == cid][-30:][::-1]
    return render_template_string(COMPANY, **_ctx(c=c, rows=rows, hist=hist, s=s, ch_key=bool(os.environ.get("CH_API_KEY"))))


@bp.post("/tracker/c/<cid>/remind")
def remind(cid):
    d = load()
    c = d["companies"].get(cid) or abort(404)
    k = request.form.get("key", "")
    r = next((x for x in all_rows(d) if x["key"] == k), None) or abort(404)
    to = (request.form.get("to") or c.get("email") or "").strip()
    if not to or "@" not in to or not _SEND["fn"]:
        flash("No e-mail address on this company (or e-mail is not set up in Settings).")
        return redirect(f"/tracker/c/{cid}")
    body = request.form.get("body") or reminder_text(r, d["settings"])
    try:
        _SEND["fn"](_CFG["fn"]() if _CFG["fn"] else {}, to, f"Reminder: {r['label']} – {c['name']}", body, [])
    except Exception as e:
        flash(f"E-mail not sent: {e}")
        return redirect(f"/tracker/c/{cid}")
    with _LOCK:
        d = load()
        st = d["status"].get(k, {"status": "To do", "note": ""})
        st["note"] = (st.get("note", "") + f" · reminder e-mailed {_today():%d %b} to {to}").strip(" ·")
        if st.get("status", "To do") == "To do":
            st["status"] = "Waiting on client"
        st["at"] = _now()
        d["status"][k] = st
        _log(d, f"Reminder e-mailed to {to}: {r['label']}", cid)
        save(d)
    flash(f"Reminder e-mailed to {to}.")
    return redirect(f"/tracker/c/{cid}")


@bp.route("/tracker/new", methods=["GET", "POST"])
def new_company():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Enter the company name.")
            return redirect("/tracker/new")
        with _LOCK:
            d = load()
            cid, new = _upsert(d, name, request.form.get("number", "").strip().upper(), request.form.get("jurisdiction", "UK"),
                               group=request.form.get("group", "").strip())
            _log(d, "Company added" if new else "Company already on the tracker", cid)
            if new and d["companies"][cid]["jurisdiction"] == "UK" and d["companies"][cid].get("number") and os.environ.get("CH_API_KEY"):
                try:
                    ch_refresh_one(d["companies"][cid])
                except Exception:
                    pass
            save(d)
        if not new:
            flash("That company is already on the tracker.")
        return redirect(f"/tracker/c/{cid}")
    return render_template_string(NEW, **_ctx())


@bp.route("/tracker/import", methods=["GET", "POST"])
def import_page():
    rep = None
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a file.")
            return redirect("/tracker/import")
        raw = f.read()
        try:
            rep = import_workbook(raw) if f.filename.lower().endswith((".xlsx", ".xlsm")) else import_csv(raw)
        except Exception as e:
            flash(f"Import failed: {e}")
            return redirect("/tracker/import")
    return render_template_string(IMPORT, **_ctx(rep=rep, n=len(load()["companies"])))


@bp.route("/tracker/settings", methods=["GET", "POST"])
def settings_page():
    with _LOCK:
        d = load()
        if request.method == "POST":
            s = d["settings"]
            s["cs_link"] = request.form.get("cs_link", "").strip() or SETTINGS_DEFAULT["cs_link"]
            s["sign_off"] = request.form.get("sign_off", "").strip() or "Team Startitup"
            s["reseller_map"] = request.form.get("reseller_map", "")
            save(d)
            flash("Tracker settings saved.")
            return redirect("/tracker/settings")
    return render_template_string(SETTINGS, **_ctx(s=d["settings"], ch_key=bool(os.environ.get("CH_API_KEY")),
                                                   shop_key=bool(os.environ.get("SHOPIFY_ADMIN_TOKEN")), d=d))


@bp.post("/tracker/refresh")
def refresh():
    flash("Companies House refresh started – it runs in the background." if _start("ch") else "A refresh is already running.")
    return redirect(request.referrer or "/tracker")


@bp.post("/tracker/shopify")
def shopify():
    flash("Shopify check started – it runs in the background." if _start("shopify") else "A Shopify check is already running.")
    return redirect(request.referrer or "/tracker/companies")


@bp.get("/tracker/job")
def job():
    return jsonify({k: dict(v) for k, v in _BG.items()})


@bp.get("/tracker/export.csv")
def export():
    d = load()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Company", "Jurisdiction", "Client / group", "What is due", "Due date", "Days left", "Status", "Note", "Deadline note",
                "WhatsApp group", "Shopify"])
    for r in all_rows(d):
        c = d["companies"][r["cid"]]
        w.writerow([r["company"], r["jurisdiction"], r["group"], r["label"], r["due"], "" if r["days"] is None else r["days"],
                    r["status"], r["snote"], r["note"], r["wa"], c.get("shopify_status", "")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=tracker_{_today():%Y%m%d}.csv"})


def register(app, cfg_fn=None, send_fn=None):
    _CFG["fn"], _SEND["fn"] = cfg_fn, send_fn
    OUT.mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)
    if not _AUTO["started"] and os.environ.get("TRACKER_NO_AUTO") != "1":
        _AUTO["started"] = True
        threading.Thread(target=_auto_loop, daemon=True).start()

    @app.context_processor
    def _tracker_badge():
        try:
            return {"tracker_alerts": alert_count()}
        except Exception:
            return {"tracker_alerts": 0}


# ----------------------------------------------------------------------------- templates

STYLE = """<style>
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 14px}.tabs a{padding:8px 14px;border-radius:8px;background:#f1f5f9;color:#111;text-decoration:none;font-weight:600;font-size:14px}
.tabs a.on{background:#111;color:#fff}
.tk select,.tk input[type=date]{padding:6px;border:1px solid #cbd5e1;border-radius:6px;font-family:inherit;font-size:13px}
.tk .filterbar select{max-width:220px}
.pill.late{background:#fee2e2;color:#991b1b;font-weight:700}.pill.soon{background:#ffedd5;color:#9a3412;font-weight:700}
.pill.mid{background:#fef9c3;color:#854d0e}.pill.ok{background:#dcfce7;color:#166534}.pill.grey{background:#f1f5f9;color:#475569}
.pill.uk{background:#dbeafe;color:#1e40af}.pill.us{background:#ede9fe;color:#5b21b6}.pill.other{background:#e0f2fe;color:#075985}.pill.sa{background:#fce7f3;color:#9d174d}
.stat a{color:inherit;text-decoration:none}.stat.red b{color:#b91c1c}.stat.orange b{color:#c2410c}.stat.yellow b{color:#a16207}
tr.closed td{color:#9ca3af}tr.closed td a{color:#9ca3af}
td.note input{width:100%;min-width:140px;padding:5px;border:1px solid transparent;border-radius:6px;font-size:13px;background:transparent}
td.note input:hover,td.note input:focus{border-color:#cbd5e1;background:#fff}
.saved{color:#16a34a;font-size:11px}
.rem{background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:10px;font-size:13px;white-space:pre-wrap}
.tk table a{color:#111;text-decoration:none}.tk table a:hover{text-decoration:underline}
.bg{background:#eef2ff;border:1px solid #c7d2fe;padding:8px 14px;border-radius:8px;margin:0 0 14px;font-size:13px}
</style>"""

NAV = r"""<div class="tabs"><a href="/tracker" class="{{'on' if page=='due'}}">Deadlines</a><a href="/tracker/companies" class="{{'on' if page=='companies'}}">Companies</a>
<a href="/tracker/new" class="{{'on' if page=='new'}}">+ Add company</a><a href="/tracker/import" class="{{'on' if page=='import'}}">Import</a>
<a href="/tracker/settings" class="{{'on' if page=='settings'}}">Settings</a><a href="/tracker/export.csv">Export CSV</a></div>
{% if bg.ch.running or bg.shopify.running or bg.ch.msg or bg.shopify.msg %}<div class="bg" id="bgmsg">{% if bg.ch.msg %}{{bg.ch.msg}}{% endif %}{% if bg.ch.msg and bg.shopify.msg %} · {% endif %}{% if bg.shopify.msg %}{{bg.shopify.msg}}{% endif %}</div>{% endif %}
{% macro jp(j) %}<span class="pill {{ {'UK':'uk','US':'us','OTHER':'other','SA':'sa'}.get(j,'') }}">{{ {'OTHER':'Other','SA':'Individual'}.get(j, j) }}</span>{% endmacro %}
{% macro duep(r) %}{% if not r.due %}<span class="pill grey">no date</span>{% else %}{{fmt(r.due)}}<br>{% if r.alert=='overdue' %}<span class="pill late">{{ 0 - r.days }} days late</span>{% elif r.alert=='7' %}<span class="pill soon">{{r.days}} days</span>{% elif r.alert=='30' %}<span class="pill mid">{{r.days}} days</span>{% elif r.alert=='closed' %}<span class="pill ok">closed</span>{% else %}<span class="muted">{{r.days}} days</span>{% endif %}{% endif %}{% endmacro %}
{% macro shp(s) %}{% if s=='Not in Shopify' %}<span class="pill late">Not in Shopify</span>{% elif s and s.startswith('Found – company') %}<span class="pill ok">Found</span>{% elif s and s.startswith('Found') %}<span class="pill mid">Via client</span>{% elif s=='Own company' %}<span class="pill grey">Own</span>{% else %}<span class="muted">–</span>{% endif %}{% endmacro %}"""

STATUS_JS = r"""<script>
function tkSave(el){
  var tr = el.closest('tr'), fd = new FormData();
  fd.append('key', tr.dataset.key);
  fd.append('status', tr.querySelector('select.st').value);
  fd.append('note', tr.querySelector('input.nt').value);
  fetch('/tracker/status', {method:'POST', body:fd, headers:{'X-Requested-With':'fetch'}}).then(function(r){return r.json()}).then(function(){
    var s = tr.querySelector('.saved'); if (s){ s.textContent = 'saved'; setTimeout(function(){ s.textContent=''; }, 1500); }
    var v = tr.querySelector('select.st').value; tr.classList.toggle('closed', v==='Done / filed' || v==='Not our job');
  }).catch(function(){ alert('Not saved – check your connection and try again.'); });
}
function tkCopy(id){ var t=document.getElementById(id).innerText; navigator.clipboard.writeText(t).then(function(){ alert('Reminder copied – paste it into the WhatsApp group.'); }); }
(function poll(){ var b=document.getElementById('bgmsg'); if(!b) return; fetch('/tracker/job').then(function(r){return r.json()}).then(function(j){
  var m=[j.ch.msg,j.shopify.msg].filter(Boolean).join(' · '); if(m) b.textContent=m; if(j.ch.running||j.shopify.running) setTimeout(poll,3000); }); })();
</script>"""

STATUS_CELLS = r"""<td><select class="st" onchange="tkSave(this)">{% for x in STATUSES %}<option {{'selected' if x==r.status}}>{{x}}</option>{% endfor %}</select></td>
<td class="note"><input class="nt" type="text" value="{{r.snote}}" placeholder="add a note…" onchange="tkSave(this)"><span class="saved"></span></td>"""

HOME = r"""{% extends "base" %}{% block body %}""" + STYLE + r"""<div class="card tk">""" + NAV.replace("page==", "'due'==") + r"""
<h1 style="margin-bottom:4px">Tracker <span class="muted" style="font-size:15px">– every client deadline: UK, US, other countries and Self Assessment</span></h1>
<p class="muted" style="margin-top:0">{{ncomp}} companies / clients on the tracker{% if d.ch_last %} · Companies House checked {{fmt(d.ch_last)}}{% endif %}{% if d.shopify_last %} · Shopify checked {{fmt(d.shopify_last)}}{% endif %}</p>
{% if not ncomp %}<div class="flash">The tracker is empty – <a href="/tracker/import">import the tracker workbook</a> or <a href="/tracker/new">add a company</a>.</div>{% endif %}
<div class="statgrid">
<div class="stat red"><a href="/tracker?win=overdue"><b>{{counts['overdue']}}</b>Overdue</a></div>
<div class="stat orange"><a href="/tracker?win=7"><b>{{counts['7']}}</b>Due in 7 days</a></div>
<div class="stat yellow"><a href="/tracker?win=30"><b>{{counts['30']}}</b>Due in 8–30 days</a></div>
<div class="stat"><a href="/tracker?win=all&s=Waiting on client"><b>{{counts['waiting']}}</b>Waiting on client</a></div>
<div class="stat"><a href="/tracker?win=nodate"><b>{{counts['no date']}}</b>No date yet</a></div>
</div>
<form class="filterbar" method="get">
<select name="win">{% for v,l in [('overdue','Overdue only'),('7','Overdue + next 7 days'),('30','Overdue + next 30 days'),('90','Overdue + next 90 days'),('all','All dates'),('nodate','No date yet')] %}<option value="{{v}}" {{'selected' if win==v}}>{{l}}</option>{% endfor %}</select>
<select name="j"><option value="">All jurisdictions</option>{% for k,v in JURIS.items() %}<option value="{{k}}" {{'selected' if a.j==k}}>{{v}}</option>{% endfor %}</select>
<select name="k"><option value="">All deadline types</option>{% for k,v in KINDS.items() %}<option value="{{k}}" {{'selected' if a.k==k}}>{{v}}</option>{% endfor %}</select>
<select name="s"><option value="open" {{'selected' if st=='open'}}>Open (not done)</option><option value="all" {{'selected' if st=='all'}}>Any status</option>{% for x in STATUSES %}<option {{'selected' if st==x}}>{{x}}</option>{% endfor %}</select>
<select name="g"><option value="">All clients / groups</option>{% for g in groups %}<option {{'selected' if a.g==g}}>{{g}}</option>{% endfor %}</select>
<input type="text" name="q" value="{{a.q or ''}}" placeholder="Search company, client, note…">
<button class="btn small">Show</button>{% if a %}<a class="btn small secondary" href="/tracker">Reset</a>{% endif %}
</form>
<div class="btnrow" style="margin-bottom:10px"><form method="post" action="/tracker/refresh"><button class="btn small secondary" title="Reload accounts and confirmation statement dates for every UK company">↻ Refresh from Companies House</button></form>
<form method="post" action="/tracker/shopify"><button class="btn small secondary" title="Check every active company against Shopify customers">↻ Run Shopify check</button></form></div>
<p class="muted">{{total}} deadline{{'' if total==1 else 's'}}{% if total > rows|length %} – showing the first {{rows|length}}{% endif %}. Change a status or type a note: it saves at once.</p>
<table><tr><th>Due</th><th>Company / client</th><th>What is due</th><th>Client / group</th><th>Status</th><th>Note</th></tr>
{% for r in rows %}<tr data-key="{{r.key}}" class="{{'closed' if r.status in CLOSED}}">
<td style="white-space:nowrap">{{duep(r)}}</td>
<td><a href="/tracker/c/{{r.cid}}"><b>{{r.company}}</b></a><br>{{jp(r.jurisdiction)}}{% if r.wa %} <span class="muted tiny" title="WhatsApp group">💬 {{r.wa}}</span>{% endif %}</td>
<td>{{r.label}}{% if r.note %}<br><span class="muted">{{r.note}}</span>{% endif %}</td><td>{{r.group}}</td>""" + STATUS_CELLS + r"""</tr>{% endfor %}</table>
{% if not rows %}<p class="muted">Nothing matches – try "All dates" or reset the filters.</p>{% endif %}
</div>""" + STATUS_JS + r"""{% endblock %}"""

COMPANIES = r"""{% extends "base" %}{% block body %}""" + STYLE + r"""<div class="card tk">""" + NAV.replace("page==", "'companies'==") + r"""
<h1>Companies</h1>
<div class="statgrid">{% for k,v in sc.items() %}<div class="stat"><a href="/tracker/companies?shop={{'' if k=='Not checked' else k}}"><b>{{v}}</b>{{k}}</a></div>{% endfor %}</div>
<p class="muted" style="margin-top:0">Shopify: whether the company – or the client / reseller who orders for it – is a customer on startitup.global.{% if d.shopify_last %} Last check {{fmt(d.shopify_last)}}.{% endif %}</p>
<form class="filterbar" method="get">
<select name="j"><option value="">All jurisdictions</option>{% for k,v in JURIS.items() %}<option value="{{k}}" {{'selected' if a.j==k}}>{{v}}</option>{% endfor %}</select>
<select name="shop"><option value="">Any Shopify status</option>{% for x in SHOPIFY_STATES if x %}<option {{'selected' if a.shop==x}}>{{x}}</option>{% endfor %}</select>
<select name="act"><option value="1" {{'selected' if a.get('act','1')=='1'}}>Active</option><option value="0" {{'selected' if a.act=='0'}}>Not active</option><option value="all" {{'selected' if a.act=='all'}}>All</option></select>
<input type="text" name="q" value="{{a.q or ''}}" placeholder="Search name, number, client, WhatsApp group…">
<button class="btn small">Show</button>{% if a %}<a class="btn small secondary" href="/tracker/companies">Reset</a>{% endif %}
</form>
<div class="btnrow" style="margin-bottom:10px"><form method="post" action="/tracker/shopify"><button class="btn small secondary">↻ Run Shopify check</button></form>
<form method="post" action="/tracker/refresh"><button class="btn small secondary">↻ Refresh from Companies House</button></form></div>
<p class="muted">{{rows|length}} companies</p>
<table><tr><th>Company</th><th>Client / group</th><th>Status</th><th>Next deadline</th><th>Shopify</th><th>WhatsApp group</th></tr>
{% for c in rows %}<tr><td><a href="/tracker/c/{{c.id}}"><b>{{c.name}}</b></a><br>{{jp(c.jurisdiction)}} <span class="muted">{{c.number or c.state or c.country or ''}}</span></td>
<td>{{c.group}}</td><td>{% if c.jurisdiction=='UK' %}{{(c.ch or {}).status or '–'}}{% else %}{{'Active' if c.active!=False else 'Not active'}}{% endif %}</td>
<td>{% if c.next %}{{c.next.label}}<br>{{duep(c.next)}}{% else %}<span class="muted">–</span>{% endif %}</td>
<td>{{shp(c.shopify_status)}}{% if c.shopify_match %}<br><span class="muted tiny">{{c.shopify_match}}</span>{% endif %}</td><td>{{c.wa_group or '–'}}</td></tr>{% endfor %}</table>
</div>""" + STATUS_JS + r"""{% endblock %}"""

COMPANY = r"""{% extends "base" %}{% block body %}""" + STYLE + r"""<div class="card tk">""" + NAV.replace("page==", "''==") + r"""
<p><a href="/tracker/companies">← Companies</a></p>
<h1 style="margin-bottom:2px">{{c.name}}</h1>
<p class="muted" style="margin-top:0">{{jp(c.jurisdiction)}} {% if c.number %}{{c.number}} · {% endif %}{% if c.jurisdiction=='UK' and c.ch %}{{c.ch.status or ''}}{% if c.ch.refreshed %} · Companies House checked {{fmt(c.ch.refreshed)}}{% endif %}{% endif %}{% if c.active==False %} · <b>not active</b>{% endif %}</p>
{% if c.jurisdiction=='UK' and c.number %}<form method="post" style="display:inline"><input type="hidden" name="action" value="ch"><button class="btn small secondary" {{'disabled' if not ch_key}} title="{{'' if ch_key else 'CH_API_KEY is not set'}}">↻ Refresh from Companies House</button></form>
<a class="btn small secondary" target="_blank" href="https://find-and-update.company-information.service.gov.uk/company/{{c.number}}">Open on Companies House</a>
<a class="btn small secondary" href="/company?name={{c.name|urlencode}}">Company file</a>{% endif %}
<h2 style="margin-top:22px">Deadlines</h2>
<table><tr><th>Due</th><th>What is due</th><th>Status</th><th>Note</th><th>Reminder</th></tr>
{% for r in rows %}<tr data-key="{{r.key}}" class="{{'closed' if r.status in CLOSED}}"><td style="white-space:nowrap">{{duep(r)}}</td>
<td>{{r.label}}{% if r.note %}<br><span class="muted">{{r.note}}</span>{% endif %}{% if r.kind=='TASK' %}<form method="post" style="display:inline"><input type="hidden" name="action" value="deltask"><input type="hidden" name="tid" value="{{r.tid}}"> <button class="btn small secondary" onclick="return confirm('Remove this task?')">✕</button></form>{% endif %}</td>""" + STATUS_CELLS + r"""
<td style="min-width:260px">{% if r.status not in CLOSED %}<div class="rem" id="rem{{loop.index}}">{{r.reminder}}</div>
<div class="btnrow" style="margin-top:6px"><button class="btn small secondary" type="button" onclick="tkCopy('rem{{loop.index}}')">Copy for WhatsApp</button>
<form method="post" action="/tracker/c/{{c.id}}/remind" onsubmit="return confirm('E-mail this reminder to ' + (this.to.value || 'the company') + '?')"><input type="hidden" name="key" value="{{r.key}}">
<input type="hidden" name="to" value="{{c.email or ''}}"><button class="btn small" {{'disabled' if not c.email}} title="{{'' if c.email else 'Add an e-mail address below first'}}">E-mail reminder</button></form></div>{% endif %}</td></tr>{% endfor %}</table>
{% if not rows %}<p class="muted">No deadlines yet{% if c.jurisdiction=='UK' %} – refresh from Companies House or add the dates below{% endif %}.</p>{% endif %}
<h2 style="margin-top:22px">Add a task</h2>
<form method="post" class="filterbar"><input type="hidden" name="action" value="task"><input type="text" name="label" placeholder="e.g. File AD01 change of address" style="max-width:340px">
<input type="date" name="due"><input type="text" name="note" placeholder="note (optional)" style="max-width:240px"><button class="btn small">Add task</button></form>
<h2 style="margin-top:22px">Details</h2>
<form method="post"><input type="hidden" name="action" value="save"><div class="dgrid">
<div><label>Name</label><input type="text" name="name" value="{{c.name}}"></div>
<div><label>Jurisdiction</label><select name="jurisdiction">{% for k,v in JURIS.items() %}<option value="{{k}}" {{'selected' if c.jurisdiction==k}}>{{v}}</option>{% endfor %}</select></div>
<div><label>Company number</label><input type="text" name="number" value="{{c.number or ''}}"></div>
<div><label>Client / group / reseller</label><input type="text" name="group" value="{{c.group or ''}}"></div>
<div><label>Client e-mail (for reminders)</label><input type="text" name="email" value="{{c.email or ''}}"></div>
<div><label>WhatsApp group</label><input type="text" name="wa_group" value="{{c.wa_group or ''}}"></div>
<div><label>Work we do (ACC, CT, VAT, CS, PAYE, SA…)</label><input type="text" name="services" value="{{c.services or ''}}"></div>
{% if c.jurisdiction=='UK' %}<div><label>VAT quarters</label><select name="vat">{% for v in VAT_STAGGERS %}<option value="{{v}}" {{'selected' if (c.vat or '')==v}}>{{v or 'Not VAT registered'}}</option>{% endfor %}</select></div>
{% if not (c.ch or {}).refreshed %}<div><label>Accounts due (until CH refresh)</label><input type="date" name="accounts_due" value="{{(c.ch or {}).accounts_due or ''}}"></div>
<div><label>Accounts made up to</label><input type="date" name="made_up_to" value="{{(c.ch or {}).made_up_to or ''}}"></div>
<div><label>Confirmation statement due</label><input type="date" name="cs_due" value="{{(c.ch or {}).cs_due or ''}}"></div>{% endif %}{% endif %}
{% if c.jurisdiction=='US' %}<div><label>State</label><select name="state"><option value="">–</option>{% for s in US_STATES %}<option {{'selected' if c.state==s}}>{{s}}</option>{% endfor %}</select></div>
<div><label>Formation date (for anniversary filings)</label><input type="date" name="formed" value="{{c.formed or ''}}"></div>{% endif %}
{% if c.jurisdiction=='OTHER' %}<div><label>Country</label><input type="text" name="country" value="{{c.country or ''}}"></div>{% endif %}
<div><label>Shopify</label><select name="shopify_status">{% for x in SHOPIFY_STATES %}<option value="{{x}}" {{'selected' if (c.shopify_status or '')==x}}>{{x or 'Not checked'}}</option>{% endfor %}</select>
{% if c.shopify_match %}<span class="muted tiny">{{c.shopify_match}}</span>{% endif %}{% if c.shopify_manual %}<span class="muted tiny"> · set by hand</span>{% endif %}</div>
<div><label><input type="checkbox" name="active" {{'checked' if c.active!=False}}> Active client</label></div>
</div><label>Notes</label><textarea name="notes" rows="3">{{c.notes or ''}}</textarea>
<p><button class="btn">Save details</button></p></form>
{% if hist %}<h2 style="margin-top:22px">History</h2><table>{% for h in hist %}<tr><td style="white-space:nowrap">{{fmt(h.at)}} {{h.at[11:16]}}</td><td>{{h.msg}}</td></tr>{% endfor %}</table>{% endif %}
</div>""" + STATUS_JS + r"""{% endblock %}"""

NEW = r"""{% extends "base" %}{% block body %}""" + STYLE + r"""<div class="card tk">""" + NAV.replace("page==", "'new'==") + r"""
<h1>Add a company</h1><form method="post" style="max-width:520px">
<label>Company / client name</label><input type="text" name="name" required>
<label>Jurisdiction</label><select name="jurisdiction">{% for k,v in JURIS.items() %}<option value="{{k}}">{{v}}</option>{% endfor %}</select>
<label>Company number (UK – dates load from Companies House)</label><input type="text" name="number">
<label>Client / group / reseller</label><input type="text" name="group">
<p><button class="btn">Add</button></p></form></div>{% endblock %}"""

IMPORT = r"""{% extends "base" %}{% block body %}""" + STYLE + r"""<div class="card tk">""" + NAV.replace("page==", "'import'==") + r"""
<h1>Import</h1>
{% if rep %}<div class="flash">Imported {{rep.sheets|join(', ')}}: <b>{{rep.new}}</b> new, <b>{{rep.updated}}</b> updated, <b>{{rep.statuses}}</b> deadline statuses brought over. <a href="/tracker">Open the deadlines</a></div>{% endif %}
<p>{{n}} companies / clients on the tracker now. Importing again <b>updates</b> companies with the same number or name – nothing is deleted.</p>
<form method="post" enctype="multipart/form-data"><div class="drop"><input type="file" name="file" accept=".xlsx,.csv" required>
<p class="muted">The tracker workbook (.xlsx – download the "Startitup – Client Work Tracker" Sheet as .xlsx) or a CSV</p></div>
<p><button class="btn">Import</button></p></form>
<h2>What is read</h2><table>
<tr><td><b>UK Companies</b></td><td>Company, Co. number, Client / group, Companies House status, Work seen in WorkDrive, accounts / CS dates, VAT quarters</td></tr>
<tr><td><b>US Companies</b></td><td>Company, Owner / client, State, Work seen in WorkDrive, Notes</td></tr>
<tr><td><b>Other Countries</b></td><td>Company / matter, Country, Folder / client, Work / what to check</td></tr>
<tr><td><b>Self Assessment</b></td><td>Client, Notes</td></tr>
<tr><td><b>WhatsApp Map</b></td><td>Company → WhatsApp group</td></tr>
<tr><td><b>Shopify Check</b></td><td>Company → Shopify status and matched customer</td></tr>
<tr><td><b>Deadlines</b></td><td>Status and notes you typed (anything other than "To do")</td></tr>
<tr><td><b>CSV</b></td><td>columns: company, jurisdiction (UK / US / Other / SA), number, group, state, vat, services, whatsapp, email</td></tr></table>
</div>{% endblock %}"""

SETTINGS = r"""{% extends "base" %}{% block body %}""" + STYLE + r"""<div class="card tk">""" + NAV.replace("page==", "'settings'==") + r"""
<h1>Tracker settings</h1>
<table style="max-width:700px"><tr><td>Companies House key (CH_API_KEY)</td><td>{{'✅ set' if ch_key else '❌ not set – UK dates are not refreshed'}}</td></tr>
<tr><td>Shopify (SHOPIFY_STORE + SHOPIFY_ADMIN_TOKEN)</td><td>{{'✅ set – checked once a day' if shop_key else '❌ not set – the Shopify column keeps the last imported values'}}</td></tr>
<tr><td>Last Companies House refresh</td><td>{{fmt(d.ch_last) if d.ch_last else 'never'}}</td></tr><tr><td>Last Shopify check</td><td>{{fmt(d.shopify_last) if d.shopify_last else 'never'}}</td></tr></table>
<form method="post" style="max-width:700px">
<label>Confirmation statement order link (goes in every CS reminder)</label><input type="text" name="cs_link" value="{{s.cs_link}}">
<label>Reminders are signed</label><input type="text" name="sign_off" value="{{s.sign_off}}">
<label>Shopify – client / reseller accounts (one per line: Client / group = customer e-mail)</label>
<textarea name="reseller_map" rows="9">{{s.reseller_map}}</textarea>
<p class="muted">A company whose client / group is listed here counts as "Found – via client/reseller" when that customer is in Shopify.</p>
<p><button class="btn">Save</button></p></form></div>{% endblock %}"""
