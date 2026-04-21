"""Run the expanded anchor_auditor against every rendered mid-clip frame and
produce a full pass/fail inventory for the TB MV.

Input:
  scenes.json at output/projects/default/prompt_os/scenes.json (25 scenes)
  Mid-clip frames at tools/_clip_audit/NN_<shot_id>_<name>_mid.png

Output:
  tools/_clip_audit/clips_audit.json — full structured results
  stdout — one-line verdict per shot + aggregated summary
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.anchor_auditor import audit_anchor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCENES_PATH = os.path.join(ROOT, "output", "projects", "default", "prompt_os", "scenes.json")
FRAMES_DIR = os.path.join(ROOT, "tools", "_clip_audit")
OUT_PATH = os.path.join(FRAMES_DIR, "clips_audit.json")


def load_scenes() -> list[dict]:
    with open(SCENES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def frame_for_shot(shot_id: str) -> str | None:
    """Find the mid-clip frame matching this shot_id."""
    for fname in os.listdir(FRAMES_DIR):
        if fname.endswith(".png") and shot_id in fname:
            return os.path.join(FRAMES_DIR, fname)
    return None


def main() -> int:
    scenes = load_scenes()
    scenes.sort(key=lambda s: s.get("orderIndex", 999))
    results: list[dict] = []
    fail_count = 0
    pass_count = 0
    missing_count = 0

    for i, scene in enumerate(scenes):
        sid = scene.get("id", "")
        name = scene.get("name", "")
        frame_path = frame_for_shot(sid)
        if not frame_path:
            print(f"[{i:02d}] {sid[:11]} {name} — MISSING FRAME")
            results.append({
                "index": i,
                "shot_id": sid,
                "name": name,
                "status": "missing_frame",
            })
            missing_count += 1
            continue

        shot_context = {
            "name": name,
            "shotDescription": scene.get("shotDescription", ""),
            "cameraAngle": scene.get("cameraAngle", ""),
        }

        t0 = time.time()
        verdict = audit_anchor(frame_path, shot_context=shot_context)
        dt = time.time() - t0

        violations = verdict.get("violations", []) or []
        codes = [v.get("code") for v in violations if isinstance(v, dict)]
        passed = bool(verdict.get("pass"))
        summary_line = verdict.get("summary", "")

        if passed:
            pass_count += 1
            tag = "PASS"
        else:
            fail_count += 1
            tag = "FAIL"

        print(f"[{i:02d}] {sid[:11]} {name:<35} {tag} ({dt:.1f}s)")
        if codes:
            print(f"       violations: {codes}")
        if summary_line:
            print(f"       {summary_line[:140]}")

        results.append({
            "index": i,
            "shot_id": sid,
            "name": name,
            "frame": os.path.relpath(frame_path, ROOT).replace("\\", "/"),
            "pass": passed,
            "violation_codes": codes,
            "violations": violations,
            "summary": summary_line,
            "bear_frame_coverage_pct": verdict.get("bear_frame_coverage_pct"),
            "character_count": verdict.get("character_count"),
            "forehead_visible": verdict.get("forehead_visible"),
            "facing": verdict.get("facing"),
            "back_of_head_emblem_scan": verdict.get("back_of_head_emblem_scan"),
            "pupil_content_scan": verdict.get("pupil_content_scan"),
            "hallucinated_character_scan": verdict.get("hallucinated_character_scan"),
            "audit_seconds": round(dt, 1),
        })

    # Aggregate
    all_codes: dict[str, int] = {}
    for r in results:
        for c in r.get("violation_codes", []) or []:
            all_codes[c] = all_codes.get(c, 0) + 1

    out = {
        "total_scenes": len(scenes),
        "passed": pass_count,
        "failed": fail_count,
        "missing": missing_count,
        "violation_code_counts": dict(sorted(all_codes.items(), key=lambda x: -x[1])),
        "results": results,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print()
    print(f"TOTAL: {len(scenes)}   PASS: {pass_count}   FAIL: {fail_count}   MISSING: {missing_count}")
    print(f"VIOLATION CODES (most common):")
    for code, n in sorted(all_codes.items(), key=lambda x: -x[1]):
        print(f"  {n:>3}  {code}")
    print(f"\nFull results saved: {OUT_PATH}")

    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
