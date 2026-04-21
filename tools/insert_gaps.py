"""CLI: report per-section insert gaps and propose candidate inserts (F7a).

Usage:
    python tools/insert_gaps.py                                 # use defaults
    python tools/insert_gaps.py --pacing <pacing_curve.json> --scenes <scenes.json>
    python tools/insert_gaps.py --write                         # also save insert_candidates.json

Output:
    output/pipeline/audits/insert_gaps_<ts>.json
    output/pipeline/insert_candidates.json   (when --write)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.insert_gaps import build_report

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PACING = os.path.join(ROOT, "output", "pipeline", "pacing_curve.json")
DEFAULT_SCENES = os.path.join(ROOT, "output", "projects", "default", "prompt_os", "scenes.json")
AUDITS_DIR     = os.path.join(ROOT, "output", "pipeline", "audits")
CANDIDATES_OUT = os.path.join(ROOT, "output", "pipeline", "insert_candidates.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pacing", default=DEFAULT_PACING, help="path to pacing_curve.json")
    ap.add_argument("--scenes", default=DEFAULT_SCENES, help="path to scenes.json")
    ap.add_argument("--write",  action="store_true",    help="also write insert_candidates.json")
    args = ap.parse_args()

    if not os.path.isfile(args.pacing):
        print(f"  pacing_curve not found: {args.pacing}")
        print("  hint: run `python tools/pacing_arc.py --persist` first")
        return 1
    if not os.path.isfile(args.scenes):
        print(f"  scenes.json not found: {args.scenes}")
        return 1

    report = build_report(args.pacing, args.scenes)

    os.makedirs(AUDITS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_path = os.path.join(AUDITS_DIR, f"insert_gaps_{ts}.json")
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    if args.write:
        payload = {"generated_at": ts, "candidates": report["candidates"]}
        with open(CANDIDATES_OUT, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    gaps   = report["gaps"]
    print(f"\n  sections:            {len(gaps['sections'])}")
    print(f"  existing scenes:     {gaps['total_existing_scenes']}")
    print(f"  suggested cuts:      {gaps['total_suggested_cuts']}")
    print(f"  total gap:           {gaps['total_gap']}  (inserts to render)")
    print(f"  proposed candidates: {report['total_candidates']}")
    print(f"  gap report:          {audit_path}")
    if args.write:
        print(f"  candidates:          {CANDIDATES_OUT}")
    print()

    for sec in gaps["sections"]:
        actual = sec["actual_scenes"]
        sug    = sec["suggested_cuts"]
        gap    = sec["gap"]
        flag   = "UNDER" if gap > 0 else "ok"
        label  = sec["label"].ljust(11)
        print(f"  [{sec['index']}] {sec['start_s']:7.2f}s -> {sec['end_s']:7.2f}s  "
              f"{label}  have {actual:2d} / want {sug:2d}  gap {gap:+d}  {flag}"
              + (f"  (anchors: {', '.join(str(a) for a in sec['anchor_scene_ids'])})" if actual else ""))

    if report["candidates"]:
        print(f"\n  first 5 candidates:")
        for c in report["candidates"][:5]:
            print(f"    {c['opus_scene_id']:<16} of {c['insert_of']:<6}  "
                  f"{c['kind']:<22}  — {c['shotDescription']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
