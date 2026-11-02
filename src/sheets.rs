//! Read and write the Google Sheet through the Python sheets bridge.
//!
//! The Google Sheets code is the Python app's own (sheets_bridge/sheets.py,
//! using gspread). This module starts it as a hidden child process and talks to
//! it over stdin/stdout, one JSON object per line; the protocol is described in
//! sheets_bridge/__main__.py. The service account key goes over stdin, never on
//! the command line.
//!
//! Where the bridge is looked for, in order:
//!   1. the PON_AUDIT_SHEETS_BRIDGE environment variable (a path to it)
//!   2. `sheets-bridge` (`.exe`) next to the app
//!   3. macOS: the app bundle's Resources folder
//!   4. Windows release build: a copy embedded in the app, unpacked into the
//!      app's data folder the first time it's needed
//!   5. from source: `python -m sheets_bridge` in this repository (development)

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};

use serde_json::{Value, json};

use crate::config::Settings;
use crate::error::{AppError, Result};
use crate::pon_audit::CellUpdate;

const EXE_NAME: &str = if cfg!(windows) { "sheets-bridge.exe" } else { "sheets-bridge" };
const STDERR_TAIL: usize = 4000;

/// An open spreadsheet, via a running bridge process.
pub struct SheetWriter {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: BufReader<ChildStdout>,
    stderr: Arc<Mutex<String>>,
    title: String,
}

impl SheetWriter {
    /// Start the bridge and open the spreadsheet.
    pub fn open(settings: &Settings) -> Result<SheetWriter> {
        let command = bridge_command()?;
        Self::open_with(command, settings)
    }

    pub fn open_with(mut command: Command, settings: &Settings) -> Result<SheetWriter> {
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        hide_window(&mut command);
        let mut child = command.spawn().map_err(|e| {
            AppError::other(format!("Could not start the Google Sheets helper ({:?}): {e}", command.get_program()))
        })?;
        let stdin = child.stdin.take();
        let stdout = BufReader::new(child.stdout.take().expect("piped"));
        let stderr = Arc::new(Mutex::new(String::new()));
        if let Some(mut err) = child.stderr.take() {
            let tail = Arc::clone(&stderr);
            std::thread::spawn(move || {
                let mut buf = [0u8; 4096];
                while let Ok(n) = err.read(&mut buf) {
                    if n == 0 {
                        break;
                    }
                    let mut t = tail.lock().unwrap_or_else(|p| p.into_inner());
                    t.push_str(&String::from_utf8_lossy(&buf[..n]));
                    if t.len() > STDERR_TAIL * 2 {
                        let cut = t.len() - STDERR_TAIL;
                        let cut = (cut..t.len()).find(|i| t.is_char_boundary(*i)).unwrap_or(t.len());
                        t.drain(..cut);
                    }
                }
            });
        }
        let mut writer = SheetWriter { child, stdin, stdout, stderr, title: String::new() };
        let response = writer.request(json!({
            "op": "open",
            "service_account": Value::Object(settings.service_account_info.clone()),
            "sheet_id": settings.sheet_id,
        }))?;
        writer.title = response.get("title").and_then(Value::as_str).unwrap_or_default().to_string();
        Ok(writer)
    }

    pub fn title(&self) -> &str {
        &self.title
    }

    /// Every cell of the given tabs, as displayed. Tabs that don't exist are left out.
    pub fn read_tabs(&mut self, titles: &[String]) -> Result<HashMap<String, Vec<Vec<String>>>> {
        let response = self.request(json!({"op": "read_tabs", "titles": titles}))?;
        let mut out = HashMap::new();
        if let Some(Value::Object(tabs)) = response.get("tabs") {
            for (title, rows) in tabs {
                let rows = rows
                    .as_array()
                    .map(|rows| {
                        rows.iter()
                            .map(|row| {
                                row.as_array()
                                    .map(|cells| {
                                        cells
                                            .iter()
                                            .map(|c| match c {
                                                Value::String(s) => s.clone(),
                                                Value::Null => String::new(),
                                                other => other.to_string(),
                                            })
                                            .collect()
                                    })
                                    .unwrap_or_default()
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                out.insert(title.clone(), rows);
            }
        }
        Ok(out)
    }

    /// Write single-cell updates. Returns the number of cells written.
    ///
    /// Values are stored as given (RAW): numbers stay numbers and ISO
    /// timestamps stay text, matching what the sheet already holds.
    pub fn write_cells(&mut self, updates: &[CellUpdate]) -> Result<usize> {
        if updates.is_empty() {
            return Ok(0);
        }
        let cells: Vec<Value> = updates.iter().map(|u| json!({"tab": u.tab, "a1": u.a1(), "value": u.value})).collect();
        let response = self.request(json!({"op": "write_cells", "updates": cells, "value_input_option": "RAW"}))?;
        Ok(response.get("written").and_then(Value::as_u64).unwrap_or(0) as usize)
    }

    fn request(&mut self, request: Value) -> Result<Value> {
        let line = request.to_string() + "\n";
        let sent = self.stdin.as_mut().map(|s| s.write_all(line.as_bytes()).and_then(|_| s.flush()));
        if !matches!(sent, Some(Ok(()))) {
            return Err(self.died());
        }
        let mut reply = String::new();
        match self.stdout.read_line(&mut reply) {
            Ok(n) if n > 0 => {}
            _ => return Err(self.died()),
        }
        let response: Value = serde_json::from_str(&reply).map_err(|e| {
            AppError::other(format!("The Google Sheets helper sent an unreadable reply ({e}): {reply}"))
        })?;
        if response.get("ok").and_then(Value::as_bool) == Some(true) {
            return Ok(response);
        }
        let message = response.get("error").and_then(Value::as_str).unwrap_or("unknown error").to_string();
        Err(match response.get("kind").and_then(Value::as_str) {
            Some("access") => AppError::SheetAccess(message),
            _ => AppError::other(message),
        })
    }

    fn died(&mut self) -> AppError {
        let _ = self.child.wait();
        // Give the stderr reader a moment to collect the last lines.
        std::thread::sleep(std::time::Duration::from_millis(100));
        let tail = self.stderr.lock().map(|t| t.trim().to_string()).unwrap_or_default();
        let last: Vec<&str> = tail.lines().rev().take(12).collect::<Vec<_>>().into_iter().rev().collect();
        AppError::other(format!("The Google Sheets helper stopped unexpectedly.\n\n{}", last.join("\n")))
    }
}

impl Drop for SheetWriter {
    fn drop(&mut self) {
        if let Some(mut stdin) = self.stdin.take() {
            let _ = stdin.write_all(b"{\"op\": \"quit\"}\n");
        } // closing stdin also ends it
        for _ in 0..50 {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(windows)]
fn hide_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_window(_command: &mut Command) {}

/// How to start the bridge (see the module docs for where it's looked for).
pub fn bridge_command() -> Result<Command> {
    if let Some(path) = std::env::var_os("PON_AUDIT_SHEETS_BRIDGE").filter(|p| !p.is_empty()) {
        return Ok(Command::new(path));
    }
    if let Some(dir) = std::env::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf)) {
        for candidate in [dir.join(EXE_NAME), dir.join("..").join("Resources").join(EXE_NAME)] {
            if candidate.is_file() {
                return Ok(Command::new(candidate));
            }
        }
    }
    if let Some(path) = embedded::unpack()? {
        return Ok(Command::new(path));
    }
    let source = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    if source.join("sheets_bridge").join("__main__.py").is_file() {
        let python =
            std::env::var_os("PYTHON").unwrap_or_else(|| (if cfg!(windows) { "python" } else { "python3" }).into());
        let mut command = Command::new(python);
        command.arg("-m").arg("sheets_bridge").current_dir(source);
        return Ok(command);
    }
    Err(AppError::other(format!(
        "The Google Sheets helper ({EXE_NAME}) is missing. It should be next to the app; download the app again."
    )))
}

#[cfg(embedded_bridge)]
mod embedded {
    use super::*;
    use sha2::{Digest, Sha256};

    static BRIDGE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/sheets-bridge.bin"));

    /// Unpack the embedded bridge into the app's data folder (once per version).
    pub fn unpack() -> Result<Option<PathBuf>> {
        let digest = Sha256::digest(BRIDGE);
        let hash: String = digest.iter().take(6).map(|b| format!("{b:02x}")).collect();
        let dir = crate::config::app_data_dir().join("bin");
        let path = dir.join(format!("sheets-bridge-{hash}{}", if cfg!(windows) { ".exe" } else { "" }));
        if std::fs::metadata(&path).map(|m| m.len() == BRIDGE.len() as u64).unwrap_or(false) {
            return Ok(Some(path));
        }
        std::fs::create_dir_all(&dir)?;
        let tmp = dir.join(format!("sheets-bridge-{hash}.{}.tmp", std::process::id()));
        std::fs::write(&tmp, BRIDGE)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o755))?;
        }
        if std::fs::rename(&tmp, &path).is_err() {
            let _ = std::fs::remove_file(&tmp); // another copy of the app got there first
        }
        // Remove copies left by older versions.
        if let Ok(entries) = std::fs::read_dir(&dir) {
            for e in entries.flatten() {
                let name = e.file_name().to_string_lossy().to_string();
                if name.starts_with("sheets-bridge-") && e.path() != path && !name.ends_with(".tmp") {
                    let _ = std::fs::remove_file(e.path());
                }
            }
        }
        Ok(Some(path))
    }
}

#[cfg(not(embedded_bridge))]
mod embedded {
    use super::*;

    pub fn unpack() -> Result<Option<PathBuf>> {
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::make_settings;
    use crate::pon_audit::CellValue;

    const KEY: &str =
        r#"{"type": "service_account", "client_email": "bot@x.iam.gserviceaccount.com", "private_key": "k"}"#;

    /// The real bridge protocol code, with gspread replaced by a fake (tests/fake_bridge.py).
    fn fake_bridge() -> Option<Command> {
        let python = if cfg!(windows) { "python" } else { "python3" };
        if Command::new(python).arg("--version").output().is_err() {
            eprintln!("python not found; skipping bridge test");
            return None;
        }
        let mut c = Command::new(python);
        c.arg("tests/fake_bridge.py").current_dir(env!("CARGO_MANIFEST_DIR"));
        Some(c)
    }

    #[test]
    fn talks_to_the_bridge() {
        let Some(cmd) = fake_bridge() else { return };
        let settings = make_settings("abc", KEY).unwrap();
        let mut w = SheetWriter::open_with(cmd, &settings).unwrap();
        assert_eq!(w.title(), "Fake PON Audit");
        let tabs = w.read_tabs(&["RMN2.DHA PON Audit".into(), "Nope".into()]).unwrap();
        assert_eq!(tabs.len(), 1);
        assert_eq!(tabs["RMN2.DHA PON Audit"][1], vec!["A", "12", "25"]);
        let n = w
            .write_cells(&[
                CellUpdate { tab: "RMN2.DHA PON Audit".into(), row: 2, col: 5, value: CellValue::Number(-18.5) },
                CellUpdate { tab: "RMN2.DHA PON Audit".into(), row: 2, col: 6, value: CellValue::Text("t".into()) },
            ])
            .unwrap();
        assert_eq!(n, 2);
        // The fake echoes what it was sent as the next read.
        let echoed = w.read_tabs(&["written".into()]).unwrap();
        assert_eq!(
            echoed["written"],
            vec![vec!["'RMN2.DHA PON Audit'!E2", "-18.5"], vec!["'RMN2.DHA PON Audit'!F2", "t"]]
        );
    }

    /// With SHEETS_BRIDGE_EMBED set at build time, the embedded bridge unpacks and answers.
    #[cfg(embedded_bridge)]
    #[test]
    fn embedded_bridge_unpacks_and_runs() {
        let path = embedded::unpack().unwrap().unwrap();
        assert_eq!(embedded::unpack().unwrap().unwrap(), path, "unpacked once");
        let mut child = Command::new(&path).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
        child.stdin.take().unwrap().write_all(b"{\"op\": \"hello\"}\n").unwrap();
        let out = child.wait_with_output().unwrap();
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), r#"{"ok": true, "protocol": 1}"#);
    }

    #[test]
    fn access_errors_and_crashes() {
        let Some(cmd) = fake_bridge() else { return };
        let settings = make_settings("unshared", KEY).unwrap();
        match SheetWriter::open_with(cmd, &settings) {
            Err(AppError::SheetAccess(m)) => assert!(m.contains("not shared with bot@x"), "{m}"),
            other => panic!("expected an access error, got {:?}", other.err()),
        }
        let Some(cmd) = fake_bridge() else { return };
        let settings = make_settings("crash", KEY).unwrap();
        match SheetWriter::open_with(cmd, &settings) {
            Err(AppError::Other(m)) => {
                assert!(m.contains("stopped unexpectedly") && m.contains("simulated crash"), "{m}")
            }
            other => panic!("expected a crash, got {:?}", other.err()),
        }
    }
}
