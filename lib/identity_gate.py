"""
Identity Gate state tracker.

Per feedback_identity_gate.md: lock subject identity in first 3-8s via
medium/close shot before other shots involving that subject proceed. This
module tracks which characters have a "locked" anchor and gates downstream
generation.

State shape (JSON, persisted to output/pipeline/identity_gate.json):
{
  "characters": {
    "Buddy": {
      "locked": true,
      "anchor_path": "...",
      "shot_id": "s01",
      "locked_at": 1734567890.0,
      "qa_overall": 0.92,
      "qa_identity": 0.95
    }
  },
  "updated_at": 1734567890.0
}
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(PROJECT_DIR, "output", "pipeline", "identity_gate.json")

# Quality floor for auto-lock. Below this, we don't auto-lock — the user must
# either regen or manually lock.
AUTO_LOCK_OVERALL_MIN = 0.80
AUTO_LOCK_IDENTITY_MIN = 0.82


def _empty_state() -> dict[str, Any]:
    return {"characters": {}, "updated_at": 0.0}


def load_state() -> dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return _empty_state()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_state()
        data.setdefault("characters", {})
        return data
    except (OSError, json.JSONDecodeError):
        return _empty_state()


def save_state(state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["updated_at"] = time.time()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def lock_identity(
    character_name: str,
    anchor_path: str,
    shot_id: str,
    qa_overall: float = 0.0,
    qa_identity: float = 0.0,
    force: bool = False,
) -> dict[str, Any]:
    """Lock a character's identity to an anchor. Returns the entry.
    By default, does not overwrite an existing lock — pass force=True to
    replace (e.g., user manually re-locks)."""
    state = load_state()
    existing = state["characters"].get(character_name)
    if existing and existing.get("locked") and not force:
        return existing
    entry = {
        "locked": True,
        "anchor_path": anchor_path,
        "shot_id": shot_id,
        "locked_at": time.time(),
        "qa_overall": round(float(qa_overall), 3),
        "qa_identity": round(float(qa_identity), 3),
    }
    state["characters"][character_name] = entry
    save_state(state)
    return entry


def unlock_identity(character_name: str) -> bool:
    state = load_state()
    if character_name in state["characters"]:
        del state["characters"][character_name]
        save_state(state)
        return True
    return False


def is_locked(character_name: str) -> bool:
    state = load_state()
    entry = state["characters"].get(character_name)
    return bool(entry and entry.get("locked"))


def get_locked(character_name: str) -> dict[str, Any] | None:
    state = load_state()
    return state["characters"].get(character_name)


def check_gate(character_names: list[str]) -> dict[str, Any]:
    """Return which of the provided characters are unlocked.
    Result:
      {
        "all_locked": bool,
        "locked": ["Buddy"],
        "unlocked": ["Owen"],
        "details": { "Buddy": {...entry...}, ... }
      }
    """
    state = load_state()
    locked: list[str] = []
    unlocked: list[str] = []
    details: dict[str, Any] = {}
    for name in character_names:
        entry = state["characters"].get(name)
        if entry and entry.get("locked"):
            locked.append(name)
            details[name] = entry
        else:
            unlocked.append(name)
    return {
        "all_locked": len(unlocked) == 0,
        "locked": locked,
        "unlocked": unlocked,
        "details": details,
    }


def maybe_auto_lock(
    character_names: list[str],
    anchor_path: str,
    shot_id: str,
    qa_report: dict[str, Any] | None,
) -> list[str]:
    """After a successful anchor gen + QA, auto-lock any unlocked characters
    whose scores clear the floor. Returns the list of characters we locked."""
    if not character_names or not anchor_path or not qa_report:
        return []
    # Only single-subject anchors auto-lock — multi-subject shots are too
    # ambiguous for the identity gate (which character is the "lock" for?).
    if len(character_names) != 1:
        return []
    pick_label = (qa_report.get("pick") or "").strip().upper()
    cand = (qa_report.get("candidates") or {}).get(pick_label, {})
    overall = float(cand.get("overall", 0) or 0)
    identity = float(cand.get("identity", 0) or 0)
    if overall < AUTO_LOCK_OVERALL_MIN or identity < AUTO_LOCK_IDENTITY_MIN:
        return []
    locked_now: list[str] = []
    for name in character_names:
        if not is_locked(name):
            lock_identity(
                character_name=name,
                anchor_path=anchor_path,
                shot_id=shot_id,
                qa_overall=overall,
                qa_identity=identity,
            )
            locked_now.append(name)
    return locked_now
