"""Module 4: Power BI-ready CSVs in dashboard_exports/ (stable file names, refreshed each run).

Every export row carries:
* ingestion_batch_id   - request-level files: the batch that last updated the request;
                         aggregate files: the batch that produced this refresh
* export_batch_id      - the batch that wrote this file
* export_source_files  - ';'-joined source Excel file names (with email ids) of that batch
so every number can be traced back to the imported emails.

contact_name / contact_info are left out unless exports.include_contact_fields is true.
"""
from __future__ import annotations

import pandas as pd

from isd_pipeline.ingestion.storage_base import StorageBackend, StoredFile, save_dataframe
from isd_pipeline.schema import CONTACT_FIELDS

EXPORT_FOLDER = "dashboard_exports"

REVIEW_COLUMNS = ["inspection_request_id", "review_reason", "dedupe_mark", "division", "ap_number", "call_datetime",
                  "address", "insp_type", "inspector", "first_seen_report_date", "last_seen_report_date",
                  "source_filename", "source_email_id", "source_row_number", "ingestion_batch_id"]
MISMATCH_COLUMNS = ["inspection_request_id", "division", "inspector", "ap_number", "insp_type", "call_date",
                    "due_date", "last_seen_report_date", "first_note_date", "ever_red", "status", "is_late",
                    "mismatch_reason", "business_days_late", "source_filename", "source_email_id", "ingestion_batch_id"]
PROBLEM_STATUSES = ("MISSING", "INVALID", "ERROR", "NO_ATTACHMENT", "NOT_EXCEL", "DUPLICATE_SAME_DAY",
                    "UNEXPECTED_SENDER")


def _stamp(df: pd.DataFrame, batch_id: str, source_files: str, *, set_batch: bool) -> pd.DataFrame:
    out = df.copy()
    if set_batch or "ingestion_batch_id" not in out.columns:
        out["ingestion_batch_id"] = batch_id
    out["export_batch_id"] = batch_id
    out["export_source_files"] = source_files
    return out


def data_quality_table(classified: pd.DataFrame, invalid_rows: pd.DataFrame, ingestion_log: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for r in classified[classified["dq_flags"] != ""].itertuples(index=False):
        for flag in r.dq_flags.split(";"):
            rows.append({"issue_type": "dq_flag", "issue": flag, "detail": r.review_reason if flag == "review_flag_upstream" else "",
                         "inspection_request_id": r.inspection_request_id, "division": r.division,
                         "report_date": pd.Timestamp(r.last_seen_report_date).strftime("%Y-%m-%d"),
                         "source_filename": r.source_filename, "source_email_id": r.source_email_id,
                         "source_row_number": str(r.source_row_number), "ingestion_batch_id": r.ingestion_batch_id})
    inv = invalid_rows.drop_duplicates(["report_date", "division", "source_email_id", "source_row_number", "reason"])
    for r in inv.itertuples(index=False):
        rows.append({"issue_type": "invalid_row", "issue": r.reason, "detail": r.values, "inspection_request_id": "",
                     "division": r.division, "report_date": r.report_date, "source_filename": r.source_filename,
                     "source_email_id": r.source_email_id, "source_row_number": r.source_row_number,
                     "ingestion_batch_id": r.ingestion_batch_id})
    log = ingestion_log[ingestion_log["status"].isin(PROBLEM_STATUSES)]
    log = log.drop_duplicates(["report_date", "division", "status", "email_id"], keep="last")
    for r in log.itertuples(index=False):
        rows.append({"issue_type": "report_problem", "issue": r.status, "detail": r.message, "inspection_request_id": "",
                     "division": r.division, "report_date": r.report_date, "source_filename": r.filename,
                     "source_email_id": r.email_id, "source_row_number": "", "ingestion_batch_id": r.ingestion_batch_id})
    cols = ["issue_type", "issue", "detail", "inspection_request_id", "division", "report_date", "source_filename",
            "source_email_id", "source_row_number", "ingestion_batch_id"]
    return pd.DataFrame(rows, columns=cols)


def build_exports(
    classified: pd.DataFrame,
    tables: dict[str, pd.DataFrame],
    *,
    batch_id: str,
    source_files: list[str],
    run_history: pd.DataFrame,
    invalid_rows: pd.DataFrame,
    ingestion_log: pd.DataFrame,
    include_contacts: bool = False,
) -> dict[str, pd.DataFrame]:
    """Pure: build every export DataFrame (file name -> frame)."""
    src = ";".join(source_files)
    req = classified.copy()
    if not include_contacts:
        req = req.drop(columns=[c for c in CONTACT_FIELDS if c in req.columns])
    out: dict[str, pd.DataFrame] = {"requests_classified.csv": _stamp(req, batch_id, src, set_batch=False)}
    out["on_time_summary.csv"] = _stamp(tables["on_time_summary"], batch_id, src, set_batch=True)
    for name, t in tables.items():
        if name.startswith("on_time_by_"):
            out[f"{name}.csv"] = _stamp(t, batch_id, src, set_batch=True)
    out["backlog.csv"] = _stamp(tables["backlog"], batch_id, src, set_batch=True)
    judged = classified[classified["is_judged"]]
    mm = judged[judged["font_mismatch"].fillna(False).astype(bool)]
    out["font_mismatches.csv"] = _stamp(mm[MISMATCH_COLUMNS], batch_id, src, set_batch=False)
    out["font_mismatch_summary.csv"] = _stamp(
        pd.concat([tables["mismatch_by_reason"], tables["red_vs_logic"]], ignore_index=True), batch_id, src, set_batch=True)
    out["data_quality.csv"] = _stamp(data_quality_table(classified, invalid_rows, ingestion_log), batch_id, src, set_batch=False)
    out["review_queue.csv"] = _stamp(classified[classified["review_flag"]][REVIEW_COLUMNS], batch_id, src, set_batch=False)
    out["run_history.csv"] = _stamp(run_history, batch_id, src, set_batch=False)
    return out


def write_exports(storage: StorageBackend, exports: dict[str, pd.DataFrame]) -> dict[str, StoredFile]:
    """Write/refresh each export in place (stable names for Power BI)."""
    return {name: save_dataframe(storage, EXPORT_FOLDER, name, df, None, {"kind": "export"}, versioned=False)
            for name, df in exports.items()}
