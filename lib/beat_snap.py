"""F3 — snap scene/shot boundaries to nearest downbeat within tolerance.

Two-phase API so callers can preview before committing:
  * plan_beat_snap(clips, downbeats, tolerance_s)  → plan (pure function)
  * apply_beat_snap(plan, out_dir)                 → re-encoded trimmed clips

Promotes the earlier output/pipeline/snap_cuts_to_downbeat.py prototype into
a real library with ffmpeg-backed trimming so the stitcher doesn't have to.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _probe_duration(path: str) -> float:
    """Return video duration in seconds via ffprobe. 0.0 on failure."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore").strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def plan_beat_snap(
    clips: list[dict],
    downbeats: list[float],
    tolerance_s: float = 2.0,
    fps: int = 24,
) -> dict:
    """Compute per-clip snapped durations.

    `clips` is a list of {shot_id, source, duration} dicts. `duration` is the
    intended scene length (can differ from the source mp4 length — we respect
    the intended length as the anchor target).

    Returns a plan dict with per-clip snapped durations and the snap deltas,
    plus the cumulative timeline. Pure function; no I/O side effects.
    """
    snapped = []
    cursor = 0.0
    for i, clip in enumerate(clips):
        target_end = cursor + float(clip.get("duration") or 0.0)

        # Never snap the final clip — it would truncate the song tail.
        if i == len(clips) - 1:
            new_end = target_end
            chosen_db = None
        else:
            cands = [d for d in downbeats if abs(d - target_end) <= tolerance_s
                     and d > cursor + 0.25]
            chosen_db = min(cands, key=lambda d: abs(d - target_end)) if cands else None
            new_end = chosen_db if chosen_db is not None else target_end

        new_duration = max(0.25, new_end - cursor)
        new_frames = max(1, round(new_duration * fps))
        quantized = new_frames / fps

        snapped.append({
            "index": i,
            "shot_id": clip.get("shot_id"),
            "source": clip.get("source"),
            "original_duration": round(float(clip.get("duration") or 0.0), 6),
            "snapped_duration": round(quantized, 6),
            "frames": new_frames,
            "snapped_to_downbeat": chosen_db,
            "delta_s": round(quantized - float(clip.get("duration") or 0.0), 6),
            "cursor_in": round(cursor, 6),
            "cursor_out": round(cursor + quantized, 6),
        })
        cursor += quantized

    original_total = sum(float(c.get("duration") or 0.0) for c in clips)
    snapped_total = sum(c["snapped_duration"] for c in snapped)

    return {
        "fps": fps,
        "tolerance_s": tolerance_s,
        "downbeat_count": len(downbeats),
        "clip_count": len(clips),
        "original_total_s": round(original_total, 3),
        "snapped_total_s": round(snapped_total, 3),
        "delta_s": round(snapped_total - original_total, 3),
        "cuts_snapped": sum(1 for c in snapped if c["snapped_to_downbeat"] is not None),
        "cuts_unsnapped": sum(1 for c in snapped if c["snapped_to_downbeat"] is None and c["index"] != len(clips) - 1),
        "clips": snapped,
    }


def apply_beat_snap(plan: dict, out_dir: str) -> dict:
    """Re-encode each source clip to its snapped duration via ffmpeg.

    Freeze-extends (last frame loop) when snapped > source, stream-copies
    a trim when snapped < source. Returns {clips: [{shot_id, output_path,
    mode}], errors: [...]}
    """
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_clips = []
    errors = []

    for entry in plan["clips"]:
        src = entry.get("source") or ""
        if not src or not os.path.isfile(src):
            errors.append({"shot_id": entry.get("shot_id"), "error": "source missing", "source": src})
            continue

        target_s = float(entry["snapped_duration"])
        src_s = _probe_duration(src) or target_s
        out_name = f"{entry.get('shot_id') or entry['index']}_snap.mp4"
        out_path = os.path.join(out_dir, out_name)

        if target_s <= src_s + 0.05:
            # Simple trim.
            cmd = [
                "ffmpeg", "-y", "-i", src,
                "-t", f"{target_s:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                out_path,
            ]
            mode = "trim"
        else:
            # Freeze-extend the last frame to reach target duration.
            extra = target_s - src_s
            cmd = [
                "ffmpeg", "-y", "-i", src,
                "-filter_complex",
                f"[0:v]tpad=stop_mode=clone:stop_duration={extra:.3f}[v];"
                f"[0:a]apad=pad_dur={extra:.3f}[a]",
                "-map", "[v]", "-map", "[a]",
                "-t", f"{target_s:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                out_path,
            ]
            mode = "extend"

        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out_clips.append({"shot_id": entry.get("shot_id"), "output_path": out_path, "mode": mode, "duration_s": target_s})
        except subprocess.CalledProcessError as e:
            errors.append({"shot_id": entry.get("shot_id"), "error": f"ffmpeg failed: {e.returncode}", "source": src})

    return {"clips": out_clips, "errors": errors, "out_dir": out_dir}


def load_grid(path: str) -> list[float]:
    """Load music_grid.json and return downbeat list (seconds)."""
    with open(path, "r", encoding="utf-8") as f:
        grid = json.load(f)
    return list(grid.get("downbeats") or [])
