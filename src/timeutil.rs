//! ISO 8601 timestamps, parsed the way Python's `datetime.fromisoformat` does
//! for the forms StrataSync, the meters and the sheet use.

use chrono::{DateTime, FixedOffset, Local, NaiveDate, NaiveDateTime, NaiveTime, SecondsFormat, TimeZone, Utc};

/// A moment with a UTC offset. Comparisons are by instant.
pub type Time = DateTime<FixedOffset>;

/// A parsed timestamp: with an offset ("aware"), or without one ("naive").
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Parsed {
    Aware(Time),
    Naive(NaiveDateTime),
}

/// Parse "2026-10-29T08:33:01-04:00", "2026-10-29 08:33", "…Z", "2026-10-29", …
pub fn parse_iso(text: &str) -> Option<Parsed> {
    let text = text.trim();
    if text.len() < 10 || !text.is_char_boundary(10) {
        return None;
    }
    if text.len() == 10 {
        let date = NaiveDate::parse_from_str(text, "%Y-%m-%d").ok()?;
        return Some(Parsed::Naive(date.and_time(NaiveTime::MIN)));
    }
    let (date, rest) = text.split_at(10);
    let sep = rest.chars().next()?;
    if sep != 'T' && sep != ' ' {
        return None;
    }
    let normalized = format!("{date}T{}", &rest[1..]);
    let (clock, offset) = split_offset(&normalized[11..]);
    let naive_text = format!("{date}T{clock}");
    let naive = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M"]
        .iter()
        .find_map(|f| NaiveDateTime::parse_from_str(&naive_text, f).ok())?;
    match offset {
        None => Some(Parsed::Naive(naive)),
        Some(off) => {
            let off = parse_offset(off)?;
            off.from_local_datetime(&naive).single().map(Parsed::Aware)
        }
    }
}

fn split_offset(clock: &str) -> (&str, Option<&str>) {
    if let Some(stripped) = clock.strip_suffix('Z') {
        return (stripped, Some("Z"));
    }
    match clock.rfind(['+', '-']) {
        Some(i) => (&clock[..i], Some(&clock[i..])),
        None => (clock, None),
    }
}

fn parse_offset(text: &str) -> Option<FixedOffset> {
    if text == "Z" {
        return FixedOffset::east_opt(0);
    }
    let sign = if text.starts_with('-') { -1 } else { 1 };
    let digits: String = text[1..].chars().filter(|c| *c != ':').collect();
    if !(digits.len() == 4 || digits.len() == 6) || !digits.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let h: i32 = digits[0..2].parse().ok()?;
    let m: i32 = digits[2..4].parse().ok()?;
    let s: i32 = if digits.len() == 6 { digits[4..6].parse().ok()? } else { 0 };
    FixedOffset::east_opt(sign * (h * 3600 + m * 60 + s))
}

/// An aware timestamp, or None (naive or unparseable).
pub fn parse_aware(text: &str) -> Option<Time> {
    match parse_iso(text)? {
        Parsed::Aware(t) => Some(t),
        Parsed::Naive(_) => None,
    }
}

/// Any timestamp; one without an offset is taken to be in this computer's time zone.
pub fn parse_lenient(text: &str) -> Option<Time> {
    match parse_iso(text)? {
        Parsed::Aware(t) => Some(t),
        Parsed::Naive(n) => local_from_naive(n),
    }
}

pub fn local_from_naive(n: NaiveDateTime) -> Option<Time> {
    Local.from_local_datetime(&n).earliest().map(|t| t.fixed_offset())
}

/// "2026-10-29T08:33:01-04:00" (Python's `isoformat(timespec="seconds")`).
pub fn iso(t: &Time) -> String {
    t.to_rfc3339_opts(SecondsFormat::Secs, false)
}

/// "2026-10-29T12:33:01Z", the form StrataSync uses (and the download position file).
pub fn iso_utc_z(t: &Time) -> String {
    t.with_timezone(&Utc).to_rfc3339_opts(SecondsFormat::Secs, true)
}

pub fn now_local() -> Time {
    Local::now().fixed_offset()
}

pub fn now_utc() -> Time {
    Utc::now().fixed_offset()
}

pub fn to_local(t: &Time) -> Time {
    t.with_timezone(&Local).fixed_offset()
}

/// "2026-10-29 08:33:01" in this computer's time zone.
pub fn fmt_local(t: &Time) -> String {
    to_local(t).format("%Y-%m-%d %H:%M:%S").to_string()
}

/// The local ISO form of an ISO string, for display ("-" kept as-is if unparseable).
pub fn fmt_iso_local(text: &str) -> String {
    match parse_lenient(text) {
        Some(t) => fmt_local(&t),
        None => text.to_string(),
    }
}

/// Midnight today in this computer's time zone.
pub fn local_midnight_today() -> Time {
    let today = Local::now().date_naive();
    local_from_naive(today.and_time(NaiveTime::MIN)).unwrap_or_else(now_local)
}

/// "+HH:MM" offset of this computer's time zone right now.
pub fn local_offset_text() -> String {
    Local::now().format("%:z").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_python_isoformat_forms() {
        let t = parse_aware("2026-10-29T08:33:01-04:00").unwrap();
        assert_eq!(iso(&t), "2026-10-29T08:33:01-04:00");
        assert_eq!(iso_utc_z(&t), "2026-10-29T12:33:01Z");
        let z = parse_aware("2026-10-29T12:33:01Z").unwrap();
        assert_eq!(z, t);
        assert_eq!(iso(&z), "2026-10-29T12:33:01+00:00");
        assert!(parse_aware("2026-10-29 12:33:01.250+0000").is_some());
        assert!(parse_aware("2026-10-29T12:33").is_none());
        assert!(matches!(parse_iso("2026-10-29T12:33"), Some(Parsed::Naive(_))));
        assert!(matches!(parse_iso("2026-10-29"), Some(Parsed::Naive(_))));
        assert!(parse_iso("").is_none());
        assert!(parse_iso("yesterday").is_none());
        assert!(parse_iso("2026-10-29X12:00").is_none());
        assert!(parse_iso("2026-13-29T12:00:00Z").is_none());
    }

    #[test]
    fn negative_offsets_and_dates_with_dashes() {
        // The '-' in the date must not be taken for an offset.
        let t = parse_aware("2020-12-31T23:59:59-08:00").unwrap();
        assert_eq!(iso_utc_z(&t), "2021-01-01T07:59:59Z");
    }
}
