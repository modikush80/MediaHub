"""Staging layout + safe de-duplication invariants."""
from pathlib import Path
import mediahub.dedupe as dedupe
from mediahub.staging import stage_subdir


def _f(**kw):
    base = {"file_name": "x.arw", "extension": ".arw", "stage": "original",
            "device": "Sony", "orientation": "Horizontal", "top_folder": "Trip"}
    base.update(kw)
    return base


def test_layout_raw_image_leaf():
    p = stage_subdir(_f())
    assert p == Path("raw/images/Sony/Horizontal")


def test_layout_edited_video_leaf():
    p = stage_subdir(_f(extension=".mp4", file_name="c.mp4", stage="edited",
                        device="Drone", orientation="Vertical"))
    assert p == Path("edited/videos/Drone/Vertical")


def test_layout_photos_and_raw_merge_into_images():
    jpg = stage_subdir(_f(extension=".jpg", file_name="a.jpg"))
    arw = stage_subdir(_f(extension=".arw", file_name="a.arw"))
    assert jpg.parts[1] == "images" and arw.parts[1] == "images"


def test_proxies_and_junk_are_skipped():
    assert stage_subdir(_f(extension=".thm", file_name="a.thm")) is None
    assert stage_subdir(_f(file_name="._a.arw")) is None
    assert stage_subdir(_f(file_name=".DS_Store", extension="")) is None


def test_orphan_sidecar_goes_to_sidecars():
    p = stage_subdir(_f(extension=".xmp", file_name="a.xmp"), parent_resolver=None)
    assert p == Path("_Sidecars")


# ---------------- dedupe ----------------
def _mk(fid, h, dev, path, size=100):
    return {"id": fid, "sha256": h, "device_name": dev, "full_path": path,
            "file_name": path.split("/")[-1], "file_size": size}


def test_dedupe_keeps_exactly_one_per_hash(monkeypatch):
    files = [
        _mk(1, "AAA", "T7 SSD2", "/a/1.arw"),
        _mk(2, "AAA", "Past T9", "/b/1.arw"),   # dup of 1
        _mk(3, "AAA", "T7 SSD1", "/c/1.arw"),   # dup of 1
        _mk(4, "BBB", "T7 SSD2", "/a/2.arw"),   # unique -> excluded from plan
    ]
    monkeypatch.setattr(dedupe, "all_files", lambda: files)
    rows = dedupe._compute_dedupe_rows()   # bypass epoch cache (fresh each call)
    # singleton BBB not in the plan
    assert all(r["sha256"] != "BBB" for r in rows)
    keeps = [r for r in rows if r["decision"] == "KEEP"]
    dels = [r for r in rows if r["decision"] == "DELETE"]
    assert len(keeps) == 1 and len(dels) == 2          # one kept, rest flagged
    assert keeps[0]["device_name"] == "T7 SSD2"        # preference order honored
    # every DELETE row points at the kept copy, and nothing is actually deleted
    assert all(r["keep_path"] == keeps[0]["full_path"] for r in dels)


def test_dedupe_ignores_hashless_files(monkeypatch):
    files = [dict(_mk(1, "", "T7 SSD2", "/a/1.arw")), dict(_mk(2, "", "Past T9", "/b/1.arw"))]
    for f in files:
        f["sha256"] = None
    monkeypatch.setattr(dedupe, "all_files", lambda: files)
    assert dedupe._compute_dedupe_rows() == []
