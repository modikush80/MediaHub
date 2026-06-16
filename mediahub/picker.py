"""Native macOS folder/file picker (osascript) so the browser UI can choose a
real server-side path. The server and browser run on the same Mac, so a Finder
'choose folder' dialog returns the actual filesystem path we need to scan."""
import subprocess


def pick_path(kind: str = "folder", prompt: str = "") -> dict:
    """Open a native chooser. Returns {"path": ...}, {"cancelled": True}, or
    {"error": ...}. Blocks the calling thread until the user responds."""
    kind = "file" if kind == "file" else "folder"
    if not prompt:
        prompt = "Choose a file" if kind == "file" else "Choose a folder"
    prompt = prompt.replace('"', "'")
    verb = "choose file" if kind == "file" else "choose folder"
    # `activate` brings the chooser to the front; without it the dialog can open
    # behind the browser window and look like nothing happened.
    script = (f'activate\n'
              f'set theItem to ({verb} with prompt "{prompt}")\n'
              f'POSIX path of theItem')
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=300)
    except Exception as e:
        return {"error": str(e)}
    if r.returncode != 0:
        if "cancel" in (r.stderr or "").lower():
            return {"cancelled": True}
        return {"error": (r.stderr or "picker failed").strip()}
    path = (r.stdout or "").strip()
    return {"path": path} if path else {"cancelled": True}
