"""On-device AI helpers built on top of the CLIP embeddings and Vision tags.

Everything here is REPORT-ONLY and safe: it reads embeddings/metadata and returns
suggestions. It never moves, renames, or deletes any file.

Features:
  - near_duplicates(): visual near-duplicate / burst clustering + best-shot pick
  - parse_query(): heuristic natural-language query -> structured filters
  - screenshot_sort_suggestions(): screenshots/receipts sitting inside photo trips
"""
import csv
import io
import os
import re

from .db import all_files
from .classify import classify
from . import search as _search


# --------------------------------------------------------------------------- meta
def _rich_meta():
    """id -> {file_name, full_path, trip, device, size, pixels, ext}"""
    m = {}
    for f in all_files():
        trip = classify(f.get("top_folder", ""))[0]
        w = f.get("image_width") or f.get("width") or 0
        h = f.get("image_height") or f.get("height") or 0
        try:
            pixels = int(w) * int(h)
        except Exception:
            pixels = 0
        m[f["id"]] = {
            "file_name": f.get("file_name"),
            "full_path": f.get("full_path"),
            "trip": trip,
            "device": f.get("device"),
            "size": int(f.get("file_size") or 0),
            "pixels": pixels,
            "ext": ("." + (f.get("extension") or "").lower().lstrip(".")),
        }
    return m


def _vec(M, i):
    return M["mat"][i]


def _info(i, M, meta):
    """Per-index info with fallbacks for directly-embedded files (not in inventory)."""
    fid = M["ids"][i]
    info = dict(meta.get(fid, {}) or {})
    path = info.get("full_path") or (M["paths"][i] if i < len(M["paths"]) else None)
    info["full_path"] = path
    if not info.get("file_name"):
        info["file_name"] = os.path.basename(path) if path else str(fid)
    if not info.get("size") and path and os.path.exists(path):
        try:
            info["size"] = os.path.getsize(path)
        except Exception:
            info["size"] = 0
    info.setdefault("size", 0)
    info.setdefault("pixels", 0)
    info.setdefault("device", None)
    if not info.get("ext"):
        info["ext"] = "." + os.path.splitext(path or "")[1].lstrip(".").lower()
    return info


def _cos(a, b):
    # vectors are pre-normalized by the embedder, so dot == cosine
    s = 0.0
    n = len(a)
    for i in range(n):
        s += a[i] * b[i]
    return s


_RAW = (".dng", ".arw", ".cr3", ".gpr", ".nef", ".raf", ".orf", ".rw2")


def _best_index(indices, infos):
    """Pick the representative ('keep') shot: most pixels, then largest file, then
    a RAW original over a derivative."""
    def score(i):
        info = infos[i]
        raw_bonus = 1 if info.get("ext") in _RAW else 0
        return (info.get("pixels", 0), info.get("size", 0), raw_bonus)
    return max(indices, key=score)


def near_duplicates(threshold: float = 0.90, max_clusters: int = 200) -> dict:
    """Group visually near-identical shots (bursts, re-shoots) within each trip.

    Comparison is blocked per-trip (near-dups are essentially always in the same
    trip/folder), which keeps it fast and avoids an N^2 blow-up across the archive.
    """
    M = _search._load_matrix()
    if not M or not M["ids"]:
        return {"clusters": [], "embedded": 0, "backend": _search._backend(),
                "error": "No embeddings yet — build embeddings first (Search tab)."}
    meta = _rich_meta()
    np = _search._get_np()

    # group embedding indices by trip
    by_trip = {}
    for i, fid in enumerate(M["ids"]):
        trip = (meta.get(fid, {}) or {}).get("trip") or "Unsorted"
        by_trip.setdefault(trip, []).append(i)

    clusters = []
    dup_files = 0
    reclaim = 0
    for trip, idxs in by_trip.items():
        if len(idxs) < 2:
            continue
        # adjacency via thresholded cosine, blocked within the trip
        groups = _cluster_indices(idxs, M, threshold, np)
        for g in groups:
            if len(g) < 2:
                continue
            infos = {i: _info(i, M, meta) for i in g}
            keep = _best_index(g, infos)
            members = []
            for i in g:
                info = infos[i]
                is_keep = (i == keep)
                if not is_keep:
                    dup_files += 1
                    reclaim += info.get("size", 0)
                members.append({
                    "file_name": info.get("file_name"),
                    "path": info.get("full_path"),
                    "device": info.get("device"),
                    "size": info.get("size", 0),
                    "pixels": info.get("pixels", 0),
                    "keep": is_keep,
                })
            members.sort(key=lambda x: (not x["keep"], -x["size"]))
            clusters.append({"trip": trip, "count": len(g), "members": members})
    clusters.sort(key=lambda c: -c["count"])
    clusters = clusters[:max_clusters]
    return {
        "clusters": clusters,
        "cluster_count": len(clusters),
        "duplicate_files": dup_files,
        "reclaimable_bytes": reclaim,
        "embedded": len(M["ids"]),
        "backend": M["backend"],
        "threshold": threshold,
        "note": ("Visual matches use the stub (filename/path) backend — enable MLX "
                 "for true pixel-based near-duplicate detection."
                 if M["backend"] != "mlx" else None),
    }


def _cluster_indices(idxs, M, threshold, np):
    """Connected-components clustering over thresholded cosine similarity."""
    n = len(idxs)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    if np is not None:
        try:
            sub = np.array([list(_vec(M, i)) for i in idxs], dtype=np.float32)
            sims = sub @ sub.T
            for a in range(n):
                row = sims[a]
                for b in range(a + 1, n):
                    if row[b] >= threshold:
                        union(a, b)
        except Exception:
            np = None
    if np is None:
        vecs = [list(_vec(M, i)) for i in idxs]
        for a in range(n):
            for b in range(a + 1, n):
                if _cos(vecs[a], vecs[b]) >= threshold:
                    union(a, b)

    comp = {}
    for a in range(n):
        comp.setdefault(find(a), []).append(idxs[a])
    return [g for g in comp.values()]


def near_duplicates_csv(threshold: float = 0.90) -> str:
    d = near_duplicates(threshold)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["cluster", "action", "keep", "file_name", "path", "device", "size_bytes", "trip"])
    for ci, c in enumerate(d.get("clusters", []), 1):
        for m in c["members"]:
            w.writerow([ci, "KEEP" if m["keep"] else "REVIEW", m["keep"],
                        m["file_name"], m["path"], m.get("device") or "",
                        m["size"], c["trip"]])
    return out.getvalue()


# --------------------------------------------------------------------------- NL query
_DEVICE_WORDS = {
    "drone": "Drone", "dji": "Drone", "aerial": "Drone",
    "gopro": "GoPro", "insta360": "Insta360", "insta": "Insta360",
    "iphone": "iPhone", "phone": "iPhone", "sony": "Sony", "camera": "Sony",
}
_ORIENT_WORDS = {"vertical": "Vertical", "portrait": "Vertical",
                 "horizontal": "Horizontal", "landscape": "Horizontal"}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def parse_query(q: str) -> dict:
    """Heuristic NL -> filters. Pulls device/orientation/year/month out of the
    query and returns the remaining words as the semantic search text. No model
    required; an Apple Foundation Models upgrade can replace this later."""
    ql = (q or "").lower()
    filters = {}
    for w, dev in _DEVICE_WORDS.items():
        if re.search(r"\b" + re.escape(w) + r"\b", ql):
            filters["device"] = dev
            break
    for w, o in _ORIENT_WORDS.items():
        if re.search(r"\b" + re.escape(w) + r"\b", ql):
            filters["orientation"] = o
            break
    ym = re.search(r"\b(19|20)\d{2}\b", ql)
    if ym:
        filters["year"] = int(ym.group(0))
    for mname, mnum in _MONTHS.items():
        if mname in ql:
            filters["month"] = mnum
            break
    # strip recognized tokens to leave the visual-content query
    strip = set(_DEVICE_WORDS) | set(_ORIENT_WORDS) | set(_MONTHS)
    residual = " ".join(t for t in re.split(r"\s+", ql)
                        if t and t not in strip and not re.fullmatch(r"(19|20)\d{2}", t))
    return {"filters": filters, "text": residual.strip() or q}


# --------------------------------------------------------------------------- screenshot sort
def screenshot_sort_suggestions(limit: int = 500) -> dict:
    """Screenshots/receipts/documents (per Vision tags) that currently sit inside
    photo trips — suggest moving them to a Documents area. Suggestion only."""
    from .config import DATA_DIR
    import sqlite3
    vdb = DATA_DIR / "vision_tags.sqlite3"
    if not vdb.exists():
        return {"suggestions": [], "error": "No Vision tags yet — run Vision tagging first."}
    rows = []
    try:
        conn = sqlite3.connect(f"file:{vdb}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT full_path, content_bucket, top_label, text_sample FROM vision_tags "
            "WHERE lower(content_bucket) IN ('documents','document','screenshot','screenshots','receipts') "
            "LIMIT ?", (limit,))
        for r in cur.fetchall():
            p = r["full_path"] or ""
            low = p.lower()
            in_docs = any(seg in low for seg in ("/screenshots", "/documents", "/receipts"))
            rows.append({"path": p, "bucket": r["content_bucket"], "label": r["top_label"],
                         "text": (r["text_sample"] or "")[:120], "already_sorted": in_docs})
        conn.close()
    except Exception as e:  # noqa: BLE001
        return {"suggestions": [], "error": str(e)}
    unsorted = [r for r in rows if not r["already_sorted"]]
    return {"suggestions": unsorted, "total": len(rows), "to_move": len(unsorted)}


# --------------------------------------------------------------------------- Immich export
_IMMICH_NOTES = """# Use this MediaHub archive as an Immich External Library

MediaHub has organized your media into a clean, human-readable tree
(Location / Year / Year-Month / orientation / raw|edited / images|videos / device).
Immich can read this folder **in place** as an *External Library* — it never moves
or copies your files, just indexes them. MediaHub stays the organizer; Immich
becomes the browse/search/share/mobile layer.

## Steps
1. In Immich: Administration -> Libraries -> Create External Library.
2. Set the import path to THIS folder (the destination shown below).
3. (Docker) make sure this path is bind-mounted into the Immich server container,
   e.g.  -v "{path}:{path}:ro"  (read-only is fine and safest).
4. Scan the library. Immich reads EXIF dates itself, so the folder layout is just
   for humans — Immich's timeline/faces/search work regardless.

## Notes
- Keep MediaHub as the source of truth for organizing + de-duplication + culling.
- Point Immich at the SAME folder read-only so the two never fight over files.
- RAW: Immich shows embedded previews; your RAW+JPEG pairs both index fine.
"""


def immich_export_notes() -> dict:
    """Write an external-library how-to into the destination folder and return its path."""
    import os as _os
    from .settings import dest_base
    base = str(dest_base())
    if not base or not _os.path.isdir(base):
        return {"error": "Set a destination folder first (Settings), then try again."}
    readme = _os.path.join(base, "IMMICH_EXTERNAL_LIBRARY.md")
    try:
        with open(readme, "w") as f:
            f.write(_IMMICH_NOTES.replace("{path}", base))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"path": base, "readme": readme}
