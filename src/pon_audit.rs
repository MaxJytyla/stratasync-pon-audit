//! Turn TruePON test results into cell updates for the RMN2 PON Audit sheet.
//!
//! Only five tabs are ever written:
//!   * "RMN2.DH{A,B,C,D} PON Audit" - rack-level tests, one row per (DH, Row, Rack).
//!     Test label "RMN2.DHA.12.25B" -> DH A, Row 12, Rack 25, channel BB (the "P2"
//!     columns; "P1" columns are channel AA).
//!   * "RMN2 F1 & F2" - F1 tests, one row per (DH, Row, Channel).
//!     Test label "RMN2.DHA.01.F1.02A" -> DH A, Row 1, Channel A, F1.02 columns.
//!
//! For each test we write the downstream power and the test timestamp. The
//! "OLT and Port" / "OLT-ID" columns hold the *expected* OLT port and are never
//! overwritten; instead, the measured OLT ID is compared against them. A result
//! with any problem (wrong OLT ID, no reading, ...) is not written at all, only
//! flagged; the sheet's Issue columns are left alone.
//!
//! OLT IDs arrive as four decimal integers (oltIdFormat "hex"). Each is shown as
//! two hex digits: [1, 23, 170, 1] -> "01 17 AA 01" = uOLT 17, channel A, port 01.
//! Those hex digits are then read literally: uOLT and port as decimal numbers,
//! the channel as a repeated letter.
//!
//! Everything here is pure logic: sheets are passed in as rows of displayed
//! values, and the output is a plan of cell updates that a writer can apply.

use std::collections::HashMap;
use std::fmt;
use std::sync::LazyLock;

use chrono::Duration;
use regex::Regex;
use serde::Serialize;
use serde_json::Value;

use crate::timeutil::{self, Time};

pub const SITE: &str = "RMN2";
pub const DATA_HALLS: [&str; 4] = ["A", "B", "C", "D"];
pub const F1_TAB: &str = "RMN2 F1 & F2";

/// The audited network is XGS-PON. A meter set up to test several PON types at
/// once (signalSetup "gpon;xgspon") reports one result per type; only the
/// XGS-PON one is used.
pub const PON_SERVICE: &str = "xgspon";

pub fn rack_tab(data_hall: &str) -> String {
    format!("{SITE}.DH{data_hall} PON Audit")
}

pub fn audit_tabs() -> Vec<String> {
    let mut tabs: Vec<String> = DATA_HALLS.iter().map(|dh| rack_tab(dh)).collect();
    tabs.push(F1_TAB.to_string());
    tabs
}

/// Header names for the cells one test writes to. Matched after collapsing
/// whitespace (the sheet has "OLT and Port  (B)" with two spaces).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Columns {
    pub power: &'static str,
    pub expected_olt: &'static str,
    pub timestamp: &'static str,
}

pub const RACK_KEY_HEADERS: [&str; 3] = ["DH", "Row", "Rack"];
pub const F1_KEY_HEADERS: [&str; 3] = ["DH", "Row", "Channel"];

/// Label suffix A = channel AA = "P1"; B = channel BB = "P2".
pub fn rack_columns(side: &str) -> Option<Columns> {
    match side {
        "A" => Some(Columns { power: "dBm in Port 1", expected_olt: "OLT and Port (A)", timestamp: "Timestamp P1" }),
        "B" => Some(Columns { power: "dBm in Port 2", expected_olt: "OLT and Port (B)", timestamp: "Timestamp P2" }),
        _ => None,
    }
}

pub fn f1_columns(index: u8) -> Option<Columns> {
    match index {
        1 => Some(Columns { power: "F1.01", expected_olt: "OLT-ID 1", timestamp: "Timestamp F1.01" }),
        2 => Some(Columns { power: "F1.02", expected_olt: "OLT-ID 2", timestamp: "Timestamp F1.02" }),
        3 => Some(Columns { power: "F1.03", expected_olt: "OLT-ID 3", timestamp: "Timestamp F1.03" }),
        _ => None,
    }
}

static RACK_LABEL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"^{SITE}\.DH([A-D])\.(\d+)\.(\d+)([AB])$")).unwrap());
static F1_LABEL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"^{SITE}\.DH([A-D])\.(\d+)\.F1\.0?([1-3])([AB])$")).unwrap());

// ---- small Python-compatible formatting helpers ---------------------------------

/// A string as Python's repr() shows it ('abc'), for messages that match the Python app's.
pub fn py_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// A float as Python's str() shows it: -18.0 stays "-18.0".
pub fn py_float(v: f64) -> String {
    if v.is_finite() && v.fract() == 0.0 && v.abs() < 1e16 {
        format!("{v:.1}")
    } else {
        format!("{v}")
    }
}

/// A JSON value as text: strings as-is, null/missing as "None" (like Python's f-strings).
pub fn json_text(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

/// A JSON value as an optional string; empty and null are None.
fn opt_text(v: Option<&Value>) -> Option<String> {
    match v {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Bool(false)) => None,
        Some(other) => Some(other.to_string()),
    }
}

fn opt_f64(v: Option<&Value>) -> Result<Option<f64>, String> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) => Ok(n.as_f64()),
        Some(Value::String(s)) if s.trim().is_empty() => Ok(None),
        Some(Value::String(s)) => s
            .trim()
            .parse::<f64>()
            .map(Some)
            .map_err(|_| format!("could not convert string to float: {}", py_repr(s))),
        Some(other) => Err(format!("not a number: {other}")),
    }
}

fn opt_i64(v: Option<&Value>) -> Option<i64> {
    match v {
        Some(Value::Number(n)) => n.as_i64(),
        Some(Value::String(s)) => s.trim().parse().ok(),
        _ => None,
    }
}

fn obj<'a>(v: Option<&'a Value>) -> &'a serde_json::Map<String, Value> {
    static EMPTY: LazyLock<serde_json::Map<String, Value>> = LazyLock::new(serde_json::Map::new);
    match v {
        Some(Value::Object(m)) => m,
        _ => &EMPTY,
    }
}

// ---- OLT IDs ------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OltId {
    pub raw: [u8; 4],
}

impl OltId {
    pub fn hex_parts(&self) -> [String; 4] {
        self.raw.map(|v| format!("{v:02X}"))
    }

    /// The form used in the sheet, e.g. "01 17 AA 01".
    pub fn sheet_format(&self) -> String {
        self.hex_parts().join(" ")
    }

    /// True for [0, 0, 0, 0], which the meter reports when no OLT ID was read.
    pub fn is_blank(&self) -> bool {
        self.raw.iter().all(|v| *v == 0)
    }

    pub fn uolt(&self) -> Option<u32> {
        hex_digits_as_decimal(&self.hex_parts()[1])
    }

    pub fn channel(&self) -> Option<char> {
        let part = &self.hex_parts()[2];
        let mut chars = part.chars();
        let (a, b) = (chars.next()?, chars.next()?);
        (a == b && "ABCDEF".contains(a)).then_some(a)
    }

    pub fn port(&self) -> Option<u32> {
        hex_digits_as_decimal(&self.hex_parts()[3])
    }

    pub fn is_well_formed(&self) -> bool {
        self.raw[0] == 1 && self.uolt().is_some() && self.channel().is_some() && self.port().is_some()
    }

    pub fn describe(&self) -> String {
        if self.is_blank() {
            return "no OLT ID".into();
        }
        match (self.is_well_formed(), self.uolt(), self.channel(), self.port()) {
            (true, Some(u), Some(c), Some(p)) => format!("oltna{u:02} channel {c} port {p}"),
            _ => format!("unrecognized OLT ID {}", self.sheet_format()),
        }
    }
}

fn hex_digits_as_decimal(hex: &str) -> Option<u32> {
    if hex.chars().all(|c| c.is_ascii_digit()) { hex.parse().ok() } else { None }
}

/// Four integers 0-255, as in a test file.
pub fn decode_olt_id(value: Option<&Value>) -> Result<Option<OltId>, String> {
    let list = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Array(list)) => list,
        Some(other) => return Err(format!("OLT ID must be four integers 0-255, got {other}")),
    };
    let nums: Vec<u8> = list.iter().filter_map(|v| v.as_u64().filter(|n| *n <= 255).map(|n| n as u8)).collect();
    if list.len() != 4 || nums.len() != 4 {
        return Err(format!("OLT ID must be four integers 0-255, got {}", Value::Array(list.clone())));
    }
    Ok(Some(OltId { raw: [nums[0], nums[1], nums[2], nums[3]] }))
}

/// Hex text, as in a search result: "01 21 AA 09".
fn olt_from_hex_text(value: Option<&Value>) -> Result<Option<OltId>, String> {
    let text = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    };
    if text.trim().is_empty() {
        return Ok(None);
    }
    let mut nums = Vec::new();
    for part in text.split_whitespace() {
        let n = u64::from_str_radix(part, 16)
            .map_err(|_| format!("invalid literal for int() with base 16: {}", py_repr(part)))?;
        nums.push(Value::from(n));
    }
    decode_olt_id(Some(&Value::Array(nums)))
}

// ---- Test locations -----------------------------------------------------------------

/// One part of a row key: a whole number, or text (upper-cased).
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum KeyPart {
    Int(i64),
    Text(String),
}

impl fmt::Display for KeyPart {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            KeyPart::Int(n) => write!(f, "{n}"),
            KeyPart::Text(s) => write!(f, "{s}"),
        }
    }
}

pub type RowKey = [KeyPart; 3];

/// A key as Python shows a tuple: ('A', 12, 25).
pub fn key_repr(key: &RowKey) -> String {
    let parts: Vec<String> = key
        .iter()
        .map(|k| match k {
            KeyPart::Int(n) => n.to_string(),
            KeyPart::Text(s) => py_repr(s),
        })
        .collect();
    format!("({})", parts.join(", "))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Location {
    /// side: "A" (channel AA, P1) or "B" (channel BB, P2)
    Rack { data_hall: String, row: i64, rack: i64, side: String },
    /// channel "A" or "B"; index 1-3 from F1.01 / F1.02 / F1.03
    F1 { data_hall: String, row: i64, channel: String, index: u8 },
}

impl Location {
    pub fn tab(&self) -> String {
        match self {
            Location::Rack { data_hall, .. } => rack_tab(data_hall),
            Location::F1 { .. } => F1_TAB.to_string(),
        }
    }

    pub fn key(&self) -> RowKey {
        match self {
            Location::Rack { data_hall, row, rack, .. } => {
                [KeyPart::Text(data_hall.clone()), KeyPart::Int(*row), KeyPart::Int(*rack)]
            }
            Location::F1 { data_hall, row, channel, .. } => {
                [KeyPart::Text(data_hall.clone()), KeyPart::Int(*row), KeyPart::Text(channel.clone())]
            }
        }
    }

    pub fn columns(&self) -> Columns {
        match self {
            Location::Rack { side, .. } => rack_columns(side).expect("side is A or B"),
            Location::F1 { index, .. } => f1_columns(*index).expect("index is 1-3"),
        }
    }
}

/// Map a test location label to where it belongs, or None if unrecognized.
pub fn parse_location(label: &str) -> Option<Location> {
    let label = label.trim();
    if let Some(m) = RACK_LABEL.captures(label) {
        return Some(Location::Rack {
            data_hall: m[1].to_string(),
            row: m[2].parse().ok()?,
            rack: m[3].parse().ok()?,
            side: m[4].to_string(),
        });
    }
    if let Some(m) = F1_LABEL.captures(label) {
        return Some(Location::F1 {
            data_hall: m[1].to_string(),
            row: m[2].parse().ok()?,
            channel: m[4].to_string(),
            index: m[3].parse().ok()?,
        });
    }
    None
}

// ---- Test results -------------------------------------------------------------------

pub fn stale_test_time() -> Duration {
    Duration::days(5 * 365)
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct TestReading {
    pub label: String,
    /// ISO 8601 with offset, as reported by the meter.
    pub test_time: String,
    pub power_dbm: Option<f64>,
    pub olt_id: Option<OltId>,
    pub signal_status: Option<String>,
    pub loss_db: Option<f64>,
    pub tech_id: Option<String>,
    pub tech_name: Option<String>,
    pub serial_no: Option<String>,
    pub work_order_id: Option<String>,
    pub workflow_id: Option<i64>,
    /// File name or StrataSync ID, for traceability.
    pub source: Option<String>,
    /// When StrataSync received it, e.g. "2026-10-29T12:33:01Z".
    pub sync_time: Option<String>,
}

impl TestReading {
    pub fn location(&self) -> Option<Location> {
        parse_location(&self.label)
    }

    fn synced(&self) -> Option<Time> {
        self.sync_time.as_deref().and_then(timeutil::parse_aware)
    }

    /// True when the meter's test time can't be trusted and the sync time is used.
    ///
    /// Meters whose clock has been reset report test times years in the past
    /// (e.g. 2020). A test time more than five years before the sync time is
    /// treated as such; otherwise the test time is used, because it is when
    /// the light was actually measured (a meter can sync hours later).
    pub fn uses_sync_time(&self) -> bool {
        let Some(synced) = self.synced() else { return false };
        match timeutil::parse_lenient(&self.test_time) {
            None => true,
            Some(tested) => tested < synced - stale_test_time(),
        }
    }

    /// The test's time as written to the sheet and compared with it: the test
    /// time, or the sync time if the test time is unreliable (in the meter's
    /// time zone where known, so the column stays consistent).
    pub fn sheet_time(&self) -> String {
        if !self.uses_sync_time() {
            return self.test_time.clone();
        }
        let synced = self.synced().expect("uses_sync_time implies a sync time");
        match timeutil::parse_aware(&self.test_time) {
            Some(tested) => timeutil::iso(&synced.with_timezone(tested.offset())),
            None => timeutil::iso(&timeutil::to_local(&synced)),
        }
    }

    pub fn timestamp(&self) -> Result<Time, String> {
        timeutil::parse_aware(&self.sheet_time()).ok_or_else(|| {
            let who = if self.label.is_empty() { self.source.clone().unwrap_or_default() } else { self.label.clone() };
            format!("{who}: no usable test time")
        })
    }

    /// How the report shows the test time.
    pub fn shown_time(&self) -> String {
        if self.uses_sync_time() {
            let said = if self.test_time.is_empty() { "nothing" } else { &self.test_time };
            format!("{} (sync time; meter clock said {said})", self.sheet_time())
        } else {
            self.test_time.clone()
        }
    }
}

/// Extract TruePON readings from one test result document (.wfl.json).
///
/// The file doesn't record when StrataSync received it; pass the search
/// result's syncTime so an unreliable test time can be replaced.
pub fn readings_from_result(doc: &Value, source: Option<&str>, sync_time: Option<&str>) -> Result<Vec<TestReading>, String> {
    let asset = obj(doc.get("assetInfo"));
    let workflow = obj(doc.get("workflow"));
    let tech = obj(workflow.get("techInfo"));
    let name_parts: Vec<String> =
        [tech.get("firstName"), tech.get("lastName")].into_iter().filter_map(opt_text).collect();
    let tech_name = if name_parts.is_empty() { None } else { Some(name_parts.join(" ")) };
    let source_text = source.map(str::to_string).unwrap_or_else(|| "None".into());

    let mut readings = Vec::new();
    let tests = match doc.get("tests") {
        Some(Value::Array(t)) => t.as_slice(),
        _ => &[],
    };
    for test in tests {
        if test.get("type").and_then(Value::as_str) != Some("truepon") {
            continue;
        }
        let config = obj(test.get("configuration"));
        let signal = pick_service(config.get("signalSetup").and_then(Value::as_str), &source_text)?;
        let signal_config = signal.as_deref().map(|s| obj(config.get(s))).unwrap_or_else(|| obj(None));
        if let Some(fmt) = signal_config.get("oltIdFormat") {
            if fmt.as_str() != Some("hex") {
                return Err(format!("Unexpected oltIdFormat {} in {source_text}", json_repr(fmt)));
            }
        }
        let results = obj(test.get("results"));
        let data = signal.as_deref().map(|s| obj(obj(results.get("data")).get(s))).unwrap_or_else(|| obj(None));

        let default_loc = [Value::Object(Default::default())];
        let locations = match test.get("testLocations") {
            Some(Value::Array(l)) if !l.is_empty() => l.as_slice(),
            _ => &default_loc,
        };
        for loc in locations {
            let loc = obj(Some(loc));
            readings.push(TestReading {
                label: loc.get("label").and_then(Value::as_str).unwrap_or("").to_string(),
                test_time: results.get("testTime").and_then(Value::as_str).unwrap_or("").to_string(),
                power_dbm: opt_f64(data.get("powerDownstream"))?,
                olt_id: decode_olt_id(data.get("oltId"))?,
                signal_status: opt_text(data.get("powerDownstreamSignalStatus")),
                loss_db: opt_f64(data.get("loss"))?,
                tech_id: opt_text(tech.get("techId")).or_else(|| opt_text(asset.get("techId"))),
                tech_name: tech_name.clone(),
                serial_no: opt_text(asset.get("serialNo")),
                work_order_id: opt_text(workflow.get("workOrderId")),
                workflow_id: opt_i64(loc.get("workflowId").or_else(|| workflow.get("workflowId"))),
                source: source.map(str::to_string),
                sync_time: sync_time.map(str::to_string),
            });
        }
    }
    Ok(readings)
}

fn json_repr(v: &Value) -> String {
    match v {
        Value::String(s) => py_repr(s),
        Value::Null => "None".into(),
        other => other.to_string(),
    }
}

/// The signalSetup key holding the result to use (see PON_SERVICE).
fn pick_service(signal_setup: Option<&str>, source: &str) -> Result<Option<String>, String> {
    let setup = signal_setup.unwrap_or("");
    let services: Vec<&str> = setup.split(';').collect();
    if services.len() == 1 {
        return Ok(signal_setup.map(str::to_string));
    }
    if services.contains(&PON_SERVICE) {
        return Ok(Some(PON_SERVICE.to_string()));
    }
    Err(format!("Test set up for {}, which doesn't include {PON_SERVICE}, in {source}", py_repr(setup)))
}

/// True for a search result from a test of several PON types at once. Its
/// power, loss and OLT ID fields join one value per type with ";", so it is
/// read from the full file instead (see reading_from_search).
pub fn is_multi_service(record: &Value) -> bool {
    match record.get("ponServices") {
        None | Some(Value::Null) => false,
        Some(v) => json_text(Some(v)).contains(';'),
    }
}

/// Build a reading from a StrataSync search result, without downloading the file.
///
/// The search response carries the same values as the file: power and loss as
/// strings, the OLT ID already as hex text ("01 21 AA 09"), and the test time
/// in the meter's time zone. It does not include the signal status.
pub fn reading_from_search(record: &Value) -> Result<TestReading, String> {
    if is_multi_service(record) {
        return Err(format!(
            "{} tested {}; read the full file",
            json_text(record.get("name")),
            json_repr(record.get("ponServices").unwrap_or(&Value::Null))
        ));
    }
    let name_parts: Vec<String> =
        [record.get("userFirstName"), record.get("userLastName")].into_iter().filter_map(opt_text).collect();
    Ok(TestReading {
        label: opt_text(record.get("testLocationsLabel")).unwrap_or_default(),
        test_time: opt_text(record.get("assetLocalTestTime")).unwrap_or_default(),
        power_dbm: opt_f64(record.get("power"))?,
        olt_id: olt_from_hex_text(record.get("oltId"))?,
        signal_status: None,
        loss_db: opt_f64(record.get("loss"))?,
        tech_id: opt_text(record.get("userTechId")),
        tech_name: if name_parts.is_empty() { None } else { Some(name_parts.join(" ")) },
        serial_no: opt_text(record.get("serialNo")),
        work_order_id: opt_text(record.get("workOrderId")),
        workflow_id: opt_i64(record.get("workflowId")),
        source: Some(record_source(record)),
        sync_time: opt_text(record.get("syncTime")),
    })
}

/// "name (id 123)" for a search result.
pub fn record_source(record: &Value) -> String {
    format!("{} (id {})", json_text(record.get("name")), json_text(record.get("_id")))
}

// ---- Sheet indexing -------------------------------------------------------------------

fn norm_header(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn norm_key_part(value: Option<&str>) -> Option<KeyPart> {
    let text = value?.trim();
    if text.is_empty() {
        return None;
    }
    match text.parse::<f64>() {
        Ok(num) if num.is_finite() && num.fract() == 0.0 && num.abs() < 9.0e15 => Some(KeyPart::Int(num as i64)),
        Ok(_) => Some(KeyPart::Text(text.to_string())),
        Err(_) => Some(KeyPart::Text(text.to_uppercase())),
    }
}

/// Lookup of rows and columns in one tab. Rows/columns are 1-based like A1 notation.
#[derive(Debug, Clone)]
pub struct TabIndex {
    pub title: String,
    pub columns: HashMap<String, usize>,
    pub rows: HashMap<RowKey, usize>,
    /// Row keys in sheet order.
    pub row_order: Vec<(RowKey, usize)>,
    pub values: Vec<Vec<String>>,
}

impl TabIndex {
    pub fn build(title: &str, values: Vec<Vec<String>>, key_headers: &[&str; 3]) -> Result<TabIndex, String> {
        let Some(header) = values.first() else {
            return Err(format!("Tab {} is empty", py_repr(title)));
        };
        let mut columns = HashMap::new();
        for (i, h) in header.iter().enumerate() {
            let name = norm_header(h);
            if !name.is_empty() {
                columns.entry(name).or_insert(i + 1);
            }
        }
        let missing: Vec<String> = key_headers.iter().filter(|h| !columns.contains_key(**h)).map(|h| py_repr(h)).collect();
        if !missing.is_empty() {
            return Err(format!("Tab {} is missing key columns [{}]", py_repr(title), missing.join(", ")));
        }
        let key_cols: Vec<usize> = key_headers.iter().map(|h| columns[*h]).collect();
        let mut rows = HashMap::new();
        let mut row_order = Vec::new();
        for (i, row) in values.iter().enumerate().skip(1) {
            let r = i + 1;
            let parts: Vec<Option<KeyPart>> =
                key_cols.iter().map(|c| norm_key_part(row.get(c - 1).map(String::as_str))).collect();
            let [Some(a), Some(b), Some(c)] = [parts[0].clone(), parts[1].clone(), parts[2].clone()] else {
                continue;
            };
            let key: RowKey = [a, b, c];
            if let Some(prev) = rows.get(&key) {
                return Err(format!(
                    "Tab {} has duplicate key {} (rows {prev} and {r})",
                    py_repr(title),
                    key_repr(&key)
                ));
            }
            rows.insert(key.clone(), r);
            row_order.push((key, r));
        }
        Ok(TabIndex { title: title.to_string(), columns, rows, row_order, values })
    }

    pub fn column(&self, header: &str) -> Result<usize, String> {
        self.columns
            .get(&norm_header(header))
            .copied()
            .ok_or_else(|| format!("Tab {} has no column {}", py_repr(&self.title), py_repr(header)))
    }

    /// The displayed value of a cell ("" when outside the data).
    pub fn get(&self, row: usize, col: usize) -> &str {
        self.values.get(row - 1).and_then(|r| r.get(col - 1)).map(String::as_str).unwrap_or("")
    }
}

/// Index the audit tabs. `tabs` maps tab title -> all cell values (rows of lists).
pub fn build_indexes(tabs: &HashMap<String, Vec<Vec<String>>>) -> Result<HashMap<String, TabIndex>, String> {
    let mut indexes = HashMap::new();
    for title in audit_tabs() {
        let Some(values) = tabs.get(&title) else { continue };
        let keys = if title == F1_TAB { &F1_KEY_HEADERS } else { &RACK_KEY_HEADERS };
        indexes.insert(title.clone(), TabIndex::build(&title, values.clone(), keys)?);
    }
    Ok(indexes)
}

// ---- Problems with a reading ----------------------------------------------------------

/// Something a person should look at. `kind` is a short machine-readable tag and
/// `message` is the full explanation.
#[derive(Debug, Clone, PartialEq)]
pub struct Flag {
    pub kind: String,
    pub message: String,
    pub reading: Option<TestReading>,
}

impl Flag {
    pub fn new(kind: &str, message: impl Into<String>, reading: Option<&TestReading>) -> Flag {
        Flag { kind: kind.into(), message: message.into(), reading: reading.cloned() }
    }
}

fn reading_problems(reading: &TestReading, expected: &str) -> Vec<Flag> {
    let expected = expected.split_whitespace().collect::<Vec<_>>().join(" ").to_uppercase();
    let label = &reading.label;
    let mut flags = Vec::new();

    if reading.power_dbm.is_none() {
        let status = reading.signal_status.as_deref().unwrap_or("unknown");
        flags.push(Flag::new(
            "no_reading",
            format!("{label}: no power reading (signal status {})", py_repr(status)),
            Some(reading),
        ));
    }

    match reading.olt_id {
        None => flags.push(no_olt_flag(reading, &expected)),
        Some(olt) if olt.is_blank() => flags.push(no_olt_flag(reading, &expected)),
        Some(olt) if expected.is_empty() => flags.push(Flag::new(
            "no_expected_olt",
            format!("{label}: measured {} but the sheet has no expected OLT", olt.sheet_format()),
            Some(reading),
        )),
        Some(olt) if olt.sheet_format() != expected => flags.push(Flag::new(
            "olt_mismatch",
            format!("{label}: measured {} ({}), expected {expected}", olt.sheet_format(), olt.describe()),
            Some(reading),
        )),
        Some(olt) if !olt.is_well_formed() => {
            flags.push(Flag::new("odd_olt_id", format!("{label}: {}", olt.describe()), Some(reading)))
        }
        Some(_) => {}
    }
    flags
}

fn no_olt_flag(reading: &TestReading, expected: &str) -> Flag {
    let shown = if expected.is_empty() { "?" } else { expected };
    Flag::new("no_olt_id", format!("{}: meter reported no OLT ID (expected {shown})", reading.label), Some(reading))
}

// ---- Planning updates -------------------------------------------------------------------

/// A value written to one cell: numbers stay numbers, text stays text.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum CellValue {
    Number(f64),
    Text(String),
}

impl fmt::Display for CellValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CellValue::Number(n) => write!(f, "{}", py_float(*n)),
            CellValue::Text(s) => write!(f, "{s}"),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CellUpdate {
    pub tab: String,
    pub row: usize,
    pub col: usize,
    pub value: CellValue,
}

impl CellUpdate {
    pub fn a1(&self) -> String {
        format!("{}{}", col_letter(self.col), self.row)
    }
}

pub const FLAGGED_NOT_WRITTEN: &str = "flagged (not written)";

#[derive(Debug, Clone, Default)]
pub struct UpdatePlan {
    pub updates: Vec<CellUpdate>,
    pub flags: Vec<Flag>,
    pub written: Vec<TestReading>,
    pub skipped: Vec<(TestReading, String)>,
}

impl UpdatePlan {
    /// Each written reading with its (power, timestamp) cell updates.
    pub fn written_cells(&self) -> Vec<(&TestReading, &CellUpdate, &CellUpdate)> {
        self.written
            .iter()
            .zip(self.updates.chunks_exact(2))
            .map(|(r, pair)| (r, &pair[0], &pair[1]))
            .collect()
    }

    /// Skipped readings other than flagged ones (those are listed with the flags).
    pub fn skipped_not_flagged(&self) -> Vec<&(TestReading, String)> {
        self.skipped.iter().filter(|(_, why)| why != FLAGGED_NOT_WRITTEN).collect()
    }
}

fn is_blank(value: &str) -> bool {
    value.trim().is_empty()
}

/// Work out which cells to write for a batch of readings.
///
/// * When a location was tested more than once, the latest test wins.
/// * A reading is skipped when the sheet already has a result for that
///   location with the same or a later timestamp (unless overwrite_newer).
///   This includes a cable replacement's timestamp (see plan_cable_replacement).
/// * A reading with any problem (OLT mismatch, no OLT ID, no power reading,
///   ...) is not written at all, only flagged, so nothing questionable
///   reaches the sheet. Issue columns are never touched.
pub fn plan_updates(
    readings: impl IntoIterator<Item = TestReading>,
    indexes: &HashMap<String, TabIndex>,
    overwrite_newer: bool,
) -> Result<UpdatePlan, String> {
    let mut plan = UpdatePlan::default();

    // Latest reading per (tab, row key, columns), in first-seen order.
    type Slot = (String, RowKey, Columns);
    let mut order: Vec<Slot> = Vec::new();
    let mut latest: HashMap<Slot, (TestReading, Time)> = HashMap::new();
    for reading in readings {
        let Some(loc) = reading.location() else {
            plan.flags.push(Flag::new(
                "unknown_label",
                format!("Unrecognized test label {}", py_repr(&reading.label)),
                Some(&reading),
            ));
            plan.skipped.push((reading, "label not recognized".into()));
            continue;
        };
        let stamp = match reading.timestamp() {
            Ok(t) => t,
            Err(msg) => {
                plan.flags.push(Flag::new("no_test_time", msg, Some(&reading)));
                plan.skipped.push((reading, "no usable test time".into()));
                continue;
            }
        };
        let slot: Slot = (loc.tab(), loc.key(), loc.columns());
        match latest.get(&slot) {
            None => {
                order.push(slot.clone());
                latest.insert(slot, (reading, stamp));
            }
            Some((_, current)) if stamp > *current => {
                let (old, _) = latest.insert(slot, (reading, stamp)).expect("present");
                plan.skipped.push((old, "superseded by a later test in this batch".into()));
            }
            Some(_) => plan.skipped.push((reading, "superseded by a later test in this batch".into())),
        }
    }

    let mut chosen: Vec<(Slot, TestReading, Time)> = order
        .into_iter()
        .map(|slot| {
            let (reading, stamp) = latest.remove(&slot).expect("every slot has a reading");
            (slot, reading, stamp)
        })
        .collect();
    chosen.sort_by_key(|(_, _, stamp)| *stamp); // stable, like Python's sorted()

    for ((tab, key, cols), reading, stamp) in chosen {
        let Some(index) = indexes.get(&tab) else {
            plan.flags.push(Flag::new(
                "missing_tab",
                format!("{}: tab {} not found", reading.label, py_repr(&tab)),
                Some(&reading),
            ));
            plan.skipped.push((reading, "tab not available".into()));
            continue;
        };
        let Some(&row) = index.rows.get(&key) else {
            plan.flags.push(Flag::new(
                "no_row",
                format!("{}: no row for {} in {}", reading.label, key_repr(&key), py_repr(&tab)),
                Some(&reading),
            ));
            plan.skipped.push((reading, "no matching row".into()));
            continue;
        };

        let time_col = index.column(cols.timestamp)?;
        let power_col = index.column(cols.power)?;
        let existing = index.get(row, time_col);
        if let Some(existing_time) = timeutil::parse_lenient(existing) {
            if existing_time >= stamp && !overwrite_newer {
                let why = if is_blank(index.get(row, power_col)) {
                    format!("tested before cable replacement ({existing})")
                } else {
                    "sheet already has this or a newer result".to_string()
                };
                plan.skipped.push((reading, why));
                continue;
            }
        }

        let problems = reading_problems(&reading, index.get(row, index.column(cols.expected_olt)?));
        if !problems.is_empty() {
            plan.flags.extend(problems);
            plan.skipped.push((reading, FLAGGED_NOT_WRITTEN.into()));
            continue;
        }

        // Two updates per written reading, power then timestamp (see written_cells).
        let power = reading.power_dbm.expect("checked by reading_problems");
        plan.updates.push(CellUpdate { tab: tab.clone(), row, col: power_col, value: CellValue::Number(power) });
        plan.updates.push(CellUpdate { tab, row, col: time_col, value: CellValue::Text(reading.sheet_time()) });
        plan.written.push(reading);
    }
    Ok(plan)
}

// ---- Cable replacement -------------------------------------------------------------------
//
// All the racks on one row and channel of a data hall are fed by one multi-drop
// cable. When it is replaced, its results are cleared from the sheet: each rack's
// dBm is blanked and its timestamp set to the replacement time. That timestamp
// is the only record of the replacement, and plan_updates' "sheet already has
// a newer result" rule then ignores every test from before it, while the first
// test after it is written as normal. (A timestamp with a blank dBm can only
// come from a replacement, since flagged results aren't written.)
//
// The comparison is against the test time (see TestReading::sheet_time), so a
// test made on the old cable but synced after the replacement is still ignored.
// Only a test time that is clearly wrong (a reset meter clock) falls back to
// the sync time.

#[derive(Debug, Clone, PartialEq)]
pub struct RackClearing {
    pub rack: KeyPart,
    /// Sheet row.
    pub row: usize,
    pub old_power: String,
    pub old_timestamp: String,
    /// False when the rack was already tested after the replacement.
    pub cleared: bool,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct CableReplacementPlan {
    pub tab: String,
    pub data_hall: String,
    pub row: i64,
    pub channel: String,
    /// ISO 8601 with offset, as written to the sheet.
    pub replaced_at: String,
    pub racks: Vec<RackClearing>,
    pub updates: Vec<CellUpdate>,
}

impl CableReplacementPlan {
    pub fn cable(&self) -> String {
        format!("{SITE}.DH{} row {} channel {}", self.data_hall, self.row, self.channel)
    }

    pub fn cleared(&self) -> usize {
        self.racks.iter().filter(|r| r.cleared).count()
    }
}

/// Cell updates that clear one cable's results and record its replacement time.
///
/// `index` is the data hall's audit tab. A rack already tested after the
/// replacement time is left alone, so a result from the new cable isn't lost;
/// so is one with a later replacement already recorded, so that protection
/// isn't weakened.
pub fn plan_cable_replacement(
    index: &TabIndex,
    data_hall: &str,
    row: i64,
    channel: &str,
    replaced_at: Time,
) -> Result<CableReplacementPlan, String> {
    let cols = rack_columns(channel).ok_or_else(|| format!("Channel must be A or B, not {}", py_repr(channel)))?;
    let (power_col, time_col) = (index.column(cols.power)?, index.column(cols.timestamp)?);
    let stamp = timeutil::iso(&replaced_at);
    let mut plan = CableReplacementPlan {
        tab: index.title.clone(),
        data_hall: data_hall.into(),
        row,
        channel: channel.into(),
        replaced_at: stamp.clone(),
        racks: Vec::new(),
        updates: Vec::new(),
    };

    let mut racks: Vec<(KeyPart, usize)> = index
        .row_order
        .iter()
        .filter(|(key, _)| key[0] == KeyPart::Text(data_hall.to_string()) && key[1] == KeyPart::Int(row))
        .map(|(key, r)| (key[2].clone(), *r))
        .collect();
    racks.sort_by_key(|(_, r)| *r);
    if racks.is_empty() {
        return Err(format!("Tab {} has no racks on row {row}", py_repr(&index.title)));
    }
    for (rack, r) in racks {
        let old_power = index.get(r, power_col).to_string();
        let old_time = index.get(r, time_col).to_string();
        if let Some(existing) = timeutil::parse_lenient(&old_time) {
            if existing > replaced_at {
                let reason = if is_blank(&old_power) {
                    "a later replacement is already recorded"
                } else {
                    "already tested after the replacement"
                };
                plan.racks.push(RackClearing {
                    rack,
                    row: r,
                    old_power,
                    old_timestamp: old_time,
                    cleared: false,
                    reason: reason.into(),
                });
                continue;
            }
        }
        plan.racks.push(RackClearing { rack, row: r, old_power, old_timestamp: old_time, cleared: true, reason: String::new() });
        plan.updates.push(CellUpdate { tab: index.title.clone(), row: r, col: power_col, value: CellValue::Text(String::new()) });
        plan.updates.push(CellUpdate { tab: index.title.clone(), row: r, col: time_col, value: CellValue::Text(stamp.clone()) });
    }
    Ok(plan)
}

pub fn col_letter(mut col: usize) -> String {
    let mut letters = Vec::new();
    while col > 0 {
        let rem = (col - 1) % 26;
        letters.push((b'A' + rem as u8) as char);
        col = (col - 1) / 26;
    }
    letters.iter().rev().collect()
}

#[cfg(test)]
mod tests;
