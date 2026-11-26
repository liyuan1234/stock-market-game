/**
 * Runtime API configuration for the static frontend.
 *
 * SAME-ORIGIN MODE (default, API_BASE empty):
 *   The page talks to whatever host served it. This is how the app behaves
 *   when you run `uvicorn dashboard:app` and open it directly.
 *
 * SPLIT MODE (Vercel static frontend + remote Python backend):
 *   Set API_BASE to the public backend origin (no trailing slash). All
 *   /api/* requests and the /ws WebSocket are rewritten to that host with
 *   credentials included.
 *
 * build_vercel.py substitutes __API_BASE__ from the VERCEL_API_BASE
 * environment variable at build time.
 */
window.API_BASE = "__API_BASE__";
