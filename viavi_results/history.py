"""A permanent record of every update that writes to the sheet.

Each update (from the window or an automatic one) appends to CSV files in
`history_dir()`:

  runs.csv      one row per update: when, how it was started, the outcome and counts
  flagged.csv   every flagged result, which was *not* written to the sheet
  written.csv   every result written: location, tab, cells, dBm, test time
  jobs.csv      every job re-issued (or that failed to be): name, locations, why, Cable ID
  cable_ids.csv every change to the Cable ID lookup file, by a cable replacement or a Cable ID change
  cable_id_jobs.csv every StrataSync job whose Cable ID a Cable ID change set (or failed to)
  cable_replacements.csv every applied cable replacement, including failed ones: who, outcome, counts
  cable_replacement_racks.csv every rack of those replacements: what it held and whether it was to be cleared

Automatic updates also append their progress messages to automatic.log.
Nothing here is ever rewritten or pruned, so the files are an audit trail.
"""

from __future__ import annotations

import csv
import getpass
import platform
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Sequence

from .config import app_data_dir

if TYPE_CHECKING:
    from .pon_audit import CableReplacementPlan, Flag
    from .reissue import ReissuePlan
    from .sync import SyncResult

Log = Callable[[str], None]

MANUAL = "manual"
AUTOMATIC = "automatic"
CABLE = "cable replacement"
# cable_ids.csv's "change" column. Rows from before it had none: all were replacements.
REPLACEMENT = "replacement"
CORRECTION = "correction"

RUNS_HEADER = ["run_at", "trigger", "status", "sheet", "searched_from", "searched_to", "found", "truepon",
               "new", "written", "flagged", "skipped", "cells_written", "download_failures",
               "position_saved", "message"]
FLAG_FIELDS = ["kind", "label", "message", "test_time", "tech", "serial_no", "source"]
FLAGGED_HEADER = ["run_at", "trigger", "sheet"] + FLAG_FIELDS
WRITTEN_HEADER = ["run_at", "trigger", "sheet", "label", "tab", "cells", "dbm", "test_time",
                  "sheet_time", "tech", "serial_no", "source"]

JOBS_HEADER = ["run_at", "trigger", "sheet", "work_order_id", "status", "workflow_id", "cohort", "locations",
               "reasons", "error", "cable_id", "cable_id_error"]
CABLE_IDS_HEADER = ["run_at", "sheet", "cable", "replaced_at", "old_cable_id", "new_cable_id", "file", "error",
                    "change", "jobs"]
CABLE_ID_JOBS_HEADER = ["run_at", "cable", "workflow_id", "work_order_id", "scheduled", "state", "old_cable_id",
                        "new_cable_id", "outcome", "error"]
CABLE_REPLACEMENTS_HEADER = ["run_at", "user", "computer", "status", "sheet", "tab", "cable", "replaced_at", "racks",
                             "cleared", "left_alone", "cells_written", "old_cable_id", "new_cable_id",
                             "jobs_created", "jobs_failed", "message"]
CABLE_REPLACEMENT_RACKS_HEADER = ["run_at", "cable", "replaced_at", "tab", "rack", "sheet_row", "action", "reason",
                                  "old_dbm", "old_timestamp"]

LOG_MAX_BYTES = 5_000_000


def history_dir() -> Path:
    return app_data_dir() / "history"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(dt: Optional[datetime]) -> str:
    return dt.astimezone().isoformat(timespec="seconds") if dt else ""


def flag_row(f: "Flag") -> list:
    r = f.reading
    return [f.kind, r and r.label, f.message, r and r.test_time, r and r.tech_name, r and r.serial_no, r and r.source]


def _last_header(path: Path, first: str) -> List[str]:
    """The newest header row in an existing file (one starting with `first`)."""
    last: List[str] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if row and row[0] == first:
                last = row
    return last


def _write(path: Path, header: Sequence[str], rows: Iterable[Sequence]) -> None:
    new = not path.exists() or path.stat().st_size == 0
    # A file started by an older version has fewer columns. It's only ever
    # added to, so the new header goes in as a row before the longer rows.
    changed = not new and _last_header(path, header[0]) != list(header)
    # utf-8-sig so Excel reads it correctly; the BOM is only written at the start of the file.
    with path.open("a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new or changed:
            w.writerow(header)
        w.writerows(rows)


def _append(name: str, header: Sequence[str], rows: List[Sequence], log: Log) -> None:
    if not rows:
        return
    path = history_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write(path, header, rows)
    except PermissionError:
        # Excel on Windows locks a CSV while it's open; keep the rows in a file beside it.
        alt = path.with_name(f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
        _write(alt, header, rows)
        log(f"History: {path.name} is open in another program, so this update's rows went to {alt.name}")


def record_run(result: "SyncResult", trigger: str, log: Log = print) -> None:
    """Append an applied update, its flagged results and its written results to the history."""
    plan, run_at, sheet = result.plan, _now(), result.sheet_title
    skipped = sum(1 for _, why in plan.skipped if why != "flagged (not written)")
    problems = result.download_failures + result.reissue_problems
    message = "; ".join(problems)
    if result.reissue and result.reissue.jobs:
        created = sum(j.status == "created" for j in result.reissue.jobs)
        message = "; ".join(filter(None, [f"re-issued {created} job(s)", message]))
    if result.missing_tabs:
        message = "; ".join(filter(None, [f"tabs not found: {', '.join(result.missing_tabs)}", message]))
    try:
        _append("runs.csv", RUNS_HEADER, [[
            run_at, trigger, "partial" if problems else "ok", sheet,
            _iso(result.start), _iso(result.end), result.found, result.truepon, result.new,
            len(plan.written), len(plan.flags), skipped, result.cells_written,
            len(result.download_failures), _iso(result.position_saved), message,
        ]], log)
        _append("flagged.csv", FLAGGED_HEADER, [[run_at, trigger, sheet] + flag_row(f) for f in plan.flags], log)
        _append("written.csv", WRITTEN_HEADER, [
            [run_at, trigger, sheet, rd.label, power.tab, f"{power.a1}, {stamp.a1}", rd.power_dbm,
             rd.test_time, rd.sheet_time, rd.tech_name, rd.serial_no, rd.source]
            for rd, power, stamp in plan.written_cells()], log)
    except OSError as exc:  # the sheet is already written; don't turn that into a failure
        log(f"Could not save the update history: {exc}")
    if result.reissue:
        record_jobs(result.reissue, trigger, sheet, log, run_at)


def record_jobs(plan: "ReissuePlan", trigger: str, sheet: str, log: Log = print,
                run_at: Optional[str] = None) -> None:
    """Append the jobs an update or cable replacement created (or failed to) to jobs.csv."""
    run_at = run_at or _now()
    try:
        _append("jobs.csv", JOBS_HEADER, [
            [run_at, trigger, sheet, j.work_order_id, j.status, j.workflow_id or "", j.cohort,
             " ".join(j.labels), " | ".join(r.reason for r in j.retests), j.error, j.cable_id, j.cable_id_error]
            for j in plan.jobs], log)
    except OSError as exc:
        log(f"Could not save the job history: {exc}")


def record_cable_id(change, replaced_at: str, sheet: str, log: Log = print, kind: str = REPLACEMENT,
                    jobs: str = "") -> None:
    """Append a change to the Cable ID lookup file to cable_ids.csv: a cable
    replacement's (`replaced_at` set) or a Cable ID change's (kind CORRECTION)."""
    try:
        _append("cable_ids.csv", CABLE_IDS_HEADER, [[
            _now(), sheet, change.cable, replaced_at, change.old, change.new, str(change.file), change.error,
            kind, jobs]], log)
    except OSError as exc:
        log(f"Could not save the Cable ID history: {exc}")


def record_cable_id_jobs(cable: str, jobs: Iterable, new: str, log: Log = print) -> None:
    """Append the jobs a Cable ID change set the Cable ID of (or tried to) to cable_id_jobs.csv."""
    run_at = _now()
    try:
        _append("cable_id_jobs.csv", CABLE_ID_JOBS_HEADER, [
            [run_at, cable, j.workflow_id, j.work_order_id, j.scheduled or "", j.state, j.current, new,
             j.outcome, j.error] for j in jobs], log)
    except OSError as exc:
        log(f"Could not save the Cable ID job history: {exc}")


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no login name available; the record is still worth keeping
        return ""


def new_cable_replacement(sheet: str, cable: str, replaced_at: str, new_cable_id: str) -> Dict[str, object]:
    """A cable_replacements.csv row for an attempt that's starting. The
    replacement fills it in as it goes, so a failure records how far it got."""
    row: Dict[str, object] = dict.fromkeys(CABLE_REPLACEMENTS_HEADER, "")
    row.update(run_at=_now(), user=_user(), computer=platform.node(), sheet=sheet, cable=cable,
               replaced_at=replaced_at, new_cable_id=new_cable_id)
    return row


def record_cable_replacement(attempt: Dict[str, object], log: Log = print) -> None:
    """Append a finished attempt to cable_replacements.csv."""
    row = {**attempt, "message": " ".join(str(attempt["message"]).split())}
    try:
        _append("cable_replacements.csv", CABLE_REPLACEMENTS_HEADER, [list(row.values())], log)
    except OSError as exc:  # the sheet may already be written; don't turn that into a failure
        log(f"Could not save the cable replacement history: {exc}")


def record_cable_replacement_racks(attempt: Dict[str, object], plan: "CableReplacementPlan", log: Log = print) -> None:
    """Append what each rack holds to cable_replacement_racks.csv. Called before
    the sheet is written, so the old values are kept however the write ends."""
    try:
        _append("cable_replacement_racks.csv", CABLE_REPLACEMENT_RACKS_HEADER, [
            [attempt["run_at"], attempt["cable"], attempt["replaced_at"], plan.tab, r.rack, r.row,
             "clear" if r.cleared else "leave", r.reason, r.old_power, r.old_timestamp]
            for r in plan.racks], log)
    except OSError as exc:
        log(f"Could not save the cable replacement's rack history: {exc}")


def cable_id_history() -> List[Dict[str, str]]:
    """Every row of cable_ids.csv, oldest first, as dicts. Rows written before a
    column existed lack it ("change" is then a replacement). Files changed
    header over time (see _write), so each row is read with the header above it."""
    rows: List[Dict[str, str]] = []
    try:
        with (history_dir() / "cable_ids.csv").open(newline="", encoding="utf-8-sig") as fh:
            header: List[str] = []
            for row in csv.reader(fh):
                if row and row[0] == CABLE_IDS_HEADER[0]:
                    header = row
                elif row and header:
                    rows.append(dict(zip(header, row)))
    except OSError:
        return []
    return rows


def record_status(trigger: str, status: str, sheet: str, message: str, log: Log = print) -> None:
    """Append an update that didn't complete ("failed" or "skipped") to runs.csv."""
    row: Dict[str, object] = dict.fromkeys(RUNS_HEADER, "")
    row.update(run_at=_now(), trigger=trigger, status=status, sheet=sheet, message=" ".join(message.split()))
    try:
        _append("runs.csv", RUNS_HEADER, [list(row.values())], log)
    except OSError as exc:
        log(f"Could not save the update history: {exc}")


def recent_runs(limit: int = 200) -> List[Dict[str, str]]:
    """The latest rows of runs.csv, newest first."""
    try:
        with (history_dir() / "runs.csv").open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return []
    return rows[::-1][:limit]


def runs_mtime() -> float:
    try:
        return (history_dir() / "runs.csv").stat().st_mtime
    except OSError:
        return 0.0


def automatic_log() -> Log:
    """A log function that appends timestamped lines to automatic.log."""
    path = history_dir() / "automatic.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass

    def log(message: str) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='seconds')}  {message}\n"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass

    return log
