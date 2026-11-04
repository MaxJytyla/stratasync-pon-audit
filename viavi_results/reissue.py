"""Re-issue StrataSync jobs for locations that need testing again.

Re-issuing waits for the job a location was tested in to be finished: once
StrataSync marks a job COMPLETED (every required location has a result; it
does so by itself, nobody has to approve it) or APPROVED, every one of its
locations is checked against the sheet, after this update's results are in it.
A location needs a retest when the sheet has no result for it tested on or
after the job's scheduled date (all of the job's tests of it were flagged, so
none were written), or, if that's turned on, when its light is weaker than a
given level. The sheet can also lack a result because no update has written
it yet (e.g. it synced before this app was first run), so such a location's
tests in the job are looked up in StrataSync before it's re-issued; if one
passed, the sheet is just behind and nothing is re-issued. A passing test earlier in the same job counts, so a stray later
test of the wrong port doesn't make a job. Low-light results are still written
to the sheet as before; re-issuing only adds a job.

Every update looks at the jobs finished in the last JOB_LOOKBACK days, so the
check doesn't depend on which update saw which flag, nor on the job's last
result being flagged. A flagged result's job is checked once it's finished
however old it is; until then the Jobs tab lists the location as waiting. A
flagged test that doesn't belong to any job is re-issued straight away.

The locations needing a retest are grouped by cohort, so a cohort gets one job
however many of its racks need retesting. Each job holds only those racks:

  * rack cohorts, per data hall row and channel: C1 = racks 1-15, C2 = 16-30,
    C3 = 31-40
  * F1: the three F1 ports of a row and channel

Jobs are named after the cohort's own job with " RT<n>" added, n counting up
from the highest RT number that cohort already has ("RMN2.DHA.12 AA C2 RT3").
A replaced cable gets new jobs for its rack cohorts, named the same way.

Each new job gets its cable's Cable ID from the lookup file (cable_ids.py):
the base serial of the row and channel's multi-drop cable for rack cohorts,
"N/A (Trunk Fiber)" for F1. A cable with no ID on file gets a blank one. The
Cable ID is set right after the job is created; if that fails, the job stays
(it's logged so the ID can be filled in by hand).

New jobs are made from the job template (RMN-BB-PON-Test by default), assigned
to no technician: they wait in StrataSync, in the STARTED state, until someone
assigns them. Each goes into the same StrataSync organization as the job it
re-issues (e.g. "BlackBox RMN"); without one, StrataSync would put it in the
account's top organization, where the team doesn't look for jobs. A cable
replacement's jobs go into the organization of that cable's earlier jobs.

A location is not re-issued when another job holding it is scheduled on or
after the finished job's date (or, for a test outside any job, the test's day):
it has already been re-issued (by an earlier update, or by hand), so every
update can look at the same finished jobs again without creating a job twice.

Two people running the app can find the same flagged results, since flagged
results aren't written to the sheet. So the existing jobs are searched again
right before each job is created, and locations someone else has re-issued in
the meantime are left out. If both still try to create the same job at once,
StrataSync refuses the second one (same name and date); that run then searches
again and creates only what's still missing, under the next RT number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .cable_ids import CableIds
from .pon_audit import (
    SITE,
    CellUpdate,
    F1Location,
    Location,
    RackLocation,
    TabIndex,
    TestReading,
    UpdatePlan,
    parse_location,
)

Log = Callable[[str], None]

# Flags a retest can fix. The others (no row or expected OLT in the sheet, an
# unrecognized label, ...) are problems with the sheet or the label, not the light.
RETEST_FLAG_KINDS = frozenset({"olt_mismatch", "no_olt_id", "no_reading", "odd_olt_id"})

# (name, first rack, last rack) of the rack-level cohorts on each row and channel.
COHORTS = (("C1", 1, 15), ("C2", 16, 30), ("C3", 31, 40))
F1_COHORT = "F1"

DEFAULT_TEMPLATE = "RMN-BB-PON-Test"
DEFAULT_LOW_LIGHT_RACK_DBM = -11.3
DEFAULT_LOW_LIGHT_F1_DBM = 2.9

# Existing jobs are searched from here on, to number new ones. The site's first
# jobs were scheduled on 2026-08-13.
JOBS_SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)

# Finished jobs changed within this long are checked on every update.
JOB_LOOKBACK = timedelta(days=14)
FINISHED_STATES = frozenset({"COMPLETED", "APPROVED"})
# A test location in one of these states has a result in its job.
TESTED_LOCATION_STATES = frozenset({"COMPLETED", "APPROVED"})

# jobManagement.createdBy of jobs made through the Jobs API.
API_CREATORS = frozenset({"ssWoApi", "ssWfApi"})

# workOrderId rules from the Jobs API guide.
_WORK_ORDER_ID = re.compile(r"^[A-Za-z0-9À-ÿ_-][A-Za-z0-9À-ÿ_ .-]{0,49}$")


@dataclass(frozen=True)
class ReissueSettings:
    enabled: bool = False
    low_light: bool = False
    rack_dbm: float = DEFAULT_LOW_LIGHT_RACK_DBM
    f1_dbm: float = DEFAULT_LOW_LIGHT_F1_DBM
    template: str = DEFAULT_TEMPLATE
    cable_ids_file: str = ""  # the Cable ID lookup file (see cable_ids.py); "" = the default one

    def threshold(self, loc: Location) -> float:
        return self.f1_dbm if isinstance(loc, F1Location) else self.rack_dbm


@dataclass(frozen=True)
class Retest:
    """One location that needs testing again, and why."""

    label: str  # in the standard form, e.g. "RMN2.DHA.12.05A"
    reason: str
    reading: Optional[TestReading] = None
    # A job holding this location scheduled on or after this day, other than
    # `own_job` (the failing test's), means it has already been re-issued.
    since: Optional[date] = None
    own_job: Optional[str] = None
    location_id: Optional[int] = None  # the location's workflowId in `own_job`, to look up its tests
    # Only a job that hasn't tested this location yet shows it's been re-issued
    # (a replaced cable: a test made before the replacement doesn't count).
    untested_only: bool = False


@dataclass
class PlannedJob:
    work_order_id: str
    cohort: str  # e.g. "RMN2.DHA row 12 channel A, C2"
    base: str  # the cohort's own job name, which work_order_id adds " RT<n>" to
    retests: List[Retest] = field(default_factory=list)
    org_id: Optional[int] = None  # the StrataSync organization to create it in
    workflow_id: Optional[int] = None  # set once created
    error: str = ""  # set if creating it failed
    cable_id: str = ""  # the Cable ID to give it; "" leaves it blank
    cable_id_error: str = ""  # set if the job was created but its Cable ID couldn't be set
    skipped: str = ""  # set if every location was re-issued by someone else first

    @property
    def labels(self) -> List[str]:
        return [r.label for r in self.retests]

    @property
    def status(self) -> str:
        if self.workflow_id is not None:
            return "created"
        if self.skipped:
            return "already re-issued"
        return "failed" if self.error else "planned"


@dataclass
class ReissuePlan:
    jobs: List[PlannedJob] = field(default_factory=list)
    already_reissued: List[Tuple[Retest, str]] = field(default_factory=list)  # (retest, existing job)
    not_reissued: List[Tuple[Retest, str]] = field(default_factory=list)  # (retest, why)
    waiting: List[Tuple[Retest, str]] = field(default_factory=list)  # (retest, job still being tested)
    sheet_behind: List[Tuple[str, str]] = field(default_factory=list)  # (label, job): passed, not in the sheet yet
    taken: Set[str] = field(default_factory=set)  # every job name in StrataSync, upper case

    @property
    def failures(self) -> List[PlannedJob]:
        return [j for j in self.jobs if j.error]


# ---- Locations and cohorts ------------------------------------------------------


def standard_label(loc: Location) -> str:
    """The label a new job gives a location, e.g. "RMN2.DHA.12.05A" or "RMN2.DHA.01.F1.02B"."""
    if isinstance(loc, F1Location):
        return f"{SITE}.DH{loc.data_hall}.{loc.row:02d}.F1.{loc.index:02d}{loc.channel}"
    return f"{SITE}.DH{loc.data_hall}.{loc.row:02d}.{loc.rack:02d}{loc.side}"


def cohort_of(loc: Location) -> Optional[str]:
    if isinstance(loc, F1Location):
        return F1_COHORT
    return next((name for name, first, last in COHORTS if first <= loc.rack <= last), None)


def _channel(loc: Location) -> str:
    return loc.channel if isinstance(loc, F1Location) else loc.side


def job_base(loc: Location, cohort: str) -> str:
    """The cohort's own job name, e.g. "RMN2.DHA.12 AA C2" or "RMN2.DHA.01 BB F1"."""
    return f"{SITE}.DH{loc.data_hall}.{loc.row:02d} {_channel(loc) * 2} {cohort}"


def _cohort_text(loc: Location, cohort: str) -> str:
    return f"{SITE}.DH{loc.data_hall} row {loc.row} channel {_channel(loc)}, {cohort}"


# ---- Existing jobs ------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingJob:
    work_order_id: str
    scheduled: Optional[date]
    locations: Tuple[Location, ...]
    state: str = ""
    scheduled_at: Optional[datetime] = None  # the scheduled date with its time zone
    modified: Optional[datetime] = None  # jobModifiedOn
    location_ids: Tuple[Tuple[Location, Optional[int]], ...] = ()  # each location's workflowId
    tested: int = 0
    total: int = 0
    org_id: Optional[int] = None
    created_by: str = ""  # jobManagement.createdBy: "ssJobMgr" (web Job Manager), "ssWoApi" (this app), ...
    tested_locations: FrozenSet[Location] = frozenset()  # locations with a result in this job

    @property
    def finished(self) -> bool:
        return self.state in FINISHED_STATES

    def progress(self) -> str:
        return f"{self.state or 'state unknown'}, {self.tested} of {self.total} tested"


def existing_jobs(jobs: Iterable[Mapping[str, Any]]) -> List[ExistingJob]:
    """The parts of Jobs API search results that re-issuing needs."""
    out = []
    for job in jobs:
        wf = job.get("workflow") or {}
        scheduled_at = _parse_dt(wf.get("date"))
        located, tested = [], set()
        for test in job.get("tests") or []:
            for loc in test.get("testLocations") or []:
                parsed = parse_location(loc.get("label") or "")
                if parsed:
                    located.append((parsed, loc.get("workflowId")))
                    if str(loc.get("state") or "").upper() in TESTED_LOCATION_STATES:
                        tested.add(parsed)
        stats = job.get("testStats") or {}
        out.append(ExistingJob(
            str(wf.get("workOrderId") or ""), scheduled_at.date() if scheduled_at else None,
            tuple(loc for loc, _ in located), str(job.get("state") or ""), scheduled_at,
            _parse_dt(job.get("jobModifiedOn")), tuple(located),
            int(stats.get("tested") or 0), int(stats.get("totalCnt") or 0), job.get("orgId"),
            str((wf.get("jobManagement") or {}).get("createdBy") or ""), frozenset(tested)))
    return out


def _parse_dt(value: Any) -> Optional[datetime]:
    """An ISO 8601 time from the API, which may be "…Z" with nanoseconds."""
    text = str(value or "").strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", text)
    if m:
        text = m.group(1) + (m.group(2) or "")[:7] + m.group(3)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def next_work_order_id(base: str, taken: Set[str]) -> str:
    """`base` + " RT<n>", n one more than the highest RT number `base` already has.

    Hand-made names count too: "… C2 RT" is RT1, "… C2RT3" is RT3.
    """
    pattern = re.compile(rf"^{re.escape(base.upper())}\s*RT\s*(\d*)$")
    numbers = [int(m.group(1) or 1) for name in taken for m in [pattern.match(name.upper())] if m]
    n = max(numbers, default=0) + 1
    while f"{base} RT{n}".upper() in taken:
        n += 1
    work_order_id = f"{base} RT{n}"
    if not _WORK_ORDER_ID.match(work_order_id):
        raise ValueError(f"{work_order_id!r} is not a valid StrataSync work order ID")
    return work_order_id


# ---- Finding retests ------------------------------------------------------------------


def find_retests(plan: UpdatePlan, settings: ReissueSettings) -> List[Retest]:
    """Locations from an update's plan that need a retest, one per location.

    Looks at the latest result of each location: flagged ones with a problem a
    retest can fix, and (if settings.low_light) written ones weaker than the
    threshold. plan_reissue decides what happens to them, mostly by checking
    the job each was tested in once it's finished.
    """
    found: Dict[str, Retest] = {}

    def add(reading: TestReading, reason: str) -> None:
        loc = reading.location
        label = standard_label(loc) if loc else reading.label.strip()
        found.setdefault(label, Retest(label, reason, reading, since=reading.timestamp.date(),
                                       own_job=reading.work_order_id))

    for flag in plan.flags:
        if flag.kind in RETEST_FLAG_KINDS and flag.reading is not None:
            add(flag.reading, flag.message)

    if settings.low_light:
        for reading in plan.written:
            loc = reading.location
            if loc is None or reading.power_dbm is None:
                continue
            limit = settings.threshold(loc)
            if reading.power_dbm < limit:
                add(reading, f"{reading.label}: {reading.power_dbm:g} dBm is weaker than {limit:g} dBm")
    return list(found.values())


# ---- Planning jobs ------------------------------------------------------------------------


def apply_updates(indexes: Mapping[str, TabIndex], updates: Iterable[CellUpdate]) -> None:
    """Put an update's cell values into the in-memory copy of the sheet, so
    finished jobs are checked against the sheet as it is after the update
    (a preview writes nothing, but must judge the same way)."""
    for u in updates:
        index = indexes.get(u.tab)
        if index is None:
            continue
        while len(index.values) < u.row:
            index.values.append([])
        row = index.values[u.row - 1]
        while len(row) < u.col:
            row.append("")
        row[u.col - 1] = u.value


def location_problem(loc: Location, since: datetime, indexes: Mapping[str, TabIndex],
                     settings: ReissueSettings) -> Optional[str]:
    """Why the sheet shows this location needs a retest after a job scheduled
    at `since`: "" if it doesn't, None if the sheet has no row for it."""
    index = indexes.get(loc.tab)
    row = index.rows.get(loc.key) if index else None
    if row is None:
        return None
    power = index.get(row, index.column(loc.columns.power))
    stamp = _parse_dt(index.get(row, index.column(loc.columns.timestamp)))
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.astimezone()
    # By day, in the job's time zone: jobs made by hand are scheduled at a time
    # of day, and tested from that day on.
    day = stamp.astimezone(since.tzinfo).date() if stamp is not None and since.tzinfo else None
    if day is None or day < since.date() or str(power if power is not None else "").strip() == "":
        return NO_RESULT.format(day=since.date())
    if settings.low_light:
        try:
            value = float(power)
        except (TypeError, ValueError):
            return ""
        limit = settings.threshold(loc)
        if value < limit:
            return (f"{standard_label(loc)}: {value:g} dBm (tested {stamp.isoformat(timespec='minutes')}) "
                    f"is weaker than {limit:g} dBm")
    return ""


NO_RESULT = "no passing result in the sheet since the job's date ({day})"

# Looks up the tests of locations the sheet has no passing result for, in their
# job (Retest.own_job / location_id). Returns, by label, None if one of them
# passed (the sheet is just behind) or why the location needs a retest.
Confirm = Callable[[List[Retest]], Dict[str, Optional[str]]]


def plan_reissue(retests: Sequence[Retest], jobs: Sequence[ExistingJob], indexes: Mapping[str, TabIndex],
                 settings: ReissueSettings, *, now: datetime, until: datetime,
                 confirm: Optional[Confirm] = None, cable_ids: Optional[CableIds] = None) -> ReissuePlan:
    """The jobs to re-issue in this update.

    `retests` are this update's flagged and low-light results (find_retests),
    `jobs` every job in StrataSync, `indexes` the sheet after this update. A
    finished job is checked if it changed in the last JOB_LOOKBACK days (and
    not after `until`, the end of this update's search, whose results the
    sheet may not have yet), or if one of `retests` was tested in it.
    `confirm` checks StrataSync for locations the sheet has no passing result
    for; without it, the sheet alone decides. Each job gets its cohort's
    Cable ID from `cable_ids` (blank without it, or if the file has none).
    """
    plan = ReissuePlan()
    groups: Dict[str, Tuple[str, List[Retest]]] = {}
    by_name = {j.work_order_id.strip().upper(): j for j in jobs}
    from_this_update = {r.label: r for r in retests}
    flagged_jobs = {(r.own_job or "").strip().upper() for r in retests}

    def add(retest: Retest, loc: Location) -> None:
        cohort = cohort_of(loc)
        if cohort is None:
            plan.not_reissued.append((retest, f"rack {loc.rack} isn't in a cohort (racks 1-40)"))
            return
        covering = _covering_job(loc, retest, jobs)
        if covering:
            plan.already_reissued.append((retest, covering))
            return
        groups.setdefault(job_base(loc, cohort), (_cohort_text(loc, cohort), []))[1].append(retest)

    # Finished jobs: every location, judged by the sheet.
    checked: Dict[str, str] = {}  # label -> the finished job it was checked in
    unconfirmed: List[Tuple[Retest, Location]] = []
    for job in sorted(jobs, key=lambda j: j.scheduled_at or now, reverse=True):
        if not (job.finished and job.scheduled_at and job.modified and job.modified <= until):
            continue
        if job.modified < now - JOB_LOOKBACK and job.work_order_id.strip().upper() not in flagged_jobs:
            continue
        for loc, location_id in job.location_ids:
            label = standard_label(loc)
            if label in checked:
                continue  # the newest finished job holding it decides
            checked[label] = job.work_order_id
            problem = location_problem(loc, job.scheduled_at, indexes, settings)
            if not problem:
                if problem is None:
                    flagged = from_this_update.get(label)
                    if flagged:
                        plan.not_reissued.append((flagged, "no row for it in the sheet"))
                elif label in from_this_update:
                    plan.not_reissued.append((from_this_update[label], (
                        f"the sheet has a passing result for it from {job.work_order_id}, "
                        "so this later test isn't re-issued")))
                continue
            flagged = from_this_update.get(label)
            retest = Retest(label, problem, flagged.reading if flagged else None,
                            since=job.scheduled_at.date(), own_job=job.work_order_id, location_id=location_id)
            if problem.startswith(NO_RESULT[:20]) and confirm:
                unconfirmed.append((retest, loc))
            else:
                add(retest, loc)

    # Locations the sheet has no passing result for: ask StrataSync.
    if unconfirmed:
        answers = confirm([r for r, _ in unconfirmed])
        for retest, loc in unconfirmed:
            why = answers.get(retest.label, retest.reason)
            if why is None:
                plan.sheet_behind.append((retest.label, retest.own_job or ""))
                continue
            add(Retest(retest.label, why, retest.reading, retest.since, retest.own_job, retest.location_id), loc)

    # This update's results whose job isn't finished, or that aren't in a job.
    for retest in retests:
        if retest.label in checked:
            continue
        loc = parse_location(retest.label)
        if loc is None:
            plan.not_reissued.append((retest, "location label not recognized"))
            continue
        job = by_name.get((retest.own_job or "").strip().upper())
        if job is None:
            add(retest, loc)  # not tested in a job, so there's nothing to wait for
        elif not job.finished or (job.modified and job.modified > until):
            plan.waiting.append((retest, f"{job.work_order_id} ({job.progress()})"))
        else:
            plan.not_reissued.append((retest, f"{job.work_order_id} doesn't list this location"))
    _add_jobs(plan, groups, jobs, lambda members: _lookup_cable_id(members, cable_ids))
    return plan


def _add_jobs(plan: ReissuePlan, groups: Mapping[str, Tuple[str, List[Retest]]],
              jobs: Sequence[ExistingJob], cable_id: Callable[[List[Retest]], str]) -> None:
    plan.taken = {j.work_order_id.upper() for j in jobs}
    for base, (cohort, members) in sorted(groups.items()):
        members.sort(key=lambda r: r.label)
        plan.jobs.append(PlannedJob(next_work_order_id(base, plan.taken), cohort, base, members,
                                    org_id=_organization(members, jobs), cable_id=cable_id(members)))


def _lookup_cable_id(members: Sequence[Retest], cable_ids: Optional[CableIds]) -> str:
    """A cohort job's Cable ID: its cable's, from the lookup file (every member
    of a cohort is on the same cable)."""
    loc = parse_location(members[0].label) if members else None
    return cable_ids.for_location(loc) if cable_ids and loc else ""


def _organization(members: Sequence[Retest], jobs: Sequence[ExistingJob]) -> Optional[int]:
    """The organization for a new job: that of the jobs its locations come from;
    else that of the newest job holding one of its locations (a cable
    replacement, or a test outside any job); else the one most of the site's
    jobs are in. None only if no job says (StrataSync then uses the root).

    Only jobs people made count: jobs made through the API (by this app before
    it set an organization) may be in the wrong one.
    """
    jobs = [j for j in jobs if j.created_by not in API_CREATORS]
    by_name = {j.work_order_id.strip().upper(): j for j in jobs}
    own = [by_name[r.own_job.strip().upper()].org_id for r in members
           if r.own_job and r.own_job.strip().upper() in by_name]
    own = [o for o in own if o]
    if own:
        return max(set(own), key=own.count)
    locations = {parse_location(r.label) for r in members}
    holding = [j for j in jobs if j.org_id and j.scheduled and locations & set(j.locations)]
    if holding:
        return max(holding, key=lambda j: j.scheduled).org_id
    site = [j.org_id for j in jobs if j.org_id and j.work_order_id.upper().startswith(SITE)]
    return max(set(site), key=site.count) if site else None


def _covering_job(loc: Location, retest: Retest, jobs: Sequence[ExistingJob]) -> str:
    """An existing job that shows `retest` has already been re-issued (see
    Retest.since), as "name (scheduled date)"; "" if none."""
    if retest.since is None:
        return ""
    own = (retest.own_job or "").strip().upper()
    for job in jobs:
        if (job.scheduled and job.scheduled >= retest.since and job.work_order_id.strip().upper() != own
                and loc in job.locations
                and not (retest.untested_only and loc in job.tested_locations)):
            return f"{job.work_order_id} (scheduled {job.scheduled})"
    return ""


def plan_cable_jobs(data_hall: str, row: int, channel: str, racks: Iterable[int], replaced_at: str,
                    jobs: Sequence[ExistingJob], cable_id: str = "") -> ReissuePlan:
    """New jobs for every rack cohort on a replaced cable (F1 isn't on it).

    Every job gets `cable_id`, the new cable's ID ("" leaves it blank).

    A rack is left out only if a job scheduled on or after the replacement day
    holds it and hasn't tested it yet, so recording the same replacement twice
    doesn't make two sets of jobs. A job that has already tested the rack
    doesn't count: that day's test may have been made on the old cable (the
    sheet's record of the rack is cleared regardless, see racks), and leaving
    the rack out would leave the new cable untested there.
    """
    replaced_day = datetime.fromisoformat(replaced_at).date()
    retests = [Retest(standard_label(RackLocation(data_hall, row, rack, channel)), f"cable replaced at {replaced_at}",
                      since=replaced_day, untested_only=True)
               for rack in sorted(racks)]
    plan = ReissuePlan()
    groups: Dict[str, Tuple[str, List[Retest]]] = {}
    for retest in retests:
        loc = parse_location(retest.label)
        cohort = cohort_of(loc)
        if cohort is None:
            plan.not_reissued.append((retest, f"rack {loc.rack} isn't in a cohort (racks 1-40)"))
            continue
        covering = _covering_job(loc, retest, jobs)
        if covering:
            plan.already_reissued.append((retest, covering))
            continue
        groups.setdefault(job_base(loc, cohort), (_cohort_text(loc, cohort), []))[1].append(retest)
    _add_jobs(plan, groups, jobs, lambda members: cable_id.strip())
    return plan


# ---- Creating jobs ---------------------------------------------------------------------------


def scheduled_today() -> datetime:
    """Midnight today in this computer's time zone, the scheduled date of new jobs.

    Always the same timestamp for a given day, so StrataSync refuses a second
    job with the same name on the same day (it keys jobs on name and date).
    """
    return datetime.combine(datetime.now().astimezone().date(), time.min).astimezone()


def create_jobs(client, plan: ReissuePlan, template: str, log: Log) -> None:
    """Create each planned job, recording its workflowId, or the error, on it.

    Right before each job, the jobs scheduled from today on are searched again,
    and locations someone else has re-issued since the plan was made are left
    out (and logged). If StrataSync refuses the job because one with that name
    and date was just created, the search is repeated and the rest is created
    under the next RT number. One failure doesn't stop the others.
    """
    scheduled = scheduled_today()
    for job in plan.jobs:
        for attempt in (1, 2, 3):
            try:
                recent = existing_jobs(client.search_jobs(scheduled - timedelta(days=1),
                                                          scheduled + timedelta(days=35)))
            except Exception as exc:  # recorded on the job and reported
                job.error = f"could not check for jobs re-issued by someone else: {exc}"
                log(f"Could not create job {job.work_order_id}: {job.error}")
                break
            if not _drop_reissued(job, plan, recent, log):
                break
            plan.taken |= {j.work_order_id.upper() for j in recent}
            if job.work_order_id.upper() in plan.taken:
                job.work_order_id = next_work_order_id(job.base, plan.taken)
            try:
                created = client.create_job_from_template(job.work_order_id, template, scheduled, job.labels,
                                                          org_id=job.org_id)
            except Exception as exc:  # recorded on the job and reported
                if attempt < 3 and "exists" in str(exc).lower():
                    log(f"Job {job.work_order_id} was created by someone else just now; checking again")
                    plan.taken.add(job.work_order_id.upper())
                    continue
                job.error = str(exc)
                log(f"Could not create job {job.work_order_id}: {exc}")
                break
            plan.taken.add(job.work_order_id.upper())
            job.workflow_id = (created.get("workflow") or {}).get("workflowId") or 0
            landed = created.get("orgId")
            log(f"Created job {job.work_order_id} ({len(job.labels)} location(s), workflowId {job.workflow_id}, "
                f"organization {landed})")
            if job.org_id and landed and int(landed) != int(job.org_id):
                log(f"Warning: {job.work_order_id} was meant for organization {job.org_id} but StrataSync put it "
                    f"in {landed}; move it in StrataSync")
            _set_cable_id(client, job, created, log)
            break


def _set_cable_id(client, job: PlannedJob, created: Mapping[str, Any], log: Log) -> None:
    """Give a just-created job its Cable ID. A failure doesn't undo the job (it
    exists and covers its locations); it's recorded on the job and logged so
    the Cable ID can be filled in by hand."""
    if not job.cable_id:
        log(f"{job.work_order_id}: no Cable ID on file for this cable, so it's left blank")
        return
    try:
        client.set_cable_id(dict(created), job.cable_id)
    except Exception as exc:  # recorded on the job and reported
        job.cable_id_error = str(exc)
        log(f"Warning: created {job.work_order_id} but couldn't set its Cable ID to {job.cable_id!r}: {exc}. "
            "Set it by hand in StrataSync.")
        return
    log(f"{job.work_order_id}: Cable ID set to {job.cable_id}")


def _drop_reissued(job: PlannedJob, plan: ReissuePlan, recent: Sequence[ExistingJob], log: Log) -> bool:
    """Leave out of `job` the locations that `recent` jobs already cover.
    Returns False if none are left."""
    keep = []
    for retest in job.retests:
        loc = parse_location(retest.label)
        covering = _covering_job(loc, retest, recent) if loc else ""
        if covering:
            log(f"{retest.label}: already re-issued in {covering}, probably by someone else; "
                f"not adding it to {job.work_order_id}")
            plan.already_reissued.append((retest, covering))
        else:
            keep.append(retest)
    job.retests = keep
    if not keep:
        job.skipped = "every location was already re-issued"
        log(f"Not creating job {job.work_order_id}: {job.skipped}")
    return bool(keep)
