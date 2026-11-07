# UK Formations tab (v41a) – setup

## 1. Upload to GitHub (repo usamamuchada-code/mailsort, main folder)
Upload these 4 files with "Add file → Upload files", then press the green "Commit changes" button:
- formations.py      (new module)
- app.py             (adds the "UK Formations" menu link, version v41a)
- requirements.txt   (adds lxml)
- ch_schemas.zip     (official Companies House XML schemas – upload the zip as it is, do NOT unzip it)

Railway redeploys automatically (about 2–3 minutes). The header should then show v41a.

## 2. Railway → mailsort service → Variables (never put these in GitHub – the repo is public)
- CH_PRESENTER_ID   = your presenter ID
- CH_PRESENTER_CODE = your presenter authentication code
- (later) CH_TEST_PRESENTER_ID / CH_TEST_PRESENTER_CODE = the test account details Companies House sends you

## 3. Mail Room → UK Formations → Settings
- Agent firm name = the firm that holds the presenter account
- Keep Mode = TEST and Package reference = 0012 until Companies House approves the software

## 4. Companies House software testing (needed before LIVE filing)
1. Apply for an XML Gateway test account (GOV.UK: "Important information for software developers – read first";
   or email xml@companieshouse.gov.uk). Ask for the form type CompanyIncorporation.
2. Send several TEST incorporations from the tab (Ltd by shares, guarantee, LLP, bespoke articles …).
3. Email Companies House with the submission numbers, then wait for their approval and the live package reference.
4. Settings: enter the live package reference, tick "Companies House has approved this software", set Mode = LIVE.
