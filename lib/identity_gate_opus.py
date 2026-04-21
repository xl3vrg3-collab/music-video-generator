"""
Opus identity gate — multi-image comparison against the character sheet +
prior anchors. Returns strict JSON verdict with specific drift callouts.

Two-stage pipeline:

    1. perceptual_gate.compare_multi  → cheap pHash/dHash/color composite
       - PASS        → skip Opus, save ~$0.20 and 4s per anchor
       - FAIL        → skip Opus (hard fail), regenerate
       - ESCALATE    → run Opus

    2. opus_multi_image_audit         → Opus vision with the bible
       Opus sees: candidate + sheet + up to 3 prior anchors. Returns
       pass/soft/hard fail with specific drift reasons.

The gate is idempotent and safe to re-run.
"""
from __future__ import annotations

import json
import pathlib
from typing import Optional

from lib.claude_client import call_opus_vision_json
from lib.perceptual_gate import compare_multi, PASS_THRESHOLD, FAIL_THRESHOLD


_AUDIT_SCHEMA_HINT = """\
Return strict JSON:
{
  "verdict": "PASS" | "SOFT_FAIL" | "HARD_FAIL",
  "composite_confidence": float,              // 0..1 — Opus's own confidence
  "identity_match": bool,                     // same character as sheet?
  "style_match": bool,                        // cel-shaded / painterly / etc. matches profile?
  "proportions_ok": bool,                     // head-to-body ratio matches sheet?
  "emblem_ok": bool,                          // signature marks correctly placed?
  "costume_ok": bool,                         // wardrobe matches sheet?
  "issues": [
    {
      "category": string,                     // "identity" | "style" | "proportions" | "emblem" | "costume" | "pose" | "drift_vs_prior"
      "severity": "HARD" | "SOFT",
      "observation": string,                  // what you see
      "ref_comparison": string,               // what the sheet shows vs candidate
      "fix": string                           // how the renderer should retry
    }
  ],
  "single_highest_impact_fix": string         // one-line summary for the renderer
}"""


def opus_multi_image_audit(candidate_path: str | pathlib.Path,
                           sheet_path: Optional[str | pathlib.Path] = None,
                           prior_anchor_paths: Optional[list] = None,
                           scene_context: Optional[dict] = None,
                           project: Optional[str] = None,
                           profile_id: Optional[str] = None,
                           thinking_budget: int = 3000) -> dict:
    """
    Run Opus vision over candidate + sheet + prior anchors.

    Args:
        candidate_path:     the anchor under review
        sheet_path:         canonical character sheet (source of identity)
        prior_anchor_paths: up to 3 already-approved anchors from the same
                            production (for drift detection)
        scene_context:      optional dict with expected emotion/acting/shot_size
        project:            project slug → loads director bible + style profile
        profile_id:         explicit profile override
        thinking_budget:    Opus extended-thinking budget (tokens)
    """
    images: list[str] = []

    if sheet_path and pathlib.Path(sheet_path).exists():
        images.append(str(sheet_path))
    for p in (prior_anchor_paths or [])[:3]:
        if pathlib.Path(p).exists():
            images.append(str(p))
    if not pathlib.Path(candidate_path).exists():
        return {"error": "candidate_missing", "path": str(candidate_path)}
    images.append(str(candidate_path))

    # Caller-visible index for Opus
    role_lines = []
    idx = 0
    if sheet_path and pathlib.Path(sheet_path).exists():
        role_lines.append(f"IMAGE {idx}: character sheet (canonical identity source)")
        idx += 1
    for p in (prior_anchor_paths or [])[:3]:
        if pathlib.Path(p).exists():
            role_lines.append(f"IMAGE {idx}: prior approved anchor — {pathlib.Path(p).name}")
            idx += 1
    role_lines.append(f"IMAGE {idx}: CANDIDATE under review — {pathlib.Path(candidate_path).name}")

    context_block = ""
    if scene_context:
        context_block = "\nSCENE CONTEXT:\n" + json.dumps(scene_context, indent=2)

    prompt = f"""\
IDENTITY AUDIT — multi-image comparison.

{chr(10).join(role_lines)}
{context_block}

Task: decide whether the CANDIDATE holds the protagonist's identity, style,
proportions, emblem/costume, compared to the character sheet and prior
anchors. Apply every bible rule.

Be especially ruthless about:
  - Identity drift (same species/color/markings as sheet?)
  - Style drift (cel-shaded vs painterly vs 3D — profile must win)
  - Proportions (toddler vs adult frame, head-to-body ratio)
  - Emblem placement (only where forehead is visible, never floating)
  - Costume consistency (hoodie color, hood state, accessories)
  - Drift vs. prior anchors (is the character recognisably the same across shots?)

HARD_FAIL = regenerate. Identity broken, style wrong, emblem violation, T-pose.
SOFT_FAIL = fix with minor re-prompt. Acting weak, minor coverage issue.
PASS = proceed.

{_AUDIT_SCHEMA_HINT}

Return ONLY the JSON object."""

    return call_opus_vision_json(
        prompt=prompt,
        image_paths=images,
        project=project,
        profile_id=profile_id,
        max_tokens=4000,
        thinking_budget=thinking_budget,
    )


def audit_anchor_full(candidate_path: str | pathlib.Path,
                      sheet_path: Optional[str | pathlib.Path] = None,
                      prior_anchor_paths: Optional[list] = None,
                      scene_context: Optional[dict] = None,
                      project: Optional[str] = None,
                      profile_id: Optional[str] = None,
                      force_opus: bool = False,
                      thinking_budget: int = 3000) -> dict:
    """
    Full two-stage audit: perceptual pre-gate, Opus vision if needed.

    Returns:
    {
      "perceptual": {...},        // perceptual_gate result
      "opus":       {...} | None, // multi-image audit result (if escalated)
      "final_verdict": "PASS" | "SOFT_FAIL" | "HARD_FAIL",
      "decision_path": "perceptual_pass" | "perceptual_fail" | "opus_*"
    }
    """
    refs = []
    if sheet_path:
        refs.append(str(sheet_path))
    if prior_anchor_paths:
        refs.extend(str(p) for p in prior_anchor_paths)

    perceptual = compare_multi(candidate_path, refs) if refs else {"skipped": True, "reason": "no refs"}

    # Shortcut only when perceptual is confident AND we're not forcing Opus
    if not force_opus and not perceptual.get("skipped"):
        if perceptual["verdict"] == "PASS":
            return {
                "perceptual": perceptual,
                "opus": None,
                "final_verdict": "PASS",
                "decision_path": "perceptual_pass",
            }
        if perceptual["verdict"] == "FAIL":
            return {
                "perceptual": perceptual,
                "opus": None,
                "final_verdict": "HARD_FAIL",
                "decision_path": "perceptual_fail",
            }

    opus = opus_multi_image_audit(
        candidate_path=candidate_path,
        sheet_path=sheet_path,
        prior_anchor_paths=prior_anchor_paths,
        scene_context=scene_context,
        project=project,
        profile_id=profile_id,
        thinking_budget=thinking_budget,
    )
    verdict = opus.get("verdict", "SOFT_FAIL") if isinstance(opus, dict) else "SOFT_FAIL"
    return {
        "perceptual": perceptual,
        "opus": opus,
        "final_verdict": verdict,
        "decision_path": f"opus_{verdict.lower()}",
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python identity_gate_opus.py <candidate.png> <sheet.png> [<prior.png> ...]")
        sys.exit(1)
    cand = sys.argv[1]
    sheet = sys.argv[2]
    priors = sys.argv[3:]
    result = audit_anchor_full(cand, sheet, priors, project="default")
    print(json.dumps(result, indent=2, default=str)[:3000])
