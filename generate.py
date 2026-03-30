#!/usr/bin/env python3
"""
Music Video Generator - CLI Tool

Usage:
    python generate.py --song track.mp3 --style "cyberpunk neon city rain" --output my_video.mp4
    python generate.py --regen 5 --prompt "new prompt for scene 5"
"""

import argparse
import json
import os
import sys
import time

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from lib.audio_analyzer import analyze
from lib.scene_planner import plan_scenes, TRANSITION_TYPES
from lib.video_generator import generate_scene, generate_all
from lib.video_stitcher import stitch
from lib.prompt_assistant import (
    STYLE_PRESETS, get_preset, enhance_prompt, suggest_from_song_name,
)


SCENE_PLAN_PATH = os.path.join("output", "scene_plan.json")


def progress_bar(current, total, width=40):
    pct = current / max(total, 1)
    filled = int(width * pct)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return f"[{bar}] {current}/{total}"


def save_scene_plan(scenes, clip_paths, song_path, output_path):
    """Save scene plan to JSON for later re-generation."""
    plan = {
        "song_path": song_path,
        "output_path": output_path,
        "scenes": [],
    }
    for i, scene in enumerate(scenes):
        entry = dict(scene)
        entry["index"] = i
        entry["clip_path"] = clip_paths[i] if i < len(clip_paths) else None
        # matched_references might not be serializable as-is, ensure it's a list
        if "matched_references" not in entry:
            entry["matched_references"] = []
        plan["scenes"].append(entry)
    os.makedirs(os.path.dirname(os.path.abspath(SCENE_PLAN_PATH)), exist_ok=True)
    with open(SCENE_PLAN_PATH, "w") as f:
        json.dump(plan, f, indent=2)
    return plan


def load_scene_plan():
    """Load existing scene plan from JSON."""
    if not os.path.isfile(SCENE_PLAN_PATH):
        raise FileNotFoundError(f"No scene plan found at {SCENE_PLAN_PATH}. Generate a video first.")
    with open(SCENE_PLAN_PATH, "r") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Generate an AI music video from an audio track."
    )
    parser.add_argument("--song", default=None, help="Path to audio file (mp3/wav)")
    parser.add_argument("--style", default=None,
                        help="Visual style description (e.g. 'cyberpunk city neon rain')")
    parser.add_argument("--output", default=None,
                        help="Output video path (default: output/final_video.mp4)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducible scene planning")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze and plan only, do not generate videos")
    parser.add_argument("--regen", type=int, default=None, metavar="INDEX",
                        help="Regenerate a single scene by index (0-based)")
    parser.add_argument("--prompt", default=None,
                        help="New prompt for the scene being regenerated (used with --regen)")
    parser.add_argument("--transition", default=None,
                        choices=TRANSITION_TYPES,
                        help="Default transition type for all scenes (e.g. crossfade, hard_cut, glitch)")
    parser.add_argument("--preset", default=None,
                        choices=list(STYLE_PRESETS.keys()),
                        help="Use a style preset (e.g. cyberpunk, synthwave, space)")
    parser.add_argument("--manual", action="store_true",
                        help="Use manual scene plan (output/manual_scene_plan.json) instead of auto-analysis")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    # --- Manual mode ---
    if args.manual:
        return handle_manual(args)

    # --- Regen mode ---
    if args.regen is not None:
        return handle_regen(args.regen, args.prompt)

    # --- Full generation mode ---
    if not args.song:
        parser.error("--song is required for full generation")

    # Resolve style: preset, --style, or both
    style = None
    if args.preset:
        preset_text = get_preset(args.preset)
        if args.style:
            # Combine preset with custom style
            style = f"{preset_text}, {args.style}"
        else:
            style = preset_text
    elif args.style:
        style = args.style
    else:
        # Try to suggest from song filename
        suggested = suggest_from_song_name(os.path.basename(args.song))
        print(f"  No --style or --preset given. Auto-suggesting: {suggested[:80]}")
        style = suggested

    if not style:
        parser.error("--style or --preset is required for full generation")

    song_path = os.path.abspath(args.song)
    output_path = args.output or os.path.join("output", "final_video.mp4")
    output_path = os.path.abspath(output_path)
    clips_dir = os.path.join(os.path.dirname(output_path), "clips")

    # --- Step 1: Audio analysis ---
    print("\n=== AUDIO ANALYSIS ===")
    print(f"Analyzing: {song_path}")
    analysis = analyze(song_path)

    print(f"  Duration: {analysis['duration']:.1f}s")
    print(f"  BPM:      {analysis['bpm']}")
    print(f"  Beats:    {len(analysis['beats'])}")
    print(f"  Sections: {len(analysis['sections'])}")

    # --- Step 2: Scene planning ---
    print("\n=== SCENE PLAN ===")
    scenes = plan_scenes(analysis, style, seed=args.seed)

    # Apply default transition override if specified
    if args.transition:
        for scene in scenes:
            scene["transition"] = args.transition

    print(f"  Total scenes: {len(scenes)}")
    print()
    for i, scene in enumerate(scenes):
        trans = scene.get("transition", "crossfade")
        print(f"  Scene {i + 1:2d} | {scene['start_sec']:6.1f}s - {scene['end_sec']:6.1f}s "
              f"| {scene['section_type']:6s} | {trans:10s} | {scene['prompt'][:60]}...")
    print()

    # --- Cost estimate ---
    n = len(scenes)
    print(f"=== COST ESTIMATE ===")
    print(f"  {n} video generation API calls")
    print(f"  (failed calls will retry as image generation + Ken Burns)")
    print()

    if args.dry_run:
        print("Dry run complete. No videos generated.")
        return

    # Confirm
    if not args.yes:
        try:
            answer = input(f"Generate {n} scenes? [Y/n] ").strip().lower()
            if answer and answer != "y":
                print("Aborted.")
                return
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

    # --- Step 3: Generate video clips ---
    print("\n=== GENERATING VIDEO CLIPS ===")
    scene_status = {}

    def on_progress(index, status):
        scene_status[index] = status
        done = sum(1 for s in scene_status.values() if "done" in s.lower() or "FAILED" in s)
        sys.stdout.write(f"\r  {progress_bar(done, n)} Scene {index + 1}: {status[:50]}   ")
        sys.stdout.flush()

    start_time = time.time()
    clip_paths = generate_all(scenes, clips_dir, progress_cb=on_progress)
    elapsed = time.time() - start_time

    print(f"\n\n  Generated {sum(1 for c in clip_paths if c)} / {n} clips in {elapsed:.0f}s")

    valid = [c for c in clip_paths if c]
    if not valid:
        print("\nERROR: No clips were generated successfully. Cannot stitch.")
        sys.exit(1)

    # Save scene plan
    save_scene_plan(scenes, clip_paths, song_path, output_path)
    print(f"  Scene plan saved to {SCENE_PLAN_PATH}")

    # --- Step 4: Stitch final video ---
    print("\n=== STITCHING FINAL VIDEO ===")

    def on_stitch(status):
        print(f"  {status}")

    # Extract per-scene transitions
    scene_transitions = [s.get("transition", "crossfade") for s in scenes]
    default_trans = args.transition or "crossfade"

    stitch(clip_paths, song_path, output_path,
           transitions=scene_transitions, default_transition=default_trans,
           progress_cb=on_stitch)

    file_size = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\n=== COMPLETE ===")
    print(f"  Output:   {output_path}")
    print(f"  Size:     {file_size:.1f} MB")
    print()


def handle_regen(scene_index: int, new_prompt: str | None):
    """Regenerate a single scene and re-stitch the video."""
    print(f"\n=== REGENERATING SCENE {scene_index} ===")

    plan = load_scene_plan()
    scenes = plan["scenes"]
    song_path = plan["song_path"]
    output_path = plan["output_path"]

    if scene_index < 0 or scene_index >= len(scenes):
        print(f"ERROR: Scene index {scene_index} out of range (0-{len(scenes) - 1})")
        sys.exit(1)

    scene = scenes[scene_index]
    old_prompt = scene["prompt"]

    if new_prompt:
        scene["prompt"] = new_prompt
        print(f"  Old prompt: {old_prompt[:80]}...")
        print(f"  New prompt: {new_prompt[:80]}...")
    else:
        print(f"  Re-generating with existing prompt: {old_prompt[:80]}...")

    clips_dir = os.path.join(os.path.dirname(output_path), "clips")

    def on_progress(index, status):
        print(f"  [{status}]")

    clip_path = generate_scene(scene, scene_index, clips_dir, progress_cb=on_progress)
    scene["clip_path"] = clip_path

    # Update scene plan
    plan["scenes"][scene_index] = scene
    with open(SCENE_PLAN_PATH, "w") as f:
        json.dump(plan, f, indent=2)
    print(f"  Scene plan updated.")

    # Re-stitch
    print("\n=== RE-STITCHING FINAL VIDEO ===")
    clip_paths = [s.get("clip_path") for s in plan["scenes"]]
    scene_transitions = [s.get("transition", "crossfade") for s in plan["scenes"]]

    def on_stitch(status):
        print(f"  {status}")

    stitch(clip_paths, song_path, output_path,
           transitions=scene_transitions, progress_cb=on_stitch)

    file_size = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\n=== COMPLETE ===")
    print(f"  Output:   {output_path}")
    print(f"  Size:     {file_size:.1f} MB")
    print()


def handle_manual(args):
    """Generate video from manual scene plan."""
    manual_plan_path = os.path.join("output", "manual_scene_plan.json")
    if not os.path.isfile(manual_plan_path):
        print(f"ERROR: No manual scene plan found at {manual_plan_path}")
        print("Use the web UI's Manual Mode to create scenes first.")
        sys.exit(1)

    with open(manual_plan_path, "r") as f:
        plan = json.load(f)

    scenes = plan.get("scenes", [])
    if not scenes:
        print("ERROR: Manual scene plan has no scenes.")
        sys.exit(1)

    song_path = plan.get("song_path")
    output_path = args.output or os.path.join("output", "manual_final_video.mp4")
    output_path = os.path.abspath(output_path)
    clips_dir = os.path.join(os.path.dirname(output_path), "manual_clips")

    print(f"\n=== MANUAL MODE ===")
    print(f"  Scenes: {len(scenes)}")
    if song_path:
        print(f"  Audio:  {song_path}")
    else:
        print(f"  Audio:  (none)")

    for i, scene in enumerate(scenes):
        print(f"  Scene {i + 1:2d} | {scene.get('duration', 8)}s | {scene.get('transition', 'crossfade'):10s} "
              f"| {scene.get('prompt', '')[:60]}...")

    # Generate clips for scenes without them
    scenes_to_gen = [(i, s) for i, s in enumerate(scenes)
                     if not s.get("has_clip") or not s.get("clip_path")
                     or not os.path.isfile(s.get("clip_path", ""))]

    if scenes_to_gen:
        print(f"\n=== GENERATING {len(scenes_to_gen)} CLIPS ===")
        n = len(scenes_to_gen)
        scene_status = {}

        def on_progress(index, status):
            scene_status[index] = status
            done = sum(1 for s in scene_status.values() if "done" in s.lower() or "FAILED" in s)
            sys.stdout.write(f"\r  {progress_bar(done, n)} Scene {index + 1}: {status[:50]}   ")
            sys.stdout.flush()

        start_time = time.time()
        for scene_idx, scene in scenes_to_gen:
            gen_prompt = scene["prompt"]
            if scene.get("photo_path") and os.path.isfile(scene["photo_path"]):
                gen_prompt += ", matching the reference image style"

            gen_scene = {
                "prompt": gen_prompt,
                "duration": scene.get("duration", 8),
            }
            try:
                clip_path = generate_scene(gen_scene, scene_idx, clips_dir,
                                           progress_cb=on_progress)
                scene["clip_path"] = clip_path
                scene["has_clip"] = True
            except Exception as e:
                print(f"\n  Scene {scene_idx} failed: {e}")

        elapsed = time.time() - start_time
        generated = sum(1 for _, s in scenes_to_gen if s.get("has_clip"))
        print(f"\n\n  Generated {generated} / {n} clips in {elapsed:.0f}s")

        # Save updated plan
        with open(manual_plan_path, "w") as f:
            json.dump(plan, f, indent=2)
    else:
        print("\n  All clips already generated.")

    # Stitch
    print("\n=== STITCHING FINAL VIDEO ===")
    clip_paths = [s.get("clip_path") for s in scenes]
    scene_transitions = [s.get("transition", "crossfade") for s in scenes]

    audio = song_path if song_path and os.path.isfile(song_path) else None

    def on_stitch(status):
        print(f"  {status}")

    stitch(clip_paths, audio, output_path,
           transitions=scene_transitions, progress_cb=on_stitch)

    file_size = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\n=== COMPLETE ===")
    print(f"  Output:   {output_path}")
    print(f"  Size:     {file_size:.1f} MB")
    print()


if __name__ == "__main__":
    main()
