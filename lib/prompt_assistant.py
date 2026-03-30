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


# ---- Color palette extraction (Item 9) ----

# Mapping from dominant color hue/saturation/value to color grade presets
_PALETTE_TO_GRADE = {
    "warm": ["warm"],
    "cold": ["cold"],
    "dark": ["noir", "dark_cinematic"],
    "neon": ["cyberpunk"],
    "vintage": ["vintage", "sepia"],
    "neutral": ["none"],
}


def extract_palette(image_path: str, n_colors: int = 5) -> dict:
    """
    Extract the dominant color palette from an image using PIL.

    Args:
        image_path: path to an image file (jpg, png, etc.)
        n_colors: number of dominant colors to extract (default 5)

    Returns:
        dict with keys:
            colors: list of hex color strings (e.g. ["#ff2d7b", "#00d4ff", ...])
            rgb_colors: list of (r, g, b) tuples
            suggested_grade: name of the best-matching color grade preset
            palette_description: human-readable description of the palette mood
    """
    try:
        from PIL import Image
    except ImportError:
        return {
            "colors": [],
            "rgb_colors": [],
            "suggested_grade": "none",
            "palette_description": "PIL not available",
        }

    import os
    if not os.path.isfile(image_path):
        return {
            "colors": [],
            "rgb_colors": [],
            "suggested_grade": "none",
            "palette_description": "Image not found",
        }

    img = Image.open(image_path).convert("RGB")
    # Resize to small for fast processing
    img = img.resize((150, 150), Image.LANCZOS)

    # Quantize to n_colors using PIL's built-in quantization
    quantized = img.quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT)
    palette_data = quantized.getpalette()  # flat list [r, g, b, r, g, b, ...]

    # Count pixels per palette index to sort by dominance
    pixel_counts = {}
    for pixel in quantized.getdata():
        pixel_counts[pixel] = pixel_counts.get(pixel, 0) + 1

    # Sort by frequency (most dominant first)
    sorted_indices = sorted(pixel_counts.keys(), key=lambda k: pixel_counts[k], reverse=True)

    rgb_colors = []
    hex_colors = []
    for idx in sorted_indices[:n_colors]:
        r = palette_data[idx * 3]
        g = palette_data[idx * 3 + 1]
        b = palette_data[idx * 3 + 2]
        rgb_colors.append((r, g, b))
        hex_colors.append(f"#{r:02x}{g:02x}{b:02x}")

    # Analyze palette mood and suggest color grade
    suggested_grade, description = _analyze_palette_mood(rgb_colors)

    return {
        "colors": hex_colors,
        "rgb_colors": rgb_colors,
        "suggested_grade": suggested_grade,
        "palette_description": description,
    }


def _analyze_palette_mood(rgb_colors: list) -> tuple:
    """
    Analyze RGB color list and suggest a color grade preset + description.

    Returns:
        (grade_name, description_string)
    """
    if not rgb_colors:
        return "none", "No colors detected"

    # Calculate averages
    avg_r = sum(c[0] for c in rgb_colors) / len(rgb_colors)
    avg_g = sum(c[1] for c in rgb_colors) / len(rgb_colors)
    avg_b = sum(c[2] for c in rgb_colors) / len(rgb_colors)
    avg_brightness = (avg_r + avg_g + avg_b) / 3.0
    avg_saturation = max(avg_r, avg_g, avg_b) - min(avg_r, avg_g, avg_b)

    descriptions = []

    # Dark palette
    if avg_brightness < 80:
        descriptions.append("dark")
        if avg_b > avg_r and avg_b > avg_g:
            descriptions.append("moody blue")
            return "cold", "Dark, moody blue tones"
        if avg_r > avg_g and avg_r > avg_b:
            descriptions.append("dramatic red")
            return "noir", "Dark, dramatic with red undertones"
        return "noir", "Dark, high-contrast palette"

    # Very bright
    if avg_brightness > 200:
        descriptions.append("bright")
        if avg_r > avg_b:
            return "warm", "Bright, warm palette"
        return "none", "Bright, clean palette"

    # Warm palette (reds/oranges/yellows dominate)
    if avg_r > avg_b + 30 and avg_r > avg_g:
        descriptions.append("warm")
        if avg_saturation < 60:
            return "sepia", "Warm, desaturated vintage tones"
        return "warm", "Warm tones with rich oranges and reds"

    # Cold palette (blues dominate)
    if avg_b > avg_r + 30:
        descriptions.append("cold")
        if avg_saturation > 120:
            return "cyberpunk", "Vivid cold neon tones"
        return "cold", "Cool blue palette"

    # High saturation / neon
    if avg_saturation > 150:
        descriptions.append("vivid")
        return "high_contrast", "Vivid, high-saturation palette"

    # Low saturation
    if avg_saturation < 40:
        descriptions.append("muted")
        return "vintage", "Muted, vintage-style palette"

    return "none", "Balanced, neutral palette"


# ---- Color Palette Extraction (Roadmap Item 9) ----

def extract_palette(image_path: str, n_colors: int = 5) -> list:
    """Extract dominant colors from an image. Returns list of hex color strings."""
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        img = img.resize((100, 100))  # small for speed
        pixels = list(img.getdata())
        
        # Simple k-means-like clustering
        from collections import Counter
        # Quantize to reduce colors
        quantized = [(r // 32 * 32, g // 32 * 32, b // 32 * 32) for r, g, b in pixels]
        counts = Counter(quantized).most_common(n_colors)
        return [f"#{r:02x}{g:02x}{b:02x}" for (r, g, b), _ in counts]
    except Exception as e:
        print(f"[palette] Error: {e}")
        return ["#333333"] * n_colors


def suggest_grade_from_palette(palette: list) -> str:
    """Suggest a color grade preset based on dominant palette colors."""
    if not palette:
        return "none"
    # Simple heuristic based on average warmth
    avg_r = sum(int(c[1:3], 16) for c in palette) / len(palette)
    avg_b = sum(int(c[5:7], 16) for c in palette) / len(palette)
    if avg_r > avg_b + 40:
        return "warm"
    elif avg_b > avg_r + 40:
        return "cold"
    elif avg_r < 80 and avg_b < 80:
        return "noir"
    return "none"
