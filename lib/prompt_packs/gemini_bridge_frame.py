"""Gemini prompt for bridge frame generation.

A bridge frame is an intermediate keyframe that visually connects
a start frame and an end frame when they're too different to animate directly.
"""

def render(*, subject_desc="", start_desc="", end_desc="",
           framing="medium shot", environment="",
           lighting="golden hour backlight", style_bible="",
           continuity_lock="", wardrobe_desc="",
           bridge_position="midpoint", **_kw) -> str:
    parts = []

    parts.append("Generate a TRUE IN-BETWEEN keyframe.")
    parts.append("Do NOT redesign the shot.")
    parts.append("Interpolate between the approved start and approved end frames.")

    if start_desc:
        parts.append(f"START frame shows: {start_desc}.")
    if end_desc:
        parts.append(f"END frame shows: {end_desc}.")

    parts.append(f"This bridge frame represents the {bridge_position} of the transition.")

    parts.append(f"{framing}.")
    if subject_desc:
        parts.append(f"{subject_desc}.")

    parts.append("Preserve EXACT same face, wardrobe, props, environment, lighting.")
    parts.append("Preserve EXACT same visual style as both source frames.")

    if wardrobe_desc:
        parts.append(f"Wardrobe: {wardrobe_desc}.")
    if environment:
        parts.append(f"Setting: {environment}.")
    parts.append(f"Lighting: {lighting}.")

    if style_bible:
        parts.append(style_bible)
    if continuity_lock:
        parts.append(continuity_lock)

    return " ".join(parts)
