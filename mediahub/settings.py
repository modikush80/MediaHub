"""Destination settings + trip-name overrides (persisted JSON)."""
import json
import shutil
from pathlib import Path

from .config import DATA_DIR, GB, bump_epoch


MAP_PATH = DATA_DIR / "trip_overrides.json"


def load_overrides() -> dict:
    if MAP_PATH.exists():
        try:
            return json.loads(MAP_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_overrides(d: dict) -> None:
    MAP_PATH.write_text(json.dumps(d, indent=2))
    bump_epoch()


# ----------------------------------------------------------------------------

SETTINGS_PATH = DATA_DIR / "settings.json"
DEFAULT_SETTINGS = {
    "dest_mode": "local",                                  # local | mounted
    "dest_path": str(Path.home() / "Desktop" / "NAS_Staging"),
    "verify_hash": False,                                  # re-hash after copy
    "auto_reconcile": False,                               # auto soft-delete deleted files on launch
    "trash_retention_days": 30,                            # recovery window before purge
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text()))
        except Exception:
            pass
    return s


def save_settings(d: dict) -> dict:
    s = load_settings()
    for k in DEFAULT_SETTINGS:
        if k in d:
            s[k] = d[k]
    SETTINGS_PATH.write_text(json.dumps(s, indent=2))
    return s


def dest_base() -> Path:
    return Path(load_settings()["dest_path"]).expanduser()


def free_gb(path: Path):
    q = path
    while not q.exists() and q != q.parent:
        q = q.parent
    try:
        return round(shutil.disk_usage(q).free / GB, 1)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Core queries
