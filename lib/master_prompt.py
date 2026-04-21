"""
Master Prompt Extraction Module (V5 Pipeline)

Parses a single creative prompt into structured production data using an LLM.
Extracts characters, costumes, environments, props, style, and scene breakdown
so the system can auto-generate all preproduction assets from one prompt.
"""

import json
import os
import re

# ── Reuse LLM infrastructure from story_planner ──
from lib.story_planner import (
    AVAILABLE_MODELS, DEFAULT_MODEL,
    _PROVIDER_URLS, _PROVIDER_KEYS,
)

# ── System prompt for asset extraction ──
EXTRACTION_SYSTEM_PROMPT = """\
You are an expert production designer and script supervisor for a film studio.

Given a creative prompt (a movie idea, concept, or story description), your job \
is to extract ALL production assets that would be needed to produce this as a \
cinematic short film.

You must identify and describe:
1. CHARACTERS — every person, animal, or sentient entity mentioned or implied
2. COSTUMES — what each character wears, inferred from context if not explicit
3. ENVIRONMENTS — every distinct location/setting mentioned or implied
4. PROPS — important objects that play a role in the story
5. STYLE — the overall visual style, color palette, and cinematic feel
6. SCENE BREAKDOWN — approximate scenes with locations and characters

RULES:
- If a character is described vaguely ("a man"), invent specific visual details \
  (age, build, hair, skin tone, distinguishing features) that fit the tone
- If wardrobe is not mentioned, infer appropriate wardrobe from context and setting
- Environments need specific atmospheric detail: time of day, weather, lighting, textures
- Props should only include items that appear on screen and matter to the story
- Style should capture the CINEMATIC FEEL, not just adjectives
- Scene breakdown should have clear cause-and-effect narrative flow

Output valid JSON only. No markdown fences, no explanation, no trailing text.
Keep descriptions concise (1-2 sentences max per field). Avoid prose or narrative in descriptions."""


EXTRACTION_USER_TEMPLATE = """\
CREATIVE PROMPT:
{prompt}

Extract all production assets needed to make this into a cinematic short film.

Return JSON in this exact format:
{{
    "narrative": {{
        "concept": "1-2 sentence story summary",
        "tone": "moody/uplifting/dark/tense/joyful/melancholic/etc",
        "genre": "drama/thriller/romance/horror/comedy/documentary/fantasy/etc",
        "approximate_duration_seconds": 30
    }},
    "style": {{
        "keywords": ["cinematic", "warm", "shallow depth of field"],
        "color_palette": "warm golds and amber with cool blue shadows",
        "lighting": "golden hour, soft directional light",
        "texture": "film grain, slightly desaturated",
        "reference_feel": "looks like a Terrence Malick film"
    }},
    "characters": [
        {{
            "name": "Character Name",
            "role": "protagonist/antagonist/supporting/extra",
            "physical_description": "detailed physical appearance: age, build, skin, hair color/style, eye color, distinguishing features",
            "expression_default": "determined/worried/joyful/etc",
            "wardrobe_notes": "what they wear in this story"
        }}
    ],
    "costumes": [
        {{
            "name": "Descriptive Costume Name",
            "character_name": "Which character wears this",
            "description": "detailed description: garment types, colors, materials, fit, accessories"
        }}
    ],
    "environments": [
        {{
            "name": "Location Name",
            "description": "what this place looks like physically",
            "lighting": "lighting conditions",
            "atmosphere": "mood and feeling of this place",
            "time_of_day": "dawn/morning/midday/afternoon/golden_hour/dusk/night"
        }}
    ],
    "props": [
        {{
            "name": "Prop Name",
            "description": "what it looks like, size, material, condition",
            "importance": "hero/supporting/background",
            "used_by": "character name or null"
        }}
    ],
    "scene_breakdown": [
        {{
            "scene_number": 1,
            "location": "Environment Name (must match an environment above)",
            "characters_present": ["Character Name"],
            "action": "what happens in this scene",
            "emotion": "emotional tone of this scene",
            "key_props": ["Prop Name"]
        }}
    ]
}}"""


def extract_production_data(prompt: str, model: str = None) -> dict:
    """
    Parse a creative prompt into structured production data via LLM.

    Args:
        prompt: The user's master creative prompt
        model: LLM model to use (default from env or claude-opus-4-7)

    Returns:
        dict with narrative, style, characters, costumes, environments,
        props, and scene_breakdown
    """
    import requests

    model = model or os.environ.get("LUMN_STORY_MODEL", DEFAULT_MODEL)
    model_info = AVAILABLE_MODELS.get(model, {})
    provider = model_info.get("provider", "anthropic")

    key_env = _PROVIDER_KEYS.get(provider, "")
    api_key = os.environ.get(key_env, "") if key_env else ""
    if not api_key:
        raise ValueError(f"{key_env} not set — cannot extract production data with {model}")

    user_prompt = EXTRACTION_USER_TEMPLATE.format(prompt=prompt)

    print(f"[MASTER PROMPT] Extracting production data using {model} ({provider})")

    # Call LLM
    if provider == "anthropic":
        resp = requests.post(
            _PROVIDER_URLS["anthropic"],
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 8192,
                "system": EXTRACTION_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
                "temperature": 0.5,
            },
            timeout=90,
        )
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(f"Anthropic API error {resp.status_code}: {detail}")
        data = resp.json()
        raw = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
    else:
        url = _PROVIDER_URLS.get(provider, _PROVIDER_URLS["openai"])
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 8192,
                "temperature": 0.5,
            },
            timeout=90,
        )
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(f"{provider} API error {resp.status_code}: {detail}")
        data = resp.json()
        raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    if not raw:
        raise RuntimeError("Empty response from LLM")

    # Parse JSON from response
    extraction = _parse_extraction(raw)
    extraction = _validate_and_fix(extraction)

    print(f"[MASTER PROMPT] Extracted: {len(extraction.get('characters', []))} characters, "
          f"{len(extraction.get('costumes', []))} costumes, "
          f"{len(extraction.get('environments', []))} environments, "
          f"{len(extraction.get('props', []))} props, "
          f"{len(extraction.get('scene_breakdown', []))} scenes")

    return extraction


def _repair_json(text: str) -> str:
    """Fix common LLM JSON issues: trailing commas, unescaped newlines."""
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    # Remove control chars except \n \r \t inside strings
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return text


def _parse_extraction(raw: str) -> dict:
    """Parse JSON from LLM response, handling markdown fences and malformed JSON."""
    # Try direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try markdown code block
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            try:
                return json.loads(_repair_json(m.group(1)))
            except json.JSONDecodeError:
                pass
    # First { to last }
    start = raw.find('{')
    end = raw.rfind('}')
    if start >= 0 and end > start:
        chunk = raw[start:end + 1]
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            try:
                return json.loads(_repair_json(chunk))
            except json.JSONDecodeError as e:
                raise ValueError(f"Could not parse extraction JSON after repair: {e}\nRaw start: {chunk[:300]}")
    raise ValueError(f"Could not find JSON in extraction response: {raw[:200]}")


def _validate_and_fix(data: dict) -> dict:
    """Validate extraction data and apply auto-fixes."""
    # Ensure all top-level keys exist
    data.setdefault("narrative", {"concept": "", "tone": "cinematic", "genre": "drama"})
    data.setdefault("style", {"keywords": ["cinematic"], "color_palette": "", "lighting": ""})
    data.setdefault("characters", [])
    data.setdefault("costumes", [])
    data.setdefault("environments", [])
    data.setdefault("props", [])
    data.setdefault("scene_breakdown", [])

    # Ensure characters have required fields
    for c in data["characters"]:
        c.setdefault("name", "Unknown Character")
        c.setdefault("role", "supporting")
        c.setdefault("physical_description", "")
        c.setdefault("expression_default", "neutral")
        c.setdefault("wardrobe_notes", "")

    # Ensure environments have required fields
    for e in data["environments"]:
        e.setdefault("name", "Unknown Location")
        e.setdefault("description", "")
        e.setdefault("lighting", "natural")
        e.setdefault("atmosphere", "")
        e.setdefault("time_of_day", "day")

    # Ensure costumes have required fields
    for co in data["costumes"]:
        co.setdefault("name", "Unnamed Costume")
        co.setdefault("character_name", "")
        co.setdefault("description", "")

    # Ensure props have required fields
    for p in data["props"]:
        p.setdefault("name", "Unnamed Prop")
        p.setdefault("description", "")
        p.setdefault("importance", "supporting")
        p.setdefault("used_by", None)

    # Auto-generate costumes for characters that don't have them
    char_names = {c["name"].lower() for c in data["characters"]}
    costume_char_names = {co["character_name"].lower() for co in data["costumes"]}
    for c in data["characters"]:
        if c["name"].lower() not in costume_char_names and c["wardrobe_notes"]:
            data["costumes"].append({
                "name": f"{c['name']}'s Outfit",
                "character_name": c["name"],
                "description": c["wardrobe_notes"],
            })

    # Validate scene_breakdown references
    env_names = {e["name"].lower() for e in data["environments"]}
    for scene in data["scene_breakdown"]:
        scene.setdefault("scene_number", 0)
        scene.setdefault("location", "")
        scene.setdefault("characters_present", [])
        scene.setdefault("action", "")
        scene.setdefault("emotion", "")
        scene.setdefault("key_props", [])

    return data


def extraction_to_packages(data: dict, mode: str = "fast") -> list:
    """
    Convert extraction data into preproduction package dicts.

    Args:
        data: Output from extract_production_data()
        mode: "fast" or "production" — controls sheet view count

    Returns:
        list of package dicts ready for PreproductionStore.save_package()
    """
    from lib.preproduction_assets import create_package

    packages = []

    # Characters
    for c in data.get("characters", []):
        pkg = create_package(
            package_type="character",
            name=c["name"],
            description=c.get("physical_description", ""),
            mode=mode,
            must_keep=[c.get("expression_default", "")],
            canonical_notes=c.get("physical_description", ""),
            lock_strength=0.9 if c.get("role") == "protagonist" else 0.7,
        )
        pkg["_extraction_role"] = c.get("role", "supporting")
        packages.append(pkg)

    # Costumes
    for co in data.get("costumes", []):
        # Link to character package
        char_name = co.get("character_name", "")
        related_char_id = ""
        for p in packages:
            if p["package_type"] == "character" and p["name"].lower() == char_name.lower():
                related_char_id = p["package_id"]
                break
        pkg = create_package(
            package_type="costume",
            name=co["name"],
            description=co.get("description", ""),
            mode=mode,
            related_ids={"character_id": related_char_id} if related_char_id else None,
            lock_strength=0.7,
        )
        packages.append(pkg)

    # Environments
    for e in data.get("environments", []):
        desc_parts = [e.get("description", "")]
        if e.get("lighting"):
            desc_parts.append(f"Lighting: {e['lighting']}")
        if e.get("atmosphere"):
            desc_parts.append(f"Atmosphere: {e['atmosphere']}")
        if e.get("time_of_day"):
            desc_parts.append(f"Time: {e['time_of_day']}")
        pkg = create_package(
            package_type="environment",
            name=e["name"],
            description=". ".join(p for p in desc_parts if p),
            mode=mode,
            lock_strength=0.8,
        )
        packages.append(pkg)

    # Props
    for p in data.get("props", []):
        pkg = create_package(
            package_type="prop",
            name=p["name"],
            description=p.get("description", ""),
            mode=mode,
            lock_strength=0.5 if p.get("importance") == "background" else 0.7,
        )
        packages.append(pkg)

    return packages


def extraction_to_pos_entities(data: dict) -> dict:
    """
    Convert extraction data into PromptOS entity format.

    Returns:
        {characters: [...], environments: [...], costumes: [...]}
        Each in the format expected by PromptOS.add_character() etc.
    """
    characters = []
    for c in data.get("characters", []):
        characters.append({
            "name": c["name"],
            "role": c.get("role", "supporting"),
            "physicalDescription": c.get("physical_description", ""),
            "description": c.get("physical_description", ""),
            "hair": "",
            "skinTone": "",
            "bodyType": "",
            "ageRange": "",
            "distinguishingFeatures": "",
            "defaultExpression": c.get("expression_default", "neutral"),
            "tags": [c.get("role", "supporting")],
        })

    environments = []
    for e in data.get("environments", []):
        environments.append({
            "name": e["name"],
            "description": e.get("description", ""),
            "lighting": e.get("lighting", "natural"),
            "atmosphere": e.get("atmosphere", ""),
            "timeOfDay": e.get("time_of_day", "day"),
            "location": e.get("description", "")[:100],
        })

    costumes = []
    for co in data.get("costumes", []):
        costumes.append({
            "name": co["name"],
            "characterName": co.get("character_name", ""),
            "description": co.get("description", ""),
        })

    return {
        "characters": characters,
        "environments": environments,
        "costumes": costumes,
    }


def extraction_to_style_bible(data: dict) -> dict:
    """Convert extraction style data into a style_bible dict for prompt assembly."""
    style = data.get("style", {})
    keywords = style.get("keywords", ["cinematic"])
    palette = style.get("color_palette", "")
    lighting = style.get("lighting", "")
    texture = style.get("texture", "")

    parts = list(keywords)
    if palette:
        parts.append(palette)
    if lighting:
        parts.append(lighting)
    if texture:
        parts.append(texture)

    return {
        "global_style": ", ".join(parts[:8]),
        "negative": "no text, no watermark, no blurry, no distorted faces",
        "color_palette": palette,
        "lighting": lighting,
        "texture": texture,
    }
