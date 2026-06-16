#!/usr/bin/env python3
"""
vision_enrich.py - optional content tagging for vague/unsorted images.

Apple Vision identifies CONTENT (scene/objects/text), not the capture device.
Device/stage sorting is handled by MediaHub from EXIF; this module is for the
pile that EXIF can't help with: files with no camera make and generic names
(e.g. the UNORGANIZED folder).

What it does on your M5 Max (Neural Engine):
  1. Selects candidate image files from the inventory that exist on disk.
  2. Runs scene classification + OCR via the bundled Swift tool (vision_tag).
  3. Stores results in a SEPARATE database (vision_tags.sqlite3) - the
     read-only inventory is never modified.
  4. Prints suggested content buckets (Documents/Screenshots, People, Nature,
     Food, etc.) you can use to triage the unsorted pile.

Standard library only. Requires the Swift tool (built automatically if needed).

Usage:
  python3 vision_enrich.py --limit 2000
  python3 vision_enrich.py --all
  python3 vision_enrich.py --db /path/to/media_indexer.sqlite3
"""

from __future__ import annotations
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWIFT_SRC = HERE / "vision_tag.swift"
SWIFT_BIN = HERE / "vision_tag"

PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff")

# Map Vision scene labels -> coarse content buckets for triage.
BUCKET_KEYWORDS = {
    "Documents": ["document", "text", "paper", "receipt", "menu", "screenshot",
                  "page", "book", "newspaper", "whiteboard", "sign"],
    "People": ["people", "person", "face", "portrait", "crowd", "group",
               "selfie", "baby", "wedding"],
    "Nature": ["mountain", "landscape", "beach", "ocean", "sea", "lake", "river",
               "forest", "tree", "sky", "cloud", "sunset", "sunrise", "waterfall",
               "snow", "desert", "field", "flower", "plant", "canyon", "valley"],
    "Wildlife": ["bird", "animal", "dog", "cat", "fish", "wildlife", "insect",
                 "puffin", "whale", "deer", "horse"],
    "Cityscape": ["city", "building", "architecture", "street", "bridge",
                  "skyline", "urban", "monument", "church", "tower"],
    "Food": ["food", "meal", "drink", "coffee", "restaurant", "dish", "fruit",
             "dessert", "plate"],
    "Vehicles": ["car", "boat", "airplane", "aircraft", "vehicle", "train",
                 "motorcycle", "bicycle"],
}


def find_db() -> Path:
    env = os.environ.get("MEDIAHUB_DB")
    cands = [Path(env).expanduser()] if env else []
    cands += [
        HERE.parent / "media_indexer.sqlite3",
        Path.home() / "Desktop" / "MediaIndexer_Package" / "media_indexer.sqlite3",
        Path.home() / "Desktop" / "MediaHub" / "media_indexer.sqlite3",
    ]
    for c in cands:
        if c and c.exists():
            return c
    sys.exit("Could not find media_indexer.sqlite3 (set MEDIAHUB_DB).")


def ensure_swift_built() -> Path:
    if SWIFT_BIN.exists() and SWIFT_BIN.stat().st_mtime >= SWIFT_SRC.stat().st_mtime:
        return SWIFT_BIN
    print("Building Swift Vision tool (one-time)...")
    r = subprocess.run(["swiftc", "-O", str(SWIFT_SRC), "-o", str(SWIFT_BIN)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"swiftc build failed:\n{r.stderr}\n"
                 "Install Xcode Command Line Tools: xcode-select --install")
    return SWIFT_BIN


def candidate_files(db: Path, limit: int | None, only_unsorted: bool, under: str | None = None):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    where = (
        "WHERE lower(f.extension) IN ({}) "
        "AND (m.camera_make IS NULL OR m.camera_make = '')"
    ).format(",".join("?" * len(PHOTO_EXTS)))
    params = list(PHOTO_EXTS)
    if only_unsorted:
        where += (" AND (f.full_path LIKE '%UNORGANIZED%' "
                  "OR f.full_path LIKE '%nsorted%' OR f.full_path LIKE '%ntitled%')")
    if under:
        where += " AND f.full_path LIKE ?"
        params.append(under.rstrip("/") + "/%")
    sql = f"""
        SELECT f.id, f.full_path, f.file_name
        FROM files f LEFT JOIN media_metadata m ON m.file_id = f.id
        {where}
        GROUP BY f.sha256
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def open_out_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vision_tags (
            file_id INTEGER PRIMARY KEY,
            full_path TEXT,
            top_label TEXT,
            top_conf REAL,
            labels_json TEXT,
            has_text INTEGER,
            text_sample TEXT,
            content_bucket TEXT,
            tagged_at TEXT
        )""")
    return conn


def bucketize(labels, has_text, text_sample) -> str:
    ids = [l["id"].lower() for l in labels]
    blob = " ".join(ids)
    # Strong signal: OCR text + document-ish labels -> Documents/Screenshots
    if has_text and (any(k in blob for k in BUCKET_KEYWORDS["Documents"])
                     or len(text_sample) > 12):
        return "Documents"
    for bucket, kws in BUCKET_KEYWORDS.items():
        if any(any(k in i for i in ids) for k in kws):
            return bucket
    return "Unsorted"


def folder_candidates(folder, limit):
    """Walk an arbitrary folder for images — independent of the inventory DB."""
    import hashlib
    out = []
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if fn.startswith("._"):
                continue
            if Path(fn).suffix.lower() in PHOTO_EXTS:
                p = os.path.join(root, fn)
                fid = int(hashlib.sha1(p.encode()).hexdigest()[:15], 16)
                out.append({"id": fid, "full_path": p, "file_name": fn})
                if limit and len(out) >= limit:
                    return out
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=HERE / "vision_tags.sqlite3")
    ap.add_argument("--limit", type=int, default=2000,
                    help="max files (default 2000; use --all to remove)")
    ap.add_argument("--all", action="store_true", help="process all candidates")
    ap.add_argument("--only-unsorted", action="store_true",
                    help="restrict to UNORGANIZED/unsorted folders")
    ap.add_argument("--under", default=None,
                    help="restrict to files under this folder path")
    ap.add_argument("--folder", default=None,
                    help="tag ANY folder directly (walk the FS, ignore the inventory)")
    args = ap.parse_args()

    binp = ensure_swift_built()
    limit = None if args.all else args.limit
    if args.folder:
        db = None
        files = folder_candidates(args.folder, limit)
    else:
        db = args.db or find_db()
        files = candidate_files(db, limit, args.only_unsorted, args.under)

    # Only those that exist on disk right now.
    present = [f for f in files if Path(f["full_path"]).exists()]
    missing = len(files) - len(present)
    print(f"Source       : {args.folder if args.folder else ('inventory ' + str(db))}")
    print(f"Candidates   : {len(files)} vague images "
          f"({missing} on unmounted drives, skipped)")
    print(f"To tag       : {len(present)}")
    if not present:
        print("Nothing to tag (attach the source drives and retry).")
        return

    out = open_out_db(args.out)
    proc = subprocess.Popen([str(binp)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    # feed paths in a thread so we can read results streaming
    import threading
    by_path = {f["full_path"]: f for f in present}

    def feeder():
        for f in present:
            proc.stdin.write(f["full_path"] + "\n")
        proc.stdin.close()
    threading.Thread(target=feeder, daemon=True).start()

    counts = {}
    done = 0
    t0 = time.time()
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        f = by_path.get(r.get("path"))
        if not f:
            continue
        done += 1
        if not r.get("ok"):
            continue
        labels = r.get("labels", [])
        has_text = 1 if r.get("hasText") else 0
        text_sample = r.get("text", "")
        bucket = bucketize(labels, has_text, text_sample)
        counts[bucket] = counts.get(bucket, 0) + 1
        top = labels[0] if labels else {"id": "", "conf": 0}
        out.execute(
            "INSERT OR REPLACE INTO vision_tags VALUES (?,?,?,?,?,?,?,?,?)",
            (f["id"], f["full_path"], top["id"], top["conf"],
             json.dumps(labels), has_text, text_sample, bucket,
             time.strftime("%Y-%m-%dT%H:%M:%S")))
        if done % 200 == 0:
            out.commit()
            rate = done / max(time.time() - t0, 0.001)
            print(f"  tagged {done}/{len(present)}  ({rate:.0f}/s)", flush=True)
    out.commit()
    proc.wait()

    print(f"\nDone: tagged {done} images in {time.time()-t0:.1f}s")
    print(f"Results DB: {args.out}")
    print("\nSuggested content buckets:")
    for b, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {b:<12} {n}")
    print("\nThese are CONTENT suggestions for the unsorted pile. Review the "
          "vision_tags table; MediaHub does not move files based on these.")


if __name__ == "__main__":
    main()
