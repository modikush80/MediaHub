#!/usr/bin/env python3
"""Zero-dependency test runner (no pytest required).

    python3 tests/run.py

Discovers test_*.py in this folder, runs every test_* function, and provides a
minimal `monkeypatch` shim so the same tests also run under `pytest`.
"""
import importlib
import inspect
import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Isolate to a throwaway data dir BEFORE importing mediahub.
os.environ.setdefault("MEDIAHUB_DATA", tempfile.mkdtemp(prefix="mh_tests_"))
os.environ.setdefault("MEDIAHUB_DB", os.path.join(os.environ["MEDIAHUB_DATA"], "inventory.sqlite3"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))


class MiniMonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        had = hasattr(target, name)
        old = getattr(target, name, None)
        self._undo.append((target, name, had, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, had, old in reversed(self._undo):
            if had:
                setattr(target, name, old)
            else:
                try:
                    delattr(target, name)
                except Exception:
                    pass
        self._undo.clear()


def main():
    modules = sorted(p.stem for p in HERE.glob("test_*.py"))
    passed = failed = 0
    failures = []
    for modname in modules:
        mod = importlib.import_module(modname)
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith("test_") or fn.__module__ != mod.__name__:
                continue
            mp = MiniMonkeyPatch()
            try:
                if "monkeypatch" in inspect.signature(fn).parameters:
                    fn(monkeypatch=mp)
                else:
                    fn()
                passed += 1
                print(f"  ✓ {modname}.{name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                failures.append((f"{modname}.{name}", traceback.format_exc()))
                print(f"  ✗ {modname}.{name}: {e}")
            finally:
                mp.undo()
    print(f"\n{passed} passed, {failed} failed")
    for name, tb in failures:
        print(f"\n=== {name} ===\n{tb}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
