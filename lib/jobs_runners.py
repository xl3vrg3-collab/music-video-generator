"""
LUMN Studio — job runners executed by lib.worker threads.

These are the "expensive" parts of v6 generation pulled out of the HTTP
request handler so they can run in a bounded worker pool with timeouts.
The sync request handler in server.py still exists and remains the
authoritative path for QA / identity-gate / preview-prompt; the async
runners here cover the long-tail fal.ai calls so a stalled Kling request
doesn't pin an HTTP thread.

Runners are pure functions of (job_id, payload). They report progress
via lib.worker.progress() and return a result dict on success.

Pre-checks (auth, moderation, budget, rate limit) MUST be done before
enqueue — the runner trusts the payload.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

import lib.db as lumn_db
import lib.worker as worker

# Resolve OUTPUT_DIR the same way server.py does.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(_REPO, "output")


def run_anchor_job(job_id: str, payload: dict) -> dict:
    """Background runner for /api/v6/anchor/generate_async.

    Payload shape:
      {
        "user_id":   int,
        "shot_id":   str,
        "prompt":    str,         # already enriched + moderated
        "ref_paths": [str],       # already validated to exist
        "num_images": int,
        "cost_cents": int,        # already budgeted
      }
    """
    from lib.fal_client import gemini_edit_image

    user_id   = int(payload.get("user_id", 0) or 0)
    shot_id   = payload.get("shot_id", "unknown")
    prompt    = payload.get("prompt", "")
    ref_paths = payload.get("ref_paths", []) or []
    num_imgs  = max(1, int(payload.get("num_images", 1) or 1))
    cost_c    = max(1, int(payload.get("cost_cents", 1) or 1))
    reserved  = bool(payload.get("reserved", False))

    # Runner does NOT refund on failure — _run_with_guard handles that
    # centrally so retries don't double-refund. Just raise normally.
    worker.progress(job_id, "calling_fal", 20)
    paths = gemini_edit_image(
        prompt=prompt,
        reference_image_paths=[p for p in ref_paths if os.path.isfile(p)],
        resolution="1K",
        num_images=num_imgs,
    )

    worker.progress(job_id, "saving", 70)
    if user_id > 0:
        anchor_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6", f"u_{user_id}", shot_id)
    else:
        anchor_dir = os.path.join(OUTPUT_DIR, "pipeline", "anchors_v6", shot_id)
    os.makedirs(anchor_dir, exist_ok=True)

    saved: list[str] = []
    for i, src in enumerate(paths):
        dest = os.path.join(anchor_dir,
                            f"candidate_{i}.png" if num_imgs > 1 else "selected.png")
        shutil.copy2(src, dest)
        saved.append(dest)

    # If the credits were already reserved at enqueue, we're done — no
    # second charge. Legacy callers without reserved=True still use the
    # old post-charge path.
    if not reserved and user_id > 0:
        worker.progress(job_id, "charging", 90)
        try:
            lumn_db.charge_user(user_id, cost_c, "anchor",
                                {"shot_id": shot_id, "count": len(saved),
                                 "async": True})
        except Exception as e:
            return {"paths": saved, "warning": f"charge_failed: {e}"}

    return {
        "shot_id": shot_id,
        "paths": saved,
        "selected_path": saved[0] if saved else None,
        "count": len(saved),
    }


def run_clip_job(job_id: str, payload: dict) -> dict:
    """Background runner for /api/v6/clip/generate_async.

    Payload shape:
      {
        "user_id":        int,
        "shot_id":        str,
        "prompt":         str,           # ignored if multi_prompt set
        "anchor_path":    str,
        "duration":       int,           # 3-15
        "tier":           str,           # v3_standard | v3_pro | ...
        "end_image_path": str | None,    # optional morph-to frame
        "multi_prompt":   list | None,   # [{prompt, duration}, ...]
        "elements":       list | None,
        "cfg_scale":      float,
        "cost_cents":     int,
      }
    """
    from lib.fal_client import kling_image_to_video

    user_id        = int(payload.get("user_id", 0) or 0)
    shot_id        = payload.get("shot_id", "unknown")
    prompt         = payload.get("prompt", "")
    anchor_path    = payload.get("anchor_path", "")
    duration       = int(payload.get("duration", 5) or 5)
    tier           = payload.get("tier") or "v3_standard"
    end_image_path = payload.get("end_image_path") or None
    multi_prompt   = payload.get("multi_prompt") or None
    elements       = payload.get("elements") or None
    cfg_scale      = float(payload.get("cfg_scale", 0.5) or 0.5)
    cost_c         = max(1, int(payload.get("cost_cents", 1) or 1))
    reserved       = bool(payload.get("reserved", False))

    if not anchor_path or not os.path.isfile(anchor_path):
        raise FileNotFoundError(f"anchor not found: {anchor_path}")
    if end_image_path and not os.path.isfile(end_image_path):
        end_image_path = None

    provenance = "multi_prompt" if multi_prompt else ("morph" if end_image_path else "single")
    print(f"[KLING] shot={shot_id} dur={duration}s tier={tier} mode={provenance}")

    worker.progress(job_id, "calling_kling", 20)
    video_path = kling_image_to_video(
        start_image_path=anchor_path,
        prompt=prompt,
        duration=duration,
        tier=tier,
        end_image_path=end_image_path,
        multi_prompt=multi_prompt,
        elements=elements,
        cfg_scale=cfg_scale,
    )

    worker.progress(job_id, "saving", 80)
    if user_id > 0:
        clip_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6", f"u_{user_id}", shot_id)
    else:
        clip_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6", shot_id)
    os.makedirs(clip_dir, exist_ok=True)
    dest = os.path.join(clip_dir, "selected.mp4")
    shutil.copy2(video_path, dest)
    try:
        root_clip_dir = os.path.join(OUTPUT_DIR, "pipeline", "clips_v6", shot_id)
        os.makedirs(root_clip_dir, exist_ok=True)
        root_dest = os.path.join(root_clip_dir, "selected.mp4")
        shutil.copy2(video_path, root_dest)
    except Exception:
        pass

    if not reserved and user_id > 0:
        worker.progress(job_id, "charging", 95)
        try:
            lumn_db.charge_user(user_id, cost_c, "clip",
                                {"shot_id": shot_id, "duration": duration,
                                 "async": True})
        except Exception as e:
            return {"path": dest, "warning": f"charge_failed: {e}"}

    return {"shot_id": shot_id, "path": dest, "duration": duration}


def register_all() -> None:
    """Register runners with the worker module. Called once at server boot."""
    worker.register_runner("v6_anchor", run_anchor_job)
    worker.register_runner("v6_clip", run_clip_job)
