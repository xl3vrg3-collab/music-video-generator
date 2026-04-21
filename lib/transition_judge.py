"""
Transition Judge — 5+1 dimension continuity scorer for shot pairs.

Two scoring modes:
  1. Heuristic (fast, free) — structural analysis from metadata
  2. Vision (Haiku/Sonnet) — actual image analysis via Claude API

Dimensions scored:
  1. Identity continuity  — Does the subject look consistent?
  2. Pose continuity      — Is body position/angle compatible across the cut?
  3. Camera continuity    — Are framing, height, lens compatible?
  4. Scene continuity     — Same environment, lighting, time of day?
  5. Motion plausibility  — Does implied motion flow logically?
  6. Overall              — Weighted composite

Scores: 0.0 (broken) to 1.0 (seamless).
Composite score drives strategy selection in transition_strategy.py.
"""

import os

# ---------------------------------------------------------------------------
# Dimension weights — how much each factor matters for the composite
# ---------------------------------------------------------------------------

DIMENSION_WEIGHTS = {
    "identity": 3.0,   # most critical — wrong subject = total failure
    "pose":     2.0,   # pose mismatch = jarring but recoverable
    "camera":   1.5,   # framing jump = disorienting
    "scene":    1.5,   # environment mismatch = location confusion
    "motion":   2.0,   # motion discontinuity = physics violation
}

# ---------------------------------------------------------------------------
# Shot size categories and distances
# ---------------------------------------------------------------------------

_SIZE_ORDER = ["extreme_wide", "wide", "establishing", "medium_wide",
               "medium", "medium_close", "close", "tight", "extreme_close"]

_SIZE_ALIASES = {
    "wide establishing": "wide",
    "wide shot": "wide",
    "medium shot": "medium",
    "medium close-up": "medium_close",
    "close-up": "close",
    "close up": "close",
    "tight close": "tight",
    "extreme close-up": "extreme_close",
    "full shot": "medium_wide",
}


def _normalize_size(framing: str) -> str:
    """Map framing description to canonical size category."""
    f = framing.lower().strip()
    for alias, canonical in _SIZE_ALIASES.items():
        if alias in f:
            return canonical
    for cat in _SIZE_ORDER:
        if cat.replace("_", " ") in f or cat in f:
            return cat
    if "wide" in f:
        return "wide"
    if "close" in f:
        return "close"
    if "medium" in f:
        return "medium"
    return "medium"


def _size_distance(size_a: str, size_b: str) -> int:
    """Number of steps between two shot sizes (0 = same, higher = bigger jump)."""
    try:
        ia = _SIZE_ORDER.index(size_a)
        ib = _SIZE_ORDER.index(size_b)
        return abs(ia - ib)
    except ValueError:
        return 2  # unknown → moderate distance


# ---------------------------------------------------------------------------
# Height categories
# ---------------------------------------------------------------------------

_HEIGHT_ORDER = ["ground", "low", "eye_level", "slightly_high", "high", "overhead"]


def _height_distance(h_a: str, h_b: str) -> int:
    ha = (h_a or "eye_level").lower().replace(" ", "_")
    hb = (h_b or "eye_level").lower().replace(" ", "_")
    # Normalize common variants
    for h in [ha, hb]:
        if "ground" in h:
            h = "ground"
        elif "low" in h:
            h = "low"
        elif "overhead" in h or "bird" in h:
            h = "overhead"
        elif "high" in h and "slightly" not in h:
            h = "high"
        elif "slightly" in h:
            h = "slightly_high"
    try:
        return abs(_HEIGHT_ORDER.index(ha) - _HEIGHT_ORDER.index(hb))
    except ValueError:
        return 1


# ---------------------------------------------------------------------------
# Individual Dimension Scorers
# ---------------------------------------------------------------------------

def score_identity(shot_a: dict, shot_b: dict, beat_a: dict, beat_b: dict) -> dict:
    """Score identity continuity between two shots.

    Factors:
    - Same character set?
    - Both shots reference the same canonical sheets?
    - Character count change?
    - Identity gate frame used as ref in both?
    """
    chars_a = set(beat_a.get("characters", []))
    chars_b = set(beat_b.get("characters", []))

    if not chars_a and not chars_b:
        return {"score": 10, "reason": "No characters in either shot"}

    # Character overlap
    if chars_a == chars_b:
        char_score = 10
        char_reason = "Same character set"
    elif chars_a & chars_b:
        overlap = len(chars_a & chars_b) / max(len(chars_a | chars_b), 1)
        char_score = int(5 + overlap * 5)
        char_reason = f"Partial overlap ({len(chars_a & chars_b)}/{len(chars_a | chars_b)})"
    else:
        char_score = 2
        char_reason = "Entirely different characters"

    # Shared anchor refs boost identity score
    refs_a = set(shot_a.get("anchor_refs", []))
    refs_b = set(shot_b.get("anchor_refs", []))
    shared_refs = refs_a & refs_b
    ref_bonus = min(2, len(shared_refs))  # up to +2 for shared refs

    # Character count change penalty
    count_delta = abs(len(chars_a) - len(chars_b))
    count_penalty = min(2, count_delta)

    score = max(0, min(10, char_score + ref_bonus - count_penalty))
    reasons = [char_reason]
    if ref_bonus:
        reasons.append(f"+{ref_bonus} shared refs")
    if count_penalty:
        reasons.append(f"-{count_penalty} count change")

    return {"score": score, "reason": "; ".join(reasons)}


def score_pose(shot_a: dict, shot_b: dict, beat_a: dict, beat_b: dict) -> dict:
    """Score pose continuity between two shots.

    Factors:
    - Action compatibility (exit action of A → entry action of B)
    - Framing jump (wide→close = pose irrelevant, close→close = pose critical)
    - Explicit pose markers in the shot data
    """
    action_a = shot_a.get("action", "").lower()
    action_b = shot_b.get("action", "").lower()

    size_a = _normalize_size(shot_a.get("framing", ""))
    size_b = _normalize_size(shot_b.get("framing", ""))
    size_jump = _size_distance(size_a, size_b)

    # If both close/tight, pose continuity is critical
    both_close = size_a in ("close", "tight", "extreme_close") and \
                 size_b in ("close", "tight", "extreme_close")

    # If big framing jump, pose matters less (audience expects repositioning)
    if size_jump >= 3:
        return {"score": 8, "reason": f"Large framing jump ({size_a}→{size_b}), pose less critical"}

    # Action flow analysis
    # Moving actions that imply continuation
    motion_words = {"walks", "walking", "runs", "running", "sprint", "sprinting",
                    "turns", "turning", "moves", "moving", "approaches", "arriving"}
    # Static actions
    static_words = {"sits", "sitting", "stands", "standing", "holds", "still",
                    "stares", "staring", "frozen", "freeze", "paused"}

    words_a = set(action_a.split())
    words_b = set(action_b.split())

    a_moving = bool(words_a & motion_words)
    b_moving = bool(words_b & motion_words)
    a_static = bool(words_a & static_words)
    b_static = bool(words_b & static_words)

    if a_moving and b_moving:
        # Both moving — good flow if direction compatible
        score = 8
        reason = "Both shots have movement — flow likely"
    elif a_static and b_static:
        # Both static — pose match critical for close, ok for wide
        score = 7 if both_close else 9
        reason = "Both static" + (" — close-up needs pose match" if both_close else "")
    elif a_moving and b_static:
        # Motion → stillness — arrival moment, generally ok
        score = 7
        reason = "Motion→stillness transition (arrival)"
    elif a_static and b_moving:
        # Stillness → motion — departure moment
        score = 7
        reason = "Stillness→motion transition (departure)"
    else:
        score = 6
        reason = "Ambiguous action continuity"

    return {"score": max(0, min(10, score)), "reason": reason}


def score_camera(shot_a: dict, shot_b: dict, beat_a: dict, beat_b: dict) -> dict:
    """Score camera continuity between two shots.

    Factors:
    - Shot size jump distance
    - Camera height change
    - 180-degree rule compliance
    - Lens focal length change
    """
    size_a = _normalize_size(shot_a.get("framing", ""))
    size_b = _normalize_size(shot_b.get("framing", ""))
    size_jump = _size_distance(size_a, size_b)

    height_a = shot_a.get("camera_height", "eye_level")
    height_b = shot_b.get("camera_height", "eye_level")
    h_jump = _height_distance(height_a, height_b)

    # Size jump scoring: 0=same→10, 1→8, 2→6, 3→4, 4+→2
    size_score = max(2, 10 - size_jump * 2)

    # Height scoring: 0=same→10, 1→8, 2→5, 3+→3
    height_score = max(3, 10 - h_jump * 2.5)

    # 30-degree rule: consecutive shots should differ by at least 30 degrees
    # OR differ enough in size. Same size + same height = potential jump cut
    jump_cut_risk = (size_jump == 0 and h_jump == 0)
    jump_penalty = 3 if jump_cut_risk else 0

    composite = (size_score * 0.5 + height_score * 0.3 + (10 - jump_penalty) * 0.2)
    score = max(0, min(10, int(composite)))

    reasons = []
    reasons.append(f"size: {size_a}→{size_b} (jump={size_jump})")
    if h_jump > 0:
        reasons.append(f"height: {height_a}→{height_b}")
    if jump_cut_risk:
        reasons.append("JUMP CUT RISK: same size+height")

    return {"score": score, "reason": "; ".join(reasons)}


def score_scene(shot_a: dict, shot_b: dict, beat_a: dict, beat_b: dict) -> dict:
    """Score scene/environment continuity between two shots.

    Factors:
    - Same location?
    - Location description similarity
    - Lighting consistency
    - Same beat (intra-beat = always same scene)
    """
    # Intra-beat transitions are always same scene
    if beat_a.get("beat_id") == beat_b.get("beat_id"):
        return {"score": 10, "reason": "Same beat — same scene guaranteed"}

    loc_a = beat_a.get("location_pkg", "")
    loc_b = beat_b.get("location_pkg", "")

    if loc_a == loc_b and loc_a:
        # Same location package
        desc_a = beat_a.get("location_desc", "")
        desc_b = beat_b.get("location_desc", "")
        if desc_a == desc_b:
            return {"score": 10, "reason": "Same location, same description"}
        # Same location but different area description
        return {"score": 7, "reason": f"Same location, different area"}

    if not loc_a or not loc_b:
        return {"score": 5, "reason": "Missing location data"}

    # Different location
    return {"score": 2, "reason": f"Different locations"}


def score_motion(shot_a: dict, shot_b: dict, beat_a: dict, beat_b: dict) -> dict:
    """Score motion continuity between two shots.

    Factors:
    - Exit direction of A matches entry direction of B
    - Speed/energy compatibility
    - Camera motion compatibility
    """
    video_a = shot_a.get("video_prompt", "").lower()
    video_b = shot_b.get("video_prompt", "").lower()
    action_a = shot_a.get("action", "").lower()
    action_b = shot_b.get("action", "").lower()

    energy_a = beat_a.get("energy", 0.5)
    energy_b = beat_b.get("energy", 0.5)
    energy_delta = abs(energy_a - energy_b)

    # Direction analysis
    left_words = {"left", "leftward", "camera-left", "screen-left"}
    right_words = {"right", "rightward", "camera-right", "screen-right"}
    toward_words = {"toward camera", "toward us", "approaches", "approaching"}
    away_words = {"away from camera", "walks away", "recedes", "retreating"}

    a_text = f"{video_a} {action_a}"
    b_text = f"{video_b} {action_b}"

    a_left = any(w in a_text for w in left_words)
    a_right = any(w in a_text for w in right_words)
    b_left = any(w in b_text for w in left_words)
    b_right = any(w in b_text for w in right_words)

    # Direction conflict: A exits left, B enters from right (or vice versa)
    direction_conflict = (a_left and b_right) or (a_right and b_left)
    direction_match = (a_left and b_left) or (a_right and b_right)

    # Energy compatibility
    if energy_delta <= 0.2:
        energy_score = 10
    elif energy_delta <= 0.4:
        energy_score = 7
    elif energy_delta <= 0.6:
        energy_score = 4
    else:
        energy_score = 2

    # Camera motion analysis
    static_cam = {"static", "holds", "locked", "stationary"}
    moving_cam = {"push", "pull", "pan", "tilt", "track", "dolly", "drift", "follows"}

    a_static = any(w in video_a for w in static_cam)
    b_static = any(w in video_b for w in static_cam)
    a_moving = any(w in video_a for w in moving_cam)
    b_moving = any(w in video_b for w in moving_cam)

    # Both static or both moving is fine
    cam_compatible = (a_static and b_static) or (a_moving and b_moving) or \
                     (not a_static and not a_moving)  # ambiguous = ok
    cam_score = 8 if cam_compatible else 5

    # Compose
    reasons = []
    score = energy_score * 0.4 + cam_score * 0.3

    if direction_conflict:
        score -= 2
        reasons.append("DIRECTION CONFLICT across cut")
    elif direction_match:
        score += 1
        reasons.append("Direction matches")

    if energy_delta > 0.3:
        reasons.append(f"Energy delta={energy_delta:.1f}")

    score = max(0, min(10, int(score + 3)))  # base boost for compositing
    if not reasons:
        reasons.append("Motion flow compatible")

    return {"score": score, "reason": "; ".join(reasons)}


# ---------------------------------------------------------------------------
# Composite Judge
# ---------------------------------------------------------------------------

def judge_transition(shot_a: dict, shot_b: dict,
                     beat_a: dict, beat_b: dict) -> dict:
    """Score all 5 dimensions for a transition between two shots.

    Returns:
        {
            "dimensions": {
                "identity": {"score": int, "reason": str},
                "pose":     {"score": int, "reason": str},
                "camera":   {"score": int, "reason": str},
                "scene":    {"score": int, "reason": str},
                "motion":   {"score": int, "reason": str},
            },
            "composite": float,       # weighted average 0-10
            "risk_level": str,         # "low" | "medium" | "high" | "critical"
            "weakest_dimension": str,  # which dimension scored lowest
            "summary": str,            # human-readable 1-liner
        }
    """
    dimensions = {
        "identity": score_identity(shot_a, shot_b, beat_a, beat_b),
        "pose":     score_pose(shot_a, shot_b, beat_a, beat_b),
        "camera":   score_camera(shot_a, shot_b, beat_a, beat_b),
        "scene":    score_scene(shot_a, shot_b, beat_a, beat_b),
        "motion":   score_motion(shot_a, shot_b, beat_a, beat_b),
    }

    # Weighted composite
    total_weight = sum(DIMENSION_WEIGHTS.values())
    weighted_sum = sum(
        dimensions[dim]["score"] * DIMENSION_WEIGHTS[dim]
        for dim in dimensions
    )
    composite = round(weighted_sum / total_weight, 1)

    # Find weakest dimension
    weakest = min(dimensions, key=lambda d: dimensions[d]["score"])
    weakest_score = dimensions[weakest]["score"]

    # Risk level based on composite AND weakest dimension
    # A single broken dimension can make a transition fail even if composite is ok
    if composite >= 8 and weakest_score >= 6:
        risk_level = "low"
    elif composite >= 6 and weakest_score >= 4:
        risk_level = "medium"
    elif composite >= 4 or weakest_score >= 3:
        risk_level = "high"
    else:
        risk_level = "critical"

    shot_a_id = shot_a.get("shot_id", "?")
    shot_b_id = shot_b.get("shot_id", "?")
    summary = (
        f"{shot_a_id}→{shot_b_id}: composite={composite}/10, "
        f"risk={risk_level}, weakest={weakest}({weakest_score})"
    )

    return {
        "from_shot": shot_a_id,
        "to_shot": shot_b_id,
        "dimensions": dimensions,
        "composite": composite,
        "risk_level": risk_level,
        "weakest_dimension": weakest,
        "weakest_score": weakest_score,
        "summary": summary,
    }


def judge_all_transitions(plan: dict) -> list:
    """Run the judge on every consecutive shot pair in the plan.

    Works at the SHOT level (not beat level) — every cut gets scored,
    including intra-beat cuts between shots within the same beat.

    Returns list of judge results, one per cut point.
    """
    results = []
    all_shots = []  # flat list of (shot, beat) tuples

    for beat in plan.get("beats", []):
        for shot in beat.get("shots", []):
            all_shots.append((shot, beat))

    for i in range(len(all_shots) - 1):
        shot_a, beat_a = all_shots[i]
        shot_b, beat_b = all_shots[i + 1]
        result = judge_transition(shot_a, shot_b, beat_a, beat_b)
        results.append(result)

    return results


def _to_normalized(result: dict) -> dict:
    """Convert 0-10 heuristic scores to 0.0-1.0 normalized format."""
    dims = result.get("dimensions", {})
    scores = {}
    for dim, data in dims.items():
        scores[f"{dim}_continuity" if dim != "motion" else "motion_plausibility"] = \
            round(data["score"] / 10.0, 2)
    scores["overall_score"] = round(result["composite"] / 10.0, 2)

    return {
        "from_shot": result["from_shot"],
        "to_shot": result["to_shot"],
        "scores": scores,
        "risk_level": result["risk_level"],
        "weakest_dimension": result["weakest_dimension"],
        "weakest_score": round(result["weakest_score"] / 10.0, 2),
        "summary": result["summary"],
        "mode": "heuristic",
        # Keep original for backward compat
        "dimensions": dims,
        "composite": result["composite"],
    }


# ---------------------------------------------------------------------------
# Vision-Based Judge (Haiku/Sonnet)
# ---------------------------------------------------------------------------

def judge_transition_vision(shot_a: dict, shot_b: dict,
                            beat_a: dict, beat_b: dict,
                            plan: dict = None,
                            lock_ref_path: str = None,
                            shot_priority: str = "standard",
                            attempt_count: int = 0,
                            auto_escalate: bool = True) -> dict:
    """Score a transition using Claude vision (Haiku default, Sonnet escalation).

    Sends the lock reference + start anchor + end anchor to Claude for
    actual visual analysis. Much more accurate than heuristics but costs money.
    Set auto_escalate=False for Haiku-only diagnostic pass.

    Args:
        shot_a, shot_b: shot dicts with anchor_path
        beat_a, beat_b: beat dicts
        plan: production plan (for continuity locks)
        lock_ref_path: path to character/asset reference sheet
        shot_priority: standard | premium | hero
        attempt_count: number of prior attempts (triggers Sonnet escalation)

    Returns same normalized format as heuristic judge, plus vision-specific fields.
    """
    from lib.claude_client import judge_transition_vision as _call_judge
    from lib.prompt_packs import haiku_transition_judge

    # Collect images
    image_paths = []
    if lock_ref_path and os.path.isfile(lock_ref_path):
        image_paths.append(lock_ref_path)
    else:
        image_paths.append("")  # placeholder

    anchor_a = shot_a.get("anchor_path", "")
    anchor_b = shot_b.get("anchor_path", "")
    if anchor_a and os.path.isfile(anchor_a):
        image_paths.append(anchor_a)
    if anchor_b and os.path.isfile(anchor_b):
        image_paths.append(anchor_b)

    # Filter out empty/missing paths
    image_paths = [p for p in image_paths if p and os.path.isfile(p)]
    if len(image_paths) < 2:
        # Not enough images — fall back to heuristic
        print("[Judge] Not enough images for vision analysis, falling back to heuristic")
        result = judge_transition(shot_a, shot_b, beat_a, beat_b)
        return _to_normalized(result)

    # Build prompt
    continuity_lock = ""
    if plan:
        locks = plan.get("continuity_locks", {})
        continuity_lock = locks.get("dog", locks.get(
            next(iter(locks), ""), ""))

    prompt = haiku_transition_judge.render(
        shot_id=f"{shot_a.get('shot_id', '?')}→{shot_b.get('shot_id', '?')}",
        duration_sec=shot_a.get("duration", 5),
        motion_note=shot_a.get("video_prompt", ""),
        camera_note=shot_a.get("camera_height", ""),
        continuity_lock=continuity_lock,
        max_allowed_change=shot_a.get("max_allowed_change"),
    )

    # Call Haiku (with optional auto-escalation to Sonnet)
    vision_result = _call_judge(
        prompt, image_paths,
        shot_priority=shot_priority,
        attempt_count=attempt_count,
        auto_escalate=auto_escalate,
    )

    if vision_result.get("_parse_error"):
        # Vision failed — fall back to heuristic
        print("[Judge] Vision parse failed, falling back to heuristic")
        result = judge_transition(shot_a, shot_b, beat_a, beat_b)
        return _to_normalized(result)

    # Normalize vision result to match our format
    scores = vision_result.get("scores", {})
    overall = scores.get("overall_score", 0.5)

    # Map risk level
    risk = vision_result.get("risk_level", "medium")

    # Find weakest
    score_dims = {k: v for k, v in scores.items() if k != "overall_score"}
    weakest = min(score_dims, key=lambda k: score_dims[k]) if score_dims else "identity_continuity"

    return {
        "from_shot": shot_a.get("shot_id", "?"),
        "to_shot": shot_b.get("shot_id", "?"),
        "scores": scores,
        "risk_level": risk,
        "recommended_action": vision_result.get("recommended_action", "direct_animate"),
        "cut_recommendation": vision_result.get("cut_recommendation", "none"),
        "main_failure_reasons": vision_result.get("main_failure_reasons", []),
        "plain_english_summary": vision_result.get("plain_english_summary", ""),
        "prompt_adjustments": vision_result.get("prompt_adjustments", []),
        "weakest_dimension": weakest,
        "weakest_score": score_dims.get(weakest, 0.5),
        "composite": round(overall * 10, 1),  # backward compat
        "summary": vision_result.get("plain_english_summary",
                                      f"Vision: overall={overall}, risk={risk}"),
        "mode": "vision",
        "_model_used": vision_result.get("_model_used", "haiku"),
        "_escalation_reason": vision_result.get("_escalation_reason", ""),
        # Keep original dimensions for heuristic compat
        "dimensions": {
            "identity": {"score": int(scores.get("identity_continuity", 0.5) * 10),
                        "reason": "vision-scored"},
            "pose":     {"score": int(scores.get("pose_continuity", 0.5) * 10),
                        "reason": "vision-scored"},
            "camera":   {"score": int(scores.get("camera_continuity", 0.5) * 10),
                        "reason": "vision-scored"},
            "scene":    {"score": int(scores.get("scene_continuity", 0.5) * 10),
                        "reason": "vision-scored"},
            "motion":   {"score": int(scores.get("motion_plausibility", 0.5) * 10),
                        "reason": "vision-scored"},
        },
    }


def judge_all_transitions_vision(plan: dict,
                                 lock_ref_path: str = None,
                                 priority_override: str = None) -> list:
    """Run vision-based judge on all consecutive shot pairs.

    Expensive (API calls) — use judge_all_transitions() for fast heuristic mode.
    """
    results = []
    all_shots = []

    for beat in plan.get("beats", []):
        for shot in beat.get("shots", []):
            all_shots.append((shot, beat))

    for i in range(len(all_shots) - 1):
        shot_a, beat_a = all_shots[i]
        shot_b, beat_b = all_shots[i + 1]
        priority = priority_override or shot_a.get("shot_priority", "standard")
        result = judge_transition_vision(
            shot_a, shot_b, beat_a, beat_b,
            plan=plan, lock_ref_path=lock_ref_path,
            shot_priority=priority,
        )
        results.append(result)

    return results


def print_judge_report(results: list):
    """Print a human-readable transition intelligence report."""
    print("\n" + "=" * 70)
    print("TRANSITION INTELLIGENCE — Judge Report")
    print("=" * 70)

    for r in results:
        risk_icon = {"low": " ", "medium": "!", "high": "!!", "critical": "XXX"}
        icon = risk_icon.get(r["risk_level"], "?")
        mode = r.get("mode", "heuristic")
        model = r.get("_model_used", "")
        mode_label = f"{mode}" + (f"/{model}" if model else "")

        print(f"\n  [{icon}] {r['from_shot']} → {r['to_shot']}  "
              f"composite={r['composite']}/10  risk={r['risk_level']}  ({mode_label})")
        for dim, data in r.get("dimensions", {}).items():
            bar = "#" * data["score"] + "." * (10 - data["score"])
            weight = DIMENSION_WEIGHTS.get(dim, 1.0)
            print(f"      {dim:10s} [{bar}] {data['score']:2d}/10  "
                  f"(w={weight})  {data['reason']}")

        # Vision-specific fields
        if r.get("plain_english_summary"):
            print(f"      Summary: {r['plain_english_summary']}")
        if r.get("main_failure_reasons"):
            for reason in r["main_failure_reasons"]:
                print(f"      FAIL: {reason}")
        if r.get("prompt_adjustments"):
            for adj in r["prompt_adjustments"]:
                print(f"      FIX: {adj}")
