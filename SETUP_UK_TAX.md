# UK Tax tab (v44a) – setup

## 1. Upload to GitHub (repo `usamamuchada-code/mailsort`)
1. Open the repo → **Add file → Upload files**.
2. Drag in these 4 files from the zip: `app.py`, `uktax.py`, `ConfirmationStatement-v1-3.xsd`, `SETUP_UK_TAX.md`.
3. Click the green **Commit changes**. Railway redeploys in about 2 minutes.
4. Open https://mail.startitup.global – the header should show **v44a** and a new **UK Tax** link.

## 2. Railway Variables (nothing new if UK Formations is already set up)
- `CH_PRESENTER_ID`, `CH_PRESENTER_CODE` – your presenter account (already used by UK Formations)
- `CH_API_KEY` – Companies House REST API key (already used) – loads name, dates, directors, SIC codes
- Optional: `CH_TEST_PRESENTER_ID` / `CH_TEST_PRESENTER_CODE` once Companies House gives you a test account

## 3. Using it
1. **UK Tax → Clients → Add a company / LLP** – type the company number; everything else loads from Companies House.
2. On the client page enter the **company authentication code** (needed for accounts and CS01) and the client email.
3. **+ Add filing**: Dormant / Micro / Small accounts, Confirmation statement, CT600 (companies); SA800 (partnerships/LLPs); SA100 (individuals).
4. Companies House filings: fill in → **Preview XML + official schema check** → tick approval → **Send TEST**.
   The status is checked every 10 minutes; accepted / rejected emails go to the notify email.
5. HMRC returns: enter the figures, the tab calculates the tax and due dates; move the **Stage** along and record the
   filed date + HMRC reference once you have filed it in your HMRC software.
   **Email the client the information checklist** sends a document request.

## 4. Before LIVE filing
Companies House must test and approve the software for each form type (**Accounts** and **ConfirmationStatement**) –
email xml@companieshouse.gov.uk asking for a test account and software testing for these forms. When approved, tick the
boxes in **UK Tax → Clients → Settings**. Until then the LIVE button stays locked.

The presenter ID, package reference and submission-number counter are shared with UK Formations → Settings.
