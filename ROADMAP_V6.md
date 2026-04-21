# LUMN V6 — Deferred Feature Roadmap

Snapshot date: 2026-04-14

This file captures features the original manifesto advertised but are **not shipping in V6**. They are deferred, not cancelled — each entry documents the vision, the rationale for deferral, and the rough implementation path so the idea can be picked up later without re-designing from scratch.

The current manifesto has been trimmed to match what actually works end-to-end. This doc is the "what we pulled and why" file.

---

## Why the trim

Pre-audit, the manifesto promised a full post-production stack: integrated TTS, voice cloning, multi-language dubbing, real stem separation, 8 export presets, album art generation, Spotify Canvas, banners. The audit found that several were paper-only: UI existed but backend was missing or stubbed, or the claim was pure marketing.

Shipping a whitepaper that 404s on first click is the worst kind of credibility hit. We'd rather ship a shorter, honest manifesto and move features from this roadmap into it one at a time as they become real.

---

## Deferred — Audio

### 1. AI Voice / TTS (narration + dialogue)
- **Vision:** Type a script, pick a voice, generate spoken audio that plays over selected scenes. Per-scene lyric/speech input on the shot list so a director can drop narration or dialogue into any clip.
- **Why deferred:** No backend. The "Generate Voice" button posts to `/api/audio/tts` which does not exist. Adding it is cheap (~45 min for an ElevenLabs wrapper) but we also haven't validated the **need** yet — most music-video and cinematic use cases don't need dialogue, and Kling 3.0 already provides diegetic ambient sound natively.
- **Note on Kling 3.0 audio:** `generate_audio=true` in `lib/fal_client.py:229` enables Kling's native scene-sound generation. This covers wind, footsteps, room tone, water, ambient atmosphere — the SFX/ambience layer the manifesto promised — *without* a second engine. Only narration/dialogue is uncovered.
- **Future home:** A per-scene "Speech" field on the shot list. Director types a line, picks a voice from a dropdown, LUMN generates the clip with the voice mixed in and auto-ducks the music during the line.
- **Dependencies:** ElevenLabs or OpenAI voices API key; UI field on shot list; audio mix pipeline update to layer voice + music + Kling ambient.

#### 1a. Lip sync (the dependency chain narration alone doesn't solve)
- **Why this matters for (1):** If narration is off-screen voiceover, plain TTS mixed over the clip works fine. But as soon as the director wants an **on-screen character speaking the line**, TTS alone produces a mismatch: the character's mouth moves randomly, the audio is correct, the viewer's brain rejects it. This is the uncanny-valley tax on dialogue.
- **Kling 3.0 I2V cannot lip sync in one pass.** Its `generate_audio=true` produces ambient diegetic sound, not speech that matches mouth shapes. You'd get a character making vague vocal noises that do not correspond to any specific words.
- **The real pipeline** is 3 steps, not 1:
  1. Generate base character video via Kling I2V (silent or ambient-audio only)
  2. Generate speech audio via TTS (item 1 above)
  3. Run (video + audio) through a **separate lip-sync model** — historically Kling has a standalone `v1.6/lipsync` endpoint on fal.ai; third-party options include Sync Labs, D-ID, HeyGen
- **One-shot alternative (not taking it):** Veo 3 (Google) natively generates speaking characters with correct lip sync from a text prompt. But LUMN is committed to fal.ai/Kling under the no-engine-mixing rule, and adopting Veo is an architecture decision, not a free upgrade. Revisit only if a talking-head use case becomes central.
- **When to build:** Only after (1) ships AND a real production needs on-screen dialogue. Voiceover-only narration (no visible mouth) can ship with just (1).

### 2. Voice Cloning / Speech-to-Speech Transfer
- **Vision:** Upload a reference voice sample, lock it as the project's narrator, and have every generated speech line use that voice. Keeps narrator consistent across a full film.
- **Why deferred:** Niche until (1) ships. No user has asked for it. Legal/ethical exposure (deepfake-adjacent) is non-trivial and deserves its own policy.
- **Future home:** Lives inside the Audio tab once TTS exists. A "Clone voice" upload button that registers a voice id against the project.
- **Dependencies:** ElevenLabs voice cloning API (or equivalent); consent/disclosure flow.

### 3. Multi-language Voice Dubbing
- **Vision:** Upload a finished film, translate all spoken lines to another language while preserving the original voice character, re-mix, re-export. Covers major release territories in one click.
- **Why deferred:** Requires (1) and (2) as foundations. Also needs automatic speech recognition, translation, and lip-sync — a whole second pipeline.
- **Future home:** Export tab, as a post-render modifier. "Dub to Spanish" → pipeline re-runs audio generation in target language and stitches a new video.
- **Dependencies:** Whisper (ASR) + Claude/GPT (translation) + cloned TTS (re-voice) + timing alignment.

### 4. Real Stem Separation (Demucs/Spleeter)
- **Vision:** Split an uploaded song into drums, bass, vocals, and melody stems. Per-instrument visual control — map drum hits to cuts, bass to camera sway, vocals to close-ups.
- **Why deferred:** Current `/api/audio/stems` is a stub that just lists files from `output/audio/`. Real separation needs a Demucs or Spleeter install (PyTorch, ~2GB model weights, GPU preferred). The per-instrument control idea is powerful but we haven't even shipped the simpler single-track beat sync yet.
- **Future home:** Audio tab. Upload a track → "Separate stems" button → 4 audio channels. Scene timeline gains 4 visualizer lanes. Scene trigger panel lets you map each stem to a visual effect.
- **Dependencies:** Demucs local install or a stem separation API; audio timeline UI rewrite.

### 5. Three-lane Audio Track Management
- **Vision:** Music, Voice/Dialogue, and SFX as three independent lanes with per-lane volume, ducking, and crossfade controls. The Audio tab UI already shows three lanes today.
- **Why deferred:** Backend handler in `_handle_mix_audio` only merges two tracks, not three. Either (a) upgrade backend to merge N tracks, or (b) collapse UI to 2 lanes until (1)/(4) land. Currently promising more than we deliver.
- **Future home:** Backend upgrade is a ~30 min change to `mix_audio_tracks` in `lib/video_processor.py`. Do it when (1) or (4) lands so the third lane has an actual purpose.

---

## Deferred — Export

### 6. YouTube Shorts export preset
- **Vision:** One-click 9:16 export sized for YT Shorts (60s cap, Shorts-specific encoding).
- **Why deferred:** Low effort — same as Instagram Reels preset with a different max duration. Just not wired yet.
- **Future home:** Add to `PLATFORM_SPECS` in `lib/video_stitcher.py`. ~5 min. Should probably just ship this now rather than defer.

### 7. Cinema / DCP export preset
- **Vision:** Export to true cinema format — 4K, DCI ratio (flat 1.85 or scope 2.39), DCP-compatible codec.
- **Why deferred:** No one is mastering DCPs from LUMN in the near term. Real DCP encoding needs specialized tooling (OpenDCP, easyDCP) and is its own rabbit hole.
- **Future home:** Only if a festival submission use case shows up. Low priority.

### 8. Album Art Generation (1:1)
- **Vision:** Generate a 1:1 album cover from the project style profile.
- **Why deferred:** Technically cheap — just a Gemini call with the project style refs and aspect ratio 1:1. Missing only because no one wired the button.
- **Future home:** Export tab, "Branding" panel. New endpoint `/api/branding/album-art` that composes a Gemini prompt from the project's style bible.
- **Dependencies:** None beyond Gemini. Easy pickup when it becomes a priority.

### 9. Spotify Canvas (9:16 short visual)
- **Vision:** Generate a looping 9:16 Spotify Canvas from a selected scene.
- **Why deferred:** Could be done now by just re-exporting one scene at 9:16 in a loop. Just needs a UI affordance.
- **Future home:** Export tab, "Branding" panel. Reuses the existing social export pipeline with a Canvas preset.

### 10. Banner / Header Generation (16:9)
- **Vision:** Generate horizontal banner stills for YouTube channel art, Twitter/X banners, web headers.
- **Why deferred:** Same as album art — cheap Gemini call, missing only the button.
- **Future home:** Export tab, "Branding" panel.

---

## Deferred — Timeline / Assembly

### 11. True drag-to-reorder / trim / scrub editor
- **Vision:** Full canvas timeline. Drag clips to reorder. Drag edges to trim. Scroll to pan. Zoom. Scrub playhead. Storyboard overview. Text overlay layers. As promised by the manifesto Assembly tab.
- **Why deferred:** UI scaffolding exists in the Assembly tab but the true editor is not implemented. Drag handlers and trim logic are missing.
- **Future home:** Own milestone. Worth delegating to a dedicated sprint — timeline editors are non-trivial to build correctly (collision handling, snap targets, undo stack).

---

## Not deferred — already working (keep in manifesto)

These are the honest keeps. Leaving them here as a reference for what the current audio/export story actually is:

**Audio**
- Upload MP3/WAV/M4A for music
- Automatic BPM + beat extraction with octave correction (`/api/v6/audio/beats`, `_correct_tempo_octave`) — biases to the music-video sweet spot (70–110 BPM) and halves 150+ detections that land in the pocket
- Beat-synced cuts: on-beat, half-time, double-time (`apply_beat_sync_cuts`)
- Auto-ducking under dialogue when vocals detected (`apply_audio_ducking`)
- Native Kling 3.0 scene sound by default (wind, footsteps, ambient — `generate_audio=True` at every call site)
- Waveform visualizer with beat markers

**Export**
- YouTube, TikTok, Instagram Reels, Twitter/X presets (4 platforms)
- GIF export (per scene)
- Real-ESRGAN upscaling for sub-1080p sources (CLI → python → Lanczos fallback chain)
- Watermarks: custom text and logo, position and opacity
- Motivated transition selection at every beat boundary (hard cut, smash, J-cut, L-cut, match) — user-directed, not auto-scored

---

## Deferred — Intelligence

### 12. Transition Intelligence verdict-driven assembly
- **Vision:** The 5-dimension Transition Judge (`lib/transition_judge.py`) scores every cut, and the verdict — not user selection — drives the final stitch. High-risk cuts get auto-substituted for safer alternatives; weakest-transition warnings surface in the UI.
- **Why deferred:** The judge works and runs in the v4 draft script path (`scripts/generate_v4_draft.py` via `compile_conform_from_ti`). But the HTTP server's manual stitch path (`_run_manual_stitch` at `server.py:2743`) passes user-selected transitions straight to `stitch()` with zero consultation of the judge. The manual plan uses a flat `scenes` list, the judge expects a `beats → shots` plan structure — the two data shapes don't match without an adapter.
- **Future home:** Adapter layer `manual_plan_to_ti_plan` that synthesizes beat/shot metadata (shot family from framing tags, action from motion prompt, character_id from UI selection), then runs `compile_transition_intelligence`, stores the report in `gen_state['ti_report']`, and surfaces weakest-transition warnings in the stitch UI. ~2 hours.
- **Dependencies:** None new — `transition_judge`, `transition_strategy`, `cinematic_compiler` all exist.

---

## Ground rules for moving items OUT of this file

1. **Don't move to manifesto until it's wired end-to-end** — UI button + backend handler + success case observed in a live smoke test.
2. **Don't build for hypothetical demand** — pick items when a real user or a real production needs them, not when they sound cool.
3. **Kill items that stop making sense** — if a deferred item turns out to be subsumed by a model upgrade (e.g. Kling starts handling dialogue), delete the entry instead of shipping it.
