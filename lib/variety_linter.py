"""
Variety linter — scans every shot in scenes.json and flags repetition across
the four populator fields (subjectAction, lighting, cameraMovement, envMotion).

Why: even after an Opus director pass, a project can drift into copy-paste
sameness — "gazes" repeated 8×, "signal core piercing clouds" verbatim in 3
scenes, every shot opening with "slow push-in". A real MV breathes. This
detects that without paying for another LLM call.

Returns a report per-field with:
  - overused_phrases: {phrase: [shot_ids]} for phrases appearing ≥ threshold
  - exact_duplicates: {phrase: [shot_ids]} for verbatim repeats across different scenes
  - diversity_score:  0.0 (all identical) → 1.0 (every shot distinct)

The server surfaces this to the UI via /api/v6/director/variety-check. The UI
shows a ranked list — user clicks a flagged shot to run the Direct button and
rewrite it.
"""
from __future__ import annotations

import os
import json
import re
from collections import defaultdict
from typing import Any


# Words that don't count toward dedup — stop words + common camera/lighting
# filler that naturally recurs. We only flag meaningful repetition.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "from",
    "of", "for", "with", "as", "is", "are", "was", "were", "be", "been",
    "being", "has", "have", "had", "do", "does", "did", "will", "would",
    "shall", "should", "may", "might", "can", "could", "his", "her", "its",
    "their", "this", "that", "these", "those", "he", "she", "it", "they",
    "into", "onto", "through", "over", "under", "above", "below",
}


def _normalize(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s\-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _tokens(s: str) -> list[str]:
    return [t for t in _normalize(s).split() if t and t not in _STOPWORDS]


def _first_phrase(s: str, n: int = 4) -> str:
    """First N meaningful tokens — used for fuzzy dedup on opening phrases."""
    toks = _tokens(s)
    return " ".join(toks[:n])


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def analyze_field(values: list[tuple[str, str]],
                  threshold_overuse: int = 3) -> dict:
    """Analyze one field across all shots.

    Args:
        values: list of (shot_id, field_value) tuples
        threshold_overuse: tokens appearing this many times or more get flagged

    Returns dict with overused_tokens, duplicate_phrases, similar_pairs,
    diversity_score, unique_ratio, total.
    """
    shot_ids_by_token = defaultdict(list)
    shot_ids_by_first_phrase = defaultdict(list)
    shot_ids_by_full = defaultdict(list)
    token_sets = {}

    n = 0
    for shot_id, val in values:
        if not val or not val.strip():
            continue
        n += 1
        toks = set(_tokens(val))
        token_sets[shot_id] = toks
        for t in toks:
            shot_ids_by_token[t].append(shot_id)
        fp = _first_phrase(val, 4)
        if fp:
            shot_ids_by_first_phrase[fp].append(shot_id)
        norm_full = _normalize(val)
        if norm_full:
            shot_ids_by_full[norm_full].append(shot_id)

    overused_tokens = {
        tok: sorted(ids) for tok, ids in shot_ids_by_token.items()
        if len(ids) >= threshold_overuse
    }
    duplicate_phrases = {
        phr: sorted(ids) for phr, ids in shot_ids_by_full.items()
        if len(ids) > 1
    }
    opening_repeats = {
        phr: sorted(ids) for phr, ids in shot_ids_by_first_phrase.items()
        if len(ids) >= threshold_overuse
    }

    similar_pairs = []
    seen_pairs = set()
    ids = list(token_sets.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if (a, b) in seen_pairs or (b, a) in seen_pairs:
                continue
            score = _jaccard(token_sets[a], token_sets[b])
            if score >= 0.6 and score < 1.0:
                similar_pairs.append({"a": a, "b": b, "jaccard": round(score, 2)})
                seen_pairs.add((a, b))
    similar_pairs.sort(key=lambda p: -p["jaccard"])

    unique_full = len(set(shot_ids_by_full.keys()))
    unique_ratio = unique_full / n if n else 1.0

    # Diversity weights meaningful repetition only — single-token overuse is
    # noise for a scene list ("rooftop" across scene-1 shots is continuity,
    # not a flaw). Duplicate PHRASES and similar_pairs are the real signal.
    flags = (
        2 * len(duplicate_phrases)
        + len(opening_repeats)
        + len(similar_pairs)
    )
    diversity_score = max(0.0, 1.0 - flags / max(n, 1))

    return {
        "total": n,
        "unique_ratio": round(unique_ratio, 2),
        "diversity_score": round(diversity_score, 2),
        "overused_tokens": overused_tokens,
        "duplicate_phrases": duplicate_phrases,
        "opening_repeats": opening_repeats,
        "similar_pairs": similar_pairs[:20],
    }


def analyze_scenes(scenes: list[dict],
                   threshold_overuse: int = 3) -> dict:
    """Run variety analysis across all shots for the four populator fields.

    Returns {by_field, flagged_shots, summary}. flagged_shots is a dedup'd
    shot_id list ranked by how many fields they appear in — the UI uses this
    to highlight which shots are the biggest offenders.
    """
    by_field = {}
    # Scan every narrative field that drives prompt authoring. Scenes.json holds
    # the canonical ones (shotDescription / cameraMovement / emotion /
    # narrativeIntent); the director pass layers subjectAction / lighting /
    # envMotion on top. Empty fields are skipped — total counts real writes.
    field_map = {
        "shotDescription": "Shot description",
        "subjectAction": "Subject action",
        "lighting": "Lighting",
        "cameraMovement": "Camera / motion",
        "envMotion": "Env motion",
        "emotion": "Emotion",
        "narrativeIntent": "Narrative intent",
    }
    flagged_count = defaultdict(int)

    for field, label in field_map.items():
        values = [
            (s.get("id"), s.get(field, ""))
            for s in scenes
            if s.get("id")
        ]
        report = analyze_field(values, threshold_overuse=threshold_overuse)
        report["label"] = label
        by_field[field] = report
        if report["total"] == 0:
            continue

        for ids in report["duplicate_phrases"].values():
            for sid in ids:
                flagged_count[sid] += 2  # exact dup is worst
        for ids in report["opening_repeats"].values():
            for sid in ids:
                flagged_count[sid] += 1
        for pair in report["similar_pairs"]:
            flagged_count[pair["a"]] += 1
            flagged_count[pair["b"]] += 1

    flagged_shots = sorted(
        [{"shot_id": sid, "score": score} for sid, score in flagged_count.items()],
        key=lambda x: -x["score"],
    )

    populated_fields = [r for r in by_field.values() if r["total"] > 0]
    diversity_avg = round(
        sum(r["diversity_score"] for r in populated_fields) / max(len(populated_fields), 1),
        2,
    ) if populated_fields else 1.0

    summary = {
        "shots_total": len(scenes),
        "shots_flagged": len(flagged_shots),
        "diversity_score": diversity_avg,
        "verdict": "SHIP" if diversity_avg >= 0.75 and not flagged_shots
                   else "REVIEW" if diversity_avg >= 0.5
                   else "REVISE",
    }

    return {
        "summary": summary,
        "by_field": by_field,
        "flagged_shots": flagged_shots,
    }


def analyze_project(project: str = "default",
                    threshold_overuse: int = 3) -> dict:
    """Convenience: load project scenes.json and run analyze_scenes."""
    from lib.active_project import get_project_root
    path = os.path.join(get_project_root(project), "prompt_os", "scenes.json")
    if not os.path.isfile(path):
        return {
            "summary": {"shots_total": 0, "shots_flagged": 0,
                        "diversity_score": 1.0, "verdict": "SHIP"},
            "by_field": {},
            "flagged_shots": [],
            "error": f"scenes.json not found at {path}",
        }
    with open(path, "r", encoding="utf-8") as f:
        scenes = json.load(f)
    scenes = sorted(scenes, key=lambda s: s.get("orderIndex", 0))
    return analyze_scenes(scenes, threshold_overuse=threshold_overuse)


if __name__ == "__main__":
    import sys
    proj = sys.argv[1] if len(sys.argv) > 1 else "default"
    result = analyze_project(proj)
    print(json.dumps(result, indent=2, default=str))
