"""Kling prompt for bridge-based animation (start→bridge or bridge→end).

Shorter segments, tighter motion constraints.
"""

def render(*, motion_desc="", camera_move="", duration_sec=3,
           segment="start_to_bridge", cinematic_tone="", **_kw) -> str:
    parts = []

    if segment == "start_to_bridge":
        parts.append("Animate from start frame toward the bridge point.")
        parts.append("Smooth, restrained motion. No sudden changes.")
    elif segment == "bridge_to_end":
        parts.append("Animate from bridge point to the end frame.")
        parts.append("Continue the established motion direction.")
    else:
        parts.append("Animate through the bridge transition.")

    if camera_move:
        parts.append(f"Camera: {camera_move}.")
    if motion_desc:
        parts.append(f"Motion: {motion_desc}.")

    parts.append("Preserve exact face, wardrobe, props, environment, lighting.")
    parts.append("Avoid morphing. Avoid redesigning the shot.")

    if cinematic_tone:
        parts.append(cinematic_tone)

    return " ".join(parts)


def render_negative(**_kw) -> str:
    return ("blur, distortion, morphing, face change, wardrobe change, "
            "extra limbs, watermark, text, low quality, style drift")
