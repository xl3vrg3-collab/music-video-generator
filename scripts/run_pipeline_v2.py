"""
LUMN V2 Pipeline — Gemini 3.1 Flash + Kling 3.0 via fal.ai

Runs step-by-step with approval gates between each phase:
  Step 0: Generate character/environment sheets (4 images)
  Step 1: Generate start anchors (5 images)
  Step 2: Generate end anchors (5 images)
  Step 3: Generate video clips (draft tier)
  Step 4: Stitch final movie

Usage:
  python scripts/run_pipeline_v2.py step0           # Generate sheets
  python scripts/run_pipeline_v2.py step0_sheet PKG  # Regenerate single sheet
  python scripts/run_pipeline_v2.py step1           # Generate start anchors
  python scripts/run_pipeline_v2.py step2           # Generate end anchors
  python scripts/run_pipeline_v2.py step3 [tier]    # Generate video (draft/review/final)
  python scripts/run_pipeline_v2.py step3_shot N [tier]  # Single shot retry
  python scripts/run_pipeline_v2.py step4 [tier]    # Stitch movie
  python scripts/run_pipeline_v2.py cost [tier]      # Cost estimate only
  python scripts/run_pipeline_v2.py status           # Show pipeline status
"""

import json
import os
import shutil
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
from lib.prompt_compiler import (
    load_json, compile_anchor_payloads, compile_video_payloads,
    estimate_cost,
)
from lib.preproduction_assets import build_sheet_prompt

PLAN_PATH = "output/pipeline/video_plan_v2.json"
PROFILE_PATH = "output/pipeline/model_profile.json"
PACKAGES_PATH = "output/preproduction/packages.json"
ANCHORS_DIR = "output/pipeline/anchors_v2"
CLIPS_DIR = "output/pipeline/clips_v2"
FINAL_DIR = "output/pipeline/final"

os.makedirs(ANCHORS_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(FINAL_DIR, exist_ok=True)


def load_all():
    plan = load_json(PLAN_PATH)
    profile = load_json(PROFILE_PATH)
    packages = load_json(PACKAGES_PATH)
    return plan, profile, packages


# ---------------------------------------------------------------------------
# Step 0: Generate character/environment sheets
# ---------------------------------------------------------------------------

def step0_sheets(single_pkg=None):
    """Generate reference sheets for all characters + environment.

    Uses Gemini 3.1 Flash text-to-image (no refs needed for sheets).
    """
    packages = load_json(PACKAGES_PATH)
    pkgs = packages.get("packages", [])

    if single_pkg:
        pkgs = [p for p in pkgs if p["package_id"] == single_pkg]
        if not pkgs:
            print(f"ERROR: Package {single_pkg} not found")
            return

    total = len(pkgs)
    failed = []

    print(f"\n{'='*60}")
    print(f"STEP 0: Generate {total} reference sheets")
    print(f"Engine: Gemini 3.1 Flash via fal.ai (text-to-image)")
    print(f"Cost: ~${total * 0.08:.2f}")
    print(f"{'='*60}\n")

    for i, pkg in enumerate(pkgs):
        pkg_id = pkg["package_id"]
        pkg_type = pkg["package_type"]
        name = pkg["name"]
        prompt = build_sheet_prompt(pkg)

        pkg_dir = os.path.join("output/preproduction", pkg_id)
        os.makedirs(pkg_dir, exist_ok=True)
        output = os.path.join(pkg_dir, "sheet.png")

        print(f"[{i+1}/{total}] {name} ({pkg_type})")
        print(f"  Package: {pkg_id}")
        print(f"  Prompt: {prompt[:100]}...")

        try:
            paths = gemini_generate_image(
                prompt=prompt,
                resolution="1K",
                aspect_ratio="16:9",
            )
            if paths and os.path.isfile(paths[0]):
                shutil.copy2(paths[0], output)
                size_kb = os.path.getsize(output) / 1024
                pkg["hero_image_path"] = os.path.abspath(output)
                pkg["sheet_images"] = [{
                    "view": "sheet",
                    "label": f"{pkg_type.title()} Reference",
                    "image_path": os.path.abspath(output),
                    "status": "generated",
                }]
                pkg["generation_metadata"] = {"model": "gemini_3.1_flash", "type": "sheet"}
                pkg["status"] = "generated"
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
    print(f"STEP 0 DONE: {total - len(failed)}/{total} sheets generated")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 1: Generate START anchors
# ---------------------------------------------------------------------------

def step1_start_anchors():
    """Generate start anchor images for all 5 shots."""
    plan, profile, packages = load_all()
    payloads = compile_anchor_payloads(plan, profile, packages)

    start_payloads = [p for p in payloads if p["anchor_type"] == "start"]
    total = len(start_payloads)
    failed = []

    print(f"\n{'='*60}")
    print(f"STEP 1: Generate {total} START anchors")
    print(f"Engine: Gemini 3.1 Flash via fal.ai")
    print(f"Cost: ~${total * 0.08:.2f}")
    print(f"{'='*60}\n")

    for i, payload in enumerate(start_payloads):
        shot_id = payload["shot_id"]
        refs = payload["reference_image_paths"]
        prompt = payload["prompt"]
        output = os.path.join(ANCHORS_DIR, f"{shot_id}_start.png")

        print(f"[{i+1}/{total}] {shot_id} START")
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
                print(f"  OK: {output}")
            else:
                print(f"  FAILED: No image returned")
                failed.append(shot_id)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append(shot_id)

        time.sleep(1)

    print(f"\n{'='*60}")
    print(f"STEP 1 DONE: {total - len(failed)}/{total} start anchors")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 2: Generate END anchors
# ---------------------------------------------------------------------------

def step2_end_anchors():
    """Generate end anchor images for all 5 shots (transition setups)."""
    plan, profile, packages = load_all()
    payloads = compile_anchor_payloads(plan, profile, packages)

    end_payloads = [p for p in payloads if p["anchor_type"] == "end"]
    total = len(end_payloads)
    failed = []

    print(f"\n{'='*60}")
    print(f"STEP 2: Generate {total} END anchors (transition frames)")
    print(f"Engine: Gemini 3.1 Flash via fal.ai")
    print(f"Cost: ~${total * 0.08:.2f}")
    print(f"{'='*60}\n")

    for i, payload in enumerate(end_payloads):
        shot_id = payload["shot_id"]
        refs = payload["reference_image_paths"]
        prompt = payload["prompt"]
        output = os.path.join(ANCHORS_DIR, f"{shot_id}_end.png")

        print(f"[{i+1}/{total}] {shot_id} END")
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
                print(f"  OK: {output}")
            else:
                print(f"  FAILED: No image returned")
                failed.append(shot_id)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append(shot_id)

        time.sleep(1)

    print(f"\n{'='*60}")
    print(f"STEP 2 DONE: {total - len(failed)}/{total} end anchors")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 3: Generate video clips
# ---------------------------------------------------------------------------

def step3_video(tier="draft", shot_filter=None):
    """Generate video clips for all shots (or a single shot)."""
    plan, profile, packages = load_all()
    payloads = compile_video_payloads(plan, profile, packages, tier, ANCHORS_DIR)

    if shot_filter is not None:
        payloads = [p for p in payloads if p["shot_id"] == f"shot_{shot_filter:02d}"]

    total = len(payloads)
    failed = []
    total_duration = 0

    cost = estimate_cost(plan, profile, tier)

    print(f"\n{'='*60}")
    print(f"STEP 3: Generate {total} video clips")
    print(f"Tier: {tier} ({cost['engine']})")
    print(f"Estimated cost: ${cost['video_cost']}")
    print(f"{'='*60}\n")

    for i, payload in enumerate(payloads):
        shot_id = payload["shot_id"]
        duration = payload["duration"]
        start_img = payload["start_image_path"]
        end_img = payload.get("end_image_path")
        prompt = payload["prompt"]
        fal_tier = payload["tier"]
        elements = payload.get("elements")
        neg = payload.get("negative_prompt")

        # Engine tag for filename
        engine_tag = fal_tier.replace("_", "")
        clip_path = os.path.join(CLIPS_DIR, f"{shot_id}_{engine_tag}.mp4")

        print(f"[{i+1}/{total}] {shot_id} — {payload.get('moment', '')}")
        print(f"  Duration: {duration}s | Tier: {fal_tier}")
        print(f"  Start: {os.path.basename(start_img)}")
        if end_img:
            print(f"  End:   {os.path.basename(end_img)}")
        if elements:
            print(f"  Elements: {len(elements)} characters")
        print(f"  Prompt: {prompt[:80]}...")

        if not os.path.isfile(start_img):
            print(f"  ERROR: Start anchor not found: {start_img}")
            failed.append(shot_id)
            continue

        try:
            result_path = kling_image_to_video(
                start_image_path=start_img,
                prompt=prompt,
                duration=duration,
                tier=fal_tier,
                end_image_path=end_img,
                elements=elements,
                negative_prompt=neg or "blur, distortion, low quality, watermark",
            )
            if result_path and os.path.isfile(result_path):
                shutil.copy2(result_path, clip_path)
                size_mb = os.path.getsize(clip_path) / (1024 * 1024)
                print(f"  OK: {clip_path} ({size_mb:.1f}MB)")
                total_duration += duration
            else:
                print(f"  FAILED: No video returned")
                failed.append(shot_id)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append(shot_id)

        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"STEP 3 DONE: {total - len(failed)}/{total} clips ({tier})")
    print(f"Total video: {total_duration}s")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    cps = cost['video_cost'] / max(cost['total_duration_sec'], 1)
    print(f"Cost: ~${total_duration * cps:.2f}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 3 Multi: Multi-shot video generation
# ---------------------------------------------------------------------------

def step3_multi(tier="draft"):
    """Generate video using multi-shot strategy from plan.

    Call 1: shot_00 — standalone with start + end anchors
    Call 2: shot_01 + shot_02 — multi-shot, shot_01 start anchor only
    Call 3: shot_03 + shot_04 — multi-shot, shot_03 start anchor only
    """
    plan, profile, packages = load_all()

    # Resolve Kling tier
    tier_key = profile.get("tiers", {}).get(tier, "video_engine")
    vid_profile = profile.get(tier_key, profile.get("video_engine", {}))
    engine_id = vid_profile.get("id", "kling_v3_pro")
    tier_map = {
        "kling_v3_standard": "v3_standard",
        "kling_v3_pro": "v3_pro",
        "kling_o3_standard": "o3_standard",
        "kling_o3_pro": "o3_pro",
    }
    fal_tier = tier_map.get(engine_id, "v3_standard")
    engine_tag = fal_tier.replace("_", "")

    shots = plan.get("shots", [])
    shot_map = {s["shot_id"]: s for s in shots}
    groups = plan.get("multi_shot_strategy", {}).get("pairs", [])

    if not groups:
        print("ERROR: No multi_shot_strategy.pairs in plan")
        return

    total_calls = len(groups)
    failed = []
    total_duration = 0

    cost = estimate_cost(plan, profile, tier)
    print(f"\n{'='*60}")
    print(f"STEP 3 MULTI: {total_calls} Kling calls (multi-shot strategy)")
    print(f"Tier: {tier} ({engine_id})")
    print(f"Estimated cost: ${cost['video_cost']}")
    print(f"{'='*60}\n")

    for gi, group in enumerate(groups):
        shot_ids = group["shots"]
        group_dur = group.get("duration", 0)
        reason = group.get("reason", "")

        print(f"[Call {gi+1}/{total_calls}] {shot_ids}")
        print(f"  Reason: {reason}")
        print(f"  Duration: {group_dur}s | Tier: {fal_tier}")

        first_shot = shot_map[shot_ids[0]]
        start_img = os.path.join(ANCHORS_DIR, f"{shot_ids[0]}_start.png")

        if not os.path.isfile(start_img):
            print(f"  ERROR: Start anchor not found: {start_img}")
            failed.extend(shot_ids)
            continue

        # Elements disabled — character appearance is baked into start anchors.
        # Kling elements can cause brief reference image flash artifacts.
        all_elements = []

        if len(shot_ids) == 1:
            # ---- Single shot: start anchor only, prompt drives the motion ----
            shot = first_shot

            print(f"  Mode: single shot, start anchor only (prompt-driven)")

            prompt = shot.get("video_prompt", "")
            neg = shot.get("negative_prompt", "blur, distortion, low quality, watermark")

            try:
                result_path = kling_image_to_video(
                    start_image_path=start_img,
                    prompt=prompt,
                    duration=shot.get("duration", 5),
                    tier=fal_tier,
                    end_image_path=None,
                    elements=all_elements[:4] or None,
                    negative_prompt=neg,
                )
                if result_path and os.path.isfile(result_path):
                    clip_path = os.path.join(CLIPS_DIR,
                                             f"{shot_ids[0]}_{engine_tag}.mp4")
                    shutil.copy2(result_path, clip_path)
                    size_mb = os.path.getsize(clip_path) / (1024 * 1024)
                    print(f"  OK: {clip_path} ({size_mb:.1f}MB)")
                    total_duration += shot.get("duration", 5)
                else:
                    print(f"  FAILED: No video returned")
                    failed.extend(shot_ids)
            except Exception as e:
                print(f"  ERROR: {e}")
                failed.extend(shot_ids)

        else:
            # ---- Multi-shot: start anchor + multi_prompt, NO end anchor ----
            multi_prompt = []
            for sid in shot_ids:
                shot = shot_map[sid]
                # Use condensed multi_shot_prompt (512 char limit) if available
                prompt = shot.get("multi_shot_prompt") or shot.get("video_prompt", "")
                multi_prompt.append({
                    "prompt": prompt[:512],
                    "duration": str(shot.get("duration", 5)),
                })
                print(f"  Shot {sid}: {shot.get('duration', 5)}s — "
                      f"{shot.get('moment', '')}")

            print(f"  Mode: multi-shot ({len(multi_prompt)} prompts, "
                  f"no end anchor)")

            neg = "blur, distortion, low quality, watermark"
            try:
                result_path = kling_image_to_video(
                    start_image_path=start_img,
                    prompt="",  # ignored when multi_prompt set
                    duration=group_dur,
                    tier=fal_tier,
                    end_image_path=None,  # no end anchor for multi-shot
                    elements=all_elements[:4] or None,
                    multi_prompt=multi_prompt,
                    negative_prompt=neg,
                )
                if result_path and os.path.isfile(result_path):
                    # Save as group clip (first shot ID + "multi")
                    clip_path = os.path.join(
                        CLIPS_DIR,
                        f"{'_'.join(shot_ids)}_{engine_tag}_multi.mp4")
                    shutil.copy2(result_path, clip_path)
                    size_mb = os.path.getsize(clip_path) / (1024 * 1024)
                    print(f"  OK: {clip_path} ({size_mb:.1f}MB)")
                    total_duration += group_dur
                else:
                    print(f"  FAILED: No video returned")
                    failed.extend(shot_ids)
            except Exception as e:
                print(f"  ERROR: {e}")
                failed.extend(shot_ids)

        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"STEP 3 MULTI DONE: {total_calls - len([g for g in groups if any(s in failed for s in g['shots'])])}/{total_calls} calls")
    print(f"Total video: {total_duration}s")
    if failed:
        print(f"Failed shots: {', '.join(failed)}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Step 4: Stitch final movie
# ---------------------------------------------------------------------------

def step4_stitch(tier="draft"):
    """Stitch clips into final movie with fade to black."""
    tier_map = {
        "draft": "v3standard",
        "review": "v3pro",
        "final": "o3pro",
    }
    engine_tag = tier_map.get(tier, "v3standard")

    plan, _, _ = load_all()
    shots = plan.get("shots", [])

    # Find clips in correct shot order — check multi-shot groups in sequence
    groups = plan.get("multi_shot_strategy", {}).get("pairs", [])
    clips = []

    if groups:
        # Use group order (already in shot sequence)
        for group in groups:
            shot_ids = group["shots"]
            # Multi-shot clip?
            multi_name = f"{'_'.join(shot_ids)}_{engine_tag}_multi.mp4"
            multi_path = os.path.join(CLIPS_DIR, multi_name)
            if os.path.isfile(multi_path):
                clips.append(multi_path)
            else:
                # Fall back to individual clips for this group
                for sid in shot_ids:
                    clip_path = os.path.join(CLIPS_DIR, f"{sid}_{engine_tag}.mp4")
                    if os.path.isfile(clip_path):
                        clips.append(clip_path)
                    else:
                        print(f"  WARNING: Missing clip: {sid}")
    else:
        # No groups — just use shot order
        for shot in shots:
            clip_path = os.path.join(CLIPS_DIR, f"{shot['shot_id']}_{engine_tag}.mp4")
            if os.path.isfile(clip_path):
                clips.append(clip_path)
            else:
                print(f"  WARNING: Missing clip: {shot['shot_id']}")

    if not clips:
        print("ERROR: No clips found to stitch")
        return

    print(f"\n{'='*60}")
    print(f"STEP 4: Stitch {len(clips)} clips ({tier})")
    print(f"{'='*60}\n")

    # Write concat file
    concat_path = os.path.join(FINAL_DIR, f"concat_{engine_tag}.txt")
    with open(concat_path, "w") as f:
        for clip in clips:
            rel = os.path.relpath(clip, FINAL_DIR).replace("\\", "/")
            f.write(f"file '{rel}'\n")

    # Concat
    import subprocess
    raw_path = os.path.join(FINAL_DIR, f"raw_{engine_tag}.mp4")
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
    fade_start = total_dur - 2.0

    # Apply fade to black
    final_path = os.path.join(FINAL_DIR, f"buddy_kling_{engine_tag}.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", raw_path,
        "-vf", f"fade=t=out:st={fade_start}:d=2.0",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        final_path,
    ]
    subprocess.run(cmd, capture_output=True)

    # Cleanup
    if os.path.isfile(raw_path):
        os.remove(raw_path)

    if os.path.isfile(final_path):
        size_mb = os.path.getsize(final_path) / (1024 * 1024)
        print(f"  DONE: {final_path} ({size_mb:.1f}MB, {total_dur:.1f}s)")
    else:
        print("  ERROR: Final encode failed")

    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def show_status():
    """Show current pipeline status."""
    plan, profile, _ = load_all()
    shots = plan.get("shots", [])

    print(f"\n{'='*60}")
    print(f"PIPELINE STATUS")
    print(f"{'='*60}\n")

    print("Sheets:")
    for pkg_type, pkg_id in [("Buddy", "pkg_char_c852b9c5"),
                              ("Owen", "pkg_char_0a0b6a7c"),
                              ("Maya", "pkg_char_67dab2d7"),
                              ("Park", "pkg_envi_82c911b9")]:
        path = f"output/preproduction/{pkg_id}/sheet.png"
        exists = "OK" if os.path.isfile(path) else "MISSING"
        print(f"  {pkg_type}: {exists}")

    print(f"\nAnchors ({ANCHORS_DIR}):")
    for shot in shots:
        sid = shot["shot_id"]
        start = "OK" if os.path.isfile(os.path.join(ANCHORS_DIR, f"{sid}_start.png")) else "---"
        end = "OK" if os.path.isfile(os.path.join(ANCHORS_DIR, f"{sid}_end.png")) else "---"
        print(f"  {sid}: start={start}  end={end}")

    print(f"\nClips ({CLIPS_DIR}):")
    for tier_tag in ["v3standard", "v3pro", "o3pro"]:
        clips = [s for s in shots
                 if os.path.isfile(os.path.join(CLIPS_DIR, f"{s['shot_id']}_{tier_tag}.mp4"))]
        if clips:
            print(f"  {tier_tag}: {len(clips)}/{len(shots)} clips")

    print(f"\nFinal movies ({FINAL_DIR}):")
    for f in os.listdir(FINAL_DIR):
        if f.endswith(".mp4"):
            size = os.path.getsize(os.path.join(FINAL_DIR, f)) / (1024 * 1024)
            print(f"  {f} ({size:.1f}MB)")

    print(f"\nCost estimates:")
    for tier in ["draft", "review", "final"]:
        cost = estimate_cost(plan, profile, tier)
        print(f"  {tier}: ${cost['total_cost']} ({cost['engine']})")

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

    if cmd == "step0":
        step0_sheets()
    elif cmd == "step0_sheet":
        pkg_id = args[1] if len(args) > 1 else None
        step0_sheets(single_pkg=pkg_id)
    elif cmd == "step1":
        step1_start_anchors()
    elif cmd == "step2":
        step2_end_anchors()
    elif cmd == "step3":
        tier = args[1] if len(args) > 1 else "draft"
        step3_video(tier)
    elif cmd == "step3_multi":
        tier = args[1] if len(args) > 1 else "draft"
        step3_multi(tier)
    elif cmd == "step3_shot":
        shot_num = int(args[1]) if len(args) > 1 else 0
        tier = args[2] if len(args) > 2 else "draft"
        step3_video(tier, shot_filter=shot_num)
    elif cmd == "step4":
        tier = args[1] if len(args) > 1 else "draft"
        step4_stitch(tier)
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
