"""
Video stitcher using ffmpeg.
Concatenates clips with crossfade transitions, overlays the audio track,
and applies fade in/out.
"""

import os
import sys
import subprocess
import tempfile


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


def stitch(clip_paths: list, audio_path: str, output_path: str,
           crossfade: float = 0.5, fade_dur: float = 1.0,
           progress_cb=None) -> str:
    """
    Stitch video clips together with audio.

    Args:
        clip_paths: ordered list of video clip file paths (Nones are skipped)
        audio_path: path to the audio track
        output_path: where to write the final video
        crossfade: crossfade duration in seconds between clips
        fade_dur: fade in/out duration in seconds
        progress_cb: optional callable(status_str)

    Returns:
        path to the final output video
    """
    _check_ffmpeg()

    # Filter out None/missing clips
    valid_clips = [p for p in clip_paths if p and os.path.isfile(p)]
    if not valid_clips:
        raise RuntimeError("No valid video clips to stitch")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if progress_cb:
        progress_cb("preparing clips...")

    if len(valid_clips) == 1:
        # Single clip - just overlay audio
        return _overlay_audio(valid_clips[0], audio_path, output_path, fade_dur,
                              progress_cb)

    # --- Multi-clip: build complex ffmpeg filter for crossfades ---
    return _stitch_with_crossfades(valid_clips, audio_path, output_path,
                                   crossfade, fade_dur, progress_cb)


def _overlay_audio(clip: str, audio: str, output: str,
                   fade_dur: float, progress_cb=None) -> str:
    """Overlay audio on a single clip with fades."""
    if progress_cb:
        progress_cb("overlaying audio on single clip...")

    cmd = [
        "ffmpeg", "-y",
        "-i", clip,
        "-i", audio,
        "-filter_complex", (
            f"[0:v]fade=t=in:st=0:d={fade_dur},"
            f"fade=t=out:st=end:d={fade_dur}[v];"
            f"[1:a]afade=t=in:st=0:d={fade_dur},"
            f"afade=t=out:st=end:d={fade_dur}[a]"
        ),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-c:a", "aac",
        "-shortest",
        output,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    if progress_cb:
        progress_cb("done")
    return output


def _stitch_with_crossfades(clips: list, audio: str, output: str,
                            crossfade: float, fade_dur: float,
                            progress_cb=None) -> str:
    """
    Concatenate clips with crossfade transitions using ffmpeg xfade filter.
    Then overlay the audio track.
    """
    if progress_cb:
        progress_cb(f"stitching {len(clips)} clips with crossfades...")

    # Get durations for offset calculations
    durations = [_get_clip_duration(c) for c in clips]

    # Build input args
    input_args = []
    for c in clips:
        input_args += ["-i", c]
    input_args += ["-i", audio]

    # Build xfade filter chain
    # xfade works pair-wise: first merge [0] and [1], then result with [2], etc.
    filter_parts = []
    n = len(clips)

    # Calculate offsets: each xfade starts at (accumulated duration - crossfade)
    # After each xfade, the combined duration shrinks by crossfade seconds
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

    # Add fade in/out to final video
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

    # Audio: use the audio input (last input index)
    audio_idx = n
    filter_parts.append(
        f"[{audio_idx}:a]afade=t=in:st=0:d={fade_dur},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_dur}[a]"
    )

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-c:a", "aac",
        "-shortest",
        "-pix_fmt", "yuv420p",
        output,
    ]

    if progress_cb:
        progress_cb("running ffmpeg...")

    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())

    if progress_cb:
        progress_cb("done")

    return output
