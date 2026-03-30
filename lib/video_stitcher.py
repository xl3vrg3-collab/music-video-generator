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
    "crossfade",    # xfade=transition=fade
    "hard_cut",     # instant concat, no filter
    "fade_black",   # fade out + black gap + fade in
    "wipe_left",    # xfade=transition=wipeleft
    "wipe_right",   # xfade=transition=wiperight
    "dissolve",     # xfade=transition=dissolve (longer duration)
    "zoom_in",      # xfade=transition=zoomin
    "glitch",       # rapid 0.1s alternating cuts
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
    }
    return mapping.get(transition)


def _get_transition_duration(transition: str, base_crossfade: float) -> float:
    """Get the xfade duration for a transition type."""
    if transition == "dissolve":
        return min(base_crossfade * 2.0, 2.0)  # slower dissolve
    if transition in ("hard_cut", "glitch", "fade_black"):
        return 0.0  # these don't use xfade
    return base_crossfade


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


def stitch(clip_paths: list, audio_path: str | None, output_path: str,
           crossfade: float = 0.5, fade_dur: float = 1.0,
           transitions: list | None = None,
           default_transition: str = "crossfade",
           speeds: list | None = None,
           text_overlays: list | None = None,
           color_grade: str = "none",
           scene_color_grades: list | None = None,
           audio_viz: str | None = None,
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
        offset = max(0, running_duration - crossfade)
        in_label = f"[{i - 1}:v]" if i == 1 else "[xf]"
        out_label = "[xf]" if i < n - 1 else "[xfout]"

        filter_parts.append(
            f"{in_label}[{i}:v]xfade=transition=fade:duration={crossfade}"
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
               scene_id: str, progress_cb=None) -> tuple:
    """
    Split a video clip into two halves at the midpoint.

    Args:
        clip_path: path to the clip to split
        output_dir: directory for output files
        scene_id: base ID for naming
        progress_cb: optional callable(status_str)

    Returns:
        (first_half_path, second_half_path)
    """
    _check_ffmpeg()

    if progress_cb:
        progress_cb("splitting clip...")

    duration = _get_clip_duration(clip_path)
    midpoint = duration / 2.0
    os.makedirs(output_dir, exist_ok=True)

    first_path = os.path.join(output_dir, f"{scene_id}_a.mp4")
    second_path = os.path.join(output_dir, f"{scene_id}_b.mp4")

    # First half
    cmd1 = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-t", f"{midpoint:.3f}",
        "-c:v", "libx264", "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        first_path,
    ]
    subprocess.run(cmd1, check=True, capture_output=True, **_subprocess_kwargs())

    # Second half
    cmd2 = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-ss", f"{midpoint:.3f}",
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
        trans_dur = _get_transition_duration(trans, crossfade)

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
