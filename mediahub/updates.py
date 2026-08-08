"""Check for updates by comparing the running VERSION to the latest published
GitHub release. Stdlib only, best-effort, short timeout — never blocks the app."""
import json
import re
import urllib.request

from .config import VERSION, GITHUB_REPO


def _ver_tuple(s):
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def check_update(timeout: float = 4.0) -> dict:
    """Return {current, latest, update_available, url, error?}. Drafts/prereleases
    are ignored (the /releases/latest endpoint only returns published releases)."""
    result = {"current": VERSION, "latest": None, "update_available": False,
              "url": f"https://github.com/{GITHUB_REPO}/releases",
              "download_url": f"https://github.com/{GITHUB_REPO}/releases/latest/download/MediaHub-Installer.dmg",
              "error": None}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "MediaHub-update-check",
            "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        tag = (data.get("tag_name") or "").strip()
        result["latest"] = tag or None
        if data.get("html_url"):
            result["url"] = data["html_url"]
        if tag:
            result["update_available"] = _ver_tuple(tag) > _ver_tuple(VERSION)
    except Exception as e:  # noqa: BLE001 — offline / no releases / rate-limited
        result["error"] = str(e)
    return result
