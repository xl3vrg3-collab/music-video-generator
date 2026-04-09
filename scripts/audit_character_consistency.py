"""
Character Consistency Audit – Golden Retriever "Buddy"
Sends each frame + reference sheet to Claude Haiku vision for structured scoring.
"""

import base64, json, os, sys, time, re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(r"C:\Users\Mathe\lumn\.env"))

import anthropic

CLIENT = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY from env
MODEL  = "claude-haiku-4-5-20251001"

REF_SHEET   = Path(r"C:\Users\Mathe\lumn\output\preproduction\pkg_char_c852b9c5\sheet.png")
FRAMES_DIR  = Path(r"C:\Users\Mathe\lumn\output\audit_frames")
OUTPUT_JSON = Path(r"C:\Users\Mathe\lumn\output\audit_character_consistency.json")

SCORING_PROMPT = """\
You are a VFX character-consistency auditor.

IMAGE 1 is the **official character reference sheet** for a golden retriever named Buddy.
Key traits on the sheet:
- Medium-large golden retriever, warm golden coat
- Red collar with a silver bone-shaped tag
- Floppy ears, friendly face, athletic build

IMAGE 2 is a **frame from the short film**.

Score the frame on each criterion using an integer 1-10 (10 = perfect match).
Return ONLY valid JSON — no markdown fences, no commentary — in this exact schema:

{
  "DOG_SIZE": <int 1-10>,
  "DOG_SIZE_NOTE": "<brief reason>",
  "DOG_APPEARANCE": <int 1-10>,
  "DOG_APPEARANCE_NOTE": "<brief reason>",
  "COLLAR_VISIBLE": <int 1-10>,
  "COLLAR_VISIBLE_NOTE": "<brief reason>",
  "BREED_MATCH": <int 1-10>,
  "BREED_MATCH_NOTE": "<brief reason>",
  "OVERALL_CONSISTENCY": <int 1-10>,
  "OVERALL_CONSISTENCY_NOTE": "<brief reason>"
}

Scoring guidance:
- DOG_SIZE: Is the dog proportioned correctly for the shot type? 10 = natural proportions, 1 = absurdly oversized or tiny.
- DOG_APPEARANCE: Does the dog match the reference sheet (coat color, build, ear shape, face)?
- COLLAR_VISIBLE: Is the red collar with silver tag visible and consistent? Score lower if collar is absent, wrong color, or tag missing. If the shot is too wide or the angle hides the collar naturally, give at least a 5.
- BREED_MATCH: Is it clearly a golden retriever? Score lower for wrong breed traits.
- OVERALL_CONSISTENCY: Does this look like the SAME specific dog from the reference sheet?
"""


def load_image_b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


def score_frame(ref_b64: str, frame_path: Path) -> dict:
    """Send reference + frame to Haiku and return parsed scores."""
    frame_b64 = load_image_b64(frame_path)

    msg = CLIENT.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": ref_b64,
                        },
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": frame_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": SCORING_PROMPT,
                    },
                ],
            }
        ],
    )

    raw = msg.content[0].text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def main():
    # Collect frame paths in sorted order
    frames = sorted(FRAMES_DIR.glob("frame_*.png"))
    if not frames:
        print("ERROR: no frames found in", FRAMES_DIR)
        sys.exit(1)

    print(f"Reference sheet : {REF_SHEET}")
    print(f"Frames to audit : {len(frames)}")
    print(f"Model           : {MODEL}")
    print("-" * 70)

    ref_b64 = load_image_b64(REF_SHEET)

    results = []
    score_keys = ["DOG_SIZE", "DOG_APPEARANCE", "COLLAR_VISIBLE",
                  "BREED_MATCH", "OVERALL_CONSISTENCY"]

    for i, fp in enumerate(frames):
        label = fp.stem
        print(f"[{i+1:2d}/{len(frames)}] Scoring {label} ... ", end="", flush=True)
        try:
            scores = score_frame(ref_b64, fp)
            entry = {"frame": label, "file": fp.name, **scores}
            results.append(entry)
            avg = sum(scores.get(k, 0) for k in score_keys) / len(score_keys)
            print(f"avg={avg:.1f}  "
                  + "  ".join(f"{k[:3]}={scores.get(k,'?')}" for k in score_keys))
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.append({"frame": label, "file": fp.name, "error": str(exc)})
        # Small delay to be kind to rate limits
        if i < len(frames) - 1:
            time.sleep(0.3)

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY TABLE")
    print("=" * 70)
    header = f"{'Frame':<25}" + "".join(f"{k:<12}" for k in score_keys) + "AVG"
    print(header)
    print("-" * len(header))

    problem_shots = []
    for r in results:
        if "error" in r:
            print(f"{r['frame']:<25} ERROR: {r['error']}")
            problem_shots.append({"frame": r["frame"], "reason": "API error"})
            continue

        vals = [r.get(k, 0) for k in score_keys]
        avg = sum(vals) / len(vals)
        line = f"{r['frame']:<25}" + "".join(f"{v:<12}" for v in vals) + f"{avg:.1f}"
        print(line)

        # Flag any individual score below 6
        bad_cats = [k for k in score_keys if r.get(k, 0) < 6]
        if bad_cats:
            problem_shots.append({
                "frame": r["frame"],
                "avg": round(avg, 1),
                "low_scores": {k: r[k] for k in bad_cats},
                "notes": {k: r.get(k + "_NOTE", "") for k in bad_cats},
            })

    print("\n" + "=" * 70)
    print(f"PROBLEM SHOTS (any individual score < 6): {len(problem_shots)}")
    print("=" * 70)
    if problem_shots:
        for ps in problem_shots:
            print(f"  {ps['frame']}")
            if "low_scores" in ps:
                for cat, val in ps["low_scores"].items():
                    note = ps["notes"].get(cat, "")
                    print(f"    {cat}: {val}/10 — {note}")
            elif "reason" in ps:
                print(f"    {ps['reason']}")
    else:
        print("  None — all scores >= 6. Character consistency is solid.")

    # ── Global stats ─────────────────────────────────────────────────────
    valid = [r for r in results if "error" not in r]
    if valid:
        global_avgs = {}
        for k in score_keys:
            vals = [r[k] for r in valid]
            global_avgs[k] = round(sum(vals) / len(vals), 2)
        overall_avg = round(sum(global_avgs.values()) / len(global_avgs), 2)
        print(f"\nGLOBAL AVERAGES:")
        for k, v in global_avgs.items():
            print(f"  {k:<25} {v}")
        print(f"  {'OVERALL':<25} {overall_avg}")

    # ── Save JSON report ─────────────────────────────────────────────────
    report = {
        "model": MODEL,
        "reference_sheet": str(REF_SHEET),
        "total_frames": len(frames),
        "frames_scored": len(valid),
        "global_averages": global_avgs if valid else {},
        "problem_shots": problem_shots,
        "frame_scores": results,
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(report, indent=2))
    print(f"\nFull report saved to: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
