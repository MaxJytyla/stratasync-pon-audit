"""Build the standalone app with PyInstaller.

    python build.py

Windows: dist/StrataSync PON Audit.exe (one file).
macOS:   dist/StrataSync PON Audit.app, zipped to dist/StrataSync-PON-Audit-macOS.zip.
PyInstaller can't cross-compile, so build each platform on that platform
(GitHub Actions does both; see .github/workflows/build.yml).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

from viavi_results.config import APP_NAME

ROOT = Path(__file__).resolve().parent


def main() -> None:
    args = [
        str(ROOT / "app.py"),
        "--name", APP_NAME,
        "--windowed",
        "--noconfirm",
        "--clean",
        # openpyxl is only for local testing against an .xlsx copy; keep it out of the bundle.
        "--exclude-module", "openpyxl",
    ]
    if sys.platform == "darwin":
        # A .app bundle is the macOS "single package"; onefile .app bundles are deprecated.
        args += ["--onedir", "--osx-bundle-identifier", "com.stratasync.ponaudit"]
    else:
        args += ["--onefile"]
    PyInstaller.__main__.run(args)

    if sys.platform == "darwin":
        app = ROOT / "dist" / f"{APP_NAME}.app"
        archive = ROOT / "dist" / "StrataSync-PON-Audit-macOS.zip"
        archive.unlink(missing_ok=True)
        # ditto keeps the bundle's symlinks and metadata intact (zip -r doesn't).
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
        shutil.rmtree(ROOT / "dist" / APP_NAME, ignore_errors=True)
        print(f"Built {archive}")


if __name__ == "__main__":
    main()
