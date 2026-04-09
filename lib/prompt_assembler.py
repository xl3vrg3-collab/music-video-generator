"""
Prompt Assembler — Deterministic prompt compilation from structured data.

Assembly order:
1. GLOBAL STYLE
2. WORLD LOCK
3. CHARACTER BLOCK
4. COSTUME BLOCK
5. ENVIRONMENT BLOCK
6. ACTION BLOCK
7. CAMERA BLOCK
8. CONTINUITY LOCKS
9. NEGATIVE PROMPT

All generation is built from structured inputs, not freeform text.
"""


# ---- Default Global Style ----

DEFAULT_GLOBAL_STYLE = (
    "Cinematic film still, 2.39:1 anamorphic widescreen"
)

DEFAULT_NEGATIVE = (
    "no text, no watermark, no UI elements, no subtitles, "
    "no blurry, no distorted faces, no extra limbs, no deformed hands"
)


# ---- Block Builders ----

def build_character_block(character: dict) -> str:
    """Build the character description block from structured data."""
    if not character:
        return ""

    parts = []

    # Core identity
    name = character.get("name", "")
    role = character.get("role", "")
    if name:
        parts.append(f"{name}")
    if role:
        parts.append(f"({role})")

    # Visual identity
    vi = character.get("visual_identity", {})
    if isinstance(vi, dict) and vi:
        if vi.get("body_type"):
            parts.append(vi["body_type"])
        if vi.get("face_traits"):
            traits = vi["face_traits"] if isinstance(vi["face_traits"], list) else [vi["face_traits"]]
            parts.append(", ".join(traits))
        if vi.get("posture"):
            parts.append(vi["posture"])
        if vi.get("silhouette_keywords"):
            kw = vi["silhouette_keywords"] if isinstance(vi["silhouette_keywords"], list) else [vi["silhouette_keywords"]]
            parts.append(", ".join(kw))

    # Fall back to legacy flat fields if visual_identity not populated
    if not vi or not any(vi.values()):
        phys = character.get("physicalDescription", character.get("description", ""))
        if phys:
            parts.append(phys)
        hair = character.get("hair", "")
        if hair:
            parts.append(hair)
        skin = character.get("skinTone", "")
        if skin:
            parts.append(f"{skin} skin")
        body = character.get("bodyType", "")
        if body:
            parts.append(body)
        features = character.get("distinguishingFeatures", "")
        if features:
            parts.append(features)
        expression = character.get("defaultExpression", "")
        if expression:
            parts.append(expression)

    return ", ".join(p for p in parts if p)


def build_costume_block(costume: dict) -> str:
    """Build the costume description block from structured data."""
    if not costume:
        return ""

    parts = []

    # New structured garments
    garments = costume.get("garments", {})
    if isinstance(garments, dict) and garments:
        for key in ("outerwear", "top", "bottom", "footwear", "gloves", "headwear"):
            val = garments.get(key, "")
            if val:
                parts.append(val)

    # Materials
    materials = costume.get("materials", {})
    if isinstance(materials, dict) and materials:
        if materials.get("fabric"):
            parts.append(materials["fabric"])
        if materials.get("finish"):
            parts.append(materials["finish"])
        if materials.get("wear_level"):
            parts.append(f"{materials['wear_level']} wear")

    # Accessories
    accessories = costume.get("accessories", [])
    if isinstance(accessories, list):
        for acc in accessories:
            if isinstance(acc, dict):
                acc_name = acc.get("name", "")
                if acc_name:
                    parts.append(acc_name)
            elif isinstance(acc, str) and acc:
                parts.append(acc)

    # Fall back to legacy flat fields
    if not parts:
        desc = costume.get("description", "")
        if desc:
            parts.append(desc)
        else:
            for field in ("upperBody", "lowerBody", "footwear"):
                val = costume.get(field, "")
                if val:
                    parts.append(val)
        if costume.get("colorPalette"):
            parts.append(costume["colorPalette"])

    if parts:
        return "wearing " + ", ".join(p for p in parts if p)
    return ""


def build_environment_block(environment: dict) -> str:
    """Build the environment description block from structured data."""
    if not environment:
        return ""

    parts = []

    # Core description
    desc = environment.get("description", "")
    if desc:
        parts.append(desc)

    # Architecture (new structured)
    arch = environment.get("architecture", {})
    if isinstance(arch, dict) and arch:
        if arch.get("layout"):
            parts.append(arch["layout"])
        if arch.get("key_features"):
            features = arch["key_features"] if isinstance(arch["key_features"], list) else [arch["key_features"]]
            parts.append(", ".join(features))
        if arch.get("walls"):
            parts.append(arch["walls"])
        if arch.get("floor"):
            parts.append(arch["floor"])

    # Lighting (new structured)
    light = environment.get("lighting_struct", {})
    if isinstance(light, dict) and light:
        if light.get("primary_source"):
            parts.append(light["primary_source"])
        if light.get("shadow_behavior"):
            parts.append(light["shadow_behavior"])
        if light.get("atmosphere_density"):
            parts.append(light["atmosphere_density"])
    else:
        # Legacy flat field
        lighting = environment.get("lighting", "")
        if lighting:
            parts.append(lighting)

    # Atmosphere
    atmos = environment.get("atmosphere", "")
    if isinstance(atmos, str) and atmos:
        parts.append(atmos)
    elif isinstance(atmos, dict):
        if atmos.get("mood"):
            parts.append(atmos["mood"])

    # Props
    props = environment.get("props", {})
    if isinstance(props, dict):
        fixed = props.get("fixed", [])
        if fixed:
            parts.append("featuring " + ", ".join(fixed))

    # Legacy flat fields fallback
    if not parts:
        for field in ("location", "timeOfDay", "weather"):
            val = environment.get(field, "")
            if val:
                parts.append(val)

    if parts:
        return "in " + ", ".join(p for p in parts if p)
    return ""


def build_action_block(scene: dict) -> str:
    """Build the action/movement block from scene data."""
    action = scene.get("action", {})
    if isinstance(action, dict) and action:
        parts = []
        if action.get("summary"):
            parts.append(action["summary"])
        if action.get("start_pose"):
            parts.append(f"starting: {action['start_pose']}")
        if action.get("end_pose"):
            parts.append(f"ending: {action['end_pose']}")
        movement = action.get("movement_rules", [])
        if movement:
            if isinstance(movement, list):
                parts.append(", ".join(movement))
            else:
                parts.append(str(movement))
        return ", ".join(parts)

    # Fall back to story beat or prompt
    story_beat = scene.get("ai_story_beat", scene.get("story_beat", ""))
    if story_beat:
        return story_beat
    return ""


def build_camera_block(scene: dict) -> str:
    """Build the camera instruction block."""
    camera = scene.get("camera", {})
    if isinstance(camera, dict) and camera:
        parts = []
        if camera.get("shot_type"):
            parts.append(camera["shot_type"])
        if camera.get("lens"):
            parts.append(f"{camera['lens']} lens")
        if camera.get("motion"):
            parts.append(camera["motion"])
        if camera.get("composition"):
            parts.append(camera["composition"])
        return ", ".join(parts)

    # Fall back to legacy camera_movement
    cam = scene.get("camera_movement", "")
    if cam:
        from lib.video_generator import CAMERA_PROMPT_SUFFIXES
        suffix = CAMERA_PROMPT_SUFFIXES.get(cam, "")
        return suffix.strip(", ") if suffix else cam.replace("_", " ")
    return ""


def build_continuity_block(character: dict = None, costume: dict = None,
                            environment: dict = None) -> str:
    """Build continuity enforcement instructions."""
    locks = []

    if character:
        cont = character.get("continuity", {})
        if isinstance(cont, dict):
            must_keep = cont.get("must_keep", [])
            if must_keep:
                locks.append(f"Character must maintain: {', '.join(must_keep)}")
            never_allow = cont.get("never_allow", [])
            if never_allow:
                locks.append(f"Character never: {', '.join(never_allow)}")

    if costume:
        cont = costume.get("continuity", {})
        if isinstance(cont, dict):
            if cont.get("strict_consistency"):
                locks.append("Costume must be exactly as described, no variation")

    if environment:
        # Environment should always be the same location
        locks.append("Same environment, consistent architecture and lighting")

    return ". ".join(locks) if locks else ""


def build_negative_prompt(scene: dict = None, global_negative: str = "",
                           style_lock: dict = None) -> str:
    """Build the negative prompt from structured rules."""
    parts = []

    # Global negative
    if global_negative:
        parts.append(global_negative)
    else:
        parts.append(DEFAULT_NEGATIVE)

    # Style lock forbidden items
    if style_lock and isinstance(style_lock, dict):
        forbidden = style_lock.get("forbidden", style_lock.get("palette_forbidden", []))
        if forbidden:
            parts.append(", ".join(f"no {f}" for f in forbidden))

    # Scene-level negative
    if scene and scene.get("negative_prompt"):
        parts.append(scene["negative_prompt"])

    return ", ".join(parts)


# ---- Main Assembly Function ----

def assemble_prompt(
    global_style: str = "",
    world_setting: str = "",
    character: dict = None,
    costume: dict = None,
    environment: dict = None,
    scene: dict = None,
    global_negative: str = "",
    universal_prompt: str = "",
) -> dict:
    """
    Assemble a complete generation prompt from structured data.

    Returns dict with:
        prompt: the compiled positive prompt
        negative_prompt: the compiled negative prompt
        blocks: dict of individual blocks for debugging/preview
    """
    scene = scene or {}

    # 1. GLOBAL STYLE
    style = global_style or universal_prompt or DEFAULT_GLOBAL_STYLE

    # 2. WORLD LOCK
    world = world_setting or ""

    # 3. CHARACTER BLOCK
    char_block = build_character_block(character)

    # 4. COSTUME BLOCK
    costume_block = build_costume_block(costume)

    # 5. ENVIRONMENT BLOCK
    env_block = build_environment_block(environment)

    # 6. ACTION BLOCK
    action_block = build_action_block(scene)

    # 7. CAMERA BLOCK
    camera_block = build_camera_block(scene)

    # 8. CONTINUITY LOCKS
    continuity_block = build_continuity_block(character, costume, environment)

    # 9. NEGATIVE PROMPT
    style_lock = None
    if character:
        style_lock = character.get("style_lock", {})
    negative = build_negative_prompt(scene, global_negative, style_lock)

    # ---- Compile in deterministic order ----
    prompt_parts = []

    if style:
        prompt_parts.append(style)
    if world:
        prompt_parts.append(world)
    if action_block:
        prompt_parts.append(action_block)
    if char_block:
        prompt_parts.append(char_block)
    if costume_block:
        prompt_parts.append(costume_block)
    if env_block:
        prompt_parts.append(env_block)
    if camera_block:
        prompt_parts.append(camera_block)
    if continuity_block:
        prompt_parts.append(continuity_block)

    compiled = ". ".join(p for p in prompt_parts if p)

    return {
        "prompt": compiled,
        "negative_prompt": negative,
        "blocks": {
            "global_style": style,
            "world_setting": world,
            "character": char_block,
            "costume": costume_block,
            "environment": env_block,
            "action": action_block,
            "camera": camera_block,
            "continuity": continuity_block,
        },
    }


# ---- Template Assemblers ----

def assemble_character_sheet(character: dict, costume: dict = None,
                              global_style: str = "") -> dict:
    """Assemble a character sheet generation prompt."""
    style = global_style or DEFAULT_GLOBAL_STYLE
    char_block = build_character_block(character)
    costume_block = build_costume_block(costume)

    parts = [
        style,
        f"Character design sheet of {character.get('name', 'character')}",
        char_block,
    ]
    if costume_block:
        parts.append(costume_block)
    parts.append("Clean white background, soft lighting, multiple angles showing front side and back views")

    return {
        "prompt": ". ".join(p for p in parts if p),
        "negative_prompt": DEFAULT_NEGATIVE + ", no text, no labels",
    }


def assemble_environment_sheet(environment: dict, global_style: str = "") -> dict:
    """Assemble an environment reference sheet prompt."""
    style = global_style or DEFAULT_GLOBAL_STYLE
    env_block = build_environment_block(environment)

    parts = [
        style,
        f"Environment reference sheet, multiple angles of same location",
        env_block,
        "No people",
    ]

    return {
        "prompt": ". ".join(p for p in parts if p),
        "negative_prompt": DEFAULT_NEGATIVE + ", no people, no text, no labels",
    }


# ---- Shot Prompt Engine (Cinematic) ----

# Style tiers for the 3 variants
_STYLE_TIERS = {
    "safe": {
        "prefix": "high quality cinematic video, realistic lighting, detailed textures",
        "camera_boost": "",
        "perf_boost": "",
    },
    "cinematic": {
        "prefix": "hyper-realistic cinematic 4K, anamorphic lens, film grain, soft bloom, shallow depth of field",
        "camera_boost": ", professional cinematography, precise framing",
        "perf_boost": ", naturalistic acting, grounded physicality",
    },
    "experimental": {
        "prefix": "hyper-stylized cinematic 4K, anamorphic lens, heavy film grain, halation, chromatic aberration, dream-like atmosphere",
        "camera_boost": ", unconventional framing, bold visual choices, auteur cinematography",
        "perf_boost": ", heightened expressionism, stylized movement",
    },
}


def _build_shot_camera_block(shot: dict) -> str:
    """Build detailed camera language from shot data."""
    camera = shot.get("camera", {})
    framing = shot.get("framing", {})
    parts = []

    # Shot type
    shot_type = camera.get("shot_type", "medium")
    parts.append(f"{shot_type} shot")

    # Lens
    lens = camera.get("lens", "35mm")
    parts.append(f"{lens} lens")

    # Movement
    movement = camera.get("movement", "static")
    if camera.get("preset"):
        try:
            from lib.cinematic_engine import CAMERA_PRESETS
            preset = CAMERA_PRESETS.get(camera["preset"], {})
            if preset.get("description"):
                parts.append(preset["description"])
                # Preset already includes everything — skip individual fields
                return "Camera: " + ", ".join(parts)
        except ImportError:
            pass
    if movement and movement != "static":
        parts.append(movement)
    elif movement == "static":
        parts.append("locked-off static camera")

    # Height + angle
    height = camera.get("height", "eye")
    angle = camera.get("angle", "straight")
    if height != "eye":
        parts.append(f"{height} camera height")
    if angle != "straight":
        parts.append(f"{angle} angle")

    # Composition
    comp = framing.get("composition", "")
    if comp:
        parts.append(comp.replace("_", " ") + " composition")

    # Depth
    depth = framing.get("depth", "")
    if depth and depth != "mid":
        parts.append(f"subject in {depth}")

    return "Camera: " + ", ".join(parts)


def _build_shot_performance_block(shot: dict) -> str:
    """Build performance language from shot data."""
    perf = shot.get("performance", {})
    if not perf:
        return ""

    parts = []

    intensity = perf.get("intensity", 5)
    if intensity <= 3:
        parts.append("understated and restrained")
    elif intensity <= 5:
        parts.append("grounded naturalistic energy")
    elif intensity <= 7:
        parts.append("heightened intensity")
    else:
        parts.append("maximum raw intensity")

    emotion = perf.get("emotion", "")
    if emotion:
        EMOTION_LANGUAGE = {
            "calm": "serene composure, still presence",
            "tense": "coiled tension, alert stillness, breath held",
            "confident": "commanding presence, direct gaze, open posture",
            "aggressive": "forward lean, hard eyes, clenched physicality",
            "vulnerable": "soft guard, exposed expression, fragile stance",
            "defiant": "chin raised, unflinching, squared shoulders",
            "melancholy": "weight in the shoulders, distant eyes, quiet ache",
        }
        parts.append(EMOTION_LANGUAGE.get(emotion, emotion))

    energy = perf.get("energy", "controlled")
    speed = perf.get("speed", "normal")
    if energy == "low":
        parts.append("minimal movement")
    elif energy == "explosive":
        parts.append("explosive physicality")
    if speed == "slow":
        parts.append("deliberate pacing, stretched time")
    elif speed == "fast":
        parts.append("sharp quick movement, urgent tempo")

    return "Performance: " + ", ".join(parts)


def _build_shot_continuity_block(shot: dict, prev_shot: dict = None) -> str:
    """Build continuity enforcement language."""
    cont = shot.get("continuity", {})
    parts = []

    if cont.get("lock_environment", True):
        parts.append("maintain exact environment from previous shot")
    if cont.get("lock_lighting", True):
        parts.append("match lighting direction and color temperature")
    if cont.get("lock_character_pose", False) and prev_shot:
        prev_end = (prev_shot.get("action", {}).get("end_pose") or "").strip()
        if prev_end:
            parts.append(f"character begins in {prev_end} position")
    if cont.get("lock_props", True):
        parts.append("all props in consistent positions")

    # Always enforce character appearance
    parts.append("maintain exact character appearance and costume throughout")

    if not parts:
        return ""
    return "Continuity: " + ", ".join(parts)


def compile_shot_prompt(
    shot: dict,
    character: dict = None,
    costume: dict = None,
    environment: dict = None,
    global_style: str = "",
    world_setting: str = "",
    global_negative: str = "",
    prev_shot: dict = None,
    tier: str = "cinematic",
) -> dict:
    """
    Compile a structured shot into a cinematic generation prompt.

    Args:
        shot: full shot object with camera, action, performance, etc.
        character: character entity dict
        costume: costume entity dict
        environment: environment entity dict
        global_style: universal style prompt
        world_setting: world rules
        global_negative: negative prompt override
        prev_shot: previous shot for continuity
        tier: "safe" | "cinematic" | "experimental"

    Returns:
        dict with prompt, negative_prompt, blocks
    """
    style_tier = _STYLE_TIERS.get(tier, _STYLE_TIERS["cinematic"])

    # 1. GLOBAL STYLE
    style_block = global_style or style_tier["prefix"]
    if global_style and tier != "safe":
        style_block = f"{style_tier['prefix']}, {global_style}"

    # 1b. STYLE LIBRARY SELECTIONS (resolve presets to prompt language)
    style_selections = shot.get("style_selections", {})
    if style_selections and any(style_selections.values()):
        try:
            from lib.shot_style_library import resolve_presets
            resolved = resolve_presets(style_selections)
            if resolved:
                style_block = f"{style_block}, {resolved}"
        except Exception:
            pass

    # 2. ENVIRONMENT (with world setting)
    env_parts = []
    if world_setting:
        env_parts.append(world_setting)
    env_block = build_environment_block(environment)
    if env_block:
        env_parts.append(env_block)
    # Add lighting from environment
    if environment:
        lighting = environment.get("lighting", "")
        atmos = environment.get("atmosphere", "")
        if lighting and lighting.lower() not in (env_block or "").lower():
            env_parts.append(lighting)
        if atmos and atmos.lower() not in (env_block or "").lower():
            env_parts.append(atmos)
    env_final = ", ".join(env_parts) if env_parts else ""

    # 3. CHARACTER + COSTUME
    char_block = build_character_block(character)
    costume_block = build_costume_block(costume)
    identity = ""
    if char_block or costume_block:
        id_parts = [p for p in [char_block, costume_block] if p]
        identity = ", ".join(id_parts)

    # 4. ACTION
    action = shot.get("action", {})
    action_parts = []
    if action.get("summary"):
        action_parts.append(action["summary"])
    if action.get("start_pose"):
        action_parts.append(f"starting from {action['start_pose']}")
    if action.get("end_pose"):
        action_parts.append(f"transitioning to {action['end_pose']}")
    # Layers add depth
    layers = shot.get("layers", {})
    if layers.get("surface") and layers["surface"] not in (action.get("summary") or ""):
        action_parts.append(layers["surface"])
    action_block = ", ".join(action_parts) if action_parts else ""

    # 5. CAMERA
    camera_block = _build_shot_camera_block(shot) + style_tier["camera_boost"]

    # 6. PERFORMANCE
    perf_block = _build_shot_performance_block(shot) + style_tier["perf_boost"]

    # 7. CONTINUITY
    continuity_block = _build_shot_continuity_block(shot, prev_shot)

    # 8. STYLE MEMORY ENFORCEMENT
    style_enforce = ""
    try:
        from lib.cinematic_engine import StyleMemory
        sm = StyleMemory()
        style_enforce = sm.build_enforcement_block()
    except Exception:
        pass

    # ---- Compile in strict cinematic order ----
    sections = []
    if style_block:
        sections.append(style_block)
    if env_final:
        sections.append(env_final)
    if identity:
        sections.append(identity)
    if action_block:
        sections.append(action_block)
    if camera_block:
        sections.append(camera_block)
    if perf_block:
        sections.append(perf_block)
    if continuity_block:
        sections.append(continuity_block)
    if style_enforce:
        sections.append(style_enforce)

    compiled = ". ".join(sections)

    # Negative prompt
    neg = global_negative or DEFAULT_NEGATIVE
    neg += ", no character duplication, no costume changes, no lighting shifts, no prop teleportation"

    return {
        "prompt": compiled,
        "negative_prompt": neg,
        "tier": tier,
        "blocks": {
            "style": style_block,
            "environment": env_final,
            "identity": identity,
            "action": action_block,
            "camera": camera_block,
            "performance": perf_block,
            "continuity": continuity_block,
        },
    }


def compile_shot_variants(
    shot: dict,
    character: dict = None,
    costume: dict = None,
    environment: dict = None,
    global_style: str = "",
    world_setting: str = "",
    global_negative: str = "",
    prev_shot: dict = None,
) -> dict:
    """
    Generate 3 prompt variations for a shot: safe, cinematic, experimental.

    Returns dict with keys: safe, cinematic, experimental — each containing
    prompt, negative_prompt, tier, blocks.
    """
    variants = {}
    for tier in ("safe", "cinematic", "experimental"):
        variants[tier] = compile_shot_prompt(
            shot=shot, character=character, costume=costume,
            environment=environment, global_style=global_style,
            world_setting=world_setting, global_negative=global_negative,
            prev_shot=prev_shot, tier=tier,
        )
    return variants


# ---- V4 Shot Prompt Builder (compact, budget-managed) ----

PROMPT_CHAR_LIMIT = 1000  # Runway API hard limit


def compile_shot_prompt_v4(
    shot: dict,
    beat: dict = None,
    style_bible: dict = None,
    character: dict = None,
    costume: dict = None,
    environment: dict = None,
    prev_shot: dict = None,
    is_first_in_beat: bool = False,
    package_notes: dict = None,
    taste_modifiers: dict = None,
) -> str:
    """
    Build a compact V4 shot prompt that fits within 1000 chars.

    Strategy:
    - First shot in beat: full environment + character + action + camera
    - Subsequent shots: delta only (action + camera change + continuity anchors)
    - Screen direction always baked in
    - Lock strengths (from shot dict) control how much context is included

    Returns:
        Assembled prompt string, guaranteed <= 1000 chars
    """
    style_bible = style_bible or {}
    beat = beat or {}

    char_lock = shot.get("character_lock_strength", 0.8)
    env_lock = shot.get("environment_lock_strength", 0.7)
    style_lock = shot.get("style_lock_strength", 0.9)

    parts = []
    budget = PROMPT_CHAR_LIMIT

    # 1. Style prefix (always, but compressed)
    if style_lock > 0.3:
        style_str = style_bible.get("global_style", DEFAULT_GLOBAL_STYLE)
        if style_lock < 0.7:
            style_str = style_str[:60]  # truncate for low lock
        parts.append(style_str)

    # 2. Environment (first shot in beat OR high lock)
    if is_first_in_beat or env_lock >= 0.8:
        env_block = build_environment_block(environment)
        if env_block:
            parts.append(env_block[:200] if is_first_in_beat else env_block[:80])
    elif env_lock >= 0.5 and environment:
        # Brief env reminder
        env_name = environment.get("name", environment.get("description", ""))
        if env_name:
            parts.append(f"in {env_name[:50]}")

    # 3. Character (first shot OR high lock)
    if is_first_in_beat or char_lock >= 0.8:
        char_block = build_character_block(character)
        if char_block:
            parts.append(char_block[:180] if is_first_in_beat else char_block[:80])
        cost_block = build_costume_block(costume)
        if cost_block:
            parts.append(cost_block[:100] if is_first_in_beat else cost_block[:50])
    elif char_lock >= 0.5 and character:
        # Brief character reminder
        name = character.get("name", "")
        if name:
            parts.append(name)

    # 4. Action (always — this is the shot-specific content)
    action = shot.get("action", "")
    if isinstance(action, dict):
        action = action.get("summary", "")
    if action:
        parts.append(action[:250])

    # 5. Camera + shot size
    shot_size = shot.get("shot_size", "MS")
    movement = shot.get("movement", "static")
    lens = shot.get("lens_feel", "")
    angle = shot.get("angle", "eye_level")

    cam_parts = [f"{shot_size} shot"]
    if lens:
        cam_parts.append(lens)
    if movement and movement != "static":
        cam_parts.append(movement.replace("_", " "))
    elif movement == "static":
        cam_parts.append("locked static camera")
    if angle and angle != "eye_level":
        cam_parts.append(f"{angle.replace('_', ' ')} angle")
    parts.append(", ".join(cam_parts))

    # 6. Screen direction
    screen_dir = shot.get("screen_direction", "neutral")
    if screen_dir == "L2R":
        parts.append("subject moves left to right")
    elif screen_dir == "R2L":
        parts.append("subject moves right to left")

    # 7. Emotion
    emotion = shot.get("emotion", "")
    if emotion:
        parts.append(f"mood: {emotion[:60]}")

    # 8. Continuity anchors (delta from previous)
    anchors = shot.get("continuity_anchors", [])
    if anchors:
        parts.append("maintain: " + ", ".join(a[:40] for a in anchors[:3]))

    # 9. Preproduction package notes (must_keep/avoid from approved packages)
    if package_notes:
        keeps = package_notes.get("must_keep", [])
        avoids = package_notes.get("avoid", [])
        if keeps:
            parts.append("keep: " + ", ".join(k[:30] for k in keeps[:3]))
        if avoids:
            parts.append("avoid: " + ", ".join(a[:30] for a in avoids[:2]))

    # 10. Taste profile modifiers (lighting, texture, tone from blended taste)
    if taste_modifiers:
        taste_parts = []
        for key in ("lighting", "texture", "tone", "realism", "composition"):
            mod = taste_modifiers.get(key, "")
            if mod:
                taste_parts.append(mod)
        if taste_parts:
            parts.append(", ".join(taste_parts[:3]))

    # Assemble and enforce budget
    assembled = ". ".join(p for p in parts if p)

    if len(assembled) > PROMPT_CHAR_LIMIT:
        # Progressive trimming: cut continuity, then emotion, then env detail
        assembled = ". ".join(p for p in parts[:-1] if p)  # drop continuity
    if len(assembled) > PROMPT_CHAR_LIMIT:
        assembled = assembled[:PROMPT_CHAR_LIMIT - 3] + "..."

    return assembled


# ── V5 Anchor Composition Prompt ──

# Shot family → framing language for still image composition
_FAMILY_FRAMING = {
    "wide_establishing": "a wide establishing cinematic still showing the full environment",
    "environment_reestablish": "a wide re-establishing shot of the location",
    "character_closeup": "a tight close-up portrait focusing on the character's face and expression",
    "emotional_moment": "an intimate close-up capturing raw emotion on the character's face",
    "wardrobe_reveal": "a medium shot showcasing the character's outfit and costume details",
    "prop_interaction": "a medium shot of the character interacting with a key prop",
    "action_scene": "a dynamic medium-wide shot capturing the character in action within the environment",
    "dialogue_shot": "a medium shot framing the character mid-conversation",
    "insert_detail": "a tight detail shot of a specific object or texture",
    "generic": "a cinematic still frame",
}


def compile_anchor_prompt(shot: dict, style_bible: dict = None,
                          refs: list = None, taste_mods: dict = None) -> str:
    """
    Build a prompt for text_to_image anchor composition.

    Optimized for STILL images (no camera movement language).
    Includes @Tag references for active canonical refs.
    Guaranteed under 1000 chars.

    Args:
        shot: Shot dict with action, emotion, shot_size, shot_family
        style_bible: Global style dict
        refs: Selected canonical refs [{tag, package_type, ...}]
        taste_mods: Optional taste modifiers

    Returns:
        Prompt string with @Tag mentions, under 1000 chars.
    """
    style_bible = style_bible or {}
    refs = refs or []

    parts = []

    # 1. Framing language from shot family
    family = shot.get("shot_family", "generic")
    framing = _FAMILY_FRAMING.get(family, _FAMILY_FRAMING["generic"])

    # Build @Tag-aware framing
    tag_names = {r.get("tag", "") for r in refs}
    if "Character" in tag_names:
        framing = framing.replace("the character", "@Character")
        framing = framing.replace("a character", "@Character")
    if "Setting" in tag_names:
        framing += " set in @Setting"
    if "Costume" in tag_names:
        framing += ", wearing outfit from @Costume"
    if "Prop" in tag_names:
        framing += ", featuring @Prop"
    parts.append(framing)

    # 2. Style
    style_str = style_bible.get("global_style", "")
    if style_str:
        parts.append(style_str[:80])

    # 3. Action / scene description
    action = shot.get("action", "")
    if isinstance(action, dict):
        action = action.get("summary", "")
    if action:
        parts.append(action[:250])

    # 4. Emotion
    emotion = shot.get("emotion", "")
    if emotion:
        parts.append(f"mood: {emotion[:50]}")

    # 5. Taste modifiers
    if taste_mods:
        taste_parts = []
        for key in ("lighting", "texture", "tone"):
            mod = taste_mods.get(key, "")
            if mod:
                taste_parts.append(mod)
        if taste_parts:
            parts.append(", ".join(taste_parts[:3]))

    # 6. Technical quality
    parts.append("cinematic still, high detail, 4k, professional color grading")

    # Assemble
    assembled = ". ".join(p for p in parts if p)
    if len(assembled) > PROMPT_CHAR_LIMIT:
        assembled = assembled[:PROMPT_CHAR_LIMIT - 3] + "..."

    return assembled
