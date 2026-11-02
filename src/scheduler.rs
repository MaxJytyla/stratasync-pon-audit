//! Automatic updates: run "Run update" on a timer, with the settings saved in the window.
//!
//! The OS scheduler starts the app as `<app> --auto-update` every N minutes:
//!
//!   macOS    a LaunchAgent, ~/Library/LaunchAgents/com.stratasync.ponaudit.update.plist
//!   Windows  a Task Scheduler task, "StrataSync PON Audit update"
//!
//! Both run as the signed-in user, only while they're signed in, and catch up on a
//! missed run after the computer wakes. `run_automatic` does one update with no
//! window; its outcome goes to the update history (history.rs).

#![cfg_attr(not(any(windows, target_os = "macos")), allow(dead_code))]

use std::process::{Command, Output};

use crate::config::{APP_NAME, SavedForm, make_settings, make_stratasync_settings};
use crate::error::AppError;
use crate::history;
use crate::sync::run_sync;

pub const AUTO_UPDATE_ARG: &str = "--auto-update";
#[cfg(target_os = "macos")]
pub const LAUNCHD_LABEL: &str = "com.stratasync.ponaudit.update";
pub const MIN_MINUTES: u32 = 5;
pub const MAX_MINUTES: u32 = 7 * 24 * 60;

pub fn windows_task() -> String {
    format!("{APP_NAME} update")
}

#[derive(Debug, Clone, PartialEq)]
pub struct Schedule {
    pub minutes: u32,
    pub command: Vec<String>,
}

impl Schedule {
    /// False when the schedule starts a different copy of the app (e.g. an older download).
    pub fn is_current_app(&self) -> bool {
        self.command == command()
    }
}

pub fn supported() -> bool {
    cfg!(any(windows, target_os = "macos"))
}

/// How the OS scheduler should start an automatic update of this copy of the app.
pub fn command() -> Vec<String> {
    let exe = std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default();
    vec![exe, AUTO_UPDATE_ARG.to_string()]
}

pub fn describe(minutes: u32) -> String {
    if !minutes.is_multiple_of(60) {
        return format!("{minutes} minutes");
    }
    match minutes / 60 {
        1 => "1 hour".into(),
        h => format!("{h} hours"),
    }
}

/// The installed schedule, or None if automatic updates are off.
pub fn current() -> Option<Schedule> {
    #[cfg(target_os = "macos")]
    return mac::current();
    #[cfg(windows)]
    return win::current();
    #[cfg(not(any(windows, target_os = "macos")))]
    None
}

/// Turn on automatic updates every `minutes`, replacing any earlier schedule.
pub fn install(minutes: u32) -> Result<(), AppError> {
    if !supported() {
        return Err(AppError::config("Automatic updates are only available on Windows and macOS."));
    }
    if !(MIN_MINUTES..=MAX_MINUTES).contains(&minutes) {
        return Err(AppError::config(format!(
            "Choose an interval between {MIN_MINUTES} minutes and {} days.",
            MAX_MINUTES / 1440
        )));
    }
    if command()[0].contains("/AppTranslocation/") {
        return Err(AppError::config(format!(
            "macOS is running the app from a temporary location. Move {APP_NAME}.app to Applications, open it from there, and try again."
        )));
    }
    #[cfg(target_os = "macos")]
    return mac::install(minutes);
    #[cfg(windows)]
    return win::install(minutes);
    #[cfg(not(any(windows, target_os = "macos")))]
    Ok(())
}

/// Turn off automatic updates (does nothing if they're already off).
pub fn remove() -> Result<(), AppError> {
    #[cfg(target_os = "macos")]
    return mac::remove();
    #[cfg(windows)]
    return win::remove();
    #[cfg(not(any(windows, target_os = "macos")))]
    Ok(())
}

fn run(program: &str, args: &[&str]) -> std::io::Result<Output> {
    let mut cmd = Command::new(program);
    cmd.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    cmd.output()
}

fn output_text(out: &Output) -> String {
    let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
    if err.is_empty() { String::from_utf8_lossy(&out.stdout).trim().to_string() } else { err }
}

// ---- macOS: launchd ------------------------------------------------------------------

#[cfg(target_os = "macos")]
mod mac {
    use super::*;
    use std::path::PathBuf;

    fn plist_path() -> PathBuf {
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
        home.join("Library").join("LaunchAgents").join(format!("{LAUNCHD_LABEL}.plist"))
    }

    fn domain() -> String {
        let uid = run("id", &["-u"]).map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
        format!("gui/{uid}")
    }

    pub fn current() -> Option<Schedule> {
        let value = plist::Value::from_file(plist_path()).ok()?;
        let dict = value.as_dictionary()?;
        let interval = dict.get("StartInterval")?.as_signed_integer()?;
        let args = dict
            .get("ProgramArguments")?
            .as_array()?
            .iter()
            .map(|a| a.as_string().map(str::to_string))
            .collect::<Option<Vec<_>>>()?;
        Some(Schedule { minutes: (interval / 60) as u32, command: args })
    }

    pub fn install(minutes: u32) -> Result<(), AppError> {
        let domain = domain();
        // Anything the app prints outside its own log.
        let out = history::history_dir().join("launchd.log");
        std::fs::create_dir_all(history::history_dir())?;
        let _ = run("launchctl", &["bootout", &format!("{domain}/{LAUNCHD_LABEL}")]);
        let path = plist_path();
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut dict = plist::Dictionary::new();
        dict.insert("Label".into(), LAUNCHD_LABEL.into());
        dict.insert(
            "ProgramArguments".into(),
            plist::Value::Array(command().into_iter().map(plist::Value::String).collect()),
        );
        dict.insert("StartInterval".into(), plist::Value::Integer((minutes as i64 * 60).into()));
        dict.insert("RunAtLoad".into(), false.into());
        dict.insert("ProcessType".into(), "Background".into());
        dict.insert("StandardOutPath".into(), out.to_string_lossy().into_owned().into());
        dict.insert("StandardErrorPath".into(), out.to_string_lossy().into_owned().into());
        plist::Value::Dictionary(dict)
            .to_file_xml(&path)
            .map_err(|e| AppError::other(format!("Could not write {}: {e}", path.display())))?;
        let path_text = path.to_string_lossy().into_owned();
        let mut last = String::new();
        for attempt in 0..5 {
            // bootstrap can fail briefly while the old job is still being removed
            match run("launchctl", &["bootstrap", &domain, &path_text]) {
                Ok(o) if o.status.success() => return Ok(()),
                Ok(o) => last = output_text(&o),
                Err(e) => last = e.to_string(),
            }
            std::thread::sleep(std::time::Duration::from_millis(500 * (attempt + 1)));
        }
        let _ = std::fs::remove_file(&path);
        Err(AppError::config(format!("launchctl could not schedule the update: {last}")))
    }

    pub fn remove() -> Result<(), AppError> {
        let _ = run("launchctl", &["bootout", &format!("{}/{LAUNCHD_LABEL}", domain())]);
        match std::fs::remove_file(plist_path()) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.into()),
            _ => Ok(()),
        }
    }
}

// ---- Windows: Task Scheduler -----------------------------------------------------------

/// XML special characters escaped.
pub fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

pub fn xml_unescape(s: &str) -> String {
    s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'").replace("&amp;", "&")
}

/// The Task Scheduler definition for an update every `minutes`, starting one interval from now.
pub fn win_task_xml(minutes: u32, cmd: &[String], user: &str, start: &str) -> String {
    let args: Vec<String> =
        cmd[1..].iter().map(|a| if a.contains(' ') { format!("\"{a}\"") } else { a.clone() }).collect();
    format!(
        r#"<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Writes new StrataSync TruePON results to the PON Audit sheet. Set up from the {app} app.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
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
      <Command>{command}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>
"#,
        app = xml_escape(APP_NAME),
        user = xml_escape(user),
        command = xml_escape(&cmd[0]),
        args = xml_escape(&args.join(" ")),
    )
}

/// Minutes in a Task Scheduler duration such as PT15M, PT2H or P1D.
pub fn parse_duration(value: &str) -> Option<u32> {
    let re = regex::Regex::new(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?)?$").unwrap();
    let m = re.captures(value.trim())?;
    let n = |i: usize| m.get(i).map(|g| g.as_str().parse::<u32>().unwrap_or(0)).unwrap_or(0);
    Some(n(1) * 1440 + n(2) * 60 + n(3))
}

/// Read the schedule back from `schtasks /Query /XML` output.
pub fn parse_task_xml(xml: &str) -> Schedule {
    let tag = |name: &str| {
        let re = regex::Regex::new(&format!(r"(?s)<{name}>(.*?)</{name}>")).unwrap();
        re.captures(xml).map(|c| xml_unescape(c[1].trim())).unwrap_or_default()
    };
    let interval = parse_duration(&tag("Interval"));
    let exe = tag("Command").trim_matches('"').to_string();
    let args_re = regex::Regex::new(r#""[^"]*"|\S+"#).unwrap();
    let args = tag("Arguments");
    let mut command = vec![exe];
    command.extend(args_re.find_iter(&args).map(|m| m.as_str().trim_matches('"').to_string()));
    match interval {
        Some(minutes) => Schedule { minutes, command },
        None => Schedule { minutes: 0, command: Vec::new() },
    }
}

/// schtasks writes its XML as UTF-16 or in the console code page.
pub fn decode_console(bytes: &[u8]) -> String {
    if bytes.len() >= 2
        && (bytes.starts_with(&[0xFF, 0xFE]) || bytes.iter().skip(1).step_by(2).take(16).all(|b| *b == 0))
    {
        let start = if bytes.starts_with(&[0xFF, 0xFE]) { 2 } else { 0 };
        let units: Vec<u16> = bytes[start..].chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        return String::from_utf16_lossy(&units);
    }
    String::from_utf8_lossy(bytes).into_owned()
}

#[cfg(windows)]
mod win {
    use super::*;

    pub fn current() -> Option<Schedule> {
        let out = run("schtasks", &["/Query", "/TN", &windows_task(), "/XML"]).ok()?;
        if !out.status.success() {
            return None;
        }
        Some(parse_task_xml(&decode_console(&out.stdout)))
    }

    pub fn install(minutes: u32) -> Result<(), AppError> {
        let start =
            (chrono::Local::now() + chrono::Duration::minutes(minutes as i64)).format("%Y-%m-%dT%H:%M:00").to_string();
        let user = format!(
            "{}\\{}",
            std::env::var("USERDOMAIN").unwrap_or_default(),
            std::env::var("USERNAME").unwrap_or_default()
        );
        let user = user.trim_start_matches('\\');
        let xml = win_task_xml(minutes, &command(), user, &start);
        // schtasks wants UTF-16 with a BOM.
        let mut bytes = vec![0xFF, 0xFE];
        for unit in xml.encode_utf16() {
            bytes.extend_from_slice(&unit.to_le_bytes());
        }
        let path = std::env::temp_dir().join(format!("pon-audit-task-{}.xml", std::process::id()));
        std::fs::write(&path, bytes)?;
        let result = run("schtasks", &["/Create", "/TN", &windows_task(), "/XML", &path.to_string_lossy(), "/F"]);
        let _ = std::fs::remove_file(&path);
        let out = result?;
        if !out.status.success() {
            return Err(AppError::config(format!(
                "Task Scheduler could not schedule the update: {}",
                output_text(&out)
            )));
        }
        Ok(())
    }

    pub fn remove() -> Result<(), AppError> {
        if current().is_none() {
            return Ok(());
        }
        let out = run("schtasks", &["/Delete", "/TN", &windows_task(), "/F"])?;
        if !out.status.success() {
            return Err(AppError::config(format!("Task Scheduler could not remove the update: {}", output_text(&out))));
        }
        Ok(())
    }
}

// ---- the automatic update itself ----------------------------------------------------------

/// One update with the saved settings and no window. Returns the process exit code.
pub fn run_automatic() -> i32 {
    let log = history::automatic_log();
    log("Automatic update started");
    let form = SavedForm::load();
    let settings = make_stratasync_settings(&form.region, &form.client_id, &form.client_secret)
        .and_then(|ss| make_settings(&form.sheet_id, &form.service_account_json).map(|s| (ss, s)));
    let (ss, settings) = match settings {
        Ok(pair) => pair,
        Err(e) => {
            let mut message = format!("The saved settings are incomplete: {}", e.text());
            if !form.remember_secrets {
                message.push_str(
                    " Automatic updates need 'Remember client secret and Google key on this computer' ticked in the app.",
                );
            }
            log(&message);
            history::record_status(history::AUTOMATIC, "failed", &form.sheet_id, &message, &log);
            return 1;
        }
    };
    match run_sync(&ss, &settings, true, None, &log, history::AUTOMATIC) {
        Err(AppError::RunInProgress(msg)) => {
            log(&format!("Skipped: {msg}"));
            history::record_status(history::AUTOMATIC, "skipped", &settings.sheet_id, &msg, &log);
            0
        }
        Err(e) => {
            // already recorded in runs.csv by run_sync
            log(&format!("Error: {}", e.text()));
            1
        }
        Ok(result) => {
            log(&format!(
                "Automatic update finished: wrote {} result(s), flagged {}",
                result.plan.written.len(),
                result.plan.flags.len()
            ));
            if result.download_failures.is_empty() { 0 } else { 1 }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn durations_and_descriptions() {
        assert_eq!(parse_duration("PT15M"), Some(15));
        assert_eq!(parse_duration("PT2H"), Some(120));
        assert_eq!(parse_duration("P1DT1H30M"), Some(1530));
        assert_eq!(parse_duration("junk"), None);
        assert_eq!(describe(60), "1 hour");
        assert_eq!(describe(180), "3 hours");
        assert_eq!(describe(90), "90 minutes");
    }

    #[test]
    fn task_xml_roundtrip() {
        let cmd = vec![r"C:\Apps\StrataSync PON Audit & Co.exe".to_string(), AUTO_UPDATE_ARG.to_string()];
        let xml = win_task_xml(30, &cmd, "CORP\\pat", "2026-11-02T14:05:00");
        assert!(xml.contains("<Interval>PT30M</Interval>"));
        assert!(xml.contains("&amp; Co.exe"));
        let sched = parse_task_xml(&xml);
        assert_eq!(sched, Schedule { minutes: 30, command: cmd });
        // As schtasks prints it (UTF-16 with a BOM).
        let mut bytes = vec![0xFF, 0xFE];
        for u in xml.encode_utf16() {
            bytes.extend_from_slice(&u.to_le_bytes());
        }
        assert_eq!(parse_task_xml(&decode_console(&bytes)).minutes, 30);
        assert_eq!(parse_task_xml("<Task/>").minutes, 0);
    }
}
