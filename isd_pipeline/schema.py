"""The shared data contract (docs/SCHEMA.md) as code.

Every module reads and writes DataFrames through `coerce()` and the CSV helpers here, so
types survive a round trip through Drive: ap_number and ward stay strings, dates stay
dates, booleans stay booleans.

Conventions
-----------
* "str" columns use "" for blank (never NaN), so `x == ""` is the blank test.
* "date" columns are pandas datetime64 normalised to midnight (NaT when missing).
* "datetime" columns are naive local timestamps (America/New_York wall clock).
* "datetime_utc" columns are tz-aware UTC timestamps.
"""
from __future__ import annotations

import io
from typing import Iterable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 3a. Snapshot rows (Module 1 output)
# ---------------------------------------------------------------------------
SNAPSHOT_COLUMNS: dict[str, str] = {
    "inspector": "str",
    "ward": "str",
    "call_datetime": "datetime",
    "ap_number": "str",
    "address": "str",
    "insp_type": "str",
    "work_group": "str",
    "contact_name": "str",
    "contact_info": "str",
    "request_notes": "str",
    "inspector_notes": "str",
    "fulfilled_late": "str",
    "is_red": "bool",
    "division": "str",
    "report_date": "date",
    "source_email_id": "str",
    "source_attachment_id": "str",
    "source_filename": "str",
    "ingested_at": "datetime_utc",
    "source_row_number": "int",
    "ingestion_batch_id": "str",
    # Extension (documented in SCHEMA.md): ';'-joined row-level problems found by the parser.
    "row_issues": "str",
}

# Fields of a snapshot row that describe the request (carried into requests_current).
REQUEST_DESCRIPTIVE_FIELDS: list[str] = [
    "inspector", "ward", "call_datetime", "ap_number", "address", "insp_type", "work_group",
    "contact_name", "contact_info", "request_notes", "inspector_notes", "fulfilled_late",
    "is_red", "division", "source_email_id", "source_attachment_id", "source_filename",
    "ingested_at", "source_row_number", "ingestion_batch_id",
]

# Fields refreshed from the latest report (never overwritten with a blank).
LATEST_VALUE_FIELDS: list[str] = [
    "inspector", "ward", "contact_name", "contact_info", "request_notes", "inspector_notes",
    "fulfilled_late", "work_group",
]
# Provenance fields that follow the latest report the request was seen on.
LATEST_PROVENANCE_FIELDS: list[str] = [
    "is_red", "source_email_id", "source_attachment_id", "source_filename", "ingested_at",
    "source_row_number", "ingestion_batch_id",
]

# ---------------------------------------------------------------------------
# 3b. Current requests (Module 2 output)
# ---------------------------------------------------------------------------
REQUEST_COLUMNS: dict[str, str] = {
    "inspection_request_id": "str",
    "match_key": "str",
    **{k: SNAPSHOT_COLUMNS[k] for k in REQUEST_DESCRIPTIVE_FIELDS},
    "first_seen_report_date": "date",
    "last_seen_report_date": "date",
    "seen_report_dates": "str",
    "n_reports": "int",
    "first_note_date": "date",
    "ever_red": "bool",
    "dedupe_mark": "str",
    "review_flag": "bool",
    "review_reason": "str",
}

ID_REGISTRY_COLUMNS: dict[str, str] = {
    "match_key": "str",
    "inspection_request_id": "str",
    "created_batch_id": "str",
    "first_seen_report_date": "date",
}

# ---------------------------------------------------------------------------
# 3c. Classified requests (Module 3 output)
# ---------------------------------------------------------------------------
CLASSIFIED_COLUMNS: dict[str, str] = {
    **REQUEST_COLUMNS,
    "call_date": "date",
    "sla_business_days": "int",
    "due_date": "date",
    "as_of": "date",
    "is_open": "bool",
    "last_seen_latest_possible": "date",
    "status": "str",
    "is_late": "bool",
    "is_judged": "bool",
    "business_days_late": "int",
    "business_days_open": "int",
    "font_mismatch": "bool_or_null",
    "mismatch_reason": "str",
    "dq_flags": "str",
}

STATUSES: tuple[str, ...] = ("open_not_due", "on_time", "on_time_contacted", "open_overdue", "late", "unknown_close",
                              "invalid")
DEDUPE_MARKS: tuple[str, ...] = ("NEW", "UPDATED", "UNCHANGED", "FLAGGED")
CONTACT_FIELDS: tuple[str, ...] = ("contact_name", "contact_info")


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------
def _to_str(s: pd.Series) -> pd.Series:
    def conv(v: object) -> str:
        if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA or v is pd.NaT:
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return pd.Series([conv(v) for v in s.tolist()], index=s.index, dtype=object)


def _to_bool(s: pd.Series, nullable: bool = False) -> pd.Series:
    def conv(v: object) -> object:
        if v is None or v is pd.NA or (isinstance(v, float) and np.isnan(v)):
            return None if nullable else False
        if isinstance(v, (bool, np.bool_)):
            return bool(v)
        text = str(v).strip().lower()
        if text in ("true", "1", "yes", "y", "t"):
            return True
        if text in ("false", "0", "no", "n", "f"):
            return False
        return None if nullable else False
    values = [conv(v) for v in s.tolist()]
    if nullable:
        return pd.Series(values, index=s.index, dtype="boolean")
    return pd.Series(values, index=s.index, dtype=bool)


def coerce(df: pd.DataFrame | None, columns: dict[str, str], keep_extra: bool = False) -> pd.DataFrame:
    """Return a copy of df with exactly `columns` (in order) and the declared types.

    Missing columns are created blank. With keep_extra=True, unknown columns are kept at the end.
    """
    if df is None:
        df = pd.DataFrame()
    out = pd.DataFrame(index=df.index)
    for col, kind in columns.items():
        s = df[col] if col in df.columns else pd.Series([None] * len(df), index=df.index, dtype=object)
        if kind == "str":
            out[col] = _to_str(s)
        elif kind == "int":
            out[col] = pd.to_numeric(s, errors="coerce").fillna(0).astype("int64")
        elif kind == "bool":
            out[col] = _to_bool(s)
        elif kind == "bool_or_null":
            out[col] = _to_bool(s, nullable=True)
        elif kind == "date":
            out[col] = pd.to_datetime(s, errors="coerce").dt.normalize().astype("datetime64[ns]")
        elif kind == "datetime":
            out[col] = pd.to_datetime(s, errors="coerce").astype("datetime64[ns]")
        elif kind == "datetime_utc":
            out[col] = pd.to_datetime(s, errors="coerce", utc=True)
        else:  # pragma: no cover - programming error
            raise ValueError(f"Unknown column kind {kind!r} for {col}")
    if keep_extra:
        for col in df.columns:
            if col not in out.columns:
                out[col] = df[col]
    return out.reset_index(drop=True)


def empty_frame(columns: dict[str, str]) -> pd.DataFrame:
    return coerce(pd.DataFrame(), columns)


# ---------------------------------------------------------------------------
# CSV round trip (the storage format for every dataset in Drive)
# ---------------------------------------------------------------------------
def to_csv_bytes(df: pd.DataFrame, columns: dict[str, str] | None = None) -> bytes:
    """Serialise with stable, Power BI-friendly formats (ISO dates, True/False)."""
    out = df.copy()
    kinds = columns or {}
    for col in out.columns:
        kind = kinds.get(col)
        s = out[col]
        if kind == "date" or (kind is None and pd.api.types.is_datetime64_any_dtype(s) and _dates_only(s)):
            out[col] = pd.to_datetime(s).dt.strftime("%Y-%m-%d").fillna("")
        elif kind == "datetime" or (kind is None and pd.api.types.is_datetime64_any_dtype(s)):
            if getattr(s.dt, "tz", None) is not None:
                out[col] = s.dt.strftime("%Y-%m-%dT%H:%M:%S%z").fillna("")
            else:
                out[col] = pd.to_datetime(s).dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")
        elif kind == "datetime_utc":
            out[col] = pd.to_datetime(s, utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ").fillna("")
    buf = io.StringIO()
    out.to_csv(buf, index=False, lineterminator="\n")
    return buf.getvalue().encode("utf-8")


def _dates_only(s: pd.Series) -> bool:
    valid = s.dropna()
    if valid.empty:
        return False
    if getattr(valid.dt, "tz", None) is not None:
        return False
    return bool((valid.dt.normalize() == valid).all())


def from_csv_bytes(data: bytes, columns: dict[str, str] | None = None) -> pd.DataFrame:
    """Read a CSV written by to_csv_bytes. Everything is read as text first (so leading
    zeros survive), then coerced to `columns` if given."""
    if not data:
        return empty_frame(columns) if columns else pd.DataFrame()
    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, na_values=[])
    df = df.astype(object)
    if columns:
        return coerce(df, columns, keep_extra=True)
    return df


def join_unique(values: Iterable[str], sep: str = ";") -> str:
    """Join non-blank values, de-duplicated, keeping first-seen order."""
    seen: list[str] = []
    for v in values:
        for part in str(v or "").split(sep):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
    return sep.join(seen)
