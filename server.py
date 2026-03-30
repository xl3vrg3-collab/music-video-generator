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
import uuid as _uuid
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from lib.audio_analyzer import analyze
from lib.scene_planner import plan_scenes, TRANSITION_TYPES
from lib.video_generator import (
    generate_scene, generate_all, generate_from_photo,
    describe_photo, CAMERA_PRESETS, CAMERA_PROMPT_SUFFIXES,
)
from lib.video_stitcher import (
    stitch, apply_lyrics_overlay, apply_aspect_ratio, split_clip,
    ASPECT_PRESETS, _get_clip_duration,
    SPEED_OPTIONS, COLOR_GRADE_PRESETS, AUDIO_VIZ_STYLES,
    generate_credits, apply_watermark, extract_thumbnail,
    mix_audio_tracks, export_for_platform, apply_beat_sync_cuts,
)
from lib.prompt_assistant import (
    STYLE_PRESETS, get_preset, enhance_prompt, suggest_from_song_name,
    get_preset_names, suggest_style, suggest_genre_from_bpm,
)
from lib.storyboard_generator import generate_storyboard

PORT = 3849
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(PROJECT_DIR, "uploads")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
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

        # Build the prompt - if there's a photo, add reference context
        gen_prompt = scene["prompt"]
        if scene.get("photo_path") and os.path.isfile(scene["photo_path"]):
            gen_prompt += ", matching the reference image style"

        gen_scene = {
            "prompt": gen_prompt,
            "duration": scene.get("duration", 8),
        }

        clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                   progress_cb=on_progress, cost_cb=_record_cost)
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

        photo_path = scene.get("photo_path", "")
        if not photo_path or not os.path.isfile(photo_path):
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

        generate_from_photo(photo_path, prompt, duration, clip_path,
                            camera=camera,
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

            gen_prompt = scene["prompt"]
            if scene.get("photo_path") and os.path.isfile(scene["photo_path"]):
                gen_prompt += ", matching the reference image style"

            gen_scene = {
                "prompt": gen_prompt,
                "duration": scene.get("duration", 8),
            }

            try:
                clip_path = generate_scene(gen_scene, scene_idx, MANUAL_CLIPS_DIR,
                                           progress_cb=on_progress, cost_cb=_record_cost)
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

        stitch(clip_paths, audio, output_path,
               transitions=transitions,
               speeds=speeds,
               text_overlays=text_overlays,
               color_grade=global_color_grade,
               scene_color_grades=scene_color_grades,
               audio_viz=audio_viz)

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
            # Try auto clips dir first, then manual clips dir
            clip_file = os.path.join(CLIPS_DIR, safe)
            if not os.path.isfile(clip_file):
                clip_file = os.path.join(MANUAL_CLIPS_DIR, safe)
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

        else:
            self.send_error(404)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if re.match(r'^/api/manual/scene/([^/]+)$', path):
            m = re.match(r'^/api/manual/scene/([^/]+)$', path)
            self._handle_manual_update_scene(m.group(1))
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
            scenes_out.append(entry)
        self._send_json({
            "scenes": scenes_out,
            "song_path": plan.get("song_path"),
            "color_grade": plan.get("color_grade", "none"),
            "audio_viz": plan.get("audio_viz"),
            "style_lock": plan.get("style_lock", ""),
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
            "photo_path": None,
            "photo_paths": [],  # multi-photo mood board (up to 4)
            "clip_path": None,
            "has_clip": False,
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

        plan["scenes"].append(scene)
        _save_manual_plan(plan)
        self._send_json({"ok": True, "scene": scene})

    def _handle_manual_update_scene(self, scene_id: str):
        """Update a manual scene's prompt, duration, transition, speed, overlay, or color_grade."""
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
                with open(photo_path, "wb") as f:
                    f.write(file_data)
                s["photo_path"] = photo_path
                _save_manual_plan(plan)
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
        _save_manual_plan(plan)
        self._send_json({"ok": True})

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
        """POST /api/manual/scene/:id/split - Split a manual scene into two halves."""
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
        half = max(2, round(orig_dur / 2))
        scene_a = {"id": new_id_a, "prompt": scene.get("prompt", ""), "duration": half,
                    "transition": scene.get("transition", "crossfade"),
                    "photo_path": scene.get("photo_path"), "clip_path": None, "has_clip": False}
        scene_b = {"id": new_id_b, "prompt": scene.get("prompt", ""), "duration": orig_dur - half,
                    "transition": "crossfade", "photo_path": None, "clip_path": None, "has_clip": False}
        if scene.get("clip_path") and os.path.isfile(scene["clip_path"]):
            try:
                p1, p2 = split_clip(scene["clip_path"], MANUAL_CLIPS_DIR, scene_id)
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
