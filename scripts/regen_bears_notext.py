"""Re-render the bear hero stills with the LUMN wordmark REMOVED.

Goal: produce bear_dark.png / bear_light.png that match the current hero
images EXACTLY in every visual dimension — bear position, lighting, mood,
shadow, reflection, setting, color grade — except the baked-in "LUMN"
wordmark must be gone. The welcome screen will then overlay a live CSS
<span class="lumn-wordmark"> on top at the same location, so the type
finally matches index.html and manifesto.html pixel-for-pixel.

Writes to public/bear_dark_notext.png and public/bear_light_notext.png
so the user can compare before overwriting the originals.
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
    "Re-render this exact photograph. CRITICAL: preserve every visual element "
    "from the reference — the 3D bear character (identical face, pose, fur, "
    "sculptural form, crescent-moon glyph on the forehead, eyes), the bear's "
    "position and scale within the frame, the framing and aspect, the lighting "
    "direction and intensity, the background environment and depth, the color "
    "grade and mood, the surface the bear is resting on, and any shadows or "
    "reflections beneath it. The feeling of the photograph must remain identical. "
)

REMOVE_WORDMARK = (
    "The ONLY change: the 'LUMN' wordmark text that currently appears in the "
    "upper portion of the reference image must be completely removed. Replace "
    "the area where the letters sit with clean, seamless background that matches "
    "the surrounding environment — no text, no letterforms, no residual glow, "
    "no outline of where the letters used to be, no artifacts. The space above "
    "the bear should read as empty atmosphere consistent with the rest of the "
    "scene. Do not add any new text, logo, symbol, or mark. Return the image "
    "with the bear and its environment intact but wordless."
)

VARIANTS = [
    {
        "slug": "bear_dark_notext",
        "ref": os.path.join(PUBLIC, "bear_dark.png"),
        "extra": (
            "This is the dark variant — moody cave/rock environment with deep "
            "shadows, warm amber accent light on the bear's crescent-moon glyph, "
            "and a cast shadow / reflection beneath the bear on a dark reflective "
            "surface. Preserve the amber glow on the bear's forehead. Preserve "
            "the cast shadow exactly. When removing the wordmark, fill the space "
            "with the same dark rocky atmosphere that surrounds it — not flat "
            "black, but the organic cave texture already visible in the frame."
        ),
    },
    {
        "slug": "bear_light_notext",
        "ref": os.path.join(PUBLIC, "bear_light.png"),
        "extra": (
            "This is the light variant — minimal pale white / cream environment "
            "with the bear softly lit against a near-white backdrop and a subtle "
            "floor reflection beneath it. Preserve the bear's soft amber crescent "
            "glow. Preserve the floor reflection exactly. When removing the "
            "wordmark, fill the space with the same clean pale atmosphere — no "
            "grey smudge, no flat white patch, just the natural soft gradient "
            "already present in the reference."
        ),
    },
]


def main():
    for v in VARIANTS:
        if not os.path.isfile(v["ref"]):
            print(f"[BEAR] Missing reference: {v['ref']}")
            continue

        prompt = f"{SHARED_PRESERVATION}{v['extra']} {REMOVE_WORDMARK}"
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

    print("\nDone. Review public/bear_dark_notext.png and public/bear_light_notext.png,")
    print("then rename to bear_dark.png / bear_light.png to replace the originals.")


if __name__ == "__main__":
    main()
