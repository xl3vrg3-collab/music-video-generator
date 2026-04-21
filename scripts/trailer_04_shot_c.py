"""Bear trailer — Shot C: dark bear climax reveal.

Kling V3 Pro I2V, 5s, starting from bear_dark_2k_backup.png. After the
click + blackout in post, this shot fades up. Subtle atmospheric drift,
amber crescent glow pulses gently, cave mood, minimal motion. The LUMN
wordmark is overlaid on top in post-production via ffmpeg.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import kling_image_to_video

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
START = os.path.join(PUBLIC, "bear_dark_2k_backup.png")
OUT = os.path.join(PUBLIC, "trailer_shot_c.mp4")

PROMPT = (
    "Subtle atmospheric drift, very slow push-in, amber crescent glow on "
    "forehead gently pulses and flickers, faint cave atmosphere, volumetric "
    "haze stirs softly, bear remains perfectly still and centered, moody, "
    "minimal camera motion."
)


def main():
    if not os.path.isfile(START):
        print(f"[SHOT C] missing start frame: {START}")
        return

    print(f"[SHOT C] Rendering dark bear climax...")
    print(f"         start: {START}")

    try:
        tmp = kling_image_to_video(
            start_image_path=START,
            prompt=PROMPT,
            duration=5,
            tier="v3_pro",
            generate_audio=False,
            cfg_scale=0.7,
            negative_prompt="bear moving, bear sliding, fast motion, rotation, camera shake, zoom out, color shift, blur, distort, low quality, text, watermark, wordmark, logo",
        )
        shutil.move(tmp, OUT)
        mb = os.path.getsize(OUT) / (1024 * 1024)
        print(f"[SHOT C] Saved -> {OUT} ({mb:.1f}MB)")
    except Exception as e:
        print(f"[SHOT C] FAILED: {e}")


if __name__ == "__main__":
    main()
