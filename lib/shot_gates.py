"""Per-shot gate state for the LUMN v6 pipeline.

Each shot flows through four gates:
  1. anchor_generated  — selected.png exists
  2. audit_passed      — lib.anchor_auditor said pass=true
  3. clip_rendered     — selected.mp4 exists
  4. signed_off        — explicit human "mark ready" or Sonnet sign-off

The stitcher consumes only signed-off shots (falls back to all if zero
signed off, so the system stays usable when gates aren't filled in yet).

State is persisted as output/projects/<slug>/shots/shot_gates.json so
the UI can show gate status without re-running expensive audits, and so
batch scripts can query/update the same source of truth.

Section mapping comes from lib.song_timing.load_timing: each shot gets
a section / bar_start / bar_end / lyric_line stamp derived from its
position on the timeline. "Position" is taken from project.json shots
(cumulative duration in render order) unless per-shot start/end fields
already exist.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

GATE_FILENAME = "shot_gates.json"
ANCHOR_PASS_THRESHOLD = 1.0  # auditor returns pass=bool; we require True


def gates_path(project_dir: str) -> str:
    return os.path.join(project_dir, "shots", GATE_FILENAME)


def _default_gate(shot_id: str, section_info: dict | None = None) -> dict:
    gate = {
        "shot_id": shot_id,
        "section": (section_info or {}).get("label"),
        "section_index": (section_info or {}).get("index"),
        "start_sec": (section_info or {}).get("start_sec"),
        "end_sec": (section_info or {}).get("end_sec"),
        "bar_start": (section_info or {}).get("bar_start"),
        "bar_end": (section_info or {}).get("bar_end"),
        "lyric_line_idx": (section_info or {}).get("lyric_line_idx"),
        "lyric_text": (section_info or {}).get("lyric_text"),
        "gates": {
            "anchor_generated": False,
            "audit_passed": None,          # None = not yet audited; True/False after run
            "audit_violations": [],
            "audit_summary": "",
            "audit_run_at": None,
            "clip_rendered": False,
            "motion_review_passed": None,  # None = not yet reviewed
            "motion_review_notes": "",
            "signed_off": False,
            "signed_off_by": None,
            "signed_off_at": None,
        },
    }
    return gate


def load_gates(project_dir: str) -> dict:
    path = gates_path(project_dir)
    if not os.path.isfile(path):
        return {"shots": {}, "updated_at": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"shots": {}, "updated_at": None}


def save_gates(project_dir: str, state: dict) -> str:
    path = gates_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["updated_at"] = time.time()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Section mapping from song timing
# ---------------------------------------------------------------------------

def _bar_containing(bars: list[dict], t: float) -> int | None:
    for b in bars:
        if b["start"] <= t < b["end"]:
            return int(b.get("index", 0))
    return None


def _section_containing(sections: list[dict], t: float) -> dict | None:
    for s in sections:
        if s["start"] <= t < s["end"]:
            return s
    return sections[-1] if sections else None


LYRIC_INTRO_LOOKAHEAD_SEC = 2.0  # shot can "intro" a lyric dropping within this window


def _lyric_at(lines: list[dict], start: float, end: float) -> tuple[int | None, str]:
    """Find the lyric line that belongs to the shot at [start, end).

    Priority:
      1. Any lyric whose timespan overlaps the shot's timespan.
      2. A lyric whose start falls within LYRIC_INTRO_LOOKAHEAD_SEC after the
         shot ends — the shot visually introduces the incoming line.
      3. Otherwise None (the shot is in an instrumental / lyric-free stretch).

    The previous version returned *any* future line regardless of distance,
    which tagged every instrumental shot with whatever the next vocal was,
    even if that was two minutes away.
    """
    best_idx: int | None = None
    best_text = ""
    best_overlap = 0.0
    for i, ln in enumerate(lines):
        l_start = ln.get("start", 0.0)
        l_end = ln.get("end", l_start)
        overlap = min(end, l_end) - max(start, l_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_idx = i
            best_text = ln.get("text", "")
    if best_idx is not None:
        return best_idx, best_text
    for i, ln in enumerate(lines):
        l_start = ln.get("start", 0.0)
        if end <= l_start <= end + LYRIC_INTRO_LOOKAHEAD_SEC:
            return i, ln.get("text", "")
    return None, ""


def map_shots_to_timing(shots: list[dict], timing: dict) -> list[dict]:
    """Given project.json shots (in render order) and timing.json, return
    per-shot section metadata keyed by shot_id.

    Shots are placed on the timeline by cumulative duration — shot 0 starts
    at t=0, shot 1 starts at t=shot_0.duration, etc. If a shot has its own
    start_sec/end_sec already, those win.
    """
    sections = timing.get("sections") or []
    bars = timing.get("bars") or []
    lines = (timing.get("lyrics") or {}).get("lines") or []

    cursor = 0.0
    out = []
    for shot in shots:
        sid = shot.get("shot_id") or shot.get("id")
        if not sid:
            continue
        start = shot.get("start_sec")
        end = shot.get("end_sec")
        if start is None or end is None:
            dur = float(shot.get("duration") or shot.get("duration_sec") or 5.0)
            start, end = cursor, cursor + dur
            cursor = end

        sec = _section_containing(sections, start) or {}
        bar_start = _bar_containing(bars, start)
        bar_end = _bar_containing(bars, max(start, end - 0.01))
        lyric_idx, lyric_text = _lyric_at(lines, start, end)

        out.append({
            "shot_id": sid,
            "label": sec.get("label") or "?",
            "index": sec.get("index"),
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "bar_start": bar_start,
            "bar_end": bar_end,
            "lyric_line_idx": lyric_idx,
            "lyric_text": lyric_text,
        })
    return out


# ---------------------------------------------------------------------------
# Reconcile on-disk reality with gate state
# ---------------------------------------------------------------------------

def sync_gates_with_disk(project_dir: str, shots: list[dict],
                         anchors_dir: str, clips_dir: str,
                         timing: dict | None = None) -> dict:
    """Merge current disk state + song-timing section mapping into
    shot_gates.json. Preserves human signoff / audit results when present.
    """
    state = load_gates(project_dir)
    shots_map = state.setdefault("shots", {})

    section_map = {}
    if timing:
        for info in map_shots_to_timing(shots, timing):
            section_map[info["shot_id"]] = info

    for shot in shots:
        sid = shot.get("shot_id") or shot.get("id")
        if not sid:
            continue
        section_info = section_map.get(sid)
        existing = shots_map.get(sid) or _default_gate(sid, section_info)

        # Refresh section stamp every sync (cheap & keeps labels fresh)
        if section_info:
            for k in ("section", "section_index", "start_sec", "end_sec",
                      "bar_start", "bar_end", "lyric_line_idx", "lyric_text"):
                # shot_gate uses "section" while section_info uses "label"
                src_key = {"section": "label", "section_index": "index"}.get(k, k)
                existing[k] = section_info.get(src_key, existing.get(k))

        # Disk reality
        sub_anchor = os.path.join(anchors_dir, sid, "selected.png")
        sub_clip = os.path.join(clips_dir, sid, "selected.mp4")
        existing["gates"]["anchor_generated"] = os.path.isfile(sub_anchor)
        existing["gates"]["clip_rendered"] = os.path.isfile(sub_clip)

        shots_map[sid] = existing

    # Drop entries for shots that are no longer in project.json so stale
    # state can't sneak through the gates into the stitcher.
    valid_ids = {s.get("shot_id") or s.get("id") for s in shots}
    for sid in list(shots_map.keys()):
        if sid not in valid_ids:
            del shots_map[sid]

    save_gates(project_dir, state)
    return state


def apply_audit_result(project_dir: str, shot_id: str, audit: dict) -> dict:
    state = load_gates(project_dir)
    shot = state.setdefault("shots", {}).setdefault(shot_id, _default_gate(shot_id))
    g = shot["gates"]
    g["audit_passed"] = bool(audit.get("pass"))
    g["audit_violations"] = audit.get("violations") or []
    g["audit_summary"] = audit.get("summary") or ""
    g["audit_run_at"] = time.time()
    save_gates(project_dir, state)
    return shot


def set_motion_review(project_dir: str, shot_id: str, passed: bool,
                      notes: str = "") -> dict:
    state = load_gates(project_dir)
    shot = state.setdefault("shots", {}).setdefault(shot_id, _default_gate(shot_id))
    g = shot["gates"]
    g["motion_review_passed"] = bool(passed)
    g["motion_review_notes"] = notes or ""
    save_gates(project_dir, state)
    return shot


def set_signoff(project_dir: str, shot_id: str, signed_off: bool,
                actor: str = "human") -> dict:
    state = load_gates(project_dir)
    shot = state.setdefault("shots", {}).setdefault(shot_id, _default_gate(shot_id))
    g = shot["gates"]
    g["signed_off"] = bool(signed_off)
    g["signed_off_by"] = actor if signed_off else None
    g["signed_off_at"] = time.time() if signed_off else None
    save_gates(project_dir, state)
    return shot


def gate_summary(state: dict) -> dict:
    """High-level counters for the batch action bar."""
    shots = (state or {}).get("shots") or {}
    total = len(shots)
    counts = {
        "total": total,
        "anchor_generated": 0,
        "audit_passed": 0,
        "audit_failed": 0,
        "audit_pending": 0,
        "clip_rendered": 0,
        "motion_passed": 0,
        "motion_failed": 0,
        "motion_pending": 0,
        "signed_off": 0,
    }
    for s in shots.values():
        g = s.get("gates", {})
        if g.get("anchor_generated"):
            counts["anchor_generated"] += 1
        if g.get("audit_passed") is True:
            counts["audit_passed"] += 1
        elif g.get("audit_passed") is False:
            counts["audit_failed"] += 1
        else:
            counts["audit_pending"] += 1
        if g.get("clip_rendered"):
            counts["clip_rendered"] += 1
        if g.get("motion_review_passed") is True:
            counts["motion_passed"] += 1
        elif g.get("motion_review_passed") is False:
            counts["motion_failed"] += 1
        else:
            counts["motion_pending"] += 1
        if g.get("signed_off"):
            counts["signed_off"] += 1
    return counts


def signed_off_shot_ids(project_dir: str) -> list[str]:
    state = load_gates(project_dir)
    return [sid for sid, s in (state.get("shots") or {}).items()
            if (s.get("gates") or {}).get("signed_off")]
