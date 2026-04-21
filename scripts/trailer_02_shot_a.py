"""Bear trailer — Shot A: slow push-in on the base white bear.

Kling V3 Pro I2V, 5s, starting from the existing bear_light.png. Camera
dollies straight forward toward the bear's face, ending close on the
crescent moon glyph. No rotation, no rig visible, no pull-out. Intimate
portrait opener.

Prompt is short (Kling sweet spot 15-40 words), camera-move only, no
subject re-description, no sound words — per the Kling I2V rules we've
learned.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import kling_image_to_video

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
START = os.path.join(PUBLIC, "bear_light_2k_backup.png")
OUT = os.path.join(PUBLIC, "trailer_shot_a.mp4")

PROMPT = (
    "Camera dollies straight forward, subject stays locked dead center "
    "of frame, bear does not move or slide, only grows larger as the "
    "camera approaches, smooth steady push-in, no pan, no tilt, no "
    "reframing, cinematic ease."
)


def main():
    if not os.path.isfile(START):
        print(f"[SHOT A] missing start frame: {START}")
        return

    print(f"[SHOT A] Rendering push-in on base bear...")
    print(f"         start: {START}")
    print(f"         out:   {OUT}")
    print(f"         prompt: {PROMPT}")

    try:
        tmp = kling_image_to_video(
            start_image_path=START,
            prompt=PROMPT,
            duration=5,
            tier="v3_pro",
            generate_audio=False,
            cfg_scale=0.75,
            negative_prompt="bear sliding, bear moving, subject reframing, composition change, rotation, orbit, spin, zoom out, pull back, camera shake, lateral motion, pan, tilt, blur, distort, low quality, text, watermark, wordmark, logo",
        )
        shutil.move(tmp, OUT)
        mb = os.path.getsize(OUT) / (1024 * 1024)
        print(f"[SHOT A] Saved -> {OUT} ({mb:.1f}MB)")
    except Exception as e:
        print(f"[SHOT A] FAILED: {e}")


if __name__ == "__main__":
    main()
