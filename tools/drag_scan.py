"""CLI: scan every rendered clip for drag (frozen motion) and write a report.

Usage:
    python tools/drag_scan.py                     # scan default clips root
    python tools/drag_scan.py --root <dir>        # scan custom dir
    python tools/drag_scan.py --only <shot_id>    # scan one shot

Output:
    output/pipeline/audits/drag_<ts>.json
"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.drag_detector import scan_clip, scan_directory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CLIPS = os.path.join(ROOT, "output", "pipeline", "clips_v6")
AUDITS_DIR    = os.path.join(ROOT, "output", "pipeline", "audits")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_CLIPS, help="clips root dir")
    ap.add_argument("--only", default="", help="shot id to scan alone")
    args = ap.parse_args()

    os.makedirs(AUDITS_DIR, exist_ok=True)
    t0 = time.time()

    if args.only:
        fp = os.path.join(args.root, args.only, "selected.mp4")
        if not os.path.isfile(fp):
            # Try per-user path
            import glob
            hits = sorted(glob.glob(os.path.join(args.root, "u_*", args.only, "selected.mp4")))
            if hits:
                fp = max(hits, key=os.path.getmtime)
        records = [dict(scan_clip(fp), clip_path=fp, shot_dir=args.only)]
    else:
        records = scan_directory(args.root)

    drag_count = sum(1 for r in records if r.get("is_drag"))
    ok_count   = len(records) - drag_count
    elapsed    = time.time() - t0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(AUDITS_DIR, f"drag_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "total":    len(records),
            "drag":     drag_count,
            "ok":       ok_count,
            "elapsed":  round(elapsed, 2),
            "records":  records,
        }, f, indent=2)

    print(f"\n  TOTAL: {len(records)}")
    print(f"  OK:    {ok_count}")
    print(f"  DRAG:  {drag_count}")
    print(f"  report:{out}")
    print(f"  elapsed:{elapsed:.1f}s\n")
    for r in records:
        mark = "DRAG" if r.get("is_drag") else "ok  "
        sid  = r.get("shot_dir", "?")
        sims = r.get("pair_similarities", [])
        sims_str = "/".join(f"{s:.2f}" for s in sims) if sims else "—"
        print(f"  {mark}  {sid[:16]:16}  sims={sims_str}  {r.get('reason','')}")
    return 0 if drag_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
