"""The sheets bridge: lets the Rust app use the Python Google Sheets code (sheets.py).

The app starts this as a child process and talks to it over stdin/stdout, one
JSON object per line in each direction. Each request gets exactly one response.

  {"op": "open", "service_account": {...key file...}, "sheet_id": "..."}
      -> {"ok": true, "title": "RMN2 PON Audit", "service_account_email": "..."}
  {"op": "read_tabs", "titles": ["Tab 1", ...]}
      -> {"ok": true, "tabs": {"Tab 1": [["A1", "B1"], ...]}}   (missing tabs left out)
  {"op": "write_cells", "updates": [{"tab": "...", "a1": "B7", "value": -18.5}, ...]}
      -> {"ok": true, "written": 2}
  {"op": "quit"}  (or end of input)
      -> {"ok": true}, then the process exits

A failed request returns {"ok": false, "kind": "access" | "error", "error": "..."};
"access" means the sheet couldn't be opened (the message says how to fix it).

The key travels over stdin, never on the command line, so other programs on the
computer can't see it.
"""

from __future__ import annotations

import io
import json
import sys
import traceback
from typing import Any, Callable, Dict, Optional, TextIO

PROTOCOL_VERSION = 1


class BridgeError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


def _default_opener(service_account: Dict[str, Any], sheet_id: str):
    from .sheets import SheetWriter  # imported here so `--version` and tests don't need gspread

    return SheetWriter(service_account, sheet_id)


class Bridge:
    """Handles requests for one spreadsheet. `opener` builds the SheetWriter (replaceable in tests)."""

    def __init__(self, opener: Callable[[Dict[str, Any], str], Any] = _default_opener):
        self._opener = opener
        self._writer = None

    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        op = request.get("op")
        if op == "hello":
            return {"ok": True, "protocol": PROTOCOL_VERSION}
        if op == "open":
            return self._open(request)
        if op == "read_tabs":
            tabs = self._require_writer().read_tabs(list(request.get("titles") or []))
            return {"ok": True, "tabs": {t: [[_cell(v) for v in row] for row in rows] for t, rows in tabs.items()}}
        if op == "write_cells":
            updates = list(request.get("updates") or [])
            for u in updates:
                if not isinstance(u, dict) or not {"tab", "a1", "value"} <= u.keys():
                    raise BridgeError(f"Bad cell update: {u!r}")
            written = self._require_writer().write_cells(updates, request.get("value_input_option") or "RAW")
            return {"ok": True, "written": written}
        if op == "quit":
            return {"ok": True}
        raise BridgeError(f"Unknown request {op!r}")

    def _open(self, request: Dict[str, Any]) -> Dict[str, Any]:
        info = request.get("service_account")
        sheet_id = request.get("sheet_id")
        if not isinstance(info, dict) or not isinstance(sheet_id, str) or not sheet_id:
            raise BridgeError("open needs service_account (the key as JSON) and sheet_id")
        try:
            self._writer = self._opener(info, sheet_id)
        except Exception as exc:
            if type(exc).__name__ == "SheetAccessError":
                raise BridgeError(str(exc), "access") from None
            raise
        return {
            "ok": True,
            "title": self._writer.spreadsheet.title,
            "service_account_email": info.get("client_email", ""),
        }

    def _require_writer(self):
        if self._writer is None:
            raise BridgeError("No spreadsheet is open (send an open request first)")
        return self._writer


def _cell(value: Any) -> str:
    # Values are read as displayed (strings); make sure of it so the app sees one type.
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, BridgeError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def serve(stdin: TextIO, stdout: TextIO, bridge: Optional[Bridge] = None) -> int:
    bridge = bridge or Bridge()
    for line in stdin:
        if not line.strip():
            continue
        request: Dict[str, Any] = {}
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise BridgeError("Each request must be a JSON object")
            request = parsed
            response = bridge.handle(request)
        except Exception as exc:  # reported to the app, which shows it to the user
            traceback.print_exc(file=sys.stderr)
            response = {"ok": False, "kind": getattr(exc, "kind", "error"), "error": _error_text(exc)}
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()
        if request.get("op") == "quit":
            break
    return 0


def main() -> int:
    if "--version" in sys.argv[1:]:
        print(f"sheets-bridge protocol {PROTOCOL_VERSION}")
        return 0
    return main_with(Bridge())


def main_with(bridge: Bridge) -> int:
    # The responses get the real stdout to themselves; anything a library prints goes to stderr.
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    inp = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    sys.stdout = sys.stderr
    return serve(inp, out, bridge)


if __name__ == "__main__":
    sys.exit(main())
