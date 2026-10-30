"""Daily standardized snapshot rows in historical_snapshots/ (one CSV per report_date + division).

Snapshots are never deleted or de-duplicated: the same request appearing on many days is
expected, and these files are the history that Module 2 collapses.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from isd_pipeline.ingestion.storage_base import StorageBackend, StoredFile, read_dataframe, save_dataframe
from isd_pipeline.schema import SNAPSHOT_COLUMNS, empty_frame
from isd_pipeline.settings import DIVISIONS, division_slug

SNAPSHOT_FOLDER = "historical_snapshots"


def snapshot_name(report_date: date, division: str) -> str:
    return f"{report_date.isoformat()}_{division_slug(division)}_snapshot.csv"


def find_snapshot(storage: StorageBackend, *, email_id: str, attachment_id: str, sha256: str) -> StoredFile | None:
    found = storage.find_files(SNAPSHOT_FOLDER, app_properties={
        "email_id": email_id, "attachment_id": attachment_id, "source_sha256": sha256})
    return found[-1] if found else None


def save_snapshot(storage: StorageBackend, df: pd.DataFrame, report_date: date, division: str, *,
                  email_id: str, attachment_id: str, sha256: str, batch_id: str) -> tuple[StoredFile, bool]:
    """Store the snapshot unless this exact attachment was already stored. Returns (file, created)."""
    existing = find_snapshot(storage, email_id=email_id, attachment_id=attachment_id, sha256=sha256)
    if existing is not None:
        return existing, False
    stored = save_dataframe(storage, SNAPSHOT_FOLDER, snapshot_name(report_date, division), df, SNAPSHOT_COLUMNS,
                            app_properties={
                                "kind": "snapshot", "report_date": report_date.isoformat(),
                                "division": division, "email_id": email_id, "attachment_id": attachment_id,
                                "source_sha256": sha256, "row_count": str(len(df)), "batch_id": batch_id,
                            })
    return stored, True


def list_snapshot_files(storage: StorageBackend, start_date: date | None = None, end_date: date | None = None,
                        divisions: list[str] | None = None) -> list[StoredFile]:
    """Latest snapshot file per (report_date, division), filtered."""
    latest: dict[tuple[str, str], StoredFile] = {}
    for f in storage.list_files(SNAPSHOT_FOLDER):
        props = f.app_properties
        if props.get("kind") != "snapshot":
            continue
        d = date.fromisoformat(props["report_date"])
        if start_date and d < start_date or end_date and d > end_date:
            continue
        if divisions and props["division"] not in divisions:
            continue
        latest[(props["report_date"], props["division"])] = f  # list is oldest-first: last wins
    return [latest[k] for k in sorted(latest)]


def load_snapshots(start_date: date | None = None, end_date: date | None = None,
                   divisions: list[str] | None = None, storage: StorageBackend | None = None) -> pd.DataFrame:
    """All snapshot rows in [start_date, end_date] (inclusive), optionally for some divisions."""
    if storage is None:
        from isd_pipeline.factory import make_storage
        storage = make_storage()
    frames = [read_dataframe(storage, f.file_id, SNAPSHOT_COLUMNS)
              for f in list_snapshot_files(storage, start_date, end_date, divisions)]
    if not frames:
        return empty_frame(SNAPSHOT_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def report_dates_by_division(storage: StorageBackend) -> dict[str, list[date]]:
    """Which report dates have a valid stored snapshot, per division (drives per-division as_of)."""
    out: dict[str, list[date]] = {d: [] for d in DIVISIONS}
    for f in list_snapshot_files(storage):
        out.setdefault(f.app_properties["division"], []).append(date.fromisoformat(f.app_properties["report_date"]))
    return {k: sorted(set(v)) for k, v in out.items()}


def list_report_dates(division: str | None = None, storage: StorageBackend | None = None) -> list[date]:
    if storage is None:
        from isd_pipeline.factory import make_storage
        storage = make_storage()
    by_div = report_dates_by_division(storage)
    if division:
        return by_div.get(division, [])
    return sorted({d for dates in by_div.values() for d in dates})


def total_snapshot_rows(storage: StorageBackend) -> int:
    return sum(int(f.app_properties.get("row_count", 0)) for f in list_snapshot_files(storage))
