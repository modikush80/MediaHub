"""Robust source-drive identity & resolution.

The inventory records each file under /Volumes/<name>/... . If a drive is later
renamed or remounts under a different path, raw string matching breaks staging.
This module resolves an indexed source drive to a *currently mounted* volume by,
in order:

  1. name match      — /Volumes/<device_name> is mounted (the common, fast case)
  2. learned UUID    — a volume whose VolumeUUID matches what we learned before
  3. content match   — a mounted volume whose top-level folders match the drive's
                       indexed top folders (recognizes a renamed drive)
  4. user choice      — if ambiguous/none, the UI asks the user to pick; we persist
                       the mapping (UUID + mount) so it's instant next time.

Nothing here mutates source media or the inventory. Learned mappings live in
DATA_DIR/drives.json. All resolution is read-only.
"""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .config import DATA_DIR, GB, VOLUMES_DIR
from .db import all_files

DRIVES_PATH = DATA_DIR / "drives.json"
_LOCK = threading.Lock()
_MATCH_STRONG = 0.5          # >= this top-folder overlap ⇒ confident rename match


# --------------------------------------------------------------------------- store
def _load() -> dict:
    if DRIVES_PATH.exists():
        try:
            return json.loads(DRIVES_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save(d: dict) -> None:
    with _LOCK:
        DRIVES_PATH.write_text(json.dumps(d, indent=2))


def _remember(device_name, mount, uuid):
    d = _load()
    rec = d.get(device_name, {})
    rec.update({"mount": mount, "uuid": uuid or rec.get("uuid")})
    d[device_name] = rec
    _save(d)


# --------------------------------------------------------------------------- volume probing
def _volume_uuid(mount_path: str):
    """Best-effort VolumeUUID via diskutil (macOS). None if unavailable."""
    try:
        r = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", mount_path],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            import plistlib
            return plistlib.loads(r.stdout).get("VolumeUUID")
    except Exception:
        pass
    return None


def _disk_info(mount_path):
    """Best-effort {VolumeUUID, FilesystemType} via diskutil (macOS)."""
    try:
        r = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", mount_path],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            import plistlib
            d = plistlib.loads(r.stdout)
            return {"uuid": d.get("VolumeUUID"), "fstype": d.get("FilesystemType")}
    except Exception:
        pass
    return {"uuid": None, "fstype": None}


def _top_names(path, limit=200):
    try:
        return sorted(e.name for e in os.scandir(path)
                      if e.is_dir() and not e.name.startswith("."))[:limit]
    except Exception:
        return []


def _mount_for(path):
    """The /Volumes/<name> that contains `path` (or '/' for the startup disk)."""
    p = Path(path).resolve()
    vroot = VOLUMES_DIR.resolve()
    try:
        rel = p.relative_to(vroot)
        name = rel.parts[0] if rel.parts else p.name
        return str(vroot / name), name
    except Exception:
        return "/", "/"


def capture_volume(path):
    """Snapshot the identity of the volume holding `path` (read-only)."""
    mount, name = _mount_for(path)
    info = _disk_info(mount) if mount and mount != "/" else _disk_info("/")
    try:
        total = shutil.disk_usage(path).total
    except Exception:
        total = None
    fp = "|".join(_top_names(mount)) if mount else ""
    return {"volume_name": name, "volume_uuid": info.get("uuid"),
            "mount_path": mount, "fstype": info.get("fstype"),
            "total_bytes": total, "root_fingerprint": fp}


def scan_root_identity():
    """Map volume_name -> latest ingest-captured identity (from scan_roots).
    Read-only; tolerates DBs that predate the table."""
    out = {}
    try:
        from .db import DB_PATH
        conn = __import__("sqlite3").connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT volume_name, volume_uuid, mount_path, fstype, root_fingerprint, scanned_at "
            "FROM scan_roots ORDER BY id").fetchall()
        conn.close()
        for vn, uuid, mount, fs, fp, ts in rows:
            out[vn] = {"volume_uuid": uuid, "mount_path": mount, "fstype": fs,
                       "root_fingerprint": fp, "scanned_at": ts}
    except Exception:
        pass
    return out


def mounted_volumes_detailed():
    out = []
    if VOLUMES_DIR.exists():
        for v in sorted(VOLUMES_DIR.iterdir()):
            if not v.is_dir():
                continue
            try:
                u = shutil.disk_usage(v)
                out.append({"name": v.name, "mount": str(v), "uuid": _volume_uuid(str(v)),
                            "total_gb": round(u.total / GB, 1), "free_gb": round(u.free / GB, 1),
                            "top": _top_names(v)})
            except Exception:
                out.append({"name": v.name, "mount": str(v), "uuid": None,
                            "total_gb": None, "free_gb": None, "top": []})
    return out


# --------------------------------------------------------------------------- inventory side
def inventory_drives():
    """device_name -> {device_name, root_path, top:set(top_folders), files, gb}."""
    drv = {}
    for f in all_files():
        dn = f["device_name"]
        d = drv.setdefault(dn, {"device_name": dn, "root_path": f["root_path"] or "",
                                "top": set(), "files": 0, "bytes": 0})
        if f["top_folder"]:
            d["top"].add(f["top_folder"])
        d["files"] += 1
        d["bytes"] += f["file_size"]
    return drv


def _fingerprint_match(expected_top, vols):
    exp = set(expected_top)
    best, best_score = None, 0.0
    for v in vols:
        vt = set(v.get("top") or [])
        if not exp or not vt:
            continue
        inter = len(exp & vt)
        if inter == 0:
            continue
        score = inter / len(exp)
        if score > best_score:
            best, best_score = v, score
    return best, best_score


# --------------------------------------------------------------------------- resolution
def resolve_one(device_name, inv=None, vols=None, roots=None) -> dict:
    """Resolve one indexed drive to a current mount. Returns a status dict and
    persists confident matches. Never blocks; pure read."""
    inv = inv or inventory_drives()
    vols = vols if vols is not None else mounted_volumes_detailed()
    roots = roots if roots is not None else scan_root_identity()
    rec = inv.get(device_name, {})
    root = rec.get("root_path") or f"{VOLUMES_DIR}/{device_name}"

    # The startup volume ("/" device) is always present; no remap.
    if device_name in ("/", "") or root in ("/", ""):
        return {"device_name": device_name, "status": "matched", "via": "local",
                "mount": "/", "root_path": "/", "expected_name": device_name}

    expected_mount = f"{VOLUMES_DIR}/{device_name}"
    learned = _load().get(device_name, {})

    # 1. name match
    if Path(expected_mount).exists():
        _remember(device_name, expected_mount, _volume_uuid(expected_mount))
        return _ok(device_name, expected_mount, "name", root, vols)

    # 1b. ingest-captured UUID (authoritative — recorded at scan time)
    ingest_uuid = (roots.get(device_name) or {}).get("volume_uuid")
    if ingest_uuid:
        for v in vols:
            if v.get("uuid") and v["uuid"] == ingest_uuid:
                _remember(device_name, v["mount"], v["uuid"])
                return _ok(device_name, v["mount"], "uuid-ingest", root, vols, renamed=True)

    # 2. learned UUID
    if learned.get("uuid"):
        for v in vols:
            if v.get("uuid") and v["uuid"] == learned["uuid"]:
                _remember(device_name, v["mount"], v["uuid"])
                return _ok(device_name, v["mount"], "uuid", root, vols, renamed=True)

    # 3. learned mount path still valid
    if learned.get("mount") and Path(learned["mount"]).exists():
        return _ok(device_name, learned["mount"], "learned", root, vols, renamed=True)

    # 4. content fingerprint (prefer the ingest-captured fingerprint when present)
    expected_top = rec.get("top", set())
    ing_fp = (roots.get(device_name) or {}).get("root_fingerprint")
    if ing_fp:
        expected_top = set(ing_fp.split("|")) | set(expected_top)
    best, score = _fingerprint_match(expected_top, vols)
    if best and score >= _MATCH_STRONG:
        _remember(device_name, best["mount"], best.get("uuid"))
        return _ok(device_name, best["mount"], "fingerprint", root, vols,
                   renamed=True, confidence=round(score, 2))
    if best and score > 0:
        return {"device_name": device_name, "status": "ambiguous", "via": "fingerprint",
                "expected_name": device_name, "root_path": root, "mount": None,
                "suggestion": best["mount"], "confidence": round(score, 2),
                "candidates": [v["mount"] for v in vols]}

    # 5. nothing
    return {"device_name": device_name, "status": "absent", "expected_name": device_name,
            "root_path": root, "mount": None, "candidates": [v["mount"] for v in vols]}


def _ok(device_name, mount, via, root, vols, renamed=False, confidence=None):
    name = Path(mount).name
    res = {"device_name": device_name, "status": "matched", "via": via,
           "expected_name": device_name, "mounted_name": name, "mount": mount,
           "root_path": root, "renamed": renamed or (name != device_name)}
    if confidence is not None:
        res["confidence"] = confidence
    return res


def identity_report():
    """Resolution status for every indexed drive (for the UI)."""
    inv = inventory_drives()
    vols = mounted_volumes_detailed()
    roots = scan_root_identity()
    out = []
    for dn, rec in sorted(inv.items(), key=lambda x: -x[1]["bytes"]):
        r = resolve_one(dn, inv, vols, roots)
        r["files"] = rec["files"]
        r["gb"] = round(rec["bytes"] / GB, 1)
        r["has_uuid"] = bool((roots.get(dn) or {}).get("volume_uuid"))
        out.append(r)
    return {"drives": out, "mounted": vols}


def resolve_all():
    """device_name -> current mount path (or None). Computed once per pass."""
    inv = inventory_drives()
    vols = mounted_volumes_detailed()
    roots = scan_root_identity()
    return {dn: resolve_one(dn, inv, vols, roots).get("mount") for dn in inv}


def set_manual(device_name, mount):
    """User explicitly maps an indexed drive to a mounted volume."""
    if not mount or not Path(mount).exists():
        return {"error": f"mount path not found: {mount}"}
    _remember(device_name, mount, _volume_uuid(mount))
    return {"device_name": device_name, "mount": mount, "status": "matched", "via": "user"}


# --------------------------------------------------------------------------- path rewrite
def rewrite_path(full_path, root_path, mount):
    """Map an indexed full_path onto the resolved current mount."""
    if not mount or not full_path:
        return full_path
    if root_path and full_path.startswith(root_path):
        return mount.rstrip("/") + full_path[len(root_path):]
    return full_path


def resolve_candidate_path(device_name, root_path, full_path, mount_map=None):
    """Return the on-disk path for a candidate right now, or None if absent.
    Fast path: the original path exists (unchanged behavior). Otherwise rewrite
    onto the resolved mount (renamed/remounted drive)."""
    if full_path and Path(full_path).exists():
        return full_path
    mount = (mount_map or {}).get(device_name)
    if mount is None and mount_map is None:
        mount = resolve_one(device_name).get("mount")
    if mount:
        rp = rewrite_path(full_path, root_path, mount)
        if rp and Path(rp).exists():
            return rp
    return None
