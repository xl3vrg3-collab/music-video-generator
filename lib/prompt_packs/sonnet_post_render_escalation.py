"""Sonnet prompt for escalated post-render critique.

Used when: hero shot, Haiku marks multi_issue, retry strategy uncertain,
or multiple retries already failed.
"""

SYSTEM = """\
You are a senior VFX supervisor reviewing rendered footage. You have deep \
expertise in identifying subtle continuity breaks, morphing artifacts, and \
motion quality issues. Provide precise, actionable feedback. Return ONLY valid JSON."""


def render(*, shot_id="", num_sampled_frames=8, continuity_lock="",
           transition_strategy="", motion_note="",
           haiku_critique=None, attempt_history=None,
           escalation_reason="", **_kw) -> str:
    parts = []

    parts.append("ESCALATED POST-RENDER REVIEW.")
    if escalation_reason:
        parts.append(f"Escalation reason: {escalation_reason}.")

    parts.append("IMAGE 1 = character/asset lock reference.")
    parts.append("IMAGE 2 = approved START frame.")
    parts.append("IMAGE 3 = approved END frame.")
    parts.append(f"IMAGES 4-{3 + num_sampled_frames} = sampled render frames.")

    parts.append(f"Shot: {shot_id}.")
    if transition_strategy:
        parts.append(f"Strategy: {transition_strategy}.")
    if motion_note:
        parts.append(f"Motion: {motion_note}.")
    if continuity_lock:
        parts.append(f"Lock: {continuity_lock}.")

    if haiku_critique:
        parts.append(f"Haiku's assessment: {haiku_critique.get('plain_english_summary', 'N/A')}.")
        parts.append(f"Haiku failure type: {haiku_critique.get('failure_type', 'N/A')}.")

    if attempt_history:
        parts.append(f"Previous attempts: {len(attempt_history)}.")

    parts.append("""
Provide thorough analysis. Score 0.0 to 1.0.

Return ONLY this JSON:
{
  "identity_retention": <float>,
  "wardrobe_retention": <float>,
  "prop_retention": <float>,
  "environment_retention": <float>,
  "lighting_consistency": <float>,
  "motion_quality": <float>,
  "morph_artifact_severity": <float>,
  "cut_quality": <float>,
  "cinematic_strength": <float>,
  "overall_pass": <bool>,
  "failure_type": "none | drift | morphing | harsh_cut | bad_motion | lighting_issue | composition_issue | multi_issue",
  "retry_strategy": "accept | rerender_lower_delta | use_alt_end | add_bridge | switch_to_cut | regenerate_end | regenerate_pair",
  "plain_english_summary": "<detailed summary>",
  "prompt_adjustments": ["<fix>", ...],
  "deep_reasoning": "<root cause analysis>",
  "confidence": <float>
}""")

    return " ".join(parts)
