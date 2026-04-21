"""V6 Full Pipeline — Gemini anchors + Sonnet review + Kling clips + conform.

Correct engine routing:
  - Anchor stills: Gemini 3.1 Flash edit mode (refs carry visual identity)
  - Prompts: camera/pose/framing ONLY — no character or environment descriptions
  - Video clips: Kling 3.0 via fal.ai (motion prompts only)
  - Character consistency: Kling elements system (frontal + ref images)
  - Transition control: Kling end_image_path where applicable

Usage:
  python scripts/pipeline_v6_gemini_kling.py anchors          # Generate 1 anchor per shot (re-run individual shots if QA fails)
  python scripts/pipeline_v6_gemini_kling.py select            # Sonnet picks best candidate per shot (when 3 candidates exist)
  python scripts/pipeline_v6_gemini_kling.py review            # Sonnet reviews anchor transitions
  python scripts/pipeline_v6_gemini_kling.py clips             # Kling video from selected anchors
  python scripts/pipeline_v6_gemini_kling.py conform           # Stitch final video
  python scripts/pipeline_v6_gemini_kling.py all               # Full pipeline
"""
import json
import os
import shutil
import sys
import time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import (
    gemini_edit_image,
    kling_image_to_video,
)
from lib.claude_client import call_json, call_vision_json, OPUS_MODEL

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PLAN_PATH = "output/pipeline/production_plan_v4.json"
PKGS_PATH = "output/preproduction/packages.json"
ANCHOR_DIR = "output/pipeline/anchors_v6"
CLIPS_DIR = "output/pipeline/clips_v6"
FINAL_DIR = "output/pipeline/final"
REVIEW_PATH = "output/pipeline/learning/sonnet_review_v6.json"

for d in [ANCHOR_DIR, CLIPS_DIR, FINAL_DIR]:
    os.makedirs(d, exist_ok=True)

# ---------------------------------------------------------------------------
# Load plan + packages
# ---------------------------------------------------------------------------
def load_plan():
    with open(PLAN_PATH) as f:
        return json.load(f)

def save_plan(plan):
    with open(PLAN_PATH, "w") as f:
        json.dump(plan, f, indent=2)

def load_packages():
    with open(PKGS_PATH) as f:
        return json.load(f)

def get_all_shots(plan):
    shots = []
    for beat in plan["beats"]:
        for shot in beat["shots"]:
            shot["_beat_id"] = beat["beat_id"]
            shot["_characters"] = beat.get("characters", [])
            shot["_location_pkg"] = beat.get("location_pkg", "")
            shot["_emotion"] = beat.get("emotion", "")
            shot["_energy"] = beat.get("energy", 0)
            shots.append(shot)
    return shots

# ---------------------------------------------------------------------------
# LEAN PROMPTS — camera/pose/framing ONLY
# Reference images carry: character identity, environment, style
# ONE ENVIRONMENT: cobblestone park path + adjacent grass edge
# ---------------------------------------------------------------------------
LEAN_PROMPTS = {
    "beat_01_shot_a": (
        "Wide establishing shot, 24mm lens, ground-level low angle. "
        "Dog mid-distance center-left on cobblestone path, facing camera, head lowered, "
        "standing still. Frozen leaf mid-air. Iron fence right, oak canopy above. "
        "Late afternoon dappled light through trees, long shadows."
    ),
    "beat_01_shot_b": (
        "Medium shot, 50mm lens, eye level at dog height. "
        "Dog mid-stride on cobblestone, three-quarter front angle toward camera. "
        "Red collar and silver tag visible. Fallen leaves on path. "
        "Shallow DOF f/2.8, warm backlight through trees, rim light on fur edges."
    ),
    "beat_02_shot_a": (
        "Medium-close shot, 50mm lens, eye level at dog height. "
        "Dog seated on cobblestone path, body facing camera, head turned right. "
        "Ears shifted slightly forward, alert. Collar visible. "
        "Iron fence and oak trees in soft-focus background. Warm afternoon light."
    ),
    "beat_02_shot_b": (
        "Close-up portrait, 85mm telephoto, shallow DOF f/2.8. "
        "Dog face filling 65% of frame, three-quarter angle. "
        "Heavy blink, tired expression. Ears in relaxed pendant drape. "
        "Collar and silver tag visible. Warm light on right ear, cobblestone bokeh below."
    ),
    "beat_03_shot_a": (
        "Wide shot, 35mm lens, slightly low angle. "
        "Dog standing on cobblestone path edge, four paws planted, "
        "head turned right looking toward grass area beside the path. "
        "Girl seated on grass just off the cobblestone, hand extended toward dog. "
        "Iron fence visible behind, oak trees, dappled afternoon light."
    ),
    "beat_04_shot_a": (
        "Medium-wide, 85mm telephoto, ground-level low angle. "
        "Dog in full-sprint gallop on cobblestone path, body fully extended, "
        "front legs reaching forward, ears swept back. "
        "Iron fence and oak trees blur in background. "
        "Warm backlight, rim light on fur, fallen leaves on path."
    ),
    "beat_04_shot_b": (
        "Medium shot, 85mm telephoto, low angle. "
        "Dog front paws on chest of crouching man, reunion embrace. "
        "Man crouched on cobblestone path, arms around dog. "
        "Iron fence in background, oak canopy above, warm golden haze."
    ),
}

# End-image anchor prompt for 3A — Maya on grass (the destination of the tracking shot)
LEAN_PROMPT_3A_END = (
    "Medium shot, 35mm lens, eye level. "
    "Dog approaching girl seated on grass just beside cobblestone path edge. "
    "Girl extends hand toward dog, dog sniffing hand. "
    "Cobblestone path visible at left edge of frame. Iron fence behind. "
    "Warm dappled afternoon light through oak trees."
)

# ---------------------------------------------------------------------------
# VIDEO PROMPTS — Realism framework
# Rules: Camera FIRST as own sentence. Subject action separate. 1-2 actions max.
# No scene re-description. No sound words. Environmental micro-motion.
# 15-25 words for 5s, 25-40 words for 10s.
# ---------------------------------------------------------------------------
VIDEO_PROMPTS = {
    "beat_01_shot_a": (
        "Camera slowly dollies forward along the path. "
        "Dog stands still, then takes one step toward camera. "
        "Leaves drift in breeze."
    ),
    "beat_01_shot_b": (
        "Camera tracks handheld following the dog. "
        "Dog walks forward, nose low, sniffing the ground. "
        "Subtle wind moves fur."
    ),
    "beat_02_shot_a": (
        "Tripod shot, fixed camera, slight handheld drift. "
        "Dog sits still, head slowly turns right, scanning. "
        "Leaves skitter across ground."
    ),
    "beat_02_shot_b": (
        "Camera holds nearly static with imperceptible push in. "
        "Dog breathes, blinks slowly. A long moment. "
        "Then ears shift — something off-screen."
    ),
    "beat_03_shot_a": (
        "Camera tracks handheld, following dog from behind. "
        "Dog steps off cobblestone onto grass, walks cautiously toward seated girl. "
        "Girl extends hand. Dog pauses, stretches neck to sniff. "
        "Wind moves grass and fallen leaves."
    ),
    "beat_04_shot_a": (
        "Tripod shot, low angle locked to ground. "
        "Dog explodes into full sprint down the path toward camera. "
        "Each stride kicks leaves. Dog grows rapidly larger in frame. "
        "Background trees blur with shallow depth of field."
    ),
    "beat_04_shot_b": (
        "Camera slowly arcs right in a quarter orbit. "
        "Dog's paws land on man's chest. Arms wrap around dog. "
        "Both settle lower. Embrace tightens. Hold."
    ),
}

# Kling tier per shot — pro for character-heavy, standard for wide/action
KLING_TIERS = {
    "beat_01_shot_a": "v3_standard",
    "beat_01_shot_b": "v3_pro",       # identity gate
    "beat_02_shot_a": "v3_pro",       # character close work
    "beat_02_shot_b": "v3_pro",       # close-up face
    "beat_03_shot_a": "v3_pro",       # 10s tracking shot, centerpiece
    "beat_04_shot_a": "v3_standard",  # action wide
    "beat_04_shot_b": "v3_pro",       # reunion close
}

# Shot durations — 10s for centerpiece and payoff shots
SHOT_DURATIONS = {
    "beat_01_shot_a": 5,
    "beat_01_shot_b": 5,
    "beat_02_shot_a": 5,
    "beat_02_shot_b": 5,
    "beat_03_shot_a": 10,   # tracking shot cobblestone→grass
    "beat_04_shot_a": 10,   # full sprint build
    "beat_04_shot_b": 10,   # reunion payoff
}

# Transition plan — which shots use end_image for in-camera transitions
# end_image = path to the DESTINATION anchor (next shot's selected.png)
# Only use for dissolves / motivated motion transitions, NOT hard cuts
END_IMAGE_SHOTS = {
    "beat_01_shot_b": True,    # dissolve into 2A (time passing)
    "beat_03_shot_a": "end",   # special: uses 3A end-image anchor (Maya on grass)
}

# Negative prompt for all Kling generations
KLING_NEGATIVE = (
    "blur, distortion, extra limbs, extra legs, face warping, morphing, "
    "texture swimming, jitter, flicker, deformation, watermark, text, "
    "low quality, extra dogs, extra animals"
)


# =========================================================================
# STEP 1: GENERATE ANCHORS via Gemini 3.1 Flash
# =========================================================================
def run_anchors():
    print("=" * 60)
    print("ANCHORS — Gemini 3.1 Flash edit mode")
    print("=" * 60)

    plan = load_plan()
    pkg_data = load_packages()
    pkg_index = {p["package_id"]: p for p in pkg_data["packages"]}
    all_shots = get_all_shots(plan)

    failed = []

    for i, shot in enumerate(all_shots):
        shot_id = shot["shot_id"]
        prompt = LEAN_PROMPTS.get(shot_id, "")
        if not prompt:
            print(f"  SKIP {shot_id}: no lean prompt defined")
            continue

        shot_dir = os.path.join(ANCHOR_DIR, shot_id)
        selected = os.path.join(shot_dir, "selected.png")

        # Skip shots that already have a selected anchor (re-run only failures)
        if os.path.isfile(selected):
            print(f"  SKIP {shot_id}: already has selected.png (delete to regenerate)")
            continue

        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(all_shots)}] {shot_id} — {shot.get('shot_title', '')}")
        print(f"{'='*60}")
        print(f"  Prompt ({len(prompt)} chars): {prompt[:120]}...")

        # Build reference image list — images carry the visual identity
        ref_paths = []

        # Character sheet(s)
        for char_pkg_id in shot["_characters"]:
            pkg = pkg_index.get(char_pkg_id, {})
            hero = pkg.get("hero_image_path", "")
            if hero and os.path.isfile(hero):
                ref_paths.append(hero)
                print(f"  REF: {pkg.get('name', '?')} character sheet")

        # Environment sheet
        env_pkg_id = shot["_location_pkg"]
        env_pkg = pkg_index.get(env_pkg_id, {})
        env_hero = env_pkg.get("hero_image_path", "")
        if env_hero and os.path.isfile(env_hero):
            ref_paths.append(env_hero)
            print(f"  REF: {env_pkg.get('name', '?')} environment sheet")

        print(f"  References: {len(ref_paths)} images")
        print(f"  Generating anchor...")

        os.makedirs(shot_dir, exist_ok=True)

        try:
            paths = gemini_edit_image(
                prompt=prompt,
                reference_image_paths=ref_paths,
                resolution="1K",
                num_images=1,
            )

            if not paths:
                print(f"  FAILED: No images returned")
                failed.append(shot_id)
                continue

            dest = os.path.join(shot_dir, "selected.png")
            shutil.copy2(paths[0], dest)
            print(f"  Anchor saved: {os.path.abspath(dest)}")

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed.append(shot_id)

        time.sleep(1)

    # Generate end_image for 3A (Maya on grass — destination of tracking shot)
    end_3a_path = os.path.join(ANCHOR_DIR, "beat_03_shot_a", "end_image.png")
    if not os.path.isfile(end_3a_path):
        print(f"\n{'='*60}")
        print(f"Generating 3A end_image (Maya on grass)")
        print(f"{'='*60}")

        # Use all character refs (dog + Maya + environment)
        end_refs = []
        for pkg in pkg_data["packages"]:
            hero = pkg.get("hero_image_path", "")
            if hero and os.path.isfile(hero):
                end_refs.append(hero)
                print(f"  REF: {pkg.get('name', '?')}")

        try:
            paths = gemini_edit_image(
                prompt=LEAN_PROMPT_3A_END,
                reference_image_paths=end_refs,
                resolution="1K",
                num_images=1,
            )
            if paths:
                shutil.copy2(paths[0], end_3a_path)
                print(f"  End image saved: {os.path.abspath(end_3a_path)}")
            else:
                print(f"  FAILED: No end image returned")
                failed.append("beat_03_shot_a_end")
        except Exception as e:
            print(f"  ERROR generating 3A end image: {e}")
            failed.append("beat_03_shot_a_end")
    else:
        print(f"  SKIP 3A end_image: already exists")

    print(f"\n{'='*60}")
    print(f"ANCHORS DONE: {len(all_shots) - len(failed)}/{len(all_shots)}")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"Anchors in {ANCHOR_DIR}/ — delete selected.png to regenerate a shot")
    print(f"Review: python scripts/pipeline_v6_gemini_kling.py review")
    print(f"{'='*60}")


# =========================================================================
# STEP 1.5: SONNET SELECT — pick best candidate per shot
# =========================================================================
SELECT_REVIEW_PATH = "output/pipeline/learning/sonnet_select_v6.json"

def run_select():
    print("=" * 60)
    print("SELECT — Sonnet picks best candidate per shot")
    print("=" * 60)

    plan = load_plan()
    all_shots = get_all_shots(plan)
    char_sheet = "output/preproduction/pkg_char_c852b9c5/sheet.png"

    SYSTEM = """You are a senior cinematographer selecting the best anchor frame from 3 candidates for each shot in a short film.

You will see:
- Image 1: Character reference sheet (ground truth for identity)
- Images 2-4: Candidate A, B, C for this shot

EVALUATE each candidate on:
1. **Identity match** (0-1): Does the dog match the reference sheet? Coat color, collar, tag, proportions, ear shape (pendant, not pricked)
2. **Prompt compliance** (0-1): Does the composition match the shot description? Lens, angle, framing, subject placement
3. **Technical quality** (0-1): Sharpness, lighting, grain, no artifacts, no extra limbs/fingers
4. **Emotional read** (0-1): Does the frame sell the emotion of this beat?
5. **Continuity fitness** (0-1): Will this frame cut well with adjacent shots in an edit?

RULES:
- Reference images carry identity — text descriptions don't override what the sheet shows
- Anatomical accuracy matters: golden retrievers have pendant ears that hang, never prick upright
- Score each candidate honestly, pick the best one even if margins are slim
- If all 3 are bad, say so — don't force a pick

Respond JSON only:
{
  "candidates": {
    "A": {"identity": N, "prompt_compliance": N, "technical": N, "emotion": N, "continuity": N, "overall": N, "notes": "..."},
    "B": {"identity": N, "prompt_compliance": N, "technical": N, "emotion": N, "continuity": N, "overall": N, "notes": "..."},
    "C": {"identity": N, "prompt_compliance": N, "technical": N, "emotion": N, "continuity": N, "overall": N, "notes": "..."}
  },
  "pick": "A|B|C",
  "pick_reason": "1-2 sentences why this candidate wins",
  "confidence": N,
  "all_acceptable": true|false,
  "regenerate_recommendation": "none|suggest|required"
}"""

    results = []

    for i, shot in enumerate(all_shots):
        shot_id = shot["shot_id"]
        shot_dir = os.path.join(ANCHOR_DIR, shot_id)

        # Find all candidates
        candidates = []
        for label in ["candidate_0.png", "candidate_1.png", "candidate_2.png"]:
            path = os.path.join(shot_dir, label)
            if os.path.isfile(path):
                candidates.append(path)

        if len(candidates) < 2:
            print(f"  SKIP {shot_id}: only {len(candidates)} candidate(s), nothing to compare")
            continue

        prompt_text = LEAN_PROMPTS.get(shot_id, "")
        beat_emotion = shot.get("_emotion", "")
        beat_energy = shot.get("_energy", "")

        user_prompt = f"""Shot {shot_id}: {shot.get('shot_title', '')}
Beat emotion: {beat_emotion} | Energy: {beat_energy}
Framing: {shot.get('framing', '')} | Lens: {shot.get('lens', '')}
Prompt: {prompt_text[:300]}

Image 1 = character reference sheet
Images 2-4 = Candidate A, B, C
Pick the best. JSON only."""

        images = [char_sheet] + candidates
        print(f"  [{i+1}/{len(all_shots)}] {shot_id} ({len(candidates)} candidates)...", end=" ", flush=True)

        result = call_vision_json(user_prompt, images, system=SYSTEM, model=OPUS_MODEL, max_tokens=2000)
        result["shot_id"] = shot_id
        result["num_candidates"] = len(candidates)

        pick = result.get("pick", "A")
        pick_idx = {"A": 0, "B": 1, "C": 2}.get(pick, 0)
        conf = result.get("confidence", 0)
        regen = result.get("regenerate_recommendation", "none")

        # Show scores
        cands = result.get("candidates", {})
        scores_str = " | ".join(
            f"{k}={v.get('overall', 0):.2f}" for k, v in sorted(cands.items())
        )
        print(f"pick={pick} conf={conf:.2f} regen={regen} [{scores_str}]")
        print(f"    Reason: {result.get('pick_reason', '')[:120]}")

        # Copy winner to selected.png
        if pick_idx < len(candidates):
            winner_path = candidates[pick_idx]
            selected_path = os.path.join(shot_dir, "selected.png")
            shutil.copy2(winner_path, selected_path)
            print(f"    -> selected.png = candidate_{pick_idx}")

        results.append(result)
        time.sleep(0.5)

    # Save results
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "sonnet_v6_candidate_selection",
        "model": OPUS_MODEL,
        "selections": results,
        "summary": {
            "total_shots": len(results),
            "avg_confidence": sum(r.get("confidence", 0) for r in results) / max(len(results), 1),
            "regenerate_needed": sum(1 for r in results if r.get("regenerate_recommendation") == "required"),
        }
    }
    os.makedirs(os.path.dirname(SELECT_REVIEW_PATH), exist_ok=True)
    with open(SELECT_REVIEW_PATH, "w") as f:
        json.dump(output, f, indent=2)

    s = output["summary"]
    print(f"\n{'='*60}")
    print(f"SELECTION DONE")
    print(f"  Avg confidence: {s['avg_confidence']:.2f}")
    print(f"  Regen needed:   {s['regenerate_needed']}")
    print(f"  Saved: {SELECT_REVIEW_PATH}")
    print(f"{'='*60}")


# =========================================================================
# STEP 2: SONNET REVIEW anchor transitions
# =========================================================================
def run_review():
    print("=" * 60)
    print("REVIEW — Sonnet vision review of anchor transitions")
    print("=" * 60)

    plan = load_plan()
    all_shots = get_all_shots(plan)
    char_sheet = "output/preproduction/pkg_char_c852b9c5/sheet.png"

    SYSTEM = """You are a senior VFX continuity supervisor reviewing anchor frame pairs.
TARGET: confidence >= 0.90, risk_score <= 0.10. Score against this bar.

Image 1 = character reference sheet, Image 2 = FROM shot, Image 3 = TO shot.

Score 0.0-1.0:
- identity_continuity: Same dog? Coat, collar, tag, proportions
- pose_continuity: Pose transition plausible for this cut type?
- camera_continuity: Camera angle/lens compatible for this cut type?
- scene_continuity: Same world? Lighting, environment, time of day
- motion_plausibility: Could this cut work editorially?

RULES:
- Hard cuts EXPECT camera changes — don't penalize normal editorial jumps
- Smash cuts deliberately break continuity — score editorial intent
- Only score generation quality issues, not plan-level concerns
- These are stills — don't penalize lack of motion

JSON only:
{"scores": {"identity_continuity": N, "pose_continuity": N, "camera_continuity": N, "scene_continuity": N, "motion_plausibility": N, "overall_score": N}, "risk_level": "low|medium|high", "main_failure_reasons": ["..."], "plain_english_summary": "2-3 sentences", "prompt_adjustments": ["..."], "confidence": N, "target_gap": "what blocks 0.90/0.10"}"""

    pairs = [(all_shots[i], all_shots[i+1]) for i in range(len(all_shots)-1)]

    # Find transition types
    conform = plan.get("conform", {}).get("transitions_sequence", [])
    trans_map = {(t["from"], t["to"]): t["type"] for t in conform}

    results = []
    for i, (from_shot, to_shot) in enumerate(pairs):
        fid, tid = from_shot["shot_id"], to_shot["shot_id"]
        trans_type = trans_map.get((fid, tid), "hard_cut")

        from_anchor = os.path.join(ANCHOR_DIR, fid, "selected.png")
        to_anchor = os.path.join(ANCHOR_DIR, tid, "selected.png")

        if not os.path.isfile(from_anchor) or not os.path.isfile(to_anchor):
            print(f"  SKIP {fid}->{tid}: missing selected.png")
            continue

        prompt = f"""Review cut {i+1}: {fid} -> {tid} ({trans_type})
FROM: {from_shot.get('shot_title','')} — {from_shot.get('framing','')} {from_shot.get('lens','')}
TO: {to_shot.get('shot_title','')} — {to_shot.get('framing','')} {to_shot.get('lens','')}
Image 1=ref sheet, Image 2=FROM, Image 3=TO. Target 0.90/0.10. JSON only."""

        images = [char_sheet, from_anchor, to_anchor]
        print(f"  [{i+1}/{len(pairs)}] {fid} -> {tid} ({trans_type})...", end=" ", flush=True)

        result = call_vision_json(prompt, images, system=SYSTEM, model=OPUS_MODEL, max_tokens=2000)
        result["from_shot"] = fid
        result["to_shot"] = tid
        result["cut_number"] = i + 1
        result["transition_type"] = trans_type
        results.append(result)

        scores = result.get("scores", {})
        overall = scores.get("overall_score", 0)
        conf = result.get("confidence", 0)
        risk = result.get("risk_level", "?")
        print(f"overall={overall:.2f} conf={conf:.2f} risk={risk}")

        failures = result.get("main_failure_reasons", [])
        for f in failures[:2]:
            print(f"    - {f[:120]}")
        time.sleep(0.5)

    # Save
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "sonnet_v6_anchor_review",
        "model": OPUS_MODEL,
        "vision_results": results,
        "summary": {
            "total_cuts": len(results),
            "avg_overall": sum(r.get("scores", {}).get("overall_score", 0) for r in results) / max(len(results), 1),
            "avg_confidence": sum(r.get("confidence", 0) for r in results) / max(len(results), 1),
            "high_risk": sum(1 for r in results if r.get("risk_level") == "high"),
        }
    }
    with open(REVIEW_PATH, "w") as f:
        json.dump(output, f, indent=2)

    s = output["summary"]
    print(f"\n{'='*60}")
    print(f"  Avg overall:    {s['avg_overall']:.2f}")
    print(f"  Avg confidence: {s['avg_confidence']:.2f}")
    print(f"  High risk:      {s['high_risk']}")
    print(f"  Saved: {REVIEW_PATH}")
    print(f"{'='*60}")


# =========================================================================
# STEP 3: GENERATE VIDEO CLIPS via Kling 3.0
# =========================================================================
def run_clips():
    print("=" * 60)
    print("CLIPS — Kling 3.0 image-to-video")
    print("=" * 60)

    plan = load_plan()
    pkg_data = load_packages()
    pkg_index = {p["package_id"]: p for p in pkg_data["packages"]}
    all_shots = get_all_shots(plan)

    # Build character frontal ref for elements system
    # Use the character sheet as both frontal and reference
    dog_sheet = pkg_index["pkg_char_c852b9c5"]["hero_image_path"]

    failed = []

    for i, shot in enumerate(all_shots):
        shot_id = shot["shot_id"]
        motion_prompt = VIDEO_PROMPTS.get(shot_id, "")
        if not motion_prompt:
            print(f"  SKIP {shot_id}: no video prompt")
            continue

        anchor = os.path.join(ANCHOR_DIR, shot_id, "selected.png")
        if not os.path.isfile(anchor):
            print(f"  SKIP {shot_id}: no selected anchor")
            failed.append(shot_id)
            continue

        tier = KLING_TIERS.get(shot_id, "v3_standard")
        duration = SHOT_DURATIONS.get(shot_id, 5)

        print(f"\n[{i+1}/{len(all_shots)}] {shot_id} — {shot.get('shot_title', '')} ({tier}, {duration}s)")
        print(f"  Motion: {motion_prompt[:120]}...")

        # Build elements for character consistency
        elements = []
        if "pkg_char_c852b9c5" in shot["_characters"]:
            elements.append({
                "frontal_image_path": dog_sheet,
                "reference_image_paths": [dog_sheet],
            })
            print(f"  Element: Buddy character sheet")

        # End image — only for specific transition shots, not all
        end_anchor = None
        end_cfg = END_IMAGE_SHOTS.get(shot_id)
        if end_cfg == "end":
            # Special end-image anchor (e.g., 3A uses Maya-on-grass end frame)
            end_path = os.path.join(ANCHOR_DIR, shot_id, "end_image.png")
            if os.path.isfile(end_path):
                end_anchor = end_path
                print(f"  End frame: {shot_id}/end_image.png (custom)")
        elif end_cfg and i < len(all_shots) - 1:
            # Use next shot's anchor as end frame (dissolve)
            next_id = all_shots[i + 1]["shot_id"]
            next_anchor = os.path.join(ANCHOR_DIR, next_id, "selected.png")
            if os.path.isfile(next_anchor):
                end_anchor = next_anchor
                print(f"  End frame: {next_id} (dissolve transition)")
        else:
            print(f"  End frame: none (hard cut)")

        try:
            clip_path = kling_image_to_video(
                start_image_path=anchor,
                prompt=motion_prompt,
                duration=duration,
                tier=tier,
                end_image_path=end_anchor,
                elements=elements if elements else None,
                negative_prompt=KLING_NEGATIVE,
                cfg_scale=0.6,
                generate_audio=True,
            )

            if not clip_path or not os.path.isfile(clip_path):
                print(f"  FAILED: No video returned")
                failed.append(shot_id)
                continue

            dest = os.path.join(CLIPS_DIR, f"{shot_id}.mp4")
            shutil.copy2(clip_path, dest)
            print(f"  SUCCESS: {os.path.abspath(dest)}")

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed.append(shot_id)

        time.sleep(1)

    print(f"\n{'='*60}")
    print(f"CLIPS DONE: {len(all_shots) - len(failed)}/{len(all_shots)}")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*60}")


# =========================================================================
# STEP 4: CONFORM STITCH
# =========================================================================
def run_conform():
    print("=" * 60)
    print("CONFORM — Final stitch")
    print("=" * 60)

    plan = load_plan()
    all_shots = get_all_shots(plan)
    conform = plan.get("conform", {})

    # Build concat file for ffmpeg
    clip_list = []
    for shot in all_shots:
        clip = os.path.join(CLIPS_DIR, f"{shot['shot_id']}.mp4")
        if os.path.isfile(clip):
            clip_list.append(clip)
            print(f"  {shot['shot_id']}: {clip}")
        else:
            print(f"  {shot['shot_id']}: MISSING")

    if not clip_list:
        print("  No clips found!")
        return

    # Write concat list
    concat_file = os.path.join(FINAL_DIR, "concat_v6.txt")
    with open(concat_file, "w") as f:
        for clip in clip_list:
            abs_path = os.path.abspath(clip).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")

    output_path = os.path.join(FINAL_DIR, "buddy_v6_gemini_kling.mp4")

    # ffmpeg concat
    import subprocess
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        output_path
    ]
    print(f"\n  ffmpeg concat -> {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        size_mb = os.path.getsize(output_path) / (1024*1024)
        print(f"  SUCCESS: {os.path.abspath(output_path)} ({size_mb:.1f}MB)")
    else:
        print(f"  FFMPEG ERROR: {result.stderr[:500]}")

    # Add fade to black at the end
    if os.path.isfile(output_path):
        fade_dur = conform.get("final_transition", {}).get("duration", 2.0)
        faded_path = output_path.replace(".mp4", "_faded.mp4")

        # Get duration for fade calculation
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            output_path
        ]
        probe = subprocess.run(probe_cmd, capture_output=True, text=True)
        if probe.returncode == 0:
            total_dur = float(probe.stdout.strip())
            fade_start = total_dur - fade_dur

            fade_cmd = [
                "ffmpeg", "-y",
                "-i", output_path,
                "-vf", f"fade=t=out:st={fade_start}:d={fade_dur}",
                "-c:a", "copy",
                faded_path
            ]
            print(f"  Adding {fade_dur}s fade to black...")
            fade_result = subprocess.run(fade_cmd, capture_output=True, text=True)
            if fade_result.returncode == 0:
                shutil.move(faded_path, output_path)
                print(f"  Fade applied.")

    print(f"\n{'='*60}")
    print(f"FINAL: {os.path.abspath(output_path)}")
    print(f"{'='*60}")


# =========================================================================
# Main dispatcher
# =========================================================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/pipeline_v6_gemini_kling.py [anchors|select|review|clips|conform|all]")
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "anchors":
        run_anchors()
    elif cmd == "select":
        run_select()
    elif cmd == "review":
        run_review()
    elif cmd == "clips":
        run_clips()
    elif cmd == "conform":
        run_conform()
    elif cmd == "all":
        run_anchors()
        run_select()
        run_review()
        run_clips()
        run_conform()
    else:
        print(f"Unknown command: {cmd}")
        print("Options: anchors, select, review, clips, conform, all")
