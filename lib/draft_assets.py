"""
Draft Asset Manager for LUMN Studio.

Assets have 3 states:
- generic: loose text, not reusable, OK for background elements
- draft: detected during planning, not yet confirmed, can be promoted
- library: confirmed POS entity, creation-ready, selectable across scenes

Draft assets are stored in output/draft_assets.json.
Library assets live in POS (prompt_os).
"""

import json
import os
import uuid


DRAFT_ASSETS_PATH = None  # Set by init()


def init(output_dir):
    """Initialize draft asset storage path."""
    global DRAFT_ASSETS_PATH
    DRAFT_ASSETS_PATH = os.path.join(output_dir, "draft_assets.json")


def _load():
    """Load draft assets from disk."""
    if DRAFT_ASSETS_PATH and os.path.isfile(DRAFT_ASSETS_PATH):
        try:
            with open(DRAFT_ASSETS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"characters": [], "costumes": [], "environments": []}


def _save(data):
    """Persist draft assets to disk."""
    if DRAFT_ASSETS_PATH:
        with open(DRAFT_ASSETS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def _type_key(asset_type):
    """Normalize asset type to plural key: character -> characters."""
    return {
        "character": "characters",
        "costume": "costumes",
        "environment": "environments",
    }.get(asset_type, asset_type)


def create_draft(asset_type, label, source="auto_plan", metadata=None):
    """Create a draft asset. Returns the draft asset dict."""
    key = _type_key(asset_type)
    canonical_type = {"characters": "character", "costumes": "costume", "environments": "environment"}.get(key, asset_type)

    asset = {
        "id": f"draft_{canonical_type[:4]}_{uuid.uuid4().hex[:8]}",
        "type": canonical_type,
        "label": label,
        "name": label,  # alias for UI consistency
        "state": "draft",
        "source": source,
        "metadata": metadata or {},
        "linked_scene_indices": [],
    }
    data = _load()
    data.setdefault(key, []).append(asset)
    _save(data)
    return asset


def get_all_drafts():
    """Get all draft assets as {characters, costumes, environments}."""
    return _load()


def get_drafts_by_type(asset_type):
    """Get draft assets for a specific type."""
    return _load().get(_type_key(asset_type), [])


def get_draft(draft_id):
    """Get a single draft by ID."""
    data = _load()
    for key in ("characters", "costumes", "environments"):
        for asset in data.get(key, []):
            if asset["id"] == draft_id:
                return asset
    return None


def update_draft(draft_id, updates):
    """Update a draft asset's metadata. Returns updated asset or None."""
    data = _load()
    for key in ("characters", "costumes", "environments"):
        for i, asset in enumerate(data.get(key, [])):
            if asset["id"] == draft_id:
                for k, v in updates.items():
                    if k not in ("id", "type", "state"):
                        asset[k] = v
                if "label" in updates:
                    asset["name"] = updates["label"]
                data[key][i] = asset
                _save(data)
                return asset
    return None


def promote_to_library(draft_id, prompt_os_instance):
    """Promote a draft asset to a library (POS) entity.

    Returns (new_pos_entity, old_draft_id) or (None, None).
    """
    draft = get_draft(draft_id)
    if not draft:
        return None, None

    asset_type = draft["type"]
    label = draft.get("label", draft.get("name", "Unnamed"))
    metadata = draft.get("metadata", {})

    entity = None
    if asset_type == "character":
        entity = prompt_os_instance.create_character({
            "name": label,
            "physicalDescription": metadata.get("description", label),
            "role": metadata.get("role", ""),
        })
    elif asset_type == "costume":
        entity = prompt_os_instance.create_costume({
            "name": label,
            "description": metadata.get("description", label),
        })
    elif asset_type == "environment":
        entity = prompt_os_instance.create_environment({
            "name": label,
            "description": metadata.get("description", label),
            "locationType": metadata.get("location_type", ""),
        })

    if entity:
        remove_draft(draft_id)
        return entity, draft_id

    return None, None


def remove_draft(draft_id):
    """Remove a draft asset by ID."""
    data = _load()
    for key in ("characters", "costumes", "environments"):
        data[key] = [a for a in data.get(key, []) if a["id"] != draft_id]
    _save(data)


def clear_all():
    """Clear all draft assets (used on project reset)."""
    _save({"characters": [], "costumes": [], "environments": []})


def extract_drafts_from_plan(scenes, pos_char_ids, pos_costume_ids, pos_env_ids):
    """Extract draft assets from a generated plan.

    Looks at scene asset assignments and creates drafts for any that
    don't have a real POS ID (i.e., were invented during planning).

    Args:
        scenes: list of scene dicts
        pos_char_ids: set of known POS character IDs
        pos_costume_ids: set of known POS costume IDs
        pos_env_ids: set of known POS environment IDs

    Returns: {characters: [...], costumes: [...], environments: []}
    """
    drafts_created = {"characters": [], "costumes": [], "environments": []}
    seen = set()

    for scene in scenes:
        # Characters
        for char in scene.get("characters", []):
            char_id = char.get("id", "")
            char_name = char.get("name", "")
            if not char_name:
                continue
            key = ("character", char_name.lower())
            if key in seen:
                continue
            seen.add(key)
            if char_id and char_id in pos_char_ids:
                char["state"] = "library"
                continue
            if char_id and char_id.startswith("draft_"):
                # Already a draft, keep it
                existing = get_draft(char_id)
                if existing:
                    char["state"] = "draft"
                    continue
            # Create new draft
            draft = create_draft("character", char_name, "auto_plan", {
                "description": char.get("description", ""),
                "role": char.get("role_in_scene", ""),
            })
            drafts_created["characters"].append(draft)
            char["id"] = draft["id"]
            char["state"] = "draft"

        # Costumes
        for cos in scene.get("costumes", []):
            cos_id = cos.get("id", "")
            cos_name = cos.get("name", "")
            if not cos_name:
                continue
            key = ("costume", cos_name.lower())
            if key in seen:
                continue
            seen.add(key)
            if cos_id and cos_id in pos_costume_ids:
                cos["state"] = "library"
                continue
            if cos_id and cos_id.startswith("draft_"):
                existing = get_draft(cos_id)
                if existing:
                    cos["state"] = "draft"
                    continue
            draft = create_draft("costume", cos_name, "auto_plan", {
                "description": cos.get("description", ""),
            })
            drafts_created["costumes"].append(draft)
            cos["id"] = draft["id"]
            cos["state"] = "draft"

        # Environments
        for env in scene.get("environments", []):
            env_id = env.get("id", "")
            env_name = env.get("name", "")
            if not env_name:
                continue
            key = ("environment", env_name.lower())
            if key in seen:
                continue
            seen.add(key)
            if env_id and env_id in pos_env_ids:
                env["state"] = "library"
                continue
            if env_id and env_id.startswith("draft_"):
                existing = get_draft(env_id)
                if existing:
                    env["state"] = "draft"
                    continue
            draft = create_draft("environment", env_name, "auto_plan", {
                "description": env.get("description", ""),
            })
            drafts_created["environments"].append(draft)
            env["id"] = draft["id"]
            env["state"] = "draft"

    return drafts_created


def replace_draft_id_in_scenes(scenes, old_draft_id, new_library_id, new_name=None):
    """Replace a draft ID with a library ID across all scenes.

    Used after promoting or resolving a draft asset.
    """
    count = 0
    for scene in scenes:
        for asset_type in ("characters", "costumes", "environments"):
            for asset in scene.get(asset_type, []):
                if asset.get("id") == old_draft_id:
                    asset["id"] = new_library_id
                    asset["state"] = "library"
                    if new_name:
                        asset["name"] = new_name
                    count += 1
    return count


def creation_readiness(scenes):
    """Check if all creation-critical assets are resolved.

    Returns {ready: bool, issues: [...], summary: str}
    """
    issues = []

    for i, scene in enumerate(scenes):
        for char in scene.get("characters", []):
            state = char.get("state", "")
            if state in ("draft", "generic") or (char.get("id", "").startswith("draft_")):
                issues.append({
                    "scene": i,
                    "type": "character",
                    "name": char.get("name", "?"),
                    "state": state or "draft",
                    "id": char.get("id", ""),
                })
        for cos in scene.get("costumes", []):
            state = cos.get("state", "")
            if state == "draft" or cos.get("id", "").startswith("draft_"):
                issues.append({
                    "scene": i,
                    "type": "costume",
                    "name": cos.get("name", "?"),
                    "state": state or "draft",
                    "id": cos.get("id", ""),
                })
        for env in scene.get("environments", []):
            state = env.get("state", "")
            if state == "draft" or env.get("id", "").startswith("draft_"):
                issues.append({
                    "scene": i,
                    "type": "environment",
                    "name": env.get("name", "?"),
                    "state": state or "draft",
                    "id": env.get("id", ""),
                })

    # Deduplicate by id
    seen_ids = set()
    unique_issues = []
    for issue in issues:
        if issue["id"] not in seen_ids:
            seen_ids.add(issue["id"])
            unique_issues.append(issue)

    ready = len(unique_issues) == 0
    summary = "All assets resolved" if ready else f"{len(unique_issues)} unresolved draft asset(s)"

    return {"ready": ready, "issues": unique_issues, "summary": summary}
