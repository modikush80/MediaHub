"""Verified-MOVE (consolidate) safety.

The critical invariant: source copies are only removed once a verified
destination exists, the surviving copy is never removed, and unmounted/absent
sources are left untouched. Every removal is logged to the moves ledger.
"""
import os
import tempfile
import mediahub.staging as staging


def test_consolidate_removes_mounted_sources_but_spares_destination(monkeypatch):
    d = tempfile.mkdtemp(prefix="mh_move_")
    # a verified destination copy (must NEVER be removed)
    dest = os.path.join(d, "dest.jpg")
    open(dest, "w").write("data")
    # two mounted source duplicates + one "unmounted" (nonexistent) candidate
    s1 = os.path.join(d, "driveA.jpg"); open(s1, "w").write("data")
    s2 = os.path.join(d, "driveB.jpg"); open(s2, "w").write("data")
    missing = os.path.join(d, "unplugged", "gone.jpg")   # parent doesn't exist

    # resolve_candidate_path returns the candidate path as-is (they're "mounted")
    monkeypatch.setattr(staging.drives, "resolve_candidate_path",
                        lambda dev, root, path, mm: (path if os.path.exists(path) else None))
    # trash_source = simulate the OS Trash by removing the file; record to a list
    trashed, logged = [], []
    monkeypatch.setattr(staging.moves, "trash_source",
                        lambda p: (trashed.append(p), os.remove(p), True)[-1])
    monkeypatch.setattr(staging.moves, "record",
                        lambda *a, **k: logged.append((a, k)))

    it = {"sha256": "H", "size": 4, "verified_sha": "H", "candidates": [
        {"device": "A", "root": "", "path": s1},
        {"device": "B", "root": "", "path": s2},
        {"device": "Dest", "root": "", "path": dest},     # same as surviving copy
        {"device": "U", "root": "", "path": missing},     # unmounted
    ]}
    job = {"mode": "move"}
    staging._consolidate_sources(job, it, dest, mount_map={})

    assert not os.path.exists(s1) and not os.path.exists(s2)   # dupes removed
    assert os.path.exists(dest)                                # surviving copy kept
    assert set(trashed) == {s1, s2}                            # dest + missing NOT trashed
    assert it["removed_sources"] == 2 and job["moved_files"] == 2
    assert len(logged) == 2                                    # every removal logged


def test_consolidate_never_touches_when_no_sources_resolve(monkeypatch):
    d = tempfile.mkdtemp(prefix="mh_move2_")
    dest = os.path.join(d, "dest.jpg"); open(dest, "w").write("x")
    monkeypatch.setattr(staging.drives, "resolve_candidate_path",
                        lambda dev, root, path, mm: None)      # nothing mounted
    calls = []
    monkeypatch.setattr(staging.moves, "trash_source", lambda p: calls.append(p) or True)
    monkeypatch.setattr(staging.moves, "record", lambda *a, **k: None)
    it = {"sha256": "H", "size": 1, "candidates": [{"device": "A", "root": "", "path": "/x/a.jpg"}]}
    staging._consolidate_sources({"mode": "move"}, it, dest, {})
    assert calls == [] and it["removed_sources"] == 0          # nothing trashed
    assert os.path.exists(dest)
