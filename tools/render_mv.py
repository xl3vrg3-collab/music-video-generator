"""F6 proxy-first render wrapper for Remotion.

Usage:
    python tools/render_mv.py                 # full 1928x1072 crf 17
    python tools/render_mv.py --proxy         # ~960x540 crf 28, ~5x faster
    python tools/render_mv.py --proxy --out my_proxy.mp4
    python tools/render_mv.py --composition LifestreamStatic --out custom.mp4

Proxy mode is the review loop: cuts feel the same (same fps, same
downbeat alignment), resolution/bitrate drop, render-time drops from ~30min
to ~5min on the TB MV. Approve on proxy, then re-run without --proxy for
spec delivery.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

STITCHER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "lumn-stitcher",
)


def render(
    composition: str,
    out_path: str,
    *,
    proxy: bool = False,
    concurrency: int | None = None,
    props_json: str | None = None,
) -> int:
    entry = os.path.join("src", "index.tsx")
    cmd = ["npx", "remotion", "render", entry, composition, out_path]

    if proxy:
        cmd += [
            "--scale=0.5",
            "--crf=28",
            "--jpeg-quality=70",
        ]
    else:
        cmd += [
            "--crf=17",
            "--jpeg-quality=95",
        ]

    if concurrency:
        cmd.append(f"--concurrency={concurrency}")
    if props_json:
        cmd.append(f"--props={props_json}")

    print(f"[render_mv] mode={'proxy' if proxy else 'full'} -> {out_path}", flush=True)
    print(f"[render_mv] cwd={STITCHER_DIR}", flush=True)
    print(f"[render_mv] cmd={' '.join(cmd)}", flush=True)

    t0 = time.time()
    rc = subprocess.call(cmd, cwd=STITCHER_DIR, shell=(os.name == "nt"))
    dt = time.time() - t0
    print(f"[render_mv] exit={rc} elapsed={dt:.1f}s", flush=True)
    if rc == 0:
        try:
            abs_out = os.path.join(STITCHER_DIR, out_path) if not os.path.isabs(out_path) else out_path
            size_mb = os.path.getsize(abs_out) / (1024 * 1024)
            print(f"[render_mv] output={abs_out} size={size_mb:.1f}MB", flush=True)
        except OSError:
            pass
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Remotion render wrapper with proxy mode (F6).")
    ap.add_argument("--composition", default="LifestreamStatic",
                    help="Remotion composition id (default: LifestreamStatic)")
    ap.add_argument("--out", default=None,
                    help="Output path relative to lumn-stitcher/ (default: out/<comp>[_proxy].mp4)")
    ap.add_argument("--proxy", action="store_true",
                    help="Proxy mode: --scale=0.5 + crf 28. ~5x faster, for review.")
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--props", default=None,
                    help="Path to JSON file with composition props override.")
    args = ap.parse_args()

    out_path = args.out
    if not out_path:
        suffix = "_proxy" if args.proxy else ""
        out_path = os.path.join("out", f"{args.composition}{suffix}.mp4")

    return render(
        composition=args.composition,
        out_path=out_path,
        proxy=args.proxy,
        concurrency=args.concurrency,
        props_json=args.props,
    )


if __name__ == "__main__":
    sys.exit(main())
