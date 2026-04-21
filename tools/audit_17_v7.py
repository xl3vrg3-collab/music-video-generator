"""One-off Opus audit of the v7 batch (17 shots) before Kling spend."""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.anchor_auditor import audit_batch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scenes_path = os.path.join(ROOT, "output", "projects", "default", "prompt_os", "scenes.json")
anchors_dir = os.path.join(ROOT, "output", "pipeline", "anchors_v6")
audits_dir  = os.path.join(ROOT, "output", "pipeline", "audits")
os.makedirs(audits_dir, exist_ok=True)

TARGETS = {
    "58a4cf26-a6e","51c246a8-5da","368d90cc-f49","b435b5a3-14b","385cbb92-8fc",
    "46193203-020","320fa568-e4a","92845e84-03c","aac41ef5-9c4","449b9844-9c1",
    "ded81c46-3f2","49fe880a-932","6f8846fd-964","3ef338b8-02c","13159ff3-a72",
    "62e2e847-059","7b0a1dac-868",
}

def main() -> int:
    with open(scenes_path, "r", encoding="utf-8") as f:
        scenes = json.load(f)
    subset = [s for s in scenes if s.get("id") in TARGETS]
    print(f"[audit_17_v7] auditing {len(subset)}/{len(TARGETS)} target shots via Opus vision...")
    t0 = time.time()
    report = audit_batch(subset, anchors_dir)
    elapsed = time.time() - t0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(audits_dir, f"v7_batch_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\n  TOTAL:   {report['total']}")
    print(f"  PASS:    {report['passed']}")
    print(f"  FAIL:    {report['failed']}")
    print(f"  MISSING: {report['missing']}")
    print(f"  report:  {out}")
    print(f"  elapsed: {elapsed:.1f}s\n")

    for r in report["results"]:
        sid = r.get("shot_id") or r.get("id", "?")
        name = r.get("name") or ""
        status = r.get("status") or ("pass" if r.get("pass") else "fail")
        v = r.get("verdict") or {}
        issues = v.get("violations") or r.get("violations") or []
        conf = v.get("confidence") or r.get("confidence")
        summary = v.get("summary") or r.get("summary") or ""
        mark = "OK " if r.get("pass") else "FAIL" if status != "missing_anchor" else "MISS"
        print(f"  {mark}  {sid[:12]}  {name[:40]:40}  conf={conf}  "
              f"issues={len(issues)}  {summary[:70]}")
        for iss in issues[:2]:
            if isinstance(iss, dict):
                print(f"       - {iss.get('rule','?')}: {iss.get('note','')[:80]}")
            else:
                print(f"       - {str(iss)[:90]}")

    return 0 if report['failed'] == 0 else 2

if __name__ == "__main__":
    sys.exit(main())
