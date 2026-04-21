"""Regenerate bear_dark.png so its head geometry EXACTLY matches bear_light.png.

Problem: the dark bear has wider cheeks / fuller face than the light bear. They
were generated independently from a shared brief and drifted.

Fix: pass the LIGHT bear as the primary head-shape reference and the current
DARK bear as the environment/lighting reference. Tell Gemini to preserve ref 1's
head geometry, scale, and framing while relighting it into ref 2's cave.

Writes bear_dark_matched.png for review before overwriting.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import gemini_edit_image

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
LIGHT_REF = os.path.join(PUBLIC, "bear_light.png")       # ref 1: the canonical bear
DARK_REF = os.path.join(PUBLIC, "bear_dark_drift.png")   # ref 2: environment/lighting style
OUT = os.path.join(PUBLIC, "bear_dark_matched.png")


PROMPT = (
    "Re-render reference 1 (the bear) with ONE change: relight the scene as a "
    "dark cave night scene. Same bear, same head sculpt, same cheek width, "
    "same eye shape, same ears, same crescent-moon glyph position, same pose, "
    "SAME scale in frame, SAME camera distance, SAME framing, SAME centering, "
    "SAME bear size. The bear in the output must be geometrically identical "
    "to reference 1 — the same 3D model, just lit differently. "
    "Changes to apply: "
    "(a) Darken the bear's fur from near-white to dark grey / near-black, "
    "    matching the fur tone in reference 2. "
    "(b) Replace the pale cream background with a deep cave rock environment "
    "    matching reference 2 — dark rocky walls, low-key moody lighting, "
    "    dark reflective floor beneath the bear. "
    "(c) Add a warm amber glow on the crescent-moon glyph on the forehead, "
    "    matching reference 2. "
    "(d) Add a cast shadow and reflection on the dark floor, matching ref 2. "
    "CRITICAL: the bear's silhouette, head shape, cheek width, scale, and "
    "position in the frame must match reference 1 PIXEL-FOR-PIXEL. When the "
    "two images are cross-faded, the bear outline must overlap perfectly. Do "
    "not re-sculpt the bear. Do not change the camera. Do not reframe. Do not "
    "resize. Only change the lighting and fur color and environment. "
    "No text, no letters, no wordmark, no logo. 16:9 aspect, photographic."
)


def main():
    for p in (LIGHT_REF, DARK_REF):
        if not os.path.isfile(p):
            print(f"[BEAR] Missing reference: {p}")
            return

    print(f"[BEAR] Regenerating dark bear — relight light bear into dark cave...")
    print(f"       shape/scale source: {LIGHT_REF}")
    print(f"       lighting source:    {DARK_REF}")

    try:
        paths = gemini_edit_image(
            prompt=PROMPT,
            reference_image_paths=[LIGHT_REF, DARK_REF],
            resolution="2K",
            num_images=1,
        )
        if not paths:
            print("[BEAR] No image returned")
            return
        shutil.move(paths[0], OUT)
        size_mb = os.path.getsize(OUT) / (1024 * 1024)
        print(f"[BEAR] Saved -> {OUT} ({size_mb:.1f} MB)")
        print()
        print("Review public/bear_dark_matched.png against public/bear_light.png.")
        print("If the head shapes line up, rename bear_dark_matched.png -> bear_dark.png.")
    except Exception as e:
        print(f"[BEAR] FAILED: {e}")


if __name__ == "__main__":
    main()
