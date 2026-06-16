"""Mounted-volume discovery + drive-name mapping."""
import shutil
from pathlib import Path

from .config import GB


def device_for_path(p: str) -> str:
    parts = Path(p).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return parts[2]                      # /Volumes/<name>/...
    return "/"                               # startup volume



def mounted_volumes():
    vols = Path("/Volumes")
    out = []
    if vols.exists():
        for v in sorted(vols.iterdir()):
            try:
                usage = shutil.disk_usage(v)
                out.append({"name": v.name,
                            "free_gb": round(usage.free / GB, 1),
                            "total_gb": round(usage.total / GB, 1)})
            except Exception:
                out.append({"name": v.name, "free_gb": None, "total_gb": None})
    return out


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
