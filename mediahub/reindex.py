"""Library reconcile / reindex — keep the database honest when you delete files.

Detects files that were deleted from disk and prunes the stale rows from the
inventory AND every derived store (embeddings, vision tags, captions, faces).

CRITICAL drive-awareness: a file is only judged "deleted" when its drive is
CURRENTLY MOUNTED and the file is gone. Files on an UNMOUNTED drive are left
untouched — their absence just means the drive isn't connected, not deleted.
This prevents wiping your index just because a drive is unplugged.

Never touches media files. Pruning the inventory is an explicit user action
(like ingest); side-DB pruning is always safe (derived/rebuildable).
"""
import os
import sqlite3
import threading
import time

from .config import DATA_DIR
from .db import DB_PATH, all_files, invalidate_files_cache
from . import drives

RECON = {"status": "idle", "log": "", "checked": 0, "present": 0,
         "missing": 0, "pruned": 0, "skipped_unmounted": 0,
         "missing_sample": [], "last_run": None, "last_prune": None}
LOCK = threading.Lock()

SIDE_DBS = ["embeddings.sqlite3", "vision_tags.sqlite3", "captions.sqlite3", "faces.sqlite3"]
SIDE_TABLES = {"embeddings.sqlite3": "embeddings", "vision_tags.sqlite3": "vision_tags",
               "captions.sqlite3": "captions", "faces.sqlite3": "faces"}


def _set(**kw):
    with LOCK:
        RECON.update(**kw)


def _scan_missing():
    """Return (missing[(id, full_path)], present, skipped_unmounted).

    A file counts as DELETED only when its containing folder is present on the
    resolved mount but the file itself is gone. If the parent folder is also
    absent, the drive/path isn't really there (wrong mount match or a
    cross-machine inventory) — we skip it rather than risk a false deletion.
    """
    mount_map = drives.resolve_all()
    missing, present, skipped = [], 0, 0
    for f in all_files():
        fp = f.get("full_path")
        if fp and os.path.exists(fp):
            present += 1
            continue
        dev = f.get("device_name")
        mount = mount_map.get(dev)
        if not mount:
            skipped += 1                      # drive not connected — unknown, leave alone
            continue
        rew = drives.rewrite_path(fp, f.get("root_path"), mount) or fp
        if rew and os.path.exists(rew):
            present += 1
            continue
        # Drive resolved, file absent — only trust this as a deletion if the
        # parent FOLDER exists on disk (proves we're looking at the right drive).
        parent = os.path.dirname(rew or "")
        if parent and os.path.isdir(parent):
            missing.append((f.get("id"), fp))
        else:
            skipped += 1                      # folder missing too → not really mounted here
    return missing, present, skipped


def _ensure_deleted_col(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN deleted_at TEXT")


def _prune_side_dbs(paths):
    """Hard-remove derived rows for these paths (always safe — rebuildable)."""
    for dbname in SIDE_DBS:
        p = DATA_DIR / dbname
        if not p.exists() or not paths:
            continue
        try:
            c = sqlite3.connect(p)
            tbl = SIDE_TABLES[dbname]
            c.executemany(f"DELETE FROM {tbl} WHERE full_path = ?", [(x,) for x in paths])
            c.commit(); c.close()
        except Exception:
            pass


def _prune(missing):
    """SOFT delete: mark rows deleted_at=now so they vanish from the app but stay
    recoverable for the retention window. Side DBs are pruned only on hard purge."""
    ids = [i for i, _p in missing if i is not None]
    if not ids:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_deleted_col(conn)
        qmarks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE files SET deleted_at=? WHERE id IN ({qmarks})", [now] + ids)
        conn.commit()
    finally:
        conn.close()
    invalidate_files_cache()                  # bump epoch -> trips/overview refresh
    return len(ids)


def _retention_days() -> int:
    try:
        from .settings import load_settings
        return int(load_settings().get("trash_retention_days", 30) or 30)
    except Exception:
        return 30


def trash_list(limit: int = 500) -> dict:
    """Soft-deleted files awaiting purge, with days remaining."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
        if "deleted_at" not in cols:
            return {"items": [], "retention_days": _retention_days()}
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, full_path, file_size, device_name, deleted_at FROM files "
            "WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    ret = _retention_days()
    items = [{"id": r["id"], "path": r["full_path"], "size": r["file_size"],
              "device": r["device_name"], "deleted_at": r["deleted_at"]} for r in rows]
    return {"items": items, "count": len(items), "retention_days": ret}


def restore(ids) -> dict:
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return {"restored": 0}
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_deleted_col(conn)
        qmarks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE files SET deleted_at=NULL WHERE id IN ({qmarks})", ids)
        conn.commit()
    finally:
        conn.close()
    invalidate_files_cache()
    return {"restored": len(ids)}


def purge(ids=None, expired=False) -> dict:
    """Hard-delete soft-deleted rows (specific ids, or all past the retention
    window) from the inventory + derived stores. Irreversible."""
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_deleted_col(conn)
        conn.row_factory = sqlite3.Row
        if expired:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - _retention_days() * 86400))
            rows = conn.execute(
                "SELECT id, full_path FROM files WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,)).fetchall()
        elif ids:
            qmarks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id, full_path FROM files WHERE deleted_at IS NOT NULL AND id IN ({qmarks})",
                [int(i) for i in ids]).fetchall()
        else:
            return {"purged": 0}
        pids = [r["id"] for r in rows]
        paths = [r["full_path"] for r in rows if r["full_path"]]
        if pids:
            qm = ",".join("?" * len(pids))
            conn.execute(f"DELETE FROM files WHERE id IN ({qm})", pids)
            try:
                conn.execute(f"DELETE FROM media_metadata WHERE file_id IN ({qm})", pids)
            except Exception:
                pass
            conn.commit()
    finally:
        conn.close()
    if paths:
        _prune_side_dbs(paths)
    invalidate_files_cache()
    return {"purged": len(pids)}


def _run(prune):
    try:
        _set(status="running", log="Scanning for deleted files (mounted drives only)…\n",
             checked=0, present=0, missing=0, pruned=0, skipped_unmounted=0, missing_sample=[])
        missing, present, skipped = _scan_missing()
        sample = [p for _i, p in missing[:20] if p]
        _set(checked=present + len(missing) + skipped, present=present,
             missing=len(missing), skipped_unmounted=skipped, missing_sample=sample,
             last_run=time.strftime("%Y-%m-%d %H:%M"))
        with LOCK:
            RECON["log"] += (f"Found {len(missing)} deleted file(s) on mounted drives; "
                             f"{skipped} file(s) on unmounted drives skipped.\n")
        pruned = 0
        if prune and missing:
            # Safety guard: if NOTHING in the library resolves on this machine,
            # the inventory likely belongs to another Mac/drive set — never prune.
            if present == 0:
                with LOCK:
                    RECON["log"] += ("Refusing to prune: 0 files resolve on this machine "
                                     "(inventory appears to be from another Mac/drive set).\n")
            else:
                pruned = _prune(missing)
                _set(pruned=pruned, last_prune=time.strftime("%Y-%m-%d %H:%M"))
                with LOCK:
                    RECON["log"] += (f"Moved {pruned} file(s) to Trash — recoverable for "
                                     f"{_retention_days()} days, then auto-purged.\n")
        _set(status="done")
    except Exception as e:  # noqa: BLE001
        with LOCK:
            RECON["log"] += f"ERROR: {e}\n"
        _set(status="error")


def start_reconcile(prune=False) -> bool:
    with LOCK:
        if RECON["status"] == "running":
            return False
        RECON["status"] = "running"
    threading.Thread(target=_run, args=(bool(prune),), daemon=True).start()
    return True


def reconcile_status() -> dict:
    with LOCK:
        return dict(RECON)
