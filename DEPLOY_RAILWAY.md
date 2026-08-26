# Deploying MailSort on Railway (≈ 20 minutes)

When you're done, your team opens a web address (e.g. `https://mailsort-production.up.railway.app`),
logs in, drags the scan in, and sends the emails. Nothing to install on any PC.

## 1. Put the code somewhere Railway can read it

Railway deploys from a GitHub repository. Create one (it can be **private**):

1. Go to https://github.com/new → name it `mailsort` → Private → Create.
2. Click **"uploading an existing file"** on the empty-repo page, drag in **all files from the zip**
   (`app.py`, `mailsort.py`, `Dockerfile`, `railway.json`, `requirements.txt`, `.dockerignore`, the `sample`
   folder, etc.) → Commit.

(Or use the GitHub Desktop app if you prefer.)

## 2. Create the Railway project

1. https://railway.app → Login with GitHub → **New Project → Deploy from GitHub repo** → pick `mailsort`.
2. Railway detects the `Dockerfile` and starts building (3–5 min the first time – it installs Tesseract).

## 3. Add a storage volume  (important – otherwise batches vanish on every restart)

In the project, click your service → **Volumes → Add Volume** → mount path: **`/data`**.
1 GB is plenty for months of batches; grow it later if needed.

## 4. Set the variables

Service → **Variables → Raw editor**, paste and edit:

```
MAILSORT_USER=admin
MAILSORT_PASSWORD=choose-a-strong-password
SECRET_KEY=any-long-random-string
ANTHROPIC_API_KEY=sk-ant-...
SENDER_NAME=The Startitup Team
FROM_EMAIL=mail@yourdomain.co.uk
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=mail@yourdomain.co.uk
SMTP_PASSWORD=your-16-character-google-app-password
```

- Anthropic key: https://console.anthropic.com → Billing (add e.g. £20) → API Keys → Create.
- Google App Password: Google Account → Security → 2-Step Verification (must be on) → App passwords.
  For Outlook/Microsoft 365 use `smtp.office365.com`, port 587, and your normal login.

Variables override anything typed on the app's Settings page, so keep secrets here rather than in the app.

## 5. Get the address

Service → **Settings → Networking → Generate Domain**. Open it, log in with `MAILSORT_USER` / `MAILSORT_PASSWORD`.
Later you can attach your own subdomain (e.g. `mail.startitup.co.uk`) on the same screen with one CNAME record.

## 6. First run checklist

1. **Settings** → "Send a test email to myself" → check it arrives.
2. **Client database** → upload your client CSV (`client_id, company_name, contact_name, email, status`).
3. **Batches** → drag the sample `bulk_scan.pdf` from the zip → watch it sort → preview an email.
4. Then a real scan, and compare against what your team did by hand.

## Updating the app later

Edit files in the GitHub repo (or drag-upload replacements) → Railway rebuilds and redeploys automatically.
Your batches and client list are on the volume and are untouched by redeploys.

## Cost guide

Railway: usually £5–10/month for this workload (Hobby plan). Anthropic: roughly £1–2 per day at 150 letters/day.

## Security notes

- The whole app sits behind the login; use a strong password and share it only with staff.
- Scans and client data live on the Railway volume in your own project – back it up via
  **Download all letters (zip)** / **download current CSV** if you want an offline copy.
- Only OCR'd page *text* is sent to Anthropic for classification; the PDFs themselves are not.
