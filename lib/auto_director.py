"""
Auto Director — One-click full music video generation.

Plans an entire music video by analyzing audio, auto-assigning characters
and environments to sections, building prompts, and executing batch generation.
"""

import hashlib
import json
import os
import re
import random
import time
import threading

from lib.audio_analyzer import analyze
from lib.video_generator import describe_photo as _describe_photo
from lib.scene_planner import (
    plan_scenes, auto_assign_transition, coherence_pass,
    SECTION_MOODS, ENERGY_DESCRIPTORS, QUALITY_SUFFIX,
    SECTION_DURATION_RANGES, _pick_natural_duration,
)
from lib.video_generator import (
    generate_scene, MODEL_DURATION_OPTIONS, get_valid_duration, get_smart_duration,
)
from lib.video_stitcher import stitch
from lib.prompt_os import PromptOS
from lib.story_planner import StoryPlanner

# ---- Workflow Presets ----

WORKFLOW_PRESETS = {
    "music_video": {
        "id": "music_video",
        "name": "Music Video",
        "description": "22 scenes, mixed durations, character-focused. Chorus = performance, Verse = narrative, Bridge = abstract.",
        "settings": {
            "natural_pacing": True,
            "scene_count_target": 22,
            "chorus_style": "performance shots, dynamic energy, fast cuts",
            "verse_style": "narrative storytelling, medium shots, emotional",
            "bridge_style": "abstract, dreamy, surreal slow-motion",
            "intro_style": "wide establishing shot, cinematic opening",
            "outro_style": "pulling back, fading, closing shot",
            "transition_style": "mixed",
            "character_density": 0.9,
        },
    },
    "lyric_video": {
        "id": "lyric_video",
        "name": "Lyric Video",
        "description": "Text overlays on every scene. Abstract/ambient visuals. Slower pacing.",
        "settings": {
            "natural_pacing": True,
            "scene_count_target": 16,
            "chorus_style": "abstract flowing colors, text-friendly backgrounds",
            "verse_style": "ambient textures, soft gradients, minimal movement",
            "bridge_style": "ethereal, particles, glowing text backgrounds",
            "intro_style": "dark minimal, title card ready",
            "outro_style": "fading to black, credits ready",
            "transition_style": "dissolve",
            "character_density": 0.2,
            "text_overlay": True,
        },
    },
    "cinematic_short": {
        "id": "cinematic_short",
        "name": "Cinematic Short",
        "description": "Longer scenes (8-10s), film grain + vignette, slower transitions (dissolve, fade_black).",
        "settings": {
            "natural_pacing": True,
            "scene_count_target": 12,
            "chorus_style": "dramatic sweeping cinematography, epic scale",
            "verse_style": "intimate close-ups, film noir lighting",
            "bridge_style": "contemplative wide shots, golden hour",
            "intro_style": "slow aerial establishing shot, epic landscape",
            "outro_style": "distant silhouette, slow fade to black",
            "transition_style": "slow",
            "character_density": 0.7,
            "preferred_duration": 10,
            "effects": ["film_grain", "vignette"],
        },
    },
    "performance_video": {
        "id": "performance_video",
        "name": "Performance Video",
        "description": "Character in every scene. Same environment throughout. Fast cuts on chorus, slow on verse.",
        "settings": {
            "natural_pacing": True,
            "scene_count_target": 20,
            "chorus_style": "performance, stage presence, dynamic angles, fast cuts",
            "verse_style": "intimate performance, close-up, emotional delivery",
            "bridge_style": "solo performance, spotlight, dramatic lighting",
            "intro_style": "approaching the stage, anticipation",
            "outro_style": "final pose, lights fading, applause",
            "transition_style": "fast",
            "character_density": 1.0,
            "same_environment": True,
        },
    },
}

# Camera movements by section type
SECTION_CAMERAS = {
    "intro": ["zoom_out", "pan_right", "tracking"],
    "verse": ["pan_left", "pan_right", "static", "zoom_in"],
    "chorus": ["orbit", "zoom_in", "tracking", "pan_left"],
    "bridge": ["zoom_out", "static", "orbit"],
    "outro": ["zoom_out", "pan_right", "static"],
}

# Energy words for prompt building
ENERGY_WORDS = {
    "low": "calm, subdued, gentle movement, soft lighting",
    "mid": "moderate energy, balanced motion, natural lighting",
    "high": "intense energy, vibrant, fast motion, dramatic lighting, pulsing",
}

# Environment energy scores for smart assignment
ENV_ENERGY_KEYWORDS = {
    "high": ["stage", "concert", "club", "neon", "city", "rooftop", "fire", "storm", "arena"],
    "mid": ["street", "park", "beach", "office", "room", "garden", "market"],
    "low": ["forest", "lake", "mountain", "bedroom", "church", "library", "field", "space"],
    "abstract": ["void", "dream", "abstract", "surreal", "underwater", "clouds", "cosmos"],
}


def _score_env_energy(env: dict) -> float:
    """Score an environment's energy level (0.0-1.0) based on description keywords."""
    desc = (env.get("description", "") + " " + env.get("atmosphere", "") + " " + env.get("name", "")).lower()
    for kw in ENV_ENERGY_KEYWORDS["high"]:
        if kw in desc:
            return 0.9
    for kw in ENV_ENERGY_KEYWORDS["mid"]:
        if kw in desc:
            return 0.5
    for kw in ENV_ENERGY_KEYWORDS["low"]:
        if kw in desc:
            return 0.2
    for kw in ENV_ENERGY_KEYWORDS["abstract"]:
        if kw in desc:
            return 0.4
    return 0.5


def _get_energy_words(energy: float) -> str:
    """Get energy descriptor words based on energy level."""
    if energy < 0.35:
        return ENERGY_WORDS["low"]
    elif energy < 0.65:
        return ENERGY_WORDS["mid"]
    else:
        return ENERGY_WORDS["high"]


_STYLE_KEYWORDS = {
    "cinematic", "dramatic", "moody", "warm", "cool", "neon", "noir",
    "golden hour", "shallow depth of field", "anamorphic", "film grain",
    "high contrast", "low key", "high key", "desaturated", "vibrant",
    "ethereal", "gritty", "dreamlike", "retro", "vintage", "futuristic",
    "minimalist", "surreal", "documentary", "editorial", "4k", "8k",
    "soft lighting", "harsh lighting", "backlit", "silhouette",
    "slow motion", "timelapse", "handheld", "steadicam",
    "emotional", "intimate", "epic", "melancholic", "joyful",
    "teal and orange", "pastel", "monochrome", "color grading",
    "professional color grading", "detailed", "photorealistic",
}


def _extract_style_from_direction(creative_direction: str) -> str:
    """Extract visual style keywords from a creative direction string.

    Keeps only style-related phrases so the full narrative concept
    isn't repeated on every shot prompt (the AI actions carry the story).
    """
    if not creative_direction:
        return DEFAULT_GLOBAL_STYLE
    text_lower = creative_direction.lower()
    found = []
    # Check for multi-word style phrases first
    for kw in sorted(_STYLE_KEYWORDS, key=len, reverse=True):
        if kw in text_lower:
            found.append(kw)
    if found:
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for f in found:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        result = ", ".join(unique[:8])  # Cap at 8 style keywords
        return result
    # Fallback: take the last clause (often has style modifiers)
    parts = creative_direction.split(".")
    for p in reversed(parts):
        stripped = p.strip()
        if stripped and len(stripped) < 120:
            return stripped
    return creative_direction[:100]


class AutoDirector:
    """Plans and executes full music video generation."""

    def __init__(self, output_dir: str, clips_dir: str, prompt_os: PromptOS = None):
        self.output_dir = output_dir
        self.clips_dir = clips_dir
        self.pos = prompt_os or PromptOS()
        self._progress = {
            "phase": "idle",
            "total_scenes": 0,
            "completed_scenes": 0,
            "failed_scenes": 0,
            "current_scene": None,
            "scenes": [],
            "error": None,
            "output_file": None,
            "plan": None,
        }
        self._lock = threading.Lock()

    @property
    def progress(self):
        with self._lock:
            return dict(self._progress)

    def _update_progress(self, **kwargs):
        with self._lock:
            self._progress.update(kwargs)

    # ---- Photo Resolution ----

    @staticmethod
    def _resolve_reference_photo(ref_photo: str, kind: str = "characters") -> str | None:
        """Resolve a referencePhoto value to an actual file path.

        Handles two cases:
        1. ref_photo is already a valid file path on disk.
        2. ref_photo is an API URL like ``/api/pos/characters/{id}/photo``
           or ``/api/pos/environments/{id}/photo`` — extract the id and
           build the real path under ``output/prompt_os/photos/{kind}/{id}.jpg``.

        Args:
            ref_photo: The referencePhoto value from the entity dict.
            kind: "characters" or "environments".

        Returns:
            Absolute path to the photo file if it exists, else None.
        """
        if not ref_photo:
            return None

        # Case 1: already a real file path
        if os.path.isfile(ref_photo):
            return ref_photo

        # Case 2: API URL — extract the entity id
        m = re.search(r"/api/pos/(?:characters|environments)/([^/]+)/photo", ref_photo)
        if m:
            entity_id = m.group(1)
            photos_base = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "output", "prompt_os", "photos",
            )
            candidate = os.path.join(photos_base, kind, f"{entity_id}.jpg")
            if os.path.isfile(candidate):
                return candidate

        return None

    # ---- Character Assignment ----

    def enrich_characters(self, characters: list) -> list:
        """Auto-describe characters from their reference photos when descriptions are empty.

        Uses Grok vision to describe the photo and fills in the description field.
        This ensures prompts contain detailed character appearance info even when
        the user only uploaded a photo without typing a description.
        """
        for char in characters:
            has_desc = (char.get("description") or char.get("physicalDescription") or "").strip()
            if has_desc:
                continue
            # Try to get photo path
            photo_path = self._resolve_reference_photo(
                char.get("referencePhoto", ""), "characters"
            )
            if not photo_path:
                continue
            try:
                desc = _describe_photo(photo_path)
                # Store it back so prompt builder can use it
                char["description"] = desc
                print(f"[AUTO DIRECTOR] Auto-described character '{char.get('name')}': {desc[:80]}...")
            except Exception as e:
                print(f"[AUTO DIRECTOR] Could not auto-describe '{char.get('name')}': {e}")
        return characters

    def enrich_environments(self, environments: list) -> list:
        """Auto-describe environments from their reference photos when descriptions are empty."""
        for env in environments:
            has_desc = (env.get("description") or "").strip()
            if has_desc:
                continue
            photo_path = self._resolve_reference_photo(
                env.get("referencePhoto", ""), "environments"
            )
            if not photo_path:
                continue
            try:
                desc = _describe_photo(photo_path)
                env["description"] = desc
                print(f"[AUTO DIRECTOR] Auto-described environment '{env.get('name')}': {desc[:80]}...")
            except Exception as e:
                print(f"[AUTO DIRECTOR] Could not auto-describe '{env.get('name')}': {e}")
        return environments

    def assign_characters_to_sections(self, characters: list, sections: list) -> list:
        """
        Assign characters to sections smartly.

        - 1 character: appears in every scene
        - 2+ characters: protagonist (first) in 70% of scenes, rotate others in 30%
        - Bridge: main character alone or no character (abstract)

        Returns list of character assignments parallel to sections.
        """
        if not characters:
            return [None] * len(sections)

        if len(characters) == 1:
            return [characters[0]] * len(sections)

        main_char = characters[0]
        featured = characters[1:]
        assignments = []
        featured_idx = 0

        for section in sections:
            stype = section.get("type", "verse")

            if stype == "bridge":
                # Bridge: 50% chance main char alone, 50% no character
                if random.random() < 0.5:
                    assignments.append(main_char)
                else:
                    assignments.append(None)
            elif stype in ("intro", "outro"):
                # Intro/outro: always main character
                assignments.append(main_char)
            elif random.random() < 0.7:
                # 70% main character
                assignments.append(main_char)
            else:
                # 30% rotate featured characters
                assignments.append(featured[featured_idx % len(featured)])
                featured_idx += 1

        return assignments

    # ---- Environment Assignment ----

    def assign_environments_to_sections(self, environments: list, sections: list) -> list:
        """
        Assign environments to sections smartly.

        - Never use same environment in adjacent scenes
        - Chorus = highest energy environment
        - Verse = story/intimate environment
        - Bridge = contrasting environment
        - Intro/Outro = most establishing environment

        Returns list of environment assignments parallel to sections.
        """
        if not environments:
            return [None] * len(sections)

        if len(environments) == 1:
            return [environments[0]] * len(sections)

        # Score environments by energy
        scored = [(env, _score_env_energy(env)) for env in environments]
        scored.sort(key=lambda x: x[1], reverse=True)

        high_energy_envs = [e for e, s in scored if s >= 0.7]
        mid_energy_envs = [e for e, s in scored if 0.3 <= s < 0.7]
        low_energy_envs = [e for e, s in scored if s < 0.3]

        # Ensure we have something in each bucket
        if not high_energy_envs:
            high_energy_envs = [scored[0][0]]
        if not mid_energy_envs:
            mid_energy_envs = [scored[len(scored) // 2][0]]
        if not low_energy_envs:
            low_energy_envs = [scored[-1][0]]

        assignments = []
        prev_env_id = None

        for i, section in enumerate(sections):
            stype = section.get("type", "verse")

            if stype == "chorus":
                pool = high_energy_envs
            elif stype == "verse":
                pool = mid_energy_envs
            elif stype == "bridge":
                # Contrasting: if previous was high energy, pick low energy and vice versa
                if prev_env_id:
                    prev_score = 0.5
                    for env, score in scored:
                        if env.get("id") == prev_env_id:
                            prev_score = score
                            break
                    pool = low_energy_envs if prev_score > 0.5 else high_energy_envs
                else:
                    pool = low_energy_envs
            elif stype in ("intro", "outro"):
                # Establishing: pick widest/most cinematic (use first by default)
                pool = [scored[0][0]]
            else:
                pool = mid_energy_envs

            # Avoid same environment as previous scene
            candidates = [e for e in pool if e.get("id") != prev_env_id]
            if not candidates:
                candidates = [e for e in environments if e.get("id") != prev_env_id]
            if not candidates:
                candidates = pool  # fallback if only 1 environment

            chosen = random.choice(candidates)
            assignments.append(chosen)
            prev_env_id = chosen.get("id")

        return assignments

    # ---- Costume Assignment ----

    def assign_costumes(self, characters: list, char_assignments: list) -> list:
        """Look up default costume for each character assignment."""
        costumes = []
        for char in char_assignments:
            if char is None:
                costumes.append(None)
                continue
            char_costumes = self.pos.get_costumes(char.get("id"))
            if char_costumes:
                costumes.append(char_costumes[0])  # use first costume
            else:
                costumes.append(None)
        return costumes

    # ---- Prompt Building ----

    def build_section_prompt(self, character=None, costume=None, environment=None,
                              style="", energy=0.5, section_type="verse",
                              preset_settings=None, story_beat="") -> str:
        """
        Build a cohesive prompt combining story beat + entity descriptions.

        Story beat leads the prompt (what's happening narratively),
        then character/costume/environment details, then style/energy.
        """
        parts = []

        # STORY BEAT first — this drives the narrative
        if story_beat:
            parts.append(story_beat)

        # Section-specific camera/mood from scene planner
        moods = SECTION_MOODS.get(section_type, SECTION_MOODS["verse"])
        parts.append(random.choice(moods))

        # Character description — rely on reference photo for likeness, text for details
        if character:
            char_desc = character.get("description", character.get("physicalDescription", ""))
            if not char_desc:
                char_desc = character.get("outfitDescription", "")
            if char_desc:
                parts.append(f"the person from the reference image, {char_desc}")
            else:
                parts.append("the person from the reference image")
            hair = character.get("hair", "")
            if hair:
                parts.append(f"with {hair}")
            skin = character.get("skinTone", "")
            if skin:
                parts.append(f"{skin} skin")
            features = character.get("distinguishingFeatures", "")
            if features:
                parts.append(features)

        # Costume
        if costume:
            costume_desc = costume.get("description", "")
            if costume_desc:
                parts.append(f"wearing {costume_desc}")
            else:
                upper = costume.get("upperBody", "")
                lower = costume.get("lowerBody", "")
                if upper:
                    parts.append(f"wearing {upper}")
                if lower:
                    parts.append(lower)

        # Environment
        if environment:
            env_desc = environment.get("description", "")
            if env_desc:
                parts.append(f"in {env_desc}")
            lighting = environment.get("lighting", "")
            if lighting:
                parts.append(lighting)
            atmosphere = environment.get("atmosphere", "")
            if atmosphere:
                parts.append(atmosphere)

        # User style
        if style:
            parts.append(style)

        # Preset overrides for section type
        if preset_settings:
            section_key = f"{section_type}_style"
            section_style = preset_settings.get(section_key, "")
            if section_style:
                parts.append(section_style)

        # Energy words
        parts.append(_get_energy_words(energy))

        # Quality suffix
        parts.append(QUALITY_SUFFIX)

        return ", ".join(p for p in parts if p)

    # ---- Cost Estimation ----

    def estimate_cost(self, num_scenes: int, engine: str = "gen4_5") -> dict:
        """Estimate generation cost and time."""
        # Cost per clip varies by engine
        costs = {
            "gen4_5": 0.50, "gen3a_turbo": 0.25, "kling_pro": 0.30,
            "kling_standard": 0.15, "veo3": 0.50, "veo3_1": 0.50,
            "veo3_1_fast": 0.25, "grok": 0.10, "luma": 0.20, "openai": 0.15,
        }
        cost_per = costs.get(engine, 0.15)
        total_cost = round(num_scenes * cost_per, 2)

        # Time per clip (seconds)
        times = {
            "gen4_5": 90, "gen3a_turbo": 45, "kling_pro": 120,
            "kling_standard": 60, "veo3": 120, "veo3_1": 120,
            "veo3_1_fast": 45, "grok": 30, "luma": 45, "openai": 50,
        }
        time_per = times.get(engine, 60)
        # With 2 concurrent workers
        total_time_min = round((num_scenes * time_per / 2) / 60, 1)

        return {
            "num_scenes": num_scenes,
            "cost_per_scene": cost_per,
            "total_cost": total_cost,
            "time_per_scene_sec": time_per,
            "total_time_min": total_time_min,
            "engine": engine,
        }

    # ---- AI Story Planning ----

    def plan_with_ai(self, song_path: str, creative_direction: str,
                      lyrics: str = "", characters=None, environments=None,
                      engine="grok", natural_pacing=True, preset_id=None,
                      budget=None, story_model=None) -> dict:
        """
        Plan a full music video using LLM-driven story planning.

        1. Analyzes audio to get sections/beats/energy
        2. Calls StoryPlanner to get AI-generated scene prompts from lyrics + direction
        3. Maps AI suggestions to character/environment IDs
        4. Falls back to template planning if AI fails
        5. Returns the same plan format as plan_full_video()
        """
        characters = characters or []
        environments = environments or []

        # Auto-describe characters/environments from photos if descriptions empty
        characters = self.enrich_characters(characters)
        environments = self.enrich_environments(environments)

        # Load preset settings
        preset_settings = None
        if preset_id and preset_id in WORKFLOW_PRESETS:
            preset_settings = WORKFLOW_PRESETS[preset_id]["settings"]

        # 1. Analyze audio
        analysis = analyze(song_path)
        sections = analysis.get("sections", [])
        beats = analysis.get("beats", [])
        bpm = analysis.get("bpm", 120)
        duration = analysis.get("duration", 180)

        if not sections:
            sections = [
                {"start": 0, "end": duration * 0.1, "type": "intro", "energy": 0.3},
                {"start": duration * 0.1, "end": duration * 0.4, "type": "verse", "energy": 0.5},
                {"start": duration * 0.4, "end": duration * 0.6, "type": "chorus", "energy": 0.8},
                {"start": duration * 0.6, "end": duration * 0.75, "type": "verse", "energy": 0.5},
                {"start": duration * 0.75, "end": duration * 0.9, "type": "chorus", "energy": 0.9},
                {"start": duration * 0.9, "end": duration, "type": "outro", "energy": 0.3},
            ]

        num_scenes = len(sections)

        # 2. Call AI Story Planner
        ai_scenes = None
        ai_error = None
        try:
            planner = StoryPlanner(model=story_model)
            ai_scenes = planner.plan_story(
                lyrics=lyrics,
                creative_direction=creative_direction,
                num_scenes=num_scenes,
                characters=characters,
                environments=environments,
                section_info=sections,
            )
        except Exception as e:
            ai_error = str(e)
            print(f"[AI PLANNER] Failed, falling back to template: {e}")

        # 3. If AI failed, fall back to template planning
        if not ai_scenes:
            return self.plan_full_video(
                song_path=song_path, style=creative_direction,
                characters=characters, environments=environments,
                engine=engine, natural_pacing=natural_pacing,
                preset_id=preset_id, budget=budget,
                storyline="",
            )

        # 4. V4: Build BEATS from AI scenes, expand each to SHOTS
        from lib.story_planner import expand_beat_to_shots, _SECTION_TO_SEQUENCE
        from lib.prompt_assembler import compile_shot_prompt_v4, DEFAULT_GLOBAL_STYLE

        # Build name->object lookup maps for characters and environments
        char_by_name = {}
        for c in characters:
            char_by_name[c.get("name", "").lower()] = c
        env_by_name = {}
        for e in environments:
            env_by_name[e.get("name", "").lower()] = e

        # Extract style keywords from creative_direction, not the full narrative.
        # The AI planner already generates per-shot narrative — global_style should
        # only carry visual style, not repeat story content on every prompt.
        style_keywords = _extract_style_from_direction(creative_direction) if creative_direction else DEFAULT_GLOBAL_STYLE
        style_bible = {
            "global_style": style_keywords,
            "negative": "no text, no watermark, no blurry, no distorted faces",
        }

        v4_beats = []
        all_shots = []
        cumulative_time = 0.0
        global_shot_idx = 0

        for i, ai_scene in enumerate(ai_scenes):
            section = sections[i] if i < len(sections) else sections[-1]
            stype = section.get("type", "verse")
            energy = section.get("energy", 0.5)
            start = section["start"]
            end = section["end"]

            # Map AI character suggestion to actual character object
            ai_char_name = ai_scene.get("character")
            char = None
            if ai_char_name:
                char = char_by_name.get(ai_char_name.lower())
                if not char:
                    for cname, cobj in char_by_name.items():
                        if ai_char_name.lower() in cname or cname in ai_char_name.lower():
                            char = cobj
                            break
            if not char and characters:
                temp_assignments = self.assign_characters_to_sections(characters, [section])
                char = temp_assignments[0]

            # Map AI environment suggestion to actual environment object
            ai_env_name = ai_scene.get("environment")
            env = None
            if ai_env_name:
                env = env_by_name.get(ai_env_name.lower())
                if not env:
                    for ename, eobj in env_by_name.items():
                        if ai_env_name.lower() in ename or ename in ai_env_name.lower():
                            env = eobj
                            break
            if not env and environments:
                temp_assignments = self.assign_environments_to_sections(environments, [section])
                env = temp_assignments[0]

            # Costume lookup
            costume = None
            if char:
                char_costumes = self.pos.get_costumes(char.get("id"))
                if char_costumes:
                    costume = char_costumes[0]

            # Reference photos
            char_photo = None
            if char and char.get("referencePhoto"):
                char_photo = self._resolve_reference_photo(char["referencePhoto"], "characters")
            env_photo = None
            if env and env.get("referencePhoto"):
                env_photo = self._resolve_reference_photo(env["referencePhoto"], "environments")

            # Build beat dict from AI scene
            beat_dict = {
                "scene_number": i,
                "beat_id": f"beat_{i:02d}",
                "story_beat": ai_scene.get("story_beat", ""),
                "visual_prompt": ai_scene.get("visual_prompt", ""),
                "emotion": ai_scene.get("emotion", ""),
                "character": ai_char_name,
                "environment": ai_env_name,
                "engine": engine,
                "characterId": char["id"] if char else None,
                "characterName": char["name"] if char else None,
                "environmentId": env["id"] if env else None,
                "environmentName": env["name"] if env else None,
                "costumeId": costume["id"] if costume else None,
                "costumeName": costume["name"] if costume else None,
                "character_photo_path": char_photo,
                "environment_photo_path": env_photo,
                "lighting": env.get("lighting", "") if env else "",
                "color_grade": "",
            }

            # Expand beat into 2-5 shots
            sequence_type = _SECTION_TO_SEQUENCE.get(stype, "montage")
            shots = expand_beat_to_shots(
                beat=beat_dict,
                sequence_type=sequence_type,
                energy=energy,
                section_type=stype,
                characters=characters,
                environments=environments,
                audio_beats=beats,
                cumulative_time=cumulative_time,
                global_shot_index=global_shot_idx,
            )

            # Assemble prompts for each shot using V4 compact builder
            char_data = self.pos.get_character(char["id"]) if char else None
            costume_data = self.pos.get_costume(costume["id"]) if costume else None
            env_data = self.pos.get_environment(env["id"]) if env else None

            for si, shot in enumerate(shots):
                shot["prompt"] = compile_shot_prompt_v4(
                    shot=shot,
                    beat=beat_dict,
                    style_bible=style_bible,
                    character=char_data,
                    costume=costume_data,
                    environment=env_data,
                    prev_shot=shots[si - 1] if si > 0 else None,
                    is_first_in_beat=(si == 0),
                )
                # Transition logic
                if global_shot_idx + si == 0:
                    shot["transition"] = "crossfade"
                elif si == 0:
                    prev_stype = sections[i - 1].get("type", "verse") if i > 0 and i < len(sections) else "verse"
                    shot["transition"] = auto_assign_transition(prev_stype, stype)
                else:
                    shot["transition"] = "hard_cut"  # within-beat cuts are hard cuts
                shot["planning_method"] = "ai_v4"

            # Build V4 beat object
            beat_total_dur = sum(s["target_duration"] for s in shots)
            v4_beat = {
                "beat_id": f"beat_{i:02d}",
                "beat_index": i,
                "beat_type": stype,
                "sequence_type": sequence_type,
                "start_sec": start,
                "end_sec": end,
                "energy": energy,
                "story_beat": ai_scene.get("story_beat", ""),
                "emotion": ai_scene.get("emotion", ""),
                "characterId": char["id"] if char else None,
                "environmentId": env["id"] if env else None,
                "shot_count": len(shots),
                "total_duration": round(beat_total_dur, 2),
                "shots": shots,
            }
            v4_beats.append(v4_beat)
            all_shots.extend(shots)
            cumulative_time += beat_total_dur
            global_shot_idx += len(shots)

        # Run shot validators if available
        blocking_errors = []
        try:
            from lib.shot_validator import validate_all
            validation = validate_all(all_shots, characters)
            if validation.get("is_blocked"):
                blocking_errors = validation["character_binding"]["failures"]
                print(f"[AUTO DIRECTOR] Character binding BLOCKED: {blocking_errors}")
        except ImportError:
            pass

        # 4b. Preproduction package binding + taste integration
        try:
            from lib.preproduction_assets import (
                PreproductionStore, bind_shots_to_packages,
                get_shot_package_notes, get_shot_references
            )
            from lib.taste_profile import TasteStore, taste_to_prompt_modifiers
            preprod_store = PreproductionStore(self.output_dir)
            packages = preprod_store.get_all()
            if packages:
                bind_shots_to_packages(all_shots, packages)
                print(f"[AUTO DIRECTOR] Bound {sum(1 for s in all_shots if s.get('character_package_id'))} shots to preproduction packages")

            # Get blended taste profile
            taste_store = TasteStore(os.path.join(self.output_dir, "taste"))
            blended = taste_store.get_blended()
            taste_mods = taste_to_prompt_modifiers(blended) if blended.get("dimensions") else None

            # Re-assemble prompts with package notes and taste
            if packages or taste_mods:
                for si, shot in enumerate(all_shots):
                    pkg_notes = get_shot_package_notes(shot, packages) if packages else None
                    beat_idx = next((bi for bi, b in enumerate(v4_beats) if b["beat_id"] == shot.get("beat_id")), 0)
                    beat_dict_ref = v4_beats[beat_idx] if beat_idx < len(v4_beats) else {}
                    char_id = shot.get("characterId") or beat_dict_ref.get("characterId")
                    env_id = shot.get("environmentId") or beat_dict_ref.get("environmentId")
                    char_data_r = self.pos.get_character(char_id) if char_id else None
                    env_data_r = self.pos.get_environment(env_id) if env_id else None
                    costume_id = shot.get("costumeId") or beat_dict_ref.get("costumeId")
                    costume_data_r = self.pos.get_costume(costume_id) if costume_id else None
                    shot["prompt"] = compile_shot_prompt_v4(
                        shot=shot, beat=beat_dict_ref, style_bible=style_bible,
                        character=char_data_r, costume=costume_data_r,
                        environment=env_data_r,
                        prev_shot=all_shots[si - 1] if si > 0 else None,
                        is_first_in_beat=(shot.get("shot_index", 0) == 0),
                        package_notes=pkg_notes, taste_modifiers=taste_mods,
                    )
        except ImportError:
            pass  # preproduction/taste modules not available

        # 5. Coherence pass on flat shot list
        all_shots = coherence_pass(all_shots)

        # Sync shots back into beats
        shot_idx = 0
        for beat_obj in v4_beats:
            n = beat_obj["shot_count"]
            beat_obj["shots"] = all_shots[shot_idx:shot_idx + n]
            shot_idx += n

        # 6. Cost estimate (V4: count total shots * runway_duration)
        total_runway_secs = sum(s.get("runway_duration", 3) for s in all_shots)
        hero_count = sum(1 for s in all_shots if s.get("is_hero"))
        estimate = self.estimate_cost(len(all_shots), engine)
        estimate["total_shots"] = len(all_shots)
        estimate["total_beats"] = len(v4_beats)
        estimate["total_runway_seconds"] = total_runway_secs
        estimate["hero_shots"] = hero_count
        estimate["hero_take_overhead"] = round(hero_count * 2 * estimate.get("cost_per_scene", 0.15), 2)

        plan = {
            "plan_version": 4,
            "song_path": song_path,
            "style": creative_direction,
            "style_bible": style_bible,
            "lyrics": lyrics,
            "engine": engine,
            "preset_id": preset_id,
            "planning_method": "ai_v4",
            "ai_error": ai_error,
            "bpm": bpm,
            "duration": duration,
            "num_sections": len(sections),
            "beats": v4_beats,
            "scenes": all_shots,  # flattened shot list for backwards compat
            "estimate": estimate,
            "analysis": {
                "bpm": bpm,
                "duration": duration,
                "num_beats": len(beats),
                "sections": sections,
            },
            "characters": [{"id": c["id"], "name": c["name"]} for c in characters],
            "environments": [{"id": e["id"], "name": e["name"]} for e in environments],
            "blocking_errors": blocking_errors,
            "status": "blocked" if blocking_errors else "ready",
            "preproduction": {
                "mode": "fast",
                "packages": {"characters": [], "costumes": [], "environments": [], "props": []},
            },
            "taste": {
                "overall_profile_id": None,
                "project_profile_id": None,
                "inherit_overall": True,
                "blend_summary": {},
            },
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        # Populate preproduction section from store
        try:
            from lib.preproduction_assets import PreproductionStore
            preprod_store = PreproductionStore(self.output_dir)
            all_pkgs = preprod_store.get_all()
            for pkg in all_pkgs:
                t = pkg.get("package_type", "")
                key = t + "s" if not t.endswith("s") else t
                if key in plan["preproduction"]["packages"]:
                    plan["preproduction"]["packages"][key].append({
                        "package_id": pkg["package_id"],
                        "name": pkg.get("name"),
                        "status": pkg.get("status"),
                        "has_hero_ref": bool(pkg.get("hero_image_path")),
                    })
            plan["preproduction"]["mode"] = preprod_store.get_mode()
        except (ImportError, Exception):
            pass

        # Populate taste section
        try:
            from lib.taste_profile import TasteStore, generate_taste_summary
            taste_store = TasteStore(os.path.join(self.output_dir, "taste"))
            overall = taste_store.get_overall()
            if overall:
                plan["taste"]["overall_profile_id"] = overall.get("profile_id")
            blended = taste_store.get_blended()
            if blended.get("dimensions"):
                plan["taste"]["blend_summary"] = blended["dimensions"]
        except (ImportError, Exception):
            pass

        return plan

    # ---- V5 Unified Production Pipeline ----

    def run_pipeline(self, master_prompt: str, song_path: str = None,
                     engine: str = "gen4_5", mode: str = "fast",
                     auto_advance: bool = False, story_model: str = None) -> dict:
        """
        V5 unified pipeline: master prompt -> assets -> plan -> anchors -> video.

        Orchestrates the entire production from a single creative prompt.
        Each stage saves state so the pipeline is resumable.

        Args:
            master_prompt: The user's creative vision in one prompt
            song_path: Optional audio file path
            engine: Video generation engine (gen4_5, gen4_turbo, etc.)
            mode: "fast" or "production" — controls asset sheet detail
            auto_advance: If True, skip approval gates
            story_model: LLM model for story planning

        Returns:
            dict with pipeline state, extraction, packages, plan
        """
        from lib.pipeline_state import PipelineState
        from lib.master_prompt import (
            extract_production_data, extraction_to_packages,
            extraction_to_pos_entities, extraction_to_style_bible,
        )
        from lib.preproduction_assets import PreproductionStore
        from lib.scene_compositor import compose_all_anchors

        # Initialize pipeline state
        pipeline = PipelineState(self.output_dir)
        pipeline.master_prompt = master_prompt
        pipeline.song_path = song_path or ""
        pipeline.engine = engine
        pipeline.mode = mode
        pipeline.auto_advance = auto_advance
        pipeline.story_model = story_model
        pipeline.advance("PROMPT_RECEIVED")

        # ── Stage 1: Extract production data from master prompt ──
        try:
            extraction = extract_production_data(master_prompt, model=story_model)
            pipeline.extraction = extraction
            pipeline.advance("ASSETS_EXTRACTED")
        except Exception as e:
            pipeline.set_error(f"Asset extraction failed: {e}")
            return {"ok": False, "error": str(e), "pipeline": pipeline.get_progress()}

        # ── Stage 2: Create preproduction packages ──
        try:
            packages = extraction_to_packages(extraction, mode=mode)
            preprod_store = PreproductionStore(self.output_dir)
            for pkg in packages:
                preprod_store.save_package(pkg)
            pipeline.packages = [p["package_id"] for p in packages]
            pipeline.advance("PACKAGES_CREATED")
            print(f"[PIPELINE] Created {len(packages)} preproduction packages")
        except Exception as e:
            pipeline.set_error(f"Package creation failed: {e}")
            return {"ok": False, "error": str(e), "pipeline": pipeline.get_progress()}

        # ── Stage 3: Create PromptOS entities for planning ──
        try:
            pos_entities = extraction_to_pos_entities(extraction)
            characters = []
            for c_data in pos_entities["characters"]:
                existing = self.pos.get_character_by_name(c_data["name"]) if hasattr(self.pos, 'get_character_by_name') else None
                if not existing:
                    cid = self.pos.add_character(c_data)
                    c_data["id"] = cid
                else:
                    c_data["id"] = existing.get("id", "")
                characters.append(c_data)

            environments = []
            for e_data in pos_entities["environments"]:
                existing = self.pos.get_environment_by_name(e_data["name"]) if hasattr(self.pos, 'get_environment_by_name') else None
                if not existing:
                    eid = self.pos.add_environment(e_data)
                    e_data["id"] = eid
                else:
                    e_data["id"] = existing.get("id", "")
                environments.append(e_data)
        except Exception as e:
            print(f"[PIPELINE] Warning: PromptOS entity creation: {e}")
            characters = []
            environments = []

        # ── Stage 4: Generate story plan (V4 beat/shot expansion) ──
        style_bible = extraction_to_style_bible(extraction)

        # Find song if not provided
        if not song_path:
            uploads_dir = os.path.join(os.path.dirname(self.output_dir), "uploads")
            if os.path.isdir(uploads_dir):
                audio_files = []
                for f in os.listdir(uploads_dir):
                    if f.endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                        fp = os.path.join(uploads_dir, f)
                        audio_files.append((os.path.getmtime(fp), fp))
                if audio_files:
                    audio_files.sort(reverse=True)
                    song_path = audio_files[0][1]
                    pipeline.song_path = song_path
                    print(f"[PIPELINE] Auto-selected song: {os.path.basename(song_path)}")

        if song_path and os.path.isfile(song_path):
            try:
                plan = self.plan_with_ai(
                    song_path=song_path,
                    creative_direction=master_prompt,
                    characters=characters,
                    environments=environments,
                    engine=engine,
                    story_model=story_model,
                )
                # Bind shots to packages using extraction scene_breakdown
                self._bind_shots_to_packages(plan, extraction, packages)
                pipeline.plan = plan
                pipeline.advance("PLAN_READY")
                print(f"[PIPELINE] Plan ready: {len(plan.get('scenes', []))} shots in {len(plan.get('beats', []))} beats")
            except Exception as e:
                pipeline.set_error(f"Story planning failed: {e}")
                return {"ok": False, "error": str(e), "pipeline": pipeline.get_progress()}
        else:
            pipeline.advance("PLAN_READY")
            print("[PIPELINE] No audio file — plan deferred until song upload")

        return {
            "ok": True,
            "pipeline": pipeline.get_progress(),
            "extraction": extraction,
            "packages": [{"package_id": p["package_id"], "name": p["name"],
                          "type": p["package_type"]} for p in packages],
            "plan_summary": {
                "num_beats": len(pipeline.plan.get("beats", [])),
                "num_shots": len(pipeline.plan.get("scenes", [])),
            },
            "style_bible": style_bible,
        }

    def _bind_shots_to_packages(self, plan: dict, extraction: dict, packages: list):
        """Bind shots to preproduction packages using extraction scene_breakdown."""
        scenes = plan.get("scenes", [])
        scene_breakdown = extraction.get("scene_breakdown", [])
        if not scenes or not scene_breakdown:
            return

        # Build package lookups by name (case-insensitive)
        pkg_by_type_name = {}
        for p in packages:
            key = (p["package_type"], p["name"].lower())
            pkg_by_type_name[key] = p["package_id"]

        # Map beat_index → scene_breakdown entry
        # Beats correspond to scene_breakdown entries by order
        beat_to_scene = {}
        beats = plan.get("beats", [])
        for i, sb in enumerate(scene_breakdown):
            if i < len(beats):
                beat_to_scene[beats[i].get("beat_id", f"beat_{i}")] = sb

        # Also distribute by shot index ranges if no beat mapping
        shots_per_scene = max(1, len(scenes) // max(1, len(scene_breakdown)))

        for si, shot in enumerate(scenes):
            # Find which scene_breakdown this shot belongs to
            beat_id = shot.get("beat_id", "")
            sb = beat_to_scene.get(beat_id)
            if not sb:
                sb_idx = min(si // shots_per_scene, len(scene_breakdown) - 1)
                sb = scene_breakdown[sb_idx] if scene_breakdown else {}

            # Bind character — pick first character in scene
            chars_present = sb.get("characters_present", [])
            if chars_present and not shot.get("character_package_id"):
                for cname in chars_present:
                    pid = pkg_by_type_name.get(("character", cname.lower()))
                    if pid:
                        shot["character_package_id"] = pid
                        shot["characterName"] = cname
                        break

            # Bind environment
            loc = sb.get("location", "")
            if loc and not shot.get("environment_package_id"):
                pid = pkg_by_type_name.get(("environment", loc.lower()))
                if pid:
                    shot["environment_package_id"] = pid
                    shot["environmentName"] = loc

            # Bind costume for the character
            if shot.get("characterName") and not shot.get("costume_package_id"):
                for co in extraction.get("costumes", []):
                    if co.get("character_name", "").lower() == shot["characterName"].lower():
                        pid = pkg_by_type_name.get(("costume", co["name"].lower()))
                        if pid:
                            shot["costume_package_id"] = pid
                            break

            # Bind props
            if not shot.get("prop_package_ids"):
                prop_ids = []
                for pname in sb.get("key_props", []):
                    pid = pkg_by_type_name.get(("prop", pname.lower()))
                    if pid:
                        prop_ids.append(pid)
                shot["prop_package_ids"] = prop_ids

        bound = sum(1 for s in scenes if s.get("character_package_id") or s.get("environment_package_id"))
        print(f"[PIPELINE] Bound {bound}/{len(scenes)} shots to packages")

    def pipeline_generate_anchors(self, progress_cb=None) -> dict:
        """Generate anchor images for all shots using the scene compositor."""
        from lib.pipeline_state import PipelineState
        from lib.preproduction_assets import PreproductionStore
        from lib.scene_compositor import compose_all_anchors
        from lib.taste_profile import TasteStore, taste_to_prompt_modifiers
        from lib.master_prompt import extraction_to_style_bible

        pipeline = PipelineState(self.output_dir)
        if not pipeline.plan or not pipeline.plan.get("scenes"):
            return {"ok": False, "error": "No plan available — run pipeline first"}

        # Load approved packages
        preprod_store = PreproductionStore(self.output_dir)
        packages = [p for p in preprod_store.get_all() if p.get("status") == "approved"]

        if not packages:
            return {"ok": False, "error": "No approved packages — approve canonical sheets first"}

        # Get style bible from extraction
        style_bible = extraction_to_style_bible(pipeline.extraction) if pipeline.extraction else {
            "global_style": "cinematic",
            "negative": "no text, no watermark",
        }

        # Get taste modifiers
        taste_mods = None
        try:
            taste_store = TasteStore(os.path.join(self.output_dir, "taste"))
            blended = taste_store.get_blended()
            if blended.get("dimensions"):
                taste_mods = taste_to_prompt_modifiers(blended)
        except Exception:
            pass

        pipeline.advance("ANCHORS_GENERATING")

        shots = pipeline.plan["scenes"]
        anchors = compose_all_anchors(
            shots=shots,
            packages=packages,
            style_bible=style_bible,
            output_dir=self.output_dir,
            taste_mods=taste_mods,
            progress_cb=progress_cb,
        )

        # Save anchors to pipeline state
        for anchor in anchors:
            pipeline.set_anchor(anchor["shot_id"], anchor)

        # Auto-approve if auto_advance
        if pipeline.auto_advance:
            for anchor in anchors:
                if anchor.get("status") == "generated":
                    pipeline.approve_anchor(anchor["shot_id"])
            pipeline.advance("ANCHORS_REVIEW")
            pipeline.advance("SHOTS_GENERATING")
        else:
            pipeline.advance("ANCHORS_REVIEW")

        # Update plan scenes with anchor paths
        plan_path = os.path.join(self.output_dir, "auto_director_plan.json")
        if os.path.isfile(plan_path):
            import json as _json
            with open(plan_path) as f:
                saved_plan = _json.load(f)
            for scene in saved_plan.get("scenes", []):
                sid = scene.get("shot_id", scene.get("id", ""))
                anchor = pipeline.get_anchor(sid)
                if anchor and anchor.get("image_path"):
                    scene["anchor_image_path"] = anchor["image_path"]
                    scene["anchor_status"] = anchor.get("status", "generated")
            with open(plan_path, "w") as f:
                _json.dump(saved_plan, f, indent=2)

        generated = sum(1 for a in anchors if a.get("status") == "generated")
        return {
            "ok": True,
            "total": len(anchors),
            "generated": generated,
            "failed": sum(1 for a in anchors if a.get("status") == "failed"),
            "skipped": sum(1 for a in anchors if a.get("status") == "skipped"),
            "pipeline": pipeline.get_progress(),
        }

    # ---- Full Video Planning ----

    def plan_full_video(self, song_path: str, style: str,
                         characters=None, environments=None,
                         engine="gen4_5", natural_pacing=True,
                         preset_id=None, budget=None,
                         storyline: str = "") -> dict:
        """
        Plan an entire music video automatically.

        1. Analyze audio (BPM, beats, sections, energy)
        2. Plan scenes with natural durations per section type
        3. Auto-assign characters/environments/costumes
        4. Build prompts combining all entities + style + energy
        5. Auto-assign transitions based on energy changes
        6. Auto-pick durations based on model limits + section type
        7. Return complete scene plan ready for batch generation
        """
        characters = characters or []
        environments = environments or []

        # Auto-describe characters/environments from photos if descriptions empty
        characters = self.enrich_characters(characters)
        environments = self.enrich_environments(environments)

        # Load preset settings
        preset_settings = None
        if preset_id and preset_id in WORKFLOW_PRESETS:
            preset_settings = WORKFLOW_PRESETS[preset_id]["settings"]

        # 1. Analyze audio
        analysis = analyze(song_path)
        sections = analysis.get("sections", [])
        beats = analysis.get("beats", [])
        bpm = analysis.get("bpm", 120)
        duration = analysis.get("duration", 180)

        if not sections:
            # Fallback: create default sections
            sections = [
                {"start": 0, "end": duration * 0.1, "type": "intro", "energy": 0.3},
                {"start": duration * 0.1, "end": duration * 0.4, "type": "verse", "energy": 0.5},
                {"start": duration * 0.4, "end": duration * 0.6, "type": "chorus", "energy": 0.8},
                {"start": duration * 0.6, "end": duration * 0.75, "type": "verse", "energy": 0.5},
                {"start": duration * 0.75, "end": duration * 0.9, "type": "chorus", "energy": 0.9},
                {"start": duration * 0.9, "end": duration, "type": "outro", "energy": 0.3},
            ]

        # 2. Assign characters, environments, costumes
        char_assignments = self.assign_characters_to_sections(characters, sections)
        env_assignments = self.assign_environments_to_sections(environments, sections)
        costume_assignments = self.assign_costumes(characters, char_assignments)

        # 3. Build scenes
        scenes = []
        for i, section in enumerate(sections):
            stype = section.get("type", "verse")
            energy = section.get("energy", 0.5)
            start = section["start"]
            end = section["end"]

            char = char_assignments[i]
            env = env_assignments[i]
            costume = costume_assignments[i]

            # Duration: smart based on engine + section type
            if natural_pacing:
                clip_dur = _pick_natural_duration(stype, energy, beats=beats, start=start, end=end)
            else:
                clip_dur = round(end - start, 3)

            # Snap to engine-valid duration
            clip_dur = get_valid_duration(engine, int(clip_dur))

            # Transition
            if i == 0:
                transition = "crossfade"
            else:
                prev_type = sections[i - 1].get("type", "verse")
                transition = auto_assign_transition(prev_type, stype)

            # Override transitions for presets
            if preset_settings:
                trans_style = preset_settings.get("transition_style", "mixed")
                if trans_style == "slow":
                    if transition == "hard_cut":
                        transition = "dissolve"
                elif trans_style == "fast":
                    if transition in ("dissolve", "fade_black"):
                        transition = "hard_cut"

            # Camera movement
            cam_pool = SECTION_CAMERAS.get(stype, SECTION_CAMERAS["verse"])
            camera = random.choice(cam_pool)

            # Build prompt with storyline beat
            story_beat = ""
            if storyline:
                # Split storyline into beats distributed across scenes
                story_sentences = [s.strip() for s in storyline.replace(". ", ".\n").split("\n") if s.strip()]
                if story_sentences:
                    # Distribute evenly: map scene index to story sentence
                    ratio = len(story_sentences) / len(sections)
                    story_beat = story_sentences[min(int(i * ratio), len(story_sentences) - 1)]

            prompt = self.build_section_prompt(
                character=char, costume=costume, environment=env,
                style=style, energy=energy, section_type=stype,
                preset_settings=preset_settings,
                story_beat=story_beat,
            )

            # Character reference photo
            char_photo = None
            if char and char.get("referencePhoto"):
                char_photo = self._resolve_reference_photo(char["referencePhoto"], "characters")

            # Environment reference photo
            env_photo = None
            if env and env.get("referencePhoto"):
                env_photo = self._resolve_reference_photo(env["referencePhoto"], "environments")

            scene = {
                "id": f"ad_{i:03d}",
                "index": i,
                "start_sec": start,
                "end_sec": end,
                "duration": clip_dur,
                "prompt": prompt,
                "section_type": stype,
                "energy": energy,
                "transition": transition,
                "camera_movement": camera,
                "engine": engine,
                "characterId": char["id"] if char else None,
                "characterName": char["name"] if char else None,
                "environmentId": env["id"] if env else None,
                "environmentName": env["name"] if env else None,
                "costumeId": costume["id"] if costume else None,
                "costumeName": costume["name"] if costume else None,
                "character_photo_path": char_photo,
                "environment_photo_path": env_photo,
                "clip_path": None,
                "has_clip": False,
                "status": "planned",
                "error": None,
            }
            scenes.append(scene)

        # 4. Coherence pass
        scenes = coherence_pass(scenes)

        # 5. Cost estimate
        estimate = self.estimate_cost(len(scenes), engine)

        plan = {
            "song_path": song_path,
            "style": style,
            "engine": engine,
            "preset_id": preset_id,
            "bpm": bpm,
            "duration": duration,
            "num_sections": len(sections),
            "scenes": scenes,
            "estimate": estimate,
            "analysis": {
                "bpm": bpm,
                "duration": duration,
                "num_beats": len(beats),
                "sections": sections,
            },
            "characters": [{"id": c["id"], "name": c["name"]} for c in characters],
            "environments": [{"id": e["id"], "name": e["name"]} for e in environments],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        return plan

    # ---- Full Video Generation ----

    @staticmethod
    def _scene_gen_hash(scene: dict) -> str:
        """Hash generation-relevant fields for cache comparison."""
        parts = [
            scene.get("prompt", ""),
            str(scene.get("duration", 8)),
            scene.get("camera_movement", ""),
            scene.get("engine", ""),
            scene.get("characterId", ""),
            scene.get("costumeId", ""),
            scene.get("environmentId", ""),
            scene.get("character_photo_path", ""),
        ]
        raw = "||".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def generate_full_video(self, plan: dict, cost_cb=None) -> str:
        """
        Execute the full plan:
        1. Generate all clips (batch, 2 concurrent)
        2. Stitch with transitions
        3. Overlay audio
        4. Return path to final video
        """
        scenes = plan["scenes"]
        song_path = plan.get("song_path")

        # Load preproduction packages for hero ref injection during generation
        try:
            from lib.preproduction_assets import PreproductionStore
            preprod_store = PreproductionStore(self.output_dir)
            self._preprod_packages = preprod_store.get_all()
            self._preprod_pkg_index = {p["package_id"]: p for p in self._preprod_packages}
        except (ImportError, Exception):
            self._preprod_packages = []
            self._preprod_pkg_index = {}

        self._update_progress(
            phase="generating",
            total_scenes=len(scenes),
            completed_scenes=0,
            failed_scenes=0,
            scenes=[{
                "id": s["id"],
                "index": s["index"],
                "status": "queued",
                "prompt": s["prompt"][:80],
                "characterName": s.get("characterName"),
                "environmentName": s.get("environmentName"),
                "section_type": s.get("section_type"),
                "error": None,
            } for s in scenes],
            error=None,
            output_file=None,
            plan=plan,
        )

        ad_clips_dir = os.path.join(self.clips_dir, "auto_director")
        os.makedirs(ad_clips_dir, exist_ok=True)

        def generate_one(scene):
            scene_id = scene["id"]
            idx = scene["index"]

            # Cache check: skip if clip exists and settings unchanged
            cur_hash = self._scene_gen_hash(scene)
            clip_file = scene.get("clip_path", "")
            if (scene.get("has_clip") and clip_file
                    and os.path.isfile(clip_file)
                    and scene.get("gen_hash") == cur_hash):
                with self._lock:
                    self._progress["completed_scenes"] += 1
                    for sq in self._progress["scenes"]:
                        if sq["id"] == scene_id:
                            sq["status"] = "cached"
                            break
                return scene_id, True, None

            # Update status
            with self._lock:
                for sq in self._progress["scenes"]:
                    if sq["id"] == scene_id:
                        sq["status"] = "rendering"
                        break

            # Build consistent character description from stored fields
            char_description = ""
            char_data = None
            char_id = scene.get("characterId")
            if char_id:
                char_data = self.pos.get_character(char_id)
                if char_data:
                    desc_parts = []
                    phys = char_data.get("physicalDescription", char_data.get("description", ""))
                    if phys:
                        desc_parts.append(phys)
                    if char_data.get("hair"):
                        desc_parts.append(char_data["hair"])
                    if char_data.get("skinTone"):
                        desc_parts.append(f"{char_data['skinTone']} skin")
                    if char_data.get("distinguishingFeatures"):
                        desc_parts.append(char_data["distinguishingFeatures"])
                    if char_data.get("outfitDescription"):
                        desc_parts.append(f"wearing {char_data['outfitDescription']}")
                    char_description = ", ".join(desc_parts)

            # Build environment description from stored fields
            env_description = ""
            env_data = None
            env_id = scene.get("environmentId")
            if env_id:
                env_data = self.pos.get_environment(env_id)
                if env_data:
                    env_parts = []
                    if env_data.get("description"):
                        env_parts.append(env_data["description"])
                    if env_data.get("lighting"):
                        env_parts.append(env_data["lighting"])
                    if env_data.get("atmosphere"):
                        env_parts.append(env_data["atmosphere"])
                    if env_data.get("location"):
                        env_parts.append(env_data["location"])
                    if env_data.get("weather"):
                        env_parts.append(env_data["weather"])
                    if env_data.get("timeOfDay"):
                        env_parts.append(env_data["timeOfDay"])
                    env_description = ", ".join(env_parts)

            # Build costume description from stored fields
            costume_description = ""
            costume_data = None
            costume_id = scene.get("costumeId")
            if costume_id:
                costume_data = self.pos.get_costume(costume_id)
                if costume_data:
                    if costume_data.get("description"):
                        costume_description = costume_data["description"]
                    else:
                        c_parts = []
                        if costume_data.get("upperBody"):
                            c_parts.append(costume_data["upperBody"])
                        if costume_data.get("lowerBody"):
                            c_parts.append(costume_data["lowerBody"])
                        if costume_data.get("footwear"):
                            c_parts.append(costume_data["footwear"])
                        if costume_data.get("accessories"):
                            c_parts.append(costume_data["accessories"])
                        costume_description = ", ".join(c_parts)

            # Resolve photo paths from entity data for vision API descriptions
            _env_photo_path = ""
            if env_id and env_data:
                _eref = env_data.get("referenceImagePath", "")
                if _eref and os.path.isfile(_eref):
                    _env_photo_path = _eref
                else:
                    _em = re.search(r"/api/pos/environments/([^/]+)/photo", _eref or "")
                    if _em:
                        for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                            _cand = os.path.join(self.output_dir, "prompt_os", "photos", "environments", f"{_em.group(1)}{_ext}")
                            if os.path.isfile(_cand):
                                _env_photo_path = _cand
                                break

            _cos_photo_path = ""
            if costume_id and costume_data:
                _cref = costume_data.get("referenceImagePath", "")
                if _cref and os.path.isfile(_cref):
                    _cos_photo_path = _cref
                else:
                    _cm = re.search(r"/api/pos/costumes/([^/]+)/photo", _cref or "")
                    if _cm:
                        for _ext in (".jpg", ".jpeg", ".png", ".webp"):
                            _cand = os.path.join(self.output_dir, "prompt_os", "photos", "costumes", f"{_cm.group(1)}{_ext}")
                            if os.path.isfile(_cand):
                                _cos_photo_path = _cand
                                break

            # Override photo paths from preproduction packages if bound
            _prop_photo_paths = []
            if scene.get("character_package_id") and hasattr(self, "_preprod_packages"):
                pkg = self._preprod_pkg_index.get(scene["character_package_id"])
                if pkg and pkg.get("hero_image_path") and os.path.isfile(pkg["hero_image_path"]):
                    scene["character_photo_path"] = pkg["hero_image_path"]
            if scene.get("environment_package_id") and hasattr(self, "_preprod_packages"):
                pkg = self._preprod_pkg_index.get(scene["environment_package_id"])
                if pkg and pkg.get("hero_image_path") and os.path.isfile(pkg["hero_image_path"]):
                    _env_photo_path = pkg["hero_image_path"]
            if scene.get("costume_package_id") and hasattr(self, "_preprod_packages"):
                pkg = self._preprod_pkg_index.get(scene["costume_package_id"])
                if pkg and pkg.get("hero_image_path") and os.path.isfile(pkg["hero_image_path"]):
                    _cos_photo_path = pkg["hero_image_path"]
            # Resolve prop photo paths from bound packages
            for prop_pkg_id in (scene.get("prop_package_ids") or []):
                if hasattr(self, "_preprod_pkg_index"):
                    pkg = self._preprod_pkg_index.get(prop_pkg_id)
                    if pkg and pkg.get("hero_image_path") and os.path.isfile(pkg["hero_image_path"]):
                        _prop_photo_paths.append(pkg["hero_image_path"])

            gen_scene = {
                "prompt": scene["prompt"],
                "duration": scene.get("duration", 8),
                "camera_movement": scene.get("camera_movement", "zoom_in"),
                "engine": scene.get("engine", "grok"),
                "id": scene_id,
                "character_description": char_description,
                "is_character_sheet": bool(char_data and char_data.get("isCharacterSheet")),
                "environment_description": env_description,
                "costume_description": costume_description,
                "environment_photo_path": _env_photo_path,
                "costume_photo_path": _cos_photo_path,
                "prop_photo_paths": _prop_photo_paths,
                # Always regenerate first frame from @Tag refs — don't chain
                # Frame chaining loses character/environment consistency
                "first_frame_path": "",
                "continuity_context": scene.get("continuity_context", {}),
                "continuity_mode": False,
            }

            photo_path = scene.get("character_photo_path")

            def on_progress(index, status):
                with self._lock:
                    for sq in self._progress["scenes"]:
                        if sq["id"] == scene_id:
                            sq["status"] = f"rendering: {status}"
                            break

            try:
                clip_path = generate_scene(gen_scene, idx, ad_clips_dir,
                                           progress_cb=on_progress,
                                           cost_cb=cost_cb,
                                           photo_path=photo_path)
                scene["clip_path"] = clip_path
                scene["has_clip"] = True
                scene["status"] = "done"
                scene["gen_hash"] = cur_hash

                with self._lock:
                    self._progress["completed_scenes"] += 1
                    for sq in self._progress["scenes"]:
                        if sq["id"] == scene_id:
                            sq["status"] = "done"
                            break

                return scene_id, True, None
            except Exception as e:
                # Retry once
                try:
                    on_progress(idx, f"retry: {str(e)[:40]}")
                    clip_path = generate_scene(gen_scene, idx, ad_clips_dir,
                                               progress_cb=on_progress,
                                               cost_cb=cost_cb,
                                               photo_path=photo_path)
                    scene["clip_path"] = clip_path
                    scene["has_clip"] = True
                    scene["status"] = "done"
                    scene["gen_hash"] = cur_hash

                    with self._lock:
                        self._progress["completed_scenes"] += 1
                        for sq in self._progress["scenes"]:
                            if sq["id"] == scene_id:
                                sq["status"] = "done"
                                break

                    return scene_id, True, None
                except Exception as e2:
                    scene["status"] = "failed"
                    scene["error"] = str(e2)
                    scene.pop("gen_hash", None)

                    with self._lock:
                        self._progress["failed_scenes"] += 1
                        for sq in self._progress["scenes"]:
                            if sq["id"] == scene_id:
                                sq["status"] = f"failed: {str(e2)[:60]}"
                                sq["error"] = str(e2)
                                break

                    return scene_id, False, str(e2)

        # V4: Generate clips SEQUENTIALLY with smart frame chaining.
        # - Same environment → frame chain (last frame of N = first frame of N+1)
        # - Environment change → break chain, use @tag re-establishment
        # - After generation, trim each clip to exact target_duration
        from lib.video_generator import trim_to_target

        prev_clip_path = None
        prev_scene = None
        trimmed_dir = os.path.join(ad_clips_dir, "trimmed")
        os.makedirs(trimmed_dir, exist_ok=True)

        for scene in scenes:
            # ── Smart frame chaining: only chain when same environment ──
            same_env = (prev_scene
                        and prev_scene.get("environmentId")
                        and prev_scene.get("environmentId") == scene.get("environmentId"))

            if prev_clip_path and os.path.isfile(prev_clip_path) and same_env:
                lf_dir = os.path.join(ad_clips_dir, "last_frames")
                os.makedirs(lf_dir, exist_ok=True)
                lf_path = os.path.join(lf_dir, f"lf_{scene['index']:03d}.jpg")
                try:
                    from lib.video_generator import extract_last_frame
                    extract_last_frame(prev_clip_path, lf_path)
                    scene["first_frame_path"] = lf_path
                    print(f"[AUTO DIRECTOR] Frame chain: shot {scene['index']} "
                          f"from last frame (same env)")
                except Exception as e:
                    print(f"[AUTO DIRECTOR] Frame chain extract failed: {e}")
            elif prev_scene and not same_env:
                print(f"[AUTO DIRECTOR] Env change at shot {scene['index']} "
                      f"— breaking frame chain, using @tag re-establishment")

            # ── Continuity context from previous scene ──
            if prev_scene:
                scene["continuity_context"] = {
                    "has_character": bool(prev_scene.get("characterId")),
                    "character_description": prev_scene.get("character_description_resolved", ""),
                    "character_photo": prev_scene.get("character_photo_path", ""),
                    "same_environment": same_env,
                    "environment_description": prev_scene.get("environment_description_resolved", ""),
                    "previous_prompt": prev_scene.get("prompt", "")[:200],
                    "color_grade": prev_scene.get("color_grade", ""),
                }

            # Generate this shot
            scene_id, ok, err = generate_one(scene)

            # ── V4: Trim clip to exact target_duration ──
            if ok and scene.get("clip_path") and scene.get("target_duration"):
                raw_clip = scene["clip_path"]
                trim_out = os.path.join(trimmed_dir, f"trimmed_{scene['index']:03d}.mp4")
                try:
                    trimmed = trim_to_target(
                        raw_clip, trim_out,
                        trim_in=scene.get("trim_in", 0.0),
                        target_duration=scene["target_duration"],
                    )
                    scene["trimmed_clip_path"] = trimmed
                except Exception as e:
                    print(f"[AUTO DIRECTOR] Trim failed for shot {scene['index']}: {e}")
                    scene["trimmed_clip_path"] = raw_clip

            # Store resolved descriptions for next scene's context
            char_id = scene.get("characterId")
            if char_id:
                char_data = self.pos.get_character(char_id) if char_id else None
                if char_data:
                    desc_parts = []
                    phys = char_data.get("physicalDescription",
                                         char_data.get("description", ""))
                    if phys:
                        desc_parts.append(phys[:300])
                    if char_data.get("hair"):
                        desc_parts.append(char_data["hair"])
                    if char_data.get("distinguishingFeatures"):
                        desc_parts.append(char_data["distinguishingFeatures"])
                    scene["character_description_resolved"] = ", ".join(desc_parts)

            env_id = scene.get("environmentId")
            if env_id:
                env_data = self.pos.get_environment(env_id) if env_id else None
                if env_data:
                    env_parts = []
                    if env_data.get("description"):
                        env_parts.append(env_data["description"])
                    if env_data.get("lighting"):
                        env_parts.append(env_data["lighting"])
                    scene["environment_description_resolved"] = ", ".join(env_parts)

            # Track for next iteration — use trimmed clip for frame chaining
            if ok and scene.get("clip_path"):
                prev_clip_path = scene.get("trimmed_clip_path", scene["clip_path"])
            prev_scene = scene

        # Sync shot statuses back into beats if V4 plan
        if plan.get("plan_version") == 4 and plan.get("beats"):
            shot_idx = 0
            for beat_obj in plan["beats"]:
                n = beat_obj.get("shot_count", len(beat_obj.get("shots", [])))
                beat_obj["shots"] = scenes[shot_idx:shot_idx + n]
                shot_idx += n

        # Save updated plan back to disk (with clip_paths and statuses)
        plan["scenes"] = scenes
        plan_path = os.path.join(self.output_dir, "auto_director_plan.json")
        try:
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[AUTO DIRECTOR] Warning: could not save plan: {e}")

        # Stitch
        self._update_progress(phase="stitching")

        # V4: prefer trimmed clips, fall back to raw clips
        clip_paths = [s.get("trimmed_clip_path") or s.get("clip_path") for s in scenes]
        valid_clips = [c for c in clip_paths if c and os.path.isfile(c)]

        if not valid_clips:
            self._update_progress(phase="error", error="No clips were generated successfully")
            return None

        # Only include transitions for scenes that have valid clips
        transitions = [s.get("transition", "crossfade") for s in scenes
                       if s.get("clip_path") and os.path.isfile(s.get("clip_path", ""))]
        output_path = os.path.join(self.output_dir, "auto_director_final.mp4")
        audio = song_path if song_path and os.path.isfile(song_path) else None

        try:
            stitch(valid_clips, audio, output_path, transitions=transitions)
        except Exception as e:
            self._update_progress(phase="error", error=f"Stitch failed: {e}")
            return None

        self._update_progress(phase="done", output_file=output_path)
        return output_path


# ---- Preset management ----

PRESETS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "workflow_presets.json"
)


def get_workflow_presets() -> list:
    """Get all workflow presets (built-in + custom)."""
    presets = list(WORKFLOW_PRESETS.values())

    # Load custom presets
    if os.path.isfile(PRESETS_PATH):
        try:
            with open(PRESETS_PATH, "r", encoding="utf-8") as f:
                custom = json.load(f)
            if isinstance(custom, list):
                presets.extend(custom)
        except (json.JSONDecodeError, IOError):
            pass

    return presets


def save_custom_preset(preset: dict) -> dict:
    """Save a custom workflow preset."""
    if os.path.isfile(PRESETS_PATH):
        try:
            with open(PRESETS_PATH, "r", encoding="utf-8") as f:
                custom = json.load(f)
        except (json.JSONDecodeError, IOError):
            custom = []
    else:
        custom = []

    # Generate ID if missing
    if not preset.get("id"):
        preset["id"] = f"custom_{int(time.time())}"
    preset["is_custom"] = True

    # Update existing or append
    found = False
    for i, p in enumerate(custom):
        if p.get("id") == preset["id"]:
            custom[i] = preset
            found = True
            break
    if not found:
        custom.append(preset)

    with open(PRESETS_PATH, "w", encoding="utf-8") as f:
        json.dump(custom, f, indent=2, ensure_ascii=False)

    return preset
