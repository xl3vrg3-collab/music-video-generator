"""
Video generator using the Grok (xAI) API.
Handles video generation requests, async polling, downloads,
and falls back to image generation + Ken Burns if video fails.
Supports photo+text style transfer via Grok image API with base64.
Supports camera movement presets for Ken Burns animations.
"""

import base64
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

    Args:
        scene: dict with at least {prompt, duration}
               optional: camera_movement (preset name)
        index: scene index (for naming)
        output_dir: directory to save clips
        progress_cb: optional callable(index, status_str)
        cost_cb: optional callable(scene_key, gen_type) to record cost
        photo_path: optional path to an uploaded photo for style transfer

    Returns:
        path to the generated video clip
    """
    clip_path = os.path.join(output_dir, f"clip_{index:03d}.mp4")
    prompt = scene["prompt"]
    duration = scene.get("duration", 8)
    camera = scene.get("camera_movement", "zoom_in")

    # Append camera movement to prompt for AI generation
    camera_suffix = CAMERA_PROMPT_SUFFIXES.get(camera, "")
    gen_prompt = prompt + camera_suffix if camera_suffix else prompt

    has_photo = photo_path and os.path.isfile(photo_path)
    print(f"[generate_scene] index={index}, prompt={gen_prompt[:80]}..., photo_path={photo_path}, has_photo={has_photo}, camera={camera}")

    def _report(msg):
        print(f"[generate_scene][{index}] {msg}")
        if progress_cb:
            progress_cb(index, msg)

    def _record(gen_type):
        if cost_cb:
            cost_cb(str(scene.get("id", index)), gen_type)

    # If scene has a photo, use photo+prompt pipeline (style transfer)
    if has_photo:
        print(f"[generate_scene][{index}] Photo detected at {photo_path}, using photo+prompt pipeline")
        try:
            _report("sending photo to Grok for style transfer...")
            edit_strength = scene.get("edit_strength", 0.3)
            img_url = _generate_image_from_photo(gen_prompt, photo_path, edit_strength)
            print(f"[generate_scene][{index}] Got styled image URL: {img_url[:80]}...")
            img_path = os.path.join(output_dir, f"img_{index:03d}_styled.png")
            _report("downloading styled image...")
            _download(img_url, img_path)
            print(f"[generate_scene][{index}] Downloaded styled image to {img_path}")
            _report("creating Ken Burns video from styled image...")
            _ken_burns(img_path, clip_path, duration, camera=camera)
            _record("image")
            _report("done (photo style transfer + Ken Burns)")
            print(f"[generate_scene][{index}] SUCCESS: photo+prompt clip at {clip_path}")
            return clip_path
        except Exception as e:
            print(f"[generate_scene][{index}] Photo+prompt failed: {e}")
            _report(f"photo style transfer failed ({e}), falling back to video...")

    # Try video generation (text-only)
    try:
        _report("submitting video request...")
        request_id = _submit_video(gen_prompt)
        _report(f"polling (id={request_id[:12]}...)")
        video_info = _poll_video(request_id)
        _report("downloading clip...")
        _download(video_info["url"], clip_path)
        _record("video")
        _report("done")
        return clip_path
    except Exception as e:
        _report(f"video failed ({e}), falling back to image...")

    # Fallback: image + Ken Burns with camera preset
    try:
        img_url = _generate_image(gen_prompt)
        img_path = os.path.join(output_dir, f"img_{index:03d}.png")
        _download(img_url, img_path)
        _ken_burns(img_path, clip_path, duration, camera=camera)
        _record("image")
        _report("done (image fallback)")
        return clip_path
    except Exception as e2:
        _report(f"image fallback also failed: {e2}")
        raise RuntimeError(f"Scene {index} generation failed entirely: {e2}") from e2


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
