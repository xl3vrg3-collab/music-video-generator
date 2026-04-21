"""Re-render ONLY the dark bear, matching the light bear's typography size/style.

User feedback v3:
- Colors are right now (amber glow on dark, charcoal on light)
- But the two bears must match in font SIZE and STYLE
- The light bear has the correct size — smaller, more restrained
- The dark bear is currently too big/bold — shrink it

Strategy: feed Gemini BOTH the dark bear (as character/environment ref) AND
the current light bear v2 (as typography size/style ref). Tell it explicitly
to match the letter scale and tracking from the light reference while keeping
the amber luminous gradient treatment from the dark reference.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import gemini_edit_image

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))

# Character/environment ref (what to preserve)
DARK_REF = os.path.join(PUBLIC, "bear_dark.png")

# Typography size/style ref (what to match for letter size and tracking)
LIGHT_REF = os.path.join(PUBLIC, "bear_light_v2.png")

DEST = os.path.join(PUBLIC, "bear_dark_v2.png")

PROMPT = (
    "This task uses TWO reference images. "
    "Reference 1 (the dark cave bear): use this for the bear character, environment, "
    "lighting, shadow, and overall composition. Preserve the 3D bear character exactly "
    "— identical face, crescent-moon glyph, fur detail, pose. Preserve the dark cave "
    "environment and the cast shadow beneath the bear. "
    ""
    "Reference 2 (the white bear with LUMN text): use this ONLY as a guide for the "
    "typography SIZE, PROPORTIONS, LETTER-SPACING, and FONT STYLE. The LUMN wordmark "
    "in Reference 2 is the correct scale — thin, elegant, restrained, sitting in the "
    "upper-center of the frame at a relatively modest size. Match that exact letter "
    "height, stroke weight, and tracking. "
    ""
    "Output: produce the dark cave bear scene from Reference 1, but with the LUMN "
    "wordmark re-rendered at the SAME SIZE AND STYLE as Reference 2's wordmark. "
    ""
    "The letters themselves must be filled with a WARM AMBER LUMINOUS GRADIENT — "
    "glowing gold, brightest along one edge of each letter, fading to cooler amber "
    "at the opposite edge. The glow should feel like the same light source as the "
    "crescent moon on the bear's forehead — as if the letters are emitting the warm "
    "amber light that illuminates the bear and casts its shadow below. NOT flat fill. "
    "NOT charcoal. Keep the amber palette. "
    ""
    "Every letter (L, U, M, N) must be equally visible with identical stroke weight "
    "and the same gradient direction. No letter dominates. Position the wordmark in "
    "the upper-center area. "
    ""
    "Do NOT make the letters larger or bolder than Reference 2's letters. Err on the "
    "side of SMALLER and THINNER. The LUMN title should feel patient and cinematic, "
    "like a film title card lit from within, not a heavy logo stamp."
)


def main():
    if not os.path.isfile(DARK_REF):
        print(f"[BEAR] Missing dark ref: {DARK_REF}")
        return
    if not os.path.isfile(LIGHT_REF):
        print(f"[BEAR] Missing light ref: {LIGHT_REF}")
        return

    print(f"[BEAR] Re-rendering dark bear matching light bear typography scale")
    print(f"       char ref:  {DARK_REF}")
    print(f"       type ref:  {LIGHT_REF}")
    print(f"       dest:      {DEST}")

    paths = gemini_edit_image(
        prompt=PROMPT,
        reference_image_paths=[DARK_REF, LIGHT_REF],
        resolution="2K",
        num_images=1,
    )
    if not paths:
        print("[BEAR] No image returned")
        return
    shutil.move(paths[0], DEST)
    size_mb = os.path.getsize(DEST) / (1024 * 1024)
    print(f"[BEAR] Saved -> {DEST} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
