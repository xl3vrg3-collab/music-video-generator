"""
Editorial Conform System -- Post-generation timing precision for LUMN V4.

Handles the gap between Runway's integer-second outputs and the sub-second
precision needed for cinematic editing:
  - Trim clips to exact durations (0.8s insert, 2.4s medium, etc.)
  - Snap cut points to music beats for rhythmic alignment
  - Detect match-cut opportunities between consecutive shots
  - Compute J-cut / L-cut metadata for smooth transitions

No external dependencies beyond subprocess and the standard library.
FFmpeg must be available on PATH.
"""

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HANDLE_SEC = 0.5   # extra seconds to generate for trim flexibility
MIN_CLIP_DURATION = 0.5    # minimum trimmed clip duration
MAX_CLIP_DURATION = 10.0   # maximum (Runway hard limit)

# Shot-size ordering from widest to tightest.  Used by match-cut detection
# to score transitions that go wider-to-tighter (cinematic convention).
_SHOT_SIZE_ORDER = {
    "EWS": 0, "VWS": 1, "WS": 2, "MWS": 3,
    "MS": 4, "MCU": 5, "CU": 6, "ECU": 7,
}

# J/L cut default offsets (seconds)
_JCUT_OFFSET_RANGE = (0.3, 0.5)
_LCUT_OFFSET_RANGE = (0.2, 0.4)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _subprocess_kwargs() -> dict:
    """Extra kwargs for subprocess calls (hide window on Windows)."""
    kw = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kw["startupinfo"] = si
    return kw


def _nearest_beat(t: float, beats: list, tolerance: float) -> float | None:
    """Return the beat timestamp closest to *t* if within *tolerance*, else None."""
    if not beats:
        return None
    best = None
    best_dist = tolerance + 1
    for b in beats:
        d = abs(b - t)
        if d < best_dist:
            best_dist = d
            best = b
    return best if best_dist <= tolerance else None


def _shot_size_index(size: str) -> int:
    """Return numeric index for a shot-size abbreviation, or -1 if unknown."""
    if not size:
        return -1
    return _SHOT_SIZE_ORDER.get(size.upper().strip(), -1)


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


# ---------------------------------------------------------------------------
# 1. compute_trim_points
# ---------------------------------------------------------------------------

def compute_trim_points(
    shots: list,
    audio_beats: list = None,
    snap_tolerance: float = 0.3,
) -> list:
    """Compute optimal trim_in / trim_out for every shot.

    For each shot the basic trim window is ``[0, target_duration]``.  When
    *audio_beats* is supplied, the out-point is nudged up to *snap_tolerance*
    seconds so the cut lands exactly on a musical beat.

    Adds to each shot dict:
        trim_in         - float, start of usable region (seconds)
        trim_out        - float, end of usable region (seconds)
        adjusted_duration - float, actual duration after beat-snapping
        handle_before   - float, unused footage before trim_in
        handle_after    - float, unused footage after trim_out

    Returns the (mutated) shots list.
    """
    beats = audio_beats or []

    # Build a running timeline so we know absolute positions of each cut.
    abs_time = 0.0

    for shot in shots:
        target = shot.get("target_duration", shot.get("runway_duration", 5))
        runway = shot.get("runway_duration", int(target) + 1)

        # Default trim window
        trim_in = 0.0
        trim_out = float(target)

        # Try to snap trim_out to a beat
        if beats:
            abs_cut = abs_time + trim_out
            snapped = _nearest_beat(abs_cut, beats, snap_tolerance)
            if snapped is not None:
                delta = snapped - abs_cut
                proposed = trim_out + delta
                # Only accept if result stays within legal range
                if MIN_CLIP_DURATION <= proposed <= runway:
                    trim_out = round(proposed, 4)
                    logger.debug(
                        "Shot %s: snapped trim_out %.3f -> %.3f (beat at %.3f)",
                        shot.get("beat_id", "?"), target, trim_out, snapped,
                    )

        adjusted = round(trim_out - trim_in, 4)
        adjusted = _clamp(adjusted, MIN_CLIP_DURATION, min(runway, MAX_CLIP_DURATION))
        trim_out = round(trim_in + adjusted, 4)

        shot["trim_in"] = round(trim_in, 4)
        shot["trim_out"] = round(trim_out, 4)
        shot["adjusted_duration"] = round(adjusted, 4)
        shot["handle_before"] = round(trim_in, 4)
        shot["handle_after"] = round(max(0.0, runway - trim_out), 4)

        abs_time += adjusted

    return shots


# ---------------------------------------------------------------------------
# 2. trim_clip
# ---------------------------------------------------------------------------

def trim_clip(
    clip_path: str,
    output_path: str,
    trim_in: float,
    duration: float,
) -> str:
    """Trim *clip_path* using FFmpeg, writing result to *output_path*.

    Attempts a fast stream-copy first.  If that produces a broken file (can
    happen with sub-second precision on certain codecs), falls back to a
    full re-encode with libx264 CRF 18.

    Returns *output_path* on success; raises ``RuntimeError`` on failure.
    """
    if not os.path.isfile(clip_path):
        raise FileNotFoundError(f"Clip not found: {clip_path}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # --- fast path: stream copy ---
    cmd_copy = [
        "ffmpeg", "-y",
        "-ss", f"{trim_in:.4f}",
        "-i", clip_path,
        "-t", f"{duration:.4f}",
        "-c", "copy",
        "-an",
        output_path,
    ]
    try:
        subprocess.run(
            cmd_copy,
            check=True,
            capture_output=True,
            **_subprocess_kwargs(),
        )
        # Quick sanity check: output must exist and be non-empty
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            logger.debug("trim_clip: stream-copy succeeded for %s", clip_path)
            return output_path
    except subprocess.CalledProcessError:
        logger.debug("trim_clip: stream-copy failed for %s, falling back to re-encode", clip_path)

    # --- slow path: re-encode ---
    cmd_encode = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-ss", f"{trim_in:.4f}",
        "-t", f"{duration:.4f}",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-an",
        output_path,
    ]
    result = subprocess.run(
        cmd_encode,
        capture_output=True,
        **_subprocess_kwargs(),
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        raise RuntimeError(
            f"FFmpeg trim failed for {clip_path}: {stderr[:500]}"
        )

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"FFmpeg produced empty output: {output_path}")

    logger.debug("trim_clip: re-encode succeeded for %s", clip_path)
    return output_path


# ---------------------------------------------------------------------------
# 3. detect_match_cuts
# ---------------------------------------------------------------------------

def detect_match_cuts(shots: list) -> list:
    """Find consecutive shot pairs where a match cut would work.

    Criteria:
      - **Size match** (high confidence 0.8-1.0): same subject, different
        shot_size, especially wider -> tighter.
      - **Action match** (medium confidence 0.5-0.7): both shot prompts /
        descriptions reference similar action verbs.

    Returns a list of dicts::

        {
            "shot_a": <beat_id or index>,
            "shot_b": <beat_id or index>,
            "type":   "size_match" | "action_match",
            "confidence": float 0-1,
        }
    """
    results = []
    for i in range(len(shots) - 1):
        a = shots[i]
        b = shots[i + 1]

        id_a = a.get("beat_id", i)
        id_b = b.get("beat_id", i + 1)

        subj_a = (a.get("subject") or "").strip().lower()
        subj_b = (b.get("subject") or "").strip().lower()
        same_subject = subj_a and subj_b and subj_a == subj_b

        size_a = _shot_size_index(a.get("shot_size", ""))
        size_b = _shot_size_index(b.get("shot_size", ""))
        sizes_known = size_a >= 0 and size_b >= 0
        different_size = sizes_known and size_a != size_b

        # -- Size match --
        if same_subject and different_size:
            # Wider-to-tighter is the stronger cinematic convention
            if size_b > size_a:
                conf = round(_clamp(0.8 + 0.05 * (size_b - size_a), 0.8, 1.0), 2)
            else:
                conf = round(_clamp(0.8 - 0.05 * (size_a - size_b), 0.6, 0.8), 2)
            results.append({
                "shot_a": id_a,
                "shot_b": id_b,
                "type": "size_match",
                "confidence": conf,
            })
            continue  # don't double-tag

        # -- Action match --
        # Simple heuristic: check if both shots share any meaningful
        # action words in their prompt/description fields.
        desc_a = (a.get("prompt") or a.get("description") or "").lower()
        desc_b = (b.get("prompt") or b.get("description") or "").lower()
        if desc_a and desc_b:
            words_a = set(desc_a.split())
            words_b = set(desc_b.split())
            # Filter to words likely describing actions (length > 3 as proxy)
            action_a = {w for w in words_a if len(w) > 3}
            action_b = {w for w in words_b if len(w) > 3}
            overlap = action_a & action_b
            if len(overlap) >= 2:
                conf = round(_clamp(0.5 + 0.05 * len(overlap), 0.5, 0.7), 2)
                results.append({
                    "shot_a": id_a,
                    "shot_b": id_b,
                    "type": "action_match",
                    "confidence": conf,
                })

    return results


# ---------------------------------------------------------------------------
# 4. compute_jl_cuts
# ---------------------------------------------------------------------------

def compute_jl_cuts(shots: list) -> list:
    """Compute J-cut and L-cut metadata for transitions between shots.

    Rules:
      - **J-cut** (audio leads video): used at emotional transitions -- a
        section boundary where the energy or mood shifts.  Audio from the
        incoming shot starts 0.3-0.5 s before the video cut.
      - **L-cut** (audio trails video): used for action continuity -- same
        environment / same subject.  Audio from the outgoing shot continues
        0.2-0.4 s after the video cut.
      - **Hard cut**: different environment, no shared context.

    Each shot receives a ``jl_cut`` key::

        {"type": "j" | "l" | "hard", "offset_sec": float}

    Returns the (mutated) shots list.
    """
    for i, shot in enumerate(shots):
        if i == 0:
            # First shot: no incoming transition
            shot["jl_cut"] = {"type": "hard", "offset_sec": 0.0}
            continue

        prev = shots[i - 1]

        subj_prev = (prev.get("subject") or "").strip().lower()
        subj_curr = (shot.get("subject") or "").strip().lower()
        same_subject = subj_prev and subj_curr and subj_prev == subj_curr

        env_prev = (prev.get("environment") or prev.get("location") or "").strip().lower()
        env_curr = (shot.get("environment") or shot.get("location") or "").strip().lower()
        same_env = env_prev and env_curr and env_prev == env_curr

        # Detect emotional transition: energy delta or section-type change
        energy_prev = prev.get("energy", prev.get("section_energy", 0.5))
        energy_curr = shot.get("energy", shot.get("section_energy", 0.5))
        energy_delta = abs(energy_curr - energy_prev)
        section_prev = (prev.get("section_type") or "").lower()
        section_curr = (shot.get("section_type") or "").lower()
        emotional_shift = energy_delta > 0.2 or (section_prev and section_curr and section_prev != section_curr)

        if emotional_shift:
            # J-cut: audio from this shot leads the video
            offset = round(_JCUT_OFFSET_RANGE[0] + energy_delta * 0.5, 2)
            offset = _clamp(offset, _JCUT_OFFSET_RANGE[0], _JCUT_OFFSET_RANGE[1])
            shot["jl_cut"] = {"type": "j", "offset_sec": round(offset, 2)}
        elif same_subject or same_env:
            # L-cut: audio from previous shot trails into this one
            offset = round(_LCUT_OFFSET_RANGE[0] + 0.1 * int(same_subject and same_env), 2)
            offset = _clamp(offset, _LCUT_OFFSET_RANGE[0], _LCUT_OFFSET_RANGE[1])
            shot["jl_cut"] = {"type": "l", "offset_sec": round(offset, 2)}
        else:
            # Hard cut: different world entirely
            shot["jl_cut"] = {"type": "hard", "offset_sec": 0.0}

    return shots


# ---------------------------------------------------------------------------
# 5. conform_to_beat_map  (master orchestrator)
# ---------------------------------------------------------------------------

def conform_to_beat_map(shots: list, audio_analysis: dict) -> list:
    """Run the full editorial conform pass.

    Orchestrates:
      1. ``compute_trim_points`` -- beat-snapped trim windows
      2. ``detect_match_cuts``   -- match-cut opportunities
      3. ``compute_jl_cuts``     -- J/L-cut transition metadata

    Args:
        shots:          list of shot dicts (mutated in place)
        audio_analysis: dict with ``bpm``, ``beats``, ``sections``, ``duration``

    Returns:
        shots list with all conform metadata populated, plus a
        ``_match_cuts`` key injected on the *first* shot for downstream
        consumers to pick up.
    """
    beats = audio_analysis.get("beats", [])

    # 1. Trim points with beat snapping
    shots = compute_trim_points(shots, audio_beats=beats)

    # 2. Match-cut detection
    match_cuts = detect_match_cuts(shots)

    # 3. J/L-cut metadata
    shots = compute_jl_cuts(shots)

    # Attach match-cut list to the first shot for easy downstream access.
    if shots:
        shots[0]["_match_cuts"] = match_cuts

    logger.info(
        "Editorial conform complete: %d shots, %d match-cut opportunities",
        len(shots), len(match_cuts),
    )
    return shots
