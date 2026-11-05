@echo off
rem Starts the app from this folder (a git clone), with no console window.
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo The app is not set up yet. See "Install" in README.md.
  pause
  exit /b 1
)
start "" .venv\Scripts\pythonw.exe app.py
