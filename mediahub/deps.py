"""On-device acceleration installer.

MediaHub owns a PRIVATE virtual environment under Application Support and installs
optional accelerators (numpy, MLX CLIP) into it. This sidesteps PEP 668
("externally-managed-environment") on Homebrew/system Python and never touches the
user's system packages — the app installs everything for itself.

Core stays stdlib-only; these are pure speedups/extras and never required.
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

from .config import DATA_DIR

VENV_DIR = DATA_DIR / "pyenv"

# Allow-listed packages installable from the UI.
ALLOWED = {"numpy", "mlx-clip", "mlx", "mlx-vlm"}
# Named bundles the UI can request with one click.
BUNDLES = {
    "accel": ["numpy"],                 # faster search
    "vision_search": ["numpy", "mlx-clip"],   # true visual CLIP search on Apple Silicon
    "captions": ["mlx-vlm"],            # AI photo captions (vision-LLM) on Apple Silicon
}

_STATE = {"status": "idle", "packages": [], "log": "", "venv": str(VENV_DIR)}
_LOCK = threading.Lock()


def _set(**kw):
    with _LOCK:
        _STATE.update(**kw)


def deps_status():
    with _LOCK:
        s = dict(_STATE)
    s["venv_ready"] = venv_python() is not None
    s["numpy"] = _has("numpy")
    s["mlx_clip"] = _has("mlx_clip") or _has("mlx")
    s["mlx_vlm"] = _has("mlx_vlm")
    return s


def venv_python() -> Path | None:
    p = VENV_DIR / "bin" / "python3"
    return p if p.exists() else None


def runtime_python() -> str:
    """Python to launch subprocesses with — the private venv if present, else the
    current interpreter (so installed extras like mlx are importable)."""
    vp = venv_python()
    return str(vp) if vp else (sys.executable or "python3")


def _site_dir() -> Path | None:
    if not VENV_DIR.exists():
        return None
    lib = VENV_DIR / "lib"
    if not lib.exists():
        return None
    for d in sorted(lib.glob("python*")):
        sp = d / "site-packages"
        if sp.exists():
            return sp
    return None


def activate_site():
    """Add the private venv's site-packages to sys.path so the running server can
    import freshly-installed packages (e.g. numpy) without a relaunch."""
    sp = _site_dir()
    if sp and str(sp) not in sys.path:
        sys.path.append(str(sp))


def _has(mod: str) -> bool:
    activate_site()
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _ensure_venv() -> bool:
    if venv_python():
        return True
    _set(log="Creating MediaHub's private environment …")
    base = sys.executable or "python3"
    try:
        r = subprocess.run([base, "-m", "venv", str(VENV_DIR)],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            _set(status="error", log="Could not create venv:\n" + (r.stderr or r.stdout))
            return False
    except Exception as e:  # noqa: BLE001
        _set(status="error", log=f"venv creation failed: {e}")
        return False
    return venv_python() is not None


def _run_install(packages):
    try:
        _set(status="running", packages=packages, log=f"Preparing to install: {', '.join(packages)} …")
        if not _ensure_venv():
            return
        vp = str(venv_python())
        # upgrade pip quietly first (best-effort)
        subprocess.run([vp, "-m", "pip", "install", "--upgrade", "pip"],
                       capture_output=True, text=True, timeout=300)
        log_acc = []
        for pkg in packages:
            _set(status="running", log="\n".join(log_acc + [f"Installing {pkg} … (this can take a while)"]))
            r = subprocess.run([vp, "-m", "pip", "install", "--upgrade", pkg],
                               capture_output=True, text=True, timeout=1800)
            out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
            tail = "\n".join(out.splitlines()[-8:])
            if r.returncode == 0:
                log_acc.append(f"✓ {pkg} installed.")
            else:
                log_acc.append(f"✗ {pkg} failed (pip {r.returncode}):\n{tail}")
                _set(status="error", log="\n".join(log_acc))
                return
        activate_site()
        log_acc.append("Done. Acceleration is now available.")
        _set(status="done", log="\n".join(log_acc))
    except subprocess.TimeoutExpired:
        _set(status="error", log="Install timed out.")
    except Exception as e:  # noqa: BLE001
        _set(status="error", log=f"Install failed: {e}")


def start_install(packages):
    """packages: a list of allow-listed package names, or a bundle name string."""
    if isinstance(packages, str):
        packages = BUNDLES.get(packages, [packages])
    packages = [p for p in (packages or []) if p in ALLOWED]
    if not packages:
        return False, "No installable packages requested."
    with _LOCK:
        if _STATE["status"] == "running":
            return False, "An install is already running."
    threading.Thread(target=_run_install, args=(packages,), daemon=True).start()
    return True, None


# Make any previously-installed venv packages importable as soon as the module loads.
activate_site()
