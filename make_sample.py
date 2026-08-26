"""Builds sample/clients.csv and sample/bulk_scan.pdf (rendered as images so OCR is exercised)."""
import csv, io
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import fitz

root = Path(__file__).parent / "sample"
root.mkdir(exist_ok=True)

clients = [
    ("C001", "Bluebird Consulting Ltd", "Sarah Khan", "sarah@bluebirdconsulting.co.uk", "active"),
    ("C002", "Northgate Logistics Limited", "Tom Reid", "tom@northgatelogistics.com", "active"),
    ("C003", "Pixel & Pine Studio Ltd", "Amira Osei", "hello@pixelandpine.co.uk", "overdue"),
    ("C004", "Harrow Property Ventures LLP", "James Whitfield", "james@harrowpv.com", "cancelled"),
    ("C005", "Greenleaf Nutrition Ltd", "Priya Nair", "priya@greenleafnutrition.com", "active"),
]
with open(root / "clients.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["client_id", "company_name", "contact_name", "email", "status"]); w.writerows(clients)

ADDR = "71-75 Shelton Street\nCovent Garden\nLondon WC2H 9JQ"

def letter(sender, to, body_pages, ref="", date="14 August 2026"):
    pages = []
    for i, body in enumerate(body_pages):
        head = ""
        if i == 0:
            head = f"{sender}\n\n{date}\n{('Ref: ' + ref) if ref else ''}\n\n{to}\n{ADDR}\n\n"
        else:
            head = f"{sender}    Page {i+1} of {len(body_pages)}\n\n"
        pages.append(head + body)
    return pages

letters = [
    letter("HM Revenue & Customs\nPAYE and Self Assessment\nBX9 1AS", "Bluebird Consulting Ltd", [
        "Dear Sir or Madam,\n\nCorporation Tax: Notice to deliver a Company Tax Return\n\nYou must deliver a Company Tax Return for the accounting period ended 31 March 2026.\nThe deadline for filing is 31 March 2027. Penalties apply if the return is late.\n\nYours faithfully,\nHMRC"], ref="1234567890 A"),
    letter("Companies House\nCrown Way, Cardiff CF14 3UZ", "Northgate Logistics Limited", [
        "Dear Director,\n\nConfirmation statement reminder\n\nYour confirmation statement is due by 12 September 2026. You can file online at\ngov.uk/companieshouse. A late filing may result in the company being struck off.\n\nCompanies House",
        "Continuation\n\nWhat you need to check before filing:\n- Registered office address\n- Officers and PSCs\n- SIC codes\n\nThis page is intentionally a continuation of the previous notice."], ref="09876543"),
    letter("Barclays Bank UK PLC\nLeicester LE87 2BB", "Pixel & Pine Studio Ltd", [
        "Dear Ms Osei,\n\nYour business current account statement is enclosed.\n\nStatement period: 1 July 2026 to 31 July 2026\nClosing balance: GBP 4,210.55\n\nBarclays Business Banking",
        "Statement continued\n\n02 Jul  Direct Debit  British Gas         -120.00\n09 Jul  Faster Payment Client ABC     +1,500.00\n21 Jul  Card payment Adobe            -54.99\n",
        "Statement continued\n\n28 Jul  Faster Payment Client XYZ     +900.00\n31 Jul  Interest                       +0.12\n\nEnd of statement"]),
    letter("Thames Water\nPO Box 286, Swindon SN38 2RA", "Harrow Property Ventures LLP", [
        "Dear Customer,\n\nFinal reminder: Water bill overdue\n\nAmount due: GBP 312.40. Please pay within 7 days to avoid further action.\n\nThames Water"]),
    letter("Office Depot Marketing", "Greenleaf Nutrition Ltd", [
        "Dear Business Owner,\n\nSummer stationery sale - up to 40% off!\n\nBrowse our catalogue and save on printer paper, toner and office furniture.\n\nOffice Depot"]),
    letter("HM Courts & Tribunals Service\nCounty Court Business Centre\nNorthampton NN1 2LH", "Greenleaf Nutrition Limited", [
        "Dear Sir/Madam,\n\nClaim Form (CPR Part 7)\nClaim number: K7XY1234\n\nA claim has been issued against you. You have 14 days from the date of service to respond.\n\nHMCTS"]),
    letter("Royal Mail", "Sunrise Bakery Co", [
        "Dear Customer,\n\nWe tried to deliver a parcel. Please collect it from your local delivery office\nwithin 18 days.\n\nRoyal Mail"]),
]

# render each page to an image, then assemble an image-only PDF (simulates a scanner)
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 26)
except Exception:
    font = ImageFont.load_default()

pdf = fitz.open()
for L in letters:
    for text in L:
        img = Image.new("RGB", (1240, 1754), "white")
        d = ImageDraw.Draw(img)
        d.multiline_text((110, 120), text, fill="black", font=font, spacing=10)
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=70)
        page = pdf.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buf.getvalue())
pdf.save(root / "bulk_scan.pdf")
print("wrote", root / "bulk_scan.pdf", "pages:", pdf.page_count)
