"""Diff Opus storyline (nested: scenes → shots) vs current TB scenes.json.

Produces a reuse/regen shopping list with cost estimate.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


ENV_MAP = {
    "INTRO":  "Rooftop Violet Sky",
    "V1":     "Neon Rain Streets",
    "V1B":    "Neon Rain Streets",
    "PC1":    "Warp Signal Sky",
    "C1":     "Shatter City",
    "V2":     "Neon Rain Streets",
    "PC2":    "Warp Signal Sky",
    "C2":     "Shatter City",
    "B":      "Dissolving City Data Streams",
    "BR":     "Dissolving City Data Streams",
    "FN":     "Rebuilt Rooftop Aurora",
    "OUTRO":  "Rebuilt Rooftop Aurora",
}


def normalize_shot_size(description: str) -> str:
    s = (description or "").lower()
    if "extreme close" in s or "ecu" in s or "close-up" in s: return "close"
    if "close" in s: return "close"
    if "wide" in s or "establishing" in s or "master" in s: return "wide"
    if "medium" in s or "two-shot" in s or "over-shoulder" in s or "ots" in s: return "medium"
    return "medium"


def current_beat_id(name: str) -> str:
    m = re.match(r"^(\d+\.\d+)", name or "")
    return m.group(1) if m else ""


def current_env_code(name: str) -> str:
    parts = (name or "").split()
    for p in parts[1:]:
        if p.upper() in ENV_MAP:
            return p.upper()
    return ""


def main() -> None:
    out_dir = ROOT / "output/pipeline/opus_storylines"
    if len(sys.argv) >= 2:
        plan_path = Path(sys.argv[1])
    else:
        finals = sorted(out_dir.glob("plan_final_*.json"))
        plans = sorted(out_dir.glob("plan_2*.json"))
        plan_path = finals[-1] if finals else plans[-1]

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    opus_scenes = plan.get("scenes") or []
    # Flatten to shots for matching
    opus_shots: list[tuple[dict, dict]] = []  # (scene, shot)
    for sc in opus_scenes:
        for sh in (sc.get("shots") or []):
            opus_shots.append((sc, sh))

    cur = json.loads((ROOT / "output/projects/default/prompt_os/scenes.json").read_text(encoding="utf-8"))
    cur_list = cur if isinstance(cur, list) else (cur.get("scenes") or [])

    anchor_root = ROOT / "output/pipeline/anchors_v6"
    clip_root = ROOT / "output/pipeline/clips_v6"

    def has_anchor(sid: str) -> bool:
        return (anchor_root / sid / "selected.png").exists()

    def has_clip(sid: str) -> bool:
        return (clip_root / sid / "selected.mp4").exists()

    # Index current scenes by env + normalized shot size for best-fit matching
    cur_indexed = []
    for c in cur_list:
        env_code = current_env_code(c.get("name", ""))
        env_name = ENV_MAP.get(env_code, "")
        size = normalize_shot_size(c.get("cameraAngle", "") + " " + c.get("shotDescription", "") + " " + c.get("name", ""))
        cur_indexed.append({
            "cur": c,
            "env_name": env_name,
            "size": size,
            "beat": current_beat_id(c.get("name", "")),
            "claimed": False,
        })

    print("=" * 110)
    print(f"PLAN:      {plan_path.name}")
    print(f"OPUS:      {len(opus_scenes)} scenes / {len(opus_shots)} shots")
    print(f"CURRENT:   {len(cur_list)} scenes")
    print("=" * 110)
    print()

    reuse = []
    regen_kling_only = []
    regen_anchor_kling = []
    new_shots = []

    print(f"{'OPUS':<5} {'LOC':<26} {'SIZE':<6} {'STATUS':<14} {'MATCH':<17} {'NOTE'}")
    print("-" * 110)

    for sc, sh in opus_shots:
        oid = sh.get("id", "?")
        oloc = sc.get("location", "?")
        osize = sh.get("shot_size", "?")

        # Try to find a best-match current scene: same env + same size, not yet claimed
        best = None
        for idx in cur_indexed:
            if idx["claimed"]:
                continue
            if idx["env_name"] == oloc and idx["size"] == osize:
                best = idx
                break
        # Second pass: same env, size mismatch (anchor regen candidate)
        if best is None:
            for idx in cur_indexed:
                if idx["claimed"]:
                    continue
                if idx["env_name"] == oloc:
                    best = idx
                    break

        if best is None:
            new_shots.append((sc, sh))
            print(f"{oid:<5} {oloc[:26]:<26} {osize:<6} {'NEW':<14} {'-':<17} fresh shot — no current to derive from")
            continue

        best["claimed"] = True
        c = best["cur"]
        a = has_anchor(c["id"])
        k = has_clip(c["id"])
        env_match = (best["env_name"] == oloc)
        size_match = (best["size"] == osize)

        flags = ("a" if a else "") + ("k" if k else "")
        flag_str = f"({flags})" if flags else "()"

        if env_match and size_match and k:
            status = "REUSE"
            reuse.append((sc, sh, c))
        elif env_match and size_match and a and not k:
            status = "RENDER_KLING"
            regen_kling_only.append((sc, sh, c))
        elif env_match and not size_match:
            status = "REGEN_ANCHOR"
            regen_anchor_kling.append((sc, sh, c, f"size {best['size']}→{osize}"))
        else:
            status = "REGEN_ANCHOR"
            regen_anchor_kling.append((sc, sh, c, f"env mismatch"))

        print(f"{oid:<5} {oloc[:26]:<26} {osize:<6} {status:<14} {c['id'][:13]+flag_str:<17} size={best['size']}/{osize}")

    print()
    print("=" * 110)
    print(f"REUSE (clip already rendered + matches):     {len(reuse)}")
    print(f"RENDER_KLING (anchor ok, Kling only):        {len(regen_kling_only)}")
    print(f"REGEN_ANCHOR (both anchor + Kling):          {len(regen_anchor_kling)}")
    print(f"NEW (no current match, full gen):            {len(new_shots)}")
    print(f"TOTAL OPUS SHOTS:                            {len(opus_shots)}")
    print("=" * 110)

    # Cost math — Kling v3_standard ≈ $0.58/clip, anchor gen ≈ $0.04
    kling_cost_per = 0.58
    anchor_cost_per = 0.04
    kling_clips = len(regen_kling_only) + len(regen_anchor_kling) + len(new_shots)
    anchor_images = len(regen_anchor_kling) + len(new_shots)
    total = kling_clips * kling_cost_per + anchor_images * anchor_cost_per
    print(f"Kling clips to render: {kling_clips} × ${kling_cost_per:.2f} = ${kling_clips*kling_cost_per:.2f}")
    print(f"Anchors to generate:   {anchor_images} × ${anchor_cost_per:.2f} = ${anchor_images*anchor_cost_per:.2f}")
    print(f"TOTAL ESTIMATED COST:  ~${total:.2f}")
    print("=" * 110)

    # Shopping list report
    report = {
        "plan_file": plan_path.name,
        "opus_scene_count": len(opus_scenes),
        "opus_shot_count": len(opus_shots),
        "current_scene_count": len(cur_list),
        "reuse": [
            {"shot_id": sh["id"], "location": sc["location"], "shot_size": sh["shot_size"],
             "cur_id": c["id"], "cur_name": c.get("name")}
            for sc, sh, c in reuse
        ],
        "render_kling_only": [
            {"shot_id": sh["id"], "location": sc["location"], "shot_size": sh["shot_size"],
             "cur_id": c["id"], "cur_name": c.get("name")}
            for sc, sh, c in regen_kling_only
        ],
        "regen_anchor_and_kling": [
            {"shot_id": sh["id"], "location": sc["location"], "shot_size": sh["shot_size"],
             "cur_id": c["id"], "cur_name": c.get("name"), "why": why}
            for sc, sh, c, why in regen_anchor_kling
        ],
        "new_shots": [
            {"shot_id": sh["id"], "location": sc["location"], "shot_size": sh["shot_size"],
             "beat_name": sc.get("beat_name"),
             "acting": sh.get("acting"), "camera": sh.get("camera")}
            for sc, sh in new_shots
        ],
        "cost_usd": round(total, 2),
        "kling_clips": kling_clips,
        "anchors": anchor_images,
    }
    out_path = out_dir / f"shopping_{plan_path.stem}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"→ {out_path}")


if __name__ == "__main__":
    main()
