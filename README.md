# MediaHub

A small, portable web app that turns your photo/video drives into an organized,
de-duplicated archive. It is the whole pipeline:

**Ingest → Index → De-duplicate → Organize by trip & device → Stage to your SSD/NAS.**

It uses **only the Python standard library** — no installs, no Node, no Electron,
no `pandas`. Runs on any Mac with Python 3.8+. The UI follows Apple's Human
Interface Guidelines (sidebar app, light/dark mode).

## Safety

- Your original files are **never modified, moved, or deleted**.
- Staging only **copies** to your chosen destination.
- The de-dup plan is a **report** (`dedupe_plan.csv`) you review and act on yourself.

## What's in this folder

| File | Purpose |
|------|---------|
| `MediaHub.app` | Double-click bundle (in `/Applications`) |
| `mediahub/` | The app package (`config`, `db`, `classify`, `settings`, `trips`, `dedupe`, `staging`, `ingest`, `mounts`, `server`, `__main__`) |
| `mediahub/ui/` | Browser interface — `index.html`, `styles.css`, `app.js` (Liquid-Glass / Apple HIG) |
| `Start MediaHub.command` | Terminal launcher (alternative to the .app) |
| `media_indexer.sqlite3` | The inventory DB (shared with the indexer) |
| `vision/` | Optional Apple Vision content-tagging module (Swift + Python) |

Runtime state (`settings.json`, `stage_job.json`, `trip_overrides.json`) lives in
`~/Library/Application Support/MediaHub/`, not in the app bundle.

## Run it

**Option A — double-click app (recommended):**
- Double-click **`MediaHub.app`**. First launch: right-click → **Open** (unsigned app,
  so macOS asks once). It starts the server and opens your browser automatically.
- The app is self-contained (the `mediahub` package lives inside the bundle). Runtime
  state (`settings.json`, `stage_job.json`, `trip_overrides.json`) is stored in
  `~/Library/Application Support/MediaHub/`, so the app itself stays read-only.
- **Robust launch:** if MediaHub is already running it just reopens the browser to the
  existing instance; if the port is taken by something else it automatically uses the
  next free port — it never crashes on a busy port.
- Logs: `~/Library/Logs/MediaHub.log`.

**Option B — script / module:**
- `cd ~/Desktop/MediaHub && python3 -m mediahub`, then open `http://127.0.0.1:8765`.

**Database location:** MediaHub auto-finds `media_indexer.sqlite3` next to the package,
in `~/Library/Application Support/MediaHub/`, or in `~/Desktop/MediaIndexer_Package/`.
Override with `MEDIAHUB_DB=/path/to/media_indexer.sqlite3`.

### Rebuilding the app after code changes
Edit files in `~/Desktop/MediaHub/mediahub/`, then refresh the bundled copy:
```bash
rm -rf "/Applications/MediaHub.app/Contents/Resources/app/mediahub"
cp -R ~/Desktop/MediaHub/mediahub "/Applications/MediaHub.app/Contents/Resources/app/mediahub"
```
Quit and relaunch the app to pick up changes (UI and code are read at startup).

## Why not Docker?

This app is deliberately **native, not containerized**, because its value is
native macOS integration:

- **Apple Vision / Neural Engine** don't exist inside Docker's Linux VM — you'd
  lose content tagging entirely.
- **Drive hot-swap** (mount two, swap to the next) is unreliable through a
  container's bind-mount snapshot of `/Volumes`.
- **Multi-TB copies** are far slower through Docker's file-sharing layer.
- There's almost nothing to install anyway (stdlib only; Vision compiles with
  the bundled Swift toolchain).

A headless Linux/Docker build would only make sense for a NAS-side, Vision-less
scheduled-dedupe service — a separate tool from this Mac workflow.

## The workflow

1. **Settings** — choose the destination: **Local SSD** or **Mounted drive / NAS**,
   set the path, and (optionally) enable **SHA-256 verify after copy**. Shows free
   space at the destination.
2. **New Trip Ingest** — point it at a freshly-offloaded card/folder/drive. It
   scans, hashes, reads EXIF (if `exiftool` is installed), and merges into the same
   database — instantly available everywhere else. **No manual indexer steps.**
3. **Overview / Trips / Sources / Duplicates** — review what you have.
4. **Stage to Destination** — pick a trip, optionally **Preview** the folder tree,
   then **Start**. Files copy into the organized layout and are verified.
5. Repeat per trip. With a NAS destination you can stage straight to it; with a
   local SSD you stage there and upload later.

### Two-drives-at-a-time? The swap-aware engine handles it

Staging is **resumable and swap-aware**:

- It picks one canonical copy of each unique file from **whichever drive is
  currently mounted** — so cross-drive duplicates never force an extra swap.
- When the only remaining files live on an unmounted drive, it **pauses and shows
  a sheet**: *"Insert 'Past T9' — 142 files / 88 GB remaining."* Plug it in, click
  **Continue**, and it resumes. **Skip this drive** defers those files.
- State is saved to `stage_job.json`, so swaps — and even app restarts — resume
  cleanly. Already-copied files are never re-copied (idempotent).

## The recommended per-trip loop

Dry run → Preview → Start → (swap drives when prompted) → verify on NAS →
delete originals using `dedupe_plan.csv` → next trip.

## NAS organization structure it produces

Each trip is organized **place-first**, then by date, then by stage (camera
original vs. your edited exports), media kind, capture device, and finally
orientation — so DNG/JPEG/ARW stills live together, Sony / iPhone / GoPro /
Insta360 / Drone never get mixed, and originals stay separate from edits:

```
<destination>/
└── <Location>/                     e.g. Iceland
    └── <Year>/                     e.g. 2023
        └── <YYYY-MM>/              e.g. 2023-09   (from EXIF capture date)
            ├── raw/                camera originals
            │   ├── images/         all stills together (ARW, DNG, JPEG, HEIC, ...)
            │   │   └── <Device>/{Horizontal,Vertical}     Sony · iPhone · GoPro · Drone · ...
            │   ├── videos/
            │   │   └── <Device>/{Horizontal,Vertical}
            │   └── other/<Device>/                         non-visual files (no H/V)
            ├── edited/             your exports (any source)
            │   ├── images/<Device>/{Horizontal,Vertical}
            │   └── videos/<Device>/{Horizontal,Vertical}
            ├── _Sidecars/          orphan sidecars only (.xml, unpaired .srt, ...)
            └── _MediaHub_manifest.csv
```

Order follows DAM best practice (Fstoppers / Peter Krogh / Icon Photography
School) adapted to a **location-first** preference: place groups every visit
together, then **date** (`Year` → `YYYY-MM` from EXIF), then `raw` vs `edited`,
then `images` vs `videos`, then **device**, with the **Horizontal/Vertical**
split kept at the *leaf* (the most granular attribute, so it never fragments the
tree). `Unknown` is used when device or dimensions are missing. Year-Month comes
from the **EXIF capture date** (most common month for the trip).

**Sidecars stay with their media.** Functional sidecars (`.xmp`, `.aae`, `.srt`)
are copied into the **same leaf folder as the file they belong to** (matched by
directory + basename), so Lightroom/Photos/telemetry keep auto-linking them.
Only unpaired/orphan sidecars (e.g. AVCHD `.xml`) fall back to `_Sidecars/`.

**Skipped automatically:** regenerable proxies (`.thm`, `.lrf`, `.lrv`, `.bin`,
`.int`, `.bdm`) and macOS junk (`._*` AppleDouble stubs, `.DS_Store`) are never
staged.

**How sources are detected** (in order): EXIF camera make/model → file extension
(`.insv`=Insta360, `.gpr`=GoPro, `.arw`=Sony, `.heic`=iPhone) → filename
(`DJI_`, `GX/GH/GOPR`, `IMG_`, `DSC`) → `AbsoluteAltitude` present = aerial/Drone.

**Edited vs original**: editing-software EXIF (Lightroom/Topaz/Premiere/etc.),
filename hints (`-edit`, `export`, `final`, `pano`, `hdr`, `select`), or an
EDITED/EXPORT/ProRes parent folder.

Use the **Stage → Preview layout** button to see the exact tree and per-folder
sizes for a trip before copying.

## Optional: Apple Vision content tagging (M-series)

For the **vague pile** — files with no EXIF camera make and generic names (your
`UNORGANIZED` folder, ~11k "Unknown" files) — `vision/` uses Apple's Vision
framework on the Neural Engine to tag image *content* and find documents.

> Vision identifies **content** (scene/objects/text), **not the capture device**.
> Device sorting is done from EXIF (above). Vision only helps triage unsorted
> images by what's *in* them.

```bash
cd ~/Desktop/MediaHub/vision
python3 vision_enrich.py --limit 2000        # tag a sample
python3 vision_enrich.py --all               # tag every vague image
python3 vision_enrich.py --only-unsorted     # just the UNORGANIZED pile
```

- Builds the Swift tool automatically (needs Xcode Command Line Tools:
  `xcode-select --install`).
- Writes results to `vision/vision_tags.sqlite3` — the inventory DB is never
  touched.
- Prints suggested content buckets: Documents/Screenshots, People, Nature,
  Wildlife, Cityscape, Food, Vehicles. Review the `vision_tags` table to triage;
  no files are moved automatically.

Tunable via env: `VISION_MIN_CONF=0.15`, `VISION_TOP_N=5`.

## Notes

- **Sources** are mounted volumes under `/Volumes/<name>` (matching the inventory).
  The sidebar shows what's connected. The swap-aware engine copies what it can from
  mounted drives, then prompts for the next one.
- **Ingest** uses `exiftool` if it's installed (`brew install exiftool`) for camera
  make/model/altitude. Without it, files are still indexed and device is inferred
  from extension/filename.
- Re-grouping: trips are auto-classified from folder names. Change the keyword rules
  in `mediahub/classify.py` (`RAW_RULES`) or save per-folder overrides.
- Override the DB or port: `MEDIAHUB_DB=/path/to.sqlite3 MEDIAHUB_PORT=8765 python3 -m mediahub`
