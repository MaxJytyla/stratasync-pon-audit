//! Errors that end a run, and how they're shown to the user.

use std::fmt;

#[derive(Debug, Clone)]
pub enum AppError {
    /// Missing or invalid settings (the message says what to fix).
    Config(String),
    /// Another update (in the window or an automatic one) is already running.
    RunInProgress(String),
    /// The StrataSync API returned an error or an unexpected response.
    StrataSync { message: String, status: Option<u16>, body: Option<String> },
    /// The spreadsheet couldn't be opened (the message says how to fix it).
    SheetAccess(String),
    /// Anything else (a sheets bridge or file error, an unexpected sheet layout, ...).
    Other(String),
}

pub type Result<T> = std::result::Result<T, AppError>;

impl AppError {
    pub fn config(msg: impl Into<String>) -> Self {
        AppError::Config(msg.into())
    }

    pub fn other(msg: impl Into<String>) -> Self {
        AppError::Other(msg.into())
    }

    pub fn stratasync(message: impl Into<String>, status: Option<u16>, body: Option<String>) -> Self {
        AppError::StrataSync { message: message.into(), status, body }
    }

    /// A readable message for a failed run.
    pub fn text(&self) -> String {
        match self {
            AppError::StrataSync { message, status, body } => {
                let mut text = message.clone();
                if matches!(status, Some(400 | 401)) && message.contains("Token") {
                    text.push_str(
                        "\n\nCheck the client ID and secret, and that the right region (USA or EU) is selected.",
                    );
                } else if let Some(body) = body.as_deref().filter(|b| !b.is_empty()) {
                    let short: String = body.chars().take(500).collect();
                    text.push_str(&format!("\n\n{short}"));
                }
                text
            }
            AppError::Config(m) | AppError::RunInProgress(m) | AppError::SheetAccess(m) | AppError::Other(m) => {
                m.clone()
            }
        }
    }
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.text())
    }
}

impl std::error::Error for AppError {}

impl From<std::io::Error> for AppError {
    fn from(e: std::io::Error) -> Self {
        AppError::Other(format!("OSError: {e}"))
    }
}

impl From<String> for AppError {
    /// Errors from the pure logic (an unexpected sheet layout and the like).
    fn from(e: String) -> Self {
        AppError::Other(e)
    }
}
