# Live setup: Gmail (BU account) and Google Drive

This takes about 20 minutes, once. Until it's done, everything works in **mock mode**.

The app asks Google for two permissions only:
- **`gmail.readonly`**: read emails and attachments. It cannot send, delete or change anything.
- **`drive.file`**: the app can see and change **only the files and folders it created itself**. It cannot see the rest of anyone's Drive. One consequence: the app creates the `Permit Inspection Automation` folder itself. A folder with the same name made by hand in Drive is invisible to it.

---

### 1. Find the real sender and subjects
In Gmail, open one of the Power BI emails → **⋮** (More) → **Show original**. Copy:
- the exact address in the `From:` line (for example `something@microsoft.com`; **don't guess it**)
- the exact `Subject:` lines

Put them in `isd_pipeline/config/email_rules.yaml`:
```yaml
sender:
  display_name: "Microsoft Power BI"
  address: "the-address-you-copied@..."
subjects:
  "Permit Inspection Request - Building Summary": Building
  ...
```
Live mode refuses to run while `sender.address` is `null`.

### 2. (Recommended) Add a Gmail label
Gmail → search box → **Show search options**. From: the sender above. Subject: `Permit Inspection Request` → **Create filter** → **Apply the label** → new label `ISD-Reports`.
Then set `label: ISD-Reports` in `email_rules.yaml`. Searches become more precise and faster.

### 3. Create a Google Cloud project
Go to <https://console.cloud.google.com>, preferably signed in with the **BU account**. Project picker → **New project**, named e.g. `isd-permit-pipeline`.

### 4. Enable the APIs
**APIs & Services → Library**. Enable **Gmail API**, then enable **Google Drive API**.

### 5. Configure the OAuth consent screen
(In newer consoles this is labeled **Google Auth Platform**.)
- If the project belongs to the BU organization, choose **Internal**. Only BU accounts can sign in, and Google doesn't need to verify the app.
- Otherwise choose **External**, leave it in **Testing**, and add the BU address under **Test users**.
  ⚠️ Google expires logins for External apps in Testing after about **7 days**, so live mode will need step 7 again every week. That is fine for the prototype.
- **Scopes** (Data access): add exactly these two and nothing broader:
  - `https://www.googleapis.com/auth/gmail.readonly`
  - `https://www.googleapis.com/auth/drive.file`

### 6. Create the credentials
**Credentials → Create credentials → OAuth client ID →** Application type **Desktop app** → Create → **Download JSON**.
Save it as `secrets/credentials.json`. The `secrets/` folder is in `.gitignore`. **Never commit `credentials.json` or `token.json`.**

### 7. Authorize once
```bash
python -m isd_pipeline.auth            # opens a browser
python -m isd_pipeline.auth --no-browser   # prints a URL instead
```
Sign in with the **BU account** and approve both permissions. The login is saved to `secrets/token.json` and refreshed automatically.

### 8. Dry-run checks
```bash
python -m isd_pipeline.cli check-gmail --date 2026-10-19 --live   # lists matched emails; downloads nothing
python -m isd_pipeline.cli check-drive --live                      # creates or verifies the folder tree
```
`check-gmail` shows, for each division, the email id, received time, sender and attachment. It also lists ignored duplicates and emails from an unexpected sender.

### 9. Switch to live
Set `mode: live` in `isd_pipeline/config/settings.yaml`, or add `--live` to each command:
```bash
python -m isd_pipeline.cli run --date 2026-10-19 --live
python -m isd_pipeline.cli backfill --start 2026-10-01 --end 2026-10-19 --live
python -m isd_pipeline.cli reclassify --live    # after changing sla.csv, holidays.csv or a late-logic rule
```
`reclassify` reads no email. It re-runs the late logic and the dashboard exports on the data already in Drive.

**Fill gaps with a backfill.** A missed day leaves requests that closed during the gap with an unknown outcome (`unknown_close`, left out of rates). Backfilling the missing days resolves them.

**Tests never touch live Google.** `pytest` always uses a mock copy of the config and refuses to load `secrets/`, even when `mode: live` is set.
The demo app (`streamlit run demo/app.py`) also shows a **LIVE** badge when `mode: live`.

### 10. Schedule it
```bash
python -m isd_pipeline.cli schedule     # prints ready-to-paste cron and Windows Task Scheduler lines
```
The default run time is 07:45 America/New_York (`schedule.run_time`). The emails arrive around 07:03. Each run searches the whole report day, so a late email is picked up by a later or manual run. **The computer must be on** at the scheduled time.

### 11. If BU blocks access
If you see "Access blocked: your administrator has restricted this app", or you can't create a Cloud project with the BU account:
- Ask **BU IT** whether internal Gmail API projects are allowed for students.
- Until then, keep presenting in **mock mode**. Everything except the two adapters is identical.
- **Do not** forward the client's emails to a personal account without the client's **and** the course's permission. The files contain contact names and phone numbers.

---

## Troubleshooting (messages the app prints)
| message | fix |
|---|---|
| `Google OAuth client file not found` | Do steps 3–6 |
| `No saved login ... Run once: python -m isd_pipeline.auth` | Do step 7 |
| `The saved Google login has expired or was revoked` | Run step 7 again (weekly for External + Testing apps) |
| `the saved login lacks the needed scope` | Delete `secrets/token.json`, run step 7 and tick every permission |
| `Gmail API is not enabled for this Google Cloud project` | Do step 4 |
| `Live mode needs the real sender address` | Do step 1 |
| `ignored email ... from unexpected sender` (warning) | The `From:` address differs from the config. Check step 1 |
