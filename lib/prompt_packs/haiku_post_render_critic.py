"""Haiku prompt for post-render quality critique.

Inputs: lock references + start frame + end frame + 6-12 sampled frames from output.
Output: structured JSON with retention scores and retry strategy.
"""

SYSTEM = """\
You are a VFX quality control reviewer. You analyze rendered video frames \
for identity drift, wardrobe changes, morphing artifacts, and motion quality. \
Return ONLY valid JSON."""


def render(*, shot_id="", num_sampled_frames=8, continuity_lock="",
           transition_strategy="", motion_note="", **_kw) -> str:
    parts = []

    parts.append("Review these rendered video frames for quality issues.")

    parts.append("IMAGE 1 = character/asset lock reference (ground truth).")
    parts.append("IMAGE 2 = approved START frame (what the video should start with).")
    parts.append("IMAGE 3 = approved END frame (what the video should end with).")
    parts.append(f"IMAGES 4-{3 + num_sampled_frames} = sampled frames from the Kling render output.")

    parts.append(f"Shot: {shot_id}.")
    if transition_strategy:
        parts.append(f"Strategy used: {transition_strategy}.")
    if motion_note:
        parts.append(f"Intended motion: {motion_note}.")
    if continuity_lock:
        parts.append(f"Character lock: {continuity_lock}.")

    parts.append("""
Score each dimension 0.0 to 1.0 (1.0 = perfect).

Return ONLY this JSON:
{
  "identity_retention": <float>,
  "wardrobe_retention": <float>,
  "prop_retention": <float>,
  "environment_retention": <float>,
  "lighting_consistency": <float>,
  "motion_quality": <float>,
  "morph_artifact_severity": <float 0=severe 1=none>,
  "cut_quality": <float>,
  "cinematic_strength": <float>,
  "overall_pass": <bool>,
  "failure_type": "none | drift | morphing | harsh_cut | bad_motion | lighting_issue | composition_issue | multi_issue",
  "retry_strategy": "accept | rerender_lower_delta | use_alt_end | add_bridge | switch_to_cut | regenerate_end | regenerate_pair",
  "plain_english_summary": "<1-2 sentence summary>",
  "prompt_adjustments": ["<fix>", ...],
  "worst_frame_index": <int or null>,
  "drift_starts_at_frame": <int or null>
}""")

    return " ".join(parts)
