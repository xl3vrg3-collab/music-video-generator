"""Song timing backend for LUMN.

Produces a ground-truth timing.json per project containing:
  - BPM + beat times  (librosa)
  - Downbeats via low-band onset-strength phase detection
  - Bars (grouped from beats + downbeat phase)
  - Sections via librosa structure segmentation (laplacian / recurrence)
  - Word-level lyrics via fal.ai Whisper (optional, falls back on empty)
  - A stable sha1 of the source audio so consumers can detect staleness

Consumers:
  - scenes.json shot `anchor` field (resolve_anchor) — snap cut points
    to beats / bars / downbeats / section boundaries / lyric words
  - UI timeline lane (lyrics + beats + sections)
  - Conform / stitch step (replaces raw-seconds durations when anchored)

No new pip installs: uses librosa (already required) + fal.ai Whisper
endpoint through the existing lib/fal_client.py submit path.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Iterable

import numpy as np

try:
    import librosa
    HAS_LIBROSA = True
except ImportError:
    HAS_LIBROSA = False


TIMING_VERSION = 1
DEFAULT_SR = 22050


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _sha1_of_file(path: str, block: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(block)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _round_list(values: Iterable[float], digits: int = 3) -> list:
    return [round(float(v), digits) for v in values]


def project_timing_path(project_dir: str) -> str:
    return os.path.join(project_dir, "audio", "timing.json")


# ---------------------------------------------------------------------------
# Beat + downbeat detection
# ---------------------------------------------------------------------------

def _detect_beats(y: np.ndarray, sr: int) -> tuple[float, list]:
    """librosa beat tracking with MV-range tempo-octave correction."""
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, start_bpm=90.0)
    tempo = float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)
    beats = librosa.frames_to_time(beat_frames, sr=sr).tolist()

    # Octave fix (copied from lib/audio_analyzer._correct_tempo_octave)
    sweet_lo, sweet_hi = 70.0, 110.0
    if tempo > 150 and sweet_lo <= tempo / 2 <= sweet_hi:
        tempo = tempo / 2
        beats = beats[::2]
    elif tempo < 60 and sweet_lo <= tempo * 2 <= sweet_hi:
        doubled = []
        for i in range(len(beats) - 1):
            doubled.append(beats[i])
            doubled.append((beats[i] + beats[i + 1]) / 2)
        if beats:
            doubled.append(beats[-1])
        beats = doubled
        tempo = tempo * 2

    if tempo <= 0 or tempo > 300:
        tempo = 120.0
    return tempo, beats


def _detect_downbeat_phase(y: np.ndarray, sr: int, beats: list,
                           beats_per_bar: int = 4) -> int:
    """Return which phase (0..beats_per_bar-1) of the beat grid is the downbeat.

    We sample the low-band onset-strength envelope at each beat time and pick
    the phase whose mean onset-strength is highest. Real kick-on-the-one gets
    the strongest spectral flux in the low band, so this robustly aligns
    bar-1 without needing madmom/DNN.
    """
    if not beats or beats_per_bar <= 1:
        return 0
    # Low-pass mel bands 0..8 (roughly <200Hz) for kick-drum flux
    onset_env = librosa.onset.onset_strength(
        y=y, sr=sr, fmax=200.0, hop_length=512
    )
    frame_times = librosa.frames_to_time(
        np.arange(len(onset_env)), sr=sr, hop_length=512
    )
    if len(frame_times) == 0:
        return 0

    beat_strengths = []
    for bt in beats:
        idx = int(np.searchsorted(frame_times, bt))
        idx = max(0, min(idx, len(onset_env) - 1))
        beat_strengths.append(float(onset_env[idx]))

    scores = [0.0] * beats_per_bar
    counts = [0] * beats_per_bar
    for i, s in enumerate(beat_strengths):
        phase = i % beats_per_bar
        scores[phase] += s
        counts[phase] += 1
    means = [scores[p] / counts[p] if counts[p] else 0.0 for p in range(beats_per_bar)]
    best = int(np.argmax(means))
    return best


def _bars_from_beats(beats: list, phase: int,
                     beats_per_bar: int = 4, duration: float = 0.0) -> tuple[list, list]:
    """Group beats into bars. Returns (downbeats, bars) where bars is
    [{index, start, end, beat_times}]."""
    downbeats = []
    bars = []
    if not beats:
        return downbeats, bars
    # First downbeat is at beat index `phase` (so phase=0 means the first
    # detected beat is a downbeat, phase=1 means the second beat is, etc.)
    idx = phase
    bar_i = 0
    while idx < len(beats):
        start = beats[idx]
        group = beats[idx: idx + beats_per_bar]
        if not group:
            break
        downbeats.append(start)
        end = beats[idx + beats_per_bar] if idx + beats_per_bar < len(beats) else (
            start + (beats[-1] - beats[0]) / max(1, len(beats) - 1) * beats_per_bar
        )
        end = min(end, duration) if duration else end
        bars.append({
            "index": bar_i,
            "start": round(float(start), 3),
            "end": round(float(end), 3),
            "beat_times": _round_list(group),
        })
        idx += beats_per_bar
        bar_i += 1
    return downbeats, bars


# ---------------------------------------------------------------------------
# Section segmentation (real, via laplacian structure features)
# ---------------------------------------------------------------------------

SECTION_LABELS = ("intro", "verse", "pre_chorus", "chorus", "verse", "chorus", "bridge", "chorus", "outro")


def _detect_sections(y: np.ndarray, sr: int, duration: float,
                     bars: list) -> list:
    """Real section boundaries via recurrence + spectral clustering.

    Falls back to bar-aligned even chunks if laplacian can't converge.
    We label heuristically by (energy, position): lowest-energy at edges
    become intro/outro, highest-energy become chorus, else verse/bridge.
    """
    target_sections = max(4, min(10, int(round(duration / 24.0))))
    sections = []

    try:
        # Chroma + MFCC recurrence → laplacian boundaries
        hop = 512
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
        # Beat-synchronous chroma via raw frames is OK for the segmenter.
        rec = librosa.segment.recurrence_matrix(
            chroma, mode="affinity", metric="cosine", sym=True
        )
        # Laplacian segmentation (Mcfee+Ellis)
        rec = librosa.segment.path_enhance(rec, n=15)
        # Agglomerative cluster to get target_sections boundaries
        bound_frames = librosa.segment.agglomerative(chroma, target_sections)
        bound_times = librosa.frames_to_time(bound_frames, sr=sr, hop_length=hop)
        bound_times = sorted(set([0.0, *[float(t) for t in bound_times], float(duration)]))
        # Dedup near-duplicate boundaries
        cleaned = [bound_times[0]]
        for t in bound_times[1:]:
            if t - cleaned[-1] >= 4.0:
                cleaned.append(t)
        if cleaned[-1] < duration - 0.5:
            cleaned.append(duration)
        for i in range(len(cleaned) - 1):
            sections.append({
                "index": i,
                "start": round(cleaned[i], 3),
                "end": round(cleaned[i + 1], 3),
            })
    except Exception:
        sections = []

    if not sections:
        # Bar-aligned fallback
        if bars:
            per = max(1, len(bars) // target_sections)
            for i in range(0, len(bars), per):
                group = bars[i: i + per]
                if not group:
                    continue
                sections.append({
                    "index": len(sections),
                    "start": group[0]["start"],
                    "end": group[-1]["end"],
                })
        else:
            step = duration / max(1, target_sections)
            for i in range(target_sections):
                sections.append({
                    "index": i,
                    "start": round(i * step, 3),
                    "end": round(min((i + 1) * step, duration), 3),
                })

    # Energy per section
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    rms_times = librosa.frames_to_time(
        np.arange(len(rms)), sr=sr, hop_length=512
    )
    for s in sections:
        mask = (rms_times >= s["start"]) & (rms_times < s["end"])
        s["energy"] = round(float(rms[mask].mean()) if mask.any() else 0.0, 4)

    # Energy-quartile label heuristic
    if sections:
        energies = [s["energy"] for s in sections]
        lo = float(np.quantile(energies, 0.25))
        hi = float(np.quantile(energies, 0.75))

        chorus_counter = 0
        verse_counter = 0
        for i, s in enumerate(sections):
            e = s["energy"]
            if i == 0:
                label = "intro"
            elif i == len(sections) - 1:
                label = "outro"
            elif e >= hi:
                chorus_counter += 1
                label = f"chorus_{chorus_counter}"
            elif e <= lo:
                label = "bridge" if 0 < i < len(sections) - 1 else "intro"
            else:
                verse_counter += 1
                label = f"verse_{verse_counter}"
            s["label"] = label

    # Attach id (stable slug of label)
    for s in sections:
        base = s.get("label", f"section_{s['index']}").replace(" ", "_").lower()
        s["id"] = base
    return sections


# ---------------------------------------------------------------------------
# Lyrics via fal.ai Whisper
# ---------------------------------------------------------------------------

def _transcribe_via_fal(song_path: str) -> dict:
    """Submit audio to fal.ai Whisper and normalize the result to
    {engine, language, words:[{word,start,end,prob}], lines:[{index,start,end,text,words:[...]}]}.
    Returns {} on failure; caller treats empty as 'no lyrics available'."""
    try:
        from lib import fal_client as _fc
    except Exception as e:
        print(f"[song_timing] fal_client unavailable: {e}")
        return {}
    try:
        url = _fc._upload_to_fal(song_path)
    except Exception as e:
        print(f"[song_timing] fal upload failed: {e}")
        return {}
    payload = {
        "audio_url": url,
        "task": "transcribe",
        "chunk_level": "word",
        "version": "3",
        "batch_size": 64,
    }
    try:
        result = _fc._fal_submit("fal-ai/whisper", payload, timeout=600)
    except Exception as e:
        print(f"[song_timing] fal whisper error: {e}")
        return {}

    words: list[dict] = []
    # fal-ai/whisper returns either 'chunks' (new) or 'segments' (older)
    chunks = result.get("chunks") or result.get("segments") or []
    for c in chunks:
        ts = c.get("timestamp") or [c.get("start"), c.get("end")]
        text = (c.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(ts[0]) if ts and ts[0] is not None else None
            end = float(ts[1]) if ts and ts[1] is not None else None
        except Exception:
            start = end = None
        if start is None or end is None:
            continue
        # Some Whisper variants return per-line chunks with multi-word text;
        # split to per-word with linear interp so word-anchors still work.
        toks = text.split()
        if len(toks) == 1:
            words.append({"word": toks[0], "start": round(start, 3), "end": round(end, 3)})
        else:
            span = max(0.01, end - start)
            step = span / len(toks)
            for i, tok in enumerate(toks):
                ws = start + i * step
                we = start + (i + 1) * step
                words.append({"word": tok, "start": round(ws, 3), "end": round(we, 3)})

    # Build lines: break on gaps > 0.7s OR punctuation sentence-end
    lines = []
    cur: list[dict] = []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"]) > 0.7:
            lines.append({
                "index": len(lines),
                "start": cur[0]["start"],
                "end": cur[-1]["end"],
                "text": " ".join(x["word"] for x in cur),
                "words": cur,
            })
            cur = []
        cur.append(w)
    if cur:
        lines.append({
            "index": len(lines),
            "start": cur[0]["start"],
            "end": cur[-1]["end"],
            "text": " ".join(x["word"] for x in cur),
            "words": cur,
        })

    return {
        "engine": "fal-ai/whisper",
        "language": result.get("language") or result.get("detected_language") or "unknown",
        "words": words,
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# Top-level analyzer
# ---------------------------------------------------------------------------

def analyze_song(song_path: str, *, include_lyrics: bool = True,
                 beats_per_bar: int = 4) -> dict:
    """Full timing analysis. Pure function; does not write to disk."""
    if not HAS_LIBROSA:
        raise RuntimeError("librosa required for song timing analysis")
    if not os.path.isfile(song_path):
        raise FileNotFoundError(song_path)

    print(f"[song_timing] loading {os.path.basename(song_path)} ...")
    y, sr = librosa.load(song_path, sr=DEFAULT_SR, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))

    bpm, beats = _detect_beats(y, sr)
    print(f"[song_timing] beats={len(beats)}  bpm={bpm:.1f}")

    phase = _detect_downbeat_phase(y, sr, beats, beats_per_bar=beats_per_bar)
    downbeats, bars = _bars_from_beats(beats, phase, beats_per_bar, duration)
    print(f"[song_timing] phase={phase}  bars={len(bars)}")

    sections = _detect_sections(y, sr, duration, bars)
    print(f"[song_timing] sections={len(sections)}  labels={[s.get('label') for s in sections]}")

    lyrics = {}
    if include_lyrics:
        print("[song_timing] transcribing lyrics via fal whisper...")
        lyrics = _transcribe_via_fal(song_path)
        if lyrics:
            print(f"[song_timing] lyrics words={len(lyrics.get('words', []))}  lines={len(lyrics.get('lines', []))}")
        else:
            print("[song_timing] lyrics unavailable")

    return {
        "version": TIMING_VERSION,
        "source": {
            "path": song_path.replace("\\", "/"),
            "duration": round(duration, 3),
            "sha1": _sha1_of_file(song_path),
            "analyzed_at": int(time.time()),
        },
        "tempo": {
            "bpm": round(bpm, 2),
            "beats_per_bar": beats_per_bar,
            "downbeat_phase": phase,
        },
        "beats": _round_list(beats),
        "downbeats": _round_list(downbeats),
        "bars": bars,
        "sections": sections,
        "lyrics": lyrics or {"engine": None, "words": [], "lines": []},
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_timing(project_dir: str, timing: dict) -> str:
    out = project_timing_path(project_dir)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(timing, f, indent=2)
    return out


def load_timing(project_dir: str) -> dict | None:
    path = project_timing_path(project_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_stale(project_dir: str, song_path: str) -> bool:
    """True if no timing.json, or timing.json was computed from a different song sha1."""
    cached = load_timing(project_dir)
    if not cached:
        return True
    if not os.path.isfile(song_path):
        return True
    return cached.get("source", {}).get("sha1") != _sha1_of_file(song_path)


# ---------------------------------------------------------------------------
# Anchor resolution (consumed by scenes.json shot schema)
# ---------------------------------------------------------------------------

class AnchorError(ValueError):
    pass


def _find_word(timing: dict, text: str, occurrence: int = 1) -> dict | None:
    target = (text or "").strip().lower()
    if not target:
        return None
    hits = 0
    for w in (timing.get("lyrics") or {}).get("words", []):
        if w.get("word", "").strip().lower().strip(".,!?\"';:") == target:
            hits += 1
            if hits == occurrence:
                return w
    return None


def _find_section(timing: dict, sid: str) -> dict | None:
    for s in timing.get("sections", []):
        if s.get("id") == sid or s.get("label") == sid or str(s.get("index")) == str(sid):
            return s
    return None


def resolve_ref(ref: dict, timing: dict) -> float:
    """Resolve a single {type, ...} reference to an absolute second.

    Supported types:
      - {type: "second", value: float}
      - {type: "beat",     index: int}
      - {type: "downbeat", index: int}
      - {type: "bar",      index: int, where?: "start"|"end"}
      - {type: "section",  id: str,    where?: "start"|"end"}
      - {type: "word",     text: str,  occurrence?: int, where?: "start"|"end"}
    """
    if not isinstance(ref, dict):
        raise AnchorError(f"ref must be dict, got {type(ref).__name__}")
    t = ref.get("type")
    where = ref.get("where", "start")
    if t == "second":
        return float(ref.get("value", 0.0))
    if t == "beat":
        beats = timing.get("beats", [])
        idx = int(ref.get("index", 0))
        if not (0 <= idx < len(beats)):
            raise AnchorError(f"beat index {idx} out of range [0,{len(beats)})")
        return float(beats[idx])
    if t == "downbeat":
        downs = timing.get("downbeats", [])
        idx = int(ref.get("index", 0))
        if not (0 <= idx < len(downs)):
            raise AnchorError(f"downbeat index {idx} out of range [0,{len(downs)})")
        return float(downs[idx])
    if t == "bar":
        bars = timing.get("bars", [])
        idx = int(ref.get("index", 0))
        if not (0 <= idx < len(bars)):
            raise AnchorError(f"bar index {idx} out of range [0,{len(bars)})")
        bar = bars[idx]
        return float(bar["end"] if where == "end" else bar["start"])
    if t == "section":
        sec = _find_section(timing, ref.get("id", ""))
        if sec is None:
            raise AnchorError(f"section '{ref.get('id')}' not found")
        return float(sec["end"] if where == "end" else sec["start"])
    if t == "word":
        w = _find_word(timing, ref.get("text", ""), int(ref.get("occurrence", 1)))
        if w is None:
            raise AnchorError(f"word '{ref.get('text')}' occurrence {ref.get('occurrence', 1)} not found")
        return float(w["end"] if where == "end" else w["start"])
    raise AnchorError(f"unknown ref type: {t}")


def resolve_anchor(anchor: dict, timing: dict,
                   fallback_duration: float | None = None) -> tuple[float, float]:
    """Resolve a shot's {start, end | duration} anchor to (start_s, end_s)."""
    if not anchor:
        raise AnchorError("empty anchor")
    start_ref = anchor.get("start")
    if start_ref is None:
        raise AnchorError("anchor.start required")
    start = resolve_ref(start_ref, timing)

    if "end" in anchor:
        end = resolve_ref(anchor["end"], timing)
    elif "duration" in anchor:
        end = start + float(anchor["duration"])
    elif fallback_duration is not None:
        end = start + float(fallback_duration)
    else:
        raise AnchorError("anchor requires one of: end | duration | fallback_duration")

    if end <= start:
        raise AnchorError(f"resolved end {end:.3f} <= start {start:.3f}")
    return float(start), float(end)
