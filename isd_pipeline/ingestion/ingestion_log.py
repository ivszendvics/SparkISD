"""Append-only CSV logs in pipeline_logs/ (each one file, updated in place).

* ingestion_log.csv          one row per division (or ignored email) per run
* processed_attachments.csv  the idempotency registry: email_id + attachment_id + sha256
* invalid_rows.csv           row-level parser problems (the row itself is kept and flagged)
* run_history.csv            one row per pipeline run
"""
from __future__ import annotations

import pandas as pd

from isd_pipeline.ingestion.storage_base import StorageBackend, read_named_csv, save_dataframe
from isd_pipeline.schema import coerce

LOG_FOLDER = "pipeline_logs"

INGESTION_LOG_COLUMNS = {
    "ingestion_batch_id": "str", "run_started_at": "str", "trigger": "str", "mode": "str",
    "report_date": "str", "division": "str", "status": "str", "email_id": "str", "attachment_id": "str",
    "filename": "str", "received_at": "str", "sha256": "str", "header_row": "str", "rows_extracted": "str",
    "invalid_rows": "str", "inspector_cells_filled": "str", "red_rows": "str", "red_cells_checked": "str",
    "raw_file_id": "str", "snapshot_file_id": "str", "message": "str",
}
PROCESSED_COLUMNS = {
    "email_id": "str", "attachment_id": "str", "sha256": "str", "division": "str", "report_date": "str",
    "status": "str", "ingestion_batch_id": "str", "processed_at": "str", "raw_file_id": "str",
    "snapshot_file_id": "str",
}
INVALID_ROW_COLUMNS = {
    "ingestion_batch_id": "str", "report_date": "str", "division": "str", "source_email_id": "str",
    "source_filename": "str", "raw_file_id": "str", "source_row_number": "str", "reason": "str", "values": "str",
}
RUN_HISTORY_COLUMNS = {
    "ingestion_batch_id": "str", "trigger": "str", "mode": "str", "report_date": "str", "started_at": "str",
    "finished_at": "str", "status": "str", "divisions_ok": "str", "divisions_problem": "str",
    "new_files": "str", "snapshot_rows_ingested": "str", "new_requests": "str", "updated_requests": "str",
    "flagged_requests": "str", "requests_total": "str", "on_time_pct": "str", "source_files": "str",
    "message": "str",
}

LOGS = {
    "ingestion_log.csv": INGESTION_LOG_COLUMNS,
    "processed_attachments.csv": PROCESSED_COLUMNS,
    "invalid_rows.csv": INVALID_ROW_COLUMNS,
    "run_history.csv": RUN_HISTORY_COLUMNS,
}


def read_log(storage: StorageBackend, name: str) -> pd.DataFrame:
    cols = LOGS[name]
    df = read_named_csv(storage, LOG_FOLDER, name, cols)
    return coerce(df, cols) if df is not None else coerce(pd.DataFrame(), cols)


def append_log(storage: StorageBackend, name: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = LOGS[name]
    current = read_log(storage, name)
    new = coerce(pd.DataFrame.from_records(rows), cols)
    combined = pd.concat([current, new], ignore_index=True) if len(current) else new
    save_dataframe(storage, LOG_FOLDER, name, combined, cols, {"kind": "log"}, versioned=False)


def processed_keys(storage: StorageBackend) -> tuple[set[tuple[str, str]], set[str]]:
    """(email_id, attachment_id) pairs and content hashes already PROCESSED successfully."""
    df = read_log(storage, "processed_attachments.csv")
    ok = df[df["status"] == "PROCESSED"]
    return set(zip(ok["email_id"], ok["attachment_id"])), set(ok["sha256"])
