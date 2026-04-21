"""Targeted strict re-audit of the 5 user-flagged anchors.
   Identifies WHICH anchors are truly broken at anchor-level vs only at
   Kling-render level. Output is a diagnosis table.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.claude_client import call_opus_vision_json

BAD5 = [
    ("1a", "8b2684fe-1c8", "INTRO over-shoulder — bear back-turned, forehead NOT visible, therefore NO emblem should be visible anywhere"),
    ("2d", "c0a97b17-864", "V2 float petals — bear face-up, emblem on forehead only, eyes visible"),
    ("4a", "7d8fbecb-995", "C1 flying debris — bear mid-tumble, FACE visible at angle, emblem on forehead only, eyes visible"),
    ("4d", "e4c32f51-951", "C2 tracking glitch — bear flies TOWARD camera front-facing, emblem on forehead only, eyes visible"),
    ("5a", "b1a21ab3-7a2", "BR kneel white void — bear kneels head bowed, back of head to camera, forehead NOT visible, NO emblem visible"),
]

PROMPT = """Inspect this single anchor PNG for a chibi anime bear named TB.
Report STRICT JSON:
{
  "bear_orientation": "front"|"three_quarter_front"|"profile"|"three_quarter_back"|"back"|"top_down_bowed",
  "forehead_visible": true|false,
  "emblem_locations_detected": ["forehead"|"temple"|"side_of_head"|"ear"|"crown"|"back_of_head"|"nape"|"cheek"|"shoulder"|"hood"|"chest"|"sky"|"background"|"none"],
  "eyes_visible": "yes"|"closed"|"occluded"|"missing"|"no_face_shown",
  "pose_summary": "one short sentence",
  "emblem_rule_followed": true|false,
  "emblem_rule_followed_reason": "why",
  "violation_summary": "one short sentence of what's wrong, or 'none'"
}
Rules you're checking:
  - Emblem may appear ONLY on the forehead.
  - If the forehead is NOT visible (back/profile/bowed), emblem MUST NOT appear anywhere.
  - Eyes must be clearly visible on the front of the face when face is forward.
  - Report "no_face_shown" for eyes if no front of face is in frame.
Intent for this shot (for context, do NOT use to excuse violations):
"""


def main() -> int:
    results = []
    for shot_id, anchor_dir, intent in BAD5:
        path = os.path.join(ROOT, "output", "pipeline", "anchors_v6", anchor_dir, "selected.png")
        if not os.path.isfile(path):
            print(f"[skip] {shot_id} ({anchor_dir}) missing")
            continue
        prompt = PROMPT + intent
        print(f"[audit] {shot_id} ({anchor_dir}) ...", flush=True)
        try:
            r = call_opus_vision_json(prompt=prompt, image_paths=[path],
                                      attach_bible=False, max_tokens=1024)
        except Exception as e:
            r = {"error": str(e)}
        r["shot_id"] = shot_id
        r["anchor_dir"] = anchor_dir
        r["intent"] = intent
        results.append(r)
        rule_ok = r.get("emblem_rule_followed")
        eyes    = r.get("eyes_visible")
        orient  = r.get("bear_orientation")
        locs    = r.get("emblem_locations_detected")
        print(f"   orient={orient}  eyes={eyes}  emblem_locs={locs}  rule_ok={rule_ok}")
        print(f"   violation: {r.get('violation_summary')}")

    out_path = os.path.join(ROOT, "output", "pipeline", "audits", "bad5_anchor_diagnosis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, indent=2)
    print(f"\n[saved] {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
