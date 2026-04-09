"""
Pipeline State Machine (V5 Pipeline)

File-backed state machine for the unified production pipeline.
Tracks progress from master prompt through asset generation, anchor
composition, video generation, and final conform.
"""

import json
import os
import time

# ── Pipeline states in order ──
PIPELINE_STATES = [
    "IDLE",
    "PROMPT_RECEIVED",
    "ASSETS_EXTRACTED",
    "PACKAGES_CREATED",
    "SHEETS_GENERATING",
    "SHEETS_REVIEW",
    "PLAN_READY",
    "ANCHORS_GENERATING",
    "ANCHORS_REVIEW",
    "SHOTS_GENERATING",
    "CONFORM",
    "COMPLETE",
    "ERROR",
]

# States that require user approval before advancing (skipped in auto_advance)
APPROVAL_GATES = {"SHEETS_REVIEW", "ANCHORS_REVIEW"}

# What must be true to advance FROM each state
_ADVANCE_REQUIREMENTS = {
    "IDLE":               lambda s: bool(s.master_prompt),
    "PROMPT_RECEIVED":    lambda s: bool(s.extraction),
    "ASSETS_EXTRACTED":   lambda s: len(s.packages) > 0,
    "PACKAGES_CREATED":   lambda s: True,  # can start generating immediately
    "SHEETS_GENERATING":  lambda s: s._all_sheets_done(),
    "SHEETS_REVIEW":      lambda s: s.auto_advance or s._all_sheets_approved(),
    "PLAN_READY":         lambda s: bool(s.plan and s.plan.get("scenes")),
    "ANCHORS_GENERATING": lambda s: s._all_anchors_done(),
    "ANCHORS_REVIEW":     lambda s: s.auto_advance or s._all_anchors_approved(),
    "SHOTS_GENERATING":   lambda s: s._all_shots_done(),
    "CONFORM":            lambda s: bool(s.output_file),
}


class PipelineState:
    """
    Persistent state machine for the V5 unified production pipeline.

    Saves to output/pipeline_state.json. Each stage is resumable.
    """

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self._state_file = os.path.join(output_dir, "pipeline_state.json")

        # Core state
        self.state = "IDLE"
        self.auto_advance = False
        self.master_prompt = ""
        self.song_path = ""
        self.engine = "gen4_5"
        self.mode = "fast"       # fast or production
        self.story_model = None

        # Pipeline data
        self.extraction = {}     # output of extract_production_data()
        self.packages = []       # package_ids created
        self.plan = {}           # V4 beat/shot plan
        self.anchors = {}        # shot_id -> anchor dict
        self.output_file = ""    # final video path

        # Tracking
        self.errors = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.state_history = []  # list of {state, timestamp}

        # Load if exists
        if os.path.isfile(self._state_file):
            self.load()

    def advance(self, target: str = None) -> str:
        """
        Advance to the next state (or a specific target state).

        Args:
            target: Specific state to advance to, or None for next in sequence.

        Returns:
            The new state string.

        Raises:
            ValueError: If the transition is invalid or prerequisites not met.
        """
        if target:
            if target not in PIPELINE_STATES:
                raise ValueError(f"Unknown state: {target}")
            if target == "ERROR":
                self._set_state(target)
                return target
        else:
            idx = PIPELINE_STATES.index(self.state)
            if idx >= len(PIPELINE_STATES) - 2:  # COMPLETE or ERROR
                raise ValueError(f"Cannot advance from {self.state}")
            target = PIPELINE_STATES[idx + 1]

        # Check prerequisites for current state
        req_fn = _ADVANCE_REQUIREMENTS.get(self.state)
        if req_fn and not req_fn(self):
            raise ValueError(
                f"Cannot advance from {self.state}: prerequisites not met"
            )

        # Skip approval gates in auto_advance mode
        if target in APPROVAL_GATES and self.auto_advance:
            idx = PIPELINE_STATES.index(target)
            if idx + 1 < len(PIPELINE_STATES):
                target = PIPELINE_STATES[idx + 1]

        self._set_state(target)
        return target

    def can_advance(self) -> bool:
        """Check if all prerequisites for the next state are met."""
        req_fn = _ADVANCE_REQUIREMENTS.get(self.state)
        if req_fn:
            return req_fn(self)
        return self.state not in ("COMPLETE", "ERROR")

    def set_error(self, error_msg: str):
        """Record an error and transition to ERROR state."""
        self.errors.append({
            "message": error_msg,
            "at_state": self.state,
            "timestamp": time.time(),
        })
        self._set_state("ERROR")

    def reset_to(self, state: str):
        """
        Reset pipeline to a specific state for re-running.
        Clears data that would be regenerated from that state forward.
        """
        if state not in PIPELINE_STATES:
            raise ValueError(f"Unknown state: {state}")

        idx = PIPELINE_STATES.index(state)

        # Clear data from later stages
        if idx <= PIPELINE_STATES.index("ASSETS_EXTRACTED"):
            self.packages = []
        if idx <= PIPELINE_STATES.index("PLAN_READY"):
            self.plan = {}
            self.anchors = {}
        if idx <= PIPELINE_STATES.index("ANCHORS_GENERATING"):
            self.anchors = {}
        if idx <= PIPELINE_STATES.index("SHOTS_GENERATING"):
            self.output_file = ""

        self._set_state(state)

    def get_progress(self) -> dict:
        """Return a UI-friendly progress summary."""
        state_idx = PIPELINE_STATES.index(self.state)
        total = len(PIPELINE_STATES) - 2  # exclude IDLE and ERROR
        progress_pct = max(0, min(100, int((state_idx / total) * 100)))

        return {
            "state": self.state,
            "state_index": state_idx,
            "total_states": total,
            "progress_percent": progress_pct,
            "can_advance": self.can_advance(),
            "auto_advance": self.auto_advance,
            "is_approval_gate": self.state in APPROVAL_GATES,
            "num_packages": len(self.packages),
            "num_anchors": len(self.anchors),
            "num_shots": len(self.plan.get("scenes", [])),
            "num_errors": len(self.errors),
            "has_output": bool(self.output_file),
            "updated_at": self.updated_at,
        }

    # ── Anchor tracking ──

    def set_anchor(self, shot_id: str, anchor_data: dict):
        """Record an anchor for a shot."""
        self.anchors[shot_id] = anchor_data
        self.save()

    def get_anchor(self, shot_id: str) -> dict:
        """Get anchor data for a shot."""
        return self.anchors.get(shot_id, {})

    def approve_anchor(self, shot_id: str):
        """Approve an anchor."""
        if shot_id in self.anchors:
            self.anchors[shot_id]["status"] = "approved"
            self.save()

    def reject_anchor(self, shot_id: str, reason: str = ""):
        """Reject an anchor with optional reason."""
        if shot_id in self.anchors:
            self.anchors[shot_id]["status"] = "rejected"
            self.anchors[shot_id].setdefault("rejection_notes", []).append(reason)
            self.save()

    # ── Persistence ──

    def save(self):
        """Save state to JSON file."""
        os.makedirs(self.output_dir, exist_ok=True)
        self.updated_at = time.time()
        data = {
            "state": self.state,
            "auto_advance": self.auto_advance,
            "master_prompt": self.master_prompt,
            "song_path": self.song_path,
            "engine": self.engine,
            "mode": self.mode,
            "story_model": self.story_model,
            "extraction": self.extraction,
            "packages": self.packages,
            "plan_ref": bool(self.plan),  # don't duplicate the full plan
            "anchors": self.anchors,
            "output_file": self.output_file,
            "errors": self.errors,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state_history": self.state_history[-50:],  # keep last 50
        }
        with open(self._state_file, "w") as f:
            json.dump(data, f, indent=2)

    def load(self):
        """Load state from JSON file."""
        if not os.path.isfile(self._state_file):
            return
        with open(self._state_file) as f:
            data = json.load(f)
        self.state = data.get("state", "IDLE")
        self.auto_advance = data.get("auto_advance", False)
        self.master_prompt = data.get("master_prompt", "")
        self.song_path = data.get("song_path", "")
        self.engine = data.get("engine", "gen4_5")
        self.mode = data.get("mode", "fast")
        self.story_model = data.get("story_model")
        self.extraction = data.get("extraction", {})
        self.packages = data.get("packages", [])
        self.anchors = data.get("anchors", {})
        self.output_file = data.get("output_file", "")
        self.errors = data.get("errors", [])
        self.created_at = data.get("created_at", time.time())
        self.updated_at = data.get("updated_at", time.time())
        self.state_history = data.get("state_history", [])

    # ── Internal helpers ──

    def _set_state(self, new_state: str):
        self.state_history.append({
            "state": new_state,
            "from": self.state,
            "timestamp": time.time(),
        })
        self.state = new_state
        self.save()

    def _all_sheets_done(self) -> bool:
        """Check if all package sheets are generated (not still pending)."""
        if not self.packages:
            return False
        # Packages are tracked by ID — check the store externally
        # For now, trust that the caller sets this state appropriately
        return True

    def _all_sheets_approved(self) -> bool:
        """Check if all packages are approved."""
        # Delegated to caller — the pipeline runner checks PreproductionStore
        return True

    def _all_anchors_done(self) -> bool:
        """Check if all shots have generated anchors."""
        shots = self.plan.get("scenes", [])
        if not shots:
            return False
        for s in shots:
            sid = s.get("shot_id", s.get("id", ""))
            anchor = self.anchors.get(sid, {})
            if anchor.get("status") not in ("generated", "approved", "rejected"):
                return False
        return True

    def _all_anchors_approved(self) -> bool:
        """Check if all anchors are approved (or no anchors needed)."""
        for anchor in self.anchors.values():
            if anchor.get("status") == "rejected":
                return False
            if anchor.get("status") not in ("approved", "generated"):
                return False
        return len(self.anchors) > 0

    def _all_shots_done(self) -> bool:
        """Check if all shots have generated clips."""
        shots = self.plan.get("scenes", [])
        if not shots:
            return False
        return all(s.get("status") == "done" for s in shots)
