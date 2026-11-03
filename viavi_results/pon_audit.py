"""Turn TruePON test result files into cell updates for the RMN2 PON Audit sheet.

Only five tabs are ever written:
  * "RMN2.DH{A,B,C,D} PON Audit" - rack-level tests, one row per (DH, Row, Rack).
    Test label "RMN2.DHA.12.25B" -> DH A, Row 12, Rack 25, channel BB (the "P2"
    columns; "P1" columns are channel AA).
  * "RMN2 F1 & F2" - F1 tests, one row per (DH, Row, Channel).
    Test label "RMN2.DHA.01.F1.02A" -> DH A, Row 1, Channel A, F1.02 columns.

For each test we write the downstream power and the test timestamp. The
"OLT and Port" / "OLT-ID" columns hold the *expected* OLT port and are never
overwritten; instead, the measured OLT ID is compared against them. A result
with any problem (wrong OLT ID, no reading, ...) is not written at all, only
flagged; the sheet's Issue columns are left alone.

OLT IDs arrive as four decimal integers (oltIdFormat "hex"). Each is shown as
two hex digits: [1, 23, 170, 1] -> "01 17 AA 01" = uOLT 17, channel A, port 01.
Those hex digits are then read literally: uOLT and port as decimal numbers,
the channel as a repeated letter.

Everything here is pure logic: sheets are passed in as lists of rows (as
returned by gspread or read from an .xlsx), and the output is a plan of cell
updates that a writer can apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

SITE = "RMN2"
DATA_HALLS = ("A", "B", "C", "D")
F1_TAB = "RMN2 F1 & F2"

# The audited network is XGS-PON. A meter set up to test several PON types at
# once (signalSetup "gpon;xgspon") reports one result per type; only the
# XGS-PON one is used.
PON_SERVICE = "xgspon"


def rack_tab(data_hall: str) -> str:
    return f"{SITE}.DH{data_hall} PON Audit"


AUDIT_TABS = tuple(rack_tab(dh) for dh in DATA_HALLS) + (F1_TAB,)


@dataclass(frozen=True)
class Columns:
    """Header names for the cells one test writes to. Matched after collapsing
    whitespace (the sheet has "OLT and Port  (B)" with two spaces)."""

    power: str
    expected_olt: str
    timestamp: str


RACK_KEY_HEADERS = ("DH", "Row", "Rack")
RACK_COLUMNS = {
    # label suffix A = channel AA = "P1"; B = channel BB = "P2"
    "A": Columns("dBm in Port 1", "OLT and Port (A)", "Timestamp P1"),
    "B": Columns("dBm in Port 2", "OLT and Port (B)", "Timestamp P2"),
}
F1_KEY_HEADERS = ("DH", "Row", "Channel")
F1_COLUMNS = {
    1: Columns("F1.01", "OLT-ID 1", "Timestamp F1.01"),
    2: Columns("F1.02", "OLT-ID 2", "Timestamp F1.02"),
    3: Columns("F1.03", "OLT-ID 3", "Timestamp F1.03"),
}

_RACK_LABEL = re.compile(rf"^{SITE}\.DH([A-D])\.(\d+)\.(\d+)([AB])$")
_F1_LABEL = re.compile(rf"^{SITE}\.DH([A-D])\.(\d+)\.F1\.0?([1-3])([AB])$")


# ---- OLT IDs ----------------------------------------------------------------


@dataclass(frozen=True)
class OltId:
    raw: Tuple[int, int, int, int]

    @property
    def hex_parts(self) -> Tuple[str, str, str, str]:
        return tuple(f"{v:02X}" for v in self.raw)  # type: ignore[return-value]

    @property
    def sheet_format(self) -> str:
        """The form used in the sheet, e.g. "01 17 AA 01"."""
        return " ".join(self.hex_parts)

    @property
    def is_blank(self) -> bool:
        """True for [0, 0, 0, 0], which the meter reports when no OLT ID was read."""
        return not any(self.raw)

    @property
    def uolt(self) -> Optional[int]:
        return _hex_digits_as_decimal(self.hex_parts[1])

    @property
    def channel(self) -> Optional[str]:
        ch = self.hex_parts[2]
        return ch[0] if ch[0] == ch[1] and ch[0] in "ABCDEF" else None

    @property
    def port(self) -> Optional[int]:
        return _hex_digits_as_decimal(self.hex_parts[3])

    @property
    def is_well_formed(self) -> bool:
        return (
            self.raw[0] == 1
            and self.uolt is not None
            and self.channel is not None
            and self.port is not None
        )

    def describe(self) -> str:
        if self.is_blank:
            return "no OLT ID"
        if not self.is_well_formed:
            return f"unrecognized OLT ID {self.sheet_format}"
        return f"oltna{self.uolt:02d} channel {self.channel} port {self.port}"


def _hex_digits_as_decimal(hex_str: str) -> Optional[int]:
    return int(hex_str) if hex_str.isdigit() else None


def decode_olt_id(value: Optional[Sequence[int]]) -> Optional[OltId]:
    if value is None:
        return None
    if len(value) != 4 or not all(isinstance(v, int) and 0 <= v <= 255 for v in value):
        raise ValueError(f"OLT ID must be four integers 0-255, got {value!r}")
    return OltId(tuple(value))  # type: ignore[arg-type]


# ---- Test locations -----------------------------------------------------------


@dataclass(frozen=True)
class RackLocation:
    data_hall: str
    row: int
    rack: int
    side: str  # "A" (channel AA, P1) or "B" (channel BB, P2)

    @property
    def tab(self) -> str:
        return rack_tab(self.data_hall)

    @property
    def key(self) -> Tuple[str, int, Union[int, str]]:
        return (self.data_hall, self.row, self.rack)

    @property
    def columns(self) -> Columns:
        return RACK_COLUMNS[self.side]


@dataclass(frozen=True)
class F1Location:
    data_hall: str
    row: int
    channel: str  # "A" or "B"
    index: int  # 1-3, from F1.01 / F1.02 / F1.03

    @property
    def tab(self) -> str:
        return F1_TAB

    @property
    def key(self) -> Tuple[str, int, Union[int, str]]:
        return (self.data_hall, self.row, self.channel)

    @property
    def columns(self) -> Columns:
        return F1_COLUMNS[self.index]


Location = Union[RackLocation, F1Location]


def parse_location(label: str) -> Optional[Location]:
    """Map a test location label to where it belongs, or None if unrecognized."""
    label = label.strip()
    m = _RACK_LABEL.match(label)
    if m:
        return RackLocation(m.group(1), int(m.group(2)), int(m.group(3)), m.group(4))
    m = _F1_LABEL.match(label)
    if m:
        return F1Location(m.group(1), int(m.group(2)), m.group(4), int(m.group(3)))
    return None


# ---- Test result files ------------------------------------------------------------


@dataclass(frozen=True)
class TestReading:
    label: str
    test_time: str  # ISO 8601 with offset, as reported by the meter
    power_dbm: Optional[float]
    olt_id: Optional[OltId]
    signal_status: Optional[str]
    loss_db: Optional[float]
    tech_id: Optional[str]
    tech_name: Optional[str]
    serial_no: Optional[str]
    work_order_id: Optional[str]
    workflow_id: Optional[int]
    source: Optional[str] = None  # file name or StrataSync ID, for traceability
    sync_time: Optional[str] = None  # when StrataSync received it, e.g. "2026-10-29T12:33:01Z"

    @property
    def location(self) -> Optional[Location]:
        return parse_location(self.label)

    @property
    def uses_sync_time(self) -> bool:
        """True when the meter's test time can't be trusted and the sync time is used.

        Meters whose clock has been reset report test times years in the past
        (e.g. 2020). A test time more than STALE_TEST_TIME before the sync time
        is treated as such; otherwise the test time is used, because it is when
        the light was actually measured (a meter can sync hours later).
        """
        synced = _parse_time(_utc_z(self.sync_time))
        if synced is None:
            return False
        tested = _parse_time(self.test_time)
        return tested is None or tested < synced - STALE_TEST_TIME

    @property
    def sheet_time(self) -> str:
        """The test's time as written to the sheet and compared with it:
        the test time, or the sync time if the test time is unreliable (in the
        meter's time zone where known, so the column stays consistent)."""
        if not self.uses_sync_time:
            return self.test_time
        synced = _parse_time(_utc_z(self.sync_time))
        tested = _parse_time(self.test_time)
        return synced.astimezone(tested.tzinfo if tested else None).isoformat(timespec="seconds")

    @property
    def timestamp(self) -> datetime:
        stamp = _parse_time(self.sheet_time)
        if stamp is None or stamp.tzinfo is None:
            raise ValueError(f"{self.label or self.source}: no usable test time")
        return stamp


STALE_TEST_TIME = timedelta(days=5 * 365)


def _utc_z(value: Optional[str]) -> Optional[str]:
    return value.replace("Z", "+00:00") if value else value


def readings_from_result(
    doc: Mapping[str, Any], source: Optional[str] = None, sync_time: Optional[str] = None
) -> List[TestReading]:
    """Extract TruePON readings from one test result document (.wfl.json).

    The file doesn't record when StrataSync received it; pass the search
    result's syncTime so an unreliable test time can be replaced (see
    TestReading.uses_sync_time).
    """
    asset = doc.get("assetInfo") or {}
    workflow = doc.get("workflow") or {}
    tech = workflow.get("techInfo") or {}
    tech_name = " ".join(p for p in (tech.get("firstName"), tech.get("lastName")) if p) or None

    readings = []
    for test in doc.get("tests") or []:
        if test.get("type") != "truepon":
            continue
        config = test.get("configuration") or {}
        signal = _pick_service(config.get("signalSetup"), source)
        signal_config = config.get(signal) or {}
        if signal_config.get("oltIdFormat", "hex") != "hex":
            raise ValueError(f"Unexpected oltIdFormat {signal_config.get('oltIdFormat')!r} in {source}")
        results = test.get("results") or {}
        data = (results.get("data") or {}).get(signal) or {}

        for loc in test.get("testLocations") or [{}]:
            readings.append(
                TestReading(
                    label=loc.get("label", ""),
                    test_time=results.get("testTime", ""),
                    power_dbm=data.get("powerDownstream"),
                    olt_id=decode_olt_id(data.get("oltId")),
                    signal_status=data.get("powerDownstreamSignalStatus"),
                    loss_db=data.get("loss"),
                    tech_id=tech.get("techId") or asset.get("techId"),
                    tech_name=tech_name,
                    serial_no=asset.get("serialNo"),
                    work_order_id=workflow.get("workOrderId"),
                    workflow_id=loc.get("workflowId", workflow.get("workflowId")),
                    source=source,
                    sync_time=sync_time,
                )
            )
    return readings


def _pick_service(signal_setup: Optional[str], source: Optional[str]) -> Optional[str]:
    """The signalSetup key holding the result to use (see PON_SERVICE)."""
    services = (signal_setup or "").split(";")
    if len(services) == 1:
        return signal_setup
    if PON_SERVICE in services:
        return PON_SERVICE
    raise ValueError(f"Test set up for {signal_setup!r}, which doesn't include {PON_SERVICE}, in {source}")


def is_multi_service(record: Mapping[str, Any]) -> bool:
    """True for a search result from a test of several PON types at once. Its
    power, loss and OLT ID fields join one value per type with ";", so it is
    read from the full file instead (see reading_from_search)."""
    return ";" in str(record.get("ponServices") or "")


def reading_from_search(record: Mapping[str, Any]) -> TestReading:
    """Build a reading from a StrataSync search result, without downloading the file.

    The search response carries the same values as the file (checked against
    636 downloaded files): power and loss as strings, the OLT ID already as
    hex text ("01 21 AA 09"), and the test time in the meter's time zone. It
    does not include the signal status, so `signal_status` is None.

    Raises ValueError for a multi-service record (see is_multi_service).
    """
    if is_multi_service(record):
        raise ValueError(f"{record.get('name')} tested {record.get('ponServices')!r}; read the full file")
    tech_name = " ".join(p for p in (record.get("userFirstName"), record.get("userLastName")) if p)
    return TestReading(
        label=record.get("testLocationsLabel") or "",
        test_time=record.get("assetLocalTestTime") or "",
        power_dbm=_float_or_none(record.get("power")),
        olt_id=_olt_from_hex_text(record.get("oltId")),
        signal_status=None,
        loss_db=_float_or_none(record.get("loss")),
        tech_id=record.get("userTechId"),
        tech_name=tech_name or None,
        serial_no=record.get("serialNo"),
        work_order_id=record.get("workOrderId"),
        workflow_id=record.get("workflowId"),
        source=f"{record.get('name')} (id {record.get('_id')})",
        sync_time=record.get("syncTime"),
    )


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _olt_from_hex_text(value: Any) -> Optional[OltId]:
    if value is None or not str(value).strip():
        return None
    parts = str(value).split()
    return decode_olt_id([int(p, 16) for p in parts])


# ---- Sheet indexing ---------------------------------------------------------------


def _norm_header(value: Any) -> str:
    return " ".join(str(value or "").split())


def _norm_key_part(value: Any) -> Union[int, str, None]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        num = float(text)
    except ValueError:
        return text.upper()
    return int(num) if num.is_integer() else text


@dataclass
class TabIndex:
    """Lookup of rows and columns in one tab. Rows/columns are 1-based like A1 notation."""

    title: str
    columns: Dict[str, int]
    rows: Dict[Tuple[Any, ...], int]
    values: List[List[Any]] = field(repr=False)

    @classmethod
    def build(cls, title: str, values: List[List[Any]], key_headers: Sequence[str]) -> "TabIndex":
        if not values:
            raise ValueError(f"Tab {title!r} is empty")
        columns = {}
        for i, h in enumerate(values[0]):
            name = _norm_header(h)
            if name and name not in columns:
                columns[name] = i + 1
        missing = [h for h in key_headers if h not in columns]
        if missing:
            raise ValueError(f"Tab {title!r} is missing key columns {missing}")

        rows: Dict[Tuple[Any, ...], int] = {}
        key_cols = [columns[h] for h in key_headers]
        for r, row in enumerate(values[1:], start=2):
            key = tuple(_norm_key_part(row[c - 1] if c - 1 < len(row) else None) for c in key_cols)
            if None in key:
                continue
            if key in rows:
                raise ValueError(f"Tab {title!r} has duplicate key {key} (rows {rows[key]} and {r})")
            rows[key] = r
        return cls(title, columns, rows, values)

    def column(self, header: str) -> int:
        try:
            return self.columns[_norm_header(header)]
        except KeyError:
            raise ValueError(f"Tab {self.title!r} has no column {header!r}") from None

    def get(self, row: int, col: int) -> Any:
        if row - 1 < len(self.values) and col - 1 < len(self.values[row - 1]):
            return self.values[row - 1][col - 1]
        return None


def build_indexes(tabs: Mapping[str, List[List[Any]]]) -> Dict[str, TabIndex]:
    """Index the audit tabs. `tabs` maps tab title -> all cell values (rows of lists)."""
    indexes = {}
    for title in AUDIT_TABS:
        if title not in tabs:
            continue
        keys = F1_KEY_HEADERS if title == F1_TAB else RACK_KEY_HEADERS
        indexes[title] = TabIndex.build(title, tabs[title], keys)
    return indexes


# ---- Problems with a reading ---------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    """Something a person should look at.

    `kind` is a short machine-readable tag and `message` is the full explanation.
    """

    kind: str
    message: str
    reading: Optional[TestReading] = None


def reading_problems(reading: TestReading, expected: Any) -> List[Flag]:
    """What's wrong with a reading, given the sheet's expected OLT ID for its location."""
    return _reading_problems(reading, expected)


def _reading_problems(reading: TestReading, expected: Any) -> List[Flag]:
    expected_str = " ".join(str(expected or "").split()).upper()
    olt = reading.olt_id
    label = reading.label
    flags = []

    if reading.power_dbm is None:
        status = reading.signal_status or "unknown"
        flags.append(Flag("no_reading", f"{label}: no power reading (signal status {status!r})", reading))

    if olt is None or olt.is_blank:
        flags.append(Flag("no_olt_id", f"{label}: meter reported no OLT ID (expected {expected_str or '?'})", reading))
    elif not expected_str:
        flags.append(Flag(
            "no_expected_olt",
            f"{label}: measured {olt.sheet_format} but the sheet has no expected OLT",
            reading,
        ))
    elif olt.sheet_format != expected_str:
        flags.append(Flag(
            "olt_mismatch",
            f"{label}: measured {olt.sheet_format} ({olt.describe()}), expected {expected_str}",
            reading,
        ))
    elif not olt.is_well_formed:
        flags.append(Flag("odd_olt_id", f"{label}: {olt.describe()}", reading))
    return flags


# ---- Planning updates ---------------------------------------------------------------


@dataclass(frozen=True)
class CellUpdate:
    tab: str
    row: int
    col: int
    value: Any

    @property
    def a1(self) -> str:
        return f"{_col_letter(self.col)}{self.row}"


@dataclass
class UpdatePlan:
    updates: List[CellUpdate] = field(default_factory=list)
    flags: List[Flag] = field(default_factory=list)
    written: List[TestReading] = field(default_factory=list)
    skipped: List[Tuple[TestReading, str]] = field(default_factory=list)

    def updates_by_tab(self) -> Dict[str, List[CellUpdate]]:
        out: Dict[str, List[CellUpdate]] = {}
        for u in self.updates:
            out.setdefault(u.tab, []).append(u)
        return out

    def written_cells(self) -> List[Tuple[TestReading, CellUpdate, CellUpdate]]:
        """Each written reading with its (power, timestamp) cell updates."""
        return list(zip(self.written, self.updates[0::2], self.updates[1::2]))


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def plan_updates(
    readings: Iterable[TestReading],
    indexes: Mapping[str, TabIndex],
    *,
    overwrite_newer: bool = False,
) -> UpdatePlan:
    """Work out which cells to write for a batch of readings.

    * When a location was tested more than once, the latest test wins.
    * A reading is skipped when the sheet already has a result for that
      location with the same or a later timestamp (unless overwrite_newer).
      This includes a cable replacement's timestamp (see plan_cable_replacement).
    * A reading with any problem (OLT mismatch, no OLT ID, no power reading,
      ...) is not written at all, only flagged, so nothing questionable
      reaches the sheet. Issue columns are never touched.
    """
    plan = UpdatePlan()

    latest: Dict[Tuple[str, Tuple[Any, ...], Columns], TestReading] = {}
    for reading in readings:
        loc = reading.location
        if loc is None:
            plan.skipped.append((reading, "label not recognized"))
            plan.flags.append(Flag("unknown_label", f"Unrecognized test label {reading.label!r}", reading))
            continue
        try:
            reading.timestamp
        except ValueError as exc:
            plan.skipped.append((reading, "no usable test time"))
            plan.flags.append(Flag("no_test_time", str(exc), reading))
            continue
        slot = (loc.tab, loc.key, loc.columns)
        current = latest.get(slot)
        if current is None or reading.timestamp > current.timestamp:
            if current is not None:
                plan.skipped.append((current, "superseded by a later test in this batch"))
            latest[slot] = reading
        else:
            plan.skipped.append((reading, "superseded by a later test in this batch"))

    for (tab, key, cols), reading in sorted(latest.items(), key=lambda kv: kv[1].timestamp):
        index = indexes.get(tab)
        if index is None:
            plan.skipped.append((reading, "tab not available"))
            plan.flags.append(Flag("missing_tab", f"{reading.label}: tab {tab!r} not found", reading))
            continue
        row = index.rows.get(key)
        if row is None:
            plan.skipped.append((reading, "no matching row"))
            plan.flags.append(Flag("no_row", f"{reading.label}: no row for {key} in {tab!r}", reading))
            continue

        existing_time = _parse_time(index.get(row, index.column(cols.timestamp)))
        if existing_time and existing_time >= reading.timestamp and not overwrite_newer:
            if _is_blank(index.get(row, index.column(cols.power))):
                why = f"tested before cable replacement ({index.get(row, index.column(cols.timestamp))})"
            else:
                why = "sheet already has this or a newer result"
            plan.skipped.append((reading, why))
            continue

        problems = _reading_problems(reading, index.get(row, index.column(cols.expected_olt)))
        if problems:
            plan.flags.extend(problems)
            plan.skipped.append((reading, "flagged (not written)"))
            continue

        # Two updates per written reading, power then timestamp (see written_cells).
        plan.updates.append(CellUpdate(tab, row, index.column(cols.power), reading.power_dbm))
        plan.updates.append(CellUpdate(tab, row, index.column(cols.timestamp), reading.sheet_time))
        plan.written.append(reading)

    return plan


# ---- Cable replacement ---------------------------------------------------------------
#
# All the racks on one row and channel of a data hall are fed by one multi-drop
# cable. When it is replaced, its results are cleared from the sheet: each rack's
# dBm is blanked and its timestamp set to the replacement time. That timestamp
# is the only record of the replacement, and plan_updates' "sheet already has
# a newer result" rule then ignores every test from before it, while the first
# test after it is written as normal. (A timestamp with a blank dBm can only
# come from a replacement, since flagged results aren't written.)
#
# The comparison is against the test time (see TestReading.sheet_time), so a
# test made on the old cable but synced after the replacement is still ignored.
# Only a test time that is clearly wrong (a reset meter clock) falls back to
# the sync time.


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


@dataclass(frozen=True)
class RackClearing:
    rack: int
    row: int  # sheet row
    old_power: Any
    old_timestamp: Any
    cleared: bool  # False when the rack was already tested after the replacement
    reason: str = ""


@dataclass
class CableReplacementPlan:
    tab: str
    data_hall: str
    row: int
    channel: str
    replaced_at: str  # ISO 8601 with offset, as written to the sheet
    racks: List[RackClearing] = field(default_factory=list)
    updates: List[CellUpdate] = field(default_factory=list)

    @property
    def cable(self) -> str:
        return f"{SITE}.DH{self.data_hall} row {self.row} channel {self.channel}"


def plan_cable_replacement(
    index: TabIndex, data_hall: str, row: int, channel: str, replaced_at: datetime
) -> CableReplacementPlan:
    """Cell updates that clear one cable's results and record its replacement time.

    `index` is the data hall's audit tab. A rack already tested after the
    replacement time is left alone, so a result from the new cable isn't lost;
    so is one with a later replacement already recorded, so that protection
    isn't weakened.
    """
    if channel not in RACK_COLUMNS:
        raise ValueError(f"Channel must be A or B, not {channel!r}")
    if replaced_at.tzinfo is None:
        raise ValueError("The replacement time must include a time zone")
    cols = RACK_COLUMNS[channel]
    power_col, time_col = index.column(cols.power), index.column(cols.timestamp)
    stamp = replaced_at.isoformat(timespec="seconds")
    plan = CableReplacementPlan(index.title, data_hall, row, channel, stamp)

    racks = sorted(((key[2], r) for key, r in index.rows.items() if key[0] == data_hall and key[1] == row),
                   key=lambda kr: kr[1])
    if not racks:
        raise ValueError(f"Tab {index.title!r} has no racks on row {row}")
    for rack, r in racks:
        old_power, old_time = index.get(r, power_col), index.get(r, time_col)
        existing = _parse_time(old_time)
        if existing and existing > replaced_at:
            reason = ("a later replacement is already recorded" if _is_blank(old_power)
                      else "already tested after the replacement")
            plan.racks.append(RackClearing(rack, r, old_power, old_time, False, reason))
            continue
        plan.racks.append(RackClearing(rack, r, old_power, old_time, True))
        plan.updates.append(CellUpdate(index.title, r, power_col, ""))
        plan.updates.append(CellUpdate(index.title, r, time_col, stamp))
    return plan


def _col_letter(col: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return letters
