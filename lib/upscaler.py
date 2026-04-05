"""
AI Upscaling via Real-ESRGAN.

Supports two backends:
1. realesrgan-ncnn-vulkan CLI (preferred — pre-compiled, fast, any GPU)
2. Python realesrgan package (fallback — needs PyTorch)
3. FFmpeg lanczos (basic fallback — no AI, just interpolation)

Usage:
    from lib.upscaler import upscale_image, upscale_video
    result = upscale_image("input.jpg", scale=4)  # Returns path to upscaled file
"""

import os
import subprocess
import shutil
import time


def _find_realesrgan_cli():
    """Find realesrgan-ncnn-vulkan binary."""
    # Check common locations
    candidates = [
        "realesrgan-ncnn-vulkan",  # On PATH
        os.path.expanduser("~/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan.exe"),
        os.path.expanduser("~/tools/realesrgan-ncnn-vulkan.exe"),
        r"C:\tools\realesrgan-ncnn-vulkan.exe",
    ]
    for c in candidates:
        if shutil.which(c):
            return shutil.which(c)
        if os.path.isfile(c):
            return c
    return None


def upscale_image(input_path: str, output_path: str = None, scale: int = 4,
                  model: str = "realesrgan-x4plus") -> str:
    """Upscale a single image using Real-ESRGAN.

    Args:
        input_path: Path to input image
        output_path: Path for output (default: adds _upscaled suffix)
        scale: Upscale factor (2 or 4)
        model: Model name (realesrgan-x4plus, realesrgan-x4plus-anime, realesr-animevideov3)

    Returns:
        Path to upscaled image, or input_path if upscaling fails
    """
    if not os.path.isfile(input_path):
        return input_path

    if not output_path:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_upscaled{ext}"

    # Try CLI first (fastest, most reliable)
    cli = _find_realesrgan_cli()
    if cli:
        try:
            cmd = [cli, "-i", input_path, "-o", output_path, "-s", str(scale), "-n", model]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and os.path.isfile(output_path):
                print(f"[UPSCALE] CLI: {os.path.basename(input_path)} -> {scale}x ({os.path.getsize(output_path)/1024:.0f}KB)")
                return output_path
            else:
                print(f"[UPSCALE] CLI failed: {result.stderr[:200]}")
        except Exception as e:
            print(f"[UPSCALE] CLI error: {e}")

    # Try Python package
    try:
        from realesrgan import RealESRGANer
        from basicsr.archs.rrdbnet_arch import RRDBNet
        import torch
        import numpy as np
        from PIL import Image

        # Load model
        model_net = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=scale)
        upsampler = RealESRGANer(
            scale=scale, model_path=None, model=model_net,
            tile=0, tile_pad=10, pre_pad=0, half=True,
        )

        img = np.array(Image.open(input_path).convert("RGB"))
        output, _ = upsampler.enhance(img, outscale=scale)

        Image.fromarray(output).save(output_path, quality=95)
        print(f"[UPSCALE] Python: {os.path.basename(input_path)} -> {scale}x")
        return output_path

    except ImportError:
        pass
    except Exception as e:
        print(f"[UPSCALE] Python error: {e}")

    # Fallback: FFmpeg lanczos (not AI, but better than nothing)
    try:
        from PIL import Image as PILFallback
        img = PILFallback.open(input_path)
        new_w = img.width * scale
        new_h = img.height * scale
        upscaled = img.resize((new_w, new_h), PILFallback.LANCZOS)
        upscaled.save(output_path, quality=95)
        print(f"[UPSCALE] Lanczos fallback: {img.width}x{img.height} -> {new_w}x{new_h}")
        return output_path
    except Exception as e:
        print(f"[UPSCALE] Fallback failed: {e}")

    return input_path  # Return original if all methods fail


def upscale_video(input_path: str, output_path: str = None, scale: int = 2) -> str:
    """Upscale a video using Real-ESRGAN or FFmpeg.

    For video, we use realesrgan-ncnn-vulkan with animevideov3 model,
    or fall back to FFmpeg lanczos scaling.
    """
    if not os.path.isfile(input_path):
        return input_path

    if not output_path:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_upscaled{ext}"

    # Try CLI with video model
    cli = _find_realesrgan_cli()
    if cli:
        try:
            cmd = [cli, "-i", input_path, "-o", output_path, "-s", str(scale),
                   "-n", "realesr-animevideov3"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0 and os.path.isfile(output_path):
                print(f"[UPSCALE] Video CLI: {scale}x upscale complete")
                return output_path
        except Exception as e:
            print(f"[UPSCALE] Video CLI error: {e}")

    # Fallback: FFmpeg lanczos
    try:
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        # Get original dimensions
        probe = subprocess.run(
            [ffmpeg, "-i", input_path],
            capture_output=True, text=True, timeout=10
        )
        # Extract resolution from ffmpeg output
        import re
        match = re.search(r'(\d{3,4})x(\d{3,4})', probe.stderr)
        if match:
            orig_w, orig_h = int(match.group(1)), int(match.group(2))
            new_w = orig_w * scale
            new_h = orig_h * scale

            cmd = [
                ffmpeg, "-i", input_path,
                "-vf", f"scale={new_w}:{new_h}:flags=lanczos",
                "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-c:a", "copy",
                "-y", output_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0 and os.path.isfile(output_path):
                print(f"[UPSCALE] FFmpeg lanczos: {orig_w}x{orig_h} -> {new_w}x{new_h}")
                return output_path
    except Exception as e:
        print(f"[UPSCALE] FFmpeg error: {e}")

    return input_path


def get_upscale_status() -> dict:
    """Check which upscaling backends are available."""
    status = {
        "cli": bool(_find_realesrgan_cli()),
        "cli_path": _find_realesrgan_cli(),
        "python": False,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "best_method": "none",
    }

    try:
        import realesrgan
        status["python"] = True
    except ImportError:
        pass

    if status["cli"]:
        status["best_method"] = "realesrgan-cli"
    elif status["python"]:
        status["best_method"] = "realesrgan-python"
    elif status["ffmpeg"]:
        status["best_method"] = "ffmpeg-lanczos"

    return status
