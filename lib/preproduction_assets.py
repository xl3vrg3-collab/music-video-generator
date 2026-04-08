"""
Preproduction Asset Pipeline for LUMN Studio.

Sits between story/beat planning and final shot generation.  Generates or
accepts structured visual reference packages for Characters, Costumes,
Environments, and Props — so V4 shots are driven by approved visual assets
instead of text-only descriptions.

Flow:
    story plan → beat expansion → **preproduction sheets** → user review
    → canonical hero ref selection → shot binding → video generation

Supports two modes:
    - Fast:       minimal sheets (3-4 views per package)
    - Production: full sheets (6-10 views per package)

No external dependencies beyond stdlib + existing LUMN modules.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACKAGE_TYPES = ("character", "costume", "environment", "prop")

STATUS_FLOW = ("draft", "generating", "generated", "approved", "rejected", "missing")

# Sheet view definitions per package type and mode
SHEET_VIEWS = {
    "character": {
        "fast": [
            {"view": "hero_front",       "label": "Full Body Front",    "prompt_suffix": "full body front view, standing pose, clean background"},
            {"view": "face_closeup",     "label": "Face Close-Up",      "prompt_suffix": "tight face close-up portrait, detailed features, neutral expression"},
            {"view": "three_quarter",    "label": "3/4 View",           "prompt_suffix": "full body three-quarter left view, natural pose"},
        ],
        "production": [
            {"view": "hero_front",       "label": "Full Body Front",    "prompt_suffix": "full body front view, standing pose, clean background"},
            {"view": "three_quarter_l",  "label": "3/4 Left",           "prompt_suffix": "full body three-quarter left view, natural pose"},
            {"view": "three_quarter_r",  "label": "3/4 Right",          "prompt_suffix": "full body three-quarter right view, natural pose"},
            {"view": "side_profile",     "label": "Side Profile",       "prompt_suffix": "full body side profile view, clean background"},
            {"view": "face_closeup",     "label": "Face Close-Up",      "prompt_suffix": "tight face close-up portrait, detailed features, neutral expression"},
            {"view": "back_view",        "label": "Back View",          "prompt_suffix": "full body back view, standing pose"},
            {"view": "expr_focused",     "label": "Expression: Focused","prompt_suffix": "face close-up, intense focused expression"},
            {"view": "expr_joyful",      "label": "Expression: Joyful", "prompt_suffix": "face close-up, warm joyful smile"},
            {"view": "expr_sad",         "label": "Expression: Sad",    "prompt_suffix": "face close-up, melancholy sad expression"},
        ],
    },
    "costume": {
        "fast": [
            {"view": "front",           "label": "Front View",          "prompt_suffix": "full outfit front view, standing pose, clean background"},
            {"view": "detail_crop",     "label": "Detail Crop",         "prompt_suffix": "close-up detail of distinctive features, fabric texture, accessories"},
        ],
        "production": [
            {"view": "front",           "label": "Front View",          "prompt_suffix": "full outfit front view, standing pose, clean background"},
            {"view": "side",            "label": "Side View",           "prompt_suffix": "full outfit side view, standing pose"},
            {"view": "back",            "label": "Back View",           "prompt_suffix": "full outfit back view, showing rear details"},
            {"view": "natural_pose",    "label": "Natural Pose",        "prompt_suffix": "natural standing pose showing how outfit moves and drapes"},
            {"view": "collar_detail",   "label": "Collar/Neckline",     "prompt_suffix": "close-up of collar, neckline, and upper body details"},
            {"view": "shoes_detail",    "label": "Shoes/Footwear",      "prompt_suffix": "close-up of shoes, footwear, and lower leg details"},
            {"view": "accessory_detail","label": "Accessories",         "prompt_suffix": "close-up of accessories, patches, logos, jewelry details"},
            {"view": "movement_pose",   "label": "Movement Pose",       "prompt_suffix": "dynamic movement pose showing outfit in motion"},
        ],
    },
    "environment": {
        "fast": [
            {"view": "wide_establish",  "label": "Wide Establishing",   "prompt_suffix": "wide establishing shot, full environment visible"},
            {"view": "medium_angle",    "label": "Medium Angle",        "prompt_suffix": "medium angle view showing key environment features"},
        ],
        "production": [
            {"view": "wide_establish",  "label": "Wide Establishing",   "prompt_suffix": "wide establishing shot, full environment visible"},
            {"view": "medium_angle",    "label": "Medium Angle",        "prompt_suffix": "medium angle view showing key environment features"},
            {"view": "low_angle",       "label": "Low Angle",           "prompt_suffix": "low angle view looking upward, dramatic perspective"},
            {"view": "reverse_angle",   "label": "Reverse Angle",       "prompt_suffix": "reverse angle showing the opposite view of the environment"},
            {"view": "empty_plate",     "label": "Clean Plate",         "prompt_suffix": "clean empty environment, no characters, full detail"},
            {"view": "golden_hour",     "label": "Golden Hour",         "prompt_suffix": "golden hour lighting variant, warm sunset tones"},
            {"view": "night_variant",   "label": "Night Variant",       "prompt_suffix": "nighttime lighting variant, dramatic shadows"},
            {"view": "texture_detail",  "label": "Texture Detail",      "prompt_suffix": "close-up of surface textures, materials, repeated details"},
        ],
    },
    "prop": {
        "fast": [
            {"view": "hero_angle",      "label": "Hero Angle",          "prompt_suffix": "hero angle product shot, clean background, full detail"},
            {"view": "detail_closeup",  "label": "Detail Close-Up",     "prompt_suffix": "extreme close-up showing fine details and texture"},
        ],
        "production": [
            {"view": "hero_angle",      "label": "Hero Angle",          "prompt_suffix": "hero angle product shot, clean background, full detail"},
            {"view": "side_angle",      "label": "Side Angle",          "prompt_suffix": "side angle view showing profile and proportions"},
            {"view": "detail_closeup",  "label": "Detail Close-Up",     "prompt_suffix": "extreme close-up showing fine details and texture"},
            {"view": "in_use",          "label": "In-Use Variant",      "prompt_suffix": "being held or used naturally, showing scale and interaction"},
            {"view": "worn_variant",    "label": "Worn/Aged Variant",   "prompt_suffix": "showing wear, age, or distressed state if relevant"},
        ],
    },
}

# Which views are REQUIRED for production-mode validation
REQUIRED_VIEWS = {
    "character": {"hero_front", "face_closeup", "side_profile"},
    "costume":   {"front"},
    "environment": {"wide_establish", "medium_angle"},
    "prop":      {"hero_angle"},
}

# Default hero view per type
DEFAULT_HERO_VIEW = {
    "character": "hero_front",
    "costume":   "front",
    "environment": "wide_establish",
    "prop":      "hero_angle",
}


# ---------------------------------------------------------------------------
# Asset Package Model
# ---------------------------------------------------------------------------

def create_package(
    package_type: str,
    name: str,
    description: str = "",
    mode: str = "fast",
    related_ids: dict = None,
    must_keep: list = None,
    avoid: list = None,
    canonical_notes: list = None,
    lock_strength: float = 0.8,
) -> dict:
    """Create a new asset package (not yet generated).

    Args:
        package_type: one of character/costume/environment/prop
        name: human-readable name
        description: text description used for prompt generation
        mode: "fast" or "production"
        related_ids: dict of related IDs (character_id, costume_id, etc.)
        must_keep: list of visual features that MUST be preserved
        avoid: list of visual features to avoid
        canonical_notes: list of production notes
        lock_strength: 0-1 how strictly to enforce this package in prompts

    Returns:
        package dict in "draft" status
    """
    if package_type not in PACKAGE_TYPES:
        raise ValueError(f"Invalid package_type: {package_type}. Must be one of {PACKAGE_TYPES}")
    if mode not in ("fast", "production"):
        raise ValueError(f"Invalid mode: {mode}. Must be 'fast' or 'production'")

    pkg_id = f"pkg_{package_type[:4]}_{uuid.uuid4().hex[:8]}"
    views = SHEET_VIEWS[package_type][mode]

    return {
        "package_id": pkg_id,
        "package_type": package_type,
        "name": name,
        "description": description,
        "prompt_used": "",
        "mode": mode,

        # Sheet images — one entry per view in the sheet plan
        "sheet_images": [
            {
                "view": v["view"],
                "label": v["label"],
                "image_path": None,
                "status": "pending",  # pending/generating/generated/failed
                "seed": None,
                "prompt_used": "",
            }
            for v in views
        ],

        # Canonical selections
        "hero_image_path": None,
        "hero_view": None,
        "alternate_image_paths": [],
        "detail_crops": [],

        # Production notes
        "canonical_notes": canonical_notes or [],
        "must_keep": must_keep or [],
        "avoid": avoid or [],
        "lock_strength": lock_strength,

        # Generation metadata
        "seed": None,
        "generation_metadata": {},

        # Status
        "status": "draft",

        # Relationships
        "related_character_id": (related_ids or {}).get("character_id"),
        "related_costume_id": (related_ids or {}).get("costume_id"),
        "related_environment_id": (related_ids or {}).get("environment_id"),
        "related_prop_id": (related_ids or {}).get("prop_id"),
    }


# ---------------------------------------------------------------------------
# Sheet Generation
# ---------------------------------------------------------------------------

def build_sheet_prompt(package: dict, view_def: dict) -> str:
    """Build a generation prompt for a single sheet view.

    Combines the package description, must_keep/avoid rules, and the
    view-specific suffix into a prompt under 1000 chars.
    """
    parts = []

    # Base description
    desc = package.get("description", package.get("name", ""))
    if desc:
        parts.append(desc.strip())

    # Must-keep features
    keeps = package.get("must_keep", [])
    if keeps:
        parts.append(f"Must include: {', '.join(keeps)}")

    # View-specific direction
    suffix = view_def.get("prompt_suffix", "")
    if suffix:
        parts.append(suffix)

    # Avoid rules
    avoids = package.get("avoid", [])
    if avoids:
        parts.append(f"Do not include: {', '.join(avoids)}")

    # Style consistency
    pkg_type = package.get("package_type", "")
    if pkg_type == "character":
        parts.append("Character reference sheet style, consistent lighting, white/neutral background")
    elif pkg_type == "costume":
        parts.append("Costume reference sheet, clean lighting, neutral background")
    elif pkg_type == "environment":
        parts.append("Cinematic environment concept art, high detail")
    elif pkg_type == "prop":
        parts.append("Product photography style, studio lighting, clean background")

    prompt = ". ".join(parts)
    return prompt[:1000]


def get_sheet_plan(package: dict) -> list[dict]:
    """Return the list of view definitions for a package's mode."""
    pkg_type = package.get("package_type", "character")
    mode = package.get("mode", "fast")
    return SHEET_VIEWS.get(pkg_type, {}).get(mode, [])


def update_sheet_image(package: dict, view: str, image_path: str,
                       seed: int = None, prompt_used: str = "") -> dict:
    """Update a specific sheet view's image after generation."""
    for img in package.get("sheet_images", []):
        if img["view"] == view:
            img["image_path"] = image_path
            img["status"] = "generated" if image_path else "failed"
            img["seed"] = seed
            img["prompt_used"] = prompt_used
            break

    # Auto-select hero ref if this is the default hero view and no hero set yet
    default_hero = DEFAULT_HERO_VIEW.get(package.get("package_type", ""))
    if view == default_hero and not package.get("hero_image_path") and image_path:
        package["hero_image_path"] = image_path
        package["hero_view"] = view

    return package


def select_hero_ref(package: dict, view: str) -> dict:
    """Set the canonical hero reference image for a package."""
    for img in package.get("sheet_images", []):
        if img["view"] == view and img.get("image_path"):
            # Demote current hero to alternate
            if package.get("hero_image_path"):
                old = package["hero_image_path"]
                if old not in package.get("alternate_image_paths", []):
                    package.setdefault("alternate_image_paths", []).append(old)

            package["hero_image_path"] = img["image_path"]
            package["hero_view"] = view

            # Remove from alternates if present
            alts = package.get("alternate_image_paths", [])
            if img["image_path"] in alts:
                alts.remove(img["image_path"])
            break
    return package


def approve_package(package: dict) -> dict:
    """Mark a package as approved for production use."""
    if not package.get("hero_image_path"):
        raise ValueError(f"Cannot approve package '{package.get('name')}': no hero ref selected")
    package["status"] = "approved"
    return package


def reject_package(package: dict, reason: str = "") -> dict:
    """Mark a package as rejected."""
    package["status"] = "rejected"
    if reason:
        package.setdefault("canonical_notes", []).append(f"Rejected: {reason}")
    return package


# ---------------------------------------------------------------------------
# Package Validation
# ---------------------------------------------------------------------------

def validate_package_completeness(package: dict) -> dict:
    """Check if a package has all required views generated.

    Returns: {complete: bool, missing_views: [...], warnings: [...]}
    """
    pkg_type = package.get("package_type", "")
    required = REQUIRED_VIEWS.get(pkg_type, set())

    generated_views = set()
    for img in package.get("sheet_images", []):
        if img.get("image_path") and img.get("status") == "generated":
            generated_views.add(img["view"])

    missing = required - generated_views
    warnings = []

    if not package.get("hero_image_path"):
        warnings.append("No hero reference image selected")

    if package.get("status") not in ("approved", "generated"):
        warnings.append(f"Package status is '{package.get('status', 'unknown')}', not approved")

    return {
        "complete": len(missing) == 0,
        "missing_views": sorted(missing),
        "generated_views": sorted(generated_views),
        "warnings": warnings,
    }


def validate_preproduction(packages: list[dict], shots: list[dict],
                           mode: str = "fast") -> dict:
    """Validate all preproduction packages against shot requirements.

    Production mode: hard-fail on missing main character/environment packages.
    Fast mode: warn instead of fail.

    Returns: {ready: bool, errors: [...], warnings: [...]}
    """
    errors = []
    warnings = []

    # Index packages by type and name/id
    pkg_by_type = {}
    pkg_by_id = {}
    for pkg in packages:
        t = pkg.get("package_type", "")
        pkg_by_type.setdefault(t, []).append(pkg)
        pkg_by_id[pkg["package_id"]] = pkg

    # Collect unique subjects and environments from shots
    subjects = set()
    environments = set()
    props_referenced = set()
    for shot in shots:
        subj = shot.get("subject", "")
        if subj:
            subjects.add(subj)
        env = shot.get("environmentName", "")
        if env:
            environments.add(env)

    # Check character coverage
    char_names = {p.get("name", "").lower() for p in pkg_by_type.get("character", [])}
    approved_chars = {p.get("name", "").lower() for p in pkg_by_type.get("character", [])
                      if p.get("status") == "approved"}
    for subj in subjects:
        if subj.lower() not in char_names:
            msg = f"Character '{subj}' has no preproduction package"
            if mode == "production":
                errors.append(msg)
            else:
                warnings.append(msg)
        elif subj.lower() not in approved_chars and mode == "production":
            warnings.append(f"Character '{subj}' package not yet approved")

    # Check environment coverage
    env_names = {p.get("name", "").lower() for p in pkg_by_type.get("environment", [])}
    approved_envs = {p.get("name", "").lower() for p in pkg_by_type.get("environment", [])
                     if p.get("status") == "approved"}
    for env in environments:
        if env.lower() not in env_names:
            msg = f"Environment '{env}' has no preproduction package"
            if mode == "production":
                errors.append(msg)
            else:
                warnings.append(msg)

    # Check per-package completeness
    for pkg in packages:
        check = validate_package_completeness(pkg)
        if not check["complete"]:
            missing = ", ".join(check["missing_views"])
            msg = f"{pkg['package_type'].title()} '{pkg['name']}' missing views: {missing}"
            if mode == "production" and pkg["package_type"] in ("character", "environment"):
                errors.append(msg)
            else:
                warnings.append(msg)

    return {
        "ready": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Shot Binding
# ---------------------------------------------------------------------------

def bind_shots_to_packages(shots: list[dict], packages: list[dict]) -> list[dict]:
    """Bind each shot to its matching preproduction packages.

    Matches by name (case-insensitive fuzzy) between shot subject/environment
    and package names.  Updates shot dicts in-place and returns them.
    """
    # Build lookup indices
    char_pkgs = {}
    costume_pkgs = {}
    env_pkgs = {}
    prop_pkgs = {}

    for pkg in packages:
        name_lower = pkg.get("name", "").lower()
        t = pkg.get("package_type", "")
        if t == "character":
            char_pkgs[name_lower] = pkg
        elif t == "costume":
            costume_pkgs[name_lower] = pkg
            # Also index by related character
            rel_char = pkg.get("related_character_id")
            if rel_char:
                costume_pkgs[f"_rel_{rel_char}"] = pkg
        elif t == "environment":
            env_pkgs[name_lower] = pkg
        elif t == "prop":
            prop_pkgs[name_lower] = pkg

    for shot in shots:
        # Character binding
        subject = (shot.get("subject") or "").lower()
        if subject and subject in char_pkgs:
            pkg = char_pkgs[subject]
            shot["character_package_id"] = pkg["package_id"]
            # Also check for costume by character relationship
            rel_key = f"_rel_{pkg['package_id']}"
            if rel_key in costume_pkgs:
                shot["costume_package_id"] = costume_pkgs[rel_key]["package_id"]

        # Environment binding
        env_name = (shot.get("environmentName") or "").lower()
        if env_name and env_name in env_pkgs:
            shot["environment_package_id"] = env_pkgs[env_name]["package_id"]

        # Costume binding by name if not already bound
        if not shot.get("costume_package_id"):
            char_name = (shot.get("characterName") or shot.get("subject") or "").lower()
            # Look for costume with same name or character name prefix
            for cname, cpkg in costume_pkgs.items():
                if cname.startswith("_rel_"):
                    continue
                if char_name and (char_name in cname or cname in char_name):
                    shot["costume_package_id"] = cpkg["package_id"]
                    break

    return shots


def get_shot_references(shot: dict, packages: list[dict]) -> list[dict]:
    """Get reference photos for a shot from its bound packages.

    Returns list of {path, tag, type} dicts suitable for
    _runway_generate_scene_image's reference_photos param.
    Max 3 refs (Runway API limit).
    """
    pkg_index = {p["package_id"]: p for p in packages}
    refs = []

    # Priority: character > costume > environment (max 3 total)
    char_pkg_id = shot.get("character_package_id")
    if char_pkg_id and char_pkg_id in pkg_index:
        pkg = pkg_index[char_pkg_id]
        hero = pkg.get("hero_image_path")
        if hero and os.path.isfile(hero):
            refs.append({"path": hero, "tag": "Character", "type": "character"})

    costume_pkg_id = shot.get("costume_package_id")
    if costume_pkg_id and costume_pkg_id in pkg_index:
        pkg = pkg_index[costume_pkg_id]
        hero = pkg.get("hero_image_path")
        if hero and os.path.isfile(hero):
            refs.append({"path": hero, "tag": "Costume", "type": "costume"})

    env_pkg_id = shot.get("environment_package_id")
    if env_pkg_id and env_pkg_id in pkg_index:
        pkg = pkg_index[env_pkg_id]
        hero = pkg.get("hero_image_path")
        if hero and os.path.isfile(hero):
            refs.append({"path": hero, "tag": "Setting", "type": "environment"})

    # Runway allows max 3 referenceImages
    return refs[:3]


def get_shot_package_notes(shot: dict, packages: list[dict]) -> dict:
    """Collect must_keep/avoid/canonical_notes from all bound packages."""
    pkg_index = {p["package_id"]: p for p in packages}
    must_keep = []
    avoid = []
    notes = []

    for field in ("character_package_id", "costume_package_id",
                  "environment_package_id"):
        pkg_id = shot.get(field)
        if pkg_id and pkg_id in pkg_index:
            pkg = pkg_index[pkg_id]
            must_keep.extend(pkg.get("must_keep", []))
            avoid.extend(pkg.get("avoid", []))
            notes.extend(pkg.get("canonical_notes", []))

    for pkg_id in shot.get("prop_package_ids", []):
        if pkg_id in pkg_index:
            pkg = pkg_index[pkg_id]
            must_keep.extend(pkg.get("must_keep", []))
            avoid.extend(pkg.get("avoid", []))

    return {
        "must_keep": list(dict.fromkeys(must_keep)),  # dedupe preserving order
        "avoid": list(dict.fromkeys(avoid)),
        "notes": list(dict.fromkeys(notes)),
    }


# ---------------------------------------------------------------------------
# Package Persistence
# ---------------------------------------------------------------------------

class PreproductionStore:
    """JSON-file-backed storage for preproduction packages."""

    def __init__(self, output_dir: str):
        self.base_dir = os.path.join(output_dir, "preproduction")
        self.index_path = os.path.join(self.base_dir, "packages.json")
        os.makedirs(self.base_dir, exist_ok=True)

    def _load(self) -> dict:
        if os.path.isfile(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {"packages": [], "mode": "fast"}

    def _save(self, data: dict):
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_all(self) -> list[dict]:
        return self._load().get("packages", [])

    def get_by_type(self, package_type: str) -> list[dict]:
        return [p for p in self.get_all() if p.get("package_type") == package_type]

    def get_by_id(self, package_id: str) -> dict | None:
        for p in self.get_all():
            if p.get("package_id") == package_id:
                return p
        return None

    def save_package(self, package: dict):
        """Insert or update a package."""
        data = self._load()
        pkgs = data.get("packages", [])
        found = False
        for i, p in enumerate(pkgs):
            if p.get("package_id") == package.get("package_id"):
                pkgs[i] = package
                found = True
                break
        if not found:
            pkgs.append(package)
        data["packages"] = pkgs
        self._save(data)

    def remove_package(self, package_id: str):
        data = self._load()
        data["packages"] = [p for p in data.get("packages", [])
                            if p.get("package_id") != package_id]
        self._save(data)

    def get_mode(self) -> str:
        return self._load().get("mode", "fast")

    def set_mode(self, mode: str):
        data = self._load()
        data["mode"] = mode
        self._save(data)

    def package_image_dir(self, package_id: str) -> str:
        """Return (and create) the directory for a package's images."""
        d = os.path.join(self.base_dir, package_id)
        os.makedirs(d, exist_ok=True)
        return d

    def clear(self):
        """Clear all packages."""
        self._save({"packages": [], "mode": "fast"})


# ---------------------------------------------------------------------------
# Preproduction Plan Generation
# ---------------------------------------------------------------------------

def plan_packages_from_beats(beats: list[dict], characters: list[dict],
                             environments: list[dict], mode: str = "fast",
                             existing_packages: list[dict] = None) -> list[dict]:
    """Auto-plan preproduction packages from story beats + asset lists.

    Creates packages for each unique character and environment referenced
    in the plan.  Doesn't duplicate existing packages.
    """
    existing = {(p["package_type"], p["name"].lower())
                for p in (existing_packages or [])}
    new_packages = []

    # Characters
    seen_chars = set()
    for char in (characters or []):
        name = char.get("name", "")
        if not name or name.lower() in seen_chars:
            continue
        seen_chars.add(name.lower())
        if ("character", name.lower()) not in existing:
            desc_parts = []
            phys = char.get("description", char.get("physicalDescription", ""))
            if phys:
                desc_parts.append(phys)
            hair = char.get("hair", "")
            if hair:
                desc_parts.append(hair)
            features = char.get("distinguishingFeatures", "")
            if features:
                desc_parts.append(features)
            pkg = create_package(
                "character", name,
                description=". ".join(desc_parts) if desc_parts else name,
                mode=mode,
                related_ids={"character_id": char.get("id", "")},
            )
            new_packages.append(pkg)

    # Environments
    seen_envs = set()
    for env in (environments or []):
        name = env.get("name", "")
        if not name or name.lower() in seen_envs:
            continue
        seen_envs.add(name.lower())
        if ("environment", name.lower()) not in existing:
            desc_parts = []
            desc = env.get("description", "")
            if desc:
                desc_parts.append(desc)
            lighting = env.get("lighting", "")
            if lighting:
                desc_parts.append(f"Lighting: {lighting}")
            atmosphere = env.get("atmosphere", "")
            if atmosphere:
                desc_parts.append(f"Atmosphere: {atmosphere}")
            pkg = create_package(
                "environment", name,
                description=". ".join(desc_parts) if desc_parts else name,
                mode=mode,
                related_ids={"environment_id": env.get("id", "")},
            )
            new_packages.append(pkg)

    # Also scan beats for any character/environment names not in the asset lists
    for beat in (beats or []):
        for shot in beat.get("shots", []):
            subj = shot.get("subject", "")
            if subj and subj.lower() not in seen_chars:
                seen_chars.add(subj.lower())
                if ("character", subj.lower()) not in existing:
                    pkg = create_package("character", subj, description=subj, mode=mode)
                    new_packages.append(pkg)
            env_name = shot.get("environmentName", "")
            if env_name and env_name.lower() not in seen_envs:
                seen_envs.add(env_name.lower())
                if ("environment", env_name.lower()) not in existing:
                    pkg = create_package("environment", env_name,
                                        description=env_name, mode=mode)
                    new_packages.append(pkg)

    return new_packages


# ---------------------------------------------------------------------------
# Exportable Report
# ---------------------------------------------------------------------------

def generate_preproduction_report(packages: list[dict], shots: list[dict] = None) -> dict:
    """Generate an exportable preproduction report.

    Returns a structured summary of all packages, their status, hero refs,
    lock strengths, notes, and shot bindings.
    """
    report = {
        "total_packages": len(packages),
        "by_type": {},
        "by_status": {},
        "packages": [],
    }

    for pkg in packages:
        t = pkg.get("package_type", "unknown")
        s = pkg.get("status", "unknown")
        report["by_type"][t] = report["by_type"].get(t, 0) + 1
        report["by_status"][s] = report["by_status"].get(s, 0) + 1

        check = validate_package_completeness(pkg)
        n_generated = len(check["generated_views"])
        n_total = len(pkg.get("sheet_images", []))

        pkg_summary = {
            "package_id": pkg["package_id"],
            "type": t,
            "name": pkg.get("name", ""),
            "status": s,
            "mode": pkg.get("mode", "fast"),
            "hero_ref_selected": bool(pkg.get("hero_image_path")),
            "hero_view": pkg.get("hero_view"),
            "views_generated": f"{n_generated}/{n_total}",
            "complete": check["complete"],
            "missing_views": check["missing_views"],
            "lock_strength": pkg.get("lock_strength", 0),
            "must_keep": pkg.get("must_keep", []),
            "avoid": pkg.get("avoid", []),
            "notes": pkg.get("canonical_notes", []),
        }

        # Shot bindings
        if shots:
            id_field = f"{t}_package_id"
            bound_shots = [s.get("shot_id", "") for s in shots
                          if s.get(id_field) == pkg["package_id"]
                          or pkg["package_id"] in s.get("prop_package_ids", [])]
            pkg_summary["bound_to_shots"] = bound_shots
            pkg_summary["shot_count"] = len(bound_shots)

        report["packages"].append(pkg_summary)

    return report
