#!/usr/bin/env python3
"""
LUMN Studio - Web UI Server
Runs on port 3849. No Flask dependency -- uses http.server.

Endpoints:
    GET  /                              Serve the web UI
    GET  /public/<file>                 Serve static files
    POST /api/upload                    Upload a song file
    POST /api/generate                  Start generation (JSON body: {style, filename})
    GET  /api/progress                  Poll generation progress
    GET  /api/download                  Download the final video
    GET  /output/<file>                 Serve output files
    GET  /api/scenes                    Get scene plan with clip URLs
    POST /api/scenes/<index>/regenerate Regenerate a single scene
    POST /api/restitch                  Re-stitch all scenes into final video
    GET  /api/clips/<filename>          Serve individual clip files
    POST /api/references/upload         Upload a reference image
    GET  /api/references                List all references
    DELETE /api/references/<name>       Delete a reference
    GET  /api/references/<name>         Serve a reference image
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime

import time as _time_mod
_server_start_time = _time_mod.time()

# Ensure ffmpeg is on PATH (cross-platform detection)
import shutil
_FFMPEG_PATH = shutil.which("ffmpeg")
if not _FFMPEG_PATH:
    # Try common Windows paths
    _win_paths = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "ffmpeg", "bin", "ffmpeg.exe"),
    ]
    for p in _win_paths:
        if os.path.isfile(p):
            _FFMPEG_PATH = p
            break
if _FFMPEG_PATH:
    _ffmpeg_dir = os.path.dirname(_FFMPEG_PATH)
    if _ffmpeg_dir not in os.environ["PATH"]:
        os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ["PATH"]
else:
    print("[WARN] ffmpeg not found. Video stitching will not work.")
import uuid as _uuid
import urllib.parse
import zipfile
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---- Auth & CSRF tokens ----
_API_TOKEN = os.environ.get("LUMN_API_TOKEN", "")
if not _API_TOKEN:
    _API_TOKEN = secrets.token_hex(16)
    print(f"[AUTH] No LUMN_API_TOKEN set. Generated session token: {_API_TOKEN[:8]}...")
    print(f"[AUTH] Full token in memory only. Set LUMN_API_TOKEN env var for persistence.")
# CSRF: per-session derived tokens. Server keeps a long-lived secret; the
# browser-facing token is HMAC(secret, session_id). No session = no valid
# CSRF token. This kills the old "static global = API key" vulnerability.
_CSRF_SECRET = secrets.token_bytes(32)

def _csrf_for_sid(sid: str) -> str:
    if not sid:
        return ""
    return hmac.new(_CSRF_SECRET, sid.encode("utf-8"), hashlib.sha256).hexdigest()

def _verify_csrf(sid: str, token: str) -> bool:
    if not sid or not token:
        return False
    expected = _csrf_for_sid(sid)
    return hmac.compare_digest(expected, token)

def _build_session_cookie(sid: str, max_age: int | None = None) -> str:
    """Build Set-Cookie value with production-grade attrs.

    Secure is set when LUMN_PRODUCTION=1 — behind Cloudflare Tunnel the edge
    is HTTPS even though the origin is HTTP, and browsers respect Secure as
    long as the request itself arrived via TLS (which CF guarantees at the
    edge after setting CF-Visitor). M3 fix.
    """
    if max_age is None:
        max_age = getattr(lumn_db, "SESSION_TTL_SECONDS", 2592000) if lumn_db else 2592000
    parts = [f"lumn_sid={sid}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if os.environ.get("LUMN_PRODUCTION") == "1":
        parts.append("Secure")
    return "; ".join(parts)

from lib.audio_analyzer import analyze
from lib.scene_planner import plan_scenes, TRANSITION_TYPES, coherence_pass
try:
    import lib.db as lumn_db
    lumn_db.init_db()
except Exception as _e:
    lumn_db = None
    print(f"[DB] init failed, multi-user disabled: {_e}")

try:
    import lib.obs as lumn_obs
except Exception as _e:
    lumn_obs = None
    print(f"[OBS] init failed, metrics disabled: {_e}")

try:
    import lib.validate as lumn_validate
except Exception as _e:
    lumn_validate = None
    print(f"[VALIDATE] init failed, input validation disabled: {_e}")

try:
    import lib.worker as lumn_worker
    import lib.jobs_runners as lumn_jobs_runners
    lumn_jobs_runners.register_all()
except Exception as _e:
    lumn_worker = None
    lumn_jobs_runners = None
    print(f"[WORKER] init failed, async jobs disabled: {_e}")
from lib.video_generator import (
    generate_scene, generate_all, generate_from_photo,
    describe_photo, CAMERA_PRESETS, CAMERA_PROMPT_SUFFIXES,
    get_available_engines, SUPPORTED_ENGINES, ENGINE_GROK,
    _load_settings as _load_gen_settings,
    _get_character_references, _resolve_character_references,
    MODEL_DURATION_OPTIONS, get_valid_duration, get_smart_duration,
    extract_last_frame, extract_first_frame,
)
from lib.video_stitcher import (
    stitch, apply_lyrics_overlay, apply_aspect_ratio, split_clip,
    ASPECT_PRESETS, _get_clip_duration,
    SPEED_OPTIONS, COLOR_GRADE_PRESETS, AUDIO_VIZ_STYLES,
    generate_credits, apply_watermark, extract_thumbnail,
    mix_audio_tracks, export_for_platform, apply_beat_sync_cuts,
    align_scenes_to_beats, overlay_scene_vocals, add_beat_cuts_to_stitch,
    _apply_speed_ramp, _apply_reverse, apply_audio_crossfade, SPEED_RAMP_TYPES,
    apply_loop_boomerang, apply_audio_ducking, export_gif,
    apply_effect, reverse_clip, boomerang_clip, SCENE_EFFECTS,
)
from lib.prompt_assistant import (
    STYLE_PRESETS, get_preset, enhance_prompt, suggest_from_song_name,
    get_preset_names, suggest_style, suggest_genre_from_bpm,
    extract_palette,
)
from lib.storyboard_generator import generate_storyboard
from lib.project_manager import ProjectManager  # retained for back-compat imports; no longer instantiated
from lib import active_project
PORT = int(os.environ.get("PORT", "3849"))
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(PROJECT_DIR, "uploads")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

# LUMN per-user credits now track the live fal.ai balance (sync'd on each
# /api/auth/me probe). Default: gate is active. Set LUMN_DEFER_USER_CREDITS=1
# to bypass (for local dev when admin key isn't configured yet).
def _user_credits_deferred() -> bool:
    v = os.environ.get("LUMN_DEFER_USER_CREDITS", "0").strip().lower()
    return v in ("1", "true", "yes", "on")

def _safe_user_path(p: str) -> str | None:
    """Validate that a user-supplied file path points inside our pipeline
    output root or uploads dir. Returns the realpath on success, None on
    rejection. SECURITY (C3): without this, a caller can post any absolute
    path and the worker will upload it to fal.ai (arbitrary local file
    exfiltration of .ssh/id_rsa, .env, etc).
    """
    if not p or not isinstance(p, str):
        return None
    # Reject obvious nasties up front
    if "\x00" in p:
        return None
    try:
        resolved = os.path.realpath(p)
    except Exception:
        return None
    if not os.path.isfile(resolved):
        return None
    # Must live under one of the allowed roots. `output/` as a whole is
    # server-managed content (generated sheets, packages, pipelines) — safe
    # to pass as refs. `uploads/` covers user-uploaded references.
    allowed_roots = [
        os.path.realpath(OUTPUT_DIR),
        os.path.realpath(UPLOADS_DIR),
    ]
    for root in allowed_roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    return None


def _safe_project_ref_path(p: str, project_slug: str | None = None) -> str | None:
    """Validate that a reference path is safe AND scoped to the active
    project. Rejects paths under `output/preproduction/` (legacy orphan
    catalog) and any other project's workspace. Returns realpath on success,
    None on rejection. Belt-and-suspenders against the Apr 20 cross-project
    leak (Buddy/Owen/Maya sheets contaminating TB anchor generation).
    """
    safe = _safe_user_path(p)
    if not safe:
        return None
    slug = project_slug or "default"
    try:
        from lib import active_project as _ap
        if not project_slug:
            slug = _ap.get_active_slug() or "default"
    except Exception:
        slug = project_slug or "default"
    # Allowed roots for a project-scoped anchor generation:
    project_root = os.path.realpath(os.path.join(OUTPUT_DIR, "projects", slug))
    refs_v6 = os.path.realpath(os.path.join(OUTPUT_DIR, "pipeline", "references_v6"))
    anchors_v6 = os.path.realpath(os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6"))
    uploads_root = os.path.realpath(UPLOADS_DIR)
    allowed = [project_root, refs_v6, anchors_v6, uploads_root]
    for root in allowed:
        if safe == root or safe.startswith(root + os.sep):
            return safe
    # Explicit denial: anything under preproduction/ (legacy packages) is
    # rejected. Same for any other project's workspace.
    return None


# Lock for all scene/movie plan file writes to prevent concurrent corruption
_plan_file_lock = threading.Lock()

from lib.prompt_os import PromptOS
_prompt_os = PromptOS()


def _check_project_header(handler):
    """Enforce X-Lumn-Project header parity on mutating routes.

    The frontend sends `X-Lumn-Project: <slug>` so the server can detect when
    a tab is acting against a project different from the currently-active
    one (e.g. the user switched projects in another tab). If the header is
    absent the request passes through — older clients and bearer-token
    callers may not send it. If it's present and mismatched we return a 409
    so the caller can refresh and retry.

    Returns None on success, or a (status, dict) tuple on mismatch.
    """
    expected = handler.headers.get("X-Lumn-Project")
    if not expected:
        return None
    try:
        actual = active_project.get_active_slug()
    except Exception:
        return None
    if expected != actual:
        return (409, {"error": "project_mismatch",
                      "expected": expected, "active": actual})
    return None


# Paths that touch project-scoped state. Used by do_POST/do_PUT/do_DELETE
# to decide whether to enforce the X-Lumn-Project header. Auth/upload routes
# and /api/projects (create) are deliberately excluded.
_PROJECT_SCOPED_PREFIXES = (
    "/api/pos/",
    "/api/vault/",
    "/api/shots/",
    "/api/scenes/",
    "/api/manual/",
    "/api/takes/",
    "/api/voice/",
    "/api/sheet/",
    "/api/char/",
    "/api/style-transfer",
    "/api/approve",
    "/api/lock",
    "/api/continuity",
)


def _is_project_scoped_mutation(path: str) -> bool:
    """True if the path mutates project-scoped state and should enforce
    the X-Lumn-Project header. /api/projects is handled per-route so create
    is exempt but active/rename/delete/snapshot enforce the header at the
    route level."""
    if not path.startswith("/api/"):
        return False
    if path.startswith("/api/auth/"):
        return False
    if path == "/api/upload" or path == "/api/feedback":
        return False
    # /api/projects and the create endpoint are handled per-route.
    if path == "/api/projects" or path == "/api/projects/":
        return False
    for pref in _PROJECT_SCOPED_PREFIXES:
        if path.startswith(pref):
            return True
    return False
# NOTE: PROMPT_OS_DATA_DIR used to point at the single shared
# output/prompt_os/ workspace. Under the multi-project refactor each project
# owns its own prompt_os/ at output/projects/<slug>/prompt_os/. Resolve it
# dynamically against the active project instead of caching a frozen path.
def _prompt_os_data_dir() -> str:
    return _prompt_os._pos_dir()

from lib.auto_director import AutoDirector, get_workflow_presets, save_custom_preset
from lib.movie_planner import (
    create_movie_plan, load_movie_plan, save_movie_plan, rebuild_bible_from_plan,
    MovieBible, BeatPlanner, SceneBuilder, AssetCoverage, PlanValidator,
    SceneRegenerator, PromptBuilder, _safe_id, _safe_name,
)
MOVIE_PLAN_PATH = os.path.join(OUTPUT_DIR, "movie_plan.json")

from lib.draft_assets import (
    init as _init_draft_assets,
    get_all_drafts as _get_all_drafts,
    get_draft as _get_draft,
    create_draft as _create_draft,
    promote_to_library as _promote_draft,
    remove_draft as _remove_draft,
    replace_draft_id_in_scenes as _replace_draft_in_scenes,
    creation_readiness as _creation_readiness,
    extract_drafts_from_plan as _extract_drafts,
    clear_all as _clear_all_drafts,
)
_init_draft_assets(OUTPUT_DIR)

AUTO_DIRECTOR_CLIPS_DIR = os.path.join(OUTPUT_DIR, "auto_director_clips")
os.makedirs(AUTO_DIRECTOR_CLIPS_DIR, exist_ok=True)
_auto_director = AutoDirector(OUTPUT_DIR, AUTO_DIRECTOR_CLIPS_DIR, _prompt_os)
AUTO_DIRECTOR_PLAN_PATH = os.path.join(OUTPUT_DIR, "auto_director_plan.json")
CLIPS_DIR = os.path.join(OUTPUT_DIR, "clips")
REFERENCES_DIR = os.path.join(PROJECT_DIR, "references")
SCENE_PLAN_PATH = os.path.join(OUTPUT_DIR, "scene_plan.json")
MANUAL_PLAN_PATH = os.path.join(OUTPUT_DIR, "manual_scene_plan.json")
MANUAL_CLIPS_DIR = os.path.join(OUTPUT_DIR, "manual_clips")
SCENE_PHOTOS_DIR = os.path.join(UPLOADS_DIR, "scene_photos")
EXPORTS_DIR = os.path.join(OUTPUT_DIR, "exports")
PROJECTS_DIR = os.path.join(OUTPUT_DIR, "projects")
COST_TRACKER_PATH = os.path.join(OUTPUT_DIR, "cost_tracker.json")
STORYBOARD_DIR = os.path.join(OUTPUT_DIR, "storyboards")
PREVIEWS_DIR = os.path.join(OUTPUT_DIR, "previews")
WATERMARK_PATH = os.path.join(OUTPUT_DIR, "watermark.png")
THUMBNAIL_PATH = os.path.join(OUTPUT_DIR, "thumbnail.jpg")
AUDIO_TRACKS_DIR = os.path.join(UPLOADS_DIR, "audio_tracks")
SOCIAL_EXPORTS_DIR = os.path.join(OUTPUT_DIR, "social_exports")
KEYFRAMES_DIR = os.path.join(OUTPUT_DIR, "keyframes")
SCENE_VIDEOS_DIR = os.path.join(UPLOADS_DIR, "scene_videos")
SCENE_VOCALS_DIR = os.path.join(UPLOADS_DIR, "scene_vocals")
FULL_PROJECTS_DIR = os.path.join(OUTPUT_DIR, "full_projects")
SETTINGS_PATH = os.path.join(OUTPUT_DIR, "settings.json")
PROMPT_HISTORY_PATH = os.path.join(OUTPUT_DIR, "prompt_history.json")
AUTOSAVE_PATH = os.path.join(OUTPUT_DIR, "autosave.json")
TEMPLATES_DIR = os.path.join(OUTPUT_DIR, "templates")
GIFS_DIR = os.path.join(OUTPUT_DIR, "gifs")
# POS asset-dir resolvers — each call lands inside the active project's
# prompt_os/ subtree. These replaced module-level POS_* path constants which
# could not survive a project switch without a process restart.
def _pos_photos_dir(kind: str) -> str:
    """kind: 'char' | 'costume' | 'env' | 'reference' (or legacy 'prop') | 'voice'."""
    return _prompt_os._photos_dir(kind)

def _pos_previews_dir(kind: str) -> str:
    """kind: 'char' | 'costume' | 'env' | 'reference' (or legacy 'prop')."""
    return _prompt_os._previews_dir(kind)

def _pos_voices_dir() -> str:
    return _prompt_os._photos_dir("voice")

# Render time estimation constants (seconds per clip by engine/model)
RENDER_TIME_ESTIMATES = {
    # Runway models
    "gen4_5": 90, "gen4.5": 90,
    "gen3a_turbo": 45,
    # Kling models
    "kling_pro": 120, "kling3.0_pro": 120,
    "kling_standard": 60, "kling3.0_standard": 60,
    # Google Veo models
    "veo3": 120,
    "veo3_1": 120, "veo3.1": 120,
    "veo3_1_fast": 45, "veo3.1_fast": 45,
    # Other engines
    "grok": 30,
    "luma": 45,
    "openai": 50,
    # Generic fallbacks
    "runway": 90,
}
STITCH_TIME_PER_CLIP = 5
WAVEFORM_CACHE_PATH = os.path.join(OUTPUT_DIR, "waveform_cache.json")
TAKES_DIR = os.path.join(OUTPUT_DIR, "takes")

# Cost defaults (fallback when tier/duration not supplied)
COST_PER_VIDEO_GEN = 0.15
COST_PER_IMAGE_GEN = 0.02
DEFAULT_BUDGET = 10.00

# Real per-second pricing for accurate ledger recording
KLING_PRICE_PER_SEC = {
    "v3_standard": 0.084,
    "v3_pro": 0.112,
    "o3_standard": 0.084,
    "o3_pro": 0.392,
}
# Flat image prices
IMAGE_PRICE_BY_ENGINE = {
    "gemini_2.5_flash": 0.039,
    "gemini": 0.039,
    "flux": 0.025,
    "grok": 0.04,
    "sdxl": 0.02,
}

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(REFERENCES_DIR, exist_ok=True)
os.makedirs(MANUAL_CLIPS_DIR, exist_ok=True)
os.makedirs(SCENE_PHOTOS_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)
os.makedirs(PROJECTS_DIR, exist_ok=True)
os.makedirs(STORYBOARD_DIR, exist_ok=True)
os.makedirs(PREVIEWS_DIR, exist_ok=True)
os.makedirs(AUDIO_TRACKS_DIR, exist_ok=True)
os.makedirs(SOCIAL_EXPORTS_DIR, exist_ok=True)
os.makedirs(KEYFRAMES_DIR, exist_ok=True)
os.makedirs(SCENE_VIDEOS_DIR, exist_ok=True)
os.makedirs(SCENE_VOCALS_DIR, exist_ok=True)
os.makedirs(FULL_PROJECTS_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(GIFS_DIR, exist_ok=True)
os.makedirs(TAKES_DIR, exist_ok=True)
# POS photo/preview dirs are created on-demand by PromptOS._photos_dir /
# _previews_dir inside the active project scaffold, so no eager mkdir here.

# ---- Project Manager (RETIRED) ----
# The old ProjectManager provided a copy-in/copy-out workspace model. Under
# the multi-project refactor each project lives at
# output/projects/<slug>/ and switching is just set_active_slug(). The
# ProjectManager import is retained for any external module that still
# references the class name, but the server no longer instantiates it.
# See lib/active_project.py for the current registry.

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
    "song_path": None,
    "cancel_requested": False,  # user clicked Stop on the render screen
}
gen_lock = threading.Lock()

# ---- Batch queue state (Feature 2 + 10) ----
batch_queue_state = {
    "active": False,
    "cancelled": False,
    "scenes": [],        # [{id, index, status, elapsed, error, prompt}]
    "total": 0,
    "completed": 0,
    "failed": 0,
    "start_time": 0,
}
batch_lock = threading.Lock()

# ---- Generation Queue (Real-Time) ----
gen_queue = {
    "items": [],        # [{id, shot_id, scene_id, shot_data, status, progress, result_url, error, started_at, completed_at}]
    "max_parallel": 2,
    "active_count": 0,
}
gen_queue_lock = threading.Lock()

# ---- Preview-first pipeline state ----
preview_state = {
    "running": False,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "results": {},   # index -> {status, preview_url, error}
}
preview_lock = threading.Lock()

SCENE_THUMBNAILS_DIR = os.path.join(OUTPUT_DIR, "scene_thumbnails")
os.makedirs(SCENE_THUMBNAILS_DIR, exist_ok=True)

# ──── Scene Preview State (for batch preview generation) ────
scene_preview_state = {
    "running": False,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "scenes": [],  # list of {index, preview}
}
scene_preview_lock = threading.Lock()


def _compute_scene_fingerprint(scene):
    """Compute hash of fields that affect visual generation."""
    visual_fields = [
        scene.get("summary", ""),
        scene.get("action", ""),
        scene.get("shot_prompt", ""),
        scene.get("prompt", ""),
        scene.get("camera_direction", ""),
        scene.get("lighting_direction", ""),
        scene.get("color_direction", ""),
        scene.get("motion_direction", ""),
        str(scene.get("characters", [])),
        str(scene.get("costumes", [])),
        str(scene.get("environments", [])),
        str(scene.get("emotional_shift", {})),
        str(scene.get("visual_shift", {})),
        str(scene.get("duration", "")),
        scene.get("transition_in", ""),
        scene.get("transition_out", ""),
    ]
    combined = "|".join(visual_fields)
    return hashlib.md5(combined.encode()).hexdigest()[:12]


def _queue_add(shot_id: str, scene_id: str, shot_data: dict) -> dict:
    """Add a shot to the generation queue."""
    import uuid as _u
    item = {
        "id": _u.uuid4().hex[:8],
        "shot_id": shot_id,
        "scene_id": scene_id,
        "shot_data": shot_data,
        "status": "pending",
        "progress": "",
        "result_url": None,
        "error": None,
        "started_at": None,
        "completed_at": None,
    }
    with gen_queue_lock:
        gen_queue["items"].append(item)
    _queue_process()
    return item


def _queue_process():
    """Start processing pending items up to max_parallel."""
    with gen_queue_lock:
        active = sum(1 for i in gen_queue["items"] if i["status"] == "generating")
        pending = [i for i in gen_queue["items"] if i["status"] == "pending"]
        slots = gen_queue["max_parallel"] - active

    for item in pending[:slots]:
        t = threading.Thread(target=_queue_generate_item, args=(item,), daemon=True)
        t.start()


def _queue_generate_item(item: dict):
    """Generate a single queue item in a background thread."""
    with gen_queue_lock:
        item["status"] = "generating"
        item["started_at"] = time.time()
        item["progress"] = "starting..."

    try:
        shot = item["shot_data"]
        scene_id = item["scene_id"]

        # Build prompt using the shot prompt engine
        from lib.prompt_assembler import compile_shot_prompt

        # Load scene entities
        scene_data = _prompt_os.get_scene(scene_id) if scene_id else None
        char = None
        costume = None
        env = None
        if scene_data:
            if scene_data.get("characterId"):
                char = _prompt_os.get_character(scene_data["characterId"])
            if scene_data.get("costumeId"):
                costume = _prompt_os.get_costume(scene_data["costumeId"])
            if scene_data.get("environmentId"):
                env = _prompt_os.get_environment(scene_data["environmentId"])

        settings = _load_settings()
        ds = settings.get("director_state", {})

        compiled = compile_shot_prompt(
            shot=shot,
            character=char, costume=costume, environment=env,
            global_style=ds.get("universalPrompt", ""),
            world_setting=ds.get("worldSetting", ""),
            tier="cinematic",
        )

        # Build character description for the generator
        char_description = ""
        if char:
            desc_parts = []
            phys = char.get("physicalDescription", char.get("description", ""))
            if phys:
                desc_parts.append(phys)
            if char.get("hair"):
                desc_parts.append(char["hair"])
            if char.get("skinTone"):
                desc_parts.append(f"{char['skinTone']} skin")
            char_description = ", ".join(desc_parts)

        # Resolve character photo
        photo_path = None
        if char and char.get("referencePhoto"):
            ref = char["referencePhoto"]
            import re as _re_q
            if os.path.isfile(ref):
                photo_path = ref
            else:
                m = _re_q.search(r"/api/pos/characters/([^/]+)/photo", ref)
                if m:
                    cid = m.group(1)
                    for ext in (".jpg", ".jpeg", ".png", ".webp"):
                        candidate = os.path.join(_pos_photos_dir("char"), f"{cid}{ext}")
                        if os.path.isfile(candidate):
                            photo_path = candidate
                            break

        # Resolve costume photo and description
        q_cos_desc = ""
        q_cos_photo = ""
        if costume:
            q_cos_desc = costume.get("description", "")
            if not q_cos_desc:
                _cp = [costume.get("upperBody", ""), costume.get("lowerBody", "")]
                q_cos_desc = ", ".join(p for p in _cp if p)
            import re as _re_qco
            _cref = costume.get("referenceImagePath", "")
            if _cref:
                if os.path.isfile(_cref):
                    q_cos_photo = _cref
                else:
                    _mqco = _re_qco.search(r"/api/pos/costumes/([^/]+)/photo", _cref)
                    if _mqco:
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            candidate = os.path.join(_pos_photos_dir("costume"), f"{_mqco.group(1)}{ext}")
                            if os.path.isfile(candidate):
                                q_cos_photo = candidate
                                break

        # Resolve environment photo and description
        q_env_desc = ""
        q_env_photo = ""
        if env:
            _ep = []
            if env.get("description"): _ep.append(env["description"])
            if env.get("lighting"): _ep.append(env["lighting"])
            if env.get("atmosphere"): _ep.append(env["atmosphere"])
            q_env_desc = ", ".join(_ep)
            import re as _re_qen
            _eref = env.get("referenceImagePath", "")
            if _eref:
                if os.path.isfile(_eref):
                    q_env_photo = _eref
                else:
                    _mqen = _re_qen.search(r"/api/pos/environments/([^/]+)/photo", _eref)
                    if _mqen:
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            candidate = os.path.join(_pos_photos_dir("env"), f"{_mqen.group(1)}{ext}")
                            if os.path.isfile(candidate):
                                q_env_photo = candidate
                                break

        # Determine engine
        engine = ds.get("engine", settings.get("default_engine", "gen4_5"))

        gen_scene = {
            "prompt": compiled["prompt"],
            "duration": shot.get("duration", 4),
            "camera_movement": (shot.get("camera", {}).get("movement") or "static"),
            "engine": engine,
            "id": item["shot_id"],
            "character_description": char_description,
            "costume_description": q_cos_desc,
            "costume_photo_path": q_cos_photo,
            "environment_description": q_env_desc,
            "environment_photo_path": q_env_photo,
        }
        # Keyframe passthrough
        if shot.get("first_frame_path"):
            gen_scene["first_frame_path"] = shot["first_frame_path"]
        if shot.get("last_frame_path"):
            gen_scene["last_frame_path"] = shot["last_frame_path"]

        def on_progress(idx, status):
            item["progress"] = status

        shot_idx = shot.get("shot_number", 1) - 1
        clip_path = generate_scene(gen_scene, shot_idx, MANUAL_CLIPS_DIR,
                                    progress_cb=on_progress, cost_cb=_record_cost,
                                    photo_path=photo_path)

        # Extract last frame for continuity
        try:
            from lib.cinematic_engine import extract_last_frame
            last_frame = extract_last_frame(clip_path)
            if last_frame:
                shot["reference_frame"] = last_frame
        except Exception:
            pass

        mtime = int(os.path.getmtime(clip_path))
        with gen_queue_lock:
            item["status"] = "completed"
            item["result_url"] = f"/api/clips/{os.path.basename(clip_path)}?v={mtime}"
            item["completed_at"] = time.time()
            item["progress"] = "done"

        # Update shot data in settings
        _queue_save_shot_clip(scene_id, item["shot_id"], clip_path)

    except Exception as e:
        with gen_queue_lock:
            item["status"] = "failed"
            item["error"] = str(e)
            item["completed_at"] = time.time()
            item["progress"] = f"failed: {str(e)[:60]}"
        print(f"[QUEUE] Shot {item['shot_id']} failed: {e}")

    # Process next pending items
    _queue_process()


def _queue_save_shot_clip(scene_id: str, shot_id: str, clip_path: str):
    """Save generated clip path back to shot data."""
    settings = _load_settings()
    shots = settings.get("shots_data", {}).get(scene_id, [])
    for shot in shots:
        if shot.get("id") == shot_id:
            shot["clip_path"] = clip_path
            shot["status"] = "generated"
            break
    settings.setdefault("shots_data", {})[scene_id] = shots
    _save_settings(settings)


def _reset_state():
    """Reset gen_state to idle. Caller MUST hold gen_lock (or use _reset_state_locked)."""
    gen_state.update({
        "running": False,
        "progress": [],
        "total_scenes": 0,
        "phase": "idle",
        "error": None,
        "output_file": None,
        "analysis": None,
        "scenes": None,
        "song_path": None,
        "cancel_requested": False,
    })


def _reset_state_locked():
    """Reset gen_state, acquiring gen_lock internally."""
    with gen_lock:
        _reset_state()


def _get_references() -> dict:
    """Get all reference images as {name: path}."""
    refs = {}
    if os.path.isdir(REFERENCES_DIR):
        for fname in os.listdir(REFERENCES_DIR):
            fpath = os.path.join(REFERENCES_DIR, fname)
            if os.path.isfile(fpath):
                name = os.path.splitext(fname)[0]
                refs[name] = fpath
    return refs


def _record_prompt_history(prompt: str, scene_index: int = -1):
    """Record a prompt to the prompt history file."""
    if not prompt:
        return
    if os.path.isfile(PROMPT_HISTORY_PATH):
        try:
            with open(PROMPT_HISTORY_PATH, "r") as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = {"prompts": []}
    else:
        history = {"prompts": []}

    # Avoid duplicate consecutive entries
    if history["prompts"] and history["prompts"][0].get("prompt") == prompt:
        return

    history["prompts"].insert(0, {
        "prompt": prompt,
        "starred": False,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scene_index": scene_index,
    })

    # Keep last 100 entries
    history["prompts"] = history["prompts"][:100]

    with open(PROMPT_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def _save_scene_plan(scenes, clip_paths, song_path, output_path):
    """Save scene plan to JSON."""
    plan = {
        "song_path": song_path,
        "output_path": output_path,
        "scenes": [],
    }
    for i, scene in enumerate(scenes):
        entry = dict(scene)
        entry["index"] = i
        entry["clip_path"] = clip_paths[i] if i < len(clip_paths) else None
        if "matched_references" not in entry:
            entry["matched_references"] = []
        plan["scenes"].append(entry)
    with _plan_file_lock:
        with open(SCENE_PLAN_PATH, "w") as f:
            json.dump(plan, f, indent=2)
    return plan


def _load_scene_plan():
    """Load scene plan from JSON. Falls back to auto_director_plan if scene_plan missing."""
    if os.path.isfile(SCENE_PLAN_PATH):
        try:
            with open(SCENE_PLAN_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {"scenes": []}
    # Fallback: try auto director plan and convert
    if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
        try:
            with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                ad_plan = json.load(f)
        except (json.JSONDecodeError, ValueError):
            ad_plan = {"scenes": []}
        if ad_plan and ad_plan.get("scenes"):
            _sync_auto_plan_to_scene_plan(ad_plan)
            return _load_scene_plan()
    return None


def _sync_auto_plan_to_scene_plan(ad_plan=None):
    """Copy auto_director_plan.json into scene_plan.json format for generation pipeline compatibility."""
    try:
        if ad_plan is None:
            if not os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
                return
            try:
                with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                    ad_plan = json.load(f)
            except (json.JSONDecodeError, ValueError):
                ad_plan = {"scenes": []}
        scenes = ad_plan.get("scenes", [])
        plan = {
            "song_path": ad_plan.get("song_path", ""),
            "style": ad_plan.get("style", ""),
            "scenes": []
        }
        for i, scene in enumerate(scenes):
            entry = dict(scene)
            entry["index"] = i
            if "clip_path" not in entry:
                entry["clip_path"] = None
            plan["scenes"].append(entry)
        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
    except Exception as e:
        print(f"[SYNC] Error syncing auto plan to scene plan: {e}")


def _enrich_scene_with_assets(scene):
    """Resolve asset IDs in a scene to photo paths and descriptions for generation.

    Works with both new movie planner scenes (structured characters/costumes/environments arrays)
    and legacy scenes (characterId/costumeId/environmentId fields).
    """
    import re

    # --- Resolve character ---
    char_description = scene.get("character_description", "")
    char_photo_path = scene.get("character_photo_path", "")

    # Try structured characters array first (new movie planner)
    chars = scene.get("characters", [])
    if chars and isinstance(chars, list):
        for char_ref in chars:
            char_id = char_ref.get("id", "") if isinstance(char_ref, dict) else ""
            if char_id:
                pos_char = _prompt_os.get_character(char_id)
                if pos_char:
                    if not char_description:
                        parts = []
                        if pos_char.get("physicalDescription"): parts.append(pos_char["physicalDescription"])
                        elif pos_char.get("physical"): parts.append(pos_char["physical"])
                        if pos_char.get("name"): parts.insert(0, pos_char["name"])
                        char_description = ", ".join(parts)
                    # Set character sheet flag from POS record
                    if pos_char.get("isCharacterSheet"):
                        scene["is_character_sheet"] = True
                    # Resolve photo — characters use "referencePhoto", not "referenceImagePath"
                    ref_img = pos_char.get("referencePhoto", "") or pos_char.get("referenceImagePath", "")
                    if ref_img and not char_photo_path:
                        if os.path.isfile(ref_img):
                            char_photo_path = ref_img
                        else:
                            m = re.search(r"/api/pos/characters/([^/]+)/photo", ref_img)
                            if m:
                                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    candidate = os.path.join(_pos_photos_dir("char"), f"{m.group(1)}{ext}")
                                    if os.path.isfile(candidate):
                                        char_photo_path = candidate
                                        break
                    # Auto-describe character from photo if no description exists
                    if char_photo_path and (not char_description or char_description == pos_char.get("name", "")):
                        try:
                            from lib.video_generator import _describe_entity_photo
                            vision_desc = _describe_entity_photo(char_photo_path, "character")
                            if vision_desc:
                                char_description = vision_desc
                                # Save it back so we don't re-describe every time
                                _prompt_os.update_character(char_id, {"physicalDescription": vision_desc})
                                import sys as _esd
                                _esd.stderr.write(f"[ENRICH] Auto-described character from photo ({len(vision_desc)} chars)\n")
                                _esd.stderr.flush()
                        except Exception as vd_err:
                            import sys as _esd2
                            _esd2.stderr.write(f"[ENRICH] Auto-describe failed: {vd_err}\n")
                            _esd2.stderr.flush()
                    break  # Use first character as primary

    # Fallback: legacy characterId field
    if not char_description and not char_photo_path:
        char_id = scene.get("characterId", "")
        if char_id:
            pos_char = _prompt_os.get_character(char_id)
            if pos_char:
                parts = []
                if pos_char.get("physical"): parts.append(pos_char["physical"])
                if pos_char.get("name"): parts.insert(0, pos_char["name"])
                char_description = ", ".join(parts)
                ref_img = pos_char.get("referencePhoto", "") or pos_char.get("referenceImagePath", "")
                if ref_img:
                    if os.path.isfile(ref_img):
                        char_photo_path = ref_img
                    else:
                        m = re.search(r"/api/pos/characters/([^/]+)/photo", ref_img)
                        if m:
                            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                                candidate = os.path.join(_pos_photos_dir("char"), f"{m.group(1)}{ext}")
                                if os.path.isfile(candidate):
                                    char_photo_path = candidate
                                    break

    # --- Resolve costume ---
    costume_description = scene.get("costume_description", "")
    costume_photo_path = scene.get("costume_photo_path", "")

    costumes = scene.get("costumes", [])
    if costumes and isinstance(costumes, list):
        for cos_ref in costumes:
            cos_id = cos_ref.get("id", "") if isinstance(cos_ref, dict) else ""
            if cos_id:
                pos_costume = _prompt_os.get_costume(cos_id)
                if pos_costume:
                    if not costume_description:
                        if pos_costume.get("description"):
                            costume_description = pos_costume["description"]
                        else:
                            c_parts = [pos_costume.get(k, "") for k in ("upperBody", "lowerBody", "footwear", "accessories") if pos_costume.get(k)]
                            costume_description = ", ".join(c_parts)
                    ref_img = pos_costume.get("referenceImagePath", "")
                    if ref_img and not costume_photo_path:
                        if os.path.isfile(ref_img):
                            costume_photo_path = ref_img
                        else:
                            m = re.search(r"/api/pos/costumes/([^/]+)/photo", ref_img)
                            if m:
                                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    candidate = os.path.join(_pos_photos_dir("costume"), f"{m.group(1)}{ext}")
                                    if os.path.isfile(candidate):
                                        costume_photo_path = candidate
                                        break
                    break

    if not costume_description and not costume_photo_path:
        cos_id = scene.get("costumeId", "")
        if cos_id:
            pos_costume = _prompt_os.get_costume(cos_id)
            if pos_costume:
                if pos_costume.get("description"):
                    costume_description = pos_costume["description"]
                ref_img = pos_costume.get("referenceImagePath", "")
                if ref_img:
                    if os.path.isfile(ref_img):
                        costume_photo_path = ref_img
                    else:
                        m = re.search(r"/api/pos/costumes/([^/]+)/photo", ref_img)
                        if m:
                            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                                candidate = os.path.join(_pos_photos_dir("costume"), f"{m.group(1)}{ext}")
                                if os.path.isfile(candidate):
                                    costume_photo_path = candidate
                                    break

    # --- Resolve environment ---
    env_description = scene.get("environment_description", "")
    env_photo_path = scene.get("environment_photo_path", "")

    envs = scene.get("environments", [])
    if envs and isinstance(envs, list):
        for env_ref in envs:
            env_id = env_ref.get("id", "") if isinstance(env_ref, dict) else ""
            if env_id:
                pos_env = _prompt_os.get_environment(env_id)
                if pos_env:
                    if not env_description:
                        e_parts = [pos_env.get(k, "") for k in ("name", "description", "locationType", "timeOfDay") if pos_env.get(k)]
                        env_description = ", ".join(e_parts)
                    ref_img = pos_env.get("referenceImagePath", "")
                    if ref_img and not env_photo_path:
                        if os.path.isfile(ref_img):
                            env_photo_path = ref_img
                        else:
                            m = re.search(r"/api/pos/environments/([^/]+)/photo", ref_img)
                            if m:
                                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    candidate = os.path.join(_pos_photos_dir("env"), f"{m.group(1)}{ext}")
                                    if os.path.isfile(candidate):
                                        env_photo_path = candidate
                                        break
                    break

    if not env_description and not env_photo_path:
        env_id = scene.get("environmentId", "")
        if env_id:
            pos_env = _prompt_os.get_environment(env_id)
            if pos_env:
                e_parts = [pos_env.get(k, "") for k in ("name", "description", "locationType", "timeOfDay") if pos_env.get(k)]
                env_description = ", ".join(e_parts)
                ref_img = pos_env.get("referenceImagePath", "")
                if ref_img:
                    if os.path.isfile(ref_img):
                        env_photo_path = ref_img
                    else:
                        m = re.search(r"/api/pos/environments/([^/]+)/photo", ref_img)
                        if m:
                            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                                candidate = os.path.join(_pos_photos_dir("env"), f"{m.group(1)}{ext}")
                                if os.path.isfile(candidate):
                                    env_photo_path = candidate
                                    break

    # Collect ALL character photos (not just the first)
    all_char_photos = []
    all_costume_photos = []
    chars_list = scene.get("characters", [])
    if chars_list and isinstance(chars_list, list):
        for char_ref in chars_list:
            cid = char_ref.get("id", "") if isinstance(char_ref, dict) else ""
            if not cid:
                continue
            pc = _prompt_os.get_character(cid)
            if not pc:
                continue
            ri = pc.get("referencePhoto", "") or pc.get("referenceImagePath", "")
            if ri:
                resolved = None
                if os.path.isfile(ri):
                    resolved = ri
                else:
                    m = re.search(r"/api/pos/characters/([^/]+)/photo", ri)
                    if m:
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            candidate = os.path.join(_pos_photos_dir("char"), f"{m.group(1)}{ext}")
                            if os.path.isfile(candidate):
                                resolved = candidate
                                break
                if resolved:
                    all_char_photos.append(resolved)

    costumes_list = scene.get("costumes", [])
    if costumes_list and isinstance(costumes_list, list):
        for cos_ref in costumes_list:
            cid = cos_ref.get("id", "") if isinstance(cos_ref, dict) else ""
            if not cid:
                continue
            pc = _prompt_os.get_costume(cid)
            if not pc:
                continue
            ri = pc.get("referenceImagePath", "")
            if ri:
                resolved = None
                if os.path.isfile(ri):
                    resolved = ri
                else:
                    m = re.search(r"/api/pos/costumes/([^/]+)/photo", ri)
                    if m:
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            candidate = os.path.join(_pos_photos_dir("costume"), f"{m.group(1)}{ext}")
                            if os.path.isfile(candidate):
                                resolved = candidate
                                break
                if resolved:
                    all_costume_photos.append(resolved)

    # Apply resolved fields
    scene["character_description"] = char_description
    scene["character_photo_path"] = char_photo_path  # Primary (first) character
    scene["character_photo_paths"] = all_char_photos  # ALL character photos
    scene["costume_description"] = costume_description
    scene["costume_photo_path"] = costume_photo_path  # Primary (first) costume
    scene["costume_photo_paths"] = all_costume_photos  # ALL costume photos
    scene["environment_description"] = env_description
    scene["environment_photo_path"] = env_photo_path

    import sys as _enr_sys
    _enr_sys.stderr.write(f"[ENRICH] {len(all_char_photos)} char photos, {len(all_costume_photos)} costume photos, env={'YES' if env_photo_path else 'NO'}\n")
    _enr_sys.stderr.flush()

    return scene


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
        references = _get_references()
        scenes = plan_scenes(analysis, style, references=references)
        with gen_lock:
            gen_state["scenes"] = [s.copy() for s in scenes]
            gen_state["total_scenes"] = len(scenes)
            gen_state["progress"] = [
                {"scene": i, "status": "pending", "prompt": s["prompt"]}
                for i, s in enumerate(scenes)
            ]

        # Enrich scenes with asset data before saving
        for s in scenes:
            _enrich_scene_with_assets(s)

        # Save draft scene plan immediately (no clip paths yet).
        # The preview-first pipeline takes over from here:
        #   1. Frontend shows thumbnails (via /api/preview-thumbnail/batch)
        #   2. User approves/rejects scenes
        #   3. User clicks "Generate Approved" -> POST /api/generate-approved
        output_file = os.path.join(OUTPUT_DIR, "final_video.mp4")
        _save_scene_plan(scenes, [None] * len(scenes), song_path, output_file)

        # Signal frontend to enter preview review mode
        with gen_lock:
            gen_state["phase"] = "preview_pending"
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


def _chain_scene_keyframes(plan: dict) -> dict:
    """
    Auto-chain keyframes across scenes.
    For each scene after the first, if no explicit first_frame is set,
    extract the last frame from the previous scene's generated clip
    and set it as this scene's first_frame_path.

    Args:
        plan: scene plan dict with "scenes" list

    Returns:
        modified plan dict
    """
    scenes = plan.get("scenes", [])
    chained_count = 0
    for i, scene in enumerate(scenes):
        if i == 0:
            continue
        # Skip if scene already has an explicit first_frame_path
        if scene.get("first_frame_path") and os.path.isfile(scene["first_frame_path"]):
            continue
        # Check if previous scene has a generated clip
        prev = scenes[i - 1]
        prev_clip = prev.get("clip_path")
        if not prev_clip or not os.path.isfile(prev_clip):
            continue
        # Extract last frame from previous clip
        kf_path = os.path.join(KEYFRAMES_DIR, f"scene_{i}_first.png")
        try:
            extract_last_frame(prev_clip, kf_path)
            scene["first_frame_path"] = kf_path
            chained_count += 1
            print(f"[AUTO-CHAIN] Scene {i}: chained first_frame from scene {i-1} clip")
        except Exception as e:
            print(f"[AUTO-CHAIN] Scene {i}: failed to extract last frame from scene {i-1}: {e}")

    print(f"[AUTO-CHAIN] Chained {chained_count} scenes")
    return plan


def _run_regen(scene_index: int, new_prompt: str):
    """Background thread to regenerate a single scene."""
    try:
        plan = _load_scene_plan()
        if not plan:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No scene plan found. Generate a video first."
                gen_state["running"] = False
            return

        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = f"Scene index {scene_index} out of range"
                gen_state["running"] = False
            return

        scene = scenes[scene_index]
        scene["prompt"] = new_prompt

        with gen_lock:
            gen_state["phase"] = "generating"
            gen_state["total_scenes"] = 1
            gen_state["progress"] = [
                {"scene": scene_index, "status": "regenerating...", "prompt": new_prompt}
            ]

        def on_progress(index, status):
            with gen_lock:
                if gen_state["progress"]:
                    gen_state["progress"][0]["status"] = status

        _enrich_scene_with_assets(scene)
        char_photo = scene.get("character_photo_path", "") or None
        clip_path = generate_scene(scene, scene_index, CLIPS_DIR, progress_cb=on_progress, cost_cb=_record_cost, photo_path=char_photo)
        # Face swap post-processing if enabled
        # clip_path = _maybe_face_swap(clip_path, char_photo)  # SHELVED
        scene["clip_path"] = clip_path
        plan["scenes"][scene_index] = scene

        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


def _run_restitch():
    """Background thread to re-stitch all scenes."""
    try:
        plan = _load_scene_plan()
        if not plan:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No scene plan found."
                gen_state["running"] = False
            return

        with gen_lock:
            gen_state["phase"] = "stitching"

        clip_paths = [s.get("clip_path") for s in plan["scenes"]]
        scene_transitions = [s.get("transition", "crossfade") for s in plan["scenes"]]
        song_path = plan.get("song_path")
        output_path = plan.get("output_path") or os.path.join(OUTPUT_DIR, "final_video.mp4")

        stitch(clip_paths, song_path, output_path, transitions=scene_transitions)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["output_file"] = output_path
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


def _run_generation_approved():
    """
    Background generation thread that only generates video clips for scenes
    that have been approved in the preview step (preview_approved == True).
    Scenes with no approval flag set are also generated (backward compat).
    Unapproved scenes keep any existing clip_path or None.
    """
    try:
        plan = _load_scene_plan()
        if not plan:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No scene plan found. Run planning first."
                gen_state["running"] = False
            return

        scenes = plan["scenes"]
        song_path = plan.get("song_path", "")
        output_path = plan.get("output_path", os.path.join(OUTPUT_DIR, "final_video.mp4"))

        # Determine which scenes to generate
        has_any_approval = any("preview_approved" in s for s in scenes)
        scenes_to_gen = []
        for i, s in enumerate(scenes):
            if has_any_approval:
                if s.get("preview_approved", False):
                    scenes_to_gen.append((i, s))
            else:
                scenes_to_gen.append((i, s))

        with gen_lock:
            gen_state["phase"] = "generating"
            gen_state["total_scenes"] = len(scenes_to_gen)
            gen_state["progress"] = [
                {"scene": orig_i, "status": "pending", "prompt": s.get("prompt", "")}
                for orig_i, s in scenes_to_gen
            ]

        clip_paths = [s.get("clip_path") for s in scenes]  # existing paths as fallback

        def on_progress(local_idx, status):
            with gen_lock:
                if local_idx < len(gen_state["progress"]):
                    gen_state["progress"][local_idx]["status"] = status

        for local_idx, (orig_i, scene) in enumerate(scenes_to_gen):
            with gen_lock:
                if gen_state.get("cancel_requested"):
                    on_progress(local_idx, "cancelled")
                    break

            # Merge preview notes into prompt if present
            notes = scene.get("preview_notes", "")
            prompt_base = scene.get("prompt", "")
            scene_copy = dict(scene)
            if notes and notes.strip():
                scene_copy["prompt"] = f"{prompt_base}. Director notes: {notes.strip()}"

            # Enrich scene with asset photos and descriptions
            _enrich_scene_with_assets(scene_copy)

            def _prog(idx_unused, status, li=local_idx):
                on_progress(li, status)

            on_progress(local_idx, "generating...")
            try:
                # Pass character photo explicitly so the engine uses it as reference
                char_photo = scene_copy.get("character_photo_path", "") or None
                clip_path = generate_scene(scene_copy, orig_i, CLIPS_DIR,
                                           progress_cb=_prog, cost_cb=_record_cost,
                                           photo_path=char_photo)
                # Face swap post-processing if enabled
                # clip_path = _maybe_face_swap(clip_path, char_photo)  # SHELVED
                clip_paths[orig_i] = clip_path
                on_progress(local_idx, "done")
                _record_prompt_history(scene_copy.get("prompt", ""), scene_index=orig_i)
            except Exception as e:
                on_progress(local_idx, f"FAILED: {e}")
                clip_paths[orig_i] = None

        # Update plan with new clip paths and save
        for i, cp in enumerate(clip_paths):
            plan["scenes"][i]["clip_path"] = cp

        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)

        valid = [c for c in clip_paths if c and os.path.isfile(c)]
        if not valid:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No clips were generated successfully"
                gen_state["running"] = False
            return

        # Stitch only valid (approved) clips
        stitch_clips = []
        stitch_trans = []
        for i, s in enumerate(plan["scenes"]):
            cp = clip_paths[i]
            if cp and os.path.isfile(cp):
                stitch_clips.append(cp)
                stitch_trans.append(s.get("transition", "crossfade"))

        with gen_lock:
            gen_state["phase"] = "stitching"

        stitch(stitch_clips, song_path, output_path, transitions=stitch_trans)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["output_file"] = output_path
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


# ---- Preview-first pipeline helpers ----

def _generate_scene_thumbnail(index: int, prompt: str, notes: str = "",
                               scene_data: dict = None) -> dict:
    """Generate a preview still for a scene via fal.ai Gemini 3.1 Flash.

    If character/costume/environment reference photos exist on the scene,
    uses gemini_edit_image so the preview respects identity. Otherwise uses
    gemini_generate_image for a text-only still.
    """
    import shutil as _shutil
    from lib.fal_client import gemini_generate_image, gemini_edit_image

    full_prompt = prompt.strip()
    if notes and notes.strip():
        full_prompt = f"{full_prompt}. {notes.strip()}"

    out_path = os.path.join(SCENE_THUMBNAILS_DIR, f"scene_{index}.jpg")
    os.makedirs(SCENE_THUMBNAILS_DIR, exist_ok=True)

    # Collect reference photos + enrich prompt with asset descriptions.
    ref_paths = []
    if scene_data:
        enriched = dict(scene_data)
        _enrich_scene_with_assets(enriched)

        char_p = enriched.get("character_photo_path", "") or ""
        if char_p and os.path.isfile(char_p):
            ref_paths.append(char_p)
        for cp in (enriched.get("character_photo_paths", []) or []):
            if cp and os.path.isfile(cp) and cp not in ref_paths:
                ref_paths.append(cp)

        cos_p = enriched.get("costume_photo_path", "") or ""
        if cos_p and os.path.isfile(cos_p):
            ref_paths.append(cos_p)
        for cp in (enriched.get("costume_photo_paths", []) or []):
            if cp and os.path.isfile(cp) and cp not in ref_paths:
                ref_paths.append(cp)

        env_p = enriched.get("environment_photo_path", "") or ""
        if env_p and os.path.isfile(env_p):
            ref_paths.append(env_p)

        # Fold asset descriptions into the prompt (no re-description of refs).
        env_desc = enriched.get("environment_description", "")
        if env_desc and env_desc.lower() not in full_prompt.lower():
            full_prompt = f"{full_prompt}. Setting: {env_desc}"
        cos_desc = enriched.get("costume_description", "")
        if cos_desc and cos_desc.lower() not in full_prompt.lower():
            full_prompt = f"{full_prompt}. Wearing: {cos_desc}"

        # Gemini edit path caps refs at 3 (per fal.ai guidance).
        ref_paths = ref_paths[:3]

    if len(full_prompt) > 800:
        full_prompt = full_prompt[:797] + "..."

    try:
        if ref_paths:
            print(f"[PREVIEW][{index}] fal.ai Gemini edit with {len(ref_paths)} refs")
            paths = gemini_edit_image(
                prompt=full_prompt,
                reference_image_paths=ref_paths,
                resolution="1K",
                num_images=1,
            )
        else:
            print(f"[PREVIEW][{index}] fal.ai Gemini text-to-image")
            paths = gemini_generate_image(
                prompt=full_prompt,
                resolution="1K",
                aspect_ratio="16:9",
                num_images=1,
            )

        if not paths or not os.path.isfile(paths[0]):
            return {"error": "fal.ai Gemini returned no image"}

        _shutil.move(paths[0], out_path)
        _record_cost(f"thumb_{index}", "image")
        return {"preview_url": f"/api/scene-thumbnails/scene_{index}.jpg"}
    except Exception as e:
        import sys as _sys, traceback as _tb
        _sys.stderr.write(f"[PREVIEW][{index}] Gemini preview failed: {e}\n")
        _tb.print_exc(file=_sys.stderr)
        _sys.stderr.flush()
        return {"error": f"Preview failed: {str(e)[:200]}"}


def _run_preview_batch(scenes_data: list):
    """
    Background thread: generate thumbnails for all scenes concurrently.
    scenes_data: list of {index, prompt, notes}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with preview_lock:
        preview_state["running"] = True
        preview_state["total"] = len(scenes_data)
        preview_state["completed"] = 0
        preview_state["failed"] = 0
        preview_state["results"] = {
            str(sd["index"]): {"status": "pending"} for sd in scenes_data
        }

    def _do_one(sd):
        idx = sd["index"]
        with preview_lock:
            preview_state["results"][str(idx)] = {"status": "generating"}
        result = _generate_scene_thumbnail(idx, sd.get("prompt", ""), sd.get("notes", ""),
                                           scene_data=sd.get("scene_data"))
        with preview_lock:
            if "error" in result:
                preview_state["results"][str(idx)] = {"status": "failed", "error": result["error"]}
                preview_state["failed"] += 1
            else:
                preview_state["results"][str(idx)] = {
                    "status": "done",
                    "preview_url": result["preview_url"],
                }
                preview_state["completed"] += 1
        # Persist to scene plan regardless of lock
        if "preview_url" in result:
            _update_scene_plan_thumbnail(idx, result["preview_url"])

    max_workers = min(4, len(scenes_data))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_do_one, sd) for sd in scenes_data]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

    with preview_lock:
        preview_state["running"] = False


def _update_scene_plan_thumbnail(index: int, preview_url: str):
    """Persist thumbnail URL into the scene_plan.json for the given scene index."""
    plan = _load_scene_plan()
    if plan and 0 <= index < len(plan["scenes"]):
        plan["scenes"][index]["preview_thumbnail"] = preview_url
        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)


def _update_scene_plan_approval(index: int, approved: bool, notes: str = ""):
    """Persist approval status into the scene_plan.json for the given scene index."""
    plan = _load_scene_plan()
    if plan and 0 <= index < len(plan["scenes"]):
        plan["scenes"][index]["preview_approved"] = approved
        if notes:
            plan["scenes"][index]["preview_notes"] = notes
        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)


# ---- Cost tracker helpers ----

def _load_cost_tracker() -> dict:
    """Load or create the cost tracker."""
    if os.path.isfile(COST_TRACKER_PATH):
        try:
            with open(COST_TRACKER_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "total_cost": 0.0,
        "video_generations": 0,
        "image_generations": 0,
        "budget": DEFAULT_BUDGET,
        "scene_costs": {},  # scene_id_or_index -> cost
        "generations": [],  # ledger: list of per-generation records
    }


def _save_cost_tracker(tracker: dict):
    with open(COST_TRACKER_PATH, "w") as f:
        json.dump(tracker, f, indent=2)


def _price_for(engine: str, tier: str, duration: float, gen_type: str) -> float:
    """Best-effort accurate pricing for a generation."""
    try:
        if gen_type == "video":
            per_sec = KLING_PRICE_PER_SEC.get(tier or "", None)
            if per_sec is not None and duration:
                return round(per_sec * float(duration), 4)
            return COST_PER_VIDEO_GEN
        # image
        return IMAGE_PRICE_BY_ENGINE.get(engine or "", COST_PER_IMAGE_GEN)
    except Exception:
        return COST_PER_VIDEO_GEN if gen_type == "video" else COST_PER_IMAGE_GEN


def _record_generation(
    shot_key: str,
    gen_type: str = "video",
    engine: str = "",
    tier: str = "",
    duration: float = 0,
    est_cost: float = 0,
    actual_cost: float = None,
    status: str = "ok",
    meta: dict = None,
):
    """Append a ledger entry + update running totals. Returns the tracker."""
    tracker = _load_cost_tracker()
    if actual_cost is None:
        actual_cost = _price_for(engine, tier, duration, gen_type)
    # Only bill delivered assets
    billable = actual_cost if status == "ok" else 0.0
    if gen_type == "video":
        if status == "ok":
            tracker["video_generations"] = tracker.get("video_generations", 0) + 1
    else:
        if status == "ok":
            tracker["image_generations"] = tracker.get("image_generations", 0) + 1
    tracker["total_cost"] = round(tracker.get("total_cost", 0) + billable, 4)
    tracker["scene_costs"][str(shot_key)] = round(
        tracker["scene_costs"].get(str(shot_key), 0) + billable, 4
    )
    entry = {
        "ts": time.time(),
        "shot_id": str(shot_key),
        "type": gen_type,
        "engine": engine,
        "tier": tier,
        "duration": duration,
        "est": round(est_cost or 0, 4),
        "actual": round(actual_cost, 4),
        "billed": round(billable, 4),
        "status": status,
    }
    if meta:
        entry["meta"] = meta
    tracker.setdefault("generations", []).append(entry)
    # Cap ledger length to last 5000 entries
    if len(tracker["generations"]) > 5000:
        tracker["generations"] = tracker["generations"][-5000:]
    _save_cost_tracker(tracker)
    return tracker


def _record_cost(scene_key: str, gen_type: str = "video"):
    """Legacy shim — use _record_generation for new code."""
    return _record_generation(scene_key, gen_type=gen_type, status="ok")


def _scene_duration_for_shot(shot_id: str) -> tuple[int | None, str]:
    """Look up planner-assigned duration for a shot in the active project.

    Returns (duration_s, source) where source is one of:
      "planner" — duration_source=="planner" in scenes.json
      "scene"   — scene has duration/duration_s but not planner-tagged
      "none"    — shot not found or no duration field present

    Returns (None, "none") if nothing resolvable.
    """
    if not shot_id:
        return None, "none"
    try:
        from lib.active_project import get_project_root
        scenes_path = os.path.join(get_project_root(), "prompt_os", "scenes.json")
        if not os.path.isfile(scenes_path):
            return None, "none"
        with open(scenes_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        scenes = raw.get("scenes", raw) if isinstance(raw, dict) else raw
        if not isinstance(scenes, list):
            return None, "none"
        for s in scenes:
            if s.get("id") == shot_id or s.get("opus_shot_id") == shot_id:
                dur = s.get("duration_s") or s.get("duration")
                if dur is None:
                    return None, "none"
                try:
                    dur_i = int(dur)
                except (TypeError, ValueError):
                    return None, "none"
                src = "planner" if s.get("duration_source") == "planner" else "scene"
                return dur_i, src
    except Exception:
        pass
    return None, "none"


def _check_budget_gate(est_cost: float) -> tuple:
    """Returns (ok, reason, tracker). ok=False when budget would be exceeded."""
    tracker = _load_cost_tracker()
    budget = float(tracker.get("budget", DEFAULT_BUDGET) or DEFAULT_BUDGET)
    spent = float(tracker.get("total_cost", 0))
    projected = spent + float(est_cost or 0)
    if budget > 0 and projected > budget:
        return False, f"Budget ${budget:.2f} would be exceeded (spent ${spent:.4f} + est ${est_cost:.4f}).", tracker
    return True, "", tracker


# ---- Settings helpers ----

def _load_settings() -> dict:
    """Load project settings from output/settings.json."""
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"default_engine": "gen4_5", "character_references": {}}


def _save_settings(settings: dict):
    """Save project settings to output/settings.json."""
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


# ---- Upscale helper ----

def _subprocess_kwargs() -> dict:
    """Extra kwargs for subprocess calls (hide window on Windows)."""
    kw = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    return kw


def _upscale_clip(clip_path: str) -> str:
    """Upscale a video clip 2x using ffmpeg lanczos scaling. Replaces the original."""
    if not os.path.isfile(clip_path):
        raise FileNotFoundError(f"Clip not found: {clip_path}")
    dirname = os.path.dirname(clip_path)
    basename = os.path.basename(clip_path)
    name, ext = os.path.splitext(basename)
    temp_path = os.path.join(dirname, f"{name}_upscaled{ext}")
    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-vf", "scale=iw*2:ih*2:flags=lanczos",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "copy",
        temp_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300, **_subprocess_kwargs())
    # Replace original with upscaled
    os.replace(temp_path, clip_path)
    return clip_path


# ---- Face swap post-processing ----

_face_swap_initialized = False

def _maybe_face_swap(clip_path: str, char_photo: str) -> str:
    """Apply face swap post-processing if enabled in settings and a character photo exists.

    Returns the (possibly swapped) clip path. If face swap fails or is disabled,
    returns the original clip_path unchanged.
    """
    global _face_swap_initialized
    if not clip_path or not os.path.isfile(clip_path):
        return clip_path
    if not char_photo or not os.path.isfile(char_photo):
        return clip_path

    settings = _load_settings()
    if not settings.get("face_swap_enabled", False):
        return clip_path

    try:
        from lib.face_swap import init as fs_init, swap_faces_in_video

        if not _face_swap_initialized:
            fs_init(OUTPUT_DIR, onnx_mode=settings.get("face_swap_onnx", False))
            _face_swap_initialized = True

        swapped_path = clip_path.replace(".mp4", "_swapped.mp4")
        print(f"[FaceSwap] Starting face swap: {clip_path} with {char_photo}")
        result = swap_faces_in_video(clip_path, char_photo, swapped_path)
        if result and os.path.isfile(result):
            print(f"[FaceSwap] Success: {result}")
            return result
        else:
            print("[FaceSwap] Face swap returned no result, keeping original clip")
            return clip_path
    except Exception as e:
        print(f"[FaceSwap] Error (keeping original): {e}")
        return clip_path


# ---- Manual scene plan helpers ----


def _load_manual_plan() -> dict:
    """Load or create the manual scene plan."""
    if os.path.isfile(MANUAL_PLAN_PATH):
        try:
            with open(MANUAL_PLAN_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {"scenes": [], "song_path": None}


def _save_manual_plan(plan: dict):
    with _plan_file_lock:
        with open(MANUAL_PLAN_PATH, "w") as f:
            json.dump(plan, f, indent=2)


def _run_manual_generate_scene(scene_id: str):
    """Background thread to generate a single manual scene."""
    try:
        plan = _load_manual_plan()
        scene = None
        scene_idx = None
        for i, s in enumerate(plan["scenes"]):
            if s["id"] == scene_id:
                scene = s
                scene_idx = i
                break
        if scene is None:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = f"Scene {scene_id} not found"
                gen_state["running"] = False
            return

        # Item 46: Track previous clip for comparison
        old_clip = scene.get("clip_path", "")
        if old_clip and os.path.isfile(old_clip):
            scene["previous_clip_path"] = old_clip
            # Feature 4: Save as take for A/B comparison
            _save_take(scene_id, old_clip, scene.get("prompt", ""))

        # If user uploaded a video, use it directly as the clip
        video_path = scene.get("video_path", "")
        if video_path and os.path.isfile(video_path):
            scene["clip_path"] = video_path
            scene["has_clip"] = True
            plan["scenes"][scene_idx] = scene
            _save_manual_plan(plan)
            with gen_lock:
                gen_state["phase"] = "done"
                gen_state["running"] = False
            return

        with gen_lock:
            gen_state["phase"] = "generating"
            gen_state["total_scenes"] = 1
            gen_state["progress"] = [
                {"scene": scene_idx, "status": "starting...", "prompt": scene["prompt"]}
            ]

        def on_progress(index, status):
            with gen_lock:
                if gen_state["progress"]:
                    gen_state["progress"][0]["status"] = status

        # ---- Structured Prompt Assembly ----
        from lib.prompt_assembler import assemble_prompt, build_character_block, build_costume_block, build_environment_block

        settings = _load_settings()
        ds = settings.get("director_state", {})
        universal_prompt = ds.get("universalPrompt", "")
        world_setting = ds.get("worldSetting", "")

        pos_chars = []
        pos_costume = None
        pos_env = None

        # Collect ALL characters
        char_ids = list(scene.get("characterIds", []))
        if scene.get("characterId") and scene["characterId"] not in char_ids:
            char_ids.append(scene["characterId"])
        for cid in char_ids:
            c = _prompt_os.get_character(cid)
            if c:
                pos_chars.append(c)
        pos_char = pos_chars[0] if pos_chars else None

        if scene.get("costumeId"):
            pos_costume = _prompt_os.get_costume(scene["costumeId"])

        if scene.get("environmentId"):
            pos_env = _prompt_os.get_environment(scene["environmentId"])

        # Use the structured assembler
        assembled = assemble_prompt(
            global_style=universal_prompt,
            world_setting=world_setting,
            character=pos_char,
            costume=pos_costume,
            environment=pos_env,
            scene=scene,
            universal_prompt=universal_prompt,
        )

        # The scene's own prompt is the user's action/creative input — combine with assembled blocks
        user_prompt = scene["prompt"]
        gen_prompt = f"{assembled['prompt']}. {user_prompt}" if assembled["prompt"] else user_prompt

        print(f"[GEN] Assembled prompt ({len(gen_prompt)} chars): {gen_prompt[:120]}...")
        if pos_char:
            print(f"[GEN] Character: {pos_char.get('name')}")
        if pos_costume:
            print(f"[GEN] Costume: {pos_costume.get('name')}")
        if pos_env:
            print(f"[GEN] Environment: {pos_env.get('name')}")

        # Build the prompt - handle multi-photo compositing
        scene_photo_path = None

        # Auto-attach character reference photo from Prompt OS
        if pos_char and pos_char.get("referencePhoto") and not scene.get("photo_path"):
            ref_photo = pos_char["referencePhoto"]
            # Resolve API URL to actual file path
            resolved_photo = None
            if os.path.isfile(ref_photo):
                resolved_photo = ref_photo
            else:
                import re as _re3
                m = _re3.search(r"/api/pos/characters/([^/]+)/photo", ref_photo)
                if m:
                    cid = m.group(1)
                    for ext in (".jpg", ".jpeg", ".png", ".webp"):
                        candidate = os.path.join(_pos_photos_dir("char"), f"{cid}{ext}")
                        if os.path.isfile(candidate):
                            resolved_photo = candidate
                            break
            if resolved_photo:
                scene_photo_path = resolved_photo
                print(f"[GEN] Auto-attached character reference photo: {resolved_photo}")

        # Check BOTH photo_path (single) and photo_paths (array)
        single_photo = scene.get("photo_path", "")
        photo_paths = scene.get("photo_paths", [])

        # Build valid photos list from both sources
        valid_photos = []
        if single_photo and os.path.isfile(single_photo):
            valid_photos.append(single_photo)
        for p in photo_paths:
            if p and os.path.isfile(p) and p not in valid_photos:
                valid_photos.append(p)

        print(f"[GEN] scene_id={scene_id}, prompt={gen_prompt[:80]}...")
        print(f"[GEN] photo_path={single_photo}, photo_paths={photo_paths}")
        print(f"[GEN] valid_photos={valid_photos}")

        if len(valid_photos) > 1:
            # Feature 5: Multiple photos - describe all and merge into prompt
            try:
                descriptions = []
                for pp in valid_photos:
                    desc = describe_photo(pp)
                    descriptions.append(desc)
                merged_desc = "Scene combining: " + " with ".join(descriptions)
                if gen_prompt:
                    gen_prompt = merged_desc + ", style: " + gen_prompt
                else:
                    gen_prompt = merged_desc
                # Use first photo for style transfer
                scene_photo_path = valid_photos[0]
                print(f"[_run_manual_generate_scene] Multi-photo: using first photo {scene_photo_path} for style transfer")
            except Exception as e:
                print(f"[_run_manual_generate_scene] Multi-photo describe failed: {e}")
                # Fall back to single photo behavior
                if scene.get("photo_path") and os.path.isfile(scene["photo_path"]):
                    scene_photo_path = scene["photo_path"]
        elif scene.get("photo_path") and os.path.isfile(scene["photo_path"]):
            scene_photo_path = scene["photo_path"]
            print(f"[_run_manual_generate_scene] Single photo found: {scene_photo_path}")

        # Read continuity_mode from plan settings
        plan_continuity = plan.get("continuity_mode", True)

        # Build a single consistent character description from stored fields
        # This gets passed to the video generator so it doesn't re-describe the photo each time
        char_description = ""
        if pos_char:
            desc_parts = []
            phys = pos_char.get("physicalDescription", pos_char.get("description", ""))
            if phys:
                desc_parts.append(phys)
            if pos_char.get("hair"):
                desc_parts.append(pos_char["hair"])
            if pos_char.get("skinTone"):
                desc_parts.append(f"{pos_char['skinTone']} skin")
            if pos_char.get("distinguishingFeatures"):
                desc_parts.append(pos_char["distinguishingFeatures"])
            if pos_char.get("outfitDescription"):
                desc_parts.append(f"wearing {pos_char['outfitDescription']}")
            char_description = ", ".join(desc_parts)

        # Build environment description from stored fields
        env_description = ""
        if pos_env:
            env_parts = []
            if pos_env.get("description"):
                env_parts.append(pos_env["description"])
            if pos_env.get("lighting"):
                env_parts.append(pos_env["lighting"])
            if pos_env.get("atmosphere"):
                env_parts.append(pos_env["atmosphere"])
            if pos_env.get("location"):
                env_parts.append(pos_env["location"])
            if pos_env.get("weather"):
                env_parts.append(pos_env["weather"])
            if pos_env.get("timeOfDay"):
                env_parts.append(pos_env["timeOfDay"])
            env_description = ", ".join(env_parts)

        # Build costume description from stored fields
        costume_description = ""
        if pos_costume:
            if pos_costume.get("description"):
                costume_description = pos_costume["description"]
            else:
                c_parts = []
                if pos_costume.get("upperBody"):
                    c_parts.append(pos_costume["upperBody"])
                if pos_costume.get("lowerBody"):
                    c_parts.append(pos_costume["lowerBody"])
                if pos_costume.get("footwear"):
                    c_parts.append(pos_costume["footwear"])
                if pos_costume.get("accessories"):
                    c_parts.append(pos_costume["accessories"])
                costume_description = ", ".join(c_parts)

        # Resolve costume reference photo to filesystem path
        costume_photo_path = None
        if pos_costume:
            ref_img = pos_costume.get("referenceImagePath", "")
            if ref_img:
                import re as _re_cos
                if os.path.isfile(ref_img):
                    costume_photo_path = ref_img
                else:
                    m_cos = _re_cos.search(r"/api/pos/costumes/([^/]+)/photo", ref_img)
                    if m_cos:
                        cid_cos = m_cos.group(1)
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            candidate = os.path.join(_pos_photos_dir("costume"), f"{cid_cos}{ext}")
                            if os.path.isfile(candidate):
                                costume_photo_path = candidate
                                break
                if costume_photo_path:
                    print(f"[GEN] Resolved costume reference photo: {costume_photo_path}")

        # Resolve environment reference photo to filesystem path
        environment_photo_path = None
        if pos_env:
            ref_img = pos_env.get("referenceImagePath", "")
            if ref_img:
                import re as _re_env
                if os.path.isfile(ref_img):
                    environment_photo_path = ref_img
                else:
                    m_env = _re_env.search(r"/api/pos/environments/([^/]+)/photo", ref_img)
                    if m_env:
                        eid_env = m_env.group(1)
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            candidate = os.path.join(_pos_photos_dir("env"), f"{eid_env}{ext}")
                            if os.path.isfile(candidate):
                                environment_photo_path = candidate
                                break
                if environment_photo_path:
                    print(f"[GEN] Resolved environment reference photo: {environment_photo_path}")

        gen_scene = {
            "prompt": gen_prompt,
            "duration": scene.get("duration", 5),
            "camera_movement": scene.get("camera_movement", "zoom_in"),
            "engine": scene.get("engine", ""),
            "id": scene.get("id", ""),
            "continuity_mode": plan_continuity,
            "character_description": char_description,
            "is_character_sheet": bool(pos_char and pos_char.get("isCharacterSheet")),
            "environment_description": env_description,
            "costume_description": costume_description,
            "costume_photo_path": costume_photo_path or "",
            "environment_photo_path": environment_photo_path or "",
        }
        # Keyframe support: pass first/last frame paths from scene to gen_scene
        if scene.get("first_frame_path"):
            gen_scene["first_frame_path"] = scene["first_frame_path"]
        if scene.get("last_frame_path"):
            gen_scene["last_frame_path"] = scene["last_frame_path"]

        # ── Frame chaining: use previous scene's last frame as this scene's first frame ──
        # This is the #1 continuity mechanism - gives the AI engine a visual starting point
        if scene_idx > 0 and not gen_scene.get("first_frame_path"):
            prev_scene = plan["scenes"][scene_idx - 1]
            # Check for extracted reference frame from previous generation
            prev_ref = prev_scene.get("reference_frame", "")
            if prev_ref and os.path.isfile(prev_ref):
                gen_scene["first_frame_path"] = prev_ref
                print(f"[CONTINUITY] Scene {scene_idx}: chained first_frame from scene {scene_idx - 1} last frame")
            else:
                # Try extracting from previous clip on-the-fly
                prev_clip = prev_scene.get("clip_path", "")
                if prev_clip and os.path.isfile(prev_clip):
                    try:
                        kf_path = os.path.join(KEYFRAMES_DIR, f"scene_{scene_idx}_chain.png")
                        extract_last_frame(prev_clip, kf_path)
                        gen_scene["first_frame_path"] = kf_path
                        print(f"[CONTINUITY] Scene {scene_idx}: extracted last frame from scene {scene_idx - 1} clip")
                    except Exception as ce:
                        print(f"[CONTINUITY] Scene {scene_idx}: frame extraction failed: {ce}")

        # ── Build continuity context from previous scene's actual data ──
        if scene_idx > 0:
            prev_scene = plan["scenes"][scene_idx - 1]
            ctx = {}
            # Character continuity
            prev_char_ids = prev_scene.get("characterIds", [])
            if prev_scene.get("characterId"):
                prev_char_ids = list(set(prev_char_ids + [prev_scene["characterId"]]))
            shared_chars = set(char_ids) & set(prev_char_ids)
            if shared_chars:
                ctx["has_character"] = True
                ctx["character_state"] = f"same character continues from previous scene"
            if char_description:
                ctx["character_description"] = char_description
            # Environment continuity
            if prev_scene.get("environmentId") and prev_scene["environmentId"] == scene.get("environmentId"):
                ctx["same_environment"] = True
            if env_description:
                ctx["environment_description"] = env_description
            # Style continuity from previous scene
            prev_grade = prev_scene.get("color_grade", "")
            if prev_grade:
                ctx["color_palette"] = prev_grade
            prev_lighting = prev_scene.get("lighting", "")
            if prev_lighting:
                ctx["lighting"] = prev_lighting
            # Pass the previous scene's prompt for keyword extraction
            ctx["previous_prompt"] = prev_scene.get("prompt", "")
            ctx["key_elements"] = []
            gen_scene["continuity_context"] = ctx

        print(f"[_run_manual_generate_scene] Calling generate_scene with photo_path={scene_photo_path}, engine={gen_scene['engine']}")
        try:
            clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                       progress_cb=on_progress, cost_cb=_record_cost,
                                       photo_path=scene_photo_path)
        except Exception as first_err:
            # Feature 9: Auto-retry once on failure
            print(f"[_run_manual_generate_scene] First attempt failed: {first_err}, retrying...")
            with gen_lock:
                if gen_state["progress"]:
                    gen_state["progress"][0]["status"] = f"retrying after: {str(first_err)[:40]}"
            try:
                clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                           progress_cb=on_progress, cost_cb=_record_cost,
                                           photo_path=scene_photo_path)
            except Exception as retry_err:
                raise RuntimeError(f"Failed after retry: {retry_err}") from retry_err

        # Face swap post-processing if enabled
        char_photo = scene.get("character_photo_path", "") or scene.get("photo_path", "") or None
        # clip_path = _maybe_face_swap(clip_path, char_photo)  # SHELVED

        scene["clip_path"] = clip_path
        scene["has_clip"] = True

        # Frame continuity: extract last frame for next shot's reference
        try:
            from lib.cinematic_engine import extract_last_frame
            last_frame = extract_last_frame(clip_path)
            if last_frame:
                scene["reference_frame"] = last_frame
                next_idx = scene_idx + 1
                if next_idx < len(plan["scenes"]):
                    plan["scenes"][next_idx]["reference_frame"] = last_frame
                print(f"[FRAME CONTINUITY] Extracted last frame: {last_frame}")
        except Exception as fe:
            print(f"[FRAME CONTINUITY] Frame extract failed: {fe}")

        plan["scenes"][scene_idx] = scene
        _save_manual_plan(plan)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


def _run_manual_generate_from_photo(scene_id: str):
    """Background thread to generate a video clip from a scene's photo + prompt."""
    try:
        print(f"[_run_manual_generate_from_photo] START scene_id={scene_id}")
        plan = _load_manual_plan()
        scene = None
        scene_idx = None
        for i, s in enumerate(plan["scenes"]):
            if s["id"] == scene_id:
                scene = s
                scene_idx = i
                break
        if scene is None:
            print(f"[_run_manual_generate_from_photo] ERROR: Scene {scene_id} not found in plan")
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = f"Scene {scene_id} not found"
                gen_state["running"] = False
            return

        photo_path = scene.get("photo_path", "")
        print(f"[_run_manual_generate_from_photo] photo_path={photo_path}, exists={os.path.isfile(photo_path) if photo_path else False}")
        print(f"[_run_manual_generate_from_photo] prompt={scene.get('prompt', '')[:80]}")
        if not photo_path or not os.path.isfile(photo_path):
            print(f"[_run_manual_generate_from_photo] ERROR: No photo file found at {photo_path}")
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "Scene has no photo uploaded"
                gen_state["running"] = False
            return

        with gen_lock:
            gen_state["phase"] = "generating"
            gen_state["total_scenes"] = 1
            gen_state["progress"] = [
                {"scene": scene_idx, "status": "starting photo-to-video...",
                 "prompt": scene.get("prompt", "")}
            ]

        def on_progress(status):
            with gen_lock:
                if gen_state["progress"]:
                    gen_state["progress"][0]["status"] = status

        clip_path = os.path.join(MANUAL_CLIPS_DIR, f"photo_clip_{scene_id}.mp4")
        prompt = scene.get("prompt", "cinematic scene")
        duration = scene.get("duration", 5)
        camera = scene.get("camera_movement", "zoom_in")

        edit_strength = scene.get("edit_strength", 0.3)
        generate_from_photo(photo_path, prompt, duration, clip_path,
                            camera=camera,
                            edit_strength=edit_strength,
                            progress_cb=on_progress)
        _record_cost(str(scene_id), "image")

        scene["clip_path"] = clip_path
        scene["has_clip"] = True
        plan["scenes"][scene_idx] = scene
        _save_manual_plan(plan)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


def _run_manual_generate_all():
    """Background thread to generate all manual scenes without clips."""
    try:
        plan = _load_manual_plan()
        scenes_to_gen = [(i, s) for i, s in enumerate(plan["scenes"])
                         if not s.get("has_clip") or not s.get("clip_path")
                         or not os.path.isfile(s.get("clip_path", ""))]

        if not scenes_to_gen:
            with gen_lock:
                gen_state["phase"] = "done"
                gen_state["running"] = False
            return

        with gen_lock:
            gen_state["phase"] = "generating"
            gen_state["total_scenes"] = len(scenes_to_gen)
            gen_state["progress"] = [
                {"scene": i, "status": "pending", "prompt": s["prompt"]}
                for i, s in scenes_to_gen
            ]

        for prog_idx, (scene_idx, scene) in enumerate(scenes_to_gen):
            def on_progress(index, status, _pi=prog_idx):
                with gen_lock:
                    if _pi < len(gen_state["progress"]):
                        gen_state["progress"][_pi]["status"] = status

            # If user uploaded a video, use it directly
            video_path = scene.get("video_path", "")
            if video_path and os.path.isfile(video_path):
                scene["clip_path"] = video_path
                scene["has_clip"] = True
                on_progress(scene_idx, "using uploaded video")
                plan["scenes"][scene_idx] = scene
                _save_manual_plan(plan)
                continue

            # Build prompt with multi-photo compositing
            gen_prompt = scene["prompt"]
            scene_photo_path = None
            photo_paths = scene.get("photo_paths", [])
            valid_photos = [p for p in photo_paths if p and os.path.isfile(p)]

            print(f"[_run_manual_generate_all] scene {scene_idx}: prompt={gen_prompt[:60]}..., photo_path={scene.get('photo_path')}")

            if len(valid_photos) > 1:
                try:
                    descriptions = []
                    for pp in valid_photos:
                        desc = describe_photo(pp)
                        descriptions.append(desc)
                    merged_desc = "Scene combining: " + " with ".join(descriptions)
                    gen_prompt = merged_desc + (", style: " + gen_prompt if gen_prompt else "")
                    scene_photo_path = valid_photos[0]
                except Exception:
                    if scene.get("photo_path") and os.path.isfile(scene["photo_path"]):
                        scene_photo_path = scene["photo_path"]
            elif scene.get("photo_path") and os.path.isfile(scene["photo_path"]):
                scene_photo_path = scene["photo_path"]

            plan_continuity = plan.get("continuity_mode", True)

            # Resolve Prompt OS entity descriptions and reference photos
            _all_char_desc = ""
            _all_env_desc = ""
            _all_cos_desc = ""
            _all_cos_photo = ""
            _all_env_photo = ""

            if scene.get("characterId"):
                _pc = _prompt_os.get_character(scene["characterId"])
                if _pc:
                    _dp = []
                    if _pc.get("physicalDescription"): _dp.append(_pc["physicalDescription"])
                    if _pc.get("hair"): _dp.append(_pc["hair"])
                    if _pc.get("skinTone"): _dp.append(f"{_pc['skinTone']} skin")
                    if _pc.get("distinguishingFeatures"): _dp.append(_pc["distinguishingFeatures"])
                    _all_char_desc = ", ".join(_dp)
                    if _pc.get("referencePhoto") and not scene_photo_path:
                        import re as _re_ca2
                        _ref = _pc["referencePhoto"]
                        if os.path.isfile(_ref):
                            scene_photo_path = _ref
                        else:
                            _mc = _re_ca2.search(r"/api/pos/characters/([^/]+)/photo", _ref)
                            if _mc:
                                for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    _cand = os.path.join(_pos_photos_dir("char"), f"{_mc.group(1)}{_ext}")
                                    if os.path.isfile(_cand):
                                        scene_photo_path = _cand
                                        break

            if scene.get("costumeId"):
                _pcos = _prompt_os.get_costume(scene["costumeId"])
                if _pcos:
                    _all_cos_desc = _pcos.get("description", "")
                    if not _all_cos_desc:
                        _cp = [_pcos.get("upperBody", ""), _pcos.get("lowerBody", "")]
                        _all_cos_desc = ", ".join(p for p in _cp if p)
                    import re as _re_co3
                    _cref = _pcos.get("referenceImagePath", "")
                    if _cref:
                        if os.path.isfile(_cref):
                            _all_cos_photo = _cref
                        else:
                            _mco3 = _re_co3.search(r"/api/pos/costumes/([^/]+)/photo", _cref)
                            if _mco3:
                                for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    _cand = os.path.join(_pos_photos_dir("costume"), f"{_mco3.group(1)}{_ext}")
                                    if os.path.isfile(_cand):
                                        _all_cos_photo = _cand
                                        break

            if scene.get("environmentId"):
                _pe = _prompt_os.get_environment(scene["environmentId"])
                if _pe:
                    _ep = []
                    if _pe.get("description"): _ep.append(_pe["description"])
                    if _pe.get("lighting"): _ep.append(_pe["lighting"])
                    if _pe.get("atmosphere"): _ep.append(_pe["atmosphere"])
                    _all_env_desc = ", ".join(_ep)
                    import re as _re_en3
                    _eref = _pe.get("referenceImagePath", "")
                    if _eref:
                        if os.path.isfile(_eref):
                            _all_env_photo = _eref
                        else:
                            _men3 = _re_en3.search(r"/api/pos/environments/([^/]+)/photo", _eref)
                            if _men3:
                                for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    _cand = os.path.join(_pos_photos_dir("env"), f"{_men3.group(1)}{_ext}")
                                    if os.path.isfile(_cand):
                                        _all_env_photo = _cand
                                        break

            gen_scene = {
                "prompt": gen_prompt,
                "duration": scene.get("duration", 5),
                "camera_movement": scene.get("camera_movement", "zoom_in"),
                "engine": scene.get("engine", ""),
                "id": scene.get("id", ""),
                "continuity_mode": plan_continuity,
                "character_description": _all_char_desc,
                "environment_description": _all_env_desc,
                "costume_description": _all_cos_desc,
                "costume_photo_path": _all_cos_photo,
                "environment_photo_path": _all_env_photo,
            }
            # Keyframe passthrough
            if scene.get("first_frame_path"):
                gen_scene["first_frame_path"] = scene["first_frame_path"]
            if scene.get("last_frame_path"):
                gen_scene["last_frame_path"] = scene["last_frame_path"]

            # ── Frame chaining from previous scene ──
            if scene_idx > 0 and not gen_scene.get("first_frame_path"):
                prev_s = plan["scenes"][scene_idx - 1]
                prev_ref = prev_s.get("reference_frame", "")
                if prev_ref and os.path.isfile(prev_ref):
                    gen_scene["first_frame_path"] = prev_ref
                    print(f"[GEN-ALL CHAIN] Scene {scene_idx}: using scene {scene_idx - 1} last frame")
                else:
                    prev_clip = prev_s.get("clip_path", "")
                    if prev_clip and os.path.isfile(prev_clip):
                        try:
                            kf = os.path.join(KEYFRAMES_DIR, f"scene_{scene_idx}_chain.png")
                            extract_last_frame(prev_clip, kf)
                            gen_scene["first_frame_path"] = kf
                            print(f"[GEN-ALL CHAIN] Scene {scene_idx}: extracted from scene {scene_idx - 1} clip")
                        except Exception:
                            pass

            # ── Continuity context from previous scene ──
            if scene_idx > 0:
                prev_s = plan["scenes"][scene_idx - 1]
                ctx = {}
                prev_cid = prev_s.get("characterId", "")
                cur_cid = scene.get("characterId", "")
                if prev_cid and prev_cid == cur_cid:
                    ctx["has_character"] = True
                if _all_char_desc:
                    ctx["character_description"] = _all_char_desc
                if prev_s.get("environmentId") and prev_s["environmentId"] == scene.get("environmentId"):
                    ctx["same_environment"] = True
                if _all_env_desc:
                    ctx["environment_description"] = _all_env_desc
                prev_grade = prev_s.get("color_grade", "")
                if prev_grade:
                    ctx["color_palette"] = prev_grade
                ctx["previous_prompt"] = prev_s.get("prompt", "")
                gen_scene["continuity_context"] = ctx

            try:
                clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                           progress_cb=on_progress, cost_cb=_record_cost,
                                           photo_path=scene_photo_path)
                scene["clip_path"] = clip_path
                scene["has_clip"] = True
                # Extract last frame for next scene's chain
                try:
                    ref_path = os.path.join(KEYFRAMES_DIR, f"scene_{scene_idx}_last.png")
                    extract_last_frame(clip_path, ref_path)
                    scene["reference_frame"] = ref_path
                except Exception:
                    pass
            except Exception as e:
                on_progress(scene_idx, f"FAILED: {e}")
                scene["has_clip"] = False

            plan["scenes"][scene_idx] = scene
            _save_manual_plan(plan)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


def _run_batch_generate_queue():
    """Background thread: generate scenes concurrently (2 at a time), fault-tolerant."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        plan = _load_manual_plan()
        scenes_to_gen = []
        for i, s in enumerate(plan["scenes"]):
            if not s.get("has_clip") or not s.get("clip_path") \
               or not os.path.isfile(s.get("clip_path", "")):
                scenes_to_gen.append((i, s))

        if not scenes_to_gen:
            with batch_lock:
                batch_queue_state["active"] = False
            return

        with batch_lock:
            batch_queue_state["active"] = True
            batch_queue_state["cancelled"] = False
            batch_queue_state["total"] = len(scenes_to_gen)
            batch_queue_state["completed"] = 0
            batch_queue_state["failed"] = 0
            batch_queue_state["start_time"] = time.time()
            batch_queue_state["scenes"] = [
                {"id": s["id"], "index": i, "status": "queued",
                 "elapsed": 0, "error": None,
                 "prompt": (s.get("prompt", ""))[:80]}
                for i, s in scenes_to_gen
            ]

        def generate_one(idx_scene_tuple):
            scene_idx, scene = idx_scene_tuple
            scene_id = scene["id"]
            # Check cancellation
            with batch_lock:
                if batch_queue_state["cancelled"]:
                    return scene_id, False, "cancelled"
                # Update status
                for sq in batch_queue_state["scenes"]:
                    if sq["id"] == scene_id:
                        sq["status"] = "rendering"
                        sq["elapsed"] = 0
                        break

            start = time.time()

            # Skip if user uploaded a video
            video_path = scene.get("video_path", "")
            if video_path and os.path.isfile(video_path):
                scene["clip_path"] = video_path
                scene["has_clip"] = True
                return scene_id, True, None

            gen_prompt = scene["prompt"]
            scene_photo_path = None

            # Prompt OS entity injection for batch queue
            if scene.get("characterId"):
                pc = _prompt_os.get_character(scene["characterId"])
                if pc:
                    cdp = []
                    if pc.get("physicalDescription"): cdp.append(pc["physicalDescription"])
                    if pc.get("hair"): cdp.append(f"with {pc['hair']}")
                    if pc.get("distinguishingFeatures"): cdp.append(pc["distinguishingFeatures"])
                    if cdp:
                        ci = ", ".join(cdp)
                        if ci.lower() not in gen_prompt.lower():
                            gen_prompt = ci + ", " + gen_prompt
                    if pc.get("referencePhoto") and os.path.isfile(pc["referencePhoto"]) and not scene.get("photo_path"):
                        scene_photo_path = pc["referencePhoto"]

            batch_cos_desc = ""
            batch_cos_photo = ""
            batch_env_desc = ""
            batch_env_photo = ""

            if scene.get("costumeId"):
                pcos = _prompt_os.get_costume(scene["costumeId"])
                if pcos:
                    cd = pcos.get("description", "")
                    if not cd:
                        pts = [pcos.get("upperBody",""), pcos.get("lowerBody","")]
                        cd = ", ".join(p for p in pts if p)
                    if cd and cd.lower() not in gen_prompt.lower():
                        gen_prompt += f", wearing {cd}"
                    batch_cos_desc = cd
                    import re as _re_bco
                    _cref = pcos.get("referenceImagePath", "")
                    if _cref:
                        if os.path.isfile(_cref):
                            batch_cos_photo = _cref
                        else:
                            _mbc = _re_bco.search(r"/api/pos/costumes/([^/]+)/photo", _cref)
                            if _mbc:
                                for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    _cand = os.path.join(_pos_photos_dir("costume"), f"{_mbc.group(1)}{_ext}")
                                    if os.path.isfile(_cand):
                                        batch_cos_photo = _cand
                                        break

            if scene.get("environmentId"):
                pe = _prompt_os.get_environment(scene["environmentId"])
                if pe:
                    ep = []
                    if pe.get("description"): ep.append(pe["description"])
                    if pe.get("lighting"): ep.append(pe["lighting"])
                    if pe.get("atmosphere"): ep.append(pe["atmosphere"])
                    if ep:
                        ei = ", ".join(ep)
                        if ei.lower() not in gen_prompt.lower():
                            gen_prompt += f", in {ei}"
                    batch_env_desc = ", ".join(ep)
                    import re as _re_ben
                    _eref = pe.get("referenceImagePath", "")
                    if _eref:
                        if os.path.isfile(_eref):
                            batch_env_photo = _eref
                        else:
                            _mbe = _re_ben.search(r"/api/pos/environments/([^/]+)/photo", _eref)
                            if _mbe:
                                for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    _cand = os.path.join(_pos_photos_dir("env"), f"{_mbe.group(1)}{_ext}")
                                    if os.path.isfile(_cand):
                                        batch_env_photo = _cand
                                        break

            single_photo = scene.get("photo_path", "")
            if single_photo and os.path.isfile(single_photo):
                scene_photo_path = single_photo

            plan_continuity = plan.get("continuity_mode", True)
            gen_scene = {
                "prompt": gen_prompt,
                "duration": scene.get("duration", 5),
                "camera_movement": scene.get("camera_movement", "zoom_in"),
                "engine": scene.get("engine", ""),
                "id": scene.get("id", ""),
                "continuity_mode": plan_continuity,
                "costume_description": batch_cos_desc,
                "costume_photo_path": batch_cos_photo,
                "environment_description": batch_env_desc,
                "environment_photo_path": batch_env_photo,
            }
            # Keyframe passthrough
            if scene.get("first_frame_path"):
                gen_scene["first_frame_path"] = scene["first_frame_path"]
            if scene.get("last_frame_path"):
                gen_scene["last_frame_path"] = scene["last_frame_path"]

            def on_progress(index, status):
                with batch_lock:
                    for sq in batch_queue_state["scenes"]:
                        if sq["id"] == scene_id:
                            sq["status"] = f"rendering: {status}"
                            sq["elapsed"] = round(time.time() - start, 1)
                            break

            try:
                clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                           progress_cb=on_progress,
                                           cost_cb=_record_cost,
                                           photo_path=scene_photo_path)
                scene["clip_path"] = clip_path
                scene["has_clip"] = True
                return scene_id, True, None
            except Exception as e:
                # Auto-retry once (Feature 9)
                try:
                    on_progress(scene_idx, f"retry after: {str(e)[:40]}")
                    clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                               progress_cb=on_progress,
                                               cost_cb=_record_cost,
                                               photo_path=scene_photo_path)
                    scene["clip_path"] = clip_path
                    scene["has_clip"] = True
                    return scene_id, True, None
                except Exception as e2:
                    return scene_id, False, str(e2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_scene = {}
            for item in scenes_to_gen:
                with batch_lock:
                    if batch_queue_state["cancelled"]:
                        break
                future = executor.submit(generate_one, item)
                future_to_scene[future] = item

            for future in as_completed(future_to_scene):
                scene_id, success, error = future.result()
                with batch_lock:
                    for sq in batch_queue_state["scenes"]:
                        if sq["id"] == scene_id:
                            if success:
                                sq["status"] = "done"
                                batch_queue_state["completed"] += 1
                            else:
                                sq["status"] = f"failed: {error or 'unknown'}"
                                sq["error"] = error
                                batch_queue_state["failed"] += 1
                            sq["elapsed"] = round(time.time() - batch_queue_state["start_time"], 1)
                            break

                # Save plan after each scene
                plan_now = _load_manual_plan()
                for i, s in enumerate(plan_now["scenes"]):
                    scene_idx, scene_data = next(
                        ((idx, sd) for idx, sd in scenes_to_gen if sd["id"] == s["id"]),
                        (None, None)
                    )
                    if scene_data and scene_data.get("clip_path"):
                        s["clip_path"] = scene_data["clip_path"]
                        s["has_clip"] = scene_data.get("has_clip", False)
                        plan_now["scenes"][i] = s
                _save_manual_plan(plan_now)

        with batch_lock:
            batch_queue_state["active"] = False

    except Exception as e:
        with batch_lock:
            batch_queue_state["active"] = False
            for sq in batch_queue_state["scenes"]:
                if sq["status"] in ("queued", "rendering"):
                    sq["status"] = f"failed: {str(e)}"
                    sq["error"] = str(e)


def _save_take(scene_id: str, clip_path: str, prompt: str):
    """Save a clip as a take for A/B comparison (Feature 4)."""
    if not clip_path or not os.path.isfile(clip_path):
        return
    scene_takes_dir = os.path.join(TAKES_DIR, scene_id)
    os.makedirs(scene_takes_dir, exist_ok=True)
    # Count existing takes
    existing = [f for f in os.listdir(scene_takes_dir) if f.startswith("take_")]
    take_num = len(existing) + 1
    ext = os.path.splitext(clip_path)[1] or ".mp4"
    take_filename = f"take_{take_num}{ext}"
    take_path = os.path.join(scene_takes_dir, take_filename)
    import shutil
    shutil.copy2(clip_path, take_path)
    # Save take metadata
    meta_path = os.path.join(scene_takes_dir, "takes.json")
    takes = []
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r") as f:
                takes = json.load(f)
        except (json.JSONDecodeError, IOError):
            takes = []
    takes.append({
        "take_num": take_num,
        "clip_path": take_path,
        "prompt": prompt,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    with open(meta_path, "w") as f:
        json.dump(takes, f, indent=2)


def _get_takes(scene_id: str) -> list:
    """Get all takes for a scene (Feature 4)."""
    scene_takes_dir = os.path.join(TAKES_DIR, scene_id)
    meta_path = os.path.join(scene_takes_dir, "takes.json")
    if not os.path.isfile(meta_path):
        return []
    try:
        with open(meta_path, "r") as f:
            takes = json.load(f)
        # Add clip_url and verify existence
        result = []
        for t in takes:
            cp = t.get("clip_path", "")
            if cp and os.path.isfile(cp):
                t["clip_url"] = f"/api/clips/{os.path.basename(cp)}"
                t["exists"] = True
            else:
                t["clip_url"] = None
                t["exists"] = False
            result.append(t)
        return result
    except (json.JSONDecodeError, IOError):
        return []


def _generate_waveform(audio_path: str) -> list:
    """Generate waveform data from audio file (Feature 3)."""
    if not audio_path or not os.path.isfile(audio_path):
        return []
    # Check cache
    if os.path.isfile(WAVEFORM_CACHE_PATH):
        try:
            with open(WAVEFORM_CACHE_PATH, "r") as f:
                cache = json.load(f)
            if cache.get("source") == audio_path:
                return cache.get("data", [])
        except (json.JSONDecodeError, IOError):
            pass
    # Generate via ffmpeg: extract raw audio, downsample to ~500 points
    try:
        cmd = [
            "ffmpeg", "-i", audio_path,
            "-ac", "1", "-ar", "500", "-f", "s16le",
            "-acodec", "pcm_s16le", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30,
                                **_subprocess_kwargs())
        if result.returncode != 0:
            print(f"[WAVEFORM] ffmpeg error: {result.stderr[:200]}")
            return []
        raw = result.stdout
        import struct
        n_samples = len(raw) // 2
        if n_samples == 0:
            return []
        samples = struct.unpack(f"<{n_samples}h", raw[:n_samples * 2])
        # Normalize to 0..1 range
        max_val = max(abs(s) for s in samples) or 1
        data = [round(abs(s) / max_val, 3) for s in samples]
        # Downsample to ~500 points
        target = 500
        if len(data) > target:
            step = len(data) / target
            data = [data[int(i * step)] for i in range(target)]
        # Cache result
        cache = {"source": audio_path, "data": data}
        with open(WAVEFORM_CACHE_PATH, "w") as f:
            json.dump(cache, f)
        return data
    except Exception as e:
        print(f"[WAVEFORM] Error: {e}")
        return []


def _auto_resize_photo(photo_path: str, max_w: int = 1280, max_h: int = 720) -> str:
    """Auto-resize a photo to max dimensions (Feature 6). Returns the path."""
    try:
        from PIL import Image
        with Image.open(photo_path) as img:
            orig_w, orig_h = img.size
            if orig_w <= max_w and orig_h <= max_h:
                return photo_path  # Already small enough
            # Determine orientation
            if orig_w > orig_h:
                target = (max_w, max_h)
            else:
                target = (max_h, max_w)
            img.thumbnail(target, Image.LANCZOS)
            img.save(photo_path, quality=90)
            new_w, new_h = img.size
            print(f"[RESIZE] Resized {orig_w}x{orig_h} -> {new_w}x{new_h}")
    except ImportError:
        print("[RESIZE] PIL not available, skipping resize")
    except Exception as e:
        print(f"[RESIZE] Error: {e}")
    return photo_path


def _run_manual_stitch():
    """Background thread to stitch manual scenes."""
    try:
        plan = _load_manual_plan()
        clip_paths = [s.get("clip_path") for s in plan["scenes"]]
        transitions = [s.get("transition", "crossfade") for s in plan["scenes"]]
        song_path = plan.get("song_path")

        # Validate we have clips
        valid = [c for c in clip_paths if c and os.path.isfile(c)]
        if not valid:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No clips available to stitch"
                gen_state["running"] = False
            return

        with gen_lock:
            gen_state["phase"] = "stitching"

        output_path = os.path.join(OUTPUT_DIR, "manual_final_video.mp4")
        # song_path can be None if user didn't upload audio
        audio = song_path if song_path and os.path.isfile(song_path) else None

        # Gather new stitch parameters
        speeds = [s.get("speed", 1.0) for s in plan["scenes"]]
        text_overlays = [s.get("overlay") for s in plan["scenes"]]
        scene_color_grades = [s.get("color_grade") for s in plan["scenes"]]
        global_color_grade = plan.get("color_grade", "none")
        audio_viz = plan.get("audio_viz")
        speed_ramps = [s.get("speed_ramp", "none") for s in plan["scenes"]]
        reversed_clips = [s.get("reversed", False) for s in plan["scenes"]]
        audio_crossfade_dur = plan.get("audio_crossfade", 0.0)

        # Apply per-scene effects before stitching
        processed_clip_paths = list(clip_paths)
        for idx, s in enumerate(plan["scenes"]):
            effect_name = s.get("effect", "none")
            if effect_name and effect_name != "none" and idx < len(processed_clip_paths):
                cp = processed_clip_paths[idx]
                if cp and os.path.isfile(cp):
                    intensity = s.get("effect_intensity", 0.5)
                    effect_out = os.path.join(MANUAL_CLIPS_DIR, f"_fx_{s.get('id', idx)}_{effect_name}.mp4")
                    try:
                        apply_effect(cp, effect_out, effect_name, intensity=intensity)
                        processed_clip_paths[idx] = effect_out
                    except Exception as e:
                        print(f"[STITCH] Effect {effect_name} failed for scene {idx}: {e}")

        stitch(processed_clip_paths, audio, output_path,
               transitions=transitions,
               speeds=speeds,
               text_overlays=text_overlays,
               color_grade=global_color_grade,
               scene_color_grades=scene_color_grades,
               audio_viz=audio_viz,
               speed_ramps=speed_ramps,
               reversed_clips=reversed_clips,
               audio_crossfade=audio_crossfade_dur)

        # Apply per-scene vocal overlays if any exist
        vocal_entries = []
        running_time = 0.0
        for s in plan["scenes"]:
            dur = s.get("duration", 5)
            vp = s.get("vocal_path", "")
            if vp and os.path.isfile(vp):
                vocal_entries.append({
                    "vocal_path": vp,
                    "start_sec": running_time,
                    "end_sec": running_time + dur,
                    "volume": s.get("vocal_volume", 80),
                })
            running_time += dur

        if vocal_entries and os.path.isfile(output_path):
            temp_vocal_out = output_path + ".vocal_tmp.mp4"
            try:
                overlay_scene_vocals(output_path, temp_vocal_out, vocal_entries)
                os.replace(temp_vocal_out, output_path)
            except Exception:
                if os.path.isfile(temp_vocal_out):
                    os.remove(temp_vocal_out)

        # Item 20: Auto-duck audio when vocals exist
        auto_duck = plan.get("auto_duck", False)
        if auto_duck and vocal_entries and os.path.isfile(output_path):
            duck_level = plan.get("duck_level", 0.3)
            duck_segments = []
            for ve in vocal_entries:
                duck_segments.append({
                    "start_sec": ve["start_sec"],
                    "end_sec": ve["end_sec"],
                })
            if duck_segments:
                temp_duck_out = output_path + ".duck_tmp.mp4"
                try:
                    apply_audio_ducking(output_path, temp_duck_out, duck_segments, duck_level)
                    os.replace(temp_duck_out, output_path)
                except Exception:
                    if os.path.isfile(temp_duck_out):
                        os.remove(temp_duck_out)

        plan["output_path"] = output_path
        _save_manual_plan(plan)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["output_file"] = output_path
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


# ---- Preview helpers ----

def _run_preview_all():
    """Background thread to generate a low-res preview of the entire video."""
    try:
        plan = _load_manual_plan()
        clip_paths = [s.get("clip_path") for s in plan["scenes"]]
        transitions = [s.get("transition", "crossfade") for s in plan["scenes"]]
        song_path = plan.get("song_path")

        valid = [c for c in clip_paths if c and os.path.isfile(c)]
        if not valid:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No clips available for preview"
                gen_state["running"] = False
            return

        with gen_lock:
            gen_state["phase"] = "stitching"

        preview_path = os.path.join(PREVIEWS_DIR, "preview_all.mp4")
        audio = song_path if song_path and os.path.isfile(song_path) else None

        # Scale down all clips to 480p first, then stitch
        scaled_clips = []
        for i, cp in enumerate(clip_paths):
            if not cp or not os.path.isfile(cp):
                scaled_clips.append(cp)
                continue
            scaled_path = os.path.join(PREVIEWS_DIR, f"_preview_scaled_{i}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-i", cp,
                "-vf", "scale=-2:480",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-an",
                scaled_path,
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=300, **_subprocess_kwargs())
            scaled_clips.append(scaled_path)

        speeds = [s.get("speed", 1.0) for s in plan["scenes"]]
        stitch(scaled_clips, audio, preview_path,
               transitions=transitions, speeds=speeds)

        # Clean up scaled clips
        for i, cp in enumerate(scaled_clips):
            tmp = os.path.join(PREVIEWS_DIR, f"_preview_scaled_{i}.mp4")
            if os.path.isfile(tmp):
                os.remove(tmp)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["output_file"] = preview_path
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


# ---- Lyrics helpers ----

def _run_lyrics_overlay(lyrics_data: list, target: str = "auto"):
    """Background thread to apply lyrics overlay to the final video."""
    try:
        # Determine which final video to apply to
        if target == "manual":
            plan = _load_manual_plan()
            video_path = plan.get("output_path", os.path.join(OUTPUT_DIR, "manual_final_video.mp4"))
        else:
            plan = _load_scene_plan()
            if not plan:
                with gen_lock:
                    gen_state["phase"] = "error"
                    gen_state["error"] = "No scene plan found"
                    gen_state["running"] = False
                return
            video_path = plan.get("output_path", os.path.join(OUTPUT_DIR, "final_video.mp4"))

        if not os.path.isfile(video_path):
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "Final video not found. Stitch first."
                gen_state["running"] = False
            return

        with gen_lock:
            gen_state["phase"] = "stitching"

        # Apply overlay to a temp file then replace
        temp_out = video_path + ".lyrics_tmp.mp4"
        apply_lyrics_overlay(video_path, temp_out, lyrics_data)

        # Replace original
        os.replace(temp_out, video_path)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["output_file"] = video_path
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


# ---- Batch export helpers ----

def _run_batch_export():
    """Background thread to export video in all 4 aspect ratios."""
    try:
        # Find the final video (auto or manual)
        auto_path = os.path.join(OUTPUT_DIR, "final_video.mp4")
        manual_path = os.path.join(OUTPUT_DIR, "manual_final_video.mp4")
        source = None
        if os.path.isfile(auto_path):
            source = auto_path
        elif os.path.isfile(manual_path):
            source = manual_path

        if not source:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No final video found. Generate and stitch first."
                gen_state["running"] = False
            return

        with gen_lock:
            gen_state["phase"] = "generating"
            gen_state["total_scenes"] = len(ASPECT_PRESETS)
            gen_state["progress"] = [
                {"scene": i, "status": "pending", "prompt": f"Exporting {ar}"}
                for i, ar in enumerate(ASPECT_PRESETS.keys())
            ]

        results = {}
        for i, (ar, (w, h)) in enumerate(ASPECT_PRESETS.items()):
            with gen_lock:
                if i < len(gen_state["progress"]):
                    gen_state["progress"][i]["status"] = f"exporting {ar}..."
            safe_name = ar.replace(":", "x")
            out_path = os.path.join(EXPORTS_DIR, f"final_{safe_name}.mp4")
            apply_aspect_ratio(source, out_path, ar)
            results[ar] = out_path
            with gen_lock:
                if i < len(gen_state["progress"]):
                    gen_state["progress"][i]["status"] = "done"

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["output_file"] = source
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


# ---- HTTP handler ----

class Handler(BaseHTTPRequestHandler):

    def _get_cors_origin(self):
        origin = self.headers.get("Origin", "")
        # Allow same-origin (localhost) requests
        allowed = ["http://localhost:3849", "http://127.0.0.1:3849", f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"]
        if origin in allowed:
            return origin
        return f"http://localhost:{PORT}"

    def log_message(self, fmt, *args):
        # Quieter logging
        sys.stderr.write(f"[server] {fmt % args}\n")

    def _parse_cookie(self, name: str) -> str:
        """Read a single cookie value from the Cookie header."""
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return ""

    def _real_client_ip(self) -> str:
        """Best-effort real client IP for rate limiting.

        Prefers CF-Connecting-IP (set by Cloudflare edge, single value, not
        client-spoofable when tunnel terminates here), then X-Forwarded-For's
        first hop, then the raw socket peer. Trusting proxy headers is gated
        on LUMN_TRUST_XFF=1 so a direct-exposed dev server can't be spoofed.
        """
        if os.environ.get("LUMN_TRUST_XFF") == "1":
            cf = (self.headers.get("CF-Connecting-IP", "") or "").strip()
            if cf:
                return cf
            xff = self.headers.get("X-Forwarded-For", "") or ""
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _current_user(self) -> dict | None:
        """Resolve the authenticated user from session cookie (DB-backed) or
        fall back to a synthetic 'local' user when the legacy bearer token is
        used. Returns None when no user is resolvable.

        The local fallback keeps dev workflows (e2e_walk, multi_shot_test)
        working without signup — they get user_id=0 = LOCAL_USER.
        """
        if lumn_db:
            sid = self._parse_cookie("lumn_sid")
            if sid:
                u = lumn_db.get_session_user(sid)
                if u:
                    return u
        # Legacy bearer path: return a synthetic local user so handlers that
        # want user_id have something to namespace by.
        token = self.headers.get("Authorization", "").replace("Bearer ", "")
        if token and hmac.compare_digest(token, _API_TOKEN):
            return {"id": 0, "email": "local@dev", "role": "admin",
                    "credits_cents": 10_000_000}
        return None

    def _check_auth(self):
        """Check auth. Browser UI uses CSRF token; external API uses bearer token;
        logged-in users use session cookies."""
        path = self.path.split("?")[0]
        # Skip auth for static files, health, the main page, and auth endpoints
        if path in ("/", "/health", "/landing", "/manifesto", "/signin", "/favicon.ico") or path.startswith("/public/") or path.startswith("/output/") or path.endswith(".png") or path.endswith(".webp") or path.endswith(".ico"):
            return True
        if path in ("/api/auth/signup", "/api/auth/login", "/api/auth/logout",
                    "/api/auth/me", "/api/feedback"):
            return True
        # Session cookie (DB-backed) = authenticated user
        if lumn_db:
            sid = self._parse_cookie("lumn_sid")
            if sid and lumn_db.get_session_user(sid):
                return True
        # Otherwise check bearer token (for external API access)
        # NOTE: we intentionally do NOT accept CSRF-alone as auth — CSRF is
        # a request-forgery guard, not an identity claim. The old code here
        # made the global CSRF token act as a shared API key (vuln C1).
        token = self.headers.get("Authorization", "").replace("Bearer ", "")
        if not hmac.compare_digest(token, _API_TOKEN):
            # Return JSON so client-side fetch wrappers that parse .json()
            # don't crash on the stdlib HTML error page.
            self._send_json({"error": "unauthorized"}, 401)
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", self._get_cors_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-CSRF-Token")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _send_json(self, data, status=200):
        self._last_status = status
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._get_cors_origin())
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-CSRF-Token")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    # In-memory cache for small text assets (css/js/html) keyed on (path, mtime).
    # Capped at ~4MB total to avoid memory blowup.
    _file_cache: dict = {}
    _file_cache_bytes: int = 0
    _FILE_CACHE_LIMIT = 4 * 1024 * 1024

    _EXT_CONTENT_TYPES = {
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".mp4": "video/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".zip": "application/zip",
    }

    def _send_file(self, path, content_type=None):
        # Single stat call — avoids 4-5 repeated os.path.* round-trips
        try:
            st = os.stat(path, follow_symlinks=False)
        except (FileNotFoundError, OSError):
            self.send_error(404)
            return
        import stat as _stat
        if _stat.S_ISLNK(st.st_mode):
            self.send_error(403, "Symlinks not allowed")
            return
        if not _stat.S_ISREG(st.st_mode):
            self.send_error(404)
            return
        # Path traversal protection: only serve files under PROJECT_DIR
        resolved = os.path.realpath(path)
        if not resolved.startswith(os.path.realpath(PROJECT_DIR)):
            self.send_error(403)
            return

        if content_type is None:
            ext = os.path.splitext(path)[1].lower()
            content_type = Handler._EXT_CONTENT_TYPES.get(ext, "application/octet-stream")

        size = st.st_size
        mtime = st.st_mtime
        etag = f'"{int(mtime)}-{size}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("ETag", etag)
        self.send_header("Access-Control-Allow-Origin", self._get_cors_origin())
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-CSRF-Token")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'")
        # Prevent browser from caching video/image clips; allow 30s TTL for static assets
        if content_type.startswith("video/") or content_type.startswith("image/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        else:
            self.send_header("Cache-Control", "public, max-age=30")
        self.end_headers()

        # In-memory cache for small text/code assets (big binaries stream from disk).
        is_cacheable = (
            size <= 512 * 1024
            and not content_type.startswith("video/")
            and not content_type.startswith("image/")
            and not content_type.startswith("audio/")
        )
        if is_cacheable:
            cache_key = (resolved, mtime, size)
            cached = Handler._file_cache.get(resolved)
            if cached and cached[0] == cache_key:
                self.wfile.write(cached[1])
                return
            with open(path, "rb") as f:
                data = f.read()
            # Evict old entry for this path if any
            if cached:
                Handler._file_cache_bytes -= len(cached[1])
            # Evict arbitrary entries until we fit
            while Handler._file_cache_bytes + len(data) > Handler._FILE_CACHE_LIMIT and Handler._file_cache:
                _, evicted = Handler._file_cache.popitem()
                Handler._file_cache_bytes -= len(evicted[1])
            Handler._file_cache[resolved] = (cache_key, data)
            Handler._file_cache_bytes += len(data)
            self.wfile.write(data)
            return

        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _parse_multipart(self, body: bytes, boundary: bytes):
        """Parse multipart form data. Returns list of {name, filename, data}."""
        parts = []
        sections = body.split(b"--" + boundary)
        for section in sections:
            if b"Content-Disposition" not in section:
                continue
            header_end = section.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            header = section[:header_end].decode(errors="replace")
            data = section[header_end + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]

            name = ""
            filename = ""
            for line in header.split("\r\n"):
                if "name=" in line:
                    m = re.search(r'name="([^"]*)"', line)
                    if m:
                        name = m.group(1)
                if "filename=" in line:
                    m = re.search(r'filename="([^"]*)"', line)
                    if m:
                        filename = m.group(1)

            parts.append({"name": name, "filename": filename, "data": data})
        return parts

    # ---- Routing ----

    def do_GET(self):
        _t0 = time.time()
        try:
            return self._do_GET_impl()
        finally:
            if lumn_obs:
                _lat = (time.time() - _t0) * 1000
                try:
                    lumn_obs.log_request("GET", self.path,
                                         getattr(self, "_last_status", 200),
                                         _lat, None)
                except Exception:
                    pass

    def _do_GET_impl(self):
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/favicon.ico":
            # Send transparent 1x1 GIF so the browser stops 404'ing on the
            # default favicon path. A real file under /public/ can override.
            fav = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Content-Length", str(len(fav)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(fav)
            return

        if path == "/":
            # In production mode, redirect unauthenticated visitors to /signin
            # so the landing page isn't a fingerprinting target.
            if os.environ.get("LUMN_PRODUCTION") == "1" and lumn_db:
                sid = self._parse_cookie("lumn_sid")
                if not (sid and lumn_db.get_session_user(sid)):
                    self.send_response(302)
                    self.send_header("Location", "/signin")
                    self.end_headers()
                    return
            # Inject CSRF token meta tag into index.html.
            # Cache the injected body in memory, keyed on file mtime so dev edits still pick up.
            index_path = os.path.join(PROJECT_DIR, "public", "index.html")
            # Per-session CSRF token: derived from the caller's session
            # cookie. Unauthenticated callers get an empty token (and won't
            # be able to mutate anything). Caching is keyed by (mtime, sid)
            # so different users don't share injected HTML.
            sid = self._parse_cookie("lumn_sid") or ""
            csrf_tok = _csrf_for_sid(sid) if sid else ""
            try:
                mtime = os.path.getmtime(index_path)
            except OSError:
                mtime = 0
            cache_key = (mtime, csrf_tok)
            cache = getattr(Handler, "_index_cache", None)
            if cache and cache[0] == cache_key:
                body = cache[1]
            else:
                with open(index_path, "r", encoding="utf-8") as f:
                    html = f.read()
                html = html.replace("</head>", f'<meta name="csrf-token" content="{csrf_tok}"></head>', 1)
                body = html.encode("utf-8")
                Handler._index_cache = (cache_key, body)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", self._get_cors_origin())
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-CSRF-Token")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/landing":
            self._send_file(os.path.join(PROJECT_DIR, "public", "landing.html"))
        elif path == "/tb-bear.png":
            self._send_file(os.path.join(PROJECT_DIR, "public", "tb-bear.png"))
        elif path in ("/bear-light.png", "/bear-dark.png", "/bg-light.png", "/bg-dark.png", "/logo.png", "/landing-light.png", "/landing-dark.png", "/landing-light.webp", "/landing-dark.webp"):
            self._send_file(os.path.join(PROJECT_DIR, "public", path.lstrip("/")))
        elif path == "/manifesto":
            self._send_file(os.path.join(PROJECT_DIR, "public", "manifesto.html"))

        elif path == "/signin":
            self._send_file(os.path.join(PROJECT_DIR, "public", "signin.html"))

        elif path == "/admin":
            # M1: gate the admin page itself on role==admin so it doesn't
            # leak structure to probes. APIs are already role-gated.
            _u = self._current_user() or {}
            if _u.get("role") != "admin":
                return self.send_error(404)
            self._send_file(os.path.join(PROJECT_DIR, "public", "admin.html"))

        elif path.startswith("/public/"):
            rel = path[len("/public/"):]
            safe = os.path.normpath(rel)
            full = os.path.realpath(os.path.join(PROJECT_DIR, "public", safe))
            if not full.startswith(os.path.realpath(os.path.join(PROJECT_DIR, "public"))):
                self.send_error(403)
                return
            self._send_file(full)

        elif path == "/health":
            import time as _time_mod
            self._send_json({"status": "ok", "uptime": _time_mod.time() - _server_start_time})

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
                    "cancel_requested": gen_state.get("cancel_requested", False),
                }
            self._send_json(data)

        elif path == "/api/download":
            with gen_lock:
                out = gen_state["output_file"]
            if out and os.path.isfile(out):
                self._send_file(out, "video/mp4")
            else:
                self.send_error(404, "No video available yet")

        elif path == "/api/scenes":
            self._handle_get_scenes()

        elif path.startswith("/api/clips/"):
            filename = path[len("/api/clips/"):]
            safe = os.path.basename(filename)
            # Try auto clips dir first, then manual clips dir, then search takes
            clip_file = os.path.join(CLIPS_DIR, safe)
            if not os.path.isfile(clip_file):
                clip_file = os.path.join(MANUAL_CLIPS_DIR, safe)
            if not os.path.isfile(clip_file):
                clip_file = os.path.join(OUTPUT_DIR, "auto_director", safe)
            if not os.path.isfile(clip_file):
                # Search in takes directories
                for scene_dir in os.listdir(TAKES_DIR) if os.path.isdir(TAKES_DIR) else []:
                    candidate = os.path.join(TAKES_DIR, scene_dir, safe)
                    if os.path.isfile(candidate):
                        clip_file = candidate
                        break
            self._send_file(clip_file)

        elif path == "/api/references":
            self._handle_get_references()

        elif path.startswith("/api/references/"):
            name = urllib.parse.unquote(path[len("/api/references/"):])
            self._handle_get_reference_image(name)

        elif path == "/api/presets":
            self._handle_get_presets()

        elif path == "/api/transitions":
            self._send_json({"transitions": TRANSITION_TYPES})

        elif path == "/api/manual/scenes":
            self._handle_manual_list_scenes()

        elif path.startswith("/api/manual/scene-photo/"):
            scene_id = path[len("/api/manual/scene-photo/"):]
            self._handle_get_scene_photo(scene_id)

        elif path == "/api/exports":
            self._handle_list_exports()

        elif path.startswith("/api/exports/"):
            filename = path[len("/api/exports/"):]
            safe = os.path.basename(filename)
            self._send_file(os.path.join(EXPORTS_DIR, safe))

        elif path.startswith("/output/"):
            rel = path[len("/output/"):]
            safe = os.path.normpath(rel)
            # Backwards compat: legacy HTML/JSON references to
            # /output/prompt_os/<rest> now resolve to
            # /output/projects/<active-slug>/prompt_os/<rest>. We try the
            # active project first, then fall back to the literal legacy
            # path in case any files survived migration.
            legacy_prefix = "prompt_os" + os.sep
            if safe == "prompt_os" or safe.startswith(legacy_prefix):
                try:
                    active_slug = active_project.get_active_slug()
                except Exception:
                    active_slug = active_project.DEFAULT_SLUG
                rest = safe[len(legacy_prefix):] if safe.startswith(legacy_prefix) else ""
                remapped = os.path.join("projects", active_slug, "prompt_os", rest) if rest else os.path.join("projects", active_slug, "prompt_os")
                remapped_full = os.path.realpath(os.path.join(OUTPUT_DIR, remapped))
                if remapped_full.startswith(os.path.realpath(OUTPUT_DIR)) and os.path.isfile(remapped_full):
                    self._send_file(remapped_full)
                    return
                # Fall through to literal legacy resolution.
            full = os.path.realpath(os.path.join(OUTPUT_DIR, safe))
            if not full.startswith(os.path.realpath(OUTPUT_DIR)):
                self.send_error(403)
                return
            self._send_file(full)

        elif path == "/api/cost":
            self._handle_get_cost()

        elif path == "/api/cost-tracker":
            # Alias for tools/multi_shot_test.py budget precheck.
            self._handle_get_cost()

        elif path == "/api/cost/ledger":
            self._handle_cost_ledger()

        elif path == "/api/cost/export.csv":
            self._handle_cost_csv()

        elif path == "/api/runway/credits":
            self._handle_runway_credits()

        elif path.startswith("/api/storyboard/"):
            fname = os.path.basename(path[len("/api/storyboard/"):])
            self._send_file(os.path.join(STORYBOARD_DIR, fname))

        elif path.startswith("/api/previews/"):
            fname = os.path.basename(path[len("/api/previews/"):])
            self._send_file(os.path.join(PREVIEWS_DIR, fname))

        elif path.startswith("/api/scene-thumbnails/"):
            fname = os.path.basename(path[len("/api/scene-thumbnails/"):])
            self._send_file(os.path.join(SCENE_THUMBNAILS_DIR, fname))

        elif path == "/api/preview-thumbnail/status":
            with preview_lock:
                self._send_json(dict(preview_state))

        elif path == "/api/auto-director/scenes/preview-status":
            self._handle_scenes_preview_status()

        elif path == "/api/camera-presets":
            presets = []
            for name, info in CAMERA_PRESETS.items():
                presets.append({"name": name, "description": info["desc"]})
            self._send_json({"presets": presets})

        elif path == "/api/genre-suggest":
            # GET with query params: ?bpm=120&energy=0.5
            qs = urllib.parse.parse_qs(parsed.query)
            bpm = float(qs.get("bpm", [120])[0])
            energy = float(qs.get("energy", [0.5])[0])
            suggestion = suggest_genre_from_bpm(bpm, energy)
            self._send_json({"ok": True, **suggestion})

        elif path == "/api/thumbnail":
            if os.path.isfile(THUMBNAIL_PATH):
                self._send_file(THUMBNAIL_PATH)
            else:
                self.send_error(404, "No thumbnail available")

        elif path == "/api/watermark":
            if os.path.isfile(WATERMARK_PATH):
                self._send_file(WATERMARK_PATH)
            else:
                self.send_error(404, "No watermark uploaded")

        elif path.startswith("/api/social-exports/"):
            fname = os.path.basename(path[len("/api/social-exports/"):])
            self._send_file(os.path.join(SOCIAL_EXPORTS_DIR, fname))

        elif path == "/api/social-exports":
            exports = []
            if os.path.isdir(SOCIAL_EXPORTS_DIR):
                for fname in sorted(os.listdir(SOCIAL_EXPORTS_DIR)):
                    fpath = os.path.join(SOCIAL_EXPORTS_DIR, fname)
                    if os.path.isfile(fpath) and fname.endswith(".mp4"):
                        size_mb = os.path.getsize(fpath) / (1024 * 1024)
                        exports.append({
                            "filename": fname,
                            "url": f"/api/social-exports/{fname}",
                            "size_mb": round(size_mb, 1),
                        })
            self._send_json({"exports": exports})

        elif path == "/api/engines":
            engines = get_available_engines()
            self._send_json({"engines": engines})

        elif path == "/api/model-durations":
            self._send_json({"durations": MODEL_DURATION_OPTIONS})

        elif path == "/api/settings":
            settings = _load_settings()
            self._send_json(settings)

        elif path == "/api/engine-catalog":
            catalog_path = os.path.join(OUTPUT_DIR, "engine_catalog.json")
            if os.path.isfile(catalog_path):
                self._send_file(catalog_path, "application/json")
            else:
                self._send_json({})

        elif path == "/api/prompt-history":
            self._handle_get_prompt_history()

        elif path == "/api/render-estimate":
            self._handle_render_estimate(parsed)

        elif path == "/api/project/autosave":
            self._handle_get_autosave()

        elif path == "/api/character-references":
            char_refs = _get_character_references()
            items = []
            for name, fpath in char_refs.items():
                items.append({
                    "name": name,
                    "path": fpath,
                    "url": f"/api/references/{urllib.parse.quote(name)}",
                    "exists": os.path.isfile(fpath),
                })
            self._send_json({"character_references": items})

        # Serve uploaded scene videos
        elif path.startswith("/api/manual/scene-video/"):
            scene_id = path[len("/api/manual/scene-video/"):]
            self._handle_get_scene_video(scene_id)

        # Serve full project zips
        elif path.startswith("/api/full-projects/"):
            fname = os.path.basename(path[len("/api/full-projects/"):])
            self._send_file(os.path.join(FULL_PROJECTS_DIR, fname))

        # Item 9: Color palette extraction
        elif re.match(r'^/api/manual/scene/([^/]+)/palette$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/palette$', path)
            self._handle_get_palette(m.group(1))

        # Item 34: Serve exported GIFs
        elif path.startswith("/api/gifs/"):
            fname = os.path.basename(path[len("/api/gifs/"):])
            self._send_file(os.path.join(GIFS_DIR, fname))

        # Item 42: Template library - list templates
        elif path == "/api/templates":
            self._handle_list_templates()

        # Item 46: Project comparison - get previous clip for a scene
        elif re.match(r'^/api/manual/scene/([^/]+)/previous-clip$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/previous-clip$', path)
            self._handle_get_previous_clip(m.group(1))

        elif path == "/api/estimate-render":
            self._handle_estimate_render_time()

        elif path.startswith("/output/gifs/"):
            filename = os.path.basename(path)
            self._send_file(os.path.join(OUTPUT_DIR, "gifs", filename), "image/gif")

        elif path == "/api/project/reset":
            self._handle_project_reset()

        # ──── Prompt OS GET routes ────
        elif path == "/api/pos/dashboard":
            self._send_json(_prompt_os.get_dashboard())

        elif path == "/api/pos/prompts":
            qs = urllib.parse.parse_qs(parsed.query)
            category = qs.get("category", [None])[0]
            self._send_json({"prompts": _prompt_os.get_prompts(category)})

        elif re.match(r'^/api/pos/prompts/([^/]+)$', path):
            m = re.match(r'^/api/pos/prompts/([^/]+)$', path)
            rec = _prompt_os.get_prompt(m.group(1))
            if rec:
                self._send_json(rec)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif path == "/api/pos/characters":
            self._send_json({"characters": _prompt_os.get_characters()})

        elif re.match(r'^/api/pos/characters/([^/]+)$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)$', path)
            rec = _prompt_os.get_character(m.group(1))
            if rec:
                self._send_json(rec)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif path == "/api/pos/costumes":
            qs = urllib.parse.parse_qs(parsed.query)
            char_id = qs.get("characterId", [None])[0]
            self._send_json({"costumes": _prompt_os.get_costumes(char_id)})

        elif re.match(r'^/api/pos/costumes/([^/]+)$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)$', path)
            rec = _prompt_os.get_costume(m.group(1))
            if rec:
                self._send_json(rec)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif path == "/api/pos/environments":
            self._send_json({"environments": _prompt_os.get_environments()})

        elif re.match(r'^/api/pos/environments/([^/]+)$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)$', path)
            rec = _prompt_os.get_environment(m.group(1))
            if rec:
                self._send_json(rec)
            else:
                self._send_json({"error": "Not found"}, 404)

        # ──── Prompt OS Photo/Preview GET routes ────
        elif re.match(r'^/api/pos/characters/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)/photo$', path)
            self._send_file(os.path.join(_pos_photos_dir("char"), m.group(1) + ".jpg"))

        elif re.match(r'^/api/pos/costumes/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)/photo$', path)
            self._send_file(os.path.join(_pos_photos_dir("costume"), m.group(1) + ".jpg"))

        elif re.match(r'^/api/pos/environments/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)/photo$', path)
            self._send_file(os.path.join(_pos_photos_dir("env"), m.group(1) + ".jpg"))

        elif re.match(r'^/api/pos/voices/([^/]+)/sample$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)/sample$', path)
            vid = m.group(1)
            sample_path = None
            for ext in (".mp3", ".wav", ".m4a", ".ogg", ".webm"):
                candidate = os.path.join(_pos_voices_dir(), f"{vid}{ext}")
                if os.path.isfile(candidate):
                    sample_path = candidate
                    break
            if sample_path:
                self._send_file(sample_path)
            else:
                self._send_json({"error": "No sample found"}, 404)

        elif re.match(r'^/api/pos/characters/([^/]+)/preview$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)/preview$', path)
            cid = m.group(1)
            # Resolve preview from character entity, with fallbacks
            _char = _prompt_os.get_character(cid)
            _prev = (_char.get("previewImage", "") or "") if _char else ""
            _prev_path = None
            if _prev and not _prev.startswith("/api/"):
                # Direct file URL like /output/prompt_os/previews/characters/...
                _cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), _prev.lstrip("/"))
                if os.path.isfile(_cand):
                    _prev_path = _cand
            if not _prev_path:
                # Legacy fallback: {cid}_sheet.jpg or {cid}.jpg
                for _ext in (".png", ".jpg", ".jpeg", ".webp"):
                    for _sfx in ("_sheet", ""):
                        _cand = os.path.join(_pos_previews_dir("char"), f"{cid}{_sfx}{_ext}")
                        if os.path.isfile(_cand):
                            _prev_path = _cand
                            break
                    if _prev_path:
                        break
            if not _prev_path:
                # Last resort: find any file matching the cid prefix
                import glob as _glob_prev
                _matches = sorted(_glob_prev.glob(os.path.join(_pos_previews_dir("char"), f"{cid}*")),
                                  key=os.path.getmtime, reverse=True)
                if _matches:
                    _prev_path = _matches[0]
            self._send_file(_prev_path or os.path.join(_pos_previews_dir("char"), f"{cid}.jpg"))

        elif re.match(r'^/api/pos/costumes/([^/]+)/preview$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)/preview$', path)
            self._send_file(os.path.join(_pos_previews_dir("costume"), m.group(1) + ".jpg"))

        elif re.match(r'^/api/pos/environments/([^/]+)/preview$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)/preview$', path)
            self._send_file(os.path.join(_pos_previews_dir("env"), m.group(1) + ".jpg"))

        elif path == "/api/pos/scenes":
            self._send_json({"scenes": _prompt_os.get_scenes()})

        elif path == "/api/pos/coverage/report":
            from lib.coverage import coverage_report, VALID_TIERS, TIER_REQUIRED_SIZES
            self._send_json({
                "report": coverage_report(_prompt_os.get_scenes()),
                "valid_tiers": list(VALID_TIERS),
                "tier_required_sizes": {k: list(v) for k, v in TIER_REQUIRED_SIZES.items()},
            })

        elif re.match(r'^/api/pos/scenes/([^/]+)/assemble$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)/assemble$', path)
            result = _prompt_os.assemble_prompt(m.group(1))
            self._send_json(result)

        elif re.match(r'^/api/pos/scenes/([^/]+)/validate$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)/validate$', path)
            result = _prompt_os.validate_assembly(m.group(1))
            self._send_json({"validations": result})

        elif re.match(r'^/api/pos/scenes/([^/]+)$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)$', path)
            rec = _prompt_os.get_scene(m.group(1))
            if rec:
                self._send_json(rec)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif path == "/api/pos/voices":
            self._send_json({"voices": _prompt_os.get_voices()})

        elif re.match(r'^/api/pos/voices/([^/]+)$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)$', path)
            rec = _prompt_os.get_voice(m.group(1))
            if rec:
                self._send_json(rec)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif path == "/api/pos/style-locks":
            self._send_json({"styleLocks": _prompt_os.get_style_locks()})

        elif path == "/api/pos/world-rules":
            self._send_json({"worldRules": _prompt_os.get_world_rules()})

        # ──── Auto Director GET routes ────
        elif path == "/api/auto-director/status":
            self._send_json(_auto_director.progress)

        elif path == "/api/story-models":
            from lib.story_planner import AVAILABLE_MODELS, DEFAULT_MODEL, _PROVIDER_KEYS
            models = []
            for model_id, info in AVAILABLE_MODELS.items():
                key_env = _PROVIDER_KEYS.get(info["provider"], "")
                available = bool(os.environ.get(key_env, "")) if key_env else False
                models.append({
                    "id": model_id,
                    "label": info["label"],
                    "provider": info["provider"],
                    "tier": info["tier"],
                    "available": available,
                })
            self._send_json({"models": models, "default": DEFAULT_MODEL})

        elif path == "/api/auto-director/plan":
            if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
                try:
                    with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                        self._send_json(json.load(f))
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"scenes": []})
            else:
                self._send_json({"scenes": []})

        elif path == "/api/auto-director/plan/score":
            if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
                try:
                    with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                        plan = json.load(f)
                    from lib.quality_metrics import score_plan_quality
                    score = score_plan_quality(plan)
                    self._send_json(score)
                except ImportError:
                    self._send_json({"error": "quality_metrics not available"}, 500)
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
            else:
                self._send_json({"error": "No plan found"}, 404)

        # ──── V5 Pipeline GET routes ────
        elif path == "/api/pipeline/state":
            self._handle_pipeline_get_state()

        elif path == "/api/pipeline/anchors":
            self._handle_pipeline_get_anchors()

        elif path == "/api/v6/anchors":
            self._handle_v6_get_anchors()

        elif path == "/api/v6/clips":
            self._handle_v6_get_clips()

        elif re.match(r'^/api/v6/clip-versions/([^/]+)$', path):
            m = re.match(r'^/api/v6/clip-versions/([^/]+)$', path)
            self._handle_v6_clip_versions(m.group(1))

        elif re.match(r'^/api/v6/clip-version-file/([^/]+)/v(\d+)\.mp4$', path):
            m = re.match(r'^/api/v6/clip-version-file/([^/]+)/v(\d+)\.mp4$', path)
            self._handle_v6_serve_file(os.path.join(OUTPUT_DIR, "pipeline", "clips_v6", "_versions", m.group(1), f"v{m.group(2)}.mp4"))

        elif re.match(r'^/api/v6/anchor-image/(.+)$', path):
            m = re.match(r'^/api/v6/anchor-image/(.+)$', path)
            self._handle_v6_serve_file(os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6", m.group(1)))

        elif re.match(r'^/api/v6/clip-video/(.+)$', path):
            m = re.match(r'^/api/v6/clip-video/(.+)$', path)
            self._handle_v6_serve_file(os.path.join(OUTPUT_DIR, "pipeline", "clips_v6", m.group(1)))

        elif re.match(r'^/api/v6/final-video/(.+)$', path):
            m = re.match(r'^/api/v6/final-video/(.+)$', path)
            self._handle_v6_serve_file(os.path.join(OUTPUT_DIR, "pipeline", m.group(1)))

        elif re.match(r'^/api/v6/render/remotion/status/([A-Za-z0-9_\-]+)$', path):
            m = re.match(r'^/api/v6/render/remotion/status/([A-Za-z0-9_\-]+)$', path)
            self._handle_v6_remotion_render_status(m.group(1))

        elif path == "/api/v6/references":
            self._handle_v6_get_references()

        elif path == "/api/templates":
            from lib.project_templates import list_templates
            self._send_json({"templates": list_templates()})

        elif path == "/api/v6/timeline":
            self._handle_v6_timeline_preview()

        elif path == "/api/auth/me":
            self._handle_auth_me()

        elif path == "/api/fal/balance":
            self._handle_fal_balance()

        elif path == "/api/metrics":
            # Admin-only in-process metrics snapshot.
            _u = self._current_user() or {}
            if _u.get("role") != "admin":
                return self._send_json({"error": "admin only"}, 403)
            if lumn_obs:
                return self._send_json(lumn_obs.snapshot())
            return self._send_json({"error": "obs disabled"}, 503)

        elif path.startswith("/api/jobs/"):
            # GET /api/jobs/<job_id>  → job status + result (when done)
            # GET /api/jobs/<job_id>/stream → SSE stream of stages
            tail = path[len("/api/jobs/"):]
            stream = tail.endswith("/stream")
            jid = tail[:-len("/stream")] if stream else tail
            if not jid or "/" in jid:
                return self._send_json({"error": "bad job id"}, 400)
            if not lumn_db:
                return self._send_json({"error": "db unavailable"}, 503)
            row = lumn_db.get_job(jid)
            if not row:
                return self._send_json({"error": "not found"}, 404)
            # Owner-only access (admins can see all)
            cu = self._current_user() or {}
            if cu.get("role") != "admin" and int(cu.get("id", 0) or 0) != int(row["user_id"]):
                return self._send_json({"error": "forbidden"}, 403)
            if stream:
                return self._stream_job(jid)
            return self._send_json(self._serialize_job(row))

        elif path == "/api/admin/users":
            _u = self._current_user() or {}
            if _u.get("role") != "admin":
                return self._send_json({"error": "admin only"}, 403)
            return self._send_json({"users": lumn_db.list_users(limit=200)})

        elif path == "/api/admin/spend":
            _u = self._current_user() or {}
            if _u.get("role") != "admin":
                return self._send_json({"error": "admin only"}, 403)
            window = int((urllib.parse.parse_qs(parsed.query).get("window", ["86400"]))[0])
            return self._send_json(lumn_db.spend_summary(window))

        elif path == "/api/admin/feedback":
            _u = self._current_user() or {}
            if _u.get("role") != "admin":
                return self._send_json({"error": "admin only"}, 403)
            qs = urllib.parse.parse_qs(parsed.query)
            return self._send_json({"feedback": lumn_db.list_feedback(
                limit=int(qs.get("limit", ["100"])[0]),
                unresolved_only=qs.get("unresolved", ["0"])[0] == "1",
            )})

        elif path == "/api/v6/identity-gate":
            from lib.identity_gate import load_state
            self._send_json(load_state())

        elif path == "/api/v6/staleness":
            self._handle_v6_staleness_check()

        elif path == "/api/v6/audio/beats":
            self._handle_v6_audio_beats()

        elif path == "/api/v6/song/timing":
            self._handle_v6_song_timing()

        elif path == "/api/v6/shots/gates":
            self._handle_v6_shot_gates_get()

        elif path == "/api/v6/project/export/fcpxml":
            self._handle_v6_export_fcpxml()

        elif re.match(r'^/api/templates/([a-z_]+)$', path):
            from lib.project_templates import get_template
            m = re.match(r'^/api/templates/([a-z_]+)$', path)
            tpl = get_template(m.group(1))
            if tpl:
                self._send_json(tpl)
            else:
                self._send_json({"error": "Template not found"}, 404)

        elif re.match(r'^/api/v6/reference-image/(.+)$', path):
            m = re.match(r'^/api/v6/reference-image/(.+)$', path)
            self._handle_v6_serve_file(os.path.join(OUTPUT_DIR, "pipeline", "references_v6", m.group(1)))

        # ──── Preproduction GET routes ────
        elif path == "/api/preproduction/packages":
            self._handle_preproduction_get_packages()

        elif re.match(r'^/api/preproduction/package/([^/]+)$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)$', path)
            self._handle_preproduction_get_package(m.group(1))

        elif path == "/api/preproduction/report":
            self._handle_preproduction_report()

        elif path == "/api/preproduction/validate":
            self._handle_preproduction_validate_get()

        # ──── Taste Profile GET routes ────
        elif path == "/api/taste/quiz":
            self._handle_taste_get_quiz()

        elif path == "/api/taste/overall":
            self._handle_taste_get_overall()

        elif re.match(r'^/api/taste/project/([^/]+)$', path):
            m = re.match(r'^/api/taste/project/([^/]+)$', path)
            self._handle_taste_get_project(m.group(1))

        elif path == "/api/taste/blended":
            self._handle_taste_get_blended()

        # ──── Movie Planner GET routes ────
        elif path == "/api/auto-director/movie-plan":
            plan = load_movie_plan(OUTPUT_DIR)
            if plan:
                self._send_json(plan)
            else:
                self._send_json({"scenes": [], "bible": {}, "beats": []})

        elif path == "/api/auto-director/coverage":
            plan = load_movie_plan(OUTPUT_DIR)
            if plan and plan.get("scenes"):
                bible = rebuild_bible_from_plan(plan)
                coverage = AssetCoverage.check_coverage(bible, plan["scenes"])
                self._send_json(coverage)
            else:
                self._send_json({"characters": {"total": 0, "used": 0, "unused_names": []},
                                  "costumes": {"total": 0, "used": 0, "unused_names": []},
                                  "environments": {"total": 0, "used": 0, "unused_names": []},
                                  "warnings": []})

        elif path == "/api/auto-director/available-assets":
            chars = _prompt_os.get_characters()
            costumes = _prompt_os.get_costumes()
            envs = _prompt_os.get_environments()
            # Merge draft assets with library assets
            drafts = _get_all_drafts()
            char_list = [{"id": c["id"], "name": c.get("name", ""), "state": "library"} for c in chars] if chars else []
            cos_list = [{"id": c["id"], "name": c.get("name", ""), "state": "library"} for c in costumes] if costumes else []
            env_list = [{"id": e["id"], "name": e.get("name", ""), "state": "library"} for e in envs] if envs else []
            for d in drafts.get("characters", []):
                char_list.append({"id": d["id"], "name": d.get("label", d.get("name", "")), "state": "draft"})
            for d in drafts.get("costumes", []):
                cos_list.append({"id": d["id"], "name": d.get("label", d.get("name", "")), "state": "draft"})
            for d in drafts.get("environments", []):
                env_list.append({"id": d["id"], "name": d.get("label", d.get("name", "")), "state": "draft"})
            self._send_json({
                "characters": char_list,
                "costumes": cos_list,
                "environments": env_list,
            })

        elif path == "/api/auto-director/creation-readiness":
            plan = load_movie_plan(OUTPUT_DIR)
            if plan and plan.get("scenes"):
                result = _creation_readiness(plan["scenes"])
                self._send_json(result)
            else:
                self._send_json({"ready": True, "issues": [], "summary": "No plan"})

        elif path == "/api/auto-director/draft-assets":
            self._send_json(_get_all_drafts())

        elif path == "/api/auto-director/validation":
            plan = load_movie_plan(OUTPUT_DIR)
            if plan and plan.get("scenes"):
                bible = rebuild_bible_from_plan(plan)
                validation = PlanValidator.validate(bible, plan["scenes"])
                self._send_json(validation)
            else:
                self._send_json({"valid": False, "score": 0, "issues": [{"severity": "error", "message": "No plan exists", "scene_index": None}]})

        elif path == "/api/workflow-presets":
            presets = get_workflow_presets()
            self._send_json({"presets": presets})

        elif path.startswith("/api/auto-director/clips/"):
            filename = os.path.basename(path[len("/api/auto-director/clips/"):])
            clip_file = os.path.join(AUTO_DIRECTOR_CLIPS_DIR, "auto_director", filename)
            if not os.path.isfile(clip_file):
                clip_file = os.path.join(AUTO_DIRECTOR_CLIPS_DIR, filename)
            self._send_file(clip_file)

        # ──── Cinematic Engine ────
        elif path == "/api/cinematic/camera-presets":
            from lib.cinematic_engine import CAMERA_PRESETS as CINEMATIC_CAMERA_PRESETS
            self._send_json({"presets": CINEMATIC_CAMERA_PRESETS})

        elif path == "/api/cinematic/performance-options":
            from lib.cinematic_engine import PERFORMANCE_DESCRIPTORS
            self._send_json({"options": PERFORMANCE_DESCRIPTORS})

        elif path == "/api/cinematic/style-memory":
            from lib.cinematic_engine import StyleMemory
            sm = StyleMemory()
            self._send_json(sm.get())

        elif path == "/api/director/pacing-styles":
            from lib.director_mode import get_pacing_styles
            self._send_json({"styles": get_pacing_styles()})

        elif path == "/api/director/plan":
            # Load saved director plan
            plan_path = os.path.join(OUTPUT_DIR, "director_plan.json")
            if os.path.isfile(plan_path):
                try:
                    with open(plan_path, "r") as f:
                        self._send_json(json.load(f))
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"scenes": [], "shots": {}})
            else:
                self._send_json({"scenes": [], "shots": {}})

        elif path == "/api/cinematic/shot-styles":
            from lib.shot_style_library import get_full_library
            self._send_json(get_full_library())

        # ──── Beat Sync ────
        elif path == "/api/cinematic/beat-sync":
            from lib.beat_sync import CUT_MODES, SYNC_PRIORITIES
            self._send_json({"cut_modes": CUT_MODES, "sync_priorities": SYNC_PRIORITIES})

        # ──── Coherence Scorer ────
        elif path == "/api/cinematic/coherence/project":
            from lib.coherence_scorer import score_project
            settings = _load_settings()
            all_shots = settings.get("shots_data", {})
            scenes_data = _prompt_os.get_scenes() if hasattr(_prompt_os, 'get_scenes') else []
            result = score_project(all_shots, scenes_data)
            self._send_json(result)

        # ──── Coverage System ────
        elif path == "/api/cinematic/coverage-modes":
            from lib.coverage_system import COVERAGE_MODES, COVERAGE_ROLES
            self._send_json({"modes": {k: {"name": v["name"], "shots_range": v["shots_range"]} for k, v in COVERAGE_MODES.items()}, "roles": COVERAGE_ROLES})

        # ──── Narrative Engine ────
        elif path == "/api/cinematic/arc-types":
            from lib.narrative_engine import get_arc_types, get_scene_roles
            self._send_json({"arc_types": get_arc_types(), "scene_roles": get_scene_roles()})

        elif path == "/api/queue":
            with gen_queue_lock:
                items_safe = []
                for it in gen_queue["items"]:
                    items_safe.append({k: v for k, v in it.items() if k != "shot_data"})
                self._send_json({"items": items_safe, "max_parallel": gen_queue["max_parallel"]})

        elif path.startswith("/api/cinematic/continuity/"):
            # GET /api/cinematic/continuity/{scene_id} — validate scene continuity
            scene_id = path.split("/")[-1]
            from lib.cinematic_engine import validate_scene_continuity
            settings = _load_settings()
            shots = settings.get("shots_data", {}).get(scene_id, [])
            scene_data = _prompt_os.get_scene(scene_id) if scene_id else None
            result = validate_scene_continuity(shots, scene_data)
            self._send_json(result)

        # ──── Multi-project registry (new API) ────
        elif path == "/api/projects":
            try:
                projects = active_project.list_projects()
                active_slug = active_project.get_active_slug()
                self._send_json({"projects": projects, "active": active_slug})
            except Exception as _e:
                self._send_json({"error": str(_e)}, 500)

        elif path == "/api/projects/current":
            # Back-compat alias → returns the active project's meta.
            try:
                active_slug = active_project.get_active_slug()
                current = next(
                    (p for p in active_project.list_projects() if p["slug"] == active_slug),
                    None,
                )
                self._send_json({"current": current, "active": active_slug})
            except Exception as _e:
                self._send_json({"error": str(_e)}, 500)

        elif re.match(r'^/api/projects/([^/]+)/thumbnail$', path):
            m = re.match(r'^/api/projects/([^/]+)/thumbnail$', path)
            thumb = os.path.join(FULL_PROJECTS_DIR, m.group(1), "thumbnail.jpg")
            if os.path.isfile(thumb):
                self._send_file(thumb)
            else:
                self.send_error(404)

        # ──── Feature 2+10: Batch Queue Status ────
        elif path == "/api/manual/queue-status":
            with batch_lock:
                data = {
                    "active": batch_queue_state["active"],
                    "total": batch_queue_state["total"],
                    "completed": batch_queue_state["completed"],
                    "failed": batch_queue_state["failed"],
                    "scenes": list(batch_queue_state["scenes"]),
                    "elapsed": round(time.time() - batch_queue_state["start_time"], 1) if batch_queue_state["active"] else 0,
                }
            self._send_json(data)

        # ──── Feature 4: Takes/Compare ────
        elif re.match(r'^/api/manual/scene/([^/]+)/takes$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/takes$', path)
            takes = _get_takes(m.group(1))
            self._send_json({"takes": takes})

        # Serve take clips
        elif path.startswith("/api/takes/"):
            rel = path[len("/api/takes/"):]
            safe = os.path.normpath(rel)
            full = os.path.realpath(os.path.join(TAKES_DIR, safe))
            if not full.startswith(os.path.realpath(TAKES_DIR)):
                self.send_error(403)
                return
            self._send_file(full)

        # Keyboard shortcuts reference
        elif path == "/api/keyboard-shortcuts":
            from lib.roadmap_features import KEYBOARD_SHORTCUTS
            self._send_json({"shortcuts": KEYBOARD_SHORTCUTS})

        # Analytics
        elif path == "/api/analytics":
            from lib.roadmap_features import get_analytics
            self._send_json(get_analytics(OUTPUT_DIR))

        # ---- Director Brain ----
        elif path == "/api/director-brain/status":
            from lib.director_brain import get_brain
            brain = get_brain()
            summary = brain.get_style_summary()
            ratings = brain._load("ratings.json", [])
            total = len(ratings)
            avg = round(sum(r.get("rating", 0) for r in ratings) / total, 1) if total else 0
            self._send_json({"total_ratings": total, "avg_rating": avg, "summary": summary})

        elif path == "/api/director-brain/presets":
            from lib.director_brain import get_brain
            brain = get_brain()
            presets = brain.get_influence_presets()
            patterns = brain.get_best_prompt_patterns()
            self._send_json({"presets": presets, "patterns": patterns})

        # ---- AutoAgent ----
        elif path == "/api/autoagent/status":
            from lib.auto_agent import get_current_run
            run = get_current_run()
            if run:
                self._send_json(run.get_status())
            else:
                self._send_json({"running": False, "iteration": 0, "total": 0, "best_score": 0, "edits": []})

        elif path == "/api/autoagent/harness":
            from lib.auto_agent import HarnessManager
            hm = HarnessManager()
            self._send_json(hm._load_or_default())

        # ---- Generation Analytics ----
        elif path == "/api/generation-analytics":
            cost_path = os.path.join(OUTPUT_DIR, "cost_log.json")
            if os.path.isfile(cost_path):
                with open(cost_path, "r") as f:
                    self._send_json(json.load(f) if f else [])
            else:
                self._send_json([])

        # ---- POS: References (formerly Props) ----
        elif path == "/api/pos/references" or path == "/api/pos/props":
            refs = _prompt_os.get_references()
            # Keep legacy "props" key alongside the new "references" key for any
            # in-flight UI code still expecting the old response shape.
            self._send_json({"references": refs or [], "props": refs or []})

        # ---- POS: Continuity Rules ----
        elif path == "/api/pos/continuity-rules":
            rules_path = os.path.join(_prompt_os_data_dir(), "continuity_rules.json")
            if os.path.isfile(rules_path):
                with open(rules_path, "r") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json([])

        # ---- POS: Project Style ----
        elif path == "/api/pos/project-style":
            style_path = os.path.join(_prompt_os_data_dir(), "project_style.json")
            if os.path.isfile(style_path):
                with open(style_path, "r") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({})

        # ---- Reference Demos ----
        elif path == "/api/reference-demos":
            demos_dir = os.path.join(OUTPUT_DIR, "reference_demos")
            result = {}
            if os.path.isdir(demos_dir):
                for cat in os.listdir(demos_dir):
                    cat_dir = os.path.join(demos_dir, cat)
                    if os.path.isdir(cat_dir):
                        result[cat] = [f"/output/reference_demos/{cat}/{f}" for f in os.listdir(cat_dir) if f.endswith(('.png', '.jpg', '.jpeg', '.webp'))]
            self._send_json(result)

        # ---- Keyframe endpoints (GET) ----
        elif re.match(r'^/api/scenes/(\d+)/keyframes$', path):
            m = re.match(r'^/api/scenes/(\d+)/keyframes$', path)
            self._handle_get_keyframes(int(m.group(1)))

        elif path.startswith("/api/keyframes/"):
            filename = path[len("/api/keyframes/"):]
            safe = os.path.basename(filename)
            kf_file = os.path.join(KEYFRAMES_DIR, safe)
            if os.path.isfile(kf_file):
                self._send_file(kf_file)
            else:
                # Fallback: check first_frames directory
                ff_file = os.path.join(OUTPUT_DIR, "first_frames", safe)
                if os.path.isfile(ff_file):
                    self._send_file(ff_file)
                else:
                    self.send_error(404)

        else:
            self.send_error(404)

    def do_POST(self):
        _t0 = time.time()
        try:
            return self._do_POST_impl()
        finally:
            if lumn_obs:
                _lat = (time.time() - _t0) * 1000
                try:
                    lumn_obs.log_request("POST", self.path,
                                         getattr(self, "_last_status", 200),
                                         _lat, None)
                except Exception:
                    pass

    def _do_POST_impl(self):
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        # CSRF check for API routes — exempt auth endpoints since the caller
        # won't have a CSRF cookie until after login. Token is now per-session
        # HMAC-derived (see C1 fix), so a bearer-token external client must
        # skip CSRF — bearer already proves possession of the API token.
        AUTH_EXEMPT = {"/api/auth/signup", "/api/auth/login", "/api/auth/logout",
                       "/api/feedback"}
        if path.startswith("/api/") and path not in AUTH_EXEMPT:
            bearer = self.headers.get("Authorization", "").replace("Bearer ", "")
            if bearer and hmac.compare_digest(bearer, _API_TOKEN):
                pass  # external API caller — CSRF not applicable
            else:
                sid = self._parse_cookie("lumn_sid") or ""
                csrf = self.headers.get("X-CSRF-Token", "")
                if not _verify_csrf(sid, csrf):
                    self._send_json({"error": "Invalid CSRF token"}, 403)
                    return
        # X-Lumn-Project header parity: applies to POS / vault / project
        # mutation routes. Auth endpoints are exempt so signin still works
        # across a project switch. The /api/projects POST (create) is also
        # exempt since it doesn't touch an existing project.
        if _is_project_scoped_mutation(path):
            mismatch = _check_project_header(self)
            if mismatch:
                self._send_json(mismatch[1], mismatch[0])
                return
        if path == "/api/upload":
            self._handle_upload()

        elif path == "/api/generate":
            self._handle_generate()

        elif re.match(r'^/api/scenes/(\d+)/regenerate$', path):
            m = re.match(r'^/api/scenes/(\d+)/regenerate$', path)
            self._handle_regen_scene(int(m.group(1)))

        elif path == "/api/restitch":
            self._handle_restitch()

        elif path == "/api/references/upload":
            self._handle_upload_reference()

        elif path == "/api/enhance-prompt":
            self._handle_enhance_prompt()

        elif path == "/api/suggest-style":
            self._handle_suggest_style()

        elif path == "/api/ai-autofill":
            self._handle_ai_autofill()

        elif path == "/api/manual/scene":
            self._handle_manual_create_scene()

        elif re.match(r'^/api/manual/scene/([^/]+)/photo$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/photo$', path)
            self._handle_manual_upload_photo(m.group(1))

        elif re.match(r'^/api/manual/scene/([^/]+)/generate$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/generate$', path)
            self._handle_manual_generate_scene(m.group(1))

        elif path == "/api/manual/generate-all":
            self._handle_manual_generate_all()

        elif path == "/api/manual/stitch":
            self._handle_manual_stitch()

        elif re.match(r'^/api/manual/scene/([^/]+)/merge$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/merge$', path)
            self._handle_manual_merge_scene(m.group(1))

        elif path == "/api/manual/stitch-settings":
            self._handle_manual_stitch_settings()

        elif path == "/api/manual/reorder":
            self._handle_manual_reorder()

        elif path == "/api/scenes/update-transitions":
            self._handle_update_transitions()

        elif re.match(r'^/api/scenes/(\d+)/transition$', path):
            m = re.match(r'^/api/scenes/(\d+)/transition$', path)
            self._handle_update_scene_transition(int(m.group(1)))

        elif path == "/api/lyrics":
            self._handle_lyrics()

        elif path == "/api/batch-export":
            self._handle_batch_export()

        elif re.match(r'^/api/manual/scene/([^/]+)/split$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/split$', path)
            self._handle_manual_split_scene(m.group(1))

        elif re.match(r'^/api/scenes/(\d+)/split$', path):
            m = re.match(r'^/api/scenes/(\d+)/split$', path)
            self._handle_auto_split_scene(int(m.group(1)))

        elif path == "/api/style-lock":
            self._handle_style_lock()

        elif re.match(r'^/api/manual/scene/([^/]+)/upscale$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/upscale$', path)
            self._handle_upscale_scene(m.group(1))

        elif path == "/api/storyboard":
            self._handle_generate_storyboard()

        elif re.match(r'^/api/manual/scene/([^/]+)/generate-from-photo$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/generate-from-photo$', path)
            self._handle_manual_generate_from_photo(m.group(1))

        elif path == "/api/manual/preview-transition":
            self._handle_preview_transition()

        elif path == "/api/manual/preview-all":
            self._handle_preview_all()

        # Feature 1: Auto-describe photo
        elif re.match(r'^/api/manual/scene/([^/]+)/describe-photo$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/describe-photo$', path)
            self._handle_describe_photo(m.group(1))

        # Feature 4: Watermark upload
        elif path == "/api/watermark/upload":
            self._handle_watermark_upload()

        # Feature 4: Apply watermark
        elif path == "/api/watermark/apply":
            self._handle_watermark_apply()

        # Feature 5: Credits roll
        elif path == "/api/credits":
            self._handle_credits()

        # Feature 6: Thumbnail
        elif path == "/api/thumbnail":
            self._handle_thumbnail()

        elif path == "/api/thumbnail/generate":
            self._handle_thumbnail_generate()

        # Feature 8: Multi-photo upload
        elif re.match(r'^/api/manual/scene/([^/]+)/photos$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/photos$', path)
            self._handle_multi_photo_upload(m.group(1))

        # Feature 9: Multi-track audio upload
        elif path == "/api/audio/upload-tracks":
            self._handle_upload_audio_tracks()

        # Feature 9: Mix audio tracks
        elif path == "/api/audio/mix":
            self._handle_mix_audio()

        # Feature 10: Social platform export
        elif path == "/api/social-export":
            self._handle_social_export()

        # Feature 3: Beat-sync cuts
        elif path == "/api/beat-sync":
            self._handle_beat_sync()

        # Feature: Video upload per scene
        elif re.match(r'^/api/manual/scene/([^/]+)/video$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/video$', path)
            self._handle_manual_upload_video(m.group(1))

        # Feature: Auto beat alignment
        elif path == "/api/auto-align-beats":
            self._handle_auto_align_beats()

        # Feature: Per-scene vocal upload
        elif re.match(r'^/api/manual/scene/([^/]+)/vocal$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/vocal$', path)
            self._handle_manual_upload_vocal(m.group(1))

        # Settings (engine, character references, etc.)
        elif path == "/api/settings":
            self._handle_update_settings()

        # Character reference management
        elif path == "/api/character-references":
            self._handle_update_character_references()

        # Prompt history star
        elif path == "/api/prompt-history/star":
            self._handle_star_prompt()

        # Autosave
        elif path == "/api/project/autosave":
            self._handle_autosave()

        # Feature: Save full project with clips
        elif path == "/api/project/save-full":
            self._handle_project_save_full()

        # Feature: Load full project from zip
        elif path == "/api/project/load-full":
            self._handle_project_load_full()

        # Item 18: Loop / boomerang effect
        elif re.match(r'^/api/manual/scene/([^/]+)/boomerang$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/boomerang$', path)
            self._handle_boomerang(m.group(1))

        # Item 20: Audio ducking
        elif path == "/api/audio-ducking":
            self._handle_audio_ducking()

        # Item 34: GIF export per scene
        elif re.match(r'^/api/manual/scene/([^/]+)/export-gif$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/export-gif$', path)
            self._handle_export_gif(m.group(1))

        # Item 42: Template library - save and load
        elif path == "/api/templates/save":
            self._handle_save_template()

        elif path == "/api/templates/load":
            self._handle_load_template()

        # Item 44: AI assistant suggestions
        elif path == "/api/suggest-prompt":
            self._handle_suggest_prompt()

        # Roadmap: Reverse clip
        elif re.match(r'^/api/manual/scene/([^/]+)/reverse$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/reverse$', path)
            self._handle_reverse_clip(m.group(1))

        # Roadmap: Enhanced prompt
        elif path == "/api/enhance-prompt-context":
            self._handle_enhance_context()

        # Roadmap: Auto transitions from energy
        elif path == "/api/auto-transitions-energy":
            self._handle_auto_transitions_energy()

        # Roadmap: Key detection
        elif path == "/api/detect-key":
            self._handle_detect_key()

        # Roadmap: Auto-mix master
        elif path == "/api/auto-mix":
            self._handle_auto_mix()

        # Roadmap: Click track
        elif path == "/api/click-track":
            self._handle_click_track()

        # Roadmap: Extract frames
        elif re.match(r'^/api/manual/scene/([^/]+)/frames$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/frames$', path)
            self._handle_extract_frames(m.group(1))

        # ──── Director Mode ────
        elif path == "/api/director/generate-plan":
            self._handle_director_generate_plan()

        elif path == "/api/director/apply-plan":
            self._handle_director_apply_plan()

        # ──── Prompt Assembly ────
        elif path == "/api/prompt/compile":
            self._handle_prompt_compile()

        elif path == "/api/prompt/compile-shot":
            self._handle_compile_shot()

        # ──── Auto Director POST routes ────
        elif path == "/api/auto-director/plan":
            self._handle_auto_director_plan()

        elif path == "/api/auto-director/ai-plan":
            self._handle_auto_director_ai_plan()

        elif path == "/api/auto-director/generate":
            self._handle_auto_director_generate()

        elif path == "/api/auto-director/restitch" or path == "/api/auto-director/stitch":
            self._handle_auto_director_restitch()

        elif path == "/api/auto-director/to-manual":
            self._handle_auto_director_to_manual()

        # ──── Movie Planner POST routes ────
        elif path == "/api/auto-director/movie-plan":
            self._handle_movie_plan()

        elif path == "/api/auto-director/import-shots":
            self._handle_import_shots()

        elif re.match(r'^/api/auto-director/scene/(\d+)/edit$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/edit$', path)
            self._handle_movie_scene_edit(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/assets$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/assets$', path)
            self._handle_scene_assets(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/validate-assets$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/validate-assets$', path)
            self._handle_scene_validate_assets(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/preview$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/preview$', path)
            self._handle_scene_preview(int(m.group(1)))

        elif path == "/api/auto-director/scenes/preview-batch":
            self._handle_scenes_preview_batch()

        elif path == "/api/auto-director/scenes/reorder":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            order = body.get("order", [])
            plan = load_movie_plan(OUTPUT_DIR)
            if plan and plan.get("scenes") and order:
                id_to_scene = {s.get("id", str(i)): s for i, s in enumerate(plan["scenes"])}
                reordered = [id_to_scene[sid] for sid in order if sid in id_to_scene]
                for i, s in enumerate(reordered):
                    s["order"] = i
                plan["scenes"] = reordered
                with _plan_file_lock:
                    save_movie_plan(plan, OUTPUT_DIR)
            self._send_json({"ok": True})

        elif re.match(r'^/api/auto-director/scene/(\d+)/regenerate$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/regenerate$', path)
            self._handle_movie_scene_regenerate(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/regenerate-downstream$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/regenerate-downstream$', path)
            self._handle_movie_scene_regenerate_downstream(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/lock$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/lock$', path)
            self._handle_movie_scene_lock(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/unlock$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/unlock$', path)
            self._handle_movie_scene_unlock(int(m.group(1)))

        elif path == "/api/auto-director/draft-assets/promote":
            self._handle_draft_promote()

        elif path == "/api/auto-director/draft-assets/resolve":
            self._handle_draft_resolve()

        elif path == "/api/auto-director/draft-assets/remove":
            self._handle_draft_remove()

        # ──── V4 Per-Shot Operations ────
        elif re.match(r'^/api/auto-director/shot/(\d+)/(\d+)/regenerate$', path):
            m = re.match(r'^/api/auto-director/shot/(\d+)/(\d+)/regenerate$', path)
            self._handle_v4_shot_regenerate(int(m.group(1)), int(m.group(2)))

        elif re.match(r'^/api/auto-director/shot/(\d+)/(\d+)/edit$', path):
            m = re.match(r'^/api/auto-director/shot/(\d+)/(\d+)/edit$', path)
            self._handle_v4_shot_edit(int(m.group(1)), int(m.group(2)))

        # ──── V5 Pipeline POST routes ────
        elif path == "/api/pipeline/start":
            self._handle_pipeline_start()

        elif path == "/api/pipeline/advance":
            self._handle_pipeline_advance()

        elif path == "/api/pipeline/sheets/generate":
            self._handle_pipeline_sheets_generate()

        elif path == "/api/pipeline/sheets/approve-all":
            self._handle_pipeline_sheets_approve_all()

        elif path == "/api/pipeline/anchors/generate":
            self._handle_pipeline_anchors_generate()

        elif re.match(r'^/api/pipeline/anchors/([^/]+)/approve$', path):
            m = re.match(r'^/api/pipeline/anchors/([^/]+)/approve$', path)
            self._handle_pipeline_anchor_approve(m.group(1))

        elif re.match(r'^/api/pipeline/anchors/([^/]+)/reject$', path):
            m = re.match(r'^/api/pipeline/anchors/([^/]+)/reject$', path)
            self._handle_pipeline_anchor_reject(m.group(1))

        elif re.match(r'^/api/pipeline/anchors/([^/]+)/regenerate$', path):
            m = re.match(r'^/api/pipeline/anchors/([^/]+)/regenerate$', path)
            self._handle_pipeline_anchor_regenerate(m.group(1))

        elif path == "/api/pipeline/generate":
            self._handle_pipeline_generate()

        elif path == "/api/pipeline/reset":
            self._handle_pipeline_reset()

        elif path == "/api/templates/apply":
            self._handle_template_apply()

        elif path == "/api/screenplay/parse":
            self._handle_screenplay_parse()

        elif path == "/api/v6/project/autosave":
            self._handle_v6_project_autosave()

        # ──── Beta feedback + shot ratings ────
        elif path == "/api/feedback":
            self._handle_feedback_submit()

        elif path == "/api/shot/rate":
            self._handle_shot_rate()

        # ──── Auth: signup / login / logout ────
        elif path == "/api/auth/signup":
            self._handle_auth_signup()

        elif path == "/api/auth/login":
            self._handle_auth_login()

        elif path == "/api/auth/logout":
            self._handle_auth_logout()

        # ──── V6 Pipeline: Gemini anchors + Kling clips via fal.ai ────
        elif path == "/api/v6/prompt/assemble":
            self._handle_v6_prompt_assemble()

        elif path == "/api/v6/prompt/lint":
            self._handle_v6_prompt_lint()

        elif path == "/api/v6/brief/expand":
            self._handle_v6_brief_expand()

        elif path == "/api/v6/director/storyplan":
            self._handle_v6_director_storyplan()

        elif path == "/api/v6/director/scene":
            self._handle_v6_director_scene()

        elif path == "/api/v6/director/kling_prompt":
            self._handle_v6_director_kling_prompt()

        elif path == "/api/v6/director/critique":
            self._handle_v6_director_critique()

        elif path == "/api/v6/director/direct-shot":
            self._handle_v6_director_direct_shot()

        elif path == "/api/v6/director/direct-all":
            self._handle_v6_director_direct_all()

        elif path == "/api/v6/director/variety-check":
            self._handle_v6_director_variety_check()

        elif path == "/api/v6/anchor/audit_full":
            self._handle_v6_anchor_audit_full()

        elif path == "/api/v6/anchor/audit_meta":
            self._handle_v6_anchor_audit_meta()

        elif path == "/api/v6/identity-gate/lock":
            self._handle_v6_identity_lock()

        elif path == "/api/v6/identity-gate/unlock":
            self._handle_v6_identity_unlock()

        elif path == "/api/v6/anchor/generate":
            self._handle_v6_anchor_generate()

        elif path == "/api/v6/clip/generate":
            self._handle_v6_clip_generate()

        elif path == "/api/v6/anchor/generate_async":
            self._handle_v6_anchor_generate_async()

        elif path == "/api/v6/clip/generate_async":
            self._handle_v6_clip_generate_async()

        elif path == "/api/v6/stitch":
            self._handle_v6_stitch()

        elif path == "/api/v6/stitch/beat-plan":
            self._handle_v6_beat_plan()

        elif path == "/api/v6/stitch/beat-snap":
            self._handle_v6_beat_snap()

        elif path == "/api/v6/render/remotion":
            self._handle_v6_remotion_render()

        elif path == "/api/v6/clips/drag-scan":
            self._handle_v6_clips_drag_scan()
        elif path == "/api/v6/clips/cut-drift":
            self._handle_v6_clips_cut_drift()
        elif path == "/api/v6/clips/motion-audit":
            self._handle_v6_clips_motion_audit()
        elif path == "/api/v6/pacing-arc":
            self._handle_v6_pacing_arc()
        elif path == "/api/v6/shots/plan-durations":
            self._handle_v6_shots_plan_durations()

        elif path == "/api/v6/sonnet/select":
            self._handle_v6_sonnet_select()
        elif path == "/api/v6/sonnet/override":
            self._handle_v6_sonnet_override()

        elif path == "/api/v6/sonnet/review":
            self._handle_v6_sonnet_review()

        elif path == "/api/v6/sonnet/audit-prompt":
            self._handle_v6_sonnet_audit_prompt()

        elif path == "/api/v6/anchor/audit":
            self._handle_v6_anchor_audit()

        elif path == "/api/v6/anchors/audit-batch":
            self._handle_v6_anchors_audit_batch()

        elif path == "/api/v6/song/analyze":
            self._handle_v6_song_analyze()

        elif path == "/api/v6/shots/gates/sync":
            self._handle_v6_shot_gates_sync()

        elif path == "/api/v6/shots/gates/set":
            self._handle_v6_shot_gates_set()

        elif path == "/api/v6/shots/gates/audit-all":
            self._handle_v6_shot_gates_audit_all()

        elif path == "/api/v6/reference/upload":
            self._handle_v6_reference_upload()

        # ──── Preproduction POST routes ────
        elif path == "/api/preproduction/package/create":
            self._handle_preproduction_create_package()

        elif re.match(r'^/api/preproduction/package/([^/]+)/update$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)/update$', path)
            self._handle_preproduction_update_package(m.group(1))

        elif re.match(r'^/api/preproduction/package/([^/]+)/generate$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)/generate$', path)
            self._handle_preproduction_generate_package(m.group(1))

        elif re.match(r'^/api/preproduction/package/([^/]+)/generate-view$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)/generate-view$', path)
            self._handle_preproduction_generate_view(m.group(1))

        elif re.match(r'^/api/preproduction/package/([^/]+)/hero-ref$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)/hero-ref$', path)
            self._handle_preproduction_hero_ref(m.group(1))

        elif re.match(r'^/api/preproduction/package/([^/]+)/approve$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)/approve$', path)
            self._handle_preproduction_approve(m.group(1))

        elif re.match(r'^/api/preproduction/package/([^/]+)/reject$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)/reject$', path)
            self._handle_preproduction_reject(m.group(1))

        elif re.match(r'^/api/preproduction/package/([^/]+)/delete$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)/delete$', path)
            self._handle_preproduction_delete(m.group(1))

        elif re.match(r'^/api/preproduction/package/([^/]+)/upload-ref$', path):
            m = re.match(r'^/api/preproduction/package/([^/]+)/upload-ref$', path)
            self._handle_preproduction_upload_ref(m.group(1))

        elif path == "/api/preproduction/plan-packages":
            self._handle_preproduction_plan_packages()

        elif path == "/api/preproduction/bind-shots":
            self._handle_preproduction_bind_shots()

        elif path == "/api/preproduction/set-mode":
            self._handle_preproduction_set_mode()

        # ──── Taste Profile POST routes ────
        elif path == "/api/taste/overall":
            self._handle_taste_save_overall()

        elif path == "/api/taste/quiz/submit":
            self._handle_taste_submit_quiz()

        elif path == "/api/taste/sliders":
            self._handle_taste_update_sliders()

        elif re.match(r'^/api/taste/project/([^/]+)$', path):
            m = re.match(r'^/api/taste/project/([^/]+)$', path)
            self._handle_taste_save_project(m.group(1))

        elif path == "/api/taste/behavior":
            self._handle_taste_record_behavior()

        elif path == "/api/taste/reset-overall":
            self._handle_taste_reset_overall()

        elif path == "/api/workflow-presets":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            preset = save_custom_preset(body)
            self._send_json({"ok": True, "preset": preset})

        # ──── Beat Sync ────
        elif path == "/api/cinematic/beat-sync/analyze":
            from lib.beat_sync import generate_beat_sync_plan
            from lib.audio_analyzer import analyze
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            cut_mode = body.get("cut_mode", "balanced")
            sync_priority = body.get("sync_priority", "hybrid")
            # Find song
            song_path = None
            mp = _load_manual_plan()
            song_path = mp.get("song_path")
            if not song_path or not os.path.isfile(song_path):
                for f in sorted(os.listdir(UPLOADS_DIR), key=lambda x: os.path.getmtime(os.path.join(UPLOADS_DIR, x)), reverse=True):
                    if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                        song_path = os.path.join(UPLOADS_DIR, f)
                        break
            if not song_path or not os.path.isfile(song_path):
                self._send_json({"error": "No audio file found"}, 400)
            else:
                audio = analyze(song_path)
                plan = generate_beat_sync_plan(audio, cut_mode=cut_mode, sync_priority=sync_priority)
                self._send_json({"ok": True, "plan": plan})

        # ──── Coherence Scorer ────
        elif path == "/api/cinematic/coherence/shot":
            from lib.coherence_scorer import score_shot
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            shot = body.get("shot", {})
            prev_shot = body.get("prev_shot")
            result = score_shot(shot, prev_shot)
            self._send_json(result)

        elif path == "/api/cinematic/coherence/scene":
            from lib.coherence_scorer import score_scene
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            scene_id = body.get("scene_id", "")
            settings = _load_settings()
            shots = settings.get("shots_data", {}).get(scene_id, [])
            scene_data = _prompt_os.get_scene(scene_id) if scene_id else None
            result = score_scene(shots, scene_data)
            self._send_json(result)

        # ──── Coverage System ────
        elif path == "/api/cinematic/coverage/generate":
            from lib.coverage_system import generate_coverage
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            beat = body.get("beat", body.get("scene_beat", ""))
            mode = body.get("mode", "standard")
            section_type = body.get("section_type", "verse")
            result = generate_coverage(beat, mode, section_type=section_type)
            # Optionally save to shots_data
            scene_id = body.get("scene_id", "")
            if scene_id and body.get("auto_save", False):
                settings = _load_settings()
                if scene_id not in settings.get("shots_data", {}):
                    settings.setdefault("shots_data", {})[scene_id] = []
                settings["shots_data"][scene_id].extend(result["shots"])
                _save_settings(settings)
            self._send_json({"ok": True, **result})

        # ──── Narrative Engine ────
        elif path == "/api/cinematic/narrative/generate":
            from lib.narrative_engine import generate_narrative_plan
            from lib.audio_analyzer import analyze
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            arc_type = body.get("arc_type", "rise")
            theme = body.get("theme", "")
            storyline = body.get("storyline", "")
            lyrics = body.get("lyrics", "")
            # Get sections from audio
            sections = []
            song_path = _load_manual_plan().get("song_path")
            if not song_path:
                for f in sorted(os.listdir(UPLOADS_DIR), key=lambda x: os.path.getmtime(os.path.join(UPLOADS_DIR, x)), reverse=True):
                    if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                        song_path = os.path.join(UPLOADS_DIR, f)
                        break
            if song_path and os.path.isfile(song_path):
                audio = analyze(song_path)
                sections = audio.get("sections", [])
            # Characters
            chars = []
            for cid in body.get("character_ids", []):
                c = _prompt_os.get_character(cid)
                if c:
                    chars.append(c)
            result = generate_narrative_plan(
                arc_type=arc_type, theme=theme, storyline=storyline,
                lyrics=lyrics, sections=sections, characters=chars,
            )
            self._send_json({"ok": True, "narrative": result})

        # ──── Shot Style Library ────
        elif path == "/api/cinematic/shot-styles/add":
            from lib.shot_style_library import add_custom_preset
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            cat = body.get("category", "")
            name = body.get("name", "")
            prompt_text = body.get("prompt_text", "")
            if not cat or not name or not prompt_text:
                self._send_json({"error": "category, name, and prompt_text required"}, 400)
            else:
                add_custom_preset(cat, name, prompt_text)
                self._send_json({"ok": True})

        elif path == "/api/cinematic/shot-styles/remove":
            from lib.shot_style_library import remove_custom_preset
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            remove_custom_preset(body.get("category", ""), body.get("name", ""))
            self._send_json({"ok": True})

        elif path == "/api/cinematic/shot-styles/resolve":
            from lib.shot_style_library import resolve_presets
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            prompt = resolve_presets(body.get("selections", {}))
            self._send_json({"ok": True, "prompt": prompt})

        # ──── Generation Queue ────
        elif path == "/api/queue/add-shot":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            scene_id = body.get("scene_id", "")
            shot_id = body.get("shot_id", "")
            # Find the shot data
            settings = _load_settings()
            shots = settings.get("shots_data", {}).get(scene_id, [])
            shot_data = None
            for s in shots:
                if s.get("id") == shot_id:
                    shot_data = s
                    break
            if not shot_data:
                self._send_json({"error": "Shot not found"}, 404)
            else:
                item = _queue_add(shot_id, scene_id, shot_data)
                self._send_json({"ok": True, "queue_id": item["id"]})

        elif path == "/api/queue/add-scene":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            scene_id = body.get("scene_id", "")
            settings = _load_settings()
            shots = settings.get("shots_data", {}).get(scene_id, [])
            if not shots:
                self._send_json({"error": "No shots in scene"}, 400)
            else:
                ids = []
                for s in shots:
                    item = _queue_add(s["id"], scene_id, s)
                    ids.append(item["id"])
                self._send_json({"ok": True, "queued": len(ids)})

        elif path == "/api/queue/add-all":
            settings = _load_settings()
            all_shots = settings.get("shots_data", {})
            count = 0
            for scene_id, shots in all_shots.items():
                for s in shots:
                    _queue_add(s["id"], scene_id, s)
                    count += 1
            self._send_json({"ok": True, "queued": count})

        elif path == "/api/queue/retry":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            queue_id = body.get("queue_id", "")
            with gen_queue_lock:
                for it in gen_queue["items"]:
                    if it["id"] == queue_id and it["status"] == "failed":
                        it["status"] = "pending"
                        it["error"] = None
                        it["progress"] = ""
                        break
            _queue_process()
            self._send_json({"ok": True})

        elif path == "/api/queue/clear":
            with gen_queue_lock:
                gen_queue["items"] = [i for i in gen_queue["items"] if i["status"] == "generating"]
            self._send_json({"ok": True})

        # ──── Style Memory ────
        elif path == "/api/cinematic/style-memory":
            from lib.cinematic_engine import StyleMemory
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            sm = StyleMemory()
            result = sm.update(body)
            self._send_json({"ok": True, "style_memory": result})

        elif path == "/api/cinematic/style-memory/from-vision":
            from lib.cinematic_engine import StyleMemory
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            sm = StyleMemory()
            result = sm.set_from_vision(
                universal_prompt=body.get("universal_prompt", ""),
                world_setting=body.get("world_setting", ""),
                style=body.get("style", ""),
            )
            self._send_json({"ok": True, "style_memory": result})

        elif path == "/api/cinematic/style-memory/learn":
            from lib.cinematic_engine import StyleMemory
            sm = StyleMemory()
            settings = _load_settings()
            all_shots = []
            for sid, shots in settings.get("shots_data", {}).items():
                all_shots.extend(shots)
            result = sm.learn_from_shots(all_shots)
            self._send_json({"ok": True, "style_memory": result})

        # ──── Continuity Fix ────
        elif path.startswith("/api/cinematic/fix-continuity/"):
            scene_id = path.split("/")[-1]
            from lib.cinematic_engine import fix_scene_continuity
            settings = _load_settings()
            shots = settings.get("shots_data", {}).get(scene_id, [])
            if not shots:
                self._send_json({"error": "No shots found for this scene"}, 404)
            else:
                scene_data = _prompt_os.get_scene(scene_id) if scene_id else None
                fixed = fix_scene_continuity(shots, scene_data)
                settings["shots_data"][scene_id] = fixed
                _save_settings(settings)
                from lib.cinematic_engine import validate_scene_continuity
                result = validate_scene_continuity(fixed, scene_data)
                self._send_json({"ok": True, "fixed": len(fixed), **result})

        # ──── AI Director: Generate Full Video Plan (scenes + shots) ────
        elif path == "/api/ai-director/full-plan":
            self._handle_ai_director_full_plan()

        # ──── Prompt OS POST routes ────
        elif path == "/api/pos/prompts":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.create_prompt(body)
            self._send_json({"ok": True, "prompt": rec})

        elif path == "/api/pos/characters":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.create_character(body)
            self._send_json({"ok": True, "character": rec})

        elif path == "/api/pos/costumes":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.create_costume(body)
            self._send_json({"ok": True, "costume": rec})

        elif path == "/api/pos/environments":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.create_environment(body)
            self._send_json({"ok": True, "environment": rec})

        elif path == "/api/pos/references" or path == "/api/pos/props":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.create_reference(body)
            self._send_json({"ok": True, "reference": rec, "prop": rec})

        elif path == "/api/pos/scenes":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.create_scene(body)
            self._send_json({"ok": True, "scene": rec})

        elif path == "/api/pos/voices":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.create_voice(body)
            self._send_json({"ok": True, "voice": rec})

        elif re.match(r'^/api/pos/scenes/([^/]+)/export/text$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)/export/text$', path)
            text = _prompt_os.export_scene_text(m.group(1))
            self._send_json({"ok": True, "text": text})

        elif re.match(r'^/api/pos/scenes/([^/]+)/export/json$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)/export/json$', path)
            data = _prompt_os.export_scene_json(m.group(1))
            self._send_json({"ok": True, "data": data})

        elif path == "/api/pos/style-locks":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            locks = _prompt_os.set_style_locks(body.get("styleLocks", []))
            self._send_json({"ok": True, "styleLocks": locks})

        elif path == "/api/pos/world-rules":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rules = _prompt_os.set_world_rules(body.get("worldRules", []))
            self._send_json({"ok": True, "worldRules": rules})

        # ──── Prompt OS Photo Upload routes ────
        elif re.match(r'^/api/pos/characters/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)/photo$', path)
            self._handle_pos_photo_upload(m.group(1), "characters")

        elif re.match(r'^/api/pos/costumes/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)/photo$', path)
            self._handle_pos_photo_upload(m.group(1), "costumes")

        elif re.match(r'^/api/pos/environments/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)/photo$', path)
            self._handle_pos_photo_upload(m.group(1), "environments")

        elif re.match(r'^/api/pos/voices/([^/]+)/sample$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)/sample$', path)
            self._handle_pos_voice_sample_upload(m.group(1))

        # ──── Auto-describe from photo ────
        elif re.match(r'^/api/pos/characters/([^/]+)/describe$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)/describe$', path)
            self._handle_pos_auto_describe(m.group(1), "characters")

        elif re.match(r'^/api/pos/environments/([^/]+)/describe$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)/describe$', path)
            self._handle_pos_auto_describe(m.group(1), "environments")

        elif re.match(r'^/api/pos/costumes/([^/]+)/describe$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)/describe$', path)
            self._handle_pos_auto_describe(m.group(1), "costumes")

        # ──── Prompt OS Preview Generation routes ────
        elif re.match(r'^/api/pos/characters/([^/]+)/generate-preview$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)/generate-preview$', path)
            self._handle_pos_generate_preview(m.group(1), "characters")

        elif re.match(r'^/api/pos/costumes/([^/]+)/generate-preview$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)/generate-preview$', path)
            self._handle_pos_generate_preview(m.group(1), "costumes")

        elif re.match(r'^/api/pos/environments/([^/]+)/generate-preview$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)/generate-preview$', path)
            self._handle_pos_generate_preview(m.group(1), "environments")

        # ──── Character Sheet Generation ────
        elif re.match(r'^/api/pos/characters/([^/]+)/generate-sheet$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)/generate-sheet$', path)
            self._handle_pos_generate_character_sheet(m.group(1))

        # ──── Unified POS Sheet Generation (fal.ai Gemini) ────
        elif path == "/api/pos/sheets/generate":
            self._handle_pos_sheet_generate_fal()

        # ──── Style Transfer (convert ref photo to target art style) ────
        elif re.match(r'^/api/pos/characters/([^/]+)/style-transfer$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)/style-transfer$', path)
            self._handle_style_transfer(m.group(1))

        # ──── Sheet Approval ────
        elif path == "/api/pos/sheets/approve":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            asset_type = (body.get("assetType") or "").lower()
            asset_id = body.get("assetId") or ""
            sheet_url = body.get("sheetUrl") or ""
            slot = body.get("slot") or "approvedSheet"
            is_unapprove = body.get("unapprove", False)
            if not asset_type or not asset_id:
                self._send_json({"error": "assetType and assetId required"}, 400)
                return
            if not is_unapprove and not sheet_url:
                self._send_json({"error": "sheetUrl required (or set unapprove: true)"}, 400)
                return
            updaters = {
                "character":   _prompt_os.update_character,
                "costume":     _prompt_os.update_costume,
                "environment": _prompt_os.update_environment,
                "reference":   _prompt_os.update_reference,
                "prop":        _prompt_os.update_reference,  # legacy alias
            }
            updater = updaters.get(asset_type)
            if not updater:
                self._send_json({"error": f"Unknown assetType: {asset_type}"}, 400)
                return
            if is_unapprove:
                updates = {slot: "", "approvalState": "generated"}
                action = "unapproved"
            else:
                updates = {slot: sheet_url}
                if slot == "approvedSheet":
                    updates["approvalState"] = "approved"
                    updates["previewImage"] = sheet_url
                action = "approved"
            result = updater(asset_id, updates)
            if not result:
                self._send_json({"error": f"{asset_type} {asset_id} not found"}, 404)
                return
            print(f"[SHEET_APPROVE] {asset_type}/{asset_id} → {action} {slot}")
            self._send_json({"ok": True, "slot": slot, "action": action})

        elif path == "/api/pos/sheets/delete":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            asset_type = (body.get("assetType") or "").lower()
            asset_id = body.get("assetId") or ""
            sheet_url = body.get("sheetUrl") or ""
            delete_file = bool(body.get("deleteFile", True))
            if not asset_type or not asset_id or not sheet_url:
                self._send_json({"error": "assetType, assetId, sheetUrl required"}, 400)
                return
            result = _prompt_os.remove_sheet_image(asset_type, asset_id, sheet_url)
            if isinstance(result, dict) and result.get("error"):
                self._send_json(result, 404)
                return
            removed_file = False
            removed_companions = []
            if delete_file:
                base = os.path.dirname(os.path.abspath(__file__))
                for candidate in (
                    os.path.join(base, "public", sheet_url.lstrip("/")),
                    os.path.join(base, sheet_url.lstrip("/")),
                ):
                    if os.path.isfile(candidate):
                        try:
                            os.remove(candidate)
                            removed_file = True
                            stem, _ = os.path.splitext(candidate)
                            for comp in (stem + "_preview.jpg", stem + "_preview.png", stem + ".jpg"):
                                if os.path.isfile(comp):
                                    try:
                                        os.remove(comp)
                                        removed_companions.append(os.path.basename(comp))
                                    except OSError as ce:
                                        print(f"[SHEET_DELETE] could not remove companion {comp}: {ce}")
                            break
                        except OSError as e:
                            print(f"[SHEET_DELETE] could not remove {candidate}: {e}")
            print(f"[SHEET_DELETE] {asset_type}/{asset_id} → removed {sheet_url} (file={removed_file}, companions={removed_companions})")
            self._send_json({"ok": True, "removed_file": removed_file, "removed_companions": removed_companions})

        elif path == "/api/pos/sheets/duplicate":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            asset_type = (body.get("assetType") or "").lower()
            asset_id = body.get("assetId") or ""
            sheet_url = body.get("sheetUrl") or ""
            if not asset_type or not asset_id or not sheet_url:
                self._send_json({"error": "assetType, assetId, sheetUrl required"}, 400)
                return
            server_root = os.path.dirname(os.path.abspath(__file__))
            result = _prompt_os.duplicate_sheet_image(asset_type, asset_id, sheet_url, server_root)
            if isinstance(result, dict) and result.get("error"):
                self._send_json(result, 404)
                return
            print(f"[SHEET_DUPLICATE] {asset_type}/{asset_id} → {sheet_url} copied to {result.get('new_url')}")
            self._send_json({"ok": True, "new_url": result.get("new_url"), "asset": result.get("asset")})

        # ──── Asset Lock / Unlock ────
        elif path == "/api/pos/assets/lock":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            asset_type = (body.get("assetType") or "").lower()
            asset_id = body.get("assetId") or ""
            action = body.get("action") or "lock"  # "lock" or "unlock"
            if not asset_type or not asset_id:
                self._send_json({"error": "assetType and assetId required"}, 400)
                return
            if action == "unlock":
                result = _prompt_os.unlock_asset(asset_type, asset_id)
            else:
                result = _prompt_os.lock_asset(asset_type, asset_id)
            if result and result.get("error"):
                self._send_json(result, 400)
                return
            print(f"[ASSET_LOCK] {asset_type}/{asset_id} → {action}")
            self._send_json({"ok": True, "action": action, "asset": result})

        # ──── Multi-project registry (new API) ────
        elif path == "/api/projects":
            # POST /api/projects — create a new project.
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            name = (body.get("name") or "").strip()
            slug = body.get("slug")
            if not name:
                self._send_json({"error": "name is required"}, 400)
                return
            try:
                meta = active_project.create_project(name, slug)
            except ValueError as _ve:
                msg = str(_ve)
                status = 409 if "already exists" in msg else 400
                self._send_json({"error": msg}, status)
                return
            except Exception as _e:
                self._send_json({"error": str(_e)}, 500)
                return
            self._send_json({"ok": True, "project": meta})

        elif path == "/api/projects/active":
            # POST /api/projects/active — switch active project.
            mismatch = _check_project_header(self)
            if mismatch:
                self._send_json(mismatch[1], mismatch[0])
                return
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            slug = (body.get("slug") or "").strip()
            if not slug:
                self._send_json({"error": "slug is required"}, 400)
                return
            known = {p["slug"] for p in active_project.list_projects()}
            if slug not in known:
                self._send_json({"error": f"unknown slug '{slug}'"}, 404)
                return
            try:
                active_project.set_active_slug(slug)
            except Exception as _e:
                self._send_json({"error": str(_e)}, 500)
                return
            self._send_json({"ok": True, "active": slug})

        elif re.match(r'^/api/projects/([^/]+)/rename$', path):
            mismatch = _check_project_header(self)
            if mismatch:
                self._send_json(mismatch[1], mismatch[0])
                return
            m = re.match(r'^/api/projects/([^/]+)/rename$', path)
            slug = m.group(1)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            new_name = (body.get("name") or "").strip()
            if not new_name:
                self._send_json({"error": "name is required"}, 400)
                return
            try:
                meta = active_project.rename_project(slug, new_name)
            except ValueError as _ve:
                self._send_json({"error": str(_ve)}, 404)
                return
            except Exception as _e:
                self._send_json({"error": str(_e)}, 500)
                return
            self._send_json({"ok": True, "project": meta})

        elif re.match(r'^/api/projects/([^/]+)/snapshot$', path):
            mismatch = _check_project_header(self)
            if mismatch:
                self._send_json(mismatch[1], mismatch[0])
                return
            m = re.match(r'^/api/projects/([^/]+)/snapshot$', path)
            slug = m.group(1)
            try:
                counts = active_project.snapshot_project_to_vault(slug)
            except ValueError as _ve:
                self._send_json({"error": str(_ve)}, 404)
                return
            except Exception as _e:
                self._send_json({"error": str(_e)}, 500)
                return
            self._send_json({"ok": True, "counts": counts})

        # ──── Legacy project endpoints (retired) ────
        # /api/projects/<id>/load and /api/projects/<id>/save were the old
        # copy-in/copy-out workspace model. Under the new architecture every
        # project is always live on disk — switching is just set_active_slug.
        # We keep the paths here to return a helpful 410 so callers update.
        elif re.match(r'^/api/projects/([^/]+)/load$', path):
            self._send_json({
                "error": "endpoint retired",
                "hint": "POST /api/projects/active with {\"slug\": \"...\"} instead",
            }, 410)

        elif re.match(r'^/api/projects/([^/]+)/save$', path):
            self._send_json({
                "error": "endpoint retired",
                "hint": "projects are always live on disk; use /api/projects/<slug>/snapshot to export to the vault",
            }, 410)

        # ──── Feature 2+10: Batch Generation Queue ────
        elif path == "/api/manual/generate-queue":
            with batch_lock:
                if batch_queue_state["active"]:
                    self._send_json({"error": "Batch already running"}, 409)
                    return
            thread = threading.Thread(target=_run_batch_generate_queue, daemon=True)
            thread.start()
            self._send_json({"ok": True, "message": "Batch queue started"})

        elif path == "/api/manual/cancel-queue":
            with batch_lock:
                batch_queue_state["cancelled"] = True
            self._send_json({"ok": True})

        # ──── Feature 3: Audio Waveform ────
        elif path == "/api/audio/waveform":
            plan = _load_manual_plan()
            audio_path = plan.get("song_path", "")
            if not audio_path or not os.path.isfile(audio_path):
                self._send_json({"error": "No audio uploaded"}, 404)
            else:
                data = _generate_waveform(audio_path)
                # Also get beats from analysis if available
                beats = []
                try:
                    analysis = analyze(audio_path)
                    total_dur = analysis.get("duration", 0)
                    beats = analysis.get("beats", [])
                except Exception:
                    total_dur = 0
                self._send_json({"ok": True, "waveform": data,
                                 "beats": beats, "duration": total_dur})

        # ──── Feature 4: Select Take ────
        elif re.match(r'^/api/manual/scene/([^/]+)/select-take$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/select-take$', path)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            scene_id = m.group(1)
            take_num = body.get("take_num", 0)
            takes = _get_takes(scene_id)
            selected = next((t for t in takes if t["take_num"] == take_num), None)
            if not selected or not selected.get("exists"):
                self._send_json({"error": "Take not found"}, 404)
            else:
                import shutil as _shutil
                plan = _load_manual_plan()
                for s in plan["scenes"]:
                    if s["id"] == scene_id:
                        s["clip_path"] = selected["clip_path"]
                        s["has_clip"] = True
                        _save_manual_plan(plan)
                        self._send_json({"ok": True})
                        return
                self._send_json({"error": "Scene not found"}, 404)

        # ──── Feature 5: Quick Preview ────
        elif re.match(r'^/api/manual/scene/([^/]+)/quick-preview$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/quick-preview$', path)
            self._handle_quick_preview(m.group(1))

        # ──── Feature 8: Budget update ────
        elif path == "/api/cost/budget":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            budget = float(body.get("budget", DEFAULT_BUDGET))
            tracker = _load_cost_tracker()
            tracker["budget"] = budget
            _save_cost_tracker(tracker)
            self._send_json({"ok": True, "budget": budget})

        # Style mixing
        elif path == "/api/mix-styles":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            from lib.prompt_assistant import mix_styles, STYLE_PRESETS
            sa = body.get("style_a", "")
            sb = body.get("style_b", "")
            w = float(body.get("weight", 0.5))
            if sa in STYLE_PRESETS: sa = STYLE_PRESETS[sa]
            if sb in STYLE_PRESETS: sb = STYLE_PRESETS[sb]
            self._send_json({"ok": True, "mixed_style": mix_styles(sa, sb, w)})

        # Emotion detection
        elif path == "/api/detect-emotion":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            from lib.prompt_assistant import detect_emotion, emotion_to_visual_prompt
            emotions = detect_emotion(body.get("lyrics", ""))
            self._send_json({"ok": True, "emotions": emotions, "visual_prompt": emotion_to_visual_prompt(emotions)})

        # ──── Preview-first pipeline ────

        elif path == "/api/preview-thumbnail":
            self._handle_preview_thumbnail_single()

        elif path == "/api/preview-thumbnail/batch":
            self._handle_preview_thumbnail_batch()

        elif re.match(r'^/api/scenes/(\d+)/approve$', path):
            m = re.match(r'^/api/scenes/(\d+)/approve$', path)
            self._handle_scene_approve(int(m.group(1)))

        elif path == "/api/generate-approved":
            self._handle_generate_approved()

        elif path == "/api/generate/cancel":
            with gen_lock:
                was_running = gen_state.get("running", False)
                gen_state["cancel_requested"] = True
            self._send_json({"ok": True, "was_running": was_running})

        # ---- Keyframe endpoints (POST) ----
        elif re.match(r'^/api/scenes/(\d+)/keyframes$', path):
            m = re.match(r'^/api/scenes/(\d+)/keyframes$', path)
            self._handle_set_keyframes(int(m.group(1)))

        elif re.match(r'^/api/scenes/(\d+)/keyframes/from-thumbnail$', path):
            m = re.match(r'^/api/scenes/(\d+)/keyframes/from-thumbnail$', path)
            self._handle_keyframe_from_thumbnail(int(m.group(1)))

        elif re.match(r'^/api/scenes/(\d+)/keyframes/from-previous$', path):
            m = re.match(r'^/api/scenes/(\d+)/keyframes/from-previous$', path)
            self._handle_keyframe_from_previous(int(m.group(1)))

        elif re.match(r'^/api/scenes/(\d+)/keyframes/clear$', path):
            m = re.match(r'^/api/scenes/(\d+)/keyframes/clear$', path)
            self._handle_clear_keyframe(int(m.group(1)))

        elif path == "/api/scenes/auto-chain":
            self._handle_auto_chain()

        # ---- Manual scene keyframe endpoints ----
        elif re.match(r'^/api/manual/scene/([^/]+)/keyframes$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/keyframes$', path)
            self._handle_manual_set_keyframes(m.group(1))

        elif re.match(r'^/api/manual/scene/([^/]+)/keyframes/from-thumbnail$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/keyframes/from-thumbnail$', path)
            self._handle_manual_keyframe_from_thumbnail(m.group(1))

        elif re.match(r'^/api/manual/scene/([^/]+)/keyframes/from-previous$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/keyframes/from-previous$', path)
            self._handle_manual_keyframe_from_previous(m.group(1))

        elif re.match(r'^/api/manual/scene/([^/]+)/keyframes/clear$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/keyframes/clear$', path)
            self._handle_manual_clear_keyframe(m.group(1))

        elif path == "/api/manual/scenes/auto-chain":
            self._handle_manual_auto_chain()

        # ---- Director Brain (POST) ----
        elif path == "/api/director-brain/recommend":
            from lib.director_brain import get_brain
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            brain = get_brain()
            rec = brain.recommend_for_scene(body)
            self._send_json(rec)

        elif path == "/api/director-brain/rate":
            from lib.director_brain import get_brain
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            brain = get_brain()
            brain.rate_scene(body.get("scene_index", 0), body.get("rating", 3), body)
            self._send_json({"ok": True})

        # ---- AutoAgent (POST) ----
        elif path == "/api/autoagent/start":
            from lib.auto_agent import get_or_create_run
            run = get_or_create_run()
            def _dummy_eval(harness):
                return []
            run.start(_dummy_eval, max_iterations=body.get("iterations", 10) if 'body' in dir() else 10)
            self._send_json({"ok": True, "message": "AutoAgent started"})

        elif path == "/api/autoagent/stop":
            from lib.auto_agent import get_current_run
            run = get_current_run()
            if run:
                run.stop()
            self._send_json({"ok": True})

        elif path == "/api/autoagent/harness":
            from lib.auto_agent import HarnessManager
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            hm = HarnessManager()
            if body.get("key_path") and body.get("value") is not None:
                hm.apply_edit(body["key_path"], body["value"])
                hm.save()
            self._send_json({"ok": True})

        elif path == "/api/autoagent/harness/revert":
            from lib.auto_agent import HarnessManager
            hm = HarnessManager()
            hm.revert_to_best()
            self._send_json({"ok": True})

        # ---- POS: Continuity Rules (POST) ----
        elif path == "/api/pos/continuity-rules":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rules_path = os.path.join(_prompt_os_data_dir(), "continuity_rules.json")
            rules = []
            if os.path.isfile(rules_path):
                with open(rules_path, "r") as f:
                    rules = json.load(f)
            rules.append({"rule": body.get("rule", ""), "created": time.time()})
            with open(rules_path, "w") as f:
                json.dump(rules, f)
            self._send_json({"ok": True})

        # ---- POS: Project Style (POST) ----
        elif path == "/api/pos/project-style":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            style_path = os.path.join(_prompt_os_data_dir(), "project_style.json")
            with open(style_path, "w") as f:
                json.dump(body, f)
            self._send_json({"ok": True})

        # ---- Audio Stems (POST) ----
        elif path == "/api/audio/stems":
            stems_dir = os.path.join(OUTPUT_DIR, "audio")
            stems = {}
            if os.path.isdir(stems_dir):
                for f in os.listdir(stems_dir):
                    if f.endswith(('.mp3', '.wav', '.m4a', '.ogg')):
                        stems[f] = f"/output/audio/{f}"
            self._send_json({"stems": stems})

        else:
            self.send_error(404)

    def do_PUT(self):
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        # CSRF check for API routes (per-session; bearer-token callers exempt)
        if path.startswith("/api/"):
            bearer = self.headers.get("Authorization", "").replace("Bearer ", "")
            if not (bearer and hmac.compare_digest(bearer, _API_TOKEN)):
                sid = self._parse_cookie("lumn_sid") or ""
                csrf = self.headers.get("X-CSRF-Token", "")
                if not _verify_csrf(sid, csrf):
                    self._send_json({"error": "Invalid CSRF token"}, 403)
                    return
        # X-Lumn-Project header parity on project-scoped mutations.
        if _is_project_scoped_mutation(path):
            mismatch = _check_project_header(self)
            if mismatch:
                self._send_json(mismatch[1], mismatch[0])
                return

        if re.match(r'^/api/manual/scene/([^/]+)$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)$', path)
            self._handle_manual_update_scene(m.group(1))

        # ──── Prompt OS PUT routes ────
        elif re.match(r'^/api/pos/prompts/([^/]+)$', path):
            m = re.match(r'^/api/pos/prompts/([^/]+)$', path)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.update_prompt(m.group(1), body)
            if rec and "error" in rec:
                self._send_json(rec, 403)
            elif rec:
                self._send_json({"ok": True, "prompt": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/characters/([^/]+)$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)$', path)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.update_character(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "character": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/costumes/([^/]+)$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)$', path)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.update_costume(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "costume": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/environments/([^/]+)$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)$', path)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.update_environment(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "environment": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/(references|props)/([^/]+)$', path):
            m = re.match(r'^/api/pos/(references|props)/([^/]+)$', path)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.update_reference(m.group(2), body)
            if rec:
                self._send_json({"ok": True, "reference": rec, "prop": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/scenes/([^/]+)$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)$', path)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.update_scene(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "scene": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/voices/([^/]+)$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)$', path)
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            rec = _prompt_os.update_voice(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "voice": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        else:
            self.send_error(404)

    def do_DELETE(self):
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        # CSRF check for API routes (per-session; bearer-token callers exempt)
        if path.startswith("/api/"):
            bearer = self.headers.get("Authorization", "").replace("Bearer ", "")
            if not (bearer and hmac.compare_digest(bearer, _API_TOKEN)):
                sid = self._parse_cookie("lumn_sid") or ""
                csrf = self.headers.get("X-CSRF-Token", "")
                if not _verify_csrf(sid, csrf):
                    self._send_json({"error": "Invalid CSRF token"}, 403)
                    return
        # X-Lumn-Project header parity on project-scoped mutations.
        # /api/projects/<slug> (DELETE a project) is handled per-route so the
        # client can still delete inactive projects without matching the
        # active slug. _is_project_scoped_mutation() returns False for those.
        if _is_project_scoped_mutation(path):
            mismatch = _check_project_header(self)
            if mismatch:
                self._send_json(mismatch[1], mismatch[0])
                return

        if re.match(r'^/api/manual/scene/([^/]+)$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)$', path)
            self._handle_manual_delete_scene(m.group(1))
        elif path.startswith("/api/references/"):
            name = urllib.parse.unquote(path[len("/api/references/"):])
            self._handle_delete_reference(name)

        # ──── Prompt OS DELETE routes ────
        elif re.match(r'^/api/pos/prompts/([^/]+)$', path):
            m = re.match(r'^/api/pos/prompts/([^/]+)$', path)
            if _prompt_os.delete_prompt(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/characters/([^/]+)$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)$', path)
            if _prompt_os.delete_character(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/costumes/([^/]+)$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)$', path)
            if _prompt_os.delete_costume(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/environments/([^/]+)$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)$', path)
            if _prompt_os.delete_environment(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/(references|props)/([^/]+)$', path):
            m = re.match(r'^/api/pos/(references|props)/([^/]+)$', path)
            if _prompt_os.delete_reference(m.group(2)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/scenes/([^/]+)$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)$', path)
            if _prompt_os.delete_scene(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/voices/([^/]+)$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)$', path)
            if _prompt_os.delete_voice(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        # ──── Multi-project registry: DELETE a project ────
        elif re.match(r'^/api/projects/([^/]+)$', path):
            mismatch = _check_project_header(self)
            if mismatch:
                self._send_json(mismatch[1], mismatch[0])
                return
            m = re.match(r'^/api/projects/([^/]+)$', path)
            slug = m.group(1)
            try:
                active_project.delete_project(slug)
            except ValueError as _ve:
                msg = str(_ve)
                # Active slug / default slug / missing slug are all user-fixable.
                status = 400 if ("active" in msg or "default" in msg) else 404
                self._send_json({"error": msg}, status)
                return
            except Exception as _e:
                self._send_json({"error": str(_e)}, 500)
                return
            self._send_json({"ok": True})

        # ──── Delete Continuity Rule ────
        elif re.match(r'^/api/pos/continuity-rules/(\d+)$', path):
            m = re.match(r'^/api/pos/continuity-rules/(\d+)$', path)
            idx = int(m.group(1))
            rules_path = os.path.join(_prompt_os_data_dir(), "continuity_rules.json")
            rules = []
            if os.path.isfile(rules_path):
                with open(rules_path, "r") as f:
                    rules = json.load(f)
            if 0 <= idx < len(rules):
                rules.pop(idx)
                with open(rules_path, "w") as f:
                    json.dump(rules, f)
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Index out of range"}, 404)

        else:
            self.send_error(404)

    # ---- Upload handlers ----

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
                # Validate file extension
                ALLOWED_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.mp4', '.webm', '.mov', '.avi', '.jpg', '.jpeg', '.png', '.webp', '.gif', '.zip'}
                ext = os.path.splitext(filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    self._send_json({"error": f"File type {ext} not allowed"}, 400)
                    return
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

        # F2: kick off music-grid analysis in background for audio uploads so
        # timing.json is ready by the time the scene planner asks for it.
        # Lyrics are skipped here (Whisper costs $) — user can re-run via
        # POST /api/v6/song/analyze { include_lyrics: true } when desired.
        audio_exts = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}
        analysis_queued = False
        if os.path.splitext(filename)[1].lower() in audio_exts:
            try:
                import threading
                def _bg_analyze(song_path: str):
                    try:
                        from lib.song_timing import analyze_song, save_timing
                    except Exception as e:
                        print(f"[upload-analyze] import failed: {e}", flush=True)
                        return
                    try:
                        timing = analyze_song(song_path, include_lyrics=False)
                    except Exception as e:
                        print(f"[upload-analyze] analyze_song failed: {e}", flush=True)
                        return
                    try:
                        project_dir = os.path.join(OUTPUT_DIR, "projects", "default")
                        out_path = save_timing(project_dir, timing)
                        bpm = timing.get("tempo", {}).get("bpm")
                        dbs = len(timing.get("downbeats", []))
                        print(f"[upload-analyze] timing saved -> {out_path} "
                              f"(bpm={bpm}, downbeats={dbs})", flush=True)
                    except Exception as e:
                        print(f"[upload-analyze] save failed: {e}", flush=True)
                t = threading.Thread(target=_bg_analyze, args=(dest,), daemon=True)
                t.start()
                analysis_queued = True
            except Exception as e:
                print(f"[upload-analyze] spawn failed: {e}", flush=True)

        self._send_json({
            "ok": True,
            "filename": filename,
            "size": len(file_data),
            "analysis_queued": analysis_queued,
        })

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

        # SECURITY (H1): moderate user-supplied style text before it flows
        # into scene plans and fal.ai. fail-closed on exception.
        try:
            from lib.moderation import moderate_prompt_strict
            _mod = moderate_prompt_strict(style or "", nsfw_allowed=False)
            if not _mod["allowed"]:
                return self._send_json({
                    "error": "moderation_blocked",
                    "severity": _mod["severity"],
                    "reasons": _mod["reasons"],
                }, 451)
            if _mod["severity"] == "warn":
                style = _mod["redacted_prompt"]
        except Exception as _e:
            return self._send_json({"error": "moderation unavailable"}, 500)

        if not filename:
            self._send_json({"error": "No filename specified"}, 400)
            return

        song_path = os.path.join(UPLOADS_DIR, os.path.basename(filename))
        if not os.path.isfile(song_path):
            self._send_json({"error": f"Song file not found: {filename}"}, 404)
            return

        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"
            gen_state["song_path"] = song_path

        thread = threading.Thread(
            target=_run_generation,
            args=(song_path, style),
            daemon=True,
        )
        thread.start()

        self._send_json({"ok": True, "message": "Generation started"})

    # ---- Preview-first pipeline endpoints ----

    def _handle_preview_thumbnail_single(self):
        """POST /api/preview-thumbnail — generate thumbnail for one scene.

        Body: { "index": 0, "prompt": "...", "notes": "..." }
        Returns: { "ok": true, "preview_url": "..." }
        """
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        index = body.get("index")
        prompt = body.get("prompt", "").strip()
        notes = body.get("notes", "").strip()

        if index is None:
            self._send_json({"error": "Missing 'index'"}, 400)
            return
        if not prompt:
            self._send_json({"error": "Missing 'prompt'"}, 400)
            return

        result = _generate_scene_thumbnail(int(index), prompt, notes)
        if "error" in result:
            self._send_json({"error": result["error"]}, 500)
            return

        # Persist thumbnail URL into scene plan
        _update_scene_plan_thumbnail(int(index), result["preview_url"])
        self._send_json({"ok": True, "preview_url": result["preview_url"], "index": index})

    def _handle_preview_thumbnail_batch(self):
        """POST /api/preview-thumbnail/batch — generate thumbnails for all scenes.

        Body: { "scenes": [{"index": 0, "prompt": "...", "notes": "..."}, ...] }
        OR omit body to use the current scene plan automatically.
        Returns immediately with { "ok": true, "total": N }.
        Poll GET /api/preview-thumbnail/status for progress.
        """
        try:
            body = json.loads(self._read_body()) if self.headers.get("Content-Length", "0") != "0" else {}
        except (json.JSONDecodeError, ValueError):
            body = {}

        scenes_data = body.get("scenes")
        if not scenes_data:
            # Auto-load from current scene plan
            plan = _load_scene_plan()
            if not plan or not plan.get("scenes"):
                self._send_json({"error": "No scene plan available and no scenes provided"}, 404)
                return
            scenes_data = [
                {"index": i, "prompt": s.get("prompt", ""), "notes": s.get("preview_notes", ""),
                 "scene_data": s}
                for i, s in enumerate(plan["scenes"])
            ]

        with preview_lock:
            if preview_state["running"]:
                self._send_json({"error": "Batch preview already running"}, 409)
                return

        thread = threading.Thread(
            target=_run_preview_batch,
            args=(scenes_data,),
            daemon=True,
        )
        thread.start()
        self._send_json({"ok": True, "total": len(scenes_data)})

    def _handle_scene_approve(self, index: int):
        """POST /api/scenes/<index>/approve — set approval status for a scene.

        Body: { "approved": true/false, "notes": "optional director notes" }
        Returns: { "ok": true }
        """
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        approved = bool(body.get("approved", False))
        notes = body.get("notes", "").strip()

        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan found"}, 404)
            return
        if index < 0 or index >= len(plan["scenes"]):
            self._send_json({"error": f"Scene index {index} out of range"}, 400)
            return

        plan["scenes"][index]["preview_approved"] = approved
        if notes:
            plan["scenes"][index]["preview_notes"] = notes
        elif "preview_notes" in plan["scenes"][index] and not notes:
            # Clear notes if explicitly sent empty
            if "notes" in body:
                plan["scenes"][index]["preview_notes"] = ""

        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)

        self._send_json({"ok": True, "index": index, "approved": approved})

    def _handle_generate_approved(self):
        """POST /api/generate-approved — generate video only for approved scenes.

        Requires an existing scene plan (from /api/generate which goes through
        planning phase). Skips scenes where preview_approved == False.
        """
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return

        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan found. Run planning first."}, 404)
            return

        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(target=_run_generation_approved, daemon=True)
        thread.start()
        self._send_json({"ok": True, "message": "Generating approved scenes"})

    # ---- Scene Editor endpoints ----

    def _handle_get_scenes(self):
        """Return the scene plan with clip URLs."""
        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan available"}, 404)
            return

        scenes_out = []
        for s in plan["scenes"]:
            entry = dict(s)
            clip_path = s.get("clip_path")
            if clip_path and os.path.isfile(clip_path):
                entry["clip_url"] = f"/api/clips/{os.path.basename(clip_path)}"
                entry["clip_exists"] = True
            else:
                entry["clip_url"] = None
                entry["clip_exists"] = False
            # Include keyframe URLs
            ff = s.get("first_frame_path")
            if ff and os.path.isfile(ff):
                entry["first_frame_url"] = f"/api/keyframes/{os.path.basename(ff)}"
            else:
                entry["first_frame_url"] = None
            lf = s.get("last_frame_path")
            if lf and os.path.isfile(lf):
                entry["last_frame_url"] = f"/api/keyframes/{os.path.basename(lf)}"
            else:
                entry["last_frame_url"] = None
            scenes_out.append(entry)

        self._send_json({"scenes": scenes_out, "song_path": plan.get("song_path", "")})

    def _handle_regen_scene(self, index: int):
        """Regenerate a single scene."""
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

        new_prompt = params.get("prompt", "")
        if not new_prompt:
            self._send_json({"error": "No prompt provided"}, 400)
            return

        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(
            target=_run_regen,
            args=(index, new_prompt),
            daemon=True,
        )
        thread.start()

        self._send_json({"ok": True, "message": f"Regenerating scene {index}"})

    def _handle_restitch(self):
        """Re-stitch all scenes into final video."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(target=_run_restitch, daemon=True)
        thread.start()

        self._send_json({"ok": True, "message": "Re-stitching video"})

    # ---- Keyframe endpoints ----

    def _handle_get_keyframes(self, index: int):
        """GET /api/scenes/<index>/keyframes -- return current keyframe URLs for a scene."""
        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan available"}, 404)
            return
        scenes = plan.get("scenes", [])
        if index < 0 or index >= len(scenes):
            self._send_json({"error": "Scene index out of range"}, 400)
            return
        scene = scenes[index]
        result = {"index": index, "first_frame": None, "last_frame": None}
        ff = scene.get("first_frame_path")
        if ff and os.path.isfile(ff):
            result["first_frame"] = f"/api/keyframes/{os.path.basename(ff)}"
        lf = scene.get("last_frame_path")
        if lf and os.path.isfile(lf):
            result["last_frame"] = f"/api/keyframes/{os.path.basename(lf)}"
        self._send_json(result)

    def _handle_set_keyframes(self, index: int):
        """POST /api/scenes/<index>/keyframes -- upload first/last frame images."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return
        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan available"}, 404)
            return
        scenes = plan.get("scenes", [])
        if index < 0 or index >= len(scenes):
            self._send_json({"error": "Scene index out of range"}, 400)
            return

        result = {"index": index, "first_frame": None, "last_frame": None}
        for part in parts:
            if part["name"] == "first_frame" and part["data"]:
                ext = os.path.splitext(part.get("filename", "") or ".png")[1] or ".png"
                dest = os.path.join(KEYFRAMES_DIR, f"scene_{index}_first{ext}")
                with open(dest, "wb") as f:
                    f.write(part["data"])
                plan["scenes"][index]["first_frame_path"] = dest
                result["first_frame"] = f"/api/keyframes/{os.path.basename(dest)}"
            elif part["name"] == "last_frame" and part["data"]:
                ext = os.path.splitext(part.get("filename", "") or ".png")[1] or ".png"
                dest = os.path.join(KEYFRAMES_DIR, f"scene_{index}_last{ext}")
                with open(dest, "wb") as f:
                    f.write(part["data"])
                plan["scenes"][index]["last_frame_path"] = dest
                result["last_frame"] = f"/api/keyframes/{os.path.basename(dest)}"

        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
        self._send_json({"ok": True, **result})

    def _handle_keyframe_from_thumbnail(self, index: int):
        """POST /api/scenes/<index>/keyframes/from-thumbnail -- use scene thumbnail as first frame."""
        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan available"}, 404)
            return
        scenes = plan.get("scenes", [])
        if index < 0 or index >= len(scenes):
            self._send_json({"error": "Scene index out of range"}, 400)
            return

        scene = scenes[index]
        # Look for thumbnail in previews dir
        thumb_url = scene.get("preview_url", "")
        thumb_path = None
        if thumb_url:
            # preview_url is like /output/previews/scene_0_thumb.png
            fname = os.path.basename(thumb_url.split("?")[0])
            candidate = os.path.join(PREVIEWS_DIR, fname)
            if os.path.isfile(candidate):
                thumb_path = candidate
        if not thumb_path:
            # Try common naming patterns
            for ext in (".png", ".jpg", ".jpeg"):
                candidate = os.path.join(PREVIEWS_DIR, f"scene_{index}_thumb{ext}")
                if os.path.isfile(candidate):
                    thumb_path = candidate
                    break
        if not thumb_path:
            self._send_json({"error": "No thumbnail found for this scene"}, 404)
            return

        import shutil
        dest = os.path.join(KEYFRAMES_DIR, f"scene_{index}_first.png")
        shutil.copy2(thumb_path, dest)
        plan["scenes"][index]["first_frame_path"] = dest
        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
        self._send_json({
            "ok": True,
            "first_frame": f"/api/keyframes/{os.path.basename(dest)}",
        })

    def _handle_keyframe_from_previous(self, index: int):
        """POST /api/scenes/<index>/keyframes/from-previous -- extract last frame from previous clip."""
        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan available"}, 404)
            return
        scenes = plan.get("scenes", [])
        if index < 1 or index >= len(scenes):
            self._send_json({"error": "Scene index out of range or is first scene"}, 400)
            return

        prev_clip = scenes[index - 1].get("clip_path")
        if not prev_clip or not os.path.isfile(prev_clip):
            self._send_json({"error": "Previous scene has no generated clip"}, 404)
            return

        dest = os.path.join(KEYFRAMES_DIR, f"scene_{index}_first.png")
        try:
            extract_last_frame(prev_clip, dest)
        except Exception as e:
            self._send_json({"error": f"Failed to extract last frame: {e}"}, 500)
            return

        plan["scenes"][index]["first_frame_path"] = dest
        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
        self._send_json({
            "ok": True,
            "first_frame": f"/api/keyframes/{os.path.basename(dest)}",
        })

    def _handle_clear_keyframe(self, index: int):
        """POST /api/scenes/<index>/keyframes/clear -- clear first or last keyframe."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            params = {}
        position = params.get("position", "both")  # "first", "last", or "both"

        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan available"}, 404)
            return
        scenes = plan.get("scenes", [])
        if index < 0 or index >= len(scenes):
            self._send_json({"error": "Scene index out of range"}, 400)
            return

        if position in ("first", "both"):
            plan["scenes"][index].pop("first_frame_path", None)
        if position in ("last", "both"):
            plan["scenes"][index].pop("last_frame_path", None)

        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
        self._send_json({"ok": True})

    def _handle_auto_chain(self):
        """POST /api/scenes/auto-chain -- auto-chain keyframes across all scenes."""
        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan available"}, 404)
            return
        plan = _chain_scene_keyframes(plan)
        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
        # Count how many scenes got chained
        chained = sum(1 for s in plan["scenes"] if s.get("first_frame_path"))
        self._send_json({"ok": True, "chained_scenes": chained})

    # ---- Manual scene keyframe handlers ----

    def _handle_manual_set_keyframes(self, scene_id: str):
        """POST /api/manual/scene/<id>/keyframes -- upload first/last frame for manual scene."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return
        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        plan = _load_manual_plan()
        scene = None
        scene_idx = None
        for i, s in enumerate(plan["scenes"]):
            if s["id"] == scene_id:
                scene = s
                scene_idx = i
                break
        if scene is None:
            self._send_json({"error": "Scene not found"}, 404)
            return

        result = {"scene_id": scene_id, "first_frame": None, "last_frame": None}
        for part in parts:
            if part["name"] == "first_frame" and part["data"]:
                ext = os.path.splitext(part.get("filename", "") or ".png")[1] or ".png"
                dest = os.path.join(KEYFRAMES_DIR, f"manual_{scene_id}_first{ext}")
                with open(dest, "wb") as f:
                    f.write(part["data"])
                scene["first_frame_path"] = dest
                result["first_frame"] = f"/api/keyframes/{os.path.basename(dest)}"
            elif part["name"] == "last_frame" and part["data"]:
                ext = os.path.splitext(part.get("filename", "") or ".png")[1] or ".png"
                dest = os.path.join(KEYFRAMES_DIR, f"manual_{scene_id}_last{ext}")
                with open(dest, "wb") as f:
                    f.write(part["data"])
                scene["last_frame_path"] = dest
                result["last_frame"] = f"/api/keyframes/{os.path.basename(dest)}"

        _save_manual_plan(plan)
        self._send_json({"ok": True, **result})

    def _handle_manual_keyframe_from_thumbnail(self, scene_id: str):
        """POST /api/manual/scene/<id>/keyframes/from-thumbnail"""
        plan = _load_manual_plan()
        scene = None
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                scene = s
                break
        if scene is None:
            self._send_json({"error": "Scene not found"}, 404)
            return

        # Look for the scene's preview thumbnail
        thumb_path = None
        preview_url = scene.get("preview_url", "")
        if preview_url:
            fname = os.path.basename(preview_url.split("?")[0])
            candidate = os.path.join(PREVIEWS_DIR, fname)
            if os.path.isfile(candidate):
                thumb_path = candidate
        # Also try photo as fallback
        if not thumb_path:
            photo_path = scene.get("photo_path", "")
            if photo_path and os.path.isfile(photo_path):
                thumb_path = photo_path
        if not thumb_path:
            self._send_json({"error": "No thumbnail or photo found for this scene"}, 404)
            return

        import shutil
        dest = os.path.join(KEYFRAMES_DIR, f"manual_{scene_id}_first.png")
        shutil.copy2(thumb_path, dest)
        scene["first_frame_path"] = dest
        _save_manual_plan(plan)
        self._send_json({"ok": True, "first_frame": f"/api/keyframes/{os.path.basename(dest)}"})

    def _handle_manual_keyframe_from_previous(self, scene_id: str):
        """POST /api/manual/scene/<id>/keyframes/from-previous"""
        plan = _load_manual_plan()
        scenes = plan["scenes"]
        scene_idx = None
        for i, s in enumerate(scenes):
            if s["id"] == scene_id:
                scene_idx = i
                break
        if scene_idx is None:
            self._send_json({"error": "Scene not found"}, 404)
            return
        if scene_idx < 1:
            self._send_json({"error": "This is the first scene, no previous to chain from"}, 400)
            return

        prev_clip = scenes[scene_idx - 1].get("clip_path")
        if not prev_clip or not os.path.isfile(prev_clip):
            self._send_json({"error": "Previous scene has no generated clip"}, 404)
            return

        dest = os.path.join(KEYFRAMES_DIR, f"manual_{scene_id}_first.png")
        try:
            extract_last_frame(prev_clip, dest)
        except Exception as e:
            self._send_json({"error": f"Failed to extract last frame: {e}"}, 500)
            return

        scenes[scene_idx]["first_frame_path"] = dest
        _save_manual_plan(plan)
        self._send_json({"ok": True, "first_frame": f"/api/keyframes/{os.path.basename(dest)}"})

    def _handle_manual_clear_keyframe(self, scene_id: str):
        """POST /api/manual/scene/<id>/keyframes/clear"""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            params = {}
        position = params.get("position", "both")

        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                if position in ("first", "both"):
                    s.pop("first_frame_path", None)
                if position in ("last", "both"):
                    s.pop("last_frame_path", None)
                break
        _save_manual_plan(plan)
        self._send_json({"ok": True})

    def _handle_manual_auto_chain(self):
        """POST /api/manual/scenes/auto-chain"""
        plan = _load_manual_plan()
        scenes = plan.get("scenes", [])
        chained = 0
        for i, scene in enumerate(scenes):
            if i == 0:
                continue
            if scene.get("first_frame_path") and os.path.isfile(scene["first_frame_path"]):
                continue
            prev_clip = scenes[i - 1].get("clip_path")
            if not prev_clip or not os.path.isfile(prev_clip):
                continue
            dest = os.path.join(KEYFRAMES_DIR, f"manual_{scene['id']}_first.png")
            try:
                extract_last_frame(prev_clip, dest)
                scene["first_frame_path"] = dest
                chained += 1
            except Exception as e:
                print(f"[MANUAL-CHAIN] Scene {scene['id']}: failed: {e}")
        _save_manual_plan(plan)
        self._send_json({"ok": True, "chained_scenes": chained})

    # ---- Reference Image endpoints ----

    def _handle_upload_reference(self):
        """Upload a reference image with a name."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()

        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        ref_name = ""
        file_data = None
        file_ext = ".jpg"

        for part in parts:
            if part["name"] == "name":
                ref_name = part["data"].decode(errors="replace").strip()
            elif part["name"] == "file" or part["filename"]:
                file_data = part["data"]
                if part["filename"]:
                    file_ext = os.path.splitext(part["filename"])[1] or ".jpg"

        if not ref_name:
            self._send_json({"error": "No reference name provided"}, 400)
            return
        if file_data is None:
            self._send_json({"error": "No file found"}, 400)
            return

        # Sanitize name
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', ref_name)
        dest = os.path.join(REFERENCES_DIR, safe_name + file_ext)
        with open(dest, "wb") as f:
            f.write(file_data)

        self._send_json({"ok": True, "name": safe_name, "path": dest})

    def _handle_get_references(self):
        """List all reference images."""
        refs = _get_references()
        items = []
        for name, path in refs.items():
            items.append({
                "name": name,
                "url": f"/api/references/{urllib.parse.quote(name)}",
                "filename": os.path.basename(path),
            })
        self._send_json({"references": items})

    def _handle_get_reference_image(self, name: str):
        """Serve a reference image by name."""
        refs = _get_references()
        if name in refs:
            self._send_file(refs[name])
        else:
            self.send_error(404, f"Reference '{name}' not found")

    def _handle_delete_reference(self, name: str):
        """Delete a reference image."""
        refs = _get_references()
        if name in refs:
            os.remove(refs[name])
            self._send_json({"ok": True, "deleted": name})
        else:
            self._send_json({"error": f"Reference '{name}' not found"}, 404)

    # ---- Manual mode endpoints ----

    def _handle_manual_list_scenes(self):
        """Return all manual scenes with state."""
        plan = _load_manual_plan()
        scenes_out = []
        for s in plan["scenes"]:
            entry = dict(s)
            clip_path = s.get("clip_path", "")
            entry["clip_exists"] = bool(clip_path and os.path.isfile(clip_path))
            if entry["clip_exists"]:
                # Include file mtime in URL to bust browser cache on regeneration
                mtime = int(os.path.getmtime(clip_path))
                entry["clip_url"] = f"/api/clips/{os.path.basename(clip_path)}?v={mtime}"
            else:
                entry["clip_url"] = None
            entry["has_photo"] = bool(s.get("photo_path") and os.path.isfile(s.get("photo_path", "")))
            if entry["has_photo"]:
                entry["photo_url"] = f"/api/manual/scene-photo/{s['id']}"
            else:
                entry["photo_url"] = None
            # Multi-photo mood board URLs
            photo_paths = s.get("photo_paths", [])
            entry["photo_urls"] = []
            for pi, pp in enumerate(photo_paths):
                if pp and os.path.isfile(pp):
                    entry["photo_urls"].append(f"/api/manual/scene-photo/{s['id']}?idx={pi}")
            entry["camera_movement"] = s.get("camera_movement", "zoom_in")
            entry["engine"] = s.get("engine", "")
            # Video upload info
            video_path = s.get("video_path", "")
            entry["has_video"] = bool(video_path and os.path.isfile(video_path))
            if entry["has_video"]:
                entry["video_url"] = f"/api/manual/scene-video/{s['id']}"
            else:
                entry["video_url"] = None
            # Vocal upload info
            vocal_path = s.get("vocal_path", "")
            entry["has_vocal"] = bool(vocal_path and os.path.isfile(vocal_path))
            entry["vocal_volume"] = s.get("vocal_volume", 80)
            # Item 18: Loop/boomerang state
            entry["loop"] = s.get("loop", False)
            # Scene effects
            entry["effect"] = s.get("effect", "none")
            entry["effect_intensity"] = s.get("effect_intensity", 0.5)
            # Item 46: Previous clip for comparison
            prev_clip = s.get("previous_clip_path", "")
            entry["has_previous_clip"] = bool(prev_clip and os.path.isfile(prev_clip))
            if entry["has_previous_clip"]:
                entry["previous_clip_url"] = f"/api/clips/{os.path.basename(prev_clip)}"
            else:
                entry["previous_clip_url"] = None
            # Keyframe URLs
            ff = s.get("first_frame_path")
            if ff and os.path.isfile(ff):
                entry["first_frame_url"] = f"/api/keyframes/{os.path.basename(ff)}"
            else:
                entry["first_frame_url"] = None
            lf = s.get("last_frame_path")
            if lf and os.path.isfile(lf):
                entry["last_frame_url"] = f"/api/keyframes/{os.path.basename(lf)}"
            else:
                entry["last_frame_url"] = None
            scenes_out.append(entry)
        self._send_json({
            "scenes": scenes_out,
            "song_path": plan.get("song_path"),
            "color_grade": plan.get("color_grade", "none"),
            "audio_viz": plan.get("audio_viz"),
            "style_lock": plan.get("style_lock", ""),
            "audio_crossfade": plan.get("audio_crossfade", 0.0),
            "continuity_mode": plan.get("continuity_mode", True),
            "natural_pacing": plan.get("natural_pacing", True),
        })

    def _handle_manual_create_scene(self):
        """Create a new manual scene. Accepts multipart (with photo) or JSON."""
        content_type = self.headers.get("Content-Type", "")
        plan = _load_manual_plan()
        scene_id = str(_uuid.uuid4())[:8]
        scene = {
            "id": scene_id,
            "prompt": "",
            "duration": 8,
            "transition": "crossfade",
            "speed": 1.0,
            "overlay": None,
            "color_grade": None,
            "camera_movement": "zoom_in",
            "engine": "",         # empty = use global default
            "photo_path": None,
            "photo_paths": [],  # multi-photo mood board (up to 4)
            "clip_path": None,
            "has_clip": False,
            "video_path": None,   # user-uploaded video clip
            "vocal_path": None,   # per-scene voiceover audio
            "vocal_volume": 80,   # voiceover volume 0-100
            "loop": False,        # boomerang effect
            "reversed": False,
            "effect": "none",
            "effect_intensity": 0.5,
            "characterIds": [],    # multiple characters per scene
            "characterId": None,   # single character (legacy, still supported)
            "costumeIds": [],      # multiple costumes per scene
            "costumeId": None,     # single costume (legacy)
            "environmentId": None, # environment for this scene
            "previous_clip_path": None,  # for comparison view
        }

        if "multipart/form-data" in content_type:
            parts_ct = content_type.split("boundary=")
            if len(parts_ct) < 2:
                self._send_json({"error": "No boundary"}, 400)
                return
            boundary = parts_ct[1].strip().encode()
            body = self._read_body()
            parts = self._parse_multipart(body, boundary)

            for part in parts:
                if part["name"] == "prompt":
                    scene["prompt"] = part["data"].decode(errors="replace").strip()
                elif part["name"] == "duration":
                    try:
                        scene["duration"] = int(part["data"].decode().strip())
                    except ValueError:
                        pass
                elif part["name"] == "transition":
                    scene["transition"] = part["data"].decode(errors="replace").strip()
                elif part["name"] == "photo" and part["filename"]:
                    ext = os.path.splitext(part["filename"])[1] or ".jpg"
                    photo_path = os.path.join(SCENE_PHOTOS_DIR, f"{scene_id}{ext}")
                    with open(photo_path, "wb") as f:
                        f.write(part["data"])
                    scene["photo_path"] = photo_path
        else:
            body = self._read_body()
            try:
                params = json.loads(body) if body else {}
            except json.JSONDecodeError:
                params = {}
            scene["prompt"] = params.get("prompt", "")
            scene["duration"] = params.get("duration", 5)
            scene["transition"] = params.get("transition", "crossfade")
            if "engine" in params:
                scene["engine"] = params["engine"]
            if "characterIds" in params:
                scene["characterIds"] = params["characterIds"]
            if "characterId" in params:
                scene["characterId"] = params["characterId"]
                if params["characterId"] and params["characterId"] not in scene["characterIds"]:
                    scene["characterIds"].append(params["characterId"])
            if "costumeIds" in params:
                scene["costumeIds"] = params["costumeIds"]
            if "costumeId" in params:
                scene["costumeId"] = params["costumeId"]
            if "environmentId" in params:
                scene["environmentId"] = params["environmentId"]
            if "camera_movement" in params:
                scene["camera_movement"] = params["camera_movement"]
            if "effect" in params:
                scene["effect"] = params["effect"]
            if "effect_intensity" in params:
                scene["effect_intensity"] = params["effect_intensity"]

        # SECURITY (H1): moderate user-supplied prompt before it can reach
        # fal.ai via the downstream manual-generate path. fail-closed.
        try:
            from lib.moderation import moderate_prompt_strict
            _mod = moderate_prompt_strict(scene.get("prompt", ""), nsfw_allowed=False)
            if not _mod["allowed"]:
                return self._send_json({
                    "error": "moderation_blocked",
                    "severity": _mod["severity"],
                    "reasons": _mod["reasons"],
                }, 451)
            if _mod["severity"] == "warn":
                scene["prompt"] = _mod["redacted_prompt"]
        except Exception:
            return self._send_json({"error": "moderation unavailable"}, 500)

        plan["scenes"].append(scene)
        _save_manual_plan(plan)
        self._send_json({"ok": True, "scene": scene})

    def _handle_manual_update_scene(self, scene_id: str):
        """Update a manual scene's prompt, duration, transition, speed, overlay, color_grade, or engine."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        # SECURITY (H1): moderate any updated prompt.
        if "prompt" in params:
            try:
                from lib.moderation import moderate_prompt_strict
                _mod = moderate_prompt_strict(params.get("prompt") or "", nsfw_allowed=False)
                if not _mod["allowed"]:
                    return self._send_json({
                        "error": "moderation_blocked",
                        "severity": _mod["severity"],
                        "reasons": _mod["reasons"],
                    }, 451)
                if _mod["severity"] == "warn":
                    params["prompt"] = _mod["redacted_prompt"]
            except Exception:
                return self._send_json({"error": "moderation unavailable"}, 500)

        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                if "prompt" in params:
                    s["prompt"] = params["prompt"]
                if "duration" in params:
                    s["duration"] = params["duration"]
                if "transition" in params:
                    s["transition"] = params["transition"]
                if "speed" in params:
                    s["speed"] = params["speed"]
                if "overlay" in params:
                    s["overlay"] = params["overlay"]
                if "color_grade" in params:
                    s["color_grade"] = params["color_grade"]
                if "camera_movement" in params:
                    s["camera_movement"] = params["camera_movement"]
                if "vocal_volume" in params:
                    s["vocal_volume"] = params["vocal_volume"]
                if "engine" in params:
                    engine_val = params["engine"]
                    if engine_val in SUPPORTED_ENGINES or engine_val == "":
                        s["engine"] = engine_val
                if "reversed" in params:
                    s["reversed"] = bool(params["reversed"])
                if "speed_ramp" in params:
                    ramp = params["speed_ramp"]
                    if ramp in SPEED_RAMP_TYPES:
                        s["speed_ramp"] = ramp
                if "loop" in params:
                    s["loop"] = bool(params["loop"])
                if "effect" in params:
                    s["effect"] = params["effect"]
                if "effect_intensity" in params:
                    try:
                        s["effect_intensity"] = max(0.1, min(1.0, float(params["effect_intensity"])))
                    except (ValueError, TypeError):
                        s["effect_intensity"] = 0.5
                # Prompt OS entity links — accept both array and singular forms
                if "characterIds" in params:
                    s["characterIds"] = list(params["characterIds"] or [])
                if "characterId" in params:
                    s["characterId"] = params["characterId"] or None
                    if s.get("characterIds") is None:
                        s["characterIds"] = []
                    if params["characterId"] and params["characterId"] not in s["characterIds"]:
                        s["characterIds"].append(params["characterId"])
                if "costumeIds" in params:
                    s["costumeIds"] = list(params["costumeIds"] or [])
                if "costumeId" in params:
                    s["costumeId"] = params["costumeId"] or None
                if "environmentId" in params:
                    s["environmentId"] = params["environmentId"] or None
                _save_manual_plan(plan)
                self._send_json({"ok": True, "scene": s})
                return

        self._send_json({"error": "Scene not found"}, 404)

    def _handle_manual_delete_scene(self, scene_id: str):
        """Delete a manual scene."""
        plan = _load_manual_plan()
        new_scenes = [s for s in plan["scenes"] if s["id"] != scene_id]
        if len(new_scenes) == len(plan["scenes"]):
            self._send_json({"error": "Scene not found"}, 404)
            return
        # Clean up files for the deleted scene
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                if s.get("photo_path") and os.path.isfile(s["photo_path"]):
                    os.remove(s["photo_path"])
                if s.get("clip_path") and os.path.isfile(s["clip_path"]):
                    os.remove(s["clip_path"])
                break
        plan["scenes"] = new_scenes
        _save_manual_plan(plan)
        self._send_json({"ok": True, "deleted": scene_id})

    def _handle_manual_upload_photo(self, scene_id: str):
        """Upload/replace a scene photo."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        file_data = None
        file_ext = ".jpg"
        for part in parts:
            if part["name"] == "photo" or part["filename"]:
                file_data = part["data"]
                if part["filename"]:
                    file_ext = os.path.splitext(part["filename"])[1] or ".jpg"
                break

        if file_data is None:
            self._send_json({"error": "No photo file found"}, 400)
            return

        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                # Remove old photo if exists
                if s.get("photo_path") and os.path.isfile(s["photo_path"]):
                    os.remove(s["photo_path"])
                photo_path = os.path.join(SCENE_PHOTOS_DIR, f"{scene_id}{file_ext}")
                os.makedirs(SCENE_PHOTOS_DIR, exist_ok=True)
                with open(photo_path, "wb") as f:
                    f.write(file_data)
                s["photo_path"] = photo_path
                # Feature 6: Auto-resize photo
                _auto_resize_photo(photo_path)
                _save_manual_plan(plan)
                print(f"[upload_photo] Saved photo for scene {scene_id}: {photo_path} ({len(file_data)} bytes)")
                self._send_json({"ok": True, "photo_url": f"/api/manual/scene-photo/{scene_id}"})
                return

        self._send_json({"error": "Scene not found"}, 404)

    def _handle_get_scene_photo(self, scene_id: str):
        """Serve a scene photo. Supports ?idx=N for multi-photo mood board."""
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        idx = qs.get("idx", [None])[0]

        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                if idx is not None:
                    # Multi-photo: serve from photo_paths array
                    photo_paths = s.get("photo_paths", [])
                    pi = int(idx)
                    if 0 <= pi < len(photo_paths) and os.path.isfile(photo_paths[pi]):
                        self._send_file(photo_paths[pi])
                        return
                elif s.get("photo_path") and os.path.isfile(s["photo_path"]):
                    self._send_file(s["photo_path"])
                    return
        self.send_error(404, "Photo not found")

    def _handle_manual_generate_scene(self, scene_id: str):
        """Generate video for a single manual scene."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(
            target=_run_manual_generate_scene,
            args=(scene_id,),
            daemon=True,
        )
        thread.start()
        self._send_json({"ok": True, "message": f"Generating scene {scene_id}"})

    def _handle_manual_generate_all(self):
        """Generate all manual scenes that don't have clips."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(target=_run_manual_generate_all, daemon=True)
        thread.start()
        self._send_json({"ok": True, "message": "Generating all scenes"})

    def _handle_manual_stitch(self):
        """Stitch all manual scenes into final video."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(target=_run_manual_stitch, daemon=True)
        thread.start()
        self._send_json({"ok": True, "message": "Stitching video"})

    def _handle_manual_merge_scene(self, scene_id: str):
        """Merge the clicked scene (current) with the next scene. Combines clips and prompts."""
        plan = _load_manual_plan()
        scenes = plan["scenes"]
        src_idx = None
        for i, s in enumerate(scenes):
            if s["id"] == scene_id:
                src_idx = i
                break
        if src_idx is None:
            self._send_json({"error": "Scene not found"}, 404)
            return
        if src_idx >= len(scenes) - 1:
            self._send_json({"error": "No next scene to merge with"}, 400)
            return

        current = scenes[src_idx]
        nxt = scenes[src_idx + 1]

        current_has_clip = current.get("clip_path") and os.path.isfile(current.get("clip_path", ""))
        next_has_clip = nxt.get("clip_path") and os.path.isfile(nxt.get("clip_path", ""))

        # Always merge prompts: scene1.prompt + " | " + scene2.prompt
        p1 = current.get("prompt", "")
        p2 = nxt.get("prompt", "")
        if p1 and p2:
            current["prompt"] = p1 + " | " + p2
        elif p2:
            current["prompt"] = p2

        if current_has_clip and next_has_clip:
            # Concatenate clip1 + clip2 via ffmpeg
            merged_clip = os.path.join(MANUAL_CLIPS_DIR, f"merged_{current['id']}.mp4")
            concat_list = os.path.join(MANUAL_CLIPS_DIR, f"_merge_{current['id']}.txt")
            with open(concat_list, "w") as f:
                f.write(f"file '{current['clip_path']}'\n")
                f.write(f"file '{nxt['clip_path']}'\n")
            try:
                import subprocess as _sp
                cmd = [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", concat_list,
                    "-c", "copy",
                    merged_clip,
                ]
                _kw = {}
                if sys.platform == "win32":
                    si = _sp.STARTUPINFO()
                    si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                    si.wShowWindow = 0
                    _kw["startupinfo"] = si
                _sp.run(cmd, check=True, capture_output=True, **_kw)
                current["clip_path"] = merged_clip
                current["has_clip"] = True
            except Exception as e:
                self._send_json({"error": f"Failed to merge clips: {e}"}, 500)
                return
            finally:
                if os.path.isfile(concat_list):
                    os.remove(concat_list)
        elif next_has_clip and not current_has_clip:
            # Only next scene has a clip -- transfer it to current
            current["clip_path"] = nxt["clip_path"]
            current["has_clip"] = True
            nxt["clip_path"] = None  # prevent cleanup from deleting it
        # If only current has a clip, keep it as-is

        # Merge durations: combined = scene1.duration + scene2.duration
        current["duration"] = current.get("duration", 5) + nxt.get("duration", 5)

        # Keep current's photo if it has one, otherwise use next's photo
        current_has_photo = current.get("photo_path") and os.path.isfile(current.get("photo_path", ""))
        next_has_photo = nxt.get("photo_path") and os.path.isfile(nxt.get("photo_path", ""))
        if not current_has_photo and next_has_photo:
            current["photo_path"] = nxt["photo_path"]
            nxt["photo_path"] = None  # prevent cleanup from deleting it
        elif next_has_photo:
            # Current already has a photo; clean up next's photo
            os.remove(nxt["photo_path"])

        # Remove next scene (clean up remaining files)
        if nxt.get("clip_path") and os.path.isfile(nxt.get("clip_path", "")):
            os.remove(nxt["clip_path"])
        if nxt.get("photo_path") and os.path.isfile(nxt.get("photo_path", "")):
            os.remove(nxt["photo_path"])

        scenes.pop(src_idx + 1)
        _save_manual_plan(plan)
        self._send_json({"ok": True, "scene": current})

    def _handle_manual_stitch_settings(self):
        """Update global stitch settings (color_grade, audio_viz)."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        plan = _load_manual_plan()
        if "color_grade" in params:
            plan["color_grade"] = params["color_grade"]
        if "audio_viz" in params:
            plan["audio_viz"] = params["audio_viz"]
        if "audio_crossfade" in params:
            try:
                plan["audio_crossfade"] = max(0.0, min(2.0, float(params["audio_crossfade"])))
            except (ValueError, TypeError):
                plan["audio_crossfade"] = 0.0
        if "auto_duck" in params:
            plan["auto_duck"] = bool(params["auto_duck"])
        if "duck_level" in params:
            try:
                plan["duck_level"] = max(0.0, min(1.0, float(params["duck_level"])))
            except (ValueError, TypeError):
                plan["duck_level"] = 0.3
        if "continuity_mode" in params:
            plan["continuity_mode"] = bool(params["continuity_mode"])
        if "natural_pacing" in params:
            plan["natural_pacing"] = bool(params["natural_pacing"])
        _save_manual_plan(plan)
        self._send_json({"ok": True})

    def _handle_update_settings(self):
        """Update global project settings (default_engine, character_references, etc.)."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        settings = _load_settings()

        if "default_engine" in params:
            engine = params["default_engine"]
            if engine in SUPPORTED_ENGINES:
                settings["default_engine"] = engine

        if "character_references" in params:
            settings["character_references"] = params["character_references"]

        if "project_title" in params:
            settings["project_title"] = params["project_title"]

        if "director_state" in params:
            settings["director_state"] = params["director_state"]

        if "face_swap_enabled" in params:
            settings["face_swap_enabled"] = bool(params["face_swap_enabled"])

        if "face_swap_onnx" in params:
            settings["face_swap_onnx"] = bool(params["face_swap_onnx"])

        _save_settings(settings)
        self._send_json({"ok": True, "settings": settings})

    def _handle_update_character_references(self):
        """Add or update a character reference mapping."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        settings = _load_settings()
        char_refs = settings.get("character_references", {})

        # Support adding single reference: {name: "TB", reference_name: "bear"}
        # where reference_name refers to a file in references/ dir
        name = params.get("name", "")
        ref_name = params.get("reference_name", "")
        ref_path = params.get("path", "")

        if name:
            if ref_path:
                char_refs[name] = ref_path
            elif ref_name:
                # Look up in references directory
                refs = _get_references()
                if ref_name in refs:
                    char_refs[name] = refs[ref_name]
                else:
                    self._send_json({"error": f"Reference '{ref_name}' not found"}, 404)
                    return
            else:
                # Delete the character reference
                char_refs.pop(name, None)

        # Support bulk update: {references: {"TB": "bear", ...}}
        if "references" in params:
            refs = _get_references()
            for char_name, ref_name_or_path in params["references"].items():
                if ref_name_or_path in refs:
                    char_refs[char_name] = refs[ref_name_or_path]
                elif os.path.isfile(ref_name_or_path):
                    char_refs[char_name] = ref_name_or_path

        settings["character_references"] = char_refs
        _save_settings(settings)
        self._send_json({"ok": True, "character_references": char_refs})

    def _handle_manual_reorder(self):
        """Reorder manual scenes and optionally set song path."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        order = params.get("order", [])  # list of scene IDs in new order

        plan = _load_manual_plan()

        # Update song path if provided
        song_filename = params.get("song_filename")
        if song_filename:
            plan["song_path"] = os.path.join(UPLOADS_DIR, os.path.basename(song_filename))

        if order:
            scene_map = {s["id"]: s for s in plan["scenes"]}
            new_scenes = []
            for sid in order:
                if sid in scene_map:
                    new_scenes.append(scene_map[sid])
            # Add any scenes not in the order list at the end
            for s in plan["scenes"]:
                if s["id"] not in order:
                    new_scenes.append(s)
            plan["scenes"] = new_scenes

        _save_manual_plan(plan)
        self._send_json({"ok": True})

    # ---- Prompt assistant endpoints ----

    def _handle_get_presets(self):
        """Return all style presets."""
        presets = []
        for name in get_preset_names():
            presets.append({
                "name": name,
                "prompt": STYLE_PRESETS[name],
            })
        self._send_json({"presets": presets})

    def _handle_enhance_prompt(self):
        """Enhance a user prompt with cinematic keywords."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        prompt = params.get("prompt", "")
        if not prompt:
            self._send_json({"error": "No prompt provided"}, 400)
            return

        enhanced = enhance_prompt(prompt)
        self._send_json({"ok": True, "original": prompt, "enhanced": enhanced})

    def _handle_suggest_style(self):
        """Suggest a style based on genre/mood."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        genre = params.get("genre", "")
        mood = params.get("mood", "")
        song_name = params.get("song_name", "")

        if song_name:
            suggestion = suggest_from_song_name(song_name)
        else:
            suggestion = suggest_style(genre=genre, mood=mood)

        self._send_json({"ok": True, "suggestion": suggestion})

    def _handle_ai_autofill(self):
        """Generic Claude completion endpoint used by Guided 'Make my movie',
        _aiAutoFillForm, _aiAutoFillScene. Accepts:
          {prompt, system?, max_tokens?, fieldKeys?, userIdea?, formType?}
        Returns:
          text completion: {ok, result, text, content}
          form autofill:   {ok, fields: {key:val,...}, result, text, content}
        """
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            self._send_json({"error": "prompt is required"}, 400)
            return
        system = body.get("system") or ""
        try:
            max_tokens = int(body.get("max_tokens", 2000))
        except (TypeError, ValueError):
            max_tokens = 2000
        max_tokens = max(128, min(max_tokens, 4000))
        field_keys = body.get("fieldKeys") or body.get("field_keys")

        est_cost = 0.02
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        try:
            from lib.claude_client import call_text, call_json
        except Exception as e:
            self._send_json({"error": f"Claude client unavailable: {e}"}, 503)
            return

        if field_keys and isinstance(field_keys, list) and field_keys:
            json_system = system or ("You are a pre-production helper for an AI film studio. "
                                     "Fill in each requested field with rich cinematic detail. "
                                     "Return JSON only with no preamble.")
            json_prompt = prompt + "\n\nReturn strict JSON with these keys: " + ", ".join(field_keys)
            try:
                result = call_json(json_prompt, system=json_system, max_tokens=max_tokens)
                fields = result if isinstance(result, dict) else {}
                payload = json.dumps(fields)
                self._send_json({
                    "ok": True,
                    "fields": fields,
                    "result": payload,
                    "text": payload,
                    "content": payload,
                })
            except Exception as e:
                self._send_json({"error": f"Claude call failed: {e}"}, 500)
            return

        try:
            text = call_text(prompt, system=system, max_tokens=max_tokens)
            self._send_json({
                "ok": True,
                "result": text,
                "text": text,
                "content": text,
            })
        except Exception as e:
            self._send_json({"error": f"Claude call failed: {e}"}, 500)

    # ---- Transition update endpoints ----

    def _handle_update_scene_transition(self, index: int):
        """Update the transition type for a single scene."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        transition = params.get("transition", "")
        if transition not in TRANSITION_TYPES and transition != "auto":
            self._send_json({"error": f"Invalid transition: {transition}"}, 400)
            return

        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan found"}, 404)
            return

        scenes = plan["scenes"]
        if index < 0 or index >= len(scenes):
            self._send_json({"error": f"Scene index {index} out of range"}, 400)
            return

        if transition == "auto":
            # Re-compute auto transition
            from lib.scene_planner import auto_assign_transition
            if index == 0:
                transition = "crossfade"
            else:
                prev_type = scenes[index - 1].get("section_type", "verse")
                cur_type = scenes[index].get("section_type", "verse")
                transition = auto_assign_transition(prev_type, cur_type)

        scenes[index]["transition"] = transition
        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)

        self._send_json({"ok": True, "index": index, "transition": transition})

    def _handle_update_transitions(self):
        """Bulk update transitions for all scenes."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        transitions = params.get("transitions", [])
        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan found"}, 404)
            return

        for i, trans in enumerate(transitions):
            if i < len(plan["scenes"]) and trans in TRANSITION_TYPES:
                plan["scenes"][i]["transition"] = trans

        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)

        self._send_json({"ok": True})

    # ---- Feature 11: Upscale ----

    def _handle_upscale_scene(self, scene_id: str):
        """Upscale a manual scene's clip 2x using ffmpeg lanczos."""
        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                clip_path = s.get("clip_path", "")
                if not clip_path or not os.path.isfile(clip_path):
                    self._send_json({"error": "No clip to upscale"}, 400)
                    return
                try:
                    _upscale_clip(clip_path)
                    self._send_json({"ok": True, "message": "Clip upscaled 2x"})
                except Exception as e:
                    self._send_json({"error": f"Upscale failed: {e}"}, 500)
                return
        self._send_json({"error": "Scene not found"}, 404)

    # ---- Feature 12: Project Save/Load ----

    # ---- Generate from Photo ----

    def _handle_manual_generate_from_photo(self, scene_id: str):
        """Generate a video clip from a scene's photo + prompt using the photo-to-video pipeline."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return

        # Save prompt first if provided
        body = self._read_body()
        if body:
            try:
                params = json.loads(body)
                if "prompt" in params:
                    plan = _load_manual_plan()
                    for s in plan["scenes"]:
                        if s["id"] == scene_id:
                            s["prompt"] = params["prompt"]
                            _save_manual_plan(plan)
                            break
            except json.JSONDecodeError:
                pass

        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(
            target=_run_manual_generate_from_photo,
            args=(scene_id,),
            daemon=True,
        )
        thread.start()
        self._send_json({"ok": True, "message": f"Generating from photo for scene {scene_id}"})

    # ---- Transition Preview ----

    def _handle_preview_transition(self):
        """Generate a 2-second preview of a transition between two scenes."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        scene_id_a = params.get("scene_id_a", "")
        scene_id_b = params.get("scene_id_b", "")
        transition_type = params.get("transition_type", "crossfade")

        plan = _load_manual_plan()
        scene_a = None
        scene_b = None
        for s in plan["scenes"]:
            if s["id"] == scene_id_a:
                scene_a = s
            elif s["id"] == scene_id_b:
                scene_b = s

        if not scene_a or not scene_b:
            self._send_json({"error": "One or both scenes not found"}, 404)
            return

        clip_a = scene_a.get("clip_path", "")
        clip_b = scene_b.get("clip_path", "")
        if not clip_a or not os.path.isfile(clip_a):
            self._send_json({"error": "Scene A has no clip"}, 400)
            return
        if not clip_b or not os.path.isfile(clip_b):
            self._send_json({"error": "Scene B has no clip"}, 400)
            return

        preview_name = f"transition_preview_{scene_id_a}_{scene_id_b}.mp4"
        preview_path = os.path.join(PREVIEWS_DIR, preview_name)

        try:
            # Get durations
            dur_a = _get_clip_duration(clip_a)
            dur_b = _get_clip_duration(clip_b)

            # Extract last 1s of clip A
            tail_a = os.path.join(PREVIEWS_DIR, f"_tail_{scene_id_a}.mp4")
            start_a = max(0, dur_a - 1.0)
            cmd_a = [
                "ffmpeg", "-y",
                "-ss", str(start_a), "-i", clip_a,
                "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
                "-an", tail_a,
            ]
            subprocess.run(cmd_a, check=True, capture_output=True, timeout=300, **_subprocess_kwargs())

            # Extract first 1s of clip B
            head_b = os.path.join(PREVIEWS_DIR, f"_head_{scene_id_b}.mp4")
            cmd_b = [
                "ffmpeg", "-y",
                "-i", clip_b,
                "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
                "-an", head_b,
            ]
            subprocess.run(cmd_b, check=True, capture_output=True, timeout=300, **_subprocess_kwargs())

            # Apply transition via xfade
            from lib.video_stitcher import _get_xfade_name
            xfade_name = _get_xfade_name(transition_type)
            if xfade_name:
                # xfade transition: overlap at 0.5s
                cmd_t = [
                    "ffmpeg", "-y",
                    "-i", tail_a, "-i", head_b,
                    "-filter_complex",
                    f"[0:v][1:v]xfade=transition={xfade_name}:duration=0.5:offset=0.5[outv]",
                    "-map", "[outv]",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    preview_path,
                ]
            else:
                # Hard cut / glitch / fade_black: just concatenate
                concat_file = os.path.join(PREVIEWS_DIR, f"_concat_{scene_id_a}.txt")
                with open(concat_file, "w") as f:
                    f.write(f"file '{tail_a}'\nfile '{head_b}'\n")
                cmd_t = [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0", "-i", concat_file,
                    "-c:v", "libx264", "-preset", "ultrafast",
                    preview_path,
                ]

            subprocess.run(cmd_t, check=True, capture_output=True, timeout=300, **_subprocess_kwargs())

            # Clean up temp files
            for tmp in [tail_a, head_b]:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            concat_tmp = os.path.join(PREVIEWS_DIR, f"_concat_{scene_id_a}.txt")
            if os.path.isfile(concat_tmp):
                os.remove(concat_tmp)

            self._send_json({
                "ok": True,
                "preview_url": f"/api/previews/{preview_name}",
            })
        except Exception as e:
            self._send_json({"error": f"Preview generation failed: {e}"}, 500)

    # ---- Full Preview ----

    def _handle_preview_all(self):
        """Generate a low-quality 480p preview of the entire video."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"

        thread = threading.Thread(target=_run_preview_all, daemon=True)
        thread.start()
        self._send_json({"ok": True, "message": "Generating full preview"})

    # ---- Feature 14: Storyboard ----

    def _handle_generate_storyboard(self):
        """Generate a storyboard PNG from manual or auto scenes."""
        plan = _load_manual_plan()
        scenes = plan.get("scenes", [])
        if not scenes:
            auto = _load_scene_plan()
            if auto:
                scenes = auto.get("scenes", [])

        if not scenes:
            self._send_json({"error": "No scenes available for storyboard"}, 400)
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(STORYBOARD_DIR, f"storyboard_{timestamp}.png")

        try:
            generate_storyboard(scenes, output_path)
            filename = os.path.basename(output_path)
            self._send_json({
                "ok": True,
                "url": f"/api/storyboard/{filename}",
                "filename": filename,
            })
        except Exception as e:
            self._send_json({"error": f"Storyboard generation failed: {e}"}, 500)

    # ---- Feature 15: Cost Tracker ----

    def _handle_get_cost(self):
        """Return current cost tracking data."""
        tracker = _load_cost_tracker()
        # Trim the ledger in the summary response for payload size
        summary = dict(tracker)
        gens = summary.get("generations", [])
        summary["generation_count"] = len(gens)
        summary["generations"] = gens[-20:]  # most-recent 20 only
        self._send_json(summary)

    def _handle_cost_ledger(self):
        """Return the full generation ledger."""
        tracker = _load_cost_tracker()
        self._send_json({
            "budget": tracker.get("budget", DEFAULT_BUDGET),
            "total_cost": tracker.get("total_cost", 0),
            "generations": tracker.get("generations", []),
        })

    def _handle_cost_csv(self):
        """Export the ledger as CSV for producer hand-off."""
        import io as _io
        import csv as _csv
        tracker = _load_cost_tracker()
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["timestamp", "shot_id", "type", "engine", "tier", "duration_s", "est_cost", "actual_cost", "billed", "status"])
        for g in tracker.get("generations", []):
            w.writerow([
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(g.get("ts", 0))),
                g.get("shot_id", ""),
                g.get("type", ""),
                g.get("engine", ""),
                g.get("tier", ""),
                g.get("duration", ""),
                g.get("est", ""),
                g.get("actual", ""),
                g.get("billed", ""),
                g.get("status", ""),
            ])
        body = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="lumn_cost_ledger.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_runway_credits(self):
        """Fetch remaining Runway API credits from /v1/organization."""
        import requests as _req
        key = os.environ.get("RUNWAY_API_KEY", "")
        if not key:
            self._send_json({"credits": None, "error": "RUNWAY_API_KEY not set"})
            return
        try:
            resp = _req.get(
                "https://api.dev.runwayml.com/v1/organization",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                    "X-Runway-Version": "2024-11-06",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                credits = data.get("creditBalance", data.get("credits", None))
                self._send_json({"credits": credits, "raw": data})
            else:
                self._send_json({"credits": None, "error": f"Runway API {resp.status_code}"})
        except Exception as e:
            self._send_json({"credits": None, "error": str(e)})

    def _handle_quick_preview(self, scene_id: str):
        """Feature 5: Generate a 1-second preview or first frame for a scene."""
        plan = _load_manual_plan()
        scene = None
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                scene = s
                break
        if scene is None:
            self._send_json({"error": "Scene not found"}, 404)
            return
        prompt = scene.get("prompt", "cinematic scene")
        if not prompt.strip():
            self._send_json({"error": "Scene has no prompt"}, 400)
            return
        # Generate a quick preview image using Grok image API (cheap $0.02)
        try:
            from lib.video_generator import _get_api_key
            import requests as _requests
            api_key = _get_api_key()
            resp = _requests.post(
                "https://api.x.ai/v1/images/generations",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": "grok-2-image", "prompt": prompt,
                      "n": 1, "size": "512x512"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                img_url = data.get("data", [{}])[0].get("url", "")
                if img_url:
                    # Download and save
                    img_resp = _requests.get(img_url, timeout=30)
                    if img_resp.status_code == 200:
                        preview_path = os.path.join(PREVIEWS_DIR, f"preview_{scene_id}.jpg")
                        with open(preview_path, "wb") as f:
                            f.write(img_resp.content)
                        _record_cost(str(scene_id), "image")
                        self._send_json({
                            "ok": True,
                            "preview_url": f"/api/previews/preview_{scene_id}.jpg",
                        })
                        return
            # Fallback: just save a placeholder
            self._send_json({"error": "Could not generate preview"}, 500)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # ---- Prompt OS: Photo Upload & Preview Generation ----

    def _handle_pos_photo_upload(self, entity_id, entity_type):
        """Handle photo upload for characters/costumes/environments."""
        try:
            ct = self.headers.get("Content-Type", "")
            if "multipart" not in ct:
                self._send_json({"error": "Expected multipart upload"}, 400)
                return
            boundary = ct.split("boundary=")[-1].encode()
            body = self._read_body()
            parts = self._parse_multipart(body, boundary)
            file_part = None
            for p in parts:
                if p.get("filename"):
                    file_part = p
                    break
            if not file_part or not file_part["data"]:
                self._send_json({"error": "No file uploaded"}, 400)
                return

            # Determine output directory and entity getter/updater
            dirs_map = {
                "characters": _pos_photos_dir("char"),
                "costumes": _pos_photos_dir("costume"),
                "environments": _pos_photos_dir("env"),
            }
            out_dir = dirs_map[entity_type]
            os.makedirs(out_dir, exist_ok=True)  # Ensure dir exists after resets
            out_path = os.path.join(out_dir, entity_id + ".jpg")

            # Save and resize with PIL
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(file_part["data"]))
            img = img.convert("RGB")
            # Resize to max 1280x720
            max_w, max_h = 1280, 720
            if img.width > max_w or img.height > max_h:
                img.thumbnail((max_w, max_h), Image.LANCZOS)
            img.save(out_path, "JPEG", quality=90)

            # Update entity record
            photo_url = f"/api/pos/{entity_type}/{entity_id}/photo"
            if entity_type == "characters":
                _prompt_os.update_character(entity_id, {"referencePhoto": photo_url})
            elif entity_type == "costumes":
                _prompt_os.update_costume(entity_id, {"referenceImagePath": photo_url})
            elif entity_type == "environments":
                _prompt_os.update_environment(entity_id, {"referenceImagePath": photo_url})

            self._send_json({"ok": True, "photo_url": photo_url})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_pos_voice_sample_upload(self, voice_id):
        """Handle audio sample upload for a POS voice record."""
        try:
            if not _prompt_os.get_voice(voice_id):
                self._send_json({"error": "Voice not found"}, 404)
                return
            ct = self.headers.get("Content-Type", "")
            if "multipart" not in ct:
                self._send_json({"error": "Expected multipart upload"}, 400)
                return
            boundary = ct.split("boundary=")[-1].encode()
            body = self._read_body()
            parts = self._parse_multipart(body, boundary)
            file_part = None
            for p in parts:
                if p.get("filename"):
                    file_part = p
                    break
            if not file_part or not file_part["data"]:
                self._send_json({"error": "No file uploaded"}, 400)
                return

            os.makedirs(_pos_voices_dir(), exist_ok=True)
            filename = file_part.get("filename") or ""
            ext = os.path.splitext(filename)[1].lower()
            if ext not in (".mp3", ".wav", ".m4a", ".ogg", ".webm"):
                ext = ".mp3"
            # Remove any existing sample for this voice across supported exts
            for old_ext in (".mp3", ".wav", ".m4a", ".ogg", ".webm"):
                old_path = os.path.join(_pos_voices_dir(), f"{voice_id}{old_ext}")
                if os.path.isfile(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
            out_path = os.path.join(_pos_voices_dir(), f"{voice_id}{ext}")
            with open(out_path, "wb") as f:
                f.write(file_part["data"])

            sample_url = f"/api/pos/voices/{voice_id}/sample"
            _prompt_os.update_voice(voice_id, {"sampleAudioPath": sample_url})
            self._send_json({"ok": True, "sample_url": sample_url})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_pos_auto_describe(self, entity_id, entity_type):
        """Auto-describe a character or environment from its reference photo using vision AI.
        Returns structured JSON with all form fields populated."""
        try:
            import base64, requests as _req

            # Find the photo file
            if entity_type == "characters":
                photo_dir = _pos_photos_dir("char")
            elif entity_type == "costumes":
                photo_dir = _pos_photos_dir("costume")
            elif entity_type == "environments":
                photo_dir = _pos_photos_dir("env")
            else:
                self._send_json({"error": f"Unsupported type: {entity_type}"}, 400)
                return

            photo_path = None
            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                candidate = os.path.join(photo_dir, f"{entity_id}{ext}")
                if os.path.isfile(candidate):
                    photo_path = candidate
                    break

            if not photo_path:
                self._send_json({"error": "No reference photo found. Upload a photo first."}, 400)
                return

            # Read photo as base64
            with open(photo_path, "rb") as f:
                photo_bytes = f.read()
            ext = os.path.splitext(photo_path)[1].lower()
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
            data_uri = f"data:{mime};base64,{base64.b64encode(photo_bytes).decode('ascii')}"

            api_key = os.environ.get("XAI_API_KEY", "")

            if entity_type == "characters":
                prompt_text = (
                    "Analyze this image of a character. This may be a SINGLE photo OR a CHARACTER SHEET "
                    "showing the SAME person from multiple angles (front, side, back, 3/4 view). "
                    "If it is a character sheet with multiple views, treat them ALL as the SAME SINGLE PERSON "
                    "and combine what you see from every angle into one unified description.\n\n"
                    "Return a JSON object with these fields. "
                    "These descriptions will be used as prompts for AI video generation, so the physicalDescription "
                    "must be extremely detailed and vivid — it is the most important field.\n\n"
                    '{\n'
                    '  "physicalDescription": "VERY detailed 4-6 sentence description of this ONE person, combining all visible angles. '
                    'Cover: face shape, exact skin tone, eye color and shape, nose, lips, jawline, facial hair if any, '
                    'build/physique, height impression, overall vibe and presence. '
                    'Write as comma-separated visual descriptors that an AI image generator can use to recreate this exact person. '
                    'Include specific details like brow thickness, cheekbone prominence, face width, ear shape if visible from side views. '
                    'If multiple angles are shown, note details only visible from certain angles (e.g. profile view reveals strong jaw, back view shows tattoo). '
                    'Be precise, not generic.",\n'
                    '  "hair": "exact hair color, texture, style, length, parting — describe from all visible angles '
                    '(e.g. short tapered black hair with low fade, clean neckline visible from back, side-parted from front)",\n'
                    '  "skinTone": "precise skin tone (e.g. deep brown, light olive, warm caramel, fair with freckles)",\n'
                    '  "bodyType": "build from all visible angles (e.g. athletic V-shaped torso visible from back, slim from side profile)",\n'
                    '  "ageRange": "estimated age range (e.g. early 20s, mid-30s, late 40s)",\n'
                    '  "distinguishingFeatures": "ALL unique identifiers from ALL angles: scars, tattoos (location and design), '
                    'piercings, moles, glasses, facial hair style, dimples, gap teeth, birthmarks — anything visible from any angle",\n'
                    '  "defaultExpression": "typical expression and energy (e.g. intense focused gaze, relaxed confident smirk)",\n'
                    '  "outfitDescription": "detailed outfit from all angles: front details, back details, side profile, '
                    'fabric types, colors, fit, accessories, shoes, jewelry"\n'
                    '}\n\n'
                    "Return ONLY valid JSON, no other text."
                )
            elif entity_type == "costumes":
                prompt_text = (
                    "Analyze this image of a costume/outfit and return a JSON object with these fields. "
                    "Be specific and visual — these descriptions will be used to generate AI video with this wardrobe.\n\n"
                    '{\n'
                    '  "description": "2-3 sentence vivid description of the complete outfit look, silhouette, and style",\n'
                    '  "upperBody": "detailed upper body garment — jacket, shirt, top, outerwear, layering",\n'
                    '  "lowerBody": "detailed lower body garment — pants, skirt, bottoms, legwear",\n'
                    '  "footwear": "shoes, boots, or other footwear visible",\n'
                    '  "accessories": "jewelry, belts, gloves, eyewear, headwear, bags, watches, chains",\n'
                    '  "colorPalette": "dominant colors in the outfit (e.g. black, gunmetal, neon blue)",\n'
                    '  "material": "primary fabric/material (e.g. leather, denim, tactical nylon, silk)",\n'
                    '  "wearLevel": "condition (pristine, clean, lightly worn, worn, distressed, damaged)",\n'
                    '  "texture": "surface finish (matte, glossy, weathered, rough, smooth)"\n'
                    '}\n\n'
                    "Return ONLY valid JSON, no other text."
                )
            else:
                prompt_text = (
                    "Analyze this image of an environment/location and return a JSON object with these fields. "
                    "Be specific and visual — these descriptions will be used to generate AI video in this setting.\n\n"
                    '{\n'
                    '  "description": "2-3 sentence vivid description of the environment",\n'
                    '  "location": "type of location (e.g. urban rooftop, forest clearing, underground club)",\n'
                    '  "timeOfDay": "time of day (e.g. night, golden hour, overcast afternoon)",\n'
                    '  "weather": "weather/conditions (e.g. rainy, clear, foggy, snowy)",\n'
                    '  "lighting": "lighting description (e.g. neon-lit, natural sunlight, dramatic shadows)",\n'
                    '  "keyProps": "notable objects or props in the scene",\n'
                    '  "atmosphere": "mood/atmosphere (e.g. gritty and tense, serene and peaceful)"\n'
                    '}\n\n'
                    "Return ONLY valid JSON, no other text."
                )

            resp = _req.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "grok-4-1-fast-non-reasoning",
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": prompt_text},
                    ]}],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 800,
                },
                timeout=60,
            )
            resp.raise_for_status()
            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")

            # Parse the JSON response
            import re as _re
            try:
                fields = json.loads(content)
            except json.JSONDecodeError:
                # Try extracting JSON from markdown
                m = _re.search(r'\{[\s\S]*\}', content)
                if m:
                    fields = json.loads(m.group(0))
                else:
                    self._send_json({"error": "Could not parse AI response"}, 500)
                    return

            self._send_json({"fields": fields, "entity_type": entity_type})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_pos_generate_preview(self, entity_id, entity_type):
        """Generate an AI preview image for a character/costume/environment."""
        try:
            # Get entity data
            entity = None
            if entity_type == "characters":
                entity = _prompt_os.get_character(entity_id)
            elif entity_type == "costumes":
                entity = _prompt_os.get_costume(entity_id)
            elif entity_type == "environments":
                entity = _prompt_os.get_environment(entity_id)

            if not entity:
                self._send_json({"error": "Entity not found"}, 404)
                return

            # Build prompt based on entity type
            if entity_type == "characters":
                parts = []
                if entity.get("physicalDescription"):
                    parts.append(entity["physicalDescription"])
                if entity.get("hair"):
                    parts.append(entity["hair"])
                if entity.get("skinTone"):
                    parts.append(entity["skinTone"])
                if entity.get("bodyType"):
                    parts.append(entity["bodyType"])
                if entity.get("outfitDescription"):
                    parts.append(entity["outfitDescription"])
                if entity.get("accessories"):
                    acc = entity["accessories"]
                    if isinstance(acc, list):
                        acc = ", ".join(acc)
                    parts.append(acc)
                desc = ", ".join(p for p in parts if p)
                prompt = f"Single cinematic portrait, front-facing, one person only: {desc}. Studio lighting, neutral background, photorealistic, high detail"

            elif entity_type == "costumes":
                parts = []
                if entity.get("description"):
                    parts.append(entity["description"])
                else:
                    for field in ("upperBody", "lowerBody", "footwear", "accessories"):
                        if entity.get(field):
                            parts.append(entity[field])
                if entity.get("colorPalette"):
                    parts.append(f"color palette: {entity['colorPalette']}")
                desc = ", ".join(p for p in parts if p)
                prompt = f"Fashion photography of {desc}. Clean background, detailed fabric texture, professional lighting"

            elif entity_type == "environments":
                parts = []
                if entity.get("description"):
                    parts.append(entity["description"])
                if entity.get("location"):
                    parts.append(entity["location"])
                if entity.get("architecture"):
                    parts.append(entity["architecture"])
                if entity.get("lighting"):
                    parts.append(f"lighting: {entity['lighting']}")
                if entity.get("atmosphere"):
                    parts.append(f"atmosphere: {entity['atmosphere']}")
                if entity.get("weather"):
                    parts.append(entity["weather"])
                if entity.get("timeOfDay"):
                    parts.append(entity["timeOfDay"])
                desc = ", ".join(p for p in parts if p)
                prompt = f"Wide establishing shot of {desc}. Cinematic, high detail, no people"

            # Resolve reference photo path
            ref_photo = entity.get("referencePhoto", entity.get("referenceImagePath", ""))
            ref_photo_path = None
            if ref_photo:
                import re as _re2
                m = _re2.search(r"/api/pos/(?:characters|environments|costumes)/([^/]+)/photo", ref_photo)
                if m:
                    eid = m.group(1)
                    photo_dirs = {"characters": _pos_photos_dir("char"), "costumes": _pos_photos_dir("costume"), "environments": _pos_photos_dir("env")}
                    pdir = photo_dirs.get(entity_type, _pos_photos_dir("char"))
                    for ext in (".jpg", ".jpeg", ".png", ".webp"):
                        candidate = os.path.join(pdir, f"{eid}{ext}")
                        if os.path.isfile(candidate):
                            ref_photo_path = candidate
                            break
                elif os.path.isfile(ref_photo):
                    ref_photo_path = ref_photo

            # For characters with a reference photo: USE THE PHOTO as the preview
            # This avoids Grok generating a different-looking person
            if ref_photo_path and entity_type == "characters":
                import shutil as _sh2
                preview_path = os.path.join(_pos_previews_dir("char"), f"{entity_id}.jpg")
                _sh2.copy2(ref_photo_path, preview_path)
                preview_url = f"/api/pos/{entity_type}/{entity_id}/preview"
                _prompt_os.update_character(entity_id, {"previewImage": preview_url})
                self._send_json({"ok": True, "preview_url": preview_url, "source": "reference_photo"})
                return

            # For environments with a reference photo: USE THE PHOTO as the preview
            if ref_photo_path and entity_type == "environments":
                import shutil as _sh3
                preview_path = os.path.join(_pos_previews_dir("env"), f"{entity_id}.jpg")
                _sh3.copy2(ref_photo_path, preview_path)
                preview_url = f"/api/pos/{entity_type}/{entity_id}/preview"
                _prompt_os.update_environment(entity_id, {"previewImage": preview_url})
                self._send_json({"ok": True, "preview_url": preview_url, "source": "reference_photo"})
                return

            if ref_photo_path and not desc.strip():
                # Use the uploaded photo AS the preview (no need to generate)
                import shutil as _sh
                preview_dirs = {
                    "characters": _pos_previews_dir("char"),
                    "costumes": _pos_previews_dir("costume"),
                    "environments": _pos_previews_dir("env"),
                }
                preview_dir = preview_dirs.get(entity_type, _pos_previews_dir("char"))
                preview_path = os.path.join(preview_dir, f"{entity_id}.jpg")
                _sh.copy2(ref_photo_path, preview_path)
                entity["previewImage"] = preview_path
                if entity_type == "characters": _prompt_os.update_character(entity_id, {"previewImage": preview_path})
                elif entity_type == "costumes": _prompt_os.update_costume(entity_id, {"previewImage": preview_path})
                elif entity_type == "environments": _prompt_os.update_environment(entity_id, {"previewImage": preview_path})
                self._send_json({"ok": True, "preview_url": f"/api/pos/{entity_type}/{entity_id}/preview", "source": "uploaded_photo"})
                return

            # If no description at all, use entity name as prompt
            if not desc.strip():
                desc = entity.get("name", "unnamed")
                prompt = f"Artistic portrait of {desc}, studio lighting, high detail"

            # Determine which engine to use for preview
            settings = _load_settings()
            preview_engine = settings.get("default_engine", "gen4_5")
            # Read body for engine override if provided
            try:
                body_data = json.loads(self._read_body()) if self.headers.get("Content-Length") else {}
                if body_data.get("engine"):
                    preview_engine = body_data["engine"]
            except Exception:
                pass

            # Video-only engines: generate short clip, extract first frame
            VIDEO_ONLY_ENGINES = {"gen4_5", "gen3a_turbo", "kling_pro", "kling_standard",
                                   "veo3", "veo3_1", "veo3_1_fast", "luma", "runway"}

            preview_dirs = {
                "characters": _pos_previews_dir("char"),
                "costumes": _pos_previews_dir("costume"),
                "environments": _pos_previews_dir("env"),
            }
            preview_path = os.path.join(preview_dirs[entity_type], entity_id + ".jpg")

            if preview_engine in VIDEO_ONLY_ENGINES:
                # Generate a 4-second clip with the selected engine, extract frame 1
                print(f"[PREVIEW] Using video engine '{preview_engine}' for {entity_type}/{entity_id}")
                import tempfile
                gen_scene = {
                    "prompt": prompt,
                    "duration": 4,
                    "camera_movement": "static",
                    "engine": preview_engine,
                    "id": f"preview_{entity_id}",
                }
                # Add character description + photo for character previews
                char_desc = ""
                photo_path_for_gen = None
                if entity_type == "characters" and ref_photo_path:
                    photo_path_for_gen = ref_photo_path
                    phys = entity.get("physicalDescription", entity.get("description", ""))
                    if phys:
                        char_desc = phys
                    gen_scene["character_description"] = char_desc

                try:
                    tmp_dir = os.path.join(OUTPUT_DIR, "preview_clips")
                    os.makedirs(tmp_dir, exist_ok=True)
                    clip_path = generate_scene(gen_scene, 0, tmp_dir,
                                                photo_path=photo_path_for_gen)
                    # Extract first frame
                    from lib.cinematic_engine import extract_last_frame
                    # Extract FIRST frame (at time 0)
                    kw = {}
                    if __import__("sys").platform == "win32":
                        si = __import__("subprocess").STARTUPINFO()
                        si.dwFlags |= __import__("subprocess").STARTF_USESHOWWINDOW
                        si.wShowWindow = 0
                        kw["startupinfo"] = si
                    __import__("subprocess").run(
                        ["ffmpeg", "-y", "-i", clip_path, "-frames:v", "1", "-q:v", "2", preview_path],
                        capture_output=True, timeout=15, **kw
                    )
                    if not os.path.isfile(preview_path):
                        raise RuntimeError("Frame extraction failed")
                    print(f"[PREVIEW] Extracted frame from {preview_engine} clip: {preview_path}")
                    _record_cost(f"pos_{entity_type}_{entity_id}", "video")
                except Exception as ve:
                    print(f"[PREVIEW] Video engine failed ({ve}), falling back to Grok image")
                    # Fall through to Grok image generation below
                    preview_engine = "grok"

            if preview_engine not in VIDEO_ONLY_ENGINES:
                # Image generation (Grok or OpenAI)
                from lib.video_generator import _get_api_key
                import requests as _requests
                api_key = _get_api_key()
                print(f"[PREVIEW] Generating image preview for {entity_type}/{entity_id} via {preview_engine}: {prompt[:80]}...")

                if preview_engine == "openai":
                    # OpenAI DALL-E
                    oai_key = os.environ.get("OPENAI_API_KEY", "")
                    if oai_key:
                        resp = _requests.post(
                            "https://api.openai.com/v1/images/generations",
                            headers={"Authorization": f"Bearer {oai_key}", "Content-Type": "application/json"},
                            json={"model": "dall-e-3", "prompt": prompt, "n": 1, "size": "1024x1024"},
                            timeout=60,
                        )
                    else:
                        # No OpenAI key, fall back to Grok
                        preview_engine = "grok"

                if preview_engine == "grok":
                    resp = _requests.post(
                        "https://api.x.ai/v1/images/generations",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": "grok-imagine-image", "prompt": prompt, "n": 1},
                        timeout=60,
                    )

                if resp.status_code != 200:
                    print(f"[PREVIEW] API error {resp.status_code}: {resp.text[:200]}")
                    self._send_json({"error": f"API error: {resp.status_code}"}, 500)
                    return

                data = resp.json()
                img_url = data.get("data", [{}])[0].get("url", "")
                if not img_url:
                    self._send_json({"error": "No image URL in API response"}, 500)
                    return

                img_resp = _requests.get(img_url, timeout=30)
                if img_resp.status_code != 200:
                    self._send_json({"error": "Failed to download image"}, 500)
                    return

                with open(preview_path, "wb") as f:
                    f.write(img_resp.content)

                _record_cost(f"pos_{entity_type}_{entity_id}", "image")

            # Update entity record
            preview_url = f"/api/pos/{entity_type}/{entity_id}/preview"
            if entity_type == "characters":
                _prompt_os.update_character(entity_id, {"previewImage": preview_url})
            elif entity_type == "costumes":
                _prompt_os.update_costume(entity_id, {"previewImage": preview_url})
            elif entity_type == "environments":
                _prompt_os.update_environment(entity_id, {"previewImage": preview_url})

            self._send_json({"ok": True, "preview_url": preview_url})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # ---- Character Sheet Generation ----

    def _handle_pos_generate_character_sheet(self, char_id):
        """Generate a multi-angle character design sheet from a character's photo.

        POST /api/pos/characters/<id>/generate-sheet

        Uses the Grok image API to create a character sheet showing
        front view, 3/4 view, and side view on a clean white background.
        Saves the result as the character's preview image.
        """
        try:
            entity = _prompt_os.get_character(char_id)
            if not entity:
                self._send_json({"error": "Character not found"}, 404)
                return

            # Build description from character fields
            parts = []
            for field in ("physicalDescription", "hair", "skinTone", "bodyType",
                          "ageRange", "distinguishingFeatures", "outfitDescription"):
                val = entity.get(field, "")
                if val:
                    parts.append(val)
            if entity.get("accessories"):
                acc = entity["accessories"]
                if isinstance(acc, list):
                    acc = ", ".join(acc)
                parts.append(acc)

            desc = ", ".join(p for p in parts if p) or entity.get("name", "character")

            # Resolve the character's reference photo
            ref_photo = entity.get("referencePhoto", entity.get("referenceImagePath", ""))
            ref_photo_path = None
            if ref_photo:
                m = re.search(r"/api/pos/characters/([^/]+)/photo", ref_photo)
                if m:
                    eid = m.group(1)
                    for ext in (".jpg", ".jpeg", ".png", ".webp"):
                        candidate = os.path.join(_pos_photos_dir("char"), f"{eid}{ext}")
                        if os.path.isfile(candidate):
                            ref_photo_path = candidate
                            break
                elif os.path.isfile(ref_photo):
                    ref_photo_path = ref_photo

            # Build the character sheet prompt
            sheet_prompt = (
                f"Professional character design reference sheet showing three views of the same person: "
                f"front view, three-quarter view, and side profile view. "
                f"Character: {desc}. "
                f"Clean white background, consistent proportions across all views, "
                f"labeled 'FRONT', '3/4', 'SIDE' below each view. "
                f"Professional illustration style, high detail, full body visible in each view."
            )

            from lib.video_generator import _get_api_key, _describe_entity_photo
            import requests as _requests
            import base64 as _b64

            api_key = _get_api_key()
            preview_path = os.path.join(_pos_previews_dir("char"), f"{char_id}_sheet.jpg")
            os.makedirs(os.path.dirname(preview_path), exist_ok=True)

            # Step 1: If photo exists, use Vision API to get a hyper-detailed description
            # This is the key — we describe every visual detail so the image gen can recreate it
            vision_desc = ""
            if ref_photo_path and os.path.isfile(ref_photo_path):
                try:
                    with open(ref_photo_path, "rb") as pf:
                        photo_bytes = pf.read()
                    ext = os.path.splitext(ref_photo_path)[1].lower()
                    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                ".png": "image/png", ".webp": "image/webp"}
                    mime = mime_map.get(ext, "image/jpeg")
                    b64_data = _b64.b64encode(photo_bytes).decode("ascii")
                    data_uri = f"data:{mime};base64,{b64_data}"

                    # Get extremely detailed physical description via Vision
                    vision_resp = _requests.post(
                        "https://api.x.ai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": "grok-4-1-fast-non-reasoning",
                            "messages": [{
                                "role": "user",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": data_uri}},
                                    {"type": "text", "text": (
                                        "Describe this person in extreme visual detail for an artist to recreate them. "
                                        "Include: exact face shape, eye shape and color, eyebrow style, nose shape and size, "
                                        "lip shape, skin tone and complexion, exact hair style and color and texture, "
                                        "facial hair if any, head shape, ear visibility, neck, jawline, "
                                        "body build and proportions, posture, clothing details and colors, "
                                        "any accessories, tattoos, or distinguishing features. "
                                        "Be extremely specific — describe shapes, proportions, and relative sizes. "
                                        "Do NOT name or identify the person. Only describe physical appearance."
                                    )}
                                ]
                            }],
                            "max_tokens": 500,
                        },
                        timeout=60,
                    )
                    if vision_resp.status_code == 200:
                        vision_desc = vision_resp.json()["choices"][0]["message"]["content"].strip()
                        print(f"[CHAR_SHEET] Vision description ({len(vision_desc)} chars): {vision_desc[:100]}...")
                except Exception as ve:
                    print(f"[CHAR_SHEET] Vision describe failed: {ve}")

            # Step 2: Build the sheet prompt from the detailed vision description
            char_desc = vision_desc or desc
            sheet_gen_prompt = (
                f"Professional character design reference sheet. "
                f"Three views of the SAME person side by side: front view, three-quarter view, side profile. "
                f"Character appearance: {char_desc}. "
                f"CRITICAL: All three views must show the EXACT SAME person with identical features. "
                f"Clean white background, full body visible, consistent proportions, high detail illustration."
            )

            # Truncate if needed
            if len(sheet_gen_prompt) > 900:
                sheet_gen_prompt = sheet_gen_prompt[:897] + "..."

            # Step 3: Generate the sheet image
            resp = _requests.post(
                "https://api.x.ai/v1/images/generations",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "grok-imagine-image", "prompt": sheet_gen_prompt, "n": 1},
                timeout=90,
            )

            if resp.status_code != 200:
                print(f"[CHAR_SHEET] API error {resp.status_code}: {resp.text[:200]}")
                self._send_json({"error": f"Image generation failed: {resp.status_code}"}, 500)
                return

            data = resp.json()
            img_url = data.get("data", [{}])[0].get("url", "")
            b64_img = data.get("data", [{}])[0].get("b64_json", "")

            if b64_img:
                img_bytes = _b64.b64decode(b64_img)
                with open(preview_path, "wb") as f:
                    f.write(img_bytes)
            elif img_url:
                img_resp = _requests.get(img_url, timeout=30)
                if img_resp.status_code != 200:
                    self._send_json({"error": "Failed to download sheet image"}, 500)
                    return
                with open(preview_path, "wb") as f:
                    f.write(img_resp.content)
            else:
                self._send_json({"error": "No image in API response"}, 500)
                return

            # Also save the vision description back to the character for future use
            if vision_desc:
                _prompt_os.update_character(char_id, {"physicalDescription": vision_desc})

            _record_cost(f"char_sheet_{char_id}", "image")

            # Update character with sheet path
            sheet_url = f"/api/pos/characters/{char_id}/preview?sheet=1&t={int(time.time())}"
            _prompt_os.update_character(char_id, {"previewImage": preview_path, "characterSheet": preview_path})

            print(f"[CHAR_SHEET] Generated character sheet: {preview_path}")
            self._send_json({"ok": True, "sheet_url": sheet_url, "preview_url": f"/api/pos/characters/{char_id}/preview"})

        except Exception as e:
            print(f"[CHAR_SHEET] Error: {e}")
            self._send_json({"error": str(e)}, 500)

    def _handle_style_transfer(self, char_id):
        """POST /api/pos/characters/<id>/style-transfer
        Convert a character's reference photo into the target art style
        (detected from physicalDescription). The result becomes a styled
        reference that sheet generation uses instead of the raw photo,
        solving the problem where Gemini edit mode locks to the ref's
        original style.

        Body: {mode?: "edit"|"generate"}
          - edit: Gemini edit with the ref + strong style-override prompt
          - generate: text-only Gemini generation describing the character
                      in the target style (no ref → no style contamination)
          Default: "edit" first; if style detection indicates the ref style
          will dominate, falls back to "generate" automatically.
        """
        try:
            body = json.loads(self._read_body()) if self.headers.get("Content-Length") else {}
        except (json.JSONDecodeError, ValueError):
            body = {}

        mode = (body.get("mode") or "edit").lower()
        custom_style = (body.get("stylePrompt") or "").strip()

        entity = _prompt_os.get_character(char_id)
        if not entity:
            self._send_json({"error": "Character not found"}, 404)
            return

        # Resolve reference photo path
        ref_photo = entity.get("referencePhoto") or entity.get("referenceImagePath") or ""
        ref_photo_path = None
        if ref_photo:
            pdir = _pos_photos_dir("char")
            m = re.search(r"/api/pos/characters/([^/]+)/photo", ref_photo)
            if m:
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    candidate = os.path.join(pdir, f"{m.group(1)}{ext}")
                    if os.path.isfile(candidate):
                        ref_photo_path = candidate
                        break
            elif os.path.isfile(ref_photo):
                ref_photo_path = ref_photo

        if not ref_photo_path:
            self._send_json({"error": "No reference photo found for this character"}, 400)
            return

        # Detect target style from physicalDescription OR use custom prompt
        phys_desc = entity.get("physicalDescription", "")
        phys_lower = phys_desc.lower() if phys_desc else ""
        name = entity.get("name", "character")

        # Build identity feature list (everything EXCEPT style keywords)
        identity_fields = []
        for f in ("hair", "distinguishingFeatures", "accessories", "outfitDescription"):
            v = entity.get(f)
            if v:
                if isinstance(v, list):
                    identity_fields.append(", ".join(str(a) for a in v if a))
                else:
                    identity_fields.append(str(v))
        identity_desc = "; ".join(identity_fields) if identity_fields else ""

        # If user provided a custom style prompt, use it directly
        if custom_style:
            style_prompt_edit = (
                f"COMPLETELY REDRAW this image in this style: {custom_style}. "
                f"Transform the entire image into this art style — do NOT preserve the original medium. "
                f"Keep the character's key identifying features recognizable ({identity_desc}). "
                f"Single character portrait, medium shot, clean background."
            )
            style_prompt_gen = (
                f"Character illustration in this style: {custom_style}. "
                f"Character: {name}. {phys_desc} "
                f"Identity features: {identity_desc}. "
                f"Single character portrait, medium shot, clean simple background. "
                f"Professional character design, high detail, consistent proportions."
            )
            detected_style = "custom"
        elif any(kw in phys_lower for kw in ("anime", "shinkai", "ghibli", "animated", "manga", "cel-shaded")):
            # Extract the first sentence of physicalDescription as the detailed style cue
            detected_style = "anime"
            style_detail = phys_desc.split(".")[0] if phys_desc else "anime realism"
            style_prompt_edit = (
                f"COMPLETELY REDRAW this image as a high-end anime illustration. "
                f"Target style: {style_detail}. "
                f"This must look like hand-drawn 2D anime artwork — NOT a photograph, NOT a 3D render, "
                f"NOT a figurine. Transform every element into anime art: large expressive eyes, "
                f"clean linework, soft cel shading, vibrant anime color palette, flat 2D backgrounds. "
                f"Keep the character's key identifying features recognizable ({identity_desc}). "
                f"Single character portrait, medium shot, clean background. "
                f"The output must be indistinguishable from a frame of a Makoto Shinkai or high-budget anime film."
            )
            style_prompt_gen = (
                f"High-end anime character illustration in the style of {style_detail}. "
                f"Character: {name}. {phys_desc} "
                f"Identity features: {identity_desc}. "
                f"Single character portrait, medium shot, clean simple background. "
                f"2D anime artwork, cel shading, expressive eyes, vibrant colors. "
                f"Must look like a frame from a Makoto Shinkai or high-budget anime film. "
                f"Professional anime character design, high detail, consistent proportions."
            )
        elif any(kw in phys_lower for kw in ("noir", "dark", "gritty")):
            detected_style = "noir"
            style_prompt_edit = (
                f"COMPLETELY REDRAW this image in cinematic noir style. "
                f"Dramatic high-contrast lighting, deep shadows, film grain, muted color palette. "
                f"Keep the character's key identifying features ({identity_desc}). "
                f"Single character portrait, medium shot. Must look like a frame from a noir film."
            )
            style_prompt_gen = (
                f"Cinematic noir character portrait. Character: {name}. {phys_desc} "
                f"Identity: {identity_desc}. Dramatic lighting, deep shadows, film grain, "
                f"muted palette. Professional illustration, single portrait, medium shot."
            )
        elif any(kw in phys_lower for kw in ("cartoon", "pixar", "3d render")):
            detected_style = "cartoon"
            style_prompt_edit = (
                f"COMPLETELY REDRAW this image as a stylized 3D character render. "
                f"Clean shading, appealing proportions, Pixar-quality character design. "
                f"Keep the character's key identifying features ({identity_desc}). "
                f"Single character portrait, medium shot, clean background."
            )
            style_prompt_gen = (
                f"Stylized 3D character render, Pixar quality. Character: {name}. {phys_desc} "
                f"Identity: {identity_desc}. Clean shading, appealing proportions, "
                f"professional character design. Single portrait, medium shot, clean background."
            )
        else:
            self._send_json({"error": "No style detected. Enter a style in the prompt dialog, or add "
                            "style keywords (anime, noir, cartoon) to the Physical Description field."}, 400)
            return

        # Output path
        os.makedirs(_pos_previews_dir("char"), exist_ok=True)
        out_filename = f"{char_id}_styled_{int(time.time())}.png"
        out_path = os.path.join(_pos_previews_dir("char"), out_filename)

        try:
            from lib.fal_client import gemini_generate_image, gemini_edit_image
            import shutil as _shutil_st

            if mode == "edit":
                print(f"[STYLE_TRANSFER] {char_id} → Gemini edit (strong style override)")
                paths = gemini_edit_image(
                    prompt=style_prompt_edit,
                    reference_image_paths=[ref_photo_path],
                    resolution="1K",
                    num_images=1,
                )
            else:
                # Text-only: no ref photo contamination, pure style from prompt
                print(f"[STYLE_TRANSFER] {char_id} → Gemini generate (text-only, no ref)")
                paths = gemini_generate_image(
                    prompt=style_prompt_gen,
                    resolution="1K",
                    aspect_ratio="1:1",
                    num_images=1,
                )

            if not paths or not os.path.isfile(paths[0]):
                self._send_json({"error": "Style transfer returned no image"}, 500)
                return

            _shutil_st.move(paths[0], out_path)
            _record_cost(f"style_transfer_{char_id}", "image")

        except Exception as e:
            print(f"[STYLE_TRANSFER] Error: {e}")
            self._send_json({"error": f"Style transfer failed: {e}"}, 500)
            return

        _repo_base = os.path.dirname(os.path.abspath(__file__))
        _rel = os.path.relpath(out_path, _repo_base).replace(os.sep, "/")
        styled_url = "/" + _rel

        # Save as styledReference on the character — sheet gen will prefer this
        _prompt_os.update_character(char_id, {"styledReference": styled_url})

        print(f"[STYLE_TRANSFER] Saved styled reference: {styled_url}")
        self._send_json({
            "ok": True,
            "styled_url": styled_url,
            "mode": mode,
            "style_detected": detected_style,
        })

    def _handle_pos_sheet_generate_fal(self):
        """POST /api/pos/sheets/generate
        Unified sheet generator for POS assets (characters, costumes, environments, references).
        Uses fal.ai Gemini 3.1 Flash image (edit with ref photo if present, else text-to-image).
        Body: {assetType, assetId, sheetType?}
        """
        try:
            body = json.loads(self._read_body()) if self.headers.get("Content-Length") else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        asset_type = (body.get("assetType") or "").lower()
        if asset_type == "prop":
            asset_type = "reference"  # legacy alias
        asset_id = body.get("assetId") or ""
        sheet_type = body.get("sheetType") or "full"
        use_approved_as_ref = bool(body.get("useApprovedAsRef", False))
        edit_instruction = (body.get("editInstruction") or "").strip()

        if asset_type not in ("character", "costume", "environment", "reference"):
            self._send_json({"error": f"Unknown assetType: {asset_type}"}, 400)
            return
        if not asset_id:
            self._send_json({"error": "assetId required"}, 400)
            return

        # Fetch entity
        getters = {
            "character":   _prompt_os.get_character,
            "costume":     _prompt_os.get_costume,
            "environment": _prompt_os.get_environment,
            "reference":   _prompt_os.get_reference,
        }
        entity = getters[asset_type](asset_id)
        if not entity:
            self._send_json({"error": f"{asset_type} not found"}, 404)
            return

        # Resolve reference images (forward-slash URL → disk path) FIRST, so the
        # prompt builder can switch to refs-not-text mode when a real ref exists.
        #
        # Priority for character sheet follow-ups (face_closeup, side, etc.):
        #   1. approvedSheet (the locked identity anchor — 6-angle turnaround)
        #   2. styledReference (anime-styled version of original photo)
        #   3. referencePhoto (raw photo)
        # For the initial 'full' sheet, skip approvedSheet (we're regenerating it).
        ref_photo_paths = []

        def _resolve_url_to_disk(url):
            if not url:
                return None
            base = os.path.dirname(os.path.abspath(__file__))
            for candidate in (
                os.path.join(base, "public", url.lstrip("/")),
                os.path.join(base, url.lstrip("/")),
            ):
                if os.path.isfile(candidate):
                    return candidate
            return None

        if asset_type == "character" and (sheet_type != "full" or use_approved_as_ref):
            approved_sheet = entity.get("approvedSheet", "")
            approved_disk = _resolve_url_to_disk(approved_sheet)
            if approved_disk:
                ref_photo_paths.append(approved_disk)
                tag = "surgical-edit anchor" if use_approved_as_ref else "identity anchor"
                print(f"[POS_SHEET] Using approved sheet as {tag}: {approved_disk}")

        styled_ref = entity.get("styledReference", "") if asset_type == "character" else ""
        styled_disk = _resolve_url_to_disk(styled_ref)
        if styled_disk:
            ref_photo_paths.append(styled_disk)
            print(f"[POS_SHEET] Using styled reference: {styled_disk}")

        # Fallback to raw referencePhoto if nothing else resolved
        if not ref_photo_paths:
            ref_photo = entity.get("referencePhoto") or entity.get("referenceImagePath") or ""
            if ref_photo:
                photo_dirs = {
                    "character":   _pos_photos_dir("char"),
                    "costume":     _pos_photos_dir("costume"),
                    "environment": _pos_photos_dir("env"),
                }
                pdir = photo_dirs.get(asset_type)
                if pdir:
                    m = re.search(r"/api/pos/(?:characters|costumes|environments)/([^/]+)/photo", ref_photo)
                    if m:
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            candidate = os.path.join(pdir, f"{m.group(1)}{ext}")
                            if os.path.isfile(candidate):
                                ref_photo_paths.append(candidate)
                                break
                    elif os.path.isfile(ref_photo):
                        ref_photo_paths.append(ref_photo)

        ref_photo_path = ref_photo_paths[0] if ref_photo_paths else None

        # Build type-specific prompt — if a ref photo is present, the builder
        # suppresses facial/body description (the refs carry identity).
        prompt = self._build_pos_sheet_prompt(asset_type, entity, sheet_type, has_ref_photo=bool(ref_photo_path))

        # Surgical-edit mode: prepend a keep-everything-else-identical directive
        # so Gemini applies only the targeted delta to the approved reference.
        if use_approved_as_ref and edit_instruction and asset_type == "character":
            prompt = (
                "SURGICAL EDIT MODE — Apply ONLY the following targeted change "
                "to the reference character sheet. Keep EVERY other pixel identical: "
                "same pose, same composition, same panel layout, same proportions, "
                "same art style, same lighting, same wardrobe, same accessories, "
                "same emblem shape/color/placement, same background. Do NOT redesign, "
                "recompose, restyle, or re-render from scratch. The reference image "
                "is the authoritative starting point — preserve it in full except "
                f"for the specified edit. EDIT: {edit_instruction}\n\n" + prompt
            )
            print(f"[POS_SHEET] Surgical edit prompt: {edit_instruction[:120]}")

        # Resolve preview output dir
        preview_dirs = {
            "character":   _pos_previews_dir("char"),
            "costume":     _pos_previews_dir("costume"),
            "environment": _pos_previews_dir("env"),
            "reference":   _pos_previews_dir("reference"),
        }
        preview_dir = preview_dirs[asset_type]
        os.makedirs(preview_dir, exist_ok=True)
        out_filename = f"{asset_id}_{sheet_type}_{int(time.time())}.png"
        out_path = os.path.join(preview_dir, out_filename)

        # Call fal.ai Gemini
        try:
            from lib.fal_client import gemini_generate_image, gemini_edit_image
            if ref_photo_path:
                # Character full sheets bake in 7 views (6 body + face inset) — need
                # enough pixels per tile to survive downstream cropping. 2K portrait
                # gives ~1536×2048 total, ~512×683 per body tile.
                is_char_full_sheet = (asset_type == "character" and sheet_type == "full")
                is_reference = (asset_type == "reference")
                sheet_resolution = "2K" if (is_char_full_sheet or is_reference) else "1K"
                if is_char_full_sheet:
                    sheet_aspect = "3:4"
                elif is_reference:
                    sheet_aspect = "1:1"
                else:
                    sheet_aspect = None
                print(f"[POS_SHEET] {asset_type}/{asset_id} → fal.ai Gemini edit w/ {len(ref_photo_paths)} ref(s), {sheet_resolution} {sheet_aspect or 'auto'}")
                paths = gemini_edit_image(
                    prompt=prompt,
                    reference_image_paths=ref_photo_paths,
                    resolution=sheet_resolution,
                    num_images=1,
                    aspect_ratio=sheet_aspect,
                )
            else:
                is_char_full_sheet = (asset_type == "character" and sheet_type == "full")
                is_reference = (asset_type == "reference")
                sheet_resolution = "2K" if (is_char_full_sheet or is_reference) else "1K"
                if is_char_full_sheet:
                    sheet_aspect = "3:4"
                elif is_reference:
                    sheet_aspect = "1:1"
                else:
                    sheet_aspect = "16:9"
                print(f"[POS_SHEET] {asset_type}/{asset_id} → fal.ai Gemini generate {sheet_resolution} {sheet_aspect}")
                paths = gemini_generate_image(
                    prompt=prompt,
                    resolution=sheet_resolution,
                    aspect_ratio=sheet_aspect,
                    num_images=1,
                )
        except Exception as e:
            print(f"[POS_SHEET] fal.ai error: {e}")
            self._send_json({"error": f"fal.ai Gemini failed: {e}"}, 500)
            return

        if not paths or not os.path.isfile(paths[0]):
            self._send_json({"error": "fal.ai returned no image"}, 500)
            return

        # Move the fal temp file into our preview dir
        import shutil as _shutil
        _shutil.move(paths[0], out_path)

        _record_cost(f"pos_sheet_{asset_type}_{asset_id}", "image")

        # Build the public URL from the actual disk path so it matches
        # wherever _pos_previews_dir wrote the file (char_previews/, etc.)
        _repo_base = os.path.dirname(os.path.abspath(__file__))
        _rel = os.path.relpath(out_path, _repo_base).replace(os.sep, "/")
        sheet_url = "/" + _rel
        sheet_data = {
            "url": sheet_url,
            "type": sheet_type,
            "model": "fal-gemini-3.1-flash",
            "generatedAt": time.time(),
        }
        _prompt_os.add_sheet_image(asset_type, asset_id, sheet_data)

        # Also drop into previewImage for thumbnail use
        updates = {"previewImage": sheet_url}
        if asset_type == "character":
            _prompt_os.update_character(asset_id, updates)
        elif asset_type == "costume":
            _prompt_os.update_costume(asset_id, updates)
        elif asset_type == "environment":
            _prompt_os.update_environment(asset_id, updates)
        elif asset_type == "reference":
            _prompt_os.update_reference(asset_id, updates)

        self._send_json({
            "ok": True,
            "sheet_url": sheet_url,
            "preview_url": sheet_url,
            "engine": "fal-gemini-3.1-flash",
            "used_ref_photo": bool(ref_photo_path),
        })

    def _build_pos_sheet_prompt(self, asset_type, entity, sheet_type, has_ref_photo=False):
        """Assemble a type-specific sheet prompt from POS entity fields.

        When has_ref_photo=True, facial/body description is suppressed — the
        refs carry identity, the prompt carries only camera/pose/wardrobe.
        See memory: feedback_refs_not_text.
        """
        name = entity.get("name", "")
        if asset_type == "character":
            # Fields that describe the subject's body/face — suppressed when refs present
            identity_fields = ("physicalDescription", "hair", "skinTone", "bodyType",
                               "ageRange", "distinguishingFeatures", "defaultExpression")
            # Fields that describe wardrobe/pose — kept regardless (action-driven)
            wardrobe_fields = ("posture", "outfitDescription")

            # Identity-mark detection (for callout panel + vertical-angle row).
            # Explicit field wins; otherwise scan body-copy for emblem keywords.
            identity_mark = (entity.get("identityMark") or "").strip()
            if not identity_mark:
                haystack = " ".join(str(entity.get(f, "")) for f in
                                    ("physicalDescription", "distinguishingFeatures")).lower()
                mark_keywords = ("emblem", "crescent", "moon mark", "tattoo", "birthmark",
                                 "scar", "sigil", "insignia", "brand", "signet")
                if any(k in haystack for k in mark_keywords):
                    # Pull the sentence containing the first keyword for the callout.
                    raw = " ".join(str(entity.get(f, "")) for f in
                                   ("physicalDescription", "distinguishingFeatures"))
                    for sent in raw.replace(";", ".").split("."):
                        sl = sent.lower()
                        if any(k in sl for k in mark_keywords):
                            identity_mark = sent.strip()
                            break
            has_identity_mark = bool(identity_mark)

            parts = []
            if not has_ref_photo:
                for f in identity_fields:
                    v = entity.get(f)
                    if v:
                        parts.append(str(v))
            for f in wardrobe_fields:
                v = entity.get(f)
                if v:
                    parts.append(str(v))
            acc = entity.get("accessories")
            if acc:
                if isinstance(acc, list):
                    acc = ", ".join(str(a) for a in acc if a)
                parts.append(str(acc))
            desc = ", ".join(p for p in parts if p)

            if has_ref_photo:
                # Refs-anchored: describe camera/pose/lighting only. The reference
                # image preserves identity (face, proportions, skin, build).
                # However, we DO read physicalDescription for rendering STYLE cues
                # (e.g. "anime", "Shinkai", "noir") — style != identity.
                subject_clause = f"Wardrobe and styling: {desc}. " if desc else ""
                phys_desc = entity.get("physicalDescription", "")
                # Detect style from physical description
                style_kw = "photorealistic"
                style_detail = "high detail, natural lighting"
                phys_lower = phys_desc.lower() if phys_desc else ""
                if any(kw in phys_lower for kw in ("anime", "shinkai", "ghibli", "animated", "manga", "cel-shaded")):
                    style_kw = "high-end anime style"
                    style_detail = phys_desc.split(".")[0] if phys_desc else "anime realism"
                elif any(kw in phys_lower for kw in ("noir", "dark", "gritty")):
                    style_kw = "cinematic noir style"
                    style_detail = "dramatic lighting, high contrast"
                elif any(kw in phys_lower for kw in ("cartoon", "pixar", "3d render")):
                    style_kw = "stylized 3D render"
                    style_detail = "clean shading, appealing proportions"

                if sheet_type == "face_closeup":
                    return (f"{style_kw.title()} close-up portrait of the character shown in the reference images. "
                            f"Match the character's front-facing view exactly — same facial features, "
                            f"same markings, same proportions. Use the EXACT eye color and glow intensity "
                            f"from the reference. "
                            f"{subject_clause}{style_detail}. "
                            f"Head-and-shoulders framing, soft cinematic key light, "
                            f"solid neutral dark gray studio background (no trees, no scenery, no environment). "
                            f"Shallow depth of field, 85mm lens look. "
                            f"Do NOT add any jewelry, necklace, pendant, collar, chain, or accessory "
                            f"that is not visible in the reference images. "
                            f"Do NOT add background scenery. Preserve every identity detail from the "
                            f"reference — do not invent new features.")
                if sheet_type == "side":
                    return (f"{style_kw.title()} side-profile portrait of the character shown in the reference images. "
                            f"Match the character's right-profile view exactly — same facial features, "
                            f"same markings, same proportions. "
                            f"{subject_clause}{style_detail}. "
                            f"90-degree profile view, studio lighting, neutral background, "
                            f"sharp focus. Preserve the exact identity from the reference.")
                if has_identity_mark:
                    return (f"Professional character model sheet in {style_kw}: a single image "
                            f"containing FOUR ROWS on a clean neutral dark-gray studio background. "
                            f"TOP ROW — three equal tiles side by side, full-body: "
                            f"FRONT VIEW, FRONT THREE-QUARTER VIEW, RIGHT SIDE PROFILE. "
                            f"SECOND ROW — three equal tiles side by side, full-body: "
                            f"BACK THREE-QUARTER VIEW, BACK VIEW, LEFT SIDE PROFILE. "
                            f"THIRD ROW — three equal HEAD-AND-SHOULDERS tiles at different vertical "
                            f"angles showing how the identity mark reads from each: "
                            f"HEAD TILTED UP (looking up), HEAD BOWED ~30° DOWN, TOP-DOWN BIRDS-EYE "
                            f"VIEW OF THE CROWN. These tiles establish exactly how the mark is "
                            f"visible (or correctly hidden) at each angle. "
                            f"BOTTOM ROW — TWO tiles side by side at equal width: "
                            f"LEFT tile = LARGE HEAD-AND-SHOULDERS FACE CLOSEUP, front-facing, "
                            f"with the identity mark rendered in full detail; "
                            f"RIGHT tile = ISOLATED IDENTITY-MARK CALLOUT — zoom 2x on just the "
                            f"mark itself against plain background, showing exact shape, stroke "
                            f"weight, color, and glow. Label below: 'IDENTITY MARK — exact shape, "
                            f"color, placement'. The callout is the authoritative reference for "
                            f"downstream renders. "
                            f"Identity mark: {identity_mark}. "
                            f"{subject_clause}{style_detail}. "
                            f"ALL tiles show the IDENTICAL character — same features, same "
                            f"mark shape and color, same proportions. The mark MUST appear ONLY "
                            f"where physically present (e.g. forehead); when the angle hides it "
                            f"(back view, top-down showing crown, bowed head) it MUST NOT be "
                            f"painted onto any other surface. Preserve the exact identity from "
                            f"the reference. Do NOT add jewelry, necklace, pendant, collar, "
                            f"chain, or any accessory not visible in the reference. Do NOT "
                            f"add background scenery.")
                return (f"Professional character model sheet in {style_kw}: a single image "
                        f"containing THREE ROWS on a clean neutral dark-gray studio background. "
                        f"TOP ROW — three equal tiles side by side, each a full-body view: "
                        f"FRONT VIEW, FRONT THREE-QUARTER VIEW, RIGHT SIDE PROFILE. "
                        f"MIDDLE ROW — three equal tiles side by side, each a full-body view: "
                        f"BACK THREE-QUARTER VIEW, BACK VIEW, LEFT SIDE PROFILE. "
                        f"BOTTOM ROW — one LARGE HEAD-AND-SHOULDERS FACE CLOSEUP "
                        f"spanning the full width of the row, rendered in high detail with the "
                        f"same features as the body views above. "
                        f"{subject_clause}{style_detail}. "
                        f"All seven views show the IDENTICAL character — same features, same "
                        f"markings, same colors, same proportions. Preserve the exact identity "
                        f"from the reference. Do NOT add jewelry, necklace, pendant, collar, "
                        f"chain, or any accessory that is not visible in the reference. Do NOT "
                        f"add background scenery.")

            # No ref photo — text-only path (original behavior)
            desc = desc or name or "character"
            if sheet_type == "face_closeup":
                return (f"Photorealistic close-up portrait of {desc}. "
                        f"Head-and-shoulders framing, soft cinematic key light, "
                        f"neutral background, shallow depth of field, 85mm lens look, "
                        f"high detail skin texture, natural expression.")
            if sheet_type == "side":
                return (f"Photorealistic side-profile portrait of {desc}. "
                        f"90-degree profile view, studio lighting, neutral background, "
                        f"sharp focus, true-to-life proportions.")
            if has_identity_mark:
                return (f"Professional character model sheet showing the SAME character arranged "
                        f"in FOUR ROWS on a neutral dark-gray studio background. "
                        f"Top row: FRONT, FRONT THREE-QUARTER, RIGHT SIDE PROFILE — three equal full-body tiles. "
                        f"Second row: BACK THREE-QUARTER, BACK, LEFT SIDE PROFILE — three equal full-body tiles. "
                        f"Third row: HEAD-AND-SHOULDERS at three vertical angles — HEAD TILTED UP, "
                        f"HEAD BOWED ~30° DOWN, TOP-DOWN BIRDS-EYE VIEW OF CROWN — showing how the "
                        f"identity mark reads at each angle. "
                        f"Bottom row: TWO equal tiles — LEFT = LARGE HEAD-AND-SHOULDERS FACE "
                        f"CLOSEUP front-facing, RIGHT = ISOLATED IDENTITY-MARK CALLOUT at 2x zoom "
                        f"on plain background with label 'IDENTITY MARK'. "
                        f"Identity mark: {identity_mark}. "
                        f"Character appearance: {desc}. Identical features across all views, "
                        f"mark only where physically present, never painted onto hidden surfaces. "
                        f"Photorealistic, high detail.")
            return (f"Professional character model sheet showing the SAME character in seven views "
                    f"arranged in THREE ROWS on a neutral dark-gray studio background. "
                    f"Top row: FRONT, FRONT THREE-QUARTER, RIGHT SIDE PROFILE — three equal full-body tiles. "
                    f"Middle row: BACK THREE-QUARTER, BACK, LEFT SIDE PROFILE — three equal full-body tiles. "
                    f"Bottom row: one LARGE HEAD-AND-SHOULDERS FACE CLOSEUP spanning the full row width. "
                    f"Character appearance: {desc}. Identical features across all views, consistent "
                    f"proportions, photorealistic, high detail.")
        if asset_type == "costume":
            parts = []
            for f in ("description", "upperBody", "lowerBody", "footwear"):
                v = entity.get(f)
                if v:
                    parts.append(str(v))
            acc = entity.get("accessories")
            if acc:
                if isinstance(acc, list):
                    acc = ", ".join(str(a) for a in acc if a)
                parts.append(f"accessories: {acc}")
            if entity.get("colorPalette"):
                parts.append(f"palette: {entity['colorPalette']}")
            if entity.get("materialNotes"):
                parts.append(f"fabric: {entity['materialNotes']}")
            desc = ", ".join(p for p in parts if p) or name or "costume"
            return (f"Fashion reference sheet: {desc}. Flat lay and front-view on "
                    f"mannequin, clean neutral background, even studio lighting, "
                    f"detailed fabric texture, photorealistic.")
        if asset_type == "environment":
            parts = []
            for f in ("description", "location", "architecture", "weather", "timeOfDay"):
                v = entity.get(f)
                if v:
                    parts.append(str(v))
            if entity.get("lighting"):
                parts.append(f"lighting: {entity['lighting']}")
            if entity.get("atmosphere"):
                parts.append(f"atmosphere: {entity['atmosphere']}")
            key_props = entity.get("keyProps") or entity.get("props")
            if key_props:
                if isinstance(key_props, list):
                    key_props = ", ".join(str(p) for p in key_props if p)
                parts.append(f"key props: {key_props}")
            if entity.get("architectureNotes"):
                parts.append(f"architecture: {entity['architectureNotes']}")
            if entity.get("materialNotes"):
                parts.append(f"materials: {entity['materialNotes']}")
            desc = ", ".join(p for p in parts if p) or name or "environment"
            # Style detection — mirror the character path so env sheets inherit
            # the project's visual language (anime / noir / stylized) instead of
            # being locked to photorealistic. Style lives in the entity's
            # description or visualStyle field.
            style_hint = (desc + " " + str(entity.get("visualStyle", ""))).lower()
            style_kw = "photorealistic, true-to-life lighting"
            if any(kw in style_hint for kw in ("anime", "shinkai", "ghibli", "cel-shaded", "manga", "animated")):
                style_kw = ("high-end anime style, Makoto Shinkai-inspired, cinematic anime realism, "
                            "soft volumetric light, painterly clouds, rich color palette, cel-shaded")
            elif any(kw in style_hint for kw in ("noir", "gritty")):
                style_kw = "cinematic noir style, dramatic lighting, high contrast, film grain"
            elif any(kw in style_hint for kw in ("cartoon", "pixar", "3d render")):
                style_kw = "stylized 3D render, clean shading, appealing proportions"
            return (f"Wide establishing cinematic shot of {desc}. No people, no characters, "
                    f"{style_kw}, high detail, atmospheric depth, "
                    f"strong sense of place.")
        # reference (motif / prop)
        parts = []
        for f in ("description", "motif_category", "category", "usage_notes", "drift_rules"):
            v = entity.get(f)
            if v:
                if isinstance(v, (list, tuple)):
                    v = ", ".join(str(x) for x in v)
                parts.append(str(v))
        desc = ". ".join(p for p in parts if p) or name or "reference motif"
        # Motif-category steers composition/framing. Objects want product-style,
        # body_parts want macro close-up, textures want tight fur/surface framing.
        motif_cat = (entity.get("motif_category") or "").lower()
        if motif_cat == "texture":
            framing = "Macro texture reference plate — tight on surface, even lighting, fills frame edge-to-edge"
        elif motif_cat == "body_part":
            framing = "Macro close-up reference of a body part, isolated subject, shallow DOF background, no full figure"
        elif motif_cat == "silhouette":
            framing = "Silhouette reference — full subject in single-tone shape against neutral backdrop"
        else:  # object, or blank
            framing = "Product photography reference, clean neutral background, multiple angles or isolated single-angle"
        return (f"{framing}: {desc}. Photorealistic, sharp focus, high detail, "
                f"controlled lighting consistent with Shinkai anime realism.")

    # ---- Lyrics Sync ----

    def _handle_lyrics(self):
        """POST /api/lyrics - Apply lyrics overlay to final video."""
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
        lyrics_text = params.get("lyrics", "")
        timestamps = params.get("timestamps", [])
        duration = params.get("duration", None)
        target = params.get("target", "auto")
        if not lyrics_text.strip():
            self._send_json({"error": "No lyrics provided"}, 400)
            return
        lines = [l.strip() for l in lyrics_text.strip().split("\n") if l.strip()]
        lyrics_data = []
        if timestamps and len(timestamps) >= len(lines):
            for i, line in enumerate(lines):
                st = float(timestamps[i])
                en = float(timestamps[i + 1]) if i + 1 < len(timestamps) else st + 4.0
                lyrics_data.append({"text": line, "start": st, "end": en})
        else:
            if not duration:
                plan = _load_scene_plan() if target == "auto" else _load_manual_plan()
                if plan and plan.get("scenes"):
                    duration = max(s.get("end_sec", s.get("duration", 5)) for s in plan["scenes"])
                else:
                    duration = len(lines) * 4.0
            seg = float(duration) / max(len(lines), 1)
            for i, line in enumerate(lines):
                lyrics_data.append({"text": line, "start": i * seg, "end": (i + 1) * seg})
        # Store in plan
        if target == "auto":
            plan = _load_scene_plan()
            if plan:
                plan["lyrics"] = lyrics_data
                with _plan_file_lock:
                    with open(SCENE_PLAN_PATH, "w") as f:
                        json.dump(plan, f, indent=2)
        else:
            plan = _load_manual_plan()
            plan["lyrics"] = lyrics_data
            _save_manual_plan(plan)
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"
        thread = threading.Thread(target=_run_lyrics_overlay, args=(lyrics_data, target), daemon=True)
        thread.start()
        self._send_json({"ok": True, "message": "Applying lyrics overlay", "lines": len(lyrics_data)})

    # ---- Batch Export ----

    def _handle_batch_export(self):
        """POST /api/batch-export - Export final video in all 4 aspect ratios."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return
            _reset_state()
            gen_state["running"] = True
            gen_state["phase"] = "starting"
        thread = threading.Thread(target=_run_batch_export, daemon=True)
        thread.start()
        self._send_json({"ok": True, "message": "Batch export started"})

    def _handle_list_exports(self):
        """GET /api/exports - List available export files."""
        exports = []
        if os.path.isdir(EXPORTS_DIR):
            for fname in sorted(os.listdir(EXPORTS_DIR)):
                fpath = os.path.join(EXPORTS_DIR, fname)
                if os.path.isfile(fpath) and fname.endswith(".mp4"):
                    exports.append({"filename": fname, "url": f"/api/exports/{fname}", "size": os.path.getsize(fpath)})
        self._send_json({"exports": exports})

    # ---- Scene Split ----

    def _handle_manual_split_scene(self, scene_id: str):
        """POST /api/manual/scene/:id/split - Split a manual scene at a given percentage."""
        body = self._read_body()
        try:
            params = json.loads(body) if body else {}
        except json.JSONDecodeError:
            params = {}
        split_pct = float(params.get("split_pct", 0.5))
        split_pct = max(0.05, min(0.95, split_pct))

        plan = _load_manual_plan()
        scene = None
        scene_idx = None
        for i, s in enumerate(plan["scenes"]):
            if s["id"] == scene_id:
                scene = s
                scene_idx = i
                break
        if scene is None:
            self._send_json({"error": "Scene not found"}, 404)
            return
        new_id_a = str(_uuid.uuid4())[:8]
        new_id_b = str(_uuid.uuid4())[:8]
        orig_dur = scene.get("duration", 5)
        dur_a = max(2, round(orig_dur * split_pct))
        dur_b = max(2, orig_dur - dur_a)
        scene_a = {"id": new_id_a, "prompt": scene.get("prompt", ""), "duration": dur_a,
                    "transition": scene.get("transition", "crossfade"),
                    "photo_path": scene.get("photo_path"), "clip_path": None, "has_clip": False,
                    "video_path": None, "vocal_path": None, "vocal_volume": 80}
        scene_b = {"id": new_id_b, "prompt": scene.get("prompt", ""), "duration": dur_b,
                    "transition": "crossfade", "photo_path": None, "clip_path": None, "has_clip": False,
                    "video_path": None, "vocal_path": None, "vocal_volume": 80}
        if scene.get("clip_path") and os.path.isfile(scene["clip_path"]):
            try:
                p1, p2 = split_clip(scene["clip_path"], MANUAL_CLIPS_DIR, scene_id,
                                    split_pct=split_pct)
                scene_a["clip_path"] = p1
                scene_a["has_clip"] = True
                scene_b["clip_path"] = p2
                scene_b["has_clip"] = True
            except Exception:
                pass
        plan["scenes"] = plan["scenes"][:scene_idx] + [scene_a, scene_b] + plan["scenes"][scene_idx + 1:]
        _save_manual_plan(plan)
        self._send_json({"ok": True, "scenes": [scene_a, scene_b]})

    def _handle_auto_split_scene(self, index: int):
        """POST /api/scenes/:index/split - Split an auto scene into two halves."""
        plan = _load_scene_plan()
        if not plan:
            self._send_json({"error": "No scene plan found"}, 404)
            return
        scenes = plan["scenes"]
        if index < 0 or index >= len(scenes):
            self._send_json({"error": f"Scene index {index} out of range"}, 400)
            return
        scene = scenes[index]
        start = scene.get("start_sec", 0)
        end = scene.get("end_sec", start + scene.get("duration", 5))
        mid = (start + end) / 2.0
        sa = dict(scene)
        sa["end_sec"] = round(mid, 3)
        sa["duration"] = round(mid - start, 3)
        sa["index"] = index
        sb = dict(scene)
        sb["start_sec"] = round(mid, 3)
        sb["duration"] = round(end - mid, 3)
        sb["index"] = index + 1
        sb["transition"] = "crossfade"
        sb["clip_path"] = None
        if scene.get("clip_path") and os.path.isfile(scene["clip_path"]):
            try:
                p1, p2 = split_clip(scene["clip_path"], CLIPS_DIR, f"clip_{index:03d}")
                sa["clip_path"] = p1
                sb["clip_path"] = p2
            except Exception:
                sa["clip_path"] = scene.get("clip_path")
        new_scenes = scenes[:index] + [sa, sb] + scenes[index + 1:]
        for i, s in enumerate(new_scenes):
            s["index"] = i
        plan["scenes"] = new_scenes
        with _plan_file_lock:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
        self._send_json({"ok": True, "total_scenes": len(new_scenes)})

    # ---- Style Consistency Lock ----

    def _handle_style_lock(self):
        """POST /api/style-lock - Lock style keywords across all scene prompts."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        style_lock = params.get("style_lock", "")
        enabled = params.get("enabled", True)
        target = params.get("target", "manual")
        if target == "manual":
            plan = _load_manual_plan()
            if enabled and style_lock:
                plan["style_lock"] = style_lock
                for s in plan["scenes"]:
                    prompt = s.get("prompt", "")
                    if style_lock not in prompt:
                        s["prompt"] = (prompt.rstrip(", ") + ", " + style_lock) if prompt else style_lock
            else:
                plan.pop("style_lock", None)
            _save_manual_plan(plan)
        else:
            plan = _load_scene_plan()
            if not plan:
                self._send_json({"error": "No scene plan found"}, 404)
                return
            if enabled and style_lock:
                plan["style_lock"] = style_lock
                for s in plan["scenes"]:
                    prompt = s.get("prompt", "")
                    if style_lock not in prompt:
                        s["prompt"] = (prompt.rstrip(", ") + ", " + style_lock) if prompt else style_lock
            else:
                plan.pop("style_lock", None)
            with _plan_file_lock:
                with open(SCENE_PLAN_PATH, "w") as f:
                    json.dump(plan, f, indent=2)
        self._send_json({"ok": True, "style_lock": style_lock, "enabled": enabled})

    # ---- Feature 1: Auto-describe photo ----

    def _handle_describe_photo(self, scene_id: str):
        """Use Grok vision to auto-describe a scene's photo for use as prompt."""
        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                photo_path = s.get("photo_path", "")
                if not photo_path or not os.path.isfile(photo_path):
                    self._send_json({"error": "Scene has no photo uploaded"}, 400)
                    return
                try:
                    description = describe_photo(photo_path)
                    self._send_json({"ok": True, "description": description})
                except Exception as e:
                    self._send_json({"error": f"Photo description failed: {e}"}, 500)
                return
        self._send_json({"error": "Scene not found"}, 404)

    # ---- Feature 3: Beat-sync hard cuts ----

    def _handle_beat_sync(self):
        """Apply beat-synced hard cuts to the final video."""
        with gen_lock:
            if gen_state["running"]:
                self._send_json({"error": "Generation already in progress"}, 409)
                return

        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            params = {}

        target = params.get("target", "manual")

        # Find video and analysis data
        if target == "manual":
            plan = _load_manual_plan()
            video_path = plan.get("output_path", os.path.join(OUTPUT_DIR, "manual_final_video.mp4"))
        else:
            plan = _load_scene_plan()
            if not plan:
                self._send_json({"error": "No scene plan found"}, 404)
                return
            video_path = plan.get("output_path", os.path.join(OUTPUT_DIR, "final_video.mp4"))

        if not os.path.isfile(video_path):
            self._send_json({"error": "Final video not found. Stitch first."}, 400)
            return

        # Get beat timestamps from analysis or params
        beats = params.get("beats", [])
        sections = params.get("sections", [])

        if not beats:
            # Try to get from stored analysis
            analysis = gen_state.get("analysis")
            if analysis:
                beats = analysis.get("beats", [])
                sections = analysis.get("sections", [])
            else:
                # Try to analyze the audio
                song_path = plan.get("song_path")
                if song_path and os.path.isfile(song_path):
                    try:
                        analysis = analyze(song_path)
                        beats = analysis.get("beats", [])
                        sections = analysis.get("sections", [])
                    except Exception:
                        pass

        if not beats:
            self._send_json({"error": "No beat data available. Upload and analyze audio first."}, 400)
            return

        try:
            temp_out = video_path + ".beatsync_tmp.mp4"
            apply_beat_sync_cuts(video_path, temp_out, beats, sections)
            os.replace(temp_out, video_path)
            self._send_json({"ok": True, "message": "Beat-sync cuts applied", "beats_used": len(beats)})
        except Exception as e:
            self._send_json({"error": f"Beat-sync failed: {e}"}, 500)

    # ---- Feature 4: Watermark upload and apply ----

    def _handle_watermark_upload(self):
        """Upload a PNG watermark file."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        file_data = None
        for part in parts:
            if part["name"] == "file" or part["filename"]:
                file_data = part["data"]
                break

        if file_data is None:
            self._send_json({"error": "No file found"}, 400)
            return

        with open(WATERMARK_PATH, "wb") as f:
            f.write(file_data)
        self._send_json({"ok": True, "path": WATERMARK_PATH})

    def _handle_watermark_apply(self):
        """Apply watermark to the final video."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            params = {}

        if not os.path.isfile(WATERMARK_PATH):
            self._send_json({"error": "No watermark uploaded. Upload first via /api/watermark/upload"}, 400)
            return

        position = params.get("position", "bottom_right")
        opacity = params.get("opacity", 50)
        target = params.get("target", "manual")

        if target == "manual":
            video_path = os.path.join(OUTPUT_DIR, "manual_final_video.mp4")
        else:
            video_path = os.path.join(OUTPUT_DIR, "final_video.mp4")

        if not os.path.isfile(video_path):
            self._send_json({"error": "Final video not found. Stitch first."}, 400)
            return

        try:
            temp_out = video_path + ".watermark_tmp.mp4"
            apply_watermark(video_path, temp_out, WATERMARK_PATH, position, opacity)
            os.replace(temp_out, video_path)
            self._send_json({"ok": True, "message": "Watermark applied"})
        except Exception as e:
            self._send_json({"error": f"Watermark failed: {e}"}, 500)

    # ---- Feature 5: Credits roll ----

    def _handle_credits(self):
        """Generate and append credits roll to the final video."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        title = params.get("title", "")
        artist = params.get("artist", "")
        extra_text = params.get("extra_text", "")
        target = params.get("target", "manual")

        if target == "manual":
            video_path = os.path.join(OUTPUT_DIR, "manual_final_video.mp4")
        else:
            video_path = os.path.join(OUTPUT_DIR, "final_video.mp4")

        if not os.path.isfile(video_path):
            self._send_json({"error": "Final video not found. Stitch first."}, 400)
            return

        try:
            # Generate credits clip
            credits_path = os.path.join(OUTPUT_DIR, "credits_roll.mp4")
            generate_credits(credits_path, title, artist, extra_text)

            # Concatenate credits to the end of the video
            concat_list = os.path.join(OUTPUT_DIR, "_credits_concat.txt")
            with open(concat_list, "w") as f:
                f.write(f"file '{video_path}'\n")
                f.write(f"file '{credits_path}'\n")

            temp_out = video_path + ".credits_tmp.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_list,
                "-c", "copy",
                temp_out,
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=300, **_subprocess_kwargs())
            os.replace(temp_out, video_path)

            # Clean up
            if os.path.isfile(concat_list):
                os.remove(concat_list)
            if os.path.isfile(credits_path):
                os.remove(credits_path)

            self._send_json({"ok": True, "message": "Credits roll added"})
        except Exception as e:
            self._send_json({"error": f"Credits generation failed: {e}"}, 500)

    # ---- Feature 6: Thumbnail generator ----

    def _handle_thumbnail(self):
        """Extract thumbnail from final video at a specific timestamp."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            params = {}

        timestamp = params.get("timestamp", -1)
        target = params.get("target", "manual")

        if target == "manual":
            video_path = os.path.join(OUTPUT_DIR, "manual_final_video.mp4")
        else:
            video_path = os.path.join(OUTPUT_DIR, "final_video.mp4")

        if not os.path.isfile(video_path):
            self._send_json({"error": "Final video not found."}, 400)
            return

        try:
            extract_thumbnail(video_path, THUMBNAIL_PATH, timestamp)
            self._send_json({
                "ok": True,
                "url": "/api/thumbnail",
                "message": "Thumbnail extracted",
            })
        except Exception as e:
            self._send_json({"error": f"Thumbnail extraction failed: {e}"}, 500)

    def _handle_thumbnail_generate(self):
        """Generate a custom thumbnail via Grok image API."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        prompt = params.get("prompt", "")
        if not prompt:
            self._send_json({"error": "No prompt provided"}, 400)
            return

        try:
            from lib.video_generator import _generate_image, _download
            img_url = _generate_image(prompt)
            _download(img_url, THUMBNAIL_PATH)
            self._send_json({
                "ok": True,
                "url": "/api/thumbnail",
                "message": "Custom thumbnail generated",
            })
        except Exception as e:
            self._send_json({"error": f"Thumbnail generation failed: {e}"}, 500)

    # ---- Feature 8: Multi-photo mood board ----

    def _handle_multi_photo_upload(self, scene_id: str):
        """Upload multiple reference photos for a scene's mood board (up to 4)."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                # Initialize photo_paths if needed
                if "photo_paths" not in s:
                    s["photo_paths"] = []

                photos_added = 0
                for part in parts:
                    if part["filename"] and len(s["photo_paths"]) < 4:
                        ext = os.path.splitext(part["filename"])[1] or ".jpg"
                        idx = len(s["photo_paths"])
                        photo_path = os.path.join(SCENE_PHOTOS_DIR, f"{scene_id}_mb{idx}{ext}")
                        with open(photo_path, "wb") as f:
                            f.write(part["data"])
                        s["photo_paths"].append(photo_path)
                        photos_added += 1

                        # Also set primary photo if not set
                        if not s.get("photo_path"):
                            s["photo_path"] = photo_path

                _save_manual_plan(plan)
                photo_urls = []
                for pi, pp in enumerate(s["photo_paths"]):
                    if os.path.isfile(pp):
                        photo_urls.append(f"/api/manual/scene-photo/{scene_id}?idx={pi}")
                self._send_json({
                    "ok": True,
                    "photos_added": photos_added,
                    "total_photos": len(s["photo_paths"]),
                    "photo_urls": photo_urls,
                })
                return

        self._send_json({"error": "Scene not found"}, 404)

    # ---- Feature 9: Multi-track audio ----

    def _handle_upload_audio_tracks(self):
        """Upload separate vocal and instrumental audio tracks."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        vocal_path = None
        instrumental_path = None

        for part in parts:
            if part["name"] == "vocal" and part["filename"]:
                ext = os.path.splitext(part["filename"])[1] or ".mp3"
                vocal_path = os.path.join(AUDIO_TRACKS_DIR, f"vocal{ext}")
                with open(vocal_path, "wb") as f:
                    f.write(part["data"])
            elif part["name"] == "instrumental" and part["filename"]:
                ext = os.path.splitext(part["filename"])[1] or ".mp3"
                instrumental_path = os.path.join(AUDIO_TRACKS_DIR, f"instrumental{ext}")
                with open(instrumental_path, "wb") as f:
                    f.write(part["data"])

        result = {"ok": True}
        if vocal_path:
            result["vocal"] = vocal_path
        if instrumental_path:
            result["instrumental"] = instrumental_path

        self._send_json(result)

    def _handle_mix_audio(self):
        """Mix vocal and instrumental tracks with custom levels."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            params = {}

        vocal_level = params.get("vocal_level", 50)
        instrumental_level = params.get("instrumental_level", 50)

        # Find track files
        vocal_path = None
        instrumental_path = None
        for fname in os.listdir(AUDIO_TRACKS_DIR):
            fpath = os.path.join(AUDIO_TRACKS_DIR, fname)
            if fname.startswith("vocal"):
                vocal_path = fpath
            elif fname.startswith("instrumental"):
                instrumental_path = fpath

        if not vocal_path or not instrumental_path:
            self._send_json({"error": "Both vocal and instrumental tracks required. Upload first."}, 400)
            return

        try:
            mixed_path = os.path.join(UPLOADS_DIR, "mixed_audio.mp3")
            mix_audio_tracks(vocal_path, instrumental_path, mixed_path,
                             vocal_level, instrumental_level)

            # Update the manual plan to use the mixed audio
            plan = _load_manual_plan()
            plan["song_path"] = mixed_path
            _save_manual_plan(plan)

            self._send_json({
                "ok": True,
                "message": "Audio tracks mixed",
                "path": mixed_path,
                "vocal_level": vocal_level,
                "instrumental_level": instrumental_level,
            })
        except Exception as e:
            self._send_json({"error": f"Audio mixing failed: {e}"}, 500)

    # ---- Feature: Scene Video Upload ----

    def _handle_manual_upload_video(self, scene_id: str):
        """Upload a user video clip for a manual scene (replaces generation)."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()

        # Check file size (max 100MB)
        if len(body) > 105 * 1024 * 1024:
            self._send_json({"error": "File too large. Maximum 100MB."}, 400)
            return

        parts = self._parse_multipart(body, boundary)

        file_data = None
        file_ext = ".mp4"
        for part in parts:
            if part["name"] == "video" or part["filename"]:
                file_data = part["data"]
                if part["filename"]:
                    ext = os.path.splitext(part["filename"])[1].lower()
                    if ext in (".mp4", ".webm", ".mov"):
                        file_ext = ext
                break

        if file_data is None:
            self._send_json({"error": "No video file found"}, 400)
            return

        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                # Remove old video if exists
                old_vid = s.get("video_path", "")
                if old_vid and os.path.isfile(old_vid):
                    os.remove(old_vid)

                video_path = os.path.join(SCENE_VIDEOS_DIR, f"{scene_id}{file_ext}")
                with open(video_path, "wb") as f:
                    f.write(file_data)
                s["video_path"] = video_path
                # Also set as clip_path so it's used directly in stitch
                s["clip_path"] = video_path
                s["has_clip"] = True
                _save_manual_plan(plan)
                self._send_json({
                    "ok": True,
                    "video_url": f"/api/manual/scene-video/{scene_id}",
                    "message": "Video uploaded - this clip IS the scene",
                })
                return

        self._send_json({"error": "Scene not found"}, 404)

    def _handle_get_scene_video(self, scene_id: str):
        """Serve an uploaded scene video."""
        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                vp = s.get("video_path", "")
                if vp and os.path.isfile(vp):
                    self._send_file(vp)
                    return
        self.send_error(404, "Video not found")

    # ---- Feature: Per-Scene Vocal Upload ----

    def _handle_manual_upload_vocal(self, scene_id: str):
        """Upload a voiceover audio clip for a manual scene."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        file_data = None
        file_ext = ".mp3"
        volume = 80

        for part in parts:
            if part["name"] == "vocal" or (part["filename"] and part["name"] != "volume"):
                file_data = part["data"]
                if part["filename"]:
                    ext = os.path.splitext(part["filename"])[1].lower()
                    if ext in (".mp3", ".wav", ".m4a", ".ogg"):
                        file_ext = ext
            elif part["name"] == "volume":
                try:
                    volume = int(part["data"].decode().strip())
                except (ValueError, UnicodeDecodeError):
                    pass

        if file_data is None:
            self._send_json({"error": "No vocal file found"}, 400)
            return

        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                # Remove old vocal if exists
                old_voc = s.get("vocal_path", "")
                if old_voc and os.path.isfile(old_voc):
                    os.remove(old_voc)

                vocal_path = os.path.join(SCENE_VOCALS_DIR, f"{scene_id}{file_ext}")
                with open(vocal_path, "wb") as f:
                    f.write(file_data)
                s["vocal_path"] = vocal_path
                s["vocal_volume"] = volume
                _save_manual_plan(plan)
                self._send_json({
                    "ok": True,
                    "message": "Voiceover uploaded",
                    "vocal_volume": volume,
                })
                return

        self._send_json({"error": "Scene not found"}, 404)

    # ---- Feature: Auto Beat Alignment ----

    def _handle_auto_align_beats(self):
        """Align scene boundaries to beat timestamps."""
        body = self._read_body()
        try:
            params = json.loads(body) if body else {}
        except json.JSONDecodeError:
            params = {}

        target = params.get("target", "manual")

        if target == "manual":
            plan = _load_manual_plan()
        else:
            plan = _load_scene_plan()
            if not plan:
                self._send_json({"error": "No scene plan found"}, 404)
                return

        # Get beat timestamps
        beats = params.get("beats", [])
        if not beats:
            # Try to analyze audio
            song_path = plan.get("song_path")
            if song_path and os.path.isfile(song_path):
                try:
                    analysis = analyze(song_path)
                    beats = analysis.get("beats", [])
                except Exception:
                    pass

        if not beats:
            self._send_json({"error": "No beat data available. Upload and analyze audio first."}, 400)
            return

        scenes = plan.get("scenes", [])
        if not scenes:
            self._send_json({"error": "No scenes to align"}, 400)
            return

        aligned = align_scenes_to_beats(scenes, beats)
        plan["scenes"] = aligned

        if target == "manual":
            _save_manual_plan(plan)
        else:
            with _plan_file_lock:
                with open(SCENE_PLAN_PATH, "w") as f:
                    json.dump(plan, f, indent=2)

        self._send_json({
            "ok": True,
            "message": f"Aligned {len(aligned)} scenes to {len(beats)} beats",
            "scenes_aligned": len(aligned),
            "beats_used": len(beats),
        })

    # ---- Feature: Save Full Project with Clips ----

    def _handle_project_save_full(self):
        """Create a zip file containing project.json + all clips, photos, and audio."""
        plan = _load_manual_plan()
        auto_plan = _load_scene_plan()
        tracker = _load_cost_tracker()

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        zip_filename = f"project_full_{timestamp}.zip"
        zip_path = os.path.join(FULL_PROJECTS_DIR, zip_filename)

        project = {
            "version": 2,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "manual_plan": plan,
            "auto_plan": auto_plan,
            "cost_tracker": tracker,
        }

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Write project metadata
                zf.writestr("project.json", json.dumps(project, indent=2))

                # Add clips
                for s in plan.get("scenes", []):
                    cp = s.get("clip_path", "")
                    if cp and os.path.isfile(cp):
                        arcname = f"clips/{os.path.basename(cp)}"
                        zf.write(cp, arcname)
                    # Add uploaded videos
                    vp = s.get("video_path", "")
                    if vp and os.path.isfile(vp):
                        arcname = f"clips/{os.path.basename(vp)}"
                        zf.write(vp, arcname)

                # Add photos
                for s in plan.get("scenes", []):
                    pp = s.get("photo_path", "")
                    if pp and os.path.isfile(pp):
                        arcname = f"photos/{os.path.basename(pp)}"
                        zf.write(pp, arcname)
                    for pp in s.get("photo_paths", []):
                        if pp and os.path.isfile(pp):
                            arcname = f"photos/{os.path.basename(pp)}"
                            zf.write(pp, arcname)

                # Add vocals
                for s in plan.get("scenes", []):
                    voc = s.get("vocal_path", "")
                    if voc and os.path.isfile(voc):
                        arcname = f"vocals/{os.path.basename(voc)}"
                        zf.write(voc, arcname)

                # Add audio track
                song_path = plan.get("song_path", "")
                if song_path and os.path.isfile(song_path):
                    zf.write(song_path, f"audio/{os.path.basename(song_path)}")

            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            self._send_json({
                "ok": True,
                "filename": zip_filename,
                "url": f"/api/full-projects/{zip_filename}",
                "size_mb": round(size_mb, 1),
            })
        except Exception as e:
            self._send_json({"error": f"Failed to create project zip: {e}"}, 500)

    def _handle_project_load_full(self):
        """Load a full project from an uploaded zip file."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()
        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        file_data = None
        for part in parts:
            if part["filename"] and part["filename"].endswith(".zip"):
                file_data = part["data"]
                break

        if file_data is None:
            self._send_json({"error": "No zip file found in upload"}, 400)
            return

        try:
            import io
            with zipfile.ZipFile(io.BytesIO(file_data), "r") as zf:
                # Extract project.json
                project_json = zf.read("project.json")
                project = json.loads(project_json)

                # Extract clips
                for name in zf.namelist():
                    if name.startswith("clips/") and not name.endswith("/"):
                        dest = os.path.join(MANUAL_CLIPS_DIR, os.path.basename(name))
                        with open(dest, "wb") as f:
                            f.write(zf.read(name))
                    elif name.startswith("photos/") and not name.endswith("/"):
                        dest = os.path.join(SCENE_PHOTOS_DIR, os.path.basename(name))
                        with open(dest, "wb") as f:
                            f.write(zf.read(name))
                    elif name.startswith("vocals/") and not name.endswith("/"):
                        dest = os.path.join(SCENE_VOCALS_DIR, os.path.basename(name))
                        with open(dest, "wb") as f:
                            f.write(zf.read(name))
                    elif name.startswith("audio/") and not name.endswith("/"):
                        dest = os.path.join(UPLOADS_DIR, os.path.basename(name))
                        with open(dest, "wb") as f:
                            f.write(zf.read(name))

                # Remap paths in the plan to local paths
                plan = project.get("manual_plan", {})
                for s in plan.get("scenes", []):
                    cp = s.get("clip_path", "")
                    if cp:
                        local_cp = os.path.join(MANUAL_CLIPS_DIR, os.path.basename(cp))
                        if os.path.isfile(local_cp):
                            s["clip_path"] = local_cp
                            s["has_clip"] = True
                    vp = s.get("video_path", "")
                    if vp:
                        local_vp = os.path.join(MANUAL_CLIPS_DIR, os.path.basename(vp))
                        if os.path.isfile(local_vp):
                            s["video_path"] = local_vp
                    pp = s.get("photo_path", "")
                    if pp:
                        local_pp = os.path.join(SCENE_PHOTOS_DIR, os.path.basename(pp))
                        if os.path.isfile(local_pp):
                            s["photo_path"] = local_pp
                    new_ppaths = []
                    for pp in s.get("photo_paths", []):
                        if pp:
                            local_pp = os.path.join(SCENE_PHOTOS_DIR, os.path.basename(pp))
                            if os.path.isfile(local_pp):
                                new_ppaths.append(local_pp)
                    s["photo_paths"] = new_ppaths
                    voc = s.get("vocal_path", "")
                    if voc:
                        local_voc = os.path.join(SCENE_VOCALS_DIR, os.path.basename(voc))
                        if os.path.isfile(local_voc):
                            s["vocal_path"] = local_voc

                song = plan.get("song_path", "")
                if song:
                    local_song = os.path.join(UPLOADS_DIR, os.path.basename(song))
                    if os.path.isfile(local_song):
                        plan["song_path"] = local_song

                _save_manual_plan(plan)

                if project.get("auto_plan"):
                    with _plan_file_lock:
                        with open(SCENE_PLAN_PATH, "w") as f:
                            json.dump(project["auto_plan"], f, indent=2)

                if project.get("cost_tracker"):
                    _save_cost_tracker(project["cost_tracker"])

            self._send_json({"ok": True, "message": "Full project loaded with clips"})
        except Exception as e:
            self._send_json({"error": f"Failed to load project zip: {e}"}, 500)

    # ---- Prompt History & Favorites (Item 10) ----

    def _handle_get_prompt_history(self):
        """GET /api/prompt-history - return prompt history with favorites."""
        if os.path.isfile(PROMPT_HISTORY_PATH):
            try:
                with open(PROMPT_HISTORY_PATH, "r") as f:
                    history = json.load(f)
            except (json.JSONDecodeError, IOError):
                history = {"prompts": []}
        else:
            history = {"prompts": []}
        self._send_json(history)

    def _handle_star_prompt(self):
        """POST /api/prompt-history/star - star/unstar a prompt."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        prompt_text = params.get("prompt", "")
        starred = params.get("starred", True)

        if os.path.isfile(PROMPT_HISTORY_PATH):
            try:
                with open(PROMPT_HISTORY_PATH, "r") as f:
                    history = json.load(f)
            except (json.JSONDecodeError, IOError):
                history = {"prompts": []}
        else:
            history = {"prompts": []}

        found = False
        for entry in history["prompts"]:
            if entry.get("prompt") == prompt_text:
                entry["starred"] = starred
                found = True
                break

        if not found and prompt_text:
            history["prompts"].insert(0, {
                "prompt": prompt_text,
                "starred": starred,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })

        with open(PROMPT_HISTORY_PATH, "w") as f:
            json.dump(history, f, indent=2)

        self._send_json({"ok": True})

    # ---- Render Time Estimation (Item 37) ----

    def _handle_render_estimate(self, parsed):
        """GET /api/render-estimate?scenes=N&engine=grok - estimate render time."""
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            num_scenes = int(qs.get("scenes", [0])[0])
        except (ValueError, IndexError):
            num_scenes = 0
        engine = qs.get("engine", ["grok"])[0]

        gen_time_per_scene = RENDER_TIME_ESTIMATES.get(engine, 45)
        total_gen = num_scenes * gen_time_per_scene
        total_stitch = num_scenes * STITCH_TIME_PER_CLIP
        total_seconds = total_gen + total_stitch
        total_minutes = round(total_seconds / 60, 1)

        self._send_json({
            "scenes": num_scenes,
            "engine": engine,
            "gen_time_per_scene": gen_time_per_scene,
            "stitch_time_per_clip": STITCH_TIME_PER_CLIP,
            "total_seconds": total_seconds,
            "total_minutes": total_minutes,
            "estimate_label": f"~{total_minutes} min" if total_minutes >= 1 else f"~{total_seconds}s",
        })

    # ---- Autosave (Item 47) ----

    def _handle_autosave(self):
        """POST /api/project/autosave - save current state."""
        body = self._read_body()
        try:
            state = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        state["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(AUTOSAVE_PATH, "w") as f:
            json.dump(state, f, indent=2)

        self._send_json({"ok": True, "saved_at": state["saved_at"]})

    def _handle_get_autosave(self):
        """GET /api/project/autosave - check for autosave."""
        if os.path.isfile(AUTOSAVE_PATH):
            try:
                with open(AUTOSAVE_PATH, "r") as f:
                    data = json.load(f)
                self._send_json({"exists": True, "data": data})
            except (json.JSONDecodeError, IOError):
                self._send_json({"exists": False})
        else:
            self._send_json({"exists": False})

    # ---- Feature 10: Social platform export ----

    def _handle_social_export(self):
        """Export video for a specific social platform."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        platform = params.get("platform", "youtube")
        target = params.get("target", "manual")

        if target == "manual":
            video_path = os.path.join(OUTPUT_DIR, "manual_final_video.mp4")
        else:
            video_path = os.path.join(OUTPUT_DIR, "final_video.mp4")

        if not os.path.isfile(video_path):
            self._send_json({"error": "Final video not found."}, 400)
            return

        out_path = os.path.join(SOCIAL_EXPORTS_DIR, f"{platform}_export.mp4")

        try:
            export_for_platform(video_path, out_path, platform)
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            self._send_json({
                "ok": True,
                "platform": platform,
                "url": f"/api/social-exports/{platform}_export.mp4",
                "size_mb": round(size_mb, 1),
            })
        except Exception as e:
            self._send_json({"error": f"Export failed: {e}"}, 500)

    # ---- Item 9: Color palette extraction ----

    def _handle_get_palette(self, scene_id: str):
        """Extract dominant color palette from a scene's photo."""
        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                photo_path = s.get("photo_path", "")
                if not photo_path or not os.path.isfile(photo_path):
                    self._send_json({"error": "Scene has no photo uploaded"}, 400)
                    return
                try:
                    result = extract_palette(photo_path)
                    self._send_json({"ok": True, **result})
                except Exception as e:
                    self._send_json({"error": f"Palette extraction failed: {e}"}, 500)
                return
        self._send_json({"error": "Scene not found"}, 404)

    # ---- Item 18: Loop / boomerang effect ----

    def _handle_boomerang(self, scene_id: str):
        """Toggle loop/boomerang on a scene's clip."""
        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                clip_path = s.get("clip_path", "")
                if not clip_path or not os.path.isfile(clip_path):
                    self._send_json({"error": "No clip to apply boomerang to"}, 400)
                    return
                is_looped = s.get("loop", False)
                if is_looped:
                    self._send_json({"error": "Clip already has boomerang applied"}, 400)
                    return
                try:
                    boom_path = os.path.join(MANUAL_CLIPS_DIR, f"boomerang_{scene_id}.mp4")
                    apply_loop_boomerang(clip_path, boom_path)
                    s["clip_path"] = boom_path
                    s["loop"] = True
                    s["has_clip"] = True
                    _save_manual_plan(plan)
                    self._send_json({"ok": True, "message": "Boomerang effect applied"})
                except Exception as e:
                    self._send_json({"error": f"Boomerang failed: {e}"}, 500)
                return
        self._send_json({"error": "Scene not found"}, 404)

    # ---- Item 20: Audio ducking ----

    def _handle_audio_ducking(self):
        """Apply audio ducking to the final video where voiceovers exist."""
        body = self._read_body()
        try:
            params = json.loads(body) if body else {}
        except json.JSONDecodeError:
            params = {}

        duck_level = params.get("duck_level", 0.3)
        target = params.get("target", "manual")

        if target == "manual":
            plan = _load_manual_plan()
            video_path = plan.get("output_path", os.path.join(OUTPUT_DIR, "manual_final_video.mp4"))
        else:
            plan = _load_scene_plan()
            if not plan:
                self._send_json({"error": "No scene plan found"}, 404)
                return
            video_path = plan.get("output_path", os.path.join(OUTPUT_DIR, "final_video.mp4"))

        if not os.path.isfile(video_path):
            self._send_json({"error": "Final video not found. Stitch first."}, 400)
            return

        # Build duck segments from scenes that have vocals
        duck_segments = []
        running_time = 0.0
        for s in plan.get("scenes", []):
            dur = s.get("duration", 5)
            vocal_path = s.get("vocal_path", "")
            if vocal_path and os.path.isfile(vocal_path):
                duck_segments.append({
                    "start_sec": running_time,
                    "end_sec": running_time + dur,
                })
            running_time += dur

        if not duck_segments:
            self._send_json({"error": "No scenes with voiceovers found. Nothing to duck."}, 400)
            return

        try:
            temp_out = video_path + ".duck_tmp.mp4"
            apply_audio_ducking(video_path, temp_out, duck_segments, duck_level)
            os.replace(temp_out, video_path)
            self._send_json({
                "ok": True,
                "message": f"Audio ducking applied ({len(duck_segments)} segments at {int(duck_level*100)}% volume)",
                "segments_ducked": len(duck_segments),
            })
        except Exception as e:
            self._send_json({"error": f"Audio ducking failed: {e}"}, 500)

    # ---- Item 34: GIF export ----

    def _handle_export_gif(self, scene_id: str):
        """Export a single scene's clip as an animated GIF."""
        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                clip_path = s.get("clip_path", "")
                if not clip_path or not os.path.isfile(clip_path):
                    self._send_json({"error": "No clip to export"}, 400)
                    return
                try:
                    gif_name = f"scene_{scene_id}.gif"
                    gif_path = os.path.join(GIFS_DIR, gif_name)
                    export_gif(clip_path, gif_path)
                    size_kb = os.path.getsize(gif_path) / 1024
                    self._send_json({
                        "ok": True,
                        "url": f"/api/gifs/{gif_name}",
                        "filename": gif_name,
                        "size_kb": round(size_kb, 1),
                    })
                except Exception as e:
                    self._send_json({"error": f"GIF export failed: {e}"}, 500)
                return
        self._send_json({"error": "Scene not found"}, 404)

    def _handle_export_best_gifs(self):
        """Auto-pick the 3 highest-energy scenes and export them as GIFs."""
        plan = _load_manual_plan()
        scenes = plan.get("scenes", [])
        if not scenes:
            self._send_json({"error": "No scenes available"}, 400)
            return

        # Identify scenes with clips, sorted by energy heuristic
        # Energy heuristic: shorter duration + higher speed = higher energy
        candidates = []
        for s in scenes:
            cp = s.get("clip_path", "")
            if cp and os.path.isfile(cp):
                speed = s.get("speed", 1.0)
                dur = s.get("duration", 5)
                # Higher speed and shorter duration = higher energy
                energy_score = speed / max(dur, 1)
                candidates.append((energy_score, s))

        if not candidates:
            self._send_json({"error": "No clips available for GIF export"}, 400)
            return

        # Sort by energy (highest first), take top 3
        candidates.sort(key=lambda x: x[0], reverse=True)
        top3 = candidates[:3]

        results = []
        for _, s in top3:
            try:
                gif_name = f"best_{s['id']}.gif"
                gif_path = os.path.join(GIFS_DIR, gif_name)
                export_gif(s["clip_path"], gif_path)
                size_kb = os.path.getsize(gif_path) / 1024
                results.append({
                    "scene_id": s["id"],
                    "url": f"/api/gifs/{gif_name}",
                    "filename": gif_name,
                    "size_kb": round(size_kb, 1),
                })
            except Exception:
                pass

        self._send_json({"ok": True, "gifs": results, "count": len(results)})

    # ---- Item 42: Template library ----

    def _handle_list_templates(self):
        """List all saved templates."""
        templates = []
        if os.path.isdir(TEMPLATES_DIR):
            for fname in sorted(os.listdir(TEMPLATES_DIR)):
                if fname.endswith(".json"):
                    fpath = os.path.join(TEMPLATES_DIR, fname)
                    try:
                        with open(fpath, "r") as f:
                            tpl = json.load(f)
                        templates.append({
                            "filename": fname,
                            "name": tpl.get("name", fname),
                            "scene_count": len(tpl.get("scenes", [])),
                            "saved_at": tpl.get("saved_at", ""),
                        })
                    except (json.JSONDecodeError, IOError):
                        pass
        self._send_json({"templates": templates})

    def _handle_save_template(self):
        """Save current scene configuration as a named template."""
        body = self._read_body()
        try:
            params = json.loads(body) if body else {}
        except json.JSONDecodeError:
            params = {}

        template_name = params.get("name", "").strip()
        if not template_name:
            template_name = f"template_{time.strftime('%Y%m%d_%H%M%S')}"

        plan = _load_manual_plan()
        scenes = plan.get("scenes", [])
        if not scenes:
            self._send_json({"error": "No scenes to save as template"}, 400)
            return

        # Strip file paths (only save configuration, not files)
        template_scenes = []
        for s in scenes:
            template_scenes.append({
                "prompt": s.get("prompt", ""),
                "duration": s.get("duration", 5),
                "transition": s.get("transition", "crossfade"),
                "speed": s.get("speed", 1.0),
                "camera_movement": s.get("camera_movement", "zoom_in"),
                "engine": s.get("engine", ""),
                "color_grade": s.get("color_grade"),
                "overlay": s.get("overlay"),
                "loop": s.get("loop", False),
            })

        template = {
            "name": template_name,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scenes": template_scenes,
            "color_grade": plan.get("color_grade", "none"),
            "audio_viz": plan.get("audio_viz"),
            "style_lock": plan.get("style_lock", ""),
        }

        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', template_name)
        filepath = os.path.join(TEMPLATES_DIR, f"{safe_name}.json")
        with open(filepath, "w") as f:
            json.dump(template, f, indent=2)

        self._send_json({
            "ok": True,
            "filename": f"{safe_name}.json",
            "name": template_name,
            "scene_count": len(template_scenes),
        })

    def _handle_load_template(self):
        """Load a template to pre-fill scenes."""
        body = self._read_body()
        try:
            params = json.loads(body) if body else {}
        except json.JSONDecodeError:
            params = {}

        filename = params.get("filename", "")
        if not filename:
            self._send_json({"error": "No template filename specified"}, 400)
            return

        filepath = os.path.join(TEMPLATES_DIR, os.path.basename(filename))
        if not os.path.isfile(filepath):
            self._send_json({"error": "Template not found"}, 404)
            return

        try:
            with open(filepath, "r") as f:
                template = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self._send_json({"error": f"Failed to read template: {e}"}, 500)
            return

        # Create new scenes from template
        plan = _load_manual_plan()
        for tpl_scene in template.get("scenes", []):
            scene_id = str(_uuid.uuid4())[:8]
            scene = {
                "id": scene_id,
                "prompt": tpl_scene.get("prompt", ""),
                "duration": tpl_scene.get("duration", 5),
                "transition": tpl_scene.get("transition", "crossfade"),
                "speed": tpl_scene.get("speed", 1.0),
                "camera_movement": tpl_scene.get("camera_movement", "zoom_in"),
                "engine": tpl_scene.get("engine", ""),
                "color_grade": tpl_scene.get("color_grade"),
                "overlay": tpl_scene.get("overlay"),
                "loop": tpl_scene.get("loop", False),
                "photo_path": None,
                "photo_paths": [],
                "clip_path": None,
                "has_clip": False,
                "video_path": None,
                "vocal_path": None,
                "vocal_volume": 80,
                "previous_clip_path": None,
            }
            plan["scenes"].append(scene)

        # Apply template-level settings
        if template.get("color_grade"):
            plan["color_grade"] = template["color_grade"]
        if template.get("style_lock"):
            plan["style_lock"] = template["style_lock"]
        if template.get("audio_viz"):
            plan["audio_viz"] = template["audio_viz"]

        _save_manual_plan(plan)
        self._send_json({
            "ok": True,
            "message": f"Loaded template '{template.get('name', '')}' with {len(template.get('scenes', []))} scenes",
            "scenes_added": len(template.get("scenes", [])),
        })

    # ---- Item 44: AI assistant suggestions ----

    def _handle_suggest_prompt(self):
        """Use Grok text API to suggest a better prompt for a scene."""
        body = self._read_body()
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        current_prompt = params.get("current_prompt", "")
        context = params.get("context", {})
        scene_id = params.get("scene_id", "")

        # Build context string
        section_type = context.get("section_type", "")
        energy = context.get("energy", "")
        adjacent_prompts = context.get("adjacent_prompts", [])
        style_lock = context.get("style_lock", "")

        context_parts = []
        if section_type:
            context_parts.append(f"This is a {section_type} section.")
        if energy:
            context_parts.append(f"Energy level: {energy}.")
        if style_lock:
            context_parts.append(f"Visual style: {style_lock}.")
        if adjacent_prompts:
            context_parts.append(f"Adjacent scenes: {'; '.join(adjacent_prompts[:2])}.")

        context_str = " ".join(context_parts)

        # Call Grok text API for suggestion
        try:
            api_key = os.environ.get("XAI_API_KEY", "")
            if not api_key:
                self._send_json({"error": "XAI_API_KEY not set"}, 500)
                return

            import requests as req
            system_msg = (
                "You are a creative AI video prompt engineer. "
                "Given a current video scene prompt and its context, "
                "suggest an enhanced, more detailed and cinematic prompt. "
                "Keep it concise (under 100 words). "
                "Focus on visual details, camera movements, lighting, mood, and atmosphere. "
                "Return ONLY the improved prompt text, nothing else."
            )

            user_msg = f"Current prompt: \"{current_prompt}\"\n"
            if context_str:
                user_msg += f"Context: {context_str}\n"
            user_msg += "Suggest a better, more detailed prompt:"

            resp = req.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "grok-3-mini",
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 200,
                    "temperature": 0.8,
                },
                timeout=30,
            )

            if resp.status_code != 200:
                self._send_json({"error": f"Grok API error: {resp.status_code}"}, 500)
                return

            data = resp.json()
            suggestion = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            suggestion = suggestion.strip().strip('"')

            if not suggestion:
                self._send_json({"error": "No suggestion received"}, 500)
                return

            self._send_json({
                "ok": True,
                "suggestion": suggestion,
                "original": current_prompt,
            })

        except Exception as e:
            self._send_json({"error": f"Suggestion failed: {e}"}, 500)

    # ---- Item 46: Project comparison ----

    def _handle_get_previous_clip(self, scene_id: str):
        """Serve a scene's previous clip for comparison."""
        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                prev_clip = s.get("previous_clip_path", "")
                if prev_clip and os.path.isfile(prev_clip):
                    self._send_file(prev_clip)
                    return
                self.send_error(404, "No previous clip available")
                return
        self.send_error(404, "Scene not found")




    # ---- Roadmap Feature Handlers ----

    def _handle_reverse_clip(self, scene_id):
        plan = _load_manual_plan()
        for s in plan.get("scenes", []):
            if s.get("id") == scene_id and s.get("clip_path") and os.path.isfile(s["clip_path"]):
                rev_path = s["clip_path"].replace(".mp4", "_rev.mp4")
                reverse_clip(s["clip_path"], rev_path)
                os.replace(rev_path, s["clip_path"])
                s["reversed"] = not s.get("reversed", False)
                _save_manual_plan(plan)
                self._send_json({"ok": True, "reversed": s["reversed"]})
                return
        self._send_json({"ok": False, "error": "Scene or clip not found"})

    def _handle_estimate_render_time(self):
        """Estimate total render time based on scenes and engines."""
        plan = _load_manual_plan()
        scenes = plan.get("scenes", [])
        if not scenes:
            self._send_json({"estimate_seconds": 0, "estimate_human": "0 seconds"})
            return
        
        STITCH_PER_CLIP = 5  # ~5 seconds per clip for stitching

        settings = _load_settings()
        default_engine = settings.get("default_engine", "gen4_5")

        total = 0
        for s in scenes:
            if s.get("has_clip") or s.get("clip_path"):
                continue  # already generated
            engine = s.get("engine") or default_engine
            total += RENDER_TIME_ESTIMATES.get(engine, 45)
        
        total += len(scenes) * STITCH_PER_CLIP  # stitching time
        
        if total < 60:
            human = f"{total} seconds"
        elif total < 3600:
            human = f"{total // 60} min {total % 60}s"
        else:
            human = f"{total // 3600}h {(total % 3600) // 60}m"
        
        self._send_json({
            "estimate_seconds": total,
            "estimate_human": human,
            "scenes_to_generate": sum(1 for s in scenes if not s.get("has_clip") and not s.get("clip_path")),
            "scenes_ready": sum(1 for s in scenes if s.get("has_clip") or s.get("clip_path")),
        })


    def _handle_mix_styles(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        from lib.prompt_assistant import mix_presets, mix_styles
        style_a = params.get("style_a", "")
        style_b = params.get("style_b", "")
        weight = float(params.get("weight", 0.5))
        # Check if they're preset names or raw styles
        from lib.prompt_assistant import STYLE_PRESETS
        if style_a in STYLE_PRESETS:
            style_a = STYLE_PRESETS[style_a]
        if style_b in STYLE_PRESETS:
            style_b = STYLE_PRESETS[style_b]
        mixed = mix_styles(style_a, style_b, weight)
        self._send_json({"ok": True, "mixed_style": mixed})

    def _handle_detect_emotion(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        lyrics = params.get("lyrics", "")
        from lib.prompt_assistant import detect_emotion, emotion_to_visual_prompt
        emotions = detect_emotion(lyrics)
        visual = emotion_to_visual_prompt(emotions)
        self._send_json({"ok": True, "emotions": emotions, "visual_prompt": visual})


    # ---- Remaining Roadmap Handlers ----

    def _handle_qr_code(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        url = params.get("url", "https://example.com")
        from lib.roadmap_features import generate_qr_code
        qr_path = os.path.join(OUTPUT_DIR, "qr_code.png")
        generate_qr_code(url, qr_path)
        self._send_json({"ok": True, "qr_url": "/output/qr_code.png"})

    def _handle_save_version(self):
        from lib.roadmap_features import save_version
        final = os.path.join(OUTPUT_DIR, "final_video.mp4")
        if os.path.isfile(final):
            path = save_version(OUTPUT_DIR, final)
            self._send_json({"ok": True, "version_path": path})
        else:
            self._send_json({"ok": False, "error": "No final video to version"})

    def _handle_enhance_context(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        from lib.roadmap_features import enhance_prompt_with_context
        result = enhance_prompt_with_context(
            params.get("prompt", ""),
            params.get("scene_index", 0),
            params.get("total_scenes", 1),
            params.get("prev_prompt", ""),
            params.get("energy", 0.5),
        )
        self._send_json({"ok": True, "enhanced": result})

    def _handle_auto_transitions_energy(self):
        from lib.roadmap_features import auto_transitions_from_energy
        plan = _load_manual_plan()
        scenes = auto_transitions_from_energy(plan.get("scenes", []))
        plan["scenes"] = scenes
        _save_manual_plan(plan)
        self._send_json({"ok": True, "scenes_updated": len(scenes)})

    def _handle_detect_key(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        audio = params.get("audio_path", "")
        if not audio:
            plan = _load_manual_plan()
            audio = plan.get("song_path", "")
        from lib.roadmap_features import detect_key
        key = detect_key(audio)
        self._send_json({"ok": True, "key": key})

    def _handle_auto_mix(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        input_path = params.get("input_path", "")
        if not input_path:
            self._send_json({"ok": False, "error": "No input_path"})
            return
        from lib.roadmap_features import auto_mix_master
        output = input_path.replace(".mp3", "_mastered.mp3").replace(".wav", "_mastered.wav")
        auto_mix_master(input_path, output)
        self._send_json({"ok": True, "output": output})

    def _handle_click_track(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        bpm = float(params.get("bpm", 120))
        duration = float(params.get("duration", 60))
        from lib.roadmap_features import generate_click_track
        output = os.path.join(OUTPUT_DIR, "click_track.aac")
        generate_click_track(bpm, duration, output)
        self._send_json({"ok": True, "click_track_url": "/output/click_track.aac"})

    def _handle_extract_frames(self, scene_id):
        plan = _load_manual_plan()
        for s in plan.get("scenes", []):
            if s.get("id") == scene_id and s.get("clip_path") and os.path.isfile(s["clip_path"]):
                from lib.roadmap_features import extract_frames
                frames_dir = os.path.join(OUTPUT_DIR, "frames", scene_id)
                frames = extract_frames(s["clip_path"], frames_dir, fps=2)
                urls = [f"/output/frames/{scene_id}/{os.path.basename(f)}" for f in frames]
                self._send_json({"ok": True, "frames": urls, "count": len(frames)})
                return
        self._send_json({"ok": False, "error": "Scene or clip not found"})

    def _handle_storyboard_pdf(self):
        from lib.roadmap_features import export_storyboard_pdf
        plan = _load_manual_plan()
        output = os.path.join(OUTPUT_DIR, "storyboard.html")
        export_storyboard_pdf(plan.get("scenes", []), output)
        self._send_json({"ok": True, "storyboard_url": "/output/storyboard.html"})

    def _handle_embed_code(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        url = params.get("video_url", "/output/final_video.mp4")
        from lib.roadmap_features import generate_embed_code
        code = generate_embed_code(url)
        self._send_json({"ok": True, "embed_code": code})


    def _handle_project_reset(self):
        """Reset everything — clear all scenes, clips, uploads, state."""
        import shutil
        cleared = []
        # Clear manual scene plan
        plan_path = os.path.join(OUTPUT_DIR, "manual_scene_plan.json")
        if os.path.isfile(plan_path):
            os.unlink(plan_path)
            cleared.append("manual_scene_plan")
        # Clear auto scene plan
        auto_plan = os.path.join(OUTPUT_DIR, "scene_plan.json")
        if os.path.isfile(auto_plan):
            os.unlink(auto_plan)
            cleared.append("scene_plan")
        # Clear auto director plan
        ad_plan = os.path.join(OUTPUT_DIR, "auto_director_plan.json")
        if os.path.isfile(ad_plan):
            os.unlink(ad_plan)
            cleared.append("auto_director_plan")
        # Clear scene thumbnails
        thumb_dir = os.path.join(OUTPUT_DIR, "scene_thumbnails")
        if os.path.isdir(thumb_dir):
            for f in os.listdir(thumb_dir):
                try: os.unlink(os.path.join(thumb_dir, f))
                except Exception: pass
            cleared.append("scene_thumbnails")
        # Clear keyframes
        kf_dir = os.path.join(OUTPUT_DIR, "keyframes")
        if os.path.isdir(kf_dir):
            for f in os.listdir(kf_dir):
                try: os.unlink(os.path.join(kf_dir, f))
                except Exception: pass
            cleared.append("keyframes")
        # Clear clips
        for clips_dir in [CLIPS_DIR, MANUAL_CLIPS_DIR]:
            if os.path.isdir(clips_dir):
                for f in os.listdir(clips_dir):
                    fp = os.path.join(clips_dir, f)
                    try: os.unlink(fp)
                    except Exception: pass
                cleared.append(os.path.basename(clips_dir))
        # Clear uploaded scene photos
        photos_dir = os.path.join(UPLOADS_DIR, "scene_photos")
        if os.path.isdir(photos_dir):
            for f in os.listdir(photos_dir):
                try: os.unlink(os.path.join(photos_dir, f))
                except Exception: pass
            cleared.append("scene_photos")
        # Clear uploaded scene videos
        videos_dir = os.path.join(UPLOADS_DIR, "scene_videos")
        if os.path.isdir(videos_dir):
            for f in os.listdir(videos_dir):
                try: os.unlink(os.path.join(videos_dir, f))
                except Exception: pass
            cleared.append("scene_videos")
        # Clear uploaded scene vocals
        vocals_dir = os.path.join(UPLOADS_DIR, "scene_vocals")
        if os.path.isdir(vocals_dir):
            for f in os.listdir(vocals_dir):
                try: os.unlink(os.path.join(vocals_dir, f))
                except Exception: pass
            cleared.append("scene_vocals")
        # Clear final video
        final = os.path.join(OUTPUT_DIR, "final_video.mp4")
        if os.path.isfile(final):
            os.unlink(final)
            cleared.append("final_video")
        # Clear autosave
        autosave = os.path.join(OUTPUT_DIR, "autosave.json")
        if os.path.isfile(autosave):
            os.unlink(autosave)
            cleared.append("autosave")
        # Clear previews
        previews_dir = os.path.join(OUTPUT_DIR, "previews")
        if os.path.isdir(previews_dir):
            for f in os.listdir(previews_dir):
                try: os.unlink(os.path.join(previews_dir, f))
                except Exception: pass
            cleared.append("previews")
        # Clear GIFs
        gifs_dir = os.path.join(OUTPUT_DIR, "gifs")
        if os.path.isdir(gifs_dir):
            for f in os.listdir(gifs_dir):
                try: os.unlink(os.path.join(gifs_dir, f))
                except Exception: pass
            cleared.append("gifs")
        # Clear uploaded songs
        songs_in_uploads = [f for f in os.listdir(UPLOADS_DIR)
                           if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac'))
                           and os.path.isfile(os.path.join(UPLOADS_DIR, f))]
        for f in songs_in_uploads:
            try: os.unlink(os.path.join(UPLOADS_DIR, f))
            except Exception: pass
        if songs_in_uploads:
            cleared.append(f"songs ({len(songs_in_uploads)})")

        # Clear cost tracker (reset to zero for new project)
        cost_path = os.path.join(OUTPUT_DIR, "cost_tracker.json")
        if os.path.isfile(cost_path):
            os.unlink(cost_path)
            cleared.append("cost_tracker")

        # Reset settings but KEEP the default engine preference
        settings_path = os.path.join(OUTPUT_DIR, "settings.json")
        if os.path.isfile(settings_path):
            try:
                old_settings = json.loads(open(settings_path, encoding="utf-8").read())
                kept_engine = old_settings.get("default_engine", "runway")
            except Exception:
                kept_engine = "runway"
            # Write fresh settings with just the engine preference
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"default_engine": kept_engine, "character_references": {}}, f, indent=2)
            cleared.append(f"settings (kept engine={kept_engine})")

        # Clear references (project-specific character photos)
        refs_dir = os.path.join(PROJECT_DIR, "references")
        if os.path.isdir(refs_dir):
            for f in os.listdir(refs_dir):
                try: os.unlink(os.path.join(refs_dir, f))
                except Exception: pass
            cleared.append("references")

        # Clear storyboards
        sb_dir = os.path.join(OUTPUT_DIR, "storyboards")
        if os.path.isdir(sb_dir):
            for f in os.listdir(sb_dir):
                try: os.unlink(os.path.join(sb_dir, f))
                except Exception: pass
            cleared.append("storyboards")

        # Clear exports
        exports_dir = os.path.join(OUTPUT_DIR, "exports")
        if os.path.isdir(exports_dir):
            for f in os.listdir(exports_dir):
                try: os.unlink(os.path.join(exports_dir, f))
                except Exception: pass
            cleared.append("exports")

        # Clear frames
        frames_dir = os.path.join(OUTPUT_DIR, "frames")
        if os.path.isdir(frames_dir):
            import shutil as sh2
            sh2.rmtree(frames_dir, ignore_errors=True)
            os.makedirs(frames_dir, exist_ok=True)
            cleared.append("frames")

        # Reset generation state
        with gen_lock:
            gen_state["running"] = False
            gen_state["phase"] = "idle"
            gen_state["progress"] = []
            gen_state["total_scenes"] = 0
            gen_state["error"] = None
            gen_state["output_file"] = None
            gen_state["analysis"] = None
            gen_state["scenes"] = None

        # Clear Prompt OS entities (characters, costumes, environments, scenes, shots)
        try:
            pos_dir = os.path.join(OUTPUT_DIR, "prompt_os")
            if os.path.isdir(pos_dir):
                for fname in os.listdir(pos_dir):
                    fpath = os.path.join(pos_dir, fname)
                    if fname.endswith(".json") and os.path.isfile(fpath):
                        with open(fpath, "w", encoding="utf-8") as f:
                            json.dump([], f)
                        cleared.append(f"prompt_os/{fname}")
                # Clear uploaded entity photos
                photos_dir = os.path.join(pos_dir, "photos")
                if os.path.isdir(photos_dir):
                    import shutil
                    shutil.rmtree(photos_dir, ignore_errors=True)
                    os.makedirs(photos_dir, exist_ok=True)
                    os.makedirs(os.path.join(photos_dir, "characters"), exist_ok=True)
                    os.makedirs(os.path.join(photos_dir, "costumes"), exist_ok=True)
                    os.makedirs(os.path.join(photos_dir, "environments"), exist_ok=True)
                    cleared.append("prompt_os/photos")
        except Exception as e:
            print(f"[RESET] Warning clearing POS data: {e}")

        print(f"[RESET] Full project reset. Cleared: {', '.join(cleared)}")
        self._send_json({"ok": True, "cleared": cleared})

    # ──── Auto Director Handlers ────

    def _handle_ai_director_full_plan(self):
        """AI Director: Generate complete video plan with scenes + shots from Vision inputs."""
        try:
            body = json.loads(self._read_body())
            style = body.get("style", "cinematic")
            lyrics = body.get("lyrics", "")
            storyline = body.get("storyline", "")
            world_setting = body.get("world_setting", "")
            engine = body.get("engine", "gen4_5")
            character_ids = body.get("character_ids", [])
            environment_ids = body.get("environment_ids", [])

            # Find song
            song_path = None
            plan = _load_manual_plan()
            song_path = plan.get("song_path")
            if not song_path or not os.path.isfile(song_path):
                audio_files = []
                for f in os.listdir(UPLOADS_DIR):
                    if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                        fp = os.path.join(UPLOADS_DIR, f)
                        audio_files.append((os.path.getmtime(fp), fp))
                if audio_files:
                    audio_files.sort(reverse=True)
                    song_path = audio_files[0][1]

            # Analyze audio for timing
            duration = 30.0
            sections = []
            if song_path and os.path.isfile(song_path):
                from lib.audio_analyzer import analyze
                analysis = analyze(song_path)
                duration = analysis.get("duration", 30)
                sections = analysis.get("sections", [])

            if not sections:
                # Default sections for the duration
                sec_dur = duration / 4
                sections = [
                    {"start": 0, "end": sec_dur, "type": "intro", "energy": 0.3},
                    {"start": sec_dur, "end": sec_dur*2, "type": "verse", "energy": 0.5},
                    {"start": sec_dur*2, "end": sec_dur*3, "type": "chorus", "energy": 0.8},
                    {"start": sec_dur*3, "end": duration, "type": "outro", "energy": 0.4},
                ]

            # Load characters and environments
            characters = []
            for cid in character_ids:
                c = _prompt_os.get_character(cid)
                if c:
                    characters.append(c)
            environments = []
            for eid in environment_ids:
                e = _prompt_os.get_environment(eid)
                if e:
                    environments.append(e)

            # Pacing rules: shots per section type
            SECTION_SHOT_COUNTS = {
                "intro": 2,    # slower, fewer shots
                "verse": 3,    # narrative, moderate
                "chorus": 5,   # fast cuts, more shots
                "bridge": 3,   # experimental
                "outro": 2,    # slow resolution
            }

            # Camera presets by section type
            from lib.cinematic_engine import CAMERA_PRESETS
            SECTION_CAMERAS = {
                "intro": ["tarkovsky_stillness", "drone_reveal", "kubrick_symmetry_static"],
                "verse": ["fincher_slow_creep", "spielberg_tracking", "handheld_documentary"],
                "chorus": ["music_video_fast_cut", "nolan_push_in", "handheld_documentary"],
                "bridge": ["kubrick_symmetry_static", "surveillance_static", "tarkovsky_stillness"],
                "outro": ["tarkovsky_stillness", "drone_reveal", "wes_anderson_centered"],
            }

            # Emotion mapping
            SECTION_EMOTIONS = {
                "intro": ["calm", "tense"],
                "verse": ["melancholy", "vulnerable", "confident"],
                "chorus": ["aggressive", "defiant", "confident"],
                "bridge": ["vulnerable", "calm", "melancholy"],
                "outro": ["calm", "melancholy"],
            }

            SECTION_ENERGY = {
                "intro": "low", "verse": "controlled",
                "chorus": "explosive", "bridge": "controlled", "outro": "low",
            }

            import random, uuid as _uuid

            created_scenes = []
            all_shots = {}
            total_shots = 0

            # Storyline beats distribution
            story_beats = []
            if storyline:
                story_beats = [s.strip() for s in storyline.replace(". ", ".\n").split("\n") if s.strip()]

            for i, section in enumerate(sections):
                stype = section.get("type", "verse")
                sec_start = section.get("start", 0)
                sec_end = section.get("end", 0)
                sec_dur = round(sec_end - sec_start, 1)
                energy = section.get("energy", 0.5)

                # Pick character (rotate)
                char = characters[i % len(characters)] if characters else None
                # Pick environment (rotate, avoid repeats)
                env = environments[i % len(environments)] if environments else None

                # Story beat
                beat = ""
                if story_beats:
                    ratio = len(story_beats) / len(sections)
                    beat = story_beats[min(int(i * ratio), len(story_beats) - 1)]

                # Build scene name
                mood_words = {"intro": "Opening", "verse": "Narrative", "chorus": "Peak",
                              "bridge": "Reflection", "outro": "Resolution"}
                scene_name = f"{mood_words.get(stype, 'Scene')} — {stype.title()}"
                if beat:
                    scene_name = beat[:40]

                # Create POS scene
                scene_data = {
                    "title": scene_name,
                    "name": scene_name,
                    "prompt": f"{style}. {beat}" if beat else style,
                    "characterId": char["id"] if char else None,
                    "environmentId": env["id"] if env else None,
                    "duration": min(sec_dur, 10),
                    "mood": stype,
                    "order": i,
                    "section_type": stype,
                    "energy": energy,
                    "story_beat": beat,
                }
                pos_scene = _prompt_os.create_scene(scene_data)
                created_scenes.append(pos_scene)

                # Generate shots for this scene
                n_shots = SECTION_SHOT_COUNTS.get(stype, 3)
                shot_dur = round(sec_dur / n_shots, 1)
                shot_dur = max(2, min(shot_dur, 8))  # clamp 2-8s

                scene_shots = []
                cam_pool = SECTION_CAMERAS.get(stype, ["handheld_documentary"])
                emo_pool = SECTION_EMOTIONS.get(stype, ["calm"])
                section_energy = SECTION_ENERGY.get(stype, "controlled")

                # Shot type progression within section
                SHOT_TYPES_BY_POSITION = {
                    0: "wide",      # establishing
                    1: "medium",    # narrative
                    2: "close",     # reaction/emotion
                    3: "medium",    # action
                    4: "close",     # intensity
                    5: "wide",      # release
                }

                for j in range(n_shots):
                    shot_type = SHOT_TYPES_BY_POSITION.get(j, "medium")
                    preset = cam_pool[j % len(cam_pool)]
                    cam_data = dict(CAMERA_PRESETS.get(preset, {}))
                    emotion = emo_pool[j % len(emo_pool)]

                    # Intensity ramps up mid-section, down at end
                    if n_shots <= 2:
                        intensity = 5
                    else:
                        progress = j / (n_shots - 1)
                        if progress < 0.5:
                            intensity = int(3 + progress * 10)
                        else:
                            intensity = int(8 - (progress - 0.5) * 6)

                    # Auto-generate shot title
                    move_word = (cam_data.get("movement", "static") or "static").split()[0].title()
                    title = f"{shot_type.title()} {move_word}"

                    shot = {
                        "id": f"shot_{_uuid.uuid4().hex[:8]}",
                        "scene_id": pos_scene["id"],
                        "shot_number": j + 1,
                        "title": title,
                        "duration": shot_dur,
                        "camera": {
                            "shot_type": shot_type,
                            "lens": cam_data.get("lens", "35mm"),
                            "height": cam_data.get("height", "eye"),
                            "angle": cam_data.get("angle", "straight"),
                            "movement": cam_data.get("movement", "static"),
                            "preset": preset,
                        },
                        "framing": {
                            "composition": cam_data.get("composition", "rule_of_thirds"),
                            "subject_position": "center",
                            "depth": "mid",
                        },
                        "action": {
                            "summary": beat if beat else f"{stype} moment — {emotion}",
                            "start_pose": "",
                            "end_pose": "",
                        },
                        "performance": {
                            "intensity": intensity,
                            "energy": section_energy,
                            "emotion": emotion,
                            "speed": "slow" if stype in ("intro", "outro", "bridge") else "normal" if stype == "verse" else "fast",
                        },
                        "layers": {
                            "surface": beat[:60] if beat else "",
                            "symbolic": "",
                            "hidden": "",
                            "emotional": emotion,
                        },
                        "locks": {
                            "character_lock": True, "environment_lock": True,
                            "tone_lock": True, "visual_lock": True,
                            "continuity_lock": True, "prop_lock": True,
                        },
                        "continuity": {
                            "lock_environment": True,
                            "lock_character_pose": False,
                            "lock_lighting": True,
                            "lock_props": True,
                        },
                        "status": "planned",
                    }
                    scene_shots.append(shot)
                    total_shots += 1

                all_shots[pos_scene["id"]] = scene_shots

            # Save shots to settings
            settings = _load_settings()
            settings["shots_data"] = all_shots
            _save_settings(settings)

            # Also run the normal auto-director plan for generation compatibility
            try:
                char_list = characters
                env_list = environments
                ad_plan = _auto_director.plan_full_video(
                    song_path=song_path or "",
                    style=style,
                    characters=char_list,
                    environments=env_list,
                    engine=engine,
                    natural_pacing=True,
                    storyline=storyline,
                )
                # Inject universal prompt + world setting
                if world_setting:
                    for s in ad_plan.get("scenes", []):
                        s["prompt"] = f"{world_setting}. {s['prompt']}"
                    ad_plan["world_setting"] = world_setting

                with open(AUTO_DIRECTOR_PLAN_PATH, "w") as f:
                    json.dump(ad_plan, f, indent=2)
                _sync_auto_plan_to_scene_plan(ad_plan)
            except Exception as pe:
                print(f"[AI DIRECTOR] Auto-director plan also created (error: {pe})")

            # Auto-populate style memory from Vision inputs
            try:
                from lib.cinematic_engine import StyleMemory
                sm = StyleMemory()
                sm.set_from_vision(
                    universal_prompt=style,
                    world_setting=body.get("world_setting", ""),
                    style=style,
                )
                print(f"[AI DIRECTOR] Style memory set: {sm.data.get('mood_profile')}, {sm.data.get('lighting_style')}")
            except Exception as sme:
                print(f"[AI DIRECTOR] Style memory error: {sme}")

            self._send_json({
                "ok": True,
                "scenes_created": len(created_scenes),
                "shots_created": total_shots,
                "scenes": [{"id": s["id"], "name": s.get("title", s.get("name", ""))} for s in created_scenes],
                "duration": round(duration, 1),
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _handle_compile_shot(self):
        """Compile cinematic prompt variants for a shot."""
        try:
            from lib.prompt_assembler import compile_shot_variants
            body = json.loads(self._read_body())

            shot = body.get("shot", {})
            scene_id = body.get("scene_id", shot.get("scene_id", ""))
            shot_idx = body.get("shot_index", -1)

            # Load entities from scene
            char = None
            costume = None
            env = None
            if scene_id:
                scene_data = _prompt_os.get_scene(scene_id)
                if scene_data:
                    cid = scene_data.get("characterId")
                    if cid:
                        char = _prompt_os.get_character(cid)
                    costid = scene_data.get("costumeId")
                    if costid:
                        costume = _prompt_os.get_costume(costid)
                    eid = scene_data.get("environmentId")
                    if eid:
                        env = _prompt_os.get_environment(eid)

            # Override with explicit IDs if provided
            if body.get("character_id"):
                char = _prompt_os.get_character(body["character_id"])
            if body.get("costume_id"):
                costume = _prompt_os.get_costume(body["costume_id"])
            if body.get("environment_id"):
                env = _prompt_os.get_environment(body["environment_id"])

            # Load global settings
            settings = _load_settings()
            ds = settings.get("director_state", {})

            # Get previous shot for continuity
            prev_shot = None
            if scene_id and shot_idx > 0:
                shots = settings.get("shots_data", {}).get(scene_id, [])
                if shot_idx < len(shots):
                    prev_shot = shots[shot_idx - 1]

            variants = compile_shot_variants(
                shot=shot,
                character=char,
                costume=costume,
                environment=env,
                global_style=ds.get("universalPrompt", ""),
                world_setting=ds.get("worldSetting", ""),
                prev_shot=prev_shot,
            )
            self._send_json({"ok": True, "variants": variants})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _handle_director_generate_plan(self):
        """Generate a full director plan from Vision inputs."""
        try:
            from lib.director_mode import generate_director_plan
            from lib.audio_analyzer import analyze
            body = json.loads(self._read_body())

            # Find audio
            song_path = _load_manual_plan().get("song_path")
            if not song_path:
                for f in sorted(os.listdir(UPLOADS_DIR), key=lambda x: os.path.getmtime(os.path.join(UPLOADS_DIR, x)), reverse=True):
                    if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                        song_path = os.path.join(UPLOADS_DIR, f)
                        break

            audio = {}
            if song_path and os.path.isfile(song_path):
                audio = analyze(song_path)

            # Load entities
            chars = [_prompt_os.get_character(cid) for cid in body.get("character_ids", []) if _prompt_os.get_character(cid)]
            envs = [_prompt_os.get_environment(eid) for eid in body.get("environment_ids", []) if _prompt_os.get_environment(eid)]
            costumes = [_prompt_os.get_costume(cid) for cid in body.get("costume_ids", []) if _prompt_os.get_costume(cid)]

            plan = generate_director_plan(
                audio_analysis=audio,
                lyrics=body.get("lyrics", ""),
                storyline=body.get("storyline", ""),
                style=body.get("style", ""),
                world_setting=body.get("world_setting", ""),
                arc_type=body.get("arc_type", "rise"),
                pacing_style=body.get("pacing_style", "balanced"),
                coverage_mode=body.get("coverage_mode", "standard"),
                emotional_intensity=body.get("emotional_intensity", 0.7),
                abstract_level=body.get("abstract_level", 0.3),
                characters=chars,
                environments=envs,
                costumes=costumes,
            )

            # Save plan
            plan_path = os.path.join(OUTPUT_DIR, "director_plan.json")
            with open(plan_path, "w") as f:
                json.dump(plan, f, indent=2)

            self._send_json({"ok": True, "plan": plan})
        except Exception as e:
            import traceback; traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _handle_director_apply_plan(self):
        """Apply a director plan — create POS scenes + shots from the plan."""
        try:
            plan_path = os.path.join(OUTPUT_DIR, "director_plan.json")
            if not os.path.isfile(plan_path):
                self._send_json({"error": "No director plan exists. Generate one first."}, 400)
                return
            try:
                with open(plan_path, "r") as f:
                    plan = json.load(f)
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Director plan file is corrupted"}, 500)
                return

            created_scenes = 0
            all_shots_data = {}

            for scene in plan.get("scenes", []):
                # Create POS scene
                scene_body = {
                    "name": scene.get("name", f"Scene {scene.get('scene_index', 0)+1}"),
                    "title": scene.get("name", ""),
                    "sceneType": scene.get("type", "verse"),
                    "emotion": scene.get("emotion", ""),
                    "energy": scene.get("energy", 5),
                    "narrativeIntent": scene.get("purpose", ""),
                    "shotDescription": scene.get("emotional_goal", ""),
                    "characterId": scene.get("character_id", ""),
                    "environmentId": scene.get("environment_id", ""),
                    "costumeId": scene.get("costume_id", ""),
                    "duration": scene.get("duration", 5),
                    "orderIndex": scene.get("scene_index", 0),
                }
                pos_scene = _prompt_os.create_scene(scene_body)
                created_scenes += 1

                # Store shots for this scene
                scene_idx = scene.get("scene_index", 0)
                shots = plan.get("shots", {}).get(str(scene_idx), plan.get("shots", {}).get(scene_idx, []))
                if shots:
                    for shot in shots:
                        shot["scene_id"] = pos_scene["id"]
                    all_shots_data[pos_scene["id"]] = shots

            # Save shots to settings
            settings = _load_settings()
            existing_shots = settings.get("shots_data", {})
            existing_shots.update(all_shots_data)
            settings["shots_data"] = existing_shots
            _save_settings(settings)

            total_shots = sum(len(v) for v in all_shots_data.values())
            self._send_json({
                "ok": True,
                "scenes_created": created_scenes,
                "shots_created": total_shots,
            })
        except Exception as e:
            import traceback; traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _handle_prompt_compile(self):
        """Compile a structured prompt from entity IDs for preview."""
        try:
            from lib.prompt_assembler import assemble_prompt
            body = json.loads(self._read_body())

            # Load entities
            char = None
            if body.get("character_id"):
                char = _prompt_os.get_character(body["character_id"])
            costume = None
            if body.get("costume_id"):
                costume = _prompt_os.get_costume(body["costume_id"])
            env = None
            if body.get("environment_id"):
                env = _prompt_os.get_environment(body["environment_id"])

            # Load global settings
            settings = _load_settings()
            ds = settings.get("director_state", {})

            result = assemble_prompt(
                global_style=body.get("global_style", ds.get("universalPrompt", "")),
                world_setting=body.get("world_setting", ds.get("worldSetting", "")),
                character=char,
                costume=costume,
                environment=env,
                scene=body.get("scene", {}),
                global_negative=body.get("negative_prompt", ""),
                universal_prompt=ds.get("universalPrompt", ""),
            )
            self._send_json({"ok": True, **result})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_auto_director_plan(self):
        """Plan a full video via Auto Director."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        style = body.get("style", "cinematic")
        engine = body.get("engine", "gen4_5")
        preset_id = body.get("preset_id")
        natural_pacing = body.get("natural_pacing", True)
        budget = body.get("budget")

        # Get song path from manual plan or uploaded file
        song_path = body.get("song_path")
        if not song_path:
            plan = _load_manual_plan()
            song_path = plan.get("song_path")
        if not song_path or not os.path.isfile(song_path):
            # Find the MOST RECENTLY uploaded audio file
            audio_files = []
            for f in os.listdir(UPLOADS_DIR):
                if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                    fp = os.path.join(UPLOADS_DIR, f)
                    audio_files.append((os.path.getmtime(fp), fp, f))
            if audio_files:
                audio_files.sort(reverse=True)  # newest first
                song_path = audio_files[0][1]
                print(f"[DIRECTOR] Using most recent song: {audio_files[0][2]}")

        if not song_path or not os.path.isfile(song_path):
            self._send_json({"error": "No song uploaded. Upload a song first."}, 400)
            return

        # Resolve characters and environments
        char_ids = body.get("character_ids", [])
        env_ids = body.get("environment_ids", [])

        characters = []
        for cid in char_ids:
            c = _prompt_os.get_character(cid)
            if c:
                characters.append(c)

        environments = []
        for eid in env_ids:
            e = _prompt_os.get_environment(eid)
            if e:
                environments.append(e)

        try:
            storyline = body.get("storyline", "")
            universal_prompt = body.get("universal_prompt", "")
            world_setting = body.get("world_setting", "")
            plan = _auto_director.plan_full_video(
                song_path=song_path,
                style=style,
                characters=characters,
                environments=environments,
                engine=engine,
                natural_pacing=natural_pacing,
                preset_id=preset_id,
                budget=budget,
                storyline=storyline,
            )

            # Inject universal prompt and world setting into every scene
            if universal_prompt or world_setting:
                prefix_parts = []
                if universal_prompt:
                    prefix_parts.append(universal_prompt)
                if world_setting:
                    prefix_parts.append(f"World: {world_setting}")
                prefix = ". ".join(prefix_parts)
                for scene in plan.get("scenes", []):
                    scene["prompt"] = f"{prefix}. {scene['prompt']}"
                plan["universal_prompt"] = universal_prompt
                plan["world_setting"] = world_setting

            # Save plan
            with open(AUTO_DIRECTOR_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
            _sync_auto_plan_to_scene_plan(plan)

            self._send_json({"ok": True, "plan": plan})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_auto_director_ai_plan(self):
        """Plan a full video via AI Story Planner (LLM-driven)."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        lyrics = body.get("lyrics", "")
        creative_direction = body.get("creative_direction", body.get("style", "cinematic"))
        engine = body.get("engine", "grok")
        preset_id = body.get("preset_id")
        natural_pacing = body.get("natural_pacing", True)
        budget = body.get("budget")

        # Get song path
        song_path = body.get("song_path")
        if not song_path:
            plan = _load_manual_plan()
            song_path = plan.get("song_path")
        if not song_path or not os.path.isfile(song_path):
            audio_files = []
            for f in os.listdir(UPLOADS_DIR):
                if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                    fp = os.path.join(UPLOADS_DIR, f)
                    audio_files.append((os.path.getmtime(fp), fp, f))
            if audio_files:
                audio_files.sort(reverse=True)
                song_path = audio_files[0][1]
                print(f"[AI PLANNER] Using most recent song: {audio_files[0][2]}")

        if not song_path or not os.path.isfile(song_path):
            self._send_json({"error": "No song uploaded. Upload a song first."}, 400)
            return

        # Resolve characters and environments
        char_ids = body.get("character_ids", [])
        env_ids = body.get("environment_ids", [])

        characters = []
        for cid in char_ids:
            c = _prompt_os.get_character(cid)
            if c:
                characters.append(c)

        environments = []
        for eid in env_ids:
            e = _prompt_os.get_environment(eid)
            if e:
                environments.append(e)

        try:
            universal_prompt = body.get("universal_prompt", "")
            world_setting = body.get("world_setting", "")
            story_model = body.get("story_model")  # e.g. "claude-sonnet-4-6", "grok-3", etc.
            plan = _auto_director.plan_with_ai(
                song_path=song_path,
                creative_direction=creative_direction,
                lyrics=lyrics,
                characters=characters,
                environments=environments,
                engine=engine,
                natural_pacing=natural_pacing,
                preset_id=preset_id,
                budget=budget,
                story_model=story_model,
            )

            # Inject universal prompt and world setting into every scene
            if universal_prompt or world_setting:
                prefix_parts = []
                if universal_prompt:
                    prefix_parts.append(universal_prompt)
                if world_setting:
                    prefix_parts.append(f"World: {world_setting}")
                prefix = ". ".join(prefix_parts)
                for scene in plan.get("scenes", []):
                    scene["prompt"] = f"{prefix}. {scene['prompt']}"
                plan["universal_prompt"] = universal_prompt
                plan["world_setting"] = world_setting

            # Save plan (same path so generate/to-manual work)
            with open(AUTO_DIRECTOR_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
            _sync_auto_plan_to_scene_plan(plan)

            self._send_json({"ok": True, "plan": plan})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _handle_auto_director_generate(self):
        """Start executing the Auto Director plan."""
        if not os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            self._send_json({"error": "No plan exists. Create a plan first."}, 400)
            return

        progress = _auto_director.progress
        if progress.get("phase") == "generating":
            self._send_json({"error": "Generation already in progress"}, 409)
            return

        try:
            with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Plan file is corrupted"}, 500)
            return

        # V4: check for blocking errors (character/asset mismatch)
        if plan.get("status") == "blocked" and plan.get("blocking_errors"):
            errors = plan["blocking_errors"]
            msg = "Cannot generate: " + "; ".join(e.get("message", str(e)) for e in errors[:3])
            self._send_json({"error": msg}, 400)
            return

        # SECURITY (H1): moderate every prompt in the plan before the worker
        # starts firing them at fal.ai. Fail-closed — block the entire plan
        # if any single prompt is disallowed.
        try:
            from lib.moderation import moderate_prompt_strict
            blocked = []
            for sc in plan.get("scenes", []):
                p = sc.get("prompt") or ""
                if not p:
                    continue
                _mod = moderate_prompt_strict(p, nsfw_allowed=False)
                if not _mod["allowed"]:
                    blocked.append({"scene": sc.get("id") or sc.get("index"),
                                    "severity": _mod["severity"],
                                    "reasons": _mod["reasons"]})
                elif _mod["severity"] == "warn":
                    sc["prompt"] = _mod["redacted_prompt"]
            if blocked:
                return self._send_json({
                    "error": "moderation_blocked",
                    "blocked": blocked,
                }, 451)
        except Exception as _e:
            return self._send_json({"error": "moderation unavailable"}, 500)

        def run_generation():
            try:
                _auto_director.generate_full_video(plan, cost_cb=_record_cost)
            except Exception as e:
                _auto_director._update_progress(phase="error", error=str(e))

        thread = threading.Thread(target=run_generation, daemon=True)
        thread.start()
        self._send_json({"ok": True, "message": "Auto Director generation started"})

    def _handle_auto_director_restitch(self):
        """Re-stitch existing clips without regenerating."""
        if not os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            self._send_json({"error": "No plan exists"}, 400)
            return

        try:
            with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Plan file is corrupted"}, 500)
            return

        # Read render payload from request body
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            body = {}

        scenes = plan.get("scenes", [])
        song_path = plan.get("song_path")

        # Filter to scenes with valid clips on disk
        valid_scenes = [s for s in scenes
                        if s.get("clip_path") and os.path.isfile(s.get("clip_path", ""))]
        if not valid_scenes:
            self._send_json({"error": "No clips found on disk to stitch"}, 400)
            return

        clip_paths = [s.get("clip_path") for s in valid_scenes]
        transitions = [s.get("transition", "crossfade") for s in valid_scenes]
        output_path = os.path.join(OUTPUT_DIR, "auto_director_final.mp4")
        audio = song_path if song_path and os.path.isfile(song_path) else None

        # Collect features from payload and scenes
        text_overlays = body.get("textOverlays", [])
        audio_tracks = body.get("audioTracks", [])
        output_resolution = body.get("outputResolution")
        output_format = body.get("format", "MP4")
        audio_crossfade = body.get("audioCrossfade", 0.0)

        # Collect per-scene data
        speeds = [s.get("speed", 1.0) for s in valid_scenes]
        color_grades = [s.get("color_grade", "") for s in valid_scenes]
        global_color_grade = body.get("colorGrade", "none")
        speed_ramps = [s.get("speed_ramp", "none") for s in valid_scenes]
        reversed_clips = [s.get("reversed", False) for s in valid_scenes]
        audio_viz = body.get("audioViz")

        def run_stitch():
            try:
                _auto_director._update_progress(phase="stitching")

                # Progress callback to track stitch steps
                def progress_cb(msg):
                    with gen_lock:
                        if "progress" not in gen_state:
                            gen_state["progress"] = []
                        gen_state["progress"].append(msg)

                # Apply per-scene effects before stitching
                processed_clip_paths = list(clip_paths)
                for idx, s in enumerate(valid_scenes):
                    effect_name = s.get("effect", "none")
                    if effect_name and effect_name != "none" and idx < len(processed_clip_paths):
                        cp = processed_clip_paths[idx]
                        if cp and os.path.isfile(cp):
                            intensity = s.get("effect_intensity", 0.5)
                            effect_out = os.path.join(AUTO_DIRECTOR_CLIPS_DIR, f"_fx_{s.get('id', idx)}_{effect_name}.mp4")
                            try:
                                apply_effect(cp, effect_out, effect_name, intensity=intensity)
                                processed_clip_paths[idx] = effect_out
                            except Exception as e:
                                print(f"[AUTO-RESTITCH] Effect {effect_name} failed for scene {idx}: {e}")

                stitch(processed_clip_paths, audio, output_path,
                       transitions=transitions,
                       speeds=speeds,
                       text_overlays=text_overlays,
                       color_grade=global_color_grade,
                       scene_color_grades=color_grades,
                       audio_viz=audio_viz,
                       speed_ramps=speed_ramps,
                       reversed_clips=reversed_clips,
                       audio_crossfade=audio_crossfade,
                       output_resolution=output_resolution,
                       progress_cb=progress_cb)

                # Apply per-scene vocal overlays if any exist
                vocal_entries = []
                running_time = 0.0
                for s in valid_scenes:
                    dur = s.get("duration", 5)
                    vp = s.get("vocal_path", "")
                    if vp and os.path.isfile(vp):
                        vocal_entries.append({
                            "vocal_path": vp,
                            "start_sec": running_time,
                            "end_sec": running_time + dur,
                            "volume": s.get("vocal_volume", 80),
                        })
                    running_time += dur

                if vocal_entries and os.path.isfile(output_path):
                    temp_vocal_out = output_path + ".vocal_tmp.mp4"
                    try:
                        overlay_scene_vocals(output_path, temp_vocal_out, vocal_entries)
                        os.replace(temp_vocal_out, output_path)
                    except Exception:
                        if os.path.isfile(temp_vocal_out):
                            os.remove(temp_vocal_out)

                # Auto-duck audio when vocals exist
                auto_duck = body.get("autoDuck", False)
                if auto_duck and vocal_entries and os.path.isfile(output_path):
                    duck_level = body.get("duckLevel", 0.3)
                    duck_segments = [{"start_sec": ve["start_sec"], "end_sec": ve["end_sec"]}
                                     for ve in vocal_entries]
                    if duck_segments:
                        temp_duck_out = output_path + ".duck_tmp.mp4"
                        try:
                            apply_audio_ducking(output_path, temp_duck_out, duck_segments, duck_level)
                            os.replace(temp_duck_out, output_path)
                        except Exception:
                            if os.path.isfile(temp_duck_out):
                                os.remove(temp_duck_out)

                _auto_director._update_progress(phase="done", output_file=output_path)
            except Exception as e:
                _auto_director._update_progress(phase="error", error=f"Stitch failed: {e}")

        thread = threading.Thread(target=run_stitch, daemon=True)
        thread.start()
        self._send_json({"ok": True, "message": f"Re-stitching {len(clip_paths)} clips..."})

    def _handle_auto_director_to_manual(self):
        """Convert Auto Director plan to manual scene plan for fine-tuning."""
        if not os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            self._send_json({"error": "No Auto Director plan exists"}, 400)
            return

        try:
            with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                ad_plan = json.load(f)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Plan file is corrupted"}, 500)
            return

        manual_plan = _load_manual_plan()
        manual_plan["song_path"] = ad_plan.get("song_path")

        for ad_scene in ad_plan.get("scenes", []):
            manual_scene = {
                "id": ad_scene.get("id", str(_uuid.uuid4())[:8]),
                "prompt": ad_scene.get("prompt", ""),
                "duration": ad_scene.get("duration", 5),
                "transition": ad_scene.get("transition", "crossfade"),
                "speed": 1.0,
                "overlay": None,
                "color_grade": None,
                "camera_movement": ad_scene.get("camera_movement", "zoom_in"),
                "engine": ad_scene.get("engine", ""),
                "photo_path": ad_scene.get("character_photo_path"),
                "photo_paths": [],
                "clip_path": ad_scene.get("clip_path"),
                "has_clip": bool(ad_scene.get("clip_path") and os.path.isfile(ad_scene.get("clip_path", ""))),
                "video_path": None,
                "vocal_path": None,
                "vocal_volume": 80,
                "loop": False,
                "previous_clip_path": None,
                "characterId": ad_scene.get("characterId"),
                "costumeId": ad_scene.get("costumeId"),
                "environmentId": ad_scene.get("environmentId"),
            }
            manual_plan["scenes"].append(manual_scene)

        _save_manual_plan(manual_plan)
        self._send_json({"ok": True, "message": f"Converted {len(ad_plan.get('scenes', []))} scenes to Manual Mode"})

    # ──────────────── Movie Planner Handlers ────────────────

    def _handle_movie_plan(self):
        """Create a full movie plan via the Movie Planner engine."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        style = body.get("style", "cinematic")
        lyrics = body.get("lyrics", "")
        storyline = body.get("storyline", "")
        world_setting = body.get("world_setting", "")
        universal_prompt = body.get("universal_prompt", "")
        engine = body.get("engine", "gen4_5")
        preset = body.get("preset", body.get("preset_id", ""))
        budget = body.get("budget")

        # Film mode params
        project_mode = body.get("project_mode", "music_video")
        film_runtime = body.get("film_runtime", 60)
        film_scene_count = body.get("film_scene_count", 5)
        film_pacing = body.get("film_pacing", "medium")
        film_climax_position = body.get("film_climax_position", "late")
        film_tension_curve = body.get("film_tension_curve", "exponential")
        film_ending_type = body.get("film_ending_type", "bittersweet")

        is_film = (project_mode != "music_video")

        # Resolve characters, costumes, environments from POS
        char_ids = body.get("character_ids", [])
        costume_ids = body.get("costume_ids", [])
        env_ids = body.get("environment_ids", [])

        characters = []
        for cid in char_ids:
            c = _prompt_os.get_character(cid)
            if c:
                characters.append(c)

        costumes = []
        for cid in costume_ids:
            c = _prompt_os.get_costume(cid)
            if c:
                costumes.append(c)

        environments = []
        for eid in env_ids:
            e = _prompt_os.get_environment(eid)
            if e:
                environments.append(e)

        # Get audio sections — only needed for music video mode
        audio_sections = body.get("audio_sections")
        num_scenes = body.get("num_scenes")
        song_path = None

        if not is_film:
            # Only look for song if music mode
            if not audio_sections:
                # Try to analyze the uploaded song for sections
                song_path = body.get("song_path")
                if not song_path:
                    plan = _load_manual_plan()
                    song_path = plan.get("song_path")
                if not song_path or not os.path.isfile(song_path):
                    audio_files = []
                    for f in os.listdir(UPLOADS_DIR):
                        if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                            fp = os.path.join(UPLOADS_DIR, f)
                            audio_files.append((os.path.getmtime(fp), fp))
                    if audio_files:
                        audio_files.sort(reverse=True)
                        song_path = audio_files[0][1]

                if song_path and os.path.isfile(song_path):
                    try:
                        from lib.audio_analyzer import analyze as _analyze_audio
                        analysis = _analyze_audio(song_path)
                        audio_sections = analysis.get("sections", [])
                    except Exception as ae:
                        print(f"[MOVIE PLANNER] Audio analysis failed: {ae}")

        try:
            result = create_movie_plan(
                style=style,
                lyrics=lyrics,
                storyline=storyline,
                world_setting=world_setting,
                universal_prompt=universal_prompt,
                characters=characters,
                costumes=costumes,
                environments=environments,
                engine=engine,
                preset=preset,
                audio_sections=audio_sections,
                num_scenes=num_scenes,
                output_dir=OUTPUT_DIR,
                project_mode=project_mode,
                film_runtime=film_runtime,
                film_scene_count=film_scene_count,
                film_pacing=film_pacing,
                film_climax_position=film_climax_position,
                film_tension_curve=film_tension_curve,
                film_ending_type=film_ending_type,
            )

            # Also sync to auto_director_plan.json and scene_plan.json for generation compatibility
            compat_plan = {
                "song_path": song_path if song_path and os.path.isfile(song_path) else "",
                "style": style,
                "engine": engine,
                "scenes": result.get("scenes", []),
                "universal_prompt": universal_prompt,
                "world_setting": world_setting,
                "project_mode": project_mode,
            }
            with open(AUTO_DIRECTOR_PLAN_PATH, "w") as f:
                json.dump(compat_plan, f, indent=2)
            _sync_auto_plan_to_scene_plan(compat_plan)

            self._send_json(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _handle_import_shots(self):
        """Import a pre-written shot sheet as a movie plan.

        Accepts: {scenes: [{title, shot_prompt, prompt, summary, duration, ...}]}
        Writes movie_plan.json + auto_director_plan.json + scene_plan.json
        so the downstream Auto Director workspace can render and generate.
        """
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        raw_scenes = body.get("scenes") or []
        if not isinstance(raw_scenes, list) or not raw_scenes:
            self._send_json({"error": "scenes array is required"}, 400)
            return

        normalized = []
        for idx, s in enumerate(raw_scenes):
            if not isinstance(s, dict):
                continue
            prompt = (s.get("shot_prompt") or s.get("prompt") or "").strip()
            title = (s.get("title") or f"Shot {idx+1}").strip()
            scene = {
                "id": s.get("id") or f"imported_{idx+1}",
                "order": idx,
                "title": title,
                "summary": s.get("summary") or title,
                "shot_prompt": prompt,
                "prompt": prompt,
                "duration": s.get("duration") or 5,
                "characters": s.get("characters") or [],
                "costumes": s.get("costumes") or [],
                "environments": s.get("environments") or [],
                "camera_framing": s.get("camera_framing") or "medium",
                "motion_direction": s.get("motion_direction") or "static",
                "lighting_direction": s.get("lighting_direction") or "",
                "atmosphere": s.get("atmosphere") or "",
                "locks": {},
            }
            normalized.append(scene)

        plan = {
            "scenes": normalized,
            "bible": {"concept": "Imported shot sheet", "theme": "", "story_arc": ""},
            "beats": [],
            "coverage": {},
            "validation": {"ok": True, "warnings": []},
            "created_at": time.time(),
            "version": 1,
            "source": "import_shots",
        }

        try:
            save_movie_plan(plan, OUTPUT_DIR)
        except Exception as e:
            self._send_json({"error": f"Failed to save movie plan: {e}"}, 500)
            return

        compat_plan = {
            "song_path": "",
            "style": "cinematic",
            "engine": "gen4_5",
            "scenes": normalized,
            "universal_prompt": "",
            "world_setting": "",
            "project_mode": "cinematic",
        }
        try:
            with open(AUTO_DIRECTOR_PLAN_PATH, "w", encoding="utf-8") as f:
                json.dump(compat_plan, f, indent=2)
            _sync_auto_plan_to_scene_plan(compat_plan)
        except Exception as _se:
            print(f"[IMPORT_SHOTS] compat sync warning: {_se}")

        self._send_json({"ok": True, "scenes": normalized, "count": len(normalized)})

    def _handle_movie_scene_edit(self, scene_index):
        """Edit specific fields of a scene in the movie plan."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No movie plan exists"}, 400)
            return

        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": f"Scene index {scene_index} out of range"}, 400)
            return

        scene = scenes[scene_index]
        locks = scene.get("locks", {})

        # Update provided fields (respecting locks)
        updated_fields = []
        for field, value in body.items():
            if field in ("locks", "id", "order"):
                continue  # Protected fields
            if locks.get(field):
                continue  # Locked
            scene[field] = value
            updated_fields.append(field)

        # If prompt-affecting fields changed, rebuild the shot prompt
        prompt_fields = {"camera_direction", "lighting_direction", "color_direction",
                         "motion_direction", "action", "characters", "costumes", "environments"}
        if prompt_fields & set(updated_fields) and not locks.get("shot_prompt"):
            bible = rebuild_bible_from_plan(plan)
            prev_scene = scenes[scene_index - 1] if scene_index > 0 else None
            scene["shot_prompt"] = PromptBuilder.build_shot_prompt(scene, bible, prev_scene)
            scene["prompt"] = scene["shot_prompt"]

        # Check if preview should be marked stale
        visual_fields_set = {"summary", "action", "shot_prompt", "prompt",
                             "camera_direction", "lighting_direction", "color_direction",
                             "motion_direction", "characters", "costumes", "environments",
                             "emotional_shift", "visual_shift", "duration",
                             "transition_in", "transition_out", "title", "purpose", "delta"}
        if visual_fields_set & set(updated_fields):
            new_hash = _compute_scene_fingerprint(scene)
            preview = scene.setdefault("preview", {"status": "none"})
            if preview.get("status") == "ready" and preview.get("prompt_hash") != new_hash:
                preview["status"] = "stale"

        scenes[scene_index] = scene
        plan["scenes"] = scenes
        save_movie_plan(plan, OUTPUT_DIR)

        # Sync to generation pipeline
        self._sync_movie_plan_to_pipeline(plan)

        self._send_json({"ok": True, "scene": scene, "updated_fields": updated_fields})

    def _handle_scene_assets(self, scene_index):
        """Update scene asset bindings and rebuild shot prompt."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No movie plan exists"}, 400)
            return

        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": f"Scene index {scene_index} out of range"}, 400)
            return

        scene = scenes[scene_index]

        # Update asset IDs and resolve full objects (library + draft)
        for asset_type in ["characters", "costumes", "environments"]:
            if asset_type in body:
                new_ids = body[asset_type]  # list of IDs
                resolved = []
                for aid in new_ids:
                    obj = None
                    state = "library"
                    # Try POS library first
                    if asset_type == "characters":
                        obj = _prompt_os.get_character(aid)
                    elif asset_type == "costumes":
                        obj = _prompt_os.get_costume(aid)
                    else:
                        obj = _prompt_os.get_environment(aid)
                    # Fall back to draft assets
                    if not obj and aid.startswith("draft_"):
                        draft = _get_draft(aid)
                        if draft:
                            obj = {"id": aid, "name": draft.get("label", draft.get("name", ""))}
                            state = "draft"
                    if obj:
                        entry = {
                            "id": aid,
                            "name": obj.get("name", ""),
                            "state": state,
                        }
                        if asset_type == "characters":
                            entry["role_in_scene"] = "featured"
                        elif asset_type == "costumes":
                            entry["when_worn"] = "throughout"
                        else:
                            entry["how_used"] = "main setting"
                        resolved.append(entry)
                scene[asset_type] = resolved

        # Rebuild shot prompt with new assets
        bible = rebuild_bible_from_plan(plan)
        prev_scene = scenes[scene_index - 1] if scene_index > 0 else None
        scene["shot_prompt"] = PromptBuilder.build_shot_prompt(scene, bible, prev_scene)
        scene["prompt"] = scene["shot_prompt"]

        # Run validation
        from lib.movie_planner import SceneBuilder
        scene["asset_validation"] = SceneBuilder.validate_scene_assets(scene, bible)

        # Mark preview stale (asset changes affect generation)
        new_hash = _compute_scene_fingerprint(scene)
        preview = scene.setdefault("preview", {"status": "none"})
        if preview.get("status") == "ready" and preview.get("prompt_hash") != new_hash:
            preview["status"] = "stale"

        # Save
        scenes[scene_index] = scene
        plan["scenes"] = scenes
        save_movie_plan(plan, OUTPUT_DIR)

        # Sync to generation pipeline
        self._sync_movie_plan_to_pipeline(plan)

        self._send_json({"ok": True, "scene": scene})

    def _handle_scene_validate_assets(self, scene_index):
        """Validate scene text against assigned assets."""
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No movie plan exists"}, 400)
            return

        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": f"Scene index {scene_index} out of range"}, 400)
            return

        scene = scenes[scene_index]
        bible = rebuild_bible_from_plan(plan)

        from lib.movie_planner import SceneBuilder
        validation = SceneBuilder.validate_scene_assets(scene, bible)

        # Also get suggestions for unresolved mentions
        all_assets = (
            [{"id": _safe_id(c), "name": _safe_name(c)} for c in bible.characters] +
            [{"id": _safe_id(c), "name": _safe_name(c)} for c in bible.costumes] +
            [{"id": _safe_id(e), "name": _safe_name(e)} for e in bible.environments]
        )
        text = ' '.join([
            scene.get('summary', ''),
            scene.get('action', ''),
            scene.get('shot_prompt', ''),
        ])
        suggestions = SceneBuilder.match_text_to_assets(text, all_assets)

        self._send_json({
            "warnings": validation.get("warnings", []),
            "unresolved": validation.get("unresolved", []),
            "suggestions": suggestions,
        })

    def _handle_draft_promote(self):
        """Promote a draft asset to a POS library entity."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        draft_id = body.get("draft_id", "")
        if not draft_id:
            self._send_json({"error": "draft_id required"}, 400)
            return

        entity, old_id = _promote_draft(draft_id, _prompt_os)
        if not entity:
            self._send_json({"error": f"Draft '{draft_id}' not found or promotion failed"}, 404)
            return

        # Update all scenes that reference this draft
        plan = load_movie_plan(OUTPUT_DIR)
        replaced = 0
        if plan and plan.get("scenes"):
            replaced = _replace_draft_in_scenes(plan["scenes"], old_id, entity["id"], entity.get("name"))
            if replaced > 0:
                save_movie_plan(plan, OUTPUT_DIR)
                self._sync_movie_plan_to_pipeline(plan)

        self._send_json({
            "ok": True,
            "entity": entity,
            "old_draft_id": old_id,
            "scenes_updated": replaced,
        })

    def _handle_draft_resolve(self):
        """Resolve a draft asset by mapping it to an existing library asset."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        draft_id = body.get("draft_id", "")
        library_id = body.get("library_id", "")
        asset_type = body.get("asset_type", "")
        if not draft_id or not library_id:
            self._send_json({"error": "draft_id and library_id required"}, 400)
            return

        # Verify library asset exists
        obj = None
        if asset_type in ("character", "characters"):
            obj = _prompt_os.get_character(library_id)
        elif asset_type in ("costume", "costumes"):
            obj = _prompt_os.get_costume(library_id)
        elif asset_type in ("environment", "environments"):
            obj = _prompt_os.get_environment(library_id)
        if not obj:
            self._send_json({"error": f"Library asset '{library_id}' not found"}, 404)
            return

        # Replace draft ID in all scenes
        plan = load_movie_plan(OUTPUT_DIR)
        replaced = 0
        if plan and plan.get("scenes"):
            replaced = _replace_draft_in_scenes(plan["scenes"], draft_id, library_id, obj.get("name"))
            if replaced > 0:
                save_movie_plan(plan, OUTPUT_DIR)
                self._sync_movie_plan_to_pipeline(plan)

        # Remove the draft
        _remove_draft(draft_id)

        self._send_json({
            "ok": True,
            "library_asset": {"id": obj["id"], "name": obj.get("name", "")},
            "old_draft_id": draft_id,
            "scenes_updated": replaced,
        })

    def _handle_draft_remove(self):
        """Remove a draft asset (keep as generic text)."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        draft_id = body.get("draft_id", "")
        if not draft_id:
            self._send_json({"error": "draft_id required"}, 400)
            return

        # Mark asset as generic in scenes (remove the ID but keep the name)
        plan = load_movie_plan(OUTPUT_DIR)
        if plan and plan.get("scenes"):
            for scene in plan["scenes"]:
                for asset_type in ("characters", "costumes", "environments"):
                    for asset in scene.get(asset_type, []):
                        if asset.get("id") == draft_id:
                            asset["state"] = "generic"
                            asset["id"] = ""
            save_movie_plan(plan, OUTPUT_DIR)

        _remove_draft(draft_id)
        self._send_json({"ok": True, "removed": draft_id})

    def _handle_movie_scene_regenerate(self, scene_index):
        """Regenerate one scene in the movie plan."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No movie plan exists"}, 400)
            return

        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": f"Scene index {scene_index} out of range"}, 400)
            return

        bible = rebuild_bible_from_plan(plan)
        respect_locks = body.get("respect_locks", True)
        locks = scenes[scene_index].get("locks", {}) if respect_locks else {}

        result = SceneRegenerator.regenerate_scene(scene_index, scenes, bible, locks)
        if result is None:
            self._send_json({"error": "Regeneration failed"}, 500)
            return

        plan["scenes"] = scenes
        save_movie_plan(plan, OUTPUT_DIR)
        self._sync_movie_plan_to_pipeline(plan)

        self._send_json({"ok": True, "scene": result})

    def _handle_movie_scene_regenerate_downstream(self, scene_index):
        """Regenerate a scene and all following scenes."""
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No movie plan exists"}, 400)
            return

        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": f"Scene index {scene_index} out of range"}, 400)
            return

        bible = rebuild_bible_from_plan(plan)
        updated = SceneRegenerator.regenerate_downstream(scene_index, scenes, bible)

        plan["scenes"] = scenes
        save_movie_plan(plan, OUTPUT_DIR)
        self._sync_movie_plan_to_pipeline(plan)

        self._send_json({"ok": True, "scenes": updated})

    # ──── V4 Per-Shot Handlers ────

    def _handle_v4_shot_regenerate(self, beat_idx, shot_idx):
        """Regenerate a single shot within a V4 beat."""
        if not os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            self._send_json({"error": "No plan found"}, 404)
            return
        try:
            with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except Exception:
            self._send_json({"error": "Could not read plan"}, 500)
            return

        beats = plan.get("beats", [])
        if beat_idx < 0 or beat_idx >= len(beats):
            self._send_json({"error": f"Beat {beat_idx} out of range"}, 400)
            return
        shots = beats[beat_idx].get("shots", [])
        if shot_idx < 0 or shot_idx >= len(shots):
            self._send_json({"error": f"Shot {shot_idx} out of range in beat {beat_idx}"}, 400)
            return

        shot = shots[shot_idx]
        # Reset shot for regeneration
        shot["status"] = "planned"
        shot["has_clip"] = False
        shot["clip_path"] = None
        shot["trimmed_clip_path"] = None
        shot["gen_hash"] = None
        shot["error"] = None

        # Update flat scenes array too
        flat_idx = shot.get("index")
        if flat_idx is not None and flat_idx < len(plan.get("scenes", [])):
            plan["scenes"][flat_idx] = shot

        with _plan_file_lock:
            with open(AUTO_DIRECTOR_PLAN_PATH, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)

        self._send_json({"ok": True, "shot": shot, "message": f"Shot {beat_idx}/{shot_idx} queued for regeneration"})

    def _handle_v4_shot_edit(self, beat_idx, shot_idx):
        """Edit properties of a single shot within a V4 beat."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if not os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            self._send_json({"error": "No plan found"}, 404)
            return
        try:
            with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except Exception:
            self._send_json({"error": "Could not read plan"}, 500)
            return

        beats = plan.get("beats", [])
        if beat_idx < 0 or beat_idx >= len(beats):
            self._send_json({"error": f"Beat {beat_idx} out of range"}, 400)
            return
        shots = beats[beat_idx].get("shots", [])
        if shot_idx < 0 or shot_idx >= len(shots):
            self._send_json({"error": f"Shot {shot_idx} out of range"}, 400)
            return

        shot = shots[shot_idx]
        # Editable fields
        for field in ("shot_size", "movement", "angle", "action", "emotion",
                      "screen_direction", "is_hero", "prompt", "prompt_short",
                      "target_duration", "character_lock_strength",
                      "environment_lock_strength", "style_lock_strength", "seed_lock"):
            if field in body:
                shot[field] = body[field]
        # Recalculate runway_duration if target changed
        if "target_duration" in body:
            import math
            shot["runway_duration"] = max(2, min(10, math.ceil(shot["target_duration"] + 0.5)))
            shot["duration"] = shot["runway_duration"]
            shot["trim_out"] = shot["target_duration"]

        # Invalidate clip if settings changed
        shot["gen_hash"] = None

        # Sync to flat scenes
        flat_idx = shot.get("index")
        if flat_idx is not None and flat_idx < len(plan.get("scenes", [])):
            plan["scenes"][flat_idx] = shot

        with _plan_file_lock:
            with open(AUTO_DIRECTOR_PLAN_PATH, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)

        self._send_json({"ok": True, "shot": shot})

    # ──── V5 Pipeline Handlers ────

    def _get_pipeline_state(self):
        from lib.pipeline_state import PipelineState
        return PipelineState(OUTPUT_DIR)

    def _handle_pipeline_get_state(self):
        pipeline = self._get_pipeline_state()
        self._send_json(pipeline.get_progress())

    def _handle_pipeline_get_anchors(self):
        pipeline = self._get_pipeline_state()
        self._send_json({"anchors": pipeline.anchors})

    def _handle_pipeline_start(self):
        """Ingest master prompt, extract assets, create packages, plan.
        Also accepts enriched Brief context (storyline, world, style, film params, etc.)
        which is folded into the master_prompt before extraction so the downstream
        Opus extract_production_data call has full creative context.
        """
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        master_prompt = (body.get("master_prompt") or "").strip()
        if not master_prompt:
            self._send_json({"error": "master_prompt is required"}, 400)
            return

        # Optional reset — if pipeline is mid-run and user wants to restart
        reset = bool(body.get("reset", False))
        if reset:
            try:
                from lib.pipeline_state import PipelineState
                ps = PipelineState(OUTPUT_DIR)
                ps.reset_to("IDLE")
            except Exception as _rerr:
                print(f"[PIPELINE] reset warning: {_rerr}")

        # Guard: if pipeline is already past IDLE/PROMPT_RECEIVED, refuse with 409
        try:
            from lib.pipeline_state import PipelineState
            _ps = PipelineState(OUTPUT_DIR)
            if _ps.state not in ("IDLE", "PROMPT_RECEIVED", "ERROR", "COMPLETE"):
                self._send_json({
                    "error": "pipeline_running",
                    "state": _ps.state,
                    "hint": "Pipeline already in progress — pass reset=true to restart",
                }, 409)
                return
        except Exception:
            pass

        # ── Brief context enrichment — fold all Brief fields into the master prompt ──
        style = (body.get("style") or "").strip()
        storyline = (body.get("storyline") or "").strip()
        world_setting = (body.get("world_setting") or "").strip()
        universal_prompt = (body.get("universal_prompt") or "").strip()
        lyrics = (body.get("lyrics") or "").strip()
        project_mode = (body.get("project_mode") or "music_video").strip()
        film_runtime = body.get("film_runtime")
        film_scene_count = body.get("film_scene_count")
        film_pacing = (body.get("film_pacing") or "").strip()
        film_climax_position = (body.get("film_climax_position") or "").strip()
        film_tension_curve = (body.get("film_tension_curve") or "").strip()
        film_ending_type = (body.get("film_ending_type") or "").strip()
        preset = (body.get("preset") or "").strip()

        parts = ["Concept: " + master_prompt]
        if storyline:
            parts.append("Storyline: " + storyline)
        if world_setting:
            parts.append("World setting: " + world_setting)
        if style:
            parts.append("Visual style: " + style)
        if universal_prompt:
            parts.append("Global constraints (apply to every shot): " + universal_prompt)
        if lyrics:
            parts.append("Lyrics:\n" + lyrics)
        if project_mode and project_mode != "music_video":
            fp_bits = ["Project type: " + project_mode]
            if film_runtime:
                fp_bits.append("runtime " + str(film_runtime) + "s")
            if film_scene_count:
                fp_bits.append(str(film_scene_count) + " scenes")
            if film_pacing:
                fp_bits.append(film_pacing + " pacing")
            if film_climax_position:
                fp_bits.append("climax " + film_climax_position)
            if film_tension_curve:
                fp_bits.append(film_tension_curve + " tension curve")
            if film_ending_type:
                fp_bits.append(film_ending_type + " ending")
            parts.append("Story structure: " + ", ".join(fp_bits))
        if preset:
            parts.append("Template preset: " + preset)

        enriched_prompt = "\n\n".join(parts) if len(parts) > 1 else master_prompt

        song_path = body.get("song_path")
        if not song_path:
            # Find most recent uploaded audio (music video mode only)
            if project_mode == "music_video":
                audio_files = []
                for f in os.listdir(UPLOADS_DIR):
                    if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                        fp = os.path.join(UPLOADS_DIR, f)
                        audio_files.append((os.path.getmtime(fp), fp))
                if audio_files:
                    audio_files.sort(reverse=True)
                    song_path = audio_files[0][1]

        engine = body.get("engine", "gen4_turbo")
        mode = body.get("mode", "fast")
        auto_advance = body.get("auto_advance", False)
        story_model = body.get("story_model")

        try:
            result = _auto_director.run_pipeline(
                master_prompt=enriched_prompt,
                song_path=song_path,
                engine=engine,
                mode=mode,
                auto_advance=auto_advance,
                story_model=story_model,
            )
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_pipeline_advance(self):
        """Advance pipeline to next state."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            body = {}
        target = body.get("target_state")
        pipeline = self._get_pipeline_state()
        try:
            new_state = pipeline.advance(target)
            self._send_json({"ok": True, "state": new_state, "progress": pipeline.get_progress()})
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_pipeline_sheets_generate(self):
        """Trigger canonical sheet generation for current pipeline packages (background)."""
        from lib.preproduction_assets import build_sheet_prompt, get_sheet_plan
        from lib.fal_client import gemini_generate_image

        pipeline = self._get_pipeline_state()
        store = self._get_preprod_store()
        current_ids = set(pipeline.packages or [])
        all_pkgs = store.get_all()
        if current_ids:
            packages = [p for p in all_pkgs if p.get("package_id") in current_ids]
        else:
            packages = all_pkgs

        try:
            pipeline.advance("SHEETS_GENERATING")
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return

        def _sheets_thread():
            try:
                for pkg in packages:
                    if pkg.get("status") in ("approved", "generating"):
                        continue
                    sheet_plan = get_sheet_plan(pkg)
                    pkg["status"] = "generating"
                    store.save_package(pkg)
                    for view_def in sheet_plan:
                        prompt = build_sheet_prompt(pkg, view_def)
                        try:
                            paths = gemini_generate_image(
                                prompt=prompt,
                                resolution="1K",
                                aspect_ratio="16:9",
                                num_images=1,
                            )
                            image_path = paths[0] if paths else ""
                            if image_path and os.path.isfile(image_path):
                                for si in pkg["sheet_images"]:
                                    if si["view"] == view_def["view"]:
                                        si["image_path"] = image_path
                                        si["status"] = "generated"
                                        si["prompt_used"] = prompt
                                        break
                            else:
                                for si in pkg["sheet_images"]:
                                    if si["view"] == view_def["view"]:
                                        si["status"] = "failed"
                                        si["error"] = "fal.ai/Gemini returned no images"
                                        si["prompt_used"] = prompt
                                        break
                        except Exception as e:
                            for si in pkg["sheet_images"]:
                                if si["view"] == view_def["view"]:
                                    si["status"] = "failed"
                                    si["error"] = str(e)
                                    break
                    any_ok = any(
                        si.get("status") == "generated" and si.get("image_path")
                        for si in pkg["sheet_images"]
                    )
                    pkg["status"] = "generated" if any_ok else "failed"
                    if any_ok and not pkg.get("hero_image_path"):
                        for si in pkg["sheet_images"]:
                            if si.get("image_path") and si["status"] == "generated":
                                pkg["hero_image_path"] = si["image_path"]
                                pkg["hero_view"] = si["view"]
                                break
                    store.save_package(pkg)
                try:
                    pipeline.advance("SHEETS_REVIEW")
                except ValueError:
                    pass
            except Exception as e:
                pipeline.set_error(f"Sheets generation failed: {e}")

        import threading
        t = threading.Thread(target=_sheets_thread, daemon=True)
        t.start()
        self._send_json({
            "ok": True,
            "message": "Sheet generation started",
            "pipeline": pipeline.get_progress(),
            "num_packages": len(packages),
        })

    def _handle_pipeline_sheets_approve_all(self):
        """Bulk approve all generated canonical sheets."""
        store = self._get_preprod_store()
        pipeline = self._get_pipeline_state()
        approved = 0
        for pkg in store.get_all():
            if pkg.get("status") == "generated":
                pkg["status"] = "approved"
                store.save_package(pkg)
                approved += 1
        if pipeline.state == "SHEETS_REVIEW" or pipeline.state == "SHEETS_GENERATING":
            pipeline.advance("SHEETS_REVIEW")
        self._send_json({"ok": True, "approved": approved, "pipeline": pipeline.get_progress()})

    def _handle_pipeline_anchors_generate(self):
        """Compose shot anchors from approved canonical sheets (background)."""
        pipeline = self._get_pipeline_state()
        if not pipeline.plan or not pipeline.plan.get("scenes"):
            self._send_json({"error": "No plan available — run pipeline first"}, 400)
            return
        store = self._get_preprod_store()
        approved = [p for p in store.get_all() if p.get("status") == "approved"]
        if not approved:
            self._send_json({"error": "No approved packages — approve canonical sheets first"}, 400)
            return

        def _anchors_thread():
            try:
                _auto_director.pipeline_generate_anchors()
            except Exception as e:
                pipeline.set_error(f"Anchor generation failed: {e}")

        import threading
        t = threading.Thread(target=_anchors_thread, daemon=True)
        t.start()
        self._send_json({
            "ok": True,
            "message": "Anchor generation started",
            "pipeline": pipeline.get_progress(),
            "num_shots": len(pipeline.plan.get("scenes", [])),
            "num_approved_packages": len(approved),
        })

    def _handle_pipeline_anchor_approve(self, shot_id):
        pipeline = self._get_pipeline_state()
        pipeline.approve_anchor(shot_id)
        self._send_json({"ok": True, "shot_id": shot_id, "status": "approved"})

    def _handle_pipeline_anchor_reject(self, shot_id):
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            body = {}
        reason = body.get("reason", "")
        pipeline = self._get_pipeline_state()
        pipeline.reject_anchor(shot_id, reason)
        self._send_json({"ok": True, "shot_id": shot_id, "status": "rejected"})

    def _handle_pipeline_anchor_regenerate(self, shot_id):
        """Regenerate an anchor from canonical sheets."""
        from lib.pipeline_state import PipelineState
        from lib.preproduction_assets import PreproductionStore
        from lib.scene_compositor import regenerate_anchor
        from lib.master_prompt import extraction_to_style_bible

        pipeline = self._get_pipeline_state()
        store = self._get_preprod_store()
        packages = [p for p in store.get_all() if p.get("status") == "approved"]

        # Find the shot in the plan
        shot = None
        for s in pipeline.plan.get("scenes", []):
            if s.get("shot_id") == shot_id or s.get("id") == shot_id:
                shot = s
                break
        if not shot:
            self._send_json({"error": f"Shot {shot_id} not found"}, 404)
            return

        style_bible = extraction_to_style_bible(pipeline.extraction) if pipeline.extraction else {
            "global_style": "cinematic", "negative": "no text, no watermark",
        }

        try:
            anchor = regenerate_anchor(shot, packages, style_bible, OUTPUT_DIR)
            pipeline.set_anchor(shot_id, anchor)
            self._send_json({"ok": True, "anchor": anchor})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_pipeline_generate(self):
        """Generate video clips per approved anchor via fal.ai Kling, then stitch.

        V6 path — bypasses the legacy auto_director Runway loop. For each scene
        in the plan with an approved anchor, calls kling_image_to_video with the
        anchor as start frame, stitches clips with the existing video_stitcher,
        overlays the song, and writes the final MP4 to pipeline.output_file.
        """
        pipeline = self._get_pipeline_state()
        plan_path = os.path.join(OUTPUT_DIR, "auto_director_plan.json")
        if not os.path.isfile(plan_path):
            self._send_json({"error": "No plan available"}, 400)
            return

        with open(plan_path) as f:
            plan = json.load(f)

        scenes = plan.get("scenes", [])
        missing_anchors = []
        for scene in scenes:
            sid = scene.get("shot_id", scene.get("id", ""))
            anchor = pipeline.get_anchor(sid)
            if anchor and anchor.get("image_path") and anchor.get("status") in ("approved", "generated"):
                scene["anchor_image_path"] = anchor["image_path"]
            else:
                missing_anchors.append(sid)

        if missing_anchors:
            self._send_json({
                "error": "Missing approved anchors",
                "missing": missing_anchors[:10],
            }, 400)
            return

        pipeline.advance("SHOTS_GENERATING")

        def _generate_thread():
            try:
                from lib.fal_client import kling_image_to_video
                from lib.video_stitcher import stitch

                clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
                os.makedirs(clips_dir, exist_ok=True)

                generated_clips = []
                transitions = []
                for idx, scene in enumerate(scenes):
                    sid = scene.get("shot_id", scene.get("id", f"shot_{idx}"))
                    anchor_path = scene.get("anchor_image_path", "")
                    if not anchor_path or not os.path.isfile(anchor_path):
                        print(f"[PIPELINE] shot {sid}: anchor missing on disk, skipping")
                        continue

                    raw_prompt = scene.get("prompt", "") or scene.get("shot_prompt", "")
                    duration = int(scene.get("duration", 5) or 5)
                    tier = scene.get("kling_tier", "v3_standard")
                    cfg_scale = float(scene.get("cfg_scale", 0.6))

                    clip_out = os.path.join(clips_dir, f"clip_{idx:03d}_{sid}.mp4")
                    print(f"[PIPELINE] shot {sid} → Kling {tier} ({duration}s)")

                    try:
                        clip_path = kling_image_to_video(
                            start_image_path=anchor_path,
                            prompt=raw_prompt,
                            duration=duration,
                            tier=tier,
                            cfg_scale=cfg_scale,
                        )
                        if clip_path and os.path.isfile(clip_path):
                            import shutil as _shutil
                            _shutil.copy2(clip_path, clip_out)
                            scene["clip_path"] = clip_out
                            scene["status"] = "done"
                            generated_clips.append(clip_out)
                            transitions.append(scene.get("transition", "crossfade"))
                            _record_cost(f"pipeline_clip_{sid}", "video")
                        else:
                            scene["status"] = "failed"
                            scene["error"] = "fal.ai Kling returned no file"
                    except Exception as e:
                        scene["status"] = "failed"
                        scene["error"] = str(e)
                        print(f"[PIPELINE] shot {sid} failed: {e}")

                plan["scenes"] = scenes
                with open(plan_path, "w", encoding="utf-8") as pf:
                    json.dump(plan, pf, indent=2, ensure_ascii=False)

                if not generated_clips:
                    pipeline.set_error("No clips generated successfully")
                    return

                pipeline.advance("CONFORM")

                song_path = plan.get("song_path") or pipeline.song_path
                audio = song_path if song_path and os.path.isfile(song_path) else None
                final_path = os.path.join(OUTPUT_DIR, "pipeline_final.mp4")
                stitch(generated_clips, audio, final_path, transitions=transitions)

                pipeline.output_file = final_path
                pipeline.advance("COMPLETE")
            except Exception as e:
                import traceback as _tb
                _tb.print_exc()
                pipeline.set_error(str(e))

        import threading
        t = threading.Thread(target=_generate_thread, daemon=True)
        t.start()
        self._send_json({
            "ok": True,
            "message": "Generation started (Kling)",
            "pipeline": pipeline.get_progress(),
            "num_scenes": len(scenes),
        })

    # ──── V6 Pipeline Handlers: Gemini + Kling via fal.ai ────

    def _active_project_shot_ids(self) -> set:
        """Set of scene IDs for the current project. Used to filter shared
        output/pipeline/ folders so legacy test shots and other projects'
        shots don't leak into the active-project UI.
        """
        try:
            from lib.active_project import get_project_root
            scenes_path = os.path.join(get_project_root(), "prompt_os", "scenes.json")
            if not os.path.isfile(scenes_path):
                return set()
            with open(scenes_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return {s.get("id") for s in data if s.get("id")}
        except Exception:
            pass
        return set()

    def _handle_v6_get_anchors(self):
        """Return V6 anchor data for the active project only, sorted by the
        scene's narrative order (orderIndex) so the UI shows 1a→9b, not a
        UUID alphabetical jumble.

        Per-user namespacing: anchor gens for authenticated users land in
        `anchors_v6/u_<uid>/<shot>/`. We union the flat + user-specific dirs
        so gens are always visible to the UI that triggered them.
        """
        anchor_base = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6")
        if not os.path.isdir(anchor_base):
            self._send_json({"anchors": []})
            return
        active_ids = self._active_project_shot_ids()
        _cu = self._current_user() or {}
        _uid = int(_cu.get("id", 0) or 0)
        user_base = os.path.join(anchor_base, f"u_{_uid}") if _uid > 0 else None

        # Load scene narrative metadata for sort + display.
        scene_meta: dict[str, dict] = {}
        try:
            from lib.active_project import get_project_root
            scenes_path = os.path.join(get_project_root(), "prompt_os", "scenes.json")
            if os.path.isfile(scenes_path):
                with open(scenes_path, "r", encoding="utf-8") as f:
                    _scenes = json.load(f)
                if isinstance(_scenes, list):
                    for _s in _scenes:
                        _sid = _s.get("id")
                        if not _sid:
                            continue
                        scene_meta[_sid] = {
                            "opus_shot_id": _s.get("opus_shot_id", ""),
                            "order_index": _s.get("orderIndex", 999),
                            "name": _s.get("name", ""),
                            "duration_s": _s.get("duration_s") or _s.get("duration"),
                            "duration_source": _s.get("duration_source", ""),
                            "duration_rationale": _s.get("duration_rationale", ""),
                        }
        except Exception:
            pass

        # Collect shot dirs: flat first, then user-specific (user shadows flat)
        shot_sources: dict[str, str] = {}
        for shot_dir in sorted(os.listdir(anchor_base)):
            shot_path = os.path.join(anchor_base, shot_dir)
            if not os.path.isdir(shot_path):
                continue
            if shot_dir.startswith("u_") or shot_dir.startswith("_"):
                continue
            if active_ids and shot_dir not in active_ids:
                continue
            shot_sources[shot_dir] = shot_path
        if user_base and os.path.isdir(user_base):
            for shot_dir in sorted(os.listdir(user_base)):
                shot_path = os.path.join(user_base, shot_dir)
                if not os.path.isdir(shot_path):
                    continue
                if active_ids and shot_dir not in active_ids:
                    continue
                shot_sources[shot_dir] = shot_path  # user shadows flat

        # Sort by narrative order_index; unknown scenes sort last by UUID.
        def _sort_key(sid: str):
            m = scene_meta.get(sid)
            if m:
                return (0, m["order_index"], m["opus_shot_id"])
            return (1, 999, sid)

        anchors = []
        for shot_dir in sorted(shot_sources.keys(), key=_sort_key):
            shot_path = shot_sources[shot_dir]
            is_user = user_base is not None and shot_path.startswith(user_base)
            url_prefix = f"u_{_uid}/{shot_dir}" if is_user else shot_dir
            meta = scene_meta.get(shot_dir, {})
            entry = {
                "shot_id": shot_dir,
                "opus_shot_id": meta.get("opus_shot_id", ""),
                "order_index": meta.get("order_index", 999),
                "scene_name": meta.get("name", ""),
                "duration_s": meta.get("duration_s"),
                "duration_source": meta.get("duration_source", ""),
                "duration_rationale": meta.get("duration_rationale", ""),
                "candidates": [],
                "selected": None,
                "end_image": None,
            }
            for f in sorted(os.listdir(shot_path)):
                rel = f"{url_prefix}/{f}"
                if f.startswith("candidate_") and f.endswith(".png"):
                    entry["candidates"].append(f"/api/v6/anchor-image/{rel}")
                elif f == "selected.png":
                    entry["selected"] = f"/api/v6/anchor-image/{rel}"
                elif f == "end_image.png":
                    entry["end_image"] = f"/api/v6/anchor-image/{rel}"
            anchors.append(entry)
        self._send_json({"anchors": anchors})

    def _handle_v6_get_clips(self):
        """Return V6 clip data for the active project only. Supports both
        the legacy flat `<shot_id>.mp4` layout and the new
        `<shot_id>/selected.mp4` subdir layout.
        """
        clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        if not os.path.isdir(clips_dir):
            self._send_json({"clips": []})
            return
        active_ids = self._active_project_shot_ids()
        clips = []
        for entry in sorted(os.listdir(clips_dir)):
            full = os.path.join(clips_dir, entry)
            if os.path.isfile(full) and entry.endswith(".mp4"):
                shot_id = entry[:-4]
                if active_ids and shot_id not in active_ids:
                    continue
                size_mb = os.path.getsize(full) / (1024 * 1024)
                clips.append({
                    "shot_id": shot_id,
                    "url": f"/api/v6/clip-video/{entry}",
                    "size_mb": round(size_mb, 1),
                })
            elif os.path.isdir(full):
                if active_ids and entry not in active_ids:
                    continue
                sel = os.path.join(full, "selected.mp4")
                if os.path.isfile(sel):
                    size_mb = os.path.getsize(sel) / (1024 * 1024)
                    clips.append({
                        "shot_id": entry,
                        "url": f"/api/v6/clip-video/{entry}/selected.mp4",
                        "size_mb": round(size_mb, 1),
                    })
        self._send_json({"clips": clips})

    def _handle_v6_serve_file(self, filepath):
        """Serve an anchor image or clip video file.

        SECURITY (C2): Realpath-confine to the pipeline output root. Without
        this check, `GET /api/v6/anchor-image/../../lumn.db` exfiltrates the
        DB (password hashes, sessions, ledger) and `..\\..\\.env` pulls keys.
        """
        pipeline_root = os.path.realpath(os.path.join(OUTPUT_DIR, "pipeline"))
        resolved = os.path.realpath(filepath)
        if not (resolved == pipeline_root or resolved.startswith(pipeline_root + os.sep)):
            self._send_json({"error": "Forbidden"}, 403)
            return
        if not os.path.isfile(resolved):
            self._send_json({"error": "File not found"}, 404)
            return
        # Also reject symlinks that pointed inside-then-outside
        try:
            st = os.lstat(resolved)
            import stat as _stat
            if _stat.S_ISLNK(st.st_mode):
                self._send_json({"error": "Forbidden"}, 403)
                return
        except OSError:
            self._send_json({"error": "Forbidden"}, 403)
            return
        # Whitelist extensions we expect to serve
        ext = os.path.splitext(resolved)[1].lower()
        content_types = {".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".mp4": "video/mp4",
                         ".webp": "image/webp"}
        if ext not in content_types:
            self._send_json({"error": "Unsupported file type"}, 403)
            return
        ct = content_types[ext]
        self.send_response(200)
        self.send_header("Content-Type", ct)
        size = os.path.getsize(resolved)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        with open(resolved, "rb") as f:
            self.wfile.write(f.read())

    def _handle_v6_identity_lock(self):
        """Manually lock a character's identity anchor (user override)."""
        from lib.identity_gate import lock_identity
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        name = (body.get("character_name") or "").strip()
        anchor_raw = body.get("anchor_path", "")
        anchor = _safe_user_path(anchor_raw)
        shot_id = body.get("shot_id", "manual")
        if not name or not anchor:
            self._send_json({"error": "character_name + valid anchor_path (must live under output/pipeline) required"}, 400)
            return
        entry = lock_identity(
            character_name=name,
            anchor_path=anchor,
            shot_id=shot_id,
            qa_overall=float(body.get("qa_overall", 0.85)),
            qa_identity=float(body.get("qa_identity", 0.85)),
            force=True,
        )
        self._send_json({"ok": True, "character": name, "entry": entry})

    def _handle_v6_identity_unlock(self):
        """Manually unlock a character's identity (user override)."""
        from lib.identity_gate import unlock_identity
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        name = (body.get("character_name") or "").strip()
        if not name:
            self._send_json({"error": "character_name required"}, 400)
            return
        removed = unlock_identity(name)
        self._send_json({"ok": True, "removed": removed, "character": name})

    def _handle_v6_brief_expand(self):
        """Opus expands a user's one-line brief into a structured project plan:
        characters, environments, shot list, tone. UI uses this to prefill the
        preproduction packages so the user can tweak rather than start blank."""
        from lib.claude_client import call_json, OPUS_MODEL
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        if lumn_validate:
            _ok, _err, _ = lumn_validate.validate(body, lumn_validate.BRIEF_EXPAND_SCHEMA)
            if not _ok:
                self._send_json({"error": _err}, 400)
                return
        brief = (body.get("brief") or "").strip()
        max_shots = int(body.get("max_shots", 8))

        # Budget gate — Opus text-only call ~$0.10
        est_cost = 0.10
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        system = """You are a pre-production planner for a short AI-generated film.
Expand the user's one-line idea into a structured plan the pipeline can use to
build reference sheets and a shot list.

Return JSON ONLY with this exact shape:
{
  "title": "short film title",
  "logline": "one-sentence logline",
  "tone": "tonal keywords, comma-sep (e.g., warm, nostalgic, observational)",
  "style_bible": "visual style one-liner (film stock, palette, lens feel)",
  "characters": [
    {
      "name": "Name",
      "role": "protagonist|supporting|background",
      "description": "45-word physical description — age, build, skin, hair, eyes, wardrobe, distinguishing features. Think character sheet prompt.",
      "must_keep": ["identifying trait 1", "trait 2", "trait 3"],
      "avoid": ["anti-trait"]
    }
  ],
  "environments": [
    {
      "name": "Location Name",
      "description": "30-word location description — time of day, weather, key props, light direction",
      "must_keep": ["trait 1", "trait 2"],
      "avoid": []
    }
  ],
  "shots": [
    {
      "shot_id": "s01",
      "title": "short title",
      "beat": "what happens in plain english",
      "shot_size": "wide|medium|close|extreme_close",
      "camera": "static|slow_push|handheld_track|pan|tilt",
      "duration_s": 5,
      "characters": ["Name"],
      "environment": "Location Name",
      "action": "one-sentence action for this shot"
    }
  ]
}

Constraints:
- 1-4 characters max
- 1-3 environments max
- {max_shots} shots max
- Shots should tell a complete micro-story with beginning/middle/end
- Every character and environment named in shots MUST exist in the characters/environments arrays
- No fantasy/sci-fi unless the brief demands it"""

        user_prompt = f"""Brief: {brief}
Max shots: {max_shots}
JSON only, no preamble."""

        try:
            result = call_json(
                user_prompt,
                system=system.replace("{max_shots}", str(max_shots)),
                model=OPUS_MODEL,
                max_tokens=4000,
            )
            # Ledger record — we successfully called Opus
            _record_generation(
                shot_key="brief_expand", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="ok",
                meta={"brief_len": len(brief)},
            )
            self._send_json({"ok": True, "plan": result})
        except Exception as e:
            _record_generation(
                shot_key="brief_expand", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="error",
                meta={"err": str(e)[:200]},
            )
            self._send_json({"error": str(e)}, 500)

    # ─── Opus Director endpoints (Phase 3) ─────────────────────────────────
    def _handle_v6_director_storyplan(self):
        """Opus director: brief + song timing → full Snyder-arc scene plan.
        Richer than brief/expand — writes emotion/acting/looking_at per scene."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        brief = (body.get("brief") or "").strip()
        duration_s = float(body.get("duration_s") or 60.0)
        project = body.get("project") or "default"
        profile_id = body.get("profile_id")
        song_analysis = body.get("song_analysis")
        environments = body.get("environments") or []
        thinking_budget = int(body.get("thinking_budget", 6000))

        # Opus extended-thinking call — budget gate
        est_cost = 0.75
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        try:
            from lib.opus_director import direct_story
            plan = direct_story(
                brief=brief,
                duration_s=duration_s,
                project=project,
                profile_id=profile_id,
                song_analysis=song_analysis,
                environments=environments,
                thinking_budget=thinking_budget,
            )
            _record_generation(
                shot_key="director_storyplan", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="ok",
                meta={"brief_len": len(brief), "duration_s": duration_s,
                      "project": project, "scenes": len(plan.get("scenes", []))},
            )
            self._send_json({"ok": True, "plan": plan})
        except Exception as e:
            _record_generation(
                shot_key="director_storyplan", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="error",
                meta={"err": str(e)[:200]},
            )
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_director_scene(self):
        """Opus director: skeletal scene → fully hydrated with acting/emotion/eyeline."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        spec = body.get("scene") or {}
        project = body.get("project") or "default"
        profile_id = body.get("profile_id")
        thinking_budget = int(body.get("thinking_budget", 2000))

        est_cost = 0.15
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        try:
            from lib.opus_director import direct_scene
            scene = direct_scene(
                minimal_spec=spec,
                project=project,
                profile_id=profile_id,
                thinking_budget=thinking_budget,
            )
            _record_generation(
                shot_key=f"director_scene:{spec.get('id','?')}", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="ok",
                meta={"project": project},
            )
            self._send_json({"ok": True, "scene": scene})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_director_kling_prompt(self):
        """Opus director: scene + anchor path → final Kling I2V prompt."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        scene = body.get("scene") or {}
        anchor_path = body.get("anchor_path")
        project = body.get("project") or "default"
        profile_id = body.get("profile_id")
        thinking_budget = int(body.get("thinking_budget", 1500))

        est_cost = 0.12
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        try:
            from lib.opus_director import direct_kling_prompt
            out = direct_kling_prompt(
                scene=scene,
                anchor_path=anchor_path,
                project=project,
                profile_id=profile_id,
                thinking_budget=thinking_budget,
            )
            _record_generation(
                shot_key=f"director_kling:{scene.get('id','?')}", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="ok",
                meta={"project": project, "prompt_len": len(out.get("prompt",""))},
            )
            self._send_json({"ok": True, "kling": out})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_director_critique(self):
        """Opus director: review a full scene plan for bible violations."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        plan = body.get("plan") or {}
        project = body.get("project") or "default"
        profile_id = body.get("profile_id")
        thinking_budget = int(body.get("thinking_budget", 4000))

        est_cost = 0.50
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        try:
            from lib.opus_director import direct_critique
            crit = direct_critique(
                scene_plan=plan,
                project=project,
                profile_id=profile_id,
                thinking_budget=thinking_budget,
            )
            _record_generation(
                shot_key="director_critique", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="ok",
                meta={"project": project, "verdict": crit.get("verdict")},
            )
            self._send_json({"ok": True, "critique": crit})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _director_context_for_shot(self, shot_id: str, project: str):
        """Load (shot, prev, next, character, environment, motifs) for direct_v6_shot.
        Scenes are sorted by orderIndex so prev/next are narrative neighbors."""
        from lib.active_project import get_project_root
        root = get_project_root(project)
        scenes_path = os.path.join(root, "prompt_os", "scenes.json")
        chars_path = os.path.join(root, "prompt_os", "characters.json")
        envs_path = os.path.join(root, "prompt_os", "environments.json")
        refs_path = os.path.join(root, "prompt_os", "references.json")

        def _load(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (IOError, json.JSONDecodeError):
                return []

        scenes = sorted(_load(scenes_path), key=lambda s: s.get("orderIndex", 0))
        chars = _load(chars_path)
        envs = _load(envs_path)
        refs = _load(refs_path)

        idx = next((i for i, s in enumerate(scenes) if s.get("id") == shot_id), -1)
        if idx < 0:
            return None, None, None, None, None, None, scenes_path, scenes
        shot = scenes[idx]
        prev_shot = scenes[idx - 1] if idx > 0 else None
        next_shot = scenes[idx + 1] if idx < len(scenes) - 1 else None

        char = next((c for c in chars if c.get("id") == shot.get("characterId")), None)
        env = next((e for e in envs if e.get("id") == shot.get("environmentId")), None)
        motif_ids = shot.get("motifIds") or shot.get("referenceIds") or []
        motifs = [r for r in refs if r.get("id") in motif_ids] if motif_ids else []

        return shot, prev_shot, next_shot, char, env, motifs, scenes_path, scenes

    @staticmethod
    def _apply_director_v2(scene: dict, result: dict) -> dict:
        """Merge result into scene.director_v2 and write populator fields through.

        Preserves originals under director_v2.original on first write, so the
        user can revert. Subsequent calls overwrite the director_v2 fields but
        leave the original snapshot untouched.
        """
        if not isinstance(scene, dict) or not isinstance(result, dict):
            return scene
        existing = scene.get("director_v2") or {}
        original = existing.get("original") or {
            "subjectAction": scene.get("subjectAction", ""),
            "shotDescription": scene.get("shotDescription", ""),
            "lighting": scene.get("lighting", ""),
            "cameraMovement": scene.get("cameraMovement", ""),
            "envMotion": scene.get("envMotion", ""),
            "continuityIn": scene.get("continuityIn", ""),
            "continuityOut": scene.get("continuityOut", ""),
            "subtext": scene.get("subtext", ""),
        }
        scene["director_v2"] = {
            "subjectAction": result.get("subjectAction", ""),
            "shotDescription": result.get("shotDescription", ""),
            "lighting": result.get("lighting", ""),
            "cameraMovement": result.get("cameraMovement", ""),
            "envMotion": result.get("envMotion", ""),
            "continuityIn": result.get("continuityIn", ""),
            "continuityOut": result.get("continuityOut", ""),
            "subtext": result.get("subtext", ""),
            "rationale": result.get("rationale", ""),
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "original": original,
        }
        # Fields the UI populator already reads — write through so the shot card shows the rewrite.
        for k in ("subjectAction", "shotDescription", "lighting", "cameraMovement",
                  "envMotion", "continuityIn", "continuityOut", "subtext"):
            v = result.get(k)
            if isinstance(v, str) and v.strip():
                scene[k] = v
        scene["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return scene

    def _handle_v6_director_direct_shot(self):
        """Opus director: rewrite a single shot card's fields for cinematic specificity.
        Body: {shot_id, project?, apply?: bool, thinking_budget?}"""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        shot_id = (body.get("shot_id") or "").strip()
        if not shot_id:
            self._send_json({"error": "shot_id required"}, 400)
            return
        project = body.get("project") or active_project.get_active_slug() or "default"
        apply_now = bool(body.get("apply", True))
        thinking_budget = int(body.get("thinking_budget", 1500))

        est_cost = 0.12
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        shot, prev_shot, next_shot, char, env, motifs, scenes_path, scenes = \
            self._director_context_for_shot(shot_id, project)
        if not shot:
            self._send_json({"error": f"shot {shot_id} not found in project {project}"}, 404)
            return

        try:
            from lib.opus_director import direct_v6_shot
            result = direct_v6_shot(
                shot=shot,
                prev_shot=prev_shot,
                next_shot=next_shot,
                character=char,
                environment=env,
                motifs=motifs,
                project=project,
                thinking_budget=thinking_budget,
            )
            if apply_now:
                self._apply_director_v2(shot, result)
                for i, s in enumerate(scenes):
                    if s.get("id") == shot_id:
                        scenes[i] = shot
                        break
                with open(scenes_path, "w", encoding="utf-8") as f:
                    json.dump(scenes, f, indent=2, ensure_ascii=False)
            _record_generation(
                shot_key=f"director_v6shot:{shot_id}", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="ok",
                meta={"project": project, "applied": apply_now},
            )
            self._send_json({"ok": True, "result": result, "applied": apply_now, "shot_id": shot_id})
        except Exception as e:
            _record_generation(
                shot_key=f"director_v6shot:{shot_id}", gen_type="image", engine="opus",
                tier="", duration=0, est_cost=est_cost, status="error",
                meta={"err": str(e)[:200]},
            )
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_director_direct_all(self):
        """Batch direct: rewrite every shot in the active project (or a subset).
        Body: {project?, shot_ids?: list, apply?: bool, thinking_budget?}

        Runs sequentially — Opus calls aren't cheap, and serialized writes avoid
        scenes.json contention. Returns a per-shot pass/fail summary."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        project = body.get("project") or active_project.get_active_slug() or "default"
        apply_now = bool(body.get("apply", True))
        thinking_budget = int(body.get("thinking_budget", 1500))
        only_ids = body.get("shot_ids") or None

        from lib.active_project import get_project_root
        scenes_path = os.path.join(get_project_root(project), "prompt_os", "scenes.json")
        try:
            with open(scenes_path, "r", encoding="utf-8") as f:
                scenes = sorted(json.load(f), key=lambda s: s.get("orderIndex", 0))
        except (IOError, json.JSONDecodeError):
            self._send_json({"error": "scenes.json not readable"}, 500)
            return

        if only_ids:
            only_ids_set = {str(x) for x in only_ids}
            targets = [s for s in scenes if s.get("id") in only_ids_set]
        else:
            targets = scenes
        if not targets:
            self._send_json({"error": "no shots to process"}, 400)
            return

        est_cost = 0.12 * len(targets)
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason,
                             "est": est_cost, "count": len(targets)}, 402)
            return

        from lib.opus_director import direct_v6_shot
        results = []
        for target in targets:
            shot_id = target.get("id")
            try:
                shot, prev_shot, next_shot, char, env, motifs, _sp, _scenes = \
                    self._director_context_for_shot(shot_id, project)
                if not shot:
                    results.append({"shot_id": shot_id, "ok": False, "error": "not_found"})
                    continue
                result = direct_v6_shot(
                    shot=shot, prev_shot=prev_shot, next_shot=next_shot,
                    character=char, environment=env, motifs=motifs,
                    project=project, thinking_budget=thinking_budget,
                )
                if apply_now:
                    self._apply_director_v2(shot, result)
                    for i, s in enumerate(scenes):
                        if s.get("id") == shot_id:
                            scenes[i] = shot
                            break
                    with open(scenes_path, "w", encoding="utf-8") as f:
                        json.dump(scenes, f, indent=2, ensure_ascii=False)
                results.append({"shot_id": shot_id, "ok": True,
                                "preview": {k: result.get(k, "") for k in
                                            ("subjectAction", "lighting", "cameraMovement")}})
                _record_generation(
                    shot_key=f"director_v6shot:{shot_id}", gen_type="image", engine="opus",
                    tier="", duration=0, est_cost=0.12, status="ok",
                    meta={"project": project, "batch": True, "applied": apply_now},
                )
            except Exception as e:
                results.append({"shot_id": shot_id, "ok": False, "error": str(e)[:200]})
                _record_generation(
                    shot_key=f"director_v6shot:{shot_id}", gen_type="image", engine="opus",
                    tier="", duration=0, est_cost=0.12, status="error",
                    meta={"err": str(e)[:200], "batch": True},
                )

        passed = sum(1 for r in results if r.get("ok"))
        self._send_json({"ok": True, "total": len(results),
                         "passed": passed, "failed": len(results) - passed,
                         "applied": apply_now, "results": results})

    def _handle_v6_director_variety_check(self):
        """Variety linter: scan every shot's populator fields for repetition.
        No LLM call — pure analysis. Body: {project?, threshold?}"""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            body = {}
        project = (body.get("project") if isinstance(body, dict) else None) \
            or active_project.get_active_slug() or "default"
        threshold = int(body.get("threshold", 3)) if isinstance(body, dict) else 3

        try:
            from lib.variety_linter import analyze_project
            report = analyze_project(project=project, threshold_overuse=threshold)
            self._send_json({"ok": True, "report": report, "project": project})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_anchor_audit_full(self):
        """Two-stage identity audit: perceptual pre-gate + Opus multi-image
        comparison. Returns decision + per-reference similarity breakdown."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        candidate_path = body.get("candidate_path") or body.get("anchor_path")
        sheet_path = body.get("sheet_path")
        priors = body.get("prior_anchor_paths") or []
        scene_ctx = body.get("scene_context")
        project = body.get("project") or "default"
        profile_id = body.get("profile_id")
        force_opus = bool(body.get("force_opus", False))
        thinking_budget = int(body.get("thinking_budget", 3000))

        if not candidate_path:
            self._send_json({"error": "candidate_path required"}, 400)
            return

        est_cost = 0.25 if force_opus else 0.10
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        try:
            from lib.identity_gate_opus import audit_anchor_full
            result = audit_anchor_full(
                candidate_path=candidate_path,
                sheet_path=sheet_path,
                prior_anchor_paths=priors,
                scene_context=scene_ctx,
                project=project,
                profile_id=profile_id,
                force_opus=force_opus,
                thinking_budget=thinking_budget,
            )
            engine = "perceptual" if result.get("opus") is None else "opus+perceptual"
            _record_generation(
                shot_key=f"audit_full:{candidate_path.split('/')[-1]}",
                gen_type="image", engine=engine,
                tier="", duration=0, est_cost=est_cost, status="ok",
                meta={"verdict": result.get("final_verdict"),
                      "path": result.get("decision_path")},
            )
            self._send_json({"ok": True, "audit": result})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_anchor_audit_meta(self):
        """Phase 5: self-consistency vote + devil's-advocate meta-critique.
        Costs 3x a single audit but catches false-passes on hero shots."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        candidate_path = body.get("candidate_path") or body.get("anchor_path")
        sheet_path = body.get("sheet_path")
        priors = body.get("prior_anchor_paths") or []
        scene_ctx = body.get("scene_context")
        project = body.get("project") or "default"
        profile_id = body.get("profile_id")
        n_votes = int(body.get("n_votes", 3))
        escalate_on = body.get("escalate_on", "LOW")
        thinking_budget = int(body.get("thinking_budget", 3000))

        if not candidate_path:
            self._send_json({"error": "candidate_path required"}, 400)
            return

        # meta_audit = N Opus vision calls + optional meta-critique
        est_cost = 0.25 * n_votes + 0.30
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        try:
            from lib.identity_gate_opus import audit_anchor_full
            from lib.meta_audit import meta_audit
            result = meta_audit(
                audit_anchor_full,
                candidate_path=candidate_path,
                sheet_path=sheet_path,
                prior_anchor_paths=priors,
                scene_context=scene_ctx,
                project=project,
                profile_id=profile_id,
                force_opus=True,              # always use Opus in meta mode
                thinking_budget=thinking_budget,
                n_votes=n_votes,
                escalate_on=escalate_on,
            )
            _record_generation(
                shot_key=f"audit_meta:{candidate_path.split('/')[-1]}",
                gen_type="image", engine="opus-meta",
                tier="", duration=0, est_cost=est_cost, status="ok",
                meta={"final": result.get("final_verdict"),
                      "escalated": result.get("escalated"),
                      "ratio": result.get("vote", {}).get("agreement_ratio")},
            )
            self._send_json({"ok": True, "meta_audit": result})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_prompt_lint(self):
        """Pre-flight Kling prompt validation (no cost)."""
        from lib.kling_prompt_linter import lint_kling_prompt
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        prompt = body.get("prompt", "")
        strict = bool(body.get("strict", False))
        result = lint_kling_prompt(prompt, strict=strict)
        self._send_json(result)

    # ─── Auth: signup / login / logout / me ──────────────────────────────
    #
    # Session cookie: lumn_sid (httpOnly, SameSite=Lax). The front-end
    # doesn't touch it directly — fetch() includes cookies automatically.
    # We keep the legacy LUMN_API_TOKEN bearer for local dev/CLI tooling.

    def _set_session_cookie(self, sid: str):
        # HttpOnly + SameSite=Lax keeps CSRF surface small. Secure flag is
        # added when the request came in over HTTPS (set by reverse proxy).
        parts = [
            f"lumn_sid={sid}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={lumn_db.SESSION_TTL_SECONDS}" if lumn_db else "Max-Age=2592000",
        ]
        if self.headers.get("X-Forwarded-Proto") == "https":
            parts.append("Secure")
        self._extra_set_cookie = "; ".join(parts)

    def _handle_feedback_submit(self):
        """In-app bug report capture. Authenticated or anon."""
        if not lumn_db:
            return self._send_json({"error": "db unavailable"}, 503)
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            return self._send_json({"error": "invalid json"}, 400)
        message = (body.get("message") or "").strip()
        if not message:
            return self._send_json({"error": "message required"}, 400)
        if len(message) > 8000:
            return self._send_json({"error": "message too long"}, 400)
        # H6: IP rate limit so anon feedback can't be used as a write amp /
        # storage-fill vector. 20/hour is generous for a real human bug-reporter.
        client_ip = self._real_client_ip()
        allowed, _count = lumn_db.rate_limit_check_ip(client_ip, "feedback", max_per_hour=20)
        if not allowed:
            return self._send_json({"error": "feedback rate limit — try again later"}, 429)
        category = (body.get("category") or "bug").strip().lower()[:40]
        cu = self._current_user() or {}
        try:
            fid = lumn_db.insert_feedback(
                user_id=cu.get("id") or None,
                email=cu.get("email"),
                category=category,
                message=message,
                context=(body.get("context") or "")[:8000],
                user_agent=self.headers.get("User-Agent", "")[:300],
                url=(body.get("url") or "")[:500],
            )
        except Exception as e:
            return self._send_json({"error": f"insert failed: {e}"}, 500)
        if lumn_obs:
            try:
                lumn_obs.structured_log("info", "feedback_submitted",
                                        id=fid, category=category,
                                        user_id=cu.get("id"))
            except Exception:
                pass
        return self._send_json({"ok": True, "id": fid})

    def _handle_shot_rate(self):
        """Thumbs up/down on a generated shot. Feeds the TI learning loop."""
        if not lumn_db:
            return self._send_json({"error": "db unavailable"}, 503)
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            return self._send_json({"error": "invalid json"}, 400)
        cu = self._current_user() or {}
        uid = int(cu.get("id") or 0)
        if uid <= 0:
            return self._send_json({"error": "auth required"}, 401)
        shot_id = (body.get("shot_id") or "").strip()
        rating = body.get("rating")
        if not shot_id or rating not in (-1, 0, 1):
            return self._send_json({"error": "shot_id + rating in {-1,0,1} required"}, 400)
        raw_asset = body.get("asset_path")
        safe_asset = None
        if isinstance(raw_asset, str) and raw_asset:
            resolved = _safe_user_path(raw_asset)
            safe_asset = resolved if resolved else None
        try:
            rid = lumn_db.insert_shot_rating(
                user_id=uid,
                shot_id=shot_id,
                rating=int(rating),
                asset_path=safe_asset,
                prompt=body.get("prompt"),
                reason=body.get("reason"),
                meta=body.get("meta"),
            )
        except Exception as e:
            return self._send_json({"error": f"insert failed: {e}"}, 500)

        # Bridge the rating into the TI learning system so the optimizer can
        # cluster failure patterns and tune prompt rules. Best-effort — the
        # rating is already persisted in SQL; this is the feedback signal.
        try:
            from lib import learning_system as _ls
            outcome = "pass" if int(rating) > 0 else "fail" if int(rating) < 0 else "neutral"
            _ls.log_attempt(
                project_id=str(body.get("project_id") or "beta"),
                scene_id=str(body.get("scene_id") or "unknown"),
                shot_id=shot_id,
                attempt_data={
                    "final_outcome": outcome,
                    "failure_type": body.get("reason") if outcome == "fail" else None,
                    "prompt_version": (body.get("prompt") or "")[:200],
                    "user_rating": int(rating),
                    "user_id": uid,
                    "asset_path": safe_asset,
                    "source": "user_feedback",
                    "meta": body.get("meta") or {},
                },
            )
        except Exception:
            pass  # learning is advisory; never block the rating

        return self._send_json({"ok": True, "id": rid})

    def _handle_auth_signup(self):
        if not lumn_db:
            return self._send_json({"error": "db unavailable"}, 503)
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            return self._send_json({"error": "invalid json"}, 400)

        # IP-based signup rate limit (5/hour) — blocks automated account farms.
        client_ip = self._real_client_ip()
        allowed, _count = lumn_db.rate_limit_check_ip(client_ip, "signup", max_per_hour=5)
        if not allowed:
            return self._send_json({"error": "signup rate limit — try again in an hour"}, 429)

        # Shared beta password gate. Set LUMN_BETA_PASSWORD in env; hand
        # the value out with your invites. If unset, signup is open.
        beta_pw_required = os.environ.get("LUMN_BETA_PASSWORD", "")
        if beta_pw_required:
            submitted = (body.get("beta_password") or body.get("invite_code") or "").strip()
            if not hmac.compare_digest(submitted, beta_pw_required):
                return self._send_json({"error": "invalid beta access code"}, 403)

        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        try:
            uid = lumn_db.create_user(email, password, credits_cents=500)
        except ValueError as e:
            return self._send_json({"error": str(e)}, 400)

        # Auto-promote to admin if email is in LUMN_ADMIN_EMAILS env (comma-sep).
        admin_emails = {e.strip().lower() for e in
                        os.environ.get("LUMN_ADMIN_EMAILS", "").split(",") if e.strip()}
        if email.lower() in admin_emails:
            try:
                with lumn_db._conn() as _c:
                    _c.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
            except Exception as _e:
                print(f"[auth] admin promote failed: {_e}")
        sid = lumn_db.create_session(uid)
        u = lumn_db.get_user(uid)
        # Set-Cookie directly in the response
        body_out = json.dumps({"ok": True, "user": {
            "id": u["id"], "email": u["email"],
            "credits_cents": u["credits_cents"], "role": u["role"],
        }, "csrf_token": _csrf_for_sid(sid)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.send_header("Set-Cookie", _build_session_cookie(sid))
        self.end_headers()
        self.wfile.write(body_out)

    def _handle_auth_login(self):
        if not lumn_db:
            return self._send_json({"error": "db unavailable"}, 503)
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            return self._send_json({"error": "invalid json"}, 400)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        # M2: per-IP login rate limit to blunt credential-stuffing. 30/hour is
        # generous for a human who forgot their password; bots hitting this
        # should be cut off long before they exhaust a dictionary.
        client_ip = self._real_client_ip()
        allowed, _count = lumn_db.rate_limit_check_ip(client_ip, "login", max_per_hour=30)
        if not allowed:
            return self._send_json({"error": "login rate limit — try again later"}, 429)
        u = lumn_db.authenticate(email, password)
        if not u:
            return self._send_json({"error": "invalid credentials"}, 401)
        sid = lumn_db.create_session(u["id"])
        body_out = json.dumps({"ok": True, "user": {
            "id": u["id"], "email": u["email"],
            "credits_cents": u["credits_cents"], "role": u["role"],
        }, "csrf_token": _csrf_for_sid(sid)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.send_header("Set-Cookie", _build_session_cookie(sid))
        self.end_headers()
        self.wfile.write(body_out)

    def _handle_auth_logout(self):
        if not lumn_db:
            return self._send_json({"error": "db unavailable"}, 503)
        sid = self._parse_cookie("lumn_sid")
        if sid:
            lumn_db.destroy_session(sid)
        body_out = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.send_header("Set-Cookie", _build_session_cookie("", max_age=0))
        self.end_headers()
        self.wfile.write(body_out)

    def _handle_auth_me(self):
        u = self._current_user()
        if not u:
            return self._send_json({"authenticated": False}, 200)
        sid = self._parse_cookie("lumn_sid") or ""
        # Best-effort sync: overwrite the user's LUMN credits with the live
        # fal.ai account balance. No-op when FAL_ADMIN_KEY isn't configured
        # (or any fal/DB error). The 10s in-process cache on get_fal_balance
        # keeps this cheap on rapid auth/me polls.
        credits_cents = int(u.get("credits_cents", 0) or 0)
        try:
            from lib.fal_billing import sync_user_credits_to_fal
            synced = sync_user_credits_to_fal(int(u["id"]))
            if synced is not None:
                credits_cents = synced
        except Exception:
            pass
        return self._send_json({
            "authenticated": True,
            "user": {
                "id": u["id"],
                "email": u["email"],
                "credits_cents": credits_cents,
                "role": u.get("role", "user"),
            },
            "csrf_token": _csrf_for_sid(sid),
        })

    def _handle_fal_balance(self):
        """Return the true fal.ai account balance (in dollars).

        Reads FAL_ADMIN_KEY (or falls back to FAL_API_KEY) and calls
        https://api.fal.ai/v1/account/billing. Requires an admin-scoped
        fal key — regular keys return 403. Cached in-process for 10s.
        """
        if not self._current_user():
            return self._send_json({"ok": False, "error": "unauthenticated"}, 401)
        try:
            from lib.fal_billing import get_fal_balance
            data = get_fal_balance()
        except Exception as e:
            return self._send_json({"ok": False, "error": f"exc:{e.__class__.__name__}"}, 500)
        return self._send_json(data)

    def _handle_v6_prompt_assemble(self):
        """Preview the enriched prompt for a given raw prompt + shot_context.
        Lets the UI show exactly what will be sent to Gemini/Kling before paying."""
        from lib.v6_prompt_assembler import assemble_v6_prompt
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        if lumn_validate:
            _ok, _err, _ = lumn_validate.validate(body, lumn_validate.PROMPT_ASSEMBLE_SCHEMA)
            if not _ok:
                self._send_json({"error": _err}, 400)
                return
        raw_prompt = body.get("prompt", "")
        shot_context = body.get("shot_context") or {}
        target = (body.get("target") or "anchor").lower()  # anchor | clip
        include_desc = target != "clip"
        max_chars = 900 if include_desc else 400
        result = assemble_v6_prompt(
            raw_prompt=raw_prompt,
            shot_context=shot_context,
            include_description=include_desc,
            max_chars=max_chars,
        )
        self._send_json({"ok": True, "target": target, **result})

    # -- Async job helpers -----------------------------------------------

    def _serialize_job(self, row: dict) -> dict:
        out = dict(row)
        # Parse JSON columns for client convenience
        for k in ("input_json", "result_json"):
            if out.get(k):
                try:
                    out[k.replace("_json", "")] = json.loads(out[k])
                except Exception:
                    pass
        return out

    def _stream_job(self, job_id: str) -> None:
        """Server-Sent Events stream of a job's progress. Closes when the
        job hits a terminal state (done|failed) or after a 5-minute cap."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        deadline = time.time() + 300
        last_payload = None
        while time.time() < deadline:
            row = lumn_db.get_job(job_id) if lumn_db else None
            if not row:
                try:
                    self.wfile.write(b"event: error\ndata: {\"error\":\"gone\"}\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                return
            payload = json.dumps(self._serialize_job(row), default=str)
            if payload != last_payload:
                try:
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    return
                last_payload = payload
            if row["status"] in ("done", "failed"):
                return
            time.sleep(1.0)

    def _handle_v6_anchor_generate_async(self):
        """Enqueue an anchor generation job, return job_id immediately.
        Pre-checks (auth, validation, moderation, budget, rate limit) all
        happen synchronously; only the fal.ai call runs in the worker."""
        if not lumn_worker:
            return self._send_json({"error": "worker unavailable"}, 503)
        from lib.v6_prompt_assembler import assemble_v6_prompt, resolve_reference_paths, load_motif_refs_for_shot, load_pos_entity_refs_for_shot, load_pos_identity_clauses_for_shot, parse_at_mentions
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            return self._send_json({"error": "Invalid JSON"}, 400)
        if lumn_validate:
            ok, err, _ = lumn_validate.validate(body, lumn_validate.ANCHOR_GENERATE_SCHEMA)
            if not ok:
                return self._send_json({"error": err}, 400)

        cu = self._current_user() or {}
        uid = int(cu.get("id", 0) or 0)
        if uid <= 0:
            return self._send_json({"error": "auth required"}, 401)

        # Daily cap
        cap = int(os.environ.get("LUMN_DAILY_CAP_CENTS", "0") or "0")
        if cap > 0 and lumn_db:
            spent = lumn_db.global_spend_since(86400)
            if spent >= cap:
                return self._send_json({"error": "daily_spend_cap_reached",
                                        "spent_cents": spent, "cap_cents": cap}, 503)

        raw_prompt = body.get("prompt", "")
        # SECURITY (H7): fail-closed on moderation exception.
        try:
            from lib.moderation import moderate_prompt_strict
            mod = moderate_prompt_strict(raw_prompt, nsfw_allowed=False)
            if not mod["allowed"]:
                return self._send_json({"error": "moderation_blocked",
                                        "severity": mod["severity"],
                                        "reasons": mod["reasons"]}, 451)
            if mod["severity"] == "warn":
                raw_prompt = mod["redacted_prompt"]
        except Exception:
            return self._send_json({"error": "moderation unavailable"}, 500)

        num_images = max(1, int(body.get("num_images", 1) or 1))
        est_cost = _price_for("gemini", "", 0, "image") * num_images
        ok_b, reason, _ = _check_budget_gate(est_cost)
        if not ok_b:
            return self._send_json({"error": "budget_exceeded", "reason": reason}, 402)

        cost_cents = max(1, int(round(est_cost * 100)))
        if lumn_db:
            allowed, n = lumn_db.rate_limit_check(uid, "anchor", max_per_hour=30)
            if not allowed:
                return self._send_json({"error": "rate_limited", "kind": "anchor",
                                        "count_in_window": n, "max_per_hour": 30}, 429)

        # SECURITY (H3): atomic credit reserve — kills TOCTOU double-spend.
        # Cached session balance is untrustworthy under concurrency; the
        # DB-level atomic debit is the only safe gate.
        if lumn_db and not _user_credits_deferred():
            if not lumn_db.charge_user(uid, cost_cents, "reserve_anchor",
                                       {"shot_id": body.get("shot_id"), "async": True}):
                return self._send_json({"error": "insufficient_credits",
                                        "need_cents": cost_cents}, 402)

        # Enrich prompt + resolve refs (synchronously — these are cheap)
        shot_context = body.get("shot_context") or {}
        try:
            _active_slug = active_project.get_active_slug() or "default"
        except Exception:
            _active_slug = "default"
        enriched = assemble_v6_prompt(raw_prompt=raw_prompt, shot_context=shot_context,
                                      include_description=True, max_chars=900,
                                      project_slug=_active_slug)
        prompt = enriched["enriched_prompt"]
        # PROJECT-SCOPED REF RESOLUTION.
        # Only POS (scenes.json characterId/environmentId/costumeId + references.json
        # motifs) drives ref selection now. Client-provided reference_image_paths
        # are logged-and-discarded unless they pass _safe_project_ref_path
        # (which rejects preproduction/pkg_* and any other project's workspace).
        # Fixes the 2026-04-20 Buddy/Owen/Maya cross-project leak.
        ref_paths: list[str] = []
        _async_shot_id = body.get("shot_id", "")

        # Per-shot UI overrides — user-excluded entity/motif UUIDs (via ref chips).
        _excluded_ids = set()
        try:
            _sctx = shot_context or {}
            raw_excl = _sctx.get("excluded_ids") if isinstance(_sctx, dict) else None
            if isinstance(raw_excl, list):
                _excluded_ids = {str(x) for x in raw_excl if x}
        except Exception:
            _excluded_ids = set()

        pos_paths = load_pos_entity_refs_for_shot(
            _async_shot_id, project=_active_slug, exclude_ids=_excluded_ids
        )
        for pp in pos_paths:
            safe_p = _safe_project_ref_path(pp, _active_slug)
            if safe_p and safe_p not in ref_paths:
                ref_paths.append(safe_p)

        # Client-supplied refs (from UI's /api/v6/references pool) only accepted
        # when they pass project-scope validation. If UI sends a pkg_char_*
        # from preproduction/, it's dropped silently and logged.
        client_refs_raw = body.get("reference_image_paths") or []
        _client_dropped = 0
        for p in client_refs_raw:
            if not isinstance(p, str):
                continue
            safe = _safe_project_ref_path(p, _active_slug)
            if safe:
                if safe not in ref_paths:
                    ref_paths.append(safe)
            else:
                _client_dropped += 1
        if _client_dropped:
            print(f"[ANCHOR async] dropped {_client_dropped} out-of-project refs for shot={_async_shot_id}")

        # Motif injection: append shot-specific motif approvedRefs so beads,
        # pawprint, gold fur, etc. actually reach Gemini as visual refs.
        motif_paths = load_motif_refs_for_shot(
            _async_shot_id, project=_active_slug, exclude_ids=_excluded_ids
        )
        for mp in motif_paths:
            safe_m = _safe_project_ref_path(mp, _active_slug)
            if safe_m and safe_m not in ref_paths:
                ref_paths.append(safe_m)

        # @mention resolution: @<Name> tokens in the raw prompt force-attach
        # that entity's sheet even if it's not the scene's configured character.
        # Extraction scans both enriched prompt (post-assembler) and raw prompt
        # (pre-enrichment) to catch mentions in either source.
        try:
            _mention_text = (raw_prompt or "") + " " + (prompt or "")
            mentions = parse_at_mentions(_mention_text, project=_active_slug)
            _mention_added = 0
            for m in mentions:
                sp = m.get("sheet_path")
                if not sp:
                    continue
                safe = _safe_project_ref_path(sp, _active_slug)
                if safe and safe not in ref_paths:
                    ref_paths.append(safe)
                    _mention_added += 1
            if mentions:
                print(f"[ANCHOR async] @mentions resolved={len(mentions)} added={_mention_added} "
                      f"names={[m.get('name') for m in mentions]}")
        except Exception as _e:
            print(f"[ANCHOR async] @mention parse error: {_e}")

        ref_paths = ref_paths[:8]

        # Identity-mark text injection: sheet image alone doesn't lock
        # emblem orientation — Gemini redraws marks semantically.
        _id_clause = load_pos_identity_clauses_for_shot(
            _async_shot_id, project=_active_slug, exclude_ids=_excluded_ids
        )
        if _id_clause:
            prompt = (prompt.rstrip(". ") + ". " + _id_clause).strip()
        try:
            _async_motifs = len(locals().get("motif_paths") or [])
        except Exception:
            _async_motifs = 0
        print(f"[ANCHOR async] project={_active_slug} shot={_async_shot_id} refs={len(ref_paths)} motifs={_async_motifs} id_mark={'Y' if _id_clause else 'N'}")

        payload = {
            "user_id": uid,
            "shot_id": body.get("shot_id", "unknown"),
            "prompt": prompt,
            "ref_paths": ref_paths,
            "num_images": num_images,
            "cost_cents": cost_cents,
            "reserved": True,  # tells runner to skip charge_user (already paid)
        }
        try:
            job_id = lumn_worker.enqueue("v6_anchor", user_id=uid, payload=payload)
        except Exception as e:
            # Enqueue failed — refund the reserve immediately.
            if lumn_db:
                try:
                    lumn_db.refund_credits(uid, cost_cents, "refund_anchor",
                                           {"reason": "enqueue_failed"})
                except Exception:
                    pass
            return self._send_json({"error": f"enqueue failed: {e}"}, 500)
        return self._send_json({"ok": True, "job_id": job_id, "status": "queued"}, 202)

    def _handle_v6_clip_generate_async(self):
        """Enqueue a Kling i2v job. See _handle_v6_anchor_generate_async for shape."""
        if not lumn_worker:
            return self._send_json({"error": "worker unavailable"}, 503)
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            return self._send_json({"error": "Invalid JSON"}, 400)
        if lumn_validate:
            ok, err, _ = lumn_validate.validate(body, lumn_validate.CLIP_GENERATE_SCHEMA)
            if not ok:
                return self._send_json({"error": err}, 400)

        cu = self._current_user() or {}
        uid = int(cu.get("id", 0) or 0)
        if uid <= 0:
            return self._send_json({"error": "auth required"}, 401)

        cap = int(os.environ.get("LUMN_DAILY_CAP_CENTS", "0") or "0")
        if cap > 0 and lumn_db:
            spent = lumn_db.global_spend_since(86400)
            if spent >= cap:
                return self._send_json({"error": "daily_spend_cap_reached",
                                        "spent_cents": spent, "cap_cents": cap}, 503)

        raw_prompt = body.get("prompt", "")
        multi_prompt = body.get("multi_prompt") or None
        if isinstance(multi_prompt, list) and not multi_prompt:
            multi_prompt = None

        # Moderate: single prompt OR every beat in multi_prompt
        try:
            from lib.moderation import moderate_prompt_strict
            texts_to_check = []
            if raw_prompt:
                texts_to_check.append(("prompt", raw_prompt))
            if multi_prompt:
                for i, beat in enumerate(multi_prompt):
                    p = (beat or {}).get("prompt", "") if isinstance(beat, dict) else ""
                    if p:
                        texts_to_check.append((f"multi_prompt[{i}]", p))
            for label, text in texts_to_check:
                mod = moderate_prompt_strict(text, nsfw_allowed=False)
                if not mod["allowed"]:
                    return self._send_json({"error": "moderation_blocked",
                                            "field": label,
                                            "severity": mod["severity"],
                                            "reasons": mod["reasons"]}, 451)
                if mod["severity"] == "warn":
                    if label == "prompt":
                        raw_prompt = mod["redacted_prompt"]
                    elif multi_prompt:
                        idx = int(label.split("[")[1].rstrip("]"))
                        multi_prompt[idx]["prompt"] = mod["redacted_prompt"]
        except Exception:
            return self._send_json({"error": "moderation unavailable"}, 500)

        # Duration: single clip = body.duration; multi_prompt = sum of beats.
        duration_source = "manual"
        if multi_prompt:
            try:
                duration = sum(int((b or {}).get("duration", 5) or 5) for b in multi_prompt)
            except Exception:
                return self._send_json({"error": "invalid multi_prompt durations"}, 400)
            if duration < 3 or duration > 15:
                return self._send_json({"error": f"multi_prompt total duration {duration}s out of range 3-15"}, 400)
            duration_source = "multi_prompt"
        elif body.get("duration") is not None:
            duration = int(body.get("duration") or 5)
            if duration < 3 or duration > 15:
                return self._send_json({"error": f"duration {duration}s out of range 3-15"}, 400)
            duration_source = "manual"
        else:
            scene_dur, src = _scene_duration_for_shot(body.get("shot_id", ""))
            if scene_dur is not None and 3 <= scene_dur <= 15:
                duration = scene_dur
                duration_source = src
            else:
                duration = 5
                duration_source = "default"
        print(f"[KLING] shot={body.get('shot_id','?')} dur={duration}s src={duration_source}")

        est_cost = _price_for("kling", "", duration, "video")
        ok_b, reason, _ = _check_budget_gate(est_cost)
        if not ok_b:
            return self._send_json({"error": "budget_exceeded", "reason": reason}, 402)
        cost_cents = max(1, int(round(est_cost * 100)))
        if lumn_db:
            allowed, n = lumn_db.rate_limit_check(uid, "clip", max_per_hour=20)
            if not allowed:
                return self._send_json({"error": "rate_limited", "kind": "clip",
                                        "count_in_window": n, "max_per_hour": 20}, 429)

        anchor_path = _safe_user_path(body.get("anchor_path") or "")
        if not anchor_path:
            return self._send_json({"error": "anchor_path missing or outside pipeline root"}, 400)

        end_image_raw = body.get("end_image_path") or ""
        end_image_path = _safe_user_path(end_image_raw) if end_image_raw else None

        tier = body.get("tier") or body.get("engine") or "v3_standard"
        elements = body.get("elements") or None
        cfg_scale = float(body.get("cfg_scale", 0.5) or 0.5)

        # SECURITY (H3): atomic credit reserve — kills TOCTOU double-spend.
        if lumn_db and not _user_credits_deferred():
            if not lumn_db.charge_user(uid, cost_cents, "reserve_clip",
                                       {"shot_id": body.get("shot_id"), "async": True}):
                return self._send_json({"error": "insufficient_credits",
                                        "need_cents": cost_cents}, 402)

        payload = {
            "user_id": uid,
            "shot_id": body.get("shot_id", "unknown"),
            "prompt": raw_prompt,
            "anchor_path": anchor_path,
            "duration": duration,
            "cost_cents": cost_cents,
            "reserved": True,
            "tier": tier,
            "end_image_path": end_image_path,
            "multi_prompt": multi_prompt,
            "elements": elements,
            "cfg_scale": cfg_scale,
        }
        try:
            job_id = lumn_worker.enqueue("v6_clip", user_id=uid, payload=payload)
        except Exception as e:
            if lumn_db:
                try:
                    lumn_db.refund_credits(uid, cost_cents, "refund_clip",
                                           {"reason": "enqueue_failed"})
                except Exception:
                    pass
            return self._send_json({"error": f"enqueue failed: {e}"}, 500)
        return self._send_json({"ok": True, "job_id": job_id, "status": "queued"}, 202)

    def _handle_v6_anchor_generate(self):
        """Generate anchor image via Gemini 3.1 Flash edit mode."""
        from lib.fal_client import gemini_edit_image
        from lib.v6_prompt_assembler import assemble_v6_prompt, resolve_reference_paths, load_motif_refs_for_shot, load_pos_entity_refs_for_shot, load_pos_identity_clauses_for_shot, parse_at_mentions
        from lib.claude_client import call_vision_json, OPUS_MODEL
        from lib.identity_gate import maybe_auto_lock
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        if lumn_validate:
            _ok, _err, _ = lumn_validate.validate(body, lumn_validate.ANCHOR_GENERATE_SCHEMA)
            if not _ok:
                self._send_json({"error": _err}, 400)
                return

        raw_prompt = body.get("prompt", "")
        reference_image_paths = body.get("reference_image_paths", [])
        shot_id = body.get("shot_id", "unknown")
        num_images = body.get("num_images", 1)
        shot_context = body.get("shot_context") or {}

        # Global daily spend circuit breaker — last-resort cost cap across
        # all users. Set LUMN_DAILY_CAP_CENTS in env; unset = no cap.
        _cap = int(os.environ.get("LUMN_DAILY_CAP_CENTS", "0") or "0")
        if _cap > 0 and lumn_db:
            _spent = lumn_db.global_spend_since(86400)
            if _spent >= _cap:
                return self._send_json({
                    "error": "daily_spend_cap_reached",
                    "spent_cents": _spent, "cap_cents": _cap,
                }, 503)

        # Content moderation pre-filter — strict (keyword + Opus borderline).
        # SECURITY (H7): fail-closed on exception.
        try:
            from lib.moderation import moderate_prompt_strict
            _mod = moderate_prompt_strict(raw_prompt, nsfw_allowed=False)
            if not _mod["allowed"]:
                return self._send_json({
                    "error": "moderation_blocked",
                    "severity": _mod["severity"],
                    "reasons": _mod["reasons"],
                }, 451)
            if _mod["severity"] == "warn":
                raw_prompt = _mod["redacted_prompt"]
        except Exception:
            return self._send_json({"error": "moderation unavailable"}, 500)

        # Hard budget gate — anchor gen is billed per image; never start a
        # request that would push the ledger past budget. (#47)
        est_cost = _price_for("gemini", "", 0, "image") * max(1, int(num_images))
        ok, reason, _tracker = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        # Per-user credits + rate limit (only for DB-backed users, id > 0).
        _cu = self._current_user() or {}
        _uid = int(_cu.get("id", 0) or 0)
        if _uid > 0 and lumn_db:
            _cost_cents = max(1, int(round(est_cost * 100)))
            _bal = int(_cu.get("credits_cents", 0) or 0)
            if not _user_credits_deferred() and _bal < _cost_cents:
                self._send_json({
                    "error": "insufficient_credits",
                    "balance_cents": _bal,
                    "need_cents": _cost_cents,
                }, 402)
                return
            allowed, n = lumn_db.rate_limit_check(_uid, "anchor", max_per_hour=30)
            if not allowed:
                self._send_json({
                    "error": "rate_limited",
                    "kind": "anchor",
                    "count_in_window": n,
                    "max_per_hour": 30,
                }, 429)
                return

        # V6 enrichment: load packages.json and inject entity metadata into the
        # prompt so character/costume/env/prop fields the user filled in
        # actually reach Gemini. include_description=True — anchors build identity.
        try:
            _active_slug = active_project.get_active_slug() or "default"
        except Exception:
            _active_slug = "default"
        enriched = assemble_v6_prompt(
            raw_prompt=raw_prompt,
            shot_context=shot_context,
            include_description=True,
            max_chars=900,
            project_slug=_active_slug,
        )
        prompt = enriched["enriched_prompt"]

        # PROJECT-SCOPED REF RESOLUTION.
        # POS (scenes.json characterId/environmentId/costumeId + references.json
        # motifs) is the only authoritative source. Client-supplied refs are
        # validated against project scope; anything pointing to preproduction/
        # or another project's workspace is dropped. Fixes the 2026-04-20
        # Buddy/Owen/Maya cross-project leak.
        valid_refs: list[str] = []

        # Per-shot UI overrides from ref chips.
        _excluded_ids = set()
        try:
            _sctx = shot_context or {}
            raw_excl = _sctx.get("excluded_ids") if isinstance(_sctx, dict) else None
            if isinstance(raw_excl, list):
                _excluded_ids = {str(x) for x in raw_excl if x}
        except Exception:
            _excluded_ids = set()

        pos_paths = load_pos_entity_refs_for_shot(
            shot_id, project=_active_slug, exclude_ids=_excluded_ids
        )
        for pp in pos_paths:
            safe_p = _safe_project_ref_path(pp, _active_slug)
            if safe_p and safe_p not in valid_refs:
                valid_refs.append(safe_p)

        # Client refs — accepted only when they pass project-scope validation.
        _client_dropped = 0
        for p in reference_image_paths:
            if not isinstance(p, str):
                continue
            safe = _safe_project_ref_path(p, _active_slug)
            if safe:
                if safe not in valid_refs:
                    valid_refs.append(safe)
            else:
                _client_dropped += 1
        if _client_dropped:
            print(f"[ANCHOR] dropped {_client_dropped} out-of-project refs for shot={shot_id}")

        # Motif injection: look up shot_ref_map_v8.json → references.json and
        # append each motif's approvedRef as a visual ref. This is how motifs
        # (necklace, beads, pawprint, gold fur) actually reach Gemini — prose
        # alone isn't enough per `feedback_refs_not_text.md`.
        motif_paths = load_motif_refs_for_shot(
            shot_id, project=_active_slug, exclude_ids=_excluded_ids
        )
        for mp in motif_paths:
            safe_m = _safe_project_ref_path(mp, _active_slug)
            if safe_m and safe_m not in valid_refs:
                valid_refs.append(safe_m)

        # @mention resolution: @<Name> tokens force-attach the mentioned
        # entity's sheet, covering the cross-character case where user wants
        # a second character ref in a scene that's owned by another.
        try:
            _mention_text = (raw_prompt or "") + " " + (prompt or "")
            mentions = parse_at_mentions(_mention_text, project=_active_slug)
            _mention_added = 0
            for m in mentions:
                sp = m.get("sheet_path")
                if not sp:
                    continue
                safe = _safe_project_ref_path(sp, _active_slug)
                if safe and safe not in valid_refs:
                    valid_refs.append(safe)
                    _mention_added += 1
            if mentions:
                print(f"[ANCHOR] @mentions resolved={len(mentions)} added={_mention_added} "
                      f"names={[m.get('name') for m in mentions]}")
        except Exception as _e:
            print(f"[ANCHOR] @mention parse error: {_e}")

        # Cap at 8 total (Gemini edit handles 10+ but latency grows with count)
        valid_refs = valid_refs[:8]

        # Identity-mark text injection: the sheet image alone doesn't lock
        # emblem orientation — Gemini redraws marks semantically. Append the
        # character's canonical identityMark prose so orientation is explicit.
        id_clause = load_pos_identity_clauses_for_shot(
            shot_id, project=_active_slug, exclude_ids=_excluded_ids
        )
        if id_clause:
            prompt = (prompt.rstrip(". ") + ". " + id_clause).strip()

        print(f"[ANCHOR] project={_active_slug} shot={shot_id} pos={len(pos_paths)} motifs={len(motif_paths)} refs={len(valid_refs)} id_mark={'Y' if id_clause else 'N'}")
        for _rp in valid_refs:
            print(f"  ref: {os.path.basename(_rp)}")

        def _gen():
            try:
                paths = gemini_edit_image(
                    prompt=prompt,
                    reference_image_paths=valid_refs,
                    resolution="1K",
                    num_images=num_images,
                )
                # Copy to anchors dir. Per-user namespacing: authenticated
                # users get a u_<id>/ prefix so two users editing "Buddy"
                # don't collide. Local dev/bearer token = no namespace.
                _cu = self._current_user() or {}
                _uid = int(_cu.get("id", 0) or 0)
                if _uid > 0:
                    anchor_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6", f"u_{_uid}", shot_id)
                else:
                    anchor_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6", shot_id)
                os.makedirs(anchor_dir, exist_ok=True)
                saved = []
                for i, src in enumerate(paths):
                    if num_images > 1:
                        dest = os.path.join(anchor_dir, f"candidate_{i}.png")
                    else:
                        dest = os.path.join(anchor_dir, "selected.png")
                    import shutil
                    shutil.copy2(src, dest)
                    saved.append(dest)
                # Ledger: bill only on successful delivery
                _record_generation(
                    shot_key=shot_id, gen_type="image", engine="gemini",
                    tier="", duration=0, est_cost=est_cost, status="ok",
                    meta={"count": len(saved)},
                )
                # Per-user charge. Re-check balance inside the charge txn.
                if _uid > 0 and lumn_db:
                    _cents = max(1, int(round(est_cost * 100)))
                    lumn_db.charge_user(_uid, _cents, "anchor",
                                        {"shot_id": shot_id, "count": len(saved)})
                return saved
            except Exception as e:
                _record_generation(
                    shot_key=shot_id, gen_type="image", engine="gemini",
                    tier="", duration=0, est_cost=est_cost, status="error",
                    meta={"err": str(e)[:200]},
                )
                return {"error": str(e)}

        import threading
        result_holder = [None]
        def _thread():
            result_holder[0] = _gen()
        t = threading.Thread(target=_thread, daemon=True)
        t.start()
        # fal.ai gemini edit call can run up to ~3 min on num_images=3.
        # The fal client itself has a 600s submit timeout — we give the
        # server-thread 300s before surfacing a 504 so the UI / orchestrator
        # doesn't give up prematurely.
        t.join(timeout=300)

        result = result_holder[0]
        if isinstance(result, dict) and "error" in result:
            self._send_json(result, 500)
            return
        if not result:
            self._send_json({"error": "Generation timed out"}, 504)
            return

        # Auto-QA + auto-rank (#48, #49). Runs only if we have at least one
        # anchor AND the caller didn't opt out (skip_qa=true). Opus scores
        # identity match, prompt compliance, must_keep compliance, technical
        # quality; picks the winner and returns full scores.
        qa_report: dict | None = None
        selected_path = result[0] if result else None
        if result and not body.get("skip_qa", False):
            ref_paths = resolve_reference_paths(enriched["injected"], limit=2) if enriched["injected"] else []
            if ref_paths:
                qa_est = 0.02 + 0.012 * len(result)
                qa_ok, _, _ = _check_budget_gate(qa_est)
                if qa_ok:
                    try:
                        qa_report = self._run_anchor_qa(
                            shot_id=shot_id,
                            prompt=prompt,
                            candidate_paths=result,
                            ref_paths=ref_paths,
                            must_keep=enriched["must_keep"],
                            avoid=enriched["avoid"],
                            entity_names=[e.get("name") for e in enriched["injected"] if e.get("name")],
                        )
                        if qa_report and qa_report.get("pick_path"):
                            selected_path = qa_report["pick_path"]
                            _record_generation(
                                shot_key=shot_id, gen_type="image", engine="opus",
                                tier="", duration=0, est_cost=qa_est, status="ok",
                                meta={"qa": "anchor", "pick": qa_report.get("pick")},
                            )
                            # Identity gate auto-lock (#50) — single-subject
                            # anchors that clear the QA floor get locked as
                            # the canonical identity anchor for that character.
                            char_names = [
                                e.get("name") for e in enriched["injected"]
                                if e.get("type") == "character" and e.get("name")
                            ]
                            locked_now = maybe_auto_lock(
                                character_names=char_names,
                                anchor_path=selected_path,
                                shot_id=shot_id,
                                qa_report=qa_report,
                            )
                            if locked_now:
                                qa_report["identity_gate_locked"] = locked_now
                    except Exception as e:
                        qa_report = {"error": str(e)[:200]}

        self._send_json({
            "ok": True,
            "shot_id": shot_id,
            "paths": result,
            "count": len(result),
            "selected_path": selected_path,
            "enriched_prompt": prompt,
            "injection": {
                "injected": enriched["injected"],
                "must_keep": enriched["must_keep"],
                "avoid": enriched["avoid"],
                "report": enriched["report"],
            },
            "qa": qa_report,
        })

    def _run_anchor_qa(
        self,
        shot_id: str,
        prompt: str,
        candidate_paths: list,
        ref_paths: list,
        must_keep: list,
        avoid: list,
        entity_names: list,
    ) -> dict:
        """Opus vision QA over anchor candidates. Returns scores + pick.
        First image(s) in the call are reference sheets for identity; then
        candidates labeled A, B, C... in order."""
        from lib.claude_client import call_vision_json, OPUS_MODEL
        labels = [chr(ord("A") + i) for i in range(len(candidate_paths))]
        must_keep_str = "; ".join(must_keep) if must_keep else "(none)"
        avoid_str = "; ".join(avoid) if avoid else "(none)"
        names_str = ", ".join(entity_names) if entity_names else "subject"

        system = """You are a senior continuity supervisor doing QA on AI-generated anchor frames.
The first image(s) are the REFERENCE SHEETS for the subject(s) — the ground truth for identity.
The remaining images are CANDIDATES labeled A, B, C... in order.

Score each candidate 0-1 on:
  identity   — does the subject match the reference sheet? (the most important)
  prompt     — does it fulfill the shot description (framing, action, lighting)?
  must_keep  — does it preserve every required trait from the must_keep list?
  quality    — technical: sharpness, composition, natural lighting, no artifacts
  avoid      — 1.0 if NONE of the avoid items are present, 0.0 if any are

overall = weighted avg (identity 0.4, prompt 0.2, must_keep 0.2, quality 0.1, avoid 0.1)

Golden retrievers: pendant ears that hang, NEVER prick upright.
Humans: count fingers (5 per hand), watch for warped faces / extra limbs.

Return JSON ONLY:
{
  "candidates": {
    "A": {"identity": N, "prompt": N, "must_keep": N, "quality": N, "avoid": N, "overall": N, "notes": "..."},
    ...
  },
  "pick": "A",
  "pick_reason": "...",
  "confidence": N,
  "worth_regen": true|false
}"""
        user = f"""Shot: {shot_id}
Subjects: {names_str}
Prompt: {prompt[:400]}
Must keep: {must_keep_str}
Avoid: {avoid_str}
Candidates: {", ".join(labels)}
JSON only."""
        images = ref_paths + candidate_paths
        images = [p for p in images if os.path.isfile(p)]
        if not images:
            return {"error": "no valid images for QA"}
        result = call_vision_json(user, images, system=system, model=OPUS_MODEL, max_tokens=2000)
        # Map pick label back to path
        pick_label = (result or {}).get("pick", "").strip().upper()
        if pick_label and pick_label in labels:
            idx = labels.index(pick_label)
            if 0 <= idx < len(candidate_paths):
                result["pick_path"] = candidate_paths[idx]
        result["labels"] = labels
        return result

    def _handle_v6_clip_generate(self):
        """Generate video clip via Kling 3.0 image-to-video."""
        from lib.fal_client import kling_image_to_video
        from lib.v6_prompt_assembler import assemble_v6_prompt
        from lib.kling_prompt_linter import lint_kling_prompt
        from lib.identity_gate import check_gate
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        if lumn_validate:
            _ok, _err, _ = lumn_validate.validate(body, lumn_validate.CLIP_GENERATE_SCHEMA)
            if not _ok:
                self._send_json({"error": _err}, 400)
                return

        shot_id = body.get("shot_id", "unknown")
        # SECURITY (C3): confine anchor path to pipeline root.
        anchor_path = _safe_user_path(body.get("anchor_path", ""))
        if not anchor_path:
            self._send_json({"error": "anchor_path missing or outside pipeline root"}, 400)
            return
        raw_prompt = body.get("prompt", "")
        shot_context = body.get("shot_context") or {}

        # Global daily spend circuit breaker (see anchor handler).
        _cap = int(os.environ.get("LUMN_DAILY_CAP_CENTS", "0") or "0")
        if _cap > 0 and lumn_db:
            _spent = lumn_db.global_spend_since(86400)
            if _spent >= _cap:
                return self._send_json({
                    "error": "daily_spend_cap_reached",
                    "spent_cents": _spent, "cap_cents": _cap,
                }, 503)

        # V6 enrichment — anchor already carries identity, so we omit
        # description blocks to avoid re-describing subjects (Kling best-practice,
        # per feedback_kling_i2v_prompting.md) but still enforce must_keep +
        # avoid as continuity constraints.
        enriched = assemble_v6_prompt(
            raw_prompt=raw_prompt,
            shot_context=shot_context,
            include_description=False,
            max_chars=400,
        )
        prompt = enriched["enriched_prompt"]

        # Identity gate (#50) — if the clip features characters whose
        # identity is not yet locked, refuse (unless skip_identity_gate=true).
        # Rationale: unlocked characters drift across shots because Kling has
        # no canonical anchor to anchor to. The gate forces a medium/close
        # identity anchor to be generated + QA-locked first.
        if not body.get("skip_identity_gate", False):
            char_names_in_shot = [
                e.get("name") for e in enriched["injected"]
                if e.get("type") == "character" and e.get("name")
            ]
            if char_names_in_shot:
                gate = check_gate(char_names_in_shot)
                if not gate["all_locked"]:
                    self._send_json({
                        "error": "identity_gate_blocked",
                        "message": (
                            "Lock identity for these characters first "
                            "(generate medium/close anchor, QA-passed): "
                            + ", ".join(gate["unlocked"])
                        ),
                        "gate": gate,
                    }, 428)
                    return

        # Kling lint — block hard errors by default; caller can pass
        # skip_lint=true to bypass for experimentation.
        if not body.get("skip_lint", False):
            lint_result = lint_kling_prompt(prompt)
            if not lint_result["ok"]:
                self._send_json({
                    "error": "prompt_lint_failed",
                    "lint": lint_result,
                    "enriched_prompt": prompt,
                }, 422)
                return

        if body.get("duration") is not None:
            duration = body.get("duration")
            _dur_src = "manual"
        else:
            _scene_dur, _scene_src = _scene_duration_for_shot(body.get("shot_id", ""))
            if _scene_dur is not None and 3 <= _scene_dur <= 15:
                duration = _scene_dur
                _dur_src = _scene_src
            else:
                duration = 5
                _dur_src = "default"
        print(f"[KLING sync] shot={body.get('shot_id','?')} dur={duration}s src={_dur_src}")
        tier = body.get("tier", "v3_standard")
        cfg_scale = body.get("cfg_scale", 0.6)
        num_candidates = max(1, min(3, int(body.get("num_candidates", 1))))
        negative_prompt = body.get("negative_prompt",
            "blur, distortion, extra limbs, extra legs, face warping, morphing, "
            "texture swimming, jitter, flicker, deformation, watermark, text, low quality")
        generate_audio = body.get("generate_audio", True)
        # SECURITY (C3): confine end_image_path to pipeline root.
        end_image_raw = body.get("end_image_path", None)
        end_image_path = _safe_user_path(end_image_raw) if end_image_raw else None

        # Elements for character consistency — every path goes through _safe_user_path.
        elements = None
        elem_data = body.get("elements", [])
        if elem_data:
            elements = []
            for e in elem_data:
                frontal = _safe_user_path(e.get("frontal_image_path", ""))
                refs_raw = e.get("reference_image_paths", []) or []
                refs = []
                for p in refs_raw:
                    safe = _safe_user_path(p) if isinstance(p, str) else None
                    if safe:
                        refs.append(safe)
                if frontal:
                    elements.append({"frontal_image_path": frontal, "reference_image_paths": refs})
        if not prompt:
            self._send_json({"error": "prompt required"}, 400)
            return

        _cu = self._current_user() or {}
        _uid = int(_cu.get("id", 0) or 0)
        if _uid > 0:
            clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6", f"u_{_uid}")
        else:
            clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        os.makedirs(clips_dir, exist_ok=True)

        # Content moderation pre-filter — strict (keyword + Opus borderline).
        # SECURITY (H2): use moderate_prompt_strict not moderate_prompt.
        # SECURITY (H7): fail closed on exception — never skip moderation.
        try:
            from lib.moderation import moderate_prompt_strict
            _mod = moderate_prompt_strict(prompt, nsfw_allowed=False)
            if not _mod["allowed"]:
                return self._send_json({
                    "error": "moderation_blocked",
                    "severity": _mod["severity"],
                    "reasons": _mod["reasons"],
                }, 451)
            if _mod["severity"] == "warn":
                prompt = _mod["redacted_prompt"]
        except Exception as _e:
            return self._send_json({"error": "moderation unavailable"}, 500)

        # --- Real cost estimate + hard budget gate (P0-1, P0-2) ---
        est_cost = _price_for("kling", tier, duration, "video") * num_candidates
        ok, reason, _tracker = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        # Per-user credits + rate limit for clip gen.
        if _uid > 0 and lumn_db:
            _cost_cents = max(1, int(round(est_cost * 100)))
            _bal = int(_cu.get("credits_cents", 0) or 0)
            if not _user_credits_deferred() and _bal < _cost_cents:
                self._send_json({
                    "error": "insufficient_credits",
                    "balance_cents": _bal, "need_cents": _cost_cents,
                }, 402)
                return
            allowed, n = lumn_db.rate_limit_check(_uid, "clip", max_per_hour=20)
            if not allowed:
                self._send_json({
                    "error": "rate_limited", "kind": "clip",
                    "count_in_window": n, "max_per_hour": 20,
                }, 429)
                return

        def _gen_thread():
            try:
                clip_path = kling_image_to_video(
                    start_image_path=anchor_path,
                    prompt=prompt,
                    duration=duration,
                    tier=tier,
                    end_image_path=end_image_path if end_image_path and os.path.isfile(end_image_path) else None,
                    elements=elements,
                    negative_prompt=negative_prompt,
                    cfg_scale=cfg_scale,
                    generate_audio=generate_audio,
                )
                if clip_path and os.path.isfile(clip_path):
                    import shutil
                    # Versioned filename: shot_id_v{N}.mp4. The "current" is a copy at shot_id.mp4.
                    versions_dir = os.path.join(clips_dir, "_versions", shot_id)
                    os.makedirs(versions_dir, exist_ok=True)
                    existing = [f for f in os.listdir(versions_dir) if f.startswith("v") and f.endswith(".mp4")]
                    next_n = len(existing) + 1
                    versioned = os.path.join(versions_dir, f"v{next_n}.mp4")
                    shutil.copy2(clip_path, versioned)
                    # Write sidecar meta for this version
                    meta = {
                        "version": next_n,
                        "shot_id": shot_id,
                        "tier": tier,
                        "duration": duration,
                        "prompt": prompt,
                        "cfg_scale": cfg_scale,
                        "ts": time.time(),
                        "cost_est": round({"v3_standard":0.084,"v3_pro":0.112,"o3_standard":0.084,"o3_pro":0.392}.get(tier, 0.084) * duration, 3),
                    }
                    with open(os.path.join(versions_dir, f"v{next_n}.json"), "w", encoding="utf-8") as mf:
                        json.dump(meta, mf, indent=2)
                    # "Current" pointer
                    dest = os.path.join(clips_dir, f"{shot_id}.mp4")
                    shutil.copy2(versioned, dest)
                    # Real ledger record — bills only on successful delivery
                    _record_generation(
                        shot_key=shot_id,
                        gen_type="video",
                        engine="kling",
                        tier=tier,
                        duration=duration,
                        est_cost=est_cost,
                        status="ok",
                        meta={"version": next_n},
                    )
                    # Per-user charge on successful delivery.
                    if _uid > 0 and lumn_db:
                        _cents = max(1, int(round(est_cost * 100)))
                        lumn_db.charge_user(_uid, _cents, "clip",
                                            {"shot_id": shot_id, "tier": tier,
                                             "duration": duration})
                    return {"ok": True, "shot_id": shot_id, "path": dest, "version": next_n}
                # No asset returned — record as failed, no bill
                _record_generation(
                    shot_key=shot_id, gen_type="video", engine="kling",
                    tier=tier, duration=duration, est_cost=est_cost, status="failed",
                )
                return {"error": "No video returned"}
            except Exception as e:
                _record_generation(
                    shot_key=shot_id, gen_type="video", engine="kling",
                    tier=tier, duration=duration, est_cost=est_cost, status="error",
                    meta={"err": str(e)[:200]},
                )
                return {"error": str(e)}

        import threading
        # P2-12: Fire N parallel candidate generations. Each one saves as a new
        # version (v1, v2, v3) via the existing versioning logic inside _gen_thread.
        for _i in range(num_candidates):
            t = threading.Thread(target=_gen_thread, daemon=True)
            t.start()

        # Return immediately — client polls for completion
        self._send_json({
            "ok": True,
            "message": f"Started {num_candidates} candidate(s)",
            "shot_id": shot_id,
            "tier": tier,
            "duration": duration,
            "num_candidates": num_candidates,
            "est_total": est_cost,
            "enriched_prompt": prompt,
            "injection": {
                "injected": enriched["injected"],
                "must_keep": enriched["must_keep"],
                "avoid": enriched["avoid"],
                "report": enriched["report"],
            },
        })

    def _handle_v6_sonnet_select(self):
        """Opus vision picks best candidate per shot. (Route name kept for
        UI backwards-compat; model is claude-opus-4-7.)"""
        from lib.claude_client import call_vision_json, OPUS_MODEL
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        shot_id = body.get("shot_id", "")
        candidate_paths = body.get("candidate_paths", [])
        ref_sheet = body.get("ref_sheet", "")
        shot_info = body.get("shot_info", {})

        if not candidate_paths or len(candidate_paths) < 2:
            self._send_json({"error": "Need at least 2 candidates"}, 400)
            return

        # Budget gate — Opus vision estimate: ~$0.10-0.15 per call
        # (ref sheet + up to 3 candidates + ~2k output at $15/$75 per M tokens).
        est_cost = 0.03 + 0.03 * len(candidate_paths)
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        system = """You are a senior cinematographer selecting the best anchor frame from candidates.
Image 1 = character reference sheet. Remaining images = candidates A, B, C.
Evaluate: identity match, prompt compliance, technical quality, emotional read, continuity fitness (0-1 each).
Golden retrievers have pendant ears that hang, never prick upright.
JSON only: {"candidates": {"A": {"overall": N, "notes": "..."}, ...}, "pick": "A|B|C", "pick_reason": "...", "confidence": N}"""

        prompt_text = shot_info.get("prompt", "")
        user_prompt = f"""Shot {shot_id}: {shot_info.get('title', '')}
Prompt: {prompt_text[:300]}
Pick the best candidate. JSON only."""

        raw_images = [ref_sheet] + candidate_paths
        valid_images = []
        for p in raw_images:
            if not isinstance(p, str) or not p:
                continue
            safe = _safe_user_path(p)
            if safe and os.path.isfile(safe):
                valid_images.append(safe)
        if not valid_images:
            self._send_json({"error": "no valid image paths"}, 400)
            return

        try:
            result = call_vision_json(user_prompt, valid_images, system=system, model=OPUS_MODEL, max_tokens=2000)
            result["shot_id"] = shot_id
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_staleness_check(self):
        """Detect shots where referenced assets (character/environment sheets) have been
        regenerated *after* the shot's anchor was rendered. Those shots need a re-gen
        because their anchor no longer reflects the latest reference.
        """
        anchors_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6")
        clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        # Newest reference mtime across all preproduction packages + uploaded refs
        newest_ref_mtime = 0.0
        newest_ref_name = ""
        try:
            store = self._get_preprod_store()
            for pkg in store.get_all():
                for img in pkg.get("sheet_images", []):
                    p = img.get("image_path")
                    if p and os.path.isfile(p):
                        m = os.path.getmtime(p)
                        if m > newest_ref_mtime:
                            newest_ref_mtime = m
                            newest_ref_name = pkg.get("name", "") + "/" + img.get("view", "")
        except Exception:
            pass

        refs_dir = os.path.join(OUTPUT_DIR, "pipeline", "references_v6")
        if os.path.isdir(refs_dir):
            for ref_type in os.listdir(refs_dir):
                type_dir = os.path.join(refs_dir, ref_type)
                if os.path.isdir(type_dir):
                    for f in os.listdir(type_dir):
                        fp = os.path.join(type_dir, f)
                        if os.path.isfile(fp):
                            m = os.path.getmtime(fp)
                            if m > newest_ref_mtime:
                                newest_ref_mtime = m
                                newest_ref_name = f"{ref_type}/{f}"

        stale_anchors = []
        stale_clips = []
        if os.path.isdir(anchors_dir):
            for f in os.listdir(anchors_dir):
                if f.endswith(('.png', '.jpg')):
                    fp = os.path.join(anchors_dir, f)
                    if os.path.getmtime(fp) < newest_ref_mtime:
                        stale_anchors.append(f)
        if os.path.isdir(clips_dir):
            for f in os.listdir(clips_dir):
                if f.endswith('.mp4'):
                    fp = os.path.join(clips_dir, f)
                    if os.path.getmtime(fp) < newest_ref_mtime:
                        stale_clips.append(f)

        self._send_json({
            "newest_ref_mtime": newest_ref_mtime,
            "newest_ref_name": newest_ref_name,
            "stale_anchors": sorted(stale_anchors),
            "stale_clips": sorted(stale_clips),
            "stale_count": len(stale_anchors) + len(stale_clips),
        })

    def _handle_v6_timeline_preview(self):
        """Assemble current project into a timeline with placeholders for missing clips.

        Returns a list of tracks: each entry has shot_id, start, duration, and either
        a real clip URL or a 'placeholder' flag. The UI uses this to render a full-length
        preview even before every shot has been generated.
        """
        project_path = os.path.join(OUTPUT_DIR, "pipeline", "project.json")
        project = {}
        if os.path.isfile(project_path):
            try:
                with open(project_path, "r", encoding="utf-8") as f:
                    project = json.load(f)
            except (json.JSONDecodeError, IOError):
                project = {}
        shots = project.get("shots", [])
        clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        anchors_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6")

        track = []
        cursor = 0.0
        total_generated = 0
        total_stubs = 0
        for shot in shots:
            sid = shot.get("shot_id", "")
            dur = float(shot.get("duration", 5))
            clip_path = os.path.join(clips_dir, f"{sid}.mp4")
            has_clip = os.path.isfile(clip_path)
            # Placeholder falls back to the anchor still if the clip isn't ready yet
            anchor_candidates = []
            if os.path.isdir(anchors_dir):
                anchor_candidates = [f for f in os.listdir(anchors_dir)
                                     if f.startswith(sid) and f.endswith(('.png', '.jpg'))]
            entry = {
                "shot_id": sid,
                "title": shot.get("title", sid),
                "beat_name": shot.get("beat_name", ""),
                "start": round(cursor, 3),
                "duration": dur,
                "tone": shot.get("tone", ""),
            }
            if has_clip:
                entry["clip_url"] = f"/api/v6/clip-video/{sid}.mp4"
                entry["status"] = "ready"
                total_generated += 1
            elif anchor_candidates:
                entry["placeholder"] = True
                entry["poster_url"] = f"/api/v6/anchor-image/{anchor_candidates[0]}"
                entry["status"] = "anchor_only"
                total_stubs += 1
            else:
                entry["placeholder"] = True
                entry["status"] = "stub"
                total_stubs += 1
            track.append(entry)
            cursor += dur

        self._send_json({
            "track": track,
            "total_duration_sec": round(cursor, 3),
            "shot_count": len(track),
            "generated": total_generated,
            "stubs": total_stubs,
            "template": project.get("template"),
        })

    def _check_motion_audit_gate(self, body: dict, stitch_sids: list) -> tuple | None:
        """Shared motion-audit precondition for stitch endpoints.

        Reads `output/pipeline/audits/motion_audit_latest.json` and refuses
        to let a stitch proceed if any shot in `stitch_sids` has severity=fail.
        Returns None to proceed, or `(payload, status)` for _send_json on block.

        Body flags:
          bypass_motion_audit: skip the check (use sparingly)
          require_audit:       require an audit file to exist at all
        """
        if bool(body.get("bypass_motion_audit")):
            return None
        latest_audit = os.path.join(
            OUTPUT_DIR, "pipeline", "audits", "motion_audit_latest.json"
        )
        require_audit = bool(body.get("require_audit"))
        if os.path.isfile(latest_audit):
            try:
                with open(latest_audit, "r", encoding="utf-8") as f:
                    audit_data = json.load(f)
                fail_ids = set(audit_data.get("fail_ids") or [])
                audit_age_s = time.time() - os.path.getmtime(latest_audit)
                sids = set(stitch_sids)
                blocked = fail_ids & sids
                if blocked:
                    return ({
                        "error": "motion_audit_blocked",
                        "failed_shots": sorted(blocked),
                        "audit_age_hours": round(audit_age_s / 3600.0, 2),
                        "latest_audit": latest_audit,
                        "hint": ("Re-render failing shots on Kling with "
                                 "anti-orbit prompts, re-run /api/v6/clips/"
                                 "motion-audit with persist=true, then retry. "
                                 "To override, POST {'bypass_motion_audit': true}."),
                    }, 409)
            except (json.JSONDecodeError, IOError, OSError):
                if require_audit:
                    return ({
                        "error": "motion_audit_unreadable",
                        "latest_audit": latest_audit,
                    }, 409)
        elif require_audit:
            return ({
                "error": "motion_audit_missing",
                "hint": ("No motion_audit_latest.json on record. Run "
                         "/api/v6/clips/motion-audit with persist=true "
                         "before stitching, or drop require_audit."),
            }, 409)
        return None

    def _handle_v6_stitch(self):
        """Concat all v6 clips (output/pipeline/clips_v6/<sid>/selected.mp4)
        in project.json shot order into output/pipeline/v6_final.mp4.

        Body (optional):
          {
            "audio_path": "...mp3",        # optional audio track
            "transitions": ["cut", ...],   # per-cut; default "cut"
            "include_shots": ["sid", ...], # optional whitelist (skip others)
            "output_name": "final.mp4"     # default "v6_final.mp4"
          }
        """
        try:
            body = json.loads(self._read_body() or "{}")
        except (json.JSONDecodeError, ValueError):
            body = {}

        project_path = os.path.join(OUTPUT_DIR, "pipeline", "project.json")
        if not os.path.isfile(project_path):
            return self._send_json({"error": "no v6 project"}, 400)
        try:
            with open(project_path, "r", encoding="utf-8") as f:
                project = json.load(f)
        except (json.JSONDecodeError, IOError):
            return self._send_json({"error": "project.json unreadable"}, 500)

        shots = project.get("shots", []) or []
        whitelist = set(body.get("include_shots") or [])
        only_signed_off = bool(body.get("only_signed_off"))
        clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")

        # If only_signed_off, intersect the whitelist with signed-off shots.
        # The caller gets back `missing` for any non-signed-off shot so they
        # know what's gating the stitch.
        if only_signed_off:
            try:
                from lib.shot_gates import signed_off_shot_ids
                project_slug = (body.get("project") or "default").strip() or "default"
                proj_dir = os.path.join(OUTPUT_DIR, "projects", project_slug)
                signed = set(signed_off_shot_ids(proj_dir))
                if whitelist:
                    whitelist = whitelist & signed
                else:
                    whitelist = signed
                if not whitelist:
                    return self._send_json({
                        "error": "no_signed_off_shots",
                        "hint": "Sign off at least one shot before stitching with only_signed_off=true",
                    }, 400)
            except Exception:
                pass

        ordered, missing = [], []
        for shot in shots:
            sid = shot.get("shot_id", "")
            if whitelist and sid not in whitelist:
                continue
            candidate = os.path.join(clips_dir, sid, "selected.mp4")
            if os.path.isfile(candidate):
                ordered.append((sid, candidate))
            else:
                missing.append(sid)

        if not ordered:
            return self._send_json(
                {"error": "no v6 clips found", "missing": missing}, 400
            )

        err = self._check_motion_audit_gate(body, [sid for sid, _ in ordered])
        if err:
            return self._send_json(*err)

        transitions = body.get("transitions")
        if not isinstance(transitions, list) or len(transitions) != len(ordered):
            transitions = ["cut"] * len(ordered)

        audio_path = body.get("audio_path")
        if audio_path and not os.path.isfile(audio_path):
            audio_path = None

        out_name = body.get("output_name") or "v6_final.mp4"
        if "/" in out_name or "\\" in out_name or ".." in out_name:
            return self._send_json({"error": "bad output_name"}, 400)
        output_path = os.path.join(OUTPUT_DIR, "pipeline", out_name)

        from lib.video_stitcher import stitch as _stitch
        try:
            _stitch(
                [p for _, p in ordered],
                audio_path,
                output_path,
                transitions=transitions,
            )
        except Exception as e:
            return self._send_json(
                {"error": f"stitch_failed: {e.__class__.__name__}: {e}"}, 500
            )

        size = os.path.getsize(output_path) if os.path.isfile(output_path) else 0
        self._send_json({
            "ok": True,
            "output_path": output_path,
            "output_url": f"/api/v6/final-video/{out_name}",
            "size_bytes": size,
            "clip_count": len(ordered),
            "shot_ids": [sid for sid, _ in ordered],
            "missing": missing,
        })

    def _build_beat_snap_clips(self, body):
        """Shared loader for beat-snap endpoints.

        Returns (clips_list, downbeats, error_tuple) where clips_list is in
        project shot order with each entry = {shot_id, source, duration}.
        On failure returns (None, None, (error_dict, status)).
        """
        project_path = os.path.join(OUTPUT_DIR, "pipeline", "project.json")
        if not os.path.isfile(project_path):
            return None, None, ({"error": "no v6 project"}, 400)
        try:
            with open(project_path, "r", encoding="utf-8") as f:
                project = json.load(f)
        except (json.JSONDecodeError, IOError):
            return None, None, ({"error": "project.json unreadable"}, 500)

        grid_path = body.get("music_grid_path") or os.path.join(
            OUTPUT_DIR, "pipeline", "music_grid.json"
        )
        if not os.path.isfile(grid_path):
            return None, None, ({"error": "music_grid not found", "path": grid_path,
                                   "hint": "run song analyze first"}, 400)

        from lib.beat_snap import load_grid, _probe_duration
        try:
            downbeats = load_grid(grid_path)
        except Exception as e:
            return None, None, ({"error": f"grid unreadable: {e}"}, 500)
        if not downbeats:
            return None, None, ({"error": "music_grid has no downbeats"}, 400)

        whitelist = set(body.get("include_shots") or [])
        duration_overrides = body.get("duration_overrides") or {}
        clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        clips = []
        missing = []
        for shot in (project.get("shots") or []):
            sid = shot.get("shot_id", "")
            if whitelist and sid not in whitelist:
                continue
            src = os.path.join(clips_dir, sid, "selected.mp4")
            if not os.path.isfile(src):
                missing.append(sid)
                continue
            # Duration source precedence:
            #   1. body.duration_overrides[sid] — explicit caller override
            #   2. probed mp4 duration         — ground truth, what actually exists
            #   3. project.json dur/duration   — user intent fallback
            #   4. 5.0                         — last resort
            # Note: project.json `dur` is often a template default (4-5s), not intent —
            # using it as primary caused the TB-v7 beat-snap truncation (278s → 134s).
            override = duration_overrides.get(sid)
            if override is not None:
                duration_s = float(override)
            else:
                probed = _probe_duration(src)
                if probed > 0:
                    duration_s = probed
                else:
                    intent = shot.get("duration") or shot.get("dur") or 5.0
                    duration_s = float(intent)
            clips.append({
                "shot_id": sid,
                "source": src,
                "duration": duration_s,
            })

        if not clips:
            return None, None, ({"error": "no clips available", "missing": missing}, 400)

        return clips, downbeats, None

    def _handle_v6_beat_plan(self):
        """Compute a downbeat-snap plan without side effects.

        Body:
          tolerance_s: float (default 2.0)
          fps: int (default 24)
          include_shots: [shot_id, ...] optional whitelist
          music_grid_path: optional explicit path (defaults to project grid)

        Returns the plan dict from beat_snap.plan_beat_snap plus a preview list.
        """
        try:
            body = json.loads(self._read_body() or "{}")
        except (json.JSONDecodeError, ValueError):
            body = {}

        clips, downbeats, err = self._build_beat_snap_clips(body)
        if err:
            return self._send_json(*err)

        from lib.beat_snap import plan_beat_snap
        plan = plan_beat_snap(
            clips,
            downbeats,
            tolerance_s=float(body.get("tolerance_s") or 2.0),
            fps=int(body.get("fps") or 24),
        )
        self._send_json({"ok": True, **plan})

    def _handle_v6_beat_snap(self):
        """Compute the plan, trim each clip to its snapped duration, and
        concatenate into a single output via the existing video_stitcher.

        Body (all optional):
          tolerance_s: float   (default 2.0)
          fps: int             (default 24)
          include_shots: list  (whitelist)
          music_grid_path: str
          audio_path: str      (audio track to mix over the result)
          output_name: str     (default 'v6_beat_snapped.mp4')
          out_dir_name: str    (default 'clips_v6_snapped')
          preview_only: bool   (when true, skip stitch, return trimmed paths only)
        """
        try:
            body = json.loads(self._read_body() or "{}")
        except (json.JSONDecodeError, ValueError):
            body = {}

        clips, downbeats, err = self._build_beat_snap_clips(body)
        if err:
            return self._send_json(*err)

        # Motion-audit precondition — same gate as /api/v6/stitch. See
        # _handle_v6_stitch for rationale. Bypass with bypass_motion_audit=true.
        err = self._check_motion_audit_gate(body, [c["shot_id"] for c in clips])
        if err:
            return self._send_json(*err)

        from lib.beat_snap import plan_beat_snap, apply_beat_snap
        tolerance_s = float(body.get("tolerance_s") or 2.0)
        fps = int(body.get("fps") or 24)
        plan = plan_beat_snap(clips, downbeats, tolerance_s=tolerance_s, fps=fps)

        out_dir_name = body.get("out_dir_name") or "clips_v6_snapped"
        if "/" in out_dir_name or "\\" in out_dir_name or ".." in out_dir_name:
            return self._send_json({"error": "bad out_dir_name"}, 400)
        snap_dir = os.path.join(OUTPUT_DIR, "pipeline", out_dir_name)

        trim_result = apply_beat_snap(plan, snap_dir)
        if trim_result["errors"]:
            return self._send_json({
                "error": "trim_errors",
                "trim_errors": trim_result["errors"],
                "plan": plan,
            }, 500)

        if body.get("preview_only"):
            return self._send_json({
                "ok": True,
                "plan": plan,
                "trimmed": trim_result["clips"],
                "out_dir": snap_dir,
                "stitched": False,
            })

        out_name = body.get("output_name") or "v6_beat_snapped.mp4"
        if "/" in out_name or "\\" in out_name or ".." in out_name:
            return self._send_json({"error": "bad output_name"}, 400)
        output_path = os.path.join(OUTPUT_DIR, "pipeline", out_name)

        audio_path = body.get("audio_path")
        if audio_path and not os.path.isfile(audio_path):
            audio_path = None

        from lib.video_stitcher import stitch as _stitch
        try:
            _stitch(
                [c["output_path"] for c in trim_result["clips"]],
                audio_path,
                output_path,
                transitions=["cut"] * len(trim_result["clips"]),
            )
        except Exception as e:
            return self._send_json({
                "error": f"stitch_failed: {e.__class__.__name__}: {e}",
                "plan": plan,
                "trimmed": trim_result["clips"],
            }, 500)

        size = os.path.getsize(output_path) if os.path.isfile(output_path) else 0
        self._send_json({
            "ok": True,
            "output_path": output_path,
            "output_url": f"/api/v6/final-video/{out_name}",
            "size_bytes": size,
            "plan_summary": {
                "clip_count": plan["clip_count"],
                "original_total_s": plan["original_total_s"],
                "snapped_total_s": plan["snapped_total_s"],
                "delta_s": plan["delta_s"],
                "cuts_snapped": plan["cuts_snapped"],
            },
            "clips": trim_result["clips"],
            "snapped_dir": snap_dir,
            "stitched": True,
        })

    def _handle_v6_remotion_render(self):
        """F6 — spawn tools/render_mv.py in a background thread and return a job id.

        Body (all optional):
          mode: "proxy" | "full"           (default: "proxy")
          composition: str                 (default: "LifestreamStatic")
          out: str                         (override output path, relative to lumn-stitcher/)
          concurrency: int
          props: str                       (path to props JSON)

        Returns {job_id, status, log_path}.
        """
        import subprocess as _sp
        import threading
        import uuid

        try:
            body = json.loads(self._read_body() or "{}")
        except (json.JSONDecodeError, ValueError):
            body = {}

        mode = (body.get("mode") or "proxy").lower()
        if mode not in ("proxy", "full"):
            return self._send_json({"error": "mode must be proxy or full"}, 400)
        composition = body.get("composition") or "LifestreamStatic"
        out_override = body.get("out")
        concurrency = body.get("concurrency")
        props = body.get("props")

        repo_root = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(repo_root, "tools", "render_mv.py")
        if not os.path.isfile(script):
            return self._send_json({"error": "render_mv.py missing"}, 500)

        jobs_dir = os.path.join(OUTPUT_DIR, "pipeline", "render_jobs")
        os.makedirs(jobs_dir, exist_ok=True)
        job_id = uuid.uuid4().hex[:12]
        state_path = os.path.join(jobs_dir, f"{job_id}.json")
        log_path = os.path.join(jobs_dir, f"{job_id}.log")

        suffix = "_proxy" if mode == "proxy" else ""
        default_out = os.path.join("out", f"{composition}{suffix}.mp4")
        out_rel = out_override or default_out
        out_abs = out_rel if os.path.isabs(out_rel) else os.path.join(
            repo_root, "lumn-stitcher", out_rel
        )

        cmd = [sys.executable, "-u", script, "--composition", composition, "--out", out_rel]
        if mode == "proxy":
            cmd.append("--proxy")
        if concurrency:
            cmd += ["--concurrency", str(int(concurrency))]
        if props:
            cmd += ["--props", str(props)]

        started_at = time.time()
        state = {
            "job_id": job_id,
            "mode": mode,
            "composition": composition,
            "status": "running",
            "exit_code": None,
            "output_path": out_abs,
            "log_path": log_path,
            "started_at": started_at,
            "finished_at": None,
            "cmd": cmd,
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        def _worker():
            try:
                with open(log_path, "w", encoding="utf-8") as lf:
                    lf.write(f"[remotion_render] cmd={' '.join(cmd)}\n")
                    lf.flush()
                    proc = _sp.Popen(
                        cmd,
                        stdout=lf,
                        stderr=_sp.STDOUT,
                        cwd=repo_root,
                    )
                    rc = proc.wait()
                state["status"] = "completed" if rc == 0 else "failed"
                state["exit_code"] = rc
            except Exception as e:
                state["status"] = "failed"
                state["exit_code"] = -1
                try:
                    with open(log_path, "a", encoding="utf-8") as lf:
                        lf.write(f"[remotion_render] worker exception: {e}\n")
                except OSError:
                    pass
            finally:
                state["finished_at"] = time.time()
                try:
                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2)
                except OSError:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

        self._send_json({
            "ok": True,
            "job_id": job_id,
            "status": "running",
            "mode": mode,
            "composition": composition,
            "log_path": log_path,
            "output_path": out_abs,
        })

    def _handle_v6_remotion_render_status(self, job_id):
        """F6 — return current state + tail of log for a render job."""
        if not re.match(r"^[A-Za-z0-9_\-]+$", job_id):
            return self._send_json({"error": "bad job_id"}, 400)

        state_path = os.path.join(OUTPUT_DIR, "pipeline", "render_jobs", f"{job_id}.json")
        if not os.path.isfile(state_path):
            return self._send_json({"error": "job not found"}, 404)

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return self._send_json({"error": f"state read failed: {e}"}, 500)

        log_tail = []
        log_path = state.get("log_path")
        if log_path and os.path.isfile(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                log_tail = lines[-40:]
            except OSError:
                log_tail = []

        elapsed_s = None
        started = state.get("started_at")
        finished = state.get("finished_at") or time.time()
        if started:
            elapsed_s = round(finished - started, 1)

        output_path = state.get("output_path") or ""
        output_size = None
        output_exists = False
        if output_path and os.path.isfile(output_path):
            output_exists = True
            try:
                output_size = os.path.getsize(output_path)
            except OSError:
                pass

        self._send_json({
            "ok": True,
            "job_id": job_id,
            "status": state.get("status"),
            "exit_code": state.get("exit_code"),
            "mode": state.get("mode"),
            "composition": state.get("composition"),
            "output_path": output_path,
            "output_exists": output_exists,
            "output_size": output_size,
            "elapsed_s": elapsed_s,
            "log_tail": log_tail,
        })

    def _handle_v6_clips_drag_scan(self):
        """Scan rendered clips for frame-drag (frozen-motion) via phash sampling.

        Body (all optional):
            {"only_ids": ["id1","id2"], "signed_off_only": false}

        Response:
            {"ok": true, "total": N, "drag": M, "ok_count": N-M,
             "records": [{shot_id, is_drag, pair_similarities, ...}]}
        """
        try:
            body = json.loads(self._read_body() or b"{}")
        except (json.JSONDecodeError, ValueError):
            body = {}
        only_ids = set(body.get("only_ids") or [])
        signed_off_only = bool(body.get("signed_off_only"))

        try:
            from lib.drag_detector import scan_clip
        except ImportError as e:
            return self._send_json({"error": f"drag_detector unavailable: {e}"}, 500)

        clips_root = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        if not os.path.isdir(clips_root):
            return self._send_json({"error": "clips root not found", "path": clips_root}, 404)

        # Build candidate list — every selected.mp4 under clips_v6 (flat + u_*).
        import glob as _glob
        candidates: dict[str, str] = {}  # shot_id -> freshest mp4 path
        for fp in _glob.glob(os.path.join(clips_root, "*", "selected.mp4")):
            sid = os.path.basename(os.path.dirname(fp))
            if sid not in candidates or os.path.getmtime(fp) > os.path.getmtime(candidates[sid]):
                candidates[sid] = fp
        for fp in _glob.glob(os.path.join(clips_root, "u_*", "*", "selected.mp4")):
            sid = os.path.basename(os.path.dirname(fp))
            if sid not in candidates or os.path.getmtime(fp) > os.path.getmtime(candidates[sid]):
                candidates[sid] = fp

        # Optional filter by signed_off state.
        if signed_off_only:
            gates_path = os.path.join(OUTPUT_DIR, "projects", "default",
                                      "shots", "shot_gates.json")
            try:
                with open(gates_path, "r", encoding="utf-8") as f:
                    gates = json.load(f)
                signed = {sid for sid, g in (gates.get("shots") or {}).items()
                          if g.get("signed_off")}
                candidates = {sid: fp for sid, fp in candidates.items() if sid in signed}
            except (OSError, json.JSONDecodeError):
                pass

        if only_ids:
            candidates = {sid: fp for sid, fp in candidates.items() if sid in only_ids}

        t0 = time.time()
        records: list[dict] = []
        for sid, fp in sorted(candidates.items()):
            rec = scan_clip(fp)
            rec["shot_id"]   = sid
            rec["clip_path"] = fp
            records.append(rec)

        drag_count = sum(1 for r in records if r.get("is_drag"))
        self._send_json({
            "ok": True,
            "total": len(records),
            "drag": drag_count,
            "ok_count": len(records) - drag_count,
            "elapsed_s": round(time.time() - t0, 2),
            "records": records,
        })

    def _handle_v6_clips_cut_drift(self):
        """Report cut-to-downbeat drift for the current stitched MV (F9).

        Body (all optional):
            {"mv_path": "...", "grid_path": "...", "threshold_s": 0.2}

        Response:
            {"ok": true, "total_cuts": N, "off_grid_count": M,
             "off_grid_pct": X, "max_drift_s": D, "mean_drift_s": Dm,
             "threshold_s": T, "recommendation": "...",
             "cuts": [...], "off_grid_only": [...]}
        """
        try:
            body = json.loads(self._read_body() or b"{}")
        except (json.JSONDecodeError, ValueError):
            body = {}

        default_mv   = os.path.join(PROJECT_DIR, "lumn-stitcher", "src", "mv-data.json")
        default_grid = os.path.join(OUTPUT_DIR, "pipeline", "music_grid.json")
        mv_path      = body.get("mv_path")   or default_mv
        grid_path    = body.get("grid_path") or default_grid
        threshold_s  = float(body.get("threshold_s") or 0.2)

        if not os.path.isfile(mv_path):
            return self._send_json({"error": "mv-data not found", "path": mv_path}, 404)
        if not os.path.isfile(grid_path):
            return self._send_json({"error": "music_grid not found", "path": grid_path}, 404)

        try:
            from lib.cut_drift import analyze_mv, recommend
        except ImportError as e:
            return self._send_json({"error": f"cut_drift unavailable: {e}"}, 500)

        t0 = time.time()
        try:
            result = analyze_mv(mv_path, grid_path, threshold_s=threshold_s)
        except (OSError, ValueError, KeyError) as e:
            return self._send_json({"error": f"analyze_mv failed: {e}"}, 500)
        result["elapsed_s"]      = round(time.time() - t0, 3)
        result["recommendation"] = recommend(result)
        result["ok"]             = True
        self._send_json(result)

    def _handle_v6_clips_motion_audit(self):
        """Audit rendered clips for identity drift during Kling motion.

        Samples N frames per clip and asks Opus to verify the character's
        identity holds (eyes, emblem, pose). Catches the failure mode the
        anchor-stills auditor cannot see: Kling rotating the subject mid-clip
        and smearing the emblem to back of head.

        Body (all optional):
            {"only_ids": [...], "sample_count": 3, "persist": false,
             "spec": {...}  # per-project identity spec override
            }

        Response:
            {"ok": true, "total": N, "pass": A, "warn": B, "fail": C,
             "fail_ids": [...], "warn_ids": [...],
             "records": [...], "elapsed_s": X}
        """
        try:
            body = json.loads(self._read_body() or b"{}")
        except (json.JSONDecodeError, ValueError):
            body = {}
        only_ids = set(body.get("only_ids") or [])
        sample_count = int(body.get("sample_count") or 3)
        persist = bool(body.get("persist"))
        spec = body.get("spec")

        try:
            from lib.motion_audit import audit_clip, summarize
        except ImportError as e:
            return self._send_json({"error": f"motion_audit unavailable: {e}"}, 500)

        # Candidate collection: prefer stitcher-staged v7_* clips, fall back
        # to clips_v6/<shot>/selected.mp4.
        import glob as _glob
        candidates: dict[str, str] = {}
        stitcher_mv = os.path.join(PROJECT_DIR, "lumn-stitcher", "public", "mv")
        for fp in _glob.glob(os.path.join(stitcher_mv, "*.mp4")):
            base = os.path.basename(fp)
            if base.startswith("v7_"):
                sid = base.replace("v7_", "").split("_", 1)[0]
                if sid not in candidates or os.path.getmtime(fp) > os.path.getmtime(candidates[sid]):
                    candidates[sid] = fp
        clips_root = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        for fp in _glob.glob(os.path.join(clips_root, "*", "selected.mp4")):
            sid = os.path.basename(os.path.dirname(fp))
            if sid not in candidates:
                candidates[sid] = fp

        if only_ids:
            candidates = {sid: fp for sid, fp in candidates.items() if sid in only_ids}

        if not candidates:
            return self._send_json({"error": "no clips found", "stitcher_mv": stitcher_mv}, 404)

        frames_dir = os.path.join(OUTPUT_DIR, "pipeline", "audits", "motion_frames")
        t0 = time.time()
        records: list[dict] = []
        for sid, fp in sorted(candidates.items()):
            r = audit_clip(fp, spec=spec, sample_count=sample_count,
                           shot_id=sid, frames_dir=frames_dir)
            r["clip_path"] = fp
            records.append(r)

        summary = summarize(records)
        resp = {
            "ok": True,
            "elapsed_s": round(time.time() - t0, 2),
            **summary,
            "records": records,
        }

        if persist:
            audits_dir = os.path.join(OUTPUT_DIR, "pipeline", "audits")
            os.makedirs(audits_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(audits_dir, f"motion_audit_{ts}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(resp, f, indent=2)
            latest = os.path.join(audits_dir, "motion_audit_latest.json")
            with open(latest, "w", encoding="utf-8") as f:
                json.dump(resp, f, indent=2)
            resp["persisted_path"] = out_path
            resp["latest_path"]    = latest

        self._send_json(resp)

    def _handle_v6_pacing_arc(self):
        """Return pacing-arc recommendation for the current project's music grid (F5).

        Body (all optional):
            {"grid_path": "...", "style": "arc|steady|climax-heavy", "persist": false}

        Response:
            {"ok": true, "tempo_bpm": X, "bar_s": Y, "total_duration_s": D,
             "total_suggested_cuts": N, "curve_style": S,
             "sections": [...], "intensity_profile": [...],
             "recommendation": "...", "persisted_path": "..."?}
        """
        try:
            body = json.loads(self._read_body() or b"{}")
        except (json.JSONDecodeError, ValueError):
            body = {}

        default_grid = os.path.join(OUTPUT_DIR, "pipeline", "music_grid.json")
        grid_path    = body.get("grid_path") or default_grid
        style        = body.get("style") or "arc"
        persist      = bool(body.get("persist"))

        if not os.path.isfile(grid_path):
            return self._send_json({"error": "music_grid not found", "path": grid_path}, 404)

        try:
            from lib.pacing_arc import analyze_grid, recommend, CURVE_STYLES
        except ImportError as e:
            return self._send_json({"error": f"pacing_arc unavailable: {e}"}, 500)

        if style not in CURVE_STYLES:
            style = "arc"

        t0 = time.time()
        try:
            result = analyze_grid(grid_path, curve_style=style)
        except (OSError, ValueError, KeyError) as e:
            return self._send_json({"error": f"analyze_grid failed: {e}"}, 500)
        result["elapsed_s"]      = round(time.time() - t0, 3)
        result["recommendation"] = recommend(result)
        result["ok"]             = True

        if persist:
            persist_path = os.path.join(OUTPUT_DIR, "pipeline", "pacing_curve.json")
            try:
                with open(persist_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2)
                result["persisted_path"] = persist_path
            except OSError as e:
                result["persist_error"] = str(e)

        self._send_json(result)

    def _handle_v6_shots_plan_durations(self):
        """Dynamic shot duration planner — maps scenes.json signals to
        per-shot target duration in [3,15] (Kling V3 range).

        Body (all optional):
            {"project": "slug"?, "apply": false}

        Response:
            {"ok": true, "total_shots": N, "total_seconds": S,
             "plan": [{"scene_id","opus_shot_id","duration_s",
                       "rationale","factors"}],
             "applied": bool, "scenes_path": "..."?}
        """
        try:
            body = json.loads(self._read_body() or b"{}")
        except (json.JSONDecodeError, ValueError):
            body = {}
        apply_changes = bool(body.get("apply"))

        try:
            from lib.active_project import get_project_root
            scenes_path = os.path.join(get_project_root(), "prompt_os", "scenes.json")
        except Exception as e:
            return self._send_json({"error": f"active_project unavailable: {e}"}, 500)

        if not os.path.isfile(scenes_path):
            return self._send_json(
                {"error": "scenes.json not found", "path": scenes_path}, 404
            )
        try:
            with open(scenes_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return self._send_json({"error": f"scenes.json read failed: {e}"}, 500)

        if isinstance(raw, dict) and "scenes" in raw:
            scenes = raw["scenes"]
            wrapper = raw
        elif isinstance(raw, list):
            scenes = raw
            wrapper = None
        else:
            return self._send_json({"error": "scenes.json has unexpected shape"}, 500)

        try:
            from lib.shot_duration_planner import plan_scene_durations, apply_plan_to_scenes
        except ImportError as e:
            return self._send_json({"error": f"shot_duration_planner unavailable: {e}"}, 500)

        t0 = time.time()
        try:
            plan = plan_scene_durations(scenes)
        except (KeyError, ValueError, TypeError) as e:
            return self._send_json({"error": f"plan_scene_durations failed: {e}"}, 500)

        total_seconds = sum(int(p.get("duration_s", 0) or 0) for p in plan)

        applied = False
        if apply_changes:
            try:
                apply_plan_to_scenes(scenes, plan, write_fields=True)
                to_write = wrapper if wrapper is not None else scenes
                with open(scenes_path, "w", encoding="utf-8") as f:
                    json.dump(to_write, f, indent=2)
                applied = True
            except (OSError, TypeError) as e:
                return self._send_json(
                    {"error": f"write failed: {e}", "plan": plan}, 500
                )

        self._send_json({
            "ok":            True,
            "total_shots":   len(plan),
            "total_seconds": total_seconds,
            "plan":          plan,
            "applied":       applied,
            "scenes_path":   scenes_path if applied else None,
            "elapsed_s":     round(time.time() - t0, 3),
        })

    def _handle_v6_export_fcpxml(self):
        """Export current project as FCPXML 1.9 for DaVinci Resolve / Final Cut Pro.

        Improvements over prior version (P2-15):
        - Stable `uid` on each asset (MD5 of path) for relink
        - Relative paths (no file:// URL — editors look up relative to the xml)
        - Per-shot `<marker>` with shot_id / prompt / seed metadata
        - Optional EDL fallback when ?format=edl
        """
        import hashlib
        import xml.sax.saxutils as _sx

        query = ""
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
        use_edl = "format=edl" in query

        project_path = os.path.join(OUTPUT_DIR, "pipeline", "project.json")
        project = {}
        if os.path.isfile(project_path):
            try:
                with open(project_path, "r", encoding="utf-8") as f:
                    project = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        shots = project.get("shots", [])
        clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        title = (project.get("template") or {}).get("name", "LUMN Project")

        # EDL fallback — simple CMX 3600 emission
        if use_edl:
            edl_lines = ["TITLE: " + title, "FCM: NON-DROP FRAME"]
            offset = 0.0
            idx = 1
            for shot in shots:
                sid = shot.get("shot_id", "")
                dur = float(shot.get("duration", 5))
                def _tc(s):
                    h = int(s // 3600); m = int((s % 3600) // 60); sec = int(s % 60); fr = int(round((s - int(s)) * 30))
                    return f"{h:02d}:{m:02d}:{sec:02d}:{fr:02d}"
                edl_lines.append(f"{idx:03d}  AX       V     C        00:00:00:00 {_tc(dur)} {_tc(offset)} {_tc(offset + dur)}")
                edl_lines.append(f"* FROM CLIP NAME: {sid}")
                offset += dur
                idx += 1
            body = ("\n".join(edl_lines) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="lumn_project.edl"')
            self.end_headers()
            self.wfile.write(body)
            return

        # Build FCPXML (timebase 30000/1001 = 29.97 NDF)
        fps_num, fps_den = 30000, 1001
        frame_duration = f"{fps_den}/{fps_num}s"

        def _sec_to_rational(sec):
            frames = int(round(sec * fps_num / fps_den))
            return f"{frames * fps_den}/{fps_num}s"

        def _xml_attr(v):
            return _sx.quoteattr(str(v))[1:-1]  # strip surrounding quotes for attribute bodies

        resources_xml = []
        spine_xml = []
        asset_id = 1
        offset_sec = 0.0
        for shot in shots:
            sid = shot.get("shot_id", "")
            dur = float(shot.get("duration", 5))
            prompt = shot.get("prompt", "") or ""
            seed = shot.get("seed", "")
            clip_path = os.path.join(clips_dir, f"{sid}.mp4")
            if os.path.isfile(clip_path):
                # Relative path — editor resolves from the FCPXML location
                try:
                    rel_path = os.path.relpath(clip_path, start=OUTPUT_DIR).replace("\\", "/")
                except ValueError:
                    rel_path = os.path.basename(clip_path)
                # Stable uid for relink across machines
                uid = hashlib.md5(rel_path.encode("utf-8")).hexdigest().upper()
                resources_xml.append(
                    f'<asset id="r{asset_id}" name="{_xml_attr(sid)}" uid="{uid}" '
                    f'src="{_xml_attr(rel_path)}" start="0s" duration="{_sec_to_rational(dur)}" '
                    f'hasVideo="1" format="r0" audioSources="0" />'
                )
                marker_xml = (
                    f'<marker start="0s" duration="{frame_duration}" value="{_xml_attr(sid)}" '
                    f'note="{_xml_attr((prompt[:180] + ("… seed=" + str(seed) if seed else "")) or sid)}" />'
                )
                spine_xml.append(
                    f'<asset-clip name="{_xml_attr(sid)}" ref="r{asset_id}" '
                    f'offset="{_sec_to_rational(offset_sec)}" '
                    f'duration="{_sec_to_rational(dur)}">'
                    + marker_xml +
                    '</asset-clip>'
                )
                asset_id += 1
            else:
                spine_xml.append(
                    f'<gap offset="{_sec_to_rational(offset_sec)}" '
                    f'duration="{_sec_to_rational(dur)}" name="{_xml_attr(sid)}_stub" />'
                )
            offset_sec += dur

        fcpxml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE fcpxml>\n'
            '<fcpxml version="1.9">\n'
            '  <resources>\n'
            f'    <format id="r0" name="FFVideoFormat1080p2997" frameDuration="{frame_duration}" width="1920" height="1080"/>\n'
            + "\n".join("    " + r for r in resources_xml) +
            '\n  </resources>\n'
            '  <library>\n'
            f'    <event name="{_xml_attr(title)}">\n'
            f'      <project name="{_xml_attr(title)}">\n'
            '        <sequence format="r0" tcStart="0s" tcFormat="NDF">\n'
            '          <spine>\n'
            + "\n".join("            " + s for s in spine_xml) +
            '\n          </spine>\n'
            '        </sequence>\n'
            '      </project>\n'
            '    </event>\n'
            '  </library>\n'
            '</fcpxml>\n'
        )
        body = fcpxml.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="lumn_project.fcpxml"')
        self.send_header("Access-Control-Allow-Origin", self._get_cors_origin())
        self.end_headers()
        self.wfile.write(body)

    def _handle_v6_project_autosave(self):
        """Receive a partial project state snapshot from the client and merge it into project.json.

        Client sends this on a debounced interval while editing. Keeps a rolling backup
        at project.json.bak.N (up to 5) so an errant save never loses work irrevocably.
        """
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        pipeline_dir = os.path.join(OUTPUT_DIR, "pipeline")
        os.makedirs(pipeline_dir, exist_ok=True)
        project_path = os.path.join(pipeline_dir, "project.json")

        # Load existing
        project = {}
        if os.path.isfile(project_path):
            try:
                with open(project_path, "r", encoding="utf-8") as f:
                    project = json.load(f)
            except (json.JSONDecodeError, IOError):
                project = {}
            # Rolling backup — shift .bak.N → .bak.N+1, drop oldest
            for i in range(4, 0, -1):
                old = project_path + f".bak.{i}"
                new = project_path + f".bak.{i+1}"
                if os.path.isfile(old):
                    try:
                        if os.path.isfile(new):
                            os.remove(new)
                        os.rename(old, new)
                    except OSError:
                        pass
            try:
                import shutil as _sh
                _sh.copy2(project_path, project_path + ".bak.1")
            except OSError:
                pass

        # Merge: shots are replaced wholesale, other top-level keys deep-merged
        for k, v in body.items():
            project[k] = v
        project["_last_autosave"] = time.time()

        with open(project_path, "w", encoding="utf-8") as f:
            json.dump(project, f, indent=2)
        self._send_json({"ok": True, "ts": project["_last_autosave"]})

    def _handle_template_apply(self):
        """Instantiate a project template into shots."""
        from lib.project_templates import get_template, instantiate_shots
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        template_id = body.get("template_id", "")
        tpl = get_template(template_id)
        if not tpl:
            self._send_json({"error": f"Unknown template: {template_id}"}, 400)
            return
        shots = instantiate_shots(template_id)
        # Persist as the current project's shot list
        project_path = os.path.join(OUTPUT_DIR, "pipeline", "project.json")
        os.makedirs(os.path.dirname(project_path), exist_ok=True)
        project = {}
        if os.path.isfile(project_path):
            try:
                with open(project_path, "r", encoding="utf-8") as f:
                    project = json.load(f)
            except (json.JSONDecodeError, IOError):
                project = {}
        project["template"] = {
            "id": tpl["id"],
            "name": tpl["name"],
            "applied_ts": time.time(),
        }
        project["shots"] = shots
        project["duration_target_sec"] = tpl.get("duration_target_sec")
        project["default_tier"] = tpl.get("default_tier")
        with open(project_path, "w", encoding="utf-8") as f:
            json.dump(project, f, indent=2)
        self._send_json({"ok": True, "template": tpl["name"], "shot_count": len(shots), "shots": shots})

    def _handle_v6_audio_beats(self):
        """Return enhanced beat info for the current project audio track.
        Feeds the editable waveform/beat-snap timeline (P2-16)."""
        try:
            from lib.audio_analyzer import analyze
            from lib import beat_sync
        except Exception as e:
            self._send_json({"error": f"audio libs unavailable: {e}"}, 500)
            return
        plan = _load_manual_plan()
        audio_path = plan.get("song_path", "")
        if not audio_path or not os.path.isfile(audio_path):
            self._send_json({"beats": [], "downbeats": [], "duration": 0, "bpm": 0, "has_audio": False})
            return
        try:
            analysis = analyze(audio_path)
            enhanced = beat_sync.analyze_audio_for_beats(analysis) if hasattr(beat_sync, "analyze_audio_for_beats") else {
                "beat_times": analysis.get("beats", []),
                "downbeats": [],
                "duration": analysis.get("duration", 0),
            }
            self._send_json({
                "has_audio": True,
                "duration": analysis.get("duration", 0),
                "bpm": analysis.get("bpm", 0),
                "beats": enhanced.get("beat_times", analysis.get("beats", [])),
                "downbeats": enhanced.get("downbeats", []),
                "bar_times": enhanced.get("bar_times", []),
                "sections": analysis.get("sections", []),
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # ------------------------------------------------------------------
    # Song timing (lyrics + beats + downbeats + sections)
    # ------------------------------------------------------------------

    def _project_dir_for(self, project_slug: str) -> str:
        return os.path.join(OUTPUT_DIR, "projects", project_slug or "default")

    def _resolve_song_path(self, body: dict, project_slug: str) -> str | None:
        """Pick a song path: explicit body.song_path → manual plan → None."""
        cand = (body.get("song_path") or "").strip()
        if cand and os.path.isfile(cand):
            return cand
        plan = _load_manual_plan() or {}
        plan_song = plan.get("song_path") or ""
        if plan_song and os.path.isfile(plan_song):
            return plan_song
        return None

    def _handle_v6_song_timing(self):
        """Return the cached timing.json for the project, or 404 if absent.

        Query: ?project=<slug>  (default "default")
        """
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        project_slug = (qs.get("project", ["default"])[0] or "default").strip()
        project_dir = self._project_dir_for(project_slug)
        try:
            from lib.song_timing import load_timing, project_timing_path
        except Exception as e:
            return self._send_json({"error": f"song_timing import failed: {e}"}, 500)
        timing = load_timing(project_dir)
        if not timing:
            return self._send_json({
                "ok": False,
                "status": "missing",
                "path": project_timing_path(project_dir),
                "hint": "POST /api/v6/song/analyze to create it",
            }, 404)
        self._send_json({"ok": True, "project": project_slug, **timing})

    def _handle_v6_song_analyze(self):
        """Run full song timing analysis and persist timing.json.

        Body:
          {
            project?: "default",
            song_path?: absolute path,     # falls back to manual plan song
            include_lyrics?: true,         # set false to skip fal Whisper
          }
        Returns a summary; full JSON is fetchable via GET /api/v6/song/timing.
        """
        try:
            body = json.loads(self._read_body() or "{}")
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        project_slug = (body.get("project") or "default").strip() or "default"
        include_lyrics = bool(body.get("include_lyrics", True))

        song_path = self._resolve_song_path(body, project_slug)
        if not song_path:
            return self._send_json({
                "error": "no_song",
                "reason": "body.song_path missing and manual plan has none",
            }, 400)

        # Whisper ~$0.006/min; 4 min track ≈ $0.024. Gate gently.
        ok, reason, _t = _check_budget_gate(0.05)
        if not ok and include_lyrics:
            return self._send_json({"error": "budget_exceeded", "reason": reason}, 402)

        try:
            from lib.song_timing import analyze_song, save_timing, project_timing_path
        except Exception as e:
            return self._send_json({"error": f"song_timing import failed: {e}"}, 500)

        try:
            timing = analyze_song(song_path, include_lyrics=include_lyrics)
        except Exception as e:
            return self._send_json({"error": f"analyze_failed: {e.__class__.__name__}: {e}"}, 500)

        project_dir = self._project_dir_for(project_slug)
        try:
            out_path = save_timing(project_dir, timing)
        except Exception as e:
            return self._send_json({"error": f"save_failed: {e}"}, 500)

        lyr = timing.get("lyrics") or {}
        self._send_json({
            "ok": True,
            "project": project_slug,
            "timing_path": out_path,
            "summary": {
                "duration": timing["source"]["duration"],
                "bpm": timing["tempo"]["bpm"],
                "beats": len(timing.get("beats", [])),
                "downbeats": len(timing.get("downbeats", [])),
                "bars": len(timing.get("bars", [])),
                "sections": [
                    {"index": s["index"], "label": s.get("label"),
                     "start": s["start"], "end": s["end"], "energy": s.get("energy")}
                    for s in timing.get("sections", [])
                ],
                "lyrics_engine": lyr.get("engine"),
                "lyrics_words": len(lyr.get("words", [])),
                "lyrics_lines": len(lyr.get("lines", [])),
            },
        })

    # ------------------------------------------------------------------
    # Shot gates — per-shot audit / motion-review / signoff state
    # ------------------------------------------------------------------

    def _load_v6_project_shots(self) -> list:
        project_path = os.path.join(OUTPUT_DIR, "pipeline", "project.json")
        if not os.path.isfile(project_path):
            return []
        try:
            with open(project_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("shots", []) or []
        except (json.JSONDecodeError, IOError):
            return []

    def _handle_v6_shot_gates_get(self):
        """GET ?project=<slug>&sync=1 — return shot_gates.json.

        With sync=1 (default), reconciles disk state + timing.json before
        returning so stale cached gates can't mislead the UI.
        """
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        project_slug = (qs.get("project", ["default"])[0] or "default").strip()
        do_sync = qs.get("sync", ["1"])[0] != "0"
        project_dir = self._project_dir_for(project_slug)

        try:
            from lib.shot_gates import (
                load_gates, sync_gates_with_disk, gate_summary,
            )
            from lib.song_timing import load_timing
        except Exception as e:
            return self._send_json({"error": f"shot_gates import failed: {e}"}, 500)

        shots = self._load_v6_project_shots()
        anchors_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6")
        clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")

        if do_sync:
            timing = load_timing(project_dir)
            state = sync_gates_with_disk(project_dir, shots, anchors_dir, clips_dir, timing)
        else:
            state = load_gates(project_dir)

        self._send_json({
            "ok": True,
            "project": project_slug,
            "shots": state.get("shots", {}),
            "summary": gate_summary(state),
            "updated_at": state.get("updated_at"),
        })

    def _handle_v6_shot_gates_sync(self):
        """POST {project} — force re-sync with disk + timing."""
        try:
            body = json.loads(self._read_body() or "{}")
        except (json.JSONDecodeError, ValueError):
            body = {}
        project_slug = (body.get("project") or "default").strip() or "default"
        project_dir = self._project_dir_for(project_slug)

        try:
            from lib.shot_gates import sync_gates_with_disk, gate_summary
            from lib.song_timing import load_timing
        except Exception as e:
            return self._send_json({"error": f"import failed: {e}"}, 500)

        shots = self._load_v6_project_shots()
        anchors_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6")
        clips_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6")
        timing = load_timing(project_dir)

        state = sync_gates_with_disk(project_dir, shots, anchors_dir, clips_dir, timing)
        self._send_json({
            "ok": True,
            "project": project_slug,
            "shots": state.get("shots", {}),
            "summary": gate_summary(state),
        })

    def _handle_v6_shot_gates_set(self):
        """POST {project, shot_id, gate, value, actor?} — flip a gate.

        gate ∈ {"motion_review_passed", "signed_off"} — audit_passed is set
        only by running the auditor through /api/v6/anchor/audit or the
        audit-all endpoint below.
        """
        try:
            body = json.loads(self._read_body() or "{}")
        except (json.JSONDecodeError, ValueError):
            return self._send_json({"error": "Invalid JSON"}, 400)

        project_slug = (body.get("project") or "default").strip() or "default"
        shot_id = (body.get("shot_id") or "").strip()
        gate = (body.get("gate") or "").strip()
        value = body.get("value")
        notes = (body.get("notes") or "").strip()
        actor = (body.get("actor") or "human").strip()

        if not shot_id:
            return self._send_json({"error": "shot_id required"}, 400)
        if gate not in ("motion_review_passed", "signed_off"):
            return self._send_json({"error": f"unsupported gate: {gate}"}, 400)

        project_dir = self._project_dir_for(project_slug)
        try:
            from lib.shot_gates import set_motion_review, set_signoff
        except Exception as e:
            return self._send_json({"error": f"import failed: {e}"}, 500)

        if gate == "motion_review_passed":
            shot = set_motion_review(project_dir, shot_id, bool(value), notes)
        else:
            shot = set_signoff(project_dir, shot_id, bool(value), actor)
        self._send_json({"ok": True, "shot": shot})

    def _handle_v6_shot_gates_audit_all(self):
        """POST {project, shot_ids?} — audit anchors and persist into gates.

        Calls lib.anchor_auditor.audit_scene_anchor for each scene with an
        anchor on disk, then writes pass/violations/summary into
        shot_gates.json so the UI can gate Kling buttons on it.

        Cost: ~$0.08/anchor via Opus vision; 25 shots ≈ $2.00.
        """
        try:
            body = json.loads(self._read_body() or "{}")
        except (json.JSONDecodeError, ValueError):
            body = {}

        project_slug = (body.get("project") or "default").strip() or "default"
        project_dir = self._project_dir_for(project_slug)
        whitelist = set(body.get("shot_ids") or [])
        character_rules = body.get("character_rules") or None

        try:
            from lib.anchor_auditor import audit_scene_anchor
            from lib.shot_gates import apply_audit_result, gate_summary, load_gates
        except Exception as e:
            return self._send_json({"error": f"import failed: {e}"}, 500)

        ok, reason, _t = _check_budget_gate(0.60)
        if not ok:
            return self._send_json({"error": "budget_exceeded", "reason": reason}, 402)

        scenes_path = os.path.join(
            OUTPUT_DIR, "projects", project_slug, "prompt_os", "scenes.json"
        )
        if not os.path.isfile(scenes_path):
            return self._send_json(
                {"error": f"scenes.json not found for project {project_slug}"}, 404,
            )
        try:
            with open(scenes_path, "r", encoding="utf-8") as f:
                scenes = json.load(f)
        except Exception as e:
            return self._send_json({"error": f"scenes.json read failed: {e}"}, 500)

        anchors_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6")
        results = []
        for scene in scenes:
            sid = scene.get("id") or ""
            if not sid:
                continue
            if whitelist and sid not in whitelist:
                continue
            anchor_path = os.path.join(anchors_dir, sid, "selected.png")
            if not os.path.isfile(anchor_path):
                results.append({"shot_id": sid, "skipped": "no_anchor"})
                continue
            try:
                verdict = audit_scene_anchor(scene, anchor_path, character_rules)
            except Exception as e:
                results.append({"shot_id": sid, "error": f"{e.__class__.__name__}: {e}"})
                continue
            apply_audit_result(project_dir, sid, verdict)
            vio_list = verdict.get("violations") or []
            results.append({
                "shot_id": sid,
                "pass": bool(verdict.get("pass")),
                "violations": len(vio_list),
                "violation_codes": [v.get("code") for v in vio_list if isinstance(v, dict)],
                "force_passed_codes": verdict.get("force_passed_codes") or [],
                "summary": (verdict.get("summary") or "")[:500],
            })

        state = load_gates(project_dir)
        self._send_json({
            "ok": True,
            "project": project_slug,
            "results": results,
            "summary": gate_summary(state),
        })

    def _handle_screenplay_parse(self):
        """Parse a Fountain/plain-text screenplay into shots."""
        from lib import screenplay_parser
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        text = body.get("text", "")
        if not text or not text.strip():
            self._send_json({"error": "text required"}, 400)
            return
        try:
            parsed = screenplay_parser.parse(text)
            parsed["shot_sheet_text"] = screenplay_parser.to_shot_sheet_text(parsed)
            self._send_json(parsed)
        except Exception as e:
            self._send_json({"error": f"parse failed: {e}"}, 500)

    def _handle_v6_clip_versions(self, shot_id):
        """List all saved versions for a shot."""
        versions_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6", "_versions", shot_id)
        result = []
        if os.path.isdir(versions_dir):
            for f in sorted(os.listdir(versions_dir)):
                if f.startswith("v") and f.endswith(".mp4"):
                    v_num = int(f[1:-4]) if f[1:-4].isdigit() else 0
                    meta_path = os.path.join(versions_dir, f"v{v_num}.json")
                    meta = {}
                    if os.path.isfile(meta_path):
                        try:
                            with open(meta_path, "r", encoding="utf-8") as mf:
                                meta = json.load(mf)
                        except (json.JSONDecodeError, IOError):
                            pass
                    result.append({
                        "version": v_num,
                        "url": f"/api/v6/clip-version-file/{shot_id}/v{v_num}.mp4",
                        "meta": meta,
                    })
        self._send_json({"shot_id": shot_id, "versions": result})

    def _handle_v6_sonnet_override(self):
        """Promote a candidate to selected.png. Works for both Opus's
        auto-pick (frontend always calls this on Accept) and manual override.

        `selected` may be a `/api/v6/anchor-image/...` URL or a filesystem
        path — we normalize either way, then copy to the same dir as the
        candidate under the name `selected.png`.
        """
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        shot_id = body.get("shot_id", "")
        selected = body.get("selected", "")
        if not shot_id or not selected:
            self._send_json({"error": "shot_id and selected required"}, 400)
            return

        # Normalize URL or path → absolute filesystem path under anchors_v6
        anchor_base = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6")
        rel = selected
        if rel.startswith("/api/v6/anchor-image/"):
            rel = rel[len("/api/v6/anchor-image/"):]
        if rel.startswith("output/pipeline/anchors_v6/"):
            rel = rel[len("output/pipeline/anchors_v6/"):]
        src_path = os.path.realpath(os.path.join(anchor_base, rel))
        anchor_root = os.path.realpath(anchor_base)
        if not src_path.startswith(anchor_root + os.sep):
            self._send_json({"error": "path outside anchor root"}, 400)
            return
        if not os.path.isfile(src_path):
            self._send_json({"error": f"file not found: {rel}"}, 404)
            return

        dest_path = os.path.join(os.path.dirname(src_path), "selected.png")
        try:
            import shutil
            if os.path.realpath(src_path) != os.path.realpath(dest_path):
                shutil.copy2(src_path, dest_path)
        except OSError as e:
            self._send_json({"error": f"copy failed: {e}"}, 500)
            return

        # Persist the override metadata alongside anchors
        overrides_path = os.path.join(anchor_base, "_overrides.json")
        os.makedirs(os.path.dirname(overrides_path), exist_ok=True)
        overrides = {}
        if os.path.isfile(overrides_path):
            try:
                with open(overrides_path, "r", encoding="utf-8") as f:
                    overrides = json.load(f)
            except (json.JSONDecodeError, IOError):
                overrides = {}
        overrides[shot_id] = {"selected": selected, "ts": time.time(), "source": "user_override"}
        with open(overrides_path, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)
        self._send_json({"ok": True, "shot_id": shot_id, "selected": selected,
                         "promoted_to": dest_path})

    def _handle_v6_sonnet_review(self):
        """Opus reviews transition between two anchors. (Route name kept for
        UI backwards-compat; model is claude-opus-4-7.)"""
        from lib.claude_client import call_vision_json, OPUS_MODEL
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        from_path = body.get("from_anchor", "")
        to_path = body.get("to_anchor", "")
        ref_sheet = body.get("ref_sheet", "")
        transition_type = body.get("transition_type", "hard_cut")
        from_info = body.get("from_info", {})
        to_info = body.get("to_info", {})

        # Budget gate (Opus vision, 3 images)
        est_cost = 0.15
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        system = """You are a senior VFX continuity supervisor reviewing anchor frame pairs.
TARGET: confidence >= 0.90, risk_score <= 0.10.
Image 1 = character ref, Image 2 = FROM shot, Image 3 = TO shot.
Score identity/pose/camera/scene/motion continuity 0-1.
JSON only: {"scores": {"overall_score": N, ...}, "risk_level": "low|medium|high", "confidence": N, "plain_english_summary": "..."}"""

        user_prompt = f"""Review: {from_info.get('shot_id','')} -> {to_info.get('shot_id','')} ({transition_type})
FROM: {from_info.get('title','')} | TO: {to_info.get('title','')}
JSON only."""

        images = [p for p in [ref_sheet, from_path, to_path] if os.path.isfile(p)]

        try:
            result = call_vision_json(user_prompt, images, system=system, model=OPUS_MODEL, max_tokens=2000)
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_get_references(self):
        """List V6 refs — merged from uploaded references_v6/ AND preproduction packages."""
        refs_dir = os.path.join(OUTPUT_DIR, "pipeline", "references_v6")
        result = {"character": [], "environment": [], "prop": [], "costume": []}

        # 1. Uploaded V6 refs (legacy, still supported)
        if os.path.isdir(refs_dir):
            for ref_type in result.keys():
                type_dir = os.path.join(refs_dir, ref_type)
                if os.path.isdir(type_dir):
                    for f in sorted(os.listdir(type_dir)):
                        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                            mtime = os.path.getmtime(os.path.join(type_dir, f))
                            result[ref_type].append({
                                "name": os.path.splitext(f)[0],
                                "filename": f,
                                "url": f"/api/v6/reference-image/{ref_type}/{f}",
                                "path": os.path.join(type_dir, f),
                                "source": "upload",
                                "mtime": mtime,
                            })

        # 2. Preproduction packages — hero image of each approved/generated package
        # PROJECT-SCOPED: only emit packages whose `project_slug` field matches
        # the active project. Legacy packages with no project_slug are skipped
        # (they pre-date the multi-project refactor and caused the 2026-04-20
        # Buddy/Owen/Maya cross-project leak into TB anchor generation).
        try:
            active_slug = active_project.get_active_slug() or "default"
        except Exception:
            active_slug = "default"
        try:
            store = self._get_preprod_store()
            project_root = os.path.realpath(PROJECT_DIR)
            skipped_unscoped = 0
            for pkg in store.get_all():
                ptype = pkg.get("package_type", "")
                if ptype not in result:
                    continue
                hero = pkg.get("hero_image_path")
                if not hero or not os.path.isfile(hero):
                    continue
                pkg_project = (pkg.get("project_slug") or pkg.get("project") or "").strip()
                if not pkg_project or pkg_project != active_slug:
                    skipped_unscoped += 1
                    continue
                # Build a URL relative to project root (served via /output/ static route)
                try:
                    rel = os.path.relpath(os.path.realpath(hero), project_root).replace("\\", "/")
                    url = "/" + rel
                except ValueError:
                    continue
                result[ptype].append({
                    "name": pkg.get("name", pkg.get("package_id", "")),
                    "filename": os.path.basename(hero),
                    "url": url,
                    "path": hero,
                    "source": "preproduction",
                    "package_id": pkg.get("package_id"),
                    "project_slug": pkg_project,
                    "hero_view": pkg.get("hero_view"),
                    "status": pkg.get("status", "generated"),
                    "mtime": os.path.getmtime(hero),
                    "sheet_count": len([s for s in pkg.get("sheet_images", []) if s.get("image_path")]),
                })
            if skipped_unscoped:
                print(f"[REFS] skipped {skipped_unscoped} unscoped preprod packages for project={active_slug}")
        except Exception as e:
            # Preprod merge is optional — don't break the V6 refs list if the store is missing
            pass

        self._send_json({"references": result})

    def _handle_v6_sonnet_audit_prompt(self):
        """Opus reviews assembled shot prompts and recommends improvements.
        (Route name kept for UI backwards-compat; model is claude-opus-4-7.)"""
        from lib.claude_client import call_json, OPUS_MODEL
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        anchor_prompt = body.get("anchor_prompt", "")
        video_prompt = body.get("video_prompt", "")
        shot_id = body.get("shot_id", "")
        shot_context = body.get("shot_context", {})

        # Budget gate (Opus text-only audit)
        est_cost = 0.08
        ok, reason, _t = _check_budget_gate(est_cost)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason, "est": est_cost}, 402)
            return

        system = """You are a senior cinematography prompt engineer for AI video generation (Kling 3.0 image-to-video).
Your job: review assembled prompts and recommend specific improvements for realism and continuity.

RULES FOR ANCHOR PROMPTS (still image generation via Gemini):
- Describe framing, composition, lighting. DO NOT describe the subject in detail — reference images carry identity.
- Aim for photorealistic cinema stills, not illustrations.

RULES FOR VIDEO PROMPTS (Kling 3.0 i2v, 15-40 words):
- Camera movement FIRST, then 1-2 subject actions max.
- No sound words. No scene re-description (anchor carries visual).
- Add environmental micro-motion (wind, leaves, light shifts).
- Observational documentary style, not directed/posed.

Return JSON: {
  "anchor_prompt_revised": "...",
  "video_prompt_revised": "...",
  "changes_made": ["list of what you changed and why"],
  "confidence": 0.0-1.0,
  "word_count_anchor": N,
  "word_count_video": N
}"""

        user_prompt = f"""Shot: {shot_id}
Context: {json.dumps(shot_context)}

ANCHOR PROMPT TO REVIEW:
{anchor_prompt}

VIDEO PROMPT TO REVIEW:
{video_prompt}

Revise both prompts following the rules. Keep video prompt 15-40 words."""

        try:
            result = call_json(user_prompt, system=system, model=OPUS_MODEL, max_tokens=1500)
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_v6_anchor_audit(self):
        """Vision-audit a single anchor PNG against character/emblem rules.

        Body: {shot_id: str, project?: str, character_rules?: dict,
               callout_path?: str}
        If callout_path is omitted, the handler tries to auto-locate an emblem
        callout PNG at output/prompt_os/previews/characters/<id>_callout*.png
        and passes it as the second image to the auditor.
        """
        from lib.anchor_auditor import audit_scene_anchor
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        shot_id = (body.get("shot_id") or "").strip()
        project_slug = (body.get("project") or "default").strip() or "default"
        character_rules = body.get("character_rules") or None
        callout_path = (body.get("callout_path") or "").strip() or None
        if not shot_id:
            self._send_json({"error": "shot_id required"}, 400)
            return

        ok, reason, _t = _check_budget_gate(0.02)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason}, 402)
            return

        scenes_path = os.path.join(
            OUTPUT_DIR, "projects", project_slug, "prompt_os", "scenes.json"
        )
        if not os.path.isfile(scenes_path):
            self._send_json({"error": f"scenes.json not found for project {project_slug}"}, 404)
            return
        try:
            with open(scenes_path, "r", encoding="utf-8") as f:
                scenes = json.load(f)
        except Exception as e:
            self._send_json({"error": f"scenes.json read failed: {e}"}, 500)
            return
        scene = next((s for s in scenes if s.get("id") == shot_id), None)
        if scene is None:
            self._send_json({"error": f"shot {shot_id} not in scenes.json"}, 404)
            return
        anchor_path = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6", shot_id, "selected.png")
        if not os.path.isfile(anchor_path):
            self._send_json({
                "shot_id": shot_id,
                "pass": False,
                "status": "missing_anchor",
                "violations": [{"code": "missing_anchor", "severity": "high",
                                "detail": "no anchor image on disk"}],
            })
            return
        if not callout_path:
            import glob as _glob
            cand = sorted(_glob.glob(os.path.join(
                OUTPUT_DIR, "prompt_os", "previews", "characters", "*_callout*.png"
            )), key=os.path.getmtime, reverse=True)
            if cand:
                callout_path = cand[0]
        try:
            verdict = audit_scene_anchor(scene, anchor_path, character_rules,
                                         callout_path=callout_path)
        except Exception as e:
            self._send_json({"error": f"audit failed: {e}"}, 500)
            return
        # Persist into shot_gates so the UI can gate Kling on this result
        try:
            from lib.shot_gates import apply_audit_result
            project_dir = self._project_dir_for(project_slug)
            apply_audit_result(project_dir, shot_id, verdict)
        except Exception:
            pass
        self._send_json({
            "shot_id": shot_id,
            "anchor_path": anchor_path,
            "callout_path": callout_path,
            **verdict,
        })

    def _handle_v6_anchors_audit_batch(self):
        """Audit every anchor in the project. Returns per-shot verdicts.

        Body: {project?: str, character_rules?: dict}
        """
        from lib.anchor_auditor import audit_batch
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            body = {}
        project_slug = (body.get("project") or "default").strip() or "default"
        character_rules = body.get("character_rules") or None

        ok, reason, _t = _check_budget_gate(0.40)
        if not ok:
            self._send_json({"error": "budget_exceeded", "reason": reason}, 402)
            return

        scenes_path = os.path.join(
            OUTPUT_DIR, "projects", project_slug, "prompt_os", "scenes.json"
        )
        if not os.path.isfile(scenes_path):
            self._send_json({"error": f"scenes.json not found for project {project_slug}"}, 404)
            return
        try:
            with open(scenes_path, "r", encoding="utf-8") as f:
                scenes = json.load(f)
        except Exception as e:
            self._send_json({"error": f"scenes.json read failed: {e}"}, 500)
            return
        anchors_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6")
        try:
            report = audit_batch(scenes, anchors_dir, character_rules)
        except Exception as e:
            self._send_json({"error": f"audit_batch failed: {e}"}, 500)
            return
        audits_dir = os.path.join(OUTPUT_DIR, "pipeline", "audits")
        try:
            os.makedirs(audits_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(os.path.join(audits_dir, f"anchors_{ts}.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        except Exception:
            pass
        self._send_json(report)

    def _handle_v6_reference_upload(self):
        """Upload reference photo for V6 pipeline (character, environment, prop sheets)."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        parts_ct = content_type.split("boundary=")
        if len(parts_ct) < 2:
            self._send_json({"error": "No boundary"}, 400)
            return
        boundary = parts_ct[1].strip().encode()

        body = self._read_body()
        parts = self._parse_multipart(body, boundary)

        ref_type = "character"  # character, environment, prop, costume
        ref_name = ""
        file_data = None
        file_ext = ".png"

        for part in parts:
            if part.get("name") == "type":
                ref_type = part["data"].decode().strip()
            elif part.get("name") == "name":
                ref_name = part["data"].decode().strip()
            elif part.get("name") == "file" and part.get("filename"):
                file_data = part["data"]
                ext = os.path.splitext(part["filename"])[1].lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp"):
                    file_ext = ext

        if not file_data:
            self._send_json({"error": "No file uploaded"}, 400)
            return
        if not ref_name:
            ref_name = f"ref_{ref_type}_{int(time.time())}"

        # Save to output/pipeline/references_v6/<type>/<name>.<ext>
        refs_dir = os.path.join(OUTPUT_DIR, "pipeline", "references_v6", ref_type)
        os.makedirs(refs_dir, exist_ok=True)
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', ref_name)
        filepath = os.path.join(refs_dir, f"{safe_name}{file_ext}")
        with open(filepath, "wb") as f:
            f.write(file_data)

        self._send_json({
            "ok": True,
            "path": filepath,
            "url": f"/api/v6/anchor-image/../references_v6/{ref_type}/{safe_name}{file_ext}",
            "type": ref_type,
            "name": ref_name
        })

    def _handle_pipeline_reset(self):
        """Reset pipeline to a specific state."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        target = body.get("to_state", "IDLE")
        pipeline = self._get_pipeline_state()
        try:
            pipeline.reset_to(target)
            self._send_json({"ok": True, "state": pipeline.state, "progress": pipeline.get_progress()})
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    # ──── Preproduction Handlers ────

    def _get_preprod_store(self):
        from lib.preproduction_assets import PreproductionStore
        return PreproductionStore(OUTPUT_DIR)

    def _handle_preproduction_get_packages(self):
        store = self._get_preprod_store()
        self._send_json({"packages": store.get_all(), "mode": store.get_mode()})

    def _handle_preproduction_get_package(self, pkg_id):
        store = self._get_preprod_store()
        pkg = store.get_by_id(pkg_id)
        if pkg:
            self._send_json(pkg)
        else:
            self._send_json({"error": "Package not found"}, 404)

    def _handle_preproduction_report(self):
        from lib.preproduction_assets import generate_preproduction_report
        store = self._get_preprod_store()
        packages = store.get_all()
        # Get shots from plan if available
        shots = []
        if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            try:
                with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                    plan = json.load(f)
                for beat in plan.get("beats", []):
                    shots.extend(beat.get("shots", []))
                if not shots:
                    shots = plan.get("scenes", [])
            except Exception:
                pass
        report = generate_preproduction_report(packages, shots)
        self._send_json(report)

    def _handle_preproduction_validate_get(self):
        from lib.preproduction_assets import validate_preproduction
        store = self._get_preprod_store()
        packages = store.get_all()
        mode = store.get_mode()
        shots = []
        if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            try:
                with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                    plan = json.load(f)
                for beat in plan.get("beats", []):
                    shots.extend(beat.get("shots", []))
                if not shots:
                    shots = plan.get("scenes", [])
            except Exception:
                pass
        result = validate_preproduction(packages, shots, mode)
        self._send_json(result)

    def _handle_preproduction_create_package(self):
        from lib.preproduction_assets import create_package
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        try:
            pkg = create_package(
                package_type=body.get("package_type", "character"),
                name=body.get("name", "Unnamed"),
                description=body.get("description", ""),
                mode=body.get("mode", self._get_preprod_store().get_mode()),
                related_ids=body.get("related_ids"),
                must_keep=body.get("must_keep"),
                avoid=body.get("avoid"),
                canonical_notes=body.get("canonical_notes"),
                lock_strength=body.get("lock_strength", 0.8),
            )
            store = self._get_preprod_store()
            store.save_package(pkg)
            self._send_json({"ok": True, "package": pkg})
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_preproduction_update_package(self, pkg_id):
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_preprod_store()
        pkg = store.get_by_id(pkg_id)
        if not pkg:
            self._send_json({"error": "Package not found"}, 404)
            return
        for field in ("name", "description", "must_keep", "avoid",
                      "canonical_notes", "lock_strength", "mode"):
            if field in body:
                pkg[field] = body[field]
        store.save_package(pkg)
        self._send_json({"ok": True, "package": pkg})

    def _handle_preproduction_generate_package(self, pkg_id):
        """Generate a single composite sheet image for a package.

        Produces ONE image with all views/angles arranged in a grid (character
        turnaround, environment collage, etc.). This single image becomes the
        canonical @Tag reference for scene generation.

        Includes vision-based quality gate: analyzes the generated image and
        retries up to MAX_RETRIES times if it fails quality checks.

        Accepts optional `model` in request body. Defaults to best engine
        for the package type (gemini_2.5_flash for photorealism).
        """
        from lib.preproduction_assets import (
            build_sheet_prompt, update_sheet_image, analyze_sheet_quality,
        )
        from lib.video_generator import generate_sheet_image, SHEET_ENGINE_DEFAULTS
        store = self._get_preprod_store()
        pkg = store.get_by_id(pkg_id)
        if not pkg:
            self._send_json({"error": "Package not found"}, 404)
            return

        # Accept optional model override from request body
        try:
            body = json.loads(self._read_body())
        except Exception:
            body = {}
        pkg_type = pkg.get("type", "character")
        model = body.get("model") or SHEET_ENGINE_DEFAULTS.get(pkg_type, "gemini_2.5_flash")

        pkg["status"] = "generating"
        store.save_package(pkg)
        img_dir = store.package_image_dir(pkg_id)

        prompt = build_sheet_prompt(pkg)
        results = []
        MAX_RETRIES = 2  # up to 3 total attempts

        for attempt in range(1 + MAX_RETRIES):
            try:
                print(f"[GENERATE] {pkg.get('name')} attempt {attempt + 1}/{1 + MAX_RETRIES} engine={model}")
                img_path = generate_sheet_image(
                    prompt=prompt,
                    model=model,
                    seed=pkg.get("seed"),
                )
                if img_path and os.path.isfile(img_path):
                    import shutil
                    dest = os.path.join(img_dir, "sheet.png")
                    shutil.copy2(img_path, dest)

                    # Quality gate — analyze before accepting
                    qa = analyze_sheet_quality(dest, pkg)
                    qa_passed = qa.get("pass", False)
                    qa_skipped = qa.get("skipped", False)

                    if qa_passed or qa_skipped:
                        pkg = update_sheet_image(pkg, "sheet", dest, prompt_used=prompt)
                        pkg["hero_image_path"] = dest
                        pkg["hero_view"] = "sheet"
                        pkg["qa_result"] = qa
                        results.append({
                            "view": "sheet", "status": "generated",
                            "path": dest, "qa": qa, "attempt": attempt + 1,
                        })
                        break  # passed quality gate
                    else:
                        # Failed QA — log issues and retry
                        issues = qa.get("issues", [])
                        print(f"[QA] {pkg.get('name')} FAILED attempt {attempt + 1}: {issues}")
                        results.append({
                            "view": "sheet", "status": "qa_failed",
                            "qa": qa, "attempt": attempt + 1,
                        })
                        if attempt < MAX_RETRIES:
                            continue  # retry
                        else:
                            # Accept on final attempt despite QA failure
                            pkg = update_sheet_image(pkg, "sheet", dest, prompt_used=prompt)
                            pkg["hero_image_path"] = dest
                            pkg["hero_view"] = "sheet"
                            pkg["qa_result"] = qa
                            pkg["qa_warning"] = "Accepted after max retries despite QA failure"
                            results.append({
                                "view": "sheet", "status": "generated_with_warnings",
                                "path": dest, "qa": qa, "attempt": attempt + 1,
                            })
                            break
                else:
                    pkg = update_sheet_image(pkg, "sheet", None, prompt_used=prompt)
                    results.append({"view": "sheet", "status": "failed", "attempt": attempt + 1})
                    if attempt < MAX_RETRIES:
                        continue
                    break
            except Exception as e:
                pkg = update_sheet_image(pkg, "sheet", None, prompt_used=prompt)
                results.append({
                    "view": "sheet", "status": "error",
                    "error": str(e), "attempt": attempt + 1,
                })
                if attempt < MAX_RETRIES:
                    continue
                break

        pkg["status"] = "generated"
        store.save_package(pkg)
        self._send_json({"ok": True, "package": pkg, "results": results})

    def _handle_preproduction_generate_view(self, pkg_id):
        """Generate or regenerate a single sheet view."""
        from lib.preproduction_assets import build_sheet_prompt, get_sheet_plan, update_sheet_image
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_preprod_store()
        pkg = store.get_by_id(pkg_id)
        if not pkg:
            self._send_json({"error": "Package not found"}, 404)
            return

        view_name = body.get("view", "")
        views = get_sheet_plan(pkg)
        view_def = next((v for v in views if v["view"] == view_name), None)
        if not view_def:
            self._send_json({"error": f"Unknown view: {view_name}"}, 400)
            return

        prompt = body.get("custom_prompt") or build_sheet_prompt(pkg, view_def)
        from lib.video_generator import generate_sheet_image, SHEET_ENGINE_DEFAULTS
        pkg_type = pkg.get("type", "character")
        model = body.get("model") or SHEET_ENGINE_DEFAULTS.get(pkg_type, "gemini_2.5_flash")
        img_dir = store.package_image_dir(pkg_id)

        try:
            img_path = generate_sheet_image(
                prompt=prompt, model=model, seed=body.get("seed"),
            )
            if img_path and os.path.isfile(img_path):
                import shutil
                dest = os.path.join(img_dir, f"{view_name}.png")
                shutil.copy2(img_path, dest)
                pkg = update_sheet_image(pkg, view_name, dest, seed=body.get("seed"), prompt_used=prompt)
                store.save_package(pkg)
                self._send_json({"ok": True, "view": view_name, "path": dest, "package": pkg})
            else:
                self._send_json({"error": "Image generation returned no result"}, 500)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_preproduction_hero_ref(self, pkg_id):
        from lib.preproduction_assets import select_hero_ref
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_preprod_store()
        pkg = store.get_by_id(pkg_id)
        if not pkg:
            self._send_json({"error": "Package not found"}, 404)
            return
        pkg = select_hero_ref(pkg, body.get("view", ""))
        store.save_package(pkg)
        self._send_json({"ok": True, "package": pkg})

    def _handle_preproduction_approve(self, pkg_id):
        from lib.preproduction_assets import approve_package
        store = self._get_preprod_store()
        pkg = store.get_by_id(pkg_id)
        if not pkg:
            self._send_json({"error": "Package not found"}, 404)
            return
        try:
            pkg = approve_package(pkg)
            store.save_package(pkg)
            self._send_json({"ok": True, "package": pkg})
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_preproduction_reject(self, pkg_id):
        from lib.preproduction_assets import reject_package
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            body = {}
        store = self._get_preprod_store()
        pkg = store.get_by_id(pkg_id)
        if not pkg:
            self._send_json({"error": "Package not found"}, 404)
            return
        pkg = reject_package(pkg, body.get("reason", ""))
        store.save_package(pkg)
        self._send_json({"ok": True, "package": pkg})

    def _handle_preproduction_delete(self, pkg_id):
        store = self._get_preprod_store()
        store.remove_package(pkg_id)
        self._send_json({"ok": True})

    def _handle_preproduction_upload_ref(self, pkg_id):
        """Upload an external reference image for a sheet view."""
        store = self._get_preprod_store()
        pkg = store.get_by_id(pkg_id)
        if not pkg:
            self._send_json({"error": "Package not found"}, 404)
            return
        # Parse multipart form data
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return
        import cgi
        form = cgi.FieldStorage(
            fp=self.rfile, headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
        )
        view_name = form.getfirst("view", "uploaded")
        file_item = form["file"] if "file" in form else None
        if not file_item or not file_item.file:
            self._send_json({"error": "No file uploaded"}, 400)
            return
        img_dir = store.package_image_dir(pkg_id)
        ext = os.path.splitext(file_item.filename or "img.png")[1] or ".png"
        dest = os.path.join(img_dir, f"{view_name}{ext}")
        with open(dest, "wb") as f:
            f.write(file_item.file.read())
        from lib.preproduction_assets import update_sheet_image
        pkg = update_sheet_image(pkg, view_name, dest, prompt_used="uploaded")
        store.save_package(pkg)
        self._send_json({"ok": True, "view": view_name, "path": dest, "package": pkg})

    def _handle_preproduction_plan_packages(self):
        """Auto-plan packages from current beats + assets."""
        from lib.preproduction_assets import plan_packages_from_beats
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            body = {}
        store = self._get_preprod_store()
        mode = body.get("mode", store.get_mode())
        # Get beats from plan
        beats = []
        if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            try:
                with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                    plan = json.load(f)
                beats = plan.get("beats", [])
            except Exception:
                pass
        chars = _prompt_os.get_characters() or []
        envs = _prompt_os.get_environments() or []
        existing = store.get_all()
        new_pkgs = plan_packages_from_beats(beats, chars, envs, mode, existing)
        for pkg in new_pkgs:
            store.save_package(pkg)
        self._send_json({"ok": True, "new_packages": len(new_pkgs), "packages": store.get_all()})

    def _handle_preproduction_bind_shots(self):
        """Bind shots in the current plan to preproduction packages."""
        from lib.preproduction_assets import bind_shots_to_packages
        store = self._get_preprod_store()
        packages = store.get_all()
        if not os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
            self._send_json({"error": "No plan found"}, 404)
            return
        try:
            with open(AUTO_DIRECTOR_PLAN_PATH, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except Exception:
            self._send_json({"error": "Could not read plan"}, 500)
            return
        # Flatten shots, bind, then put back
        all_shots = []
        for beat in plan.get("beats", []):
            all_shots.extend(beat.get("shots", []))
        if not all_shots:
            all_shots = plan.get("scenes", [])
        bind_shots_to_packages(all_shots, packages)
        # Save plan back
        with _plan_file_lock:
            with open(AUTO_DIRECTOR_PLAN_PATH, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
        bound_count = sum(1 for s in all_shots if s.get("character_package_id") or s.get("environment_package_id"))
        self._send_json({"ok": True, "bound_shots": bound_count, "total_shots": len(all_shots)})

    def _handle_preproduction_set_mode(self):
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_preprod_store()
        mode = body.get("mode", "fast")
        store.set_mode(mode)
        self._send_json({"ok": True, "mode": mode})

    # ──── Taste Profile Handlers ────

    def _get_taste_store(self):
        from lib.taste_profile import TasteStore
        data_dir = os.path.join(OUTPUT_DIR, "taste")
        return TasteStore(data_dir)

    def _handle_taste_get_quiz(self):
        from lib.taste_profile import get_quiz_pairs, DIMENSION_LABELS
        self._send_json({"pairs": get_quiz_pairs(), "dimension_labels": DIMENSION_LABELS})

    def _handle_taste_get_overall(self):
        store = self._get_taste_store()
        profile = store.get_overall()
        if profile:
            from lib.taste_profile import generate_taste_summary
            profile["summary"] = generate_taste_summary(profile)
            self._send_json(profile)
        else:
            self._send_json({"exists": False})

    def _handle_taste_get_project(self, project_id):
        store = self._get_taste_store()
        profile = store.get_project_profile(project_id)
        if profile:
            from lib.taste_profile import generate_taste_summary
            profile["summary"] = generate_taste_summary(profile)
            self._send_json(profile)
        else:
            self._send_json({"exists": False})

    def _handle_taste_get_blended(self):
        store = self._get_taste_store()
        # Use project_id from query if available
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        project_id = qs.get("project_id", [None])[0]
        blended = store.get_blended(project_id)
        from lib.taste_profile import generate_taste_summary
        blended["summary"] = generate_taste_summary(blended)
        self._send_json(blended)

    def _handle_taste_save_overall(self):
        from lib.taste_profile import create_profile, update_from_sliders
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_taste_store()
        profile = store.get_overall() or create_profile(
            name=body.get("name", "My Style"), source="manual", is_overall=True)
        if "dimensions" in body:
            profile = update_from_sliders(profile, body["dimensions"])
        if "notes" in body:
            profile["notes"] = body["notes"]
        store.save_overall(profile)
        from lib.taste_profile import generate_taste_summary
        profile["summary"] = generate_taste_summary(profile)
        self._send_json({"ok": True, "profile": profile})

    def _handle_taste_submit_quiz(self):
        from lib.taste_profile import create_profile, process_quiz_answers, generate_taste_summary
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_taste_store()
        answers = body.get("answers", [])
        is_project = body.get("is_project", False)
        project_id = body.get("project_id")
        if is_project and project_id:
            profile = store.get_project_profile(project_id) or create_profile(
                name=body.get("name", "Project Style"), source="quiz", project_id=project_id)
        else:
            profile = store.get_overall() or create_profile(
                name="My Style", source="quiz", is_overall=True)
        profile = process_quiz_answers(profile, answers)
        if is_project and project_id:
            store.save_project_profile(project_id, profile)
        else:
            store.save_overall(profile)
        profile["summary"] = generate_taste_summary(profile)
        self._send_json({"ok": True, "profile": profile})

    def _handle_taste_update_sliders(self):
        from lib.taste_profile import update_from_sliders, generate_taste_summary
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_taste_store()
        is_project = body.get("is_project", False)
        project_id = body.get("project_id")
        if is_project and project_id:
            profile = store.get_project_profile(project_id)
            if not profile:
                self._send_json({"error": "Project profile not found"}, 404)
                return
        else:
            profile = store.get_overall()
            if not profile:
                self._send_json({"error": "Overall profile not found"}, 404)
                return
        profile = update_from_sliders(profile, body.get("sliders", {}))
        if is_project and project_id:
            store.save_project_profile(project_id, profile)
        else:
            store.save_overall(profile)
        profile["summary"] = generate_taste_summary(profile)
        self._send_json({"ok": True, "profile": profile})

    def _handle_taste_save_project(self, project_id):
        from lib.taste_profile import create_profile, update_from_sliders, generate_taste_summary
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_taste_store()
        profile = store.get_project_profile(project_id) or create_profile(
            name=body.get("name", "Project Style"), source="manual", project_id=project_id)
        if "dimensions" in body:
            profile = update_from_sliders(profile, body["dimensions"])
        if "inherit_overall" in body:
            profile["inherit_overall"] = body["inherit_overall"]
        if "notes" in body:
            profile["notes"] = body["notes"]
        store.save_project_profile(project_id, profile)
        profile["summary"] = generate_taste_summary(profile)
        self._send_json({"ok": True, "profile": profile})

    def _handle_taste_record_behavior(self):
        from lib.taste_profile import record_behavior
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        store = self._get_taste_store()
        action = body.get("action", "")
        context = body.get("context", {})
        is_project = body.get("is_project", False)
        project_id = body.get("project_id")
        # Record to both overall and project if applicable
        overall = store.get_overall()
        if overall:
            overall = record_behavior(overall, action, context)
            store.save_overall(overall)
        if is_project and project_id:
            proj = store.get_project_profile(project_id)
            if proj:
                proj = record_behavior(proj, action, context)
                store.save_project_profile(project_id, proj)
        self._send_json({"ok": True})

    def _handle_taste_reset_overall(self):
        store = self._get_taste_store()
        store.reset_overall()
        self._send_json({"ok": True})

    def _handle_movie_scene_lock(self, scene_index):
        """Lock specific fields of a scene."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No movie plan exists"}, 400)
            return

        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": f"Scene index {scene_index} out of range"}, 400)
            return

        scene = scenes[scene_index]
        locks = scene.get("locks", {})
        fields = body.get("fields", [])

        if fields == "all":
            # Lock all editable fields
            for key in scene:
                if key not in ("id", "order", "locks", "status", "validation"):
                    locks[key] = True
        else:
            for field in fields:
                locks[field] = True

        scene["locks"] = locks
        scenes[scene_index] = scene
        plan["scenes"] = scenes
        save_movie_plan(plan, OUTPUT_DIR)

        self._send_json({"ok": True, "scene": scene})

    def _handle_movie_scene_unlock(self, scene_index):
        """Unlock specific fields of a scene."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No movie plan exists"}, 400)
            return

        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": f"Scene index {scene_index} out of range"}, 400)
            return

        scene = scenes[scene_index]
        locks = scene.get("locks", {})
        fields = body.get("fields", [])

        if fields == "all":
            locks = {}
        else:
            for field in fields:
                locks.pop(field, None)

        scene["locks"] = locks
        scenes[scene_index] = scene
        plan["scenes"] = scenes
        save_movie_plan(plan, OUTPUT_DIR)

        self._send_json({"ok": True, "scene": scene})

    def _handle_scene_preview(self, scene_index):
        """Generate a preview for a single scene."""
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No plan"}, 400)
            return
        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": "Invalid index"}, 400)
            return

        scene = scenes[scene_index]
        prompt = scene.get("shot_prompt", scene.get("prompt", ""))
        notes = scene.get("notes", "")

        # Generate preview using existing thumbnail infrastructure
        result = _generate_scene_thumbnail(
            scene_index, prompt, notes,
            scene_data=scene
        )

        fingerprint = _compute_scene_fingerprint(scene)
        engine_used = "runway" if scene.get("character_photo_path") else "grok"
        # Detect engine from enriched data
        enriched_copy = dict(scene)
        _enrich_scene_with_assets(enriched_copy)
        if enriched_copy.get("character_photo_path"):
            engine_used = "runway"

        if "error" in result:
            scene["preview"] = {
                "status": "failed",
                "image_url": None,
                "prompt_hash": fingerprint,
                "last_generated_at": datetime.utcnow().isoformat(),
                "engine": engine_used,
                "error": result["error"],
            }
        else:
            scene["preview"] = {
                "status": "ready",
                "image_url": result.get("preview_url", ""),
                "video_url": result.get("video_url", ""),
                "prompt_hash": fingerprint,
                "last_generated_at": datetime.utcnow().isoformat(),
                "engine": engine_used,
                "error": None,
            }

        scenes[scene_index] = scene
        plan["scenes"] = scenes
        save_movie_plan(plan, OUTPUT_DIR)

        # Sync to pipeline
        self._sync_movie_plan_to_pipeline(plan)

        self._send_json({"ok": True, "preview": scene["preview"]})

    def _handle_scenes_preview_batch(self):
        """Generate previews for all missing/stale scenes in background."""
        try:
            body = json.loads(self._read_body()) if self.headers.get("Content-Length") else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        mode = body.get("mode", "missing_and_stale")

        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No plan"}, 400)
            return

        with scene_preview_lock:
            if scene_preview_state["running"]:
                self._send_json({"error": "Batch already running"}, 409)
                return

        scenes = plan["scenes"]
        to_generate = []
        for i, scene in enumerate(scenes):
            preview = scene.get("preview", {})
            status = preview.get("status", "none")
            if mode == "all" or status in ("none", "stale", "failed"):
                to_generate.append(i)

        if not to_generate:
            self._send_json({"ok": True, "message": "Nothing to generate", "queued": 0})
            return

        # Mark queued
        for i in to_generate:
            scenes[i].setdefault("preview", {})["status"] = "queued"
        save_movie_plan(plan, OUTPUT_DIR)

        with scene_preview_lock:
            scene_preview_state["running"] = True
            scene_preview_state["total"] = len(to_generate)
            scene_preview_state["completed"] = 0
            scene_preview_state["failed"] = 0
            scene_preview_state["scenes"] = [
                {"index": i, "preview": scenes[i].get("preview", {})} for i in range(len(scenes))
            ]

        def _run_batch():
            try:
                for idx in to_generate:
                    current_plan = load_movie_plan(OUTPUT_DIR)
                    if not current_plan:
                        break
                    sc = current_plan["scenes"][idx]
                    sc.setdefault("preview", {})["status"] = "generating"
                    save_movie_plan(current_plan, OUTPUT_DIR)

                    with scene_preview_lock:
                        scene_preview_state["scenes"][idx]["preview"] = dict(sc.get("preview", {}))

                    prompt = sc.get("shot_prompt", sc.get("prompt", ""))
                    notes = sc.get("notes", "")
                    result = _generate_scene_thumbnail(idx, prompt, notes, scene_data=sc)
                    fingerprint = _compute_scene_fingerprint(sc)

                    enriched_copy = dict(sc)
                    _enrich_scene_with_assets(enriched_copy)
                    engine_used = "runway" if enriched_copy.get("character_photo_path") else "grok"

                    if "error" in result:
                        sc["preview"] = {
                            "status": "failed",
                            "image_url": None,
                            "prompt_hash": fingerprint,
                            "last_generated_at": datetime.utcnow().isoformat(),
                            "engine": engine_used,
                            "error": result["error"],
                        }
                        with scene_preview_lock:
                            scene_preview_state["failed"] += 1
                    else:
                        sc["preview"] = {
                            "status": "ready",
                            "image_url": result.get("preview_url", ""),
                            "video_url": result.get("video_url", ""),
                            "prompt_hash": fingerprint,
                            "last_generated_at": datetime.utcnow().isoformat(),
                            "engine": engine_used,
                            "error": None,
                        }
                        with scene_preview_lock:
                            scene_preview_state["completed"] += 1

                    current_plan["scenes"][idx] = sc
                    save_movie_plan(current_plan, OUTPUT_DIR)

                    with scene_preview_lock:
                        scene_preview_state["scenes"][idx]["preview"] = dict(sc["preview"])
            finally:
                with scene_preview_lock:
                    scene_preview_state["running"] = False

        t = threading.Thread(target=_run_batch, daemon=True)
        t.start()

        self._send_json({"ok": True, "queued": len(to_generate)})

    def _handle_scenes_preview_status(self):
        """Return per-scene preview status for polling."""
        with scene_preview_lock:
            data = {
                "running": scene_preview_state["running"],
                "total": scene_preview_state["total"],
                "completed": scene_preview_state["completed"],
                "failed": scene_preview_state["failed"],
                "scenes": list(scene_preview_state["scenes"]),
            }
        self._send_json(data)

    def _sync_movie_plan_to_pipeline(self, plan):
        """Sync movie plan scenes to the generation pipeline files."""
        try:
            compat_plan = {
                "song_path": plan.get("bible", {}).get("song_path", ""),
                "style": plan.get("bible", {}).get("style", ""),
                "scenes": plan.get("scenes", []),
            }
            with open(AUTO_DIRECTOR_PLAN_PATH, "w") as f:
                json.dump(compat_plan, f, indent=2)
            _sync_auto_plan_to_scene_plan(compat_plan)
        except Exception as e:
            print(f"[MOVIE PLANNER] Sync to pipeline failed: {e}")


def _cleanup_old_temp_files():
    """Remove temp files older than 7 days from output directories."""
    import time
    cutoff = time.time() - 7 * 86400
    for dirname in [os.path.join(OUTPUT_DIR, "temp"), os.path.join(OUTPUT_DIR, "waveforms")]:
        if not os.path.isdir(dirname):
            continue
        for f in os.listdir(dirname):
            fp = os.path.join(dirname, f)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
            except OSError:
                pass


import ssl
_SSL_CERT = os.environ.get("LUMN_SSL_CERT", "")
_SSL_KEY = os.environ.get("LUMN_SSL_KEY", "")


def _preflight_production_checks():
    """Refuse to start in production mode without the safety baseline set.
    Enable by setting LUMN_PRODUCTION=1 when running behind a public tunnel."""
    if os.environ.get("LUMN_PRODUCTION") != "1":
        return
    missing = []
    tok = os.environ.get("LUMN_API_TOKEN", "").strip()
    if not tok or tok == "test" or len(tok) < 16:
        missing.append("LUMN_API_TOKEN (>=16 chars, not 'test')")
    if not os.environ.get("LUMN_BETA_PASSWORD", "").strip():
        missing.append("LUMN_BETA_PASSWORD (shared invite code)")
    if not os.environ.get("LUMN_DAILY_CAP_CENTS", "").strip():
        missing.append("LUMN_DAILY_CAP_CENTS (global spend cap)")
    if not os.environ.get("LUMN_ADMIN_EMAILS", "").strip():
        missing.append("LUMN_ADMIN_EMAILS (so /api/metrics is reachable)")
    if missing:
        print("\n[PRODUCTION MODE] Refusing to start. Set these env vars first:")
        for m in missing:
            print(f"  - {m}")
        print("\nOr unset LUMN_PRODUCTION to run in dev mode.\n")
        raise SystemExit(1)
    print("[PRODUCTION MODE] preflight passed — beta gate active")


def main():
    _cleanup_old_temp_files()
    _preflight_production_checks()

    # One-time migration: move pre-refactor output/prompt_os/ into
    # output/projects/default/. Safe to call on every boot — returns None
    # once projects/ has at least one subdirectory.
    try:
        _migrated_slug = active_project.migrate_legacy_workspace()
        if _migrated_slug:
            print(f"[migration] Moved legacy workspace into project '{_migrated_slug}'")
        active_project.ensure_vault_scaffold()
    except Exception as _e:
        print(f"[migration] WARN: {_e}")

    # ThreadingHTTPServer: one thread per request so parallel fetches from the
    # browser don't queue behind a long-running generation. Single-threaded
    # HTTPServer caused ERR_CONNECTION_REFUSED storms under Playwright load.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True

    if _SSL_CERT and _SSL_KEY and os.path.isfile(_SSL_CERT) and os.path.isfile(_SSL_KEY):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(_SSL_CERT, _SSL_KEY)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print(f"[HTTPS] SSL enabled with cert: {_SSL_CERT}")
        proto = "https"
    else:
        proto = "http"

    print(f"\n  LUMN Studio")
    print(f"  UI running at {proto}://localhost:{PORT}")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
