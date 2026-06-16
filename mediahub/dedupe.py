"""Safe de-duplication plan (keep one copy per hash)."""
from .db import all_files
from .config import data_epoch

_CACHE = {}


def dedupe_plan_rows():
    """One row per file with keep/delete decision (preserve one copy/hash)."""
    ep = data_epoch()
    if _CACHE.get("ep") != ep:
        _CACHE.clear()
        _CACHE["ep"] = ep
        _CACHE["rows"] = _compute_dedupe_rows()
    return _CACHE["rows"]


def _compute_dedupe_rows():
    files = all_files()
    by_hash = {}
    for f in files:
        if not f["sha256"]:
            continue
        by_hash.setdefault(f["sha256"], []).append(f)
    # Preference for which copy to KEEP: prefer drives in this order, then shortest path
    pref = {"T7 SSD2": 0, "T7 SSD1": 1, "Past T9": 2, "Current T9": 3}
    rows = []
    for h, group in by_hash.items():
        if len(group) == 1:
            continue
        group.sort(key=lambda g: (pref.get(g["device_name"], 9), len(g["full_path"])))
        keep = group[0]
        for i, g in enumerate(group):
            rows.append({
                "decision": "KEEP" if i == 0 else "DELETE",
                "sha256": h,
                "file_name": g["file_name"],
                "full_path": g["full_path"],
                "device_name": g["device_name"],
                "file_size": g["file_size"],
                "keep_path": keep["full_path"],
            })
    return rows


# ----------------------------------------------------------------------------
# Staging engine (background job, copy-only)
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Staging engine: persistent, resumable, swap-aware (copy-only).
#
# A job is a list of items (one per unique file/hash). Each item carries ALL
# candidate source copies across drives, so the executor can satisfy a file
# from whichever drive is currently mounted (minimizing drive swaps). State is
# persisted to stage_job.json, so swaps and app restarts resume cleanly.
# ----------------------------------------------------------------------------
