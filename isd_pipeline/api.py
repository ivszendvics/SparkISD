"""The public integration API (section 4.5). Each function uses the adapters chosen by
settings.yaml (mock or live) unless you pass your own."""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from isd_pipeline.factory import make_email_source, make_storage
from isd_pipeline.ingestion.email_base import EmailSource, ReportAttachment, find_daily_reports
from isd_pipeline.ingestion.excel_parser import ValidationResult
from isd_pipeline.ingestion.excel_parser import process_excel_report as _process_excel_report
from isd_pipeline.ingestion.manifest import load_latest_dataset as _load_latest_dataset
from isd_pipeline.ingestion.storage_base import StorageBackend, StoredFile, save_dataframe
from isd_pipeline.pipeline import RunResult
from isd_pipeline.pipeline import run_daily_pipeline as _run_daily_pipeline
from isd_pipeline.settings import Settings, load_settings


def fetch_daily_reports(report_date: date, *, settings: Settings | None = None,
                        source: EmailSource | None = None, download: bool = True) -> list[ReportAttachment]:
    """Find (and download) the day's report attachments, one per division that arrived."""
    settings = settings or load_settings()
    result = find_daily_reports(source or make_email_source(settings), settings.email_rules, report_date,
                                tz=settings.timezone, download=download, retry_attempts=settings.retry_attempts,
                                retry_base_delay=settings.retry_base_delay)
    return result.attachments


def process_excel_report(file_path, division: str, report_date: date, **kwargs: Any) -> tuple[pd.DataFrame, ValidationResult]:
    """Parse one report into snapshot rows + a ValidationResult. See ingestion/excel_parser.py."""
    return _process_excel_report(file_path, division, report_date, **kwargs)


def save_to_google_drive(dataframe: pd.DataFrame, metadata: dict[str, Any], *,
                         storage: StorageBackend | None = None) -> StoredFile:
    """Save a DataFrame as CSV in Drive.

    metadata keys: folder (e.g. "processed_data"), name ("x.csv"), app_properties (dict, optional),
    versioned (bool, default True: always a new file; False: update the same-named file in place),
    columns (optional schema dict from isd_pipeline.schema).
    """
    storage = storage or make_storage()
    storage.ensure_folders()
    return save_dataframe(storage, metadata["folder"], metadata["name"], dataframe, metadata.get("columns"),
                          metadata.get("app_properties"), versioned=metadata.get("versioned", True))


def load_latest_dataset(name: str = "requests_current", *, storage: StorageBackend | None = None) -> pd.DataFrame:
    """Read the latest successful dataset through pipeline_logs/manifest.json."""
    return _load_latest_dataset(storage or make_storage(), name)


def run_daily_pipeline(report_date: date | None = None, **kwargs: Any) -> RunResult:
    """Run the whole pipeline for one report date. Returns SUCCESS | PARTIAL_SUCCESS | FAILURE."""
    return _run_daily_pipeline(report_date, **kwargs)
