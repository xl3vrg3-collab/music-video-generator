"""
Narrative AI Engine — Create coherent story arcs across the full music video.

Turns disconnected clips into real emotional and visual progression.
"""

# Arc type presets
ARC_TYPES = {
    "rise": "Steady build from low to high — character gains power, clarity, or freedom",
    "fall": "Descent from stability into chaos, loss, or ruin",
    "fall_and_redemption": "Collapse followed by recovery — broken then rebuilt",
    "search_and_discovery": "Quest for something missing — physical or emotional journey",
    "chase": "Pursuit — literal or metaphorical, escalating urgency",
    "transformation": "Character changes fundamentally — metamorphosis of identity",
    "loop_recurrence": "Time loop or repeating pattern — until something breaks it",
    "dream_to_reality": "Blur between inner world and outer truth — awakening",
    "fragmented_memory": "Non-linear memory pieces assembling into revelation",
    "performance_abstract": "Pure performance energy — emotion drives visual, not plot",
    "love_and_loss": "Connection then separation — tenderness then grief",
    "power_ascension": "Rise to dominance — growing command over world",
    "escape": "Breaking free from confinement — physical or psychological",
    "ritual_initiation": "Passage through ceremony or test — transformation through ordeal",
}

# Scene roles
SCENE_ROLES = [
    "Opening Image", "World Setup", "Inciting Shift", "Search",
    "Escalation", "Chorus Release", "Collapse", "Reflection",
    "Confrontation", "Transformation", "Final Resolve", "Ambiguous Ending",
]

# Section → role mapping for auto-assignment
SECTION_TO_ROLE = {
    "intro": "Opening Image",
    "verse": "World Setup",
    "pre-chorus": "Escalation",
    "chorus": "Chorus Release",
    "bridge": "Reflection",
    "outro": "Final Resolve",
}

# Emotional curve templates by arc type
EMOTIONAL_CURVES = {
    "rise": [0.2, 0.3, 0.5, 0.7, 0.85, 1.0],
    "fall": [0.9, 0.8, 0.6, 0.4, 0.2, 0.1],
    "fall_and_redemption": [0.7, 0.5, 0.2, 0.1, 0.4, 0.8],
    "search_and_discovery": [0.3, 0.4, 0.5, 0.6, 0.8, 0.9],
    "chase": [0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    "transformation": [0.3, 0.3, 0.5, 0.7, 0.9, 0.8],
    "loop_recurrence": [0.5, 0.6, 0.5, 0.7, 0.5, 0.8],
    "dream_to_reality": [0.4, 0.3, 0.5, 0.4, 0.7, 0.9],
    "fragmented_memory": [0.6, 0.3, 0.7, 0.2, 0.8, 0.5],
    "performance_abstract": [0.5, 0.7, 0.9, 0.7, 1.0, 0.6],
    "love_and_loss": [0.3, 0.6, 0.9, 0.8, 0.3, 0.2],
    "power_ascension": [0.2, 0.4, 0.6, 0.8, 0.95, 1.0],
    "escape": [0.2, 0.3, 0.5, 0.7, 0.9, 1.0],
    "ritual_initiation": [0.3, 0.5, 0.8, 0.4, 0.9, 0.7],
}


def generate_narrative_plan(
    arc_type: str = "rise",
    theme: str = "",
    storyline: str = "",
    lyrics: str = "",
    sections: list = None,
    characters: list = None,
    num_scenes: int = None,
) -> dict:
    """
    Generate a narrative plan with emotional arc and scene roles.

    Returns narrative_plan dict.
    """
    sections = sections or []
    characters = characters or []

    if not num_scenes:
        num_scenes = len(sections) if sections else 4

    # Arc info
    arc_desc = ARC_TYPES.get(arc_type, ARC_TYPES["rise"])

    # Emotional curve
    curve_template = EMOTIONAL_CURVES.get(arc_type, EMOTIONAL_CURVES["rise"])
    # Interpolate to match num_scenes
    emotional_curve = []
    for i in range(num_scenes):
        progress = i / max(num_scenes - 1, 1)
        idx = progress * (len(curve_template) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(curve_template) - 1)
        frac = idx - lo
        val = curve_template[lo] * (1 - frac) + curve_template[hi] * frac
        emotional_curve.append(round(val, 2))

    # Protagonist arc
    protagonist = characters[0].get("name", "protagonist") if characters else "protagonist"
    protagonist_arc = f"{protagonist}: {arc_desc}"

    # Build scene narrative assignments
    scene_plans = []
    # Distribute roles across scenes
    available_roles = list(SCENE_ROLES)
    role_map = {}

    for i in range(num_scenes):
        sec = sections[i] if i < len(sections) else {"type": "verse", "start": 0, "end": 0}
        sec_type = sec.get("type", "verse")

        # Assign role from section type or distribute
        if sec_type in SECTION_TO_ROLE:
            role = SECTION_TO_ROLE[sec_type]
        elif i == 0:
            role = "Opening Image"
        elif i == num_scenes - 1:
            role = "Final Resolve"
        elif available_roles:
            # Pick role matching progress through story
            progress = i / max(num_scenes - 1, 1)
            role_idx = int(progress * (len(available_roles) - 1))
            role = available_roles[min(role_idx, len(available_roles) - 1)]
        else:
            role = "Escalation"

        emotion_val = emotional_curve[i] if i < len(emotional_curve) else 0.5

        # Emotional goal from curve value
        if emotion_val < 0.3:
            emotional_goal = "quiet, subdued, reflective"
        elif emotion_val < 0.5:
            emotional_goal = "building tension, anticipation"
        elif emotion_val < 0.7:
            emotional_goal = "rising intensity, engagement"
        elif emotion_val < 0.9:
            emotional_goal = "peak emotion, catharsis"
        else:
            emotional_goal = "maximum impact, transcendence"

        # Symbolic goal from arc type
        symbolic_map = {
            "rise": ["seed planted", "roots growing", "reaching upward", "breaking through", "standing tall", "in full bloom"],
            "fall": ["the height", "first crack", "crumbling", "free fall", "impact", "dust settling"],
            "loop_recurrence": ["the pattern", "repetition", "glitch", "awareness", "break attempt", "reset or escape"],
        }
        symbols = symbolic_map.get(arc_type, ["beginning", "journey", "challenge", "crisis", "resolution", "aftermath"])
        sym_idx = int((i / max(num_scenes - 1, 1)) * (len(symbols) - 1))
        symbolic_goal = symbols[min(sym_idx, len(symbols) - 1)]

        # Transition to next scene
        if i < num_scenes - 1:
            next_emotion = emotional_curve[i + 1] if i + 1 < len(emotional_curve) else 0.5
            if next_emotion > emotion_val:
                transition = "energy builds, tension increases"
            elif next_emotion < emotion_val:
                transition = "energy drops, moment of release or reflection"
            else:
                transition = "steady continuation, same emotional plane"
        else:
            transition = "final moment — resolve, echo, or destabilize"

        scene_plan = {
            "scene_index": i,
            "scene_id": None,  # to be linked
            "role": role,
            "section_type": sec_type,
            "emotional_intensity": emotion_val,
            "emotional_goal": emotional_goal,
            "symbolic_goal": symbolic_goal,
            "surface": "",  # user fills or AI generates
            "narrative_transition_to_next": transition,
        }
        scene_plans.append(scene_plan)

    return {
        "theme": theme or f"A story of {arc_type.replace('_', ' ')}",
        "arc_type": arc_type,
        "arc_description": arc_desc,
        "protagonist_arc": protagonist_arc,
        "emotional_curve": emotional_curve,
        "num_scenes": num_scenes,
        "scenes": scene_plans,
    }


def get_arc_types() -> dict:
    """Return all available arc types with descriptions."""
    return dict(ARC_TYPES)


def get_scene_roles() -> list:
    """Return all scene role options."""
    return list(SCENE_ROLES)
