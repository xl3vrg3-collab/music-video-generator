"""
Video stitcher using ffmpeg.
Concatenates clips with configurable transitions (crossfade, hard_cut,
fade_black, wipe, dissolve, zoom, glitch), overlays the audio track,
and applies fade in/out.
"""

import os
import sys
import subprocess
import tempfile


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


def stitch(clip_paths: list, audio_path: str | None, output_path: str,
           crossfade: float = 0.5, fade_dur: float = 1.0,
           transitions: list | None = None,
           default_transition: str = "crossfade",
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
        progress_cb: optional callable(status_str)

    Returns:
        path to the final output video
    """
    _check_ffmpeg()

    # Filter out None/missing clips, tracking original indices for transition mapping
    valid_clips = []
    valid_transitions = []
    for i, p in enumerate(clip_paths):
        if p and os.path.isfile(p):
            valid_clips.append(p)
            if transitions and i < len(transitions) and transitions[i]:
                valid_transitions.append(transitions[i])
            else:
                valid_transitions.append(default_transition)

    if not valid_clips:
        raise RuntimeError("No valid video clips to stitch")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if progress_cb:
        progress_cb("preparing clips...")

    if len(valid_clips) == 1:
        if audio_path and os.path.isfile(audio_path):
            return _overlay_audio(valid_clips[0], audio_path, output_path, fade_dur,
                                  progress_cb)
        else:
            return _copy_clip(valid_clips[0], output_path, progress_cb)

    # Check if we need the advanced multi-transition pipeline or simple crossfade
    unique_transitions = set(valid_transitions[1:])  # skip first (no previous clip)
    has_complex = unique_transitions - {"crossfade"}

    if not has_complex:
        # All crossfade: use the simple pipeline
        return _stitch_with_crossfades(valid_clips, audio_path, output_path,
                                       crossfade, fade_dur, progress_cb)

    # Mixed transitions: use the advanced pipeline
    return _stitch_with_transitions(valid_clips, audio_path, output_path,
                                     crossfade, fade_dur, valid_transitions,
                                     progress_cb)


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
