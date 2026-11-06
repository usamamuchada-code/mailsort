"""
Tracking slips (v41a) – shared by the ITIN tab (itinforms.py) and the Company forms tab (chforms.py).

read_slip(data, filename) -> {"carrier", "tracking_no", "date_posted" (YYYY-MM-DD), "service", "sent_to",
                              "method" ("ai" / "ocr"), "format_ok" (bool), "note"}
tracking_url(carrier, number) -> carrier's public tracking page (or "")

AI (Anthropic, same key as the rest of the mail room) reads the photo/PDF of the receipt or label.
Without a key it falls back to OCR (tesseract) + the number patterns below. app.py is not changed.
"""
from __future__ import annotations

import base64, datetime as dt, io, json, os, re, sys

# carrier key: (display name, regex for the tracking number with spaces removed, tracking URL with {n})
CARRIERS = {
    "royal_mail": ("Royal Mail", r"[A-Z]{2}\d{9}GB", "https://www.royalmail.com/track-your-item#/tracking-results/{n}"),
    "parcelforce": ("Parcelforce", r"[A-Z]{2}\d{7}|[A-Z]{2}\d{9}GB|[A-Z]{4}\d{10}",
                    "https://www.parcelforce.com/track-trace?trackNumber={n}"),
    "ups": ("UPS", r"1Z[0-9A-Z]{16}", "https://www.ups.com/track?tracknum={n}"),
    "fedex": ("FedEx", r"\d{12}|\d{15}|\d{20}|\d{22}", "https://www.fedex.com/fedextrack/?trknbr={n}"),
    "dhl": ("DHL", r"\d{10}|JJD\d{16,20}|[A-Z]{3}\d{7}", "https://www.dhl.com/gb-en/home/tracking.html?tracking-id={n}"),
    "usps": ("USPS", r"9[1-5]\d{18,24}|[A-Z]{2}\d{9}US", "https://tools.usps.com/go/TrackConfirmAction?tLabels={n}"),
    "evri": ("Evri", r"[A-Z0-9]{16}|H[A-Z0-9]{15}", "https://www.evri.com/track/parcel/{n}"),
    "dpd": ("DPD", r"\d{14}|\d{10}", "https://track.dpd.co.uk/parcels/{n}"),
}
_ALIASES = {"royal mail": "royal_mail", "royalmail": "royal_mail", "post office": "royal_mail", "special delivery": "royal_mail",
            "signed for": "royal_mail", "international tracked": "royal_mail", "parcelforce": "parcelforce",
            "parcel force": "parcelforce", "ups": "ups", "united parcel": "ups", "fedex": "fedex", "fed ex": "fedex",
            "federal express": "fedex", "dhl": "dhl", "usps": "usps", "us postal": "usps", "united states postal": "usps",
            "evri": "evri", "hermes": "evri", "dpd": "dpd"}

PROMPT = """You read photos/scans of postage receipts, courier labels and shipping confirmations (Post Office
receipts, Royal Mail Special Delivery / Signed For / International Tracked, Parcelforce, UPS, FedEx, DHL, USPS, Evri, DPD).
Reply with ONLY one JSON object, no other text:
{"carrier": "", "tracking_no": "", "date_posted": "", "service": "", "sent_to": "", "other_numbers": [], "notes": ""}
- carrier: the company carrying the item (e.g. "Royal Mail", "UPS", "FedEx", "DHL").
- tracking_no: the tracking / consignment / waybill / airway bill number exactly as printed (letters and digits).
  NOT the receipt number, session/branch/FAD code, account number, phone number, price or postcode.
  Royal Mail tracked items look like AB123456789GB; UPS 1Z…; DHL Express waybill 10 digits; FedEx 12 digits.
- date_posted: date the item was posted / shipped / accepted, as YYYY-MM-DD ("" if not shown). UK receipts are day/month/year.
- service: e.g. "Special Delivery Guaranteed", "International Tracked & Signed", "UPS Express Saver".
- sent_to: recipient name and town/country if shown, short.
- other_numbers: any other tracking numbers if several items are on the receipt.
If something is not on the slip, leave it "". Never guess a tracking number."""


def _cfg() -> dict:
    """The mail room settings (Settings page + Railway variables) – read from app.py without importing it again."""
    for name in ("app", "__main__"):
        mod = sys.modules.get(name)
        fn = getattr(mod, "load_config", None)
        if fn:
            try:
                return fn()
            except Exception:
                pass
    return {}


def _images(data: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Photo (any format) or PDF -> up to 2 JPEG/PNG images for the AI / OCR."""
    if data[:4] == b"%PDF" or (filename or "").lower().endswith(".pdf"):
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        return [("image/png", doc[i].get_pixmap(dpi=200).tobytes("png")) for i in range(min(2, len(doc)))]
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    if max(im.size) > 2400:
        im.thumbnail((2400, 2400))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return [("image/jpeg", buf.getvalue())]


def carrier_key(name: str, number: str = "") -> str:
    n = (name or "").lower()
    for alias, k in _ALIASES.items():
        if alias in n:
            return k
    num = clean_number(number)
    for k in ("royal_mail", "ups", "usps", "fedex", "dhl"):          # most distinctive patterns first
        if num and re.fullmatch(CARRIERS[k][1], num):
            return k
    return ""


def clean_number(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def format_ok(carrier: str, number: str) -> bool:
    k = carrier_key(carrier, number)
    num = clean_number(number)
    return bool(k and num and re.fullmatch(CARRIERS[k][1], num))


def tracking_url(carrier: str, number: str) -> str:
    num = clean_number(number)
    k = carrier_key(carrier, num)
    return CARRIERS[k][2].format(n=num) if k and num else ""


def _iso(s: str) -> str:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if dt.date(2000, 1, 1) <= d <= dt.date.today() + dt.timedelta(days=1):
            return d.isoformat()
    return ""


def _from_text(text: str) -> dict:
    """OCR fallback – find a tracking number by the known patterns and a date."""
    t = text.upper()
    flat = re.sub(r"(?<=[A-Z0-9]) (?=[A-Z0-9])", "", t)       # "AB 1234 5678 9GB" -> "AB123456789GB"
    out = {"carrier": "", "tracking_no": "", "date_posted": "", "service": "", "sent_to": ""}
    for k in ("royal_mail", "ups", "usps", "parcelforce", "fedex", "dhl"):
        m = re.search(r"(?<![A-Z0-9])(" + CARRIERS[k][1] + r")(?![A-Z0-9])", flat)
        if m:
            named = carrier_key(t)
            out["carrier"] = CARRIERS[named or k][0]
            out["tracking_no"] = m.group(1)
            break
    if not out["carrier"]:
        k = carrier_key(t)
        out["carrier"] = CARRIERS[k][0] if k else ""
    for m in re.finditer(r"\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2} [A-Z]{3,9} \d{4})\b", t):
        iso = _iso(m.group(1).title())
        if iso:
            out["date_posted"] = iso
            break
    return out


def read_slip(data: bytes, filename: str = "") -> dict:
    imgs = _images(data, filename)
    cfg = _cfg()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    d, method = None, "ocr"
    if key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(b).decode()}}
                       for mt, b in imgs]
            content.append({"type": "text", "text": "Read this postage slip and return the JSON."})
            resp = client.messages.create(model=cfg.get("model") or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5"),
                                          max_tokens=600, system=PROMPT, messages=[{"role": "user", "content": content}])
            m = re.search(r"\{.*\}", resp.content[0].text, re.S)
            if m:
                d, method = json.loads(m.group(0)), "ai"
        except Exception as ex:
            print(f"[tracking] AI read failed, using OCR: {ex}", flush=True)
    if d is None:
        try:
            import pytesseract
            from PIL import Image
            text = "\n".join(pytesseract.image_to_string(Image.open(io.BytesIO(b))) for _mt, b in imgs)
        except Exception as ex:
            raise RuntimeError(f"Could not read the slip ({ex}) – type the tracking number by hand.")
        d = _from_text(text)
    out = {k: str(d.get(k) or "").strip() for k in ("carrier", "tracking_no", "date_posted", "service", "sent_to")}
    out["tracking_no"] = clean_number(out["tracking_no"])
    k = carrier_key(out["carrier"], out["tracking_no"])
    if k:
        out["carrier"] = CARRIERS[k][0]
    out["date_posted"] = _iso(out["date_posted"]) or ""
    out["method"] = method
    out["format_ok"] = format_ok(out["carrier"], out["tracking_no"])
    out["other_numbers"] = [clean_number(x) for x in (d.get("other_numbers") or []) if clean_number(x)
                            and clean_number(x) != out["tracking_no"]][:5] if isinstance(d.get("other_numbers"), list) else []
    if not out["tracking_no"]:
        out["note"] = "No tracking number found on the slip – type it by hand."
    elif not out["format_ok"]:
        out["note"] = "Check the tracking number – it does not look like a usual " + (out["carrier"] or "carrier") + " number."
    else:
        out["note"] = ""
    out["url"] = tracking_url(out["carrier"], out["tracking_no"])
    return out


def save_slip(folder, data: bytes, filename: str) -> str:
    """Store the slip (as uploaded) in <folder>/slips/ and return its file name."""
    from pathlib import Path
    sd = Path(folder) / "slips"
    sd.mkdir(parents=True, exist_ok=True)
    ext = (Path(filename or "").suffix.lower() or ".jpg")[:6]
    if not re.fullmatch(r"\.[a-z0-9]+", ext):
        ext = ".jpg"
    name = dt.datetime.now().strftime("slip_%Y%m%d_%H%M%S") + ext
    i = 1
    while (sd / name).exists():
        i += 1
        name = dt.datetime.now().strftime("slip_%Y%m%d_%H%M%S") + f"_{i}{ext}"
    (sd / name).write_bytes(data)
    return name


def mimetype(name: str) -> str:
    n = name.lower()
    return ("application/pdf" if n.endswith(".pdf") else "image/png" if n.endswith(".png") else
            "image/webp" if n.endswith(".webp") else "image/heic" if n.endswith((".heic", ".heif")) else "image/jpeg")
