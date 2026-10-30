"""Store the original Excel attachments in raw_reports/<division>/ (idempotent)."""
from __future__ import annotations

from isd_pipeline.ingestion.email_base import ReportAttachment
from isd_pipeline.ingestion.storage_base import XLSX_MIME, StorageBackend, StoredFile
from isd_pipeline.settings import division_slug


def raw_folder(division: str) -> str:
    return f"raw_reports/{division_slug(division)}"


def raw_name(att: ReportAttachment) -> str:
    return f"{att.report_date.isoformat()}_{division_slug(att.division)}_{att.email_id}.xlsx"


def store_raw_report(storage: StorageBackend, att: ReportAttachment) -> tuple[StoredFile, bool]:
    """Upload unless the same email+attachment (or the same content) is already stored.
    Returns (file, created)."""
    if att.content is None:
        raise ValueError("Attachment content must be downloaded before storing")
    folder = raw_folder(att.division)
    sha = att.sha256
    for f in storage.list_files(folder):
        p = f.app_properties
        if (p.get("email_id") == att.email_id and p.get("attachment_id") == att.attachment_id) or p.get("sha256") == sha:
            return f, False
    stored = storage.create_file(folder, raw_name(att), att.content, XLSX_MIME, {
        "kind": "raw_report", "email_id": att.email_id, "attachment_id": att.attachment_id, "sha256": sha,
        "division": att.division, "report_date": att.report_date.isoformat(),
        "received_at": att.received_at.isoformat(),
    })
    return stored, True
