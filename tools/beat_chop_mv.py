"""Chop each TB shot into beat-paced sub-cuts and rewrite mv-data.json.

Strategy:
  * Detect beats in the song via librosa (~129 BPM → 0.466s interval)
  * Per shot, pick N sub-cuts by scene.energy:
      energy <=3  -> 1 (no chop, breathe)
      energy 4-5  -> 2
      energy 6-7  -> 3
      energy 8-9  -> 4
      energy 10   -> 5
  * Snap each sub-cut duration to the nearest beat interval (>=1 beat)
  * ffmpeg stream-copy from existing selected.mp4 (no re-encode, no quality loss)
  * Rewrite lumn-stitcher/public/mv/ with numbered sub-clips
      e.g. 1_1_a.mp4, 1_1_b.mp4 for shot 1.1's two cuts
  * Rewrite src/mv-data.json so Remotion plays them in order

Idempotent: re-run any time. Sub-cuts overwrite cleanly.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys

import librosa
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FPS = 24
WIDTH = 1928
HEIGHT = 1072
BEAT_AUDIO = r"C:/Users/Mathe/Downloads/Lifestream Static.mp3"
PROJECT_ROOT = os.path.join(ROOT, "output", "projects", "default", "prompt_os")
SCENES_JSON = os.path.join(PROJECT_ROOT, "scenes.json")
CLIPS_DIR = os.path.join(ROOT, "output", "pipeline", "clips_v6")

STITCHER_DIR = os.path.join(ROOT, "lumn-stitcher")
PUBLIC_DIR = os.path.join(STITCHER_DIR, "public")
MV_DIR = os.path.join(PUBLIC_DIR, "mv")
DATA_JSON = os.path.join(STITCHER_DIR, "src", "mv-data.json")


def _scene_key(name: str) -> str:
    return (name or "").split(" ", 1)[0].strip()


def _sort_key(name: str) -> tuple:
    k = _scene_key(name)
    if "." in k:
        a, b = k.split(".", 1)
        try:
            return (int(a), int(b))
        except ValueError:
            return (99, 99)
    return (99, 99)


def _num_cuts(energy: int | float | None) -> int:
    e = int(energy or 5)
    if e <= 3:
        return 1
    if e <= 5:
        return 2
    if e <= 7:
        return 3
    if e <= 9:
        return 4
    return 5


def _detect_beat_interval() -> float:
    y, sr = librosa.load(BEAT_AUDIO, sr=22050)
    _, beats = librosa.beat.beat_track(y=y, sr=sr)
    bt = librosa.frames_to_time(beats, sr=sr)
    return float(np.mean(np.diff(bt)))


def _probe_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=15,
    )
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _extract_segment(src: str, dst: str, start: float, duration: float) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", src,
         "-t", f"{duration:.3f}", "-c", "copy", "-avoid_negative_ts", "1",
         dst],
        capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0 and os.path.isfile(dst)


def _plan_cuts(total_sec: float, n_cuts: int, beat_interval: float) -> list[tuple[float, float]]:
    """Return list of (start, duration) for n_cuts sub-clips covering total_sec.

    Each sub-cut is at least 1 beat long, snapped to beat-interval multiples.
    """
    if n_cuts <= 1 or total_sec < beat_interval * 2:
        return [(0.0, total_sec)]
    total_beats = max(n_cuts, int(round(total_sec / beat_interval)))
    base = total_beats // n_cuts
    extra = total_beats - base * n_cuts
    lens = [base + (1 if i < extra else 0) for i in range(n_cuts)]
    cuts = []
    cursor = 0.0
    for i, beats in enumerate(lens):
        dur = beats * beat_interval
        if i == n_cuts - 1:
            dur = max(beat_interval, total_sec - cursor)
        cuts.append((cursor, dur))
        cursor += dur
    return cuts


def main():
    beat = _detect_beat_interval()
    print(f"beat interval: {beat*1000:.0f} ms  (~{60/beat:.1f} BPM)")

    with open(SCENES_JSON, "r", encoding="utf-8") as f:
        scenes = json.load(f)
    scenes.sort(key=lambda s: _sort_key(s.get("name", "")))

    os.makedirs(MV_DIR, exist_ok=True)
    for f in os.listdir(MV_DIR):
        if f.endswith(".mp4"):
            os.remove(os.path.join(MV_DIR, f))

    audio_dst = os.path.join(PUBLIC_DIR, "lifestream_static.mp3")
    if not os.path.isfile(audio_dst) and os.path.isfile(BEAT_AUDIO):
        shutil.copy2(BEAT_AUDIO, audio_dst)

    clips_out = []
    total_frames = 0
    missing = []

    for s in scenes:
        sid = s["id"]
        name = s["name"]
        energy = s.get("energy") or 5
        key = _scene_key(name).replace(".", "_")
        src = os.path.join(CLIPS_DIR, sid, "selected.mp4")
        if not os.path.isfile(src):
            missing.append(name)
            continue

        src_dur = _probe_duration(src)
        if src_dur <= 0:
            missing.append(f"{name} (ffprobe failed)")
            continue

        n = _num_cuts(energy)
        cuts = _plan_cuts(src_dur, n, beat)
        suffixes = "abcdefgh"

        for idx, (start, dur) in enumerate(cuts):
            dst_name = f"{key}_{suffixes[idx]}_{sid[:7]}.mp4"
            dst = os.path.join(MV_DIR, dst_name)
            ok = _extract_segment(src, dst, start, dur)
            if not ok:
                print(f"  FAIL {name} seg {idx}")
                continue
            frames = int(round(dur * FPS))
            clips_out.append({
                "name": f"{name}  [{suffixes[idx]}]",
                "src": f"mv/{dst_name}",
                "duration": round(dur, 2),
                "durationInFrames": frames,
            })
            total_frames += frames

        print(f"  {name}  energy={energy}  cuts={n}  src={src_dur:.1f}s")

    data = {
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "audio": "lifestream_static.mp3" if os.path.isfile(audio_dst) else None,
        "totalFrames": total_frames,
        "clips": clips_out,
    }
    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"\nsub-cuts: {len(clips_out)}  total: {total_frames/FPS:.1f}s")
    if missing:
        print(f"missing source clips ({len(missing)}):")
        for m in missing:
            print(f"  - {m}")


if __name__ == "__main__":
    main()
