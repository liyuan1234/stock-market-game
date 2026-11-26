#!/usr/bin/env python3
"""
Vercel build step for the static frontend.

Vercel cannot host the Python backend: the simulation needs a long-lived
process with an in-memory order book, a 20 Hz ticker thread, and
WebSockets. Only the HTML/CSS/JS shell is deployed here. It talks to the
real backend over CORS + WSS.

This script:
  1. Injects VERCEL_API_BASE into config.js (the backend origin).
  2. Copies the static assets into public/ (Vercel's output directory).
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PUBLIC = ROOT / "public"

api_base = os.environ.get("VERCEL_API_BASE", "").strip().rstrip("/")

if not api_base:
    print("=" * 68)
    print("WARNING: VERCEL_API_BASE is not set.")
    print()
    print("Without it the frontend uses same-origin paths and will NOT")
    print("reach the Python backend -- the page will render but stay frozen.")
    print()
    print("Set it in Vercel:")
    print("  Project -> Settings -> Environment Variables")
    print("  Name : VERCEL_API_BASE")
    print("  Value: https://<your-backend-host>")
    print("=" * 68)
else:
    print(f"Backend API base: {api_base}")

# --- 1. config.js with the real backend origin ---------------------------
cfg_template = (ROOT / "config.js").read_text(encoding="utf-8")
if "__API_BASE__" not in cfg_template:
    sys.exit("ERROR: config.js is missing the __API_BASE__ placeholder.")
cfg = cfg_template.replace("__API_BASE__", api_base)

# --- 2. assemble public/ -------------------------------------------------
if PUBLIC.exists():
    shutil.rmtree(PUBLIC)
PUBLIC.mkdir(parents=True)

for name in ("index.html", "login.html"):
    shutil.copy2(ROOT / name, PUBLIC / name)
(PUBLIC / "config.js").write_text(cfg, encoding="utf-8")

# assets/ may exist for the README screenshot; copy if present.
if (ROOT / "assets").is_dir():
    shutil.copytree(ROOT / "assets", PUBLIC / "assets")

print("Built public/:")
for p in sorted(PUBLIC.rglob("*")):
    if p.is_file():
        print(f"  {p.relative_to(PUBLIC)}  ({p.stat().st_size:,} bytes)")
