"""Gemini prompt for repairing a frame that failed quality checks.

Targeted fix: addresses specific failures while preserving what worked.
"""

def render(*, subject_desc="", framing="medium shot",
           environment="", lighting="golden hour backlight",
           style_bible="", continuity_lock="", wardrobe_desc="",
           original_frame_desc="", failure_type="",
           failure_details="", fix_instructions="", **_kw) -> str:
    parts = []

    parts.append("REPAIR this frame. Fix ONLY the specific issues listed below.")
    parts.append("Preserve everything that is correct.")

    if original_frame_desc:
        parts.append(f"Original frame: {original_frame_desc}.")

    if failure_type:
        parts.append(f"Failure type: {failure_type}.")
    if failure_details:
        parts.append(f"Specific issue: {failure_details}.")
    if fix_instructions:
        parts.append(f"Fix: {fix_instructions}.")

    parts.append(f"{framing}.")
    if subject_desc:
        parts.append(f"{subject_desc}.")

    if wardrobe_desc:
        parts.append(f"Wardrobe must be: {wardrobe_desc}.")
    if environment:
        parts.append(f"Setting must be: {environment}.")
    parts.append(f"Lighting must be: {lighting}.")

    parts.append("Do NOT change anything that was not flagged as a failure.")

    if style_bible:
        parts.append(style_bible)
    if continuity_lock:
        parts.append(continuity_lock)

    return " ".join(parts)
