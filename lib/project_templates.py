"""Project templates for LUMN Studio.

Templates seed a new project with a genre-appropriate shot structure, default
settings, and prompt hints. They're intentionally lightweight — the user fills
in story/characters themselves, but the scaffolding removes the blank-page
problem.
"""

TEMPLATES = {
    "trailer": {
        "id": "trailer",
        "name": "Movie Trailer",
        "description": "Short teaser: hook → escalation → reveal → logo sting. 10-14 shots, mostly 3-5s each.",
        "duration_target_sec": 60,
        "shot_count_target": 12,
        "tone": "cinematic, high contrast, urgent",
        "default_tier": "v3_pro",
        "default_duration": 5,
        "beats": [
            {"id": "b1", "name": "Cold open",        "shots": 2, "tone": "mysterious, quiet"},
            {"id": "b2", "name": "Inciting image",   "shots": 2, "tone": "charged, loaded"},
            {"id": "b3", "name": "Escalation",       "shots": 4, "tone": "rising stakes"},
            {"id": "b4", "name": "Mid-trailer drop", "shots": 1, "tone": "silent beat"},
            {"id": "b5", "name": "Climax montage",   "shots": 2, "tone": "peak intensity"},
            {"id": "b6", "name": "Title card",       "shots": 1, "tone": "logo sting"},
        ],
    },
    "short_film": {
        "id": "short_film",
        "name": "Short Film",
        "description": "Three-act short: setup, confrontation, resolution. Variable shot lengths.",
        "duration_target_sec": 180,
        "shot_count_target": 24,
        "tone": "observational, character-driven",
        "default_tier": "v3_standard",
        "default_duration": 5,
        "beats": [
            {"id": "a1", "name": "Opening image",      "shots": 2, "tone": "establishing"},
            {"id": "a2", "name": "Character intro",    "shots": 4, "tone": "intimate"},
            {"id": "a3", "name": "Inciting incident",  "shots": 3, "tone": "disruption"},
            {"id": "b1", "name": "Rising action",      "shots": 6, "tone": "building tension"},
            {"id": "b2", "name": "Midpoint reversal",  "shots": 3, "tone": "shift"},
            {"id": "c1", "name": "Climax",             "shots": 4, "tone": "peak"},
            {"id": "c2", "name": "Resolution",         "shots": 2, "tone": "aftermath"},
        ],
    },
    "music_video": {
        "id": "music_video",
        "name": "Music Video",
        "description": "Performance + narrative cutaways, synced to beats. 30-60 shots for a 3-4 min track.",
        "duration_target_sec": 210,
        "shot_count_target": 40,
        "tone": "rhythmic, stylized, color-graded",
        "default_tier": "v3_pro",
        "default_duration": 5,
        "beats": [
            {"id": "intro",    "name": "Intro bars",   "shots": 4,  "tone": "world-building"},
            {"id": "verse1",   "name": "Verse 1",      "shots": 8,  "tone": "narrative cutaways"},
            {"id": "chorus1",  "name": "Chorus 1",     "shots": 6,  "tone": "performance, movement"},
            {"id": "verse2",   "name": "Verse 2",      "shots": 8,  "tone": "escalation"},
            {"id": "chorus2",  "name": "Chorus 2",     "shots": 6,  "tone": "explosive"},
            {"id": "bridge",   "name": "Bridge",       "shots": 4,  "tone": "breakdown, intimate"},
            {"id": "outro",    "name": "Final chorus", "shots": 4,  "tone": "catharsis"},
        ],
    },
    "teaser": {
        "id": "teaser",
        "name": "30-Second Teaser",
        "description": "Ultra-short marketing cut: logo → hook shot → title card. 5-8 shots.",
        "duration_target_sec": 30,
        "shot_count_target": 6,
        "tone": "bold, graphic, immediate",
        "default_tier": "v3_pro",
        "default_duration": 5,
        "beats": [
            {"id": "hook",    "name": "Hook image",    "shots": 2, "tone": "arresting"},
            {"id": "promise", "name": "What you get",  "shots": 2, "tone": "intriguing"},
            {"id": "sting",   "name": "Title + date",  "shots": 2, "tone": "confident"},
        ],
    },
}


def list_templates():
    """Return summary list for the picker UI."""
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "duration_target_sec": t["duration_target_sec"],
            "shot_count_target": t["shot_count_target"],
            "tone": t["tone"],
        }
        for t in TEMPLATES.values()
    ]


def get_template(template_id: str) -> dict | None:
    return TEMPLATES.get(template_id)


def instantiate_shots(template_id: str) -> list[dict]:
    """Expand a template's beats into concrete shot stubs."""
    tpl = TEMPLATES.get(template_id)
    if not tpl:
        return []
    shots = []
    idx = 1
    default_dur = tpl.get("default_duration", 5)
    default_tier = tpl.get("default_tier", "v3_standard")
    for beat in tpl.get("beats", []):
        for _ in range(beat.get("shots", 1)):
            shots.append({
                "shot_id": f"shot_{idx:03d}",
                "beat_id": beat["id"],
                "beat_name": beat["name"],
                "title": f"{beat['name']} — shot {idx}",
                "tone": beat.get("tone", ""),
                "duration": default_dur,
                "tier": default_tier,
                "prompt": "",
                "status": "stub",
            })
            idx += 1
    return shots
