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

from dotenv import load_dotenv
load_dotenv()

FAL_API_KEY = os.environ.get("FAL_API_KEY", "")
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
    """Submit a job to fal.ai queue and poll until complete.

    Returns the result dict on success, raises RuntimeError on failure.
    """
    url = f"{FAL_BASE}/{endpoint}"
    print(f"[FAL] Submitting to {endpoint}...")

    resp = requests.post(url, headers=_fal_headers(), json=payload, timeout=60)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"fal.ai submit error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()

    # Some endpoints return results directly (sync mode)
    if "images" in data or "video" in data or "output" in data:
        return data

    # Queue mode: poll for result
    request_id = data.get("request_id", "")
    if not request_id:
        # Might be a direct response
        return data

    status_url = f"{FAL_STATUS_BASE}/{endpoint}/requests/{request_id}/status"
    result_url = f"{FAL_STATUS_BASE}/{endpoint}/requests/{request_id}"

    start = time.time()
    polls = 0
    while time.time() - start < timeout:
        time.sleep(5)
        polls += 1
        try:
            sr = requests.get(status_url, headers=_fal_headers(), timeout=30)
            if sr.status_code != 200:
                print(f"[FAL] Poll error {sr.status_code}: {sr.text[:200]}")
                continue
            status_data = sr.json()
            status = status_data.get("status", "UNKNOWN")
            elapsed = int(time.time() - start)
            print(f"[FAL] Poll {request_id[:12]}... status={status} elapsed={elapsed}s")

            if status == "COMPLETED":
                rr = requests.get(result_url, headers=_fal_headers(), timeout=60)
                if rr.status_code == 200:
                    print(f"[FAL] Completed in {elapsed}s after {polls} polls")
                    return rr.json()
                raise RuntimeError(f"Failed to fetch result: {rr.status_code}")

            if status in ("FAILED", "CANCELLED"):
                error = status_data.get("error", "Unknown error")
                raise RuntimeError(f"Job {status}: {error}")

        except requests.RequestException as e:
            print(f"[FAL] Poll network error: {e}")
            continue

    raise RuntimeError(f"Timeout after {timeout}s ({polls} polls)")


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
    """Upload a local file to fal.ai storage, return the URL.

    Uses fal's upload endpoint so local files can be referenced in API calls.
    """
    upload_url = "https://fal.run/fal-ai/any/upload"

    # Use fal's CDN upload
    with open(file_path, "rb") as f:
        content = f.read()

    # Determine content type
    ext = os.path.splitext(file_path)[1].lower()
    ct_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".mp4": "video/mp4"}
    content_type = ct_map.get(ext, "application/octet-stream")

    # fal.ai file upload via their REST storage API
    resp = requests.post(
        "https://fal.ai/api/storage/upload/initiate",
        headers={"Authorization": f"Key {FAL_API_KEY}"},
        json={"file_name": os.path.basename(file_path), "content_type": content_type},
        timeout=30,
    )
    if resp.status_code != 200:
        # Fallback: try direct upload endpoint
        resp2 = requests.put(
            "https://fal.ai/api/storage/upload",
            headers={
                "Authorization": f"Key {FAL_API_KEY}",
                "Content-Type": content_type,
            },
            data=content,
            timeout=60,
        )
        if resp2.status_code == 200:
            return resp2.json().get("url", "")
        raise RuntimeError(f"Upload failed: {resp.status_code} / {resp2.status_code}")

    upload_data = resp.json()
    upload_target = upload_data.get("upload_url", "")
    file_url = upload_data.get("file_url", "")

    if upload_target:
        requests.put(upload_target, data=content,
                     headers={"Content-Type": content_type}, timeout=60)

    return file_url


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
                      num_images: int = 1) -> list:
    """Image editing/composition via Gemini 3.1 Flash with reference images.

    Good for: anchor composition (feed character sheets + environment).
    Accepts up to 10+ reference images.
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
        "cost_per_sec": 0.112,
        "label": "Kling V3 Standard",
    },
    "v3_pro": {
        "endpoint": "fal-ai/kling-video/v3/pro/image-to-video",
        "cost_per_sec": 0.20,
        "label": "Kling V3 Pro",
    },
    "o3_standard": {
        "endpoint": "fal-ai/kling-video/o3/standard/image-to-video",
        "cost_per_sec": 0.168,
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
    generate_audio: bool = False,
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
        payload["prompt"] = prompt[:2000]

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
            fal_elem = {"frontal_image_url": _upload_to_fal(frontal)}
            refs = elem.get("reference_image_paths", [])
            if refs:
                ref_urls = []
                for rp in refs[:3]:  # max 3 additional refs
                    if os.path.isfile(rp):
                        ref_urls.append(_upload_to_fal(rp))
                if ref_urls:
                    fal_elem["reference_image_urls"] = ref_urls
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
