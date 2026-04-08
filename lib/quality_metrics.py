"""
Plan Quality Metrics — Score V4 plans on 6 cinematic quality dimensions.

Dimensions (weighted):
1. Shot Diversity      (30%) — variety of shot sizes and avoiding repetition
2. Timing Variance     (20%) — dynamic pacing via duration spread
3. Coverage Complete   (15%) — each beat has wide, medium, and close
4. Narrative Arc       (15%) — energy curve matches setup-build-climax-release
5. Continuity          (10%) — character binding and screen direction consistency
6. Music Sync          (10%) — shot cuts land on musical beats

No external dependencies beyond stdlib + math.
"""

import math


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_SHOT_SIZES = ("EWS", "WS", "MS", "MCU", "CU", "ECU", "OTS", "POV", "INSERT")

WIDE_SIZES = {"EWS", "WS"}
MEDIUM_SIZES = {"MS", "MCU", "OTS"}
CLOSE_SIZES = {"CU", "ECU", "INSERT"}
SPECIAL_SIZES = {"POV"}

CATEGORY_SETS = [WIDE_SIZES, MEDIUM_SIZES, CLOSE_SIZES, SPECIAL_SIZES]

GRADE_THRESHOLDS = [
    (95, "A+"),
    (85, "A"),
    (75, "B+"),
    (65, "B"),
    (50, "C"),
    (35, "D"),
]

IDEAL_ARC = [0.3, 0.5, 0.7, 0.9, 0.7, 0.4]

BEAT_PROXIMITY_WINDOW = 0.3  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(val, lo=0, hi=100):
    return max(lo, min(hi, val))


def _std_dev(values):
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def _interpolate_arc(n):
    """Interpolate the ideal arc to *n* points."""
    if n <= 1:
        return [0.6]
    src = IDEAL_ARC
    result = []
    for i in range(n):
        pos = i / (n - 1) * (len(src) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(src) - 1)
        frac = pos - lo
        result.append(src[lo] * (1 - frac) + src[hi] * frac)
    return result


def _pearson(xs, ys):
    """Pearson correlation coefficient between two equal-length sequences."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def _letter_grade(score):
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


def _flatten_shots(plan):
    """Return a flat list of shots from either beats or scenes."""
    shots = []
    for beat in plan.get("beats", []):
        shots.extend(beat.get("shots", []))
    if not shots:
        shots = list(plan.get("scenes", []))
    return shots


def _extract_beats(plan):
    """Return beats list. For V3 plans, wrap each scene as a single-shot beat."""
    beats = plan.get("beats", [])
    if beats:
        return beats
    # V3 fallback: each scene becomes a 1-shot beat
    scenes = plan.get("scenes", [])
    return [
        {
            "beat_id": s.get("shot_id", f"scene_{i}"),
            "energy": s.get("energy", 0.5),
            "shots": [s],
        }
        for i, s in enumerate(scenes)
    ]


# ---------------------------------------------------------------------------
# Dimension scorers
# ---------------------------------------------------------------------------

def _score_shot_diversity(shots):
    """30% weight — unique sizes, consecutive-repeat penalty, category bonus."""
    if not shots:
        return {"score": 0, "weight": 0.30, "details": "no shots"}

    sizes_used = {s.get("shot_size", "") for s in shots if s.get("shot_size")}
    coverage_ratio = len(sizes_used) / len(ALL_SHOT_SIZES)
    base = _clamp(coverage_ratio * 100)

    # Penalty: -20 for any run of 3+ consecutive identical shot_size
    penalty = 0
    for i in range(len(shots) - 2):
        a = shots[i].get("shot_size")
        b = shots[i + 1].get("shot_size")
        c = shots[i + 2].get("shot_size")
        if a and a == b == c:
            penalty = 20
            break

    # Bonus: +10 if all 4 categories represented
    categories_hit = sum(1 for cat in CATEGORY_SETS if sizes_used & cat)
    bonus = 10 if categories_hit == len(CATEGORY_SETS) else 0

    score = _clamp(base - penalty + bonus)

    parts = [f"{len(sizes_used)}/{len(ALL_SHOT_SIZES)} sizes used"]
    if penalty:
        parts.append("triple repeat penalty")
    else:
        parts.append("no triple repeats")
    if bonus:
        parts.append("all categories")

    return {"score": score, "weight": 0.30, "details": ", ".join(parts)}


def _score_timing_variance(shots):
    """20% weight — std deviation of target_durations."""
    durations = [s.get("target_duration", 0) for s in shots if s.get("target_duration")]
    if not durations:
        return {"score": 0, "weight": 0.20, "details": "no durations"}

    sd = _std_dev(durations)
    # sd * 50: std_dev of 2.0s = 100 (realistic for music video pacing)
    score = _clamp(min(100, sd * 50))
    return {"score": score, "weight": 0.20, "details": f"std_dev={sd:.1f}s"}


def _score_coverage_completeness(beats):
    """15% weight — each beat has wide + medium + close."""
    if not beats:
        return {"score": 0, "weight": 0.15, "details": "no beats"}

    covered = 0
    for beat in beats:
        sizes = {s.get("shot_size", "") for s in beat.get("shots", [])}
        has_wide = bool(sizes & WIDE_SIZES)
        has_medium = bool(sizes & MEDIUM_SIZES)
        has_close = bool(sizes & CLOSE_SIZES)
        if has_wide and has_medium and has_close:
            covered += 1

    total = len(beats)
    score = _clamp(int((covered / total) * 100)) if total else 0
    return {
        "score": score,
        "weight": 0.15,
        "details": f"{covered}/{total} beats fully covered",
    }


def _score_narrative_arc(beats):
    """15% weight — energy curve correlation with ideal arc."""
    energies = [b.get("energy", 0.5) for b in beats]
    if len(energies) < 2:
        return {"score": 50, "weight": 0.15, "details": "too few beats for arc analysis"}

    ideal = _interpolate_arc(len(energies))
    corr = _pearson(energies, ideal)
    # Map correlation (-1..1) to score (0..100). 1.0 -> 100, 0 -> 50, -1 -> 0
    score = _clamp(int((corr + 1) * 50))
    return {
        "score": score,
        "weight": 0.15,
        "details": f"correlation={corr:.2f} with ideal arc",
    }


def _score_continuity(shots):
    """10% weight — character binding + screen direction presence."""
    if not shots:
        return {"score": 0, "weight": 0.10, "details": "no shots"}

    total = len(shots)
    bound = sum(1 for s in shots if s.get("characterId"))
    directed = sum(1 for s in shots if s.get("screen_direction"))

    pct_bound = (bound / total) * 100
    pct_dir = (directed / total) * 100
    score = _clamp(int((pct_bound + pct_dir) / 2))
    return {
        "score": score,
        "weight": 0.10,
        "details": f"{bound}/{total} shots bound, {directed}/{total} with direction",
    }


def _score_music_sync(shots, plan):
    """10% weight — shot boundaries near audio beats."""
    audio = plan.get("audio_analysis", {})
    beat_times = audio.get("beats", [])

    if not beat_times:
        return {"score": 50, "weight": 0.10, "details": "no audio data, default 50"}

    # Build cumulative cut points from target_durations
    cuts = []
    cumulative = 0.0
    for s in shots:
        dur = s.get("target_duration", 0)
        if dur:
            cumulative += dur
            cuts.append(cumulative)

    if not cuts:
        return {"score": 50, "weight": 0.10, "details": "no durations for sync check"}

    # Check how many cuts are within BEAT_PROXIMITY_WINDOW of any beat
    synced = 0
    for cut in cuts:
        if any(abs(cut - bt) <= BEAT_PROXIMITY_WINDOW for bt in beat_times):
            synced += 1

    pct = int((synced / len(cuts)) * 100) if cuts else 0
    score = _clamp(pct)
    return {"score": score, "weight": 0.10, "details": f"{pct}% cuts on beat"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_plan_quality(plan: dict) -> dict:
    """Score a V4 (or V3) plan across 6 quality dimensions.

    Returns a dict with per-dimension scores/weights/details, plus
    ``total`` (weighted 0-100) and ``grade`` (A+ through F).
    """
    beats = _extract_beats(plan)
    shots = _flatten_shots(plan)

    diversity = _score_shot_diversity(shots)
    timing = _score_timing_variance(shots)
    coverage = _score_coverage_completeness(beats)
    arc = _score_narrative_arc(beats)
    continuity = _score_continuity(shots)
    sync = _score_music_sync(shots, plan)

    dimensions = {
        "shot_diversity": diversity,
        "timing_variance": timing,
        "coverage_completeness": coverage,
        "narrative_arc": arc,
        "continuity": continuity,
        "music_sync": sync,
    }

    total = sum(d["score"] * d["weight"] for d in dimensions.values())
    total = _clamp(round(total))

    return {
        **dimensions,
        "total": total,
        "grade": _letter_grade(total),
    }


def compare_v3_v4(v3_plan: dict, v4_plan: dict) -> dict:
    """Score both a V3 and V4 plan and return the delta.

    V3 plans have a flat ``scenes`` array with no beats.  Each scene is
    treated as a single-shot beat for scoring purposes.
    """
    v3_score = score_plan_quality(v3_plan)
    v4_score = score_plan_quality(v4_plan)

    dimension_keys = [
        "shot_diversity", "timing_variance", "coverage_completeness",
        "narrative_arc", "continuity", "music_sync",
    ]

    improvement = {"total": v4_score["total"] - v3_score["total"]}
    for key in dimension_keys:
        improvement[key] = v4_score[key]["score"] - v3_score[key]["score"]

    summary = (
        f"V4 scores {v4_score['total']} vs V3 at {v3_score['total']} "
        f"({'+' if improvement['total'] >= 0 else ''}{improvement['total']} improvement)"
    )

    return {
        "v3_score": v3_score,
        "v4_score": v4_score,
        "improvement": improvement,
        "summary": summary,
    }
