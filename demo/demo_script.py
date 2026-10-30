"""Terminal fallback for the 4 live demos (use it if the Streamlit UI fails mid-presentation).

    python demo/demo_script.py            # reset, preload history, then walk through demos 1-4
    python demo/demo_script.py --no-pause # run straight through (no "press Enter" stops)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO_DIR))
sys.path.insert(0, str(DEMO_DIR.parent))

import pandas as pd  # noqa: E402
from demo_state import DemoController  # noqa: E402

from isd_pipeline.dedupe.matching import build_match_key  # noqa: E402
from isd_pipeline.ingestion.ingestion_log import read_log  # noqa: E402
from isd_pipeline.ingestion.manifest import load_kpi_bundle, load_latest_dataset, read_manifest  # noqa: E402
from isd_pipeline.ingestion.mock_gmail_adapter import MockGmailAdapter  # noqa: E402
from isd_pipeline.ingestion.snapshots import load_snapshots  # noqa: E402
from isd_pipeline.ingestion.storage_base import FOLDERS  # noqa: E402
from isd_pipeline.late_logic.rules import explain_status  # noqa: E402

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)
pd.set_option("display.max_colwidth", 45)


def banner(mode: str, title: str) -> None:
    print("\n" + "=" * 100)
    print(f"[{mode}]  {title}")
    print("=" * 100)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-pause", action="store_true")
    ap.add_argument("--keep", action="store_true", help="don't reset; continue from the current demo state")
    args = ap.parse_args(argv)
    pause = (lambda: None) if args.no_pause else (lambda: input("\n   ⏎  press Enter to continue… "))

    ctl = DemoController()
    if not ctl.is_mock:
        print("The terminal demo runs in MOCK mode only (set mode: mock in settings.yaml).")
        return 1
    MODE = "MOCK"
    storage = ctl.storage()
    if not args.keep:
        print(f"[{MODE}] Resetting the demo and preloading the history before {ctl.demo_day} …")
        ctl.reset()
        ctl.preload_history(lambda d, r: print(f"   preloaded {d}: {r.status}"))

    # ------------------------------------------------------------------ Demo 1
    day = ctl.deliver_next_day()
    banner(MODE, f"DEMO 1 · A new report arrives ({day:%A %m/%d/%Y})")
    for m in MockGmailAdapter(ctl.settings.mock_inbox_dir).list_messages()[:3]:
        t = m.received_at.strftime("%-I:%M %p")
        print(f"  ✉  {m.sender_name:<20} {m.subject:<52} {t:>8}   📎 {m.attachments[0].filename[:15]}…")
    pause()
    print("\n  ▶ Run Pipeline")
    res = ctl.run(day, on_step=lambda ev: (print(f"   {'✔' if ev.ok else '⚠'} {ev.label}" +
                                                   (f"  — {ev.detail}" if ev.detail else "")), time.sleep(0.2)))
    print()
    for div, dr in res.divisions.items():
        print(f"  {div:<13} {dr.status:<10} rows={dr.rows:<4} header_row={dr.header_row}  "
              f"inspector_cells_filled={dr.inspector_cells_filled:<3} invalid_rows={dr.invalid_rows}")
    pause()

    # ------------------------------------------------------------------ Demo 2
    banner(MODE, "DEMO 2 · Data updates automatically")
    since = pd.Timestamp(res.started_at)
    for folder in FOLDERS:
        files = storage.list_files(folder)
        new = [f for f in files if pd.Timestamp(f.created_time) >= since]
        print(f"  📁 {folder:<26} {len(files):>4} files   ({len(new)} new this run)")
        for f in new[:4]:
            print(f"       + {f.name:<55} id={f.file_id}")
    print(f"\n  snapshot rows ingested: {res.snapshot_rows_ingested} | new requests: {res.new_requests} | "
          f"updated: {res.updated_requests} | requests_current {res.requests_before} → {res.requests_after} | "
          f"duplicates collapsed (all time): {res.duplicates_collapsed}")
    log = read_log(storage, "ingestion_log.csv")
    print("\n  Ingestion log for this batch (trigger = pipeline, no manual upload):")
    print(log[log["ingestion_batch_id"] == res.batch_id][["division", "status", "email_id", "rows_extracted",
                                                          "raw_file_id", "trigger"]].to_string(index=False))
    pause()
    print("\n  ▶ Run the same day again …")
    again = ctl.run(day)
    print(f"   → {again.status}: {again.new_files} new files, {len(again.new_ids)} new IDs (idempotent)")
    nxt = ctl.deliver_next_day()
    if nxt:
        print(f"\n  ▶ Deliver {nxt:%m/%d} emails and run")
        r2 = ctl.run(nxt)
        print(f"   → {r2.status} ({', '.join(r2.problem_divisions) or 'all divisions OK'}); "
              f"requests_current {r2.requests_before} → {r2.requests_after}")
    pause()

    # ------------------------------------------------------------------ Demo 3
    banner(MODE, "DEMO 3 · The business logic runs")
    cls = load_latest_dataset(storage, "requests_classified")
    print(cls["dedupe_mark"].value_counts().to_string(), "\n")
    print(cls["status"].value_counts().to_string(), "\n")
    sc = next(s for s in ctl.storyline["scenarios"] if s["scenario"] == "spotlight")
    req = cls[(cls["ap_number"] == sc["ap_number"]) & (cls["insp_type"] == sc["insp_type"])].iloc[0]
    snaps = load_snapshots(storage=storage)
    snaps = snaps[snaps["ap_number"] == req["ap_number"]]
    snaps = snaps[[build_match_key(r, ctl.settings.address_abbreviations) == req["match_key"]
                   for r in snaps.to_dict("records")]]
    print(f"  SPOTLIGHT {req['inspection_request_id']}: {len(snaps)} snapshot rows → 1 request")
    print(snaps[["report_date", "address", "inspector_notes", "is_red"]].to_string(index=False))
    print(f"\n  call {req['call_date']:%a %m/%d} → due {req['due_date']:%a %m/%d}; as_of {req['as_of']:%a %m/%d}")
    for name, hit, detail in explain_status(req):
        print(f"   {'TRUE ' if hit else 'false'}  {name:<42} {detail}")
        if hit:
            break
    print(f"\n  Red font says: {'late' if req['ever_red'] else 'not late'}   |   Our logic says: {req['status']}"
          f"   ({req['mismatch_reason'] or 'agree'})")
    pause()

    # ------------------------------------------------------------------ Demo 4
    banner(MODE, "DEMO 4 · The dashboard refreshes")
    k = load_kpi_bundle(storage)
    s = k["on_time_summary"].iloc[0]
    print(f"  On-time rate        {s['on_time_pct']}%  ({int(s['numerator'])}/{int(s['denominator'])} judged)")
    print(f"  Open-overdue backlog {int(s['open_overdue_backlog'])}")
    print(f"  Mislabeled red flags {int(s['font_mismatches'])}")
    print(k["mismatch_by_reason"].to_string(index=False))
    print(f"  Duplicates collapsed {int(s['duplicates_collapsed'])}  ({int(s['snapshot_rows_ingested'])} rows → "
          f"{int(s['unique_requests'])} requests)")
    print(f"  Review queue        {int(s['review_queue'])}")
    print("\n  On-time by division:")
    print(k["on_time_by_division"][["division", "on_time_pct", "numerator", "denominator", "low_n"]].to_string(index=False))
    man = read_manifest(storage)
    print(f"\n  Provenance: batch {man['batch_id']} (report {man['report_date']}), refreshed {man['updated_at']}")
    for src in man.get("sources", []):
        print(f"   {src['division']:<13} email {src['email_id']} received {src['received_at']}  "
              f"raw={src['raw_file_id']}  snapshot={src['snapshot_file_id']}")
    print(f"\n[{MODE}] Demo complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
