"""One-shot renderer for TB Lifestream Static shots 1.1 + 1.2.

Step 1: Build anchor via Gemini 3.1 Flash edit mode (TB sheet + Rooftop env)
Step 2: Animate via Kling V3 Pro I2V
"""
from __future__ import annotations
import os
import sys
import time
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from lib.fal_client import gemini_edit_image, kling_image_to_video  # noqa: E402

# Assets
TB_SHEET = os.path.join(ROOT, "output", "projects", "default", "prompt_os",
                        "previews", "characters",
                        "6d31f281-4cc_full_1776356463.png")
TB_FACE  = os.path.join(ROOT, "output", "projects", "default", "prompt_os",
                        "previews", "characters",
                        "6d31f281-4cc_face_closeup_1776355100.png")
ENV_ROOFTOP = os.path.join(ROOT, "output", "projects", "default", "prompt_os",
                           "env_previews",
                           "436ff890-2df_full_1776362595.png")

ANCHORS_DIR = os.path.join(ROOT, "output", "pipeline", "anchors_v6")
CLIPS_DIR   = os.path.join(ROOT, "output", "pipeline", "clips_v6")

SHOTS = [
    {
        "id": "58a4cf26-a6e",
        "name": "1.1_intro_establishing",
        "duration": 6,
        "anchor_prompt": (
            "Wide establishing cinematic shot. Small chibi-proportioned anime bear "
            "(from the character reference) stands back-to-camera at the rooftop "
            "edge, hood down, silhouetted against the violet dusk neon skyline "
            "(from the environment reference). The crescent moon emblem glints "
            "faintly on the back of the bear's head. Light rain streaks across the "
            "frame, volumetric fog softens the far city, Makoto Shinkai anime "
            "realism, soft bloom, painterly clouds, atmospheric perspective."
        ),
        "kling_prompt": (
            "Slow dolly push-in toward the rooftop silhouette. "
            "Rain drifts diagonally across the frame. "
            "Distant neon skyline shimmers and flickers gently. "
            "Soft volumetric fog drifts. Cinematic, anime realism."
        ),
    },
    {
        "id": "8b2684fe-1c8",
        "name": "1.2_intro_over_shoulder",
        "duration": 9,
        "anchor_prompt": (
            "Cinematic medium over-shoulder composition. The small anime bear's "
            "shoulder and rounded ear (from the character reference) are in the "
            "right foreground, slightly out of focus, as the vast neon violet "
            "skyline (from the environment reference) reveals past his shoulder. "
            "Crescent moon emblem just visible on the back of his head. Rain "
            "intensifies, cold neon bokeh drifts, distant towers glow soft pink "
            "and teal under a deep violet sky. Makoto Shinkai anime realism, "
            "cinematic, shallow depth of field, soft bloom."
        ),
        "kling_prompt": (
            "Slight left orbit, camera arcs sideways around the shoulder. "
            "Rain intensifies, neon bokeh drifts through the background. "
            "Distant signs flicker softly. Atmospheric, cinematic, anime realism."
        ),
    },
]


def _banner(msg):
    print("\n" + "=" * 70)
    print("  " + msg)
    print("=" * 70)


def _render_shot(shot):
    shot_id = shot["id"]
    name = shot["name"]
    anchor_dir = os.path.join(ANCHORS_DIR, shot_id)
    clip_dir   = os.path.join(CLIPS_DIR, shot_id)
    os.makedirs(anchor_dir, exist_ok=True)
    os.makedirs(clip_dir, exist_ok=True)

    _banner(f"SHOT {name}  (id={shot_id}, {shot['duration']}s)")

    # Step 1 — anchor
    print(f"[1/2] Gemini edit — building anchor  (2K, 16:9)")
    t0 = time.time()
    tmp_paths = gemini_edit_image(
        prompt=shot["anchor_prompt"],
        reference_image_paths=[ENV_ROOFTOP, TB_SHEET, TB_FACE],
        resolution="2K",
        num_images=1,
        aspect_ratio="16:9",
    )
    if not tmp_paths:
        print("  !! anchor generation failed — no paths returned")
        return None
    anchor_dst = os.path.join(anchor_dir, "selected.png")
    shutil.copy2(tmp_paths[0], anchor_dst)
    print(f"  OK anchor  ({time.time()-t0:.1f}s)  ->  {anchor_dst}")

    # Step 2 — Kling V3 Pro
    print(f"[2/2] Kling V3 Pro I2V — animating anchor")
    t0 = time.time()
    clip_path = kling_image_to_video(
        start_image_path=anchor_dst,
        prompt=shot["kling_prompt"],
        duration=shot["duration"],
        tier="v3_pro",
        aspect_ratio="16:9",
        cfg_scale=0.5,
        negative_prompt="blur, distortion, low quality, watermark, text, extra limbs, deformed",
    )
    if not clip_path or not os.path.isfile(clip_path):
        print("  !! clip generation failed")
        return None
    clip_dst = os.path.join(clip_dir, "selected.mp4")
    shutil.copy2(clip_path, clip_dst)
    print(f"  OK clip    ({time.time()-t0:.1f}s)  ->  {clip_dst}")
    return {"anchor": anchor_dst, "clip": clip_dst}


def main():
    # Sanity
    for p in (TB_SHEET, TB_FACE, ENV_ROOFTOP):
        if not os.path.isfile(p):
            print(f"MISSING reference: {p}")
            sys.exit(1)

    results = []
    for shot in SHOTS:
        r = _render_shot(shot)
        results.append({"shot": shot["name"], "result": r})

    _banner("DONE")
    for r in results:
        print(f"  {r['shot']}:  {'OK' if r['result'] else 'FAILED'}")
        if r["result"]:
            print(f"    anchor -> {r['result']['anchor']}")
            print(f"    clip   -> {r['result']['clip']}")


if __name__ == "__main__":
    main()
