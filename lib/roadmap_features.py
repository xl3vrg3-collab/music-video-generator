"""
Roadmap features module — contains implementations for remaining roadmap items.
These are utility functions called by server.py endpoints.
"""
import os
import json
import subprocess
import sys
import time
import hashlib
from pathlib import Path


def _subprocess_kwargs():
    kw = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    return kw


# ---- #1 Better Prompt Engineering ----

CONTEXT_MODIFIERS = {
    "opening": "establishing the scene, wide angle, first impression",
    "building": "increasing intensity, tighter framing, forward motion",
    "climax": "peak energy, extreme close-ups mixed with wide shots, rapid movement",
    "resolving": "calming down, pulling back, softer lighting",
    "closing": "final moments, fading, contemplative wide shot",
}

def enhance_prompt_with_context(prompt, scene_index, total_scenes, prev_prompt="", energy=0.5):
    position = scene_index / max(total_scenes - 1, 1)
    if position < 0.1: context = CONTEXT_MODIFIERS["opening"]
    elif position < 0.4: context = CONTEXT_MODIFIERS["building"]
    elif position < 0.7: context = CONTEXT_MODIFIERS["climax"]
    elif position < 0.9: context = CONTEXT_MODIFIERS["resolving"]
    else: context = CONTEXT_MODIFIERS["closing"]
    continuity = ""
    if prev_prompt:
        key_words = [w for w in prev_prompt.split() if len(w) > 4][:3]
        if key_words:
            continuity = f"continuing from {', '.join(key_words)}, "
    energy_mod = "high energy, dynamic, " if energy > 0.7 else ("calm, gentle, " if energy < 0.3 else "")
    return f"{continuity}{energy_mod}{prompt}, {context}"


# ---- #5 Auto Transitions from Energy ----

def auto_transitions_from_energy(scenes):
    for i in range(1, len(scenes)):
        prev_energy = scenes[i-1].get("energy", 0.5)
        curr_energy = scenes[i].get("energy", 0.5)
        delta = curr_energy - prev_energy
        if delta > 0.3: scenes[i]["transition"] = "hard_cut"
        elif delta < -0.3: scenes[i]["transition"] = "fade_black"
        elif abs(delta) < 0.1: scenes[i]["transition"] = "crossfade"
        elif delta > 0: scenes[i]["transition"] = "zoom_in"
        else: scenes[i]["transition"] = "dissolve"
    return scenes


# ---- #7 AI Upscale ----

def upscale_video(input_path, output_path, scale=2):
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-vf", f"scale=iw*{scale}:ih*{scale}:flags=lanczos",
           "-c:v", "libx264", "-preset", "slow", "-crf", "18",
           "-c:a", "copy", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    return output_path


# ---- #12 Frame-by-Frame: extract frames ----

def extract_frames(video_path, output_dir, fps=1):
    os.makedirs(output_dir, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", video_path,
           "-vf", f"fps={fps}", os.path.join(output_dir, "frame_%04d.jpg")]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    frames = sorted(Path(output_dir).glob("frame_*.jpg"))
    return [str(f) for f in frames]


# ---- #21 BPM Detection Improvement ----

def detect_bpm_multi(audio_path):
    bpms = []
    try:
        import librosa
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        tempo1, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpms.append(float(tempo1[0]) if hasattr(tempo1, '__len__') else float(tempo1))
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        tempo2 = librosa.feature.tempo(onset_envelope=onset_env, sr=sr)
        bpms.append(float(tempo2[0]) if hasattr(tempo2, '__len__') else float(tempo2))
    except Exception as e:
        print(f"[bpm_multi] {e}")
    return round(sorted(bpms)[len(bpms)//2], 1) if bpms else 120.0


# ---- #22 Key Detection ----

def detect_key(audio_path):
    try:
        import librosa, numpy as np
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_avg = np.mean(chroma, axis=1)
        keys = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
        key_idx = int(np.argmax(chroma_avg))
        major_profile = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
        minor_profile = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
        major_corr = np.corrcoef(chroma_avg, np.roll(major_profile, key_idx))[0,1]
        minor_corr = np.corrcoef(chroma_avg, np.roll(minor_profile, key_idx))[0,1]
        return f"{keys[key_idx]} {'major' if major_corr > minor_corr else 'minor'}"
    except Exception as e:
        return "Unknown"


# ---- #25 Auto-Mix Master ----

def auto_mix_master(input_path, output_path):
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,acompressor=threshold=-20dB:ratio=4:attack=5:release=50,equalizer=f=80:t=h:width=200:g=2,equalizer=f=10000:t=h:width=2000:g=1",
           "-c:a", "aac", "-b:a", "256k", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    return output_path


# ---- #30 Click Track ----

def generate_click_track(bpm, duration, output_path):
    interval_ms = int(60000 / bpm)
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
           f"sine=frequency=1000:duration=0.03",
           "-af", f"apad=whole_dur={duration},aecho=0.6:0.3:{interval_ms}:0.5",
           "-t", str(duration), "-c:a", "aac", output_path]
    subprocess.run(cmd, check=True, capture_output=True, **_subprocess_kwargs())
    return output_path


# ---- #32 QR Code Generation ----

def generate_qr_code(url, output_path):
    try:
        import qrcode
        qr = qrcode.make(url)
        qr.save(output_path)
        return output_path
    except ImportError:
        # Fallback: generate via API
        import requests
        resp = requests.get(f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={url}", timeout=10)
        if resp.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(resp.content)
            return output_path
        raise RuntimeError("QR code generation failed — install qrcode package or check internet")


# ---- #33 Embed Code ----

def generate_embed_code(video_url, width=640, height=360):
    return f'<video width="{width}" height="{height}" controls><source src="{video_url}" type="video/mp4">Your browser does not support the video tag.</video>'


# ---- #36 Version History ----

def save_version(output_dir, video_path, metadata=None):
    versions_dir = os.path.join(output_dir, "versions")
    os.makedirs(versions_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    version_name = f"v_{ts}.mp4"
    version_path = os.path.join(versions_dir, version_name)
    import shutil
    shutil.copy2(video_path, version_path)
    # Save metadata
    meta_path = os.path.join(versions_dir, f"v_{ts}.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"timestamp": ts, "source": video_path, "metadata": metadata or {}}, f, indent=2)
    return version_path


def list_versions(output_dir):
    versions_dir = os.path.join(output_dir, "versions")
    if not os.path.isdir(versions_dir):
        return []
    versions = []
    for f in sorted(Path(versions_dir).glob("v_*.mp4"), reverse=True):
        meta_path = f.with_suffix(".json")
        meta = {}
        if meta_path.exists():
            try: meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except: pass
        versions.append({"file": f.name, "path": str(f), "size": f.stat().st_size, **meta})
    return versions


# ---- #37 Batch Render Queue ----

_render_queue = []
_render_queue_running = False

def add_to_render_queue(job):
    _render_queue.append(job)
    return len(_render_queue)

def get_render_queue():
    return [{"index": i, **j} for i, j in enumerate(_render_queue)]


# ---- #41 Keyboard Shortcuts ----

KEYBOARD_SHORTCUTS = {
    "Ctrl+Z": "Undo",
    "Ctrl+Y": "Redo",
    "Ctrl+S": "Save project",
    "Ctrl+Shift+S": "Save full project (with clips)",
    "Space": "Play/pause preview",
    "1": "Switch to Auto mode",
    "2": "Switch to Manual mode",
    "N": "Add new scene",
    "Delete": "Delete selected scene",
    "Ctrl+G": "Generate selected scene",
    "Ctrl+Shift+G": "Generate all scenes",
    "Ctrl+E": "Export/stitch video",
    "?": "Show keyboard shortcuts",
}


# ---- #45 Analytics ----

def get_analytics(output_dir):
    history_path = os.path.join(output_dir, "prompt_history.json")
    cost_path = os.path.join(output_dir, "cost_tracker.json")
    analytics = {
        "total_prompts": 0,
        "favorite_prompts": 0,
        "total_cost_usd": 0,
        "video_generations": 0,
        "image_generations": 0,
        "most_used_style": "unknown",
    }
    if os.path.isfile(history_path):
        try:
            data = json.loads(open(history_path, encoding="utf-8").read())
            analytics["total_prompts"] = len(data.get("prompts", []))
            analytics["favorite_prompts"] = len(data.get("favorites", []))
        except: pass
    if os.path.isfile(cost_path):
        try:
            data = json.loads(open(cost_path, encoding="utf-8").read())
            analytics["total_cost_usd"] = data.get("total_usd", 0)
            analytics["video_generations"] = data.get("video_count", 0)
            analytics["image_generations"] = data.get("image_count", 0)
        except: pass
    return analytics


# ---- #49 Export Storyboard as PDF ----

def export_storyboard_pdf(scenes, output_path):
    """Generate a storyboard as PDF-like HTML that can be printed."""
    html = """<!DOCTYPE html><html><head><style>
    body{font-family:monospace;background:#111;color:#ccc;padding:20px}
    .scene{display:inline-block;width:45%;margin:10px;padding:15px;border:1px solid #ff6a00;background:#1a1a2e}
    .scene h3{color:#ff6a00;margin:0 0 8px 0}
    .scene p{font-size:12px;margin:4px 0}
    .meta{color:#666;font-size:10px}
    @media print{body{background:white;color:black}.scene{border-color:#333}}
    </style></head><body><h1 style="color:#ff6a00">STORYBOARD</h1>"""
    for i, s in enumerate(scenes):
        html += f"""<div class="scene">
        <h3>Scene {i+1}</h3>
        <p><b>Prompt:</b> {s.get('prompt','')[:100]}</p>
        <p class="meta">Duration: {s.get('duration',8)}s | Transition: {s.get('transition','crossfade')}</p>
        <p class="meta">Engine: {s.get('engine','default')} | Section: {s.get('section_type','')}</p>
        </div>"""
    html += "</body></html>"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path
