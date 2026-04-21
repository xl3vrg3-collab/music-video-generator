"""Audit user-scoped anchors (u_14/<shot_id>/candidate_0.png) for the TB project.

Standard audit_batch expects anchors_v6/<sid>/selected.png. After the
identity-injection workaround (direct API calls), anchors land at
anchors_v6/u_14/<sid>/candidate_0.png with no selected.png, so this wrapper
points the auditor at candidate_0.png directly and writes the report.
"""

import json
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from lib.anchor_auditor import audit_anchor, DEFAULT_CHARACTER_RULES

USER_ID = os.environ.get("LUMN_AUDIT_USER", "u_14")
PROJECT = os.environ.get("LUMN_PROJECT", "default")

SCENES_PATH = os.path.join(PROJECT_ROOT, "output", "projects", PROJECT, "prompt_os", "scenes.json")
ANCHORS_ROOT = os.path.join(PROJECT_ROOT, "output", "pipeline", "anchors_v6", USER_ID)


def main() -> int:
    with open(SCENES_PATH, encoding="utf-8") as f:
        scenes = json.load(f)

    results = []
    passed = failed = missing = 0

    for scene in scenes:
        sid = scene.get("id", "")
        name = scene.get("name", sid)
        path = os.path.join(ANCHORS_ROOT, sid, "selected.png")
        if not os.path.isfile(path):
            path = os.path.join(ANCHORS_ROOT, sid, "candidate_0.png")
        if not os.path.isfile(path):
            missing += 1
            results.append({"id": sid, "name": name, "status": "missing_anchor"})
            print(f"MISS  {name}")
            continue

        shot_ctx = {
            "name": name,
            "shotDescription": scene.get("shotDescription"),
            "cameraAngle": scene.get("cameraAngle"),
        }
        verdict = audit_anchor(path, DEFAULT_CHARACTER_RULES, shot_ctx)
        entry = {"id": sid, "name": name, "anchor_path": path, **verdict}
        results.append(entry)
        ok = verdict.get("pass")
        n_v = len(verdict.get("violations") or [])
        if ok:
            passed += 1
            print(f"PASS  {name}")
        else:
            failed += 1
            codes = ",".join(v.get("code", "?") for v in (verdict.get("violations") or []))
            print(f"FAIL  {name} [{n_v}] {codes}")

    summary = {
        "total": len(scenes),
        "passed": passed,
        "failed": failed,
        "missing": missing,
        "results": results,
    }
    out_dir = os.path.join(PROJECT_ROOT, "output", "pipeline", "audits")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"anchors_regen_{int(time.time())}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"\nTotal {len(scenes)} | Pass {passed} | Fail {failed} | Missing {missing}")
    print(f"Report -> {out_path}")
    return 0 if failed == 0 and missing == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
