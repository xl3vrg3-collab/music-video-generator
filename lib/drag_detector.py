"""Frame-drift detector — flags rendered clips where motion is barely visible.

Extracts frames at 25%/50%/75% of clip duration via ffmpeg and computes
pairwise perceptual-hash similarity. If all pairs read as near-identical the
clip is 'dragging' (frozen or imperceptibly-moving) and should be reshot
before it reaches the stitcher.

The threshold 0.92 was tuned against a set of TB Lifestream Static clips
where the human reviewer had already separated good-motion from drag-motion.
Clips with all 3 pair-similarities >= 0.92 were unanimously drag.

Public API:
    scan_clip(path) -> dict
        Returns {
            "is_drag": bool,
            "max_similarity": float,        # worst (highest) pair, 0..1
            "min_similarity": float,        # best (lowest) pair, 0..1
            "pair_similarities": [float]*3, # [25v50, 50v75, 25v75]
            "duration_sec": float,
            "frames_sampled": int,          # usually 3
            "reason": str,                  # human-readable summary
        }

Integration:
    - `tools/drag_scan.py` — CLI batch scanner, writes JSON report.
    - `POST /api/v6/clips/drag-scan` — batch endpoint in server.py.
    - Surface as per-card "DRAG" badge once endpoint lands.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Optional

try:
    from PIL import Image
    import imagehash
    _AVAILABLE = True
except Exception:
    _AVAILABLE = False

# A phash similarity >= this on EVERY sampled pair = clip is barely moving.
# Tuned against TB v3-v5 clips. 0.92 = ~5/64 bits differ per hash pair.
DRAG_THRESHOLD = 0.92

# Minimum pair similarity to even consider it "drag territory" — if any pair
# is below this, clip definitely has motion and we skip the detailed analysis.
MOTION_FLOOR = 0.75


def _ffprobe_duration(path: str) -> Optional[float]:
    """Return clip duration in seconds, or None if ffprobe fails."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).decode("utf-8", "replace").strip()
        return float(out)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            ValueError, FileNotFoundError):
        return None


def _extract_frame(clip_path: str, at_sec: float, out_path: str) -> bool:
    """Extract a single frame at `at_sec` via ffmpeg. Returns True on success."""
    try:
        subprocess.check_call(
            [
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{at_sec:.2f}",
                "-i", clip_path,
                "-frames:v", "1",
                "-q:v", "3",
                out_path,
            ],
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError):
        return False


def _hash_sim(h1, h2, bits: int = 64) -> float:
    """Normalize hamming distance to [0..1] similarity."""
    return max(0.0, 1.0 - (h1 - h2) / bits)


def scan_clip(clip_path: str) -> dict:
    """Analyze a single clip for drag. See module docstring for shape."""
    result = {
        "is_drag": False,
        "max_similarity": 0.0,
        "min_similarity": 0.0,
        "pair_similarities": [],
        "duration_sec": 0.0,
        "frames_sampled": 0,
        "reason": "",
    }
    if not _AVAILABLE:
        result["reason"] = "imagehash / PIL unavailable — install pillow + imagehash"
        return result
    if not os.path.isfile(clip_path):
        result["reason"] = f"clip not found: {clip_path}"
        return result

    duration = _ffprobe_duration(clip_path)
    if not duration or duration < 1.0:
        result["reason"] = "clip too short or duration unreadable"
        result["duration_sec"] = duration or 0.0
        return result
    result["duration_sec"] = duration

    # Sample at 25 / 50 / 75% — covers intro, middle, and tail motion.
    sample_points = [duration * 0.25, duration * 0.50, duration * 0.75]
    with tempfile.TemporaryDirectory(prefix="drag_") as tmp:
        frames = []
        for idx, t in enumerate(sample_points):
            fp = os.path.join(tmp, f"f{idx}.jpg")
            if _extract_frame(clip_path, t, fp):
                try:
                    frames.append(imagehash.phash(Image.open(fp)))
                except Exception:
                    pass
        result["frames_sampled"] = len(frames)
        if len(frames) < 3:
            result["reason"] = f"only extracted {len(frames)}/3 frames"
            return result

        pairs = [
            _hash_sim(frames[0], frames[1]),
            _hash_sim(frames[1], frames[2]),
            _hash_sim(frames[0], frames[2]),
        ]
        result["pair_similarities"] = [round(p, 4) for p in pairs]
        result["min_similarity"] = round(min(pairs), 4)
        result["max_similarity"] = round(max(pairs), 4)

    # Drag verdict: worst pair must also be above threshold. Any pair below
    # MOTION_FLOOR is plenty of motion regardless of the other two.
    min_sim = result["min_similarity"]
    if min_sim < MOTION_FLOOR:
        result["is_drag"] = False
        result["reason"] = f"motion detected (min pair sim {min_sim:.2f})"
    elif min_sim >= DRAG_THRESHOLD:
        result["is_drag"] = True
        result["reason"] = (
            f"all 3 sampled frames near-identical (min pair sim "
            f"{min_sim:.2f} ≥ {DRAG_THRESHOLD}) — clip likely dragging"
        )
    else:
        result["is_drag"] = False
        result["reason"] = f"subtle motion (min pair sim {min_sim:.2f})"
    return result


def scan_directory(clips_root: str, pattern: str = "selected.mp4") -> list[dict]:
    """Walk a clips root and scan every matching .mp4. Returns list of records."""
    out: list[dict] = []
    for root, _dirs, files in os.walk(clips_root):
        for fn in files:
            if fn == pattern:
                fp = os.path.join(root, fn)
                rec = scan_clip(fp)
                rec["clip_path"] = fp
                rec["shot_dir"] = os.path.basename(root)
                out.append(rec)
    return out
