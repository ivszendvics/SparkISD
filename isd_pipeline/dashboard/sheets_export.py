"""OPTIONAL stub: push dashboard exports to Google Sheets.

Not required - the Power BI dashboard reads the CSVs in dashboard_exports/. If the client
later wants Sheets, implement `push()` with the Sheets API (scope
https://www.googleapis.com/auth/drive.file is enough for spreadsheets the app creates) and
call it after `write_exports()` in pipeline.py.
"""
from __future__ import annotations

import pandas as pd


class GoogleSheetsExportAdapter:
    def __init__(self, service=None, spreadsheet_title: str = "ISD Permit Inspection KPIs"):
        self.service = service
        self.spreadsheet_title = spreadsheet_title

    def push(self, exports: dict[str, pd.DataFrame]) -> str:
        """Write each export to its own tab; return the spreadsheet id."""
        raise NotImplementedError("Google Sheets export is an optional extension; see module docstring.")
