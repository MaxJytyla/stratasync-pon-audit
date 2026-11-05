"""The Cable ID that new jobs get, from a lookup file.

Every rack on one data hall row and channel is fed by one multi-drop cable, so
every rack-level job of that row and channel (C1, C2 and C3 alike) gets the
same Cable ID: the cable's base serial (e.g. "S7XXXXXXXX031"). Cohort 2 spans
both legs of the cable (.100A and .200A), which is why the leg serials aren't
used. F1 ports are fed by trunk fiber, whose serials aren't recorded, so F1
jobs get F1_CABLE_ID.

The lookup file is a CSV with one row per cable:

    Cable,Cable ID
    RMN2.DHA.01 AA,S7XXXXXXXX031
    RMN2.DHB.12 BB,

A blank Cable ID is allowed (a cable whose serial isn't known yet): its jobs
are created with the Cable ID left blank. A cable that isn't in the file at
all is treated the same way. Recording a cable replacement writes the new
cable's ID (or a blank) over the old one in the file. A Cable ID that was never
entered, or has a typo, is changed with cable_id_change.py, which also changes
it on the cable's jobs.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .pon_audit import SITE, F1Location, Location

F1_CABLE_ID = "N/A (Trunk Fiber)"
HEADER = ("Cable", "Cable ID")
FILE_NAME = "cable_ids.csv"

# (data hall letter, row, channel letter), e.g. ("A", 1, "A") for RMN2.DHA.01 AA
CableKey = Tuple[str, int, str]

_CABLE = re.compile(rf"^{SITE}\.DH([A-Z])\.(\d{{1,2}})\s+(AA|BB)$", re.IGNORECASE)


class CableIdFileError(ValueError):
    """The lookup file can't be read or has rows that don't make sense."""


def cable_name(key: CableKey) -> str:
    data_hall, row, channel = key
    return f"{SITE}.DH{data_hall}.{row:02d} {channel * 2}"


def parse_cable(text: str) -> Optional[CableKey]:
    m = _CABLE.match(text.strip())
    return (m.group(1).upper(), int(m.group(2)), m.group(3)[0].upper()) if m else None


def cable_of(loc: Location) -> CableKey:
    """The cable feeding a rack location, by data hall, row and channel."""
    channel = loc.channel if isinstance(loc, F1Location) else loc.side
    return (loc.data_hall, loc.row, channel)


@dataclass
class CableIds:
    """The lookup file's contents. `path` is None when no file is set up."""

    path: Optional[Path] = None
    ids: Dict[CableKey, str] = field(default_factory=dict)

    def for_location(self, loc: Location) -> str:
        """The Cable ID for a job holding `loc`; "" if it isn't known."""
        if isinstance(loc, F1Location):
            return F1_CABLE_ID
        return self.ids.get(cable_of(loc), "")

    def get(self, key: CableKey) -> str:
        return self.ids.get(key, "")

    def describe(self) -> str:
        if self.path is None:
            return "no Cable ID file"
        known = sum(1 for v in self.ids.values() if v)
        return f"{len(self.ids)} cable(s) in {self.path.name}, {len(self.ids) - known} with no Cable ID yet"

    def set(self, key: CableKey, cable_id: str) -> str:
        """Record `cable_id` (may be "") for a cable and save the file. Returns the old ID."""
        if self.path is None:
            raise CableIdFileError("No Cable ID file is set up.")
        old = self.ids.get(key, "")
        self.ids[key] = cable_id.strip()
        save(self)
        return old


def load(path: Optional[Path]) -> CableIds:
    """Read the lookup file. A missing file gives an empty lookup (every job
    gets a blank Cable ID), so the app works before the file exists."""
    if path is None:
        return CableIds()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return CableIds(path)
    except OSError as exc:
        raise CableIdFileError(f"Can't read the Cable ID file {path}: {exc}") from None
    return CableIds(path, parse(text, str(path)))


def parse(text: str, where: str = "the Cable ID file") -> Dict[CableKey, str]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {}
    header = [c.strip().lower() for c in rows[0]]
    if header[:2] != [h.lower() for h in HEADER]:
        raise CableIdFileError(f"{where} must start with the header row: {','.join(HEADER)}")
    ids: Dict[CableKey, str] = {}
    problems: List[str] = []
    for n, cells in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in cells):
            continue
        key = parse_cable(cells[0])
        if key is None:
            problems.append(f"line {n}: {cells[0]!r} isn't a cable like {SITE}.DHA.01 AA")
            continue
        if key in ids:
            problems.append(f"line {n}: {cable_name(key)} is listed twice")
            continue
        ids[key] = (cells[1] if len(cells) > 1 else "").strip()
    if problems:
        raise CableIdFileError(f"Problems in {where}: " + "; ".join(problems[:5])
                               + (f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""))
    return ids


def save(lookup: CableIds) -> None:
    """Write the file, sorted by cable; through a temporary file so a failure
    can't leave it half written."""
    path = lookup.path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for key in sorted(lookup.ids):
            w.writerow([cable_name(key), lookup.ids[key]])
    os.replace(tmp, path)
