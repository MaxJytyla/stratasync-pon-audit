#!/bin/sh
# Starts the app from this folder (a git clone). Double-click it in Finder.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo 'The app is not set up yet. See "Install" in README.md.'
  exit 1
fi
nohup .venv/bin/python app.py >/dev/null 2>&1 &
# Close this Terminal window once the app has started.
osascript -e 'tell application "Terminal" to close (every window whose name contains "Start PON Audit")' >/dev/null 2>&1 &
