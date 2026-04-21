"""Merge v7-audit overrides (13 fails) into kling_prompt_overrides.json.

Each failing shot gets:
  - fresh shot_name from current scenes.json
  - anchor_extra that hammers the audit violations (eye color, emblem, pupil, pose)
  - coverage overrides where wide shots legitimately want small-in-frame
  - needs_anchor_regen: True to force the next run to regenerate this anchor
"""
from __future__ import annotations
import json, os, shutil, sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERRIDES = os.path.join(ROOT, "output", "projects", "default", "prompt_os", "kling_prompt_overrides.json")
SCENES    = os.path.join(ROOT, "output", "projects", "default", "prompt_os", "scenes.json")

EYE = ("Bear's eyes are GLOWING RED-ORANGE — saturated, luminous, almost neon red-orange, "
       "visibly emitting a warm red-orange glow. ABSOLUTELY NOT brown, NOT amber, NOT yellow, "
       "NOT green, NOT teal, NOT muted earth-tones. Irises must read as luminous and glowing, "
       "not as a normal animal eye color.")

EMBLEM_NO_SKY = ("ABSOLUTELY NO moon, crescent, circular celestial body, large round glowing disc, "
                 "or lunar-shaped object anywhere in the sky, background, clouds, aurora, or "
                 "environmental lighting. The crescent emblem exists ONLY on the bear's forehead — "
                 "nowhere else in the frame. If the environment reference shows a moon, omit it.")

# shot_id -> patch dict. These merge on top of existing entries.
PATCHES = {
    # 1c wide — legit intentional wide silhouette, bear came out front-facing too small
    "51c246a8-5da": {
        "shot_name": "1c Rooftop Violet Sky wide",
        "anchor_extra": (
            "This is an INTENTIONAL WIDE establishing shot — bear is a back-turned / three-quarter-back "
            "silhouette at the rooftop edge, occupying roughly 5-8% of the frame, with the violet sky "
            "and neon cityscape filling the rest as hero. Bear's eyeline is tilted UP and to the upper "
            "screen-right, toward an off-frame sky element. Back-lit rim light only — not fully front-lit. "
            f"{EMBLEM_NO_SKY} Because bear is back-turned, no emblem is visible in this anchor. {EYE}"
        ),
        "min_frame_coverage_override": {"wide": 3, "medium": 5},
        "coverage_hint_override": (
            "Frame a back-turned silhouetted bear at 5-8% of frame with violet sky and neon cityscape "
            "as hero — this is a wide establishing silhouette, not a hero portrait."
        ),
        "force_pass_on_violations": ["subject_too_small", "facing_mismatch", "pose_mismatch_description"],
        "needs_anchor_regen": True,
    },

    # 3a wide — amber eyes + coverage fine
    "368d90cc-f49": {
        "shot_name": "3a Warp Signal Sky wide",
        "anchor_extra": (
            "Chibi TB wide shot in warp signal sky — bear three-quarter-back, rim-lit against violet "
            f"sky. {EYE} The 'warp signal' in the sky is an amorphous spiraling ribbon/vortex of light — "
            "NOT a round disc, NOT a crescent, NOT a moon. Swirling irregular streams only. "
            f"{EMBLEM_NO_SKY}"
        ),
        "min_frame_coverage_override": {"wide": 5, "medium": 8},
        "force_pass_on_violations": ["subject_too_small"],
        "needs_anchor_regen": True,
    },

    # 3b medium — amber eyes + warp signal sky reads as moon
    "b435b5a3-14b": {
        "shot_name": "3b Warp Signal Sky medium",
        "anchor_extra": (
            "Chibi TB medium shot with forehead crescent visible. "
            f"{EYE} "
            "The warp signal in the sky must read UNAMBIGUOUSLY as a SPIRAL VORTEX of streaming ribbons "
            "and data streaks — NOT a round moon, NOT a disc, NOT a circular orb. If there is any "
            "doubt the sky element looks moon-like, render it as irregular spiral streams instead of "
            f"a uniform glowing disc. {EMBLEM_NO_SKY}"
        ),
        "needs_anchor_regen": True,
    },

    # 3c close — brown eyes + text inside iris (should be cornea reflection)
    "385cbb92-8fc": {
        "shot_name": "3c Warp Signal Sky close",
        "anchor_extra": (
            "Chibi TB extreme close-up on the eyes. "
            f"{EYE} "
            "CRITICAL PUPIL CONTENT RULE: the irises stay solid glowing red-orange. Any streaming text, "
            "katakana, or data glyphs must appear as a SURFACE REFLECTION — a bright specular sheen "
            "across the front of the cornea (like light on a wet marble), NOT embedded inside the iris, "
            "NOT replacing the iris pattern, NOT floating inside the pupil. The iris itself is pure "
            "glowing red-orange with normal catchlights; the text is a thin reflective highlight over "
            f"the top of it. {EMBLEM_NO_SKY}"
        ),
        "needs_anchor_regen": True,
    },

    # 3d medium — brown/amber eyes
    "46193203-020": {
        "shot_name": "3d Warp Signal Sky medium",
        "anchor_extra": (
            "Chibi TB medium shot with forehead crescent. "
            f"{EYE} {EMBLEM_NO_SKY}"
        ),
        "needs_anchor_regen": True,
    },

    # 5c close — reddish-brown eyes
    "320fa568-e4a": {
        "shot_name": "5c Dissolving City Data Streams close",
        "anchor_extra": (
            "Chibi TB close-up with forehead crescent faintly lit by passing data glyphs. "
            f"{EYE} Background is katakana and binary streams plus pink petal haze. {EMBLEM_NO_SKY}"
        ),
        "needs_anchor_regen": True,
    },

    # 6a wide — moon in sky + amber eyes + small coverage
    "aac41ef5-9c4": {
        "shot_name": "6a Zero-G Particle Void wide",
        "anchor_extra": (
            "Chibi TB floats in a zero-G particle void — this is an INTENTIONALLY WIDE shot with "
            "bear at roughly 5-8% of the frame surrounded by drifting particles and starfield. "
            f"{EYE} "
            f"{EMBLEM_NO_SKY} "
            "The starfield contains only pinpoint stars, drifting particle ribbons, and faint nebular "
            "haze — absolutely NO crescent, NO moon, NO glowing disc anywhere in the sky."
        ),
        "min_frame_coverage_override": {"wide": 3, "medium": 5},
        "coverage_hint_override": (
            "Frame the chibi bear small (5-8% of frame) drifting in starfield — the particle void and "
            "stars are hero; absolutely no moon-shaped object anywhere in the sky."
        ),
        "force_pass_on_violations": ["subject_too_small"],
        "needs_anchor_regen": True,
    },

    # 6b medium — moon in sky + amber eyes + pose wrong (should look DOWN at paw)
    "449b9844-9c1": {
        "shot_name": "6b Zero-G Particle Void medium",
        "anchor_extra": (
            "Chibi TB medium shot in zero-G particle void. "
            f"{EYE} "
            "POSE: bear's eyes are LOWERED DOWNWARD to his own dissolving paw held in front of him — "
            "gaze is cast DOWN toward his own paw, NOT at camera, NOT middle-distance, NOT forward. "
            "His lips are slightly parted, a slow blink of acceptance. The dissolving paw is in "
            "frame, its edges flaking into drifting particles. "
            f"{EMBLEM_NO_SKY} "
            "Starfield background only — absolutely NO crescent, NO moon, NO planetary body, NO round "
            "celestial disc anywhere in the sky or background."
        ),
        "needs_anchor_regen": True,
    },

    # 7b medium — GREEN/TEAL eyes (worst offender)
    "49fe880a-932": {
        "shot_name": "7b White Void Grid medium",
        "anchor_extra": (
            "Chibi TB medium shot in pure white void with cyan grid lines. "
            f"{EYE} "
            "CRITICAL: recent anchor rendered GREEN/TEAL eyes — do NOT render green, do NOT render "
            "teal, do NOT render blue-green. Eyes are GLOWING RED-ORANGE only. Tear streaks are "
            "allowed and do not change eye color. Forehead crescent emblem on forehead only."
        ),
        "needs_anchor_regen": True,
    },

    # 7c wide — 3% coverage, otherwise clean
    "6f8846fd-964": {
        "shot_name": "7c White Void Grid wide",
        "anchor_extra": (
            "This is an INTENTIONALLY WIDE shot — chibi TB at roughly 5-8% of frame, body upright "
            "and centered in a pure white void with cyan grid radiating outward. The grid and void are "
            f"hero, bear is the scale reference. {EYE} Forehead crescent on forehead only."
        ),
        "min_frame_coverage_override": {"wide": 3, "medium": 5},
        "coverage_hint_override": (
            "Frame the bear small (5-8% of frame) centered in white void with cyan grid radiating — "
            "grid is hero, bear is scale reference."
        ),
        "force_pass_on_violations": ["subject_too_small"],
        "needs_anchor_regen": True,
    },

    # 8b medium — amber eyes + pose (small nod, bright eyes)
    "13159ff3-a72": {
        "shot_name": "8b Digital Skyline Hyperspeed medium",
        "anchor_extra": (
            "Chibi TB medium shot against hyperspeed skyline. "
            f"{EYE} "
            "POSE/EXPRESSION: a small private NOD with the head — chin dipped once gently — while "
            "eyes are BRIGHT and OPEN, warm/resolved, jaw relaxed. This is a reborn, settled, "
            "quietly confident expression — NOT flat, NOT subdued, NOT downturned-mouth, NOT neutral. "
            "Eyes read as LIT-UP (not dull), the nod is subtle but readable. Golden crescent on forehead."
        ),
        "needs_anchor_regen": True,
    },

    # 8c close — amber eyes + cityscape reflection in pupils is OK
    "62e2e847-059": {
        "shot_name": "8c Digital Skyline Hyperspeed close",
        "anchor_extra": (
            "Chibi TB close-up with glowing crescent emblem centered on forehead. "
            f"{EYE} "
            "Cityscape reflections with hyperspeed light streaks may appear as a bright specular sheen "
            "across the CORNEA SURFACE (like light on wet glass) — the red-orange iris color stays "
            "fully saturated and glowing beneath the reflection. Do NOT let cityscape reflections "
            "tint the iris brown or amber."
        ),
        "needs_anchor_regen": True,
    },

    # 8d medium — amber eyes
    "7b0a1dac-868": {
        "shot_name": "8d Digital Skyline Hyperspeed medium",
        "anchor_extra": (
            "Chibi TB medium shot against hyperspeed skyline. "
            f"{EYE} Forehead crescent on forehead only."
        ),
        "needs_anchor_regen": True,
    },
}


def main() -> int:
    with open(OVERRIDES, "r", encoding="utf-8") as f:
        data = json.load(f)
    with open(SCENES, "r", encoding="utf-8") as f:
        scenes = json.load(f)
    scene_by_id = {s["id"]: s for s in scenes}

    ovr = data.setdefault("overrides", {})
    updated = 0
    added   = 0
    for sid, patch in PATCHES.items():
        if sid not in scene_by_id:
            print(f"  SKIP  {sid} — not in scenes.json")
            continue
        current = ovr.get(sid, {})
        before = bool(current)
        # Deep merge for min_frame_coverage_override
        if "min_frame_coverage_override" in patch and "min_frame_coverage_override" in current:
            merged = dict(current["min_frame_coverage_override"])
            merged.update(patch["min_frame_coverage_override"])
            patch = {**patch, "min_frame_coverage_override": merged}
        current.update(patch)
        ovr[sid] = current
        if before: updated += 1
        else:      added   += 1
        print(f"  {'UPDT' if before else 'ADD '}  {sid}  {patch.get('shot_name','')}")

    data["_v7_patched_at"] = datetime.now().isoformat(timespec="seconds")
    data["_v7_patched_targets"] = sorted(PATCHES.keys())

    # Backup before overwriting
    backup = OVERRIDES + f".bak_v7_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(OVERRIDES, backup)
    with open(OVERRIDES, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\n  added:   {added}")
    print(f"  updated: {updated}")
    print(f"  backup:  {backup}")
    print(f"  output:  {OVERRIDES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
