"""Rebuild scenes.json from the Opus plan_final, preserving REUSE scene IDs.

Reads:
  output/pipeline/opus_storylines/plan_final_<stamp>.json
  output/pipeline/opus_storylines/shopping_plan_final_<stamp>.json

Writes (after backing up):
  output/projects/default/prompt_os/scenes.json  — now 30 rows, one per Opus shot
  output/projects/default/prompt_os/scenes.backup_<stamp>.json
"""
from __future__ import annotations
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCENES_PATH = ROOT / "output/projects/default/prompt_os/scenes.json"
ENVS_PATH = ROOT / "output/projects/default/prompt_os/environments.json"
CHARS_PATH = ROOT / "output/projects/default/prompt_os/characters.json"
PLAN_DIR = ROOT / "output/pipeline/opus_storylines"


SHOT_SIZE_TO_CAMERA = {
    "close":  "close-up",
    "medium": "medium shot",
    "wide":   "wide establishing",
}

BEAT_TO_TYPE = {
    "Opening Image / Setup": "intro",
    "Theme Stated": "setup",
    "Catalyst / Call-to-Signal": "inciting",
    "Midpoint / Chaos Peak": "climax",
    "Dark Moment": "low",
    "Rebirth / Resolution — Pivot": "pivot",
    "Rebirth / Resolution — Build": "resolution",
    "Final Image / Outro": "outro",
}


def _gen_id() -> str:
    return str(uuid.uuid4())[:12]


def main() -> None:
    # Pick latest plan_final + matching shopping list
    finals = sorted(PLAN_DIR.glob("plan_final_*.json"))
    if not finals:
        print("no plan_final found")
        sys.exit(1)
    plan_path = finals[-1]
    stamp = plan_path.stem.replace("plan_final_", "")
    shopping_path = PLAN_DIR / f"shopping_plan_final_{stamp}.json"
    if not shopping_path.exists():
        print(f"no shopping list: {shopping_path}")
        sys.exit(1)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    shopping = json.loads(shopping_path.read_text(encoding="utf-8"))

    envs = json.loads(ENVS_PATH.read_text(encoding="utf-8"))
    env_by_name = {e["name"]: e["id"] for e in envs}

    chars = json.loads(CHARS_PATH.read_text(encoding="utf-8"))
    char_list = chars if isinstance(chars, list) else (chars.get("characters") or [])
    tb_id = next((c["id"] for c in char_list if "Trillion" in (c.get("name") or "") or "TB" in (c.get("name") or "")), None)
    if not tb_id:
        print("ERR: TB character not found in characters.json")
        sys.exit(1)
    print(f"TB character id: {tb_id}")

    # shot_id → (preserved_cur_id or None)
    preserve = {}
    for row in shopping.get("reuse", []):
        preserve[row["shot_id"]] = row["cur_id"]
    for row in shopping.get("render_kling_only", []):
        preserve[row["shot_id"]] = row["cur_id"]
    for row in shopping.get("regen_anchor_and_kling", []):
        preserve[row["shot_id"]] = row["cur_id"]
    # NEW shots get fresh IDs

    # Backup
    backup_path = SCENES_PATH.parent / f"scenes.backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.write_text(SCENES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"backup → {backup_path.name}")

    new_scenes = []
    order = 0
    for sc in plan.get("scenes", []):
        beat_name = sc.get("beat_name", "")
        scene_type = BEAT_TO_TYPE.get(beat_name, "")
        cont = sc.get("continuity_anchors") or {}
        env_id = env_by_name.get(sc.get("location", ""), "")
        if not env_id:
            print(f"WARN: no env id for {sc.get('location')!r}")

        for sh in sc.get("shots", []):
            oid = sh.get("id", "?")
            scene_id = preserve.get(oid) or _gen_id()
            shot_size = sh.get("shot_size", "medium")
            camera_angle = SHOT_SIZE_TO_CAMERA.get(shot_size, shot_size)

            # Compose shot description from Opus fields
            desc_parts = []
            if sh.get("acting"):
                desc_parts.append(sh["acting"])
            if sh.get("micro_expression"):
                desc_parts.append(f"micro: {sh['micro_expression']}")
            if cont.get("wardrobe"):
                desc_parts.append(f"wardrobe: {cont['wardrobe']}")
            if cont.get("eyeline_target"):
                desc_parts.append(f"eyeline: {cont['eyeline_target']}")
            shot_description = ". ".join(desc_parts)[:700]

            notes_parts = []
            if sh.get("purpose"):
                notes_parts.append(f"purpose: {sh['purpose']}")
            if sh.get("continuity_in"):
                notes_parts.append(f"cont_in: {sh['continuity_in']}")
            if sh.get("continuity_out"):
                notes_parts.append(f"cont_out: {sh['continuity_out']}")
            if sh.get("transition_in"):
                notes_parts.append(f"transition_in: {sh['transition_in']}")
            if sh.get("kling_prompt_note"):
                notes_parts.append(f"KLING NOTE: {sh['kling_prompt_note']}")
            if sh.get("dissolve_spec"):
                notes_parts.append(f"DISSOLVE: {sh['dissolve_spec']}")
            notes = "\n".join(notes_parts)

            tags = [f"opus_{oid}", f"scene_{sc.get('id','?')}", shot_size]
            if beat_name:
                tags.append(beat_name.split(" /")[0].lower().replace(" ", "_"))

            new_scene = {
                "id": scene_id,
                "name": f"{oid} {sc.get('location','')} {shot_size}",
                "promptId": "",
                "characterId": tb_id,
                "costumeId": "",
                "environmentId": env_id,
                "shotDescription": shot_description,
                "cameraAngle": camera_angle,
                "cameraMovement": sh.get("camera", ""),
                "duration": int(round(sh.get("duration_s") or 5)),
                "orderIndex": order,
                "tags": tags,
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "notes": notes,
                "sceneType": scene_type,
                "narrativeIntent": sc.get("dramatic_action", "")[:300],
                "emotion": sc.get("emotion", ""),
                "energy": 5,
                "opus_shot_id": oid,
                "opus_scene_id": sc.get("id", ""),
                "opus_beat_name": beat_name,
                "opus_time_start": sc.get("time_start"),
                "opus_time_end": sc.get("time_end"),
                "opus_lyric_anchor": sc.get("lyric_anchor", ""),
            }
            new_scenes.append(new_scene)
            order += 1

    SCENES_PATH.write_text(json.dumps(new_scenes, indent=2), encoding="utf-8")
    print()
    print("=" * 80)
    print(f"wrote {len(new_scenes)} scenes → {SCENES_PATH.name}")
    reuse_count = sum(1 for sc in new_scenes if sc["id"] in preserve.values())
    new_count = len(new_scenes) - reuse_count
    print(f"  preserved ids: {reuse_count}")
    print(f"  new ids:       {new_count}")
    print("=" * 80)


if __name__ == "__main__":
    main()
