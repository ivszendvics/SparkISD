"""Live Gmail adapter (scope: gmail.readonly).

Attachment identity: Gmail's `attachmentId` changes every time a message is fetched, so it
is useless as an idempotency key. We use the MIME `partId` (stable for a given message) as
attachment_id, and look up a fresh attachmentId right before downloading.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import parseaddr

from isd_pipeline.ingestion.email_base import AttachmentMeta, EmailMessage, EmailQuery, EmailSource
from isd_pipeline.ingestion.google_errors import is_retriable, translate
from isd_pipeline.retry import call_with_retry
from isd_pipeline.settings import Settings


class GmailAdapter(EmailSource):
    mode = "live"

    def __init__(self, service, user_id: str = "me", retry_attempts: int = 4, retry_base_delay: float = 1.0):
        self.service = service
        self.user_id = user_id
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay

    @classmethod
    def from_settings(cls, settings: Settings) -> "GmailAdapter":
        from googleapiclient.discovery import build

        from isd_pipeline.auth import load_credentials
        creds = load_credentials(settings)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return cls(service, retry_attempts=settings.retry_attempts, retry_base_delay=settings.retry_base_delay)

    def _exec(self, request, what: str):
        def go():
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001
                raise translate(exc, "Gmail API") from exc
        return call_with_retry(go, attempts=self.retry_attempts, base_delay=self.retry_base_delay, what=what,
                               is_retriable=is_retriable)

    # ------------------------------------------------------------------
    def search(self, query: EmailQuery) -> list[EmailMessage]:
        q = query.to_gmail_query()
        ids: list[str] = []
        page = None
        while True:
            resp = self._exec(self.service.users().messages().list(userId=self.user_id, q=q, pageToken=page,
                                                                   maxResults=50), "Gmail search")
            ids += [m["id"] for m in resp.get("messages", [])]
            page = resp.get("nextPageToken")
            if not page:
                break
        return [self._get(mid) for mid in ids]

    def _get(self, message_id: str) -> EmailMessage:
        msg = self._exec(self.service.users().messages().get(userId=self.user_id, id=message_id, format="full"),
                         "Gmail get message")
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        name, addr = parseaddr(headers.get("from", ""))
        received = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc)
        atts = []
        for part in _walk(msg.get("payload", {})):
            body = part.get("body", {})
            if part.get("filename") and body.get("attachmentId"):
                atts.append(AttachmentMeta(attachment_id=str(part.get("partId", "")), filename=part["filename"],
                                           mime_type=part.get("mimeType", ""), size=int(body.get("size", 0)),
                                           download_token=body["attachmentId"]))
        return EmailMessage(email_id=msg["id"], thread_id=msg.get("threadId", ""), sender_name=name,
                            sender_address=addr, subject=headers.get("subject", ""), received_at=received,
                            snippet=msg.get("snippet", ""), attachments=tuple(atts),
                            labels=tuple(msg.get("labelIds", [])))

    def download_attachment(self, email_id: str, attachment: AttachmentMeta) -> bytes:
        # Refresh the volatile attachmentId via the stable partId.
        fresh = next((a for a in self._get(email_id).attachments if a.attachment_id == attachment.attachment_id), None)
        token = fresh.download_token if fresh else attachment.download_token
        resp = self._exec(self.service.users().messages().attachments().get(
            userId=self.user_id, messageId=email_id, id=token), "Gmail download attachment")
        return base64.urlsafe_b64decode(resp["data"].encode())

    def describe(self) -> str:
        return "live Gmail (gmail.readonly)"


def _walk(part: dict):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)
