"""Sonnet prompt for deep optimizer analysis.

Used when: repeated system-level failures, project-wide pattern analysis,
or major strategy rethink needed.
"""

SYSTEM = """\
You are a senior AI pipeline architect. You analyze production data to find \
deep systemic patterns and recommend strategic improvements. Be thorough but \
conservative — bad recommendations are worse than no recommendations. \
Return ONLY valid JSON."""


def render(*, shot_records=None, failure_clusters=None,
           current_thresholds=None, haiku_recommendations=None,
           project_context="", escalation_reason="", **_kw) -> str:
    parts = []

    parts.append("DEEP ANALYSIS: Review the optimizer's findings and provide strategic recommendations.")

    if escalation_reason:
        parts.append(f"Escalation reason: {escalation_reason}.")
    if project_context:
        parts.append(f"Project context: {project_context}.")

    if shot_records:
        parts.append(f"Total records analyzed: {len(shot_records)}.")

    if haiku_recommendations:
        parts.append(f"Haiku's recommendations: {haiku_recommendations.get('summary', 'N/A')}.")
        rules = haiku_recommendations.get("prompt_rule_updates", [])
        if rules:
            parts.append(f"Haiku suggested {len(rules)} rule changes.")

    if failure_clusters:
        parts.append(f"Failure clusters: {len(failure_clusters)}.")

    parts.append("""
Provide deep strategic analysis. Validate or override Haiku's suggestions.

Return ONLY this JSON:
{
  "validated_rules": ["<rule that Haiku got right>", ...],
  "overridden_rules": [{"rule": "<Haiku's rule>", "override": "<your correction>", "reason": "<why>"}],
  "new_strategic_rules": [{"rule": "<new rule>", "confidence": <float>, "evidence": "<pattern>", "impact": "high | medium | low"}],
  "system_health": "healthy | degrading | needs_attention",
  "root_causes": ["<systemic issue>", ...],
  "priority_fixes": ["<most important fix>", ...],
  "summary": "<strategic assessment>",
  "confidence": <float>
}""")

    return " ".join(parts)
