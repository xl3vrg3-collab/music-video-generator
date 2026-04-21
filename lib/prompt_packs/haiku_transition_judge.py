"""Haiku prompt for transition compatibility scoring.

Inputs: lock references + start frame + end frame + optional bridge + metadata.
Output: structured JSON with 0.0–1.0 scores per dimension.
"""

SYSTEM = """\
You are a VFX transition continuity judge. You analyze frame pairs and score \
their compatibility for smooth video interpolation. Return ONLY valid JSON.

IMPORTANT: Shallow depth-of-field bokeh can make outdoor backgrounds appear \
bright or blurred. Do NOT misidentify bokeh blur as a "white studio backdrop." \
Look for environmental cues (trees, sky gradients, natural light direction) \
before concluding an image was shot in a studio."""


def render(*, shot_id="", duration_sec=5, transition_intent="",
           motion_note="", camera_note="", editorial_note="",
           max_allowed_change=None, continuity_lock="",
           has_bridge=False, **_kw) -> str:
    parts = []

    parts.append("Analyze these frames for transition compatibility.")

    # Image labels
    parts.append("IMAGE 1 = character/asset lock reference (ground truth).")
    parts.append("IMAGE 2 = START frame of the shot.")
    parts.append("IMAGE 3 = END frame of the shot (or next shot's start frame).")
    if has_bridge:
        parts.append("IMAGE 4 = BRIDGE frame (intermediate keyframe).")

    parts.append(f"Shot: {shot_id}, Duration: {duration_sec}s.")
    if transition_intent:
        parts.append(f"Intent: {transition_intent}.")
    if motion_note:
        parts.append(f"Motion: {motion_note}.")
    if camera_note:
        parts.append(f"Camera: {camera_note}.")
    if editorial_note:
        parts.append(f"Editorial: {editorial_note}.")
    if continuity_lock:
        parts.append(f"Character lock: {continuity_lock}.")

    if max_allowed_change:
        parts.append(f"Max allowed change: pose={max_allowed_change.get('pose', 0.35)}, "
                     f"camera={max_allowed_change.get('camera', 0.25)}, "
                     f"lighting={max_allowed_change.get('lighting', 0.15)}, "
                     f"background={max_allowed_change.get('background', 0.20)}.")

    parts.append("""
Score each dimension 0.0 to 1.0 (1.0 = perfect continuity).
Heavily penalize: face drift, wardrobe drift, prop teleporting, lighting direction flip, environment geometry shift, subject scale jump, camera axis flip, impossible pose delta, unrealistic motion for the duration.

Return ONLY this JSON:
{
  "scores": {
    "identity_continuity": <float>,
    "pose_continuity": <float>,
    "camera_continuity": <float>,
    "scene_continuity": <float>,
    "motion_plausibility": <float>,
    "overall_score": <float>
  },
  "risk_level": "low | medium | high",
  "recommended_action": "direct_animate | end_variants | bridge_frame | motivated_cut | regenerate_pair",
  "cut_recommendation": "none | motivated_cut | hidden_cut | whip_pan_cut | object_wipe_cut | foreground_wipe_cut | blink_cut | reaction_cut | insert_cut | match_cut | impact_cut | audio_led_cut",
  "main_failure_reasons": ["<reason1>", ...],
  "plain_english_summary": "<1-2 sentence summary>",
  "prompt_adjustments": ["<suggested fix>", ...]
}""")

    return " ".join(parts)
