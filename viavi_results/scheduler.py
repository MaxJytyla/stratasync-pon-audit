"""Automatic updates: run "Run update" on a timer, with the settings saved in the window.

The OS scheduler starts the app as `<app> --auto-update` every N minutes:

  macOS    a LaunchAgent, ~/Library/LaunchAgents/com.stratasync.ponaudit.update.plist
  Windows  a Task Scheduler task, "StrataSync PON Audit update"

Both run as the signed-in user, only while they're signed in, and catch up on a
missed run after the computer wakes. `run_automatic` does one update with no
window; its outcome goes to the update history (history.py).
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from . import history
from .config import APP_NAME, ConfigError, SavedForm, make_settings, make_stratasync_settings

AUTO_UPDATE_ARG = "--auto-update"
LAUNCHD_LABEL = "com.stratasync.ponaudit.update"
WINDOWS_TASK = f"{APP_NAME} update"
MIN_MINUTES = 5
MAX_MINUTES = 7 * 24 * 60


class ScheduleError(ConfigError):
    """Raised when automatic updates can't be turned on or off."""


@dataclass(frozen=True)
class Schedule:
    minutes: int
    command: List[str]

    @property
    def is_current_app(self) -> bool:
        """False when the schedule starts a different copy of the app (e.g. an older download)."""
        return self.command == command()


def supported() -> bool:
    return sys.platform in ("darwin", "win32")


def command() -> List[str]:
    """How the OS scheduler should start an automatic update of this copy of the app."""
    if getattr(sys, "frozen", False):  # the PyInstaller build
        return [sys.executable, AUTO_UPDATE_ARG]
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")  # no console window
    return [str(exe), str(Path(__file__).resolve().parent.parent / "app.py"), AUTO_UPDATE_ARG]


def describe(minutes: int) -> str:
    if minutes % 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    return "1 hour" if hours == 1 else f"{hours} hours"


def current() -> Optional[Schedule]:
    """The installed schedule, or None if automatic updates are off."""
    if sys.platform == "darwin":
        return _mac_current()
    if sys.platform == "win32":
        return _win_current()
    return None


def install(minutes: int) -> None:
    """Turn on automatic updates every `minutes`, replacing any earlier schedule."""
    if not supported():
        raise ScheduleError("Automatic updates are only available on Windows and macOS.")
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise ScheduleError(f"Choose an interval between {MIN_MINUTES} minutes and {MAX_MINUTES // 1440} days.")
    if "/AppTranslocation/" in sys.executable:
        raise ScheduleError("macOS is running the app from a temporary location. Move "
                            f"{APP_NAME}.app to Applications, open it from there, and try again.")
    if sys.platform == "darwin":
        _mac_install(minutes)
    else:
        _win_install(minutes)


def remove() -> None:
    """Turn off automatic updates (does nothing if they're already off)."""
    if sys.platform == "darwin":
        _mac_remove()
    elif sys.platform == "win32":
        _win_remove()


def _run(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---- macOS: launchd ------------------------------------------------------------


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _mac_current() -> Optional[Schedule]:
    try:
        with _plist_path().open("rb") as fh:
            data = plistlib.load(fh)
        return Schedule(int(data["StartInterval"]) // 60, list(data["ProgramArguments"]))
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
        return None


def _mac_install(minutes: int) -> None:
    domain = f"gui/{os.getuid()}"
    out = history.history_dir() / "launchd.log"  # anything the app prints outside its own log
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        plistlib.dump({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": command(),
            "StartInterval": minutes * 60,
            "RunAtLoad": False,
            "ProcessType": "Background",
            "StandardOutPath": str(out),
            "StandardErrorPath": str(out),
        }, fh)
    for attempt in range(5):  # bootstrap can fail briefly while the old job is still being removed
        proc = _run(["launchctl", "bootstrap", domain, str(path)])
        if proc.returncode == 0:
            return
        time.sleep(0.5 * (attempt + 1))
    path.unlink(missing_ok=True)
    raise ScheduleError(f"launchctl could not schedule the update: {(proc.stderr or proc.stdout).strip()}")


def _mac_remove() -> None:
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
    _plist_path().unlink(missing_ok=True)


# ---- Windows: Task Scheduler ----------------------------------------------------

_TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _win_task_xml(minutes: int) -> str:
    cmd = command()
    start = (datetime.now() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Writes new StrataSync TruePON results to the PON Audit sheet. Set up from the {escape(APP_NAME)} app.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start.isoformat()}</StartBoundary>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(cmd[0])}</Command>
      <Arguments>{escape(subprocess.list2cmdline(cmd[1:]))}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _parse_duration(value: str) -> int:
    """Minutes in a Task Scheduler duration such as PT15M, PT2H or P1D."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?)?", value.strip())
    if not m:
        raise ValueError(value)
    days, hours, mins = (int(g or 0) for g in m.groups())
    return days * 1440 + hours * 60 + mins


def _win_current() -> Optional[Schedule]:
    proc = _run(["schtasks", "/Query", "/TN", WINDOWS_TASK, "/XML"])
    if proc.returncode != 0:
        return None
    try:
        root = ElementTree.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", proc.stdout))
        interval = root.findtext(".//t:Repetition/t:Interval", namespaces=_TASK_NS) or ""
        exe = root.findtext(".//t:Exec/t:Command", namespaces=_TASK_NS) or ""
        args = root.findtext(".//t:Exec/t:Arguments", namespaces=_TASK_NS) or ""
        return Schedule(_parse_duration(interval), [exe.strip('"')] + _split_args(args))
    except (ElementTree.ParseError, ValueError):
        return Schedule(0, [])


def _split_args(args: str) -> List[str]:
    # Only ever our own arguments: an optional quoted script path, then the flag.
    return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', args)]


def _win_install(minutes: int) -> None:
    fd, name = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(_win_task_xml(minutes).encode("utf-16"))  # schtasks wants UTF-16 with a BOM
        proc = _run(["schtasks", "/Create", "/TN", WINDOWS_TASK, "/XML", name, "/F"])
    finally:
        os.unlink(name)
    if proc.returncode != 0:
        raise ScheduleError(f"Task Scheduler could not schedule the update: {(proc.stderr or proc.stdout).strip()}")


def _win_remove() -> None:
    if _win_current() is None:
        return
    proc = _run(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"])
    if proc.returncode != 0:
        raise ScheduleError(f"Task Scheduler could not remove the update: {(proc.stderr or proc.stdout).strip()}")


# ---- the automatic update itself ---------------------------------------------------


def run_automatic() -> int:
    """One update with the saved settings and no window. Returns the process exit code."""
    from .sync import RunInProgress, error_text, run_sync  # keep `--auto-update` startup light

    log = history.automatic_log()
    log("Automatic update started")
    form = SavedForm.load()
    try:
        ss = make_stratasync_settings(form.region, form.client_id, form.client_secret)
        settings = make_settings(form.sheet_id, form.service_account_json)
    except ConfigError as exc:
        message = f"The saved settings are incomplete: {exc}"
        if not form.remember_secrets:
            message += (" Automatic updates need 'Remember client secret and Google key on this "
                        "computer' ticked in the app.")
        log(message)
        history.record_status(history.AUTOMATIC, "failed", form.sheet_id, message, log)
        return 1
    try:
        result = run_sync(ss, settings, apply=True, log=log, trigger=history.AUTOMATIC)
    except RunInProgress as exc:
        log(f"Skipped: {exc}")
        history.record_status(history.AUTOMATIC, "skipped", settings.sheet_id, str(exc), log)
        return 0
    except Exception as exc:  # already recorded in runs.csv by run_sync
        log(f"Error: {error_text(exc)}")
        log(traceback.format_exc().rstrip())
        return 1
    log(f"Automatic update finished: wrote {len(result.plan.written)} result(s), "
        f"flagged {len(result.plan.flags)}")
    return 1 if result.download_failures else 0
