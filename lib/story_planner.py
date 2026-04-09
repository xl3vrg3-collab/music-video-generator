"""
Story Planner — Uses an LLM to create coherent storylines from creative direction,
replacing template-based scene planning with intelligent AI-driven storytelling.

Supports multiple providers: Claude (Anthropic), Grok (xAI), OpenAI.
Falls back gracefully to template planning if the API call fails.
"""

import json
import math
import os
import re


# ── V4 Shot-Based Planning ──

# Shot size → duration range (seconds)
SHOT_DURATION_TABLE = {
    "INSERT": (0.6, 1.8),
    "ECU":    (0.8, 2.2),
    "CU":     (1.2, 3.0),
    "MCU":    (1.8, 3.5),
    "MS":     (1.8, 4.0),
    "OTS":    (1.8, 3.5),
    "POV":    (1.2, 3.0),
    "WS":     (2.5, 5.5),
    "EWS":    (3.0, 7.0),
}

# Sequence type → ordered list of "SHOT_SIZE:movement" patterns
SEQUENCE_GRAMMAR = {
    "establish":  ["EWS:slow_push", "WS:static", "MS:tracking", "CU:push_in"],
    "pursuit":    ["WS:tracking", "MS:handheld", "POV:handheld", "CU:snap_zoom", "INSERT:shake"],
    "reveal":     ["CU:static", "MS:dolly_out", "EWS:crane_up"],
    "reunion":    ["WS:static", "OTS:push_in", "MCU:push_in", "CU:hold", "MS:pullback"],
    "tension":    ["WS:static", "MCU:static", "ECU:slow_push", "CU:static", "INSERT:detail"],
    "release":    ["MS:static", "WS:slow_dolly_out", "CU:hold"],
    "montage":    ["MS:tracking", "CU:static", "INSERT:detail", "WS:pan"],
    "dialogue":   ["WS:static", "MS:static", "OTS:static", "CU:push_in", "OTS:reverse"],
}

# Map section_type to a default sequence_type
_SECTION_TO_SEQUENCE = {
    "intro":       "establish",
    "verse":       "montage",
    "pre-chorus":  "tension",
    "chorus":      "pursuit",
    "bridge":      "reveal",
    "outro":       "release",
    "drop":        "pursuit",
    "buildup":     "tension",
    "breakdown":   "release",
}

# Shot size → lens/angle defaults (from coverage_system patterns)
_SHOT_SIZE_DEFAULTS = {
    "EWS":    {"lens_feel": "18mm Ultra Wide", "angle": "eye_level", "composition": "Leading Lines"},
    "WS":     {"lens_feel": "24mm Wide",       "angle": "eye_level", "composition": "Centered Symmetry"},
    "MS":     {"lens_feel": "35mm Natural",    "angle": "eye_level", "composition": "Rule of Thirds"},
    "MCU":    {"lens_feel": "50mm Standard",   "angle": "eye_level", "composition": "Rule of Thirds"},
    "CU":     {"lens_feel": "85mm Portrait",   "angle": "eye_level", "composition": "Rule of Thirds"},
    "ECU":    {"lens_feel": "Macro Lens",      "angle": "eye_level", "composition": "Centered Symmetry"},
    "OTS":    {"lens_feel": "50mm Standard",   "angle": "shoulder",  "composition": "Foreground Framing"},
    "POV":    {"lens_feel": "28mm Wide",       "angle": "eye_level", "composition": "Centered Symmetry"},
    "INSERT": {"lens_feel": "Macro Lens",      "angle": "high",      "composition": "Centered Symmetry"},
}

# Screen direction alternation patterns per sequence
_SEQUENCE_SCREEN_DIR = {
    "establish":  ["neutral", "neutral", "L2R", "neutral"],
    "pursuit":    ["L2R", "L2R", "L2R", "L2R", "neutral"],
    "reveal":     ["neutral", "neutral", "neutral"],
    "reunion":    ["L2R", "R2L", "L2R", "neutral", "neutral"],
    "tension":    ["neutral", "neutral", "neutral", "neutral", "neutral"],
    "release":    ["neutral", "neutral", "neutral"],
    "montage":    ["L2R", "neutral", "neutral", "R2L"],
    "dialogue":   ["neutral", "L2R", "R2L", "L2R", "R2L"],
}


def _compute_target_duration(shot_size, energy, section_type, is_first_in_section=False):
    """Compute target duration for a shot based on size, energy, and section pacing."""
    from lib.beat_sync import SECTION_PACING

    lo, hi = SHOT_DURATION_TABLE.get(shot_size, (2.0, 4.0))
    pacing = SECTION_PACING.get(section_type, SECTION_PACING.get("verse"))
    energy_mult = pacing.get("energy_mult", 0.7)

    # Steeper energy curve: power 0.6 spreads values more at extremes
    energy_factor = (energy * energy_mult) ** 0.6

    # Section-type scaling creates additional spread across sections
    _SECTION_SCALE = {
        "intro": 1.5, "outro": 1.4, "bridge": 1.25,
        "verse": 1.0, "pre-chorus": 0.85,
        "chorus": 0.6, "drop": 0.55, "buildup": 0.8, "breakdown": 1.3,
    }
    scale = _SECTION_SCALE.get(section_type, 1.0)

    # High energy → shorter (toward lo), low energy → longer (toward hi)
    t = lo + (hi - lo) * (1.0 - energy_factor) * scale

    # First shot of a new section gets +50% (held shot for transition)
    if is_first_in_section:
        t *= 1.5
        t = min(t, hi * 1.5)

    # Clamp within extended range (allow slight overshoot for variety)
    t = max(lo * 0.8, min(t, hi * 1.4))

    return round(t, 2)


def _snap_to_beat(target_dur, cumulative_time, audio_beats, tolerance=0.3):
    """Snap cut point to nearest audio beat. Bidirectional with wider fallback."""
    if not audio_beats:
        return target_dur

    cut_time = cumulative_time + target_dur

    # Find the single closest beat to the cut point
    best_snap = None
    best_dist = float('inf')
    for beat_time in audio_beats:
        dist = abs(cut_time - beat_time)
        if dist < best_dist:
            snapped_dur = beat_time - cumulative_time
            if snapped_dur >= 0.5:  # min shot duration
                best_dist = dist
                best_snap = snapped_dur

    if best_snap is None:
        return target_dur

    # Tight tolerance: snap directly
    if best_dist <= tolerance:
        return round(best_snap, 3)

    # Wider tolerance: snap if duration change is ≤25% of target
    if best_dist <= tolerance * 2.0:
        change_pct = abs(best_snap - target_dur) / target_dur if target_dur > 0 else 1
        if change_pct <= 0.25:
            return round(best_snap, 3)

    return target_dur


def expand_beat_to_shots(beat, sequence_type=None, energy=0.5, section_type="verse",
                         characters=None, environments=None, audio_beats=None,
                         cumulative_time=0.0, global_shot_index=0):
    """
    Expand a single narrative beat into 2-5 cinematically diverse shots.

    Args:
        beat: dict with story_beat, emotion, visual_prompt, character, environment, etc.
        sequence_type: one of SEQUENCE_GRAMMAR keys (auto-detected from section_type if None)
        energy: 0-1 energy level for this section
        section_type: audio section type (intro, verse, chorus, etc.)
        characters: list of character dicts for subject binding
        environments: list of environment dicts
        audio_beats: list of beat timestamps for snap-to-beat
        cumulative_time: running total of seconds for beat snapping
        global_shot_index: running shot counter across all beats

    Returns:
        list of shot dicts ready for video_generator.generate_scene()
    """
    characters = characters or []
    environments = environments or []

    # Resolve sequence type
    if not sequence_type:
        sequence_type = _SECTION_TO_SEQUENCE.get(section_type, "montage")
    if sequence_type not in SEQUENCE_GRAMMAR:
        sequence_type = "montage"

    grammar = SEQUENCE_GRAMMAR[sequence_type]
    screen_dirs = _SEQUENCE_SCREEN_DIR.get(sequence_type, ["neutral"] * len(grammar))

    beat_id = beat.get("beat_id", f"beat_{beat.get('scene_number', 0):02d}")
    story_beat = beat.get("story_beat", "")
    visual_prompt = beat.get("visual_prompt", "")
    emotion = beat.get("emotion", "")
    character_name = beat.get("character") or ""
    environment_name = beat.get("environment") or ""

    # Split the visual prompt into action fragments for individual shots
    action_fragments = _split_action(visual_prompt, len(grammar))

    shots = []
    running_time = cumulative_time

    for i, pattern in enumerate(grammar):
        parts = pattern.split(":", 1)
        shot_size = parts[0]
        movement = parts[1] if len(parts) > 1 else "static"
        is_first = (i == 0 and cumulative_time == 0)

        target_dur = _compute_target_duration(shot_size, energy, section_type, is_first)
        target_dur = _snap_to_beat(target_dur, running_time, audio_beats)
        runway_dur = max(2, math.ceil(target_dur + 0.5))
        runway_dur = min(runway_dur, 10)

        defaults = _SHOT_SIZE_DEFAULTS.get(shot_size, {})
        screen_dir = screen_dirs[i] if i < len(screen_dirs) else "neutral"

        # Shot importance tier (hero/support/bridge)
        is_hero = False
        importance = "support"
        if sequence_type == "reveal" and i == len(grammar) - 1:
            is_hero, importance = True, "hero"
        elif sequence_type == "reunion" and i == len(grammar) - 2:
            is_hero, importance = True, "hero"
        elif energy >= 0.85 and shot_size in ("CU", "ECU"):
            is_hero, importance = True, "hero"
        elif i == 0 and sequence_type in ("release", "montage"):
            importance = "bridge"
        elif shot_size == "INSERT":
            importance = "bridge"

        # Shot purpose — what this shot accomplishes narratively
        _PURPOSE_MAP = {
            "EWS": "establish_place", "WS": "establish_place",
            "MS": "show_action", "MCU": "show_emotion",
            "CU": "show_emotion", "ECU": "show_detail",
            "OTS": "show_relationship", "POV": "show_perspective",
            "INSERT": "show_detail",
        }
        shot_purpose = _PURPOSE_MAP.get(shot_size, "show_action")
        if is_hero:
            shot_purpose = "payoff_moment"
        elif importance == "bridge":
            shot_purpose = "transition"

        # Composition intent — context-aware composition template
        composition = defaults.get("composition", "Rule of Thirds")
        if is_hero and shot_size in ("CU", "MCU"):
            composition = "Golden Ratio"
        elif shot_size in ("EWS", "WS") and i == 0:
            composition = "Leading Lines"
        elif shot_size == "OTS":
            composition = "Foreground Framing"
        elif energy < 0.3 and shot_size in ("MS", "MCU", "CU"):
            composition = "Negative Space"
        elif sequence_type == "tension" and shot_size in ("MCU", "ECU"):
            composition = "Dutch Angle"

        shot_idx = global_shot_index + i
        shot = {
            # V4 identity
            "shot_id": f"b{beat.get('scene_number', 0):02d}_s{i:02d}",
            "beat_id": beat_id,
            "shot_index": i,

            # Shot specification
            "shot_size": shot_size,
            "angle": defaults.get("angle", "eye_level"),
            "lens_feel": defaults.get("lens_feel", "35mm Natural"),
            "movement": movement,
            "subject": character_name,
            "action": action_fragments[i] if i < len(action_fragments) else story_beat,
            "emotion": emotion,
            "screen_direction": screen_dir,
            "sequence_type": sequence_type,
            "is_hero": is_hero,
            "importance": importance,
            "shot_purpose": shot_purpose,
            "composition_intent": composition,

            # Timing
            "target_duration": target_dur,
            "runway_duration": runway_dur,
            "handle_frames": 0.5,
            "trim_in": 0.0,
            "trim_out": target_dur,

            # Continuity
            "continuity_anchors": [],
            "must_keep": [],
            "avoid": [],
            "prompt_short": action_fragments[i][:120] if i < len(action_fragments) else story_beat[:120],

            # Lock strengths (defaults, overridden by UI)
            "character_lock_strength": 0.8,
            "costume_lock_strength": 0.8,
            "environment_lock_strength": 0.7,
            "prop_lock_strength": 0.6,
            "style_lock_strength": 0.9,
            "seed_lock": None,

            # Preproduction package bindings (populated by preproduction pipeline)
            "character_package_id": None,
            "costume_package_id": None,
            "environment_package_id": None,
            "prop_package_ids": [],

            # V5 anchor composition (populated by scene compositor)
            "anchor_image_path": None,       # path to composed anchor image
            "anchor_status": "pending",      # pending/generated/approved/rejected
            "anchor_source_refs": [],        # which canonical assets composed it
            "shot_family": None,             # set by classify_shot_family()

            # Multi-take
            "takes": [],

            # Existing scene-compatible fields
            "id": f"shot_{shot_idx:03d}",
            "index": shot_idx,
            "prompt": "",  # assembled later by prompt_assembler
            "duration": runway_dur,
            "camera_movement": movement,
            "engine": beat.get("engine", "gen4_5"),
            "characterId": beat.get("characterId", ""),
            "characterName": character_name,
            "environmentId": beat.get("environmentId", ""),
            "environmentName": environment_name,
            "character_photo_path": beat.get("character_photo_path", ""),
            "environment_photo_path": beat.get("environment_photo_path", ""),
            "section_type": section_type,
            "clip_path": None,
            "trimmed_clip_path": None,
            "has_clip": False,
            "status": "planned",
            "error": None,
            "first_frame_path": "",
            "continuity_context": {},
            "gen_hash": None,
            "lighting": beat.get("lighting", ""),
            "color_grade": beat.get("color_grade", ""),
        }
        shots.append(shot)
        running_time += target_dur

    return shots


def _split_action(visual_prompt, n_shots):
    """Split a visual prompt into n action fragments, one per shot."""
    if not visual_prompt:
        return [""] * n_shots

    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+', visual_prompt.strip())
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) >= n_shots:
        # Distribute sentences across shots
        per = max(1, len(sentences) // n_shots)
        fragments = []
        for i in range(n_shots):
            start = i * per
            end = start + per if i < n_shots - 1 else len(sentences)
            fragments.append(" ".join(sentences[start:end]))
        return fragments
    else:
        # Fewer sentences than shots — reuse and shorten
        fragments = list(sentences)
        while len(fragments) < n_shots:
            fragments.append(sentences[-1] if sentences else "")
        return fragments


# ── Available models by provider ──
AVAILABLE_MODELS = {
    "claude-sonnet-4-6":  {"provider": "anthropic", "label": "Claude Sonnet 4.6",  "tier": "premium"},
    "claude-haiku-4-5":   {"provider": "anthropic", "label": "Claude Haiku 4.5",   "tier": "standard"},
    "grok-3":             {"provider": "xai",       "label": "Grok 3",             "tier": "premium"},
    "grok-3-mini":        {"provider": "xai",       "label": "Grok 3 Mini",        "tier": "budget"},
    "gpt-4o":             {"provider": "openai",    "label": "GPT-4o",             "tier": "premium"},
    "gpt-4o-mini":        {"provider": "openai",    "label": "GPT-4o Mini",        "tier": "budget"},
}

DEFAULT_MODEL = "claude-sonnet-4-6"

# Provider API endpoints
_PROVIDER_URLS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "xai":       "https://api.x.ai/v1/chat/completions",
    "openai":    "https://api.openai.com/v1/chat/completions",
}

# Provider API key env vars
_PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "xai":       "XAI_API_KEY",
    "openai":    "OPENAI_API_KEY",
}


# System prompt for the LLM
STORY_DIRECTOR_SYSTEM_PROMPT = """\
You are an award-winning film director and screenwriter. You think in shots, \
not paragraphs. Every scene you write is a precise visual blueprint that an AI \
video generator can execute.

Given a creative concept (and optionally lyrics/audio structure), create a \
scene-by-scene visual storyline.

RULES:
- Build a COHERENT NARRATIVE ARC: setup → rising action → climax → resolution
- Each scene MUST connect to the previous one — same characters, same world, cause and effect
- Write each visual_prompt as a DETAILED cinematographic description (3-4 sentences minimum):
  * What the camera SEES (subject, action, environment details)
  * How the camera MOVES (dolly, crane, tracking, static, handheld)
  * LIGHTING and COLOR (warm golden, cold blue, high contrast noir, etc.)
  * MOOD and ATMOSPHERE (tension, wonder, melancholy, triumph)
- Use the PROVIDED characters by their physical descriptions, NOT just names
- Keep the SAME character appearance across ALL scenes (describe them consistently)
- Vary shot types across scenes: wide establishing → medium → close-up → wide, etc.
- Do NOT include text overlays, titles, or lyrics in the visual description
- Focus on what the VIEWER SEES, not what they hear
- Camera field must be one of: zoom_in, zoom_out, pan_left, pan_right, orbit, tracking, static, crane_up, crane_down, dolly_in, dolly_out, handheld

Output valid JSON only."""


def _format_time(seconds: float) -> str:
    """Format seconds as m:ss."""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


class StoryPlanner:
    """Uses an LLM to create coherent storylines. Supports Claude, Grok, and OpenAI."""

    def __init__(self, api_key: str = None, model: str = None):
        self.model = model or os.environ.get("LUMN_STORY_MODEL", DEFAULT_MODEL)
        model_info = AVAILABLE_MODELS.get(self.model, {})
        self.provider = model_info.get("provider", "anthropic")

        # Resolve API key: explicit > env for specific provider
        if api_key:
            self.api_key = api_key
        else:
            key_env = _PROVIDER_KEYS.get(self.provider, "")
            self.api_key = os.environ.get(key_env, "") if key_env else ""

    def plan_story(self, lyrics: str, creative_direction: str,
                   num_scenes: int, characters: list, environments: list,
                   section_info: list) -> list:
        """
        Takes a creative concept + optional lyrics and generates scene-by-scene prompts.

        Returns:
            list of {scene_number, section_type, story_beat, emotion,
                     visual_prompt, character, environment, camera}
        """
        if not self.api_key:
            key_env = _PROVIDER_KEYS.get(self.provider, "???")
            raise ValueError(f"{key_env} not set — cannot use AI story planner with {self.model}")

        user_prompt = self._build_user_prompt(
            lyrics, creative_direction, num_scenes, characters, environments, section_info
        )

        print(f"[STORY PLANNER] Using {self.model} ({self.provider}) for story planning")
        raw_response = self._call_llm(user_prompt)
        scenes = self._parse_response(raw_response, num_scenes, section_info)
        return scenes

    def _build_user_prompt(self, lyrics: str, creative_direction: str,
                            num_scenes: int, characters: list,
                            environments: list, section_info: list) -> str:
        """Build the user prompt with all context for the LLM."""
        parts = []

        parts.append(f"CREATIVE DIRECTION: {creative_direction}")
        parts.append("")

        # Lyrics
        parts.append("LYRICS:")
        if lyrics and lyrics.strip():
            parts.append(lyrics.strip())
        else:
            parts.append("(no lyrics provided — create a visual-only narrative)")
        parts.append("")

        # Song structure from audio analysis
        parts.append("SONG STRUCTURE:")
        for i, section in enumerate(section_info[:num_scenes]):
            start = _format_time(section.get("start", 0))
            end = _format_time(section.get("end", 0))
            stype = section.get("type", "verse")
            energy_val = section.get("energy", 0.5)
            if energy_val < 0.35:
                energy_label = "low energy"
            elif energy_val < 0.65:
                energy_label = "medium energy"
            else:
                energy_label = "high energy"
            parts.append(f"Scene {i + 1}: {start}-{end} ({stype}, {energy_label})")
        parts.append("")

        # Characters
        parts.append("AVAILABLE CHARACTERS:")
        if characters:
            for char in characters:
                name = char.get("name", "Unknown")
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
                has_photo = "(has reference photo — use physical description, NOT names)" if char.get("referencePhoto") else ""
                desc_str = ", ".join(desc_parts) if desc_parts else "no description"
                parts.append(f"- {name}: {desc_str} {has_photo}".strip())
        else:
            parts.append("- (none provided — create scenes without specific characters)")
        parts.append("")

        # Environments
        parts.append("AVAILABLE ENVIRONMENTS:")
        if environments:
            for env in environments:
                name = env.get("name", "Unknown")
                desc_parts = []
                desc = env.get("description", "")
                if desc:
                    desc_parts.append(desc)
                lighting = env.get("lighting", "")
                if lighting:
                    desc_parts.append(lighting)
                atmosphere = env.get("atmosphere", "")
                if atmosphere:
                    desc_parts.append(atmosphere)
                desc_str = ", ".join(desc_parts) if desc_parts else "no description"
                parts.append(f"- {name}: {desc_str}")
        else:
            parts.append("- (none provided — invent environments matching the creative direction)")
        parts.append("")

        # Final instruction
        parts.append(f"""Generate exactly {num_scenes} scene descriptions. Each scene should be a \
detailed visual prompt for AI video generation. Return as JSON:
{{
    "scenes": [
        {{
            "scene_number": 1,
            "section_type": "intro",
            "lyrics_at_this_point": "first two lines of lyrics here or empty string",
            "story_beat": "what happens narratively",
            "emotion": "mysterious, anticipation",
            "visual_prompt": "DETAILED prompt for video generation - describe what the viewer sees, camera angles, lighting, colors, movement, environment details - at least 2-3 sentences",
            "character": "CharacterName" or null,
            "environment": "EnvironmentName" or null,
            "camera": "slow pan right"
        }}
    ]
}}""")

        return "\n".join(parts)

    def _call_llm(self, user_prompt: str) -> str:
        """Call the configured LLM provider and return the raw response content."""
        import requests

        if self.provider == "anthropic":
            return self._call_anthropic(user_prompt)
        else:
            return self._call_openai_compatible(user_prompt)

    def _call_anthropic(self, user_prompt: str) -> str:
        """Call Claude via the Anthropic Messages API."""
        import requests

        resp = requests.post(
            _PROVIDER_URLS["anthropic"],
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "system": STORY_DIRECTOR_SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.7,
            },
            timeout=90,
        )

        if resp.status_code != 200:
            error_detail = ""
            try:
                error_detail = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                error_detail = resp.text[:200]
            raise RuntimeError(f"Anthropic API error {resp.status_code}: {error_detail}")

        data = resp.json()
        content_blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        if not text:
            raise RuntimeError("Empty response from Anthropic API")
        return text

    def _call_openai_compatible(self, user_prompt: str) -> str:
        """Call Grok (xAI) or OpenAI via the OpenAI-compatible chat completions API."""
        import requests

        url = _PROVIDER_URLS.get(self.provider, _PROVIDER_URLS["openai"])

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": STORY_DIRECTOR_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 4096,
                "temperature": 0.7,
            },
            timeout=90,
        )

        if resp.status_code != 200:
            error_detail = ""
            try:
                error_detail = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                error_detail = resp.text[:200]
            raise RuntimeError(f"{self.provider} API error {resp.status_code}: {error_detail}")

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise RuntimeError(f"Empty response from {self.provider} API")
        return content

    def _parse_response(self, raw: str, expected_count: int, section_info: list) -> list:
        """Parse the JSON response from the LLM into our scene format."""
        # Try to parse as JSON
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', raw)
            if json_match:
                parsed = json.loads(json_match.group(1))
            else:
                # Last resort: find the first { to last }
                start = raw.find('{')
                end = raw.rfind('}')
                if start >= 0 and end > start:
                    parsed = json.loads(raw[start:end + 1])
                else:
                    raise ValueError(f"Could not parse LLM response as JSON: {raw[:200]}")

        # Extract scenes array
        scenes_raw = parsed.get("scenes", [])
        if not scenes_raw and isinstance(parsed, list):
            scenes_raw = parsed

        if not scenes_raw:
            raise ValueError("LLM response contained no scenes")

        # Normalize and validate each scene
        scenes = []
        for i, s in enumerate(scenes_raw[:expected_count]):
            scene = {
                "scene_number": s.get("scene_number", i + 1),
                "section_type": s.get("section_type", section_info[i]["type"] if i < len(section_info) else "verse"),
                "lyrics_at_this_point": s.get("lyrics_at_this_point", ""),
                "story_beat": s.get("story_beat", ""),
                "emotion": s.get("emotion", ""),
                "visual_prompt": s.get("visual_prompt", s.get("prompt", "")),
                "character": s.get("character"),
                "environment": s.get("environment"),
                "camera": s.get("camera", "static"),
            }
            scenes.append(scene)

        return scenes
