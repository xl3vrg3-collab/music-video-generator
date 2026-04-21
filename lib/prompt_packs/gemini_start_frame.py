"""Gemini prompt for generating start frames (shot anchors)."""

def render(*, subject_desc="", action="", framing="medium shot",
           camera_height="eye level", lens="35mm", environment="",
           lighting="golden hour backlight", style_bible="",
           continuity_lock="", wardrobe_desc="", prop_desc="",
           emotional_tone="", **_kw) -> str:
    parts = []

    # Framing + camera
    parts.append(f"{framing}, {camera_height}, {lens} lens.")

    # Subject + action
    if subject_desc:
        parts.append(f"{subject_desc}.")
    if action:
        parts.append(f"Action: {action}.")

    # Wardrobe + props
    if wardrobe_desc:
        parts.append(f"Wardrobe: {wardrobe_desc}.")
    if prop_desc:
        parts.append(f"Props: {prop_desc}.")

    # Environment
    if environment:
        parts.append(f"Setting: {environment}.")

    # Lighting
    parts.append(f"Lighting: {lighting}.")

    # Emotional tone
    if emotional_tone:
        parts.append(f"Mood: {emotional_tone}.")

    # Style bible
    if style_bible:
        parts.append(style_bible)

    # Continuity lock (always last — most important for consistency)
    if continuity_lock:
        parts.append(continuity_lock)

    return " ".join(parts)
