"""Dashboard summary + trip and capture-source aggregations."""
from collections import Counter

from .config import GB, data_epoch
from .db import connect, all_files, DB_PATH
from .classify import classify, file_year_month, slugify, TRIP_DATE
from .settings import load_overrides


def _memo(fn):
    """Memoize a zero/low-arg query against the current data epoch. The cache
    holds only the latest epoch, so results refresh after ingest/override edits
    but repeated reads (tab switches) are instant."""
    cache = {}

    def wrap(*a, **k):
        key = (data_epoch(), a, tuple(sorted(k.items())))
        if key not in cache:
            cache.clear()
            cache[key] = fn(*a, **k)
        return cache[key]

    wrap.__name__ = fn.__name__
    wrap.__doc__ = fn.__doc__
    return wrap


@_memo
def q_summary() -> dict:
    conn = connect()
    try:
        c = conn.cursor()
        total_files, total_bytes = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(file_size),0) FROM files"
        ).fetchone()
        # exact-duplicate reclaimable bytes
        row = c.execute(
            """
            WITH dups AS (
              SELECT sha256, file_size, COUNT(*) c, SUM(file_size) total
              FROM files WHERE sha256 <> '' GROUP BY sha256 HAVING c > 1)
            SELECT COUNT(*) groups, COALESCE(SUM(c),0) dup_files,
                   COALESCE(SUM(total - file_size),0) reclaim
            FROM dups
            """
        ).fetchone()
        unique_files, unique_bytes = c.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(file_size),0) FROM (
              SELECT sha256, MIN(file_size) file_size FROM files
              WHERE sha256 <> '' GROUP BY sha256)
            """
        ).fetchone()
        devices = [dict(r) for r in c.execute(
            """
            SELECT device_name,
                   COUNT(*) files,
                   ROUND(SUM(file_size)/1073741824.0, 1) gb
            FROM files GROUP BY device_name ORDER BY SUM(file_size) DESC
            """
        ).fetchall()]
        return {
            "db_path": str(DB_PATH),
            "total_files": total_files,
            "total_gb": round(total_bytes / GB, 1),
            "unique_files": unique_files,
            "unique_gb": round(unique_bytes / GB, 1),
            "dup_groups": row["groups"],
            "dup_files": row["dup_files"],
            "reclaim_gb": round(row["reclaim"] / GB, 1),
            "devices": devices,
        }
    finally:
        conn.close()



@_memo
def build_trips():
    """Group files into trips (dedup-aware) applying user overrides."""
    overrides = load_overrides()
    files = all_files()
    trips = {}  # trip_label -> aggregation
    for f in files:
        key = f"{f['device_name']}::{f['top_folder']}"
        if key in overrides:
            label = overrides[key].get("trip") or classify(f["top_folder"])[0]
            cat = overrides[key].get("category") or classify(f["top_folder"])[1]
        else:
            label, cat = classify(f["top_folder"])
        t = trips.setdefault(label, {
            "trip": label, "category": cat,
            "files": 0, "bytes": 0,
            "unique_hashes": set(), "unique_bytes": 0,
            "sources": set(), "devices": set(),
            "months": Counter(),
        })
        t["files"] += 1
        t["bytes"] += f["file_size"]
        t["devices"].add(f["device_name"])
        t["sources"].add(key)
        ym = file_year_month(f)
        if ym:
            t["months"][ym] += 1
        h = f["sha256"]
        if h and h not in t["unique_hashes"]:
            t["unique_hashes"].add(h)
            t["unique_bytes"] += f["file_size"]
    out = []
    for t in trips.values():
        # dominant capture month wins; fall back to the hardcoded hint, else Undated
        if t["months"]:
            year_month = t["months"].most_common(1)[0][0]
        else:
            year_month = TRIP_DATE.get(t["trip"], "")
        year = year_month[:4] if year_month else "Undated"
        year_month = year_month or ""
        place = slugify(t["trip"])
        nas_path = "/".join(p for p in (place, year, year_month) if p)
        out.append({
            "trip": t["trip"],
            "category": t["category"],
            "year": year,
            "year_month": year_month,
            "place": place,
            "nas_folder": nas_path,
            "date_prefix": year_month,
            "files": t["files"],
            "total_gb": round(t["bytes"] / GB, 2),
            "unique_files": len(t["unique_hashes"]),
            "unique_gb": round(t["unique_bytes"] / GB, 2),
            "dup_gb": round((t["bytes"] - t["unique_bytes"]) / GB, 2),
            "devices": sorted(t["devices"]),
        })
    out.sort(key=lambda x: x["unique_gb"], reverse=True)
    return out




@_memo
def build_sources():
    """Global capture-source breakdown (de-duplicated by hash)."""
    seen = set()
    agg = {}
    for f in all_files():
        h = f["sha256"] or f"__nh{f['id']}"
        if h in seen:
            continue
        seen.add(h)
        dev = f["device"]
        a = agg.setdefault(dev, {"device": dev, "files": 0, "bytes": 0,
                                 "originals": 0, "edited": 0})
        a["files"] += 1
        a["bytes"] += f["file_size"]
        if f["stage"] == "edited":
            a["edited"] += 1
        elif f["stage"] == "original":
            a["originals"] += 1
    out = []
    for a in agg.values():
        out.append({"device": a["device"], "files": a["files"],
                    "gb": round(a["bytes"] / GB, 1),
                    "originals": a["originals"], "edited": a["edited"]})
    out.sort(key=lambda x: x["gb"], reverse=True)
    return out


