"""Faces / People — on-device face detection + clustering into person groups.

Pipeline: a Swift Vision tool (face_detect.swift) detects faces and emits a
per-face feature print (L2-normalized). We cluster those vectors with a cosine
threshold into "people" and store them in a SEPARATE faces.sqlite3. Report-only.

No model download needed — Apple Vision is built in; we only need `swiftc` once to
build the helper (same as the content-tagging tool). Honest caveat: Apple does not
expose a public face-identity embedding, so clustering uses the cropped-face image
feature print — good for grouping obvious same-person shots, not forensic identity.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from .config import DATA_DIR, CACHE_DIR
from . import search as _search

FACES_DB = DATA_DIR / "faces.sqlite3"
FACES = {"status": "idle", "log": "", "done": 0, "total": 0, "people": 0}
FACES_LOCK = threading.Lock()


def _vision_dir():
    here = Path(__file__).resolve().parent
    for c in (here.parent / "vision", Path.home() / "Desktop" / "MediaHub" / "vision"):
        if (c / "face_detect.swift").exists():
            return c
    return None


def _tool() -> Path | None:
    """Return the compiled face_detect binary, building it once if needed."""
    vd = _vision_dir()
    if not vd:
        return None
    binp = CACHE_DIR / "face_detect"
    src = vd / "face_detect.swift"
    if binp.exists() and binp.stat().st_mtime >= src.stat().st_mtime:
        return binp
    if not shutil.which("swiftc"):
        return binp if binp.exists() else None
    _flog("Building Swift face tool (one-time)…\n")
    r = subprocess.run(
        ["swiftc", "-O", "-framework", "Vision", "-framework", "AppKit",
         "-framework", "CoreImage", str(src), "-o", str(binp)],
        capture_output=True, text=True)
    if r.returncode != 0:
        _flog("build failed:\n" + (r.stderr or "")[-800:] + "\n")
        return None
    return binp


def faces_available() -> dict:
    vd = _vision_dir()
    has_bin = (CACHE_DIR / "face_detect").exists()
    has_swiftc = bool(shutil.which("swiftc"))
    ok = bool(vd) and (has_bin or has_swiftc)
    return {"available": ok, "has_binary": has_bin, "has_swiftc": has_swiftc,
            "reason": "" if ok else "needs Xcode Command Line Tools (swiftc) for the face tool"}


def _db():
    conn = sqlite3.connect(FACES_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS faces(
        id INTEGER PRIMARY KEY AUTOINCREMENT, full_path TEXT, person_id INTEGER,
        bbox TEXT, vec BLOB, created_at TEXT)""")
    return conn


def _flog(msg: str):
    with FACES_LOCK:
        FACES["log"] = (FACES["log"] + msg)[-12000:]


def _targets(under, folder):
    cands = (_search.folder_candidate_images(folder) if folder
             else _search.candidate_images())
    out = []
    for _fid, p in cands:
        if p and os.path.exists(p):
            if folder is None and under and under not in p:
                continue
            out.append(p)
    return out


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cluster(vectors, threshold=0.82):
    """Greedy online clustering by cosine to existing centroids."""
    centroids = []  # list of (sum_vec, count)
    labels = []
    for v in vectors:
        best, bi = threshold, -1
        for i, (cs, cnt) in enumerate(centroids):
            c = [s / cnt for s in cs]
            sim = _cos(v, c)
            if sim >= best:
                best, bi = sim, i
        if bi < 0:
            centroids.append(([x for x in v], 1))
            labels.append(len(centroids) - 1)
        else:
            cs, cnt = centroids[bi]
            centroids[bi] = ([s + x for s, x in zip(cs, v)], cnt + 1)
            labels.append(bi)
    return labels


def _run_faces(under=None, folder=None):
    try:
        tool = _tool()
        if not tool:
            _flog("Face tool unavailable (need swiftc / Xcode CLT).\n")
            with FACES_LOCK:
                FACES["status"] = "error"
            return
        targets = _targets(under, folder)
        with FACES_LOCK:
            FACES.update(total=len(targets), done=0)
        _flog(f"Detecting faces in {len(targets)} images…\n")
        if not targets:
            _flog("No images to scan.\n")
            with FACES_LOCK:
                FACES["status"] = "done"
            return
        proc = subprocess.Popen([str(tool)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, bufsize=1)
        face_rows = []  # (path, bbox, vec)
        done = 0

        def _feed():
            for p in targets:
                try:
                    proc.stdin.write(p + "\n"); proc.stdin.flush()
                except Exception:
                    break
            try:
                proc.stdin.close()
            except Exception:
                pass
        threading.Thread(target=_feed, daemon=True).start()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            for f in r.get("faces", []):
                face_rows.append((r["path"], f.get("bbox"), f.get("vec")))
            done += 1
            with FACES_LOCK:
                FACES["done"] = done
            if done % 50 == 0:
                _flog(f"  scanned {done}/{len(targets)} ({len(face_rows)} faces)\n")
        proc.wait()

        _flog(f"Clustering {len(face_rows)} faces…\n")
        vecs = [fr[2] for fr in face_rows if fr[2]]
        labels = _cluster(vecs) if vecs else []
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn = _db()
        conn.execute("DELETE FROM faces")
        li = 0
        for (path, bbox, vec) in face_rows:
            if not vec:
                continue
            import array as _arr
            blob = _arr.array("f", vec).tobytes()
            conn.execute("INSERT INTO faces(full_path,person_id,bbox,vec,created_at) VALUES(?,?,?,?,?)",
                         (path, int(labels[li]), json.dumps(bbox), blob, now))
            li += 1
        conn.commit()
        npeople = len(set(labels))
        conn.close()
        with FACES_LOCK:
            FACES.update(status="done", people=npeople)
        _flog(f"Done. {len(vecs)} faces in {npeople} people groups.\n")
    except Exception as e:  # noqa: BLE001
        _flog(f"ERROR: {e}\n")
        with FACES_LOCK:
            FACES["status"] = "error"


def start_faces(under=None, folder=None) -> bool:
    with FACES_LOCK:
        if FACES["status"] == "running":
            return False
        FACES.update(status="running", log="", done=0, total=0)
    threading.Thread(target=_run_faces, args=(under, folder), daemon=True).start()
    return True


def faces_status() -> dict:
    with FACES_LOCK:
        st = dict(FACES)
    st["available_info"] = faces_available()
    cnt = 0
    if FACES_DB.exists():
        try:
            c = _db()
            cnt = c.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
            c.close()
        except Exception:
            pass
    st["faces"] = cnt
    return st


def people_groups(limit_people: int = 60, per_person: int = 12) -> dict:
    if not FACES_DB.exists():
        return {"people": [], "error": "No faces yet — run face detection first."}
    try:
        conn = _db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT person_id, full_path, bbox FROM faces ORDER BY person_id").fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001
        return {"people": [], "error": str(e)}
    groups = {}
    for r in rows:
        groups.setdefault(r["person_id"], []).append({"path": r["full_path"], "bbox": r["bbox"]})
    people = [{"person_id": pid, "count": len(members), "members": members[:per_person]}
              for pid, members in groups.items()]
    people.sort(key=lambda p: -p["count"])
    return {"people": people[:limit_people], "total_people": len(people),
            "note": "Grouping uses cropped-face image feature prints (Apple Vision) — "
                    "good for obvious same-person shots, not forensic identity."}
