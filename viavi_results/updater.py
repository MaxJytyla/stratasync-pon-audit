"""Keep a copy of the app run from a git clone up to date with the repo's main branch.

The app is run from a clone of the repo with Python (the built apps aren't
signed, which some computers block). The window checks for new commits on
origin/main when it opens and every few hours after; when there are some, it
asks, and on yes it pulls them and restarts itself.

Pulling is fast-forward only, and only when it's safe: on the main branch,
with no changes to the app's files and no local commits. Anything else is
reported instead, so nobody's edits are ever overwritten. If requirements.txt
changed, the new requirements are installed into the Python running the app
before it restarts. The pull holds the update lock (sync.run_lock), so an
automatic update can't start while the files are changing. Automatic updates
run the clone's code, so they use the new version from their next run.

Nothing here applies to the built apps (PyInstaller), which aren't clones.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import ConfigError

REPO_DIR = Path(__file__).resolve().parent.parent
BRANCH = "main"
REMOTE = "origin"
CHECK_EVERY_MS = 4 * 60 * 60 * 1000  # while the window is open

# Keep git from opening a password prompt or an editor in the background.
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no console flashing up


class UpdateError(ConfigError):
    """The app can't be updated from here; the message says why and what to do."""


@dataclass
class UpdateInfo:
    current: str  # short commit hash running now
    latest: str  # short commit hash of origin/main
    commits: List[str] = field(default_factory=list)  # "subject (date)", newest first
    blocked: str = ""  # why it can't be pulled from here, if it can't

    @property
    def available(self) -> bool:
        return bool(self.commits)


def supported() -> bool:
    """True when running from a git clone with git installed (not a built app)."""
    return not getattr(sys, "frozen", False) and (REPO_DIR / ".git").exists() and _git_exe() is not None


def _git_exe() -> Optional[str]:
    return shutil.which("git")


def _git(*args: str, timeout: float = 60) -> str:
    exe = _git_exe()
    if exe is None:
        raise UpdateError("git isn't installed (or isn't on the PATH), so the app can't update itself.")
    try:
        proc = subprocess.run([exe, *args], cwd=REPO_DIR, capture_output=True, text=True, timeout=timeout,
                              env=_ENV, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise UpdateError(f"git {args[0]} took too long (no network?).") from None
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip().splitlines()
        raise UpdateError(f"git {' '.join(args)} failed: {message[-1] if message else proc.returncode}")
    return proc.stdout.rstrip()  # not lstrip: "git status --porcelain" lines start with a space


def version() -> str:
    """The running version, e.g. "7536d8b (2026-11-05)"; "" if not a clone."""
    if not supported():
        return ""
    try:
        return _git("log", "-1", "--format=%h (%cs)")
    except UpdateError:
        return ""


def _blocked() -> str:
    """Why a pull isn't safe here, or ""."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        return f"this copy is on branch {branch!r}, not {BRANCH}"
    changed = _git("status", "--porcelain", "--untracked-files=no")
    if changed:
        files = [line[3:] for line in changed.splitlines()]
        return f"the app's files have been changed here ({', '.join(files[:5])})"
    ahead = int(_git("rev-list", "--count", f"{REMOTE}/{BRANCH}..HEAD") or 0)
    if ahead:
        return f"this copy has {ahead} commit(s) that aren't on {REMOTE}/{BRANCH}"
    return ""


def check() -> UpdateInfo:
    """Fetch origin/main and say whether it has commits this copy doesn't."""
    if not supported():
        raise UpdateError("This copy of the app wasn't installed with git clone, so it can't update itself.")
    _git("fetch", "--quiet", REMOTE, BRANCH, timeout=90)
    target = f"{REMOTE}/{BRANCH}"
    log = _git("log", "--format=%s (%cs)", f"HEAD..{target}")
    info = UpdateInfo(_git("rev-parse", "--short", "HEAD"), _git("rev-parse", "--short", target),
                      [line for line in log.splitlines() if line.strip()])
    if info.available:
        info.blocked = _blocked()
    return info


def install(log=print) -> None:
    """Fast-forward to origin/main (fetched by check), and install new requirements.

    Raises UpdateError if it isn't safe, or RunInProgress if an update is
    running on this computer; nothing is changed then.
    """
    from .sync import run_lock

    with run_lock():
        blocked = _blocked()
        if blocked:
            raise UpdateError(f"Can't update automatically: {blocked}. Update it by hand (see the README).")
        target = f"{REMOTE}/{BRANCH}"
        requirements_changed = bool(_git("diff", "--name-only", f"HEAD..{target}", "--", "requirements.txt"))
        before = _git("rev-parse", "--short", "HEAD")
        _git("merge", "--ff-only", "--quiet", target)
        log(f"App updated: {before} -> {_git('rev-parse', '--short', 'HEAD')}")
        if requirements_changed:
            log("App update: installing the new requirements")
            proc = subprocess.run([_python(), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                                   "-r", str(REPO_DIR / "requirements.txt")],
                                  capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
                raise UpdateError("The app's files were updated, but installing its requirements failed: "
                                  + " ".join(tail) + "\n\nRun this in the app's folder, then start the app again:"
                                  "\n  " + _pip_hint())


def _python() -> str:
    """The console Python next to the running one (pythonw.exe has no output for pip)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def _pip_hint() -> str:
    return (r".venv\Scripts\python -m pip install -r requirements.txt" if sys.platform == "win32"
            else ".venv/bin/python -m pip install -r requirements.txt")


def restart() -> None:
    """Start a new copy of the app (the updated code), for the caller to then exit."""
    args = [sys.executable, str(REPO_DIR / "app.py"), *[a for a in sys.argv[1:] if a != "--auto-update"]]
    kwargs = {"cwd": str(REPO_DIR)}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    # Its own I/O, so it doesn't hold on to whatever started this copy (a Terminal, a pipe).
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
