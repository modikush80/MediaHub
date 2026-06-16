"""App identity, paths, and shared constants."""
import os
import threading
from pathlib import Path

APP_NAME = "MediaHub"
HOST = "127.0.0.1"
PORT = int(os.environ.get("MEDIAHUB_PORT", "8765"))
# Where external drives mount (overridable for testing).
VOLUMES_DIR = Path(os.environ.get("MEDIAHUB_VOLUMES", "/Volumes"))

def data_dir() -> Path:
    """Writable location for runtime state (settings, job, overrides).
    Lives outside the app bundle so a read-only/relocated .app still works."""
    env = os.environ.get("MEDIAHUB_DATA")
    d = (Path(env).expanduser() if env
         else Path.home() / "Library" / "Application Support" / APP_NAME)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path(__file__).resolve().parent
    return d


DATA_DIR = data_dir()

GB = 1073741824.0

# Writable subdirectories (all outside the app bundle).
LOGS_DIR = DATA_DIR / "logs"
MANIFESTS_DIR = DATA_DIR / "manifests"
CACHE_DIR = DATA_DIR / "cache"
for _d in (LOGS_DIR, MANIFESTS_DIR, CACHE_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Data epoch: bumped whenever the underlying inventory or trip overrides change
# (ingest, override edits). Derived/aggregated queries memoize against it so
# repeated reads are instant without ever serving stale results.
# ---------------------------------------------------------------------------
_EPOCH = 0
_EPOCH_LOCK = threading.Lock()


def data_epoch() -> int:
    return _EPOCH


def bump_epoch() -> None:
    global _EPOCH
    with _EPOCH_LOCK:
        _EPOCH += 1
