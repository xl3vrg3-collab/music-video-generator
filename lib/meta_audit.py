"""
Meta-audit + self-consistency — Opus reviewing Opus.

Two patterns:

  (1) self_consistency_vote(fn, *args, n=3)
      Run the same judgment function N times. Opus uses temperature≈0 but
      extended thinking introduces genuine path variance. Aggregate by
      majority verdict; return a disagreement score so callers can gate on
      confidence (e.g. unanimous → auto-accept; 2-vs-1 → human review).

  (2) meta_critique_verdict(original_verdict, case_payload)
      Opus plays devil's advocate against a prior verdict. Returns a
      critique object flagging the reasoning's weakest link and a suggested
      revised verdict if warranted.

Both are designed to sit on top of the existing Opus auditors (anchor_auditor,
identity_gate_opus.audit_anchor_full, opus_director.direct_critique) without
coupling to any single one.

Cost note: self-consistency at N=3 triples the Opus bill on whatever function
it wraps. Use it for hero shots, batch-failing anchors, final-mv critique —
not for every frame.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable, Optional

from lib.claude_client import call_opus_json


# ───────────────────────────────────────────────────────────────────────────
# 1. Self-consistency voter
# ───────────────────────────────────────────────────────────────────────────

def _verdict_field(result: Any, field_candidates: list[str]) -> Optional[str]:
    """Pull the verdict from whatever shape the wrapped function returns."""
    if not isinstance(result, dict):
        return None
    for f in field_candidates:
        v = result.get(f)
        if isinstance(v, str):
            return v.upper()
        if isinstance(v, dict):
            inner = _verdict_field(v, field_candidates)
            if inner:
                return inner
    return None


def self_consistency_vote(fn: Callable,
                          *args,
                          n: int = 3,
                          verdict_fields: Optional[list[str]] = None,
                          **kwargs) -> dict:
    """
    Run `fn(*args, **kwargs)` N times, aggregate the verdict.

    Returns:
    {
      "runs":            [per-run result, ...],
      "verdicts":        ["PASS", "SOFT_FAIL", ...],
      "majority":        "PASS" | "SOFT_FAIL" | "HARD_FAIL" | "UNKNOWN",
      "agreement_ratio": float,   // 0..1 — 1.0 means unanimous
      "confidence":      "HIGH" | "MEDIUM" | "LOW",
      "disagreement":    bool,    // True when not unanimous
    }

    `verdict_fields`: which keys to look for in the result dict. Defaults
    to common names used in LUMN auditors.
    """
    if verdict_fields is None:
        verdict_fields = ["final_verdict", "verdict"]

    runs = []
    verdicts = []
    for _ in range(n):
        try:
            r = fn(*args, **kwargs)
        except Exception as e:
            r = {"error": str(e)[:200]}
        runs.append(r)
        v = _verdict_field(r, verdict_fields) or "UNKNOWN"
        verdicts.append(v)

    counts = Counter(verdicts)
    majority, maj_count = counts.most_common(1)[0]
    ratio = maj_count / n
    disagreement = len(counts) > 1

    if ratio == 1.0:
        conf = "HIGH"
    elif ratio >= 0.66:
        conf = "MEDIUM"
    else:
        conf = "LOW"

    return {
        "runs": runs,
        "verdicts": verdicts,
        "majority": majority,
        "agreement_ratio": round(ratio, 3),
        "confidence": conf,
        "disagreement": disagreement,
    }


# ───────────────────────────────────────────────────────────────────────────
# 2. Devil's-advocate meta-critique
# ───────────────────────────────────────────────────────────────────────────

_META_SCHEMA_HINT = """\
Return strict JSON:
{
  "meta_verdict": "AFFIRM" | "REVISE" | "OVERTURN",
  "weakest_link": string,                 // the single weakest part of the original reasoning
  "overlooked_factors": [string],         // what the original audit didn't weigh
  "hallucination_risks": [string],        // places the original may have invented detail not in the evidence
  "revised_verdict": string,              // what the verdict SHOULD be if REVISE/OVERTURN, else mirror original
  "revised_highest_impact_fix": string,   // one-line new fix recommendation
  "confidence_calibration": "UNDERCONFIDENT" | "CALIBRATED" | "OVERCONFIDENT"
}"""


def meta_critique_verdict(original_verdict: dict,
                          case_payload: dict,
                          project: Optional[str] = None,
                          profile_id: Optional[str] = None,
                          thinking_budget: int = 4000) -> dict:
    """
    Ask Opus to devil's-advocate a prior verdict. Feed it the evidence the
    original auditor saw (case_payload) AND the verdict it produced
    (original_verdict). Opus must hunt weak reasoning, overlooked factors,
    and hallucinations.

    Use after self_consistency_vote returns LOW confidence, or for hero
    shots where a single false-pass is costly.
    """
    prompt = f"""\
META-CRITIQUE — a prior auditor (also you, a previous call) produced this verdict.
Your job is to review the reasoning, not re-audit the evidence from scratch.

ORIGINAL VERDICT (under review):
{json.dumps(original_verdict, indent=2, default=str)[:12000]}

CASE PAYLOAD (evidence the original auditor had):
{json.dumps(case_payload, indent=2, default=str)[:12000]}

Apply these tests:
  - Does the verdict's reasoning actually support its conclusion?
  - Are any claims made about the images that contradict what the evidence shows?
  - Did the auditor apply every bible rule (identity, style, emblem, proportions, acting)?
  - Did the auditor miss documented failure modes (banned-sole-verbs, T-pose triggers, mouth-open drift, second-character hallucination, style bleed)?
  - Is the confidence calibrated? (Confident PASS when evidence was borderline = overconfident.)

AFFIRM = the original verdict stands and its reasoning is sound.
REVISE = the verdict category may be right but the reasoning has gaps worth fixing.
OVERTURN = the verdict should be replaced (e.g. PASS → HARD_FAIL).

{_META_SCHEMA_HINT}

Return ONLY the JSON object."""
    return call_opus_json(
        prompt=prompt,
        project=project,
        profile_id=profile_id,
        max_tokens=4000,
        thinking_budget=thinking_budget,
    )


# ───────────────────────────────────────────────────────────────────────────
# 3. End-to-end meta audit wrapper
# ───────────────────────────────────────────────────────────────────────────

def meta_audit(fn: Callable,
               *args,
               case_payload: Optional[dict] = None,
               n_votes: int = 3,
               escalate_on: str = "LOW",
               project: Optional[str] = None,
               profile_id: Optional[str] = None,
               verdict_fields: Optional[list[str]] = None,
               **kwargs) -> dict:
    """
    Self-consistency vote → optional devil's-advocate meta-critique.

    Args:
        fn:             auditor function to run (e.g. audit_anchor_full)
        n_votes:        voting runs (default 3)
        escalate_on:    trigger meta-critique when vote confidence is this
                        level or lower. "LOW" | "MEDIUM" | "HIGH".
        case_payload:   evidence summary to show the meta-critic. If None,
                        uses the first vote's payload where possible.
    """
    vote = self_consistency_vote(fn, *args, n=n_votes,
                                 verdict_fields=verdict_fields, **kwargs)

    conf_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    should_escalate = conf_order.get(vote["confidence"], 0) <= conf_order.get(escalate_on, 1)

    meta = None
    if should_escalate:
        anchor_verdict = vote["runs"][0] if vote["runs"] else {}
        payload = case_payload or {
            "args_preview": [str(a)[:200] for a in args],
            "kwargs_preview": {k: str(v)[:200] for k, v in kwargs.items()
                               if k not in ("candidate_path", "sheet_path")},
            "voting_summary": {
                "verdicts": vote["verdicts"],
                "agreement_ratio": vote["agreement_ratio"],
            },
        }
        try:
            meta = meta_critique_verdict(
                original_verdict=anchor_verdict,
                case_payload=payload,
                project=project,
                profile_id=profile_id,
            )
        except Exception as e:
            meta = {"error": str(e)[:200]}

    return {
        "vote": vote,
        "meta_critique": meta,
        "final_verdict": (
            (meta or {}).get("revised_verdict") or vote["majority"]
        ),
        "escalated": should_escalate,
    }


if __name__ == "__main__":
    print("[meta_audit] entry points:")
    for fn in [self_consistency_vote, meta_critique_verdict, meta_audit]:
        print(f"  - {fn.__name__}")
