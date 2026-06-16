"""Swap-aware, resumable staging engine (copy-only) + layout rules."""
import hashlib
import json
import os
import shutil
import threading
import time
from collections import Counter
from pathlib import Path

from .config import GB, DATA_DIR, MANIFESTS_DIR
from .db import all_files
from .classify import bucket_for, classify_stage, slugify, classify
from .settings import dest_base, load_overrides
from .trips import build_trips
from . import drives
from . import applog

# Regenerable / disposable derivatives: dropped during staging.
PROXY_SKIP_EXT = {".thm", ".lrf", ".lrv", ".bin", ".int", ".bdm"}
# Functional sidecars that MUST sit beside their parent media.
KEEP_WITH_PARENT_EXT = {".xmp", ".aae", ".srt"}


def _parent_media_index(files):
    """Map (source_dir, stem) -> representative media file, used to drop a
    sidecar into the SAME leaf folder as the file it belongs to. Raw originals
    are preferred as the anchor when several share a stem (e.g. ARW + JPG)."""
    idx = {}
    rank = {"raw": 0, "photos": 1, "videos": 2}
    for f in files:
        fn = f.get("file_name") or ""
        if fn.startswith("._") or fn == ".DS_Store":
            continue
        mt = bucket_for(f["extension"])
        if mt not in ("raw", "photos", "videos"):
            continue
        d = os.path.dirname(f["full_path"] or "")
        stem = Path(f["file_name"] or "").stem.lower()
        k = (d, stem)
        cur = idx.get(k)
        if cur is None or rank.get(mt, 9) < rank.get(bucket_for(cur["extension"]), 9):
            idx[k] = f
    return idx


def make_sidecar_resolver(files):
    """Return resolve(sidecar) -> parent media leaf Path (or None if orphan)."""
    idx = _parent_media_index(files)

    def resolve(sc):
        name = sc["file_name"] or ""
        ext = (sc["extension"] or "").lower()
        base = name[:-len(ext)] if ext and name.lower().endswith(ext) else name
        stem = Path(base).stem.lower()        # also strips a media ext: DSC1.ARW.xmp
        d = os.path.dirname(sc["full_path"] or "")
        parent = idx.get((d, stem))
        return stage_subdir(parent) if parent else None

    return resolve


# ----------------------------------------------------------------------------
# Capture-source (device) detection.
# Priority: EXIF make/model -> extension -> filename -> aerial-altitude hint.


STAGE_STATE_PATH = DATA_DIR / "stage_job.json"
STAGE_LOCK = threading.Lock()
STAGE_THREAD = None


def _load_job():
    if STAGE_STATE_PATH.exists():
        try:
            return json.loads(STAGE_STATE_PATH.read_text())
        except Exception:
            return None
    return None


def _save_job(job):
    tmp = STAGE_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(job))
    tmp.replace(STAGE_STATE_PATH)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_stage(trip_label, dry_run, verify_hash):
    """Build a persistent job: one item per unique hash, with all source
    candidates across drives and a fixed destination path."""
    overrides = load_overrides()
    files = all_files()
    groups = {}
    for f in files:
        key = f"{f['device_name']}::{f['top_folder']}"
        label = (overrides.get(key, {}).get("trip")
                 or classify(f["top_folder"])[0])
        if label != trip_label:
            continue
        h = f["sha256"] or f"__nh{f['id']}"
        groups.setdefault(h, []).append(f)

    trips = {t["trip"]: t for t in build_trips()}
    info = trips.get(trip_label, {})
    year = info.get("year") or "Undated"
    year_month = info.get("year_month") or ""
    place = info.get("place") or slugify(trip_label)
    dest_root = dest_base()
    for part in (place, year, year_month):
        if part:
            dest_root = dest_root / part

    resolver = make_sidecar_resolver(files)
    items = []
    seen_names = {}
    for h, grp in groups.items():
        rep = grp[0]
        sub = stage_subdir(rep, resolver)
        if sub is None:
            continue                       # disposable proxy -> never staged
        rel = str(sub)
        base = rep["file_name"]
        nm_key = (rel, base.lower())
        if nm_key in seen_names and seen_names[nm_key] != h:
            stem = Path(base).stem
            suf = Path(base).suffix
            base = f"{stem}__{h[:8]}{suf}"
        seen_names[nm_key] = h
        cands = [{"device": g["device_name"], "path": g["full_path"],
                  "root": g.get("root_path") or ""} for g in grp]
        mt = bucket_for(rep["extension"])
        items.append({
            "sha256": h, "dest_rel": rel, "dest_name": base,
            "size": rep["file_size"], "device": rep["device"],
            "stage": rep["stage"], "candidates": cands,
            "orig_path": rep["full_path"], "trip": trip_label,
            "bucket": mt, "orientation": rep.get("orientation"),
            "sidecar_of": (Path(rep["file_name"]).stem if mt == "sidecar" else None),
            "state": "pending", "used_path": None, "error": None,
            "copied_at": None, "verified_sha": None, "verify_status": None,
        })
    return {
        "trip": trip_label, "dest_root": str(dest_root),
        "verify_hash": bool(verify_hash), "dry_run": bool(dry_run),
        "status": "running", "awaiting_drive": None,
        "message": "Planning…", "current": "",
        "started_at": time.time(), "updated_at": time.time(),
        "items": items,
    }


_MANIFEST_COLS = [
    "trip", "dest_name", "dest_subfolder", "device", "stage", "bucket",
    "orientation", "sidecar_of", "size_bytes", "sha256_inventory",
    "sha256_verified", "copy_status", "verify_status", "original_path",
    "resolved_source_path", "destination_path", "copied_at", "error",
]


def _manifest_rows(job):
    dest_root = job["dest_root"]
    rows = []
    for it in job["items"]:
        dest_path = f"{dest_root}/{it['dest_rel']}/{it['dest_name']}"
        rows.append({
            "trip": it.get("trip") or job.get("trip"),
            "dest_name": it["dest_name"],
            "dest_subfolder": it["dest_rel"],
            "device": it["device"],
            "stage": it["stage"],
            "bucket": it.get("bucket"),
            "orientation": it.get("orientation"),
            "sidecar_of": it.get("sidecar_of") or "",
            "size_bytes": it["size"],
            "sha256_inventory": it["sha256"],
            "sha256_verified": it.get("verified_sha") or "",
            "copy_status": it["state"],
            "verify_status": it.get("verify_status") or "",
            "original_path": it.get("orig_path") or "",
            "resolved_source_path": it.get("used_path") or "",
            "destination_path": dest_path,
            "copied_at": it.get("copied_at") or "",
            "error": it.get("error") or "",
        })
    return rows


def _summary(job):
    items = job["items"]
    def n(state): return sum(1 for i in items if i["state"] == state)
    verified_bytes = sum(i["size"] for i in items if i["state"] == "verified")
    return {
        "trip": job.get("trip"),
        "dest_root": job["dest_root"],
        "dry_run": bool(job.get("dry_run")),
        "verify_hash": bool(job.get("verify_hash")),
        "status": job.get("status"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "total_files": len(items),
        "verified_files": n("verified"),
        "pending_files": n("pending"),
        "skipped_files": n("skipped"),
        "error_files": n("error"),
        "total_bytes": sum(i["size"] for i in items),
        "verified_bytes": verified_bytes,
        "verified_gb": round(verified_bytes / GB, 2),
        "manifest_version": 2,
    }


def _csv_cell(v):
    s = "" if v is None else str(v)
    return '"' + s.replace('"', "'") + '"'


def _write_manifest(job):
    """Emit the full audit trail next to the staged trip. Never deletes anything."""
    try:
        dest_root = Path(job["dest_root"])
        dest_root.mkdir(parents=True, exist_ok=True)
        rows = _manifest_rows(job)

        with (dest_root / "_MediaHub_manifest.csv").open("w") as fh:
            fh.write(",".join(_MANIFEST_COLS) + "\n")
            for r in rows:
                fh.write(",".join(_csv_cell(r[c]) for c in _MANIFEST_COLS) + "\n")

        (dest_root / "_MediaHub_manifest.json").write_text(
            json.dumps({"summary": _summary(job), "files": rows}, indent=2))

        err_rows = [r for r in rows if r["copy_status"] == "error"]
        with (dest_root / "_MediaHub_errors.csv").open("w") as fh:
            fh.write(",".join(_MANIFEST_COLS) + "\n")
            for r in err_rows:
                fh.write(",".join(_csv_cell(r[c]) for c in _MANIFEST_COLS) + "\n")

        (dest_root / "_MediaHub_stage_summary.json").write_text(
            json.dumps(_summary(job), indent=2))
    except Exception:
        pass


def stage_manifest():
    """Live manifest (summary + rows) for the current job — for the UI/audit."""
    job = _load_job()
    if not job:
        return {"summary": None, "files": []}
    return {"summary": _summary(job), "files": _manifest_rows(job)}


def _auto_embed_after_stage(job):
    """Best-effort: embed the just-staged destination folder so semantic search /
    culling stay current automatically. Non-blocking; never raises into staging."""
    try:
        from . import search
        paths = [it.get("destination_path") for it in job.get("items", [])
                 if it.get("destination_path")]
        if not paths:
            return
        folder = os.path.commonpath(paths)
        if folder and not os.path.isdir(folder):
            folder = os.path.dirname(folder)
        if folder and os.path.isdir(folder):
            if search.start_embed(folder=folder):
                applog.log(f"AUTO-EMBED: queued embeddings for staged folder {folder}")
    except Exception as e:  # noqa: BLE001
        applog.log(f"AUTO-EMBED skipped: {e}")


def _archive_manifest(job):
    """Copy a completed job's manifest set into DATA_DIR/manifests/<trip>_<ts>/
    so it stays auditable from the UI even after the destination is moved."""
    import shutil as _sh
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(job.get("started_at") or time.time()))
    jobdir = MANIFESTS_DIR / f"{slugify(job.get('trip') or 'job')}_{ts}"
    try:
        jobdir.mkdir(parents=True, exist_ok=True)
        src = Path(job["dest_root"])
        for name in ("_MediaHub_manifest.csv", "_MediaHub_manifest.json",
                     "_MediaHub_errors.csv", "_MediaHub_stage_summary.json"):
            f = src / name
            if f.exists():
                _sh.copy2(f, jobdir / name)
    except Exception:
        pass


def list_archived_manifests():
    out = []
    if MANIFESTS_DIR.exists():
        for d in sorted(MANIFESTS_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            s = {}
            sf = d / "_MediaHub_stage_summary.json"
            if sf.exists():
                try:
                    s = json.loads(sf.read_text())
                except Exception:
                    s = {}
            out.append({"job": d.name, "trip": s.get("trip"),
                        "verified_files": s.get("verified_files"),
                        "error_files": s.get("error_files"),
                        "verified_gb": s.get("verified_gb"),
                        "status": s.get("status"), "dest_root": s.get("dest_root")})
    return {"manifests": out}


def _finish_message(job):
    v = sum(1 for i in job["items"] if i["state"] == "verified")
    sk = sum(1 for i in job["items"] if i["state"] == "skipped")
    er = sum(1 for i in job["items"] if i["state"] == "error")
    gb = round(sum(i["size"] for i in job["items"]
                   if i["state"] == "verified") / GB, 2)
    kind = "Planned" if job.get("dry_run") else "Staged"
    return (f"{kind} {v} files ({gb} GB) to {job['dest_root']}. "
            f"Skipped {sk}, errors {er}.")


def _stage_pass():
    """One pass: copy every pending item that has a mounted source candidate.
    Then either finish or request the drive that unblocks the most files."""
    job = _load_job()
    if not job:
        return
    dest_root = Path(job["dest_root"])
    dry = job.get("dry_run")
    verify_hash = job.get("verify_hash")
    mount_map = drives.resolve_all()          # device_name -> current mount (renamed-aware)

    def _src_for(it):
        for c in it["candidates"]:
            p = drives.resolve_candidate_path(c["device"], c.get("root", ""),
                                              c["path"], mount_map)
            if p:
                return p
        return None

    # A 'copying' state persisted from a previous run means we crashed mid-copy;
    # reset it to pending so it is retried (its partial file is cleaned below).
    for it in job["items"]:
        if it["state"] == "copying":
            it["state"] = "pending"

    for it in job["items"]:
        if it["state"] in ("verified", "skipped"):
            continue
        if dry:
            # Planning only: mark verified regardless of what's mounted.
            it["used_path"] = _src_for(it) or (
                it["candidates"][0]["path"] if it["candidates"] else None)
            it["state"] = "verified"
            _save_job(job)
            continue
        src = _src_for(it)
        if not src:
            continue  # all candidate drives absent -> stays pending
        dest = dest_root / it["dest_rel"] / it["dest_name"]
        partial = dest.with_name(dest.name + ".mediahub-partial")
        do_hash = verify_hash and not it["sha256"].startswith("__nh")
        try:
            # Idempotent resume: trust an existing FINAL file only if size (and
            # hash, when verifying) match — a partial never masquerades as final.
            if dest.exists() and dest.stat().st_size == it["size"] \
                    and (not do_hash or _sha256_file(dest) == it["sha256"]):
                it["state"] = "verified"
                it["used_path"] = src
                it["verify_status"] = it.get("verify_status") or (
                    "sha256-verified (resumed)" if do_hash else "size-verified (resumed)")
                it["copied_at"] = it.get("copied_at") or time.strftime("%Y-%m-%dT%H:%M:%S")
                _save_job(job)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            if partial.exists():
                partial.unlink()                 # discard a stale partial (dest-only)
            it["state"] = "copying"
            job["current"] = it["dest_name"]
            _save_job(job)

            # 1) copy source -> destination-side temp (source is never touched)
            shutil.copy2(src, partial)
            # 2) verify the temp before it can become the final file
            if partial.stat().st_size != it["size"]:
                it["verify_status"] = "size-mismatch"
                raise IOError(f"size mismatch after copy "
                              f"(expected {it['size']}, got {partial.stat().st_size})")
            if do_hash:
                actual = _sha256_file(partial)
                if actual != it["sha256"]:
                    it["verify_status"] = "sha256-mismatch"
                    raise IOError("sha256 mismatch after copy — copied file "
                                  "does not match the inventory hash")
                it["verified_sha"] = actual
                it["verify_status"] = "sha256-verified"
            else:
                it["verify_status"] = "size-verified"
            # 3) atomic promote: the final path only appears once verified
            os.replace(partial, dest)
            it["state"] = "verified"
            it["used_path"] = src
            it["copied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception as e:
            it["state"] = "error"
            it["error"] = str(e)
            try:
                if partial.exists():
                    partial.unlink()             # never leave a partial behind
            except OSError:
                pass
        _save_job(job)

    pending = [it for it in job["items"] if it["state"] == "pending"]
    job["current"] = ""
    if not pending:
        job["status"] = "done"
        job["awaiting_drive"] = None
        job["message"] = _finish_message(job)
    else:
        cnt = Counter()
        for it in pending:
            for c in it["candidates"]:
                cnt[c["device"]] += 1
        drive = cnt.most_common(1)[0][0]
        rem = [it for it in pending
               if any(c["device"] == drive for c in it["candidates"])]
        job["status"] = "awaiting_drive"
        job["awaiting_drive"] = drive
        job["message"] = (
            f"Insert drive '{drive}' and click Continue "
            f"({len(rem)} files, {round(sum(i['size'] for i in rem)/GB,2)} GB "
            f"remaining on it).")
    job["updated_at"] = time.time()
    _save_job(job)
    if not dry:
        _write_manifest(job)          # keep the audit trail current every pass
        if job["status"] == "done":
            _archive_manifest(job)
            _auto_embed_after_stage(job)
            applog.log(f"STAGE done: {_finish_message(job)}")


def start_stage(trip, dry_run, verify_hash):
    global STAGE_THREAD
    with STAGE_LOCK:
        if STAGE_THREAD and STAGE_THREAD.is_alive():
            return False
        job = plan_stage(trip, dry_run, verify_hash)
        _save_job(job)
        applog.log(f"STAGE start: trip='{trip}' dry_run={dry_run} verify={verify_hash} "
                   f"items={len(job['items'])} dest={job['dest_root']}")
        STAGE_THREAD = threading.Thread(target=_stage_pass, daemon=True)
        STAGE_THREAD.start()
    return True


def continue_stage():
    global STAGE_THREAD
    with STAGE_LOCK:
        job = _load_job()
        if not job or job["status"] == "done":
            return False
        if STAGE_THREAD and STAGE_THREAD.is_alive():
            return True
        job["status"] = "running"
        job["awaiting_drive"] = None
        _save_job(job)
        STAGE_THREAD = threading.Thread(target=_stage_pass, daemon=True)
        STAGE_THREAD.start()
    return True


def skip_drive(drive):
    job = _load_job()
    if not job:
        return False
    for it in job["items"]:
        if it["state"] != "pending":
            continue
        if any(c["device"] == drive for c in it["candidates"]) \
                and not any(Path(c["path"]).exists() for c in it["candidates"]):
            it["state"] = "skipped"
    _save_job(job)
    return continue_stage()


def stage_status():
    job = _load_job()
    if not job:
        return {"status": "idle"}
    items = job["items"]
    total = len(items)
    total_b = sum(i["size"] for i in items)
    verified = [i for i in items if i["state"] == "verified"]
    done_b = sum(i["size"] for i in verified)
    pending = [i for i in items if i["state"] == "pending"]
    errors = [i for i in items if i["state"] == "error"]
    skipped = [i for i in items if i["state"] == "skipped"]
    mount_map = drives.resolve_all()
    drive_rem = {}
    for it in pending:
        for d in {c["device"] for c in it["candidates"]}:
            r = drive_rem.setdefault(d, {"device": d, "files": 0, "bytes": 0})
            r["files"] += 1
            r["bytes"] += it["size"]
    for d, r in drive_rem.items():
        r["gb"] = round(r["bytes"] / GB, 2)
        r["connected"] = bool(mount_map.get(d))
        r["mount"] = mount_map.get(d)
    remaining = sorted(drive_rem.values(), key=lambda x: -x["bytes"])
    # next recommended drive = absent drive holding the most remaining bytes
    next_drive = next((r["device"] for r in remaining if not r["connected"]), None)
    return {
        "status": job["status"],
        "trip": job["trip"],
        "dest_root": job["dest_root"],
        "dry_run": job.get("dry_run"),
        "awaiting_drive": job.get("awaiting_drive"),
        "next_drive": next_drive,
        "message": job.get("message", ""),
        "current": job.get("current", ""),
        "total_files": total,
        "total_gb": round(total_b / GB, 2),
        "done_files": len(verified),
        "done_gb": round(done_b / GB, 2),
        "pending_files": len(pending),
        "skipped_files": len(skipped),
        "error_files": len(errors),
        "pct": round(100 * done_b / total_b, 1) if total_b else 0,
        "drive_remaining": remaining,
        "errors_sample": [{"file": i["dest_name"], "error": i["error"]}
                          for i in errors[:8]],
    }


def stage_errors():
    """Full list of failed files with reason + source path (for review/export)."""
    job = _load_job()
    if not job:
        return {"count": 0, "errors": []}
    errs = []
    for it in job["items"]:
        if it["state"] == "error":
            src = it.get("used_path") or (it["candidates"][0]["path"] if it["candidates"] else "")
            errs.append({"dest_name": it["dest_name"], "dest_rel": it["dest_rel"],
                         "device": it["device"], "size": it["size"],
                         "sha256": it["sha256"], "source": src,
                         "error": it.get("error")})
    return {"trip": job.get("trip"), "count": len(errs), "errors": errs}


def retry_errors():
    """Reset error-state items to pending and run another pass (re-resolving
    drives). Works even after the job reported done-with-errors."""
    global STAGE_THREAD
    with STAGE_LOCK:
        job = _load_job()
        if not job:
            return {"retried": 0}
        n = 0
        for it in job["items"]:
            if it["state"] == "error":
                it["state"] = "pending"
                it["error"] = None
                n += 1
        if n:
            job["status"] = "running"
            job["awaiting_drive"] = None
            _save_job(job)
        if STAGE_THREAD and STAGE_THREAD.is_alive():
            return {"retried": n}
        if n:
            STAGE_THREAD = threading.Thread(target=_stage_pass, daemon=True)
            STAGE_THREAD.start()
    return {"retried": n}



def stage_subdir(f, parent_resolver=None):
    """Destination sub-folder under <Location>/<Year>/<YYYY-MM>/.
    Layout:  raw|edited / images|videos / <Device> / Horizontal|Vertical
      - raw  = camera originals; edited = exports
      - images = all stills (DNG, ARW, JPEG, HEIC, ...) together; videos = clips
    Functional sidecars (.xmp/.aae/.srt) are placed in the SAME leaf as their
    parent media so editors keep auto-linking them. Disposable proxies
    (.thm/.lrf/.lrv/...) return None and are skipped. orphan sidecars -> _Sidecars."""
    ext = (f["extension"] or "").lower()
    fname = f.get("file_name") or ""
    if fname.startswith("._") or fname == ".DS_Store":
        return None                                   # macOS AppleDouble / junk
    if ext in PROXY_SKIP_EXT:
        return None                                   # regenerable junk -> drop
    mt = bucket_for(ext)                               # photos|raw|videos|sidecar|other
    stage = f.get("stage") or classify_stage(
        f["extension"], f["file_name"], False, f.get("top_folder", ""))
    if mt == "sidecar":
        if ext in KEEP_WITH_PARENT_EXT and parent_resolver is not None:
            leaf = parent_resolver(f)
            if leaf is not None:
                return leaf                           # beside its media
        return Path("_Sidecars")                      # orphan / non-paired
    stage_dir = "edited" if stage == "edited" else "raw"
    if mt in ("photos", "raw", "videos"):
        media = "videos" if mt == "videos" else "images"   # photos + raw -> images
        dev = f.get("device") or "Unknown"
        orient = f.get("orientation") or "Unknown"
        return Path(stage_dir) / media / dev / orient
    return Path(stage_dir) / "other" / (f.get("device") or "Unknown")


def staging_targets(trip_label: str):
    """Pick one canonical source file per unique hash for the given trip."""
    overrides = load_overrides()
    files = all_files()
    pref = {"T7 SSD2": 0, "T7 SSD1": 1, "Past T9": 2, "Current T9": 3}
    chosen = {}
    for f in files:
        key = f"{f['device_name']}::{f['top_folder']}"
        if key in overrides and overrides[key].get("trip"):
            label = overrides[key]["trip"]
        else:
            label = classify(f["top_folder"])[0]
        if label != trip_label:
            continue
        h = f["sha256"] or f"__nohash__{f['id']}"
        cur = chosen.get(h)
        rank = (pref.get(f["device_name"], 9), len(f["full_path"]))
        if cur is None or rank < cur[0]:
            chosen[h] = (rank, f)
    return [v[1] for v in chosen.values()]




def stage_preview(trip_label: str):
    """Folder-tree counts/sizes for a trip without copying anything."""
    targets = staging_targets(trip_label)
    resolver = make_sidecar_resolver(all_files())
    tree = {}
    staged_files = 0
    staged_bytes = 0
    for f in targets:
        sub = stage_subdir(f, resolver)
        if sub is None:
            continue                       # disposable proxy -> skipped
        rel = str(sub)
        node = tree.setdefault(rel, {"path": rel, "files": 0, "bytes": 0})
        node["files"] += 1
        node["bytes"] += f["file_size"]
        staged_files += 1
        staged_bytes += f["file_size"]
    nodes = sorted(tree.values(), key=lambda n: n["path"])
    for n in nodes:
        n["gb"] = round(n["bytes"] / GB, 2)
    return {
        "trip": trip_label,
        "total_files": staged_files,
        "total_gb": round(staged_bytes / GB, 2),
        "tree": nodes,
    }


# ----------------------------------------------------------------------------
# Ingest: a self-contained scanner that indexes a new drive/folder into the
# SAME database MediaHub reads. No third-party deps. exiftool is used only if
# present (richer device detection); without it, files are still indexed and
# device is inferred from extension/filename.
# ----------------------------------------------------------------------------
