"""V4 Anchor Regeneration with Sonnet Prompt Review.

Flow:
  1. Read Sonnet's v4 review and production plan
  2. Sonnet rewrites each anchor prompt AND reviews it in one pass
     (incorporates adjustments + flags remaining issues + returns confidence)
  3. Save reviewed prompts to production plan
  4. Optionally regenerate anchors with --generate

Usage:
  python scripts/review_and_regenerate_v4.py              # Rewrite+review only
  python scripts/review_and_regenerate_v4.py --generate   # Rewrite+review+generate
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

from lib.claude_client import call_json, OPUS_MODEL
from lib.video_generator import _runway_generate_scene_image, _ENGINE_RATIO_MAP

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PLAN_PATH = "output/pipeline/production_plan_v4.json"
PKGS_PATH = "output/preproduction/packages.json"
REVIEW_PATH = "output/pipeline/learning/sonnet_review_v4.json"
ANCHOR_DIR = "output/pipeline/anchors_v5"
PROMPT_REVIEW_PATH = "output/pipeline/learning/sonnet_prompt_review_v5b.json"

MODEL = "gemini_2.5_flash"
RATIO = _ENGINE_RATIO_MAP.get(MODEL, "1344:768")

os.makedirs(ANCHOR_DIR, exist_ok=True)
os.makedirs(os.path.dirname(PROMPT_REVIEW_PATH), exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHAR_LOCK = ("Adult golden retriever, honey-gold medium-length coat with chest and leg feathering, "
             "broad rounded skull, wide-set dark brown eyes, defined forehead-to-muzzle stop, "
             "pendant floppy ears, dark wide nose, athletic medium build, "
             "red nylon collar with round silver ID tag.")

REWRITE_AND_REVIEW_SYSTEM = """You are a senior VFX supervisor AND prompt engineer for AI image generation.

You will rewrite an anchor frame prompt incorporating specific adjustments from a previous review,
then immediately review your own rewrite for generation readiness — all in ONE pass.

TARGET: confidence >= 0.90, risk_score <= 0.10. Every decision you make should push toward this bar.
If your rewrite can't reach 0.90/0.10, explain exactly what's blocking it and what would fix it.

REWRITE RULES:
- Keep prompt under 900 characters (hard limit for generation API)
- Preserve the canonical character lock at the end
- Use precise, measurable language (percentages, degrees, distances) not vague terms
- Never use words that imply sound or audio
- Do not re-describe subjects provided via @Tag reference images except for the character lock
- Style: photorealistic 35mm film, Kodak Vision3 250D, golden hour
- Camera/lighting constraints must be explicit and specific
- If adjustments conflict, prioritize spatial/camera continuity over emotional description

CRITICAL — STILL IMAGE RULES (these are ANCHOR STILLS, not video frames):
- NO temporal language: "drifts", "lifts", "rises", "shifts", "begins to", "completing"
- NO motion blur, motion energy, or implied movement over time
- NO frame timing references ("at 2 seconds", "by end of shot")
- Describe a SINGLE FROZEN MOMENT — one pose, one expression, one state
- Falling leaves must be frozen mid-air, not "drifting" or "settling"
- If an adjustment was written for video, translate it to its single-frame equivalent

ANATOMICAL ACCURACY:
- Golden retrievers have pendant (floppy) ears that CANNOT prick upright
- "Alert ear position" = ears shifted slightly forward from resting drape, not erect
- Use breed-accurate body language only

PROMPT BUDGET:
- Primary subject (dog) gets 50%+ of prompt character budget
- Secondary subjects (humans) get essential identifiers only (3-4 key traits), not full wardrobe
- Environment gets 20-25% max — enough for spatial grounding, no excess detail
- Cut any element that competes with the primary subject for model attention

LENS AUTHORITY:
- The SHOT CONTEXT lens field is canonical. If adjustments suggest a different focal length,
  use the shot context lens unless there is an explicit override note.

REVIEW CHECKLIST (run against your own rewrite):
1. CAMERA CONSISTENCY: compatible with adjacent shots?
2. SPATIAL LOGIC: environment matches story position?
3. CHARACTER LOCK: present and not contradicted?
4. MEASURABILITY: precise enough for the model?
5. PROMPT BLOAT: competing instructions that will confuse the model?
6. GENERATION PITFALLS: anything the model will likely misinterpret?
7. CONTINUITY: does lighting direction, time of day, environment match neighbors?
8. STILL-IMAGE COMPLIANCE: zero temporal/motion language remaining?

SCORING RULES:
- Only score issues that affect THIS PROMPT's generation quality
- Do NOT count plan-level issues (shot splits, insert frames, missing companion shots)
  — those are production decisions, not prompt flaws
- Do NOT count video-only concerns (motion timing, temporal arcs) — these are stills
- A clean, unambiguous, measurable prompt with no conflicting instructions = 0.90+ confidence
- Risk is about what the generation MODEL will get wrong, not editorial concerns

Respond with ONLY this JSON:
{
  "rewritten_prompt": "the full rewritten prompt text",
  "confidence": 0.0-1.0,
  "risk_score": 0.0-1.0,
  "issues_found": ["any remaining issues after rewrite, or empty if clean"],
  "changes_made": ["brief list of what you changed from original"],
  "predicted_failure_modes": ["what might still go wrong at generation time"],
  "target_gap": "if below 0.90 conf or above 0.10 risk, explain what's blocking and what would fix it"
}"""

# ---------------------------------------------------------------------------
# Step 1: Load everything
# ---------------------------------------------------------------------------
print("=" * 60)
print("STEP 1: Loading production plan + Sonnet review")
print("=" * 60)

with open(PLAN_PATH) as f:
    plan = json.load(f)
with open(PKGS_PATH) as f:
    pkg_data = json.load(f)
with open(REVIEW_PATH) as f:
    review = json.load(f)

pkg_index = {p["package_id"]: p for p in pkg_data["packages"]}
env_pkg = next(p for p in pkg_data["packages"] if p["package_type"] == "environment")

# Index Sonnet's review by cut_number
review_by_cut = {r["cut_number"]: r for r in review["vision_results"]}

# Flatten all shots with beat context
all_shots = []
for beat in plan["beats"]:
    for shot in beat["shots"]:
        shot["_beat_id"] = beat["beat_id"]
        shot["_beat_title"] = beat["title"]
        shot["_emotion"] = beat["emotion"]
        shot["_energy"] = beat["energy"]
        all_shots.append(shot)

print(f"  {len(all_shots)} shots, {len(review_by_cut)} transition reviews")
print()

# ---------------------------------------------------------------------------
# Step 2: Gather per-shot adjustments from Sonnet's review
# ---------------------------------------------------------------------------
shot_adjustments = {}

for cut_num, rev in review_by_cut.items():
    from_shot = rev["from_shot"]
    to_shot = rev["to_shot"]
    for adj in rev.get("prompt_adjustments", []):
        adj_lower = adj.lower()
        if "start frame" in adj_lower or "start " in adj_lower[:20]:
            shot_adjustments.setdefault(from_shot, []).append(adj)
        if "end frame" in adj_lower or "end " in adj_lower[:16]:
            shot_adjustments.setdefault(to_shot, []).append(adj)
        if "start" not in adj_lower[:20] and "end" not in adj_lower[:16]:
            shot_adjustments.setdefault(to_shot, []).append(adj)

# ---------------------------------------------------------------------------
# Step 3: Sonnet rewrite+review in one pass per shot
# ---------------------------------------------------------------------------
print("=" * 60)
print("STEP 2: Sonnet rewrite + review (single pass per shot)")
print("=" * 60)

results = {}

for idx, shot in enumerate(all_shots):
    shot_id = shot["shot_id"]
    original_prompt = shot["anchor_prompt"]
    adjs = shot_adjustments.get(shot_id, [])

    # Adjacent prompts for continuity context
    prev_prompt = all_shots[idx - 1]["anchor_prompt"][:300] if idx > 0 else "(first shot — cold open)"
    next_prompt = all_shots[idx + 1]["anchor_prompt"][:300] if idx < len(all_shots) - 1 else "(last shot — fade to black)"

    adj_block = chr(10).join(f"- {a}" for a in adjs) if adjs else "(none — original prompt had no flagged issues for this shot)"

    prompt = f"""Rewrite this anchor prompt incorporating the adjustments, then review your rewrite.
TARGET: confidence >= 0.90, risk <= 0.10. Push hard for this bar.

ORIGINAL PROMPT:
{original_prompt}

ADJUSTMENTS FROM PREVIOUS REVIEW:
{adj_block}

SHOT CONTEXT (lens field is CANONICAL — use this focal length):
- ID: {shot_id} | Title: {shot.get('shot_title', '')}
- Purpose: {shot.get('purpose', '')}
- Framing: {shot.get('framing', '')} | Lens: {shot.get('lens', '')} | Camera: {shot.get('camera_height', '')}
- Action (translate to single frozen moment): {shot.get('action', '')}
- Beat: {shot.get('_beat_title', '')} | Emotion: {shot.get('_emotion', '')} | Energy: {shot.get('_energy', '')}

ADJACENT SHOTS (for continuity):
Previous: {prev_prompt}
Next: {next_prompt}

STYLE BIBLE: {plan['style_bible']}
CAMERA RULES: All shots from left of axis. Dog screen direction left-to-right. Backlight from camera-right always.

REMINDERS:
- This is a STILL IMAGE prompt. Zero temporal/motion language.
- Golden retrievers have pendant ears — they do NOT prick upright.
- Secondary humans get 3-4 key identifiers max, not full wardrobe.
- Only flag issues that affect THIS prompt's generation. Not plan-level concerns.

CHARACTER LOCK (must appear at end of rewritten prompt):
{CHAR_LOCK}

Rewrite and review. Output ONLY the JSON."""

    print(f"  [{idx+1}/{len(all_shots)}] {shot_id}...", end=" ", flush=True)
    result = call_json(prompt, system=REWRITE_AND_REVIEW_SYSTEM, model=OPUS_MODEL, max_tokens=2000)

    rewritten = result.get("rewritten_prompt", original_prompt)

    # Ensure character lock
    if CHAR_LOCK not in rewritten:
        rewritten = rewritten.rstrip(". ") + ". " + CHAR_LOCK

    # Enforce length
    if len(rewritten) > 950:
        lock_start = rewritten.rfind(CHAR_LOCK)
        if lock_start > 0:
            excess = len(rewritten) - 900
            pre = rewritten[:lock_start].rstrip(". ")
            rewritten = pre[:len(pre) - excess].rstrip(". ") + ". " + CHAR_LOCK

    result["rewritten_prompt"] = rewritten
    result["shot_id"] = shot_id
    result["original_prompt"] = original_prompt
    result["adjustments_applied"] = len(adjs)
    results[shot_id] = result

    conf = result.get("confidence", 0)
    risk = result.get("risk_score", 0)
    issues = result.get("issues_found", [])
    changes = result.get("changes_made", [])

    print(f"conf={conf:.2f} risk={risk:.2f} changes={len(changes)} issues={len(issues)} ({len(rewritten)}ch)")

    if issues:
        for iss in issues[:2]:
            print(f"    ! {iss[:120]}")

    time.sleep(0.5)

print()

# ---------------------------------------------------------------------------
# Step 4: Save everything
# ---------------------------------------------------------------------------
print("=" * 60)
print("STEP 3: Saving")
print("=" * 60)

# Update production plan
for beat in plan["beats"]:
    for shot in beat["shots"]:
        sid = shot["shot_id"]
        if sid in results:
            shot["anchor_prompt_v4"] = shot["anchor_prompt"]
            shot["anchor_prompt"] = results[sid]["rewritten_prompt"]
            shot["anchor_status"] = "prompt_reviewed"
            shot["prompt_review"] = {
                "confidence": results[sid].get("confidence", 0),
                "risk_score": results[sid].get("risk_score", 0),
                "issues": results[sid].get("issues_found", []),
                "changes": results[sid].get("changes_made", []),
                "failure_modes": results[sid].get("predicted_failure_modes", []),
            }

plan["version"] = "v5_prompt_reviewed"
plan["prompt_review_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")

with open(PLAN_PATH, "w") as f:
    json.dump(plan, f, indent=2)
print(f"  Updated {PLAN_PATH}")

# Save review
review_output = {
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "mode": "sonnet_rewrite_and_review",
    "model": OPUS_MODEL,
    "reviews": list(results.values()),
    "summary": {
        "total": len(results),
        "avg_confidence": sum(r.get("confidence", 0) for r in results.values()) / max(len(results), 1),
        "avg_risk": sum(r.get("risk_score", 0) for r in results.values()) / max(len(results), 1),
        "total_issues": sum(len(r.get("issues_found", [])) for r in results.values()),
        "total_changes": sum(len(r.get("changes_made", [])) for r in results.values()),
    }
}
with open(PROMPT_REVIEW_PATH, "w") as f:
    json.dump(review_output, f, indent=2)
print(f"  Saved review to {PROMPT_REVIEW_PATH}")

# Summary
print()
print("=" * 60)
s = review_output["summary"]
print(f"  Shots:          {s['total']}")
print(f"  Avg confidence: {s['avg_confidence']:.2f}")
print(f"  Avg risk:       {s['avg_risk']:.2f}")
print(f"  Total changes:  {s['total_changes']}")
print(f"  Total issues:   {s['total_issues']}")
print("=" * 60)
print()

# Print each rewritten prompt
for shot in all_shots:
    sid = shot["shot_id"]
    r = results.get(sid, {})
    conf = r.get("confidence", 0)
    risk = r.get("risk_score", 0)
    prompt_text = r.get("rewritten_prompt", "???")
    print(f"--- {sid} (conf={conf:.2f} risk={risk:.2f}) ---")
    print(f"  {prompt_text[:200]}...")
    changes = r.get("changes_made", [])
    if changes:
        print(f"  Changes: {'; '.join(changes[:3])}")
    print()

# ---------------------------------------------------------------------------
# Step 5: Generate (only with --generate flag)
# ---------------------------------------------------------------------------
if "--generate" in sys.argv:
    print("=" * 60)
    print("STEP 4: Generating anchors with reviewed prompts")
    print("=" * 60)

    env_hero = env_pkg["hero_image_path"]
    failed = []

    for i, shot in enumerate(all_shots):
        shot_id = shot["shot_id"]
        print(f"\n[{i+1}/{len(all_shots)}] {shot_id} — {shot.get('shot_title', '')}")

        ref_photos = []
        beat = next(b for b in plan["beats"] if any(s["shot_id"] == shot_id for s in b["shots"]))
        char_pkgs = beat.get("characters", [])

        if char_pkgs:
            primary = pkg_index[char_pkgs[0]]
            ref_photos.append({"path": primary["hero_image_path"], "tag": "Character"})
            print(f"  @Character = {primary['name']}")

        ref_photos.append({"path": env_hero, "tag": "Setting"})
        print(f"  @Setting = {env_pkg['name']}")

        if len(char_pkgs) > 1:
            second = pkg_index[char_pkgs[1]]
            ref_photos.append({"path": second["hero_image_path"], "tag": "PropRef"})
            print(f"  @PropRef = {second['name']}")

        prompt = results[shot_id]["rewritten_prompt"]
        print(f"  Prompt ({len(prompt)} chars): {prompt[:100]}...")

        anchor_path = os.path.join(ANCHOR_DIR, f"{shot_id}.png")
        if os.path.isfile(anchor_path):
            os.remove(anchor_path)

        try:
            tmp_path = _runway_generate_scene_image(
                prompt=prompt,
                reference_photos=ref_photos,
                ratio=RATIO,
                model=MODEL,
            )
            if not tmp_path or not os.path.isfile(tmp_path):
                print(f"  FAILED: No image returned")
                failed.append(shot_id)
                continue

            shutil.copy2(tmp_path, anchor_path)
            abs_path = os.path.abspath(anchor_path)

            for b in plan["beats"]:
                for s in b["shots"]:
                    if s["shot_id"] == shot_id:
                        s["anchor_path"] = abs_path
                        s["anchor_status"] = "generated_v5"

            print(f"  SUCCESS: {abs_path}")

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed.append(shot_id)

        time.sleep(1)

    with open(PLAN_PATH, "w") as f:
        json.dump(plan, f, indent=2)

    print(f"\n{'='*60}")
    print(f"DONE: {len(all_shots) - len(failed)}/{len(all_shots)} anchors")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"Credits: ~{(len(all_shots) - len(failed)) * 5}")
    print(f"{'='*60}")
else:
    print("Prompts reviewed and saved. Run with --generate to generate anchors:")
    print(f"  python scripts/review_and_regenerate_v4.py --generate")
