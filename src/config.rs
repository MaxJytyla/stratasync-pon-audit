//! Settings for StrataSync and the Google Sheet, and where the app keeps its files.
//!
//! Nothing secret lives in code. The app collects the values in its window and,
//! if the user asks it to, remembers them in a settings file in the per-user
//! application data folder (see `app_data_dir`). The files are the same as the
//! Python version's, so settings, download positions and history carry over.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::{AppError, Result};

pub const APP_NAME: &str = "StrataSync PON Audit";

/// StrataSync production servers, by region.
pub const STRATASYNC_URLS: [(&str, &str); 2] =
    [("USA", "https://stratasync.viavisolutions.com"), ("EU", "https://eu.stratasync.viavisolutions.com")];
pub const DEFAULT_REGION: &str = "USA";

pub fn region_url(region: &str) -> Option<&'static str> {
    STRATASYNC_URLS.iter().find(|(name, _)| *name == region).map(|(_, url)| *url)
}

#[derive(Debug, Clone)]
pub struct Settings {
    /// The service account key file, parsed.
    pub service_account_info: Map<String, Value>,
    pub sheet_id: String,
}

impl Settings {
    pub fn service_account_email(&self) -> &str {
        self.service_account_info.get("client_email").and_then(Value::as_str).unwrap_or("the service account")
    }
}

#[derive(Debug, Clone)]
pub struct StrataSyncSettings {
    pub base_url: String,
    pub access_token: Option<String>,
    pub client_id: Option<String>,
    pub client_secret: Option<String>,
}

/// The per-user folder for settings, the download position and the history.
pub fn app_data_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("PON_AUDIT_DATA_DIR") {
        return PathBuf::from(dir); // for tests
    }
    let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let base = if cfg!(windows) {
        std::env::var_os("APPDATA").map(PathBuf::from).unwrap_or_else(|| home.join("AppData").join("Roaming"))
    } else if cfg!(target_os = "macos") {
        home.join("Library").join("Application Support")
    } else {
        std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).unwrap_or_else(|| home.join(".config"))
    };
    base.join(APP_NAME)
}

// ---- validation -------------------------------------------------------------------

static SHEET_URL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"/spreadsheets/d/([A-Za-z0-9_-]+)").unwrap());
static SHEET_ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_-]+$").unwrap());

/// Accept a bare sheet ID or a full Google Sheets URL and return the ID.
pub fn parse_sheet_id(value: &str) -> Result<String> {
    let value = value.trim();
    if let Some(m) = SHEET_URL.captures(value) {
        return Ok(m[1].to_string());
    }
    if value.is_empty() {
        return Err(AppError::config("Enter the Google Sheet ID."));
    }
    if !SHEET_ID.is_match(value) {
        return Err(AppError::config(
            "The Google Sheet ID should look like the part of the sheet's URL between /d/ and /edit.",
        ));
    }
    Ok(value.to_string())
}

/// Parse and sanity-check the contents of a service account key file.
pub fn parse_service_account(text: &str) -> Result<Map<String, Value>> {
    if text.trim().is_empty() {
        return Err(AppError::config("Load or paste the Google service account key (service_account.json)."));
    }
    let info: Value = serde_json::from_str(text)
        .map_err(|e| AppError::config(format!("The service account key is not valid JSON: {e}")))?;
    let Value::Object(info) = info else {
        return Err(not_a_key());
    };
    if info.get("type").and_then(Value::as_str) != Some("service_account") {
        return Err(not_a_key());
    }
    let missing: Vec<&str> = ["client_email", "private_key"]
        .into_iter()
        .filter(|k| info.get(*k).and_then(Value::as_str).is_none_or(str::is_empty))
        .collect();
    if !missing.is_empty() {
        return Err(AppError::config(format!("The service account key is missing {}.", missing.join(", "))));
    }
    Ok(info)
}

fn not_a_key() -> AppError {
    AppError::config(
        "That JSON is not a Google service account key (it should contain \"type\": \"service_account\").",
    )
}

pub fn make_settings(sheet_id: &str, service_account_json: &str) -> Result<Settings> {
    Ok(Settings {
        service_account_info: parse_service_account(service_account_json)?,
        sheet_id: parse_sheet_id(sheet_id)?,
    })
}

pub fn make_stratasync_settings(region: &str, client_id: &str, client_secret: &str) -> Result<StrataSyncSettings> {
    let url = region_url(region).ok_or_else(|| AppError::config(format!("Unknown StrataSync region '{region}'.")))?;
    let (client_id, client_secret) = (client_id.trim(), client_secret.trim());
    if client_id.is_empty() || client_secret.is_empty() {
        return Err(AppError::config(
            "Enter the StrataSync client ID and client secret (VIAVI TAC provides these).",
        ));
    }
    Ok(StrataSyncSettings {
        base_url: url.to_string(),
        access_token: None,
        client_id: Some(client_id.to_string()),
        client_secret: Some(client_secret.to_string()),
    })
}

// ---- remembered form values ---------------------------------------------------------

/// What the app's window remembers between runs (settings.json).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct SavedForm {
    pub region: String,
    pub client_id: String,
    pub client_secret: String,
    pub service_account_json: String,
    pub sheet_id: String,
    pub remember_secrets: bool,
    pub auto_update_minutes: u32,
}

impl Default for SavedForm {
    fn default() -> Self {
        SavedForm {
            region: DEFAULT_REGION.into(),
            client_id: String::new(),
            client_secret: String::new(),
            service_account_json: String::new(),
            sheet_id: String::new(),
            remember_secrets: true,
            auto_update_minutes: 60,
        }
    }
}

pub fn settings_path() -> PathBuf {
    app_data_dir().join("settings.json")
}

impl SavedForm {
    pub fn load() -> SavedForm {
        Self::load_from(&settings_path())
    }

    pub fn load_from(path: &Path) -> SavedForm {
        fs::read_to_string(path).ok().and_then(|text| serde_json::from_str(&text).ok()).unwrap_or_default()
    }

    pub fn save(&self) -> std::io::Result<()> {
        self.save_to(&settings_path())
    }

    pub fn save_to(&self, path: &Path) -> std::io::Result<()> {
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        let mut data = self.clone();
        if !self.remember_secrets {
            data.client_secret.clear();
            data.service_account_json.clear();
        }
        let tmp = path.with_extension("tmp");
        let _ = fs::remove_file(&tmp);
        // Owner-only from the start, since it may hold the client secret and Google key.
        let mut fh = owner_only_file(&tmp)?;
        fh.write_all(serde_json::to_string_pretty(&data).map_err(std::io::Error::other)?.as_bytes())?;
        fh.sync_all()?;
        drop(fh);
        fs::rename(&tmp, path)
    }
}

#[cfg(unix)]
fn owner_only_file(path: &Path) -> std::io::Result<fs::File> {
    use std::os::unix::fs::OpenOptionsExt;
    fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(path)
}

#[cfg(not(unix))]
fn owner_only_file(path: &Path) -> std::io::Result<fs::File> {
    // %APPDATA% is already private to the user on Windows.
    fs::OpenOptions::new().write(true).create(true).truncate(true).open(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: &str = r#"{"type": "service_account", "client_email": "bot@x.iam.gserviceaccount.com", "private_key": "k"}"#;

    #[test]
    fn sheet_ids() {
        assert_eq!(parse_sheet_id(" https://docs.google.com/spreadsheets/d/1AbC-d_9/edit#gid=0 ").unwrap(), "1AbC-d_9");
        assert_eq!(parse_sheet_id("1AbC-d_9").unwrap(), "1AbC-d_9");
        assert!(parse_sheet_id("").is_err());
        assert!(parse_sheet_id("not an id!").is_err());
    }

    #[test]
    fn service_account_keys() {
        assert_eq!(parse_service_account(KEY).unwrap()["client_email"], "bot@x.iam.gserviceaccount.com");
        assert!(parse_service_account("").unwrap_err().text().contains("Load or paste"));
        assert!(parse_service_account("{").unwrap_err().text().contains("not valid JSON"));
        assert!(parse_service_account("[]").unwrap_err().text().contains("not a Google service account key"));
        assert!(
            parse_service_account(r#"{"type": "service_account", "client_email": ""}"#)
                .unwrap_err()
                .text()
                .contains("missing client_email, private_key")
        );
    }

    #[test]
    fn stratasync_settings() {
        let s = make_stratasync_settings("EU", " id ", " secret ").unwrap();
        assert_eq!(s.base_url, "https://eu.stratasync.viavisolutions.com");
        assert_eq!(s.client_id.as_deref(), Some("id"));
        assert!(make_stratasync_settings("Mars", "a", "b").is_err());
        assert!(make_stratasync_settings("USA", "a", " ").is_err());
    }

    #[test]
    fn saved_form_roundtrip_and_python_compat() {
        let dir = std::env::temp_dir().join(format!("pon-audit-test-{}", std::process::id()));
        let path = dir.join("settings.json");
        // A file written by the Python app, with an extra unknown key.
        fs::create_dir_all(&dir).unwrap();
        fs::write(&path, r#"{"region": "EU", "client_id": "abc", "sheet_id": "S", "auto_update_minutes": 30, "old": 1}"#)
            .unwrap();
        let form = SavedForm::load_from(&path);
        assert_eq!((form.region.as_str(), form.client_id.as_str(), form.auto_update_minutes), ("EU", "abc", 30));
        assert!(form.remember_secrets);

        let form = SavedForm { client_secret: "s".into(), remember_secrets: false, ..form };
        form.save_to(&path).unwrap();
        let back = SavedForm::load_from(&path);
        assert_eq!(back.client_secret, "");
        assert!(!back.remember_secrets);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        }
        assert_eq!(SavedForm::load_from(&dir.join("missing.json")), SavedForm::default());
        fs::remove_dir_all(&dir).unwrap();
    }
}
