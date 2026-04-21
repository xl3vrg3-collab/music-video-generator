"""
V4 Draft Generation Pipeline
=============================
Generates all assets for production_plan_v4.json in the correct order:
  1. Scene stills (4)
  2. Identity gate anchor (shot 1B) — QA checkpoint
  3. Remaining anchors (6) — using 1B as additional ref
  4. Video clips (7) — V3 Standard draft tier
  5. Conform stitch — hard cuts, no music, fade to black

Run: python scripts/generate_v4_draft.py
"""

import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.fal_client import gemini_edit_image, kling_image_to_video
from lib.cinematic_compiler import (
    compile_shot_anchor_payloads, compile_video_payloads,
    compile_conform_payload, compile_conform_from_ti,
    compile_transition_intelligence, conform_from_payload, load_json,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)

PLAN_PATH = "output/pipeline/production_plan_v4.json"
PROFILE_PATH = "output/pipeline/model_profile.json"
PACKAGES_PATH = "output/preproduction/packages.json"

STILLS_DIR = "output/pipeline/scene_stills_v4"
ANCHORS_DIR = "output/pipeline/anchors_v4"
CLIPS_DIR = "output/pipeline/clips_v4"
FINAL_DIR = "output/pipeline/final"


def load_plan():
    return load_json(PLAN_PATH)


def save_plan(plan):
    with open(PLAN_PATH, "w") as f:
        json.dump(plan, f, indent=2)


def ensure_dirs():
    for d in [STILLS_DIR, ANCHORS_DIR, CLIPS_DIR, FINAL_DIR]:
        os.makedirs(d, exist_ok=True)


# -------------------------------------------------------------------------
# Step 1: Scene Stills
# -------------------------------------------------------------------------

def generate_scene_stills(plan, packages):
    """Generate one mood still per beat."""
    print("\n" + "=" * 60)
    print("STEP 1: SCENE STILLS")
    print("=" * 60)

    for beat in plan["beats"]:
        beat_id = beat["beat_id"]
        out_path = os.path.join(STILLS_DIR, f"{beat_id}.png")

        if beat.get("scene_still_status") == "generated" and os.path.isfile(beat.get("scene_still_path", "")):
            print(f"  [{beat_id}] Already generated, skipping")
            continue

        prompt = beat.get("scene_still_prompt", "")
        if not prompt:
            print(f"  [{beat_id}] No scene still prompt, skipping")
            continue

        # Collect refs
        ref_paths = []
        for pkg_id in beat.get("scene_still_refs", []):
            for pkg in packages.get("packages", []):
                if pkg["package_id"] == pkg_id:
                    hero = pkg.get("hero_image_path", "")
                    if hero and os.path.isfile(hero):
                        ref_paths.append(hero)

        print(f"\n  [{beat_id}] Generating scene still ({len(ref_paths)} refs)...")
        try:
            paths = gemini_edit_image(prompt, ref_paths)
            if paths:
                shutil.copy2(paths[0], out_path)
                beat["scene_still_path"] = os.path.abspath(out_path)
                beat["scene_still_status"] = "generated"
                save_plan(plan)
                print(f"  [{beat_id}] OK -> {out_path}")
            else:
                print(f"  [{beat_id}] FAILED — no image returned")
        except Exception as e:
            print(f"  [{beat_id}] ERROR: {e}")

    return plan


# -------------------------------------------------------------------------
# Step 2: Identity Gate Anchor (Shot 1B)
# -------------------------------------------------------------------------

def generate_identity_gate(plan, packages):
    """Generate shot 1B anchor — the identity lock frame."""
    print("\n" + "=" * 60)
    print("STEP 2: IDENTITY GATE (Shot 1B)")
    print("=" * 60)

    gate_shot_id = plan.get("identity_gate", {}).get("gate_shot", "beat_01_shot_b")

    for beat in plan["beats"]:
        for shot in beat.get("shots", []):
            if shot["shot_id"] != gate_shot_id:
                continue

            out_path = os.path.join(ANCHORS_DIR, f"{shot['shot_id']}.png")

            if shot.get("anchor_status") == "generated" and os.path.isfile(shot.get("anchor_path", "")):
                print(f"  [{shot['shot_id']}] Already generated, skipping")
                return plan

            prompt = shot.get("anchor_prompt", "")
            if not prompt:
                print(f"  [{shot['shot_id']}] No anchor prompt!")
                return plan

            # Refs: canonical sheets + scene still
            ref_paths = []
            for pkg_id in shot.get("anchor_refs", []):
                for pkg in packages.get("packages", []):
                    if pkg["package_id"] == pkg_id:
                        hero = pkg.get("hero_image_path", "")
                        if hero and os.path.isfile(hero):
                            ref_paths.append(hero)

            scene_still = beat.get("scene_still_path")
            if scene_still and os.path.isfile(scene_still):
                ref_paths.append(scene_still)

            print(f"\n  [{shot['shot_id']}] IDENTITY GATE — generating ({len(ref_paths)} refs)...")
            print(f"  This frame MUST match the canonical dog sheet.")
            try:
                paths = gemini_edit_image(prompt, ref_paths)
                if paths:
                    shutil.copy2(paths[0], out_path)
                    shot["anchor_path"] = os.path.abspath(out_path)
                    shot["anchor_status"] = "generated"
                    save_plan(plan)
                    print(f"  [{shot['shot_id']}] OK -> {out_path}")
                    print(f"  >>> QA this image against the dog sheet before proceeding! <<<")
                else:
                    print(f"  [{shot['shot_id']}] FAILED — no image returned")
            except Exception as e:
                print(f"  [{shot['shot_id']}] ERROR: {e}")

    return plan


# -------------------------------------------------------------------------
# Step 3: Remaining Anchors (using 1B as additional ref)
# -------------------------------------------------------------------------

def generate_remaining_anchors(plan, packages):
    """Generate all anchors except the identity gate."""
    print("\n" + "=" * 60)
    print("STEP 3: REMAINING ANCHORS")
    print("=" * 60)

    gate_shot_id = plan.get("identity_gate", {}).get("gate_shot", "beat_01_shot_b")

    # Find the identity gate image to use as additional ref
    gate_image = None
    for beat in plan["beats"]:
        for shot in beat.get("shots", []):
            if shot["shot_id"] == gate_shot_id:
                gate_image = shot.get("anchor_path")
                break

    if gate_image and os.path.isfile(gate_image):
        print(f"  Using identity gate as additional ref: {os.path.basename(gate_image)}")
    else:
        print(f"  WARNING: Identity gate image not found! Proceeding without it.")
        gate_image = None

    for beat in plan["beats"]:
        scene_still = beat.get("scene_still_path")

        for shot in beat.get("shots", []):
            shot_id = shot["shot_id"]

            # Skip the identity gate (already generated)
            if shot_id == gate_shot_id:
                continue

            out_path = os.path.join(ANCHORS_DIR, f"{shot_id}.png")

            if shot.get("anchor_status") == "generated" and os.path.isfile(shot.get("anchor_path", "")):
                print(f"  [{shot_id}] Already generated, skipping")
                continue

            prompt = shot.get("anchor_prompt", "")
            if not prompt:
                continue

            # Refs: canonical sheets + scene still + identity gate
            ref_paths = []
            for pkg_id in shot.get("anchor_refs", []):
                for pkg in packages.get("packages", []):
                    if pkg["package_id"] == pkg_id:
                        hero = pkg.get("hero_image_path", "")
                        if hero and os.path.isfile(hero):
                            ref_paths.append(hero)

            if scene_still and os.path.isfile(scene_still):
                ref_paths.append(scene_still)

            # Add identity gate as additional ref for dog consistency
            if gate_image:
                ref_paths.append(gate_image)

            print(f"\n  [{shot_id}] Generating anchor ({len(ref_paths)} refs, incl gate)...")
            try:
                paths = gemini_edit_image(prompt, ref_paths)
                if paths:
                    shutil.copy2(paths[0], out_path)
                    shot["anchor_path"] = os.path.abspath(out_path)
                    shot["anchor_status"] = "generated"
                    save_plan(plan)
                    print(f"  [{shot_id}] OK -> {out_path}")
                else:
                    print(f"  [{shot_id}] FAILED — no image returned")
            except Exception as e:
                print(f"  [{shot_id}] ERROR: {e}")

    return plan


# -------------------------------------------------------------------------
# Step 4: Video Clips (V3 Standard draft)
# -------------------------------------------------------------------------

def generate_video_clips(plan, profile, packages):
    """Generate all video clips from anchors."""
    print("\n" + "=" * 60)
    print("STEP 4: VIDEO CLIPS (V3 Standard draft)")
    print("=" * 60)

    payloads = compile_video_payloads(plan, profile, packages, tier="draft")

    for payload in payloads:
        shot_id = payload.get("shot_id", "_".join(payload.get("shot_ids", [])))
        is_multi = payload.get("is_multi_shot", False)

        # Build output filename
        if is_multi:
            fname = f"{'_'.join(payload['shot_ids'])}_v3standard_multi.mp4"
        else:
            fname = f"{shot_id}_v3standard.mp4"
        out_path = os.path.join(CLIPS_DIR, fname)

        # Check if already generated
        already_done = False
        for beat in plan["beats"]:
            for shot in beat.get("shots", []):
                if shot["shot_id"] == (payload.get("shot_id") or payload.get("shot_ids", [""])[0]):
                    if shot.get("clip_status") == "generated" and os.path.isfile(shot.get("clip_path", "")):
                        print(f"  [{shot_id}] Already generated, skipping")
                        already_done = True
        if already_done:
            continue

        # Get anchor image
        anchor_path = payload.get("start_image_path", "")
        if not os.path.isfile(anchor_path):
            print(f"  [{shot_id}] Anchor not found: {anchor_path}, skipping")
            continue

        print(f"\n  [{shot_id}] Generating {'multi-shot' if is_multi else 'single'} clip ({payload['duration']}s)...")

        try:
            if is_multi:
                clip_path = kling_image_to_video(
                    start_image_path=anchor_path,
                    prompt="",
                    duration=payload["duration"],
                    tier=payload["tier"],
                    multi_prompt=payload["multi_prompt"],
                    negative_prompt=payload["negative_prompt"],
                    generate_audio=True,
                )
            else:
                clip_path = kling_image_to_video(
                    start_image_path=anchor_path,
                    prompt=payload["prompt"],
                    duration=payload["duration"],
                    tier=payload["tier"],
                    negative_prompt=payload["negative_prompt"],
                    generate_audio=True,
                )

            shutil.copy2(clip_path, out_path)
            abs_out = os.path.abspath(out_path)

            # Update plan
            for beat in plan["beats"]:
                for shot in beat.get("shots", []):
                    if is_multi and shot["shot_id"] in payload.get("shot_ids", []):
                        shot["clip_path"] = abs_out
                        shot["clip_status"] = "generated"
                    elif not is_multi and shot["shot_id"] == shot_id:
                        shot["clip_path"] = abs_out
                        shot["clip_status"] = "generated"
            save_plan(plan)
            print(f"  [{shot_id}] OK -> {out_path}")

        except Exception as e:
            print(f"  [{shot_id}] ERROR: {e}")

    return plan


# -------------------------------------------------------------------------
# Step 5: Conform Stitch
# -------------------------------------------------------------------------

def run_conform(plan, ti_result=None):
    """Stitch all clips with TI-informed transitions, no music."""
    print("\n" + "=" * 60)
    print("STEP 5: CONFORM (TI-informed transitions, no music)")
    print("=" * 60)

    from lib.video_stitcher import stitch

    # Use TI-based conform if available, otherwise fall back to standard
    if ti_result:
        payload = compile_conform_from_ti(plan, ti_result)
        print("  Using Transition Intelligence for conform decisions")
    else:
        payload = compile_conform_payload(plan)
        print("  Using standard conform (no TI data)")
    conform = conform_from_payload(payload)

    # Override: no audio for V4
    conform["audio_path"] = None

    # Verify clips
    missing = [c for c in conform["clip_paths"] if not os.path.isfile(c)]
    if missing:
        print(f"  MISSING CLIPS: {missing}")
        print("  Cannot conform. Generate missing clips first.")
        return

    output = os.path.join(FINAL_DIR, "cinematic_v4.mp4")
    conform["output_path"] = output

    print(f"  Clips: {len(conform['clip_paths'])}")
    for i, (c, t) in enumerate(zip(conform["clip_paths"], conform["transitions"])):
        print(f"    [{i}] {t:12s} -> {os.path.basename(c)}")
    print(f"  Fade: {conform['fade_dur']}s")
    print(f"  Audio: None")
    print(f"  Output: {output}")

    result = stitch(
        clip_paths=conform["clip_paths"],
        audio_path=None,
        output_path=output,
        crossfade=0.0,
        fade_dur=conform["fade_dur"],
        transitions=conform["transitions"],
        default_transition="hard_cut",
        progress_cb=lambda msg: print(f"    [{msg}]"),
    )

    size_kb = os.path.getsize(result) // 1024
    print(f"\n  DONE: {result} ({size_kb}KB)")
    return result


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

def run_transition_intelligence(plan, packages):
    """Run Transition Intelligence on all cut points.

    Scores every shot pair, assigns strategies, generates bridge frames
    where needed. Saves TI results to the plan.
    """
    print("\n" + "=" * 60)
    print("STEP 4.5: TRANSITION INTELLIGENCE")
    print("=" * 60)

    from lib.cinematic_compiler import compile_transition_intelligence
    from lib.transition_judge import print_judge_report
    from lib.transition_strategy import print_strategy_report

    ti = compile_transition_intelligence(plan, packages)

    # Print reports
    print_judge_report(ti["judge_results"])
    print_strategy_report(ti["strategies"])

    # Summary
    summary = ti["summary"]
    print(f"\n  --- TI Summary ---")
    print(f"  Cuts scored:    {summary['total_cuts']}")
    print(f"  Avg composite:  {summary['average_composite']}/10")
    print(f"  Risk: {summary['risk_distribution']}")
    print(f"  Strategies: {summary['strategy_distribution']}")
    if summary["weakest_transition"]:
        wt = summary["weakest_transition"]
        print(f"  Weakest cut:    {wt['from_shot']}→{wt['to_shot']} "
              f"(composite={wt['composite']})")

    # Generate bridge frames where strategy demands them
    bridge_count = 0
    for i, strat in enumerate(ti["strategies"]):
        if strat["strategy"] == "bridge_frame":
            gen_params = strat.get("generation_params", {})
            bridge_prompt = gen_params.get("bridge_prompt", "")
            bridge_refs = gen_params.get("bridge_refs", [])

            if bridge_prompt:
                bridge_id = f"bridge_{i}"
                out_path = os.path.join(ANCHORS_DIR, f"{bridge_id}.png")

                if os.path.isfile(out_path):
                    print(f"\n  [{bridge_id}] Bridge anchor exists, skipping")
                    gen_params["bridge_anchor_path"] = os.path.abspath(out_path)
                    continue

                print(f"\n  [{bridge_id}] Generating bridge anchor...")
                try:
                    paths = gemini_edit_image(bridge_prompt, bridge_refs)
                    if paths:
                        shutil.copy2(paths[0], out_path)
                        gen_params["bridge_anchor_path"] = os.path.abspath(out_path)
                        print(f"  [{bridge_id}] OK -> {out_path}")
                        bridge_count += 1
                    else:
                        print(f"  [{bridge_id}] FAILED — no image returned")
                except Exception as e:
                    print(f"  [{bridge_id}] ERROR: {e}")

    # Save TI results to plan for conform step
    plan["transition_intelligence"] = {
        "summary": ti["summary"],
        "per_cut": [
            {
                "from_shot": strat["judge"]["from_shot"],
                "to_shot": strat["judge"]["to_shot"],
                "composite": strat["judge"]["composite"],
                "risk_level": strat["judge"]["risk_level"],
                "strategy": strat["strategy"],
                "importance": strat.get("importance", "standard"),
                "reason": strat.get("reason", ""),
            }
            for strat in ti["strategies"]
        ],
    }
    save_plan(plan)

    print(f"\n  Bridge frames generated: {bridge_count}")
    return plan, ti


def run_critic(plan, ti_result=None):
    """Run the post-render quality critic on all clips."""
    print("\n" + "=" * 60)
    print("STEP 6: RENDER CRITIC")
    print("=" * 60)

    from lib.render_critic import critique_all_clips, print_critic_report

    strategies = ti_result["strategies"] if ti_result else None
    result = critique_all_clips(plan, strategies)
    print_critic_report(result)
    return result


def main():
    print("=" * 60)
    print("V4 DRAFT GENERATION PIPELINE")
    print("=" * 60)

    ensure_dirs()
    plan = load_plan()
    profile = load_json(PROFILE_PATH)
    packages = load_json(PACKAGES_PATH)

    start = time.time()

    # Step 1: Scene stills
    plan = generate_scene_stills(plan, packages)

    # Step 2: Identity gate
    plan = generate_identity_gate(plan, packages)

    # Step 3: Remaining anchors
    plan = generate_remaining_anchors(plan, packages)

    # Step 4: Video clips
    plan = generate_video_clips(plan, profile, packages)

    # Step 4.5: Transition Intelligence (score all cuts, assign strategies)
    plan, ti_result = run_transition_intelligence(plan, packages)

    # Step 5: Conform (uses TI strategies for cut decisions)
    run_conform(plan, ti_result)

    # Step 6: Render Critic (post-render quality check)
    critic_result = run_critic(plan, ti_result)

    # Step 7: Log to learning system
    try:
        from lib.learning_system import log_attempt, print_learning_summary
        for i, beat in enumerate(plan.get("beats", [])):
            for shot in beat.get("shots", []):
                log_attempt(
                    project_id=plan.get("project", "unknown"),
                    scene_id=beat.get("beat_id", ""),
                    shot_id=shot.get("shot_id", ""),
                    attempt_data={
                        "chosen_strategy": "motivated_cut",
                        "attempt_number": 1,
                        "final_outcome": "pass",
                        "prompt_version": "v4",
                        "duration_sec": shot.get("duration", 5),
                    },
                )
        print_learning_summary()
    except Exception as e:
        print(f"  Learning system log failed: {e}")

    elapsed = int(time.time() - start)
    print(f"\n{'=' * 60}")
    print(f"PIPELINE COMPLETE — {elapsed}s total")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
