"""Photo captioning — turns images into searchable natural-language descriptions.

Two backends, identical storage (captions.sqlite3), mirroring the embeddings design:
  - stub  (default, zero-dep): synthesizes a caption from Vision tags + OCR text +
           filename/folder. Useful immediately and fully testable offline.
  - mlxvlm (Apple Silicon): a real vision-LLM caption per image via mlx-vlm,
           installed into MediaHub's private venv. Decodes RAW via sips first.

Report-only and isolated: writes only to captions.sqlite3, never touches media.
"""
import os
import re
import sqlite3
import threading
import time

from .config import DATA_DIR
from . import search as _search

CAP_DB = DATA_DIR / "captions.sqlite3"
VISION_DB = DATA_DIR / "vision_tags.sqlite3"

CAPTION = {"status": "idle", "log": "", "done": 0, "total": 0, "backend": "stub"}
CAP_LOCK = threading.Lock()


def _backend() -> str:
    return os.environ.get("MEDIAHUB_CAPTION_BACKEND", "stub").strip().lower()


def _clog(msg):
    with CAP_LOCK:
        CAPTION["log"] = (CAPTION["log"] + msg)[-10000:]


def _db():
    conn = sqlite3.connect(CAP_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS captions(
        file_id INTEGER PRIMARY KEY, full_path TEXT, caption TEXT,
        source TEXT, created_at TEXT)""")
    return conn


def _vision_lookup():
    """path -> (top_label, text_sample, bucket) from Vision tags, if present."""
    m = {}
    if not VISION_DB.exists():
        return m
    try:
        c = sqlite3.connect(f"file:{VISION_DB}?mode=ro", uri=True)
        for fp, lab, txt, buck in c.execute(
                "SELECT full_path, top_label, text_sample, content_bucket FROM vision_tags"):
            m[fp] = (lab, txt, buck)
        c.close()
    except Exception:
        pass
    return m


_STOP = {"img", "image", "photo", "dsc", "dji", "gopro", "screenshot", "the", "and", "of"}


def _stub_caption(path, vis):
    """Synthesize a searchable caption without a model."""
    parts = []
    lab, txt, buck = vis.get(path, (None, None, None))
    if buck:
        parts.append(buck)
    if lab:
        parts.append(lab.replace("_", " "))
    # folder context (often the place/trip)
    parent = os.path.basename(os.path.dirname(path))
    if parent:
        parts.append(parent.replace("_", " ").replace("-", " "))
    # filename tokens
    stem = os.path.splitext(os.path.basename(path))[0]
    toks = [t for t in re.split(r"[\s_\-.]+", stem.lower()) if t and not t.isdigit() and t not in _STOP]
    parts.extend(toks[:6])
    if txt:
        parts.append(txt[:80])
    cap = ", ".join(p for p in parts if p)
    return cap or os.path.basename(path)


def _caption_targets(under, folder):
    if folder:
        cands = _search.folder_candidate_images(folder)
    else:
        cands = _search.candidate_images()
    out = []
    for fid, p in cands:
        if not p:
            continue
        if folder is None and under and under not in p:
            continue
        if os.path.exists(p):
            out.append((fid, p))
    return out


def _run_captions(backend, under=None, folder=None):
    try:
        targets = _caption_targets(under, folder)
        with CAP_LOCK:
            CAPTION.update(total=len(targets), done=0, backend=backend)
        _clog(f"Captioning {len(targets)} images (backend: {backend}, scope: {folder or under or 'all'})\n")
        if not targets:
            _clog("Nothing to caption.\n")
            with CAP_LOCK:
                CAPTION["status"] = "done"
            return
        conn = _db()
        if backend == "mlxvlm":
            _run_mlxvlm(targets, conn)
        else:
            vis = _vision_lookup()
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            batch, done = [], 0
            for fid, p in targets:
                batch.append((fid, p, _stub_caption(p, vis), "stub", now))
                done += 1
                if len(batch) >= 500:
                    conn.executemany("INSERT OR REPLACE INTO captions VALUES(?,?,?,?,?)", batch)
                    conn.commit(); batch = []
                    with CAP_LOCK:
                        CAPTION["done"] = done
            if batch:
                conn.executemany("INSERT OR REPLACE INTO captions VALUES(?,?,?,?,?)", batch)
            conn.commit()
            with CAP_LOCK:
                CAPTION["done"] = done
        conn.close()
        with CAP_LOCK:
            CAPTION["status"] = "done"
        _clog("Done.\n")
    except Exception as e:  # noqa: BLE001
        _clog(f"ERROR: {e}\n")
        with CAP_LOCK:
            CAPTION["status"] = "error"


def _run_mlxvlm(targets, conn):
    """Real captions via mlx-vlm in the private venv. One subprocess per image
    (RAW decoded to a temp JPEG first). Falls back to error if the model is missing."""
    import subprocess
    import tempfile
    from . import deps
    py = deps.runtime_python()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    model = os.environ.get("MEDIAHUB_VLM_MODEL", "mlx-community/Qwen2-VL-2B-Instruct-4bit")
    done = 0
    for fid, p in targets:
        img = p
        tmp = None
        ext = os.path.splitext(p)[1].lower()
        if ext in (".arw", ".dng", ".cr3", ".gpr", ".nef", ".raf", ".orf", ".rw2"):
            tmp = tempfile.mktemp(suffix=".jpg")
            subprocess.run(["sips", "-s", "format", "jpeg", p, "--out", tmp],
                           capture_output=True, timeout=60)
            img = tmp
        try:
            r = subprocess.run(
                [py, "-m", "mlx_vlm.generate", "--model", model, "--max-tokens", "60",
                 "--temp", "0.0", "--image", img,
                 "--prompt", "Describe this photo in one concise sentence."],
                capture_output=True, text=True, timeout=180)
            cap = (r.stdout or "").strip().splitlines()[-1] if r.returncode == 0 else ""
        except Exception as e:  # noqa: BLE001
            cap = ""
            _clog(f"  vlm error on {os.path.basename(p)}: {e}\n")
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        if cap:
            conn.execute("INSERT OR REPLACE INTO captions VALUES(?,?,?,?,?)",
                         (fid, p, cap, "mlxvlm", now))
            conn.commit()
        done += 1
        with CAP_LOCK:
            CAPTION["done"] = done
        if done % 10 == 0:
            _clog(f"  captioned {done}/{len(targets)}\n")


def start_captions(backend=None, under=None, folder=None) -> bool:
    with CAP_LOCK:
        if CAPTION["status"] == "running":
            return False
        CAPTION.update(status="running", log="", done=0, total=0)
    threading.Thread(target=_run_captions,
                     args=(backend or _backend(), under, folder), daemon=True).start()
    return True


def caption_status() -> dict:
    with CAP_LOCK:
        st = dict(CAPTION)
    cnt = 0
    if CAP_DB.exists():
        try:
            c = _db()
            cnt = c.execute("SELECT COUNT(*) FROM captions").fetchone()[0]
            c.close()
        except Exception:
            pass
    st["captioned"] = cnt
    st["backend"] = _backend()
    return st


def caption_search(q: str, limit: int = 60) -> dict:
    if not CAP_DB.exists():
        return {"results": [], "error": "No captions yet — build captions first."}
    toks = [t for t in re.split(r"\s+", (q or "").lower().strip()) if t]
    if not toks:
        return {"results": [], "error": "Empty query."}
    try:
        conn = _db()
        conn.row_factory = sqlite3.Row
        where = " AND ".join(["lower(caption) LIKE ?"] * len(toks))
        rows = conn.execute(
            f"SELECT full_path, caption, source FROM captions WHERE {where} LIMIT ?",
            [f"%{t}%" for t in toks] + [limit]).fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001
        return {"results": [], "error": str(e)}
    out = [{"path": r["full_path"], "caption": r["caption"], "source": r["source"]}
           for r in rows]
    return {"results": out, "matched_tokens": toks,
            "note": ("Captions are heuristic (stub) — enable MLX-VLM for true AI descriptions."
                     if out and out[0]["source"] == "stub" else None)}
