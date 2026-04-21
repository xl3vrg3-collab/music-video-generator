"""Batch renderer for all 26 TB Lifestream Static shots.

- Reads scenes.json from the active project
- For each scene, generates anchor via Gemini 3.1 Flash edit (2K, 16:9)
  using [env preview + TB sheet + TB face] as references
- Animates via Kling V3 Pro I2V using camera movement as motion driver
- Skips any shot that already has a selected.mp4 (resume-safe)
- Writes a run manifest to output/pipeline/batch_runs/<timestamp>.json
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from lib.fal_client import gemini_edit_image, kling_image_to_video  # noqa: E402
from lib.kling_prompt_linter import lint_kling_prompt  # noqa: E402
from lib.anchor_auditor import audit_scene_anchor  # noqa: E402
from lib.render_critic import critique_clip  # noqa: E402

PROJECT_ROOT = os.path.join(ROOT, "output", "projects", "default", "prompt_os")
SCENES_JSON  = os.path.join(PROJECT_ROOT, "scenes.json")
OVERRIDES_JSON = os.path.join(PROJECT_ROOT, "kling_prompt_overrides.json")
ENV_PREV_DIR = os.path.join(PROJECT_ROOT, "env_previews")
CHAR_PREV_DIR = os.path.join(PROJECT_ROOT, "previews", "characters")

FORCE_REGEN = os.environ.get("LUMN_FORCE_REGEN", "").strip().lower() in ("1", "true", "yes")
FORCE_ANCHOR_REGEN = os.environ.get("LUMN_FORCE_ANCHOR_REGEN", "").strip().lower() in ("1", "true", "yes")
FORCE_CLIP_ONLY = os.environ.get("LUMN_FORCE_CLIP_ONLY", "").strip().lower() in ("1", "true", "yes")
ANCHOR_ONLY = os.environ.get("LUMN_ANCHOR_ONLY", "").strip().lower() in ("1", "true", "yes")
REGEN_IDS = {s for s in (os.environ.get("LUMN_REGEN_IDS", "").split(",")) if s.strip()}
ONLY_IDS = {s for s in (os.environ.get("LUMN_ONLY_IDS", "").split(",")) if s.strip()}
KLING_TIER = (os.environ.get("LUMN_KLING_TIER") or "v3_standard").strip()
PREFLIGHT_STRICT = os.environ.get("LUMN_PREFLIGHT_STRICT", "").strip().lower() in ("1", "true", "yes")
SKIP_AUDIT = os.environ.get("LUMN_SKIP_AUDIT", "").strip().lower() in ("1", "true", "yes")

TB_SHEET = os.path.join(CHAR_PREV_DIR, "6d31f281-4cc_full_1776356463.png")
TB_FACE  = os.path.join(CHAR_PREV_DIR, "6d31f281-4cc_face_closeup_1776355100.png")

ANCHORS_DIR = os.path.join(ROOT, "output", "pipeline", "anchors_v6")
CLIPS_DIR   = os.path.join(ROOT, "output", "pipeline", "clips_v6")
RUNS_DIR    = os.path.join(ROOT, "output", "pipeline", "batch_runs")

# ── Style profile resolution (task #69) ─────────────────────────────────────
# Pacing/verb/style rules are per-project. We load from the project's
# style_profile.json → preset file. Hardcoded constants below are FALLBACKS
# only (used when no profile is attached — e.g. brand-new project).

_PROJECT_SLUG = os.environ.get("LUMN_PROJECT", "default")

_STYLE_TAIL_FALLBACK = (
    "Makoto Shinkai anime realism, soft bloom, painterly clouds, "
    "atmospheric perspective, cinematic composition, shallow depth of field."
)
_NEGATIVE_BASE_FALLBACK = (
    "blur, distortion, low quality, watermark, text, extra limbs, deformed, "
    "photorealistic human, live action, 3D figurine, plastic, doll, "
    "talking, speaking, walking in place, wobble, morphing, "
    "floating moon, crescent above head, emblem detached from forehead, "
    "halo over head, moon in background near head, emblem visible when forehead is not visible, "
    "pulsing emblem, strobing emblem, flashing emblem, glowing emblem, emblem changing size, "
    "beat sync, rhythmic flashing, on beat, mid-clip morph, sudden transformation, "
    "subject leaning into camera, subject mouth opening, face warping, face stretching, "
    "identity drift, character transformation, different character, second bear appearing, "
    "adult bear, realistic bear proportions, tall bear, human proportions, toddler proportions, "
    "hood suddenly raised, hood suddenly lowered, outfit change, hoodie color change"
)
_KLING_GUARDRAIL_FALLBACK = (
    " Camera moves exactly as described; subject holds anchor pose and identity. "
    "Emblem, proportions, hoodie, and facial features remain exactly as in reference frame."
)


def _load_style_profile() -> dict:
    """Resolve the active project's style profile. Returns {} on miss."""
    # 1. project-attached profile
    attach = os.path.join(ROOT, "output", "projects", _PROJECT_SLUG, "style_profile.json")
    if os.path.isfile(attach):
        try:
            with open(attach, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        # Project file can inline overrides; otherwise it points to a preset
        pid = data.get("profile_id")
        if pid:
            preset = os.path.join(ROOT, "output", "presets", "style_profiles", f"{pid}.json")
            if os.path.isfile(preset):
                try:
                    with open(preset, "r", encoding="utf-8") as f:
                        base = json.load(f)
                except Exception:
                    base = {}
                # project-level overrides beat preset values
                base.update({k: v for k, v in data.items() if k not in ("profile_id", "source_preset", "project")})
                return base
        return data
    return {}


_STYLE_PROFILE = _load_style_profile()

STYLE_TAIL = _STYLE_PROFILE.get("style_tail") or _STYLE_TAIL_FALLBACK
NEGATIVE_BASE = _STYLE_PROFILE.get("negative_base") or _NEGATIVE_BASE_FALLBACK
_guardrail = _STYLE_PROFILE.get("kling_guardrail") or _KLING_GUARDRAIL_FALLBACK
KLING_GUARDRAIL = _guardrail if _guardrail.startswith(" ") else " " + _guardrail
STYLE_ANCHOR_CONSTRAINT = _STYLE_PROFILE.get("style_anchor_constraint", "")
BANNED_SOLE_VERBS = set(v.lower() for v in (_STYLE_PROFILE.get("banned_sole_verbs") or []))

if _STYLE_PROFILE:
    print(f"[style_profile] loaded profile_id={_STYLE_PROFILE.get('profile_id','(inline)')} "
          f"for project={_PROJECT_SLUG} — tail={len(STYLE_TAIL)}ch, negatives={len(NEGATIVE_BASE)}ch, "
          f"banned_verbs={len(BANNED_SOLE_VERBS)}", flush=True)
else:
    print(f"[style_profile] no profile attached for project={_PROJECT_SLUG}, using fallbacks", flush=True)

RETRY_DELAYS = (5, 15, 45)


def _with_retry(label: str, fn):
    last_err = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS, 1):
        if delay:
            print(f"    retry {attempt}/{len(RETRY_DELAYS)+1} in {delay}s (last: {last_err})", flush=True)
            time.sleep(delay)
        try:
            return fn()
        except Exception as e:
            last_err = str(e)[:160]
            msg = last_err.lower()
            transient = any(t in msg for t in (
                "nameresolutionerror", "getaddrinfo", "max retries",
                "connection", "timeout", "timed out", "502", "503", "504",
                "read timed out", "temporarily", "failed to resolve",
                "exceeded", "deadline",
            ))
            if not transient:
                raise
    raise RuntimeError(f"{label}: retries exhausted — {last_err}")


def _find_env_preview(env_id: str) -> str | None:
    """Return the most recent _full_*.png for this env id."""
    if not os.path.isdir(ENV_PREV_DIR):
        return None
    candidates = [
        f for f in os.listdir(ENV_PREV_DIR)
        if f.startswith(env_id + "_full_") and f.endswith(".png")
    ]
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return os.path.join(ENV_PREV_DIR, candidates[0])


def _shot_scale(angle: str, desc: str) -> str:
    """Infer shot scale from cameraAngle / description. Returns wide/medium/close."""
    tokens = f"{angle} {desc}".lower()
    if any(k in tokens for k in ("ultra-wide", "ultra wide", "wide shot", "establishing", "extreme long", "long shot")):
        return "wide"
    if any(k in tokens for k in ("close-up", "close up", "closeup", "ecu", "extreme close", "macro")):
        return "close"
    if "wide" in tokens:
        return "wide"
    return "medium"


def _build_anchor_prompt(scene: dict) -> str:
    angle = (scene.get("cameraAngle") or "").strip()
    desc = (scene.get("shotDescription") or "").strip()
    sid = scene.get("id", "")

    overrides = _load_overrides()
    shot_override = overrides.get(sid, {}) or {}

    if shot_override.get("render_as_pov"):
        extra = (shot_override.get("anchor_extra") or "").strip()
        suffix = (" " + extra) if extra else ""
        return (
            f"{angle.capitalize() if angle else 'First-person'} POV shot. {desc} "
            "The camera is the subject's own eyes — the bear is NOT visible as a full figure. "
            "At most a blurred paw or partial silhouette at the very edge of frame if the shot "
            "description mentions one, otherwise show only the environment racing past. "
            "No full bear body, no face, no emblem — this is a POV / first-person frame."
            f"{suffix} {STYLE_TAIL}"
        )

    lead = f"{angle.capitalize()} cinematic shot." if angle else "Cinematic shot."
    scale = _shot_scale(angle, desc)
    coverage_override = shot_override.get("coverage_hint_override")
    coverage_hint = coverage_override if isinstance(coverage_override, str) and coverage_override else {
        "wide":   "The bear occupies at least ~15% of the frame area (readable, not a tiny distant silhouette).",
        "medium": "The bear occupies roughly ~30% of the frame area, legible mid-body framing.",
        "close":  "The bear fills ~60%+ of the frame — face/emblem clearly legible.",
    }[scale]
    subject = (
        "EXACTLY ONE bear in the entire frame — the small chibi-proportioned anime bear "
        "from the character reference, on-model, wearing the dark hoodie. "
        "Never draw a second bear, a duplicate bear, a bear inside a mirror or reflection, "
        "a bear inside a photograph or memory fragment, a bear silhouette, or any "
        "additional bear-shaped element anywhere in the composition. "
        "The bear is CHIBI-proportioned — head roughly half the body height, stubby arms "
        "and legs, rounded torso. NEVER render a tall, adult, realistic, or human-proportioned bear. "
        "Match the character reference exactly: large head, short limbs, ~2 heads tall total. "
        "The hood is DOWN with both rounded ears fully visible above the head, UNLESS the "
        "shot description explicitly says 'hood up'. "
        "The blue beaded necklace and dark navy zippered hoodie are always present and visible. "
        "Muzzle is mauve/pink with a dark nose button, eyes are large solid GLOWING RED-ORANGE "
        "(never amber, never brown, never yellow). "
        f"{coverage_hint} "
        "The bear's POSE and FACING must match the shot description — if the description says "
        "'faces camera' or 'front-facing', the bear is front-facing, not back-turned. "
        "The crescent moon emblem sits flush on his FOREHEAD ONLY — "
        "it is skin/fur marking, NOT a floating object. "
        "If the forehead is not visible in frame (back-turned, profile, hooded), "
        "the emblem is hidden — NEVER draw a moon floating above his head or behind him. "
        "The sky must contain NO moon and NO crescent shapes; if the environment "
        "reference shows a moon, OMIT it and replace with clouds, aurora, or open sky. "
        "Environment matches the environment reference in layout and lighting only."
    )
    extra_val = (shot_override.get("anchor_extra") or "").strip()
    extra = (" " + extra_val) if extra_val else ""
    # Profile-driven anchor technique constraint (cel-shading, realism, etc.)
    anchor_constraint = f" {STYLE_ANCHOR_CONSTRAINT}" if STYLE_ANCHOR_CONSTRAINT else ""
    return f"{lead} {desc} {subject}{extra}{anchor_constraint} {STYLE_TAIL}"


def _check_banned_sole_verb(scene: dict) -> str:
    """Return reason string if the scene's only action is a banned sole-verb,
    else empty string. Profile-driven (task #69). Reads scene.acting first,
    falls back to shotDescription scan."""
    if not BANNED_SOLE_VERBS:
        return ""
    acting = (scene.get("acting") or "").strip().lower()
    desc = (scene.get("shotDescription") or "").strip().lower()
    # 1. explicit acting field
    if acting and acting in BANNED_SOLE_VERBS:
        return f"scene.acting='{acting}' is a banned sole-verb — profile demands a grounded acting beat"
    # 2. description-only fallback: action verb = first word of desc
    if not acting and desc:
        first = desc.split(",")[0].split(".")[0].split()
        if first:
            v = first[0].rstrip("s").lower()
            if any(b.rstrip("s") == v for b in BANNED_SOLE_VERBS):
                return f"shotDescription leads with banned sole-verb '{first[0]}' — profile demands a grounded acting beat"
    return ""


_OVERRIDES_CACHE: dict | None = None


def _load_overrides() -> dict:
    global _OVERRIDES_CACHE
    if _OVERRIDES_CACHE is not None:
        return _OVERRIDES_CACHE
    if not os.path.isfile(OVERRIDES_JSON):
        _OVERRIDES_CACHE = {}
        return _OVERRIDES_CACHE
    try:
        with open(OVERRIDES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        _OVERRIDES_CACHE = data.get("overrides", {}) or {}
    except (json.JSONDecodeError, IOError):
        _OVERRIDES_CACHE = {}
    return _OVERRIDES_CACHE


def _build_kling_prompt(scene: dict) -> tuple[str, str]:
    """Return (prompt, extra_negative). Overrides win when present.

    Non-override path leads with scene.shotDescription (subject action) as the
    primary verb — fixes the 'TB just stands' bug where the old camera-only
    prompt gave Kling nothing to animate on the subject.
    """
    overrides = _load_overrides()
    sid = scene.get("id", "")
    if sid in overrides:
        o = overrides[sid]
        prompt = (o.get("kling_prompt") or "").strip()
        neg = (o.get("negative_extra") or "").strip()
        if prompt:
            return prompt + KLING_GUARDRAIL, neg

    action = (scene.get("shotDescription") or "").strip()
    move = (scene.get("cameraMovement") or "").strip()
    energy = scene.get("energy") or 5

    parts: list[str] = []
    if action:
        parts.append(action)
    if move:
        parts.append(f"Camera: {move}.")
    else:
        parts.append("Camera holds steady.")

    if energy >= 8:
        parts.append("Subject drives the action with clear body motion.")
    elif energy <= 3:
        parts.append("Subject still, atmosphere carries the beat.")

    parts.append("Makoto Shinkai anime realism, cel shading, painterly clouds, on-model.")
    return " ".join(parts) + KLING_GUARDRAIL, ""


def _scene_key(name: str) -> str:
    return (name or "").split(" ", 1)[0].strip()


def _banner(msg: str):
    print("\n" + "=" * 72)
    print("  " + msg)
    print("=" * 72)


def _render_one(scene: dict, env_path: str) -> dict:
    shot_id = scene["id"]
    name = scene["name"]
    duration = int(scene.get("duration") or 6)
    duration = max(3, min(15, duration))

    anchor_dir = os.path.join(ANCHORS_DIR, shot_id)
    clip_dir   = os.path.join(CLIPS_DIR, shot_id)
    os.makedirs(anchor_dir, exist_ok=True)
    os.makedirs(clip_dir, exist_ok=True)

    anchor_dst = os.path.join(anchor_dir, "selected.png")
    clip_dst   = os.path.join(clip_dir, "selected.mp4")

    result = {
        "id": shot_id, "name": name, "duration": duration,
        "anchor": None, "clip": None,
        "anchor_elapsed": 0.0, "clip_elapsed": 0.0,
        "status": "pending",
    }

    overrides = _load_overrides()
    shot_override = overrides.get(shot_id, {})
    needs_anchor_regen = bool(shot_override.get("needs_anchor_regen"))

    is_forced = FORCE_REGEN or FORCE_CLIP_ONLY or shot_id in REGEN_IDS
    force_anchor = FORCE_ANCHOR_REGEN or needs_anchor_regen or (is_forced and not FORCE_CLIP_ONLY)
    # Clip-exists skip only applies when we'd actually re-render the clip. With
    # ANCHOR_ONLY we never touch the clip, so an existing clip is not a reason
    # to skip an anchor regen.
    if not ANCHOR_ONLY and os.path.isfile(clip_dst) and not is_forced:
        result["status"] = "skipped_exists"
        result["anchor"] = anchor_dst if os.path.isfile(anchor_dst) else None
        result["clip"] = clip_dst
        print(f"  SKIP  already rendered  ->  {clip_dst}")
        return result
    if is_forced and os.path.isfile(clip_dst):
        os.remove(clip_dst)
        print(f"  FORCE regen — removed existing clip")
    if force_anchor and os.path.isfile(anchor_dst):
        os.remove(anchor_dst)
        reason = ("needs_anchor_regen" if needs_anchor_regen
                  else "LUMN_FORCE_ANCHOR_REGEN" if FORCE_ANCHOR_REGEN
                  else "LUMN_FORCE_REGEN")
        print(f"  FORCE regen — removed existing anchor ({reason})")

    anchor_prompt = _build_anchor_prompt(scene)
    kling_prompt, kling_negative_extra = _build_kling_prompt(scene)

    print(f"  anchor prompt: {anchor_prompt[:100]}...")
    print(f"  kling  prompt: {kling_prompt[:140]}{'...' if len(kling_prompt) > 140 else ''}")
    if kling_negative_extra:
        print(f"  kling  neg+  : {kling_negative_extra}")

    # Pre-flight Kling lint (~$0, word count / sound words / banned terms)
    lint = lint_kling_prompt(kling_prompt, strict=PREFLIGHT_STRICT)
    result["kling_lint"] = lint
    errs = [i for i in lint.get("issues", []) if i["severity"] == "error"]
    warns = [i for i in lint.get("issues", []) if i["severity"] == "warn"]
    for w in warns:
        print(f"  [lint warn] {w['message']}")
    if errs:
        print(f"  [lint FAIL] {'; '.join(e['message'] for e in errs[:3])}")
        if PREFLIGHT_STRICT and not ANCHOR_ONLY:
            result["status"] = f"lint_failed: {errs[0]['rule']}"
            return result

    # Anchor
    if not os.path.isfile(anchor_dst):
        print(f"  [1/2] Gemini edit (2K, 16:9)...")
        t0 = time.time()
        try:
            paths = _with_retry("gemini_edit", lambda: gemini_edit_image(
                prompt=anchor_prompt,
                reference_image_paths=[env_path, TB_SHEET, TB_FACE],
                resolution="2K", num_images=1, aspect_ratio="16:9",
            ))
        except Exception as e:
            result["status"] = f"anchor_error: {e}"
            print(f"  !! anchor error: {e}")
            return result
        result["anchor_elapsed"] = time.time() - t0
        if not paths:
            result["status"] = "anchor_no_paths"
            print(f"  !! anchor returned no paths")
            return result
        shutil.copy2(paths[0], anchor_dst)
        print(f"        OK  ({result['anchor_elapsed']:.1f}s)")
    else:
        print(f"  [1/2] anchor exists, reusing")
    result["anchor"] = anchor_dst

    # Pre-Kling anchor vision audit (~$0.01 per shot)
    if not SKIP_AUDIT:
        print(f"  [audit] anchor vision QA...")
        try:
            verdict = audit_scene_anchor(scene, anchor_dst)
            result["anchor_audit"] = verdict
            vs = verdict.get("violations", []) or []
            if verdict.get("pass"):
                print(f"          OK — {verdict.get('summary','')[:80]}")
            else:
                codes = ",".join(v.get("code", "?") for v in vs[:3])
                print(f"          FAIL ({codes}) — {verdict.get('summary','')[:80]}")
                if PREFLIGHT_STRICT:
                    result["status"] = f"audit_failed: {codes}"
                    return result
                else:
                    print(f"          (non-strict mode: continuing to Kling)")
        except Exception as e:
            print(f"  [audit] error: {e}")

    if ANCHOR_ONLY:
        result["status"] = "anchor_only"
        print(f"  LUMN_ANCHOR_ONLY set — skipping Kling.")
        return result

    # Kling
    print(f"  [2/2] Kling V3 Pro I2V ({duration}s)...")
    t0 = time.time()
    full_neg = NEGATIVE_BASE + (", " + kling_negative_extra if kling_negative_extra else "")
    try:
        clip_path = _with_retry("kling_i2v", lambda: kling_image_to_video(
            start_image_path=anchor_dst,
            prompt=kling_prompt,
            duration=duration,
            tier=KLING_TIER,
            aspect_ratio="16:9",
            cfg_scale=0.5,
            negative_prompt=full_neg,
        ))
    except Exception as e:
        result["status"] = f"clip_error: {e}"
        print(f"  !! clip error: {e}")
        return result
    result["clip_elapsed"] = time.time() - t0
    if not clip_path or not os.path.isfile(clip_path):
        result["status"] = "clip_no_path"
        print(f"  !! clip returned no path")
        return result
    shutil.copy2(clip_path, clip_dst)
    result["clip"] = clip_dst

    # Post-render structural critic (free — ffprobe only)
    try:
        crit = critique_clip(
            {"shot_id": shot_id, "duration": duration, "video_prompt": kling_prompt},
            {},
            clip_dst,
        )
        result["clip_critique"] = {
            "overall_pass": crit.get("overall_pass"),
            "checks": {k: {"pass": v.get("pass"), "detail": v.get("detail")} for k, v in (crit.get("checks") or {}).items()},
            "suggestions": crit.get("suggestions", [])[:3],
        }
        if not crit.get("overall_pass"):
            sug = crit.get("suggestions", [])[:2]
            print(f"  [critic] FAIL — {sug}")
            if PREFLIGHT_STRICT:
                result["status"] = "clip_critic_failed"
                return result
        else:
            info = crit.get("clip_info", {})
            print(f"  [critic] OK — {info.get('width','?')}x{info.get('height','?')} "
                  f"{info.get('duration',0):.1f}s {info.get('size_kb',0)}KB")
    except Exception as e:
        print(f"  [critic] error: {e}")

    result["status"] = "ok"
    print(f"        OK  ({result['clip_elapsed']:.1f}s)  ->  {clip_dst}")
    return result


def _kling_cost(duration: int) -> float:
    return round(0.112 * duration, 4)


def _estimate_total_cost(scenes: list) -> float:
    total = 0.0
    for s in scenes:
        clip = os.path.join(CLIPS_DIR, s["id"], "selected.mp4")
        if os.path.isfile(clip):
            continue
        total += 0.04 + _kling_cost(int(s.get("duration") or 6))
    return total


def main():
    for p in (TB_SHEET, TB_FACE):
        if not os.path.isfile(p):
            print(f"MISSING reference: {p}")
            sys.exit(1)

    with open(SCENES_JSON, "r", encoding="utf-8") as f:
        scenes = json.load(f)
    scenes = sorted(scenes, key=lambda x: _scene_key(x.get("name", "")))
    if ONLY_IDS:
        scenes = [s for s in scenes if s.get("id") in ONLY_IDS]
        print(f"ONLY_IDS filter: rendering {len(scenes)} shot(s) out of 26")

    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_path = os.path.join(RUNS_DIR, f"{stamp}.json")

    remaining = [
        s for s in scenes
        if not os.path.isfile(os.path.join(CLIPS_DIR, s["id"], "selected.mp4"))
    ]
    est_cost = _estimate_total_cost(scenes)

    _banner(f"BATCH RENDER — {len(scenes)} total, {len(remaining)} to render")
    print(f"  estimated spend (remaining):  ${est_cost:.2f}")
    print(f"  run manifest:                 {run_path}")

    results = []
    batch_t0 = time.time()
    for idx, scene in enumerate(scenes, 1):
        env_id = scene.get("environmentId") or ""
        env_path = _find_env_preview(env_id)
        if not env_path:
            print(f"\n[{idx}/{len(scenes)}] {scene['name']}  -- MISSING env {env_id}")
            results.append({
                "id": scene["id"], "name": scene["name"],
                "status": f"missing_env:{env_id}",
            })
            continue

        _banner(
            f"[{idx}/{len(scenes)}] {scene['name']}  "
            f"(id={scene['id']}, {scene.get('duration','?')}s)"
        )
        print(f"  env: {os.path.basename(env_path)}")

        # Profile-driven sole-verb gate (task #69)
        banned_reason = _check_banned_sole_verb(scene)
        if banned_reason:
            if PREFLIGHT_STRICT:
                print(f"  [GATE FAIL] {banned_reason}")
                results.append({
                    "id": scene["id"], "name": scene["name"],
                    "status": f"banned_sole_verb:{banned_reason[:80]}",
                })
                continue
            else:
                print(f"  [GATE WARN] {banned_reason} (non-strict: proceeding)")

        r = _render_one(scene, env_path)
        results.append(r)

        with open(run_path, "w", encoding="utf-8") as f:
            json.dump({
                "started": stamp,
                "elapsed_sec": round(time.time() - batch_t0, 1),
                "results": results,
            }, f, indent=2)

    _banner("DONE")
    success_statuses = {"ok", "anchor_only"}
    ok = sum(1 for r in results if r.get("status") in success_statuses)
    skipped = sum(1 for r in results if r.get("status") == "skipped_exists")
    fail = len(results) - ok - skipped
    elapsed = time.time() - batch_t0
    mode_note = " (anchor-only)" if ANCHOR_ONLY else ""
    print(f"  ok:      {ok}{mode_note}")
    print(f"  skipped: {skipped} (already rendered)")
    print(f"  failed:  {fail}")
    print(f"  elapsed: {elapsed/60:.1f} min")
    print(f"  manifest: {run_path}")
    if fail:
        print("\n  FAILED SHOTS:")
        for r in results:
            if r.get("status") not in (success_statuses | {"skipped_exists"}):
                print(f"    {r.get('name','?')}: {r.get('status')}")


if __name__ == "__main__":
    main()
