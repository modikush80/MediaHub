"""Consolidation ledger — records every source file removed by a verified MOVE.

A MOVE (consolidate) only ever removes a source AFTER the destination copy is
hash-verified. Each removal is logged here so the whole operation is auditable:
what was removed, from which drive, where the surviving copy lives, and its hash.

Removal goes to the macOS Trash by default (recoverable) rather than a hard
delete — space is reclaimed when the user empties the Trash.
"""
import os
import sqlite3
import subprocess
import time

from .config import DATA_DIR

MOVES_DB = DATA_DIR / "moves.sqlite3"


def _db():
    conn = sqlite3.connect(MOVES_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS moves(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sha256 TEXT, source_path TEXT, source_device TEXT,
        dest_path TEXT, size INTEGER, verified_hash INTEGER,
        action TEXT, moved_at TEXT)""")
    return conn


def record(sha256, source_path, source_device, dest_path, size,
           verified_hash=True, action="trashed"):
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO moves(sha256,source_path,source_device,dest_path,size,"
            "verified_hash,action,moved_at) VALUES(?,?,?,?,?,?,?,?)",
            (sha256, source_path, source_device, dest_path, int(size or 0),
             1 if verified_hash else 0, action,
             time.strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def _finder_trash(path: str) -> bool:
    """Best-effort real macOS Trash via Finder (nice 'Put Back', but needs
    Automation permission — may be unavailable, so callers must have a fallback)."""
    script = ('tell application "Finder" to move (POSIX file %r as alias) to trash'
              % path)
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and not os.path.exists(path)
    except Exception:
        return False


def _recycle_dir_for(path: str) -> str:
    """A recoverable recycle folder on the SAME volume as `path` (so removal is a
    fast, permission-free rename that never crosses devices)."""
    ap = os.path.abspath(path)
    if ap.startswith("/Volumes/"):
        parts = ap.split("/")
        if len(parts) > 2:
            return os.path.join("/Volumes", parts[2], ".MediaHub_Removed")
    # boot volume: a hidden folder in the user's home (same volume, no perms needed)
    return os.path.expanduser("~/.MediaHub_Removed")


def _recycle(path: str):
    """Move `path` into the same-volume recycle folder. Returns the new path or None."""
    rd = _recycle_dir_for(path)
    try:
        os.makedirs(rd, exist_ok=True)
        base = os.path.basename(path)
        dest = os.path.join(rd, base)
        n = 1
        while os.path.exists(dest):               # avoid clobbering same-named files
            stem, ext = os.path.splitext(base)
            dest = os.path.join(rd, f"{stem}__{n}{ext}"); n += 1
        os.rename(path, dest)                      # same volume -> instant
        return dest
    except OSError:
        try:
            import shutil as _sh
            dest = os.path.join(rd, os.path.basename(path))
            _sh.move(path, dest)                   # cross-device fallback
            return dest
        except Exception:
            return None


def trash_source(path: str):
    """Remove a verified source copy. Returns the action string on success
    ('trashed' via Finder, or 'recycled' to the same-volume recycle folder), or
    None on failure. Never a silent no-op — the caller logs the returned action."""
    if not path or not os.path.exists(path):
        return None
    if _finder_trash(path):
        return "trashed"
    if _recycle(path):
        return "recycled"
    return None


def moves_summary() -> dict:
    if not MOVES_DB.exists():
        return {"count": 0, "reclaimed_bytes": 0}
    conn = _db()
    try:
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM moves").fetchone()
    finally:
        conn.close()
    return {"count": row[0], "reclaimed_bytes": int(row[1] or 0)}


def list_moves(limit: int = 500) -> dict:
    if not MOVES_DB.exists():
        return {"moves": [], "count": 0, "reclaimed_bytes": 0}
    conn = _db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT sha256, source_path, source_device, dest_path, size, "
            "verified_hash, action, moved_at FROM moves ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    finally:
        conn.close()
    s = moves_summary()
    return {"moves": [dict(r) for r in rows], "count": s["count"],
            "reclaimed_bytes": s["reclaimed_bytes"]}


def moves_csv() -> str:
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["moved_at", "action", "sha256", "source_device", "source_path",
                "dest_path", "size_bytes", "verified_hash"])
    for m in list_moves(100000)["moves"]:
        w.writerow([m["moved_at"], m["action"], m["sha256"], m["source_device"],
                    m["source_path"], m["dest_path"], m["size"], m["verified_hash"]])
    return out.getvalue()
