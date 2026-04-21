"""Sonnet prompt for escalated transition analysis.

Used when: hero shots, borderline Haiku scores, repeated failures,
multiple failure types, or explicit escalation request.
Returns same schema as Haiku but with deeper reasoning.
"""

SYSTEM = """\
You are an expert VFX supervisor and film editor. You analyze frame pairs \
with deep knowledge of cinematography, continuity, and motion interpolation. \
Your analysis must be precise and actionable. Return ONLY valid JSON."""


def render(*, shot_id="", duration_sec=5, transition_intent="",
           motion_note="", camera_note="", editorial_note="",
           max_allowed_change=None, continuity_lock="",
           has_bridge=False, haiku_result=None,
           escalation_reason="", attempt_history=None, **_kw) -> str:
    parts = []

    parts.append("ESCALATED REVIEW: This shot requires deeper analysis.")
    if escalation_reason:
        parts.append(f"Escalation reason: {escalation_reason}.")

    # Image labels
    parts.append("IMAGE 1 = character/asset lock reference.")
    parts.append("IMAGE 2 = START frame.")
    parts.append("IMAGE 3 = END frame (or next shot's start).")
    if has_bridge:
        parts.append("IMAGE 4 = BRIDGE frame.")

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

    # Previous Haiku analysis for context
    if haiku_result:
        parts.append(f"Haiku's initial assessment: {haiku_result.get('plain_english_summary', 'N/A')}.")
        haiku_scores = haiku_result.get("scores", {})
        if haiku_scores:
            parts.append(f"Haiku scores: {haiku_scores}.")

    if attempt_history:
        parts.append(f"Previous attempts: {len(attempt_history)}.")
        for i, attempt in enumerate(attempt_history[-3:]):  # last 3
            parts.append(f"  Attempt {i+1}: strategy={attempt.get('strategy')}, "
                        f"result={attempt.get('result', 'unknown')}.")

    parts.append("""
Provide a thorough analysis. Score each dimension 0.0 to 1.0.

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
  "main_failure_reasons": ["<reason>", ...],
  "plain_english_summary": "<detailed summary>",
  "prompt_adjustments": ["<fix>", ...],
  "deep_reasoning": "<paragraph explaining root cause and recommended approach>",
  "confidence": <float 0.0-1.0>
}""")

    return " ".join(parts)
