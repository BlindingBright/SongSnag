# Frozen-build entry point (PyInstaller needs a script, not a module).
import sys

from songsnag.app import main

sys.exit(main())
