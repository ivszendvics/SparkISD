"""Shared fixtures: one synthetic storyline per test session, copied into per-test mock inboxes."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

from generate_synthetic import generate  # noqa: E402

from isd_pipeline.factory import make_storage  # noqa: E402
from isd_pipeline.pipeline import run_daily_pipeline  # noqa: E402
from isd_pipeline.settings import load_settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _never_touch_live_google(tmp_path_factory):
    """Tests must never reach the real Gmail or Drive, whatever settings.yaml says.

    1. Every load_settings() without an explicit config_dir reads a copy of the config with
       mode: mock and sender.address: null (the shipped defaults).
    2. Loading the real OAuth files in secrets/ raises, as a backstop.
    """
    import yaml

    import isd_pipeline.auth as auth
    import isd_pipeline.settings as settings_mod

    cfg = tmp_path_factory.mktemp("config")
    shutil.copytree(settings_mod.DEFAULT_CONFIG_DIR, cfg, dirs_exist_ok=True)
    s = yaml.safe_load((cfg / "settings.yaml").read_text())
    s["mode"] = "mock"
    (cfg / "settings.yaml").write_text(yaml.safe_dump(s))
    r = yaml.safe_load((cfg / "email_rules.yaml").read_text())
    r.setdefault("sender", {})["address"] = None
    (cfg / "email_rules.yaml").write_text(yaml.safe_dump(r))

    real_secrets = (ROOT / "secrets").resolve()
    original = auth.load_credentials

    def guarded(settings=None):
        files = [] if settings is None else [settings.credentials_file, settings.token_file]
        if settings is None or any(Path(f).resolve().is_relative_to(real_secrets) for f in files):
            raise RuntimeError("Tests must not load the real Google credentials in secrets/")
        return original(settings)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings_mod, "DEFAULT_CONFIG_DIR", cfg)
        mp.setattr(auth, "load_credentials", guarded)
        yield


@pytest.fixture(scope="session")
def scripted_inbox(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("scripted") / "inbox"
    generate(out)
    return out


@pytest.fixture(scope="session")
def storyline(scripted_inbox) -> dict:
    return json.loads((scripted_inbox / "storyline.json").read_text())


class Env:
    """A mock inbox + mock Drive in tmp_path, with helpers to deliver days and run."""

    def __init__(self, tmp_path: Path, scripted: Path):
        self.scripted = scripted
        self.inbox = tmp_path / "inbox"
        self.drive = tmp_path / "drive"
        self.inbox.mkdir()
        self.settings = load_settings(mock_inbox_dir=self.inbox, mock_drive_dir=self.drive).with_overrides(
            retry_base_delay=0.0)
        self.storage = make_storage(self.settings)

    def deliver(self, *days: str) -> None:
        for d in days:
            dst = self.inbox / d
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(self.scripted / d, dst)

    def run(self, day: str, **kw):
        return run_daily_pipeline(date.fromisoformat(day), settings=self.settings, storage=self.storage,
                                  trigger="test", retry_sleep=lambda s: None, **kw)

    def deliver_and_run(self, *days: str):
        results = []
        for d in days:
            self.deliver(d)
            results.append(self.run(d))
        return results

    def drive_file_count(self) -> int:
        idx = self.drive / ".drive_index.json"
        return len(json.loads(idx.read_text())["files"]) if idx.exists() else 0


@pytest.fixture
def env(tmp_path, scripted_inbox) -> Env:
    return Env(tmp_path, scripted_inbox)
