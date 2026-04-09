"""
Beat-Synced Shot Generator — Align shot timing, cuts, and pacing to music.

Creates musically satisfying cuts, not random edits or mechanical beat spam.
"""

import json
import os
import math

# Shot duration presets
DURATION_PRESETS = {
    "very_fast": (0.5, 1.2),
    "fast": (1.2, 2.0),
    "medium": (2.0, 3.5),
    "slow": (3.5, 6.0),
    "held": (6.0, 10.0),
}

# Section pacing rules
SECTION_PACING = {
    "intro": {"duration": "slow", "movement": "minimal", "cuts": "few", "energy_mult": 0.5},
    "verse": {"duration": "medium", "movement": "moderate", "cuts": "moderate", "energy_mult": 0.7},
    "pre-chorus": {"duration": "fast", "movement": "increasing", "cuts": "increasing", "energy_mult": 0.85},
    "chorus": {"duration": "fast", "movement": "dynamic", "cuts": "frequent", "energy_mult": 1.0},
    "bridge": {"duration": "medium", "movement": "experimental", "cuts": "varied", "energy_mult": 0.6},
    "outro": {"duration": "held", "movement": "resolving", "cuts": "few", "energy_mult": 0.4},
}

# Cut aggressiveness presets
CUT_MODES = {
    "minimal": {"cuts_per_bar": 0.5, "desc": "Fewer cuts, longer shots, contemplative pacing"},
    "balanced": {"cuts_per_bar": 1.0, "desc": "Standard music video pacing, cut on bars"},
    "aggressive": {"cuts_per_bar": 2.0, "desc": "Fast cuts, hypercut energy, beat-reactive"},
}

# Sync priority modes
SYNC_PRIORITIES = {
    "beat": {"beat_weight": 1.0, "lyric_weight": 0.3, "narrative_weight": 0.2},
    "lyric": {"beat_weight": 0.3, "lyric_weight": 1.0, "narrative_weight": 0.4},
    "narrative": {"beat_weight": 0.2, "lyric_weight": 0.4, "narrative_weight": 1.0},
    "hybrid": {"beat_weight": 0.6, "lyric_weight": 0.5, "narrative_weight": 0.5},
}


def _approx_in(val, lst, tol=0.01):
    """Check if val is approximately in lst (within tolerance)."""
    return any(abs(val - v) < tol for v in lst)


def analyze_for_sync(audio_analysis: dict) -> dict:
    """
    Build a beat-sync analysis from audio analysis data.

    Args:
        audio_analysis: dict from audio_analyzer.analyze() with bpm, beats, sections, duration

    Returns:
        Enhanced analysis with bar_times, downbeats, transition points
    """
    bpm = audio_analysis.get("bpm", 120)
    beats = audio_analysis.get("beats", [])
    sections = audio_analysis.get("sections", [])
    duration = audio_analysis.get("duration", 0)

    # Calculate bar times (4 beats per bar)
    beat_duration = 60.0 / bpm if bpm > 0 else 0.5
    bar_duration = beat_duration * 4
    if bar_duration <= 0:
        bar_duration = 0.5  # Fallback
    bar_times = []
    t = 0
    while t < duration:
        bar_times.append(round(t, 3))
        t += bar_duration

    # Identify downbeats (first beat of each bar)
    downbeats = bar_times[:]

    # Identify section transitions
    transitions = []
    for i, sec in enumerate(sections):
        transitions.append({
            "time": sec.get("start", 0),
            "from_type": sections[i-1].get("type", "intro") if i > 0 else None,
            "to_type": sec.get("type", "verse"),
            "energy_change": sec.get("energy", 0.5),
        })

    # Energy curve from sections
    energy_curve = []
    for sec in sections:
        energy_curve.append({
            "start": sec.get("start", 0),
            "end": sec.get("end", 0),
            "energy": sec.get("energy", 0.5),
            "type": sec.get("type", "verse"),
        })

    return {
        "duration_seconds": round(duration, 3),
        "bpm": round(bpm, 1),
        "beat_times": beats,
        "bar_times": bar_times,
        "section_markers": sections,
        "energy_curve": energy_curve,
        "downbeats": downbeats,
        "transitions": transitions,
        "beat_duration": round(beat_duration, 4),
        "bar_duration": round(bar_duration, 4),
    }


def generate_beat_sync_plan(
    audio_analysis: dict,
    num_shots: int = None,
    cut_mode: str = "balanced",
    sync_priority: str = "hybrid",
    sections_override: list = None,
) -> dict:
    """
    Generate a beat-aware shot timing plan.

    Returns:
        beat_sync_plan with cuts, pacing_profile, recommended durations
    """
    sync = analyze_for_sync(audio_analysis)
    duration = sync["duration_seconds"]
    bpm = sync["bpm"]
    bar_dur = sync["bar_duration"]
    sections = sections_override or sync["section_markers"]
    mode = CUT_MODES.get(cut_mode, CUT_MODES["balanced"])

    cuts = []
    cut_time = 0.0
    shot_idx = 0

    for sec in sections:
        sec_start = sec.get("start", 0)
        sec_end = sec.get("end", duration)
        sec_type = sec.get("type", "verse")
        sec_energy = sec.get("energy", 0.5)
        pacing = SECTION_PACING.get(sec_type, SECTION_PACING["verse"])

        # Get duration range for this section
        dur_preset = pacing["duration"]
        dur_range = DURATION_PRESETS.get(dur_preset, (2.0, 3.5))

        # Adjust by cut aggressiveness
        cuts_per_bar = mode["cuts_per_bar"] * (0.7 + sec_energy * 0.6)
        if sec_type == "chorus":
            cuts_per_bar *= 1.3
        elif sec_type in ("intro", "outro"):
            cuts_per_bar *= 0.6

        # Target shot duration
        if cuts_per_bar > 0:
            target_dur = bar_dur / cuts_per_bar
        else:
            target_dur = dur_range[1]
        target_dur = max(dur_range[0], min(dur_range[1], target_dur))

        # Generate cuts within this section
        t = sec_start
        while t < sec_end - 0.3:
            # Snap to nearest bar boundary or downbeat
            best_snap = t
            best_dist = float('inf')
            for bt in sync["bar_times"]:
                dist = abs(bt - t)
                if dist < best_dist and bt >= sec_start:
                    best_dist = dist
                    best_snap = bt

            # Only snap if close enough (within half a beat)
            snap_threshold = sync["beat_duration"] * 0.5
            if best_dist < snap_threshold:
                t = best_snap

            # Determine cut reason
            reason = "beat"
            if abs(t - sec_start) < 0.1:
                reason = f"{sec_type}_start"
            elif _approx_in(t, sync["downbeats"]):
                reason = "downbeat"
            elif any(abs(t - tr["time"]) < 0.2 for tr in sync["transitions"]):
                reason = "section_transition"

            cuts.append({
                "time": round(t, 3),
                "shot_index": shot_idx,
                "duration": round(target_dur, 2),
                "section_type": sec_type,
                "reason": reason,
                "energy": round(sec_energy, 2),
            })

            shot_idx += 1
            t += target_dur

    # Filter out any cuts with negative times
    cuts = [c for c in cuts if c.get("time", 0) >= 0]

    # Determine pacing profile
    if len(sections) >= 3:
        first_energy = sections[0].get("energy", 0.3)
        mid_energy = sections[len(sections)//2].get("energy", 0.7)
        last_energy = sections[-1].get("energy", 0.3)
        if first_energy < mid_energy > last_energy:
            pacing_profile = "slow_to_fast_to_resolve"
        elif first_energy < mid_energy and mid_energy <= last_energy:
            pacing_profile = "building_intensity"
        else:
            pacing_profile = "varied"
    else:
        pacing_profile = "uniform"

    return {
        "total_duration": round(duration, 3),
        "bpm": round(bpm, 1),
        "total_cuts": len(cuts),
        "cuts": cuts,
        "pacing_profile": pacing_profile,
        "cut_mode": cut_mode,
        "sync_priority": sync_priority,
        "bar_duration": round(bar_dur, 4),
    }
