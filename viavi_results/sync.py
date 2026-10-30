"""Download new TruePON results from StrataSync and write them to the PON Audit sheet.

Each run picks up where the last one left off (the download position is kept
in a state file per StrataSync region, client ID and sheet). The first run,
with no saved state, starts from midnight today (local time).

Results are read straight from the StrataSync search response, which carries
the same values as the full test files, so no per-file downloads are needed.
Full files are only downloaded for tests with no power reading, to get the
signal status for the flag message.

Nothing is written, and the saved position doesn't move, unless `apply` is set.
"""

from __future__ import annotations

import csv
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Callable, List, Optional

from .config import Settings, StrataSyncSettings, app_data_dir
from .pon_audit import AUDIT_TABS, UpdatePlan, build_indexes, plan_updates, reading_from_search, readings_from_result
from .sheets import SheetWriter
from .stratasync import SYNC_TIME, StrataSyncClient, StrataSyncError
from .sync_state import SyncState

TEST_TYPE = "truepon"

Log = Callable[[str], None]


@dataclass
class SyncResult:
    start: datetime
    end: datetime
    applied: bool
    sheet_title: str = ""
    found: int = 0
    truepon: int = 0
    new: int = 0
    plan: UpdatePlan = field(default_factory=UpdatePlan)
    missing_tabs: List[str] = field(default_factory=list)
    download_failures: List[str] = field(default_factory=list)
    cells_written: int = 0
    position_saved: Optional[datetime] = None

    def skipped_summary(self) -> List[tuple]:
        """(reason, count) for every reading that wasn't written, most common first."""
        return Counter(reason for _, reason in self.plan.skipped).most_common()


def state_file(ss: StrataSyncSettings, settings: Settings) -> Path:
    """One download position per StrataSync server, client ID and sheet, so
    switching any of them doesn't skip results the other combination hasn't seen."""
    key = f"{ss.base_url}|{ss.client_id}|{settings.sheet_id}".encode()
    return app_data_dir() / "state" / f"last_download-{hashlib.sha256(key).hexdigest()[:12]}.json"


def last_position(ss: StrataSyncSettings, settings: Settings) -> SyncState:
    return SyncState.load(state_file(ss, settings))


def run_sync(
    ss: StrataSyncSettings,
    settings: Settings,
    *,
    apply: bool,
    since: Optional[datetime] = None,
    log: Log = print,
) -> SyncResult:
    """Search StrataSync, plan the sheet updates and (if `apply`) write them.

    Raises ConfigError, StrataSyncError or SheetAccessError on failure.
    """
    path = state_file(ss, settings)
    state = SyncState.load(path)
    run_at = datetime.now(timezone.utc)
    if since:
        start = since
    elif state.last_sync_time:
        start = state.last_sync_time
    else:
        start = datetime.combine(datetime.now().astimezone().date(), time.min).astimezone()
    result = SyncResult(start=start, end=run_at, applied=apply)

    log(f"Searching StrataSync ({ss.base_url}) for results received "
        f"{start.astimezone().strftime('%Y-%m-%d %H:%M:%S')} -> now")
    client = StrataSyncClient(ss)
    found = client.search_between(start, run_at, by=SYNC_TIME)
    records = [r for r in found if r.get("type") == TEST_TYPE]
    new = [r for r in records if since or not state.is_already_processed(r)]
    result.found, result.truepon, result.new = len(found), len(records), len(new)
    log(f"Found {len(found)} result(s): {len(records)} {TEST_TYPE}, {len(new)} new")

    readings = []
    for rec in sorted(new, key=lambda r: r.get("syncTime", "")):
        if rec.get("power") not in (None, ""):
            readings.append(reading_from_search(rec))
            continue
        log(f"Downloading {rec.get('name')} (no power reading in search result)")
        try:
            doc = client.download_json(rec["_id"])
        except (StrataSyncError, ValueError) as exc:
            result.download_failures.append(f"{rec.get('name')} ({rec['_id']}): {exc}")
            log(f"  could not download {rec.get('name')} ({rec['_id']}): {exc}")
            continue
        readings.extend(readings_from_result(doc, source=f"{rec.get('name')} (id {rec['_id']})"))

    log("Opening the Google Sheet")
    writer = SheetWriter(settings)
    result.sheet_title = writer.spreadsheet.title
    tabs = writer.read_tabs(AUDIT_TABS)
    result.missing_tabs = [t for t in AUDIT_TABS if t not in tabs]
    if result.missing_tabs:
        log(f"Warning: tabs not found: {', '.join(result.missing_tabs)}")
    result.plan = plan_updates(readings, build_indexes(tabs))
    log(f"Planned {len(result.plan.written)} result(s) to write, {len(result.plan.flags)} flag(s)")

    if not apply:
        log("Preview only: nothing was written and the download position was not moved.")
        return result

    result.cells_written = writer.write_cells(result.plan.updates)
    log(f"Wrote {result.cells_written} cell(s) to '{result.sheet_title}'")
    if result.download_failures:
        log("Some downloads failed, so the download position was NOT moved; "
            "the next run will retry them.")
        return result
    state.advance(records, run_at)
    state.save(path)
    result.position_saved = state.last_sync_time
    if state.last_sync_time:
        log(f"Saved download position: {state.last_sync_time.astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    return result


FLAG_CSV_HEADER = ["kind", "label", "message", "test_time", "tech", "serial_no", "source"]


def write_flags_csv(plan: UpdatePlan, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(FLAG_CSV_HEADER)
        for f in plan.flags:
            r = f.reading
            w.writerow([f.kind, r and r.label, f.message, r and r.test_time,
                        r and r.tech_name, r and r.serial_no, r and r.source])
