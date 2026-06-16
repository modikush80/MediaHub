"""Read-only inventory DB access + the joined/derived all_files() cache."""
import os
import sqlite3
import threading
from pathlib import Path

from .config import DATA_DIR, bump_epoch
from .classify import classify_device, classify_stage, classify_orientation


def find_db() -> Path:
    candidates = []
    env = os.environ.get("MEDIAHUB_DB")
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve().parent
    candidates += [
        here / "media_indexer.sqlite3",
        DATA_DIR / "media_indexer.sqlite3",
        here.parent / "MediaIndexer_Package" / "media_indexer.sqlite3",
        Path.home() / "Desktop" / "MediaIndexer_Package" / "media_indexer.sqlite3",
        Path.home() / "Desktop" / "MediaHub" / "media_indexer.sqlite3",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise SystemExit(
        "Could not find media_indexer.sqlite3.\n"
        "Place it next to mediahub.py, or set MEDIAHUB_DB=/path/to/media_indexer.sqlite3"
    )


DB_PATH = find_db()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def top_folder_expr() -> str:
    # SQL expression: first path segment after root_path
    rel = "REPLACE(full_path, root_path || '/', '')"
    return (f"CASE WHEN instr({rel}, '/') > 0 "
            f"THEN substr({rel}, 1, instr({rel}, '/') - 1) ELSE {rel} END")


# ----------------------------------------------------------------------------

_FILES_CACHE = None
_FILES_LOCK = threading.Lock()


def all_files():
    """All files joined with capture metadata, with derived device/stage.

    Cached for the process lifetime (the inventory DB is read-only/static).
    """
    global _FILES_CACHE
    with _FILES_LOCK:
        if _FILES_CACHE is not None:
            return _FILES_CACHE
        conn = connect()
        try:
            c = conn.cursor()
            # Soft-deleted rows (deleted_at set by reconcile) are hidden from the
            # whole app but kept recoverable. The column may not exist yet.
            try:
                cols = [r[1] for r in c.execute("PRAGMA table_info(files)").fetchall()]
                _del = "WHERE f.deleted_at IS NULL" if "deleted_at" in cols else ""
            except Exception:
                _del = ""
            rows = c.execute(
                f"""
                SELECT f.id, f.full_path, f.file_name, f.extension, f.file_size,
                       f.sha256, f.device_name, f.root_path, f.created_time,
                       {top_folder_expr().replace('full_path','f.full_path').replace('root_path','f.root_path')} AS top_folder,
                       m.camera_make AS make, m.camera_model AS model,
                       m.capture_date AS capture_date,
                       m.creation_date AS creation_date,
                       CASE WHEN m.raw_metadata_json LIKE '%AbsoluteAltitude%'
                            THEN 1 ELSE 0 END AS has_altitude,
                       CASE WHEN m.raw_metadata_json LIKE '%Adobe Lightroom%'
                              OR m.raw_metadata_json LIKE '%Adobe Photoshop%'
                              OR m.raw_metadata_json LIKE '%Adobe ImageReady%'
                              OR m.raw_metadata_json LIKE '%Topaz%'
                              OR m.raw_metadata_json LIKE '%Capture One%'
                              OR m.raw_metadata_json LIKE '%DaVinci%'
                              OR m.raw_metadata_json LIKE '%Premiere%'
                              OR m.raw_metadata_json LIKE '%Final Cut%'
                            THEN 1 ELSE 0 END AS edited_sw,
                       m.image_width AS width, m.image_height AS height,
                       CASE WHEN m.raw_metadata_json LIKE '%"Orientation": 5%'
                              OR m.raw_metadata_json LIKE '%"Orientation": 6%'
                              OR m.raw_metadata_json LIKE '%"Orientation": 7%'
                              OR m.raw_metadata_json LIKE '%"Orientation": 8%'
                              OR m.raw_metadata_json LIKE '%"Orientation":5%'
                              OR m.raw_metadata_json LIKE '%"Orientation":6%'
                              OR m.raw_metadata_json LIKE '%"Orientation":7%'
                              OR m.raw_metadata_json LIKE '%"Orientation":8%'
                            THEN 1 ELSE 0 END AS rotated
                FROM files f
                LEFT JOIN media_metadata m ON m.file_id = f.id
                {_del}
                """
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["device"] = classify_device(
                    d.get("make"), d.get("model"), d["extension"],
                    d["file_name"], bool(d.get("has_altitude")))
                d["stage"] = classify_stage(
                    d["extension"], d["file_name"],
                    bool(d.get("edited_sw")), d["top_folder"])
                d["orientation"] = classify_orientation(
                    d.get("width"), d.get("height"), bool(d.get("rotated")))
                out.append(d)
            _FILES_CACHE = out
            return out
        finally:
            conn.close()



def invalidate_files_cache():
    """Clear the all_files cache (call after ingest mutates the DB)."""
    global _FILES_CACHE
    with _FILES_LOCK:
        _FILES_CACHE = None
    bump_epoch()
