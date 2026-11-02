"""Tests for the bridge protocol, with a fake SheetWriter (no Google access needed).

    python -m unittest discover -s sheets_bridge/tests -t .
"""

import io
import json
import types
import unittest

from sheets_bridge.__main__ import Bridge, serve


class SheetAccessError(RuntimeError):
    """Same name as sheets.SheetAccessError, which the bridge reports as kind "access"."""


class FakeWriter:
    def __init__(self, info, sheet_id):
        if sheet_id == "unshared":
            raise SheetAccessError(f"Spreadsheet {sheet_id} not found, or not shared with {info['client_email']}.")
        self.spreadsheet = types.SimpleNamespace(title="RMN2 PON Audit")
        self.tabs = {"Tab A": [["DH", "Row"], ["A", 12]], "Tab B": []}
        self.written = []

    def read_tabs(self, titles):
        return {t: self.tabs[t] for t in titles if t in self.tabs}

    def write_cells(self, updates, value_input_option="RAW"):
        self.written.append((updates, value_input_option))
        return len(updates)


KEY = {"type": "service_account", "client_email": "bot@example.iam.gserviceaccount.com", "private_key": "x"}


def run(*requests):
    bridge = Bridge(opener=FakeWriter)
    out = io.StringIO()
    serve(io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), out, bridge)
    return [json.loads(line) for line in out.getvalue().splitlines()], bridge


class BridgeTest(unittest.TestCase):
    def test_open_read_write(self):
        responses, bridge = run(
            {"op": "hello"},
            {"op": "open", "service_account": KEY, "sheet_id": "abc"},
            {"op": "read_tabs", "titles": ["Tab A", "Missing"]},
            {"op": "write_cells", "updates": [{"tab": "Tab A", "a1": "B2", "value": -18.5}]},
            {"op": "quit"},
            {"op": "hello"},  # after quit: not answered
        )
        self.assertEqual(len(responses), 5)
        self.assertEqual(responses[0], {"ok": True, "protocol": 1})
        self.assertEqual(responses[1]["title"], "RMN2 PON Audit")
        self.assertEqual(responses[1]["service_account_email"], KEY["client_email"])
        # Non-string cells come back as text, and missing tabs are left out.
        self.assertEqual(responses[2]["tabs"], {"Tab A": [["DH", "Row"], ["A", "12"]]})
        self.assertEqual(responses[3], {"ok": True, "written": 1})
        self.assertEqual(bridge._writer.written[0][1], "RAW")

    def test_access_error(self):
        responses, _ = run({"op": "open", "service_account": KEY, "sheet_id": "unshared"})
        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["kind"], "access")
        self.assertIn("not shared with bot@example", responses[0]["error"])

    def test_errors_keep_the_bridge_running(self):
        responses, _ = run(
            {"op": "read_tabs", "titles": ["Tab A"]},
            "not an object",
            {"op": "open", "service_account": KEY, "sheet_id": "abc"},
            {"op": "write_cells", "updates": [{"tab": "Tab A"}]},
            {"op": "nonsense"},
        )
        self.assertEqual([r["ok"] for r in responses], [False, False, True, False, False])
        self.assertIn("No spreadsheet is open", responses[0]["error"])
        self.assertIn("Bad cell update", responses[3]["error"])
        self.assertIn("Unknown request", responses[4]["error"])

    def test_bad_json_line(self):
        out = io.StringIO()
        serve(io.StringIO("{oops\n"), out, Bridge(opener=FakeWriter))
        response = json.loads(out.getvalue())
        self.assertFalse(response["ok"])
        self.assertIn("JSONDecodeError", response["error"])


class SheetsModuleTest(unittest.TestCase):
    def test_write_cells_builds_quoted_ranges(self):
        try:
            from sheets_bridge import sheets
        except ImportError as exc:  # gspread not installed
            self.skipTest(str(exc))
        calls = []
        writer = object.__new__(sheets.SheetWriter)
        writer._spreadsheet = types.SimpleNamespace(values_batch_update=calls.append)
        n = writer.write_cells([{"tab": "RMN2 F1 & F2", "a1": "C5", "value": "2026-10-29T08:00:00-04:00"},
                                {"tab": "It's", "a1": "A1", "value": ""}])
        self.assertEqual(n, 2)
        self.assertEqual(calls[0]["valueInputOption"], "RAW")
        self.assertEqual([d["range"] for d in calls[0]["data"]], ["'RMN2 F1 & F2'!C5", "'It''s'!A1"])


if __name__ == "__main__":
    unittest.main()
