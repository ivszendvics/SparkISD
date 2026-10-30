"""Module 3 tests. Expected dates are worked out by hand in the tables below, independent
of the implementation (Oct 2026: Mon 10/12 and Wed 11/11 and Thu 11/26 are holidays)."""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from isd_pipeline.late_logic.aggregate import aggregate
from isd_pipeline.late_logic.calendar import BusinessCalendar, HolidayYearMissingError
from isd_pipeline.late_logic.rules import classify_requests, default_config, explain_status
from isd_pipeline.notes import is_meaningful_note
from tests.helpers import request, requests_frame

CAL = BusinessCalendar.from_csv()
CFG = default_config()

# Business report days in the test window (10/12 is a holiday but reports still arrive).
OCT_REPORTS = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
ALL_DIVS = {"Building": OCT_REPORTS, "Electric": OCT_REPORTS, "Plumbing/Gas": OCT_REPORTS}

DUE_DATE_TABLE = [
    # call date,      expected due,       why
    (date(2026, 10, 5), date(2026, 10, 7), "Mon + 2 business days = Wed"),
    (date(2026, 10, 9), date(2026, 10, 14), "Fri -> Tue 10/13 (Mon 10/12 holiday), Wed 10/14"),
    (date(2026, 10, 10), date(2026, 10, 15), "Sat rolls to Tue 10/13 (holiday Mon), +2 = Thu 10/15"),
    (date(2026, 10, 12), date(2026, 10, 15), "holiday rolls to Tue 10/13, +2 = Thu 10/15"),
    (date(2026, 11, 10), date(2026, 11, 13), "Tue -> Thu 11/12 (11/11 holiday), Fri 11/13"),
    (date(2026, 11, 25), date(2026, 11, 30), "Wed -> Fri 11/27 (11/26 holiday; 11/27 not listed), Mon 11/30"),
]


@pytest.mark.parametrize("call, expected, why", DUE_DATE_TABLE)
def test_due_dates(call, expected, why):
    assert CAL.due_date(call, 2) == expected, why


def test_year_without_holidays_raises_naming_the_year():
    df = requests_frame([request("2027-01-04 09:00", "2027-01-05", "2027-01-05")])
    with pytest.raises(HolidayYearMissingError, match="2027"):
        classify_requests(df, {"Building": [date(2027, 1, 5)]}, config=CFG)


def test_lenient_mode_flags_instead_of_raising():
    from dataclasses import replace
    df = requests_frame([request("2027-01-04 09:00", "2027-01-05", "2027-01-05")])
    out = classify_requests(df, {"Building": [date(2027, 1, 5)]}, config=replace(CFG, strict_holidays=False))
    assert "holiday_year_missing" in out.loc[0, "dq_flags"]


def classify_one(row: dict, report_dates=ALL_DIVS) -> pd.Series:
    return classify_requests(requests_frame([row]), report_dates, config=CFG).iloc[0]


def test_on_time_closed_before_due():
    r = classify_one(request("2026-10-05 09:00", "2026-10-06", "2026-10-06"))
    assert r["status"] == "on_time" and not r["is_late"] and r["is_judged"]


def test_red_on_time_calendar_adjustment():
    r = classify_one(request("2026-10-09 10:30", "2026-10-12", "2026-10-13", red=True))
    assert r["due_date"] == pd.Timestamp("2026-10-14")
    assert r["status"] == "on_time"
    assert bool(r["font_mismatch"]) and r["mismatch_reason"] == "calendar_adjustment"


@pytest.mark.parametrize("red, reason", [(False, ""), (True, "excused_by_note")])
def test_on_time_contacted(red, reason):
    r = classify_one(request("2026-10-05 09:00", "2026-10-06", "2026-10-09", note="2026-10-07", red=red))
    assert r["status"] == "on_time_contacted"
    assert r["mismatch_reason"] == reason


def test_late_no_note_three_business_days():
    r = classify_one(request("2026-10-05 09:00", "2026-10-06", "2026-10-13"))
    assert r["status"] == "late"
    assert r["business_days_late"] == 3   # 10/8, 10/9, 10/13 (10/12 holiday)
    assert r["mismatch_reason"] == "missed_by_source"


def test_late_note_after_due_two_business_days():
    r = classify_one(request("2026-10-05 09:00", "2026-10-06", "2026-10-14", note="2026-10-09", red=True))
    assert r["status"] == "late"
    assert r["business_days_late"] == 2   # 10/8, 10/9 (ends at the note)
    assert not bool(r["font_mismatch"])


def test_open_overdue_six_business_days():
    r = classify_one(request("2026-10-05 09:00", "2026-10-06", "2026-10-16"))
    assert r["status"] == "open_overdue" and r["is_open"]
    assert r["business_days_late"] == 6   # 10/8, 9, 13, 14, 15, 16


def test_open_not_due_excluded_from_rates():
    reports = {"Building": [date(2026, 10, 14), date(2026, 10, 15)]}
    r = classify_one(request("2026-10-14 09:00", "2026-10-15", "2026-10-15"), reports)
    assert r["status"] == "open_not_due" and not r["is_judged"]
    assert pd.isna(r["font_mismatch"])


def test_placeholder_note_is_not_meaningful():
    for text in ["N/A", " n/a ", "TBD", "-", ".", "None", "na", "", "   ", None]:
        assert not is_meaningful_note(text), text
    assert is_meaningful_note("Left voicemail")


def test_missing_call_date_is_invalid():
    r = classify_one(request(None, "2026-10-06", "2026-10-06"))
    assert r["status"] == "invalid" and not r["is_judged"]
    assert "missing_call_date" in r["dq_flags"]


def test_per_division_as_of_missing_electric_report():
    """Electric's report is missing on 10/16: Electric requests last seen 10/15 stay open."""
    reports = {"Building": OCT_REPORTS, "Electric": OCT_REPORTS[:-1], "Plumbing/Gas": OCT_REPORTS}
    rows = [request("2026-10-05 09:00", "2026-10-06", "2026-10-15", division="Electric"),
            request("2026-10-05 09:00", "2026-10-06", "2026-10-15", division="Building")]
    out = classify_requests(requests_frame(rows), reports, config=CFG)
    elec, bldg = out.iloc[0], out.iloc[1]
    assert elec["as_of"] == pd.Timestamp("2026-10-15") and elec["is_open"] and elec["status"] == "open_overdue"
    assert bldg["as_of"] == pd.Timestamp("2026-10-16") and not bldg["is_open"] and bldg["status"] == "late"


def test_report_gap_flag():
    reports = {"Building": [d for d in OCT_REPORTS if d != date(2026, 10, 8)]}
    r = classify_one(request("2026-10-05 09:00", "2026-10-06", "2026-10-09"), reports)
    assert "report_gap" in r["dq_flags"]


def test_explain_status_marks_exactly_one_rule():
    r = classify_one(request("2026-10-05 09:00", "2026-10-06", "2026-10-09", note="2026-10-07"))
    checks = explain_status(r)
    assert [c[1] for c in checks].count(True) == 1
    assert checks[3][1] is True   # on_time_contacted


# ---------------------------------------------------------------------------
# Aggregation on a hand-built set of 10 requests
# ---------------------------------------------------------------------------
def _ten():
    R = request
    return requests_frame([
        R("2026-10-05 09:00", "2026-10-06", "2026-10-06", inspector="A"),                       # on_time
        R("2026-10-05 09:00", "2026-10-06", "2026-10-07", inspector="A"),                       # on_time
        R("2026-10-09 10:00", "2026-10-12", "2026-10-13", inspector="A", red=True),             # on_time (cal adj)
        R("2026-10-05 09:00", "2026-10-06", "2026-10-09", note="2026-10-07", inspector="A", red=True),  # contacted
        R("2026-10-05 09:00", "2026-10-06", "2026-10-13", inspector="B"),                       # late, not red
        R("2026-10-05 09:00", "2026-10-06", "2026-10-14", note="2026-10-09", inspector="B", red=True),  # late
        R("2026-10-05 09:00", "2026-10-06", "2026-10-16", inspector="B", red=True),             # open_overdue 6
        R("2026-10-07 09:00", "2026-10-08", "2026-10-16", inspector="B", red=True),             # open_overdue 4
        R("2026-10-15 09:00", "2026-10-16", "2026-10-16", inspector="B"),                       # open_not_due
        R(None, "2026-10-06", "2026-10-06", inspector="B"),                                      # invalid
    ])


def test_aggregation_exact_counts():
    out = classify_requests(_ten(), {"Building": OCT_REPORTS}, config=CFG)
    assert list(out["status"]) == ["on_time", "on_time", "on_time", "on_time_contacted", "late", "late",
                                   "open_overdue", "open_overdue", "open_not_due", "invalid"]
    t = aggregate(out, CFG, snapshot_rows_total=40)
    s = t["on_time_summary"].iloc[0]
    assert (s["numerator"], s["denominator"]) == (4, 8)
    assert s["on_time_pct"] == 50.0
    assert (s["on_time"], s["on_time_contacted"], s["late"], s["open_overdue"], s["open_not_due"], s["invalid"]) == (3, 1, 2, 2, 1, 1)
    assert s["duplicates_collapsed"] == 30
    by_insp = t["on_time_by_inspector"].set_index("inspector")
    assert by_insp.loc["A", "on_time_pct"] == 100.0 and by_insp.loc["A", "denominator"] == 4
    assert by_insp.loc["B", "on_time_pct"] == 0.0 and by_insp.loc["B", "denominator"] == 4
    assert by_insp["low_n"].all()          # 4 < 10
    backlog = t["backlog"].set_index(["scope", "group"])
    assert backlog.loc[("overall", "all"), "open_overdue"] == 2
    assert backlog.loc[("overall", "all"), "median_business_days_late"] == 5.0   # median(6, 4)
    mm = t["mismatch_by_reason"].set_index("mismatch_reason")["count"]
    assert mm["calendar_adjustment"] == 1 and mm["excused_by_note"] == 1 and mm["missed_by_source"] == 1


def test_low_n_threshold():
    rows = [request("2026-10-05 09:00", "2026-10-06", "2026-10-06", inspector="Big") for _ in range(10)]
    rows += [request("2026-10-05 09:00", "2026-10-06", "2026-10-06", inspector="Small") for _ in range(9)]
    t = aggregate(classify_requests(requests_frame(rows), {"Building": OCT_REPORTS}, config=CFG), CFG)
    low = t["on_time_by_inspector"].set_index("inspector")["low_n"]
    assert not low["Big"] and low["Small"]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_same_input_same_output():
    a = classify_requests(_ten(), {"Building": OCT_REPORTS}, config=CFG)
    b = classify_requests(_ten(), {"Building": OCT_REPORTS}, config=CFG)
    pd.testing.assert_frame_equal(a.drop(columns=["inspection_request_id", "match_key", "ap_number", "address"]),
                                  b.drop(columns=["inspection_request_id", "match_key", "ap_number", "address"]))


def test_system_clock_changes_nothing(monkeypatch):
    df = _ten()
    before = classify_requests(df, {"Building": OCT_REPORTS}, config=CFG)
    monkeypatch.setattr(pd.Timestamp, "now", classmethod(lambda cls, tz=None: pd.Timestamp("2031-06-01")))
    monkeypatch.setattr(pd.Timestamp, "today", classmethod(lambda cls, tz=None: pd.Timestamp("2031-06-01")))
    after = classify_requests(df, {"Building": OCT_REPORTS}, config=CFG)
    pd.testing.assert_frame_equal(before, after)


def test_late_logic_never_reads_the_clock():
    """Static check: no now()/today()/time() calls in the pure modules."""
    pkg = Path(__file__).resolve().parent.parent / "isd_pipeline" / "late_logic"
    for f in ("rules.py", "aggregate.py", "calendar.py"):
        tree = ast.parse((pkg / f).read_text())
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not calls & {"now", "today", "utcnow", "time"}, f


# ---------------------------------------------------------------------------
# Requests that close during a report gap (rule change approved 2026-10-30)
# Reports only on Fri 10/16 and Fri 10/30 (the live-data case that exposed this).
# ---------------------------------------------------------------------------
GAP_REPORTS = {"Building": [date(2026, 10, 16), date(2026, 10, 30)]}


def test_closed_in_gap_past_due_is_unknown_and_not_judged():
    # Thu 10/15 call, due Mon 10/19; gone by 10/30, could have closed any day 10/19-10/29
    r = classify_one(request("2026-10-15 09:00", "2026-10-16", "2026-10-16"), GAP_REPORTS)
    assert r["status"] == "unknown_close" and not r["is_judged"] and not r["is_late"]
    assert r["last_seen_latest_possible"] == pd.Timestamp("2026-10-29")
    assert "closed_in_report_gap" in r["dq_flags"]
    assert pd.isna(r["font_mismatch"])


def test_closed_in_gap_already_late_stays_late():
    r = classify_one(request("2026-10-07 09:00", "2026-10-16", "2026-10-16"), GAP_REPORTS)
    assert r["status"] == "late" and "closed_in_report_gap" in r["dq_flags"]


def test_closed_in_gap_with_note_before_due_is_contacted():
    r = classify_one(request("2026-10-15 09:00", "2026-10-16", "2026-10-16", note="2026-10-16"), GAP_REPORTS)
    assert r["status"] == "on_time_contacted"


def test_gap_ending_before_due_is_still_on_time():
    # Reports 10/14 and 10/16 (10/15 missing); call Wed 10/14, due Fri 10/16 -> certainly on time
    r = classify_one(request("2026-10-14 09:00", "2026-10-14", "2026-10-14"),
                     {"Building": [date(2026, 10, 14), date(2026, 10, 16)]})
    assert r["status"] == "on_time" and "closed_in_report_gap" in r["dq_flags"]


def test_daily_reports_have_no_gap_flag():
    r = classify_one(request("2026-10-05 09:00", "2026-10-06", "2026-10-06"))
    assert r["status"] == "on_time" and "closed_in_report_gap" not in r["dq_flags"]
    assert r["last_seen_latest_possible"] == pd.Timestamp("2026-10-06")


def test_explain_status_shows_unknown_close():
    r = classify_one(request("2026-10-15 09:00", "2026-10-16", "2026-10-16"), GAP_REPORTS)
    decided = [name for name, ok, _ in explain_status(r) if ok]
    assert decided and decided[0].startswith("unknown_close")
