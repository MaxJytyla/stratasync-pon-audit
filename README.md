# StrataSync PON Audit

A desktop app for Windows and macOS. It pulls new VIAVI TruePON test results from StrataSync and writes them into the RMN2 PON Audit Google Sheet, then shows a report of what was written and what was flagged.

## Download

Get the latest build from this repo's **Releases** page (or from the artifacts of the latest **Build** workflow run):

| Platform | File | How to open it |
|---|---|---|
| Windows | `StrataSync-PON-Audit-Windows.exe` | Double-click it. If SmartScreen warns about an unrecognized app, click **More info → Run anyway**. |
| macOS (Apple silicon) | `StrataSync-PON-Audit-macOS.zip` | Unzip it and move **StrataSync PON Audit.app** to Applications. The first time, right-click the app and choose **Open**, then confirm. If macOS says the app is damaged, run `xattr -dr com.apple.quarantine "/Applications/StrataSync PON Audit.app"` once. |

The builds aren't code-signed, which is why the first launch needs those extra steps.

## Using it

1. **StrataSync**: pick the **USA** or **EU** server, then enter the client ID and client secret. VIAVI TAC provides these. The API also needs a StrataSync service user with an API role that can see the instruments (TPA Test Results JSON API Developer Guide, pp. 14–18).
2. **Google Sheet**: enter the sheet ID. You can paste the whole sheet URL and the app takes the ID from it. Then click **Load file…** to choose `service_account.json`, or paste the key's contents into the box. The app shows the service account's email address. Share the sheet with that address as an **Editor**.
3. Click **Preview (no changes)** to see what would be written. Click **Run update** to write it.
4. The **Report** has four tabs:
   - **Written**: each result with its location, tab, cells, dBm, test time, tech and meter serial.
   - **Flagged**: results with a problem. These are *not* written to the sheet. **Export flagged to CSV…** saves them.
   - **Skipped**: results that were superseded by a later test of the same location, or that are already in the sheet.
   - **Log**: progress messages and errors.
   - **History**: every update that wrote to the sheet, manual or automatic (see below).

**Search from** is normally left blank. Each update then continues from where the last one finished. Enter a date (`2026-10-22`) or a date and time (`2026-10-22 08:00`) to look further back. Re-running over the same period is safe, because results older than what's already in the sheet are skipped.

### Automatic updates

Under **Automatic updates**, choose how often (e.g. every 1 hour or every 30 minutes) and click **Turn on**. The OS scheduler then does a **Run update** on that interval, starting one interval from now. The app doesn't need to be open. **Turn off** stops it, and **Update** changes the interval.

- Automatic updates use the settings saved in the window, so **Remember client secret and Google key on this computer** must be ticked. Changes you make in the window are used from the next update on.
- They run only while you're signed in to the computer and it's awake. A run missed while the computer was asleep happens when it wakes.
- Only one update runs at a time on a computer. If an automatic update starts while one from the window (or a cable replacement) is running, it's skipped and recorded as `skipped`. The next one catches up, because each update continues from the saved download position.
- Windows: it's a Task Scheduler task named **StrataSync PON Audit update**. macOS: it's a LaunchAgent, `~/Library/LaunchAgents/com.stratasync.ponaudit.update.plist`. macOS may show a "Background Items Added" notice. Run the app from Applications before turning this on, not from Downloads.
- The schedule starts the copy of the app that turned it on. After you download a new version to a different place, open it and click **Turn on** again. The app notes when the schedule points at a different copy.

### Update history

Every update that writes to the sheet is recorded, whether it was started from the window (`manual`) or automatically (`automatic`). The records are kept in the `history` folder in the app's data folder (**Open history folder**). These files are only ever added to:

| File | One row per |
|---|---|
| `runs.csv` | update: time, manual/automatic, outcome (`ok`, `partial` if some downloads failed, `failed`, `skipped`), search window, counts, error message |
| `flagged.csv` | flagged result (not written to the sheet): problem, location, details, test time, tech, serial, StrataSync source |
| `written.csv` | result written: location, tab, cells, dBm, test time, tech, serial, source |
| `automatic.log` | progress message from an automatic update (it starts over at 5 MB, keeping one old copy as `automatic.log.1`) |

The **History** tab lists the latest updates, and the **Automatic updates** section shows the outcome of the last automatic one. If a CSV is open in Excel when an update runs, Windows won't let the app add to it, so that update's rows go to a dated file next to it (e.g. `flagged-20261030-140000.csv`).

### Replacing a multi-drop cable

All the racks on one row and channel of a data hall share one multi-drop cable (e.g. DHB row 5 channel A). When a cable is replaced, its old results no longer mean anything:

1. Click **Replace cable…** and choose the data hall, row and channel. Set **Replaced at** to when the new cable went in (it defaults to now).
2. Click **Preview**. A list shows every rack on that cable, with its current dBm and timestamp and what will happen to it. Nothing is changed yet.
3. Click **Replace cable** and confirm.

For each rack, the app clears `dBm in Port 1` (channel A) or `dBm in Port 2` (channel B), and sets `Timestamp P1`/`P2` to the replacement time. That timestamp *is* the record of the replacement. Every update after that ignores tests on that cable from before it, over any time window, on any computer, and with the command-line script too. These show in **Skipped** as "tested before cable replacement". The first test after the replacement is written as normal. Racks already tested after the replacement time are left alone, and so are racks with a later replacement already recorded. Nothing else in the sheet is touched (OLT, Issue, Fix and the History tabs included). The old values can only be recovered from the sheet's version history.

F1 cables are separate and aren't covered by this.

### Test times and reset meter clocks

A result's time is the meter's **test time**, which is when the light was measured. A meter can sync hours later, and a test made on an old cable must still count as before its replacement. Some meters' clocks get reset (e.g. to 2020-12-31). If a test time is more than five years before the result's **sync time** (when StrataSync received it), the sync time is used instead. That applies when it's compared, and when it's written to the Timestamp column. Such results are marked "(sync time; meter clock said …)" in the report.

### Where the app keeps its data

| | Folder |
|---|---|
| Windows | `%APPDATA%\StrataSync PON Audit\` |
| macOS | `~/Library/Application Support/StrataSync PON Audit/` |

- `settings.json` holds the form values. The client secret and Google key are saved there only while **Remember client secret and Google key on this computer** is ticked. The file is readable only by your user account.
- `state/` holds the download position. There is one position for each combination of server, client ID and sheet.
- `history/` holds the update history (see above).

VIAVI typically allows only one valid token per client ID, so avoid running updates on two computers with the same client ID at the same time.

### Google credentials (one-time)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project (or use an existing one) and enable the **Google Sheets API**.
2. Under **APIs & Services → Credentials**, create a **Service account**. Then go to **Keys → Add key → JSON** and download the key. This is the `service_account.json` the app asks for.
3. Share the sheet with the service account's email as an **Editor**.

The sheet must be a native Google Sheet. An `.xlsx` file uploaded to Drive won't work. If the sheet's title has an `.XLSX` badge, use **File → Save as Google Sheets** and use the new file's ID.

## What gets written

`viavi_results/pon_audit.py` maps each TruePON result to the sheet. It only writes to five tabs:

| Test label | Tab | Row lookup | Columns written |
|---|---|---|---|
| `RMN2.DHA.12.25A` / `…25B` | `RMN2.DHA PON Audit` (DHA–DHD) | DH, Row, Rack | A (channel AA): `dBm in Port 1`, `Timestamp P1`; B (channel BB): `dBm in Port 2`, `Timestamp P2` |
| `RMN2.DHA.01.F1.02A` | `RMN2 F1 & F2` | DH, Row, Channel | `F1.02` + `Timestamp F1.02` (likewise for .01 and .03) |

- The value written is the downstream power in dBm. The sheet's own formulas grade it.
- The OLT columns hold the *expected* port and are never overwritten. The measured OLT ID is compared against them.
- A result with any problem is flagged and not written. Problems are an OLT mismatch, no OLT ID, an unrecognized OLT ID, no power reading, no expected OLT in the sheet, an unrecognized label, no matching row, no usable test time, or a test file that can't be read. Issue columns are never touched.
- A meter set up to test GPON and XGS-PON at once reports both. Only the XGS-PON result is used.
- If a location was tested more than once, the latest test wins.
- If some full-file downloads fail, the results that did download are still written, but the download position doesn't move, so the next update retries.

## Development

Requires Python 3.10+ with Tk. On macOS with Homebrew Python, run `brew install python-tk@3.13`. The python.org installers include Tk.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-build.txt     # Windows: .venv\Scripts\pip ...
.venv/bin/python app.py                             # run from source
.venv/bin/python build.py                           # build the app into dist/
```

PyInstaller can't cross-compile, so `build.py` builds for the platform it runs on. The **Build** workflow (`.github/workflows/build.yml`) builds both platforms on every push. Pushing a tag such as `v1.0.0` publishes a release with both files:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

```
app.py                entry point (--auto-update: one update with the saved settings, no window)
build.py              PyInstaller build
viavi_results/
  gui.py              the window: settings form, run, report, CSV export
  sync.py             one sync run: search -> plan -> write, flags CSV, run lock
  scheduler.py        automatic updates: launchd / Task Scheduler, the headless run
  history.py          update history: runs.csv, flagged.csv, written.csv
  config.py           server URLs, settings validation, saved form, app data folder
  stratasync.py       StrataSyncClient: login, search, file download
  pon_audit.py        test result -> PON Audit cells, OLT ID decoding, checks
  sheets.py           SheetWriter: batch read/write across the sheet's tabs
  sync_state.py       remembers the last download position
```
