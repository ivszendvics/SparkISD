"""Email source interface + the shared logic that turns search results into daily reports.

Adapters (live Gmail, mock folder inbox) only implement `search()` and
`download_attachment()`. Everything about *which* emails count (sender check, subject ->
division, report day in America/New_York, duplicate emails, bad attachments) lives in
`find_daily_reports()` so both adapters behave identically.
"""
from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Callable
from zoneinfo import ZoneInfo

from isd_pipeline.errors import LiveSetupError
from isd_pipeline.retry import call_with_retry
from isd_pipeline.settings import DIVISIONS, EmailRules

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttachmentMeta:
    attachment_id: str          # stable id (Gmail: MIME partId, mock: from JSON)
    filename: str
    mime_type: str
    size: int
    download_token: str = ""    # adapter-specific handle (Gmail's volatile attachmentId)


@dataclass(frozen=True)
class EmailMessage:
    email_id: str
    thread_id: str
    sender_name: str
    sender_address: str
    subject: str
    received_at: datetime       # tz-aware
    snippet: str
    attachments: tuple[AttachmentMeta, ...]
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailQuery:
    """Structured search. Live: rendered to a Gmail query string. Mock: applied directly."""
    subject: str
    after: date                 # inclusive (Gmail `after:` semantics are applied loosely)
    before: date                # exclusive
    sender_address: str | None = None
    label: str | None = None
    has_attachment: bool = True

    def to_gmail_query(self) -> str:
        parts = []
        if self.sender_address:
            parts.append(f"from:{self.sender_address}")
        parts.append(f'subject:"{self.subject}"')
        if self.has_attachment:
            parts.append("has:attachment")
        if self.label:
            label = self.label.replace(" ", "-")
            parts.append(f"label:{label}")
        parts.append(f"after:{self.after:%Y/%m/%d}")
        parts.append(f"before:{self.before:%Y/%m/%d}")
        return " ".join(parts)


@dataclass
class ReportAttachment:
    """One daily report attachment, selected for a division and report date."""
    division: str
    report_date: date
    email_id: str
    thread_id: str
    attachment_id: str
    filename: str
    size: int
    received_at: datetime
    subject: str
    sender: str
    snippet: str = ""
    mime_type: str = ""
    download_token: str = ""
    content: bytes | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest() if self.content is not None else ""


@dataclass
class DivisionFetch:
    division: str
    status: str                 # FOUND | MISSING | NO_ATTACHMENT | NOT_EXCEL | ERROR
    attachment: ReportAttachment | None = None
    message: str = ""
    ignored: list[dict] = field(default_factory=list)   # duplicate / rejected emails, for the log


@dataclass
class FetchResult:
    report_date: date
    divisions: dict[str, DivisionFetch]
    warnings: list[str] = field(default_factory=list)

    @property
    def attachments(self) -> list[ReportAttachment]:
        return [d.attachment for d in self.divisions.values() if d.attachment is not None]

    @property
    def missing(self) -> list[str]:
        return [d for d, f in self.divisions.items() if f.status != "FOUND"]


class EmailSource(ABC):
    """Interface implemented by GmailAdapter (live) and MockGmailAdapter."""

    mode: str = "abstract"

    @abstractmethod
    def search(self, query: EmailQuery) -> list[EmailMessage]:
        """Return messages matching the query (metadata only, no attachment bytes)."""

    @abstractmethod
    def download_attachment(self, email_id: str, attachment: AttachmentMeta) -> bytes:
        """Return the raw bytes of one attachment."""

    def describe(self) -> str:
        return self.mode


# ---------------------------------------------------------------------------
# Shared selection logic
# ---------------------------------------------------------------------------
def local_report_date(received_at: datetime, tz: str = "America/New_York") -> date:
    return received_at.astimezone(ZoneInfo(tz)).date()


def _sender_ok(msg: EmailMessage, rules: EmailRules) -> bool:
    if rules.sender_address:
        return msg.sender_address.strip().lower() == rules.sender_address.strip().lower()
    return msg.sender_name.strip().lower() == rules.sender_display_name.strip().lower()


def find_daily_reports(
    source: EmailSource,
    rules: EmailRules,
    report_date: date,
    *,
    tz: str = "America/New_York",
    download: bool = True,
    retry_attempts: int = 4,
    retry_base_delay: float = 1.0,
    sleep: Callable[[float], None] | None = None,
) -> FetchResult:
    """Find (and optionally download) the attachment for each division on `report_date`.

    One search per division, so an API failure only fails that division.
    """
    result = FetchResult(report_date=report_date, divisions={})
    pad = timedelta(days=max(1, rules.search_padding_days))
    retry_kwargs = {"attempts": retry_attempts, "base_delay": retry_base_delay}
    if sleep is not None:
        retry_kwargs["sleep"] = sleep

    for division in DIVISIONS:
        subject = rules.subject_for_division(division)
        query = EmailQuery(subject=subject, after=report_date - pad, before=report_date + pad,
                           sender_address=rules.sender_address, label=rules.label,
                           has_attachment=rules.has_attachment)
        try:
            messages = call_with_retry(lambda: source.search(query), what=f"search {division}", **retry_kwargs)
        except LiveSetupError:
            raise
        except Exception as exc:  # noqa: BLE001
            result.divisions[division] = DivisionFetch(division, "ERROR", message=f"Email search failed: {exc}")
            continue

        ignored: list[dict] = []
        candidates: list[EmailMessage] = []
        for msg in messages:
            if rules.division_for_subject(msg.subject) != division:
                continue  # Gmail subject search is fuzzy; require the exact subject
            if local_report_date(msg.received_at, tz) != report_date:
                continue  # outside the America/New_York report day
            if not _sender_ok(msg, rules):
                ignored.append({"email_id": msg.email_id, "reason": "UNEXPECTED_SENDER",
                                "detail": f"{msg.sender_name} <{msg.sender_address}>"})
                result.warnings.append(f"{division}: ignored email {msg.email_id} from unexpected sender "
                                       f"{msg.sender_name} <{msg.sender_address}>")
                continue
            candidates.append(msg)

        if not candidates:
            result.divisions[division] = DivisionFetch(
                division, "MISSING", message=f"No '{subject}' email received on {report_date}", ignored=ignored)
            continue

        candidates.sort(key=lambda m: m.received_at)
        chosen = candidates[-1]
        for older in candidates[:-1]:
            ignored.append({"email_id": older.email_id, "reason": "DUPLICATE_SAME_DAY",
                            "detail": f"older copy received {older.received_at.isoformat()}; kept {chosen.email_id}"})
            result.warnings.append(f"{division}: {len(candidates)} emails on {report_date}; kept the latest "
                                   f"({chosen.email_id}), ignored {older.email_id}")

        excel = [a for a in chosen.attachments
                 if PurePosixPath(a.filename.lower()).suffix in rules.allowed_extensions]
        if not chosen.attachments:
            result.divisions[division] = DivisionFetch(
                division, "NO_ATTACHMENT", message=f"Email {chosen.email_id} has no attachment", ignored=ignored)
            continue
        if not excel:
            names = ", ".join(a.filename for a in chosen.attachments)
            result.divisions[division] = DivisionFetch(
                division, "NOT_EXCEL", message=f"Email {chosen.email_id} has no Excel attachment ({names})",
                ignored=ignored)
            continue
        att = excel[0]
        if len(excel) > 1:
            result.warnings.append(f"{division}: email {chosen.email_id} has {len(excel)} Excel files; using {att.filename}")

        report = ReportAttachment(
            division=division, report_date=report_date, email_id=chosen.email_id, thread_id=chosen.thread_id,
            attachment_id=att.attachment_id, filename=att.filename, size=att.size,
            received_at=chosen.received_at, subject=chosen.subject,
            sender=f"{chosen.sender_name} <{chosen.sender_address}>", snippet=chosen.snippet,
            mime_type=att.mime_type, download_token=att.download_token,
        )
        if download:
            try:
                report.content = call_with_retry(lambda: source.download_attachment(chosen.email_id, att),
                                                 what=f"download {division}", **retry_kwargs)
            except LiveSetupError:
                raise
            except Exception as exc:  # noqa: BLE001
                result.divisions[division] = DivisionFetch(
                    division, "ERROR", attachment=None, message=f"Attachment download failed: {exc}", ignored=ignored)
                continue
        result.divisions[division] = DivisionFetch(division, "FOUND", attachment=report, ignored=ignored)
    return result
