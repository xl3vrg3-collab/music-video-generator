"""Haiku prompt for self-healing optimizer — batch failure analysis.

Analyzes patterns across many shots to find reusable improvements.
"""

SYSTEM = """\
You are a production pipeline optimizer. You analyze structured shot-attempt \
data to find patterns and suggest rule improvements. Be conservative — only \
suggest changes with strong evidence. Return ONLY valid JSON."""


def render(*, shot_records=None, failure_clusters=None,
           current_thresholds=None, current_prompt_rules=None, **_kw) -> str:
    parts = []

    parts.append("Analyze these shot attempt records and suggest improvements.")

    if shot_records:
        parts.append(f"Total records: {len(shot_records)}.")
        # Summarize key stats
        total = len(shot_records)
        passed = sum(1 for r in shot_records if r.get("final_outcome") == "pass")
        parts.append(f"Pass rate: {passed}/{total} ({100*passed//max(total,1)}%).")

    if failure_clusters:
        parts.append(f"Failure clusters found: {len(failure_clusters)}.")
        for cluster in failure_clusters[:5]:
            parts.append(f"  - {cluster.get('type', '?')}: {cluster.get('count', 0)} occurrences, "
                        f"pattern: {cluster.get('pattern', '?')}.")

    if current_thresholds:
        parts.append(f"Current thresholds: {current_thresholds}.")

    if current_prompt_rules:
        parts.append(f"Current prompt rules: {len(current_prompt_rules)} active.")

    parts.append("""
Find patterns and suggest improvements. Only suggest changes with strong evidence.

Return ONLY this JSON:
{
  "prompt_rule_updates": [
    {"rule": "<rule description>", "confidence": <float>, "evidence": "<what pattern supports this>", "risk": "low | medium | high"}
  ],
  "threshold_changes": [
    {"threshold": "<name>", "current": <value>, "suggested": <value>, "reason": "<why>"}
  ],
  "fallback_order_changes": [
    {"context": "<when>", "current_order": [...], "suggested_order": [...], "reason": "<why>"}
  ],
  "shot_type_rules": [
    {"shot_type": "<type>", "recommendation": "<special handling>", "evidence": "<pattern>"}
  ],
  "confidence_adjustments": [
    {"dimension": "<which score>", "adjustment": "<direction>", "reason": "<why>"}
  ],
  "summary": "<1-2 sentence overall finding>"
}""")

    return " ".join(parts)
