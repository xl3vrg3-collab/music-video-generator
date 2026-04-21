"""
Anchor Auditor — vision-based QA for generated anchor images.

Audits a single anchor PNG against the shot's rules and the character's
emblem-binding. Catches the violation patterns that burned us on the TB MV:
  - floating moon / emblem-above-head when forehead not visible
  - duplicate character in reflections / photos / memory fragments
  - emblem on wrong body part
  - subject pose mismatch vs intended (e.g. back-turned when prompt says front)

Returns structured JSON so callers (CLI, LUMN server, CI) can gate on it.
"""
from __future__ import annotations

import json
import os
from typing import Any

from lib.claude_client import call_vision_json, OPUS_MODEL

_MAX_IMAGE_BYTES = 4_500_000  # Anthropic caps base64 at 5MB; keep headroom.


def _downsize_if_needed(anchor_path: str) -> str:
    """If the image is too big for the API, resize to a temp file and return it."""
    try:
        if os.path.getsize(anchor_path) <= _MAX_IMAGE_BYTES:
            return anchor_path
    except OSError:
        return anchor_path
    try:
        from PIL import Image
        import tempfile
        img = Image.open(anchor_path)
        img = img.convert("RGB") if img.mode in ("RGBA", "P") else img
        max_dim = 1600
        w, h = img.size
        if max(w, h) > max_dim:
            ratio = max_dim / float(max(w, h))
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        tmp = tempfile.NamedTemporaryFile(prefix="audit_", suffix=".jpg", delete=False)
        tmp.close()
        quality = 85
        img.save(tmp.name, "JPEG", quality=quality, optimize=True)
        while os.path.getsize(tmp.name) > _MAX_IMAGE_BYTES and quality > 50:
            quality -= 10
            img.save(tmp.name, "JPEG", quality=quality, optimize=True)
        return tmp.name
    except Exception:
        return anchor_path


DEFAULT_CHARACTER_RULES = {
    "emblem_binding": "forehead_only",  # forehead_only | chest_only | full_body | none
    "emblem_shape": "crescent moon",
    "emblem_color": "white-silver with soft glow",  # canonical TB
    "emblem_tips_orientation": "up",  # up | down | sideways | inward
    "emblem_position": "centered between brows, upper forehead",
    "single_instance": True,  # exactly one character in frame
    "proportions": "chibi",  # chibi | adult | realistic
    "hood_default": "down",  # down | up
    "required_accessories": [],  # TB canonical: no necklace, no zipper. Override per-project if needed.
    "muzzle_color": "",  # unspecified — don't flag muzzle color
    "eye_color": "glowing red-orange",  # TB canonical per character sheet
    # Minimum readable bear area as % of frame area (wide/medium/close)
    "min_frame_coverage": {"wide": 10, "medium": 25, "close": 50},
}


def _shot_scale_from_context(shot_context: dict) -> str:
    angle = (shot_context.get("cameraAngle") or "").lower()
    desc = (shot_context.get("shotDescription") or "").lower()
    tokens = f"{angle} {desc}"
    if any(k in tokens for k in ("ultra-wide", "ultra wide", "wide shot", "establishing", "extreme long", "long shot")):
        return "wide"
    if any(k in tokens for k in ("close-up", "close up", "closeup", "ecu", "extreme close", "macro")):
        return "close"
    if "wide" in tokens:
        return "wide"
    return "medium"


AUDIT_SYSTEM_PROMPT = (
    "You are a strict continuity-QA reviewer for animated character shots. "
    "You examine a single still image and flag rule violations the way a "
    "senior editor would before a clip goes out for VFX work. Be concrete and "
    "specific. Never hallucinate passing — if you're not sure, flag it."
)


def _build_audit_prompt(rules: dict[str, Any], shot_context: dict[str, Any],
                        callout_present: bool = False) -> str:
    emblem_binding = rules.get("emblem_binding", "forehead_only")
    emblem_shape = rules.get("emblem_shape", "crescent moon")
    emblem_color = rules.get("emblem_color", "white-silver with soft glow")
    emblem_tips = rules.get("emblem_tips_orientation", "up")
    emblem_position = rules.get("emblem_position", "centered between brows, upper forehead")
    single_instance = rules.get("single_instance", True)
    proportions = rules.get("proportions", "chibi")
    hood_default = rules.get("hood_default", "down")
    required_accessories = rules.get("required_accessories", [])
    muzzle_color = rules.get("muzzle_color", "mauve pink")
    eye_color = rules.get("eye_color", "amber")
    min_coverage = rules.get("min_frame_coverage", {"wide": 10, "medium": 25, "close": 50})
    scale = _shot_scale_from_context(shot_context or {})
    coverage_floor = min_coverage.get(scale, 20)

    lines: list[str] = []
    if callout_present:
        lines.append("TWO IMAGES ATTACHED:")
        lines.append("  Image 1 = THE ANCHOR to audit.")
        lines.append("  Image 2 = EMBLEM CALLOUT reference (authoritative shape/color/orientation).")
        lines.append("  Compare the emblem rendered in Image 1 against the callout in Image 2. "
                     "Any deviation in shape, color, stroke weight, tip orientation, or placement "
                     "counts as emblem_shape_drift or emblem_color_drift.")
        lines.append("")
    lines.append("AUDIT THE ANCHOR IMAGE against the rules below.")
    lines.append("")
    lines.append("CHARACTER RULES:")
    lines.append(f"  - emblem_shape: {emblem_shape}")
    lines.append(f"  - emblem_color: {emblem_color}")
    lines.append(f"  - emblem_tips_orientation: {emblem_tips} (crescent tips must point {emblem_tips.upper()})")
    lines.append(f"  - emblem_position: {emblem_position}")
    lines.append(f"  - emblem_binding: {emblem_binding}")
    if emblem_binding == "forehead_only":
        lines.append(
            f"    RULE: The {emblem_shape} emblem belongs ONLY on the forehead. "
            f"If the forehead is not visible (back-turned, hooded, fully profile with forehead "
            f"obscured), the emblem MUST NOT appear anywhere in the frame. "
            f"Critically: NO moon/crescent in the sky above or behind the character's head. "
            f"NO emblem on the back of the head, the hoodie, the chest, or any other body part."
        )
        lines.append(
            "    STRICT CROSS-CHECKS (any triggering condition = FAIL):"
        )
        lines.append(
            "      * If facing ∈ {back, back_three_quarter} → emblem MUST be absent. "
            "If any emblem visible → emblem_when_forehead_hidden."
        )
        lines.append(
            "      * If emblem present on forehead but tips NOT pointing "
            f"{emblem_tips} → emblem_tips_wrong."
        )
        lines.append(
            f"      * If emblem present but color visibly differs from \"{emblem_color}\" "
            "(e.g. red-orange glow, gold, saturated blue) → emblem_color_drift."
        )
        lines.append(
            "      * If emblem present but NOT centered between brows on upper forehead "
            "(drifted left/right/up-to-crown) → emblem_position_drift."
        )
        lines.append(
            "      * If MORE THAN ONE crescent visible on the character → multiple_emblems."
        )
    lines.append(f"  - single_instance_required: {single_instance}")
    if single_instance:
        lines.append(
            "    RULE: Exactly ONE character in the frame. No duplicates in reflections, "
            "puddles, mirrors, photographs, memory fragments, or anywhere else."
        )
    lines.append(f"  - proportions: {proportions}")
    if proportions == "chibi":
        lines.append(
            "    RULE: Character is CHIBI — head ~half body height, stubby limbs, ~2 heads tall. "
            "FAIL if rendered as an adult bear, realistic bear, tall/slim bear, or human-proportioned."
        )
    lines.append(f"  - hood_default: {hood_default}")
    lines.append(
        f"    RULE: Hood is {hood_default.upper()} with rounded ears visible, UNLESS the shot "
        f"description explicitly says otherwise."
    )
    if required_accessories:
        lines.append(f"  - required_accessories: {', '.join(required_accessories)}")
    else:
        lines.append("  - required_accessories: NONE (do not flag missing_accessory)")
    if muzzle_color:
        lines.append(f"  - muzzle_color: {muzzle_color}")
    else:
        lines.append("  - muzzle_color: UNSPECIFIED (do not flag muzzle_color_wrong)")
    lines.append(f"  - eye_color: {eye_color}")
    lines.append(
        "    RULE: Only evaluate eye_color if eyes are clearly visible in the frame. "
        "Back-turned / silhouetted / hooded / eyes-closed / eyes-offscreen shots are EXEMPT "
        "— do not flag eye_color_wrong when eyes cannot be seen."
    )
    lines.append(f"  - min_frame_coverage for this shot ({scale}): {coverage_floor}%")
    lines.append(
        f"    RULE: Bear must occupy at least ~{coverage_floor}% of frame area. "
        f"FAIL if the bear is a tiny distant silhouette (subject_too_small). "
        f"EXCEPTION: if the shot description explicitly calls for a tiny/distant silhouette "
        f"(e.g. ultra-wide establishing, vast scale reveal), this rule is SUSPENDED."
    )
    lines.append("")
    if shot_context:
        lines.append("SHOT CONTEXT:")
        for k in ("name", "shotDescription", "cameraAngle"):
            v = shot_context.get(k)
            if v:
                lines.append(f"  - {k}: {v}")
        lines.append("")
        # Reflection-shot carve-out: if the shot is literally about a reflection,
        # a second instance in the puddle/mirror is intentional, not a duplicate.
        desc = (shot_context.get("shotDescription") or "").lower()
        name = (shot_context.get("name") or "").lower()
        if "reflection" in desc or "reflection" in name or "mirror" in desc:
            lines.append(
                "NOTE: This shot is about a REFLECTION — a second rendering of the character "
                "inside the reflective surface is INTENDED and MUST NOT be flagged as "
                "duplicate_character. Only flag if a second TB appears OUTSIDE the reflection."
            )
            lines.append("")
    lines.append("INSTRUCTIONS — scan the frame through EVERY check below before scoring.")
    lines.append("Do NOT skip any scan. Record the scan result even when you think nothing is wrong.")
    lines.append("")
    lines.append("  1. Count the characters actually rendered in the frame.")
    lines.append("  2. Determine if the forehead is visible.")
    lines.append("  3. EMBLEM LOCATION SWEEP — perform this scan EVERY time, even if the forehead")
    lines.append("     appears normal. Inspect in order: forehead, crown/top-of-head, back-of-head,")
    lines.append("     nape, temples, ears, cheeks, muzzle, chest, shoulders, sleeves, paws, hood,")
    lines.append("     clothing, sky/background, reflections. Record every crescent/moon/glow you")
    lines.append("     see. Any crescent outside the forehead is a violation — including subtle")
    lines.append("     glows that wrap from forehead around to the crown/back.")
    lines.append("     → Set back_of_head_emblem_scan to false only after you have looked")
    lines.append("       specifically at the BACK/CROWN/NAPE region and confirmed no emblem there.")
    lines.append("  3b. EMBLEM CONFORMANCE SCAN — if one or more emblems were detected on the")
    lines.append("      forehead, verify ALL of:")
    lines.append(f"        * color matches \"{emblem_color}\" (not red, not gold, not saturated blue)")
    lines.append(f"        * shape is a clean {emblem_shape}, not a disc or full circle")
    lines.append(f"        * crescent tips point {emblem_tips.upper()}")
    lines.append("        * centered between brows on upper forehead (not temple, not crown)")
    lines.append("        * exactly ONE emblem (not duplicated across forehead)")
    lines.append("      → Set emblem_conformance_scan to performed with per-field pass/fail.")
    lines.append("      Skip only when no emblem is visible (then set performed=false, reason).")
    lines.append("  4. PUPIL CONTENT SCAN — zoom mentally into each visible eye. The pupils should")
    lines.append("     show solid color, normal highlights, or environment reflections only. FAIL")
    lines.append("     (code: pupil_content_error) if the pupils contain: a bear face, a bear")
    lines.append("     silhouette, a miniature character, text, symbols, or any duplicate-identity")
    lines.append("     artifact. This scan is REQUIRED for any shot where eyes occupy more than")
    lines.append("     ~3% of the frame. Skip only when eyes are not visible.")
    lines.append("     → Set pupil_content_scan to true/false/skipped with notes.")
    lines.append("  5. HALLUCINATED-CHARACTER SCAN — search for ANY additional face or figure the")
    lines.append("     shot description did not ask for: human faces, ghost figures, cloaked")
    lines.append("     humanoids, holographic/wireframe people, crowd silhouettes, second bears,")
    lines.append("     mascots, AI-generated bystanders. FAIL (code: hallucinated_character) for")
    lines.append("     each one. Environmental signage/posters with drawn characters are OK if they")
    lines.append("     read as advertising, not characters. Reflections of TB himself are OK in")
    lines.append("     reflection-intent shots (see carve-out above).")
    lines.append("     → Set hallucinated_character_scan to true/false with notes.")
    lines.append("  6. Check for duplicate TB copies (self-duplication) in reflections/photos/etc")
    lines.append("     (respecting any reflection-shot carve-out above).")
    lines.append("  7. Assess proportions — chibi vs adult/realistic/tall.")
    lines.append("  8. Check hood state (up/down), ears visible or hidden.")
    if required_accessories:
        lines.append(f"  9. Check for required accessories: {', '.join(required_accessories)}.")
    else:
        lines.append("  9. (No accessories required — skip.)")
    if muzzle_color:
        lines.append(" 10. Check muzzle color and eye color against the rule.")
    else:
        lines.append(" 10. Check eye color only if eyes are visible (see eye_color rule above).")
    lines.append(" 11. Estimate the bear's frame coverage as a % of total frame area.")
    lines.append(" 12. Classify facing: front | three_quarter | profile | back | back_three_quarter.")
    lines.append(" 13. Judge whether the pose matches the shot description's intent.")
    lines.append("")
    lines.append("Respond with JSON ONLY in this exact shape:")
    lines.append("{")
    lines.append('  "character_count": <int>,')
    lines.append('  "forehead_visible": <bool>,')
    lines.append('  "emblems_detected": [ {"location": "<forehead|crown|back_of_head|nape|temple|ear|cheek|muzzle|chest|shoulder|sleeve|paw|hood|clothing|sky|background|reflection|other>", "shape": "<crescent|full_moon|other>", "notes": "..."} ],')
    lines.append('  "back_of_head_emblem_scan": {"performed": <bool>, "emblem_found_on_back": <bool>, "notes": "..."},')
    lines.append('  "emblem_conformance_scan": {"performed": <bool>, "color_ok": <bool>, "shape_ok": <bool>, "tips_ok": <bool>, "position_ok": <bool>, "single_ok": <bool>, "notes": "..."},')
    lines.append('  "pupil_content_scan": {"performed": <bool>, "eyes_visible": <bool>, "pupil_ok": <bool>, "notes": "..."},')
    lines.append('  "hallucinated_character_scan": {"performed": <bool>, "extra_figures_found": <bool>, "descriptions": ["..."]},')
    lines.append('  "duplicate_characters": [ {"location": "<puddle_reflection|photo_fragment|memory_shard>", "notes": "..."} ],')
    lines.append('  "proportions_check": {"observed": "<chibi|adult|realistic|tall>", "pass": <bool>},')
    lines.append('  "hood_state": {"up_or_down": "<up|down>", "ears_visible": <bool>, "pass": <bool>},')
    lines.append('  "accessories_present": ["beaded_necklace", "hoodie_zipper"],')
    lines.append('  "muzzle_color_ok": <bool>,')
    lines.append('  "eye_color_ok": <bool>,')
    lines.append('  "bear_frame_coverage_pct": <int 0-100>,')
    lines.append('  "facing": "<front|three_quarter|profile|back|back_three_quarter>",')
    lines.append('  "pose_matches_description": <bool>,')
    lines.append('  "violations": [ {"code": "<emblem_on_wrong_part|emblem_on_back_of_head|emblem_when_forehead_hidden|emblem_color_drift|emblem_shape_drift|emblem_tips_wrong|emblem_position_drift|moon_in_sky|multiple_emblems|pupil_content_error|hallucinated_character|duplicate_character|proportions_drift|hood_state_wrong|missing_accessory|muzzle_color_wrong|eye_color_wrong|subject_too_small|facing_mismatch|pose_mismatch_description|other>", "severity": "<high|medium|low>", "detail": "..."} ],')
    lines.append('  "pass": <bool — true only if violations list is empty AND all three scans performed>,')
    lines.append('  "summary": "<one short sentence>"')
    lines.append("}")
    return "\n".join(lines)


def audit_anchor(
    anchor_path: str,
    character_rules: dict[str, Any] | None = None,
    shot_context: dict[str, Any] | None = None,
    model: str | None = None,
    callout_path: str | None = None,
) -> dict[str, Any]:
    """Audit a single anchor image. Returns the parsed JSON verdict.

    If `callout_path` points to an emblem-callout reference PNG, it is passed as
    a SECOND image alongside the anchor. The prompt instructs the auditor to
    compare the emblem in the anchor against the callout, catching color/shape
    drift that a single-image audit would miss.
    """
    if not os.path.isfile(anchor_path):
        return {
            "pass": False,
            "error": f"anchor not found: {anchor_path}",
            "violations": [{"code": "missing_anchor", "severity": "high", "detail": anchor_path}],
        }

    rules = {**DEFAULT_CHARACTER_RULES, **(character_rules or {})}
    image_paths = [_downsize_if_needed(anchor_path)]
    callout_used = False
    if callout_path and os.path.isfile(callout_path):
        image_paths.append(_downsize_if_needed(callout_path))
        callout_used = True

    prompt = _build_audit_prompt(rules, shot_context or {}, callout_present=callout_used)

    try:
        result = call_vision_json(
            prompt=prompt,
            image_paths=image_paths,
            system=AUDIT_SYSTEM_PROMPT,
            model=model or OPUS_MODEL,
            max_tokens=1500,
        )
    except Exception as e:
        return {
            "pass": False,
            "error": f"vision call failed: {e}",
            "violations": [{"code": "audit_error", "severity": "high", "detail": str(e)[:200]}],
        }

    if not isinstance(result, dict):
        return {
            "pass": False,
            "error": "vision returned non-dict",
            "raw": str(result)[:500],
        }

    _enforce_mandatory_scans(result)
    _enforce_strict_cross_checks(result)
    if callout_used:
        result["callout_compared"] = True
    if "violations" in result:
        result["pass"] = len(result["violations"]) == 0
    return result


def _enforce_mandatory_scans(result: dict[str, Any]) -> None:
    """Add violations if the three mandatory scans were skipped or report
    positive without being surfaced as a violation. Keeps the auditor honest
    when the model drifts back to summary-only output."""
    violations = result.setdefault("violations", [])

    # 1. Back-of-head emblem scan must be performed
    boh = result.get("back_of_head_emblem_scan") or {}
    if not boh.get("performed"):
        violations.append({
            "code": "audit_scan_skipped",
            "severity": "high",
            "detail": "back_of_head_emblem_scan not performed",
        })
    elif boh.get("emblem_found_on_back"):
        if not any(v.get("code") in ("emblem_on_back_of_head", "emblem_on_wrong_part") for v in violations):
            violations.append({
                "code": "emblem_on_back_of_head",
                "severity": "high",
                "detail": boh.get("notes") or "emblem found on back/crown/nape",
            })

    # 2. Pupil content scan must be performed when eyes are visible.
    # Skipping is legitimate when eyes are not visible (back-turned, distant,
    # hooded, off-frame). Only flag as skipped if eyes ARE visible but the
    # scan was not performed.
    pcs = result.get("pupil_content_scan") or {}
    eyes_visible = pcs.get("eyes_visible")
    if not pcs.get("performed") and eyes_visible is True:
        violations.append({
            "code": "audit_scan_skipped",
            "severity": "high",
            "detail": "pupil_content_scan not performed despite eyes_visible=true",
        })
    elif pcs.get("eyes_visible") and pcs.get("pupil_ok") is False:
        if not any(v.get("code") == "pupil_content_error" for v in violations):
            violations.append({
                "code": "pupil_content_error",
                "severity": "high",
                "detail": pcs.get("notes") or "pupils contain duplicate identity artifact",
            })

    # 3. Hallucinated character scan must be performed
    hcs = result.get("hallucinated_character_scan") or {}
    if not hcs.get("performed"):
        violations.append({
            "code": "audit_scan_skipped",
            "severity": "high",
            "detail": "hallucinated_character_scan not performed",
        })
    elif hcs.get("extra_figures_found"):
        if not any(v.get("code") == "hallucinated_character" for v in violations):
            descs = hcs.get("descriptions") or []
            violations.append({
                "code": "hallucinated_character",
                "severity": "high",
                "detail": "; ".join(descs) if descs else "unexpected extra figure(s) in frame",
            })

    # 4. Emblem conformance — enforce when emblem is visibly on forehead. The
    # scan may legitimately be skipped when no emblem is rendered; in that case
    # the forehead-only rule + back-of-head scan already cover the concern.
    ecs = result.get("emblem_conformance_scan") or {}
    emblems = result.get("emblems_detected") or []
    emblem_on_forehead = any(
        (e.get("location") == "forehead") for e in emblems if isinstance(e, dict)
    )
    if emblem_on_forehead and not ecs.get("performed"):
        violations.append({
            "code": "audit_scan_skipped",
            "severity": "high",
            "detail": "emblem_conformance_scan not performed despite emblem on forehead",
        })
    elif ecs.get("performed"):
        def _dupe(code: str) -> bool:
            return any(v.get("code") == code for v in violations)
        if ecs.get("color_ok") is False and not _dupe("emblem_color_drift"):
            violations.append({"code": "emblem_color_drift", "severity": "high",
                               "detail": ecs.get("notes") or "emblem color diverges from canonical"})
        if ecs.get("shape_ok") is False and not _dupe("emblem_shape_drift"):
            violations.append({"code": "emblem_shape_drift", "severity": "high",
                               "detail": ecs.get("notes") or "emblem shape diverges from canonical"})
        if ecs.get("tips_ok") is False and not _dupe("emblem_tips_wrong"):
            violations.append({"code": "emblem_tips_wrong", "severity": "high",
                               "detail": ecs.get("notes") or "crescent tips not pointing up"})
        if ecs.get("position_ok") is False and not _dupe("emblem_position_drift"):
            violations.append({"code": "emblem_position_drift", "severity": "high",
                               "detail": ecs.get("notes") or "emblem drifted off-center"})
        if ecs.get("single_ok") is False and not _dupe("multiple_emblems"):
            violations.append({"code": "multiple_emblems", "severity": "high",
                               "detail": ecs.get("notes") or "more than one emblem rendered"})


def _enforce_strict_cross_checks(result: dict[str, Any]) -> None:
    """Apply absolute invariants the auditor must never let through, even if
    the model's own JSON surface looks clean. These encode 'if X and Y, FAIL'
    rules that protect against Opus non-determinism on the individual fields.
    """
    violations = result.setdefault("violations", [])
    facing = (result.get("facing") or "").lower()
    forehead_visible = result.get("forehead_visible")
    emblems = result.get("emblems_detected") or []
    emblems_anywhere = len([e for e in emblems if isinstance(e, dict) and e.get("location")]) > 0

    def _dupe(code: str) -> bool:
        return any(v.get("code") == code for v in violations)

    # Invariant 1: facing=back / back_three_quarter → emblem MUST be absent.
    if facing in ("back", "back_three_quarter") and emblems_anywhere:
        if not _dupe("emblem_when_forehead_hidden") and not _dupe("emblem_on_back_of_head"):
            violations.append({
                "code": "emblem_when_forehead_hidden",
                "severity": "high",
                "detail": f"facing={facing} but emblem rendered on {emblems[0].get('location')}",
            })

    # Invariant 2: forehead_visible=false → emblem MUST be absent (same family
    # as above, covers hooded/profile-with-occluded-forehead).
    if forehead_visible is False and emblems_anywhere:
        if not _dupe("emblem_when_forehead_hidden"):
            violations.append({
                "code": "emblem_when_forehead_hidden",
                "severity": "high",
                "detail": f"forehead_visible=false but emblem rendered on {emblems[0].get('location')}",
            })

    # Invariant 3: any emblem location outside forehead is a violation.
    off_forehead = [e for e in emblems
                    if isinstance(e, dict)
                    and e.get("location")
                    and e.get("location") != "forehead"
                    and e.get("location") != "reflection"]
    for e in off_forehead:
        loc = e.get("location")
        if loc in ("back_of_head", "crown", "nape") and not _dupe("emblem_on_back_of_head"):
            violations.append({"code": "emblem_on_back_of_head", "severity": "high",
                               "detail": e.get("notes") or f"emblem at {loc}"})
        elif loc == "sky" and not _dupe("moon_in_sky"):
            violations.append({"code": "moon_in_sky", "severity": "high",
                               "detail": e.get("notes") or "crescent in sky/background"})
        elif not _dupe("emblem_on_wrong_part"):
            violations.append({"code": "emblem_on_wrong_part", "severity": "high",
                               "detail": f"emblem at {loc}: {e.get('notes') or ''}"})


def _load_shot_override(scene_id: str) -> dict[str, Any]:
    """Look up per-shot override record from the project's kling_prompt_overrides.json.

    The override file lives at output/projects/default/prompt_os/kling_prompt_overrides.json
    and stores per-shot fields like anchor_extra, needs_anchor_regen, and the new
    min_frame_coverage_override / render_as_pov flags the auditor honors.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(
        root, "output", "projects", "default", "prompt_os", "kling_prompt_overrides.json"
    )
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}
    overrides = data.get("overrides", {}) or {}
    return overrides.get(scene_id) or {}


def audit_scene_anchor(
    scene: dict[str, Any],
    anchor_path: str,
    character_rules: dict[str, Any] | None = None,
    model: str | None = None,
    callout_path: str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper that extracts shot context from a scene dict.

    Respects per-shot overrides from kling_prompt_overrides.json:
      - min_frame_coverage_override: merge into character_rules["min_frame_coverage"]
        to relax the coverage floor for intentionally-wide shots.
      - render_as_pov: exempt the shot from subject_too_small and proportions checks
        entirely — POV shots may show only environment + minimal bear silhouette.
    """
    shot_context = {
        "name": scene.get("name"),
        "shotDescription": scene.get("shotDescription"),
        "cameraAngle": scene.get("cameraAngle"),
    }
    sid = scene.get("id") or ""
    override = _load_shot_override(sid)

    merged_rules = dict(character_rules or {})
    cov_override = override.get("min_frame_coverage_override")
    if isinstance(cov_override, dict):
        base_cov = dict((character_rules or {}).get("min_frame_coverage")
                        or DEFAULT_CHARACTER_RULES["min_frame_coverage"])
        base_cov.update({k: v for k, v in cov_override.items() if isinstance(v, (int, float))})
        merged_rules["min_frame_coverage"] = base_cov

    if override.get("render_as_pov"):
        shot_context["render_as_pov"] = True

    verdict = audit_anchor(anchor_path, merged_rules or character_rules, shot_context,
                           model, callout_path=callout_path)

    if override.get("render_as_pov"):
        vs = verdict.get("violations") or []
        filtered = [v for v in vs if v.get("code") not in
                    ("subject_too_small", "proportions_drift", "pose_mismatch_description", "facing_mismatch")]
        if len(filtered) != len(vs):
            verdict["violations"] = filtered
            verdict["pass"] = len(filtered) == 0

    force_pass = override.get("force_pass_on_violations")
    if isinstance(force_pass, list) and force_pass:
        allowed = {c for c in force_pass if isinstance(c, str)}
        vs = verdict.get("violations") or []
        kept = [v for v in vs if v.get("code") not in allowed]
        dropped = [v for v in vs if v.get("code") in allowed]
        if dropped:
            verdict["violations"] = kept
            verdict["force_passed_codes"] = [v.get("code") for v in dropped]
            verdict["pass"] = len(kept) == 0

    return verdict


def audit_batch(
    scenes: list[dict[str, Any]],
    anchors_dir: str,
    character_rules: dict[str, Any] | None = None,
    model: str | None = None,
    callout_path: str | None = None,
) -> dict[str, Any]:
    """Audit every scene that has an anchor on disk. Returns a summary dict."""
    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    missing = 0
    import glob as _glob
    for scene in scenes:
        sid = scene.get("id", "")
        # Candidate paths: per-user namespace (newer, authenticated UI writes here)
        # first, then flat (legacy). Among per-user dirs, pick the most recent file.
        candidates: list[str] = []
        for user_dir in sorted(_glob.glob(os.path.join(anchors_dir, "u_*", sid, "selected.png"))):
            candidates.append(user_dir)
        flat = os.path.join(anchors_dir, sid, "selected.png")
        if os.path.isfile(flat):
            candidates.append(flat)
        candidates = [p for p in candidates if os.path.isfile(p)]
        if not candidates:
            missing += 1
            results.append({
                "id": sid,
                "name": scene.get("name"),
                "status": "missing_anchor",
            })
            continue
        anchor_path = max(candidates, key=os.path.getmtime)
        verdict = audit_scene_anchor(scene, anchor_path, character_rules, model,
                                     callout_path=callout_path)
        entry = {
            "id": sid,
            "name": scene.get("name"),
            "anchor_path": anchor_path,
            **verdict,
        }
        results.append(entry)
        if verdict.get("pass"):
            passed += 1
        else:
            failed += 1
    return {
        "total": len(scenes),
        "passed": passed,
        "failed": failed,
        "missing": missing,
        "results": results,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m lib.anchor_auditor <anchor_path>")
        sys.exit(1)
    out = audit_anchor(sys.argv[1])
    print(json.dumps(out, indent=2))
