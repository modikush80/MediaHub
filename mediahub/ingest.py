"""Self-contained scanner that indexes new media into the shared DB."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .db import DB_PATH, invalidate_files_cache
from .classify import BUCKETS
from .mounts import device_for_path
from . import drives


INGEST_LOCK = threading.Lock()
INGEST = {"status": "idle", "path": "", "log": "", "started_at": None,
          "discovered": 0, "scanned": 0}

INGEST_EXTS = (BUCKETS["photos"] | BUCKETS["raw"] | BUCKETS["videos"]
               | BUCKETS["sidecar"])


def _ilog(msg):
    with INGEST_LOCK:
        INGEST["log"] = (INGEST["log"] + msg + "\n")[-8000:]



def _sha256_path(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _exiftool_batch(paths):
    """Return {path: {Make,Model,DateTimeOriginal,GPSAltitude,...}} or {}."""
    if not shutil.which("exiftool") or not paths:
        return {}
    try:
        r = subprocess.run(
            ["exiftool", "-json", "-n", "-Make", "-Model", "-DateTimeOriginal",
             "-CreateDate", "-GPSAltitude", "-AbsoluteAltitude", "-Software",
             "-ImageWidth", "-ImageHeight", *paths],
            capture_output=True, text=True, timeout=300)
        data = json.loads(r.stdout or "[]")
        return {d.get("SourceFile"): d for d in data}
    except Exception:
        return {}


def _run_ingest(scan_path):
    with INGEST_LOCK:
        INGEST.update(status="running", path=scan_path, log="",
                      started_at=time.time(), discovered=0, scanned=0)
    has_exif = bool(shutil.which("exiftool"))
    _ilog(f"Scanning {scan_path}")
    _ilog(f"exiftool: {'found — extracting camera metadata' if has_exif else 'not found — indexing without EXIF (optional: brew install exiftool)'}")
    try:
        # discover
        targets = []
        for root, _dirs, files in os.walk(scan_path):
            for fn in files:
                if Path(fn).suffix.lower() in INGEST_EXTS:
                    targets.append(os.path.join(root, fn))
        with INGEST_LOCK:
            INGEST["discovered"] = len(targets)
        _ilog(f"Discovered {len(targets)} media files.")

        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        now = datetime.now().isoformat(timespec="seconds")
        cur.execute(
            "INSERT INTO scan_runs(started_at,roots_json,files_discovered,status) "
            "VALUES(?,?,?,?)", (now, json.dumps([scan_path]), len(targets), "running"))
        scan_id = cur.lastrowid
        conn.commit()

        # Capture the source volume identity (UUID etc.) for exact future
        # renamed-drive recognition. Normalized into scan_roots; no per-file dup.
        cur.execute("""CREATE TABLE IF NOT EXISTS scan_roots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, scan_run_id INTEGER, scan_path TEXT,
            volume_name TEXT, volume_uuid TEXT, mount_path TEXT, fstype TEXT,
            total_bytes INTEGER, root_fingerprint TEXT, scanned_at TEXT)""")
        try:
            vid = drives.capture_volume(scan_path)
            cur.execute("""INSERT INTO scan_roots(scan_run_id,scan_path,volume_name,
                volume_uuid,mount_path,fstype,total_bytes,root_fingerprint,scanned_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (scan_id, scan_path, vid["volume_name"], vid["volume_uuid"],
                 vid["mount_path"], vid["fstype"], vid["total_bytes"],
                 vid["root_fingerprint"], now))
            conn.commit()
            _ilog(f"Volume identity: '{vid['volume_name']}' uuid={vid['volume_uuid']} "
                  f"fs={vid['fstype']}")
        except Exception as e:
            _ilog(f"  (volume identity capture skipped: {e})")
        scanned = 0
        BATCH = 80
        for i in range(0, len(targets), BATCH):
            batch = targets[i:i + BATCH]
            meta = _exiftool_batch(batch) if has_exif else {}
            for p in batch:
                try:
                    stat = os.stat(p)
                    sha = _sha256_path(p)
                    ext = Path(p).suffix.lower()
                    ctime = datetime.fromtimestamp(stat.st_birthtime
                                if hasattr(stat, "st_birthtime") else stat.st_ctime).isoformat()
                    mtime = datetime.fromtimestamp(stat.st_mtime).isoformat()
                    mime = ("video/" if ext in BUCKETS["videos"] else "image/") + ext.lstrip(".")
                    dev = device_for_path(p)
                    root_path = scan_path
                    cur.execute("""
                        INSERT INTO files(full_path,file_name,extension,file_size,
                            created_time,modified_time,sha256,mime_type,root_path,
                            device_name,first_seen_scan_id,last_seen_scan_id,indexed_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(full_path) DO UPDATE SET
                            file_size=excluded.file_size, sha256=excluded.sha256,
                            modified_time=excluded.modified_time,
                            last_seen_scan_id=excluded.last_seen_scan_id,
                            indexed_at=excluded.indexed_at
                    """, (p, Path(p).name, ext, stat.st_size, ctime, mtime, sha, mime,
                          root_path, dev, scan_id, scan_id, now))
                    fid = cur.execute("SELECT id FROM files WHERE full_path=?",
                                      (p,)).fetchone()[0]
                    md = meta.get(p)
                    if md:
                        mt = "video" if ext in BUCKETS["videos"] else "photo"
                        cur.execute("""
                            INSERT INTO media_metadata(file_id,media_type,capture_date,
                                creation_date,camera_make,camera_model,image_width,
                                image_height,raw_metadata_json,extracted_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(file_id) DO UPDATE SET
                                camera_make=excluded.camera_make,
                                camera_model=excluded.camera_model,
                                raw_metadata_json=excluded.raw_metadata_json
                        """, (fid, mt, md.get("DateTimeOriginal"), md.get("CreateDate"),
                              md.get("Make"), md.get("Model"), md.get("ImageWidth"),
                              md.get("ImageHeight"), json.dumps(md), now))
                    scanned += 1
                except Exception as e:
                    _ilog(f"  ! {Path(p).name}: {e}")
            conn.commit()
            with INGEST_LOCK:
                INGEST["scanned"] = scanned
            _ilog(f"  indexed {scanned}/{len(targets)}")

        cur.execute("UPDATE scan_runs SET ended_at=?,files_scanned=?,status=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), scanned, "completed", scan_id))
        conn.commit()
        conn.close()
        invalidate_files_cache()             # refresh trips/sources/dedupe
        _ilog(f"Done. Indexed {scanned} files into the shared database. "
              f"Trips & duplicates refreshed.")
        with INGEST_LOCK:
            INGEST["status"] = "done"
    except Exception as e:
        _ilog(f"ERROR: {e}")
        with INGEST_LOCK:
            INGEST["status"] = "error"


def start_ingest(scan_path):
    with INGEST_LOCK:
        if INGEST["status"] == "running":
            return False
        INGEST.update(status="running", log="", started_at=time.time(),
                      discovered=0, scanned=0)
    threading.Thread(target=_run_ingest, args=(scan_path,), daemon=True).start()
    return True


