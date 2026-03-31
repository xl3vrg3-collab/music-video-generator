"""
Story Planner — Uses an LLM (Grok) to create coherent music video storylines from
lyrics + creative direction, replacing template-based scene planning with intelligent
AI-driven storytelling.

Falls back gracefully to template planning if the API call fails.
"""

import json
import os
import re


# System prompt for the LLM
STORY_DIRECTOR_SYSTEM_PROMPT = """\
You are a music video director. Given song lyrics and a creative direction,
create a scene-by-scene visual storyline for a music video.

Rules:
- Each scene must connect to the previous one narratively
- Match scene emotions to the lyrics at that timestamp
- Use the provided characters and environments
- Vary camera movements and shot types
- Build tension through verse, climax at chorus, resolve at bridge/outro
- Every scene prompt must be a detailed visual description for AI video generation
- Do NOT include text overlays or lyrics in the visual description
- Focus on what the VIEWER SEES, not what they hear
- Camera suggestions should be one of: zoom_in, zoom_out, pan_left, pan_right, orbit, tracking, static

Output JSON array of scenes."""


def _format_time(seconds: float) -> str:
    """Format seconds as m:ss."""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


class StoryPlanner:
    """Uses an LLM to create a coherent music video storyline from lyrics + creative direction."""

    def __init__(self, api_key: str = None, model: str = "grok-3-mini"):
        self.api_key = api_key or os.environ.get("XAI_API_KEY", "")
        self.model = model

    def plan_story(self, lyrics: str, creative_direction: str,
                   num_scenes: int, characters: list, environments: list,
                   section_info: list) -> list:
        """
        Takes lyrics + one creative prompt and generates scene-by-scene prompts.

        Args:
            lyrics: full song lyrics
            creative_direction: user's vision ("dark redemption in cyberpunk city")
            num_scenes: how many scenes to create
            characters: list of character dicts from Prompt OS
            environments: list of environment dicts
            section_info: [{type: "intro", start, end, energy}, ...] from audio analysis

        Returns:
            list of {scene_number, section_type, story_beat, emotion,
                     visual_prompt, character, environment, camera}
        """
        if not self.api_key:
            raise ValueError("XAI_API_KEY not set — cannot use AI story planner")

        user_prompt = self._build_user_prompt(
            lyrics, creative_direction, num_scenes, characters, environments, section_info
        )

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
                has_photo = "(has reference photo)" if char.get("referencePhoto") else ""
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
        """Call the Grok text API and return the raw response content."""
        import requests

        resp = requests.post(
            "https://api.x.ai/v1/chat/completions",
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
                "max_tokens": 4000,
                "temperature": 0.7,
            },
            timeout=60,
        )

        if resp.status_code != 200:
            error_detail = ""
            try:
                error_detail = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                error_detail = resp.text[:200]
            raise RuntimeError(f"Grok API error {resp.status_code}: {error_detail}")

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise RuntimeError("Empty response from Grok API")
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
