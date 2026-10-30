"""Module 2 tests (D1-D5 + update rules)."""
from __future__ import annotations

import pandas as pd

from isd_pipeline.dedupe.matching import build_match_key, normalize_address
from isd_pipeline.dedupe.update import update_requests
from isd_pipeline.settings import load_settings
from tests.helpers import snap, snaps

ABBR = load_settings().address_abbreviations


def run(rows, current=None, registry=None, batch="B1"):
    return update_requests(current, registry, snaps(rows), batch, abbreviations=ABBR)


def test_d1_three_reports_one_id_latest_notes():
    rows = [
        snap("2026-10-06", "2026-10-05 09:14", "ALT1", notes="N/A", address="88 Dorchester Avenue"),
        snap("2026-10-07", "2026-10-05 09:14", "ALT1", notes="Left voicemail", address="88 DORCHESTER AVE"),
        snap("2026-10-08", "2026-10-05 09:14", "ALT1", notes="Scheduled 10/13", address="88  Dorchester Ave."),
    ]
    res = run(rows)
    cur = res.requests_current
    assert len(cur) == 1
    r = cur.iloc[0]
    assert r["inspection_request_id"] == "ISD-000001"
    assert r["n_reports"] == 3
    assert r["inspector_notes"] == "Scheduled 10/13"
    assert r["first_note_date"] == pd.Timestamp("2026-10-07")     # N/A on 10/06 doesn't count
    assert r["first_seen_report_date"] == pd.Timestamp("2026-10-06")
    assert r["last_seen_report_date"] == pd.Timestamp("2026-10-08")
    assert r["call_datetime"] == pd.Timestamp("2026-10-05 09:14")
    assert r["address"] == "88 Dorchester Avenue"                   # original text kept
    assert res.stats.duplicates_collapsed_this_run == 2


def test_d2_same_ap_different_type_or_call_time_separate_ids():
    rows = [
        snap("2026-10-14", "2026-10-13 10:02", "E205", insp_type="Rough Wiring", division="Electric"),
        snap("2026-10-14", "2026-10-13 10:02", "E205", insp_type="Service Upgrade", division="Electric"),
        snap("2026-10-16", "2026-10-15 08:40", "E205", insp_type="Rough Wiring", division="Electric"),
    ]
    cur = run(rows).requests_current
    assert len(cur) == 3
    assert cur["inspection_request_id"].nunique() == 3
    assert not cur["review_flag"].any()


def test_d3_new_inspection_gets_new_id():
    first = run([snap("2026-10-06", "2026-10-05 09:00", "ALT1")])
    second = run([snap("2026-10-07", "2026-10-05 09:00", "ALT1"), snap("2026-10-07", "2026-10-06 11:00", "ALT2")],
                 first.requests_current, first.id_registry, "B2")
    cur = second.requests_current.set_index("ap_number")
    assert len(cur) == 2
    assert cur.loc["ALT2", "inspection_request_id"] == "ISD-000002"
    assert cur.loc["ALT2", "dedupe_mark"] == "NEW"
    assert second.new_ids == ["ISD-000002"]


def test_d4_reprocessing_is_idempotent():
    rows = [snap("2026-10-06", "2026-10-05 09:00", "ALT1", notes="x"),
            snap("2026-10-07", "2026-10-05 09:00", "ALT1", notes="y"),
            snap("2026-10-07", "2026-10-06 10:00", "ALT2")]
    a = run(rows)
    b = run(rows, a.requests_current, a.id_registry, "B2")
    assert len(b.requests_current) == len(a.requests_current) == 2
    assert len(b.id_registry) == len(a.id_registry) == 2
    assert b.new_ids == []
    assert list(b.requests_current["n_reports"]) == list(a.requests_current["n_reports"]) == [2, 1]
    assert set(b.requests_current["dedupe_mark"]) == {"UNCHANGED"}


def test_d5_missing_key_field_flagged_not_merged():
    rows = [snap("2026-10-16", "2026-10-15 10:45", "", address="560 Columbia Rd", row=7),
            snap("2026-10-16", "2026-10-15 10:45", "", address="560 Columbia Rd", row=8)]
    res = run(rows)
    cur = res.requests_current
    assert len(cur) == 2                                  # never merged, even with each other
    assert cur["review_flag"].all()
    assert cur["review_reason"].str.contains("ap_number").all()
    assert set(cur["dedupe_mark"]) == {"FLAGGED"}
    again = run(rows, cur, res.id_registry, "B2")         # same file again -> same IDs
    assert again.new_ids == [] and len(again.requests_current) == 2


def test_d5_conflicting_address_flagged_with_own_id():
    rows = [snap("2026-10-15", "2026-10-14 08:15", "E2060001", address="45 Tremont St"),
            snap("2026-10-16", "2026-10-14 08:15", "E2060001", address="45 Tremont St"),
            snap("2026-10-16", "2026-10-14 08:15", "E2060001", address="47 Tremont St", row=9)]
    cur = run(rows).requests_current.set_index("address")
    assert len(cur) == 2
    assert not cur.loc["45 Tremont St", "review_flag"]
    assert cur.loc["47 Tremont St", "review_flag"]
    assert "ISD-000001" in cur.loc["47 Tremont St", "review_reason"]


def test_blank_never_overwrites_value():
    a = run([snap("2026-10-06", "2026-10-05 09:00", "ALT1", notes="Called owner", request_notes="Rear door",
                  contact="(617) 555-0100")])
    b = run([snap("2026-10-07", "2026-10-05 09:00", "ALT1", notes="", request_notes="", contact="")],
            a.requests_current, a.id_registry, "B2")
    r = b.requests_current.iloc[0]
    assert (r["inspector_notes"], r["request_notes"], r["contact_info"]) == ("Called owner", "Rear door", "(617) 555-0100")
    assert r["n_reports"] == 2


def test_late_arriving_older_report_does_not_overwrite_newer_fields():
    a = run([snap("2026-10-08", "2026-10-05 09:00", "ALT1", notes="Newest note", red=False)])
    b = run([snap("2026-10-06", "2026-10-05 09:00", "ALT1", notes="Older note", red=True)],
            a.requests_current, a.id_registry, "B2")
    r = b.requests_current.iloc[0]
    assert r["inspector_notes"] == "Newest note"
    assert r["first_seen_report_date"] == pd.Timestamp("2026-10-06")     # tracking still updated
    assert r["last_seen_report_date"] == pd.Timestamp("2026-10-08")
    assert r["first_note_date"] == pd.Timestamp("2026-10-06")
    assert r["ever_red"] and r["n_reports"] == 2


def test_updated_mark_when_notes_change():
    a = run([snap("2026-10-06", "2026-10-05 09:00", "ALT1", notes="")])
    b = run([snap("2026-10-07", "2026-10-05 09:00", "ALT1", notes="Left voicemail")], a.requests_current, a.id_registry)
    assert b.requests_current.iloc[0]["dedupe_mark"] == "UPDATED"
    c = run([snap("2026-10-08", "2026-10-05 09:00", "ALT1", notes="Left voicemail")], b.requests_current, b.id_registry)
    assert c.requests_current.iloc[0]["dedupe_mark"] == "UNCHANGED"


def test_deterministic_ids_regardless_of_input_order():
    rows = [snap("2026-10-06", "2026-10-05 11:00", "B"), snap("2026-10-06", "2026-10-05 09:00", "A"),
            snap("2026-10-06", "2026-10-05 09:00", "C", division="Electric")]
    a = run(rows).requests_current.set_index("ap_number")["inspection_request_id"]
    b = run(list(reversed(rows))).requests_current.set_index("ap_number")["inspection_request_id"]
    assert a.to_dict() == b.to_dict() == {"A": "ISD-000001", "B": "ISD-000002", "C": "ISD-000003"}


def test_key_normalization():
    assert normalize_address("  88 dorchester   Avenue ", ABBR) == "88 DORCHESTER AVE"
    k1 = build_match_key(snap("2026-10-06", "2026-10-05 09:14:00", " alt1 ", insp_type="final  building"), ABBR)
    k2 = build_match_key(snap("2026-10-07", "2026-10-05 09:14:00", "ALT1", insp_type="Final Building"), ABBR)
    k3 = build_match_key(snap("2026-10-07", "2026-10-05 09:14:01", "ALT1", insp_type="Final Building"), ABBR)
    assert k1 == k2 and k1 != k3                       # seconds are kept
    assert "BUILDING" in k1.split("|")[-1]             # work group falls back to the division
