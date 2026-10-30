"""Builders for hand-made test rows."""
from __future__ import annotations

import pandas as pd

from isd_pipeline.schema import REQUEST_COLUMNS, SNAPSHOT_COLUMNS, coerce

_counter = iter(range(1, 10**6))


def request(call: str | None, first_seen: str, last_seen: str, *, note: str | None = None, red: bool = False,
            division: str = "Building", insp_type: str = "Final Building", inspector: str = "Insp A",
            ward: str = "01", review: bool = False, rid: str | None = None) -> dict:
    """One requests_current row (schema 3b) for late-logic tests."""
    n = next(_counter)
    seen = pd.bdate_range(first_seen, last_seen)
    return {
        "inspection_request_id": rid or f"ISD-{n:06d}", "match_key": f"k{n}",
        "inspector": inspector, "ward": ward, "call_datetime": pd.Timestamp(call) if call else None,
        "ap_number": f"ALT{n:07d}", "address": f"{n} Main St", "insp_type": insp_type, "division": division,
        "first_seen_report_date": first_seen, "last_seen_report_date": last_seen,
        "seen_report_dates": ";".join(d.strftime("%Y-%m-%d") for d in seen), "n_reports": len(seen),
        "first_note_date": note, "ever_red": red, "is_red": red, "dedupe_mark": "NEW",
        "review_flag": review, "review_reason": "test" if review else "",
    }


def requests_frame(rows: list[dict]) -> pd.DataFrame:
    return coerce(pd.DataFrame(rows), REQUEST_COLUMNS)


def snap(report_date: str, call: str | None, ap: str, *, address: str = "10 Main St", insp_type: str = "Final Building",
         division: str = "Building", notes: str = "", request_notes: str = "", contact: str = "", red: bool = False,
         inspector: str = "Insp A", ward: str = "01", row: int = 5, email: str = "e1") -> dict:
    """One snapshot row (schema 3a) for dedupe tests."""
    return {
        "inspector": inspector, "ward": ward, "call_datetime": pd.Timestamp(call) if call else None,
        "ap_number": ap, "address": address, "insp_type": insp_type, "work_group": "",
        "contact_name": "C Name", "contact_info": contact, "request_notes": request_notes, "inspector_notes": notes,
        "fulfilled_late": "", "is_red": red, "division": division, "report_date": report_date,
        "source_email_id": f"{email}-{report_date}", "source_attachment_id": "1",
        "source_filename": f"{report_date}.xlsx", "ingested_at": "2026-10-20T12:00:00Z",
        "source_row_number": row, "ingestion_batch_id": f"B-{report_date}", "row_issues": "",
    }


def snaps(rows: list[dict]) -> pd.DataFrame:
    return coerce(pd.DataFrame(rows), SNAPSHOT_COLUMNS)
