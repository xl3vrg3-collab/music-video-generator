"""One-command finalizer for the TB Lifestream Static music video.

Workflow:
  1. retry_failed_shots — fills any gaps from the batch render
  2. qa_contact_sheet   — refreshes the QA gallery at output/pipeline/qa/contact_sheet.html
  3. prepare_mv_assembly — stages clips + audio into lumn-stitcher/public/
  4. prints the Remotion render command for the user to run

We deliberately stop before the Remotion render itself. That's a ~15-20 min
ffmpeg-bound step and the user should kick it off manually after visual QA.
"""
from __future__ import annotations
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _run(msg: str, argv: list):
    print("\n" + "=" * 72)
    print("  " + msg)
    print("=" * 72)
    r = subprocess.run([sys.executable] + argv, cwd=ROOT)
    if r.returncode != 0:
        print(f"  !! {argv[0]} exited {r.returncode}")
        sys.exit(r.returncode)


def main():
    _run("STEP 1  retry any failed shots", ["tools/retry_failed_shots.py"])
    _run("STEP 2  refresh QA contact sheet", ["tools/qa_contact_sheet.py"])
    _run("STEP 3  stage clips for Remotion", ["tools/prepare_mv_assembly.py"])

    print("\n" + "=" * 72)
    print("  READY — final render command:")
    print("=" * 72)
    print("\n  cd lumn-stitcher && npx remotion render src/index.tsx "
          "LifestreamStatic out/lifestream_static.mp4\n")
    print("  QA gallery: file:///" +
          os.path.join(ROOT, "output/pipeline/qa/contact_sheet.html").replace("\\", "/"))


if __name__ == "__main__":
    main()
