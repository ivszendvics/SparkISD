"""Live adapters against fake Google API services (no network, no credentials)."""
from __future__ import annotations

import base64
import json
import re
from datetime import date

import pytest

from isd_pipeline.errors import LiveSetupError
from isd_pipeline.ingestion.email_base import AttachmentMeta, EmailQuery, find_daily_reports
from isd_pipeline.ingestion.gdrive_adapter import GoogleDriveAdapter
from isd_pipeline.ingestion.gmail_adapter import GmailAdapter
from isd_pipeline.ingestion.google_errors import is_retriable, translate
from isd_pipeline.ingestion.storage_base import FOLDERS
from isd_pipeline.settings import load_settings


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------
class FakeGmail:
    def __init__(self, messages):
        self.messages_by_id = {m["id"]: m for m in messages}
        self.calls = 0
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def list(self, userId, q, pageToken=None, maxResults=50):
        self.queries.append(q)
        subj = q.split('subject:"')[1].split('"')[0]
        ids = [{"id": i} for i, m in self.messages_by_id.items() if subj in _hdr(m, "Subject")]
        return Req(lambda: {"messages": ids})

    def get(self, userId, id, format=None, messageId=None):
        if messageId is not None:   # attachments().get
            return Req(lambda: {"data": base64.urlsafe_b64encode(b"xlsx-bytes-" + id.encode()).decode()})
        self.calls += 1
        msg = json.loads(json.dumps(self.messages_by_id[id]))
        # Gmail hands out a NEW attachmentId on every fetch
        msg["payload"]["parts"][1]["body"]["attachmentId"] = f"att-{self.calls}"
        return Req(lambda: msg)


def _hdr(m, name):
    return next(h["value"] for h in m["payload"]["headers"] if h["name"] == name)


def _gmail_msg(mid, subject, ms):
    return {"id": mid, "threadId": "t" + mid, "internalDate": str(ms), "snippet": "Here is your subscription",
            "labelIds": ["INBOX"], "payload": {"headers": [
                {"name": "From", "value": "Microsoft Power BI <no-reply@powerbi.example>"},
                {"name": "Subject", "value": subject}],
                "parts": [{"partId": "0", "mimeType": "text/html", "filename": "", "body": {"size": 10}},
                          {"partId": "1", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           "filename": "Permit Inspecti.xlsx", "body": {"attachmentId": "x", "size": 1234}}]}}


def test_gmail_adapter_search_and_download():
    ms = 1791802992000   # 2026-10-12 07:03:12 EDT
    fake = FakeGmail([_gmail_msg("m1", "Permit Inspection Request - Building Summary", ms),
                      _gmail_msg("m2", "Permit Inspection Request - Electric Summary", ms)])
    adapter = GmailAdapter(fake, retry_base_delay=0)
    rules = load_settings().email_rules
    fr = find_daily_reports(adapter, rules, date(2026, 10, 12), retry_base_delay=0)
    assert fr.divisions["Building"].status == "FOUND"
    att = fr.divisions["Building"].attachment
    assert att.attachment_id == "1"                 # stable partId, not the volatile attachmentId
    assert att.content.startswith(b"xlsx-bytes-att-")
    assert fr.divisions["Plumbing/Gas"].status == "MISSING"
    assert "has:attachment" in fake.queries[0] and "after:2026/10/11" in fake.queries[0]


def test_gmail_query_rendering():
    q = EmailQuery(subject="Permit Inspection Request - Building Summary", after=date(2026, 10, 11),
                   before=date(2026, 10, 13), sender_address="x@y.com", label="ISD Reports")
    assert q.to_gmail_query() == ('from:x@y.com subject:"Permit Inspection Request - Building Summary" '
                                  'has:attachment label:ISD-Reports after:2026/10/11 before:2026/10/13')


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------
class FakeDrive:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.n = 0

    def files(self):
        return self

    def _match(self, meta, q):
        for clause in re.split(r" and (?![^{]*})", q):
            clause = clause.strip()
            if clause.endswith("in parents"):
                if clause.split("'")[1] not in meta.get("parents", []):
                    return False
            elif clause.startswith("name ="):
                if meta["name"] != clause.split("'", 1)[1].rsplit("'", 1)[0].replace("\\'", "'"):
                    return False
            elif clause.startswith("mimeType ="):
                if meta["mimeType"] != clause.split("'")[1]:
                    return False
            elif clause.startswith("mimeType !="):
                if meta["mimeType"] == clause.split("'")[1]:
                    return False
            elif clause.startswith("appProperties has"):
                k = clause.split("key='")[1].split("'")[0]
                v = clause.split("value='")[1].split("'")[0]
                if meta.get("appProperties", {}).get(k) != v:
                    return False
        return True

    def list(self, q, fields=None, spaces=None, orderBy=None, pageSize=None, pageToken=None):
        q = q.replace(" and trashed = false", "")
        return Req(lambda: {"files": [m for m in self.items.values() if self._match(m, q)]})

    def create(self, body, fields=None, media_body=None):
        self.n += 1
        fid = f"g{self.n}"
        meta = {"id": fid, "name": body["name"], "mimeType": body.get("mimeType", ""), "parents": body["parents"],
                "appProperties": body.get("appProperties", {}), "createdTime": f"2026-10-20T00:00:{self.n:02d}Z",
                "modifiedTime": "2026-10-20T00:00:00Z", "webViewLink": f"https://drive/{fid}"}
        if media_body is not None:
            data = media_body.getbytes(0, media_body.size())
            self.blobs[fid] = data
            meta["size"] = str(len(data))
        self.items[fid] = meta
        return Req(lambda: meta)

    def update(self, fileId, body, media_body=None, fields=None):
        meta = self.items[fileId]
        meta["appProperties"].update(body.get("appProperties", {}))
        self.blobs[fileId] = media_body.getbytes(0, media_body.size())
        return Req(lambda: meta)

    def get(self, fileId, fields=None):
        return Req(lambda: self.items[fileId])

    def get_media(self, fileId):
        return Req(lambda: self.blobs[fileId])


def test_drive_adapter_tree_upload_find_update():
    fake = FakeDrive()
    d = GoogleDriveAdapter(fake, retry_base_delay=0)
    ids = d.ensure_folders()
    assert set(ids) == set(FOLDERS)
    n_folders = len(fake.items)
    GoogleDriveAdapter(fake).ensure_folders()       # second run finds, doesn't recreate
    assert len(fake.items) == n_folders
    f = d.create_file("processed_data", "a.csv", b"x,y\n1,2\n", "text/csv", {"dataset": "t"})
    assert d.read_file(f.file_id) == b"x,y\n1,2\n" and f.sha256
    assert [x.file_id for x in d.find_files("processed_data", app_properties={"dataset": "t"})] == [f.file_id]
    u = d.upsert_file("processed_data", "a.csv", b"new")
    assert u.file_id == f.file_id and d.read_file(f.file_id) == b"new"
    assert d.files_created == 1


# ---------------------------------------------------------------------------
# Errors and auth
# ---------------------------------------------------------------------------
class _Resp(dict):
    def __init__(self, status):
        super().__init__()
        self.status = status
        self.reason = ""


def _http_error(status, reason):
    from googleapiclient.errors import HttpError
    body = json.dumps({"error": {"message": "msg", "errors": [{"reason": reason}]}}).encode()
    return HttpError(_Resp(status), body)


def test_http_errors_translated():
    assert isinstance(translate(_http_error(401, "authError"), "Gmail API"), LiveSetupError)
    e = translate(_http_error(403, "insufficientPermissions"), "Gmail API")
    assert isinstance(e, LiveSetupError) and "token.json" in str(e)
    assert isinstance(translate(_http_error(403, "accessNotConfigured"), "Gmail API"), LiveSetupError)
    assert is_retriable(_http_error(503, "backendError")) and not is_retriable(_http_error(404, "notFound"))


def test_auth_missing_files_clear_message(tmp_path):
    from isd_pipeline.auth import load_credentials
    s = load_settings(mode="live", credentials_file=tmp_path / "c.json", token_file=tmp_path / "t.json")
    with pytest.raises(LiveSetupError, match="OAuth client file not found"):
        load_credentials(s)
    (tmp_path / "c.json").write_text("{}")
    with pytest.raises(LiveSetupError, match="python -m isd_pipeline.auth"):
        load_credentials(s)


def test_live_mode_requires_sender_address(tmp_path):
    from isd_pipeline.pipeline import run_daily_pipeline
    s = load_settings(mode="live")
    with pytest.raises(LiveSetupError, match="Show original"):
        run_daily_pipeline(date(2026, 10, 5), settings=s)


def test_cli_prints_message_not_traceback(capsys):
    from isd_pipeline.cli import main
    assert main(["run", "--live", "--date", "2026-10-05"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: Live mode needs the real sender address") and "Traceback" not in err
