"""
Prompt Operating System — Core module for managing structured prompt libraries,
characters, costumes, environments, scenes, and prompt assembly.

All data is stored as JSON files in output/prompt_os/.
"""

import json
import os
import re
import threading
import time
import uuid


PROMPT_OS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "prompt_os")

# JSON file paths
MASTER_PROMPTS_PATH = os.path.join(PROMPT_OS_DIR, "master_prompts.json")
CHARACTERS_PATH = os.path.join(PROMPT_OS_DIR, "characters.json")
COSTUMES_PATH = os.path.join(PROMPT_OS_DIR, "costumes.json")
ENVIRONMENTS_PATH = os.path.join(PROMPT_OS_DIR, "environments.json")
SCENES_PATH = os.path.join(PROMPT_OS_DIR, "scenes.json")
STYLE_LOCKS_PATH = os.path.join(PROMPT_OS_DIR, "style_locks.json")
WORLD_RULES_PATH = os.path.join(PROMPT_OS_DIR, "world_rules.json")
CONTINUITY_RULES_PATH = os.path.join(PROMPT_OS_DIR, "continuity_rules.json")
PROPS_PATH = os.path.join(PROMPT_OS_DIR, "props.json")
VOICES_PATH = os.path.join(PROMPT_OS_DIR, "voices.json")
SHEETS_DIR = os.path.join(PROMPT_OS_DIR, "sheets")
PROJECT_STYLE_PATH = os.path.join(PROMPT_OS_DIR, "project_style.json")

# Ensure directories exist
os.makedirs(PROMPT_OS_DIR, exist_ok=True)
os.makedirs(SHEETS_DIR, exist_ok=True)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _gen_id():
    return str(uuid.uuid4())[:12]  # 12 chars = much lower collision risk


def _load_json(path, default=None):
    if default is None:
        default = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return default
    return default


_pos_file_lock = threading.Lock()


def _save_json(path, data):
    with _pos_file_lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


# ─────────────────────────── Variable handling ───────────────────────────

VARIABLE_PATTERN = re.compile(r'\[([A-Z][A-Z0-9_ ]*)\]')


def extract_variables(raw_prompt):
    """Find all [BRACKETED VARIABLES] in a prompt string."""
    if not raw_prompt:
        return []
    return list(set(VARIABLE_PATTERN.findall(raw_prompt)))


def resolve_variables(text, character=None, costume=None, environment=None):
    """Replace [VARIABLES] with entity data. Returns resolved text."""
    if not text:
        return text

    replacements = {}

    if character:
        replacements["CHARACTER NAME"] = character.get("name", "")
        replacements["CHARACTER"] = character.get("name", "")
        replacements["CHARACTER DESCRIPTION"] = character.get("physicalDescription", "")
        replacements["PHYSICAL DESCRIPTION"] = character.get("physicalDescription", "")
        replacements["HAIR"] = character.get("hair", "")
        replacements["SKIN TONE"] = character.get("skinTone", "")
        replacements["BODY TYPE"] = character.get("bodyType", "")
        replacements["DISTINGUISHING FEATURES"] = character.get("distinguishingFeatures", "")
        replacements["DEFAULT EXPRESSION"] = character.get("defaultExpression", "")
        replacements["AGE RANGE"] = character.get("ageRange", "")

    if costume:
        replacements["COSTUME"] = costume.get("name", "")
        replacements["COSTUME NAME"] = costume.get("name", "")
        replacements["COSTUME DESCRIPTION"] = costume.get("description", "")
        replacements["UPPER BODY"] = costume.get("upperBody", "")
        replacements["LOWER BODY"] = costume.get("lowerBody", "")
        replacements["FOOTWEAR"] = costume.get("footwear", "")
        replacements["ACCESSORIES"] = costume.get("accessories", "")
        replacements["COLOR PALETTE"] = costume.get("colorPalette", "")

    if environment:
        replacements["ENVIRONMENT"] = environment.get("name", "")
        replacements["ENVIRONMENT NAME"] = environment.get("name", "")
        replacements["ENVIRONMENT DESCRIPTION"] = environment.get("description", "")
        replacements["LOCATION"] = environment.get("location", "")
        replacements["TIME OF DAY"] = environment.get("timeOfDay", "")
        replacements["WEATHER"] = environment.get("weather", "")
        replacements["LIGHTING"] = environment.get("lighting", "")
        replacements["KEY PROPS"] = environment.get("keyProps", "")
        replacements["ATMOSPHERE"] = environment.get("atmosphere", "")

    def _replace(match):
        var_name = match.group(1)
        if var_name in replacements and replacements[var_name]:
            return replacements[var_name]
        return match.group(0)  # leave unresolved

    return VARIABLE_PATTERN.sub(_replace, text)


# ─────────────────────────── PromptOS Class ───────────────────────────

class PromptOS:
    """Manages the entire Prompt Operating System."""

    # ───── Master Prompts ─────

    def create_prompt(self, data):
        prompts = _load_json(MASTER_PROMPTS_PATH)
        record = {
            "id": _gen_id(),
            "name": data.get("name", "Untitled"),
            "category": data.get("category", "general"),
            "rawPrompt": data.get("rawPrompt", ""),
            "variables": extract_variables(data.get("rawPrompt", "")),
            "tags": data.get("tags", []),
            "isImmutable": data.get("isImmutable", False),
            "version": 1,
            "createdAt": _now(),
            "updatedAt": _now(),
            "notes": data.get("notes", ""),
        }
        prompts.append(record)
        _save_json(MASTER_PROMPTS_PATH, prompts)
        return record

    def get_prompts(self, category=None):
        prompts = _load_json(MASTER_PROMPTS_PATH)
        if category:
            prompts = [p for p in prompts if p.get("category") == category]
        return prompts

    def get_prompt(self, pid):
        for p in _load_json(MASTER_PROMPTS_PATH):
            if p["id"] == pid:
                return p
        return None

    def update_prompt(self, pid, data):
        prompts = _load_json(MASTER_PROMPTS_PATH)
        for i, p in enumerate(prompts):
            if p["id"] == pid:
                if p.get("isImmutable"):
                    return {"error": "Prompt is locked (immutable)"}
                for key in ("name", "category", "rawPrompt", "tags", "isImmutable", "notes"):
                    if key in data:
                        p[key] = data[key]
                if "rawPrompt" in data:
                    p["variables"] = extract_variables(data["rawPrompt"])
                p["version"] = p.get("version", 1) + 1
                p["updatedAt"] = _now()
                prompts[i] = p
                _save_json(MASTER_PROMPTS_PATH, prompts)
                return p
        return None

    def delete_prompt(self, pid):
        prompts = _load_json(MASTER_PROMPTS_PATH)
        new = [p for p in prompts if p["id"] != pid]
        if len(new) == len(prompts):
            return False
        _save_json(MASTER_PROMPTS_PATH, new)
        return True

    # ───── Characters ─────

    def create_character(self, data):
        name = data.get("name", "").strip()
        if not name:
            return {"error": "Name is required"}
        chars = _load_json(CHARACTERS_PATH)
        # Accept both field name formats
        desc = data.get("description", data.get("physicalDescription", data.get("physical", "")))
        record = {
            "id": _gen_id(),
            "name": data.get("name", "Unnamed"),
            "role": data.get("role", ""),
            "description": desc,
            "physicalDescription": desc,
            "hair": data.get("hair", ""),
            "skinTone": data.get("skinTone", ""),
            "bodyType": data.get("bodyType", ""),
            "posture": data.get("posture", ""),
            "outfitDescription": data.get("outfitDescription", ""),
            "accessories": data.get("accessories", []),
            "movementRules": data.get("movementRules", ""),
            "distinguishingFeatures": data.get("distinguishingFeatures", ""),
            "defaultExpression": data.get("defaultExpression", ""),
            "ageRange": data.get("ageRange", ""),
            "referencePhoto": data.get("referencePhoto", ""),
            "previewImage": data.get("previewImage", ""),
            "costumes": data.get("costumes", []),
            "styleOverrides": data.get("styleOverrides", []),
            "tags": data.get("tags", []),
            "linkedPromptIds": data.get("linkedPromptIds", []),
            "createdAt": _now(),
            "updatedAt": _now(),
            "notes": data.get("notes", ""),
            "isCharacterSheet": bool(data.get("isCharacterSheet", False)),
            "approvalState": data.get("approvalState", "draft"),  # draft|generated|selected|approved|locked|archived
            "sheetImages": data.get("sheetImages", []),  # list of {url, type, resolution, generatedAt, model}
            "approvedSheet": data.get("approvedSheet", ""),  # URL of approved sheet image
            "approvedFaceCloseUp": data.get("approvedFaceCloseUp", ""),  # dedicated face close-up
            "approvedHeroPortrait": data.get("approvedHeroPortrait", ""),  # hero portrait
            "approvedFullBody": data.get("approvedFullBody", ""),  # full body reference
            "approvedSideAngle": data.get("approvedSideAngle", ""),  # side view
            "linkedCostumeIds": data.get("linkedCostumeIds", []),
            "linkedPropIds": data.get("linkedPropIds", []),
            "continuityNotes": data.get("continuityNotes", ""),
            "versionHistory": data.get("versionHistory", []),  # list of {version, sheetUrl, timestamp, notes}
            "sourceResolution": data.get("sourceResolution", {}),  # {width, height, format}
        }
        chars.append(record)
        _save_json(CHARACTERS_PATH, chars)
        return record

    def get_characters(self):
        return _load_json(CHARACTERS_PATH)

    def get_character(self, cid):
        for c in _load_json(CHARACTERS_PATH):
            if c["id"] == cid:
                return c
        return None

    def update_character(self, cid, data):
        chars = _load_json(CHARACTERS_PATH)
        for i, c in enumerate(chars):
            if c["id"] == cid:
                for key in ("name", "role", "description", "physicalDescription",
                             "hair", "skinTone", "bodyType", "posture",
                             "outfitDescription", "accessories", "movementRules",
                             "distinguishingFeatures", "defaultExpression", "ageRange",
                             "referencePhoto", "previewImage", "costumes",
                             "styleOverrides", "tags", "linkedPromptIds", "notes",
                             "isCharacterSheet",
                             "approvalState", "sheetImages", "approvedSheet",
                             "approvedFaceCloseUp", "approvedHeroPortrait",
                             "approvedFullBody", "approvedSideAngle",
                             "linkedCostumeIds", "linkedPropIds",
                             "continuityNotes", "versionHistory", "sourceResolution"):
                    if key in data:
                        c[key] = data[key]
                c["updatedAt"] = _now()
                chars[i] = c
                _save_json(CHARACTERS_PATH, chars)
                return c
        return None

    def delete_character(self, cid):
        chars = _load_json(CHARACTERS_PATH)
        new = [c for c in chars if c.get("id") != cid]
        if len(new) == len(chars):
            return False
        _save_json(CHARACTERS_PATH, new)
        # Cascade: remove costumes linked to this character
        costumes = _load_json(COSTUMES_PATH)
        costumes = [c for c in costumes if c.get("characterId") != cid]
        _save_json(COSTUMES_PATH, costumes)
        return True

    # ───── Costumes ─────

    def create_costume(self, data):
        name = data.get("name", "").strip()
        if not name:
            return {"error": "Name is required"}
        costumes = _load_json(COSTUMES_PATH)
        record = {
            "id": _gen_id(),
            "name": data.get("name", "Untitled Costume"),
            "characterId": data.get("characterId", ""),
            "description": data.get("description", ""),
            "upperBody": data.get("upperBody", ""),
            "lowerBody": data.get("lowerBody", ""),
            "footwear": data.get("footwear", ""),
            "accessories": data.get("accessories", ""),
            "colorPalette": data.get("colorPalette", ""),
            "referenceImagePath": data.get("referenceImagePath", ""),
            "previewImage": data.get("previewImage", ""),
            "tags": data.get("tags", []),
            "createdAt": _now(),
            "updatedAt": _now(),
            "notes": data.get("notes", ""),
            "approvalState": data.get("approvalState", "draft"),
            "sheetImages": data.get("sheetImages", []),
            "approvedSheet": data.get("approvedSheet", ""),
            "detailCrops": data.get("detailCrops", []),  # material/detail crop images
            "linkedCharacterIds": data.get("linkedCharacterIds", []),  # multiple characters can wear it
            "linkedAccessoryIds": data.get("linkedAccessoryIds", []),
            "materialNotes": data.get("materialNotes", ""),
            "continuityNotes": data.get("continuityNotes", ""),
            "versionHistory": data.get("versionHistory", []),
            "sourceResolution": data.get("sourceResolution", {}),
        }
        record["referencePhoto"] = record.get("referenceImagePath", "")
        costumes.append(record)
        _save_json(COSTUMES_PATH, costumes)
        return record

    def get_costumes(self, character_id=None):
        costumes = _load_json(COSTUMES_PATH)
        if character_id:
            costumes = [c for c in costumes if c.get("characterId") == character_id]
        return costumes

    def get_costume(self, cid):
        for c in _load_json(COSTUMES_PATH):
            if c["id"] == cid:
                return c
        return None

    def update_costume(self, cid, data):
        costumes = _load_json(COSTUMES_PATH)
        for i, c in enumerate(costumes):
            if c["id"] == cid:
                for key in ("name", "characterId", "description", "upperBody", "lowerBody",
                             "footwear", "accessories", "colorPalette",
                             "referenceImagePath", "previewImage", "tags", "notes",
                             "approvalState", "sheetImages", "approvedSheet",
                             "detailCrops", "linkedCharacterIds", "linkedAccessoryIds",
                             "materialNotes", "continuityNotes", "versionHistory",
                             "sourceResolution"):
                    if key in data:
                        c[key] = data[key]
                c["updatedAt"] = _now()
                costumes[i] = c
                _save_json(COSTUMES_PATH, costumes)
                return c
        return None

    def delete_costume(self, cid):
        costumes = _load_json(COSTUMES_PATH)
        new = [c for c in costumes if c["id"] != cid]
        if len(new) == len(costumes):
            return False
        _save_json(COSTUMES_PATH, new)
        return True

    # ───── Environments ─────

    def create_environment(self, data):
        name = data.get("name", "").strip()
        if not name:
            return {"error": "Name is required"}
        envs = _load_json(ENVIRONMENTS_PATH)
        record = {
            "id": _gen_id(),
            "name": data.get("name", "Untitled Environment"),
            "locationType": data.get("locationType", data.get("location", "")),
            "description": data.get("description", ""),
            "architecture": data.get("architecture", ""),
            "lighting": data.get("lighting", ""),
            "atmosphere": data.get("atmosphere", ""),
            "props": data.get("props", data.get("keyProps", [])),
            "location": data.get("location", data.get("locationType", "")),
            "timeOfDay": data.get("timeOfDay", ""),
            "weather": data.get("weather", ""),
            "keyProps": data.get("keyProps", ""),
            "continuityNotes": data.get("continuityNotes", ""),
            "referenceImagePath": data.get("referenceImagePath", ""),
            "previewImage": data.get("previewImage", ""),
            "linkedPromptIds": data.get("linkedPromptIds", []),
            "tags": data.get("tags", []),
            "createdAt": _now(),
            "updatedAt": _now(),
            "notes": data.get("notes", ""),
            "approvalState": data.get("approvalState", "draft"),
            "sheetImages": data.get("sheetImages", []),
            "approvedSheet": data.get("approvedSheet", ""),
            "alternateViews": data.get("alternateViews", []),  # different angles of same location
            "architectureNotes": data.get("architectureNotes", ""),
            "materialNotes": data.get("materialNotes", ""),
            "versionHistory": data.get("versionHistory", []),
            "sourceResolution": data.get("sourceResolution", {}),
        }
        record["referencePhoto"] = record.get("referenceImagePath", "")
        envs.append(record)
        _save_json(ENVIRONMENTS_PATH, envs)
        return record

    def get_environments(self):
        return _load_json(ENVIRONMENTS_PATH)

    def get_environment(self, eid):
        for e in _load_json(ENVIRONMENTS_PATH):
            if e["id"] == eid:
                return e
        return None

    def update_environment(self, eid, data):
        envs = _load_json(ENVIRONMENTS_PATH)
        for i, e in enumerate(envs):
            if e["id"] == eid:
                for key in ("name", "locationType", "description", "architecture",
                             "location", "timeOfDay", "weather",
                             "lighting", "keyProps", "atmosphere", "props",
                             "continuityNotes", "referenceImagePath", "previewImage",
                             "linkedPromptIds", "tags", "notes",
                             "approvalState", "sheetImages", "approvedSheet",
                             "alternateViews", "architectureNotes", "materialNotes",
                             "versionHistory", "sourceResolution"):
                    if key in data:
                        e[key] = data[key]
                e["updatedAt"] = _now()
                envs[i] = e
                _save_json(ENVIRONMENTS_PATH, envs)
                return e
        return None

    def delete_environment(self, eid):
        envs = _load_json(ENVIRONMENTS_PATH)
        new = [e for e in envs if e["id"] != eid]
        if len(new) == len(envs):
            return False
        _save_json(ENVIRONMENTS_PATH, new)
        return True

    # ───── Props ─────

    def create_prop(self, data):
        props = _load_json(PROPS_PATH)
        record = {
            "id": _gen_id(),
            "name": data.get("name", "Untitled Prop"),
            "description": data.get("description", ""),
            "category": data.get("category", ""),
            "referenceImagePath": data.get("referenceImagePath", ""),
            "tags": data.get("tags", []),
            "createdAt": _now(),
            "updatedAt": _now(),
            "approvalState": data.get("approvalState", "draft"),
            "sheetImages": data.get("sheetImages", []),
            "approvedSheet": data.get("approvedSheet", ""),
            "linkedCharacterIds": data.get("linkedCharacterIds", []),
            "linkedCostumeIds": data.get("linkedCostumeIds", []),
            "continuityNotes": data.get("continuityNotes", ""),
            "versionHistory": data.get("versionHistory", []),
            "sourceResolution": data.get("sourceResolution", {}),
        }
        props.append(record)
        _save_json(PROPS_PATH, props)
        return record

    def get_props(self):
        return _load_json(PROPS_PATH)

    def get_prop(self, pid):
        for p in _load_json(PROPS_PATH):
            if p["id"] == pid:
                return p
        return None

    def update_prop(self, pid, data):
        props = _load_json(PROPS_PATH)
        for i, p in enumerate(props):
            if p["id"] == pid:
                for key in ("name", "description", "category",
                             "referenceImagePath", "tags",
                             "approvalState", "sheetImages", "approvedSheet",
                             "linkedCharacterIds", "linkedCostumeIds",
                             "continuityNotes", "versionHistory", "sourceResolution"):
                    if key in data:
                        p[key] = data[key]
                p["updatedAt"] = _now()
                props[i] = p
                _save_json(PROPS_PATH, props)
                return p
        return None

    def delete_prop(self, pid):
        props = _load_json(PROPS_PATH)
        new = [p for p in props if p["id"] != pid]
        if len(new) == len(props):
            return False
        _save_json(PROPS_PATH, new)
        return True

    # ───── Voices ─────

    def create_voice(self, data):
        voices = _load_json(VOICES_PATH)
        record = {
            "id": _gen_id(),
            "name": data.get("name", "Untitled Voice"),
            "characterId": data.get("characterId", ""),
            "voicePresetId": data.get("voicePresetId", ""),
            "description": data.get("description", ""),
            "sampleAudioPath": data.get("sampleAudioPath", ""),
            "tags": data.get("tags", []),
            "createdAt": _now(),
            "updatedAt": _now(),
        }
        voices.append(record)
        _save_json(VOICES_PATH, voices)
        return record

    def get_voices(self):
        return _load_json(VOICES_PATH)

    def get_voice(self, vid):
        for v in _load_json(VOICES_PATH):
            if v["id"] == vid:
                return v
        return None

    def update_voice(self, vid, data):
        voices = _load_json(VOICES_PATH)
        for i, v in enumerate(voices):
            if v["id"] == vid:
                for key in ("name", "characterId", "voicePresetId", "description",
                             "sampleAudioPath", "tags"):
                    if key in data:
                        v[key] = data[key]
                v["updatedAt"] = _now()
                voices[i] = v
                _save_json(VOICES_PATH, voices)
                return v
        return None

    def delete_voice(self, vid):
        voices = _load_json(VOICES_PATH)
        new = [v for v in voices if v["id"] != vid]
        if len(new) == len(voices):
            return False
        _save_json(VOICES_PATH, new)
        return True

    # ───── Scenes ─────

    def create_scene(self, data):
        scenes = _load_json(SCENES_PATH)
        record = {
            "id": _gen_id(),
            "name": data.get("name", "Untitled Scene"),
            "promptId": data.get("promptId", ""),
            "characterId": data.get("characterId", ""),
            "costumeId": data.get("costumeId", ""),
            "environmentId": data.get("environmentId", ""),
            "shotDescription": data.get("shotDescription", ""),
            "cameraAngle": data.get("cameraAngle", ""),
            "cameraMovement": data.get("cameraMovement", ""),
            "duration": data.get("duration", 5),
            "orderIndex": data.get("orderIndex", len(scenes)),
            "tags": data.get("tags", []),
            "createdAt": _now(),
            "updatedAt": _now(),
            "notes": data.get("notes", ""),
        }
        scenes.append(record)
        _save_json(SCENES_PATH, scenes)
        return record

    def get_scenes(self):
        scenes = _load_json(SCENES_PATH)
        return sorted(scenes, key=lambda s: s.get("orderIndex", 0))

    def get_scene(self, sid):
        for s in _load_json(SCENES_PATH):
            if s["id"] == sid:
                return s
        return None

    def update_scene(self, sid, data):
        scenes = _load_json(SCENES_PATH)
        for i, s in enumerate(scenes):
            if s["id"] == sid:
                for key in ("name", "promptId", "characterId", "costumeId", "environmentId",
                             "shotDescription", "cameraAngle", "cameraMovement", "duration",
                             "orderIndex", "tags", "notes"):
                    if key in data:
                        s[key] = data[key]
                s["updatedAt"] = _now()
                scenes[i] = s
                _save_json(SCENES_PATH, scenes)
                return s
        return None

    def delete_scene(self, sid):
        scenes = _load_json(SCENES_PATH)
        new = [s for s in scenes if s["id"] != sid]
        if len(new) == len(scenes):
            return False
        _save_json(SCENES_PATH, new)
        return True

    # ───── Style Locks ─────

    def get_style_locks(self):
        return _load_json(STYLE_LOCKS_PATH)

    def set_style_locks(self, locks):
        _save_json(STYLE_LOCKS_PATH, locks)
        return locks

    # ───── World Rules ─────

    def get_world_rules(self):
        return _load_json(WORLD_RULES_PATH)

    def set_world_rules(self, rules):
        _save_json(WORLD_RULES_PATH, rules)
        return rules

    # ───── Continuity Rules ─────

    def get_continuity_rules(self):
        return _load_json(CONTINUITY_RULES_PATH)

    def set_continuity_rules(self, rules):
        _save_json(CONTINUITY_RULES_PATH, rules)
        return rules

    # ───── Project Style Lock ─────

    def get_project_style(self):
        return _load_json(PROJECT_STYLE_PATH, default={})

    def set_project_style(self, style):
        """Save structured project style lock.
        Expected fields: worldSetting, tone, visualLanguage, colorPalette,
        textureMaterial, cameraLanguage, continuityRules, negativePrompt"""
        if not isinstance(style, dict):
            return {"error": "Style must be a dict"}
        style["updatedAt"] = _now()
        _save_json(PROJECT_STYLE_PATH, style)
        return style

    # ───── Sheet Management ─────

    def add_sheet_image(self, asset_type, asset_id, sheet_data):
        """Add a generated sheet image to an asset.
        asset_type: 'character'|'costume'|'environment'|'prop'
        sheet_data: {url, type, resolution:{width,height}, model, generatedAt}
        """
        getter = getattr(self, f'get_{asset_type}', None)
        updater = getattr(self, f'update_{asset_type}', None)
        if not getter or not updater:
            return {"error": f"Unknown asset type: {asset_type}"}
        asset = getter(asset_id)
        if not asset:
            return {"error": f"{asset_type} not found: {asset_id}"}
        sheets = asset.get("sheetImages", [])
        sheet_data["addedAt"] = _now()
        sheets.append(sheet_data)
        updater(asset_id, {"sheetImages": sheets, "approvalState": "generated"})
        return getter(asset_id)

    def approve_sheet(self, asset_type, asset_id, sheet_url, slot="approvedSheet"):
        """Promote a sheet image to an approved slot.
        slot: 'approvedSheet'|'approvedFaceCloseUp'|'approvedHeroPortrait'|'approvedFullBody'|'approvedSideAngle'
        """
        getter = getattr(self, f'get_{asset_type}', None)
        updater = getattr(self, f'update_{asset_type}', None)
        if not getter or not updater:
            return {"error": f"Unknown asset type: {asset_type}"}
        asset = getter(asset_id)
        if not asset:
            return {"error": f"{asset_type} not found: {asset_id}"}
        update = {slot: sheet_url}
        if slot == "approvedSheet":
            update["approvalState"] = "approved"
        # Add to version history
        history = asset.get("versionHistory", [])
        history.append({
            "version": len(history) + 1,
            "sheetUrl": sheet_url,
            "slot": slot,
            "timestamp": _now(),
        })
        update["versionHistory"] = history
        updater(asset_id, update)
        return getter(asset_id)

    def lock_asset(self, asset_type, asset_id):
        """Lock an approved asset to prevent accidental changes."""
        getter = getattr(self, f'get_{asset_type}', None)
        updater = getattr(self, f'update_{asset_type}', None)
        if not getter or not updater:
            return {"error": f"Unknown asset type: {asset_type}"}
        asset = getter(asset_id)
        if not asset:
            return {"error": f"{asset_type} not found: {asset_id}"}
        if asset.get("approvalState") not in ("approved", "locked"):
            return {"error": "Asset must be approved before locking"}
        updater(asset_id, {"approvalState": "locked"})
        return getter(asset_id)

    def get_asset_readiness(self, asset_type, asset_id):
        """Check production readiness of an asset."""
        getter = getattr(self, f'get_{asset_type}', None)
        if not getter:
            return {"error": f"Unknown asset type: {asset_type}"}
        asset = getter(asset_id)
        if not asset:
            return {"error": f"{asset_type} not found: {asset_id}"}

        readiness = {
            "hasUploadedRef": bool(asset.get("referencePhoto") or asset.get("referenceImagePath")),
            "hasGeneratedSheets": len(asset.get("sheetImages", [])) > 0,
            "hasApprovedSheet": bool(asset.get("approvedSheet")),
            "isLocked": asset.get("approvalState") == "locked",
            "approvalState": asset.get("approvalState", "draft"),
        }

        if asset_type == "character":
            readiness["hasFaceCloseUp"] = bool(asset.get("approvedFaceCloseUp"))
            readiness["hasHeroPortrait"] = bool(asset.get("approvedHeroPortrait"))
            readiness["hasFullBody"] = bool(asset.get("approvedFullBody"))
            readiness["closeUpReady"] = bool(asset.get("approvedFaceCloseUp"))
            readiness["productionReady"] = all([
                readiness["hasApprovedSheet"],
                readiness["hasFaceCloseUp"],
            ])
        else:
            readiness["productionReady"] = readiness["hasApprovedSheet"]

        return readiness

    # ───── Assembly ─────

    def assemble_prompt(self, scene_id):
        """Assemble a final prompt from a scene record.
        Returns {text, sections, warnings}."""
        scene = self.get_scene(scene_id)
        if not scene:
            return {"text": "", "sections": [], "warnings": [{"level": "error", "message": "Scene not found"}]}

        sections = []
        warnings = []

        # 0. Project Style Lock
        project_style = self.get_project_style()
        if project_style:
            style_parts = []
            for key in ("worldSetting", "tone", "visualLanguage", "colorPalette", "textureMaterial", "cameraLanguage"):
                if project_style.get(key):
                    style_parts.append(project_style[key])
            if style_parts:
                sections.append({"label": "Project Style", "text": ". ".join(style_parts)})
            if project_style.get("negativePrompt"):
                sections.append({"label": "Negative", "text": "AVOID: " + project_style["negativePrompt"]})

        # 1. Style locks
        style_locks = self.get_style_locks()
        if style_locks:
            style_text = ". ".join(
                lock.get("rule", "") for lock in style_locks if lock.get("rule")
            )
            if style_text:
                sections.append({"label": "Style", "text": style_text})

        # 2. World rules
        world_rules = self.get_world_rules()
        if world_rules:
            world_text = ". ".join(
                rule.get("rule", "") for rule in world_rules if rule.get("rule")
            )
            if world_text:
                sections.append({"label": "World Rules", "text": world_text})

        # 3. Get linked entities
        character = self.get_character(scene.get("characterId", "")) if scene.get("characterId") else None
        costume = self.get_costume(scene.get("costumeId", "")) if scene.get("costumeId") else None
        environment = self.get_environment(scene.get("environmentId", "")) if scene.get("environmentId") else None

        # 4. Clone + resolve source prompt
        prompt_record = self.get_prompt(scene.get("promptId", "")) if scene.get("promptId") else None
        if prompt_record:
            raw = prompt_record.get("rawPrompt", "")
            resolved = resolve_variables(raw, character, costume, environment)
            sections.append({"label": "Prompt", "text": resolved})

            # Check for unresolved variables
            unresolved = extract_variables(resolved)
            if unresolved:
                warnings.append({
                    "level": "warning",
                    "message": f"Unresolved variables: {', '.join('[' + v + ']' for v in unresolved)}"
                })
        else:
            if scene.get("promptId"):
                warnings.append({"level": "warning", "message": "Linked prompt not found"})

        # 5. Character section
        if character:
            char_parts = []
            if character.get("physicalDescription"):
                char_parts.append(character["physicalDescription"])
            if character.get("hair"):
                char_parts.append(f"Hair: {character['hair']}")
            if character.get("distinguishingFeatures"):
                char_parts.append(character["distinguishingFeatures"])
            if char_parts:
                sections.append({"label": "Character", "text": ". ".join(char_parts)})
        elif scene.get("characterId"):
            warnings.append({"level": "warning", "message": "Linked character not found"})

        # 6. Costume section
        if costume:
            costume_parts = []
            if costume.get("description"):
                costume_parts.append(costume["description"])
            else:
                for field in ("upperBody", "lowerBody", "footwear", "accessories"):
                    if costume.get(field):
                        costume_parts.append(f"{field}: {costume[field]}")
            if costume_parts:
                sections.append({"label": "Costume", "text": ". ".join(costume_parts)})
        elif scene.get("costumeId"):
            warnings.append({"level": "warning", "message": "Linked costume not found"})

        # 7. Environment section
        if environment:
            env_parts = []
            if environment.get("description"):
                env_parts.append(environment["description"])
            for field in ("location", "timeOfDay", "weather", "lighting", "atmosphere"):
                if environment.get(field):
                    env_parts.append(f"{field}: {environment[field]}")
            if env_parts:
                sections.append({"label": "Environment", "text": ". ".join(env_parts)})
        elif scene.get("environmentId"):
            warnings.append({"level": "warning", "message": "Linked environment not found"})

        # 8. Shot description
        if scene.get("shotDescription"):
            sections.append({"label": "Shot", "text": scene["shotDescription"]})

        # 9. Camera
        cam_parts = []
        if scene.get("cameraAngle"):
            cam_parts.append(scene["cameraAngle"])
        if scene.get("cameraMovement"):
            cam_parts.append(scene["cameraMovement"])
        if cam_parts:
            sections.append({"label": "Camera", "text": ", ".join(cam_parts)})

        # 10. Continuity rules
        continuity = self.get_continuity_rules()
        if continuity:
            cont_text = ". ".join(
                r.get("rule", "") for r in continuity if r.get("rule")
            )
            if cont_text:
                sections.append({"label": "Continuity", "text": cont_text})

        # Build final text
        assembled = ". ".join(s["text"] for s in sections if s.get("text"))

        if not sections:
            warnings.append({"level": "warning", "message": "No content assembled — scene has no linked entities or prompt"})

        return {
            "text": assembled,
            "sections": sections,
            "warnings": warnings,
            "sceneId": scene_id,
            "sceneName": scene.get("name", ""),
        }

    def validate_assembly(self, scene_id):
        """Returns list of {level, message} validation results."""
        results = []
        scene = self.get_scene(scene_id)
        if not scene:
            return [{"level": "error", "message": "Scene not found"}]

        # Check empty fields
        if not scene.get("promptId"):
            results.append({"level": "warning", "message": "No prompt linked"})
        if not scene.get("characterId"):
            results.append({"level": "info", "message": "No character linked"})
        if not scene.get("environmentId"):
            results.append({"level": "info", "message": "No environment linked"})
        if not scene.get("shotDescription"):
            results.append({"level": "warning", "message": "No shot description"})

        # Check linked entities exist
        if scene.get("promptId") and not self.get_prompt(scene["promptId"]):
            results.append({"level": "error", "message": "Linked prompt not found (deleted?)"})
        if scene.get("characterId") and not self.get_character(scene["characterId"]):
            results.append({"level": "error", "message": "Linked character not found (deleted?)"})
        if scene.get("costumeId") and not self.get_costume(scene["costumeId"]):
            results.append({"level": "error", "message": "Linked costume not found (deleted?)"})
        if scene.get("environmentId") and not self.get_environment(scene["environmentId"]):
            results.append({"level": "error", "message": "Linked environment not found (deleted?)"})

        # Check unresolved variables in prompt
        if scene.get("promptId"):
            prompt_rec = self.get_prompt(scene["promptId"])
            if prompt_rec:
                character = self.get_character(scene.get("characterId", "")) if scene.get("characterId") else None
                costume = self.get_costume(scene.get("costumeId", "")) if scene.get("costumeId") else None
                environment = self.get_environment(scene.get("environmentId", "")) if scene.get("environmentId") else None
                resolved = resolve_variables(prompt_rec.get("rawPrompt", ""), character, costume, environment)
                unresolved = extract_variables(resolved)
                if unresolved:
                    results.append({
                        "level": "warning",
                        "message": f"Unresolved variables after assembly: {', '.join('[' + v + ']' for v in unresolved)}"
                    })

        if not results:
            results.append({"level": "ok", "message": "Scene passes all validation checks"})

        return results

    # ───── Dashboard ─────

    def get_dashboard(self):
        prompts = _load_json(MASTER_PROMPTS_PATH)
        characters = _load_json(CHARACTERS_PATH)
        costumes = _load_json(COSTUMES_PATH)
        environments = _load_json(ENVIRONMENTS_PATH)
        scenes = _load_json(SCENES_PATH)
        style_locks = _load_json(STYLE_LOCKS_PATH)
        world_rules = _load_json(WORLD_RULES_PATH)

        # Count locked prompts
        locked = sum(1 for p in prompts if p.get("isImmutable"))

        # Count categories
        categories = {}
        for p in prompts:
            cat = p.get("category", "general")
            categories[cat] = categories.get(cat, 0) + 1

        return {
            "counts": {
                "prompts": len(prompts),
                "characters": len(characters),
                "costumes": len(costumes),
                "environments": len(environments),
                "scenes": len(scenes),
                "styleLocks": len(style_locks),
                "worldRules": len(world_rules),
            },
            "lockedPrompts": locked,
            "categories": categories,
        }

    # ───── Export ─────

    def export_scene_text(self, scene_id):
        """Export assembled prompt as plain text."""
        assembly = self.assemble_prompt(scene_id)
        lines = []
        lines.append(f"# Scene: {assembly.get('sceneName', scene_id)}")
        lines.append("")
        for section in assembly.get("sections", []):
            lines.append(f"## {section['label']}")
            lines.append(section["text"])
            lines.append("")
        if assembly.get("warnings"):
            lines.append("## Warnings")
            for w in assembly["warnings"]:
                lines.append(f"  [{w['level'].upper()}] {w['message']}")
        lines.append("")
        lines.append(f"--- Assembled prompt ---")
        lines.append(assembly.get("text", ""))
        return "\n".join(lines)

    def export_scene_json(self, scene_id):
        """Export assembled prompt as JSON with full context."""
        assembly = self.assemble_prompt(scene_id)
        scene = self.get_scene(scene_id)
        result = {
            "scene": scene,
            "assembly": assembly,
        }
        if scene:
            if scene.get("promptId"):
                result["prompt"] = self.get_prompt(scene["promptId"])
            if scene.get("characterId"):
                result["character"] = self.get_character(scene["characterId"])
            if scene.get("costumeId"):
                result["costume"] = self.get_costume(scene["costumeId"])
            if scene.get("environmentId"):
                result["environment"] = self.get_environment(scene["environmentId"])
        return result
