# ISD Permit Inspection Automation (prototype)

Boston University DS539 capstone for the City of Boston **Inspectional Services Department (ISD)**.

Every morning Microsoft Power BI emails ISD three Excel reports of outstanding permit inspection requests (Building, Electric, Plumbing/Gas). Staff currently download, clean and analyze them by hand. This pipeline does it automatically:

1. **Ingestion & storage**: read the emails, parse the Excel files, store raw files and standardized daily snapshots in Google Drive
2. **Duplicate detection**: collapse the daily snapshots into one current row per request, with a persistent ID (`ISD-000123`)
3. **Late logic**: decide whether each request was handled on time under ISD's rules (business days, holidays, inspector notes) and compute KPIs
4. **Dashboard data**: Power BI-ready CSVs, plus a local demo app for the four client demos

Everything runs end to end in **mock mode** (a local fake inbox and a fake Drive folder, synthetic data, no credentials). Live Gmail and Drive adapters drop in once OAuth is set up: see [docs/LIVE_SETUP.md](docs/LIVE_SETUP.md).

---

## Quick start (mock mode)

```bash
python3.11 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest                                                     # 82 tests, ~20 s
streamlit run demo/app.py                                  # sidebar: "Preload history", then Demo 1-4
```
Terminal fallback for the demo: `python demo/demo_script.py`.

Pipeline without the UI:
```bash
python demo/generate_synthetic.py --deliver-all            # put all 12 scripted days in the mock inbox
python -m isd_pipeline.cli backfill --start 2026-10-05 --end 2026-10-20
python -m isd_pipeline.cli status
```

## Presenting the demo (~10 minutes)
1. Sidebar: **Reset demo**, then **Preload history**. This silently processes 10/05–10/16, so late statuses already exist.
2. **Deliver next day's emails** (Mon 10/19). In **Demo 1**, show the Gmail-style inbox and click **Run Pipeline**. The steps appear live, followed by the per-file extraction details.
3. **Demo 2**: show the Drive tree (NEW badges), the before/after counts and the ingestion-log entry. Click **Deliver 10/20 → Run**: the Electric email is missing that day, so the run is PARTIAL_SUCCESS. Then click **Run the same day again** to show "0 new files, 0 new IDs".
4. **Demo 3**: the requests table with filters and badges. The spotlight follows `ALT1045821`: 4 snapshot rows become 1 ID, the notes change, and red font says late while our logic says *on time (contacted)*.
5. **Demo 4**: KPI cards, charts, abnormal records and data provenance. The values changed after the 10/20 run.

The **MOCK / LIVE** badge is always visible.

---

## Architecture

```
isd_pipeline/
  config/                 settings.yaml, email_rules.yaml, report_schema.yaml, holidays.csv, sla.csv, note_placeholders.csv
  settings.py  schema.py  typed config + the shared data contract (docs/SCHEMA.md) as code
  factory.py              picks mock or live adapters from settings.mode
  notes.py                is_meaningful_note(), shared by Modules 2 and 3
  ingestion/
    email_base.py         EmailSource interface + find_daily_reports() (sender/subject/day/duplicate rules)
    mock_gmail_adapter.py gmail_adapter.py
    excel_parser.py       header detection, primary block, justified fill-down, red font, validation
    storage_base.py       StorageBackend interface + DataFrame helpers
    mock_gdrive_adapter.py gdrive_adapter.py google_errors.py
    raw_reports.py        raw Excel files (idempotent)
    snapshots.py          historical_snapshots/, load_snapshots(), list_report_dates()
    manifest.py           manifest.json + load_latest_dataset()
    ingestion_log.py      pipeline_logs/*.csv
  dedupe/                 matching.py (keys) · registry.py (IDs) · update.py (merge rules)
  late_logic/             calendar.py · rules.py · aggregate.py   (pure functions, no I/O, no clock)
  dashboard/              exports.py (Power BI CSVs) · sheets_export.py (optional stub)
  pipeline.py             run_daily_pipeline()
  api.py                  the public API in section 4.5
  cli.py  auth.py
demo/  generate_synthetic.py · demo_state.py · app.py · demo_script.py
tests/ docs/ secrets/ (gitignored)
```

**Differences from the suggested layout, and why:**
- `schema.py`: one place that defines column names and types and handles the CSV round trip. Without it, `ap_number`/`ward` would be re-cast in four places, and leading zeros would eventually be lost.
- `notes.py`: the spec requires Modules 2 and 3 to share `is_meaningful_note`, so it lives in neither.
- `manifest.py`, `raw_reports.py`, `google_errors.py`: small modules split out of the storage code so each stays readable.
- `demo/demo_state.py`: the app and the terminal script share one controller, so the two demos can't drift apart.

### Data flow of one run
`fetch_daily_reports` → (per division) download → `store_raw_report` → `process_excel_report` → `save_snapshot` → `update_requests` (Module 2) → `classify_requests` + `aggregate` (Module 3) → versioned datasets in `processed_data/` → `dashboard_exports/` → **manifest (last)** → logs.

### Safety guarantees
- **Idempotent.** Attachments are tracked by email id + attachment id + SHA-256. Raw files and snapshots are looked up before upload. Module 2 treats seen dates as a set. Re-running any day creates 0 files and 0 IDs.
- **Safe writes.** Datasets are new versioned files, and `manifest.json` is updated last. A run that fails, or that has only an invalid file, leaves the manifest and the datasets it points to untouched.
- **Per-division isolation.** A missing or invalid report or an API error affects only its own division, and the run returns PARTIAL_SUCCESS naming it.
- **Nothing silently dropped.** Bad rows are kept, flagged and logged. Data-quality problems are flagged, never deleted.

## Running each module alone
```python
from datetime import date
from isd_pipeline.api import fetch_daily_reports, process_excel_report, save_to_google_drive, load_latest_dataset, run_daily_pipeline

atts = fetch_daily_reports(date(2026, 10, 19))                         # Module 1a: email
df, vr = process_excel_report("file.xlsx", "Building", date(2026, 10, 19))  # Module 1b: one Excel file
print(vr.header_row, vr.filled_inspector_cells, vr.invalid_rows)

from isd_pipeline.dedupe.update import update_requests                 # Module 2 (pure)
res = update_requests(current_df, registry_df, df, batch_id="B1")

from isd_pipeline.late_logic.rules import classify_requests, default_config   # Module 3 (pure)
from isd_pipeline.late_logic.aggregate import aggregate
cls = classify_requests(res.requests_current, {"Building": [date(2026, 10, 19)]}, config=default_config())
tables = aggregate(cls)

load_latest_dataset("requests_classified")                             # read back through the manifest
run_daily_pipeline(date(2026, 10, 19))                                 # everything
```
CLI: `python -m isd_pipeline.cli {run|backfill|check-gmail|check-drive|status|schedule} --help`.

## Scheduling
`python -m isd_pipeline.cli schedule` prints a ready-to-paste cron line (macOS/Linux) and a Windows Task Scheduler command. Both run weekdays at `schedule.run_time` (default 07:45 America/New_York). For a late delivery, re-run manually: `python -m isd_pipeline.cli run --date YYYY-MM-DD`. Exit codes: 0 success, 2 partial, 1 failure.

## Tests
`pytest` runs everything in mock mode:
- ingestion I1–I5, duplicate emails, API errors and retries, crash recovery
- parser: headers, fill-down, red font, leading zeros
- dedupe D1–D5, blank-overwrite and late-arriving-report rules
- late logic: the due-date table, every status example, per-division as_of, aggregation on 10 hand-built requests, determinism and clock independence
- live adapters against fake Google services
- end to end: all 12 days, compared with `tests/expected/e2e_kpis.json` and every scripted scenario

If you change a rule or the generator **on purpose**, run `UPDATE_SNAPSHOT=1 pytest tests/test_end_to_end.py` and review the JSON diff.

## Synthetic storyline (`demo/generate_synthetic.py`, seed 539)
12 report days, 10/05–10/20/2026, including Mon 10/12, Indigenous Peoples' Day. Power BI still sends that day's email. Each day has 36–53 rows per division. The three layouts differ the way real exports do:
- **Building**: title rows, inspector cells **merged**.
- **Electric**: inspector **group-header rows** with blank cells beneath them.
- **Plumbing/Gas**: different header names and text dates, in a **flat** layout.

Scripted cases (full list in `demo_data/scripted_inbox/storyline.json`):
- The spotlight request: 4 reports, changing notes, the address spelled two ways.
- The same Ap # with a different insp_type or call time.
- Red-but-on-time requests over the holiday weekend.
- A late request that was never red.
- An open-overdue backlog.
- A missing Ap # (review queue) and an address conflict.
- A side-by-side duplicate table and a theme-coloured (ambiguous) font.
- A duplicate Building email on 10/08.
- A **malformed Plumbing/Gas file on 10/15** and **no Electric email on 10/20**.

The final on-time rate is 56.1%, close to the client's 50–57% estimate.

---

## Assumptions and questions for the client

**Open questions (sent to ISD):**
1. Does "on time" mean the inspector **called** by the deadline, or that the inspection was **completed** by then?
2. Does **any** meaningful inspector note excuse lateness, or only certain kinds (e.g. "spoke with contractor")?
3. Should the due date use the call **timestamp** (a true 48-hour window) rather than business days by date?
4. Is the day after Thanksgiving (or any other day) also an ISD closure? The **2027 holidays must be added before January 2027**. Until then, runs that touch 2027 stop with an error naming the year (or flag rows with `--lenient`).
5. Is 2 business days the right SLA for every inspection type and division? (`config/sla.csv` supports per-type values.)
6. Are the reports **morning snapshots**? The rule `last_seen <= due_date ⇒ on time` depends on it.
7. Is there a "Work Group" column in the real files, or is the division the work group?
8. Does Power BI send the emails on holidays and weekends? The synthetic data assumes holidays yes, weekends no. Weekend reports would simply be processed too.
9. When reports are missing for some days, may requests that closed during the gap (and whose due date fell inside it) be left out of the on-time rate as `unknown_close`? See the rule change below.
10. Requests that never get an inspector (Power BI lists them in an unnamed group at the top) and disappear after one report: are they cancellations? They currently count toward the on-time rate and appear as **"(no inspector assigned)"** in the by-inspector table.

**Implementation decisions** (spec conflicts or ambiguities; no business rule was changed):
- **Rows missing a key field.** §4.2 says to log them as invalid rows; §5.1 and the storyline send them to the review queue. They are **both** logged in `invalid_rows.csv` **and** kept, flagged, with their own ID, so they reach the review queue. A missing call date then gets status `invalid`.
- **"A rerun creates zero new files" vs "each dataset is a new file".** Datasets are versioned only when a run has new data. A run with no new attachments is a no-op, apart from in-place log updates.
- **Gmail's `attachmentId` changes on every API call**, so the idempotency key uses the MIME **partId** together with the SHA-256 of the content.
- **The sender address isn't known yet.** Mock mode checks the display name "Microsoft Power BI". Live mode refuses to run until the real `From:` address is configured.
- **`UPDATED` mark.** It means a descriptive field changed (notes, contact, inspector, ward, work group, fulfilled_late, red flag). Requests not on this run's reports are `UNCHANGED`, and the `NEW`/`FLAGGED` counts refer to this run.
- **Conflicts.** A request with the same Ap #, insp_type, work group and call time but a different address is flagged and gets its own ID. The *original* request is not flagged.
- **Fill-down is "justified"** only for merged inspector cells or an inspector-only group-header row, and it stops at a blank or total row. A blank inspector in a flat table stays blank and is logged.
- **Ambiguous font colours.** Theme accent colours (theme 4–9) and system indexed colours count as not red and are logged. Theme 0–3 (default text/background) are simply not red.
- **Invalid files.** The raw Excel file is still stored in `raw_reports/` for audit, but no snapshot is created. That day then counts as having no report for the division, which drives per-division `as_of` and `report_gap`.
- **`classify_requests(as_of=...)`** caps each division's latest report date at `as_of` and drops requests first seen after it.
- **`business_days_open`** is counted up to `min(last_seen, as_of)`.
- **Two snapshots for the same day and division** (a corrected re-send): the latest one is used for counts and as-of dates. Both are kept.
- **`id_registry`** is versioned (`id_registry__<batch>.csv`) like the other datasets. The manifest points to the current one.

**Rule change (approved by the team on 2026-10-30, pending client confirmation, question 9):**
- **Requests that close during a report gap.** The spec's `on_time` rule (`closed AND last_seen <= due_date`) assumes a report every business day. When a division has no report on some business days after a request's last sighting, the request really closed on an unknown day in that gap. Seen in live data: with only the 10/16 and 10/30 reports processed, requests due 10/19 that vanished in between all counted as on time.
  - Every such request gets the data-quality flag `closed_in_report_gap`. The column `last_seen_latest_possible` holds the last day with no report before the next one.
  - `on_time` now requires `last_seen_latest_possible <= due_date`. If the request was last seen on or before the due date but the gap runs past it, the outcome is unknown: status `unknown_close`, **not judged** (left out of rates like `open_not_due` and `invalid`). An inspector note on or before the due date still gives `on_time_contacted`, and a request last seen after its due date is still `late`.
  - With a report every business day nothing changes. The fix for a gap is to backfill the missing days (`cli backfill`), after which the statuses resolve.

## Known limitations
- `drive.file` scope: the app sees only the folders it created. Share the app-created `Permit Inspection Automation` folder with the team in the Drive UI.
- External OAuth apps in Testing need re-authorization about every 7 days (see LIVE_SETUP).
- The scheduled job needs the computer to be on. There is no cloud hosting, by design.
- The red-font flag is read from the Call Date cell only (configurable via `red_font_column` in `report_schema.yaml`).
