"""One-off: re-render ONLY the light bear with a darker, readable wordmark.

The first pass used muted-white type which disappeared into the pale backdrop
(the L especially was dissolving). This run specifies a medium-dark charcoal
tone so every letter reads clearly.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import gemini_edit_image

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
REF = os.path.join(PUBLIC, "bear_light.png")
DEST = os.path.join(PUBLIC, "bear_light_v2.png")

PROMPT = (
    "CRITICAL: Preserve the exact 3D bear character from the reference photograph — "
    "identical face, pose, fur detail, crescent-moon glyph on the forehead, eyes, and "
    "sculptural form. Preserve the surface material, lighting, and framing. Keep the "
    "setting similar to the reference. Preserve the soft floor reflection beneath the "
    "bear on the pale surface — the reflection is part of the composition and must "
    "remain exactly as in the reference. "
    ""
    "ONLY change the LUMN wordmark typography. Re-render the LUMN title so it reads "
    "as a thin, elegant sans-serif in the style of Inter Tight weight 300, with extreme "
    "wide letter-spacing (approximately 0.4em tracking between letters). "
    ""
    "Render the text in MEDIUM-DARK CHARCOAL GREY — color approximately "
    "rgba(70,70,78,0.85) — NOT light grey, NOT white. The letters must have STRONG "
    "visible contrast against the pale white backdrop so every character reads clearly. "
    ""
    "Every letter (L, U, M, N) must be equally legible — identical stroke weight, "
    "no fading, no letter dissolving into the background, no gradient across the word. "
    "The L in particular must be as visible as the N. "
    ""
    "No drop shadow, no outline, no glow. Uniform stroke weight across the entire word. "
    "Position the wordmark in the upper-center area where it currently sits in the "
    "reference image. The letters should feel cinematic and patient, like a film title "
    "card printed in charcoal ink on pale museum paper."
)


def main():
    if not os.path.isfile(REF):
        print(f"[BEAR] Missing reference: {REF}")
        return

    print(f"[BEAR] Re-rendering light bear with darker charcoal wordmark")
    print(f"       ref:  {REF}")
    print(f"       dest: {DEST}")

    paths = gemini_edit_image(
        prompt=PROMPT,
        reference_image_paths=[REF],
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
