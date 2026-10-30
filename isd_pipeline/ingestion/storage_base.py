"""Storage interface (Google Drive is the durable store) + DataFrame helpers on top of it.

Folders are addressed by their logical path under the root folder, e.g.
"raw_reports/building" or "pipeline_logs". Adapters map those to Drive folder IDs
(live) or local directories (mock).
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd

from isd_pipeline.schema import from_csv_bytes, to_csv_bytes

ROOT_FOLDER_NAME = "Permit Inspection Automation"
FOLDERS: tuple[str, ...] = (
    "raw_reports",
    "raw_reports/building",
    "raw_reports/electric",
    "raw_reports/plumbing_gas",
    "processed_data",
    "historical_snapshots",
    "pipeline_logs",
    "dashboard_exports",
)
CSV_MIME = "text/csv"
JSON_MIME = "application/json"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    name: str
    folder: str                    # logical folder path, e.g. "processed_data"
    size: int
    sha256: str
    created_time: str              # ISO 8601 UTC
    modified_time: str
    mime_type: str = ""
    app_properties: dict[str, str] = field(default_factory=dict)
    location: str = ""             # local path (mock) or webViewLink (live)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StorageError(RuntimeError):
    pass


class StorageBackend(ABC):
    """Interface implemented by GoogleDriveAdapter (live) and MockDriveAdapter."""

    mode: str = "abstract"

    def __init__(self) -> None:
        self.files_created = 0     # counts create_file() calls on this instance (idempotency checks)

    # --- implemented by adapters ---
    @abstractmethod
    def ensure_folders(self) -> dict[str, str]:
        """Create the folder tree if needed. Returns logical path -> folder id."""

    @abstractmethod
    def list_files(self, folder: str) -> list[StoredFile]:
        """Files directly inside a logical folder (not recursive), oldest first."""

    @abstractmethod
    def _create(self, folder: str, name: str, data: bytes, mime_type: str,
                app_properties: dict[str, str]) -> StoredFile: ...

    @abstractmethod
    def update_file(self, file_id: str, data: bytes, app_properties: dict[str, str] | None = None) -> StoredFile:
        """Replace a file's content in place (same file id)."""

    @abstractmethod
    def read_file(self, file_id: str) -> bytes: ...

    @abstractmethod
    def get_file(self, file_id: str) -> StoredFile: ...

    def describe(self) -> str:
        return self.mode

    # --- shared behaviour ---
    def create_file(self, folder: str, name: str, data: bytes, mime_type: str = CSV_MIME,
                    app_properties: dict[str, str] | None = None) -> StoredFile:
        if folder not in FOLDERS:
            raise StorageError(f"Unknown folder {folder!r}; expected one of {FOLDERS}")
        props = {k: str(v) for k, v in (app_properties or {}).items()}
        props.setdefault("sha256", sha256_bytes(data))
        stored = self._create(folder, name, data, mime_type, props)
        self.files_created += 1
        return stored

    def find_files(self, folder: str, *, name: str | None = None,
                   app_properties: dict[str, str] | None = None) -> list[StoredFile]:
        out = []
        for f in self.list_files(folder):
            if name is not None and f.name != name:
                continue
            if app_properties and any(f.app_properties.get(k) != str(v) for k, v in app_properties.items()):
                continue
            out.append(f)
        return out

    def upsert_file(self, folder: str, name: str, data: bytes, mime_type: str = CSV_MIME,
                    app_properties: dict[str, str] | None = None) -> StoredFile:
        """Update the file with this name in place, or create it. Used for logs and exports
        (stable names), never for versioned datasets."""
        existing = self.find_files(folder, name=name)
        props = {k: str(v) for k, v in (app_properties or {}).items()}
        if existing:
            current = existing[-1]
            if current.sha256 == sha256_bytes(data):
                return current
            props["sha256"] = sha256_bytes(data)
            return self.update_file(current.file_id, data, props)
        return self.create_file(folder, name, data, mime_type, props)


# ---------------------------------------------------------------------------
# DataFrame helpers
# ---------------------------------------------------------------------------
def save_dataframe(storage: StorageBackend, folder: str, name: str, df: pd.DataFrame,
                   columns: dict[str, str] | None = None, app_properties: dict[str, str] | None = None,
                   *, versioned: bool = True) -> StoredFile:
    """Save a DataFrame as CSV. versioned=True always creates a new file (safe write);
    versioned=False updates the same-named file in place."""
    data = to_csv_bytes(df, columns)
    if versioned:
        return storage.create_file(folder, name, data, CSV_MIME, app_properties)
    return storage.upsert_file(folder, name, data, CSV_MIME, app_properties)


def read_dataframe(storage: StorageBackend, file_id: str, columns: dict[str, str] | None = None) -> pd.DataFrame:
    return from_csv_bytes(storage.read_file(file_id), columns)


def read_named_csv(storage: StorageBackend, folder: str, name: str,
                   columns: dict[str, str] | None = None) -> pd.DataFrame | None:
    """Read the (latest) file with this name, or None if it doesn't exist."""
    found = storage.find_files(folder, name=name)
    if not found:
        return None
    return read_dataframe(storage, found[-1].file_id, columns)
