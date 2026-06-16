# MediaHub — Design & Architecture

A portable, local-first macOS tool that organizes a multi-terabyte photo/video
archive into trips, de-duplicates it safely, stages it (copy-only) to a local
SSD or NAS one trip at a time, and adds optional on-device AI (Apple Vision
tagging + CLIP semantic search). Built to run on the user's own Macs (an M-series
laptop and a Mac mini), reading drives directly, with **no third-party runtime**
in the core app.

> **Implementation status (Priorities 1–5 complete).**
> 1. **Robust drive identity** — resolves a source drive by name → learned UUID →
>    content fingerprint → user choice, so a *renamed/remounted* drive still
>    stages (no longer pure `/Volumes/<name>` matching).
> 2. **Staging status + retry** — per-state counts, next-recommended drive,
>    failed-file list with reasons, retry, and CSV export.
> 3. **Manifests + verification** — every job emits `_MediaHub_manifest.{csv,json}`,
>    `_MediaHub_errors.csv`, `_MediaHub_stage_summary.json`; SHA/size mismatch is a
>    hard error, never silently ignored; manifests archived to Application Support.
> 4. **Native Mac shell** — SwiftUI + WKWebView app that starts/attaches the Python
>    backend and hosts the UI in a real window (no terminal, no browser tab).
> 5. **Storage & logs** — all writable state under Application Support (`logs/`,
>    `manifests/`, `cache/`), with an app log and UI to find/export logs & manifests.
>
> Remaining: Priority 6 (code-signing/notarization, security-scoped bookmarks).

---

## 1. Goals & non-goals

**Goals**
- Turn ~82k files / ~4.6 TB scattered across multiple SSDs into a clean,
  date+place organized archive on a NAS, **without ever risking the originals**.
- Work across two Macs; be trivially portable (copy a folder, run it).
- Handle a hard physical constraint: **only two drives can be mounted at once**.
- Use Apple Silicon (M5 Max) for on-device AI: content tagging and semantic
  search — fully private, no cloud.

**Non-goals**
- Not a cloud service, not multi-tenant, not an App Store app.
- The app never deletes or moves source files (see §8 Safety).
- Not a RAW developer / editor; it organizes and indexes, it doesn't edit.

---

## 2. High-level architecture

```
┌──────────────────────── one Mac (laptop or Mac mini) ────────────────────────┐
│                                                                               │
│  Browser UI (Liquid-Glass) ──HTTP──►  Local Python server (stdlib only)       │
│   index.html / styles.css / app.js     mediahub package, 127.0.0.1:8765       │
│                                              │                                │
│         ┌────────────────────────────────────┼─────────────────────────────┐ │
│         ▼                 ▼                    ▼              ▼              ▼ │
│   Inventory DB      Derived views        Staging engine   Ingest      AI subprocs│
│  media_indexer      trips/dedupe/        (copy-only,      (stdlib      Swift Vision│
│  .sqlite3 (RO)      sources (cached)     swap-aware)      scanner)     + MLX CLIP │
│         │                                     │                          │     │
│         └─ read-only ──────────────────► source drives /Volumes/*  ──────┘     │
│                                               │ (copy)                          │
│                                               ▼                                │
│                                     Destination (local SSD or NAS)             │
│                                                                               │
│  Writable state (never in the app bundle): ~/Library/Application Support/MediaHub │
│   settings.json · stage_job.json · trip_overrides.json · embeddings.sqlite3 ·  │
│   vision_tags.sqlite3                                                          │
└───────────────────────────────────────────────────────────────────────────────┘
```

- **Frontend**: a single-page browser UI (no framework, no build step) served by
  the Python server. Talks to a small JSON API.
- **Backend**: a Python **standard-library-only** package (`mediahub/`) exposing
  an HTTP API and serving static UI assets.
- **AI**: invoked as **subprocesses** (Swift Vision tool; MLX/CLIP embedder) so
  the long-running server stays light and dependency-free; AI deps are optional
  and isolated.

Rationale for this shape is in §11 (Design decisions).

---

## 3. Package layout (the refactor)

The app started as one 1,346-line file and was split into a maintainable package:

```
mediahub/
├── __main__.py     entry point: `python3 -m mediahub`
├── config.py       identity, paths (logs/manifests/cache), GB, data-epoch counter
├── classify.py     pure classifiers: media bucket, device, stage, orientation,
│                   trip-name rules, slugify  (no I/O, no cross-deps)
├── db.py           read-only inventory access; all_files() joined+derived cache
├── settings.py     destination settings + trip-name overrides (JSON)
├── trips.py        q_summary, build_trips, build_sources (epoch-memoized)
├── dedupe.py       safe de-duplication plan (one copy per hash)
├── drives.py       drive-identity resolver (name → UUID → fingerprint → user)
├── staging.py      layout rules + swap-aware, resumable copy engine + manifests
├── ingest.py       self-contained scanner that indexes new media into the DB
├── mounts.py       mounted-volume discovery + drive-name mapping
├── picker.py       native macOS folder/file chooser (osascript)
├── vision.py       wraps the Apple Vision content-tagging tool
├── search.py       embeddings store + cosine semantic search
├── applog.py       app log (stdout + capped logs/mediahub.log)
├── server.py       HTTP routes, static serving, instance detection, main()
└── ui/             index.html · styles.css · app.js  (Liquid-Glass)

embed/clip_embed.py  pluggable embedder: stub (zero-dep) | mlx CLIP (+RAW decode)
vision/              vision_tag.swift (CoreML/Vision) + vision_enrich.py
                     + face_detect.swift (Vision face rects + per-face feature print)
shell/MediaHubShell.swift  SwiftUI + WKWebView native shell (starts/attaches backend)

mediahub/deps.py     private-venv installer (numpy / mlx-clip / mlx-vlm) — PEP 668-safe
mediahub/ai.py       near-duplicate/burst clustering + best-shot, NL query parsing,
                     screenshot/receipt sort suggestions (all report-only)
mediahub/captions.py photo captions: stub (Vision tags+OCR+filename) | mlxvlm (vision-LLM)
mediahub/faces.py    builds/runs face_detect, clusters feature prints into People groups
```

**Dependency direction** (acyclic): `config` ← `classify` ← `db` ← {`settings`,
`trips`, `dedupe`, `drives`, `staging`, `ingest`, `search`, `vision`, `mounts`,
`picker`, `applog`} ← `server` ← `__main__`. `classify` is pure; `staging` owns the
sidecar/destination logic and uses `drives` for source resolution; `server` is the
only module that imports everything.

---

## 4. Data model

**Inventory DB** (`media_indexer.sqlite3`, opened **read-only** via
`file:...?mode=ro`):
- `files(id, full_path, file_name, extension, file_size, sha256, device_name,
  root_path, created_time, ...)`
- `media_metadata(file_id, camera_make, camera_model, capture_date,
  image_width, image_height, raw_metadata_json, ...)`
- `scan_runs(...)` — ingest history.

**`all_files()`** is the core read: a single LEFT JOIN of `files` + `media_metadata`,
with three derived fields computed in Python per row — `device`, `stage`,
`orientation` — then cached for the process lifetime. EXIF `Orientation` (5–8) is
honored so a rotated-portrait Sony frame is classified Vertical even though its
sensor dimensions are landscape.

**App-owned state** (in `~/Library/Application Support/MediaHub/`, never in the
bundle so a read-only/relocated `.app` works):
- `settings.json` — destination mode/path, verify flag.
- `stage_job.json` — persistent per-file staging state (resume across swaps/restarts).
- `trip_overrides.json` — user re-mapping of folders → trips.
- `drives.json` — learned drive identities (UUID + last mount) for resolution.
- `embeddings.sqlite3` — semantic-search vectors (separate DB).
- `vision_tags.sqlite3` — Vision content tags (separate DB).
- `logs/mediahub.log` — app log (size-capped).
- `manifests/<trip>_<ts>/` — archived audit manifests per completed job.
- `cache/` — reserved for derived caches.

The inventory is **never mutated** except by the optional Ingest scanner, which
only INSERTs/UPDATEs rows (never touches media files).

---

## 5. Classification

For each file, deterministic rules (no ML) decide:
- **Media bucket** — by extension: `photos | raw | videos | sidecar | other`.
- **Device** — EXIF make/model first (covers ~50k files), then extension
  (`.insv`=Insta360, `.gpr`=GoPro, `.arw`=Sony, `.heic`=iPhone), then filename
  (`DJI_`, `GX/GH/GOPR`, `IMG_`, `DSC`), then `AbsoluteAltitude` present ⇒ Drone.
- **Stage** — `original` vs `edited` vs `sidecar`, from editing-software EXIF,
  filename hints (`-edit`, `export`, `final`, `pano`, `hdr`, `select`), or an
  EDITED/EXPORT/ProRes parent folder.
- **Orientation** — `Horizontal | Vertical` from *display* dimensions (EXIF
  Orientation-aware), `Unknown` when dimensions are missing.
- **Trip** — keyword rules over the top-level folder name → canonical trip label
  + category (`trips | events | camera-dumps | personal | unsorted`); user
  overrides win.

---

## 6. Organization scheme (staging layout)

Researched against DAM best practice (Krogh / Fstoppers / Icon) and the user's
explicit preference (location-first):

```
<Location>/<Year>/<YYYY-MM>/
   raw | edited /
      images | videos /              images = all stills together (ARW, DNG, JPEG, HEIC)
         <Device> /                  Sony · iPhone · GoPro · Insta360 · Drone · …
            Horizontal | Vertical    orientation kept at the LEAF (most granular)
   _Sidecars/                        orphan sidecars only
   _MediaHub_manifest.csv
```

Decisions:
- **Location → date → raw/edited → media → device → orientation.** Orientation is
  the most granular/aesthetic attribute, so it lives at the leaf and never
  fragments the tree.
- **`images` merges photos+raw** (DNG/ARW/JPEG/HEIC together) so RAW+JPEG pairs stay together.
- **Year-Month from EXIF capture date** (most common month per trip), not folder
  names or filesystem timestamps (the inventory's `created_time` is unreliable).
- **Functional sidecars (`.xmp/.aae/.srt`) are copied into the SAME leaf as their
  parent media** (matched by directory + basename), so Lightroom/Photos/telemetry
  keep auto-linking. Orphans fall back to `_Sidecars/`.
- **Skipped entirely**: regenerable proxies (`.thm/.lrf/.lrv/.bin/.int/.bdm`) and
  macOS junk (`._*` AppleDouble, `.DS_Store`).

---

## 7. Staging engine (the hard part)

Designed around the **two-drives-at-a-time** constraint.

- **Plan / execute separation.** `plan_stage` builds a persistent job: one item
  per unique file (by sha256), each with *all* source-drive candidates and a
  fixed destination path; state ∈ `pending|copied|verified|skipped|error`.
- **Mount-aware canonical selection.** A unique file may exist on several drives.
  The engine copies it from whichever candidate is *currently mounted*, so the
  ~1.3 TB of cross-drive duplicates rarely force an extra swap.
- **Drive-identity resolution (`drives.py`).** Source files are located via a
  resolver, not raw `/Volumes/<name>` matching: **name → learned UUID → learned
  mount → content fingerprint** (top-folder overlap ≥ 50%) **→ user choice**. A
  *renamed/remounted* drive is recognized and its paths are rewritten onto the
  current mount; confident matches are persisted to `drives.json`. Resolution is
  read-only (lists volumes + reads top-level folder names only).
- **Swap loop.** Copy all pending items whose source is resolvable/mounted; if
  remaining files live only on an absent drive, **pause** and ask for the drive
  that unblocks the most files (greedy → fewest swaps). UI shows a modal sheet
  with Continue / Skip, the per-state counts, and the **next recommended drive**.
- **Resumable & idempotent.** State persists to `stage_job.json`; already-verified
  files (present at destination with matching size) are skipped on resume — safe
  across drive swaps *and* app restarts.
- **Errors & retry.** Failed files carry a reason + resolved source path; the UI
  lists them with a **Retry failed files** action (re-resolves drives) and an
  errors-CSV export. A verification mismatch is always an `error`, never success.
- **Integrity & manifests.** After each copy: size check + optional SHA-256 re-hash
  vs the inventory hash. Every (non-dry) pass writes the full audit set next to the
  staged trip — `_MediaHub_manifest.csv` / `_MediaHub_manifest.json` /
  `_MediaHub_errors.csv` / `_MediaHub_stage_summary.json` — with rich per-row
  fields (original + resolved-source + destination paths, inventory & verified
  sha256, copy/verify status, device, stage, bucket, orientation, trip, sidecar
  relationship, timestamp, error). Completed jobs are also archived to
  Application Support for later audit.
- **Copy-only**: uses `shutil.copy2`; never moves/deletes. Dry-run plans without copying.

---

## 8. Safety model

The strongest guarantee is structural, not procedural:
- **No code ever deletes, moves, or renames a media file** (audited: no
  `os.remove`/`unlink`/`rmtree`/`move`/`rename` on source paths). File writes are
  `shutil.copy2` (source→dest) only.
- The inventory DB is **read-only during normal use**. The only writes are
  ingest (`INSERT/UPDATE`) and **reconcile** (below) — both explicit user actions.
- **Reconcile / Trash (soft-delete with recovery window):** when you delete files
  from disk, "Scan for deleted files" finds them (drive-aware: only files whose
  parent folder exists on a *currently-mounted* drive; refuses to act if nothing
  resolves on this machine). Pruning is a **soft-delete** — rows get `deleted_at`
  set and are hidden everywhere, but stay **recoverable in Trash** for
  `trash_retention_days` (default 30). Restore clears the flag; purge (manual or
  auto-after-retention) hard-deletes the row + derived AI rows. This only ever
  touches *database rows*, never media files.
- **De-dup is a report**: `dedupe_plan_rows` produces a downloadable CSV marking
  which copies are redundant; the app never acts on it. Deletion is the user's,
  outside the app.
- **Confirmation before a real copy**: the UI gates a non-dry copy behind a
  modal (trip, destination, free space); dry runs are one-click.
- Recommended flow: **Preview → Dry run → Confirm → Copy → verify → (you) delete**.

---

## 9. AI features (on-device, optional)

### 9a. Apple Vision content tagging
- A Swift CLI (`vision_tag`) uses the Vision framework / Neural Engine for scene
  classification + OCR; `vision_enrich.py` selects the "vague pile" (photos with
  no EXIF make), tags content, and writes `vision_tags.sqlite3` with suggested
  buckets (Documents/People/Nature/Wildlife/Cityscape/Food/Vehicles).
- Honest scope: Vision classifies **content, not capture device**.

### 9b. CLIP semantic search
- **Pluggable embedder** (`embed/clip_embed.py`), identical I/O for both backends:
  - **`stub`** (default, zero-dep): signed hashed bag-of-words over text and file
    paths → instant, offline, works with drives unmounted. Effectively a
    filename/path search; also the fully-testable reference path.
  - **`mlx`** (Apple Silicon): true CLIP via `mlx-clip` — text and image embeddings
    share a space, so queries match **pixels** ("drone shot of a waterfall").
- **RAW-aware** (critical: ~56% of this archive is RAW-only): candidate selection
  is **per-shot** (folder+stem), preferring a JPEG/HEIC but keeping RAW when there's
  no viewable sibling; the `mlx` backend decodes RAW to a temp JPEG via macOS
  `sips`/ImageIO (handles ARW/DNG/CR3/GPR) before embedding.
- **Store + search** (`search.py`): one normalized vector per shot in
  `embeddings.sqlite3`; query text is encoded with the same backend; cosine
  ranking uses NumPy if present, else a pure-Python fallback; hits are enriched
  with trip/device/filename.
- Backend via `MEDIAHUB_EMBED_BACKEND` (default `stub`); embed job is
  folder-scopable so you can index a drive/trip at a time.
- **Relevance floor**: results below a per-backend cosine floor (stub 0.04, mlx
  0.18) are dropped, so an unrelated query returns "no strong matches" instead of
  dumping every embedded image.
- **Auto-embed on stage**: when a real copy finishes, the staged destination folder
  is embedded automatically (best-effort, non-blocking) so search stays current.

### 9c. Near-duplicate / burst culling + best-shot (`ai.py`)
Report-only. Reuses the CLIP vectors: groups visually near-identical shots **within
each trip** (per-trip blocking avoids an N² blow-up) via connected-components over
thresholded cosine (UI presets 0.85–0.95). Picks a **best-shot** to keep
(most pixels → largest file → RAW original) and marks the rest *review*. Emits a
downloadable CSV plan (`/api/ai/near-duplicates.csv`). Never deletes — it only
suggests, consistent with the dedupe report.

### 9d. Natural-language query parsing (`ai.parse_query`)
Heuristic (no model): pulls device / orientation / year / month tokens out of a
query (e.g. *"drone sunset 2024"* → `device=Drone, year=2024`, residual text
`sunset`), and `search()` masks candidates by those filters **before** ranking. An
Apple Foundation Models (Tahoe, Swift) upgrade can replace the heuristic later.

### 9e. Photo captions (`captions.py`)
Turns images into searchable descriptions, stored in `captions.sqlite3`:
- **`stub`** (default, zero-dep): synthesizes a caption from Vision tags + OCR text
  + folder/filename tokens — searchable immediately.
- **`mlxvlm`** (Apple Silicon): a real vision-LLM caption per image via `mlx-vlm`
  (model `MEDIAHUB_VLM_MODEL`, default Qwen2-VL-2B-4bit), RAW decoded via `sips`.
Caption search is keyword/substring over the stored captions.

### 9f. Faces / People (`faces.py` + `vision/face_detect.swift`)
On-device, private. The Swift tool detects faces (Vision) and emits a per-face
**feature print** (L2-normalized); Python clusters them by cosine (greedy, 0.82)
into person groups stored in `faces.sqlite3`. No model download — only `swiftc`
once to build the helper. Honest caveat: Apple exposes no public face-identity
embedding, so the cropped-face image feature print is a practical proxy — good for
grouping obvious same-person shots, not forensic identity.

### On-device dependency management (`deps.py`)
Optional accelerators (numpy, mlx-clip, mlx-vlm) install into a **private virtual
environment** under Application Support (`pyenv/`) — sidestepping PEP 668 on
Homebrew/system Python and never touching system packages. The venv's site is added
to `sys.path` so installs apply without a relaunch; subprocess tools use the venv's
Python. Installed via one-click bundles in **Search → On-device components**.

---

## 10. HTTP API (stable contract)

`GET`  `/` · `/index.html` · `/app.js` · `/styles.css` (static, no-store)
`GET`  `/api/summary` · `/api/trips` · `/api/sources` · `/api/mounts` · `/api/settings`
`GET`  `/api/stage/preview?trip=` · `/api/stage/status` · `/api/ingest/status`
`GET`  `/api/vision/status` · `/api/embed/status` · `/api/search?q=&k=`
`GET`  `/api/dedupe-plan.csv` · `/api/dedupe-plan/summary`
`GET`  `/api/drives/identity` · `/api/stage/errors` · `/api/stage/errors.csv` ·
       `/api/stage/manifest` · `/api/manifests`
`GET`  `/api/paths` · `/api/logs/tail` · `/api/logs/export`
`GET`  `/api/ai/near-duplicates?threshold=` · `/api/ai/near-duplicates.csv` ·
       `/api/ai/screenshot-sort`
`GET`  `/api/ai/caption/status` · `/api/ai/caption/search?q=&limit=`
`GET`  `/api/ai/faces/status` · `/api/ai/faces/people`
`GET`  `/api/reindex/status` · `/api/reindex/trash`
`GET`  `/api/deps/status` · `/api/thumb?path=`
`POST` `/api/stage/start` {trip, dry_run, verify_hash} · `/api/stage/continue` ·
       `/api/stage/skip-drive` {drive} · `/api/stage/retry-errors`
`POST` `/api/settings` · `/api/override` · `/api/ingest` {path} ·
       `/api/vision/start` {limit, only_unsorted, under, folder} ·
       `/api/embed/start` {backend, under, folder} · `/api/pick` {kind, prompt} ·
       `/api/drives/resolve` {device_name, mount}
`POST` `/api/ai/caption/start` {backend, under, folder} ·
       `/api/ai/faces/start` {under, folder} ·
       `/api/reindex/scan` {prune} · `/api/reindex/restore` {ids} ·
       `/api/reindex/purge` {ids|expired} · `/api/immich/export` ·
       `/api/deps/install` {bundle|packages|package} ·
       `/api/open` {path, reveal}

All responses carry an `X-App: MediaHub` header (used for instance detection by
both the CLI launcher and the native shell).

---

## 11. Key design decisions & rationale

- **Python stdlib only (core), no Docker.** Zero-install portability; full drive
  access; native Vision/Neural Engine (impossible in Docker's Linux VM); fast
  multi-TB copies. AI deps (MLX, NumPy, exiftool, swiftc) are optional and isolated.
- **Browser UI, not Electron/native.** No build step, fast iteration, trivially
  portable. Tradeoff: it's a browser tab; a thin WKWebView wrapper is a possible
  future upgrade.
- **Subprocess AI.** Keeps the long-running server light and stdlib; the model
  lives in short-lived child processes (or a future warm daemon).
- **Plan/execute split + persistent job.** The only robust way to handle drive
  swaps and resume.
- **Pluggable embedder with a zero-dep default.** The search UI works for everyone
  immediately and upgrades to true visual search by flipping a backend — and it
  made the whole pipeline testable without a model.
- **Epoch-based memoization.** The inventory is static between ingests, so derived
  views (summary/trips/sources/dedupe) are cached against a `data_epoch` that
  bumps on ingest/override changes; startup warms them in a background thread.

---

## 12. Performance

- `all_files()` is the one heavy read (~82k rows joined + per-row classify, ~4 s
  cold); cached for the process lifetime.
- Derived queries are epoch-memoized → warm reads are ~0 ms (HTTP ~1 ms after warmup).
- Semantic search: NumPy matrix-multiply for cosine (instant); pure-Python
  fallback ~0.5 s over 12–36k vectors.
- Heaviest real cost is the one-time `mlx` embedding pass (RAW decode + CLIP);
  mitigated by per-folder scoping and (future) embedded-preview extraction.

---

## 13. Packaging & deployment

- **`MediaHub.app` (native shell)**: a SwiftUI + WKWebView app
  (`shell/MediaHubShell.swift`, compiled with `swiftc`) that starts/attaches the
  bundled Python backend (`python3 -m mediahub`, browser suppressed via
  `MEDIAHUB_NO_BROWSER`), hosts the UI in a real window, and stops the backend on
  quit (only if it started it). No terminal, no browser tab. The Python engine is
  bundled at `Contents/Resources/app/`.
- **`MediaHub.app` (script launcher)**: an alternative bundle whose launcher runs
  `python3 -m mediahub` and opens the browser — kept as a fallback.
- **Robust launch**: binds the port authoritatively, walking forward on conflict
  (never crashes with Errno 48); an already-running instance is detected via the
  `X-App` header and reused instead of duplicated.
- **Portable zip**: `MediaHub_portable.zip` (package + embed + vision + shell +
  launcher + README + DESIGN) for moving to the other Mac; run `python3 -m mediahub`.
- **Auto-find DB**: `media_indexer.sqlite3` next to the package, in Application
  Support, or `~/Desktop/MediaIndexer_Package/`; override with `MEDIAHUB_DB`.

### Hosting (Mac mini as always-on node) — future
- Bind localhost + reach it via **Tailscale** (private, no public exposure), or
  LAN via `0.0.0.0`. ⚠️ The app currently has **no authentication** — exposing it
  beyond localhost requires adding a token/password first. Never port-forward it
  publicly. A `launchd` agent keeps it always-on.

---

## 14. Known limitations / tradeoffs

- **Unsigned app** → Gatekeeper prompt (code-signing needs a paid Apple account).
- **No auth** → localhost-only is safe; hosting needs auth added first.
- **`stub` semantic search is lexical** (path/filename); true visual search needs
  the `mlx` backend + an embedding pass with drives mounted.
- **Drive identity** is now resolver-based (name → UUID → fingerprint → user), so
  a renamed/remounted drive is recognized. Caveat: the *indexer* doesn't yet record
  the volume UUID at scan time, so first-time recognition of a renamed drive relies
  on the content fingerprint (capturing UUID during indexing would make it exact).
- **RAW**: CLIP can't read RAW directly; decoded via `sips` (full decode — slower
  than extracting the embedded preview, a future optimization).
- Trip date for catch-all groups (card dumps) that span months is approximate.

---

## 15. Suggested future work

**Shipped since the last revision (on-device AI suite, §9c–9f):** near-duplicate /
burst culling + best-shot, natural-language query parsing, photo captions
(stub + MLX-VLM), Faces/People clustering, auto-embed on stage, thumbnails +
Open/Reveal in results, relevance floor, and a private-venv dependency installer.
Plus, from Immich learnings: **drive-aware reconcile with soft-delete + a Trash
recovery window** (restore/purge, auto-purge after retention), an
**auto-reconcile-on-launch** setting (off by default), and an **Immich
external-library export** helper.

**Deliberately deferred (with rationale):**
- *pgvector / FAISS vector index* — NumPy cosine is instant at ~36k shots; a vector
  DB only earns its complexity in the millions. Revisit if latency becomes real.
- *Full background job-queue refactor* — the current per-feature thread+status
  jobs work and are observable enough; a unified queue is churn with little payoff
  at single-user scale.

Remaining:
1. **Code-signing / notarization + security-scoped bookmarks** (Priority 6) — drop
   the Gatekeeper prompt and formalize drive/folder permissions.
2. **Auth + Tailscale/LAN hosting** on the Mac mini (prerequisite for remote use).
3. **Capture volume UUID during indexing** so renamed-drive recognition is exact
   (today it falls back to content fingerprint).
4. **Embedded-preview RAW extraction** for much faster `mlx` embedding/captioning.
5. **Native `NSOpenPanel` picker bridge** (WKWebView ↔ Swift) — today the shell
   uses the backend's `osascript` picker, which works but isn't a Swift bridge.
6. **Apple Foundation Models** (Tahoe, Swift) to upgrade the heuristic NL query
   parser into true intent parsing + trip summaries.
7. **Guarded delete-executor** for the dedupe / culling plans (dry-run + typed
   confirmation), if the user ever wants the app to act on duplicates.

Done so far: Priorities 1–5 (drive identity · errors/retry · manifests+verification ·
native shell · storage/logs). See the status block at the top.

---

## 16. Review prompts for an external LLM

- Is the **plan/execute + persistent job** model the simplest correct design for
  resumable, swap-aware copying? Any race conditions in the swap loop?
- Is the **organization scheme** (location→date→raw/edited→media→device→orientation,
  sidecars-with-media, proxies skipped) sound for a 36k-shot, RAW-heavy archive?
- Is **stdlib + subprocess AI** the right portability/maintainability tradeoff vs.
  a packaged app or a small service?
- Is the **safety model** (no delete code, RO DB, copy-only, confirm-before-copy)
  sufficient, or should the manifest/verification be stronger?
- For **semantic search at this scale**, is per-shot CLIP with `sips` RAW decode +
  NumPy cosine adequate, or is a vector index (FAISS/sqlite-vec) warranted?
