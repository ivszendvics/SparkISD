"""Live Google Drive adapter (scope: drive.file).

drive.file means the app can only see files and folders IT created. Consequences:
* The "Permit Inspection Automation" tree is created by the app on first run (check-drive).
  A folder with the same name made by hand in the Drive UI is invisible to the app.
* People can still open/share the app-created folder normally in Drive.
"""
from __future__ import annotations

import io

from isd_pipeline.ingestion.google_errors import is_retriable, translate
from isd_pipeline.ingestion.storage_base import FOLDERS, ROOT_FOLDER_NAME, StorageBackend, StorageError, StoredFile
from isd_pipeline.retry import call_with_retry
from isd_pipeline.settings import Settings

FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,size,createdTime,modifiedTime,mimeType,appProperties,webViewLink,parents"


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveAdapter(StorageBackend):
    mode = "live"

    def __init__(self, service, root_name: str = ROOT_FOLDER_NAME, retry_attempts: int = 4,
                 retry_base_delay: float = 1.0):
        super().__init__()
        self.service = service
        self.root_name = root_name
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay
        self._folders: dict[str, str] = {}
        self._folder_paths: dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "GoogleDriveAdapter":
        from googleapiclient.discovery import build

        from isd_pipeline.auth import load_credentials
        creds = load_credentials(settings)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return cls(service, settings.root_folder_name, settings.retry_attempts, settings.retry_base_delay)

    def _exec(self, request, what: str):
        def go():
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001
                raise translate(exc, "Google Drive API") from exc
        return call_with_retry(go, attempts=self.retry_attempts, base_delay=self.retry_base_delay, what=what,
                               is_retriable=is_retriable)

    # ---------------------------------------------------------------- folders
    def _find_or_create_folder(self, name: str, parent: str, logical: str) -> str:
        q = (f"name = '{_q(name)}' and mimeType = '{FOLDER_MIME}' and '{parent}' in parents and trashed = false")
        found = self._exec(self.service.files().list(q=q, fields="files(id,name)", spaces="drive"), "Drive find folder")
        files = found.get("files", [])
        if files:
            return files[0]["id"]
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent],
                "appProperties": {"isd_folder": logical or "root"}}
        return self._exec(self.service.files().create(body=body, fields="id"), "Drive create folder")["id"]

    def ensure_folders(self) -> dict[str, str]:
        if self._folders:
            return dict(self._folders)
        root = self._find_or_create_folder(self.root_name, "root", "")
        ids = {"": root}
        for path in FOLDERS:
            parent_path, _, name = path.rpartition("/")
            ids[path] = self._find_or_create_folder(name, ids[parent_path], path)
        self._folders = {k: v for k, v in ids.items() if k}
        self._folder_paths = {v: k for k, v in self._folders.items()}
        return dict(self._folders)

    def _folder_id(self, folder: str) -> str:
        if folder not in self._folders:
            self.ensure_folders()
        if folder not in self._folders:
            raise StorageError(f"Unknown folder {folder!r}")
        return self._folders[folder]

    # ---------------------------------------------------------------- files
    def _to_stored(self, meta: dict, folder: str | None = None) -> StoredFile:
        props = meta.get("appProperties", {}) or {}
        if folder is None:
            parents = meta.get("parents") or [""]
            folder = self._folder_paths.get(parents[0], "")
        return StoredFile(file_id=meta["id"], name=meta["name"], folder=folder, size=int(meta.get("size", 0) or 0),
                          sha256=props.get("sha256", ""), created_time=meta.get("createdTime", ""),
                          modified_time=meta.get("modifiedTime", ""), mime_type=meta.get("mimeType", ""),
                          app_properties=dict(props), location=meta.get("webViewLink", ""))

    def _list(self, q: str, folder: str) -> list[StoredFile]:
        out, page = [], None
        while True:
            resp = self._exec(self.service.files().list(
                q=q, fields=f"nextPageToken,files({FILE_FIELDS})", orderBy="createdTime", pageSize=1000,
                pageToken=page, spaces="drive"), "Drive list")
            out += [self._to_stored(m, folder) for m in resp.get("files", [])]
            page = resp.get("nextPageToken")
            if not page:
                return out

    def list_files(self, folder: str) -> list[StoredFile]:
        fid = self._folder_id(folder)
        return self._list(f"'{fid}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'", folder)

    def find_files(self, folder: str, *, name: str | None = None,
                   app_properties: dict[str, str] | None = None) -> list[StoredFile]:
        """Server-side filter on name/appProperties (cheaper than listing the folder)."""
        fid = self._folder_id(folder)
        q = [f"'{fid}' in parents", "trashed = false"]
        if name is not None:
            q.append(f"name = '{_q(name)}'")
        for k, v in (app_properties or {}).items():
            q.append(f"appProperties has {{ key='{_q(k)}' and value='{_q(str(v))}' }}")
        return self._list(" and ".join(q), folder)

    def _media(self, data: bytes, mime_type: str):
        from googleapiclient.http import MediaIoBaseUpload
        return MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type or "application/octet-stream", resumable=False)

    def _create(self, folder, name, data, mime_type, app_properties) -> StoredFile:
        body = {"name": name, "parents": [self._folder_id(folder)], "appProperties": app_properties,
                "mimeType": mime_type}
        meta = self._exec(self.service.files().create(body=body, media_body=self._media(data, mime_type),
                                                      fields=FILE_FIELDS), "Drive upload")
        return self._to_stored(meta, folder)

    def update_file(self, file_id, data, app_properties=None) -> StoredFile:
        current = self.get_file(file_id)
        body = {"appProperties": {k: str(v) for k, v in (app_properties or {}).items()}} if app_properties else {}
        meta = self._exec(self.service.files().update(fileId=file_id, body=body,
                                                      media_body=self._media(data, current.mime_type),
                                                      fields=FILE_FIELDS), "Drive update")
        return self._to_stored(meta, current.folder)

    def read_file(self, file_id) -> bytes:
        # get_media().execute() returns the file bytes (files here are small CSV/JSON/XLSX).
        return self._exec(self.service.files().get_media(fileId=file_id), "Drive download")

    def get_file(self, file_id) -> StoredFile:
        self.ensure_folders()
        meta = self._exec(self.service.files().get(fileId=file_id, fields=FILE_FIELDS), "Drive get")
        return self._to_stored(meta)

    def describe(self) -> str:
        return f"live Google Drive (drive.file) / {self.root_name}"
