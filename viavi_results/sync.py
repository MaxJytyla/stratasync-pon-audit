"""Download new TruePON results from StrataSync and write them to the PON Audit sheet.

Each run picks up where the last one left off (the download position is kept
in a state file per StrataSync region, client ID and sheet). The first run,
with no saved state, starts from midnight today (local time).

Results are read straight from the StrataSync search response, which carries
the same values as the full test files, so no per-file downloads are needed.
Full files are only downloaded for tests with no power reading, to get the
signal status for the flag message.

Nothing is written, and the saved position doesn't move, unless `apply` is set.
Every applied run is recorded in the update history (see history.py).

Only one run at a time, per computer: a run holds `run_lock()`, so an automatic
update and one started from the window can't overlap (VIAVI allows only one
valid token per client ID, and both would move the same download position).
"""

from __future__ import annotations

import csv
import hashlib
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

from . import history
from .config import ConfigError, Settings, StrataSyncSettings, app_data_dir
from .pon_audit import (
    AUDIT_TABS,
    RACK_KEY_HEADERS,
    CableReplacementPlan,
    Flag,
    TabIndex,
    UpdatePlan,
    build_indexes,
    is_multi_service,
    plan_cable_replacement,
    plan_updates,
    rack_tab,
    reading_from_search,
    readings_from_result,
)
from .sheets import SheetAccessError, SheetWriter
from .stratasync import SYNC_TIME, StrataSyncClient, StrataSyncError
from .sync_state import SyncState

TEST_TYPE = "truepon"

Log = Callable[[str], None]


class RunInProgress(ConfigError):
    """Raised when another update (in the window or an automatic one) is already running."""


@contextmanager
def run_lock() -> Iterator[None]:
    """Hold the per-computer update lock, or raise RunInProgress.

    An OS file lock, so it's released even if the process is killed.
    """
    path = app_data_dir() / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RunInProgress("Another update is running on this computer (perhaps an automatic one). "
                                "Try again in a minute.") from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def error_text(exc: BaseException) -> str:
    """A readable message for a failed run."""
    if isinstance(exc, StrataSyncError):
        text = str(exc)
        if exc.status in (400, 401) and "Token" in text:
            text += ("\n\nCheck the client ID and secret, and that the right region "
                     "(USA or EU) is selected.")
        elif exc.body:
            text += f"\n\n{str(exc.body)[:500]}"
        return text
    if isinstance(exc, (ConfigError, SheetAccessError, ValueError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


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
    trigger: str = history.MANUAL,
) -> SyncResult:
    """Search StrataSync, plan the sheet updates and (if `apply`) write them.

    Applied runs, and their failures, are recorded in the history under `trigger`.
    Raises RunInProgress, ConfigError, StrataSyncError or SheetAccessError on failure.
    """
    with run_lock():
        if not apply:
            return _run_sync(ss, settings, apply=False, since=since, log=log)
        try:
            result = _run_sync(ss, settings, apply=True, since=since, log=log)
        except Exception as exc:
            history.record_status(trigger, "failed", settings.sheet_id, error_text(exc), log)
            raise
        history.record_run(result, trigger, log)
        return result


def _run_sync(
    ss: StrataSyncSettings,
    settings: Settings,
    *,
    apply: bool,
    since: Optional[datetime],
    log: Log,
) -> SyncResult:
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
    # A record that can't be read is flagged rather than stopping the run, and
    # doesn't hold back the download position (it would fail the same way every time).
    unreadable = []
    for rec in sorted(new, key=lambda r: r.get("syncTime", "")):
        source = f"{rec.get('name')} (id {rec['_id']})"
        if rec.get("power") not in (None, "") and not is_multi_service(rec):
            try:
                readings.append(reading_from_search(rec))
                continue
            except ValueError as exc:
                log(f"Unexpected search values for {source}: {exc}")
        why = "tested several PON types" if is_multi_service(rec) else "no usable power reading in search result"
        log(f"Downloading {rec.get('name')} ({why})")
        try:
            doc = client.download_json(rec["_id"])
        except StrataSyncError as exc:
            result.download_failures.append(f"{source}: {exc}")
            log(f"  could not download {source}: {exc}")
            continue
        except ValueError as exc:
            unreadable.append(Flag("unreadable", f"{rec.get('testLocationsLabel') or source}: "
                                                 f"test file could not be read ({exc})"))
            continue
        try:
            readings.extend(readings_from_result(doc, source=source, sync_time=rec.get("syncTime")))
        except ValueError as exc:
            unreadable.append(Flag("unreadable", f"{rec.get('testLocationsLabel') or source}: {exc}"))

    log("Opening the Google Sheet")
    writer = SheetWriter(settings)
    result.sheet_title = writer.spreadsheet.title
    tabs = writer.read_tabs(AUDIT_TABS)
    result.missing_tabs = [t for t in AUDIT_TABS if t not in tabs]
    if result.missing_tabs:
        log(f"Warning: tabs not found: {', '.join(result.missing_tabs)}")
    result.plan = plan_updates(readings, build_indexes(tabs))
    result.plan.flags.extend(unreadable)
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


def replace_cable(
    settings: Settings,
    data_hall: str,
    row: int,
    channel: str,
    replaced_at: datetime,
    *,
    apply: bool,
    log: Log = print,
) -> Tuple[str, CableReplacementPlan]:
    """Plan (and if `apply`, write) the clearing of one cable's results.

    Returns (sheet title, CableReplacementPlan). The sheet is read afresh each
    time, so applying never relies on an earlier preview's values.
    """
    if replaced_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ConfigError("The replacement time is in the future. Every test before it would be "
                          "ignored, including tests of the new cable.")
    with run_lock():
        return _replace_cable(settings, data_hall, row, channel, replaced_at, apply=apply, log=log)


def _replace_cable(settings: Settings, data_hall: str, row: int, channel: str, replaced_at: datetime,
                   *, apply: bool, log: Log) -> Tuple[str, CableReplacementPlan]:
    writer = SheetWriter(settings)
    tab = rack_tab(data_hall)
    values = writer.read_tabs([tab]).get(tab)
    if values is None:
        raise ConfigError(f"Tab {tab!r} not found in '{writer.spreadsheet.title}'.")
    plan = plan_cable_replacement(TabIndex.build(tab, values, RACK_KEY_HEADERS),
                                  data_hall, row, channel, replaced_at)
    cleared = sum(r.cleared for r in plan.racks)
    if not apply:
        return writer.spreadsheet.title, plan
    n = writer.write_cells(plan.updates)
    log(f"Cable replaced: {plan.cable} at {plan.replaced_at}. Cleared {cleared} rack(s) "
        f"({n} cells) in '{tab}'" + (f", left {len(plan.racks) - cleared} alone" if cleared < len(plan.racks) else ""))
    return writer.spreadsheet.title, plan


FLAG_CSV_HEADER = history.FLAG_FIELDS


def write_flags_csv(plan: UpdatePlan, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(FLAG_CSV_HEADER)
        w.writerows(history.flag_row(f) for f in plan.flags)
