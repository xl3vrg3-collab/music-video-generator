"""
Perceptual similarity gate — cheap first-line identity check.

Uses imagehash (phash + dhash) plus PIL color histograms to compute similarity
between a candidate image and reference images (character sheet, anchor, prior
frames). No torch/CLIP required; runs in milliseconds.

Strategy:
  - pHash catches structural drift (different shape, wrong pose, new subject)
  - dHash catches gradient/edge drift (style bleed, rendering swap)
  - Color histogram catches palette drift (wrong costume, wrong lighting)
  - Return a composite score in [0..1]; >0.78 "likely same", <0.58 "drift",
    otherwise escalate to Opus vision.

This is NOT a replacement for the Opus identity gate. It's a fast filter that
lets us skip the Opus call for obvious passes AND hard fails, spending the API
budget on the borderline cases where it actually matters.
"""
from __future__ import annotations

import pathlib
from typing import Optional

try:
    from PIL import Image
    import imagehash
    _AVAILABLE = True
except Exception:
    _AVAILABLE = False


PHASH_WEIGHT = 0.45
DHASH_WEIGHT = 0.25
COLOR_WEIGHT = 0.30

# Thresholds tuned against TB anchor batch (2026-04-19)
PASS_THRESHOLD = 0.78   # >= → perceptual pass, skip Opus
FAIL_THRESHOLD = 0.58   # <  → perceptual hard-fail, flag for regen


def _hash_sim(h1, h2, bits: int = 64) -> float:
    """Normalize hamming distance to a [0..1] similarity."""
    dist = h1 - h2  # imagehash supports subtraction → hamming distance
    return max(0.0, 1.0 - dist / bits)


def _color_hist_sim(img1: "Image.Image", img2: "Image.Image") -> float:
    """Coarse-binned RGB histogram cosine similarity, thumbnailed for speed."""
    import math
    t1 = img1.convert("RGB").resize((64, 64))
    t2 = img2.convert("RGB").resize((64, 64))

    def _hist(img):
        # 4-bin per channel = 64-dim descriptor, cheap and robust
        h = [0] * 64
        for r, g, b in img.getdata():
            idx = (r // 64) * 16 + (g // 64) * 4 + (b // 64)
            h[idx] += 1
        s = sum(h) or 1
        return [v / s for v in h]

    v1 = _hist(t1)
    v2 = _hist(t2)
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1)) or 1e-9
    n2 = math.sqrt(sum(b * b for b in v2)) or 1e-9
    return max(0.0, min(1.0, dot / (n1 * n2)))


def compare(candidate_path: str | pathlib.Path,
            reference_path: str | pathlib.Path) -> dict:
    """Return similarity breakdown for candidate vs. one reference."""
    if not _AVAILABLE:
        return {"skipped": True, "reason": "imagehash/PIL not installed"}

    c = Image.open(str(candidate_path))
    r = Image.open(str(reference_path))

    ph_c = imagehash.phash(c, hash_size=8)
    ph_r = imagehash.phash(r, hash_size=8)
    dh_c = imagehash.dhash(c, hash_size=8)
    dh_r = imagehash.dhash(r, hash_size=8)

    phash_sim = _hash_sim(ph_c, ph_r, bits=64)
    dhash_sim = _hash_sim(dh_c, dh_r, bits=64)
    color_sim = _color_hist_sim(c, r)

    composite = (
        PHASH_WEIGHT * phash_sim +
        DHASH_WEIGHT * dhash_sim +
        COLOR_WEIGHT * color_sim
    )

    if composite >= PASS_THRESHOLD:
        verdict = "PASS"
    elif composite < FAIL_THRESHOLD:
        verdict = "FAIL"
    else:
        verdict = "ESCALATE"

    return {
        "skipped": False,
        "composite": round(composite, 4),
        "phash_sim": round(phash_sim, 4),
        "dhash_sim": round(dhash_sim, 4),
        "color_sim": round(color_sim, 4),
        "verdict": verdict,
        "candidate": str(candidate_path),
        "reference": str(reference_path),
    }


def compare_multi(candidate_path: str | pathlib.Path,
                  reference_paths: list[str | pathlib.Path],
                  aggregate: str = "max") -> dict:
    """
    Compare candidate against multiple references. Aggregate via `max`
    (best-match reference wins — good when refs are equally valid poses)
    or `mean` (average — good for conservative drift-sensitive checks).
    """
    if not _AVAILABLE:
        return {"skipped": True, "reason": "imagehash/PIL not installed"}
    if not reference_paths:
        return {"skipped": True, "reason": "no references"}

    per_ref = [compare(candidate_path, r) for r in reference_paths]
    scores = [p["composite"] for p in per_ref if not p.get("skipped")]
    if not scores:
        return {"skipped": True, "reason": "all references failed to load"}

    if aggregate == "mean":
        agg = sum(scores) / len(scores)
    else:
        agg = max(scores)

    if agg >= PASS_THRESHOLD:
        verdict = "PASS"
    elif agg < FAIL_THRESHOLD:
        verdict = "FAIL"
    else:
        verdict = "ESCALATE"

    return {
        "skipped": False,
        "aggregate": aggregate,
        "composite": round(agg, 4),
        "verdict": verdict,
        "per_reference": per_ref,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python perceptual_gate.py <candidate.png> <reference.png> [<reference2.png> ...]")
        sys.exit(1)
    cand = sys.argv[1]
    refs = sys.argv[2:]
    if len(refs) == 1:
        print(compare(cand, refs[0]))
    else:
        print(compare_multi(cand, refs))
