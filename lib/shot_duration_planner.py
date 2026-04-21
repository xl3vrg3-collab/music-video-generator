"""
LUMN Studio — dynamic shot duration planner.

Synthesizes a per-shot target duration (3-15s) from signals already present
in scenes.json:
  - opus_time_start / opus_time_end (per-scene-group window in the song)
  - sceneGroupId / opus_scene_id (groups shots within a window)
  - cameraAngle (wide / medium / close / insert)
  - emotion (narrative tone)
  - energy (1-10 scalar)
  - notes.transition_in (hard_cut / match_cut / action_cut / etc.)
  - opus_beat_name (Prologue, Inciting Incident, Rising Action, Climax, ...)
  - opus_lyric_anchor (lyric line + timing)

Outputs a plan: list of dicts with fields:
  - scene_id:   str (scenes.json id)
  - duration_s: int (3-15)
  - rationale:  str (human-readable explanation)
  - source:     "planner"
  - factors:    dict (all signals that fed the decision, for debugging)

The planner does NOT touch the audio file. It relies on window timings and
categorical/scalar signals already baked into scenes.json by prior planning
(Opus director, song-timing pass). A future pass can add real downbeat
snapping via lib/beat_snap.py.

Invariants:
  - Every shot duration is clamped to [3, 15] (Kling V3 range).
  - Within a scene group, the sum of shot durations targets the window
    width (opus_time_end - opus_time_start). If the window is missing,
    each shot gets its weighted base duration.
  - All weights + clamps happen before integer snap. Integer snap
    redistributes rounding delta to the longest shot so the group total
    stays within 1s of the window.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

DURATION_MIN = 3
DURATION_MAX = 15

# Base weight per camera-angle category. Wide shots carry more weight so
# the planner gives them proportionally longer holds when distributing a
# scene window across multiple shots. Close-ups get less because they
# usually punch on a beat rather than breathe.
ANGLE_WEIGHTS = {
    "wide establishing":    1.6,
    "wide":                 1.4,
    "medium-wide":          1.2,
    "medium shot":          1.0,
    "medium":               1.0,
    "medium close-up":      0.95,
    "mcu":                  0.95,
    "close-up":             0.9,
    "close":                0.9,
    "ecu":                  0.7,
    "extreme close-up":     0.7,
    "insert":               0.65,
    "cutaway":              0.7,
    "pov":                  1.1,
    "ots":                  1.0,
    "aerial":               1.4,
    "overhead":             1.3,
}

# Emotion modifier — multiplies the base weight. Reflective moods
# breathe; driving/climactic moods cut faster.
EMOTION_MODIFIERS = [
    (r"\b(climax|climactic|explosive|explosion|peak|urgent|panic|chaos|frantic)\b", 0.70),
    (r"\b(driving|propulsive|rising|building|acceleration|momentum)\b", 0.85),
    (r"\b(melancholic|reflective|contemplative|calm|still|held|quiet|reverent|tender)\b", 1.20),
    (r"\b(release|resolution|afterglow|peaceful|serene|aftermath|settle)\b", 1.25),
    (r"\b(wonder|awe|recognition|realization|discovery)\b", 1.10),
    (r"\b(grief|loss|longing|sorrow|yearning)\b", 1.15),
]

# Narrative beat multiplier — which act/section of the story is this in.
# Prologues establish, climaxes punch, outros breathe.
BEAT_NAME_MODIFIERS = [
    (r"\b(prologue|intro|opening|cold open)\b", 1.15),
    (r"\b(inciting incident|call to action|departure|threshold)\b", 1.00),
    (r"\b(rising action|complication|investigation|pursuit)\b", 0.95),
    (r"\b(midpoint|revelation|discovery)\b", 1.00),
    (r"\b(climax|crisis|confrontation|peak)\b", 0.75),
    (r"\b(falling action|aftermath|reconciliation|descent)\b", 1.10),
    (r"\b(resolution|denouement|coda|outro|ending|final)\b", 1.20),
    (r"\b(bridge|interlude)\b", 1.15),
]

# Transition style pulled from notes.transition_in — hard cuts want a
# clean tail, so we trim slightly to leave a beat of silence. Match cuts
# keep full length because motion bleeds across the cut.
TRANSITION_MODIFIERS = {
    "hard_cut":   0.92,
    "jump_cut":   0.85,
    "smash_cut":  0.80,
    "match_cut":  1.00,
    "action_cut": 1.00,
    "graphic_match": 1.00,
    "eye_trace":  1.00,
    "whip_pan":   0.90,
    "crossfade":  1.05,
    "dissolve":   1.10,
    "wipe":       0.95,
}


def _parse_transition_in(notes: str) -> str:
    if not notes:
        return ""
    m = re.search(r"transition_in\s*:\s*([a-z_]+)", notes, re.IGNORECASE)
    return (m.group(1) if m else "").strip().lower()


def _normalize_angle(raw: str) -> str:
    s = (raw or "").strip().lower()
    # Strip trailing qualifiers like "wide establishing low-angle"
    # and match longest prefix in ANGLE_WEIGHTS keys.
    best = ""
    for key in ANGLE_WEIGHTS:
        if key in s and len(key) > len(best):
            best = key
    return best or s


def _angle_weight(angle_raw: str) -> float:
    key = _normalize_angle(angle_raw)
    return ANGLE_WEIGHTS.get(key, 1.0)


def _emotion_multiplier(emotion: str) -> float:
    if not emotion:
        return 1.0
    txt = emotion.lower()
    # Compose multipliers — multiple matches compound, but clamped to
    # a sane range so we don't swing a 5s into a 15s on a gushy adjective.
    mult = 1.0
    for pattern, factor in EMOTION_MODIFIERS:
        if re.search(pattern, txt):
            mult *= factor
    return max(0.60, min(1.40, mult))


def _beat_multiplier(beat_name: str) -> float:
    if not beat_name:
        return 1.0
    txt = beat_name.lower()
    for pattern, factor in BEAT_NAME_MODIFIERS:
        if re.search(pattern, txt):
            return factor
    return 1.0


def _transition_multiplier(notes: str) -> float:
    t = _parse_transition_in(notes)
    if not t:
        return 1.0
    return TRANSITION_MODIFIERS.get(t, 1.0)


def _energy_multiplier(energy: Any) -> float:
    try:
        e = float(energy)
    except (TypeError, ValueError):
        return 1.0
    # Energy 1-10 → 1.25 at 1 → 0.70 at 10. Linear interp.
    if e <= 1:
        return 1.25
    if e >= 10:
        return 0.70
    # piecewise so mid-range stays stable
    if e <= 4:
        return 1.25 - (e - 1) * 0.08    # 1→1.25, 4→1.01
    if e <= 7:
        return 1.00 - (e - 4) * 0.05    # 4→1.00, 7→0.85
    return 0.85 - (e - 7) * 0.05        # 7→0.85, 10→0.70


def _base_seconds(angle_weight: float) -> float:
    """Base seconds before any scene-window normalization."""
    return 5.0 * angle_weight


def _plan_one_shot(scene: dict) -> dict:
    """Compute a single shot's weighted duration + rationale."""
    angle_raw = scene.get("cameraAngle", "")
    emotion   = scene.get("emotion", "")
    notes     = scene.get("notes", "")
    beat_name = scene.get("opus_beat_name", "")
    energy    = scene.get("energy", 5)

    w_angle   = _angle_weight(angle_raw)
    m_emotion = _emotion_multiplier(emotion)
    m_beat    = _beat_multiplier(beat_name)
    m_trans   = _transition_multiplier(notes)
    m_energy  = _energy_multiplier(energy)

    base = _base_seconds(w_angle)
    weighted = base * m_emotion * m_beat * m_trans * m_energy

    rationale_bits = []
    rationale_bits.append(f"angle={_normalize_angle(angle_raw) or 'default'}×{w_angle:.2f}")
    if m_emotion != 1.0:
        rationale_bits.append(f"emotion×{m_emotion:.2f}")
    if m_beat != 1.0:
        rationale_bits.append(f"beat×{m_beat:.2f}")
    if m_trans != 1.0:
        trans = _parse_transition_in(notes) or "trans"
        rationale_bits.append(f"{trans}×{m_trans:.2f}")
    if m_energy != 1.0:
        rationale_bits.append(f"energy{energy}×{m_energy:.2f}")

    return {
        "scene_id":   scene.get("id", ""),
        "opus_shot_id": scene.get("opus_shot_id", ""),
        "order_index": scene.get("orderIndex", 999),
        "group_id":   scene.get("sceneGroupId") or scene.get("opus_scene_id") or "",
        "window_start": scene.get("opus_time_start"),
        "window_end":   scene.get("opus_time_end"),
        "base_seconds": round(base, 2),
        "weighted_seconds": round(weighted, 2),
        "factors": {
            "angle_weight":    round(w_angle, 3),
            "emotion_mult":    round(m_emotion, 3),
            "beat_mult":       round(m_beat, 3),
            "transition_mult": round(m_trans, 3),
            "energy_mult":     round(m_energy, 3),
        },
        "rationale_bits": rationale_bits,
    }


def _distribute_window(group_shots: list[dict], window_s: float | None) -> None:
    """Distribute the scene window across its shots proportional to
    weighted_seconds, then integer-snap and clamp to [3,15]. Mutates
    each shot dict in place with final `duration_s` + `rationale`.
    """
    if not group_shots:
        return

    weighted_total = sum(s["weighted_seconds"] for s in group_shots) or 1.0

    if window_s and window_s > 0:
        # Window-proportional mode
        target_total = float(window_s)
        raw_allocs = [s["weighted_seconds"] / weighted_total * target_total for s in group_shots]
        mode = "window"
    else:
        # Free mode — each shot keeps its weighted duration
        raw_allocs = [s["weighted_seconds"] for s in group_shots]
        mode = "free"

    # Clamp each to [3, 15], then integer-snap
    int_allocs = []
    for a in raw_allocs:
        clamped = max(DURATION_MIN, min(DURATION_MAX, a))
        int_allocs.append(int(round(clamped)))

    # Reconcile rounding delta so total matches target (only in window mode)
    if mode == "window":
        current = sum(int_allocs)
        delta = int(round(window_s - current))
        if delta != 0:
            # Apply delta to shots with most slack (closest to mid 9s)
            # prefer growing longest shots, shrinking shortest
            order = sorted(range(len(int_allocs)),
                           key=lambda i: -int_allocs[i] if delta > 0 else int_allocs[i])
            step = 1 if delta > 0 else -1
            for idx in order:
                if delta == 0:
                    break
                new_val = int_allocs[idx] + step
                if DURATION_MIN <= new_val <= DURATION_MAX:
                    int_allocs[idx] = new_val
                    delta -= step

    for shot, final in zip(group_shots, int_allocs):
        shot["duration_s"] = int(final)
        rationale = " · ".join(shot["rationale_bits"])
        if mode == "window":
            rationale += f" · window={window_s:.1f}s→{final}s"
        else:
            rationale += f" · free={final}s"
        shot["rationale"] = rationale


def plan_scene_durations(scenes: list[dict]) -> list[dict]:
    """Plan durations for all scenes. Returns a list of plan dicts,
    one per input scene, in the same order as input.

    Scenes with the same sceneGroupId / opus_scene_id are treated as a
    group and share a window (from opus_time_start / opus_time_end).
    """
    if not scenes:
        return []

    # Compute per-shot weighted seconds
    plans = [_plan_one_shot(s) for s in scenes]

    # Group by sceneGroupId (or opus_scene_id)
    groups: dict[str, list[dict]] = {}
    group_order: list[str] = []
    for p in plans:
        gid = p["group_id"] or p["scene_id"]
        if gid not in groups:
            groups[gid] = []
            group_order.append(gid)
        groups[gid].append(p)

    # Distribute each group's window
    for gid in group_order:
        group_shots = groups[gid]
        # Window = max(end) - min(start) across group (all shots should share)
        starts = [g["window_start"] for g in group_shots if g["window_start"] is not None]
        ends   = [g["window_end"]   for g in group_shots if g["window_end"]   is not None]
        window_s = None
        if starts and ends:
            try:
                window_s = float(max(ends)) - float(min(starts))
            except (TypeError, ValueError):
                window_s = None
        _distribute_window(group_shots, window_s)

    # Flatten back to input order — each `plans` entry is already a dict
    # with duration_s + rationale mutated in place.
    out = []
    for p in plans:
        out.append({
            "scene_id":   p["scene_id"],
            "opus_shot_id": p["opus_shot_id"],
            "order_index": p["order_index"],
            "group_id":   p["group_id"],
            "duration_s": p.get("duration_s", DURATION_MIN),
            "rationale":  p.get("rationale", ""),
            "source":     "planner",
            "factors":    p["factors"],
        })
    return out


def plan_from_scenes_file(scenes_path: str) -> list[dict]:
    """Convenience: load scenes.json from disk and plan."""
    with open(scenes_path, encoding="utf-8") as f:
        scenes = json.load(f)
    return plan_scene_durations(scenes)


def apply_plan_to_scenes(scenes: list[dict], plan: list[dict],
                         write_fields: bool = True) -> list[dict]:
    """Merge plan results into scene dicts in place.

    For each scene, sets:
      duration          — the final integer duration (keeps existing field)
      duration_s        — same value, explicit
      duration_rationale — human-readable rationale from planner
      duration_source   — "planner"
    """
    if not write_fields:
        return scenes
    by_id = {p["scene_id"]: p for p in plan}
    for s in scenes:
        p = by_id.get(s.get("id"))
        if not p:
            continue
        s["duration"] = int(p["duration_s"])
        s["duration_s"] = int(p["duration_s"])
        s["duration_rationale"] = p["rationale"]
        s["duration_source"] = "planner"
    return scenes


def main_cli(argv: list[str] | None = None) -> int:
    """CLI: python -m lib.shot_duration_planner <scenes.json> [--apply]

    --apply writes the planned durations back to the file.
    """
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m lib.shot_duration_planner <scenes.json> [--apply]")
        return 1
    scenes_path = args[0]
    apply = "--apply" in args

    with open(scenes_path, encoding="utf-8") as f:
        scenes = json.load(f)

    plan = plan_scene_durations(scenes)
    total_planned = sum(p["duration_s"] for p in plan)

    print(f"Planned {len(plan)} shots, total {total_planned}s")
    print(f"{'shot':>6} {'dur':>4} rationale")
    for p in plan:
        print(f"{p['opus_shot_id']:>6} {p['duration_s']:>3}s {p['rationale']}")

    if apply:
        apply_plan_to_scenes(scenes, plan, write_fields=True)
        with open(scenes_path, "w", encoding="utf-8") as f:
            json.dump(scenes, f, indent=2, ensure_ascii=False)
        print(f"\nApplied to {scenes_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
