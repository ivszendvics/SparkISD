"""`is_meaningful_note()` - shared by Module 2 (first_note_date) and Module 3 (status rules)."""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from isd_pipeline.settings import DEFAULT_CONFIG_DIR


@lru_cache(maxsize=8)
def _load_placeholders(path: str) -> frozenset[str]:
    with open(path, newline="", encoding="utf-8") as fh:
        return frozenset(row["placeholder"].strip().lower() for row in csv.DictReader(fh) if row.get("placeholder"))


def load_note_placeholders(config_dir: Path = DEFAULT_CONFIG_DIR) -> frozenset[str]:
    return _load_placeholders(str(Path(config_dir) / "note_placeholders.csv"))


def is_meaningful_note(text: object, placeholders: Iterable[str] | None = None) -> bool:
    """A note counts only if it is non-empty after stripping whitespace AND is not a
    placeholder (n/a, na, -, ., none, tbd - compared case-insensitively)."""
    if text is None:
        return False
    stripped = str(text).strip()
    if not stripped:
        return False
    ph = frozenset(p.lower() for p in placeholders) if placeholders is not None else load_note_placeholders()
    return stripped.lower() not in ph
