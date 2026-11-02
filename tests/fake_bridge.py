#!/usr/bin/env python3
"""The real sheets bridge protocol, with gspread replaced by an in-memory sheet.

Used by the Rust tests (src/sheets.rs) to check the app and the bridge agree.
"""

import json
import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sheets_bridge.__main__ import Bridge, main_with  # noqa: E402


class SheetAccessError(RuntimeError):
    pass


class FakeWriter:
    def __init__(self, info, sheet_id):
        if sheet_id == "unshared":
            raise SheetAccessError(f"Spreadsheet {sheet_id} not found, or not shared with {info['client_email']}.")
        if sheet_id == "crash":
            sys.stderr.write("Traceback (most recent call last):\nRuntimeError: simulated crash\n")
            sys.stderr.flush()
            os._exit(3)
        self.spreadsheet = types.SimpleNamespace(title="Fake PON Audit")
        # FAKE_SHEET names a JSON file holding the tabs; writes are saved back to it.
        self.path = os.environ.get("FAKE_SHEET")
        if self.path:
            with open(self.path, encoding="utf-8") as fh:
                self.tabs = json.load(fh)
        else:
            self.tabs = {"RMN2.DHA PON Audit": [["DH", "Row", "Rack"], ["A", 12, 25]]}

    def read_tabs(self, titles):
        return {t: self.tabs[t] for t in titles if t in self.tabs}

    def write_cells(self, updates, value_input_option="RAW"):
        if not self.path:
            self.tabs["written"] = [[f"'{u['tab']}'!{u['a1']}", u["value"]] for u in updates]
            return len(updates)
        for u in updates:
            letters, row = re.fullmatch(r"([A-Z]+)(\d+)", u["a1"]).groups()
            col = 0
            for ch in letters:
                col = col * 26 + ord(ch) - 64
            rows = self.tabs[u["tab"]]
            while len(rows) < int(row):
                rows.append([])
            cells = rows[int(row) - 1]
            while len(cells) < col:
                cells.append("")
            cells[col - 1] = u["value"] if isinstance(u["value"], str) else repr(u["value"])
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.tabs, fh)
        return len(updates)


if __name__ == "__main__":
    sys.exit(main_with(Bridge(opener=FakeWriter)))
