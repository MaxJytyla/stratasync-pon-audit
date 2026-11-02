use super::*;
use serde_json::json;

fn s(v: &[&str]) -> Vec<String> {
    v.iter().map(|x| x.to_string()).collect()
}

/// A rack tab like the real one (note the double space in "OLT and Port  (B)").
fn rack_values() -> Vec<Vec<String>> {
    vec![
        s(&["DH", "Row", "Rack", "OLT and Port (A)", "dBm in Port 1", "Timestamp P1", "OLT and Port  (B)", "dBm in Port 2", "Timestamp P2", "Issue"]),
        s(&["A", "12", "25", "01 17 AA 01", "", "", "01 17 BB 01", "", "", ""]),
        s(&["A", "12", "26", "01 17 AA 02", "-19.1", "2026-10-20T08:00:00-04:00", "01 17 BB 02", "", "", ""]),
        s(&["A", "12", "27", "", "", "", "", "", "", ""]),
        s(&["A", "13", "1", "01 18 AA 01", "", "", "01 18 BB 01", "", "", ""]),
        s(&["", "", "", "", "", "", "", "", "", ""]),
    ]
}

fn f1_values() -> Vec<Vec<String>> {
    vec![
        s(&["DH", "Row", "Channel", "OLT-ID 1", "F1.01", "Timestamp F1.01", "OLT-ID 2", "F1.02", "Timestamp F1.02", "OLT-ID 3", "F1.03", "Timestamp F1.03"]),
        s(&["A", "1", "a", "01 21 AA 09", "", "", "01 21 AA 10", "", "", "01 21 AA 11", "", ""]),
    ]
}

fn indexes() -> HashMap<String, TabIndex> {
    let mut tabs = HashMap::new();
    tabs.insert(rack_tab("A"), rack_values());
    tabs.insert(F1_TAB.to_string(), f1_values());
    build_indexes(&tabs).unwrap()
}

fn reading(label: &str, time: &str, power: Option<f64>, olt: &str) -> TestReading {
    let olt_id = if olt.is_empty() {
        None
    } else {
        let parts: Vec<u8> = olt.split(' ').map(|p| u8::from_str_radix(p, 16).unwrap()).collect();
        Some(OltId { raw: [parts[0], parts[1], parts[2], parts[3]] })
    };
    TestReading {
        label: label.into(),
        test_time: time.into(),
        power_dbm: power,
        olt_id,
        tech_name: Some("Pat Tech".into()),
        serial_no: Some("SN1".into()),
        source: Some(format!("{label}.wfl.json")),
        ..Default::default()
    }
}

#[test]
fn olt_ids() {
    let olt = decode_olt_id(Some(&json!([1, 23, 170, 1]))).unwrap().unwrap();
    assert_eq!(olt.sheet_format(), "01 17 AA 01");
    assert_eq!((olt.uolt(), olt.channel(), olt.port()), (Some(17), Some('A'), Some(1)));
    assert!(olt.is_well_formed());
    assert_eq!(olt.describe(), "oltna17 channel A port 1");
    let blank = decode_olt_id(Some(&json!([0, 0, 0, 0]))).unwrap().unwrap();
    assert!(blank.is_blank());
    assert_eq!(blank.describe(), "no OLT ID");
    let odd = decode_olt_id(Some(&json!([1, 0x1A, 0xAB, 1]))).unwrap().unwrap();
    assert!(!odd.is_well_formed());
    assert_eq!(odd.describe(), "unrecognized OLT ID 01 1A AB 01");
    assert!(decode_olt_id(Some(&json!([1, 2, 3]))).is_err());
    assert!(decode_olt_id(Some(&json!([1, 2, 3, 256]))).is_err());
    assert_eq!(decode_olt_id(None).unwrap(), None);
}

#[test]
fn labels() {
    assert_eq!(
        parse_location("RMN2.DHA.12.25B"),
        Some(Location::Rack { data_hall: "A".into(), row: 12, rack: 25, side: "B".into() })
    );
    assert_eq!(
        parse_location(" RMN2.DHD.01.F1.02A "),
        Some(Location::F1 { data_hall: "D".into(), row: 1, channel: "A".into(), index: 2 })
    );
    assert_eq!(parse_location("RMN2.DHA.01.F1.2B").unwrap().columns().power, "F1.02");
    assert_eq!(parse_location("RMN2.DHE.12.25A"), None);
    assert_eq!(parse_location("RMN2.DHA.12.25C"), None);
    assert_eq!(parse_location("RMN2.DHA.01.F1.04A"), None);
    let loc = parse_location("RMN2.DHA.12.25A").unwrap();
    assert_eq!(loc.tab(), "RMN2.DHA PON Audit");
    assert_eq!(key_repr(&loc.key()), "('A', 12, 25)");
}

#[test]
fn reading_from_a_search_result() {
    let rec = json!({
        "_id": 4711, "name": "RMN2.DHA.12.25A.wfl.json", "type": "truepon",
        "testLocationsLabel": "RMN2.DHA.12.25A", "assetLocalTestTime": "2026-10-29T08:33:01-04:00",
        "power": "-18.52", "loss": "", "oltId": "01 17 AA 01", "userTechId": "T1",
        "userFirstName": "Pat", "userLastName": "Tech", "serialNo": "SN1", "workflowId": 99,
        "syncTime": "2026-10-29T12:40:00Z", "ponServices": "xgspon"
    });
    let r = reading_from_search(&rec).unwrap();
    assert_eq!(r.label, "RMN2.DHA.12.25A");
    assert_eq!(r.power_dbm, Some(-18.52));
    assert_eq!(r.loss_db, None);
    assert_eq!(r.olt_id.unwrap().sheet_format(), "01 17 AA 01");
    assert_eq!(r.tech_name.as_deref(), Some("Pat Tech"));
    assert_eq!(r.workflow_id, Some(99));
    assert_eq!(r.source.as_deref(), Some("RMN2.DHA.12.25A.wfl.json (id 4711)"));
    assert!(!r.uses_sync_time());
    assert_eq!(r.sheet_time(), "2026-10-29T08:33:01-04:00");

    let multi = json!({"name": "x", "ponServices": "gpon;xgspon", "power": "-20;-18"});
    assert!(is_multi_service(&multi));
    assert!(reading_from_search(&multi).unwrap_err().contains("read the full file"));
    assert!(reading_from_search(&json!({"oltId": "01 ZZ AA 01"})).is_err());
}

#[test]
fn reading_from_a_test_file_picks_xgspon() {
    let doc = json!({
        "assetInfo": {"serialNo": "SN9", "techId": "A1"},
        "workflow": {"workflowId": 5, "workOrderId": "WO1", "techInfo": {"firstName": "Pat", "lastName": null}},
        "tests": [
            {"type": "otdr"},
            {
                "type": "truepon",
                "configuration": {"signalSetup": "gpon;xgspon", "xgspon": {"oltIdFormat": "hex"}},
                "results": {"testTime": "2026-10-29T08:33:01-04:00", "data": {
                    "gpon": {"powerDownstream": -21.0, "oltId": [1, 23, 170, 9]},
                    "xgspon": {"powerDownstream": -18.0, "oltId": [1, 23, 170, 1], "powerDownstreamSignalStatus": "ok"}
                }},
                "testLocations": [{"label": "RMN2.DHA.12.25A", "workflowId": 7}, {"label": "RMN2.DHA.12.26A"}]
            }
        ]
    });
    let rs = readings_from_result(&doc, Some("file.json"), Some("2026-10-29T12:40:00Z")).unwrap();
    assert_eq!(rs.len(), 2);
    assert_eq!(rs[0].power_dbm, Some(-18.0));
    assert_eq!(rs[0].olt_id.unwrap().sheet_format(), "01 17 AA 01");
    assert_eq!(rs[0].workflow_id, Some(7));
    assert_eq!(rs[1].workflow_id, Some(5));
    assert_eq!(rs[0].tech_name.as_deref(), Some("Pat"));
    assert_eq!(rs[0].tech_id.as_deref(), Some("A1"));
    assert_eq!(rs[0].signal_status.as_deref(), Some("ok"));

    let gpon_only = json!({"tests": [{"type": "truepon", "configuration": {"signalSetup": "gpon;epon"}}]});
    assert!(readings_from_result(&gpon_only, Some("f"), None).unwrap_err().contains("doesn't include xgspon"));
    let dec = json!({"tests": [{"type": "truepon", "configuration": {"signalSetup": "xgspon", "xgspon": {"oltIdFormat": "dec"}}}]});
    assert!(readings_from_result(&dec, Some("f"), None).unwrap_err().contains("oltIdFormat"));
    // No test locations still gives one (unlabelled) reading.
    let bare = json!({"tests": [{"type": "truepon", "configuration": {"signalSetup": "xgspon"}}]});
    assert_eq!(readings_from_result(&bare, None, None).unwrap()[0].label, "");
}

#[test]
fn reset_meter_clock_uses_sync_time() {
    let mut r = reading("RMN2.DHA.12.25A", "2020-12-31T19:00:00-05:00", Some(-18.0), "01 17 AA 01");
    r.sync_time = Some("2026-10-29T12:40:00Z".into());
    assert!(r.uses_sync_time());
    assert_eq!(r.sheet_time(), "2026-10-29T07:40:00-05:00");
    assert!(r.shown_time().contains("meter clock said 2020-12-31T19:00:00-05:00"));
    // Hours late is normal: keep the test time.
    r.test_time = "2026-10-29T01:00:00-04:00".into();
    assert!(!r.uses_sync_time());
    // No test time at all: sync time, in local time.
    r.test_time = String::new();
    assert!(r.uses_sync_time());
    assert!(r.timestamp().is_ok());
}

#[test]
fn plan_writes_good_results_and_flags_bad_ones() {
    let idx = indexes();
    let readings = vec![
        reading("RMN2.DHA.12.25A", "2026-10-29T08:00:00-04:00", Some(-18.5), "01 17 AA 01"), // good
        reading("RMN2.DHA.12.25B", "2026-10-29T08:01:00-04:00", Some(-18.0), "01 17 BB 09"), // mismatch
        reading("RMN2.DHA.12.26A", "2026-10-19T08:00:00-04:00", Some(-17.0), "01 17 AA 02"), // older than sheet
        reading("RMN2.DHA.12.27A", "2026-10-29T08:02:00-04:00", Some(-17.0), "01 17 AA 03"), // no expected OLT
        reading("RMN2.DHA.13.1A", "2026-10-29T08:03:00-04:00", None, "00 00 00 00"),       // no reading, no OLT
        reading("RMN2.DHA.99.1A", "2026-10-29T08:04:00-04:00", Some(-17.0), "01 17 AA 01"), // no row
        reading("RMN2.DHB.1.1A", "2026-10-29T08:05:00-04:00", Some(-17.0), "01 17 AA 01"),  // tab missing
        reading("SOMETHING.ELSE", "2026-10-29T08:06:00-04:00", Some(-17.0), "01 17 AA 01"), // label
        reading("RMN2.DHA.01.F1.02A", "2026-10-29T08:07:00-04:00", Some(-15.25), "01 21 AA 10"), // F1 good
        reading("RMN2.DHA.13.1B", "", Some(-15.0), "01 18 BB 01"),                           // no time
    ];
    let plan = plan_updates(readings, &idx, false).unwrap();

    let written: Vec<&str> = plan.written.iter().map(|r| r.label.as_str()).collect();
    assert_eq!(written, ["RMN2.DHA.12.25A", "RMN2.DHA.01.F1.02A"]);
    let cells: Vec<(String, String)> = plan.updates.iter().map(|u| (u.a1(), u.value.to_string())).collect();
    assert_eq!(
        cells,
        [
            ("E2".to_string(), "-18.5".to_string()),
            ("F2".into(), "2026-10-29T08:00:00-04:00".into()),
            ("H2".into(), "-15.25".into()),
            ("I2".into(), "2026-10-29T08:07:00-04:00".into()),
        ]
    );
    assert_eq!(plan.updates[0].value, CellValue::Number(-18.5));
    assert_eq!(plan.written_cells().len(), 2);

    let kinds: Vec<&str> = plan.flags.iter().map(|f| f.kind.as_str()).collect();
    for k in ["olt_mismatch", "no_expected_olt", "no_reading", "no_olt_id", "no_row", "missing_tab", "unknown_label", "no_test_time"] {
        assert!(kinds.contains(&k), "missing flag {k}: {kinds:?}");
    }
    let mismatch = plan.flags.iter().find(|f| f.kind == "olt_mismatch").unwrap();
    assert_eq!(
        mismatch.message,
        "RMN2.DHA.12.25B: measured 01 17 BB 09 (oltna17 channel B port 9), expected 01 17 BB 01"
    );
    let no_row = plan.flags.iter().find(|f| f.kind == "no_row").unwrap();
    assert_eq!(no_row.message, "RMN2.DHA.99.1A: no row for ('A', 99, 1) in 'RMN2.DHA PON Audit'");
    let no_reading = plan.flags.iter().find(|f| f.kind == "no_reading").unwrap();
    assert_eq!(no_reading.message, "RMN2.DHA.13.1A: no power reading (signal status 'unknown')");

    let older = plan.skipped.iter().find(|(r, _)| r.label == "RMN2.DHA.12.26A").unwrap();
    assert_eq!(older.1, "sheet already has this or a newer result");
    assert_eq!(plan.skipped_not_flagged().len(), 5); // older, no row, tab, label, no time
}

#[test]
fn latest_test_of_a_location_wins() {
    let idx = indexes();
    let plan = plan_updates(
        vec![
            reading("RMN2.DHA.12.25A", "2026-10-29T09:00:00-04:00", Some(-18.0), "01 17 AA 01"),
            reading("RMN2.DHA.12.25A", "2026-10-29T12:30:00Z", Some(-17.0), "01 17 AA 01"), // 08:30 local: older
            reading("RMN2.DHA.12.25A", "2026-10-29T10:00:00-04:00", Some(-16.0), "01 17 AA 01"),
        ],
        &idx,
        false,
    )
    .unwrap();
    assert_eq!(plan.written.len(), 1);
    assert_eq!(plan.updates[0].value, CellValue::Number(-16.0));
    assert_eq!(plan.skipped.len(), 2);
    assert!(plan.skipped.iter().all(|(_, why)| why == "superseded by a later test in this batch"));
}

#[test]
fn tab_index_rules() {
    let mut bad = rack_values();
    bad.push(s(&["a", "12.0", "25"])); // same key as row 2 once normalized
    assert!(TabIndex::build("T", bad, &RACK_KEY_HEADERS).unwrap_err().contains("duplicate key ('A', 12, 25) (rows 2 and 7)"));
    assert!(TabIndex::build("T", vec![], &RACK_KEY_HEADERS).unwrap_err().contains("is empty"));
    assert!(TabIndex::build("T", vec![s(&["DH", "Row"])], &RACK_KEY_HEADERS).unwrap_err().contains("missing key columns ['Rack']"));
    let idx = TabIndex::build("T", rack_values(), &RACK_KEY_HEADERS).unwrap();
    assert_eq!(idx.column("OLT and Port (B)").unwrap(), 7);
    assert_eq!(idx.get(2, 50), "");
    assert_eq!(idx.rows.len(), 4);
    // Channel keys are upper-cased.
    let f1 = &indexes()[F1_TAB];
    assert!(f1.rows.contains_key(&[KeyPart::Text("A".into()), KeyPart::Int(1), KeyPart::Text("A".into())]));
}

#[test]
fn cable_replacement() {
    let idx = TabIndex::build(&rack_tab("A"), rack_values(), &RACK_KEY_HEADERS).unwrap();
    let at = timeutil::parse_aware("2026-10-25T12:00:00-04:00").unwrap();
    let plan = plan_cable_replacement(&idx, "A", 12, "A", at).unwrap();
    assert_eq!(plan.cable(), "RMN2.DHA row 12 channel A");
    assert_eq!(plan.racks.len(), 3);
    assert_eq!(plan.cleared(), 3);
    assert_eq!(plan.racks[1].old_power, "-19.1");
    let cells: Vec<String> = plan.updates.iter().map(|u| format!("{}={}", u.a1(), u.value)).collect();
    assert_eq!(cells[..2], ["E2=".to_string(), "F2=2026-10-25T12:00:00-04:00".to_string()]);

    // A rack tested after the replacement is left alone.
    let early = timeutil::parse_aware("2026-10-19T12:00:00-04:00").unwrap();
    let plan = plan_cable_replacement(&idx, "A", 12, "A", early).unwrap();
    assert_eq!(plan.cleared(), 2);
    assert_eq!(plan.racks[1].reason, "already tested after the replacement");

    assert!(plan_cable_replacement(&idx, "A", 40, "A", at).unwrap_err().contains("no racks on row 40"));
    assert!(plan_cable_replacement(&idx, "A", 12, "C", at).is_err());

    // After a replacement, older tests are skipped with the reason shown.
    let mut values = rack_values();
    values[1][4] = String::new();
    values[1][5] = "2026-10-25T12:00:00-04:00".into();
    let mut tabs = HashMap::new();
    tabs.insert(rack_tab("A"), values);
    let idx = build_indexes(&tabs).unwrap();
    let plan = plan_updates(
        vec![reading("RMN2.DHA.12.25A", "2026-10-25T11:00:00-04:00", Some(-18.0), "01 17 AA 01")],
        &idx,
        false,
    )
    .unwrap();
    assert_eq!(plan.skipped[0].1, "tested before cable replacement (2026-10-25T12:00:00-04:00)");
}

#[test]
fn helpers() {
    assert_eq!(col_letter(1), "A");
    assert_eq!(col_letter(26), "Z");
    assert_eq!(col_letter(27), "AA");
    assert_eq!(col_letter(703), "AAA");
    assert_eq!(py_float(-18.0), "-18.0");
    assert_eq!(py_float(-18.52), "-18.52");
    assert_eq!(py_repr("it's"), "\"it's\"");
    assert_eq!(serde_json::to_string(&CellValue::Number(-18.5)).unwrap(), "-18.5");
    assert_eq!(serde_json::to_string(&CellValue::Text("x".into())).unwrap(), "\"x\"");
}
