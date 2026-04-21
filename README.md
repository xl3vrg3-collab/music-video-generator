# LUMN Studio

AI film studio for short films, trailers, and cinematic pieces. V6 pipeline runs on **fal.ai** (Gemini 3.1 Flash image + Kling 3.0 image-to-video) with **Claude** (Haiku + Sonnet) as the QA arbiter and **FFmpeg** for editorial conform.

LUMN is not a music video generator, a slideshow maker, or a social-clip tool. It is built around a film production mental model: brief, preproduction, shot design, anchor generation, video rendering, and editorial conform.

See `WHITEPAPER.txt` for the full V6 technical specification.

---

## Prerequisites

- Python 3.11+
- ffmpeg on PATH
- A `fal.ai` API key with billing enabled (Gemini 3.1 Flash + Kling 3.0 access)
- An `anthropic` API key (for Haiku/Sonnet QA)

## Setup

```bash
cd C:\Users\Mathe\lumn
pip install -r requirements.txt
copy env.example .env
# Edit .env:
#   FAL_API_KEY=fal-...
#   ANTHROPIC_API_KEY=sk-ant-...
#   LUMN_API_TOKEN=...  (optional, auto-generated if omitted)
```

## Run

```bash
python server.py
# Open http://localhost:3849
```

If `LUMN_API_TOKEN` is not set, a random session token is generated at startup and printed to the console. That token is required for all API calls from the UI.

## CLI pipeline (no server)

```bash
python scripts/pipeline_v6_gemini_kling.py anchors    # Gemini anchor stills
python scripts/pipeline_v6_gemini_kling.py select     # Sonnet picks candidates
python scripts/pipeline_v6_gemini_kling.py review     # Sonnet transition audit
python scripts/pipeline_v6_gemini_kling.py clips      # Kling image-to-video
python scripts/pipeline_v6_gemini_kling.py conform    # FFmpeg editorial conform
python scripts/pipeline_v6_gemini_kling.py all        # Full end-to-end run
```

Inputs: `output/pipeline/production_plan_v4.json` (beats + shots) and
`output/preproduction/packages.json` (approved preproduction packages).

---

## How it works

V6 flow:

```
master prompt
  -> POS packages  (character / costume / environment / prop)
  -> sheets        (Gemini 3.1 Flash, multi-panel reference sheets)
  -> shot plan     (beats expanded into shot-level camera specs)
  -> anchors       (Gemini edit-mode, refs carry identity, prompt is camera-only)
  -> video clips   (Kling 3.0 image-to-video, ~15-40 word motion prompt)
  -> conform       (FFmpeg, per-shot grade + transitions, final MP4)
```

Claude Haiku runs default per-shot QA. Claude Sonnet handles hero shots, borderline escalations, and full-sequence transition audits. See `WHITEPAPER.txt` sections **V6 PIPELINE** and **CLAUDE QA SYSTEM** for detail.

### PromptOS

PromptOS (POS) is the central registry for all creative entities in a production -- characters, costumes, environments, props, scenes, style locks, and world rules. Each entity holds structured metadata plus its canonical visual reference (a hero image drawn from its generated sheet). Prompt assembly pulls from these records when building anchor and motion prompts, so every shot inherits the correct visual context without the operator having to re-state world details per shot. POS is the heart of V6: sheets feed anchors, anchors feed clips, and the POS package is what binds them together.

### Refs not text

Visual identity is carried entirely by reference images, not prompt text. Anchor prompts describe only camera, pose, and framing. Describing the subject in a prompt when a reference image is present dilutes the reference signal and causes drift. This is the single most important rule in the system.

---

## Aesthetic / brand

LUMN's interface is cinematic and artistic -- not SaaS. It is meant to feel like a filmmaker's production wall, not a dashboard.

- **Type**: Inter Tight, light weight 300 for body, wide letter-spacing (6-8px) uppercase labels
- **Surfaces**: Muted rgba whites on dark backgrounds, 12px backdrop-blur translucent panels
- **Welcome atmosphere**: `bear_light.png` / `bear_dark.png` hero imagery -- the only heavy visual in the app, used as an atmospheric anchor not a logo
- **Brand assets**: `brand_assets/LUMN_Welcome_White_*.png` and `LUMN_Welcome_Dark_*.png` for 5K and 11K masters

Older iterations of the UI used a cyberpunk neon palette (cyan `#00E5FF`, magenta `#FF5E00`, amber, violet, JetBrains Mono, glowing shadows). That aesthetic is retired. Any reference to it in older docs or CSS should be treated as historical.

---

## Project structure

```
lumn/
  server.py                         HTTP server (port 3849, no Flask)
  generate.py                       Legacy CLI entry (kept for compat)
  requirements.txt
  WHITEPAPER.txt                    V6 technical spec

  lib/
    fal_client.py                   Gemini + Kling via fal.ai SDK
    claude_client.py                Haiku/Sonnet QA with escalation
    prompt_os.py                    Entity registry (POS)
    preproduction_assets.py         Sheet generation + package management
    video_stitcher.py               FFmpeg conform / grade / export
    audio_analyzer.py               librosa beat/BPM/section detection
    scene_planner.py                Scene plan + transition map
    transition_judge.py             5D continuity scorer
    transition_strategy.py          Strategy engine + fallback chain
    learning_system.py              Evidence log + self-healing rules
    prompt_packs/                   14 prompt pack modules

  scripts/
    pipeline_v6_gemini_kling.py     Full V6 CLI pipeline

  public/
    index.html                      Main web UI
    v6-pipeline.js                  V6 pipeline panel
    timeline.js                     Timeline / clip review
    landing.html                    Welcome / intro
    manifesto.html                  Design philosophy

  output/
    prompt_os/                      POS entity JSON + sheet images
    preproduction/                  Package records
    pipeline/
      anchors_v6/                   Gemini anchor stills
      clips_v6/                     Kling video clips
      final/                        Conformed MP4 output
      learning/                     Shot logs + rule history

  brand_assets/                     Welcome imagery, LUMN masters
  docs/                             Design docs (workspace-redesign, etc.)
  references/                       API refs + spec notes
```

---

## Deprecated engines (historical note)

- **Runway** -- deprecated in V6. All production runs through fal.ai. Legacy stubs remain in `lib/video_generator.py` for backward compatibility with old project files but are not called by the V6 pipeline.
- **Luma Ray2, Grok (xAI), OpenAI + Ken Burns** -- legacy engines retained as stubs for experimental use, not part of the V6 workflow.

No engine mixing: a production uses exactly one video engine from start to finish. Currently that is Kling 3.0 via fal.ai for all video.
