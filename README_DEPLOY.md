# LUMN Studio — AI Film Production App

## Quick Start (Local)

```bash
# 1. Clone and enter directory
cd C:\Users\Mathe\lumn

# 2. Copy environment file and add your API keys
copy env.example .env
# Edit .env with your RUNWAY_API_KEY (required)

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Start server
python -B server.py

# 5. Open browser
# http://localhost:3849
```

## Deploy to Railway

```bash
# 1. Install Railway CLI
npm install -g @railway/cli

# 2. Login
railway login

# 3. Create project
railway init

# 4. Set environment variables
railway variables set RUNWAY_API_KEY=your_key
railway variables set OPENAI_API_KEY=your_key  # Optional
railway variables set ANTHROPIC_API_KEY=your_key  # Optional

# 5. Deploy
railway up
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `RUNWAY_API_KEY` | Yes | Runway ML API key for video/image generation |
| `OPENAI_API_KEY` | No | OpenAI key for AI auto-fill and transcription |
| `ANTHROPIC_API_KEY` | No | Anthropic key for AI auto-fill (fallback) |
| `ELEVENLABS_API_KEY` | No | ElevenLabs key for voice cloning |
| `SUNO_API_KEY` | No | Suno key for music generation |
| `XAI_API_KEY` | No | xAI Grok key for video generation |
| `PORT` | No | Server port (default: 3849, Railway auto-sets) |

## Features

### Generation Pipeline
- **6-workspace flow**: Project → Assets → Shots → Edit → Audio → Output
- **Multi-model support**: Runway Gen4.5, Veo 3.1, Grok, Luma Ray2
- **Smart routing**: AI recommends best model per scene
- **Character sheets**: Multi-angle reference with approval workflow
- **Seed control**: Reproducible generation with seed parameter

### Editing
- **Canvas timeline**: Drag to reorder, trim edges, zoom/scrub
- **16 transitions**: Fade, dissolve, wipe, zoom, blur + CSS preview
- **Beat sync**: Auto-align cuts to music beats
- **Multi-track audio**: Music + voice + SFX with per-track volume
- **Text overlays**: Captions, titles, lower thirds with timing

### AI Features
- **Director Brain**: Learns your style from ratings, recommends settings
- **AutoAgent**: Self-improving generation quality via eval loops
- **AI Auto-Fill**: Describe an idea, AI fills all form fields
- **Auto-Captions**: Whisper transcription → styled caption overlay
- **Suno Integration**: Import or generate music

### Quality
- **2K first frames**: 2560×1440 image generation
- **Real-ESRGAN upscale**: AI 4K upscaling post-generation
- **4K export**: Ultra HD output with lanczos upscaling
- **64 reference photos**: Visual lookbook for every creative option

## Architecture

```
public/
  index.html      — 21,800+ line single-page app (UI)
  timeline.js     — Canvas-based video timeline editor
lib/
  video_generator.py  — Multi-engine video generation
  prompt_os.py        — Character/costume/environment data model
  prompt_templates.py — Centralized prompt builder system
  director_brain.py   — Creative DNA learning system
  auto_agent.py       — Self-improving generation optimizer
  upscaler.py         — Real-ESRGAN AI upscaling
  audio_analyzer.py   — BPM/beat/section detection
  scene_planner.py    — AI scene planning from audio
  video_stitcher.py   — FFmpeg-based clip assembly
server.py           — HTTP server with all API endpoints
```

## AutoAgent (Self-Improving System)

```bash
# Run the collector to evaluate generation quality
cd C:\Users\Mathe
python autoagent/run.py collect lumn --rounds 3

# Check results
# Then tell Claude Code: "check autoagent results for lumn"
```
