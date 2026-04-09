"""
Movie Planner — Structured movie-building system for LUMN Studio.

Replaces the repetitive scene plan generator with a proper narrative engine
that builds a MovieBible, plans beats, constructs scenes with state tracking,
validates coverage, and produces delta-first shot prompts.

All planning is heuristic-based (no AI API calls) for instant results.
"""

import json
import math
import os
import re
import threading
import time
import uuid


# ──────────────────────────── Helpers ────────────────────────────

def _gen_id():
    return str(uuid.uuid4())[:8]


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _safe_name(obj):
    """Extract name from a dict or object."""
    if isinstance(obj, dict):
        return obj.get("name", "unknown")
    return getattr(obj, "name", "unknown")


def _safe_id(obj):
    if isinstance(obj, dict):
        return obj.get("id", "")
    return getattr(obj, "id", "")


def _safe_get(obj, key, default=""):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ──────────────────────────── Beat Types ────────────────────────────

BEAT_TYPES = [
    "opening",       # establish world, tone, character
    "discovery",     # something new revealed
    "tension",       # conflict, stakes rise
    "escalation",    # intensity peaks
    "breakthrough",  # resolution moment
    "release",       # ending, reflection, transformation
]

BEAT_PURPOSES = {
    "opening": "Establish the world, introduce the tone and the main character/setting",
    "discovery": "Reveal something new — a character, location, emotion, or secret",
    "tension": "Raise the stakes, introduce conflict or uncertainty",
    "escalation": "Push intensity to its peak, maximum visual and emotional energy",
    "breakthrough": "The turning point — resolution, revelation, or catharsis",
    "release": "Wind down, reflect, transform, or leave the audience with a final image",
}

BEAT_EMOTIONS = {
    "opening": ("curiosity", "anticipation"),
    "discovery": ("wonder", "intrigue"),
    "tension": ("anxiety", "excitement"),
    "escalation": ("adrenaline", "awe"),
    "breakthrough": ("catharsis", "triumph"),
    "release": ("peace", "nostalgia"),
}

# Map audio section types to beat types
SECTION_TO_BEAT = {
    "intro": "opening",
    "verse": "discovery",
    "chorus": "escalation",
    "bridge": "tension",
    "outro": "release",
    "drop": "escalation",
    "build": "tension",
    "breakdown": "tension",
    "hook": "breakthrough",
    "pre-chorus": "tension",
    "post-chorus": "release",
    "interlude": "discovery",
}

# Camera directions per beat type
CAMERA_DIRECTIONS = {
    "opening": ["wide establishing shot", "slow aerial descent", "tracking shot approaching subject",
                 "crane shot revealing the environment"],
    "discovery": ["medium shot following character", "dolly push-in to reveal detail",
                   "pan across new environment", "over-shoulder reveal shot"],
    "tension": ["handheld close-up", "dutch angle", "rapid push-in",
                 "low angle looking up", "tight framing with shallow depth"],
    "escalation": ["dynamic sweeping crane", "fast tracking shot", "360-degree orbit",
                    "extreme close-up to wide pull", "whip pan"],
    "breakthrough": ["slow-motion close-up", "steadicam circular reveal",
                      "dramatic zoom out", "bird's-eye overhead shot"],
    "release": ["slow pull-back to wide", "floating aerial ascent",
                 "gentle dolly out", "static contemplative frame"],
}

LIGHTING_DIRECTIONS = {
    "opening": "soft ambient light with slight mystery, dawn-like quality",
    "discovery": "warm directional light revealing details, golden highlights",
    "tension": "harsh contrasting shadows, dramatic side-lighting",
    "escalation": "intense saturated lighting, strobing or pulsing quality",
    "breakthrough": "bright breakthrough light, lens flare, volumetric rays",
    "release": "soft diffused glow, twilight warmth, gentle fade",
}

COLOR_DIRECTIONS = {
    "opening": "muted palette with one accent color emerging",
    "discovery": "warm tones expanding, richer saturation",
    "tension": "desaturated with sharp red or blue accents, high contrast",
    "escalation": "fully saturated, vibrant, possibly neon or electric",
    "breakthrough": "bright whites and golds breaking through dark tones",
    "release": "soft pastels, or warm amber fading to cool tones",
}

MOTION_DIRECTIONS = {
    "opening": "slow and deliberate, establishing rhythm",
    "discovery": "moderate pace, exploratory movement",
    "tension": "quickening pace, restless energy",
    "escalation": "fast and dynamic, peak kinetic energy",
    "breakthrough": "sudden shift — either explosive burst or dramatic stillness",
    "release": "decelerating, coming to rest, gentle drift",
}

TRANSITION_MAP = {
    "opening": {"in": "fade_from_black", "out": "dissolve"},
    "discovery": {"in": "dissolve", "out": "crossfade"},
    "tension": {"in": "hard_cut", "out": "hard_cut"},
    "escalation": {"in": "hard_cut", "out": "flash_white"},
    "breakthrough": {"in": "flash_white", "out": "dissolve"},
    "release": {"in": "dissolve", "out": "fade_to_black"},
}

# Visual intensity per beat (escalation curve)
BEAT_ESCALATION = {
    "opening": 0.2,
    "discovery": 0.4,
    "tension": 0.6,
    "escalation": 0.9,
    "breakthrough": 1.0,
    "release": 0.3,
}


# ──────────────────────────── Keyword extraction ────────────────────────────

_EMOTION_KEYWORDS = {
    "love": "love", "heart": "love", "kiss": "love", "baby": "love",
    "cry": "sadness", "tear": "sadness", "pain": "sadness", "hurt": "sadness",
    "lost": "sadness", "gone": "sadness", "miss": "longing",
    "fire": "passion", "burn": "passion", "flame": "passion",
    "fight": "anger", "war": "anger", "rage": "anger", "scream": "anger",
    "free": "liberation", "fly": "liberation", "rise": "liberation", "break": "liberation",
    "dark": "darkness", "night": "darkness", "shadow": "darkness",
    "light": "hope", "sun": "hope", "shine": "hope", "star": "hope",
    "dream": "wonder", "magic": "wonder", "wonder": "wonder",
    "dance": "joy", "party": "joy", "celebrate": "joy", "happy": "joy",
    "run": "urgency", "chase": "urgency", "fast": "urgency",
    "alone": "isolation", "empty": "isolation", "cold": "isolation",
    "power": "strength", "strong": "strength", "king": "strength", "queen": "strength",
    "home": "comfort", "warm": "comfort", "safe": "comfort",
    "dead": "death", "die": "death", "end": "finality", "last": "finality",
    "new": "renewal", "begin": "renewal", "born": "renewal", "start": "renewal",
}

_THEME_KEYWORDS = {
    "love": ["love", "heart", "kiss", "baby", "together", "forever", "hold"],
    "rebellion": ["fight", "break", "free", "rebel", "against", "burn", "rise"],
    "loss": ["lost", "gone", "miss", "cry", "tear", "empty", "fade"],
    "empowerment": ["power", "strong", "queen", "king", "crown", "rule", "own"],
    "journey": ["road", "walk", "run", "path", "find", "search", "way"],
    "transformation": ["change", "new", "become", "evolve", "grow", "born"],
    "celebration": ["dance", "party", "tonight", "celebrate", "alive", "feel"],
    "darkness": ["dark", "night", "shadow", "demon", "hell", "black"],
    "hope": ["light", "sun", "shine", "hope", "dream", "believe", "tomorrow"],
    "isolation": ["alone", "cold", "empty", "nobody", "silence", "wall"],
}


def _extract_emotions_from_text(text):
    """Extract emotional beats from lyrics/storyline text."""
    if not text:
        return ["neutral"]
    words = re.findall(r'\b\w+\b', text.lower())
    emotions = []
    for w in words:
        if w in _EMOTION_KEYWORDS:
            emotions.append(_EMOTION_KEYWORDS[w])
    # Deduplicate preserving order
    seen = set()
    unique = []
    for e in emotions:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    return unique if unique else ["neutral"]


def _detect_theme(text):
    """Detect the dominant theme from text."""
    if not text:
        return "visual journey"
    words = set(re.findall(r'\b\w+\b', text.lower()))
    scores = {}
    for theme, keywords in _THEME_KEYWORDS.items():
        scores[theme] = sum(1 for k in keywords if k in words)
    if not scores or max(scores.values()) == 0:
        return "visual journey"
    return max(scores, key=scores.get)


# ──────────────────────────── MovieBible ────────────────────────────

class MovieBible:
    """Structured object built from all user inputs that defines the entire movie."""

    def __init__(self):
        self.concept = ""
        self.theme = ""
        self.story_arc = ""
        self.emotional_arc = []       # list of emotional beats
        self.visual_arc = []          # list of visual progression points
        self.world_rules = ""
        self.characters = []          # full character objects with photos
        self.costumes = []
        self.environments = []
        self.ending_state = ""
        self.progression_strategy = ""
        self.style = ""
        self.world_setting = ""
        self.universal_prompt = ""
        # Film mode params (defaults to music_video behavior)
        self.project_mode = "music_video"
        self.film_runtime = 60
        self.film_scene_count = 5
        self.film_pacing = "medium"
        self.film_climax_position = "late"
        self.film_tension_curve = "exponential"
        self.film_ending_type = "bittersweet"

    @classmethod
    def from_inputs(cls, style, lyrics, storyline, world_setting, universal_prompt,
                    characters, costumes, environments, engine, preset,
                    project_mode="music_video", film_runtime=60, film_scene_count=5,
                    film_pacing="medium", film_climax_position="late",
                    film_tension_curve="exponential", film_ending_type="bittersweet"):
        """Build a movie bible from all user inputs."""
        bible = cls()
        bible.style = style or "cinematic"
        bible.world_rules = world_setting or ""
        bible.world_setting = world_setting or ""
        bible.universal_prompt = universal_prompt or ""
        bible.characters = characters or []
        bible.costumes = costumes or []
        bible.environments = environments or []

        # Store film mode params
        bible.project_mode = project_mode
        bible.film_runtime = film_runtime
        bible.film_scene_count = film_scene_count
        bible.film_pacing = film_pacing
        bible.film_climax_position = film_climax_position
        bible.film_tension_curve = film_tension_curve
        bible.film_ending_type = film_ending_type

        # Derive theme, concept, arcs from inputs
        is_film = (project_mode != "music_video")
        mode_label = project_mode.replace("_", " ").title() if is_film else "music video"
        bible.concept = storyline or f"A {mode_label} with {style} aesthetic"
        bible.theme = cls._extract_theme(storyline, lyrics)
        bible.story_arc = cls._build_story_arc(storyline, lyrics,
                                                len(characters), len(environments))
        bible.emotional_arc = cls._build_emotional_arc(lyrics)
        bible.visual_arc = cls._build_visual_arc(style, environments)
        if is_film and film_ending_type:
            bible.ending_state = cls._infer_ending_from_type(film_ending_type)
        else:
            bible.ending_state = cls._infer_ending(storyline, lyrics)
        bible.progression_strategy = cls._choose_progression(preset, storyline)
        return bible

    def to_dict(self):
        """Serialize for storage/API."""
        def _obj_to_dict(obj):
            if isinstance(obj, dict):
                return obj
            try:
                return vars(obj)
            except TypeError:
                return {"value": str(obj)}

        d = {
            "concept": self.concept,
            "theme": self.theme,
            "story_arc": self.story_arc,
            "emotional_arc": self.emotional_arc,
            "visual_arc": self.visual_arc,
            "world_rules": self.world_rules,
            "characters": [_obj_to_dict(c) for c in self.characters],
            "costumes": [_obj_to_dict(c) for c in self.costumes],
            "environments": [_obj_to_dict(e) for e in self.environments],
            "ending_state": self.ending_state,
            "progression_strategy": self.progression_strategy,
            "style": self.style,
            "world_setting": self.world_setting,
            "universal_prompt": self.universal_prompt,
            "project_mode": getattr(self, "project_mode", "music_video"),
            "film_runtime": getattr(self, "film_runtime", 60),
            "film_scene_count": getattr(self, "film_scene_count", 5),
            "film_pacing": getattr(self, "film_pacing", "medium"),
            "film_climax_position": getattr(self, "film_climax_position", "late"),
            "film_tension_curve": getattr(self, "film_tension_curve", "exponential"),
            "film_ending_type": getattr(self, "film_ending_type", "bittersweet"),
        }
        return d

    @staticmethod
    def _extract_theme(storyline, lyrics):
        """Derive a one-line theme from storyline and lyrics."""
        combined = f"{storyline or ''} {lyrics or ''}"
        theme = _detect_theme(combined)
        # Build a one-liner
        theme_descriptions = {
            "love": "A story of connection, desire, and emotional intimacy",
            "rebellion": "A tale of defiance, breaking free from constraints",
            "loss": "An exploration of grief, absence, and what remains",
            "empowerment": "A declaration of strength, ownership, and self-sovereignty",
            "journey": "A quest through unknown territory toward self-discovery",
            "transformation": "A metamorphosis from one state of being to another",
            "celebration": "An explosion of joy, life, and shared ecstasy",
            "darkness": "A descent into shadow, confronting what lurks within",
            "hope": "A reach toward light through the weight of the world",
            "isolation": "The weight of solitude and the echo of empty spaces",
            "visual journey": "A cinematic visual experience driven by mood and aesthetic",
        }
        return theme_descriptions.get(theme, f"A visual narrative exploring {theme}")

    @staticmethod
    def _build_story_arc(storyline, lyrics, num_chars, num_envs):
        """Choose an arc structure based on available material."""
        combined = f"{storyline or ''} {lyrics or ''}".lower()
        emotions = _extract_emotions_from_text(combined)

        # Choose arc structure based on content
        has_conflict = any(e in emotions for e in ["anger", "urgency", "darkness"])
        has_love = any(e in emotions for e in ["love", "passion", "comfort"])
        has_transformation = any(e in emotions for e in ["liberation", "renewal", "strength"])
        has_loss = any(e in emotions for e in ["sadness", "longing", "isolation"])

        if has_conflict and has_transformation:
            return "struggle → resistance → breaking point → breakthrough → transformation → freedom"
        elif has_love and has_loss:
            return "connection → intimacy → fracture → grief → acceptance → memory"
        elif has_love:
            return "meeting → attraction → connection → passion → union → tenderness"
        elif has_loss:
            return "presence → distance → emptiness → searching → acceptance → peace"
        elif has_transformation:
            return "stasis → awakening → growth → challenge → metamorphosis → emergence"
        elif has_conflict:
            return "calm → provocation → confrontation → escalation → climax → aftermath"
        elif num_envs >= 3:
            return "arrival → exploration → immersion → revelation → peak → departure"
        elif num_chars >= 2:
            return "introduction → interaction → tension → alignment → climax → resolution"
        else:
            return "establishing → building → developing → intensifying → climaxing → resolving"

    @staticmethod
    def _build_emotional_arc(lyrics):
        """Map emotional progression from lyrics analysis."""
        if not lyrics:
            return [
                {"position": 0.0, "emotion": "neutral", "intensity": 0.3},
                {"position": 0.25, "emotion": "curiosity", "intensity": 0.5},
                {"position": 0.5, "emotion": "engagement", "intensity": 0.7},
                {"position": 0.75, "emotion": "peak", "intensity": 1.0},
                {"position": 1.0, "emotion": "resolution", "intensity": 0.4},
            ]

        # Split lyrics into chunks and analyze each
        lines = [l.strip() for l in lyrics.strip().split("\n") if l.strip()]
        if not lines:
            return [{"position": 0.0, "emotion": "neutral", "intensity": 0.5}]

        arc = []
        chunk_size = max(1, len(lines) // 5)
        for i in range(0, len(lines), chunk_size):
            chunk = " ".join(lines[i:i + chunk_size])
            emotions = _extract_emotions_from_text(chunk)
            position = i / max(len(lines) - 1, 1)
            # Intensity follows a natural arc curve
            intensity = 0.3 + 0.7 * math.sin(position * math.pi)
            arc.append({
                "position": round(position, 2),
                "emotion": emotions[0] if emotions else "neutral",
                "intensity": round(intensity, 2),
            })
        return arc

    @staticmethod
    def _build_visual_arc(style, environments):
        """Plan visual escalation across the video."""
        env_names = [_safe_name(e) for e in environments] if environments else ["main setting"]

        arc = [
            {"position": 0.0, "visual": "muted, establishing",
             "description": f"Introduce {env_names[0]} with restrained palette"},
            {"position": 0.2, "visual": "warming, revealing",
             "description": "Colors begin to saturate, details emerge"},
            {"position": 0.4, "visual": "building, dynamic",
             "description": "Camera becomes more active, visual complexity increases"},
            {"position": 0.6, "visual": "intense, vivid",
             "description": "Full visual intensity, rich color, dramatic framing"},
            {"position": 0.8, "visual": "peak, overwhelming",
             "description": "Maximum visual impact, fastest cuts, strongest contrasts"},
            {"position": 1.0, "visual": "resolving, softening",
             "description": "Visual energy dissipates, gentle conclusion"},
        ]

        # Distribute environments across visual arc
        if len(env_names) > 1:
            for i, point in enumerate(arc):
                env_idx = min(i * len(env_names) // len(arc), len(env_names) - 1)
                point["environment"] = env_names[env_idx]

        return arc

    @staticmethod
    def _infer_ending(storyline, lyrics):
        """What state should the video end in."""
        combined = f"{storyline or ''} {lyrics or ''}".lower()
        emotions = _extract_emotions_from_text(combined)

        if "liberation" in emotions or "renewal" in emotions:
            return "transformed — the subject has changed, broken free, become something new"
        elif "love" in emotions or "comfort" in emotions:
            return "connected — warmth, togetherness, emotional fulfillment"
        elif "sadness" in emotions or "longing" in emotions:
            return "reflective — a bittersweet stillness, carrying the weight of memory"
        elif "strength" in emotions or "passion" in emotions:
            return "empowered — standing tall, claiming space, radiating power"
        elif "darkness" in emotions or "death" in emotions:
            return "faded — dissolving into shadow, a haunting final image"
        elif "joy" in emotions:
            return "elated — energy and light, frozen in a moment of pure joy"
        else:
            return "resolved — the visual journey has completed its arc, returning to stillness"

    @staticmethod
    def _infer_ending_from_type(film_ending_type):
        """Infer ending state from the film ending type selector."""
        endings = {
            "resolved": "resolved — conflict is settled, characters find peace and closure",
            "bittersweet": "bittersweet — victory comes at a cost, joy tinged with loss",
            "open": "open — the question lingers, the audience decides what happens next",
            "tragic": "tragic — loss prevails, a haunting final image of what was lost",
            "cliffhanger": "suspended — everything hangs in the balance, demanding continuation",
            "cyclical": "cyclical — we return to where we began, but everything has changed",
        }
        return endings.get(film_ending_type, "resolved — the story reaches its natural conclusion")

    @staticmethod
    def _choose_progression(preset, storyline):
        """Choose progression strategy: linear, cyclical, parallel, etc."""
        if preset:
            preset_name = preset if isinstance(preset, str) else _safe_get(preset, "id", "")
            if preset_name in ("performance_video",):
                return "cyclical"  # Performance loops between verse/chorus setups
            elif preset_name in ("cinematic_short",):
                return "linear"  # Straight narrative
            elif preset_name in ("lyric_video",):
                return "parallel"  # Visual runs alongside text
        if storyline and len(storyline) > 50:
            return "linear"  # User provided a narrative, follow it
        return "linear"


# ──────────────────────────── BeatPlanner ────────────────────────────

class BeatPlanner:
    """Plans narrative beats across scenes."""

    @staticmethod
    def plan_beats(bible, num_scenes, audio_sections=None, project_mode="music_video"):
        """Create a beat plan that distributes beats across scenes.

        Each beat has:
        - order: int
        - beat_type: str
        - purpose: str
        - emotional_goal: str
        - escalation_level: float (0.0 to 1.0)
        - required_assets: dict
        - delta_goal: str
        - inherits_from_previous: list
        - audio_section: dict or None
        """
        beats = []

        is_film = (project_mode != "music_video")

        # If we have audio sections and are in music mode, map them to beat types
        if not is_film and audio_sections and len(audio_sections) > 0:
            beats = BeatPlanner._beats_from_audio(bible, audio_sections, num_scenes)
        elif is_film:
            beats = BeatPlanner._beats_from_film(bible, num_scenes)
        else:
            beats = BeatPlanner._beats_from_count(bible, num_scenes)

        # Assign assets to beats (ensure full coverage)
        BeatPlanner._assign_assets(bible, beats)

        return beats

    @staticmethod
    def _beats_from_audio(bible, audio_sections, num_scenes):
        """Generate beats mapped to audio sections."""
        beats = []
        n = min(num_scenes, len(audio_sections))

        for i in range(n):
            section = audio_sections[i]
            section_type = section.get("type", "verse")
            beat_type = SECTION_TO_BEAT.get(section_type, "discovery")

            # Override first and last
            if i == 0:
                beat_type = "opening"
            elif i == n - 1:
                beat_type = "release"
            # Ensure we get a breakthrough somewhere in the 70-85% range
            elif 0.65 < (i / max(n - 1, 1)) < 0.85:
                if not any(b.get("beat_type") == "breakthrough" for b in beats):
                    beat_type = "breakthrough"

            escalation = BeatPlanner._escalation_at(i, n)
            em_arc = bible.emotional_arc
            em_pos = i / max(n - 1, 1)
            emotional_goal = BeatPlanner._emotion_at_position(em_arc, em_pos)

            beats.append({
                "order": i,
                "beat_type": beat_type,
                "purpose": BEAT_PURPOSES.get(beat_type, "Advance the narrative"),
                "emotional_goal": emotional_goal,
                "escalation_level": round(escalation, 2),
                "required_assets": {},
                "delta_goal": BeatPlanner._delta_for_beat(beat_type, i, n),
                "inherits_from_previous": BeatPlanner._inheritance(beat_type, i),
                "audio_section": section,
            })
        return beats

    @staticmethod
    def _beats_from_count(bible, num_scenes):
        """Generate beats from just a scene count (no audio)."""
        beats = []
        # Distribute beat types across the arc
        arc_sequence = BeatPlanner._build_beat_sequence(num_scenes)

        for i in range(num_scenes):
            beat_type = arc_sequence[i]
            escalation = BeatPlanner._escalation_at(i, num_scenes)
            em_arc = bible.emotional_arc
            em_pos = i / max(num_scenes - 1, 1)
            emotional_goal = BeatPlanner._emotion_at_position(em_arc, em_pos)

            beats.append({
                "order": i,
                "beat_type": beat_type,
                "purpose": BEAT_PURPOSES.get(beat_type, "Advance the narrative"),
                "emotional_goal": emotional_goal,
                "escalation_level": round(escalation, 2),
                "required_assets": {},
                "delta_goal": BeatPlanner._delta_for_beat(beat_type, i, num_scenes),
                "inherits_from_previous": BeatPlanner._inheritance(beat_type, i),
                "audio_section": None,
            })
        return beats

    @staticmethod
    def _beats_from_film(bible, num_scenes):
        """Generate beats for film mode using narrative structure and film timing params."""
        beats = []
        pacing = getattr(bible, "film_pacing", "medium")
        climax_pos = getattr(bible, "film_climax_position", "late")
        tension_curve = getattr(bible, "film_tension_curve", "exponential")
        ending_type = getattr(bible, "film_ending_type", "bittersweet")

        # Determine breakthrough (climax) position
        climax_fractions = {"early": 0.30, "middle": 0.50, "late": 0.75, "final": 0.90}
        climax_frac = climax_fractions.get(climax_pos, 0.75)
        climax_index = max(1, min(num_scenes - 2, int(round(num_scenes * climax_frac))))
        if climax_pos == "final":
            climax_index = num_scenes - 1

        # Build beat sequence for film
        seq = []
        for i in range(num_scenes):
            if i == 0:
                seq.append("opening")
            elif i == climax_index:
                seq.append("breakthrough")
            elif i == num_scenes - 1:
                seq.append("release")
            elif i < climax_index:
                # Pre-climax: cycle through discovery, tension, escalation
                pre_cycle = ["discovery", "tension", "escalation"]
                if pacing == "slow":
                    pre_cycle = ["discovery", "discovery", "tension", "escalation"]
                elif pacing == "fast":
                    pre_cycle = ["tension", "escalation"]
                seq.append(pre_cycle[(i - 1) % len(pre_cycle)])
            else:
                # Post-climax: wind down
                if ending_type in ("tragic", "cliffhanger"):
                    seq.append("tension")
                elif ending_type == "cyclical":
                    seq.append("discovery")
                else:
                    seq.append("release" if i == num_scenes - 1 else "discovery")

        # Compute escalation based on tension curve
        for i in range(num_scenes):
            beat_type = seq[i]
            t = i / max(num_scenes - 1, 1)

            if tension_curve == "linear":
                raw_esc = t if t <= climax_frac else max(0.1, 1.0 - (t - climax_frac) / (1.0 - climax_frac))
            elif tension_curve == "wave":
                wave = 0.5 * (1 + math.sin(2 * math.pi * t * 2 - math.pi / 2))
                envelope = t / climax_frac if t <= climax_frac else max(0.1, 1.0 - (t - climax_frac) / (1.0 - climax_frac))
                raw_esc = wave * 0.4 + envelope * 0.6
            elif tension_curve == "plateau":
                if t < 0.4:
                    raw_esc = t / 0.4 * 0.7
                elif t < 0.7:
                    raw_esc = 0.7 + (t - 0.4) / 0.3 * 0.3
                elif t < 0.85:
                    raw_esc = 1.0
                else:
                    raw_esc = max(0.1, 1.0 - (t - 0.85) / 0.15 * 0.9)
            else:  # exponential (default)
                if t <= climax_frac:
                    raw_esc = (t / climax_frac) ** 2
                else:
                    raw_esc = max(0.1, 1.0 - ((t - climax_frac) / (1.0 - climax_frac)) ** 0.5)

            raw_esc = max(0.1, min(1.0, raw_esc))
            em_arc = bible.emotional_arc
            em_pos = t
            emotional_goal = BeatPlanner._emotion_at_position(em_arc, em_pos)

            beats.append({
                "order": i,
                "beat_type": beat_type,
                "purpose": BEAT_PURPOSES.get(beat_type, "Advance the narrative"),
                "emotional_goal": emotional_goal,
                "escalation_level": round(raw_esc, 2),
                "required_assets": {},
                "delta_goal": BeatPlanner._delta_for_beat(beat_type, i, num_scenes),
                "inherits_from_previous": BeatPlanner._inheritance(beat_type, i),
                "audio_section": None,
            })
        return beats

    @staticmethod
    def _build_beat_sequence(num_scenes):
        """Build an ideal beat type sequence for N scenes."""
        if num_scenes <= 1:
            return ["opening"]
        if num_scenes == 2:
            return ["opening", "release"]
        if num_scenes == 3:
            return ["opening", "escalation", "release"]

        seq = ["opening"]
        middle = num_scenes - 2
        # Distribute: discovery, tension, escalation, breakthrough across middle
        # The pattern repeats: discovery → tension → escalation, with breakthrough near 75%
        breakthrough_pos = max(1, int(num_scenes * 0.75))
        cycle = ["discovery", "tension", "escalation"]
        for i in range(1, num_scenes - 1):
            if i == breakthrough_pos - 1:
                seq.append("breakthrough")
            else:
                seq.append(cycle[(i - 1) % len(cycle)])
        seq.append("release")
        return seq

    @staticmethod
    def _escalation_at(index, total):
        """Calculate escalation level at a given position.
        Follows a dramatic arc: slow build, peak at ~75%, drop at end.
        """
        if total <= 1:
            return 0.5
        t = index / (total - 1)
        # Modified sine curve: peaks at ~75% of the way through
        # Shift the peak: sin(pi * (t * 0.75 + 0.125))
        raw = math.sin(math.pi * (t * 0.85 + 0.075))
        return max(0.1, min(1.0, raw))

    @staticmethod
    def _emotion_at_position(emotional_arc, position):
        """Interpolate emotion from the emotional arc at a given position."""
        if not emotional_arc:
            return "engaged"
        # Find surrounding points
        prev = emotional_arc[0]
        for point in emotional_arc:
            if point["position"] <= position:
                prev = point
            else:
                break
        return prev.get("emotion", "engaged")

    @staticmethod
    def _delta_for_beat(beat_type, index, total):
        """What must change in this scene."""
        deltas = {
            "opening": "The world exists — establish setting, tone, and visual language",
            "discovery": "Something new is revealed that wasn't visible before",
            "tension": "The emotional or visual stakes visibly increase",
            "escalation": "Intensity breaks through to a new level",
            "breakthrough": "An irreversible shift — nothing can go back to how it was",
            "release": "Energy transforms into its final form — resolution or reflection",
        }
        base = deltas.get(beat_type, "Advance the narrative meaningfully")
        if index == 0:
            return "First impression — everything is new, set the tone for the entire piece"
        if index == total - 1:
            return "Final image — this is what the audience carries away"
        return base

    @staticmethod
    def _inheritance(beat_type, index):
        """What carries forward from the previous scene."""
        if index == 0:
            return []
        return ["emotional_state", "visual_palette", "character_state", "world_state"]

    @staticmethod
    def _assign_assets(bible, beats):
        """Distribute characters, costumes, and environments across beats.
        Ensures every asset is used at least once.
        """
        chars = bible.characters or []
        costumes = bible.costumes or []
        envs = bible.environments or []
        n = len(beats)
        if n == 0:
            return

        # Characters: distribute across beats, ensuring each appears at least once
        if chars:
            # Primary character appears in most scenes
            primary_char = chars[0]
            for beat in beats:
                beat["required_assets"]["characters"] = [
                    {"id": _safe_id(primary_char), "name": _safe_name(primary_char)}
                ]
            # Additional characters get introduced in discovery/tension beats
            secondary_indices = [i for i, b in enumerate(beats)
                                 if b["beat_type"] in ("discovery", "tension", "escalation")]
            for ci, char in enumerate(chars[1:], 1):
                if secondary_indices:
                    idx = secondary_indices[ci % len(secondary_indices)]
                else:
                    idx = min(ci, n - 1)
                assets = beats[idx]["required_assets"].get("characters", [])
                assets.append({"id": _safe_id(char), "name": _safe_name(char)})
                beats[idx]["required_assets"]["characters"] = assets

        # Costumes: spread across scenes
        if costumes:
            costume_beats = list(range(n))
            for ci, costume in enumerate(costumes):
                idx = costume_beats[ci % len(costume_beats)]
                beats[idx]["required_assets"]["costumes"] = beats[idx]["required_assets"].get("costumes", [])
                beats[idx]["required_assets"]["costumes"].append(
                    {"id": _safe_id(costume), "name": _safe_name(costume)}
                )

        # Environments: distribute with intention
        if envs:
            if len(envs) == 1:
                # Same environment throughout
                for beat in beats:
                    beat["required_assets"]["environments"] = [
                        {"id": _safe_id(envs[0]), "name": _safe_name(envs[0])}
                    ]
            else:
                # Distribute environments across the arc
                env_block_size = max(1, n // len(envs))
                for i, beat in enumerate(beats):
                    env_idx = min(i // env_block_size, len(envs) - 1)
                    beat["required_assets"]["environments"] = [
                        {"id": _safe_id(envs[env_idx]), "name": _safe_name(envs[env_idx])}
                    ]
                # Ensure all environments used at least once
                for ei, env in enumerate(envs):
                    used = any(
                        any(a.get("id") == _safe_id(env)
                            for a in b["required_assets"].get("environments", []))
                        for b in beats
                    )
                    if not used and beats:
                        idx = min(ei * env_block_size, n - 1)
                        beats[idx]["required_assets"]["environments"] = [
                            {"id": _safe_id(env), "name": _safe_name(env)}
                        ]


# ──────────────────────────── SceneBuilder ────────────────────────────

class SceneBuilder:
    """Builds structured scene objects from beats."""

    @staticmethod
    def build_scenes(bible, beats, audio_sections=None):
        """Build structured scene objects from beats.

        Returns list of scene dicts with comprehensive fields for the
        editing UI and generation pipeline.
        """
        scenes = []
        prev_state = {
            "emotional": "neutral",
            "visual_intensity": 0.0,
            "world": "unestablished",
        }

        total_duration = 0
        is_film = getattr(bible, "project_mode", "music_video") != "music_video"

        if audio_sections:
            total_duration = max(
                (s.get("end", 0) for s in audio_sections), default=0
            )
        elif is_film:
            total_duration = getattr(bible, "film_runtime", 60)

        # Pre-compute film scene durations if in film mode
        film_durations = None
        if is_film and len(beats) > 0:
            film_durations = SceneBuilder._compute_film_durations(bible, beats)

        for i, beat in enumerate(beats):
            audio = beat.get("audio_section")
            if audio:
                start_time = audio.get("start", 0)
                end_time = audio.get("end", start_time + 5.0)
            elif film_durations:
                start_time = sum(film_durations[:i])
                end_time = start_time + film_durations[i]
            else:
                start_time = i * 5.0
                end_time = start_time + 5.0
            duration = round(end_time - start_time, 3)

            beat_type = beat["beat_type"]
            escalation = beat["escalation_level"]

            # Emotional shift
            new_emotion = beat["emotional_goal"]
            emotional_shift = {"from": prev_state["emotional"], "to": new_emotion}

            # Visual shift
            new_visual = round(escalation, 2)
            visual_shift = {
                "from": f"intensity {prev_state['visual_intensity']:.1f}",
                "to": f"intensity {new_visual:.1f}",
            }

            # World shift
            env_assets = beat["required_assets"].get("environments", [])
            env_name = env_assets[0]["name"] if env_assets else "continuous"
            world_shift = f"Setting: {env_name}" if i == 0 else f"Environment continues as {env_name}"
            if i > 0:
                prev_envs = beats[i - 1]["required_assets"].get("environments", [])
                prev_env_name = prev_envs[0]["name"] if prev_envs else ""
                if env_name != prev_env_name and prev_env_name:
                    world_shift = f"Transition from {prev_env_name} to {env_name}"

            # Characters in scene
            char_assets = beat["required_assets"].get("characters", [])
            costume_assets = beat["required_assets"].get("costumes", [])

            # Camera, lighting, color, motion from beat type
            import random as _rand
            cam_options = CAMERA_DIRECTIONS.get(beat_type, ["medium shot"])
            camera_dir = cam_options[i % len(cam_options)]
            lighting_dir = LIGHTING_DIRECTIONS.get(beat_type, "natural lighting")
            color_dir = COLOR_DIRECTIONS.get(beat_type, "natural palette")
            motion_dir = MOTION_DIRECTIONS.get(beat_type, "moderate movement")

            # Transitions
            trans = TRANSITION_MAP.get(beat_type, {"in": "crossfade", "out": "crossfade"})
            transition_in = trans["in"] if i > 0 else "fade_from_black"
            transition_out = trans["out"]

            # Build action description
            action = SceneBuilder._build_action(beat, bible, i, len(beats))

            # Build title
            arc_parts = bible.story_arc.split("→") if "→" in bible.story_arc else bible.story_arc.split(" → ")
            arc_parts = [p.strip() for p in arc_parts]
            title_hint = arc_parts[min(i, len(arc_parts) - 1)] if arc_parts else beat_type
            title = f"Scene {i + 1}: {title_hint.capitalize()}"

            # Delta — what irreversibly changes
            delta = beat["delta_goal"]

            # Start/end state tracking
            start_state = dict(prev_state)
            end_state = {
                "emotional": new_emotion,
                "visual_intensity": new_visual,
                "world": env_name,
            }

            # Section type for generation compatibility
            section_type = "verse"
            if audio:
                section_type = audio.get("type", "verse")
            elif beat_type == "opening":
                section_type = "intro"
            elif beat_type == "release":
                section_type = "outro"
            elif beat_type in ("escalation", "breakthrough"):
                section_type = "chorus"
            elif beat_type == "tension":
                section_type = "bridge"

            scene = {
                "id": _gen_id(),
                "order": i,
                "title": title,
                "beat_type": beat_type,
                "purpose": beat["purpose"],
                "summary": action,
                "characters": [
                    {"id": c.get("id", ""), "name": c.get("name", ""), "role_in_scene": "protagonist" if ci == 0 else "featured"}
                    for ci, c in enumerate(char_assets)
                ],
                "costumes": [
                    {"id": c.get("id", ""), "name": c.get("name", ""), "when_worn": "throughout"}
                    for c in costume_assets
                ],
                "environments": [
                    {"id": e.get("id", ""), "name": e.get("name", ""), "how_used": "main setting" if ei == 0 else "secondary setting"}
                    for ei, e in enumerate(env_assets)
                ],
                "props": [],
                "unresolved_mentions": [],
                "asset_validation": {"has_mismatch": False, "warnings": []},
                "emotional_shift": emotional_shift,
                "visual_shift": visual_shift,
                "world_shift": world_shift,
                "camera_direction": camera_dir,
                "lighting_direction": lighting_dir,
                "color_direction": color_dir,
                "motion_direction": motion_dir,
                "action": action,
                "transition_in": transition_in,
                "transition_out": transition_out,
                "start_state": start_state,
                "end_state": end_state,
                "delta": delta,
                "carries_forward": beat.get("inherits_from_previous", []),
                "duration": duration,
                "time_start": round(start_time, 3),
                "time_end": round(end_time, 3),
                "shot_prompt": "",  # Built by PromptBuilder
                "notes": "",
                "locks": {},
                "validation": {},
                "status": "draft",
                # Generation pipeline compatibility fields
                "prompt": "",
                "start_sec": round(start_time, 3),
                "end_sec": round(end_time, 3),
                "section_type": section_type,
                "matched_references": [],
                "transition": transition_in if i > 0 else "crossfade",
                "clip_path": None,
                "energy": escalation,
                "preview": {
                    "status": "none",
                    "image_url": None,
                    "prompt_hash": None,
                    "last_generated_at": None,
                    "engine": None,
                    "error": None,
                },
            }

            # Build the shot prompt
            prev_scene = scenes[-1] if scenes else None
            scene["shot_prompt"] = PromptBuilder.build_shot_prompt(scene, bible, prev_scene)
            scene["prompt"] = scene["shot_prompt"]  # Alias for generation pipeline

            # Validate asset bindings
            SceneBuilder.validate_scene_assets(scene, bible)

            scenes.append(scene)
            prev_state = end_state

        return scenes

    @staticmethod
    def _build_action(beat, bible, index, total):
        """Build a description of what happens in this scene."""
        beat_type = beat["beat_type"]
        chars = beat["required_assets"].get("characters", [])
        envs = beat["required_assets"].get("environments", [])

        char_str = ", ".join(c.get("name", "subject") for c in chars) if chars else "the subject"
        env_str = envs[0].get("name", "the setting") if envs else "the setting"

        actions = {
            "opening": f"{char_str} appears in {env_str}. The world establishes its rules — colors, mood, and rhythm emerge.",
            "discovery": f"{char_str} encounters something new within {env_str}. A detail or presence that shifts understanding.",
            "tension": f"Pressure builds around {char_str} in {env_str}. The visual and emotional stakes escalate visibly.",
            "escalation": f"{char_str} is caught in peak intensity within {env_str}. Everything amplifies — motion, color, energy.",
            "breakthrough": f"A decisive shift for {char_str}. The visual language breaks from its pattern in {env_str}.",
            "release": f"{char_str} settles into the aftermath within {env_str}. Energy transforms into its final state.",
        }
        return actions.get(beat_type, f"{char_str} continues through {env_str}.")

    @staticmethod
    def validate_scene_assets(scene, bible):
        """Check scene text against assigned assets. Return validation dict."""
        warnings = []
        unresolved = []

        # Get all text to scan
        text = ' '.join([
            scene.get('summary', ''),
            scene.get('action', ''),
            scene.get('shot_prompt', ''),
            scene.get('prompt', ''),
        ]).lower()

        # Get assigned asset names
        assigned_char_names = [c.get('name', '').lower() for c in scene.get('characters', []) if c.get('name')]
        assigned_costume_names = [c.get('name', '').lower() for c in scene.get('costumes', []) if c.get('name')]
        assigned_env_names = [e.get('name', '').lower() for e in scene.get('environments', []) if e.get('name')]

        # Get all available asset names from bible
        all_chars = bible.characters if hasattr(bible, 'characters') else []
        all_costumes = bible.costumes if hasattr(bible, 'costumes') else []
        all_envs = bible.environments if hasattr(bible, 'environments') else []

        all_char_names = [_safe_name(c).lower() for c in all_chars if _safe_name(c)]
        all_costume_names = [_safe_name(c).lower() for c in all_costumes if _safe_name(c)]
        all_env_names = [_safe_name(e).lower() for e in all_envs if _safe_name(e)]

        # Check: available assets mentioned in text but not assigned
        for name in all_char_names:
            if name and len(name) > 1 and name in text and name not in assigned_char_names:
                warnings.append(f'Scene mentions "{name}" but character not assigned')
        for name in all_costume_names:
            if name and len(name) > 1 and name in text and name not in assigned_costume_names:
                warnings.append(f'Scene mentions "{name}" but costume not assigned')
        for name in all_env_names:
            if name and len(name) > 1 and name in text and name not in assigned_env_names:
                warnings.append(f'Scene mentions "{name}" but environment not assigned')

        # Update scene fields
        scene['unresolved_mentions'] = unresolved
        scene['asset_validation'] = {
            "has_mismatch": len(warnings) > 0 or len(unresolved) > 0,
            "warnings": warnings,
            "unresolved": unresolved,
        }

        return scene['asset_validation']

    @staticmethod
    def match_text_to_assets(text, available_assets):
        """Match text against available assets using substring/word matching.

        Args:
            text: Scene text to scan
            available_assets: List of asset dicts with 'id' and 'name' keys

        Returns:
            List of {asset_id, asset_name, confidence} for fuzzy matches
        """
        if not text or not available_assets:
            return []

        text_lower = text.lower()
        words = set(re.findall(r'\b\w+\b', text_lower))
        matches = []

        for asset in available_assets:
            asset_name = _safe_name(asset).lower()
            asset_id = _safe_id(asset)
            if not asset_name or not asset_id:
                continue

            confidence = 0.0

            # Exact substring match
            if asset_name in text_lower:
                confidence = 0.9
            else:
                # Word-level matching
                asset_words = set(re.findall(r'\b\w+\b', asset_name))
                if asset_words:
                    overlap = asset_words & words
                    if overlap:
                        confidence = 0.5 * len(overlap) / len(asset_words)

            if confidence > 0.2:
                matches.append({
                    "asset_id": asset_id,
                    "asset_name": _safe_name(asset),
                    "confidence": round(confidence, 2),
                })

        matches.sort(key=lambda m: m["confidence"], reverse=True)
        return matches

    @staticmethod
    def _compute_film_durations(bible, beats):
        """Compute scene durations for film mode based on runtime, pacing, and beat importance."""
        total_runtime = getattr(bible, "film_runtime", 60)
        pacing = getattr(bible, "film_pacing", "medium")
        n = len(beats)
        if n == 0:
            return []

        # Weight beats by importance: escalation/breakthrough get more time
        weights = []
        for beat in beats:
            bt = beat.get("beat_type", "discovery")
            w = {
                "opening": 1.2,
                "discovery": 1.0,
                "tension": 1.0,
                "escalation": 1.4,
                "breakthrough": 1.5,
                "release": 1.1,
            }.get(bt, 1.0)
            weights.append(w)

        # Adjust for pacing
        pacing_mult = {"slow": 1.3, "medium": 1.0, "fast": 0.8}.get(pacing, 1.0)
        # Slow pacing gives more time to contemplative beats (opening, release, discovery)
        # Fast pacing gives more time to action beats (escalation, tension)
        for i, beat in enumerate(beats):
            bt = beat.get("beat_type", "discovery")
            if pacing == "slow" and bt in ("opening", "release", "discovery"):
                weights[i] *= 1.2
            elif pacing == "fast" and bt in ("escalation", "tension", "breakthrough"):
                weights[i] *= 1.2

        total_weight = sum(weights)
        durations = [round(total_runtime * w / total_weight, 3) for w in weights]

        # Ensure minimum duration per scene
        min_dur = 3.0
        for i in range(len(durations)):
            if durations[i] < min_dur:
                durations[i] = min_dur

        # Re-normalize to fit total_runtime
        dur_sum = sum(durations)
        if dur_sum > 0:
            scale = total_runtime / dur_sum
            durations = [round(d * scale, 3) for d in durations]

        return durations


# ──────────────────────────── PromptBuilder ────────────────────────────

class PromptBuilder:
    """Builds generation-ready shot prompts using delta-first strategy."""

    QUALITY_SUFFIX = "cinematic, 4k, detailed, professional color grading"

    @staticmethod
    def build_shot_prompt(scene, bible, prev_scene=None):
        """Build the actual generation prompt for a scene.

        Uses delta-first strategy:
        1. Start with what's NEW in this scene (not the global concept)
        2. Include assigned character/costume/environment details
        3. Include camera, lighting, color directions
        4. Include emotional and visual goals
        5. Add universal prompt suffix
        6. Reference previous scene state for continuity
        """
        parts = []

        # 1. Delta-first: what's NEW in this scene
        beat_type = scene.get("beat_type", "discovery")
        delta = scene.get("delta", "")
        action = scene.get("action", "")

        is_film = getattr(bible, "project_mode", "music_video") != "music_video"

        if prev_scene:
            # Describe what CHANGES, not what stays the same
            em_shift = scene.get("emotional_shift", {})
            if em_shift.get("from") != em_shift.get("to"):
                if is_film:
                    parts.append(f"Emotional shift from {em_shift.get('from', 'neutral')} to {em_shift.get('to', 'neutral')}")
                else:
                    parts.append(f"Shifting from {em_shift.get('from', 'neutral')} to {em_shift.get('to', 'neutral')}")
            world_shift = scene.get("world_shift", "")
            if "Transition" in world_shift:
                parts.append(world_shift)
        else:
            # First scene — establish
            if is_film:
                parts.append(f"Opening scene — establishing the narrative world and characters")
            else:
                parts.append(f"Opening scene establishing the world")

        # 2. Character/costume/environment details (resolved from bible by ID)
        char_descs = []
        for char_ref in scene.get("characters", []):
            char_id = char_ref.get("id", "")
            role = char_ref.get("role_in_scene", "featured")
            # Find full character data from bible
            for c in bible.characters:
                if _safe_id(c) == char_id:
                    name = _safe_get(c, "name", "")
                    physical = _safe_get(c, "physicalDescription", _safe_get(c, "physical", ""))
                    desc = physical or _safe_get(c, "description", "")
                    has_photo = bool(_safe_get(c, "referencePhoto", "") or _safe_get(c, "previewImage", ""))
                    char_parts = []
                    if name:
                        char_parts.append(name)
                    if role and role != "featured":
                        char_parts.append(f"({role})")
                    if desc:
                        char_parts.append(f"— {desc}")
                    if has_photo:
                        char_parts.append("[reference photo available]")
                    if char_parts:
                        char_descs.append(" ".join(char_parts))
                    break
        if char_descs:
            parts.append(". ".join(char_descs))

        for cost_ref in scene.get("costumes", []):
            cost_id = cost_ref.get("id", "")
            for c in bible.costumes:
                if _safe_id(c) == cost_id:
                    name = _safe_get(c, "name", "")
                    desc = _safe_get(c, "description", "")
                    color = _safe_get(c, "color", "")
                    material = _safe_get(c, "material", "")
                    costume_parts = [p for p in [desc, color, material] if p]
                    if costume_parts:
                        label = f"{name}: " if name else ""
                        parts.append(f"Wearing: {label}{', '.join(costume_parts)}")
                    elif name:
                        parts.append(f"Wearing: {name}")
                    break

        for env_ref in scene.get("environments", []):
            env_id = env_ref.get("id", "")
            for e in bible.environments:
                if _safe_id(e) == env_id:
                    desc = _safe_get(e, "description", "")
                    name = _safe_get(e, "name", "")
                    atmosphere = _safe_get(e, "atmosphere", "")
                    lighting = _safe_get(e, "lighting", "")
                    time_of_day = _safe_get(e, "timeOfDay", _safe_get(e, "time_of_day", ""))
                    env_parts = [p for p in [desc or name, atmosphere, lighting, time_of_day] if p]
                    if env_parts:
                        parts.append(f"Setting: {', '.join(env_parts)}")
                    break

        # 3. Camera, lighting, color, motion
        camera = scene.get("camera_direction", "")
        if camera:
            parts.append(camera)

        lighting = scene.get("lighting_direction", "")
        if lighting:
            parts.append(lighting)

        color = scene.get("color_direction", "")
        if color:
            parts.append(color)

        motion = scene.get("motion_direction", "")
        if motion:
            parts.append(motion)

        # 4. Style
        if bible.style:
            parts.append(bible.style)

        # 5. World setting
        if bible.world_setting:
            parts.append(f"World: {bible.world_setting}")

        # 6. Quality suffix
        parts.append(PromptBuilder.QUALITY_SUFFIX)

        # 6b. Film mode: add narrative-focused direction
        if is_film:
            parts.append("story-driven visual storytelling, narrative progression, character-focused")

        # 7. Universal prompt
        if bible.universal_prompt:
            parts.append(bible.universal_prompt)

        # 8. Continuity from previous scene
        if prev_scene:
            parts.append("continuing visual coherence from previous scene")

        return ", ".join(p for p in parts if p)


# ──────────────────────────── AssetCoverage ────────────────────────────

class AssetCoverage:
    """Checks if all selected assets are used across scenes."""

    @staticmethod
    def check_coverage(bible, scenes):
        """Check if all selected assets are used.

        Returns: {
            characters: {total, used, unused_names},
            costumes: {total, used, unused_names},
            environments: {total, used, unused_names},
            warnings: list of strings
        }
        """
        warnings = []

        # Collect used asset IDs from all scenes
        used_char_ids = set()
        used_costume_ids = set()
        used_env_ids = set()

        for scene in scenes:
            for c in scene.get("characters", []):
                if isinstance(c, dict):
                    used_char_ids.add(c.get("id", ""))
            for c in scene.get("costumes", []):
                if isinstance(c, dict):
                    used_costume_ids.add(c.get("id", ""))
            for e in scene.get("environments", []):
                if isinstance(e, dict):
                    used_env_ids.add(e.get("id", ""))

        # Characters
        all_char_ids = {_safe_id(c) for c in bible.characters}
        unused_chars = all_char_ids - used_char_ids
        unused_char_names = [
            _safe_name(c) for c in bible.characters if _safe_id(c) in unused_chars
        ]
        if unused_char_names:
            warnings.append(f"Characters not used: {', '.join(unused_char_names)}")

        # Costumes
        all_costume_ids = {_safe_id(c) for c in bible.costumes}
        unused_costumes = all_costume_ids - used_costume_ids
        unused_costume_names = [
            _safe_name(c) for c in bible.costumes if _safe_id(c) in unused_costumes
        ]
        if unused_costume_names:
            warnings.append(f"Costumes not used: {', '.join(unused_costume_names)}")

        # Environments
        all_env_ids = {_safe_id(e) for e in bible.environments}
        unused_envs = all_env_ids - used_env_ids
        unused_env_names = [
            _safe_name(e) for e in bible.environments if _safe_id(e) in unused_envs
        ]
        if unused_env_names:
            warnings.append(f"Environments not used: {', '.join(unused_env_names)}")

        return {
            "characters": {
                "total": len(bible.characters),
                "used": len(all_char_ids - unused_chars),
                "unused_names": unused_char_names,
            },
            "costumes": {
                "total": len(bible.costumes),
                "used": len(all_costume_ids - unused_costumes),
                "unused_names": unused_costume_names,
            },
            "environments": {
                "total": len(bible.environments),
                "used": len(all_env_ids - unused_envs),
                "unused_names": unused_env_names,
            },
            "warnings": warnings,
        }


# ──────────────────────────── PlanValidator ────────────────────────────

class PlanValidator:
    """Validates plan quality."""

    @staticmethod
    def validate(bible, scenes):
        """Validate the plan for quality.

        Returns: {
            valid: bool,
            score: float (0-1),
            issues: list of {severity, message, scene_index}
        }
        """
        issues = []
        total_checks = 0
        passed_checks = 0

        # 1. No two adjacent scenes have same purpose
        total_checks += 1
        dup_purposes = False
        for i in range(1, len(scenes)):
            if scenes[i].get("purpose") == scenes[i - 1].get("purpose"):
                issues.append({
                    "severity": "warning",
                    "message": f"Scenes {i} and {i + 1} have identical purpose",
                    "scene_index": i,
                })
                dup_purposes = True
        if not dup_purposes:
            passed_checks += 1

        # 2. All assets used
        total_checks += 1
        coverage = AssetCoverage.check_coverage(bible, scenes)
        if coverage["warnings"]:
            for w in coverage["warnings"]:
                issues.append({"severity": "info", "message": w, "scene_index": None})
        else:
            passed_checks += 1

        # 3. Each scene has meaningful delta
        total_checks += 1
        empty_deltas = 0
        for i, s in enumerate(scenes):
            if not s.get("delta"):
                issues.append({
                    "severity": "warning",
                    "message": f"Scene {i + 1} has no delta defined",
                    "scene_index": i,
                })
                empty_deltas += 1
        if empty_deltas == 0:
            passed_checks += 1

        # 4. End state differs from start
        total_checks += 1
        if len(scenes) >= 2:
            first_em = scenes[0].get("emotional_shift", {}).get("to", "")
            last_em = scenes[-1].get("emotional_shift", {}).get("to", "")
            if first_em != last_em:
                passed_checks += 1
            else:
                issues.append({
                    "severity": "warning",
                    "message": "Emotional state doesn't change from start to end",
                    "scene_index": len(scenes) - 1,
                })
        else:
            passed_checks += 1

        # 5. Emotional progression exists (not all same emotion)
        total_checks += 1
        emotions = set()
        for s in scenes:
            emotions.add(s.get("emotional_shift", {}).get("to", "neutral"))
        if len(emotions) >= min(3, len(scenes)):
            passed_checks += 1
        else:
            issues.append({
                "severity": "warning",
                "message": f"Limited emotional variety — only {len(emotions)} distinct emotions",
                "scene_index": None,
            })

        # 6. Escalation curve present (not flat)
        total_checks += 1
        energies = [s.get("energy", 0.5) for s in scenes]
        if len(energies) >= 3:
            has_rise = any(energies[i] < energies[i + 1] for i in range(len(energies) - 1))
            has_fall = any(energies[i] > energies[i + 1] for i in range(len(energies) - 1))
            if has_rise and has_fall:
                passed_checks += 1
            else:
                issues.append({
                    "severity": "warning",
                    "message": "Escalation curve is monotonic — no rise and fall",
                    "scene_index": None,
                })
        else:
            passed_checks += 1

        # 7. Scene prompts not too short
        total_checks += 1
        short_prompts = 0
        for i, s in enumerate(scenes):
            prompt = s.get("shot_prompt", s.get("prompt", ""))
            if len(prompt) < 50:
                issues.append({
                    "severity": "info",
                    "message": f"Scene {i + 1} has a very short prompt ({len(prompt)} chars)",
                    "scene_index": i,
                })
                short_prompts += 1
        if short_prompts == 0:
            passed_checks += 1

        # 8. Beat variety
        total_checks += 1
        beat_types = set(s.get("beat_type", "") for s in scenes)
        if len(beat_types) >= min(3, len(scenes)):
            passed_checks += 1
        else:
            issues.append({
                "severity": "warning",
                "message": f"Limited beat variety — only {len(beat_types)} types used",
                "scene_index": None,
            })

        score = passed_checks / max(total_checks, 1)
        return {
            "valid": len([i for i in issues if i["severity"] == "error"]) == 0,
            "score": round(score, 2),
            "issues": issues,
        }


# ──────────────────────────── SceneRegenerator ────────────────────────────

class SceneRegenerator:
    """Regenerate scenes while respecting locks and continuity."""

    @staticmethod
    def regenerate_field(scene, field, bible, prev_scene=None, next_scene=None):
        """Regenerate a single field of a scene while respecting locks."""
        locks = scene.get("locks", {})
        if locks.get(field):
            return scene.get(field)  # Locked, don't change

        beat_type = scene.get("beat_type", "discovery")

        if field == "camera_direction":
            options = CAMERA_DIRECTIONS.get(beat_type, ["medium shot"])
            import random
            return random.choice(options)
        elif field == "lighting_direction":
            return LIGHTING_DIRECTIONS.get(beat_type, "natural lighting")
        elif field == "color_direction":
            return COLOR_DIRECTIONS.get(beat_type, "natural palette")
        elif field == "motion_direction":
            return MOTION_DIRECTIONS.get(beat_type, "moderate movement")
        elif field in ("shot_prompt", "prompt"):
            return PromptBuilder.build_shot_prompt(scene, bible, prev_scene)
        elif field == "action":
            # Rebuild action from beat info
            beat = {
                "beat_type": beat_type,
                "required_assets": {
                    "characters": scene.get("characters", []),
                    "environments": scene.get("environments", []),
                },
            }
            return SceneBuilder._build_action(beat, bible, scene.get("order", 0), 1)
        else:
            return scene.get(field)

    @staticmethod
    def regenerate_scene(scene_index, scenes, bible, locks=None):
        """Regenerate one scene while preserving locked fields and maintaining continuity."""
        if scene_index < 0 or scene_index >= len(scenes):
            return None

        old_scene = scenes[scene_index]
        effective_locks = locks or old_scene.get("locks", {})

        # Get context
        prev_scene = scenes[scene_index - 1] if scene_index > 0 else None
        next_scene = scenes[scene_index + 1] if scene_index < len(scenes) - 1 else None

        # Regenerate unlocked fields
        regenerable_fields = [
            "camera_direction", "lighting_direction", "color_direction",
            "motion_direction", "action", "summary",
        ]
        for field in regenerable_fields:
            if not effective_locks.get(field):
                old_scene[field] = SceneRegenerator.regenerate_field(
                    old_scene, field, bible, prev_scene, next_scene
                )

        # Always rebuild prompt from current state (unless prompt itself is locked)
        if not effective_locks.get("shot_prompt") and not effective_locks.get("prompt"):
            old_scene["shot_prompt"] = PromptBuilder.build_shot_prompt(old_scene, bible, prev_scene)
            old_scene["prompt"] = old_scene["shot_prompt"]

        old_scene["status"] = "draft"
        return old_scene

    @staticmethod
    def regenerate_downstream(from_index, scenes, bible):
        """Regenerate scene at from_index and all following scenes, respecting locks."""
        for i in range(from_index, len(scenes)):
            SceneRegenerator.regenerate_scene(i, scenes, bible)
        return scenes[from_index:]

    @staticmethod
    def regenerate_all(bible, beats, audio_sections=None):
        """Full regeneration from scratch."""
        return SceneBuilder.build_scenes(bible, beats, audio_sections)


# ──────────────────────────── Top-level API ────────────────────────────

MOVIE_PLAN_PATH = None  # Set by server.py


def create_movie_plan(style, lyrics, storyline, world_setting, universal_prompt,
                      characters, costumes, environments, engine, preset,
                      audio_sections=None, num_scenes=None, output_dir=None,
                      project_mode="music_video", film_runtime=60, film_scene_count=5,
                      film_pacing="medium", film_climax_position="late",
                      film_tension_curve="exponential", film_ending_type="bittersweet"):
    """Top-level function: create a full movie plan from inputs.

    Returns: {ok, bible, beats, scenes, coverage, validation}
    """
    is_film = (project_mode != "music_video")

    # Build bible
    bible = MovieBible.from_inputs(
        style=style,
        lyrics=lyrics,
        storyline=storyline,
        world_setting=world_setting,
        universal_prompt=universal_prompt,
        characters=characters,
        costumes=costumes,
        environments=environments,
        engine=engine,
        preset=preset,
        project_mode=project_mode,
        film_runtime=film_runtime,
        film_scene_count=film_scene_count,
        film_pacing=film_pacing,
        film_climax_position=film_climax_position,
        film_tension_curve=film_tension_curve,
        film_ending_type=film_ending_type,
    )

    # Determine scene count
    if num_scenes:
        scene_count = num_scenes
    elif is_film:
        scene_count = film_scene_count
    elif audio_sections:
        scene_count = len(audio_sections)
    else:
        scene_count = 12  # sensible default

    # Plan beats
    beats = BeatPlanner.plan_beats(bible, scene_count, audio_sections, project_mode=project_mode)

    # Build scenes (no audio sections in film mode)
    effective_audio = audio_sections if not is_film else None
    scenes = SceneBuilder.build_scenes(bible, beats, effective_audio)

    # Check coverage
    coverage = AssetCoverage.check_coverage(bible, scenes)

    # Validate
    validation = PlanValidator.validate(bible, scenes)

    # Build result
    result = {
        "ok": True,
        "bible": bible.to_dict(),
        "beats": beats,
        "scenes": scenes,
        "coverage": coverage,
        "validation": validation,
        "created_at": _now(),
        "version": 1,
    }

    # Save to disk
    if output_dir:
        plan_path = os.path.join(output_dir, "movie_plan.json")
        os.makedirs(output_dir, exist_ok=True)
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    return result


def load_movie_plan(output_dir):
    """Load the saved movie plan from disk."""
    plan_path = os.path.join(output_dir, "movie_plan.json")
    if os.path.isfile(plan_path):
        with open(plan_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


_movie_plan_file_lock = threading.Lock()

def save_movie_plan(plan, output_dir):
    """Save the movie plan to disk."""
    plan_path = os.path.join(output_dir, "movie_plan.json")
    os.makedirs(output_dir, exist_ok=True)
    with _movie_plan_file_lock:
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)


def rebuild_bible_from_plan(plan):
    """Reconstruct a MovieBible object from a saved plan dict."""
    bible_data = plan.get("bible", {})
    bible = MovieBible()
    bible.concept = bible_data.get("concept", "")
    bible.theme = bible_data.get("theme", "")
    bible.story_arc = bible_data.get("story_arc", "")
    bible.emotional_arc = bible_data.get("emotional_arc", [])
    bible.visual_arc = bible_data.get("visual_arc", [])
    bible.world_rules = bible_data.get("world_rules", "")
    bible.characters = bible_data.get("characters", [])
    bible.costumes = bible_data.get("costumes", [])
    bible.environments = bible_data.get("environments", [])
    bible.ending_state = bible_data.get("ending_state", "")
    bible.progression_strategy = bible_data.get("progression_strategy", "")
    bible.style = bible_data.get("style", "")
    bible.world_setting = bible_data.get("world_setting", "")
    bible.universal_prompt = bible_data.get("universal_prompt", "")
    # Film mode params
    bible.project_mode = bible_data.get("project_mode", "music_video")
    bible.film_runtime = bible_data.get("film_runtime", 60)
    bible.film_scene_count = bible_data.get("film_scene_count", 5)
    bible.film_pacing = bible_data.get("film_pacing", "medium")
    bible.film_climax_position = bible_data.get("film_climax_position", "late")
    bible.film_tension_curve = bible_data.get("film_tension_curve", "exponential")
    bible.film_ending_type = bible_data.get("film_ending_type", "bittersweet")
    return bible
