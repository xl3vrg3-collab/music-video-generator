"""Motion audit — post-Kling identity / emblem check sampled across the mp4.

The anchor auditor reviews a single still. Kling renders motion that can
drift identity (character rotates away, emblem smears to back of head, eyes
go missing mid-tumble). This library samples N frames across the rendered
mp4 and asks Opus to verify identity holds across motion.

Public API:
    audit_clip(path, spec=None) -> dict
    audit_clips_batch(clip_paths, spec=None) -> list[dict]

Return shape (per clip):
    {
      "shot_id": "<if provided>",
      "duration": 8.25,
      "severity": "pass"|"warn"|"fail"|"error",
      "any_missing_eyes": bool,
      "any_wrong_emblem": bool,
      "summary": "one-sentence clip summary",
      "frames": [
        {"t": 1.00, "eyes_visible": "yes|closed|occluded|missing|back_view|no_face",
         "emblem_location": "forehead|back_of_head|above_head|shoulder|absent|not_applicable",
         "notes": "..."},
        ...
      ],
      "frame_paths": ["..."]
    }

The `spec` dict lets callers customize the identity rules. Defaults are
tuned for TB but accept per-project overrides.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Iterable

from lib.claude_client import call_opus_vision_json


DEFAULT_SPEC = {
    "character_name": "the subject",
    "character_summary": "a chibi-style anime bear named TB (Trillion Bear). Dark fur, round ears, red-orange glowing eyes, crescent-moon emblem on forehead ONLY.",
    "identity_mark": "crescent-moon emblem on the CENTER OF THE FOREHEAD, between/above the eyes",
    "identity_mark_forbidden_locations": ["back of head", "above head (floating)", "shoulder", "body", "hood"],
    "eye_rule": "Two large anime eyes on the front of the face.",
}


def _build_prompt(spec: dict, sample_times: list[float]) -> str:
    mark = spec.get("identity_mark", DEFAULT_SPEC["identity_mark"])
    forbid = ", ".join(spec.get("identity_mark_forbidden_locations", DEFAULT_SPEC["identity_mark_forbidden_locations"]))
    summary = spec.get("character_summary", DEFAULT_SPEC["character_summary"])
    eye_rule = spec.get("eye_rule", DEFAULT_SPEC["eye_rule"])
    times_s = ", ".join(f"{t:.2f}" for t in sample_times)
    return (
        f"You are auditing {len(sample_times)} frames sampled from a single "
        f"rendered video clip.\n\nCharacter: {summary}\n"
        f"Identity rules:\n"
        f"  - Identity mark: {mark}. NEVER on {forbid}.\n"
        f"  - {eye_rule}\n\n"
        "For each frame report:\n"
        '  * eyes_visible: "yes" | "closed" | "occluded" | "missing" | '
        '"back_view" | "no_face"\n'
        '  * emblem_location: "forehead" | "back_of_head" | "above_head" | '
        '"shoulder" | "absent" | "not_applicable"\n'
        "  * notes: one short sentence of what you see\n\n"
        "Then a clip-level verdict:\n"
        "  * any_missing_eyes = true if any frame has eyes_visible=\"missing\"\n"
        "  * any_wrong_emblem = true if any frame has emblem_location in "
        '["back_of_head","above_head","shoulder"]\n'
        "  * severity = \"fail\" if any_missing_eyes OR any_wrong_emblem, "
        "else \"warn\" if emblem is absent while face is clearly forward, else \"pass\".\n\n"
        "Return STRICT JSON only:\n"
        "{\n"
        '  "frames": [{"t": <seconds>, "eyes_visible": "...", '
        '"emblem_location": "...", "notes": "..."}, ...],\n'
        '  "any_missing_eyes": true|false,\n'
        '  "any_wrong_emblem": true|false,\n'
        '  "severity": "pass"|"warn"|"fail",\n'
        '  "summary": "one-sentence clip summary"\n'
        "}\n\n"
        f"Frame times (seconds into clip): {times_s}."
    )


def ffprobe_duration(path: str) -> float:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL,
        )
        return float(out.decode().strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return 0.0


def extract_frame(src: str, t: float, dst: str) -> bool:
    try:
        subprocess.check_call(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", src,
             "-frames:v", "1", "-q:v", "3", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return os.path.isfile(dst) and os.path.getsize(dst) > 0
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _sample_times(duration: float, n: int = 3) -> list[float]:
    if duration <= 0 or n <= 0:
        return []
    if n == 1:
        return [duration / 2.0]
    if n == 2:
        return [duration * 0.25, duration * 0.75]
    # n >= 3: evenly spaced, clamped away from edges.
    step = (duration - 0.6) / (n - 1)
    return [min(duration - 0.3, 0.3 + step * i) for i in range(n)]


def audit_clip(path: str, spec: dict | None = None,
               sample_count: int = 3,
               shot_id: str | None = None,
               frames_dir: str | None = None) -> dict:
    """Run Opus motion audit on a single mp4. Returns structured dict."""
    spec = dict(DEFAULT_SPEC, **(spec or {}))
    dur = ffprobe_duration(path)
    if dur <= 0:
        return {"shot_id": shot_id, "error": "probe_failed",
                "severity": "error", "any_missing_eyes": False,
                "any_wrong_emblem": False, "summary": "ffprobe failed"}

    times = _sample_times(dur, sample_count)
    work_dir = frames_dir or tempfile.mkdtemp(prefix="motion_audit_")
    os.makedirs(work_dir, exist_ok=True)
    frame_paths: list[str] = []
    tag = shot_id or os.path.splitext(os.path.basename(path))[0]
    for i, t in enumerate(times):
        dst = os.path.join(work_dir, f"{tag}_f{i+1}.jpg")
        if extract_frame(path, t, dst):
            frame_paths.append(dst)

    if len(frame_paths) < sample_count:
        return {"shot_id": shot_id, "error": "frame_extract_failed",
                "extracted": len(frame_paths), "severity": "error",
                "any_missing_eyes": False, "any_wrong_emblem": False,
                "summary": "ffmpeg failed to extract all frames"}

    prompt = _build_prompt(spec, times)
    try:
        result = call_opus_vision_json(
            prompt=prompt,
            image_paths=frame_paths,
            attach_bible=False,
            max_tokens=2048,
        )
    except Exception as e:
        return {"shot_id": shot_id, "error": f"vision_failed: {e}",
                "severity": "error", "any_missing_eyes": False,
                "any_wrong_emblem": False, "summary": "Opus vision call failed"}

    result["shot_id"]     = shot_id
    result["duration"]    = dur
    result["sample_times"] = times
    result["frame_paths"] = frame_paths
    result.setdefault("severity", "error")
    result.setdefault("any_missing_eyes", False)
    result.setdefault("any_wrong_emblem", False)
    result.setdefault("summary", "")
    return result


def audit_clips_batch(clips: Iterable[tuple[str, str]],
                      spec: dict | None = None,
                      sample_count: int = 3,
                      frames_dir: str | None = None) -> list[dict]:
    """Audit many clips sequentially. `clips` yields (shot_id, path) tuples."""
    results: list[dict] = []
    for sid, fp in clips:
        results.append(audit_clip(fp, spec=spec, sample_count=sample_count,
                                   shot_id=sid, frames_dir=frames_dir))
    return results


def summarize(results: list[dict]) -> dict:
    pass_n = sum(1 for r in results if r.get("severity") == "pass")
    warn_n = sum(1 for r in results if r.get("severity") == "warn")
    fail_n = sum(1 for r in results if r.get("severity") == "fail")
    err_n  = sum(1 for r in results if r.get("severity") == "error")
    return {
        "total":   len(results),
        "pass":    pass_n,
        "warn":    warn_n,
        "fail":    fail_n,
        "error":   err_n,
        "fail_ids": [r.get("shot_id") for r in results if r.get("severity") == "fail"],
        "warn_ids": [r.get("shot_id") for r in results if r.get("severity") == "warn"],
    }
