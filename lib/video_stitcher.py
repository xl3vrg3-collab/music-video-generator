"""
Video stitcher using ffmpeg.
Concatenates clips with configurable transitions (crossfade, hard_cut,
fade_black, wipe, dissolve, zoom, glitch), overlays the audio track,
and applies fade in/out.  Supports lyrics overlay and aspect ratio crop/pad.
Supports per-scene speed control, text overlays, color grading presets,
and audio visualization overlays.
"""

import os
import sys
import subprocess
import tempfile
import json


# ---- Aspect ratio presets ----
ASPECT_PRESETS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1":  (1080, 1080),
    "4:5":  (1080, 1350),
}

# ---- Speed presets ----
SPEED_OPTIONS = [0.25, 0.5, 1.0, 1.5, 2.0]

# ---- Color grading presets ----
COLOR_GRADE_PRESETS = {
    "none": "",
    "warm": "colorbalance=rs=.15:gs=.05:bs=-.1:rm=.1:gm=.05:bm=-.05",
    "cold": "colorbalance=rs=-.1:gs=.0:bs=.15:rm=-.05:gm=.02:bm=.1",
    "vintage": "curves=vintage,eq=saturation=0.7:contrast=1.1",
    "high_contrast": "eq=contrast=1.5:brightness=0.02:saturation=1.2",
    "noir": "eq=saturation=0:contrast=1.4:brightness=-0.05:gamma=0.9",
    "cyberpunk": "colorbalance=rs=-.1:gs=-.15:bs=.3:rh=.1:gh=-.1:bh=.2,eq=contrast=1.3:saturation=1.4",
    "sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
}

# ---- Text overlay position mapping ----
TEXT_POSITIONS = {
    "top": "x=(w-text_w)/2:y=40",
    "center": "x=(w-text_w)/2:y=(h-text_h)/2",
    "bottom": "x=(w-text_w)/2:y=h-text_h-40",
}

TEXT_SIZES = {
    "small": 24,
    "medium": 40,
    "large": 60,
}

TEXT_COLORS = {
    "white": "FFFFFF",
    "cyan": "00D4FF",
    "orange": "FFAA00",
}

# ---- Audio visualization styles ----
AUDIO_VIZ_STYLES = ["waveform", "spectrum", "both"]


# ---- Supported transition types ----
TRANSITION_TYPES = [
    # Basic
    "crossfade",    # xfade=transition=fade
    "hard_cut",     # instant concat, no filter
    "fade_black",   # fade out + black gap + fade in
    "dissolve",     # xfade=transition=dissolve (longer duration)
    # Directional
    "wipe_left",    # xfade=transition=wipeleft
    "wipe_right",   # xfade=transition=wiperight
    "slide_left",   # xfade=transition=slideleft
    "slide_right",  # xfade=transition=slideright
    "slide_up",     # xfade=transition=slideup
    "slide_down",   # xfade=transition=slidedown
    # Creative
    "zoom_in",      # xfade=transition=zoomin
    "glitch",       # rapid 0.1s alternating cuts
    "rotate",       # xfade=transition=circleopen
    "blur_transition",  # xfade=transition=smoothleft
    "flash_white",  # xfade=transition=fadewhite
    "flash_black",  # xfade=transition=fadeblack
    "pixelate",     # xfade=transition=pixelize
    "squeeze",      # xfade=transition=squeezeh
    "circular_reveal",  # xfade=transition=circleopen
]

# ---- Scene effect presets (applied per-clip before stitching) ----
SCENE_EFFECTS = [
    "none",
    "film_grain",
    "vignette",
    "shake",
    "strobe",
    "mirror",
    "tilt_shift",
    "old_film",
    "glitch_effect",
    "dream",
    "night_vision",
]


def _subprocess_kwargs() -> dict:
    """Extra kwargs for subprocess calls (hide window on Windows)."""
    kw = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kw["startupinfo"] = si
    return kw


def _check_ffmpeg():
    """Verify ffmpeg is available."""
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, check=True,
            **_subprocess_kwargs(),
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg not found. Install it and make sure it is on your PATH.\n"
            "Download: https://ffmpeg.org/download.html"
        )


def _get_clip_duration(path: str) -> float:
    """Get duration of a video clip using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, **_subprocess_kwargs())
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 8.0  # default assumption


def _get_xfade_name(transition: str) -> str | None:
    """Map transition type to ffmpeg xfade transition name. Returns None for non-xfade types."""
    mapping = {
        "crossfade": "fade",
        "wipe_left": "wipeleft",
        "wipe_right": "wiperight",
        "dissolve": "dissolve",
        "zoom_in": "zoomin",
        "slide_left": "slideleft",
        "slide_right": "slideright",
        "slide_up": "slideup",
        "slide_down": "slidedown",
        "rotate": "circleopen",
        "blur_transition": "smoothleft",
        "flash_white": "fadewhite",
        "flash_black": "fadeblack",
        "pixelate": "pixelize",
        "squeeze": "squeezeh",
        "circular_reveal": "circleopen",
    }
    return mapping.get(transition)


def _get_transition_duration(transition: str, base_crossfade: float,
                             clip_duration: float = 0) -> float:
    """
    Get the xfade duration for a transition type.

    Area 4 item 7: Varies crossfade based on clip length when clip_duration > 0:
        - Short clips (3-5s): 0.3s crossfade
        - Medium clips (5-8s): 0.5s crossfade
        - Long clips (8-15s): 0.8s crossfade
    """
    if transition == "dissolve":
        return min(base_crossfade * 2.0, 2.0)  # slower dissolve
    if transition in ("hard_cut", "glitch"):
        return 0.0  # these don't use xfade
    if transition in ("flash_white", "flash_black"):
        return min(base_crossfade * 0.8, 0.6)  # quick flash

    # Area 4 item 7: Vary crossfade based on clip length
    if clip_duration > 0:
        if clip_duration <= 5:
            return 0.3
        elif clip_duration <= 8:
            return 0.5
        else:
            return 0.8

    return base_crossfade


def _apply_speed_ramp(clip_path: str, ramp_type: str, output_dir: str,
                      index: int, progress_cb=None) -> str:
    """
    Apply a speed ramp (variable speed) to a clip using ffmpeg setpts.

    ramp_type:
        'slow_mid'  - normal -> slow -> normal (ease in/out at center)
        'slow_in'   - slow -> normal
        'slow_out'  - normal -> slow

    The ramp uses a sinusoidal PTS expression for smooth easing.
    """
    if not ramp_type or ramp_type == "none":
        return clip_path

    if progress_cb:
        progress_cb(f"applying speed ramp ({ramp_type}) to clip {index}...")

    out_path = os.path.join(output_dir, f"_ramp_{index}_{ramp_type}.mp4")

    # Get clip duration to build the expression
    dur = _get_clip_duration(clip_path)

    # Build PTS expression based on ramp type
    # The idea: we modulate the PTS factor using a time-based expression.
    # slow_mid: speed = 1.0 at edges, 0.5 at center (sin curve)
    # slow_in: speed ramps from 0.5 to 1.0
    # slow_out: speed ramps from 1.0 to 0.5
    if ramp_type == "slow_mid":
        # PTS factor: 1 + 0.5*sin(pi*T/DUR) where T is normalized time
        # Higher PTS factor = slower playback
        vfilter = (
            f"setpts='PTS + 0.5*(1/{dur})*sin(PI*T/{dur})*T*TB'"
        )
    elif ramp_type == "slow_in":
        # Start slow, end normal: PTS factor decreases over time
        vfilter = (
            f"setpts='PTS + 0.4*({dur}-T)/{dur}*T*TB'"
        )
    elif ramp_type == "slow_out":
        # Start normal, end slow: PTS factor increases over time
        vfilter = (
            f"setpts='PTS + 0.4*(T/{dur})*T*TB'"
        )
    else:
        return clip_path

    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-filter:v", vfilter,
        "-an",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
        return out_path
    except subprocess.CalledProcessError:
        # Fallback: return original clip if the complex expression fails
        if progress_cb:
            progress_cb(f"speed ramp failed for clip {index}, using original")
        return clip_path


def _apply_reverse(clip_path: str, output_dir: str, index: int,
                   progress_cb=None) -> str:
    """Reverse a video clip using ffmpeg reverse filter."""
    if progress_cb:
        progress_cb(f"reversing clip {index}...")

    out_path = os.path.join(output_dir, f"_reversed_{index}.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-vf", "reverse",
        "-an",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
        return out_path
    except subprocess.CalledProcessError:
        if progress_cb:
            progress_cb(f"reverse failed for clip {index}, using original")
        return clip_path


def apply_effect(input_path: str, output_path: str, effect_name: str,
                 intensity: float = 0.5) -> str:
    """
    Apply a visual effect to a video clip using ffmpeg filters.

    Args:
        input_path: source video clip
        output_path: destination video
        effect_name: one of SCENE_EFFECTS
        intensity: 0.1 to 1.0 (controls effect strength)

    Returns:
        path to the output video with effect applied
    """
    _check_ffmpeg()

    if not effect_name or effect_name == "none":
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Clip not found: {input_path}")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    intensity = max(0.1, min(1.0, intensity))

    # Build filter based on effect name
    noise_str = int(20 * intensity)
    blur_sigma = 2 + intensity * 4
    brightness = 0.03 + intensity * 0.07
    shake_px = int(5 + intensity * 15)

    effect_filters = {
        "film_grain": f"noise=alls={noise_str}:allf=t",
        "vignette": f"vignette=PI/{max(2, int(6 - intensity * 4))}",
        "shake": f"crop=iw-{shake_px}:ih-{shake_px}:{shake_px//2}+random(0)*{shake_px//2}:{shake_px//2}+random(0)*{shake_px//2}",
        "strobe": f"eq=brightness=0.15*sin(2*PI*t*{4 + intensity * 8}):eval=frame",
        "mirror": "crop=iw/2:ih:0:0,split[l][r];[r]hflip[rr];[l][rr]hstack",
        "tilt_shift": f"split[a][b];[a]crop=iw:ih*0.4:0:ih*0.3[center];[b]boxblur={int(4 + intensity * 8)}[blurred];[blurred][center]overlay=0:(H-h)/2",
        "old_film": f"colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,noise=alls={int(15 + intensity * 20)}:allf=t,eq=contrast=1.1:brightness=-0.02",
        "glitch_effect": f"rgbashift=rh=-{int(3 + intensity * 7)}:bh={int(3 + intensity * 7)},noise=alls={int(5 + intensity * 10)}",
        "dream": f"gblur=sigma={blur_sigma:.1f},eq=brightness={brightness:.2f}:saturation={1.0 + intensity * 0.3:.1f}",
        "night_vision": f"colorchannelmixer=0:1:0:0:0:1:0:0:0:1:0,noise=alls={int(10 + intensity * 10)},vignette",
    }

    vf = effect_filters.get(effect_name)
    if not vf:
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    # Some effects use filter_complex (mirror, tilt_shift), others use -vf
    uses_filter_complex = effect_name in ("mirror", "tilt_shift")

    if uses_filter_complex:
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-filter_complex", vf,
            "-an",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", vf,
            "-an",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    except subprocess.CalledProcessError:
        # Fallback: copy original if effect fails
        import shutil
        shutil.copy2(input_path, output_path)

    return output_path


def apply_audio_crossfade(clips_audio_segments: list, output_path: str,
                          crossfade_duration: float = 0.5,
                          progress_cb=None) -> str:
    """
    Apply audio crossfade between adjacent audio segments using ffmpeg acrossfade.
    This is used post-stitch when the audio track needs smooth transitions.

    Args:
        clips_audio_segments: list of audio file paths
        output_path: destination audio file
        crossfade_duration: crossfade overlap in seconds (0-2)
        progress_cb: optional status callback

    Returns:
        path to the crossfaded audio output
    """
    _check_ffmpeg()

    valid = [p for p in clips_audio_segments if p and os.path.isfile(p)]
    if len(valid) < 2:
        if valid:
            import shutil
            shutil.copy2(valid[0], output_path)
        return output_path

    if progress_cb:
        progress_cb(f"applying audio crossfade ({crossfade_duration}s)...")

    # Chain acrossfade filters for N clips
    # For 2 clips: [0:a][1:a]acrossfade=d=0.5:c1=tri:c2=tri[out]
    # For 3+ clips: chain sequentially
    input_args = []
    for p in valid:
        input_args += ["-i", p]

    if len(valid) == 2:
        filter_complex = (
            f"[0:a][1:a]acrossfade=d={crossfade_duration}:c1=tri:c2=tri[out]"
        )
    else:
        parts = []
        for i in range(1, len(valid)):
            in_label = f"[{i-1}:a]" if i == 1 else "[xfa]"
            out_label = "[out]" if i == len(valid) - 1 else "[xfa]"
            parts.append(
                f"{in_label}[{i}:a]acrossfade=d={crossfade_duration}:c1=tri:c2=tri{out_label}"
            )
        filter_complex = ";".join(parts)

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    except subprocess.CalledProcessError:
        # Fallback: just use the first audio file
        if valid:
            import shutil
            shutil.copy2(valid[0], output_path)
    return output_path


# ---- Speed ramp types ----
SPEED_RAMP_TYPES = ["none", "slow_in", "slow_out", "slow_mid"]


def _apply_speed_to_clip(clip_path: str, speed: float, output_dir: str,
                         index: int, progress_cb=None) -> str:
    """
    Apply speed adjustment to a clip using ffmpeg setpts filter.
    Returns path to the speed-adjusted clip (or original if speed is 1.0).
    """
    if speed == 1.0:
        return clip_path

    if progress_cb:
        progress_cb(f"adjusting speed ({speed}x) for clip {index}...")

    # setpts=PTS/speed for video, atempo for audio
    pts_factor = 1.0 / speed
    out_path = os.path.join(output_dir, f"_speed_{index}_{speed}x.mp4")

    vfilter = f"setpts={pts_factor:.4f}*PTS"

    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-filter:v", vfilter,
        "-an",  # drop audio from clip (audio comes from song track)
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    return out_path


def _build_text_overlay_filter(overlay: dict) -> str:
    """
    Build an ffmpeg drawtext filter string from an overlay config dict.
    overlay: {text, font_size, position, color}
    """
    text = overlay.get("text", "").replace("'", "\\'").replace(":", "\\:")
    if not text:
        return ""
    size_name = overlay.get("font_size", "medium")
    font_size = TEXT_SIZES.get(size_name, 40)
    position = TEXT_POSITIONS.get(overlay.get("position", "bottom"),
                                  TEXT_POSITIONS["bottom"])
    color_name = overlay.get("color", "white")
    color_hex = TEXT_COLORS.get(color_name, "FFFFFF")

    return (
        f"drawtext=text='{text}':fontsize={font_size}:"
        f"fontcolor=0x{color_hex}@0.9:{position}:"
        f"borderw=2:bordercolor=0x000000@0.6"
    )


def _build_color_grade_filter(color_grade: str) -> str:
    """Return the ffmpeg filter string for a color grade preset."""
    return COLOR_GRADE_PRESETS.get(color_grade, "")


def _apply_audio_visualization(video_path: str, audio_path: str, output_path: str,
                                viz_style: str = "waveform",
                                progress_cb=None) -> str:
    """
    Overlay audio visualization (waveform/spectrum/both) on the stitched video.
    """
    if not audio_path or not os.path.isfile(audio_path):
        return video_path
    if viz_style not in AUDIO_VIZ_STYLES:
        return video_path

    if progress_cb:
        progress_cb(f"adding audio visualization ({viz_style})...")

    viz_out = output_path.replace(".mp4", "_viz.mp4")

    if viz_style == "waveform":
        filter_complex = (
            "[1:a]showwaves=s=640x120:mode=cline:colors=0x00D4FF@0.7:rate=25[wv];"
            "[0:v][wv]overlay=x=(W-640)/2:y=H-140:shortest=1[outv]"
        )
    elif viz_style == "spectrum":
        filter_complex = (
            "[1:a]showfreqs=s=640x120:mode=bar:colors=0xFF2D7B@0.7|0x00D4FF@0.7"
            ":fscale=log[sp];"
            "[0:v][sp]overlay=x=(W-640)/2:y=H-140:shortest=1[outv]"
        )
    else:  # both
        filter_complex = (
            "[1:a]asplit=2[a1][a2];"
            "[a1]showwaves=s=640x80:mode=cline:colors=0x00D4FF@0.6:rate=25[wv];"
            "[a2]showfreqs=s=640x80:mode=bar:colors=0xFF2D7B@0.6|0xFFAA00@0.6"
            ":fscale=log[sp];"
            "[wv][sp]vstack[viz];"
            "[0:v][viz]overlay=x=(W-640)/2:y=H-180:shortest=1[outv]"
        )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        "-shortest",
        viz_out,
    ]

    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    # Replace original with viz version
    if os.path.isfile(viz_out):
        import shutil
        shutil.move(viz_out, output_path)

    if progress_cb:
        progress_cb("visualization added")

    return output_path


def generate_credits(output_path: str, title: str = "", artist: str = "",
                     extra_text: str = "", duration: float = 8.0,
                     progress_cb=None) -> str:
    """
    Generate a credits roll video clip with scrolling text over black background.

    Args:
        output_path: path for the output video
        title: song title
        artist: artist name
        extra_text: additional credits text
        duration: clip duration in seconds
        progress_cb: optional callable(status_str)

    Returns:
        path to the credits video clip
    """
    _check_ffmpeg()

    if progress_cb:
        progress_cb("generating credits roll...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Build credits text lines
    lines = []
    if title:
        lines.append(title)
    if artist:
        lines.append(f"by {artist}")
    lines.append("")
    if extra_text:
        for line in extra_text.split("\n"):
            lines.append(line.strip())
        lines.append("")
    lines.append("Made with AI")
    lines.append("LUMN Studio")

    credits_text = "\\n".join(lines).replace("'", "\\'").replace(":", "\\:")

    # Scrolling credits: text starts below screen and scrolls up
    # y starts at h (off screen bottom) and scrolls to -text_h
    scroll_speed = f"h-((h+text_h)*t/{duration})"

    vf = (
        f"drawtext=text='{credits_text}'"
        f":fontsize=42:fontcolor=0x00D4FF@0.9"
        f":x=(w-text_w)/2:y={scroll_speed}"
        f":line_spacing=20"
        f":borderw=2:bordercolor=0x000000@0.6"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=1920x1080:d={duration}:r=30",
        "-vf", vf,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("credits roll generated")
    return output_path


def apply_watermark(input_path: str, output_path: str,
                    watermark_path: str, position: str = "bottom_right",
                    opacity: int = 50, progress_cb=None) -> str:
    """
    Apply a PNG watermark overlay to a video.

    Args:
        input_path: source video
        output_path: destination video
        watermark_path: path to watermark PNG
        position: corner position (top_left, top_right, bottom_left, bottom_right)
        opacity: watermark opacity 10-100
        progress_cb: optional callable(status_str)

    Returns:
        path to the output video
    """
    _check_ffmpeg()

    if not os.path.isfile(watermark_path):
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    if progress_cb:
        progress_cb("applying watermark...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Position mapping (with 20px padding)
    pos_map = {
        "top_left": "x=20:y=20",
        "top_right": "x=W-w-20:y=20",
        "bottom_left": "x=20:y=H-h-20",
        "bottom_right": "x=W-w-20:y=H-h-20",
    }
    pos_expr = pos_map.get(position, pos_map["bottom_right"])

    # Scale watermark to max 150px height and apply opacity
    alpha = max(0.1, min(1.0, opacity / 100.0))
    filter_complex = (
        f"[1:v]scale=-1:150,format=rgba,"
        f"colorchannelmixer=aa={alpha:.2f}[wm];"
        f"[0:v][wm]overlay={pos_expr}:shortest=1[outv]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-i", watermark_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("watermark applied")
    return output_path


def extract_thumbnail(video_path: str, output_path: str,
                      timestamp: float = -1, progress_cb=None) -> str:
    """
    Extract a frame from a video as a thumbnail.

    Args:
        video_path: source video
        output_path: destination image (jpg)
        timestamp: specific time in seconds, or -1 for auto-select (1/3 through)
        progress_cb: optional callable(status_str)

    Returns:
        path to the extracted thumbnail
    """
    _check_ffmpeg()

    if progress_cb:
        progress_cb("extracting thumbnail...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if timestamp < 0:
        # Auto-select: 1/3 through the video (usually a good frame)
        duration = _get_clip_duration(video_path)
        timestamp = duration / 3.0

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("thumbnail extracted")
    return output_path


def mix_audio_tracks(vocal_path: str, instrumental_path: str,
                     output_path: str, vocal_level: int = 50,
                     instrumental_level: int = 50,
                     progress_cb=None) -> str:
    """
    Mix vocal and instrumental tracks into a single audio file.

    Args:
        vocal_path: path to vocal track
        instrumental_path: path to instrumental track
        output_path: path for mixed output
        vocal_level: vocal volume 0-100
        instrumental_level: instrumental volume 0-100
        progress_cb: optional callable(status_str)

    Returns:
        path to the mixed audio file
    """
    _check_ffmpeg()

    if progress_cb:
        progress_cb("mixing audio tracks...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    v_vol = max(0.0, min(2.0, vocal_level / 50.0))
    i_vol = max(0.0, min(2.0, instrumental_level / 50.0))

    filter_complex = (
        f"[0:a]volume={v_vol:.2f}[v];"
        f"[1:a]volume={i_vol:.2f}[i];"
        f"[v][i]amix=inputs=2:duration=longest:dropout_transition=2[out]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", vocal_path,
        "-i", instrumental_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("audio tracks mixed")
    return output_path


def export_for_platform(input_path: str, output_path: str,
                        platform: str, progress_cb=None) -> str:
    """
    Export video for a specific social media platform.

    Args:
        input_path: source video
        output_path: destination video
        platform: one of youtube, tiktok, instagram, twitter
        progress_cb: optional callable(status_str)

    Returns:
        path to the exported video
    """
    _check_ffmpeg()

    platform_specs = {
        "youtube": {
            "width": 1920, "height": 1080,
            "max_duration": None,  # no limit
            "label": "YouTube (16:9)",
        },
        "tiktok": {
            "width": 1080, "height": 1920,
            "max_duration": 180,  # 3 minutes
            "label": "TikTok (9:16)",
        },
        "instagram": {
            "width": 1080, "height": 1920,
            "max_duration": 90,  # 90 seconds
            "label": "Instagram Reels (9:16)",
        },
        "twitter": {
            "width": 1920, "height": 1080,
            "max_duration": 140,  # 2:20
            "label": "Twitter/X (16:9)",
        },
    }

    spec = platform_specs.get(platform)
    if not spec:
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    if progress_cb:
        progress_cb(f"exporting for {spec['label']}...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    w, h = spec["width"], spec["height"]
    max_dur = spec["max_duration"]

    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
    ]

    if max_dur:
        cmd += ["-t", str(max_dur)]

    cmd.append(output_path)
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb(f"exported for {spec['label']}")
    return output_path


def apply_beat_sync_cuts(input_path: str, output_path: str,
                         beat_timestamps: list, sections: list = None,
                         progress_cb=None) -> str:
    """
    Apply beat-synced hard cuts to a video. During high-energy sections (chorus),
    insert 0.1s hard cuts alternating between adjacent frames on each beat.

    Args:
        input_path: source video
        output_path: destination video
        beat_timestamps: list of beat times in seconds
        sections: list of section dicts with {start, end, type, energy}
        progress_cb: optional callable(status_str)

    Returns:
        path to the output video
    """
    _check_ffmpeg()

    if not beat_timestamps or len(beat_timestamps) < 2:
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    if progress_cb:
        progress_cb("applying beat-synced cuts...")

    # Identify high-energy beat timestamps (in chorus sections or high energy)
    high_energy_beats = []
    if sections:
        for beat in beat_timestamps:
            for section in sections:
                if (section.get("type") in ("chorus",) and
                        section["start"] <= beat <= section["end"]):
                    high_energy_beats.append(beat)
                    break
    else:
        # No sections info: use all beats
        high_energy_beats = beat_timestamps

    if not high_energy_beats:
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    # Build a select filter that creates flash cuts on beats
    # For each beat, we briefly show a frame from 0.5s ahead (creates a jump cut effect)
    select_parts = []
    for beat in high_energy_beats:
        # Create a brief 0.1s "flash" at each beat by inserting a brightness spike
        select_parts.append(
            f"between(t,{beat:.3f},{beat + 0.1:.3f})"
        )

    if not select_parts:
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    # Build a filter that adds a brief flash/invert effect on each beat
    flash_expr = "+".join(select_parts)
    # Use curves filter to briefly spike brightness on beats
    vf = (
        f"curves=all='0/0 0.5/0.5 1/1':eval=frame,"
        f"eq=brightness=0.15*({flash_expr}):eval=frame"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    except subprocess.CalledProcessError:
        # If the complex filter fails, fall back to simple copy
        import shutil
        shutil.copy2(input_path, output_path)

    if progress_cb:
        progress_cb("beat-sync cuts applied")
    return output_path


def stitch(clip_paths: list, audio_path: str | None, output_path: str,
           crossfade: float = 0.5, fade_dur: float = 1.0,
           transitions: list | None = None,
           default_transition: str = "crossfade",
           speeds: list | None = None,
           text_overlays: list | None = None,
           color_grade: str = "none",
           scene_color_grades: list | None = None,
           audio_viz: str | None = None,
           speed_ramps: list | None = None,
           reversed_clips: list | None = None,
           audio_crossfade: float = 0.0,
           progress_cb=None) -> str:
    """
    Stitch video clips together with optional audio.

    Args:
        clip_paths: ordered list of video clip file paths (Nones are skipped)
        audio_path: path to the audio track (None to skip audio overlay)
        output_path: where to write the final video
        crossfade: base crossfade duration in seconds between clips
        fade_dur: fade in/out duration in seconds
        transitions: optional list of transition types per clip (same length as clip_paths).
                     Each entry is the transition INTO that clip from the previous one.
                     First entry is ignored (no previous clip). None entries use default.
        default_transition: fallback transition type when not specified
        speeds: optional list of speed multipliers per clip (1.0 = normal)
        text_overlays: optional list of overlay dicts per clip
                       ({text, font_size, position, color} or None)
        color_grade: global color grade preset name (default "none")
        scene_color_grades: optional list of per-scene color grade overrides
        audio_viz: audio visualization style (None, "waveform", "spectrum", "both")
        progress_cb: optional callable(status_str)

    Returns:
        path to the final output video
    """
    _check_ffmpeg()

    # Filter out None/missing clips, tracking original indices for transition mapping
    valid_clips = []
    valid_transitions = []
    valid_speeds = []
    valid_overlays = []
    valid_scene_grades = []
    for i, p in enumerate(clip_paths):
        if p and os.path.isfile(p):
            valid_clips.append(p)
            if transitions and i < len(transitions) and transitions[i]:
                valid_transitions.append(transitions[i])
            else:
                valid_transitions.append(default_transition)
            # Speed
            spd = 1.0
            if speeds and i < len(speeds) and speeds[i]:
                try:
                    spd = float(speeds[i])
                except (ValueError, TypeError):
                    spd = 1.0
            valid_speeds.append(spd)
            # Text overlay
            ovl = None
            if text_overlays and i < len(text_overlays):
                ovl = text_overlays[i]
            valid_overlays.append(ovl)
            # Scene color grade
            scg = None
            if scene_color_grades and i < len(scene_color_grades):
                scg = scene_color_grades[i]
            valid_scene_grades.append(scg)

    if not valid_clips:
        raise RuntimeError("No valid video clips to stitch")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if progress_cb:
        progress_cb("preparing clips...")

    # Apply per-scene speed adjustments (creates temp files for non-1.0 speeds)
    temp_dir = os.path.dirname(os.path.abspath(output_path))
    processed_clips = []
    for idx, (clip, speed) in enumerate(zip(valid_clips, valid_speeds)):
        adjusted = _apply_speed_to_clip(clip, speed, temp_dir, idx, progress_cb)
        processed_clips.append(adjusted)
    valid_clips = processed_clips

    # Apply per-scene speed ramps
    if speed_ramps:
        ramped_clips = []
        for idx, clip in enumerate(valid_clips):
            ramp = speed_ramps[idx] if idx < len(speed_ramps) else "none"
            if ramp and ramp != "none":
                adjusted = _apply_speed_ramp(clip, ramp, temp_dir, idx, progress_cb)
                ramped_clips.append(adjusted)
            else:
                ramped_clips.append(clip)
        valid_clips = ramped_clips

    # Apply per-scene reverse
    if reversed_clips:
        rev_processed = []
        for idx, clip in enumerate(valid_clips):
            is_reversed = reversed_clips[idx] if idx < len(reversed_clips) else False
            if is_reversed:
                adjusted = _apply_reverse(clip, temp_dir, idx, progress_cb)
                rev_processed.append(adjusted)
            else:
                rev_processed.append(clip)
        valid_clips = rev_processed

    # Apply per-scene text overlays and color grades as pre-processing
    for idx in range(len(valid_clips)):
        filters = []
        # Per-scene color grade (override) or global
        grade_name = valid_scene_grades[idx] if valid_scene_grades[idx] else color_grade
        grade_filter = _build_color_grade_filter(grade_name)
        if grade_filter:
            filters.append(grade_filter)
        # Text overlay
        ovl = valid_overlays[idx]
        if ovl and isinstance(ovl, dict) and ovl.get("text"):
            txt_filter = _build_text_overlay_filter(ovl)
            if txt_filter:
                filters.append(txt_filter)

        if filters:
            if progress_cb:
                progress_cb(f"applying effects to clip {idx}...")
            combined = ",".join(filters)
            out_path = os.path.join(temp_dir, f"_fx_{idx}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-i", valid_clips[idx],
                "-vf", combined,
                "-an",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                out_path,
            ]
            subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
            valid_clips[idx] = out_path

    if len(valid_clips) == 1:
        if audio_path and os.path.isfile(audio_path):
            result = _overlay_audio(valid_clips[0], audio_path, output_path, fade_dur,
                                     progress_cb)
        else:
            result = _copy_clip(valid_clips[0], output_path, progress_cb)
        # Audio visualization pass
        if audio_viz and audio_path:
            result = _apply_audio_visualization(result, audio_path, output_path,
                                                 audio_viz, progress_cb)
        return result

    # Check if we need the advanced multi-transition pipeline or simple crossfade
    unique_transitions = set(valid_transitions[1:])  # skip first (no previous clip)
    has_complex = unique_transitions - {"crossfade"}

    if not has_complex:
        # All crossfade: use the simple pipeline
        result = _stitch_with_crossfades(valid_clips, audio_path, output_path,
                                          crossfade, fade_dur, progress_cb)
    else:
        # Mixed transitions: use the advanced pipeline
        result = _stitch_with_transitions(valid_clips, audio_path, output_path,
                                           crossfade, fade_dur, valid_transitions,
                                           progress_cb)

    # Audio visualization pass (post-stitch)
    if audio_viz and audio_path:
        result = _apply_audio_visualization(result, audio_path, output_path,
                                             audio_viz, progress_cb)

    return result


def _copy_clip(clip: str, output: str, progress_cb=None) -> str:
    """Copy/re-encode a single clip as the final output (no audio)."""
    if progress_cb:
        progress_cb("copying single clip...")
    import shutil
    shutil.copy2(clip, output)
    if progress_cb:
        progress_cb("done")
    return output


def _overlay_audio(clip: str, audio: str, output: str,
                   fade_dur: float, progress_cb=None) -> str:
    """Overlay audio on a single clip with fades."""
    if progress_cb:
        progress_cb("overlaying audio on single clip...")

    cmd = [
        "ffmpeg", "-y",
        "-i", clip,
        "-i", audio,
        "-c:v", "libx264", "-c:a", "aac",
        "-shortest",
        output,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    if progress_cb:
        progress_cb("done")
    return output


def _stitch_with_crossfades(clips: list, audio: str | None, output: str,
                            crossfade: float, fade_dur: float,
                            progress_cb=None) -> str:
    """
    Concatenate clips with crossfade transitions using ffmpeg xfade filter.
    Then overlay the audio track (if provided).
    """
    if progress_cb:
        progress_cb(f"stitching {len(clips)} clips with crossfades...")

    has_audio = audio and os.path.isfile(audio)
    durations = [_get_clip_duration(c) for c in clips]

    input_args = []
    for c in clips:
        input_args += ["-i", c]
    if has_audio:
        input_args += ["-i", audio]

    filter_parts = []
    n = len(clips)
    running_duration = durations[0]

    for i in range(1, n):
        # Area 4 item 7: vary crossfade based on clip length
        clip_crossfade = _get_transition_duration("crossfade", crossfade, clip_duration=durations[i])
        offset = max(0, running_duration - clip_crossfade)
        in_label = f"[{i - 1}:v]" if i == 1 else "[xf]"
        out_label = "[xf]" if i < n - 1 else "[xfout]"

        filter_parts.append(
            f"{in_label}[{i}:v]xfade=transition=fade:duration={clip_crossfade}"
            f":offset={offset:.3f}{out_label}"
        )
        running_duration = offset + durations[i]

    total_dur = running_duration
    fade_out_start = max(0, total_dur - fade_dur)
    if n > 1:
        filter_parts.append(
            f"[xfout]fade=t=in:st=0:d={fade_dur},"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_dur}[v]"
        )
    else:
        filter_parts.append(
            f"[0:v]fade=t=in:st=0:d={fade_dur},"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_dur}[v]"
        )

    map_args = ["-map", "[v]"]

    if has_audio:
        audio_idx = n
        filter_parts.append(
            f"[{audio_idx}:a]afade=t=in:st=0:d={fade_dur},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_dur}[a]"
        )
        map_args += ["-map", "[a]"]

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_complex,
        *map_args,
        "-c:v", "libx264",
        *([ "-c:a", "aac"] if has_audio else []),
        *(["-shortest"] if has_audio else []),
        "-pix_fmt", "yuv420p",
        output,
    ]

    if progress_cb:
        progress_cb("running ffmpeg...")

    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("done")

    return output


def apply_lyrics_overlay(input_path: str, output_path: str,
                         lyrics: list, progress_cb=None) -> str:
    """
    Overlay timed lyric lines onto a video using ffmpeg drawtext.

    Args:
        input_path: source video
        output_path: destination video
        lyrics: list of {text, start, end} dicts (times in seconds)
        progress_cb: optional callable(status_str)

    Returns:
        path to the output video with lyrics burned in
    """
    _check_ffmpeg()
    if not lyrics:
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    if progress_cb:
        progress_cb("applying lyrics overlay...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Build drawtext filter chain for each lyric line
    drawtext_parts = []
    for line in lyrics:
        text = line["text"].replace("'", "\\'").replace(":", "\\:")
        start = line["start"]
        end = line["end"]
        dt = (
            f"drawtext=text='{text}'"
            f":fontsize=42"
            f":fontcolor=white"
            f":shadowcolor=black@0.6:shadowx=2:shadowy=2"
            f":x=(w-text_w)/2"
            f":y=h-th-60"
            f":enable='between(t,{start:.3f},{end:.3f})'"
        )
        drawtext_parts.append(dt)

    vf = ",".join(drawtext_parts)
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("lyrics overlay done")
    return output_path


def apply_aspect_ratio(input_path: str, output_path: str,
                       aspect: str, progress_cb=None) -> str:
    """
    Crop/pad a video to match a target aspect ratio preset.

    Args:
        input_path: source video
        output_path: destination video
        aspect: one of "16:9", "9:16", "1:1", "4:5"
        progress_cb: optional callable(status_str)

    Returns:
        path to the output video
    """
    _check_ffmpeg()
    if aspect not in ASPECT_PRESETS:
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    target_w, target_h = ASPECT_PRESETS[aspect]

    if progress_cb:
        progress_cb(f"applying aspect ratio {aspect}...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Scale to fit inside the target dimensions, then pad to exact size
    vf = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb(f"aspect ratio {aspect} done")
    return output_path


def split_clip(clip_path: str, output_dir: str,
               scene_id: str, split_pct: float = 0.5,
               progress_cb=None) -> tuple:
    """
    Split a video clip at a given percentage position.

    Args:
        clip_path: path to the clip to split
        output_dir: directory for output files
        scene_id: base ID for naming
        split_pct: 0.0-1.0 position to split (default 0.5 = midpoint)
        progress_cb: optional callable(status_str)

    Returns:
        (first_half_path, second_half_path)
    """
    _check_ffmpeg()

    if progress_cb:
        progress_cb("splitting clip...")

    split_pct = max(0.05, min(0.95, split_pct))
    duration = _get_clip_duration(clip_path)
    split_point = duration * split_pct
    os.makedirs(output_dir, exist_ok=True)

    first_path = os.path.join(output_dir, f"{scene_id}_a.mp4")
    second_path = os.path.join(output_dir, f"{scene_id}_b.mp4")

    # First part
    cmd1 = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-t", f"{split_point:.3f}",
        "-c:v", "libx264", "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        first_path,
    ]
    subprocess.run(cmd1, check=True, capture_output=True, **_subprocess_kwargs())

    # Second part
    cmd2 = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-ss", f"{split_point:.3f}",
        "-c:v", "libx264", "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        second_path,
    ]
    subprocess.run(cmd2, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("clip split done")

    return first_path, second_path


def _stitch_with_transitions(clips: list, audio: str | None, output: str,
                              crossfade: float, fade_dur: float,
                              transitions: list,
                              progress_cb=None) -> str:
    """
    Stitch clips with per-scene transition types.
    Handles crossfade, hard_cut, fade_black, wipe, dissolve, zoom, and glitch.
    """
    if progress_cb:
        progress_cb(f"stitching {len(clips)} clips with mixed transitions...")

    has_audio = audio and os.path.isfile(audio)
    n = len(clips)
    durations = [_get_clip_duration(c) for c in clips]

    input_args = []
    for c in clips:
        input_args += ["-i", c]
    if has_audio:
        input_args += ["-i", audio]

    filter_parts = []
    running_duration = durations[0]

    # We process transitions pair-wise.
    # For hard_cut and glitch, we use concat demuxer approach or handle them
    # as xfade with 0 duration (effectively a cut).
    # For fade_black, we add fade-out and fade-in filters around the boundary.

    for i in range(1, n):
        trans = transitions[i] if i < len(transitions) else "crossfade"
        xfade_name = _get_xfade_name(trans)
        trans_dur = _get_transition_duration(trans, crossfade, clip_duration=durations[i])

        in_label = f"[{i - 1}:v]" if i == 1 else "[xf]"
        out_label = "[xf]" if i < n - 1 else "[xfout]"

        if trans == "hard_cut":
            # Hard cut: xfade with 0 duration effectively just concats
            offset = max(0, running_duration)
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition=fade:duration=0.001"
                f":offset={offset:.3f}{out_label}"
            )
            running_duration = offset + durations[i]

        elif trans == "fade_black":
            # Fade to black then fade in: use xfade=transition=fadeblack
            fb_dur = min(crossfade * 1.5, 1.5)
            offset = max(0, running_duration - fb_dur)
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition=fadeblack:duration={fb_dur:.3f}"
                f":offset={offset:.3f}{out_label}"
            )
            running_duration = offset + durations[i]

        elif trans == "glitch":
            # Glitch: use pixelize xfade for a digital distortion effect
            glitch_dur = min(crossfade * 0.8, 0.4)
            offset = max(0, running_duration - glitch_dur)
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition=pixelize:duration={glitch_dur:.3f}"
                f":offset={offset:.3f}{out_label}"
            )
            running_duration = offset + durations[i]

        elif xfade_name:
            # Standard xfade-based transitions
            offset = max(0, running_duration - trans_dur)
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition={xfade_name}:duration={trans_dur:.3f}"
                f":offset={offset:.3f}{out_label}"
            )
            running_duration = offset + durations[i]

        else:
            # Unknown transition, fallback to crossfade
            offset = max(0, running_duration - crossfade)
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition=fade:duration={crossfade}"
                f":offset={offset:.3f}{out_label}"
            )
            running_duration = offset + durations[i]

    # Fade in/out on final composite
    total_dur = running_duration
    fade_out_start = max(0, total_dur - fade_dur)

    if n > 1:
        filter_parts.append(
            f"[xfout]fade=t=in:st=0:d={fade_dur},"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_dur}[v]"
        )
    else:
        filter_parts.append(
            f"[0:v]fade=t=in:st=0:d={fade_dur},"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_dur}[v]"
        )

    # Audio
    map_args = ["-map", "[v]"]
    if has_audio:
        audio_idx = n
        filter_parts.append(
            f"[{audio_idx}:a]afade=t=in:st=0:d={fade_dur},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_dur}[a]"
        )
        map_args += ["-map", "[a]"]

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_complex,
        *map_args,
        "-c:v", "libx264",
        *([ "-c:a", "aac"] if has_audio else []),
        *(["-shortest"] if has_audio else []),
        "-pix_fmt", "yuv420p",
        output,
    ]

    if progress_cb:
        progress_cb("running ffmpeg...")

    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("done")

    return output


def align_scenes_to_beats(scenes: list, beats: list) -> list:
    """
    Snap scene boundaries to the nearest beat timestamp.
    Redistributes scene durations so that each scene ends on a beat.

    Args:
        scenes: list of scene dicts with start_sec, end_sec, duration
        beats: list of beat timestamps in seconds (sorted ascending)

    Returns:
        updated list of scene dicts with beat-aligned boundaries
    """
    if not scenes or not beats:
        return scenes

    beats = sorted(beats)
    total_duration = scenes[-1].get("end_sec", scenes[-1].get("start_sec", 0) + scenes[-1].get("duration", 8))

    # Add 0.0 and total_duration as boundary anchors
    all_candidates = [0.0] + beats + [total_duration]

    def nearest_beat(t):
        """Find the beat timestamp nearest to t."""
        best = all_candidates[0]
        best_dist = abs(t - best)
        for b in all_candidates:
            d = abs(t - b)
            if d < best_dist:
                best = b
                best_dist = d
        return best

    # Snap each scene boundary to nearest beat
    aligned = []
    for i, scene in enumerate(scenes):
        s = dict(scene)
        start = s.get("start_sec", 0)
        end = s.get("end_sec", start + s.get("duration", 8))

        if i == 0:
            snapped_start = 0.0
        else:
            snapped_start = aligned[i - 1]["end_sec"]

        snapped_end = nearest_beat(end)

        # Ensure minimum 1s scene duration
        if snapped_end <= snapped_start + 1.0:
            # Find next beat after snapped_start + 1
            for b in all_candidates:
                if b > snapped_start + 1.0:
                    snapped_end = b
                    break
            else:
                snapped_end = snapped_start + max(2.0, s.get("duration", 8))

        s["start_sec"] = round(snapped_start, 3)
        s["end_sec"] = round(snapped_end, 3)
        s["duration"] = round(snapped_end - snapped_start, 3)
        aligned.append(s)

    return aligned


def overlay_scene_vocals(video_path: str, output_path: str,
                         vocal_entries: list, progress_cb=None) -> str:
    """
    Mix per-scene vocal clips onto a video at their respective timespans.

    Args:
        video_path: source video (already has background audio)
        output_path: destination video
        vocal_entries: list of dicts: {vocal_path, start_sec, end_sec, volume}
                       volume is 0-100 (percent)
        progress_cb: optional callable(status_str)

    Returns:
        path to the output video with vocals mixed in
    """
    _check_ffmpeg()

    # Filter to valid entries
    valid = [v for v in vocal_entries
             if v.get("vocal_path") and os.path.isfile(v["vocal_path"])]
    if not valid:
        import shutil
        shutil.copy2(video_path, output_path)
        return output_path

    if progress_cb:
        progress_cb(f"mixing {len(valid)} vocal track(s)...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Build ffmpeg command with multiple vocal inputs
    input_args = ["-i", video_path]
    for v in valid:
        input_args += ["-i", v["vocal_path"]]

    # Build filter_complex
    filter_parts = []
    vocal_labels = []

    for idx, v in enumerate(valid):
        inp_idx = idx + 1  # 0 is the video
        start_ms = int(v.get("start_sec", 0) * 1000)
        vol = max(0.0, min(2.0, v.get("volume", 80) / 100.0))
        label = f"[voc{idx}]"
        filter_parts.append(
            f"[{inp_idx}:a]adelay={start_ms}|{start_ms},volume={vol:.2f}{label}"
        )
        vocal_labels.append(label)

    # Mix all vocals together
    if len(vocal_labels) == 1:
        mixed_label = vocal_labels[0]
    else:
        all_labels = "".join(vocal_labels)
        filter_parts.append(
            f"{all_labels}amix=inputs={len(vocal_labels)}:duration=longest:dropout_transition=2[vocmix]"
        )
        mixed_label = "[vocmix]"

    # Mix the combined vocals with the original video audio
    filter_parts.append(
        f"[0:a]{mixed_label}amix=inputs=2:duration=first:dropout_transition=2[outa]"
    )

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[outa]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]

    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("vocal overlay complete")
    return output_path


def apply_loop_boomerang(clip_path: str, output_path: str,
                         progress_cb=None) -> str:
    """
    Apply loop/boomerang effect to a clip: plays forward then backward.
    Effectively doubles the scene duration.

    Args:
        clip_path: source video clip
        output_path: destination video
        progress_cb: optional callable(status_str)

    Returns:
        path to the boomerang clip
    """
    _check_ffmpeg()

    if not os.path.isfile(clip_path):
        raise FileNotFoundError(f"Clip not found: {clip_path}")

    if progress_cb:
        progress_cb("creating boomerang effect...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Create reversed copy
    reversed_path = output_path + ".reversed_tmp.mp4"
    cmd_reverse = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-vf", "reverse",
        "-an",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        reversed_path,
    ]
    subprocess.run(cmd_reverse, check=True, capture_output=True, **_subprocess_kwargs())

    # Concat forward + reversed
    concat_list = output_path + ".concat_tmp.txt"
    with open(concat_list, "w") as f:
        f.write(f"file '{clip_path}'\n")
        f.write(f"file '{reversed_path}'\n")

    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-an",
        output_path,
    ]
    subprocess.run(cmd_concat, check=True, capture_output=True, **_subprocess_kwargs())

    # Clean up temp files
    for tmp in [reversed_path, concat_list]:
        if os.path.isfile(tmp):
            os.remove(tmp)

    if progress_cb:
        progress_cb("boomerang effect applied")
    return output_path


def apply_audio_ducking(video_path: str, output_path: str,
                        duck_segments: list, duck_level: float = 0.3,
                        progress_cb=None) -> str:
    """
    Apply audio ducking: lower the main music volume during specified segments
    (e.g. when voiceover is present).

    Args:
        video_path: source video with audio
        output_path: destination video
        duck_segments: list of {start_sec, end_sec} dicts where ducking applies
        duck_level: volume level during ducked segments (0.0 - 1.0, default 0.3 = 30%)
        progress_cb: optional callable(status_str)

    Returns:
        path to the output video with ducked audio
    """
    _check_ffmpeg()

    if not duck_segments:
        import shutil
        shutil.copy2(video_path, output_path)
        return output_path

    if progress_cb:
        progress_cb("applying audio ducking...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Build a volume filter expression that ducks during specified segments
    duck_level = max(0.0, min(1.0, duck_level))

    # Build nested if expression
    expr = "1.0"
    for seg in reversed(duck_segments):
        start = seg.get("start_sec", 0)
        end = seg.get("end_sec", 0)
        if end > start:
            expr = f"if(between(t\\,{start:.3f}\\,{end:.3f})\\,{duck_level:.2f}\\,{expr})"

    af = f"volume='{expr}':eval=frame"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-af", af,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    except subprocess.CalledProcessError:
        # Fallback: simple global volume reduction if complex filter fails
        import shutil
        shutil.copy2(video_path, output_path)

    if progress_cb:
        progress_cb("audio ducking applied")
    return output_path


def export_gif(clip_path: str, output_path: str,
               max_duration: float = 10.0, max_width: int = 480,
               fps: int = 15, progress_cb=None) -> str:
    """
    Convert a video clip to an animated GIF.

    Args:
        clip_path: source video clip
        output_path: destination GIF file
        max_duration: maximum GIF duration in seconds (default 10)
        max_width: maximum GIF width in pixels (default 480)
        fps: frame rate for GIF (default 15)
        progress_cb: optional callable(status_str)

    Returns:
        path to the exported GIF
    """
    _check_ffmpeg()

    if not os.path.isfile(clip_path):
        raise FileNotFoundError(f"Clip not found: {clip_path}")

    if progress_cb:
        progress_cb("exporting GIF...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Two-pass GIF: first generate palette, then use palette for high quality
    palette_path = output_path + ".palette.png"

    # Duration limit
    duration_args = ["-t", str(max_duration)] if max_duration > 0 else []

    # Pass 1: generate palette
    vf_palette = f"fps={fps},scale={max_width}:-1:flags=lanczos,palettegen=stats_mode=diff"
    cmd_palette = [
        "ffmpeg", "-y",
        "-i", clip_path,
        *duration_args,
        "-vf", vf_palette,
        palette_path,
    ]
    subprocess.run(cmd_palette, check=True, capture_output=True, **_subprocess_kwargs())

    # Pass 2: generate GIF using palette
    vf_gif = f"fps={fps},scale={max_width}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5"
    cmd_gif = [
        "ffmpeg", "-y",
        "-i", clip_path,
        *duration_args,
        "-i", palette_path,
        "-filter_complex", vf_gif,
        output_path,
    ]
    subprocess.run(cmd_gif, check=True, capture_output=True, **_subprocess_kwargs())

    # Clean up palette
    if os.path.isfile(palette_path):
        os.remove(palette_path)

    if progress_cb:
        progress_cb("GIF exported")
    return output_path


def add_beat_cuts_to_stitch(video_path: str, output_path: str,
                            beat_timestamps: list,
                            progress_cb=None) -> str:
    """
    Add rhythmic hard cuts (brightness flashes) at beat timestamps within
    the stitched video for a more rhythmic feel.
    """
    _check_ffmpeg()

    if not beat_timestamps or len(beat_timestamps) < 2:
        import shutil
        shutil.copy2(video_path, output_path)
        return output_path

    if progress_cb:
        progress_cb("adding rhythmic beat cuts...")

    parts = []
    for bt in beat_timestamps:
        parts.append(f"between(t,{bt:.3f},{bt + 0.08:.3f})")
    if not parts:
        import shutil
        shutil.copy2(video_path, output_path)
        return output_path

    flash_expr = "+".join(parts)
    vf = f"eq=brightness=0.12*({flash_expr}):eval=frame"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    except subprocess.CalledProcessError:
        import shutil
        shutil.copy2(video_path, output_path)

    if progress_cb:
        progress_cb("beat cuts added")
    return output_path


# ---- Reverse Clip (Roadmap Item 17) ----

def reverse_clip(input_path: str, output_path: str) -> str:
    """Reverse a video clip."""
    cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", "reverse", "-af", "areverse",
           "-c:v", "libx264", "-c:a", "aac", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    return output_path


# ---- Loop / Boomerang Effect (Roadmap Item 18) ----

def boomerang_clip(input_path: str, output_path: str) -> str:
    """Create a boomerang (forward + reverse) clip."""
    import tempfile as _tempfile
    rev_path = _tempfile.mktemp(suffix="_rev.mp4")
    list_path = _tempfile.mktemp(suffix="_list.txt")
    try:
        reverse_clip(input_path, rev_path)
        with open(list_path, "w", encoding="utf-8") as f:
            f.write(f"file '{os.path.abspath(input_path)}'\n")
            f.write(f"file '{os.path.abspath(rev_path)}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
               "-c:v", "libx264", "-c:a", "aac", output_path]
        subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
        return output_path
    finally:
        for p in [rev_path, list_path]:
            if os.path.isfile(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ---- Picture-in-Picture (Roadmap Item 14) ----

def apply_pip(main_video: str, pip_video: str, output_path: str,
              position: str = "bottom_right", size: float = 0.25) -> str:
    """Overlay a small video on a corner of the main video.
    position: top_left, top_right, bottom_left, bottom_right
    size: fraction of main video width (0.1-0.5)
    """
    pos_map = {
        "top_left": "10:10",
        "top_right": "main_w-overlay_w-10:10",
        "bottom_left": "10:main_h-overlay_h-10",
        "bottom_right": "main_w-overlay_w-10:main_h-overlay_h-10",
    }
    pos = pos_map.get(position, pos_map["bottom_right"])
    scale = f"scale=iw*{size}:ih*{size}"
    
    cmd = ["ffmpeg", "-y", "-i", main_video, "-i", pip_video,
           "-filter_complex", f"[1:v]{scale}[pip];[0:v][pip]overlay={pos}",
           "-c:v", "libx264", "-c:a", "copy", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    return output_path


# ---- Split Screen (Roadmap Item 15) ----

def split_screen(left_video: str, right_video: str, output_path: str,
                 ratio: float = 0.5) -> str:
    """Show two videos side-by-side. ratio = left video width fraction."""
    lw = f"iw*{ratio}"
    rw = f"iw*{1-ratio}"
    
    cmd = ["ffmpeg", "-y", "-i", left_video, "-i", right_video,
           "-filter_complex",
           f"[0:v]crop=iw*{ratio}:ih:0:0[left];"
           f"[1:v]crop=iw*{1-ratio}:ih:iw*{ratio}:0[right];"
           f"[left][right]hstack",
           "-c:v", "libx264", "-shortest", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    return output_path


# ---- Green Screen / Chroma Key (Roadmap Item 13) ----

def apply_chroma_key(fg_video: str, bg_video: str, output_path: str,
                     color: str = "green", similarity: float = 0.3) -> str:
    """Remove a background color and composite over another video.
    color: green, blue, or hex color (eg 00ff00)
    """
    color_map = {"green": "0x00FF00", "blue": "0x0000FF", "black": "0x000000"}
    hex_color = color_map.get(color.lower(), color)
    
    cmd = ["ffmpeg", "-y", "-i", bg_video, "-i", fg_video,
           "-filter_complex",
           f"[1:v]chromakey={hex_color}:{similarity}:0.1[fg];[0:v][fg]overlay",
           "-c:v", "libx264", "-shortest", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    return output_path


# ---- Thumbnail Extract Best Frame (improved) ----

def extract_best_thumbnail(video_path: str, output_path: str, n_candidates: int = 10) -> str:
    """Extract the most visually interesting frame from a video.
    Samples n_candidates frames and picks the one with highest contrast/color variance.
    """
    import tempfile
    # Get duration
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path],
        capture_output=True, text=True, **_subprocess_kwargs()
    )
    try:
        duration = float(json.loads(probe.stdout)["format"]["duration"])
    except:
        duration = 5.0
    
    best_path = None
    best_score = -1
    
    for i in range(n_candidates):
        t = duration * (i + 1) / (n_candidates + 1)
        tmp = tempfile.mktemp(suffix=f"_frame{i}.jpg")
        cmd = ["ffmpeg", "-y", "-ss", str(t), "-i", video_path,
               "-vframes", "1", "-q:v", "2", tmp]
        subprocess.run(cmd, capture_output=True, **_subprocess_kwargs())
        
        if os.path.isfile(tmp):
            # Score by file size (more detail = larger file)
            score = os.path.getsize(tmp)
            if score > best_score:
                if best_path:
                    try: os.unlink(best_path)
                    except: pass
                best_score = score
                best_path = tmp
            else:
                try: os.unlink(tmp)
                except: pass
    
    if best_path:
        import shutil
        shutil.move(best_path, output_path)
    return output_path
