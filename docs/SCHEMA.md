# Data contract (shared schema)

The code version of this contract is [`isd_pipeline/schema.py`](../isd_pipeline/schema.py). Every module reads and
writes through `coerce()` and the CSV helpers there, so types survive the round trip through Drive.

**Conventions**
- Text columns use `""` for blank (never NaN). `ap_number` and `ward` are **always text**, so leading zeros survive.
- `date` columns are stored as `YYYY-MM-DD`. `datetime` columns are stored as `YYYY-MM-DD HH:MM:SS` in America/New_York wall-clock time.
- `ingested_at` is UTC (`...Z`).
- Booleans are stored as `True`/`False`. Lists are `;`-joined text.
- Everything in Drive is CSV (Power BI-friendly). The KPI tables are one JSON bundle per run.

## 3a. Snapshot rows: Module 1 output (`historical_snapshots/`)
One row per source row per report. File name: `YYYY-MM-DD_<division>_snapshot.csv`.

| column | type | notes |
|---|---|---|
| inspector | str | after *justified* fill-down only (merged cells, or a group header row above). Never inferred otherwise |
| ward | str | kept as text ("07") |
| call_datetime | datetime | full timestamp, seconds kept |
| ap_number | str | never cast to a number |
| address | str | original text, exactly as written |
| insp_type | str | |
| work_group | str | "" when the report has no Work Group column (current reports) |
| contact_name, contact_info | str | kept as written; left out of dashboard exports by default |
| request_notes, inspector_notes | str | original text, kept verbatim |
| fulfilled_late | str | the source "FullFilled Late" value, if the column exists |
| is_red | bool | font colour of the **Call Date** cell. The cells checked are recorded in `ingestion_log.red_cells_checked` |
| division | str | Building / Electric / Plumbing/Gas, **from the email subject** |
| report_date | date | the email's received date in America/New_York |
| source_email_id, source_attachment_id, source_filename | str | `source_attachment_id` is Gmail's stable MIME partId |
| ingested_at | datetime (UTC) | |
| source_row_number | int | worksheet row number |
| ingestion_batch_id | str | one per pipeline run, e.g. `B20261019-114501-a3f9` |
| **row_issues** | str | *extension.* `;`-joined row problems (`missing_ap_number`, `unparseable_call_datetime: '...'`, ...). The row is kept **and** logged in `pipeline_logs/invalid_rows.csv` |

## 3b. Current requests: Module 2 output (`processed_data/requests_current__<batch>.csv`)
One row per unique request. It carries the descriptive fields of 3a (latest non-blank values; `call_datetime`, `ap_number`, `address` and `insp_type` stay as first seen), but **not** `report_date` (see `last_seen_report_date`), plus:

| column | type | notes |
|---|---|---|
| inspection_request_id | str | persistent, `ISD-000123` |
| match_key | str | `call_datetime|AP|ADDRESS|INSP TYPE|WORK GROUP` (normalized), or `UNMATCHED|...` for rows missing a key field |
| first_seen_report_date, last_seen_report_date | date | |
| seen_report_dates | str | `;`-joined distinct report dates (keeps reruns idempotent) |
| n_reports | int | number of distinct seen dates |
| first_note_date | date/null | first report date with a meaningful inspector note (`is_meaningful_note`) |
| ever_red | bool | red on any report |
| dedupe_mark | str | for the latest run: `NEW`, `UPDATED` (a descriptive field changed: notes, contact, inspector, ward, work group, fulfilled_late, is_red), `UNCHANGED` (no change, or not on this run's reports), or `FLAGGED` |
| review_flag | bool | missing key field or conflict; never auto-merged |
| review_reason | str | |

**Work group used in the key:** `work_group` when the report has a non-blank value, otherwise `division`. No current report has a Work Group column, so the division is used.

`processed_data/id_registry__<batch>.csv`: `match_key`, `inspection_request_id`, `created_batch_id`, `first_seen_report_date`.

## 3c. Classified requests: Module 3 output (`processed_data/requests_classified__<batch>.csv`)
3b plus:

| column | type | notes |
|---|---|---|
| call_date | date | date part of call_datetime |
| sla_business_days | int | from `config/sla.csv` (*extension*, shown for transparency) |
| due_date | date | `busday_offset(call_date, sla, roll="forward")` with the holiday calendar |
| as_of | date | latest valid report date **for this request's division** |
| is_open | bool | `last_seen_report_date >= as_of` |
| last_seen_latest_possible | date | *extension.* For a closed request: the last business day with **no** division report before the next report (the request may still have been open then); equals `last_seen_report_date` when there is no gap |
| status | str | `open_not_due`, `on_time`, `on_time_contacted`, `open_overdue`, `late`, `unknown_close`, `invalid` |
| is_late | bool | status in {late, open_overdue} |
| is_judged | bool | status not in {open_not_due, unknown_close, invalid} |
| business_days_late | int | business days in (due_date, end] (see README) |
| business_days_open | int | business days in (call_date, last_seen] |
| font_mismatch | bool/null | `ever_red != is_late` for judged requests, null otherwise |
| mismatch_reason | str | `excused_by_note`, `calendar_adjustment`, `missed_by_source`, `other_red_not_late` |
| dq_flags | str | `;`-joined: `missing_call_date`, `call_after_first_seen`, `first_note_outside_seen_range`, `report_gap`, `closed_in_report_gap`, `long_open`, `review_flag_upstream`, `holiday_year_missing` |

## KPI bundle (`processed_data/kpis__<batch>.json`)
A dictionary of tables: `on_time_summary`, `on_time_by_{division, call_month, call_week, insp_type, inspector, ward, inspector_month}`, `backlog`, `mismatch_by_reason`, `red_vs_logic`, `dq_summary`.
Every rate table includes `on_time_pct`, `numerator`, `denominator`, `low_n`, `total_requests`, `judged` and every status count.

## Dashboard exports (`dashboard_exports/`, stable names, refreshed in place)
`requests_classified.csv`, `on_time_summary.csv`, `on_time_by_<dimension>.csv`, `backlog.csv`, `font_mismatches.csv`,
`font_mismatch_summary.csv`, `data_quality.csv`, `review_queue.csv`, `run_history.csv`.
Every row has `ingestion_batch_id`. On request-level rows it is the batch that last updated the request; on aggregate rows it is the batch of this refresh. Every row also has `export_batch_id` and `export_source_files` (the batch's source file names and email ids).
`contact_name` and `contact_info` are excluded unless `exports.include_contact_fields: true`.

`data_quality.csv` (long format): `issue_type` ∈ {`dq_flag`, `invalid_row`, `report_problem`}, `issue`, `detail`,
`inspection_request_id`, `division`, `report_date`, `source_filename`, `source_email_id`, `source_row_number`.

## Pipeline logs (`pipeline_logs/`, updated in place)
| file | one row per |
|---|---|
| `manifest.json` | the pointer to the latest successful datasets, plus this refresh's sources (email ids, received times, Drive file ids) |
| `processed_attachments.csv` | processed attachment: `email_id`, `attachment_id`, `sha256`, `status` (the idempotency registry) |
| `ingestion_log.csv` | division (or ignored email) per run: status, header row, rows, fill-downs, red cells checked, file ids |
| `invalid_rows.csv` | row-level parser problem, with the raw cell values |
| `run_history.csv` | pipeline run |

## Drive `appProperties`
- Raw report: `kind=raw_report`, `email_id`, `attachment_id`, `sha256`, `division`, `report_date`, `received_at`.
- Snapshot: `kind=snapshot`, `report_date`, `division`, `email_id`, `attachment_id`, `source_sha256`, `row_count`, `batch_id`.
- Dataset: `kind=dataset`, `dataset`, `batch_id`.
