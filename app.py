"""Entry point for the StrataSync PON Audit desktop app (and the PyInstaller build).

`--auto-update` runs one update with the saved settings and no window; the OS
scheduler starts the app that way for automatic updates.
"""

import sys

if __name__ == "__main__":
    if "--auto-update" in sys.argv[1:]:
        from viavi_results.scheduler import run_automatic
        sys.exit(run_automatic())
    from viavi_results.gui import main
    main()
