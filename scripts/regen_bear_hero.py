"""Re-render the bear hero stills with the new LUMN typography baked in.

Constraints from user (2026-04-14):
- Keep the SAME bear character (identical 3D render)
- Keep the SETTING similar (not identical, but recognizable)
- PRESERVE the reflection on the white/light version
- PRESERVE the shadow on the dark version
- ONLY change the LUMN wordmark typography to match the welcome page:
  Inter Tight weight 300, very wide letter-spacing, muted rgba whites
  (previously: heavier warm amber on dark / muted grey on light)

Strategy: feed each existing bear image into gemini_edit_image as a reference,
and instruct Gemini to preserve everything except the type treatment. Writes
outputs to public/bear_dark_v2.png and public/bear_light_v2.png so the user
can compare before overwriting the originals.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import gemini_edit_image

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))

SHARED_PRESERVATION = (
    "CRITICAL: Preserve the exact 3D bear character from the reference photograph — "
    "identical face, pose, fur detail, crescent-moon glyph on the forehead, eyes, "
    "and sculptural form. Preserve the surface material, lighting, and framing. "
    "Keep the setting similar to the reference. "
)

TYPE_TREATMENT_DARK = (
    "ONLY change the LUMN wordmark typography. Re-render the LUMN title so it reads "
    "as a thin, elegant sans-serif in the style of Inter Tight weight 300, "
    "with extreme wide letter-spacing (approximately 0.4em tracking between letters), "
    "rendered in a soft muted white at roughly 75% opacity (color: rgba(228,228,234,0.72)). "
    "No amber, no warm yellow, no heavy weight, no drop shadow. "
    "Position the wordmark where it currently sits in the reference image. "
    "The letters should feel cinematic and patient, like a film title card, not a logo stamp. "
    "EVERY letter (L, U, M, N) must be equally visible and have identical stroke weight."
)

TYPE_TREATMENT_LIGHT = (
    "ONLY change the LUMN wordmark typography. Re-render the LUMN title so it reads "
    "as a thin, elegant sans-serif in the style of Inter Tight weight 300, "
    "with extreme wide letter-spacing (approximately 0.4em tracking between letters), "
    "rendered in a medium-dark warm grey at roughly 70% opacity "
    "(color approximately rgba(80,80,88,0.78)) — NOT light grey, NOT white. "
    "The text must have STRONG visible contrast against the pale white backdrop. "
    "Every letter must be equally legible — no fading, no dissolving into the background. "
    "No drop shadow, no outline, no gradient. Uniform stroke weight across L, U, M, N. "
    "Position the wordmark where it currently sits in the reference image. "
    "The letters should feel cinematic and patient, like a film title card printed in charcoal ink."
)

VARIANTS = [
    {
        "slug": "bear_dark_v2",
        "ref": os.path.join(PUBLIC, "bear_dark.png"),
        "type_treatment": TYPE_TREATMENT_DARK,
        "extra": (
            "Preserve the deep cast shadow under the bear on the dark surface — "
            "the shadow is part of the composition and must remain exactly as in the reference."
        ),
    },
    {
        "slug": "bear_light_v2",
        "ref": os.path.join(PUBLIC, "bear_light.png"),
        "type_treatment": TYPE_TREATMENT_LIGHT,
        "extra": (
            "Preserve the soft floor reflection beneath the bear on the pale surface — "
            "the reflection is part of the composition and must remain exactly as in the reference."
        ),
    },
]


def main():
    for v in VARIANTS:
        if not os.path.isfile(v["ref"]):
            print(f"[BEAR] Missing reference: {v['ref']}")
            continue

        prompt = f"{SHARED_PRESERVATION}{v['extra']} {v['type_treatment']}"
        print(f"\n[BEAR] Re-rendering {v['slug']} ...")
        print(f"       ref: {v['ref']}")

        try:
            paths = gemini_edit_image(
                prompt=prompt,
                reference_image_paths=[v["ref"]],
                resolution="2K",
                num_images=1,
            )
            if not paths:
                print(f"[BEAR] {v['slug']}: no image returned")
                continue
            dest = os.path.join(PUBLIC, f"{v['slug']}.png")
            shutil.move(paths[0], dest)
            size_mb = os.path.getsize(dest) / (1024 * 1024)
            print(f"[BEAR] {v['slug']}: saved -> {dest} ({size_mb:.1f} MB)")
        except Exception as e:
            print(f"[BEAR] {v['slug']} FAILED: {e}")

    print("\nDone. Review public/bear_dark_v2.png and public/bear_light_v2.png,")
    print("then rename to bear_dark.png / bear_light.png to replace the originals.")


if __name__ == "__main__":
    main()
