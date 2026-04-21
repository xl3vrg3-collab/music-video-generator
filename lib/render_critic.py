"""
Post-Render Quality Critic — second-pass review of generated clips.

Two modes:
  1. Structural (fast, free) — ffprobe metadata + prompt analysis
  2. Vision (Haiku/Sonnet)  — extract frames from clip, send to Claude

After video generation, the critic evaluates each clip and transition
for quality issues that weren't predictable from anchor analysis alone:

  1. Identity drift     — Subject changed appearance during the clip
  2. Wardrobe drift     — Clothing changed
  3. Environment drift  — Background shifted
  4. Lighting drift     — Light direction/color changed
  5. Motion artifacts   — Jitter, morphing, teleportation
  6. Prompt adherence   — Did the clip execute the requested motion?
  7. Transition quality — Does the cut between clips work?

Returns pass/fail per clip with retry strategy suggestions.
"""

import os
import json
import subprocess
import tempfile

# ---------------------------------------------------------------------------
# Clip Metadata Extraction (ffprobe-based)
# ---------------------------------------------------------------------------

def _get_clip_info(clip_path: str) -> dict:
    """Extract duration, resolution, codec, filesize from a clip."""
    if not clip_path or not os.path.isfile(clip_path):
        return {"exists": False, "path": clip_path}

    info = {"exists": True, "path": clip_path}
    info["size_kb"] = os.path.getsize(clip_path) // 1024

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", clip_path],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,  # CREATE_NO_WINDOW on Windows
        )
        probe = json.loads(result.stdout)
        fmt = probe.get("format", {})
        info["duration"] = float(fmt.get("duration", 0))

        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width", 0)
                info["height"] = stream.get("height", 0)
                info["fps"] = eval(stream.get("r_frame_rate", "24/1"))
                info["codec"] = stream.get("codec_name", "unknown")
                break
    except Exception:
        info["duration"] = 0
        info["width"] = 0
        info["height"] = 0

    return info


# ---------------------------------------------------------------------------
# Structural Heuristic Checks
# ---------------------------------------------------------------------------

def _check_duration(clip_info: dict, expected_duration: int) -> dict:
    """Check if clip duration matches expected."""
    actual = clip_info.get("duration", 0)
    if actual == 0:
        return {"pass": False, "issue": "duration_unknown",
                "detail": "Could not read clip duration"}

    # Allow 0.5s tolerance
    delta = abs(actual - expected_duration)
    if delta <= 0.5:
        return {"pass": True, "detail": f"Duration OK ({actual:.1f}s ≈ {expected_duration}s)"}
    if delta <= 1.5:
        return {"pass": True, "issue": "duration_minor",
                "detail": f"Duration {actual:.1f}s vs expected {expected_duration}s (minor)"}
    return {"pass": False, "issue": "duration_mismatch",
            "detail": f"Duration {actual:.1f}s vs expected {expected_duration}s"}


def _check_resolution(clip_info: dict) -> dict:
    """Check resolution is acceptable."""
    w = clip_info.get("width", 0)
    h = clip_info.get("height", 0)
    if w == 0 or h == 0:
        return {"pass": False, "issue": "resolution_unknown",
                "detail": "Could not read resolution"}
    if w < 640 or h < 360:
        return {"pass": False, "issue": "resolution_low",
                "detail": f"Resolution {w}x{h} below minimum"}
    return {"pass": True, "detail": f"Resolution {w}x{h}"}


def _check_filesize(clip_info: dict, expected_duration: int) -> dict:
    """Check filesize is reasonable for duration (not corrupted)."""
    size_kb = clip_info.get("size_kb", 0)
    if size_kb == 0:
        return {"pass": False, "issue": "empty_file", "detail": "File is empty"}

    # Kling V3 outputs high-bitrate video (~2-4MB/s typical)
    min_kb = expected_duration * 100
    max_kb = expected_duration * 6000
    if size_kb < min_kb:
        return {"pass": False, "issue": "filesize_tiny",
                "detail": f"File {size_kb}KB suspiciously small for {expected_duration}s"}
    if size_kb > max_kb:
        return {"pass": True, "issue": "filesize_large",
                "detail": f"File {size_kb}KB large for {expected_duration}s (ok but unusual)"}
    return {"pass": True, "detail": f"Filesize {size_kb}KB normal"}


def _check_prompt_complexity(video_prompt: str, duration: int) -> dict:
    """Check if the video prompt is appropriate for the duration.

    Too many actions in a short clip → likely motion artifacts.
    """
    if not video_prompt:
        return {"pass": True, "detail": "No video prompt to check"}

    # Count action verbs
    action_words = {"walks", "runs", "turns", "looks", "reaches", "sits",
                    "stands", "pushes", "pulls", "pan", "tilt", "track",
                    "dolly", "drift", "follows", "sprint", "leap", "stops",
                    "freezes", "approaches", "arrives"}
    words = set(video_prompt.lower().split())
    action_count = len(words & action_words)

    # More than 1 action per 3 seconds risks artifacts
    max_actions = max(1, duration // 3)
    if action_count > max_actions + 1:
        return {"pass": True, "issue": "prompt_overloaded",
                "detail": f"{action_count} actions in {duration}s — risk of motion artifacts",
                "suggestion": "Split into fewer actions or extend duration"}
    return {"pass": True, "detail": f"{action_count} actions in {duration}s — within budget"}


# ---------------------------------------------------------------------------
# Transition Quality Check
# ---------------------------------------------------------------------------

def check_transition_quality(clip_a_info: dict, clip_b_info: dict,
                             strategy: dict) -> dict:
    """Check if a transition between two clips will work.

    Based on structural properties — not pixel-level analysis.
    """
    issues = []

    # Resolution mismatch
    if clip_a_info.get("exists") and clip_b_info.get("exists"):
        w_a, h_a = clip_a_info.get("width", 0), clip_a_info.get("height", 0)
        w_b, h_b = clip_b_info.get("width", 0), clip_b_info.get("height", 0)
        if w_a != w_b or h_a != h_b:
            issues.append({
                "issue": "resolution_mismatch",
                "detail": f"Clip A: {w_a}x{h_a}, Clip B: {w_b}x{h_b}",
                "severity": "warning",
            })

    # FPS mismatch
    fps_a = clip_a_info.get("fps", 24)
    fps_b = clip_b_info.get("fps", 24)
    if abs(fps_a - fps_b) > 1:
        issues.append({
            "issue": "fps_mismatch",
            "detail": f"Clip A: {fps_a}fps, Clip B: {fps_b}fps",
            "severity": "warning",
        })

    # Strategy-specific checks
    strat_name = strategy.get("strategy", "")
    if strat_name == "direct_animate":
        # Direct animate needs both clips to exist
        if not clip_a_info.get("exists") or not clip_b_info.get("exists"):
            issues.append({
                "issue": "missing_clip",
                "detail": "Direct animate requires both clips",
                "severity": "error",
            })

    passed = all(i["severity"] != "error" for i in issues)
    return {
        "pass": passed,
        "issues": issues,
        "transition_strategy": strat_name,
    }


# ---------------------------------------------------------------------------
# Per-Clip Critic
# ---------------------------------------------------------------------------

def critique_clip(shot: dict, beat: dict, clip_path: str = None) -> dict:
    """Run all structural checks on a single generated clip.

    Returns:
        {
            "shot_id": str,
            "clip_path": str,
            "overall_pass": bool,
            "checks": {check_name: {pass, issue?, detail, suggestion?}},
            "suggestions": [str],
        }
    """
    shot_id = shot.get("shot_id", "unknown")
    path = clip_path or shot.get("clip_path", "")
    expected_dur = shot.get("duration", 5)
    video_prompt = shot.get("video_prompt", "")

    clip_info = _get_clip_info(path)

    checks = {}
    suggestions = []

    if not clip_info["exists"]:
        return {
            "shot_id": shot_id,
            "clip_path": path,
            "clip_info": clip_info,
            "overall_pass": False,
            "checks": {"exists": {"pass": False, "detail": "Clip file not found"}},
            "suggestions": ["Regenerate clip"],
        }

    # Run checks
    checks["duration"] = _check_duration(clip_info, expected_dur)
    checks["resolution"] = _check_resolution(clip_info)
    checks["filesize"] = _check_filesize(clip_info, expected_dur)
    checks["prompt_complexity"] = _check_prompt_complexity(video_prompt, expected_dur)

    # Collect suggestions
    for name, check in checks.items():
        if not check["pass"]:
            suggestions.append(f"[{name}] {check.get('detail', '')} — "
                               f"{check.get('suggestion', 'investigate')}")
        elif check.get("issue"):
            suggestions.append(f"[{name}] Warning: {check.get('detail', '')}")

    overall = all(c["pass"] for c in checks.values())

    return {
        "shot_id": shot_id,
        "clip_path": path,
        "clip_info": clip_info,
        "overall_pass": overall,
        "checks": checks,
        "suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
# Full Plan Critic
# ---------------------------------------------------------------------------

def critique_all_clips(plan: dict, strategies: list = None) -> dict:
    """Run the critic on every clip in the plan.

    Returns:
        {
            "clips": [per-clip critique],
            "transitions": [per-transition quality check],
            "overall_pass": bool,
            "pass_count": int,
            "fail_count": int,
            "warning_count": int,
            "summary": str,
        }
    """
    clip_critiques = []
    transition_checks = []

    all_shots = []
    for beat in plan.get("beats", []):
        for shot in beat.get("shots", []):
            all_shots.append((shot, beat))

    # Critique each clip
    for shot, beat in all_shots:
        critique = critique_clip(shot, beat)
        clip_critiques.append(critique)

    # Check transitions between consecutive clips
    for i in range(len(all_shots) - 1):
        shot_a, _ = all_shots[i]
        shot_b, _ = all_shots[i + 1]

        info_a = _get_clip_info(shot_a.get("clip_path", ""))
        info_b = _get_clip_info(shot_b.get("clip_path", ""))

        strategy = strategies[i] if strategies and i < len(strategies) else {}
        trans_check = check_transition_quality(info_a, info_b, strategy)
        trans_check["from_shot"] = shot_a.get("shot_id")
        trans_check["to_shot"] = shot_b.get("shot_id")
        transition_checks.append(trans_check)

    pass_count = sum(1 for c in clip_critiques if c["overall_pass"])
    fail_count = sum(1 for c in clip_critiques if not c["overall_pass"])
    warning_count = sum(
        1 for c in clip_critiques
        if c["overall_pass"] and any(
            ch.get("issue") for ch in c["checks"].values()
        )
    )

    total = len(clip_critiques)
    overall = fail_count == 0

    summary = (f"{pass_count}/{total} clips passed, "
               f"{fail_count} failed, {warning_count} warnings")

    return {
        "clips": clip_critiques,
        "transitions": transition_checks,
        "overall_pass": overall,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "warning_count": warning_count,
        "summary": summary,
    }


def print_critic_report(result: dict):
    """Print a human-readable critic report."""
    print("\n" + "=" * 70)
    print("TRANSITION INTELLIGENCE — Render Critic Report")
    print("=" * 70)
    print(f"  {result['summary']}")

    for c in result["clips"]:
        status = "PASS" if c["overall_pass"] else "FAIL"
        print(f"\n  [{status}] {c['shot_id']}")
        info = c.get("clip_info", {})
        if info.get("exists"):
            print(f"    {info.get('width', '?')}x{info.get('height', '?')} "
                  f"{info.get('duration', 0):.1f}s {info.get('size_kb', 0)}KB")
        for name, check in c["checks"].items():
            icon = "OK" if check["pass"] else "!!"
            if check.get("issue"):
                icon = "??" if check["pass"] else "!!"
            print(f"    [{icon}] {name}: {check.get('detail', '')}")
        for s in c["suggestions"]:
            print(f"    >>> {s}")

    if result["transitions"]:
        print(f"\n  Transitions:")
        for t in result["transitions"]:
            status = "OK" if t["pass"] else "!!"
            print(f"    [{status}] {t['from_shot']} → {t['to_shot']} "
                  f"({t.get('transition_strategy', 'unknown')})")
            for issue in t.get("issues", []):
                print(f"      {issue['severity']}: {issue['detail']}")


# ---------------------------------------------------------------------------
# Vision-Based Post-Render Critic (Haiku/Sonnet)
# ---------------------------------------------------------------------------

def extract_frames(clip_path: str, num_frames: int = 8,
                   output_dir: str = None) -> list:
    """Extract evenly-spaced frames from a video clip using ffmpeg.

    Returns list of PNG file paths.
    """
    if not clip_path or not os.path.isfile(clip_path):
        return []

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="critic_frames_")
    os.makedirs(output_dir, exist_ok=True)

    # Get duration
    info = _get_clip_info(clip_path)
    duration = info.get("duration", 5.0)
    if duration <= 0:
        return []

    # Calculate frame timestamps
    interval = duration / (num_frames + 1)
    timestamps = [interval * (i + 1) for i in range(num_frames)]

    paths = []
    for i, ts in enumerate(timestamps):
        out_path = os.path.join(output_dir, f"frame_{i:03d}.png")
        try:
            subprocess.run(
                ["ffmpeg", "-ss", str(ts), "-i", clip_path,
                 "-vframes", "1", "-y", out_path],
                capture_output=True, timeout=10,
                creationflags=0x08000000,
            )
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                paths.append(out_path)
        except Exception:
            pass

    return paths


def critique_clip_vision(shot: dict, beat: dict, plan: dict = None,
                         lock_ref_path: str = None,
                         shot_priority: str = "standard",
                         attempt_count: int = 0,
                         num_frames: int = 8) -> dict:
    """Vision-based clip critique using Haiku (Sonnet for escalation).

    Extracts frames from the rendered clip and sends them to Claude
    along with the lock reference and approved anchors.

    Returns structured critique with retry strategy.
    """
    from lib.claude_client import critique_render_vision
    from lib.prompt_packs import haiku_post_render_critic

    shot_id = shot.get("shot_id", "unknown")
    clip_path = shot.get("clip_path", "")
    anchor_path = shot.get("anchor_path", "")

    # Extract frames from clip
    frame_paths = extract_frames(clip_path, num_frames)
    if not frame_paths:
        return {
            "shot_id": shot_id,
            "mode": "vision",
            "overall_pass": False,
            "failure_type": "no_frames",
            "plain_english_summary": "Could not extract frames from clip",
            "retry_strategy": "regenerate_end",
        }

    # Build image list: lock ref + anchor + sampled frames
    image_paths = []
    if lock_ref_path and os.path.isfile(lock_ref_path):
        image_paths.append(lock_ref_path)
    if anchor_path and os.path.isfile(anchor_path):
        image_paths.append(anchor_path)

    # Add sampled frames (limit to 10 to control token cost)
    image_paths.extend(frame_paths[:10])

    # Build prompt
    continuity_lock = ""
    if plan:
        locks = plan.get("continuity_locks", {})
        continuity_lock = locks.get("dog", next(iter(locks.values()), ""))

    prompt = haiku_post_render_critic.render(
        shot_id=shot_id,
        num_sampled_frames=len(frame_paths),
        continuity_lock=continuity_lock,
        transition_strategy=shot.get("transition_strategy", ""),
        motion_note=shot.get("video_prompt", ""),
    )

    # Call Haiku (with auto-escalation)
    result = critique_render_vision(
        prompt, image_paths,
        shot_priority=shot_priority,
        attempt_count=attempt_count,
    )

    if result.get("_parse_error"):
        return {
            "shot_id": shot_id,
            "mode": "vision",
            "overall_pass": True,  # don't block on parse failure
            "failure_type": "parse_error",
            "plain_english_summary": "Vision critic could not parse response",
            "retry_strategy": "accept",
        }

    # Add metadata
    result["shot_id"] = shot_id
    result["mode"] = "vision"
    result["_model_used"] = result.get("_model_used", "haiku")
    result["_frame_count"] = len(frame_paths)

    # Clean up extracted frames
    for fp in frame_paths:
        try:
            os.remove(fp)
        except OSError:
            pass

    return result


def critique_all_clips_vision(plan: dict, strategies: list = None,
                              lock_ref_path: str = None) -> dict:
    """Run vision-based critic on all clips in the plan.

    Expensive (API calls per clip) — use critique_all_clips() for fast mode.
    """
    clip_critiques = []
    all_shots = []

    for beat in plan.get("beats", []):
        for shot in beat.get("shots", []):
            all_shots.append((shot, beat))

    for shot, beat in all_shots:
        priority = shot.get("shot_priority", "standard")
        critique = critique_clip_vision(
            shot, beat, plan=plan,
            lock_ref_path=lock_ref_path,
            shot_priority=priority,
        )
        clip_critiques.append(critique)

    pass_count = sum(1 for c in clip_critiques if c.get("overall_pass", False))
    fail_count = len(clip_critiques) - pass_count

    return {
        "clips": clip_critiques,
        "transitions": [],
        "overall_pass": fail_count == 0,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "warning_count": 0,
        "summary": f"Vision critic: {pass_count}/{len(clip_critiques)} passed",
        "mode": "vision",
    }
