"""On-time KPIs. Rates use judged requests only:

    on_time_pct = (on_time + on_time_contacted) / judged * 100

Every rate is published with its numerator, denominator and every status count, plus a
low_n flag when the denominator is under the threshold (default 10).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from isd_pipeline.late_logic.rules import LateLogicConfig
from isd_pipeline.schema import STATUSES

JUDGED = ("on_time", "on_time_contacted", "late", "open_overdue")
DIMENSIONS: dict[str, list[str]] = {
    "division": ["division"],
    "call_month": ["call_month"],
    "call_week": ["call_week"],
    "insp_type": ["insp_type"],
    "inspector": ["inspector"],
    "ward": ["ward"],
    "inspector_month": ["inspector", "call_month"],
}


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["call_month"] = d["call_date"].dt.strftime("%Y-%m").fillna("(no call date)")
    week_start = d["call_date"] - pd.to_timedelta(d["call_date"].dt.weekday, unit="D")
    d["call_week"] = week_start.dt.strftime("%Y-%m-%d").fillna("(no call date)")
    # A request with no inspector on any report: Power BI lists these in an unnamed group
    # (not yet assigned). Fill-down never invents a name, so the label says exactly that.
    d["inspector"] = d["inspector"].replace("", "(no inspector assigned)")
    for col in ("ward", "insp_type", "division"):
        d[col] = d[col].replace("", "(blank)")
    return d


def rate_table(df: pd.DataFrame, group_cols: list[str], low_n_threshold: int = 10) -> pd.DataFrame:
    """Status counts + on-time rate per group (or one overall row when group_cols is empty)."""
    if group_cols:
        counts = pd.crosstab([df[c] for c in group_cols], df["status"])
    else:
        counts = df["status"].value_counts().to_frame().T
        counts.index = [0]
    for s in STATUSES:
        if s not in counts.columns:
            counts[s] = 0
    counts = counts[list(STATUSES)].astype("int64")
    out = counts.reset_index() if group_cols else counts.reset_index(drop=True)
    out.columns.name = None
    out["total_requests"] = out[list(STATUSES)].sum(axis=1)
    out["numerator"] = out["on_time"] + out["on_time_contacted"]
    out["denominator"] = out[list(JUDGED)].sum(axis=1)
    out["judged"] = out["denominator"]
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.where(out["denominator"] > 0, out["numerator"] / out["denominator"].where(out["denominator"] > 0) * 100, np.nan)
    out["on_time_pct"] = np.round(pct.astype(float), 1)
    out["low_n"] = out["denominator"] < low_n_threshold
    lead = group_cols + ["on_time_pct", "numerator", "denominator", "low_n", "total_requests", "judged"]
    return out[lead + list(STATUSES)]


def _backlog(df: pd.DataFrame) -> pd.DataFrame:
    oo = df[df["status"] == "open_overdue"]
    rows = [{"scope": "overall", "group": "all", "open_overdue": len(oo),
             "median_business_days_late": float(oo["business_days_late"].median()) if len(oo) else 0.0,
             "max_business_days_late": int(oo["business_days_late"].max()) if len(oo) else 0}]
    for scope in ("division", "inspector"):
        for key, g in oo.groupby(scope, sort=True):
            rows.append({"scope": scope, "group": key, "open_overdue": len(g),
                         "median_business_days_late": float(g["business_days_late"].median()),
                         "max_business_days_late": int(g["business_days_late"].max())})
    return pd.DataFrame(rows)


def aggregate(classified_df: pd.DataFrame, config: LateLogicConfig | None = None, *,
              snapshot_rows_total: int | None = None) -> dict[str, pd.DataFrame]:
    """Pure function: classified requests -> dict of KPI tables."""
    low_n = config.low_n_threshold if config else 10
    df = _prep(classified_df)
    tables: dict[str, pd.DataFrame] = {}

    overall = rate_table(df, [], low_n)
    judged = df[df["is_judged"]]
    oo = df[df["status"] == "open_overdue"]
    overall.insert(0, "scope", "overall")
    overall["open_overdue_backlog"] = len(oo)
    overall["median_business_days_late_backlog"] = float(oo["business_days_late"].median()) if len(oo) else 0.0
    overall["font_mismatches"] = int(judged["font_mismatch"].fillna(False).astype(bool).sum())
    overall["ever_red_requests"] = int(df["ever_red"].sum())
    overall["review_queue"] = int(df["review_flag"].sum())
    overall["dq_flagged_requests"] = int((df["dq_flags"] != "").sum())
    overall["unique_requests"] = len(df)
    overall["snapshot_rows_ingested"] = snapshot_rows_total if snapshot_rows_total is not None else np.nan
    overall["duplicates_collapsed"] = (snapshot_rows_total - len(df)) if snapshot_rows_total is not None else np.nan
    as_of = df["as_of"].dropna()
    overall["latest_as_of"] = as_of.max().strftime("%Y-%m-%d") if len(as_of) else ""
    tables["on_time_summary"] = overall

    for name, cols in DIMENSIONS.items():
        tables[f"on_time_by_{name}"] = rate_table(df, cols, low_n)

    tables["backlog"] = _backlog(df)

    mm = judged[judged["font_mismatch"].fillna(False).astype(bool)]
    reasons = ["excused_by_note", "calendar_adjustment", "missed_by_source", "other_red_not_late"]
    tables["mismatch_by_reason"] = pd.DataFrame(
        [{"mismatch_reason": r, "count": int((mm["mismatch_reason"] == r).sum())} for r in reasons])

    rv = []
    for red in (True, False):
        for late in (True, False):
            rv.append({"red_font_says": "late" if red else "not late", "our_logic_says": "late" if late else "on time",
                       "count": int(((judged["ever_red"] == red) & (judged["is_late"] == late)).sum())})
    tables["red_vs_logic"] = pd.DataFrame(rv)

    flags = df["dq_flags"].str.split(";").explode()
    flags = flags[flags.notna() & (flags != "")]
    tables["dq_summary"] = flags.value_counts().rename_axis("dq_flag").reset_index(name="count")
    return tables
