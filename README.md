# LUMN

AI-powered music video generator using Grok (xAI) for video synthesis, Python for audio analysis, and ffmpeg for stitching.

## Prerequisites

- **Python 3.10+**
- **ffmpeg** on your PATH ([download](https://ffmpeg.org/download))
- **xAI API key** with access to `grok-imagine-video` and `grok-imagine-image`

## Setup

```bash
cd music-video-generator
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your XAI_API_KEY
```

> **Note:** `librosa` is used for audio analysis. If it fails to install, the tool falls back to basic wave/numpy analysis (less accurate beat detection).

## Usage

### CLI

```bash
python generate.py --song track.mp3 --style "cyberpunk city, neon rain, dark mood"
```

Options:
- `--song` - Path to audio file (mp3/wav)
- `--style` - Visual style description
- `--output` - Output path (default: `output/final_video.mp4`)
- `--seed` - Random seed for reproducible scene planning
- `--dry-run` - Analyze and plan without generating

### Web UI

```bash
python server.py
```

Open `http://localhost:3849` in your browser. Upload a song, enter a style prompt, and hit Generate.

## How it works

1. **Audio analysis** - Detects BPM, beats, energy curve, and segments the track into sections (intro/verse/chorus/bridge/outro)
2. **Scene planning** - Generates a video prompt for each ~8-second section, varying camera angles and energy based on the music
3. **Video generation** - Calls the Grok video API for each scene (max 3 concurrent). Falls back to image generation + Ken Burns effect if video fails
4. **Stitching** - Uses ffmpeg to crossfade clips together and overlay the original audio track

## Project Structure

```
generate.py          CLI tool
server.py            Web UI server (port 3849)
public/index.html    Blade Runner-themed web interface
lib/
  audio_analyzer.py  Beat detection, sections, energy
  scene_planner.py   Generate prompts per section
  video_generator.py Grok API integration
  video_stitcher.py  ffmpeg compositing
```
