"""Local folder that behaves like the Google Drive folder tree.

Layout on disk:
    <mock_drive_dir>/Permit Inspection Automation/<folder>/<file name>   real files you can open
    <mock_drive_dir>/.drive_index.json                                   file ids + metadata

Like Drive, every file has an opaque id, appProperties, created/modified times, and two
files may share a name (the on-disk copy then gets an id suffix).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from isd_pipeline.ingestion.storage_base import (
    FOLDERS, ROOT_FOLDER_NAME, StorageBackend, StorageError, StoredFile, sha256_bytes,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class MockDriveAdapter(StorageBackend):
    mode = "mock"

    def __init__(self, base_dir: Path | str, root_name: str = ROOT_FOLDER_NAME):
        super().__init__()
        self.base_dir = Path(base_dir)
        self.root_name = root_name
        self.root = self.base_dir / root_name
        self.index_path = self.base_dir / ".drive_index.json"

    # --- index persistence ---
    def _load(self) -> dict:
        if not self.index_path.exists():
            return {"folders": {}, "files": {}}
        return json.loads(self.index_path.read_text())

    def _save(self, index: dict) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=1, sort_keys=True))
        os.replace(tmp, self.index_path)   # atomic, like a Drive API call either happening or not

    @staticmethod
    def _to_stored(meta: dict) -> StoredFile:
        return StoredFile(
            file_id=meta["id"], name=meta["name"], folder=meta["folder"], size=meta["size"],
            sha256=meta["appProperties"].get("sha256", ""), created_time=meta["createdTime"],
            modified_time=meta["modifiedTime"], mime_type=meta["mimeType"],
            app_properties=dict(meta["appProperties"]), location=meta["path"],
        )

    # --- interface ---
    def ensure_folders(self) -> dict[str, str]:
        index = self._load()
        changed = False
        for folder in FOLDERS:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
            if folder not in index["folders"]:
                index["folders"][folder] = "fld_" + uuid.uuid4().hex[:12]
                changed = True
        if changed:
            self._save(index)
        return dict(index["folders"])

    def list_files(self, folder: str) -> list[StoredFile]:
        index = self._load()
        metas = [m for m in index["files"].values() if m["folder"] == folder]
        metas.sort(key=lambda m: (m["createdTime"], m["id"]))
        return [self._to_stored(m) for m in metas]

    def _create(self, folder, name, data, mime_type, app_properties) -> StoredFile:
        index = self._load()
        if folder not in index["folders"]:
            self.ensure_folders()
            index = self._load()
        file_id = "mock_" + uuid.uuid4().hex[:20]
        path = self.root / folder / name
        if path.exists():  # Drive allows duplicate names; keep both on disk
            path = path.with_name(f"{path.stem}__{file_id[-6:]}{path.suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        now = _now()
        meta = {
            "id": file_id, "name": name, "folder": folder, "mimeType": mime_type, "size": len(data),
            "createdTime": now, "modifiedTime": now, "appProperties": dict(app_properties),
            "path": str(path), "parents": [index["folders"][folder]],
        }
        index["files"][file_id] = meta
        self._save(index)
        return self._to_stored(meta)

    def update_file(self, file_id, data, app_properties=None) -> StoredFile:
        index = self._load()
        meta = index["files"].get(file_id)
        if meta is None:
            raise StorageError(f"File {file_id} not found")
        Path(meta["path"]).write_bytes(data)
        meta["size"] = len(data)
        meta["modifiedTime"] = _now()
        props = dict(meta["appProperties"])
        props.update({k: str(v) for k, v in (app_properties or {}).items()})
        props["sha256"] = sha256_bytes(data)
        meta["appProperties"] = props
        self._save(index)
        return self._to_stored(meta)

    def read_file(self, file_id) -> bytes:
        meta = self._load()["files"].get(file_id)
        if meta is None:
            raise StorageError(f"File {file_id} not found")
        return Path(meta["path"]).read_bytes()

    def get_file(self, file_id) -> StoredFile:
        meta = self._load()["files"].get(file_id)
        if meta is None:
            raise StorageError(f"File {file_id} not found")
        return self._to_stored(meta)

    def describe(self) -> str:
        return f"mock Drive at {self.root}"

    # --- demo helper ---
    def folder_ids(self) -> dict[str, str]:
        return dict(self._load()["folders"])
