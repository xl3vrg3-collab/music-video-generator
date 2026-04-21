"""Audit every v7 clip for two specific motion-induced identity breaks:
  1. Eyes not visible while face is on-camera (missing/closed/occluded).
  2. Crescent emblem appearing anywhere other than the forehead (back of head,
     above head, floating, on shoulder, etc.).

Samples 3 frames per clip (t~=1s, mid, dur-1s), sends them to Opus vision in
one structured call per clip, and produces a flagged list for regen.

Usage:
    python scripts/audit_v7_motion.py                 # audit all 30 v7 clips
    python scripts/audit_v7_motion.py --only 7a,8b    # only these shots
    python scripts/audit_v7_motion.py --limit 5       # first N
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.claude_client import call_opus_vision_json

CLIPS_DIR   = os.path.join(ROOT, "lumn-stitcher", "public", "mv")
FRAMES_DIR  = os.path.join(ROOT, "output", "pipeline", "audits", "v7_frames")
REPORT_DIR  = os.path.join(ROOT, "output", "pipeline", "audits")
os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)


PROMPT = """You are auditing 3 frames sampled from a single 9-second Kling clip
of a chibi-style anime bear named TB ("Trillion Bear"). The bear has a locked
identity:
  - Crescent-moon emblem glowing on the CENTER OF THE FOREHEAD, between/above
    the eyes. NEVER on the back of the head, NEVER floating above, NEVER on
    the shoulders or body.
  - Two large anime-style eyes on the front of the face.

For each of the 3 frames, report:
  * eyes_visible: one of
      - "yes"         : both eyes clearly visible on the front of the face
      - "closed"      : blinked / closed but eye positions visible
      - "occluded"    : hand/hair/shadow covering eyes
      - "missing"     : face on-camera but eyes simply not rendered (rendering fail)
      - "back_view"   : bear is facing away / back of head — no face visible (OK)
      - "no_face"     : bear not in frame or only body/paws visible (OK)
  * emblem_location: one of
      - "forehead"    : correct — crescent on forehead
      - "back_of_head": WRONG — crescent visible on back of head
      - "above_head"  : WRONG — floating above head, halo-style
      - "shoulder"    : WRONG — on shoulder/body
      - "absent"      : no emblem visible (may be OK if back view or far wide)
      - "not_applicable": bear not visible at all
  * notes: 1 short sentence on what you see

Then a clip-level verdict:
  * any_missing_eyes: true if any frame is eyes_visible="missing"
  * any_wrong_emblem: true if any frame has emblem_location in
                      ["back_of_head","above_head","shoulder"]
  * severity: "pass" | "warn" | "fail"
      fail   = any_missing_eyes OR any_wrong_emblem
      warn   = emblem absent when face is clearly shown forward
      pass   = otherwise

Return STRICTLY JSON matching this schema, no prose:
{
  "frames": [
    {"t": <seconds>, "eyes_visible": "...", "emblem_location": "...", "notes": "..."},
    ...
  ],
  "any_missing_eyes": true|false,
  "any_wrong_emblem": true|false,
  "severity": "pass"|"warn"|"fail",
  "summary": "one-sentence clip summary"
}
"""


def ffprobe_duration(path: str) -> float:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL)
        return float(out.decode().strip())
    except Exception:
        return 0.0


def extract_frame(src: str, t: float, dst: str) -> bool:
    """Extract single JPG frame at time t. Returns True on success."""
    try:
        subprocess.check_call(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", src,
             "-frames:v", "1", "-q:v", "3", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.isfile(dst) and os.path.getsize(dst) > 0
    except Exception:
        return False


def parse_shot_id(fname: str) -> str:
    # v7_7a_ded81c4.mp4 -> "7a"
    base = fname.replace("v7_", "").split("_", 1)[0]
    return base


def audit_clip(path: str, shot_id: str) -> dict:
    dur = ffprobe_duration(path)
    if dur <= 0:
        return {"shot_id": shot_id, "error": "probe_failed", "severity": "fail"}

    # sample times: t=1, mid, dur-1 (clamped)
    t1 = min(1.0, max(0.5, dur * 0.15))
    t2 = dur / 2.0
    t3 = max(dur - 1.0, dur * 0.85)
    stamps = [t1, t2, t3]

    frame_paths = []
    for i, t in enumerate(stamps):
        dst = os.path.join(FRAMES_DIR, f"{shot_id}_f{i+1}.jpg")
        if extract_frame(path, t, dst):
            frame_paths.append(dst)

    if len(frame_paths) < 3:
        return {"shot_id": shot_id, "error": "frame_extract_failed",
                "extracted": len(frame_paths), "severity": "fail"}

    prompt = PROMPT + f"\n\nThe 3 frame times (seconds into clip) are: " \
                      f"{stamps[0]:.2f}, {stamps[1]:.2f}, {stamps[2]:.2f}."

    try:
        result = call_opus_vision_json(
            prompt=prompt,
            image_paths=frame_paths,
            attach_bible=False,
            max_tokens=2048,
        )
    except Exception as e:
        return {"shot_id": shot_id, "error": f"vision_failed: {e}",
                "severity": "fail"}

    result["shot_id"] = shot_id
    result["duration"] = dur
    result["sampled_times"] = stamps
    result["frame_paths"]   = [os.path.relpath(p, ROOT) for p in frame_paths]
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only",  default="", help="comma-sep shot ids (e.g. 7a,8b)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    only = set([s.strip() for s in args.only.split(",") if s.strip()])

    clips = sorted([f for f in os.listdir(CLIPS_DIR) if f.startswith("v7_") and f.endswith(".mp4")])
    if only:
        clips = [c for c in clips if parse_shot_id(c) in only]
    if args.limit:
        clips = clips[: args.limit]

    print(f"[audit] {len(clips)} clips")
    results = []
    flagged = []

    for i, fname in enumerate(clips, 1):
        shot_id = parse_shot_id(fname)
        src = os.path.join(CLIPS_DIR, fname)
        print(f"  [{i}/{len(clips)}] {shot_id} ({fname}) ...", flush=True)
        t0 = time.time()
        r = audit_clip(src, shot_id)
        results.append(r)
        sev = r.get("severity", "?")
        elapsed = time.time() - t0
        print(f"      -> {sev}  ({elapsed:.1f}s)  {r.get('summary','')[:80]}")
        if sev == "fail":
            flagged.append({
                "shot_id": shot_id,
                "any_missing_eyes": r.get("any_missing_eyes"),
                "any_wrong_emblem": r.get("any_wrong_emblem"),
                "summary": r.get("summary"),
                "frames": r.get("frames"),
            })

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(REPORT_DIR, f"v7_motion_audit_{ts}.json")
    payload = {
        "generated_at": ts,
        "clip_count": len(clips),
        "flagged_count": len(flagged),
        "flagged": flagged,
        "all_results": results,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"\n[audit] flagged {len(flagged)}/{len(clips)} clips")
    for f in flagged:
        eyes  = "EYES"   if f.get("any_missing_eyes") else "    "
        emb   = "EMBLEM" if f.get("any_wrong_emblem") else "      "
        print(f"  FAIL  {f['shot_id']:<4}  [{eyes} {emb}]  {f.get('summary','')[:100]}")
    print(f"\n[audit] report: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
