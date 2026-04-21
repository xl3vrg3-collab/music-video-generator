"""Pacing-arc analyzer (F5) — recommend per-section cut density from the music grid.

The v6 stitcher today either concatenates shots end-to-end (flat) or snaps every
cut to the nearest downbeat (beat-snap). Neither knows *how often* cuts should
happen within a section. A slow intro wants 4-bar holds; a climax wants 1-bar
or half-bar rapid cuts. Without that curve, beat-snap will faithfully lock
every shot but still feel wrong — too choppy in the quiet parts, too sluggish
in the loud parts.

This module converts `music_grid.json` (tempo, downbeats, section_boundaries)
into a pacing recommendation: for each section, a label (intro/verse/build/
chorus/climax/transition/outro), a target cut duration in seconds, and a list
of suggested cut times snapped to downbeats. Downstream consumers:

    - beat_snap restitch can honour the suggested cut count per section
    - Remotion mv-data builder can generate placeholder cuts at these times
    - Opus can be asked to pick shots that *fit* a 1-bar beat instead of 4-bar

Heuristic classification (no ML, just position + duration):
    - First section                 -> intro      (4 bars/cut)
    - Last section                  -> outro      (3 bars/cut)
    - Duration < 8s                 -> transition (1 bar/cut)
    - Mid-position, mid-length      -> build      (2 bars/cut)
    - Late-position (t_rel>0.65),
      longer than the median        -> climax     (1 bar/cut)
    - Late, shorter than median     -> chorus     (1.5 bars/cut)
    - Otherwise                     -> verse      (3 bars/cut)

Public API:
    classify_sections(section_boundaries, duration_s) -> list[dict]
    build_pacing_curve(music_grid, curve_style='arc') -> dict
    analyze_grid(grid_path, curve_style='arc') -> dict
    recommend(result) -> str
"""
from __future__ import annotations

import json
import pathlib
from typing import Iterable


LABEL_BARS_PER_CUT = {
    "intro":      4.0,
    "verse":      3.0,
    "build":      2.0,
    "chorus":     1.5,
    "climax":     1.0,
    "transition": 1.0,
    "outro":      3.0,
}

CURVE_STYLES = ("arc", "steady", "climax-heavy")


def _section_durations(boundaries: list[float], total_duration_s: float) -> list[float]:
    out: list[float] = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else total_duration_s
        out.append(max(0.0, end - start))
    return out


def classify_sections(
    section_boundaries: Iterable[float],
    total_duration_s: float,
) -> list[dict]:
    """Bucket sections by position + relative length. Pure function."""
    bounds = [float(b) for b in section_boundaries]
    if not bounds:
        return []

    durations = _section_durations(bounds, float(total_duration_s))
    n = len(bounds)
    # median of non-trivial durations (drop transitions < 8s)
    long_durations = [d for d in durations if d >= 8.0]
    median = sorted(long_durations)[len(long_durations) // 2] if long_durations else 0.0

    sections: list[dict] = []
    for i, start in enumerate(bounds):
        end = bounds[i + 1] if i + 1 < n else float(total_duration_s)
        dur = durations[i]
        mid = (start + end) / 2.0
        t_rel = (mid / total_duration_s) if total_duration_s > 0 else 0.0

        if i == 0:
            label = "intro"
        elif i == n - 1:
            label = "outro"
        elif dur < 8.0:
            label = "transition"
        elif t_rel >= 0.65 and dur >= median:
            label = "climax"
        elif t_rel >= 0.65:
            label = "chorus"
        elif t_rel >= 0.3:
            label = "build"
        else:
            label = "verse"

        sections.append({
            "index": i,
            "start_s": round(start, 3),
            "end_s": round(end, 3),
            "duration_s": round(dur, 3),
            "t_rel": round(t_rel, 3),
            "label": label,
        })
    return sections


def _propose_cut_times(
    start_s: float,
    end_s: float,
    num_cuts: int,
    downbeats: list[float],
) -> list[float]:
    """Distribute num_cuts-1 interior cut times evenly, snap to nearest downbeat.

    We return interior cuts only — the section's start boundary is already a cut
    by definition. So a section with 4 suggested cuts yields 3 interior cut
    times.
    """
    if num_cuts < 2:
        return []
    spacing = (end_s - start_s) / num_cuts
    targets = [start_s + spacing * i for i in range(1, num_cuts)]
    if not downbeats:
        return [round(t, 3) for t in targets]
    # Snap each target to the nearest downbeat that still falls inside the section
    inside = [d for d in downbeats if start_s < d < end_s]
    pool = inside or downbeats
    snapped = []
    used: set[float] = set()
    for t in targets:
        candidates = [d for d in pool if d not in used] or pool
        nearest = min(candidates, key=lambda d: abs(d - t))
        used.add(nearest)
        snapped.append(round(nearest, 3))
    return sorted(snapped)


def build_pacing_curve(
    music_grid: dict,
    curve_style: str = "arc",
) -> dict:
    """Main entry: turn a music_grid dict into a per-section pacing plan."""
    if curve_style not in CURVE_STYLES:
        curve_style = "arc"

    bpm = float(music_grid.get("tempo_bpm") or 120.0)
    duration_s = float(music_grid.get("duration_s") or 0.0)
    downbeats = [float(d) for d in (music_grid.get("downbeats") or [])]
    boundaries = [float(b) for b in (music_grid.get("section_boundaries") or [])]

    beat_s = 60.0 / bpm if bpm > 0 else 0.5
    bar_s = 4.0 * beat_s

    sections = classify_sections(boundaries, duration_s)

    total_cuts = 0
    for s in sections:
        bars_per_cut = LABEL_BARS_PER_CUT.get(s["label"], 2.0)
        # Curve style modifier
        if curve_style == "climax-heavy" and s["label"] in ("climax", "chorus"):
            bars_per_cut = max(0.5, bars_per_cut - 0.5)
        elif curve_style == "steady":
            bars_per_cut = 2.0
        target_cut_s = bars_per_cut * bar_s
        suggested_cuts = max(1, round(s["duration_s"] / target_cut_s)) if target_cut_s > 0 else 1
        cut_times = _propose_cut_times(s["start_s"], s["end_s"], suggested_cuts, downbeats)
        s["bars_per_cut"] = bars_per_cut
        s["target_cut_duration_s"] = round(target_cut_s, 3)
        s["suggested_cuts"] = suggested_cuts
        s["interior_cut_times_s"] = cut_times
        total_cuts += suggested_cuts

    return {
        "tempo_bpm": round(bpm, 3),
        "beat_s": round(beat_s, 4),
        "bar_s": round(bar_s, 3),
        "total_duration_s": round(duration_s, 3),
        "total_suggested_cuts": total_cuts,
        "curve_style": curve_style,
        "sections": sections,
        "intensity_profile": [s["label"] for s in sections],
    }


def analyze_grid(
    grid_path: str,
    curve_style: str = "arc",
) -> dict:
    """File-wrapper around build_pacing_curve."""
    grid = json.loads(pathlib.Path(grid_path).read_text(encoding="utf-8"))
    out = build_pacing_curve(grid, curve_style=curve_style)
    out["music_grid_path"] = grid_path
    return out


def recommend(result: dict) -> str:
    """One-liner describing the pacing arc."""
    total = result.get("total_suggested_cuts", 0)
    sections = result.get("sections", [])
    if not sections:
        return "no sections in music_grid — run beat-sync first"
    profile = result.get("intensity_profile", [])
    climax_ct = profile.count("climax")
    build_ct = profile.count("build")
    if total == 0:
        return "no cuts suggested"
    if climax_ct == 0 and build_ct == 0:
        return f"flat pacing — {total} cuts across {len(sections)} sections, no dynamic climax detected"
    style = result.get("curve_style", "arc")
    return (
        f"{style} pacing: {total} cuts across {len(sections)} sections "
        f"({build_ct} build, {climax_ct} climax)"
    )
