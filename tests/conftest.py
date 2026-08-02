"""Pytest config — isolate the test run to a throwaway data dir so it never
touches ~/Library/Application Support/MediaHub or the real inventory."""
import os
import sys
import tempfile
from pathlib import Path

# Must be set BEFORE any `mediahub` import (config reads these at import time).
os.environ.setdefault("MEDIAHUB_DATA", tempfile.mkdtemp(prefix="mh_tests_"))
os.environ.setdefault("MEDIAHUB_DB", os.path.join(os.environ["MEDIAHUB_DATA"], "inventory.sqlite3"))

# Make the repo root importable (so `import mediahub` works from anywhere).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
