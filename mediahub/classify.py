"""Pure classifiers: media bucket, capture device, stage, orientation,
and trip-name rules. No I/O, no cross-module deps."""
import re
from pathlib import Path


BUCKETS = {
    "photos": {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"},
    "raw": {".arw", ".dng", ".cr3", ".gpr", ".nef", ".raf", ".orf", ".rw2"},
    "videos": {".mp4", ".mov", ".avi", ".m4v", ".mxf", ".insv", ".lrv", ".mts", ".m2ts"},
    "sidecar": {".xmp", ".srt", ".lrf", ".thm", ".aae", ".xml", ".insp",
                ".lrtemplate", ".bdm", ".bin", ".int", ".prproj"},
}


def bucket_for(ext: str) -> str:
    ext = (ext or "").lower()
    for name, exts in BUCKETS.items():
        if ext in exts:
            return name
    return "other"



_GOPRO_FN = re.compile(r"^(gopr|gx|gh|gp|gs)\d", re.I)


def classify_device(make, model, ext, fname, has_altitude=False) -> str:
    mk = (make or "").lower()
    md = (model or "").lower()
    e = (ext or "").lower()
    fn = (fname or "").lower()

    # Insta360 (rarely writes standard EXIF make)
    if e in (".insv", ".insp"):
        return "Insta360"
    # GoPro
    if "gopro" in mk or md.startswith("hero") or "hero" in md or e == ".gpr" \
            or _GOPRO_FN.match(fn):
        return "GoPro"
    # Drone (DJI). Hasselblad L3D-100c is the DJI Mavic 3 camera.
    if "dji" in mk or md.startswith("fc") or "l3d" in md or fn.startswith("dji_") \
            or (has_altitude and e in (".dng", ".mp4", ".mov", ".jpg")):
        return "Drone"
    # Sony
    if "sony" in mk or "ilce" in md or e == ".arw" or fn.startswith("dsc"):
        return "Sony"
    # iPhone / Apple
    if "apple" in mk or "iphone" in md or e == ".heic" \
            or (fn.startswith("img_") and e in (".heic", ".mov", ".jpg")):
        return "iPhone"
    # Canon
    if "canon" in mk:
        return "Canon"
    # Standalone Hasselblad (not the drone module)
    if "hasselblad" in mk and "l3d" not in md:
        return "Hasselblad"
    return "Unknown"


# ----------------------------------------------------------------------------
# Stage detection: camera ORIGINAL vs EDITED export vs SIDECAR.
# ----------------------------------------------------------------------------

_EDIT_FN = re.compile(
    r"(-edit|_edit|edited|export|exported|final|graded|grade|retouch|"
    r"select|pano|panorama|hdr|stitch|reframe|_lr|_ps)", re.I)
_SIDECAR_EXT = {".xmp", ".srt", ".lrf", ".thm", ".aae", ".lrtemplate", ".lrv"}


def classify_stage(ext, fname, edited_sw=False, folder="") -> str:
    e = (ext or "").lower()
    fn = fname or ""
    fl = (folder or "").lower()
    if e in _SIDECAR_EXT:
        return "sidecar"
    if e == ".prproj":
        return "edited"
    if edited_sw:
        return "edited"
    if _EDIT_FN.search(fn):
        return "edited"
    if any(k in fl for k in ("edited", "shortlist", "export", "prores", "final", "select")):
        return "edited"
    return "original"


def file_year_month(f) -> str:
    """Return 'YYYY-MM' from EXIF capture/creation date, else '' (created_time
    on this inventory is unreliable, so it is intentionally ignored)."""
    for key in ("capture_date", "creation_date"):
        v = (f.get(key) or "").strip()
        if len(v) >= 7:
            s = v[:10].replace(":", "-")   # tolerate 'YYYY:MM:DD'
            return s[:7]
    return ""


def classify_orientation(width, height, rotated=False) -> str:
    """Horizontal / Vertical using display dimensions (accounts for EXIF
    Orientation 5-8, which rotate a landscape sensor frame to portrait)."""
    try:
        w = int(width or 0)
        h = int(height or 0)
    except (TypeError, ValueError):
        return "Unknown"
    if w <= 0 or h <= 0:
        return "Unknown"
    if rotated:                      # 90/270 rotation swaps display dims
        w, h = h, w
    return "Horizontal" if w >= h else "Vertical"


# ----------------------------------------------------------------------------
# Trip classification rules.
# Each rule: (compiled keyword regex, canonical trip label, category)
# Categories: trips | events | camera-dumps | personal | unsorted
# Order matters - first match wins.

RAW_RULES = [
    (r"costa\s*rica|gunacaste|guanacaste", "Costa Rica", "trips"),
    (r"french.?polynesia|borabora|bora\s*bora|tahiti|moorea", "French Polynesia", "trips"),
    (r"iceland|puffin|northern.?light", "Iceland", "trips"),
    (r"new.?zealand|^nz\b|milford", "New Zealand", "trips"),
    (r"christmas", "Christmas 2025", "trips"),
    (r"fall.?colors?", "Fall Colors 2025", "trips"),
    (r"\bbanff\b", "Banff", "trips"),
    (r"\bfrance\b|paris", "France 2025", "trips"),
    (r"grand.?cayman", "Grand Cayman", "trips"),
    (r"los.?angeles|\bla\b", "Los Angeles 2025", "trips"),
    (r"balloon|new.?mexico", "New Mexico Balloon Fest", "trips"),
    (r"niagara", "Niagara Falls", "trips"),
    (r"havasupai", "Havasupai", "trips"),
    (r"\butah\b", "Utah", "trips"),
    (r"arizona", "Arizona", "trips"),
    (r"olympic.?national", "Olympic National Park", "trips"),
    (r"whistler", "Whistler", "trips"),
    (r"high.?steel.?bridge", "High Steel Bridge", "trips"),
    (r"rainier", "Mount Rainier", "trips"),
    (r"\boregon\b", "Oregon", "trips"),
    (r"vancouver", "Vancouver", "trips"),
    (r"seattle", "Seattle", "trips"),
    # Events
    (r"wedding", "Wedding", "events"),
    (r"engagement", "Engagement", "events"),
    # Personal / non-trip backups
    (r"office.?mac|laptop.?items|vandini|iphone.?data|t9.?backup|current.?t9",
     "Personal Backups", "personal"),
    # Camera card dumps (raw offloads to triage later)
    (r"sony.?dump|sony.?memory|samsungcard|lexar|apple.?prores|prores|"
     r"sony.?\d|sony.?camera|may.?july.?25|card.?unload|\d{8}_\d{8}|"
     r"sony\s*camera\s*15", "Camera Card Dumps", "camera-dumps"),
    (r"\bdrone\b", "Drone Footage", "camera-dumps"),
    # Unsorted
    (r"unorganized|unsorted|untitled|till\s*\d", "Unsorted", "unsorted"),
]
RULES = [(re.compile(p, re.I), label, cat) for p, label, cat in RAW_RULES]

# Best-guess YYYY-MM prefix per canonical trip (sortable on NAS).
TRIP_DATE = {
    "Costa Rica": "2024-04",
    "Iceland": "2024-09",
    "Wedding": "2024-06",
    "Engagement": "2023-10",
    "French Polynesia": "2025-01",
    "New Zealand": "2025-02",
    "Banff": "2025-06",
    "Los Angeles 2025": "2025-07",
    "France 2025": "2025-09",
    "Fall Colors 2025": "2025-10",
    "New Mexico Balloon Fest": "2025-10",
    "Christmas 2025": "2025-12",
    "Grand Cayman": "2024-08",
}


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return s or "Untitled"


def classify(top_folder: str):
    """Return (trip_label, category) for a top-level folder name."""
    name = top_folder or "(root)"
    for rx, label, cat in RULES:
        if rx.search(name):
            return label, cat
    # default: treat the folder itself as a trip
    cleaned = re.sub(r"[_]+", " ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.title() if cleaned else "(root)", "trips"


# ----------------------------------------------------------------------------
# Database access
