"""
Audio analysis module.
Uses librosa for beat detection, energy analysis, and section segmentation.
Falls back to basic wave/numpy analysis if librosa is not available.
"""

import os
import struct
import wave

import numpy as np

try:
    import librosa

    HAS_LIBROSA = True
except ImportError:
    HAS_LIBROSA = False


def analyze(song_path: str) -> dict:
    """
    Analyze an audio file and return timing/energy information.

    Returns dict with keys:
        bpm       - estimated tempo
        beats     - list of beat timestamps in seconds
        sections  - list of {start, end, type, energy}
        duration  - total duration in seconds
    """
    if not os.path.isfile(song_path):
        raise FileNotFoundError(f"Song not found: {song_path}")

    if HAS_LIBROSA:
        return _analyze_librosa(song_path)
    else:
        return _analyze_basic(song_path)


# ---------- librosa path ----------

def _analyze_librosa(path: str) -> dict:
    y, sr = librosa.load(path, sr=22050, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))

    # Tempo and beats
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    if hasattr(tempo, "__len__"):
        tempo = float(tempo[0])
    else:
        tempo = float(tempo)
    beats = librosa.frames_to_time(beat_frames, sr=sr).tolist()

    # RMS energy curve (one value per frame)
    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)

    # Build sections from energy
    sections = _build_sections(frame_times, rms, duration)

    return {
        "bpm": round(tempo, 1),
        "beats": [round(b, 3) for b in beats],
        "sections": sections,
        "duration": round(duration, 3),
    }


# ---------- basic fallback path (wave + numpy) ----------

def _analyze_basic(path: str) -> dict:
    """Fallback analysis using wave module and numpy (no librosa)."""
    # Attempt to read as WAV; for mp3 files this will fail and we estimate.
    try:
        wf = wave.open(path, "rb")
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        wf.close()

        if sample_width == 2:
            fmt = f"<{n_frames * n_channels}h"
            samples = np.array(struct.unpack(fmt, raw), dtype=np.float32)
        else:
            samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128

        if n_channels > 1:
            samples = samples.reshape(-1, n_channels).mean(axis=1)

        duration = len(samples) / sr
    except Exception:
        # Cannot read file natively; estimate from file size for mp3
        file_size = os.path.getsize(path)
        # rough mp3 estimate: 128kbps
        duration = file_size / (128_000 / 8)
        sr = 22050
        samples = np.zeros(int(duration * sr))

    # Very rough tempo estimation via zero-crossing rate peaks
    bpm = 120.0  # default guess
    beats = []
    if len(samples) > sr:
        hop = 512
        n_hops = len(samples) // hop
        energy = np.array([
            np.sqrt(np.mean(samples[i * hop:(i + 1) * hop] ** 2))
            for i in range(n_hops)
        ])
        # Normalize
        if energy.max() > 0:
            energy = energy / energy.max()
        frame_times = np.arange(n_hops) * hop / sr

        # Simple onset detection: energy rises
        threshold = 0.3
        peaks = []
        for i in range(1, len(energy)):
            if energy[i] > threshold and energy[i] > energy[i - 1]:
                peaks.append(frame_times[i])
        # Estimate BPM from median inter-onset interval
        if len(peaks) > 2:
            intervals = np.diff(peaks)
            median_ioi = float(np.median(intervals))
            if median_ioi > 0:
                bpm = round(60.0 / median_ioi, 1)
                # Constrain to reasonable range
                while bpm > 200:
                    bpm /= 2
                while bpm < 60:
                    bpm *= 2
            beats = [round(p, 3) for p in peaks]
    else:
        hop = 512
        energy = np.array([0.5])
        frame_times = np.array([0.0])

    sections = _build_sections(frame_times, energy, duration)

    return {
        "bpm": round(bpm, 1),
        "beats": beats,
        "sections": sections,
        "duration": round(duration, 3),
    }


# ---------- section building (shared) ----------

SECTION_TYPES = ["intro", "verse", "chorus", "verse", "chorus", "bridge", "chorus", "outro"]


def _build_sections(frame_times: np.ndarray, rms: np.ndarray, duration: float) -> list:
    """
    Split the track into sections based on energy contour.
    Returns list of {start, end, type, energy}.
    """
    if duration <= 0 or len(rms) == 0:
        return [{"start": 0, "end": duration, "type": "verse", "energy": 0.5}]

    # Target section duration based on total length
    # Short clips (<60s): fewer, longer sections
    # Full songs (>120s): more sections at ~8s each
    if duration <= 30:
        target_section_dur = max(5, duration / 3)  # 3 sections max for <30s
    elif duration <= 60:
        target_section_dur = max(6, duration / 5)  # 5 sections max for <60s
    elif duration <= 120:
        target_section_dur = 8.0  # ~15 sections for 2 min
    else:
        target_section_dur = 8.0  # ~22-25 sections for 3 min

    n_sections = max(1, min(30, int(round(duration / target_section_dur))))  # cap at 30 scenes max

    section_dur = duration / n_sections
    sections = []

    for i in range(n_sections):
        start = i * section_dur
        end = min((i + 1) * section_dur, duration)

        # Average energy for this window
        mask = (frame_times >= start) & (frame_times < end)
        if mask.any():
            avg_energy = float(np.mean(rms[mask]))
        else:
            avg_energy = 0.5

        # Assign section type in a cyclic pattern
        stype = SECTION_TYPES[i % len(SECTION_TYPES)]
        # Override first/last
        if i == 0:
            stype = "intro"
        elif i == n_sections - 1:
            stype = "outro"

        sections.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "type": stype,
            "energy": round(avg_energy, 3),
        })

    return sections


# ---- Audio Normalization (Roadmap Item 23) ----

def normalize_audio(input_path: str, output_path: str, target_db: float = -14.0) -> str:
    """Normalize audio to target loudness level using ffmpeg loudnorm."""
    import subprocess
    kw = {}
    if __import__("sys").platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-af", f"loudnorm=I={target_db}:TP=-1.5:LRA=11",
           "-c:a", "aac", "-b:a", "192k", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **kw)
    return output_path


# ---- Noise Reduction (Roadmap Item 24) ----

def reduce_noise(input_path: str, output_path: str, noise_floor: float = -30) -> str:
    """Basic noise reduction using ffmpeg afftdn filter."""
    import subprocess
    kw = {}
    if __import__("sys").platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-af", f"afftdn=nf={noise_floor}",
           "-c:a", "aac", "-b:a", "192k", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **kw)
    return output_path


# ---- Tempo Adjustment (Roadmap Item 27) ----

def adjust_tempo(input_path: str, output_path: str, factor: float = 1.0) -> str:
    """Adjust audio tempo without changing pitch.
    factor: 0.5 = half speed, 1.0 = normal, 2.0 = double speed
    """
    import subprocess
    kw = {}
    if __import__("sys").platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-af", f"atempo={factor}",
           "-c:a", "aac", "-b:a", "192k", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **kw)
    return output_path


# ---- Pitch Shift (Roadmap Item 28) ----

def pitch_shift(input_path: str, output_path: str, semitones: float = 0) -> str:
    """Shift pitch by semitones without changing tempo."""
    import subprocess, math
    kw = {}
    if __import__("sys").platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    # Pitch shift = adjust sample rate ratio
    ratio = 2 ** (semitones / 12.0)
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-af", f"asetrate=44100*{ratio},atempo={1/ratio}",
           "-c:a", "aac", "-b:a", "192k", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **kw)
    return output_path


# ---- Audio Effects (Roadmap Item 29) ----

def apply_audio_effect(input_path: str, output_path: str, effect: str = "reverb") -> str:
    """Apply audio effect: reverb, echo, delay."""
    import subprocess
    kw = {}
    if __import__("sys").platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    
    effect_filters = {
        "reverb": "aecho=0.8:0.88:60:0.4",
        "echo": "aecho=0.8:0.9:200:0.3",
        "delay": "adelay=500|500",
        "bass_boost": "bass=g=10:f=110:w=0.6",
        "treble_boost": "treble=g=8:f=3000:w=0.6",
    }
    filt = effect_filters.get(effect, effect_filters["reverb"])
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-af", filt,
           "-c:a", "aac", "-b:a", "192k", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **kw)
    return output_path
