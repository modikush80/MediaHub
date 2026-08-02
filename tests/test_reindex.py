"""Reconcile / Trash safety — the code that edits the inventory DB.

These lock in the invariants that protect a real archive:
  • a file is only "deleted" if its parent folder exists on a mounted drive
  • never prune when nothing resolves on this machine (wrong-Mac inventory)
  • prune is a reversible soft-delete; purge is the only hard delete
"""
import os
import sqlite3
import tempfile
from pathlib import Path

import mediahub.reindex as reindex


def _tmp_inventory(rows):
    """rows: list of (id, full_path, device_name, root_path). Returns db path."""
    d = tempfile.mkdtemp(prefix="mh_rx_")
    db = os.path.join(d, "inv.sqlite3")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE files(id INTEGER PRIMARY KEY, full_path TEXT, "
              "file_size INT, device_name TEXT, root_path TEXT)")
    c.execute("CREATE TABLE media_metadata(file_id INTEGER PRIMARY KEY)")
    for (fid, fp, dev, root) in rows:
        c.execute("INSERT INTO files VALUES(?,?,?,?,?)", (fid, fp, 100, dev, root))
    c.commit(); c.close()
    return db, d


def test_scan_only_flags_deleted_when_parent_folder_present(monkeypatch):
    drive = tempfile.mkdtemp(prefix="mh_drive_")
    keep = os.path.join(drive, "keep.jpg")
    gone = os.path.join(drive, "gone.jpg")
    open(keep, "w").write("x")                       # present
    open(gone, "w").write("y"); os.remove(gone)      # deleted, parent dir exists
    ghost = "/Volumes/NopeDrive/x/ghost.jpg"          # wrong mount: parent absent

    rows = [
        {"id": 1, "full_path": keep, "device_name": "D", "root_path": drive},
        {"id": 2, "full_path": gone, "device_name": "D", "root_path": drive},
        {"id": 3, "full_path": ghost, "device_name": "G", "root_path": "/Volumes/NopeDrive"},
    ]
    monkeypatch.setattr(reindex, "all_files", lambda: rows)
    monkeypatch.setattr(reindex.drives, "resolve_all",
                        lambda: {"D": drive, "G": "/Volumes/NopeDrive"})
    missing, present, skipped = reindex._scan_missing()
    assert present == 1                               # keep.jpg
    assert [m[0] for m in missing] == [2]            # only the genuinely deleted file
    assert skipped == 1                               # ghost (parent folder absent)


def test_scan_skips_unmounted_drive(monkeypatch):
    rows = [{"id": 1, "full_path": "/Volumes/Unplugged/a.jpg",
             "device_name": "U", "root_path": "/Volumes/Unplugged"}]
    monkeypatch.setattr(reindex, "all_files", lambda: rows)
    monkeypatch.setattr(reindex.drives, "resolve_all", lambda: {"U": None})
    missing, present, skipped = reindex._scan_missing()
    assert missing == [] and skipped == 1            # unplugged != deleted


def test_refuse_to_prune_when_nothing_resolves(monkeypatch):
    # Simulates a cross-machine inventory: many "missing" but present == 0.
    monkeypatch.setattr(reindex, "_scan_missing",
                        lambda: ([(1, "/x/a.jpg"), (2, "/x/b.jpg")], 0, 5))
    reindex._run(prune=True)
    assert reindex.reconcile_status()["missing"] == 2
    assert reindex.reconcile_status()["pruned"] == 0   # guard held: nothing purged


def test_soft_delete_trash_restore_purge(monkeypatch):
    db, d = _tmp_inventory([(1, "/x/a.jpg", "D", "/x"),
                            (2, "/x/b.jpg", "D", "/x"),
                            (3, "/x/c.jpg", "D", "/x")])
    monkeypatch.setattr(reindex, "DB_PATH", db)
    monkeypatch.setattr(reindex, "DATA_DIR", Path(d))
    monkeypatch.setattr(reindex, "invalidate_files_cache", lambda: None)

    # soft-delete 1 & 2 -> they land in Trash (reversible)
    assert reindex._prune([(1, "/x/a.jpg"), (2, "/x/b.jpg")]) == 2
    assert reindex.trash_list()["count"] == 2

    # restore 1
    assert reindex.restore([1])["restored"] == 1
    assert reindex.trash_list()["count"] == 1

    # hard purge 2
    assert reindex.purge(ids=[2])["purged"] == 1
    assert reindex.trash_list()["count"] == 0

    # final: row 1 restored (deleted_at NULL), 3 untouched, 2 gone
    c = sqlite3.connect(db)
    ids = sorted(r[0] for r in c.execute("SELECT id FROM files").fetchall())
    c.close()
    assert ids == [1, 3]
