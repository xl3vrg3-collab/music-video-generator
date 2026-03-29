"""
Scene planner.
Takes audio analysis + user style prompts and generates a list of scenes,
each with a video-generation prompt tailored to section type and energy.
"""

import random

# Section-specific prompt modifiers
SECTION_MOODS = {
    "intro": [
        "wide establishing shot",
        "slow camera pull through",
        "aerial view descending into",
        "fog revealing",
    ],
    "verse": [
        "medium shot tracking through",
        "steady dolly shot of",
        "close-up details of",
        "slow pan across",
    ],
    "chorus": [
        "dynamic sweeping shot of",
        "fast motion through",
        "high-energy montage of",
        "dramatic rotating shot of",
        "intense close-up of",
    ],
    "bridge": [
        "dreamy slow-motion shot of",
        "abstract flowing visuals of",
        "surreal morphing shapes in",
        "soft-focus transition through",
    ],
    "outro": [
        "pulling back wide shot of",
        "fading aerial view of",
        "slow dissolve revealing",
        "distant silhouette in",
    ],
}

ENERGY_DESCRIPTORS = {
    "low": ["calm", "muted tones", "subtle movement", "quiet atmosphere"],
    "mid": ["moderate motion", "balanced lighting", "flowing movement"],
    "high": ["vibrant colors", "fast motion", "intense lighting", "pulsing energy"],
}

QUALITY_SUFFIX = "cinematic, 4k, detailed, moody lighting, professional color grading"


def plan_scenes(analysis: dict, style: str, seed: int | None = None,
                references: dict | None = None) -> list:
    """
    Generate a scene list from audio analysis and user style prompt.

    Args:
        analysis: dict from audio_analyzer.analyze()
        style: user-provided style description (e.g. "cyberpunk city neon rain")
        seed: optional random seed for reproducibility
        references: optional dict of {"name": "path/to/image.jpg"} for character/env references

    Returns:
        list of scene dicts: [{start_sec, end_sec, duration, prompt, section_type, matched_references}]
    """
    if seed is not None:
        random.seed(seed)

    refs = references or {}

    sections = analysis.get("sections", [])
    if not sections:
        # Fallback: single scene for the whole track
        dur = analysis.get("duration", 8)
        matched = _match_references(style, refs)
        return [{
            "start_sec": 0,
            "end_sec": dur,
            "duration": dur,
            "prompt": f"{style}, {QUALITY_SUFFIX}",
            "section_type": "verse",
            "matched_references": matched,
        }]

    scenes = []
    for section in sections:
        start = section["start"]
        end = section["end"]
        dur = round(end - start, 3)
        stype = section.get("type", "verse")
        energy = section.get("energy", 0.5)

        prompt = _build_prompt(style, stype, energy)
        matched = _match_references(prompt, refs)

        scenes.append({
            "start_sec": start,
            "end_sec": end,
            "duration": dur,
            "prompt": prompt,
            "section_type": stype,
            "matched_references": matched,
        })

    return scenes


def _match_references(text: str, references: dict) -> list:
    """Find reference names mentioned in the text. Returns list of matched names."""
    text_lower = text.lower()
    return [name for name in references if name.lower() in text_lower]


def _build_prompt(style: str, section_type: str, energy: float) -> str:
    """Build a detailed video generation prompt."""
    # Pick a section-appropriate camera/mood modifier
    moods = SECTION_MOODS.get(section_type, SECTION_MOODS["verse"])
    mood = random.choice(moods)

    # Energy descriptor
    if energy < 0.35:
        energy_words = random.choice(ENERGY_DESCRIPTORS["low"])
    elif energy < 0.65:
        energy_words = random.choice(ENERGY_DESCRIPTORS["mid"])
    else:
        energy_words = random.choice(ENERGY_DESCRIPTORS["high"])

    return f"{mood} {style}, {energy_words}, {QUALITY_SUFFIX}"
