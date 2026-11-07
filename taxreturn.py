"""
1040-NR preparation for ITIN applications (v42a) – used by itinforms.py.

For W-7 applications that must go with a U.S. tax return (box b, c, e, f without h), staff can
prepare a Form 1040-NR here: a NIL return (no U.S. income) or, if the client's U.S. LLC had income,
a return with Schedule C (business profit), Schedule 1 and Form 8995 (20% QBI deduction) and the tax
worked out with the year's tax rates.

Templates (official IRS fill-in PDFs) live in ./tax_templates (or the repo root):
    f1040nr_<year>.pdf   f1040s1_<year>.pdf   f1040sc_<year>.pdf   f8995_<year>.pdf   f1040nrsoi_<year>.pdf
Field maps below are per tax year – a new year needs its forms AND its map.
"""
from __future__ import annotations

import io, math, re
from pathlib import Path

ROOT = Path(__file__).parent
TPL_DIRS = [ROOT / "tax_templates", ROOT]

# ----------------------------------------------------------------------------- tax year data

YEARS = {
    2025: {
        # 2025 rate schedules (Rev. Proc. 2024-40) – upper limit of each bracket
        "brackets": {
            "single": [(11925, .10), (48475, .12), (103350, .22), (197300, .24), (250525, .32), (626350, .35), (None, .37)],
            "mfs": [(11925, .10), (48475, .12), (103350, .22), (197300, .24), (250525, .32), (375800, .35), (None, .37)],
        },
        "qbi_threshold": {"single": 197300, "mfs": 197300},     # Form 8995 limit (taxable income before QBI)
    },
}
FORMS = {   # key: (label, file stem, IRS attachment sequence – order behind the 1040-NR)
    "nr": ("Form 1040-NR", "f1040nr", 0),
    "s1": ("Schedule 1 (Form 1040)", "f1040s1", 1),
    "sc": ("Schedule C (Form 1040)", "f1040sc", 9),
    "q": ("Form 8995", "f8995", 55),
    "oi": ("Schedule OI (Form 1040-NR)", "f1040nrsoi", 7),
}
STATUS = {"single": "Single", "mfs": "Married filing separately"}

EXPENSES = [   # (key, label, Schedule C line field (2025 map))
    ("advertising", "Advertising (line 8)", "f1_20[0]"),
    ("commissions", "Commissions and fees, e.g. Stripe / PayPal / Amazon fees (line 10)", "f1_22[0]"),
    ("contract", "Contract labour / freelancers (line 11)", "f1_23[0]"),
    ("legal", "Legal and professional services, e.g. accountant, registered agent (line 17)", "f1_32[0]"),
    ("office", "Office expense, software, subscriptions (line 18)", "f1_33[0]"),
    ("supplies", "Supplies (line 22)", "f1_38[0]"),
    ("taxes", "Taxes and licences, e.g. state fees (line 23)", "f1_39[0]"),
    ("travel", "Travel (line 24a)", "f1_40[0]"),
    ("other", "Other expenses (line 27a)", "f1_44[0]"),
]


def template(form: str, year: int) -> Path | None:
    stem = FORMS[form][1]
    for d in TPL_DIRS:
        for name in (f"{stem}_{year}.pdf", f"{stem}--{year}.pdf"):
            if (d / name).exists():
                return d / name
    return None


def years_available() -> list[int]:
    return sorted(y for y in YEARS if template("nr", y))


def missing_templates(year: int, with_income: bool) -> list[str]:
    need = ["nr", "oi"] + (["s1", "sc", "q"] if with_income else [])
    return [FORMS[k][0] for k in need if not template(k, year)]


# ----------------------------------------------------------------------------- tax maths

def _sched_tax(ti: float, status: str, year: int) -> float:
    tax, lower = 0.0, 0
    for upper, rate in YEARS[year]["brackets"][status]:
        top = ti if upper is None else min(ti, upper)
        if top > lower:
            tax += (top - lower) * rate
        if upper is None or ti <= upper:
            break
        lower = upper
    return tax


def income_tax(ti: int, status: str, year: int) -> int:
    """Tax on taxable income: IRS Tax Table method under $100,000 (tax on the midpoint of the row), rate schedule above."""
    if ti <= 0:
        return 0
    if ti < 100000:
        if ti < 5:
            return 0
        if ti < 15:
            mid = 10
        elif ti < 25:
            mid = 20
        elif ti < 3000:
            mid = (ti // 25) * 25 + 12.5
        else:
            mid = (ti // 50) * 50 + 25
        return int(math.floor(_sched_tax(mid, status, year) + 0.5))
    return int(math.floor(_sched_tax(ti, status, year) + 0.5))


def _n(v) -> int:
    try:
        return int(round(float(str(v or "0").replace(",", "").replace("$", "").strip() or 0)))
    except ValueError:
        return 0


def compute(t: dict) -> dict:
    """All the line amounts. t = the saved tax form (see itinforms TAX_KEYS)."""
    year, status = int(t.get("year") or 0), t.get("status") or "single"
    c = {"warnings": [], "nil": bool(t.get("nil"))}
    if t.get("nil"):
        c.update({k: 0 for k in ("gross", "returns", "cogs", "expenses", "net", "agi", "qbi_ded", "ti", "tax", "total_tax", "owe")})
        return c
    gross, returns, cogs = _n(t.get("gross")), _n(t.get("returns")), _n(t.get("cogs"))
    exp = {k: _n(t.get(f"exp_{k}")) for k, _l, _f in EXPENSES}
    total_exp = sum(exp.values())
    line3 = gross - returns
    line5 = line3 - cogs
    net = line5 - total_exp
    c.update({"gross": gross, "returns": returns, "line3": line3, "cogs": cogs, "line5": line5, "exp": exp,
              "expenses": total_exp, "net": net})
    agi = net                                        # no adjustments to income
    ti_before = max(0, agi)                          # no itemized deductions (line 12 = 0)
    qbi_ded, qbi_ok = 0, True
    if net > 0:
        if ti_before > YEARS[year]["qbi_threshold"][status]:
            qbi_ok = False
            c["warnings"].append("Taxable income is above the Form 8995 limit – the QBI deduction needs Form 8995-A "
                                 "(not prepared here). The deduction is left blank, so the tax shown is higher than it may be.")
        else:
            qbi_ded = int(min(round(net * 0.2), round(ti_before * 0.2)))
    ti = max(0, agi - qbi_ded)
    tax = income_tax(ti, status, year)
    c.update({"agi": agi, "ti_before": ti_before, "qbi_ded": qbi_ded, "qbi_ok": qbi_ok, "ti": ti, "tax": tax,
              "total_tax": tax, "owe": tax, "loss": -net if net < 0 else 0})
    if tax:
        c["warnings"].append(f"Tax due: ${tax:,}. The client must pay it to the IRS (no tax was withheld).")
    return c


# ----------------------------------------------------------------------------- PDF values

def _m(v: int) -> str:
    """Whole dollars for the form; losses in brackets."""
    return f"({abs(v):,})" if v < 0 else f"{v:,}"


def _addr(t: dict, a: dict) -> dict:
    return {"street": t.get("addr_street") or a.get("foreign_1") or a.get("mail_1") or "",
            "city": t.get("addr_city") or "", "province": t.get("addr_province") or "",
            "postal": t.get("addr_postal") or "", "country": t.get("addr_country") or a.get("country") or ""}


def nr_values(a: dict, caa: dict, t: dict, c: dict) -> dict:
    """Form 1040-NR (2025 map)."""
    first = " ".join(x for x in (a.get("first"), (a.get("middle") or "")[:1]) if x)
    ad = _addr(t, a)
    status = t.get("status") or "single"
    v = {
        "f1_14[0]": first, "f1_15[0]": a.get("last", ""), "f1_16[0]": "",      # identifying number blank – ITIN applied for (W-7 attached)
        "f1_17[0]": ad["street"], "f1_19[0]": ad["city"],
        "f1_22[0]": ad["country"], "f1_23[0]": ad["province"], "f1_24[0]": ad["postal"],
        "c1_5[0]": status == "single", "c1_5[1]": status == "mfs",
        "c1_6[0]": t.get("digital") == "yes", "c1_6[1]": t.get("digital") != "yes",
        "f2_47[0]": t.get("occupation", ""), "f2_49[0]": t.get("phone", ""), "f2_50[0]": t.get("email", ""),
        # paid preparer – our firm
        "f2_51[0]": caa.get("responsible_party", ""), "f2_52[0]": caa.get("ptin", ""),
        "f2_53[0]": caa.get("company", ""), "f2_54[0]": caa.get("phone", ""),
        "f2_55[0]": ", ".join(x for x in (caa.get("our_mail_1"), caa.get("our_mail_2")) if x), "f2_56[0]": caa.get("ein", ""),
        "c2_7[1]": True,                                 # third party designee: No
    }
    if c["nil"]:
        for k in ("f1_69[0]", "f1_71[0]", "f2_01[0]", "f2_06[0]", "f2_07[0]", "f2_09[0]", "f2_11[0]", "f2_15[0]",
                  "f2_20[0]", "f2_35[0]", "f2_42[0]"):
            v[k] = "0"
        return v
    net = c["net"]
    v.update({"f1_68[0]": _m(net), "f1_69[0]": _m(net), "f1_71[0]": _m(c["agi"]), "f2_01[0]": _m(c["agi"]),
              "f2_02[0]": "0", "f2_03[0]": _m(c["qbi_ded"]) if c["qbi_ded"] else "",
              "f2_06[0]": _m(c["qbi_ded"]), "f2_07[0]": _m(c["ti"]), "f2_09[0]": _m(c["tax"]),
              "f2_11[0]": _m(c["tax"]), "f2_15[0]": _m(c["tax"]), "f2_20[0]": _m(c["total_tax"]),
              "f2_35[0]": "0", "f2_42[0]": _m(c["owe"])})
    return v


def s1_values(a: dict, t: dict, c: dict) -> dict:
    name = " ".join(x for x in (a.get("first"), a.get("middle"), a.get("last")) if x)
    return {"f1_01[0]": name, "f1_02[0]": "", "f1_07[0]": _m(c["net"]), "f1_38[0]": _m(c["net"])}


def sc_values(a: dict, t: dict, c: dict) -> dict:
    name = " ".join(x for x in (a.get("first"), a.get("middle"), a.get("last")) if x)
    v = {"f1_1[0]": name, "f1_2[0]": "", "f1_3[0]": t.get("llc_activity", ""), "f1_4[0]": re.sub(r"\D", "", t.get("llc_naics", ""))[:6],
         "f1_5[0]": t.get("llc_name", ""), "f1_6[0]": t.get("llc_ein", ""),
         "f1_7[0]": t.get("llc_street", ""), "f1_9[0]": t.get("llc_city_state_zip", ""),
         "c1_1[0]": True,                                   # F: cash method
         "c1_2[0]": True,                                   # G: materially participated – Yes
         "f1_13[0]": _m(c["gross"]), "f1_14[0]": _m(c["returns"]) if c["returns"] else "", "f1_15[0]": _m(c["line3"]),
         "f1_16[0]": _m(c["cogs"]) if c["cogs"] else "", "f1_17[0]": _m(c["line5"]), "f1_19[0]": _m(c["line5"]),
         "f1_46[0]": _m(c["expenses"]), "f1_47[0]": _m(c["line5"] - c["expenses"]), "f1_51[0]": _m(c["net"])}
    for k, _l, fld in EXPENSES:
        if c["exp"].get(k):
            v[fld] = _m(c["exp"][k])
    if c["net"] < 0:
        v["c1_7[0]"] = True                                # 32a all investment at risk
    return v


def q_values(a: dict, t: dict, c: dict) -> dict:
    name = " ".join(x for x in (a.get("first"), a.get("middle"), a.get("last")) if x)
    net = c["net"]
    v = {"f1_01[0]": name, "f1_02[0]": "", "f1_03[0]": t.get("llc_name", ""), "f1_04[0]": t.get("llc_ein", ""),
         "f1_05[0]": _m(net), "f1_18[0]": _m(net)}
    if net > 0:
        l5 = round(net * 0.2)
        v.update({"f1_20[0]": _m(net), "f1_21[0]": _m(l5), "f1_26[0]": _m(l5), "f1_27[0]": _m(c["ti_before"]),
                  "f1_28[0]": "0", "f1_29[0]": _m(c["ti_before"]), "f1_30[0]": _m(round(c["ti_before"] * 0.2)),
                  "f1_31[0]": _m(c["qbi_ded"])})
    else:
        v.update({"f1_20[0]": "0", "f1_21[0]": "0", "f1_26[0]": "0", "f1_31[0]": "0", "f1_32[0]": f"{abs(net):,}"})
    return v


def build(a: dict, caa: dict, t: dict, fill_irs) -> tuple[bytes, dict, list[str]]:
    """-> (combined PDF: 1040-NR then attachments in IRS sequence order, computed amounts, forms included)."""
    import pymupdf
    year = int(t["year"])
    c = compute(t)
    parts = [("nr", nr_values(a, caa, t, c))]
    if not c["nil"]:
        parts += [("s1", s1_values(a, t, c)), ("sc", sc_values(a, t, c))]
        if c["qbi_ok"] and c["net"] != 0:
            parts.append(("q", q_values(a, t, c)))
    if template("oi", year):
        parts.append(("oi", oi_values(a, t)))
    parts.sort(key=lambda p: FORMS[p[0]][2])
    out = pymupdf.open()
    included = []
    for key, vals in parts:
        tp = template(key, year)
        if not tp:
            raise RuntimeError(f"{FORMS[key][0]} {year} is not installed yet (tax_templates/{FORMS[key][1]}_{year}.pdf).")
        out.insert_pdf(pymupdf.open(stream=fill_irs(tp, vals), filetype="pdf"))
        included.append(FORMS[key][0])
    buf = io.BytesIO()
    out.save(buf, garbage=3, deflate=True)
    return buf.getvalue(), c, included


def oi_values(a: dict, t: dict) -> dict:
    """Schedule OI – mapped when the IRS form for the year is installed (no 2025 map yet)."""
    raise RuntimeError("Schedule OI field map not added yet – send the Schedule OI PDF for this year.")
