"""
Opus Director — LUMN's story/scene/prompt generator.

Three entry points, all Opus-backed with the cached director bible:

    direct_story(...)        → Snyder-arc scenes from a brief + song timing
    direct_scene(...)        → hydrate a minimal scene with emotion/acting/eyeline
    direct_kling_prompt(...) → author the final Kling shot prompt

Every function:
  - pulls the director bible via project= (or profile_id=)
  - runs Opus with extended thinking on decision-heavy calls
  - returns strict JSON matching a documented schema
  - never restates the bible in output (the bible is cached system context)

Callers that want a simpler one-shot LLM (legacy flows) should continue to
use call_json / story_planner. Opus Director is the high-stakes path: main
protagonist's MV, full-length productions, any scene that has to carry
emotional weight.
"""
from __future__ import annotations

import json
from typing import Optional

from lib.claude_client import call_opus_json


# ───────────────────────────────────────────────────────────────────────────
# 1. STORY — brief → 7-beat scene plan
# ───────────────────────────────────────────────────────────────────────────

_STORY_SCHEMA_HINT = """\
Return strict JSON with this exact shape. A SCENE is a unified dramatic unit in ONE location with ONE performance arc — each scene is COVERED BY MULTIPLE SHOTS (like a real film). Default 2–4 shots per scene. The character's body pose, eyeline, wardrobe, lighting, and emotional temperature MUST flow continuously across every shot in the scene.

{
  "title": string,
  "logline": string,
  "protagonist": { "name": string, "internal_need": string, "external_want": string },
  "theme": string,
  "scenes": [
    {
      "id": string,                    // scene id — e.g. "1", "2", "3" (NOT shot-level)
      "beat_id": int,                  // maps to profile.beat_template.beats[].id
      "beat_name": string,             // e.g. "Inciting Incident"
      "location": string,              // must match an environment the user has provided
      "time_start": float,             // seconds from song start — scene opens
      "time_end": float,               // seconds from song start — scene closes
      "emotion": string,               // internal state driving the whole scene
      "dramatic_action": string,       // the ONE thing the character does in this scene
      "performance_arc": string,       // where the character starts emotionally → where they land
      "lyric_anchor": string,          // specific lyric phrase(s) the scene images, if any
      "continuity_anchors": {
        "lighting": string,            // direction + color — applies to every shot in scene
        "wardrobe": string,            // hoodie up/down, beads visible, etc.
        "weather": string,             // rain state, wind, particles
        "time_of_day": string,         // dusk, night, etc.
        "key_props": string,           // what the character interacts with across shots
        "eyeline_target": string       // where the character is looking through the scene
      },
      "shots": [
        {
          "id": string,                // e.g. "1a", "1b", "1c" — scene id + letter
          "shot_size": "wide" | "medium" | "close",
          "duration_s": float,
          "acting": string,            // specific verb from profile palette — grounded, not "floats/drifts"
          "micro_expression": string,  // face/eye beat for this specific shot
          "camera": string,            // ONE move only — static, push-in, pull-back, tilt, pan, orbit, tracking
          "continuity_in": string,     // what the prior shot hands off: body pose, eyeline, cut motivation ("TB's rightward gaze from 1a carries over — cut on head-turn")
          "continuity_out": string,    // what this shot hands to the next
          "transition_in": "hard_cut" | "match_cut" | "action_cut" | "eyeline_cut" | "smash_cut" | "dissolve",
          "purpose": string            // why this shot exists inside the scene
        }
      ]
    }
  ],
  "coverage_notes": [string]           // wides/inserts/reactions and the continuity anchors the editor must protect
}

SHOT-GRAMMAR RULES (enforce as you plan):
- Default coverage: master/establishing wide → medium/OTS → close/reaction. Reverse only when the scene opens on a reveal.
- The IDENTITY LOCK scene (opening 3–8s) may be a single medium/close held shot — identity comes BEFORE geography.
- Every shot's `continuity_in` must be concrete and motivated. Do not write "hard cut" unless the scene actually changes (location, time jump, violent smash).
- Eyeline across shots: if shot Na is looking screen-right, shot Nb must either maintain that vector OR rotate with a motivated camera move / character turn. Call out the screen-direction in `continuity_out`.
- The 180° line is SET by the first shot of the scene. Do not cross it without a motivated cue written into the shot's continuity notes.
- A wide shot WITHOUT a following medium/close is usually a failure — the audience needs to read the face within the beat.
- A close shot WITHOUT an establishing wide at some point in the scene leaves geography ambiguous — unless the scene deliberately opens in media res, budget an insert or a later wide in coverage_notes."""


def direct_story(brief: str,
                 duration_s: float,
                 project: Optional[str] = None,
                 profile_id: Optional[str] = None,
                 song_analysis: Optional[dict] = None,
                 environments: Optional[list[str]] = None,
                 thinking_budget: int = 6000) -> dict:
    """
    Produce a full Snyder-arc scene plan from a one-line brief.

    Args:
        brief:          user's one-line creative brief
        duration_s:     target production length in seconds
        project:        project slug — loads bible + style profile
        profile_id:     explicit profile override (usually leave None)
        song_analysis:  optional dict with bpm/sections/downbeats/lyrics
                        (see lib.song_timing output)
        environments:   names of available environment sets — scenes MUST
                        use these, don't invent locations the user can't render
        thinking_budget: Opus extended-thinking budget (tokens)
    """
    env_list = "\n".join(f"  - {e}" for e in (environments or [])) or "  (none provided — use 'default')"

    timing_ctx = ""
    if song_analysis:
        sec = song_analysis.get("sections") or []
        if sec:
            timing_ctx = "\nSONG SECTIONS (respect these boundaries when assigning scenes):\n"
            for s in sec[:16]:
                timing_ctx += f"  - {s.get('label','?')}: {s.get('start_s',0):.1f}s–{s.get('end_s',0):.1f}s\n"
        bpm = song_analysis.get("bpm")
        if bpm:
            timing_ctx += f"BPM: {bpm}\n"
        lyr = song_analysis.get("lyrics_timed") or []
        if lyr:
            timing_ctx += (
                "\nLYRICS WITH TIMING (THIS IS YOUR PRIMARY EDITORIAL ANCHOR — "
                "map scene beats to the lyric moments, image the key nouns/verbs, "
                "and honor the emotional register each line carries):\n"
            )
            for L in lyr[:40]:
                st = L.get("start_s") or L.get("start") or 0
                en = L.get("end_s") or L.get("end") or 0
                txt = (L.get("text") or "").strip()
                if not txt:
                    continue
                if len(txt) > 240:
                    txt = txt[:240] + "…"
                timing_ctx += f"  - {st:6.1f}s – {en:6.1f}s : {txt}\n"
            timing_ctx += (
                "\nLYRIC MAPPING RULES:\n"
                "- When a scene's time_start..time_end overlaps a lyric, the scene's "
                "visual action MUST image one concrete noun or verb from that lyric "
                "(not literally, but unmistakably). State which lyric phrase the scene "
                "images inside its `purpose` field.\n"
                "- Silence beats (no lyric overlap) earn the longer held shots.\n"
                "- The title-drop 'Life stream static' at 3.0s should land on the "
                "identity-lock scene's peak moment.\n"
                "- The 'if I break let me break like sunrise' line at 181.4s is the "
                "bridge's thesis — the Dark Moment / Rebirth pivot should land on it.\n"
            )

    prompt = f"""\
BRIEF: {brief}

DURATION: {duration_s:.1f}s

AVAILABLE ENVIRONMENTS (scenes must use these):
{env_list}
{timing_ctx}
Your job: produce the full scene plan that a director hands to the DP.

RULES:
- DIRECT LIKE A FILM. A scene is a dramatic unit in one location with one performance arc, COVERED BY MULTIPLE SHOTS. Default 2–4 shots per scene. The character's body, eyeline, wardrobe, lighting, and emotional temperature MUST flow continuously between shots in the same scene.
- Use the profile's beat_template to lay out the emotional arc. Every beat must be represented by at least one scene.
- First 3–8s = IDENTITY LOCK. The opening scene opens on a medium or close shot with clean face read. Wide establishing shots come AFTER identity is locked (next scene, or later shot inside the opener).
- Scene coverage grammar: default is MASTER (wide/establishing) → MEDIUM or OVER-SHOULDER → CLOSE (reaction/decision). You may invert the order when the beat justifies opening in close (a reveal, an intimate moment), but make it deliberate.
- Every shot MUST declare: acting (specific verb from profile palette), micro_expression (face/eye beat), camera (ONE move), continuity_in (concrete handoff from prior shot — body pose or eyeline), continuity_out (what this shot hands to next), transition_in.
- Ban "floats/drifts/stands/hovers/pulses" as the sole action. Those are non-acting.
- Transitions inside a scene are cut-on-action, eyeline-cut, match-cut — never hard_cut unless the scene changes (location/time jump). Between scenes, transitions are motivated by the beat.
- Scene durations align with song sections/lyric boundaries. Shot durations inside a scene add up to scene duration.
- Number scenes 1, 2, 3... (top-level). Number shots inside each scene 1a, 1b, 1c.
- EYELINE + 180° RULE: First shot of a scene sets the line. If shot Na looks screen-right, shot Nb maintains the vector unless there's a motivated turn or camera move. Never cross the line silently.
- A wide shot without a following medium/close fails the audience's need to read the face. A close without a wide anywhere in the scene leaves geography ambiguous.

{_STORY_SCHEMA_HINT}

Return ONLY the JSON object. No prose wrapper."""
    return call_opus_json(
        prompt=prompt,
        project=project,
        profile_id=profile_id,
        max_tokens=24000,
        thinking_budget=thinking_budget,
    )


# ───────────────────────────────────────────────────────────────────────────
# 2. SCENE — minimal scene spec → fully hydrated with acting beats
# ───────────────────────────────────────────────────────────────────────────

_SCENE_SCHEMA_HINT = """\
Return strict JSON:
{
  "id": string,
  "beat_id": int,
  "location": string,
  "shot_size": "wide" | "medium" | "close",
  "emotion": string,
  "acting": string,
  "looking_at": string,
  "micro_expression": string,     // one-sentence face/body micro-beat
  "camera": string,
  "duration_s": float,
  "transition_in": string,
  "purpose": string,
  "notes": string                 // optional director's note for the renderer
}"""


def direct_scene(minimal_spec: dict,
                 project: Optional[str] = None,
                 profile_id: Optional[str] = None,
                 thinking_budget: int = 2000) -> dict:
    """
    Hydrate a skeletal scene (id + location + rough emotion) into a full
    scene card with emotion/acting/looking_at/camera/transition.
    """
    prompt = f"""\
MINIMAL SCENE SPEC:
{json.dumps(minimal_spec, indent=2)}

Task: fill in every required field using the profile's acting_verb_palette and beat_template.
Reject banned sole-verbs. Pick a specific acting beat that externalizes the emotion.

{_SCENE_SCHEMA_HINT}

Return ONLY the JSON object."""
    return call_opus_json(
        prompt=prompt,
        project=project,
        profile_id=profile_id,
        max_tokens=2000,
        thinking_budget=thinking_budget,
    )


# ───────────────────────────────────────────────────────────────────────────
# 3. KLING PROMPT — scene + refs → final Kling shot prompt
# ───────────────────────────────────────────────────────────────────────────

_KLING_SCHEMA_HINT = """\
Return strict JSON:
{
  "prompt": string,               // 15–40 words, the Kling prompt body
  "negative_prompt": string,      // inherits profile.negative_base + shot-specific adds
  "duration_s": int,              // 5 or 10 (Kling V3 standard tier)
  "camera_move": string,          // one move: static | slow_push_in | slow_pull_back | slow_pan_left | slow_pan_right | slow_tilt_up | slow_tilt_down
  "acting_beat": string,          // verbatim from scene.acting
  "transition_in": string,
  "rationale": string             // one-line why this prompt works for this beat
}"""


def direct_kling_prompt(scene: dict,
                        anchor_path: Optional[str] = None,
                        project: Optional[str] = None,
                        profile_id: Optional[str] = None,
                        thinking_budget: int = 1500) -> dict:
    """
    Author the Kling image-to-video prompt for a single scene.

    Args:
        scene:        fully hydrated scene dict (output of direct_scene or
                      direct_story.scenes[n])
        anchor_path:  optional path to the anchor still — used only as
                      context note; Kling reads the ref image directly.
    """
    anchor_note = f"\nANCHOR STILL: {anchor_path} (reference frame — describes opening pose)" if anchor_path else ""

    prompt = f"""\
SCENE:
{json.dumps(scene, indent=2)}
{anchor_note}

Task: write the Kling prompt. Strict rules from the bible:
- 15–40 words. Anything longer is diluted.
- ONE camera move. No stacking.
- Ban motion verbs ("floats/drifts/hovers/pulses") — Kling will render them literally.
- Ban sound words ("echoes/hears/whispers") — Kling is silent.
- Do NOT re-describe the subject's appearance. The reference image carries identity.
- DO describe: camera, subject's acting beat, micro-expression, eyeline, emotional beat, transition intent.

{_KLING_SCHEMA_HINT}

Return ONLY the JSON object."""
    return call_opus_json(
        prompt=prompt,
        project=project,
        profile_id=profile_id,
        max_tokens=1500,
        thinking_budget=thinking_budget,
    )


# ───────────────────────────────────────────────────────────────────────────
# 3b. V6 SHOT CARD — per-shot cinematic rewrite for the V6 UI fields
# ───────────────────────────────────────────────────────────────────────────

_V6_SHOT_SCHEMA_HINT = """\
Return strict JSON with this EXACT shape. Every field fills a specific UI card slot;
empty strings are NOT acceptable — return something for every field.

{
  "subjectAction": string,        // Anchor prompt "Subject" field. Beat-level acting,
                                  // NOT a single verb. Examples:
                                  //   GOOD: "pupils dilate on the signal pulse; left ear
                                  //          flicks; breath catches mid-exhale"
                                  //   BAD:  "gazes"  "eyes widen"  "paw reaches"
  "shotDescription": string,      // 1 sentence — composition + emotional focus + framing intent
  "lighting": string,             // Anchor prompt "Lighting" field. SHOT-SPECIFIC accent that
                                  // evolves within the scene. If scene is "violet rooftop" and
                                  // this is shot 3 of 3, do NOT repeat the scene-wide light —
                                  // name a new accent (low sun shaft, rim on left ear, etc.).
  "cameraMovement": string,       // Video prompt "Motion" field — ONE camera move tied to the
                                  // acting beat. Motivated, not generic. 5-12 words.
                                  //   GOOD: "slow push-in following his lean forward, 50mm MS to MCU"
                                  //   BAD:  "slow push-in"  "Camera on tripod"
  "envMotion": string,            // Video prompt "Env motion" field — SHOT-SPECIFIC atmospheric
                                  // detail. Vary across shots in the scene.
  "continuityIn": string,         // handoff from prev shot: body pose / eyeline / prop position
                                  // (e.g. "left paw position matches prev final frame")
  "continuityOut": string,        // handoff to next shot
  "subtext": string,              // the emotional WHY of this shot — one sentence
  "rationale": string             // one-line why these choices work for this beat
}
"""


def direct_v6_shot(shot: dict,
                   prev_shot: Optional[dict] = None,
                   next_shot: Optional[dict] = None,
                   character: Optional[dict] = None,
                   environment: Optional[dict] = None,
                   motifs: Optional[list] = None,
                   project: Optional[str] = None,
                   profile_id: Optional[str] = None,
                   thinking_budget: int = 1500) -> dict:
    """Rewrite a V6 shot-card's prompt fields with cinematic specificity.

    Takes the current shot scene record plus neighbors (prev/next) for continuity,
    the referenced character (for identityMark), environment (for location), and
    any motifs. Returns a dict with the eight V6 UI fields (subjectAction,
    shotDescription, lighting, cameraMovement, envMotion, continuityIn,
    continuityOut, subtext) — fresh language, no copy-paste from the scene's other
    shots, acting beats instead of single verbs, motivated camera.

    Call from server handler `/api/v6/director/direct-shot` — result is written
    back to scenes.json under `director_v2.*` (preserves originals) and read by
    the UI populator.
    """
    ctx_parts = []
    ctx_parts.append(f"CURRENT SHOT:\n{json.dumps(shot, indent=2, default=str)}")
    if prev_shot:
        ctx_parts.append(f"PREV SHOT (for continuity_in):\n{json.dumps(prev_shot, indent=2, default=str)}")
    if next_shot:
        ctx_parts.append(f"NEXT SHOT (for continuity_out):\n{json.dumps(next_shot, indent=2, default=str)}")
    if character:
        _c = {k: character.get(k) for k in ("name", "identityMark", "physDesc", "description") if character.get(k)}
        ctx_parts.append(f"CHARACTER (identity is locked by ref image, do NOT re-describe appearance):\n{json.dumps(_c, indent=2)}")
    if environment:
        _e = {k: environment.get(k) for k in ("name", "description", "tags") if environment.get(k)}
        ctx_parts.append(f"ENVIRONMENT:\n{json.dumps(_e, indent=2)}")
    if motifs:
        _m = [{k: m.get(k) for k in ("name", "description", "tags") if m.get(k)} for m in motifs if isinstance(m, dict)]
        if _m:
            ctx_parts.append(f"MOTIFS (visual threads for this scene):\n{json.dumps(_m, indent=2)}")

    prompt = f"""\
{chr(10).join(ctx_parts)}

Task: rewrite this shot's prompt-card fields for the LUMN V6 UI. Make it cinematic — beat-level
acting (not single verbs), shot-specific lighting accents, motivated camera, continuity handoffs
with the neighbors, and a clear emotional subtext. Do NOT re-describe the character's appearance;
the sheet image carries identity. No motion verbs ("floats/drifts"), no sound words ("echoes").

{_V6_SHOT_SCHEMA_HINT}

Return ONLY the JSON object."""
    return call_opus_json(
        prompt=prompt,
        project=project,
        profile_id=profile_id,
        max_tokens=1500,
        thinking_budget=thinking_budget,
    )


# ───────────────────────────────────────────────────────────────────────────
# 4. CRITIQUE — meta-review of a full scene plan
# ───────────────────────────────────────────────────────────────────────────

_CRITIQUE_SCHEMA_HINT = """\
Return strict JSON:
{
  "verdict": "SHIP" | "REVISE",
  "highest_impact_fix": string,            // single most important correction
  "issues": [
    {
      "scene_id": string,
      "severity": "HARD" | "SOFT" | "NIT",
      "category": string,                  // e.g. "banned_sole_verb" | "shot_mix" | "transition" | "identity_lock" | "arc"
      "problem": string,
      "fix": string
    }
  ],
  "arc_health": {
    "identity_locked_early": bool,
    "emotional_arc_complete": bool,
    "shot_mix_on_target": bool,
    "transitions_motivated": bool
  }
}"""


def direct_critique(scene_plan: dict,
                    project: Optional[str] = None,
                    profile_id: Optional[str] = None,
                    thinking_budget: int = 4000) -> dict:
    """
    Opus reviews a full scene plan against the bible.

    Use after direct_story and before rendering — cheap compared to a
    failed batch, and catches drift/mix/arc issues humans miss.
    """
    prompt = f"""\
SCENE PLAN UNDER REVIEW:
{json.dumps(scene_plan, indent=2)[:30000]}

Act as the director reviewing the writers' room draft. Apply every bible rule:
- Identity locked within first 3–8s? (medium or close, face readable)
- Every scene has emotion + acting + looking_at?
- Sole-action verbs all banned ones?
- Shot mix hits profile targets? Min close-per-act met?
- Every transition motivated?
- Full emotional arc present (no missing beats)?
- Scene durations respect song sections?

Lead with the HIGHEST-IMPACT fix. Don't dump 20 nits.

{_CRITIQUE_SCHEMA_HINT}

Return ONLY the JSON object."""
    return call_opus_json(
        prompt=prompt,
        project=project,
        profile_id=profile_id,
        max_tokens=8000,
        thinking_budget=thinking_budget,
    )


if __name__ == "__main__":
    # Smoke test — no API call, just check signatures + imports.
    print("[opus_director] entry points:")
    for fn in [direct_story, direct_scene, direct_kling_prompt, direct_critique]:
        print(f"  - {fn.__name__}{fn.__doc__.splitlines()[0] if fn.__doc__ else ''}")
