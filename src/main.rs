//! StrataSync PON Audit: a desktop app that pulls new VIAVI TruePON test results
//! from StrataSync and writes them into the RMN2 PON Audit Google Sheet.
//!
//! `--auto-update` runs one update with the saved settings and no window; the OS
//! scheduler starts the app that way for automatic updates.

// No console window on Windows, for the app or for automatic updates.
#![cfg_attr(not(test), windows_subsystem = "windows")]

mod config;
mod error;
mod gui;
mod history;
mod pon_audit;
mod scheduler;
mod sheets;
mod stratasync;
mod sync;
mod sync_state;
#[cfg(test)]
mod testutil;
mod timeutil;

fn main() {
    if std::env::args().skip(1).any(|a| a == scheduler::AUTO_UPDATE_ARG) {
        std::process::exit(scheduler::run_automatic());
    }
    if let Err(e) = gui::run() {
        eprintln!("{}: {e}", config::APP_NAME);
        std::process::exit(1);
    }
}
