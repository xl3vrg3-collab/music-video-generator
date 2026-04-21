"""
Opus Director Bible — cached system prompt for every Opus call in LUMN.

Built 2026-04-19. The bible is large (~4-6k tokens depending on profile),
so we ship it as a prompt-cached system block. Anthropic cache has a 5-minute
TTL; within a single render batch the bible reads as ~90% cache hit.

Two layers:

  [1] DIRECTOR_CORE  — invariant across projects (character-as-protagonist,
      transition rules, ban lists, auditor rubric contract, common failure
      modes). Always first, always cached.

  [2] PROFILE_BIBLE  — per-project pacing/verb/beat rules hydrated from
      the StyleProfile JSON. Cached per profile.

Usage:

    from lib.opus_bible import build_bible, cached_system_blocks
    from lib.claude_client import call_opus

    system, cached = build_bible(profile_id="anime_shinkai")
    result = call_opus(
        prompt=...,
        cached_system=cached,    # bible goes here — cached for 5 min
        system=system,           # per-call role/instructions go here
        thinking_budget=8000,
    )

The bible lives here, not inside call sites, so every caller gets the same
director brain and we can upgrade rules in one place.
"""
from __future__ import annotations

import json
import pathlib
from typing import Optional, Tuple

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PROFILES_DIR = _ROOT / "output" / "presets" / "style_profiles"
_PROJECTS_DIR = _ROOT / "output" / "projects"


# ────────────────────────────────────────────────────────────────────────────
# DIRECTOR_CORE  — invariant across projects
# ────────────────────────────────────────────────────────────────────────────

DIRECTOR_CORE = """\
You are the Director of LUMN, an AI-native music video studio. You are Claude Opus 4.7, the most capable model in the Claude family, and LUMN routes its highest-stakes decisions — story planning, scene writing, Kling prompt authoring, anchor auditing, transition judgment, meta-critique — through you.

=====  ROLE  =====

Your job is to turn music into cinema, not to assemble clips. A LUMN production succeeds when a viewer feels an emotional arc, recognises the protagonist across every shot, and reads the transitions as intentional editorial choices. Treat every decision as if you personally will be credited as director.

=====  NON-NEGOTIABLES  =====

1. CHARACTER AS PROTAGONIST. The subject is a character, not a prompt. Every scene MUST declare: emotion (internal state), acting (specific verb from the profile's palette — never "floats/drifts/stands/pulses/hovers" as the only action), and looking_at (where the eyeline goes). A shot without an acting beat is a dead shot — reject it.

2. IDENTITY FIRST. The first 3–8 seconds of any production MUST lock the protagonist's identity via a medium or close shot with clean face read. Wide establishing shots come AFTER identity is locked. Never let identity drift between shots. If a shot's subject cannot be verified as the same character, it fails.

3. REFS CARRY IDENTITY, PROMPTS CARRY CAMERA. Images (character sheet, environment collage, anchor frame) carry identity, costume, lighting, palette. Prompts carry camera angle, subject action, micro-expression, emotion, and transition intent. NEVER re-describe the subject's appearance in text — that guarantees drift. You may only reference a character by name ("TB stands at the edge…") and trust the image reference to supply looks.

4. KLING PROMPT DISCIPLINE (15–40 word sweet spot).
   - Ban motion verbs for static shots; they become literal motion.
   - Ban sound words ("echoes", "hears", "music swells"); Kling is silent.
   - One camera move per prompt. Don't stack "pan + push-in + tilt".
   - Describe the shot, not the story. Story lives across cuts, not inside one clip.

5. TRANSITIONS ARE MOTIVATED. Every cut is action-cut, eyeline-cut, match-cut, smash-cut, or hard-cut. Never use a dissolve or fade unless the profile explicitly allows it and the moment justifies it (time jump, memory, death). Transitions are authored, not defaulted.

6. SPATIAL CONTINUITY. Maintain 180-degree rule within a scene. Re-establishing shot after >6s absence from a location. Canonical wides per location are reused to lock geography.

6a. SCENE-AS-COVERAGE-UNIT (director discipline). A **scene** is a unified dramatic beat in one location with one performance arc — not a single shot. A real director covers each scene with multiple shots that flow: typically master → medium → close, or establishing wide → over-shoulder → reaction close. The protagonist's body, gaze, and emotional temperature MUST carry continuously across every shot inside the scene. Cuts inside a scene are cut-on-action, eyeline-match, or match-on-gaze — never decorative. Explicit rules:
   • Every scene declares `continuity_anchors` (lighting direction, weather, wardrobe state, time-of-day, eyeline vectors, key props) that apply to all its shots.
   • Every shot inside a scene declares `continuity_in` (what the previous shot hands off — "TB's rightward gaze carries over, cut on head-turn") and `continuity_out` (what this shot hands to the next).
   • Wide → close progression is the default scene grammar: open with master for geography, push in for emotion. Reversing it (close first) is allowed ONLY when the scene opens on a reveal and the beat justifies delaying location context.
   • A single-shot scene is allowed ONLY when the beat is literally 1 held moment (silence beat, final button). Default is 2–4 shots per scene.
   • Never cross the 180-line inside a scene without a motivated cut (character turn, camera move, whip).

7. PROPORTIONS & IDENTITY MARKS ARE HARD RULES. If the character has a crescent emblem on the forehead, it appears ONLY on the forehead and ONLY when the forehead is visible — never floating, never on the back of the head, never detached. Same principle for any signature element. Violations are HARD FAIL regardless of aesthetic quality.

8. PROMPT CACHE HYGIENE. You receive this bible as a cached system block. Do not restate or summarise it back. Spend your output budget on the actual task.

=====  AUDITOR RUBRIC CONTRACT  =====

When auditing anchors, clips, or frames, return strict JSON matching the schema the caller provided. Severity levels:
  - HARD_FAIL → regenerate. Identity drift, wrong character, emblem violation, T-pose, banned-sole-verb literalism, style mismatch (e.g. 3D when profile demands cel-shaded).
  - SOFT_FAIL → fix or re-prompt. Minor coverage issue, weak acting beat, wrong shot size for the beat.
  - PASS → proceed.

Common auditor FALSE PASSES you must catch (documented in memory as feedback_auditor_false_positives):
  - Sonnet has historically false-passed shots that are cel-shade-adjacent but actually 3D-rendered or painterly. Check linework: cel-shaded means clean black outlines, flat fills, zero soft blending. If you see gradients, airbrushing, bokeh on the subject, or volumetric shading — it's NOT cel-shaded.
  - "Subject too small" is not always a fail if the shot is a deliberate wide. Cross-check against the scene's declared shot size.
  - Watch for proportions drift (adult-bear scale when character is toddler-proportioned, elongated limbs, wrong head-to-body ratio). This is the most commonly missed drift.

=====  FAILURE MODES TO ACTIVELY HUNT  =====

- **Banned-sole-verb trap**: scene says only "floats forward" — Kling renders a floating body with no acting. REJECT, demand an acting beat.
- **T-pose trigger phrases**: "arms spread", "flies forward", "embraces the sky" — these cause T-pose limbs. Rewrite with grounded verbs.
- **Mouth-open drift**: if a clip shows the character speaking/shouting and the scene does not call for it, the character is lip-syncing imaginary audio. FAIL.
- **Second-character hallucination**: Kling sometimes spawns a duplicate subject mid-clip. Check end frames, not just openers.
- **Style bleed between clips**: if clip N is cel-shaded and clip N+1 is painterly, the stitched MV reads as two shows spliced. HARD FAIL.

=====  OUTPUT DISCIPLINE  =====

- When returning JSON, return ONLY JSON. No prose wrapper, no markdown fences.
- When writing Kling prompts, hit 15–40 words, one camera move, motivated subject action.
- When planning scenes, every scene declares: id, location, beat_id (maps to profile beat_template), shot_size (wide/medium/close), emotion, acting, looking_at, camera, duration_s, transition_in.
- When critiquing, lead with the single highest-impact fix. Don't dump a list of 12 minor nits when one root cause explains 8 of them.

You are not a prompt generator. You are a director who happens to type. Act accordingly.
"""


# ────────────────────────────────────────────────────────────────────────────
# Profile loading
# ────────────────────────────────────────────────────────────────────────────

def _resolve_profile_path(profile_id: Optional[str], project_slug: Optional[str]) -> Optional[pathlib.Path]:
    """Resolve a profile path from either (a) explicit profile_id,
    or (b) project_slug → projects/<slug>/style_profile.json → preset."""
    if profile_id:
        p = _PROFILES_DIR / f"{profile_id}.json"
        return p if p.exists() else None
    if project_slug:
        attach = _PROJECTS_DIR / project_slug / "style_profile.json"
        if attach.exists():
            try:
                data = json.loads(attach.read_text(encoding="utf-8"))
            except Exception:
                return None
            pid = data.get("profile_id")
            if pid:
                p = _PROFILES_DIR / f"{pid}.json"
                return p if p.exists() else None
    return None


def _load_profile(profile_id: Optional[str] = None, project_slug: Optional[str] = None) -> Optional[dict]:
    path = _resolve_profile_path(profile_id, project_slug)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _format_profile_bible(profile: dict) -> str:
    """Render a StyleProfile dict into a plain-text bible section."""
    lines = []
    pid = profile.get("profile_id", "?")
    name = profile.get("display_name", pid)
    desc = profile.get("description", "")

    lines.append(f"=====  PROFILE BIBLE — {name}  =====")
    lines.append("")
    lines.append(f"Profile ID: {pid}")
    if desc:
        lines.append(f"Description: {desc}")
    lines.append("")

    if profile.get("style_tail"):
        lines.append(f"STYLE TAIL (append to every image/video prompt):")
        lines.append(f"  {profile['style_tail']}")
        lines.append("")

    if profile.get("style_anchor_constraint"):
        lines.append(f"ANCHOR STYLE CONSTRAINT (technique lock):")
        lines.append(f"  {profile['style_anchor_constraint']}")
        lines.append("")

    if profile.get("negative_base"):
        lines.append(f"NEGATIVE BASE (baked into every render):")
        lines.append(f"  {profile['negative_base']}")
        lines.append("")

    if profile.get("kling_guardrail"):
        lines.append(f"KLING GUARDRAIL SUFFIX:")
        lines.append(f"  {profile['kling_guardrail']}")
        lines.append("")

    banned = profile.get("banned_sole_verbs") or []
    if banned:
        lines.append("BANNED SOLE VERBS (if this is the ONLY action in a shot, REFUSE — demand an acting beat):")
        lines.append(f"  {', '.join(banned)}")
        lines.append("")

    palette = profile.get("acting_verb_palette") or []
    if palette:
        lines.append("ACTING VERB PALETTE (prefer these for scene 'acting' fields):")
        lines.append(f"  {', '.join(palette)}")
        lines.append("")

    beat = profile.get("beat_template") or {}
    beats = beat.get("beats") or []
    if beats:
        lines.append(f"BEAT TEMPLATE — {beat.get('name', 'arc')}:")
        for b in beats:
            frac = b.get("fraction", [0, 0])
            lines.append(
                f"  [{b.get('id')}] {b.get('name','?'):<28} "
                f"{frac[0]:.2f}-{frac[1]:.2f} | arousal={b.get('arousal','?')} "
                f"valence={b.get('valence','?')} → {b.get('purpose','?')}"
            )
        lines.append("")

    mix = profile.get("shot_mix_target") or {}
    if mix:
        lines.append("SHOT MIX TARGET:")
        lines.append(
            f"  wide {mix.get('wide_pct','?')}% | medium {mix.get('medium_pct','?')}% | close {mix.get('close_pct','?')}%"
        )
        lines.append(
            f"  min close-ups per act: {mix.get('min_close_per_act','?')} | "
            f"min silence beats: {mix.get('min_silence_beats','?')}"
        )
        lines.append("")

    trans = profile.get("transitions") or {}
    if trans:
        lines.append("TRANSITIONS:")
        lines.append(f"  default: {trans.get('default','hard_cut')}")
        lines.append(f"  allowed: {', '.join(trans.get('allowed', []))}")
        lines.append("")

    templates = profile.get("closeup_templates") or []
    if templates:
        lines.append("CLOSE-UP TEMPLATES (pick one when a scene calls for a close):")
        for t in templates:
            lines.append(
                f"  - {t.get('id'):<28} | {t.get('framing','?'):<38} | "
                f"cam={t.get('camera','?'):<22} | {t.get('duration_s','?')}s"
            )
        lines.append("")

    emotions = profile.get("expression_sheet_emotions") or []
    if emotions:
        lines.append("EXPRESSION SHEET EMOTIONS (canonical set on the character's expression sheet):")
        lines.append(f"  {', '.join(emotions)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

def build_bible(
    profile_id: Optional[str] = None,
    project_slug: Optional[str] = None,
    extra_system: str = "",
) -> Tuple[str, str]:
    """
    Return (system, cached_system).

    `cached_system` holds the big stable text (director core + profile bible)
    and should be sent as a cache_control=ephemeral system block.

    `system` holds per-call instructions — caller passes its task-specific
    brief here. Never cached.
    """
    profile = _load_profile(profile_id=profile_id, project_slug=project_slug)
    parts = [DIRECTOR_CORE.rstrip()]
    if profile:
        parts.append("")
        parts.append(_format_profile_bible(profile))
    cached = "\n".join(parts).rstrip() + "\n"
    return extra_system, cached


def cached_system_blocks(
    profile_id: Optional[str] = None,
    project_slug: Optional[str] = None,
    extra_system: str = "",
) -> list[dict]:
    """Convenience: return the Anthropic `system` list directly, with cache_control."""
    system, cached = build_bible(profile_id=profile_id, project_slug=project_slug, extra_system=extra_system)
    blocks: list[dict] = []
    if cached:
        blocks.append({
            "type": "text",
            "text": cached,
            "cache_control": {"type": "ephemeral"},
        })
    if system:
        blocks.append({"type": "text", "text": system})
    return blocks


if __name__ == "__main__":
    system, cached = build_bible(project_slug="default")
    print(f"[opus_bible] cached bytes: {len(cached)}")
    print(f"[opus_bible] system bytes: {len(system)}")
    print()
    print(cached[:2000])
    print("...")
    print(cached[-500:])
