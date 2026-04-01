"""
Multi-Angle Coverage System — Generate professional shot coverage like a real film shoot.

Prevents repetitive visual language and provides editorial flexibility.
"""

import random

# Coverage roles library
COVERAGE_ROLES = [
    "Master Wide", "Wide Establishing", "Medium Front", "Medium Side",
    "Close Reaction", "Extreme Close Detail", "Over-the-Shoulder", "POV",
    "Insert Detail", "Tracking Entrance", "Reverse Angle", "Profile Walk",
    "Low Hero Angle", "High Surveillance Angle", "Top Down",
    "Cutaway Environment", "Prop Insert", "Hands Detail",
    "Feet / Movement Detail", "Silhouette Reveal", "Backlit Profile",
    "Reflection Shot", "Mirror Shot", "Crowd / Background Plate",
    "Texture / Atmosphere Plate",
]

# Coverage mode templates
COVERAGE_MODES = {
    "minimal": {
        "name": "Minimal Coverage",
        "shots_range": (2, 3),
        "required": ["Wide Establishing", "Medium Front"],
        "optional": ["Close Reaction"],
    },
    "standard": {
        "name": "Standard Coverage",
        "shots_range": (3, 5),
        "required": ["Wide Establishing", "Medium Front", "Close Reaction"],
        "optional": ["Insert Detail", "Reverse Angle", "Tracking Entrance"],
    },
    "full_cinematic": {
        "name": "Full Cinematic Coverage",
        "shots_range": (5, 8),
        "required": ["Master Wide", "Wide Establishing", "Medium Front", "Close Reaction", "Reverse Angle"],
        "optional": ["Over-the-Shoulder", "Insert Detail", "Profile Walk", "Low Hero Angle", "Cutaway Environment"],
    },
    "music_video": {
        "name": "Music Video Coverage",
        "shots_range": (4, 7),
        "required": ["Wide Establishing", "Close Reaction", "Tracking Entrance"],
        "optional": ["Low Hero Angle", "Silhouette Reveal", "Profile Walk", "Extreme Close Detail", "Backlit Profile"],
    },
    "performance": {
        "name": "Performance Coverage",
        "shots_range": (4, 6),
        "required": ["Master Wide", "Medium Front", "Close Reaction"],
        "optional": ["Profile Walk", "Low Hero Angle", "Crowd / Background Plate", "Hands Detail"],
    },
    "dialogue": {
        "name": "Dialogue Coverage",
        "shots_range": (4, 6),
        "required": ["Master Wide", "Medium Front", "Over-the-Shoulder", "Close Reaction"],
        "optional": ["Reverse Angle", "Insert Detail", "Medium Side"],
    },
    "action": {
        "name": "Action Coverage",
        "shots_range": (5, 8),
        "required": ["Wide Establishing", "Tracking Entrance", "Close Reaction", "Low Hero Angle"],
        "optional": ["POV", "Feet / Movement Detail", "Hands Detail", "Insert Detail", "Reverse Angle"],
    },
    "experimental": {
        "name": "Experimental Coverage",
        "shots_range": (3, 6),
        "required": ["Extreme Close Detail"],
        "optional": ["Silhouette Reveal", "Reflection Shot", "Mirror Shot", "Top Down",
                     "Texture / Atmosphere Plate", "POV", "High Surveillance Angle"],
    },
}

# Role → shot data mapping
ROLE_TO_SHOT = {
    "Master Wide": {"shot_type": "wide", "lens": "24mm Wide", "movement": "Static", "angle": "Eye Level", "composition": "Centered Symmetry"},
    "Wide Establishing": {"shot_type": "wide", "lens": "24mm Wide", "movement": "Slow Dolly Out", "angle": "Eye Level", "composition": "Leading Lines"},
    "Medium Front": {"shot_type": "medium", "lens": "35mm Natural", "movement": "Static", "angle": "Eye Level", "composition": "Rule of Thirds"},
    "Medium Side": {"shot_type": "medium", "lens": "50mm Standard", "movement": "Tracking Right", "angle": "Eye Level", "composition": "Rule of Thirds"},
    "Close Reaction": {"shot_type": "close", "lens": "85mm Portrait", "movement": "Static", "angle": "Eye Level", "composition": "Rule of Thirds"},
    "Extreme Close Detail": {"shot_type": "macro", "lens": "Macro Lens", "movement": "Static", "angle": "Eye Level", "composition": "Centered Symmetry"},
    "Over-the-Shoulder": {"shot_type": "medium", "lens": "50mm Standard", "movement": "Static", "angle": "Shoulder Height", "composition": "Foreground Framing"},
    "POV": {"shot_type": "medium", "lens": "28mm Wide", "movement": "Handheld", "angle": "Eye Level", "composition": "Centered Symmetry"},
    "Insert Detail": {"shot_type": "close", "lens": "Macro Lens", "movement": "Static", "angle": "High Angle", "composition": "Centered Symmetry"},
    "Tracking Entrance": {"shot_type": "medium", "lens": "35mm Natural", "movement": "Steadicam", "angle": "Eye Level", "composition": "Leading Lines"},
    "Reverse Angle": {"shot_type": "medium", "lens": "35mm Natural", "movement": "Static", "angle": "Eye Level", "composition": "Rule of Thirds"},
    "Profile Walk": {"shot_type": "medium", "lens": "50mm Standard", "movement": "Tracking Right", "angle": "Eye Level", "composition": "Rule of Thirds"},
    "Low Hero Angle": {"shot_type": "medium", "lens": "24mm Wide", "movement": "Static", "angle": "Low Angle", "composition": "Centered Symmetry"},
    "High Surveillance Angle": {"shot_type": "wide", "lens": "18mm Ultra Wide", "movement": "Static", "angle": "High Angle", "composition": "Centered Symmetry"},
    "Top Down": {"shot_type": "wide", "lens": "24mm Wide", "movement": "Static", "angle": "Overhead Top Down", "composition": "Centered Symmetry"},
    "Cutaway Environment": {"shot_type": "wide", "lens": "35mm Natural", "movement": "Slow Dolly In", "angle": "Eye Level", "composition": "Negative Space"},
    "Prop Insert": {"shot_type": "close", "lens": "Macro Lens", "movement": "Static", "angle": "High Angle", "composition": "Centered Symmetry"},
    "Hands Detail": {"shot_type": "close", "lens": "Macro Lens", "movement": "Static", "angle": "High Angle", "composition": "Centered Symmetry"},
    "Feet / Movement Detail": {"shot_type": "close", "lens": "35mm Natural", "movement": "Tracking Forward", "angle": "Ground Level", "composition": "Leading Lines"},
    "Silhouette Reveal": {"shot_type": "wide", "lens": "35mm Natural", "movement": "Static", "angle": "Eye Level", "composition": "Silhouette Composition"},
    "Backlit Profile": {"shot_type": "medium", "lens": "85mm Portrait", "movement": "Static", "angle": "Eye Level", "composition": "Rule of Thirds"},
    "Reflection Shot": {"shot_type": "medium", "lens": "50mm Standard", "movement": "Static", "angle": "Eye Level", "composition": "Reflections Composition"},
    "Mirror Shot": {"shot_type": "medium", "lens": "50mm Standard", "movement": "Static", "angle": "Eye Level", "composition": "Mirrored Composition"},
    "Crowd / Background Plate": {"shot_type": "wide", "lens": "24mm Wide", "movement": "Handheld", "angle": "Eye Level", "composition": "Crowded Frame"},
    "Texture / Atmosphere Plate": {"shot_type": "wide", "lens": "35mm Natural", "movement": "Drift Float Movement", "angle": "Eye Level", "composition": "Negative Space"},
}


def generate_coverage(scene_beat: str, mode: str = "standard",
                       scene_data: dict = None, section_type: str = "verse") -> dict:
    """
    Generate coverage shots for a scene beat.

    Returns:
        {coverage_group_id, mode, beat, shots: [{role, shot_data}], categories}
    """
    import uuid
    template = COVERAGE_MODES.get(mode, COVERAGE_MODES["standard"])
    min_shots, max_shots = template["shots_range"]

    # Start with required shots
    selected_roles = list(template["required"])

    # Add optional shots up to max
    optional = list(template["optional"])
    random.shuffle(optional)
    remaining = max_shots - len(selected_roles)
    selected_roles.extend(optional[:remaining])

    # Avoid duplicate shot types in a row
    # Reorder: wide first, then medium, then close, then detail
    type_order = {"wide": 0, "medium": 1, "close": 2, "macro": 3}
    selected_roles.sort(key=lambda r: type_order.get(ROLE_TO_SHOT.get(r, {}).get("shot_type", "medium"), 1))

    # Build shot objects
    shots = []
    base_dur = 3.0
    if section_type in ("intro", "outro"):
        base_dur = 5.0
    elif section_type == "chorus":
        base_dur = 2.0

    for i, role in enumerate(selected_roles):
        role_data = ROLE_TO_SHOT.get(role, {})
        shot_type = role_data.get("shot_type", "medium")

        # Vary duration by shot type
        dur = base_dur
        if shot_type == "wide":
            dur = base_dur * 1.2
        elif shot_type == "close":
            dur = base_dur * 0.8
        elif shot_type == "macro":
            dur = base_dur * 0.6

        shot = {
            "id": f"cov_{uuid.uuid4().hex[:6]}",
            "shot_number": i + 1,
            "title": role,
            "coverage_role": role,
            "duration": round(dur, 1),
            "camera": {
                "shot_type": shot_type,
                "lens": role_data.get("lens", "35mm Natural"),
                "movement": role_data.get("movement", "Static"),
                "angle": role_data.get("angle", "Eye Level"),
                "preset": None,
            },
            "framing": {
                "composition": role_data.get("composition", "Rule of Thirds"),
                "subject_position": "center",
                "depth": "mid",
            },
            "action": {
                "summary": f"{role}: {scene_beat}" if scene_beat else role,
                "start_pose": "",
                "end_pose": "",
            },
            "performance": {
                "intensity": 5,
                "energy": "controlled",
                "emotion": "confident",
                "speed": "normal",
            },
            "continuity": {
                "lock_environment": True,
                "lock_character_pose": False,
                "lock_lighting": True,
                "lock_props": True,
            },
            "layers": {"surface": "", "symbolic": "", "hidden": "", "emotional": ""},
            "style_selections": {},
        }
        shots.append(shot)

    # Categorize
    primary = [s for s in shots if s["coverage_role"] in template["required"]]
    detail = [s for s in shots if s["coverage_role"] not in template["required"]]

    return {
        "coverage_group_id": f"cov_{uuid.uuid4().hex[:8]}",
        "mode": mode,
        "mode_name": template["name"],
        "beat": scene_beat,
        "total_shots": len(shots),
        "shots": shots,
        "categories": {
            "primary": [s["coverage_role"] for s in primary],
            "detail": [s["coverage_role"] for s in detail],
        },
    }
