# StrataSync PON Audit

A desktop app for Windows and macOS. It pulls new VIAVI TruePON test results from StrataSync and writes them into the RMN2 PON Audit Google Sheet, then shows a report of what was written and what was flagged.

## Install

The built apps (Releases) aren't code-signed, and some computers block them. So the app is run from a copy of this repo with Python instead. It keeps itself up to date from the repo (see **Updating the app** below).

### Windows

1. Install **Python 3.13** from [python.org](https://www.python.org/downloads/windows/). On the installer's first screen, tick **Add python.exe to PATH**.
2. Install **Git** from [git-scm.com](https://git-scm.com/download/win) (the default options are fine).
3. Open **PowerShell** (Start menu → type "PowerShell") and run these one at a time. The app goes in `Documents\stratasync-pon-audit`.

   ```powershell
   cd $HOME\Documents
   git clone https://github.com/MaxJytyla/stratasync-pon-audit.git
   cd stratasync-pon-audit
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

4. To start the app, double-click **Start PON Audit.bat** in that folder. To put it on the desktop, right-click it → **Show more options → Send to → Desktop (create shortcut)**. (From PowerShell: `.venv\Scripts\pythonw app.py` in the folder.)

### macOS

1. Install **Python 3.13** from [python.org](https://www.python.org/downloads/macos/) (it includes Tk, which the window needs). With Homebrew Python instead, also run `brew install python-tk@3.13`.
2. Open **Terminal** (Applications → Utilities). If `git --version` asks to install the command line developer tools, click **Install** and wait for it to finish.
3. Run these one at a time. The app goes in `Documents/stratasync-pon-audit`.

   ```bash
   cd ~/Documents
   git clone https://github.com/MaxJytyla/stratasync-pon-audit.git
   cd stratasync-pon-audit
   python3.13 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

4. To start the app, double-click **Start PON Audit.command** in that folder (it opens Terminal briefly). To keep it in the Dock, drag it there. (From Terminal: `.venv/bin/python app.py` in the folder.)

### Moving from a downloaded build

Settings, the download position, the history and the Cable ID file are kept in the app's data folder (see **Where the app keeps its data**), not with the app, so the clone picks them all up. If automatic updates were on, open the clone's app and click **Turn on** (or **Update**) under **Automatic updates** once, so they run the clone instead of the old build. Then delete the old build.

### Updating the app

When the app opens, and every 4 hours while it's open, it checks this repo's `main` branch for new commits. If there are some, it lists them and asks whether to update. **Yes** downloads them (`git pull`), installs any new requirements, and restarts the app; settings are kept. **No** waits until the next time you open the app, or click **Check for app updates** (under **Automatic updates**). The window's title shows the version running (commit and date).

- It doesn't ask in the middle of an update from the window; it waits for it to finish. It doesn't update while an automatic update is running either ("Another update is running"); try again in a minute.
- Automatic updates run the clone's code, so they use the new version from their next run.
- It only updates a clean copy on `main`: if files in the folder were edited, or it's on another branch, it says so and leaves it alone. To update such a copy by hand, in the folder:

  ```bash
  git status                 # what was changed
  git checkout -- .          # throw those changes away (only if you don't need them)
  git pull --ff-only origin main
  ```

  then install the requirements again (step 3's last command) and restart the app.
- Updating needs internet access to github.com. If the check fails (offline, proxy), the Log tab says so and the app works as before.

The Releases builds still exist for computers that allow them (Windows: SmartScreen → **More info → Run anyway**; macOS: right-click → **Open**). They don't update themselves.

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
   - **Jobs**: the StrataSync jobs re-issued for locations that need a retest, if that's turned on (see below).

**Search from** is normally left blank. Each update then continues from where the last one finished. Enter a date (`2026-10-22`) or a date and time (`2026-10-22 08:00`) to look further back. Re-running over the same period is safe, because results older than what's already in the sheet are skipped.

### Automatic updates

Under **Automatic updates**, choose how often (e.g. every 1 hour or every 30 minutes) and click **Turn on**. The OS scheduler then does a **Run update** on that interval, starting one interval from now. The app doesn't need to be open. **Turn off** stops it, and **Update** changes the interval.

- Automatic updates use the settings saved in the window, so **Remember client secret and Google key on this computer** must be ticked. Changes you make in the window are used from the next update on.
- They run only while you're signed in to the computer and it's awake. A run missed while the computer was asleep happens when it wakes.
- Only one update runs at a time on a computer. If an automatic update starts while one from the window (or a cable replacement) is running, it's skipped and recorded as `skipped`. The next one catches up, because each update continues from the saved download position.
- Windows: it's a Task Scheduler task named **StrataSync PON Audit update**. macOS: it's a LaunchAgent, `~/Library/LaunchAgents/com.stratasync.ponaudit.update.plist`. macOS may show a "Background Items Added" notice. With a downloaded build on macOS, run it from Applications before turning this on, not from Downloads.
- The schedule starts the copy of the app that turned it on. Updating the clone (see **Updating the app**) doesn't change that. After you move the app to a different place (e.g. from a downloaded build to a clone), open it and click **Turn on** again. The app notes when the schedule points at a different copy.

### Update history

Every update that writes to the sheet is recorded, whether it was started from the window (`manual`) or automatically (`automatic`). The records are kept in the `history` folder in the app's data folder (**Open history folder**). These files are only ever added to:

| File | One row per |
|---|---|
| `runs.csv` | update: time, manual/automatic, outcome (`ok`, `partial` if some downloads failed, `failed`, `skipped`), search window, counts, error message |
| `flagged.csv` | flagged result (not written to the sheet): problem, location, details, test time, tech, serial, StrataSync source |
| `written.csv` | result written: location, tab, cells, dBm, test time, tech, serial, source |
| `jobs.csv` | job re-issued, or that failed to be, by an update or a cable replacement: name, status, workflowId, cohort, locations, why, error, Cable ID, and why the Cable ID couldn't be set (files from before Cable IDs get the new header as a row before the first longer row) |
| `cable_ids.csv` | change to the Cable ID file, by a cable replacement or **Change cable ID…**: cable, replacement time (blank for a Cable ID change), old and new Cable ID, which of the two it was (`replacement` or `correction`; older rows have none and are replacements), and what happened to the jobs |
| `cable_id_jobs.csv` | job whose Cable ID **Change cable ID…** set, or tried to: job, scheduled date, state, old and new Cable ID, outcome (`changed`, `failed`, `skipped`), error |
| `cable_replacements.csv` | **Replace cable…** attempt that was confirmed, including failed ones: time, user, computer, outcome (`ok`; `partial` if the sheet was written but something after it failed; `failed` if the sheet write didn't finish), sheet, tab, cable, replacement time, racks / cleared / left alone, cells written, old and new Cable ID, jobs created and failed, problems |
| `cable_replacement_racks.csv` | rack of a confirmed **Replace cable…**, saved just before the sheet is written: time (matches `run_at` in `cable_replacements.csv`), cable, replacement time, tab, rack, sheet row, `clear` or `leave`, why it was left, and its old dBm and timestamp. Rows with no matching `cable_replacements.csv` row mean the app stopped during that replacement. After a `failed` attempt some racks may have been cleared, so check the sheet |
| `connection_drops.csv` | dropped connection to Google Sheets, even one a retry recovered from: time, host, error, retries left |
| `connection_problems/` | one JSON report each time the app gives up reaching Google: where connections failed for Google and for other sites, who issued the certificates, which network adapter carries each host, proxy settings, running VPN and security software, recent Windows updates and installs. Contains no credentials or sheet data. It takes up to a minute after the failure |
| `automatic.log` | progress message from an automatic update (it starts over at 5 MB, keeping one old copy as `automatic.log.1`) |

The **History** tab lists the latest updates, and the **Automatic updates** section shows the outcome of the last automatic one. If a CSV is open in Excel when an update runs, Windows won't let the app add to it, so that update's rows go to a dated file next to it (e.g. `flagged-20261030-140000.csv`).

### Re-issuing jobs

Under **Re-issue jobs**, when **When an update finds locations that need a retest…** is ticked, every **Run update** (from the window or automatic) also creates StrataSync jobs for those locations. It and the low-light option are both ticked by default; untick them to turn re-issuing off.

- **When:** once the job a location was tested in is **COMPLETED**. StrataSync sets that by itself when every required location has a result; nobody has to approve it. Until then a flagged location is listed as "waiting for … to be completed" (the tech may still retest it). Every update checks the jobs completed or approved in the last 14 days, and the job of any flagged result however old, so it doesn't matter which update saw which flag, or whether the job's last test was flagged. A flagged test that isn't part of any job is re-issued straight away.
- **What counts as needing a retest:** a location of a completed job with no passing result in the sheet since the job's date (every test of it in the job was flagged: wrong OLT ID, no OLT ID, an unrecognized OLT ID or no power reading). A passing test earlier in the same job counts, so a stray later test of another port doesn't make a job. If the sheet has no result because no update has written it yet, the location's tests in the job are looked up in StrataSync first: if one passed, nothing is re-issued, and the summary says how many results the sheet is missing (run an update with an earlier **Search from** date to add them). Flags a retest can't fix (no row or expected OLT in the sheet, an unrecognized label, an unreadable file) aren't re-issued.
- **Low light (optional):** tick **Also when the light is weaker than…** to also re-issue locations whose result in the sheet is below the levels you enter. The defaults are -11.3 dBm for rack tests and +2.9 dBm for F1 tests. These results are still written to the sheet exactly as before. Re-issuing only adds a job.
- **One job per cohort:** all the locations of a completed job that need a retest go into one job per cohort, holding just those locations. The cohorts are C1 (racks 1–15), C2 (16–30), C3 (31–40) and F1 of one data hall row and channel. A whole cohort reading the wrong OLT ID gives one job of 15 racks (10 for C3), not 15 jobs.
- **Names:** the cohort's own job name plus `RT<n>`, e.g. `RMN2.DHA.12 AA C2 RT1` or `RMN2.DHA.01 BB F1 RT2`. n is one more than the highest RT number that cohort already has in StrataSync, counting hand-made ones (`… C2 RT` counts as RT1). Locations get the standard labels (`RMN2.DHA.12.05A`, `RMN2.DHA.01.F1.02B`).
- **Cable ID:** each new job gets its cable's Cable ID from the **Cable ID file** (see below). Every rack job of a data hall row and channel gets that cable's base serial, whatever the cohort; F1 jobs get `N/A (Trunk Fiber)`. A cable with no Cable ID in the file (or not in it) gets a blank one, and the Jobs tab and Log say so. The Cable ID is set right after the job is created. If that fails, the job is kept and the failure is shown on the Jobs tab, in the Log and in `jobs.csv`, so the Cable ID can be entered by hand.
- **Not assigned, in the right organization:** jobs are made from the **Job template** (`RMN-BB-PON-Test` by default), scheduled for today, and assigned to no technician. Each goes into the same StrataSync organization as the job it re-issues (BlackBox RMN for the RMN sites); a cable replacement's jobs go where that cable's earlier jobs are. They wait in the STARTED state until someone assigns them, and aren't sent to any meter before that. (Without an organization, StrataSync would put them in the account's top organization, BlackBox.)
- **No duplicates:** a location isn't re-issued again when another job holding it is already scheduled on or after the completed job's date (an earlier update re-issued it, or someone did by hand). The **Jobs** tab and the Log list these as "already in …". Re-running an update over the same period is still safe.
- **Two people at once:** flagged results aren't written to the sheet, so two people running updates can both find the same ones. Right before each job is created, the app searches StrataSync's jobs again and leaves out locations someone else has re-issued in the meantime, logging "already re-issued in …, probably by someone else". If both create the same job at the same moment, StrataSync refuses the second (same name and date), and that app checks again and creates only what's still missing.
- **Preview first:** **Preview (no changes)** lists the jobs it would create on the **Jobs** tab, without creating them.
- **If a job can't be created:** the sheet is still written. The download position isn't moved, so the next update tries that job again. Jobs that were created aren't repeated. The run shows as `partial` in the history.

#### The Cable ID file

A CSV with one row per multi-drop cable, chosen under **Re-issue jobs → Cable ID file** (leave the box blank for `cable_ids.csv` in the app's data folder). **Show** opens its folder. The line below the box says how many cables it has and how many have no Cable ID yet.

```
Cable,Cable ID
RMN2.DHA.01 AA,S7XXXXXXXX031
RMN2.DHB.12 BB,
```

- `Cable` is the data hall, row and channel (`RMN2.DHA.01 AA`). `Cable ID` is the cable's base serial. Cohort 2 spans both legs of a cable (`.100A` and `.200A`), so the base serial is used for every cohort, not a leg serial.
- A blank Cable ID is fine (a cable whose serial isn't known yet). Fill it in later; jobs created after that get it.
- If the file can't be read (a cable listed twice, a line that isn't a cable), updates still write the sheet but don't re-issue jobs until it's fixed, so no jobs are created with blank Cable IDs by mistake.
- A cable replacement writes the new cable's ID over the old one (see below).
- To enter a Cable ID that's missing, or fix a typo, use **Change cable ID…** (see below). It changes the file and the cable's jobs together. Editing the file by hand only affects jobs created after that.
- Each computer has its own file unless you point **Cable ID file** at a shared one (e.g. on a shared drive). A replacement recorded on one computer only updates the file that computer uses.

Re-issuing uses the same client ID and secret as the test results. They need access to the StrataSync TPA Jobs API, and the service user needs permission to create jobs.

### Changing a cable's ID (missing or mistyped)

**Change cable ID…** is for a Cable ID that was never entered, or was entered wrong. It's not for a replaced cable: that's a new cable, so use **Replace cable…**, which leaves the old cable's jobs with the old ID.

1. Choose the data hall, row and channel. The line beside them shows the cable's Cable ID in the Cable ID file now (or that it isn't in the file yet; it's then added).
2. Enter the **New cable ID** and click **Preview**. The cable's jobs in StrataSync are listed (every rack job of that row and channel, C1, C2 and C3 alike, including RT jobs; not F1, which is on trunk fiber), with their current Cable ID. Nothing is changed yet.
3. Tick the jobs to change (click a row), and click **Change cable ID**, then confirm.

Which jobs are ticked to begin with:

- **Ticked:** jobs with a blank Cable ID, or the one the file has now (the typo being fixed; capitals don't matter).
- **Not ticked, but you can tick them:** jobs with some other Cable ID (perhaps a different typo typed by hand, perhaps another cable's), and jobs scheduled before the cable's latest replacement. Those tested the old cable, so they keep its ID. The replacement time comes from this computer's history (`cable_ids.csv`) and from the sheet (a rack on the cable with a blank dBm and a timestamp), whichever is later. A replacement recorded on another computer, whose racks have all been retested since, isn't known here: check the jobs' dates against it yourself. Jobs scheduled on the replacement day itself count as new-cable jobs.
- **Can't be ticked:** jobs that already have the new ID, jobs that also hold locations on other cables (change those by hand), and approved jobs unless **Also change approved jobs** is ticked. Jobs at other sites (RMN1) are never listed.

Approved jobs are locked in StrataSync. With **Also change approved jobs** ticked, each one is unapproved, given the new ID and approved again, as `job_manager`'s step-by-step does (unapproving uses an undocumented StrataSync endpoint). It's approved again even if setting the ID failed. If approving it again fails, the message says so: approve it in StrataSync.

How it's done, and what's checked:

- Only the job's Cable ID is sent (the same change new jobs get), never its technician, which would reset the job. After each job, its state, technician, dates and results are compared with before; at the first one where anything else changed, it stops and says which. At the end, every changed job is looked up again to check its Cable ID.
- Everything is looked up again when you click **Change cable ID**. If the file's Cable ID for the cable changed since the preview, nothing is changed (preview again). A ticked job whose Cable ID changed since is skipped.
- The jobs are changed first, then the file, so jobs re-issued in the meantime don't get a mix. The file is changed even if some jobs failed, since the new ID is the right one. It runs as an update does: not at the same time as an update or cable replacement on this computer.
- The preview warns if the new ID doesn't look like the site's others (S7 + 8 characters + 031, or TS + 8 characters + 030), or if another cable in the file already has it.
- **To retry** jobs that failed or were skipped, do it again with the same ID. The file already has it, so only jobs change, and jobs still holding the ID it replaced (from this computer's history) are ticked.
- Each change goes into `cable_ids.csv` and each job into `cable_id_jobs.csv` in the history folder, and the Log lists every step.

### Replacing a multi-drop cable

All the racks on one row and channel of a data hall share one multi-drop cable (e.g. DHB row 5 channel A). When a cable is replaced, its old results no longer mean anything:

1. Click **Replace cable…** and choose the data hall, row and channel. Set **Replaced at** to when the new cable went in (it defaults to now). Enter the **New cable ID** (the new cable's base serial), or leave it blank if it isn't known yet.
2. Click **Preview**. A list shows every rack on that cable, with its current dBm and timestamp and what will happen to it. Nothing is changed yet.
3. Click **Replace cable** and confirm.

**Also create new jobs for the cable's rack cohorts** is ticked by default. It creates one job per rack cohort on that cable (C1, C2, C3), named and left unassigned as described under **Re-issuing jobs**, e.g. `RMN2.DHB.05 AA C1 RT1`, with the new cable ID. Each job covers every rack being cleared, including racks whose old result was in spec: that result measured the old cable. No F1 jobs are created, since F1 is on a separate cable. The preview lists the jobs. A rack is left out only if a job scheduled on or after the replacement day holds it and **hasn't tested it yet**, so recording the same replacement twice doesn't make a second set. A job that has already tested the rack (for example a retest job done that morning, before the swap) doesn't count, since that test may have been on the old cable. The jobs are created after the sheet is written. If one fails, the message says so. Click **Replace cable** again with the same time to retry.

The **Cable ID file** gets the new cable ID in place of the old one (a blank one if **New cable ID** was left blank), after the sheet is written. The preview and the confirmation show the old and new IDs. Every change is added to `cable_ids.csv` in the history folder. Jobs created before the replacement keep the old cable's ID in StrataSync.

For each rack, the app clears `dBm in Port 1` (channel A) or `dBm in Port 2` (channel B), and sets `Timestamp P1`/`P2` to the replacement time. That timestamp *is* the record of the replacement. Every update after that ignores tests on that cable from before it, over any time window, on any computer, and with the command-line script too. These show in **Skipped** as "tested before cable replacement". Re-issuing ignores them too: when a completed job's rack has no result in the sheet, the app looks up the rack's tests in StrataSync, and a test from before the replacement doesn't count as a pass, so the rack is re-issued (unless a new-cable job already holds it) instead of being reported as a result the sheet is missing.

The app only knows about a replacement recorded with **Replace cable…**. If a cable is swapped and not recorded, its old in-spec results stay in the sheet and nothing retests those racks. The first test after the replacement is written as normal. Racks already tested after the replacement time are left alone, and so are racks with a later replacement already recorded. Nothing else in the sheet is touched (OLT, Issue, Fix and the History tabs included). The old values can only be recovered from the sheet's version history.

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
- `cable_ids.csv` is the default Cable ID file (see **Re-issuing jobs**).

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
Start PON Audit.bat   Windows launcher for a clone (pythonw, no console)
Start PON Audit.command  macOS launcher for a clone
build.py              PyInstaller build
scripts/smoke_gui.py  opens the window and checks sizing and scrolling (run by the Build workflow)
viavi_results/
  gui.py              the window: settings form, run, report, CSV export
  updater.py          checks origin/main for new commits; pulls (fast-forward only) and restarts
  cable_id_change.py  Change cable ID: the Cable ID file and the cable's jobs
  sync.py             one sync run: search -> plan -> write, flags CSV, run lock
  scheduler.py        automatic updates: launchd / Task Scheduler, the headless run
  history.py          update history: runs.csv, flagged.csv, written.csv
  config.py           server URLs, settings validation, saved form, app data folder
  stratasync.py       StrataSyncClient: login, search, file download, job search/creation
  reissue.py          which locations need a retest, grouping into cohort RT jobs, creating them
  pon_audit.py        test result -> PON Audit cells, OLT ID decoding, checks
  sheets.py           SheetWriter: batch read/write across the sheet's tabs
  sync_state.py       remembers the last download position
```
