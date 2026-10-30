"""Module 1 integration tests (mock Gmail + mock Drive)."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from isd_pipeline import pipeline as pipeline_mod
from isd_pipeline.ingestion.ingestion_log import read_log
from isd_pipeline.ingestion.manifest import load_latest_dataset, read_manifest
from isd_pipeline.ingestion.mock_gmail_adapter import MockGmailAdapter
from isd_pipeline.ingestion.snapshots import list_report_dates, load_snapshots
from isd_pipeline.api import save_to_google_drive


def _processed_files(env) -> dict[str, bytes]:
    return {f.file_id: env.storage.read_file(f.file_id) for f in env.storage.list_files("processed_data")}


def test_i1_three_emails_three_divisions_by_subject(env):
    env.deliver("2026-10-05")
    # Gmail truncates attachment names; the division must come from the subject.
    for meta_path in (env.inbox / "2026-10-05").glob("*.json"):
        meta = json.loads(meta_path.read_text())
        meta["attachments"][0]["filename"] = "Permit Inspecti….xlsx"
        meta_path.write_text(json.dumps(meta))
    r = env.run("2026-10-05")
    assert r.status == "SUCCESS"
    assert {d: x.status for d, x in r.divisions.items()} == {
        "Building": "PROCESSED", "Electric": "PROCESSED", "Plumbing/Gas": "PROCESSED"}
    snaps = load_snapshots(storage=env.storage)
    assert set(snaps["division"]) == {"Building", "Electric", "Plumbing/Gas"}
    for div, folder in [("Building", "building"), ("Electric", "electric"), ("Plumbing/Gas", "plumbing_gas")]:
        raws = env.storage.list_files(f"raw_reports/{folder}")
        assert len(raws) == 1 and raws[0].app_properties["division"] == div
        assert raws[0].name.startswith(f"2026-10-05_{folder}_")
        assert raws[0].app_properties["sha256"] and raws[0].app_properties["email_id"]
    assert len(load_latest_dataset(env.storage, "requests_current")) == len(snaps)


def test_i2_second_day_preserves_first_day_snapshots(env):
    r1, = env.deliver_and_run("2026-10-05")
    day1 = load_snapshots(date(2026, 10, 5), date(2026, 10, 5), storage=env.storage)
    env.deliver_and_run("2026-10-06")
    assert list_report_dates(storage=env.storage) == [date(2026, 10, 5), date(2026, 10, 6)]
    again = load_snapshots(date(2026, 10, 5), date(2026, 10, 5), storage=env.storage)
    pd.testing.assert_frame_equal(day1, again)
    assert len(day1) == r1.snapshot_rows_ingested
    both = load_snapshots(storage=env.storage)
    cur = load_latest_dataset(env.storage, "requests_current")
    assert len(both) > len(cur)                     # requests seen on both days collapsed


def test_i3_rerun_creates_nothing(env):
    env.deliver_and_run("2026-10-05")
    files_before = env.drive_file_count()
    cur_before = load_latest_dataset(env.storage, "requests_current")
    manifest_before = read_manifest(env.storage)
    r = env.run("2026-10-05")
    assert r.status == "SUCCESS" and r.nothing_new
    assert r.new_files == 0 and r.new_ids == []
    assert env.drive_file_count() == files_before
    assert read_manifest(env.storage)["batch_id"] == manifest_before["batch_id"]
    pd.testing.assert_frame_equal(cur_before, load_latest_dataset(env.storage, "requests_current"))
    assert set(r.divisions[d].status for d in r.divisions) == {"ALREADY_PROCESSED"}


def test_i4_missing_division_partial_success(env):
    env.deliver_and_run("2026-10-19")
    r, = env.deliver_and_run("2026-10-20")          # Electric email never arrived
    assert r.status == "PARTIAL_SUCCESS"
    assert r.problem_divisions == ["Electric"] and r.divisions["Electric"].status == "MISSING"
    assert list_report_dates("Building", env.storage)[-1] == date(2026, 10, 20)
    assert list_report_dates("Plumbing/Gas", env.storage)[-1] == date(2026, 10, 20)
    assert list_report_dates("Electric", env.storage)[-1] == date(2026, 10, 19)
    assert r.manifest_updated


def test_i5_invalid_file_alone_changes_nothing(env):
    env.deliver_and_run("2026-10-14")
    manifest_before = read_manifest(env.storage)
    processed_before = _processed_files(env)
    env.deliver("2026-10-15")
    for slug in ("building", "electric"):          # only the malformed Plumbing/Gas email arrives
        (env.inbox / "2026-10-15" / f"{slug}.json").unlink()
    r = env.run("2026-10-15")
    assert r.status == "FAILURE"
    assert r.divisions["Plumbing/Gas"].status == "INVALID"
    assert "call_datetime" in r.divisions["Plumbing/Gas"].message
    assert read_manifest(env.storage) == manifest_before
    assert _processed_files(env) == processed_before
    log = read_log(env.storage, "ingestion_log.csv")
    assert ((log["division"] == "Plumbing/Gas") & (log["status"] == "INVALID")).any()
    assert len(env.storage.list_files("raw_reports/plumbing_gas")) == 2   # raw file kept for audit


def test_i5_invalid_division_does_not_corrupt_others(env):
    env.deliver_and_run("2026-10-14")
    plumbing_before = load_latest_dataset(env.storage, "requests_current")
    plumbing_before = plumbing_before[plumbing_before["division"] == "Plumbing/Gas"]
    r, = env.deliver_and_run("2026-10-15")
    assert r.status == "PARTIAL_SUCCESS" and r.problem_divisions == ["Plumbing/Gas"]
    after = load_latest_dataset(env.storage, "requests_current")
    after = after[after["division"] == "Plumbing/Gas"].set_index("inspection_request_id")
    before = plumbing_before.set_index("inspection_request_id")
    assert set(before.index) == set(after.index)
    pd.testing.assert_series_equal(before["last_seen_report_date"], after.loc[before.index, "last_seen_report_date"])
    cls = load_latest_dataset(env.storage, "requests_classified")
    pg = cls[cls["division"] == "Plumbing/Gas"]
    assert (pg["as_of"] == pd.Timestamp("2026-10-14")).all()   # per-division as_of


def test_duplicate_email_same_day_keeps_latest(env):
    r, = env.deliver_and_run("2026-10-08")
    resend = json.loads((env.inbox / "2026-10-08" / "building_resend.json").read_text())
    assert r.divisions["Building"].email_id == resend["email_id"]
    log = read_log(env.storage, "ingestion_log.csv")
    assert (log["status"] == "DUPLICATE_SAME_DAY").sum() == 1
    assert any("kept the latest" in w for w in r.warnings)


def test_api_error_fails_only_that_division(env):
    env.deliver("2026-10-05")
    src = MockGmailAdapter(env.inbox, fail_subjects={"Permit Inspection Request - Electric Summary"})
    r = pipeline_mod.run_daily_pipeline(date(2026, 10, 5), settings=env.settings, storage=env.storage,
                                        email_source=src, retry_sleep=lambda s: None)
    assert r.status == "PARTIAL_SUCCESS" and r.divisions["Electric"].status == "ERROR"
    assert r.divisions["Building"].status == "PROCESSED"


def test_transient_api_error_is_retried(env):
    env.deliver("2026-10-05")
    src = MockGmailAdapter(env.inbox, fail_subjects={"Permit Inspection Request - Electric Summary"}, fail_times=2)
    r = pipeline_mod.run_daily_pipeline(date(2026, 10, 5), settings=env.settings, storage=env.storage,
                                        email_source=src, retry_sleep=lambda s: None)
    assert r.status == "SUCCESS"


def test_unexpected_sender_ignored(env):
    from dataclasses import replace
    env.deliver("2026-10-05")
    env.settings = env.settings.with_overrides(
        email_rules=replace(env.settings.email_rules, sender_address="real-powerbi@example.com"))
    r = env.run("2026-10-05")
    # The mock emails come from a different address -> no matches (adapter filters on from:).
    assert r.status == "FAILURE" and set(x.status for x in r.divisions.values()) == {"MISSING"}


def test_no_attachment_and_non_excel(env):
    env.deliver("2026-10-05")
    day = env.inbox / "2026-10-05"
    b = json.loads((day / "building.json").read_text())
    b["attachments"][0]["filename"] = "report.pdf"
    (day / "building.json").write_text(json.dumps(b))
    e = json.loads((day / "electric.json").read_text())
    e["attachments"] = []
    (day / "electric.json").write_text(json.dumps(e))
    env.settings = env.settings.with_overrides(
        email_rules=__import__("dataclasses").replace(env.settings.email_rules, has_attachment=False))
    r = env.run("2026-10-05")
    assert r.divisions["Building"].status == "NOT_EXCEL"
    assert r.divisions["Electric"].status == "NO_ATTACHMENT"
    assert r.status == "PARTIAL_SUCCESS"


def test_failure_after_ingestion_leaves_manifest_and_retries(env, monkeypatch):
    env.deliver_and_run("2026-10-05")
    manifest_before = read_manifest(env.storage)
    env.deliver("2026-10-06")

    def boom(*a, **k):
        raise RuntimeError("simulated crash in late logic")
    monkeypatch.setattr(pipeline_mod, "classify_requests", boom)
    r = env.run("2026-10-06")
    assert r.status == "FAILURE" and not r.manifest_updated
    assert read_manifest(env.storage) == manifest_before
    monkeypatch.undo()
    r2 = env.run("2026-10-06")                        # attachments were not marked processed
    assert r2.status == "SUCCESS" and r2.manifest_updated and not r2.nothing_new
    assert all(d.snapshot_created is False for d in r2.divisions.values())   # snapshots reused


def test_save_to_google_drive_and_versions(env):
    env.storage.ensure_folders()
    df = pd.DataFrame({"ap_number": ["0012"], "n": [1]})
    f1 = save_to_google_drive(df, {"folder": "processed_data", "name": "x.csv"}, storage=env.storage)
    f2 = save_to_google_drive(df, {"folder": "processed_data", "name": "x.csv"}, storage=env.storage)
    assert f1.file_id != f2.file_id and f1.sha256 == f2.sha256
    f3 = save_to_google_drive(df, {"folder": "pipeline_logs", "name": "y.csv", "versioned": False}, storage=env.storage)
    f4 = save_to_google_drive(df.assign(n=2), {"folder": "pipeline_logs", "name": "y.csv", "versioned": False},
                              storage=env.storage)
    assert f3.file_id == f4.file_id and f3.sha256 != f4.sha256
