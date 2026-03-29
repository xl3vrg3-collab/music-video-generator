#!/usr/bin/env python3
"""
Music Video Generator - CLI Tool

Usage:
    python generate.py --song track.mp3 --style "cyberpunk neon city rain" --output my_video.mp4
"""

import argparse
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
from lib.scene_planner import plan_scenes
from lib.video_generator import generate_all
from lib.video_stitcher import stitch


def progress_bar(current, total, width=40):
    pct = current / max(total, 1)
    filled = int(width * pct)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return f"[{bar}] {current}/{total}"


def main():
    parser = argparse.ArgumentParser(
        description="Generate an AI music video from an audio track."
    )
    parser.add_argument("--song", required=True, help="Path to audio file (mp3/wav)")
    parser.add_argument("--style", required=True,
                        help="Visual style description (e.g. 'cyberpunk city neon rain')")
    parser.add_argument("--output", default=None,
                        help="Output video path (default: output/final_video.mp4)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducible scene planning")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze and plan only, do not generate videos")
    args = parser.parse_args()

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
    scenes = plan_scenes(analysis, args.style, seed=args.seed)

    print(f"  Total scenes: {len(scenes)}")
    print()
    for i, scene in enumerate(scenes):
        print(f"  Scene {i + 1:2d} | {scene['start_sec']:6.1f}s - {scene['end_sec']:6.1f}s "
              f"| {scene['section_type']:6s} | {scene['prompt'][:70]}...")
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

    # --- Step 4: Stitch final video ---
    print("\n=== STITCHING FINAL VIDEO ===")

    def on_stitch(status):
        print(f"  {status}")

    stitch(clip_paths, song_path, output_path, progress_cb=on_stitch)

    file_size = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\n=== COMPLETE ===")
    print(f"  Output:   {output_path}")
    print(f"  Size:     {file_size:.1f} MB")
    print()


if __name__ == "__main__":
    main()
