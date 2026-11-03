"""Settings for StrataSync and the Google Sheet, and where the app keeps its files.

Nothing secret lives in code. The app collects the values in its window and,
if the user asks it to, remembers them in a settings file in the per-user
application data folder (see `app_data_dir`).
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .reissue import DEFAULT_LOW_LIGHT_F1_DBM, DEFAULT_LOW_LIGHT_RACK_DBM, DEFAULT_TEMPLATE, ReissueSettings

APP_NAME = "StrataSync PON Audit"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


# StrataSync production servers, by region.
STRATASYNC_URLS = {
    "USA": "https://stratasync.viavisolutions.com",
    "EU": "https://eu.stratasync.viavisolutions.com",
}
DEFAULT_REGION = "USA"
DEFAULT_STRATASYNC_URL = STRATASYNC_URLS[DEFAULT_REGION]


@dataclass(frozen=True)
class Settings:
    service_account_info: Dict[str, Any]
    sheet_id: str


@dataclass(frozen=True)
class StrataSyncSettings:
    base_url: str
    access_token: Optional[str]
    client_id: Optional[str]
    client_secret: Optional[str]


def app_data_dir() -> Path:
    """The per-user folder for settings and the download position."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


# ---- validation ---------------------------------------------------------------

_SHEET_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def parse_sheet_id(value: str) -> str:
    """Accept a bare sheet ID or a full Google Sheets URL and return the ID."""
    value = value.strip()
    m = _SHEET_URL.search(value)
    if m:
        return m.group(1)
    if not value:
        raise ConfigError("Enter the Google Sheet ID.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ConfigError("The Google Sheet ID should look like the part of the sheet's URL "
                          "between /d/ and /edit.")
    return value


def parse_service_account(text: str) -> Dict[str, Any]:
    """Parse and sanity-check the contents of a service account key file."""
    if not text.strip():
        raise ConfigError("Load or paste the Google service account key (service_account.json).")
    try:
        info = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"The service account key is not valid JSON: {exc}") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ConfigError("That JSON is not a Google service account key "
                          '(it should contain "type": "service_account").')
    missing = [k for k in ("client_email", "private_key") if not info.get(k)]
    if missing:
        raise ConfigError(f"The service account key is missing {', '.join(missing)}.")
    return info


def make_settings(sheet_id: str, service_account_json: str) -> Settings:
    return Settings(
        service_account_info=parse_service_account(service_account_json),
        sheet_id=parse_sheet_id(sheet_id),
    )


def make_stratasync_settings(region: str, client_id: str, client_secret: str) -> StrataSyncSettings:
    if region not in STRATASYNC_URLS:
        raise ConfigError(f"Unknown StrataSync region {region!r}.")
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not (client_id and client_secret):
        raise ConfigError("Enter the StrataSync client ID and client secret "
                          "(VIAVI TAC provides these).")
    return StrataSyncSettings(
        base_url=STRATASYNC_URLS[region],
        access_token=None,
        client_id=client_id,
        client_secret=client_secret,
    )


def make_reissue_settings(enabled: bool, low_light: bool, rack_dbm: str, f1_dbm: str,
                          template: str) -> ReissueSettings:
    """Validate the job re-issue options. The thresholds are only checked when used."""
    template = template.strip()
    if enabled and not template:
        raise ConfigError("Enter the StrataSync job template that re-issued jobs are made from.")
    values = []
    for text, what in ((rack_dbm, "rack-level"), (f1_dbm, "F1")):
        try:
            values.append(float(str(text).strip()))
        except ValueError:
            if enabled and low_light:
                raise ConfigError(f"The {what} low-light level must be a number of dBm, like -11.3.") from None
            values.append(DEFAULT_LOW_LIGHT_RACK_DBM if what == "rack-level" else DEFAULT_LOW_LIGHT_F1_DBM)
    return ReissueSettings(enabled=enabled, low_light=low_light, rack_dbm=values[0], f1_dbm=values[1],
                           template=template)


# ---- remembered form values -------------------------------------------------------


# 2: re-issuing jobs (and on low light) became on by default.
SETTINGS_VERSION = 2


@dataclass
class SavedForm:
    """What the app's window remembers between runs."""

    region: str = DEFAULT_REGION
    client_id: str = ""
    client_secret: str = ""
    service_account_json: str = ""
    sheet_id: str = ""
    remember_secrets: bool = True
    auto_update_minutes: int = 60
    reissue_enabled: bool = True
    reissue_low_light: bool = True
    low_light_rack_dbm: str = str(DEFAULT_LOW_LIGHT_RACK_DBM)
    low_light_f1_dbm: str = str(DEFAULT_LOW_LIGHT_F1_DBM)
    job_template: str = DEFAULT_TEMPLATE
    # Bumped when a default changes in a way saved settings shouldn't keep; see load().
    version: int = SETTINGS_VERSION

    def reissue_settings(self) -> ReissueSettings:
        return make_reissue_settings(self.reissue_enabled, self.reissue_low_light, self.low_light_rack_dbm,
                                     self.low_light_f1_dbm, self.job_template)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SavedForm":
        path = path or app_data_dir() / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if known.get("version", 1) < 2:
            # Saved before re-issuing was on by default, when "off" was only the old default.
            known.pop("reissue_enabled", None)
            known.pop("reissue_low_light", None)
        known["version"] = SETTINGS_VERSION
        return cls(**known)

    def save(self, path: Optional[Path] = None) -> None:
        path = path or app_data_dir() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        if not self.remember_secrets:
            data["client_secret"] = ""
            data["service_account_json"] = ""
        tmp = path.with_suffix(".tmp")
        # Owner-only from the start, since it may hold the client secret and Google key.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        tmp.replace(path)
