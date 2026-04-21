"""Kling prompt for direct animation between start and end frames.

Focus: motion, timing, camera behavior ONLY.
Never: redesign character, reinterpret wardrobe, restyle environment.
"""

def render(*, motion_desc="", camera_move="", duration_sec=5,
           emotional_restraint="natural", cinematic_tone="",
           negative_prompt="", **_kw) -> str:
    parts = []

    # Motion instruction (the core I2V directive)
    if camera_move:
        parts.append(f"Camera: {camera_move}.")
    if motion_desc:
        parts.append(f"Motion: {motion_desc}.")

    # Preservation instructions
    parts.append("Preserve exact face identity throughout.")
    parts.append("Preserve exact wardrobe and props.")
    parts.append("Preserve exact environment and lighting.")
    parts.append("Animate only the approved motion.")

    # Emotional restraint
    if emotional_restraint:
        parts.append(f"Performance energy: {emotional_restraint}.")

    # Cinematic tone
    if cinematic_tone:
        parts.append(cinematic_tone)

    # Anti-morph
    parts.append("Avoid morphing artifacts. Avoid redesigning the shot.")

    return " ".join(parts)


def render_negative(**_kw) -> str:
    """Standard negative prompt for direct animation."""
    return ("blur, distortion, morphing, face change, wardrobe change, "
            "extra limbs, extra people, watermark, text, low quality, "
            "cartoon, anime, redesigned character, style drift")
