/* MediaHub UI logic — talks to the stable /api contract. */
"use strict";
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const fmt = n => n == null ? "–" : Number(n).toLocaleString();
const api = async (p, m, b) => {
  const r = await fetch(p, m ? {
    method: m, headers: { "Content-Type": "application/json" },
    body: JSON.stringify(b || {})
  } : undefined);
  return r.json().catch(() => ({}));
};

const TITLES = {
  overview: ["Overview", ""],
  guide: ["Guide", "How to use MediaHub — safely"],
  search: ["Semantic Search", "On-device · find photos by content"],
  trips: ["Trips", "De-duplicated and grouped across all drives"],
  sources: ["Capture Sources", "Sony · iPhone · GoPro · Insta360 · Drone"],
  dedupe: ["Duplicates", "Safe de-duplication plan"],
  stage: ["Stage to Destination", "Swap-aware, resumable copy"],
  ingest: ["New Trip Ingest", "Scan and merge new media"],
  vision: ["Vision & OCR", "On-device · text and scenes — Neural Engine"],
  culling: ["Smart Culling", "Near-duplicate & burst review — report only"],
  people: ["People", "On-device face grouping — private"],
  settings: ["Settings", ""]
};

let dryRun = false, destMode = "local";

/* ---------- routing ---------- */
$$(".navitem").forEach(n => n.onclick = () => {
  $$(".navitem").forEach(x => x.classList.remove("active"));
  $$(".view").forEach(x => x.classList.remove("active"));
  n.classList.add("active");
  $("#" + n.dataset.view).classList.add("active");
  const [t, s] = TITLES[n.dataset.view];
  $("#tbTitle").textContent = t;
  $("#tbSub").textContent = s;
  $("#tbAction").innerHTML = "";
  const v = n.dataset.view;
  if (v === "trips") loadTrips();
  if (v === "sources") loadSources();
  if (v === "dedupe") loadDedupe();
  if (v === "settings") { loadSettings(); loadRecon(); }
  if (v === "stage") loadTripsForStage();
  if (v === "vision") loadVision();
  if (v === "search") { loadEmbedInfo(); loadSetup(); loadCaptionInfo(); }
  if (v === "culling") loadScreenshotSort();
  if (v === "people") loadPeople();
});

/* ---------- overview ---------- */
async function loadSummary() {
  const s = await api("/api/summary");
  $("#s_files").textContent = fmt(s.total_files);
  $("#s_total").textContent = (s.total_gb ?? "–") + " GB";
  $("#s_unique").textContent = (s.unique_gb ?? "–") + " GB";
  $("#s_reclaim").textContent = (s.reclaim_gb ?? "–") + " GB";
  const tb = $("#devtbl tbody"); tb.innerHTML = "";
  (s.devices || []).forEach(d => tb.insertAdjacentHTML("beforeend",
    `<tr><td>${d.device_name}</td><td class="num">${fmt(d.files)}</td><td class="num">${d.gb}</td></tr>`));
}
async function loadMounts() {
  const m = await api("/api/mounts"); const box = $("#mountbox");
  box.innerHTML = '<div class="muted" style="margin-bottom:5px">Connected drives</div>' +
    (Array.isArray(m) && m.length
      ? m.map(v => `<div class="m"><span><span class="dot"></span>${v.name}</span><span>${v.free_gb ?? "–"} GB</span></div>`).join("")
      : '<div class="muted">none</div>');
}

/* ---------- trips ---------- */
async function loadTrips() {
  const tb = $("#tripstbl tbody");
  tb.innerHTML = '<tr><td colspan="7" class="muted">Loading…</td></tr>';
  try {
    const t = await api("/api/trips");
    if (!Array.isArray(t)) {
      tb.innerHTML = `<tr><td colspan="7" class="muted">Error: ${t && t.error ? t.error : "unexpected response"}</td></tr>`;
      return;
    }
    if (t.length === 0) {
      tb.innerHTML = '<tr><td colspan="7" class="muted">No trips found. Use New Trip Ingest to scan media.</td></tr>';
      return;
    }
    tb.innerHTML = "";
    t.forEach(x => tb.insertAdjacentHTML("beforeend",
      `<tr><td><b>${x.trip}</b></td><td><span class="chip ${x.category}">${x.category}</span></td>
        <td><code>${x.nas_folder}</code></td><td class="num">${fmt(x.unique_files)}</td>
        <td class="num">${x.unique_gb}</td><td class="num muted">${x.dup_gb}</td>
        <td>${(x.devices || []).map(d => `<span class="chip">${d}</span>`).join(" ")}</td></tr>`));
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="7" class="muted">Failed to load trips: ${e.message}</td></tr>`;
  }
}
async function loadTripsForStage() {
  loadDrives();
  const t = await api("/api/trips"); const sel = $("#tripSelect"); sel.innerHTML = "";
  (Array.isArray(t) ? t : []).forEach(x => sel.insertAdjacentHTML("beforeend",
    `<option value="${x.trip.replace(/"/g, "&quot;")}">${x.trip} — ${x.unique_gb} GB (${x.unique_files})</option>`));
  const st = await api("/api/settings");
  renderDestLine(st);
}
function renderDestLine(st) {
  const el = $("#destLine");
  if (el) el.innerHTML = `Destination: <code>${st.dest_path}</code> · ${st.dest_free_gb ?? "?"} GB free · mode <b>${st.dest_mode}</b>`;
}
async function loadSources() {
  const s = await api("/api/sources"); const tb = $("#srctbl tbody"); tb.innerHTML = "";
  (Array.isArray(s) ? s : []).forEach(d => tb.insertAdjacentHTML("beforeend",
    `<tr><td><b>${d.device}</b></td><td class="num">${fmt(d.files)}</td><td class="num">${d.gb}</td>
      <td class="num">${fmt(d.originals)}</td><td class="num">${fmt(d.edited)}</td></tr>`));
}
async function loadDedupe() {
  const d = await api("/api/dedupe-plan/summary");
  $("#d_files").textContent = fmt(d.delete_files);
  $("#d_reclaim").textContent = (d.reclaim_gb ?? "–") + " GB";
  const tb = $("#d_devtbl tbody"); tb.innerHTML = "";
  (d.by_device || []).forEach(x => tb.insertAdjacentHTML("beforeend",
    `<tr><td>${x.device}</td><td class="num">${x.reclaim_gb}</td></tr>`));
  $("#tbAction").innerHTML = '<a class="btn primary" href="/api/dedupe-plan.csv">Download plan CSV</a>';
}

/* ---------- stage: source drives ---------- */
async function loadDrives() {
  const d = await api("/api/drives/identity");
  const tb = $("#drivesTbl tbody"); tb.innerHTML = "";
  const mounts = d.mounted || [];
  (d.drives || []).forEach(dr => {
    let badge;
    if (dr.status === "matched")
      badge = dr.renamed ? '<span class="chip events" title="Recognized even though this drive is mounted under a different name. Ready to stage — nothing more to do.">matched (renamed) ✓</span>'
                         : '<span class="chip trips" title="Mounted under its original name. Ready to stage.">connected</span>';
    else if (dr.status === "ambiguous") badge = '<span class="chip camera-dumps" title="More than one drive could match — pick the right one and click Use this.">ambiguous</span>';
    else badge = '<span class="chip unsorted" title="Attach this drive, or pick the matching mounted volume and click Use this.">not connected</span>';
    let action = "";
    if (dr.status !== "matched" && mounts.length) {
      const opts = mounts.map(m => `<option value="${m.mount}">${m.name}</option>`).join("");
      action = `<select class="drvSel" data-dn="${dr.device_name}" style="min-width:130px">${opts}</select> <button class="btn bordered drvResolve" data-dn="${dr.device_name}">Use this</button>`;
    }
    const mountCell = dr.mount ? `<code>${dr.mount}</code>`
      : (dr.suggestion ? `<span class="muted">maybe ${dr.suggestion}</span>` : "");
    tb.insertAdjacentHTML("beforeend",
      `<tr><td><b>${dr.expected_name}</b></td><td>${badge}</td><td>${mountCell}</td>
        <td class="num">${fmt(dr.files)} / ${dr.gb}</td><td>${action}</td></tr>`);
  });
  $$(".drvResolve").forEach(b => b.onclick = async () => {
    const dn = b.dataset.dn;
    const sel = [...$$(".drvSel")].find(s => s.dataset.dn === dn);
    const r = await api("/api/drives/resolve", "POST", { device_name: dn, mount: sel ? sel.value : "" });
    if (r.error) alert(r.error); else loadDrives();
  });
}

/* ---------- stage: mode / verify ---------- */
$("#modeSeg").querySelectorAll("button").forEach(b => b.onclick = () => {
  $("#modeSeg").querySelectorAll("button").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); dryRun = b.dataset.dry === "true";
});
$("#previewBtn").onclick = async () => {
  const d = await api("/api/stage/preview?trip=" + encodeURIComponent($("#tripSelect").value));
  $("#previewBox").classList.remove("hidden");
  const tb = $("#pvtbl tbody"); tb.innerHTML = "";
  (d.tree || []).forEach(n => tb.insertAdjacentHTML("beforeend",
    `<tr><td><code>${n.path}</code></td><td class="num">${fmt(n.files)}</td><td class="num">${n.gb}</td></tr>`));
};
async function startStage() {
  $("#startBtn").disabled = true;
  const r = await api("/api/stage/start", "POST",
    { trip: $("#tripSelect").value, dry_run: dryRun, verify_hash: $("#verifyChk").checked });
  if (r.error) { alert(r.error); $("#startBtn").disabled = false; return; }
  $("#progBox").classList.remove("hidden"); pollStage();
}
$("#startBtn").onclick = async () => {
  if (dryRun) { startStage(); return; }          // dry run: no confirmation needed
  // Real copy: confirm first.
  const opt = $("#tripSelect").selectedOptions[0];
  $("#cc_trip").textContent = opt ? opt.textContent : $("#tripSelect").value;
  const st = await api("/api/settings");
  $("#cc_dest").textContent = st.dest_path + (st.dest_free_gb != null ? `  ·  ${st.dest_free_gb} GB free` : "");
  $("#copyConfirm").classList.add("show");
};
$("#cc_cancel").onclick = () => $("#copyConfirm").classList.remove("show");
$("#cc_confirm").onclick = () => { $("#copyConfirm").classList.remove("show"); startStage(); };
async function pollStage() {
  const s = await api("/api/stage/status");
  if (s.status === "idle") { $("#startBtn").disabled = false; return; }
  $("#pg_trip").textContent = s.trip || "";
  $("#pg_mode").textContent = s.dry_run ? "DRY RUN" : "COPY";
  $("#pg_counts").textContent = `${fmt(s.done_files)}/${fmt(s.total_files)} files · ${s.done_gb}/${s.total_gb} GB`;
  $("#pg_states").textContent =
    `${fmt(s.done_files)} copied · ${fmt(s.pending_files)} pending · ${fmt(s.skipped_files)} skipped · ${fmt(s.error_files)} errors`;
  $("#pg_bar").style.width = (s.pct || 0) + "%";
  $("#pg_current").textContent = s.current ? ("Copying: " + s.current) : "";

  // errors panel + retry
  const ep = $("#pg_errors");
  if (s.error_files > 0) {
    ep.classList.remove("hidden");
    $("#pg_errcount").textContent = `${fmt(s.error_files)} file(s) failed`;
    const tb = $("#pg_errtbl tbody"); tb.innerHTML = "";
    (s.errors_sample || []).forEach(e => tb.insertAdjacentHTML("beforeend",
      `<tr><td>${e.file}</td><td class="muted">${e.error || ""}</td></tr>`));
    if (s.error_files > (s.errors_sample || []).length)
      tb.insertAdjacentHTML("beforeend",
        `<tr><td colspan="2" class="muted">… and ${fmt(s.error_files - (s.errors_sample || []).length)} more — download the report.</td></tr>`);
  } else {
    ep.classList.add("hidden");
  }

  const dr = $("#pg_drives");
  if (s.status === "awaiting_drive") {
    $("#swapDrive").textContent = s.awaiting_drive;
    const d = (s.drive_remaining || []).find(x => x.device === s.awaiting_drive);
    $("#swapRemain").textContent = d ? `${d.files} files · ${d.gb} GB remaining on this drive` : "";
    $("#swapSheet").classList.add("show");
    dr.innerHTML = "";
  } else {
    $("#swapSheet").classList.remove("show");
    // next recommended drive hint while running
    dr.innerHTML = (s.next_drive && s.status !== "done")
      ? `<div class="note warn">Next: connect <b>${s.next_drive}</b> to continue the remaining files.</div>` : "";
  }

  if (s.status === "done") {
    $("#pg_msg").textContent = s.message; $("#pg_msg").classList.remove("hidden");
    $("#pg_msg").className = s.error_files ? "note warn" : "note green";
    $("#startBtn").disabled = false;
    if (s.skipped_files)
      dr.innerHTML = `<div class="note warn">${s.skipped_files} files skipped (drive unavailable).</div>`;
    loadMounts(); return;
  }
  setTimeout(pollStage, 700);
}
$("#pg_retry").onclick = async () => {
  $("#pg_retry").disabled = true;
  await api("/api/stage/retry-errors", "POST", {});
  $("#pg_retry").disabled = false;
  pollStage();
};
$("#swapContinue").onclick = async () => {
  $("#swapSheet").classList.remove("show"); await api("/api/stage/continue", "POST"); pollStage();
};
$("#swapSkip").onclick = async () => {
  const d = $("#swapDrive").textContent;
  $("#swapSheet").classList.remove("show"); await api("/api/stage/skip-drive", "POST", { drive: d }); pollStage();
};

/* ---------- native folder/file picker ---------- */
async function pickInto(inputSel, kind, prompt) {
  const r = await api("/api/pick", "POST", { kind: kind || "folder", prompt: prompt || "" });
  if (r.path) $(inputSel).value = r.path;
  else if (r.error) alert("Picker error: " + r.error);
}

/* ---------- ingest ---------- */
$("#ingestBrowse").onclick = () => pickInto("#ingestPath", "folder", "Choose a folder or drive to index");
$("#ingestBtn").onclick = async () => {
  const p = $("#ingestPath").value.trim(); if (!p) { alert("Enter a path"); return; }
  $("#ingestBtn").disabled = true;
  const r = await api("/api/ingest", "POST", { path: p });
  if (r.error) { alert(r.error); $("#ingestBtn").disabled = false; return; }
  pollIngest();
};
async function pollIngest() {
  const s = await api("/api/ingest/status");
  $("#ingestLog").textContent = s.log || ("[" + s.status + "]");
  $("#ingestLog").scrollTop = $("#ingestLog").scrollHeight;
  if (s.status === "running") { setTimeout(pollIngest, 800); }
  else { $("#ingestBtn").disabled = false; if (s.status === "done") loadSummary(); }
}

/* ---------- search ---------- */
async function doSearch() {
  const q = $("#searchQ").value.trim();
  if (!q) return;
  $("#searchInfo").textContent = "Searching…";
  const d = await api("/api/search?k=40&q=" + encodeURIComponent(q));
  const card = $("#searchResultsCard"), tb = $("#searchTbl tbody"); tb.innerHTML = "";
  if (d.error) {
    $("#searchInfo").textContent = d.error;
    card.classList.add("hidden");
    return;
  }
  $("#searchInfo").innerHTML = `${(d.results || []).length} results · backend <b>${d.backend}</b> · ${fmt(d.embedded)} images indexed`
    + (d.parsed && Object.keys(d.parsed.filters || {}).length
       ? ` · filters: ${Object.entries(d.parsed.filters).map(([k, v]) => `${k}=${v}`).join(", ")}` : "");
  if (!(d.results || []).length) {
    if (d.note) $("#searchInfo").innerHTML += `<br><span class="muted">${d.note}</span>`;
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  const esc = t => (t || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  d.results.forEach(r => tb.insertAdjacentHTML("beforeend",
    `<tr>
      <td><img class="thumb" src="/api/thumb?path=${encodeURIComponent(r.path)}" loading="lazy"
           style="width:84px;height:60px;object-fit:cover;border-radius:7px;background:var(--fill);cursor:pointer" data-path="${esc(r.path)}"></td>
      <td class="num">${r.score}</td>
      <td><b>${esc(r.file_name)}</b><div class="muted" style="font-size:11px"><code>${esc(r.path)}</code></div></td>
      <td>${r.trip ? `<span class="chip">${esc(r.trip)}</span>` : ""}</td>
      <td>${esc(r.device) || ""}</td>
      <td><div class="row" style="gap:6px;flex-wrap:nowrap">
        <button class="btn bordered openbtn" data-path="${esc(r.path)}">Open</button>
        <button class="btn revealbtn" data-path="${esc(r.path)}">Reveal</button>
      </div></td>
    </tr>`));
  const open = (p, reveal) => api("/api/open", "POST", { path: p, reveal: !!reveal });
  $$("#searchTbl .openbtn").forEach(b => b.onclick = () => open(b.dataset.path));
  $$("#searchTbl .revealbtn").forEach(b => b.onclick = () => open(b.dataset.path, true));
  $$("#searchTbl .thumb").forEach(t => t.onclick = () => open(t.dataset.path));
}
$("#searchBtn").onclick = doSearch;
$("#searchQ").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });

async function loadEmbedInfo() { renderEmbed(await api("/api/embed/status")); }
function renderEmbed(s) {
  s = s || {};
  const numpyBit = s.numpy
    ? ' · <span class="green">numpy ✓</span>'
    : ' · <button class="btn bordered" id="installNumpy" style="padding:3px 10px;font-size:11px">Install numpy (faster search)</button> <span id="numpyMsg" class="muted" style="font-size:11px"></span>';
  $("#embInfo").innerHTML = `<b>${fmt(s.embedded)}</b> / ${fmt(s.candidates)} images embedded · backend <b>${s.backend}</b>${numpyBit}`;
  const inb = $("#installNumpy");
  if (inb) inb.onclick = installNumpy;
  const note = $("#embBackendNote");
  if (s.backend === "mlx") {
    note.className = "note green";
    note.innerHTML = "Visual CLIP search active (MLX / Neural Engine) — queries match image <b>content</b>.";
  } else {
    note.className = "note accent";
    note.innerHTML = "Keyword/path search (stub backend) — works offline with no dependencies. For true visual search on your M5: <code>pip install mlx-clip</code>, set <code>MEDIAHUB_EMBED_BACKEND=mlx</code>, mount the drives, then Build embeddings.";
  }
  $("#embedLog").textContent = s.log || ("[" + (s.status || "idle") + "]");
  $("#embedLog").scrollTop = $("#embedLog").scrollHeight;
  $("#embedBtn").disabled = s.status === "running";
  $("#embedBtn").textContent = s.status === "running"
    ? `Embedding… ${fmt(s.done)}/${fmt(s.total)}` : "Build / update embeddings";
}
$("#embBrowse").onclick = async () => {
  await pickInto("#embUnder", "folder", "Choose a folder to embed");
  if ($("#embUnder").value.trim()) $("#emb_direct").checked = true;  // browsing ⇒ embed it directly
};
$("#embedBtn").onclick = async () => {
  $("#embedBtn").disabled = true;
  const folderVal = $("#embUnder").value.trim();
  const body = {};
  if ($("#emb_direct").checked && folderVal) body.folder = folderVal;
  else body.under = folderVal;
  const r = await api("/api/embed/start", "POST", body);
  if (r.error) { alert(r.error); $("#embedBtn").disabled = false; return; }
  pollEmbed();
};
async function pollEmbed() {
  const s = await api("/api/embed/status"); renderEmbed(s);
  if (s.status === "running") setTimeout(pollEmbed, 1000);
}

/* ---------- vision ---------- */
async function loadVision() { renderVision(await api("/api/vision/status")); }
function renderVision(s) {
  s = s || {};
  const av = s.available_info || {};
  $("#v_candidates").textContent = fmt(s.candidates);
  $("#v_tagged").textContent = fmt(s.tagged);
  const banner = $("#v_avail");
  if (av.available) {
    banner.className = "note accent";
    banner.innerHTML = "Apple Vision ready — runs on the Neural Engine. " +
      (av.has_binary ? "" : "The Swift tool will build automatically on first run.");
  } else {
    banner.className = "note warn";
    banner.innerHTML = "Vision unavailable: " + (av.reason || "") +
      " — install with <code>xcode-select --install</code>.";
  }
  const tb = $("#v_buckets tbody"); tb.innerHTML = "";
  if ((s.buckets || []).length) {
    s.buckets.forEach(b => tb.insertAdjacentHTML("beforeend",
      `<tr class="vbucket" data-bucket="${b.bucket}" style="cursor:pointer"><td>${b.bucket}</td><td class="num">${fmt(b.count)}</td></tr>`));
    $$(".vbucket").forEach(r => r.onclick = () => loadVisionResults(r.dataset.bucket));
  } else {
    tb.innerHTML = '<tr><td colspan="2" class="muted">No tags yet — run Vision to populate.</td></tr>';
  }
  $("#visionLog").textContent = s.log || ("[" + (s.status || "idle") + "]");
  $("#visionLog").scrollTop = $("#visionLog").scrollHeight;
  $("#visionBtn").disabled = s.status === "running" || !av.available;
}
$("#v_browse").onclick = async () => {
  await pickInto("#v_under", "folder", "Choose a folder to tag with Vision");
  if ($("#v_under").value.trim()) $("#v_direct").checked = true;  // browsing a folder ⇒ tag it directly
};
$("#visionBtn").onclick = async () => {
  const all = $("#v_all").checked;
  const limit = all ? 0 : (parseInt($("#v_limit").value, 10) || 2000);
  const folderVal = $("#v_under").value.trim();
  const body = { limit, only_unsorted: $("#v_unsorted").checked };
  if ($("#v_direct").checked && folderVal) body.folder = folderVal;
  else body.under = folderVal;
  $("#visionBtn").disabled = true;
  const r = await api("/api/vision/start", "POST", body);
  if (r.error) { alert(r.error); $("#visionBtn").disabled = false; return; }
  pollVision();
};
async function pollVision() {
  const s = await api("/api/vision/status"); renderVision(s);
  if (s.status === "running") setTimeout(pollVision, 1000);
}
let visionOpenBucket = null;
function highlightBucket(bucket) {
  $$(".vbucket").forEach(r => {
    r.style.background = (bucket && r.dataset.bucket === bucket) ? "rgba(120,200,255,.14)" : "";
  });
}
function renderVisionRows(results) {
  const card = $("#v_resultsCard"), tb = $("#v_resultsTbl tbody");
  card.classList.remove("hidden"); tb.innerHTML = "";
  const esc = t => (t || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  if (!(results || []).length) { tb.innerHTML = '<tr><td colspan="5" class="muted">No matches.</td></tr>'; return; }
  results.forEach(r => tb.insertAdjacentHTML("beforeend",
    `<tr>
      <td><img class="thumb" src="/api/thumb?path=${encodeURIComponent(r.path)}" loading="lazy"
           style="width:84px;height:60px;object-fit:cover;border-radius:7px;background:var(--fill);cursor:pointer" data-path="${esc(r.path)}"></td>
      <td><b>${esc(r.file_name)}</b><div class="muted" style="font-size:11px"><code>${esc(r.path)}</code></div></td>
      <td><span class="chip">${r.bucket || ""}</span></td>
      <td class="muted" style="font-size:11px">${esc((r.text || "").slice(0, 110))}</td>
      <td><div class="row" style="gap:6px;flex-wrap:nowrap">
        <button class="btn bordered openbtn" data-path="${esc(r.path)}">Open</button>
        <button class="btn revealbtn" data-path="${esc(r.path)}">Reveal</button>
      </div></td>
    </tr>`));
  const open = (p, reveal) => api("/api/open", "POST", { path: p, reveal: !!reveal });
  $$("#v_resultsTbl .openbtn").forEach(b => b.onclick = () => open(b.dataset.path));
  $$("#v_resultsTbl .revealbtn").forEach(b => b.onclick = () => open(b.dataset.path, true));
  $$("#v_resultsTbl .thumb").forEach(t => t.onclick = () => open(t.dataset.path));
}
async function loadVisionResults(bucket) {
  // Toggle: clicking the already-open bucket collapses the results panel.
  if (visionOpenBucket === bucket && !$("#v_resultsCard").classList.contains("hidden")) {
    $("#v_resultsCard").classList.add("hidden");
    visionOpenBucket = null;
    highlightBucket(null);
    return;
  }
  visionOpenBucket = bucket;
  highlightBucket(bucket);
  $("#v_textInfo").textContent = "";
  const d = await api("/api/vision/results?limit=400&bucket=" + encodeURIComponent(bucket));
  renderVisionRows(d.results);
}
$("#v_textBtn").onclick = async () => {
  const q = $("#v_textq").value.trim(); if (!q) return;
  visionOpenBucket = null; highlightBucket(null);  // text search owns the panel now
  $("#v_textInfo").textContent = "Searching OCR text…";
  const d = await api("/api/vision/search?limit=80&q=" + encodeURIComponent(q));
  if (d.error) { $("#v_textInfo").textContent = d.error; return; }
  $("#v_textInfo").innerHTML = `${(d.results || []).length} match(es) for <b>${q}</b>` +
    (d.matched_tokens ? ` · tokens: ${d.matched_tokens.join(", ")}` : "");
  renderVisionRows(d.results);
};
$("#v_textq").addEventListener("keydown", e => { if (e.key === "Enter") $("#v_textBtn").click(); });

/* ---------- settings ---------- */
$("#destSeg").querySelectorAll("button").forEach(b => b.onclick = () => {
  $("#destSeg").querySelectorAll("button").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); destMode = b.dataset.mode;
});
async function loadSettings() {
  const s = await api("/api/settings"); destMode = s.dest_mode;
  $("#destSeg").querySelectorAll("button").forEach(x => x.classList.toggle("on", x.dataset.mode === s.dest_mode));
  $("#destPath").value = s.dest_path; $("#setVerify").checked = s.verify_hash;
  if ($("#autoRecon")) $("#autoRecon").checked = !!s.auto_reconcile;
  $("#freeNote").innerHTML = `Free space at destination: <b>${s.dest_free_gb ?? "?"} GB</b>`;
  loadStorage();
  loadTrash();
}
async function loadStorage() {
  const p = await api("/api/paths");
  $("#pathsInfo").innerHTML =
    `App data: <code>${p.data_dir}</code> (${p.data_mb ?? "?"} MB)<br>` +
    `Logs: <code>${p.logs_dir}</code> · Manifests: <code>${p.manifests_dir}</code><br>` +
    `Database: <code>${p.db_path}</code>`;
  const lg = await api("/api/logs/tail");
  $("#appLog").textContent = lg.log || "(empty)";
  $("#appLog").scrollTop = $("#appLog").scrollHeight;
  const m = await api("/api/manifests");
  const tb = $("#manifestsTbl tbody"); tb.innerHTML = "";
  if (!(m.manifests || []).length) tb.innerHTML = '<tr><td colspan="5" class="muted">No staging jobs yet.</td></tr>';
  (m.manifests || []).forEach(x => tb.insertAdjacentHTML("beforeend",
    `<tr><td><code>${x.job}</code></td><td>${x.trip || ""}</td><td class="num">${fmt(x.verified_files)}</td>
      <td class="num">${fmt(x.error_files)}</td><td class="num">${x.verified_gb ?? ""}</td></tr>`));
}
$("#refreshLog").onclick = loadStorage;
$("#saveSettings").onclick = async () => {
  const s = await api("/api/settings", "POST",
    { dest_mode: destMode, dest_path: $("#destPath").value.trim(), verify_hash: $("#setVerify").checked });
  $("#savedMsg").textContent = "Saved.";
  $("#freeNote").innerHTML = `Free space at destination: <b>${s.dest_free_gb ?? "?"} GB</b>`;
  setTimeout(() => $("#savedMsg").textContent = "", 2000);
};

/* ---------- init ---------- */
loadSummary(); loadMounts(); loadTrips();
setInterval(loadMounts, 5000);

/* ---------- on-device components (private venv installer) ---------- */
function renderSetup(s) {
  s = s || {};
  const el = $("#setupStatus");
  if (el) {
    const pill = (label, ok) => `<span class="pill${ok ? " ok" : ""}">${ok ? "✓ " : ""}${label}</span>`;
    el.innerHTML = pill("private env", s.venv_ready) + pill("numpy (fast search)", s.numpy)
      + pill("MLX visual search", s.mlx_clip) + pill("MLX-VLM captions", s.mlx_vlm);
  }
  const running = s.status === "running";
  const a = $("#setupAccel"), v = $("#setupVision"), cp = $("#setupCaptions");
  if (a) { a.disabled = running || s.numpy; a.textContent = s.numpy ? "Faster search enabled ✓" : (running ? "Installing…" : "Enable faster search (numpy)"); }
  if (v) { v.disabled = running || s.mlx_clip; v.textContent = s.mlx_clip ? "Visual search enabled ✓" : (running ? "Installing…" : "Enable visual search (MLX CLIP)"); }
  if (cp) { cp.disabled = running || s.mlx_vlm; cp.textContent = s.mlx_vlm ? "AI captions enabled ✓" : (running ? "Installing…" : "Enable AI captions (MLX-VLM)"); }
  if (s.log) { const lg = $("#setupLog"); if (lg) { lg.style.display = "block"; lg.textContent = s.log; lg.scrollTop = lg.scrollHeight; } }
}
async function loadSetup() { renderSetup(await api("/api/deps/status")); }
async function installBundle(bundle) {
  const r = await api("/api/deps/install", "POST", { bundle });
  if (r.error) { const lg = $("#setupLog"); if (lg) { lg.style.display = "block"; lg.textContent = r.error; } return; }
  const poll = async () => {
    const s = await api("/api/deps/status");
    renderSetup(s);
    if (s.status === "running") { setTimeout(poll, 1500); return; }
    loadEmbedInfo();   // refresh the embeddings panel (numpy ✓ etc.)
  };
  poll();
}
{
  const a = $("#setupAccel"), v = $("#setupVision"), cp = $("#setupCaptions");
  if (a) a.onclick = () => installBundle("accel");
  if (v) v.onclick = () => installBundle("vision_search");
  if (cp) cp.onclick = () => installBundle("captions");
}

/* back-compat: the inline "Install numpy" button in the embeddings panel */
async function installNumpy() {
  const btn = $("#installNumpy"), msg = $("#numpyMsg");
  if (btn) { btn.disabled = true; btn.textContent = "Installing…"; }
  const r = await api("/api/deps/install", "POST", { package: "numpy" });
  if (r.error) {
    if (msg) msg.textContent = r.error;
    if (btn) { btn.disabled = false; btn.textContent = "Install numpy (faster search)"; }
    return;
  }
  const poll = async () => {
    const s = await api("/api/deps/status");
    if (msg) msg.textContent = s.status === "running" ? "Installing numpy…" : "";
    if (s.status === "running") { setTimeout(poll, 1500); return; }
    if (s.status === "done") loadEmbedInfo();
    else if (msg) msg.textContent = "Install failed — you can still search (slower). See log.";
  };
  poll();
}

/* ---------- appearance: theme + accent (persisted in localStorage) ---------- */
function mhApplyTheme(t) {
  t = t || "auto";
  if (t !== "auto") document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem("mh_theme", t); } catch (e) {}
  $$("#themeOpts .themeopt").forEach(o => o.classList.toggle("on", o.dataset.theme === t));
}
function mhApplyAccent(a) {
  a = a || "blue";
  if (a !== "blue") document.documentElement.setAttribute("data-accent", a);
  else document.documentElement.removeAttribute("data-accent");
  try { localStorage.setItem("mh_accent", a); } catch (e) {}
  $$("#accentSwatches .swatch").forEach(s => s.classList.toggle("on", s.dataset.accent === a));
}
$$("#themeOpts .themeopt").forEach(o => o.onclick = () => mhApplyTheme(o.dataset.theme));
$$("#accentSwatches .swatch").forEach(s => s.onclick = () => mhApplyAccent(s.dataset.accent));
(function () {
  let t = "auto", a = "blue";
  try { t = localStorage.getItem("mh_theme") || "auto"; a = localStorage.getItem("mh_accent") || "blue"; } catch (e) {}
  mhApplyTheme(t); mhApplyAccent(a);
})();

/* ---------- smart culling: near-duplicates + screenshot sort ---------- */
function openFile(p, reveal) { return api("/api/open", "POST", { path: p, reveal: !!reveal }); }
const escHtml = t => (t || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

async function loadCulling() {
  const thr = $("#cullThresh").value;
  $("#cullCsv").href = "/api/ai/near-duplicates.csv?threshold=" + thr;
  $("#cullInfo").textContent = "Scanning…";
  $("#cullClusters").innerHTML = "";
  const d = await api("/api/ai/near-duplicates?threshold=" + thr);
  if (d.error) { $("#cullInfo").textContent = d.error; return; }
  const mb = (d.reclaimable_bytes / 1e6).toFixed(1);
  $("#cullInfo").innerHTML = `<b>${d.cluster_count}</b> near-duplicate groups · <b>${fmt(d.duplicate_files)}</b> redundant shots · ~<b>${mb} MB</b> reclaimable · backend <b>${d.backend}</b>`
    + (d.note ? `<br><span class="muted">${d.note}</span>` : "");
  if (!d.clusters.length) { $("#cullClusters").innerHTML = '<div class="muted">No near-duplicates found at this threshold.</div>'; return; }
  d.clusters.forEach((c, ci) => {
    const rows = c.members.map(m => `
      <tr>
        <td><img class="thumb" src="/api/thumb?path=${encodeURIComponent(m.path)}" loading="lazy"
             style="width:84px;height:60px;object-fit:cover;border-radius:7px;background:var(--fill);cursor:pointer" data-path="${escHtml(m.path)}"></td>
        <td>${m.keep ? '<span class="chip trips">KEEP</span>' : '<span class="chip">review</span>'}</td>
        <td><b>${escHtml(m.file_name)}</b><div class="muted" style="font-size:11px"><code>${escHtml(m.path)}</code></div></td>
        <td class="num">${(m.size/1e6).toFixed(1)} MB</td>
        <td><div class="row" style="gap:6px;flex-wrap:nowrap">
          <button class="btn bordered cull-open" data-path="${escHtml(m.path)}">Open</button>
          <button class="btn cull-reveal" data-path="${escHtml(m.path)}">Reveal</button>
        </div></td>
      </tr>`).join("");
    $("#cullClusters").insertAdjacentHTML("beforeend",
      `<div class="listcard" style="margin-bottom:14px">
         <div class="row" style="justify-content:space-between;padding:10px 14px">
           <b>Group ${ci + 1} · ${c.trip}</b><span class="muted">${c.count} similar shots</span></div>
         <table><tbody>${rows}</tbody></table>
       </div>`);
  });
  $$("#cullClusters .cull-open").forEach(b => b.onclick = () => openFile(b.dataset.path));
  $$("#cullClusters .cull-reveal").forEach(b => b.onclick = () => openFile(b.dataset.path, true));
  $$("#cullClusters .thumb").forEach(t => t.onclick = () => openFile(t.dataset.path));
}
$("#cullScan").onclick = loadCulling;

async function loadScreenshotSort() {
  const tb = $("#ssortTbl tbody"); tb.innerHTML = "";
  const d = await api("/api/ai/screenshot-sort");
  if (d.error) { tb.innerHTML = `<tr><td colspan="5" class="muted">${d.error}</td></tr>`; return; }
  if (!(d.suggestions || []).length) {
    tb.innerHTML = `<tr><td colspan="5" class="muted">No misplaced screenshots — ${fmt(d.total || 0)} document(s) checked, all already organized.</td></tr>`;
    return;
  }
  d.suggestions.forEach(r => tb.insertAdjacentHTML("beforeend",
    `<tr>
      <td><img class="thumb" src="/api/thumb?path=${encodeURIComponent(r.path)}" loading="lazy"
           style="width:84px;height:60px;object-fit:cover;border-radius:7px;background:var(--fill);cursor:pointer" data-path="${escHtml(r.path)}"></td>
      <td><b>${escHtml(r.path.split("/").pop())}</b><div class="muted" style="font-size:11px"><code>${escHtml(r.path)}</code></div></td>
      <td><span class="chip">${escHtml(r.bucket || "")}</span></td>
      <td class="muted" style="font-size:11px">${escHtml((r.text || "").slice(0, 100))}</td>
      <td><div class="row" style="gap:6px;flex-wrap:nowrap">
        <button class="btn bordered ss-open" data-path="${escHtml(r.path)}">Open</button>
        <button class="btn ss-reveal" data-path="${escHtml(r.path)}">Reveal</button>
      </div></td>
    </tr>`));
  $$("#ssortTbl .ss-open").forEach(b => b.onclick = () => openFile(b.dataset.path));
  $$("#ssortTbl .ss-reveal").forEach(b => b.onclick = () => openFile(b.dataset.path, true));
  $$("#ssortTbl .thumb").forEach(t => t.onclick = () => openFile(t.dataset.path));
}

/* ---------- AI captions ---------- */
function renderCaptionInfo(s) {
  s = s || {};
  $("#capInfo").innerHTML = `<b>${fmt(s.captioned)}</b> captioned · backend <b>${s.backend}</b>`
    + (s.backend !== "mlxvlm" ? " · <span class=\"muted\">(heuristic — enable MLX-VLM for AI descriptions)</span>" : "");
  const b = $("#capBuild");
  if (b) { b.disabled = s.status === "running"; b.textContent = s.status === "running" ? `Captioning… ${fmt(s.done)}/${fmt(s.total)}` : "Build / update captions"; }
  if (s.log) { const lg = $("#capLog"); if (lg) { lg.style.display = "block"; lg.textContent = s.log; lg.scrollTop = lg.scrollHeight; } }
}
async function loadCaptionInfo() { renderCaptionInfo(await api("/api/ai/caption/status")); }
$("#capBuild").onclick = async () => {
  const folderVal = $("#embUnder").value.trim();   // reuse the embeddings folder scope if set
  const body = {};
  if ($("#emb_direct") && $("#emb_direct").checked && folderVal) body.folder = folderVal;
  else if (folderVal) body.under = folderVal;
  const r = await api("/api/ai/caption/start", "POST", body);
  if (r.error) { alert(r.error); return; }
  const poll = async () => {
    const s = await api("/api/ai/caption/status");
    renderCaptionInfo(s);
    if (s.status === "running") setTimeout(poll, 1200);
  };
  poll();
};
async function captionSearch() {
  const q = $("#capQ").value.trim(); if (!q) return;
  $("#capSearchInfo").textContent = "Searching captions…";
  const d = await api("/api/ai/caption/search?limit=80&q=" + encodeURIComponent(q));
  const card = $("#capResultsCard"), tb = $("#capTbl tbody"); tb.innerHTML = "";
  if (d.error) { $("#capSearchInfo").textContent = d.error; card.classList.add("hidden"); return; }
  $("#capSearchInfo").innerHTML = `${(d.results || []).length} match(es)`
    + (d.note ? ` · <span class="muted">${d.note}</span>` : "");
  if (!(d.results || []).length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  d.results.forEach(r => tb.insertAdjacentHTML("beforeend",
    `<tr>
      <td><img class="thumb" src="/api/thumb?path=${encodeURIComponent(r.path)}" loading="lazy"
           style="width:84px;height:60px;object-fit:cover;border-radius:7px;background:var(--fill);cursor:pointer" data-path="${escHtml(r.path)}"></td>
      <td>${escHtml(r.caption)}<div class="muted" style="font-size:11px"><code>${escHtml(r.path)}</code></div></td>
      <td><div class="row" style="gap:6px;flex-wrap:nowrap">
        <button class="btn bordered cap-open" data-path="${escHtml(r.path)}">Open</button>
        <button class="btn cap-reveal" data-path="${escHtml(r.path)}">Reveal</button>
      </div></td>
    </tr>`));
  $$("#capTbl .cap-open").forEach(b => b.onclick = () => openFile(b.dataset.path));
  $$("#capTbl .cap-reveal").forEach(b => b.onclick = () => openFile(b.dataset.path, true));
  $$("#capTbl .thumb").forEach(t => t.onclick = () => openFile(t.dataset.path));
}
$("#capSearchBtn").onclick = captionSearch;
$("#capQ").addEventListener("keydown", e => { if (e.key === "Enter") captionSearch(); });

/* ---------- people (faces) ---------- */
async function loadPeople() {
  const s = await api("/api/ai/faces/status");
  const av = (s.available_info || {});
  $("#pplInfo").innerHTML = av.available
    ? `<b>${fmt(s.faces)}</b> faces · <b>${fmt(s.people)}</b> people groups`
    : `Face tool unavailable: ${av.reason || ""}`;
  if (s.log) { const lg = $("#pplLog"); if (lg) { lg.style.display = "block"; lg.textContent = s.log; lg.scrollTop = lg.scrollHeight; } }
  $("#pplScan").disabled = s.status === "running" || !av.available;
  $("#pplScan").textContent = s.status === "running" ? `Detecting… ${fmt(s.done)}/${fmt(s.total)}` : "Detect & group faces";
  renderPeople(await api("/api/ai/faces/people"));
}
function renderPeople(d) {
  const wrap = $("#pplGroups"); wrap.innerHTML = "";
  if (d.error) { wrap.innerHTML = `<div class="muted">${d.error}</div>`; return; }
  if (!(d.people || []).length) { wrap.innerHTML = '<div class="muted">No people grouped yet — run face detection.</div>'; return; }
  d.people.forEach((p, i) => {
    const thumbs = p.members.map(m =>
      `<img class="thumb pgt" src="/api/thumb?path=${encodeURIComponent(m.path)}" loading="lazy" data-path="${escHtml(m.path)}"
            title="${escHtml(m.path)}" style="width:74px;height:74px;object-fit:cover;border-radius:50%;margin:4px;cursor:pointer;background:var(--fill)">`).join("");
    wrap.insertAdjacentHTML("beforeend",
      `<div class="card" style="margin-bottom:14px">
         <div class="row" style="justify-content:space-between">
           <b>Person ${i + 1}</b><span class="muted">${p.count} face(s)</span></div>
         <div class="row" style="flex-wrap:wrap;margin-top:8px">${thumbs}</div>
       </div>`);
  });
  $$("#pplGroups .pgt").forEach(t => t.onclick = () => openFile(t.dataset.path));
}
$("#pplScan").onclick = async () => {
  const folderVal = $("#pplUnder").value.trim();
  const body = {};
  if (folderVal) body.folder = folderVal;
  const r = await api("/api/ai/faces/start", "POST", body);
  if (r.error) { alert(r.error); return; }
  const poll = async () => {
    const s = await api("/api/ai/faces/status");
    $("#pplScan").disabled = s.status === "running";
    $("#pplScan").textContent = s.status === "running" ? `Detecting… ${fmt(s.done)}/${fmt(s.total)}` : "Detect & group faces";
    if (s.log) { const lg = $("#pplLog"); if (lg) { lg.style.display = "block"; lg.textContent = s.log; lg.scrollTop = lg.scrollHeight; } }
    if (s.status === "running") { setTimeout(poll, 1500); return; }
    loadPeople();
  };
  poll();
};
$("#pplBrowse").onclick = async () => { await pickInto("#pplUnder", "folder", "Choose a folder for face detection"); };

/* ---------- library maintenance: reconcile deleted files ---------- */
function renderRecon(s) {
  s = s || {};
  const el = $("#reconStatus");
  if (el) {
    const pill = (label) => `<span class="pill">${label}</span>`;
    el.innerHTML = pill(`${fmt(s.present)} present`)
      + pill(`${fmt(s.missing)} deleted (mounted)`)
      + pill(`${fmt(s.skipped_unmounted)} on unplugged drives`)
      + (s.last_run ? pill(`scanned ${s.last_run}`) : "");
  }
  const scan = $("#reconScan"), prune = $("#reconPrune");
  const running = s.status === "running";
  if (scan) { scan.disabled = running; scan.textContent = running ? "Scanning…" : "Scan for deleted files"; }
  if (prune) {
    if (s.missing > 0 && !running) {
      prune.classList.remove("hidden");
      prune.textContent = `Remove ${fmt(s.missing)} from index`;
      prune.disabled = false;
    } else {
      prune.classList.add("hidden");
    }
  }
  if (s.log) { const lg = $("#reconLog"); if (lg) { lg.style.display = "block"; lg.textContent = s.log; lg.scrollTop = lg.scrollHeight; } }
}
async function loadRecon() { renderRecon(await api("/api/reindex/status")); }
function pollRecon(then) {
  const tick = async () => {
    const s = await api("/api/reindex/status");
    renderRecon(s);
    if (s.status === "running") { setTimeout(tick, 1000); return; }
    if (then) then(s);
  };
  tick();
}
$("#reconScan").onclick = async () => {
  const r = await api("/api/reindex/scan", "POST", { prune: false });
  if (r.error) { alert(r.error); return; }
  pollRecon();
};
$("#reconPrune").onclick = async () => {
  const s = await api("/api/reindex/status");
  if (!s.missing) return;
  if (!confirm(`Remove ${s.missing} deleted file(s) from the index? This only updates the database — it never touches any media files, and only affects files already gone from mounted drives.`)) return;
  const r = await api("/api/reindex/scan", "POST", { prune: true });
  if (r.error) { alert(r.error); return; }
  pollRecon(() => { loadSummary(); loadTrash(); });   // refresh Overview + Trash after prune
};

/* ---------- trash / auto-reconcile / immich ---------- */
if ($("#autoRecon")) $("#autoRecon").onchange = async () => {
  await api("/api/settings", "POST", { auto_reconcile: $("#autoRecon").checked });
};
async function loadTrash() {
  const d = await api("/api/reindex/trash");
  setTrashBadge(d.count || 0);
  const tb = $("#trashTbl tbody"); if (!tb) return; tb.innerHTML = "";
  $("#trashInfo").innerHTML = `${fmt(d.count || 0)} file(s) in Trash · recovery window <b>${d.retention_days || 30}</b> days`;
  if (!(d.items || []).length) { tb.innerHTML = '<tr><td colspan="3" class="muted">Trash is empty.</td></tr>'; return; }
  d.items.forEach(it => tb.insertAdjacentHTML("beforeend",
    `<tr>
      <td><b>${escHtml(it.path.split("/").pop())}</b><div class="muted" style="font-size:11px"><code>${escHtml(it.path)}</code></div></td>
      <td class="muted" style="font-size:11px">${escHtml(it.deleted_at || "")}</td>
      <td><button class="btn bordered tr-restore" data-id="${it.id}">Restore</button></td>
    </tr>`));
  $$("#trashTbl .tr-restore").forEach(b => b.onclick = async () => {
    await api("/api/reindex/restore", "POST", { ids: [parseInt(b.dataset.id, 10)] });
    loadTrash(); loadSummary();
  });
}
if ($("#trashRefresh")) $("#trashRefresh").onclick = loadTrash;
if ($("#trashEmpty")) $("#trashEmpty").onclick = async () => {
  const d = await api("/api/reindex/trash");
  if (!d.count) return;
  if (!confirm(`Permanently remove ${d.count} file(s) from the index? This only deletes database rows — your media files are never touched. This cannot be undone.`)) return;
  const ids = (d.items || []).map(i => i.id);
  await api("/api/reindex/purge", "POST", { ids });
  loadTrash(); loadSummary();
};
if ($("#immichExport")) $("#immichExport").onclick = async () => {
  const r = await api("/api/immich/export", "POST", {});
  if (r.error) { $("#immichInfo").textContent = r.error; return; }
  $("#immichInfo").innerHTML = `Wrote <code>IMMICH_EXTERNAL_LIBRARY.md</code> to your destination.`;
  openFile(r.readme, true);   // reveal the notes file in Finder
};

/* ---------- sidebar trash badge ---------- */
function setTrashBadge(n) {
  const b = $("#trashBadge");
  if (!b) return;
  if (n > 0) { b.textContent = n > 999 ? "999+" : n; b.classList.remove("hidden"); }
  else { b.classList.add("hidden"); }
}
async function refreshTrashBadge() {
  try { const d = await api("/api/reindex/trash"); setTrashBadge(d.count || 0); } catch (e) {}
}
refreshTrashBadge();                       // on load
setInterval(refreshTrashBadge, 30000);     // keep it current (reconcile runs in background)

/* ---------- quickstart jump links ---------- */
function gotoView(v) {
  const n = document.querySelector('.navitem[data-view="' + v + '"]');
  if (n) { n.click(); window.scrollTo(0, 0); }
}
document.querySelectorAll("[data-goto]").forEach(el => {
  el.onclick = (e) => { e.preventDefault(); gotoView(el.dataset.goto); };
});

/* ---------- staging destination picker ---------- */
async function pickStagingDest() {
  const r = await api("/api/pick", "POST", { kind: "folder", prompt: "Choose a staging destination folder" });
  if (r.error) { alert("Picker error: " + r.error); return; }
  if (!r.path) return;
  // mounted volumes live under /Volumes; everything else is treated as local
  const mode = r.path.startsWith("/Volumes/") ? "mounted" : "local";
  const st = await api("/api/settings", "POST", { dest_path: r.path, dest_mode: mode });
  renderDestLine(st);
  if ($("#destPath")) $("#destPath").value = st.dest_path;
  if ($("#destSeg")) $("#destSeg").querySelectorAll("button").forEach(x => x.classList.toggle("on", x.dataset.mode === st.dest_mode));
  if ($("#freeNote")) $("#freeNote").innerHTML = `Free space at destination: <b>${st.dest_free_gb ?? "?"} GB</b>`;
}
if ($("#destChange")) $("#destChange").onclick = pickStagingDest;
if ($("#destBrowse")) $("#destBrowse").onclick = async () => {
  await pickInto("#destPath", "folder", "Choose a staging destination folder");
  const p = $("#destPath").value.trim();
  if (p && $("#destSeg")) {
    destMode = p.startsWith("/Volumes/") ? "mounted" : "local";
    $("#destSeg").querySelectorAll("button").forEach(x => x.classList.toggle("on", x.dataset.mode === destMode));
  }
};
