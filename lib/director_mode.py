"""
Full Director Mode — AI-directed cinematic planning system.

Generates complete video plans from song + concept:
- Narrative arc + emotional curve
- Scene list with roles and timing
- Shot list per scene with coverage
- Beat-synced timing
- Style rules + continuity strategy
"""

import json
import os
import time

from lib.narrative_engine import generate_narrative_plan, ARC_TYPES, EMOTIONAL_CURVES
from lib.beat_sync import generate_beat_sync_plan, SECTION_PACING, CUT_MODES
from lib.coverage_system import generate_coverage, COVERAGE_MODES
from lib.coherence_scorer import score_scene


# Pacing styles
PACING_STYLES = {
    "contemplative": {"desc": "Long shots, slow builds, held moments", "cut_mode": "minimal", "dur_mult": 1.4},
    "balanced": {"desc": "Standard music video pacing", "cut_mode": "balanced", "dur_mult": 1.0},
    "energetic": {"desc": "Fast cuts, dynamic camera, high energy", "cut_mode": "aggressive", "dur_mult": 0.7},
    "hypercut": {"desc": "Extreme fast cuts, beat-reactive", "cut_mode": "aggressive", "dur_mult": 0.5},
}

# Coverage strategies by scene importance
COVERAGE_STRATEGY = {
    "intro": "minimal",
    "verse": "standard",
    "pre-chorus": "standard",
    "chorus": "music_video",
    "bridge": "experimental",
    "outro": "minimal",
    "build": "standard",
    "breakdown": "experimental",
    "transition": "minimal",
}

# Emotion → visual style mapping
EMOTION_STYLE_MAP = {
    "calm": {"lighting": "natural soft", "color_grade": "Muted Pastels", "camera": "tarkovsky_stillness"},
    "tense": {"lighting": "low key dramatic", "color_grade": "Deep Shadows Contrast", "camera": "fincher_slow_creep"},
    "aggressive": {"lighting": "neon", "color_grade": "Cyberpunk Neon", "camera": "music_video_fast_cut"},
    "emotional": {"lighting": "golden hour", "color_grade": "Warm Vintage", "camera": "handheld_documentary"},
    "triumphant": {"lighting": "cinematic contrast", "color_grade": "Teal and Orange", "camera": "nolan_push_in"},
    "dark": {"lighting": "high contrast noir", "color_grade": "Desaturated Grit", "camera": "fincher_slow_creep"},
    "mysterious": {"lighting": "volumetric fog", "color_grade": "Dark Purple Tint", "camera": "kubrick_symmetry_static"},
    "chaotic": {"lighting": "flickering", "color_grade": "Bleach Bypass", "camera": "music_video_fast_cut"},
    "cinematic": {"lighting": "cinematic contrast", "color_grade": "Teal and Orange", "camera": "spielberg_tracking"},
    "surreal": {"lighting": "mixed color", "color_grade": "High Exposure Dream", "camera": "tarkovsky_stillness"},
    "melancholic": {"lighting": "window side", "color_grade": "Soft Film Fade", "camera": "handheld_documentary"},
}


def generate_director_plan(
    audio_analysis: dict = None,
    lyrics: str = "",
    storyline: str = "",
    style: str = "",
    world_setting: str = "",
    arc_type: str = "rise",
    pacing_style: str = "balanced",
    coverage_mode: str = "standard",
    emotional_intensity: float = 0.7,
    abstract_level: float = 0.3,
    characters: list = None,
    environments: list = None,
    costumes: list = None,
) -> dict:
    """
    Generate a complete director plan from inputs.

    Returns director_plan dict with theme, arc, scenes, shots, style rules, etc.
    """
    characters = characters or []
    environments = environments or []
    costumes = costumes or []
    audio_analysis = audio_analysis or {}

    duration = audio_analysis.get("duration", 30)
    sections = audio_analysis.get("sections", [])
    bpm = audio_analysis.get("bpm", 120)

    if not sections:
        sec_dur = duration / 4
        sections = [
            {"start": 0, "end": sec_dur, "type": "intro", "energy": 0.3},
            {"start": sec_dur, "end": sec_dur*2, "type": "verse", "energy": 0.5},
            {"start": sec_dur*2, "end": sec_dur*3, "type": "chorus", "energy": 0.9},
            {"start": sec_dur*3, "end": duration, "type": "outro", "energy": 0.3},
        ]

    pacing = PACING_STYLES.get(pacing_style, PACING_STYLES["balanced"])

    # 1. Generate narrative arc
    narrative = generate_narrative_plan(
        arc_type=arc_type,
        theme=storyline or style,
        storyline=storyline,
        lyrics=lyrics,
        sections=sections,
        characters=characters,
    )

    # 2. Generate beat sync plan
    beat_plan = generate_beat_sync_plan(
        audio_analysis,
        cut_mode=pacing["cut_mode"],
    )

    # 3. Determine style rules from dominant emotion
    dominant_emotion = "cinematic"
    if narrative.get("emotional_curve"):
        curve = narrative["emotional_curve"]
        mid_val = curve[len(curve)//2] if curve else 0.5
        if mid_val > 0.8:
            dominant_emotion = "aggressive" if emotional_intensity > 0.7 else "triumphant"
        elif mid_val > 0.6:
            dominant_emotion = "cinematic"
        elif mid_val > 0.4:
            dominant_emotion = "tense" if emotional_intensity > 0.5 else "emotional"
        else:
            dominant_emotion = "melancholic" if abstract_level < 0.5 else "surreal"

    emo_style = EMOTION_STYLE_MAP.get(dominant_emotion, EMOTION_STYLE_MAP["cinematic"])

    style_rules = {
        "lighting_style": emo_style["lighting"],
        "color_grade": emo_style["color_grade"],
        "camera_language": emo_style["camera"],
        "pacing_style": pacing_style,
        "dominant_emotion": dominant_emotion,
        "abstract_level": abstract_level,
        "emotional_intensity": emotional_intensity,
    }

    # 4. Build scene list with narrative roles
    scene_plans = []
    for i, section in enumerate(sections):
        sec_type = section.get("type", "verse")
        sec_start = section.get("start", 0)
        sec_end = section.get("end", 0)
        sec_energy = section.get("energy", 0.5)

        # Get narrative data for this scene
        narr_scene = narrative["scenes"][i] if i < len(narrative["scenes"]) else {}

        # Pick character (rotate through available)
        char = characters[i % len(characters)] if characters else None
        # Pick environment
        env = environments[i % len(environments)] if environments else None
        # Pick costume
        costume = costumes[i % len(costumes)] if costumes else None

        # Emotion for this scene from curve
        emo_val = narrative["emotional_curve"][i] if i < len(narrative["emotional_curve"]) else 0.5
        scene_emotion = dominant_emotion
        if emo_val < 0.3:
            scene_emotion = "calm"
        elif emo_val < 0.5:
            scene_emotion = "melancholic" if abstract_level > 0.5 else "tense"
        elif emo_val > 0.8:
            scene_emotion = "aggressive" if sec_type == "chorus" else "triumphant"

        scene = {
            "scene_index": i,
            "name": narr_scene.get("symbolic_goal", f"Scene {i+1} — {sec_type}"),
            "type": sec_type,
            "role": narr_scene.get("role", ""),
            "purpose": narr_scene.get("emotional_goal", ""),
            "emotional_goal": narr_scene.get("emotional_goal", ""),
            "symbolic_goal": narr_scene.get("symbolic_goal", ""),
            "emotion": scene_emotion,
            "energy": round(sec_energy * 10),
            "start_time": round(sec_start, 2),
            "end_time": round(sec_end, 2),
            "duration": round(sec_end - sec_start, 1),
            "character_id": char["id"] if char else None,
            "character_name": char["name"] if char else None,
            "environment_id": env["id"] if env else None,
            "environment_name": env["name"] if env else None,
            "costume_id": costume["id"] if costume else None,
            "transition_to_next": narr_scene.get("narrative_transition_to_next", ""),
        }
        scene_plans.append(scene)

    # 5. Generate shots per scene using coverage system
    all_shots = {}
    total_shots = 0
    for scene in scene_plans:
        cov_mode = COVERAGE_STRATEGY.get(scene["type"], coverage_mode)
        beat = scene.get("purpose") or scene.get("name", "")

        coverage = generate_coverage(
            scene_beat=beat,
            mode=cov_mode,
            section_type=scene["type"],
        )

        # Adjust shot durations by pacing
        for shot in coverage["shots"]:
            shot["duration"] = round(shot["duration"] * pacing["dur_mult"], 1)
            shot["duration"] = max(1.5, min(shot["duration"], 8))

        all_shots[scene["scene_index"]] = coverage["shots"]
        total_shots += len(coverage["shots"])
        scene["shot_count"] = len(coverage["shots"])

    # 6. Build continuity strategy
    continuity_strategy = {
        "character_lock": True,
        "environment_lock_per_scene": True,
        "lighting_consistency": True,
        "costume_consistency": True,
        "reference_frame_chain": True,
    }

    # 7. Assemble plan
    director_plan = {
        "theme": narrative.get("theme", storyline or "cinematic music video"),
        "arc_type": arc_type,
        "arc_description": ARC_TYPES.get(arc_type, ""),
        "protagonist_arc": narrative.get("protagonist_arc", ""),
        "emotional_curve": narrative.get("emotional_curve", []),
        "style_rules": style_rules,
        "scenes": scene_plans,
        "shots": all_shots,
        "total_scenes": len(scene_plans),
        "total_shots": total_shots,
        "duration": round(duration, 1),
        "bpm": round(bpm, 1),
        "beat_sync_plan": {
            "total_cuts": beat_plan.get("total_cuts", 0),
            "pacing_profile": beat_plan.get("pacing_profile", ""),
            "cut_mode": pacing["cut_mode"],
        },
        "continuity_strategy": continuity_strategy,
        "coverage_strategy": {
            "mode": coverage_mode,
            "section_overrides": dict(COVERAGE_STRATEGY),
        },
        "pacing_style": pacing_style,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    return director_plan


def get_pacing_styles() -> dict:
    return {k: v["desc"] for k, v in PACING_STYLES.items()}
