"""Bear trailer — Step 1: master establishing still.

The opening/setup image for the trailer: the white bear placed small in
the center of a vast empty film studio. A professional cinema camera and
a softbox are visible in the frame but scaled down — the room dwarfs them.

This is Kling I2V's starting frame for the push-in and orbit shots.

User vets this still before we proceed to any video generation.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import gemini_edit_image

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
LIGHT_REF = os.path.join(PUBLIC, "bear_light.png")
OUT = os.path.join(PUBLIC, "trailer_01_master.png")


PROMPT = (
    "Preserve the bear from reference 1 EXACTLY — same sculpt, same face, "
    "same crescent moon glyph, same cheek width, same eye shape, same fur "
    "color, same soft floor reflection underneath. Keep it as the identical "
    "3D character, only change the scene around it. "
    "NEW SCENE: place the bear in the exact center of a COMPLETELY ENDLESS "
    "PURE WHITE INFINITY VOID. The bear should be readable — roughly 12 "
    "percent of the frame width, clearly visible as the subject, but still "
    "dwarfed by the vast white space around him. No walls, no "
    "ceiling, no floor seam, no horizon line visible — the background must "
    "match the pale cream-white tone of reference 1 and extend forever in "
    "every direction. Think infinity cyclorama cove, product photography "
    "white void, seamless endless white limbo. The ground beneath the bear "
    "is the same soft pale white as the background, with a gentle subtle "
    "floor reflection underneath the bear exactly like reference 1. "
    "On screen-left, a professional cinema camera on a heavy tripod stands "
    "pointed directly at the bear — matte black body, visible lens, small "
    "in frame but clearly recognizable, casting its own faint floor "
    "reflection on the pale white surface. On screen-right, a tall 4-foot "
    "softbox on a black C-stand, also casting a faint reflection. The "
    "camera and softbox are the ONLY dark shapes in the otherwise pure "
    "white scene. "
    "CRITICAL SCALE: the bear, camera, and softbox must all feel tiny "
    "inside the ENDLESS white void. Enormous negative space in every "
    "direction. No visible walls, no ceiling, no room boundaries of any "
    "kind. The whiteness is unbroken. "
    "Wide establishing shot, full 16:9 frame, photographic, bright soft "
    "diffuse light from every direction (no harsh shadows), clean white "
    "color grade matching reference 1. "
    "NO text, NO letters, NO LUMN wordmark, NO logos, NO people, NO crew, "
    "NO visible walls, NO visible ceiling, NO concrete, NO grey, NO truss, "
    "NO overlays. Pure seamless white limbo."
)


def main():
    if not os.path.isfile(LIGHT_REF):
        print(f"[TRAILER] missing ref: {LIGHT_REF}")
        return

    print(f"[TRAILER] Generating master establishing still...")
    print(f"          identity ref: {LIGHT_REF}")
    print(f"          output:       {OUT}")

    try:
        paths = gemini_edit_image(
            prompt=PROMPT,
            reference_image_paths=[LIGHT_REF],
            resolution="2K",
            num_images=1,
        )
        if not paths:
            print("[TRAILER] No image returned")
            return
        shutil.move(paths[0], OUT)
        mb = os.path.getsize(OUT) / (1024 * 1024)
        print(f"[TRAILER] Saved -> {OUT} ({mb:.1f}MB)")
        print()
        print("Review public/trailer_01_master.png.")
        print("If the framing and scale feel right, we advance to Kling push-in.")
    except Exception as e:
        print(f"[TRAILER] FAILED: {e}")


if __name__ == "__main__":
    main()
