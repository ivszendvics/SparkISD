"""Business-day calendar: Mon-Fri minus the City of Boston observed holidays (config/holidays.csv).

Strict mode: if any date we need falls in a year with NO holidays in the file, raise
HolidayYearMissingError naming the year(s). Lenient mode: the caller adds the dq flag
`holiday_year_missing` instead. A year is never silently treated as holiday-free.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from isd_pipeline.settings import DEFAULT_CONFIG_DIR


class HolidayYearMissingError(ValueError):
    def __init__(self, years: Iterable[int]):
        self.years = sorted(set(int(y) for y in years))
        ys = ", ".join(str(y) for y in self.years)
        super().__init__(
            f"No holidays configured for year(s) {ys}. Add the City of Boston observed holidays for {ys} "
            f"to isd_pipeline/config/holidays.csv (or run with --lenient to flag affected rows instead).")


def load_holidays(path: Path | str | None = None) -> list[date]:
    path = Path(path) if path else DEFAULT_CONFIG_DIR / "holidays.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        return sorted(date.fromisoformat(row["date"].strip()) for row in csv.DictReader(fh) if row.get("date"))


def _to_d64(values) -> np.ndarray:
    """Anything date-like (scalar, list, Series) -> numpy datetime64[D] array (NaT allowed)."""
    s = pd.to_datetime(pd.Series(values if not np.isscalar(values) and not isinstance(values, (date, pd.Timestamp))
                                 else [values]), errors="coerce")
    return s.values.astype("datetime64[D]")


@dataclass(frozen=True)
class BusinessCalendar:
    holidays: tuple[date, ...]

    @classmethod
    def from_csv(cls, path: Path | str | None = None) -> "BusinessCalendar":
        return cls(tuple(load_holidays(path)))

    @property
    def years_covered(self) -> frozenset[int]:
        return frozenset(h.year for h in self.holidays)

    @property
    def _np_holidays(self) -> np.ndarray:
        return np.array(self.holidays, dtype="datetime64[D]")

    def missing_years(self, dates) -> list[int]:
        arr = _to_d64(dates)
        arr = arr[~np.isnat(arr)]
        years = {int(str(d)[:4]) for d in np.unique(arr)}
        return sorted(years - self.years_covered)

    def check_years(self, dates, strict: bool = True) -> list[int]:
        """Raise (strict) or return the uncovered years among `dates`."""
        missing = self.missing_years(dates)
        if missing and strict:
            raise HolidayYearMissingError(missing)
        return missing

    def is_business_day(self, d) -> bool:
        return bool(np.is_busday(_to_d64(d)[0], holidays=self._np_holidays))

    def due_dates(self, call_dates, sla_days) -> np.ndarray:
        """numpy.busday_offset(call_date, sla, roll='forward'): a weekend/holiday call counts
        as received on the next business day. NaT in -> NaT out."""
        cd = _to_d64(call_dates)
        sla = np.broadcast_to(np.asarray(sla_days, dtype="int64"), cd.shape)
        out = np.full(cd.shape, np.datetime64("NaT"), dtype="datetime64[D]")
        ok = ~np.isnat(cd)
        if ok.any():
            out[ok] = np.busday_offset(cd[ok], sla[ok], roll="forward", holidays=self._np_holidays)
        return out

    def due_date(self, call_date, sla_days: int = 2) -> date:
        return pd.Timestamp(self.due_dates([call_date], [sla_days])[0]).date()

    def busdays_between(self, start, end) -> np.ndarray:
        """Business days in the half-open interval (start, end]. 0 when end <= start or NaT."""
        s = _to_d64(start)
        e = _to_d64(end)
        s, e = np.broadcast_arrays(s, e)
        out = np.zeros(s.shape, dtype="int64")
        ok = ~np.isnat(s) & ~np.isnat(e) & (e > s)
        if ok.any():
            one = np.timedelta64(1, "D")
            out[ok] = np.busday_count(s[ok] + one, e[ok] + one, holidays=self._np_holidays)
        return out

    def business_days_in(self, start, end) -> list[date]:
        """Business days strictly between start and end (exclusive both ends)."""
        days = pd.date_range(pd.Timestamp(start) + pd.Timedelta(days=1), pd.Timestamp(end) - pd.Timedelta(days=1))
        hol = set(self.holidays)
        return [d.date() for d in days if d.weekday() < 5 and d.date() not in hol]


def load_sla(path: Path | str | None = None) -> tuple[dict[str, int], int]:
    """Return ({insp_type (lower-case): business_days}, default)."""
    path = Path(path) if path else DEFAULT_CONFIG_DIR / "sla.csv"
    table: dict[str, int] = {}
    default = 2
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("insp_type") or "").strip().lower()
            if not key:
                continue
            if key == "default":
                default = int(row["business_days"])
            else:
                table[key] = int(row["business_days"])
    return table, default
