"""
World Labs Flask App v2
-----------------------
Generate a Marble world from:
  - a text prompt
  - a single 360° panorama (uploaded local file)         <- best fidelity for real places
  - up to 4 normal images with azimuth (Front/Right/Back/Left)
  - up to 8 normal images with Auto Layout

Then view the resulting world as a 360° panorama in-browser via A-Frame.

Run:
    pip install flask requests
    python app.py
Then open: http://127.0.0.1:5000

Notes on accuracy (from World Labs docs):
  - Single pano = max spatial control; recommended for reconstructing a real place.
  - Multi-pano in ONE call is NOT supported by the API. Generate one world per pano
    and stitch them with Marble Studio Compose if you need a larger scene.
  - Multi-image Auto Layout works best with images from the SAME space, similar
    aspect ratio + resolution, and some overlap.
"""

import os
import sqlite3
import threading
import time
import uuid
import mimetypes
import requests
from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, abort, flash
)
from werkzeug.utils import secure_filename

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
API_KEY = os.environ.get("WORLDLABS_API_KEY", "UpyHNSlDrzdryS5RpJXITqOouKqpFsjr")
API_BASE = "https://api.worldlabs.ai/marble/v1"
DB_PATH = "worlds.db"
UPLOAD_DIR = "uploads"
POLL_INTERVAL = 15
POLL_TIMEOUT = 30 * 60                         # 30 min — panos can take longer
ALLOWED_EXTS = {"jpg", "jpeg", "png", "webp"}
MAX_FILE_SIZE = 50 * 1024 * 1024               # 50 MB per file

os.makedirs(UPLOAD_DIR, exist_ok=True)

HEADERS_JSON = {
    "WLT-Api-Key": API_KEY,
    "Content-Type": "application/json",
}
HEADERS_KEY = {"WLT-Api-Key": API_KEY}

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024   # total request size cap

# -----------------------------------------------------------------------------
# DB
# -----------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS worlds (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id    TEXT UNIQUE,
                world_id        TEXT,
                display_name    TEXT,
                prompt          TEXT,
                input_kind      TEXT,                -- text | pano | multi-image
                status          TEXT DEFAULT 'PENDING',
                pano_url        TEXT,
                splat_url       TEXT,                -- 3D Gaussian Splat SPZ file (the actual 3D world)
                thumbnail_url   TEXT,
                marble_url      TEXT,
                error           TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add splat_url to older DBs that don't have it yet
        cols = {r[1] for r in conn.execute("PRAGMA table_info(worlds)").fetchall()}
        if "splat_url" not in cols:
            conn.execute("ALTER TABLE worlds ADD COLUMN splat_url TEXT")

init_db()

# -----------------------------------------------------------------------------
# World Labs API helpers
# -----------------------------------------------------------------------------
def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

def allowed(filename: str) -> bool:
    return _ext(filename) in ALLOWED_EXTS

def prepare_upload(file_name: str, extension: str, kind: str = "image") -> dict:
    """Returns {media_asset:{id,...}, upload_info:{upload_url, upload_method, required_headers}}"""
    payload = {"file_name": file_name, "kind": kind, "extension": extension}
    r = requests.post(f"{API_BASE}/media-assets:prepare_upload",
                      headers=HEADERS_JSON, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def upload_file_to_signed_url(local_path: str, upload_info: dict):
    """PUT the file bytes to the signed upload URL using the headers the API gave us."""
    method = upload_info.get("upload_method", "PUT").upper()
    url = upload_info["upload_url"]
    headers = dict(upload_info.get("required_headers") or {})
    # Best-effort content-type so Google Storage stores it correctly
    ctype, _ = mimetypes.guess_type(local_path)
    if ctype and "Content-Type" not in headers:
        headers["Content-Type"] = ctype
    with open(local_path, "rb") as f:
        r = requests.request(method, url, data=f, headers=headers, timeout=300)
    r.raise_for_status()

def upload_local_image(local_path: str) -> str:
    """Convenience: prepare_upload + PUT. Returns media_asset_id."""
    file_name = os.path.basename(local_path)
    info = prepare_upload(file_name, _ext(file_name), "image")
    upload_file_to_signed_url(local_path, info["upload_info"])
    # Per OpenAPI spec, the MediaAsset object's id field is "media_asset_id".
    # The quickstart docs show "id" — that's outdated. Handle both just in case.
    ma = info["media_asset"]
    return ma.get("media_asset_id") or ma["id"]

def generate_world(payload: dict) -> dict:
    r = requests.post(f"{API_BASE}/worlds:generate",
                      headers=HEADERS_JSON, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()

def get_operation(operation_id: str) -> dict:
    r = requests.get(f"{API_BASE}/operations/{operation_id}",
                     headers=HEADERS_KEY, timeout=30)
    r.raise_for_status()
    return r.json()

# -----------------------------------------------------------------------------
# Background poller
# -----------------------------------------------------------------------------
def poll_operation_async(operation_id: str):
    start = time.time()
    while time.time() - start < POLL_TIMEOUT:
        try:
            op = get_operation(operation_id)
        except requests.RequestException as e:
            print(f"[poll] network error: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        meta = op.get("metadata") or {}
        progress = meta.get("progress") or {}
        status = progress.get("status", "IN_PROGRESS")
        world_id = meta.get("world_id")

        if op.get("done"):
            if op.get("error"):
                with db() as conn:
                    conn.execute(
                        "UPDATE worlds SET status=?, error=? WHERE operation_id=?",
                        ("FAILED", str(op["error"]), operation_id),
                    )
                return
            response = op.get("response") or {}
            assets = response.get("assets") or {}
            imagery = assets.get("imagery") or {}
            splats = assets.get("splats") or {}
            spz_urls = splats.get("spz_urls") or {}
            # Prefer 500k tier — good quality, manageable size. Fall back to others.
            splat_url = (spz_urls.get("500k")
                         or spz_urls.get("100k")
                         or spz_urls.get("full_res"))
            with db() as conn:
                conn.execute("""
                    UPDATE worlds
                       SET status=?, world_id=?, pano_url=?, splat_url=?,
                           thumbnail_url=?, marble_url=?
                     WHERE operation_id=?
                """, ("SUCCEEDED", world_id,
                      imagery.get("pano_url"),
                      splat_url,
                      assets.get("thumbnail_url"),
                      response.get("world_marble_url"),
                      operation_id))
            print(f"[poll] {operation_id} done -> world {world_id} (splat: {bool(splat_url)})")
            return

        with db() as conn:
            conn.execute(
                "UPDATE worlds SET status=?, world_id=? WHERE operation_id=?",
                (status, world_id, operation_id),
            )
        time.sleep(POLL_INTERVAL)

    with db() as conn:
        conn.execute(
            "UPDATE worlds SET status=?, error=? WHERE operation_id=?",
            ("TIMEOUT", "Polling exceeded timeout", operation_id),
        )

# -----------------------------------------------------------------------------
# Save uploaded files locally + push to World Labs media-assets
# -----------------------------------------------------------------------------
def save_local(file_storage) -> str:
    """Save a FileStorage to UPLOAD_DIR with a unique name. Returns local path."""
    fname = secure_filename(file_storage.filename or "upload")
    if not allowed(fname):
        raise ValueError(f"Unsupported file type: {fname}")
    unique = f"{uuid.uuid4().hex}_{fname}"
    path = os.path.join(UPLOAD_DIR, unique)
    file_storage.save(path)
    return path

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM worlds ORDER BY created_at DESC"
        ).fetchall()
    return render_template("index.html", worlds=rows)

@app.route("/generate", methods=["POST"])
def generate():
    """
    Handles three input modes via form field `mode`:
      - text
      - pano        (1 file: 360 equirectangular)
      - multi       (2-8 files, with per-file azimuth OR auto-layout)
    """
    mode = request.form.get("mode", "text")
    display_name = (request.form.get("display_name") or "Untitled World").strip()
    text_prompt = (request.form.get("prompt") or "").strip()
    model = request.form.get("model", "marble-1.1")

    try:
        if mode == "text":
            if not text_prompt:
                flash("Prompt is required for text mode.")
                return redirect(url_for("index"))
            payload = {
                "display_name": display_name,
                "model": model,
                "world_prompt": {"type": "text", "text_prompt": text_prompt},
            }

        elif mode == "pano":
            f = request.files.get("pano_file")
            if not f or not f.filename:
                flash("Please attach a 360° panorama image.")
                return redirect(url_for("index"))
            local = save_local(f)
            media_id = upload_local_image(local)
            payload = {
                "display_name": display_name,
                "model": model,
                "world_prompt": {
                    "type": "image",
                    "image_prompt": {
                        "source": "media_asset",
                        "media_asset_id": media_id,
                    },
                    "is_pano": True,                          # ImagePrompt-level field, not inside image_prompt
                    **({"text_prompt": text_prompt} if text_prompt else {}),
                },
            }

        elif mode == "multi":
            files = request.files.getlist("multi_files")
            files = [f for f in files if f and f.filename]
            if len(files) < 2:
                flash("Multi-image mode needs at least 2 images.")
                return redirect(url_for("index"))
            if len(files) > 8:
                flash("Max 8 images for multi-image.")
                return redirect(url_for("index"))

            azimuths = request.form.getlist("azimuths")   # may be empty for auto-layout
            use_auto_layout = (request.form.get("auto_layout") == "on") or not azimuths

            multi_image_prompt = []
            for i, f in enumerate(files):
                local = save_local(f)
                media_id = upload_local_image(local)
                item = {
                    "content": {"source": "media_asset", "media_asset_id": media_id}
                }
                if not use_auto_layout:
                    try:
                        item["azimuth"] = float(azimuths[i])
                    except (IndexError, ValueError):
                        item["azimuth"] = 0.0
                multi_image_prompt.append(item)

            payload = {
                "display_name": display_name,
                "model": model,
                "world_prompt": {
                    "type": "multi-image",
                    "multi_image_prompt": multi_image_prompt,
                    # reconstruct_images=true enables auto-layout mode and up to 8 images;
                    # required when using more than 4 images or when no azimuths are set.
                    "reconstruct_images": bool(use_auto_layout),
                    **({"text_prompt": text_prompt} if text_prompt else {}),
                },
            }

        else:
            flash("Unknown mode.")
            return redirect(url_for("index"))

        op = generate_world(payload)

    except requests.HTTPError as e:
        return f"<h2>API error</h2><pre>{e.response.status_code}: {e.response.text}</pre>", 500
    except requests.RequestException as e:
        return f"<h2>Network error</h2><pre>{e}</pre>", 500
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("index"))

    operation_id = op["operation_id"]
    with db() as conn:
        conn.execute("""
            INSERT INTO worlds (operation_id, display_name, prompt, input_kind, status)
            VALUES (?, ?, ?, ?, ?)
        """, (operation_id, display_name, text_prompt or f"[{mode} input]",
              mode, "IN_PROGRESS"))

    threading.Thread(target=poll_operation_async,
                     args=(operation_id,), daemon=True).start()
    return redirect(url_for("index"))

@app.route("/status/<operation_id>")
def status(operation_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM worlds WHERE operation_id=?", (operation_id,)
        ).fetchone()
    if not row:
        abort(404)
    return jsonify(dict(row))

@app.route("/view/<int:world_db_id>")
def view(world_db_id):
    mode = request.args.get("mode", "auto")     # "auto" | "pano" | "3d"
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM worlds WHERE id=?", (world_db_id,)
        ).fetchone()
    if not row:
        abort(404)
    if not row["pano_url"] and not row["splat_url"]:
        return "<h2>Assets not ready yet. Refresh in a bit.</h2>", 202

    # Auto-pick best available mode
    if mode == "auto":
        mode = "3d" if row["splat_url"] else "pano"

    return render_template("viewer.html", world=row, mode=mode)

@app.route("/pano/<int:world_db_id>")
def pano_proxy(world_db_id):
    """Stream the pano image through Flask to avoid CORS issues in the viewer."""
    from flask import Response, stream_with_context
    with db() as conn:
        row = conn.execute(
            "SELECT pano_url FROM worlds WHERE id=?", (world_db_id,)
        ).fetchone()
    if not row or not row["pano_url"]:
        abort(404)
    upstream = requests.get(row["pano_url"], stream=True, timeout=60)
    if not upstream.ok:
        return f"Upstream returned {upstream.status_code}", 502
    return Response(
        stream_with_context(upstream.iter_content(chunk_size=64 * 1024)),
        content_type=upstream.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )

@app.route("/splat/<int:world_db_id>")
def splat_proxy(world_db_id):
    """Stream the Gaussian Splat SPZ file through Flask. Same-origin = no CORS hassles."""
    from flask import Response, stream_with_context
    with db() as conn:
        row = conn.execute(
            "SELECT splat_url FROM worlds WHERE id=?", (world_db_id,)
        ).fetchone()
    if not row or not row["splat_url"]:
        abort(404)
    upstream = requests.get(row["splat_url"], stream=True, timeout=120)
    if not upstream.ok:
        return f"Upstream returned {upstream.status_code}", 502
    return Response(
        stream_with_context(upstream.iter_content(chunk_size=256 * 1024)),
        content_type="application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=3600",
            # Forward content-length so the viewer can show progress
            **({"Content-Length": upstream.headers["Content-Length"]}
               if "Content-Length" in upstream.headers else {}),
        },
    )

def extract_splat_url(response_or_world: dict) -> tuple[str | None, dict]:
    """
    Find the best splat URL in a World Labs response, trying multiple known shapes.
    Returns (url, debug_info) — debug_info lists where it looked.

    Marble worlds can store splats in different places depending on the model version:
      - assets.splats.spz_urls.{100k,500k,full_res}        (current docs)
      - assets.splats[].spz_urls.{...}                      (array of capture points)
      - assets.gaussian_splats / assets.splat / assets.spz  (older variants)
      - top-level splat_url
    """
    info = {"checked_paths": [], "found_keys": []}
    candidates = []

    def note(path, value):
        info["checked_paths"].append(path)
        if value:
            info["found_keys"].append(path)

    assets = response_or_world.get("assets") or {}

    # 1) The documented shape: assets.splats.spz_urls.{tier}
    splats = assets.get("splats")
    note("assets.splats", splats)
    if isinstance(splats, dict):
        spz = splats.get("spz_urls") or {}
        note("assets.splats.spz_urls", spz)
        if isinstance(spz, dict):
            for tier in ("500k", "100k", "full_res", "full", "high", "medium", "low"):
                if spz.get(tier):
                    candidates.append(("assets.splats.spz_urls." + tier, spz[tier]))
        # Fallbacks if structured differently
        for k in ("url", "spz_url", "splat_url"):
            if splats.get(k):
                candidates.append((f"assets.splats.{k}", splats[k]))

    # 2) Array shape: assets.splats is a list of capture points
    if isinstance(splats, list) and splats:
        for i, item in enumerate(splats):
            if not isinstance(item, dict): continue
            spz = item.get("spz_urls") or {}
            if isinstance(spz, dict):
                for tier in ("500k", "100k", "full_res"):
                    if spz.get(tier):
                        candidates.append((f"assets.splats[{i}].spz_urls.{tier}", spz[tier]))
            for k in ("url", "spz_url", "splat_url"):
                if item.get(k):
                    candidates.append((f"assets.splats[{i}].{k}", item[k]))

    # 3) Older / alternate keys at the assets level
    for k in ("gaussian_splats", "splat", "spz", "gaussian_splat_url", "splat_url"):
        v = assets.get(k)
        note(f"assets.{k}", v)
        if isinstance(v, str):
            candidates.append((f"assets.{k}", v))
        elif isinstance(v, dict):
            for inner in ("url", "spz_url", "500k", "100k", "full_res"):
                if v.get(inner):
                    candidates.append((f"assets.{k}.{inner}", v[inner]))

    # 4) Top-level on the world/response object
    for k in ("splat_url", "world_splat_url", "spz_url"):
        v = response_or_world.get(k)
        note(k, v)
        if v:
            candidates.append((k, v))

    info["candidates"] = candidates
    if candidates:
        return candidates[0][1], info
    return None, info


@app.route("/refetch/<int:world_db_id>")
def refetch(world_db_id):
    """Re-fetch a completed world from the API to backfill missing asset URLs."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM worlds WHERE id=?", (world_db_id,)
        ).fetchone()
    if not row or not row["world_id"]:
        return "No world_id stored for this row — cannot refetch.", 400

    try:
        r = requests.get(f"{API_BASE}/worlds/{row['world_id']}",
                         headers=HEADERS_KEY, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        return f"Refetch failed: {e}", 502

    world = r.json().get("world") or r.json()       # API may or may not wrap in "world"
    assets = world.get("assets") or {}
    imagery = assets.get("imagery") or {}

    splat_url, _ = extract_splat_url(world)

    with db() as conn:
        conn.execute("""
            UPDATE worlds
               SET pano_url=COALESCE(?, pano_url),
                   splat_url=COALESCE(?, splat_url),
                   thumbnail_url=COALESCE(?, thumbnail_url),
                   marble_url=COALESCE(?, marble_url)
             WHERE id=?
        """, (imagery.get("pano_url"),
              splat_url,
              assets.get("thumbnail_url"),
              world.get("world_marble_url"),
              world_db_id))
    return redirect(url_for("debug", world_db_id=world_db_id))


@app.route("/raw/<int:world_db_id>")
def raw_world(world_db_id):
    """Dump the raw JSON response from GET /worlds/{world_id}.
    Use this when the splat URL extraction is failing — you'll see exactly
    where the splat lives in the response so we can fix the parser."""
    import json
    with db() as conn:
        row = conn.execute(
            "SELECT world_id, operation_id FROM worlds WHERE id=?", (world_db_id,)
        ).fetchone()
    if not row or not row["world_id"]:
        return "No world_id stored for this row.", 400

    try:
        r_world = requests.get(f"{API_BASE}/worlds/{row['world_id']}",
                               headers=HEADERS_KEY, timeout=30)
        world_json = r_world.json() if r_world.ok else {"_error": r_world.status_code, "_body": r_world.text}
    except Exception as e:
        world_json = {"_exception": str(e)}

    try:
        r_op = requests.get(f"{API_BASE}/operations/{row['operation_id']}",
                            headers=HEADERS_KEY, timeout=30)
        op_json = r_op.json() if r_op.ok else {"_error": r_op.status_code, "_body": r_op.text}
    except Exception as e:
        op_json = {"_exception": str(e)}

    splat, info = extract_splat_url(world_json.get("world") or world_json)

    return f"""
<html><body style="font-family: monospace; padding: 20px; background:#0a0a0e; color:#ddd; font-size:12px;">
<h2>Raw API responses — world #{world_db_id}</h2>

<h3 style="color:#6dd58c;">Splat extraction result</h3>
<p>Found URL: <b style="color:{'#6dd58c' if splat else '#ff8a8a'}">{splat or 'NONE'}</b></p>
<p>Paths checked: <code style="color:#888">{', '.join(info['checked_paths']) or '(none)'}</code></p>
<p>Paths with data: <code style="color:#9a8cff">{', '.join(info['found_keys']) or '(none)'}</code></p>
<p>All candidate URLs found:</p>
<pre style="background:#1a1a22;padding:10px;border-radius:6px;overflow:auto;">{json.dumps(info['candidates'], indent=2)}</pre>

<h3 style="color:#9a8cff;">GET /worlds/{row['world_id']}</h3>
<pre style="background:#1a1a22;padding:10px;border-radius:6px;overflow:auto;max-height:60vh;">{json.dumps(world_json, indent=2)}</pre>

<h3 style="color:#9a8cff;">GET /operations/{row['operation_id']}</h3>
<pre style="background:#1a1a22;padding:10px;border-radius:6px;overflow:auto;max-height:60vh;">{json.dumps(op_json, indent=2)}</pre>

<p><a style="color:#9a8cff" href="/debug/{world_db_id}">← back to debug</a></p>
</body></html>
"""

@app.route("/debug/<int:world_db_id>")
def debug(world_db_id):
    """Show the raw DB row + a direct link to the pano image, for sanity-checking."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM worlds WHERE id=?", (world_db_id,)
        ).fetchone()
    if not row:
        abort(404)
    d = dict(row)
    pano = d.get("pano_url") or ""
    splat = d.get("splat_url") or ""
    return f"""
    <html><body style="font-family: monospace; padding: 20px; background:#111; color:#eee; line-height:1.6;">
      <h2>Debug — world #{world_db_id}</h2>
      <p><b>display_name:</b> {d.get('display_name')}</p>
      <p><b>status:</b> {d.get('status')}</p>
      <p><b>world_id:</b> {d.get('world_id')}</p>
      <p><b>operation_id:</b> {d.get('operation_id')}</p>
      <p><b>marble_url:</b> <a style="color:#9a8cff" href="{d.get('marble_url')}" target="_blank">{d.get('marble_url')}</a></p>
      <p><b>pano_url:</b><br><a style="color:#9a8cff;word-break:break-all" href="{pano}" target="_blank">{pano or '(none)'}</a></p>
      <p><b>splat_url (3D Gaussian Splat):</b><br><a style="color:#9a8cff;word-break:break-all" href="{splat}" target="_blank">{splat or '(none — try Refetch below)'}</a></p>
      <p><b>thumbnail_url:</b><br><a style="color:#9a8cff;word-break:break-all" href="{d.get('thumbnail_url')}" target="_blank">{d.get('thumbnail_url')}</a></p>

      <hr>
      <p style="background:#1a1a22;padding:10px;border-radius:6px;">
        <b>Missing splat_url?</b> Try these in order:
        <ol style="margin:8px 0 0 20px;">
          <li><a style="color:#6dd58c;font-weight:bold;" href="/refetch/{world_db_id}">→ Refetch from the API</a> — pulls latest asset URLs (free, no credits).</li>
          <li>If refetch doesn't populate it, <a style="color:#ffd966;font-weight:bold;" href="/raw/{world_db_id}">→ view the raw API response</a> to see where the splat actually lives.</li>
        </ol>
      </p>

      <hr>
      <p>Quick links:</p>
      <p>
        <a style="color:#9a8cff" href="/view/{world_db_id}?mode=3d">View as 3D world (splat) →</a> ·
        <a style="color:#9a8cff" href="/view/{world_db_id}?mode=pano">View as pano →</a>
      </p>

      <hr>
      <p>Pano image (loaded via Flask proxy — no CORS):</p>
      <img src="/pano/{world_db_id}" style="max-width:100%; border:1px solid #333;">
    </body></html>
    """


@app.route("/sanity")
def sanity():
    """Verify Spark itself works in your browser by rendering the official
    Spark demo splat (a butterfly). If this loads, the renderer is fine and
    any 3D-view issues are about the Marble splat URL, not Spark."""
    return """
<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>Spark sanity check</title>
<style>
  body, html { margin:0; padding:0; overflow:hidden; background:#000; height:100%; }
  #info {
    position:fixed; top:16px; left:16px; z-index:10; color:white;
    font-family:-apple-system,sans-serif; background:rgba(0,0,0,0.6);
    padding:10px 14px; border-radius:8px; backdrop-filter:blur(8px);
  }
  #info h1 { margin:0 0 4px; font-size:14px; }
  #info p  { margin:0; font-size:12px; color:#b8b8c2; }
  #info a  { color:#9a8cff; font-size:12px; }
</style>
</head><body>
<div id="info">
  <h1>Spark sanity check (butterfly demo splat)</h1>
  <p>If you see a butterfly, Spark works in your browser. <a href="/">← Back</a></p>
</div>

<script type="importmap">
{
  "imports": {
    "three": "https://cdnjs.cloudflare.com/ajax/libs/three.js/0.180.0/three.module.js",
    "@sparkjsdev/spark": "https://sparkjs.dev/releases/spark/2.0.0/spark.module.js"
  }
}
</script>
<script type="module">
import * as THREE from "three";
import { SparkRenderer, SplatMesh } from "@sparkjsdev/spark";

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(60, innerWidth/innerHeight, 0.01, 1000);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);

const spark = new SparkRenderer({ renderer });
scene.add(spark);

const butterfly = new SplatMesh({
  url: "https://sparkjs.dev/assets/splats/butterfly.spz"
});
butterfly.quaternion.set(1, 0, 0, 0);
butterfly.position.set(0, 0, -3);
scene.add(butterfly);

renderer.setAnimationLoop((time) => {
  butterfly.rotation.y = time / 2000;
  renderer.render(scene, camera);
});
window.addEventListener("resize", () => {
  camera.aspect = innerWidth/innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
</script>
</body></html>
"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)