"""Re-render bears v3: thin Inter Tight letterforms WITH luminous gradient.

User feedback from v2:
- Solid-color typography feels dead compared to originals
- Originals had a warm gradient on the dark bear where the LUMN letters
  looked like they were emitting the same light as the crescent glyph —
  "creating the shadow on the bear"
- Want new thin Inter Tight weight 300 forms + wide letter-spacing,
  BUT with the old light-gradient treatment, not flat fill

Dark version:  warm amber light gradient on letters (matches crescent glow)
Light version: cool tonal gradient on letters (matches floor reflection glow)
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

DARK_PROMPT = SHARED_PRESERVATION + (
    "Preserve the deep cast shadow under the bear on the dark surface — "
    "the shadow is part of the composition and must remain exactly as in the reference. "
    ""
    "Re-render the LUMN wordmark typography ONLY. New letterforms: thin, elegant "
    "sans-serif in the style of Inter Tight weight 300, with extreme wide letter-spacing "
    "(approximately 0.4em tracking). Letters should be slim and patient, not bold — "
    "feel like a film title card, not a heavy logo stamp. "
    ""
    "BUT preserve the LUMINOUS GRADIENT fill from the reference image: warm amber-gold "
    "light that is brighter along one edge of each letter (as if lit from the same "
    "source as the glowing crescent moon on the bear's forehead), fading to a cooler "
    "dimmer tone at the opposite edge. The letters should feel like they are glowing "
    "from within — the same warm light that illuminates the bear and casts the shadow "
    "below it. NOT a flat fill. Keep the amber palette (warm gold, not white). "
    ""
    "Every letter (L, U, M, N) must be equally legible with identical stroke weight "
    "and the same gradient direction. Position the wordmark in the upper area where "
    "it currently sits in the reference."
)

LIGHT_PROMPT = SHARED_PRESERVATION + (
    "Preserve the soft floor reflection beneath the bear on the pale surface — "
    "the reflection is part of the composition and must remain exactly as in the reference. "
    ""
    "Re-render the LUMN wordmark typography ONLY. New letterforms: thin, elegant "
    "sans-serif in the style of Inter Tight weight 300, with extreme wide letter-spacing "
    "(approximately 0.4em tracking). Letters should be slim and patient, not bold. "
    ""
    "Fill the letters with a SOFT TONAL GRADIENT — medium-dark warm grey at one edge "
    "fading to a slightly lighter warm grey at the opposite edge. Think charcoal ink "
    "with depth, not flat paint. The letters should have the same quiet luminous "
    "quality as the floor reflection beneath the bear — tonal, layered, alive. "
    ""
    "The text must have STRONG visible contrast against the pale white backdrop — "
    "every letter (L, U, M, N) equally legible, identical stroke weight, same gradient "
    "direction. The L must be as visible as the N. No letter may dissolve into the "
    "background. Darkest value in the gradient should be around rgba(60,60,66,0.9). "
    ""
    "Position the wordmark in the upper area where it currently sits in the reference. "
    "Feel like a cinematic film title card printed in gradient charcoal on pale paper."
)

VARIANTS = [
    ("bear_dark_v2",  os.path.join(PUBLIC, "bear_dark.png"),  DARK_PROMPT),
    ("bear_light_v2", os.path.join(PUBLIC, "bear_light.png"), LIGHT_PROMPT),
]


def main():
    for slug, ref, prompt in VARIANTS:
        if not os.path.isfile(ref):
            print(f"[BEAR] Missing reference: {ref}")
            continue

        print(f"\n[BEAR] Re-rendering {slug} (luminous gradient)")
        print(f"       ref: {ref}")

        try:
            paths = gemini_edit_image(
                prompt=prompt,
                reference_image_paths=[ref],
                resolution="2K",
                num_images=1,
            )
            if not paths:
                print(f"[BEAR] {slug}: no image returned")
                continue
            dest = os.path.join(PUBLIC, f"{slug}.png")
            shutil.move(paths[0], dest)
            size_mb = os.path.getsize(dest) / (1024 * 1024)
            print(f"[BEAR] {slug}: saved -> {dest} ({size_mb:.1f} MB)")
        except Exception as e:
            print(f"[BEAR] {slug} FAILED: {e}")


if __name__ == "__main__":
    main()
