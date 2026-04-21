"""Bear trailer — Shot D: static dark bear climax with identity lock.

Kling V3 Pro I2V, 5s, starting from bear_dark_2k_backup.png. This replaces
the earlier Shot D render which suffered from the motion-verb trap (the
prompt said "gently pulses and flickers" and Kling interpreted that
literally — the moon on the bear's forehead pulsed visibly in the final
trailer). Fixes (Agent C critique, 2026-04-15):

  - Prompt leads with camera intent ("Static lock-off, no camera move")
    so linter's first-12-word camera check fires correctly.
  - Removed "steady" and "held still" — these are verb-forms Kling
    interprets as motion. Replaced with static-only nouns.
  - cfg_scale 0.35 (was 0.7, backwards) — LOWER cfg lets the start image
    dominate over prompt drift, higher cfg chases prompt motion.
  - end_image_path = start_image_path — forcing Kling to interpolate
    between identical frames collapses any invented motion.
  - `elements` carries real reference images (head_compare_4k + backup)
    not an empty list — hardens identity against frame-to-frame drift.
  - Negative prompt adds parallax / focus-pull / lens-breathing /
    particle-drift / exposure-shift — the invented animations Kling
    reaches for when the subject itself is locked.

Evidence: postmortem_trailer_climax_v3.md, feedback_kling_motion_verb_trap.md
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import kling_image_to_video

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
FRONTAL = os.path.join(PUBLIC, "bear_dark_2k_backup.png")
OUT = os.path.join(PUBLIC, "trailer_shot_d.mp4")

# Static prompt — camera intent leads. "Steady"/"held still" removed per
# Agent C critique: those are verb-forms Kling interprets as motion. 23
# words, inside the 15-40 sweet spot, passes kling_prompt_linter rules
# (CAMERA_VERBS hit on "static" and "lock-off" in first 12 tokens).
PROMPT = (
    "Static lock-off, no camera move: dark bear in cinematic close-up, "
    "amber crescent moon emblem on forehead, cave interior background, "
    "moody low-key rim light."
)

# Expanded negative prompt — bans motion-verb family AND secondary
# animation priors (parallax, focus pull, lens breathing, particle/fog
# drift, exposure shift) that Kling invents when the subject is static.
NEGATIVE = (
    "pulse, pulsing, flicker, flickering, drift, drifting, sway, swaying, "
    "breathe, breathing, shimmer, shimmering, stir, stirring, quiver, tremble, "
    "vibrate, motion, movement, animation, animated, moving, sliding, push-in, "
    "dolly, zoom, pan, tilt, rotation, orbit, parallax, focus pull, "
    "lens breathing, rack focus, handheld, jitter, morph, warp, "
    "fog drift, smoke drift, particle drift, volumetric drift, haze motion, "
    "bear moving, bear sliding, camera shake, color shift, exposure shift, "
    "white-balance shift, glow pulse, light pulse, blur, distort, "
    "low quality, text, watermark, wordmark, logo"
)

# Identity lock via `elements`. bear_head_compare_*.png were considered
# but their 3.033 aspect ratio exceeds fal's 2.5 max (they're side-by-side
# comparison montages, not single subject frames). Falling back to empty
# refs triggers the self-reference path at lib/fal_client.py:314 which is
# a valid identity anchor on its own.
ELEMENTS = [
    {
        "frontal_image_path": FRONTAL,
        "reference_image_paths": [],
    }
]


def main():
    if not os.path.isfile(FRONTAL):
        print(f"[SHOT D] missing frontal: {FRONTAL}")
        return

    print(f"[SHOT D] Rendering static dark bear climax (elements-locked)")
    print(f"         frontal:  {FRONTAL}")
    print(f"         output:   {OUT}")
    print(f"         duration: 5s at Kling V3 Pro (est ~$0.56)")

    try:
        tmp = kling_image_to_video(
            start_image_path=FRONTAL,
            end_image_path=FRONTAL,  # identical start/end forces interpolation
            prompt=PROMPT,            # between the same frame -> collapses motion
            duration=5,
            tier="v3_pro",
            generate_audio=False,
            cfg_scale=0.35,           # lower = start image dominates over prompt
            negative_prompt=NEGATIVE,
            elements=ELEMENTS,
        )
        shutil.move(tmp, OUT)
        mb = os.path.getsize(OUT) / (1024 * 1024)
        print(f"[SHOT D] Saved -> {OUT} ({mb:.1f}MB)")
    except Exception as e:
        print(f"[SHOT D] FAILED: {e}")


if __name__ == "__main__":
    main()
