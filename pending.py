"""
MailSort v45a – Waiting for KYC (orders, unclaimed letters, search ALL batches, Companies House watch).

Why: customers get our address at ORDER time (they need it to register their new company), but a
company only goes into the client database after KYC – sometimes weeks later. Letters that arrive in
between sat unmatched in old batches, and "Re-check held letters" only looked at one batch.

Self-contained add-on: app.py imports this file and calls register(app, hooks).

  /pending                      -> every order still waiting for KYC: unit, company name, letters waiting
  /pending/order/<oid>          -> one order: linked letters, suggested letters (tick + link), edit, KYC done
  /pending/kyc_done/<oid> POST  -> adds the company to the client database, attaches the linked letters,
                                   searches ALL batches for it, then shows "Send all waiting letters"
  /unclaimed                    -> every letter with no client, from ALL batches, grouped by printed name
  /pending/new_companies        -> Companies House daily check: new companies registered at our address
  /pending/search/<sid>         -> result of an all-batch search (+ Send all waiting letters)
  /pending/search_company POST  -> search all batches for one client (button on the company file)

SAFETY (unchanged rules – "correct person or nobody"):
  • Nothing is ever emailed while an order is waiting for KYC – letters are only LINKED to the order.
  • Linking is always a staff decision (suggestions only: same unit, or near-exact name ≥ 0.93).
  • A letter that arrived BEFORE the order/start date is never suggested, never linked, and an
    all-batch search that finds one holds it for staff ("arrived before start date").
  • The all-batch search uses the normal matcher (unit-first, name guard, AI verification, hold-not-guess).
Data: DATA/pending/pending.json
"""
from __future__ import annotations

import base64, csv, datetime as dt, difflib, io, json, os, re, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template_string, request

import mailsort as ms

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or ROOT / "data")
BATCHES = DATA / "batches"
OUT = DATA / "pending"
STORE = OUT / "pending.json"
CLIENTS_CSV = DATA / "clients.csv"
CH_API = "https://api.company-information.service.gov.uk"
NAME_OK = 0.93          # same bar as the mail room's hold-not-guess rule
KEEP_SEARCHES = 40

bp = Blueprint("pending", __name__)
H: dict = {}            # hooks from app.py
_lock = threading.RLock()
_bg = {"running": False, "msg": ""}


# ----------------------------------------------------------------------------- storage

def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _today() -> dt.date:
    return dt.date.today()


def load() -> dict:
    try:
        s = json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        s = {}
    s.setdefault("orders", [])
    s.setdefault("next", 1)
    s.setdefault("dismissed", {})     # "bid/lid" -> {when, by, why}
    s.setdefault("ch", {})
    s["ch"].setdefault("found", {})   # company number -> {...}
    s.setdefault("searches", [])
    return s


def save(s: dict):
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    tmp.replace(STORE)


def _staff() -> str:
    return os.environ.get("MAILSORT_USER", "staff")


def _date(v) -> dt.date | None:
    if not v:
        return None
    if isinstance(v, dt.date):
        return v
    return ms.parse_date(str(v)[:10])


def batch_date(b: dict) -> dt.date | None:
    return _date((b.get("created") or "")[:10])


def iter_batches():
    """(bid, batch) for every saved batch, oldest first."""
    if not BATCHES.exists():
        return []
    out = []
    for bdir in BATCHES.iterdir():
        if bdir.is_dir() and (bdir / "batch.json").exists():
            try:
                b = H["load_batch"](bdir.name)
            except Exception:
                b = None
            if b:
                out.append((bdir.name, b))
    out.sort(key=lambda x: x[1].get("created") or "")
    return out


def name_score(a: str, b: str) -> float:
    na, nb = ms.normalise_name(a or ""), ms.normalise_name(b or "")
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    s = difflib.SequenceMatcher(None, na, nb).ratio()
    if ms._token_subset(na, nb) and ms.names_agree(a, b):
        s = max(s, 0.95)
    return s


def _raw_clients() -> list[dict]:
    """clients.csv exactly as stored (no automatic 'overdue' status applied) – used when writing it back."""
    if not CLIENTS_CSV.exists():
        return []
    with open(CLIENTS_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def clients_by_key() -> dict:
    return {ms.normalise_name(c.get("company_name", "")): c for c in H["read_clients"]() if c.get("company_name")}


# ----------------------------------------------------------------------------- unclaimed pool

def _held(L: dict) -> bool:
    """A letter the matcher could re-check: not emailed, not staff-decided, unmatched or unverified."""
    return (not L.get("emailed_at") and L.get("matched_by") != "staff"
            and (not L.get("client") or L.get("match_state") == "review"))


def pool(s: dict | None = None, include_dismissed: bool = False) -> list[dict]:
    """Every letter with NO client, from every batch, not yet emailed."""
    s = s or load()
    linked = {k: o["id"] for o in s["orders"] if o.get("status") != "done" for k in o.get("links", [])}
    out = []
    for bid, b in iter_batches():
        bd = batch_date(b)
        for L in b.get("letters", []):
            if L.get("client") or L.get("emailed_at"):
                continue
            key = f"{bid}/{L['letter_id']}"
            dis = s["dismissed"].get(key)
            if dis and not include_dismissed:
                continue
            out.append({"key": key, "bid": bid, "lid": L["letter_id"], "date": bd.isoformat() if bd else "",
                        "recipient": (L.get("recipient_company") or "").strip(),
                        "norm": ms.normalise_name(L.get("recipient_company") or ""),
                        "unit": ms.normalise_unit(L.get("unit") or ""), "sender": L.get("sender", ""),
                        "type": L.get("letter_type", ""), "summary": L.get("summary", ""),
                        "doc_kind": L.get("doc_kind", ""), "file": L.get("file", ""),
                        "address": L.get("address", ""), "suggested": L.get("suggested_client", ""),
                        "urgency": L.get("urgency", ""), "linked": linked.get(key, ""), "dismissed": dis})
    return out


def group_pool(entries: list[dict]) -> list[dict]:
    groups: dict = {}
    for e in entries:
        k = e["norm"] or "~"
        g = groups.setdefault(k, {"norm": k, "names": [], "letters": [], "units": set()})
        g["letters"].append(e)
        if e["recipient"]:
            g["names"].append(e["recipient"])
        if e["unit"]:
            g["units"].add(e["unit"])
    out = []
    for g in groups.values():
        names = g["names"]
        g["name"] = max(set(names), key=names.count) if names else "(no company name readable)"
        g["units"] = sorted(g["units"])
        g["first"] = min(e["date"] for e in g["letters"])
        g["last"] = max(e["date"] for e in g["letters"])
        g["n"] = len(g["letters"])
        g["kinds"] = sorted({e["doc_kind"] for e in g["letters"] if e["doc_kind"]})
        g["linked"] = sorted({e["linked"] for e in g["letters"] if e["linked"]})
        out.append(g)
    out.sort(key=lambda g: g["last"], reverse=True)
    return out


def order_suggestions(o: dict, entries: list[dict]) -> list[dict]:
    """Pool letters that PROBABLY belong to this order. Only suggestions – staff link them.
    Never letters that arrived before the order date."""
    od = _date(o.get("order_date"))
    unit = ms.normalise_unit(o.get("unit") or "")
    name = o.get("company_name") or ""
    links = set(o.get("links", []))
    out = []
    for e in entries:
        if e["key"] in links or e["linked"]:
            continue
        ed = _date(e["date"])
        if od and ed and ed < od:
            continue
        why = ""
        if unit and e["unit"] == unit and (not name or not e["recipient"] or ms.names_agree(e["recipient"], name)):
            why = f"same unit {unit}"
        elif name and e["recipient"]:
            sc = name_score(e["recipient"], name)
            if sc >= NAME_OK:
                why = "same name" if sc >= 0.999 else f"name {int(sc * 100)}% similar"
        if why:
            out.append({**e, "why": why})
    return out


# ----------------------------------------------------------------------------- search ALL batches

def _could_be(L: dict, targets: list[dict], units: set) -> bool:
    u = ms.normalise_unit(L.get("unit") or "")
    if u and u in units:
        return True
    nm = L.get("recipient_company") or ""
    for c in targets:
        if name_score(nm, c.get("company_name", "")) >= 0.80:
            return True
        if L.get("suggested_client") and ms.normalise_name(L["suggested_client"]) == c.get("_norm"):
            return True
    return False


def search_all(names: list[str] | None, reason: str) -> dict:
    """Re-check held letters in EVERY batch against the current client database.
    names = only letters that could belong to these companies (fast, used after adding a client);
    None = every held letter (full re-check). Returns the saved search record."""
    with _lock:
        cfg = H["load_config"]()
        clients = ms.load_clients(CLIENTS_CSV) if CLIENTS_CSV.exists() else []
        wanted = {ms.normalise_name(n) for n in (names or []) if n}
        targets = [c for c in clients if c["_norm"] in wanted] if names is not None else clients
        units = {c["_unit"] for c in targets if c.get("_unit")}
        now = _now()
        res, checked, nb = [], 0, 0
        for bid, b in iter_batches():
            held = [L for L in b.get("letters", []) if _held(L)]
            if names is not None:
                held = [L for L in held if _could_be(L, targets, units)]
            if not held or not clients:
                continue
            nb += 1
            checked += len(held)
            before = {L["letter_id"]: ((L.get("client") or {}).get("company_name"), L.get("match_state")) for L in held}
            ms.match_letters(held, clients, use_ai=bool(cfg.get("anthropic_api_key")),
                             api_key=cfg.get("anthropic_api_key") or None, model=cfg.get("model") or None)
            bd = batch_date(b)
            for L in held:
                c = L.get("client")
                if c:
                    L["client"] = c = {k: c.get(k, "") for k in H["REQUIRED_COLS"]}
                    sd = _date(c.get("start_date"))
                    if L.get("match_state") == "verified" and sd and bd and bd < sd:
                        # arrived before this customer's service started – could be a previous occupant's post
                        L["match_state"] = "review"
                        L["match_note"] = (f"letter arrived {bd.strftime('%d/%m/%Y')}, BEFORE {c['company_name']}'s "
                                           f"start date {sd.strftime('%d/%m/%Y')} – HELD, staff must confirm")
                        L["needs_review"] = "ARRIVED BEFORE START DATE – staff must confirm; " + (L.get("needs_review") or "")
                state = ("verified" if c and L.get("match_state") == "verified" and not L.get("unit_mismatch")
                         else "review" if c or L.get("suggested_client") else "none")
                if state == "verified":
                    L["rematched_at"] = now
                    L["found_by_search"] = now
                old = before.get(L["letter_id"])
                changed = old != ((c or {}).get("company_name"), L.get("match_state"))
                if state != "none" and (changed or names is not None):
                    res.append({"bid": bid, "lid": L["letter_id"], "date": bd.isoformat() if bd else "",
                                "recipient": L.get("recipient_company", ""), "sender": L.get("sender", ""),
                                "company": (c or {}).get("company_name") or L.get("suggested_client", ""),
                                "state": state, "note": L.get("match_note", ""), "file": L.get("file", ""),
                                "doc_kind": L.get("doc_kind", "")})
            H["recompute_email_flags"](b)
            H["save_batch"](bid, b)
        s = load()
        rec = {"id": dt.datetime.now().strftime("%Y%m%d%H%M%S%f")[:17], "when": now, "reason": reason,
               "names": sorted(names) if names is not None else None, "checked": checked, "batches": nb,
               "results": res}
        s["searches"] = [rec] + s["searches"][:KEEP_SEARCHES - 1]
        save(s)
        return rec


def search_in_background(names: list[str] | None, reason: str):
    if _bg["running"]:
        return False

    def run():
        _bg["running"] = True
        _bg["msg"] = reason
        try:
            search_all(names, reason)
        except Exception as ex:
            print(f"[pending] background search failed: {ex}", flush=True)
            s = load()
            s["searches"] = [{"id": dt.datetime.now().strftime("%Y%m%d%H%M%S%f")[:17], "when": _now(),
                              "reason": reason, "names": names, "error": str(ex), "checked": 0, "batches": 0,
                              "results": []}] + s["searches"][:KEEP_SEARCHES - 1]
            save(s)
        finally:
            _bg["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def waiting_letters(company: str) -> list[tuple[str, dict]]:
    """Letters of this company found by a search or linked from an order, verified and not yet emailed."""
    k = ms.normalise_name(company)
    out = []
    for bid, b in iter_batches():
        sent_bundle = any(ms.normalise_name(e.get("company", "")) == k and (e.get("sent_at") or e.get("manual_sent_at"))
                          for e in b.get("emails", []))
        for L in b.get("letters", []):
            c = L.get("client") or {}
            if (c and ms.normalise_name(c.get("company_name", "")) == k and L.get("rematched_at")
                    and L.get("match_state") == "verified" and not L.get("unit_mismatch")
                    and not L.get("emailed_at") and not sent_bundle):
                out.append((bid, L))
    return out


def send_waiting(company: str) -> tuple[int, list[str], list[str]]:
    cfg = H["load_config"]()
    ok, skipped, failed = 0, [], []
    touched = {}
    for bid, L in waiting_letters(company):
        b = touched.get(bid) or H["load_batch"](bid)
        touched[bid] = b
        L = next(x for x in b["letters"] if x["letter_id"] == L["letter_id"])
        c = L["client"]
        if not c.get("email"):
            skipped.append(f"{L['letter_id']} (no email on file)"); continue
        if (c.get("status") or "").lower() not in ms.STATUS_OK:
            skipped.append(f"{L['letter_id']} (status {c.get('status') or 'blank'})"); continue
        if (c.get("kyc") or "").lower() == "no":
            skipped.append(f"{L['letter_id']} (KYC not done)"); continue
        if not L.get("in_package", True):
            skipped.append(f"{L['letter_id']} (not in {c.get('package') or 'their'} package)"); continue
        try:
            H["send_letter_email"](bid, L, cfg); ok += 1
        except Exception as ex:
            failed.append(f"{L['letter_id']}: {ex}")
    for bid, b in touched.items():
        H["save_batch"](bid, b)
    return ok, skipped, failed


# ----------------------------------------------------------------------------- orders

ORDER_FIELDS = ["order_ref", "order_date", "contact_name", "email", "unit", "company_name", "company_number",
                "package", "reseller", "reseller_email", "services", "note"]


def get_order(s: dict, oid: str) -> dict:
    o = next((x for x in s["orders"] if x["id"] == oid), None)
    if not o:
        abort(404)
    return o


def new_order(s: dict, data: dict) -> dict:
    o = {k: (data.get(k) or "").strip() for k in ORDER_FIELDS}
    d = _date(o["order_date"]) or _today()
    o["order_date"] = d.isoformat()
    o["unit"] = ms.normalise_unit(o["unit"]) if o["unit"] else ""
    o["id"] = f"P{s['next']:04d}"
    s["next"] += 1
    o.update({"created": _now(), "status": "open", "links": [], "name_source": "staff" if o["company_name"] else ""})
    s["orders"].append(o)
    return o


def unit_warning(unit: str, except_name: str = "") -> str:
    u = ms.normalise_unit(unit or "")
    if not u:
        return ""
    for c in H["read_clients"]():
        if ms.normalise_unit(c.get("siu") or "") == u and ms.normalise_name(c.get("company_name", "")) != ms.normalise_name(except_name):
            return f"Unit {u} is still recorded for {c['company_name']} in the client database."
    return ""


def link_letters(s: dict, o: dict, keys: list[str]) -> tuple[int, list[str]]:
    od = _date(o.get("order_date"))
    by_key = {e["key"]: e for e in pool(s)}
    other = {k: x["id"] for x in s["orders"] if x is not o and x.get("status") != "done" for k in x.get("links", [])}
    n, refused = 0, []
    for k in keys:
        e = by_key.get(k)
        if not e:
            refused.append(f"{k} (no longer unclaimed)"); continue
        if k in other:
            refused.append(f"{e['lid']} (already linked to {other[k]})"); continue
        ed = _date(e["date"])
        if od and ed and ed < od:
            refused.append(f"{e['lid']} (arrived {ed.strftime('%d/%m/%Y')}, before the order date)"); continue
        if k not in o["links"]:
            o["links"].append(k); n += 1
    return n, refused


def complete_kyc(oid: str, form: dict) -> tuple[dict | None, str]:
    """KYC finished: add/update the client row, attach linked letters, search all batches."""
    with _lock:
        s = load()
        o = get_order(s, oid)
        name = (form.get("company_name") or o.get("company_name") or "").strip()
        email = (form.get("email") or o.get("email") or "").strip()
        if not name:
            return None, "Enter the company name first – the client database needs it to match letters."
        if not email:
            return None, "Enter the customer's email first."
        rows = _raw_clients()
        key = ms.normalise_name(name)
        warn = unit_warning(o.get("unit"), name)
        if warn:
            return None, warn + " Fix the client database (or the order's unit) first – two companies can't share a unit."
        row = next((r for r in rows if ms.normalise_name(r.get("company_name", "")) == key), None)
        if row is None:
            row = {k: "" for k in H["REQUIRED_COLS"]}
            rows.append(row)
        fill = {"client_id": o.get("order_ref") or o["id"], "company_name": name, "contact_name": o.get("contact_name", ""),
                "email": email, "package": o.get("package", ""), "start_date": o.get("order_date", ""),
                "reseller": o.get("reseller", ""), "reseller_email": o.get("reseller_email", ""),
                "siu": o.get("unit", ""), "services": o.get("services", "")}
        for k, v in fill.items():
            if v and not (row.get(k) or "").strip():
                row[k] = v
        row["kyc"] = "yes"
        if (row.get("status") or "").lower() in ("", "pending", "pending_kyc"):
            row["status"] = "active"
        H["write_clients"](rows)
        client = {k: row.get(k, "") for k in H["REQUIRED_COLS"]}
        # attach the letters staff linked while the order was waiting
        now, attached = _now(), 0
        by_bid: dict = {}
        for k in o.get("links", []):
            bid, lid = k.split("/", 1)
            by_bid.setdefault(bid, []).append(lid)
        for bid, lids in by_bid.items():
            b = H["load_batch"](bid)
            if not b:
                continue
            for L in b["letters"]:
                if L["letter_id"] in lids and not L.get("emailed_at") and (not L.get("client") or L.get("match_state") == "review"):
                    L["client"] = dict(client)
                    L.update({"match_state": "verified", "matched_by": "staff", "match_score": 1.0, "unit_mismatch": False,
                              "match_note": f"linked by staff while waiting for KYC (order {o['id']})",
                              "rematched_at": now, "linked_at": now})
                    L["in_package"] = ms.letter_in_package(L, client)
                    L["needs_review"] = "; ".join(x for x in (L.get("needs_review") or "").split("; ")
                                                  if x and not any(t in x for t in ("MATCH NOT VERIFIED", "UNIT MISMATCH",
                                                                                     "no client match", "fuzzy match", "HELD")))
                    attached += 1
            H["recompute_email_flags"](b)
            H["save_batch"](bid, b)
        o.update({"company_name": name, "email": email, "status": "done", "done_at": now, "attached": attached})
        save(s)
    rec = search_all([name], f"KYC done – {name} (order {o['id']}) added to the client database")
    rec["attached"] = attached
    s = load()
    for x in s["searches"]:
        if x["id"] == rec["id"]:
            x["attached"] = attached
            x["company"] = name
    save(s)
    return rec, ""


def parse_orders_csv(raw: bytes) -> list[dict]:
    alias = {"order_id": "order_ref", "order": "order_ref", "order_number": "order_ref", "ref": "order_ref",
             "reference": "order_ref", "id": "order_ref", "client_id": "order_ref",
             "date": "order_date", "ordered": "order_date", "created": "order_date", "created_at": "order_date",
             "start_date": "order_date", "purchase_date": "order_date", "order_created": "order_date",
             "contact": "contact_name", "customer": "contact_name", "customer_name": "contact_name",
             "full_name": "contact_name", "name": "contact_name",
             "email_address": "email", "e-mail": "email", "customer_email": "email",
             "siu": "unit", "unit_number": "unit", "siu_number": "unit", "office": "unit", "suite": "unit",
             "company": "company_name", "business_name": "company_name", "plan": "package", "tier": "package",
             "partner": "reseller", "agent": "reseller", "partner_email": "reseller_email",
             "service": "services", "product": "services", "products": "services", "company_no": "company_number",
             "crn": "company_number"}
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))
    out = []
    for r in rows:
        n = {}
        for k, v in r.items():
            key = (k or "").strip().lower().replace(" ", "_")
            key = alias.get(key, key)
            if (v or "").strip() or key not in n:
                n[key] = (v or "").strip()
        if n.get("email") or n.get("company_name"):
            out.append(n)
    a = b = False
    for r in out:
        m = re.match(r"\s*(\d{1,2})[/\-.](\d{1,2})[/\-.]\d{2,4}", r.get("order_date") or "")
        if m:
            a |= int(m.group(1)) > 12
            b |= int(m.group(2)) > 12
    prefer = "mdy" if (b and not a) else "dmy"
    for r in out:
        d = ms.parse_date(r.get("order_date", ""), prefer=prefer)
        r["order_date"] = d.isoformat() if d else ""
    return out


# ----------------------------------------------------------------------------- Companies House watch

def ch_settings() -> dict:
    return {"location": os.environ.get("PENDING_CH_LOCATION", "E1 5NF").strip(),
            "match": [w.strip().lower() for w in os.environ.get("PENDING_CH_MATCH", "greatorex").split(",") if w.strip()],
            "key": bool(os.environ.get("CH_API_KEY", "").strip())}


def _ch_get(path: str):
    key = os.environ.get("CH_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CH_API_KEY is not set in Railway Variables.")
    req = urllib.request.Request(CH_API + path, headers={
        "Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode(),
        "User-Agent": "StartitupMailRoom/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return None
        if ex.code == 401:
            raise RuntimeError("Companies House rejected the API key (401) – check CH_API_KEY.")
        if ex.code == 429:
            raise RuntimeError("Companies House rate limit reached – try again in a few minutes.")
        raise RuntimeError(f"Companies House error {ex.code}")
    except urllib.error.URLError as ex:
        raise RuntimeError(f"Could not reach Companies House ({ex.reason})")


def _addr(a: dict) -> str:
    keys = ("premises", "address_line_1", "address_line_2", "locality", "region", "postal_code", "country")
    return ", ".join(str(a.get(k)).strip() for k in keys if a.get(k))


def ch_check(days_back: int | None = None) -> tuple[int, str]:
    """Find companies incorporated recently with their registered office at our address."""
    cfg = ch_settings()
    with _lock:
        s = load()
        ch = s["ch"]
        if days_back is None:
            last = _date(ch.get("last_ok"))
            frm = (last - dt.timedelta(days=3)) if last else (_today() - dt.timedelta(days=30))
        else:
            frm = _today() - dt.timedelta(days=days_back)
        ch["last_run"] = _now()
        save(s)
    new, start, items_all = 0, 0, []
    try:
        while True:
            q = urllib.parse.urlencode({"location": cfg["location"], "incorporated_from": frm.isoformat(),
                                        "incorporated_to": _today().isoformat(), "size": 500, "start_index": start})
            d = _ch_get("/advanced-search/companies?" + q) or {}
            items = d.get("items") or []
            items_all.extend(items)
            start += len(items)
            if not items or start >= int(d.get("hits") or 0) or start >= 5000:
                break
    except Exception as ex:
        with _lock:
            s = load(); s["ch"]["error"] = str(ex); save(s)
        return 0, str(ex)
    with _lock:
        s = load()
        ch = s["ch"]
        ckeys = clients_by_key()
        for it in items_all:
            num = (it.get("company_number") or "").strip()
            addr = _addr(it.get("registered_office_address") or {})
            if not num or num in ch["found"]:
                continue
            if cfg["match"] and not any(w in addr.lower() for w in cfg["match"]):
                continue
            name = (it.get("company_name") or "").strip()
            st = "client" if ms.normalise_name(name) in ckeys else "new"
            ch["found"][num] = {"number": num, "name": name, "created": it.get("date_of_creation", ""),
                                "address": addr, "unit": ms.extract_unit(addr) or "", "status": st,
                                "seen": _now(), "ch_status": it.get("company_status", "")}
            new += st == "new"
        ch["last_ok"] = _today().isoformat()
        ch["error"] = ""
        save(s)
    return new, ""


def ch_suggest(f: dict, s: dict) -> dict | None:
    """Best open order for a newly found company: same name, or same unit with no name yet."""
    opn = [o for o in s["orders"] if o.get("status") != "done"]
    for o in opn:
        if o.get("company_name") and name_score(o["company_name"], f["name"]) >= NAME_OK:
            return o
    u = ms.normalise_unit(f.get("unit") or "")
    if u:
        for o in opn:
            if not o.get("company_name") and ms.normalise_unit(o.get("unit") or "") == u:
                return o
    return None


def _poll_loop():
    time.sleep(90)
    while True:
        try:
            s = load()
            if ch_settings()["key"] and (s["ch"].get("last_run") or "")[:10] != _today().isoformat() \
                    and dt.datetime.now().hour >= 6:
                n, err = ch_check()
                print(f"[pending] Companies House check: {n} new" + (f" – {err}" if err else ""), flush=True)
        except Exception as ex:
            print(f"[pending] CH loop error: {ex}", flush=True)
        time.sleep(1800)


def alert_count() -> int:
    try:
        s = load()
        return sum(1 for f in s["ch"]["found"].values() if f.get("status") == "new")
    except Exception:
        return 0


# ----------------------------------------------------------------------------- routes

def _order_view(o: dict, entries: list[dict]) -> dict:
    links = set(o.get("links", []))
    linked = [e for e in entries if e["key"] in links]
    sugg = order_suggestions(o, entries) if o.get("status") != "done" else []
    od = _date(o.get("order_date"))
    return {"o": o, "linked": linked, "sugg": sugg, "days": (_today() - od).days if od else None,
            "kinds": sorted({e["doc_kind"] for e in linked + sugg if e["doc_kind"]})}


@bp.get("/pending")
def pending_home():
    s = load()
    entries = pool(s)
    show = request.args.get("show", "open")
    orders = [o for o in s["orders"] if (o.get("status") == "done") == (show == "done")]
    rows = [_order_view(o, entries) for o in orders]
    rows.sort(key=lambda r: (-(len(r["linked"]) + len(r["sugg"])), r["o"].get("order_date") or ""))
    ckeys = clients_by_key()
    for r in rows:
        r["in_db"] = bool(r["o"].get("company_name")) and ms.normalise_name(r["o"]["company_name"]) in ckeys
    newco = sum(1 for f in s["ch"]["found"].values() if f.get("status") == "new")
    return render_template_string(HOME, rows=rows, show=show, n_open=sum(1 for o in s["orders"] if o.get("status") != "done"),
                                  n_pool=len(entries), newco=newco, bg=_bg, searches=s["searches"][:8],
                                  today=_today().isoformat(), ch=s["ch"], chs=ch_settings())


@bp.post("/pending/new")
def pending_new():
    with _lock:
        s = load()
        o = new_order(s, request.form)
        save(s)
    w = unit_warning(o["unit"])
    flash(f"Order {o['id']} added." + (f" ⚠ {w} Letters for that unit will keep matching the old client until "
                                      f"the client database is fixed." if w else ""))
    return redirect(f"/pending/order/{o['id']}")


@bp.post("/pending/upload")
def pending_upload():
    f = request.files.get("csv")
    try:
        rows = parse_orders_csv(f.read())
    except Exception as ex:
        flash(f"Could not read the file: {ex}")
        return redirect("/pending")
    ckeys = clients_by_key()
    added = skipped = 0
    with _lock:
        s = load()
        have = {((o.get("order_ref") or "").lower(), (o.get("email") or "").lower()) for o in s["orders"]}
        for r in rows:
            if r.get("company_name") and ms.normalise_name(r["company_name"]) in ckeys:
                skipped += 1; continue
            k = ((r.get("order_ref") or "").lower(), (r.get("email") or "").lower())
            if k in have:
                skipped += 1; continue
            new_order(s, r); have.add(k); added += 1
        save(s)
    flash(f"{added} order(s) added; {skipped} skipped (already on the list or already in the client database).")
    return redirect("/pending")


@bp.get("/pending/order/<oid>")
def pending_order(oid):
    s = load()
    o = get_order(s, oid)
    v = _order_view(o, pool(s))
    return render_template_string(ORDER, v=v, o=o, warn=unit_warning(o.get("unit"), o.get("company_name", "")),
                                  in_db=bool(o.get("company_name")) and ms.normalise_name(o["company_name"]) in clients_by_key())


@bp.post("/pending/order/<oid>/edit")
def pending_edit(oid):
    with _lock:
        s = load()
        o = get_order(s, oid)
        for k in ORDER_FIELDS:
            if k in request.form:
                o[k] = request.form.get(k, "").strip()
        o["order_date"] = (_date(o["order_date"]) or _today()).isoformat()
        o["unit"] = ms.normalise_unit(o["unit"]) if o.get("unit") else ""
        if "company_name" in request.form and o["company_name"]:
            o["name_source"] = o.get("name_source") or "staff"
        save(s)
    flash("Order saved.")
    return redirect(f"/pending/order/{oid}")


@bp.post("/pending/order/<oid>/link")
def pending_link(oid):
    with _lock:
        s = load()
        o = get_order(s, oid)
        n, refused = link_letters(s, o, request.form.getlist("key"))
        if n and not o.get("company_name") and request.form.get("take_name"):
            o["company_name"] = request.form["take_name"].strip()
            o["name_source"] = "letter"
        save(s)
    flash(f"{n} letter(s) linked to order {oid} – they stay HELD until KYC is done."
          + (" Not linked: " + "; ".join(refused) if refused else ""))
    return redirect(request.form.get("back") or f"/pending/order/{oid}")


@bp.post("/pending/order/<oid>/unlink")
def pending_unlink(oid):
    with _lock:
        s = load()
        o = get_order(s, oid)
        k = request.form.get("key", "")
        if k in o["links"]:
            o["links"].remove(k)
        save(s)
    flash("Letter unlinked – it is back in Unclaimed letters.")
    return redirect(f"/pending/order/{oid}")


@bp.post("/pending/order/<oid>/remove")
def pending_remove(oid):
    if request.form.get("pw", "") != H.get("delete_password", "askusama"):
        flash("Wrong delete password – order NOT removed.")
        return redirect(f"/pending/order/{oid}")
    with _lock:
        s = load()
        o = get_order(s, oid)
        s["orders"].remove(o)
        save(s)
    flash(f"Order {oid} removed. Its linked letters are back in Unclaimed letters.")
    return redirect("/pending")


@bp.post("/pending/kyc_done/<oid>")
def pending_kyc_done(oid):
    rec, err = complete_kyc(oid, request.form)
    if err:
        flash(err)
        return redirect(f"/pending/order/{oid}")
    return redirect(f"/pending/search/{rec['id']}")


@bp.get("/pending/search/<sid>")
def pending_search_view(sid):
    s = load()
    rec = next((x for x in s["searches"] if x["id"] == sid), None) or abort(404)
    comps = {}
    for r in rec["results"]:
        if r["state"] == "verified":
            comps.setdefault(r["company"], 0)
    if rec.get("company"):
        comps.setdefault(rec["company"], 0)
    ready = {c: len(waiting_letters(c)) for c in comps}
    return render_template_string(SEARCH, rec=rec, ready=ready)


@bp.post("/pending/search_company")
def pending_search_company():
    name = (request.form.get("name") or "").strip()
    if not name or ms.normalise_name(name) not in clients_by_key():
        flash("That company is not in the client database – add it first (or use Waiting for KYC).")
        return redirect(request.referrer or "/pending")
    rec = search_all([name], f"Search all batches for {name}")
    rec_s = load()
    for x in rec_s["searches"]:
        if x["id"] == rec["id"]:
            x["company"] = name
    save(rec_s)
    return redirect(f"/pending/search/{rec['id']}")


@bp.post("/pending/search_everything")
def pending_search_everything():
    if search_in_background(None, "Full re-check of every held letter in every batch"):
        flash("Searching every batch in the background – refresh this page in a minute to see the result.")
    else:
        flash("A search is already running – wait for it to finish.")
    return redirect("/pending")


@bp.post("/pending/send_waiting")
def pending_send_waiting():
    company = (request.form.get("company") or "").strip()
    ok, skipped, failed = send_waiting(company)
    msg = f"{company}: sent {ok} waiting letter(s)."
    if skipped: msg += " Skipped: " + "; ".join(skipped) + "."
    if failed: msg += " Failed: " + "; ".join(failed) + "."
    flash(msg)
    return redirect(request.form.get("back") or "/pending")


@bp.get("/unclaimed")
def unclaimed():
    s = load()
    show_dis = request.args.get("show") == "dismissed"
    entries = pool(s, include_dismissed=show_dis)
    if show_dis:
        entries = [e for e in entries if e["dismissed"]]
    groups = group_pool(entries)
    opn = [o for o in s["orders"] if o.get("status") != "done"]
    for g in groups:
        g["sugg"] = []
        def after(o, e):
            od = _date(o.get("order_date"))
            return not od or (_date(e["date"]) or _today()) >= od
        for o in opn:
            why = ""
            if o.get("company_name") and g["norm"] != "~" and name_score(g["name"], o["company_name"]) >= NAME_OK:
                why = "same name"
            elif o.get("unit") and ms.normalise_unit(o["unit"]) in g["units"]:
                why = f"same unit {o['unit']}"
            n_after = sum(1 for e in g["letters"] if after(o, e))
            if why and n_after:   # letters that all arrived before the order date are never suggested
                g["sugg"].append({"o": o, "why": why, "date_ok": n_after == len(g["letters"])})
    return render_template_string(POOL, groups=groups, orders=opn, show_dis=show_dis, n=len(entries), bg=_bg)


@bp.post("/unclaimed/dismiss")
def unclaimed_dismiss():
    keys = request.form.getlist("key")
    undo = request.form.get("undo") == "1"
    with _lock:
        s = load()
        for k in keys:
            if undo:
                s["dismissed"].pop(k, None)
            else:
                s["dismissed"][k] = {"when": _now(), "by": _staff(), "why": request.form.get("why", "not our customer")}
        save(s)
    flash(f"{len(keys)} letter(s) " + ("restored to the list." if undo else "marked as not our customer (hidden)."))
    return redirect("/unclaimed" + ("?show=dismissed" if undo else ""))


@bp.post("/unclaimed/new_order")
def unclaimed_new_order():
    """Unclaimed letters for a name nobody has entered as an order yet – start an order from them."""
    with _lock:
        s = load()
        o = new_order(s, {"company_name": request.form.get("name", ""), "unit": request.form.get("unit", ""),
                          "order_date": request.form.get("order_date", "")})
        o["name_source"] = "letter"
        save(s)
    flash(f"Order {o['id']} started for {o['company_name']} – add the customer's email and order date, then link the letters.")
    return redirect(f"/pending/order/{o['id']}")


@bp.get("/pending/new_companies")
def new_companies():
    s = load()
    show = request.args.get("show", "new")
    fs = sorted(s["ch"]["found"].values(), key=lambda f: f.get("created") or "", reverse=True)
    fs = [f for f in fs if (f.get("status") == "new") == (show == "new")]
    entries = pool(s)
    for f in fs:
        f["_sugg"] = ch_suggest(f, s) if f.get("status") == "new" else None
        f["_letters"] = sum(1 for e in entries if e["recipient"] and name_score(e["recipient"], f["name"]) >= NAME_OK)
    opn = [o for o in s["orders"] if o.get("status") != "done"]
    return render_template_string(NEWCO, fs=fs, show=show, orders=opn, ch=s["ch"], chs=ch_settings())


@bp.post("/pending/new_companies/check")
def new_companies_check():
    days = request.form.get("days")
    n, err = ch_check(int(days) if days and days.isdigit() else None)
    flash(f"Companies House check failed: {err}" if err else f"Companies House checked – {n} new company(ies) at our address.")
    return redirect("/pending/new_companies")


@bp.post("/pending/new_companies/act")
def new_companies_act():
    num = request.form.get("number", "")
    act = request.form.get("act", "")
    with _lock:
        s = load()
        f = s["ch"]["found"].get(num) or abort(404)
        if act == "ignore":
            f["status"] = "ignored"
            msg = f"{f['name']} ignored."
        elif act == "restore":
            f["status"] = "new"
            msg = f"{f['name']} back on the list."
        elif act == "new_order":
            o = new_order(s, {"company_name": f["name"], "company_number": num, "unit": f.get("unit", ""),
                              "order_date": f.get("created", "")})
            o["name_source"] = "Companies House"
            f.update({"status": "linked", "order": o["id"]})
            save(s)
            flash(f"Order {o['id']} created for {f['name']} – add the customer's email.")
            return redirect(f"/pending/order/{o['id']}")
        else:
            o = get_order(s, request.form.get("order", ""))
            if o.get("company_name") and name_score(o["company_name"], f["name"]) < NAME_OK \
                    and request.form.get("confirm") != "1":
                save(s)
                flash(f"Order {o['id']} already has the name '{o['company_name']}' – not changed. "
                      f"Edit the order if the customer changed the name.")
                return redirect("/pending/new_companies")
            o["company_name"] = f["name"]
            o["company_number"] = num
            o["name_source"] = "Companies House"
            f.update({"status": "linked", "order": o["id"]})
            msg = f"{f['name']} linked to order {o['id']}."
        save(s)
    flash(msg)
    return redirect("/pending/new_companies")


def register(app, **hooks):
    OUT.mkdir(parents=True, exist_ok=True)
    H.update(hooks)
    app.register_blueprint(bp)

    @app.context_processor
    def _pending_badge():
        return {"pending_alerts": alert_count(), "pkind": KIND}

    if os.environ.get("PENDING_NO_POLL") != "1":
        threading.Thread(target=_poll_loop, daemon=True).start()


# ----------------------------------------------------------------------------- pages

STYLE = """<style>
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 16px}.tabs a{padding:8px 14px;border-radius:8px;background:#f1f5f9;color:#111;text-decoration:none;font-weight:600;font-size:14px}
.tabs a.on{background:#111;color:#fff}.tabs a .n{background:#dc2626;color:#fff;border-radius:9px;padding:0 7px;font-size:11px;margin-left:4px}
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:2px 14px}
.okb{background:#e8f7ee;border:1px solid #86efac;padding:10px 14px;border-radius:8px;margin:10px 0}
.warnb{background:#fef9c3;border:1px solid #fde047;padding:10px 14px;border-radius:8px;margin:10px 0}
.errb{background:#fee2e2;border:1px solid #fca5a5;padding:10px 14px;border-radius:8px;margin:10px 0}
.dk{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:600;background:#e0f2fe;color:#075985;white-space:nowrap}
.inl{display:inline}.inl select,.inl input{padding:5px 6px;font-size:13px;border:1px solid #cbd5e1;border-radius:6px;width:auto}
select{padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px}
</style>"""

KIND = {"ch_auth_code": "🔑 CH auth code", "hmrc_utr": "🧾 UTR letter", "hmrc_activation": "🔓 activation code"}

TABS = """<div class="tabs"><a href="/pending" class="{{ 'on' if tab=='orders' }}">Waiting for KYC</a>
<a href="/unclaimed" class="{{ 'on' if tab=='pool' }}">Unclaimed letters (all batches)</a>
<a href="/pending/new_companies" class="{{ 'on' if tab=='ch' }}">New companies at our address{% if pending_alerts %}<span class="n">{{pending_alerts}}</span>{% endif %}</a></div>"""

HOME = """{% extends "base" %}{% block body %}""" + STYLE + """{% set tab='orders' %}""" + TABS + """
<div class="card"><h1>Waiting for KYC</h1>
<p class="muted">Customers who have their address but are not in the client database yet. Their letters are kept here,
<b>held</b> – nothing is emailed until KYC is done. When KYC is complete press <b>KYC done</b>: the company goes into the
client database, its letters from <b>every</b> batch are attached, and you can send them all in one click.</p>
<div class="statgrid"><div class="stat"><b>{{n_open}}</b>orders waiting</div><div class="stat"><b>{{n_pool}}</b>unclaimed letters</div>
<div class="stat"><b>{{newco}}</b>new companies to link</div></div>
{% if bg.running %}<div class="warnb">⏳ Searching all batches in the background: {{bg.msg}} – refresh in a minute.</div>{% endif %}
<div class="btnrow" style="margin:6px 0 14px"><a class="btn small secondary {{ '' if show=='done' else 'on' }}" href="/pending">Open orders</a>
<a class="btn small secondary" href="/pending?show=done">KYC done (history)</a></div>
<table><tr><th>Order</th><th>Customer</th><th>Unit</th><th>Company</th><th>Letters</th><th></th></tr>
{% for r in rows %}{% set o = r.o %}<tr {% if r.linked or r.sugg %}class="review"{% endif %}>
<td><a href="/pending/order/{{o.id}}" style="font-weight:700;color:#111">{{o.id}}</a>{% if o.order_ref %}<br><span class="muted">{{o.order_ref}}</span>{% endif %}
<br><span class="tiny">{{o.order_date|ukdate}}{% if r.days is not none %} · {{r.days}} days{% endif %}</span></td>
<td>{{o.contact_name or "—"}}<br><span class="muted">{{o.email or "no email yet"}}</span>{% if o.reseller %}<br><span class="pill">{{o.reseller}}</span>{% endif %}</td>
<td>{{o.unit or "SIU Office (no unit)"}}</td>
<td>{% if o.company_name %}<b>{{o.company_name}}</b>{% if o.company_number %}<br><span class="muted">{{o.company_number}}</span>{% endif %}
{% if o.name_source %}<br><span class="tiny muted">name from {{o.name_source}}</span>{% endif %}
{% if r.in_db and show != 'done' %}<br><span class="pill active">already in client database</span>{% endif %}
{% else %}<span class="pill high">awaiting company name</span>{% endif %}</td>
<td>{% if r.linked %}<span class="pill active">{{r.linked|length}} linked</span> {% endif %}
{% if r.sugg %}<span class="pill" style="background:#fef3c7;color:#92400e">{{r.sugg|length}} to check</span>{% endif %}
{% if not r.linked and not r.sugg %}<span class="muted">—</span>{% endif %}
{% for k in r.kinds %}<br><span class="dk">{{ pkind[k] }}</span>{% endfor %}
{% if show=='done' %}<span class="muted tiny">KYC done {{o.done_at|ukdate}} · {{o.attached or 0}} letter(s) attached</span>{% endif %}</td>
<td><a class="btn small" href="/pending/order/{{o.id}}">Open</a></td></tr>
{% else %}<tr><td colspan="6" class="muted">No orders here.</td></tr>{% endfor %}</table></div>

<div class="card"><h2>Add an order</h2>
<p class="muted">Add the customer when they order – even before they have a company name. Leave the unit empty for "SIU Office" customers.</p>
<form method="post" action="/pending/new"><div class="fgrid">
<div><label>Order ref</label><input type="text" name="order_ref"></div>
<div><label>Order date</label><input type="text" name="order_date" value="{{today|ukdate}}"></div>
<div><label>Customer name</label><input type="text" name="contact_name"></div>
<div><label>Customer email</label><input type="text" name="email"></div>
<div><label>Unit (if any)</label><input type="text" name="unit" placeholder="e.g. A80 – empty = SIU Office"></div>
<div><label>Company name (if known)</label><input type="text" name="company_name"></div>
<div><label>Package</label><select name="package" style="width:100%"><option></option><option>Basic</option><option>Standard</option><option>Premium</option></select></div>
<div><label>Reseller</label><input type="text" name="reseller"></div>
<div><label>Reseller email</label><input type="text" name="reseller_email"></div>
<div><label>Services</label><input type="text" name="services"></div></div>
<p><button class="btn">Add order</button></p></form>
<form method="post" action="/pending/upload" enctype="multipart/form-data" style="margin-top:10px">
<label>…or upload orders from the dashboard (CSV with email, order date, unit, customer name; company name optional)</label>
<input type="file" name="csv" accept=".csv"> <button class="btn small secondary">Upload orders</button></form></div>

<div class="card"><h2>Search all batches</h2>
<p class="muted">Runs automatically whenever a client is added or the client list is uploaded. Use this button for a full re-check of
every held letter in every batch against the current client database (letters are only matched under the normal safety rules).</p>
<form method="post" action="/pending/search_everything"><button class="btn small">🔎 Re-check every held letter in every batch</button></form>
{% if searches %}<table style="margin-top:12px"><tr><th>When</th><th>What</th><th>Checked</th><th>Found</th><th></th></tr>
{% for x in searches %}<tr><td>{{x.when|ukdate}}</td><td>{{x.reason}}{% if x.error %}<br><span class="pill high">failed: {{x.error}}</span>{% endif %}</td>
<td>{{x.checked}} letter(s) in {{x.batches}} batch(es)</td>
<td>{{ x.results|selectattr("state","equalto","verified")|list|length }} matched · {{ x.results|selectattr("state","equalto","review")|list|length }} to confirm</td>
<td><a class="btn small secondary" href="/pending/search/{{x.id}}">View</a></td></tr>{% endfor %}</table>{% endif %}</div>
{% endblock %}"""

LTABLE = """{% macro ltable(ls, check=False, unlink=None) %}<table><tr>{% if check %}<th></th>{% endif %}<th>Arrived</th><th>Printed name</th><th>Unit</th><th>From</th><th>Summary</th><th></th></tr>
{% for e in ls %}<tr>{% if check %}<td><input type="checkbox" name="key" value="{{e.key}}" checked></td>{% endif %}
<td style="white-space:nowrap">{{e.date|ukdate}}<br><a class="tiny muted" href="/batch/{{e.bid}}">{{e.bid}} · {{e.lid}}</a></td>
<td>{{e.recipient or "—"}}{% if e.why %}<br><span class="pill" style="background:#fef3c7;color:#92400e">{{e.why}}</span>{% endif %}</td>
<td>{{e.unit or "—"}}</td><td>{{e.sender}}{% if e.doc_kind %}<br><span class="dk">{{pkind[e.doc_kind]}}</span>{% endif %}</td>
<td class="tiny">{{e.summary}}</td>
<td><a class="btn small secondary" target="_blank" href="/batch/{{e.bid}}/file/{{e.file}}">View PDF</a>
{% if unlink %}<form class="inl" method="post" action="/pending/order/{{unlink}}/unlink"><input type="hidden" name="key" value="{{e.key}}"><button class="btn small secondary">Unlink</button></form>{% endif %}</td></tr>
{% endfor %}</table>{% endmacro %}"""

ORDER = """{% extends "base" %}""" + LTABLE + """{% block body %}""" + STYLE + """{% set tab='orders' %}""" + TABS + """
<div class="card"><h1>Order {{o.id}}{% if o.company_name %} – {{o.company_name}}{% endif %}</h1>
{% if o.status == 'done' %}<div class="okb">✔ KYC done {{o.done_at|ukdate}} – this company is in the client database.</div>{% endif %}
{% if warn %}<div class="errb">⚠ {{warn}} Two companies can't share a unit – fix the client database or this order before KYC done.</div>{% endif %}
<form method="post" action="/pending/order/{{o.id}}/edit"><div class="fgrid">
<div><label>Order ref</label><input type="text" name="order_ref" value="{{o.order_ref}}"></div>
<div><label>Order date</label><input type="text" name="order_date" value="{{o.order_date|ukdate}}"></div>
<div><label>Customer name</label><input type="text" name="contact_name" value="{{o.contact_name}}"></div>
<div><label>Customer email</label><input type="text" name="email" value="{{o.email}}"></div>
<div><label>Unit (empty = SIU Office)</label><input type="text" name="unit" value="{{o.unit}}"></div>
<div><label>Company name</label><input type="text" name="company_name" value="{{o.company_name}}"></div>
<div><label>Company number</label><input type="text" name="company_number" value="{{o.company_number}}"></div>
<div><label>Package</label><select name="package" style="width:100%">{% for p in ['', 'Basic', 'Standard', 'Premium'] %}<option {{ 'selected' if o.package == p }}>{{p}}</option>{% endfor %}</select></div>
<div><label>Reseller</label><input type="text" name="reseller" value="{{o.reseller}}"></div>
<div><label>Reseller email</label><input type="text" name="reseller_email" value="{{o.reseller_email}}"></div>
<div><label>Services</label><input type="text" name="services" value="{{o.services}}"></div>
<div><label>Note</label><input type="text" name="note" value="{{o.note}}"></div></div>
{% if o.name_source %}<p class="muted tiny">Company name from: {{o.name_source}}</p>{% endif %}
<p><button class="btn small secondary">Save order</button></p></form></div>

<div class="card"><h2>Letters linked to this order ({{v.linked|length}})</h2>
<p class="muted">Held – they will be attached to the company and can be sent once KYC is done.</p>
{% if v.linked %}{{ ltable(v.linked, unlink=o.id if o.status != 'done' else None) }}{% else %}<p class="muted">None yet.</p>{% endif %}</div>

{% if o.status != 'done' %}
<div class="card"><h2>Letters that may belong to this order ({{v.sugg|length}})</h2>
<p class="muted">Found in the unclaimed letters of ALL batches: same unit, or (almost) the same company name. Letters that arrived before
the order date are never shown. Open each PDF, untick any that are not this customer's, then press Link.</p>
{% if v.sugg %}<form method="post" action="/pending/order/{{o.id}}/link">
{% if not o.company_name %}<label>Use this printed name as the order's company name?</label>
<select name="take_name"><option value="">No – leave empty</option>{% for n in v.sugg|map(attribute='recipient')|reject('equalto','')|unique %}<option>{{n}}</option>{% endfor %}</select>{% endif %}
{{ ltable(v.sugg, check=True) }}<p><button class="btn small">Link ticked letters to {{o.id}}</button></p></form>
{% else %}<p class="muted">Nothing found{% if not o.company_name and not o.unit %} – this order has no unit and no company name yet, so there is nothing
to search for. Add the name (or check <a href="/pending/new_companies">New companies at our address</a>){% endif %}.</p>{% endif %}
<p class="muted tiny">Can't see a letter? Search <a href="/unclaimed">Unclaimed letters</a> – you can link any letter from there.</p></div>

<div class="card"><h2>KYC done</h2>
<p class="muted">Adds <b>{{o.company_name or "the company"}}</b> to the client database (status active, KYC yes, start date = order date, unit
{{o.unit or "none"}}), attaches the {{v.linked|length}} linked letter(s), then searches ALL batches for any other letters for it.
Nothing is emailed until you press Send on the next page.</p>
{% if in_db %}<div class="warnb">This company name is already in the client database – its row will be updated (KYC yes; empty fields filled in, nothing overwritten).</div>{% endif %}
<form method="post" action="/pending/kyc_done/{{o.id}}" onsubmit="return confirm('KYC is complete for {{o.company_name or 'this customer'}}? This adds them to the client database.')">
<div class="fgrid"><div><label>Company name</label><input type="text" name="company_name" value="{{o.company_name}}" required></div>
<div><label>Customer email</label><input type="text" name="email" value="{{o.email}}" required></div></div>
<p><button class="btn">✔ KYC done – add to client database</button></p></form></div>

<div class="card"><h2>Remove order</h2><form method="post" action="/pending/order/{{o.id}}/remove" class="btnrow" onsubmit="return confirm('Remove order {{o.id}}?')">
<input type="password" name="pw" placeholder="delete password" style="max-width:200px"><button class="btn small secondary">Remove (customer cancelled)</button></form></div>
{% endif %}{% endblock %}"""

SEARCH = """{% extends "base" %}{% block body %}""" + STYLE + """{% set tab='orders' %}""" + TABS + """
<div class="card"><h1>Search all batches</h1>
<p><b>{{rec.reason}}</b> · {{rec.when|ukdate}}</p>
{% if rec.error %}<div class="errb">Search failed: {{rec.error}}</div>{% endif %}
{% if rec.attached is defined %}<div class="okb">{{rec.attached}} letter(s) linked while waiting for KYC were attached to the company.</div>{% endif %}
<p class="muted">Checked {{rec.checked}} held letter(s) in {{rec.batches}} batch(es).
{{ rec.results|selectattr("state","equalto","verified")|list|length }} matched and verified ·
{{ rec.results|selectattr("state","equalto","review")|list|length }} need a staff check (open the batch and confirm the client).</p>
{% for c, n in ready.items() %}<div class="okb" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><b>{{c}}</b>: {{n}} letter(s) ready to send
{% if n %}<form method="post" action="/pending/send_waiting" class="inl" onsubmit="return confirm('Email {{n}} letter(s) to {{c}} now?')">
<input type="hidden" name="company" value="{{c}}"><input type="hidden" name="back" value="/pending/search/{{rec.id}}">
<button class="btn small">📧 Send all {{n}} waiting letter(s)</button></form>{% endif %}
<a class="btn small secondary" href="/company?name={{c|urlencode}}">Company file</a></div>{% endfor %}
<table><tr><th>Arrived</th><th>Letter</th><th>Printed name</th><th>From</th><th>Result</th></tr>
{% for r in rec.results %}<tr {% if r.state != 'verified' %}class="review"{% endif %}>
<td>{{r.date|ukdate}}</td><td><a href="/batch/{{r.bid}}">{{r.bid}} · {{r.lid}}</a><br><a class="tiny" target="_blank" href="/batch/{{r.bid}}/file/{{r.file}}">View PDF</a></td>
<td>{{r.recipient}}</td><td>{{r.sender}}</td>
<td>{% if r.state == 'verified' %}<span class="pill active">✔ {{r.company}}</span>{% else %}<span class="pill high">check: {{r.company or "?"}}</span>{% endif %}
<br><span class="tiny muted">{{r.note}}</span></td></tr>
{% else %}<tr><td colspan="5" class="muted">No older letters found.</td></tr>{% endfor %}</table></div>{% endblock %}"""

POOL = """{% extends "base" %}{% block body %}""" + STYLE + """{% set tab='pool' %}""" + TABS + """
<div class="card"><h1>Unclaimed letters – all batches</h1>
<p class="muted">Every letter with no client, from every batch, grouped by the company name printed on it. Link a group to an order
(the letters stay held until KYC), or hide letters that are not for our customers. {{n}} letter(s).</p>
<div class="btnrow" style="margin-bottom:10px"><input type="text" id="q" placeholder="Search name, unit, sender…" style="max-width:340px">
{% if show_dis %}<a class="btn small secondary" href="/unclaimed">← Back</a>{% else %}<a class="btn small secondary" href="/unclaimed?show=dismissed">Hidden letters</a>{% endif %}
<form method="post" action="/pending/search_everything" class="inl"><button class="btn small secondary">🔎 Re-check all against client database</button></form></div>
{% if bg.running %}<div class="warnb">⏳ Searching in the background – refresh in a minute.</div>{% endif %}
<table id="t"><tr><th>Printed company name</th><th>Letters</th><th>Arrived</th><th>Possible order</th><th>Action</th></tr>
{% for g in groups %}<tr class="g" data-s="{{ (g.name ~ ' ' ~ g.units|join(' ') ~ ' ' ~ g.letters|map(attribute='sender')|join(' '))|lower }}">
<td><b>{{g.name}}</b>{% if g.units %}<br><span class="muted">unit {{g.units|join(", ")}}</span>{% endif %}
{% for k in g.kinds %}<br><span class="dk">{{pkind[k]}}</span>{% endfor %}
<details><summary class="tiny">show {{g.n}} letter(s)</summary>{% for e in g.letters %}<div class="tiny" style="margin:4px 0">
{{e.date|ukdate}} · {{e.sender}} · <a target="_blank" href="/batch/{{e.bid}}/file/{{e.file}}">PDF</a> · <a href="/batch/{{e.bid}}">{{e.bid}} {{e.lid}}</a>
{% if e.linked %}<span class="pill active">linked {{e.linked}}</span>{% endif %}{% if e.suggested %}<span class="pill">possible client: {{e.suggested}}</span>{% endif %}</div>{% endfor %}</details></td>
<td>{{g.n}}</td><td class="tiny" style="white-space:nowrap">{{g.first|ukdate}}{% if g.last != g.first %}<br>– {{g.last|ukdate}}{% endif %}</td>
<td>{% for x in g.sugg %}<a href="/pending/order/{{x.o.id}}">{{x.o.id}} {{x.o.company_name or x.o.contact_name or x.o.email}}</a> <span class="pill">{{x.why}}</span>
{% if not x.date_ok %}<br><span class="pill high tiny">some letters arrived before the order date</span>{% endif %}<br>{% endfor %}
{% for o in g.linked %}<span class="pill active">linked to {{o}}</span> {% endfor %}{% if not g.sugg and not g.linked %}<span class="muted">—</span>{% endif %}</td>
<td>{% if show_dis %}<form method="post" action="/unclaimed/dismiss" class="inl"><input type="hidden" name="undo" value="1">
{% for e in g.letters %}<input type="hidden" name="key" value="{{e.key}}">{% endfor %}<button class="btn small secondary">Restore</button></form>
{% else %}{% set free = g.letters|rejectattr("linked")|list %}{% if free %}
<form method="post" class="inl" action="" onsubmit="this.action='/pending/order/'+this.order.value+'/link';return this.order.value!=''">
{% for e in free %}<input type="hidden" name="key" value="{{e.key}}">{% endfor %}
<input type="hidden" name="take_name" value="{{ g.name if g.norm != '~' else '' }}"><input type="hidden" name="back" value="/unclaimed">
<select name="order"><option value="">Link to order…</option>{% for o in orders %}<option value="{{o.id}}" {{ 'selected' if g.sugg and g.sugg[0].o.id == o.id }}>{{o.id}} – {{o.company_name or o.contact_name or o.email or "no name"}}{% if o.unit %} ({{o.unit}}){% endif %}</option>{% endfor %}</select>
<button class="btn small">Link</button></form>
<div class="btnrow" style="margin-top:6px">{% if g.norm != '~' %}<form method="post" action="/unclaimed/new_order" class="inl">
<input type="hidden" name="name" value="{{g.name}}"><input type="hidden" name="unit" value="{{g.units[0] if g.units|length == 1 else ''}}"><input type="hidden" name="order_date" value="{{g.first}}">
<button class="btn small secondary">New order from this</button></form>{% endif %}
<form method="post" action="/unclaimed/dismiss" class="inl" onsubmit="return confirm('Hide these {{free|length}} letter(s) as not our customer?')">
{% for e in free %}<input type="hidden" name="key" value="{{e.key}}">{% endfor %}<button class="btn small secondary">Not our customer</button></form></div>
{% endif %}{% endif %}</td></tr>
{% else %}<tr><td colspan="5" class="muted">No unclaimed letters.</td></tr>{% endfor %}</table></div>
<script>document.getElementById('q').addEventListener('input',function(){const v=this.value.toLowerCase().trim();
document.querySelectorAll('#t tr.g').forEach(r=>{r.style.display=!v||r.dataset.s.includes(v)?'':'none'})});</script>{% endblock %}"""

NEWCO = """{% extends "base" %}{% block body %}""" + STYLE + """{% set tab='ch' %}""" + TABS + """
<div class="card"><h1>New companies at our address</h1>
<p class="muted">Checked every day on Companies House: companies newly incorporated with their registered office at our address
(search "{{chs.location}}", address containing "{{chs.match|join('" or "')}}"). Link each one to the customer's order – the order
then knows its company name and its letters can be found.</p>
{% if not chs.key %}<div class="errb">CH_API_KEY is not set in Railway Variables – the daily check is off.</div>{% endif %}
{% if ch.error %}<div class="errb">Last check failed: {{ch.error}}</div>{% endif %}
<p class="muted">Last check: {{ch.last_run|ukdate if ch.last_run else "never"}}</p>
<div class="btnrow"><form method="post" action="/pending/new_companies/check" class="inl"><button class="btn small">Check now</button></form>
<form method="post" action="/pending/new_companies/check" class="inl"><input type="hidden" name="days" value="90"><button class="btn small secondary">Check last 90 days</button></form>
<a class="btn small secondary {{ 'on' if show=='new' }}" href="/pending/new_companies">To link</a>
<a class="btn small secondary" href="/pending/new_companies?show=other">Linked / ignored / existing clients</a></div>
<table style="margin-top:12px"><tr><th>Company</th><th>Incorporated</th><th>Registered office</th><th>Letters waiting</th><th>Action</th></tr>
{% for f in fs %}<tr><td><b>{{f.name}}</b><br><a class="tiny" target="_blank" href="https://find-and-update.company-information.service.gov.uk/company/{{f.number}}">{{f.number}}</a></td>
<td>{{f.created|ukdate}}</td><td class="tiny">{{f.address}}{% if f.unit %}<br><span class="pill">unit {{f.unit}}</span>{% endif %}</td>
<td>{% if f._letters %}<a href="/unclaimed" class="pill" style="background:#fef3c7;color:#92400e">{{f._letters}} unclaimed</a>{% else %}<span class="muted">—</span>{% endif %}</td>
<td>{% if f.status == 'new' %}
<form method="post" action="/pending/new_companies/act" class="inl"><input type="hidden" name="number" value="{{f.number}}"><input type="hidden" name="act" value="link">
<select name="order"><option value="">Link to order…</option>{% for o in orders %}<option value="{{o.id}}" {{ 'selected' if f._sugg and f._sugg.id == o.id }}>{{o.id}} – {{o.company_name or "no name yet"}} · {{o.contact_name or o.email}}{% if o.unit %} ({{o.unit}}){% endif %} · {{o.order_date|ukdate}}</option>{% endfor %}</select>
<button class="btn small" onclick="return this.form.order.value!=''">Link</button></form>
{% if f._sugg %}<br><span class="tiny muted">suggested: {{f._sugg.id}}</span>{% endif %}
<div class="btnrow" style="margin-top:6px"><form method="post" action="/pending/new_companies/act" class="inl"><input type="hidden" name="number" value="{{f.number}}"><input type="hidden" name="act" value="new_order"><button class="btn small secondary">New order</button></form>
<form method="post" action="/pending/new_companies/act" class="inl"><input type="hidden" name="number" value="{{f.number}}"><input type="hidden" name="act" value="ignore"><button class="btn small secondary">Ignore</button></form></div>
{% else %}<span class="pill">{{ {'linked':'linked to '~(f.order or ''), 'ignored':'ignored', 'client':'already a client'}[f.status] }}</span>
{% if f.status == 'ignored' %}<form method="post" action="/pending/new_companies/act" class="inl"><input type="hidden" name="number" value="{{f.number}}"><input type="hidden" name="act" value="restore"><button class="btn small secondary">Restore</button></form>{% endif %}{% endif %}</td></tr>
{% else %}<tr><td colspan="5" class="muted">Nothing here.</td></tr>{% endfor %}</table></div>{% endblock %}"""
