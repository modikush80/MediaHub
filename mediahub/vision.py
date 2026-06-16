"""Optional Apple Vision content-tagging — wraps the vision/ Swift+Python tool.

Vision identifies image *content* (scene/objects/text), not the capture device.
This drives the "Vision" tab: it tags the vague pile (photos with no EXIF make)
on the Neural Engine and writes results to a SEPARATE vision_tags.sqlite3 — the
read-only inventory is never touched, and no files are moved.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from .config import DATA_DIR
from .db import DB_PATH, connect

PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff")
OUT_DB = DATA_DIR / "vision_tags.sqlite3"

VISION_LOCK = threading.Lock()
VISION = {"status": "idle", "log": "", "started_at": None}


def _vision_dir():
    here = Path(__file__).resolve().parent
    for c in (here.parent / "vision", Path.home() / "Desktop" / "MediaHub" / "vision"):
        if (c / "vision_enrich.py").exists():
            return c
    return None


def vision_available() -> dict:
    vd = _vision_dir()
    if not vd:
        return {"available": False, "has_binary": False, "has_swiftc": False,
                "reason": "Vision module folder not found"}
    has_bin = (vd / "vision_tag").exists()
    has_swiftc = bool(shutil.which("swiftc"))
    ok = has_bin or has_swiftc
    return {"available": ok, "has_binary": has_bin, "has_swiftc": has_swiftc,
            "reason": "" if ok else "needs Xcode Command Line Tools (swiftc) to build the Vision tool"}


def vision_candidates() -> int:
    """Count unique vague photos (photo extension, no EXIF camera make)."""
    try:
        conn = connect()
        q = ("SELECT COUNT(*) FROM (SELECT f.sha256 FROM files f "
             "LEFT JOIN media_metadata m ON m.file_id = f.id "
             "WHERE lower(f.extension) IN ({}) "
             "AND (m.camera_make IS NULL OR m.camera_make = '') "
             "GROUP BY f.sha256)").format(",".join("?" * len(PHOTO_EXTS)))
        n = conn.execute(q, list(PHOTO_EXTS)).fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def vision_summary() -> dict:
    if not OUT_DB.exists():
        return {"tagged": 0, "buckets": [], "db_present": False}
    try:
        conn = sqlite3.connect(f"file:{OUT_DB}?mode=ro", uri=True)
        total = conn.execute("SELECT COUNT(*) FROM vision_tags").fetchone()[0]
        rows = conn.execute(
            "SELECT content_bucket, COUNT(*) FROM vision_tags "
            "GROUP BY content_bucket ORDER BY COUNT(*) DESC").fetchall()
        conn.close()
        return {"tagged": int(total), "db_present": True,
                "buckets": [{"bucket": b or "Unsorted", "count": n} for b, n in rows]}
    except Exception as e:
        return {"tagged": 0, "buckets": [], "db_present": False, "error": str(e)}


_STOP = {"screenshot", "screenshots", "screen", "with", "the", "and", "for",
         "image", "images", "photo", "photos", "a", "of", "in", "on", "to"}


def vision_search(query, limit=80) -> dict:
    """Keyword search over the OCR text + labels captured by Vision — e.g.
    'rakuten cashback 10%' finds the screenshot whose text contains them."""
    if not OUT_DB.exists():
        return {"query": query, "results": []}
    toks = [t for t in re.findall(r"[a-z0-9%$.]+", (query or "").lower())
            if len(t) >= 2 and t not in _STOP]
    if not toks:
        return {"query": query, "results": []}
    try:
        conn = sqlite3.connect(f"file:{OUT_DB}?mode=ro", uri=True)
        rows = conn.execute("SELECT full_path, content_bucket, top_label, has_text, "
                            "text_sample FROM vision_tags").fetchall()
        conn.close()
    except Exception as e:
        return {"query": query, "results": [], "error": str(e)}
    scored = []
    for fp, bucket, label, ht, txt in rows:
        hay = f"{txt or ''} {label or ''} {bucket or ''}".lower()
        hits = sum(1 for t in toks if t in hay)
        if not hits:
            continue
        # snippet around the first matched token
        snip = (txt or "")
        for t in toks:
            i = (txt or "").lower().find(t)
            if i >= 0:
                snip = (txt or "")[max(0, i - 30):i + 70]
                break
        scored.append((hits, {
            "file_name": os.path.basename(fp or ""), "path": fp,
            "bucket": bucket, "top_label": label, "has_text": bool(ht),
            "text": snip, "score": hits}))
    scored.sort(key=lambda x: -x[0])
    return {"query": query, "matched_tokens": toks,
            "results": [r for _, r in scored[:limit]]}


def vision_results(bucket=None, limit=300) -> dict:
    """List tagged files (optionally filtered to one bucket) for the UI browser."""
    if not OUT_DB.exists():
        return {"bucket": bucket, "results": []}
    try:
        conn = sqlite3.connect(f"file:{OUT_DB}?mode=ro", uri=True)
        q = ("SELECT full_path, top_label, top_conf, content_bucket, has_text, "
             "substr(text_sample,1,140) FROM vision_tags")
        params = []
        if bucket:
            q += " WHERE content_bucket = ?"
            params.append(bucket)
        q += " ORDER BY top_conf DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(q, params).fetchall()
        conn.close()
        return {"bucket": bucket, "results": [
            {"file_name": os.path.basename(r[0] or ""), "path": r[0],
             "top_label": r[1], "top_conf": round(r[2] or 0, 2),
             "bucket": r[3], "has_text": bool(r[4]), "text": r[5] or ""}
            for r in rows]}
    except Exception as e:
        return {"bucket": bucket, "results": [], "error": str(e)}


def vision_status() -> dict:
    with VISION_LOCK:
        st = dict(VISION)
    st["available_info"] = vision_available()
    st["candidates"] = vision_candidates()
    st.update(vision_summary())
    return st


def _vlog(msg: str):
    with VISION_LOCK:
        VISION["log"] = (VISION["log"] + msg)[-12000:]


def _run_vision(limit, only_unsorted, under=None, folder=None):
    vd = _vision_dir()
    if not vd:
        with VISION_LOCK:
            VISION.update(status="error", log="Vision module folder not found\n")
        return
    with VISION_LOCK:
        VISION.update(status="running", log="", started_at=time.time())
    cmd = ["python3", str(vd / "vision_enrich.py"),
           "--db", str(DB_PATH), "--out", str(OUT_DB)]
    if limit and int(limit) > 0:
        cmd += ["--limit", str(int(limit))]
    else:
        cmd += ["--all"]
    if folder:
        cmd += ["--folder", str(folder)]
    else:
        if only_unsorted:
            cmd += ["--only-unsorted"]
        if under:
            cmd += ["--under", str(under)]
    env = dict(os.environ, MEDIAHUB_DB=str(DB_PATH))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
        for line in proc.stdout:
            _vlog(line)
        proc.wait()
        with VISION_LOCK:
            VISION["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as e:
        _vlog(f"ERROR: {e}\n")
        with VISION_LOCK:
            VISION["status"] = "error"


def start_vision(limit=2000, only_unsorted=False, under=None, folder=None) -> bool:
    with VISION_LOCK:
        if VISION["status"] == "running":
            return False
        VISION.update(status="running", log="", started_at=time.time())
    threading.Thread(target=_run_vision, args=(limit, only_unsorted, under, folder),
                     daemon=True).start()
    return True
