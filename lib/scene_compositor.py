"""
Scene Compositor (V5 Pipeline)

Composes SCENE-LEVEL anchor images from approved canonical asset sheets.

Like real filmmaking: you set up a scene (characters, environment, costumes,
props locked in), compose ONE anchor image per scene, QA/approve it, then
all shots within that scene animate from the same approved anchor.

This gives:
  - Consistency: all shots in a scene share the same character/environment look
  - Control: review 5 scene anchors instead of 20 shot frames
  - Efficiency: fewer text_to_image calls (1 per scene vs 1 per shot)

Design rule: Canonical sheets are the permanent source of truth.
Scene anchors are derived from canonical sheets and can always be recomposed.

Runway limit: max 3 @Tag references per text_to_image call.
Priority: Character > Environment > Prop > Costume
"""

import os

# ── Shot family classification ──
# Maps shot families to asset type priority weights.
# Higher weight = more likely to be included in the 3-ref limit.
SHOT_FAMILY_PRIORITY = {
    "wide_establishing": {
        "environment": 1.0,
        "character": 0.3,
        "costume": 0.1,
        "prop": 0.0,
    },
    "environment_reestablish": {
        "environment": 1.0,
        "character": 0.1,
        "costume": 0.0,
        "prop": 0.0,
    },
    "character_closeup": {
        "character": 1.0,
        "costume": 0.2,
        "environment": 0.1,
        "prop": 0.0,
    },
    "emotional_moment": {
        "character": 1.0,
        "costume": 0.5,
        "environment": 0.3,
        "prop": 0.0,
    },
    "wardrobe_reveal": {
        "costume": 1.0,
        "character": 0.8,
        "environment": 0.2,
        "prop": 0.0,
    },
    "prop_interaction": {
        "prop": 1.0,
        "character": 0.7,
        "costume": 0.3,
        "environment": 0.2,
    },
    "action_scene": {
        "character": 0.8,
        "costume": 0.7,
        "environment": 0.8,
        "prop": 0.2,
    },
    "dialogue_shot": {
        "character": 1.0,
        "costume": 0.6,
        "environment": 0.4,
        "prop": 0.0,
    },
    "insert_detail": {
        "prop": 0.8,
        "environment": 0.5,
        "character": 0.2,
        "costume": 0.1,
    },
    "generic": {
        "character": 0.7,
        "costume": 0.5,
        "environment": 0.6,
        "prop": 0.2,
    },
}

# Shot family → preferred canonical view to pull from each package type
FAMILY_VIEW_PREFERENCE = {
    # All types now use a single composite "sheet" image as the canonical ref.
    # _get_best_view falls back to hero_image_path (the sheet) if view name doesn't match.
    "character_closeup":       {"character": "sheet", "costume": "sheet"},
    "emotional_moment":        {"character": "sheet", "costume": "sheet"},
    "wide_establishing":       {"environment": "sheet", "character": "sheet"},
    "environment_reestablish": {"environment": "sheet"},
    "wardrobe_reveal":         {"costume": "sheet", "character": "sheet"},
    "action_scene":            {"character": "sheet", "environment": "sheet"},
    "dialogue_shot":           {"character": "sheet", "environment": "sheet"},
    "prop_interaction":        {"prop": "sheet", "character": "sheet"},
    "insert_detail":           {"prop": "sheet", "environment": "sheet"},
    "generic":                 {"character": "sheet", "environment": "sheet"},
}

# Tag names per asset type (Runway @Tag system, 3-16 chars)
_TYPE_TO_TAG = {
    "character": "Character",
    "costume": "Costume",
    "environment": "Setting",
    "prop": "Prop",
}


def classify_shot_family(shot: dict) -> str:
    """
    Classify a shot into a family based on shot_size, shot_purpose, and sequence_type.

    Shot families determine which canonical assets are prioritized when
    composing the anchor image.
    """
    size = shot.get("shot_size", "MS")
    purpose = shot.get("shot_purpose", "show_action")
    seq_type = shot.get("sequence_type", "montage")

    # Direct mappings from shot_purpose
    if purpose == "show_detail":
        return "insert_detail"
    if purpose == "payoff_moment":
        return "emotional_moment"
    if purpose == "show_relationship":
        return "dialogue_shot"
    if purpose == "show_perspective":
        return "action_scene"

    # Size-based classification
    if size in ("EWS",):
        return "wide_establishing"
    if size in ("WS",) and seq_type == "establish":
        return "wide_establishing"
    if size in ("WS",) and seq_type in ("release", "montage"):
        return "environment_reestablish"
    if size in ("ECU", "CU") and purpose == "show_emotion":
        return "character_closeup"
    if size in ("CU", "MCU") and seq_type in ("reunion", "tension"):
        return "emotional_moment"
    if size == "INSERT":
        return "insert_detail"
    if size == "OTS":
        return "dialogue_shot"

    # Sequence-type fallbacks
    if seq_type == "pursuit":
        return "action_scene"
    if seq_type == "reveal" and size in ("CU", "MCU", "ECU"):
        return "emotional_moment"

    return "generic"


def select_canonical_refs(shot: dict, packages: list, family: str = None) -> list:
    """
    Select 1-3 canonical hero refs based on shot family priority weights.

    Always selects from approved canonical sheets. Different shot families
    prioritize different asset types.

    Args:
        shot: Shot dict with character/environment package bindings
        packages: All preproduction packages
        family: Shot family (auto-classified if None)

    Returns:
        list of {package_id, package_type, view, path, tag} — max 3
    """
    if not family:
        family = classify_shot_family(shot)

    priorities = SHOT_FAMILY_PRIORITY.get(family, SHOT_FAMILY_PRIORITY["generic"])
    view_prefs = FAMILY_VIEW_PREFERENCE.get(family, FAMILY_VIEW_PREFERENCE["generic"])

    # Build a package lookup by ID
    pkg_by_id = {p["package_id"]: p for p in packages}

    # Collect candidate refs with priority scores
    candidates = []

    # Character package
    char_pkg_id = shot.get("character_package_id")
    if char_pkg_id and char_pkg_id in pkg_by_id:
        pkg = pkg_by_id[char_pkg_id]
        ref_path = _get_best_view(pkg, view_prefs.get("character"))
        if ref_path:
            candidates.append({
                "package_id": char_pkg_id,
                "package_type": "character",
                "view": view_prefs.get("character", "hero_front"),
                "path": ref_path,
                "tag": _TYPE_TO_TAG["character"],
                "priority": priorities.get("character", 0),
            })

    # Costume package
    costume_pkg_id = shot.get("costume_package_id")
    if costume_pkg_id and costume_pkg_id in pkg_by_id:
        pkg = pkg_by_id[costume_pkg_id]
        ref_path = _get_best_view(pkg, view_prefs.get("costume"))
        if ref_path:
            candidates.append({
                "package_id": costume_pkg_id,
                "package_type": "costume",
                "view": view_prefs.get("costume", "front"),
                "path": ref_path,
                "tag": _TYPE_TO_TAG["costume"],
                "priority": priorities.get("costume", 0),
            })

    # Environment package
    env_pkg_id = shot.get("environment_package_id")
    if env_pkg_id and env_pkg_id in pkg_by_id:
        pkg = pkg_by_id[env_pkg_id]
        ref_path = _get_best_view(pkg, view_prefs.get("environment"))
        if ref_path:
            candidates.append({
                "package_id": env_pkg_id,
                "package_type": "environment",
                "view": view_prefs.get("environment", "wide_establish"),
                "path": ref_path,
                "tag": _TYPE_TO_TAG["environment"],
                "priority": priorities.get("environment", 0),
            })

    # Prop packages
    for prop_id in shot.get("prop_package_ids", []):
        if prop_id in pkg_by_id:
            pkg = pkg_by_id[prop_id]
            ref_path = _get_best_view(pkg, view_prefs.get("prop"))
            if ref_path:
                candidates.append({
                    "package_id": prop_id,
                    "package_type": "prop",
                    "view": view_prefs.get("prop", "hero_angle"),
                    "path": ref_path,
                    "tag": _TYPE_TO_TAG["prop"],
                    "priority": priorities.get("prop", 0),
                })

    # Sort by priority descending, take top 3
    candidates.sort(key=lambda c: -c["priority"])

    # Filter out zero-priority refs
    candidates = [c for c in candidates if c["priority"] > 0]

    return candidates[:3]


def compose_anchor(shot: dict, refs: list, style_bible: dict,
                   output_dir: str, taste_mods: dict = None) -> dict:
    """
    Compose a shot-specific anchor image from canonical asset refs.

    Calls fal.ai Gemini 3.1 Flash image edit with the selected refs and a
    composition prompt built from the shot's action and style.

    Args:
        shot: Shot dict
        refs: Output from select_canonical_refs() — [{path, tag, ...}]
        style_bible: Global style dict
        output_dir: Where to save the anchor image
        taste_mods: Optional taste profile modifiers

    Returns:
        Anchor dict: {anchor_id, shot_id, shot_family, source_refs,
                      image_path, prompt_used, status, derived_from_canon}
    """
    from lib.prompt_assembler import compile_anchor_prompt
    from lib.fal_client import gemini_edit_image, gemini_generate_image

    shot_id = shot.get("shot_id", shot.get("id", "unknown"))
    family = shot.get("shot_family") or classify_shot_family(shot)

    # Build composition prompt (optimized for still image, no camera movement)
    prompt = compile_anchor_prompt(
        shot=shot,
        style_bible=style_bible,
        refs=refs,
        taste_mods=taste_mods,
    )

    # Collect reference image paths for Gemini edit (refs carry identity/locations)
    ref_paths = [r["path"] for r in refs if r.get("path") and os.path.isfile(r["path"])]

    # Generate the anchor image
    anchors_dir = os.path.join(output_dir, "pipeline", "anchors")
    os.makedirs(anchors_dir, exist_ok=True)

    image_path = None
    try:
        if ref_paths:
            paths = gemini_edit_image(
                prompt=prompt,
                reference_image_paths=ref_paths,
                resolution="1K",
                num_images=1,
            )
        else:
            paths = gemini_generate_image(
                prompt=prompt,
                resolution="1K",
                aspect_ratio="16:9",
                num_images=1,
            )
        image_path = paths[0] if paths else None
        # Move to anchors directory with descriptive name
        if image_path and os.path.isfile(image_path):
            dest = os.path.join(anchors_dir, f"anchor_{shot_id}.png")
            import shutil
            shutil.move(image_path, dest)
            image_path = dest
    except Exception as e:
        print(f"[COMPOSITOR] Failed to compose anchor for {shot_id}: {e}")
        return {
            "anchor_id": f"anchor_{shot_id}",
            "shot_id": shot_id,
            "shot_family": family,
            "source_refs": [{"package_id": r["package_id"], "view": r["view"],
                             "tag": r["tag"]} for r in refs],
            "image_path": None,
            "prompt_used": prompt,
            "status": "failed",
            "error": str(e),
            "derived_from_canon": True,
        }

    return {
        "anchor_id": f"anchor_{shot_id}",
        "shot_id": shot_id,
        "shot_family": family,
        "source_refs": [{"package_id": r["package_id"], "view": r["view"],
                         "tag": r["tag"]} for r in refs],
        "image_path": image_path,
        "prompt_used": prompt,
        "status": "generated",
        "derived_from_canon": True,
        "rejection_notes": [],
    }


def compose_all_anchors(shots: list, packages: list, style_bible: dict,
                        output_dir: str, taste_mods: dict = None,
                        progress_cb=None) -> list:
    """
    Compose anchor images for all shots in a plan.

    Groups by shot_family for logging. Calls compose_anchor() per shot.

    Args:
        shots: Flat list of shot dicts (plan["scenes"])
        packages: All approved preproduction packages
        style_bible: Global style
        output_dir: Output directory
        taste_mods: Optional taste modifiers
        progress_cb: Optional callback(shot_index, total, anchor_dict)

    Returns:
        list of anchor dicts
    """
    # Only use approved packages with hero refs
    approved = [p for p in packages if p.get("status") == "approved"
                and p.get("hero_image_path")]
    if not approved:
        print("[COMPOSITOR] No approved packages with hero refs — skipping anchor composition")
        return []

    total = len(shots)
    anchors = []

    for i, shot in enumerate(shots):
        # Classify and store family on the shot
        family = classify_shot_family(shot)
        shot["shot_family"] = family

        # Select canonical refs
        refs = select_canonical_refs(shot, approved, family)

        if not refs:
            print(f"[COMPOSITOR] Shot {shot.get('shot_id', i)}: no refs available, skipping anchor")
            anchors.append({
                "anchor_id": f"anchor_{shot.get('shot_id', str(i))}",
                "shot_id": shot.get("shot_id", str(i)),
                "shot_family": family,
                "source_refs": [],
                "image_path": None,
                "status": "skipped",
                "derived_from_canon": True,
            })
            continue

        print(f"[COMPOSITOR] Shot {shot.get('shot_id', i)} ({family}): "
              f"composing from {len(refs)} refs "
              f"[{', '.join(r['tag'] for r in refs)}]")

        anchor = compose_anchor(shot, refs, style_bible, output_dir, taste_mods)
        anchors.append(anchor)

        # Set anchor path on the shot dict for downstream use
        if anchor.get("image_path"):
            shot["anchor_image_path"] = anchor["image_path"]
            shot["anchor_status"] = anchor["status"]
            shot["anchor_source_refs"] = anchor["source_refs"]

        if progress_cb:
            progress_cb(i, total, anchor)

    generated = sum(1 for a in anchors if a.get("status") == "generated")
    print(f"[COMPOSITOR] Composed {generated}/{total} anchors")

    return anchors


def regenerate_anchor(shot: dict, packages: list, style_bible: dict,
                      output_dir: str, taste_mods: dict = None) -> dict:
    """
    Recompose an anchor FROM CANONICAL SHEETS.

    This is the correct way to fix a bad anchor — go back to the approved
    canonical refs and recompose, never edit the existing anchor image.

    Args:
        shot: Shot dict
        packages: Approved preproduction packages (canonical source)
        style_bible: Global style
        output_dir: Output directory

    Returns:
        New anchor dict (replaces the old one)
    """
    family = shot.get("shot_family") or classify_shot_family(shot)
    refs = select_canonical_refs(shot, packages, family)

    print(f"[COMPOSITOR] Regenerating anchor for {shot.get('shot_id')} from canonical sheets")
    return compose_anchor(shot, refs, style_bible, output_dir, taste_mods)


# ── Internal helpers ──

def _get_best_view(package: dict, preferred_view: str = None) -> str:
    """
    Get the best image path from a package.

    Priority: preferred view → hero_image_path → first generated sheet image
    """
    # Try preferred view from sheet_images
    if preferred_view:
        for img in package.get("sheet_images", []):
            if img.get("view") == preferred_view and img.get("image_path"):
                if os.path.isfile(img["image_path"]):
                    return img["image_path"]

    # Fall back to hero image
    hero = package.get("hero_image_path")
    if hero and os.path.isfile(hero):
        return hero

    # Fall back to first available sheet image
    for img in package.get("sheet_images", []):
        if img.get("image_path") and os.path.isfile(img["image_path"]):
            return img["image_path"]

    return None


# ══════════════════════════════════════════════════════════════════════
# SCENE-LEVEL ANCHOR COMPOSITION
# One anchor per scene (beat/environment group), shared by all shots.
# ══════════════════════════════════════════════════════════════════════

def group_shots_into_scenes(scenes: list) -> dict:
    """Group shots by beat_id into scene groups.

    Returns: {beat_id: {
        "beat_id", "environment", "env_pkg", "char_pkgs", "costume_pkgs",
        "prop_pkgs", "shots", "hero_shot"
    }}
    """
    groups = {}
    for s in scenes:
        key = s.get("beat_id", "unknown")
        if key not in groups:
            groups[key] = {
                "beat_id": key,
                "environment": s.get("environmentName", ""),
                "env_pkg": s.get("environment_package_id"),
                "char_pkgs": set(),
                "costume_pkgs": set(),
                "prop_pkgs": set(),
                "shots": [],
            }
        g = groups[key]
        g["shots"].append(s)
        cpkg = s.get("character_package_id")
        if cpkg:
            g["char_pkgs"].add(cpkg)
        costpkg = s.get("costume_package_id")
        if costpkg:
            g["costume_pkgs"].add(costpkg)
        for ppkg in (s.get("prop_package_ids") or []):
            g["prop_pkgs"].add(ppkg)

    # Pick hero shot per scene (widest establishing shot)
    for g in groups.values():
        hero = None
        for s in g["shots"]:
            if s.get("shot_size") in ("EWS", "WS"):
                hero = s
                break
        if not hero:
            hero = g["shots"][0]
        g["hero_shot"] = hero
        # Convert sets to lists
        g["char_pkgs"] = list(g["char_pkgs"])
        g["costume_pkgs"] = list(g["costume_pkgs"])
        g["prop_pkgs"] = list(g["prop_pkgs"])

    return groups


def select_scene_refs(scene_group: dict, pkg_index: dict, max_tags: int = 3) -> list:
    """Select @Tag references for a scene anchor.

    Priority: Character > Environment > Prop > Costume
    Returns: [{"path", "tag", "pkg_id", "pkg_type"}] max 3
    """
    refs = []

    # 1. Primary character
    for cpkg_id in scene_group.get("char_pkgs", []):
        pkg = pkg_index.get(cpkg_id, {})
        hero = pkg.get("hero_image_path", "")
        if hero and os.path.isfile(hero):
            refs.append({"path": hero, "tag": "Character", "pkg_id": cpkg_id,
                         "pkg_type": "character"})
            break

    # 2. Environment
    env_pkg_id = scene_group.get("env_pkg")
    if env_pkg_id:
        pkg = pkg_index.get(env_pkg_id, {})
        hero = pkg.get("hero_image_path", "")
        if hero and os.path.isfile(hero):
            refs.append({"path": hero, "tag": "Setting", "pkg_id": env_pkg_id,
                         "pkg_type": "environment"})

    # 3. Props (if slot available)
    if len(refs) < max_tags:
        for ppkg_id in scene_group.get("prop_pkgs", []):
            pkg = pkg_index.get(ppkg_id, {})
            hero = pkg.get("hero_image_path", "")
            if hero and os.path.isfile(hero):
                refs.append({"path": hero, "tag": "PropRef", "pkg_id": ppkg_id,
                             "pkg_type": "prop"})
                break

    # 4. Costume (if slot available)
    if len(refs) < max_tags:
        for costpkg_id in scene_group.get("costume_pkgs", []):
            pkg = pkg_index.get(costpkg_id, {})
            hero = pkg.get("hero_image_path", "")
            if hero and os.path.isfile(hero):
                refs.append({"path": hero, "tag": "Costume", "pkg_id": costpkg_id,
                             "pkg_type": "costume"})
                break

    return refs[:max_tags]


def build_scene_anchor_prompt(scene_group: dict, refs: list,
                              style_bible: dict = None) -> str:
    """Build prompt for scene anchor composition.

    Uses the hero shot (widest establishing shot) as basis.
    Injects @Tag mentions for resolved references.
    """
    parts = []

    # Style
    if style_bible:
        gs = style_bible.get("global_style", "")
        if gs:
            parts.append(gs + ".")

    # @Tag mentions
    tag_names = [r["tag"] for r in refs]
    if "Character" in tag_names:
        parts.append("@Character in a cinematic scene.")
    if "Setting" in tag_names:
        parts.append("Set in the location from @Setting.")

    # Hero shot action
    hero = scene_group.get("hero_shot")
    if hero:
        action = hero.get("action", hero.get("prompt", ""))
        if action:
            parts.append(action)

    # Prop/Costume mentions
    if "PropRef" in tag_names:
        parts.append("Featuring the item from @PropRef.")
    if "Costume" in tag_names:
        parts.append("Wearing the outfit from @Costume.")

    prompt = " ".join(parts)
    return prompt[:1000]  # Runway limit


def compose_scene_anchors(plan: dict, pkg_index: dict, output_dir: str,
                          model: str = "gemini",
                          progress_cb=None) -> list:
    """Compose ONE anchor image per scene.

    Each scene (beat/environment group) gets a single composed anchor.
    All shots within that scene will use this anchor as their first frame.

    Args:
        plan: auto_director_plan with scenes
        pkg_index: {package_id: package_dict}
        output_dir: where to save anchors
        model: ignored — anchors always go through fal.ai Gemini (parameter
               kept for backwards-compatible callers)
        progress_cb: optional callback(scene_key, status, anchor_dict)

    Returns: list of scene anchor dicts
    """
    from lib.fal_client import gemini_edit_image, gemini_generate_image
    import shutil

    scenes = plan.get("scenes", [])
    style_bible = plan.get("style_bible", {})
    groups = group_shots_into_scenes(scenes)

    anchor_dir = os.path.join(output_dir, "pipeline", "anchors")
    os.makedirs(anchor_dir, exist_ok=True)

    anchors = []

    for scene_key, group in groups.items():
        # Resolve refs
        refs = select_scene_refs(group, pkg_index)
        ref_tags = [r["tag"] for r in refs]

        # Build prompt
        prompt = build_scene_anchor_prompt(group, refs, style_bible)

        print(f"[SCENE_ANCHOR] {scene_key} ({group['environment']}): "
              f"{len(refs)} refs ({ref_tags})")
        print(f"[SCENE_ANCHOR] Prompt: {prompt[:120]}...")

        if progress_cb:
            progress_cb(scene_key, "composing", None)

        # Compose via fal.ai Gemini — edit with refs when we have them,
        # text-to-image otherwise.
        ref_paths = [r["path"] for r in refs if r.get("path") and os.path.isfile(r["path"])]
        try:
            if ref_paths:
                paths = gemini_edit_image(
                    prompt=prompt,
                    reference_image_paths=ref_paths,
                    resolution="1K",
                    num_images=1,
                )
            else:
                paths = gemini_generate_image(
                    prompt=prompt,
                    resolution="1K",
                    aspect_ratio="16:9",
                    num_images=1,
                )
            img_path = paths[0] if paths else None
        except Exception as _e:
            print(f"[SCENE_ANCHOR] {scene_key}: gemini error {_e}")
            img_path = None

        anchor = {
            "scene_key": scene_key,
            "environment": group["environment"],
            "characters": list(set(
                s.get("characterName") for s in group["shots"]
                if s.get("characterName")
            )),
            "num_shots": len(group["shots"]),
            "image_path": None,
            "source_refs": [{"pkg_id": r["pkg_id"], "tag": r["tag"],
                             "pkg_type": r["pkg_type"]} for r in refs],
            "prompt_used": prompt,
            "status": "failed",
        }

        if img_path and os.path.isfile(img_path):
            dest = os.path.join(anchor_dir, f"scene_{scene_key}.png")
            shutil.copy2(img_path, dest)
            anchor["image_path"] = dest
            anchor["status"] = "generated"
            print(f"[SCENE_ANCHOR] {scene_key}: saved to {dest}")
        else:
            print(f"[SCENE_ANCHOR] {scene_key}: FAILED")

        if progress_cb:
            progress_cb(scene_key, anchor["status"], anchor)

        anchors.append(anchor)

    generated = sum(1 for a in anchors if a["status"] == "generated")
    print(f"[SCENE_ANCHOR] Composed {generated}/{len(groups)} scene anchors")
    return anchors


def bind_scene_anchors_to_shots(plan: dict, anchors: list) -> int:
    """Bind scene anchor paths to all shots within each scene.

    Every shot in a scene gets the same anchor_image_path.
    The video generator will use this as the first frame.

    Returns: number of shots bound.
    """
    anchor_by_scene = {}
    for a in anchors:
        if a.get("image_path") and os.path.isfile(a["image_path"]):
            anchor_by_scene[a["scene_key"]] = a["image_path"]

    bound = 0
    for scene in plan.get("scenes", []):
        beat_id = scene.get("beat_id", "")
        anchor_path = anchor_by_scene.get(beat_id)
        if anchor_path:
            scene["anchor_image_path"] = anchor_path
            scene["first_frame_path"] = ""  # clear stale chains
            bound += 1

    return bound


def save_scene_anchor_manifest(anchors: list, output_dir: str) -> str:
    """Save scene anchor metadata for review."""
    import json
    path = os.path.join(output_dir, "pipeline", "scene_anchor_manifest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(anchors, f, indent=2)
    return path
