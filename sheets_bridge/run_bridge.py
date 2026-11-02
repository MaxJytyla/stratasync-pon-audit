"""Entry point for the PyInstaller build of the sheets bridge (and for running it from source)."""

import sys

from sheets_bridge.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
