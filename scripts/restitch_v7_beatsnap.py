"""One-shot driver: beat-snap restitch of TB-v7 with the fixed duration logic.

Outputs:
    output/pipeline/snapped_v7/<shot_id>_snap.mp4    (per-clip re-encodes)
    output/pipeline/v7_final_beatsnap_v2.mp4         (final with audio)
    output/pipeline/audits/beatsnap_v2_summary.json  (plan + cut-drift compare)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lib.beat_snap import _probe_duration, plan_beat_snap, apply_beat_snap, load_grid
from lib.cut_drift import analyze_cut_drift


OUT_ROOT    = os.path.join(ROOT, "output", "pipeline")
PROJECT     = os.path.join(OUT_ROOT, "project.json")
GRID        = os.path.join(OUT_ROOT, "music_grid.json")
CLIPS_ROOT  = os.path.join(OUT_ROOT, "clips_v6")
SNAPPED_DIR = os.path.join(OUT_ROOT, "snapped_v7")
FINAL_MP4   = os.path.join(OUT_ROOT, "v7_final_beatsnap_v2.mp4")
AUDIO       = os.path.join(ROOT, "lumn-stitcher", "public", "lifestream_static.mp3")
SUMMARY     = os.path.join(OUT_ROOT, "audits", "beatsnap_v2_summary.json")


def main() -> int:
    t0 = time.time()

    project = json.loads(Path(PROJECT).read_text(encoding="utf-8"))
    downbeats = load_grid(GRID)

    clips = []
    for shot in project.get("shots", []):
        sid = shot.get("shot_id")
        src = os.path.join(CLIPS_ROOT, sid, "selected.mp4")
        if not os.path.isfile(src):
            continue
        probed = _probe_duration(src)
        duration = probed if probed > 0 else float(shot.get("dur") or 5.0)
        clips.append({"shot_id": sid, "source": src, "duration": duration})

    print(f"[resolve]  {len(clips)} clips, total probed {sum(c['duration'] for c in clips):.2f}s")

    plan = plan_beat_snap(clips, downbeats, tolerance_s=2.0, fps=24)
    print(f"[plan]     snapped_total {plan['snapped_total_s']:.2f}s  "
          f"delta {plan['delta_s']:+.2f}s  "
          f"cuts_snapped {plan['cuts_snapped']}/{plan['clip_count']}")

    print(f"[apply]    re-encoding {plan['clip_count']} clips to {SNAPPED_DIR} ...")
    result = apply_beat_snap(plan, SNAPPED_DIR)
    if result.get("errors"):
        print(f"[apply]    {len(result['errors'])} errors: {result['errors'][:3]}")
    print(f"[apply]    wrote {len(result['clips'])} files")

    # Build ffmpeg concat list in shot order
    concat_list = os.path.join(SNAPPED_DIR, "concat_list.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for c in result["clips"]:
            p = c["output_path"].replace("\\", "/")
            f.write(f"file '{p}'\n")

    print(f"[concat]   concat + audio -> {FINAL_MP4}")
    # First concat video-only
    vid_only = os.path.join(SNAPPED_DIR, "_concat_no_audio.mp4")
    cmd_concat = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-an",
        vid_only,
    ]
    subprocess.check_call(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Then mux with song audio
    cmd_mux = [
        "ffmpeg", "-y",
        "-i", vid_only,
        "-i", AUDIO,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "256k",
        "-shortest",
        FINAL_MP4,
    ]
    subprocess.check_call(cmd_mux, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[final]    {FINAL_MP4}  duration {_probe_duration(FINAL_MP4):.2f}s")

    # Cut-drift compare (snapped timeline vs downbeats)
    cut_times = [0.0]
    for c in result["clips"]:
        cut_times.append(cut_times[-1] + c["duration_s"])
    drift = analyze_cut_drift(cut_times, downbeats, threshold_s=0.2)

    summary = {
        "elapsed_s": round(time.time() - t0, 2),
        "plan": {
            "clip_count": plan["clip_count"],
            "snapped_total_s": plan["snapped_total_s"],
            "delta_s": plan["delta_s"],
            "cuts_snapped": plan["cuts_snapped"],
            "tolerance_s": plan["tolerance_s"],
        },
        "apply": {
            "written": len(result["clips"]),
            "errors": result.get("errors", []),
        },
        "final_path": FINAL_MP4,
        "final_duration_s": round(_probe_duration(FINAL_MP4), 3),
        "cut_drift": {
            "total_cuts":    drift["total_cuts"],
            "off_grid_count": drift["off_grid_count"],
            "off_grid_pct":   drift["off_grid_pct"],
            "max_drift_s":    drift["max_drift_s"],
            "mean_drift_s":   drift["mean_drift_s"],
        },
    }
    Path(os.path.dirname(SUMMARY)).mkdir(parents=True, exist_ok=True)
    Path(SUMMARY).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[summary]  {SUMMARY}")

    print(f"\n[cut_drift] {drift['off_grid_count']}/{drift['total_cuts']} off-grid ({drift['off_grid_pct']}%)")
    print(f"[cut_drift] max {drift['max_drift_s']:.3f}s  mean {drift['mean_drift_s']:.3f}s")
    print(f"\n[done]     elapsed {summary['elapsed_s']:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
