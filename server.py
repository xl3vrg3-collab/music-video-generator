#!/usr/bin/env python3
"""
Music Video Generator - Web UI Server
Runs on port 3849. No Flask dependency -- uses http.server.

Endpoints:
    GET  /                      Serve the web UI
    GET  /public/<file>         Serve static files
    POST /api/upload            Upload a song file
    POST /api/generate          Start generation (JSON body: {style, filename})
    GET  /api/progress          Poll generation progress
    GET  /api/download          Download the final video
    GET  /output/<file>         Serve output files
"""

import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from lib.audio_analyzer import analyze
from lib.scene_planner import plan_scenes
from lib.video_generator import generate_all
from lib.video_stitcher import stitch

PORT = 3849
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(PROJECT_DIR, "uploads")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
CLIPS_DIR = os.path.join(OUTPUT_DIR, "clips")

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---- Global generation state ----
gen_state = {
    "running": False,
    "progress": [],       # list of {scene, status}
    "total_scenes": 0,
    "phase": "idle",      # idle | analyzing | planning | generating | stitching | done | error
    "error": None,
    "output_file": None,
    "analysis": None,
    "scenes": None,
}
gen_lock = threading.Lock()


def _reset_state():
    gen_state.update({
        "running": False,
        "progress": [],
        "total_scenes": 0,
        "phase": "idle",
        "error": None,
        "output_file": None,
        "analysis": None,
        "scenes": None,
    })


def _run_generation(song_path: str, style: str):
    """Background generation thread."""
    try:
        # Analyze
        with gen_lock:
            gen_state["phase"] = "analyzing"
        analysis = analyze(song_path)
        with gen_lock:
            gen_state["analysis"] = analysis

        # Plan
        with gen_lock:
            gen_state["phase"] = "planning"
        scenes = plan_scenes(analysis, style)
        with gen_lock:
            gen_state["scenes"] = [s.copy() for s in scenes]
            gen_state["total_scenes"] = len(scenes)
            gen_state["progress"] = [
                {"scene": i, "status": "pending", "prompt": s["prompt"]}
                for i, s in enumerate(scenes)
            ]

        # Generate clips
        with gen_lock:
            gen_state["phase"] = "generating"

        def on_progress(index, status):
            with gen_lock:
                if index < len(gen_state["progress"]):
                    gen_state["progress"][index]["status"] = status

        clip_paths = generate_all(scenes, CLIPS_DIR, progress_cb=on_progress)

        valid = [c for c in clip_paths if c]
        if not valid:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No clips were generated successfully"
                gen_state["running"] = False
            return

        # Stitch
        with gen_lock:
            gen_state["phase"] = "stitching"

        output_file = os.path.join(OUTPUT_DIR, "final_video.mp4")
        stitch(clip_paths, song_path, output_file)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["output_file"] = output_file
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


# ---- HTTP handler ----

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Quieter logging
        sys.stderr.write(f"[server] {fmt % args}\n")

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type=None):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        if content_type is None:
            ext = os.path.splitext(path)[1].lower()
            content_type = {
                ".html": "text/html",
                ".css": "text/css",
                ".js": "application/javascript",
                ".json": "application/json",
                ".mp4": "video/mp4",
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".svg": "image/svg+xml",
            }.get(ext, "application/octet-stream")
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    # ---- Routing ----

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._send_file(os.path.join(PROJECT_DIR, "public", "index.html"))

        elif path.startswith("/public/"):
            rel = path[len("/public/"):]
            safe = os.path.normpath(rel)
            self._send_file(os.path.join(PROJECT_DIR, "public", safe))

        elif path == "/api/progress":
            with gen_lock:
                data = {
                    "running": gen_state["running"],
                    "phase": gen_state["phase"],
                    "total_scenes": gen_state["total_scenes"],
                    "progress": gen_state["progress"],
                    "error": gen_state["error"],
                    "analysis": gen_state["analysis"],
                    "scenes": gen_state["scenes"],
                    "has_output": gen_state["output_file"] is not None,
                }
            self._send_json(data)

        elif path == "/api/download":
            with gen_lock:
                out = gen_state["output_file"]
            if out and os.path.isfile(out):
                self._send_file(out, "video/mp4")
            else:
                self.send_error(404, "No video available yet")

        elif path.startswith("/output/"):
            rel = path[len("/output/"):]
            safe = os.path.normpath(rel)
            self._send_file(os.path.join(OUTPUT_DIR, safe))

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/upload":
            self._handle_upload()

        elif path == "/api/generate":
            self._handle_generate()

        else:
            self.send_error(404)

    def _handle_upload(self):
        """Handle multipart file upload."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        # Parse boundary
        parts = content_type.split("boundary=")
        if len(parts) < 2:
            self._send_json({"error": "No boundary in content-type"}, 400)
            return
        boundary = parts[1].strip().encode()

        body = self._read_body()

        # Simple multipart parser
        filename = "uploaded_song.mp3"
        file_data = None

        sections = body.split(b"--" + boundary)
        for section in sections:
            if b"filename=" in section:
                # Extract filename
                header_end = section.find(b"\r\n\r\n")
                if header_end < 0:
                    continue
                header = section[:header_end].decode(errors="replace")
                for line in header.split("\r\n"):
                    if "filename=" in line:
                        # Extract filename from Content-Disposition
                        parts = line.split("filename=")
                        if len(parts) > 1:
                            fn = parts[1].strip().strip('"').strip("'")
                            if fn:
                                filename = os.path.basename(fn)
                file_data = section[header_end + 4:]
                # Remove trailing \r\n-- if present
                if file_data.endswith(b"\r\n"):
                    file_data = file_data[:-2]
                break

        if file_data is None:
            self._send_json({"error": "No file found in upload"}, 400)
            return

        dest = os.path.join(UPLOADS_DIR, filename)
        with open(dest, "wb") as f:
            f.write(file_data)

        self._send_json({"ok": True, "filename": filename, "size": len(file_data)})

    def _handle_generate(self):
        """Start video generation."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return

        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        style = params.get("style", "cinematic, atmospheric")
        filename = params.get("filename", "")

        if not filename:
            self._send_json({"error": "No filename specified"}, 400)
            return

        song_path = os.path.join(UPLOADS_DIR, os.path.basename(filename))
        if not os.path.isfile(song_path):
            self._send_json({"error": f"Song file not found: {filename}"}, 404)
            return

        with gen_lock:
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(
            target=_run_generation,
            args=(song_path, style),
            daemon=True,
        )
        thread.start()

        self._send_json({"ok": True, "message": "Generation started"})


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n  Music Video Generator")
    print(f"  UI running at http://localhost:{PORT}")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
