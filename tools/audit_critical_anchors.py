"""Re-audit the 12 critical-error anchors to separate 'fix the anchor'
from 'Kling drift - just re-render the clip'.

For each critical shot, audits the anchor PNG at
output/pipeline/anchors_v6/<shot_id>/selected.png and compares its violation
codes to the clip's violation codes. If the anchor already has the same
violation, regen the anchor. If the anchor is clean, re-render the clip.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.anchor_auditor import audit_anchor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CLIPS_AUDIT = os.path.join(ROOT, "tools", "_clip_audit", "clips_audit.json")
ANCHORS_DIR = os.path.join(ROOT, "output", "pipeline", "anchors_v6")
SCENES_PATH = os.path.join(ROOT, "output", "projects", "default", "prompt_os", "scenes.json")
OUT_PATH = os.path.join(ROOT, "tools", "_clip_audit", "critical_anchor_audit.json")

# Codes that represent true identity/continuity errors (not coverage/pose noise)
CRITICAL_CODES = {
    "emblem_on_back_of_head",
    "emblem_on_wrong_part",
    "emblem_when_forehead_hidden",
    "moon_in_sky",
    "multiple_emblems",
    "pupil_content_error",
    "hallucinated_character",
    "duplicate_character",
    "eye_color_wrong",
    "proportions_drift",
    "hood_state_wrong",
}


def load_scenes() -> list[dict]:
    with open(SCENES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    with open(CLIPS_AUDIT, "r", encoding="utf-8") as f:
        clips = json.load(f)

    # Build shot_id -> clip-level critical violations
    clip_crits: dict[str, list[str]] = {}
    for r in clips.get("results", []):
        sid = r.get("shot_id") or ""
        codes = r.get("violation_codes") or []
        crits = [c for c in codes if c in CRITICAL_CODES]
        if crits:
            clip_crits[sid] = crits

    if not clip_crits:
        print("No critical clip failures found.")
        return 0

    scenes_by_id = {s["id"]: s for s in load_scenes()}
    report: list[dict] = []

    for sid, clip_codes in clip_crits.items():
        scene = scenes_by_id.get(sid) or {}
        name = scene.get("name", "?")
        anchor = os.path.join(ANCHORS_DIR, sid, "selected.png")
        if not os.path.isfile(anchor):
            print(f"[{sid[:11]}] {name}  ANCHOR MISSING: {anchor}")
            report.append({
                "shot_id": sid,
                "name": name,
                "clip_critical_codes": clip_codes,
                "anchor_status": "missing",
                "recommendation": "skip — no anchor on disk",
            })
            continue

        shot_context = {
            "name": name,
            "shotDescription": scene.get("shotDescription", ""),
            "cameraAngle": scene.get("cameraAngle", ""),
        }

        t0 = time.time()
        verdict = audit_anchor(anchor, shot_context=shot_context)
        dt = time.time() - t0

        anchor_codes = [v.get("code") for v in verdict.get("violations", []) if isinstance(v, dict)]
        anchor_crits = [c for c in anchor_codes if c in CRITICAL_CODES]
        shared = sorted(set(clip_codes) & set(anchor_crits))
        only_in_clip = sorted(set(clip_codes) - set(anchor_crits))

        if shared:
            recommendation = "REGEN ANCHOR — failure exists in source still, will persist through Kling"
        elif only_in_clip:
            recommendation = "RE-RENDER CLIP — anchor is clean, Kling drifted; use same anchor with tighter prompt/seed"
        else:
            recommendation = "CHECK — clip flagged critical but anchor clean; mixed/unclear case"

        print(f"[{sid[:11]}] {name} ({dt:.1f}s)")
        print(f"  clip critical : {clip_codes}")
        print(f"  anchor critical: {anchor_crits or '(clean)'}")
        print(f"  shared         : {shared or '(none)'}")
        print(f"  -> {recommendation}")
        print()

        report.append({
            "shot_id": sid,
            "name": name,
            "clip_critical_codes": clip_codes,
            "anchor_critical_codes": anchor_crits,
            "shared_codes": shared,
            "only_in_clip": only_in_clip,
            "recommendation": recommendation,
            "anchor_pass": verdict.get("pass"),
            "anchor_summary": verdict.get("summary"),
            "anchor_seconds": round(dt, 1),
        })

    regen_anchor = [r for r in report if r["recommendation"].startswith("REGEN ANCHOR")]
    rerender_clip = [r for r in report if r["recommendation"].startswith("RE-RENDER CLIP")]
    check_mixed = [r for r in report if r["recommendation"].startswith("CHECK")]

    summary = {
        "total_critical_shots": len(clip_crits),
        "regen_anchor": [r["shot_id"] for r in regen_anchor],
        "rerender_clip": [r["shot_id"] for r in rerender_clip],
        "check_mixed": [r["shot_id"] for r in check_mixed],
        "details": report,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 70)
    print(f"TOTAL CRITICAL CLIP FAILURES: {len(clip_crits)}")
    print(f"  REGEN ANCHOR    : {len(regen_anchor)}")
    print(f"  RE-RENDER CLIP  : {len(rerender_clip)}")
    print(f"  CHECK (mixed)   : {len(check_mixed)}")
    print(f"\nFull report: {OUT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
