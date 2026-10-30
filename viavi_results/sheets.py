"""Read and write a Google Sheet (any number of tabs) using a service account."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

import gspread
from google.oauth2.service_account import Credentials

from .config import Settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Keep each batch request comfortably under API payload limits.
_BATCH_SIZE = 500


class SheetAccessError(RuntimeError):
    """Raised when the spreadsheet can't be opened, with a hint on how to fix it."""


class SheetWriter:
    """Batch reads and cell writes across the tabs of one spreadsheet."""

    def __init__(self, settings: Settings):
        creds = Credentials.from_service_account_info(
            settings.service_account_info, scopes=SCOPES
        )
        self._client = gspread.authorize(creds)
        email = settings.service_account_info.get("client_email", "the service account")
        try:
            self._spreadsheet = self._client.open_by_key(settings.sheet_id)
        except gspread.SpreadsheetNotFound:
            raise SheetAccessError(
                f"Spreadsheet {settings.sheet_id} not found, or not shared with {email}. "
                "Check the sheet ID and share the sheet with that address as an Editor."
            ) from None
        except gspread.exceptions.APIError as exc:
            message = str(exc)
            if "must not be an Office file" in message:
                raise SheetAccessError(
                    "This file is an Excel (.xlsx) file stored in Google Drive, which the "
                    "Sheets API can't edit. Open it, choose File > Save as Google Sheets, "
                    "and use the new file's ID (and share it with "
                    f"{email})."
                ) from None
            if exc.response.status_code == 403:
                raise SheetAccessError(
                    f"Permission denied. Share the sheet with {email} as an Editor, and make "
                    "sure the Google Sheets API is enabled for the service account's project."
                ) from None
            raise

    @property
    def spreadsheet(self) -> gspread.Spreadsheet:
        return self._spreadsheet

    def tab_titles(self) -> List[str]:
        return [ws.title for ws in self._spreadsheet.worksheets()]

    def read_tabs(self, titles: Sequence[str]) -> Dict[str, List[List[Any]]]:
        """Read every cell of the given tabs in one request.

        Values come back as displayed in the sheet (strings). Tabs that don't
        exist are left out of the result.
        """
        existing = set(self.tab_titles())
        wanted = [t for t in titles if t in existing]
        if not wanted:
            return {}
        resp = self._spreadsheet.values_batch_get([_quote(t) for t in wanted])
        out = {}
        for title, value_range in zip(wanted, resp.get("valueRanges", [])):
            out[title] = value_range.get("values", [])
        return out

    def write_cells(self, updates: Iterable[Any], value_input_option: str = "RAW") -> int:
        """Write single-cell updates (objects with tab, a1 and value). Returns cells written.

        RAW stores values exactly as given: numbers stay numbers and ISO
        timestamps stay text, matching what the sheet already holds.
        """
        data = [
            {"range": f"{_quote(u.tab)}!{u.a1}", "values": [[u.value]]}
            for u in updates
        ]
        for start in range(0, len(data), _BATCH_SIZE):
            self._spreadsheet.values_batch_update({
                "valueInputOption": value_input_option,
                "data": data[start:start + _BATCH_SIZE],
            })
        return len(data)


def _quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"
