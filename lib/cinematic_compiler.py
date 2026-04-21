"""
Cinematic Compiler — translates production plans into API payloads.

Reads:
  - production_plan_v3/v4.json (beats, shots, scene stills, anchors)
  - model_profile.json (engine capabilities)
  - packages.json (character/environment asset paths)

Outputs payloads for each pipeline stage:
  - Identity/location sheet generation (Gemini text-to-image)
  - Scene still generation (Gemini edit with refs)
  - Shot anchor generation (Gemini edit with refs + scene still)
  - Video generation (Kling image-to-video, camera+motion prompts only)
  - Transition intelligence (judge + strategy per cut point)
  - Editorial conform (ffmpeg export)
"""

import json
import os

_DEFAULT_NEGATIVE = (
    "blur, distortion, low quality, watermark, text, cartoon, anime, "
    "deformed face, extra limbs, shaky camera"
)

# Style bible energy variants (appended to base style bible)
_STYLE_ENERGY = {
    "low": ", slow deliberate camera drift, contemplative stillness, held frames",
    "medium": ", steady natural rhythm, balanced pacing",
    "high": ", dynamic handheld energy, urgent forward motion, quick cuts",
}


# ---------------------------------------------------------------------------
# Rule-Based Decision Functions
# ---------------------------------------------------------------------------

def choose_duration(shot, beat_energy=0.5):
    """Auto-select shot duration based on framing and energy.

    Wide + low energy = longer. Close + high energy = shorter.
    Returns integer 3-15.
    """
    framing = shot.get("framing", "").lower()
    explicit = shot.get("duration")

    # If plan specifies a duration, respect it
    if explicit:
        return int(explicit)

    # Framing base
    if "wide" in framing or "establishing" in framing:
        base = 8
    elif "close" in framing:
        base = 4
    else:
        base = 5  # medium

    # Energy modifier
    if beat_energy <= 0.3:
        base += 2  # slow = longer holds
    elif beat_energy >= 0.7:
        base -= 1  # fast = tighter cuts

    return max(3, min(15, base))


def choose_negative_prompt(shot, beat):
    """Build per-shot negative prompt from context.

    Adds specific exclusions based on character count, framing, and action.
    """
    parts = [_DEFAULT_NEGATIVE]

    # Character-count negatives
    chars = beat.get("characters", [])
    if len(chars) == 1:
        parts.append("extra people, extra animals")
    elif len(chars) == 2:
        parts.append("third character, extra people")
    elif len(chars) >= 3:
        parts.append("extra people, wrong clothing")

    # Framing negatives
    framing = shot.get("framing", "").lower()
    if "close" in framing:
        parts.append("wide angle distortion")
    if "wide" in framing:
        parts.append("macro distortion")

    # Action negatives
    action = shot.get("action", "").lower()
    if any(w in action for w in ["sprint", "run", "chase", "fast"]):
        parts.append("slow motion, floating, static")
    if any(w in action for w in ["still", "freeze", "holds", "sits"]):
        parts.append("jittery, shaking, bouncing")

    return ", ".join(parts)


def choose_style_suffix(style_bible, energy=0.5, is_video=True):
    """Customize style bible suffix based on energy and context.

    Returns the style bible with energy-appropriate motion language appended.
    """
    if not style_bible:
        return ""

    if not is_video:
        return style_bible  # image prompts get the base style only

    if energy <= 0.3:
        return style_bible + _STYLE_ENERGY["low"]
    if energy >= 0.7:
        return style_bible + _STYLE_ENERGY["high"]
    return style_bible + _STYLE_ENERGY["medium"]


def choose_anchor_ref_order(shot, ref_paths, scene_still_path=None):
    """Reorder reference images based on shot framing priority.

    Close-up/emotional → character sheet first.
    Wide/establishing → environment sheet first.
    Scene still always second (spatial context).
    """
    if not ref_paths:
        return ref_paths

    framing = shot.get("framing", "").lower()
    is_character_focus = any(w in framing for w in [
        "close", "medium", "emotional", "portrait",
    ])
    is_environment_focus = any(w in framing for w in [
        "wide", "establishing", "re-establish",
    ])

    # Separate character refs, environment refs, and scene still
    char_refs = [p for p in ref_paths if "char" in p.lower()]
    env_refs = [p for p in ref_paths if "envi" in p.lower()]
    other_refs = [p for p in ref_paths if p not in char_refs and p not in env_refs]

    if is_character_focus:
        ordered = char_refs + env_refs + other_refs
    elif is_environment_focus:
        ordered = env_refs + char_refs + other_refs
    else:
        ordered = ref_paths  # default plan order

    # Scene still as second ref (spatial context, not dominant)
    if scene_still_path and scene_still_path in ordered:
        ordered.remove(scene_still_path)
        ordered.insert(min(1, len(ordered)), scene_still_path)

    return ordered


def should_multi_shot(beat):
    """Auto-detect whether a beat's shots should be multi-shot grouped.

    Criteria: same beat, 2+ shots, energy > 0.5, action continuity keywords.
    """
    shots = beat.get("shots", [])
    if len(shots) < 2:
        return False

    # Already manually set
    if beat.get("multi_shot_group") is not None:
        return beat["multi_shot_group"]

    energy = beat.get("energy", 0.5)
    if energy < 0.5:
        return False

    # Check for action continuity keywords in consecutive shots
    continuity_words = {"sprint", "run", "leap", "chase", "catches", "grabs",
                        "continues", "follows", "into", "onto"}
    for i in range(len(shots) - 1):
        action_a = set(shots[i].get("action", "").lower().split())
        action_b = set(shots[i + 1].get("action", "").lower().split())
        if continuity_words & (action_a | action_b):
            return True

    return False


def needs_transition_anchor(beat_a, beat_b):
    """Decide whether a transition needs a dedicated bridge shot
    or if redesigning the exit/entry anchors could handle it.

    Returns True if a transition anchor is needed (compositions too different).
    Returns False if anchor redesign could bridge the gap.
    """
    shots_a = beat_a.get("shots", [])
    shots_b = beat_b.get("shots", [])
    if not shots_a or not shots_b:
        return True

    last_shot = shots_a[-1]
    first_shot = shots_b[0]

    diff_count = 0

    # 1. Shot size difference
    size_a = last_shot.get("framing", "").lower()
    size_b = first_shot.get("framing", "").lower()
    size_cats = {"wide", "establishing", "medium", "close", "tight"}
    cat_a = next((c for c in size_cats if c in size_a), "medium")
    cat_b = next((c for c in size_cats if c in size_b), "medium")
    if cat_a != cat_b:
        diff_count += 1

    # 2. Camera height difference
    h_a = last_shot.get("camera_height", "").lower()
    h_b = first_shot.get("camera_height", "").lower()
    if h_a and h_b and h_a != h_b:
        diff_count += 1

    # 3. New characters introduced
    chars_a = set(beat_a.get("characters", []))
    chars_b = set(beat_b.get("characters", []))
    new_chars = chars_b - chars_a
    if new_chars:
        diff_count += 1

    # 4. Different environment area
    loc_a = beat_a.get("location_desc", "")
    loc_b = beat_b.get("location_desc", "")
    if loc_a and loc_b and loc_a != loc_b:
        diff_count += 1

    # 5. Conflicting shot purposes
    purpose_intimate = {"close", "emotional", "portrait", "reaction"}
    purpose_spatial = {"wide", "establishing", "two-shot", "dynamic"}
    a_intimate = any(w in size_a for w in purpose_intimate)
    b_spatial = any(w in size_b for w in purpose_spatial)
    a_spatial = any(w in size_a for w in purpose_spatial)
    b_intimate = any(w in size_b for w in purpose_intimate)
    if (a_intimate and b_spatial) or (a_spatial and b_intimate):
        diff_count += 1

    # 3+ differences → transition anchor needed
    return diff_count >= 3


# ---------------------------------------------------------------------------
# Tier 2 Rule Functions
# ---------------------------------------------------------------------------

def choose_reestablishing(beats):
    """Auto-insert re-establishing wide shots when close-up count exceeds threshold.

    Rule: after 5-8 consecutive non-wide shots in the same location,
    inject a re-establishing wide. Returns list of insertion points.

    Each insertion: {after_beat_id, after_shot_id, location_pkg, reason}
    """
    insertions = []
    non_wide_count = 0
    current_location = None
    threshold = 6  # middle of 5-8 range

    for beat in beats:
        location = beat.get("location_pkg", "")

        # Location change resets counter (new space = fresh mental map)
        if location != current_location:
            non_wide_count = 0
            current_location = location

        for shot in beat.get("shots", []):
            framing = shot.get("framing", "").lower()
            is_wide = any(w in framing for w in ["wide", "establishing",
                                                   "re-establish"])
            if is_wide:
                non_wide_count = 0
            else:
                non_wide_count += 1

            if non_wide_count >= threshold:
                insertions.append({
                    "after_beat_id": beat["beat_id"],
                    "after_shot_id": shot["shot_id"],
                    "location_pkg": current_location,
                    "reason": (f"{non_wide_count} non-wide shots since last "
                               f"wide — audience needs spatial re-anchor"),
                })
                non_wide_count = 0  # reset after scheduling insertion

    return insertions


# Default camera heights per framing type
_CAMERA_HEIGHT_DEFAULTS = {
    "wide": "eye_level",
    "establishing": "slightly_high",
    "re-establish": "eye_level",
    "medium": "eye_level",
    "close": "eye_level",
    "tight": "eye_level",
    "low_angle": "low",
    "high_angle": "high",
    "overhead": "overhead",
    "ground": "ground",
}


def choose_camera_height(shot, location_heights=None):
    """Enforce consistent camera height per location.

    If the shot has an explicit camera_height, respect it.
    Otherwise, check if this location has a locked default.
    Falls back to framing-based default.

    Args:
        shot: shot dict from production plan
        location_heights: dict of {location_pkg: default_height} built up
                         across the plan. Mutated in-place to lock heights.

    Returns: camera height string
    """
    if location_heights is None:
        location_heights = {}

    # Explicit override always wins
    explicit = shot.get("camera_height")
    if explicit:
        return explicit

    location = shot.get("location_pkg", "")
    framing = shot.get("framing", "").lower()

    # Check framing for specific angle keywords first
    for keyword, height in _CAMERA_HEIGHT_DEFAULTS.items():
        if keyword in framing:
            # Lock this as default for the location if not yet set
            if location and location not in location_heights:
                location_heights[location] = height
            return height

    # Use locked location default if available
    if location and location in location_heights:
        return location_heights[location]

    # Fallback
    return "eye_level"


def truncate_prompt_smart(prompt, max_chars=2500, priorities=None):
    """Priority-based prompt truncation.

    Instead of blindly cutting at max_chars, trim low-priority segments first.
    Priority order (highest to lowest):
      1. Camera/motion instruction (the core I2V directive)
      2. Subject action (what's happening)
      3. Style bible (visual consistency)
      4. Atmosphere/mood modifiers
      5. Negative descriptors embedded in positive prompt

    Args:
        prompt: full prompt string
        max_chars: character limit
        priorities: optional list of (segment, priority) tuples

    Returns: truncated prompt within limit
    """
    if len(prompt) <= max_chars:
        return prompt

    # Split on sentence boundaries (period + space, or comma for long lists)
    import re
    segments = re.split(r'(?<=[.!])\s+', prompt)
    if len(segments) <= 1:
        return prompt[:max_chars]

    # Score each segment by priority keywords
    high_priority = {"camera", "push", "pull", "pan", "tilt", "dolly",
                     "tracking", "follows", "slowly", "drift"}
    medium_priority = {"subject", "walks", "runs", "turns", "looks",
                       "holds", "reaches", "sits", "stands"}
    style_keywords = {"cinematic", "35mm", "film", "grain", "golden",
                      "shallow", "depth", "warm", "tones"}

    scored = []
    for seg in segments:
        words = set(seg.lower().split())
        if words & high_priority:
            score = 3
        elif words & medium_priority:
            score = 2
        elif words & style_keywords:
            score = 1
        else:
            score = 0
        scored.append((score, seg))

    # Sort by priority (highest first), rebuild within limit
    scored.sort(key=lambda x: -x[0])

    result_parts = []
    current_len = 0
    for score, seg in scored:
        if current_len + len(seg) + 2 <= max_chars:
            result_parts.append(seg)
            current_len += len(seg) + 2  # +2 for ". " joiner

    # Restore original order
    original_order = {seg: i for i, seg in enumerate(segments)}
    result_parts.sort(key=lambda s: original_order.get(s, 999))

    # Join without double periods
    joined = " ".join(result_parts)
    return joined.replace("..", ".").strip()


def vary_transition_prompt(beat_a, beat_b, packages, style_bible=""):
    """Generate context-specific transition shot prompts instead of generic ones.

    Analyzes the emotional/spatial shift between beats and tailors the
    camera movement + framing for the bridge shot.

    Returns: {anchor_prompt, video_prompt}
    """
    energy_a = beat_a.get("energy", 0.5)
    energy_b = beat_b.get("energy", 0.5)
    delta = energy_b - energy_a  # positive = rising, negative = falling

    loc_a = beat_a.get("location_desc", "")
    loc_b = beat_b.get("location_desc", "")
    same_location = (beat_a.get("location_pkg") == beat_b.get("location_pkg"))

    chars_b = [_resolve_pkg_name(c, packages)
               for c in beat_b.get("characters", [])]
    char_str = ", ".join(chars_b) if chars_b else ""

    # --- Anchor prompt: still image for the transition first frame ---
    if same_location:
        # Same space: wider framing to show spatial relationship
        anchor_prompt = (
            f"Wide shot showing the full space. "
            f"{loc_a}. "
            f"{char_str + ' visible at a distance. ' if char_str else ''}"
            f"Golden hour warm light. Deep perspective. "
            f"Photorealistic 35mm film."
        )
    else:
        # Different space: foreground old location, background new
        anchor_prompt = (
            f"Wide establishing shot showing spatial transition. "
            f"Foreground: {loc_a}. Background/distance: {loc_b}. "
            f"{char_str + ' visible in the distance. ' if char_str else ''}"
            f"Golden hour warm light. Deep perspective. "
            f"Photorealistic 35mm film."
        )

    # --- Video prompt: camera movement for the 3s bridge ---
    if delta > 0.3:
        # Rising energy: camera accelerates toward new scene
        camera_move = (
            "Camera pushes forward with building momentum. "
            "Movement accelerates gently, revealing the new space ahead."
        )
    elif delta < -0.3:
        # Falling energy: camera drifts, exhales
        camera_move = (
            "Camera slowly drifts back, settling into stillness. "
            "Movement decelerates, the space opens and breathes."
        )
    else:
        # Neutral: steady reveal
        camera_move = (
            "Camera slowly pulls back and pans to reveal the wider space. "
            "Subject moves naturally into the new area."
        )

    # Add subject if walking between areas
    if not same_location and char_str:
        camera_move += f" {char_str.split(',')[0]} walks forward."

    style_suffix = choose_style_suffix(
        style_bible, (energy_a + energy_b) / 2, is_video=True)
    video_prompt = f"{camera_move} {style_suffix}"

    return {
        "anchor_prompt": truncate_prompt_smart(anchor_prompt, 2500),
        "video_prompt": truncate_prompt_smart(video_prompt, 2500),
    }


# Mapping from our scored transition types → video_stitcher transition names
_CONFORM_TRANSITION_MAP = {
    "hard_cut": "hard_cut",
    "smash_cut": "hard_cut",           # same ffmpeg impl, velocity is in content
    "eyeline_cut": "hard_cut",         # gaze carries the cut, hard splice
    "audio_bridge": "hard_cut",        # visual hard cut, audio bleed post
    "hard_cut_audio_bridge": "hard_cut",
    "match_cut": "hard_cut",           # geometry match does the work, not ffmpeg
    "transition_shot": "hard_cut",     # we INSERT a clip, not dissolve
    "fade_to_black": "fade_black",
    "fade_to_black_transition": "fade_black",
}


def conform_from_payload(payload):
    """Convert compile_conform_payload output into stitch() arguments.

    Expands transition_shot clips into the clip list (inserted between
    the two beats they bridge). Maps our scored transition types to
    ffmpeg-compatible names for video_stitcher.stitch().

    Returns: {clip_paths, transitions, audio_path, output_path, fade_dur}
    """
    clips = payload.get("clips", [])
    scored_transitions = payload.get("transitions", [])
    audio = payload.get("audio_path")
    output = payload.get("output_path", "output/pipeline/final/cinematic_v3.mp4")

    clip_paths = []
    stitch_transitions = []  # one per clip, first is ignored by stitcher

    for ci, clip in enumerate(clips):
        path = clip["path"]

        # First clip: no transition into it
        if ci == 0:
            clip_paths.append(path)
            stitch_transitions.append("hard_cut")  # placeholder, ignored
            continue

        # Get the transition BEFORE this clip
        trans_idx = ci - 1
        if trans_idx < len(scored_transitions):
            trans = scored_transitions[trans_idx]
            trans_type = trans.get("type", "hard_cut")
            trans_clip = trans.get("clip_path")

            # If transition_shot has a generated clip, INSERT it
            if trans_type == "transition_shot" and trans_clip and os.path.isfile(trans_clip):
                # Hard cut into the transition clip
                clip_paths.append(trans_clip)
                stitch_transitions.append("hard_cut")
                # Hard cut from transition clip into next beat
                clip_paths.append(path)
                stitch_transitions.append("hard_cut")
            else:
                # Map our type to ffmpeg name
                ffmpeg_type = _CONFORM_TRANSITION_MAP.get(trans_type, "hard_cut")
                clip_paths.append(path)
                stitch_transitions.append(ffmpeg_type)
        else:
            clip_paths.append(path)
            stitch_transitions.append("hard_cut")

    # Final transition (fade out)
    final = payload.get("final_transition", {"type": "fade", "duration": 2.0})
    fade_dur = final.get("duration", 2.0)

    return {
        "clip_paths": clip_paths,
        "transitions": stitch_transitions,
        "audio_path": audio,
        "output_path": output,
        "fade_dur": fade_dur,
    }


def load_json(path):
    with open(path) as f:
        return json.load(f)


def _resolve_pkg_path(pkg_id, packages):
    """Get hero_image_path for a package ID."""
    for pkg in packages.get("packages", []):
        if pkg["package_id"] == pkg_id:
            return pkg.get("hero_image_path", "")
    return ""


def _resolve_pkg_name(pkg_id, packages):
    """Get name for a package ID."""
    for pkg in packages.get("packages", []):
        if pkg["package_id"] == pkg_id:
            return pkg.get("name", pkg_id)
    return pkg_id


# ---------------------------------------------------------------------------
# Stage 2/3: Identity & Location Sheet Payloads
# ---------------------------------------------------------------------------

def compile_sheet_payloads(plan, packages, sheet_type="character"):
    """Generate sheet payloads for characters or locations.

    Args:
        sheet_type: "character" or "environment"

    Returns list of {pkg_id, name, prompt, output_path}.
    """
    if sheet_type == "character":
        items = plan.get("characters", [])
    else:
        items = plan.get("locations", [])

    payloads = []
    for item in items:
        pkg_id = item["pkg"]
        name = item["name"]

        # Find the package to get its description for prompt
        pkg_data = None
        for pkg in packages.get("packages", []):
            if pkg["package_id"] == pkg_id:
                pkg_data = pkg
                break

        if not pkg_data:
            continue

        pkg_dir = os.path.join("output/preproduction", pkg_id)
        payloads.append({
            "pkg_id": pkg_id,
            "name": name,
            "pkg_data": pkg_data,
            "output_path": os.path.join(pkg_dir, "sheet.png"),
        })

    return payloads


# ---------------------------------------------------------------------------
# Stage 4: Scene Still Payloads
# ---------------------------------------------------------------------------

def compile_scene_still_payloads(plan, profile, packages):
    """Generate scene still payloads — one mood image per beat.

    Scene stills establish the emotional palette and spatial context.
    They are NOT shots — no camera spec, no lens, no framing.

    Refs: character sheet(s) + environment sheet for this beat.

    Returns list of {beat_id, prompt, reference_image_paths, output_path}.
    """
    payloads = []

    for beat in plan.get("beats", []):
        beat_id = beat["beat_id"]

        # Skip if already generated and approved
        if beat.get("scene_still_status") == "approved":
            continue

        # Collect reference images
        ref_paths = []
        for pkg_id in beat.get("scene_still_refs", []):
            path = _resolve_pkg_path(pkg_id, packages)
            if path and os.path.isfile(path):
                ref_paths.append(path)

        prompt = beat.get("scene_still_prompt", "")
        if not prompt:
            continue

        payloads.append({
            "beat_id": beat_id,
            "title": beat.get("title", ""),
            "prompt": prompt,
            "reference_image_paths": ref_paths,
            "output_path": f"output/pipeline/scene_stills/{beat_id}.png",
        })

    return payloads


# ---------------------------------------------------------------------------
# Stage 6: Shot Anchor Payloads
# ---------------------------------------------------------------------------

def compile_shot_anchor_payloads(plan, profile, packages):
    """Generate shot anchor payloads — exact first frame per shot.

    The anchor encodes: camera height, lens, framing, pose, action state,
    lighting direction. This is the bridge between stills and video.

    Refs: character sheet(s) + environment sheet + scene still (if available).

    Returns list of {beat_id, shot_id, prompt, camera_height, reference_image_paths, output_path}.
    """
    payloads = []
    location_heights = {}  # tracks locked camera heights per location

    for beat in plan.get("beats", []):
        beat_id = beat["beat_id"]
        scene_still = beat.get("scene_still_path")

        for shot in beat.get("shots", []):
            shot_id = shot["shot_id"]

            # Skip if already approved
            if shot.get("anchor_status") == "approved":
                continue

            # Collect reference images: character sheets + env sheet
            ref_paths = []
            for pkg_id in shot.get("anchor_refs", []):
                path = _resolve_pkg_path(pkg_id, packages)
                if path and os.path.isfile(path):
                    ref_paths.append(path)

            # Add scene still as additional ref (mood/spatial context)
            if scene_still and os.path.isfile(scene_still):
                ref_paths.append(scene_still)

            # Rule: reorder refs based on shot framing priority
            ref_paths = choose_anchor_ref_order(shot, ref_paths,
                                                 scene_still_path=scene_still)

            prompt = shot.get("anchor_prompt", "")
            if not prompt:
                continue

            # Rule: consistent camera height per location
            shot["location_pkg"] = beat.get("location_pkg", "")
            cam_height = choose_camera_height(shot, location_heights)

            payloads.append({
                "beat_id": beat_id,
                "shot_id": shot_id,
                "prompt": truncate_prompt_smart(prompt, 2500),
                "camera_height": cam_height,
                "reference_image_paths": ref_paths,
                "output_path": f"output/pipeline/anchors_v3/{shot_id}.png",
            })

    return payloads


# ---------------------------------------------------------------------------
# Stage 7/8: Video Payloads
# ---------------------------------------------------------------------------

def compile_video_payloads(plan, profile, packages, tier="preview"):
    """Generate video payloads from shot anchors.

    Rules:
      - Video prompt = camera + motion ONLY. Subject is in the anchor.
      - Style bible appended to every prompt.
      - Duration: V3 Pro clamps to 5/10. V3 Standard & O3 allow 3-15.
      - Elements always None (character refs cause artifacts).
      - Multi-shot only within same beat when beat.multi_shot_group is true.

    Returns list of dicts ready for fal_client.
    """
    # Resolve engine tier
    tier_key = profile.get("tiers", {}).get(tier, "video_engine")
    vid_profile = profile.get(tier_key, profile.get("video_engine", {}))
    engine_id = vid_profile.get("id", "kling_v3_pro")

    tier_map = {
        "kling_v3_standard": "v3_standard",
        "kling_v3_pro": "v3_pro",
        "kling_o3_standard": "o3_standard",
        "kling_o3_pro": "o3_pro",
    }
    fal_tier = tier_map.get(engine_id, "v3_standard")

    style_bible = plan.get("style_bible", "")
    payloads = []

    for beat in plan.get("beats", []):
        beat_id = beat["beat_id"]
        beat_energy = beat.get("energy", 0.5)
        shots = beat.get("shots", [])

        if not shots:
            continue

        # Rule: auto-detect multi-shot grouping if not explicitly set
        use_multi = should_multi_shot(beat)

        # Multi-shot group: combine shots within this beat into one call
        if use_multi and len(shots) > 1:
            first_shot = shots[0]
            anchor_path = first_shot.get("anchor_path")
            if not anchor_path:
                anchor_path = f"output/pipeline/anchors_v3/{first_shot['shot_id']}.png"

            multi_prompt = []
            shot_ids = []
            total_dur = 0
            for shot in shots:
                prompt = shot.get("video_prompt", "")
                # Rule: energy-aware style suffix
                styled = choose_style_suffix(style_bible, beat_energy,
                                              is_video=True)
                if styled and styled not in prompt:
                    prompt = f"{prompt} {styled}"
                # Rule: auto-duration from framing + energy
                d = _clamp_duration(
                    choose_duration(shot, beat_energy), fal_tier)
                multi_prompt.append({
                    "prompt": truncate_prompt_smart(prompt, 512),
                    "duration": str(d),
                })
                shot_ids.append(shot["shot_id"])
                total_dur += d

            # Rule: context-aware negative prompt
            neg = choose_negative_prompt(shots[0], beat)

            payloads.append({
                "beat_id": beat_id,
                "shot_ids": shot_ids,
                "is_multi_shot": True,
                "start_image_path": anchor_path,
                "end_image_path": None,
                "multi_prompt": multi_prompt,
                "duration": total_dur,
                "tier": fal_tier,
                "elements": None,
                "negative_prompt": neg,
                "generate_audio": True,
            })

        else:
            # Individual shots
            for shot in shots:
                shot_id = shot["shot_id"]
                anchor_path = shot.get("anchor_path")
                if not anchor_path:
                    anchor_path = f"output/pipeline/anchors_v3/{shot_id}.png"

                prompt = shot.get("video_prompt", "")
                # Rule: energy-aware style suffix
                styled = choose_style_suffix(style_bible, beat_energy,
                                              is_video=True)
                if styled and styled not in prompt:
                    prompt = f"{prompt} {styled}"

                # Rule: auto-duration from framing + energy
                duration = _clamp_duration(
                    choose_duration(shot, beat_energy), fal_tier)

                # Rule: context-aware negative prompt
                neg = choose_negative_prompt(shot, beat)

                payloads.append({
                    "beat_id": beat_id,
                    "shot_id": shot_id,
                    "is_multi_shot": False,
                    "start_image_path": anchor_path,
                    "end_image_path": None,
                    "prompt": truncate_prompt_smart(prompt, 2500),
                    "duration": duration,
                    "tier": fal_tier,
                    "elements": None,
                    "negative_prompt": neg,
                    "generate_audio": True,
                })

    return payloads


def _clamp_duration(dur, fal_tier="v3_pro"):
    """Clamp duration to valid values for the given tier.

    V3 Pro: 5 or 10 only.
    V3 Standard / O3: 3-15 (any integer).
    """
    dur = max(3, min(15, int(dur)))
    if fal_tier == "v3_pro":
        return 5 if dur <= 7 else 10
    return dur


# ---------------------------------------------------------------------------
# Stage 8b: Transition Decision System
# ---------------------------------------------------------------------------

def _energy_score(delta):
    """Score energy delta between beats."""
    if delta < 0.2:
        return 0
    if delta < 0.5:
        return 1
    return 2


def _character_overlap_score(chars_a, chars_b):
    """Score character change between beats."""
    set_a = set(chars_a) if chars_a else set()
    set_b = set(chars_b) if chars_b else set()
    if set_a == set_b:
        return 0
    if set_a & set_b:
        return 1  # partial overlap
    return 2  # entirely new characters


def _beat_type_score(arc_a, arc_b):
    """Score narrative arc adjacency."""
    arc_order = ["setup", "rising", "climax", "resolution"]
    try:
        idx_a = arc_order.index(arc_a)
        idx_b = arc_order.index(arc_b)
    except ValueError:
        return 1
    gap = abs(idx_b - idx_a)
    if gap <= 0:
        return 0
    if gap <= 1:
        return 1
    return 2


def _emotion_score(emo_a, emo_b):
    """Score emotional shift between beats.

    Simple heuristic: if emotion strings share any keywords, lower score.
    """
    if not emo_a or not emo_b:
        return 1
    words_a = set(emo_a.lower().replace(",", " ").split())
    words_b = set(emo_b.lower().replace(",", " ").split())
    overlap = words_a & words_b
    if len(overlap) >= 2:
        return 0
    if overlap:
        return 1
    return 2


def choose_transition(beat_a, beat_b):
    """Auto-decide transition type between two beats.

    Uses a 6-factor scoring system from film editing theory (Murch).
    Exit motivation overrides score — emotion beats everything.

    Returns dict: {type, score, reason, needs_transition_shot}
    """
    exit_trans = beat_a.get("transition_out", {})
    exit_type = exit_trans.get("motivation", "")
    energy_a = beat_a.get("energy", 0.5)
    energy_b = beat_b.get("energy", 0.5)
    delta = abs(energy_a - energy_b)

    # === Override checks (Murch: emotion beats everything) ===

    if exit_type in ("cut_on_action", "action"):
        return {
            "type": "hard_cut",
            "score": 0,
            "reason": f"Action exit override — {exit_trans.get('note', 'action carries the cut')}",
            "needs_transition_shot": False,
        }

    if exit_type == "fade_to_black" or exit_trans.get("type") == "fade":
        return {
            "type": "fade_to_black",
            "score": 99,
            "reason": "Fade exit — always honor",
            "needs_transition_shot": False,
        }

    if exit_type in ("smash_cut", "freeze"):
        if delta > 0.3:
            return {
                "type": "smash_cut",
                "score": 0,
                "reason": f"Freeze exit + energy delta {delta:.1f} — smash cut",
                "needs_transition_shot": False,
            }
        return {
            "type": "hard_cut",
            "score": 0,
            "reason": "Freeze exit, low energy delta — hard cut",
            "needs_transition_shot": False,
        }

    # === Score accumulation ===

    score = 0
    reasons = []

    # Energy delta
    e = _energy_score(delta)
    score += e
    if e:
        reasons.append(f"energy_delta={delta:.1f}(+{e})")

    # Location change
    loc_a = beat_a.get("location_pkg", "")
    loc_b = beat_b.get("location_pkg", "")
    loc_desc_a = beat_a.get("location_desc", "")
    loc_desc_b = beat_b.get("location_desc", "")
    if loc_a != loc_b or loc_desc_a != loc_desc_b:
        score += 2
        reasons.append("location_change(+2)")

    # Character change
    c = _character_overlap_score(
        beat_a.get("characters", []),
        beat_b.get("characters", []),
    )
    score += c
    if c:
        reasons.append(f"character_change(+{c})")

    # Emotional shift
    em = _emotion_score(
        beat_a.get("emotion", ""),
        beat_b.get("emotion", ""),
    )
    score += em
    if em:
        reasons.append(f"emotion_shift(+{em})")

    # Narrative arc adjacency
    bt = _beat_type_score(
        beat_a.get("narrative_arc", ""),
        beat_b.get("narrative_arc", ""),
    )
    score += bt
    if bt:
        reasons.append(f"beat_type(+{bt})")

    # === Look-cut special case (eyeline cut) ===
    # A motivated gaze carries the cut. Only escalate to transition_shot
    # when the score is very high (9+), meaning too many factors changed.
    if exit_type == "cut_on_look":
        if score <= 6:
            return {
                "type": "eyeline_cut",
                "score": score,
                "reason": f"Look exit + score {score} ({', '.join(reasons)}) — gaze carries the cut",
                "needs_transition_shot": False,
            }
        if score <= 8:
            return {
                "type": "match_cut",
                "score": score,
                "reason": f"Look exit + score {score} ({', '.join(reasons)}) — high change, match cut",
                "needs_transition_shot": False,
            }
        return {
            "type": "transition_shot",
            "score": score,
            "reason": f"Look exit + score {score} ({', '.join(reasons)}) — extreme change, bridge needed",
            "needs_transition_shot": True,
        }

    # === Standard score mapping ===
    reason_str = f"score={score} ({', '.join(reasons)})" if reasons else f"score={score}"

    if score <= 2:
        return {
            "type": "hard_cut",
            "score": score,
            "reason": reason_str,
            "needs_transition_shot": False,
        }
    if score <= 4:
        return {
            "type": "hard_cut_audio_bridge",
            "score": score,
            "reason": reason_str,
            "needs_transition_shot": False,
        }
    if score <= 6:
        return {
            "type": "match_cut",
            "score": score,
            "reason": reason_str,
            "needs_transition_shot": False,
        }
    if score <= 9:
        return {
            "type": "transition_shot",
            "score": score,
            "reason": reason_str,
            "needs_transition_shot": True,
        }
    return {
        "type": "fade_to_black_transition",
        "score": score,
        "reason": reason_str,
        "needs_transition_shot": True,
    }


def analyze_all_transitions(plan):
    """Run choose_transition() on every beat boundary in the plan.

    Returns list of {from_beat, to_beat, decision}.
    """
    beats = plan.get("beats", [])
    results = []
    for i in range(len(beats) - 1):
        decision = choose_transition(beats[i], beats[i + 1])
        results.append({
            "from_beat": beats[i]["beat_id"],
            "to_beat": beats[i + 1]["beat_id"],
            "decision": decision,
        })
    return results


def compile_transition_shot_payloads(plan, profile, packages):
    """Generate payloads for transition shots where the scoring system demands them.

    A transition shot is a 3s micro-shot (V3 Standard) that bridges two beats.
    Uses last frame of beat A's final clip as start, beat B's first anchor as mood ref.

    Returns list of {from_beat, to_beat, prompt, anchor_prompt, anchor_refs, duration, ...}
    """
    transitions = analyze_all_transitions(plan)
    style_bible = plan.get("style_bible", "")
    payloads = []

    for trans in transitions:
        if not trans["decision"]["needs_transition_shot"]:
            continue

        from_id = trans["from_beat"]
        to_id = trans["to_beat"]
        beat_a = None
        beat_b = None
        for b in plan.get("beats", []):
            if b["beat_id"] == from_id:
                beat_a = b
            if b["beat_id"] == to_id:
                beat_b = b

        if not beat_a or not beat_b:
            continue

        # Get the last shot of beat A for frame extraction
        shots_a = beat_a.get("shots", [])
        last_shot_a = shots_a[-1] if shots_a else None
        clip_a = last_shot_a.get("clip_path") if last_shot_a else None

        # Get the first shot of beat B for target anchor
        shots_b = beat_b.get("shots", [])
        first_shot_b = shots_b[0] if shots_b else None
        anchor_b = first_shot_b.get("anchor_path") if first_shot_b else None
        if not anchor_b:
            anchor_b = f"output/pipeline/anchors_v3/{first_shot_b['shot_id']}.png" if first_shot_b else None

        # Collect refs for the transition anchor image
        ref_pkgs = list(set(
            beat_a.get("characters", []) +
            beat_b.get("characters", []) +
            [beat_a.get("location_pkg", ""), beat_b.get("location_pkg", "")]
        ))
        ref_paths = []
        for pkg_id in ref_pkgs:
            if not pkg_id:
                continue
            path = _resolve_pkg_path(pkg_id, packages)
            if path and os.path.isfile(path):
                ref_paths.append(path)

        # Rule: context-specific transition prompts (energy, location, characters)
        prompts = vary_transition_prompt(beat_a, beat_b, packages, style_bible)
        anchor_prompt = prompts["anchor_prompt"]
        video_prompt = prompts["video_prompt"]

        payloads.append({
            "from_beat": from_id,
            "to_beat": to_id,
            "transition_id": f"trans_{from_id}_to_{to_id}",
            "decision": trans["decision"],
            "clip_a_path": clip_a,
            "anchor_b_path": anchor_b,
            "anchor_prompt": anchor_prompt[:2500],
            "anchor_refs": ref_paths,
            "anchor_output": f"output/pipeline/anchors_v3/trans_{from_id}_to_{to_id}.png",
            "video_prompt": video_prompt[:2500],
            "duration": 3,
            "tier": "v3_standard",
            "negative_prompt": _DEFAULT_NEGATIVE,
        })

    return payloads


# ---------------------------------------------------------------------------
# Stage 8c: Transition Intelligence
# ---------------------------------------------------------------------------

def compile_transition_intelligence(plan, packages=None):
    """Run full Transition Intelligence pipeline on a production plan.

    Scores every cut point with the 5-dimension judge, assigns strategies
    based on cost tiers, and returns the full intelligence report.

    This is the unified entry point — replaces manual choose_transition()
    calls for plans that want the full TI system.

    Returns:
        {
            "judge_results": [...],       # per-cut 5-dimension scores
            "strategies": [...],          # per-cut strategy assignments
            "summary": {
                "total_cuts": int,
                "risk_distribution": {low: n, medium: n, high: n, critical: n},
                "strategy_distribution": {strategy: n, ...},
                "average_composite": float,
                "weakest_transition": {...},
            },
        }
    """
    from lib.transition_judge import judge_all_transitions, DIMENSION_WEIGHTS
    from lib.transition_strategy import assign_all_strategies

    # Run judge on all shot pairs
    judge_results = judge_all_transitions(plan)

    # Assign strategies based on scores
    strategies = assign_all_strategies(plan, judge_results)

    # Build summary
    risk_dist = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    strat_dist = {}
    composites = []
    weakest = None

    for i, jr in enumerate(judge_results):
        risk_dist[jr["risk_level"]] = risk_dist.get(jr["risk_level"], 0) + 1
        composites.append(jr["composite"])
        strat_name = strategies[i]["strategy"]
        strat_dist[strat_name] = strat_dist.get(strat_name, 0) + 1

        if weakest is None or jr["composite"] < weakest["composite"]:
            weakest = jr

    avg_composite = round(sum(composites) / max(len(composites), 1), 1)

    return {
        "judge_results": judge_results,
        "strategies": strategies,
        "summary": {
            "total_cuts": len(judge_results),
            "risk_distribution": risk_dist,
            "strategy_distribution": strat_dist,
            "average_composite": avg_composite,
            "weakest_transition": weakest,
        },
    }


def compile_conform_from_ti(plan, ti_result):
    """Build conform payload using Transition Intelligence results.

    Uses TI strategies instead of the simpler choose_transition() logic.
    Bridge frames/clips are inserted where strategies demand them.

    Returns same format as compile_conform_payload() for compatibility
    with video_stitcher.stitch().
    """
    strategies = ti_result["strategies"]
    clips = []
    transitions = []

    all_shots = []
    for beat in plan.get("beats", []):
        for shot in beat.get("shots", []):
            all_shots.append((shot, beat))

    # Build clip list with transitions
    for si, (shot, beat) in enumerate(all_shots):
        clip_path = shot.get("clip_path")
        if not clip_path or not os.path.isfile(clip_path):
            continue

        # First clip: no transition into it
        if si == 0:
            clips.append({
                "beat_id": beat["beat_id"],
                "shot_id": shot["shot_id"],
                "path": clip_path,
            })
            continue

        # Get the strategy for this cut (index = si - 1 in strategies list)
        strat_idx = si - 1
        if strat_idx < len(strategies):
            strat = strategies[strat_idx]
            strategy_name = strat["strategy"]
            gen_params = strat.get("generation_params", {})

            if strategy_name == "bridge_frame":
                # Check if a bridge clip was generated
                bridge_clip = gen_params.get("bridge_clip_path", "")
                if bridge_clip and os.path.isfile(bridge_clip):
                    # Insert bridge clip with hard cuts around it
                    clips.append({
                        "beat_id": f"bridge_{strat_idx}",
                        "shot_id": f"bridge_{strat_idx}",
                        "path": bridge_clip,
                    })
                    transitions.append({
                        "type": "hard_cut",
                        "score": strat["judge"]["composite"],
                        "reason": f"Into bridge: {strat['reason']}",
                        "strategy": strategy_name,
                    })
                    # Then hard cut from bridge to next clip
                    clips.append({
                        "beat_id": beat["beat_id"],
                        "shot_id": shot["shot_id"],
                        "path": clip_path,
                    })
                    transitions.append({
                        "type": "hard_cut",
                        "score": strat["judge"]["composite"],
                        "reason": f"From bridge: {strat['reason']}",
                        "strategy": strategy_name,
                    })
                    continue

            # Map strategy to ffmpeg transition type
            ffmpeg_type = _TI_STRATEGY_TO_FFMPEG.get(strategy_name, "hard_cut")

            # Check for motivated cut subtypes
            if strategy_name == "motivated_cut":
                mot = strat.get("motivation", {})
                if mot and mot.get("type") == "eyeline":
                    ffmpeg_type = "hard_cut"  # eyeline = hard splice

            clips.append({
                "beat_id": beat["beat_id"],
                "shot_id": shot["shot_id"],
                "path": clip_path,
            })
            transitions.append({
                "type": ffmpeg_type,
                "score": strat["judge"]["composite"],
                "reason": strat["reason"],
                "strategy": strategy_name,
                "importance": strat.get("importance", "standard"),
            })
        else:
            clips.append({
                "beat_id": beat["beat_id"],
                "shot_id": shot["shot_id"],
                "path": clip_path,
            })
            transitions.append({
                "type": "hard_cut",
                "score": 0,
                "reason": "No strategy data — fallback hard cut",
            })

    # Final transition
    final_trans = plan.get("conform", {}).get("final_transition",
        {"type": "fade", "duration": 2.0})

    return {
        "clips": clips,
        "transitions": transitions,
        "final_transition": final_trans,
        "audio_path": (plan.get("audio") or {}).get("song_path"),
        "output_path": "output/pipeline/final/cinematic_v4.mp4",
    }


# Strategy → ffmpeg transition name mapping
_TI_STRATEGY_TO_FFMPEG = {
    "motivated_cut":   "hard_cut",
    "direct_animate":  "hard_cut",
    "end_variants":    "hard_cut",
    "bridge_frame":    "hard_cut",  # bridge clip is inserted, cuts around it
    "regenerate_pair": "hard_cut",
}


# ---------------------------------------------------------------------------
# Stage 9: Conform Payloads
# ---------------------------------------------------------------------------

def compile_conform_payload(plan):
    """Build editorial conform payload from completed clips.

    Returns {clips, transitions, audio_path, output_path, fade_out}.
    """
    clips = []
    transitions = []

    beats = plan.get("beats", [])
    for bi, beat in enumerate(beats):
        beat_id = beat["beat_id"]
        shots = beat.get("shots", [])

        # Check for multi-shot clip
        if beat.get("multi_shot_group") and len(shots) > 1:
            shot_ids = [s["shot_id"] for s in shots]
            # Look for multi-shot clip file
            clip_path = shots[0].get("clip_path")
            if clip_path and os.path.isfile(clip_path):
                clips.append({
                    "beat_id": beat_id,
                    "shot_ids": shot_ids,
                    "path": clip_path,
                })
        else:
            for shot in shots:
                clip_path = shot.get("clip_path")
                if clip_path and os.path.isfile(clip_path):
                    clips.append({
                        "beat_id": beat_id,
                        "shot_id": shot["shot_id"],
                        "path": clip_path,
                    })

        # Transition after this beat (except last)
        # Use scored transition decision, falling back to plan's transition_out
        if bi < len(beats) - 1:
            scored = choose_transition(beat, beats[bi + 1])
            trans_out = beat.get("transition_out", {})

            # Check for generated transition clip
            trans_id = f"trans_{beat_id}_to_{beats[bi + 1]['beat_id']}"
            trans_clip = f"output/pipeline/clips_v3/{trans_id}_v3standard.mp4"

            trans = {
                "type": scored["type"],
                "score": scored["score"],
                "reason": scored["reason"],
            }
            if scored["needs_transition_shot"] and os.path.isfile(trans_clip):
                trans["clip_path"] = trans_clip
            transitions.append(trans)

    # Final transition
    final_trans = plan.get("conform", {}).get("final_transition",
        {"type": "fade", "duration": 2.0})

    return {
        "clips": clips,
        "transitions": transitions,
        "final_transition": final_trans,
        "audio_path": (plan.get("audio") or {}).get("song_path"),
        "output_path": "output/pipeline/final/cinematic_v3.mp4",
    }


# ---------------------------------------------------------------------------
# Cost Estimation
# ---------------------------------------------------------------------------

def estimate_cost(plan, profile, tier="preview"):
    """Estimate total pipeline cost."""
    tier_key = profile.get("tiers", {}).get(tier, "video_engine")
    vid_profile = profile.get(tier_key, profile.get("video_engine", {}))
    img_profile = profile.get("image_engine", {})

    vid_cost_per_sec = vid_profile.get("cost_per_sec", 0.112)
    img_cost = img_profile.get("cost_per_image", 0.08)

    beats = plan.get("beats", [])
    total_shots = sum(len(b.get("shots", [])) for b in beats)
    total_duration = 0
    for beat in beats:
        for shot in beat.get("shots", []):
            total_duration += _clamp_duration(shot.get("duration", 5))

    # Count assets
    num_chars = len(plan.get("characters", []))
    num_locs = len(plan.get("locations", []))
    num_stills = len(beats)
    num_anchors = total_shots

    sheet_cost = (num_chars + num_locs) * img_cost
    still_cost = num_stills * img_cost
    anchor_cost = num_anchors * img_cost
    video_cost = total_duration * vid_cost_per_sec

    return {
        "tier": tier,
        "engine": vid_profile.get("id", "unknown"),
        "beats": len(beats),
        "shots": total_shots,
        "total_duration_sec": total_duration,
        "sheets": {"count": num_chars + num_locs, "cost": round(sheet_cost, 2)},
        "scene_stills": {"count": num_stills, "cost": round(still_cost, 2)},
        "anchors": {"count": num_anchors, "cost": round(anchor_cost, 2)},
        "video": {"duration": total_duration, "cost": round(video_cost, 2)},
        "total_cost": round(sheet_cost + still_cost + anchor_cost + video_cost, 2),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(base)

    plan = load_json("output/pipeline/production_plan_v3.json")
    profile = load_json("output/pipeline/model_profile.json")
    packages = load_json("output/preproduction/packages.json")

    print("=" * 60)
    print("CINEMATIC COMPILER — Summary")
    print("=" * 60)

    # Scene stills
    stills = compile_scene_still_payloads(plan, profile, packages)
    print(f"\nScene stills: {len(stills)}")
    for s in stills:
        print(f"  {s['beat_id']}: {len(s['reference_image_paths'])} refs")

    # Anchors
    anchors = compile_shot_anchor_payloads(plan, profile, packages)
    print(f"\nShot anchors: {len(anchors)}")
    for a in anchors:
        print(f"  {a['shot_id']}: {len(a['reference_image_paths'])} refs")

    # Videos
    for t in ["draft", "review", "final"]:
        vids = compile_video_payloads(plan, profile, packages, t)
        cost = estimate_cost(plan, profile, t)
        print(f"\n--- {t.upper()} ---")
        for v in vids:
            if v.get("is_multi_shot"):
                print(f"  Multi: {v['shot_ids']} {v['duration']}s")
            else:
                print(f"  {v['shot_id']}: {v['duration']}s")
        print(f"  Total: ${cost['total_cost']}")
