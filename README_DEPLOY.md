# LUMN Studio — AI Film Production App

## Quick Start (Local)

```bash
# 1. Enter directory
cd C:\Users\Mathe\lumn

# 2. Copy environment file and add your API keys
copy env.example .env
# Edit .env with your FAL_API_KEY (required) and ANTHROPIC_API_KEY (required)

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
railway variables set FAL_API_KEY=your_fal_key
railway variables set ANTHROPIC_API_KEY=your_anthropic_key
railway variables set LUMN_API_TOKEN=your_session_token  # Optional

# 5. Deploy
railway up
```

For the full production deployment runbook (S3/R2 object storage, Stripe billing,
Fly.io vs Railway, pre-launch checklist), see `DEPLOY.md`.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FAL_API_KEY` | Yes | fal.ai API key for Gemini 3.1 Flash + Kling 3.0 |
| `ANTHROPIC_API_KEY` | Yes | Anthropic key for Claude Haiku/Sonnet QA |
| `LUMN_API_TOKEN` | No | Session auth token for the web UI (auto-generated if unset) |
| `PORT` | No | Server port (default: 3849, Railway auto-sets) |

> **Note:** Earlier versions of LUMN referenced `RUNWAY_API_KEY`, `OPENAI_API_KEY`,
> `XAI_API_KEY`, `ELEVENLABS_API_KEY`, and `SUNO_API_KEY`. V6 is 100% fal.ai for
> generation and Anthropic for QA. Those older keys are not used by the V6 pipeline.

## Features

### V6 Pipeline

- **7-stage workflow**: Brief -> Drafts & Refs -> Assets -> Scenes -> Shots -> Render -> Output
- **fal.ai only**: Gemini 3.1 Flash for image, Kling 3.0 (v3 / o3, standard / pro) for video
- **PromptOS (POS)**: Character / costume / environment / prop entities with reference sheets as the source of truth for identity
- **Claude QA**: Haiku per-shot default, Sonnet for hero shots, borderline escalations, and full-sequence transition audits
- **Anchor-first rendering**: Gemini generates the start frame from references, Kling animates it. Prompts are camera-only; refs carry identity
- **Transition Intelligence**: 5D continuity scorer picks motivated-cut vs direct-animate vs bridge-frame strategies per cut

### Preproduction

- Multi-panel character sheets (8 panels: front/side/back/3-quarter/face x2/detail/expression)
- Costume sheets (full-body + detail panels)
- Environment sheets (establishing wide + interior POVs + light reads)
- Prop sheets (multi-angle detail)
- Package status gate: draft -> generating -> generated -> approved/locked
- Hero ref selection per package (the one canonical image driving downstream generation)

### Shot design

- Per-shot camera: angle (eye level, low, high, ground, bird, dutch, OTS), lens (24/35/50/85/135/200mm), size (XWS through XCU), movement (push in, pull back, pan, crane, static, handheld, orbit, whip pan, etc.)
- Per-shot Kling tier: v3_standard / v3_pro / o3_standard / o3_pro
- Per-shot duration 3-15s (V3 Pro locked to 5 or 10)
- Character element binding (up to 4 elements via Kling V3)

### Editorial

- Conformed output at target aspect (16:9 / 9:16 / 1:1 / 4:5)
- Per-shot color grade preset (none / warm / cold / vintage / high-contrast / noir / cyberpunk / sepia)
- 19 supported transition types assigned per cut by the Transition Intelligence system
- Audio track mixing with fade in/out
- Platform export presets (YouTube / Instagram / TikTok)
- Watermark, credits, thumbnail extraction

## Architecture

```
server.py                       HTTP server (port 3849, no Flask)
public/
  index.html                    Main web UI
  v6-pipeline.js                V6 pipeline panel
  timeline.js                   Timeline / clip review
lib/
  fal_client.py                 Gemini 3.1 Flash + Kling 3.0 via fal.ai SDK
  claude_client.py              Haiku/Sonnet QA with escalation
  prompt_os.py                  Character/costume/environment/prop/scene registry
  preproduction_assets.py       Sheet generation + package management
  video_stitcher.py             FFmpeg editorial conform + grade + export
  audio_analyzer.py             librosa beat/BPM/section analysis
  scene_planner.py              Scene plan + transition map
  transition_judge.py           5D continuity scorer
  transition_strategy.py        Strategy engine + fallback chain
  learning_system.py            Evidence log + self-healing rules
  prompt_packs/                 14 structured prompt pack modules
scripts/
  pipeline_v6_gemini_kling.py   Full V6 CLI pipeline
```

## CLI pipeline

```bash
python scripts/pipeline_v6_gemini_kling.py anchors    # Gemini anchor stills
python scripts/pipeline_v6_gemini_kling.py select     # Sonnet candidate selection
python scripts/pipeline_v6_gemini_kling.py review     # Sonnet sequence transition audit
python scripts/pipeline_v6_gemini_kling.py clips      # Kling image-to-video
python scripts/pipeline_v6_gemini_kling.py conform    # FFmpeg editorial conform
python scripts/pipeline_v6_gemini_kling.py all        # Full end-to-end run
```
