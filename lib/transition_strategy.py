"""
Transition Strategy Engine — picks and executes transition strategies.

Based on Transition Judge scores, selects the optimal strategy for each
cut point. Respects cost tiers (standard/premium/hero) and implements
fallback chains.

Strategies (in order of increasing cost):
  1. motivated_cut     — Hard cut carried by narrative motivation (free)
  2. direct_animate    — Animate straight from A→B anchor (cheapest)
  3. end_variants      — Generate end-frame variants of clip A (medium)
  4. bridge_frame      — Generate intermediate still bridging A→B (medium)
  5. regenerate_pair   — Regenerate both clips with matched exit/entry (expensive)

Cut types (for motivated_cut strategy):
  - motivated_cut, hidden_cut, whip_pan_cut, object_wipe_cut,
    foreground_wipe_cut, blink_cut, reaction_cut, insert_cut,
    match_cut, impact_cut, audio_led_cut

Fallback chain:
  keep_start → regen_end_variants → bridge_frame → motivated_cut → regen_pair

Cost tiers control which strategies are available:
  - standard: motivated_cut, direct_animate only
  - premium:  + end_variants, bridge_frame
  - hero:     + regenerate_pair (full retry budget)
"""

import os
from lib.transition_judge import judge_transition, DIMENSION_WEIGHTS

# ---------------------------------------------------------------------------
# Strategy Definitions
# ---------------------------------------------------------------------------

STRATEGIES = {
    "motivated_cut": {
        "label": "Motivated Cut",
        "description": "Hard cut carried by narrative motivation (action, eyeline, smash)",
        "cost_multiplier": 0.0,
        "requires_generation": False,
        "ffmpeg_type": "hard_cut",
    },
    "direct_animate": {
        "label": "Direct Animate",
        "description": "Use shot A anchor as start, shot B anchor as end frame for video gen",
        "cost_multiplier": 1.0,
        "requires_generation": True,
        "ffmpeg_type": "hard_cut",
    },
    "end_variants": {
        "label": "End Variants",
        "description": "Generate 2-3 end-frame variants of clip A, pick best match to B",
        "cost_multiplier": 2.0,
        "requires_generation": True,
        "ffmpeg_type": "hard_cut",
    },
    "bridge_frame": {
        "label": "Bridge Frame",
        "description": "Generate intermediate still that visually bridges A and B",
        "cost_multiplier": 1.5,
        "requires_generation": True,
        "ffmpeg_type": "hard_cut",  # bridge clip inserted, hard cuts around it
    },
    "regenerate_pair": {
        "label": "Regenerate Pair",
        "description": "Regenerate both clips with matched exit/entry constraints",
        "cost_multiplier": 4.0,
        "requires_generation": True,
        "ffmpeg_type": "hard_cut",
    },
}

# ---------------------------------------------------------------------------
# Cost Tier Definitions
# ---------------------------------------------------------------------------

COST_TIERS = {
    "standard": {
        "allowed_strategies": ["motivated_cut", "direct_animate"],
        "max_retries": 0,
        "description": "Budget tier — hard cuts and direct animation only",
    },
    "premium": {
        "allowed_strategies": ["motivated_cut", "direct_animate",
                               "end_variants", "bridge_frame"],
        "max_retries": 1,
        "description": "Mid tier — adds end variants and bridge frames",
    },
    "hero": {
        "allowed_strategies": ["motivated_cut", "direct_animate",
                               "end_variants", "bridge_frame", "regenerate_pair"],
        "max_retries": 2,
        "description": "Full budget — all strategies including regeneration",
    },
}

# ---------------------------------------------------------------------------
# Shot Importance Classification
# ---------------------------------------------------------------------------

def classify_shot_importance(shot_a: dict, shot_b: dict,
                             beat_a: dict, beat_b: dict) -> str:
    """Classify the importance of a transition for cost tier assignment.

    Hero transitions:
    - Identity gate adjacent (first 2 shots)
    - Climax beat boundary
    - Character introduction moment

    Premium transitions:
    - Beat boundaries (inter-beat)
    - Emotional shift moments
    - Close-up to close-up (audience sees everything)

    Standard transitions:
    - Intra-beat cuts
    - Wide to wide
    - Low-energy scenes
    """
    is_inter_beat = beat_a.get("beat_id") != beat_b.get("beat_id")
    is_climax = (beat_a.get("narrative_arc") == "climax" or
                 beat_b.get("narrative_arc") == "climax")
    is_identity_gate = shot_a.get("is_identity_gate", False) or \
                       shot_b.get("is_identity_gate", False)

    # Character introduction: new characters appear in beat B
    chars_a = set(beat_a.get("characters", []))
    chars_b = set(beat_b.get("characters", []))
    new_chars = chars_b - chars_a

    # Both close-ups
    fa = shot_a.get("framing", "").lower()
    fb = shot_b.get("framing", "").lower()
    both_close = ("close" in fa or "tight" in fa) and ("close" in fb or "tight" in fb)

    # Hero
    if is_identity_gate or (is_climax and is_inter_beat):
        return "hero"

    if new_chars and is_inter_beat:
        return "hero"

    # Premium
    if is_inter_beat:
        return "premium"

    if both_close:
        return "premium"

    energy_a = beat_a.get("energy", 0.5)
    energy_b = beat_b.get("energy", 0.5)
    if abs(energy_a - energy_b) > 0.4:
        return "premium"

    # Standard
    return "standard"


# ---------------------------------------------------------------------------
# Motivation Detection
# ---------------------------------------------------------------------------

_MOTIVATION_TYPES = {
    "action": {
        "exit_words": {"sprint", "run", "leap", "jump", "catches", "grabs",
                       "throws", "pushes", "falls"},
        "description": "Action carries the cut — movement momentum bridges the edit",
    },
    "eyeline": {
        "exit_words": {"looks", "stares", "gazes", "watches", "spots", "notices",
                       "sees", "turns to look", "glances"},
        "description": "Gaze direction carries the cut — audience follows the look",
    },
    "smash": {
        "conditions": lambda ea, eb: abs(ea - eb) > 0.4,
        "description": "Energy contrast carries the cut — shock transition",
    },
    "match": {
        "conditions": lambda ea, eb: True,  # checked via camera score
        "description": "Visual geometry match carries the cut — shape rhyme",
    },
}


def detect_motivation(shot_a: dict, shot_b: dict,
                      beat_a: dict, beat_b: dict,
                      judge_result: dict) -> dict:
    """Detect if a narrative motivation can carry a hard cut.

    Returns: {type, strength, reason} or None if no motivation found.
    """
    action_a = shot_a.get("action", "").lower()
    exit_trans = beat_a.get("transition_out", {})
    exit_type = exit_trans.get("motivation", "")
    energy_a = beat_a.get("energy", 0.5)
    energy_b = beat_b.get("energy", 0.5)

    # Explicit exit motivation in plan
    if exit_type in ("cut_on_action", "action"):
        return {
            "type": "action",
            "strength": 9,
            "reason": exit_trans.get("note", "Action exit — plan override"),
        }

    if exit_type == "cut_on_look":
        return {
            "type": "eyeline",
            "strength": 8,
            "reason": exit_trans.get("note", "Look exit — gaze carries the cut"),
        }

    if exit_type in ("smash_cut", "freeze"):
        return {
            "type": "smash",
            "strength": 8 if abs(energy_a - energy_b) > 0.3 else 6,
            "reason": "Freeze/smash exit — energy contrast",
        }

    # Auto-detect from action text
    words_a = set(action_a.split())
    for mot_type, config in _MOTIVATION_TYPES.items():
        if "exit_words" in config:
            if words_a & config["exit_words"]:
                return {
                    "type": mot_type,
                    "strength": 6,
                    "reason": f"Auto-detected {mot_type} from action text",
                }

    # Smash cut auto-detect from energy delta
    if abs(energy_a - energy_b) > 0.5:
        return {
            "type": "smash",
            "strength": 5,
            "reason": f"Auto-detected smash from energy delta={abs(energy_a - energy_b):.1f}",
        }

    return None


# ---------------------------------------------------------------------------
# Strategy Selection
# ---------------------------------------------------------------------------

def select_strategy(judge_result: dict,
                    shot_a: dict, shot_b: dict,
                    beat_a: dict, beat_b: dict,
                    cost_tier: str = None,
                    force_strategy: str = None) -> dict:
    """Select the optimal transition strategy based on judge scores.

    Decision logic:
    1. If narrative motivation detected and score allows → motivated_cut
    2. If composite >= 7 and no critical dimension → direct_animate
    3. If identity/pose weak but camera/scene ok → end_variants
    4. If scene/camera weak → bridge_frame
    5. If everything broken → regenerate_pair (hero only)

    Returns:
        {
            "strategy": str,
            "reason": str,
            "cost_tier": str,
            "importance": str,
            "motivation": dict | None,
            "fallback_chain": [str],
            "generation_params": dict,
        }
    """
    if force_strategy and force_strategy in STRATEGIES:
        return {
            "strategy": force_strategy,
            "reason": f"Forced: {force_strategy}",
            "cost_tier": cost_tier or "hero",
            "importance": "hero",
            "motivation": None,
            "fallback_chain": [],
            "generation_params": _build_gen_params(force_strategy, shot_a, shot_b,
                                                    beat_a, beat_b, judge_result),
        }

    # Classify importance → determines cost tier
    importance = classify_shot_importance(shot_a, shot_b, beat_a, beat_b)
    if not cost_tier:
        cost_tier = importance  # importance maps directly to tier name

    tier_config = COST_TIERS.get(cost_tier, COST_TIERS["standard"])
    allowed = tier_config["allowed_strategies"]

    composite = judge_result["composite"]
    dims = judge_result["dimensions"]
    weakest = judge_result["weakest_dimension"]
    weakest_score = judge_result["weakest_score"]

    # Step 1: Check for narrative motivation
    motivation = detect_motivation(shot_a, shot_b, beat_a, beat_b, judge_result)
    if motivation and motivation["strength"] >= 6:
        # Motivated cuts work when composite >= 4 (not totally broken)
        if composite >= 4 and "motivated_cut" in allowed:
            return _build_result(
                "motivated_cut", cost_tier, importance, motivation,
                f"Motivation ({motivation['type']}, str={motivation['strength']}) "
                f"carries the cut at composite={composite}",
                _fallback_chain("motivated_cut", allowed),
                shot_a, shot_b, beat_a, beat_b, judge_result,
            )

    # Step 2: High composite — direct animate
    if composite >= 7 and weakest_score >= 5:
        if "direct_animate" in allowed:
            return _build_result(
                "direct_animate", cost_tier, importance, motivation,
                f"High continuity (composite={composite}, weakest={weakest}={weakest_score})",
                _fallback_chain("direct_animate", allowed),
                shot_a, shot_b, beat_a, beat_b, judge_result,
            )

    # Step 3: Identity or pose weak — end variants can fix exit pose
    if weakest in ("identity", "pose") and composite >= 5:
        if "end_variants" in allowed:
            return _build_result(
                "end_variants", cost_tier, importance, motivation,
                f"Weak {weakest} (score={weakest_score}) — end variants can match exit to entry",
                _fallback_chain("end_variants", allowed),
                shot_a, shot_b, beat_a, beat_b, judge_result,
            )

    # Step 4: Scene or camera weak — bridge frame
    if weakest in ("scene", "camera") or composite < 5:
        if "bridge_frame" in allowed:
            return _build_result(
                "bridge_frame", cost_tier, importance, motivation,
                f"Weak {weakest} (score={weakest_score}) — bridge frame needed for visual continuity",
                _fallback_chain("bridge_frame", allowed),
                shot_a, shot_b, beat_a, beat_b, judge_result,
            )

    # Step 5: Everything broken — regenerate pair (hero only)
    if composite < 4 and "regenerate_pair" in allowed:
        return _build_result(
            "regenerate_pair", cost_tier, importance, motivation,
            f"Critical discontinuity (composite={composite}) — regeneration required",
            _fallback_chain("regenerate_pair", allowed),
            shot_a, shot_b, beat_a, beat_b, judge_result,
        )

    # Fallback: motivated cut if we have motivation, else direct animate, else hard cut
    if motivation and "motivated_cut" in allowed:
        strategy = "motivated_cut"
        reason = f"Fallback to motivated cut ({motivation['type']})"
    elif "direct_animate" in allowed:
        strategy = "direct_animate"
        reason = f"Fallback to direct animate (composite={composite})"
    else:
        strategy = "motivated_cut"
        reason = f"Final fallback — motivated cut (composite={composite})"

    return _build_result(
        strategy, cost_tier, importance, motivation, reason,
        [], shot_a, shot_b, beat_a, beat_b, judge_result,
    )


def _build_result(strategy, cost_tier, importance, motivation, reason,
                  fallback_chain, shot_a, shot_b, beat_a, beat_b, judge_result):
    return {
        "strategy": strategy,
        "reason": reason,
        "cost_tier": cost_tier,
        "importance": importance,
        "motivation": motivation,
        "fallback_chain": fallback_chain,
        "generation_params": _build_gen_params(strategy, shot_a, shot_b,
                                                beat_a, beat_b, judge_result),
    }


def _fallback_chain(current: str, allowed: list) -> list:
    """Build ordered fallback chain from current strategy down."""
    chain_order = ["direct_animate", "end_variants", "bridge_frame",
                   "motivated_cut", "regenerate_pair"]
    idx = chain_order.index(current) if current in chain_order else 0
    return [s for s in chain_order[idx + 1:] if s in allowed]


def _build_gen_params(strategy: str, shot_a: dict, shot_b: dict,
                      beat_a: dict, beat_b: dict,
                      judge_result: dict) -> dict:
    """Build generation parameters for the selected strategy."""
    params = {"strategy": strategy}

    if strategy == "motivated_cut":
        # No generation needed — just a cut type
        motivation = detect_motivation(shot_a, shot_b, beat_a, beat_b, judge_result)
        cut_type = "hard_cut"
        if motivation:
            cut_type = {
                "action": "hard_cut",
                "eyeline": "hard_cut",
                "smash": "hard_cut",
                "match": "hard_cut",
            }.get(motivation["type"], "hard_cut")
        params["cut_type"] = cut_type
        params["motivation_type"] = motivation["type"] if motivation else "default"

    elif strategy == "direct_animate":
        # Use shot B's anchor as end_image for clip A's video generation
        params["use_end_frame"] = True
        params["end_image_source"] = "shot_b_anchor"
        params["end_image_path"] = shot_b.get("anchor_path", "")

    elif strategy == "end_variants":
        # Generate multiple end-frame variants, pick closest to B's anchor
        params["num_variants"] = 3
        params["target_match_path"] = shot_b.get("anchor_path", "")
        params["variant_prompt_base"] = _build_end_variant_prompt(shot_a, beat_a)

    elif strategy == "bridge_frame":
        # Generate an intermediate still that bridges A→B
        params["bridge_prompt"] = _build_bridge_prompt(
            shot_a, shot_b, beat_a, beat_b, judge_result)
        params["bridge_refs"] = _collect_bridge_refs(shot_a, shot_b)
        params["bridge_duration"] = 3  # seconds

    elif strategy == "regenerate_pair":
        # Full regeneration with matched constraints
        params["regen_shot_a"] = True
        params["regen_shot_b"] = True
        params["match_constraints"] = _build_match_constraints(
            shot_a, shot_b, beat_a, beat_b, judge_result)

    return params


def _build_end_variant_prompt(shot: dict, beat: dict) -> str:
    """Build prompt for end-frame variants of a shot."""
    action = shot.get("action", "")
    framing = shot.get("framing", "medium")
    return (
        f"Final frame of shot. {framing} framing. "
        f"Subject completing action: {action}. "
        f"Pose suitable for cut to next scene."
    )


def _build_bridge_prompt(shot_a: dict, shot_b: dict,
                         beat_a: dict, beat_b: dict,
                         judge_result: dict) -> str:
    """Build prompt for a bridge frame between two shots."""
    weakest = judge_result["weakest_dimension"]
    loc_a = beat_a.get("location_desc", "")
    loc_b = beat_b.get("location_desc", "")

    if weakest == "scene":
        # Bridge between locations
        return (
            f"Wide transitional shot showing spatial connection. "
            f"Foreground: {loc_a}. Background: {loc_b}. "
            f"Golden hour warm light. Deep perspective. "
            f"Photorealistic 35mm film, natural grain."
        )
    elif weakest == "camera":
        # Bridge with intermediate framing
        size_a = shot_a.get("framing", "medium")
        size_b = shot_b.get("framing", "medium")
        return (
            f"Medium shot bridging {size_a} to {size_b}. "
            f"{loc_a}. Natural lighting. "
            f"Photorealistic 35mm film, shallow depth of field."
        )
    else:
        # General bridge
        return (
            f"Transitional still between scenes. "
            f"Subject in natural pose, {loc_a}. "
            f"Golden hour backlight, warm tones. "
            f"Photorealistic 35mm film."
        )


def _collect_bridge_refs(shot_a: dict, shot_b: dict) -> list:
    """Collect reference image paths for bridge frame generation."""
    refs = []
    for path_key in ["anchor_path", "scene_still_path"]:
        for shot in [shot_a, shot_b]:
            path = shot.get(path_key, "")
            if path and os.path.isfile(path) and path not in refs:
                refs.append(path)
    return refs[:4]  # max 4 refs


def _build_match_constraints(shot_a: dict, shot_b: dict,
                             beat_a: dict, beat_b: dict,
                             judge_result: dict) -> dict:
    """Build regeneration constraints to ensure matched exit/entry."""
    dims = judge_result["dimensions"]
    constraints = {}

    if dims["identity"]["score"] < 5:
        constraints["lock_identity"] = True
        constraints["identity_refs"] = list(set(
            shot_a.get("anchor_refs", []) + shot_b.get("anchor_refs", [])
        ))

    if dims["camera"]["score"] < 5:
        constraints["match_framing"] = True
        constraints["target_size"] = shot_b.get("framing", "medium")

    if dims["motion"]["score"] < 5:
        constraints["match_exit_direction"] = True

    return constraints


# ---------------------------------------------------------------------------
# Plan-Level Strategy Assignment
# ---------------------------------------------------------------------------

def assign_all_strategies(plan: dict, judge_results: list,
                          cost_override: str = None) -> list:
    """Assign strategies to every transition in the plan.

    Args:
        plan: production plan dict
        judge_results: output of judge_all_transitions()
        cost_override: force all transitions to a specific cost tier

    Returns list of strategy assignments, one per judge result.
    """
    all_shots = []
    for beat in plan.get("beats", []):
        for shot in beat.get("shots", []):
            all_shots.append((shot, beat))

    strategies = []
    for i, judge_result in enumerate(judge_results):
        shot_a, beat_a = all_shots[i]
        shot_b, beat_b = all_shots[i + 1]

        strategy = select_strategy(
            judge_result, shot_a, shot_b, beat_a, beat_b,
            cost_tier=cost_override,
        )
        strategy["judge"] = judge_result
        strategies.append(strategy)

    return strategies


def print_strategy_report(strategies: list):
    """Print a human-readable strategy report."""
    print("\n" + "=" * 70)
    print("TRANSITION INTELLIGENCE — Strategy Report")
    print("=" * 70)

    cost_total = 0.0
    for s in strategies:
        judge = s["judge"]
        strat_def = STRATEGIES[s["strategy"]]
        cost_total += strat_def["cost_multiplier"]

        mot = s.get("motivation")
        mot_str = f" [{mot['type']}]" if mot else ""

        print(f"\n  {judge['from_shot']} → {judge['to_shot']}")
        print(f"    Strategy:   {strat_def['label']}{mot_str}")
        print(f"    Importance: {s['importance']}  (tier={s['cost_tier']})")
        print(f"    Reason:     {s['reason']}")
        if s["fallback_chain"]:
            print(f"    Fallbacks:  {' → '.join(s['fallback_chain'])}")
        print(f"    Composite:  {judge['composite']}/10  "
              f"risk={judge['risk_level']}")

    print(f"\n  Total cost multiplier: {cost_total:.1f}x base")
    print(f"  Transitions: {len(strategies)}")


# ---------------------------------------------------------------------------
# Cut Type Definitions (for motivated_cut strategy)
# ---------------------------------------------------------------------------

CUT_TYPES = {
    "motivated_cut": {
        "label": "Motivated Cut",
        "description": "Action momentum carries the audience across the edit",
        "requires_motion": True,
    },
    "hidden_cut": {
        "label": "Hidden Cut",
        "description": "Camera sweeps past foreground element, concealing the splice",
        "requires_motion": True,
    },
    "whip_pan_cut": {
        "label": "Whip Pan Cut",
        "description": "Fast camera whip with motion blur bridges the transition",
        "requires_motion": True,
    },
    "object_wipe_cut": {
        "label": "Object Wipe Cut",
        "description": "Subject or prop crosses frame, creating a natural wipe",
        "requires_motion": True,
    },
    "foreground_wipe_cut": {
        "label": "Foreground Wipe Cut",
        "description": "Camera pushes through foreground element for seamless wipe",
        "requires_motion": True,
    },
    "blink_cut": {
        "label": "Blink Cut",
        "description": "Subject blinks, creating a natural pause point for the edit",
        "requires_motion": False,
    },
    "reaction_cut": {
        "label": "Reaction Cut",
        "description": "Subject's look or reaction motivates cutting to what they see",
        "requires_motion": False,
    },
    "insert_cut": {
        "label": "Insert Cut",
        "description": "Camera pushes in on a detail, bridging two different scales",
        "requires_motion": True,
    },
    "match_cut": {
        "label": "Match Cut",
        "description": "Visual geometry match between last and first frames",
        "requires_motion": False,
    },
    "impact_cut": {
        "label": "Impact Cut",
        "description": "Sudden stop or collision creates a hard natural cut point",
        "requires_motion": True,
    },
    "audio_led_cut": {
        "label": "Audio-Led Cut",
        "description": "Motion settles into stillness, audio carries the transition",
        "requires_motion": False,
    },
}


def choose_cut_type(shot_a: dict, shot_b: dict,
                    beat_a: dict, beat_b: dict,
                    motivation: dict = None) -> str:
    """Choose the best cut type for a motivated cut strategy.

    Analyzes action, framing, energy to pick the most appropriate cut style.
    """
    action_a = shot_a.get("action", "").lower()
    energy_a = beat_a.get("energy", 0.5)
    energy_b = beat_b.get("energy", 0.5)
    energy_delta = abs(energy_a - energy_b)
    framing_a = shot_a.get("framing", "").lower()
    framing_b = shot_b.get("framing", "").lower()

    # Explicit motivation type maps to cut type
    if motivation:
        mot_type = motivation.get("type", "")
        if mot_type == "action":
            if energy_delta > 0.4:
                return "impact_cut"
            return "motivated_cut"
        if mot_type == "eyeline":
            return "reaction_cut"
        if mot_type == "smash":
            return "impact_cut" if energy_delta > 0.5 else "whip_pan_cut"
        if mot_type == "match":
            return "match_cut"

    # Auto-detect from context
    if "close" in framing_a and "close" in framing_b:
        return "blink_cut"  # close-to-close = subtle cut
    if "wide" in framing_b:
        return "foreground_wipe_cut"  # going wide = sweep out
    if energy_delta > 0.5:
        return "impact_cut"
    if any(w in action_a for w in ["looks", "stares", "watches", "sees"]):
        return "reaction_cut"
    if any(w in action_a for w in ["runs", "sprint", "chase"]):
        return "motivated_cut"

    return "motivated_cut"  # safe default


# ---------------------------------------------------------------------------
# Prompt Builder Integration
# ---------------------------------------------------------------------------

def build_gemini_prompt(strategy: str, shot_a: dict, shot_b: dict,
                        beat_a: dict, beat_b: dict, plan: dict = None,
                        **kwargs) -> str:
    """Build Gemini prompt for the given strategy using prompt packs.

    Returns the rendered prompt string ready for gemini_edit_image().
    """
    from lib.prompt_packs import render

    locks = (plan or {}).get("continuity_locks", {})
    style_bible = (plan or {}).get("style_bible", "")
    continuity_lock = locks.get("dog", next(iter(locks.values()), ""))

    if strategy == "end_variants":
        return render("gemini_end_variants",
            subject_desc=shot_a.get("action", ""),
            exit_action=shot_a.get("action", ""),
            framing=shot_a.get("framing", "medium"),
            camera_height=shot_a.get("camera_height", "eye level"),
            environment=beat_a.get("location_desc", ""),
            lighting="golden hour backlight",
            style_bible=style_bible,
            continuity_lock=continuity_lock,
            start_frame_desc=shot_a.get("anchor_prompt", ""),
            failure_reasons=kwargs.get("failure_reasons"),
        )

    elif strategy == "bridge_frame":
        return render("gemini_bridge_frame",
            subject_desc=locks.get("dog", ""),
            start_desc=shot_a.get("anchor_prompt", "")[:200],
            end_desc=shot_b.get("anchor_prompt", "")[:200],
            framing="medium shot",
            environment=beat_a.get("location_desc", ""),
            lighting="golden hour backlight",
            style_bible=style_bible,
            continuity_lock=continuity_lock,
        )

    elif strategy == "repair_frame":
        return render("gemini_repair_frame",
            subject_desc=locks.get("dog", ""),
            framing=shot_a.get("framing", "medium"),
            environment=beat_a.get("location_desc", ""),
            lighting="golden hour backlight",
            style_bible=style_bible,
            continuity_lock=continuity_lock,
            failure_type=kwargs.get("failure_type", ""),
            failure_details=kwargs.get("failure_details", ""),
            fix_instructions=kwargs.get("fix_instructions", ""),
        )

    else:
        # Default start frame prompt
        return render("gemini_start_frame",
            subject_desc=locks.get("dog", ""),
            action=shot_a.get("action", ""),
            framing=shot_a.get("framing", "medium"),
            camera_height=shot_a.get("camera_height", "eye level"),
            environment=beat_a.get("location_desc", ""),
            lighting="golden hour backlight",
            style_bible=style_bible,
            continuity_lock=continuity_lock,
        )


def build_kling_prompt(strategy: str, shot_a: dict, shot_b: dict = None,
                       beat_a: dict = None, plan: dict = None,
                       cut_type: str = "motivated_cut",
                       **kwargs) -> dict:
    """Build Kling video prompt for the given strategy using prompt packs.

    Returns {"prompt": str, "negative_prompt": str}.
    """
    from lib.prompt_packs import render

    style_bible = (plan or {}).get("style_bible", "")

    if strategy == "direct_animate":
        from lib.prompt_packs import kling_direct_motion
        prompt = kling_direct_motion.render(
            motion_desc=shot_a.get("video_prompt", ""),
            camera_move=shot_a.get("camera_height", ""),
            duration_sec=shot_a.get("duration", 5),
            cinematic_tone=style_bible,
        )
        negative = kling_direct_motion.render_negative()

    elif strategy == "bridge_frame":
        from lib.prompt_packs import kling_bridge_motion
        segment = kwargs.get("segment", "start_to_bridge")
        prompt = kling_bridge_motion.render(
            motion_desc=shot_a.get("video_prompt", ""),
            camera_move="",
            duration_sec=3,
            segment=segment,
            cinematic_tone=style_bible,
        )
        negative = kling_bridge_motion.render_negative()

    elif strategy == "motivated_cut":
        from lib.prompt_packs import kling_motivated_cut
        prompt = kling_motivated_cut.render(
            cut_type=cut_type,
            motion_desc=shot_a.get("video_prompt", ""),
            cinematic_tone=style_bible,
        )
        negative = kling_motivated_cut.render_negative(cut_type=cut_type)

    else:
        # Default: direct motion
        from lib.prompt_packs import kling_direct_motion
        prompt = kling_direct_motion.render(
            motion_desc=shot_a.get("video_prompt", ""),
            cinematic_tone=style_bible,
        )
        negative = kling_direct_motion.render_negative()

    return {"prompt": prompt, "negative_prompt": negative}
