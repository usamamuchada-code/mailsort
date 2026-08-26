# MailSort – AI mail sorting for Startitup Business Address

Turns one bulk scan PDF (letters for many companies) into: one PDF per letter filed under the
right client, a manifest with each client's status and email, a draft notification email per
client, and a review page for a final human check.

## What it does, step by step

1. **Extract** – reads every page. If the page is an image (scanner output) it is OCR'd with Tesseract.
2. **Classify (AI)** – Claude reads each page and decides: which client it is addressed to, who
   sent it, what kind of letter it is (government / bank / legal / bill / marketing / other), how
   urgent it is, a one-line summary, and whether the page is a continuation of the previous letter.
3. **Group** – continuation pages are joined to their letter, so a 3-page bank statement stays one file.
4. **Match** – the addressee is matched (fuzzy, so "Ltd" vs "Limited" is fine) to `clients.csv`,
   which gives the client's email and account status.
5. **Split** – writes `letters/<Client>/<batch>_<id>_<sender>_<type>.pdf`. Letters it can't match go to `letters/_UNMATCHED/`.
6. **Notify** – writes one draft email per client into `emails/`. Each starts with `# ACTION: SEND`
   or `# ACTION: HOLD – account status is 'overdue'` so nothing goes to lapsed/cancelled accounts.
   Also writes `manifest.csv` and `review.html`.

## Running it

```bash
pip install pymupdf pytesseract pillow anthropic      # plus Tesseract OCR installed on the machine
export ANTHROPIC_API_KEY=sk-ant-...                   # from console.anthropic.com
python mailsort.py process bulk_scan.pdf --clients clients.csv --out output/
```

`clients.csv` needs these columns (export from your portal database):

```
client_id,company_name,contact_name,email,status
C001,Bluebird Consulting Ltd,Sarah Khan,sarah@bluebirdconsulting.co.uk,active
```

`status` values other than `active` cause the email to be held and the row to be flagged for review.

Try the included sample: `python make_sample.py` then run on `sample/bulk_scan.pdf` with `sample/clients.csv`.
`--classification sample/classification_ai.json` reruns the sample without an API key.

## Cost and speed (approx.)

Claude Sonnet reads a page for well under a penny; a 150-letter day is roughly £1–2 of AI usage and
a few minutes of processing, most of that OCR.

## Connecting to the portal (next step for your developer)

The tool deliberately stops at files + manifest, because the portal has no API yet. Two options:

- **Best:** add a small upload endpoint to the portal (`POST /api/clients/{id}/mail` with the PDF,
  sender, type, summary). `mailsort.py` then uploads each letter and sends the email through your
  mail provider (SendGrid, Postmark, Gmail API) in the same run. The `manifest.csv` already has every
  field needed.
- **Interim:** keep the review page as the human step – staff open `review.html`, glance at the
  flagged rows, then drag the per-client folders into the portal and send the drafts.

## Safety rails built in

- Nothing is emailed automatically yet – drafts only, until you've validated accuracy on real batches.
- Any letter with no confident client match, a non-active account, or a missing email is flagged.
- Everything is kept: `pages.json` (OCR text) and `classification.json` (AI decisions) for audit.
