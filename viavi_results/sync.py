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

If re-issuing is turned on, locations that need a retest get new, unassigned
StrataSync jobs after the sheet is written (see reissue.py). A preview shows
the jobs it would create without creating them.

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
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from . import cable_ids as cable_id_file
from . import history
from .cable_ids import CableIdFileError, cable_name
from .config import ConfigError, Settings, StrataSyncSettings, app_data_dir, cable_ids_path
from .reissue import (
    JOB_LOOKBACK,
    JOBS_SINCE,
    ReissuePlan,
    ReissueSettings,
    apply_updates,
    create_jobs,
    existing_jobs,
    find_retests,
    plan_cable_jobs,
    plan_reissue,
)
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
    reading_problems,
    readings_from_result,
    replacement_time,
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
    reissue: Optional[ReissuePlan] = None  # None when re-issuing is off
    reissue_error: str = ""  # why re-issuing couldn't be planned

    @property
    def reissue_problems(self) -> List[str]:
        """Re-issue failures; while there are any, the download position isn't moved."""
        failed = [f"{j.work_order_id}: {j.error}" for j in (self.reissue.failures if self.reissue else [])]
        return ([f"re-issue: {self.reissue_error}"] if self.reissue_error else []) + failed

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
    reissue: Optional[ReissueSettings] = None,
) -> SyncResult:
    """Search StrataSync, plan the sheet updates and (if `apply`) write them.

    With `reissue` enabled, also plans (and if `apply`, creates) jobs for
    locations that need a retest. Applied runs, and their failures, are
    recorded in the history under `trigger`.
    Raises RunInProgress, ConfigError, StrataSyncError or SheetAccessError on failure.
    """
    with run_lock():
        if not apply:
            return _run_sync(ss, settings, apply=False, since=since, log=log, reissue=reissue)
        try:
            result = _run_sync(ss, settings, apply=True, since=since, log=log, reissue=reissue)
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
    reissue: Optional[ReissueSettings] = None,
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
    indexes = build_indexes(tabs)
    result.plan = plan_updates(readings, indexes)
    result.plan.flags.extend(unreadable)
    log(f"Planned {len(result.plan.written)} result(s) to write, {len(result.plan.flags)} flag(s)")
    if reissue and reissue.enabled:
        result.reissue = _plan_reissue(client, result, indexes, reissue, log)

    if not apply:
        log("Preview only: nothing was written, no jobs were created and the download position was not moved.")
        return result

    result.cells_written = writer.write_cells(result.plan.updates)
    log(f"Wrote {result.cells_written} cell(s) to '{result.sheet_title}'")
    if result.reissue and result.reissue.jobs:
        create_jobs(client, result.reissue, reissue.template, log)
    if result.download_failures:
        log("Some downloads failed, so the download position was NOT moved; "
            "the next run will retry them.")
        return result
    if result.reissue_problems:
        log("Some jobs could not be re-issued, so the download position was NOT moved; "
            "the next run will try them again (jobs already created aren't repeated).")
        return result
    state.advance(records, run_at)
    state.save(path)
    result.position_saved = state.last_sync_time
    if state.last_sync_time:
        log(f"Saved download position: {state.last_sync_time.astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    return result


def _plan_reissue(client: StrataSyncClient, result: SyncResult, indexes, reissue: ReissueSettings,
                  log: Log) -> ReissuePlan:
    """The jobs to re-issue for this run (see reissue.py). A failure here doesn't
    stop the sheet update; it's recorded in result.reissue_error instead."""
    if result.download_failures:
        result.reissue_error = "some results couldn't be downloaded; the next update checks the jobs again"
        log(f"Re-issue: skipped, because {result.reissue_error}")
        return ReissuePlan()
    try:
        lookup = cable_id_file.load(cable_ids_path(reissue.cable_ids_file))
    except CableIdFileError as exc:
        # Stop rather than create jobs with blank Cable IDs; the next update tries again.
        result.reissue_error = f"{exc}. Fix the file; jobs are re-issued once it can be read"
        log(f"Re-issue: {result.reissue_error}")
        return ReissuePlan()
    log(f"Re-issue: Cable IDs from {lookup.path} ({lookup.describe()})"
        if lookup.path and lookup.path.exists() else
        f"Re-issue: no Cable ID file at {lookup.path}, so new jobs get a blank Cable ID (F1 jobs: "
        f"{cable_id_file.F1_CABLE_ID})")
    retests = find_retests(result.plan, reissue)
    apply_updates(indexes, result.plan.updates)  # judge finished jobs by the sheet after this update
    log(f"Re-issue: checking jobs finished in the last {JOB_LOOKBACK.days} days"
        + (f" and the jobs of {len(retests)} result(s) needing a retest" if retests else ""))
    try:
        jobs = existing_jobs(client.search_jobs(JOBS_SINCE, datetime.now(timezone.utc) + timedelta(days=35)))
        plan = plan_reissue(retests, jobs, indexes, reissue, now=datetime.now(timezone.utc), until=result.end,
                            confirm=lambda found: _confirm(client, found, indexes, reissue, log),
                            cable_ids=lookup)
    except (StrataSyncError, ValueError) as exc:
        result.reissue_error = error_text(exc)
        log(f"Re-issue: could not plan jobs: {result.reissue_error}")
        return ReissuePlan()
    for retest, existing in plan.already_reissued:
        log(f"Re-issue: {retest.label} is already in job {existing}; not re-issuing it")
    for retest, job in plan.waiting:
        log(f"Re-issue: {retest.label} needs a retest, but its job {job} isn't finished; "
            "waiting for it to be completed")
    for retest, why in plan.not_reissued:
        log(f"Re-issue: {retest.label} not re-issued: {why}")
    if plan.sheet_behind:
        log(f"Re-issue: {len(plan.sheet_behind)} location(s) passed in their job but the sheet doesn't have "
            "that result yet (synced before this computer's download position). Run an update with an "
            "earlier 'Search from' date to catch the sheet up; they aren't re-issued.")
    log(f"Re-issue: {len(plan.jobs)} job(s) for {sum(len(j.retests) for j in plan.jobs)} location(s); "
        f"{len(plan.already_reissued)} already re-issued, {len(plan.waiting)} waiting for their job, "
        f"{len(plan.not_reissued)} not re-issued")
    return plan


def _confirm(client: StrataSyncClient, retests: List, indexes, reissue: ReissueSettings,
             log: Log) -> dict:
    """reissue.Confirm: look up each location's tests in its job, several at once.

    A location passes if a test in the job has no problem (OLT ID as the sheet
    expects, a power reading) and, with low light on, its latest such test
    isn't weaker than the threshold. Otherwise the reason is what was wrong
    with its latest test. A lookup that fails counts as "needs a retest" with
    the sheet's reason, so nothing is silently dropped.

    Tests from before a cable replacement recorded in the sheet don't count:
    they measured the old cable. Without this, an in-spec test of the old
    cable would look like a result the sheet is merely missing, and the
    location would never be retested on the new cable.
    """
    from concurrent.futures import ThreadPoolExecutor

    def check(retest):
        if not retest.location_id:
            return retest.reason
        try:
            records = [r for r in client.search_workflow(retest.location_id) if r.get("type") == TEST_TYPE]
            readings = sorted((reading_from_search(r) for r in records if not is_multi_service(r)),
                              key=lambda rd: rd.test_time)
        except (StrataSyncError, ValueError):
            return retest.reason
        if not readings:
            return f"{retest.label}: not tested in {retest.own_job}"
        loc = readings[-1].location
        replaced = replacement_time(loc, indexes) if loc else None
        if replaced is not None:
            readings = [rd for rd in readings if _tested_after(rd, replaced)]
            if not readings:
                return (f"{retest.label}: its tests in {retest.own_job} were made before the cable was replaced "
                        f"({replaced.isoformat(timespec='minutes')})")
        index = indexes.get(loc.tab) if loc else None
        row = index.rows.get(loc.key) if index else None
        expected = index.get(row, index.column(loc.columns.expected_olt)) if row else None
        passing = [rd for rd in readings if not reading_problems(rd, expected)]
        if passing:
            last = passing[-1]
            limit = reissue.threshold(loc)
            if reissue.low_light and last.power_dbm is not None and last.power_dbm < limit:
                return f"{retest.label}: {last.power_dbm:g} dBm is weaker than {limit:g} dBm"
            return None
        return reading_problems(readings[-1], expected)[0].message

    log(f"Re-issue: looking up {len(retests)} location(s) in StrataSync that the sheet has no passing result for")
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip((r.label for r in retests), pool.map(check, retests)))


def _tested_after(reading, when: datetime) -> bool:
    try:
        return reading.timestamp > when
    except ValueError:  # no usable test time: can't show it's from the new cable
        return False


def replace_cable(
    settings: Settings,
    data_hall: str,
    row: int,
    channel: str,
    replaced_at: datetime,
    *,
    apply: bool,
    log: Log = print,
    jobs_for: Optional[StrataSyncSettings] = None,
    template: str = "",
    new_cable_id: Optional[str] = None,
    cable_ids_file: str = "",
) -> Tuple[str, CableReplacementPlan, Optional[ReissuePlan], Optional["CableIdChange"]]:
    """Plan (and if `apply`, write) the clearing of one cable's results.

    With `jobs_for` (the StrataSync account), also plans (and if `apply`,
    creates) new unassigned jobs from `template` for the cable's rack cohorts,
    covering the racks that are cleared. They're created after the sheet is
    written; any that fail are reported on the returned plan.

    With `new_cable_id` (the new cable's ID; "" if not known yet), the Cable
    ID lookup file gets it in place of the old cable's, after the sheet is
    written, and the new jobs get it.

    Returns (sheet title, CableReplacementPlan, jobs plan or None, Cable ID
    change or None). The sheet is read afresh each time, so applying never
    relies on an earlier preview's values. Applied attempts, including failed
    ones, are recorded in the history.
    """
    if replaced_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ConfigError("The replacement time is in the future. Every test before it would be "
                          "ignored, including tests of the new cable.")
    attempt = history.new_cable_replacement(settings.sheet_id, cable_name((data_hall, row, channel)),
                                            replaced_at.isoformat(timespec="seconds"), (new_cable_id or "").strip())
    try:
        with run_lock():
            result = _replace_cable(settings, data_hall, row, channel, replaced_at, apply=apply, log=log,
                                    jobs_for=jobs_for, template=template, new_cable_id=new_cable_id,
                                    cable_ids_file=cable_ids_file, attempt=attempt)
    except Exception as exc:
        if apply:
            # cells_written is set once the sheet has been changed.
            attempt.update(status="failed" if attempt["cells_written"] == "" else "partial", message=error_text(exc))
            history.record_cable_replacement(attempt, log)
        raise
    if apply:
        history.record_cable_replacement(attempt, log)
    return result


@dataclass
class CableIdChange:
    """The lookup file's entry for a replaced cable, before and after."""

    cable: str  # e.g. "RMN2.DHB.05 AA"
    old: str
    new: str
    file: Path
    error: str = ""  # set if the file couldn't be updated


def _replace_cable(settings: Settings, data_hall: str, row: int, channel: str, replaced_at: datetime,
                   *, apply: bool, log: Log, jobs_for: Optional[StrataSyncSettings], template: str,
                   new_cable_id: Optional[str], cable_ids_file: str, attempt: Dict[str, object],
                   ) -> Tuple[str, CableReplacementPlan, Optional[ReissuePlan], Optional[CableIdChange]]:
    writer = SheetWriter(settings)
    attempt["sheet"] = writer.spreadsheet.title
    tab = rack_tab(data_hall)
    attempt["tab"] = tab
    values = writer.read_tabs([tab]).get(tab)
    if values is None:
        raise ConfigError(f"Tab {tab!r} not found in '{writer.spreadsheet.title}'.")
    plan = plan_cable_replacement(TabIndex.build(tab, values, RACK_KEY_HEADERS),
                                  data_hall, row, channel, replaced_at)
    cleared = sum(r.cleared for r in plan.racks)
    change = lookup = None
    if new_cable_id is not None:
        key = (data_hall, row, channel)
        try:
            lookup = cable_id_file.load(cable_ids_path(cable_ids_file))
        except CableIdFileError as exc:
            raise ConfigError(f"{exc}. Fix the file before recording the replacement, so the new "
                              "cable's ID can be saved in it.") from None
        change = CableIdChange(cable_name(key), lookup.get(key), new_cable_id.strip(), lookup.path)
    jobs_plan = client = None
    if jobs_for:
        client = StrataSyncClient(jobs_for)
        jobs = existing_jobs(client.search_jobs(JOBS_SINCE, datetime.now(timezone.utc) + timedelta(days=35)))
        jobs_plan = plan_cable_jobs(data_hall, row, channel, [r.rack for r in plan.racks if r.cleared],
                                    plan.replaced_at, jobs, cable_id=change.new if change else "")
    if not apply:
        return writer.spreadsheet.title, plan, jobs_plan, change
    attempt.update(racks=len(plan.racks), cleared=cleared, left_alone=len(plan.racks) - cleared,
                   old_cable_id=change.old if change else "")
    history.record_cable_replacement_racks(attempt, plan, log)
    n = writer.write_cells(plan.updates)
    attempt["cells_written"] = n
    log(f"Cable replaced: {plan.cable} at {plan.replaced_at}. Cleared {cleared} rack(s) "
        f"({n} cells) in '{tab}'" + (f", left {len(plan.racks) - cleared} alone" if cleared < len(plan.racks) else ""))
    if change is not None:
        try:
            lookup.set((data_hall, row, channel), change.new)
            log(f"Cable ID of {change.cable} in {change.file.name}: {change.old or '(blank)'} -> "
                f"{change.new or '(blank)'}")
        except OSError as exc:
            change.error = f"couldn't save {change.file}: {exc}"
            log(f"Warning: the Cable ID file wasn't updated: {change.error}. Enter {change.cable} = "
                f"{change.new or '(blank)'} in it by hand.")
        history.record_cable_id(change, plan.replaced_at, writer.spreadsheet.title, log)
    if jobs_plan and jobs_plan.jobs:
        create_jobs(client, jobs_plan, template, log)
        history.record_jobs(jobs_plan, history.CABLE, writer.spreadsheet.title, log)
    problems = [f"Cable ID file: {change.error}"] if change and change.error else []
    if jobs_plan:
        problems += [f"{j.work_order_id}: {j.error}" for j in jobs_plan.failures]
        problems += [f"{j.work_order_id}: Cable ID not set: {j.cable_id_error}" for j in jobs_plan.jobs if j.cable_id_error]
        attempt.update(jobs_created=sum(j.status == "created" for j in jobs_plan.jobs),
                       jobs_failed=len(jobs_plan.failures))
    attempt.update(status="partial" if problems else "ok", message="; ".join(problems))
    return writer.spreadsheet.title, plan, jobs_plan, change


FLAG_CSV_HEADER = history.FLAG_FIELDS


def write_flags_csv(plan: UpdatePlan, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(FLAG_CSV_HEADER)
        w.writerows(history.flag_row(f) for f in plan.flags)
