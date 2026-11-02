//! Remembers how far test result downloads have got, so each run only fetches new results.
//!
//! The watermark is the StrataSync *sync time* (when the server received a test),
//! not the test's creation time. A meter can run a test in the morning and sync
//! it hours later; filtering on creation time would miss that test forever once
//! the watermark had moved past it, while sync time only ever moves forward.
//!
//! The watermark is the latest syncTime seen, taken from the server's own
//! timestamps so the local clock doesn't matter. The next run searches from that
//! time inclusive, and the IDs already processed at exactly that second are
//! remembered so they aren't downloaded twice.

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use serde_json::{Value, json};

use crate::timeutil::{self, Time};

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SyncState {
    pub last_sync_time: Option<Time>,
    pub ids_at_last_sync_time: Vec<i64>,
    pub last_run_at: Option<Time>,
    pub last_run_results: usize,
}

fn record_sync_time(record: &Value) -> Option<Time> {
    record.get("syncTime").and_then(Value::as_str).filter(|s| !s.is_empty()).and_then(timeutil::parse_aware)
}

fn record_id(record: &Value) -> Option<i64> {
    record.get("_id").and_then(Value::as_i64)
}

impl SyncState {
    pub fn load(path: &Path) -> std::io::Result<SyncState> {
        if !path.is_file() {
            return Ok(SyncState::default());
        }
        let data: Value = serde_json::from_str(&fs::read_to_string(path)?).map_err(std::io::Error::other)?;
        let time = |k: &str| data.get(k).and_then(Value::as_str).and_then(timeutil::parse_aware);
        Ok(SyncState {
            last_sync_time: time("last_sync_time"),
            ids_at_last_sync_time: data
                .get("ids_at_last_sync_time")
                .and_then(Value::as_array)
                .map(|ids| ids.iter().filter_map(Value::as_i64).collect())
                .unwrap_or_default(),
            last_run_at: time("last_run_at"),
            last_run_results: data.get("last_run_results").and_then(Value::as_u64).unwrap_or(0) as usize,
        })
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        let mut ids = self.ids_at_last_sync_time.clone();
        ids.sort();
        let data = json!({
            "last_sync_time": self.last_sync_time.as_ref().map(timeutil::iso_utc_z),
            "ids_at_last_sync_time": ids,
            "last_run_at": self.last_run_at.as_ref().map(timeutil::iso_utc_z),
            "last_run_results": self.last_run_results,
        });
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, serde_json::to_string_pretty(&data).map_err(std::io::Error::other)? + "\n")?;
        fs::rename(&tmp, path) // atomic, so an interrupted run can't leave a half-written file
    }

    /// True if this search result was handled by a previous run.
    pub fn is_already_processed(&self, record: &Value) -> bool {
        let (Some(last), Some(synced)) = (self.last_sync_time, record_sync_time(record)) else {
            return false;
        };
        if synced < last {
            return true;
        }
        synced == last && record_id(record).is_some_and(|id| self.ids_at_last_sync_time.contains(&id))
    }

    /// Move the watermark forward to the newest syncTime among `records`.
    pub fn advance(&mut self, records: &[Value], run_at: Time) {
        let timed: Vec<(Time, Option<i64>)> =
            records.iter().filter_map(|r| record_sync_time(r).map(|t| (t, record_id(r)))).collect();
        self.last_run_at = Some(run_at);
        self.last_run_results = timed.len();
        let Some(newest) = timed.iter().map(|(t, _)| *t).max() else { return };
        let mut ids_at_newest: BTreeSet<i64> =
            timed.iter().filter(|(t, _)| *t == newest).filter_map(|(_, id)| *id).collect();
        if self.last_sync_time == Some(newest) {
            ids_at_newest.extend(self.ids_at_last_sync_time.iter().copied());
        }
        if self.last_sync_time.is_none_or(|last| newest >= last) {
            self.last_sync_time = Some(newest);
            self.ids_at_last_sync_time = ids_at_newest.into_iter().collect();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(id: i64, sync: &str) -> Value {
        json!({"_id": id, "syncTime": sync})
    }

    #[test]
    fn watermark() {
        let mut state = SyncState::default();
        let run_at = timeutil::parse_aware("2026-10-29T13:00:00Z").unwrap();
        assert!(!state.is_already_processed(&rec(1, "2026-10-29T12:00:00Z")));
        state.advance(
            &[rec(1, "2026-10-29T12:00:00Z"), rec(2, "2026-10-29T12:30:00Z"), rec(3, "2026-10-29T12:30:00Z")],
            run_at,
        );
        assert_eq!(state.ids_at_last_sync_time, vec![2, 3]);
        assert_eq!(state.last_run_results, 3);
        assert!(state.is_already_processed(&rec(1, "2026-10-29T12:00:00Z")));
        assert!(state.is_already_processed(&rec(2, "2026-10-29T12:30:00Z")));
        assert!(!state.is_already_processed(&rec(4, "2026-10-29T12:30:00Z")));
        assert!(!state.is_already_processed(&rec(5, "2026-10-29T12:31:00Z")));
        // Same second again: the IDs are merged.
        state.advance(&[rec(4, "2026-10-29T12:30:00Z")], run_at);
        assert_eq!(state.ids_at_last_sync_time, vec![2, 3, 4]);
        // Nothing new: position stays.
        state.advance(&[], run_at);
        assert_eq!(state.last_sync_time, timeutil::parse_aware("2026-10-29T12:30:00Z"));
    }

    #[test]
    fn file_format_matches_python() {
        let dir = std::env::temp_dir().join(format!("pon-audit-state-{}", std::process::id()));
        let path = dir.join("last_download-abc.json");
        fs::create_dir_all(&dir).unwrap();
        fs::write(&path, r#"{"last_sync_time": "2026-10-29T12:30:00Z", "ids_at_last_sync_time": [7, 3], "last_run_at": "2026-10-29T13:00:00Z", "last_run_results": 4}"#).unwrap();
        let state = SyncState::load(&path).unwrap();
        assert_eq!(state.ids_at_last_sync_time, vec![7, 3]);
        state.save(&path).unwrap();
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.contains(r#""last_sync_time": "2026-10-29T12:30:00Z""#), "{text}");
        assert!(text.contains("3,\n    7"), "{text}");
        assert_eq!(SyncState::load(&dir.join("none.json")).unwrap(), SyncState::default());
        fs::remove_dir_all(&dir).unwrap();
    }
}
