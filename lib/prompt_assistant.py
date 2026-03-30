"""
AI-assisted prompt generation.
Provides style presets, prompt enhancement, and song-name-based suggestions.
No external API calls — pure text processing with curated lookup tables.
"""

import re

# ---- Style presets ----

STYLE_PRESETS = {
    "cyberpunk": "neon city streets, rain, holographic signs, dark moody, purple and cyan lighting",
    "synthwave": "retro 80s sunset, grid landscape, chrome, neon pink and blue, VHS aesthetic",
    "dark_cinematic": "shadows, film noir, dramatic lighting, smoke, high contrast black and white",
    "nature": "lush forest, golden hour, mist, flowing water, aerial drone shots",
    "abstract": "geometric shapes, bold colors, fluid motion, kaleidoscope, minimal",
    "horror": "dark corridors, flickering lights, fog, abandoned building, unsettling",
    "space": "nebulas, stars, astronaut, planet surface, cosmic, deep blue and purple",
    "urban": "graffiti walls, skateboarding, street art, handheld camera, gritty",
    "underwater": "deep ocean, bioluminescent creatures, coral reef, blue and green",
    "vintage": "super 8 film grain, faded colors, 1970s aesthetic, warm tones",
    "anime": "anime style, vibrant colors, dynamic action, cherry blossoms",
    "desert": "sand dunes, sunset, silhouettes, warm orange and red, vast landscape",
    "winter": "snow falling, frozen landscape, ice crystals, cold blue light, breath visible",
    "party": "dance floor, laser lights, crowd silhouettes, confetti, high energy",
    "emotional": "rain on window, lonely figure, soft focus, melancholic, warm indoor light",
}

# ---- Enhancement building blocks ----

_LIGHTING_KEYWORDS = [
    "dramatic lighting", "volumetric light", "rim lighting",
    "golden hour", "neon glow", "soft diffused light",
    "chiaroscuro", "backlit silhouette",
]

_CAMERA_KEYWORDS = [
    "cinematic", "shallow depth of field", "anamorphic lens",
    "steadicam tracking shot", "dolly zoom", "wide angle",
    "macro detail", "handheld camera",
]

_MOOD_KEYWORDS = [
    "atmospheric", "moody", "ethereal", "intense",
    "dreamlike", "gritty", "serene", "haunting",
]

_QUALITY_KEYWORDS = [
    "8k", "photorealistic", "highly detailed",
    "professional color grading", "film grain",
    "ray tracing", "unreal engine",
]

# Keywords in song titles mapped to style suggestions
_SONG_KEYWORD_MAP = {
    # mood words
    "dark": "dark_cinematic",
    "shadow": "dark_cinematic",
    "night": "cyberpunk",
    "neon": "cyberpunk",
    "cyber": "cyberpunk",
    "rain": "cyberpunk",
    "retro": "synthwave",
    "80s": "synthwave",
    "sunset": "synthwave",
    "dream": "abstract",
    "space": "space",
    "star": "space",
    "galaxy": "space",
    "cosmic": "space",
    "ocean": "underwater",
    "sea": "underwater",
    "water": "underwater",
    "wave": "underwater",
    "forest": "nature",
    "mountain": "nature",
    "river": "nature",
    "flower": "nature",
    "bloom": "nature",
    "horror": "horror",
    "ghost": "horror",
    "dead": "horror",
    "fear": "horror",
    "scream": "horror",
    "city": "urban",
    "street": "urban",
    "hood": "urban",
    "graffiti": "urban",
    "desert": "desert",
    "sand": "desert",
    "snow": "winter",
    "ice": "winter",
    "cold": "winter",
    "frozen": "winter",
    "party": "party",
    "dance": "party",
    "club": "party",
    "love": "emotional",
    "heart": "emotional",
    "cry": "emotional",
    "tear": "emotional",
    "sad": "emotional",
    "lonely": "emotional",
    "alone": "emotional",
    "anime": "anime",
    "old": "vintage",
    "memory": "vintage",
    "memories": "vintage",
    "fire": "desert",
    "burn": "dark_cinematic",
}

# Genre-to-preset mapping
_GENRE_MAP = {
    "hip hop": "urban",
    "hiphop": "urban",
    "rap": "urban",
    "trap": "cyberpunk",
    "electronic": "synthwave",
    "edm": "party",
    "techno": "cyberpunk",
    "house": "party",
    "dubstep": "cyberpunk",
    "metal": "dark_cinematic",
    "rock": "dark_cinematic",
    "punk": "urban",
    "pop": "party",
    "indie": "vintage",
    "folk": "nature",
    "country": "desert",
    "jazz": "vintage",
    "blues": "emotional",
    "classical": "nature",
    "ambient": "space",
    "lo-fi": "vintage",
    "lofi": "vintage",
    "r&b": "emotional",
    "rnb": "emotional",
    "soul": "emotional",
    "reggae": "nature",
    "latin": "party",
    "k-pop": "anime",
    "kpop": "anime",
    "j-pop": "anime",
    "jpop": "anime",
    "synthpop": "synthwave",
    "darkwave": "dark_cinematic",
    "vaporwave": "synthwave",
    "chillwave": "abstract",
    "shoegaze": "abstract",
    "post-punk": "dark_cinematic",
    "industrial": "cyberpunk",
    "grunge": "dark_cinematic",
    "emo": "emotional",
    "drill": "urban",
    "phonk": "dark_cinematic",
    "hyperpop": "anime",
}


def suggest_style(genre: str = "", mood: str = "") -> str:
    """
    Suggest a style prompt based on genre and/or mood.

    Args:
        genre: music genre (e.g. "electronic", "hip hop")
        mood: mood descriptor (e.g. "dark", "dreamy")

    Returns:
        A style prompt string from the presets, or a combined suggestion.
    """
    genre_lower = genre.strip().lower()
    mood_lower = mood.strip().lower()

    # Try genre first
    preset_key = _GENRE_MAP.get(genre_lower)

    # Try mood as preset key
    if not preset_key and mood_lower in STYLE_PRESETS:
        preset_key = mood_lower

    # Try mood keywords
    if not preset_key:
        for keyword, key in _SONG_KEYWORD_MAP.items():
            if keyword in mood_lower or keyword in genre_lower:
                preset_key = key
                break

    if preset_key and preset_key in STYLE_PRESETS:
        base = STYLE_PRESETS[preset_key]
        # If mood was also given and different from the preset, append it
        if mood_lower and mood_lower != preset_key:
            return f"{base}, {mood}"
        return base

    # Fallback: generic cinematic
    parts = []
    if genre:
        parts.append(genre)
    if mood:
        parts.append(mood)
    parts.append("cinematic, atmospheric, moody lighting, professional color grading")
    return ", ".join(parts)


def enhance_prompt(user_prompt: str) -> str:
    """
    Take a basic user prompt and enrich it with cinematic keywords.
    Adds lighting, camera, mood, and quality terms that are not already present.

    Args:
        user_prompt: the user's raw style description

    Returns:
        An enriched prompt string.
    """
    prompt_lower = user_prompt.lower()
    additions = []

    # Check each category and add one keyword if none from that category exist
    for category in [_LIGHTING_KEYWORDS, _CAMERA_KEYWORDS, _MOOD_KEYWORDS, _QUALITY_KEYWORDS]:
        has_any = any(kw.lower() in prompt_lower for kw in category)
        if not has_any:
            # Pick the first keyword that fits well
            additions.append(category[0])

    if additions:
        return f"{user_prompt.rstrip(', ')}, {', '.join(additions)}"
    return user_prompt


def suggest_genre_from_bpm(bpm: float, energy: float = 0.5) -> dict:
    """
    Estimate genre and suggest matching visual style from BPM and energy.

    Args:
        bpm: beats per minute
        energy: average energy level (0.0 - 1.0)

    Returns:
        dict with keys: genre, style, preset, bpm_range, description
    """
    if bpm >= 130:
        # High BPM: EDM/electronic
        if energy >= 0.6:
            return {
                "genre": "EDM / Electronic",
                "style": "abstract geometric shapes, laser lights, pulsing neon, digital particles, "
                         "futuristic dance floor, strobing colors, high energy visual effects",
                "preset": "party",
                "bpm_range": "high (130+)",
                "description": "High-energy electronic visuals with lasers and abstract shapes",
            }
        else:
            return {
                "genre": "Trance / Ambient Electronic",
                "style": "flowing light trails, cosmic nebula, ethereal particles, "
                         "deep space visuals, hypnotic patterns, soft neon glow",
                "preset": "space",
                "bpm_range": "high (130+)",
                "description": "Ethereal electronic visuals with cosmic and hypnotic patterns",
            }
    elif bpm >= 90:
        # Mid BPM: hip-hop/pop
        if energy >= 0.6:
            return {
                "genre": "Hip-Hop / Pop",
                "style": "urban streetscape, stylish fashion, neon signs, luxury cars, "
                         "city nightlife, dynamic camera movements, bold colors",
                "preset": "urban",
                "bpm_range": "mid (90-130)",
                "description": "Urban street style with bold colors and city nightlife",
            }
        else:
            return {
                "genre": "R&B / Chill Pop",
                "style": "soft golden hour lighting, intimate setting, warm tones, "
                         "slow motion details, elegant and moody atmosphere",
                "preset": "emotional",
                "bpm_range": "mid (90-130)",
                "description": "Warm intimate visuals with soft lighting and elegant mood",
            }
    else:
        # Low BPM: ballad/ambient
        if energy >= 0.5:
            return {
                "genre": "Rock Ballad / Folk",
                "style": "sweeping landscape, golden hour, mountain vistas, "
                         "flowing water, natural beauty, cinematic wide shots",
                "preset": "nature",
                "bpm_range": "low (60-90)",
                "description": "Sweeping natural landscapes with cinematic wide shots",
            }
        else:
            return {
                "genre": "Ambient / Ballad",
                "style": "soft focus, rain on window, gentle mist, emotional close-ups, "
                         "candlelight, melancholic atmosphere, pastel tones",
                "preset": "emotional",
                "bpm_range": "low (60-90)",
                "description": "Soft emotional visuals with gentle atmosphere and pastel tones",
            }


def suggest_from_song_name(name: str) -> str:
    """
    Extract keywords from a song title/filename to suggest a matching style preset.

    Args:
        name: song title or filename

    Returns:
        A style prompt string.
    """
    # Clean up filename: remove extension, replace separators
    clean = re.sub(r'\.[^.]+$', '', name)  # remove extension
    clean = re.sub(r'[-_]', ' ', clean)     # replace separators with spaces
    clean = clean.lower().strip()

    # Try to match keywords
    for keyword, preset_key in _SONG_KEYWORD_MAP.items():
        if keyword in clean:
            if preset_key in STYLE_PRESETS:
                return STYLE_PRESETS[preset_key]

    # No match found, return a generic suggestion
    return "cinematic, atmospheric, moody lighting, dramatic, 4k, professional color grading"


def get_preset_names() -> list:
    """Return sorted list of available preset names."""
    return sorted(STYLE_PRESETS.keys())


def get_preset(name: str) -> str | None:
    """Get the prompt text for a preset by name. Returns None if not found."""
    return STYLE_PRESETS.get(name.lower().strip())
