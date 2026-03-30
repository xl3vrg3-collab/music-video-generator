"""
Storyboard generator - creates a visual grid of all scenes as a PNG image.
Uses PIL/Pillow for image composition and ffmpeg for thumbnail extraction.
"""

import os
import subprocess
import sys
import math
from PIL import Image, ImageDraw, ImageFont


# ---- Constants ----
CARD_W = 640
CARD_H = 420
COLS = 2
PADDING = 24
HEADER_H = 80
BG_COLOR = (10, 10, 15)          # --bg
SURFACE_COLOR = (18, 18, 26)     # --surface
BORDER_COLOR = (42, 42, 58)      # --border
CYAN = (0, 212, 255)
MAGENTA = (255, 45, 123)
AMBER = (255, 170, 0)
TEXT_COLOR = (200, 200, 212)
TEXT_DIM = (106, 106, 122)
GREEN = (0, 255, 136)
THUMB_H = 240


def _subprocess_kwargs() -> dict:
    """Extra kwargs for subprocess calls (hide window on Windows)."""
    kw = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    return kw


def _extract_thumbnail(video_path: str, output_path: str, time_sec: float = 1.0) -> bool:
    """Extract a single frame from a video using ffmpeg."""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(time_sec),
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "2",
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
        return os.path.isfile(output_path)
    except Exception:
        return False


def _get_font(size: int):
    """Try to load a monospace font, fall back to default."""
    font_paths = [
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ]
    for fp in font_paths:
        if os.path.isfile(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(text: str, font, max_width: int, draw: ImageDraw.Draw) -> list:
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def generate_storyboard(scenes: list, output_path: str, temp_dir: str = None) -> str:
    """
    Generate a storyboard PNG from a list of scenes.

    Args:
        scenes: list of scene dicts with keys: prompt, duration, transition,
                clip_path (optional), index, start_sec, end_sec, etc.
        output_path: path for the output PNG
        temp_dir: directory for temporary thumbnail files

    Returns:
        path to the generated storyboard image
    """
    if temp_dir is None:
        temp_dir = os.path.join(os.path.dirname(output_path), "storyboard_tmp")
    os.makedirs(temp_dir, exist_ok=True)

    n = len(scenes)
    rows = math.ceil(n / COLS)

    img_w = COLS * CARD_W + (COLS + 1) * PADDING
    img_h = HEADER_H + rows * CARD_H + (rows + 1) * PADDING

    img = Image.new("RGB", (img_w, img_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    font_title = _get_font(28)
    font_scene = _get_font(16)
    font_small = _get_font(12)
    font_prompt = _get_font(13)

    # Draw header
    draw.text(
        (PADDING, PADDING),
        "STORYBOARD",
        fill=CYAN,
        font=font_title,
    )
    draw.text(
        (PADDING + 220, PADDING + 8),
        f"{n} scenes",
        fill=TEXT_DIM,
        font=font_small,
    )
    # Header line
    draw.line(
        [(PADDING, HEADER_H - 4), (img_w - PADDING, HEADER_H - 4)],
        fill=BORDER_COLOR,
        width=1,
    )

    for idx, scene in enumerate(scenes):
        row = idx // COLS
        col = idx % COLS

        x = PADDING + col * (CARD_W + PADDING)
        y = HEADER_H + PADDING + row * (CARD_H + PADDING)

        # Card background
        draw.rectangle(
            [x, y, x + CARD_W, y + CARD_H],
            fill=SURFACE_COLOR,
            outline=BORDER_COLOR,
            width=1,
        )

        # Scene number badge
        badge_text = f"#{idx + 1}"
        draw.rectangle([x, y, x + 50, y + 28], fill=AMBER)
        draw.text((x + 8, y + 5), badge_text, fill=BG_COLOR, font=font_scene)

        # Duration and transition info
        duration = scene.get("duration", 0)
        if not duration and "start_sec" in scene and "end_sec" in scene:
            duration = scene["end_sec"] - scene["start_sec"]
        transition = scene.get("transition", "crossfade")
        info_text = f"{duration:.1f}s | {transition.replace('_', ' ')}"
        draw.text((x + 60, y + 7), info_text, fill=TEXT_DIM, font=font_small)

        # Thumbnail area
        thumb_y = y + 36
        thumb_w = CARD_W - 16
        thumb_area = (x + 8, thumb_y, x + 8 + thumb_w, thumb_y + THUMB_H)

        clip_path = scene.get("clip_path", "")
        has_thumb = False

        if clip_path and os.path.isfile(clip_path):
            thumb_path = os.path.join(temp_dir, f"thumb_{idx:03d}.jpg")
            if _extract_thumbnail(clip_path, thumb_path):
                try:
                    thumb = Image.open(thumb_path)
                    thumb = thumb.resize((thumb_w, THUMB_H), Image.LANCZOS)
                    img.paste(thumb, (x + 8, thumb_y))
                    has_thumb = True
                except Exception:
                    pass

        if not has_thumb:
            # Draw a styled "no clip" card with prompt text
            draw.rectangle(
                thumb_area,
                fill=(15, 15, 22),
                outline=BORDER_COLOR,
                width=1,
            )
            prompt = scene.get("prompt", "(no prompt)")
            lines = _wrap_text(prompt, font_prompt, thumb_w - 24, draw)
            text_y = thumb_y + 16
            for line in lines[:8]:  # max 8 lines
                draw.text((x + 20, text_y), line, fill=TEXT_DIM, font=font_prompt)
                text_y += 18
            if len(lines) > 8:
                draw.text((x + 20, text_y), "...", fill=TEXT_DIM, font=font_prompt)

            # "NO CLIP" label
            draw.text(
                (x + thumb_w // 2 - 20, thumb_y + THUMB_H - 24),
                "NO CLIP",
                fill=MAGENTA,
                font=font_small,
            )

        # Prompt text below thumbnail
        prompt_y = thumb_y + THUMB_H + 8
        prompt = scene.get("prompt", "")
        if prompt:
            lines = _wrap_text(prompt, font_small, CARD_W - 24, draw)
            for line in lines[:4]:
                draw.text((x + 12, prompt_y), line, fill=TEXT_COLOR, font=font_small)
                prompt_y += 16
            if len(lines) > 4:
                draw.text((x + 12, prompt_y), "...", fill=TEXT_DIM, font=font_small)

    # Save
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    img.save(output_path, "PNG")

    # Clean up temp thumbnails
    try:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass

    return output_path
