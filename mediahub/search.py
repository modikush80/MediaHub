"""Semantic search over the media archive.

Stores one embedding per unique image in a SEPARATE embeddings.sqlite3 (the
inventory is never modified). Embeddings come from embed/clip_embed.py via a
pluggable backend (stub = zero-dep keyword/path search; mlx = true CLIP on the
M5 Max). Query: encode the text with the same backend, cosine-rank stored
vectors, enrich hits with trip/device/filename from the inventory.
"""
import array
import json
import os
import struct
import subprocess
import threading
import time
from pathlib import Path

from .config import DATA_DIR
from .db import DB_PATH, all_files
from .classify import classify, bucket_for

EMB_DB = DATA_DIR / "embeddings.sqlite3"
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff")
RAW_EXTS = (".dng", ".arw", ".cr3", ".gpr", ".nef", ".raf", ".orf", ".rw2")
# Lower = preferred representative for a shot (a viewable JPEG beats a RAW).
_REP_PRIO = {".jpg": 0, ".jpeg": 0, ".heic": 0, ".png": 1, ".tif": 2, ".tiff": 2,
             ".dng": 5, ".arw": 5, ".cr3": 5, ".gpr": 5, ".nef": 5, ".raf": 5,
             ".orf": 5, ".rw2": 5}

EMBED_LOCK = threading.Lock()
EMBED = {"status": "idle", "log": "", "started_at": None, "done": 0, "total": 0}

try:
    import numpy as _np
except Exception:
    _np = None


def _get_np():
    """Return numpy if available, re-attempting import so a runtime `pip install
    numpy` is picked up without restarting the server."""
    global _np
    if _np is None:
        try:
            try:
                from . import deps as _deps
                _deps.activate_site()
            except Exception:
                pass
            import numpy as _n
            _np = _n
        except Exception:
            _np = None
    return _np


def _runtime_py() -> str:
    """Python interpreter for embed subprocesses — MediaHub's private venv if it
    exists (so installed extras like mlx-clip are importable), else 'python3'."""
    try:
        from . import deps as _deps
        return _deps.runtime_python()
    except Exception:
        return "python3"

_MATRIX = {"sig": None, "ids": [], "paths": [], "mat": None, "backend": "stub", "dim": 0}


# --------------------------------------------------------------------------- helpers
def _embed_script() -> Path | None:
    here = Path(__file__).resolve().parent
    for c in (here.parent / "embed" / "clip_embed.py",
              Path.home() / "Desktop" / "MediaHub" / "embed" / "clip_embed.py"):
        if c.exists():
            return c
    return None


def _backend() -> str:
    return os.environ.get("MEDIAHUB_EMBED_BACKEND", "stub")


def _connect():
    conn = __import__("sqlite3").connect(EMB_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS embeddings(
        file_id INTEGER PRIMARY KEY, full_path TEXT, vec BLOB, dim INTEGER)""")
    conn.execute("CREATE TABLE IF NOT EXISTS embed_meta(key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _meta_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM embed_meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def candidate_images():
    """One viewable representative per shot (folder + filename stem). Prefers a
    JPEG/HEIC when present, otherwise keeps the RAW (decoded at embed time), so
    RAW-only shots are still searchable. Collapses RAW+JPEG pairs to one."""
    best = {}
    for f in all_files():
        ext = (f["extension"] or "").lower()
        if ext not in _REP_PRIO:
            continue
        fn = f.get("file_name") or ""
        if fn.startswith("._"):
            continue
        d = os.path.dirname(f["full_path"] or "")
        stem = Path(fn).stem.lower()
        key = (d, stem)
        cur = best.get(key)
        if cur is None or _REP_PRIO[ext] < cur[2]:
            best[key] = (f["id"], f["full_path"], _REP_PRIO[ext])
    return [(fid, p) for fid, p, _ in best.values()]


# --------------------------------------------------------------------------- embed job
def _elog(msg):
    with EMBED_LOCK:
        EMBED["log"] = (EMBED["log"] + msg)[-10000:]


def folder_candidate_images(folder):
    """One representative image per shot by walking an arbitrary folder
    directly (independent of the inventory) — for direct-folder embedding."""
    import hashlib
    best = {}
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if fn.startswith("._"):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _REP_PRIO:
                continue
            p = os.path.join(root, fn)
            stem = Path(fn).stem.lower()
            key = (root, stem)
            cur = best.get(key)
            if cur is None or _REP_PRIO[ext] < cur[2]:
                fid = int(hashlib.sha1(p.encode()).hexdigest()[:15], 16)
                best[key] = (fid, p, _REP_PRIO[ext])
    return [(fid, p) for fid, p, _ in best.values()]


def _run_embed(backend, under=None, folder=None):
    script = _embed_script()
    if not script:
        with EMBED_LOCK:
            EMBED.update(status="error", log="embed/clip_embed.py not found\n")
        return
    if folder:
        cands = folder_candidate_images(folder)
    else:
        cands = candidate_images()
        if under:
            u = under.rstrip("/") + "/"
            cands = [(fid, p) for fid, p in cands if (p or "").startswith(u)]
    skipped_absent = 0
    if backend == "mlx":
        # CLIP reads real pixels — only embed files present on disk
        # (mounted source drives or local staged copies).
        present = [(fid, p) for fid, p in cands if os.path.exists(p)]
        skipped_absent = len(cands) - len(present)
        cands = present
    with EMBED_LOCK:
        EMBED.update(status="running", log="", started_at=time.time(),
                     done=0, total=len(cands))
    note = f" ({skipped_absent} not mounted — skipped)" if skipped_absent else ""
    scope = f"\nScope: {folder} (direct)" if folder else (f"\nScope: {under}" if under else "")
    _elog(f"Backend: {backend}{scope}\nImages to embed: {len(cands)}{note}\n")
    if not cands:
        with EMBED_LOCK:
            EMBED["status"] = "done"
        _elog("Nothing to embed. Pick a folder with images (use 'Embed folder "
              "directly'), or mount the source drives for inventory embedding.\n")
        return
    by_path = {p: fid for fid, p in cands}
    try:
        conn = _connect()
        conn.execute("INSERT OR REPLACE INTO embed_meta VALUES('backend',?)", (backend,))
        proc = subprocess.Popen(
            [_runtime_py(), str(script), "--stdin-paths", "--backend", backend],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            env=dict(os.environ, MEDIAHUB_EMBED_BACKEND=backend))

        def feed():
            for _fid, p in cands:
                proc.stdin.write(p + "\n")
            proc.stdin.close()
        threading.Thread(target=feed, daemon=True).start()

        done = 0
        batch = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "vec" not in r:
                continue
            fid = by_path.get(r["path"])
            if fid is None:
                continue
            vec = array.array("f", r["vec"])
            batch.append((fid, r["path"], vec.tobytes(), len(vec)))
            done += 1
            if len(batch) >= 500:
                conn.executemany("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?)", batch)
                conn.commit(); batch = []
                with EMBED_LOCK:
                    EMBED["done"] = done
                _elog(f"  embedded {done}/{len(cands)}\n")
        if batch:
            conn.executemany("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?)", batch)
        conn.execute("INSERT OR REPLACE INTO embed_meta VALUES('count',?)", (str(done),))
        conn.commit(); conn.close()
        proc.wait()
        with EMBED_LOCK:
            EMBED.update(status="done", done=done)
        _elog(f"Done. {done} embeddings stored.\n")
    except Exception as e:
        _elog(f"ERROR: {e}\n")
        with EMBED_LOCK:
            EMBED["status"] = "error"


def start_embed(backend=None, under=None, folder=None) -> bool:
    with EMBED_LOCK:
        if EMBED["status"] == "running":
            return False
        EMBED.update(status="running", log="", done=0, total=0, started_at=time.time())
    threading.Thread(target=_run_embed, args=(backend or _backend(), under, folder),
                     daemon=True).start()
    return True


def embed_status() -> dict:
    with EMBED_LOCK:
        st = dict(EMBED)
    embedded, backend, dim = 0, _backend(), 0
    if EMB_DB.exists():
        try:
            conn = _connect()
            embedded = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            backend = _meta_get(conn, "backend", backend)
            row = conn.execute("SELECT dim FROM embeddings LIMIT 1").fetchone()
            dim = row[0] if row else 0
            conn.close()
        except Exception:
            pass
    st["embedded"] = embedded
    st["backend"] = backend
    st["dim"] = dim
    st["candidates"] = len(candidate_images())
    st["numpy"] = _get_np() is not None
    return st


# --------------------------------------------------------------------------- search
def _load_matrix():
    if not EMB_DB.exists():
        return None
    sig = (EMB_DB.stat().st_mtime, EMB_DB.stat().st_size)
    if _MATRIX["sig"] == sig and _MATRIX["mat"] is not None:
        return _MATRIX
    conn = _connect()
    backend = _meta_get(conn, "backend", "stub")
    rows = conn.execute("SELECT file_id, full_path, vec, dim FROM embeddings").fetchall()
    conn.close()
    ids, paths, vecs, dim = [], [], [], 0
    for fid, path, blob, d in rows:
        ids.append(fid); paths.append(path); dim = d
        vecs.append(array.array("f", blob))
    _np_ = _get_np()
    if _np_ is not None and vecs:
        mat = _np_.array(vecs, dtype=_np_.float32)
    else:
        mat = vecs
    _MATRIX.update(sig=sig, ids=ids, paths=paths, mat=mat, backend=backend, dim=dim)
    return _MATRIX


def _encode_query(text, backend):
    script = _embed_script()
    if not script:
        return None
    r = subprocess.run([_runtime_py(), str(script), "--text", text, "--backend", backend],
                       capture_output=True, text=True,
                       env=dict(os.environ, MEDIAHUB_EMBED_BACKEND=backend))
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip())["vec"]
    except Exception:
        return None


def _meta_map():
    m = {}
    for f in all_files():
        trip = classify(f["top_folder"])[0]
        m[f["id"]] = {"file_name": f["file_name"], "device": f.get("device"),
                      "trip": trip, "full_path": f["full_path"],
                      "orientation": f.get("orientation")}
    return m


def search(query: str, k: int = 30, filters: dict = None) -> dict:
    M = _load_matrix()
    if not M or not M["ids"]:
        return {"results": [], "embedded": 0, "backend": _backend(),
                "error": "No embeddings yet — click Build embeddings first."}
    backend = M["backend"]
    qv = _encode_query(query, backend)
    if qv is None:
        return {"results": [], "backend": backend,
                "error": f"Could not encode query with backend '{backend}'."}
    meta = _meta_map()
    # Optional structured filters (device/orientation) restrict the candidate set
    # BEFORE ranking so recall is preserved within the filtered subset.
    allowed = None
    filters = filters or {}
    f_dev, f_or = filters.get("device"), filters.get("orientation")
    if f_dev or f_or:
        allowed = set()
        for i, fid in enumerate(M["ids"]):
            info = meta.get(fid, {})
            if f_dev and info.get("device") != f_dev:
                continue
            if f_or and info.get("orientation") != f_or:
                continue
            allowed.add(i)
    _np_ = _get_np()
    if _np_ is not None:
        q = _np_.array(qv, dtype=_np_.float32)
        scores = M["mat"] @ q                      # vectors are pre-normalized
        if allowed is not None:
            masked = _np_.full(len(scores), -1.0, dtype=_np_.float32)
            if allowed:
                idxs = list(allowed)
                masked[idxs] = scores[idxs]
            scores = masked
        order = scores.argsort()[::-1][:k]
        ranked = [(int(i), float(scores[i])) for i in order]
    else:
        pairs = [(_dot(v, qv), idx) for idx, v in enumerate(M["mat"])
                 if allowed is None or idx in allowed]
        pairs.sort(reverse=True)
        ranked = [(idx, sc) for sc, idx in pairs[:k]]
    # Relevance floor: the stub backend scores exactly 0 for queries that share no
    # tokens with a file's path, so without this it would dump every embedded image.
    # CLIP (mlx) cosine for a genuine visual match is well above this floor.
    floor = 0.18 if backend == "mlx" else 0.04
    ranked = [(idx, sc) for idx, sc in ranked if sc > floor]
    out = []
    for idx, score in ranked:
        fid = M["ids"][idx]
        info = meta.get(fid, {})
        out.append({
            "score": round(score, 4),
            "file_name": info.get("file_name") or os.path.basename(M["paths"][idx]),
            "trip": info.get("trip"), "device": info.get("device"),
            "path": info.get("full_path") or M["paths"][idx],
        })
    msg = None
    if not out:
        msg = (f"No strong matches for “{query}”. "
               + ("This backend matches filenames/paths — enable MLX visual search to match image content."
                  if backend != "mlx" else
                  "Try different words, or embed more of your archive."))
    return {"results": out, "embedded": len(M["ids"]), "backend": backend,
            "query": query, "filters": filters, "note": msg}


def _dot(a, b):
    s = 0.0
    for i in range(len(a)):
        s += a[i] * b[i]
    return s
