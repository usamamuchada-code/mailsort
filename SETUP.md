# MailSort – setting it up on your office PC (about 15 minutes)

## 1. Install the two programs it needs

**Python 3.10 or newer** – https://www.python.org/downloads/
On Windows, tick **"Add python.exe to PATH"** on the first screen of the installer.

**Tesseract OCR** (reads the scanned pages)
- Windows: https://github.com/UB-Mannheim/tesseract/wiki → download the installer, keep the default folder
  (`C:\Program Files\Tesseract-OCR`). If MailSort later says it can't find Tesseract, add that folder to PATH.
- Mac: `brew install tesseract`
- Linux: `sudo apt install tesseract-ocr`

## 2. Unzip MailSort and install the Python libraries

Unzip the folder somewhere permanent, e.g. `C:\MailSort`. Open a terminal / Command Prompt in that folder and run:

```
pip install -r requirements.txt
```

## 3. Start it

Double-click `start_windows.bat` (or run `./start_mac_linux.sh`). A window stays open while it runs.
Open **http://localhost:5000** in your browser. To stop, close the window.

## 4. First-time settings (Settings page)

- **Anthropic API key** – create one at https://console.anthropic.com (Billing → add credit, then API Keys).
  Without it the app still splits and matches, but uses a rough matcher and gives no summaries.
- **Email** – for Gmail / Google Workspace: host `smtp.gmail.com`, port `587`, username = your address,
  password = an **App Password** (Google Account → Security → 2‑Step Verification → App passwords).
  Click **"Send a test email to myself"** to confirm it works.

## 5. Daily use

1. **Client database** page → upload your client CSV whenever it changes (it replaces the list), or edit one
   client with the small form. Columns: `client_id, company_name, contact_name, email, status`.
2. **Batches** page → drag the bulk scan PDF in → **Upload & sort**. The file stays on your PC; only the page
   text goes to the AI. Progress shows on screen; a 150-letter scan takes a few minutes.
3. Open the batch: check the orange rows (no match / fuzzy match / non-active account), open any PDF, then
   **Preview** and **Send** per client, or **Send all active & unsent**. Emails carry the letter PDFs as
   attachments (switchable in Settings). Non-active accounts are never included in "send all".
4. **Download all letters (zip)** gives the per-client folders for uploading to the portal until it has an API.

Everything is stored in the `data` folder next to the app – back it up like any other business data.

## Access from other PCs in the office

In `app.py`, change the last line's `host="127.0.0.1"` to `host="0.0.0.0"`, then staff can open
`http://<this-pc-name>:5000`. Only do this on a trusted office network – there is no login screen.
