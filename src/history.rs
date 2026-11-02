//! A permanent record of every update that writes to the sheet.
//!
//! Each update (from the window or an automatic one) appends to CSV files in
//! `history_dir()`:
//!
//!   runs.csv      one row per update: when, how it was started, the outcome and counts
//!   flagged.csv   every flagged result, which was *not* written to the sheet
//!   written.csv   every result written: location, tab, cells, dBm, test time
//!
//! Automatic updates also append their progress messages to automatic.log.
//! Nothing here is ever rewritten or pruned, so the files are an audit trail.
//! The columns are the same as the Python version's, so old and new rows mix.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use chrono::Local;

use crate::config::app_data_dir;
use crate::pon_audit::{Flag, py_float};
use crate::sync::SyncResult;
use crate::timeutil::{self, Time};

pub type Log<'a> = &'a dyn Fn(&str);

pub const MANUAL: &str = "manual";
pub const AUTOMATIC: &str = "automatic";

pub const RUNS_HEADER: [&str; 16] = [
    "run_at", "trigger", "status", "sheet", "searched_from", "searched_to", "found", "truepon", "new", "written",
    "flagged", "skipped", "cells_written", "download_failures", "position_saved", "message",
];
pub const FLAG_FIELDS: [&str; 7] = ["kind", "label", "message", "test_time", "tech", "serial_no", "source"];
const WRITTEN_HEADER: [&str; 12] = [
    "run_at", "trigger", "sheet", "label", "tab", "cells", "dbm", "test_time", "sheet_time", "tech", "serial_no",
    "source",
];

const LOG_MAX_BYTES: u64 = 5_000_000;
const BOM: &[u8] = b"\xEF\xBB\xBF";

pub fn history_dir() -> PathBuf {
    app_data_dir().join("history")
}

fn now() -> String {
    timeutil::iso(&timeutil::now_local())
}

fn iso_local(t: Option<&Time>) -> String {
    t.map(|t| timeutil::iso(&timeutil::to_local(t))).unwrap_or_default()
}

pub fn flag_row(f: &Flag) -> Vec<String> {
    let r = f.reading.as_ref();
    vec![
        f.kind.clone(),
        r.map(|r| r.label.clone()).unwrap_or_default(),
        f.message.clone(),
        r.map(|r| r.test_time.clone()).unwrap_or_default(),
        r.and_then(|r| r.tech_name.clone()).unwrap_or_default(),
        r.and_then(|r| r.serial_no.clone()).unwrap_or_default(),
        r.and_then(|r| r.source.clone()).unwrap_or_default(),
    ]
}

/// Write rows as CSV (with the UTF-8 BOM Excel wants at the start of a new file).
pub fn write_csv(path: &Path, header: &[&str], rows: &[Vec<String>], append: bool) -> io::Result<()> {
    let new = !append || fs::metadata(path).map(|m| m.len() == 0).unwrap_or(true);
    let mut fh = if append {
        OpenOptions::new().create(true).append(true).open(path)?
    } else {
        File::create(path)?
    };
    let mut buf = Vec::new();
    {
        let mut w = csv::WriterBuilder::new().terminator(csv::Terminator::CRLF).from_writer(&mut buf);
        if new {
            w.write_record(header).map_err(io::Error::other)?;
        }
        for row in rows {
            w.write_record(row).map_err(io::Error::other)?;
        }
        w.flush()?;
    }
    if new {
        fh.write_all(BOM)?;
    }
    fh.write_all(&buf)
}

/// Windows refuses to open a file another program (Excel) has open.
fn is_locked(e: &io::Error) -> bool {
    e.kind() == io::ErrorKind::PermissionDenied || matches!(e.raw_os_error(), Some(32 | 33) if cfg!(windows))
}

fn append(name: &str, header: &[&str], rows: &[Vec<String>], log: Log) -> io::Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let dir = history_dir();
    fs::create_dir_all(&dir)?;
    let path = dir.join(name);
    match write_csv(&path, header, rows, true) {
        Err(e) if is_locked(&e) => {
            // Keep the rows in a file beside it.
            let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("history");
            let alt = dir.join(format!("{stem}-{}.csv", Local::now().format("%Y%m%d-%H%M%S")));
            write_csv(&alt, header, rows, true)?;
            log(&format!(
                "History: {name} is open in another program, so this update's rows went to {}",
                alt.file_name().unwrap_or_default().to_string_lossy()
            ));
            Ok(())
        }
        other => other,
    }
}

/// Append an applied update, its flagged results and its written results to the history.
pub fn record_run(result: &SyncResult, trigger: &str, log: Log) {
    let (plan, run_at, sheet) = (&result.plan, now(), result.sheet_title.clone());
    let skipped = plan.skipped_not_flagged().len();
    let mut message = result.download_failures.join("; ");
    if !result.missing_tabs.is_empty() {
        let tabs = format!("tabs not found: {}", result.missing_tabs.join(", "));
        message = if message.is_empty() { tabs } else { format!("{tabs}; {message}") };
    }
    let status = if result.download_failures.is_empty() { "ok" } else { "partial" };
    let run_row = vec![
        run_at.clone(),
        trigger.into(),
        status.into(),
        sheet.clone(),
        iso_local(Some(&result.start)),
        iso_local(Some(&result.end)),
        result.found.to_string(),
        result.truepon.to_string(),
        result.new.to_string(),
        plan.written.len().to_string(),
        plan.flags.len().to_string(),
        skipped.to_string(),
        result.cells_written.to_string(),
        result.download_failures.len().to_string(),
        iso_local(result.position_saved.as_ref()),
        message,
    ];
    let flagged: Vec<Vec<String>> = plan
        .flags
        .iter()
        .map(|f| [vec![run_at.clone(), trigger.into(), sheet.clone()], flag_row(f)].concat())
        .collect();
    let written: Vec<Vec<String>> = plan
        .written_cells()
        .into_iter()
        .map(|(rd, power, stamp)| {
            vec![
                run_at.clone(),
                trigger.into(),
                sheet.clone(),
                rd.label.clone(),
                power.tab.clone(),
                format!("{}, {}", power.a1(), stamp.a1()),
                rd.power_dbm.map(py_float).unwrap_or_default(),
                rd.test_time.clone(),
                rd.sheet_time(),
                rd.tech_name.clone().unwrap_or_default(),
                rd.serial_no.clone().unwrap_or_default(),
                rd.source.clone().unwrap_or_default(),
            ]
        })
        .collect();
    let mut flagged_header = vec!["run_at", "trigger", "sheet"];
    flagged_header.extend(FLAG_FIELDS);
    let outcome = append("runs.csv", &RUNS_HEADER, &[run_row], log)
        .and_then(|_| append("flagged.csv", &flagged_header, &flagged, log))
        .and_then(|_| append("written.csv", &WRITTEN_HEADER, &written, log));
    if let Err(e) = outcome {
        // The sheet is already written; don't turn that into a failure.
        log(&format!("Could not save the update history: {e}"));
    }
}

/// Append an update that didn't complete ("failed" or "skipped") to runs.csv.
pub fn record_status(trigger: &str, status: &str, sheet: &str, message: &str, log: Log) {
    let mut row = vec![String::new(); RUNS_HEADER.len()];
    row[0] = now();
    row[1] = trigger.into();
    row[2] = status.into();
    row[3] = sheet.into();
    row[15] = message.split_whitespace().collect::<Vec<_>>().join(" ");
    if let Err(e) = append("runs.csv", &RUNS_HEADER, &[row], log) {
        log(&format!("Could not save the update history: {e}"));
    }
}

/// The latest rows of runs.csv, newest first.
pub fn recent_runs(limit: usize) -> Vec<HashMap<String, String>> {
    let Ok(bytes) = fs::read(history_dir().join("runs.csv")) else { return Vec::new() };
    let bytes = bytes.strip_prefix(BOM).unwrap_or(&bytes);
    let mut reader = csv::ReaderBuilder::new().flexible(true).from_reader(bytes);
    let Ok(headers) = reader.headers().cloned() else { return Vec::new() };
    let mut rows: Vec<HashMap<String, String>> = reader
        .records()
        .filter_map(|r| r.ok())
        .map(|r| headers.iter().zip(r.iter()).map(|(h, v)| (h.to_string(), v.to_string())).collect())
        .collect();
    rows.reverse();
    rows.truncate(limit);
    rows
}

pub fn runs_mtime() -> Option<SystemTime> {
    fs::metadata(history_dir().join("runs.csv")).and_then(|m| m.modified()).ok()
}

/// A log function that appends timestamped lines to automatic.log.
pub fn automatic_log() -> impl Fn(&str) {
    let dir = history_dir();
    let _ = fs::create_dir_all(&dir);
    let path = dir.join("automatic.log");
    if fs::metadata(&path).map(|m| m.len() > LOG_MAX_BYTES).unwrap_or(false) {
        let _ = fs::rename(&path, dir.join("automatic.log.1"));
    }
    move |message: &str| {
        let line = format!("{}  {message}\n", now());
        if let Ok(mut fh) = OpenOptions::new().create(true).append(true).open(&path) {
            let _ = fh.write_all(line.as_bytes());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn csv_has_bom_once_and_crlf() {
        let dir = std::env::temp_dir().join(format!("pon-audit-hist-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("t.csv");
        let _ = fs::remove_file(&path);
        write_csv(&path, &["a", "b"], &[vec!["1".into(), "x, y".into()]], true).unwrap();
        write_csv(&path, &["a", "b"], &[vec!["2".into(), "".into()]], true).unwrap();
        let bytes = fs::read(&path).unwrap();
        assert_eq!(bytes, b"\xEF\xBB\xBFa,b\r\n1,\"x, y\"\r\n2,\r\n");
        fs::remove_dir_all(&dir).unwrap();
    }
}
