"""Remembers how far test result downloads have got, so each run only fetches new results.

The watermark is the StrataSync *sync time* (when the server received a test),
not the test's creation time. A meter can run a test in the morning and sync
it hours later; filtering on creation time would miss that test forever once
the watermark had moved past it, while sync time only ever moves forward.

The watermark is the latest syncTime seen, taken from the server's own
timestamps so the local clock doesn't matter. The next run searches from that
time inclusive, and the IDs already processed at exactly that second are
remembered so they aren't downloaded twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def parse_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp from StrataSync (e.g. "2026-10-29T12:33:01Z")."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class SyncState:
    last_sync_time: Optional[datetime] = None
    ids_at_last_sync_time: List[int] = field(default_factory=list)
    last_run_at: Optional[datetime] = None
    last_run_results: int = 0

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            last_sync_time=parse_utc(data["last_sync_time"]) if data.get("last_sync_time") else None,
            ids_at_last_sync_time=list(data.get("ids_at_last_sync_time") or []),
            last_run_at=parse_utc(data["last_run_at"]) if data.get("last_run_at") else None,
            last_run_results=int(data.get("last_run_results") or 0),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {
            "last_sync_time": _fmt_utc(self.last_sync_time) if self.last_sync_time else None,
            "ids_at_last_sync_time": sorted(self.ids_at_last_sync_time),
            "last_run_at": _fmt_utc(self.last_run_at) if self.last_run_at else None,
            "last_run_results": self.last_run_results,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)  # atomic, so an interrupted run can't leave a half-written file

    def is_already_processed(self, record: Dict[str, Any]) -> bool:
        """True if this search result was handled by a previous run."""
        if self.last_sync_time is None or not record.get("syncTime"):
            return False
        synced = parse_utc(record["syncTime"])
        if synced < self.last_sync_time:
            return True
        return synced == self.last_sync_time and record.get("_id") in self.ids_at_last_sync_time

    def advance(self, records: Iterable[Dict[str, Any]], run_at: datetime) -> None:
        """Move the watermark forward to the newest syncTime among `records`."""
        records = [r for r in records if r.get("syncTime")]
        self.last_run_at = run_at
        self.last_run_results = len(records)
        if not records:
            return
        newest = max(parse_utc(r["syncTime"]) for r in records)
        ids_at_newest = [r["_id"] for r in records if parse_utc(r["syncTime"]) == newest]
        if self.last_sync_time is not None and newest == self.last_sync_time:
            ids_at_newest = sorted(set(self.ids_at_last_sync_time) | set(ids_at_newest))
        if self.last_sync_time is None or newest >= self.last_sync_time:
            self.last_sync_time = newest
            self.ids_at_last_sync_time = ids_at_newest
