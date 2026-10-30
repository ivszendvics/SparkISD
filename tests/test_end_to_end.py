"""End to end: generate the synthetic storyline -> run every day -> final KPIs match the stored
snapshot (tests/expected/e2e_kpis.json) and every scripted scenario lands where it should.

If you change the generator or a business rule ON PURPOSE, regenerate the snapshot with
    UPDATE_SNAPSHOT=1 pytest tests/test_end_to_end.py
and review the diff of e2e_kpis.json before committing it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from isd_pipeline.ingestion.manifest import load_kpi_bundle, load_latest_dataset

EXPECTED = Path(__file__).parent / "expected" / "e2e_kpis.json"


@pytest.fixture(scope="module")
def full_run(tmp_path_factory, scripted_inbox, storyline):
    from tests.conftest import Env
    env = Env(tmp_path_factory.mktemp("e2e"), scripted_inbox)
    results = env.deliver_and_run(*storyline["report_days"])
    return env, results


def _kpi_snapshot(env) -> dict:
    t = load_kpi_bundle(env.storage)
    s = t["on_time_summary"].iloc[0]
    keep = ["on_time_pct", "numerator", "denominator", "on_time", "on_time_contacted", "late", "open_overdue",
            "open_not_due", "invalid", "open_overdue_backlog", "median_business_days_late_backlog",
            "font_mismatches", "ever_red_requests", "review_queue", "dq_flagged_requests", "unique_requests",
            "snapshot_rows_ingested", "duplicates_collapsed", "latest_as_of"]
    by_div = t["on_time_by_division"][["division", "on_time_pct", "numerator", "denominator"]]
    return {
        "summary": {k: (s[k].item() if hasattr(s[k], "item") else s[k]) for k in keep},
        "by_division": by_div.to_dict("records"),
        "mismatch_by_reason": t["mismatch_by_reason"].to_dict("records"),
        "dq_summary": t["dq_summary"].to_dict("records"),
    }


def test_run_statuses(full_run, storyline):
    env, results = full_run
    by_day = {r.report_date.isoformat(): r for r in results}
    assert by_day[storyline["events"]["malformed_plumbing"]].status == "PARTIAL_SUCCESS"
    assert by_day[storyline["events"]["malformed_plumbing"]].problem_divisions == ["Plumbing/Gas"]
    assert by_day[storyline["events"]["electric_missing"]].problem_divisions == ["Electric"]
    others = [r for d, r in by_day.items() if d not in storyline["events"].values() or
              d in (storyline["events"]["duplicate_building_email"], storyline["events"]["side_by_side_electric"])]
    assert all(r.status == "SUCCESS" for r in others)
    assert all(not r.errors for r in results)


def test_final_kpis_match_snapshot(full_run):
    env, _ = full_run
    got = json.loads(json.dumps(_kpi_snapshot(env), default=str))
    if os.environ.get("UPDATE_SNAPSHOT") or not EXPECTED.exists():
        EXPECTED.parent.mkdir(exist_ok=True)
        EXPECTED.write_text(json.dumps(got, indent=2, sort_keys=True))
        if not os.environ.get("UPDATE_SNAPSHOT"):
            pytest.fail("Snapshot did not exist; it was created. Review it and re-run.")
    assert got == json.loads(EXPECTED.read_text())


def test_scripted_scenarios(full_run, storyline):
    env, _ = full_run
    cls = load_latest_dataset(env.storage, "requests_classified")
    for sc in storyline["scenarios"]:
        m = cls[(cls["ap_number"] == sc["ap_number"]) & (cls["insp_type"] == sc["insp_type"])
                & (cls["division"] == sc["division"]) & (cls["call_datetime"] == pd.Timestamp(sc["call_datetime"]))]
        if sc["scenario"].startswith("conflict"):
            addr_ok = m["address"].str.startswith("47" if sc["scenario"] == "conflict_address" else "45")
            m = m[addr_ok]
        assert len(m) == 1, sc["scenario"]
        r = m.iloc[0]
        # Days whose report for this division was rejected (malformed) or never arrived don't count.
        lost = {"Plumbing/Gas": storyline["events"]["malformed_plumbing"],
                "Electric": storyline["events"]["electric_missing"]}.get(sc["division"])
        expected_reports = len([d for d in sc["report_days"] if d != lost])
        assert r["n_reports"] == expected_reports, sc["scenario"]
        for k, v in sc["expected"].items():
            actual = r[k]
            if isinstance(actual, pd.Timestamp):
                actual = actual.strftime("%Y-%m-%d")
            assert actual == v or str(actual) == str(v), (sc["scenario"], k, actual, v)


def test_one_row_per_request_and_unique_ids(full_run):
    env, _ = full_run
    cur = load_latest_dataset(env.storage, "requests_current")
    reg = load_latest_dataset(env.storage, "id_registry")
    assert cur["inspection_request_id"].is_unique and cur["match_key"].is_unique
    assert len(reg) == len(cur) and set(reg["inspection_request_id"]) == set(cur["inspection_request_id"])


def test_rerunning_every_day_creates_nothing(full_run, storyline):
    env, _ = full_run
    before = env.drive_file_count()
    for d in storyline["report_days"]:
        r = env.run(d)
        assert r.new_files == 0 and r.new_ids == [] and r.nothing_new
    assert env.drive_file_count() == before


def test_rebuild_from_scratch_gives_same_ids(full_run, tmp_path, scripted_inbox, storyline):
    """Same reports, fresh Drive -> identical IDs for identical requests."""
    from tests.conftest import Env
    env, _ = full_run
    env2 = Env(tmp_path, scripted_inbox)
    env2.deliver_and_run(*storyline["report_days"])
    a = load_latest_dataset(env.storage, "requests_current").set_index("match_key")["inspection_request_id"]
    b = load_latest_dataset(env2.storage, "requests_current").set_index("match_key")["inspection_request_id"]
    unmatched = [k for k in a.index if k.startswith("UNMATCHED")]   # tied to email ids, same here too
    assert a.drop(unmatched).sort_index().equals(b.drop(unmatched).sort_index())


def test_exports_have_provenance_and_no_contacts(full_run):
    env, _ = full_run
    files = {f.name: f for f in env.storage.list_files("dashboard_exports")}
    for name in ["requests_classified.csv", "on_time_summary.csv", "on_time_by_division.csv", "on_time_by_inspector.csv",
                 "on_time_by_call_month.csv", "on_time_by_insp_type.csv", "on_time_by_ward.csv",
                 "on_time_by_inspector_month.csv", "backlog.csv", "font_mismatches.csv", "data_quality.csv",
                 "review_queue.csv", "run_history.csv"]:
        assert name in files, name
        df = pd.read_csv(files[name].location, dtype=str, keep_default_na=False)
        assert {"ingestion_batch_id", "export_batch_id", "export_source_files"} <= set(df.columns), name
        assert "contact_name" not in df.columns and "contact_info" not in df.columns, name
    dq = pd.read_csv(files["data_quality.csv"].location, dtype=str, keep_default_na=False)
    assert {"dq_flag", "invalid_row", "report_problem"} <= set(dq["issue_type"])
    assert set(dq.loc[dq["issue_type"] == "report_problem", "issue"]) >= {"MISSING", "INVALID", "DUPLICATE_SAME_DAY"}


def test_reclassify_reuses_requests_and_matches_last_run(full_run):
    from isd_pipeline.ingestion.manifest import read_manifest
    from isd_pipeline.pipeline import reclassify
    env, results = full_run
    before = read_manifest(env.storage)
    out = reclassify(settings=env.settings, storage=env.storage)
    after = read_manifest(env.storage)
    assert after["batch_id"] == out["batch_id"] and after["trigger"] == "reclassify"
    assert after["datasets"]["requests_current"] == before["datasets"]["requests_current"]
    assert after["datasets"]["requests_classified"]["file_id"] != before["datasets"]["requests_classified"]["file_id"]
    assert out["on_time_pct"] == results[-1].kpis["on_time_pct"]
