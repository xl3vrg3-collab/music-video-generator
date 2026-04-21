"""CLI: compute cut-drift for a stitched MV vs its music grid.

Usage:
    python tools/cut_drift.py                                      # use defaults
    python tools/cut_drift.py --mv <mv-data.json> --grid <grid.json>
    python tools/cut_drift.py --threshold 0.15                     # tighter grid

Output:
    output/pipeline/audits/cut_drift_<ts>.json
"""
from __future__ import annotations

import argparse, json, os, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.cut_drift import analyze_mv, recommend

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MV   = os.path.join(ROOT, "lumn-stitcher", "src", "mv-data.json")
DEFAULT_GRID = os.path.join(ROOT, "output", "pipeline", "music_grid.json")
AUDITS_DIR   = os.path.join(ROOT, "output", "pipeline", "audits")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mv", default=DEFAULT_MV, help="path to mv-data.json")
    ap.add_argument("--grid", default=DEFAULT_GRID, help="path to music_grid.json")
    ap.add_argument("--threshold", type=float, default=0.2, help="off-grid threshold in seconds")
    args = ap.parse_args()

    if not os.path.isfile(args.mv):
        print(f"  mv-data not found: {args.mv}")
        return 1
    if not os.path.isfile(args.grid):
        print(f"  music_grid not found: {args.grid}")
        return 1

    os.makedirs(AUDITS_DIR, exist_ok=True)
    t0 = time.time()
    result = analyze_mv(args.mv, args.grid, threshold_s=args.threshold)
    result["elapsed_s"] = round(time.time() - t0, 3)
    result["recommendation"] = recommend(result)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(AUDITS_DIR, f"cut_drift_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"\n  total cuts:    {result['total_cuts']}")
    print(f"  off-grid:      {result['off_grid_count']} ({result['off_grid_pct']}%)")
    print(f"  max drift:     {result.get('max_drift_s', 0):.3f}s")
    print(f"  mean drift:    {result.get('mean_drift_s', 0):.3f}s")
    print(f"  threshold:     {args.threshold}s")
    print(f"  recommendation:  {result['recommendation']}")
    print(f"  report:        {out}\n")

    for r in result.get("off_grid_only", [])[:15]:
        scene_bridge = f"{r.get('scene_out', '?')[:22]} -> {r.get('scene_in', '?')[:22]}"
        print(f"  cut {r['cut_idx']:2d} @ {r['cut_time_s']:6.2f}s  delta {r['delta_s']:+.3f}s  ({scene_bridge})")

    return 0 if result["off_grid_count"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
