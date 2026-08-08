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


def trash_source(path: str) -> bool:
    """Move a source file to the macOS Trash (recoverable). Returns True on success.
    Uses Finder via osascript so it lands in the correct volume's .Trashes."""
    if not path or not os.path.exists(path):
        return False
    script = ('tell application "Finder" to move (POSIX file %r as alias) to trash'
              % path)
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and not os.path.exists(path)
    except Exception:
        return False


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
