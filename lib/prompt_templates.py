"""
Centralized Prompt Template System for LUMN Studio.
All generation prompt patterns live here — no magic strings scattered in code.
"""


# ─────────────────────── Project Style Lock ───────────────────────

def build_style_prefix(project_style: dict) -> str:
    """Build a style prefix from the project style lock.
    Fields: worldSetting, tone, visualLanguage, colorPalette, textureMaterial, cameraLanguage, negativePrompt"""
    if not project_style:
        return ""
    parts = []
    if project_style.get("worldSetting"):
        parts.append(f"World: {project_style['worldSetting']}.")
    if project_style.get("tone"):
        parts.append(f"Tone: {project_style['tone']}.")
    if project_style.get("visualLanguage"):
        parts.append(project_style["visualLanguage"])
    if project_style.get("colorPalette"):
        parts.append(f"Color palette: {project_style['colorPalette']}.")
    if project_style.get("textureMaterial"):
        parts.append(f"Materials: {project_style['textureMaterial']}.")
    if project_style.get("cameraLanguage"):
        parts.append(f"Camera: {project_style['cameraLanguage']}.")
    return " ".join(parts)


def build_negative_prompt(project_style: dict) -> str:
    """Extract negative prompt from project style."""
    if not project_style:
        return ""
    return project_style.get("negativePrompt", "")


# ─────────────────────── Character Sheet ───────────────────────

def build_character_sheet_prompt(character: dict, costume: dict = None,
                                  project_style: dict = None,
                                  include_face_closeup: bool = True) -> str:
    """Build a prompt for generating a character reference sheet.

    Produces a multi-view character sheet with:
    - Front view, 3/4 view, side view, back view
    - Consistent proportions, outfit, accessories
    - Optional dedicated face close-up inset
    """
    parts = []

    # Style prefix
    style = build_style_prefix(project_style)
    if style:
        parts.append(style)

    # Core sheet instruction
    parts.append("Professional character reference sheet, white background, clean presentation.")
    parts.append("Multiple consistent views of the SAME character: front view, three-quarter view, side view, back view.")
    parts.append("Same proportions, same outfit, same accessories across all views.")

    # Character details — use @Character tag so Runway binds the reference photo
    name = character.get("name", "character")
    desc = character.get("physicalDescription") or character.get("description", "")
    if desc:
        parts.append(f"@Character {name} — {desc}")
    else:
        parts.append(f"@Character {name}")

    if character.get("hair"):
        parts.append(f"Hair: {character['hair']}.")
    if character.get("skinTone"):
        parts.append(f"Skin: {character['skinTone']}.")
    if character.get("bodyType"):
        parts.append(f"Body type: {character['bodyType']}.")
    if character.get("distinguishingFeatures"):
        parts.append(f"Distinguishing features: {character['distinguishingFeatures']}.")
    if character.get("ageRange"):
        parts.append(f"Age: {character['ageRange']}.")

    # Costume if linked
    if costume:
        costume_desc = costume.get("description", "")
        if costume_desc:
            parts.append(f"Outfit: {costume_desc}")
        else:
            outfit_parts = []
            for field in ("upperBody", "lowerBody", "footwear", "accessories"):
                if costume.get(field):
                    outfit_parts.append(costume[field])
            if outfit_parts:
                parts.append(f"Outfit: {', '.join(outfit_parts)}")
    elif character.get("outfitDescription"):
        parts.append(f"Outfit: {character['outfitDescription']}")

    # Face close-up
    if include_face_closeup:
        parts.append("Include one dedicated FACE CLOSE-UP panel: extreme detail, visible skin texture, catch-light in eyes, highest facial definition for identity locking.")

    # Quality
    parts.append("Hyper-realistic, photorealistic, 8K ultra-high definition, studio lighting, sharp focus.")

    return " ".join(parts)[:1500]


def build_face_closeup_prompt(character: dict, project_style: dict = None) -> str:
    """Build a prompt specifically for generating a face close-up portrait.
    Used to create the dedicated identity-lock portrait for a character."""
    parts = []

    style = build_style_prefix(project_style)
    if style:
        parts.append(style)

    name = character.get("name", "character")
    desc = character.get("physicalDescription") or character.get("description", "")

    parts.append(f"Extreme close-up portrait of @Character {name}.")
    if desc:
        parts.append(desc)
    if character.get("hair"):
        parts.append(f"Hair: {character['hair']}.")
    if character.get("skinTone"):
        parts.append(f"Skin: {character['skinTone']}.")
    if character.get("distinguishingFeatures"):
        parts.append(f"Features: {character['distinguishingFeatures']}.")

    parts.append("Head and shoulders framing, shallow depth of field, clean studio lighting.")
    parts.append("Visible pores, catch-light in eyes, highest possible facial definition and realism.")
    parts.append("This image will be used as an identity reference — preserve EXACT facial structure.")
    parts.append("8K ultra-high definition, photorealistic, sharp focus, professional portrait photography.")

    return " ".join(parts)[:1000]


# ─────────────────────── Costume Sheet ───────────────────────

def build_costume_sheet_prompt(costume: dict, character: dict = None,
                                project_style: dict = None) -> str:
    """Build a prompt for generating a costume/accessory reference sheet."""
    parts = []

    style = build_style_prefix(project_style)
    if style:
        parts.append(style)

    parts.append("Professional costume/wardrobe reference sheet, clean white background.")
    parts.append("Multiple views: front, back, detail panels for materials and accessories.")

    name = costume.get("name", "costume")
    desc = costume.get("description", "")
    if desc:
        parts.append(f"@Costume {name} — {desc}")
    else:
        parts.append(f"@Costume {name}")
        for field in ("upperBody", "lowerBody", "footwear", "accessories"):
            if costume.get(field):
                parts.append(f"{field}: {costume[field]}")

    if costume.get("colorPalette"):
        parts.append(f"Colors: {costume['colorPalette']}.")
    if costume.get("materialNotes"):
        parts.append(f"Materials: {costume['materialNotes']}.")

    if character:
        parts.append(f"Shown on @Character {character.get('name', 'character')}.")

    parts.append("Include material/texture detail insets. Consistent across all views.")
    parts.append("8K ultra-high definition, photorealistic, fashion photography quality, sharp focus.")

    return " ".join(parts)[:1000]


# ─────────────────────── Environment Sheet ───────────────────────

def build_environment_sheet_prompt(environment: dict, project_style: dict = None) -> str:
    """Build a prompt for generating an environment reference sheet."""
    parts = []

    style = build_style_prefix(project_style)
    if style:
        parts.append(style)

    parts.append("Professional environment/location reference sheet.")
    parts.append("Multiple cinematic angles of the SAME location: wide establishing, medium interior/exterior, detail texture panels.")
    parts.append("No characters unless explicitly specified. Empty environment, architectural focus.")

    name = environment.get("name", "environment")
    desc = environment.get("description", "")
    if desc:
        parts.append(f"@Setting {name} — {desc}")
    else:
        parts.append(f"@Setting {name}")

    if environment.get("architecture") or environment.get("architectureNotes"):
        arch = environment.get("architecture") or environment.get("architectureNotes", "")
        parts.append(f"Architecture: {arch}.")
    if environment.get("lighting"):
        parts.append(f"Lighting: {environment['lighting']}.")
    if environment.get("atmosphere"):
        parts.append(f"Atmosphere: {environment['atmosphere']}.")
    if environment.get("weather"):
        parts.append(f"Weather: {environment['weather']}.")
    if environment.get("timeOfDay"):
        parts.append(f"Time: {environment['timeOfDay']}.")
    if environment.get("materialNotes"):
        parts.append(f"Materials: {environment['materialNotes']}.")

    parts.append("Consistent architecture, materials, lighting, and atmosphere across all views.")
    parts.append("8K ultra-high definition, cinematic photography, architectural visualization quality.")

    return " ".join(parts)[:1000]


# ─────────────────────── Prop Sheet ───────────────────────

def build_prop_sheet_prompt(prop: dict, project_style: dict = None) -> str:
    """Build a prompt for generating a prop/accessory reference sheet."""
    parts = []

    style = build_style_prefix(project_style)
    if style:
        parts.append(style)

    parts.append("Professional prop/object reference sheet, clean white background.")
    parts.append("Multiple angles: front, side, top, detail insets for texture and material.")

    name = prop.get("name", "prop")
    desc = prop.get("description", "")
    if desc:
        parts.append(f"@Prop {name} — {desc}")
    else:
        parts.append(f"@Prop {name}")

    if prop.get("category"):
        parts.append(f"Type: {prop['category']}.")

    parts.append("Consistent scale, material, and detail across all views.")
    parts.append("8K ultra-high definition, product photography quality, sharp focus.")

    return " ".join(parts)[:1000]


# ─────────────────────── Shot Prompt (enhanced) ───────────────────────

def build_enhanced_shot_prompt(shot_type: str, scene_prompt: str,
                                character: dict = None, costume: dict = None,
                                environment: dict = None, props: list = None,
                                project_style: dict = None,
                                has_char_ref: bool = False,
                                has_costume_ref: bool = False,
                                has_env_ref: bool = False,
                                has_face_closeup: bool = False) -> str:
    """Build a generation prompt optimized for shot type with full project context.

    This is the enhanced version that uses project style lock + canonical asset data.
    """
    import re as _re

    parts = []

    # 1. Project style
    style = build_style_prefix(project_style)
    if style:
        parts.append(style)

    # 2. Shot type framing
    shot_type = (shot_type or "medium").lower().strip()

    if shot_type == "close-up":
        parts.append("Extreme close-up shot. Detailed skin texture, visible pores, catch-light in eyes, shallow depth of field.")
        if has_char_ref:
            parts.append("PRESERVE EXACT LIKENESS from reference — same face, same features, same proportions.")
        if has_face_closeup:
            parts.append("Use the approved face close-up as primary identity reference.")
    elif shot_type == "medium":
        parts.append("Medium shot, waist-up framing, balanced composition.")
        if has_char_ref:
            parts.append("PRESERVE EXACT LIKENESS from reference photos.")
    elif shot_type == "full":
        parts.append("Full body shot, head-to-toe framing, character centered in frame.")
        if has_costume_ref:
            parts.append("Show the complete outfit clearly.")
        if has_char_ref:
            parts.append("PRESERVE EXACT LIKENESS from reference.")
    elif shot_type == "wide":
        parts.append("Wide shot, expansive environment, character small in frame, cinematic composition, atmosphere.")
        if has_env_ref:
            parts.append("Match the exact environment from @Setting reference.")
    elif shot_type == "establishing":
        parts.append("Establishing shot, sweeping vista, grand scale, environmental storytelling, dramatic atmosphere.")
        if has_env_ref:
            parts.append("Match the exact location from @Setting reference.")
    elif shot_type == "insert":
        parts.append("Insert/detail shot, extreme close-up on object or detail, shallow depth of field.")
    else:
        parts.append("Cinematic shot.")

    # 3. Scene prompt (cleaned)
    cleaned = scene_prompt
    cleaned = _re.sub(r'(?i)\b(hyper[- ]?realistic|photorealistic|8k|4k)\b', '', cleaned)
    cleaned = _re.sub(r'\s{2,}', ' ', cleaned).strip()
    if cleaned:
        parts.append(cleaned)

    # 4. Quality
    parts.append("Hyper-realistic, photorealistic, 8K, cinematic lighting, sharp focus.")

    # 5. Negative from style
    neg = build_negative_prompt(project_style)
    if neg:
        parts.append(f"AVOID: {neg}")

    return " ".join(parts)[:1500]


# ─────────────────────── Video Prompt ───────────────────────

def build_video_prompt(scene_prompt: str, shot_type: str = "medium",
                       project_style: dict = None,
                       camera_movement: str = "") -> str:
    """Build a prompt for video generation (image_to_video or text_to_video)."""
    parts = []

    style = build_style_prefix(project_style)
    if style:
        parts.append(style)

    shot_type = (shot_type or "medium").lower().strip()

    # Shot-type-aware motion hints
    if shot_type == "close-up":
        parts.append("Subtle facial movement, breathing, eye movement, slight expression change.")
    elif shot_type == "medium":
        parts.append("Natural body movement, gestures, balanced motion.")
    elif shot_type == "full":
        parts.append("Full body motion, walking, turning, character animation.")
    elif shot_type in ("wide", "establishing"):
        parts.append("Sweeping camera movement, atmospheric motion, environmental animation.")
    elif shot_type == "insert":
        parts.append("Subtle object movement, slow reveal, detail focus.")

    if camera_movement:
        parts.append(f"Camera: {camera_movement}.")

    # Clean and add scene prompt
    import re as _re
    cleaned = _re.sub(r'(?i)\b(hyper[- ]?realistic|photorealistic|8k|4k)\b', '', scene_prompt)
    cleaned = _re.sub(r'\s{2,}', ' ', cleaned).strip()
    if cleaned:
        parts.append(cleaned)

    parts.append("Cinematic quality, smooth motion, natural physics.")

    return " ".join(parts)[:1000]


# ─────────────────────── Reference Package Builder ───────────────────────

def build_reference_package(shot_type: str, character: dict = None,
                            costume: dict = None, environment: dict = None,
                            props: list = None) -> dict:
    """Build a deterministic reference package from approved assets for a shot.

    Returns:
    {
        "characterRefs": [{"url": str, "tag": str, "slot": str, "priority": float}],
        "costumeRefs": [...],
        "environmentRefs": [...],
        "propRefs": [...],
        "quality": "production"|"approved"|"loose"|"text-only",
        "warnings": [str]
    }
    """
    shot_type = (shot_type or "medium").lower().strip()

    quality_levels = ["text-only", "loose", "approved", "production"]

    package = {
        "characterRefs": [],
        "costumeRefs": [],
        "environmentRefs": [],
        "propRefs": [],
        "quality": "text-only",
        "warnings": [],
    }

    # ---- Character refs ----
    if character:
        # Priority order by shot type
        if shot_type == "close-up":
            # Face close-up is highest priority for close-ups
            if character.get("approvedFaceCloseUp"):
                package["characterRefs"].append({
                    "url": character["approvedFaceCloseUp"],
                    "tag": "@Character",
                    "slot": "faceCloseUp",
                    "priority": 1.0,
                })
                package["quality"] = "production"
            elif character.get("approvedHeroPortrait"):
                package["characterRefs"].append({
                    "url": character["approvedHeroPortrait"],
                    "tag": "@Character",
                    "slot": "heroPortrait",
                    "priority": 0.9,
                })
                package["quality"] = "approved"
            else:
                package["warnings"].append("No approved face close-up. Close-up shot fidelity may be weaker.")
        else:
            # For non-close-ups, prefer full body or hero portrait
            if character.get("approvedFullBody"):
                package["characterRefs"].append({
                    "url": character["approvedFullBody"],
                    "tag": "@Character",
                    "slot": "fullBody",
                    "priority": 0.9,
                })
                package["quality"] = "production"
            elif character.get("approvedHeroPortrait"):
                package["characterRefs"].append({
                    "url": character["approvedHeroPortrait"],
                    "tag": "@Character",
                    "slot": "heroPortrait",
                    "priority": 0.8,
                })
                package["quality"] = "approved"

        # Fallback to approved sheet or raw reference
        if not package["characterRefs"]:
            if character.get("approvedSheet"):
                package["characterRefs"].append({
                    "url": character["approvedSheet"],
                    "tag": "@Character",
                    "slot": "sheet",
                    "priority": 0.7,
                })
                package["quality"] = max(
                    package["quality"], "approved",
                    key=lambda x: quality_levels.index(x)
                )
            elif character.get("referencePhoto"):
                package["characterRefs"].append({
                    "url": character["referencePhoto"],
                    "tag": "@Character",
                    "slot": "upload",
                    "priority": 0.5,
                })
                package["quality"] = max(
                    package["quality"], "loose",
                    key=lambda x: quality_levels.index(x)
                )

    # ---- Costume refs ----
    if costume:
        if costume.get("approvedSheet"):
            package["costumeRefs"].append({
                "url": costume["approvedSheet"],
                "tag": "@Costume",
                "slot": "sheet",
                "priority": 0.8,
            })
        elif costume.get("referenceImagePath"):
            package["costumeRefs"].append({
                "url": costume["referenceImagePath"],
                "tag": "@Costume",
                "slot": "upload",
                "priority": 0.5,
            })

    # ---- Environment refs ----
    if environment:
        if environment.get("approvedSheet"):
            package["environmentRefs"].append({
                "url": environment["approvedSheet"],
                "tag": "@Setting",
                "slot": "sheet",
                "priority": 0.8,
            })
        elif environment.get("referenceImagePath"):
            package["environmentRefs"].append({
                "url": environment["referenceImagePath"],
                "tag": "@Setting",
                "slot": "upload",
                "priority": 0.5,
            })

    # ---- Prop refs ----
    if props:
        for prop in props[:2]:  # Max 2 prop refs
            if prop.get("approvedSheet"):
                package["propRefs"].append({
                    "url": prop["approvedSheet"],
                    "tag": "@Prop",
                    "slot": "sheet",
                    "priority": 0.6,
                })
            elif prop.get("referenceImagePath"):
                package["propRefs"].append({
                    "url": prop["referenceImagePath"],
                    "tag": "@Prop",
                    "slot": "upload",
                    "priority": 0.3,
                })

    # Determine overall quality level
    all_ref_lists = [package["characterRefs"], package["costumeRefs"], package["environmentRefs"]]
    has_any_approved = any(
        r.get("slot") in ("sheet", "faceCloseUp", "heroPortrait", "fullBody")
        for refs in all_ref_lists
        for r in refs
    )
    has_any_upload = any(
        r.get("slot") == "upload"
        for refs in all_ref_lists
        for r in refs
    )

    if has_any_approved:
        package["quality"] = "production" if all(
            r.get("slot") != "upload"
            for refs in all_ref_lists
            for r in refs
        ) else "approved"
    elif has_any_upload:
        package["quality"] = "loose"
    else:
        package["quality"] = "text-only"

    return package


def select_best_refs_for_shot(package: dict, shot_type: str, max_refs: int = 3) -> list:
    """From a reference package, select the best refs for a shot, respecting the 3-ref API limit.

    Returns list of {"url": str, "tag": str, "priority": float} sorted by priority.
    """
    shot_type = (shot_type or "medium").lower().strip()

    # Weight multipliers by shot type
    type_weights = {
        "close-up": {"character": 1.0, "costume": 0.4, "environment": 0.1, "prop": 0.1},
        "medium": {"character": 0.9, "costume": 0.8, "environment": 0.5, "prop": 0.3},
        "full": {"character": 0.8, "costume": 0.9, "environment": 0.6, "prop": 0.3},
        "wide": {"character": 0.4, "costume": 0.3, "environment": 1.0, "prop": 0.2},
        "establishing": {"character": 0.2, "costume": 0.1, "environment": 1.0, "prop": 0.1},
        "insert": {"character": 0.1, "costume": 0.3, "environment": 0.1, "prop": 1.0},
    }
    weights = type_weights.get(shot_type, type_weights["medium"])

    all_refs = []
    for ref in package.get("characterRefs", []):
        all_refs.append({**ref, "weight": ref["priority"] * weights["character"]})
    for ref in package.get("costumeRefs", []):
        all_refs.append({**ref, "weight": ref["priority"] * weights["costume"]})
    for ref in package.get("environmentRefs", []):
        all_refs.append({**ref, "weight": ref["priority"] * weights["environment"]})
    for ref in package.get("propRefs", []):
        all_refs.append({**ref, "weight": ref["priority"] * weights["prop"]})

    # Sort by weight descending
    all_refs.sort(key=lambda r: -r["weight"])

    # Take top refs
    selected = all_refs[:max_refs]

    return [{"url": r["url"], "tag": r["tag"], "priority": r["weight"]} for r in selected]
