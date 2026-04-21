"""Bear trailer — Shot B: orbit reveal of the rig.

NEW direction: Shot B picks up EXACTLY where Shot A ends (the close-up of
the white bear's face) and interpolates out to the master establishing
wide via Kling's first+last frame anchoring. Camera pulls back and arcs
around the subject, revealing the cinema camera and softbox watching from
either side. The 4th-wall reveal moment.

Start: trailer_shot_a_lastframe.png (extracted from end of trailer_shot_a.mp4)
End:   trailer_01_master.png (wide establishing with bear + rig)
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from lib.fal_client import kling_image_to_video

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
START = os.path.join(PUBLIC, "trailer_shot_a_lastframe.png")
END = os.path.join(PUBLIC, "trailer_01_master.png")
OUT = os.path.join(PUBLIC, "trailer_shot_b.mp4")

PROMPT = (
    "Camera slowly pulls back and arcs around the subject, revealing the "
    "professional cinema camera and softbox on either side of the bear, "
    "smooth cinematic reveal, steady dolly-back, fourth wall moment."
)


def main():
    if not os.path.isfile(START):
        print(f"[SHOT B] missing start frame: {START}")
        return
    if not os.path.isfile(END):
        print(f"[SHOT B] missing end frame: {END}")
        return

    print(f"[SHOT B] Rendering orbit reveal...")
    print(f"         start: {START}")
    print(f"         end:   {END}")

    try:
        tmp = kling_image_to_video(
            start_image_path=START,
            end_image_path=END,
            prompt=PROMPT,
            duration=5,
            tier="v3_pro",
            generate_audio=False,
            cfg_scale=0.7,
            negative_prompt="bear moving, bear sliding, fast motion, camera shake, blur, distort, low quality, text, watermark, wordmark, logo, jump cut, discontinuity",
        )
        shutil.move(tmp, OUT)
        mb = os.path.getsize(OUT) / (1024 * 1024)
        print(f"[SHOT B] Saved -> {OUT} ({mb:.1f}MB)")
    except Exception as e:
        print(f"[SHOT B] FAILED: {e}")


if __name__ == "__main__":
    main()
