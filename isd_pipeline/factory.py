"""Pick mock or live adapters from settings.mode. Live modules are imported lazily so mock
mode never needs Google credentials (or even the Google libraries) to run."""
from __future__ import annotations

from isd_pipeline.ingestion.email_base import EmailSource
from isd_pipeline.ingestion.storage_base import StorageBackend
from isd_pipeline.settings import Settings, load_settings


def make_email_source(settings: Settings | None = None) -> EmailSource:
    settings = settings or load_settings()
    if settings.is_mock:
        from isd_pipeline.ingestion.mock_gmail_adapter import MockGmailAdapter
        return MockGmailAdapter(settings.mock_inbox_dir)
    from isd_pipeline.ingestion.gmail_adapter import GmailAdapter
    return GmailAdapter.from_settings(settings)


def make_storage(settings: Settings | None = None) -> StorageBackend:
    settings = settings or load_settings()
    if settings.is_mock:
        from isd_pipeline.ingestion.mock_gdrive_adapter import MockDriveAdapter
        return MockDriveAdapter(settings.mock_drive_dir, settings.root_folder_name)
    from isd_pipeline.ingestion.gdrive_adapter import GoogleDriveAdapter
    return GoogleDriveAdapter.from_settings(settings)
