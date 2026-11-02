//! Download new TruePON results from StrataSync and write them to the PON Audit sheet.
//!
//! Each run picks up where the last one left off (the download position is kept
//! in a state file per StrataSync region, client ID and sheet). The first run,
//! with no saved state, starts from midnight today (local time).
//!
//! Results are read straight from the StrataSync search response, which carries
//! the same values as the full test files, so no per-file downloads are needed.
//! Full files are only downloaded for tests with no power reading (to get the
//! signal status for the flag message) and tests of several PON types.
//!
//! Nothing is written, and the saved position doesn't move, unless `apply` is set.
//! Every applied run is recorded in the update history (see history.rs).
//!
//! Only one run at a time, per computer: a run holds `run_lock()`, so an automatic
//! update and one started from the window can't overlap (VIAVI allows only one
//! valid token per client ID, and both would move the same download position).

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};

use chrono::Duration;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::config::{Settings, StrataSyncSettings, app_data_dir};
use crate::error::{AppError, Result};
use crate::history::{self, FLAG_FIELDS, Log};
use crate::pon_audit::{
    CableReplacementPlan, Flag, RACK_KEY_HEADERS, TabIndex, UpdatePlan, audit_tabs, build_indexes, is_multi_service,
    json_text, plan_cable_replacement, plan_updates, rack_tab, reading_from_search, readings_from_result,
    record_source,
};
use crate::sheets::SheetWriter;
use crate::stratasync::{DownloadError, SYNC_TIME, StrataSyncClient};
use crate::sync_state::SyncState;
use crate::timeutil::{self, Time};

pub const TEST_TYPE: &str = "truepon";

/// Holds the per-computer update lock until dropped.
pub struct RunLock {
    _file: File,
}

/// Take the per-computer update lock, or fail with RunInProgress.
///
/// An OS file lock, so it's released even if the process is killed.
pub fn run_lock() -> Result<RunLock> {
    let path = app_data_dir().join("run.lock");
    fs::create_dir_all(app_data_dir())?;
    let file = OpenOptions::new().create(true).append(true).read(true).open(&path)?;
    match file.try_lock() {
        Ok(()) => Ok(RunLock { _file: file }),
        Err(_) => Err(AppError::RunInProgress(
            "Another update is running on this computer (perhaps an automatic one). Try again in a minute.".into(),
        )),
    }
}

#[derive(Debug, Clone)]
pub struct SyncResult {
    pub start: Time,
    pub end: Time,
    pub applied: bool,
    pub sheet_title: String,
    pub found: usize,
    pub truepon: usize,
    pub new: usize,
    pub plan: UpdatePlan,
    pub missing_tabs: Vec<String>,
    pub download_failures: Vec<String>,
    pub cells_written: usize,
    pub position_saved: Option<Time>,
}

/// One download position per StrataSync server, client ID and sheet, so
/// switching any of them doesn't skip results the other combination hasn't seen.
pub fn state_file(ss: &StrataSyncSettings, settings: &Settings) -> PathBuf {
    let key = format!("{}|{}|{}", ss.base_url, ss.client_id.as_deref().unwrap_or("None"), settings.sheet_id);
    let digest = Sha256::digest(key.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    app_data_dir().join("state").join(format!("last_download-{}.json", &hex[..12]))
}

pub fn last_position(ss: &StrataSyncSettings, settings: &Settings) -> SyncState {
    SyncState::load(&state_file(ss, settings)).unwrap_or_default()
}

/// Search StrataSync, plan the sheet updates and (if `apply`) write them.
///
/// Applied runs, and their failures, are recorded in the history under `trigger`.
pub fn run_sync(
    ss: &StrataSyncSettings,
    settings: &Settings,
    apply: bool,
    since: Option<Time>,
    log: Log,
    trigger: &str,
) -> Result<SyncResult> {
    let _lock = run_lock()?;
    if !apply {
        return run_sync_locked(ss, settings, false, since, log);
    }
    match run_sync_locked(ss, settings, true, since, log) {
        Ok(result) => {
            history::record_run(&result, trigger, log);
            Ok(result)
        }
        Err(e) => {
            history::record_status(trigger, "failed", &settings.sheet_id, &e.text(), log);
            Err(e)
        }
    }
}

fn run_sync_locked(
    ss: &StrataSyncSettings,
    settings: &Settings,
    apply: bool,
    since: Option<Time>,
    log: Log,
) -> Result<SyncResult> {
    let path = state_file(ss, settings);
    let mut state = SyncState::load(&path)
        .map_err(|e| AppError::other(format!("Could not read the download position ({}): {e}", path.display())))?;
    let run_at = timeutil::now_utc();
    let start = since.or(state.last_sync_time).unwrap_or_else(timeutil::local_midnight_today);
    let mut result = SyncResult {
        start,
        end: run_at,
        applied: apply,
        sheet_title: String::new(),
        found: 0,
        truepon: 0,
        new: 0,
        plan: UpdatePlan::default(),
        missing_tabs: Vec::new(),
        download_failures: Vec::new(),
        cells_written: 0,
        position_saved: None,
    };

    log(&format!("Searching StrataSync ({}) for results received {} -> now", ss.base_url, timeutil::fmt_local(&start)));
    let mut client = StrataSyncClient::new(ss.clone())?;
    let found = client.search_between(start, run_at, SYNC_TIME)?;
    let records: Vec<Value> =
        found.iter().filter(|r| r.get("type").and_then(Value::as_str) == Some(TEST_TYPE)).cloned().collect();
    let mut new: Vec<&Value> = records.iter().filter(|r| since.is_some() || !state.is_already_processed(r)).collect();
    (result.found, result.truepon, result.new) = (found.len(), records.len(), new.len());
    log(&format!("Found {} result(s): {} {TEST_TYPE}, {} new", found.len(), records.len(), new.len()));

    let mut readings = Vec::new();
    // A record that can't be read is flagged rather than stopping the run, and
    // doesn't hold back the download position (it would fail the same way every time).
    let mut unreadable = Vec::new();
    new.sort_by_key(|r| r.get("syncTime").and_then(Value::as_str).unwrap_or("").to_string());
    for rec in new {
        let source = record_source(rec);
        let has_power =
            !matches!(rec.get("power"), None | Some(Value::Null)) && rec.get("power") != Some(&Value::from(""));
        if has_power && !is_multi_service(rec) {
            match reading_from_search(rec) {
                Ok(r) => {
                    readings.push(r);
                    continue;
                }
                Err(e) => log(&format!("Unexpected search values for {source}: {e}")),
            }
        }
        let why =
            if is_multi_service(rec) { "tested several PON types" } else { "no usable power reading in search result" };
        log(&format!("Downloading {} ({why})", json_text(rec.get("name"))));
        let label = rec
            .get("testLocationsLabel")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| source.clone());
        let Some(id) = rec.get("_id").and_then(Value::as_i64) else {
            unreadable.push(Flag::new("unreadable", format!("{label}: the search result has no usable _id"), None));
            continue;
        };
        let doc = match client.download_json(id) {
            Ok(doc) => doc,
            Err(DownloadError::Fetch(e)) => {
                result.download_failures.push(format!("{source}: {}", e.text()));
                log(&format!("  could not download {source}: {}", e.text()));
                continue;
            }
            Err(DownloadError::Unreadable(e)) => {
                unreadable.push(Flag::new("unreadable", format!("{label}: test file could not be read ({e})"), None));
                continue;
            }
        };
        let sync_time = rec.get("syncTime").and_then(Value::as_str);
        match readings_from_result(&doc, Some(&source), sync_time) {
            Ok(rs) => readings.extend(rs),
            Err(e) => unreadable.push(Flag::new("unreadable", format!("{label}: {e}"), None)),
        }
    }

    log("Opening the Google Sheet");
    let mut writer = SheetWriter::open(settings)?;
    result.sheet_title = writer.title().to_string();
    let tabs = writer.read_tabs(&audit_tabs())?;
    result.missing_tabs = audit_tabs().into_iter().filter(|t| !tabs.contains_key(t)).collect();
    if !result.missing_tabs.is_empty() {
        log(&format!("Warning: tabs not found: {}", result.missing_tabs.join(", ")));
    }
    result.plan = plan_updates(readings, &build_indexes(&tabs)?, false)?;
    result.plan.flags.extend(unreadable);
    log(&format!("Planned {} result(s) to write, {} flag(s)", result.plan.written.len(), result.plan.flags.len()));

    if !apply {
        log("Preview only: nothing was written and the download position was not moved.");
        return Ok(result);
    }

    result.cells_written = writer.write_cells(&result.plan.updates)?;
    log(&format!("Wrote {} cell(s) to '{}'", result.cells_written, result.sheet_title));
    if !result.download_failures.is_empty() {
        log("Some downloads failed, so the download position was NOT moved; the next run will retry them.");
        return Ok(result);
    }
    state.advance(&records, run_at);
    state.save(&path)?;
    result.position_saved = state.last_sync_time;
    if let Some(t) = state.last_sync_time {
        log(&format!("Saved download position: {}", timeutil::fmt_local(&t)));
    }
    Ok(result)
}

/// Plan (and if `apply`, write) the clearing of one cable's results.
///
/// Returns (sheet title, plan). The sheet is read afresh each time, so applying
/// never relies on an earlier preview's values.
pub fn replace_cable(
    settings: &Settings,
    data_hall: &str,
    row: i64,
    channel: &str,
    replaced_at: Time,
    apply: bool,
    log: Log,
) -> Result<(String, CableReplacementPlan)> {
    if replaced_at > timeutil::now_utc() + Duration::minutes(5) {
        return Err(AppError::config(
            "The replacement time is in the future. Every test before it would be ignored, including tests of the new cable.",
        ));
    }
    let _lock = run_lock()?;
    let mut writer = SheetWriter::open(settings)?;
    let tab = rack_tab(data_hall);
    let mut tabs: HashMap<String, Vec<Vec<String>>> = writer.read_tabs(std::slice::from_ref(&tab))?;
    let Some(values) = tabs.remove(&tab) else {
        return Err(AppError::config(format!("Tab '{tab}' not found in '{}'.", writer.title())));
    };
    let index = TabIndex::build(&tab, values, &RACK_KEY_HEADERS)?;
    let plan = plan_cable_replacement(&index, data_hall, row, channel, replaced_at)?;
    let title = writer.title().to_string();
    if !apply {
        return Ok((title, plan));
    }
    let n = writer.write_cells(&plan.updates)?;
    let cleared = plan.cleared();
    let left =
        if cleared < plan.racks.len() { format!(", left {} alone", plan.racks.len() - cleared) } else { String::new() };
    log(&format!(
        "Cable replaced: {} at {}. Cleared {cleared} rack(s) ({n} cells) in '{tab}'{left}",
        plan.cable(),
        plan.replaced_at
    ));
    Ok((title, plan))
}

pub fn write_flags_csv(plan: &UpdatePlan, path: &Path) -> std::io::Result<()> {
    let rows: Vec<Vec<String>> = plan.flags.iter().map(history::flag_row).collect();
    history::write_csv(path, &FLAG_FIELDS, &rows, false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::make_stratasync_settings;

    #[test]
    fn state_file_name_matches_python() {
        // hashlib.sha256(b"https://stratasync.viavisolutions.com|abc|SHEET").hexdigest()[:12]
        let ss = make_stratasync_settings("USA", "abc", "s").unwrap();
        let settings = Settings { service_account_info: Default::default(), sheet_id: "SHEET".into() };
        let name = state_file(&ss, &settings).file_name().unwrap().to_string_lossy().to_string();
        assert_eq!(name, "last_download-79e658d3b507.json");
    }

    /// A whole update against a fake StrataSync server and a fake sheet behind the
    /// real bridge protocol: preview, apply, then a re-run that finds nothing new.
    #[cfg(unix)]
    #[test]
    fn end_to_end() {
        use crate::testutil::serve;
        use std::os::unix::fs::PermissionsExt;

        let dir = std::env::temp_dir().join(format!("pon-audit-e2e-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let sheet = dir.join("sheet.json");
        fs::write(
            &sheet,
            serde_json::json!({
                "RMN2.DHA PON Audit": [
                    ["DH", "Row", "Rack", "OLT and Port (A)", "dBm in Port 1", "Timestamp P1", "OLT and Port  (B)", "dBm in Port 2", "Timestamp P2"],
                    ["A", "12", "25", "01 17 AA 01", "", "", "01 17 BB 01", "", ""],
                    ["A", "12", "26", "01 17 AA 02", "", "", "01 17 BB 02", "", ""]
                ],
                "RMN2 F1 & F2": [["DH", "Row", "Channel"]]
            })
            .to_string(),
        )
        .unwrap();
        let wrapper = dir.join("bridge.sh");
        let fake = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests").join("fake_bridge.py");
        fs::write(&wrapper, format!("#!/bin/sh\nFAKE_SHEET='{}' exec python3 '{}'\n", sheet.display(), fake.display()))
            .unwrap();
        fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();
        // SAFETY: only this test reads these variables.
        unsafe {
            std::env::set_var("PON_AUDIT_DATA_DIR", dir.join("data"));
            std::env::set_var("PON_AUDIT_SHEETS_BRIDGE", &wrapper);
        }

        const TOKEN: &str = r#"{"access_token": "T", "expires_in": 900}"#;
        // Sync times shortly before now, so the re-run searches a single window.
        let t1 = timeutil::iso_utc_z(&(timeutil::now_utc() - Duration::minutes(60)));
        let t2 = timeutil::iso_utc_z(&(timeutil::now_utc() - Duration::minutes(30)));
        let search: &'static str = Box::leak(
            r#"{"type": "success", "result": [
            {"_id": 1, "type": "truepon", "name": "good.json", "testLocationsLabel": "RMN2.DHA.12.25A",
             "assetLocalTestTime": "2026-10-29T08:00:00-04:00", "power": "-18.5", "oltId": "01 17 AA 01",
             "syncTime": "2026-10-29T12:05:00Z", "userFirstName": "Pat"},
            {"_id": 2, "type": "truepon", "name": "wrong-olt.json", "testLocationsLabel": "RMN2.DHA.12.25B",
             "assetLocalTestTime": "2026-10-29T08:01:00-04:00", "power": "-18.0", "oltId": "01 17 BB 09",
             "syncTime": "2026-10-29T12:05:00Z"},
            {"_id": 3, "type": "truepon", "name": "multi.json", "ponServices": "gpon;xgspon", "power": "-21;-17",
             "syncTime": "2026-10-29T12:10:00Z"},
            {"_id": 4, "type": "otdr", "name": "other.json", "syncTime": "2026-10-29T12:10:00Z"}
        ]}"#
            .replace("2026-10-29T12:05:00Z", &t1)
            .replace("2026-10-29T12:10:00Z", &t2)
            .into_boxed_str(),
        );
        const FILE: &str = r#"{"tests": [{"type": "truepon",
            "configuration": {"signalSetup": "gpon;xgspon"},
            "results": {"testTime": "2026-10-29T08:10:00-04:00", "data": {"xgspon": {"powerDownstream": -17.25, "oltId": [1, 23, 170, 2]}}},
            "testLocations": [{"label": "RMN2.DHA.12.26A"}]}]}"#;
        let (base, seen) = serve(vec![
            (200, TOKEN),
            (200, search),
            (200, FILE), // preview
            (200, TOKEN),
            (200, search),
            (200, FILE), // apply
            (200, TOKEN),
            (200, search), // re-run: nothing new
        ]);
        let ss = StrataSyncSettings {
            base_url: base,
            access_token: None,
            client_id: Some("id".into()),
            client_secret: Some("secret".into()),
        };
        let settings = crate::config::make_settings(
            "fake",
            r#"{"type": "service_account", "client_email": "bot@x", "private_key": "k"}"#,
        )
        .unwrap();
        let log = |_: &str| {};

        let preview = run_sync(&ss, &settings, false, None, &log, history::MANUAL).unwrap();
        assert_eq!((preview.found, preview.truepon, preview.new), (4, 3, 3));
        assert_eq!(preview.sheet_title, "Fake PON Audit");
        assert_eq!(preview.plan.written.len(), 2);
        assert_eq!(preview.plan.flags.len(), 1);
        assert_eq!(preview.plan.flags[0].kind, "olt_mismatch");
        assert!(preview.position_saved.is_none());
        assert!(!fs::read_to_string(&sheet).unwrap().contains("-18.5"), "preview must not write");

        let applied = run_sync(&ss, &settings, true, None, &log, history::MANUAL).unwrap();
        assert_eq!(applied.cells_written, 4);
        assert_eq!(applied.position_saved, timeutil::parse_aware(&t2));
        let written: serde_json::Value = serde_json::from_str(&fs::read_to_string(&sheet).unwrap()).unwrap();
        let rows = &written["RMN2.DHA PON Audit"];
        assert_eq!(rows[1][4], "-18.5");
        assert_eq!(rows[1][5], "2026-10-29T08:00:00-04:00");
        assert_eq!(rows[2][4], "-17.25");
        assert_eq!(rows[1][7], "", "the flagged result is not written");

        let again = run_sync(&ss, &settings, true, None, &log, history::MANUAL).unwrap();
        assert_eq!((again.found, again.new, again.plan.written.len()), (4, 0, 0));
        assert_eq!(seen.lock().unwrap().len(), 8);

        let runs = history::recent_runs(10);
        assert_eq!(runs.len(), 2, "previews aren't recorded");
        assert_eq!(runs[1]["written"], "2");
        assert_eq!(runs[1]["flagged"], "1");
        assert_eq!(runs[1]["status"], "ok");
        let written_csv = fs::read_to_string(history::history_dir().join("written.csv")).unwrap();
        assert!(written_csv.contains("RMN2.DHA.12.26A,RMN2.DHA PON Audit,\"E3, F3\",-17.25"), "{written_csv}");
        let flagged_csv = fs::read_to_string(history::history_dir().join("flagged.csv")).unwrap();
        assert!(flagged_csv.contains("olt_mismatch,RMN2.DHA.12.25B"), "{flagged_csv}");

        // The lock is held during a run: a second one is refused.
        let lock = run_lock().unwrap();
        assert!(matches!(
            run_sync(&ss, &settings, true, None, &log, history::AUTOMATIC),
            Err(AppError::RunInProgress(_))
        ));
        drop(lock);

        fs::remove_dir_all(&dir).unwrap();
    }
}
