"""
LUMN Studio — lightweight input validation.

Dict-schema checker. Zero external dependencies. Returns a tuple of
(ok: bool, error: str | None, cleaned: dict). Intended to run before any
expensive handler work so bad payloads get a cheap 400 instead of
eating credits.

Schema syntax:

    SCHEMA = {
        "prompt":    {"type": str,  "required": True,  "max_len": 4000},
        "shot_id":   {"type": str,  "required": True,  "max_len": 120,
                      "pattern": r"^[a-zA-Z0-9_-]+$"},
        "seed":      {"type": int,  "required": False, "min": 0, "max": 2**31},
        "nsfw":      {"type": bool, "required": False, "default": False},
        "engine":    {"type": str,  "required": False, "choices":
                      ["gemini_2.5_flash", "imagen-4", "grok-image"]},
        "refs":      {"type": list, "required": False, "max_items": 6,
                      "item_type": str, "item_max_len": 500},
    }

Call: ok, err, data = validate(body, SCHEMA). On failure, err is a short
human string suitable for returning to the client.
"""

from __future__ import annotations

import re
from typing import Any


def validate(body: Any, schema: dict[str, dict]) -> tuple[bool, str | None, dict]:
    """Validate a request body against a schema. Returns (ok, err, cleaned)."""
    if not isinstance(body, dict):
        return False, "body must be a JSON object", {}

    cleaned: dict[str, Any] = {}

    for field, rules in schema.items():
        present = field in body
        value = body.get(field)

        if not present or value is None:
            if rules.get("required"):
                return False, f"missing required field: {field}", {}
            if "default" in rules:
                cleaned[field] = rules["default"]
            continue

        expected_type = rules.get("type")
        if expected_type is int and isinstance(value, bool):
            # bool is subclass of int; reject
            return False, f"{field} must be an integer", {}
        if expected_type and not isinstance(value, expected_type):
            # Allow int where float expected
            if expected_type is float and isinstance(value, int):
                value = float(value)
            else:
                return False, f"{field} must be of type {expected_type.__name__}", {}

        if expected_type is str:
            max_len = rules.get("max_len")
            if max_len is not None and len(value) > max_len:
                return False, f"{field} exceeds max length of {max_len}", {}
            min_len = rules.get("min_len")
            if min_len is not None and len(value) < min_len:
                return False, f"{field} below min length of {min_len}", {}
            pattern = rules.get("pattern")
            if pattern and not re.match(pattern, value):
                return False, f"{field} has invalid format", {}
            choices = rules.get("choices")
            if choices and value not in choices:
                return False, f"{field} must be one of: {', '.join(choices)}", {}

        if expected_type in (int, float):
            mn = rules.get("min")
            mx = rules.get("max")
            if mn is not None and value < mn:
                return False, f"{field} below minimum of {mn}", {}
            if mx is not None and value > mx:
                return False, f"{field} above maximum of {mx}", {}

        if expected_type is list:
            max_items = rules.get("max_items")
            if max_items is not None and len(value) > max_items:
                return False, f"{field} exceeds max items of {max_items}", {}
            item_type = rules.get("item_type")
            item_max_len = rules.get("item_max_len")
            if item_type or item_max_len:
                for i, item in enumerate(value):
                    if item_type and not isinstance(item, item_type):
                        return False, f"{field}[{i}] must be {item_type.__name__}", {}
                    if item_max_len and isinstance(item, str) and len(item) > item_max_len:
                        return False, f"{field}[{i}] exceeds max length of {item_max_len}", {}

        cleaned[field] = value

    return True, None, cleaned


# ---------------------------------------------------------------------------
# Reusable schemas for V6 endpoints
# ---------------------------------------------------------------------------

SHOT_ID_PATTERN = r"^[a-zA-Z0-9_-]{1,120}$"

ANCHOR_GENERATE_SCHEMA = {
    "prompt":                {"type": str,  "required": True,  "max_len": 4000, "min_len": 1},
    "shot_id":               {"type": str,  "required": False, "max_len": 120, "pattern": SHOT_ID_PATTERN},
    "reference_image_paths": {"type": list, "required": False, "max_items": 12, "item_type": str, "item_max_len": 1000},
    "shot_context":          {"type": dict, "required": False},
    "num_images":            {"type": int,  "required": False, "min": 1, "max": 4},
    "engine":                {"type": str,  "required": False, "max_len": 60},
    "seed":                  {"type": int,  "required": False, "min": 0, "max": 2**31 - 1},
    "nsfw":                  {"type": bool, "required": False, "default": False},
    "aspect":                {"type": str,  "required": False, "max_len": 20},
}

CLIP_GENERATE_SCHEMA = {
    "prompt":             {"type": str,  "required": False, "max_len": 2000, "min_len": 0},
    "shot_id":            {"type": str,  "required": False, "max_len": 120, "pattern": SHOT_ID_PATTERN},
    "anchor_path":        {"type": str,  "required": False, "max_len": 2000},
    "image_url":          {"type": str,  "required": False, "max_len": 2000},
    "shot_context":       {"type": dict, "required": False},
    "duration":           {"type": int,  "required": False, "min": 3, "max": 15},
    "engine":             {"type": str,  "required": False, "max_len": 60},
    "tier":               {"type": str,  "required": False, "max_len": 40},
    "end_image_path":     {"type": str,  "required": False, "max_len": 2000},
    "multi_prompt":       {"type": list, "required": False},
    "elements":           {"type": list, "required": False},
    "cfg_scale":          {"type": float, "required": False, "min": 0, "max": 1},
    "seed":               {"type": int,  "required": False, "min": 0, "max": 2**31 - 1},
    "nsfw":               {"type": bool, "required": False, "default": False},
    "skip_identity_gate": {"type": bool, "required": False, "default": False},
    "skip_lint":          {"type": bool, "required": False, "default": False},
}

PROMPT_ASSEMBLE_SCHEMA = {
    "prompt":       {"type": str,  "required": True,  "max_len": 4000, "min_len": 0},
    "shot_context": {"type": dict, "required": False},
    "target":       {"type": str,  "required": False, "choices": ["anchor", "clip"]},
}

BRIEF_EXPAND_SCHEMA = {
    "brief":     {"type": str, "required": True, "max_len": 2000, "min_len": 1},
    "max_shots": {"type": int, "required": False, "min": 1, "max": 40},
}


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Valid
    ok, err, data = validate({"prompt": "a man walks"}, ANCHOR_GENERATE_SCHEMA)
    assert ok and err is None, f"valid case failed: {err}"
    assert data["nsfw"] is False  # default applied

    # Missing required
    ok, err, _ = validate({}, ANCHOR_GENERATE_SCHEMA)
    assert not ok and "prompt" in err, f"missing-field case failed: {err}"

    # Too long
    ok, err, _ = validate({"prompt": "x" * 5000}, ANCHOR_GENERATE_SCHEMA)
    assert not ok and "max length" in err

    # Bad shot_id pattern
    ok, err, _ = validate({"prompt": "ok", "shot_id": "bad id!"}, ANCHOR_GENERATE_SCHEMA)
    assert not ok and "invalid format" in err

    # Wrong type
    ok, err, _ = validate({"prompt": 123}, ANCHOR_GENERATE_SCHEMA)
    assert not ok and "type" in err

    # Bool rejected as int
    ok, err, _ = validate({"prompt": "ok", "seed": True}, ANCHOR_GENERATE_SCHEMA)
    assert not ok

    # List item validation
    ok, err, _ = validate({"prompt": "ok", "reference_image_paths": ["a", 5]}, ANCHOR_GENERATE_SCHEMA)
    assert not ok and "reference_image_paths[1]" in err

    # Not a dict body
    ok, err, _ = validate("nope", ANCHOR_GENERATE_SCHEMA)
    assert not ok

    print("validate.py self-tests: 8/8 PASS")
