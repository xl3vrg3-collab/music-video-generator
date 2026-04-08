"""
Hierarchical Taste Profile System for LUMN Studio.

Two-layer taste system:
  1. Overall Taste Profile — persistent across all projects, learned over time
  2. Project Taste Profile — per-project, inherits/overrides overall

Blend order (weakest → strongest):
  overall taste → project taste → story brief + approved canon + hard constraints

Taste is OPTIONAL — the product works without it.

Dimensions (sliders ± 1.0 scale, 0.0 = neutral):
  composition:  symmetrical (-1) ↔ handheld (+1)
  lighting:     warm (-1) ↔ cool (+1)
  focus:        shallow DOF (-1) ↔ deep focus (+1)
  density:      minimal (-1) ↔ maximal (+1)
  realism:      polished (-1) ↔ raw/documentary (+1)
  pacing:       calm (-1) ↔ aggressive (+1)
  wardrobe:     grounded (-1) ↔ editorial/fashion (+1)
  framing:      intimate close-up (-1) ↔ epic wide (+1)
  texture:      clean (-1) ↔ gritty (+1)
  tone:         cinematic (-1) ↔ raw (+1)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TASTE_DIMENSIONS = [
    "composition", "lighting", "focus", "density", "realism",
    "pacing", "wardrobe", "framing", "texture", "tone",
]

DIMENSION_LABELS = {
    "composition": ("Symmetrical", "Handheld"),
    "lighting":    ("Warm", "Cool"),
    "focus":       ("Shallow DOF", "Deep Focus"),
    "density":     ("Minimal", "Dense/Maximal"),
    "realism":     ("Polished", "Raw/Documentary"),
    "pacing":      ("Calm", "Aggressive"),
    "wardrobe":    ("Grounded", "Editorial/Fashion"),
    "framing":     ("Intimate Close-Up", "Epic Wide"),
    "texture":     ("Clean", "Gritty"),
    "tone":        ("Cinematic", "Raw"),
}

# Quiz: paired comparisons — each pair isolates one dimension
QUIZ_PAIRS = [
    {
        "id": "q01", "dimension": "composition",
        "option_a": {"label": "Symmetrical, centered framing", "value": -0.8,
                     "visual_desc": "Perfectly centered subject, clean symmetry, Wes Anderson style"},
        "option_b": {"label": "Handheld, off-center framing", "value": 0.8,
                     "visual_desc": "Shaky handheld, subject off-frame, documentary feel"},
    },
    {
        "id": "q02", "dimension": "lighting",
        "option_a": {"label": "Warm golden hour", "value": -0.8,
                     "visual_desc": "Golden warm light, orange/amber tones, sunset glow"},
        "option_b": {"label": "Cool blue moonlight", "value": 0.8,
                     "visual_desc": "Cold blue tones, moonlit atmosphere, steel grays"},
    },
    {
        "id": "q03", "dimension": "focus",
        "option_a": {"label": "Dreamy shallow focus", "value": -0.8,
                     "visual_desc": "Ultra shallow depth of field, creamy bokeh, subject isolated"},
        "option_b": {"label": "Everything sharp", "value": 0.8,
                     "visual_desc": "Deep focus, everything in frame sharp and readable"},
    },
    {
        "id": "q04", "dimension": "density",
        "option_a": {"label": "Minimal, sparse", "value": -0.8,
                     "visual_desc": "Sparse composition, negative space, one subject, breathing room"},
        "option_b": {"label": "Dense, layered", "value": 0.8,
                     "visual_desc": "Packed frame, multiple layers, rich detail everywhere"},
    },
    {
        "id": "q05", "dimension": "realism",
        "option_a": {"label": "Polished commercial", "value": -0.8,
                     "visual_desc": "High production value, perfect skin, clean grade, commercial finish"},
        "option_b": {"label": "Indie realism", "value": 0.8,
                     "visual_desc": "Natural imperfections, real textures, authentic indie look"},
    },
    {
        "id": "q06", "dimension": "pacing",
        "option_a": {"label": "Slow emotional takes", "value": -0.8,
                     "visual_desc": "Long holds, slow camera moves, contemplative rhythm"},
        "option_b": {"label": "Fast kinetic cuts", "value": 0.8,
                     "visual_desc": "Rapid cuts, dynamic energy, beat-reactive editing"},
    },
    {
        "id": "q07", "dimension": "wardrobe",
        "option_a": {"label": "Everyday clothes", "value": -0.8,
                     "visual_desc": "Realistic everyday wardrobe, natural, lived-in"},
        "option_b": {"label": "Fashion editorial", "value": 0.8,
                     "visual_desc": "High fashion, editorial styling, statement pieces"},
    },
    {
        "id": "q08", "dimension": "framing",
        "option_a": {"label": "Intimate portraits", "value": -0.8,
                     "visual_desc": "Tight close-ups, face fills frame, emotional intimacy"},
        "option_b": {"label": "Epic landscapes", "value": 0.8,
                     "visual_desc": "Grand wide shots, tiny figure in vast landscape"},
    },
    {
        "id": "q09", "dimension": "texture",
        "option_a": {"label": "Clean and smooth", "value": -0.8,
                     "visual_desc": "Clean surfaces, smooth skin, no noise or grain"},
        "option_b": {"label": "Gritty and textured", "value": 0.8,
                     "visual_desc": "Film grain, rough textures, visible wear and grit"},
    },
    {
        "id": "q10", "dimension": "tone",
        "option_a": {"label": "Cinematic drama", "value": -0.8,
                     "visual_desc": "Theatrical lighting, dramatic staging, movie-like quality"},
        "option_b": {"label": "Raw unfiltered", "value": 0.8,
                     "visual_desc": "Unfiltered reality, found footage feel, no stylization"},
    },
    # Tie-breaker / refinement pairs
    {
        "id": "q11", "dimension": "lighting",
        "option_a": {"label": "High contrast noir", "value": -0.5,
                     "visual_desc": "Hard shadows, dramatic contrast, noir lighting"},
        "option_b": {"label": "Soft diffused light", "value": 0.5,
                     "visual_desc": "Soft even lighting, gentle gradients, overcast mood"},
    },
    {
        "id": "q12", "dimension": "composition",
        "option_a": {"label": "Leading lines, geometric", "value": -0.5,
                     "visual_desc": "Strong leading lines, geometric framing, architectural"},
        "option_b": {"label": "Organic, natural framing", "value": 0.5,
                     "visual_desc": "Organic shapes, natural elements as frame, flowing"},
    },
]


# ---------------------------------------------------------------------------
# Taste Profile Model
# ---------------------------------------------------------------------------

def create_profile(
    name: str = "Default",
    source: str = "manual",
    is_overall: bool = False,
    project_id: str = None,
) -> dict:
    """Create a new blank taste profile."""
    return {
        "profile_id": f"taste_{uuid.uuid4().hex[:8]}",
        "name": name,
        "source": source,  # quiz / uploaded_refs / learned / manual
        "is_overall": is_overall,
        "project_id": project_id,

        # Dimension values: -1.0 to +1.0, 0.0 = neutral
        "dimensions": {d: 0.0 for d in TASTE_DIMENSIONS},

        # Confidence per dimension (0-1): how sure we are about this preference
        "confidence": {d: 0.0 for d in TASTE_DIMENSIONS},

        # Reference uploads
        "reference_images": [],  # [{path, category, notes}]

        # Quiz answers (for re-scoring)
        "quiz_answers": [],

        # Behavior signals
        "behavior_signals": [],  # [{action, timestamp, context}]

        # Inheritance
        "inherit_overall": not is_overall,

        # Notes
        "notes": "",

        # Timestamps
        "created_at": time.time(),
        "updated_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Quiz Processing
# ---------------------------------------------------------------------------

def get_quiz_pairs() -> list[dict]:
    """Return the quiz pairs for the taste quiz UI."""
    return QUIZ_PAIRS


def process_quiz_answers(profile: dict, answers: list[dict]) -> dict:
    """Process quiz answers and update profile dimensions.

    Args:
        profile: taste profile dict
        answers: list of {question_id, choice: "a"|"b"|"both"|"skip"}

    Returns:
        updated profile
    """
    quiz_lookup = {q["id"]: q for q in QUIZ_PAIRS}

    # Accumulate values per dimension
    dim_values = {d: [] for d in TASTE_DIMENSIONS}

    for ans in answers:
        qid = ans.get("question_id", "")
        choice = ans.get("choice", "skip")
        q = quiz_lookup.get(qid)
        if not q:
            continue

        dim = q["dimension"]
        if choice == "a":
            dim_values[dim].append(q["option_a"]["value"])
        elif choice == "b":
            dim_values[dim].append(q["option_b"]["value"])
        elif choice == "both":
            dim_values[dim].append(0.0)  # neutral
        # skip = no data

    # Average values per dimension
    for dim in TASTE_DIMENSIONS:
        vals = dim_values[dim]
        if vals:
            avg = sum(vals) / len(vals)
            profile["dimensions"][dim] = round(avg, 2)
            # Confidence based on consistency and data volume
            spread = max(vals) - min(vals) if len(vals) > 1 else 0
            profile["confidence"][dim] = round(min(1.0, len(vals) * 0.4) * (1.0 - spread * 0.3), 2)

    profile["quiz_answers"] = answers
    profile["source"] = "quiz"
    profile["updated_at"] = time.time()
    return profile


def update_from_sliders(profile: dict, sliders: dict) -> dict:
    """Update profile from manual slider values.

    Args:
        sliders: {dimension_name: float} values from -1.0 to 1.0
    """
    for dim, val in sliders.items():
        if dim in TASTE_DIMENSIONS:
            profile["dimensions"][dim] = round(max(-1.0, min(1.0, float(val))), 2)
            profile["confidence"][dim] = max(profile["confidence"].get(dim, 0), 0.7)

    profile["source"] = "manual" if profile["source"] != "quiz" else "quiz+manual"
    profile["updated_at"] = time.time()
    return profile


# ---------------------------------------------------------------------------
# Behavior Learning
# ---------------------------------------------------------------------------

def record_behavior(profile: dict, action: str, context: dict = None) -> dict:
    """Record a behavior signal for learning.

    Actions: hero_selected, hero_rejected, take_chosen, take_rejected,
             shot_regenerated, video_exported, slider_adjusted
    """
    signal = {
        "action": action,
        "timestamp": time.time(),
        "context": context or {},
    }
    profile.setdefault("behavior_signals", []).append(signal)

    # Keep last 200 signals
    if len(profile["behavior_signals"]) > 200:
        profile["behavior_signals"] = profile["behavior_signals"][-200:]

    # Auto-learn from signals
    _learn_from_signals(profile)
    profile["updated_at"] = time.time()
    return profile


def _learn_from_signals(profile: dict):
    """Adjust taste dimensions based on accumulated behavior signals.

    Learning rate is low (0.05 per signal) to avoid sudden shifts.
    """
    signals = profile.get("behavior_signals", [])
    if len(signals) < 3:
        return

    # Count recent approvals/rejections by context tags
    recent = signals[-20:]
    for sig in recent:
        ctx = sig.get("context", {})
        action = sig.get("action", "")

        # Learn from hero ref selections
        if action == "hero_selected":
            style_tags = ctx.get("style_tags", {})
            for dim, direction in style_tags.items():
                if dim in TASTE_DIMENSIONS:
                    lr = 0.05  # learning rate
                    profile["dimensions"][dim] += lr * direction
                    profile["dimensions"][dim] = round(
                        max(-1.0, min(1.0, profile["dimensions"][dim])), 2)

        # Learn from take rankings
        elif action == "take_chosen":
            if ctx.get("warm_lighting"):
                profile["dimensions"]["lighting"] -= 0.03
            if ctx.get("cool_lighting"):
                profile["dimensions"]["lighting"] += 0.03
            if ctx.get("tight_framing"):
                profile["dimensions"]["framing"] -= 0.03
            if ctx.get("wide_framing"):
                profile["dimensions"]["framing"] += 0.03


# ---------------------------------------------------------------------------
# Profile Blending
# ---------------------------------------------------------------------------

def blend_profiles(overall: dict | None, project: dict | None) -> dict:
    """Blend overall and project profiles into a single effective profile.

    Project dimensions override overall when project confidence is higher.
    When project inherits from overall, unset project dimensions fall through
    to overall values.
    """
    result = {d: 0.0 for d in TASTE_DIMENSIONS}
    confidence = {d: 0.0 for d in TASTE_DIMENSIONS}

    if not overall and not project:
        return {"dimensions": result, "confidence": confidence}

    # Start with overall
    if overall:
        for d in TASTE_DIMENSIONS:
            result[d] = overall.get("dimensions", {}).get(d, 0.0)
            confidence[d] = overall.get("confidence", {}).get(d, 0.0)

    # Override with project where project has data
    if project:
        inherit = project.get("inherit_overall", True)
        for d in TASTE_DIMENSIONS:
            proj_val = project.get("dimensions", {}).get(d, 0.0)
            proj_conf = project.get("confidence", {}).get(d, 0.0)

            if proj_conf > 0 or not inherit:
                if proj_conf >= confidence[d] or not inherit:
                    result[d] = proj_val
                    confidence[d] = proj_conf
                else:
                    # Weighted blend: higher confidence wins more
                    total = confidence[d] + proj_conf
                    if total > 0:
                        w_overall = confidence[d] / total
                        w_project = proj_conf / total
                        result[d] = round(result[d] * w_overall + proj_val * w_project, 2)
                        confidence[d] = max(confidence[d], proj_conf)

    return {"dimensions": result, "confidence": confidence}


def generate_taste_summary(profile: dict) -> str:
    """Generate a human-readable taste summary.

    Example: "Your style leans cinematic, warm, intimate, polished, and controlled."
    """
    dims = profile.get("dimensions", {})
    if isinstance(profile.get("confidence"), dict):
        conf = profile["confidence"]
    else:
        conf = {d: 0.5 for d in TASTE_DIMENSIONS}

    descriptors = []
    for dim in TASTE_DIMENSIONS:
        val = dims.get(dim, 0.0)
        c = conf.get(dim, 0.0)
        if c < 0.2 or abs(val) < 0.2:
            continue  # too uncertain or too neutral

        labels = DIMENSION_LABELS.get(dim, ("Low", "High"))
        if val < -0.5:
            descriptors.append(labels[0].lower())
        elif val < -0.2:
            descriptors.append(f"slightly {labels[0].lower()}")
        elif val > 0.5:
            descriptors.append(labels[1].lower())
        elif val > 0.2:
            descriptors.append(f"slightly {labels[1].lower()}")

    if not descriptors:
        return "No strong style preferences detected yet."

    joined = ", ".join(descriptors[:-1]) + f", and {descriptors[-1]}" if len(descriptors) > 1 else descriptors[0]
    return f"Your style leans {joined}."


# ---------------------------------------------------------------------------
# Planner Integration
# ---------------------------------------------------------------------------

def taste_to_prompt_modifiers(blended: dict) -> dict:
    """Convert blended taste profile into prompt modifier strings.

    Returns a dict of modifier strings that can be appended to generation prompts.
    """
    dims = blended.get("dimensions", {})
    mods = {}

    # Lighting
    val = dims.get("lighting", 0)
    if val < -0.4:
        mods["lighting"] = "warm golden lighting, amber tones"
    elif val > 0.4:
        mods["lighting"] = "cool blue lighting, steel tones"
    elif val < -0.2:
        mods["lighting"] = "warm natural lighting"
    elif val > 0.2:
        mods["lighting"] = "cool natural lighting"

    # Texture
    val = dims.get("texture", 0)
    if val < -0.4:
        mods["texture"] = "clean smooth surfaces, no grain"
    elif val > 0.4:
        mods["texture"] = "film grain, textured gritty surfaces"

    # Realism
    val = dims.get("realism", 0)
    if val < -0.4:
        mods["realism"] = "polished cinematic quality, high production value"
    elif val > 0.4:
        mods["realism"] = "raw documentary feel, natural imperfections"

    # Composition
    val = dims.get("composition", 0)
    if val < -0.4:
        mods["composition"] = "centered symmetrical framing"
    elif val > 0.4:
        mods["composition"] = "dynamic off-center framing"

    # Density
    val = dims.get("density", 0)
    if val < -0.4:
        mods["density"] = "minimal composition, negative space"
    elif val > 0.4:
        mods["density"] = "dense layered composition, rich detail"

    # Tone
    val = dims.get("tone", 0)
    if val < -0.4:
        mods["tone"] = "dramatic cinematic atmosphere"
    elif val > 0.4:
        mods["tone"] = "raw unfiltered reality"

    return mods


def taste_to_pacing_bias(blended: dict) -> dict:
    """Convert taste into pacing preferences for the shot planner.

    Returns modifiers for duration scaling, cut density, and motion.
    """
    dims = blended.get("dimensions", {})
    pacing_val = dims.get("pacing", 0)
    framing_val = dims.get("framing", 0)

    return {
        # Duration scale: calm = longer shots, aggressive = shorter
        "duration_scale": 1.0 + pacing_val * -0.2,
        # Cut density: calm = fewer cuts, aggressive = more
        "cut_density": 1.0 + pacing_val * 0.3,
        # Close-up bias: intimate = more close-ups, epic = more wides
        "closeup_bias": max(0.0, -framing_val * 0.3),
        "wide_bias": max(0.0, framing_val * 0.3),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TasteStore:
    """JSON-file-backed storage for taste profiles."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.profiles_path = os.path.join(data_dir, "taste_profiles.json")
        os.makedirs(data_dir, exist_ok=True)

    def _load(self) -> dict:
        if os.path.isfile(self.profiles_path):
            try:
                with open(self.profiles_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {"overall": None, "projects": {}}

    def _save(self, data: dict):
        with open(self.profiles_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_overall(self) -> dict | None:
        return self._load().get("overall")

    def save_overall(self, profile: dict):
        data = self._load()
        profile["is_overall"] = True
        data["overall"] = profile
        self._save(data)

    def get_project_profile(self, project_id: str) -> dict | None:
        return self._load().get("projects", {}).get(project_id)

    def save_project_profile(self, project_id: str, profile: dict):
        data = self._load()
        profile["project_id"] = project_id
        profile["is_overall"] = False
        data.setdefault("projects", {})[project_id] = profile
        self._save(data)

    def get_blended(self, project_id: str = None) -> dict:
        """Get the effective blended profile for a project."""
        overall = self.get_overall()
        project = self.get_project_profile(project_id) if project_id else None
        return blend_profiles(overall, project)

    def delete_project_profile(self, project_id: str):
        data = self._load()
        data.get("projects", {}).pop(project_id, None)
        self._save(data)

    def reset_overall(self):
        data = self._load()
        data["overall"] = None
        self._save(data)
