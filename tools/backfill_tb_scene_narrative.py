"""Backfill narrative metadata (sceneType, narrativeIntent, emotion, energy)
for the TB "Lifestream Static" shot list. Idempotent.
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCENES_JSON = os.path.join(ROOT, "output", "projects", "default",
                            "prompt_os", "scenes.json")

# name prefix -> (sceneType, narrativeIntent, emotion, energy)
META = {
    "1.1": ("intro",        "establish character", "melancholic",  3),
    "1.2": ("intro",        "establish character", "melancholic",  3),
    "2.1": ("verse",        "build tension",       "mysterious",   4),
    "2.2": ("verse",        "build tension",       "melancholic",  4),
    "2.3": ("verse",        "reveal",              "mysterious",   4),
    "3.1": ("pre-chorus",   "reveal",              "tense",        6),
    "3.2": ("pre-chorus",   "transition energy",   "surreal",      7),
    "4.1": ("chorus",       "emotional release",   "chaotic",     10),
    "4.2": ("chorus",       "emotional release",   "chaotic",     10),
    "4.3": ("chorus",       "reveal",              "emotional",    8),
    "5.1": ("verse",        "transition energy",   "surreal",      5),
    "5.2": ("verse",        "reveal",              "surreal",      5),
    "5.3": ("verse",        "build tension",       "mysterious",   5),
    "6.1": ("pre-chorus",   "transition energy",   "cinematic",    6),
    "6.2": ("pre-chorus",   "reveal",              "emotional",    5),
    "7.1": ("chorus",       "emotional release",   "triumphant",  10),
    "7.2": ("chorus",       "build tension",       "chaotic",     10),
    "7.3": ("chorus",       "emotional release",   "triumphant",  10),
    "8.1": ("bridge",       "emotional release",   "emotional",    4),
    "8.2": ("bridge",       "resolution",          "triumphant",   6),
    "9.1": ("outro",        "resolution",          "triumphant",   8),
    "9.2": ("outro",        "resolution",          "triumphant",   8),
    "9.3": ("outro",        "resolution",          "cinematic",    7),
    "9.4": ("outro",        "resolution",          "calm",         5),
    "9.5": ("outro",        "resolution",          "calm",         4),
    "9.6": ("outro",        "resolution",          "cinematic",    3),
}


def _key(name: str) -> str:
    """Return '1.1' from '1.1 INTRO establishing'."""
    return (name or "").split(" ", 1)[0].strip()


def main():
    with open(SCENES_JSON, "r", encoding="utf-8") as f:
        scenes = json.load(f)

    now = datetime.now().isoformat(timespec="seconds")
    updates = 0
    for s in scenes:
        k = _key(s.get("name", ""))
        m = META.get(k)
        if not m:
            print(f"  SKIP {k}: no meta mapping")
            continue
        st, ni, em, en = m
        changed = (
            s.get("sceneType") != st
            or s.get("narrativeIntent") != ni
            or s.get("emotion") != em
            or s.get("energy") != en
        )
        if changed:
            s["sceneType"] = st
            s["narrativeIntent"] = ni
            s["emotion"] = em
            s["energy"] = en
            s["updatedAt"] = now
            updates += 1
            print(f"  {k:4s} {s.get('name','')[:32]:32s} -> {st}/{ni}/{em}/e{en}")
        else:
            print(f"  {k:4s} already matches, skipping")

    # backup + write
    bak = SCENES_JSON + ".bak_backfill"
    if updates > 0:
        with open(bak, "w", encoding="utf-8") as f:
            json.dump(scenes, f, indent=2)  # write fresh (we already loaded)
    # overwrite
    with open(SCENES_JSON, "w", encoding="utf-8") as f:
        json.dump(scenes, f, indent=2)
    print(f"\n  {updates}/{len(scenes)} scenes updated.")


if __name__ == "__main__":
    main()
