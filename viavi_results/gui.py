"""Desktop window: collect settings, run the StrataSync -> PON Audit sync, show the report."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, time
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .config import (
    APP_NAME,
    STRATASYNC_URLS,
    ConfigError,
    SavedForm,
    make_reissue_settings,
    make_settings,
    make_stratasync_settings,
    parse_service_account,
)
from . import history, scheduler
from .pon_audit import DATA_HALLS
from .sync import SyncResult, error_text as _error_text, last_position, replace_cable, run_sync, write_flags_csv

PAD = 6


def _parse_since(text: str) -> Optional[datetime]:
    """Blank (use the saved position), a date (local midnight) or a date and time."""
    text = text.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ConfigError("'Search from' must be a date like 2026-10-22, or a date and "
                          "time like 2026-10-22 08:00. Leave it blank to continue from the last run.") from None
    if len(text) == 10:
        dt = datetime.combine(dt.date(), time.min)
    return dt if dt.tzinfo else dt.astimezone()


def _parse_when(text: str) -> datetime:
    """A required date and time, in this computer's time zone unless one is given."""
    text = text.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = None
    if dt is None or len(text) <= 10:
        raise ConfigError("Enter the replacement time as a date and time, like 2026-10-30 14:30.")
    return dt if dt.tzinfo else dt.astimezone()


def _shown_time(reading) -> str:
    if reading.uses_sync_time:
        return f"{reading.sheet_time} (sync time; meter clock said {reading.test_time or 'nothing'})"
    return reading.test_time


def _fmt_local(dt: Optional[datetime]) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def _fmt_iso(value: str) -> str:
    try:
        return _fmt_local(datetime.fromisoformat(value))
    except ValueError:
        return value


def _open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        # The window's contents sit on a canvas, so they scroll when the window
        # is shorter than they are (e.g. on a laptop screen); see _fit.
        self.canvas = tk.Canvas(root, highlightthickness=0, borderwidth=0, yscrollincrement=20,
                                background=ttk.Style(root).lookup("TFrame", "background") or None)
        self.vbar = ttk.Scrollbar(root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        super().__init__(self.canvas, padding=(PAD * 2, PAD))
        self.root = root
        self.saved = SavedForm.load()
        self.result: Optional[SyncResult] = None
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.history_mtime = -1.0
        self.polls = 0
        self.warned_no_secrets = False

        root.title(APP_NAME)
        root.minsize(900, 420)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.content = self.canvas.create_window(0, 0, window=self, anchor="nw")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)
        self.canvas.bind("<Configure>", lambda _e: self._fit())
        root.bind_all("<MouseWheel>", self._on_wheel, add="+")
        try:  # Tk 9 reports trackpad scrolling as its own event
            root.bind_all("<TouchpadScroll>", self._on_touchpad, add="+")
        except tk.TclError:  # Tk 8.6 sends it as <MouseWheel>
            pass

        self._build_stratasync()
        self._build_google()
        self._build_schedule()
        self._build_reissue()
        self._build_actions()
        self._build_report()
        self._load_form()
        self._refresh_history()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._initial_size()
        self.after(100, self._poll)

    # ---- window size and scrolling ---------------------------------------------

    def _initial_size(self) -> None:
        """Open at the contents' natural size, but no taller than the screen allows."""
        self.root.update_idletasks()
        # Capped to the screen both ways: with Windows display scaling at 150% the
        # contents can be wider or taller than the screen.
        width = min(max(self.winfo_reqwidth(), 900), self.root.winfo_screenwidth() - 40)
        height = min(self.winfo_reqheight(), self.root.winfo_screenheight() - 120)
        self.root.geometry(f"{width}x{max(height, 420)}")

    def _fit(self) -> None:
        """Size the contents to the window: stretch them when there's room (the
        report gets the extra height), else keep their natural height and scroll."""
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        needed = self.winfo_reqheight()
        self.canvas.itemconfigure(self.content, width=width, height=max(height, needed))
        self.canvas.configure(scrollregion=(0, 0, width, max(height, needed)))
        if needed > height:
            self.vbar.grid(row=0, column=1, sticky="ns")
        else:
            self.vbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _scrolls_window(self, widget) -> bool:
        """True if a wheel or trackpad scroll over `widget` should scroll the
        window: not over lists and text boxes, which scroll themselves, nor
        over other windows, nor when everything already fits."""
        if not isinstance(widget, tk.Misc) or widget.winfo_toplevel() is not self.root:
            return False
        if widget.winfo_class() in ("Treeview", "Text", "TCombobox", "TSpinbox"):
            return False
        return bool(self.vbar.winfo_ismapped())

    def _on_touchpad(self, event) -> None:
        """Trackpad scrolling (Tk 9): the vertical movement in pixels is the low
        16 bits of event.delta, as a signed number (see tk::PreciseScrollDeltas)."""
        if not self._scrolls_window(event.widget):
            return
        low = event.delta & 0xFFFF
        self._scroll_pixels(-(low - 0x10000 if low >= 0x8000 else low))

    def _on_wheel(self, event) -> None:
        """Scroll the window with the mouse wheel (see _scrolls_window).

        On Windows, and with Tk 9 everywhere, a wheel notch is a delta of 120
        and a precision touchpad sends smaller deltas for smaller movements;
        Tk 8.6 on macOS sends a few units per notch instead.
        """
        if not self._scrolls_window(event.widget):
            return
        if sys.platform == "darwin" and tk.TkVersion < 9:
            self._scroll_pixels(-event.delta * 20)
        else:
            self._scroll_pixels(-event.delta * 60 / 120)  # 60 pixels a notch

    def _scroll_pixels(self, pixels: float) -> None:
        """Scroll the window down by `pixels` (up if negative)."""
        total = float(self.canvas.cget("scrollregion").split()[3] or 0)
        if pixels and total:
            top = self.canvas.yview()[0]
            self.canvas.yview_moveto(max(0.0, top + pixels / total))

    # ---- layout -------------------------------------------------------------

    def _build_stratasync(self) -> None:
        box = ttk.LabelFrame(self, text="StrataSync", padding=PAD)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Server:").grid(row=0, column=0, sticky="w")
        self.region = tk.StringVar()
        regions = ttk.Frame(box)
        regions.grid(row=0, column=1, sticky="w")
        for name, url in STRATASYNC_URLS.items():
            ttk.Radiobutton(regions, text=f"{name}  ({url.removeprefix('https://')})",
                            value=name, variable=self.region).pack(side="left", padx=(0, PAD * 3))

        ttk.Label(box, text="Client ID:").grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.client_id = tk.StringVar()
        ttk.Entry(box, textvariable=self.client_id).grid(row=1, column=1, columnspan=2, sticky="ew", pady=(PAD, 0))

        ttk.Label(box, text="Client secret:").grid(row=2, column=0, sticky="w", pady=(PAD, 0))
        self.client_secret = tk.StringVar()
        self.secret_entry = ttk.Entry(box, textvariable=self.client_secret, show="•")
        self.secret_entry.grid(row=2, column=1, sticky="ew", pady=(PAD, 0))
        self.show_secret = tk.BooleanVar()
        ttk.Checkbutton(box, text="Show", variable=self.show_secret,
                        command=lambda: self.secret_entry.configure(show="" if self.show_secret.get() else "•")
                        ).grid(row=2, column=2, padx=(PAD, 0), pady=(PAD, 0))

    def _build_google(self) -> None:
        box = ttk.LabelFrame(self, text="Google Sheet", padding=PAD)
        box.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Sheet ID or URL:").grid(row=0, column=0, sticky="w")
        self.sheet_id = tk.StringVar()
        ttk.Entry(box, textvariable=self.sheet_id).grid(row=0, column=1, columnspan=2, sticky="ew")

        ttk.Label(box, text="Service account key:").grid(row=1, column=0, sticky="nw", pady=(PAD, 0))
        key_frame = ttk.Frame(box)
        key_frame.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(PAD, 0))
        key_frame.columnconfigure(0, weight=1)
        self.key_text = tk.Text(key_frame, height=3, wrap="char", font=("TkFixedFont", 9), undo=True)
        self.key_text.grid(row=0, column=0, sticky="ew")
        self.key_text.bind("<<Modified>>", self._on_key_modified)
        key_buttons = ttk.Frame(key_frame)
        key_buttons.grid(row=0, column=1, sticky="n", padx=(PAD, 0))
        ttk.Button(key_buttons, text="Load file…", command=self._load_key_file).pack(fill="x")
        ttk.Button(key_buttons, text="Clear", command=lambda: self.key_text.delete("1.0", "end")).pack(fill="x", pady=(PAD, 0))
        self.key_status = ttk.Label(key_frame, text="", foreground="gray")
        self.key_status.grid(row=1, column=0, columnspan=2, sticky="w")

    def _build_schedule(self) -> None:
        box = ttk.LabelFrame(self, text="Automatic updates", padding=PAD)
        box.grid(row=2, column=0, sticky="ew", pady=(PAD, 0))
        box.columnconfigure(4, weight=1)

        ttk.Label(box, text="Run update every").grid(row=0, column=0, sticky="w")
        self.every = tk.StringVar(value="1")
        ttk.Spinbox(box, from_=1, to=999, textvariable=self.every, width=5).grid(row=0, column=1, padx=(PAD, 0))
        self.every_unit = tk.StringVar(value="hours")
        ttk.Combobox(box, textvariable=self.every_unit, values=("minutes", "hours"), state="readonly",
                     width=8).grid(row=0, column=2, padx=(PAD, 0))
        buttons = ttk.Frame(box)
        buttons.grid(row=0, column=3, sticky="w", padx=(PAD * 2, 0))
        self.schedule_btn = ttk.Button(buttons, text="Turn on", command=self._turn_on_schedule)
        self.schedule_btn.pack(side="left")
        self.unschedule_btn = ttk.Button(buttons, text="Turn off", command=self._turn_off_schedule)
        self.unschedule_btn.pack(side="left", padx=(PAD, 0))
        ttk.Button(box, text="Open history folder", command=lambda: _open_folder(history.history_dir())
                   ).grid(row=0, column=5, sticky="e")
        self.schedule_status = ttk.Label(box, text="", foreground="gray", wraplength=840, justify="left")
        self.schedule_status.grid(row=1, column=0, columnspan=6, sticky="w", pady=(PAD, 0))
        if not scheduler.supported():
            for w in (self.schedule_btn, self.unschedule_btn):
                w.configure(state="disabled")

    def _build_reissue(self) -> None:
        box = ttk.LabelFrame(self, text="Re-issue jobs", padding=PAD)
        box.grid(row=3, column=0, sticky="ew", pady=(PAD, 0))

        self.reissue_enabled = tk.BooleanVar()
        ttk.Checkbutton(box, text="When a job is completed and some of its locations still need a retest "
                                  "(wrong or no OLT ID, no reading), create a StrataSync job for them",
                        variable=self.reissue_enabled).grid(row=0, column=0, columnspan=8, sticky="w")
        self.reissue_low_light = tk.BooleanVar()
        ttk.Checkbutton(box, text="Also when the light is weaker than", variable=self.reissue_low_light
                        ).grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.rack_dbm = tk.StringVar()
        ttk.Entry(box, textvariable=self.rack_dbm, width=7).grid(row=1, column=1, padx=(PAD, 0), pady=(PAD, 0))
        ttk.Label(box, text="dBm (rack tests)").grid(row=1, column=2, sticky="w", padx=(PAD, 0), pady=(PAD, 0))
        self.f1_dbm = tk.StringVar()
        ttk.Entry(box, textvariable=self.f1_dbm, width=7).grid(row=1, column=3, padx=(PAD * 2, 0), pady=(PAD, 0))
        ttk.Label(box, text="dBm (F1 tests)").grid(row=1, column=4, sticky="w", padx=(PAD, 0), pady=(PAD, 0))
        ttk.Label(box, text="Job template:").grid(row=1, column=5, sticky="e", padx=(PAD * 3, 0), pady=(PAD, 0))
        self.job_template = tk.StringVar()
        ttk.Entry(box, textvariable=self.job_template, width=20).grid(row=1, column=6, sticky="w",
                                                                      padx=(PAD, 0), pady=(PAD, 0))
        ttk.Label(box, text="One unassigned job per cohort, e.g. RMN2.DHA.12 AA C2 RT3. Low-light results are "
                            "still written to the sheet. Preview lists the jobs without creating them.",
                  foreground="gray", wraplength=840, justify="left").grid(row=2, column=0, columnspan=8,
                                                                          sticky="w", pady=(2, 0))

    def _reissue_settings(self):
        return make_reissue_settings(self.reissue_enabled.get(), self.reissue_low_light.get(),
                                     self.rack_dbm.get(), self.f1_dbm.get(), self.job_template.get())

    def _build_actions(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=4, column=0, sticky="ew", pady=(PAD * 2, PAD))
        bar.columnconfigure(3, weight=1)

        ttk.Label(bar, text="Search from:").grid(row=0, column=0, sticky="w")
        self.since = tk.StringVar()
        ttk.Entry(bar, textvariable=self.since, width=18).grid(row=0, column=1, sticky="w", padx=(PAD, 0))
        self.position_label = ttk.Label(bar, text="", foreground="gray")
        self.position_label.grid(row=0, column=2, columnspan=2, sticky="w", padx=(PAD, 0))

        self.remember = tk.BooleanVar()
        ttk.Checkbutton(bar, text="Remember client secret and Google key on this computer",
                        variable=self.remember).grid(row=1, column=0, columnspan=4, sticky="w", pady=(PAD, 0))

        buttons = ttk.Frame(bar)
        buttons.grid(row=0, column=4, rowspan=2, sticky="e")
        self.replace_btn = ttk.Button(buttons, text="Replace cable…", command=self._open_replace_dialog)
        self.replace_btn.pack(side="left", padx=(0, PAD * 4))
        self.preview_btn = ttk.Button(buttons, text="Preview (no changes)", command=lambda: self._start(apply=False))
        self.preview_btn.pack(side="left")
        self.run_btn = ttk.Button(buttons, text="Run update", command=lambda: self._start(apply=True))
        self.run_btn.pack(side="left", padx=(PAD, 0))
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.grid(row=2, column=4, sticky="e", pady=(PAD, 0))

    def _build_report(self) -> None:
        box = ttk.LabelFrame(self, text="Report", padding=PAD)
        box.grid(row=5, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)

        self.summary = ttk.Label(box, text="Run a preview or an update to see what goes into the sheet.",
                                 wraplength=840, justify="left")
        self.summary.grid(row=0, column=0, sticky="ew", pady=(0, PAD))

        self.tabs = ttk.Notebook(box)
        self.tabs.grid(row=1, column=0, sticky="nsew")

        self.written_tree = self._tree_tab(
            "Written", ("label", "tab", "cells", "power", "time", "tech", "serial"),
            ("Location", "Tab", "Cells", "dBm", "Test time", "Tech", "Serial"),
            (150, 150, 80, 60, 190, 130, 90))

        flagged = ttk.Frame(self.tabs)
        self.flag_tree = self._tree(
            flagged, ("kind", "label", "message", "time", "tech", "serial"),
            ("Problem", "Location", "Details", "Test time", "Tech", "Serial"),
            (110, 140, 330, 180, 120, 90))
        export = ttk.Frame(flagged)
        export.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))
        self.export_btn = ttk.Button(export, text="Export flagged to CSV…", command=self._export_flags, state="disabled")
        self.export_btn.pack(side="right")
        ttk.Label(export, text="Flagged results are not written to the sheet. Retest the location to write it.",
                  foreground="gray").pack(side="left")
        self.tabs.add(flagged, text="Flagged")

        self.skipped_tree = self._tree_tab(
            "Skipped", ("label", "reason", "time"), ("Location", "Reason", "Test time"), (180, 380, 200))

        log_frame = ttk.Frame(self.tabs)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=4, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.tabs.add(log_frame, text="Log")

        history_frame = ttk.Frame(self.tabs)
        self.history_tree = self._tree(
            history_frame, ("time", "trigger", "status", "written", "flagged", "details"),
            ("Update at", "Started", "Outcome", "Written", "Flagged", "Details"),
            (150, 80, 70, 60, 60, 400))
        ttk.Label(history_frame, text="Every update that wrote to the sheet, including automatic ones "
                                      "(details in the history folder).",
                  foreground="gray", wraplength=820, justify="left").grid(row=1, column=0, columnspan=2,
                                                                          sticky="w", pady=(PAD, 0))
        self.tabs.add(history_frame, text="History")

        jobs_frame = ttk.Frame(self.tabs)
        self.jobs_tree = self._tree(
            jobs_frame, ("job", "status", "locations", "why"),
            ("Job", "Status", "Locations", "Why"), (200, 150, 260, 330))
        ttk.Label(jobs_frame, text="Jobs re-issued for locations that need a retest (also in jobs.csv).",
                  foreground="gray", wraplength=820, justify="left").grid(row=1, column=0, columnspan=2,
                                                                          sticky="w", pady=(PAD, 0))
        self.tabs.add(jobs_frame, text="Jobs")

    def _tree_tab(self, title, cols, headings, widths) -> ttk.Treeview:
        frame = ttk.Frame(self.tabs)
        tree = self._tree(frame, cols, headings, widths)
        self.tabs.add(frame, text=title)
        return tree

    def _tree(self, frame, cols, headings, widths) -> ttk.Treeview:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=3)
        for col, heading, width in zip(cols, headings, widths):
            tree.heading(col, text=heading)
            tree.column(col, width=width, stretch=True, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        return tree

    # ---- form ---------------------------------------------------------------

    def _load_form(self) -> None:
        s = self.saved
        self.region.set(s.region if s.region in STRATASYNC_URLS else "USA")
        self.client_id.set(s.client_id)
        self.client_secret.set(s.client_secret)
        self.sheet_id.set(s.sheet_id)
        self.key_text.insert("1.0", s.service_account_json)
        self.remember.set(s.remember_secrets)
        self.reissue_enabled.set(s.reissue_enabled)
        self.reissue_low_light.set(s.reissue_low_light)
        self.rack_dbm.set(s.low_light_rack_dbm)
        self.f1_dbm.set(s.low_light_f1_dbm)
        self.job_template.set(s.job_template)
        if s.auto_update_minutes % 60:
            self.every.set(str(s.auto_update_minutes))
            self.every_unit.set("minutes")
        else:
            self.every.set(str(s.auto_update_minutes // 60))
            self.every_unit.set("hours")
        self._update_key_status()
        self._update_position()
        for var in (self.region, self.client_id, self.sheet_id):
            var.trace_add("write", lambda *_: self._update_position())

    def _form(self) -> SavedForm:
        return SavedForm(
            region=self.region.get(),
            client_id=self.client_id.get().strip(),
            client_secret=self.client_secret.get().strip(),
            service_account_json=self.key_text.get("1.0", "end").strip(),
            sheet_id=self.sheet_id.get().strip(),
            remember_secrets=self.remember.get(),
            auto_update_minutes=self._interval_minutes(strict=False),
            reissue_enabled=self.reissue_enabled.get(),
            reissue_low_light=self.reissue_low_light.get(),
            low_light_rack_dbm=self.rack_dbm.get().strip(),
            low_light_f1_dbm=self.f1_dbm.get().strip(),
            job_template=self.job_template.get().strip(),
        )

    def _interval_minutes(self, strict: bool = True) -> int:
        try:
            n = int(self.every.get().strip())
        except ValueError:
            if strict:
                raise ConfigError("Enter how often to run the update, as a whole number.") from None
            return self.saved.auto_update_minutes
        return n * (60 if self.every_unit.get() == "hours" else 1)

    def _save_form(self) -> None:
        try:
            form = self._form()
            form.save()
        except OSError as exc:
            self._log(f"Could not save settings: {exc}")
            return
        if not form.remember_secrets and self._schedule() and not self.warned_no_secrets:
            self.warned_no_secrets = True
            messagebox.showwarning(APP_NAME, (
                "Automatic updates are on, but 'Remember client secret and Google key on this computer' "
                "is not ticked, so they can't sign in and will fail. Tick it, or turn automatic updates off."),
                parent=self.root)

    # ---- automatic updates ------------------------------------------------------

    def _schedule(self) -> Optional[scheduler.Schedule]:
        try:
            return scheduler.current()
        except OSError:
            return None

    def _update_schedule_status(self) -> None:
        if not scheduler.supported():
            self.schedule_status.configure(text="Automatic updates are only available on Windows and macOS.")
            return
        sched = self._schedule()
        if sched is None:
            lines = ["Off. Updates only run when you click Run update."]
        elif not sched.minutes:
            lines = ["On, but set up in a way this app doesn't recognise. Turn on again to replace it."]
        else:
            lines = [f"On: every {scheduler.describe(sched.minutes)} while you're signed in to this computer, "
                     "with the settings saved here."]
            if not sched.is_current_app:
                lines.append("It starts a different copy of this app (" + sched.command[0] +
                             "). Click Turn on to use this one.")
        last = next((r for r in history.recent_runs() if r.get("trigger") == history.AUTOMATIC), None)
        if last:
            outcome = last.get("status", "")
            if outcome in ("ok", "partial"):
                outcome = f"wrote {last.get('written')}, flagged {last.get('flagged')}" + (
                    " (some downloads failed; will retry)" if outcome == "partial" else "")
            else:
                outcome = f"{outcome}: {last.get('message', '')}"
            lines.append(f"Last automatic update: {_fmt_iso(last.get('run_at', ''))}, {outcome}")
        self.schedule_status.configure(text="\n".join(lines),
                                       foreground="#b00020" if last and last.get("status") == "failed" else "gray")
        self.unschedule_btn.configure(state="normal" if sched else "disabled")
        self.schedule_btn.configure(text="Update" if sched and sched.is_current_app else "Turn on")

    def _turn_on_schedule(self) -> None:
        try:
            minutes = self._interval_minutes()
            make_stratasync_settings(self.region.get(), self.client_id.get(), self.client_secret.get())
            make_settings(self.sheet_id.get(), self.key_text.get("1.0", "end"))
            self._reissue_settings()
        except ConfigError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        if not self.remember.get():
            messagebox.showerror(APP_NAME, (
                "Automatic updates run without this window, so they need the client secret and Google key "
                "saved on this computer. Tick 'Remember client secret and Google key on this computer' first."),
                parent=self.root)
            return
        self._save_form()
        try:
            scheduler.install(minutes)
        except (ConfigError, OSError) as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        self._log(f"Automatic updates on: every {scheduler.describe(minutes)}")
        self._update_schedule_status()
        messagebox.showinfo(APP_NAME, (
            f"Run update will happen every {scheduler.describe(minutes)}, starting "
            f"{scheduler.describe(minutes)} from now, while you're signed in to this computer and "
            "it's awake. The app doesn't need to be open.\n\n"
            "It uses the settings saved here; changes you make in this window are used from the next "
            "update on. Every update, with its flagged results, is recorded on the History tab and in "
            "the history folder."), parent=self.root)

    def _turn_off_schedule(self) -> None:
        try:
            scheduler.remove()
        except (ConfigError, OSError) as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        self._log("Automatic updates off")
        self._update_schedule_status()

    def _refresh_history(self) -> None:
        """Reload the History tab if runs.csv has changed (e.g. after an automatic update)."""
        mtime = history.runs_mtime()
        if mtime == self.history_mtime:
            return
        self.history_mtime = mtime
        self.history_tree.delete(*self.history_tree.get_children())
        for r in history.recent_runs():
            self.history_tree.insert("", "end", values=(
                _fmt_iso(r.get("run_at", "")), r.get("trigger", ""), r.get("status", ""),
                r.get("written", ""), r.get("flagged", ""), r.get("message", "")))
        self._update_position()
        self._update_schedule_status()

    def _load_key_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose the service account key",
            filetypes=[("JSON key", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
            parse_service_account(text)
        except (OSError, UnicodeDecodeError, ConfigError) as exc:
            messagebox.showerror(APP_NAME, f"Could not use {Path(path).name}:\n\n{exc}", parent=self.root)
            return
        self.key_text.delete("1.0", "end")
        self.key_text.insert("1.0", text.strip())

    def _on_key_modified(self, _event=None) -> None:
        self.key_text.edit_modified(False)
        self._update_key_status()
        self._update_position()

    def _update_key_status(self) -> None:
        text = self.key_text.get("1.0", "end").strip()
        if not text:
            self.key_status.configure(text="Load service_account.json, or paste its contents above.", foreground="gray")
            return
        try:
            info = parse_service_account(text)
        except ConfigError as exc:
            self.key_status.configure(text=str(exc), foreground="#b00020")
            return
        self.key_status.configure(
            text=f"Service account: {info['client_email']}  (share the sheet with this address as an Editor)",
            foreground="#1b5e20")

    def _update_position(self) -> None:
        try:
            ss = make_stratasync_settings(self.region.get(), self.client_id.get(), "-")
            settings = make_settings(self.sheet_id.get(), self.key_text.get("1.0", "end"))
        except ConfigError:
            self.position_label.configure(text="(blank = continue from the last update)")
            return
        state = last_position(ss, settings)
        if state.last_sync_time:
            self.position_label.configure(
                text=f"(blank = continue from the last update: {_fmt_local(state.last_sync_time)})")
        else:
            self.position_label.configure(text="(blank = from midnight today; no update run yet for this sheet)")

    # ---- running ------------------------------------------------------------

    def _start(self, apply: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            ss = make_stratasync_settings(self.region.get(), self.client_id.get(), self.client_secret.get())
            settings = make_settings(self.sheet_id.get(), self.key_text.get("1.0", "end"))
            since = _parse_since(self.since.get())
            reissue = self._reissue_settings()
        except ConfigError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        self._save_form()

        self._clear_report()
        self.summary.configure(text="Running…" if apply else "Previewing…")
        self.tabs.select(3)
        for btn in (self.replace_btn, self.preview_btn, self.run_btn):
            btn.configure(state="disabled")
        self.progress.start(12)

        def work():
            try:
                result = run_sync(ss, settings, apply=apply, since=since, reissue=reissue,
                                  log=lambda msg: self.events.put(("log", msg)))
                self.events.put(("done", result))
            except Exception as exc:  # shown to the user, not swallowed
                self.events.put(("error", exc))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "call":
                    payload()
                elif kind == "done":
                    self._finish()
                    self._show_result(payload)
                elif kind == "error":
                    self._finish()
                    self._log(f"Error: {_error_text(payload)}")
                    self.summary.configure(text="The run failed; nothing was written. See the Log tab.")
                    messagebox.showerror(APP_NAME, _error_text(payload), parent=self.root)
        except queue.Empty:
            pass
        self.polls += 1
        if self.polls % 50 == 0:  # every ~5 s
            self._refresh_history()
        self.after(100, self._poll)

    def _finish(self) -> None:
        self.progress.stop()
        for btn in (self.replace_btn, self.preview_btn, self.run_btn):
            btn.configure(state="normal")
        self._update_position()
        self._refresh_history()

    def background(self, work, on_done, on_error) -> bool:
        """Run `work()` off the UI thread, then `on_done(result)` or `on_error(exc)`
        on it. Returns False (and does nothing) if something is already running."""
        if self.worker and self.worker.is_alive():
            return False
        for btn in (self.replace_btn, self.preview_btn, self.run_btn):
            btn.configure(state="disabled")
        self.progress.start(12)

        def run():
            try:
                result = work()
                self.events.put(("call", lambda: (self._finish(), on_done(result))))
            except Exception as exc:  # shown to the user, not swallowed
                self.events.put(("call", lambda exc=exc: (self._finish(), on_error(exc))))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        return True

    def _open_replace_dialog(self) -> None:
        try:
            settings = make_settings(self.sheet_id.get(), self.key_text.get("1.0", "end"))
        except ConfigError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        self._save_form()
        CableDialog(self, settings)

    def stratasync_settings(self):
        """The StrataSync account from the form; raises ConfigError if incomplete."""
        return make_stratasync_settings(self.region.get(), self.client_id.get(), self.client_secret.get())

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now().strftime('%H:%M:%S')}  {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---- report -------------------------------------------------------------

    def _clear_report(self) -> None:
        self.result = None
        for tree in (self.written_tree, self.flag_tree, self.skipped_tree, self.jobs_tree):
            tree.delete(*tree.get_children())
        self.export_btn.configure(state="disabled")
        self._set_tab_counts(None, None, None)
        self.tabs.tab(self.JOBS_TAB, text="Jobs")

    JOBS_TAB = 5

    def _show_jobs(self, r: SyncResult) -> Optional[str]:
        """Fill the Jobs tab; returns the summary line, or None if re-issuing is off."""
        if r.reissue is None and not r.reissue_error:
            return None
        plan = r.reissue
        if plan:
            for job in plan.jobs:
                org = f", organization {job.org_id}" if job.org_id else ""
                status = {"created": f"created ({job.workflow_id}{org})", "failed": f"failed: {job.error}",
                          "already re-issued": "not created: already re-issued (see below)",
                          "planned": (f"would create{org}" if not r.applied else "not created")}[job.status]
                self.jobs_tree.insert("", "end", values=(
                    job.work_order_id, status, " ".join(job.labels), " | ".join(t.reason for t in job.retests)))
            for retest, existing in plan.already_reissued:
                self.jobs_tree.insert("", "end", values=(
                    "", f"already in {existing}", retest.label, retest.reason))
            for retest, job in plan.waiting:
                self.jobs_tree.insert("", "end", values=(
                    "", f"waiting for {job} to be completed", retest.label, retest.reason))
            for retest, why in plan.not_reissued:
                self.jobs_tree.insert("", "end", values=("", f"not re-issued: {why}", retest.label, retest.reason))
            self.tabs.tab(self.JOBS_TAB, text=f"Jobs ({len(plan.jobs)})")
        if r.reissue_error:
            return f"Jobs could not be re-issued: {r.reissue_error}"
        created = sum(j.status == "created" for j in plan.jobs)
        line = (f"Re-issued {created} job(s)" if r.applied else f"Would re-issue {len(plan.jobs)} job(s)") + (
            f" for {sum(len(j.retests) for j in plan.jobs)} location(s); see the Jobs tab.")
        if plan.failures:
            line += f" {len(plan.failures)} failed, so the download position was not moved; the next update retries them."
        if plan.waiting:
            line += f" {len(plan.waiting)} location(s) waiting for their job to be completed."
        if plan.sheet_behind:
            line += (f" {len(plan.sheet_behind)} location(s) passed in their job but that result isn't in the "
                     "sheet yet; run an update with an earlier 'Search from' date to add it.")
        if plan.already_reissued:
            line += f" {len(plan.already_reissued)} location(s) already re-issued."
        return line

    def _set_tab_counts(self, written, flagged, skipped) -> None:
        for i, (name, n) in enumerate((("Written", written), ("Flagged", flagged), ("Skipped", skipped))):
            self.tabs.tab(i, text=name if n is None else f"{name} ({n})")

    def _show_result(self, r: SyncResult) -> None:
        self.result = r
        plan = r.plan

        for reading, power, stamp in plan.written_cells():
            self.written_tree.insert("", "end", values=(
                reading.label, power.tab, f"{power.a1}, {stamp.a1}", reading.power_dbm,
                _shown_time(reading), reading.tech_name or "", reading.serial_no or ""))
        for f in plan.flags:
            rd = f.reading
            self.flag_tree.insert("", "end", values=(
                f.kind, rd.label if rd else "", f.message, rd.test_time if rd else "",
                (rd.tech_name or "") if rd else "", (rd.serial_no or "") if rd else ""))
        # Flagged readings are listed in full on the Flagged tab.
        skipped = [(rd, why) for rd, why in plan.skipped if why != "flagged (not written)"]
        for rd, why in skipped:
            self.skipped_tree.insert("", "end", values=(rd.label, why, _shown_time(rd)))
        self._set_tab_counts(len(plan.written), len(plan.flags), len(skipped))
        self.export_btn.configure(state="normal" if plan.flags else "disabled")

        verb = "Wrote" if r.applied else "Would write"
        lines = [
            f"{'Update' if r.applied else 'Preview'} of '{r.sheet_title}' — results received "
            f"{_fmt_local(r.start)} to {_fmt_local(r.end)}.",
            f"StrataSync: {r.found} result(s), {r.truepon} TruePON, {r.new} new.  "
            f"{verb} {len(plan.written)} result(s) ({len(plan.updates)} cells).  "
            f"Flagged (not written): {len(plan.flags)}.  Skipped: {len(skipped)}.",
        ]
        if r.missing_tabs:
            lines.append(f"Tabs not found in the sheet: {', '.join(r.missing_tabs)}.")
        if r.download_failures:
            lines.append(f"{len(r.download_failures)} download(s) failed, so the download position was not "
                         "moved; the next update will retry them.")
        jobs_line = self._show_jobs(r)
        if jobs_line:
            lines.append(jobs_line)
        if not r.applied:
            lines.append("Preview only: nothing was written and no jobs were created. "
                         "Click Run update to write these results.")
        self.summary.configure(text="\n".join(lines))
        self.tabs.select(1 if plan.flags and not plan.written else 0)

    def _export_flags(self) -> None:
        if not (self.result and self.result.plan.flags):
            return
        default = f"flagged-{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save flagged results", defaultextension=".csv",
            initialfile=default, filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            write_flags_csv(self.result.plan, Path(path))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save {path}:\n\n{exc}", parent=self.root)
            return
        self._log(f"Saved {len(self.result.plan.flags)} flagged result(s) to {path}")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(APP_NAME, "An update is still running. Quit anyway?", parent=self.root):
                return
        self._save_form()
        self.root.destroy()


class CableDialog(tk.Toplevel):
    """Record a multi-drop cable replacement: clear the cable's results and block older tests."""

    def __init__(self, app: App, settings):
        super().__init__(app.root)
        self.app, self.settings = app, settings
        self.previewed = None  # the form values the current preview was made for
        self.title("Replace cable")
        self.transient(app.root)
        self.minsize(640, 460)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        form = ttk.Frame(self, padding=PAD * 2)
        form.grid(row=0, column=0, sticky="ew")
        ttk.Label(form, text="Data hall:").grid(row=0, column=0, sticky="w")
        self.data_hall = tk.StringVar(value=DATA_HALLS[0])
        ttk.Combobox(form, textvariable=self.data_hall, values=DATA_HALLS, state="readonly",
                     width=4).grid(row=0, column=1, sticky="w", padx=(PAD, PAD * 3))
        ttk.Label(form, text="Row:").grid(row=0, column=2, sticky="w")
        self.row = tk.StringVar()
        ttk.Entry(form, textvariable=self.row, width=6).grid(row=0, column=3, sticky="w", padx=(PAD, PAD * 3))
        ttk.Label(form, text="Channel:").grid(row=0, column=4, sticky="w")
        self.channel = tk.StringVar(value="A")
        for i, ch in enumerate("AB"):
            ttk.Radiobutton(form, text=ch, value=ch, variable=self.channel).grid(row=0, column=5 + i, padx=(PAD, 0))

        ttk.Label(form, text="Replaced at:").grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.when = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d %H:%M"))
        ttk.Entry(form, textvariable=self.when, width=18).grid(row=1, column=1, columnspan=3, sticky="w",
                                                               padx=(PAD, 0), pady=(PAD, 0))
        offset = datetime.now().astimezone().strftime("%z")
        ttk.Label(form, text=f"This computer's time (UTC{offset[:3]}:{offset[3:]}). Tests on this cable made "
                             "before this time will be ignored.",
                  foreground="gray", wraplength=600, justify="left").grid(row=2, column=0, columnspan=8, sticky="w")
        ttk.Button(form, text="Preview", command=self._preview).grid(row=1, column=4, columnspan=3, sticky="w",
                                                                    padx=(0, 0), pady=(PAD, 0))
        self.make_jobs = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Also create new jobs for the cable's rack cohorts (C1, C2, C3; not F1), "
                                   "assigned to no technician",
                        variable=self.make_jobs).grid(row=3, column=0, columnspan=8, sticky="w", pady=(PAD, 0))

        self.status = ttk.Label(self, text="Choose the cable and click Preview to see which racks will be cleared.",
                                wraplength=620, justify="left", padding=(PAD * 2, 0))
        self.status.grid(row=1, column=0, sticky="ew")

        frame = ttk.Frame(self, padding=(PAD * 2, PAD))
        frame.grid(row=2, column=0, sticky="nsew")
        self.tree = app._tree(frame, ("rack", "dbm", "time", "action"),
                              ("Rack", "Current dBm", "Current timestamp", "Action"), (60, 100, 210, 250))

        buttons = ttk.Frame(self, padding=PAD * 2)
        buttons.grid(row=3, column=0, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left")
        self.apply_btn = ttk.Button(buttons, text="Replace cable", command=self._apply, state="disabled")
        self.apply_btn.pack(side="left", padx=(PAD, 0))

        for var in (self.data_hall, self.row, self.channel, self.when, self.make_jobs):
            var.trace_add("write", lambda *_: self._invalidate())

    def _inputs(self):
        try:
            row = int(self.row.get().strip())
        except ValueError:
            raise ConfigError("Enter the row number.") from None
        return self.data_hall.get(), row, self.channel.get(), _parse_when(self.when.get())

    def _job_options(self) -> dict:
        """replace_cable's job arguments: none unless 'create new jobs' is ticked."""
        if not self.make_jobs.get():
            return {}
        template = self.app.job_template.get().strip()
        if not template:
            raise ConfigError("Enter the job template under Re-issue jobs in the main window.")
        return {"jobs_for": self.app.stratasync_settings(), "template": template}

    def _invalidate(self) -> None:
        self.previewed = None
        self.apply_btn.configure(state="disabled")

    def _preview(self) -> None:
        try:
            inputs = self._inputs()
            options = self._job_options()
        except ConfigError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.status.configure(text="Reading the sheet…" + (" and StrataSync's jobs…" if options else ""))
        if not self.app.background(lambda: replace_cable(self.settings, *inputs, apply=False, **options),
                                   lambda res: self._show_preview((inputs, options), *res), self._failed):
            self.status.configure(text="Wait for the current run to finish, then click Preview again.")

    def _show_preview(self, inputs, sheet_title, plan, jobs_plan) -> None:
        if not self.winfo_exists():
            return
        self.tree.delete(*self.tree.get_children())
        for r in plan.racks:
            action = f"clear, set timestamp to {plan.replaced_at}" if r.cleared else f"leave: {r.reason}"
            self.tree.insert("", "end", values=(r.rack, r.old_power or "", r.old_timestamp or "", action))
        cleared = sum(r.cleared for r in plan.racks)
        jobs = ""
        if jobs_plan is not None:
            jobs = "New jobs: " + ("; ".join(f"{j.work_order_id} ({len(j.labels)} rack(s))" for j in jobs_plan.jobs)
                                   or "none") + ". "
            jobs += "".join(f"Not in a job: {r.label} ({why}). " for r, why in jobs_plan.not_reissued)
            if jobs_plan.already_reissued:
                jobs += (f"{len(jobs_plan.already_reissued)} rack(s) left out, already in a job from the "
                         f"replacement day on: " + ", ".join(sorted({e for _, e in jobs_plan.already_reissued})) + ". ")
        self.status.configure(text=(
            f"{plan.cable} in '{sheet_title}': {len(plan.racks)} rack(s), {cleared} to clear. {jobs}"
            "Nothing has been changed yet."))
        self.previewed = (inputs, sheet_title, plan.cable, plan.replaced_at, cleared, jobs_plan)
        self.apply_btn.configure(state="normal" if cleared else "disabled")

    def _apply(self) -> None:
        if not self.previewed:
            return
        (inputs, options), sheet_title, cable, stamp, cleared, jobs_plan = self.previewed
        jobs = ""
        if jobs_plan and jobs_plan.jobs:
            jobs = (f"{len(jobs_plan.jobs)} new job(s) will be created, assigned to no technician: "
                    + ", ".join(j.work_order_id for j in jobs_plan.jobs) + ".\n\n")
        if not messagebox.askyesno(APP_NAME, (
                f"Replace {cable} in '{sheet_title}'?\n\n"
                f"The dBm of {cleared} rack(s) will be cleared and their timestamps set to {stamp}. "
                "Tests on this cable from before that time will be ignored from now on.\n\n" + jobs +
                "The old values can only be recovered from the sheet's version history."),
                icon="warning", parent=self):
            return
        self.status.configure(text="Writing…")
        self.app.background(
            lambda: replace_cable(self.settings, *inputs, apply=True, **options,
                                  log=lambda msg: self.app.events.put(("log", msg))),
            self._applied, self._failed)

    def _applied(self, res) -> None:
        sheet_title, plan, jobs_plan = res
        cleared = sum(r.cleared for r in plan.racks)
        message = f"Replaced {plan.cable}: cleared {cleared} rack(s) in '{sheet_title}'."
        if jobs_plan and jobs_plan.jobs:
            made = [j.work_order_id for j in jobs_plan.jobs if j.status == "created"]
            message += "\n\nCreated job(s): " + (", ".join(made) or "none") + "."
            if jobs_plan.failures:
                message += "\n\nCould not create: " + "; ".join(
                    f"{j.work_order_id} ({j.error})" for j in jobs_plan.failures) + (
                    "\n\nThe cable replacement itself was recorded. Click Replace cable again with the same "
                    "time to retry: racks already in a new job are left out.")
        show = messagebox.showwarning if jobs_plan and jobs_plan.failures else messagebox.showinfo
        show(APP_NAME, message, parent=self if self.winfo_exists() else self.app.root)
        if self.winfo_exists():
            self.destroy()

    def _failed(self, exc) -> None:
        self.app._log(f"Error: {_error_text(exc)}")
        if self.winfo_exists():
            self.status.configure(text="Failed; see the message.")
        messagebox.showerror(APP_NAME, _error_text(exc), parent=self if self.winfo_exists() else self.app.root)


def main() -> None:
    if sys.platform == "win32":
        try:  # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
