"""
Shot Coherence Scoring — Score shots and sequences for cinematic quality and consistency.

Scores from 0-100 using 5 weighted categories:
1. Continuity Consistency (25%)
2. Cinematic Quality (25%)
3. Narrative Relevance (20%)
4. Style Consistency (15%)
5. Pacing Fit (15%)
"""


def _clamp(val, lo=0, hi=100):
    return max(lo, min(hi, int(val)))


def score_shot(shot: dict, prev_shot: dict = None, scene: dict = None,
               style_memory: dict = None, beat_sync: dict = None) -> dict:
    """Score a single shot across all 5 categories."""
    warnings = []
    suggestions = []

    # 1. CONTINUITY (25%)
    cont_score = 100
    cont = shot.get("continuity", {})
    if not cont.get("lock_environment", True):
        cont_score -= 15
        warnings.append("Environment not locked — drift risk")
    if not cont.get("lock_lighting", True):
        cont_score -= 15
        warnings.append("Lighting not locked — inconsistency risk")
    if not cont.get("lock_props", True):
        cont_score -= 10
    if prev_shot:
        prev_end = (prev_shot.get("action", {}).get("end_pose") or "").strip()
        cur_start = (shot.get("action", {}).get("start_pose") or "").strip()
        if prev_end and cur_start and prev_end.lower() != cur_start.lower():
            cont_score -= 20
            warnings.append(f"Pose mismatch: prev ends '{prev_end}', this starts '{cur_start}'")
        elif prev_end and not cur_start:
            cont_score -= 10
            suggestions.append("Add start pose matching previous shot's end pose")
    cont_score = _clamp(cont_score)

    # 2. CINEMATIC QUALITY (25%)
    cine_score = 50  # base
    cam = shot.get("camera", {})
    perf = shot.get("performance", {})
    framing = shot.get("framing", {})

    if cam.get("preset"):
        cine_score += 15  # using a cinematic preset
    if cam.get("lens"):
        cine_score += 10
    if cam.get("movement") and cam["movement"] != "static":
        cine_score += 10
    elif cam.get("movement") == "static":
        cine_score += 5  # intentional static is fine
    if framing.get("composition"):
        cine_score += 10
    if perf.get("emotion"):
        cine_score += 5

    # Penalize identical framing to previous shot
    if prev_shot:
        prev_cam = prev_shot.get("camera", {})
        if (cam.get("shot_type") == prev_cam.get("shot_type") and
            cam.get("lens") == prev_cam.get("lens") and
            cam.get("movement") == prev_cam.get("movement")):
            cine_score -= 15
            warnings.append("Redundant framing — same as previous shot")
            suggestions.append("Vary lens or shot type for visual diversity")
    cine_score = _clamp(cine_score)

    # 3. NARRATIVE RELEVANCE (20%)
    narr_score = 50
    action = shot.get("action", {})
    layers = shot.get("layers", {})

    if action.get("summary"):
        narr_score += 20
    else:
        warnings.append("No action description")
        suggestions.append("Add action summary for narrative clarity")
    if layers.get("surface"):
        narr_score += 10
    if layers.get("emotional"):
        narr_score += 10
    if layers.get("symbolic"):
        narr_score += 10
    narr_score = _clamp(narr_score)

    # 4. STYLE CONSISTENCY (15%)
    style_score = 70  # base
    styles = shot.get("style_selections", {})
    if styles.get("lighting"):
        style_score += 10
    if styles.get("color_grading"):
        style_score += 10
    if styles.get("director_styles"):
        style_score += 10
    if style_memory:
        if style_memory.get("locked"):
            style_score += 5  # locked = inherently consistent
        if style_memory.get("mood_profile") and perf.get("emotion"):
            # Check mood alignment
            pass  # no penalty for now
    style_score = _clamp(style_score)

    # 5. PACING FIT (15%)
    pace_score = 70
    dur = shot.get("duration", 4)
    if beat_sync:
        # Check if duration fits section
        for cut in beat_sync.get("cuts", []):
            if cut.get("shot_index") == shot.get("shot_number", 1) - 1:
                rec_dur = cut.get("duration", 4)
                diff = abs(dur - rec_dur)
                if diff < 0.5:
                    pace_score += 20
                elif diff < 1.5:
                    pace_score += 10
                else:
                    pace_score -= 10
                    suggestions.append(f"Adjust duration to ~{rec_dur}s for better music sync")
                break
    if dur < 1:
        pace_score -= 10
        warnings.append("Shot very short — may feel jarring")
    elif dur > 8:
        pace_score -= 5
        suggestions.append("Consider splitting long shot for more visual interest")
    pace_score = _clamp(pace_score)

    # Weighted total
    total = int(
        cont_score * 0.25 +
        cine_score * 0.25 +
        narr_score * 0.20 +
        style_score * 0.15 +
        pace_score * 0.15
    )

    # Label
    if total >= 90:
        label = "Elite"
    elif total >= 75:
        label = "Strong"
    elif total >= 60:
        label = "Needs Work"
    else:
        label = "Weak"

    return {
        "shot_id": shot.get("id", ""),
        "total_score": total,
        "label": label,
        "breakdown": {
            "continuity": cont_score,
            "cinematic_quality": cine_score,
            "narrative_relevance": narr_score,
            "style_consistency": style_score,
            "pacing_fit": pace_score,
        },
        "warnings": warnings,
        "suggestions": suggestions,
    }


def score_scene(shots: list, scene: dict = None, style_memory: dict = None,
                beat_sync: dict = None) -> dict:
    """Score an entire scene's shot sequence."""
    if not shots:
        return {"score": 0, "label": "Empty", "strengths": [], "weaknesses": [], "shots": []}

    shot_scores = []
    for i, shot in enumerate(shots):
        prev = shots[i-1] if i > 0 else None
        s = score_shot(shot, prev, scene, style_memory, beat_sync)
        shot_scores.append(s)

    avg = sum(s["total_score"] for s in shot_scores) / len(shot_scores)

    # Identify strengths/weaknesses
    strengths = []
    weaknesses = []
    cats = ["continuity", "cinematic_quality", "narrative_relevance", "style_consistency", "pacing_fit"]
    for cat in cats:
        cat_avg = sum(s["breakdown"][cat] for s in shot_scores) / len(shot_scores)
        nice_name = cat.replace("_", " ").title()
        if cat_avg >= 85:
            strengths.append(f"{nice_name}: {int(cat_avg)}")
        elif cat_avg < 65:
            weaknesses.append(f"{nice_name}: {int(cat_avg)}")

    label = "Elite" if avg >= 90 else "Strong" if avg >= 75 else "Needs Work" if avg >= 60 else "Weak"

    return {
        "scene_id": scene.get("id", "") if scene else "",
        "score": int(avg),
        "label": label,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "shots": shot_scores,
    }


def score_project(all_scenes: dict, scenes_data: list = None,
                   style_memory: dict = None) -> dict:
    """Score the entire project."""
    scene_scores = []
    for scene_id, shots in all_scenes.items():
        scene = None
        if scenes_data:
            for sd in scenes_data:
                if sd.get("id") == scene_id:
                    scene = sd
                    break
        ss = score_scene(shots, scene, style_memory)
        scene_scores.append(ss)

    if not scene_scores:
        return {"score": 0, "label": "Empty", "scenes": []}

    avg = sum(s["score"] for s in scene_scores) / len(scene_scores)
    strongest = max(scene_scores, key=lambda s: s["score"])
    weakest = min(scene_scores, key=lambda s: s["score"])

    return {
        "score": int(avg),
        "label": "Elite" if avg >= 90 else "Strong" if avg >= 75 else "Needs Work" if avg >= 60 else "Weak",
        "strongest_scene": strongest.get("scene_id", ""),
        "weakest_scene": weakest.get("scene_id", ""),
        "scenes": scene_scores,
    }
