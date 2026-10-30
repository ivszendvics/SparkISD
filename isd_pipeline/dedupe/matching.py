"""Match-key construction for duplicate detection (Module 2, section 5.1).

The key is the exact equality of five normalized fields:
    call_datetime (full timestamp) | ap_number | address | insp_type | work group
Work group = the `work_group` column when the report has one (non-blank), otherwise the
division (the current synthetic and - as far as we know - real reports have no Work Group
column, so the division is used).

Inspector, notes and report_date are NEVER part of the key.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

import pandas as pd

KEY_FIELDS: tuple[str, ...] = ("call_datetime", "ap_number", "address", "insp_type", "work_group")
_WS = re.compile(r"\s+")
_ADDR_PUNCT = re.compile(r"[.,#|]")


def normalize_text(value: Any) -> str:
    """Trim, collapse internal whitespace, upper-case (case-insensitive compare)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return _WS.sub(" ", str(value)).strip().upper()


def normalize_address(value: Any, abbreviations: Mapping[str, str]) -> str:
    """normalize_text + drop . , # and map long street words to their abbreviation."""
    text = _ADDR_PUNCT.sub(" ", normalize_text(value))
    words = [abbreviations.get(w, w) for w in text.split()]
    return " ".join(words)


def format_call_datetime(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%dT%H:%M:%S")


def work_group_of(row: Mapping[str, Any]) -> str:
    wg = normalize_text(row.get("work_group", ""))
    return wg if wg else normalize_text(row.get("division", ""))


def missing_key_fields(row: Mapping[str, Any]) -> list[str]:
    missing = []
    if not format_call_datetime(row.get("call_datetime")):
        missing.append("call_datetime")
    for f in ("ap_number", "address", "insp_type"):
        if not normalize_text(row.get(f, "")):
            missing.append(f)
    return missing


def key_parts(row: Mapping[str, Any], abbreviations: Mapping[str, str]) -> dict[str, str]:
    return {
        "call_datetime": format_call_datetime(row.get("call_datetime")),
        "ap_number": normalize_text(row.get("ap_number", "")),
        "address": normalize_address(row.get("address", ""), abbreviations),
        "insp_type": normalize_text(row.get("insp_type", "")),
        "work_group": work_group_of(row),
    }


def build_match_key(row: Mapping[str, Any], abbreviations: Mapping[str, str]) -> str:
    p = key_parts(row, abbreviations)
    return "|".join(p[f] for f in KEY_FIELDS)


def partial_key(row: Mapping[str, Any], abbreviations: Mapping[str, str]) -> str:
    """The key without the address - used only to DETECT conflicts, never to merge."""
    p = key_parts(row, abbreviations)
    return "|".join(p[f] for f in KEY_FIELDS if f != "address")


def unmatched_key(row: Mapping[str, Any]) -> str:
    """Rows with a missing key field are never merged with anything. Their key is tied to
    the exact source row, so reprocessing the same file still maps to the same ID."""
    rd = row.get("report_date")
    rd_txt = pd.Timestamp(rd).strftime("%Y-%m-%d") if rd is not None and not pd.isna(rd) else ""
    return "UNMATCHED|" + "|".join([
        rd_txt, str(row.get("division", "")), str(row.get("source_email_id", "")),
        str(row.get("source_attachment_id", "")), str(row.get("source_row_number", "")),
    ])
