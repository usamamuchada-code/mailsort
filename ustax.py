"""
US Tax tab (v43a) – year-end US returns for clients and their US LLCs.

  /ustax                      -> all filings: 1040-NR returns · US LLC returns · Other forms · Questionnaires
  /ustax/new  (POST)          -> add a client manually, or create a client questionnaire link
  /ustax/case/<id>            -> client + LLC details, figures, the filings the case needs (decided by rules), tracker,
                                 data sheet / CSV export for MyTaxPrepOffice, 1040-NR PDF (when the year's forms are installed)
  /sign/ustax/<token>         -> client questionnaire (simple steps, figures, signature) – no login, token-protected

The rules engine (decide()) works out which returns a case needs:
  foreign-owned single-member LLC (disregarded)  -> Form 5472 + pro-forma 1120 (+ 1040-NR if US trade or business)
  multi-member LLC (partnership)                  -> Form 1065 + K-1 per member (+ 8804/8805, K-2/K-3, members' 1040-NR)
  LLC taxed as a corporation                      -> Form 1120 (+ 5472 if 25 %+ foreign owner; 1042-S on dividends)
  US contractors paid >= threshold                -> 1099-NEC (+ 1096 if paper)
  foreign bank accounts > $10,000                 -> FinCEN 114 (FBAR) for the LLC
  US-person single owner                          -> owner's own Form 1040 Schedule C (not prepared here)
Self-contained: app.py imports it and calls register(app); only the menu link was added in app.py.
"""
from __future__ import annotations

import base64, csv, datetime as dt, io, json, os, re, secrets, threading, uuid, zipfile
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, render_template_string, request, send_file, session

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
OUT = DATA / "ustax"
TOKENS = OUT / "tokens.json"
_LOCK = threading.Lock()
LINK_DAYS = 45
bp = Blueprint("ustax", __name__)

STAGES = [("info_requested", "Questionnaire sent"), ("info_received", "Info received"), ("prepared", "Prepared"),
          ("client_signed", "Client signed (8879 / return)"), ("in_mtpo", "Entered in MyTaxPrepOffice"),
          ("filed", "E-filed / faxed / mailed"), ("accepted", "Accepted"), ("rejected", "Rejected – action needed"),
          ("not_needed", "Not needed")]
STAGE_LABEL = dict(STAGES)
DONE = {"filed", "accepted", "not_needed"}
GROUPS = {"nr": "1040-NR returns", "llc": "US LLC returns", "other": "Other forms"}

EXP_KEYS = [("formation", "State formation fee (start-up cost)"), ("agent", "Registered agent fee"),
            ("legal", "Accountant / legal fees"), ("advertising", "Advertising"), ("commissions", "Payment / platform fees"),
            ("contract", "Contractors / freelancers"), ("office", "Software, subscriptions, office"),
            ("travel", "Travel"), ("taxes", "State annual report / franchise tax, licences"), ("other", "Other costs")]


# ----------------------------------------------------------------------------- storage

def _case_dir(cid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", cid or ""):
        abort(404)
    d = OUT / cid
    if not (d / "case.json").exists():
        abort(404)
    return d


def load_case(cid: str) -> dict:
    return json.loads((_case_dir(cid) / "case.json").read_text())


def save_case(c: dict):
    d = OUT / c["id"]
    d.mkdir(parents=True, exist_ok=True)
    c["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    (d / "case.json").write_text(json.dumps(c, indent=1))


def all_cases() -> list[dict]:
    out = []
    if OUT.exists():
        for d in OUT.iterdir():
            f = d / "case.json"
            if f.exists():
                try:
                    out.append(json.loads(f.read_text()))
                except Exception:
                    pass
    return sorted(out, key=lambda c: c.get("created", ""), reverse=True)


def _tokens() -> dict:
    try:
        return json.loads(TOKENS.read_text())
    except Exception:
        return {}


def _staff() -> str:
    return session.get("user") or os.environ.get("MAILSORT_USER", "admin")


def _n(v) -> float:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v or "")) or 0)
    except ValueError:
        return 0.0


def _money(v) -> str:
    x = _n(v)
    return f"${x:,.0f}" if x >= 0 else f"(${-x:,.0f})"


def _itin():
    import itinforms
    return itinforms


def default_year() -> int:
    """The tax year being prepared: the year that is ending (from October), otherwise last year."""
    t = dt.date.today()
    return t.year if t.month >= 10 else t.year - 1


def blank_case(year: int | None = None) -> dict:
    return {"id": uuid.uuid4().hex[:12], "created": dt.datetime.now().isoformat(timespec="seconds"),
            "year": str(year or default_year()), "source": "manual",
            "owner": {"first": "", "middle": "", "last": "", "email": "", "phone": "", "citizenship": "", "us_person": "no",
                      "street": "", "city": "", "province": "", "postal": "", "country": "", "foreign_tin": "", "itin": "",
                      "occupation": "Business owner", "days_y0": "0", "days_y1": "0", "days_y2": "0", "trips": "",
                      "prior_return": "no", "prior_text": ""},
            "llc": {"name": "", "ein": "", "state": "", "formed": "", "activity": "", "naics": "", "street": "", "city_state_zip": "",
                    "classification": "default", "us_presence": "no", "foreign_bank": "no", "foreign_bank_max": ""},
            "members": [],          # extra owners: {name, country, pct, us_person}
            "fig": {"sales": "", "returns": "", "cogs": "", **{f"exp_{k}": "" for k, _l in EXP_KEYS}, "exp_other_desc": "",
                    "contributions": "", "distributions": "", "loans_from_owner": "", "loans_to_owner": "",
                    "bank_end": "", "total_assets": "", "dividends_paid": ""},
            "contractors": [],      # {name, us_person yes/no, corp yes/no, amount, tin}
            "filings": {}, "history": [], "q": {}}


# ----------------------------------------------------------------------------- rules engine

def _due(y: int, m: int, d: int) -> dt.date:
    x = dt.date(y, m, d)
    while x.weekday() >= 5:            # weekend -> next Monday (federal holidays not checked)
        x += dt.timedelta(days=1)
    return x


def nec_threshold(year: int) -> int:
    return 600 if year <= 2025 else 2000          # OBBBA: $2,000 for payments made after 31 Dec 2025


def decide(c: dict) -> list[dict]:
    """Which filings this case needs: [{key, group, form, title, due, why, how, prepare}] (prepare: pdf / sheet / none)."""
    y = int(c.get("year") or default_year())
    o, l, f = c["owner"], c["llc"], c["fig"]
    members = [m for m in c.get("members", []) if (m.get("name") or "").strip()]
    n_owners = 1 + len(members)
    owner_foreign = o.get("us_person") != "yes"
    any_foreign = owner_foreign or any(m.get("us_person") != "yes" for m in members)
    corp = l.get("classification") == "corporation"
    has_llc = bool((l.get("name") or "").strip())
    eci = l.get("us_presence") in ("yes", "unsure")
    sales = _n(f.get("sales"))
    out = []

    def add(key, group, form, title, due, why, how="Enter in MyTaxPrepOffice from the data sheet", prepare="sheet"):
        out.append({"key": key, "group": group, "form": form, "title": title, "due": due.isoformat() if due else "",
                    "why": why, "how": how, "prepare": prepare})

    if has_llc and corp:
        add("1120", "llc", "Form 1120", "U.S. Corporation Income Tax Return", _due(y + 1, 4, 15),
            "The LLC elected to be taxed as a corporation (Form 8832 / 2553). Extension: Form 7004 (to 15 October).")
        if any_foreign:
            add("5472c", "llc", "Form 5472", "Information return of a 25% foreign-owned U.S. corporation", _due(y + 1, 4, 15),
                "A foreign owner holds 25% or more – attach Form 5472 to the 1120 for each related party with reportable "
                "transactions. Penalty $25,000 per form if missing.")
        if any_foreign and _n(f.get("dividends_paid")) > 0:
            add("1042", "other", "Form 1042 + 1042-S", "Withholding on dividends paid to foreign owners", _due(y + 1, 3, 15),
                "Dividends were paid to a foreign owner – 30% withholding (or treaty rate) must be reported.")
    elif has_llc and n_owners >= 2:
        add("1065", "llc", "Form 1065 + Schedule K-1 per member", "U.S. Return of Partnership Income", _due(y + 1, 3, 15),
            f"The LLC has {n_owners} owners, so it is taxed as a partnership. Extension: Form 7004 (to 15 September).")
        if any_foreign:
            add("k23", "llc", "Schedules K-2 / K-3", "International information for partners", _due(y + 1, 3, 15),
                "Foreign members – K-2/K-3 are usually required with the 1065 (check the domestic filing exception).")
            if eci and sales > 0:
                add("8804", "other", "Forms 8804 / 8805 (+ 8813 payments)", "Withholding on foreign partners' share of US business income",
                    _due(y + 1, 3, 15), "Foreign members' share of effectively connected income – the LLC must pay and report "
                    "section 1446 withholding.")
        if owner_foreign and eci:
            add("1040nr", "nr", "Form 1040-NR (+ Schedule E, Schedule OI)", "Nonresident return of the foreign member(s)",
                _due(y + 1, 6, 15), "Each foreign member engaged in a US trade or business through the LLC files a 1040-NR "
                "(15 June; 15 April if US wages).", prepare="sheet")
    elif has_llc and owner_foreign:
        add("5472", "llc", "Form 5472 + pro-forma Form 1120", "Foreign-owned U.S. disregarded entity (single-member LLC)",
            _due(y + 1, 4, 15), "A single-member LLC owned by a non-US person must file a pro-forma 1120 with Form 5472 every "
            "year it has reportable transactions with its owner (formation costs paid, money put in or taken out, loans). "
            "Penalty $25,000. Extension: Form 7004.",
            how="Enter in MyTaxPrepOffice from the data sheet; if it cannot be e-filed there, fax to the IRS (855-887-7737)")
        if eci:
            add("1040nr", "nr", "Form 1040-NR (+ Schedule C, Schedule OI)", "Nonresident return of the owner", _due(y + 1, 6, 15),
                "The owner is engaged in a US trade or business through the LLC (US office, staff, agent or work done in the US"
                + (" – answer was 'not sure', staff to confirm" if l.get("us_presence") == "unsure" else "") + "). "
                "Due 15 June (15 April if US wages). Extension: Form 4868.", prepare="pdf")
    elif has_llc and not owner_foreign:
        add("owner1040", "other", "Owner's Form 1040 Schedule C", "US-person single owner", _due(y + 1, 4, 15),
            "The LLC is disregarded and owned by a US person – the income goes on the owner's own Form 1040 (not prepared here).",
            how="Owner's own return", prepare="none")
    elif not has_llc and owner_foreign:
        add("1040nr", "nr", "Form 1040-NR (+ Schedule OI)", "Nonresident return", _due(y + 1, 6, 15),
            "No US company – a 1040-NR is only needed if the client had US income or needs it for a W-7 (ITIN).", prepare="pdf")

    thr = nec_threshold(y)
    us_cons = [k for k in c.get("contractors", []) if k.get("us_person") == "yes" and k.get("corp") != "yes" and _n(k.get("amount")) >= thr]
    if us_cons:
        add("1099nec", "other", f"Form 1099-NEC ×{len(us_cons)}", "Payments to US contractors", _due(y + 1, 1, 31),
            f"Paid US non-corporate contractors ${thr:,} or more in {y}. File with the IRS (IRIS / MTPO) and give each a copy by "
            "31 January. Keep their Form W-9.")
    if any(k.get("us_person") != "yes" and _n(k.get("amount")) > 0 for k in c.get("contractors", [])):
        add("w8", "other", "Forms W-8BEN on file", "Foreign contractors", None,
            "No 1099 for foreign contractors working outside the US – keep their Form W-8BEN. If they worked in the US, "
            "Form 1042-S may be due.", how="Keep on file", prepare="none")
    if has_llc and (l.get("foreign_bank") == "yes") and _n(l.get("foreign_bank_max")) > 10000:
        add("fbar", "other", "FinCEN 114 (FBAR)", "Foreign bank accounts of the LLC", _due(y + 1, 4, 15),
            "The LLC is a US person for FBAR – foreign accounts over $10,000 in total at any time. File on BSA E-Filing "
            "(automatic extension to 15 October).", how="BSA E-Filing (not MyTaxPrepOffice)")
    if has_llc and l.get("state"):
        add("state", "other", f"{l['state']} annual report / franchise tax", "State filing", None,
            f"Check the {l['state']} annual report or franchise-tax due date and fee (not an IRS form).",
            how="State website", prepare="none")
    return out


def sync_filings(c: dict):
    """Keep a tracker entry for every filing decide() returns (stage, dates, MTPO reference)."""
    need = {x["key"]: x for x in decide(c)}
    fl = c.setdefault("filings", {})
    for k, x in need.items():
        e = fl.setdefault(k, {"stage": "info_received" if c.get("q", {}).get("submitted") else
                              ("info_requested" if c.get("q", {}).get("token") else "info_received"),
                              "filed_date": "", "mtpo_ref": "", "notes": ""})
        if x["prepare"] == "none" and e["stage"] in ("info_requested", "info_received"):
            e["stage"] = "not_needed" if k in ("w8", "owner1040") else e["stage"]
        e.update({kk: x[kk] for kk in ("group", "form", "title", "due", "why", "how", "prepare")})
        e["active"] = True
    for k, e in fl.items():
        if k not in need:
            e["active"] = False


def alert_count() -> int:
    n, today = 0, dt.date.today()
    for c in all_cases():
        for e in (c.get("filings") or {}).values():
            if e.get("active") and e.get("stage") not in DONE and e.get("due"):
                try:
                    if dt.date.fromisoformat(e["due"]) - today <= dt.timedelta(days=21):
                        n += 1
                except ValueError:
                    pass
    return n


# ----------------------------------------------------------------------------- data sheet (for MyTaxPrepOffice)

def sheet_sections(c: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    o, l, f = c["owner"], c["llc"], c["fig"]
    y = c["year"]
    owner = " ".join(x for x in (o["first"], o["middle"], o["last"]) if x)
    addr = ", ".join(x for x in (o["street"], o["city"], o["province"], o["postal"], o["country"]) if x)
    exp_total = sum(_n(f.get(f"exp_{k}")) for k, _l in EXP_KEYS)
    gross = _n(f.get("sales")) - _n(f.get("returns"))
    net = gross - _n(f.get("cogs")) - exp_total
    sec = [("Client", [("Tax year", y), ("Owner", owner), ("Citizenship", o["citizenship"]),
                       ("US person (citizen / green card)", "Yes" if o["us_person"] == "yes" else "No"),
                       ("ITIN / SSN", o.get("itin") or "None – ITIN applied for / not needed"),
                       ("Foreign tax ID", o.get("foreign_tin", "")), ("Address", addr), ("Phone", o["phone"]),
                       ("Email", o["email"]), ("Occupation", o.get("occupation", ""))])]
    if l.get("name"):
        cls = {"default": "Default (disregarded if 1 owner / partnership if 2+)", "corporation": "Taxed as a corporation"}
        sec.append(("US LLC", [("Name", l["name"]), ("EIN", l["ein"]), ("State", l["state"]), ("Date formed", l["formed"]),
                               ("Business activity", l["activity"]), ("Business code (NAICS)", l.get("naics", "")),
                               ("US address", ", ".join(x for x in (l["street"], l["city_state_zip"]) if x)),
                               ("Tax classification", cls.get(l.get("classification"), l.get("classification", ""))),
                               ("US office / staff / agent / work in the US (ECI)", {"yes": "Yes", "no": "No", "unsure": "Not sure"}.get(l.get("us_presence"), "")),
                               ("Foreign bank account", "Yes – max " + _money(l.get("foreign_bank_max")) if l.get("foreign_bank") == "yes" else "No")]))
        mem = [(f"Member {i + 2}", f"{m.get('name')} – {m.get('country', '')} – {m.get('pct', '')}% – "
                                   f"{'US person' if m.get('us_person') == 'yes' else 'foreign'}") for i, m in enumerate(c.get("members", [])) if m.get("name")]
        if mem:
            sec.append(("Other members", mem))
        sec.append((f"Income statement {y}", [("Gross receipts / sales", _money(f.get("sales"))), ("Returns and allowances", _money(f.get("returns"))),
                                              ("Cost of goods sold", _money(f.get("cogs")))] +
                    [(lbl, _money(f.get(f"exp_{k}"))) for k, lbl in EXP_KEYS if _n(f.get(f"exp_{k}"))] +
                    ([("Other costs – description", f.get("exp_other_desc", ""))] if _n(f.get("exp_other")) else []) +
                    [("Total expenses", _money(exp_total)), ("Net profit / (loss)", _money(net))]))
        sec.append(("Owner transactions & balance sheet (Form 5472 Part V / Schedule L)", [
            ("Money the owner put in (contributions, incl. costs paid personally)", _money(f.get("contributions"))),
            ("Money the owner took out (distributions)", _money(f.get("distributions"))),
            ("Loans from owner to LLC", _money(f.get("loans_from_owner"))), ("Loans from LLC to owner", _money(f.get("loans_to_owner"))),
            ("Bank balance at 31 December", _money(f.get("bank_end"))),
            ("Total assets at 31 December", _money(f.get("total_assets") or f.get("bank_end"))),
            ("Dividends paid (corporation only)", _money(f.get("dividends_paid")))]))
    cons = c.get("contractors") or []
    if cons:
        sec.append(("Contractors paid", [(k.get("name", ""), f"{_money(k.get('amount'))} – {'US' if k.get('us_person') == 'yes' else 'foreign'}"
                                          f"{' corporation' if k.get('corp') == 'yes' else ''}{' – TIN ' + k['tin'] if k.get('tin') else ''}") for k in cons]))
    sec.append(("Filings needed", [(x["form"], f"due {x['due'] or 'n/a'} – {x['why']}") for x in decide(c)]))
    return sec


def _ascii(t) -> str:
    return (str(t if t not in (None, "") else "-").replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
            .replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\u2192", "->").replace("\u00d7", "x"))


def _wrap(text: str, width: float, size: float) -> list[str]:
    import pymupdf
    out = []
    for para in text.split("\n"):
        line = ""
        for w in para.split(" "):
            t = (line + " " + w).strip()
            if pymupdf.get_text_length(t, fontname="helv", fontsize=size) <= width or not line:
                line = t
            else:
                out.append(line)
                line = w
        out.append(line)
    return out


def sheet_pdf(c: dict) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    W, H, M, FS, LH = 612, 792, 40, 8.5, 11
    KW, VX = 180, M + 190
    page, y = None, 0
    title_txt = _ascii(f"US tax data sheet - {c['llc'].get('name') or (c['owner']['first'] + ' ' + c['owner']['last'])} - tax year {c['year']}")

    def newpage():
        nonlocal page, y
        page = doc.new_page(width=W, height=H)
        page.insert_text((M, 34), title_txt, fontsize=11, fontname="hebo")
        page.insert_text((M, 48), "Prepared by Startitup Global for entry in MyTaxPrepOffice. Check every figure against the client's records.",
                         fontsize=7.5, fontname="helv", color=(0.35, 0.35, 0.35))
        page.insert_text((W - M - 40, H - 20), f"page {len(doc)}", fontsize=7, fontname="helv", color=(0.5, 0.5, 0.5))
        y = 64

    newpage()
    for title, rows in sheet_sections(c):
        if y > H - 90:
            newpage()
        page.draw_rect(pymupdf.Rect(M, y, W - M, y + 16), color=None, fill=(0.92, 0.94, 0.97))
        page.insert_text((M + 4, y + 11.5), _ascii(title), fontsize=9.5, fontname="hebo")
        y += 20
        for k, v in rows:
            kl, vl = _wrap(_ascii(k), KW, FS), _wrap(_ascii(v), W - M - VX, FS)
            h = max(len(kl), len(vl)) * LH
            if y + h > H - 36:
                newpage()
            for i, t in enumerate(kl):
                page.insert_text((M + 4, y + 8.5 + i * LH), t, fontsize=FS, fontname="helv", color=(0.25, 0.25, 0.25))
            for i, t in enumerate(vl):
                page.insert_text((VX, y + 8.5 + i * LH), t, fontsize=FS, fontname="helv")
            y += h + 3
        y += 6
    buf = io.BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    return buf.getvalue()


def sheet_csv(c: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["case", "section", "field", "value"])
    for title, rows in sheet_sections(c):
        for k, v in rows:
            w.writerow([c["id"], title, k, v])
    return buf.getvalue()


# ----------------------------------------------------------------------------- 1040-NR (uses taxreturn.py)

def nr_inputs(c: dict) -> tuple[dict, dict]:
    o, l, f = c["owner"], c["llc"], c["fig"]
    a = {"first": o["first"], "middle": o["middle"], "last": o["last"], "citizenship": o["citizenship"],
         "foreign_1": o["street"], "foreign_2": ", ".join(x for x in (o["city"], o["province"], o["country"]) if x)}
    has_llc = bool(l.get("name"))
    mode = "nil" if not has_llc else ("income" if _n(f.get("sales")) > 0 else "expenses")
    legal = _n(f.get("exp_agent")) + _n(f.get("exp_legal"))
    t = {"year": c["year"], "status": "single", "digital": "no", "occupation": o.get("occupation") or "Business owner",
         "phone": o["phone"], "email": o["email"], "prepared_by": "client", "mode": mode, "nil": mode == "nil",
         "addr_street": o["street"], "addr_city": o["city"], "addr_province": o["province"], "addr_postal": o["postal"],
         "addr_country": o["country"], "llc_name": l["name"], "llc_ein": l["ein"], "llc_state": l["state"], "llc_formed": l["formed"],
         "llc_activity": l["activity"], "llc_naics": l.get("naics", ""), "llc_street": l["street"], "llc_city_state_zip": l["city_state_zip"],
         "gross": f.get("sales", ""), "returns": f.get("returns", ""), "cogs": f.get("cogs", ""),
         "exp_formation": f.get("exp_formation", ""), "exp_legal": f"{legal:g}" if legal else "",
         "exp_advertising": f.get("exp_advertising", ""), "exp_commissions": f.get("exp_commissions", ""),
         "exp_contract": f.get("exp_contract", ""), "exp_office": f.get("exp_office", ""), "exp_travel": f.get("exp_travel", ""),
         "exp_taxes": f.get("exp_taxes", ""), "exp_other": f.get("exp_other", ""), "exp_other_desc": f.get("exp_other_desc", ""),
         "oi_citizen": o["citizenship"], "oi_residence": o["country"], "oi_visa": "NOT APPLICABLE",
         "oi_days_2023": o.get("days_y2") or "0", "oi_days_2024": o.get("days_y1") or "0", "oi_days_2025": o.get("days_y0") or "0",
         "oi_trips": o.get("trips", ""), "oi_prior": o.get("prior_return", "no"), "oi_prior_text": o.get("prior_text", "")}
    return a, t


def build_1040nr(c: dict) -> tuple[bytes, dict, list[str]]:
    import taxreturn
    it = _itin()
    a, t = nr_inputs(c)
    if int(c["year"]) not in taxreturn.years_available():
        raise RuntimeError(f"The {c['year']} Form 1040-NR is not installed yet – use the data sheet, or add the {c['year']} forms.")
    return taxreturn.build(a, it.load_caa(), t, it.fill_irs)


# ----------------------------------------------------------------------------- helpers for forms

OWNER_KEYS = ("first", "middle", "last", "email", "phone", "citizenship", "us_person", "street", "city", "province", "postal",
              "country", "foreign_tin", "itin", "occupation", "days_y0", "days_y1", "days_y2", "trips", "prior_return", "prior_text")
LLC_KEYS = ("name", "ein", "state", "formed", "activity", "naics", "street", "city_state_zip", "classification", "us_presence",
            "foreign_bank", "foreign_bank_max")
FIG_KEYS = ("sales", "returns", "cogs", *[f"exp_{k}" for k, _l in EXP_KEYS], "exp_other_desc", "contributions", "distributions",
            "loans_from_owner", "loans_to_owner", "bank_end", "total_assets", "dividends_paid")


def read_form(c: dict, f, prefix: str = ""):
    """Fill case dicts from a posted form (staff page and questionnaire use the same field names)."""
    g = lambda k: (f.get(prefix + k) or "").strip()[:300]
    for k in OWNER_KEYS:
        if (prefix + "o_" + k) in f:
            c["owner"][k] = g("o_" + k)
    for k in LLC_KEYS:
        if (prefix + "l_" + k) in f:
            c["llc"][k] = g("l_" + k)
    for k in FIG_KEYS:
        if (prefix + "f_" + k) in f:
            c["fig"][k] = g("f_" + k)
    mem = []
    for i in range(4):
        if g(f"m{i}_name"):
            mem.append({"name": g(f"m{i}_name"), "country": g(f"m{i}_country"), "pct": g(f"m{i}_pct"), "us_person": g(f"m{i}_us") or "no"})
    if any((prefix + f"m{i}_name") in f for i in range(4)):
        c["members"] = mem
    cons = []
    for i in range(6):
        if g(f"k{i}_name"):
            cons.append({"name": g(f"k{i}_name"), "us_person": g(f"k{i}_us") or "no", "corp": g(f"k{i}_corp") or "no",
                         "amount": g(f"k{i}_amount"), "tin": g(f"k{i}_tin")})
    if any((prefix + f"k{i}_name") in f for i in range(6)):
        c["contractors"] = cons


def ctx():
    try:
        import taxreturn
        fees, years = taxreturn.STATE_FEES, taxreturn.years_available()
    except Exception:
        fees, years = {}, []
    y = default_year()
    return {"state_fees": fees, "nr_years": years, "years": [str(y - 1), str(y)], "exp_keys": EXP_KEYS, "stages": STAGES,
            "stage_label": STAGE_LABEL, "groups": GROUPS, "money": _money, "nec_threshold": nec_threshold}


# ----------------------------------------------------------------------------- staff pages

@bp.get("/ustax")
def ustax_home():
    tab = request.args.get("tab") or "nr"
    rows = []
    for c in all_cases():
        name = " ".join(x for x in (c["owner"].get("first"), c["owner"].get("last")) if x) or "(no name yet)"
        for k, e in (c.get("filings") or {}).items():
            if e.get("active") and (tab == "all" or e.get("group") == tab):
                due = e.get("due") or ""
                days = (dt.date.fromisoformat(due) - dt.date.today()).days if due else None
                rows.append({"c": c, "name": name, "key": k, "e": e, "days": days})
    rows.sort(key=lambda r: (r["e"].get("stage") in DONE, r["e"].get("due") or "9999"))
    qs = [c for c in all_cases() if c.get("q", {}).get("token")]
    return render_template_string(HOME, tab=tab, rows=rows, qs=qs, cases=all_cases(), today=dt.date.today().isoformat(), **ctx())


@bp.post("/ustax/new")
def ustax_new():
    f = request.form
    c = blank_case(int(f.get("year") or default_year()))
    c["owner"].update(first=(f.get("first") or "").strip(), last=(f.get("last") or "").strip(),
                      email=(f.get("email") or "").strip())
    c["llc"]["name"] = (f.get("llc_name") or "").strip()
    c["staff"] = _staff()
    if not c["owner"]["first"] or not c["owner"]["last"]:
        flash("⚠ Enter the client's first and last name.")
        return redirect("/ustax?tab=q")
    if f.get("kind") == "link":
        tok = secrets.token_hex(16)
        c["source"] = "questionnaire"
        c["q"] = {"token": tok, "created": c["created"], "expires": (dt.date.today() + dt.timedelta(days=LINK_DAYS)).isoformat()}
        with _LOCK:
            t = _tokens()
            t[tok] = c["id"]
            OUT.mkdir(parents=True, exist_ok=True)
            TOKENS.write_text(json.dumps(t, indent=1))
        if f.get("send_email") and c["owner"]["email"]:
            try:
                it = _itin()
                it._send_mail(c["owner"]["email"], f"Your US tax return {c['year']} – questionnaire",
                              f"Dear {c['owner']['first']},\n\nPlease fill in this short questionnaire so we can prepare your US "
                              f"tax returns for {c['year']}:\n\n{it._base_url()}/sign/ustax/{tok}\n\nThe link works for {LINK_DAYS} days.\n\n"
                              "Startitup Global")
                flash(f"✔ Questionnaire emailed to {c['owner']['email']}.")
            except Exception as ex:
                flash(f"⚠ Link created but the email could not be sent: {ex}")
    sync_filings(c)
    c["history"].append({"when": c["created"], "what": "created (" + c["source"] + ")", "staff": _staff()})
    save_case(c)
    return redirect(f"/ustax/case/{c['id']}")


@bp.get("/ustax/case/<cid>")
def ustax_case(cid):
    c = load_case(cid)
    sync_filings(c)
    base = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0] + "://" + request.host
    return render_template_string(CASE, c=c, need=decide(c), base=base, nr_ready=c.get("nr_file"), **ctx())


@bp.post("/ustax/case/<cid>")
def ustax_case_save(cid):
    with _LOCK:
        c = load_case(cid)
        f = request.form
        if f.get("year") in ctx()["years"]:
            c["year"] = f["year"]
        read_form(c, f)
        for k, e in (c.get("filings") or {}).items():
            st = f.get(f"st_{k}")
            if st in STAGE_LABEL:
                if st != e.get("stage"):
                    c["history"].append({"when": dt.datetime.now().isoformat(timespec="seconds"),
                                         "what": f"{e.get('form', k)}: {STAGE_LABEL[st]}", "staff": _staff()})
                e["stage"] = st
            for fld in ("filed_date", "mtpo_ref", "notes"):
                if f.get(f"{fld}_{k}") is not None:
                    e[fld] = (f.get(f"{fld}_{k}") or "").strip()[:300]
        sync_filings(c)
        save_case(c)
    flash("✔ Saved – the filings needed were re-checked.")
    return redirect(f"/ustax/case/{cid}")


@bp.get("/ustax/case/<cid>/sheet.pdf")
def ustax_sheet(cid):
    c = load_case(cid)
    nm = re.sub(r"[^A-Za-z0-9]+", "_", c["llc"].get("name") or c["owner"].get("last") or "case")
    return send_file(io.BytesIO(sheet_pdf(c)), mimetype="application/pdf", as_attachment=request.args.get("inline") != "1",
                     download_name=f"US_tax_{c['year']}_{nm}_data_sheet.pdf")


@bp.get("/ustax/case/<cid>/sheet.csv")
def ustax_sheet_csv(cid):
    c = load_case(cid)
    nm = re.sub(r"[^A-Za-z0-9]+", "_", c["llc"].get("name") or c["owner"].get("last") or "case")
    return send_file(io.BytesIO(sheet_csv(c).encode()), mimetype="text/csv", as_attachment=True,
                     download_name=f"US_tax_{c['year']}_{nm}_data.csv")


@bp.post("/ustax/case/<cid>/1040nr")
def ustax_1040nr(cid):
    d = _case_dir(cid)
    with _LOCK:
        c = load_case(cid)
        try:
            pdf, comp, forms = build_1040nr(c)
        except Exception as ex:
            flash(f"⚠ {ex}")
            return redirect(f"/ustax/case/{cid}#filings")
        sig = d / "client_signature.png"
        if sig.exists() and request.form.get("signed"):
            import pymupdf
            it = _itin()
            doc = pymupdf.open(stream=pdf, filetype="pdf")
            if len(doc) > 1:
                doc[1].insert_image(pymupdf.Rect(*it.NR_SIGN_BOX), stream=sig.read_bytes(), keep_proportion=True, overlay=True)
                doc[1].insert_text(it.NR_DATE_AT, dt.date.today().strftime("%m/%d/%Y"), fontsize=9, fontname="helv", color=(0, 0, 0.55))
            buf = io.BytesIO()
            doc.save(buf, garbage=3, deflate=True)
            pdf = buf.getvalue()
        fname = f"1040NR_{c['year']}_{re.sub(r'[^A-Za-z0-9]+', '_', c['owner']['last'] or 'client')}.pdf"
        (d / fname).write_bytes(pdf)
        c["nr_file"] = {"file": fname, "forms": forms, "tax": comp.get("tax", 0), "net": comp.get("net", 0),
                        "when": dt.datetime.now().isoformat(timespec="seconds"), "warnings": comp.get("warnings", [])}
        e = c.get("filings", {}).get("1040nr")
        if e and e.get("stage") in ("info_requested", "info_received"):
            e["stage"] = "prepared"
        save_case(c)
    flash("✔ 1040-NR prepared: " + ", ".join(forms) + ".")
    return redirect(f"/ustax/case/{cid}#filings")


@bp.get("/ustax/case/<cid>/file/<name>")
def ustax_file(cid, name):
    d = _case_dir(cid)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name) or not (d / name).exists():
        abort(404)
    mt = "application/pdf" if name.endswith(".pdf") else "image/png"
    return send_file(d / name, mimetype=mt, as_attachment=request.args.get("inline") != "1", download_name=name)


@bp.get("/ustax/case/<cid>/zip")
def ustax_zip(cid):
    d = _case_dir(cid)
    c = load_case(cid)
    buf = io.BytesIO()
    nm = re.sub(r"[^A-Za-z0-9]+", "_", c["llc"].get("name") or c["owner"].get("last") or "case")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"1_data_sheet_{nm}.pdf", sheet_pdf(c))
        z.writestr(f"2_data_{nm}.csv", sheet_csv(c))
        if c.get("nr_file") and (d / c["nr_file"]["file"]).exists():
            z.write(d / c["nr_file"]["file"], "3_" + c["nr_file"]["file"])
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"US_tax_{c['year']}_{nm}_MTPO_pack.zip")


@bp.get("/ustax/export.csv")
def ustax_export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["client", "llc", "ein", "tax year", "form", "due", "stage", "filed", "mtpo ref", "case link"])
    for c in all_cases():
        for k, e in (c.get("filings") or {}).items():
            if e.get("active"):
                w.writerow([" ".join([c["owner"].get("first", ""), c["owner"].get("last", "")]).strip(), c["llc"].get("name", ""),
                            c["llc"].get("ein", ""), c["year"], e.get("form"), e.get("due"), STAGE_LABEL.get(e.get("stage"), ""),
                            e.get("filed_date"), e.get("mtpo_ref"), f"/ustax/case/{c['id']}"])
    return send_file(io.BytesIO(buf.getvalue().encode()), mimetype="text/csv", as_attachment=True,
                     download_name=f"US_tax_filings_{dt.date.today().isoformat()}.csv")


@bp.post("/ustax/case/<cid>/delete")
def ustax_delete(cid):
    d = _case_dir(cid)
    c = load_case(cid)
    if (request.form.get("password") or "") != os.environ.get("DELETE_PASSWORD", "askusama"):
        flash("⚠ Wrong delete password.")
        return redirect(f"/ustax/case/{cid}")
    if any(e.get("active") and e.get("stage") in ("filed", "accepted") for e in (c.get("filings") or {}).values()):
        flash("⚠ A filing of this case was already filed – it cannot be deleted (keep the record).")
        return redirect(f"/ustax/case/{cid}")
    import shutil
    with _LOCK:
        t = {k: v for k, v in _tokens().items() if v != cid}
        TOKENS.write_text(json.dumps(t, indent=1))
        shutil.rmtree(d, ignore_errors=True)
    flash("Case deleted.")
    return redirect("/ustax")


# ----------------------------------------------------------------------------- client questionnaire

def _q_case(token: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        abort(404)
    cid = _tokens().get(token)
    if not cid:
        abort(404)
    return load_case(cid)


@bp.get("/sign/ustax/<token>")
def q_page(token):
    c = _q_case(token)
    expired = bool(c["q"].get("expires") and c["q"]["expires"] < dt.date.today().isoformat())
    return render_template_string(QPAGE, c=c, token=token, err="", saved=request.args.get("saved"), expired=expired, **ctx())


@bp.get("/sign/ustax/<token>/postcode")
def q_postcode(token):
    _q_case(token)
    import postcode
    r = postcode.lookup(request.args.get("street", ""), request.args.get("city", ""), request.args.get("region", ""),
                        request.args.get("country", ""))
    return jsonify({"postcode": r.get("postcode", ""),
                    "note": f"We found postcode {r['postcode']} – please check it." if r.get("postcode") else "Please type your postcode."})


@bp.post("/sign/ustax/<token>")
def q_submit(token):
    c = _q_case(token)
    if c["q"].get("submitted"):
        return redirect(f"/sign/ustax/{token}")
    f = request.form
    with _LOCK:
        c = _q_case(token)
        read_form(c, f)
        c["q"]["saved"] = dt.datetime.now().isoformat(timespec="seconds")
        save_case(c)
    if f.get("action") == "save":
        return redirect(f"/sign/ustax/{token}?saved=1")
    o, l = c["owner"], c["llc"]
    errs = []
    for k, lbl in (("first", "first name"), ("last", "last name"), ("citizenship", "citizenship"), ("street", "home address"),
                   ("city", "city"), ("country", "country"), ("phone", "phone number")):
        if not o.get(k):
            errs.append(f"Please enter your {lbl}.")
    if f.get("has_llc") == "yes":
        for k, lbl in (("name", "company name"), ("state", "state of the company"), ("formed", "date the company was formed"),
                       ("activity", "what the company does")):
            if not l.get(k):
                errs.append(f"Please enter the {lbl}.")
    if len((f.get("typed_name") or "").strip()) < 3:
        errs.append("Please type your full name to sign.")
    sig = f.get("sig") or ""
    if not sig.startswith("data:image/png;base64,") or len(sig) < 1500:
        errs.append("Please draw your signature in the box.")
    if not f.get("decl"):
        errs.append("Please tick the declaration.")
    if errs:
        return render_template_string(QPAGE, c=c, token=token, err=" ".join(errs), saved=False, expired=False, **ctx())
    with _LOCK:
        c = _q_case(token)
        if f.get("has_llc") != "yes":
            c["llc"]["name"] = ""
        (OUT / c["id"] / "client_signature.png").write_bytes(base64.b64decode(sig.split(",", 1)[1]))
        now = dt.datetime.now(dt.timezone.utc)
        c["q"].update(submitted=dt.datetime.now().isoformat(timespec="seconds"), typed_name=(f.get("typed_name") or "").strip(),
                      ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip(),
                      when_utc=now.strftime("%Y-%m-%d %H:%M:%S"))
        c["history"].append({"when": c["q"]["submitted"], "what": "questionnaire submitted and signed", "staff": "client"})
        sync_filings(c)
        for e in c["filings"].values():
            if e.get("stage") == "info_requested":
                e["stage"] = "info_received"
        save_case(c)
    try:
        it = _itin()
        notify = it.load_caa().get("notify_email")
        if notify:
            it._send_mail(notify, f"US tax questionnaire submitted – {o['first']} {o['last']}",
                          f"{o['first']} {o['last']} submitted the {c['year']} US tax questionnaire.\n\n{it._base_url()}/ustax/case/{c['id']}\n")
    except Exception:
        pass
    return redirect(f"/sign/ustax/{token}")


def register(app):
    OUT.mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)

    @app.context_processor
    def _ustax_badge():
        try:
            return {"ustax_alerts": alert_count()}
        except Exception:
            return {"ustax_alerts": 0}


# ----------------------------------------------------------------------------- templates

STYLE = """<style>
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 14px}.tabs a{padding:8px 14px;border-radius:8px;background:#f1f5f9;color:#111;text-decoration:none;font-weight:600;font-size:14px}
.tabs a.on{background:#111;color:#fff}
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:4px 16px}
table.t{width:100%;border-collapse:collapse;font-size:14px}table.t th,table.t td{border-bottom:1px solid var(--line);padding:7px 6px;text-align:left;vertical-align:top}
.pill{display:inline-block;border-radius:10px;padding:2px 9px;font-size:12px;font-weight:700;background:#e5e7eb}
.pill.late{background:#fee2e2;color:#991b1b}.pill.soon{background:#ffedd5;color:#9a3412}.pill.ok{background:#dcfce7;color:#166534}
.why{font-size:12px;color:#555}.okb{background:#e8f7ee;border:1px solid #86efac;padding:10px 14px;border-radius:8px;margin:10px 0}
.warn{background:#fef9c3;border:1px solid #fde047;padding:10px 14px;border-radius:8px;margin:10px 0}
select,input[type=date]{padding:7px;border:1px solid #cbd5e1;border-radius:6px;font-family:inherit}
</style>"""

HOME = r"""{% extends "base" %}{% block body %}""" + STYLE + r"""
<div class="card"><h1 style="margin-bottom:4px">US Tax <span class="muted" style="font-size:15px">– year-end returns for clients and their US LLCs</span></h1>
<p class="muted" style="margin-top:0">Send the client a questionnaire (or add them yourself). The system works out which returns are needed, prepares the data sheet
(and the 1040-NR) and tracks each filing until MyTaxPrepOffice has filed it.</p>
<div class="tabs">{% for k, v in groups.items() %}<a href="/ustax?tab={{k}}" class="{{'on' if tab==k}}">{{v}}</a>{% endfor %}
<a href="/ustax?tab=all" class="{{'on' if tab=='all'}}">All filings</a><a href="/ustax?tab=q" class="{{'on' if tab=='q'}}">Clients &amp; questionnaires</a>
<a href="/ustax/export.csv" style="margin-left:auto">⬇ Export all (CSV)</a></div>
{% if tab != 'q' %}
<table class="t"><tr><th>Client / LLC</th><th>Form</th><th>Tax year</th><th>Due</th><th>Stage</th><th></th></tr>
{% for r in rows %}<tr><td><b>{{r.name}}</b><br><span class="muted">{{r.c.llc.name or '–'}}</span></td><td>{{r.e.form}}<br><span class="why">{{r.e.title}}</span></td>
<td>{{r.c.year}}</td><td>{% if r.e.due %}{{r.e.due|ukdate}}<br>{% if r.e.stage not in ('filed','accepted','not_needed') %}{% if r.days < 0 %}<span class="pill late">{{-r.days}} days late</span>{% elif r.days <= 21 %}<span class="pill soon">{{r.days}} days</span>{% endif %}{% endif %}{% else %}–{% endif %}</td>
<td><span class="pill {{'ok' if r.e.stage in ('filed','accepted','not_needed')}}">{{stage_label[r.e.stage]}}</span></td><td><a class="btn small" href="/ustax/case/{{r.c.id}}#filings">Open</a></td></tr>
{% else %}<tr><td colspan="6" class="muted">Nothing here yet – add a client under "Clients &amp; questionnaires".</td></tr>{% endfor %}</table>
{% else %}
<div class="fgrid" style="align-items:start">
<form method="post" action="/ustax/new" class="card" style="margin:0"><h3 style="margin-top:0">📨 Send a questionnaire</h3>
<input type="hidden" name="kind" value="link">
<div><label>First name</label><input type="text" name="first" required></div><div><label>Last name</label><input type="text" name="last" required></div>
<div><label>Client email</label><input type="email" name="email"></div>
<div><label>Tax year</label><select name="year">{% for y in years %}<option {{'selected' if loop.last}}>{{y}}</option>{% endfor %}</select></div>
<label class="tick" style="font-weight:400"><input type="checkbox" name="send_email" value="1" checked> Email the link to the client</label>
<button class="btn" type="submit">Create questionnaire link</button></form>
<form method="post" action="/ustax/new" class="card" style="margin:0"><h3 style="margin-top:0">✍️ Add a client manually</h3>
<input type="hidden" name="kind" value="manual">
<div><label>First name</label><input type="text" name="first" required></div><div><label>Last name</label><input type="text" name="last" required></div>
<div><label>LLC name (if any)</label><input type="text" name="llc_name"></div>
<div><label>Tax year</label><select name="year">{% for y in years %}<option {{'selected' if loop.last}}>{{y}}</option>{% endfor %}</select></div>
<button class="btn" type="submit">Add and fill in</button></form></div>
<h3>Clients</h3><table class="t"><tr><th>Client</th><th>LLC</th><th>Year</th><th>Questionnaire</th><th>Filings</th><th></th></tr>
{% for c in cases %}<tr><td>{{c.owner.first}} {{c.owner.last}}</td><td>{{c.llc.name or '–'}}</td><td>{{c.year}}</td>
<td>{% if c.q.submitted %}✔ submitted {{c.q.submitted|ukdate}}{% elif c.q.token %}⏳ sent – waiting{% else %}manual{% endif %}</td>
<td>{% for k, e in c.filings.items() if e.active %}{{e.form.split(' (')[0]}}{% if not loop.last %} · {% endif %}{% endfor %}</td>
<td><a class="btn small" href="/ustax/case/{{c.id}}">Open</a></td></tr>{% else %}<tr><td colspan="6" class="muted">No clients yet.</td></tr>{% endfor %}</table>
{% endif %}</div>
{% endblock %}"""

FIELDS = r"""
<h3>Client (owner)</h3><div class="fgrid">
 <div><label>First name</label><input type="text" name="o_first" value="{{c.owner.first}}"></div>
 <div><label>Middle name</label><input type="text" name="o_middle" value="{{c.owner.middle}}"></div>
 <div><label>Last name</label><input type="text" name="o_last" value="{{c.owner.last}}"></div>
 <div><label>Citizenship</label><input type="text" name="o_citizenship" value="{{c.owner.citizenship}}"></div>
 <div><label>US citizen / green card holder?</label><select name="o_us_person"><option value="no">No</option><option value="yes" {{'selected' if c.owner.us_person=='yes'}}>Yes</option></select></div>
 <div><label>ITIN / SSN (if any)</label><input type="text" name="o_itin" value="{{c.owner.itin}}"></div>
 <div><label>Foreign tax ID</label><input type="text" name="o_foreign_tin" value="{{c.owner.foreign_tin}}"></div>
 <div><label>Occupation</label><input type="text" name="o_occupation" value="{{c.owner.occupation}}"></div>
 <div><label>Phone</label><input type="text" name="o_phone" value="{{c.owner.phone}}"></div>
 <div><label>Email</label><input type="text" name="o_email" value="{{c.owner.email}}"></div>
 <div><label>Home street</label><input type="text" name="o_street" value="{{c.owner.street}}"></div>
 <div><label>City</label><input type="text" name="o_city" value="{{c.owner.city}}"></div>
 <div><label>Province / state</label><input type="text" name="o_province" value="{{c.owner.province}}"></div>
 <div><label>Postcode</label><input type="text" name="o_postal" value="{{c.owner.postal}}"></div>
 <div><label>Country</label><input type="text" name="o_country" value="{{c.owner.country}}"></div>
 <div><label>Days in the US {{c.year}}</label><input type="text" name="o_days_y0" value="{{c.owner.days_y0}}"></div>
 <div><label>Days {{c.year|int - 1}}</label><input type="text" name="o_days_y1" value="{{c.owner.days_y1}}"></div>
 <div><label>Days {{c.year|int - 2}}</label><input type="text" name="o_days_y2" value="{{c.owner.days_y2}}"></div>
 <div><label>US visits {{c.year}} (arrived – left)</label><input type="text" name="o_trips" value="{{c.owner.trips}}"></div>
 <div><label>Filed a US return before?</label><select name="o_prior_return"><option value="no">No</option><option value="yes" {{'selected' if c.owner.prior_return=='yes'}}>Yes</option></select></div>
</div>
<h3>US LLC <span class="muted" style="font-size:13px;font-weight:400">(leave the name empty if the client has no US company)</span></h3><div class="fgrid">
 <div><label>LLC name</label><input type="text" name="l_name" value="{{c.llc.name}}"></div>
 <div><label>EIN</label><input type="text" name="l_ein" value="{{c.llc.ein}}"></div>
 <div><label>State</label><select name="l_state" id="l_state"><option value="">Choose…</option>{% for st, (fee, chk) in state_fees.items() %}<option value="{{st}}" data-fee="{{fee}}" {{'selected' if c.llc.state==st}}>{{st}} (${{fee}})</option>{% endfor %}</select></div>
 <div><label>Date formed</label><input type="date" name="l_formed" value="{{c.llc.formed}}"></div>
 <div><label>Business activity</label><input type="text" name="l_activity" value="{{c.llc.activity}}"></div>
 <div><label>Business code (NAICS)</label><input type="text" name="l_naics" value="{{c.llc.naics}}"></div>
 <div><label>US street address</label><input type="text" name="l_street" value="{{c.llc.street}}"></div>
 <div><label>City, state, ZIP</label><input type="text" name="l_city_state_zip" value="{{c.llc.city_state_zip}}"></div>
 <div><label>Tax classification</label><select name="l_classification"><option value="default">Default (no election)</option><option value="corporation" {{'selected' if c.llc.classification=='corporation'}}>Elected corporation (8832 / 2553)</option></select></div>
 <div><label>US office, staff, agent or work done in the US?</label><select name="l_us_presence">{% for v, t in [('no','No'),('yes','Yes'),('unsure','Not sure')] %}<option value="{{v}}" {{'selected' if c.llc.us_presence==v}}>{{t}}</option>{% endfor %}</select></div>
 <div><label>Bank account outside the US?</label><select name="l_foreign_bank"><option value="no">No</option><option value="yes" {{'selected' if c.llc.foreign_bank=='yes'}}>Yes</option></select></div>
 <div><label>Highest total balance outside the US ($)</label><input type="text" name="l_foreign_bank_max" value="{{c.llc.foreign_bank_max}}"></div>
</div>
<h3>Other members <span class="muted" style="font-size:13px;font-weight:400">(only if the LLC has more than one owner)</span></h3>
<table class="t"><tr><th>Name</th><th>Country</th><th>%</th><th>US person?</th></tr>
{% for i in range(4) %}{% set m = c.members[i] if c.members|length > i else {} %}<tr><td><input type="text" name="m{{i}}_name" value="{{m.name}}"></td><td><input type="text" name="m{{i}}_country" value="{{m.country}}"></td>
<td><input type="text" name="m{{i}}_pct" value="{{m.pct}}" style="width:70px"></td><td><select name="m{{i}}_us"><option value="no">No</option><option value="yes" {{'selected' if m.us_person=='yes'}}>Yes</option></select></td></tr>{% endfor %}</table>
<h3>Figures for {{c.year}} (US dollars)</h3><div class="fgrid">
 <div><label>Sales / income</label><input type="text" name="f_sales" value="{{c.fig.sales}}"></div>
 <div><label>Returns / refunds</label><input type="text" name="f_returns" value="{{c.fig.returns}}"></div>
 <div><label>Cost of goods sold</label><input type="text" name="f_cogs" value="{{c.fig.cogs}}"></div>
 {% for k, lbl in exp_keys %}<div><label>{{lbl}}</label><input type="text" name="f_exp_{{k}}" id="f_exp_{{k}}" value="{{c.fig['exp_'~k]}}"></div>{% endfor %}
 <div><label>Other costs – description</label><input type="text" name="f_exp_other_desc" value="{{c.fig.exp_other_desc}}"></div>
 <div><label>Money the owner put in</label><input type="text" name="f_contributions" value="{{c.fig.contributions}}"></div>
 <div><label>Money the owner took out</label><input type="text" name="f_distributions" value="{{c.fig.distributions}}"></div>
 <div><label>Loans owner → LLC</label><input type="text" name="f_loans_from_owner" value="{{c.fig.loans_from_owner}}"></div>
 <div><label>Loans LLC → owner</label><input type="text" name="f_loans_to_owner" value="{{c.fig.loans_to_owner}}"></div>
 <div><label>Bank balance 31 December</label><input type="text" name="f_bank_end" value="{{c.fig.bank_end}}"></div>
 <div><label>Total assets 31 December</label><input type="text" name="f_total_assets" value="{{c.fig.total_assets}}"></div>
 <div><label>Dividends paid (corporation only)</label><input type="text" name="f_dividends_paid" value="{{c.fig.dividends_paid}}"></div>
</div>
<h3>Contractors paid in {{c.year}}</h3>
<table class="t"><tr><th>Name</th><th>US person?</th><th>A corporation?</th><th>Amount ($)</th><th>Their TIN (US only)</th></tr>
{% for i in range(6) %}{% set k = c.contractors[i] if c.contractors|length > i else {} %}<tr><td><input type="text" name="k{{i}}_name" value="{{k.name}}"></td>
<td><select name="k{{i}}_us"><option value="no">No</option><option value="yes" {{'selected' if k.us_person=='yes'}}>Yes</option></select></td>
<td><select name="k{{i}}_corp"><option value="no">No</option><option value="yes" {{'selected' if k.corp=='yes'}}>Yes</option></select></td>
<td><input type="text" name="k{{i}}_amount" value="{{k.amount}}" style="width:110px"></td><td><input type="text" name="k{{i}}_tin" value="{{k.tin}}"></td></tr>{% endfor %}</table>
"""

CASE = r"""{% extends "base" %}{% block body %}""" + STYLE + r"""
{% set nm = (c.owner.first ~ ' ' ~ c.owner.last).strip() %}
<div class="card"><h1 style="margin-bottom:2px">{{nm or 'New client'}} <span class="muted" style="font-size:15px">{{c.llc.name}} · tax year {{c.year}}</span></h1>
<div class="btnrow" style="margin-top:10px"><a class="btn" href="/ustax/case/{{c.id}}/zip">⬇ MyTaxPrepOffice pack (ZIP)</a>
<a class="btn secondary" href="/ustax/case/{{c.id}}/sheet.pdf?inline=1" target="_blank">📄 Data sheet (PDF)</a>
<a class="btn secondary" href="/ustax/case/{{c.id}}/sheet.csv">⬇ Data (CSV)</a><a class="btn secondary" href="/ustax">All US Tax</a></div>
{% if c.q.token %}<p class="muted" style="margin-bottom:0">Questionnaire: {% if c.q.submitted %}✔ submitted and signed {{c.q.submitted|ukdate}} by {{c.q.typed_name}}
{% if c.q.submitted %} · <a href="/ustax/case/{{c.id}}/file/client_signature.png?inline=1" target="_blank">signature</a>{% endif %}{% else %}⏳ waiting for the client – link:
<input type="text" readonly value="{{base}}/sign/ustax/{{c.q.token}}" onclick="this.select()" style="width:420px"> (valid until {{c.q.expires|ukdate}}){% endif %}</p>{% endif %}</div>

<div class="card" id="filings"><h2 style="margin-top:0">Filings this case needs</h2>
<p class="muted" style="margin-top:0">Worked out from the answers below – they update when you save. Enter each in MyTaxPrepOffice from the data sheet, then move the stage along.</p>
<form method="post" action="/ustax/case/{{c.id}}">
<table class="t"><tr><th style="width:34%">Form</th><th>Due</th><th>Stage</th><th>Filed / MTPO ref / notes</th></tr>
{% for x in need %}{% set e = c.filings[x.key] %}<tr><td><b>{{x.form}}</b> – {{x.title}}<div class="why">{{x.why}}</div><div class="why">➜ {{x.how}}</div>
{% if x.key == '1040nr' %}<div style="margin-top:6px">{% if nr_ready %}✔ <a href="/ustax/case/{{c.id}}/file/{{nr_ready.file}}?inline=1" target="_blank">{{nr_ready.file}}</a> <span class="why">({{nr_ready.forms|join(', ')}}{% if nr_ready.tax %}, tax ${{nr_ready.tax}}{% endif %})</span>{% endif %}
<button class="btn small" formaction="/ustax/case/{{c.id}}/1040nr" formmethod="post" {{'disabled' if c.year|int not in nr_years}}>📄 Prepare 1040-NR PDF</button>
<label style="font-weight:400;font-size:12px"><input type="checkbox" name="signed" value="1" checked> with client's signature</label>
{% if c.year|int not in nr_years %}<div class="why">The {{c.year}} 1040-NR forms are not installed yet – use the data sheet.</div>{% endif %}</div>{% endif %}</td>
<td>{{x.due|ukdate if x.due else '–'}}</td>
<td><select name="st_{{x.key}}">{% for k, v in stages %}<option value="{{k}}" {{'selected' if e.stage==k}}>{{v}}</option>{% endfor %}</select></td>
<td><input type="date" name="filed_date_{{x.key}}" value="{{e.filed_date}}"><input type="text" name="mtpo_ref_{{x.key}}" value="{{e.mtpo_ref}}" placeholder="MTPO / IRS ref" style="margin-top:4px"><input type="text" name="notes_{{x.key}}" value="{{e.notes}}" placeholder="notes" style="margin-top:4px"></td></tr>
{% else %}<tr><td colspan="4" class="muted">Fill in the client and LLC details below and press Save.</td></tr>{% endfor %}</table>
</div>
<div class="card"><h2 style="margin-top:0">Details</h2>
<div><label>Tax year</label><select name="year">{% for y in years %}<option {{'selected' if c.year==y}}>{{y}}</option>{% endfor %}</select></div>
""" + FIELDS + r"""
<div class="btnrow" style="margin-top:14px"><button class="btn" type="submit">Save and re-check the filings</button></div></form>
{% if c.history %}<details style="margin-top:12px"><summary class="muted" style="cursor:pointer">History</summary><ul>{% for h in c.history %}<li>{{h.when|ukdate}} – {{h.what}} ({{h.staff}})</li>{% endfor %}</ul></details>{% endif %}
<details style="margin-top:8px"><summary class="muted" style="cursor:pointer">Delete this case</summary>
<form method="post" action="/ustax/case/{{c.id}}/delete" onsubmit="return confirm('Delete this case?')"><input type="password" name="password" placeholder="delete password"> <button class="btn small secondary">Delete</button></form></details>
</div>
<script>(function(){var s=document.getElementById('l_state'),fb=document.getElementById('f_exp_formation'),fd=document.querySelector('[name=l_formed]'),yr='{{c.year}}';
function fee(user){var o=s.options[s.selectedIndex];if(!o||!o.value)return;if(fd.value&&fd.value.slice(0,4)!=yr)return;if(user&&!fb.value.trim()){fb.value=o.getAttribute('data-fee');fb.style.background='#fef9c3'}}
s.addEventListener('change',function(){fee(true)});fd.addEventListener('change',function(){fee(true)});})();</script>
{% endblock %}"""

QPAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>US tax questionnaire {{c.year}}</title><style>
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f6f7f9;margin:0;color:#111}.wrap{max-width:760px;margin:0 auto;padding:18px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 18px;margin:14px 0}h1{font-size:22px}h2{font-size:18px;margin:0 0 6px}
label{display:block;font-weight:600;font-size:14px;margin:10px 0 4px}input[type=text],input[type=email],input[type=tel],input[type=date],select,textarea{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cbd5e1;border-radius:8px;font-size:15px;font-family:inherit}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}@media(max-width:560px){.row2{grid-template-columns:1fr}}.muted{color:#666;font-size:14px}
.c{font-weight:400;display:flex;gap:8px;align-items:flex-start}.c input{margin-top:3px}.hide{display:none}.btn{background:#111;color:#fff;border:0;border-radius:8px;padding:12px 18px;font-size:15px;cursor:pointer}
.btn.sec{background:#e5e7eb;color:#111}.err{background:#fee2e2;border:1px solid #fca5a5;color:#991b1b;padding:10px 14px;border-radius:8px}.ok{background:#e8f7ee;border:1px solid #86efac;padding:10px 14px;border-radius:8px}
.step{display:inline-flex;width:26px;height:26px;border-radius:50%;background:#111;color:#fff;font-size:14px;align-items:center;justify-content:center;margin-right:8px}
canvas{width:100%;height:170px;border:2px dashed #94a3b8;border-radius:10px;background:#fff;touch-action:none}.req:after{content:" *";color:#dc2626}
table{width:100%;border-collapse:collapse}td{padding:3px}</style></head><body><div class="wrap">
<h1>Your US tax returns {{c.year}}</h1>
{% if c.q.submitted %}<div class="card ok"><b>Thank you – we have received your answers.</b><p>We will prepare your US tax returns for {{c.year}} and contact you if we need anything else.</p></div>
{% elif expired %}<div class="card err">This link has expired – please ask us for a new one.</div>
{% else %}
<p class="muted">A few simple questions about you, your US company and its money in {{c.year}} (1 January – 31 December {{c.year}}). Answer what you know – we will check everything. You can save and finish later.</p>
{% if err %}<div class="err">{{err}}</div>{% endif %}{% if saved %}<div class="ok">✔ Saved – you can come back to this link any time.</div>{% endif %}
<form method="post" id="qf" onsubmit="return go()"><input type="hidden" name="action" id="action" value="submit">
<div class="card"><h2><span class="step">1</span>About you</h2>
<div class="row2"><div><label class="req">First name</label><input type="text" name="o_first" value="{{c.owner.first}}"></div><div><label>Middle name</label><input type="text" name="o_middle" value="{{c.owner.middle}}"></div></div>
<label class="req">Last name</label><input type="text" name="o_last" value="{{c.owner.last}}">
<div class="row2"><div><label class="req">Citizenship (country)</label><input type="text" name="o_citizenship" value="{{c.owner.citizenship}}"></div>
<div><label>Are you a US citizen or green-card holder?</label><select name="o_us_person"><option value="no">No</option><option value="yes" {{'selected' if c.owner.us_person=='yes'}}>Yes</option></select></div></div>
<div class="row2"><div><label class="req">Mobile number</label><input type="tel" name="o_phone" value="{{c.owner.phone}}" placeholder="+92 300 1234567"></div><div><label>Email</label><input type="email" name="o_email" value="{{c.owner.email}}"></div></div>
<div class="row2"><div><label>Your ITIN (if you have one)</label><input type="text" name="o_itin" value="{{c.owner.itin}}" placeholder="9XX-XX-XXXX"></div><div><label>Your tax number at home</label><input type="text" name="o_foreign_tin" value="{{c.owner.foreign_tin}}" placeholder="e.g. NTN, UTR"></div></div>
<label class="req">Home address – house and street</label><input type="text" name="o_street" value="{{c.owner.street}}">
<div class="row2"><div><label class="req">City</label><input type="text" name="o_city" value="{{c.owner.city}}"></div><div><label>Province / state</label><input type="text" name="o_province" value="{{c.owner.province}}"></div></div>
<div class="row2"><div><label class="req">Country</label><input type="text" name="o_country" value="{{c.owner.country}}"></div>
<div><label>Postcode <a href="#" onclick="return pc()" style="font-weight:400">find it</a></label><input type="text" name="o_postal" value="{{c.owner.postal}}"><div id="pcn" class="muted" style="font-size:13px"></div></div></div>
<label>Your occupation</label><input type="text" name="o_occupation" value="{{c.owner.occupation or 'Business owner'}}"></div>

<div class="card"><h2><span class="step">2</span>Your US company</h2>
<label class="req">Do you have a US company (LLC)?</label>
<label class="c"><input type="radio" name="has_llc" value="yes" {{'checked' if c.llc.name or not c.q.saved}}> Yes</label>
<label class="c"><input type="radio" name="has_llc" value="no" {{'checked' if c.q.saved and not c.llc.name}}> No</label>
<div id="x_llc">
<div class="row2"><div><label class="req">Company name</label><input type="text" name="l_name" value="{{c.llc.name}}"></div><div><label>Company EIN</label><input type="text" name="l_ein" value="{{c.llc.ein}}" placeholder="12-3456789"></div></div>
<div class="row2"><div><label class="req">State</label><select name="l_state" id="l_state"><option value="">Choose…</option>{% for st, (fee, chk) in state_fees.items() %}<option value="{{st}}" data-fee="{{fee}}" {{'selected' if c.llc.state==st}}>{{st}}</option>{% endfor %}</select></div>
<div><label class="req">Date it was formed</label><input type="date" name="l_formed" value="{{c.llc.formed}}"></div></div>
<label class="req">What does it do?</label><input type="text" name="l_activity" value="{{c.llc.activity}}" placeholder="e.g. online shop, software services">
<div class="row2"><div><label>Company US address</label><input type="text" name="l_street" value="{{c.llc.street}}"></div><div><label>City, state, ZIP</label><input type="text" name="l_city_state_zip" value="{{c.llc.city_state_zip}}"></div></div>
<label>How many owners does the company have?</label><select name="n_owners" id="n_owners"><option value="1">Only me</option><option value="2" {{'selected' if c.members}}>More than one</option></select>
<div id="x_mem" class="hide"><p class="muted">The other owners:</p><table>{% for i in range(3) %}{% set m = c.members[i] if c.members|length > i else {} %}<tr>
<td><input type="text" name="m{{i}}_name" value="{{m.name}}" placeholder="Full name"></td><td><input type="text" name="m{{i}}_country" value="{{m.country}}" placeholder="Country"></td>
<td style="width:80px"><input type="text" name="m{{i}}_pct" value="{{m.pct}}" placeholder="%"></td><td><select name="m{{i}}_us"><option value="no">Not US</option><option value="yes" {{'selected' if m.us_person=='yes'}}>US person</option></select></td></tr>{% endfor %}</table></div>
<label>Did the company choose to be taxed as a corporation (Form 8832 or 2553)?</label>
<select name="l_classification"><option value="default">No / I don't know</option><option value="corporation" {{'selected' if c.llc.classification=='corporation'}}>Yes</option></select>
<label>Does the company have an office, staff, a warehouse or an agent in the USA – or did you work for it while in the USA?</label>
<select name="l_us_presence">{% for v, t in [('no','No'),('yes','Yes'),('unsure','Not sure')] %}<option value="{{v}}" {{'selected' if c.llc.us_presence==v}}>{{t}}</option>{% endfor %}</select>
<label>Does the company have a bank account outside the USA?</label>
<div class="row2"><select name="l_foreign_bank"><option value="no">No</option><option value="yes" {{'selected' if c.llc.foreign_bank=='yes'}}>Yes</option></select>
<input type="text" name="l_foreign_bank_max" value="{{c.llc.foreign_bank_max}}" placeholder="Highest balance in the year ($)"></div></div></div>

<div class="card" id="x_money"><h2><span class="step">3</span>The company's money in {{c.year}}</h2><p class="muted" style="margin:0">US dollars. Only what was actually received or paid – keep the receipts. Leave empty if none.</p>
<div class="row2"><div><label>Sales / income</label><input type="text" name="f_sales" value="{{c.fig.sales}}"></div><div><label>Cost of the goods sold</label><input type="text" name="f_cogs" value="{{c.fig.cogs}}"></div></div>
<div class="row2">{% for k, lbl in exp_keys %}<div><label>{{lbl}}</label><input type="text" name="f_exp_{{k}}" id="f_exp_{{k}}" value="{{c.fig['exp_'~k]}}">{% if k=='formation' %}<div id="feen" class="muted" style="font-size:13px"></div>{% endif %}</div>{% endfor %}</div>
<label>What were the other costs?</label><input type="text" name="f_exp_other_desc" value="{{c.fig.exp_other_desc}}">
<div class="row2"><div><label>Money you put into the company (including costs you paid yourself)</label><input type="text" name="f_contributions" value="{{c.fig.contributions}}"></div>
<div><label>Money you took out of the company</label><input type="text" name="f_distributions" value="{{c.fig.distributions}}"></div></div>
<div class="row2"><div><label>Loans you gave the company</label><input type="text" name="f_loans_from_owner" value="{{c.fig.loans_from_owner}}"></div><div><label>Loans the company gave you</label><input type="text" name="f_loans_to_owner" value="{{c.fig.loans_to_owner}}"></div></div>
<label>Bank balance on 31 December {{c.year}}</label><input type="text" name="f_bank_end" value="{{c.fig.bank_end}}">
<p style="margin:14px 0 4px"><b>People or companies the company paid for work</b> (freelancers, contractors)</p>
<table>{% for i in range(4) %}{% set k = c.contractors[i] if c.contractors|length > i else {} %}<tr><td><input type="text" name="k{{i}}_name" value="{{k.name}}" placeholder="Name"></td>
<td><select name="k{{i}}_us"><option value="no">Outside USA</option><option value="yes" {{'selected' if k.us_person=='yes'}}>In the USA</option></select></td>
<td><select name="k{{i}}_corp"><option value="no">Person</option><option value="yes" {{'selected' if k.corp=='yes'}}>Company</option></select></td>
<td style="width:110px"><input type="text" name="k{{i}}_amount" value="{{k.amount}}" placeholder="$"></td></tr>{% endfor %}</table></div>

<div class="card"><h2><span class="step">4</span>Time in the USA</h2>
<div class="row2"><div><label>Days in the USA in {{c.year}}</label><input type="text" name="o_days_y0" value="{{c.owner.days_y0 or '0'}}"></div><div><label>Days in {{c.year|int - 1}}</label><input type="text" name="o_days_y1" value="{{c.owner.days_y1 or '0'}}"></div></div>
<label>Days in {{c.year|int - 2}}</label><input type="text" name="o_days_y2" value="{{c.owner.days_y2 or '0'}}">
<label>Your visits in {{c.year}} – arrived – left (day/month/year), one per line</label><textarea name="o_trips" rows="2">{{c.owner.trips}}</textarea>
<label>Have you filed a US tax return before?</label><select name="o_prior_return"><option value="no">No</option><option value="yes" {{'selected' if c.owner.prior_return=='yes'}}>Yes</option></select></div>

<div class="card"><h2><span class="step">5</span>Sign</h2>
<label class="c"><input type="checkbox" name="decl" value="1"> I confirm that this information is true and complete to the best of my knowledge, and I authorise Startitup Global to prepare my US tax returns for {{c.year}} from it.</label>
<label class="req">Type your full name</label><input type="text" name="typed_name">
<label class="req">Draw your signature</label><canvas id="pad"></canvas><div style="margin-top:8px"><button type="button" class="btn sec" onclick="clr()">Clear</button></div>
<input type="hidden" name="sig" id="sig">
<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px"><button class="btn" type="submit" onclick="$('action').value='submit'">Submit</button>
<button class="btn sec" type="submit" onclick="$('action').value='save'">Save and finish later</button></div></div>
</form>
<script>
function $(i){return document.getElementById(i)}
function val(n){var e=document.querySelector('[name='+n+']:checked');return e?e.value:''}
function show(){var y=val('has_llc')=='yes';$('x_llc').classList.toggle('hide',!y);$('x_money').classList.toggle('hide',!y);$('x_mem').classList.toggle('hide',$('n_owners').value!='2')}
$('qf').addEventListener('change',show);show();
(function(){var s=$('l_state'),fb=$('f_exp_formation'),fd=document.querySelector('[name=l_formed]'),n=$('feen');
function fee(user){var o=s.options[s.selectedIndex];if(!o||!o.value){n.textContent='';return}
 if(fd.value&&fd.value.slice(0,4)!='{{c.year}}'){n.textContent='Formed before {{c.year}} – leave empty.';return}
 if(user&&!fb.value.trim())fb.value=o.getAttribute('data-fee');n.textContent='Usual '+o.value+' fee: $'+o.getAttribute('data-fee')+' – change it if you paid a different amount.'}
s.addEventListener('change',function(){fee(true)});fd.addEventListener('change',function(){fee(true)});fee(false)})();
function pc(){var g=function(n){return document.querySelector('[name='+n+']').value};$('pcn').textContent='Looking up…';
 fetch('/sign/ustax/{{token}}/postcode?street='+encodeURIComponent(g('o_street'))+'&city='+encodeURIComponent(g('o_city'))+'&region='+encodeURIComponent(g('o_province'))+'&country='+encodeURIComponent(g('o_country')))
 .then(function(r){return r.json()}).then(function(r){if(r.postcode&&!g('o_postal')){document.querySelector('[name=o_postal]').value=r.postcode}$('pcn').textContent=r.note}).catch(function(){$('pcn').textContent=''});return false}
var c=$('pad'),x=c.getContext('2d'),drawing=false,drawn=0;
function size(){var r=c.getBoundingClientRect(),d=window.devicePixelRatio||1;c.width=r.width*d;c.height=r.height*d;x.scale(d,d);x.lineWidth=2.4;x.lineCap='round';x.lineJoin='round';x.strokeStyle='#0b1f66'}size();
function pt(e){var r=c.getBoundingClientRect(),t=e.touches?e.touches[0]:e;return[t.clientX-r.left,t.clientY-r.top]}
function down(e){e.preventDefault();drawing=true;var p=pt(e);x.beginPath();x.moveTo(p[0],p[1])}
function move(e){if(!drawing)return;e.preventDefault();var p=pt(e);x.lineTo(p[0],p[1]);x.stroke();drawn++}
function up(){drawing=false}
c.addEventListener('mousedown',down);c.addEventListener('mousemove',move);window.addEventListener('mouseup',up);
c.addEventListener('touchstart',down);c.addEventListener('touchmove',move);c.addEventListener('touchend',up);
function clr(){x.clearRect(0,0,c.width,c.height);drawn=0}
function go(){if($('action').value=='save')return true;if(drawn<8){alert('Please draw your signature in the box (or press "Save and finish later").');return false}$('sig').value=c.toDataURL('image/png');return true}
</script>{% endif %}
<p class="muted" style="text-align:center;margin-top:20px">Startitup Global Inc · secure link · your answers are only used to prepare your US tax returns</p>
</div></body></html>"""
