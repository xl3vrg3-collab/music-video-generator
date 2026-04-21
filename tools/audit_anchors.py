"""
Bulk anchor audit for a project's v6 anchors.

Reads scenes.json, runs Sonnet vision audit on every anchor on disk,
prints a per-shot pass/fail table, and writes the full JSON report to
output/pipeline/audits/anchors_<timestamp>.json.

Usage:
  python tools/audit_anchors.py                           # default project
  python tools/audit_anchors.py --project default
  python tools/audit_anchors.py --only-failed             # re-run only last-failed shots
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.anchor_auditor import audit_batch


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="default")
    ap.add_argument("--shot-id", default=None, help="audit only this shot id")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project_dir = os.path.join(root, "output", "projects", args.project, "prompt_os")
    scenes_path = os.path.join(project_dir, "scenes.json")
    anchors_dir = os.path.join(root, "output", "pipeline", "anchors_v6")
    audits_dir = os.path.join(root, "output", "pipeline", "audits")
    os.makedirs(audits_dir, exist_ok=True)

    if not os.path.isfile(scenes_path):
        print(f"scenes.json not found at {scenes_path}")
        return 1

    with open(scenes_path, "r", encoding="utf-8") as f:
        scenes = json.load(f)

    if args.shot_id:
        scenes = [s for s in scenes if s.get("id") == args.shot_id]
        if not scenes:
            print(f"no scene with id={args.shot_id}")
            return 1

    print(f"Auditing {len(scenes)} scene(s) in project '{args.project}'...")
    t0 = time.time()
    report = audit_batch(scenes, anchors_dir)
    elapsed = time.time() - t0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(audits_dir, f"anchors_{ts}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # Per-shot summary
    print("")
    print("=" * 72)
    print(f"  ANCHOR AUDIT  — {report['total']} scenes, {elapsed:.1f}s")
    print("=" * 72)
    print(f"  PASS:    {report['passed']}")
    print(f"  FAIL:    {report['failed']}")
    print(f"  MISSING: {report['missing']}")
    print(f"  report:  {report_path}")
    print("")
    # Detail rows
    for r in report["results"]:
        status = r.get("status")
        if status == "missing_anchor":
            mark = "- "
            detail = "no anchor on disk"
        elif r.get("pass"):
            mark = "OK"
            detail = r.get("summary", "")
        else:
            mark = "XX"
            vs = r.get("violations") or []
            codes = ",".join(v.get("code", "?") for v in vs[:3])
            detail = f"{codes} | {r.get('summary', '')}"
        print(f"  [{mark}] {r.get('id','?'):<16} {r.get('name','?'):<30} {detail[:60]}")

    return 0 if report["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
