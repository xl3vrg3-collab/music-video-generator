"""
Video generator supporting multiple AI engines:
  - Grok (xAI) -- text-to-video, image generation + Ken Burns
  - Luma Dream Machine (Ray2) -- text-to-video and image-to-video
  - OpenAI GPT -- image generation (stills) + Ken Burns
  - Runway Gen-3 Alpha Turbo -- image-to-video and text-to-video (recommended for photo scenes)

Handles video generation requests, async polling, downloads,
and falls back to image generation + Ken Burns if video fails.
Supports photo+text style transfer via Grok image API with base64.
Supports camera movement presets for Ken Burns animations.
Supports character reference system for auto-attaching photos to prompts.
"""

import base64
import json
import os
import sys
import time
import subprocess
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://api.x.ai/v1"
LUMA_API_BASE = "https://api.lumalabs.ai"
POLL_INTERVAL = 5       # seconds between status checks
POLL_TIMEOUT = 300      # max seconds to wait for a single video
LUMA_POLL_INTERVAL = 5  # seconds between Luma status checks
LUMA_POLL_TIMEOUT = 600 # max seconds to wait for Luma (longer due to queue)
RUNWAY_API_BASE = "https://api.dev.runwayml.com/v1"
RUNWAY_POLL_INTERVAL = 5   # seconds between Runway status checks
RUNWAY_POLL_TIMEOUT = 600  # max seconds to wait for Runway
RUNWAY_COST_PER_SEC = 0.05 # ~$0.05 per second (5 credits/sec for turbo)
MAX_CONCURRENT = 3      # max parallel video generations

# ---- Moderation detection ----
_MODERATION_KEYWORDS = [
    "content_moderation", "moderation", "safety", "policy",
    "flagged", "nsfw", "inappropriate", "prohibited",
    "violates", "not allowed", "rejected",
]

def _is_moderation_error(error_str: str) -> bool:
    """Check if an error message indicates a content moderation rejection."""
    lower = error_str.lower()
    return any(kw in lower for kw in _MODERATION_KEYWORDS)

# ---- Engine names ----
ENGINE_GROK = "grok"
ENGINE_LUMA = "luma"
ENGINE_OPENAI = "openai"
ENGINE_RUNWAY = "runway"
SUPPORTED_ENGINES = [ENGINE_GROK, ENGINE_LUMA, ENGINE_OPENAI, ENGINE_RUNWAY]

# ---- Model duration options (Area 3) ----
# Per Runway API spec: duration is integer 2-10 for all Runway-hosted models.
# Luma and other engines may have different ranges.
MODEL_DURATION_OPTIONS = {
    "gen4_5": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    "gen4.5": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    "gen4_turbo": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    "veo3": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    "veo3.1": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    "veo3_1": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    "veo3.1_fast": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    "veo3_1_fast": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    "grok": [8],
    "luma": [5, 9],
    "openai": [3, 4, 5, 6, 7, 8, 9, 10, 12, 15],
}

# Smart defaults: best duration per model + section type
MODEL_SECTION_DEFAULTS = {
    "gen4_5":         {"intro": 8, "verse": 6, "chorus": 4, "bridge": 8, "outro": 8},
    "gen3a_turbo":    {"intro": 10, "verse": 5, "chorus": 5, "bridge": 10, "outro": 10},
    "kling_pro":      {"intro": 10, "verse": 10, "chorus": 5, "bridge": 10, "outro": 10},
    "kling_standard": {"intro": 10, "verse": 5, "chorus": 5, "bridge": 10, "outro": 10},
    "veo3":           {"intro": 8, "verse": 5, "chorus": 5, "bridge": 8, "outro": 8},
    "veo3_1":         {"intro": 8, "verse": 5, "chorus": 5, "bridge": 8, "outro": 8},
    "veo3_1_fast":    {"intro": 8, "verse": 5, "chorus": 5, "bridge": 8, "outro": 8},
    "grok":           {"intro": 8, "verse": 8, "chorus": 8, "bridge": 8, "outro": 8},
    "luma":           {"intro": 9, "verse": 5, "chorus": 5, "bridge": 9, "outro": 9},
    "openai":         {"intro": 8, "verse": 6, "chorus": 4, "bridge": 8, "outro": 10},
}

# ---- Shot type reference priority system ----
# Values are weights 0.0-1.0 for how strongly each ref type should influence generation
SHOT_TYPE_REF_PRIORITY = {
    "close-up": {
        "character": 1.0,    # highest — face/identity critical
        "costume": 0.4,      # secondary — visible but not focus
        "environment": 0.1,  # minimal — background blur
    },
    "medium": {
        "character": 0.9,
        "costume": 0.8,
        "environment": 0.5,
    },
    "full": {
        "character": 0.8,
        "costume": 0.9,
        "environment": 0.6,
    },
    "wide": {
        "character": 0.4,
        "costume": 0.3,
        "environment": 1.0,  # highest — world-building
    },
    "establishing": {
        "character": 0.2,
        "costume": 0.1,
        "environment": 1.0,  # highest
    },
}


def select_refs_for_shot_type(shot_type: str, char_photos: list, costume_photos: list,
                              env_photos: list, max_refs: int = 3) -> list:
    """Select and prioritize reference photos based on shot type.

    Each photo entry in the input lists should be a dict with at least
    ``{"path": str, "tag": str}``.  A ``priority`` key is added by this
    function based on shot-type weights.

    Returns list of {path, tag, priority} sorted by priority descending.
    Only returns up to *max_refs* (Runway text_to_image API limit is 3).

    Guarantees:
    - For close-up shots, at least one character ref is always included.
    - For wide / establishing shots, at least one environment ref is always
      included.
    """
    shot_type = (shot_type or "medium").lower().strip()
    priorities = SHOT_TYPE_REF_PRIORITY.get(shot_type, SHOT_TYPE_REF_PRIORITY["medium"])

    candidates = []
    for p in char_photos:
        candidates.append({"path": p["path"], "tag": p["tag"],
                           "priority": priorities["character"], "type": "character"})
    for p in costume_photos:
        candidates.append({"path": p["path"], "tag": p["tag"],
                           "priority": priorities["costume"], "type": "costume"})
    for p in env_photos:
        candidates.append({"path": p["path"], "tag": p["tag"],
                           "priority": priorities["environment"], "type": "environment"})

    # Sort by priority descending, then by type preference (char > costume > env)
    type_order = {"character": 0, "costume": 1, "environment": 2}
    candidates.sort(key=lambda c: (-c["priority"], type_order.get(c["type"], 9)))

    selected = candidates[:max_refs]

    # Enforce mandatory ref for certain shot types
    if shot_type == "close-up" and char_photos:
        if not any(s["type"] == "character" for s in selected):
            # Swap last slot with the best character ref
            best_char = next(c for c in candidates if c["type"] == "character")
            selected[-1] = best_char

    if shot_type in ("wide", "establishing") and env_photos:
        if not any(s["type"] == "environment" for s in selected):
            best_env = next(c for c in candidates if c["type"] == "environment")
            selected[-1] = best_env

    # Strip internal "type" key from output, keep path/tag/priority
    return [{"path": s["path"], "tag": s["tag"], "priority": s["priority"]} for s in selected]


def build_shot_prompt(shot_type: str, scene_prompt: str,
                      has_char_ref: bool, has_costume_ref: bool,
                      has_env_ref: bool) -> str:
    """Build a generation prompt optimized for the shot type.

    For close-ups: focus on facial detail, expression, skin texture.
    For medium: balanced character + scene description.
    For wide / establishing: focus on composition, environment, atmosphere.

    When refs exist the prompt avoids over-describing identity (the photo
    handles that).
    """
    shot_type = (shot_type or "medium").lower().strip()

    # Quality suffix applied to all prompts
    quality = "Hyper-realistic, photorealistic, 8K UHD, cinematic lighting, sharp focus, professional cinematography."

    # Shot-type specific modifiers — stronger identity language for better ref adherence
    if shot_type == "close-up":
        framing = "Extreme close-up shot. Detailed skin texture, visible pores, catch-light in eyes, shallow depth of field, 85mm portrait lens bokeh."
        if has_char_ref:
            framing += " CRITICAL: PRESERVE EXACT LIKENESS from @Character reference — identical face shape, identical features, identical proportions, identical skin tone. Do NOT alter or stylize the face."
    elif shot_type == "medium":
        framing = "Medium shot, waist-up framing, balanced composition, 50mm lens."
        if has_char_ref:
            framing += " PRESERVE EXACT LIKENESS from @Character reference — same face, same build, same features."
        if has_costume_ref:
            framing += " Outfit matches @Costume reference exactly."
    elif shot_type == "full":
        framing = "Full body shot, head-to-toe framing, character centered, 35mm lens, full figure visible."
        if has_costume_ref:
            framing += " Show the COMPLETE outfit clearly — every detail from @Costume reference."
        if has_char_ref:
            framing += " PRESERVE EXACT LIKENESS from @Character reference."
    elif shot_type == "wide":
        framing = "Wide shot, 24mm lens, expansive environment fills frame, character small in frame, cinematic composition, atmosphere, depth."
        if has_env_ref:
            framing += " Match the EXACT environment from @Setting reference — same architecture, materials, lighting, atmosphere."
    elif shot_type == "establishing":
        framing = "Establishing shot, 16mm ultra-wide, sweeping vista, grand scale, environmental storytelling, dramatic atmosphere, no characters in foreground."
        if has_env_ref:
            framing += " Match the EXACT location from @Setting reference — same environment, same lighting, same mood."
    else:
        framing = "Cinematic shot, professional cinematography."

    # Clean scene prompt — strip redundant quality keywords that we'll add ourselves
    import re as _re_shot
    cleaned = scene_prompt
    cleaned = _re_shot.sub(r'(?i)\b(hyper[- ]?realistic|photorealistic|8k|4k)\b', '', cleaned)
    cleaned = _re_shot.sub(r'\s{2,}', ' ', cleaned).strip()

    prompt = f"{framing} {cleaned} {quality}"
    return prompt[:1000]  # API limit


def get_valid_duration(engine: str, requested_duration: int) -> int:
    """
    Snap a requested duration to the nearest valid duration for the given model.

    Args:
        engine: model/engine ID (e.g. "gen4_5", "grok", "luma")
        requested_duration: desired duration in seconds

    Returns:
        nearest valid duration for that model
    """
    options = MODEL_DURATION_OPTIONS.get(engine)
    if not options:
        # Fallback: try common durations
        options = [5, 10]
    # Find the closest valid duration
    return min(options, key=lambda d: abs(d - requested_duration))


def get_smart_duration(engine: str, section_type: str) -> int:
    """
    Get the best default duration for a model + section type combo.

    Args:
        engine: model/engine ID
        section_type: "intro", "verse", "chorus", "bridge", "outro"

    Returns:
        recommended duration in seconds
    """
    section_defaults = MODEL_SECTION_DEFAULTS.get(engine, {})
    return section_defaults.get(section_type, get_valid_duration(engine, 8))


# ---- Scene-to-scene continuity (Area 1) ----

def _build_continuity_prompt(scene: dict, index: int) -> str:
    """
    Build a continuity suffix for a scene prompt based on previous scene context.
    Only applies when continuity_mode is enabled and index > 0.

    Reads the continuity_context from the scene dict (set by scene planner or UI).

    Returns:
        continuity suffix string to append to the prompt, or empty string.
    """
    if index == 0:
        return ""

    ctx = scene.get("continuity_context", {})
    if not ctx:
        return ""

    parts = []
    parts.append("Continuing from the previous scene. Maintain visual continuity: same color palette, same lighting, same time of day.")

    if ctx.get("has_character"):
        parts.append("The same character from the previous scene continues to appear.")

    if ctx.get("key_elements"):
        elements = ctx["key_elements"][:5]
        parts.append(f"Maintain these visual elements: {', '.join(elements)}.")

    if ctx.get("lighting"):
        parts.append(f"Lighting: {ctx['lighting']}.")

    if ctx.get("color_palette"):
        parts.append(f"Color palette: {ctx['color_palette']}.")

    if ctx.get("character_state"):
        parts.append(f"Character: {ctx['character_state']}.")

    return " ".join(parts)

# ---- Camera movement presets for Ken Burns ----
# Each preset maps to an ffmpeg crop animation expression
# Format: (crop_x_expr, crop_y_expr) applied on a 3840x2160 -> 1920x1080 crop
CAMERA_PRESETS = {
    "static": {
        "desc": "No camera movement",
        "crop_x": "'960'",
        "crop_y": "'540'",
    },
    "pan_left": {
        "desc": "Camera slowly panning left",
        "crop_x": f"'1920-1920*min(t/{{dur}},1)'",
        "crop_y": "'540'",
    },
    "pan_right": {
        "desc": "Camera slowly panning right",
        "crop_x": f"'1920*min(t/{{dur}},1)'",
        "crop_y": "'540'",
    },
    "zoom_in": {
        "desc": "Camera slowly zooming in",
        "crop_x": f"'960-960*min(t/{{dur}},1)'",
        "crop_y": f"'540-540*min(t/{{dur}},1)'",
    },
    "zoom_out": {
        "desc": "Camera slowly zooming out from center",
        "crop_x": f"'960*min(t/{{dur}},1)'",
        "crop_y": f"'540*min(t/{{dur}},1)'",
    },
    "orbit": {
        "desc": "Camera orbiting around subject",
        "crop_x": f"'960+480*sin(2*PI*t/{{dur}})'",
        "crop_y": f"'540+270*cos(2*PI*t/{{dur}})'",
    },
    "tracking": {
        "desc": "Camera tracking diagonally",
        "crop_x": f"'1920*min(t/{{dur}},1)'",
        "crop_y": f"'1080*min(t/{{dur}},1)'",
    },
}

# Camera movement prompt suffixes for AI generation
CAMERA_PROMPT_SUFFIXES = {
    "static": "",
    "pan_left": ", camera slowly panning left",
    "pan_right": ", camera slowly panning right",
    "zoom_in": ", camera slowly zooming in",
    "zoom_out": ", camera slowly zooming out",
    "orbit": ", camera orbiting around subject",
    "tracking": ", camera tracking shot following subject",
}


def _get_api_key() -> str:
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY environment variable is not set")
    return key


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
    }


# ---- Luma Dream Machine (Ray2) API ----

def _get_luma_api_key() -> str:
    key = os.environ.get("LUMA_API_KEY", "")
    if not key:
        raise RuntimeError(
            "LUMA_API_KEY environment variable is not set. "
            "Get an API key from https://lumalabs.ai and add it to your .env file."
        )
    return key


def _luma_headers() -> dict:
    """Auth headers for Luma Dream Machine API."""
    return {
        "Authorization": f"Bearer {_get_luma_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _luma_submit_video(prompt: str, image_url: str = None,
                       duration: int = 5,
                       first_frame_url: str = None,
                       last_frame_url: str = None) -> str:
    """
    Submit a video generation request to Luma Dream Machine (Ray2).

    Args:
        prompt: text description of the video
        image_url: optional URL to an image for image-to-video (legacy, sets frame0)
        duration: video duration -- "5s" or "9s" (Luma supports 5s and 9s)
        first_frame_url: optional data URI or URL for first frame keyframe
        last_frame_url: optional data URI or URL for last frame keyframe

    Returns:
        generation ID string
    """
    # Clamp duration to Luma-supported values
    dur_str = "9s" if duration > 6 else "5s"

    payload = {
        "prompt": prompt,
        "model": "ray-2",
        "resolution": "1080p",
        "duration": dur_str,
    }

    # Build keyframes dict: first_frame/last_frame take priority over legacy image_url
    keyframes = {}
    if first_frame_url:
        keyframes["frame0"] = {"type": "image", "url": first_frame_url}
    elif image_url:
        keyframes["frame0"] = {"type": "image", "url": image_url}
    if last_frame_url:
        keyframes["frame1"] = {"type": "image", "url": last_frame_url}
    if keyframes:
        payload["keyframes"] = keyframes

    print(f"[LUMA] Submitting generation: prompt={prompt[:80]}..., "
          f"image={'yes' if image_url else 'no'}, duration={dur_str}")

    resp = requests.post(
        f"{LUMA_API_BASE}/dream-machine/v1/generations",
        headers=_luma_headers(),
        json=payload,
        timeout=30,
    )

    if resp.status_code != 200 and resp.status_code != 201:
        print(f"[LUMA] Submit error {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()

    data = resp.json()
    gen_id = data.get("id", "")
    print(f"[LUMA] Generation submitted: id={gen_id}")
    return gen_id


def _luma_poll(generation_id: str) -> dict:
    """
    Poll Luma Dream Machine until video is completed or failed.

    Returns:
        dict with video info including download URL
    """
    deadline = time.time() + LUMA_POLL_TIMEOUT
    while time.time() < deadline:
        resp = requests.get(
            f"{LUMA_API_BASE}/dream-machine/v1/generations/{generation_id}",
            headers=_luma_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("state", data.get("status", ""))
        print(f"[LUMA] Poll {generation_id[:12]}... status={status}")

        if status == "completed":
            # Extract video URL from response
            assets = data.get("assets", {})
            video_url = assets.get("video", "")
            if not video_url:
                # Try alternate response shapes
                video_url = data.get("video", {}).get("url", "")
            if not video_url:
                video_url = data.get("download_url", "")
            if not video_url:
                raise RuntimeError(
                    f"Luma generation completed but no video URL found in response: "
                    f"{json.dumps(data)[:500]}"
                )
            return {"url": video_url, "generation_id": generation_id}
        elif status in ("failed", "error"):
            failure_reason = data.get("failure_reason", data.get("error", "unknown"))
            raise RuntimeError(
                f"Luma generation failed: {failure_reason}"
            )

        time.sleep(LUMA_POLL_INTERVAL)

    raise TimeoutError(
        f"Luma generation timed out after {LUMA_POLL_TIMEOUT}s "
        f"(id={generation_id})"
    )


def _runway_upload_file(file_path: str) -> str:
    """Upload a file to Runway's upload service and return a runway:// URI.

    This avoids base64 encoding large files in API calls.
    Falls back to data URI if upload fails.
    """
    if not os.path.isfile(file_path):
        return ""

    filename = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    # Only use uploads API for files > 1MB (smaller files are fine as data URIs)
    if file_size < 1024 * 1024:
        return _photo_to_data_uri(file_path)

    try:
        # Step 1: Get upload URL
        resp = requests.post(
            f"{RUNWAY_API_BASE}/uploads",
            headers=_runway_headers(),
            json={"filename": filename, "type": "ephemeral"},
            timeout=30,
        )

        if resp.status_code not in (200, 201):
            print(f"[RUNWAY/upload] Failed to get upload URL: {resp.status_code} {resp.text[:200]}")
            return _photo_to_data_uri(file_path)

        data = resp.json()
        upload_url = data.get("uploadUrl", "")
        fields = data.get("fields", {})
        runway_uri = data.get("runwayUri", "")

        if not upload_url or not runway_uri:
            print(f"[RUNWAY/upload] Missing uploadUrl or runwayUri in response")
            return _photo_to_data_uri(file_path)

        # Step 2: Upload the file (multipart form to the presigned URL)
        with open(file_path, "rb") as f:
            file_data = f.read()

        # Build multipart form with fields + file
        upload_fields = dict(fields)
        files = {"file": (filename, file_data)}

        upload_resp = requests.post(upload_url, data=upload_fields, files=files, timeout=120)

        if upload_resp.status_code not in (200, 201, 204):
            print(f"[RUNWAY/upload] File upload failed: {upload_resp.status_code}")
            return _photo_to_data_uri(file_path)

        print(f"[RUNWAY/upload] Uploaded {filename} ({file_size/1024:.0f}KB) -> {runway_uri}")
        return runway_uri

    except Exception as e:
        print(f"[RUNWAY/upload] Error: {e}, falling back to data URI")
        return _photo_to_data_uri(file_path)


def _photo_to_data_uri(photo_path: str) -> str:
    """Convert a local photo to a base64 data URI string."""
    with open(photo_path, "rb") as f:
        photo_bytes = f.read()
    ext = os.path.splitext(photo_path)[1].lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    }
    mime = mime_map.get(ext, "image/jpeg")
    # Safety: downscale if file is too large for data URI (Runway ~5MB limit)
    MAX_FILE_SIZE = 4 * 1024 * 1024  # 4MB to be safe
    if len(photo_bytes) > MAX_FILE_SIZE:
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(photo_bytes)) as img:
                # Downscale to fit within size limit
                scale = (MAX_FILE_SIZE / len(photo_bytes)) ** 0.5
                new_w = int(img.width * scale)
                new_h = int(img.height * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92)
                photo_bytes = buf.getvalue()
                mime = "image/jpeg"
                print(f"[REF ASSET] Downscaled {os.path.basename(photo_path)} to {new_w}x{new_h} ({len(photo_bytes)/1024:.0f}KB) for API limit")
        except Exception as e:
            print(f"[REF ASSET] WARNING: Could not downscale large photo: {e}")
    b64_data = base64.b64encode(photo_bytes).decode("ascii")
    data_uri = f"data:{mime};base64,{b64_data}"
    # Diagnostic: log actual dimensions and size being sent
    file_kb = len(photo_bytes) / 1024
    uri_chars = len(data_uri)
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(photo_bytes)) as img:
            print(f"[REF ASSET] {os.path.basename(photo_path)}: {img.width}x{img.height}, {file_kb:.0f}KB file, {uri_chars:,} chars data URI")
    except Exception:
        print(f"[REF ASSET] {os.path.basename(photo_path)}: {file_kb:.0f}KB file, {uri_chars:,} chars data URI")
    if uri_chars > 5242880:
        print(f"[REF ASSET] WARNING: data URI exceeds 5.2M char limit! {uri_chars:,} chars")
    return data_uri


def _luma_generate_scene(scene: dict, output_dir: str, index: int,
                         progress_cb=None, cost_cb=None,
                         photo_path: str = None) -> str:
    """
    Generate a video clip using Luma Dream Machine (Ray2).

    Args:
        scene: dict with at least {prompt, duration}
        output_dir: directory to save clips
        index: scene index for naming
        progress_cb: optional callable(index, status_str)
        cost_cb: optional callable(scene_key, gen_type)
        photo_path: optional photo path for image-to-video

    Returns:
        path to the generated video clip
    """
    clip_path = os.path.join(output_dir, f"clip_{index:03d}.mp4")
    prompt = scene["prompt"]
    duration = scene.get("duration", 8)
    camera = scene.get("camera_movement", "zoom_in")

    # Add camera movement to prompt for Luma
    camera_suffix = CAMERA_PROMPT_SUFFIXES.get(camera, "")
    gen_prompt = prompt + camera_suffix if camera_suffix else prompt

    # Area 1: Add continuity context if enabled
    if scene.get("continuity_mode", True):
        continuity = _build_continuity_prompt(scene, index)
        if continuity:
            gen_prompt = f"{gen_prompt}. {continuity}"

    def _report(msg):
        print(f"[LUMA][{index}] {msg}")
        if progress_cb:
            progress_cb(index, msg)

    def _record(gen_type):
        if cost_cb:
            cost_cb(str(scene.get("id", index)), gen_type)

    image_url = None
    if photo_path and os.path.isfile(photo_path):
        # Luma needs an image URL -- try data URI first
        _report("encoding photo for Luma image-to-video...")
        image_url = _photo_to_data_uri(photo_path)

    # Keyframe support: first_frame_path / last_frame_path
    first_frame_url = None
    last_frame_url = None
    ff_path = scene.get("first_frame_path")
    lf_path = scene.get("last_frame_path")
    if ff_path and os.path.isfile(ff_path):
        _report("encoding first_frame keyframe for Luma...")
        first_frame_url = _photo_to_data_uri(ff_path)
    if lf_path and os.path.isfile(lf_path):
        _report("encoding last_frame keyframe for Luma...")
        last_frame_url = _photo_to_data_uri(lf_path)

    try:
        _report("submitting to Luma Dream Machine (Ray2)...")
        gen_id = _luma_submit_video(gen_prompt, image_url=image_url,
                                    duration=duration,
                                    first_frame_url=first_frame_url,
                                    last_frame_url=last_frame_url)
        _report(f"polling Luma (id={gen_id[:12]}...)")
        video_info = _luma_poll(gen_id)
        _report("downloading Luma video...")
        _download(video_info["url"], clip_path)
        _record("video")
        _report("done (Luma Ray2)")
        return clip_path
    except Exception as e:
        _report(f"Luma video failed ({e})")

        # If Luma fails with data URI for image, retry without image
        if image_url and "data:" in str(image_url)[:10]:
            _report("retrying Luma text-only (without image)...")
            try:
                gen_id = _luma_submit_video(gen_prompt, image_url=None,
                                            duration=duration)
                _report(f"polling Luma text-only (id={gen_id[:12]}...)")
                video_info = _luma_poll(gen_id)
                _report("downloading Luma video...")
                _download(video_info["url"], clip_path)
                _record("video")
                _report("done (Luma Ray2 text-only)")
                return clip_path
            except Exception as e2:
                _report(f"Luma text-only also failed ({e2})")

        # Fall back to Grok image + Ken Burns
        _report("falling back to Grok image + Ken Burns...")
        try:
            img_url = _generate_image(gen_prompt)
            img_path = os.path.join(output_dir, f"img_{index:03d}.png")
            _download(img_url, img_path)
            _ken_burns(img_path, clip_path, duration, camera=camera)
            _record("image")
            _report("done (Grok image fallback)")
            return clip_path
        except Exception as e3:
            _report(f"all generation attempts failed: {e3}")
            raise RuntimeError(
                f"Scene {index} Luma generation failed entirely: {e3}"
            ) from e3


# ---- Runway Gen-3 Alpha Turbo ----

def _get_runway_api_key() -> str:
    key = os.environ.get("RUNWAY_API_KEY", "")
    if not key:
        raise RuntimeError(
            "RUNWAY_API_KEY environment variable is not set. "
            "Get an API key from https://dev.runwayml.com and add it to your .env file."
        )
    return key


def _runway_headers() -> dict:
    """Auth headers for Runway API."""
    return {
        "Authorization": f"Bearer {_get_runway_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Runway-Version": "2024-11-06",
    }

# Runway uses pixel dimensions as ratio, not aspect ratio strings
# Ratios accepted by image_to_video (Gen 4.5, Veo, etc.)
IMAGE_TO_VIDEO_RATIO_MAP = {
    "16:9": "1280:720",
    "9:16": "720:1280",
    "4:3": "1104:832",
    "3:4": "832:1104",
    "1:1": "960:960",
    "4:5": "832:1104",
    "21:9": "1584:672",
    # Direct valid values
    "1280:720": "1280:720",
    "720:1280": "720:1280",
    "1104:832": "1104:832",
    "960:960": "960:960",
    "832:1104": "832:1104",
    "1584:672": "1584:672",
    # Common generated frame pixel dimensions -> nearest valid i2v ratio
    "1920:1080": "1280:720",
    "1536:864": "1280:720",
    "1080:1920": "720:1280",
    "864:1536": "720:1280",
    "1024:1024": "960:960",
    "1536:1536": "960:960",
    "1536:1152": "1104:832",
    "1152:1536": "832:1104",
}

# text_to_video ONLY accepts these two ratios per API spec
TEXT_TO_VIDEO_RATIO_MAP = {
    "16:9": "1280:720",
    "9:16": "720:1280",
    "1280:720": "1280:720",
    "720:1280": "720:1280",
}

def _get_ratio_for_endpoint(endpoint: str, ratio: str = "16:9") -> str:
    """Return the correct ratio string based on endpoint type."""
    if endpoint == "text_to_video":
        return TEXT_TO_VIDEO_RATIO_MAP.get(ratio, "1280:720")
    return IMAGE_TO_VIDEO_RATIO_MAP.get(ratio, "1280:720")



def _runway_generate_scene_image(prompt: str, reference_photos: list,
                                  ratio: str = "1280:720",
                                  model: str = "gen4_image",
                                  seed: int = None) -> str:
    """
    Generate a scene image using text_to_image with @tag referenceImages.

    Per API spec: text_to_image supports up to 3 referenceImages with tags.
    Use @Tag in promptText to reference specific photos.

    Args:
        prompt: scene description with @Tag mentions (e.g. "A cinematic shot of @Character...")
        reference_photos: list of dicts [{path, tag, type}, ...] max 3
            - path: local file path
            - tag: 3-16 char tag for @mention (e.g. "Character", "Costume", "Setting")
        ratio: output ratio (many options, default 1280:720)
        model: gen4_image (quality) or gen4_image_turbo (fast/cheap)

    Returns:
        path to downloaded image, or "" on failure
    """
    ref_images = []
    for ref in (reference_photos or [])[:3]:  # API max 3
        path = ref.get("path", "")
        tag = ref.get("tag", "")
        if not path or not os.path.isfile(path):
            continue
        entry = {"uri": _runway_upload_file(path)}
        if tag:
            # Tag must be 3-16 chars
            tag = tag[:16]
            if len(tag) < 3:
                tag = tag + "Ref"
            entry["tag"] = tag
        ref_images.append(entry)

    # Strip @Tag mentions from prompt if no matching refs were resolved
    clean_prompt = prompt
    if not ref_images:
        import re as _re_strip
        clean_prompt = _re_strip.sub(r'@\w+\s*', '', prompt).strip()

    payload = {
        "model": model,
        "promptText": clean_prompt[:1000],
        "ratio": ratio,
        "contentModeration": {"publicFigureThreshold": "low"},
    }
    if ref_images:
        payload["referenceImages"] = ref_images
    if seed is not None:
        payload["seed"] = seed

    print(f"[RUNWAY/text_to_image] Generating scene image with {len(ref_images)} references, "
          f"seed={seed}, tags={[r.get('tag','') for r in ref_images]}, prompt={prompt[:80]}...")

    # Retry logic for transient failures
    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{RUNWAY_API_BASE}/text_to_image",
                headers=_runway_headers(),
                json=payload,
                timeout=120,
            )

            if resp.status_code == 429:
                # Rate limited — wait and retry
                wait = min(30, 5 * (attempt + 1))
                print(f"[RUNWAY/text_to_image] Rate limited (429), waiting {wait}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue

            if resp.status_code in (500, 502, 503):
                # Server error — retry
                wait = min(20, 3 * (attempt + 1))
                print(f"[RUNWAY/text_to_image] Server error ({resp.status_code}), retrying in {wait}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue

            if resp.status_code not in (200, 201):
                err_body = resp.text[:500]
                print(f"[RUNWAY/text_to_image] Error {resp.status_code}: {err_body}")
                raise RuntimeError(f"text_to_image API {resp.status_code}: {err_body}")

            # Success — continue with polling
            break

        except requests.exceptions.Timeout:
            print(f"[RUNWAY/text_to_image] Timeout (attempt {attempt+1}/{max_retries})")
            last_error = "Request timed out"
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            raise RuntimeError(f"text_to_image timed out after {max_retries} attempts")
        except requests.exceptions.ConnectionError as e:
            print(f"[RUNWAY/text_to_image] Connection error: {e} (attempt {attempt+1}/{max_retries})")
            last_error = str(e)
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            raise RuntimeError(f"text_to_image connection failed after {max_retries} attempts")
    else:
        # All retries exhausted (loop completed without break)
        raise RuntimeError(f"text_to_image failed after {max_retries} retries: {last_error or 'server errors'}")

    data = resp.json()
    task_id = data.get("id", "")
    if not task_id:
        print(f"[RUNWAY/text_to_image] No task ID returned: {data}")
        return ""

    # Poll for completion (reuse existing poll function)
    try:
        result = _runway_poll(task_id)
        image_url = result.get("url", "")
        if image_url:
            # Download to temp file
            import tempfile
            img_path = os.path.join(tempfile.gettempdir(), f"runway_scene_{task_id[:8]}.png")
            _download(image_url, img_path)
            print(f"[RUNWAY/text_to_image] Scene image saved to {img_path}")
            return img_path
    except Exception as e:
        print(f"[RUNWAY/text_to_image] Poll/download failed: {e}")

    return ""


def _runway_video_to_video(video_path: str, prompt: str, reference_image_path: str = None) -> str:
    """Transform an existing video using gen4_aleph with an optional image reference.

    Per API spec: POST /v1/video_to_video
    - model: gen4_aleph (required)
    - videoUri: data URI of input video
    - promptText: what to change
    - references: optional [{type:"image", uri:"data:image/..."}] (max 1)

    Returns path to output video, or raises on failure.
    """
    if not video_path or not os.path.isfile(video_path):
        raise FileNotFoundError(f"Input video not found: {video_path}")

    # Read video and convert to data URI
    with open(video_path, "rb") as f:
        video_bytes = f.read()
    ext = os.path.splitext(video_path)[1].lower()
    video_mime_map = {
        ".mp4": "video/mp4", ".webm": "video/webm",
        ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    }
    video_mime = video_mime_map.get(ext, "video/mp4")
    video_b64 = base64.b64encode(video_bytes).decode("ascii")
    video_uri = f"data:{video_mime};base64,{video_b64}"

    file_kb = len(video_bytes) / 1024
    print(f"[RUNWAY/video_to_video] Input video: {os.path.basename(video_path)}, "
          f"{file_kb:.0f}KB, prompt={prompt[:80]}...")

    payload = {
        "model": "gen4_aleph",
        "videoUri": video_uri,
        "promptText": prompt[:1000],
    }

    # Add optional image reference
    if reference_image_path and os.path.isfile(reference_image_path):
        ref_uri = _photo_to_data_uri(reference_image_path)
        payload["references"] = [{"type": "image", "uri": ref_uri}]
        print(f"[RUNWAY/video_to_video] Added image reference: {os.path.basename(reference_image_path)}")

    resp = requests.post(
        f"{RUNWAY_API_BASE}/video_to_video",
        headers=_runway_headers(),
        json=payload,
        timeout=120,
    )

    if resp.status_code not in (200, 201):
        err_body = resp.text[:500]
        print(f"[RUNWAY/video_to_video] Error {resp.status_code}: {err_body}")
        raise RuntimeError(f"video_to_video API {resp.status_code}: {err_body}")

    data = resp.json()
    task_id = data.get("id", "")
    if not task_id:
        print(f"[RUNWAY/video_to_video] No task ID returned: {data}")
        raise RuntimeError("video_to_video returned no task ID")

    # Poll for completion
    result = _runway_poll(task_id)
    video_url = result.get("url", "")
    if not video_url:
        raise RuntimeError(f"video_to_video completed but no output URL (task={task_id})")

    # Download to output file
    import tempfile
    out_path = os.path.join(tempfile.gettempdir(), f"runway_v2v_{task_id[:8]}.mp4")
    _download(video_url, out_path)
    print(f"[RUNWAY/video_to_video] Output saved to {out_path}")
    return out_path


def _runway_submit_text_to_video(prompt: str, duration: int = 5,
                                  ratio: str = "16:9",
                                  model: str = "gen4.5",
                                  first_frame_path: str = None,
                                  last_frame_path: str = None,
                                  seed: int = None,
                                  **_kwargs) -> str:
    """
    Submit a text-to-video or image-to-video request to Runway.

    Per API spec (see references/runway_api_reference.md):
    - text_to_video: promptText + ratio (1280:720 or 720:1280 only) + duration (2-10)
    - image_to_video: adds promptImage (always first frame, position="first")
    - NO referenceImages for video — that's text_to_image only
    - gen4_turbo is image_to_video ONLY (requires promptImage)
    """
    # Map UI model names to Runway API model names
    _MODEL_MAP = {
        "runway": "gen4.5", "gen4_5": "gen4.5",
        "gen4_turbo": "gen4_turbo",
        "veo3_1": "veo3.1", "veo3_1_fast": "veo3.1_fast",
    }
    model = _MODEL_MAP.get(model, model)

    # Duration: API accepts integer 2-10, snap to nearest valid
    duration = max(2, min(10, int(duration)))

    # Determine endpoint: image_to_video if we have a first frame, else text_to_video
    # gen4_turbo REQUIRES image_to_video (no text-only mode)
    _use_i2v = False
    if first_frame_path and os.path.isfile(first_frame_path):
        _use_i2v = True
    elif model == "gen4_turbo":
        print(f"[RUNWAY] WARNING: gen4_turbo requires promptImage but none provided, will likely fail")

    _endpoint = "image_to_video" if _use_i2v else "text_to_video"

    payload = {
        "model": model,
        "promptText": prompt[:1000],  # API max 1000 chars
        "duration": duration,
        "ratio": _get_ratio_for_endpoint(_endpoint, ratio),
        "contentModeration": {"publicFigureThreshold": "low"},
    }

    # Only image_to_video gets promptImage
    if _use_i2v and first_frame_path:
        if last_frame_path and os.path.isfile(last_frame_path):
            # Multi-keyframe: first + last frame for angle interpolation
            payload["promptImage"] = [
                {"uri": _runway_upload_file(first_frame_path), "position": "first"},
                {"uri": _runway_upload_file(last_frame_path), "position": "last"},
            ]
            print(f"[RUNWAY] Multi-keyframe: first + last frame")
        else:
            # Single frame
            payload["promptImage"] = _runway_upload_file(first_frame_path)
    if seed is not None:
        payload["seed"] = seed

    print(f"[RUNWAY] Submitting {_endpoint}: model={model}, "
          f"duration={duration}s, ratio={payload['ratio']}, "
          f"seed={seed}, publicFigureThreshold=low, "
          f"prompt={prompt[:80]}...")

    # Retry logic for transient failures
    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{RUNWAY_API_BASE}/{_endpoint}",
                headers=_runway_headers(),
                json=payload,
                timeout=60,
            )

            if resp.status_code == 429:
                wait = min(30, 5 * (attempt + 1))
                print(f"[RUNWAY/{_endpoint}] Rate limited (429), waiting {wait}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue

            if resp.status_code in (500, 502, 503):
                wait = min(20, 3 * (attempt + 1))
                print(f"[RUNWAY/{_endpoint}] Server error ({resp.status_code}), retrying in {wait}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue

            if resp.status_code not in (200, 201):
                err_body = resp.text[:500]
                print(f"[RUNWAY] Submit error {resp.status_code}: {err_body}")
                print(f"[RUNWAY] Payload: model={payload.get('model')}, duration={payload.get('duration')}, ratio={payload.get('ratio')}")
                raise RuntimeError(f"Runway API {resp.status_code}: {err_body}")

            # Success
            break

        except requests.exceptions.Timeout:
            print(f"[RUNWAY/{_endpoint}] Timeout (attempt {attempt+1}/{max_retries})")
            last_error = "Request timed out"
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            raise RuntimeError(f"{_endpoint} timed out after {max_retries} attempts")
        except requests.exceptions.ConnectionError as e:
            print(f"[RUNWAY/{_endpoint}] Connection error: {e} (attempt {attempt+1}/{max_retries})")
            last_error = str(e)
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            raise RuntimeError(f"{_endpoint} connection failed after {max_retries} attempts")
    else:
        raise RuntimeError(f"{_endpoint} failed after {max_retries} retries: {last_error or 'server errors'}")

    data = resp.json()
    task_id = data.get("id", "")
    print(f"[RUNWAY] Task submitted: id={task_id}")
    return task_id


def _runway_poll(task_id: str, progress_cb=None) -> dict:
    """
    Poll Runway task until SUCCEEDED or FAILED.
    Reports progress with elapsed time.
    """
    deadline = time.time() + RUNWAY_POLL_TIMEOUT
    start_time = time.time()
    poll_count = 0

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{RUNWAY_API_BASE}/tasks/{task_id}",
                headers=_runway_headers(),
                timeout=30,
            )
            if resp.status_code in (429, 500, 502, 503):
                print(f"[RUNWAY/poll] Transient {resp.status_code}, will retry...")
                time.sleep(RUNWAY_POLL_INTERVAL)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"Poll error {resp.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[RUNWAY/poll] Request error: {e}, retrying...")
            time.sleep(RUNWAY_POLL_INTERVAL)
            continue
        data = resp.json()
        status = data.get("status", "")
        elapsed = int(time.time() - start_time)
        poll_count += 1
        progress_pct = data.get("progress", None)

        # Build status message with timing
        if progress_pct is not None:
            status_msg = f"rendering {int(progress_pct * 100)}% ({elapsed}s elapsed)"
        else:
            status_msg = f"rendering... ({elapsed}s elapsed)"

        print(f"[RUNWAY] Poll {task_id[:12]}... status={status} elapsed={elapsed}s")

        if progress_cb:
            progress_cb(status_msg)

        if status == "SUCCEEDED":
            output = data.get("output", [])
            if not output:
                raise RuntimeError(
                    f"Runway task succeeded but no output URL found: "
                    f"{json.dumps(data)[:500]}"
                )
            print(f"[RUNWAY] Completed in {elapsed}s after {poll_count} polls")
            return {"url": output[0], "task_id": task_id}
        elif status == "FAILED":
            failure = data.get("failure", "unknown reason")
            raise RuntimeError(
                f"Runway generation failed: {failure} (task={task_id})"
            )

        time.sleep(RUNWAY_POLL_INTERVAL)

    raise TimeoutError(
        f"Runway generation timed out after {RUNWAY_POLL_TIMEOUT}s "
        f"(task={task_id})"
    )


def _runway_generate_scene(scene: dict, output_dir: str, index: int,
                            progress_cb=None, cost_cb=None,
                            photo_path: str = None) -> str:
    """
    Generate a video clip using Runway API.
    Supports all models: gen3a_turbo, gen4.5, kling3.0_pro/standard, veo3/3.1/3.1_fast

    Photo+prompt = real video with motion (THE key feature for Runway/Kling/Veo).
    """
    clip_path = os.path.join(output_dir, f"clip_{index:03d}.mp4")
    prompt = scene["prompt"]
    duration = scene.get("duration", 8)
    camera = scene.get("camera_movement", "zoom_in")

    # Get the specific model — scene can override the engine with a model name
    runway_model = scene.get("runway_model", scene.get("engine", "gen4.5"))
    # Map UI dropdown IDs (underscores) to Runway API model IDs (dots)
    MODEL_MAP = {
        "runway": "gen4.5",
        "gen4_5": "gen4.5",         # UI uses underscores
        "gen4.5": "gen4.5",         # API uses dots
        "gen4_turbo": "gen4_turbo",
        "veo3": "veo3",
        "veo3_1": "veo3.1",          # UI uses underscores
        "veo3.1": "veo3.1",
        "veo3_1_fast": "veo3.1_fast", # UI uses underscores
        "veo3.1_fast": "veo3.1_fast",
    }
    model = MODEL_MAP.get(runway_model, "gen4.5")

    camera_suffix = CAMERA_PROMPT_SUFFIXES.get(camera, "")
    gen_prompt = prompt + camera_suffix if camera_suffix else prompt
    gen_prompt = enhance_prompt_with_references(gen_prompt)

    # Area 1: Add continuity context if enabled
    if scene.get("continuity_mode", True):
        continuity = _build_continuity_prompt(scene, index)
        if continuity:
            gen_prompt = f"{gen_prompt}. {continuity}"

    has_photo = photo_path and os.path.isfile(photo_path)

    # Area 1: Auto-attach character reference from previous scenes
    # If continuity_context has a character_photo and no explicit photo provided,
    # use it for consistent character appearance across scenes.
    if not has_photo and scene.get("continuity_context", {}).get("character_photo"):
        char_photo = scene["continuity_context"]["character_photo"]
        if os.path.isfile(char_photo):
            photo_path = char_photo
            has_photo = True
            print(f"[RUNWAY/{model}][{index}] Auto-attached character photo from continuity context")

    def _report(msg):
        print(f"[RUNWAY/{model}][{index}] {msg}")
        if progress_cb:
            progress_cb(index, msg)

    def _record(gen_type):
        if cost_cb:
            cost_cb(str(scene.get("id", index)), gen_type)

    # Inject environment and costume descriptions (prefer vision-descriptions from photos when available)
    # Note: character_description is handled below in the has_photo / no-photo branches
    env_photo = scene.get("environment_photo_path", "")
    if env_photo:
        vision_desc = _describe_entity_photo(env_photo, "environment")
        if vision_desc and vision_desc.lower() not in gen_prompt.lower():
            gen_prompt = f"{gen_prompt} Setting (from reference photo): {vision_desc}."
            print(f"[RUNWAY/{model}][{index}] Injected environment vision description from photo")
    else:
        env_desc = scene.get("environment_description", "")
        if env_desc and env_desc.lower() not in gen_prompt.lower():
            gen_prompt = f"{gen_prompt} Setting: {env_desc}."
            print(f"[RUNWAY/{model}][{index}] Injected environment text description ({len(env_desc)} chars)")

    costume_photo = scene.get("costume_photo_path", "")
    if costume_photo:
        vision_desc = _describe_entity_photo(costume_photo, "costume")
        if vision_desc and vision_desc.lower() not in gen_prompt.lower():
            gen_prompt = f"{gen_prompt} Costume (from reference photo): {vision_desc}."
            print(f"[RUNWAY/{model}][{index}] Injected costume vision description from photo")
    else:
        costume_desc = scene.get("costume_description", "")
        if costume_desc and costume_desc.lower() not in gen_prompt.lower():
            gen_prompt = f"{gen_prompt} Wearing: {costume_desc}."
            print(f"[RUNWAY/{model}][{index}] Injected costume text description ({len(costume_desc)} chars)")

    try:
        # ---- TWO-STEP PIPELINE: Generate scene image with @tag refs, then animate ----
        # Collect all available reference photos for text_to_image @tag system
        reference_photos = []
        if has_photo and photo_path:
            reference_photos.append({"path": photo_path, "tag": "Character"})
        costume_photo = scene.get("costume_photo_path", "")
        if costume_photo and os.path.isfile(costume_photo):
            reference_photos.append({"path": costume_photo, "tag": "Costume"})
        env_photo_path = scene.get("environment_photo_path", "")
        if env_photo_path and os.path.isfile(env_photo_path):
            reference_photos.append({"path": env_photo_path, "tag": "Setting"})

        # PIPELINE: When photos exist, generate a scene image first using text_to_image
        # with @tag references (preserves likeness), then animate that scene image.
        # This way the video starts IN the scene, not from the raw uploaded photo.
        reference_photos = []
        if has_photo and photo_path:
            reference_photos.append({"path": photo_path, "tag": "Character"})
        costume_photo = scene.get("costume_photo_path", "")
        if costume_photo and os.path.isfile(costume_photo):
            reference_photos.append({"path": costume_photo, "tag": "Costume"})
        env_photo_path = scene.get("environment_photo_path", "")
        if env_photo_path and os.path.isfile(env_photo_path):
            reference_photos.append({"path": env_photo_path, "tag": "Setting"})

        first_frame = None
        if reference_photos:
            # Build prompt with @tag mentions
            tag_prompt = gen_prompt
            if any(r["tag"] == "Character" for r in reference_photos):
                if "@Character" not in tag_prompt:
                    tag_prompt = f"@Character in a cinematic scene. {tag_prompt}"
            if any(r["tag"] == "Costume" for r in reference_photos):
                if "@Costume" not in tag_prompt:
                    tag_prompt = f"{tag_prompt} Wearing the outfit from @Costume."
            if any(r["tag"] == "Setting" for r in reference_photos):
                if "@Setting" not in tag_prompt:
                    tag_prompt = f"{tag_prompt} Set in the location from @Setting."

            _report(f"generating scene image with {len(reference_photos)} photo refs (@tags)...")
            scene_image = _runway_generate_scene_image(
                tag_prompt, reference_photos,
                ratio="1280:720",
                model="gen4_image",
            )
            if scene_image:
                first_frame = scene_image
                _report(f"scene image ready — animating into video ({model}, {duration}s)...")
            else:
                _report(f"scene image failed — falling back to text-only video...")

        # Fallback to explicit keyframe if no scene image generated
        if not first_frame and scene.get("first_frame_path") and os.path.isfile(scene.get("first_frame_path", "")):
            first_frame = scene["first_frame_path"]

        # Also inject text descriptions to reinforce
        char_desc = scene.get("character_description", "")
        if has_photo and not char_desc:
            try:
                char_desc = describe_photo(photo_path)
            except Exception:
                char_desc = ""
        if char_desc:
            gen_prompt = f"This person: {char_desc}. Keep their EXACT appearance. {gen_prompt}"

        _report(f"submitting {'image' if first_frame else 'text'}-to-video ({model}, {duration}s)...")
        task_id = _runway_submit_text_to_video(
            gen_prompt, duration=duration, model=model,
            first_frame_path=first_frame,
        )

        _report(f"polling Runway (task={task_id[:12]}...)")
        video_info = _runway_poll(task_id, progress_cb=lambda msg: _report(msg))
        _report("downloading Runway video...")
        _download(video_info["url"], clip_path)

        # Cost tracking: ~$0.05 per second for gen3a_turbo (5 credits/sec)
        effective_dur = duration
        est_cost = effective_dur * RUNWAY_COST_PER_SEC
        print(f"[RUNWAY][{index}] Estimated cost: ~${est_cost:.2f} "
              f"({effective_dur}s x ${RUNWAY_COST_PER_SEC}/s)")
        _record("video")

        # Apply camera movement post-processing if the video needs Ken Burns overlay
        # Runway natively handles camera movement via prompt, so skip Ken Burns
        _report("done (Runway Gen-3 Alpha Turbo)")
        return clip_path

    except Exception as e:
        if _is_moderation_error(str(e)):
            _report(f"MODERATION BLOCK ({model}): celebrity likeness rejected. Try a different engine manually.")
            raise RuntimeError(
                f"Scene {index} blocked by {model} moderation (celebrity likeness). "
                f"Try switching this scene to a different engine (Kling, Veo, Luma) in the scene settings."
            ) from e

        _report(f"Runway generation failed: {e}")
        import traceback
        traceback.print_exc()
        raise RuntimeError(f"Scene {index} video generation failed: {e}") from e


# ---- OpenAI GPT Image Generation ----

def _get_openai_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is not set. "
            "Get an API key from https://platform.openai.com and add it to your .env file."
        )
    return key


def _openai_headers() -> dict:
    """Auth headers for OpenAI API."""
    return {
        "Authorization": f"Bearer {_get_openai_api_key()}",
        "Content-Type": "application/json",
    }


def _openai_generate_image(prompt: str) -> str:
    """Generate an image via OpenAI DALL-E 3. Returns image URL."""
    resp = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers=_openai_headers(),
        json={
            "model": "dall-e-3",
            "prompt": prompt,
            "n": 1,
            "size": "1792x1024",
            "quality": "hd",
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    images = data.get("data", [])
    if not images:
        raise RuntimeError("No image returned from OpenAI API")
    return images[0]["url"]


def _openai_generate_scene(scene: dict, output_dir: str, index: int,
                           progress_cb=None, cost_cb=None,
                           photo_path: str = None) -> str:
    """
    Generate a video clip using OpenAI GPT image + Ken Burns.
    OpenAI produces stills which are animated via Ken Burns effect.

    Args:
        scene: dict with at least {prompt, duration}
        output_dir: directory to save clips
        index: scene index for naming
        progress_cb: optional callable(index, status_str)
        cost_cb: optional callable(scene_key, gen_type)
        photo_path: optional photo path (used as reference in prompt)

    Returns:
        path to the generated video clip
    """
    clip_path = os.path.join(output_dir, f"clip_{index:03d}.mp4")
    prompt = scene["prompt"]
    duration = scene.get("duration", 8)
    camera = scene.get("camera_movement", "zoom_in")

    camera_suffix = CAMERA_PROMPT_SUFFIXES.get(camera, "")
    gen_prompt = prompt + camera_suffix if camera_suffix else prompt

    # Area 1: Add continuity context if enabled
    if scene.get("continuity_mode", True):
        continuity = _build_continuity_prompt(scene, index)
        if continuity:
            gen_prompt = f"{gen_prompt}. {continuity}"

    # Inject character/costume/environment descriptions (or vision-descriptions from photos)
    gen_prompt = _enrich_prompt_from_entity_photos(gen_prompt, scene, index, "OPENAI")

    def _report(msg):
        print(f"[OPENAI][{index}] {msg}")
        if progress_cb:
            progress_cb(index, msg)

    def _record(gen_type):
        if cost_cb:
            cost_cb(str(scene.get("id", index)), gen_type)

    try:
        _report("generating image via OpenAI DALL-E 3...")
        img_url = _openai_generate_image(gen_prompt)
        img_path = os.path.join(output_dir, f"img_{index:03d}_openai.png")
        _report("downloading OpenAI image...")
        _download(img_url, img_path)
        _report("creating Ken Burns video from image...")
        _ken_burns(img_path, clip_path, duration, camera=camera)
        _record("image")
        _report("done (OpenAI DALL-E 3 + Ken Burns)")
        return clip_path
    except Exception as e:
        _report(f"OpenAI generation failed ({e}), falling back to Grok...")
        # Fall back to Grok pipeline
        try:
            img_url = _generate_image(gen_prompt)
            img_path = os.path.join(output_dir, f"img_{index:03d}.png")
            _download(img_url, img_path)
            _ken_burns(img_path, clip_path, duration, camera=camera)
            _record("image")
            _report("done (Grok image fallback)")
            return clip_path
        except Exception as e2:
            _report(f"all generation attempts failed: {e2}")
            raise RuntimeError(
                f"Scene {index} OpenAI generation failed entirely: {e2}"
            ) from e2


# ---- Character Reference System ----

SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "settings.json"
)
REFERENCES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "references"
)


def _load_settings() -> dict:
    """Load project settings from output/settings.json."""
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _get_character_references() -> dict:
    """
    Get character reference map: {name: path}.
    Sources: output/settings.json character_references + references/ directory files.
    Reference names are case-insensitive for matching.
    """
    settings = _load_settings()
    char_refs = dict(settings.get("character_references", {}))

    # Also include any reference images from the references/ directory
    if os.path.isdir(REFERENCES_DIR):
        for fname in os.listdir(REFERENCES_DIR):
            fpath = os.path.join(REFERENCES_DIR, fname)
            if os.path.isfile(fpath):
                name = os.path.splitext(fname)[0]
                # Don't overwrite explicit character_references entries
                if name not in char_refs:
                    char_refs[name] = fpath

    return char_refs


def _resolve_character_references(prompt: str, char_refs: dict = None) -> tuple:
    """
    Scan prompt for character reference names and return (matched_name, photo_path).
    Returns the first matched character reference, or (None, None).
    """
    if char_refs is None:
        char_refs = _get_character_references()

    prompt_lower = prompt.lower()
    for name, path in char_refs.items():
        if name.lower() in prompt_lower and os.path.isfile(path):
            return name, path

    return None, None


# Cache for character descriptions so we don't re-describe every generation
_char_description_cache = {}

# ---- Photo path resolver and vision-description helpers ----

def _resolve_pos_photo_path(ref_url: str, entity_type: str) -> str | None:
    """
    Resolve a Prompt OS photo API URL to the actual filesystem path.

    ref_url  : e.g. "/api/pos/characters/{id}/photo"
    entity_type: "characters" | "costumes" | "environments"

    Returns the absolute path if the file exists, else None.
    """
    if not ref_url:
        return None

    # Direct filesystem path (already resolved)
    if os.path.isfile(ref_url):
        return ref_url

    import re
    m = re.search(r"/api/pos/(?:characters|costumes|environments)/([^/]+)/photo", ref_url)
    if not m:
        return None

    entity_id = m.group(1)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subdirs = {
        "characters": os.path.join(base_dir, "output", "prompt_os", "photos", "characters"),
        "costumes":   os.path.join(base_dir, "output", "prompt_os", "photos", "costumes"),
        "environments": os.path.join(base_dir, "output", "prompt_os", "photos", "environments"),
    }
    photo_dir = subdirs.get(entity_type)
    if not photo_dir:
        return None

    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = os.path.join(photo_dir, f"{entity_id}{ext}")
        if os.path.isfile(candidate):
            return candidate
    return None


# Cache for costume/environment vision descriptions (keyed by photo path)
_entity_description_cache = {}


def _describe_entity_photo(photo_path: str, entity_type: str) -> str:
    """
    Get a vision description of a costume or environment photo.

    Returns a concise prompt-ready description string, or "" on failure.
    entity_type: "costume" | "environment" — changes the vision prompt wording.
    """
    if not photo_path or not os.path.isfile(photo_path):
        return ""

    if photo_path in _entity_description_cache:
        return _entity_description_cache[photo_path]

    try:
        with open(photo_path, "rb") as f:
            photo_bytes = f.read()

        ext = os.path.splitext(photo_path)[1].lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".webp": "image/webp"}
        mime = mime_map.get(ext, "image/jpeg")
        b64_data = base64.b64encode(photo_bytes).decode("ascii")
        data_uri = f"data:{mime};base64,{b64_data}"

        if entity_type == "character":
            vision_text = (
                "Describe this person's physical appearance in extreme detail for an artist to recreate them. "
                "Include: face shape, eye shape/color, eyebrow style, nose shape, lip shape, skin tone, "
                "exact hair style/color/texture/length, facial hair if any, jawline, body build, "
                "clothing details and colors, accessories, tattoos, distinguishing features. "
                "Be extremely specific about shapes and proportions. "
                "Do NOT name or identify the person. Only describe physical appearance. Under 150 words."
            )
        elif entity_type == "costume":
            vision_text = (
                "Describe the clothing and costume in this image for use as an AI video generation prompt. "
                "Focus on: garment types (top, bottom, outerwear, footwear), colors, fabrics, patterns, accessories, overall style. "
                "Write as comma-separated visual descriptors. Be specific about colors and materials. Under 100 words."
            )
        else:  # environment
            vision_text = (
                "Describe the environment/location in this image for use as an AI video generation prompt. "
                "Focus on: setting type, architecture, lighting, atmosphere, time of day, color palette, notable features. "
                "Write as comma-separated visual descriptors. Do NOT mention any people. Under 100 words."
            )

        resp = requests.post(
            f"{API_BASE}/chat/completions",
            headers=_headers(),
            json={
                "model": "grok-4-1-fast-non-reasoning",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_uri}},
                            {"type": "text", "text": vision_text},
                        ],
                    }
                ],
                "max_tokens": 200,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if choices:
            desc = choices[0]["message"]["content"].strip()
            _entity_description_cache[photo_path] = desc
            print(f"[VISION] {entity_type} photo described ({len(desc)} chars): {desc[:80]}...")
            return desc
    except Exception as e:
        print(f"[VISION] Failed to describe {entity_type} photo {photo_path}: {e}")

    return ""


def _enrich_prompt_from_entity_photos(gen_prompt: str, scene: dict, index: int,
                                       engine_tag: str = "GEN") -> str:
    """
    Enrich a generation prompt with vision-descriptions of costume and environment
    reference photos stored in scene["costume_photo_path"] and
    scene["environment_photo_path"].

    Also injects pre-built text descriptions from scene["costume_description"] and
    scene["environment_description"] if no photo path is available.

    Returns the enriched prompt string.
    """
    # --- Character description (always inject first for prominence) ---
    char_desc = scene.get("character_description", "")
    if char_desc and char_desc.lower() not in gen_prompt.lower():
        gen_prompt = f"{char_desc}. {gen_prompt}"
        print(f"[{engine_tag}][{index}] Injected character description ({len(char_desc)} chars)")

    # --- Costume: prefer vision description of photo, fall back to text ---
    costume_photo = scene.get("costume_photo_path", "")
    if costume_photo:
        vision_desc = _describe_entity_photo(costume_photo, "costume")
        if vision_desc and vision_desc.lower() not in gen_prompt.lower():
            gen_prompt = f"{gen_prompt} Costume (from reference photo): {vision_desc}."
            print(f"[{engine_tag}][{index}] Injected costume vision description from photo")
    else:
        costume_desc = scene.get("costume_description", "")
        if costume_desc and costume_desc.lower() not in gen_prompt.lower():
            gen_prompt = f"{gen_prompt} Wearing: {costume_desc}."
            print(f"[{engine_tag}][{index}] Injected costume text description ({len(costume_desc)} chars)")

    # --- Environment: prefer vision description of photo, fall back to text ---
    env_photo = scene.get("environment_photo_path", "")
    if env_photo:
        vision_desc = _describe_entity_photo(env_photo, "environment")
        if vision_desc and vision_desc.lower() not in gen_prompt.lower():
            gen_prompt = f"{gen_prompt} Setting (from reference photo): {vision_desc}."
            print(f"[{engine_tag}][{index}] Injected environment vision description from photo")
    else:
        env_desc = scene.get("environment_description", "")
        if env_desc and env_desc.lower() not in gen_prompt.lower():
            gen_prompt = f"{gen_prompt} Setting: {env_desc}."
            print(f"[{engine_tag}][{index}] Injected environment text description ({len(env_desc)} chars)")

    return gen_prompt

def enhance_prompt_with_references(prompt: str, char_refs: dict = None) -> str:
    """
    If prompt mentions a character reference name, describe the character
    and inject the description into the prompt. This makes Grok video gen
    understand what the character looks like even without an image input.
    """
    if char_refs is None:
        char_refs = _get_character_references()

    prompt_lower = prompt.lower()
    enhanced = prompt

    for name, path in char_refs.items():
        if name.lower() in prompt_lower and os.path.isfile(path):
            # Check cache first
            if name in _char_description_cache:
                desc = _char_description_cache[name]
            else:
                # Try to describe the photo using Grok vision
                try:
                    desc = describe_photo(path)
                    _char_description_cache[name] = desc
                    print(f"[CHAR_REF] Described '{name}': {desc[:80]}...")
                except Exception as e:
                    desc = name  # fallback to just the name
                    print(f"[CHAR_REF] Could not describe '{name}': {e}")

            # Replace the character name with the description
            import re
            enhanced = re.sub(
                re.escape(name),
                f"{name} ({desc})",
                enhanced,
                flags=re.IGNORECASE,
                count=1
            )

    return enhanced


def _subprocess_kwargs() -> dict:
    """Extra kwargs for subprocess calls (hide window on Windows)."""
    kw = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kw["startupinfo"] = si
    return kw


def extract_last_frame(clip_path: str, output_path: str) -> str:
    """
    Extract the last frame from a video clip using ffmpeg.

    Args:
        clip_path: path to the source video clip
        output_path: path to save the extracted frame image

    Returns:
        output_path on success

    Raises:
        RuntimeError if extraction fails
    """
    if not os.path.isfile(clip_path):
        raise RuntimeError(f"Clip not found: {clip_path}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Use ffmpeg to seek to near the end and grab the last frame
    cmd = [
        "ffmpeg", "-y",
        "-sseof", "-0.1",      # seek to 0.1s before end
        "-i", clip_path,
        "-frames:v", "1",
        "-q:v", "2",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=30,
            **_subprocess_kwargs(),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[:500]
            raise RuntimeError(f"ffmpeg last-frame extraction failed: {stderr}")
        if not os.path.isfile(output_path):
            raise RuntimeError("ffmpeg produced no output file")
        print(f"[KEYFRAME] Extracted last frame from {clip_path} -> {output_path}")
        return output_path
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg last-frame extraction timed out")


def extract_first_frame(clip_path: str, output_path: str) -> str:
    """
    Extract the first frame from a video clip using ffmpeg.

    Args:
        clip_path: path to the source video clip
        output_path: path to save the extracted frame image

    Returns:
        output_path on success
    """
    if not os.path.isfile(clip_path):
        raise RuntimeError(f"Clip not found: {clip_path}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-frames:v", "1",
        "-q:v", "2",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=30,
            **_subprocess_kwargs(),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[:500]
            raise RuntimeError(f"ffmpeg first-frame extraction failed: {stderr}")
        if not os.path.isfile(output_path):
            raise RuntimeError("ffmpeg produced no output file")
        print(f"[KEYFRAME] Extracted first frame from {clip_path} -> {output_path}")
        return output_path
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg first-frame extraction timed out")


# ---- Video generation via Grok API ----

def _submit_video(prompt: str) -> str:
    """Submit a video generation request. Returns request_id."""
    resp = requests.post(
        f"{API_BASE}/videos/generations",
        headers=_headers(),
        json={"model": "grok-imagine-video", "prompt": prompt},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["request_id"]


def _poll_video(request_id: str, progress_cb=None) -> dict:
    """Poll until video is done. Returns {url, duration}."""
    deadline = time.time() + POLL_TIMEOUT
    start_time = time.time()
    while time.time() < deadline:
        resp = requests.get(
            f"{API_BASE}/videos/{request_id}",
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "")
        elapsed = int(time.time() - start_time)
        if progress_cb:
            progress_cb(f"rendering... ({elapsed}s elapsed)")
        if status == "done":
            print(f"[GROK] Video completed in {elapsed}s")
            return data["video"]
        elif status in ("failed", "error"):
            raise RuntimeError(f"Video generation failed: {data}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Video generation timed out after {POLL_TIMEOUT}s")


def _download(url: str, dest: str):
    """Download a file from URL to dest path."""
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


# ---- Fallback: image + Ken Burns ----

def _generate_image(prompt: str) -> str:
    """Generate an image via Grok. Returns image URL."""
    resp = requests.post(
        f"{API_BASE}/images/generations",
        headers=_headers(),
        json={"model": "grok-imagine-image", "prompt": prompt, "n": 1},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    # response may have data[0].url or similar
    images = data.get("data", [])
    if not images:
        raise RuntimeError("No image returned from API")
    return images[0]["url"]


def _generate_image_from_photo(prompt: str, photo_path: str,
                                edit_strength: float = 0.3) -> str:
    """
    Generate a styled image via Grok image API with a source photo.
    Sends the photo as base64 data URI for style transfer.

    edit_strength: 0.0 = keep photo exactly, 1.0 = ignore photo completely
                   0.2-0.4 recommended for maintaining photo likeness with style applied

    Returns the styled image URL.
    """
    print(f"[PHOTO_GEN] START photo={photo_path}, strength={edit_strength}")
    print(f"[PHOTO_GEN] Prompt: {prompt[:100]}...")

    # Read photo and convert to base64 data URI
    with open(photo_path, "rb") as f:
        photo_bytes = f.read()

    ext = os.path.splitext(photo_path)[1].lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    }
    mime = mime_map.get(ext, "image/jpeg")
    b64_data = base64.b64encode(photo_bytes).decode("ascii")
    data_uri = f"data:{mime};base64,{b64_data}"
    print(f"[PHOTO_GEN] Encoded {len(photo_bytes)} bytes, mime={mime}")

    # Build a prompt that PRESERVES the original photo
    # Key: tell the model to edit, not recreate
    edit_prompt = (
        f"Edit this image: {prompt}. "
        f"Keep the original subjects, composition, and key visual elements intact. "
        f"Only modify the style, lighting, and atmosphere as described."
    )

    print(f"[PHOTO_GEN] Calling Grok API with edit prompt + image_strength={edit_strength}...")
    resp = requests.post(
        f"{API_BASE}/images/generations",
        headers=_headers(),
        json={
            "model": "grok-imagine-image",
            "prompt": edit_prompt,
            "image": data_uri,
            "n": 1,
            "strength": edit_strength,        # How much to deviate from original
            "image_strength": 1.0 - edit_strength,  # How much to keep original
        },
        timeout=120,
    )
    print(f"[PHOTO_GEN] API response: {resp.status_code}")
    if resp.status_code != 200:
        print(f"[PHOTO_GEN] Error: {resp.text[:500]}")
    resp.raise_for_status()
    data = resp.json()
    images = data.get("data", [])
    if not images:
        raise RuntimeError("No image returned from photo edit API")
    print(f"[PHOTO_GEN] SUCCESS: {images[0]['url'][:80]}...")
    return images[0]["url"]


def describe_photo(photo_path: str) -> str:
    """
    Send a photo to the Grok vision API and get a detailed description
    suitable for use as a video generation prompt.
    Returns the description string.
    """
    # Read photo and convert to base64
    with open(photo_path, "rb") as f:
        photo_bytes = f.read()

    ext = os.path.splitext(photo_path)[1].lower()
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    mime = mime_map.get(ext, "image/jpeg")
    b64_data = base64.b64encode(photo_bytes).decode("ascii")
    data_uri = f"data:{mime};base64,{b64_data}"

    # Use Grok chat completions with vision
    resp = requests.post(
        f"{API_BASE}/chat/completions",
        headers=_headers(),
        json={
            "model": "grok-4-1-fast-non-reasoning",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_uri},
                        },
                        {
                            "type": "text",
                            "text": (
                                "Describe this person in EXTREME physical detail for an AI video generator that has never seen this image. "
                                "The video AI needs to recreate this EXACT person from text alone. "
                                "PRIORITIZE FACE: exact face shape (round/oval/square/heart), jawline, cheekbones, brow ridge, nose shape and size, "
                                "lip shape and fullness, eye shape and color, eyebrow thickness and arch, forehead size, chin shape. "
                                "Then: exact skin tone (be very specific — light olive, deep brown, pale porcelain, etc.), "
                                "hair color, style, length, texture. Build, height impression, posture. "
                                "Then: outfit, accessories, distinctive marks/tattoos/piercings. "
                                "If character sheet with multiple angles, merge into ONE unified description. "
                                "Write as dense comma-separated descriptors. NO filler words. Under 200 words."
                            ),
                        },
                    ],
                }
            ],
            "max_tokens": 350,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if choices:
        return choices[0]["message"]["content"].strip()
    raise RuntimeError("No description returned from vision API")


def _ken_burns(image_path: str, output_path: str, duration: float = 8.0,
               camera: str = "zoom_in"):
    """Create a Ken Burns (slow zoom/pan) video from a still image using ffmpeg.

    Args:
        image_path: path to source image
        output_path: path for output video
        duration: clip duration in seconds
        camera: camera movement preset name
    """
    preset = CAMERA_PRESETS.get(camera, CAMERA_PRESETS["zoom_in"])
    crop_x = preset["crop_x"].replace("{dur}", str(duration))
    crop_y = preset["crop_y"].replace("{dur}", str(duration))

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-vf", (
            f"scale=3840:2160,crop=1920:1080:{crop_x}:{crop_y}"
        ),
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", "30",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())


# ---- Public API ----

def generate_scene(scene: dict, index: int, output_dir: str,
                   progress_cb=None, cost_cb=None,
                   photo_path: str = None) -> str:
    """
    Generate a single video clip for a scene.
    Dispatches to the appropriate engine based on scene.get("engine").

    Args:
        scene: dict with at least {prompt, duration}
               optional: camera_movement (preset name), engine ("grok"|"luma"|"openai"|"runway")
        index: scene index (for naming)
        output_dir: directory to save clips
        progress_cb: optional callable(index, status_str)
        cost_cb: optional callable(scene_key, gen_type) to record cost
        photo_path: optional path to an uploaded photo for style transfer

    Returns:
        path to the generated video clip
    """
    os.makedirs(output_dir, exist_ok=True)

    # Determine engine: scene-level > global settings > default "gen4_5" (Runway)
    engine = scene.get("engine", None)
    if not engine:
        settings = _load_settings()
        engine = settings.get("default_engine", "gen4_5")
    engine = engine.lower()

    # Auto-resolve character references if no explicit photo provided
    if not photo_path:
        char_name, char_photo = _resolve_character_references(scene.get("prompt", ""))
        if char_photo:
            photo_path = char_photo
            print(f"[generate_scene] Auto-attached character reference "
                  f"'{char_name}' -> {char_photo}")

    # Area 3: Enforce valid duration for this engine
    requested_dur = scene.get("duration", 8)
    valid_dur = get_valid_duration(engine, requested_dur)
    if valid_dur != requested_dur:
        print(f"[generate_scene] Snapping duration {requested_dur}s -> {valid_dur}s for engine={engine}")
        scene["duration"] = valid_dur

    print(f"[generate_scene] index={index}, engine={engine}, "
          f"prompt={scene.get('prompt', '')[:80]}..., photo={photo_path}, duration={valid_dur}s")

    # All Runway-hosted models (use underscore versions from UI dropdown)
    RUNWAY_ENGINES = {
        "runway", "gen4_5", "gen4_turbo",
        "veo3", "veo3_1", "veo3_1_fast",
        # Also accept dot-separated versions from the API
        "gen4.5", "veo3.1", "veo3.1_fast",
    }

    # Dispatch to engine
    if engine == ENGINE_LUMA:
        return _luma_generate_scene(
            scene, output_dir, index, progress_cb, cost_cb, photo_path
        )
    elif engine == ENGINE_OPENAI:
        return _openai_generate_scene(
            scene, output_dir, index, progress_cb, cost_cb, photo_path
        )
    elif engine in RUNWAY_ENGINES or engine == ENGINE_RUNWAY:
        # Map UI engine IDs to Runway model names
        scene["runway_model"] = engine  # _runway_generate_scene will map this
        return _runway_generate_scene(
            scene, output_dir, index, progress_cb, cost_cb, photo_path
        )
    else:
        # Default: Grok engine (original behavior)
        return _grok_generate_scene(
            scene, output_dir, index, progress_cb, cost_cb, photo_path
        )


def _grok_generate_scene(scene: dict, output_dir: str, index: int,
                          progress_cb=None, cost_cb=None,
                          photo_path: str = None) -> str:
    """
    Generate a video clip using the Grok (xAI) engine.
    This is the original pipeline extracted into a named function.
    """
    clip_path = os.path.join(output_dir, f"clip_{index:03d}.mp4")
    prompt = scene["prompt"]
    duration = scene.get("duration", 8)
    camera = scene.get("camera_movement", "zoom_in")

    # Append camera movement to prompt for AI generation
    camera_suffix = CAMERA_PROMPT_SUFFIXES.get(camera, "")
    gen_prompt = prompt + camera_suffix if camera_suffix else prompt

    # Area 1: Add continuity context if enabled
    if scene.get("continuity_mode", True):
        continuity = _build_continuity_prompt(scene, index)
        if continuity:
            gen_prompt = f"{gen_prompt}. {continuity}"

    has_photo = photo_path and os.path.isfile(photo_path)
    print(f"[GROK][{index}] prompt={gen_prompt[:80]}..., photo={has_photo}, camera={camera}")

    def _report(msg):
        print(f"[GROK][{index}] {msg}")
        if progress_cb:
            progress_cb(index, msg)

    def _record(gen_type):
        if cost_cb:
            cost_cb(str(scene.get("id", index)), gen_type)

    # ALWAYS try real video first (real motion is what users want)
    # Enhance prompt with character reference descriptions if any match
    gen_prompt = enhance_prompt_with_references(gen_prompt)

    # For Grok, text is ALL we have — no reference image input support.
    # Inject character/costume/environment descriptions (or vision-descriptions from photos).
    gen_prompt = _enrich_prompt_from_entity_photos(gen_prompt, scene, index, "GROK")

    if has_photo:
        # Grok video API has no image input — but we can still vision-describe
        # the character photo and prepend it if not already described
        char_photo_desc = scene.get("character_description", "")
        if not char_photo_desc:
            try:
                char_photo_desc = describe_photo(photo_path)
                if char_photo_desc and char_photo_desc.lower() not in gen_prompt.lower():
                    gen_prompt = f"{char_photo_desc}. {gen_prompt}"
                    print(f"[GROK][{index}] Vision-described character photo and prepended to prompt")
            except Exception as _e:
                print(f"[GROK][{index}] Could not vision-describe character photo: {_e}")
        print(f"[GROK][{index}] Character photo present — used via text description (Grok video has no image input)")

    # Add uniqueness to prevent identical videos from identical prompts
    import random
    unique_seed = random.randint(1000, 9999)
    unique_prompt = f"{gen_prompt} [scene {index + 1}, variation {unique_seed}]"

    # VIDEO ONLY — no image/Ken Burns fallback
    max_retries = 2
    for attempt in range(max_retries):
        try:
            _report(f"submitting video request (attempt {attempt + 1})...")
            request_id = _submit_video(unique_prompt)
            _report(f"polling (id={request_id[:12]}...)")
            video_info = _poll_video(request_id, progress_cb=lambda msg: _report(msg))
            _report("downloading clip...")
            _download(video_info["url"], clip_path)
            _record("video")
            _report("done")
            return clip_path
        except Exception as e:
            if attempt < max_retries - 1:
                _report(f"attempt {attempt + 1} failed ({e}), retrying with different seed...")
                unique_seed = random.randint(1000, 9999)
                unique_prompt = f"{gen_prompt} [scene {index + 1}, take {unique_seed}]"
            else:
                _report(f"video generation failed after {max_retries} attempts: {e}")
                raise RuntimeError(f"Scene {index} video generation failed: {e}") from e


def generate_from_photo(photo_path: str, prompt: str, duration: float,
                        output_path: str, progress_cb=None,
                        camera: str = "zoom_in",
                        edit_strength: float = 0.3) -> str:
    """
    Generate a video clip from a reference photo + text prompt.
    Uses TRUE photo-to-video with style transfer via Grok image API.

    Pipeline:
      1. Read the photo, convert to base64 data URI
      2. Send to Grok image API with prompt + base64 image for style transfer
      3. Create Ken Burns video from the styled image with camera preset

    Args:
        photo_path: path to the uploaded reference photo
        prompt: text prompt describing the desired scene
        duration: clip duration in seconds
        output_path: where to save the resulting video
        progress_cb: optional callable(status_str)
        camera: camera movement preset name

    Returns:
        path to the generated video clip
    """
    def _report(msg):
        print(f"[generate_from_photo] {msg}")
        if progress_cb:
            progress_cb(msg)

    print(f"[generate_from_photo] START photo_path={photo_path}, prompt={prompt[:80]}..., duration={duration}, camera={camera}")
    print(f"[generate_from_photo] Photo exists: {os.path.isfile(photo_path)}, output_path={output_path}")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Attempt 1: True style transfer with base64 photo
    _report("sending photo to Grok for style transfer (base64)...")
    try:
        img_url = _generate_image_from_photo(prompt, photo_path, edit_strength)
        img_path = output_path.replace(".mp4", "_styled.png")
        _report("downloading styled image...")
        _download(img_url, img_path)
        print(f"[generate_from_photo] Styled image downloaded to {img_path}, size={os.path.getsize(img_path)} bytes")
        _report("creating Ken Burns video from styled image...")
        _ken_burns(img_path, output_path, duration, camera=camera)
        _report("done (photo style transfer + Ken Burns)")
        print(f"[generate_from_photo] SUCCESS: clip at {output_path}")
        return output_path
    except Exception as e:
        print(f"[generate_from_photo] Style transfer FAILED: {e}")
        import traceback
        traceback.print_exc()
        _report(f"style transfer failed ({e}), trying text-only generation...")

    # Attempt 2: Generate image using text prompt referencing the photo
    try:
        photo_desc = os.path.splitext(os.path.basename(photo_path))[0].replace("_", " ").replace("-", " ")
        styled_prompt = f"{prompt}, inspired by and matching the visual style of the reference image ({photo_desc})"
        img_url = _generate_image(styled_prompt)
        img_path = output_path.replace(".mp4", "_styled.png")
        _report("downloading generated image...")
        _download(img_url, img_path)
        _ken_burns(img_path, output_path, duration, camera=camera)
        _report("done (text-only generation + Ken Burns)")
        return output_path
    except Exception as e2:
        _report(f"text generation failed ({e2}), using original photo with Ken Burns...")

    # Attempt 3: Fall back to original photo directly
    _ken_burns(photo_path, output_path, duration, camera=camera)
    _report("done (original photo + Ken Burns)")
    return output_path


def _build_continuity_context_chain(scenes: list) -> list:
    """
    Build continuity_context for each scene based on previous scenes.
    Returns modified scenes list with continuity_context set.

    Area 1: Scene-to-scene continuity support.
    """
    import re as _re

    for i, scene in enumerate(scenes):
        if i == 0:
            scene["continuity_context"] = {}
            continue

        prev = scenes[i - 1]
        ctx = {}

        # Extract visual elements from previous scene prompt
        prev_prompt = prev.get("prompt", "")
        # Key visual elements
        visual_keywords = _re.findall(
            r'\b(neon|city|rain|ocean|forest|desert|mountain|space|sky|fire|water|'
            r'night|sunset|sunrise|fog|smoke|crystal|glass|metal|gold|silver|'
            r'cyberpunk|synthwave|gothic|vintage|industrial|abstract|flowers|stars)\b',
            prev_prompt.lower()
        )
        if visual_keywords:
            ctx["key_elements"] = list(set(visual_keywords))

        # Detect character presence
        char_refs = _get_character_references()
        prompt_lower = prev_prompt.lower()
        for name, path in char_refs.items():
            if name.lower() in prompt_lower:
                ctx["has_character"] = True
                ctx["character_photo"] = path
                break

        # Detect lighting/time of day from previous prompt
        for tod in ["night", "sunset", "sunrise", "dawn", "dusk", "midday", "golden hour", "twilight"]:
            if tod in prev_prompt.lower():
                ctx["lighting"] = tod
                break

        # Detect color palette hints
        colors_found = _re.findall(
            r'\b(neon|warm|cold|blue|red|green|purple|orange|golden|silver|monochrome|pastel|vivid)\b',
            prev_prompt.lower()
        )
        if colors_found:
            ctx["color_palette"] = ", ".join(list(set(colors_found))[:4])

        # Carry forward accumulated context
        prev_ctx = prev.get("continuity_context", {})
        if prev_ctx.get("key_elements"):
            existing = ctx.get("key_elements", [])
            merged = list(set(existing + prev_ctx["key_elements"]))[:8]
            ctx["key_elements"] = merged
        if not ctx.get("character_photo") and prev_ctx.get("character_photo"):
            ctx["character_photo"] = prev_ctx["character_photo"]
            ctx["has_character"] = prev_ctx.get("has_character", False)

        scene["continuity_context"] = ctx

    return scenes


def generate_all(scenes: list, output_dir: str,
                 progress_cb=None, cost_cb=None) -> list:
    """
    Generate video clips for all scenes with concurrency limit.
    Each scene can specify its own engine via scene["engine"].

    Args:
        scenes: list of scene dicts
        output_dir: directory to save clips
        progress_cb: optional callable(index, status_str)
        cost_cb: optional callable(scene_key, gen_type) to record cost

    Returns:
        ordered list of clip file paths
    """
    os.makedirs(output_dir, exist_ok=True)
    results = [None] * len(scenes)

    # Area 1: Build continuity context chain across scenes
    continuity_enabled = any(s.get("continuity_mode", True) for s in scenes)
    if continuity_enabled:
        scenes = _build_continuity_context_chain(scenes)

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
        futures = {}
        for i, scene in enumerate(scenes):
            fut = pool.submit(generate_scene, scene, i, output_dir, progress_cb, cost_cb)
            futures[fut] = i

        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                if progress_cb:
                    progress_cb(idx, f"FAILED: {e}")
                results[idx] = None

    return results


def get_available_engines() -> list:
    """Return list of engine info dicts with availability status."""
    engines = []

    # Grok
    grok_available = bool(os.environ.get("XAI_API_KEY", ""))
    engines.append({
        "id": ENGINE_GROK,
        "name": "Grok (xAI)",
        "description": "Text-to-video and image style transfer via Grok API",
        "available": grok_available,
        "missing_key": "XAI_API_KEY" if not grok_available else None,
    })

    # Luma
    luma_available = bool(os.environ.get("LUMA_API_KEY", ""))
    engines.append({
        "id": ENGINE_LUMA,
        "name": "Luma Dream Machine (Ray2)",
        "description": "High-quality text-to-video and image-to-video via Luma Ray2",
        "available": luma_available,
        "missing_key": "LUMA_API_KEY" if not luma_available else None,
    })

    # OpenAI
    openai_available = bool(os.environ.get("OPENAI_API_KEY", ""))
    engines.append({
        "id": ENGINE_OPENAI,
        "name": "OpenAI GPT (DALL-E 3)",
        "description": "High-quality image generation + Ken Burns animation",
        "available": openai_available,
        "missing_key": "OPENAI_API_KEY" if not openai_available else None,
    })

    # Runway
    runway_available = bool(os.environ.get("RUNWAY_API_KEY", ""))
    engines.append({
        "id": ENGINE_RUNWAY,
        "name": "Runway (Gen 4.5, Gen 4 Turbo, Veo)",
        "description": "Multi-photo text/image-to-video — Gen 4.5, Gen 4 Turbo, Veo 3/3.1",
        "available": runway_available,
        "missing_key": "RUNWAY_API_KEY" if not runway_available else None,
    })

    return engines
