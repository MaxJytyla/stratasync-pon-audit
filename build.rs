//! Release builds can embed the sheets bridge in the app, so Windows gets one
//! .exe: set SHEETS_BRIDGE_EMBED to the built bridge (dist/sheets-bridge.exe).

use std::env;
use std::path::PathBuf;

fn main() {
    println!("cargo::rustc-check-cfg=cfg(embedded_bridge)");
    println!("cargo:rerun-if-env-changed=SHEETS_BRIDGE_EMBED");
    let Some(src) = env::var_os("SHEETS_BRIDGE_EMBED").filter(|s| !s.is_empty()) else { return };
    let src = PathBuf::from(src);
    println!("cargo:rerun-if-changed={}", src.display());
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("sheets-bridge.bin");
    std::fs::copy(&src, &out).unwrap_or_else(|e| panic!("SHEETS_BRIDGE_EMBED={}: {e}", src.display()));
    println!("cargo:rustc-cfg=embedded_bridge");
}
