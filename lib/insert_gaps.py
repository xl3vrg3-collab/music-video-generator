"""Insert-gap reporter (F7a) — compare pacing arc to actual scene coverage.

Given a pacing curve (from F5) and a flat scenes.json, this module:
  * Maps each scene to the pacing section whose [start_s, end_s] contains its
    opus_time_start.
  * Computes per-section gap = suggested_cuts - actual_scene_count.
  * For each gap, proposes N candidate insert specs anchored to the scenes
    in that section. Each candidate reuses the anchor scene's
    character/costume/environment but swaps framing, emotion, or action to
    give a fresh edit beat.

Output is a draft `insert_candidates.json` — human reviews and approves before
rendering. No image/video gen; no network calls; pure logic.

Public API:
    analyze_gaps(pacing_curve: dict, scenes: list[dict]) -> dict
    propose_inserts(scene: dict, count: int, section_label: str) -> list[dict]
    build_report(pacing_path, scenes_path) -> dict

The point is to give v7-shape MVs a *concrete shopping list* of what to render
next, rather than a vague 'add more shots'. With the shopping list, Opus or the
user can refine prompts, but the count/distribution is already planned.
"""
from __future__ import annotations

import json
import pathlib


# For each section label, the kinds of inserts that fit editorially. These are
# short, reaction-oriented beats that intercut with the main scene.
INSERT_TEMPLATES = {
    "intro": [
        ("establishing_detail", "tight detail of environment — mood-setting texture"),
        ("slow_reveal_tilt",    "tilt up from ground to subject, revealing silhouette"),
    ],
    "verse": [
        ("reaction_medium",     "TB medium shot — eyeline reaction to the last beat"),
        ("hand_or_paw_insert",  "close-up on paws / hands — detail punctuation"),
    ],
    "build": [
        ("emblem_punch_in",     "push-in on crescent emblem, energy rising"),
        ("environmental_cut",   "cutaway to environment shifting — wind, light, debris"),
        ("silhouette_cutaway",  "backlit silhouette beat, negative space foreground"),
    ],
    "chorus": [
        ("rapid_reaction",      "TB reaction close — eyes widening on the hook"),
        ("motion_trail_insert", "brief motion-trail composite of the subject in arc"),
    ],
    "climax": [
        ("emblem_flash",        "emblem pulse brightness flare — single-bar impact"),
        ("debris_particle",     "particle / debris cutaway — peripheral chaos"),
        ("hand_thrust",         "fist / paw into frame — kinetic gesture, 1 bar"),
        ("rapid_face_cut",      "ECU face cut — single-bar emotional peak"),
        ("pov_fragment",        "POV fragment — subject's-eye-view slice"),
    ],
    "transition": [
        ("pivot_whip",          "whip pan or match-cut bridge — section hinge"),
    ],
    "outro": [
        ("slow_pullback",       "slow pullback revealing final composition"),
        ("final_hold",          "held beat — subject stillness on outro breath"),
    ],
}


def _scene_section(scene: dict, sections: list[dict]) -> int | None:
    """Return the index of the pacing section containing this scene's midpoint."""
    start = float(scene.get("opus_time_start") or 0.0)
    end   = float(scene.get("opus_time_end")   or (start + (scene.get("duration") or 4.0)))
    mid   = (start + end) / 2.0
    for s in sections:
        if s["start_s"] <= mid < s["end_s"]:
            return s["index"]
    # Scenes past the last boundary anchor to the last section.
    return sections[-1]["index"] if sections else None


def analyze_gaps(pacing_curve: dict, scenes: list[dict]) -> dict:
    """Section-by-section comparison of pacing suggestions vs actual scenes."""
    sections = pacing_curve.get("sections") or []
    per_section: dict[int, list[dict]] = {s["index"]: [] for s in sections}
    for sc in scenes:
        idx = _scene_section(sc, sections)
        if idx is not None:
            per_section[idx].append(sc)

    report = []
    total_gap = 0
    for s in sections:
        actual = per_section.get(s["index"], [])
        gap = max(0, int(s["suggested_cuts"]) - len(actual))
        total_gap += gap
        report.append({
            "index":            s["index"],
            "label":            s["label"],
            "start_s":          s["start_s"],
            "end_s":            s["end_s"],
            "duration_s":       s["duration_s"],
            "suggested_cuts":   s["suggested_cuts"],
            "actual_scenes":    len(actual),
            "gap":              gap,
            "anchor_scene_ids": [sc.get("opus_shot_id") or sc.get("opus_scene_id") or sc.get("id") for sc in actual],
        })

    return {
        "sections":  report,
        "total_gap": total_gap,
        "total_existing_scenes": sum(len(v) for v in per_section.values()),
        "total_suggested_cuts":  pacing_curve.get("total_suggested_cuts"),
    }


def propose_inserts(scene: dict, count: int, section_label: str) -> list[dict]:
    """Draft `count` insert specs anchored to a single scene.

    Each returned dict is a scenes.json-shaped skeleton: id/name/envelope fields
    from the anchor, with a fresh `shotDescription`, `coverageTier: 'P2'`, and
    `opus_time_start/end` left to the caller to assign.
    """
    templates = INSERT_TEMPLATES.get(section_label) or INSERT_TEMPLATES["build"]
    inserts = []
    scene_tag = scene.get("opus_shot_id") or scene.get("opus_scene_id") or scene.get("id") or "anchor"
    for i in range(count):
        kind, description = templates[i % len(templates)]
        inserts.append({
            "insert_of":        scene_tag,
            "kind":             kind,
            "shotDescription":  description,
            "cameraAngle":      "insert",
            "cameraMovement":   "static" if kind.endswith(("insert", "hold", "punch_in", "flash", "pov_fragment")) else "subtle",
            "duration":         2,
            "characterId":      scene.get("characterId"),
            "costumeId":        scene.get("costumeId"),
            "environmentId":    scene.get("environmentId"),
            "promptId":         scene.get("promptId"),
            "emotion":          scene.get("emotion"),
            "energy":           scene.get("energy"),
            "coverageTier":     "P2",
            "sceneGroupId":     scene.get("sceneGroupId"),
            "opus_scene_id":    f"{scene_tag}_ins{i+1}",
            "opus_beat_name":   scene.get("opus_beat_name"),
            "sceneType":        "insert",
            "narrativeIntent":  f"{section_label} insert beat — {description}",
            "notes":            f"Auto-drafted F7a insert for {section_label} section. Review before rendering.",
        })
    return inserts


def build_report(pacing_path: str, scenes_path: str) -> dict:
    """File-wrapper: load pacing_curve + scenes, emit full gap+candidate report."""
    pacing = json.loads(pathlib.Path(pacing_path).read_text(encoding="utf-8"))
    scenes = json.loads(pathlib.Path(scenes_path).read_text(encoding="utf-8"))
    if isinstance(scenes, dict):
        scenes = scenes.get("scenes") or []

    gaps = analyze_gaps(pacing, scenes)

    # Round-robin anchor selection across the section's scenes so inserts
    # spread across multiple anchors rather than clustering on the first.
    candidates: list[dict] = []
    by_id = {(sc.get("opus_shot_id") or sc.get("opus_scene_id") or sc.get("id")): sc for sc in scenes}
    for sec in gaps["sections"]:
        if sec["gap"] <= 0 or not sec["anchor_scene_ids"]:
            continue
        anchors = [by_id[sid] for sid in sec["anchor_scene_ids"] if sid in by_id]
        if not anchors:
            continue
        per_anchor = [0] * len(anchors)
        for i in range(sec["gap"]):
            a_idx = i % len(anchors)
            per_anchor[a_idx] += 1
        for a, n in zip(anchors, per_anchor):
            if n > 0:
                candidates.extend(propose_inserts(a, n, sec["label"]))

    return {
        "pacing_path":    pacing_path,
        "scenes_path":    scenes_path,
        "gaps":           gaps,
        "candidates":     candidates,
        "total_candidates": len(candidates),
    }
