"""Batch regen all 30 v8 TB anchors via the LUMN v6 pipeline.

Uses the authenticated UI path (login -> /api/v6/anchor/generate) so that
POS reference injection picks up the freshly-approved TB sheet
(6d31f281-4cc_full_1776697777.png) with the canonical cup-crescent.

Runs sequentially at 1 shot at a time to keep fal.ai polite. ~30s/shot -> ~15min.
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SCENES_PATH = ROOT / "output/projects/default/prompt_os/scenes.json"
ENVS_PATH = ROOT / "output/projects/default/prompt_os/environments.json"
LOG_DIR = ROOT / "logs"

BASE = "http://localhost:3849"
EMAIL = "mctest@local"
PASSWORD = "testpass123"

SIZE_TO_COMPOSITION = {
    "wide": "wide establishing, TB small in frame",
    "medium": "medium shot, TB three-quarter to camera, head + upper body",
    "close": "close-up, eyes + emblem fill centered",
}


def build_prompt(scene: dict, env: dict) -> str:
    """Compose an anchor prompt for a v8 scene."""
    size_hint = (scene.get("cameraAngle", "") or "").lower()
    if "close" in size_hint or "ecu" in size_hint or "mcu" in size_hint:
        comp = SIZE_TO_COMPOSITION["close"]
    elif "wide" in size_hint or "establishing" in size_hint:
        comp = SIZE_TO_COMPOSITION["wide"]
    else:
        comp = SIZE_TO_COMPOSITION["medium"]

    env_name = env.get("name", "")
    env_loc = env.get("location", "").strip()
    env_desc = (env.get("description", "") or "").strip()
    camera = scene.get("cameraMovement", "") or scene.get("cameraAngle", "")
    acting = scene.get("shotDescription", "")
    narrative = (scene.get("narrativeIntent", "") or "").strip()
    emotion = scene.get("emotion", "")

    lines = [
        "High-end anime cinematic still, Makoto Shinkai realism, Your Name / Weathering With You aesthetic.",
        f"Composition: {comp}. {camera}.",
        "Subject: Trillion Bear - small chibi-stylized bear, navy hoodie (hood DOWN), blue beads at collarbone, "
        "white-silver CRESCENT emblem on forehead ONLY, in CUP/BOAT orientation with both horns pointing STRAIGHT UP "
        "(vertical, parallel) and concave opening facing the sky.",
        f"Action: {acting}.",
        f"Emotion: {emotion}.",
        f"Environment: {env_name} - {env_loc}. {env_desc[:280]}",
        f"Intent: {narrative[:160]}",
        "Cel-shaded, painterly, atmospheric depth. NO text, NO HUD, NO signage letters, "
        "NO second TB, emblem on forehead only.",
    ]
    return " ".join(ln for ln in lines if ln and not ln.endswith(": ."))


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated opus shot ids (e.g. 1a,2b)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    scenes_data = json.loads(SCENES_PATH.read_text(encoding="utf-8"))
    scenes = scenes_data if isinstance(scenes_data, list) else scenes_data.get("scenes", [])
    envs_data = json.loads(ENVS_PATH.read_text(encoding="utf-8"))
    envs = envs_data if isinstance(envs_data, list) else envs_data.get("environments", [])
    env_by_id = {e["id"]: e for e in envs}

    only = set(args.only.split(",")) if args.only else None
    targets = [s for s in scenes if (not only) or s.get("opus_shot_id") in only]
    print(f"[PREP] {len(targets)} shots to regen")

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"batch_regen_v8_{time.strftime('%Y%m%d_%H%M%S')}.log"
    log_f = log_path.open("w", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        log_f.write(line + "\n"); log_f.flush()

    sess = requests.Session()
    r = sess.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=10)
    r.raise_for_status()
    auth = r.json()
    csrf = auth["csrf_token"]
    log(f"[LOGIN] uid={auth['user']['id']} balance={auth['user']['credits_cents']}c")

    ok_count = fail_count = 0
    for scene in targets:
        shot_id = scene["id"]
        opus = scene.get("opus_shot_id", "?")
        env = env_by_id.get(scene.get("environmentId", "")) or {}
        prompt = build_prompt(scene, env)
        log(f"[{opus}] shot_id={shot_id} env={env.get('name','?')} prompt_len={len(prompt)}")
        if args.dry:
            log(f"[{opus}] DRY: {prompt[:240]}...")
            continue

        t0 = time.time()
        try:
            r = sess.post(
                f"{BASE}/api/v6/anchor/generate",
                headers={"X-CSRF-Token": csrf},
                json={
                    "shot_id": shot_id,
                    "prompt": prompt,
                    "num_images": 3,
                    "shot_context": {"shot_id": shot_id},
                },
                timeout=240,
            )
        except requests.RequestException as e:
            log(f"[{opus}] REQUEST ERR: {e}")
            fail_count += 1
            continue
        dur = time.time() - t0
        if r.status_code != 200:
            log(f"[{opus}] HTTP {r.status_code}: {r.text[:300]}")
            fail_count += 1
            continue
        j = r.json()
        saved = j.get("saved_paths") or j.get("paths") or []
        log(f"[{opus}] OK {dur:.1f}s saved={len(saved)}")
        ok_count += 1

    log(f"[DONE] ok={ok_count} fail={fail_count} total={len(targets)}")
    log_f.close()


if __name__ == "__main__":
    main()
