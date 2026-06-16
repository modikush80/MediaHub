"""HTTP server: routes, static assets, existing-instance detection, main()."""
import hashlib
import io
import json
import os
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .config import APP_NAME, HOST, PORT, GB, DATA_DIR, LOGS_DIR, MANIFESTS_DIR, CACHE_DIR
from .db import DB_PATH
from .trips import q_summary, build_trips, build_sources
from .dedupe import dedupe_plan_rows
from .staging import (stage_preview, stage_status, start_stage,
                      continue_stage, skip_drive, stage_errors, retry_errors,
                      stage_manifest, list_archived_manifests)
from . import applog
from .settings import (load_settings, save_settings, dest_base, free_gb,
                       load_overrides, save_overrides)
from .ingest import INGEST, INGEST_LOCK, start_ingest
from .mounts import mounted_volumes
from .vision import vision_status, start_vision, vision_results, vision_search
from .picker import pick_path
from .search import search, embed_status, start_embed
from . import drives
from . import deps
from . import ai
from . import captions
from . import faces
from . import reindex

UI_DIR = Path(__file__).resolve().parent / "ui"
_CTYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".json": "application/json",
    ".webmanifest": "application/manifest+json", ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-App", APP_NAME)
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, rel):
        rel = rel.split("?", 1)[0].lstrip("/")
        p = (UI_DIR / rel).resolve()
        if not str(p).startswith(str(UI_DIR)) or not p.is_file():
            return self._send(404, {"error": "not found"})
        ctype = _CTYPES.get(p.suffix.lower(), "application/octet-stream")
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-App", APP_NAME)
        self.end_headers()
        self.wfile.write(body)

    def _serve_thumb(self, src):
        """Render (and cache) a JPEG thumbnail of a local image via macOS sips
        (handles RAW/HEIC/PNG/JPEG). Localhost only."""
        if not src:
            return self._send(400, {"error": "path required"})
        p = Path(src)
        if not p.is_file():
            return self._send(404, {"error": "not found"})
        try:
            key = hashlib.sha1(f"{p}:{p.stat().st_mtime_ns}".encode()).hexdigest()
        except Exception:
            return self._send(404, {"error": "stat failed"})
        out = CACHE_DIR / f"thumb_{key}.jpg"
        if not out.exists():
            try:
                subprocess.run(["sips", "-Z", "400", "-s", "format", "jpeg",
                                str(p), "--out", str(out)],
                               capture_output=True, timeout=30)
            except Exception:
                pass
        if not out.exists():
            return self._send(415, {"error": "could not render thumbnail"})
        data = out.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("X-App", APP_NAME)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path in ("/app.js", "/styles.css") or path.startswith("/assets/"):
                return self._serve_static(path)
            if path == "/api/summary":
                return self._send(200, q_summary())
            if path == "/api/trips":
                return self._send(200, build_trips())
            if path == "/api/sources":
                return self._send(200, build_sources())
            if path == "/api/stage/preview":
                trip = parse_qs(u.query).get("trip", [""])[0]
                if not trip:
                    return self._send(400, {"error": "trip required"})
                return self._send(200, stage_preview(trip))
            if path == "/api/mounts":
                return self._send(200, mounted_volumes())
            if path == "/api/drives/identity":
                return self._send(200, drives.identity_report())
            if path == "/api/settings":
                s = load_settings()
                s["dest_free_gb"] = free_gb(dest_base())
                return self._send(200, s)
            if path == "/api/paths":
                def _du(p):
                    try:
                        return round(sum(f.stat().st_size for f in Path(p).rglob("*")
                                         if f.is_file()) / (1024 * 1024), 1)
                    except Exception:
                        return None
                return self._send(200, {
                    "data_dir": str(DATA_DIR), "logs_dir": str(LOGS_DIR),
                    "manifests_dir": str(MANIFESTS_DIR), "cache_dir": str(CACHE_DIR),
                    "db_path": str(DB_PATH),
                    "data_mb": _du(DATA_DIR), "cache_mb": _du(CACHE_DIR)})
            if path == "/api/logs/tail":
                return self._send(200, {"log": applog.tail(400)})
            if path == "/api/logs/export":
                data = applog.tail(100000).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Disposition", 'attachment; filename="mediahub.log"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-App", APP_NAME)
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/manifests":
                return self._send(200, list_archived_manifests())
            if path == "/api/stage/status":
                return self._send(200, stage_status())
            if path == "/api/stage/errors":
                return self._send(200, stage_errors())
            if path == "/api/stage/manifest":
                return self._send(200, stage_manifest())
            if path == "/api/stage/errors.csv":
                errs = stage_errors().get("errors", [])
                buf = io.StringIO()
                buf.write("dest_name,dest_subfolder,device,size_bytes,sha256,source_path,error\n")
                for e in errs:
                    buf.write(",".join([
                        '"' + (e["dest_name"] or "").replace('"', "'") + '"',
                        '"' + (e["dest_rel"] or "") + '"', e["device"] or "",
                        str(e["size"]), e["sha256"] or "",
                        '"' + (e["source"] or "").replace('"', "'") + '"',
                        '"' + (e["error"] or "").replace('"', "'") + '"',
                    ]) + "\n")
                data = buf.getvalue().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", 'attachment; filename="stage_errors.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-App", APP_NAME)
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/ingest/status":
                with INGEST_LOCK:
                    return self._send(200, dict(INGEST))
            if path == "/api/vision/status":
                return self._send(200, vision_status())
            if path == "/api/vision/results":
                qs = parse_qs(u.query)
                b = qs.get("bucket", [""])[0] or None
                k = int(qs.get("limit", ["300"])[0])
                return self._send(200, vision_results(b, k))
            if path == "/api/vision/search":
                qs = parse_qs(u.query)
                q = qs.get("q", [""])[0]
                if not q.strip():
                    return self._send(400, {"error": "query required"})
                return self._send(200, vision_search(q, int(qs.get("limit", ["80"])[0])))
            if path == "/api/thumb":
                return self._serve_thumb(parse_qs(u.query).get("path", [""])[0])
            if path == "/api/embed/status":
                return self._send(200, embed_status())
            if path == "/api/deps/status":
                return self._send(200, deps.deps_status())
            if path == "/api/search":
                qs = parse_qs(u.query)
                q = qs.get("q", [""])[0]
                k = int(qs.get("k", ["30"])[0])
                if not q.strip():
                    return self._send(400, {"error": "query required"})
                parsed = ai.parse_query(q)
                res = search(parsed["text"], k, filters=parsed["filters"])
                res["parsed"] = parsed
                return self._send(200, res)
            if path == "/api/ai/near-duplicates":
                thr = float(parse_qs(u.query).get("threshold", ["0.9"])[0])
                return self._send(200, ai.near_duplicates(thr))
            if path == "/api/ai/near-duplicates.csv":
                thr = float(parse_qs(u.query).get("threshold", ["0.9"])[0])
                data = ai.near_duplicates_csv(thr).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition",
                                 'attachment; filename="near_duplicates.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/ai/screenshot-sort":
                return self._send(200, ai.screenshot_sort_suggestions())
            if path == "/api/ai/caption/status":
                return self._send(200, captions.caption_status())
            if path == "/api/ai/faces/status":
                return self._send(200, faces.faces_status())
            if path == "/api/ai/faces/people":
                return self._send(200, faces.people_groups())
            if path == "/api/reindex/status":
                return self._send(200, reindex.reconcile_status())
            if path == "/api/reindex/trash":
                return self._send(200, reindex.trash_list())
            if path == "/api/ai/caption/search":
                qs = parse_qs(u.query)
                return self._send(200, captions.caption_search(
                    qs.get("q", [""])[0], int(qs.get("limit", ["60"])[0])))
            if path == "/api/dedupe-plan.csv":
                rows = dedupe_plan_rows()
                buf = io.StringIO()
                buf.write("decision,sha256,file_name,device_name,file_size,full_path,keep_path\n")
                for r in rows:
                    buf.write(",".join([
                        r["decision"], r["sha256"],
                        '"' + r["file_name"].replace('"', "'") + '"',
                        r["device_name"], str(r["file_size"]),
                        '"' + r["full_path"].replace('"', "'") + '"',
                        '"' + r["keep_path"].replace('"', "'") + '"',
                    ]) + "\n")
                data = buf.getvalue().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition",
                                 'attachment; filename="dedupe_plan.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/dedupe-plan/summary":
                rows = dedupe_plan_rows()
                dele = [r for r in rows if r["decision"] == "DELETE"]
                by_dev = {}
                for r in dele:
                    by_dev[r["device_name"]] = by_dev.get(r["device_name"], 0) + r["file_size"]
                return self._send(200, {
                    "delete_files": len(dele),
                    "reclaim_gb": round(sum(r["file_size"] for r in dele) / GB, 1),
                    "by_device": [{"device": k, "reclaim_gb": round(v / GB, 1)}
                                  for k, v in sorted(by_dev.items(), key=lambda x: -x[1])],
                })
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        try:
            if u.path == "/api/stage/start":
                st = stage_status()
                if st.get("status") in ("running",):
                    return self._send(409, {"error": "a staging job is already running"})
                trip = payload.get("trip")
                dry = bool(payload.get("dry_run", False))
                verify = bool(payload.get("verify_hash", load_settings()["verify_hash"]))
                if not trip:
                    return self._send(400, {"error": "trip required"})
                start_stage(trip, dry, verify)
                time.sleep(0.2)
                return self._send(200, {"started": True, "trip": trip, "dry_run": dry})
            if u.path == "/api/stage/continue":
                ok = continue_stage()
                time.sleep(0.2)
                return self._send(200, {"continued": ok})
            if u.path == "/api/stage/skip-drive":
                drive = payload.get("drive")
                if not drive:
                    return self._send(400, {"error": "drive required"})
                skip_drive(drive)
                time.sleep(0.2)
                return self._send(200, {"skipped_drive": drive})
            if u.path == "/api/stage/retry-errors":
                r = retry_errors()
                time.sleep(0.2)
                return self._send(200, r)
            if u.path == "/api/settings":
                s = save_settings(payload)
                s["dest_free_gb"] = free_gb(dest_base())
                return self._send(200, s)
            if u.path == "/api/ingest":
                scan_path = payload.get("path")
                if not scan_path:
                    return self._send(400, {"error": "path required"})
                if not Path(scan_path).expanduser().exists():
                    return self._send(400, {"error": f"path not found: {scan_path}"})
                ok = start_ingest(str(Path(scan_path).expanduser()))
                return self._send(200 if ok else 409,
                                  {"started": ok} if ok else {"error": "ingest already running"})
            if u.path == "/api/vision/start":
                ok = start_vision(payload.get("limit", 2000),
                                  bool(payload.get("only_unsorted")),
                                  (payload.get("under") or "").strip() or None,
                                  (payload.get("folder") or "").strip() or None)
                return self._send(200 if ok else 409,
                                  {"started": ok} if ok else {"error": "vision run already in progress"})
            if u.path == "/api/open":
                fp = (payload.get("path") or "").strip()
                if not fp or not Path(fp).exists():
                    return self._send(400, {"error": "path not found"})
                try:
                    # reveal=True selects the file in Finder; otherwise open it in the default app.
                    cmd = ["open", "-R", fp] if payload.get("reveal") else ["open", fp]
                    subprocess.run(cmd, capture_output=True, timeout=10)
                    return self._send(200, {"opened": True})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            if u.path == "/api/pick":
                return self._send(200, pick_path(payload.get("kind", "folder"),
                                                 payload.get("prompt", "")))
            if u.path == "/api/drives/resolve":
                dn = payload.get("device_name"); mt = payload.get("mount")
                if not dn or not mt:
                    return self._send(400, {"error": "device_name and mount required"})
                return self._send(200, drives.set_manual(dn, mt))
            if u.path == "/api/embed/start":
                ok = start_embed(payload.get("backend"),
                                 (payload.get("under") or "").strip() or None,
                                 (payload.get("folder") or "").strip() or None)
                return self._send(200 if ok else 409,
                                  {"started": ok} if ok else {"error": "embedding already in progress"})
            if u.path == "/api/deps/install":
                req = payload.get("bundle") or payload.get("packages") or payload.get("package")
                ok, err = deps.start_install(req)
                return self._send(200 if ok else 409,
                                  {"started": True} if ok else {"error": err})
            if u.path == "/api/ai/caption/start":
                ok = captions.start_captions(
                    payload.get("backend"),
                    (payload.get("under") or "").strip() or None,
                    (payload.get("folder") or "").strip() or None)
                return self._send(200 if ok else 409,
                                  {"started": ok} if ok else {"error": "captioning already in progress"})
            if u.path == "/api/ai/faces/start":
                ok = faces.start_faces(
                    (payload.get("under") or "").strip() or None,
                    (payload.get("folder") or "").strip() or None)
                return self._send(200 if ok else 409,
                                  {"started": ok} if ok else {"error": "face detection already in progress"})
            if u.path == "/api/reindex/scan":
                ok = reindex.start_reconcile(bool(payload.get("prune")))
                return self._send(200 if ok else 409,
                                  {"started": ok} if ok else {"error": "reconcile already in progress"})
            if u.path == "/api/reindex/restore":
                return self._send(200, reindex.restore(payload.get("ids")))
            if u.path == "/api/reindex/purge":
                return self._send(200, reindex.purge(payload.get("ids"), bool(payload.get("expired"))))
            if u.path == "/api/immich/export":
                return self._send(200, ai.immich_export_notes())
            if u.path == "/api/override":
                ov = load_overrides()
                key = payload.get("key")
                if not key:
                    return self._send(400, {"error": "key required"})
                ov[key] = {"trip": payload.get("trip"),
                           "category": payload.get("category")}
                save_overrides(ov)
                return self._send(200, {"saved": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})



def _is_mediahub(host: str, port: int) -> bool:
    """True if a MediaHub instance is already serving this port."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/summary")
        with urllib.request.urlopen(req, timeout=0.8) as r:
            return r.headers.get("X-App") == APP_NAME
    except Exception:
        return False


def _bind_server(host: str, start: int):
    """Create the HTTP server, walking forward from `start` on conflict.
    Binding is authoritative (no connect-probe race), so launch never dies
    with Errno 48. Returns (server, port) or (None, None) if none free."""
    port = start
    for _ in range(60):
        try:
            return ThreadingHTTPServer((host, port), Handler), port
        except OSError:
            port += 1
    return None, None


def main():
    global PORT
    # If a MediaHub instance already owns the preferred port, just surface it.
    if _is_mediahub(HOST, PORT):
        url = f"http://{HOST}:{PORT}"
        print(f"{APP_NAME} is already running at {url} — opening browser.")
        if not os.environ.get("MEDIAHUB_NO_BROWSER"):
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return

    server, port = _bind_server(HOST, PORT)
    if server is None:
        print(f"{APP_NAME}: no free port near {PORT}; is something misbehaving?")
        return
    PORT = port
    url = f"http://{HOST}:{port}"
    applog.log(f"{APP_NAME} running at {url}  (db: {DB_PATH})")
    print("Press Ctrl+C to stop.")

    # Warm the heavy caches in the background so the first tab render is instant.
    def _warm():
        try:
            q_summary(); build_trips(); build_sources()
        except Exception:
            pass
        # Reconcile: purge anything past its recovery window, then surface current
        # deletions. Auto-prune (soft-delete) only when the user opted in.
        try:
            reindex.purge(expired=True)            # remove trash older than retention
            auto = bool(load_settings().get("auto_reconcile"))
            reindex.start_reconcile(prune=auto)    # prune=soft-delete only if opted in
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()

    if not os.environ.get("MEDIAHUB_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


