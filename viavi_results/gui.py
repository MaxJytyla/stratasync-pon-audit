"""Desktop window: collect settings, run the StrataSync -> PON Audit sync, show the report."""

from __future__ import annotations

import queue
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
    make_settings,
    make_stratasync_settings,
    parse_service_account,
)
from .sheets import SheetAccessError
from .stratasync import StrataSyncError
from .pon_audit import DATA_HALLS
from .sync import SyncResult, last_position, replace_cable, run_sync, write_flags_csv

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


def _error_text(exc: BaseException) -> str:
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


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=PAD * 2)
        self.root = root
        self.saved = SavedForm.load()
        self.result: Optional[SyncResult] = None
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None

        root.title(APP_NAME)
        root.minsize(900, 680)
        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self._build_stratasync()
        self._build_google()
        self._build_actions()
        self._build_report()
        self._load_form()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

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
        self.key_text = tk.Text(key_frame, height=4, wrap="char", font=("TkFixedFont", 9), undo=True)
        self.key_text.grid(row=0, column=0, sticky="ew")
        self.key_text.bind("<<Modified>>", self._on_key_modified)
        key_buttons = ttk.Frame(key_frame)
        key_buttons.grid(row=0, column=1, sticky="n", padx=(PAD, 0))
        ttk.Button(key_buttons, text="Load file…", command=self._load_key_file).pack(fill="x")
        ttk.Button(key_buttons, text="Clear", command=lambda: self.key_text.delete("1.0", "end")).pack(fill="x", pady=(PAD, 0))
        self.key_status = ttk.Label(key_frame, text="", foreground="gray")
        self.key_status.grid(row=1, column=0, columnspan=2, sticky="w")

    def _build_actions(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", pady=(PAD * 2, PAD))
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
        box.grid(row=3, column=0, sticky="nsew")
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
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.tabs.add(log_frame, text="Log")

    def _tree_tab(self, title, cols, headings, widths) -> ttk.Treeview:
        frame = ttk.Frame(self.tabs)
        tree = self._tree(frame, cols, headings, widths)
        self.tabs.add(frame, text=title)
        return tree

    def _tree(self, frame, cols, headings, widths) -> ttk.Treeview:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=cols, show="headings")
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
        )

    def _save_form(self) -> None:
        try:
            self._form().save()
        except OSError as exc:
            self._log(f"Could not save settings: {exc}")

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
                result = run_sync(ss, settings, apply=apply, since=since,
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
        self.after(100, self._poll)

    def _finish(self) -> None:
        self.progress.stop()
        for btn in (self.replace_btn, self.preview_btn, self.run_btn):
            btn.configure(state="normal")
        self._update_position()

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

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now().strftime('%H:%M:%S')}  {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---- report -------------------------------------------------------------

    def _clear_report(self) -> None:
        self.result = None
        for tree in (self.written_tree, self.flag_tree, self.skipped_tree):
            tree.delete(*tree.get_children())
        self.export_btn.configure(state="disabled")
        self._set_tab_counts(None, None, None)

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
        if not r.applied:
            lines.append("Preview only: nothing was written. Click Run update to write these results.")
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

        for var in (self.data_hall, self.row, self.channel, self.when):
            var.trace_add("write", lambda *_: self._invalidate())

    def _inputs(self):
        try:
            row = int(self.row.get().strip())
        except ValueError:
            raise ConfigError("Enter the row number.") from None
        return self.data_hall.get(), row, self.channel.get(), _parse_when(self.when.get())

    def _invalidate(self) -> None:
        self.previewed = None
        self.apply_btn.configure(state="disabled")

    def _preview(self) -> None:
        try:
            inputs = self._inputs()
        except ConfigError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.status.configure(text="Reading the sheet…")
        if not self.app.background(lambda: replace_cable(self.settings, *inputs, apply=False),
                                   lambda res: self._show_preview(inputs, *res), self._failed):
            self.status.configure(text="Wait for the current run to finish, then click Preview again.")

    def _show_preview(self, inputs, sheet_title, plan) -> None:
        if not self.winfo_exists():
            return
        self.tree.delete(*self.tree.get_children())
        for r in plan.racks:
            action = f"clear, set timestamp to {plan.replaced_at}" if r.cleared else f"leave: {r.reason}"
            self.tree.insert("", "end", values=(r.rack, r.old_power or "", r.old_timestamp or "", action))
        cleared = sum(r.cleared for r in plan.racks)
        self.status.configure(text=(
            f"{plan.cable} in '{sheet_title}': {len(plan.racks)} rack(s), {cleared} to clear. "
            "Nothing has been changed yet."))
        self.previewed = (inputs, sheet_title, plan.cable, plan.replaced_at, cleared)
        self.apply_btn.configure(state="normal" if cleared else "disabled")

    def _apply(self) -> None:
        if not self.previewed:
            return
        inputs, sheet_title, cable, stamp, cleared = self.previewed
        if not messagebox.askyesno(APP_NAME, (
                f"Replace {cable} in '{sheet_title}'?\n\n"
                f"The dBm of {cleared} rack(s) will be cleared and their timestamps set to {stamp}. "
                "Tests on this cable from before that time will be ignored from now on.\n\n"
                "The old values can only be recovered from the sheet's version history."),
                icon="warning", parent=self):
            return
        self.status.configure(text="Writing…")
        self.app.background(
            lambda: replace_cable(self.settings, *inputs, apply=True,
                                  log=lambda msg: self.app.events.put(("log", msg))),
            self._applied, self._failed)

    def _applied(self, res) -> None:
        sheet_title, plan = res
        cleared = sum(r.cleared for r in plan.racks)
        messagebox.showinfo(APP_NAME, f"Replaced {plan.cable}: cleared {cleared} rack(s) in '{sheet_title}'.",
                            parent=self if self.winfo_exists() else self.app.root)
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
