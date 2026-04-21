"""Drive remaining 15 anchor regens/new-gens through the authenticated LUMN
v6 pipeline: generate -> Sonnet select -> promote to selected.png.

Uses the same server-side code paths as the UI ("Generate Anchor" -> "Sonnet
Select" -> "Accept"), via HTTP with a session cookie from /api/auth/login.
Runs sequentially so fal.ai doesn't rate-limit and the Kling budget stays
predictable.

Usage:
    python tools/opus_drive_anchors.py [--only shot_id1,shot_id2]
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = ROOT / "output/pipeline/opus_storylines/plan_final_20260419_152717.json"
SCENES_PATH = ROOT / "output/projects/default/prompt_os/scenes.json"
ENVS_PATH = ROOT / "output/projects/default/prompt_os/environments.json"
CHARS_PATH = ROOT / "output/projects/default/prompt_os/characters.json"
LOG_DIR = ROOT / "logs"

BASE = "http://localhost:3849"
EMAIL = "mctest@local"
PASSWORD = "testpass123"

SIZE_TO_COMPOSITION = {
    "wide": "wide establishing, TB small in frame",
    "medium": "medium shot, TB three-quarter to camera, head + upper body",
    "close": "close-up, eyes + emblem fill centered",
}


def _resolve_sheet_fs(url: str | None) -> str | None:
    """Turn a /output/... URL into a filesystem path under lumn/output/."""
    if not url:
        return None
    if url.startswith("/output/projects/default/"):
        fs = "output/" + url[len("/output/"):]
    elif url.startswith("/output/prompt_os/"):
        fs = "output/projects/default/prompt_os/" + url[len("/output/prompt_os/"):]
    elif url.startswith("/output/preproduction/"):
        fs = "output/preproduction/" + url[len("/output/preproduction/"):]
    elif url.startswith("/output/"):
        fs = "output/" + url[len("/output/"):]
    else:
        fs = url.lstrip("/")
    candidate = ROOT / fs
    if candidate.is_file():
        return fs
    # Fallback: search for the filename under output/
    name = Path(fs).name
    for p in (ROOT / "output").rglob(name):
        try:
            rel = p.relative_to(ROOT)
            return str(rel).replace("\\", "/")
        except ValueError:
            continue
    return None


def build_prompt(shot: dict, scene: dict, env: dict) -> str:
    """Compose an Opus-aligned anchor prompt from plan + env + scene."""
    size = shot["shot_size"]
    comp = SIZE_TO_COMPOSITION.get(size, size)
    cont = scene.get("continuity_anchors", {})
    env_desc = env.get("description", "").strip()
    env_name = env.get("name", "")
    env_loc = env.get("location", "").strip()
    lighting = cont.get("lighting", "")
    wardrobe = cont.get("wardrobe", "navy hoodie down, blue beads at collarbone, fur dry")
    weather = cont.get("weather", "")
    time_of_day = cont.get("time_of_day", "")
    eyeline = cont.get("eyeline_target", "")
    key_props = cont.get("key_props", "")
    acting = shot.get("acting", "")
    micro = shot.get("micro_expression", "")
    camera = shot.get("camera", "")
    purpose = shot.get("purpose", "")

    lines = [
        "High-end anime cinematic still, Makoto Shinkai realism, Your Name / Weathering With You aesthetic.",
        f"Composition: {comp}. {camera}.",
        f"Subject: Trillion Bear — small chibi-stylized bear, navy hoodie (hood DOWN), blue beads at collarbone, CRESCENT emblem ONLY on forehead (never floating). Wardrobe: {wardrobe}.",
        f"Action: {acting}. Micro-expression: {micro}. Eyeline: {eyeline}.",
        f"Environment: {env_name} — {env_loc}. {env_desc[:280]}",
        f"Lighting: {lighting}.",
        f"Atmosphere: {weather}. Time: {time_of_day}.",
        f"Key props: {key_props}.",
        f"Intent: {purpose[:160]}",
        "Cel-shaded, painterly, atmospheric depth. NO text, NO HUD, NO signage letters, NO vehicle interior/dashboard, NO second TB, emblem on forehead only.",
    ]
    return " ".join(ln for ln in lines if ln and not ln.endswith(": ."))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated opus shot ids (e.g. 3a,3b)")
    ap.add_argument("--dry", action="store_true", help="print prompt only; no gen")
    args = ap.parse_args()

    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    scenes = json.loads(SCENES_PATH.read_text(encoding="utf-8"))
    envs = json.loads(ENVS_PATH.read_text(encoding="utf-8"))
    chars = json.loads(CHARS_PATH.read_text(encoding="utf-8"))
    char_list = chars if isinstance(chars, list) else chars.get("characters", [])
    tb = next((c for c in char_list if "Trillion" in (c.get("name") or "")), None)
    if not tb:
        print("TB character not found")
        sys.exit(1)
    tb_sheet_url = (tb.get("sheetImages") or [{}])[0].get("url") or tb.get("previewImage")
    tb_sheet = _resolve_sheet_fs(tb_sheet_url)
    if not tb_sheet:
        print(f"TB sheet fs not resolvable from {tb_sheet_url!r}")
        sys.exit(1)
    print(f"[PREP] TB sheet: {tb_sheet}")

    env_by_name: dict[str, dict] = {e["name"]: e for e in envs}

    scene_by_opus_shot: dict[str, dict] = {}
    for sc in scenes:
        oid = sc.get("opus_shot_id")
        if oid:
            scene_by_opus_shot[oid] = sc

    # Flatten plan -> (shot_id, scene_dict, shot_dict)
    plan_shots: list[tuple[str, dict, dict]] = []
    for sc in plan.get("scenes", []):
        for sh in sc.get("shots", []):
            plan_shots.append((sh["id"], sc, sh))

    # Targets: everything except 1a/1b/2a-d/4a-d/5a/5b/9a/9b (already exist)
    # AND except 1b which we just did.
    preserved_ids = {"1a", "2a", "2b", "2c", "2d", "4a", "4b", "4c", "4d", "5a", "5b", "9a", "9b"}
    already_done = {"1b"}
    skip = preserved_ids | already_done

    targets = [(oid, sc, sh) for (oid, sc, sh) in plan_shots if oid not in skip]
    if args.only:
        wanted = set(args.only.split(","))
        targets = [t for t in targets if t[0] in wanted]

    print(f"[PREP] {len(targets)} shots to process: {[t[0] for t in targets]}")

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"opus_drive_anchors_{time.strftime('%Y%m%d_%H%M%S')}.log"
    log_f = log_path.open("w", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line)
        log_f.write(line + "\n")
        log_f.flush()

    # Session login
    sess = requests.Session()
    r = sess.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=10)
    r.raise_for_status()
    auth = r.json()
    csrf = auth["csrf_token"]
    log(f"[LOGIN] uid={auth['user']['id']} balance={auth['user']['credits_cents']}c csrf={csrf[:8]}")

    results: dict[str, dict] = {}
    for (oid, sc, sh) in targets:
        pos_scene = scene_by_opus_shot.get(oid)
        if not pos_scene:
            log(f"[SKIP] {oid} — no POS scene with opus_shot_id={oid}")
            continue
        shot_id = pos_scene["id"]  # LUMN v6 shot id == POS scene id
        env_name = sc.get("location", "")
        env = env_by_name.get(env_name)
        if not env:
            log(f"[SKIP] {oid} — env {env_name!r} not found")
            continue
        env_sheet_url = (env.get("sheetImages") or [{}])[0].get("url") or env.get("previewImage")
        env_sheet = _resolve_sheet_fs(env_sheet_url)
        if not env_sheet:
            log(f"[SKIP] {oid} — env sheet fs unresolved for {env_name}")
            continue

        prompt = build_prompt(sh, sc, env)
        log(f"[{oid}] shot_id={shot_id} env={env_name} size={sh['shot_size']} prompt_len={len(prompt)}")
        if args.dry:
            log(f"[{oid}] DRY RUN prompt:\n{prompt}\n")
            continue

        # 1. Generate
        t0 = time.time()
        try:
            gen_r = sess.post(
                f"{BASE}/api/v6/anchor/generate",
                headers={"X-CSRF-Token": csrf},
                json={
                    "shot_id": shot_id,
                    "prompt": prompt,
                    "reference_image_paths": [tb_sheet, env_sheet],
                    "num_images": 3,
                },
                timeout=180,
            )
        except requests.RequestException as e:
            log(f"[{oid}] gen REQUEST ERR: {e}")
            continue
        elapsed = time.time() - t0
        if gen_r.status_code != 200:
            log(f"[{oid}] gen HTTP {gen_r.status_code}: {gen_r.text[:200]}")
            continue
        gen_j = gen_r.json()
        saved = gen_j.get("saved_paths") or gen_j.get("paths") or []
        log(f"[{oid}] gen ok {elapsed:.1f}s saved={len(saved)} first={saved[0] if saved else '?'}")
        if len(saved) < 3:
            log(f"[{oid}] only {len(saved)} candidates — skipping Sonnet select")
            results[oid] = {"status": "partial", "saved": saved}
            continue

        # 2. Sonnet select
        # Candidate paths: convert absolute saved paths to relative under output/pipeline
        cand_paths = []
        for p in saved:
            pp = Path(p)
            try:
                rel = pp.relative_to(ROOT)
                cand_paths.append(str(rel).replace("\\", "/"))
            except ValueError:
                cand_paths.append(str(pp).replace("\\", "/"))
        try:
            sel_r = sess.post(
                f"{BASE}/api/v6/sonnet/select",
                headers={"X-CSRF-Token": csrf},
                json={
                    "shot_id": shot_id,
                    "candidate_paths": cand_paths,
                    "ref_sheet": tb_sheet,
                    "shot_info": {"title": f"{oid} {env_name} {sh['shot_size']}", "prompt": prompt[:300]},
                },
                timeout=120,
            )
        except requests.RequestException as e:
            log(f"[{oid}] opus/select REQUEST ERR: {e}")
            continue
        if sel_r.status_code != 200:
            log(f"[{oid}] opus/select HTTP {sel_r.status_code}: {sel_r.text[:200]}")
            continue
        sel_j = sel_r.json()
        pick_letter = sel_j.get("pick", "A")
        confidence = sel_j.get("confidence")
        reason = (sel_j.get("pick_reason") or "")[:160]
        # Map A/B/C → candidate_0/1/2
        letter_to_idx = {"A": 0, "B": 1, "C": 2}
        pick_idx = letter_to_idx.get(pick_letter.upper(), 0)
        if pick_idx >= len(cand_paths):
            pick_idx = 0
        pick_path = cand_paths[pick_idx]
        # Convert to /api/v6/anchor-image/... URL for override
        rel_url = pick_path.replace("output/pipeline/anchors_v6/", "")
        pick_url = f"/api/v6/anchor-image/{rel_url}"
        log(f"[{oid}] opus pick={pick_letter}→{Path(pick_path).name} conf={confidence} reason={reason!r}")

        # 3. Override (promotes to selected.png)
        try:
            ov_r = sess.post(
                f"{BASE}/api/v6/sonnet/override",
                headers={"X-CSRF-Token": csrf},
                json={"shot_id": shot_id, "selected": pick_url},
                timeout=30,
            )
        except requests.RequestException as e:
            log(f"[{oid}] override REQUEST ERR: {e}")
            continue
        if ov_r.status_code != 200:
            log(f"[{oid}] override HTTP {ov_r.status_code}: {ov_r.text[:200]}")
            continue
        ov_j = ov_r.json()
        log(f"[{oid}] promoted → {Path(ov_j.get('promoted_to','?')).name}")
        results[oid] = {
            "status": "ok",
            "shot_id": shot_id,
            "pick": pick_letter,
            "confidence": confidence,
            "reason": reason,
            "elapsed_s": elapsed,
        }

    log("")
    log("=" * 72)
    log(f"DONE. {sum(1 for v in results.values() if v.get('status') == 'ok')}/{len(targets)} anchors promoted.")
    for oid, r in results.items():
        log(f"  {oid}: {r.get('status')} pick={r.get('pick')} conf={r.get('confidence')}")
    log_f.close()
    print(f"\nLog → {log_path}")


if __name__ == "__main__":
    main()
