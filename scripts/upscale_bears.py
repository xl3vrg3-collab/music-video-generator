"""Upscale welcome bears via fal clarity-upscaler.

Current bears are 2752x1536. We want max detail — run both through
fal-ai/clarity-upscaler at 2x (→ 5504x3072), low creativity so the
matched head geometry is preserved pixel-for-pixel.

Outputs _4k variants for side-by-side review; rename manually after QA.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import fal_client as _fal_sdk

from lib.fal_client import _fal_submit, _upload_to_fal, _download_file

PUBLIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))

TARGETS = [
    {
        "slug": "bear_light",
        "src": os.path.join(PUBLIC, "bear_light.png"),
        "out": os.path.join(PUBLIC, "bear_light_4k.png"),
        "prompt": (
            "Studio product photograph of a matte vinyl bear figurine, "
            "sharp micro-detail in the sculpt, soft white paper backdrop, "
            "fine floor reflection, photographic, crisp, no text."
        ),
    },
    {
        "slug": "bear_dark",
        "src": os.path.join(PUBLIC, "bear_dark.png"),
        "out": os.path.join(PUBLIC, "bear_dark_4k.png"),
        "prompt": (
            "Studio product photograph of a matte dark vinyl bear figurine "
            "in a moody cave, amber crescent glow on forehead, sharp rock "
            "texture, dark reflective floor, photographic, crisp, no text."
        ),
    },
]


def upscale(src_path: str, prompt: str, out_path: str):
    print(f"\n[UPSCALE] {os.path.basename(src_path)} -> {os.path.basename(out_path)}")
    url = _upload_to_fal(src_path)
    payload = {
        "image_url": url,
        "prompt": prompt,
        "upscale_factor": 2,
        "creativity": 0.2,
        "resemblance": 1.0,
        "guidance_scale": 4,
        "num_inference_steps": 18,
    }
    result = _fal_submit("fal-ai/clarity-upscaler", payload, timeout=900)
    images = result.get("image") or result.get("images") or []
    if isinstance(images, dict):
        images = [images]
    if not images:
        print(f"[UPSCALE] no image returned: {result}")
        return False
    img_url = images[0]["url"] if isinstance(images[0], dict) else images[0]
    _download_file(img_url, out_path)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    from PIL import Image
    w, h = Image.open(out_path).size
    print(f"[UPSCALE] saved {out_path}  {w}x{h}  ({size_mb:.1f}MB)")
    return True


def main():
    for t in TARGETS:
        if not os.path.isfile(t["src"]):
            print(f"[UPSCALE] missing src: {t['src']}")
            continue
        try:
            upscale(t["src"], t["prompt"], t["out"])
        except Exception as e:
            print(f"[UPSCALE] {t['slug']} FAILED: {e}")

    print("\nDone. Review public/bear_{light,dark}_4k.png.")
    print("If good, rename to bear_{light,dark}.png to replace.")


if __name__ == "__main__":
    main()
