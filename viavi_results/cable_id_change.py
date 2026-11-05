"""Change one cable's Cable ID: in the lookup file and on that cable's StrataSync jobs.

For a Cable ID that was never entered (blank in the file, or the cable isn't in
it), or one entered with a typo. It isn't for a replaced cable: a replacement
(sync.replace_cable) is a new cable, so it gives the file the new cable's ID
and leaves the old cable's jobs with the old one. This change keeps that
split:

  * Jobs scheduled before the cable's latest known replacement tested the old
    cable, so they're listed but not changed by default. The replacement time
    comes from this computer's history (cable_ids.csv) and from the sheet (a
    rack on the cable with a blank dBm and a timestamp, see
    pon_audit.replacement_time), whichever is later.
  * Only jobs whose Cable ID is blank, or is the file's current one (the typo
    being fixed), are changed by default. A job with some other Cable ID is
    listed for a person to decide: it may be the old cable's, or a different
    typo entered by hand.

The person can tick or untick any listed job (except ones that also hold
locations on other cables) before applying.

A job's Cable ID is set with the same PATCH as a new job's
(StrataSyncClient.set_cable_id): only its Job Manager attributes are sent.
Approved jobs are locked, so with `approved` on, each is unapproved, changed
and approved again, as job_manager's step-by-step does; it's approved again
even if the change fails, so it isn't left unapproved. After each change the
job's state, tech, dates and results are compared with before, and the run
stops at the first job where anything besides the Cable ID changed. At the end
every changed job is looked up again to check it.

The file is saved after the jobs, so new jobs keep getting the old ID until the
jobs are done; it's saved even if some jobs failed (the new ID is the right
one). To retry the failed jobs, change the cable to the same ID again: jobs
still holding the ID it replaced (from this computer's history) are ticked.

Everything happens under sync.run_lock(), so an update can't create a job with
the old ID, or replace the cable, halfway through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from . import cable_ids as cable_id_file
from . import history
from .cable_ids import CableIdFileError, CableKey, cable_name
from .config import ConfigError, Settings, StrataSyncSettings, cable_ids_path
from .pon_audit import (DATA_HALLS, RACK_KEY_HEADERS, SITE, RackLocation, TabIndex, parse_location, rack_tab,
                        replacement_time)
from .reissue import JOBS_SINCE, _parse_dt
from .stratasync import StrataSyncClient, StrataSyncError

Log = Callable[[str], None]

# What the site's Cable IDs look like (S7 + 8 characters + 031). Anything else
# gets a warning, not a refusal: it's a likely typo, but the format may change.
CABLE_ID_FORMAT = re.compile(r"^S7[0-9A-Z]{8}031$")


@dataclass
class JobChange:
    """One of the cable's jobs, and whether (and how) its Cable ID is changed."""

    workflow_id: int
    work_order_id: str
    scheduled: Optional[date]
    state: str
    current: str  # its Cable ID when it was looked up
    eligible: bool  # ticked by default (see note)
    note: str  # why it is or isn't ticked by default
    tickable: bool = True  # False: it can't be ticked (holds other cables' locations)
    job: Dict[str, Any] = field(default_factory=dict, repr=False)  # as StrataSync returned it
    outcome: str = ""  # after applying: "changed", "failed", "skipped", "not ticked"
    error: str = ""

    @property
    def approved(self) -> bool:
        return self.state == "APPROVED"


@dataclass
class CableIdCorrection:
    key: CableKey
    cable: str  # e.g. "RMN2.DHB.05 AA"
    old: str  # in the file now ("" if blank or not in it)
    new: str
    file: Path
    in_file: bool  # False: the cable isn't in the file yet, so it's added
    replaced_at: Optional[datetime] = None  # the cable's latest known replacement
    replaced_source: str = ""  # where that came from
    sheet_note: str = ""  # why the sheet wasn't checked, if it wasn't
    jobs: List[JobChange] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    file_error: str = ""  # set if the file couldn't be saved
    stopped: str = ""  # set if the run stopped early (a job changed more than its Cable ID)

    @property
    def file_changes(self) -> bool:
        return self.old != self.new or not self.in_file

    @property
    def changed(self) -> List[JobChange]:
        return [j for j in self.jobs if j.outcome == "changed"]

    @property
    def failed(self) -> List[JobChange]:
        return [j for j in self.jobs if j.outcome == "failed"]

    def summary(self) -> str:
        """For the history: what happened to the jobs."""
        counts = {k: sum(j.outcome == k for j in self.jobs) for k in ("changed", "failed", "skipped")}
        return ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "no jobs changed"


# ---- Which jobs belong to the cable --------------------------------------------------


def _locations(job: Mapping[str, Any]) -> List[str]:
    return [str(loc.get("label") or "") for test in job.get("tests") or [] for loc in test.get("testLocations") or []]


def _work_order(job: Mapping[str, Any]) -> str:
    return str((job.get("workflow") or {}).get("workOrderId") or "")


def job_cable_id(job: Mapping[str, Any]) -> str:
    attr = ((job.get("workflow") or {}).get("jobManagerAttributes") or {}).get("cableId") or {}
    return str(attr.get("value") or "").strip() if isinstance(attr, dict) else ""


def on_cable(job: Mapping[str, Any], key: CableKey) -> Optional[bool]:
    """True if every test location of `job` is a rack on this cable; False if
    some are and some aren't (other cables, F1, other sites); None if none are.

    A job with no recognizable locations counts if its name is one of the
    cable's rack cohorts ("RMN2.DHB.05 AA C1", "… C3 F", "… C2 RT1").
    """
    data_hall, row, channel = key
    labels = _locations(job)
    here = other = 0
    for label in labels:
        loc = parse_location(label)
        if isinstance(loc, RackLocation) and (loc.data_hall, loc.row, loc.side) == key:
            here += 1
        elif label.strip():
            other += 1
    if here:
        return not other
    if not labels:
        name = re.compile(rf"^{re.escape(cable_name(key))}\s*C[123]\b", re.IGNORECASE)
        return True if name.match(_work_order(job).strip()) else None
    return None


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def classify(job: Mapping[str, Any], key: CableKey, old: Set[str], new: str,
             replaced_at: Optional[datetime]) -> Optional[JobChange]:
    """The cable's job as a JobChange, ticked by default or not; None if it isn't the cable's."""
    belongs = on_cable(job, key)
    if belongs is None:
        return None
    wf = job.get("workflow") or {}
    scheduled_at = _parse_dt(wf.get("date"))
    current = job_cable_id(job)
    change = JobChange(int(wf.get("workflowId") or 0), _work_order(job),
                       scheduled_at.date() if scheduled_at else None, str(job.get("state") or ""),
                       current, False, "", job=dict(job))
    shown_old = " or ".join(repr(o) if o else "blank" for o in sorted(old, key=bool))
    if not change.workflow_id:
        change.note, change.tickable = "no workflowId from StrataSync", False
    elif not belongs:
        change.note, change.tickable = "also holds locations on other cables; change it by hand", False
    elif current == new:
        change.note = "already has the new Cable ID"
    elif replaced_at and scheduled_at and scheduled_at.date() < replaced_at.astimezone(scheduled_at.tzinfo).date():
        change.note = (f"scheduled before the cable was replaced ({replaced_at.astimezone():%Y-%m-%d %H:%M}), "
                       "so it tested the old cable")
    elif not any(_same(current, o) for o in old):
        change.note = f"has Cable ID {current!r}, not {shown_old}; tick it if that's wrong too"
    else:
        change.eligible = True
        change.note = "no Cable ID yet" if not current else f"has the Cable ID being replaced, {current!r}"
    return change


# ---- When the cable was last replaced -------------------------------------------------


def latest_replacement(cable: str, rows: Sequence[Mapping[str, str]]) -> Optional[datetime]:
    """The latest replacement of `cable` in cable_ids.csv's rows."""
    times = []
    for r in rows:
        if r.get("cable", "").strip().upper() != cable.upper():
            continue
        if (r.get("change") or history.REPLACEMENT) != history.REPLACEMENT:
            continue
        when = _parse_dt(r.get("replaced_at"))
        if when is not None:
            times.append(when if when.tzinfo else when.astimezone())
    return max(times, default=None)


def previous_ids(cable: str, current: str, rows: Sequence[Mapping[str, str]]) -> Set[str]:
    """The IDs Cable ID changes on this computer replaced with `current`,
    following the chain back (a typo fixed twice). Jobs still holding one
    are ones an earlier change didn't get to (it failed, or was stopped)."""
    found: Set[str] = set()
    target = {current.strip().casefold()}
    for r in reversed(rows):
        if r.get("cable", "").strip().upper() != cable.upper() or r.get("change") != history.CORRECTION:
            continue
        if r.get("new_cable_id", "").strip().casefold() in target:
            prev = r.get("old_cable_id", "").strip()
            if prev.casefold() not in target:
                found.add(prev)
                target.add(prev.casefold())
    return found


def sheet_replacement(settings: Settings, key: CableKey) -> Optional[datetime]:
    """The latest replacement of the cable recorded in the sheet (a rack with a
    blank dBm and a timestamp). A later test of the rack overwrites it, which is
    why this computer's history is checked too."""
    from .sheets import SheetWriter

    data_hall, row, channel = key
    tab = rack_tab(data_hall)
    values = SheetWriter(settings).read_tabs([tab]).get(tab)
    if values is None:
        return None
    index = TabIndex.build(tab, values, RACK_KEY_HEADERS)
    racks = [k[2] for k in index.rows if k[0] == data_hall and k[1] == row]
    times = [replacement_time(RackLocation(data_hall, row, rack, channel), {tab: index}) for rack in racks]
    return max((t for t in times if t is not None), default=None)


# ---- Planning ---------------------------------------------------------------------------------


def check_cable(data_hall: str, row: int, channel: str) -> CableKey:
    if data_hall not in DATA_HALLS:
        raise ConfigError(f"Choose a data hall ({', '.join(DATA_HALLS)}).")
    if not 1 <= row <= 99:
        raise ConfigError("Enter a row number from 1 to 99.")
    if channel not in ("A", "B"):
        raise ConfigError("Choose channel A or B.")
    return (data_hall, row, channel)


def plan_change(key: CableKey, new: str, *, cable_ids_file: str, jobs: Sequence[Mapping[str, Any]],
                history_rows: Sequence[Mapping[str, str]], sheet_replaced: Optional[datetime] = None,
                sheet_note: str = "") -> CableIdCorrection:
    """What changing the cable's ID to `new` would do (nothing is changed).

    `jobs` are StrataSync's jobs (search results), `history_rows` cable_ids.csv's.
    """
    try:
        lookup = cable_id_file.load(cable_ids_path(cable_ids_file))
    except CableIdFileError as exc:
        raise ConfigError(f"{exc}. Fix the file first.") from None
    new = new.strip()
    name = cable_name(key)
    plan = CableIdCorrection(key, name, lookup.get(key), new, lookup.path, key in lookup.ids, sheet_note=sheet_note)

    replaced = latest_replacement(name, history_rows)
    sources = []
    if replaced:
        sources.append("this computer's history")
    if sheet_replaced and (replaced is None or sheet_replaced > replaced):
        replaced, sources = sheet_replaced, ["the sheet"]
    elif sheet_replaced and sheet_replaced == replaced:
        sources.append("the sheet")
    plan.replaced_at, plan.replaced_source = replaced, " and ".join(sources)

    # The IDs being replaced: blank, the file's, and any an earlier change here replaced with it.
    old = {"", plan.old} | previous_ids(name, plan.old, history_rows)
    for job in jobs:
        change = classify(job, key, old, new, replaced)
        if change:
            plan.jobs.append(change)
    plan.jobs.sort(key=lambda j: (j.scheduled or date.min, j.work_order_id))

    if not new:
        plan.warnings.append("The new Cable ID is blank: the ticked jobs' Cable ID will be cleared.")
    elif not CABLE_ID_FORMAT.match(new):
        plan.warnings.append(f"{new!r} doesn't look like the site's other Cable IDs (S7 + 8 characters + 031). "
                             "Check it for a typo.")
    others = [cable_name(k) for k, v in lookup.ids.items() if k != key and v and _same(v, new)]
    if others:
        plan.warnings.append(f"{', '.join(others)} already {'has' if len(others) == 1 else 'have'} Cable ID {new!r} "
                             "in the file. Each cable has its own serial: check which cable this is.")
    return plan


def _search(client: StrataSyncClient) -> List[Dict[str, Any]]:
    jobs = client.search_jobs(JOBS_SINCE, datetime.now(timezone.utc) + timedelta(days=35))
    return [j for j in jobs if not _protected(j)]


def _protected(job: Mapping[str, Any]) -> bool:
    """Jobs at other sites (e.g. RMN1) are never changed, whatever their labels say about this site."""
    labels = [_work_order(job)] + _locations(job)
    return any(re.search(r"RMN\d", label.upper()) and not label.strip().upper().startswith(SITE) for label in labels)


def preview(stratasync: StrataSyncSettings, key: CableKey, new: str, *, cable_ids_file: str = "",
            sheet: Optional[Settings] = None, log: Log = print) -> CableIdCorrection:
    """Look everything up and plan the change; nothing is changed."""
    sheet_replaced, sheet_note = None, ""
    if sheet is None:
        sheet_note = "the sheet isn't set up in the main window"
    else:
        try:
            sheet_replaced = sheet_replacement(sheet, key)
        except Exception as exc:  # the history may still know; say so instead of failing
            sheet_note = f"couldn't read the sheet ({exc})"
    if sheet_note:
        log(f"Cable ID change: {sheet_note}, so only this computer's history shows when the cable was replaced")
    jobs = _search(StrataSyncClient(stratasync))
    return plan_change(key, new, cable_ids_file=cable_ids_file, jobs=jobs, history_rows=history.cable_id_history(),
                       sheet_replaced=sheet_replaced, sheet_note=sheet_note)


# ---- Applying -------------------------------------------------------------------------------------


def fingerprint(job: Mapping[str, Any]) -> Dict[str, Any]:
    """What a Cable ID change must leave alone: state, tech, dates, results (as job_manager checks)."""
    wf = job.get("workflow") or {}
    info = wf.get("techInfo") or {}
    return {
        "state": job.get("state"),
        "status": job.get("status"),
        "techId": info.get("techId"),
        "teamTechIds": sorted(info.get("teamTechIds") or []),
        "testStats": job.get("testStats"),
        "testDataCount": job.get("testDataCount"),
        "date": wf.get("date"),
        "locations": [(loc.get("workflowId"), loc.get("state"))
                      for test in job.get("tests") or [] for loc in test.get("testLocations") or []],
    }


def _differences(before: Mapping[str, Any], after: Mapping[str, Any], ignore=()) -> List[str]:
    a, b = fingerprint(before), fingerprint(after)
    return [f"{k}: {a[k]} -> {b[k]}" for k in a if k not in ignore and a[k] != b[k]]


def apply(stratasync: StrataSyncSettings, previewed: CableIdCorrection, ticked: Set[int], *, approved: bool,
          cable_ids_file: str = "", log: Log = print) -> CableIdCorrection:
    """Change the ticked jobs (workflowIds) and then the file, as previewed.

    Everything is looked up again first. Nothing is changed if the file's
    Cable ID for the cable has changed since the preview; a ticked job whose
    Cable ID or cable has changed since is skipped. Approved jobs are only
    changed with `approved` (see the module docstring).
    """
    from .sync import run_lock

    with run_lock():
        client = StrataSyncClient(stratasync)
        fresh = plan_change(previewed.key, previewed.new, cable_ids_file=cable_ids_file, jobs=_search(client),
                            history_rows=history.cable_id_history(), sheet_replaced=previewed.replaced_at,
                            sheet_note=previewed.sheet_note)
        if fresh.old != previewed.old or fresh.file != previewed.file:
            raise ConfigError(f"{fresh.cable}'s Cable ID in {fresh.file.name} is now {fresh.old or '(blank)'}, not "
                              f"{previewed.old or '(blank)'} as when you previewed. Preview again.")
        before = {j.workflow_id: j for j in previewed.jobs}
        for job in fresh.jobs:
            was = before.get(job.workflow_id)
            if job.workflow_id not in ticked:
                job.outcome = "not ticked"
            elif was is None or not job.tickable:
                job.outcome, job.error = "skipped", job.note if was else "not in the preview"
            elif job.current != was.current:
                job.outcome, job.error = "skipped", f"its Cable ID changed to {job.current!r} since the preview"
            elif job.current == fresh.new:
                job.outcome, job.error = "skipped", "already has the new Cable ID"
            elif job.approved and not approved:
                job.outcome, job.error = "skipped", "approved (locked); tick 'Also change approved jobs'"
        for wid in sorted(ticked - {j.workflow_id for j in fresh.jobs}):
            was = before.get(wid)
            log(f"Cable ID change: {was.work_order_id if was else wid} is no longer one of the cable's jobs; skipped")

        for job in fresh.jobs:
            if job.outcome:
                if job.outcome == "skipped":
                    log(f"Cable ID change: {job.work_order_id} skipped: {job.error}")
                continue
            if fresh.stopped:
                job.outcome, job.error = "skipped", "not tried: the run stopped (see above)"
                continue
            _change_job(client, job, fresh, log)

        _verify(client, fresh, log)

        if fresh.file_changes:
            lookup = cable_id_file.load(fresh.file)
            try:
                lookup.set(fresh.key, fresh.new)
                log(f"Cable ID of {fresh.cable} in {fresh.file.name}: {fresh.old or '(blank)'} -> {fresh.new or '(blank)'}")
            except OSError as exc:
                fresh.file_error = f"couldn't save {fresh.file}: {exc}"
                log(f"Warning: the Cable ID file wasn't updated: {fresh.file_error}. Enter {fresh.cable} = "
                    f"{fresh.new or '(blank)'} in it by hand.")
        change = _FileChange(fresh.cable, fresh.old, fresh.new, fresh.file, fresh.file_error)
        if fresh.file_changes or fresh.changed or fresh.failed:
            history.record_cable_id(change, "", "", log, kind=history.CORRECTION, jobs=fresh.summary())
        history.record_cable_id_jobs(fresh.cable, [j for j in fresh.jobs if j.outcome not in ("", "not ticked")],
                                     fresh.new, log)
        return fresh


@dataclass
class _FileChange:  # what history.record_cable_id reads
    cable: str
    old: str
    new: str
    file: Path
    error: str


def _change_job(client: StrataSyncClient, job: JobChange, plan: CableIdCorrection, log: Log) -> None:
    """Set one job's Cable ID; for an approved job, unapprove it first and approve it again after."""
    current = job.job
    if job.approved:
        try:
            reply = client.unapprove_job(job.workflow_id)
        except Exception as exc:  # recorded on the job and reported
            job.outcome, job.error = "failed", f"couldn't unapprove it: {exc}"
            log(f"Cable ID change: {job.work_order_id}: {job.error}")
            return
        log(f"Cable ID change: {job.work_order_id} unapproved")
        # The change is made from the searched job's attributes (the reply may lack them).
        current = {**job.job, "state": (reply or {}).get("state") or "COMPLETED"}
    try:
        updated = client.set_cable_id(current, plan.new)
    except Exception as exc:  # recorded on the job and reported
        job.outcome, job.error = "failed", str(exc)
        log(f"Cable ID change: couldn't set {job.work_order_id}'s Cable ID: {exc}")
        updated = None
    else:
        job.outcome = "changed"
        log(f"Cable ID change: {job.work_order_id}: {job.current or '(blank)'} -> {plan.new or '(blank)'}")
        changed = _differences(current, updated, ignore=("state",) if job.approved else ()) if updated else []
        if changed:
            plan.stopped = (f"{job.work_order_id} changed more than its Cable ID ({'; '.join(changed)}). "
                            "Check it in StrataSync before changing any more jobs.")
            log(f"Cable ID change: stopped: {plan.stopped}")
    if job.approved:
        try:
            client.approve_job(job.workflow_id)
            log(f"Cable ID change: {job.work_order_id} approved again")
        except Exception as exc:  # recorded on the job and reported
            note = f"couldn't approve it again ({exc}); it's left COMPLETED, approve it in StrataSync"
            job.error = "; ".join(filter(None, [job.error, note]))
            if job.outcome == "changed":
                job.outcome = "failed"
            log(f"Warning: {job.work_order_id}: {note}")


def _verify(client: StrataSyncClient, plan: CableIdCorrection, log: Log) -> None:
    """Look up every job that was sent a change, and check its Cable ID and that
    nothing else changed (approved jobs must be approved again)."""
    tried = [j for j in plan.jobs if j.outcome in ("changed", "failed")]
    if not tried:
        return
    try:
        now = {int((j.get("workflow") or {}).get("workflowId") or 0): j for j in _search(client)}
    except (StrataSyncError, ValueError) as exc:
        log(f"Cable ID change: couldn't look the jobs up again to check them: {exc}")
        return
    for job in tried:
        after = now.get(job.workflow_id)
        problems = []
        if after is None:
            problems.append("not found when looked up again")
        else:
            got = job_cable_id(after)
            if job.outcome == "changed" and got != plan.new:
                problems.append(f"its Cable ID is {got!r}")
            problems += _differences(job.job, after)
        if problems:
            if job.outcome == "changed":
                job.outcome = "failed"
            job.error = "; ".join(filter(None, [job.error, "checked afterwards: " + "; ".join(problems)]))
            log(f"Cable ID change: problem with {job.work_order_id}: {'; '.join(problems)}")
    log(f"Cable ID change: checked {len(tried)} job(s); {len(plan.changed)} changed and verified, "
        f"{len(plan.failed)} with problems")
