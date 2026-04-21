"""Gemini prompt for generating end frames (shot exit anchors)."""

def render(*, subject_desc="", exit_action="", framing="medium shot",
           camera_height="eye level", lens="35mm", environment="",
           lighting="golden hour backlight", style_bible="",
           continuity_lock="", wardrobe_desc="", prop_desc="",
           emotional_tone="", start_frame_desc="", **_kw) -> str:
    parts = []

    # Context from start frame
    if start_frame_desc:
        parts.append(f"This is the FINAL FRAME of a shot that started with: {start_frame_desc}.")

    # Framing + camera
    parts.append(f"{framing}, {camera_height}, {lens} lens.")

    # Subject completing action
    if subject_desc:
        parts.append(f"{subject_desc}.")
    if exit_action:
        parts.append(f"Subject is completing: {exit_action}.")

    # Preserve everything from start
    parts.append("CRITICAL: Preserve exact same face, wardrobe, props, environment geometry, and lighting direction as the start frame.")

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

    if emotional_tone:
        parts.append(f"Mood: {emotional_tone}.")

    if style_bible:
        parts.append(style_bible)

    if continuity_lock:
        parts.append(continuity_lock)

    return " ".join(parts)
