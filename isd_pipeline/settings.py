"""Typed settings loaded from isd_pipeline/config/*.yaml.

Everything configurable lives in the YAML/CSV files; this module only turns them into
dataclasses so the rest of the code gets autocomplete and type checking.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DEFAULT_CONFIG_DIR = PACKAGE_DIR / "config"

DIVISIONS: tuple[str, ...] = ("Building", "Electric", "Plumbing/Gas")
DIVISION_SLUGS: dict[str, str] = {
    "Building": "building",
    "Electric": "electric",
    "Plumbing/Gas": "plumbing_gas",
}


def division_slug(division: str) -> str:
    """'Plumbing/Gas' -> 'plumbing_gas' (used in file and folder names)."""
    return DIVISION_SLUGS[division]


@dataclass(frozen=True)
class EmailRules:
    sender_display_name: str
    sender_address: str | None
    subjects: dict[str, str]  # exact subject -> division
    has_attachment: bool = True
    allowed_extensions: tuple[str, ...] = (".xlsx", ".xlsm")
    label: str | None = None
    search_padding_days: int = 1

    def division_for_subject(self, subject: str) -> str | None:
        return self.subjects.get((subject or "").strip())

    def subject_for_division(self, division: str) -> str:
        for subj, div in self.subjects.items():
            if div == division:
                return subj
        raise KeyError(f"No subject configured for division {division!r}")


@dataclass(frozen=True)
class Settings:
    mode: str
    timezone: str
    root_folder_name: str
    mock_drive_dir: Path
    mock_inbox_dir: Path
    credentials_file: Path
    token_file: Path
    run_time: str
    header_scan_rows: int
    min_header_matches: int
    strict_holidays: bool
    long_open_business_days: int
    low_n_threshold: int
    include_contact_fields: bool
    retry_attempts: int
    retry_base_delay: float
    address_abbreviations: dict[str, str]
    email_rules: EmailRules
    report_schema: dict[str, Any] = field(repr=False)
    config_dir: Path = DEFAULT_CONFIG_DIR

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"

    def with_overrides(self, **kwargs: Any) -> "Settings":
        return replace(self, **kwargs)


def _resolve(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_email_rules(config_dir: Path = DEFAULT_CONFIG_DIR) -> EmailRules:
    raw = _read_yaml(config_dir / "email_rules.yaml")
    sender = raw.get("sender", {}) or {}
    return EmailRules(
        sender_display_name=str(sender.get("display_name") or "Microsoft Power BI"),
        sender_address=(sender.get("address") or None),
        subjects={str(k).strip(): str(v) for k, v in (raw.get("subjects") or {}).items()},
        has_attachment=bool(raw.get("has_attachment", True)),
        allowed_extensions=tuple(x.lower() for x in raw.get("allowed_extensions", [".xlsx", ".xlsm"])),
        label=raw.get("label") or None,
        search_padding_days=int(raw.get("search_padding_days", 1)),
    )


def load_settings(config_dir: Path | str | None = None, **overrides: Any) -> Settings:
    """Load settings from YAML. Keyword overrides replace fields (e.g. mode='live')."""
    cdir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    raw = _read_yaml(cdir / "settings.yaml")
    g = lambda section, key, default: (raw.get(section) or {}).get(key, default)  # noqa: E731
    settings = Settings(
        mode=str(raw.get("mode", "mock")).lower(),
        timezone=str(raw.get("timezone", "America/New_York")),
        root_folder_name=str(g("storage", "root_folder_name", "Permit Inspection Automation")),
        mock_drive_dir=_resolve(g("storage", "mock_drive_dir", "demo_data/mock_drive")),
        mock_inbox_dir=_resolve(g("email", "mock_inbox_dir", "demo_data/mock_inbox")),
        credentials_file=_resolve(g("live", "credentials_file", "secrets/credentials.json")),
        token_file=_resolve(g("live", "token_file", "secrets/token.json")),
        run_time=str(g("schedule", "run_time", "07:45")),
        header_scan_rows=int(g("parser", "header_scan_rows", 30)),
        min_header_matches=int(g("parser", "min_header_matches", 4)),
        strict_holidays=bool(g("late_logic", "strict_holidays", True)),
        long_open_business_days=int(g("late_logic", "long_open_business_days", 30)),
        low_n_threshold=int(g("aggregation", "low_n_threshold", 10)),
        include_contact_fields=bool(g("exports", "include_contact_fields", False)),
        retry_attempts=int(g("retry", "max_attempts", 4)),
        retry_base_delay=float(g("retry", "base_delay_seconds", 1.0)),
        address_abbreviations={
            str(k).upper(): str(v).upper()
            for k, v in (g("dedupe", "address_abbreviations", {}) or {}).items()
        },
        email_rules=load_email_rules(cdir),
        report_schema=_read_yaml(cdir / "report_schema.yaml"),
        config_dir=cdir,
    )
    for key in ("mock_drive_dir", "mock_inbox_dir", "credentials_file", "token_file"):
        if key in overrides and overrides[key] is not None:
            overrides[key] = _resolve(overrides[key])
    if "mode" in overrides and overrides["mode"]:
        overrides["mode"] = str(overrides["mode"]).lower()
    overrides = {k: v for k, v in overrides.items() if v is not None}
    settings = replace(settings, **overrides)
    if settings.mode not in ("mock", "live"):
        raise ValueError(f"settings.mode must be 'mock' or 'live', got {settings.mode!r}")
    return settings


def division_schema(report_schema: dict[str, Any], division: str) -> dict[str, Any]:
    """Merge the per-division overrides into the base report schema."""
    merged = copy.deepcopy(report_schema)
    override = (report_schema.get("division_overrides") or {}).get(division) or {}
    for key, value in override.items():
        if key == "aliases":
            for canon, extra in (value or {}).items():
                merged.setdefault("aliases", {}).setdefault(canon, [])
                merged["aliases"][canon] = list(merged["aliases"][canon]) + list(extra)
        else:
            merged[key] = value
    merged.pop("division_overrides", None)
    return merged
