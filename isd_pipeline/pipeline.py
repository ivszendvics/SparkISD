"""run_daily_pipeline: search email -> download -> validate/standardize -> store raw files and
snapshots -> Module 2 -> Module 3 -> dashboard exports -> manifest -> logs -> summary.

Safety rules
* Each division is handled independently: a missing/invalid/erroring division never stops
  the others (result: PARTIAL_SUCCESS naming the division).
* Raw files and snapshots are stored idempotently (looked up by appProperties/hash first).
* Processed datasets are written as NEW versioned files; the manifest is updated last.
  Any exception before that leaves the manifest - and the datasets it points to - untouched.
* The processed-attachment registry is written after the manifest, so a crash in between
  just means the next run re-applies those rows (Module 2 is idempotent).
* A run with no new attachments is a no-op: 0 new files, 0 new IDs (logs updated in place).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from isd_pipeline.dashboard.exports import build_exports, write_exports
from isd_pipeline.dedupe.update import update_requests
from isd_pipeline.errors import LiveSetupError
from isd_pipeline.factory import make_email_source, make_storage
from isd_pipeline.ingestion.email_base import EmailSource, find_daily_reports
from isd_pipeline.ingestion.excel_parser import process_excel_report
from isd_pipeline.ingestion.ingestion_log import append_log, processed_keys, read_log
from isd_pipeline.ingestion.manifest import (
    DatasetNotFound, file_entry, load_latest_dataset, read_manifest, write_manifest,
)
from isd_pipeline.ingestion.raw_reports import store_raw_report
from isd_pipeline.ingestion.snapshots import report_dates_by_division, save_snapshot, total_snapshot_rows
from isd_pipeline.ingestion.storage_base import JSON_MIME, StorageBackend, save_dataframe
from isd_pipeline.late_logic.aggregate import aggregate
from isd_pipeline.late_logic.rules import classify_requests, default_config
from isd_pipeline.notes import load_note_placeholders
from isd_pipeline.retry import call_with_retry
from isd_pipeline.schema import CLASSIFIED_COLUMNS, ID_REGISTRY_COLUMNS, REQUEST_COLUMNS
from isd_pipeline.settings import DIVISIONS, Settings, load_settings

log = logging.getLogger(__name__)
OK_STATUSES = ("PROCESSED", "ALREADY_PROCESSED")


@dataclass
class StepEvent:
    key: str        # search | found | download | parse | drive | dedupe | late | exports | manifest | done
    label: str
    detail: str = ""
    ok: bool = True


@dataclass
class DivisionResult:
    division: str
    status: str = "PENDING"   # PROCESSED | ALREADY_PROCESSED | MISSING | INVALID | ERROR | NO_ATTACHMENT | NOT_EXCEL
    message: str = ""
    email_id: str = ""
    attachment_id: str = ""
    filename: str = ""
    subject: str = ""
    received_at: str = ""
    sha256: str = ""
    rows: int = 0
    invalid_rows: int = 0
    header_row: int | None = None
    inspector_cells_filled: int = 0
    red_rows: int = 0
    raw_file_id: str = ""
    raw_created: bool = False
    snapshot_file_id: str = ""
    snapshot_created: bool = False
    validation: dict[str, Any] | None = None


@dataclass
class RunResult:
    batch_id: str
    report_date: date
    status: str = "FAILURE"   # SUCCESS | PARTIAL_SUCCESS | FAILURE
    mode: str = "mock"
    trigger: str = "manual"
    started_at: str = ""
    finished_at: str = ""
    divisions: dict[str, DivisionResult] = field(default_factory=dict)
    new_files: int = 0
    snapshot_rows_ingested: int = 0
    requests_before: int = 0
    requests_after: int = 0
    new_requests: int = 0
    updated_requests: int = 0
    unchanged_requests: int = 0
    flagged_requests: int = 0
    duplicates_collapsed: int = 0
    new_ids: list[str] = field(default_factory=list)
    nothing_new: bool = False
    manifest_updated: bool = False
    kpis: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    steps: list[StepEvent] = field(default_factory=list)

    @property
    def problem_divisions(self) -> list[str]:
        return [d for d, r in self.divisions.items() if r.status not in OK_STATUSES]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["report_date"] = self.report_date.isoformat()
        return d

    def summary_text(self) -> str:
        lines = [f"Run {self.batch_id} for {self.report_date} [{self.mode.upper()}]: {self.status}"]
        for div, r in self.divisions.items():
            extra = f"{r.rows} rows, header row {r.header_row}, {r.inspector_cells_filled} inspector cells filled, " \
                    f"{r.invalid_rows} invalid rows" if r.status == "PROCESSED" else r.message
            lines.append(f"  {div:<13} {r.status:<18} {extra}")
        if self.nothing_new:
            lines.append(f"  Nothing new to process: {self.new_files} new files, {len(self.new_ids)} new IDs")
        else:
            lines.append(f"  Snapshot rows ingested {self.snapshot_rows_ingested}; requests {self.requests_before} -> "
                         f"{self.requests_after} (new {self.new_requests}, updated {self.updated_requests}, "
                         f"flagged {self.flagged_requests}); new files {self.new_files}")
        if self.kpis:
            k = self.kpis
            lines.append(f"  On-time {k.get('on_time_pct')}% ({k.get('numerator')}/{k.get('denominator')} judged); "
                         f"open-overdue backlog {k.get('open_overdue_backlog')}; duplicates collapsed "
                         f"{k.get('duplicates_collapsed')}")
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        return "\n".join(lines)


def make_batch_id(report_date: date) -> str:
    now = datetime.now(timezone.utc)
    return f"B{report_date:%Y%m%d}-{now:%H%M%S}-{uuid.uuid4().hex[:4]}"


def validate_live_settings(settings: Settings) -> None:
    if settings.is_mock:
        return
    if not settings.email_rules.sender_address:
        raise LiveSetupError(
            "Live mode needs the real sender address. Open one of the Power BI emails in Gmail -> "
            "⋮ -> 'Show original', copy the From: address into isd_pipeline/config/email_rules.yaml "
            "(sender.address), then run again. See docs/LIVE_SETUP.md step 1.")


def _json_records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso"))


def run_daily_pipeline(
    report_date: date | None = None,
    *,
    settings: Settings | None = None,
    email_source: EmailSource | None = None,
    storage: StorageBackend | None = None,
    trigger: str = "manual",
    on_step: Callable[[StepEvent], None] | None = None,
    strict_holidays: bool | None = None,
    retry_sleep: Callable[[float], None] | None = None,
) -> RunResult:
    settings = settings or load_settings()
    if report_date is None:   # the system clock only picks WHICH day to fetch, never business logic
        report_date = datetime.now(ZoneInfo(settings.timezone)).date()
    validate_live_settings(settings)
    storage = storage or make_storage(settings)
    source = email_source or make_email_source(settings)
    batch_id = make_batch_id(report_date)
    started = datetime.now(timezone.utc)
    res = RunResult(batch_id=batch_id, report_date=report_date, mode=settings.mode, trigger=trigger,
                    started_at=started.isoformat(timespec="seconds"))
    files_before = storage.files_created

    def step(key: str, label: str, detail: str = "", ok: bool = True) -> None:
        ev = StepEvent(key, label, detail, ok)
        res.steps.append(ev)
        log.info("[%s] %s %s", key, label, detail)
        if on_step:
            on_step(ev)

    # ------------------------------------------------------------------ 1. search
    storage.ensure_folders()
    step("search", "Searching the inbox", f"{source.describe()} - report date {report_date}")
    retry_kw: dict[str, Any] = {"attempts": settings.retry_attempts, "base_delay": settings.retry_base_delay}
    if retry_sleep is not None:
        retry_kw["sleep"] = retry_sleep
    fetch = find_daily_reports(source, settings.email_rules, report_date, tz=settings.timezone, download=False,
                               retry_attempts=retry_kw["attempts"], retry_base_delay=retry_kw["base_delay"],
                               sleep=retry_sleep)
    res.warnings.extend(fetch.warnings)
    found = [d for d, f in fetch.divisions.items() if f.status == "FOUND"]
    step("found", f"{len(found)} of {len(DIVISIONS)} emails found",
         ", ".join(f"{d}: {f.status}" for d, f in fetch.divisions.items()), ok=len(found) == len(DIVISIONS))

    done_pairs, done_hashes = processed_keys(storage)
    log_rows: list[dict] = []
    invalid_log: list[dict] = []
    processed_rows: list[dict] = []
    new_frames: list[pd.DataFrame] = []
    sources: list[dict] = []
    schema = settings.report_schema

    def log_row(dr: DivisionResult, status: str | None = None, message: str | None = None, **kw: Any) -> None:
        log_rows.append({
            "ingestion_batch_id": batch_id, "run_started_at": res.started_at, "trigger": trigger,
            "mode": settings.mode, "report_date": report_date.isoformat(), "division": dr.division,
            "status": status or dr.status, "email_id": kw.get("email_id", dr.email_id),
            "attachment_id": dr.attachment_id, "filename": dr.filename, "received_at": dr.received_at,
            "sha256": dr.sha256, "header_row": dr.header_row or "", "rows_extracted": dr.rows,
            "invalid_rows": dr.invalid_rows, "inspector_cells_filled": dr.inspector_cells_filled,
            "red_rows": dr.red_rows, "red_cells_checked": (dr.validation or {}).get("red_cells_checked", ""),
            "raw_file_id": dr.raw_file_id, "snapshot_file_id": dr.snapshot_file_id,
            "message": message if message is not None else dr.message,
        })

    # ------------------------------------------------------------------ 2-4. per division
    for division in DIVISIONS:
        f = fetch.divisions[division]
        dr = DivisionResult(division=division, status=f.status, message=f.message)
        res.divisions[division] = dr
        for ign in f.ignored:
            log_row(dr, status=ign["reason"], message=ign["detail"], email_id=ign["email_id"])
        if f.status != "FOUND" or f.attachment is None:
            res.warnings.append(f"{division}: {f.message}")
            log_row(dr)
            continue
        att = f.attachment
        dr.email_id, dr.attachment_id, dr.filename = att.email_id, att.attachment_id, att.filename
        dr.subject, dr.received_at = att.subject, att.received_at.isoformat()

        if (att.email_id, att.attachment_id) in done_pairs:
            dr.status, dr.message = "ALREADY_PROCESSED", "This attachment was processed in an earlier run"
            log_row(dr)
            continue
        try:
            from isd_pipeline.ingestion.email_base import AttachmentMeta
            meta = AttachmentMeta(att.attachment_id, att.filename, att.mime_type, att.size, att.download_token)
            att.content = call_with_retry(lambda: source.download_attachment(att.email_id, meta),
                                          what=f"download {division}", **retry_kw)
        except LiveSetupError:
            raise
        except Exception as exc:  # noqa: BLE001
            dr.status, dr.message = "ERROR", f"Attachment download failed after retries: {exc}"
            res.warnings.append(f"{division}: {dr.message}")
            log_row(dr)
            continue
        dr.sha256 = att.sha256
        if dr.sha256 in done_hashes:
            dr.status, dr.message = "ALREADY_PROCESSED", "Identical file content was already processed"
            log_row(dr)
            continue
        step("download", f"{division}: attachment downloaded", f"{att.filename} ({len(att.content):,} bytes)")

        raw, created = store_raw_report(storage, att)
        dr.raw_file_id, dr.raw_created = raw.file_id, created

        df, vr = process_excel_report(
            att.content, division, report_date,
            source={"email_id": att.email_id, "attachment_id": att.attachment_id, "filename": raw.name},
            batch_id=batch_id, schema=schema, scan_rows=settings.header_scan_rows,
            min_header_matches=settings.min_header_matches, ingested_at=started)
        dr.validation = vr.summary()
        dr.header_row, dr.rows, dr.invalid_rows = vr.header_row, vr.rows_extracted, vr.n_invalid_rows
        dr.inspector_cells_filled, dr.red_rows = vr.filled_inspector_cells, vr.red_count
        res.warnings.extend(f"{division}: {w}" for w in vr.warnings if "inspector blank" not in w)
        if not vr.is_valid:
            dr.status, dr.message = "INVALID", "; ".join(vr.errors)
            step("parse", f"{division}: file rejected", dr.message, ok=False)
            res.warnings.append(f"{division}: INVALID file - {dr.message}. Stored datasets were not changed.")
            log_row(dr)
            processed_rows.append({"email_id": att.email_id, "attachment_id": att.attachment_id, "sha256": dr.sha256,
                                   "division": division, "report_date": report_date.isoformat(), "status": "INVALID",
                                   "ingestion_batch_id": batch_id, "processed_at": started.isoformat(),
                                   "raw_file_id": raw.file_id, "snapshot_file_id": ""})
            continue
        step("parse", f"{division}: header row {vr.header_row} detected, {vr.rows_extracted} rows extracted",
             f"{vr.filled_inspector_cells} inspector cells filled down, {vr.n_invalid_rows} invalid rows logged")

        snap, snap_created = save_snapshot(storage, df, report_date, division, email_id=att.email_id,
                                           attachment_id=att.attachment_id, sha256=dr.sha256, batch_id=batch_id)
        dr.snapshot_file_id, dr.snapshot_created = snap.file_id, snap_created
        dr.status = "PROCESSED"
        step("drive", f"{division}: saved to Drive", f"raw {raw.file_id}{'' if created else ' (existing)'}, "
                                                     f"snapshot {snap.file_id}{'' if snap_created else ' (existing)'}")
        for inv in vr.invalid_rows:
            invalid_log.append({"ingestion_batch_id": batch_id, "report_date": report_date.isoformat(),
                                "division": division, "source_email_id": att.email_id, "source_filename": raw.name,
                                "raw_file_id": raw.file_id, "source_row_number": inv["source_row_number"],
                                "reason": inv["reason"], "values": json.dumps(inv["values"])})
        processed_rows.append({"email_id": att.email_id, "attachment_id": att.attachment_id, "sha256": dr.sha256,
                               "division": division, "report_date": report_date.isoformat(), "status": "PROCESSED",
                               "ingestion_batch_id": batch_id, "processed_at": started.isoformat(),
                               "raw_file_id": raw.file_id, "snapshot_file_id": snap.file_id})
        log_row(dr)
        new_frames.append(df)
        sources.append({"division": division, "email_id": att.email_id, "subject": att.subject,
                        "received_at": att.received_at.isoformat(), "filename": att.filename,
                        "raw_file_name": raw.name, "raw_file_id": raw.file_id, "snapshot_file_id": snap.file_id,
                        "rows": len(df)})

    ok = [d for d, r in res.divisions.items() if r.status in OK_STATUSES]
    res.status = "SUCCESS" if len(ok) == len(DIVISIONS) else ("PARTIAL_SUCCESS" if ok else "FAILURE")
    res.snapshot_rows_ingested = int(sum(len(f) for f in new_frames))

    # ------------------------------------------------------------------ 5-8. modules 2-4 + manifest
    try:
        current = registry = None
        manifest = read_manifest(storage)
        if manifest:
            current = load_latest_dataset(storage, "requests_current")
            registry = load_latest_dataset(storage, "id_registry")
        res.requests_before = res.requests_after = 0 if current is None else len(current)

        if not new_frames:
            res.nothing_new = True
            step("done", "Nothing new to process", "0 new rows; datasets and manifest unchanged",
                 ok=res.status != "FAILURE")
        else:
            incoming = pd.concat(new_frames, ignore_index=True)
            dd = update_requests(current, registry, incoming, batch_id, abbreviations=settings.address_abbreviations,
                                 placeholders=load_note_placeholders(settings.config_dir))
            s = dd.stats
            res.requests_before, res.requests_after = s.requests_before, s.requests_after
            res.new_requests, res.updated_requests = s.new_requests, s.updated_requests
            res.unchanged_requests, res.flagged_requests = s.unchanged_seen, s.flagged_requests
            res.new_ids = dd.new_ids
            res.warnings.extend(dd.events)
            step("dedupe", f"Duplicate detection: {s.snapshot_rows_in} rows -> {s.new_requests} new, "
                           f"{s.updated_requests} updated, {s.flagged_requests} flagged",
                 f"requests_current {s.requests_before} -> {s.requests_after}")

            cfg = default_config(settings, strict_holidays=strict_holidays)
            classified = classify_requests(dd.requests_current, report_dates_by_division(storage), config=cfg)
            snap_total = total_snapshot_rows(storage)
            tables = aggregate(classified, cfg, snapshot_rows_total=snap_total)
            summary = tables["on_time_summary"].iloc[0]
            res.duplicates_collapsed = int(summary["duplicates_collapsed"])
            res.kpis = {k: (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v))
                        for k, v in summary.to_dict().items()}
            step("late", f"Late logic: on-time {summary['on_time_pct']}% "
                         f"({summary['numerator']}/{summary['denominator']} judged)",
                 f"open-overdue backlog {summary['open_overdue_backlog']}, red-font mismatches {summary['font_mismatches']}")

            meta = {"kind": "dataset", "batch_id": batch_id}
            f_req = save_dataframe(storage, "processed_data", f"requests_current__{batch_id}.csv",
                                   dd.requests_current, REQUEST_COLUMNS, {**meta, "dataset": "requests_current"})
            f_reg = save_dataframe(storage, "processed_data", f"id_registry__{batch_id}.csv",
                                   dd.id_registry, ID_REGISTRY_COLUMNS, {**meta, "dataset": "id_registry"})
            f_cls = save_dataframe(storage, "processed_data", f"requests_classified__{batch_id}.csv",
                                   classified, CLASSIFIED_COLUMNS, {**meta, "dataset": "requests_classified"})
            bundle = json.dumps({k: _json_records(v) for k, v in tables.items()}, indent=1).encode()
            f_kpi = storage.create_file("processed_data", f"kpis__{batch_id}.json", bundle, JSON_MIME,
                                        {**meta, "dataset": "kpis"})

            # run history row for this run (also exported)
            res.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            res.new_files = storage.files_created - files_before
            this_run = _run_history_row(res, sources)
            history = pd.concat([read_log(storage, "run_history.csv"), pd.DataFrame([this_run])], ignore_index=True)
            all_invalid = pd.concat([read_log(storage, "invalid_rows.csv"), pd.DataFrame(invalid_log)], ignore_index=True)
            all_ingestion = pd.concat([read_log(storage, "ingestion_log.csv"), pd.DataFrame(log_rows)], ignore_index=True)
            exports = build_exports(
                classified, tables, batch_id=batch_id,
                source_files=[f"{s['raw_file_name']} (email {s['email_id']})" for s in sources],
                run_history=history.fillna(""), invalid_rows=all_invalid.fillna(""),
                ingestion_log=all_ingestion.fillna(""), include_contacts=settings.include_contact_fields)
            written = write_exports(storage, exports)
            step("exports", f"Dashboard exports refreshed ({len(written)} CSVs)", "dashboard_exports/")

            # ---- the manifest is the LAST storage write of a successful run ----
            write_manifest(storage, {
                "batch_id": batch_id, "report_date": report_date.isoformat(), "mode": settings.mode,
                "trigger": trigger, "run_status": res.status,
                "datasets": {
                    "requests_current": file_entry(f_req, rows=len(dd.requests_current)),
                    "id_registry": file_entry(f_reg, rows=len(dd.id_registry)),
                    "requests_classified": file_entry(f_cls, rows=len(classified)),
                    "kpis": file_entry(f_kpi),
                },
                "exports": {name: file_entry(f) for name, f in written.items()},
                "sources": sources,
                "previous_batch_id": manifest.get("batch_id", ""),
            })
            res.manifest_updated = True
            step("manifest", "Manifest updated", f"batch {batch_id}")
    except LiveSetupError:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, manifest untouched
        log.exception("Pipeline failed after ingestion")
        res.status = "FAILURE"
        res.errors.append(f"{type(exc).__name__}: {exc}")
        step("done", "Run failed - manifest NOT updated, previous datasets still current", str(exc), ok=False)

    # ------------------------------------------------------------------ 9. logs (in place)
    if res.errors:
        # Failed after storing snapshots: don't mark them processed, so the next run retries them.
        processed_rows = [p for p in processed_rows if p["status"] != "PROCESSED"]
    res.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        append_log(storage, "processed_attachments.csv", processed_rows)
        append_log(storage, "invalid_rows.csv", invalid_log if not res.errors else [])
        append_log(storage, "ingestion_log.csv", log_rows)
        res.new_files = storage.files_created - files_before
        append_log(storage, "run_history.csv", [_run_history_row(res, sources)])
        res.new_files = storage.files_created - files_before
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"Could not write pipeline logs: {exc}")
    if not res.errors and res.manifest_updated:
        step("done", f"Done: {res.status}", f"{res.new_files} new files, {len(res.new_ids)} new IDs")
    elif not res.errors and res.nothing_new:
        pass
    return res


def _run_history_row(res: RunResult, sources: list[dict]) -> dict:
    k = res.kpis or {}
    return {
        "ingestion_batch_id": res.batch_id, "trigger": res.trigger, "mode": res.mode,
        "report_date": res.report_date.isoformat(), "started_at": res.started_at, "finished_at": res.finished_at,
        "status": res.status,
        "divisions_ok": ";".join(d for d, r in res.divisions.items() if r.status in OK_STATUSES),
        "divisions_problem": ";".join(f"{d}:{r.status}" for d, r in res.divisions.items() if r.status not in OK_STATUSES),
        "new_files": res.new_files, "snapshot_rows_ingested": res.snapshot_rows_ingested,
        "new_requests": res.new_requests, "updated_requests": res.updated_requests,
        "flagged_requests": res.flagged_requests, "requests_total": res.requests_after,
        "on_time_pct": k.get("on_time_pct", ""), "source_files": ";".join(s["raw_file_name"] for s in sources),
        "message": "; ".join(res.errors) if res.errors else ("nothing new" if res.nothing_new else ""),
    }


def latest_dataset_or_empty(storage: StorageBackend, name: str) -> pd.DataFrame | None:
    try:
        return load_latest_dataset(storage, name)
    except DatasetNotFound:
        return None


def reclassify(*, settings: Settings | None = None, storage: StorageBackend | None = None,
               strict_holidays: bool | None = None) -> dict:
    """Re-run modules 3-4 on the stored requests, without reading any email.

    Use after a late-logic rule or config change (SLA, holidays). The requests and ID
    registry are reused as they are; a new classified dataset, KPI bundle and dashboard
    exports are written, and the manifest (written last) points to them.
    """
    settings = settings or load_settings()
    if not settings.is_mock:
        validate_live_settings(settings)
    storage = storage or make_storage(settings)
    manifest = read_manifest(storage)
    if not manifest:
        raise DatasetNotFound("No manifest yet - run the pipeline first.")
    current = load_latest_dataset(storage, "requests_current")
    batch_id = make_batch_id(date.fromisoformat(manifest["report_date"])) + "-rc"
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cfg = default_config(settings, strict_holidays=strict_holidays)
    classified = classify_requests(current, report_dates_by_division(storage), config=cfg)
    tables = aggregate(classified, cfg, snapshot_rows_total=total_snapshot_rows(storage))
    summary = tables["on_time_summary"].iloc[0]

    meta = {"kind": "dataset", "batch_id": batch_id}
    f_cls = save_dataframe(storage, "processed_data", f"requests_classified__{batch_id}.csv",
                           classified, CLASSIFIED_COLUMNS, {**meta, "dataset": "requests_classified"})
    bundle = json.dumps({k: _json_records(v) for k, v in tables.items()}, indent=1).encode()
    f_kpi = storage.create_file("processed_data", f"kpis__{batch_id}.json", bundle, JSON_MIME,
                                {**meta, "dataset": "kpis"})

    history_row = {"ingestion_batch_id": batch_id, "trigger": "reclassify", "mode": settings.mode,
                   "report_date": manifest["report_date"], "started_at": started,
                   "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "SUCCESS",
                   "new_files": 2, "snapshot_rows_ingested": 0, "requests_total": len(current),
                   "on_time_pct": summary["on_time_pct"], "message": "late logic re-run on stored requests"}
    history = pd.concat([read_log(storage, "run_history.csv"), pd.DataFrame([history_row])], ignore_index=True)
    append_log(storage, "run_history.csv", [history_row])
    sources = manifest.get("sources") or []
    exports = build_exports(
        classified, tables, batch_id=batch_id,
        source_files=[f"{s.get('raw_file_name', '')} (email {s.get('email_id', '')})" for s in sources],
        run_history=history.fillna(""), invalid_rows=read_log(storage, "invalid_rows.csv").fillna(""),
        ingestion_log=read_log(storage, "ingestion_log.csv").fillna(""),
        include_contacts=settings.include_contact_fields)
    written = write_exports(storage, exports)

    new_manifest = dict(manifest)
    new_manifest.update({
        "batch_id": batch_id, "mode": settings.mode, "trigger": "reclassify", "run_status": "SUCCESS",
        "previous_batch_id": manifest.get("batch_id", ""),
        "datasets": {**manifest["datasets"],
                     "requests_classified": file_entry(f_cls, rows=len(classified)),
                     "kpis": file_entry(f_kpi)},
        "exports": {name: file_entry(f) for name, f in written.items()},
    })
    write_manifest(storage, new_manifest)
    return {"batch_id": batch_id, **{k: summary[k] for k in ("on_time_pct", "numerator", "denominator")}}
