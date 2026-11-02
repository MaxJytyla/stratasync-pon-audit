//! Client for the StrataSync TPA Test Results JSON API (v1).
//!
//! Reference: "StrataSync TPA Test Results JSON API Developer Guide" (May 2023).
//!
//! Auth: POST {base_url}/oauth/token with client credentials returns a bearer
//! token (valid ~15 minutes; typically only one valid token per client ID, so we
//! cache and reuse it). Search: POST {base_url}/api/v1/testresults/search.
//!
//! Search constraints from the guide:
//!   * createdOn or syncTime is required unless filtering by workflowId or _id.
//!   * Each date range must cover at most a 24-hour period, so longer
//!     queries are split into several requests.
//!   * Response fields vary by test type and firmware; never assume a fixed set.

use std::collections::HashSet;
use std::io::Read;
use std::time::{Duration as StdDuration, Instant};

use chrono::Duration;
use reqwest::StatusCode;
use reqwest::blocking::{Client, RequestBuilder, Response};
use serde_json::{Map, Value, json};

use crate::config::StrataSyncSettings;
use crate::error::{AppError, Result};
use crate::pon_audit::json_text;
use crate::timeutil::{self, Time};

const TOKEN_PATH: &str = "/oauth/token";
const SEARCH_PATH: &str = "/api/v1/testresults/search";
pub const SYNC_TIME: &str = "syncTime";

/// Renew the token this long before it actually expires.
const TOKEN_MARGIN_S: f64 = 60.0;
/// Retries for 429 and 5xx responses and network errors (with 2, 4, 8 s waits).
const RETRIES: u32 = 3;
const RETRY_STATUSES: [u16; 5] = [429, 500, 502, 503, 504];

/// Longest span one search may cover (the API allows at most 24 hours).
fn max_window() -> Duration {
    Duration::hours(24) - Duration::seconds(1)
}

/// Why a full test file couldn't be had.
#[derive(Debug)]
pub enum DownloadError {
    /// The download failed (retry next time).
    Fetch(AppError),
    /// It downloaded, but isn't JSON (or gzipped JSON).
    Unreadable(String),
}

pub struct StrataSyncClient {
    settings: StrataSyncSettings,
    http: Client,
    token: Option<String>,
    token_expires_at: Option<Instant>,
}

impl StrataSyncClient {
    pub fn new(settings: StrataSyncSettings) -> Result<StrataSyncClient> {
        let http = Client::builder()
            .timeout(StdDuration::from_secs(60))
            .user_agent(concat!("stratasync-pon-audit/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|e| AppError::other(format!("Could not set up HTTPS: {e}")))?;
        // A token supplied directly has an unknown expiry; treat it as valid
        // until the server says otherwise.
        let token = settings.access_token.clone();
        Ok(StrataSyncClient { settings, http, token, token_expires_at: None })
    }

    fn can_refresh(&self) -> bool {
        self.settings.client_id.is_some() && self.settings.client_secret.is_some()
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.settings.base_url)
    }

    // ---- auth --------------------------------------------------------------------

    /// Request a new access token with the client credentials, and use it from now on.
    pub fn request_token(&mut self) -> Result<Value> {
        let (Some(id), Some(secret)) = (self.settings.client_id.clone(), self.settings.client_secret.clone()) else {
            return Err(AppError::stratasync(
                "A client ID and client secret are required to request a token",
                None,
                None,
            ));
        };
        let form =
            [("grant_type", "client_credentials"), ("scope", ""), ("client_id", &id), ("client_secret", &secret)];
        let url = self.url(TOKEN_PATH);
        let resp = send(|| self.http.post(&url).form(&form).header("Accept", "application/json"))?;
        let status = resp.status();
        let text = resp.text().unwrap_or_default();
        if status != StatusCode::OK {
            return Err(AppError::stratasync(
                format!("Token request failed: HTTP {}", status.as_u16()),
                Some(status.as_u16()),
                Some(text),
            ));
        }
        let body: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
        let Some(token) = body.get("access_token").and_then(Value::as_str) else {
            return Err(AppError::stratasync("Token response did not contain an access_token", Some(200), Some(text)));
        };
        self.token = Some(token.to_string());
        let expires_in = match body.get("expires_in") {
            Some(Value::Number(n)) => n.as_f64().unwrap_or(900.0),
            Some(Value::String(s)) => s.parse().unwrap_or(900.0),
            _ => 900.0,
        };
        self.token_expires_at =
            Some(Instant::now() + StdDuration::from_secs_f64((expires_in - TOKEN_MARGIN_S).max(0.0)));
        Ok(body)
    }

    /// The current token, renewed if expired (or if `force_refresh`, after a 401).
    fn bearer(&mut self, force_refresh: bool) -> Result<String> {
        let expired = self.token_expires_at.is_some_and(|t| Instant::now() >= t);
        if self.can_refresh() && (force_refresh || expired || self.token.is_none()) {
            self.request_token()?;
        }
        self.token.clone().ok_or_else(|| AppError::stratasync("No access token available", None, None))
    }

    /// Send an authorized request; on 401, renew the token once (if we can) and retry.
    fn authorized(&mut self, build: impl Fn(&Client, &str) -> RequestBuilder) -> Result<Response> {
        let mut attempt = 1;
        loop {
            let token = self.bearer(attempt == 2)?;
            let resp = send(|| build(&self.http, &token))?;
            if resp.status() == StatusCode::UNAUTHORIZED && attempt == 1 && self.can_refresh() {
                attempt = 2;
                continue;
            }
            return Ok(resp);
        }
    }

    // ---- search ---------------------------------------------------------------------

    /// POST a raw search query and return the list of result records.
    pub fn search(&mut self, query: &Value) -> Result<Vec<Value>> {
        let url = self.url(SEARCH_PATH);
        let resp = self.authorized(|http, token| {
            http.post(&url).json(query).bearer_auth(token).header("Accept", "application/json")
        })?;
        let status = resp.status().as_u16();
        let text = resp.text().unwrap_or_default();
        let body: Option<Map<String, Value>> = match serde_json::from_str(&text) {
            Ok(Value::Object(m)) => Some(m),
            _ => None,
        };
        let Some(body) = body.filter(|_| status == 200) else {
            let message = match serde_json::from_str::<Value>(&text) {
                Ok(Value::Object(m)) => json_text(m.get("message")),
                _ => text.chars().take(500).collect(),
            };
            let hint = if status == 401 && !self.can_refresh() {
                " (access token may have expired; tokens last ~15 minutes)"
            } else {
                ""
            };
            return Err(AppError::stratasync(
                format!("HTTP {status} from {SEARCH_PATH}: {message}{hint}"),
                Some(status),
                Some(text),
            ));
        };
        if body.get("type").and_then(Value::as_str) != Some("success") {
            let message = match body.get("message") {
                Some(m) if !m.is_null() && m != "" => json_text(Some(m)),
                _ => Value::Object(body.clone()).to_string(),
            };
            let status = body.get("httpStatus").and_then(Value::as_u64).map(|s| s as u16);
            return Err(AppError::stratasync(format!("Search failed: {message}"), status, Some(text)));
        }
        Ok(match body.get("result") {
            Some(Value::Array(list)) => list.clone(),
            _ => Vec::new(),
        })
    }

    /// Search an arbitrary time span, split into windows under 24 hours.
    ///
    /// Both ends are inclusive. Results are de-duplicated by `_id`.
    pub fn search_between(&mut self, start: Time, end: Time, by: &str) -> Result<Vec<Value>> {
        let mut seen = HashSet::new();
        let mut out = Vec::new();
        let mut window_start = start;
        while window_start <= end {
            let window_end = std::cmp::min(window_start + max_window(), end);
            let query = json!({
                by: {"startDate": timeutil::iso(&window_start), "endDate": timeutil::iso(&window_end)}
            });
            for rec in self.search(&query)? {
                if seen.insert(rec.get("_id").map(Value::to_string).unwrap_or_default()) {
                    out.push(rec);
                }
            }
            window_start = window_end + Duration::seconds(1);
        }
        Ok(out)
    }

    // ---- download ---------------------------------------------------------------------

    /// Download the raw test data file for a search result's `_id`.
    pub fn download_contents(&mut self, test_id: i64) -> Result<Vec<u8>> {
        let path = format!("/api/v1/testresults/{test_id}/contents");
        let url = self.url(&path);
        let resp = self.authorized(|http, token| http.get(&url).bearer_auth(token))?;
        let status = resp.status().as_u16();
        let bytes = resp.bytes().map_err(network_error)?.to_vec();
        if status != 200 {
            let text: String = String::from_utf8_lossy(&bytes).chars().take(300).collect();
            return Err(AppError::stratasync(
                format!("HTTP {status} downloading test {test_id}: {text}"),
                Some(status),
                Some(String::from_utf8_lossy(&bytes).into_owned()),
            ));
        }
        Ok(bytes)
    }

    /// Download a JSON test data file (e.g. a TruePON .wfl.json) and parse it.
    pub fn download_json(&mut self, test_id: i64) -> std::result::Result<Value, DownloadError> {
        let content = self.download_contents(test_id).map_err(DownloadError::Fetch)?;
        parse_test_file(&content).map_err(DownloadError::Unreadable)
    }
}

/// A test file is JSON, or gzipped JSON (format application/zip / .json.gz).
pub fn parse_test_file(content: &[u8]) -> std::result::Result<Value, String> {
    match serde_json::from_slice(content) {
        Ok(v) => Ok(v),
        Err(json_err) => {
            let mut text = Vec::new();
            flate2::read::GzDecoder::new(content).read_to_end(&mut text).map_err(|_| json_err.to_string())?;
            serde_json::from_slice(&text).map_err(|e| e.to_string())
        }
    }
}

fn network_error(e: reqwest::Error) -> AppError {
    let mut text = e.to_string();
    let mut source = std::error::Error::source(&e);
    while let Some(s) = source {
        text.push_str(&format!(": {s}"));
        source = s.source();
    }
    AppError::stratasync(format!("Could not reach StrataSync: {text}"), None, None)
}

/// Send a request, retrying 429/5xx responses and network errors with backoff
/// (honouring Retry-After). After the last retry the response is returned as is.
fn send(build: impl Fn() -> RequestBuilder) -> Result<Response> {
    let mut retry = 0;
    loop {
        let wait = StdDuration::from_secs(2u64 << retry);
        match build().send() {
            Ok(resp) if retry < RETRIES && RETRY_STATUSES.contains(&resp.status().as_u16()) => {
                let after = resp
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.trim().parse::<u64>().ok())
                    .map(|s| StdDuration::from_secs(s.min(120)));
                std::thread::sleep(after.unwrap_or(wait));
            }
            Ok(resp) => return Ok(resp),
            Err(e) if retry < RETRIES && (e.is_connect() || e.is_timeout() || e.is_request()) => {
                std::thread::sleep(wait);
            }
            Err(e) => return Err(network_error(e)),
        }
        retry += 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::serve;
    use std::io::Write;

    #[test]
    fn test_files_plain_or_gzipped() {
        assert_eq!(parse_test_file(br#"{"a": 1}"#).unwrap()["a"], 1);
        let mut gz = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        gz.write_all(br#"{"b": 2}"#).unwrap();
        assert_eq!(parse_test_file(&gz.finish().unwrap()).unwrap()["b"], 2);
        assert!(parse_test_file(b"<html>").is_err());
    }

    fn client(base: String) -> StrataSyncClient {
        StrataSyncClient::new(StrataSyncSettings {
            base_url: base,
            access_token: None,
            client_id: Some("id".into()),
            client_secret: Some("secret".into()),
        })
        .unwrap()
    }

    #[test]
    fn token_search_and_401_renewal() {
        let (base, seen) = serve(vec![
            (200, r#"{"access_token": "T1", "expires_in": 900}"#),
            (401, r#"{"message": "expired"}"#),
            (200, r#"{"access_token": "T2", "expires_in": 900}"#),
            (200, r#"{"type": "success", "result": [{"_id": 1}, {"_id": 2}]}"#),
        ]);
        let mut c = client(base);
        let found = c.search(&json!({"syncTime": {"startDate": "x"}})).unwrap();
        assert_eq!(found.len(), 2);
        let seen = seen.lock().unwrap();
        assert!(seen[0].starts_with("POST /oauth/token"));
        assert!(seen[0].contains("grant_type=client_credentials") && seen[0].contains("client_secret=secret"));
        assert!(seen[1].contains("authorization: Bearer T1") || seen[1].contains("Authorization: Bearer T1"));
        assert!(seen[3].to_lowercase().contains("bearer t2"));
    }

    #[test]
    fn errors() {
        let (base, _) = serve(vec![(400, r#"{"error": "invalid_client"}"#)]);
        let err = client(base).search(&json!({})).unwrap_err();
        assert!(err.text().contains("Token request failed: HTTP 400"));
        assert!(err.text().contains("Check the client ID and secret"));

        let (base, _) = serve(vec![
            (200, r#"{"access_token": "T"}"#),
            (200, r#"{"type": "error", "message": "bad range", "httpStatus": 400}"#),
        ]);
        let err = client(base).search(&json!({})).unwrap_err();
        assert!(
            matches!(&err, AppError::StrataSync { message, status: Some(400), .. } if message == "Search failed: bad range"),
            "{err:?}"
        );

        let (base, _) = serve(vec![(200, r#"{"access_token": "T"}"#), (404, "not here")]);
        let err = client(base).download_json(5).unwrap_err();
        assert!(
            matches!(err, DownloadError::Fetch(e) if e.text().starts_with("HTTP 404 downloading test 5: not here"))
        );

        let (base, _) = serve(vec![(200, r#"{"access_token": "T"}"#), (200, "<html>")]);
        assert!(matches!(client(base).download_json(5).unwrap_err(), DownloadError::Unreadable(_)));
    }

    #[test]
    fn retries_server_errors() {
        let (base, seen) = serve(vec![
            (200, r#"{"access_token": "T"}"#),
            (503, "busy"),
            (200, r#"{"type": "success", "result": []}"#),
        ]);
        let start = Instant::now();
        assert!(client(base).search(&json!({})).unwrap().is_empty());
        assert_eq!(seen.lock().unwrap().len(), 3);
        assert!(start.elapsed() >= StdDuration::from_secs(2));
    }

    #[test]
    fn long_spans_are_split_into_windows() {
        let (base, seen) = serve(vec![
            (200, r#"{"access_token": "T"}"#),
            (200, r#"{"type": "success", "result": [{"_id": 1}, {"_id": 2}]}"#),
            (200, r#"{"type": "success", "result": [{"_id": 2}, {"_id": 3}]}"#),
        ]);
        let start = timeutil::parse_aware("2026-10-28T00:00:00-04:00").unwrap();
        let end = timeutil::parse_aware("2026-10-29T12:00:00-04:00").unwrap();
        let found = client(base).search_between(start, end, SYNC_TIME).unwrap();
        assert_eq!(found.iter().map(|r| r["_id"].as_i64().unwrap()).collect::<Vec<_>>(), [1, 2, 3]);
        let seen = seen.lock().unwrap();
        assert!(
            seen[1].contains(r#""startDate":"2026-10-28T00:00:00-04:00","endDate":"2026-10-28T23:59:59-04:00""#),
            "{}",
            seen[1]
        );
        assert!(seen[2].contains(r#""startDate":"2026-10-29T00:00:00-04:00","endDate":"2026-10-29T12:00:00-04:00""#));
    }
}
