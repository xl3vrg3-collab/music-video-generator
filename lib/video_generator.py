"""
Video generator using the Grok (xAI) API.
Handles video generation requests, async polling, downloads,
and falls back to image generation + Ken Burns if video fails.
"""

import os
import sys
import time
import subprocess
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://api.x.ai/v1"
POLL_INTERVAL = 5       # seconds between status checks
POLL_TIMEOUT = 300      # max seconds to wait for a single video
MAX_CONCURRENT = 3      # max parallel video generations


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


def _ken_burns(image_path: str, output_path: str, duration: float = 8.0):
    """Create a Ken Burns (slow zoom) video from a still image using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-vf", (
            f"scale=3840:2160,crop=1920:1080:"
            f"'960-960*min(t/{duration},1)':'540-540*min(t/{duration},1)'"
        ),
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", "30",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())


# ---- Public API ----

def generate_scene(scene: dict, index: int, output_dir: str,
                   progress_cb=None, cost_cb=None) -> str:
    """
    Generate a single video clip for a scene.

    Args:
        scene: dict with at least {prompt, duration}
        index: scene index (for naming)
        output_dir: directory to save clips
        progress_cb: optional callable(index, status_str)
        cost_cb: optional callable(scene_key, gen_type) to record cost

    Returns:
        path to the generated video clip
    """
    clip_path = os.path.join(output_dir, f"clip_{index:03d}.mp4")
    prompt = scene["prompt"]
    duration = scene.get("duration", 8)

    def _report(msg):
        if progress_cb:
            progress_cb(index, msg)

    def _record(gen_type):
        if cost_cb:
            cost_cb(str(scene.get("id", index)), gen_type)

    # Try video generation first
    try:
        _report("submitting video request...")
        request_id = _submit_video(prompt)
        _report(f"polling (id={request_id[:12]}...)")
        video_info = _poll_video(request_id)
        _report("downloading clip...")
        _download(video_info["url"], clip_path)
        _record("video")
        _report("done")
        return clip_path
    except Exception as e:
        _report(f"video failed ({e}), falling back to image...")

    # Fallback: image + Ken Burns
    try:
        img_url = _generate_image(prompt)
        img_path = os.path.join(output_dir, f"img_{index:03d}.png")
        _download(img_url, img_path)
        _ken_burns(img_path, clip_path, duration)
        _record("image")
        _report("done (image fallback)")
        return clip_path
    except Exception as e2:
        _report(f"image fallback also failed: {e2}")
        raise RuntimeError(f"Scene {index} generation failed entirely: {e2}") from e2


def generate_all(scenes: list, output_dir: str,
                 progress_cb=None, cost_cb=None) -> list:
    """
    Generate video clips for all scenes with concurrency limit.

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
