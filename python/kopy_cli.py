"""Entry point for PyInstaller (and for running without installing).

PyInstaller runs its target as a top-level script, outside any package, so it
must not be kopy/__main__.py itself. Build with:

    pyinstaller --onefile --name kopy kopy_cli.py
"""

import sys

from kopy.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
