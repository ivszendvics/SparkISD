"""ISD Permit Inspection Automation - live demo app (4 demos).

    streamlit run demo/app.py

Runs fully in MOCK mode with no credentials. The MOCK / LIVE badge is always visible.
"""
from __future__ import annotations

import html
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO_DIR))
sys.path.insert(0, str(DEMO_DIR.parent))

from demo_state import DemoController  # noqa: E402

from isd_pipeline.dedupe.matching import build_match_key  # noqa: E402
from isd_pipeline.ingestion.email_base import find_daily_reports  # noqa: E402
from isd_pipeline.ingestion.ingestion_log import read_log  # noqa: E402
from isd_pipeline.ingestion.manifest import load_kpi_bundle, load_latest_dataset, read_manifest  # noqa: E402
from isd_pipeline.ingestion.mock_gmail_adapter import MockGmailAdapter  # noqa: E402
from isd_pipeline.ingestion.snapshots import load_snapshots  # noqa: E402
from isd_pipeline.ingestion.storage_base import FOLDERS, read_named_csv  # noqa: E402
from isd_pipeline.late_logic.rules import explain_status  # noqa: E402

NY = ZoneInfo("America/New_York")

# Reference palette (dataviz skill): one blue series, muted grey for low-n, status colours + icons.
BLUE = "#2a78d6"
MUTED = "#c3c2b7"
GRID = "rgba(137,135,129,0.25)"
STATUS_LABEL = {
    "on_time": "✅ on time", "on_time_contacted": "📞 on time (contacted)", "open_not_due": "⏳ open, not due",
    "open_overdue": "⚠️ open overdue", "late": "❌ late", "unknown_close": "❔ unknown (report gap)",
    "invalid": "⛔ invalid",
}
MARK_LABEL = {"NEW": "🆕 NEW", "UPDATED": "✏️ UPDATED", "UNCHANGED": "▫️ UNCHANGED", "FLAGGED": "🚩 FLAGGED"}
REASON_LABEL = {
    "excused_by_note": "Excused by inspector note", "calendar_adjustment": "Weekend/holiday counted by source",
    "missed_by_source": "Late but NOT red in source", "other_red_not_late": "Red but not late (other)",
}

st.set_page_config(page_title="ISD Permit Inspection Automation", page_icon="🏗️", layout="wide")

st.markdown("""
<style>
.badge {display:inline-block;padding:3px 12px;border-radius:999px;font-weight:700;font-size:0.85rem;letter-spacing:.04em}
.badge-mock {background:#fab219;color:#0b0b0b}
.badge-live {background:#d03b3b;color:#fff}
.gmail {border:1px solid rgba(128,128,128,.25);border-radius:10px;overflow:hidden;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
.gmail-top {padding:8px 14px;font-size:.85rem;opacity:.75;border-bottom:1px solid rgba(128,128,128,.25)}
.gmail-row {display:grid;grid-template-columns:190px 1fr 90px;gap:12px;padding:10px 14px;border-bottom:1px solid rgba(128,128,128,.15);align-items:start}
.gmail-row > div {min-width:0}
.gmail-row.unread {font-weight:600;background:rgba(42,120,214,.06)}
.gmail-row.old {opacity:.6}
.gmail-sender {white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gmail-subject {white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gmail-snippet {font-weight:400;opacity:.7}
.gmail-time {text-align:right;font-size:.85rem}
.chip {display:inline-flex;align-items:center;gap:6px;margin-top:6px;padding:3px 10px;border:1px solid rgba(128,128,128,.35);border-radius:999px;font-size:.8rem;font-weight:500}
.chip .x {background:#1d6f42;color:#fff;border-radius:3px;padding:0 4px;font-size:.7rem;font-weight:700}
.tree {font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem;line-height:1.55}
.tree .new {background:#0ca30c;color:#fff;border-radius:4px;padding:0 5px;font-size:.7rem;margin-left:6px}
.muted {opacity:.65}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
ctl = DemoController()
if ctl.is_mock:
    ctl.ensure_generated()
storage = ctl.storage()
storage.ensure_folders()
state = ctl.load_state()
MODE = "MOCK" if ctl.is_mock else "LIVE"
BADGE = f'<span class="badge badge-{MODE.lower()}">{MODE}</span>'


def badge_line(text: str) -> None:
    st.markdown(f"{BADGE} &nbsp; <span class='muted'>{html.escape(text)}</span>", unsafe_allow_html=True)


def current_day() -> date | None:
    if not ctl.is_mock:
        return st.session_state.get("live_date", datetime.now(NY).date())
    return date.fromisoformat(state.current_date) if state.current_date else None


def fmt_local(ts: str) -> str:
    if not ts:
        return ""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(NY).strftime("%a %b %d, %I:%M %p")


def load_data() -> dict:
    manifest = read_manifest(storage)
    data = {"manifest": manifest, "classified": None, "kpis": {}}
    if manifest:
        data["classified"] = load_latest_dataset(storage, "requests_classified")
        data["kpis"] = load_kpi_bundle(storage)
    return data


def run_with_live_steps(day: date | None = None, delay: float = 0.35):
    with st.status("Running the pipeline…", expanded=True) as status:
        def on_step(ev):
            icon = "✅" if ev.ok else "⚠️"
            status.write(f"{icon} **{ev.label}**" + (f" — {ev.detail}" if ev.detail else ""))
            time.sleep(delay)
        res = ctl.run(day, on_step=on_step)
        label = f"Pipeline finished: {res.status} — {res.new_files} new files, {len(res.new_ids)} new IDs"
        status.update(label=label, state="error" if res.status == "FAILURE" else "complete", expanded=True)
    st.session_state["last_run"] = res
    return res


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(f"## {BADGE}", unsafe_allow_html=True)
    if ctl.is_mock:
        st.caption("Mock Gmail inbox + mock Google Drive (local folders). No credentials used. "
                   "All data is synthetic.")
        date_slot = st.empty()      # filled at the end of the script so it reflects this run's clicks
        nxt = ctl.next_day()
        if st.button("🔄 Reset demo", width="stretch"):
            ctl.reset()
            st.session_state.clear()
            st.rerun()
        if st.button(f"⏩ Preload history (before {ctl.demo_day:%m/%d})", width="stretch"):
            with st.spinner("Processing the scripted days before demo day…"):
                rs = ctl.preload_history()
            st.session_state["last_run"] = rs[-1] if rs else st.session_state.get("last_run")
            st.toast(f"Preloaded {len(rs)} days")
            st.rerun()
        if st.button("📨 Deliver next day's emails", width="stretch", disabled=nxt is None):
            d = ctl.deliver_next_day()
            st.toast(f"3 Power BI emails for {d:%a %m/%d} landed in the mock inbox" if d else "No more days")
            st.rerun()
        st.divider()
        status_slot = st.empty()
    else:
        st.caption("LIVE: real Gmail (gmail.readonly) and Google Drive (drive.file).")
        st.session_state["live_date"] = st.date_input("Report date", value=datetime.now(NY).date())
    st.caption(f"Inbox: {ctl.email_source().describe()}")
    st.caption(f"Storage: {storage.describe()}")

st.markdown(f"# 🏗️ ISD Permit Inspection Automation &nbsp; {BADGE}", unsafe_allow_html=True)
st.caption("City of Boston Inspectional Services Department · BU DS539 capstone prototype")

tab1, tab2, tab3, tab4 = st.tabs(["1 · A new report arrives", "2 · Data updates automatically",
                                  "3 · The business logic runs", "4 · The dashboard refreshes"])

# ---------------------------------------------------------------------------
# Demo 1: a new report arrives
# ---------------------------------------------------------------------------
with tab1:
    badge_line("Emails are read straight from the inbox — nobody downloads or uploads anything.")
    day = current_day()
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Inbox")
        if ctl.is_mock:
            msgs = MockGmailAdapter(ctl.settings.mock_inbox_dir).list_messages()
        else:
            try:
                fr = find_daily_reports(ctl.email_source(), ctl.settings.email_rules, day, download=False)
                msgs = []
                for f in fr.divisions.values():
                    if f.attachment:
                        a = f.attachment
                        msgs.append(type("M", (), dict(sender_name=a.sender.split(" <")[0], subject=a.subject,
                                                         snippet=a.snippet, received_at=a.received_at,
                                                         attachments=[type("A", (), dict(filename=a.filename))])))
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
                msgs = []
        if not msgs:
            st.info("The inbox is empty. Use **Deliver next day's emails** (or **Preload history**) in the sidebar.")
        else:
            rows = []
            for i, m in enumerate(msgs[:12]):
                local = m.received_at.astimezone(NY)
                is_today = day is not None and local.date() == day
                cls = "unread" if is_today else "old"
                when = local.strftime("%-I:%M %p") if is_today else local.strftime("%b %-d")
                chips = "".join(
                    f'<span class="chip"><span class="x">X</span>{html.escape(a.filename[:15])}…</span>'
                    for a in m.attachments)
                rows.append(
                    f'<div class="gmail-row {cls}"><div class="gmail-sender">{html.escape(m.sender_name)}</div>'
                    f'<div><div class="gmail-subject">{html.escape(m.subject)} '
                    f'<span class="gmail-snippet">— {html.escape(m.snippet[:90])}</span></div>{chips}</div>'
                    f'<div class="gmail-time">{when}</div></div>')
            st.markdown(f'<div class="gmail"><div class="gmail-top">Primary · {MODE} inbox</div>{"".join(rows)}</div>',
                        unsafe_allow_html=True)
    with right:
        st.subheader("Run")
        st.write(f"Report date: **{day:%A %b %d, %Y}**" if day else "No report date yet.")
        if st.button("▶️ Run Pipeline", type="primary", disabled=day is None, key="run1"):
            run_with_live_steps()
        res = st.session_state.get("last_run")
        if res is not None:
            st.caption(f"Last run {res.batch_id}: **{res.status}**")
            with st.expander("Steps of the last run", expanded=False):
                for ev in res.steps:
                    st.write(("✅ " if ev.ok else "⚠️ ") + f"**{ev.label}**" + (f" — {ev.detail}" if ev.detail else ""))

    res = st.session_state.get("last_run")
    if res is not None:
        st.subheader(f"Extracted files — run {res.batch_id} ({res.report_date:%m/%d})")
        cols = st.columns(3)
        for col, (div, dr) in zip(cols, res.divisions.items()):
            with col:
                ok = dr.status in ("PROCESSED", "ALREADY_PROCESSED")
                st.markdown(f"#### {'✅' if ok else '⚠️'} {div}")
                st.caption(dr.status)
                if dr.status == "PROCESSED":
                    v = dr.validation or {}
                    st.markdown(
                        f"- **{dr.rows}** rows extracted\n"
                        f"- header detected on **row {dr.header_row}** (columns {v.get('primary_block', '')})\n"
                        f"- **{dr.inspector_cells_filled}** inspector cells filled down\n"
                        f"- **{dr.invalid_rows}** invalid rows logged\n"
                        f"- **{dr.red_rows}** red Call Date cells ({v.get('red_cells_checked', '')})")
                    if v.get("secondary_block_detected"):
                        st.info("A second side-by-side copy of the table was ignored.")
                    if v.get("ambiguous_colors"):
                        st.warning("; ".join(v["ambiguous_colors"]))
                    with st.expander("Preview rows"):
                        try:
                            snap = pd.read_csv(storage.get_file(dr.snapshot_file_id).location, dtype=str,
                                               keep_default_na=False) if ctl.is_mock else None
                            if snap is not None:
                                st.dataframe(snap[["inspector", "ward", "call_datetime", "ap_number", "address",
                                                   "insp_type", "inspector_notes", "is_red"]].head(15),
                                             hide_index=True)
                        except Exception as exc:  # noqa: BLE001
                            st.caption(f"(preview unavailable: {exc})")
                    if v.get("fill_log"):
                        with st.expander("Fill-down log"):
                            st.code("\n".join(v["fill_log"][:40]))
                else:
                    st.write(dr.message)

# ---------------------------------------------------------------------------
# Demo 2: data updates automatically
# ---------------------------------------------------------------------------
with tab2:
    badge_line("Every file below was created by the pipeline run — no manual upload or data entry.")
    res = st.session_state.get("last_run")
    b1, b2, _ = st.columns([2, 2, 3])
    with b1:
        nxt = ctl.next_day() if ctl.is_mock else None
        if st.button(f"📨 Deliver {nxt:%m/%d} emails → ▶️ Run" if nxt else "Storyline finished",
                     disabled=nxt is None, key="deliver_run"):
            ctl.deliver_next_day()
            state = ctl.load_state()
            res = run_with_live_steps(delay=0.15)
    with b2:
        if st.button("🔁 Run the same day again (idempotency)", disabled=current_day() is None, key="rerun"):
            res = run_with_live_steps(delay=0.05)
            if res.nothing_new:
                st.success(f"Idempotent: **{res.new_files} new files, {len(res.new_ids)} new IDs**. "
                           "Every attachment was already processed.")

    if res is not None:
        m = st.columns(5)
        m[0].metric("Snapshot rows ingested", res.snapshot_rows_ingested)
        m[1].metric("New requests added", res.new_requests)
        m[2].metric("Existing requests updated", res.updated_requests)
        m[3].metric("requests_current", res.requests_after, delta=res.requests_after - res.requests_before)
        m[4].metric("Duplicates collapsed (all time)", res.duplicates_collapsed or "—")
        st.caption(f"Run {res.batch_id} · {res.status} · {res.new_files} new Drive files · "
                   f"{len(res.new_ids)} new IDs" + (f" ({res.new_ids[0]} … {res.new_ids[-1]})" if res.new_ids else ""))

    st.subheader("Google Drive — Permit Inspection Automation/")
    since = pd.Timestamp(res.started_at) if res is not None else None
    lines = []
    for folder in FOLDERS:
        files = storage.list_files(folder)
        depth = folder.count("/")
        name = folder.split("/")[-1]
        lines.append(f"{'&nbsp;' * 4 * depth}📁 <b>{name}/</b> <span class='muted'>({len(files)} files)</span>")
        show = files[-6:] if folder != "dashboard_exports" else files
        if len(files) > len(show):
            lines.append(f"{'&nbsp;' * 4 * (depth + 1)}<span class='muted'>… {len(files) - len(show)} older files</span>")
        for f in show:
            created = pd.Timestamp(f.created_time)
            modified = pd.Timestamp(f.modified_time)
            new = since is not None and (created >= since)
            upd = since is not None and not new and modified >= since
            tag = "<span class='new'>NEW</span>" if new else ("<span class='new' style='background:#2a78d6'>UPDATED</span>" if upd else "")
            lines.append(f"{'&nbsp;' * 4 * (depth + 1)}📄 {html.escape(f.name)} "
                         f"<span class='muted'>id={f.file_id} · {fmt_local(f.created_time)}</span>{tag}")
    st.markdown(f"<div class='tree'>{'<br>'.join(lines)}</div>", unsafe_allow_html=True)

    st.subheader("Ingestion log for this batch")
    ilog = read_log(storage, "ingestion_log.csv")
    if res is not None and len(ilog):
        this = ilog[ilog["ingestion_batch_id"] == res.batch_id]
        st.caption(f"trigger = **{res.trigger}** (pipeline run) · mode = {res.mode} · "
                   "uploaded by: the pipeline, not a person")
        st.dataframe(this[["division", "status", "email_id", "filename", "received_at", "header_row", "rows_extracted",
                           "invalid_rows", "inspector_cells_filled", "raw_file_id", "snapshot_file_id", "message"]],
                     hide_index=True)
    elif len(ilog):
        st.dataframe(ilog.tail(10), hide_index=True)
    else:
        st.info("No runs yet.")

# ---------------------------------------------------------------------------
# Demo 3: the business logic runs
# ---------------------------------------------------------------------------
data = load_data()
cls = data["classified"]
with tab3:
    badge_line("Duplicate detection → one row per request with a persistent ID → ISD's on-time rules.")
    if cls is None or cls.empty:
        st.info("Run the pipeline first (Demo 1) or click **Preload history**.")
    else:
        f1, f2, f3 = st.columns(3)
        divs = f1.multiselect("Division", sorted(cls["division"].unique()))
        stats = f2.multiselect("Status", list(STATUS_LABEL), format_func=lambda s: STATUS_LABEL[s])
        marks = f3.multiselect("Dedupe mark", list(MARK_LABEL), format_func=lambda s: MARK_LABEL[s])
        view = cls.copy()
        if divs:
            view = view[view["division"].isin(divs)]
        if stats:
            view = view[view["status"].isin(stats)]
        if marks:
            view = view[view["dedupe_mark"].isin(marks)]
        table = pd.DataFrame({
            "ID": view["inspection_request_id"], "Mark": view["dedupe_mark"].map(MARK_LABEL),
            "Division": view["division"], "Ap #": view["ap_number"], "Insp type": view["insp_type"],
            "Inspector": view["inspector"], "Reports": view["n_reports"],
            "Call date": view["call_datetime"].dt.strftime("%m/%d %I:%M %p"),
            "Due": view["due_date"].dt.strftime("%a %m/%d"), "Status": view["status"].map(STATUS_LABEL),
            "Bus. days late": view["business_days_late"],
            "Red font": view["ever_red"].map({True: "🔴 red", False: ""}),
            "Mismatch reason": view["mismatch_reason"].map(REASON_LABEL).fillna(""),
            "Review": view["review_reason"],
        })
        st.caption(f"{len(view)} of {len(cls)} requests")
        st.dataframe(table, hide_index=True, height=360)

        st.subheader("🔦 Spotlight: one request across many reports")
        story = ctl.storyline if ctl.is_mock else {"scenarios": []}
        options = {}
        for sc in story["scenarios"]:
            m = cls[(cls["ap_number"] == sc["ap_number"]) & (cls["insp_type"] == sc["insp_type"])
                    & (cls["call_datetime"] == pd.Timestamp(sc["call_datetime"]))]
            for _, r in m.iterrows():
                options[f"{r['inspection_request_id']} · {sc['scenario']} — {sc['description']}"] = r["inspection_request_id"]
        multi = cls.sort_values("n_reports", ascending=False).head(15)
        for _, r in multi.iterrows():
            options.setdefault(f"{r['inspection_request_id']} · {r['n_reports']} reports · {r['ap_number']}",
                               r["inspection_request_id"])
        choice = st.selectbox("Request", list(options))
        req = cls[cls["inspection_request_id"] == options[choice]].iloc[0]
        snaps = load_snapshots(storage=storage)
        snaps = snaps[snaps["ap_number"] == req["ap_number"]]
        abbr = ctl.settings.address_abbreviations
        snaps = snaps[[build_match_key(r, abbr) == req["match_key"] for r in snaps.to_dict("records")]] \
            if not req["match_key"].startswith("UNMATCHED") else snaps.head(0)
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(f"**{len(snaps)} snapshot rows → 1 request `{req['inspection_request_id']}`** "
                        f"({req['division']}, Ap # {req['ap_number']}, {req['insp_type']})")
            if len(snaps):
                st.dataframe(pd.DataFrame({
                    "Report": snaps["report_date"].dt.strftime("%a %m/%d"),
                    "Address as written": snaps["address"], "Inspector notes": snaps["inspector_notes"],
                    "Contact": snaps["contact_info"], "Red": snaps["is_red"].map({True: "🔴", False: ""}),
                    "Source file": snaps["source_filename"]}), hide_index=True)
            st.markdown(f"Current row keeps the **latest non-blank** values → notes: *“{req['inspector_notes']}”* · "
                        f"first meaningful note: **{req['first_note_date']:%a %m/%d}**"
                        if pd.notna(req["first_note_date"]) else
                        f"Current row notes: *“{req['inspector_notes']}”* · no meaningful note yet")
            if req["review_flag"]:
                st.warning(f"🚩 Review queue: {req['review_reason']}")
        with c2:
            st.markdown(f"**How the status was decided** (call {req['call_date']:%a %m/%d} → due "
                        f"{req['due_date']:%a %m/%d}, as of {req['as_of']:%a %m/%d})"
                        if pd.notna(req["call_date"]) else "**How the status was decided**")
            for name, hit, detail in explain_status(req):
                st.markdown(f"{'🟢 **TRUE**' if hit else '⚪ false'} &nbsp; `{name}`  \n<span class='muted'>{detail}</span>",
                            unsafe_allow_html=True)
                if hit:
                    break
            st.markdown("---")
            red_says = "late 🔴" if req["ever_red"] else "not late"
            ours = STATUS_LABEL[req["status"]]
            st.markdown(f"**Red font says:** {red_says}  \n**Our logic says:** {ours}")
            if req["mismatch_reason"]:
                st.info(f"Mismatch: {REASON_LABEL[req['mismatch_reason']]}")

        st.subheader("Red font vs. our logic (judged requests)")
        judged = cls[cls["is_judged"]]
        rv = pd.crosstab(judged["ever_red"].map({True: "🔴 red (source says late)", False: "not red"}),
                         judged["is_late"].map({True: "late (our rules)", False: "on time (our rules)"}))
        rc1, rc2 = st.columns([2, 3])
        rc1.dataframe(rv)
        mm = judged[judged["font_mismatch"].fillna(False).astype(bool)]
        rc2.dataframe(pd.DataFrame({
            "ID": mm["inspection_request_id"], "Division": mm["division"],
            "Red font says": mm["ever_red"].map({True: "late", False: "not late"}),
            "Our logic says": mm["status"].map(STATUS_LABEL),
            "Reason": mm["mismatch_reason"].map(REASON_LABEL)}), hide_index=True, height=240)

# ---------------------------------------------------------------------------
# Demo 4: the dashboard refreshes
# ---------------------------------------------------------------------------
def _layout(fig: go.Figure, height: int, pct_axis: str | None = "x") -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=8, b=8), showlegend=False, bargap=0.35,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family='system-ui,-apple-system,"Segoe UI",sans-serif'))
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    ticks = dict(tickvals=[0, 25, 50, 75, 100], ticksuffix="%")
    if pct_axis == "x":      # extra room on the right so the "% (n/d) · low n" labels aren't clipped
        fig.update_xaxes(range=[0, 155], **ticks)
    elif pct_axis == "y":
        fig.update_yaxes(range=[0, 105], **ticks)
    return fig


def rate_bar(t: pd.DataFrame, label_col: str, grey_low_n: bool = False) -> go.Figure:
    t = t.sort_values("on_time_pct", na_position="first")
    colors = [MUTED if (grey_low_n and low) else BLUE for low in t["low_n"]]
    text = [f"{p:.1f}%  ({n}/{d}){' · low n' if low else ''}" if pd.notna(p) else "no judged requests"
            for p, n, d, low in zip(t["on_time_pct"], t["numerator"], t["denominator"], t["low_n"])]
    fig = go.Figure(go.Bar(
        x=t["on_time_pct"], y=t[label_col], orientation="h", marker=dict(color=colors, cornerradius=4),
        text=text, textposition="outside", cliponaxis=False,
        customdata=t[["numerator", "denominator"]].to_numpy(),
        hovertemplate="%{y}<br>On time %{x:.1f}%<br>%{customdata[0]} of %{customdata[1]} judged<extra></extra>"))
    return _layout(fig, 60 + 34 * len(t))


with tab4:
    badge_line("Every number below is read back from Drive through the manifest (load_latest_dataset).")
    k = data["kpis"]
    if not k:
        st.info("Run the pipeline first (Demo 1) or click **Preload history**.")
    else:
        s = k["on_time_summary"].iloc[0]
        hist = read_log(storage, "run_history.csv")
        hist = hist[hist["on_time_pct"] != ""]
        delta = None
        if len(hist) >= 2:
            delta = round(float(hist.iloc[-1]["on_time_pct"]) - float(hist.iloc[-2]["on_time_pct"]), 1)
        mm = k["mismatch_by_reason"]
        c = st.columns(6)
        c[0].metric("On-time rate", f"{s['on_time_pct']}%", delta=f"{delta:+.1f} pts vs prior run" if delta is not None else None,
                    help="(on_time + on_time_contacted) / judged requests")
        c[0].caption(f"{int(s['numerator'])} of {int(s['denominator'])} judged requests")
        c[1].metric("Open-overdue backlog", int(s["open_overdue_backlog"]))
        c[1].caption(f"median {s['median_business_days_late_backlog']:.0f} business days late")
        c[2].metric("Mislabeled red flags", int(s["font_mismatches"]))
        c[2].caption(" · ".join(f"{REASON_LABEL[r].split(' ')[0]} {n}" for r, n in zip(mm["mismatch_reason"], mm["count"]) if n))
        c[3].metric("Duplicates collapsed", f"{int(s['duplicates_collapsed']):,}")
        c[3].caption(f"{int(s['snapshot_rows_ingested']):,} rows → {int(s['unique_requests']):,} requests")
        c[4].metric("Review queue", int(s["review_queue"]))
        c[4].caption(f"{int(s['dq_flagged_requests'])} requests with dq flags")
        updated = data["manifest"].get("updated_at", "")
        c[5].metric("Last refresh", fmt_local(updated).split(", ")[-1] if updated else "—")
        c[5].caption(f"{fmt_local(updated)} · data as of {s['latest_as_of']}")

        st.markdown("##### Mislabeled red flags by reason")
        st.dataframe(pd.DataFrame({"Reason": mm["mismatch_reason"].map(REASON_LABEL), "Requests": mm["count"]}),
                     hide_index=True)

        g1, g2 = st.columns(2)
        with g1:
            st.markdown("##### On-time % by division")
            st.plotly_chart(rate_bar(k["on_time_by_division"], "division"), width="stretch")
            st.markdown("##### On-time % by week of call")
            w = k["on_time_by_call_week"].sort_values("call_week")
            w = w[w["denominator"] > 0]
            # Weeks before the first report only contain requests still open on day 1 (survivor bias).
            first_report = cls["first_seen_report_date"].min()
            first_week = (first_report - pd.Timedelta(days=first_report.weekday())).strftime("%Y-%m-%d")
            w = w[w["call_week"] >= first_week]
            fig = go.Figure(go.Scatter(
                x=w["call_week"], y=w["on_time_pct"], mode="lines+markers",
                line=dict(color=BLUE, width=2),
                marker=dict(size=9, color=[MUTED if l else BLUE for l in w["low_n"]], line=dict(width=2, color=BLUE)),
                customdata=w[["numerator", "denominator"]].to_numpy(),
                hovertemplate="Week of %{x}<br>On time %{y:.1f}%<br>%{customdata[0]} of %{customdata[1]} judged<extra></extra>"))
            st.plotly_chart(_layout(fig, 260, pct_axis="y"), width="stretch")
            st.caption(f"Weeks from {first_week} (first report). Grey markers: fewer than 10 judged requests (low n).")
        with g2:
            st.markdown("##### On-time % by inspector")
            st.plotly_chart(rate_bar(k["on_time_by_inspector"], "inspector", grey_low_n=True), width="stretch")
            st.caption("Grey bars: fewer than 10 judged requests (low n) — don't rank on these.")
            st.markdown("##### Open-overdue backlog by inspector")
            b = k["backlog"]
            b = b[b["scope"] == "inspector"].sort_values("open_overdue")
            b_max = max(1, int(b["open_overdue"].max())) if len(b) else 1
            fig = go.Figure(go.Bar(
                x=b["open_overdue"], y=b["group"], orientation="h", marker=dict(color=BLUE, cornerradius=4),
                text=[f"{n} · median {m:.0f} bd late" for n, m in zip(b["open_overdue"], b["median_business_days_late"])],
                textposition="outside", cliponaxis=False,
                hovertemplate="%{y}<br>%{x} open overdue<extra></extra>"))
            fig = _layout(fig, 60 + 34 * len(b), pct_axis=None)
            fig.update_xaxes(range=[0, b_max * 1.6])
            st.plotly_chart(fig, width="stretch")

        st.subheader("⚠️ Abnormal records")
        a1, a2, a3, a4 = st.tabs(["Review queue", "Data-quality flags", "Invalid rows", "Missing / invalid reports"])
        with a1:
            rq = cls[cls["review_flag"]]
            st.dataframe(rq[["inspection_request_id", "review_reason", "division", "ap_number", "address",
                             "call_datetime", "source_filename", "source_email_id", "source_row_number"]], hide_index=True)
        dq = read_named_csv(storage, "dashboard_exports", "data_quality.csv")
        with a2:
            if dq is not None:
                d2 = dq[dq["issue_type"] == "dq_flag"]
                st.dataframe(d2[["issue", "inspection_request_id", "division", "report_date", "source_filename",
                                 "source_email_id"]], hide_index=True)
                st.caption("report_gap = a business day between first and last seen has no valid report for that division")
        with a3:
            inv = read_log(storage, "invalid_rows.csv")
            st.dataframe(inv[["report_date", "division", "source_row_number", "reason", "source_filename",
                              "source_email_id", "values"]], hide_index=True)
        with a4:
            if dq is not None:
                d4 = dq[dq["issue_type"] == "report_problem"]
                st.dataframe(d4[["report_date", "division", "issue", "detail", "source_filename", "source_email_id"]],
                             hide_index=True)

        st.subheader("🧾 Data provenance for this refresh")
        man = data["manifest"]
        st.markdown(f"Batch **`{man.get('batch_id')}`** · report date **{man.get('report_date')}** · "
                    f"mode **{man.get('mode', '').upper()}** · trigger **{man.get('trigger')}** · "
                    f"refreshed {fmt_local(man.get('updated_at', ''))}")
        src = pd.DataFrame(man.get("sources", []))
        if len(src):
            src["received_at"] = src["received_at"].map(fmt_local)
            st.dataframe(src[["division", "email_id", "received_at", "subject", "filename", "raw_file_name",
                              "raw_file_id", "snapshot_file_id", "rows"]], hide_index=True)
        ds = pd.DataFrame([{"dataset": n, "file": v["name"], "drive_file_id": v["file_id"], "rows": str(v.get("rows", ""))}
                           for n, v in man.get("datasets", {}).items()])
        st.dataframe(ds, hide_index=True)
        st.caption("Every KPI traces back: KPI → dataset file id → batch → email id + received time → raw Excel file id.")


# ---------------------------------------------------------------------------
# Sidebar status, rendered last so it reflects buttons clicked during this run
# ---------------------------------------------------------------------------
if ctl.is_mock:
    final = ctl.load_state()
    cd = date.fromisoformat(final.current_date) if final.current_date else None
    nd = ctl.next_day()
    with date_slot.container():
        st.metric("Simulated date", cd.strftime("%a %b %d, %Y") if cd else "—")
        st.caption(f"Next scripted day: {nd.strftime('%a %m/%d') if nd else 'none (storyline finished)'}")
    status_slot.caption(f"Delivered: {len(final.delivered)} days · Processed: {len(final.processed)} days")
