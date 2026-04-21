"""Re-run any TB shot whose clip is missing on disk.

Reads scenes.json and re-runs render_all_shots logic for any scene that
doesn't have output/pipeline/clips_v6/<id>/selected.mp4 yet. Transient fal
failures (DNS, HTTP 5xx, etc.) during the main batch leave gaps that this
script fills.
"""
from __future__ import annotations
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tools.render_all_shots import (  # noqa: E402
    SCENES_JSON, CLIPS_DIR, _find_env_preview, _render_one, _scene_key,
)


def main():
    with open(SCENES_JSON, "r", encoding="utf-8") as f:
        scenes = json.load(f)
    scenes.sort(key=lambda s: _scene_key(s.get("name", "")))

    missing = []
    for s in scenes:
        clip = os.path.join(CLIPS_DIR, s["id"], "selected.mp4")
        if not os.path.isfile(clip):
            missing.append(s)

    if not missing:
        print("no missing clips — nothing to retry")
        return

    print(f"retrying {len(missing)} shots:")
    for s in missing:
        print(f"  - {s['name']}")
    print()

    for s in missing:
        env = _find_env_preview(s.get("environmentId") or "")
        if not env:
            print(f"  SKIP {s['name']} — missing env")
            continue
        print(f"\n>>> {s['name']}")
        _render_one(s, env)


if __name__ == "__main__":
    main()
