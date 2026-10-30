"""Mock inbox: serves demo_data/mock_inbox/<YYYY-MM-DD>/*.json + attachments through the
same EmailSource interface as the live Gmail adapter."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from isd_pipeline.ingestion.email_base import AttachmentMeta, EmailMessage, EmailQuery, EmailSource


class MockGmailAdapter(EmailSource):
    mode = "mock"

    def __init__(self, inbox_dir: Path | str, *, fail_subjects: set[str] | None = None,
                 fail_times: int = 10**9):
        """`fail_subjects` simulates API errors for testing: searches for those subjects raise
        the first `fail_times` times they are called."""
        self.inbox_dir = Path(inbox_dir)
        self.fail_subjects = set(fail_subjects or ())
        self.fail_times = fail_times
        self._fail_counts: dict[str, int] = {}
        self._index: dict[str, tuple[Path, dict]] = {}

    # -- helpers ------------------------------------------------------------
    def _load_all(self) -> list[tuple[Path, dict]]:
        out = []
        if not self.inbox_dir.exists():
            return out
        for meta_path in sorted(self.inbox_dir.glob("*/*.json")):
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                continue
            if "email_id" not in meta:
                continue
            out.append((meta_path.parent, meta))
            self._index[meta["email_id"]] = (meta_path.parent, meta)
        return out

    @staticmethod
    def _to_message(folder: Path, meta: dict) -> EmailMessage:
        atts = []
        for a in meta.get("attachments", []):
            f = folder / a.get("file", "")
            atts.append(AttachmentMeta(
                attachment_id=str(a["attachment_id"]), filename=a["filename"],
                mime_type=a.get("mime_type", ""), size=f.stat().st_size if f.exists() else 0,
                download_token=a.get("file", ""),
            ))
        sender = meta.get("from", {})
        return EmailMessage(
            email_id=meta["email_id"], thread_id=meta.get("thread_id", meta["email_id"]),
            sender_name=sender.get("name", ""), sender_address=sender.get("address", ""),
            subject=meta.get("subject", ""), received_at=datetime.fromisoformat(meta["received_at"]),
            snippet=meta.get("snippet", ""), attachments=tuple(atts), labels=tuple(meta.get("labels", [])),
        )

    # -- interface ------------------------------------------------------------
    def search(self, query: EmailQuery) -> list[EmailMessage]:
        if query.subject in self.fail_subjects:
            n = self._fail_counts.get(query.subject, 0)
            self._fail_counts[query.subject] = n + 1
            if n < self.fail_times:
                raise ConnectionError(f"Simulated Gmail API error for '{query.subject}'")
        tz = ZoneInfo("America/New_York")
        lo = datetime.combine(query.after, datetime.min.time(), tz)
        hi = datetime.combine(query.before, datetime.min.time(), tz) + timedelta(days=0)
        results = []
        for folder, meta in self._load_all():
            msg = self._to_message(folder, meta)
            if query.subject.lower() not in msg.subject.lower():
                continue
            if query.sender_address and msg.sender_address.lower() != query.sender_address.lower():
                continue
            if query.label and query.label not in msg.labels:
                continue
            if query.has_attachment and not msg.attachments:
                continue
            if not (lo <= msg.received_at < hi):
                continue
            results.append(msg)
        return results

    def download_attachment(self, email_id: str, attachment: AttachmentMeta) -> bytes:
        if email_id not in self._index:
            self._load_all()
        folder, _meta = self._index[email_id]
        return (folder / attachment.download_token).read_bytes()

    def describe(self) -> str:
        return f"mock inbox at {self.inbox_dir}"

    # -- demo helpers -------------------------------------------------------------
    def list_messages(self) -> list[EmailMessage]:
        """All messages in the inbox, newest first (for the Gmail-style demo view)."""
        msgs = [self._to_message(f, m) for f, m in self._load_all()]
        return sorted(msgs, key=lambda m: m.received_at, reverse=True)
