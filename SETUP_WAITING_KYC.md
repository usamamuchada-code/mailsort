# MailSort v45a – Waiting for KYC

Letters for customers who have their address but are not in the client database yet (KYC not done).

## Upload (GitHub web, same as always)
1. Open github.com/usamamuchada-code/mailsort → **Add file → Upload files**.
2. Drag in: `app.py`, `pending.py`, `companyfile.py`, `SETUP_WAITING_KYC.md`.
3. Click the green **Commit changes**. Railway redeploys in about 2 minutes.
4. Open mail.startitup.global, press Ctrl+F5 – the header should show **v45a** and a new **Waiting for KYC** tab.

No new Railway variables are needed (it uses the existing `CH_API_KEY`). Optional:
- `PENDING_CH_LOCATION` – what to search on Companies House (default `E1 5NF`)
- `PENDING_CH_MATCH` – words the registered office must contain (default `greatorex`; several = comma separated)

## Daily routine
1. **New order** → Waiting for KYC → *Add an order* (email, order date, unit if any). No company name needed.
   Or upload a CSV export of orders from the dashboard.
2. **New companies at our address** (checked automatically every morning) → link each new company to its order.
3. **Unclaimed letters** (all batches, grouped by printed name) → *Link to order*, *New order from this*, or *Not our customer*.
   Each order page also lists letters that may belong to it (same unit / same name) – tick and Link.
4. **KYC done** on the order → company goes into the client database, linked letters are attached, every batch is
   searched → **Send all waiting letters**.

Also automatic: adding a client in Client database, or uploading the client CSV, searches ALL earlier batches for
that client. Every company file has a **Search all batches** button.

## Safety
- Nothing is emailed while an order is waiting – letters are only linked.
- Letters that arrived before the order date / client start date are never linked and are held for staff.
- No unit → near-exact name only (≥ 93%) and staff confirm.
