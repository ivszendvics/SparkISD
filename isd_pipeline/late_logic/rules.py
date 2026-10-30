"""Module 3: decide on-time status for each request. Pure functions - no file or network I/O,
and never the system clock (the reference date comes from the report dates).

Status, first match wins (section 6.4):
    invalid            call date missing (excluded from all rates)
    open_not_due       is_open AND as_of <= due_date
    on_time            NOT is_open AND last_seen_report_date <= due_date
    on_time_contacted  first_note_date <= due_date
    unknown_close      closed during a report gap that runs past the due date (see below)
    open_overdue       is_open
    late               everything else
where is_open = last_seen_report_date >= as_of, and as_of = latest report date OF THE
REQUEST'S DIVISION (so a missing Electric report doesn't make Electric requests look resolved).

Report gaps (extension, see README): when the division has no report on some business days
after a request's last sighting, the request really closed on an unknown day inside that gap.
`last_seen_latest_possible` is the last missing business day before the next report (or
last_seen when there is no gap). The `on_time` rule only applies when even that latest
possible date is <= due; otherwise the outcome can't be known and the status is
`unknown_close`, which is not judged (excluded from rates, like open_not_due and invalid).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from isd_pipeline.late_logic.calendar import BusinessCalendar, load_sla
from isd_pipeline.notes import load_note_placeholders
from isd_pipeline.schema import CLASSIFIED_COLUMNS, REQUEST_COLUMNS, coerce


@dataclass(frozen=True)
class LateLogicConfig:
    calendar: BusinessCalendar
    sla_by_type: Mapping[str, int] = field(default_factory=dict)   # lower-case insp_type -> days
    default_sla: int = 2
    placeholders: frozenset[str] = frozenset({"n/a", "na", "-", ".", "none", "tbd"})
    long_open_threshold: int = 30
    strict_holidays: bool = True
    low_n_threshold: int = 10

    def sla_for(self, insp_type: str) -> int:
        return int(self.sla_by_type.get(str(insp_type or "").strip().lower(), self.default_sla))


def default_config(settings=None, *, strict_holidays: bool | None = None) -> LateLogicConfig:
    """Build the config from isd_pipeline/config (this is the only place that reads files)."""
    from isd_pipeline.settings import load_settings
    settings = settings or load_settings()
    sla, default = load_sla(settings.config_dir / "sla.csv")
    return LateLogicConfig(
        calendar=BusinessCalendar.from_csv(settings.config_dir / "holidays.csv"),
        sla_by_type=sla, default_sla=default,
        placeholders=load_note_placeholders(settings.config_dir),
        long_open_threshold=settings.long_open_business_days,
        strict_holidays=settings.strict_holidays if strict_holidays is None else strict_holidays,
        low_n_threshold=settings.low_n_threshold,
    )


def _d(x) -> pd.Timestamp:
    return pd.Timestamp(x).normalize()


def division_as_of(report_dates_by_division: Mapping[str, Sequence[date]], as_of: date | None = None) -> dict[str, pd.Timestamp]:
    """Latest report date per division (capped at `as_of` if given)."""
    out = {}
    for div, dates in report_dates_by_division.items():
        ds = [_d(x) for x in dates if as_of is None or _d(x) <= _d(as_of)]
        if ds:
            out[div] = max(ds)
    return out


def classify_requests(
    requests_df: pd.DataFrame,
    report_dates_by_division: Mapping[str, Sequence[date]],
    as_of: date | None = None,
    config: LateLogicConfig | None = None,
) -> pd.DataFrame:
    """Return requests_df (schema 3b) plus the 3c columns.

    report_dates_by_division: the report dates that have a valid stored report, per division.
    as_of: optional cap - classify as of this date (requests first seen after it are dropped).
    """
    cfg = config or default_config()
    cal = cfg.calendar
    df = coerce(requests_df, REQUEST_COLUMNS)
    asof_by_div = division_as_of(report_dates_by_division, as_of)
    if as_of is not None:
        df = df[df["first_seen_report_date"] <= _d(as_of)].reset_index(drop=True)
    n = len(df)
    if n == 0:
        return coerce(pd.DataFrame(), CLASSIFIED_COLUMNS)

    call_date = df["call_datetime"].dt.normalize()
    sla = np.array([cfg.sla_for(t) for t in df["insp_type"]], dtype="int64")
    due = pd.to_datetime(pd.Series(cal.due_dates(call_date, sla))).astype("datetime64[ns]")
    as_of_s = pd.to_datetime(df["division"].map(asof_by_div)).astype("datetime64[ns]")
    last_seen = df["last_seen_report_date"]
    first_seen = df["first_seen_report_date"]
    first_note = df["first_note_date"]

    # --- holiday coverage (strict: raise; lenient: flag rows) ---
    needed = pd.concat([call_date, due, as_of_s, last_seen, first_seen, first_note])
    missing_years = cal.check_years(needed, strict=cfg.strict_holidays)
    year_flag = np.zeros(n, dtype=bool)
    if missing_years:
        for s in (call_date, due, as_of_s, last_seen, first_seen, first_note):
            year_flag |= s.dt.year.isin(missing_years).to_numpy()

    # --- status ---
    has_call = call_date.notna().to_numpy()
    is_open = (last_seen >= as_of_s).fillna(False).to_numpy()
    latest_last = _latest_possible_last_seen(df, report_dates_by_division, cal, as_of_s, is_open)
    closed_in_gap = ~is_open & (latest_last > last_seen).fillna(False).to_numpy()
    c1 = is_open & (as_of_s <= due).fillna(False).to_numpy()
    c2 = ~is_open & (latest_last <= due).fillna(False).to_numpy()
    c3 = (first_note.notna() & (first_note <= due)).fillna(False).to_numpy()
    c_unknown = closed_in_gap & (last_seen <= due).fillna(False).to_numpy()
    status = np.select(
        [~has_call, c1, c2, c3, c_unknown, is_open],
        ["invalid", "open_not_due", "on_time", "on_time_contacted", "unknown_close", "open_overdue"],
        default="late",
    )
    is_late = np.isin(status, ["late", "open_overdue"])
    is_judged = ~np.isin(status, ["open_not_due", "invalid", "unknown_close"])

    # --- lateness measures ---
    note_after_due = (first_note.notna() & (first_note > due)).fillna(False).to_numpy()
    end_late = np.where(note_after_due, first_note.to_numpy(), last_seen.to_numpy())
    bdl = np.zeros(n, dtype="int64")
    m_late = status == "late"
    m_oo = status == "open_overdue"
    if m_late.any():
        bdl[m_late] = cal.busdays_between(due.to_numpy()[m_late], end_late[m_late])
    if m_oo.any():
        bdl[m_oo] = cal.busdays_between(due.to_numpy()[m_oo], as_of_s.to_numpy()[m_oo])
    open_end = np.where((last_seen > as_of_s).fillna(False).to_numpy(), as_of_s.to_numpy(), last_seen.to_numpy())
    bdo = cal.busdays_between(call_date.to_numpy(), open_end)

    # --- red-font comparison ---
    ever_red = df["ever_red"].to_numpy(dtype=bool)
    mismatch = pd.array([bool(r != l) if j else None for r, l, j in zip(ever_red, is_late, is_judged)],
                        dtype="boolean")
    cal_adj = ((call_date + pd.Timedelta(days=2)) < due).fillna(False).to_numpy()
    reason = np.select(
        [
            ever_red & (status == "on_time_contacted"),
            ever_red & (status == "on_time") & cal_adj,
            ~ever_red & is_late,
            ever_red & ~is_late,
        ],
        ["excused_by_note", "calendar_adjustment", "missed_by_source", "other_red_not_late"],
        default="",
    )
    reason = np.where(mismatch.fillna(False).to_numpy(dtype=bool), reason, "")

    # --- data-quality flags ---
    gaps = _report_gap_flags(df, report_dates_by_division, cal, as_of)
    flags = []
    for i in range(n):
        f = []
        if not has_call[i]:
            f.append("missing_call_date")
        elif pd.notna(first_seen.iat[i]) and call_date.iat[i] > first_seen.iat[i]:
            f.append("call_after_first_seen")
        if pd.notna(first_note.iat[i]) and (first_note.iat[i] < first_seen.iat[i] or first_note.iat[i] > last_seen.iat[i]):
            f.append("first_note_outside_seen_range")
        if gaps[i]:
            f.append("report_gap")
        if closed_in_gap[i]:
            f.append("closed_in_report_gap")
        if bdo[i] > cfg.long_open_threshold:
            f.append("long_open")
        if bool(df["review_flag"].iat[i]):
            f.append("review_flag_upstream")
        if year_flag[i]:
            f.append("holiday_year_missing")
        flags.append(";".join(f))

    out = df.copy()
    out["call_date"] = call_date
    out["sla_business_days"] = sla
    out["due_date"] = due
    out["as_of"] = as_of_s
    out["is_open"] = is_open
    out["last_seen_latest_possible"] = latest_last
    out["status"] = status
    out["is_late"] = is_late
    out["is_judged"] = is_judged
    out["business_days_late"] = bdl
    out["business_days_open"] = bdo
    out["font_mismatch"] = mismatch
    out["mismatch_reason"] = reason
    out["dq_flags"] = flags
    return coerce(out, CLASSIFIED_COLUMNS)


def _latest_possible_last_seen(df: pd.DataFrame, report_dates_by_division: Mapping[str, Sequence[date]],
                               cal: BusinessCalendar, as_of_s: pd.Series, is_open: np.ndarray) -> pd.Series:
    """For closed requests: the last business day before the division's next report that had
    no report (a day the request may still have been open), or last_seen when there is no gap.
    Open requests get last_seen unchanged."""
    have = {div: sorted({_d(x) for x in dates}) for div, dates in report_dates_by_division.items()}
    last_seen = df["last_seen_report_date"]
    out = last_seen.copy()
    cache: dict[tuple, pd.Timestamp] = {}
    for i, (div, ls, cap) in enumerate(zip(df["division"], last_seen, as_of_s)):
        if is_open[i] or pd.isna(ls) or pd.isna(cap):
            continue
        key = (div, ls, cap)
        if key not in cache:
            dates = have.get(div, [])
            nxt = next((d for d in dates if ls < d <= cap), None)
            missing = []
            if nxt is not None:
                present = set(d.date() for d in dates)
                missing = [d for d in cal.business_days_in(ls, nxt) if d not in present]
            cache[key] = pd.Timestamp(max(missing)) if missing else ls
        out.iat[i] = cache[key]
    return out


def _report_gap_flags(df: pd.DataFrame, report_dates_by_division: Mapping[str, Sequence[date]],
                      cal: BusinessCalendar, as_of: date | None) -> np.ndarray:
    """True when a business day strictly between first and last seen has no report for the division."""
    have = {div: {_d(x).date() for x in dates} for div, dates in report_dates_by_division.items()}
    out = np.zeros(len(df), dtype=bool)
    cache: dict[tuple, bool] = {}
    for i, (div, fs, ls) in enumerate(zip(df["division"], df["first_seen_report_date"], df["last_seen_report_date"])):
        if pd.isna(fs) or pd.isna(ls):
            continue
        key = (div, fs, ls)
        if key not in cache:
            days = cal.business_days_in(fs, ls)
            cache[key] = any(d not in have.get(div, set()) for d in days)
        out[i] = cache[key]
    return out


def explain_status(row: Mapping) -> list[tuple[str, bool, str]]:
    """The rule checks for one classified request, in order (for the demo spotlight)."""
    fmt = lambda x: "-" if x is None or pd.isna(x) else pd.Timestamp(x).strftime("%a %m/%d")  # noqa: E731
    is_open = bool(row["is_open"])
    due, as_of, last, note = row["due_date"], row["as_of"], row["last_seen_report_date"], row["first_note_date"]
    latest = row.get("last_seen_latest_possible", last)
    latest = last if latest is None or pd.isna(latest) else latest
    gap = (not is_open) and pd.notna(last) and latest > last
    checks = [
        ("invalid: call date missing", pd.isna(row["call_date"]), f"call date = {fmt(row['call_date'])}"),
        ("open_not_due: open AND as_of <= due", is_open and pd.notna(due) and as_of <= due,
         f"open={is_open}, as_of {fmt(as_of)} vs due {fmt(due)}"),
        ("on_time: closed AND last seen <= due", (not is_open) and pd.notna(due) and latest <= due,
         f"open={is_open}, last seen {fmt(last)}" + (f" (could be {fmt(latest)}: report gap)" if gap else "")
         + f" vs due {fmt(due)}"),
        ("on_time_contacted: first note <= due", pd.notna(note) and pd.notna(due) and note <= due,
         f"first meaningful note {fmt(note)} vs due {fmt(due)}"),
        ("unknown_close: closed during a report gap past the due date", gap and pd.notna(due) and last <= due,
         f"no report between {fmt(last)} and the next report; due {fmt(due)}"),
        ("open_overdue: still open", is_open, f"last seen {fmt(last)} >= as_of {fmt(as_of)}"),
        ("late: everything else", True, ""),
    ]
    out, decided = [], False
    for name, ok, detail in checks:
        out.append((name, bool(ok) and not decided, detail))
        decided = decided or bool(ok)
    return out
