"""Gemini prompt for alternate end frame variants.

Used when the original end frame is incompatible with the start frame.
Generates a variant with REDUCED pose/camera delta while preserving identity.
"""

def render(*, subject_desc="", exit_action="", framing="medium shot",
           camera_height="eye level", environment="", lighting="golden hour backlight",
           style_bible="", continuity_lock="", wardrobe_desc="",
           max_pose_change="subtle", max_camera_change="minimal",
           start_frame_desc="", failure_reasons=None, **_kw) -> str:
    parts = []

    parts.append("Generate an ALTERNATE end frame for this shot.")
    if start_frame_desc:
        parts.append(f"The start frame shows: {start_frame_desc}.")

    if failure_reasons:
        parts.append(f"The previous end frame failed because: {'; '.join(failure_reasons)}.")
        parts.append("Fix these specific issues while preserving everything else.")

    parts.append(f"{framing}, {camera_height}.")

    if subject_desc:
        parts.append(f"{subject_desc}.")
    if exit_action:
        parts.append(f"Action: {exit_action}.")

    # Constraint: reduce delta
    parts.append(f"CONSTRAINT: Maximum pose change from start: {max_pose_change}.")
    parts.append(f"CONSTRAINT: Maximum camera change from start: {max_camera_change}.")
    parts.append("Preserve EXACT same face, wardrobe, props, environment, lighting.")
    parts.append("Only reduce or refine the motion/pose/framing delta.")

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
