"""The persistent ID registry: match_key -> inspection_request_id (ISD-000001, ...)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from isd_pipeline.schema import ID_REGISTRY_COLUMNS, coerce

ID_PREFIX = "ISD-"
_ID_RE = re.compile(r"^ISD-(\d+)$")


def format_id(n: int) -> str:
    return f"{ID_PREFIX}{n:06d}"


@dataclass
class IdRegistry:
    by_key: dict[str, str] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    next_number: int = 1
    created_this_run: list[str] = field(default_factory=list)

    @classmethod
    def from_frame(cls, df: pd.DataFrame | None) -> "IdRegistry":
        reg = cls()
        if df is None or df.empty:
            return reg
        df = coerce(df, ID_REGISTRY_COLUMNS)
        for rec in df.to_dict("records"):
            reg.by_key[rec["match_key"]] = rec["inspection_request_id"]
            reg.rows.append(rec)
            m = _ID_RE.match(rec["inspection_request_id"])
            if m:
                reg.next_number = max(reg.next_number, int(m.group(1)) + 1)
        return reg

    def get(self, key: str) -> str | None:
        return self.by_key.get(key)

    def assign(self, key: str, batch_id: str, report_date: date | pd.Timestamp) -> str:
        """Return the existing ID for key, or allocate the next sequential one."""
        if key in self.by_key:
            return self.by_key[key]
        new_id = format_id(self.next_number)
        self.next_number += 1
        self.by_key[key] = new_id
        self.rows.append({"match_key": key, "inspection_request_id": new_id, "created_batch_id": batch_id,
                          "first_seen_report_date": pd.Timestamp(report_date)})
        self.created_this_run.append(new_id)
        return new_id

    def to_frame(self) -> pd.DataFrame:
        return coerce(pd.DataFrame.from_records(self.rows), ID_REGISTRY_COLUMNS)
