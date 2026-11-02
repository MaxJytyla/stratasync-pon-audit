//! Desktop window: collect settings, run the StrataSync -> PON Audit sync, show the report.

use std::path::Path;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::thread::JoinHandle;
use std::time::{Duration, SystemTime};

use eframe::egui::{self, Color32, RichText};
use egui_extras::{Column, TableBuilder};

use crate::config::{
    APP_NAME, STRATASYNC_URLS, SavedForm, Settings, make_settings, make_stratasync_settings, parse_service_account,
    region_url,
};
use crate::error::AppError;
use crate::history;
use crate::pon_audit::{CableReplacementPlan, DATA_HALLS, py_float};
use crate::scheduler;
use crate::sync::{SyncResult, last_position, replace_cable, run_sync, write_flags_csv};
use crate::timeutil::{self, Parsed, Time};

/// Status colours that read well on the light and the dark theme.
fn red(ui: &egui::Ui) -> Color32 {
    if ui.visuals().dark_mode { Color32::from_rgb(0xFF, 0x6B, 0x6B) } else { Color32::from_rgb(0xB0, 0x00, 0x20) }
}

fn green(ui: &egui::Ui) -> Color32 {
    if ui.visuals().dark_mode { Color32::from_rgb(0x7C, 0xD9, 0x8B) } else { Color32::from_rgb(0x1B, 0x5E, 0x20) }
}

#[derive(Clone, Copy, PartialEq)]
enum Tone {
    Plain,
    Good,
    Bad,
}
const ROW_HEIGHT: f32 = 20.0;

pub fn run() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(APP_NAME)
            .with_inner_size([980.0, 860.0])
            .with_min_inner_size([900.0, 700.0]),
        ..Default::default()
    };
    eframe::run_native(APP_NAME, options, Box::new(|_cc| Ok(Box::new(App::new()))))
}

/// 'Search from': blank (use the saved position), a date (local midnight) or a date and time.
pub fn parse_since(text: &str) -> Result<Option<Time>, AppError> {
    let text = text.trim();
    if text.is_empty() {
        return Ok(None);
    }
    let bad = || {
        AppError::config(
            "'Search from' must be a date like 2026-10-22, or a date and time like 2026-10-22 08:00. \
             Leave it blank to continue from the last run.",
        )
    };
    match timeutil::parse_iso(text).ok_or_else(bad)? {
        Parsed::Aware(t) => Ok(Some(t)),
        Parsed::Naive(n) => timeutil::local_from_naive(n).map(Some).ok_or_else(bad),
    }
}

/// A required date and time, in this computer's time zone unless one is given.
pub fn parse_when(text: &str) -> Result<Time, AppError> {
    let text = text.trim();
    let bad = || AppError::config("Enter the replacement time as a date and time, like 2026-10-30 14:30.");
    if text.len() <= 10 {
        return Err(bad());
    }
    match timeutil::parse_iso(text).ok_or_else(bad)? {
        Parsed::Aware(t) => Ok(t),
        Parsed::Naive(n) => timeutil::local_from_naive(n).ok_or_else(bad),
    }
}

fn open_folder(path: &Path) {
    let _ = std::fs::create_dir_all(path);
    let program = if cfg!(windows) {
        "explorer"
    } else if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    };
    let _ = std::process::Command::new(program).arg(path).spawn();
}

// ---- background work -----------------------------------------------------------------

enum Event {
    Log(String),
    Synced(Result<SyncResult, AppError>),
    CablePreviewed(CableInputs, Result<(String, CableReplacementPlan), AppError>),
    CableReplaced(Result<(String, CableReplacementPlan), AppError>),
}

#[derive(Clone, PartialEq)]
struct CableInputs {
    data_hall: String,
    row: i64,
    channel: String,
    replaced_at: Time,
}

#[derive(Clone, Copy, PartialEq)]
enum Tab {
    Written,
    Flagged,
    Skipped,
    Log,
    History,
}

#[derive(Clone, Copy, PartialEq)]
enum Unit {
    Minutes,
    Hours,
}

/// What a confirmation dialog does when the user says yes.
#[derive(Clone)]
enum Confirm {
    ReplaceCable,
    Quit,
}

#[derive(Clone)]
struct Dialog {
    title: String,
    message: String,
    error: bool,
    confirm: Option<(String, Confirm)>,
}

/// The report's rows, prepared once when a result arrives.
#[derive(Default)]
struct Report {
    summary: String,
    written: Vec<Vec<String>>,
    flagged: Vec<Vec<String>>,
    skipped: Vec<Vec<String>>,
    result: Option<SyncResult>,
}

struct CableDialog {
    settings: Settings,
    data_hall: String,
    row: String,
    channel: String,
    when: String,
    status: String,
    rows: Vec<Vec<String>>,
    previewed: Option<(CableInputs, String, String, String, usize)>, // inputs, sheet, cable, stamp, cleared
}

struct App {
    saved: SavedForm,
    region: String,
    client_id: String,
    client_secret: String,
    show_secret: bool,
    sheet_id: String,
    key_text: String,
    remember: bool,
    every: String,
    unit: Unit,
    since: String,

    events_tx: Sender<Event>,
    events_rx: Receiver<Event>,
    worker: Option<JoinHandle<()>>,
    busy_text: String,

    tab: Tab,
    report: Report,
    log: Vec<String>,
    history_rows: Vec<Vec<String>>,
    history_mtime: Option<Option<SystemTime>>,
    schedule_status: (String, bool),
    schedule: Option<scheduler::Schedule>,
    key_status: (String, String, Tone), // for key text, message, colour
    position: (String, String),         // for inputs, message
    warned_no_secrets: bool,

    dialog: Option<Dialog>,
    cable: Option<CableDialog>,
    allow_close: bool,
}

impl App {
    fn new() -> App {
        let saved = SavedForm::load();
        let (every, unit) = if !saved.auto_update_minutes.is_multiple_of(60) {
            (saved.auto_update_minutes.to_string(), Unit::Minutes)
        } else {
            ((saved.auto_update_minutes / 60).to_string(), Unit::Hours)
        };
        let (tx, rx) = channel();
        let mut app = App {
            region: if region_url(&saved.region).is_some() { saved.region.clone() } else { "USA".into() },
            client_id: saved.client_id.clone(),
            client_secret: saved.client_secret.clone(),
            show_secret: false,
            sheet_id: saved.sheet_id.clone(),
            key_text: saved.service_account_json.clone(),
            remember: saved.remember_secrets,
            every,
            unit,
            since: String::new(),
            saved,
            events_tx: tx,
            events_rx: rx,
            worker: None,
            busy_text: String::new(),
            tab: Tab::Written,
            report: Report {
                summary: "Run a preview or an update to see what goes into the sheet.".into(),
                ..Default::default()
            },
            log: Vec::new(),
            history_rows: Vec::new(),
            history_mtime: None,
            schedule_status: (String::new(), false),
            schedule: None,
            key_status: (String::from("\u{0}"), String::new(), Tone::Plain),
            position: (String::from("\u{0}"), String::new()),
            warned_no_secrets: false,
            dialog: None,
            cable: None,
            allow_close: false,
        };
        app.refresh_history();
        app
    }

    fn busy(&self) -> bool {
        self.worker.as_ref().is_some_and(|w| !w.is_finished())
    }

    fn log(&mut self, message: &str) {
        let now = chrono::Local::now().format("%H:%M:%S");
        self.log.push(format!("{now}  {message}"));
    }

    fn info(&mut self, message: impl Into<String>) {
        self.dialog = Some(Dialog { title: APP_NAME.into(), message: message.into(), error: false, confirm: None });
    }

    fn error(&mut self, message: impl Into<String>) {
        self.dialog = Some(Dialog { title: APP_NAME.into(), message: message.into(), error: true, confirm: None });
    }

    // ---- form ------------------------------------------------------------------------

    fn interval_minutes(&self, strict: bool) -> Result<u32, AppError> {
        match self.every.trim().parse::<u32>() {
            Ok(n) => Ok(n.saturating_mul(if self.unit == Unit::Hours { 60 } else { 1 })),
            Err(_) if !strict => Ok(self.saved.auto_update_minutes),
            Err(_) => Err(AppError::config("Enter how often to run the update, as a whole number.")),
        }
    }

    fn form(&self) -> SavedForm {
        SavedForm {
            region: self.region.clone(),
            client_id: self.client_id.trim().into(),
            client_secret: self.client_secret.trim().into(),
            service_account_json: self.key_text.trim().into(),
            sheet_id: self.sheet_id.trim().into(),
            remember_secrets: self.remember,
            auto_update_minutes: self.interval_minutes(false).unwrap_or(60),
        }
    }

    fn save_form(&mut self) {
        let form = self.form();
        if let Err(e) = form.save() {
            self.log(&format!("Could not save settings: {e}"));
            return;
        }
        self.saved = form.clone();
        if !form.remember_secrets && self.schedule.is_some() && !self.warned_no_secrets {
            self.warned_no_secrets = true;
            self.error(
                "Automatic updates are on, but 'Remember client secret and Google key on this computer' is not \
                 ticked, so they can't sign in and will fail. Tick it, or turn automatic updates off.",
            );
        }
    }

    fn update_key_status(&mut self) {
        if self.key_status.0 == self.key_text {
            return;
        }
        let text = self.key_text.trim();
        self.key_status = if text.is_empty() {
            (self.key_text.clone(), "Load service_account.json, or paste its contents above.".into(), Tone::Plain)
        } else {
            match parse_service_account(text) {
                Ok(info) => (
                    self.key_text.clone(),
                    format!(
                        "Service account: {}  (share the sheet with this address as an Editor)",
                        info["client_email"].as_str().unwrap_or("")
                    ),
                    Tone::Good,
                ),
                Err(e) => (self.key_text.clone(), e.text(), Tone::Bad),
            }
        };
    }

    fn update_position(&mut self, force: bool) {
        let key = format!("{}|{}|{}|{}", self.region, self.client_id, self.sheet_id, self.key_text.len());
        if !force && self.position.0 == key {
            return;
        }
        let ready = make_stratasync_settings(&self.region, &self.client_id, "-")
            .and_then(|ss| make_settings(&self.sheet_id, &self.key_text).map(|s| (ss, s)));
        let message = match ready {
            Err(_) => "(blank = continue from the last update)".to_string(),
            Ok((ss, settings)) => match last_position(&ss, &settings).last_sync_time {
                Some(t) => format!("(blank = continue from the last update: {})", timeutil::fmt_local(&t)),
                None => "(blank = from midnight today; no update run yet for this sheet)".to_string(),
            },
        };
        self.position = (key, message);
    }

    // ---- automatic updates -------------------------------------------------------------

    fn update_schedule_status(&mut self) {
        if !scheduler::supported() {
            self.schedule_status = ("Automatic updates are only available on Windows and macOS.".into(), false);
            return;
        }
        self.schedule = scheduler::current();
        let mut lines = Vec::new();
        match &self.schedule {
            None => lines.push("Off. Updates only run when you click Run update.".to_string()),
            Some(s) if s.minutes == 0 => lines
                .push("On, but set up in a way this app doesn't recognise. Turn on again to replace it.".to_string()),
            Some(s) => {
                lines.push(format!(
                    "On: every {} while you're signed in to this computer, with the settings saved here.",
                    scheduler::describe(s.minutes)
                ));
                if !s.is_current_app() {
                    lines.push(format!(
                        "It starts a different copy of this app ({}). Click Turn on to use this one.",
                        s.command.first().cloned().unwrap_or_default()
                    ));
                }
            }
        }
        let runs = history::recent_runs(200);
        let last = runs.iter().find(|r| r.get("trigger").map(String::as_str) == Some(history::AUTOMATIC));
        let mut failed = false;
        if let Some(last) = last {
            let get = |k: &str| last.get(k).cloned().unwrap_or_default();
            let status = get("status");
            failed = status == "failed";
            let outcome = if status == "ok" || status == "partial" {
                let mut o = format!("wrote {}, flagged {}", get("written"), get("flagged"));
                if status == "partial" {
                    o.push_str(" (some downloads failed; will retry)");
                }
                o
            } else {
                format!("{status}: {}", get("message"))
            };
            lines.push(format!("Last automatic update: {}, {outcome}", timeutil::fmt_iso_local(&get("run_at"))));
        }
        self.schedule_status = (lines.join("\n"), failed);
    }

    fn turn_on_schedule(&mut self) {
        let checked = self.interval_minutes(true).and_then(|m| {
            make_stratasync_settings(&self.region, &self.client_id, &self.client_secret)?;
            make_settings(&self.sheet_id, &self.key_text)?;
            Ok(m)
        });
        let minutes = match checked {
            Ok(m) => m,
            Err(e) => return self.error(e.text()),
        };
        if !self.remember {
            return self.error(
                "Automatic updates run without this window, so they need the client secret and Google key saved on \
                 this computer. Tick 'Remember client secret and Google key on this computer' first.",
            );
        }
        self.save_form();
        if let Err(e) = scheduler::install(minutes) {
            return self.error(e.text());
        }
        let every = scheduler::describe(minutes);
        self.log(&format!("Automatic updates on: every {every}"));
        self.update_schedule_status();
        self.info(format!(
            "Run update will happen every {every}, starting {every} from now, while you're signed in to this computer \
             and it's awake. The app doesn't need to be open.\n\nIt uses the settings saved here; changes you make in \
             this window are used from the next update on. Every update, with its flagged results, is recorded on the \
             History tab and in the history folder."
        ));
    }

    fn turn_off_schedule(&mut self) {
        if let Err(e) = scheduler::remove() {
            return self.error(e.text());
        }
        self.log("Automatic updates off");
        self.update_schedule_status();
    }

    /// Reload the History tab if runs.csv has changed (e.g. after an automatic update).
    fn refresh_history(&mut self) {
        let mtime = history::runs_mtime();
        if self.history_mtime == Some(mtime) {
            return;
        }
        self.history_mtime = Some(mtime);
        self.history_rows = history::recent_runs(200)
            .into_iter()
            .map(|r| {
                let get = |k: &str| r.get(k).cloned().unwrap_or_default();
                vec![
                    timeutil::fmt_iso_local(&get("run_at")),
                    get("trigger"),
                    get("status"),
                    get("written"),
                    get("flagged"),
                    get("message"),
                ]
            })
            .collect();
        self.update_position(true);
        self.update_schedule_status();
    }

    fn load_key_file(&mut self) {
        let Some(path) = rfd::FileDialog::new()
            .set_title("Choose the service account key")
            .add_filter("JSON key", &["json"])
            .add_filter("All files", &["*"])
            .pick_file()
        else {
            return;
        };
        let name = path.file_name().unwrap_or_default().to_string_lossy().to_string();
        match std::fs::read_to_string(&path)
            .map_err(|e| AppError::other(e.to_string()))
            .and_then(|t| parse_service_account(&t).map(|_| t))
        {
            Ok(text) => self.key_text = text.trim().to_string(),
            Err(e) => self.error(format!("Could not use {name}:\n\n{}", e.text())),
        }
    }

    // ---- running -----------------------------------------------------------------------

    fn start(&mut self, ctx: &egui::Context, apply: bool) {
        if self.busy() {
            return;
        }
        let ready = make_stratasync_settings(&self.region, &self.client_id, &self.client_secret).and_then(|ss| {
            let settings = make_settings(&self.sheet_id, &self.key_text)?;
            Ok((ss, settings, parse_since(&self.since)?))
        });
        let (ss, settings, since) = match ready {
            Ok(r) => r,
            Err(e) => return self.error(e.text()),
        };
        self.save_form();
        self.report =
            Report { summary: if apply { "Running…" } else { "Previewing…" }.into(), ..Default::default() };
        self.busy_text = if apply { "Running update…" } else { "Previewing…" }.into();
        self.tab = Tab::Log;
        let tx = self.events_tx.clone();
        let ctx = ctx.clone();
        self.worker = Some(std::thread::spawn(move || {
            let log = |m: &str| {
                let _ = tx.send(Event::Log(m.to_string()));
                ctx.request_repaint();
            };
            let result = run_sync(&ss, &settings, apply, since, &log, history::MANUAL);
            let _ = tx.send(Event::Synced(result));
            ctx.request_repaint();
        }));
    }

    fn background(
        &mut self,
        ctx: &egui::Context,
        busy: &str,
        work: impl FnOnce(&dyn Fn(&str)) -> Event + Send + 'static,
    ) -> bool {
        if self.busy() {
            return false;
        }
        self.busy_text = busy.into();
        let tx = self.events_tx.clone();
        let ctx = ctx.clone();
        self.worker = Some(std::thread::spawn(move || {
            let log = |m: &str| {
                let _ = tx.send(Event::Log(m.to_string()));
                ctx.request_repaint();
            };
            let event = work(&log);
            let _ = tx.send(event);
            ctx.request_repaint();
        }));
        true
    }

    fn handle_events(&mut self) {
        while let Ok(event) = self.events_rx.try_recv() {
            match event {
                Event::Log(m) => self.log(&m),
                Event::Synced(result) => {
                    self.finish();
                    match result {
                        Ok(r) => self.show_result(r),
                        Err(e) => {
                            self.log(&format!("Error: {}", e.text()));
                            self.report.summary = "The run failed; nothing was written. See the Log tab.".into();
                            self.error(e.text());
                        }
                    }
                }
                Event::CablePreviewed(inputs, result) => {
                    self.finish();
                    match result {
                        Ok((title, plan)) => self.show_cable_preview(inputs, title, plan),
                        Err(e) => self.cable_failed(e),
                    }
                }
                Event::CableReplaced(result) => {
                    self.finish();
                    match result {
                        Ok((title, plan)) => {
                            self.cable = None;
                            self.info(format!(
                                "Replaced {}: cleared {} rack(s) in '{title}'.",
                                plan.cable(),
                                plan.cleared()
                            ));
                        }
                        Err(e) => self.cable_failed(e),
                    }
                }
            }
        }
    }

    fn finish(&mut self) {
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
        self.busy_text.clear();
        self.update_position(true);
        self.history_mtime = None;
        self.refresh_history();
    }

    fn show_result(&mut self, r: SyncResult) {
        let plan = &r.plan;
        let written: Vec<Vec<String>> = plan
            .written_cells()
            .into_iter()
            .map(|(rd, power, stamp)| {
                vec![
                    rd.label.clone(),
                    power.tab.clone(),
                    format!("{}, {}", power.a1(), stamp.a1()),
                    rd.power_dbm.map(py_float).unwrap_or_default(),
                    rd.shown_time(),
                    rd.tech_name.clone().unwrap_or_default(),
                    rd.serial_no.clone().unwrap_or_default(),
                ]
            })
            .collect();
        let flagged: Vec<Vec<String>> = plan
            .flags
            .iter()
            .map(|f| {
                let rd = f.reading.as_ref();
                vec![
                    f.kind.clone(),
                    rd.map(|r| r.label.clone()).unwrap_or_default(),
                    f.message.clone(),
                    rd.map(|r| r.test_time.clone()).unwrap_or_default(),
                    rd.and_then(|r| r.tech_name.clone()).unwrap_or_default(),
                    rd.and_then(|r| r.serial_no.clone()).unwrap_or_default(),
                ]
            })
            .collect();
        // Flagged readings are listed in full on the Flagged tab.
        let skipped: Vec<Vec<String>> = plan
            .skipped_not_flagged()
            .into_iter()
            .map(|(rd, why)| vec![rd.label.clone(), why.clone(), rd.shown_time()])
            .collect();

        let verb = if r.applied { "Wrote" } else { "Would write" };
        let mut lines = vec![
            format!(
                "{} of '{}' — results received {} to {}.",
                if r.applied { "Update" } else { "Preview" },
                r.sheet_title,
                timeutil::fmt_local(&r.start),
                timeutil::fmt_local(&r.end)
            ),
            format!(
                "StrataSync: {} result(s), {} TruePON, {} new.  {verb} {} result(s) ({} cells).  \
                 Flagged (not written): {}.  Skipped: {}.",
                r.found,
                r.truepon,
                r.new,
                plan.written.len(),
                plan.updates.len(),
                plan.flags.len(),
                skipped.len()
            ),
        ];
        if !r.missing_tabs.is_empty() {
            lines.push(format!("Tabs not found in the sheet: {}.", r.missing_tabs.join(", ")));
        }
        if !r.download_failures.is_empty() {
            lines.push(format!(
                "{} download(s) failed, so the download position was not moved; the next update will retry them.",
                r.download_failures.len()
            ));
        }
        if !r.applied {
            lines.push("Preview only: nothing was written. Click Run update to write these results.".into());
        }
        self.tab = if !plan.flags.is_empty() && plan.written.is_empty() { Tab::Flagged } else { Tab::Written };
        self.report = Report { summary: lines.join("\n"), written, flagged, skipped, result: Some(r) };
    }

    fn export_flags(&mut self) {
        let Some(result) = self.report.result.as_ref().filter(|r| !r.plan.flags.is_empty()) else { return };
        let default = format!("flagged-{}.csv", chrono::Local::now().format("%Y-%m-%d_%H%M"));
        let Some(path) = rfd::FileDialog::new()
            .set_title("Save flagged results")
            .set_file_name(&default)
            .add_filter("CSV", &["csv"])
            .save_file()
        else {
            return;
        };
        let n = result.plan.flags.len();
        match write_flags_csv(&result.plan, &path) {
            Ok(()) => self.log(&format!("Saved {n} flagged result(s) to {}", path.display())),
            Err(e) => self.error(format!("Could not save {}:\n\n{e}", path.display())),
        }
    }

    // ---- cable replacement ---------------------------------------------------------------

    fn open_cable_dialog(&mut self) {
        match make_settings(&self.sheet_id, &self.key_text) {
            Err(e) => self.error(e.text()),
            Ok(settings) => {
                self.save_form();
                self.cable = Some(CableDialog {
                    settings,
                    data_hall: DATA_HALLS[0].into(),
                    row: String::new(),
                    channel: "A".into(),
                    when: chrono::Local::now().format("%Y-%m-%d %H:%M").to_string(),
                    status: "Choose the cable and click Preview to see which racks will be cleared.".into(),
                    rows: Vec::new(),
                    previewed: None,
                });
            }
        }
    }

    fn cable_inputs(d: &CableDialog) -> Result<CableInputs, AppError> {
        let row = d.row.trim().parse::<i64>().map_err(|_| AppError::config("Enter the row number."))?;
        Ok(CableInputs {
            data_hall: d.data_hall.clone(),
            row,
            channel: d.channel.clone(),
            replaced_at: parse_when(&d.when)?,
        })
    }

    fn cable_preview(&mut self, ctx: &egui::Context) {
        let Some(d) = self.cable.as_mut() else { return };
        let inputs = match Self::cable_inputs(d) {
            Ok(i) => i,
            Err(e) => return self.error(e.text()),
        };
        let settings = d.settings.clone();
        d.status = "Reading the sheet…".into();
        let started = self.background(ctx, "Reading the sheet…", move |log| {
            let result = replace_cable(
                &settings,
                &inputs.data_hall,
                inputs.row,
                &inputs.channel,
                inputs.replaced_at,
                false,
                log,
            );
            Event::CablePreviewed(inputs, result)
        });
        if !started && let Some(d) = self.cable.as_mut() {
            d.status = "Wait for the current run to finish, then click Preview again.".into();
        }
    }

    fn show_cable_preview(&mut self, inputs: CableInputs, title: String, plan: CableReplacementPlan) {
        let Some(d) = self.cable.as_mut() else { return };
        d.rows = plan
            .racks
            .iter()
            .map(|r| {
                let action = if r.cleared {
                    format!("clear, set timestamp to {}", plan.replaced_at)
                } else {
                    format!("leave: {}", r.reason)
                };
                vec![r.rack.to_string(), r.old_power.clone(), r.old_timestamp.clone(), action]
            })
            .collect();
        let cleared = plan.cleared();
        d.status = format!(
            "{} in '{title}': {} rack(s), {cleared} to clear. Nothing has been changed yet.",
            plan.cable(),
            plan.racks.len()
        );
        d.previewed = Some((inputs, title, plan.cable(), plan.replaced_at.clone(), cleared));
    }

    fn cable_confirm(&mut self) {
        let Some((_, title, cable, stamp, cleared)) = self.cable.as_ref().and_then(|d| d.previewed.clone()) else {
            return;
        };
        self.dialog = Some(Dialog {
            title: "Replace cable".into(),
            message: format!(
                "Replace {cable} in '{title}'?\n\nThe dBm of {cleared} rack(s) will be cleared and their timestamps \
                 set to {stamp}. Tests on this cable from before that time will be ignored from now on.\n\nThe old \
                 values can only be recovered from the sheet's version history."
            ),
            error: false,
            confirm: Some(("Replace cable".into(), Confirm::ReplaceCable)),
        });
    }

    fn cable_apply(&mut self, ctx: &egui::Context) {
        let Some(d) = self.cable.as_mut() else { return };
        let Some((inputs, ..)) = d.previewed.clone() else { return };
        let settings = d.settings.clone();
        d.status = "Writing…".into();
        self.background(ctx, "Replacing cable…", move |log| {
            Event::CableReplaced(replace_cable(
                &settings,
                &inputs.data_hall,
                inputs.row,
                &inputs.channel,
                inputs.replaced_at,
                true,
                log,
            ))
        });
    }

    fn cable_failed(&mut self, e: AppError) {
        self.log(&format!("Error: {}", e.text()));
        if let Some(d) = self.cable.as_mut() {
            d.status = "Failed; see the message.".into();
        }
        self.error(e.text());
    }

    // ---- drawing ---------------------------------------------------------------------------

    fn ui_stratasync(&mut self, ui: &mut egui::Ui) {
        section(ui, "StrataSync", |ui| {
            egui::Grid::new("ss").num_columns(2).spacing([8.0, 6.0]).show(ui, |ui| {
                ui.label("Server:");
                ui.horizontal(|ui| {
                    for (name, url) in STRATASYNC_URLS {
                        ui.radio_value(&mut self.region, name.to_string(), format!("{name}  ({})", &url[8..]));
                        ui.add_space(12.0);
                    }
                });
                ui.end_row();
                ui.label("Client ID:");
                ui.add(egui::TextEdit::singleline(&mut self.client_id).desired_width(f32::INFINITY));
                ui.end_row();
                ui.label("Client secret:");
                ui.horizontal(|ui| {
                    ui.add(
                        egui::TextEdit::singleline(&mut self.client_secret)
                            .password(!self.show_secret)
                            .desired_width(ui.available_width() - 70.0),
                    );
                    ui.checkbox(&mut self.show_secret, "Show");
                });
                ui.end_row();
            });
        });
    }

    fn ui_google(&mut self, ui: &mut egui::Ui) {
        section(ui, "Google Sheet", |ui| {
            egui::Grid::new("gs").num_columns(2).spacing([8.0, 6.0]).show(ui, |ui| {
                ui.label("Sheet ID or URL:");
                ui.add(egui::TextEdit::singleline(&mut self.sheet_id).desired_width(f32::INFINITY));
                ui.end_row();
                ui.label("Service account key:");
                ui.horizontal(|ui| {
                    egui::ScrollArea::vertical()
                        .id_salt("key")
                        .max_height(72.0)
                        .max_width(ui.available_width() - 90.0)
                        .show(ui, |ui| {
                            ui.add(
                                egui::TextEdit::multiline(&mut self.key_text)
                                    .code_editor()
                                    .desired_rows(4)
                                    .desired_width(f32::INFINITY),
                            );
                        });
                    ui.vertical(|ui| {
                        if ui.button("Load file…").clicked() {
                            self.load_key_file();
                        }
                        if ui.button("Clear").clicked() {
                            self.key_text.clear();
                        }
                    });
                });
                ui.end_row();
                ui.label("");
                self.update_key_status();
                let (_, text, tone) = &self.key_status;
                let color = match tone {
                    Tone::Plain => ui.visuals().weak_text_color(),
                    Tone::Good => green(ui),
                    Tone::Bad => red(ui),
                };
                ui.label(RichText::new(text).color(color));
                ui.end_row();
            });
        });
    }

    fn ui_schedule(&mut self, ui: &mut egui::Ui) {
        section(ui, "Automatic updates", |ui| {
            ui.horizontal(|ui| {
                ui.label("Run update every");
                ui.add(egui::TextEdit::singleline(&mut self.every).desired_width(40.0));
                egui::ComboBox::from_id_salt("unit")
                    .width(80.0)
                    .selected_text(if self.unit == Unit::Hours { "hours" } else { "minutes" })
                    .show_ui(ui, |ui| {
                        ui.selectable_value(&mut self.unit, Unit::Minutes, "minutes");
                        ui.selectable_value(&mut self.unit, Unit::Hours, "hours");
                    });
                ui.add_space(8.0);
                let supported = scheduler::supported();
                let label =
                    if self.schedule.as_ref().is_some_and(|s| s.is_current_app()) { "Update" } else { "Turn on" };
                if ui.add_enabled(supported, egui::Button::new(label)).clicked() {
                    self.turn_on_schedule();
                }
                if ui.add_enabled(supported && self.schedule.is_some(), egui::Button::new("Turn off")).clicked() {
                    self.turn_off_schedule();
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.button("Open history folder").clicked() {
                        open_folder(&history::history_dir());
                    }
                });
            });
            let (text, failed) = &self.schedule_status;
            let color = if *failed { red(ui) } else { ui.visuals().weak_text_color() };
            ui.label(RichText::new(text).color(color));
        });
    }

    fn ui_actions(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.label("Search from:");
            ui.add(egui::TextEdit::singleline(&mut self.since).desired_width(170.0).hint_text("YYYY-MM-DD HH:MM"));
            self.update_position(false);
            ui.label(RichText::new(&self.position.1).color(ui.visuals().weak_text_color()));
        });
        ui.horizontal(|ui| {
            ui.checkbox(&mut self.remember, "Remember client secret and Google key on this computer");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let idle = !self.busy();
                if ui.add_enabled(idle, egui::Button::new(RichText::new("Run update").strong())).clicked() {
                    self.start(ctx, true);
                }
                if ui.add_enabled(idle, egui::Button::new("Preview (no changes)")).clicked() {
                    self.start(ctx, false);
                }
                ui.add_space(24.0);
                if ui.add_enabled(idle, egui::Button::new("Replace cable…")).clicked() {
                    self.open_cable_dialog();
                }
                if !idle {
                    ui.add_space(12.0);
                    ui.label(&self.busy_text);
                    ui.spinner();
                }
            });
        });
        ui.add_space(6.0);
    }

    fn ui_report(&mut self, ui: &mut egui::Ui) {
        section(ui, "Report", |ui| {
            ui.label(&self.report.summary);
            ui.add_space(4.0);
            let has_result = self.report.result.is_some();
            let count = |n: usize, name: &str| if has_result { format!("{name} ({n})") } else { name.to_string() };
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.tab, Tab::Written, count(self.report.written.len(), "Written"));
                ui.selectable_value(&mut self.tab, Tab::Flagged, count(self.report.flagged.len(), "Flagged"));
                ui.selectable_value(&mut self.tab, Tab::Skipped, count(self.report.skipped.len(), "Skipped"));
                ui.selectable_value(&mut self.tab, Tab::Log, "Log");
                ui.selectable_value(&mut self.tab, Tab::History, "History");
            });
            ui.separator();
            match self.tab {
                Tab::Written => table(
                    ui,
                    "written",
                    &[
                        ("Location", 150.0),
                        ("Tab", 150.0),
                        ("Cells", 80.0),
                        ("dBm", 60.0),
                        ("Test time", 200.0),
                        ("Tech", 130.0),
                        ("Serial", 90.0),
                    ],
                    &self.report.written,
                    0.0,
                ),
                Tab::Flagged => {
                    let footer = 30.0;
                    table(
                        ui,
                        "flagged",
                        &[
                            ("Problem", 110.0),
                            ("Location", 140.0),
                            ("Details", 330.0),
                            ("Test time", 180.0),
                            ("Tech", 120.0),
                            ("Serial", 90.0),
                        ],
                        &self.report.flagged,
                        footer,
                    );
                    ui.horizontal(|ui| {
                        ui.label(
                            RichText::new(
                                "Flagged results are not written to the sheet. Retest the location to write it.",
                            )
                            .color(ui.visuals().weak_text_color()),
                        );
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui
                                .add_enabled(
                                    !self.report.flagged.is_empty(),
                                    egui::Button::new("Export flagged to CSV…"),
                                )
                                .clicked()
                            {
                                self.export_flags();
                            }
                        });
                    });
                }
                Tab::Skipped => table(
                    ui,
                    "skipped",
                    &[("Location", 180.0), ("Reason", 380.0), ("Test time", 200.0)],
                    &self.report.skipped,
                    0.0,
                ),
                Tab::Log => {
                    egui::ScrollArea::vertical().id_salt("log").stick_to_bottom(true).auto_shrink([false, false]).show(
                        ui,
                        |ui| {
                            for line in &self.log {
                                ui.add(egui::Label::new(RichText::new(line).monospace()).wrap());
                            }
                        },
                    );
                }
                Tab::History => {
                    table(
                        ui,
                        "history",
                        &[
                            ("Update at", 150.0),
                            ("Started", 80.0),
                            ("Outcome", 70.0),
                            ("Written", 60.0),
                            ("Flagged", 60.0),
                            ("Details", 400.0),
                        ],
                        &self.history_rows,
                        40.0,
                    );
                    ui.label(
                        RichText::new(
                            "Every update that wrote to the sheet, including automatic ones. The flagged and written \
                             results of each are in flagged.csv and written.csv in the history folder.",
                        )
                        .color(ui.visuals().weak_text_color()),
                    );
                }
            }
        });
    }

    fn ui_cable(&mut self, ctx: &egui::Context) {
        let Some(d) = self.cable.as_mut() else { return };
        let mut open = true;
        let (mut preview, mut apply, mut cancel) = (false, false, false);
        let busy = self.worker.as_ref().is_some_and(|w| !w.is_finished());
        egui::Window::new("Replace cable")
            .open(&mut open)
            .default_width(680.0)
            .min_width(640.0)
            .resizable([true, false])
            .collapsible(false)
            .show(ctx, |ui| {
                let before = (d.data_hall.clone(), d.row.clone(), d.channel.clone(), d.when.clone());
                ui.horizontal(|ui| {
                    ui.label("Data hall:");
                    egui::ComboBox::from_id_salt("dh").width(50.0).selected_text(&d.data_hall).show_ui(ui, |ui| {
                        for dh in DATA_HALLS {
                            ui.selectable_value(&mut d.data_hall, dh.to_string(), dh);
                        }
                    });
                    ui.add_space(12.0);
                    ui.label("Row:");
                    ui.add(egui::TextEdit::singleline(&mut d.row).desired_width(50.0));
                    ui.add_space(12.0);
                    ui.label("Channel:");
                    ui.radio_value(&mut d.channel, "A".to_string(), "A");
                    ui.radio_value(&mut d.channel, "B".to_string(), "B");
                });
                ui.horizontal(|ui| {
                    ui.label("Replaced at:");
                    ui.add(egui::TextEdit::singleline(&mut d.when).desired_width(150.0));
                    if ui.add_enabled(!busy, egui::Button::new("Preview")).clicked() {
                        preview = true;
                    }
                });
                ui.label(
                    RichText::new(format!(
                        "This computer's time (UTC{}). Tests on this cable made before this time will be ignored.",
                        timeutil::local_offset_text()
                    ))
                    .color(ui.visuals().weak_text_color()),
                );
                if before != (d.data_hall.clone(), d.row.clone(), d.channel.clone(), d.when.clone()) {
                    d.previewed = None;
                }
                ui.separator();
                ui.label(&d.status);
                table(
                    ui,
                    "cable",
                    &[("Rack", 60.0), ("Current dBm", 100.0), ("Current timestamp", 210.0), ("Action", 250.0)],
                    &d.rows,
                    -280.0,
                );
                ui.separator();
                ui.horizontal(|ui| {
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        let can_apply = !busy && d.previewed.as_ref().is_some_and(|p| p.4 > 0);
                        if ui.add_enabled(can_apply, egui::Button::new("Replace cable")).clicked() {
                            apply = true;
                        }
                        if ui.button("Cancel").clicked() {
                            cancel = true;
                        }
                    });
                });
            });
        if !open || cancel {
            self.cable = None;
        } else if preview {
            self.cable_preview(ctx);
        } else if apply {
            self.cable_confirm();
        }
    }

    fn ui_dialog(&mut self, ctx: &egui::Context) {
        let Some(dialog) = self.dialog.clone() else { return };
        let mut close = false;
        let mut confirmed = None;
        egui::Modal::new(egui::Id::unique("dialog")).show(ctx, |ui| {
            ui.set_max_width(460.0);
            ui.heading(&dialog.title);
            ui.add_space(6.0);
            let text = RichText::new(&dialog.message);
            ui.label(if dialog.error { text.color(red(ui)) } else { text });
            ui.add_space(10.0);
            ui.horizontal(|ui| {
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| match &dialog.confirm {
                    Some((label, action)) => {
                        if ui.button(label.as_str()).clicked() {
                            confirmed = Some(action.clone());
                        }
                        if ui.button("Cancel").clicked() {
                            close = true;
                        }
                    }
                    None => {
                        if ui.button("OK").clicked() {
                            close = true;
                        }
                    }
                });
            });
        });
        if close || confirmed.is_some() {
            self.dialog = None;
        }
        match confirmed {
            Some(Confirm::ReplaceCable) => self.cable_apply(ctx),
            Some(Confirm::Quit) => {
                self.allow_close = true;
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            }
            None => {}
        }
    }
}

/// A titled box, like the Tk app's LabelFrame.
fn section<R>(ui: &mut egui::Ui, title: &str, add: impl FnOnce(&mut egui::Ui) -> R) -> R {
    ui.label(RichText::new(title).strong());
    let inner = egui::Frame::group(ui.style()).show(ui, |ui| {
        ui.set_width(ui.available_width());
        add(ui)
    });
    ui.add_space(6.0);
    inner.inner
}

/// A scrolling table with resizable columns. `reserve` keeps room below it;
/// a negative `reserve` is a fixed height instead (for use inside windows).
fn table(ui: &mut egui::Ui, id: &str, columns: &[(&str, f32)], rows: &[Vec<String>], reserve: f32) {
    let height = if reserve < 0.0 { -reserve } else { (ui.available_height() - reserve - 12.0).max(80.0) };
    let mut builder = TableBuilder::new(ui)
        .id_salt(id)
        .striped(true)
        .resizable(true)
        .auto_shrink([false, false])
        .min_scrolled_height(height)
        .max_scroll_height(height)
        .cell_layout(egui::Layout::left_to_right(egui::Align::Center));
    for (i, (_, width)) in columns.iter().enumerate() {
        builder = if i + 1 == columns.len() {
            builder.column(Column::remainder().at_least(60.0).clip(true))
        } else {
            builder.column(Column::initial(*width).at_least(40.0).clip(true))
        };
    }
    builder
        .header(ROW_HEIGHT + 2.0, |mut header| {
            for (name, _) in columns {
                header.col(|ui| {
                    ui.strong(*name);
                });
            }
        })
        .body(|body| {
            body.rows(ROW_HEIGHT, rows.len(), |mut row| {
                let cells = &rows[row.index()];
                for i in 0..columns.len() {
                    row.col(|ui| {
                        let text = cells.get(i).map(String::as_str).unwrap_or("");
                        ui.add(egui::Label::new(text).truncate()).on_hover_text(text);
                    });
                }
            });
        });
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        self.handle_events();

        // Every ~5 s, pick up updates made by automatic runs.
        if (ctx.input(|i| i.time) as u64).is_multiple_of(5) {
            self.refresh_history();
        }
        ctx.request_repaint_after(Duration::from_secs(5));

        if ctx.input(|i| i.viewport().close_requested()) && !self.allow_close {
            if self.busy() {
                ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
                self.dialog = Some(Dialog {
                    title: APP_NAME.into(),
                    message: "An update is still running. Quit anyway?".into(),
                    error: false,
                    confirm: Some(("Quit".into(), Confirm::Quit)),
                });
            } else {
                self.save_form();
            }
        }

        egui::CentralPanel::default_margins().show(ui, |ui| {
            self.ui_stratasync(ui);
            self.ui_google(ui);
            self.ui_schedule(ui);
            self.ui_actions(ui, &ctx);
            self.ui_report(ui);
        });
        self.ui_cable(&ctx);
        self.ui_dialog(&ctx);
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        if !self.busy() {
            self.save_form();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn search_from_and_replaced_at() {
        assert_eq!(parse_since("  ").unwrap(), None);
        let midnight = parse_since("2026-10-22").unwrap().unwrap();
        assert_eq!(timeutil::to_local(&midnight).format("%Y-%m-%d %H:%M").to_string(), "2026-10-22 00:00");
        assert!(parse_since("2026-10-22 08:00").unwrap().is_some());
        assert_eq!(
            parse_since("2026-10-22T08:00:00-04:00").unwrap().map(|t| timeutil::iso(&t)),
            Some("2026-10-22T08:00:00-04:00".into())
        );
        assert!(parse_since("last week").is_err());
        assert!(parse_when("2026-10-30").is_err());
        assert!(parse_when("2026-10-30 14:30").is_ok());
        assert!(parse_when("").is_err());
    }
}
