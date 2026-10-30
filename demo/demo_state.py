"""Demo controller shared by the Streamlit app and the terminal fallback.

It owns the simulated calendar: which scripted day has been "delivered" into the mock inbox
and which days the pipeline has processed. State lives in demo_data/demo_state.json so the
app survives a browser refresh.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEMO_DIR = Path(__file__).resolve().parent
if str(DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(DEMO_DIR))

from generate_synthetic import deliver, generate  # noqa: E402

from isd_pipeline.factory import make_email_source, make_storage  # noqa: E402
from isd_pipeline.pipeline import RunResult, StepEvent, run_daily_pipeline  # noqa: E402
from isd_pipeline.settings import Settings, load_settings  # noqa: E402

DEMO_DATA = ROOT / "demo_data"
SCRIPTED = DEMO_DATA / "scripted_inbox"
STATE_FILE = DEMO_DATA / "demo_state.json"


@dataclass
class DemoState:
    delivered: list[str] = field(default_factory=list)
    processed: list[str] = field(default_factory=list)
    current_date: str | None = None
    last_batch_id: str | None = None


class DemoController:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.scripted = SCRIPTED

    # ---------------------------------------------------------------- state
    @property
    def is_mock(self) -> bool:
        return self.settings.is_mock

    def ensure_generated(self) -> None:
        if not (self.scripted / "storyline.json").exists():
            generate(self.scripted)

    @property
    def storyline(self) -> dict:
        self.ensure_generated()
        return json.loads((self.scripted / "storyline.json").read_text())

    @property
    def report_days(self) -> list[date]:
        return [date.fromisoformat(d) for d in self.storyline["report_days"]]

    @property
    def demo_day(self) -> date:
        return date.fromisoformat(self.storyline["demo_day"])

    def load_state(self) -> DemoState:
        if STATE_FILE.exists():
            return DemoState(**json.loads(STATE_FILE.read_text()))
        return DemoState()

    def save_state(self, st: DemoState) -> None:
        DEMO_DATA.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st.__dict__, indent=2))

    def storage(self):
        return make_storage(self.settings)

    def email_source(self):
        return make_email_source(self.settings)

    # ---------------------------------------------------------------- actions
    def reset(self) -> None:
        """Wipe the mock inbox and mock Drive, regenerate the scripted storyline."""
        if not self.is_mock:
            raise RuntimeError("Reset only works in MOCK mode (it deletes the mock Drive folder).")
        for p in (self.settings.mock_inbox_dir, self.settings.mock_drive_dir):
            if p.exists():
                shutil.rmtree(p)
        generate(self.scripted)
        self.settings.mock_inbox_dir.mkdir(parents=True, exist_ok=True)
        self.save_state(DemoState())

    def next_day(self) -> date | None:
        st = self.load_state()
        for d in self.report_days:
            if d.isoformat() not in st.delivered:
                return d
        return None

    def deliver_day(self, day: date) -> None:
        deliver(self.scripted, self.settings.mock_inbox_dir, day)
        st = self.load_state()
        if day.isoformat() not in st.delivered:
            st.delivered.append(day.isoformat())
        st.current_date = day.isoformat()
        self.save_state(st)

    def deliver_next_day(self) -> date | None:
        day = self.next_day()
        if day is not None:
            self.deliver_day(day)
        return day

    def run(self, day: date | None = None, on_step: Callable[[StepEvent], None] | None = None,
            trigger: str = "demo") -> RunResult:
        st = self.load_state()
        if day is None:
            if not st.current_date:
                raise RuntimeError("No day delivered yet - click 'Deliver next day's emails' first.")
            day = date.fromisoformat(st.current_date)
        res = run_daily_pipeline(day, settings=self.settings, storage=self.storage(), trigger=trigger,
                                 on_step=on_step)
        st = self.load_state()
        if day.isoformat() not in st.processed:
            st.processed.append(day.isoformat())
        st.last_batch_id = res.batch_id
        self.save_state(st)
        return res

    def preload_history(self, on_day: Callable[[date, RunResult], None] | None = None) -> list[RunResult]:
        """Silently deliver + process every scripted day before the demo day."""
        results = []
        for d in self.report_days:
            if d >= self.demo_day:
                break
            if d.isoformat() in self.load_state().processed:
                continue
            self.deliver_day(d)
            r = self.run(d, trigger="demo-preload")
            results.append(r)
            if on_day:
                on_day(d, r)
        return results
