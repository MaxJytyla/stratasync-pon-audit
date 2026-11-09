"""Read and write a Google Sheet (any number of tabs) using a service account."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Sequence

import google.auth.exceptions
import gspread
import requests
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import diagnostics, history
from .config import Settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Keep each batch request comfortably under API payload limits.
_BATCH_SIZE = 500

# Seconds to connect, and to wait for a reply (reading whole tabs can take a while).
_TIMEOUT = (15, 120)


class _RecordingRetry(Retry):
    """Notes each dropped connection, so ones a retry recovers from are still on record."""

    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        if error is not None:
            history.record_connection_drop(getattr(_pool, "host", ""), error, self.total)
        return super().increment(method, url, response, error, _pool, _stacktrace)


# A dropped connection is tried 3 more times, after 0, 4 and 8 seconds. POST is
# safe to repeat: it's only used to write fixed values to fixed cells.
_RETRY = _RecordingRetry(total=3, backoff_factor=2, allowed_methods=frozenset({"GET", "POST"}))

_CONNECTION_ERRORS = (
    requests.exceptions.ConnectionError,  # includes SSLError
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,  # cut off partway through a reply
    google.auth.exceptions.TransportError,  # couldn't reach Google to sign in
)


class SheetAccessError(RuntimeError):
    """Raised when the spreadsheet can't be used, with a hint on how to fix it."""


@contextmanager
def _explain_connection_errors() -> Iterator[None]:
    """Turn a failure to reach Google into a message a user can act on."""
    try:
        yield
    except _CONNECTION_ERRORS as exc:
        report = diagnostics.capture(exc)
        saved = f"\n\nA report on this was saved to send to support: {report}" if report else ""
        raise SheetAccessError(
            "Couldn't connect to Google Sheets from this computer, even after several tries. "
            "This is usually caused by the network, a VPN or proxy, or security software "
            "blocking the connection. Try again, or try a different network. If it keeps "
            "happening, ask IT to allow sheets.googleapis.com and oauth2.googleapis.com."
            f"{saved}\n\nTechnical details: {type(exc).__name__}: {exc}"
        ) from exc


class SheetWriter:
    """Batch reads and cell writes across the tabs of one spreadsheet."""

    def __init__(self, settings: Settings):
        creds = Credentials.from_service_account_info(
            settings.service_account_info, scopes=SCOPES
        )
        self._client = gspread.authorize(creds)
        self._client.http_client.session.mount("https://", HTTPAdapter(max_retries=_RETRY))
        self._client.set_timeout(_TIMEOUT)
        email = settings.service_account_info.get("client_email", "the service account")
        try:
            with _explain_connection_errors():
                self._spreadsheet = self._client.open_by_key(settings.sheet_id)
        except gspread.SpreadsheetNotFound:
            raise SheetAccessError(
                f"Spreadsheet {settings.sheet_id} not found, or not shared with {email}. "
                "Check the sheet ID and share the sheet with that address as an Editor."
            ) from None
        except (gspread.exceptions.APIError, PermissionError) as exc:
            # gspread reports a 403 as a bare PermissionError, raised from the APIError.
            message = str(exc.__cause__ or exc)
            if "must not be an Office file" in message:
                raise SheetAccessError(
                    "This file is an Excel (.xlsx) file stored in Google Drive, which the "
                    "Sheets API can't edit. Open it, choose File > Save as Google Sheets, "
                    "and use the new file's ID (and share it with "
                    f"{email})."
                ) from None
            if isinstance(exc, PermissionError) or exc.response.status_code == 403:
                raise SheetAccessError(
                    f"Permission denied. Share the sheet with {email} as an Editor, and make "
                    "sure the Google Sheets API is enabled for the service account's project."
                ) from None
            raise

    @property
    def spreadsheet(self) -> gspread.Spreadsheet:
        return self._spreadsheet

    @_explain_connection_errors()
    def tab_titles(self) -> List[str]:
        return [ws.title for ws in self._spreadsheet.worksheets()]

    @_explain_connection_errors()
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

    @_explain_connection_errors()
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
