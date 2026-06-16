#!/usr/bin/env python3
"""
clip_embed.py - pluggable embedder for MediaHub semantic search.

Two backends, identical I/O so the rest of the app never changes:

  stub  (default, zero dependencies)
        Hashed bag-of-words. For TEXT it embeds the query words; for an IMAGE
        it embeds the words in its file path (folders + filename). Cosine
        similarity then reflects keyword/path overlap -> instant, offline,
        works even when the source drives are not mounted. Great as a
        filename/path search and for testing the whole pipeline.

  mlx   (true CLIP on Apple Silicon - your M5 Max)
        Real vision-language embeddings: TEXT and IMAGE land in the same space,
        so "drone shot of a waterfall" matches the actual pixels. Requires:
            pip install mlx-clip
        and the source drives mounted (it reads real image pixels).

Modes:
  --text "a sunset over water"      -> {"dim":D,"vec":[...]}
  --stdin-paths                     -> one JSON line per path: {"path":..,"vec":[...]}

Common:
  --backend stub|mlx   (default: env MEDIAHUB_EMBED_BACKEND or "stub")
  --dim N              (stub only; default 512)
  --model NAME         (mlx only; default a CLIP ViT-B/32)
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import re
import sys

DEFAULT_DIM = 512
DEFAULT_MLX_MODEL = "openai/clip-vit-base-patch32"
_WORD = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------- stub
def _tokens(s: str):
    return _WORD.findall((s or "").lower())


def _path_text(path: str) -> str:
    # Turn a file path into searchable words: split folders, strip extension,
    # break camelCase / separators so "DSC_Iceland2023.jpg" -> dsc iceland 2023.
    base = os.path.splitext(path)[0]
    base = base.replace("/", " ").replace("_", " ").replace("-", " ")
    base = re.sub(r"([a-z])([A-Z])", r"\1 \2", base)
    return base


def stub_vector(text: str, dim: int):
    """L2-normalized signed hashing vectorizer (collision-robust bag-of-words)."""
    v = [0.0] * dim
    for tok in _tokens(text):
        h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)
        bucket = h % dim
        sign = 1.0 if (h >> 8) & 1 else -1.0
        v[bucket] += sign
    n = math.sqrt(sum(x * x for x in v))
    if n > 0:
        v = [x / n for x in v]
    return v


# --------------------------------------------------------------------------- mlx
RAW_EXTS = {".dng", ".arw", ".cr3", ".gpr", ".nef", ".raf", ".orf", ".rw2"}


def _raw_to_jpeg(path: str):
    """RAW files can't be read by CLIP directly. Decode to a temp JPEG via
    macOS `sips` (ImageIO — natively supports Sony ARW, DNG, CR3, etc.).
    Returns (viewable_path, tempfile_to_cleanup_or_None)."""
    import tempfile
    import subprocess
    if os.path.splitext(path)[1].lower() not in RAW_EXTS:
        return path, None
    tmp = tempfile.mktemp(suffix=".jpg")
    r = subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "768", path, "--out", tmp],
                       capture_output=True)
    if r.returncode == 0 and os.path.exists(tmp):
        return tmp, tmp
    return None, None


class _MlxClip:
    """Lazy MLX CLIP wrapper. Imported only when backend=mlx is used."""
    def __init__(self, model_name: str):
        try:
            from mlx_clip import mlx_clip  # type: ignore
        except Exception as e:
            sys.exit("mlx backend needs mlx-clip:  pip install mlx-clip\n"
                     f"(import error: {e})")
        self.clip = mlx_clip(model_name)

    def text(self, s: str):
        emb = self.clip.encode_text(s)
        return _to_list_normalized(emb)

    def image(self, path: str):
        viewable, tmp = _raw_to_jpeg(path)
        if viewable is None:
            raise RuntimeError("could not decode RAW (sips failed)")
        try:
            emb = self.clip.encode_image(viewable)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return _to_list_normalized(emb)


def _to_list_normalized(emb):
    try:
        vals = [float(x) for x in (emb.tolist() if hasattr(emb, "tolist") else emb)]
    except Exception:
        vals = list(map(float, emb))
    if vals and isinstance(vals[0], list):      # shape (1, D)
        vals = vals[0]
    n = math.sqrt(sum(x * x for x in vals))
    return [x / n for x in vals] if n > 0 else vals


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=os.environ.get("MEDIAHUB_EMBED_BACKEND", "stub"),
                    choices=["stub", "mlx"])
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM)
    ap.add_argument("--model", default=DEFAULT_MLX_MODEL)
    ap.add_argument("--text", default=None)
    ap.add_argument("--stdin-paths", action="store_true")
    args = ap.parse_args()

    if args.backend == "mlx":
        clip = _MlxClip(args.model)
        text_fn = clip.text
        image_fn = clip.image
        dim = None
    else:
        text_fn = lambda s: stub_vector(s, args.dim)
        image_fn = lambda p: stub_vector(_path_text(p), args.dim)
        dim = args.dim

    if args.text is not None:
        vec = text_fn(args.text)
        print(json.dumps({"dim": dim or len(vec), "backend": args.backend, "vec": vec}))
        return

    if args.stdin_paths:
        for line in sys.stdin:
            p = line.strip()
            if not p:
                continue
            try:
                vec = image_fn(p)
                sys.stdout.write(json.dumps({"path": p, "vec": vec}) + "\n")
            except Exception as e:
                sys.stdout.write(json.dumps({"path": p, "error": str(e)}) + "\n")
            sys.stdout.flush()
        return

    ap.error("nothing to do: pass --text or --stdin-paths")


if __name__ == "__main__":
    main()
