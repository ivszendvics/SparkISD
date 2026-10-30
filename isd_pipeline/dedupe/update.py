"""Collapse snapshot rows into requests_current (one row per request, persistent ID).

Rules (section 5):
* Exact match on the 5 normalized key fields -> same request, same ID.
* Missing key field, or a conflict (same Ap #/insp_type/work group/call_datetime but a
  different address) -> review_flag + reason, its OWN ID, never auto-merged.
* New IDs are assigned in a deterministic order (report_date, division, call_datetime,
  ap_number, then tie-breakers), so a rebuild from scratch gives the same IDs.
* Reappearing request: refresh the "latest" fields only from a report dated on/after the
  row's last_seen_report_date, and never overwrite a value with a blank. call_datetime,
  the ID and the original address text are never changed. Tracking fields
  (seen_report_dates, first/last seen, n_reports, first_note_date, ever_red) always update.
* Reprocessing the same reports changes nothing (seen dates are a set).
* dedupe_mark for this run: FLAGGED > NEW > UPDATED (a descriptive field changed) > UNCHANGED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import pandas as pd

from isd_pipeline.dedupe.matching import build_match_key, missing_key_fields, normalize_address, partial_key, unmatched_key
from isd_pipeline.dedupe.registry import IdRegistry
from isd_pipeline.notes import is_meaningful_note
from isd_pipeline.schema import (
    LATEST_PROVENANCE_FIELDS, LATEST_VALUE_FIELDS, REQUEST_COLUMNS, REQUEST_DESCRIPTIVE_FIELDS, SNAPSHOT_COLUMNS,
    coerce, join_unique,
)

# A change in any of these makes a reappearing request UPDATED.
CHANGE_TRACKED_FIELDS: tuple[str, ...] = (*LATEST_VALUE_FIELDS, "is_red")


@dataclass
class DedupeStats:
    snapshot_rows_in: int = 0
    requests_before: int = 0
    requests_after: int = 0
    new_requests: int = 0
    updated_requests: int = 0
    unchanged_seen: int = 0
    flagged_requests: int = 0
    not_seen_this_run: int = 0
    rows_matched_existing: int = 0
    within_report_duplicates: int = 0

    @property
    def duplicates_collapsed_this_run(self) -> int:
        return self.snapshot_rows_in - self.new_requests


@dataclass
class DedupeResult:
    requests_current: pd.DataFrame
    id_registry: pd.DataFrame
    stats: DedupeStats
    new_ids: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip()) \
        or v is pd.NaT


def _sort_incoming(rows: pd.DataFrame) -> pd.DataFrame:
    return rows.sort_values(
        ["report_date", "division", "call_datetime", "ap_number", "insp_type", "address", "source_row_number"],
        na_position="last", kind="mergesort").reset_index(drop=True)


def _parse_seen(text: str) -> set[str]:
    return {p for p in str(text or "").split(";") if p}


def update_requests(
    current: pd.DataFrame | None,
    registry: pd.DataFrame | None,
    snapshot_rows: pd.DataFrame,
    batch_id: str,
    *,
    abbreviations: Mapping[str, str] | None = None,
    placeholders: Iterable[str] | None = None,
) -> DedupeResult:
    """Merge new snapshot rows into the current requests. Pure function (no I/O)."""
    abbreviations = abbreviations or {}
    ph = frozenset(placeholders) if placeholders is not None else None
    meaningful = (lambda t: is_meaningful_note(t, ph)) if ph is not None else is_meaningful_note

    cur = coerce(current, REQUEST_COLUMNS)
    reg = IdRegistry.from_frame(registry)
    rows = _sort_incoming(coerce(snapshot_rows, SNAPSHOT_COLUMNS))
    stats = DedupeStats(snapshot_rows_in=len(rows), requests_before=len(cur))
    events: list[str] = []

    records: dict[str, dict[str, Any]] = {r["inspection_request_id"]: r for r in cur.to_dict("records")}
    before: dict[str, tuple] = {rid: tuple(r[f] for f in CHANGE_TRACKED_FIELDS) for rid, r in records.items()}

    # partial key (no address) -> (id, normalized address) of requests with complete keys
    partial_index: dict[str, tuple[str, str]] = {}
    for rid, r in records.items():
        if not missing_key_fields(r):
            partial_index.setdefault(partial_key(r, abbreviations), (rid, normalize_address(r["address"], abbreviations)))

    seen_this_run: set[str] = set()
    created: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()

    for row in rows.to_dict("records"):
        rd: pd.Timestamp = row["report_date"]
        rd_txt = rd.strftime("%Y-%m-%d")
        flag_reasons: list[str] = []
        missing = missing_key_fields(row)
        if missing:
            key = unmatched_key(row)
            flag_reasons.append(f"missing key field(s): {', '.join(missing)}")
        else:
            key = build_match_key(row, abbreviations)

        existing_id = reg.get(key)
        if existing_id is None:
            rid = reg.assign(key, batch_id, rd)
            if not missing:
                pk = partial_key(row, abbreviations)
                addr = normalize_address(row["address"], abbreviations)
                other = partial_index.get(pk)
                if other is not None and other[1] != addr:
                    flag_reasons.append(
                        f"conflict: same Ap #, insp type, work group and call time as {other[0]} "
                        f"but a different address ('{row['address']}')")
                    events.append(f"{rid} flagged: conflicts with {other[0]} (address differs)")
                partial_index.setdefault(pk, (rid, addr))
        else:
            rid = existing_id

        if (rid, rd_txt) in seen_pairs:
            stats.within_report_duplicates += 1
        seen_pairs.add((rid, rd_txt))
        seen_this_run.add(rid)
        note_date = rd if meaningful(row["inspector_notes"]) else pd.NaT

        if rid not in records:
            rec = {f: row[f] for f in REQUEST_DESCRIPTIVE_FIELDS}
            rec.update({
                "inspection_request_id": rid, "match_key": key,
                "first_seen_report_date": rd, "last_seen_report_date": rd, "seen_report_dates": rd_txt,
                "n_reports": 1, "first_note_date": note_date, "ever_red": bool(row["is_red"]),
                "dedupe_mark": "NEW", "review_flag": bool(flag_reasons), "review_reason": "; ".join(flag_reasons),
            })
            records[rid] = rec
            created.add(rid)
            continue

        rec = records[rid]
        stats.rows_matched_existing += 1
        prev_last = rec["last_seen_report_date"]
        if pd.isna(prev_last) or rd >= prev_last:
            for f in LATEST_VALUE_FIELDS:
                if not _blank(row[f]):
                    rec[f] = row[f]
            for f in LATEST_PROVENANCE_FIELDS:
                rec[f] = row[f]
        seen = _parse_seen(rec["seen_report_dates"]) | {rd_txt}
        rec["seen_report_dates"] = ";".join(sorted(seen))
        rec["n_reports"] = len(seen)
        rec["first_seen_report_date"] = min(x for x in (rec["first_seen_report_date"], rd) if not pd.isna(x))
        rec["last_seen_report_date"] = max(x for x in (rec["last_seen_report_date"], rd) if not pd.isna(x))
        if not pd.isna(note_date) and (pd.isna(rec["first_note_date"]) or note_date < rec["first_note_date"]):
            rec["first_note_date"] = note_date
        rec["ever_red"] = bool(rec["ever_red"]) or bool(row["is_red"])
        if flag_reasons:
            rec["review_flag"] = True
            rec["review_reason"] = join_unique([rec["review_reason"], *flag_reasons], sep="; ")

    # --- marks for this run ---
    for rid, rec in records.items():
        if rid not in seen_this_run:
            rec["dedupe_mark"] = "UNCHANGED"
            stats.not_seen_this_run += 1
        elif rec["review_flag"]:
            rec["dedupe_mark"] = "FLAGGED"
            stats.flagged_requests += 1
            if rid in created:
                stats.new_requests += 1
        elif rid in created:
            rec["dedupe_mark"] = "NEW"
            stats.new_requests += 1
        elif tuple(rec[f] for f in CHANGE_TRACKED_FIELDS) != before.get(rid):
            rec["dedupe_mark"] = "UPDATED"
            stats.updated_requests += 1
        else:
            rec["dedupe_mark"] = "UNCHANGED"
            stats.unchanged_seen += 1

    out = coerce(pd.DataFrame.from_records(list(records.values())), REQUEST_COLUMNS)
    out = out.sort_values("inspection_request_id", kind="mergesort").reset_index(drop=True)
    stats.requests_after = len(out)
    return DedupeResult(requests_current=out, id_registry=reg.to_frame(), stats=stats,
                        new_ids=list(reg.created_this_run), events=events)
