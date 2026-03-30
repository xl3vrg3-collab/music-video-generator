"""
Scene planner.
Takes audio analysis + user style prompts and generates a list of scenes,
each with a video-generation prompt tailored to section type and energy.
Includes per-scene transition type assignment based on section flow.
Includes coherence pass for visual consistency across adjacent scenes.
"""

import re
import random

# ---- Transition types ----
TRANSITION_TYPES = [
    "crossfade",    # xfade=transition=fade (default)
    "hard_cut",     # instant cut, no filter
    "fade_black",   # fade to black, then fade in
    "wipe_left",    # horizontal wipe left
    "wipe_right",   # horizontal wipe right
    "dissolve",     # like crossfade but slower duration
    "zoom_in",      # zoom into center, next scene zooms out
    "glitch",       # rapid 0.1s alternating cuts (cyberpunk)
]

# Auto-assign transitions based on section flow (from_type -> to_type)
TRANSITION_MAP = {
    ("intro", "verse"):   "dissolve",
    ("intro", "chorus"):  "zoom_in",
    ("verse", "chorus"):  "hard_cut",
    ("verse", "verse"):   "crossfade",
    ("verse", "bridge"):  "dissolve",
    ("chorus", "verse"):  "fade_black",
    ("chorus", "chorus"): "crossfade",
    ("chorus", "bridge"): "dissolve",
    ("chorus", "outro"):  "fade_black",
    ("bridge", "chorus"): "hard_cut",
    ("bridge", "verse"):  "dissolve",
    ("bridge", "outro"):  "fade_black",
}

# Fallback: any section going to outro gets fade_black
_DEFAULT_TRANSITION = "crossfade"


def auto_assign_transition(from_type: str, to_type: str) -> str:
    """Pick a transition type based on section flow."""
    if to_type == "outro":
        return "fade_black"
    return TRANSITION_MAP.get((from_type, to_type), _DEFAULT_TRANSITION)


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

# ---- Natural clip duration ranges per section type (Area 2) ----
SECTION_DURATION_RANGES = {
    "intro":  (6, 8),    # establishing, slow
    "verse":  (4, 6),    # moderate pacing
    "chorus": (3, 5),    # fast cuts, high energy
    "bridge": (6, 10),   # contemplative, slower
    "outro":  (8, 10),   # closing, slow
}


def _pick_natural_duration(section_type: str, energy: float, beats: list | None = None,
                            start: float = 0, end: float = 8) -> float:
    """
    Pick a natural clip duration based on section type and energy.

    Args:
        section_type: "intro", "verse", "chorus", "bridge", "outro"
        energy: 0.0-1.0 energy level
        beats: optional list of beat timestamps (seconds)
        start: scene start time
        end: scene end time

    Returns:
        duration in seconds, snapped to nearest beat if beats provided
    """
    lo, hi = SECTION_DURATION_RANGES.get(section_type, (4, 8))
    # High energy -> shorter duration within the range
    # Low energy -> longer duration within the range
    t = 1.0 - min(max(energy, 0), 1.0)  # invert: high energy = low t
    raw_dur = lo + t * (hi - lo)
    raw_dur = round(raw_dur, 1)

    # Clamp to actual section length
    section_len = end - start
    if raw_dur > section_len:
        raw_dur = section_len

    # Snap to nearest beat if beats are provided
    if beats:
        scene_end_target = start + raw_dur
        # Find closest beat to the target end time
        closest_beat = min(beats, key=lambda b: abs(b - scene_end_target))
        # Only snap if the beat is reasonably close (within 1.5 seconds)
        if abs(closest_beat - scene_end_target) < 1.5 and closest_beat > start + 2:
            raw_dur = round(closest_beat - start, 3)

    return max(raw_dur, 2.0)  # minimum 2 seconds


def plan_scenes(analysis: dict, style: str, seed: int | None = None,
                references: dict | None = None,
                natural_pacing: bool = True) -> list:
    """
    Generate a scene list from audio analysis and user style prompt.

    Args:
        analysis: dict from audio_analyzer.analyze()
        style: user-provided style description (e.g. "cyberpunk city neon rain")
        seed: optional random seed for reproducibility
        references: optional dict of {"name": "path/to/image.jpg"} for character/env references
        natural_pacing: if True, vary clip durations based on section type and energy (Area 2)

    Returns:
        list of scene dicts: [{start_sec, end_sec, duration, prompt, section_type,
                               matched_references, transition}]
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
            "transition": "crossfade",
        }]

    beats = analysis.get("beats", None)

    scenes = []
    for i, section in enumerate(sections):
        start = section["start"]
        end = section["end"]
        stype = section.get("type", "verse")
        energy = section.get("energy", 0.5)

        # Area 2: Natural clip lengths based on section type and energy
        if natural_pacing:
            dur = _pick_natural_duration(stype, energy, beats=beats, start=start, end=end)
        else:
            dur = round(end - start, 3)

        prompt = _build_prompt(style, stype, energy)
        matched = _match_references(prompt, refs)

        # Auto-assign transition to the *next* scene boundary
        # (transition is the effect leading INTO this scene from the previous one)
        if i == 0:
            transition = "crossfade"  # first scene: no previous, default
        else:
            prev_type = sections[i - 1].get("type", "verse")
            transition = auto_assign_transition(prev_type, stype)

        scenes.append({
            "start_sec": start,
            "end_sec": end,
            "duration": dur,
            "prompt": prompt,
            "section_type": stype,
            "matched_references": matched,
            "transition": transition,
        })

    # Coherence pass: ensure visual consistency across adjacent scenes
    scenes = coherence_pass(scenes)

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


# ---- Coherence pass ----

# Keywords that represent strong visual elements worth carrying across scenes
_VISUAL_ELEMENT_PATTERNS = [
    r'\bneon\b', r'\bcity\b', r'\brain\b', r'\bocean\b', r'\bforest\b',
    r'\bdesert\b', r'\bmountain\b', r'\bspace\b', r'\bsky\b', r'\bfire\b',
    r'\bwater\b', r'\bnight\b', r'\bsunset\b', r'\bsunrise\b', r'\bfog\b',
    r'\bsmoke\b', r'\bcrystal\b', r'\bglass\b', r'\bmetal\b', r'\bgold\b',
    r'\bsilver\b', r'\bpurple\b', r'\bblue\b', r'\bred\b', r'\bgreen\b',
    r'\bcyberpunk\b', r'\bsynthwave\b', r'\bgothic\b', r'\bvintage\b',
    r'\bindustrial\b', r'\babstract\b', r'\bminimal\b', r'\bflowers\b',
    r'\bstars\b', r'\bplanets\b', r'\bhologram\b', r'\blaser\b',
    r'\bchurch\b', r'\btemple\b', r'\balley\b', r'\bstreet\b',
    r'\brooftop\b', r'\btunnel\b', r'\bbridge\b', r'\bwindow\b',
]


def _extract_visual_elements(prompt: str) -> list:
    """Extract key visual element words from a prompt."""
    elements = []
    prompt_lower = prompt.lower()
    for pattern in _VISUAL_ELEMENT_PATTERNS:
        match = re.search(pattern, prompt_lower)
        if match:
            elements.append(match.group())
    return elements


def coherence_pass(scenes: list) -> list:
    """
    Post-process scene prompts for visual coherence across adjacent scenes.

    For each scene (after the first), appends a continuity note that references
    visual elements from the previous scene. Accumulates a context_carry string
    of key visual elements across the entire sequence so later scenes can
    reference motifs from earlier in the video.

    Modifies scenes in place and returns them.
    """
    if not scenes or len(scenes) < 2:
        return scenes

    context_carry = []  # Accumulated visual elements across all scenes

    # Extract elements from the first scene and seed the carry
    first_elements = _extract_visual_elements(scenes[0]["prompt"])
    context_carry.extend(first_elements)
    scenes[0]["context_carry"] = ", ".join(context_carry) if context_carry else ""

    for i in range(1, len(scenes)):
        prev_prompt = scenes[i - 1]["prompt"]
        curr_prompt = scenes[i]["prompt"]

        # Extract elements from previous scene
        prev_elements = _extract_visual_elements(prev_prompt)

        # Build coherence suffix
        coherence_parts = []

        # Reference previous scene's visual style
        coherence_parts.append(
            "continuing from the previous scene's visual style and color palette"
        )

        # If previous scene has specific visual elements, reference them
        if prev_elements:
            element_str = ", ".join(prev_elements[:4])  # limit to 4 elements
            coherence_parts.append(
                f"with echoes of {element_str} from the previous scene"
            )

        # If we have accumulated context, weave in recurring motifs
        if len(context_carry) >= 3:
            # Pick up to 3 recurring motifs from the accumulated context
            recurring = list(set(context_carry))[:3]
            coherence_parts.append(
                f"recurring visual motifs: {', '.join(recurring)}"
            )

        # Append coherence to the prompt (avoid duplication)
        coherence_suffix = ", ".join(coherence_parts)
        if coherence_suffix not in curr_prompt:
            scenes[i]["prompt"] = f"{curr_prompt}, {coherence_suffix}"

        # Update the carry with this scene's elements
        curr_elements = _extract_visual_elements(scenes[i]["prompt"])
        context_carry.extend(curr_elements)
        # Keep carry manageable (last 10 unique elements)
        seen = set()
        unique_carry = []
        for elem in reversed(context_carry):
            if elem not in seen:
                seen.add(elem)
                unique_carry.append(elem)
            if len(unique_carry) >= 10:
                break
        context_carry = list(reversed(unique_carry))
        scenes[i]["context_carry"] = ", ".join(context_carry) if context_carry else ""

    return scenes
