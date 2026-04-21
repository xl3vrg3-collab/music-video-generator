"""
Self-Healing Learning System — evidence-based pipeline improvement.

Stores structured results from every shot attempt, clusters failures,
and recommends prompt/threshold/strategy improvements.

Uses Haiku for cheap batch analysis, Sonnet for deep pattern analysis.

Storage: JSON files in output/pipeline/learning/
  - shot_attempts.json — per-shot attempt log
  - failure_clusters.json — grouped failure patterns
  - prompt_rules.json — active prompt rule adjustments
  - thresholds.json — current scoring thresholds
  - optimizer_history.json — history of optimizer runs
"""

import json
import os
import time
from collections import Counter, defaultdict

LEARNING_DIR = "output/pipeline/learning"


def _ensure_dir():
    os.makedirs(LEARNING_DIR, exist_ok=True)


def _load_json(filename: str) -> dict | list:
    path = os.path.join(LEARNING_DIR, filename)
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return {} if filename.endswith("thresholds.json") else []


def _save_json(filename: str, data):
    _ensure_dir()
    path = os.path.join(LEARNING_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Shot Attempt Logging
# ---------------------------------------------------------------------------

def log_attempt(project_id: str, scene_id: str, shot_id: str,
                attempt_data: dict) -> dict:
    """Log a single shot generation attempt.

    attempt_data should include:
    - start_frame_id, end_frame_id, bridge_frame_id (if any)
    - transition_analysis: judge scores
    - render_critique: critic scores
    - chosen_strategy
    - prompt_version
    - attempt_number
    - final_outcome: "pass" | "fail" | "retry"
    - failure_type (if failed)
    - retry_strategy (if retrying)
    - cost_estimate
    - duration_sec
    """
    attempts = _load_json("shot_attempts.json")
    if not isinstance(attempts, list):
        attempts = []

    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_id": project_id,
        "scene_id": scene_id,
        "shot_id": shot_id,
        **attempt_data,
    }

    attempts.append(record)
    _save_json("shot_attempts.json", attempts)
    return record


def get_shot_history(shot_id: str) -> list:
    """Get all attempts for a specific shot."""
    attempts = _load_json("shot_attempts.json")
    if not isinstance(attempts, list):
        return []
    return [a for a in attempts if a.get("shot_id") == shot_id]


def get_project_stats(project_id: str = None) -> dict:
    """Get aggregate stats for a project (or all projects)."""
    attempts = _load_json("shot_attempts.json")
    if not isinstance(attempts, list):
        return {"total": 0}

    if project_id:
        attempts = [a for a in attempts if a.get("project_id") == project_id]

    total = len(attempts)
    passed = sum(1 for a in attempts if a.get("final_outcome") == "pass")
    failed = sum(1 for a in attempts if a.get("final_outcome") == "fail")
    retried = sum(1 for a in attempts if a.get("final_outcome") == "retry")

    strategies = Counter(a.get("chosen_strategy", "unknown") for a in attempts)
    failure_types = Counter(
        a.get("failure_type", "unknown")
        for a in attempts if a.get("final_outcome") in ("fail", "retry")
    )

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "retried": retried,
        "pass_rate": round(passed / max(total, 1) * 100, 1),
        "strategies": dict(strategies),
        "failure_types": dict(failure_types),
    }


# ---------------------------------------------------------------------------
# Failure Clustering
# ---------------------------------------------------------------------------

def cluster_failures(min_occurrences: int = 2) -> list:
    """Group failures by pattern.

    Looks for recurring failure types, strategies that consistently fail,
    shot types that drift, and prompt patterns that cause issues.

    Returns list of clusters:
    [{type, count, pattern, affected_shots, suggested_fix}]
    """
    attempts = _load_json("shot_attempts.json")
    if not isinstance(attempts, list):
        return []

    failed = [a for a in attempts if a.get("final_outcome") in ("fail", "retry")]
    if not failed:
        return []

    clusters = []

    # Cluster by failure_type
    by_type = defaultdict(list)
    for a in failed:
        ft = a.get("failure_type", "unknown")
        by_type[ft].append(a)

    for ft, records in by_type.items():
        if len(records) >= min_occurrences:
            shots = list(set(r.get("shot_id", "?") for r in records))
            strategies = Counter(r.get("chosen_strategy") for r in records)
            most_common_strat = strategies.most_common(1)[0][0] if strategies else "unknown"

            clusters.append({
                "type": ft,
                "count": len(records),
                "pattern": f"{ft} occurs with strategy={most_common_strat}",
                "affected_shots": shots[:10],
                "suggested_fix": _suggest_fix_for_cluster(ft, records),
            })

    # Cluster by strategy failure rate
    strat_attempts = defaultdict(lambda: {"total": 0, "failed": 0})
    for a in attempts:
        if not isinstance(a, dict):
            continue
        s = a.get("chosen_strategy", "unknown")
        strat_attempts[s]["total"] += 1
        if a.get("final_outcome") in ("fail", "retry"):
            strat_attempts[s]["failed"] += 1

    for strat, counts in strat_attempts.items():
        if counts["total"] >= 3:
            fail_rate = counts["failed"] / counts["total"]
            if fail_rate > 0.5:
                clusters.append({
                    "type": "strategy_failure_rate",
                    "count": counts["failed"],
                    "pattern": f"Strategy '{strat}' fails {fail_rate*100:.0f}% of the time",
                    "affected_shots": [],
                    "suggested_fix": f"Consider deprioritizing '{strat}' in fallback chain",
                })

    return sorted(clusters, key=lambda c: -c["count"])


def _suggest_fix_for_cluster(failure_type: str, records: list) -> str:
    """Generate a fix suggestion for a failure cluster."""
    fixes = {
        "drift": "Add stronger continuity lock text. Increase identity weight in judge.",
        "morphing": "Reduce motion delta. Use shorter durations or bridge frames.",
        "bad_motion": "Simplify video prompt. Reduce action count per clip.",
        "lighting_issue": "Lock lighting direction in prompt. Add explicit light instructions.",
        "harsh_cut": "Switch to motivated cut with appropriate cut type.",
        "composition_issue": "Tighten framing constraints. Use end variants.",
        "multi_issue": "Escalate to Sonnet for root cause. Consider regenerate_pair.",
    }
    return fixes.get(failure_type, "Investigate manually — uncommon failure pattern.")


# ---------------------------------------------------------------------------
# Threshold Management
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    "direct_animate_min": 0.85,
    "end_variants_min": 0.65,
    "bridge_frame_min": 0.45,
    "motivated_cut_min": 0.45,
    "regenerate_pair_below": 0.45,
    "identity_fail_fast": 0.3,
    "lighting_fail_fast": 0.3,
    "scene_fail_fast": 0.25,
    "post_render_pass": 0.75,
    "post_render_retry": 0.5,
}


def get_thresholds() -> dict:
    """Get current scoring thresholds (user-customized or defaults)."""
    custom = _load_json("thresholds.json")
    if isinstance(custom, dict) and custom:
        merged = {**DEFAULT_THRESHOLDS, **custom}
        return merged
    return dict(DEFAULT_THRESHOLDS)


def update_threshold(name: str, value: float, reason: str = ""):
    """Update a single threshold value."""
    thresholds = _load_json("thresholds.json")
    if not isinstance(thresholds, dict):
        thresholds = {}
    thresholds[name] = value
    _save_json("thresholds.json", thresholds)

    # Log the change
    history = _load_json("threshold_changes.json")
    if not isinstance(history, list):
        history = []
    history.append({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "threshold": name,
        "new_value": value,
        "reason": reason,
    })
    _save_json("threshold_changes.json", history)


# ---------------------------------------------------------------------------
# Prompt Rule Management
# ---------------------------------------------------------------------------

def get_prompt_rules() -> list:
    """Get active prompt rule adjustments."""
    rules = _load_json("prompt_rules.json")
    return rules if isinstance(rules, list) else []


def add_prompt_rule(rule: str, evidence: str, confidence: float = 0.5,
                    risk: str = "low", auto_apply: bool = False) -> dict:
    """Add a new prompt rule based on learning evidence."""
    rules = get_prompt_rules()

    new_rule = {
        "id": f"rule_{len(rules)+1:04d}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rule": rule,
        "evidence": evidence,
        "confidence": confidence,
        "risk": risk,
        "auto_apply": auto_apply and risk == "low",  # only auto-apply low-risk
        "status": "active" if (auto_apply and risk == "low") else "pending",
    }

    rules.append(new_rule)
    _save_json("prompt_rules.json", rules)
    return new_rule


# ---------------------------------------------------------------------------
# Optimizer (Haiku default, Sonnet escalation)
# ---------------------------------------------------------------------------

def run_optimizer(force_sonnet: bool = False) -> dict:
    """Run the self-healing optimizer on accumulated shot data.

    Analyzes failure patterns and suggests improvements.
    Uses Haiku by default, Sonnet for escalation.

    Returns optimizer recommendations.
    """
    from lib.claude_client import call_json, OPUS_MODEL
    from lib.prompt_packs import haiku_optimizer

    attempts = _load_json("shot_attempts.json")
    if not isinstance(attempts, list) or len(attempts) < 3:
        return {"summary": "Not enough data for optimization (need 3+ attempts)",
                "recommendations": []}

    clusters = cluster_failures()
    thresholds = get_thresholds()
    rules = get_prompt_rules()

    # Build optimizer prompt
    prompt = haiku_optimizer.render(
        shot_records=attempts[-50:],  # last 50 for context window
        failure_clusters=clusters,
        current_thresholds=thresholds,
        current_prompt_rules=rules,
    )

    model = OPUS_MODEL  # Opus everywhere (2026-04-19 — feedback_claude_model_upgrade)
    result = call_json(prompt, system=haiku_optimizer.SYSTEM, model=model)

    if result.get("_parse_error"):
        return {"summary": "Optimizer failed to produce valid JSON",
                "recommendations": [], "_raw": result.get("_raw", "")}

    # Auto-apply low-risk prompt rules
    for rule_update in result.get("prompt_rule_updates", []):
        if rule_update.get("risk") == "low" and rule_update.get("confidence", 0) >= 0.7:
            add_prompt_rule(
                rule=rule_update["rule"],
                evidence=rule_update.get("evidence", "optimizer"),
                confidence=rule_update["confidence"],
                risk="low",
                auto_apply=True,
            )

    # Log optimizer run
    history = _load_json("optimizer_history.json")
    if not isinstance(history, list):
        history = []
    history.append({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "input_records": len(attempts),
        "clusters_found": len(clusters),
        "result": result,
    })
    _save_json("optimizer_history.json", history)

    return result


def run_deep_optimizer() -> dict:
    """Run Sonnet-level deep analysis.

    Used when: repeated system failures, project-wide issues,
    or major strategy rethink needed.
    """
    from lib.claude_client import call_json, OPUS_MODEL
    from lib.prompt_packs import sonnet_optimizer_escalation

    attempts = _load_json("shot_attempts.json")
    clusters = cluster_failures()

    # Get Haiku's recommendations first
    haiku_result = run_optimizer(force_sonnet=False)

    prompt = sonnet_optimizer_escalation.render(
        shot_records=attempts[-100:],
        failure_clusters=clusters,
        haiku_recommendations=haiku_result,
        escalation_reason="Deep analysis requested",
    )

    result = call_json(prompt, system=sonnet_optimizer_escalation.SYSTEM,
                       model=OPUS_MODEL)

    # Log
    history = _load_json("optimizer_history.json")
    if not isinstance(history, list):
        history = []
    history.append({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": OPUS_MODEL,
        "type": "deep_analysis",
        "result": result,
    })
    _save_json("optimizer_history.json", history)

    return result


# ---------------------------------------------------------------------------
# Convenience: print summary
# ---------------------------------------------------------------------------

def print_learning_summary():
    """Print a summary of the learning system state."""
    print("\n" + "=" * 70)
    print("SELF-HEALING LEARNING SYSTEM — Summary")
    print("=" * 70)

    stats = get_project_stats()
    print(f"  Total attempts:  {stats['total']}")
    print(f"  Pass rate:       {stats['pass_rate']}%")
    print(f"  Strategies used: {stats['strategies']}")
    if stats['failure_types']:
        print(f"  Failure types:   {stats['failure_types']}")

    clusters = cluster_failures()
    if clusters:
        print(f"\n  Failure clusters: {len(clusters)}")
        for c in clusters[:5]:
            print(f"    [{c['count']}x] {c['type']}: {c['pattern']}")
            print(f"         Fix: {c['suggested_fix']}")

    rules = get_prompt_rules()
    active = [r for r in rules if r.get("status") == "active"]
    pending = [r for r in rules if r.get("status") == "pending"]
    if rules:
        print(f"\n  Prompt rules: {len(active)} active, {len(pending)} pending")
        for r in active[:3]:
            print(f"    [ACTIVE] {r['rule']}")

    thresholds = get_thresholds()
    custom_count = len(_load_json("thresholds.json"))
    if isinstance(custom_count, dict):
        custom_count = len(custom_count)
    print(f"\n  Thresholds: {len(thresholds)} total"
          f" ({custom_count} customized)" if isinstance(custom_count, int) else "")
