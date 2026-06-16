#!/usr/bin/env python3
"""Render README screenshots from the REAL UI with fake demo data (no server,
no personal data). Injects a stubbed fetch into a copy of index.html, then uses
headless Chrome to snapshot each view. Output -> docs/screenshots/*.png."""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "mediahub" / "ui"
OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# ---- demo data (entirely fictional) ----
DEMO_JS = r"""
<script>
(function(){
  const D = {
    "/api/summary": {total_files:24680,total_gb:1820,unique_gb:1240,reclaim_gb:580,
      devices:[{device_name:"Sony",files:12000,gb:900},{device_name:"iPhone",files:8000,gb:500},
               {device_name:"Drone",files:3200,gb:300},{device_name:"GoPro",files:1480,gb:120}]},
    "/api/mounts": [{name:"Field SSD",free_gb:842},{name:"Archive T9",free_gb:1180}],
    "/api/trips": [{trip:"Iceland",category:"trip",dest:"Iceland/2023",unique_files:3400,unique_gb:120,dupe_gb:14,drives:"Field SSD"},
                   {trip:"Costa Rica",category:"trip",dest:"Costa Rica/2024",unique_files:2900,unique_gb:98,dupe_gb:9,drives:"Archive T9"},
                   {trip:"Banff",category:"trip",dest:"Banff/2025",unique_files:1750,unique_gb:64,dupe_gb:5,drives:"Field SSD"}],
    "/api/sources": [{device:"Sony",files:12000,gb:900},{device:"iPhone",files:8000,gb:500},{device:"Drone",files:3200,gb:300}],
    "/api/settings": {dest_mode:"mounted",dest_path:"/Volumes/Archive/Library",dest_free_gb:1180,verify_hash:true,auto_reconcile:false,trash_retention_days:30},
    "/api/drives/identity": {drives:[
        {expected_name:"Field SSD",status:"matched",renamed:false,mount:"/Volumes/Field SSD",files:12000,gb:900},
        {expected_name:"Archive T9",status:"matched",renamed:true,mount:"/Volumes/Archive T9 (2)",files:8600,gb:1180},
        {expected_name:"Old Trip Drive",status:"not connected",files:4080,gb:420}], mounted:[{name:"Field SSD",mount:"/Volumes/Field SSD"},{name:"Archive T9 (2)",mount:"/Volumes/Archive T9 (2)"}]},
    "/api/embed/status": {embedded:24680,candidates:24680,backend:"mlx",numpy:true,status:"idle",done:0,total:0},
    "/api/deps/status": {venv_ready:true,numpy:true,mlx_clip:true,mlx_vlm:true,status:"idle"},
    "/api/ai/caption/status": {captioned:24680,backend:"mlxvlm",status:"idle",done:0,total:0},
    "/api/ai/screenshot-sort": {suggestions:[],total:0,to_move:0},
    "/api/ai/faces/status": {faces:0,people:0,status:"idle",available_info:{available:true}},
    "/api/ai/faces/people": {people:[]},
    "/api/reindex/status": {present:24680,missing:0,skipped_unmounted:0,status:"idle",last_run:"2026-06-16 15:00"},
    "/api/reindex/trash": {items:[],count:0,retention_days:30},
    "/api/paths": {data_dir:"~/Library/Application Support/MediaHub",data_mb:42,logs_dir:"…/logs",manifests_dir:"…/manifests",db_path:"…/media_indexer.sqlite3"},
    "/api/logs/tail": {log:"MediaHub running.\nSTAGE done: Banff (1750 files).\n"},
    "/api/manifests": {manifests:[{job:"Banff_20250416",trip:"Banff",verified_files:1750,error_files:0,verified_gb:64}]},
    "/api/dedupe-plan/summary": {groups:420,reclaim_gb:580,total_dupes:5200},
    "/api/vision/status": {buckets:[],available_info:{available:true},status:"idle",candidates:0},
    "/api/ingest/status": {status:"idle",log:""}
  };
  function pick(u){ u=u.split("?")[0]; if(D[u]!==undefined) return D[u]; 
    for(const k in D){ if(u.indexOf(k)===0) return D[k]; } return {}; }
  window.fetch = function(url){ const u=(typeof url==="string")?url:(url&&url.url)||"";
    return Promise.resolve({ok:true,status:200,headers:{get:()=>"MediaHub"},
      json:()=>Promise.resolve(pick(u)), text:()=>Promise.resolve("")}); };
  window.addEventListener("load", function(){
    var v=new URLSearchParams(location.search).get("v");
    if(v){ var n=document.querySelector('.navitem[data-view="'+v+'"]'); if(n) n.click(); }
  });
})();
</script>
"""

def build_demo(theme=None):
    html = (UI / "index.html").read_text()
    inject = DEMO_JS
    if theme:
        inject += (f'\n<script>try{{localStorage.setItem("mh_theme","{theme}");'
                   f'document.documentElement.setAttribute("data-theme","{theme}")}}catch(e){{}}</script>')
    html = html.replace("</head>", inject + "\n</head>", 1)
    demo = UI / "_demo.html"
    demo.write_text(html)
    return demo

def shot(demo, view, name):
    url = f"file://{demo}" + (f"?v={view}" if view else "")
    out = OUT / f"{name}.png"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars", "--force-device-scale-factor=2",
                    "--window-size=1340,880", "--virtual-time-budget=2500",
                    f"--screenshot={out}", url], capture_output=True, text=True)
    return out

def main():
    # light-theme tabs
    demo = build_demo()
    try:
        for view, name in [(None, "overview"), ("stage", "stage"),
                           ("search", "search")]:
            p = shot(demo, view, name)
            print(f"{name}: {'OK' if p.exists() and p.stat().st_size>5000 else 'FAILED'} "
                  f"({p.stat().st_size if p.exists() else 0} bytes)")
        # Aurora (dark refractive glass) hero
        build_demo(theme="aurora")
        p = shot(demo, None, "hero-aurora")
        print(f"hero-aurora: {'OK' if p.exists() and p.stat().st_size>5000 else 'FAILED'} "
              f"({p.stat().st_size if p.exists() else 0} bytes)")
    finally:
        demo.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
