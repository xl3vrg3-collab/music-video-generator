"""
Shot Style Library — Comprehensive preset system for cinematic shot creation.

Every preset maps to prompt language for the Shot Prompt Engine.
Presets are selectable via UI dropdowns/chips and used by AI Director Mode.
Extensible: users can add custom presets via the API.
"""

import json
import os

LIBRARY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "cinematic_engine", "shot_style_library.json"
)

# ---- Built-in Presets ----

FRAMING = {
    "Wide Establishing": "wide establishing shot showing full environment and subject context",
    "Extreme Wide": "extreme wide shot, vast landscape, subject small in frame",
    "Medium Shot": "medium shot from waist up, balanced framing",
    "Medium Close-Up": "medium close-up from chest up, intimate but contextual",
    "Close-Up": "close-up on face, filling frame with emotion and detail",
    "Extreme Close-Up": "extreme close-up on eyes or specific detail, macro-level intimacy",
    "Over-the-Shoulder": "over-the-shoulder shot, depth between two subjects",
    "POV Shot": "point-of-view shot, camera as the character's eyes",
    "Insert Shot": "insert shot, tight detail of object or action",
    "Two Shot": "two shot framing both subjects equally",
    "Three Shot": "three shot with balanced positioning of three subjects",
    "Tracking Profile": "tracking profile shot, side view moving with subject",
    "Silhouette Shot": "silhouette shot, subject backlit as dark shape against light",
    "Full Body Shot": "full body shot, head to toe framing with environment",
    "Knee-Up Shot": "knee-up shot, American shot, from knees to head",
    "Waist-Up Shot": "waist-up shot, medium framing with upper body emphasis",
    "Head-and-Shoulders": "head-and-shoulders framing, news anchor style tight",
    "Profile Close-Up": "profile close-up, side view of face showing jawline and silhouette",
    "Rear View Shot": "rear view shot, subject facing away from camera, mystery and distance",
    "Foreground Framed Shot": "foreground framed shot, subject seen through objects in foreground",
}

MOVEMENT = {
    "Static": "locked-off static camera, no movement, still frame",
    "Slow Dolly In": "slow dolly in, gradually closing distance, building tension",
    "Slow Dolly Out": "slow dolly out, gradually revealing environment, expanding scope",
    "Push In": "push in toward subject, intensifying focus and urgency",
    "Pull Back": "pull back from subject, revealing context and environment",
    "Tracking Left": "tracking left, smooth lateral movement following or revealing",
    "Tracking Right": "tracking right, smooth lateral movement",
    "Tracking Forward": "tracking forward, moving through space toward destination",
    "Tracking Backward": "tracking backward, subject approaching camera",
    "Handheld": "handheld camera, natural subtle movement, documentary feel",
    "Steadicam": "steadicam smooth floating movement, elegant and weightless",
    "Crane Up": "crane up, ascending to reveal scope and scale",
    "Crane Down": "crane down, descending from height to intimate level",
    "Jib Sweep": "jib sweep, arcing movement across scene",
    "Orbit Left": "orbit left around subject, circling counterclockwise",
    "Orbit Right": "orbit right around subject, circling clockwise",
    "Whip Pan Left": "whip pan left, fast snap movement creating motion blur",
    "Whip Pan Right": "whip pan right, fast snap movement creating motion blur",
    "Snap Zoom In": "snap zoom in, sudden aggressive zoom toward subject",
    "Snap Zoom Out": "snap zoom out, sudden pull revealing environment",
    "Rolling Camera Tilt": "rolling camera tilt, disorienting rotation on lens axis",
    "Drift Float Movement": "drifting float movement, dreamlike weightless camera",
    "Arc Around Subject": "arc around subject, sweeping 180-degree movement",
    "Follow Behind": "follow behind subject, tracking from rear",
    "Lead Ahead Tracking": "lead ahead of subject, camera moving backward as subject advances",
}

LENS = {
    "14mm Ultra Wide": "14mm ultra wide lens, extreme perspective distortion, vast field of view",
    "18mm Ultra Wide": "18mm ultra wide lens, expansive view with slight distortion",
    "24mm Wide": "24mm wide lens, cinematic wide establishing look",
    "28mm Wide": "28mm wide lens, natural wide angle, documentary feel",
    "35mm Natural": "35mm lens, natural field of view closest to human eye",
    "40mm Natural": "40mm lens, natural standard perspective",
    "50mm Standard": "50mm standard lens, classic portrait perspective, no distortion",
    "65mm Cinematic": "65mm cinematic lens, IMAX-style depth and clarity",
    "85mm Portrait": "85mm portrait lens, beautiful bokeh, subject isolation",
    "100mm Portrait": "100mm portrait lens, compressed background, strong subject separation",
    "135mm Telephoto": "135mm telephoto lens, compressed depth, voyeuristic distance",
    "200mm Telephoto": "200mm telephoto, extreme compression, flat depth planes",
    "Macro Lens": "macro lens, extreme close-up detail, razor-thin depth of field",
    "Fisheye Lens": "fisheye lens, barrel distortion, surreal perspective",
    "Anamorphic Wide": "anamorphic wide lens, horizontal lens flares, cinematic 2.39:1 feel",
    "Anamorphic Standard": "anamorphic lens, oval bokeh, characteristic flares, widescreen",
    "Vintage Soft Lens": "vintage soft lens, gentle halation, dreamy edges, imperfect beauty",
    "Sharp Digital Lens": "sharp digital lens, clinical precision, razor detail throughout",
}

LIGHTING = {
    "Soft Cinematic": "soft cinematic lighting, diffused key light, gentle shadows, flattering",
    "High Contrast Noir": "high contrast noir lighting, deep blacks, sharp shadows, single hard source",
    "Neon Cyberpunk": "neon-lit environment with strong magenta and cyan highlights, reflective surfaces",
    "Golden Hour": "golden hour warm light, long shadows, amber glow, magic hour beauty",
    "Blue Hour": "blue hour cool twilight, deep blue ambient, city lights emerging",
    "Harsh Overhead": "harsh overhead lighting, strong downward shadows under eyes and chin",
    "Backlit Silhouette": "backlit silhouette, subject dark against bright background, rim light edge",
    "Volumetric Fog Lighting": "volumetric fog lighting, visible light beams cutting through haze",
    "Practical Lighting": "practical lighting from in-scene sources, lamps, screens, signs",
    "Flickering Industrial": "flickering industrial lighting, unstable fluorescent, tension atmosphere",
    "Low Key Dramatic": "low key dramatic lighting, mostly shadow with selective illumination",
    "High Key Bright": "high key bright lighting, minimal shadows, clean airy feel",
    "Window Side Lighting": "window side lighting, soft natural directional light, Vermeer-like",
    "Top Down Spotlight": "top down spotlight, pool of light, theatrical isolation",
    "Underlighting Horror": "underlighting from below, horror aesthetic, unnatural shadows cast upward",
    "Mixed Color Lighting": "mixed color lighting, multiple colored sources creating complex mood",
    "Fluorescent Office Lighting": "flat fluorescent office lighting, clinical, lifeless green cast",
    "Candlelight Warm": "candlelight warm lighting, flickering orange, intimate, period feel",
    "Streetlight Sodium Glow": "streetlight sodium glow, amber-orange cast, urban night atmosphere",
    "Police Light Flash": "police light flash, alternating red and blue, chaotic urgency",
    "Firelight Flicker": "firelight flicker, dancing warm shadows, primal atmosphere",
    "Moonlight Blue": "moonlight blue, cool pale illumination, night exterior, silver tones",
    "Stage Lighting Performance": "stage lighting, dramatic colored spots, performance energy, concert feel",
    "Shadow Pattern Lighting": "shadow pattern lighting, light through blinds or grids, striped shadows",
}

PERFORMANCE = {
    "Calm Introspective": "calm introspective, still presence, thoughtful gaze, minimal movement",
    "Emotional Vulnerable": "emotionally vulnerable, soft guard, trembling subtlety, exposed feeling",
    "Aggressive Intense": "aggressive intensity, hard eyes, forward lean, confrontational energy",
    "Confident Controlled": "confident and controlled, commanding presence, measured gestures",
    "Erratic Chaotic": "erratic chaotic movement, unpredictable, frantic energy, instability",
    "Dreamlike Slow Motion": "dreamlike slow motion, suspended movement, floating ethereal quality",
    "Detached Cold": "detached cold affect, hollow gaze, mechanical movement, emotional shutdown",
    "Romantic Soft": "romantic soft presence, warm gaze, gentle movements, tender vulnerability",
    "Fearful Anxious": "fearful anxious energy, darting eyes, tense shoulders, ready to flee",
    "Determined Focused": "determined focused intensity, locked gaze, unstoppable forward momentum",
    "Reckless Energy": "reckless abandon, wild movement, no regard for consequences",
    "Playful Light": "playful light energy, easy smile, bouncing movement, joy",
    "Melancholic Heavy": "melancholic weight, heavy shoulders, distant eyes, carrying unseen burden",
    "Angry Explosive": "angry explosive power, clenched everything, barely contained rage",
    "Stoic Minimal": "stoic minimal expression, stone face, immovable, power through stillness",
    "Euphoric High Energy": "euphoric high energy, arms wide, face skyward, transcendent joy",
    "Suspicious Alert": "suspicious alertness, scanning eyes, guarded posture, trust nothing",
    "Seductive Controlled": "seductive controlled presence, deliberate eye contact, magnetic pull",
    "Broken Exhausted": "broken exhausted collapse, depleted, barely standing, end of strength",
    "Heroic Rising": "heroic rising moment, gathering strength, standing tall against odds",
}

ATMOSPHERE = {
    "Rain": "rain falling, wet surfaces, water droplets visible",
    "Heavy Rain": "heavy rain downpour, sheets of water, splashing ground, low visibility",
    "Drizzle": "light drizzle, gentle mist, glistening surfaces",
    "Fog": "fog, atmospheric haze, reduced visibility, mysterious depth",
    "Dense Fog": "dense fog, barely visible beyond arm's length, isolation",
    "Smoke": "smoke wisps, atmospheric haze, diffused light",
    "Thick Smoke": "thick smoke, billowing clouds, obscured vision, emergency feel",
    "Dust": "dust particles floating in light, aged atmosphere",
    "Sandstorm": "sandstorm, abrasive wind, limited visibility, harsh environment",
    "Snow": "snowfall, white ground, cold atmosphere, muffled world",
    "Light Snow": "light snow, gentle flurries, peaceful winter air",
    "Blizzard": "blizzard whiteout, driving snow, survival conditions",
    "Neon Reflections": "neon reflections on wet surfaces, urban night, color pools on ground",
    "Wet Ground Reflections": "wet ground reflections, mirror-like pavement, doubled world",
    "Wind Movement": "wind movement in hair and clothes, dynamic air",
    "Strong Wind": "strong wind, clothes and debris whipping, resistance in movement",
    "Fire Embers": "floating fire embers, orange particles, heat distortion",
    "Floating Ash": "floating ash particles, grey descent, aftermath atmosphere",
    "Floating Particles": "floating particles in light beams, dust motes, ethereal",
    "Glitch Artifacts": "digital glitch artifacts, scan lines, fragmented reality",
    "Holographic Distortion": "holographic distortion, prismatic light scatter, sci-fi atmosphere",
    "Steam Vents": "steam vents, rising vapor, industrial atmosphere, obscuring bursts",
    "Industrial Sparks": "industrial sparks, welding particles, factory energy, dangerous beauty",
}

COLOR_GRADING = {
    "Teal and Orange": "teal and orange color grading, complementary contrast, cinematic blockbuster look",
    "Cyberpunk Neon": "cyberpunk neon color grading, saturated magenta and cyan, electric urban",
    "Desaturated Grit": "desaturated gritty color grading, pulled color, raw documentary feel",
    "Warm Vintage": "warm vintage color grading, amber tones, faded edges, nostalgic film look",
    "Cold Blue Steel": "cold blue steel color grading, steely cyan shadows, clinical and stark",
    "High Contrast Black and White": "high contrast black and white, deep blacks, bright whites, dramatic monochrome",
    "Muted Pastels": "muted pastel color grading, soft desaturated hues, dreamy quality",
    "Deep Shadows Contrast": "deep shadows with high contrast, rich blacks, selective highlights",
    "Soft Film Fade": "soft film fade, lifted blacks, reduced contrast, gentle nostalgic",
    "Bleach Bypass": "bleach bypass look, desaturated with high contrast, gritty silver",
    "Sepia Tone": "sepia tone, warm brown monochrome, timeless historical feel",
    "Vivid Saturation": "vivid saturated colors, punchy and bold, eye-catching vibrancy",
    "Cool Monochrome": "cool monochrome, blue-grey palette, muted and clinical",
    "Warm Skin Tone Boost": "warm skin tone boost, flattering amber warmth, beauty lighting grade",
    "Dark Purple Tint": "dark purple tint, violet shadows, mysterious and otherworldly",
    "Green Matrix Tint": "green tint, matrix-style digital cast, digital world aesthetic",
    "Sunset Orange Glow": "sunset orange glow grading, golden to deep orange, warm enveloping light",
    "Neon Magenta Blue": "neon magenta and blue grading, electric nightlife colors",
    "Natural Film Look": "natural film look, subtle grain, accurate colors, organic texture",
    "High Exposure Dream": "high exposure dream grading, blown highlights, ethereal overexposure",
}

DIRECTOR_STYLES = {
    "Denis Villeneuve": "Denis Villeneuve style, vast scale, minimal dialogue, atmospheric dread, geometric compositions",
    "Christopher Nolan": "Christopher Nolan style, IMAX grandeur, practical effects, non-linear tension, steady push-ins",
    "David Fincher": "David Fincher style, meticulous framing, slow creeping camera, desaturated palette, controlled dread",
    "Wong Kar-wai": "Wong Kar-wai style, neon-soaked, step-printed motion, romantic melancholy, saturated color",
    "Gaspar Noe": "Gaspar Noe style, long takes, aggressive camera, neon lighting, visceral disorientation",
    "Music Video Hypercut": "music video hypercut style, rapid cuts, dynamic angles, beat-synced editing, high energy",
    "Ridley Scott": "Ridley Scott style, epic scale, atmospheric smoke, practical textures, painterly composition",
    "Quentin Tarantino": "Tarantino style, low angles, trunk shots, long dialogue takes, pop culture energy",
    "Stanley Kubrick": "Kubrick style, one-point perspective symmetry, cold precision, unsettling stillness",
    "Martin Scorsese": "Scorsese style, steadicam tracking, long take energy, kinetic editing, street authenticity",
    "Zack Snyder": "Zack Snyder style, speed ramping, hyper-stylized slow motion, high contrast, heroic framing",
    "Nicolas Winding Refn": "Nicolas Winding Refn style, neon noir, extreme stillness, sudden violence, synth-wave mood",
    "Spike Jonze": "Spike Jonze style, intimate handheld, natural light, quiet surrealism, emotional truth",
    "Terrence Malick": "Terrence Malick style, golden hour, whispered narration, nature intercuts, spiritual drift",
    "Park Chan-wook": "Park Chan-wook style, precise symmetry, baroque color, elegant violence, layered frames",
    "Michael Bay": "Michael Bay style, epic explosions, golden hour, spinning hero shots, maximum spectacle",
    "Safdie Brothers": "Safdie Brothers style, claustrophobic handheld, anxiety pacing, gritty telephoto, urban chaos",
    "Ari Aster": "Ari Aster style, wide static frames, slow dread, daylight horror, symmetrical compositions",
    "Jordan Peele": "Jordan Peele style, clean compositions, social subtext, unsettling normalcy, precise reveals",
    "Experimental Avant-Garde": "experimental avant-garde style, non-traditional framing, abstract visuals, rule-breaking",
}

CAMERA_ANGLES = {
    "Eye Level": "eye level angle, neutral perspective, direct engagement",
    "Low Angle": "low angle looking up, subject appears powerful and dominant",
    "High Angle": "high angle looking down, subject appears small or vulnerable",
    "Birds Eye View": "bird's eye view, directly overhead, map-like perspective",
    "Worms Eye View": "worm's eye view, extreme low from ground level looking straight up",
    "Dutch Tilt": "dutch tilt, canted angle, disorientation and unease",
    "Overhead Top Down": "overhead top-down, flat perspective, geometric patterns",
    "Ground Level": "ground level, camera on floor, dramatic low perspective",
    "Shoulder Height": "shoulder height, slightly below eye level, natural conversational",
    "Extreme Low Angle": "extreme low angle, heroic perspective, towering over camera",
    "Extreme High Angle": "extreme high angle, God's eye diminishing perspective",
    "Canted Frame": "canted frame, tilted reality, psychological instability",
    "Diagonal Composition": "diagonal composition, dynamic tension through angled framing",
    "Centered Symmetry": "centered symmetry, perfect balance, formal precision",
    "Off-Center Rule of Thirds": "off-center rule of thirds, balanced asymmetry, natural eye flow",
}

COMPOSITION = {
    "Centered Symmetry": "centered symmetrical composition, balanced formal framing",
    "Rule of Thirds": "rule of thirds composition, subject at intersection points",
    "Leading Lines": "leading lines composition, environmental lines guiding eye to subject",
    "Frame Within Frame": "frame within frame, doorways or windows creating inner border",
    "Foreground Framing": "foreground framing, objects in front creating depth and context",
    "Negative Space": "negative space composition, subject small in vast empty area",
    "Layered Depth": "layered depth composition, foreground mid and background all active",
    "Silhouette Composition": "silhouette composition, dark shape against light, graphic simplicity",
    "Reflections Composition": "reflections composition, mirrored surfaces doubling the image",
    "Mirrored Composition": "mirrored symmetrical composition, left and right balanced",
    "Asymmetrical Balance": "asymmetrical balance, uneven but visually weighted composition",
    "Crowded Frame": "crowded frame, filled with detail and subjects, overwhelming density",
    "Minimalist Frame": "minimalist frame, stripped bare, single focus, clean and stark",
    "Diagonal Lines": "diagonal lines composition, dynamic tension and movement through frame",
    "Depth Through Objects": "depth through objects, shooting past foreground elements to subject",
    "Obstructed View": "obstructed view, partial visibility, peering through barriers",
    "Peeking Perspective": "peeking perspective, voyeuristic, hidden viewpoint, surveillance feel",
}

# ---- All categories ----

BUILTIN_LIBRARY = {
    "framing": FRAMING,
    "movement": MOVEMENT,
    "lens": LENS,
    "lighting": LIGHTING,
    "performance": PERFORMANCE,
    "atmosphere": ATMOSPHERE,
    "color_grading": COLOR_GRADING,
    "director_styles": DIRECTOR_STYLES,
    "camera_angles": CAMERA_ANGLES,
    "composition": COMPOSITION,
}


# ---- Custom presets (user-extensible) ----

def _load_custom() -> dict:
    if os.path.isfile(LIBRARY_PATH):
        try:
            with open(LIBRARY_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_custom(custom: dict):
    os.makedirs(os.path.dirname(LIBRARY_PATH), exist_ok=True)
    with open(LIBRARY_PATH, "w") as f:
        json.dump(custom, f, indent=2)


def get_full_library() -> dict:
    """Get merged library: built-in + custom presets."""
    merged = {}
    for cat, presets in BUILTIN_LIBRARY.items():
        merged[cat] = dict(presets)
    # Merge custom on top
    custom = _load_custom()
    for cat, presets in custom.items():
        if cat not in merged:
            merged[cat] = {}
        merged[cat].update(presets)
    return merged


def add_custom_preset(category: str, name: str, prompt_text: str) -> dict:
    """Add a user custom preset to a category."""
    custom = _load_custom()
    if category not in custom:
        custom[category] = {}
    custom[category][name] = prompt_text
    _save_custom(custom)
    return custom


def remove_custom_preset(category: str, name: str) -> dict:
    """Remove a user custom preset."""
    custom = _load_custom()
    if category in custom and name in custom[category]:
        del custom[category][name]
        _save_custom(custom)
    return custom


def resolve_presets(selections: dict) -> str:
    """
    Resolve a dict of {category: preset_name} into combined prompt language.

    Args:
        selections: {"framing": "Close-Up", "lighting": "Neon Cyberpunk", ...}

    Returns:
        Combined prompt string from all selected presets.
    """
    library = get_full_library()
    parts = []
    for cat, name in selections.items():
        if isinstance(name, list):
            # Multi-select (atmosphere, etc.)
            for n in name:
                presets = library.get(cat, {})
                if n in presets:
                    parts.append(presets[n])
        else:
            presets = library.get(cat, {})
            if name in presets:
                parts.append(presets[name])
    return ", ".join(parts)
