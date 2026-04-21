"""Batch-audit all 30 TB v8 shot candidates, promote best, save verdicts."""
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.anchor_auditor import audit_scene_anchor

ROOT = r"C:/Users/Mathe/lumn"
SCENES = os.path.join(ROOT, "output/projects/default/prompt_os/scenes.json")
ANCHORS_U = os.path.join(ROOT, "output/pipeline/anchors_v6/u_14")
ANCHORS_FLAT = os.path.join(ROOT, "output/pipeline/anchors_v6")
CALLOUT = os.path.join(ROOT, "output/prompt_os/previews/characters/6d31f281-4cc_callout_1776646911.png")
OUT = os.path.join(ROOT, "output/projects/default/prompt_os/v8_anchor_audit_last.json")


def score(verdict: dict) -> tuple[int, int, int]:
    """Lower is better. (failed, high_sev, total_viols)."""
    passed = 1 if verdict.get("pass") else 0
    viols = verdict.get("violations") or []
    high = sum(1 for v in viols if v.get("severity") == "high")
    return (1 - passed, high, len(viols))


TB_RULES = {
    "emblem_tips_orientation": "up",  # TB canonical: cup/boat orientation, tips straight up
}


def audit_candidate(scene: dict, cand_path: str) -> dict:
    try:
        verdict = audit_scene_anchor(scene, cand_path, character_rules=TB_RULES,
                                     callout_path=CALLOUT)
        return {"candidate": os.path.basename(cand_path), **verdict}
    except Exception as e:
        return {"candidate": os.path.basename(cand_path), "pass": False, "error": str(e)[:200]}


def audit_shot(scene: dict) -> dict:
    sid = scene["id"]
    opus = scene.get("opus_shot_id", "?")
    shot_dir = os.path.join(ANCHORS_U, sid)
    cands = sorted([f for f in os.listdir(shot_dir) if f.startswith("candidate_") and f.endswith(".png")])
    results = []
    for c in cands:
        cp = os.path.join(shot_dir, c)
        t0 = time.time()
        r = audit_candidate(scene, cp)
        r["elapsed_s"] = round(time.time() - t0, 1)
        results.append(r)
    ranked = sorted(results, key=lambda r: score(r))
    best = ranked[0]
    return {"shot_id": sid, "opus": opus, "best": best["candidate"],
            "best_pass": best.get("pass", False), "best_viols": len(best.get("violations") or []),
            "candidates": results}


def promote_selected(shot: dict):
    if not shot.get("best"):
        return
    sid = shot["shot_id"]
    src = os.path.join(ANCHORS_U, sid, shot["best"])
    if not os.path.isfile(src):
        return
    for dst_dir in (os.path.join(ANCHORS_U, sid), os.path.join(ANCHORS_FLAT, sid)):
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "selected.png")
        shutil.copyfile(src, dst)


def main():
    scenes_data = json.load(open(SCENES, "r", encoding="utf-8"))
    scenes = scenes_data.get("scenes", scenes_data) if isinstance(scenes_data, dict) else scenes_data
    print(f"[AUDIT] {len(scenes)} shots × 3 candidates = {len(scenes)*3} calls")
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(audit_shot, s): s.get("opus_shot_id", "?") for s in scenes}
        for fut in as_completed(futures):
            opus = futures[fut]
            try:
                row = fut.result()
                rows.append(row)
                tag = "PASS" if row["best_pass"] else "FAIL"
                print(f"[AUDIT] {opus} {tag} best={row['best']} viols={row['best_viols']}")
            except Exception as e:
                print(f"[AUDIT] {opus} ERROR {e}")

    rows.sort(key=lambda r: r["opus"])
    passes = sum(1 for r in rows if r["best_pass"])
    fails = [r["opus"] for r in rows if not r["best_pass"]]
    summary = {
        "total": len(rows),
        "pass": passes,
        "fail": len(rows) - passes,
        "fail_opus": fails,
        "elapsed_s": round(time.time() - t0, 1),
        "rows": rows,
    }
    json.dump(summary, open(OUT, "w", encoding="utf-8"), indent=2)
    print(f"\n[AUDIT] {passes}/{len(rows)} PASS · {len(rows)-passes} FAIL")
    if fails:
        print(f"[AUDIT] fails: {fails}")
    print(f"[AUDIT] verdicts saved to {OUT}")
    print(f"[AUDIT] elapsed: {summary['elapsed_s']}s")

    for r in rows:
        promote_selected(r)
    print(f"[AUDIT] promoted best-candidate → selected.png for {len(rows)} shots")


if __name__ == "__main__":
    main()
