"""Build the sheets bridge into one executable with PyInstaller.

    python sheets_bridge/build.py

Output: dist/sheets-bridge (macOS/Linux) or dist/sheets-bridge.exe (Windows).
The Rust app's build picks it up from there (see README "Development").
PyInstaller can't cross-compile, so build on each platform.
"""

from __future__ import annotations

from pathlib import Path

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main() -> None:
    PyInstaller.__main__.run([
        str(HERE / "run_bridge.py"),
        "--name", "sheets-bridge",
        "--onefile",
        # A console program: the app starts it hidden (no window) and talks over stdin/stdout.
        "--console",
        "--noconfirm",
        "--clean",
        "--paths", str(ROOT),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "sheets-bridge"),
        "--specpath", str(ROOT / "build"),
        "--hidden-import", "sheets_bridge.sheets",
        "--exclude-module", "tkinter",
    ])


if __name__ == "__main__":
    main()
