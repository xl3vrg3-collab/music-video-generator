"""CLI driver for lib.song_timing.

Usage:
    python tools/analyze_song.py [song_path] [--project default] [--no-lyrics]

Writes output/projects/<project>/audio/timing.json and prints a summary.
This is a thin wrapper around lib.song_timing — the real logic lives there
so the /api/v6/song/analyze endpoint and the UI can share it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.song_timing import analyze_song, save_timing, project_timing_path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_SONG = r"C:/Users/Mathe/Downloads/Lifestream Static.mp3"


def _project_dir(project: str) -> str:
    return os.path.join(ROOT, "output", "projects", project)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("song", nargs="?", default=DEFAULT_SONG)
    ap.add_argument("--project", default="default")
    ap.add_argument("--no-lyrics", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.song):
        print(f"song not found: {args.song}", file=sys.stderr)
        return 2

    timing = analyze_song(args.song, include_lyrics=not args.no_lyrics)

    project_dir = _project_dir(args.project)
    out_path = save_timing(project_dir, timing)

    # Summary
    print()
    print("=" * 70)
    print(f"song      : {os.path.basename(args.song)}")
    print(f"duration  : {timing['source']['duration']:.2f}s")
    print(f"bpm       : {timing['tempo']['bpm']:.1f}")
    print(f"beats     : {len(timing['beats'])}")
    print(f"downbeats : {len(timing['downbeats'])}  (phase={timing['tempo']['downbeat_phase']})")
    print(f"bars      : {len(timing['bars'])}")
    print(f"sections  : {len(timing['sections'])}")
    for s in timing["sections"]:
        print(f"    [{s['index']:>2}] {s['label']:<12} {s['start']:>7.2f}s -> {s['end']:>7.2f}s  energy={s['energy']:.3f}")
    lyr = timing.get("lyrics") or {}
    print(f"lyrics    : engine={lyr.get('engine')}  words={len(lyr.get('words', []))}  lines={len(lyr.get('lines', []))}")
    for ln in (lyr.get("lines") or [])[:10]:
        print(f"    [{ln['start']:>6.2f} - {ln['end']:>6.2f}]  {ln['text']}")
    if len(lyr.get("lines", [])) > 10:
        print(f"    ... +{len(lyr['lines']) - 10} more lines")
    print()
    print(f"saved to  : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
