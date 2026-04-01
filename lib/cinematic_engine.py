"""
Cinematic Consistency Engine — Phase 2

Converts the system from "image generator" → "cinematic engine".
Video is built from SHOTS, not scenes. Scenes = containers, Shots = generation units.

Core systems:
- Shots: individual camera shots as smallest generation unit
- State Memory: track character/environment/prop state between shots
- Frame Continuity: save last frame, pass as reference to next shot
- Performance: control intensity, energy, emotion, speed
- Locks: prevent drift between shots
- Camera Presets: standardized cinematic camera language
- Props: track prop consistency
- Layers: embed deeper meaning (surface, symbolic, hidden, emotional)
"""

import json
import os
import subprocess
import sys
import time
import uuid


# ---- Data Directories ----

ENGINE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "cinematic_engine"
)
FRAMES_DIR = os.path.join(ENGINE_DIR, "frames")
STATE_DIR = os.path.join(ENGINE_DIR, "state")
PROPS_DIR = os.path.join(ENGINE_DIR, "props")
SHOTS_DIR = os.path.join(ENGINE_DIR, "shots")

for d in (ENGINE_DIR, FRAMES_DIR, STATE_DIR, PROPS_DIR, SHOTS_DIR):
    os.makedirs(d, exist_ok=True)


# ---- Cinematic Camera Presets ----

CAMERA_PRESETS = {
    "kubrick_symmetry_static": {
        "shot_type": "wide",
        "lens": "24mm",
        "height": "eye",
        "angle": "straight",
        "movement": "static",
        "composition": "symmetry",
        "description": "perfectly symmetrical static wide shot, one-point perspective, centered subject, Kubrick-style",
    },
    "fincher_slow_creep": {
        "shot_type": "medium",
        "lens": "35mm",
        "height": "eye",
        "angle": "straight",
        "movement": "slow dolly forward",
        "composition": "centered",
        "description": "slow creeping dolly in, methodical movement, controlled tension, Fincher-style",
    },
    "nolan_push_in": {
        "shot_type": "close",
        "lens": "50mm",
        "height": "eye",
        "angle": "straight",
        "movement": "steady push in",
        "composition": "rule_of_thirds",
        "description": "steady push-in toward subject face, building intensity, shallow depth of field, Nolan-style",
    },
    "handheld_documentary": {
        "shot_type": "medium",
        "lens": "35mm",
        "height": "eye",
        "angle": "straight",
        "movement": "handheld subtle sway",
        "composition": "off-center",
        "description": "handheld documentary style, natural movement, intimate and raw, slight camera breathing",
    },
    "music_video_fast_cut": {
        "shot_type": "close",
        "lens": "85mm",
        "height": "low",
        "angle": "straight",
        "movement": "quick pan",
        "composition": "dynamic",
        "description": "fast dynamic cuts, dramatic angles, high energy, shallow depth of field, music video style",
    },
    "surveillance_static": {
        "shot_type": "wide",
        "lens": "24mm",
        "height": "high",
        "angle": "overhead",
        "movement": "static",
        "composition": "centered",
        "description": "static overhead surveillance angle, cold detached perspective, wide lens distortion",
    },
    "tarkovsky_stillness": {
        "shot_type": "wide",
        "lens": "35mm",
        "height": "eye",
        "angle": "straight",
        "movement": "imperceptible slow drift",
        "composition": "balanced",
        "description": "meditative stillness, barely perceptible camera drift, contemplative framing, Tarkovsky-style",
    },
    "spielberg_tracking": {
        "shot_type": "medium",
        "lens": "40mm",
        "height": "eye",
        "angle": "straight",
        "movement": "smooth tracking alongside subject",
        "composition": "rule_of_thirds",
        "description": "smooth tracking shot moving alongside walking subject, steady and elegant, Spielberg-style",
    },
    "wes_anderson_centered": {
        "shot_type": "medium",
        "lens": "40mm",
        "height": "eye",
        "angle": "straight",
        "movement": "static or precise pan",
        "composition": "dead center symmetry",
        "description": "perfectly centered symmetrical frame, pastel palette, whimsical precision, Wes Anderson-style",
    },
    "drone_reveal": {
        "shot_type": "wide",
        "lens": "24mm",
        "height": "high",
        "angle": "descending",
        "movement": "ascending crane reveal",
        "composition": "centered",
        "description": "ascending aerial reveal, starting close pulling to wide establishing shot, cinematic scope",
    },
}


# ---- Performance System ----

PERFORMANCE_DESCRIPTORS = {
    "energy": {
        "low": "minimal movement, subdued, still, restrained energy",
        "controlled": "measured movement, purposeful gestures, controlled intensity",
        "explosive": "dynamic movement, explosive energy, powerful gestures, high physicality",
    },
    "emotion": {
        "calm": "serene expression, relaxed posture, peaceful demeanor",
        "tense": "tight jaw, alert eyes, coiled tension in body, ready to act",
        "confident": "chin up, direct gaze, open posture, commanding presence",
        "aggressive": "furrowed brow, clenched fists, forward lean, confrontational stance",
        "vulnerable": "downcast eyes, protective posture, soft expression, exposed",
        "defiant": "raised chin, hard stare, squared shoulders, unyielding stance",
        "melancholy": "distant gaze, slight droop in shoulders, weight of sorrow visible",
    },
    "speed": {
        "slow": "slow deliberate movement, time feels stretched",
        "normal": "natural movement speed, realistic pacing",
        "fast": "quick sharp movements, urgent pacing, heightened tempo",
    },
}


def build_performance_block(performance: dict) -> str:
    """Build performance descriptor from structured data."""
    if not performance:
        return ""

    parts = []

    intensity = performance.get("intensity", 5)
    if isinstance(intensity, (int, float)):
        if intensity <= 3:
            parts.append("understated restrained performance")
        elif intensity <= 6:
            parts.append("moderate grounded performance")
        elif intensity <= 8:
            parts.append("heightened intense performance")
        else:
            parts.append("maximum intensity, raw powerful performance")

    energy = performance.get("energy", "controlled")
    desc = PERFORMANCE_DESCRIPTORS["energy"].get(energy, "")
    if desc:
        parts.append(desc)

    emotion = performance.get("emotion", "")
    desc = PERFORMANCE_DESCRIPTORS["emotion"].get(emotion, "")
    if desc:
        parts.append(desc)

    speed = performance.get("speed", "normal")
    desc = PERFORMANCE_DESCRIPTORS["speed"].get(speed, "")
    if desc:
        parts.append(desc)

    return ", ".join(parts)


# ---- Layer System ----

def build_layer_block(layers: dict) -> str:
    """Build layer system prompt injection."""
    if not layers:
        return ""

    parts = []
    if layers.get("surface"):
        parts.append(layers["surface"])
    if layers.get("symbolic"):
        parts.append(f"visual metaphor: {layers['symbolic']}")
    if layers.get("emotional"):
        parts.append(f"viewer should feel: {layers['emotional']}")
    # Hidden layer influences generation subtly through the symbolic/emotional layers
    # but is not explicitly stated in the prompt
    return ", ".join(parts)


# ---- Props System ----

def build_props_block(props: list) -> str:
    """Build props description for prompt injection."""
    if not props:
        return ""

    parts = []
    for prop in props:
        if isinstance(prop, dict):
            name = prop.get("name", "")
            material = prop.get("material", "")
            position = prop.get("position_rules", "")
            if name:
                desc = name
                if material:
                    desc += f" ({material})"
                if position:
                    desc += f" {position}"
                parts.append(desc)
        elif isinstance(prop, str):
            parts.append(prop)

    if parts:
        return "Props: " + ", ".join(parts)
    return ""


# ---- Lock System ----

DEFAULT_LOCKS = {
    "character_lock": True,
    "environment_lock": True,
    "tone_lock": True,
    "visual_lock": True,
    "continuity_lock": True,
    "prop_lock": True,
}


def build_lock_block(locks: dict) -> str:
    """Build continuity lock instructions."""
    if not locks:
        locks = DEFAULT_LOCKS

    instructions = []
    if locks.get("character_lock"):
        instructions.append("maintain exact character appearance")
    if locks.get("environment_lock"):
        instructions.append("same environment, no location changes")
    if locks.get("tone_lock"):
        instructions.append("consistent color grade and mood")
    if locks.get("visual_lock"):
        instructions.append("consistent visual style and lighting")
    if locks.get("continuity_lock"):
        instructions.append("smooth continuity from previous shot")
    if locks.get("prop_lock"):
        instructions.append("all props in correct positions")

    if instructions:
        return "Continuity: " + ", ".join(instructions)
    return ""


# ---- State Memory System ----

class StateMemory:
    """Maintain continuity state between shots."""

    def __init__(self, scene_id: str):
        self.scene_id = scene_id
        self.state_path = os.path.join(STATE_DIR, f"{scene_id}.json")
        self.state = self._load()

    def _load(self) -> dict:
        if os.path.isfile(self.state_path):
            try:
                with open(self.state_path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "scene_id": self.scene_id,
            "shot_count": 0,
            "character_state": {},
            "environment_state": {},
            "prop_state": {},
            "last_shot_id": None,
            "reference_frame": None,
        }

    def save(self):
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)

    def get_previous_state(self) -> dict:
        """Get the state to inject into the next shot."""
        return self.state

    def update_after_shot(self, shot_id: str, updates: dict = None,
                           reference_frame: str = None):
        """Update state after a shot is generated."""
        self.state["shot_count"] += 1
        self.state["last_shot_id"] = shot_id
        if reference_frame:
            self.state["reference_frame"] = reference_frame
        if updates:
            if "character_state" in updates:
                self.state["character_state"].update(updates["character_state"])
            if "environment_state" in updates:
                self.state["environment_state"].update(updates["environment_state"])
            if "prop_state" in updates:
                self.state["prop_state"].update(updates["prop_state"])
        self.save()

    def build_state_prompt(self) -> str:
        """Build a prompt fragment from current state for continuity."""
        parts = []
        cs = self.state.get("character_state", {})
        if cs.get("position"):
            parts.append(f"character is {cs['position']}")
        if cs.get("pose"):
            parts.append(f"in {cs['pose']} pose")
        if cs.get("emotion"):
            parts.append(f"expressing {cs['emotion']}")

        es = self.state.get("environment_state", {})
        if es.get("lighting"):
            parts.append(f"lighting: {es['lighting']}")

        return ", ".join(parts)


# ---- Frame Continuity System ----

def extract_last_frame(video_path: str, output_path: str = None) -> str:
    """Extract the last frame from a video as a reference image for the next shot."""
    if not os.path.isfile(video_path):
        return None

    if output_path is None:
        base = os.path.splitext(os.path.basename(video_path))[0]
        output_path = os.path.join(FRAMES_DIR, f"{base}_last_frame.jpg")

    try:
        # Get video duration
        kw = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kw["startupinfo"] = si

        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=10, **kw
        )
        duration = float(probe.stdout.strip())

        # Extract frame at duration - 0.1s
        frame_time = max(0, duration - 0.1)
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(frame_time), "-i", video_path,
             "-frames:v", "1", "-q:v", "2", output_path],
            capture_output=True, timeout=15, **kw
        )

        if os.path.isfile(output_path):
            return output_path
    except Exception as e:
        print(f"[FRAME CONTINUITY] Failed to extract last frame: {e}")

    return None


# ---- Shot Data Model ----

def create_shot(scene_id: str, shot_number: int, **kwargs) -> dict:
    """Create a new shot object with all fields."""
    return {
        "id": f"shot_{uuid.uuid4().hex[:8]}",
        "scene_id": scene_id,
        "shot_number": shot_number,
        "duration": kwargs.get("duration", 4),

        "camera": {
            "shot_type": kwargs.get("shot_type", "medium"),
            "lens": kwargs.get("lens", "35mm"),
            "height": kwargs.get("height", "eye"),
            "angle": kwargs.get("angle", "straight"),
            "movement": kwargs.get("movement", "static"),
            "preset": kwargs.get("camera_preset", None),
        },

        "framing": {
            "composition": kwargs.get("composition", "rule_of_thirds"),
            "subject_position": kwargs.get("subject_position", "center"),
            "depth": kwargs.get("depth", "mid"),
        },

        "action": {
            "summary": kwargs.get("action_summary", ""),
            "start_pose": kwargs.get("start_pose", ""),
            "end_pose": kwargs.get("end_pose", ""),
            "movement_rules": kwargs.get("movement_rules", []),
        },

        "performance": {
            "intensity": kwargs.get("intensity", 5),
            "energy": kwargs.get("energy", "controlled"),
            "emotion": kwargs.get("emotion", "calm"),
            "speed": kwargs.get("speed", "normal"),
        },

        "layers": {
            "surface": kwargs.get("surface_layer", ""),
            "symbolic": kwargs.get("symbolic_layer", ""),
            "hidden": kwargs.get("hidden_layer", ""),
            "emotional": kwargs.get("emotional_layer", ""),
        },

        "locks": kwargs.get("locks", dict(DEFAULT_LOCKS)),

        "props": kwargs.get("props", []),

        "continuity": {
            "lock_environment": kwargs.get("lock_environment", True),
            "lock_character_pose": kwargs.get("lock_character_pose", False),
            "lock_lighting": kwargs.get("lock_lighting", True),
            "lock_props": kwargs.get("lock_props", True),
        },

        "reference_frame": None,
        "clip_path": None,
        "status": "planned",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ---- Shot Prompt Assembly (22-step pipeline) ----

def assemble_shot_prompt(
    shot: dict,
    character: dict = None,
    costume: dict = None,
    environment: dict = None,
    global_style: str = "",
    world_setting: str = "",
    global_negative: str = "",
    state: dict = None,
    props: list = None,
) -> dict:
    """
    The 22-step cinematic generation pipeline, condensed into prompt assembly.

    Steps 1-17 produce the prompt. Steps 18-22 happen during/after generation.

    Returns:
        dict with prompt, negative_prompt, reference_frame, blocks
    """
    from lib.prompt_assembler import (
        build_character_block, build_costume_block, build_environment_block,
        DEFAULT_NEGATIVE,
    )

    # 1. Validate (caller's responsibility, but we provide defaults)
    # 2. Load scene (shot has scene_id)
    # 3. Load shot (we have it)
    # 4. Load previous state
    state_prompt = ""
    reference_frame = None
    if state:
        sm = StateMemory.__new__(StateMemory)
        sm.state = state
        state_prompt = sm.build_state_prompt()
        reference_frame = state.get("reference_frame")

    # 5. Apply locks
    lock_block = build_lock_block(shot.get("locks", DEFAULT_LOCKS))

    # 6. Global style preset
    style = global_style or "Cinematic film still, 2.39:1 anamorphic widescreen"

    # 7. Environment block
    env_block = build_environment_block(environment)

    # 8. Character block
    char_block = build_character_block(character)

    # 9. Costume block
    costume_block = build_costume_block(costume)

    # 10. Props block
    props_block = build_props_block(props or shot.get("props", []))

    # 11. Action block
    action = shot.get("action", {})
    action_parts = []
    if action.get("summary"):
        action_parts.append(action["summary"])
    if action.get("start_pose"):
        action_parts.append(f"starting from {action['start_pose']}")
    if action.get("end_pose"):
        action_parts.append(f"moving to {action['end_pose']}")
    action_block = ", ".join(action_parts)

    # 12. Performance block
    perf_block = build_performance_block(shot.get("performance", {}))

    # 13. Camera block
    camera = shot.get("camera", {})
    cam_preset_name = camera.get("preset")
    if cam_preset_name and cam_preset_name in CAMERA_PRESETS:
        cam_block = CAMERA_PRESETS[cam_preset_name]["description"]
    else:
        cam_parts = []
        if camera.get("shot_type"):
            cam_parts.append(f"{camera['shot_type']} shot")
        if camera.get("lens"):
            cam_parts.append(f"{camera['lens']} lens")
        if camera.get("height") and camera["height"] != "eye":
            cam_parts.append(f"{camera['height']} angle")
        if camera.get("angle") and camera["angle"] != "straight":
            cam_parts.append(f"{camera['angle']} angle")
        if camera.get("movement") and camera["movement"] != "static":
            cam_parts.append(camera["movement"])
        framing = shot.get("framing", {})
        if framing.get("composition"):
            cam_parts.append(framing["composition"].replace("_", " "))
        cam_block = ", ".join(cam_parts)

    # 14. Layer system
    layer_block = build_layer_block(shot.get("layers", {}))

    # 15. Continuity constraints
    continuity_parts = []
    if state_prompt:
        continuity_parts.append(f"Continuing from: {state_prompt}")
    if lock_block:
        continuity_parts.append(lock_block)
    continuity_block = ". ".join(continuity_parts)

    # 16. Reference frame (returned for the generator to use)
    # 17. Negative prompt
    neg_parts = [global_negative or DEFAULT_NEGATIVE]
    neg_parts.append("no character duplication, no prop teleportation, no lighting shifts, no costume changes")
    negative = ", ".join(neg_parts)

    # ---- Compile in strict order ----
    prompt_parts = []
    if style:
        prompt_parts.append(style)
    if world_setting:
        prompt_parts.append(world_setting)
    if env_block:
        prompt_parts.append(env_block)
    if char_block:
        prompt_parts.append(char_block)
    if costume_block:
        prompt_parts.append(costume_block)
    if props_block:
        prompt_parts.append(props_block)
    if action_block:
        prompt_parts.append(action_block)
    if perf_block:
        prompt_parts.append(perf_block)
    if cam_block:
        prompt_parts.append(cam_block)
    if layer_block:
        prompt_parts.append(layer_block)
    if continuity_block:
        prompt_parts.append(continuity_block)

    compiled = ". ".join(p for p in prompt_parts if p)

    return {
        "prompt": compiled,
        "negative_prompt": negative,
        "reference_frame": reference_frame,
        "blocks": {
            "global_style": style,
            "world_setting": world_setting,
            "environment": env_block,
            "character": char_block,
            "costume": costume_block,
            "props": props_block,
            "action": action_block,
            "performance": perf_block,
            "camera": cam_block,
            "layers": layer_block,
            "continuity": continuity_block,
        },
    }


# ---- Continuity Engine ----

# Continuity types tracked
CONTINUITY_TYPES = [
    "environment", "character", "costume", "lighting", "props", "motion_direction",
]

# Lock field mapping
LOCK_MAP = {
    "environment": "lock_environment",
    "character": "lock_character_pose",
    "costume": "lock_costume",
    "lighting": "lock_lighting",
    "props": "lock_props",
}


def validate_shot_continuity(shot: dict, prev_shot: dict = None,
                              scene_data: dict = None) -> list:
    """
    Validate a shot's continuity against its predecessor and scene context.

    Returns list of warning dicts:
        {type, severity, message, field, auto_fixable}
        severity: "ok" | "warning" | "error"
    """
    warnings = []
    cont = shot.get("continuity", {})
    perf = shot.get("performance", {})
    action = shot.get("action", {})
    camera = shot.get("camera", {})

    # 1. Environment continuity
    if cont.get("lock_environment", True):
        if prev_shot:
            prev_cam = prev_shot.get("camera", {})
            # Environment drift: different shot types with environment lock = risk
            # (not a hard error, just awareness)
            pass  # Environment is inherited from scene — locked means no issue
        warnings.append({
            "type": "environment", "severity": "ok",
            "message": "Environment locked", "field": "lock_environment",
            "auto_fixable": False,
        })
    else:
        warnings.append({
            "type": "environment", "severity": "warning",
            "message": "Environment NOT locked — drift risk",
            "field": "lock_environment", "auto_fixable": True,
        })

    # 2. Character continuity
    if not cont.get("lock_character_pose", False) and prev_shot:
        prev_end = (prev_shot.get("action", {}).get("end_pose") or "").strip()
        cur_start = (action.get("start_pose") or "").strip()
        if prev_end and cur_start and prev_end.lower() != cur_start.lower():
            warnings.append({
                "type": "character", "severity": "warning",
                "message": f"Pose mismatch: prev ends '{prev_end}', this starts '{cur_start}'",
                "field": "start_pose", "auto_fixable": True,
            })
        elif prev_end and not cur_start:
            warnings.append({
                "type": "character", "severity": "warning",
                "message": f"No start pose — prev shot ends '{prev_end}'",
                "field": "start_pose", "auto_fixable": True,
            })

    # 3. Lighting continuity
    if cont.get("lock_lighting", True):
        warnings.append({
            "type": "lighting", "severity": "ok",
            "message": "Lighting locked", "field": "lock_lighting",
            "auto_fixable": False,
        })
    else:
        warnings.append({
            "type": "lighting", "severity": "warning",
            "message": "Lighting NOT locked — inconsistency risk",
            "field": "lock_lighting", "auto_fixable": True,
        })

    # 4. Props continuity
    if cont.get("lock_props", True):
        warnings.append({
            "type": "props", "severity": "ok",
            "message": "Props locked", "field": "lock_props",
            "auto_fixable": False,
        })
    else:
        warnings.append({
            "type": "props", "severity": "warning",
            "message": "Props NOT locked — teleportation risk",
            "field": "lock_props", "auto_fixable": True,
        })

    # 5. Reference frame check
    if prev_shot and not shot.get("reference_frame"):
        warnings.append({
            "type": "continuity", "severity": "warning",
            "message": "No reference frame from previous shot",
            "field": "reference_frame", "auto_fixable": False,
        })

    # 6. Motion direction
    if prev_shot:
        prev_move = (prev_shot.get("camera", {}).get("movement") or "").lower()
        cur_move = (camera.get("movement") or "").lower()
        # 180-degree rule check: opposite pans in consecutive shots
        opposite_pairs = [("pan_left", "pan_right"), ("pan right", "pan left"),
                          ("tracking left", "tracking right"), ("dolly left", "dolly right")]
        for a, b in opposite_pairs:
            if (a in prev_move and b in cur_move) or (b in prev_move and a in cur_move):
                warnings.append({
                    "type": "motion_direction", "severity": "warning",
                    "message": "Camera direction reversal — 180-degree rule risk",
                    "field": "movement", "auto_fixable": False,
                })
                break

    # 7. Performance continuity (energy jumps)
    if prev_shot:
        prev_intensity = prev_shot.get("performance", {}).get("intensity", 5)
        cur_intensity = perf.get("intensity", 5)
        if abs(cur_intensity - prev_intensity) > 5:
            warnings.append({
                "type": "performance", "severity": "warning",
                "message": f"Large intensity jump: {prev_intensity} → {cur_intensity}",
                "field": "intensity", "auto_fixable": False,
            })

    # 8. Missing required data
    if not action.get("summary"):
        warnings.append({
            "type": "data", "severity": "warning",
            "message": "No action description",
            "field": "action.summary", "auto_fixable": False,
        })
    if not perf.get("emotion"):
        warnings.append({
            "type": "data", "severity": "warning",
            "message": "No emotion set",
            "field": "performance.emotion", "auto_fixable": False,
        })

    return warnings


def get_continuity_status(warnings: list) -> str:
    """Compute overall continuity health from warnings list."""
    severities = [w["severity"] for w in warnings]
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "warning"
    return "ok"


def fix_shot_continuity(shot: dict, prev_shot: dict = None,
                         scene_data: dict = None) -> dict:
    """
    Auto-fix continuity issues in a shot based on its predecessor.

    Returns the modified shot dict.
    """
    cont = shot.setdefault("continuity", {})
    action = shot.setdefault("action", {})

    # Fix 1: Inherit start pose from previous shot's end pose
    if prev_shot:
        prev_end = (prev_shot.get("action", {}).get("end_pose") or "").strip()
        cur_start = (action.get("start_pose") or "").strip()
        if prev_end and not cur_start:
            action["start_pose"] = prev_end

    # Fix 2: Enable all locks by default
    cont.setdefault("lock_environment", True)
    cont.setdefault("lock_lighting", True)
    cont.setdefault("lock_props", True)

    # Fix 3: Link previous shot
    if prev_shot:
        shot["previous_shot_id"] = prev_shot.get("id")

    # Fix 4: Copy reference frame from previous shot if available
    if prev_shot and prev_shot.get("reference_frame") and not shot.get("reference_frame"):
        shot["reference_frame"] = prev_shot["reference_frame"]

    return shot


def validate_scene_continuity(shots: list, scene_data: dict = None) -> dict:
    """
    Validate continuity across all shots in a scene.

    Returns:
        {
            status: "ok" | "warning" | "error",
            total_warnings: int,
            total_errors: int,
            shots: [{shot_id, shot_number, status, warnings}, ...]
        }
    """
    results = []
    for i, shot in enumerate(shots):
        prev = shots[i - 1] if i > 0 else None
        warnings = validate_shot_continuity(shot, prev, scene_data)
        status = get_continuity_status(warnings)
        results.append({
            "shot_id": shot.get("id", f"shot_{i}"),
            "shot_number": shot.get("shot_number", i + 1),
            "status": status,
            "warnings": warnings,
        })

    all_statuses = [r["status"] for r in results]
    total_warnings = sum(1 for w in results for ww in w["warnings"] if ww["severity"] == "warning")
    total_errors = sum(1 for w in results for ww in w["warnings"] if ww["severity"] == "error")

    overall = "error" if "error" in all_statuses else ("warning" if "warning" in all_statuses else "ok")

    return {
        "status": overall,
        "total_warnings": total_warnings,
        "total_errors": total_errors,
        "shots": results,
    }


def fix_scene_continuity(shots: list, scene_data: dict = None) -> list:
    """Auto-fix continuity across all shots in a scene."""
    for i, shot in enumerate(shots):
        prev = shots[i - 1] if i > 0 else None
        fix_shot_continuity(shot, prev, scene_data)
    return shots


# ---- Style Memory System ----

STYLE_MEMORY_PATH = os.path.join(ENGINE_DIR, "style_memory.json")

DEFAULT_STYLE_MEMORY = {
    "color_palette": [],
    "lighting_style": "",
    "camera_language": "",
    "film_texture": "",
    "mood_profile": "",
    "visual_rules": [],
    "locked": False,
    "source": "default",
}


class StyleMemory:
    """Maintain consistent visual identity across all scenes and shots."""

    def __init__(self):
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.isfile(STYLE_MEMORY_PATH):
            try:
                with open(STYLE_MEMORY_PATH, "r") as f:
                    loaded = json.load(f)
                # Merge with defaults for any missing keys
                merged = dict(DEFAULT_STYLE_MEMORY)
                merged.update(loaded)
                return merged
            except (json.JSONDecodeError, IOError):
                pass
        return dict(DEFAULT_STYLE_MEMORY)

    def save(self):
        os.makedirs(os.path.dirname(STYLE_MEMORY_PATH), exist_ok=True)
        with open(STYLE_MEMORY_PATH, "w") as f:
            json.dump(self.data, f, indent=2)

    def get(self) -> dict:
        return dict(self.data)

    def update(self, updates: dict):
        """Update style memory fields. Won't update if locked unless force=True."""
        if self.data.get("locked") and not updates.get("force"):
            return self.data
        for key in DEFAULT_STYLE_MEMORY:
            if key in updates and key != "locked":
                self.data[key] = updates[key]
        if "locked" in updates:
            self.data["locked"] = updates["locked"]
        self.data["source"] = updates.get("source", "user")
        self.save()
        return self.data

    def set_from_vision(self, universal_prompt: str, world_setting: str,
                         style: str = ""):
        """Populate style memory from Vision page inputs."""
        # Parse color hints from prompts
        colors = []
        for word in (universal_prompt + " " + world_setting + " " + style).lower().split(","):
            word = word.strip()
            for c in ["red", "blue", "green", "purple", "cyan", "amber", "gold",
                       "neon", "warm", "cool", "muted", "desaturated", "monochrome",
                       "black", "white", "grey", "silver", "orange", "pink",
                       "sage", "khaki", "teal", "crimson", "violet"]:
                if c in word and c not in colors:
                    colors.append(c)

        # Parse lighting
        lighting = ""
        for hint in ["golden hour", "neon-lit", "natural light", "harsh shadows",
                      "soft diffused", "dramatic", "flat", "backlit", "rim light",
                      "candlelight", "moonlight", "fluorescent", "overhead",
                      "low key", "high key", "chiaroscuro"]:
            if hint in (universal_prompt + " " + world_setting).lower():
                lighting = hint
                break
        if not lighting:
            lighting = "cinematic natural lighting"

        # Parse film texture
        texture = ""
        for hint in ["film grain", "35mm", "16mm", "digital clean", "vintage",
                      "anamorphic", "matte", "glossy", "halation", "bloom"]:
            if hint in (universal_prompt + " " + world_setting).lower():
                texture = hint
                break
        if not texture:
            texture = "subtle film grain"

        # Parse mood
        mood = ""
        for hint in ["noir", "dreamy", "gritty", "serene", "tense", "melancholy",
                      "euphoric", "dark", "moody", "calm", "intense", "ethereal",
                      "ominous", "hopeful", "nostalgic"]:
            if hint in (universal_prompt + " " + world_setting + " " + style).lower():
                mood = hint
                break

        self.data.update({
            "color_palette": colors[:8],
            "lighting_style": lighting,
            "film_texture": texture,
            "mood_profile": mood or "cinematic",
            "camera_language": style[:100] if style else "cinematic",
            "source": "vision",
        })
        self.save()
        return self.data

    def learn_from_shots(self, shots: list):
        """Auto-learn patterns from edited shots (only if not locked)."""
        if self.data.get("locked"):
            return self.data

        if len(shots) < 2:
            return self.data

        # Detect dominant camera patterns
        lenses = {}
        movements = {}
        emotions = {}
        for shot in shots:
            cam = shot.get("camera", {})
            lens = cam.get("lens", "")
            if lens:
                lenses[lens] = lenses.get(lens, 0) + 1
            move = cam.get("movement", "")
            if move:
                movements[move] = movements.get(move, 0) + 1
            perf = shot.get("performance", {})
            emo = perf.get("emotion", "")
            if emo:
                emotions[emo] = emotions.get(emo, 0) + 1

        # Update with dominant patterns
        if lenses:
            dominant_lens = max(lenses, key=lenses.get)
            self.data["camera_language"] = f"predominantly {dominant_lens}"

        if emotions:
            dominant_emo = max(emotions, key=emotions.get)
            if not self.data.get("mood_profile"):
                self.data["mood_profile"] = dominant_emo

        self.data["source"] = "auto-learned"
        self.save()
        return self.data

    def build_enforcement_block(self) -> str:
        """Build a prompt enforcement string from style memory."""
        parts = []

        colors = self.data.get("color_palette", [])
        if colors:
            parts.append(f"color palette: {', '.join(colors)}")

        lighting = self.data.get("lighting_style", "")
        if lighting:
            parts.append(f"lighting: {lighting}")

        texture = self.data.get("film_texture", "")
        if texture:
            parts.append(texture)

        mood = self.data.get("mood_profile", "")
        if mood:
            parts.append(f"{mood} mood")

        rules = self.data.get("visual_rules", [])
        for rule in rules[:5]:
            if isinstance(rule, str) and rule:
                parts.append(rule)

        if not parts:
            return ""

        prefix = "Adhere to established visual style" if self.data.get("locked") else "Visual style"
        return f"{prefix}: {', '.join(parts)}"
