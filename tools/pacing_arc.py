"""CLI: compute pacing-arc recommendation from a music_grid.json.

Usage:
    python tools/pacing_arc.py                                  # use defaults
    python tools/pacing_arc.py --grid <music_grid.json>
    python tools/pacing_arc.py --style climax-heavy
    python tools/pacing_arc.py --persist                        # also save to pacing_curve.json

Output:
    output/pipeline/audits/pacing_curve_<ts>.json
    output/pipeline/pacing_curve.json  (when --persist)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.pacing_arc import analyze_grid, recommend, CURVE_STYLES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GRID    = os.path.join(ROOT, "output", "pipeline", "music_grid.json")
AUDITS_DIR      = os.path.join(ROOT, "output", "pipeline", "audits")
PERSIST_PATH    = os.path.join(ROOT, "output", "pipeline", "pacing_curve.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", default=DEFAULT_GRID, help="path to music_grid.json")
    ap.add_argument("--style", default="arc", choices=CURVE_STYLES, help="pacing curve style")
    ap.add_argument("--persist", action="store_true",
                    help="also write to output/pipeline/pacing_curve.json for downstream use")
    args = ap.parse_args()

    if not os.path.isfile(args.grid):
        print(f"  music_grid not found: {args.grid}")
        return 1

    os.makedirs(AUDITS_DIR, exist_ok=True)
    t0 = time.time()
    result = analyze_grid(args.grid, curve_style=args.style)
    result["elapsed_s"]      = round(time.time() - t0, 3)
    result["recommendation"] = recommend(result)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(AUDITS_DIR, f"pacing_curve_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    if args.persist:
        with open(PERSIST_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

    print(f"\n  sections:        {len(result['sections'])}")
    print(f"  tempo:           {result['tempo_bpm']} bpm ({result['bar_s']}s/bar)")
    print(f"  style:           {result['curve_style']}")
    print(f"  total cuts:      {result['total_suggested_cuts']}")
    print(f"  profile:         {' -> '.join(result['intensity_profile'])}")
    print(f"  recommendation:  {result['recommendation']}")
    print(f"  report:          {out}")
    if args.persist:
        print(f"  persisted:       {PERSIST_PATH}")
    print()

    for s in result["sections"]:
        label_pad = s["label"].ljust(11)
        print(f"  [{s['index']}] {s['start_s']:7.2f}s -> {s['end_s']:7.2f}s  "
              f"{label_pad}  {s['bars_per_cut']:>3.1f} bars/cut  "
              f"-> {s['suggested_cuts']:>2d} cuts @ {s['target_cut_duration_s']}s each")

    return 0


if __name__ == "__main__":
    sys.exit(main())
