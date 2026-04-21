"""Backfill coverageTier + sceneGroupId on existing scenes.json rows.

Groups by `opus_scene_id` (falling back to `id`) and infers the tier from the
shot sizes actually present. Writes back to scenes.json after making a
timestamped backup. Idempotent — re-runs recompute and overwrite cleanly.
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.coverage import (  # noqa: E402
    DEFAULT_TIER,
    group_key,
    group_scenes,
    infer_tier,
    validate_tier,
)

SCENES_PATH = ROOT / "output/projects/default/prompt_os/scenes.json"


def main(path: Path = SCENES_PATH, dry: bool = False) -> None:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        print(f"unexpected shape in {path}")
        sys.exit(1)

    grouped = group_scenes(rows)
    tier_by_group = {gk: infer_tier(members) for gk, members in grouped.items()}

    changed = 0
    for r in rows:
        gk = group_key(r)
        new_tier = validate_tier(r.get("coverageTier") or tier_by_group.get(gk, DEFAULT_TIER))
        new_group = r.get("sceneGroupId") or gk
        if r.get("coverageTier") != new_tier or r.get("sceneGroupId") != new_group:
            r["coverageTier"] = new_tier
            r["sceneGroupId"] = new_group
            changed += 1

    print(f"groups: {len(grouped)}")
    for gk, members in sorted(grouped.items(), key=lambda kv: (kv[1][0].get('orderIndex') or 0)):
        sizes = sorted({m.get('cameraAngle', '').split()[0] for m in members if m.get('cameraAngle')})
        print(f"  {gk:>3} → {tier_by_group[gk]:<10} ({len(members)} shots, sizes={sizes})")
    print(f"rows updated: {changed}/{len(rows)}")

    if dry:
        print("DRY RUN — no write")
        return

    if changed:
        backup = path.with_name(f"scenes.backup_coverage_{time.strftime('%Y%m%d_%H%M%S')}.json")
        backup.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"wrote {path.name}; backup → {backup.name}")
    else:
        print("no changes, skipping write")


if __name__ == "__main__":
    main(dry=("--dry" in sys.argv))
