"""Stage TB Lifestream Static MV assets into lumn-stitcher/public/.

Reads scenes.json from the active project, walks them in order, and for each
scene that has a rendered clip at output/pipeline/clips_v6/<id>/selected.mp4,
copies the clip into lumn-stitcher/public/mv/ as <sortkey>_<id>.mp4.
Also copies the audio track and writes mv-data.json with the ordered
[ { src, durationInFrames, name, duration } ].

Idempotent. Safe to re-run after partial renders complete.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FPS = 24
WIDTH = 1928
HEIGHT = 1072
PROJECT_ROOT  = os.path.join(ROOT, "output", "projects", "default", "prompt_os")
SCENES_JSON   = os.path.join(PROJECT_ROOT, "scenes.json")
CLIPS_DIR     = os.path.join(ROOT, "output", "pipeline", "clips_v6")
AUDIO_SRC     = r"C:/Users/Mathe/Downloads/Lifestream Static.mp3"

STITCHER_DIR  = os.path.join(ROOT, "lumn-stitcher")
PUBLIC_DIR    = os.path.join(STITCHER_DIR, "public")
MV_DIR        = os.path.join(PUBLIC_DIR, "mv")
DATA_JSON     = os.path.join(STITCHER_DIR, "src", "mv-data.json")


def _probe_frames(path: str, declared_duration: int) -> int:
    """Actual frame count via ffprobe; fall back to declared*fps."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames", "-of",
             "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15,
        )
        n = int(r.stdout.strip())
        if n > 0:
            return n
    except Exception:
        pass
    return declared_duration * FPS


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


def main():
    with open(SCENES_JSON, "r", encoding="utf-8") as f:
        scenes = json.load(f)
    scenes.sort(key=lambda s: _sort_key(s.get("name", "")))

    os.makedirs(MV_DIR, exist_ok=True)

    clips_out = []
    total_frames = 0
    missing = []

    for s in scenes:
        sid = s["id"]
        name = s["name"]
        key = _scene_key(name).replace(".", "_")
        duration = int(s.get("duration") or 6)
        frames = duration * FPS

        src_clip = os.path.join(CLIPS_DIR, sid, "selected.mp4")
        if not os.path.isfile(src_clip):
            missing.append(name)
            continue

        dst_name = f"{key}_{sid}.mp4"
        dst_path = os.path.join(MV_DIR, dst_name)
        if (not os.path.isfile(dst_path)
                or os.path.getmtime(src_clip) > os.path.getmtime(dst_path)):
            shutil.copy2(src_clip, dst_path)

        actual_frames = _probe_frames(dst_path, duration)
        clips_out.append({
            "name": name,
            "src": f"mv/{dst_name}",
            "duration": duration,
            "durationInFrames": actual_frames,
        })
        total_frames += actual_frames

    # Audio
    audio_rel = None
    if os.path.isfile(AUDIO_SRC):
        audio_dst = os.path.join(PUBLIC_DIR, "lifestream_static.mp3")
        if not os.path.isfile(audio_dst):
            shutil.copy2(AUDIO_SRC, audio_dst)
        audio_rel = "lifestream_static.mp3"

    data = {
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "audio": audio_rel,
        "totalFrames": total_frames,
        "clips": clips_out,
    }
    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"staged {len(clips_out)}/{len(scenes)} clips  total={total_frames} frames "
          f"({total_frames/FPS:.1f}s)")
    if missing:
        print(f"missing ({len(missing)}):")
        for m in missing:
            print(f"  - {m}")
    print(f"wrote {DATA_JSON}")


if __name__ == "__main__":
    main()
