"""
V4 Shot Validation System for LUMN Movie Studio.

Enforces cinematic grammar rules at the shot level to prevent the "slideshow"
look that plagued V3.  Three validators cover camera diversity, screen direction
(the 180-degree rule applied to subject movement), and character asset binding.

All validators are non-destructive by default: they return violation reports and
only mutate shot dicts when an auto-fix is applied.  ``validate_all`` is the
single entry-point used by the Auto Director pipeline.

References
----------
- cinematic_engine.py line 721 — 180-degree camera *movement* check
  (pan_left vs pan_right).  ``validate_screen_direction`` extends this to
  *subject* screen direction (L2R / R2L).
- coverage_system.py — COVERAGE_MODES, ROLE_TO_SHOT definitions.
- V4 Shot Architecture (10-pillar plan) — pillar 4 (camera diversity) and
  pillar 6 (character continuity).

No external dependencies beyond the standard library.
"""

from __future__ import annotations

import difflib
from collections import Counter
from typing import Any

# ---------------------------------------------------------------------------
# Shot-size taxonomy
# ---------------------------------------------------------------------------

WIDE_SIZES = {"EWS", "WS"}
MEDIUM_SIZES = {"MS", "MCU", "OTS"}
CLOSE_SIZES = {"CU", "ECU", "INSERT"}
SPECIAL_SIZES = {"POV"}  # POV counts as close for diversity checks

ALL_SIZES = WIDE_SIZES | MEDIUM_SIZES | CLOSE_SIZES | SPECIAL_SIZES

VALID_SCREEN_DIRECTIONS = {"L2R", "R2L", "neutral"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _size_category(shot_size: str) -> str | None:
    """Return 'wide', 'medium', or 'close' for a given shot_size string."""
    s = (shot_size or "").upper().strip()
    if s in WIDE_SIZES:
        return "wide"
    if s in MEDIUM_SIZES:
        return "medium"
    if s in CLOSE_SIZES or s in SPECIAL_SIZES:
        return "close"
    return None


def _pick_most_needed_size(counter: Counter, exclude: str) -> str:
    """Choose a representative shot size from the least-used category.

    *counter* maps category -> count.  *exclude* is the category to avoid
    (since we want to break a streak of that category).
    """
    candidates = {
        "wide": "WS",
        "medium": "MS",
        "close": "CU",
    }
    # Sort categories by count ascending; pick the first that isn't *exclude*.
    for cat, _ in sorted(counter.items(), key=lambda x: x[1]):
        if cat != exclude:
            return candidates[cat]
    # Fallback: just return the opposite of exclude.
    return candidates.get({"wide": "medium", "medium": "close", "close": "wide"}.get(exclude, "medium"), "MS")


def _violation(
    shot_id: str,
    rule: str,
    severity: str = "warning",
    auto_fixed: bool = False,
    old_value: str = "",
    new_value: str = "",
) -> dict[str, Any]:
    return {
        "shot_id": shot_id,
        "rule": rule,
        "severity": severity,
        "auto_fixed": auto_fixed,
        "old_value": old_value,
        "new_value": new_value,
    }


# ---------------------------------------------------------------------------
# 1. Camera Diversity Validator
# ---------------------------------------------------------------------------


def validate_camera_diversity(shots: list[dict]) -> list[dict]:
    """Check and auto-fix camera diversity violations.

    Rules
    -----
    A. No 3 consecutive shots with the same shot_size category.
       Auto-fix: swap the middle shot to the most-needed size.
    B. Each beat (group of shots sharing ``beat_id``) must contain at least one
       wide, one medium, and one close shot.
    C. No 2 consecutive shots with the same camera movement.
       Auto-fix: swap the second shot's movement to ``"static"``.

    Parameters
    ----------
    shots : list[dict]
        Ordered list of shot dicts.  Each shot is expected to have at minimum:
        ``id`` (or ``shot_id``), ``shot_size``, ``beat_id``,
        and ``camera.movement`` (or top-level ``movement``).

    Returns
    -------
    list[dict]
        Violation dicts with keys: shot_id, rule, severity, auto_fixed,
        old_value, new_value.
    """
    violations: list[dict] = []
    if not shots:
        return violations

    # --- Helper to read fields consistently ---
    def _sid(s: dict) -> str:
        return s.get("shot_id") or s.get("id") or ""

    def _size(s: dict) -> str:
        return (s.get("shot_size") or "").upper().strip()

    def _movement(s: dict) -> str:
        cam = s.get("camera") or {}
        return (cam.get("movement") or s.get("movement") or "").lower().strip()

    # Build a running category counter for most-needed calculation.
    cat_counter: Counter = Counter({"wide": 0, "medium": 0, "close": 0})
    for s in shots:
        cat = _size_category(_size(s))
        if cat:
            cat_counter[cat] += 1

    # --- Rule A: No 3 consecutive same-category sizes ---
    for i in range(1, len(shots) - 1):
        prev_cat = _size_category(_size(shots[i - 1]))
        cur_cat = _size_category(_size(shots[i]))
        next_cat = _size_category(_size(shots[i + 1]))

        if prev_cat and cur_cat and next_cat and prev_cat == cur_cat == next_cat:
            old_size = _size(shots[i])
            new_size = _pick_most_needed_size(cat_counter, cur_cat)

            # Auto-fix: update the middle shot.
            if cat_counter[cur_cat] > 0:
                cat_counter[cur_cat] -= 1
            new_cat = _size_category(new_size)
            if new_cat:
                cat_counter[new_cat] += 1

            shots[i]["shot_size"] = new_size
            violations.append(
                _violation(
                    _sid(shots[i]),
                    "3_consecutive_same_size",
                    severity="warning",
                    auto_fixed=True,
                    old_value=old_size,
                    new_value=new_size,
                )
            )

    # --- Rule B: Beat completeness ---
    beats: dict[str, list[dict]] = {}
    for s in shots:
        bid = s.get("beat_id")
        if bid is not None:
            beats.setdefault(bid, []).append(s)

    for bid, beat_shots in beats.items():
        cats_present = {_size_category(_size(s)) for s in beat_shots} - {None}
        for required_cat in ("wide", "medium", "close"):
            if required_cat not in cats_present:
                # Report on first shot in the beat.
                violations.append(
                    _violation(
                        _sid(beat_shots[0]),
                        f"beat_missing_{required_cat}",
                        severity="error",
                        auto_fixed=False,
                        old_value=f"beat {bid}",
                        new_value=f"needs {required_cat} shot",
                    )
                )

    # --- Rule C: No 2 consecutive same movement ---
    for i in range(1, len(shots)):
        prev_mov = _movement(shots[i - 1])
        cur_mov = _movement(shots[i])
        if prev_mov and cur_mov and prev_mov == cur_mov and cur_mov != "static":
            old_mov = cur_mov
            # Auto-fix: set to static.
            cam = shots[i].setdefault("camera", {})
            cam["movement"] = "static"
            if "movement" in shots[i] and not shots[i].get("camera"):
                shots[i]["movement"] = "static"

            violations.append(
                _violation(
                    _sid(shots[i]),
                    "consecutive_same_movement",
                    severity="warning",
                    auto_fixed=True,
                    old_value=old_mov,
                    new_value="static",
                )
            )

    return violations


# ---------------------------------------------------------------------------
# 2. Screen Direction Validator
# ---------------------------------------------------------------------------


def validate_screen_direction(shots: list[dict]) -> list[dict]:
    """Enforce the 180-degree rule for *subject* screen direction.

    This extends the camera-movement check in ``cinematic_engine.py`` (line 721)
    to cover **subject movement direction** (L2R / R2L), which is the
    foundational continuity rule in professional editing.

    Rules
    -----
    A. If shot N has subject X moving L2R, the next shot featuring the same
       subject must also be L2R -- unless a neutral "axis reset" shot
       intervenes.
    B. Over-the-shoulder (OTS) shots: a character's frame position
       (``frame_position``: "frame-left" / "frame-right") must remain
       consistent for the same character across all shots.
    C. Auto-fix: flip ``screen_direction`` OR suggest inserting a neutral reset
       shot.

    Parameters
    ----------
    shots : list[dict]
        Ordered shot list.  Relevant fields per shot:
        - ``subject`` (str) — the character/object in frame
        - ``screen_direction`` — "L2R", "R2L", or "neutral"
        - ``shot_size`` — used to detect OTS
        - ``frame_position`` — "frame-left" or "frame-right" (OTS shots)

    Returns
    -------
    list[dict]
        Violation / auto-fix reports.
    """
    violations: list[dict] = []
    if not shots:
        return violations

    def _sid(s: dict) -> str:
        return s.get("shot_id") or s.get("id") or ""

    def _subject(s: dict) -> str:
        return (s.get("subject") or "").strip().lower()

    def _direction(s: dict) -> str:
        return (s.get("screen_direction") or "").strip().upper()

    def _is_ots(s: dict) -> bool:
        return (s.get("shot_size") or "").upper().strip() == "OTS"

    def _frame_pos(s: dict) -> str:
        return (s.get("frame_position") or "").strip().lower()

    # --- Rule A: Subject direction continuity ---
    # Track last-known direction per subject.
    last_direction: dict[str, str] = {}  # subject -> "L2R" | "R2L"
    last_direction_shot: dict[str, int] = {}  # subject -> index of that shot
    neutral_since: dict[str, bool] = {}  # subject -> True if a neutral shot appeared since last directional shot

    for i, shot in enumerate(shots):
        subj = _subject(shot)
        if not subj:
            continue
        direction = _direction(shot)

        if direction == "NEUTRAL":
            neutral_since[subj] = True
            continue

        if direction not in ("L2R", "R2L"):
            continue

        if subj in last_direction:
            prev_dir = last_direction[subj]
            reset_happened = neutral_since.get(subj, False)

            if direction != prev_dir and not reset_happened:
                # Violation: direction flipped without a neutral reset.
                old_dir = direction
                new_dir = prev_dir  # auto-fix: flip back to match.
                shot["screen_direction"] = new_dir

                violations.append(
                    _violation(
                        _sid(shot),
                        "screen_direction_flip",
                        severity="warning",
                        auto_fixed=True,
                        old_value=old_dir,
                        new_value=new_dir,
                    )
                )
                # Also suggest a neutral insert.
                violations.append(
                    _violation(
                        _sid(shot),
                        "suggest_neutral_reset",
                        severity="warning",
                        auto_fixed=False,
                        old_value=f"between shots {last_direction_shot.get(subj, '?')} and {i}",
                        new_value="insert neutral axis-reset shot",
                    )
                )
                # Keep the corrected direction as the new baseline.
                direction = new_dir

        last_direction[subj] = direction
        last_direction_shot[subj] = i
        neutral_since[subj] = False

    # --- Rule B: OTS frame-position consistency ---
    char_frame_pos: dict[str, str] = {}  # subject -> "frame-left" | "frame-right"

    for shot in shots:
        if not _is_ots(shot):
            continue
        subj = _subject(shot)
        if not subj:
            continue
        fp = _frame_pos(shot)
        if not fp:
            continue

        if subj in char_frame_pos:
            expected = char_frame_pos[subj]
            if fp != expected:
                old_fp = fp
                shot["frame_position"] = expected
                violations.append(
                    _violation(
                        _sid(shot),
                        "ots_frame_position_flip",
                        severity="warning",
                        auto_fixed=True,
                        old_value=old_fp,
                        new_value=expected,
                    )
                )
        else:
            char_frame_pos[subj] = fp

    return violations


# ---------------------------------------------------------------------------
# 3. Character Binding Validator
# ---------------------------------------------------------------------------


def _fuzzy_match_character(
    subject: str,
    characters: list[dict],
    threshold: float = 0.6,
) -> dict | None:
    """Try to match *subject* text to a character entry.

    Strategy (in order):
    1. Case-insensitive exact match on ``name``.
    2. Case-insensitive substring match on ``name`` or any alias.
    3. ``difflib.SequenceMatcher`` ratio >= *threshold* against name/aliases.

    Returns the matched character dict or ``None``.
    """
    subj_lower = subject.strip().lower()
    if not subj_lower:
        return None

    # Pass 1: exact name match.
    for char in characters:
        if (char.get("name") or "").strip().lower() == subj_lower:
            return char

    # Pass 2: substring match (name or alias appears in subject, or vice-versa).
    for char in characters:
        name_lower = (char.get("name") or "").strip().lower()
        aliases = [a.strip().lower() for a in (char.get("aliases") or [])]
        all_names = [name_lower] + aliases

        for n in all_names:
            if not n:
                continue
            if n in subj_lower or subj_lower in n:
                return char

    # Pass 3: fuzzy ratio.
    best_score = 0.0
    best_char = None
    for char in characters:
        name_lower = (char.get("name") or "").strip().lower()
        aliases = [a.strip().lower() for a in (char.get("aliases") or [])]
        all_names = [name_lower] + aliases

        for n in all_names:
            if not n:
                continue
            score = difflib.SequenceMatcher(None, subj_lower, n).ratio()
            if score > best_score:
                best_score = score
                best_char = char

    if best_score >= threshold:
        return best_char

    return None


def validate_character_binding(
    shots: list[dict],
    characters: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Bind each shot's ``subject`` to a character asset.  **Hard-fail validator.**

    For every shot with a non-empty ``subject`` field this function attempts a
    fuzzy match against the *characters* list.  On success the shot dict is
    **mutated in place** to carry ``characterId`` and ``character_photo_path``.
    On failure the shot is added to the *failures* list which blocks generation.

    Parameters
    ----------
    shots : list[dict]
        Shot dicts.  Must have ``subject`` (str).
    characters : list[dict]
        Character assets.  Each entry:
        ``{"id", "name", "aliases": [...], "isCharacterSheet": bool, "photo_path": str}``

    Returns
    -------
    failures : list[dict]
        ``{"shot_id", "subject", "message"}`` for unmatched subjects.
    warnings : list[dict]
        ``{"shot_id", "message"}`` for non-critical notes (e.g. missing photo).
    """
    failures: list[dict] = []
    warnings: list[dict] = []

    if not characters:
        # If no characters provided, every shot with a subject is a failure.
        for s in shots:
            subj = (s.get("subject") or "").strip()
            sid = s.get("shot_id") or s.get("id") or ""
            if subj:
                failures.append({
                    "shot_id": sid,
                    "subject": subj,
                    "message": f"No character list provided; cannot bind subject '{subj}'.",
                })
        return failures, warnings

    for shot in shots:
        subj = (shot.get("subject") or "").strip()
        sid = shot.get("shot_id") or shot.get("id") or ""

        if not subj:
            continue

        matched = _fuzzy_match_character(subj, characters)

        if matched is None:
            failures.append({
                "shot_id": sid,
                "subject": subj,
                "message": f"Subject '{subj}' does not match any character or alias.",
            })
            continue

        # Bind character data onto the shot.
        shot["characterId"] = matched.get("id", "")
        shot["character_photo_path"] = matched.get("photo_path", "")

        # Warn if photo missing.
        if not matched.get("photo_path"):
            warnings.append({
                "shot_id": sid,
                "message": (
                    f"Character '{matched.get('name')}' matched for subject "
                    f"'{subj}' but has no photo_path."
                ),
            })

    return failures, warnings


# ---------------------------------------------------------------------------
# 4. Convenience: Run all validators
# ---------------------------------------------------------------------------


def validate_all(
    shots: list[dict],
    characters: list[dict] | None = None,
) -> dict[str, Any]:
    """Run every validator and return a unified report.

    Parameters
    ----------
    shots : list[dict]
        The full ordered shot list for a project / sequence.
    characters : list[dict] | None
        Character asset list.  If ``None``, the character binding validator is
        skipped (no failures reported for that stage).

    Returns
    -------
    dict
        {
            "camera_diversity": [violations],
            "screen_direction": [violations],
            "character_binding": {"failures": [...], "warnings": [...]},
            "is_blocked": bool,       # True if any character binding failures
            "total_violations": int,   # Sum across all validators
            "auto_fixed": int,         # How many were auto-corrected
        }
    """
    cam_violations = validate_camera_diversity(shots)
    dir_violations = validate_screen_direction(shots)

    if characters is not None:
        char_failures, char_warnings = validate_character_binding(shots, characters)
    else:
        char_failures, char_warnings = [], []

    all_violations = cam_violations + dir_violations
    total = len(all_violations) + len(char_failures) + len(char_warnings)
    auto_fixed = sum(1 for v in all_violations if v.get("auto_fixed"))

    return {
        "camera_diversity": cam_violations,
        "screen_direction": dir_violations,
        "character_binding": {
            "failures": char_failures,
            "warnings": char_warnings,
        },
        "is_blocked": len(char_failures) > 0,
        "total_violations": total,
        "auto_fixed": auto_fixed,
    }
