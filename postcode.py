"""
Postcode lookup (v42i) – fills a missing postal code from the rest of the address.

1. OpenStreetMap Nominatim (free; structured search, then reverse lookup of the point found)
2. AI fallback (same Anthropic key as the mail room) – only a postcode it is sure of, marked "check"

Every result says where it came from so staff can check it. Results are cached in memory.
Nominatim usage policy: identify the app (User-Agent) and max 1 request per second.
"""
from __future__ import annotations

import json, os, re, sys, threading, time, urllib.parse, urllib.request

UA = "StartitupMailRoom/1.0 (+https://mail.startitup.global)"
_CACHE: dict = {}
_LOCK = threading.Lock()
_LAST = [0.0]

PC_RE = re.compile(r"\b(\d{4,6}(?:-\d{3,4})?|[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}|[A-Z]\d[A-Z] ?\d[A-Z]\d)\b", re.I)


def has_postcode(text: str) -> bool:
    return bool(PC_RE.search(text or ""))


def _get(url: str) -> object:
    with _LOCK:                                   # Nominatim: max 1 request / second
        wait = 1.05 - (time.time() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def _clean(pc: str) -> str:
    pc = (pc or "").strip().upper()
    pc = pc.split(";")[0].split(",")[0].strip()
    return pc if pc and any(ch.isdigit() for ch in pc) and len(pc) <= 10 else ""


def _osm(street: str, city: str, region: str, country: str) -> dict | None:
    base = "https://nominatim.openstreetmap.org"
    tries = []
    if street:
        tries.append({"street": street, "city": city, "state": region, "country": country})
    tries.append({"city": city, "state": region, "country": country})
    tries.append({"q": ", ".join(x for x in (street, city, region, country) if x)})
    for params in tries:
        params = {k: v for k, v in params.items() if v}
        if not params:
            continue
        res = _get(f"{base}/search?format=jsonv2&addressdetails=1&limit=3&" + urllib.parse.urlencode(params))
        for hit in res or []:
            ad = hit.get("address") or {}
            pc = _clean(ad.get("postcode"))
            place = ", ".join(x for x in (ad.get("city") or ad.get("town") or ad.get("village") or ad.get("county"),
                                          ad.get("state"), ad.get("country")) if x)
            if not pc and hit.get("lat"):
                rev = _get(f"{base}/reverse?format=jsonv2&addressdetails=1&zoom=16&lat={hit['lat']}&lon={hit['lon']}")
                pc = _clean(((rev or {}).get("address") or {}).get("postcode"))
            if pc:
                return {"postcode": pc, "source": "OpenStreetMap", "place": place}
    return None


def _cfg() -> dict:
    for name in ("app", "__main__"):
        fn = getattr(sys.modules.get(name), "load_config", None)
        if fn:
            try:
                return fn()
            except Exception:
                pass
    return {}


def _ai(street: str, city: str, region: str, country: str) -> dict | None:
    cfg = _cfg()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    addr = ", ".join(x for x in (street, city, region, country) if x)
    resp = client.messages.create(
        model=cfg.get("model") or os.environ.get("MAILSORT_MODEL", "claude-sonnet-4-5"), max_tokens=200,
        system=("You give the official postal code for an address. Reply with ONLY JSON: "
                '{"postcode": "", "confidence": "high|medium|low", "area": ""}. '
                'Use the postal code of the town/area named. If you do not know it, return "postcode": "". Never guess.'),
        messages=[{"role": "user", "content": addr}])
    m = re.search(r"\{.*\}", resp.content[0].text, re.S)
    if not m:
        return None
    d = json.loads(m.group(0))
    pc = _clean(d.get("postcode"))
    if not pc or d.get("confidence") == "low":
        return None
    return {"postcode": pc, "source": "AI estimate", "place": d.get("area", "")}


def lookup(street: str = "", city: str = "", region: str = "", country: str = "") -> dict:
    """-> {"postcode", "source", "place", "note"}; postcode "" when nothing reliable was found."""
    street, city, region, country = (x.strip() for x in (street or "", city or "", region or "", country or ""))
    if not (city or street) or not country:
        return {"postcode": "", "source": "", "place": "", "note": "Enter at least the city and the country."}
    key = (street.lower(), city.lower(), region.lower(), country.lower())
    if key in _CACHE:
        return _CACHE[key]
    out = None
    for fn in (_osm, _ai):
        try:
            out = fn(street, city, region, country)
        except Exception as ex:
            print(f"[postcode] {fn.__name__} failed: {ex}", flush=True)
            out = None
        if out:
            break
    if out:
        out["note"] = f"Postcode {out['postcode']} found ({out['source']}{': ' + out['place'] if out.get('place') else ''}) – please check."
    else:
        out = {"postcode": "", "source": "", "place": "", "note": "No postcode found – ask the client."}
    _CACHE[key] = out
    return out


def split_line(line: str) -> tuple[str, str, str]:
    """'Sialkot, Punjab, Pakistan' -> (city, region, country)."""
    parts = [x.strip() for x in (line or "").split(",") if x.strip()]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        words = parts[0].split()
        return (" ".join(words[:-1]), "", words[-1]) if len(words) > 1 else (parts[0], "", "")
    return parts[0], (parts[1] if len(parts) > 2 else ""), parts[-1]


def add_to_line(street: str, line: str) -> tuple[str, dict | None]:
    """W-7 line 2/3 'City, Province, Country' without a postcode -> 'City, Province 12345, Country'."""
    if not line or has_postcode(line):
        return line, None
    city, region, country = split_line(line)
    r = lookup(street, city, region, country)
    if not r["postcode"]:
        return line, r
    parts = [x.strip() for x in line.split(",") if x.strip()]
    if len(parts) >= 2:
        parts[-2] = f"{parts[-2]} {r['postcode']}"
        return ", ".join(parts), r
    return f"{line} {r['postcode']}", r
