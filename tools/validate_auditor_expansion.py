"""Validate the expanded anchor_auditor against the 4 known-bad mid-clip
frames from the TB MV.

Known failures the prior auditor missed:
  - Shot 00 (1.1 INTRO establishing)      → back-of-head emblem
  - Shot 08 (4.2 C1 flying debris)        → pupil-clone faces
  - Shot 09 (4.3 C1 memory close-up)      → hallucinated human face
  - Shot 15 (7.1 C2 POV flight)           → back-of-head + duplicate emblem

Expected after expansion:
  - shot 00 → violations contain 'emblem_on_back_of_head' or 'emblem_on_wrong_part'
  - shot 08 → violations contain 'pupil_content_error'
  - shot 09 → violations contain 'hallucinated_character'
  - shot 15 → violations contain 'emblem_on_back_of_head' and/or 'multiple_emblems'
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.anchor_auditor import audit_anchor

FRAMES = {
    "00": {
        "path": "tools/_clip_audit/00_58a4cf26-a6e_1.1_INTRO_establishing_mid.png",
        "shot_id": "58a4cf26-a6e",
        "name": "1.1 INTRO establishing",
        "shotDescription": "dolly push-in toward chibi bear on Shinjuku rooftop at dusk, aurora blooms in sky",
        "cameraAngle": "wide establishing",
        "expected_violation_codes": {"emblem_on_back_of_head", "emblem_on_wrong_part"},
    },
    "08": {
        "path": "tools/_clip_audit/08_7d8fbecb-995_4.2_C1_flying_debris_mid.png",
        "shot_id": "7d8fbecb-995",
        "name": "4.2 C1 flying debris",
        "shotDescription": "chibi bear floats through flying debris, emotional chorus energy",
        "cameraAngle": "medium",
        "expected_violation_codes": {"pupil_content_error"},
    },
    "09": {
        "path": "tools/_clip_audit/09_53edc7ea-498_4.3_C1_memory_close-up_mid.png",
        "shot_id": "53edc7ea-498",
        "name": "4.3 C1 memory close-up",
        "shotDescription": "close-up on chibi bear face, memory shards drifting past",
        "cameraAngle": "close-up",
        "expected_violation_codes": {"hallucinated_character"},
    },
    "15": {
        "path": "tools/_clip_audit/15_d6bd0da5-287_7.1_C2_POV_flight_mid.png",
        "shot_id": "d6bd0da5-287",
        "name": "7.1 C2 POV flight",
        "shotDescription": "chibi bear flies through neon portal, POV momentum",
        "cameraAngle": "medium tracking",
        "expected_violation_codes": {"emblem_on_back_of_head", "multiple_emblems", "emblem_on_wrong_part"},
    },
}


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    results = {}
    all_ok = True
    for key, spec in FRAMES.items():
        frame_path = os.path.join(root, spec["path"])
        if not os.path.isfile(frame_path):
            print(f"[{key}] MISSING FRAME: {frame_path}")
            results[key] = {"status": "missing"}
            all_ok = False
            continue
        print(f"\n[{key}] auditing {spec['name']} ...")
        shot_context = {
            "name": spec["name"],
            "shotDescription": spec["shotDescription"],
            "cameraAngle": spec["cameraAngle"],
        }
        verdict = audit_anchor(frame_path, shot_context=shot_context)
        codes = {v.get("code") for v in verdict.get("violations", []) if isinstance(v, dict)}
        expected = spec["expected_violation_codes"]
        hit = codes & expected
        passed_check = bool(hit)
        if not passed_check:
            all_ok = False
        print(f"  pass flag: {verdict.get('pass')}")
        print(f"  summary: {verdict.get('summary')}")
        print(f"  violation codes: {sorted(codes)}")
        print(f"  expected one of: {sorted(expected)}")
        print(f"  >>> {'OK: caught expected class' if passed_check else 'MISS: did not raise expected code'}")
        boh = verdict.get("back_of_head_emblem_scan") or {}
        pcs = verdict.get("pupil_content_scan") or {}
        hcs = verdict.get("hallucinated_character_scan") or {}
        print(f"  scans: boh.performed={boh.get('performed')} (found={boh.get('emblem_found_on_back')}), "
              f"pupil.performed={pcs.get('performed')} (ok={pcs.get('pupil_ok')}), "
              f"hallucination.performed={hcs.get('performed')} (extra={hcs.get('extra_figures_found')})")
        results[key] = {
            "violations": sorted(codes),
            "expected_hit": list(hit),
            "passed_check": passed_check,
            "verdict": verdict,
        }
    out_path = os.path.join(root, "tools", "_clip_audit", "expansion_validation.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nfull results saved: {out_path}")
    print(f"\nOVERALL: {'ALL 4 CAUGHT' if all_ok else 'SOME MISSED — auditor still gapped'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
