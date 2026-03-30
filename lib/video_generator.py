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

# ---- Engine names ----
ENGINE_GROK = "grok"
ENGINE_LUMA = "luma"
ENGINE_OPENAI = "openai"
ENGINE_RUNWAY = "runway"
SUPPORTED_ENGINES = [ENGINE_GROK, ENGINE_LUMA, ENGINE_OPENAI, ENGINE_RUNWAY]

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
                       duration: int = 5) -> str:
    """
    Submit a video generation request to Luma Dream Machine (Ray2).

    Args:
        prompt: text description of the video
        image_url: optional URL to an image for image-to-video
        duration: video duration -- "5s" or "9s" (Luma supports 5s and 9s)

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

    if image_url:
        payload["keyframes"] = {
            "frame0": {
                "type": "image",
                "url": image_url,
            }
        }

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
    b64_data = base64.b64encode(photo_bytes).decode("ascii")
    return f"data:{mime};base64,{b64_data}"


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

    try:
        _report("submitting to Luma Dream Machine (Ray2)...")
        gen_id = _luma_submit_video(gen_prompt, image_url=image_url,
                                    duration=duration)
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
RUNWAY_RATIO_MAP = {
    "16:9": "1280:720",
    "9:16": "720:1280",
    "1:1": "960:960",
    "4:5": "832:1104",
    "1280:720": "1280:720",
    "720:1280": "720:1280",
}


def _runway_submit_image_to_video(prompt: str, image_path: str,
                                   duration: int = 5,
                                   ratio: str = "16:9") -> str:
    """
    Submit an image-to-video request to Runway Gen-3 Alpha Turbo.

    Args:
        prompt: text description of what should happen in the video
        image_path: local path to image file (will be base64-encoded)
        duration: 5 or 10 seconds
        ratio: "16:9" or "9:16"

    Returns:
        task ID string
    """
    duration = 10 if duration > 7 else 5  # Runway supports 5 or 10

    prompt_image = _photo_to_data_uri(image_path)

    payload = {
        "model": "gen4.5",
        "promptImage": prompt_image,
        "promptText": f"Animate this scene: {prompt}. Maintain the visual elements from the image.",
        "duration": duration,
        "ratio": RUNWAY_RATIO_MAP.get(ratio, "1280:720"),
    }

    print(f"[RUNWAY] Submitting image-to-video: prompt={prompt[:80]}..., "
          f"duration={duration}s, ratio={ratio}")

    resp = requests.post(
        f"{RUNWAY_API_BASE}/image_to_video",
        headers=_runway_headers(),
        json=payload,
        timeout=60,
    )

    if resp.status_code not in (200, 201):
        print(f"[RUNWAY] Submit error {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()

    data = resp.json()
    task_id = data.get("id", "")
    print(f"[RUNWAY] Task submitted: id={task_id}")
    return task_id


def _runway_submit_text_to_video(prompt: str, duration: int = 5,
                                  ratio: str = "16:9") -> str:
    """
    Submit a text-to-video request to Runway Gen-3 Alpha Turbo.

    Args:
        prompt: text description of the video
        duration: 5 or 10 seconds
        ratio: "16:9" or "9:16"

    Returns:
        task ID string
    """
    duration = 10 if duration > 7 else 5  # Runway supports 5 or 10

    payload = {
        "model": "gen4.5",
        "promptText": prompt,
        "duration": duration,
        "ratio": RUNWAY_RATIO_MAP.get(ratio, "1280:720"),
    }

    print(f"[RUNWAY] Submitting text-to-video: prompt={prompt[:80]}..., "
          f"duration={duration}s, ratio={ratio}")

    resp = requests.post(
        f"{RUNWAY_API_BASE}/text_to_video",
        headers=_runway_headers(),
        json=payload,
        timeout=60,
    )

    if resp.status_code not in (200, 201):
        print(f"[RUNWAY] Submit error {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()

    data = resp.json()
    task_id = data.get("id", "")
    print(f"[RUNWAY] Task submitted: id={task_id}")
    return task_id


def _runway_poll(task_id: str) -> dict:
    """
    Poll Runway task until SUCCEEDED or FAILED.

    Returns:
        dict with video output URL
    """
    deadline = time.time() + RUNWAY_POLL_TIMEOUT
    while time.time() < deadline:
        resp = requests.get(
            f"{RUNWAY_API_BASE}/tasks/{task_id}",
            headers=_runway_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "")
        print(f"[RUNWAY] Poll {task_id[:12]}... status={status}")

        if status == "SUCCEEDED":
            output = data.get("output", [])
            if not output:
                raise RuntimeError(
                    f"Runway task succeeded but no output URL found: "
                    f"{json.dumps(data)[:500]}"
                )
            return {"url": output[0], "task_id": task_id}
        elif status == "FAILED":
            raise RuntimeError(
                f"Runway generation failed (task={task_id}): "
                f"{json.dumps(data)[:500]}"
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
    Generate a video clip using Runway Gen-3 Alpha Turbo.

    This is the recommended engine for scenes with character reference photos,
    as Runway excels at image-to-video with motion while preserving the source image.

    Args:
        scene: dict with at least {prompt, duration}
        output_dir: directory to save clips
        index: scene index for naming
        progress_cb: optional callable(index, status_str)
        cost_cb: optional callable(scene_key, gen_type)
        photo_path: optional photo path for image-to-video (THE key feature)

    Returns:
        path to the generated video clip
    """
    clip_path = os.path.join(output_dir, f"clip_{index:03d}.mp4")
    prompt = scene["prompt"]
    duration = scene.get("duration", 8)
    camera = scene.get("camera_movement", "zoom_in")

    # Add camera movement to prompt for Runway
    camera_suffix = CAMERA_PROMPT_SUFFIXES.get(camera, "")
    gen_prompt = prompt + camera_suffix if camera_suffix else prompt

    has_photo = photo_path and os.path.isfile(photo_path)

    def _report(msg):
        print(f"[RUNWAY][{index}] {msg}")
        if progress_cb:
            progress_cb(index, msg)

    def _record(gen_type):
        if cost_cb:
            cost_cb(str(scene.get("id", index)), gen_type)

    try:
        if has_photo:
            # Image-to-video: THE reason we added Runway
            _report(f"submitting image-to-video to Runway Gen-3 Alpha Turbo (photo: {os.path.basename(photo_path)})...")
            task_id = _runway_submit_image_to_video(
                gen_prompt, photo_path, duration=duration
            )
        else:
            # Text-to-video fallback
            _report("submitting text-to-video to Runway Gen-3 Alpha Turbo...")
            task_id = _runway_submit_text_to_video(
                gen_prompt, duration=duration
            )

        _report(f"polling Runway (task={task_id[:12]}...)")
        video_info = _runway_poll(task_id)
        _report("downloading Runway video...")
        _download(video_info["url"], clip_path)

        # Cost tracking: ~$0.05 per second for gen3a_turbo (5 credits/sec)
        effective_dur = 10 if duration > 7 else 5
        est_cost = effective_dur * RUNWAY_COST_PER_SEC
        print(f"[RUNWAY][{index}] Estimated cost: ~${est_cost:.2f} "
              f"({effective_dur}s x ${RUNWAY_COST_PER_SEC}/s)")
        _record("video")

        # Apply camera movement post-processing if the video needs Ken Burns overlay
        # Runway natively handles camera movement via prompt, so skip Ken Burns
        _report("done (Runway Gen-3 Alpha Turbo)")
        return clip_path

    except Exception as e:
        _report(f"Runway generation failed ({e})")

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
        except Exception as e2:
            _report(f"all generation attempts failed: {e2}")
            raise RuntimeError(
                f"Scene {index} Runway generation failed entirely: {e2}"
            ) from e2


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


def _poll_video(request_id: str) -> dict:
    """Poll until video is done. Returns {url, duration}."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        resp = requests.get(
            f"{API_BASE}/videos/{request_id}",
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "")
        if status == "done":
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
            "model": "grok-2-vision-latest",
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
                                "Describe this image in vivid detail for use as a video generation prompt. "
                                "Focus on: visual style, color palette, lighting, mood, subjects, environment, "
                                "and atmosphere. Keep the description under 100 words, as a comma-separated list "
                                "of visual descriptors. Do not use complete sentences."
                            ),
                        },
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

    # Determine engine: scene-level > global settings > default "grok"
    engine = scene.get("engine", None)
    if not engine:
        settings = _load_settings()
        engine = settings.get("default_engine", ENGINE_GROK)
    engine = engine.lower()

    # Auto-resolve character references if no explicit photo provided
    if not photo_path:
        char_name, char_photo = _resolve_character_references(scene.get("prompt", ""))
        if char_photo:
            photo_path = char_photo
            print(f"[generate_scene] Auto-attached character reference "
                  f"'{char_name}' -> {char_photo}")

    print(f"[generate_scene] index={index}, engine={engine}, "
          f"prompt={scene.get('prompt', '')[:80]}..., photo={photo_path}")

    # Dispatch to engine
    if engine == ENGINE_LUMA:
        return _luma_generate_scene(
            scene, output_dir, index, progress_cb, cost_cb, photo_path
        )
    elif engine == ENGINE_OPENAI:
        return _openai_generate_scene(
            scene, output_dir, index, progress_cb, cost_cb, photo_path
        )
    elif engine == ENGINE_RUNWAY:
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

    if has_photo:
        print(f"[GROK][{index}] Photo exists — using VIDEO gen (not Ken Burns). Photo ignored for Grok video.")

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
            video_info = _poll_video(request_id)
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
        "name": "Runway Gen-3 Alpha Turbo",
        "description": "Fast image-to-video and text-to-video — best for scenes with photos",
        "available": runway_available,
        "missing_key": "RUNWAY_API_KEY" if not runway_available else None,
    })

    return engines
