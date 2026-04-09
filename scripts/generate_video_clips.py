"""Generate 5 video clips from anchors using image_to_video via gen4_turbo."""
import json
import os
import shutil
import sys
import time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from lib.video_generator import _runway_upload_file, _runway_poll, _download

PLAN_PATH = "output/pipeline/video_plan.json"
CLIPS_DIR = "output/pipeline/clips"
os.makedirs(CLIPS_DIR, exist_ok=True)

RUNWAY_API_KEY = os.environ.get("RUNWAY_API_KEY", "")
RUNWAY_BASE = "https://api.dev.runwayml.com/v1"

# Engine from CLI arg or default
VIDEO_ENGINE = sys.argv[1] if len(sys.argv) > 1 else "gen4_turbo"

CREDITS_PER_SEC = {
    "gen4_turbo": 5,
    "gen4.5": 12,
    "veo3": 40,
    "veo3.1": 40,
    "veo3.1_fast": 15,
}
cps = CREDITS_PER_SEC.get(VIDEO_ENGINE, 5)
print(f"Video engine: {VIDEO_ENGINE} ({cps} credits/sec)")

import requests

def generate_clip(anchor_path, prompt, duration, output_path):
    """image_to_video: anchor as first frame, prompt describes motion."""
    # Upload anchor
    uri = _runway_upload_file(anchor_path)
    if not uri:
        print(f"  Upload failed for {anchor_path}")
        return False

    # Submit image_to_video task
    # Clamp duration to API limits (2-10s)
    api_duration = max(2, min(10, duration))
    payload = {
        "model": VIDEO_ENGINE,
        "promptImage": uri,
        "promptText": prompt[:1000],
        "duration": api_duration,
        "ratio": "1280:720",
    }

    headers = {
        "Authorization": f"Bearer {RUNWAY_API_KEY}",
        "Content-Type": "application/json",
        "X-Runway-Version": "2024-11-06",
    }

    print(f"  Submitting image_to_video ({VIDEO_ENGINE}, {duration}s)...")
    resp = requests.post(f"{RUNWAY_BASE}/image_to_video", json=payload, headers=headers)
    if resp.status_code != 200:
        print(f"  API error {resp.status_code}: {resp.text[:200]}")
        return False

    data = resp.json()
    task_id = data.get("id", "")
    if not task_id:
        print(f"  No task ID returned: {data}")
        return False

    print(f"  Task: {task_id[:12]}...")

    # Poll until complete
    try:
        result = _runway_poll(task_id)
    except RuntimeError as e:
        print(f"  Poll failed: {e}")
        return False

    if not result:
        print(f"  Poll returned empty")
        return False

    # _runway_poll returns {"url": "...", "task_id": "..."}
    video_url = result.get("url", "") if isinstance(result, dict) else str(result)

    if not video_url:
        print(f"  No video URL in result: {result}")
        return False

    _download(video_url, output_path)
    if os.path.isfile(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  Downloaded: {output_path} ({size_mb:.1f}MB)")
        return True
    return False


def main():
    with open(PLAN_PATH) as f:
        plan = json.load(f)

    shots = plan["shots"]
    total = len(shots)
    failed = []
    total_duration = 0

    for i, shot in enumerate(shots):
        shot_id = shot["shot_id"]
        anchor = shot["anchor_image"]
        prompt = shot["video_prompt"]
        duration = shot["duration"]

        print(f"\n{'='*60}")
        print(f"[{i+1}/{total}] {shot_id} — {shot['moment']}")
        print(f"  Duration: {duration}s | Transition out: {shot.get('transition_out', 'none')}")
        print(f"  Prompt: {prompt[:80]}...")
        print(f"{'='*60}")

        if not os.path.isfile(anchor):
            print(f"  ERROR: Anchor not found: {anchor}")
            failed.append(shot_id)
            continue

        # Engine suffix in filename so gen4_turbo and veo clips don't overwrite each other
        engine_tag = VIDEO_ENGINE.replace(".", "_")
        clip_path = os.path.join(CLIPS_DIR, f"{shot_id}_{engine_tag}.mp4")
        if os.path.isfile(clip_path):
            os.remove(clip_path)

        try:
            ok = generate_clip(anchor, prompt, duration, clip_path)
            if ok:
                shot["clip_path"] = os.path.abspath(clip_path)
                shot["clip_status"] = "generated"
                total_duration += duration
            else:
                failed.append(shot_id)
                shot["clip_status"] = "failed"
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed.append(shot_id)
            shot["clip_status"] = "failed"

        time.sleep(1)

    # Save updated plan
    with open(PLAN_PATH, "w") as f:
        json.dump(plan, f, indent=2)

    print(f"\n{'='*60}")
    print(f"DONE: {total - len(failed)}/{total} clips generated")
    print(f"Total video: {total_duration}s")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    credits = total_duration * cps
    print(f"Credits used: ~{credits} ({total_duration}s x {cps}/sec for {VIDEO_ENGINE})")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
