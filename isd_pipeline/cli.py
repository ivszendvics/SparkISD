"""Command line entry point:  python -m isd_pipeline.cli <command> [options]

Commands
  run          run the daily pipeline for one report date (default: today, America/New_York)
  backfill     run every weekday in a date range, oldest first
  check-gmail  dry run: list the matching emails for a date (no download)
  check-drive  create / verify the Drive folder tree and print folder ids
  status       show the latest manifest and KPIs
  schedule     print cron (macOS/Linux) and Windows Task Scheduler examples

Common options: --mock / --live (override settings.yaml), --lenient (holiday years), -v (verbose)
Exit codes: 0 SUCCESS, 2 PARTIAL_SUCCESS, 1 FAILURE or setup error.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from isd_pipeline.errors import PipelineError
from isd_pipeline.late_logic.calendar import HolidayYearMissingError
from isd_pipeline.settings import PROJECT_ROOT, Settings, load_settings

EXIT = {"SUCCESS": 0, "PARTIAL_SUCCESS": 2, "FAILURE": 1}


def _settings(args) -> Settings:
    mode = "live" if getattr(args, "live", False) else ("mock" if getattr(args, "mock", False) else None)
    s = load_settings(mode=mode)
    if getattr(args, "lenient", False):
        s = s.with_overrides(strict_holidays=False)
    return s


def _date(text: str | None, settings: Settings) -> date:
    return date.fromisoformat(text) if text else datetime.now(ZoneInfo(settings.timezone)).date()


def cmd_run(args) -> int:
    from isd_pipeline.pipeline import run_daily_pipeline
    s = _settings(args)
    res = run_daily_pipeline(_date(args.date, s), settings=s, trigger=args.trigger)
    print(res.summary_text())
    return EXIT[res.status]


def cmd_backfill(args) -> int:
    from isd_pipeline.factory import make_email_source, make_storage
    from isd_pipeline.pipeline import run_daily_pipeline
    s = _settings(args)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    storage, source = make_storage(s), make_email_source(s)
    worst = 0
    d = start
    while d <= end:
        if d.weekday() < 5 or args.include_weekends:
            res = run_daily_pipeline(d, settings=s, storage=storage, email_source=source, trigger="backfill")
            print(res.summary_text().splitlines()[0])
            worst = max(worst, EXIT[res.status] if res.status != "FAILURE" else 3)
        d += timedelta(days=1)
    return {0: 0, 2: 2, 3: 1}[worst]


def cmd_check_gmail(args) -> int:
    from isd_pipeline.factory import make_email_source
    from isd_pipeline.ingestion.email_base import EmailQuery, find_daily_reports
    s = _settings(args)
    d = _date(args.date, s)
    src = make_email_source(s)
    rules = s.email_rules
    q = EmailQuery(subject="<subject>", after=d - timedelta(days=rules.search_padding_days),
                   before=d + timedelta(days=rules.search_padding_days), sender_address=rules.sender_address,
                   label=rules.label, has_attachment=rules.has_attachment)
    print(f"[{s.mode.upper()}] {src.describe()}\nGmail query per division: {q.to_gmail_query()}\n")
    fr = find_daily_reports(src, rules, d, tz=s.timezone, download=False, retry_attempts=s.retry_attempts,
                            retry_base_delay=s.retry_base_delay)
    for div, f in fr.divisions.items():
        if f.attachment:
            a = f.attachment
            print(f"  ✔ {div:<13} email {a.email_id}  {a.received_at.astimezone(ZoneInfo(s.timezone)):%Y-%m-%d %H:%M}  "
                  f"{a.sender}\n      subject: {a.subject}\n      attachment: {a.filename} ({a.size:,} bytes)")
        else:
            print(f"  ✘ {div:<13} {f.status}: {f.message}")
        for ign in f.ignored:
            print(f"      ignored {ign['email_id']}: {ign['reason']} ({ign['detail']})")
    for w in fr.warnings:
        print(f"  warning: {w}")
    print("\nNothing was downloaded.")
    return 0 if not fr.missing else 2


def cmd_check_drive(args) -> int:
    from isd_pipeline.factory import make_storage
    s = _settings(args)
    st = make_storage(s)
    folders = st.ensure_folders()
    print(f"[{s.mode.upper()}] {st.describe()}")
    for path, fid in folders.items():
        print(f"  {'  ' * path.count('/')}📁 {path.split('/')[-1]:<24} id={fid}  ({len(st.list_files(path))} files)")
    print("Folder tree OK.")
    return 0


def cmd_reclassify(args) -> int:
    from isd_pipeline.pipeline import reclassify
    s = _settings(args)
    out = reclassify(settings=s, strict_holidays=False if getattr(args, "lenient", False) else None)
    print(f"[{s.mode.upper()}] reclassified as batch {out['batch_id']}: on-time {out['on_time_pct']}% "
          f"({out['numerator']}/{out['denominator']} judged). No email was read.")
    return 0


def cmd_status(args) -> int:
    from isd_pipeline.factory import make_storage
    from isd_pipeline.ingestion.manifest import load_kpi_bundle, read_manifest
    s = _settings(args)
    st = make_storage(s)
    man = read_manifest(st)
    if not man:
        print("No successful run yet (no manifest).")
        return 1
    print(f"[{s.mode.upper()}] latest batch {man['batch_id']} · report {man['report_date']} · updated {man['updated_at']}")
    for name, e in man["datasets"].items():
        print(f"  {name:<20} {e['name']}  id={e['file_id']}  rows={e.get('rows', '')}")
    k = load_kpi_bundle(st)["on_time_summary"].iloc[0]
    print(f"  on-time {k['on_time_pct']}% ({k['numerator']}/{k['denominator']}), backlog {k['open_overdue_backlog']}, "
          f"red-font mismatches {k['font_mismatches']}, duplicates collapsed {k['duplicates_collapsed']}, "
          f"review queue {k['review_queue']}")
    return 0


def cmd_schedule(args) -> int:
    s = _settings(args)
    hh, mm = s.run_time.split(":")
    py = PROJECT_ROOT / ".venv" / "bin" / "python"
    pyw = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    print(f"""Scheduled run time: {s.run_time} {s.timezone} (settings.yaml -> schedule.run_time)

macOS / Linux (crontab -e). cron uses the machine's local time - set the machine to Eastern time, or
add CRON_TZ=America/New_York on Linux:
  CRON_TZ=America/New_York
  {int(mm)} {int(hh)} * * 1-5 cd "{PROJECT_ROOT}" && "{py}" -m isd_pipeline.cli run --live --trigger scheduled >> "{PROJECT_ROOT}/pipeline_cron.log" 2>&1

Windows Task Scheduler (PowerShell, run once):
  $a = New-ScheduledTaskAction -Execute "{pyw}" -Argument "-m isd_pipeline.cli run --live --trigger scheduled" -WorkingDirectory "{PROJECT_ROOT}"
  $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At {s.run_time}
  Register-ScheduledTask -TaskName "ISD Permit Pipeline" -Action $a -Trigger $t

Each run searches the whole report day, so a late delivery is picked up by a later run
(e.g. re-run at noon: python -m isd_pipeline.cli run --date YYYY-MM-DD). The machine must be on.""")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m isd_pipeline.cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, lenient=False):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--mock", action="store_true", help="force mock adapters")
        g.add_argument("--live", action="store_true", help="force live Gmail + Drive")
        if lenient:
            sp.add_argument("--lenient", action="store_true", help="flag rows in years without holidays instead of failing")

    sp = sub.add_parser("run", help="run the pipeline for one report date")
    sp.add_argument("--date", help="YYYY-MM-DD (default: today in America/New_York)")
    sp.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "backfill"])
    common(sp, lenient=True)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("backfill", help="run every weekday in a range")
    sp.add_argument("--start", required=True)
    sp.add_argument("--end", required=True)
    sp.add_argument("--include-weekends", action="store_true")
    common(sp, lenient=True)
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser("check-gmail", help="list matching emails for a date (no download)")
    sp.add_argument("--date")
    common(sp)
    sp.set_defaults(func=cmd_check_gmail)

    sp = sub.add_parser("check-drive", help="create/verify the Drive folder tree")
    common(sp)
    sp.set_defaults(func=cmd_check_drive)

    sp = sub.add_parser("reclassify", help="re-run late logic + exports on stored data (after a rule/config change)")
    common(sp, lenient=True)
    sp.set_defaults(func=cmd_reclassify)

    sp = sub.add_parser("status", help="show the latest manifest and KPIs")
    common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("schedule", help="print cron / Task Scheduler examples")
    common(sp)
    sp.set_defaults(func=cmd_schedule)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (PipelineError, HolidayYearMissingError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
