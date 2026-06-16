"""Lightweight app log: prints to stdout (captured by the native shell) and
appends to DATA_DIR/logs/mediahub.log, size-capped so it never grows unbounded."""
import threading
import time

from .config import LOGS_DIR

LOG_FILE = LOGS_DIR / "mediahub.log"
_LOCK = threading.Lock()
_MAX_BYTES = 2_000_000


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    print(line, flush=True)
    try:
        with _LOCK:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > _MAX_BYTES:
                LOG_FILE.write_bytes(LOG_FILE.read_bytes()[-1_000_000:])
            with LOG_FILE.open("a") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def tail(n: int = 300) -> str:
    try:
        return "\n".join(LOG_FILE.read_text(errors="replace").splitlines()[-n:])
    except Exception:
        return ""
