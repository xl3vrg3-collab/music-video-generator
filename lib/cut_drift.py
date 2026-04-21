"""Cut-drift reporter (F9) — measure how far stitched cuts land from the downbeat grid.

After a Remotion stitch, each scene boundary is at a specific timeline second.
This module reads a Remotion `mv-data.json` + the project's `music_grid.json`
and computes, for every interior cut, the delta to the nearest downbeat. Cuts
beyond `threshold_s` are flagged as "off-grid".

Useful diagnostic for deciding whether a MV needs beat-snap restitch. Typical
heuristic: if >30% of cuts are off-grid by >200ms, beat_snap mode pays off.

Public API:
    analyze_cut_drift(cut_times, downbeats, threshold_s=0.2) -> dict
        Pure function. Returns {total_cuts, off_grid_count, off_grid_pct,
        threshold_s, cuts:[...], off_grid_only:[...]}.

    analyze_mv(mv_data_path, music_grid_path, threshold_s=0.2) -> dict
        File-wrapper. Adds scene names to each cut record.

Integration:
    - `tools/cut_drift.py` — CLI, writes report JSON.
    - `POST /api/v6/clips/cut-drift` — batch endpoint in server.py.
    - UI button next to DRAG SCAN surfaces results.
"""
from __future__ import annotations

import json
import pathlib
from typing import Iterable


DEFAULT_THRESHOLD_S = 0.2


def analyze_cut_drift(
    cut_times: Iterable[float],
    downbeats: Iterable[float],
    threshold_s: float = DEFAULT_THRESHOLD_S,
) -> dict:
    """Compute per-cut drift vs. nearest downbeat. Interior cuts only.

    First and last cuts are skipped — they are bounded by the MV duration, not
    by an editorial choice.
    """
    cuts_list = [float(t) for t in cut_times]
    db_list = [float(d) for d in downbeats]
    if not db_list:
        return {
            "total_cuts": 0,
            "off_grid_count": 0,
            "off_grid_pct": 0.0,
            "threshold_s": threshold_s,
            "cuts": [],
            "off_grid_only": [],
            "reason": "no downbeats provided",
        }

    report: list[dict] = []
    for i, t in enumerate(cuts_list):
        if i == 0 or i == len(cuts_list) - 1:
            continue
        nearest = min(db_list, key=lambda d: abs(d - t))
        delta = t - nearest
        report.append({
            "cut_idx": i,
            "cut_time_s": round(t, 3),
            "nearest_downbeat_s": round(nearest, 3),
            "delta_s": round(delta, 3),
            "abs_delta_s": round(abs(delta), 3),
            "off_grid": abs(delta) > threshold_s,
        })

    off_grid = [r for r in report if r["off_grid"]]
    return {
        "total_cuts": len(report),
        "off_grid_count": len(off_grid),
        "off_grid_pct": round(100.0 * len(off_grid) / len(report), 1) if report else 0.0,
        "threshold_s": threshold_s,
        "max_drift_s": max((r["abs_delta_s"] for r in report), default=0.0),
        "mean_drift_s": round(
            sum(r["abs_delta_s"] for r in report) / len(report), 3
        ) if report else 0.0,
        "cuts": report,
        "off_grid_only": off_grid,
    }


def analyze_mv(
    mv_data_path: str,
    music_grid_path: str,
    threshold_s: float = DEFAULT_THRESHOLD_S,
) -> dict:
    """Load Remotion mv-data.json + music_grid.json and drift-report the cuts.

    Returns the same shape as analyze_cut_drift() plus each cut record carries a
    `scene_in` / `scene_out` pair showing which clips it sits between.
    """
    mv = json.loads(pathlib.Path(mv_data_path).read_text(encoding="utf-8"))
    grid = json.loads(pathlib.Path(music_grid_path).read_text(encoding="utf-8"))
    downbeats = grid.get("downbeats") or []

    clips = mv.get("clips") or []
    if not clips:
        return {
            "total_cuts": 0, "off_grid_count": 0, "off_grid_pct": 0.0,
            "threshold_s": threshold_s, "cuts": [], "off_grid_only": [],
            "reason": "mv-data has no clips",
        }

    cut_times = [0.0]
    for c in clips:
        cut_times.append(cut_times[-1] + float(c.get("duration", 0.0)))

    result = analyze_cut_drift(cut_times, downbeats, threshold_s)

    for r in result["cuts"]:
        i = r["cut_idx"]
        if 0 < i <= len(clips):
            r["scene_out"] = clips[i - 1].get("name", f"clip_{i - 1}")
        if i < len(clips):
            r["scene_in"] = clips[i].get("name", f"clip_{i}")
    # also copy for the off_grid_only view
    by_idx = {r["cut_idx"]: r for r in result["cuts"]}
    result["off_grid_only"] = [by_idx[r["cut_idx"]] for r in result["off_grid_only"]]

    result["mv_total_s"] = round(cut_times[-1], 3)
    result["mv_path"] = mv_data_path
    result["music_grid_path"] = music_grid_path
    result["tempo_bpm"] = grid.get("tempo_bpm")
    return result


def recommend(result: dict) -> str:
    """One-liner recommendation based on drift stats."""
    pct = result.get("off_grid_pct", 0)
    max_d = result.get("max_drift_s", 0)
    if result.get("total_cuts", 0) == 0:
        return "no interior cuts to analyze"
    if pct >= 30:
        return f"{pct}% of cuts are off-grid — beat_snap restitch will improve pacing"
    if pct >= 10:
        return f"{pct}% off-grid, max drift {max_d:.2f}s — consider snapping worst offenders"
    return f"cuts are tight ({pct}% off-grid, max {max_d:.2f}s) — no restitch needed"
