"""
Prompt Compiler — translates canonical video_plan into model-ready API payloads.

Reads:
  - video_plan_v2.json (story intent, three-act prompts, anchor descriptions)
  - model_profile.json (what the current engine supports)
  - packages.json (character/environment asset paths)

Outputs:
  - Anchor generation payloads (for Gemini 3.1 Flash)
  - Video generation payloads (for Kling 3.0 / any fal.ai model)

The plan holds INTENT. The profile holds CAPABILITIES. This compiler
bridges them — maximizing what the model can do, gracefully dropping
what it can't.
"""

import json
import os


def load_json(path):
    with open(path) as f:
        return json.load(f)


def _resolve_pkg_path(pkg_id, packages):
    """Get hero_image_path for a package ID."""
    for pkg in packages.get("packages", []):
        if pkg["package_id"] == pkg_id:
            return pkg.get("hero_image_path", "")
    return ""


def _resolve_pkg_name(pkg_id, packages):
    """Get name for a package ID."""
    for pkg in packages.get("packages", []):
        if pkg["package_id"] == pkg_id:
            return pkg.get("name", pkg_id)
    return pkg_id


# ---------------------------------------------------------------------------
# Anchor payloads (image generation)
# ---------------------------------------------------------------------------

def compile_anchor_payloads(plan, profile, packages):
    """Generate image generation payloads for all start + end anchors.

    Returns list of dicts:
    [{
        "shot_id": "shot_00",
        "anchor_type": "start" | "end",
        "prompt": "...",
        "reference_image_paths": ["path1", "path2", ...],
        "resolution": "1K",
        "output_path": "output/pipeline/anchors_v2/shot_00_start.png"
    }, ...]
    """
    img_profile = profile.get("image_engine", {})
    max_refs = img_profile.get("max_ref_images", 10)
    resolution = img_profile.get("default_resolution", "1K")

    payloads = []

    for shot in plan.get("shots", []):
        shot_id = shot["shot_id"]

        # Start anchor
        start_refs = []
        for pkg_id in shot.get("start_anchor_refs", []):
            path = _resolve_pkg_path(pkg_id, packages)
            if path and os.path.isfile(path):
                start_refs.append(path)

        if shot.get("start_anchor_prompt"):
            payloads.append({
                "shot_id": shot_id,
                "anchor_type": "start",
                "prompt": shot["start_anchor_prompt"],
                "reference_image_paths": start_refs[:max_refs],
                "resolution": resolution,
                "output_path": f"output/pipeline/anchors_v2/{shot_id}_start.png",
            })

        # End anchor (only if model supports last frame)
        vid_profile = profile.get("video_engine", {})
        if vid_profile.get("supports_last_frame") and shot.get("end_anchor_prompt"):
            end_refs = []
            for pkg_id in shot.get("end_anchor_refs", []):
                path = _resolve_pkg_path(pkg_id, packages)
                if path and os.path.isfile(path):
                    end_refs.append(path)

            payloads.append({
                "shot_id": shot_id,
                "anchor_type": "end",
                "prompt": shot["end_anchor_prompt"],
                "reference_image_paths": end_refs[:max_refs],
                "resolution": resolution,
                "output_path": f"output/pipeline/anchors_v2/{shot_id}_end.png",
            })

    return payloads


# ---------------------------------------------------------------------------
# Video payloads (video generation)
# ---------------------------------------------------------------------------

def compile_video_payloads(plan, profile, packages, tier="review",
                           anchors_dir="output/pipeline/anchors_v2"):
    """Generate video generation payloads for all shots.

    Args:
        tier: "draft" | "review" | "final" — selects engine from profile
        anchors_dir: where anchor images live

    Returns list of dicts ready for fal_client:
    [{
        "shot_id": "shot_00",
        "start_image_path": "path/to/start_anchor.png",
        "end_image_path": "path/to/end_anchor.png" or None,
        "prompt": "compiled motion prompt",
        "duration": 7,
        "tier": "v3_standard",
        "elements": [...] or None,
        "negative_prompt": "...",
        "generate_audio": false,
    }, ...]
    """
    # Resolve which engine tier to use
    tier_key = profile.get("tiers", {}).get(tier, "video_engine")
    vid_profile = profile.get(tier_key, profile.get("video_engine", {}))

    engine_id = vid_profile.get("id", "kling_v3_pro")
    supports_last = vid_profile.get("supports_last_frame", False)
    supports_elements = vid_profile.get("supports_elements", False)
    max_elements = vid_profile.get("max_elements", 4)
    supports_neg = vid_profile.get("supports_negative_prompt", False)
    max_dur = vid_profile.get("max_duration", 15)
    min_dur = vid_profile.get("min_duration", 3)
    max_chars = vid_profile.get("prompt_max_chars", 2000)

    # Map engine_id to fal_client tier name
    tier_map = {
        "kling_v3_standard": "v3_standard",
        "kling_v3_pro": "v3_pro",
        "kling_o3_standard": "o3_standard",
        "kling_o3_pro": "o3_pro",
    }
    fal_tier = tier_map.get(engine_id, "v3_standard")

    payloads = []

    for shot in plan.get("shots", []):
        shot_id = shot["shot_id"]
        duration = max(min_dur, min(max_dur, shot.get("duration", 5)))

        # Start anchor
        start_path = os.path.join(anchors_dir, f"{shot_id}_start.png")
        if not os.path.isfile(start_path):
            # Fallback to v1 anchors
            start_path = f"output/pipeline/anchors/{shot_id}.png"

        # End anchor (if supported)
        end_path = None
        if supports_last:
            ep = os.path.join(anchors_dir, f"{shot_id}_end.png")
            if os.path.isfile(ep):
                end_path = ep

        # Video prompt (truncate to model limit)
        prompt = shot.get("video_prompt", "")[:max_chars]

        # Elements (character refs for consistency)
        elements = None
        if supports_elements and shot.get("elements"):
            elements = []
            for elem in shot["elements"][:max_elements]:
                pkg_id = elem.get("pkg", "")
                frontal = _resolve_pkg_path(pkg_id, packages)
                if frontal and os.path.isfile(frontal):
                    elements.append({
                        "frontal_image_path": frontal,
                        "reference_image_paths": [],
                    })

        # Negative prompt
        neg = shot.get("negative_prompt", "") if supports_neg else None

        payload = {
            "shot_id": shot_id,
            "moment": shot.get("moment", ""),
            "start_image_path": start_path,
            "end_image_path": end_path,
            "prompt": prompt,
            "duration": duration,
            "tier": fal_tier,
            "elements": elements,
            "negative_prompt": neg,
            "generate_audio": True,
        }

        payloads.append(payload)

    return payloads


# ---------------------------------------------------------------------------
# Multi-shot payloads (optional grouping)
# ---------------------------------------------------------------------------

def compile_multishot_payloads(plan, profile, packages, tier="review",
                                anchors_dir="output/pipeline/anchors_v2"):
    """Group shots into multi-shot calls based on plan strategy.

    Only works if the model supports multi_prompt.
    Falls back to individual shots if not supported.
    """
    tier_key = profile.get("tiers", {}).get(tier, "video_engine")
    vid_profile = profile.get(tier_key, profile.get("video_engine", {}))

    if not vid_profile.get("supports_multi_shot", False):
        print("[Compiler] Model doesn't support multi-shot, using individual shots")
        return compile_video_payloads(plan, profile, packages, tier, anchors_dir)

    strategy = plan.get("multi_shot_strategy", {})
    pairs = strategy.get("pairs", [])

    if not pairs:
        return compile_video_payloads(plan, profile, packages, tier, anchors_dir)

    # Build shot lookup
    shot_map = {s["shot_id"]: s for s in plan.get("shots", [])}

    engine_id = vid_profile.get("id", "kling_v3_pro")
    tier_map = {
        "kling_v3_standard": "v3_standard",
        "kling_v3_pro": "v3_pro",
        "kling_o3_standard": "o3_standard",
        "kling_o3_pro": "o3_pro",
    }
    fal_tier = tier_map.get(engine_id, "v3_standard")
    max_chars = vid_profile.get("prompt_max_chars", 2000)

    payloads = []

    for group in pairs:
        shot_ids = group.get("shots", [])
        if len(shot_ids) == 1:
            # Single shot — use normal payload
            individual = compile_video_payloads(
                {"shots": [shot_map[shot_ids[0]]]},
                profile, packages, tier, anchors_dir
            )
            payloads.extend(individual)
            continue

        # Multi-shot group
        first_shot = shot_map[shot_ids[0]]
        start_path = os.path.join(anchors_dir, f"{first_shot['shot_id']}_start.png")
        if not os.path.isfile(start_path):
            start_path = f"output/pipeline/anchors/{first_shot['shot_id']}.png"

        multi_prompt = []
        for sid in shot_ids:
            shot = shot_map[sid]
            prompt = shot.get("video_prompt", "")[:max_chars]
            dur = str(shot.get("duration", 5))
            multi_prompt.append({"prompt": prompt, "duration": dur})

        # Elements from first shot (applies to whole sequence)
        elements = None
        supports_elements = vid_profile.get("supports_elements", False)
        if supports_elements:
            all_elements = []
            seen_pkgs = set()
            for sid in shot_ids:
                for elem in shot_map[sid].get("elements", []):
                    pkg_id = elem.get("pkg", "")
                    if pkg_id not in seen_pkgs:
                        frontal = _resolve_pkg_path(pkg_id, packages)
                        if frontal and os.path.isfile(frontal):
                            all_elements.append({
                                "frontal_image_path": frontal,
                                "reference_image_paths": [],
                            })
                            seen_pkgs.add(pkg_id)
            elements = all_elements[:vid_profile.get("max_elements", 4)] or None

        payload = {
            "shot_ids": shot_ids,
            "moment": group.get("reason", ""),
            "start_image_path": start_path,
            "end_image_path": None,  # multi-shot doesn't use end frame well
            "multi_prompt": multi_prompt,
            "duration": group.get("duration", 10),
            "tier": fal_tier,
            "elements": elements,
            "negative_prompt": "blur, distortion, low quality, watermark",
            "generate_audio": True,
            "is_multi_shot": True,
        }

        payloads.append(payload)

    return payloads


# ---------------------------------------------------------------------------
# Summary / cost estimation
# ---------------------------------------------------------------------------

def estimate_cost(plan, profile, tier="review"):
    """Estimate total cost for a full pipeline run."""
    tier_key = profile.get("tiers", {}).get(tier, "video_engine")
    vid_profile = profile.get(tier_key, profile.get("video_engine", {}))
    img_profile = profile.get("image_engine", {})

    vid_cost_per_sec = vid_profile.get("cost_per_sec", 0.20)
    img_cost = img_profile.get("cost_per_image", 0.08)

    shots = plan.get("shots", [])
    total_duration = sum(s.get("duration", 5) for s in shots)

    # Count anchors: start + end per shot
    has_last = vid_profile.get("supports_last_frame", False)
    anchor_count = len(shots) * (2 if has_last else 1)

    # Sheets (already generated, but for reference)
    sheet_count = 4  # 3 characters + 1 environment

    video_cost = total_duration * vid_cost_per_sec
    anchor_cost = anchor_count * img_cost
    sheet_cost = sheet_count * img_cost

    return {
        "tier": tier,
        "engine": vid_profile.get("id", "unknown"),
        "total_duration_sec": total_duration,
        "video_cost": round(video_cost, 2),
        "anchor_count": anchor_count,
        "anchor_cost": round(anchor_cost, 2),
        "sheet_cost": round(sheet_cost, 2),
        "total_cost": round(video_cost + anchor_cost + sheet_cost, 2),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(base)

    plan = load_json("output/pipeline/video_plan_v2.json")
    profile = load_json("output/pipeline/model_profile.json")
    packages = load_json("output/preproduction/packages.json")

    print("=" * 60)
    print("PROMPT COMPILER — Payload Summary")
    print("=" * 60)

    # Anchor payloads
    anchors = compile_anchor_payloads(plan, profile, packages)
    print(f"\nAnchor payloads: {len(anchors)}")
    for a in anchors:
        refs = len(a["reference_image_paths"])
        print(f"  {a['shot_id']} ({a['anchor_type']}): {refs} refs, "
              f"prompt={len(a['prompt'])}chars")

    # Video payloads per tier
    for tier in ["draft", "review", "final"]:
        print(f"\n--- {tier.upper()} tier ---")
        videos = compile_video_payloads(plan, profile, packages, tier)
        for v in videos:
            has_end = "+" if v.get("end_image_path") else "-"
            has_elem = len(v.get("elements") or [])
            print(f"  {v['shot_id']}: {v['duration']}s, "
                  f"end_frame={has_end}, elements={has_elem}, "
                  f"tier={v['tier']}")

        cost = estimate_cost(plan, profile, tier)
        print(f"  Cost: ${cost['total_cost']} "
              f"(video=${cost['video_cost']} + "
              f"anchors=${cost['anchor_cost']} + "
              f"sheets=${cost['sheet_cost']})")

    # Multi-shot option
    print(f"\n--- MULTI-SHOT option ---")
    multi = compile_multishot_payloads(plan, profile, packages, "review")
    for m in multi:
        if m.get("is_multi_shot"):
            print(f"  Group {m['shot_ids']}: {len(m['multi_prompt'])} shots, "
                  f"{m['duration']}s")
        else:
            print(f"  Single {m['shot_id']}: {m['duration']}s")
