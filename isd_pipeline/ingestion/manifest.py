"""pipeline_logs/manifest.json - the pointer to the latest successful dataset files.

Safe-write rule: every run writes its processed datasets as NEW files, and only then
updates the manifest (the very last storage step). A run that fails before that point
leaves the manifest - and therefore everything `load_latest_dataset()` returns - untouched.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from isd_pipeline.ingestion.storage_base import JSON_MIME, StorageBackend, StoredFile, read_dataframe
from isd_pipeline.schema import CLASSIFIED_COLUMNS, ID_REGISTRY_COLUMNS, REQUEST_COLUMNS, from_csv_bytes

MANIFEST_FOLDER = "pipeline_logs"
MANIFEST_NAME = "manifest.json"

DATASET_SCHEMAS: dict[str, dict[str, str]] = {
    "requests_current": REQUEST_COLUMNS,
    "id_registry": ID_REGISTRY_COLUMNS,
    "requests_classified": CLASSIFIED_COLUMNS,
}


class DatasetNotFound(LookupError):
    pass


def read_manifest(storage: StorageBackend) -> dict[str, Any]:
    found = storage.find_files(MANIFEST_FOLDER, name=MANIFEST_NAME)
    if not found:
        return {}
    return json.loads(storage.read_file(found[-1].file_id) or b"{}")


def write_manifest(storage: StorageBackend, manifest: dict[str, Any]) -> StoredFile:
    manifest = dict(manifest)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode()
    return storage.upsert_file(MANIFEST_FOLDER, MANIFEST_NAME, data, JSON_MIME, {"kind": "manifest"})


def file_entry(f: StoredFile, **extra: Any) -> dict[str, Any]:
    return {"file_id": f.file_id, "name": f.name, "folder": f.folder, "sha256": f.sha256,
            "created_time": f.created_time, **extra}


def load_latest_dataset(storage: StorageBackend, name: str = "requests_current") -> pd.DataFrame:
    """Read a dataset through the manifest.

    `name` is one of requests_current, id_registry, requests_classified, or the name of an
    aggregate table (e.g. on_time_summary, on_time_by_division, backlog).
    """
    manifest = read_manifest(storage)
    datasets = manifest.get("datasets", {})
    if name in datasets:
        return read_dataframe(storage, datasets[name]["file_id"], DATASET_SCHEMAS.get(name))
    kpis = datasets.get("kpis")
    if kpis:
        bundle = json.loads(storage.read_file(kpis["file_id"]))
        if name in bundle:
            return pd.DataFrame.from_records(bundle[name])
    if not manifest:
        raise DatasetNotFound("No manifest yet - run the pipeline at least once.")
    raise DatasetNotFound(f"Dataset {name!r} is not in the manifest (have: {sorted(datasets)})")


def load_kpi_bundle(storage: StorageBackend) -> dict[str, pd.DataFrame]:
    manifest = read_manifest(storage)
    kpis = manifest.get("datasets", {}).get("kpis")
    if not kpis:
        return {}
    bundle = json.loads(storage.read_file(kpis["file_id"]))
    return {k: pd.DataFrame.from_records(v) for k, v in bundle.items()}


def read_csv_by_id(storage: StorageBackend, file_id: str) -> pd.DataFrame:
    return from_csv_bytes(storage.read_file(file_id))
