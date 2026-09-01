#!/usr/bin/env python3
"""
MailSort – AI-assisted sorting of bulk mail scans for a virtual business address.

Pipeline
  1. extract   : read every page of the bulk PDF (text layer, OCR fallback)
  2. classify  : AI decides, per page, which company the letter is for, who sent it,
                 what kind of letter it is, and whether the page continues the previous letter
  3. group     : consecutive pages belonging to the same letter are grouped
  4. match     : each letter is matched to a client record (email + account status)
  5. split     : one PDF per letter, filed under the client's folder
  6. notify    : one draft email per client, plus a manifest and a review page

Usage
  python mailsort.py process BULK.pdf --clients clients.csv --out output/
  python mailsort.py process BULK.pdf --clients clients.csv --out output/ --classification my.json
      (use a pre-made classification file instead of calling the AI)

Env
  ANTHROPIC_API_KEY  – required for the AI classification step (unless --classification is given)
  MAILSORT_MODEL     – optional, defaults to claude-sonnet-4-5
"""
from __future__ import annotations

import argparse, csv, datetime as dt, difflib, html, io, json, os, re, sys
from pathlib import Path

import fitz  # PyMuPDF

# ----------------------------------------------------------------------------- helpers

def slug(s: str, n: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")
    return (s or "unknown")[:n]


def normalise_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(ltd|limited|llp|plc|inc|co|company|the|uk|group|holdings)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ----------------------------------------------------------------------------- 1. extract

def _ocr_png(png: bytes) -> str:
    import pytesseract
    from PIL import Image
    return pytesseract.image_to_string(Image.open(io.BytesIO(png))).strip()


def extract_pages(pdf_path: Path, ocr_dpi: int = 200, min_chars: int = 40,
                  progress=None, workers: int = 4) -> list[dict]:
    """Return [{page: 1, text: ..., source: 'text'|'ocr'}] for every page. OCR runs in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    doc = fitz.open(pdf_path)
    pages, jobs = [], {}
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        rec = {"page": i + 1, "text": text, "source": "text"}
        # low-res JPEG of every page so the AI can READ the address from the image, not just trust OCR
        try:
            pix = page.get_pixmap(dpi=110)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=60)
            rec["_img"] = buf.getvalue()
        except Exception:
            rec["_img"] = None
        if len(text) < min_chars:  # scanned image – render now, OCR later in parallel
            jobs[i] = page.get_pixmap(dpi=ocr_dpi).tobytes("png")
            rec["source"] = "ocr"
        pages.append(rec)
    doc.close()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_ocr_png, png): i for i, png in jobs.items()}
        for f in futs:
            i = futs[f]
            try:
                pages[i]["text"] = f.result()
            except Exception as e:  # pragma: no cover
                pages[i]["text"], pages[i]["source"] = "", f"ocr-failed: {e}"
            done += 1
            if progress:
                progress(done, len(jobs))
    return pages


# ----------------------------------------------------------------------------- 2. classify

CLASSIFY_SYSTEM = """You sort scanned post for a virtual business address provider.
You receive an IMAGE of ONE page of a bulk scan together with its OCR text, plus the text of the
previous page. The OCR text can be garbled – READ THE IMAGE YOURSELF, especially the recipient
address block, the unit/SIU number and the company name; the image is the ground truth. Decide:

- recipient_company: the company name EXACTLY as printed on the page, copied letter-for-letter
  from the image. NEVER replace it with a similar-looking company name and never "correct" it to
  a company you think it should be – letters are sometimes addressed to companies that are NOT
  clients, and the exact printed name decides whether the letter is held or delivered.
  "" if the page has no addressee (e.g. a continuation sheet or blank page).
- is_continuation: true if this page is a later page of the SAME letter/document as the previous page.
  Strong signals: NO fresh recipient address block, page numbering ("2 of 3", "Page 2"), same sender,
  same subject or reference number, text that carries on mid-sentence, statement/table rows continuing,
  terms and conditions or appendix pages. Many multi-page documents only show the address on page 1 –
  a page with no recipient address that plausibly follows on is a continuation, NOT a new letter.
- is_blank: true if the page is essentially empty (scanner separator, back of a sheet).
- sender: organisation that sent the letter (HMRC, Companies House, a bank, a supplier, ...).
- letter_type: one of official_government, bank_financial, legal, invoice_bill, marketing, personal, other.
- urgency: high | normal | low  (high = deadlines, penalties, legal, court, tax demands).
- summary: one short sentence describing the letter for the client.
- address: the full recipient address block exactly as printed on the page (company name and all
  address lines, separated by ", "); "" if the page shows no recipient address.

Respond with ONLY a JSON object with those keys."""


def classify_pages_with_ai(pages: list[dict], client_names: list[str], model: str,
                           progress=None, api_key: str | None = None) -> list[dict]:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    out = []
    prev_text = ""
    for p in pages:
        # NOTE: the client list is deliberately NOT given to the classifier – it must report the
        # name exactly as printed, never snap it to a known client (that caused a misdelivery).
        user = (
            f"PREVIOUS PAGE TEXT (may be empty):\n<<<\n{prev_text[:2500]}\n>>>\n\n"
            f"THIS PAGE (page {p['page']}) TEXT:\n<<<\n{p['text'][:6000]}\n>>>"
        )
        content = []
        if p.get("_img"):
            import base64
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(p["_img"]).decode()}})
        content.append({"type": "text", "text": user})
        resp = client.messages.create(
            model=model, max_tokens=400, system=CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"recipient_company": "", "is_continuation": False, "is_blank": False,
                    "sender": "", "letter_type": "other", "urgency": "normal",
                    "summary": "AI response could not be parsed", "_raw": raw}
        data["page"] = p["page"]
        out.append(data)
        prev_text = p["text"]
        print(f"  page {p['page']:>3}: {data.get('recipient_company') or '-':35.35} "
              f"{'(cont.)' if data.get('is_continuation') else ''}", file=sys.stderr)
        if progress:
            progress(p["page"], len(pages))
    return out


def classify_pages_heuristic(pages: list[dict], client_names: list[str]) -> list[dict]:
    """No-AI fallback: fuzzy-match client names in the page text. Weak but useful for testing."""
    out, prev = [], None
    norm = {normalise_name(c): c for c in client_names}
    for p in pages:
        t = normalise_name(p["text"])
        best, score = "", 0.0
        for n, orig in norm.items():
            if n and n in t:
                best, score = orig, 1.0
                break
        blank = len(p["text"].strip()) < 20
        cont = (not blank) and (best == "" or best == prev) and not re.search(r"\bdear\b", p["text"], re.I)
        out.append({"page": p["page"], "recipient_company": best or (prev if cont else ""),
                    "is_continuation": cont and prev is not None, "is_blank": blank,
                    "sender": "", "letter_type": "other", "urgency": "normal", "address": "",
                    "summary": "(heuristic mode – no AI summary)"})
        if not blank:
            prev = best or prev
    return out


# ----------------------------------------------------------------------------- SIU office check

# "SIU" as printed in the address – tolerant of common OCR misreads (S1U, SlU, S I U, 5IU).
SIU_RE = re.compile(r"(?<![A-Z0-9])[S5]\s?[I1l|]\s?U(?![A-Z])", re.I)


def has_siu(text: str) -> bool:
    return bool(SIU_RE.search(text or ""))


# Unit/SIU number as printed in an address, e.g. "SIU A1049", "Unit A80", "Suite 12B", "Office A-80"
UNIT_RE = re.compile(r"(?:SIU|UNIT|SUITE|OFFICE)\s*(?:NO\.?|#|:|-)?\s*([A-Z]{0,2}\s?-?\d{1,5}[A-Z]?)", re.I)


def normalise_unit(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def extract_unit(*texts: str) -> str:
    """First unit/SIU number found in the given texts (address block first, then page text)."""
    for text in texts:
        m = UNIT_RE.search(text or "")
        if m:
            return normalise_unit(m.group(1))
    return ""


# ----------------------------------------------------------------------------- 3. group

def merge_orphans(letters: list[dict]) -> list[dict]:
    """Safety net: a 'letter' with no addressee and no address block cannot be a real letter start –
    merge it into the previous letter (the AI likely missed a continuation)."""
    out = []
    for L in letters:
        if out and not (L.get("recipient_company") or "").strip() and not (L.get("address") or "").strip():
            out[-1]["pages"].extend(L["pages"])
            if not out[-1].get("summary") and L.get("summary"):
                out[-1]["summary"] = L["summary"]
        else:
            out.append(L)
    return out


def group_letters(classified: list[dict], auto_merge: bool = True) -> list[dict]:
    letters, current = [], None
    for c in classified:
        if c.get("is_blank"):
            continue
        if current is not None and c.get("is_continuation"):
            current["pages"].append(c["page"])
            continue
        current = {"pages": [c["page"]], "address": c.get("address", ""), "recipient_company": c.get("recipient_company", ""),
                   "sender": c.get("sender", ""), "letter_type": c.get("letter_type", "other"),
                   "urgency": c.get("urgency", "normal"), "summary": c.get("summary", "")}
        letters.append(current)
    if auto_merge:
        letters = merge_orphans(letters)
    for i, L in enumerate(letters, 1):
        L["letter_id"] = f"L{i:03d}"
    return letters


# ----------------------------------------------------------------------------- 4. match

def parse_date(s: str):
    """Very tolerant date reader: ISO, UK formats, Excel exports, month names, 2-digit years."""
    s = (s or "").strip()
    if not s:
        return None
    s = s.split("T")[0].strip()               # drop time part of ISO timestamps
    if " " in s and ":" in s:
        s = s.split(" ")[0]                    # drop "12:00:00" style time
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%Y.%m.%d",
                "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
                "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y",
                "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y", "%Y%m%d"):
        try:
            d = dt.datetime.strptime(s, fmt).date()
            if d.year < 100:                   # 2-digit year came through oddly
                d = d.replace(year=d.year + 2000)
            return d
        except ValueError:
            pass
    # Excel serial number (days since 1899-12-30), e.g. 46082
    if s.isdigit() and 20000 < int(s) < 80000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(s))
    return None


SERVICE_DAYS = 365


def apply_service_expiry(r: dict) -> dict:
    """If an active client's start_date is over a year ago, treat them as overdue (automatic)."""
    d = parse_date(r.get("start_date", ""))
    r["_expiry"] = None
    r["_auto_overdue"] = False
    if d:
        expiry = d + dt.timedelta(days=SERVICE_DAYS)
        r["_expiry"] = expiry.isoformat()
        if (r.get("status") or "").strip().lower() == "active" and dt.date.today() >= expiry:
            r["status"] = "overdue"
            r["_auto_overdue"] = True
    return r


def load_clients(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_norm"] = normalise_name(r.get("company_name", ""))
        r["_unit"] = normalise_unit(r.get("siu") or r.get("unit") or "")
        apply_service_expiry(r)
    return rows


def top_candidates(name: str, clients: list[dict], k: int = 8):
    """Best-scoring clients for a printed name, for ambiguity checks and AI verification."""
    n = normalise_name(name)
    scored = []
    for c in clients:
        if not c["_norm"]:
            continue
        s = difflib.SequenceMatcher(None, n, c["_norm"]).ratio()
        if _token_subset(n, c["_norm"]):
            s = max(s, 0.9)
        if c["_norm"] == n:
            s = 1.0
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


VERIFY_SYSTEM = """You verify that scanned post is routed to the correct client of a virtual business
address provider. These are CONFIDENTIAL documents – a wrong match sends one company's post to another.

You get: the addressee and address exactly as printed on the letter (may contain OCR errors), the unit
number found on it (if any), and a numbered list of candidate clients (name + unit number + the
registered address we hold for them). Compare the printed address against each candidate's registered
address as well as the names – matching address details are strong evidence, conflicting ones are
strong evidence AGAINST.

Decide which candidate the letter is really for. Company names can differ by OCR noise ("lnoviox" vs
"Inoviox") but BEWARE of genuinely different companies with similar names ("Inoviox Solutions" vs
"Innovex Solution") – if two candidates are plausible and the unit number does not settle it, you must
answer unsure. Only answer with a candidate when you are confident beyond reasonable doubt.

Respond with ONLY JSON: {"choice": <candidate number, or 0 if none/unsure>, "confident": true/false,
"reason": "<one short sentence>"}"""


def ai_verify_match(letter: dict, candidates: list[dict], model: str, api_key: str | None):
    """Ask the AI which candidate the letter belongs to. Returns (client_or_None, confident, reason)."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    listing = "\n".join(f"{i}. {c['company_name']} (unit: {c.get('siu') or c.get('_unit') or 'none'}; "
                         f"registered address: {c.get('address') or 'not on file'})"
                         for i, c in enumerate(candidates, 1))
    user = (f"LETTER ADDRESSEE (as printed): {letter.get('recipient_company') or '(none)'}\n"
            f"FULL ADDRESS ON LETTER: {letter.get('address') or '(none)'}\n"
            f"UNIT NUMBER FOUND ON LETTER: {letter.get('unit') or '(none)'}\n\n"
            f"CANDIDATE CLIENTS:\n{listing}")
    resp = client.messages.create(model=model, max_tokens=200, system=VERIFY_SYSTEM,
                                  messages=[{"role": "user", "content": user}])
    raw = re.sub(r"^```(?:json)?|```$", "", resp.content[0].text.strip(), flags=re.M).strip()
    try:
        d = json.loads(raw)
        i = int(d.get("choice") or 0)
        if 1 <= i <= len(candidates) and d.get("confident"):
            return candidates[i - 1], True, d.get("reason", "")
        return (candidates[i - 1] if 1 <= i <= len(candidates) else None), False, d.get("reason", "")
    except Exception:
        return None, False, "verification reply could not be parsed"


def _token_subset(a: str, b: str) -> bool:
    """True when every WORD of one normalised name appears in the other (word-boundary check).
    Replaces the old raw-substring boost, which wrongly treated e.g. 'one solutions' as part of
    'connexusone solutions'."""
    ta, tb = set(a.split()), set(b.split())
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


# Words so common in company names that sharing them proves nothing about identity.
GENERIC_NAME_WORDS = {
    "solutions", "solution", "services", "service", "group", "consulting", "consultancy",
    "trading", "enterprises", "enterprise", "ventures", "venture", "holdings", "holding",
    "international", "global", "digital", "tech", "technologies", "technology", "media",
    "marketing", "management", "capital", "partners", "associates", "network", "networks",
    "systems", "logistics", "construction", "properties", "property", "investments",
    "commerce", "online", "store", "shop", "studio", "design", "academy", "london",
}


def names_agree(printed: str, client_name: str) -> bool:
    """True when the name printed on a letter is plausibly the SAME company as client_name.
    Used to stop a unit-number match delivering post addressed to a DIFFERENT company
    (e.g. a letter for 'MOVIQO SOLUTIONS LTD' must never go to 'Inoviox Solutions Ltd' just
    because the unit numbers coincide – the distinctive words 'moviqo'/'inoviox' disagree)."""
    a, b = normalise_name(printed), normalise_name(client_name)
    if not a or not b:
        return True  # nothing printed – the unit number is all the evidence there is
    if a == b or _token_subset(a, b):
        return True
    da = [t for t in a.split() if t not in GENERIC_NAME_WORDS and len(t) > 1]
    db = [t for t in b.split() if t not in GENERIC_NAME_WORDS and len(t) > 1]
    if da and db:
        best = max(difflib.SequenceMatcher(None, x, y).ratio() for x in da for y in db)
        best = max(best, difflib.SequenceMatcher(None, "".join(da), "".join(db)).ratio())
        return best >= 0.75  # allows OCR slips ('SW1FTIQ'≈'swiftiq'), rejects different companies
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def match_client(name: str, clients: list[dict], threshold: float = 0.80):
    n = normalise_name(name)
    if not n:
        return None, 0.0
    best, best_score = None, 0.0
    for c in clients:
        if not c["_norm"]:
            continue
        if c["_norm"] == n:
            return c, 1.0
        s = difflib.SequenceMatcher(None, n, c["_norm"]).ratio()
        if _token_subset(n, c["_norm"]):
            s = max(s, 0.9)
        if s > best_score:
            best, best_score = c, s
    return (best, best_score) if best_score >= threshold else (None, best_score)


def match_letters(letters: list[dict], clients: list[dict], page_text: dict | None = None,
                  use_ai: bool = True, api_key: str | None = None, model: str | None = None):
    """Match each letter to a client using the full safety policy (unit-first, AI verification,
    hold-not-guess). Mutates the letter dicts in place. Called by run_batch on a fresh batch and
    by the app's "Re-check held letters" button after a CSV update (page_text=None then – the
    stored address/unit on each letter is reused)."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    model = model or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5")
    page_text = page_text or {}
    unit_counts: dict = {}
    for c in clients:
        if c.get("_unit"):
            unit_counts[c["_unit"]] = unit_counts.get(c["_unit"], 0) + 1
    dup_units = {u for u, n in unit_counts.items() if n > 1}
    by_unit = {c["_unit"]: c for c in clients if c.get("_unit") and c["_unit"] not in dup_units}
    for L in letters:
        first_text = page_text.get(L["pages"][0], "") if L.get("pages") else ""
        L["unit"] = extract_unit(L.get("address", ""), first_text) or L.get("unit") or ""
        L["unit_mismatch"] = False
        c, s, matched_by = None, 0.0, ""
        L["match_state"], L["match_note"] = "review", ""
        L.pop("suggested_client", None)
        if L["unit"] and L["unit"] in dup_units:
            L["match_note"] = f"unit {L['unit']} is assigned to MORE THAN ONE client in the database – fix the client list"
            c, s = match_client(L["recipient_company"], clients)
            matched_by = "name" if c else ""
        elif L["unit"] and L["unit"] in by_unit:
            c0 = by_unit[L["unit"]]
            if names_agree(L.get("recipient_company", ""), c0["company_name"]):
                c, s, matched_by = c0, 1.0, "unit"
                L["match_state"] = "verified"
                L["match_note"] = "unit number match"
            else:
                # Unit says c0, but the letter is printed for a clearly DIFFERENT company
                # (e.g. MOVIQO SOLUTIONS at a unit recorded for Inoviox). Never deliver on the
                # unit number alone – hold for staff.
                L["match_state"] = "review"
                L["match_note"] = (f"unit {L['unit']} is recorded for {c0['company_name']}, but the letter is "
                                   f"addressed to '{L.get('recipient_company') or 'an unreadable name'}' – a different "
                                   f"company. HELD – check the letter and the client list before assigning.")
                L["suggested_client"] = c0["company_name"]
                c, s, matched_by = None, 0.0, ""
        else:
            c, s = match_client(L["recipient_company"], clients)
            matched_by = "name" if c else ""
            if c and L["unit"] and c.get("_unit") and c["_unit"] != L["unit"]:
                L["unit_mismatch"] = True
            cands = top_candidates(L["recipient_company"], clients)
            runner_up = cands[1][0] if len(cands) > 1 else 0.0
            ambiguous = (c is None) or L["unit_mismatch"] or s < 0.97 or (runner_up >= s - 0.08)
            if c and not ambiguous:
                L["match_state"] = "verified"
                L["match_note"] = "exact name match"
            elif c and use_ai and api_key and not L["unit_mismatch"]:
                # AI double-check against the plausible candidates – confidential post must not be guessed
                try:
                    pick, confident, reason = ai_verify_match(L, [x[1] for x in cands], model, api_key)
                except Exception as ex:
                    pick, confident, reason = None, False, f"verification failed: {ex}"
                if pick is not None and confident:
                    c, s, matched_by = pick, 1.0, "ai-verified"
                    L["match_state"] = "verified"
                    L["match_note"] = reason
                else:
                    L["match_state"] = "review"
                    L["match_note"] = reason or "AI not confident"
                    if pick is not None:
                        L["suggested_client"] = pick["company_name"]
        if c is not None and L["match_state"] == "review" and matched_by == "name" and s < 0.93:
            # too weak to even tentatively assign confidential post – hold as unmatched, keep the hint for staff
            L["suggested_client"] = L.get("suggested_client") or c["company_name"]
            c, s, matched_by = None, 0.0, ""
        L["client"], L["match_score"], L["matched_by"] = c, s, matched_by
        reasons = []
        if c and L["match_state"] == "review":
            reasons.append("MATCH NOT VERIFIED – staff must confirm before sending")
        if not c and L["match_state"] == "review" and L.get("suggested_client"):
            reasons.append(f"HELD – no reliable match (possible: {L['suggested_client']}) – staff must assign")
        if L["unit_mismatch"]:
            reasons.append(f"UNIT MISMATCH – letter shows unit {L['unit']}, but {c['company_name']} is unit {c.get('siu') or c.get('_unit')}")
        if not c: reasons.append("no client match")
        elif s < 0.95 and matched_by != "unit": reasons.append("fuzzy match")
        if c and (c.get("status") or "").lower() not in STATUS_OK: reasons.append(f"status={c.get('status')}")
        if c and not c.get("email"): reasons.append("no email on file")
        if not L.get("siu_ok", True): reasons.append("SIU OFFICE MISSING")
        if c and (c.get("kyc") or "").lower() == "no": reasons.append("KYC NOT DONE")
        L["in_package"] = letter_in_package(L, c)
        if not L["in_package"]: reasons.append(f"not in {c.get('package')} package")
        L["needs_review"] = "; ".join(reasons)
    return letters


# ----------------------------------------------------------------------------- 5. split

def split_letters(pdf_path: Path, letters: list[dict], out_dir: Path, batch_tag: str):
    doc = fitz.open(pdf_path)
    for L in letters:
        folder_name = slug(L["client"]["company_name"]) if L.get("client") else "_UNMATCHED"
        folder = out_dir / "letters" / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        fname = f"{batch_tag}_{L['letter_id']}_{slug(L['sender'] or 'unknown_sender', 30)}_{L['letter_type']}.pdf"
        new = fitz.open()
        for pg in L["pages"]:
            new.insert_pdf(doc, from_page=pg - 1, to_page=pg - 1)
        new.save(folder / fname)
        new.close()
        L["file"] = str((folder / fname).relative_to(out_dir))
    doc.close()


# ----------------------------------------------------------------------------- 6. notify

# --- editable email templates: the app can override these via the TEMPLATES dict (see /templates page)
TEMPLATES: dict = {}


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(text: str, **kw) -> str:
    try:
        return text.format_map(_SafeDict(**kw))
    except Exception:
        return text  # a malformed template must never break sending


DEFAULTS = {
    "batch_subject": "{n} new item(s) of post received – {company}",
    "batch_body": (
        "Dear {contact},\n\n"
        "We have received {n} item(s) of post for {company} today ({date}). They have been scanned and\n"
        "uploaded to your client portal.\n\n{items}\n"
        "{urgent_note}{upgrade_note}"
        "You can view and download the scans by logging in to your portal.\n\n"
        "Kind regards,\n{sender_name}\n"),
    "urgent_note": "At least one item looks time-sensitive – please review it promptly.\n\n",
    "upgrade_note": ("You also received {n_excluded} item(s) of post not covered by the {package} package "
                     "(e.g. non-government mail). Upgrade your package to have these scanned and sent to you.\n\n"),
    "excluded_body": (
        "Dear {contact},\n\n"
        "We have received {n_excluded} item(s) of post for {company} today ({date}). These items are not covered "
        "by your {package} package (e.g. non-government mail), so they have not been scanned and sent. "
        "Upgrade your package to have all your post scanned and emailed to you, or contact us to arrange "
        "collection or forwarding.\n\nKind regards,\n{sender_name}\n"),
}


def tget(key: str) -> str:
    return TEMPLATES.get(key) or DEFAULTS.get(key, "")


STATUS_OK = {"active"}

# Government senders – covered by every package. Basic/Standard get ONLY these.
GOV_RE = re.compile(r"(hmrc|h\.?m\.?\s*revenue|revenue\s*(&|and)\s*customs|companies\s*house|hm\s*courts|"
                    r"tribunal|county\s*court|magistrates|hmcts|gov\.uk|home\s*office|dvla|dwp|"
                    r"insolvency\s*service|valuation\s*office|information\s*commissioner|\bico\b|"
                    r"pensions?\s*regulator|border\s*force|uk\s*visas|hse\b|council\b)", re.I)


def letter_in_package(L: dict, client: dict | None) -> bool:
    """Premium (or no package set) = everything. Basic/Standard = official government mail only."""
    if not client:
        return True
    pkg = (client.get("package") or "").strip().capitalize()
    if pkg in ("", "Premium"):
        return True
    return L.get("letter_type") == "official_government" or bool(GOV_RE.search(L.get("sender") or ""))


def build_emails(letters: list[dict], out_dir: Path, sender_name: str, today: str):
    by_client: dict[str, list[dict]] = {}
    for L in letters:
        if L.get("client"):
            by_client.setdefault(L["client"]["company_name"], []).append(L)
    emails_dir = out_dir / "emails"
    emails_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for company, all_ls in by_client.items():
        c = all_ls[0]["client"]
        ls = [L for L in all_ls if L.get("in_package", True)]
        excluded = [L for L in all_ls if not L.get("in_package", True)]
        status = (c.get("status") or "").strip().lower()
        items = "\n".join(
            f"  {i}. From {L['sender'] or 'unknown sender'} – {L['summary']}"
            + ("  [URGENT]" if L["urgency"] == "high" else "")
            for i, L in enumerate(ls, 1))
        urgent = any(L["urgency"] == "high" for L in ls)
        pkg = (c.get("package") or "your").strip()
        upgrade_note = render(tget("upgrade_note"), n_excluded=len(excluded), package=pkg) if excluded else ""
        fields = dict(n=len(ls), company=company, contact=c.get("contact_name") or "Client",
                      date=today, items=items, sender_name=sender_name,
                      urgent_note=tget("urgent_note") if urgent else "", upgrade_note=upgrade_note)
        subject = render(tget("batch_subject"), **fields)
        body = f"To: {c.get('email', '')}\nSubject: {subject}\n\n" + render(tget("batch_body"), **fields)
        action = "SEND" if status in STATUS_OK else f"HOLD – account status is '{status or 'unknown'}'"
        if not ls and excluded:
            action = f"HOLD – all {len(excluded)} letter(s) outside {pkg} package (staff to decide)"
            subject = render(tget("batch_subject"), **fields)
            body = f"To: {c.get('email', '')}\nSubject: {subject}\n\n" + render(
                tget("excluded_body"), n_excluded=len(excluded), company=company, date=today,
                contact=c.get("contact_name") or "Client", package=pkg, sender_name=sender_name)
        path = emails_dir / f"{slug(company)}.txt"
        path.write_text(f"# ACTION: {action}\n\n{body}", encoding="utf-8")
        results.append({"company": company, "email": c.get("email", ""), "status": status,
                        "action": action, "letters": len(ls), "excluded": len(excluded),
                        "file": str(path.relative_to(out_dir))})
    return results


def write_manifest(letters, emails, out_dir: Path, batch_tag: str, today: str):
    with open(out_dir / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["batch", "letter_id", "pages", "recipient_as_printed", "address_as_printed", "client_address_on_file", "matched_client", "client_id",
                    "match_score", "client_status", "client_email", "sender", "letter_type", "urgency",
                    "summary", "file", "siu_office", "unit_on_letter", "matched_by", "in_package", "reseller", "kyc", "needs_review"])
        for L in letters:
            c = L.get("client") or {}
            w.writerow([batch_tag, L["letter_id"], "-".join(map(str, L["pages"])) if len(L["pages"]) > 1 else L["pages"][0],
                        L["recipient_company"], L.get("address", ""), c.get("address", ""), c.get("company_name", ""), c.get("client_id", ""),
                        f"{L['match_score']:.2f}", c.get("status", ""), c.get("email", ""), L["sender"],
                        L["letter_type"], L["urgency"], L["summary"], L.get("file", ""),
                        "yes" if L.get("siu_ok", True) else "MISSING",
                        L.get("unit", ""), L.get("matched_by", ""),
                        "yes" if L.get("in_package", True) else "EXCLUDED",
                        c.get("reseller", "") or ("Direct" if c else ""), c.get("kyc", ""), L["needs_review"]])

    def esc(x):
        return html.escape(str(x))

    rows = "".join(
        f"<tr class='{'review' if L['needs_review'] else ''}'>"
        f"<td>{L['letter_id']}</td><td>{'-'.join(map(str, L['pages']))}</td>"
        f"<td>{esc(L['recipient_company'])}</td>"
        f"<td>{esc((L.get('client') or {}).get('company_name', '— no match —'))}<br><small>{L['match_score']:.0%}</small></td>"
        f"<td>{esc((L.get('client') or {}).get('status', ''))}</td>"
        f"<td>{esc(L['sender'])}</td><td>{esc(L['letter_type'])}</td><td>{esc(L['urgency'])}</td>"
        f"<td>{esc(L['summary'])}</td><td><a href='{esc(L.get('file', ''))}'>open</a></td>"
        f"<td>{esc(L['needs_review'])}</td></tr>"
        for L in letters)
    erows = "".join(
        f"<tr><td>{esc(e['company'])}</td><td>{esc(e['email'])}</td><td>{esc(e['status'])}</td>"
        f"<td>{e['letters']}</td><td><b>{esc(e['action'])}</b></td><td><a href='{esc(e['file'])}'>draft</a></td></tr>"
        for e in emails)
    page = f"""<!doctype html><html><head><meta charset='utf-8'><title>MailSort review – {batch_tag}</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%;margin-bottom:32px}}
th,td{{border:1px solid #ddd;padding:6px 8px;font-size:14px;vertical-align:top}}th{{background:#f3f3f3;text-align:left}}
tr.review{{background:#fff4e5}}h2{{margin-top:32px}}</style></head><body>
<h1>MailSort review – batch {batch_tag} ({today})</h1>
<p>{len(letters)} letters found. Rows highlighted in orange need a human check before upload/email.</p>
<h2>Letters</h2><table><tr><th>ID</th><th>Pages</th><th>Addressee (as printed)</th><th>Matched client</th><th>Status</th>
<th>Sender</th><th>Type</th><th>Urgency</th><th>Summary</th><th>PDF</th><th>Review?</th></tr>{rows}</table>
<h2>Client emails</h2><table><tr><th>Client</th><th>Email</th><th>Status</th><th>Letters</th><th>Action</th><th>Draft</th></tr>{erows}</table>
</body></html>"""
    (out_dir / "review.html").write_text(page, encoding="utf-8")


# ----------------------------------------------------------------------------- main

def run_batch(pdf: Path, clients_csv: Path, out: Path, *, batch_tag: str | None = None,
              sender_name: str = "The Startitup Team", classification: Path | None = None,
              use_ai: bool = True, api_key: str | None = None, model: str | None = None,
              status=lambda msg, frac=None: None) -> dict:
    """Run the whole pipeline. `status(message, fraction)` is called with progress updates.
    Returns {"letters": [...], "emails": [...], "batch": tag, "out": out}."""
    out.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    batch_tag = batch_tag or f"{today}_{slug(pdf.stem, 20)}"
    clients = load_clients(clients_csv)
    client_names = [c["company_name"] for c in clients]
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    model = model or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5")

    status(f"Reading {pdf.name} …", 0.0)
    pages = extract_pages(pdf, progress=lambda d, n: status(f"OCR page {d}/{n}", 0.05 + 0.40 * d / max(n, 1)))
    (out / "pages.json").write_text(json.dumps([{k: v for k, v in pg.items() if not k.startswith("_")}
                                                 for pg in pages], indent=1), encoding="utf-8")
    status(f"{len(pages)} pages read ({sum(p['source']=='ocr' for p in pages)} OCR'd)", 0.45)

    if classification:
        classified = json.loads(Path(classification).read_text(encoding="utf-8"))
        mode = "file"
    elif use_ai and api_key:
        classified = classify_pages_with_ai(
            pages, client_names, model, api_key=api_key,
            progress=lambda d, n: status(f"AI reading page {d}/{n}", 0.45 + 0.40 * d / max(n, 1)))
        mode = "ai"
    else:
        status("No API key – using rough heuristic matching", 0.5)
        classified = classify_pages_heuristic(pages, client_names)
        mode = "heuristic"
    (out / "classification.json").write_text(json.dumps(classified, indent=1), encoding="utf-8")

    status("Grouping pages into letters …", 0.87)
    # auto-merge orphan pages only in AI mode – the heuristic can't tell unknown companies from continuations
    letters = group_letters(classified, auto_merge=(mode == "ai" or mode == "file"))

    status("Checking SIU office on each letter …", 0.89)
    page_text = {pg["page"]: pg["text"] for pg in pages}
    for L in letters:
        L["siu_ok"] = has_siu(page_text.get(L["pages"][0], ""))

    status("Matching letters to clients …", 0.90)
    match_letters(letters, clients, page_text, use_ai=use_ai, api_key=api_key, model=model)

    status("Splitting PDF …", 0.93)
    split_letters(pdf, letters, out, batch_tag)

    status("Drafting emails + manifest …", 0.97)
    emails = build_emails(letters, out, sender_name, today)
    write_manifest(letters, emails, out, batch_tag, today)

    matched = sum(1 for L in letters if L.get("client"))
    summary = (f"{len(letters)} letters, {matched} matched, {len(letters) - matched} unmatched, "
               f"{sum(1 for L in letters if L['needs_review'])} flagged for review")
    status("Done – " + summary, 1.0)
    return {"letters": letters, "emails": emails, "batch": batch_tag, "out": out,
            "pages": len(pages), "mode": mode, "summary": summary}


def cmd_process(a):
    def status(msg, frac=None):
        print(msg, file=sys.stderr)
    r = run_batch(Path(a.pdf), Path(a.clients), Path(a.out), batch_tag=a.batch, sender_name=a.sender_name,
                  classification=Path(a.classification) if a.classification else None,
                  use_ai=not a.no_ai, status=status)
    print(f"Open {r['out'] / 'review.html'} to check the batch.", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process", help="process one bulk scan")
    p.add_argument("pdf")
    p.add_argument("--clients", required=True, help="CSV: client_id,company_name,contact_name,email,status")
    p.add_argument("--out", default="output")
    p.add_argument("--batch", help="batch tag used in file names (default: date + file name)")
    p.add_argument("--classification", help="use a pre-made classification JSON instead of AI")
    p.add_argument("--no-ai", action="store_true", help="force heuristic mode")
    p.add_argument("--sender-name", default="The Startitup Team")
    p.set_defaults(fn=cmd_process)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
