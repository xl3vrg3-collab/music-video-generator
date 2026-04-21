"""Apply Opus's own critique fixes to the latest plan, save as plan_final."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_shot(plan: dict, shot_id: str) -> dict | None:
    for sc in plan.get("scenes", []):
        for sh in sc.get("shots", []):
            if sh.get("id") == shot_id:
                return sh
    return None


def main() -> None:
    out_dir = ROOT / "output/pipeline/opus_storylines"
    plans = sorted(out_dir.glob("plan_2*.json"))
    if not plans:
        print("no plan file")
        sys.exit(1)
    plan_path = plans[-1]
    stamp = plan_path.stem.split("_", 1)[1]
    crit_path = out_dir / f"critique_{stamp}.json"

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    crit = json.loads(crit_path.read_text(encoding="utf-8")) if crit_path.exists() else {}

    applied = []

    # Fix 1a: ECU → MCU for identity lock, save ECU for 3c/4c/7a/8c
    sh = find_shot(plan, "1a")
    if sh and sh.get("shot_size") == "close":
        sh["shot_size"] = "medium"
        sh["camera"] = "static locked MCU (emblem + eyes + muzzle all in frame)"
        sh["kling_prompt_note"] = (
            "Frame MUST include forehead emblem at top, mauve muzzle at bottom, "
            "eyes centered. Do NOT crop to ECU — identity lock depends on all three markers."
        )
        applied.append("1a: ECU→MCU with explicit framing spec (identity lock safety)")

    # Fix 5a: dissolve must be hard 1-2 frame crossfade, not soft blend
    sh = find_shot(plan, "5a")
    if sh:
        sh["dissolve_spec"] = "hard 1–2 frame crossfade (NOT a 12-frame soft blend); reads editorial, not VFX morph"
        applied.append("5a: dissolve spec locked to hard 1–2 frame")

    # Fix 4d: orbit competes with shards → lock static
    sh = find_shot(plan, "4d")
    if sh:
        old_cam = sh.get("camera", "")
        sh["camera"] = "static wide, TB lower-third, shards continue around locked frame"
        sh["camera_note_original"] = old_cam
        applied.append(f"4d: camera locked static (was: {old_cam[:60]})")

    # Save final plan
    final_path = out_dir / f"plan_final_{stamp}.json"
    final_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    print("=" * 80)
    print(f"Applied {len(applied)} critique fix(es):")
    for a in applied:
        print(f"  ✓ {a}")
    print()
    print(f"→ {final_path}")
    print(f"  scenes: {len(plan.get('scenes', []))}")
    print(f"  shots:  {sum(len(sc.get('shots', [])) for sc in plan.get('scenes', []))}")
    print(f"  verdict before fixes: {crit.get('verdict', '?')}")
    print(f"  highest_impact_fix:   {(crit.get('highest_impact_fix') or '?')[:120]}...")


if __name__ == "__main__":
    main()
