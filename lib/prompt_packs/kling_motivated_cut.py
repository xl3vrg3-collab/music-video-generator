"""Kling prompt for motivated cut / hidden cut animation.

When smooth interpolation is wrong, animate for a cut-friendly exit.
The motion should create a natural cut point (whip pan, blink, etc).
"""

CUT_MOTION_TEMPLATES = {
    "motivated_cut": "Subject completes action with natural momentum.",
    "hidden_cut": "Camera sweeps past a foreground element, creating a natural wipe.",
    "whip_pan_cut": "Fast camera whip pan to the right, motion blur at the end.",
    "object_wipe_cut": "Subject or prop moves across frame, briefly filling the lens.",
    "foreground_wipe_cut": "Camera pushes through foreground foliage/element creating a wipe.",
    "blink_cut": "Subject blinks at the end of the shot, natural pause point.",
    "reaction_cut": "Subject reacts with a look or turn, creating eyeline motivation.",
    "insert_cut": "Camera pushes in tight on a detail (hands, object, texture).",
    "match_cut": "Hold on a geometric shape or silhouette that matches the next shot.",
    "impact_cut": "Sudden stop or impact moment creates a natural hard cut point.",
    "audio_led_cut": "Motion settles into stillness, allowing audio to carry the transition.",
}


def render(*, cut_type="motivated_cut", motion_desc="", camera_move="",
           cinematic_tone="", **_kw) -> str:
    parts = []

    # Cut-specific motion template
    template = CUT_MOTION_TEMPLATES.get(cut_type, CUT_MOTION_TEMPLATES["motivated_cut"])
    parts.append(template)

    if camera_move:
        parts.append(f"Camera: {camera_move}.")
    if motion_desc:
        parts.append(f"Additional motion: {motion_desc}.")

    parts.append("Preserve face, wardrobe, props, environment, lighting.")

    if cinematic_tone:
        parts.append(cinematic_tone)

    return " ".join(parts)


def render_negative(cut_type="motivated_cut", **_kw) -> str:
    base = "blur, distortion, morphing, face change, watermark, low quality"
    if cut_type == "whip_pan_cut":
        return base  # blur is expected for whip pan
    return base + ", extra people, style drift"
