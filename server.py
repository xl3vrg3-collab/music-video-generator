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
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

# Ensure ffmpeg is on PATH (winget installs to a long path)
_FFMPEG_DIR = os.path.expanduser(
    r"~\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
)
if os.path.isdir(_FFMPEG_DIR) and _FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
import uuid as _uuid
import urllib.parse
import zipfile
from http.server import HTTPServer, BaseHTTPRequestHandler

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from lib.audio_analyzer import analyze
from lib.scene_planner import plan_scenes, TRANSITION_TYPES, coherence_pass
from lib.video_generator import (
    generate_scene, generate_all, generate_from_photo,
    describe_photo, CAMERA_PRESETS, CAMERA_PROMPT_SUFFIXES,
    get_available_engines, SUPPORTED_ENGINES, ENGINE_GROK,
    _load_settings as _load_gen_settings,
    _get_character_references, _resolve_character_references,
    MODEL_DURATION_OPTIONS, get_valid_duration, get_smart_duration,
    extract_last_frame, extract_first_frame,
    SHOT_TYPE_REF_PRIORITY, select_refs_for_shot_type, build_shot_prompt,
    _runway_headers, _runway_poll, _download, RUNWAY_API_BASE,
)
from lib.video_stitcher import (
    stitch, apply_lyrics_overlay, apply_aspect_ratio, split_clip,
    ASPECT_PRESETS, _get_clip_duration,
    SPEED_OPTIONS, COLOR_GRADE_PRESETS, AUDIO_VIZ_STYLES,
    generate_credits, apply_watermark, extract_thumbnail,
    mix_audio_tracks, mix_multi_audio_tracks, export_for_platform, apply_beat_sync_cuts,
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
from lib.project_manager import ProjectManager
def _scene_gen_hash(scene: dict) -> str:
    """Hash the generation-relevant fields of a scene.
    If the hash matches the stored gen_hash, the clip can be reused."""
    parts = [
        scene.get("prompt", ""),
        str(scene.get("duration", 8)),
        scene.get("camera_movement", ""),
        scene.get("engine", ""),
        scene.get("characterId", ""),
        scene.get("costumeId", ""),
        scene.get("environmentId", ""),
        scene.get("first_frame_path", ""),
        scene.get("last_frame_path", ""),
        scene.get("photo_path", ""),
        "|".join(scene.get("photo_paths", [])),
        scene.get("video_path", ""),
        scene.get("character_photo_path", ""),
    ]
    raw = "||".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


PORT = int(os.environ.get("PORT", 3849))
_server_start_time = time.time()
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(PROJECT_DIR, "uploads")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

from lib.prompt_os import PromptOS
_prompt_os = PromptOS()

from lib.prompt_templates import (
    build_character_sheet_prompt, build_face_closeup_prompt,
    build_costume_sheet_prompt, build_environment_sheet_prompt,
    build_prop_sheet_prompt, build_reference_package, select_best_refs_for_shot,
    build_enhanced_shot_prompt, build_video_prompt,
)
PROMPT_OS_DATA_DIR = os.path.join(OUTPUT_DIR, "prompt_os")
os.makedirs(PROMPT_OS_DATA_DIR, exist_ok=True)

from lib.auto_agent import (
    get_or_create_run, get_current_run,
    OptimizationRun, EvalResult, EvalBatch, HarnessManager,
)
from lib.auto_director import AutoDirector, get_workflow_presets, save_custom_preset
from lib.director_brain import get_brain
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
AUDIO_GEN_DIR = os.path.join(OUTPUT_DIR, "audio")
os.makedirs(AUDIO_GEN_DIR, exist_ok=True)
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
POS_PHOTOS_DIR = os.path.join(PROMPT_OS_DATA_DIR, "photos")
POS_PHOTOS_CHARS_DIR = os.path.join(POS_PHOTOS_DIR, "characters")
POS_PHOTOS_COSTUMES_DIR = os.path.join(POS_PHOTOS_DIR, "costumes")
POS_PHOTOS_ENVS_DIR = os.path.join(POS_PHOTOS_DIR, "environments")
POS_PHOTOS_PROPS_DIR = os.path.join(POS_PHOTOS_DIR, "props")
POS_PREVIEWS_DIR = os.path.join(PROMPT_OS_DATA_DIR, "previews")
POS_PREVIEWS_CHARS_DIR = os.path.join(POS_PREVIEWS_DIR, "characters")
POS_PREVIEWS_COSTUMES_DIR = os.path.join(POS_PREVIEWS_DIR, "costumes")
POS_PREVIEWS_ENVS_DIR = os.path.join(POS_PREVIEWS_DIR, "environments")

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

# Cost defaults
COST_PER_VIDEO_GEN = 0.15
COST_PER_IMAGE_GEN = 0.02
DEFAULT_BUDGET = 10.00

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
os.makedirs(POS_PHOTOS_CHARS_DIR, exist_ok=True)
os.makedirs(POS_PHOTOS_COSTUMES_DIR, exist_ok=True)
os.makedirs(POS_PHOTOS_ENVS_DIR, exist_ok=True)
os.makedirs(POS_PHOTOS_PROPS_DIR, exist_ok=True)
os.makedirs(POS_PREVIEWS_CHARS_DIR, exist_ok=True)
os.makedirs(POS_PREVIEWS_COSTUMES_DIR, exist_ok=True)
os.makedirs(POS_PREVIEWS_ENVS_DIR, exist_ok=True)

# ---- Project Manager instance ----
_project_mgr = ProjectManager(FULL_PROJECTS_DIR, OUTPUT_DIR, UPLOADS_DIR, REFERENCES_DIR)

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
                        candidate = os.path.join(POS_PHOTOS_CHARS_DIR, f"{cid}{ext}")
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
                            candidate = os.path.join(POS_PHOTOS_COSTUMES_DIR, f"{_mqco.group(1)}{ext}")
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
                            candidate = os.path.join(POS_PHOTOS_ENVS_DIR, f"{_mqen.group(1)}{ext}")
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
        item["status"] = "completed"
        item["result_url"] = f"/api/clips/{os.path.basename(clip_path)}?v={mtime}"
        item["completed_at"] = time.time()
        item["progress"] = "done"

        # Update shot data in settings
        _queue_save_shot_clip(scene_id, item["shot_id"], clip_path)

    except Exception as e:
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
    })


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
    with open(SCENE_PLAN_PATH, "w") as f:
        json.dump(plan, f, indent=2)
    return plan


def _load_scene_plan():
    """Load scene plan from JSON. Falls back to auto_director_plan if scene_plan missing."""
    if os.path.isfile(SCENE_PLAN_PATH):
        with open(SCENE_PLAN_PATH, "r") as f:
            return json.load(f)
    # Fallback: try auto director plan and convert
    if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
        with open(AUTO_DIRECTOR_PLAN_PATH, "r") as f:
            ad_plan = json.load(f)
        if ad_plan and ad_plan.get("scenes"):
            _sync_auto_plan_to_scene_plan(ad_plan)
            return _load_scene_plan()
    return None


def _assign_shot_types(scenes):
    """Assign varied shot types to scenes for cinematic variety."""
    if not scenes:
        return

    n = len(scenes)

    # Cinematic pattern: establish -> build variety -> climax close-ups -> resolve wide
    patterns = {
        1: ["medium"],
        2: ["establishing", "close-up"],
        3: ["establishing", "medium", "close-up"],
        4: ["establishing", "medium", "close-up", "wide"],
        5: ["establishing", "medium", "close-up", "medium", "wide"],
    }

    if n <= 5:
        type_sequence = patterns.get(n, patterns[5])
    else:
        # Build a repeating pattern for longer sequences
        type_sequence = ["establishing"]  # Always start wide

        cycle = ["medium", "close-up", "medium", "full", "wide", "close-up", "medium"]
        for i in range(1, n - 1):
            type_sequence.append(cycle[(i - 1) % len(cycle)])

        type_sequence.append("wide")  # End wide for resolution

    for i, scene in enumerate(scenes):
        if i < len(type_sequence):
            # Only set if not already specified by user
            if not scene.get("shot_type") or scene.get("shot_type") == "medium":
                scene["shot_type"] = type_sequence[i]


def _sync_auto_plan_to_scene_plan(ad_plan=None):
    """Copy auto_director_plan.json into scene_plan.json format for generation pipeline compatibility."""
    try:
        if ad_plan is None:
            if not os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
                return
            with open(AUTO_DIRECTOR_PLAN_PATH, "r") as f:
                ad_plan = json.load(f)
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
        with open(SCENE_PLAN_PATH, "w") as f:
            json.dump(plan, f, indent=2)
    except Exception as e:
        print(f"[SYNC] Error syncing auto plan to scene plan: {e}")


def _enrich_scene_prompt(scene: dict, project_style: dict = None) -> str:
    """Enrich a scene's prompt with cinematic detail if it's too short/generic."""
    prompt = scene.get("shot_prompt", scene.get("prompt", ""))
    if not prompt or len(prompt) < 20:
        # Build from available fields
        parts = []
        if scene.get("summary"):
            parts.append(scene["summary"])
        if scene.get("action"):
            parts.append(scene["action"])
        if scene.get("visual_description"):
            parts.append(scene["visual_description"])
        prompt = ". ".join(parts) if parts else "Cinematic scene"

    # Add shot type framing if not present
    shot_type = scene.get("shot_type", "medium")
    if shot_type and shot_type not in prompt.lower():
        prompt = f"{shot_type} shot. {prompt}"

    # Add camera if specified
    camera = scene.get("camera", "") or scene.get("camera_movement", "")
    if camera and camera not in prompt.lower():
        prompt += f" Camera: {camera}."

    return prompt


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
            char_name = char_ref.get("name", "") if isinstance(char_ref, dict) else ""
            pos_char = None
            if char_id:
                pos_char = _prompt_os.get_character(char_id)
            # Fallback: if ID not found (deleted/recreated), match by name
            if not pos_char and char_name:
                all_chars = _prompt_os.get_characters()
                for c in all_chars:
                    if c.get("name", "").lower().strip() == char_name.lower().strip():
                        pos_char = c
                        # Auto-update the stale ID in the scene
                        char_ref["id"] = c["id"]
                        import sys as _sys_fb
                        _sys_fb.stderr.write(f"[ENRICH] Character '{char_name}' ID was stale ({char_id}), auto-updated to {c['id']}\n")
                        _sys_fb.stderr.flush()
                        break
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
                                    candidate = os.path.join(POS_PHOTOS_CHARS_DIR, f"{m.group(1)}{ext}")
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
                                candidate = os.path.join(POS_PHOTOS_CHARS_DIR, f"{m.group(1)}{ext}")
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
            cos_name = cos_ref.get("name", "") if isinstance(cos_ref, dict) else ""
            pos_costume = None
            if cos_id:
                pos_costume = _prompt_os.get_costume(cos_id)
            if not pos_costume and cos_name:
                for c in _prompt_os.get_costumes():
                    if c.get("name", "").lower().strip() == cos_name.lower().strip():
                        pos_costume = c
                        cos_ref["id"] = c["id"]
                        break
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
                                    candidate = os.path.join(POS_PHOTOS_COSTUMES_DIR, f"{m.group(1)}{ext}")
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
                                candidate = os.path.join(POS_PHOTOS_COSTUMES_DIR, f"{m.group(1)}{ext}")
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
            env_name = env_ref.get("name", "") if isinstance(env_ref, dict) else ""
            pos_env = None
            if env_id:
                pos_env = _prompt_os.get_environment(env_id)
            if not pos_env and env_name:
                for e in _prompt_os.get_environments():
                    if e.get("name", "").lower().strip() == env_name.lower().strip():
                        pos_env = e
                        env_ref["id"] = e["id"]
                        break
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
                                    candidate = os.path.join(POS_PHOTOS_ENVS_DIR, f"{m.group(1)}{ext}")
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
                                candidate = os.path.join(POS_PHOTOS_ENVS_DIR, f"{m.group(1)}{ext}")
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
                            candidate = os.path.join(POS_PHOTOS_CHARS_DIR, f"{m.group(1)}{ext}")
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
                            candidate = os.path.join(POS_PHOTOS_COSTUMES_DIR, f"{m.group(1)}{ext}")
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
        song_path = plan["song_path"]
        output_path = plan["output_path"]

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
    """
    Generate a preview thumbnail for a scene.

    Strategy:
    1. If a character photo exists, use Runway to generate a 5-second clip
       with the photo as character reference, then extract the first frame.
       This produces an accurate preview that matches the final video.
    2. Fallback: use Grok image generation (text-only, no character likeness).

    Returns dict with 'preview_url' on success or 'error' on failure.
    """
    import subprocess, tempfile

    full_prompt = prompt.strip()
    if notes and notes.strip():
        full_prompt = f"{full_prompt}. {notes.strip()}"

    out_path = os.path.join(SCENE_THUMBNAILS_DIR, f"scene_{index}.jpg")

    # --- Resolve character photo from scene data ---
    char_photo = None
    if scene_data:
        # Enrich scene to get photo paths
        enriched = dict(scene_data)
        _enrich_scene_with_assets(enriched)
        char_photo = enriched.get("character_photo_path", "") or None
        # Diagnostic logging (stderr to bypass HTTP handler)
        import sys as _sys
        chars_in_scene = scene_data.get("characters", [])
        _sys.stderr.write(f"[PREVIEW][{index}] Scene characters: {chars_in_scene}\n")
        _sys.stderr.write(f"[PREVIEW][{index}] Resolved char_photo: {char_photo}\n")
        _sys.stderr.write(f"[PREVIEW][{index}] costume_photo: {enriched.get('costume_photo_path', 'NONE')}\n")
        _sys.stderr.write(f"[PREVIEW][{index}] env_photo: {enriched.get('environment_photo_path', 'NONE')}\n")
        _sys.stderr.flush()
    else:
        print(f"[PREVIEW][{index}] No scene_data provided")

    # --- Strategy A: Generate scene image using text_to_image with @tag references ---
    # This creates a first frame with character likeness from reference photos.
    # Fast + cheap: gen4_image_turbo = 2 credits per image.
    has_any_photos = (char_photo and os.path.isfile(char_photo)) or \
        (scene_data and scene_data.get("costume_photo_path") and os.path.isfile(scene_data.get("costume_photo_path", ""))) or \
        (scene_data and scene_data.get("environment_photo_path") and os.path.isfile(scene_data.get("environment_photo_path", "")))
    if has_any_photos:
        try:
            from lib.video_generator import _runway_generate_scene_image, _download

            # Build @tag reference list (max 3 per API)
            refs = []
            if char_photo and os.path.isfile(char_photo):
                refs.append({"path": char_photo, "tag": "Character"})
            cos_p = enriched.get("costume_photo_path", "")
            if cos_p and os.path.isfile(cos_p):
                refs.append({"path": cos_p, "tag": "Costume"})
            env_p = enriched.get("environment_photo_path", "")
            if env_p and os.path.isfile(env_p):
                refs.append({"path": env_p, "tag": "Setting"})

            # Build prompt with @tag mentions
            tag_prompt = full_prompt
            if any(r["tag"] == "Character" for r in refs):
                tag_prompt = f"@Character in a cinematic scene. {tag_prompt}"
            if any(r["tag"] == "Costume" for r in refs):
                tag_prompt = f"{tag_prompt} Wearing the outfit from @Costume."
            if any(r["tag"] == "Setting" for r in refs):
                tag_prompt = f"{tag_prompt} Set in the location from @Setting."

            print(f"[PREVIEW][{index}] Generating scene image with {len(refs)} @tag refs: {[r['tag'] for r in refs]}")

            img_path = _runway_generate_scene_image(
                tag_prompt, refs,
                ratio="1280:720",
                model="gen4_image",
            )

            if img_path and os.path.isfile(img_path):
                # Copy to thumbnails dir
                import shutil
                shutil.copy2(img_path, out_path)
                _record_cost(f"thumb_{index}", "image_preview")
                print(f"[PREVIEW][{index}] Scene image saved: {out_path}")

                # Also save as the scene's first_frame for video generation
                first_frame_dir = os.path.join(OUTPUT_DIR, "first_frames")
                os.makedirs(first_frame_dir, exist_ok=True)
                first_frame_path = os.path.join(first_frame_dir, f"scene_{index}_first.jpg")
                shutil.copy2(img_path, first_frame_path)

                # Update the scene plan with the first frame path
                plan = _load_scene_plan()
                if plan and index < len(plan.get("scenes", [])):
                    plan["scenes"][index]["first_frame_path"] = first_frame_path
                    plan["scenes"][index]["_lastGeneratedPrompt"] = full_prompt
                    with open(SCENE_PLAN_PATH, "w") as f:
                        json.dump(plan, f, indent=2)

                return {
                    "preview_url": f"/api/scene-thumbnails/scene_{index}.jpg",
                    "first_frame_path": first_frame_path,
                }
            else:
                print(f"[PREVIEW][{index}] Scene image generation failed, falling back to Grok")

        except Exception as e:
            import sys as _sys2, traceback as _tb
            _sys2.stderr.write(f"[PREVIEW][{index}] Scene image FAILED: {e}\n")
            _tb.print_exc(file=_sys2.stderr)
            _sys2.stderr.flush()
            return {"error": f"Scene image failed: {str(e)[:200]}"}

    # --- Strategy B: Runway text-to-video (no character photo) ---
    try:
        from lib.video_generator import (
            _runway_submit_text_to_video, _runway_poll, _download
        )
        import sys as _sys3

        _sys3.stderr.write(f"[PREVIEW][{index}] Using Runway text-to-video (no character photo)\n")
        _sys3.stderr.flush()

        preview_engine = "gen4.5"
        if scene_data:
            eng = scene_data.get("engine", "")
            if eng and eng not in ("grok", "openai"):
                preview_engine = eng
        if preview_engine in ("gen4.5", "runway"):
            settings_b = _load_settings()
            ds_b = settings_b.get("director_state", {})
            eng_b = ds_b.get("engine", "")
            if eng_b and eng_b not in ("grok", "openai"):
                preview_engine = eng_b

        # Add costume/environment descriptions if available
        enriched_prompt = full_prompt
        if scene_data:
            enriched_b = dict(scene_data)
            _enrich_scene_with_assets(enriched_b)
            cos_p = enriched_b.get("costume_photo_path", "")
            env_p = enriched_b.get("environment_photo_path", "")
            if cos_p and os.path.isfile(cos_p):
                try:
                    from lib.video_generator import _describe_entity_photo
                    cd = _describe_entity_photo(cos_p, "costume")
                    if cd: enriched_prompt = f"{enriched_prompt}. Wearing: {cd}"
                except Exception: pass
            if env_p and os.path.isfile(env_p):
                try:
                    from lib.video_generator import _describe_entity_photo
                    ed = _describe_entity_photo(env_p, "environment")
                    if ed: enriched_prompt = f"{enriched_prompt}. Setting: {ed}"
                except Exception: pass

        if len(enriched_prompt) > 500:
            enriched_prompt = enriched_prompt[:497] + "..."

        # Use environment photo as promptImage if available (visual scene reference)
        env_photo_for_input = None
        if scene_data:
            ep = enriched_b.get("environment_photo_path", "") if 'enriched_b' in dir() else ""
            if ep and os.path.isfile(ep):
                env_photo_for_input = ep

        scene_dur = int(scene_data.get("duration", 5)) if scene_data else 5
        task_id = _runway_submit_text_to_video(
            enriched_prompt,
            duration=scene_dur,
            model=preview_engine,
            first_frame_path=env_photo_for_input,
        )

        _sys3.stderr.write(f"[PREVIEW][{index}] Runway text-to-video task: {task_id[:16]}...\n")
        _sys3.stderr.flush()
        video_info = _runway_poll(task_id)

        clip_path = os.path.join(SCENE_THUMBNAILS_DIR, f"scene_{index}_preview.mp4")
        _download(video_info["url"], clip_path)

        subprocess.run(
            ["ffmpeg", "-y", "-i", clip_path, "-vframes", "1", "-q:v", "2", out_path],
            capture_output=True, timeout=30,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )

        if os.path.isfile(clip_path):
            _record_cost(f"thumb_{index}", "video_preview")
            return {
                "preview_url": f"/api/scene-thumbnails/scene_{index}.jpg",
                "video_url": f"/api/scene-thumbnails/scene_{index}_preview.mp4",
            }

        # If clip download failed somehow, just return what we have
        if os.path.isfile(out_path):
            _record_cost(f"thumb_{index}", "video_preview")
            return {"preview_url": f"/api/scene-thumbnails/scene_{index}.jpg"}

        return {"error": "Preview generation produced no output"}

    except Exception as e:
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
        with open(SCENE_PLAN_PATH, "w") as f:
            json.dump(plan, f, indent=2)


def _update_scene_plan_approval(index: int, approved: bool, notes: str = ""):
    """Persist approval status into the scene_plan.json for the given scene index."""
    plan = _load_scene_plan()
    if plan and 0 <= index < len(plan["scenes"]):
        plan["scenes"][index]["preview_approved"] = approved
        if notes:
            plan["scenes"][index]["preview_notes"] = notes
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
    }


def _save_cost_tracker(tracker: dict):
    with open(COST_TRACKER_PATH, "w") as f:
        json.dump(tracker, f, indent=2)


def _record_cost(scene_key: str, gen_type: str = "video"):
    """Record a generation cost. gen_type: 'video' or 'image'."""
    tracker = _load_cost_tracker()
    if gen_type == "video":
        cost = COST_PER_VIDEO_GEN
        tracker["video_generations"] += 1
    else:
        cost = COST_PER_IMAGE_GEN
        tracker["image_generations"] += 1
    tracker["total_cost"] = round(tracker["total_cost"] + cost, 4)
    tracker["scene_costs"][str(scene_key)] = round(
        tracker["scene_costs"].get(str(scene_key), 0) + cost, 4
    )
    _save_cost_tracker(tracker)
    return tracker


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
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
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
        with open(MANUAL_PLAN_PATH, "r") as f:
            return json.load(f)
    return {"scenes": [], "song_path": None}


def _save_manual_plan(plan: dict):
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
                        candidate = os.path.join(POS_PHOTOS_CHARS_DIR, f"{cid}{ext}")
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
                            candidate = os.path.join(POS_PHOTOS_COSTUMES_DIR, f"{cid_cos}{ext}")
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
                            candidate = os.path.join(POS_PHOTOS_ENVS_DIR, f"{eid_env}{ext}")
                            if os.path.isfile(candidate):
                                environment_photo_path = candidate
                                break
                if environment_photo_path:
                    print(f"[GEN] Resolved environment reference photo: {environment_photo_path}")

        gen_scene = {
            "prompt": gen_prompt,
            "duration": scene.get("duration", 8),
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
        duration = scene.get("duration", 8)
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
        scenes_to_gen = []
        cached_count = 0
        for i, s in enumerate(plan["scenes"]):
            has_valid_clip = (s.get("has_clip") and s.get("clip_path")
                              and os.path.isfile(s.get("clip_path", "")))
            if has_valid_clip and s.get("gen_hash") == _scene_gen_hash(s):
                cached_count += 1
                continue  # clip exists and settings unchanged — skip
            scenes_to_gen.append((i, s))

        if cached_count:
            print(f"[generate_all] Skipping {cached_count} cached scene(s)")

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
                                    _cand = os.path.join(POS_PHOTOS_CHARS_DIR, f"{_mc.group(1)}{_ext}")
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
                                    _cand = os.path.join(POS_PHOTOS_COSTUMES_DIR, f"{_mco3.group(1)}{_ext}")
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
                                    _cand = os.path.join(POS_PHOTOS_ENVS_DIR, f"{_men3.group(1)}{_ext}")
                                    if os.path.isfile(_cand):
                                        _all_env_photo = _cand
                                        break

            gen_scene = {
                "prompt": gen_prompt,
                "duration": scene.get("duration", 8),
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

            try:
                clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                           progress_cb=on_progress, cost_cb=_record_cost,
                                           photo_path=scene_photo_path)
                scene["clip_path"] = clip_path
                scene["has_clip"] = True
                scene["gen_hash"] = _scene_gen_hash(scene)
            except Exception as e:
                on_progress(scene_idx, f"FAILED: {e}")
                scene["has_clip"] = False
                scene.pop("gen_hash", None)

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
        cached_count = 0
        for i, s in enumerate(plan["scenes"]):
            has_valid_clip = (s.get("has_clip") and s.get("clip_path")
                              and os.path.isfile(s.get("clip_path", "")))
            if has_valid_clip and s.get("gen_hash") == _scene_gen_hash(s):
                cached_count += 1
                continue  # clip exists and settings unchanged — skip
            scenes_to_gen.append((i, s))

        if cached_count:
            print(f"[batch_generate] Skipping {cached_count} cached scene(s)")

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
                                    _cand = os.path.join(POS_PHOTOS_COSTUMES_DIR, f"{_mbc.group(1)}{_ext}")
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
                                    _cand = os.path.join(POS_PHOTOS_ENVS_DIR, f"{_mbe.group(1)}{_ext}")
                                    if os.path.isfile(_cand):
                                        batch_env_photo = _cand
                                        break

            single_photo = scene.get("photo_path", "")
            if single_photo and os.path.isfile(single_photo):
                scene_photo_path = single_photo

            plan_continuity = plan.get("continuity_mode", True)
            gen_scene = {
                "prompt": gen_prompt,
                "duration": scene.get("duration", 8),
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
                scene["gen_hash"] = _scene_gen_hash(scene)
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
                    scene["gen_hash"] = _scene_gen_hash(scene)
                    return scene_id, True, None
                except Exception as e2:
                    scene.pop("gen_hash", None)
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


def _auto_resize_photo(photo_path: str, max_w: int = 4096, max_h: int = 4096) -> str:
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
            img.save(photo_path, quality=95)
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

        # Apply per-scene trim (in/out points) before stitching
        processed_clip_paths = list(clip_paths)
        for idx, s in enumerate(plan["scenes"]):
            trim_in = s.get("trim_in", 0)
            trim_out = s.get("trim_out", 0)
            if (trim_in > 0 or trim_out > 0) and idx < len(processed_clip_paths):
                cp = processed_clip_paths[idx]
                if cp and os.path.isfile(cp):
                    trimmed = os.path.join(MANUAL_CLIPS_DIR, f"_trim_{s.get('id', idx)}.mp4")
                    cmd = ["ffmpeg", "-y", "-i", cp]
                    if trim_in > 0:
                        cmd += ["-ss", str(trim_in)]
                    if trim_out > 0:
                        cmd += ["-to", str(trim_out)]
                    cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", trimmed]
                    try:
                        subprocess.run(cmd, capture_output=True, check=True,
                                       **({'creationflags': 0x08000000} if sys.platform == 'win32' else {}))
                        processed_clip_paths[idx] = trimmed
                        print(f"[STITCH] Trimmed scene {idx}: in={trim_in}s out={trim_out}s")
                    except Exception as e:
                        print(f"[STITCH] Trim failed for scene {idx}: {e}")

        # Apply per-scene effects before stitching
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
            dur = s.get("duration", 8)
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
            subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
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


def _resolve_sheet_or_photo(url_or_path):
    """Resolve a sheet URL or file path to a local file path."""
    if not url_or_path:
        return None
    if os.path.isfile(url_or_path):
        return url_or_path
    # Check if it's an API URL for sheets
    if url_or_path.startswith("/api/pos/sheets/"):
        filename = url_or_path.split("/api/pos/sheets/")[-1]
        sheets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "prompt_os", "sheets")
        candidate = os.path.join(sheets_dir, filename)
        if os.path.isfile(candidate):
            return candidate
    elif url_or_path.startswith("/api/pos/"):
        # Character/costume/env photo API URL
        import re as _re_resolve
        for entity_type, photo_dir in [("characters", POS_PHOTOS_CHARS_DIR), ("costumes", POS_PHOTOS_COSTUMES_DIR), ("environments", POS_PHOTOS_ENVS_DIR), ("props", POS_PHOTOS_PROPS_DIR)]:
            m = _re_resolve.search(rf"/api/pos/{entity_type}/([^/]+)/photo", url_or_path)
            if m:
                eid = m.group(1)
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    candidate = os.path.join(photo_dir, f"{eid}{ext}")
                    if os.path.isfile(candidate):
                        return candidate
    return None


# ---- AutoAgent eval function ----

def _autoagent_eval_fn():
    """Eval function for AutoAgent — generates test frames and measures success.

    Uses the current project's scenes as test cases.
    Returns an EvalBatch with results.
    """
    from lib.auto_agent import EvalBatch, EvalResult, HarnessManager
    from lib.video_generator import _runway_generate_scene_image

    harness = HarnessManager()
    batch = EvalBatch(harness.current.get("version", 0), harness.hash())

    # Load scenes from current plan
    plan = load_movie_plan(OUTPUT_DIR)
    if not plan or not plan.get("scenes"):
        # No plan — create a synthetic test batch
        batch.results.append(EvalResult(0, "medium", True, generation_time=0, prompt_used="synthetic"))
        batch.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        return batch

    scenes = plan["scenes"]
    # Test up to 5 scenes per eval (balance speed vs coverage)
    import random
    test_scenes = random.sample(range(len(scenes)), min(5, len(scenes)))

    for scene_idx in test_scenes:
        scene = scenes[scene_idx]
        shot_type = scene.get("shot_type", "medium")
        prompt = scene.get("shot_prompt", scene.get("prompt", "Cinematic scene"))

        # Build prompt using current harness
        h = harness.current
        framing = h.get("framing_prefix", {}).get(shot_type, "Cinematic shot.")
        quality = h.get("quality_suffix", "Photorealistic, 8K.")

        # Check for refs
        enriched = dict(scene)
        _enrich_scene_with_assets(enriched)

        char_photos = []
        char_photo = enriched.get("character_photo_path", "")
        if char_photo and os.path.isfile(char_photo):
            char_photos.append({"path": char_photo, "tag": "Character"})

        env_photos = []
        env_photo = enriched.get("environment_photo_path", "")
        if env_photo and os.path.isfile(env_photo):
            env_photos.append({"path": env_photo, "tag": "Setting"})

        # Collect refs
        refs = char_photos[:1] + env_photos[:1]  # Max 2 for speed

        has_char = len(char_photos) > 0
        has_env = len(env_photos) > 0

        # Build test prompt from harness
        identity = ""
        if has_char:
            identity = h.get("identity_strength", {}).get(shot_type, "")

        test_prompt = f"{framing} {identity} {prompt} {quality}"
        if h.get("negative_keywords"):
            test_prompt += f" AVOID: {h['negative_keywords']}"
        test_prompt = test_prompt[:h.get("max_prompt_length", 1000)]

        # Generate
        start_time = time.time()
        try:
            result_path = _runway_generate_scene_image(
                test_prompt, refs,
                ratio="1280:720",
                model="gen4_image_turbo",  # Use turbo for speed in evals
            )
            gen_time = time.time() - start_time

            if result_path and os.path.isfile(result_path):
                batch.results.append(EvalResult(
                    scene_idx, shot_type, True,
                    generation_time=gen_time,
                    prompt_used=test_prompt,
                ))
            else:
                batch.results.append(EvalResult(
                    scene_idx, shot_type, False,
                    error="No image returned",
                    generation_time=gen_time,
                    prompt_used=test_prompt,
                ))
        except Exception as e:
            gen_time = time.time() - start_time
            error_str = str(e)[:300]
            is_mod = any(kw in error_str.lower() for kw in ["moderation", "safety", "policy", "flagged"])
            batch.results.append(EvalResult(
                scene_idx, shot_type, False,
                error=error_str,
                moderation_blocked=is_mod,
                generation_time=gen_time,
                prompt_used=test_prompt,
            ))

    batch.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    return batch


# ---- Smart Engine Router ----

def _recommend_engine(scene: dict, project_default: str = "gen4_5") -> dict:
    """Recommend the best generation engine for a scene.

    Returns {engine, reason, confidence} where confidence is 0-1.

    Logic:
    - Default to project's chosen engine for consistency
    - Only recommend switching when a specific capability is needed
    - Close-up face shots -> Runway (best identity preservation)
    - Action/fast motion -> Veo 3.1 (best motion quality)
    - Wide cinematic -> Veo 3.1 (best environmental quality)
    - Dialogue scenes -> Gen 4.5 (best identity preservation during speech)
    """
    shot_type = scene.get("shot_type", "medium")
    has_dialogue = bool(scene.get("dialogue") or scene.get("has_dialogue"))
    has_action = any(w in (scene.get("action", "") or "").lower() for w in
                     ["fight", "run", "chase", "explod", "dance", "jump", "crash", "fast"])
    has_face = shot_type in ("close-up",)
    has_environment = shot_type in ("wide", "establishing")

    result = {
        "engine": project_default,
        "reason": "Project default for visual consistency",
        "confidence": 0.8,
        "switchRecommended": False,
    }

    # Only recommend switching for strong reasons
    if has_dialogue:
        result = {
            "engine": "gen4_5",
            "reason": "Dialogue scene — Gen 4.5 preserves identity during speech",
            "confidence": 0.7,
            "switchRecommended": True,
        }
    elif has_face and project_default not in ("gen4_5", "gen4_turbo"):
        result = {
            "engine": "gen4_5",
            "reason": "Close-up shot — Runway Gen 4.5 has best face/identity preservation",
            "confidence": 0.6,
            "switchRecommended": True,
        }
    elif has_action and project_default not in ("gen4_5", "veo3_1"):
        result = {
            "engine": "veo3_1",
            "reason": "Action scene — Veo 3.1 handles fast motion well",
            "confidence": 0.5,
            "switchRecommended": False,
        }
    elif has_environment and project_default not in ("veo3_1", "veo3"):
        result = {
            "engine": "veo3_1",
            "reason": "Wide/establishing shot — Veo 3.1 has best environmental detail",
            "confidence": 0.5,
            "switchRecommended": False,
        }

    return result


# ---- Rate limiter ----

import time as _time
_rate_limits = {}  # {ip: [timestamps]}
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX = {
    "generate": 10,      # 10 generations per minute
    "default": 60,       # 60 requests per minute
    "upload": 20,        # 20 uploads per minute
}

def _check_rate_limit(ip: str, category: str = "default") -> bool:
    """Returns True if request is allowed, False if rate limited."""
    now = _time.time()
    key = f"{ip}:{category}"

    if key not in _rate_limits:
        _rate_limits[key] = []

    # Clean old entries
    _rate_limits[key] = [t for t in _rate_limits[key] if now - t < _RATE_LIMIT_WINDOW]

    max_req = _RATE_LIMIT_MAX.get(category, _RATE_LIMIT_MAX["default"])

    if len(_rate_limits[key]) >= max_req:
        return False

    _rate_limits[key].append(now)
    return True


# ---- HTTP handler ----

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Quieter logging
        sys.stderr.write(f"[server] {fmt % args}\n")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        # Compress large responses if client accepts gzip
        use_gzip = False
        accept_encoding = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_encoding and len(body) > 1024:
            import gzip as _gzip
            body = _gzip.compress(body)
            use_gzip = True

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
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
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".svg": "image/svg+xml",
                ".webm": "video/webm",
                ".mov": "video/quicktime",
                ".zip": "application/zip",
            }.get(ext, "application/octet-stream")
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        # Cache control: HTML always fresh, static assets cached 1 hour
        if content_type.startswith("text/html"):
            self.send_header("Cache-Control", "no-cache")
        elif content_type.startswith(("image/", "text/css", "application/javascript")):
            self.send_header("Cache-Control", "public, max-age=3600")
        elif content_type.startswith("video/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            self._send_json({"status": "ok", "uptime": time.time() - _server_start_time})
        elif path == "/":
            self._send_file(os.path.join(PROJECT_DIR, "public", "index.html"))
        elif path == "/landing":
            self._send_file(os.path.join(PROJECT_DIR, "public", "landing.html"))
        elif path == "/tb-bear.png":
            self._send_file(os.path.join(PROJECT_DIR, "public", "tb-bear.png"))
        elif path in ("/bear-light.png", "/bear-dark.png", "/bg-light.png", "/bg-dark.png", "/logo.png", "/landing-light.png", "/landing-dark.png"):
            self._send_file(os.path.join(PROJECT_DIR, "public", path.lstrip("/")))
        elif path == "/manifesto":
            self._send_file(os.path.join(PROJECT_DIR, "public", "manifesto.html"))

        elif path.startswith("/public/"):
            rel = path[len("/public/"):]
            safe = os.path.normpath(rel)
            fpath = os.path.join(PROJECT_DIR, "public", safe)
            resolved = os.path.abspath(fpath)
            allowed_dir = os.path.abspath(os.path.join(PROJECT_DIR, "public"))
            if not resolved.startswith(allowed_dir + os.sep) and resolved != allowed_dir:
                self.send_error(403, "Access denied")
                return
            self._send_file(fpath)

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
                clip_file = os.path.join(OUTPUT_DIR, "auto_director", "clips", safe)
            if not os.path.isfile(clip_file):
                # Search in takes directories
                for scene_dir in os.listdir(TAKES_DIR) if os.path.isdir(TAKES_DIR) else []:
                    candidate = os.path.join(TAKES_DIR, scene_dir, safe)
                    if os.path.isfile(candidate):
                        clip_file = candidate
                        break
            self._send_file(clip_file)

        elif path.startswith("/api/performance-ref/"):
            filename = os.path.basename(path.split("/api/performance-ref/")[-1])
            perf_dir = os.path.join(OUTPUT_DIR, "performance_refs")
            fpath = os.path.join(perf_dir, filename)
            if os.path.isfile(fpath):
                self._send_file(fpath)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif path.startswith("/api/audio/generated/"):
            filename = path[len("/api/audio/generated/"):]
            safe = os.path.basename(urllib.parse.unquote(filename))
            self._send_file(os.path.join(AUDIO_GEN_DIR, safe))

        elif path.startswith("/api/audio/stems/"):
            filename = os.path.basename(path.split("/api/audio/stems/")[-1])
            stems_dir = os.path.join(OUTPUT_DIR, "stems")
            found = False
            for root, dirs, files in os.walk(stems_dir):
                if filename in files:
                    self._send_file(os.path.join(root, filename))
                    found = True
                    break
            if not found:
                self._send_json({"error": "Stem file not found"}, 404)

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
            fpath = os.path.join(OUTPUT_DIR, safe)
            resolved = os.path.abspath(fpath)
            allowed_dir = os.path.abspath(OUTPUT_DIR)
            if not resolved.startswith(allowed_dir + os.sep) and resolved != allowed_dir:
                self.send_error(403, "Access denied")
                return
            self._send_file(fpath)

        elif path == "/api/project/save":
            self._handle_project_save()

        elif path == "/api/cost":
            self._handle_get_cost()

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
            self._send_file(os.path.join(POS_PHOTOS_CHARS_DIR, m.group(1) + ".jpg"))

        elif re.match(r'^/api/pos/costumes/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)/photo$', path)
            self._send_file(os.path.join(POS_PHOTOS_COSTUMES_DIR, m.group(1) + ".jpg"))

        elif re.match(r'^/api/pos/environments/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)/photo$', path)
            self._send_file(os.path.join(POS_PHOTOS_ENVS_DIR, m.group(1) + ".jpg"))

        elif re.match(r'^/api/pos/characters/([^/]+)/preview$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)/preview$', path)
            cid = m.group(1)
            # Try sheet first, then regular preview
            sheet_path = os.path.join(POS_PREVIEWS_CHARS_DIR, f"{cid}_sheet.jpg")
            reg_path = os.path.join(POS_PREVIEWS_CHARS_DIR, f"{cid}.jpg")
            self._send_file(sheet_path if os.path.isfile(sheet_path) else reg_path)

        elif re.match(r'^/api/pos/costumes/([^/]+)/preview$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)/preview$', path)
            self._send_file(os.path.join(POS_PREVIEWS_COSTUMES_DIR, m.group(1) + ".jpg"))

        elif re.match(r'^/api/pos/environments/([^/]+)/preview$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)/preview$', path)
            self._send_file(os.path.join(POS_PREVIEWS_ENVS_DIR, m.group(1) + ".jpg"))

        elif path == "/api/pos/props":
            self._send_json({"props": _prompt_os.get_props()})

        elif re.match(r'^/api/pos/props/([^/]+)$', path):
            m = re.match(r'^/api/pos/props/([^/]+)$', path)
            rec = _prompt_os.get_prop(m.group(1))
            if rec:
                self._send_json(rec)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/props/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/props/([^/]+)/photo$', path)
            self._send_file(os.path.join(POS_PHOTOS_PROPS_DIR, m.group(1) + ".jpg"))

        elif path == "/api/pos/voices":
            self._send_json({"voices": _prompt_os.get_voices()})

        elif re.match(r'^/api/pos/voices/([^/]+)$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)$', path)
            rec = _prompt_os.get_voice(m.group(1))
            if rec:
                self._send_json(rec)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/voices/([^/]+)/sample$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)/sample$', path)
            audio_dir = os.path.join(PROMPT_OS_DATA_DIR, "audio", "voices")
            self._send_file(os.path.join(audio_dir, m.group(1) + ".mp3"))

        elif path == "/api/pos/scenes":
            self._send_json({"scenes": _prompt_os.get_scenes()})

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

        elif path == "/api/pos/style-locks":
            self._send_json({"styleLocks": _prompt_os.get_style_locks()})

        elif path == "/api/pos/world-rules":
            self._send_json({"worldRules": _prompt_os.get_world_rules()})

        elif path == "/api/pos/continuity-rules":
            self._send_json(_prompt_os.get_continuity_rules())

        elif path == "/api/pos/project-style":
            self._send_json(_prompt_os.get_project_style())

        elif re.match(r'^/api/pos/assets/([^/]+)/([^/]+)/readiness$', path):
            m = re.match(r'^/api/pos/assets/([^/]+)/([^/]+)/readiness$', path)
            self._send_json(_prompt_os.get_asset_readiness(m.group(1), m.group(2)))

        elif re.match(r'^/api/pos/missing-assets/(\d+)$', path):
            m = re.match(r'^/api/pos/missing-assets/(\d+)$', path)
            shot_index = int(m.group(1))
            missing = {"shotIndex": shot_index, "missing": []}
            if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
                with open(AUTO_DIRECTOR_PLAN_PATH, "r") as f:
                    plan = json.load(f)
                shots = plan.get("shots", plan.get("scenes", []))
                if 0 <= shot_index < len(shots):
                    shot = shots[shot_index]
                    # Check character readiness
                    for cid in shot.get("characterIds", []):
                        r = _prompt_os.get_asset_readiness("character", cid)
                        if not r.get("ready"):
                            missing["missing"].append({"type": "character", "id": cid, "readiness": r})
                    # Check costume readiness
                    for cid in shot.get("costumeIds", []):
                        r = _prompt_os.get_asset_readiness("costume", cid)
                        if not r.get("ready"):
                            missing["missing"].append({"type": "costume", "id": cid, "readiness": r})
                    # Check environment readiness
                    eid = shot.get("environmentId", "")
                    if eid:
                        r = _prompt_os.get_asset_readiness("environment", eid)
                        if not r.get("ready"):
                            missing["missing"].append({"type": "environment", "id": eid, "readiness": r})
                    # Check prop readiness
                    for pid in shot.get("propIds", []):
                        r = _prompt_os.get_asset_readiness("prop", pid)
                        if not r.get("ready"):
                            missing["missing"].append({"type": "prop", "id": pid, "readiness": r})
                else:
                    missing["error"] = f"Shot index {shot_index} out of range (total: {len(shots)})"
            else:
                missing["error"] = "No auto-director plan found"
            self._send_json(missing)

        elif path.startswith("/api/pos/sheets/"):
            filename = os.path.basename(path.split("/api/pos/sheets/")[-1])
            if not filename or '..' in filename or '/' in filename or '\\' in filename:
                self._send_json({"error": "Invalid filename"}, 400)
                return
            sheets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "prompt_os", "sheets")
            fpath = os.path.join(sheets_dir, filename)
            # Verify resolved path is inside sheets_dir
            if not os.path.abspath(fpath).startswith(os.path.abspath(sheets_dir)):
                self._send_json({"error": "Access denied"}, 403)
                return
            if os.path.isfile(fpath):
                self._send_file(fpath)
            else:
                self._send_json({"error": "Sheet not found"}, 404)

        # ──── Auto Director GET routes ────
        elif path == "/api/auto-director/status":
            self._send_json(_auto_director.progress)

        elif path == "/api/auto-director/plan":
            if os.path.isfile(AUTO_DIRECTOR_PLAN_PATH):
                with open(AUTO_DIRECTOR_PLAN_PATH, "r") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"scenes": []})

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
            from lib.cinematic_engine import CAMERA_PRESETS
            self._send_json({"presets": CAMERA_PRESETS})

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
                with open(plan_path, "r") as f:
                    self._send_json(json.load(f))
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

        # ──── Feature 1: Project Browser ────
        elif path == "/api/projects":
            projects = _project_mgr.list_projects()
            current = _project_mgr.get_current_project()
            self._send_json({"projects": projects, "current": current})

        elif path == "/api/projects/current":
            current = _project_mgr.get_current_project()
            self._send_json({"current": current})

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
            self._send_file(os.path.join(TAKES_DIR, safe))

        # Keyboard shortcuts reference
        elif path == "/api/keyboard-shortcuts":
            from lib.roadmap_features import KEYBOARD_SHORTCUTS
            self._send_json({"shortcuts": KEYBOARD_SHORTCUTS})

        # Analytics
        elif path == "/api/analytics":
            from lib.roadmap_features import get_analytics
            self._send_json(get_analytics(OUTPUT_DIR))

        # Version history
        elif path == "/api/versions":
            from lib.roadmap_features import list_versions
            self._send_json({"versions": list_versions(OUTPUT_DIR)})

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
                self.send_error(404)

        # ---- AutoAgent GET endpoints ----
        elif path == "/api/autoagent/status":
            run = get_current_run()
            if run:
                self._send_json(run.get_status())
            else:
                self._send_json({"status": "no_run", "message": "No optimization run started"})

        elif path == "/api/autoagent/harness":
            from lib.auto_agent import HarnessManager
            hm = HarnessManager()
            self._send_json({"harness": hm.current, "hash": hm.hash(), "best_score": hm.best_score})

        elif path == "/api/autoagent/history":
            from lib.auto_agent import AUTOAGENT_DIR
            history_path = os.path.join(AUTOAGENT_DIR, "harness_history.json")
            if os.path.isfile(history_path):
                with open(history_path, "r") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json([])

        # ---- Director Brain GET ----
        elif path == "/api/director-brain/status":
            brain = get_brain()
            self._send_json({
                "style_vector": brain.style_vector,
                "total_ratings": len(brain.ratings),
                "style_summary": brain.get_style_summary(),
            })

        # ---- Reference Demo Images (Lookbook) ----
        elif path == "/api/reference-demos":
            refs_dir = os.path.join(OUTPUT_DIR, "reference_demos")
            images = {}
            if os.path.isdir(refs_dir):
                for f in os.listdir(refs_dir):
                    if f.endswith(('.jpg', '.png')):
                        cat = f.split('_')[0]  # light, angle, shot, grade
                        if cat not in images:
                            images[cat] = []
                        name = f.replace(cat + '_', '').replace('.jpg', '').replace('.png', '').replace('_', ' ')
                        images[cat].append({
                            "name": name,
                            "url": f"/output/reference_demos/{f}",
                            "filename": f,
                        })
            self._send_json(images)

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
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

        elif path == "/api/manual/scene":
            self._handle_manual_create_scene()

        elif re.match(r'^/api/manual/scene/([^/]+)/photo$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/photo$', path)
            self._handle_manual_upload_photo(m.group(1))

        elif re.match(r'^/api/manual/scene/([^/]+)/duplicate$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/duplicate$', path)
            self._handle_manual_duplicate_scene(m.group(1))

        elif re.match(r'^/api/manual/scene/([^/]+)/trim$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/trim$', path)
            self._handle_manual_trim_scene(m.group(1))

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

        elif path == "/api/project/load":
            self._handle_project_load()

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

        # Item 34: Export best GIFs
        elif path == "/api/export-best-gifs":
            self._handle_export_best_gifs()

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

        # Roadmap: QR code
        elif path == "/api/qr-code":
            self._handle_qr_code()

        # Roadmap: Version history save
        elif path == "/api/versions/save":
            self._handle_save_version()

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

        # Roadmap: Storyboard PDF
        elif path == "/api/storyboard-pdf":
            self._handle_storyboard_pdf()

        # Roadmap: Embed code
        elif path == "/api/embed-code":
            self._handle_embed_code()

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

        elif path == "/api/auto-director/restitch":
            self._handle_auto_director_restitch()

        elif path == "/api/auto-director/stitch":
            # Alias for restitch — frontend uses both
            self._handle_auto_director_restitch()

        elif path == "/api/auto-director/to-manual":
            self._handle_auto_director_to_manual()

        # ──── Movie Planner POST routes ────
        elif path == "/api/auto-director/movie-plan":
            self._handle_movie_plan()

        elif re.match(r'^/api/auto-director/scene/(\d+)/edit$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/edit$', path)
            self._handle_movie_scene_edit(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/assets$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/assets$', path)
            self._handle_scene_assets(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/validate-assets$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/validate-assets$', path)
            self._handle_scene_validate_assets(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/first-frame$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/first-frame$', path)
            self._handle_generate_first_frame(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/generate-clip$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/generate-clip$', path)
            self._handle_generate_scene_clip(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/preview$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/preview$', path)
            self._handle_scene_preview(int(m.group(1)))

        elif path == "/api/auto-director/scenes/preview-batch":
            self._handle_scenes_preview_batch()

        elif re.match(r'^/api/auto-director/scene/(\d+)/restyle$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/restyle$', path)
            self._handle_scene_restyle(int(m.group(1)), body)

        elif re.match(r'^/api/auto-director/scene/(\d+)/multi-angle$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/multi-angle$', path)
            self._handle_multi_angle_clip(int(m.group(1)), body)

        elif re.match(r'^/api/auto-director/scene/(\d+)/character-performance$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/character-performance$', path)
            body = json.loads(self._read_body())
            self._handle_character_performance(int(m.group(1)), body)

        elif path == "/api/performance-ref/upload":
            # Handle multipart upload of performance reference video
            try:
                ct = self.headers.get("Content-Type", "")
                boundary = ct.split("boundary=")[-1].encode()
                body_bytes = self._read_body()
                parts = self._parse_multipart(body_bytes, boundary)
                file_part = None
                for p in parts:
                    if p.get("filename"):
                        file_part = p
                        break
                if not file_part:
                    self._send_json({"error": "No file uploaded"}, 400)
                    return
                perf_dir = os.path.join(OUTPUT_DIR, "performance_refs")
                os.makedirs(perf_dir, exist_ok=True)
                filename = f"perf_ref_{int(time.time())}.mp4"
                filepath = os.path.join(perf_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(file_part["data"])
                self._send_json({"ok": True, "path": filepath, "url": f"/api/performance-ref/{filename}"})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif re.match(r'^/api/auto-director/scene/(\d+)/upload-clip$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/upload-clip$', path)
            self._handle_scene_upload_clip(int(m.group(1)))

        elif re.match(r'^/api/auto-director/scene/(\d+)/upload-frame$', path):
            m = re.match(r'^/api/auto-director/scene/(\d+)/upload-frame$', path)
            self._handle_scene_upload_frame(int(m.group(1)))

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

        elif path == "/api/auto-director/reorder":
            body = json.loads(self._read_body())
            order = body.get("order", [])
            plan = load_movie_plan(OUTPUT_DIR)
            if plan and plan.get("scenes") and order:
                id_to_scene = {s["id"]: s for s in plan["scenes"]}
                reordered = [id_to_scene[sid] for sid in order if sid in id_to_scene]
                for i, s in enumerate(reordered):
                    s["order"] = i
                plan["scenes"] = reordered
                save_movie_plan(plan, OUTPUT_DIR)
                # Also save to auto_director_plan
                ad_path = os.path.join(OUTPUT_DIR, "auto_director_plan.json")
                with open(ad_path, "w", encoding="utf-8") as f:
                    json.dump(plan, f, indent=2, ensure_ascii=False)
            self._send_json({"ok": True})

        elif path == "/api/auto-director/draft-assets/promote":
            self._handle_draft_promote()

        elif path == "/api/auto-director/draft-assets/resolve":
            self._handle_draft_resolve()

        elif path == "/api/auto-director/draft-assets/remove":
            self._handle_draft_remove()

        elif path == "/api/workflow-presets":
            body = json.loads(self._read_body())
            preset = save_custom_preset(body)
            self._send_json({"ok": True, "preset": preset})

        # ──── Beat Sync ────
        elif path == "/api/cinematic/beat-sync/analyze":
            from lib.beat_sync import generate_beat_sync_plan
            from lib.audio_analyzer import analyze
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
            shot = body.get("shot", {})
            prev_shot = body.get("prev_shot")
            result = score_shot(shot, prev_shot)
            self._send_json(result)

        elif path == "/api/cinematic/coherence/scene":
            from lib.coherence_scorer import score_scene
            body = json.loads(self._read_body())
            scene_id = body.get("scene_id", "")
            settings = _load_settings()
            shots = settings.get("shots_data", {}).get(scene_id, [])
            scene_data = _prompt_os.get_scene(scene_id) if scene_id else None
            result = score_scene(shots, scene_data)
            self._send_json(result)

        # ──── Coverage System ────
        elif path == "/api/cinematic/coverage/generate":
            from lib.coverage_system import generate_coverage
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
            remove_custom_preset(body.get("category", ""), body.get("name", ""))
            self._send_json({"ok": True})

        elif path == "/api/cinematic/shot-styles/resolve":
            from lib.shot_style_library import resolve_presets
            body = json.loads(self._read_body())
            prompt = resolve_presets(body.get("selections", {}))
            self._send_json({"ok": True, "prompt": prompt})

        # ──── Generation Queue ────
        elif path == "/api/queue/add-shot":
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
            sm = StyleMemory()
            result = sm.update(body)
            self._send_json({"ok": True, "style_memory": result})

        elif path == "/api/cinematic/style-memory/from-vision":
            from lib.cinematic_engine import StyleMemory
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
            rec = _prompt_os.create_prompt(body)
            self._send_json({"ok": True, "prompt": rec})

        elif path == "/api/pos/characters":
            body = json.loads(self._read_body())
            rec = _prompt_os.create_character(body)
            self._send_json({"ok": True, "character": rec})

        elif path == "/api/pos/costumes":
            body = json.loads(self._read_body())
            rec = _prompt_os.create_costume(body)
            self._send_json({"ok": True, "costume": rec})

        elif path == "/api/pos/environments":
            body = json.loads(self._read_body())
            rec = _prompt_os.create_environment(body)
            self._send_json({"ok": True, "environment": rec})

        elif path == "/api/pos/props":
            body = json.loads(self._read_body())
            rec = _prompt_os.create_prop(body)
            self._send_json({"ok": True, "prop": rec})

        elif path == "/api/pos/voices":
            body = json.loads(self._read_body())
            rec = _prompt_os.create_voice(body)
            self._send_json({"ok": True, "voice": rec})

        elif path == "/api/pos/scenes":
            body = json.loads(self._read_body())
            rec = _prompt_os.create_scene(body)
            self._send_json({"ok": True, "scene": rec})

        elif re.match(r'^/api/pos/scenes/([^/]+)/export/text$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)/export/text$', path)
            text = _prompt_os.export_scene_text(m.group(1))
            self._send_json({"ok": True, "text": text})

        elif re.match(r'^/api/pos/scenes/([^/]+)/export/json$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)/export/json$', path)
            data = _prompt_os.export_scene_json(m.group(1))
            self._send_json({"ok": True, "data": data})

        elif path == "/api/pos/style-locks":
            body = json.loads(self._read_body())
            locks = _prompt_os.set_style_locks(body.get("styleLocks", []))
            self._send_json({"ok": True, "styleLocks": locks})

        elif path == "/api/pos/world-rules":
            body = json.loads(self._read_body())
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

        elif re.match(r'^/api/pos/props/([^/]+)/photo$', path):
            m = re.match(r'^/api/pos/props/([^/]+)/photo$', path)
            self._handle_pos_photo_upload(m.group(1), "props")

        elif re.match(r'^/api/pos/voices/([^/]+)/sample$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)/sample$', path)
            self._handle_voice_sample_upload(m.group(1))

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

        # ──── Asset Sheet Generation System ────
        elif path == "/api/pos/project-style":
            body = json.loads(self._read_body())
            self._send_json(_prompt_os.set_project_style(body))

        elif path == "/api/pos/sheets/generate":
            body = json.loads(self._read_body())
            self._handle_pos_generate_sheet(body)

        elif path == "/api/pos/sheets/approve":
            body = json.loads(self._read_body())
            asset_type = body.get("assetType", "")
            asset_id = body.get("assetId", "")
            sheet_url = body.get("sheetUrl", "")
            slot = body.get("slot", "approvedSheet")
            result = _prompt_os.approve_sheet(asset_type, asset_id, sheet_url, slot)
            if isinstance(result, dict) and "error" in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)

        elif path == "/api/pos/assets/lock":
            body = json.loads(self._read_body())
            asset_type = body.get("assetType", "")
            asset_id = body.get("assetId", "")
            result = _prompt_os.lock_asset(asset_type, asset_id)
            if isinstance(result, dict) and "error" in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)

        elif path == "/api/pos/reference-package":
            body = json.loads(self._read_body())
            shot_type = body.get("shotType", "medium")
            character_id = body.get("characterId", "")
            costume_id = body.get("costumeId", "")
            environment_id = body.get("environmentId", "")
            prop_ids = body.get("propIds", [])

            character = _prompt_os.get_character(character_id) if character_id else None
            costume = _prompt_os.get_costume(costume_id) if costume_id else None
            environment = _prompt_os.get_environment(environment_id) if environment_id else None
            props = [_prompt_os.get_prop(pid) for pid in prop_ids if _prompt_os.get_prop(pid)]

            package = build_reference_package(shot_type, character, costume, environment, props)
            best_refs = select_best_refs_for_shot(package, shot_type)
            package["selectedRefs"] = best_refs
            self._send_json(package)

        # ──── Feature 1: Project Browser ────
        elif path == "/api/projects":
            body = json.loads(self._read_body())
            name = body.get("name", "Untitled Project")
            meta = _project_mgr.create_project(name)
            self._send_json({"ok": True, "project": meta})

        elif re.match(r'^/api/projects/([^/]+)/load$', path):
            m = re.match(r'^/api/projects/([^/]+)/load$', path)
            meta = _project_mgr.load_project(m.group(1))
            if meta:
                self._send_json({"ok": True, "project": meta})
            else:
                self._send_json({"error": "Project not found"}, 404)

        elif re.match(r'^/api/projects/([^/]+)/save$', path):
            _project_mgr.save_current()
            self._send_json({"ok": True})

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
            body = json.loads(self._read_body())
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
            body = json.loads(self._read_body())
            budget = float(body.get("budget", DEFAULT_BUDGET))
            tracker = _load_cost_tracker()
            tracker["budget"] = budget
            _save_cost_tracker(tracker)
            self._send_json({"ok": True, "budget": budget})

        # Style mixing
        elif path == "/api/mix-styles":
            body = json.loads(self._read_body())
            from lib.prompt_assistant import mix_styles, STYLE_PRESETS
            sa = body.get("style_a", "")
            sb = body.get("style_b", "")
            w = float(body.get("weight", 0.5))
            if sa in STYLE_PRESETS: sa = STYLE_PRESETS[sa]
            if sb in STYLE_PRESETS: sb = STYLE_PRESETS[sb]
            self._send_json({"ok": True, "mixed_style": mix_styles(sa, sb, w)})

        # Emotion detection
        elif path == "/api/detect-emotion":
            body = json.loads(self._read_body())
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

        elif path == "/api/generate-sound":
            self._handle_generate_sound()

        elif path == "/api/generate-tts":
            self._handle_generate_tts()

        elif path == "/api/generate-speech-to-speech":
            self._handle_generate_sts()

        elif path == "/api/generate-voice-dubbing":
            self._handle_generate_voice_dubbing()

        elif path == "/api/generate-voice-isolation":
            self._handle_generate_voice_isolation()

        elif path == "/api/pos/continuity-rules":
            body = json.loads(self._read_body())
            rule_text = body.get("rule", "").strip()
            if not rule_text:
                self._send_json({"error": "rule is required"}, 400)
            else:
                rules = _prompt_os.get_continuity_rules()
                rules.append({"rule": rule_text})
                _prompt_os.set_continuity_rules(rules)
                self._send_json({"ok": True})

        elif path == "/api/ai-autofill":
            self._handle_ai_autofill()

        elif path == "/api/voice-clone/generate":
            self._handle_voice_clone()

        # ---- AutoAgent POST endpoints ----
        elif path == "/api/autoagent/start":
            body = json.loads(self._read_body())
            max_iter = body.get("maxIterations", 20)
            run = get_or_create_run()
            result = run.start(_autoagent_eval_fn, max_iterations=max_iter)
            self._send_json(result)

        elif path == "/api/autoagent/stop":
            run = get_current_run()
            if run:
                self._send_json(run.stop())
            else:
                self._send_json({"error": "No run active"})

        elif path == "/api/autoagent/harness/edit":
            body = json.loads(self._read_body())
            from lib.auto_agent import HarnessManager
            hm = HarnessManager()
            key_path = body.get("keyPath", "")
            new_value = body.get("newValue", "")
            success = hm.apply_edit(key_path, new_value)
            self._send_json({"ok": success, "harness": hm.current})

        elif path == "/api/autoagent/harness/revert":
            from lib.auto_agent import HarnessManager
            hm = HarnessManager()
            score = hm.revert_to_best()
            self._send_json({"ok": True, "reverted_to_score": score, "harness": hm.current})

        elif path == "/api/recommend-engine":
            body = json.loads(self._read_body())
            scenes = body.get("scenes", [])
            project_default = body.get("projectDefault", "gen4_5")
            recommendations = []
            for scene in scenes:
                rec = _recommend_engine(scene, project_default)
                recommendations.append(rec)
            self._send_json({"recommendations": recommendations})

        elif path == "/api/audio/stems":
            body = json.loads(self._read_body())
            self._handle_audio_stems(body)

        # ---- Director Brain POST endpoints ----
        elif path == "/api/director-brain/rate":
            body = json.loads(self._read_body())
            brain = get_brain()
            scene_index = body.get("sceneIndex", 0)
            rating = body.get("rating", 3)
            scene_data = body.get("sceneData", {})
            result = brain.rate_scene(scene_index, rating, scene_data)
            self._send_json({"ok": True, "rating": result, "style_summary": brain.get_style_summary()})

        elif path == "/api/director-brain/recommend":
            body = json.loads(self._read_body())
            brain = get_brain()
            scene_data = body.get("sceneData", {})
            recs = brain.recommend_for_scene(scene_data)
            self._send_json(recs)

        elif path == "/api/director-brain/analyze":
            body = json.loads(self._read_body())
            brain = get_brain()
            scene_data = body.get("sceneData", {})
            analysis = brain.analyze_success_factors(scene_data)
            self._send_json(analysis)

        else:
            self.send_error(404)

    def _handle_audio_stems(self, body):
        """Separate audio into stems (drums, bass, vocals, other).
        Uses Demucs if available, falls back to FFT-based estimation."""
        audio_path = body.get("audioPath", "")

        if not audio_path:
            plan = load_movie_plan(OUTPUT_DIR)
            if plan:
                audio_path = plan.get("song_path", "")

        if not audio_path or not os.path.isfile(audio_path):
            self._send_json({"error": "No audio file found"}, 400)
            return

        stems_dir = os.path.join(OUTPUT_DIR, "stems")
        os.makedirs(stems_dir, exist_ok=True)

        try:
            # Try Demucs first
            try:
                result = subprocess.run(
                    ["demucs", "--two-stems", "vocals", "-o", stems_dir, audio_path],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0:
                    base = os.path.splitext(os.path.basename(audio_path))[0]
                    vocals_path = os.path.join(stems_dir, "htdemucs", base, "vocals.wav")
                    instrumental_path = os.path.join(stems_dir, "htdemucs", base, "no_vocals.wav")

                    stems = {
                        "vocals": vocals_path if os.path.isfile(vocals_path) else None,
                        "instrumental": instrumental_path if os.path.isfile(instrumental_path) else None,
                    }

                    self._send_json({
                        "ok": True,
                        "method": "demucs",
                        "stems": {k: f"/api/audio/stems/{os.path.basename(v)}" if v else None for k, v in stems.items()},
                    })
                    return
            except (FileNotFoundError, subprocess.TimeoutExpired):
                print("[STEMS] Demucs not available, using FFT estimation")

            # Fallback: FFT-based stem energy estimation
            analysis = analyze(audio_path)

            beats = analysis.get("beats", [])
            bpm = analysis.get("bpm", 120)

            self._send_json({
                "ok": True,
                "method": "estimation",
                "stems": None,
                "beats": beats[:100],
                "bpm": bpm,
                "message": "Demucs not installed — using energy estimation. Install Demucs for full stem separation: pip install demucs",
            })

        except Exception as e:
            self._send_json({"error": f"Stem separation failed: {str(e)[:200]}"}, 500)

    def _handle_ai_autofill(self):
        """POST /api/ai-autofill -- AI auto-fill form fields from a simple user prompt."""
        try:
            body = json.loads(self._read_body())
        except Exception:
            self._send_json({"error": "Invalid JSON body"}, 400)
            return

        user_idea = body.get("userIdea", "")
        form_type = body.get("formType", "")
        field_keys = body.get("fieldKeys", [])
        system_prompt = body.get("prompt", "")

        if not user_idea:
            self._send_json({"error": "No user idea provided"}, 400)
            return

        # Build the LLM prompt
        field_list = ", ".join(field_keys)
        llm_prompt = f"""{system_prompt}

User's concept: "{user_idea}"

Generate detailed, cinematic, production-quality content for each field.
Be specific about visual details — colors, textures, materials, lighting, dimensions.
Optimize text for AI image generation prompts.

Return ONLY valid JSON with these exact keys: {field_list}
Each value should be a descriptive string (not nested objects).
Do not include any explanation, just the JSON object."""

        # Try OpenAI first, then Anthropic
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

        result = None

        if openai_key:
            try:
                import requests
                resp = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": "You are a cinematic production assistant. Output only valid JSON."},
                            {"role": "user", "content": llm_prompt}
                        ],
                        "temperature": 0.8,
                        "max_tokens": 1500,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"].strip()
                    # Strip markdown code fences if present
                    if content.startswith("```"):
                        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                    result = json.loads(content)
            except Exception as e:
                print(f"[AI-AutoFill] OpenAI error: {e}")

        if not result and anthropic_key:
            try:
                import requests
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": anthropic_key,
                        "Content-Type": "application/json",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 1500,
                        "messages": [{"role": "user", "content": llm_prompt}],
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["content"][0]["text"].strip()
                    if content.startswith("```"):
                        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                    result = json.loads(content)
            except Exception as e:
                print(f"[AI-AutoFill] Anthropic error: {e}")

        if result:
            self._send_json({"fields": result})
        else:
            self._send_json({"error": "No AI provider available. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env"}, 500)

    def _handle_voice_clone(self):
        """POST /api/voice-clone/generate -- Generate voice clone via ElevenLabs API."""
        elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not elevenlabs_key:
            self._send_json({"error": "ElevenLabs API key not set. Add ELEVENLABS_API_KEY to your .env file."}, 400)
            return

        # Frontend sends FormData, so parse multipart
        content_type = self.headers.get("Content-Type", "")
        text = ""
        voice_name = "Custom Voice"

        if "multipart/form-data" in content_type:
            parts_ct = content_type.split("boundary=")
            if len(parts_ct) < 2:
                self._send_json({"error": "Missing multipart boundary"}, 400)
                return
            boundary = parts_ct[1].strip().encode()
            raw_body = self._read_body()
            parts = self._parse_multipart(raw_body, boundary)
            for part in parts:
                if part["name"] == "text":
                    text = part["data"].decode("utf-8", errors="replace").strip()
                elif part["name"] == "voiceName":
                    voice_name = part["data"].decode("utf-8", errors="replace").strip()
                # voiceSample file ignored for now -- will be used for actual cloning later
        else:
            # Fallback: try JSON body
            try:
                body = json.loads(self._read_body())
                text = body.get("text", "")
                voice_name = body.get("voiceName", "Custom Voice")
            except Exception:
                self._send_json({"error": "Invalid request body"}, 400)
                return

        if not text:
            self._send_json({"error": "No text provided"}, 400)
            return

        try:
            import requests
            # Use ElevenLabs text-to-speech with a default voice
            # (full voice cloning requires their voice-lab/add endpoint which needs multipart)
            # For now, use their high-quality preset voices
            voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel - default ElevenLabs voice

            resp = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": elevenlabs_key,
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                    }
                },
                timeout=60,
            )

            if resp.status_code == 200:
                # Save audio file
                audio_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "audio")
                os.makedirs(audio_dir, exist_ok=True)
                filename = f"voice_clone_{voice_name.replace(' ', '_')}_{int(time.time())}.mp3"
                filepath = os.path.join(audio_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(resp.content)

                self._send_json({
                    "audioUrl": f"/api/audio/generated/{filename}",
                    "filename": filename,
                    "voiceName": voice_name,
                })
            else:
                error_msg = resp.text[:200]
                self._send_json({"error": f"ElevenLabs error ({resp.status_code}): {error_msg}"}, resp.status_code)

        except Exception as e:
            self._send_json({"error": f"Voice clone error: {str(e)}"}, 500)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if re.match(r'^/api/manual/scene/([^/]+)$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)$', path)
            self._handle_manual_update_scene(m.group(1))

        # ──── Prompt OS PUT routes ────
        elif re.match(r'^/api/pos/prompts/([^/]+)$', path):
            m = re.match(r'^/api/pos/prompts/([^/]+)$', path)
            body = json.loads(self._read_body())
            rec = _prompt_os.update_prompt(m.group(1), body)
            if rec and "error" in rec:
                self._send_json(rec, 403)
            elif rec:
                self._send_json({"ok": True, "prompt": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/characters/([^/]+)$', path):
            m = re.match(r'^/api/pos/characters/([^/]+)$', path)
            body = json.loads(self._read_body())
            rec = _prompt_os.update_character(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "character": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/costumes/([^/]+)$', path):
            m = re.match(r'^/api/pos/costumes/([^/]+)$', path)
            body = json.loads(self._read_body())
            rec = _prompt_os.update_costume(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "costume": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/environments/([^/]+)$', path):
            m = re.match(r'^/api/pos/environments/([^/]+)$', path)
            body = json.loads(self._read_body())
            rec = _prompt_os.update_environment(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "environment": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/props/([^/]+)$', path):
            m = re.match(r'^/api/pos/props/([^/]+)$', path)
            body = json.loads(self._read_body())
            rec = _prompt_os.update_prop(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "prop": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/voices/([^/]+)$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)$', path)
            body = json.loads(self._read_body())
            rec = _prompt_os.update_voice(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "voice": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/scenes/([^/]+)$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)$', path)
            body = json.loads(self._read_body())
            rec = _prompt_os.update_scene(m.group(1), body)
            if rec:
                self._send_json({"ok": True, "scene": rec})
            else:
                self._send_json({"error": "Not found"}, 404)

        else:
            self.send_error(404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

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

        elif re.match(r'^/api/pos/props/([^/]+)$', path):
            m = re.match(r'^/api/pos/props/([^/]+)$', path)
            if _prompt_os.delete_prop(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/voices/([^/]+)$', path):
            m = re.match(r'^/api/pos/voices/([^/]+)$', path)
            if _prompt_os.delete_voice(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/scenes/([^/]+)$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)$', path)
            if _prompt_os.delete_scene(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        # ──── Delete project template ────
        elif re.match(r'^/api/templates/([^/]+)$', path):
            self._handle_delete_template()

        # ──── Feature 1: Delete Project ────
        elif re.match(r'^/api/projects/([^/]+)$', path):
            m = re.match(r'^/api/projects/([^/]+)$', path)
            if _project_mgr.delete_project(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        elif re.match(r'^/api/pos/continuity-rules/(\d+)$', path):
            m = re.match(r'^/api/pos/continuity-rules/(\d+)$', path)
            idx = int(m.group(1))
            rules = _prompt_os.get_continuity_rules()
            if 0 <= idx < len(rules):
                rules.pop(idx)
                _prompt_os.set_continuity_rules(rules)
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Index out of range"}, 404)

        # ──── Suno Integration ────
        elif path == "/api/suno/import":
            body = json.loads(self._read_body())
            self._handle_suno_import(body)

        elif path == "/api/suno/generate":
            body = json.loads(self._read_body())
            self._handle_suno_generate(body)

        # ──── Auto-Captions / Transcription ────
        elif path == "/api/transcribe":
            body = json.loads(self._read_body())
            self._handle_transcribe(body)

        # ──── Album Art Generator ────
        elif path == "/api/generate-album-art":
            body = json.loads(self._read_body())
            self._handle_generate_album_art(body)

        # ──── Reference Demo Image Generation (Lookbook) ────
        elif path == "/api/generate-reference-images":
            self._handle_generate_reference_images()

        else:
            self.send_error(404)

    # ---- Suno handlers ----

    def _handle_suno_import(self, body):
        """Import audio from a Suno URL."""
        url = body.get("url", "").strip()
        if not url:
            self._send_json({"error": "No URL provided"}, 400)
            return

        # Suno URLs look like: https://suno.com/song/UUID or https://cdn.suno.ai/UUID.mp3
        # Extract the audio URL
        try:
            audio_url = None
            song_id = ""

            import re as _re_suno
            # Extract song ID from various Suno URL formats
            m = _re_suno.search(r'suno\.com/song/([a-f0-9-]+)', url)
            if m:
                song_id = m.group(1)
                audio_url = f"https://cdn1.suno.ai/{song_id}.mp3"

            m2 = _re_suno.search(r'cdn\d?\.suno\.ai/([a-f0-9-]+)\.mp3', url)
            if m2:
                song_id = m2.group(1)
                audio_url = url

            if not audio_url:
                # Try treating the whole URL as a direct audio link
                if url.endswith('.mp3') or url.endswith('.wav'):
                    audio_url = url
                else:
                    self._send_json({"error": "Could not parse Suno URL. Try: https://suno.com/song/ID or paste the direct audio link"}, 400)
                    return

            # Download the audio
            import requests
            print(f"[SUNO] Downloading from: {audio_url}")
            resp = requests.get(audio_url, timeout=60, stream=True)
            if resp.status_code != 200:
                self._send_json({"error": f"Failed to download audio (HTTP {resp.status_code})"}, 400)
                return

            # Save to uploads directory
            upload_dir = os.path.join(OUTPUT_DIR, "uploads")
            os.makedirs(upload_dir, exist_ok=True)
            filename = f"suno_{song_id or int(time.time())}.mp3"
            filepath = os.path.join(upload_dir, filename)

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)

            file_size = os.path.getsize(filepath)
            print(f"[SUNO] Downloaded: {filepath} ({file_size/1024:.0f}KB)")

            self._send_json({
                "ok": True,
                "filename": filename,
                "filepath": filepath,
                "size": file_size,
                "source": "suno",
                "songId": song_id,
            })

        except Exception as e:
            self._send_json({"error": f"Suno import failed: {str(e)[:200]}"}, 500)

    def _handle_suno_generate(self, body):
        """Generate music via Suno API (or compatible music AI).
        Falls back to instructions if no API key available."""
        prompt = body.get("prompt", "")
        style = body.get("style", "")
        duration = body.get("duration", 60)

        if not prompt:
            self._send_json({"error": "Describe the music you want"}, 400)
            return

        suno_key = os.environ.get("SUNO_API_KEY", "")

        if suno_key:
            # TODO: Call Suno API when available
            # For now, return instructions
            pass

        # No API key -- provide instructions
        self._send_json({
            "ok": False,
            "noApiKey": True,
            "instructions": {
                "step1": "Go to suno.com and create an account (free tier available)",
                "step2": f"Generate a song with this prompt: \"{prompt}\"",
                "step3": "Copy the song URL from your browser",
                "step4": "Paste it in the 'Import from Suno' field below",
            },
            "suggestedPrompt": f"{style + '. ' if style else ''}{prompt}",
        })

    # ---- Transcription handler ----

    def _handle_transcribe(self, body):
        """Transcribe audio to text with timestamps using OpenAI Whisper."""
        audio_path = body.get("audioPath", "")

        # Find the audio file
        if not audio_path:
            # Try current project's song
            plan = load_movie_plan(OUTPUT_DIR)
            if plan:
                audio_path = plan.get("song_path", "")

        if not audio_path or not os.path.isfile(audio_path):
            # Check uploads directory
            upload_dir = os.path.join(OUTPUT_DIR, "uploads")
            if os.path.isdir(upload_dir):
                for f in os.listdir(upload_dir):
                    if f.endswith(('.mp3', '.wav', '.m4a')):
                        audio_path = os.path.join(upload_dir, f)
                        break

        if not audio_path or not os.path.isfile(audio_path):
            self._send_json({"error": "No audio file found. Upload a song first."}, 400)
            return

        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if not openai_key:
            self._send_json({"error": "OPENAI_API_KEY not set. Add it to .env for transcription."}, 400)
            return

        try:
            import requests as _req

            # Call Whisper API
            with open(audio_path, "rb") as f:
                resp = _req.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                    data={
                        "model": "whisper-1",
                        "response_format": "verbose_json",
                        "timestamp_granularities[]": "word",
                    },
                    timeout=120,
                )

            if resp.status_code != 200:
                self._send_json({"error": f"Whisper API error ({resp.status_code}): {resp.text[:200]}"}, resp.status_code)
                return

            data = resp.json()

            # Extract word-level timestamps
            words = []
            for w in data.get("words", []):
                words.append({
                    "word": w.get("word", ""),
                    "start": w.get("start", 0),
                    "end": w.get("end", 0),
                })

            # Group into caption segments (roughly 5-8 words per segment)
            segments = []
            current_segment = {"text": "", "start": 0, "end": 0, "words": []}
            word_count = 0

            for w in words:
                if word_count == 0:
                    current_segment["start"] = w["start"]

                current_segment["text"] += (" " if current_segment["text"] else "") + w["word"]
                current_segment["end"] = w["end"]
                current_segment["words"].append(w)
                word_count += 1

                # Break segment at 6 words or punctuation
                if word_count >= 6 or w["word"].rstrip().endswith(('.', '!', '?', ',')):
                    segments.append(current_segment)
                    current_segment = {"text": "", "start": 0, "end": 0, "words": []}
                    word_count = 0

            if current_segment["text"]:
                segments.append(current_segment)

            # Save transcription
            trans_path = os.path.join(OUTPUT_DIR, "transcription.json")
            with open(trans_path, "w", encoding="utf-8") as f:
                json.dump({
                    "text": data.get("text", ""),
                    "language": data.get("language", ""),
                    "duration": data.get("duration", 0),
                    "segments": segments,
                    "words": words,
                }, f, indent=2, ensure_ascii=False)

            self._send_json({
                "ok": True,
                "text": data.get("text", ""),
                "language": data.get("language", ""),
                "segmentCount": len(segments),
                "wordCount": len(words),
                "segments": segments,
            })

        except Exception as e:
            self._send_json({"error": f"Transcription failed: {str(e)[:200]}"}, 500)

    # ---- Album Art handler ----

    def _handle_generate_album_art(self, body):
        """Generate album cover art matched to the project's visual style."""
        title = body.get("title", "Untitled")
        artist = body.get("artist", "")
        style = body.get("style", "cinematic")
        size = body.get("size", "1:1")  # 1:1 for album, 9:16 for Spotify Canvas

        # Load project style for visual consistency
        project_style = _prompt_os.get_project_style()

        # Build prompt
        parts = []

        # Style from project
        if project_style:
            if project_style.get("visualLanguage"):
                parts.append(project_style["visualLanguage"])
            if project_style.get("colorPalette"):
                parts.append(f"Color palette: {project_style['colorPalette']}")
            if project_style.get("tone"):
                parts.append(f"Mood: {project_style['tone']}")

        # Album art specific
        if size == "9:16":
            parts.append(f"Spotify Canvas vertical loop visual for '{title}' by {artist}.")
            parts.append("Abstract, mesmerizing, loop-friendly motion concept.")
        else:
            parts.append(f"Album cover art for '{title}' by {artist}.")
            parts.append("Professional album artwork, striking composition, music industry quality.")

        parts.append(f"Style: {style}.")
        parts.append("High contrast, eye-catching, gallery quality. No text or typography in the image.")

        prompt = " ".join(parts)[:1000]

        # Get character/environment refs for visual consistency
        refs = []
        chars = _prompt_os.get_characters()
        if chars:
            # Use the first character's photo as style reference
            char = chars[0]
            ref_img = char.get("referencePhoto") or char.get("referenceImagePath", "")
            if ref_img:
                path = _resolve_sheet_or_photo(ref_img) if callable(_resolve_sheet_or_photo) else None
                if not path:
                    # Try direct file
                    for ext in (".jpg", ".jpeg", ".png", ".webp"):
                        cand = os.path.join(POS_PHOTOS_CHARS_DIR, f"{char['id']}{ext}")
                        if os.path.isfile(cand):
                            path = cand
                            break
                if path:
                    refs.append({"path": path, "tag": "Character"})

        try:
            from lib.video_generator import _runway_generate_scene_image

            ratio_map = {"1:1": "1024:1024", "9:16": "720:1280", "16:9": "1280:720"}
            gen_ratio = ratio_map.get(size, "1024:1024")

            # Add @tags to prompt
            for r in refs:
                if f"@{r['tag']}" not in prompt:
                    prompt = f"@{r['tag']} " + prompt

            img_path = _runway_generate_scene_image(
                prompt, refs[:3],
                ratio=gen_ratio,
                model="gen4_image",
            )

            if not img_path or not os.path.isfile(img_path):
                self._send_json({"error": "Album art generation failed"}, 500)
                return

            # Save to output
            art_dir = os.path.join(OUTPUT_DIR, "album_art")
            os.makedirs(art_dir, exist_ok=True)
            filename = f"album_{size.replace(':', 'x')}_{int(time.time())}.png"
            dest = os.path.join(art_dir, filename)
            import shutil
            shutil.copy2(img_path, dest)

            self._send_json({
                "ok": True,
                "imageUrl": f"/output/album_art/{filename}",
                "filename": filename,
                "size": size,
                "prompt": prompt[:200],
            })

        except Exception as e:
            self._send_json({"error": f"Album art error: {str(e)[:200]}"}, 500)

    # ---- Reference Demo Image Generation (Lookbook) ----

    def _handle_generate_reference_images(self):
        """Generate reference/demo images for creative controls.
        Uses a consistent subject (golden retriever) to show how each
        lighting, camera angle, shot type, and color grade looks."""

        refs_dir = os.path.join(OUTPUT_DIR, "reference_demos")
        os.makedirs(refs_dir, exist_ok=True)

        from lib.video_generator import _runway_generate_scene_image

        base_subject = "A golden retriever dog sitting in a sunlit park, green grass, trees in background"

        # Define all options to generate
        demos = []

        # Lighting types
        for light in ["natural soft", "harsh sunlight", "overcast diffused", "neon",
                       "cinematic contrast", "low key dramatic", "high key bright",
                       "practical lighting", "volumetric fog", "backlit silhouette"]:
            demos.append({
                "category": "lighting",
                "name": light,
                "filename": f"light_{light.replace(' ', '_')}.jpg",
                "prompt": f"{base_subject}. Lighting: {light}. Photorealistic, 8K, professional photography.",
            })

        # Camera angles
        for angle in ["eye level", "low angle", "high angle", "top down", "dutch tilt", "ground level"]:
            demos.append({
                "category": "camera_angle",
                "name": angle,
                "filename": f"angle_{angle.replace(' ', '_')}.jpg",
                "prompt": f"{base_subject}. Camera angle: {angle}. Photorealistic, 8K, professional cinematography.",
            })

        # Shot types
        for shot in ["close-up", "medium", "full", "wide", "establishing"]:
            framing = {
                "close-up": "Extreme close-up of the dog's face, shallow depth of field, 85mm lens",
                "medium": "Medium shot showing the dog from chest up, 50mm lens",
                "full": "Full body shot of the dog head to tail, 35mm lens",
                "wide": "Wide shot with the dog small in frame, expansive park visible, 24mm lens",
                "establishing": "Establishing shot of the entire park, dog barely visible in distance, 16mm ultra-wide",
            }
            demos.append({
                "category": "shot_type",
                "name": shot,
                "filename": f"shot_{shot.replace('-', '_')}.jpg",
                "prompt": f"{framing[shot]}. {base_subject}. Photorealistic, 8K.",
            })

        # Color grades
        for grade in ["warm", "cool", "vintage", "noir", "cyberpunk", "golden"]:
            grade_desc = {
                "warm": "warm golden tones, amber highlights, cozy feel",
                "cool": "cool blue tones, teal shadows, crisp atmosphere",
                "vintage": "muted desaturated colors, slight sepia, film grain",
                "noir": "black and white, high contrast, dramatic shadows",
                "cyberpunk": "neon purple and cyan lighting, futuristic mood",
                "golden": "golden hour lighting, warm orange glow, long shadows",
            }
            demos.append({
                "category": "color_grade",
                "name": grade,
                "filename": f"grade_{grade}.jpg",
                "prompt": f"{base_subject}. Color grading: {grade_desc[grade]}. Photorealistic, 8K, cinematic.",
            })

        # Check which already exist (skip regeneration)
        to_generate = []
        already_done = []
        for d in demos:
            fpath = os.path.join(refs_dir, d["filename"])
            if os.path.isfile(fpath):
                already_done.append(d["name"])
            else:
                to_generate.append(d)

        if not to_generate:
            self._send_json({
                "ok": True,
                "message": f"All {len(demos)} reference images already exist",
                "total": len(demos),
                "generated": 0,
            })
            return

        # Generate in background thread
        def _gen_refs():
            generated = 0
            failed = 0
            for i, d in enumerate(to_generate):
                try:
                    print(f"[REF-DEMO] Generating {i+1}/{len(to_generate)}: {d['category']}/{d['name']}...")
                    img_path = _runway_generate_scene_image(
                        d["prompt"], [],
                        ratio="1024:1024",
                        model="gen4_image_turbo",
                    )
                    if img_path and os.path.isfile(img_path):
                        import shutil
                        dest = os.path.join(refs_dir, d["filename"])
                        shutil.copy2(img_path, dest)
                        generated += 1
                    else:
                        failed += 1
                        print(f"[REF-DEMO] Failed: {d['name']}")
                except Exception as e:
                    failed += 1
                    print(f"[REF-DEMO] Error generating {d['name']}: {e}")

                # Brief pause between generations
                import time as _t
                _t.sleep(1)

            print(f"[REF-DEMO] Done! Generated: {generated}, Failed: {failed}, Skipped: {len(already_done)}")

        thread = threading.Thread(target=_gen_refs, daemon=True)
        thread.start()

        self._send_json({
            "ok": True,
            "message": f"Generating {len(to_generate)} reference images in background ({len(already_done)} already exist)",
            "total": len(demos),
            "toGenerate": len(to_generate),
            "alreadyDone": len(already_done),
        })

    # ---- Upload handlers ----

    def _handle_upload(self):
        """Handle multipart file upload."""
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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

        with gen_lock:
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

    # ---- Audio Generation endpoints (Runway) ----

    def _handle_generate_sound(self):
        """POST /api/generate-sound -- Runway sound_effect API."""
        try:
            body = json.loads(self._read_body())
        except Exception:
            self._send_json({"ok": False, "error": "Invalid JSON body"}, 400)
            return

        prompt_text = body.get("promptText", "").strip()
        duration = body.get("duration", 10)
        loop = body.get("loop", False)

        if not prompt_text:
            self._send_json({"ok": False, "error": "promptText is required"}, 400)
            return

        def _run():
            import requests
            try:
                # Submit to Runway sound_effect API
                payload = {
                    "model": "eleven_text_to_sound_v2",
                    "promptText": prompt_text,
                    "duration": int(duration),
                    "loop": bool(loop),
                }
                resp = requests.post(
                    f"{RUNWAY_API_BASE}/sound_effect",
                    headers=_runway_headers(),
                    json=payload,
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("id", "")
                if not task_id:
                    raise RuntimeError(f"No task ID returned: {data}")

                print(f"[AUDIO] Sound effect task submitted: {task_id}")

                # Poll for completion
                result = _runway_poll(task_id)
                audio_url = result["url"]

                # Download to output/audio/
                ext = ".mp3"
                if ".wav" in audio_url:
                    ext = ".wav"
                filename = f"sfx_{task_id[:12]}_{int(time.time())}{ext}"
                dest = os.path.join(AUDIO_GEN_DIR, filename)
                _download(audio_url, dest)

                print(f"[AUDIO] Sound effect saved: {dest}")
                self._send_json({
                    "ok": True,
                    "audio_url": f"/api/audio/generated/{filename}",
                    "filename": filename,
                })
            except Exception as e:
                print(f"[AUDIO] Sound effect generation failed: {e}")
                self._send_json({"ok": False, "error": str(e)}, 500)

        # Run synchronously (polling blocks but keeps it simple like video gen)
        _run()

    def _handle_generate_tts(self):
        """POST /api/generate-tts -- Runway text_to_speech API."""
        try:
            body = json.loads(self._read_body())
        except Exception:
            self._send_json({"ok": False, "error": "Invalid JSON body"}, 400)
            return

        prompt_text = body.get("promptText", "").strip()
        voice_preset_id = body.get("voicePresetId", "Leslie")

        if not prompt_text:
            self._send_json({"ok": False, "error": "promptText is required"}, 400)
            return

        def _run():
            import requests
            try:
                payload = {
                    "model": "eleven_multilingual_v2",
                    "promptText": prompt_text,
                    "voice": {
                        "type": "runway-preset",
                        "presetId": voice_preset_id,
                    },
                }
                resp = requests.post(
                    f"{RUNWAY_API_BASE}/text_to_speech",
                    headers=_runway_headers(),
                    json=payload,
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("id", "")
                if not task_id:
                    raise RuntimeError(f"No task ID returned: {data}")

                print(f"[AUDIO] TTS task submitted: {task_id}")

                # Poll for completion
                result = _runway_poll(task_id)
                audio_url = result["url"]

                # Download to output/audio/
                ext = ".mp3"
                if ".wav" in audio_url:
                    ext = ".wav"
                filename = f"tts_{task_id[:12]}_{int(time.time())}{ext}"
                dest = os.path.join(AUDIO_GEN_DIR, filename)
                _download(audio_url, dest)

                print(f"[AUDIO] TTS saved: {dest}")
                self._send_json({
                    "ok": True,
                    "audio_url": f"/api/audio/generated/{filename}",
                    "filename": filename,
                })
            except Exception as e:
                print(f"[AUDIO] TTS generation failed: {e}")
                self._send_json({"ok": False, "error": str(e)}, 500)

        _run()

    def _handle_generate_sts(self):
        """POST /api/generate-speech-to-speech -- Runway speech_to_speech API."""
        try:
            body = json.loads(self._read_body())
        except Exception:
            self._send_json({"ok": False, "error": "Invalid JSON body"}, 400)
            return

        audio_filename = body.get("audioFilename", "").strip()
        voice_preset_id = body.get("voicePresetId", "Maggie")

        if not audio_filename:
            self._send_json({"ok": False, "error": "audioFilename is required"}, 400)
            return

        # Resolve the uploaded audio file
        audio_path = os.path.join(UPLOADS_DIR, audio_filename)
        if not os.path.isfile(audio_path):
            self._send_json({"ok": False, "error": f"Audio file not found: {audio_filename}"}, 404)
            return

        def _run():
            import requests, base64
            try:
                # Read audio and convert to base64 data URI
                with open(audio_path, "rb") as f:
                    audio_bytes = f.read()
                ext = os.path.splitext(audio_filename)[1].lower().lstrip(".")
                mime = {"mp3": "audio/mp3", "wav": "audio/wav", "m4a": "audio/m4a"}.get(ext, "audio/mp3")
                data_uri = f"data:{mime};base64,{base64.b64encode(audio_bytes).decode()}"

                payload = {
                    "model": "eleven_multilingual_sts_v2",
                    "media": {
                        "type": "audio",
                        "uri": data_uri,
                    },
                    "voice": {
                        "type": "runway-preset",
                        "presetId": voice_preset_id,
                    },
                }
                resp = requests.post(
                    f"{RUNWAY_API_BASE}/speech_to_speech",
                    headers=_runway_headers(),
                    json=payload,
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("id", "")
                if not task_id:
                    raise RuntimeError(f"No task ID returned: {data}")

                print(f"[AUDIO] STS task submitted: {task_id}")

                # Poll for completion
                result = _runway_poll(task_id)
                audio_url = result["url"]

                # Download to output/audio/
                out_ext = ".mp3"
                if ".wav" in audio_url:
                    out_ext = ".wav"
                filename = f"sts_{task_id[:12]}_{int(time.time())}{out_ext}"
                dest = os.path.join(AUDIO_GEN_DIR, filename)
                _download(audio_url, dest)

                print(f"[AUDIO] STS saved: {dest}")
                self._send_json({
                    "ok": True,
                    "audio_url": f"/api/audio/generated/{filename}",
                    "filename": filename,
                })
            except Exception as e:
                print(f"[AUDIO] STS generation failed: {e}")
                self._send_json({"ok": False, "error": str(e)}, 500)

        _run()

    def _handle_generate_voice_dubbing(self):
        """POST /api/generate-voice-dubbing -- Runway voice_dubbing API."""
        try:
            body = json.loads(self._read_body())
        except Exception:
            self._send_json({"ok": False, "error": "Invalid JSON body"}, 400)
            return

        audio_filename = body.get("audioFilename", "").strip()
        target_lang = body.get("targetLang", "es").strip()

        if not audio_filename:
            self._send_json({"ok": False, "error": "audioFilename is required"}, 400)
            return

        SUPPORTED_LANGS = {"en","hi","pt","zh","es","fr","de","ja","ar","ru","ko","id","it","nl","tr","pl","sv","fil","ms","ro","uk","el","cs","da","fi","bg","hr","sk","ta"}
        if target_lang not in SUPPORTED_LANGS:
            self._send_json({"ok": False, "error": f"Unsupported language: {target_lang}"}, 400)
            return

        audio_path = os.path.join(UPLOADS_DIR, audio_filename)
        if not os.path.isfile(audio_path):
            self._send_json({"ok": False, "error": f"Audio file not found: {audio_filename}"}, 404)
            return

        def _run():
            import requests, base64
            try:
                with open(audio_path, "rb") as f:
                    audio_bytes = f.read()
                ext = os.path.splitext(audio_filename)[1].lower().lstrip(".")
                mime = {"mp3": "audio/mp3", "wav": "audio/wav", "m4a": "audio/m4a"}.get(ext, "audio/mp3")
                data_uri = f"data:{mime};base64,{base64.b64encode(audio_bytes).decode()}"

                payload = {
                    "model": "eleven_voice_dubbing",
                    "audioUri": data_uri,
                    "targetLang": target_lang,
                    "disableVoiceCloning": False,
                    "dropBackgroundAudio": False,
                }
                resp = requests.post(
                    f"{RUNWAY_API_BASE}/voice_dubbing",
                    headers=_runway_headers(),
                    json=payload,
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("id", "")
                if not task_id:
                    raise RuntimeError(f"No task ID returned: {data}")

                print(f"[AUDIO] Voice dubbing task submitted: {task_id}")

                result = _runway_poll(task_id)
                audio_url = result["url"]

                out_ext = ".mp3"
                if ".wav" in audio_url:
                    out_ext = ".wav"
                filename = f"dub_{target_lang}_{task_id[:12]}_{int(time.time())}{out_ext}"
                dest = os.path.join(AUDIO_GEN_DIR, filename)
                _download(audio_url, dest)

                print(f"[AUDIO] Voice dubbing saved: {dest}")
                self._send_json({
                    "ok": True,
                    "audio_url": f"/api/audio/generated/{filename}",
                    "filename": filename,
                })
            except Exception as e:
                print(f"[AUDIO] Voice dubbing failed: {e}")
                self._send_json({"ok": False, "error": str(e)}, 500)

        _run()

    def _handle_generate_voice_isolation(self):
        """POST /api/generate-voice-isolation -- Runway voice_isolation API."""
        try:
            body = json.loads(self._read_body())
        except Exception:
            self._send_json({"ok": False, "error": "Invalid JSON body"}, 400)
            return

        audio_filename = body.get("audioFilename", "").strip()

        if not audio_filename:
            self._send_json({"ok": False, "error": "audioFilename is required"}, 400)
            return

        audio_path = os.path.join(UPLOADS_DIR, audio_filename)
        if not os.path.isfile(audio_path):
            self._send_json({"ok": False, "error": f"Audio file not found: {audio_filename}"}, 404)
            return

        def _run():
            import requests, base64
            try:
                with open(audio_path, "rb") as f:
                    audio_bytes = f.read()
                ext = os.path.splitext(audio_filename)[1].lower().lstrip(".")
                mime = {"mp3": "audio/mp3", "wav": "audio/wav", "m4a": "audio/m4a"}.get(ext, "audio/mp3")
                data_uri = f"data:{mime};base64,{base64.b64encode(audio_bytes).decode()}"

                payload = {
                    "model": "eleven_voice_isolation",
                    "audioUri": data_uri,
                }
                resp = requests.post(
                    f"{RUNWAY_API_BASE}/voice_isolation",
                    headers=_runway_headers(),
                    json=payload,
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("id", "")
                if not task_id:
                    raise RuntimeError(f"No task ID returned: {data}")

                print(f"[AUDIO] Voice isolation task submitted: {task_id}")

                result = _runway_poll(task_id)
                audio_url = result["url"]

                out_ext = ".mp3"
                if ".wav" in audio_url:
                    out_ext = ".wav"
                filename = f"iso_{task_id[:12]}_{int(time.time())}{out_ext}"
                dest = os.path.join(AUDIO_GEN_DIR, filename)
                _download(audio_url, dest)

                print(f"[AUDIO] Voice isolation saved: {dest}")
                self._send_json({
                    "ok": True,
                    "audio_url": f"/api/audio/generated/{filename}",
                    "filename": filename,
                })
            except Exception as e:
                print(f"[AUDIO] Voice isolation failed: {e}")
                self._send_json({"ok": False, "error": str(e)}, 500)

        _run()

    # ---- Reference Image endpoints ----

    def _handle_upload_reference(self):
        """Upload a reference image with a name."""
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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
            scene["duration"] = params.get("duration", 8)
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
                # Prompt OS entity links
                if "characterId" in params:
                    s["characterId"] = params["characterId"] or None
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

    def _handle_manual_duplicate_scene(self, scene_id: str):
        """Duplicate a manual scene (clone prompt, settings, photo — not clip)."""
        plan = _load_manual_plan()
        source = None
        insert_idx = 0
        for i, s in enumerate(plan["scenes"]):
            if s["id"] == scene_id:
                source = s
                insert_idx = i + 1
                break
        if not source:
            self._send_json({"error": "Scene not found"}, 404)
            return

        import copy
        new_scene = copy.deepcopy(source)
        new_scene["id"] = str(_uuid.uuid4())[:8]
        # Clear generation state so it doesn't skip caching
        new_scene.pop("clip_path", None)
        new_scene.pop("has_clip", None)
        new_scene.pop("gen_hash", None)
        new_scene.pop("video_path", None)

        # Copy photo file if it exists
        if source.get("photo_path") and os.path.isfile(source["photo_path"]):
            ext = os.path.splitext(source["photo_path"])[1]
            new_photo = os.path.join(SCENE_PHOTOS_DIR, f"{new_scene['id']}{ext}")
            import shutil
            shutil.copy2(source["photo_path"], new_photo)
            new_scene["photo_path"] = new_photo

        plan["scenes"].insert(insert_idx, new_scene)
        _save_manual_plan(plan)
        self._send_json({"ok": True, "new_id": new_scene["id"]})

    def _handle_manual_trim_scene(self, scene_id: str):
        """Set trim in/out points for a scene clip."""
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        plan = _load_manual_plan()
        for s in plan["scenes"]:
            if s["id"] == scene_id:
                trim_in = float(body.get("trim_in", 0))
                trim_out = float(body.get("trim_out", 0))
                s["trim_in"] = max(0, trim_in)
                s["trim_out"] = max(0, trim_out)
                _save_manual_plan(plan)
                self._send_json({"ok": True, "trim_in": s["trim_in"], "trim_out": s["trim_out"]})
                return
        self._send_json({"error": "Scene not found"}, 404)

    def _handle_manual_upload_photo(self, scene_id: str):
        """Upload/replace a scene photo."""
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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

        with gen_lock:
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

        with gen_lock:
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

        with gen_lock:
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
        current["duration"] = current.get("duration", 8) + nxt.get("duration", 8)

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

    def _handle_project_save(self):
        """Save entire project state to a JSON file."""
        plan = _load_manual_plan()
        auto_plan = _load_scene_plan()
        tracker = _load_cost_tracker()

        project = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "manual_plan": plan,
            "auto_plan": auto_plan,
            "cost_tracker": tracker,
            "style": "",
        }

        clip_files = {}
        for s in plan.get("scenes", []):
            cp = s.get("clip_path", "")
            if cp and os.path.isfile(cp):
                clip_files[s["id"]] = os.path.basename(cp)
        project["existing_clips"] = clip_files

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"project_{timestamp}.json"
        filepath = os.path.join(PROJECTS_DIR, filename)
        with open(filepath, "w") as f:
            json.dump(project, f, indent=2)

        self._send_json({
            "ok": True,
            "filename": filename,
            "path": filepath,
            "project": project,
        })

    def _handle_project_load(self):
        """Load a project from uploaded JSON."""
        body = self._read_body()
        try:
            project = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if "manual_plan" in project and project["manual_plan"]:
            _save_manual_plan(project["manual_plan"])

        if "auto_plan" in project and project["auto_plan"]:
            with open(SCENE_PLAN_PATH, "w") as f:
                json.dump(project["auto_plan"], f, indent=2)

        if "cost_tracker" in project and project["cost_tracker"]:
            _save_cost_tracker(project["cost_tracker"])

        self._send_json({"ok": True, "message": "Project loaded"})

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
            subprocess.run(cmd_a, check=True, capture_output=True, **_subprocess_kwargs())

            # Extract first 1s of clip B
            head_b = os.path.join(PREVIEWS_DIR, f"_head_{scene_id_b}.mp4")
            cmd_b = [
                "ffmpeg", "-y",
                "-i", clip_b,
                "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
                "-an", head_b,
            ]
            subprocess.run(cmd_b, check=True, capture_output=True, **_subprocess_kwargs())

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

            subprocess.run(cmd_t, check=True, capture_output=True, **_subprocess_kwargs())

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

        with gen_lock:
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
        self._send_json(tracker)

    def _handle_runway_credits(self):
        """Get real Runway credit balance."""
        try:
            import requests
            resp = requests.get(
                f"{RUNWAY_API_BASE}/organization",
                headers=_runway_headers(),
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._send_json({
                    "ok": True,
                    "creditBalance": data.get("creditBalance", 0),
                    "tier": data.get("tier", {}),
                    "usage": data.get("usage", {}),
                })
            else:
                self._send_json({"error": f"Runway API {resp.status_code}"}, resp.status_code)
        except Exception as e:
            self._send_json({"error": str(e)[:200]}, 500)

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
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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
                "characters": POS_PHOTOS_CHARS_DIR,
                "costumes": POS_PHOTOS_COSTUMES_DIR,
                "environments": POS_PHOTOS_ENVS_DIR,
                "props": POS_PHOTOS_PROPS_DIR,
            }
            out_dir = dirs_map[entity_type]
            os.makedirs(out_dir, exist_ok=True)  # Ensure dir exists after resets
            out_path = os.path.join(out_dir, entity_id + ".jpg")

            # Save and resize with PIL
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(file_part["data"]))
            img = img.convert("RGB")
            orig_w, orig_h = img.size
            # Preserve high resolution for character identity — cap at 4096
            # Runway data URI limit ~5.2MB base64 = ~3.5MB file
            max_dim = 4096
            if img.width > max_dim or img.height > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            img.save(out_path, "JPEG", quality=95)
            print(f"[UPLOAD] {entity_type}/{entity_id}: {orig_w}x{orig_h} -> {img.width}x{img.height} (quality=95)")

            # Update entity record
            photo_url = f"/api/pos/{entity_type}/{entity_id}/photo"
            if entity_type == "characters":
                _prompt_os.update_character(entity_id, {"referencePhoto": photo_url})
            elif entity_type == "costumes":
                _prompt_os.update_costume(entity_id, {"referenceImagePath": photo_url})
            elif entity_type == "environments":
                _prompt_os.update_environment(entity_id, {"referenceImagePath": photo_url})
            elif entity_type == "props":
                _prompt_os.update_prop(entity_id, {"referenceImagePath": photo_url})

            self._send_json({"ok": True, "photo_url": photo_url})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_voice_sample_upload(self, voice_id):
        """Handle audio sample upload for a voice profile."""
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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

            audio_dir = os.path.join(PROMPT_OS_DATA_DIR, "audio", "voices")
            os.makedirs(audio_dir, exist_ok=True)
            out_path = os.path.join(audio_dir, voice_id + ".mp3")

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
                photo_dir = POS_PHOTOS_CHARS_DIR
            elif entity_type == "costumes":
                photo_dir = POS_PHOTOS_COSTUMES_DIR
            elif entity_type == "environments":
                photo_dir = POS_PHOTOS_ENVS_DIR
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
                    photo_dirs = {"characters": POS_PHOTOS_CHARS_DIR, "costumes": POS_PHOTOS_COSTUMES_DIR, "environments": POS_PHOTOS_ENVS_DIR}
                    pdir = photo_dirs.get(entity_type, POS_PHOTOS_CHARS_DIR)
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
                preview_path = os.path.join(POS_PREVIEWS_CHARS_DIR, f"{entity_id}.jpg")
                _sh2.copy2(ref_photo_path, preview_path)
                preview_url = f"/api/pos/{entity_type}/{entity_id}/preview"
                _prompt_os.update_character(entity_id, {"previewImage": preview_url})
                self._send_json({"ok": True, "preview_url": preview_url, "source": "reference_photo"})
                return

            # For environments with a reference photo: USE THE PHOTO as the preview
            if ref_photo_path and entity_type == "environments":
                import shutil as _sh3
                preview_path = os.path.join(POS_PREVIEWS_ENVS_DIR, f"{entity_id}.jpg")
                _sh3.copy2(ref_photo_path, preview_path)
                preview_url = f"/api/pos/{entity_type}/{entity_id}/preview"
                _prompt_os.update_environment(entity_id, {"previewImage": preview_url})
                self._send_json({"ok": True, "preview_url": preview_url, "source": "reference_photo"})
                return

            if ref_photo_path and not desc.strip():
                # Use the uploaded photo AS the preview (no need to generate)
                import shutil as _sh
                preview_dirs = {
                    "characters": POS_PREVIEWS_CHARS_DIR,
                    "costumes": POS_PREVIEWS_COSTUMES_DIR,
                    "environments": POS_PREVIEWS_ENVS_DIR,
                }
                preview_dir = preview_dirs.get(entity_type, POS_PREVIEWS_CHARS_DIR)
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
                "characters": POS_PREVIEWS_CHARS_DIR,
                "costumes": POS_PREVIEWS_COSTUMES_DIR,
                "environments": POS_PREVIEWS_ENVS_DIR,
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

    # ---- Asset Sheet Generation (Runway-based) ----

    def _handle_pos_generate_sheet(self, body):
        """Generate an asset sheet for any asset type via Runway text_to_image.

        POST /api/pos/sheets/generate
        Body: {assetType, assetId, sheetType?, model?}
        """
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "generate"):
            self._send_json({"error": "Rate limited — too many generation requests. Please wait a minute."}, 429)
            return
        asset_type = body.get("assetType", "character")  # character|costume|environment|prop
        asset_id = body.get("assetId", "")
        sheet_type = body.get("sheetType", "full")  # full|face_closeup|detail
        model = body.get("model", "gen4_image")

        # Load asset
        getter = getattr(_prompt_os, f'get_{asset_type}', None)
        if not getter:
            self._send_json({"error": f"Unknown asset type: {asset_type}"}, 400)
            return
        asset = getter(asset_id)
        if not asset:
            self._send_json({"error": f"{asset_type} not found"}, 404)
            return

        # Load project style
        project_style = _prompt_os.get_project_style()

        # Build prompt based on asset type and sheet type
        ref_photos = []  # For _runway_generate_scene_image: [{path, tag}, ...]

        # Resolve the reference photo to a local file path
        def _resolve_ref_photo(ref_val, entity_type, entity_id):
            """Resolve a reference photo value to a local file path."""
            if not ref_val:
                return None
            m = re.search(rf"/api/pos/{entity_type}/([^/]+)/photo", ref_val)
            if m:
                eid = m.group(1)
                type_dirs = {
                    "characters": POS_PHOTOS_CHARS_DIR,
                    "costumes": POS_PHOTOS_COSTUMES_DIR,
                    "environments": POS_PHOTOS_ENVS_DIR,
                    "props": POS_PHOTOS_PROPS_DIR,
                }
                photo_dir = type_dirs.get(entity_type, "")
                if photo_dir:
                    for ext in (".jpg", ".jpeg", ".png", ".webp"):
                        candidate = os.path.join(photo_dir, f"{eid}{ext}")
                        if os.path.isfile(candidate):
                            return candidate
            elif os.path.isfile(ref_val):
                return ref_val
            return None

        ref_photo_val = asset.get("referencePhoto") or asset.get("referenceImagePath") or ""

        if asset_type == "character":
            costume = None
            if asset.get("costumes") and len(asset["costumes"]) > 0:
                costume = _prompt_os.get_costume(asset["costumes"][0])
            elif asset.get("linkedCostumeIds") and len(asset["linkedCostumeIds"]) > 0:
                costume = _prompt_os.get_costume(asset["linkedCostumeIds"][0])

            if sheet_type == "face_closeup":
                prompt = build_face_closeup_prompt(asset, project_style)
            else:
                prompt = build_character_sheet_prompt(asset, costume, project_style)

            ref_path = _resolve_ref_photo(ref_photo_val, "characters", asset_id)
            if ref_path:
                ref_photos.append({"path": ref_path, "tag": "Character"})

        elif asset_type == "costume":
            character = None
            char_id = asset.get("characterId") or ""
            if not char_id and asset.get("linkedCharacterIds"):
                char_id = asset["linkedCharacterIds"][0] if asset["linkedCharacterIds"] else ""
            if char_id:
                character = _prompt_os.get_character(char_id)
            prompt = build_costume_sheet_prompt(asset, character, project_style)
            ref_path = _resolve_ref_photo(ref_photo_val, "costumes", asset_id)
            if ref_path:
                ref_photos.append({"path": ref_path, "tag": "Costume"})
            if character:
                char_ref = character.get("referencePhoto") or ""
                char_path = _resolve_ref_photo(char_ref, "characters", char_id)
                if char_path:
                    ref_photos.append({"path": char_path, "tag": "Character"})

        elif asset_type == "environment":
            prompt = build_environment_sheet_prompt(asset, project_style)
            ref_path = _resolve_ref_photo(ref_photo_val, "environments", asset_id)
            if ref_path:
                ref_photos.append({"path": ref_path, "tag": "Setting"})

        elif asset_type == "prop":
            prompt = build_prop_sheet_prompt(asset, project_style)
            ref_path = _resolve_ref_photo(ref_photo_val, "props", asset_id)
            if ref_path:
                ref_photos.append({"path": ref_path, "tag": "Prop"})

        else:
            self._send_json({"error": f"Unsupported asset type: {asset_type}"}, 400)
            return

        # Generate via Runway text_to_image
        try:
            from lib.video_generator import _runway_generate_scene_image, _download

            # Generate — returns local file path or "" on failure
            img_path = _runway_generate_scene_image(
                prompt=prompt,
                reference_photos=ref_photos[:3],  # API max 3
                model=model,
                ratio="1536:1536",  # Higher-res square for production-quality sheets
            )

            if not img_path or not os.path.isfile(img_path):
                self._send_json({"error": "Sheet generation failed — no image returned. Check Runway API key and credits."}, 500)
                return

            # Copy to sheets directory with a stable name
            sheets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "prompt_os", "sheets")
            os.makedirs(sheets_dir, exist_ok=True)
            filename = f"{asset_type}_{asset_id}_{sheet_type}_{int(time.time())}.png"
            local_path = os.path.join(sheets_dir, filename)

            import shutil
            shutil.copy2(img_path, local_path)

            # Store sheet data
            sheet_data = {
                "url": f"/api/pos/sheets/{filename}",
                "localPath": local_path,
                "type": sheet_type,
                "model": model,
                "resolution": {"width": 1024, "height": 1024},
                "prompt": prompt[:500],
                "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }

            result = _prompt_os.add_sheet_image(asset_type, asset_id, sheet_data)
            self._send_json(result)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": f"Sheet generation error: {str(e)}"}, 500)

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
                        candidate = os.path.join(POS_PHOTOS_CHARS_DIR, f"{eid}{ext}")
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
            preview_path = os.path.join(POS_PREVIEWS_CHARS_DIR, f"{char_id}_sheet.jpg")
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
                    duration = max(s.get("end_sec", s.get("duration", 8)) for s in plan["scenes"])
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
                with open(SCENE_PLAN_PATH, "w") as f:
                    json.dump(plan, f, indent=2)
        else:
            plan = _load_manual_plan()
            plan["lyrics"] = lyrics_data
            _save_manual_plan(plan)
        with gen_lock:
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
        with gen_lock:
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
        orig_dur = scene.get("duration", 8)
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
        end = scene.get("end_sec", start + scene.get("duration", 8))
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
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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
            subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
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
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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
            dur = s.get("duration", 8)
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
                dur = s.get("duration", 8)
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
        """List all saved project templates."""
        templates_dir = os.path.join(OUTPUT_DIR, "templates")
        templates = []
        if os.path.isdir(templates_dir):
            for fname in sorted(os.listdir(templates_dir)):
                if fname.endswith(".json"):
                    try:
                        with open(os.path.join(templates_dir, fname), "r") as f:
                            t = json.load(f)
                        templates.append({
                            "id": t.get("id", ""),
                            "name": t.get("name", "Untitled"),
                            "description": t.get("description", ""),
                            "createdAt": t.get("createdAt", ""),
                            "characterCount": len(t.get("characters", [])),
                            "environmentCount": len(t.get("environments", [])),
                        })
                    except (json.JSONDecodeError, IOError):
                        pass
        self._send_json(templates)

    def _handle_save_template(self):
        """Save the current project state as a named project template."""
        body = self._read_body()
        try:
            params = json.loads(body) if body else {}
        except json.JSONDecodeError:
            params = {}

        name = params.get("name", "").strip()
        if not name:
            name = f"Template {time.strftime('%Y%m%d_%H%M%S')}"
        description = params.get("description", "")

        # Collect all reusable project data
        template = {
            "id": str(_uuid.uuid4())[:8],
            "name": name,
            "description": description,
            "createdAt": datetime.utcnow().isoformat(),
            "projectStyle": _prompt_os.get_project_style(),
            "styleLocks": _prompt_os.get_style_locks(),
            "worldRules": _prompt_os.get_world_rules(),
            "continuityRules": _prompt_os.get_continuity_rules(),
            "characters": _prompt_os.get_characters(),
            "costumes": _prompt_os.get_costumes(),
            "environments": _prompt_os.get_environments(),
            "props": _prompt_os.get_props(),
            "voices": _prompt_os.get_voices(),
            "settings": _load_settings(),
        }

        # Save to templates directory
        templates_dir = os.path.join(OUTPUT_DIR, "templates")
        os.makedirs(templates_dir, exist_ok=True)
        template_path = os.path.join(templates_dir, f"{template['id']}.json")
        with open(template_path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2, ensure_ascii=False)

        self._send_json({"ok": True, "template": {"id": template["id"], "name": name}})

    def _handle_load_template(self):
        """Load a project template — replaces assets + style in the current project."""
        body = self._read_body()
        try:
            params = json.loads(body) if body else {}
        except json.JSONDecodeError:
            params = {}

        template_id = params.get("id", "")
        if not template_id:
            self._send_json({"error": "No template id specified"}, 400)
            return

        templates_dir = os.path.join(OUTPUT_DIR, "templates")
        template_path = os.path.join(templates_dir, f"{template_id}.json")
        if not os.path.isfile(template_path):
            self._send_json({"error": "Template not found"}, 404)
            return

        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self._send_json({"error": f"Failed to read template: {e}"}, 500)
            return

        # Apply template data — style, rules, continuity
        if template.get("projectStyle"):
            _prompt_os.set_project_style(template["projectStyle"])
        if template.get("styleLocks"):
            _prompt_os.set_style_locks(template["styleLocks"])
        if template.get("worldRules"):
            _prompt_os.set_world_rules(template["worldRules"])
        if template.get("continuityRules"):
            _prompt_os.set_continuity_rules(template["continuityRules"])

        # Replace or merge assets
        merge = params.get("merge", False)
        if not merge:
            # Full replace — clear existing and load template assets
            from lib.prompt_os import CHARACTERS_PATH, COSTUMES_PATH, ENVIRONMENTS_PATH, PROPS_PATH, VOICES_PATH, _save_json
            _save_json(CHARACTERS_PATH, template.get("characters", []))
            _save_json(COSTUMES_PATH, template.get("costumes", []))
            _save_json(ENVIRONMENTS_PATH, template.get("environments", []))
            _save_json(PROPS_PATH, template.get("props", []))
            _save_json(VOICES_PATH, template.get("voices", []))

        self._send_json({"ok": True, "name": template.get("name", "")})

    def _handle_delete_template(self):
        """Delete a saved project template by id."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        m = re.match(r'^/api/templates/([^/]+)$', path)
        if not m:
            self._send_json({"error": "Invalid template id"}, 400)
            return
        tid = m.group(1)
        templates_dir = os.path.join(OUTPUT_DIR, "templates")
        fpath = os.path.join(templates_dir, f"{tid}.json")
        if os.path.isfile(fpath):
            os.remove(fpath)
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "Not found"}, 404)

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
                except: pass
            cleared.append("scene_thumbnails")
        # Clear keyframes
        kf_dir = os.path.join(OUTPUT_DIR, "keyframes")
        if os.path.isdir(kf_dir):
            for f in os.listdir(kf_dir):
                try: os.unlink(os.path.join(kf_dir, f))
                except: pass
            cleared.append("keyframes")
        # Clear clips
        for clips_dir in [CLIPS_DIR, MANUAL_CLIPS_DIR]:
            if os.path.isdir(clips_dir):
                for f in os.listdir(clips_dir):
                    fp = os.path.join(clips_dir, f)
                    try: os.unlink(fp)
                    except: pass
                cleared.append(os.path.basename(clips_dir))
        # Clear uploaded scene photos
        photos_dir = os.path.join(UPLOADS_DIR, "scene_photos")
        if os.path.isdir(photos_dir):
            for f in os.listdir(photos_dir):
                try: os.unlink(os.path.join(photos_dir, f))
                except: pass
            cleared.append("scene_photos")
        # Clear uploaded scene videos
        videos_dir = os.path.join(UPLOADS_DIR, "scene_videos")
        if os.path.isdir(videos_dir):
            for f in os.listdir(videos_dir):
                try: os.unlink(os.path.join(videos_dir, f))
                except: pass
            cleared.append("scene_videos")
        # Clear uploaded scene vocals
        vocals_dir = os.path.join(UPLOADS_DIR, "scene_vocals")
        if os.path.isdir(vocals_dir):
            for f in os.listdir(vocals_dir):
                try: os.unlink(os.path.join(vocals_dir, f))
                except: pass
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
                except: pass
            cleared.append("previews")
        # Clear GIFs
        gifs_dir = os.path.join(OUTPUT_DIR, "gifs")
        if os.path.isdir(gifs_dir):
            for f in os.listdir(gifs_dir):
                try: os.unlink(os.path.join(gifs_dir, f))
                except: pass
            cleared.append("gifs")
        # Clear uploaded songs
        songs_in_uploads = [f for f in os.listdir(UPLOADS_DIR)
                           if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac'))
                           and os.path.isfile(os.path.join(UPLOADS_DIR, f))]
        for f in songs_in_uploads:
            try: os.unlink(os.path.join(UPLOADS_DIR, f))
            except: pass
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
            except:
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
                except: pass
            cleared.append("references")

        # Clear storyboards
        sb_dir = os.path.join(OUTPUT_DIR, "storyboards")
        if os.path.isdir(sb_dir):
            for f in os.listdir(sb_dir):
                try: os.unlink(os.path.join(sb_dir, f))
                except: pass
            cleared.append("storyboards")

        # Clear exports
        exports_dir = os.path.join(OUTPUT_DIR, "exports")
        if os.path.isdir(exports_dir):
            for f in os.listdir(exports_dir):
                try: os.unlink(os.path.join(exports_dir, f))
                except: pass
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
            with open(plan_path, "r") as f:
                plan = json.load(f)

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
        body = json.loads(self._read_body())
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
        print(f"[DIRECTOR] character_ids={char_ids}, resolved {len(characters)} characters: {[c.get('name') for c in characters]}")

        environments = []
        for eid in env_ids:
            e = _prompt_os.get_environment(eid)
            if e:
                environments.append(e)
        print(f"[DIRECTOR] environment_ids={env_ids}, resolved {len(environments)} environments: {[e.get('name') for e in environments]}")

        costume_ids = body.get("costume_ids", [])
        costumes = []
        for cid in costume_ids:
            c = _prompt_os.get_costume(cid)
            if c:
                costumes.append(c)
        print(f"[DIRECTOR] costume_ids={costume_ids}, resolved {len(costumes)} costumes: {[c.get('name') for c in costumes]}")

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

            # Assign varied shot types for cinematic variety
            _assign_shot_types(plan.get("scenes", []))

            # Save plan
            with open(AUTO_DIRECTOR_PLAN_PATH, "w") as f:
                json.dump(plan, f, indent=2)
            _sync_auto_plan_to_scene_plan(plan)

            self._send_json({"ok": True, "plan": plan})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_auto_director_ai_plan(self):
        """Plan a full video via AI Story Planner (LLM-driven)."""
        body = json.loads(self._read_body())
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

            # Assign varied shot types for cinematic variety
            _assign_shot_types(plan.get("scenes", []))

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

        with open(AUTO_DIRECTOR_PLAN_PATH, "r") as f:
            plan = json.load(f)

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

        # Read render options from body (audio tracks, text overlays, transitions)
        body = {}
        try:
            raw = self._read_body()
            if raw:
                body = json.loads(raw)
        except Exception:
            body = {}

        with open(AUTO_DIRECTOR_PLAN_PATH, "r") as f:
            plan = json.load(f)

        scenes = plan.get("scenes", [])
        song_path = plan.get("song_path")

        # Collect valid clips
        clip_paths = [s.get("clip_path") for s in scenes
                      if s.get("clip_path") and os.path.isfile(s.get("clip_path", ""))]
        if not clip_paths:
            self._send_json({"error": "No clips found on disk to stitch"}, 400)
            return

        default_transition = "crossfade"
        transitions = [s.get("transition", default_transition) for s in scenes
                       if s.get("clip_path") and os.path.isfile(s.get("clip_path", ""))]
        output_path = os.path.join(OUTPUT_DIR, "auto_director_final.mp4")
        audio = song_path if song_path and os.path.isfile(song_path) else None

        # --- Audio mixing: combine multiple timeline tracks into one file ---
        audio_tracks = body.get("audioTracks", [])
        mixed_audio = audio  # Default to song path from plan

        if audio_tracks:
            track_files = []
            for track in audio_tracks:
                url = track.get("url", "")
                fpath = None
                if url.startswith("/api/audio/generated/"):
                    fname = url.split("/api/audio/generated/")[-1]
                    fpath = os.path.join(AUDIO_GEN_DIR, os.path.basename(fname))
                elif url.startswith("/uploads/") or url.startswith("uploads/"):
                    fname = url.split("/uploads/")[-1] if "/uploads/" in url else url[len("uploads/"):]
                    fpath = os.path.join(UPLOADS_DIR, os.path.basename(fname))
                if fpath and os.path.isfile(fpath):
                    vol = track.get("volume", 80)
                    track_files.append({
                        "path": fpath,
                        "volume": max(0.0, min(2.0, vol / 100.0)),
                        "type": track.get("type", "music"),
                    })

            if track_files and len(track_files) > 1:
                # Mix multiple tracks using ffmpeg
                try:
                    mixed_path = os.path.join(OUTPUT_DIR, "mixed_audio.mp3")
                    mix_multi_audio_tracks(
                        [t["path"] for t in track_files],
                        mixed_path,
                        volumes=[t["volume"] for t in track_files],
                    )
                    mixed_audio = mixed_path
                    print(f"[STITCH] Mixed {len(track_files)} audio tracks into {mixed_path}")
                except Exception as e:
                    print(f"[STITCH] Audio mix failed: {e}, using primary audio only")
            elif track_files:
                mixed_audio = track_files[0]["path"]
                print(f"[STITCH] Using single audio track: {mixed_audio}")

        # --- Text overlays: map timeline overlays to per-scene format ---
        text_overlays_data = body.get("textOverlays", [])
        text_overlays = None
        if text_overlays_data:
            text_overlays = []
            cumulative_time = 0
            for i, s in enumerate(scenes):
                if not (s.get("clip_path") and os.path.isfile(s.get("clip_path", ""))):
                    continue
                dur = float(s.get("duration", 4))
                scene_start = cumulative_time
                scene_end = cumulative_time + dur

                # Find overlays that overlap this scene
                scene_overlay = None
                for ovl in text_overlays_data:
                    ovl_start = float(ovl.get("startTime", 0))
                    ovl_end = float(ovl.get("endTime", 5))
                    if ovl_start < scene_end and ovl_end > scene_start:
                        scene_overlay = {
                            "text": ovl.get("text", ""),
                            "font_size": ovl.get("fontSize", 24),
                            "position": ovl.get("position", "bottom-center"),
                            "color": ovl.get("color", "#ffffff"),
                        }
                        break
                text_overlays.append(scene_overlay)
                cumulative_time += dur

        # --- Per-scene transitions from Edit workspace ---
        scene_transitions = body.get("sceneTransitions", {})
        if scene_transitions:
            new_transitions = []
            valid_idx = 0
            for i, s in enumerate(scenes):
                if not (s.get("clip_path") and os.path.isfile(s.get("clip_path", ""))):
                    continue
                st = scene_transitions.get(str(i), {})
                trans = st.get("entry", "none")
                if trans == "none":
                    trans = default_transition if valid_idx > 0 else "none"
                new_transitions.append(trans)
                valid_idx += 1
            if new_transitions:
                transitions = new_transitions

        def run_stitch():
            try:
                _auto_director._update_progress(phase="stitching", progress_detail="preparing...")

                def _stitch_progress(status):
                    _auto_director._update_progress(phase="stitching", progress_detail=status)

                stitch(clip_paths, mixed_audio, output_path,
                       transitions=transitions,
                       text_overlays=text_overlays,
                       progress_cb=_stitch_progress)
                _auto_director._update_progress(phase="done", output_file=output_path, progress_detail="")
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

        with open(AUTO_DIRECTOR_PLAN_PATH, "r") as f:
            ad_plan = json.load(f)

        manual_plan = _load_manual_plan()
        manual_plan["song_path"] = ad_plan.get("song_path")

        for ad_scene in ad_plan.get("scenes", []):
            manual_scene = {
                "id": ad_scene.get("id", str(_uuid.uuid4())[:8]),
                "prompt": ad_scene.get("prompt", ""),
                "duration": ad_scene.get("duration", 8),
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
        body = json.loads(self._read_body())
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

    def _handle_movie_scene_edit(self, scene_index):
        """Edit specific fields of a scene in the movie plan."""
        body = json.loads(self._read_body())
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
        body = json.loads(self._read_body())
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
        body = json.loads(self._read_body())
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
        body = json.loads(self._read_body())
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
        body = json.loads(self._read_body())
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
        body = json.loads(self._read_body())
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

    def _handle_movie_scene_lock(self, scene_index):
        """Lock specific fields of a scene."""
        body = json.loads(self._read_body())
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
        body = json.loads(self._read_body())
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

    def _handle_scene_upload_clip(self, scene_index):
        """Upload an external video clip to a scene."""
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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

            plan = load_movie_plan(OUTPUT_DIR)
            if not plan or scene_index >= len(plan.get("scenes", [])):
                self._send_json({"error": "Invalid scene"}, 400)
                return

            # Save clip
            clips_dir = os.path.join(OUTPUT_DIR, "auto_director", "clips")
            os.makedirs(clips_dir, exist_ok=True)
            ext = os.path.splitext(file_part.get("filename", "clip.mp4"))[1] or ".mp4"
            filename = f"scene_{scene_index}_upload_{int(time.time())}{ext}"
            clip_path = os.path.join(clips_dir, filename)
            with open(clip_path, "wb") as f:
                f.write(file_part["data"])

            # Update plan
            scene = plan["scenes"][scene_index]
            scene["clip_path"] = clip_path
            scene["clip_url"] = f"/api/clips/{filename}"
            scene["has_clip"] = True
            scene["clip_source"] = "upload"
            save_movie_plan(plan, OUTPUT_DIR)

            print(f"[UPLOAD] Clip uploaded for scene {scene_index}: {clip_path} ({len(file_part['data'])/1024:.0f}KB)")

            self._send_json({
                "ok": True,
                "clip_url": scene["clip_url"],
                "clip_path": clip_path,
                "filename": filename,
                "size": len(file_part["data"]),
            })
        except Exception as e:
            self._send_json({"error": f"Upload failed: {str(e)[:200]}"}, 500)

    def _handle_scene_upload_frame(self, scene_index):
        """Upload an external image as a scene's first frame."""
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "upload"):
            self._send_json({"error": "Rate limited — too many upload requests. Please wait a minute."}, 429)
            return
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

            plan = load_movie_plan(OUTPUT_DIR)
            if not plan or scene_index >= len(plan.get("scenes", [])):
                self._send_json({"error": "Invalid scene"}, 400)
                return

            # Save frame
            frames_dir = os.path.join(OUTPUT_DIR, "first_frames")
            os.makedirs(frames_dir, exist_ok=True)

            # Resize to project standard
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(file_part["data"]))
            img = img.convert("RGB")
            if img.width > 4096 or img.height > 4096:
                img.thumbnail((4096, 4096), Image.LANCZOS)

            frame_path = os.path.join(frames_dir, f"scene_{scene_index}_first.jpg")
            img.save(frame_path, "JPEG", quality=95)

            # Also save thumbnail
            os.makedirs(SCENE_THUMBNAILS_DIR, exist_ok=True)
            thumb_path = os.path.join(SCENE_THUMBNAILS_DIR, f"scene_{scene_index}.jpg")
            img.save(thumb_path, "JPEG", quality=90)

            # Update plan
            scene = plan["scenes"][scene_index]
            scene["first_frame_path"] = frame_path
            scene["first_frame_source"] = "upload"
            scene["preview"] = {
                "status": "ready",
                "image_url": f"/api/scene-thumbnails/scene_{scene_index}.jpg",
            }
            save_movie_plan(plan, OUTPUT_DIR)

            print(f"[UPLOAD] Frame uploaded for scene {scene_index}: {img.width}x{img.height}")

            self._send_json({
                "ok": True,
                "first_frame_url": f"/api/scene-thumbnails/scene_{scene_index}.jpg",
                "frame_path": frame_path,
                "width": img.width,
                "height": img.height,
            })
        except Exception as e:
            self._send_json({"error": f"Upload failed: {str(e)[:200]}"}, 500)

    def _handle_generate_first_frame(self, scene_index):
        """Generate ONLY a first frame image for a scene (fast, no video)."""
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "generate"):
            self._send_json({"error": "Rate limited — too many generation requests. Please wait a minute."}, 429)
            return
        # Read optional body for seed
        body_data = {}
        try:
            cl = int(self.headers.get("Content-Length", 0))
            if cl > 0:
                raw_body = self.rfile.read(cl)
                body_data = json.loads(raw_body)
        except Exception:
            pass

        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No plan"}, 400)
            return
        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": "Invalid index"}, 400)
            return

        scene = scenes[scene_index]

        # Apply seed from request body if provided
        if body_data.get("seed") is not None:
            scene["seed"] = body_data["seed"]

        shot_type = scene.get("shot_type", "medium")

        # Enrich scene to resolve photo paths (also fixes stale IDs by name match)
        enriched = dict(scene)
        _enrich_scene_with_assets(enriched)
        # Persist any auto-fixed IDs back to the plan
        for key in ("characters", "costumes", "environments"):
            if enriched.get(key):
                scene[key] = enriched[key]
        save_movie_plan(plan, OUTPUT_DIR)

        # Load project style lock and inject into prompt
        project_style = _prompt_os.get_project_style()
        style_prefix = ""
        if project_style:
            style_parts = []
            if project_style.get("worldSetting"):
                style_parts.append(f"Setting: {project_style['worldSetting']}.")
            if project_style.get("tone"):
                style_parts.append(f"Tone: {project_style['tone']}.")
            if project_style.get("visualLanguage"):
                style_parts.append(project_style["visualLanguage"] + ".")
            if project_style.get("colorPalette"):
                style_parts.append(f"Colors: {project_style['colorPalette']}.")
            if project_style.get("textureMaterial"):
                style_parts.append(f"Materials: {project_style['textureMaterial']}.")
            if project_style.get("cameraLanguage"):
                style_parts.append(f"Camera style: {project_style['cameraLanguage']}.")
            if style_parts:
                style_prefix = " ".join(style_parts) + " "

        # --- Collect all candidate photos by type ---
        char_photos = []
        char_tags = []

        # Multiple characters from scene.characters array
        chars_in_scene = enriched.get("characters", [])
        if chars_in_scene:
            for ci, char_ref in enumerate(chars_in_scene):
                cid = char_ref.get("id", "") if isinstance(char_ref, dict) else ""
                if cid:
                    pc = _prompt_os.get_character(cid)
                    if pc:
                        ref_img = pc.get("referencePhoto", "") or pc.get("referenceImagePath", "")
                        photo = ""
                        if ref_img and os.path.isfile(ref_img):
                            photo = ref_img
                        else:
                            import re as _re_ff
                            _m = _re_ff.search(r"/api/pos/characters/([^/]+)/photo", ref_img or "")
                            if _m:
                                for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    _cand = os.path.join(POS_PHOTOS_CHARS_DIR, f"{_m.group(1)}{_ext}")
                                    if os.path.isfile(_cand):
                                        photo = _cand
                                        break
                        if photo:
                            tag = pc.get("name", f"Char{ci}").replace(" ", "")[:16]
                            if len(tag) < 3:
                                tag = tag + "Ref"
                            # Prefer approved sheet assets over raw reference photo
                            if shot_type == "close-up" and pc.get("approvedFaceCloseUp"):
                                face_url = pc["approvedFaceCloseUp"]
                                face_path = _resolve_sheet_or_photo(face_url)
                                if face_path:
                                    # Replace with face close-up for close-up shots
                                    char_photos = [{"path": face_path, "tag": tag}]
                                    char_tags = [tag]
                                    break  # Use this one
                            elif pc.get("approvedSheet"):
                                sheet_url = pc["approvedSheet"]
                                sheet_path = _resolve_sheet_or_photo(sheet_url)
                                if sheet_path:
                                    # Use approved sheet instead of raw photo
                                    photo = sheet_path
                            char_photos.append({"path": photo, "tag": tag})
                            char_tags.append(tag)
        else:
            # Fallback: single character photo
            char_photo = enriched.get("character_photo_path", "")
            if char_photo and os.path.isfile(char_photo):
                char_photos.append({"path": char_photo, "tag": "Character"})
                char_tags.append("Character")

        costume_photos = []
        # Check for approved costume sheets first
        costumes_in_scene = enriched.get("costumes", [])
        if costumes_in_scene:
            for cos_ref in costumes_in_scene:
                cos_id = cos_ref.get("id", "") if isinstance(cos_ref, dict) else ""
                if cos_id:
                    cos_obj = _prompt_os.get_costume(cos_id)
                    if cos_obj:
                        # Prefer approved sheet
                        if cos_obj.get("approvedSheet"):
                            cos_path = _resolve_sheet_or_photo(cos_obj["approvedSheet"])
                            if cos_path:
                                costume_photos.append({"path": cos_path, "tag": "Costume"})
                                break
                        # Fallback to reference photo
                        cos_ref_img = cos_obj.get("referenceImagePath", "")
                        if cos_ref_img:
                            cos_path = _resolve_sheet_or_photo(cos_ref_img)
                            if not cos_path:
                                # Try photo directory
                                for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    _cand = os.path.join(POS_PHOTOS_COSTUMES_DIR, f"{cos_id}{_ext}")
                                    if os.path.isfile(_cand):
                                        cos_path = _cand
                                        break
                            if cos_path:
                                costume_photos.append({"path": cos_path, "tag": "Costume"})
                                break
        if not costume_photos:
            cos_photo = enriched.get("costume_photo_path", "")
            if cos_photo and os.path.isfile(cos_photo):
                costume_photos.append({"path": cos_photo, "tag": "Costume"})

        env_photos = []
        # Check for approved environment sheets first
        envs_in_scene = enriched.get("environments", [])
        if envs_in_scene:
            for env_ref in envs_in_scene:
                env_id = env_ref.get("id", "") if isinstance(env_ref, dict) else ""
                if env_id:
                    env_obj = _prompt_os.get_environment(env_id)
                    if env_obj:
                        if env_obj.get("approvedSheet"):
                            env_path = _resolve_sheet_or_photo(env_obj["approvedSheet"])
                            if env_path:
                                env_photos.append({"path": env_path, "tag": "Setting"})
                                break
                        env_ref_img = env_obj.get("referenceImagePath", "")
                        if env_ref_img:
                            env_path = _resolve_sheet_or_photo(env_ref_img)
                            if not env_path:
                                for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                                    _cand = os.path.join(POS_PHOTOS_ENVS_DIR, f"{env_id}{_ext}")
                                    if os.path.isfile(_cand):
                                        env_path = _cand
                                        break
                            if env_path:
                                env_photos.append({"path": env_path, "tag": "Setting"})
                                break
        if not env_photos:
            env_photo = enriched.get("environment_photo_path", "")
            if env_photo and os.path.isfile(env_photo):
                env_photos.append({"path": env_photo, "tag": "Setting"})

        # Warn about low-resolution references
        for photo_entry in char_photos + costume_photos + env_photos:
            photo_path = photo_entry.get("path", "")
            if photo_path and os.path.isfile(photo_path):
                try:
                    from PIL import Image
                    with Image.open(photo_path) as _img:
                        w, h = _img.size
                        if w < 512 or h < 512:
                            print(f"[QUALITY WARNING] Low-res reference: {os.path.basename(photo_path)} is {w}x{h} — recommend 1024+ for better detail")
                except Exception:
                    pass

        # --- Use shot type system to select refs ---
        refs = select_refs_for_shot_type(shot_type, char_photos, costume_photos, env_photos, max_refs=3)

        if not refs:
            # Build specific guidance about what's missing
            missing = []
            if not char_photos:
                missing.append("character photo")
            if not costume_photos:
                missing.append("costume photo")
            if not env_photos:
                missing.append("environment photo")
            if missing:
                guidance = "Missing: " + ", ".join(missing) + ". Upload reference photos in the ASSETS tab."
            else:
                guidance = "No reference photos available for this scene. Upload photos in the ASSETS tab."
            self._send_json({"error": guidance}, 400)
            return

        print(f"[FIRST FRAME][{scene_index}] Shot type: {shot_type} | Selected refs: {[(r['tag'], r['priority']) for r in refs]}")

        # --- Build shot-type-aware prompt ---
        has_char = any(r["tag"] in char_tags for r in refs)
        has_costume = any(r["tag"] == "Costume" for r in refs)
        has_env = any(r["tag"] == "Setting" for r in refs)

        import re as _re_tag
        # Use enriched prompt with cinematic detail fallbacks
        prompt = _enrich_scene_prompt(scene, project_style)

        # Clean prompt: remove inline character descriptions when we have photo refs
        # The @tag reference image defines identity — text descriptions fight with it
        if char_photos:
            # Remove "name (role) — Long description. Face: details. More details." patterns
            # This handles multi-sentence character descriptions embedded in scene prompts
            prompt = _re_tag.sub(r'\b\w+\s*\([^)]*\)\s*[—–-]\s*[^,;]*(?:\.\s*(?:Face|Hair|Skin|Body|Eyes|Ears|Build|Height|Posture)[^.]*)*\.?', '', prompt)
            # Remove standalone character description blocks (Silver anthropomorphic...)
            prompt = _re_tag.sub(r'(?:Silver|Gold|Bronze|Metallic)\s+anthropomorphic[^.;]*[.;]?', '', prompt)
            # Remove "Face: ..." / "Hair: ..." / "Build: ..." standalone descriptions
            prompt = _re_tag.sub(r'(?:Face|Hair|Skin tone|Body type|Build|Eyes|Ears|Posture|Height):\s*[^.;]*[.;]', '', prompt)
            # Remove "[reference photo available]" and similar brackets
            prompt = _re_tag.sub(r'\[(?:reference photo|photo|ref)[^]]*\]', '', prompt)
            # Remove any "character name — description" that slipped through (generic catch)
            for ct in char_tags:
                # Strip "CharName (role) — multi-sentence description..." blocks
                clean_name = _re_tag.escape(ct.replace("Ref", ""))
                if clean_name and len(clean_name) >= 2:
                    # Greedy: match name + optional role + dash + everything until next scene action keyword or double newline
                    prompt = _re_tag.sub(rf'(?:^|\s){clean_name}\s*\([^)]*\)\s*[—–-]\s*.*?(?=\.\s*(?:Setting|Camera|Transition|Shifting|Opening|Closing|The\s|In\s|A\s)|$)', '', prompt, flags=_re_tag.IGNORECASE | _re_tag.DOTALL)
                    # Also catch without parenthetical: "tb — Silver anthropomorphic..."
                    prompt = _re_tag.sub(rf'(?:^|\s){clean_name}\s+[—–-]\s*[^.]*(?:\.\s*(?:Face|Hair|Skin|Body|Eyes|Build)[^.]*)*\.?', '', prompt, flags=_re_tag.IGNORECASE)
            # Clean up whitespace, dangling commas, double periods
            prompt = _re_tag.sub(r',\s*,', ',', prompt)
            prompt = _re_tag.sub(r'\.\s*\.', '.', prompt)
            prompt = _re_tag.sub(r'\s{2,}', ' ', prompt).strip()
            prompt = prompt.strip(' ,;—–-')

        tag_prompt = prompt
        # Remove any remaining vision-API artifacts
        tag_prompt = _re_tag.sub(r'\[reference photo available\]', '', tag_prompt)
        tag_prompt = _re_tag.sub(r'\s{2,}', ' ', tag_prompt).strip()

        # Prepend project style to prompt
        if style_prefix:
            tag_prompt = style_prefix + tag_prompt

        # Build shot-aware prompt (handles quality keywords + shot framing)
        tag_prompt = build_shot_prompt(shot_type, tag_prompt, has_char, has_costume, has_env)

        # Auto-inject continuity for non-first scenes
        if scene_index > 0:
            prev_scene = scenes[scene_index - 1] if scene_index < len(scenes) else None
            if prev_scene:
                continuity_text = "Continuing from the previous scene. Maintain visual continuity: same color palette, same lighting, same time of day."
                # Check if same character appears
                prev_chars = set()
                for c in (prev_scene.get("characters") or []):
                    cid = c.get("id", "") if isinstance(c, dict) else ""
                    if cid: prev_chars.add(cid)
                curr_chars = set()
                for c in (scene.get("characters") or []):
                    cid = c.get("id", "") if isinstance(c, dict) else ""
                    if cid: curr_chars.add(cid)
                if prev_chars & curr_chars:
                    continuity_text += " The same character continues to appear — maintain identical appearance."

                tag_prompt = tag_prompt + " " + continuity_text
                tag_prompt = tag_prompt[:1000]

        # Add @tag refs — let the PHOTOS define identity
        selected_char_tags = [r["tag"] for r in refs if r["tag"] in char_tags]
        for ct in selected_char_tags:
            if f"@{ct}" not in tag_prompt:
                tag_prompt = f"@{ct} " + tag_prompt
        if has_costume:
            if "@Costume" not in tag_prompt:
                tag_prompt = f"{tag_prompt} Wearing the exact outfit from @Costume."
        if has_env:
            if "@Setting" not in tag_prompt:
                tag_prompt = f"{tag_prompt} Set in the exact location from @Setting."
        tag_prompt = tag_prompt[:1000]  # API limit

        # Add negative prompt from project style
        if project_style and project_style.get("negativePrompt"):
            neg = project_style["negativePrompt"]
            tag_prompt = tag_prompt + f" AVOID: {neg}"
            tag_prompt = tag_prompt[:1000]

        # Seed for reproducibility
        scene_seed = scene.get("seed")
        if scene_seed is not None:
            try:
                scene_seed = int(scene_seed)
            except (ValueError, TypeError):
                scene_seed = None
        # Store seed for reproducibility — generate one if not provided
        if scene_seed is None:
            import random
            scene_seed = random.randint(0, 4294967295)
        scene["last_seed"] = scene_seed

        try:
            from lib.video_generator import _runway_generate_scene_image
            print(f"[FIRST FRAME][{scene_index}] Generating with {len(refs)} refs: {[r['tag'] for r in refs]}, seed={scene_seed}")

            # Get aspect ratio from project settings for higher-res output
            settings = _load_settings()
            ds = settings.get("director_state", {})
            aspect = ds.get("aspect_ratio", "16:9")
            ratio_map = {
                "16:9": "1920:1080",
                "9:16": "1080:1920",
                "1:1": "1024:1024",
                "4:3": "1536:1152",
                "3:4": "1152:1536",
            }
            gen_ratio = ratio_map.get(aspect, "1920:1080")

            img_path = _runway_generate_scene_image(
                tag_prompt, refs,
                ratio=gen_ratio,
                model="gen4_image",
                seed=scene_seed,
            )

            if not img_path or not os.path.isfile(img_path):
                self._send_json({"error": "Image generation failed"}, 500)
                return

            # Save as first frame
            first_frame_dir = os.path.join(OUTPUT_DIR, "first_frames")
            os.makedirs(first_frame_dir, exist_ok=True)
            first_frame_path = os.path.join(first_frame_dir, f"scene_{scene_index}_first.jpg")
            import shutil
            shutil.copy2(img_path, first_frame_path)

            # Also save as thumbnail
            os.makedirs(SCENE_THUMBNAILS_DIR, exist_ok=True)
            thumb_path = os.path.join(SCENE_THUMBNAILS_DIR, f"scene_{scene_index}.jpg")
            shutil.copy2(img_path, thumb_path)

            # Update plan with first_frame_path and preview
            scene["first_frame_path"] = first_frame_path
            scene["_lastGeneratedPrompt"] = prompt
            scene["preview"] = {
                "status": "ready",
                "image_url": f"/api/scene-thumbnails/scene_{scene_index}.jpg",
                "last_generated_at": datetime.utcnow().isoformat(),
                "engine": "gen4_image_turbo",
            }
            scenes[scene_index] = scene
            save_movie_plan(plan, OUTPUT_DIR)

            print(f"[FIRST FRAME][{scene_index}] Saved: {first_frame_path}")

            # Check ref quality for UI warning
            ref_warnings = []
            for photo_entry in char_photos + costume_photos + env_photos:
                pp = photo_entry.get("path", "")
                if pp and os.path.isfile(pp):
                    try:
                        from PIL import Image
                        with Image.open(pp) as _qi:
                            qw, qh = _qi.size
                            if qw < 512 or qh < 512:
                                ref_warnings.append(f"{photo_entry.get('tag', 'ref')} photo is only {qw}x{qh} — upload a higher resolution for better detail")
                    except Exception:
                        pass

            self._send_json({"ok": True, "first_frame_path": first_frame_path,
                             "preview_url": f"/api/scene-thumbnails/scene_{scene_index}.jpg",
                             "scene": scene,
                             "ref_warnings": ref_warnings})

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": str(e)[:300]}, 500)

    def _handle_multi_angle_clip(self, scene_index, body):
        """Generate a multi-angle clip using keyframe interpolation.

        1. Generate first frame at shot_type_start (e.g., wide establishing)
        2. Generate last frame at shot_type_end (e.g., close-up)
        3. Send both as keyframes to image_to_video
        4. Runway interpolates between angles in one clip
        """
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "generate"):
            self._send_json({"error": "Rate limited — too many generation requests. Please wait a minute."}, 429)
            return
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No plan"}, 400)
            return
        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": "Invalid scene index"}, 400)
            return

        scene = scenes[scene_index]

        # Shot types for start and end of the clip
        start_type = body.get("startShotType", "wide")
        end_type = body.get("endShotType", "close-up")
        duration = max(4, min(10, int(body.get("duration", 8))))  # Min 4s for multi-angle

        # Use existing first frame if available, or generate new ones
        enriched = dict(scene)
        _enrich_scene_with_assets(enriched)

        prompt = scene.get("shot_prompt", scene.get("prompt", "Cinematic scene"))

        # Collect refs
        char_photos = []
        chars = enriched.get("characters", [])
        for char_ref in chars:
            cid = char_ref.get("id", "") if isinstance(char_ref, dict) else ""
            if cid:
                pc = _prompt_os.get_character(cid)
                if pc:
                    ref_img = pc.get("referencePhoto", "") or pc.get("referenceImagePath", "")
                    import re as _mre
                    _m = _mre.search(r"/api/pos/characters/([^/]+)/photo", ref_img or "")
                    if _m:
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            cand = os.path.join(POS_PHOTOS_CHARS_DIR, f"{_m.group(1)}{ext}")
                            if os.path.isfile(cand):
                                tag = pc.get("name", "Char").replace(" ", "")[:16]
                                if len(tag) < 3: tag += "Ref"
                                char_photos.append({"path": cand, "tag": tag})
                                break

        env_photos = []
        envs = enriched.get("environments", [])
        for env_ref in envs:
            eid = env_ref.get("id", "") if isinstance(env_ref, dict) else ""
            if eid:
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    cand = os.path.join(POS_PHOTOS_ENVS_DIR, f"{eid}{ext}")
                    if os.path.isfile(cand):
                        env_photos.append({"path": cand, "tag": "Setting"})
                        break

        all_refs = (char_photos + env_photos)[:3]

        if not all_refs:
            self._send_json({"error": "No reference photos — upload character or environment photos first"}, 400)
            return

        try:
            from lib.video_generator import (
                _runway_generate_scene_image, _runway_submit_text_to_video,
                _runway_poll, _download, build_shot_prompt,
            )

            # Step 1: Generate FIRST frame (start angle)
            has_char = len(char_photos) > 0
            has_env = len(env_photos) > 0

            start_prompt = build_shot_prompt(start_type, prompt, has_char, False, has_env)
            # Add @tags
            for ref in all_refs:
                if f"@{ref['tag']}" not in start_prompt:
                    start_prompt = f"@{ref['tag']} {start_prompt}"
            start_prompt = start_prompt[:1000]

            print(f"[MULTI-ANGLE][{scene_index}] Generating start frame ({start_type})...")
            first_path = _runway_generate_scene_image(start_prompt, all_refs, ratio="1280:720", model="gen4_image")

            if not first_path or not os.path.isfile(first_path):
                self._send_json({"error": f"Failed to generate {start_type} frame"}, 500)
                return

            # Step 2: Generate LAST frame (end angle)
            end_prompt = build_shot_prompt(end_type, prompt, has_char, False, has_env)
            for ref in all_refs:
                if f"@{ref['tag']}" not in end_prompt:
                    end_prompt = f"@{ref['tag']} {end_prompt}"
            end_prompt = end_prompt[:1000]

            print(f"[MULTI-ANGLE][{scene_index}] Generating end frame ({end_type})...")
            last_path = _runway_generate_scene_image(end_prompt, all_refs, ratio="1280:720", model="gen4_image")

            if not last_path or not os.path.isfile(last_path):
                # Fall back to single-frame if last frame fails
                print(f"[MULTI-ANGLE][{scene_index}] Last frame failed, using single-frame mode")
                last_path = None

            # Step 3: Generate video with both keyframes
            video_prompt = f"Camera smoothly transitions from {start_type} to {end_type}. Cinematic movement, professional cinematography, smooth motion."

            # Get engine
            engine = scene.get("engine", "")
            if not engine:
                settings = _load_settings()
                engine = settings.get("director_state", {}).get("engine", "gen4_5")

            print(f"[MULTI-ANGLE][{scene_index}] Generating clip: {start_type} -> {end_type}, {duration}s")

            task_id = _runway_submit_text_to_video(
                video_prompt, duration=duration, model=engine,
                first_frame_path=first_path,
                last_frame_path=last_path,
            )

            video_info = _runway_poll(task_id)

            # Download clip
            clips_dir = os.path.join(OUTPUT_DIR, "auto_director", "clips")
            os.makedirs(clips_dir, exist_ok=True)
            clip_filename = f"scene_{scene_index}_multiangle_{int(time.time())}.mp4"
            clip_path = os.path.join(clips_dir, clip_filename)
            _download(video_info["url"], clip_path)

            # Save first frames
            first_frame_dir = os.path.join(OUTPUT_DIR, "first_frames")
            os.makedirs(first_frame_dir, exist_ok=True)
            import shutil
            ff_path = os.path.join(first_frame_dir, f"scene_{scene_index}_first.jpg")
            shutil.copy2(first_path, ff_path)

            # Update scene
            scene["clip_path"] = clip_path
            scene["clip_url"] = f"/api/clips/{clip_filename}"
            scene["first_frame_path"] = ff_path
            scene["has_clip"] = True
            scene["multi_angle"] = True
            scene["multi_angle_start"] = start_type
            scene["multi_angle_end"] = end_type
            scenes[scene_index] = scene
            save_movie_plan(plan, OUTPUT_DIR)

            self._send_json({
                "ok": True,
                "clip_url": scene["clip_url"],
                "first_frame_url": f"/api/scene-thumbnails/scene_{scene_index}.jpg",
                "start_type": start_type,
                "end_type": end_type,
                "duration": duration,
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": f"Multi-angle error: {str(e)[:300]}"}, 500)

    def _handle_scene_restyle(self, scene_index, body):
        """Restyle an existing clip using video_to_video (gen4_aleph)."""
        from lib.video_generator import _runway_video_to_video, _download
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan:
            self._send_json({"error": "No plan found"}, 404)
            return
        scenes = plan.get("scenes", [])
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": f"Scene {scene_index} out of range"}, 404)
            return
        scene = scenes[scene_index]
        clip_path = scene.get("clip_path", "")
        if not clip_path or not os.path.isfile(clip_path):
            self._send_json({"error": "No clip file found for this scene. Generate a clip first."}, 400)
            return
        style = body.get("style", "")
        if not style:
            self._send_json({"error": "No style description provided"}, 400)
            return
        try:
            prompt = f"Restyle this video: {style}. Maintain all motion, action, and composition. Change only the visual style."
            restyled_path = _runway_video_to_video(clip_path, prompt)
            if not restyled_path or not os.path.isfile(restyled_path):
                self._send_json({"error": "Restyle generation failed — no video returned"}, 500)
                return
            # Save to clips directory
            clips_dir = os.path.join(OUTPUT_DIR, "auto_director", "clips")
            os.makedirs(clips_dir, exist_ok=True)
            dest = os.path.join(clips_dir, f"scene_{scene_index}_restyled_{int(time.time())}.mp4")
            import shutil
            shutil.copy2(restyled_path, dest)
            scene["clip_path"] = dest
            scene["clip_url"] = f"/api/clips/{os.path.basename(dest)}"
            scene["restyled"] = True
            scene["restyle_prompt"] = style
            save_movie_plan(plan, OUTPUT_DIR)
            self._send_json({
                "ok": True,
                "clip_url": scene["clip_url"],
                "restyled_url": scene["clip_url"],
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": f"Restyle error: {str(e)[:300]}"}, 500)

    def _handle_character_performance(self, scene_index, body):
        """Generate character performance video using act_two model.

        Takes a character image + reference performance video and generates
        a new video where the character performs the same movements/expressions.
        """
        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No plan"}, 400)
            return
        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": "Invalid scene index"}, 400)
            return

        scene = scenes[scene_index]

        # Get character image -- prefer first frame, then character photo
        char_image_path = scene.get("first_frame_path", "")
        if not char_image_path or not os.path.isfile(char_image_path):
            # Try character reference photo
            enriched = dict(scene)
            _enrich_scene_with_assets(enriched)
            char_image_path = enriched.get("character_photo_path", "")

        if not char_image_path or not os.path.isfile(char_image_path):
            self._send_json({"error": "No character image available. Generate a first frame or upload a character photo."}, 400)
            return

        # Reference video can come from body or from a previously generated clip
        ref_video_path = body.get("referenceVideoPath", "")
        if not ref_video_path:
            # Check if there's an uploaded performance reference
            perf_dir = os.path.join(OUTPUT_DIR, "performance_refs")
            if os.path.isdir(perf_dir):
                for f in os.listdir(perf_dir):
                    if f.endswith(('.mp4', '.webm', '.mov')):
                        ref_video_path = os.path.join(perf_dir, f)
                        break

        if not ref_video_path or not os.path.isfile(ref_video_path):
            self._send_json({"error": "No reference performance video. Upload a 3-30 second video of the performance you want the character to replicate."}, 400)
            return

        body_control = body.get("bodyControl", True)
        expression_intensity = body.get("expressionIntensity", 3)

        try:
            import base64
            import requests
            from lib.video_generator import _photo_to_data_uri

            # Build character image URI
            char_uri = _photo_to_data_uri(char_image_path)

            # Build reference video URI
            with open(ref_video_path, "rb") as vf:
                video_bytes = vf.read()
            video_b64 = base64.b64encode(video_bytes).decode("ascii")
            video_uri = f"data:video/mp4;base64,{video_b64}"

            payload = {
                "model": "act_two",
                "character": {"type": "image", "uri": char_uri},
                "reference": {"type": "video", "uri": video_uri},
                "bodyControl": body_control,
                "expressionIntensity": max(1, min(5, int(expression_intensity))),
                "contentModeration": {"publicFigureThreshold": "low"},
            }

            print(f"[ACT_TWO][{scene_index}] Submitting character performance: bodyControl={body_control}, intensity={expression_intensity}")

            resp = requests.post(
                f"{RUNWAY_API_BASE}/character_performance",
                headers=_runway_headers(),
                json=payload,
                timeout=120,
            )

            if resp.status_code not in (200, 201):
                err = resp.text[:500]
                print(f"[ACT_TWO] Error {resp.status_code}: {err}")
                self._send_json({"error": f"Act Two API error: {err[:200]}"}, resp.status_code)
                return

            data = resp.json()
            task_id = data.get("id", "")
            if not task_id:
                self._send_json({"error": "No task ID returned from Act Two"}, 500)
                return

            print(f"[ACT_TWO][{scene_index}] Task: {task_id}")
            result = _runway_poll(task_id)

            # Download result
            clips_dir = os.path.join(OUTPUT_DIR, "auto_director", "clips")
            os.makedirs(clips_dir, exist_ok=True)
            clip_path = os.path.join(clips_dir, f"scene_{scene_index}_performance_{int(time.time())}.mp4")
            _download(result["url"], clip_path)

            # Update scene
            scene["clip_path"] = clip_path
            scene["clip_url"] = f"/api/clips/{os.path.basename(clip_path)}"
            scene["has_clip"] = True
            scene["performance_mode"] = True
            scenes[scene_index] = scene
            save_movie_plan(plan, OUTPUT_DIR)

            self._send_json({
                "ok": True,
                "clip_url": scene["clip_url"],
                "clip_path": clip_path,
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": f"Character performance error: {str(e)[:300]}"}, 500)

    def _handle_generate_scene_clip(self, scene_index):
        """Generate a video clip for a scene using image_to_video with its first frame."""
        ip = self.client_address[0]
        if not _check_rate_limit(ip, "generate"):
            self._send_json({"error": "Rate limited — too many generation requests. Please wait a minute."}, 429)
            return
        # Read optional body for seed
        clip_body_data = {}
        try:
            cl = int(self.headers.get("Content-Length", 0))
            if cl > 0:
                raw_body = self.rfile.read(cl)
                clip_body_data = json.loads(raw_body)
        except Exception:
            pass

        plan = load_movie_plan(OUTPUT_DIR)
        if not plan or not plan.get("scenes"):
            self._send_json({"error": "No plan"}, 400)
            return
        scenes = plan["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            self._send_json({"error": "Invalid index"}, 400)
            return

        scene = scenes[scene_index]

        # Apply seed from request body if provided
        if clip_body_data.get("seed") is not None:
            scene["seed"] = clip_body_data["seed"]

        first_frame = scene.get("first_frame_path", "")

        if not first_frame or not os.path.isfile(first_frame):
            self._send_json({"error": "No first frame. Generate a first frame first."}, 400)
            return

        prompt = scene.get("shot_prompt", scene.get("prompt", ""))
        duration = max(2, min(10, int(scene.get("duration", 5))))

        # Get engine
        enriched = dict(scene)
        _enrich_scene_with_assets(enriched)
        engine = scene.get("engine", "")
        if not engine:
            settings = _load_settings()
            ds = settings.get("director_state", {})
            engine = ds.get("engine", "") or settings.get("default_engine", "gen4_5")

        try:
            from lib.video_generator import _runway_submit_text_to_video, _runway_poll, _download

            print(f"[CLIP][{scene_index}] Generating clip: engine={engine}, duration={duration}s, first_frame={first_frame}")

            # For image_to_video: focus prompt on MOTION and ACTION, not visual description.
            # The first frame image IS the visual reference — the prompt should describe
            # what HAPPENS, not what things look like.
            shot_type = scene.get("shot_type", "medium")
            action = scene.get("action", "") or scene.get("visual_description", "") or ""
            camera_move = scene.get("camera", "") or scene.get("camera_movement", "") or ""

            video_prompt_parts = []
            # Action/motion from scene
            if action and action != prompt:
                video_prompt_parts.append(action)
            else:
                video_prompt_parts.append(prompt)
            # Camera movement
            if camera_move:
                video_prompt_parts.append(f"Camera: {camera_move}.")
            # Shot-type motion hints
            if shot_type == "close-up":
                video_prompt_parts.append("Subtle facial movement, breathing, natural micro-expressions. Maintain exact likeness throughout.")
            elif shot_type in ("wide", "establishing"):
                video_prompt_parts.append("Atmospheric motion, environmental animation, cinematic sweep.")
            else:
                video_prompt_parts.append("Natural, fluid motion. Cinematic quality.")
            video_prompt_parts.append("Smooth motion, no artifacts, consistent lighting.")
            video_prompt = " ".join(video_prompt_parts)[:1000]

            # Load project style for video prompt
            project_style = _prompt_os.get_project_style()
            if project_style:
                vid_style_parts = []
                if project_style.get("tone"):
                    vid_style_parts.append(project_style["tone"])
                if project_style.get("visualLanguage"):
                    vid_style_parts.append(project_style["visualLanguage"])
                if vid_style_parts:
                    video_prompt = " ".join(vid_style_parts) + ". " + video_prompt
                    video_prompt = video_prompt[:1000]

            # Seed for reproducibility
            clip_seed = scene.get("seed")
            if clip_seed is not None:
                try:
                    clip_seed = int(clip_seed)
                except (ValueError, TypeError):
                    clip_seed = None
            if clip_seed is None:
                import random
                clip_seed = random.randint(0, 4294967295)
            scene["last_seed"] = clip_seed

            task_id = _runway_submit_text_to_video(
                video_prompt, duration=duration, model=engine,
                first_frame_path=first_frame,
                seed=clip_seed,
            )

            print(f"[CLIP][{scene_index}] Task submitted: {task_id[:16]}..., seed={clip_seed}")
            video_info = _runway_poll(task_id)

            # Download clip
            clips_dir = os.path.join(OUTPUT_DIR, "auto_director")
            os.makedirs(clips_dir, exist_ok=True)
            clip_path = os.path.join(clips_dir, f"clip_{scene_index:03d}.mp4")
            _download(video_info["url"], clip_path)

            # Update scene
            scene["clip_path"] = clip_path
            scene["has_clip"] = True
            scene["status"] = "done"
            scenes[scene_index] = scene
            # Save to BOTH plan files so UI and backend stay in sync
            save_movie_plan(plan, OUTPUT_DIR)
            with open(AUTO_DIRECTOR_PLAN_PATH, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)

            _record_cost(f"clip_{scene_index}", "video")
            print(f"[CLIP][{scene_index}] Clip saved: {clip_path}")

            self._send_json({
                "ok": True,
                "clip_path": clip_path,
                "clip_url": f"/api/clips/{os.path.basename(clip_path)}",
                "scene": scene,
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": str(e)[:300]}, 500)

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
        body = json.loads(self._read_body()) if self.headers.get("Content-Length") else {}
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


def _kill_stale_servers(port):
    """Kill any existing processes listening on our port before starting."""
    import subprocess as _sp
    try:
        # Find PIDs on our port
        result = _sp.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5
        )
        pids = set()
        my_pid = os.getpid()
        for line in result.stdout.split("\n"):
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                if parts:
                    try:
                        pid = int(parts[-1])
                        if pid != my_pid and pid != 0:
                            pids.add(pid)
                    except ValueError:
                        pass

        if pids:
            print(f"  Cleaning up {len(pids)} stale server process(es) on port {port}...")
            for pid in pids:
                try:
                    _sp.run(["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True, timeout=5)
                except Exception:
                    pass
            # Wait for port to free
            import time as _t
            _t.sleep(2)
            print(f"  Cleanup done.")
    except Exception as e:
        print(f"  Port cleanup skipped: {e}")


def _write_pid_file():
    """Write PID file so we can find ourselves later."""
    pid_path = os.path.join(OUTPUT_DIR, "server.pid")
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid_file():
    """Remove PID file on shutdown."""
    pid_path = os.path.join(OUTPUT_DIR, "server.pid")
    try:
        os.remove(pid_path)
    except OSError:
        pass


def main():
    # Kill any stale servers before starting
    _kill_stale_servers(PORT)

    # Allow port reuse
    import socketserver
    socketserver.TCPServer.allow_reuse_address = True
    HTTPServer.allow_reuse_address = True

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.allow_reuse_address = True

    _write_pid_file()

    print(f"\n  LUMN Studio")
    print(f"  UI running at http://localhost:{PORT}")
    print(f"  PID: {os.getpid()}")
    print(f"  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
        _remove_pid_file()
        print("Server stopped.")


if __name__ == "__main__":
    main()
