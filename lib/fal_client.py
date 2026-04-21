"""
fal.ai client for LUMN Studio.

Handles:
  - Gemini 3.1 Flash image generation (text-to-image + edit with refs)
  - Kling 3.0 video generation (V3 Standard/Pro, O3 Standard/Pro)

All calls go through fal.ai's REST API (no Chinese servers needed).
"""

import json
import os
import time
import tempfile
import requests

import fal_client as _fal_sdk

from dotenv import load_dotenv
load_dotenv()

FAL_API_KEY = os.environ.get("FAL_API_KEY", "")
# fal-client SDK uses FAL_KEY env var
os.environ["FAL_KEY"] = FAL_API_KEY
FAL_BASE = "https://queue.fal.run"
FAL_STATUS_BASE = "https://queue.fal.run"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fal_headers():
    return {
        "Authorization": f"Key {FAL_API_KEY}",
        "Content-Type": "application/json",
    }


def _fal_submit(endpoint: str, payload: dict, timeout: int = 600) -> dict:
    """Submit a job to fal.ai and wait for result using the official SDK.

    Returns the result dict on success, raises RuntimeError on failure or
    TimeoutError if the job hasn't completed within `timeout` seconds.
    The callback raises if the deadline is exceeded, which crashes the
    subscribe() call and surfaces as an exception here.
    """
    print(f"[FAL] Submitting to {endpoint} (timeout={timeout}s)...")
    start = time.time()
    deadline = start + timeout

    try:
        result = _fal_sdk.subscribe(
            endpoint,
            arguments=payload,
            with_logs=False,
            on_queue_update=lambda update: _on_queue_update(update, start, deadline),
        )
        elapsed = int(time.time() - start)
        print(f"[FAL] Completed in {elapsed}s")
        return result
    except TimeoutError:
        raise
    except Exception as e:
        raise RuntimeError(f"fal.ai error: {e}")


_LAST_POLL_LOG = {}


def _on_queue_update(update, start_time, deadline=None):
    """Callback for queue status updates. Throttles InProgress spam to
    once per 10s per job so the logs stay readable during long runs.

    Raises TimeoutError if deadline is past — caller catches in _fal_submit."""
    now = time.time()
    elapsed = int(now - start_time)
    if deadline is not None and now > deadline:
        _LAST_POLL_LOG.pop(start_time, None)
        raise TimeoutError(f"fal job exceeded {int(deadline - start_time)}s deadline (elapsed={elapsed}s)")
    status_type = type(update).__name__
    if hasattr(update, 'logs') and update.logs:
        for log in update.logs:
            print(f"[FAL] {log.get('message', '')}")
        return
    # Always log non-InProgress statuses (Completed, Failed, Queued first time).
    # Throttle InProgress to once every 10s per job (keyed by start_time).
    if status_type == "InProgress":
        last = _LAST_POLL_LOG.get(start_time, 0)
        if elapsed - last < 10:
            return
        _LAST_POLL_LOG[start_time] = elapsed
    print(f"[FAL] {status_type} elapsed={elapsed}s")
    if status_type in ("Completed", "Failed"):
        _LAST_POLL_LOG.pop(start_time, None)


def _download_file(url: str, dest: str) -> str:
    """Download a URL to a local file path."""
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        f.write(resp.content)
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    print(f"[FAL] Downloaded: {dest} ({size_mb:.1f}MB)")
    return dest


def _upload_to_fal(file_path: str) -> str:
    """Upload a local file to fal.ai CDN via the official SDK.

    Returns the public URL for use in API calls.
    """
    if not os.path.isfile(file_path):
        raise RuntimeError(f"File not found: {file_path}")

    url = _fal_sdk.upload_file(file_path)
    size_kb = os.path.getsize(file_path) / 1024
    print(f"[FAL/upload] {os.path.basename(file_path)} ({size_kb:.0f}KB) -> {url[:60]}...")
    return url


# ---------------------------------------------------------------------------
# Gemini 3.1 Flash — Image Generation
# ---------------------------------------------------------------------------

def gemini_generate_image(prompt: str, resolution: str = "1K",
                          aspect_ratio: str = "16:9",
                          num_images: int = 1) -> list:
    """Text-to-image via Gemini 3.1 Flash (no reference images).

    Good for: character sheets, environment sheets.
    Returns list of local file paths.
    """
    payload = {
        "prompt": prompt,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "num_images": num_images,
        "output_format": "png",
        "safety_tolerance": "4",
    }

    result = _fal_submit("fal-ai/gemini-3.1-flash-image-preview", payload)
    images = result.get("images", [])

    paths = []
    for i, img in enumerate(images):
        url = img.get("url", "")
        if url:
            dest = os.path.join(tempfile.gettempdir(),
                                f"gemini31_{int(time.time())}_{i}.png")
            _download_file(url, dest)
            paths.append(dest)

    return paths


def gemini_edit_image(prompt: str, reference_image_paths: list,
                      resolution: str = "1K",
                      num_images: int = 1,
                      aspect_ratio: str = None) -> list:
    """Image editing/composition via Gemini 3.1 Flash with reference images.

    Good for: anchor composition (feed character sheets + environment).
    Accepts up to 10+ reference images.
    resolution: '0.5K', '1K', '2K', '4K' (higher = more per-tile detail in model sheets).
    aspect_ratio: e.g. '3:4' for portrait body sheets, '16:9' for wides. Omit = model picks.
    Returns list of local file paths.
    """
    # Upload reference images and collect URLs
    image_urls = []
    for path in reference_image_paths:
        if os.path.isfile(path):
            url = _upload_to_fal(path)
            if url:
                image_urls.append(url)
                print(f"[FAL/Gemini] Uploaded ref: {os.path.basename(path)}")

    if not image_urls:
        print("[FAL/Gemini] No valid reference images, falling back to text-to-image")
        return gemini_generate_image(prompt, resolution)

    payload = {
        "prompt": prompt,
        "image_urls": image_urls,
        "resolution": resolution,
        "num_images": num_images,
        "output_format": "png",
        "safety_tolerance": "4",
    }
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio

    result = _fal_submit("fal-ai/gemini-3.1-flash-image-preview/edit", payload)
    images = result.get("images", [])

    paths = []
    for i, img in enumerate(images):
        url = img.get("url", "")
        if url:
            dest = os.path.join(tempfile.gettempdir(),
                                f"gemini31_edit_{int(time.time())}_{i}.png")
            _download_file(url, dest)
            paths.append(dest)

    return paths


# ---------------------------------------------------------------------------
# Kling 3.0 — Video Generation
# ---------------------------------------------------------------------------

# Kling tier configs
KLING_TIERS = {
    "v3_standard": {
        "endpoint": "fal-ai/kling-video/v3/standard/image-to-video",
        "cost_per_sec": 0.084,
        "label": "Kling V3 Standard",
    },
    "v3_pro": {
        "endpoint": "fal-ai/kling-video/v3/pro/image-to-video",
        "cost_per_sec": 0.112,
        "label": "Kling V3 Pro",
    },
    "o3_standard": {
        "endpoint": "fal-ai/kling-video/o3/standard/image-to-video",
        "cost_per_sec": 0.084,
        "label": "Kling O3 Standard",
    },
    "o3_pro": {
        "endpoint": "fal-ai/kling-video/o3/pro/image-to-video",
        "cost_per_sec": 0.392,
        "label": "Kling O3 Pro",
    },
}


def kling_image_to_video(
    start_image_path: str,
    prompt: str,
    duration: int = 5,
    tier: str = "v3_standard",
    end_image_path: str = None,
    elements: list = None,
    multi_prompt: list = None,
    generate_audio: bool = True,
    negative_prompt: str = "blur, distort, low quality, watermark",
    cfg_scale: float = 0.5,
    aspect_ratio: str = "16:9",
) -> str:
    """Generate video from anchor image via Kling 3.0.

    Args:
        start_image_path: local path to first frame anchor image
        prompt: motion description (ignored if multi_prompt provided)
        duration: seconds (3-15)
        tier: v3_standard, v3_pro, o3_standard, o3_pro
        end_image_path: optional last frame anchor (for transition control)
        elements: list of character element dicts for consistency
            [{"frontal_image_path": "...", "reference_image_paths": ["...", ...]}, ...]
        multi_prompt: list of shot dicts for multi-shot mode
            [{"prompt": "...", "duration": "5"}, ...]
        generate_audio: native audio generation
        negative_prompt: terms to exclude
        cfg_scale: prompt adherence (0.0-1.0)
        aspect_ratio: 16:9, 9:16, or 1:1

    Returns:
        local path to downloaded video file
    """
    config = KLING_TIERS.get(tier)
    if not config:
        raise ValueError(f"Unknown Kling tier: {tier}. Options: {list(KLING_TIERS.keys())}")

    endpoint = config["endpoint"]
    label = config["label"]
    cost_est = config["cost_per_sec"] * duration
    print(f"[FAL/Kling] {label} | {duration}s | est ${cost_est:.2f}")

    # Upload start image
    start_url = _upload_to_fal(start_image_path)
    if not start_url:
        raise RuntimeError(f"Failed to upload start image: {start_image_path}")

    # Build payload — field name differs between V3 and O3
    is_o3 = tier.startswith("o3")
    payload = {
        "image_url" if is_o3 else "start_image_url": start_url,
        "duration": str(duration),
        "generate_audio": generate_audio,
        "aspect_ratio": aspect_ratio,
    }

    # End image (transition control)
    if end_image_path and os.path.isfile(end_image_path):
        end_url = _upload_to_fal(end_image_path)
        if end_url:
            payload["end_image_url"] = end_url
            print(f"[FAL/Kling] End frame set: {os.path.basename(end_image_path)}")

    # Multi-prompt mode (overrides single prompt)
    if multi_prompt:
        payload["multi_prompt"] = multi_prompt
        payload["shot_type"] = "customize"
        total_dur = sum(int(s.get("duration", 5)) for s in multi_prompt)
        print(f"[FAL/Kling] Multi-shot: {len(multi_prompt)} shots, {total_dur}s total")
    else:
        payload["prompt"] = prompt[:2500]

    # Negative prompt and cfg (V3 only)
    if not is_o3:
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        payload["cfg_scale"] = cfg_scale

    # Character elements (V3 endpoints)
    if elements and not is_o3:
        fal_elements = []
        for elem in elements:
            frontal = elem.get("frontal_image_path", "")
            if not frontal or not os.path.isfile(frontal):
                continue
            frontal_url = _upload_to_fal(frontal)
            fal_elem = {"frontal_image_url": frontal_url}
            ref_urls = []
            refs = elem.get("reference_image_paths", [])
            for rp in refs[:3]:  # max 3 additional refs
                if os.path.isfile(rp):
                    ref_urls.append(_upload_to_fal(rp))
            # API requires non-empty reference_image_urls; use frontal as fallback
            fal_elem["reference_image_urls"] = ref_urls if ref_urls else [frontal_url]
            fal_elements.append(fal_elem)
            print(f"[FAL/Kling] Element added: {os.path.basename(frontal)}")

        if fal_elements:
            payload["elements"] = fal_elements[:4]  # max 4 total

    # Submit and wait
    result = _fal_submit(endpoint, payload, timeout=600)

    # Extract video URL
    video_info = result.get("video", result.get("output", {}))
    if isinstance(video_info, dict):
        video_url = video_info.get("url", "")
    elif isinstance(video_info, str):
        video_url = video_info
    else:
        # Check alternative response formats
        video_url = result.get("video_url", result.get("url", ""))

    if not video_url:
        raise RuntimeError(f"No video URL in response: {json.dumps(result)[:300]}")

    # Download
    dest = os.path.join(tempfile.gettempdir(),
                        f"kling_{tier}_{int(time.time())}.mp4")
    _download_file(video_url, dest)
    return dest


# ---------------------------------------------------------------------------
# Kling V3 Pro — Motion Control (camera motion or complex action reference)
# ---------------------------------------------------------------------------

def kling_motion_control(
    subject_image_path: str,
    motion_video_path: str,
    character_orientation: str = "image",
    prompt: str = "",
    keep_original_sound: bool = False,
    elements: list = None,
    negative_prompt: str = "blur, distort, low quality, watermark",
    cfg_scale: float = 0.5,
) -> str:
    """Generate video from subject image + motion reference video via Kling V3 Pro.

    Args:
        subject_image_path: local path to the subject/character image
        motion_video_path: local path to the motion-reference video (3-30s)
        character_orientation:
            "image" — subject follows the CAMERA motion of the reference (<=10s).
                      Use for orbits, push-ins, dollies, jibs on a static subject.
            "video" — subject performs the COMPLEX ACTIONS of the reference (<=30s).
                      Use when the subject should match the reference's body motion.
                      Only this mode accepts `elements` for identity lock.
        prompt: optional additional guidance (motion is mostly driven by the ref video)
        keep_original_sound: keep the reference video's audio track
        elements: character elements (only honored when character_orientation='video')
        negative_prompt: terms to exclude
        cfg_scale: prompt adherence (0.0-1.0)

    Returns:
        local path to downloaded video file
    """
    if character_orientation not in ("image", "video"):
        raise ValueError(
            f"character_orientation must be 'image' or 'video', got {character_orientation!r}"
        )

    endpoint = "fal-ai/kling-video/v3/pro/motion-control"
    max_secs = 10 if character_orientation == "image" else 30
    print(
        f"[FAL/Kling] Motion-Control (orientation={character_orientation}, "
        f"max {max_secs}s)"
    )

    subject_url = _upload_to_fal(subject_image_path)
    if not subject_url:
        raise RuntimeError(f"Failed to upload subject image: {subject_image_path}")

    motion_url = _upload_to_fal(motion_video_path)
    if not motion_url:
        raise RuntimeError(f"Failed to upload motion video: {motion_video_path}")

    payload = {
        "image_url": subject_url,
        "video_url": motion_url,
        "character_orientation": character_orientation,
        "keep_original_sound": keep_original_sound,
        "cfg_scale": cfg_scale,
    }
    if prompt:
        payload["prompt"] = prompt[:2500]
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt

    # Elements only supported in 'video' orientation per API spec
    if elements and character_orientation == "video":
        fal_elements = []
        for elem in elements:
            frontal = elem.get("frontal_image_path", "")
            if not frontal or not os.path.isfile(frontal):
                continue
            frontal_url = _upload_to_fal(frontal)
            fal_elem = {"frontal_image_url": frontal_url}
            ref_urls = []
            for rp in elem.get("reference_image_paths", [])[:3]:
                if os.path.isfile(rp):
                    ref_urls.append(_upload_to_fal(rp))
            fal_elem["reference_image_urls"] = ref_urls if ref_urls else [frontal_url]
            fal_elements.append(fal_elem)
            print(f"[FAL/Kling] Element added: {os.path.basename(frontal)}")
        if fal_elements:
            payload["elements"] = fal_elements[:4]
    elif elements and character_orientation == "image":
        print(
            "[FAL/Kling] NOTE: elements ignored in 'image' orientation — "
            "motion-control only honors elements in 'video' mode"
        )

    result = _fal_submit(endpoint, payload, timeout=900)

    video_info = result.get("video", result.get("output", {}))
    if isinstance(video_info, dict):
        video_url = video_info.get("url", "")
    elif isinstance(video_info, str):
        video_url = video_info
    else:
        video_url = result.get("video_url", result.get("url", ""))

    if not video_url:
        raise RuntimeError(f"No video URL in response: {json.dumps(result)[:300]}")

    dest = os.path.join(
        tempfile.gettempdir(),
        f"kling_motion_{character_orientation}_{int(time.time())}.mp4",
    )
    _download_file(video_url, dest)
    return dest


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def generate_anchor(prompt: str, reference_image_paths: list,
                    resolution: str = "1K") -> str:
    """Generate a single anchor image from reference sheets.

    Convenience wrapper around gemini_edit_image.
    Returns local file path or "" on failure.
    """
    try:
        paths = gemini_edit_image(prompt, reference_image_paths, resolution)
        return paths[0] if paths else ""
    except Exception as e:
        print(f"[FAL] Anchor generation failed: {e}")
        return ""


def generate_video_clip(
    start_anchor: str,
    prompt: str,
    duration: int = 5,
    tier: str = "v3_standard",
    end_anchor: str = None,
    elements: list = None,
) -> str:
    """Generate a single video clip from an anchor.

    Convenience wrapper around kling_image_to_video.
    Returns local file path or "" on failure.
    """
    try:
        return kling_image_to_video(
            start_image_path=start_anchor,
            prompt=prompt,
            duration=duration,
            tier=tier,
            end_image_path=end_anchor,
            elements=elements,
        )
    except Exception as e:
        print(f"[FAL] Video generation failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"FAL_API_KEY: {'set' if FAL_API_KEY else 'MISSING'}")
    print(f"Key prefix: {FAL_API_KEY[:12]}..." if FAL_API_KEY else "No key")

    # Quick connectivity test
    try:
        resp = requests.get("https://fal.ai/api/health", timeout=10)
        print(f"fal.ai health: {resp.status_code}")
    except Exception as e:
        print(f"fal.ai unreachable: {e}")
