#!/usr/bin/env python3
"""
Music Video Generator - Web UI Server
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

import json
import os
import re
import subprocess
import sys
import threading
import time

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
)
from lib.prompt_assistant import (
    STYLE_PRESETS, get_preset, enhance_prompt, suggest_from_song_name,
    get_preset_names, suggest_style, suggest_genre_from_bpm,
    extract_palette,
)
from lib.storyboard_generator import generate_storyboard
from lib.project_manager import ProjectManager
PORT = 3849
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(PROJECT_DIR, "uploads")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

from lib.prompt_os import PromptOS
_prompt_os = PromptOS()
PROMPT_OS_DATA_DIR = os.path.join(OUTPUT_DIR, "prompt_os")
os.makedirs(PROMPT_OS_DATA_DIR, exist_ok=True)
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
SCENE_VIDEOS_DIR = os.path.join(UPLOADS_DIR, "scene_videos")
SCENE_VOCALS_DIR = os.path.join(UPLOADS_DIR, "scene_vocals")
FULL_PROJECTS_DIR = os.path.join(OUTPUT_DIR, "full_projects")
SETTINGS_PATH = os.path.join(OUTPUT_DIR, "settings.json")
PROMPT_HISTORY_PATH = os.path.join(OUTPUT_DIR, "prompt_history.json")
AUTOSAVE_PATH = os.path.join(OUTPUT_DIR, "autosave.json")
TEMPLATES_DIR = os.path.join(OUTPUT_DIR, "templates")
GIFS_DIR = os.path.join(OUTPUT_DIR, "gifs")

# Render time estimation constants (seconds per clip by engine)
RENDER_TIME_ESTIMATES = {
    "grok": 30,
    "runway": 60,
    "luma": 45,
    "openai": 50,
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
os.makedirs(SCENE_VIDEOS_DIR, exist_ok=True)
os.makedirs(SCENE_VOCALS_DIR, exist_ok=True)
os.makedirs(FULL_PROJECTS_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(GIFS_DIR, exist_ok=True)
os.makedirs(TAKES_DIR, exist_ok=True)

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

    with open(PROMPT_HISTORY_PATH, "w") as f:
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
    """Load scene plan from JSON."""
    if not os.path.isfile(SCENE_PLAN_PATH):
        return None
    with open(SCENE_PLAN_PATH, "r") as f:
        return json.load(f)


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

        # Generate clips
        with gen_lock:
            gen_state["phase"] = "generating"

        def on_progress(index, status):
            with gen_lock:
                if index < len(gen_state["progress"]):
                    gen_state["progress"][index]["status"] = status

        clip_paths = generate_all(scenes, CLIPS_DIR, progress_cb=on_progress, cost_cb=_record_cost)

        # Record prompts to history
        for i, scene in enumerate(scenes):
            _record_prompt_history(scene.get("prompt", ""), scene_index=i)

        valid = [c for c in clip_paths if c]
        if not valid:
            with gen_lock:
                gen_state["phase"] = "error"
                gen_state["error"] = "No clips were generated successfully"
                gen_state["running"] = False
            return

        # Save scene plan
        output_file = os.path.join(OUTPUT_DIR, "final_video.mp4")
        _save_scene_plan(scenes, clip_paths, song_path, output_file)

        # Stitch
        with gen_lock:
            gen_state["phase"] = "stitching"

        # Extract transitions for stitching
        scene_transitions = [s.get("transition", "crossfade") for s in scenes]
        stitch(clip_paths, song_path, output_file, transitions=scene_transitions)

        with gen_lock:
            gen_state["phase"] = "done"
            gen_state["output_file"] = output_file
            gen_state["running"] = False

    except Exception as e:
        with gen_lock:
            gen_state["phase"] = "error"
            gen_state["error"] = str(e)
            gen_state["running"] = False


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

        clip_path = generate_scene(scene, scene_index, CLIPS_DIR, progress_cb=on_progress, cost_cb=_record_cost)
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
    return {"default_engine": "grok", "character_references": {}}


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

        # Build the prompt - handle multi-photo compositing
        gen_prompt = scene["prompt"]
        scene_photo_path = None

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

        gen_scene = {
            "prompt": gen_prompt,
            "duration": scene.get("duration", 8),
            "camera_movement": scene.get("camera_movement", "zoom_in"),
            "engine": scene.get("engine", ""),
            "id": scene.get("id", ""),
            "continuity_mode": plan_continuity,
        }

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
            gen_scene = {
                "prompt": gen_prompt,
                "duration": scene.get("duration", 8),
                "camera_movement": scene.get("camera_movement", "zoom_in"),
                "engine": scene.get("engine", ""),
                "id": scene.get("id", ""),
                "continuity_mode": plan_continuity,
            }

            try:
                clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                           progress_cb=on_progress, cost_cb=_record_cost,
                                           photo_path=scene_photo_path)
                scene["clip_path"] = clip_path
                scene["has_clip"] = True
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
            }

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

        stitch(clip_paths, audio, output_path,
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
            self._send_file(os.path.join(OUTPUT_DIR, safe))

        elif path == "/api/project/save":
            self._handle_project_save()

        elif path == "/api/cost":
            self._handle_get_cost()

        elif path.startswith("/api/storyboard/"):
            fname = os.path.basename(path[len("/api/storyboard/"):])
            self._send_file(os.path.join(STORYBOARD_DIR, fname))

        elif path.startswith("/api/previews/"):
            fname = os.path.basename(path[len("/api/previews/"):])
            self._send_file(os.path.join(PREVIEWS_DIR, fname))

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

        elif path == "/api/prompt-history":
            self._handle_prompt_history()

        elif path == "/api/estimate-render":
            self._handle_estimate_render_time()

        elif path == "/api/project/autosave":
            autosave_path = os.path.join(OUTPUT_DIR, "autosave.json")
            if os.path.isfile(autosave_path):
                self._send_file(autosave_path, "application/json")
            else:
                self._send_json({"exists": False})

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

        # Roadmap: Boomerang clip
        elif re.match(r'^/api/manual/scene/([^/]+)/boomerang$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/boomerang$', path)
            self._handle_boomerang_clip(m.group(1))

        # Roadmap: Export GIF
        elif re.match(r'^/api/manual/scene/([^/]+)/export-gif$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/export-gif$', path)
            self._handle_export_gif(m.group(1))

        # Roadmap: Color palette from photo
        elif re.match(r'^/api/manual/scene/([^/]+)/palette$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)/palette$', path)
            self._handle_get_palette(m.group(1))

        # Roadmap: Prompt history
        elif path == "/api/prompt-history":
            self._handle_prompt_history()

        # Roadmap: Star a prompt
        elif path == "/api/prompt-history/star":
            self._handle_star_prompt()

        # Roadmap: Style mixing
        elif path == "/api/mix-styles":
            self._handle_mix_styles()

        # Roadmap: Emotion detection from lyrics
        elif path == "/api/detect-emotion":
            self._handle_detect_emotion()

        # Roadmap: Auto-save
        elif path == "/api/project/autosave":
            self._handle_autosave()

        # Roadmap: Style mixing
        elif path == "/api/mix-styles":
            self._handle_mix_styles()

        # Roadmap: Emotion detection
        elif path == "/api/detect-emotion":
            self._handle_detect_emotion()

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

        else:
            self.send_error(404)

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

        elif re.match(r'^/api/pos/scenes/([^/]+)$', path):
            m = re.match(r'^/api/pos/scenes/([^/]+)$', path)
            if _prompt_os.delete_scene(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

        # ──── Feature 1: Delete Project ────
        elif re.match(r'^/api/projects/([^/]+)$', path):
            m = re.match(r'^/api/projects/([^/]+)$', path)
            if _project_mgr.delete_project(m.group(1)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)

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
                entry["clip_url"] = f"/api/clips/{os.path.basename(clip_path)}"
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
            # Item 46: Previous clip for comparison
            prev_clip = s.get("previous_clip_path", "")
            entry["has_previous_clip"] = bool(prev_clip and os.path.isfile(prev_clip))
            if entry["has_previous_clip"]:
                entry["previous_clip_url"] = f"/api/clips/{os.path.basename(prev_clip)}"
            else:
                entry["previous_clip_url"] = None
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
            if "engine" in params and params["engine"] in SUPPORTED_ENGINES:
                scene["engine"] = params["engine"]

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
            # Expects dict like {"TB": "path/to/bear.jpg", "HERO": "path/to/hero.jpg"}
            settings["character_references"] = params["character_references"]

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
                "duration": s.get("duration", 8),
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
                "duration": tpl_scene.get("duration", 8),
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
                from lib.video_stitcher import reverse_clip
                rev_path = s["clip_path"].replace(".mp4", "_rev.mp4")
                reverse_clip(s["clip_path"], rev_path)
                os.replace(rev_path, s["clip_path"])  # overwrite original
                s["reversed"] = not s.get("reversed", False)
                _save_manual_plan(plan)
                self._send_json({"ok": True, "reversed": s["reversed"]})
                return
        self._send_json({"ok": False, "error": "Scene or clip not found"})

    def _handle_boomerang_clip(self, scene_id):
        plan = _load_manual_plan()
        for s in plan.get("scenes", []):
            if s.get("id") == scene_id and s.get("clip_path") and os.path.isfile(s["clip_path"]):
                from lib.video_stitcher import boomerang_clip
                boom_path = s["clip_path"].replace(".mp4", "_boom.mp4")
                boomerang_clip(s["clip_path"], boom_path)
                os.replace(boom_path, s["clip_path"])
                s["boomerang"] = True
                _save_manual_plan(plan)
                self._send_json({"ok": True})
                return
        self._send_json({"ok": False, "error": "Scene or clip not found"})

    def _handle_export_gif(self, scene_id):
        plan = _load_manual_plan()
        for s in plan.get("scenes", []):
            if s.get("id") == scene_id and s.get("clip_path") and os.path.isfile(s["clip_path"]):
                from lib.video_stitcher import export_gif
                gif_dir = os.path.join(OUTPUT_DIR, "gifs")
                os.makedirs(gif_dir, exist_ok=True)
                gif_path = os.path.join(gif_dir, f"scene_{scene_id}.gif")
                export_gif(s["clip_path"], gif_path)
                self._send_json({"ok": True, "gif_url": f"/output/gifs/scene_{scene_id}.gif"})
                return
        self._send_json({"ok": False, "error": "Scene or clip not found"})

    def _handle_get_palette(self, scene_id):
        plan = _load_manual_plan()
        for s in plan.get("scenes", []):
            if s.get("id") == scene_id:
                photo = s.get("photo_path") or (s.get("photo_paths", [None])[0])
                if photo and os.path.isfile(photo):
                    from lib.prompt_assistant import extract_palette, suggest_grade_from_palette
                    colors = extract_palette(photo)
                    grade = suggest_grade_from_palette(colors)
                    self._send_json({"ok": True, "palette": colors, "suggested_grade": grade})
                    return
                self._send_json({"ok": False, "error": "No photo found"})
                return
        self._send_json({"ok": False, "error": "Scene not found"})

    def _handle_prompt_history(self):
        history_path = os.path.join(OUTPUT_DIR, "prompt_history.json")
        if os.path.isfile(history_path):
            data = json.loads(open(history_path, encoding="utf-8").read())
        else:
            data = {"prompts": [], "favorites": []}
        self._send_json(data)

    def _handle_star_prompt(self):
        body = self._read_body()
        params = json.loads(body) if body else {}
        prompt = params.get("prompt", "")
        history_path = os.path.join(OUTPUT_DIR, "prompt_history.json")
        if os.path.isfile(history_path):
            data = json.loads(open(history_path, encoding="utf-8").read())
        else:
            data = {"prompts": [], "favorites": []}
        if prompt and prompt not in data["favorites"]:
            data["favorites"].append(prompt)
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self._send_json({"ok": True})

    def _handle_autosave(self):
        body = self._read_body()
        autosave_path = os.path.join(OUTPUT_DIR, "autosave.json")
        with open(autosave_path, "w", encoding="utf-8") as f:
            f.write(body.decode("utf-8") if isinstance(body, bytes) else body)
        self._send_json({"ok": True})


    def _handle_estimate_render_time(self):
        """Estimate total render time based on scenes and engines."""
        plan = _load_manual_plan()
        scenes = plan.get("scenes", [])
        if not scenes:
            self._send_json({"estimate_seconds": 0, "estimate_human": "0 seconds"})
            return
        
        # Average generation time per engine
        ENGINE_TIMES = {
            "grok": 35,      # ~35 seconds per clip
            "runway": 60,    # ~60 seconds per clip  
            "luma": 45,      # ~45 seconds per clip
            "openai": 15,    # ~15 seconds (image only + Ken Burns)
        }
        STITCH_PER_CLIP = 5  # ~5 seconds per clip for stitching
        
        settings = _load_settings()
        default_engine = settings.get("default_engine", "grok")
        
        total = 0
        for s in scenes:
            if s.get("has_clip") or s.get("clip_path"):
                continue  # already generated
            engine = s.get("engine") or default_engine
            total += ENGINE_TIMES.get(engine, 35)
        
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
            gen_state["phase"] = ""
            gen_state["progress"] = 0
            gen_state["total_scenes"] = 0
            gen_state["error"] = None
            gen_state["output_file"] = None
            gen_state["analysis"] = None
            gen_state["scenes"] = []

        print(f"[RESET] Full project reset. Cleared: {', '.join(cleared)}")
        self._send_json({"ok": True, "cleared": cleared})


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
