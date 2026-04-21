"""
LUMN Cinematic Pipeline — 9-stage production workflow.

Stages:
  1. Story beats       — validate beat breakdown
  2. Identity sheets   — generate character sheets (Gemini text-to-image)
  3. Location sheets   — generate environment sheets (Gemini text-to-image)
  4. Scene stills      — generate mood image per beat (Gemini edit w/ refs)
  5. Shot list         — validate shot list
  6. Shot anchors      — generate exact first frame per shot (Gemini edit w/ refs)
  7. Shot preview      — optional cheap 5s test (Kling V3 Standard)
  8. Final render      — production clips (Kling V3 Pro or O3 Pro)
  9. Conform           — edit, transition, audio, export

Usage:
  python scripts/run_cinematic.py step1_beats
  python scripts/run_cinematic.py step2_identity
  python scripts/run_cinematic.py step3_location
  python scripts/run_cinematic.py step4_stills
  python scripts/run_cinematic.py step4_still BEAT_ID
  python scripts/run_cinematic.py step5_shotlist
  python scripts/run_cinematic.py step6_anchors
  python scripts/run_cinematic.py step6_anchor SHOT_ID
  python scripts/run_cinematic.py step7_preview
  python scripts/run_cinematic.py step8_render [tier]
  python scripts/run_cinematic.py step8_shot SHOT_ID [tier]
  python scripts/run_cinematic.py step9_conform
  python scripts/run_cinematic.py status
  python scripts/run_cinematic.py cost [tier]
"""

import json
import os
import shutil
import subprocess
import sys
import time

# Setup paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
os.chdir(PROJECT_ROOT)
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import (
    gemini_edit_image, gemini_generate_image,
    kling_image_to_video, FAL_API_KEY,
)
from lib.cinematic_compiler import (
    load_json, compile_sheet_payloads, compile_scene_still_payloads,
    compile_shot_anchor_payloads, compile_video_payloads,
    compile_conform_payload, estimate_cost,
)
from lib.preproduction_assets import build_sheet_prompt

PLAN_PATH = "output/pipeline/production_plan_v3.json"
PROFILE_PATH = "output/pipeline/model_profile.json"
PACKAGES_PATH = "output/preproduction/packages.json"
STILLS_DIR = "output/pipeline/scene_stills"
ANCHORS_DIR = "output/pipeline/anchors_v3"
CLIPS_DIR = "output/pipeline/clips_v3"
FINAL_DIR = "output/pipeline/final"

for d in [STILLS_DIR, ANCHORS_DIR, CLIPS_DIR, FINAL_DIR]:
    os.makedirs(d, exist_ok=True)


def load_all():
    plan = load_json(PLAN_PATH)
    profile = load_json(PROFILE_PATH)
    packages = load_json(PACKAGES_PATH)
    return plan, profile, packages


def save_plan(plan):
    with open(PLAN_PATH, "w") as f:
        json.dump(plan, f, indent=2)


# ---------------------------------------------------------------------------
# Step 1: Validate Story Beats
# ---------------------------------------------------------------------------

def step1_beats():
    """Validate and display beat breakdown."""
    plan, _, _ = load_all()
    beats = plan.get("beats", [])

    print(f"\n{'='*60}")
    print(f"STEP 1: Story Beat Breakdown")
    print(f"Project: {plan.get('project', 'Untitled')}")
    print(f"Style Bible: {plan.get('style_bible', 'None')}")
    print(f"{'='*60}\n")

    total_dur = 0
    total_shots = 0
    for beat in beats:
        shots = beat.get("shots", [])
        beat_dur = sum(s.get("duration", 5) for s in shots)
        total_dur += beat_dur
        total_shots += len(shots)

        arc = beat.get("narrative_arc", "")
        energy = beat.get("energy", 0)
        bar = "#" * int(energy * 20)

        print(f"  {beat['beat_id']}: {beat.get('title', '')}")
        print(f"    Arc: {arc} | Energy: [{bar:<20}] {energy}")
        print(f"    Emotion: {beat.get('emotion', '')}")
        print(f"    Characters: {len(beat.get('characters', []))}")
        print(f"    Shots: {len(shots)} | Duration: {beat_dur}s")
        trans_out = beat.get("transition_out", {})
        print(f"    Transition out: {trans_out.get('motivation', trans_out.get('type', ''))}")
        if beat.get("multi_shot_group"):
            print(f"    ** Multi-shot group (continuous momentum)")
        print()

    audio_dur = plan.get("audio", {}).get("duration", 0)
    print(f"  Total: {len(beats)} beats, {total_shots} shots, {total_dur}s video")
    print(f"  Audio: {audio_dur:.1f}s")
    diff = total_dur - audio_dur
    if abs(diff) > 2:
        print(f"  WARNING: Video/audio mismatch: {diff:+.1f}s")

    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 2: Identity Sheets (Characters)
# ---------------------------------------------------------------------------

def step2_identity():
    """Generate character reference sheets."""
    plan, _, packages = load_all()
    payloads = compile_sheet_payloads(plan, packages, "character")
    _generate_sheets(payloads, "Identity Sheets (Characters)")


# ---------------------------------------------------------------------------
# Step 3: Location Sheets (Environments)
# ---------------------------------------------------------------------------

def step3_location():
    """Generate environment reference sheets."""
    plan, _, packages = load_all()
    payloads = compile_sheet_payloads(plan, packages, "environment")
    _generate_sheets(payloads, "Location Sheets (Environments)")


def _generate_sheets(payloads, title):
    """Shared sheet generation logic for steps 2 and 3."""
    packages = load_json(PACKAGES_PATH)
    total = len(payloads)
    failed = []

    print(f"\n{'='*60}")
    print(f"STEP: {title}")
    print(f"Engine: Gemini 3.1 Flash (text-to-image)")
    print(f"Cost: ~${total * 0.08:.2f}")
    print(f"{'='*60}\n")

    for i, payload in enumerate(payloads):
        pkg_id = payload["pkg_id"]
        name = payload["name"]
        pkg_data = payload["pkg_data"]
        output = payload["output_path"]

        os.makedirs(os.path.dirname(output), exist_ok=True)
        prompt = build_sheet_prompt(pkg_data)

        print(f"[{i+1}/{total}] {name}")
        print(f"  Package: {pkg_id}")
        print(f"  Prompt: {prompt[:80]}...")

        try:
            paths = gemini_generate_image(
                prompt=prompt,
                resolution="1K",
                aspect_ratio="16:9",
            )
            if paths and os.path.isfile(paths[0]):
                shutil.copy2(paths[0], output)
                size_kb = os.path.getsize(output) / 1024
                # Update package
                for pkg in packages.get("packages", []):
                    if pkg["package_id"] == pkg_id:
                        pkg["hero_image_path"] = os.path.abspath(output)
                        pkg["status"] = "generated"
                        break
                print(f"  OK: {output} ({size_kb:.0f}KB)")
            else:
                print(f"  FAILED: No image returned")
                failed.append(name)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append(name)

        time.sleep(1)

    # Save updated packages
    with open(PACKAGES_PATH, "w") as f:
        json.dump(packages, f, indent=2)

    print(f"\n{'='*60}")
    print(f"DONE: {total - len(failed)}/{total} sheets")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 4: Scene Stills (mood image per beat)
# ---------------------------------------------------------------------------

def step4_stills(single_beat=None):
    """Generate scene stills — one mood image per beat."""
    plan, profile, packages = load_all()
    payloads = compile_scene_still_payloads(plan, profile, packages)

    if single_beat:
        payloads = [p for p in payloads if p["beat_id"] == single_beat]

    total = len(payloads)
    failed = []

    print(f"\n{'='*60}")
    print(f"STEP 4: Scene Stills ({total} beats)")
    print(f"Engine: Gemini 3.1 Flash (edit with refs)")
    print(f"Cost: ~${total * 0.08:.2f}")
    print(f"{'='*60}\n")

    for i, payload in enumerate(payloads):
        beat_id = payload["beat_id"]
        refs = payload["reference_image_paths"]
        prompt = payload["prompt"]
        output = payload["output_path"]

        os.makedirs(os.path.dirname(output), exist_ok=True)

        print(f"[{i+1}/{total}] {beat_id} — {payload.get('title', '')}")
        print(f"  Refs: {len(refs)} images")
        print(f"  Prompt: {prompt[:80]}...")

        try:
            paths = gemini_edit_image(
                prompt=prompt,
                reference_image_paths=refs,
                resolution="1K",
            )
            if paths and os.path.isfile(paths[0]):
                shutil.copy2(paths[0], output)
                # Update plan
                for beat in plan["beats"]:
                    if beat["beat_id"] == beat_id:
                        beat["scene_still_path"] = os.path.abspath(output)
                        beat["scene_still_status"] = "generated"
                        break
                print(f"  OK: {output}")
            else:
                print(f"  FAILED: No image returned")
                failed.append(beat_id)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append(beat_id)

        time.sleep(1)

    save_plan(plan)

    print(f"\n{'='*60}")
    print(f"STEP 4 DONE: {total - len(failed)}/{total} scene stills")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 5: Validate Shot List
# ---------------------------------------------------------------------------

def step5_shotlist():
    """Validate and display shot list with camera details."""
    plan, _, _ = load_all()

    print(f"\n{'='*60}")
    print(f"STEP 5: Shot List Review")
    print(f"{'='*60}\n")

    total_shots = 0
    total_dur = 0
    for beat in plan.get("beats", []):
        print(f"  --- {beat['beat_id']}: {beat.get('title', '')} ---")
        for shot in beat.get("shots", []):
            total_shots += 1
            dur = shot.get("duration", 5)
            total_dur += dur
            print(f"  {shot['shot_id']}:")
            print(f"    Framing: {shot.get('framing', '')}")
            print(f"    Camera:  {shot.get('camera_height', '')} | {shot.get('lens', '')}")
            print(f"    Action:  {shot.get('action', '')}")
            print(f"    Duration: {dur}s")
            print(f"    Video prompt: {shot.get('video_prompt', '')[:60]}...")
            print()

    print(f"  Total: {total_shots} shots, {total_dur}s")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 6: Shot Anchors
# ---------------------------------------------------------------------------

def step6_anchors(single_shot=None):
    """Generate shot anchor images — exact first frame per shot."""
    plan, profile, packages = load_all()
    payloads = compile_shot_anchor_payloads(plan, profile, packages)

    if single_shot:
        payloads = [p for p in payloads if p["shot_id"] == single_shot]

    total = len(payloads)
    failed = []

    print(f"\n{'='*60}")
    print(f"STEP 6: Shot Anchors ({total} shots)")
    print(f"Engine: Gemini 3.1 Flash (edit with refs + scene still)")
    print(f"Cost: ~${total * 0.08:.2f}")
    print(f"{'='*60}\n")

    for i, payload in enumerate(payloads):
        shot_id = payload["shot_id"]
        beat_id = payload["beat_id"]
        refs = payload["reference_image_paths"]
        prompt = payload["prompt"]
        output = payload["output_path"]

        os.makedirs(os.path.dirname(output), exist_ok=True)

        print(f"[{i+1}/{total}] {shot_id} (beat: {beat_id})")
        print(f"  Refs: {len(refs)} images (sheets + scene still)")
        print(f"  Prompt: {prompt[:80]}...")

        try:
            paths = gemini_edit_image(
                prompt=prompt,
                reference_image_paths=refs,
                resolution="1K",
            )
            if paths and os.path.isfile(paths[0]):
                shutil.copy2(paths[0], output)
                # Update plan
                for beat in plan["beats"]:
                    for shot in beat.get("shots", []):
                        if shot["shot_id"] == shot_id:
                            shot["anchor_path"] = os.path.abspath(output)
                            shot["anchor_status"] = "generated"
                            break
                print(f"  OK: {output}")
            else:
                print(f"  FAILED: No image returned")
                failed.append(shot_id)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append(shot_id)

        time.sleep(1)

    save_plan(plan)

    print(f"\n{'='*60}")
    print(f"STEP 6 DONE: {total - len(failed)}/{total} shot anchors")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 7: Shot Preview (optional, cheap)
# ---------------------------------------------------------------------------

def step7_preview():
    """Generate cheap preview clips (V3 Standard, 5s)."""
    _generate_video("draft", "STEP 7: Shot Preview (cheap test)")


# ---------------------------------------------------------------------------
# Step 8: Final Render
# ---------------------------------------------------------------------------

def step8_render(tier="review", single_shot=None):
    """Generate production-quality clips."""
    _generate_video(tier, f"STEP 8: Final Render ({tier})", single_shot)


def _generate_video(tier, title, single_shot=None):
    """Shared video generation logic for steps 7 and 8."""
    plan, profile, packages = load_all()
    payloads = compile_video_payloads(plan, profile, packages, tier)

    if single_shot:
        payloads = [p for p in payloads
                    if p.get("shot_id") == single_shot
                    or single_shot in p.get("shot_ids", [])]

    total = len(payloads)
    failed = []
    total_duration = 0

    cost = estimate_cost(plan, profile, tier)

    print(f"\n{'='*60}")
    print(f"{title}")
    print(f"Tier: {tier} ({cost['engine']})")
    print(f"Estimated cost: ${cost['video']['cost']}")
    print(f"{'='*60}\n")

    # Resolve tier name for filenames
    tier_map = {"draft": "v3standard", "review": "v3pro", "final": "o3pro"}
    engine_tag = tier_map.get(tier, "v3standard")

    for i, payload in enumerate(payloads):
        is_multi = payload.get("is_multi_shot", False)
        shot_ids = payload.get("shot_ids", [payload.get("shot_id", "?")])
        duration = payload["duration"]
        start_img = payload["start_image_path"]
        fal_tier = payload["tier"]

        label = "+".join(shot_ids)
        print(f"[{i+1}/{total}] {label}")
        print(f"  Duration: {duration}s | Tier: {fal_tier}")
        print(f"  Start: {os.path.basename(start_img)}")
        if is_multi:
            print(f"  Mode: multi-shot ({len(payload['multi_prompt'])} prompts)")
        else:
            print(f"  Prompt: {payload.get('prompt', '')[:60]}...")

        if not os.path.isfile(start_img):
            print(f"  ERROR: Anchor not found: {start_img}")
            failed.extend(shot_ids)
            continue

        try:
            if is_multi:
                result_path = kling_image_to_video(
                    start_image_path=start_img,
                    prompt="",
                    duration=duration,
                    tier=fal_tier,
                    end_image_path=None,
                    elements=None,
                    multi_prompt=payload["multi_prompt"],
                    negative_prompt=payload.get("negative_prompt", ""),
                )
            else:
                result_path = kling_image_to_video(
                    start_image_path=start_img,
                    prompt=payload["prompt"],
                    duration=duration,
                    tier=fal_tier,
                    end_image_path=None,
                    elements=None,
                    negative_prompt=payload.get("negative_prompt", ""),
                )

            if result_path and os.path.isfile(result_path):
                if is_multi:
                    clip_name = f"{'_'.join(shot_ids)}_{engine_tag}_multi.mp4"
                else:
                    clip_name = f"{shot_ids[0]}_{engine_tag}.mp4"

                clip_path = os.path.join(CLIPS_DIR, clip_name)
                shutil.copy2(result_path, clip_path)
                size_mb = os.path.getsize(clip_path) / (1024 * 1024)
                total_duration += duration

                # Update plan
                abs_clip = os.path.abspath(clip_path)
                for beat in plan["beats"]:
                    if is_multi:
                        if beat.get("multi_shot_group"):
                            beat_shot_ids = [s["shot_id"] for s in beat.get("shots", [])]
                            if beat_shot_ids == shot_ids:
                                for shot in beat["shots"]:
                                    shot["clip_path"] = abs_clip
                                    shot["clip_status"] = "generated"
                    else:
                        for shot in beat.get("shots", []):
                            if shot["shot_id"] == shot_ids[0]:
                                shot["clip_path"] = abs_clip
                                shot["clip_status"] = "generated"

                print(f"  OK: {clip_path} ({size_mb:.1f}MB)")
            else:
                print(f"  FAILED: No video returned")
                failed.extend(shot_ids)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.extend(shot_ids)

        time.sleep(2)

    save_plan(plan)

    print(f"\n{'='*60}")
    print(f"DONE: {total - len([p for p in payloads if any(s in failed for s in (p.get('shot_ids') or [p.get('shot_id', '')]))])}/{total} calls")
    print(f"Total video: {total_duration}s")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 9: Conform (edit + export)
# ---------------------------------------------------------------------------

def step9_conform():
    """Edit clips together with transitions and export final movie."""
    plan, _, _ = load_all()
    conform = compile_conform_payload(plan)

    clips = conform["clips"]
    if not clips:
        print("ERROR: No clips found. Run step 7 or 8 first.")
        return

    print(f"\n{'='*60}")
    print(f"STEP 9: Editorial Conform")
    print(f"Clips: {len(clips)}")
    print(f"{'='*60}\n")

    # List clips in order
    for i, clip in enumerate(clips):
        size_mb = os.path.getsize(clip["path"]) / (1024 * 1024)
        label = clip.get("shot_id") or "+".join(clip.get("shot_ids", []))
        print(f"  [{i+1}] {clip['beat_id']} / {label} ({size_mb:.1f}MB)")

    # Write concat file
    concat_path = os.path.join(FINAL_DIR, "concat_cinematic.txt")
    with open(concat_path, "w") as f:
        for clip in clips:
            rel = os.path.relpath(clip["path"], FINAL_DIR).replace("\\", "/")
            f.write(f"file '{rel}'\n")

    # Concat
    raw_path = os.path.join(FINAL_DIR, "raw_cinematic.mp4")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
           "-i", concat_path, "-c", "copy", raw_path]
    subprocess.run(cmd, capture_output=True)

    if not os.path.isfile(raw_path):
        print("ERROR: Concat failed")
        return

    # Get duration for fade
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", raw_path],
        capture_output=True, text=True,
    )
    total_dur = float(probe.stdout.strip())

    # Build filter: fade to black at end
    fade_dur = conform.get("final_transition", {}).get("duration", 2.0)
    fade_start = total_dur - fade_dur

    output_path = conform.get("output_path", "output/pipeline/final/cinematic_v3.mp4")
    audio_path = conform.get("audio_path")

    # Build ffmpeg command
    filter_parts = [f"fade=t=out:st={fade_start}:d={fade_dur}"]
    vf = ",".join(filter_parts)

    cmd = ["ffmpeg", "-y", "-i", raw_path]
    if audio_path and os.path.isfile(audio_path):
        cmd.extend(["-i", audio_path])
        cmd.extend([
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v", "-map", "1:a",
            "-shortest",
            output_path,
        ])
        print(f"\n  Audio: {os.path.basename(audio_path)}")
    else:
        cmd.extend([
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            output_path,
        ])

    subprocess.run(cmd, capture_output=True)

    # Cleanup
    if os.path.isfile(raw_path):
        os.remove(raw_path)

    if os.path.isfile(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"\n  DONE: {output_path} ({size_mb:.1f}MB, {total_dur:.1f}s)")

        # Update plan
        plan["conform"]["output_path"] = os.path.abspath(output_path)
        save_plan(plan)
    else:
        print("  ERROR: Final encode failed")

    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def show_status():
    """Show full pipeline status."""
    plan, profile, _ = load_all()
    beats = plan.get("beats", [])

    print(f"\n{'='*60}")
    print(f"CINEMATIC PIPELINE STATUS")
    print(f"Project: {plan.get('project', '')}")
    print(f"{'='*60}\n")

    # Sheets
    print("Identity & Location Sheets:")
    for char in plan.get("characters", []):
        pkg_id = char["pkg"]
        path = f"output/preproduction/{pkg_id}/sheet.png"
        status = "OK" if os.path.isfile(path) else "---"
        print(f"  {char['name']}: {status}")
    for loc in plan.get("locations", []):
        pkg_id = loc["pkg"]
        path = f"output/preproduction/{pkg_id}/sheet.png"
        status = "OK" if os.path.isfile(path) else "---"
        print(f"  {loc['name']}: {status}")

    # Scene stills
    print(f"\nScene Stills ({STILLS_DIR}):")
    for beat in beats:
        status = beat.get("scene_still_status", "pending")
        has_file = "OK" if beat.get("scene_still_path") and os.path.isfile(beat["scene_still_path"]) else "---"
        print(f"  {beat['beat_id']} ({beat.get('title', '')}): {has_file} [{status}]")

    # Anchors
    print(f"\nShot Anchors ({ANCHORS_DIR}):")
    for beat in beats:
        for shot in beat.get("shots", []):
            path = f"output/pipeline/anchors_v3/{shot['shot_id']}.png"
            has = "OK" if os.path.isfile(path) else "---"
            print(f"  {shot['shot_id']}: {has} [{shot.get('anchor_status', 'pending')}]")

    # Clips
    print(f"\nVideo Clips ({CLIPS_DIR}):")
    for f_name in sorted(os.listdir(CLIPS_DIR)) if os.path.isdir(CLIPS_DIR) else []:
        if f_name.endswith(".mp4"):
            size = os.path.getsize(os.path.join(CLIPS_DIR, f_name)) / (1024 * 1024)
            print(f"  {f_name} ({size:.1f}MB)")

    # Final
    print(f"\nFinal Output ({FINAL_DIR}):")
    for f_name in sorted(os.listdir(FINAL_DIR)) if os.path.isdir(FINAL_DIR) else []:
        if f_name.endswith(".mp4"):
            size = os.path.getsize(os.path.join(FINAL_DIR, f_name)) / (1024 * 1024)
            print(f"  {f_name} ({size:.1f}MB)")

    # Cost
    print(f"\nCost Estimates:")
    for t in ["draft", "review", "final"]:
        c = estimate_cost(plan, profile, t)
        print(f"  {t}: ${c['total_cost']} ({c['engine']}) — "
              f"{c['shots']} shots, {c['total_duration_sec']}s")

    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not FAL_API_KEY:
        print("ERROR: FAL_API_KEY not set in .env")
        sys.exit(1)

    args = sys.argv[1:] if len(sys.argv) > 1 else ["status"]
    cmd = args[0]

    if cmd == "step1_beats":
        step1_beats()
    elif cmd == "step2_identity":
        step2_identity()
    elif cmd == "step3_location":
        step3_location()
    elif cmd == "step4_stills":
        step4_stills()
    elif cmd == "step4_still":
        beat_id = args[1] if len(args) > 1 else None
        step4_stills(single_beat=beat_id)
    elif cmd == "step5_shotlist":
        step5_shotlist()
    elif cmd == "step6_anchors":
        step6_anchors()
    elif cmd == "step6_anchor":
        shot_id = args[1] if len(args) > 1 else None
        step6_anchors(single_shot=shot_id)
    elif cmd == "step7_preview":
        step7_preview()
    elif cmd == "step8_render":
        tier = args[1] if len(args) > 1 else "review"
        step8_render(tier)
    elif cmd == "step8_shot":
        shot_id = args[1] if len(args) > 1 else None
        tier = args[2] if len(args) > 2 else "review"
        step8_render(tier, single_shot=shot_id)
    elif cmd == "step9_conform":
        step9_conform()
    elif cmd == "cost":
        tier = args[1] if len(args) > 1 else "review"
        plan, profile, _ = load_all()
        cost = estimate_cost(plan, profile, tier)
        print(json.dumps(cost, indent=2))
    elif cmd == "status":
        show_status()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
