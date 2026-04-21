# LUMN V6 — Master Punch List

Last updated: 2026-04-14

A living checklist of everything that needs to work, everything to test, and everything to remove. Work this top-to-bottom, tick items as they pass.

---

## How to use this doc

- **Status keys:** ✅ working & verified · 🟡 partial / has known issues · 🔴 broken · ⚠️ not tested · 🚫 deferred to ROADMAP_V6.md
- **Rule:** don't mark ✅ until the feature has been exercised end-to-end in the real app within the last 7 days.
- **When you find a bug:** add it to the "Known Issues" section at the bottom with a short repro, don't bury it in a sub-item.
- **When you kill a feature:** move the entry to "Removed" with a one-line reason so we don't accidentally re-add it.

---

## Approach — three-prong QA strategy

Instead of whack-a-mole smoke testing, attack it from three angles that reinforce each other:

1. **Scripted golden-path E2E test** (`scripts/smoke_golden_path.py` — TO BUILD). One Python script that drives the full pipeline via HTTP: new project → seed 2 characters → 3 shots → render stitch → export → download. Runs in 30s against a live server. Catches regressions in the critical path. Run after every significant change.
2. **Live punch list** (this doc). Tracks feature-level state organized by surface. Updated as bugs are found and fixed. Source of truth for "what works."
3. **Manual aesthetic walkthrough** (`MANUAL_QA.md` — TO BUILD). A human-only checklist: typography, spacing, color, microcopy, empty states, loading states, error states. Scripts can't judge these. Walk through once per sprint.

---

## 1. Golden Path (critical — must always work)

This is the single flow that justifies the product. If any of these break, everything else is secondary.

- [ ] ⚠️ Fresh install: server boots, DB init, no migration errors
- [ ] ⚠️ Welcome → Enter → landing into `/`
- [ ] ⚠️ Sign up / sign in / sign out flow
- [ ] ⚠️ Create new project from scratch (empty state)
- [ ] ⚠️ Upload music → beats extracted → BPM correct
- [ ] ⚠️ Create 1 character in POS (upload photo, auto-describe, save)
- [ ] ⚠️ Create 1 environment in POS (upload photo, auto-describe, save)
- [ ] ⚠️ Add shot to shot list, link character + environment
- [ ] ⚠️ Generate Gemini anchor still for that shot (with correct reference fidelity)
- [ ] ⚠️ Generate Kling 3.0 clip from the anchor
- [ ] ⚠️ Add 2 more shots, repeat
- [ ] ⚠️ Stitch clips with selected transitions → final video plays
- [ ] ⚠️ Export YouTube preset → downloads MP4
- [ ] ⚠️ Save project (zip) → Reset → Load → everything restored

---

## 2. Backend API — endpoint smoke

Group by surface. Each endpoint needs: correct auth gating, valid-input happy path, invalid-input rejection, expected JSON shape.

### Auth & session
- [ ] ⚠️ `POST /api/auth/signup` — creates user, returns session
- [ ] ⚠️ `POST /api/auth/login` — valid creds → session; bad creds → 401
- [ ] ⚠️ `GET /api/auth/me` — returns current user
- [ ] ⚠️ `POST /api/auth/logout` — clears session
- [ ] ⚠️ Unauth request to protected endpoint → 401 JSON

### Prompt OS (characters, costumes, environments, scenes, voices)
- [ ] ⚠️ `GET /api/pos/characters` — list
- [ ] ⚠️ `POST /api/pos/characters` — create
- [ ] ⚠️ `PUT /api/pos/characters/{id}` — update (fields persist)
- [ ] ⚠️ `DELETE /api/pos/characters/{id}` — delete
- [ ] ⚠️ `POST /api/pos/characters/{id}/photo` — upload reference photo
- [ ] ⚠️ `POST /api/pos/characters/{id}/describe` — vision auto-describe
- [ ] ⚠️ `POST /api/pos/characters/{id}/generate-preview` — generate sheet
- [ ] ⚠️ Same 6 routes for **costumes**
- [ ] ⚠️ Same 6 routes for **environments**
- [ ] ⚠️ Scene CRUD: `GET/POST/PUT/DELETE /api/pos/scenes`
- [ ] ⚠️ Voice CRUD: `GET/POST/PUT/DELETE /api/pos/voices`
- [ ] ✅ `POST /api/pos/voices/{id}/sample` — upload audio sample (wired this turn)
- [ ] ⚠️ `GET /api/pos/voices/{id}/sample` — retrieve audio sample

### Shot list / manual plan
- [ ] ⚠️ `GET /api/manual/scenes` — list shots
- [ ] ⚠️ `POST /api/manual/scene` — add shot
- [ ] ⚠️ `PUT /api/manual/scene/{id}` — update (characterIds array, costumeIds array, environmentId all persist)
- [ ] ⚠️ `DELETE /api/manual/scene/{id}` — remove
- [ ] ⚠️ `POST /api/manual/scene/{id}/reorder` — reorder
- [ ] ⚠️ `POST /api/manual/scene/{id}/duplicate` — duplicate
- [ ] ⚠️ `POST /api/manual/scene/{id}/generate` — Kling render
- [ ] ⚠️ `POST /api/manual/scene/{id}/regenerate` — re-render
- [ ] ⚠️ `POST /api/manual/scene/{id}/reverse` — reverse clip
- [ ] ⚠️ `POST /api/manual/scene/{id}/frames` — extract frames
- [ ] ⚠️ `POST /api/manual/stitch` — assemble final video

### Project / state
- [ ] ✅ `POST /api/project/save-full` — zip save (verified via roundtrip test)
- [ ] ✅ `POST /api/project/load-full` — zip load (verified via roundtrip test)
- [ ] ⚠️ `GET /api/project/reset` — clears everything, returns `cleared[]`
- [ ] ⚠️ `POST /api/project/autosave` — saves silent snapshot
- [ ] ⚠️ `GET /api/project/autosave` — returns snapshot for restore banner
- [ ] ⚠️ `GET /api/projects` — project browser list
- [ ] ⚠️ `POST /api/projects` — create project record
- [ ] ⚠️ `POST /api/projects/{id}/load` — open by id
- [ ] ⚠️ `DELETE /api/projects/{id}` — delete by id

### Templates
- [ ] ⚠️ `GET /api/templates` — list
- [ ] ⚠️ `POST /api/templates/save` — save current state as template
- [ ] ⚠️ `POST /api/templates/load` — load template
- [ ] ⚠️ `POST /api/templates/apply` — apply template to current project
- [ ] ⚠️ `DELETE /api/templates/{id}` — delete

### Audio
- [ ] ⚠️ `POST /api/audio/upload` — MP3/WAV/M4A
- [ ] ⚠️ `POST /api/v6/audio/beats` — BPM + beat extraction with octave correction
- [ ] ⚠️ `POST /api/audio/mix` — 2-lane mix (music + vocal)
- [ ] ⚠️ `POST /api/audio/ducking` — auto-duck under vocals

### Export / stitch
- [ ] ⚠️ `POST /api/manual/stitch` — happy path
- [ ] ⚠️ `POST /api/export/platform` — YouTube / TikTok / IG Reel / Twitter / GIF presets
- [ ] ⚠️ Watermark (text + logo) applies correctly
- [ ] ⚠️ Real-ESRGAN upscale path for sub-1080p
- [ ] ⚠️ GIF per-scene export

### Misc
- [ ] ⚠️ `GET /api/cost` — cost tracker
- [ ] ⚠️ `GET /api/analytics` — project analytics
- [ ] ⚠️ `GET /api/engines/catalog` — model catalog
- [ ] ⚠️ `POST /api/feedback` — user feedback endpoint

---

## 3. Frontend — by workspace

### Top toolbar / menu (always visible)
- [ ] ✅ Project dropdown (Projects · New · Save · Load · Prompt Vault · Analytics)
- [ ] ✅ Save button — uses zip flow (verified this turn)
- [ ] ✅ Load button — accepts .zip only (verified this turn)
- [ ] ✅ Analytics modal populates (fixed DOM id mismatch this turn)
- [ ] ⚠️ Prompt Vault navigation
- [ ] ⚠️ Undo/Redo buttons + Ctrl+Z / Ctrl+Y
- [ ] ⚠️ `?` keyboard shortcuts modal opens + lists accurate shortcuts
- [ ] ⚠️ Welcome button returns to landing
- [ ] ⚠️ Guided/Expert mode toggle actually changes UI
- [ ] ⚠️ A11y menu — text size, contrast, reduced motion all persist
- [ ] ⚠️ User menu — email, credits, sign out
- [ ] ⚠️ Ctrl+S saves, Ctrl+G generates, Ctrl+Enter stitches, Space plays
- [ ] ⚠️ 1-5 keys switch stages (brief/assets/shots/render/output)
- [ ] ⚠️ Theme toggle (dark/light) — persists via localStorage
- [ ] ⚠️ Stage stepper click navigation (brief → assets → shots → render → output)

### Welcome page (`/landing`)
- [ ] ⚠️ Light phase loads and fades to dark
- [ ] ⚠️ ENTER button fades in after 1.2s
- [ ] ⚠️ Parallax on mouse move (subtle, 1-2px)
- [ ] ⚠️ Keyboard: Enter key triggers transition
- [ ] ⚠️ Theme handoff to main app via `lumn-theme` localStorage
- [ ] ⚠️ LUMN wordmark matches main app (amber glow dark / muted light)

### Manifesto page (`/manifesto`)
- [ ] ⚠️ Loads without 404
- [ ] ⚠️ All section links scroll correctly
- [ ] ⚠️ Stage-by-Stage table has 5 rows (not 7)
- [ ] ⚠️ No Transition Intelligence claim
- [ ] ⚠️ Audio section reflects 2-lane reality (music + vocal, not 3-lane)
- [ ] ⚠️ Export section lists 5 presets (not 8)
- [ ] ⚠️ No TTS / voice clone / dubbing / stem separation claims
- [ ] ⚠️ Proof row renders
- [ ] ⚠️ Welcome page aesthetic consistency

### Stage 1 — Brief (`#projectContainer`)
- [ ] ⚠️ Project title input persists
- [ ] ⚠️ Style profile (genre, mood, tone)
- [ ] ⚠️ Duration target field
- [ ] ⚠️ World/scene-bible references
- [ ] ⚠️ Cost tracker badge visible and live-updating
- [ ] ⚠️ "Next: Cast & Sets" button advances stage

### Stage 2 — Cast & Sets (POS)
- [ ] ⚠️ Sub-tabs: Characters · Costumes · Environments · Voices
- [ ] ⚠️ Add Character button opens form
- [ ] ⚠️ Upload photo → shows in preview
- [ ] ⚠️ Auto-describe button fills all fields
- [ ] ⚠️ Save character → appears in library
- [ ] ⚠️ Edit character → changes persist
- [ ] ⚠️ Delete character → gone from library, removed from any shots
- [ ] ⚠️ Generate Preview button creates character sheet
- [ ] ⚠️ Same for costumes, environments
- [ ] ⚠️ Voices: all CRUD works + sample upload/playback
- [ ] ⚠️ World Rules field persists

### Stage 3 — Shot List
- [ ] ⚠️ Add Shot button creates blank shot
- [ ] ⚠️ Prompt field persists
- [ ] ⚠️ Duration selector (5 or 10s — Kling limits)
- [ ] ⚠️ Engine selector shows only Kling (3 options)
- [ ] ⚠️ Character link picker (multi-select → characterIds array persists)
- [ ] ⚠️ Costume link picker (multi-select → costumeIds array persists)
- [ ] ⚠️ Environment link picker (single → environmentId persists)
- [ ] ⚠️ Transition selector (hard cut, smash, J-cut, L-cut, match)
- [ ] ⚠️ Reorder drag handle works
- [ ] ⚠️ Duplicate shot button
- [ ] ⚠️ Delete shot button + confirm
- [ ] ⚠️ Generate anchor still → shows thumbnail
- [ ] ⚠️ Generate clip → shows video preview
- [ ] ⚠️ Regenerate clip button
- [ ] ⚠️ Reverse clip option
- [ ] ⚠️ Loop option
- [ ] ⚠️ Effect intensity slider
- [ ] ⚠️ Color grade selector
- [ ] ⚠️ Camera movement selector
- [ ] ⚠️ Keyboard: `n` adds shot, `d` duplicates, `Delete` removes, `r` regenerates

### Stage 4 — Render (stitch)
- [ ] ⚠️ Timeline shows all shots in order
- [ ] ⚠️ Transitions preview correctly
- [ ] ⚠️ Music lane displays waveform + beat markers
- [ ] ⚠️ Vocal lane (when set) with ducking preview
- [ ] ⚠️ Stitch button assembles final MP4
- [ ] ⚠️ Progress indicator during render
- [ ] ⚠️ Pause button (if render is pauseable)
- [ ] ⚠️ Final video player works
- [ ] ⚠️ Cost ticker updates
- [ ] ⚠️ Error states: no shots, no clips, no music → clear warnings

### Stage 5 — Export
- [ ] ⚠️ Preset buttons: YouTube · TikTok · IG Reel · Twitter · GIF
- [ ] ⚠️ Watermark text field + position + opacity
- [ ] ⚠️ Watermark logo upload + position + opacity
- [ ] ⚠️ Upscale toggle (Real-ESRGAN)
- [ ] ⚠️ Export button produces correct aspect ratio per preset
- [ ] ⚠️ Download delivered file

---

## 4. Global / cross-cutting

- [ ] ⚠️ Autosave runs every 60s without blocking UI
- [ ] ⚠️ Autosave restore banner appears on reload after crash
- [ ] ⚠️ `beforeunload` warning on unsaved changes
- [ ] ⚠️ Toast system (success, error, info) — positioning, dismissal, stacking
- [ ] ⚠️ Modal system (escape closes, click-outside closes, focus trap)
- [ ] ⚠️ Loading spinners on all async buttons
- [ ] ⚠️ Error states actually render (not blank screen on failure)
- [ ] ⚠️ 404 page for unknown routes
- [ ] ⚠️ 500 page / error boundary for JS crashes
- [ ] ⚠️ Mobile viewport gracefully degrades or shows "desktop only"

---

## 5. Aesthetic polish (manual, human-judged)

These can't be scripted — walk through them eyes-on.

- [ ] ⚠️ LUMN wordmark is consistent everywhere (font, weight, letter-spacing, glow)
- [ ] ⚠️ Welcome page → main app theme handoff has no flash
- [ ] ⚠️ All button text is consistent case (no stray UPPERCASE resets)
- [ ] ⚠️ Dropdown items use consistent padding / hover state
- [ ] ⚠️ Modal titles consistent weight + color
- [ ] ⚠️ Form labels consistent font-size + color + spacing
- [ ] ⚠️ Empty states have helpful copy + illustration, not blank boxes
- [ ] ⚠️ Loading states don't show "undefined" or raw JSON
- [ ] ⚠️ Error messages are human-readable, not stack traces
- [ ] ⚠️ Manifesto typography matches Welcome page
- [ ] ⚠️ Bear hero images render at correct aspect ratio, no stretching
- [ ] ⚠️ Color tokens (amber, cyan, green) used consistently
- [ ] ⚠️ Dark mode has no white flashes on nav/modal open
- [ ] ⚠️ Light mode has no black flashes on nav/modal open
- [ ] ⚠️ Keyboard focus rings visible on all interactive elements
- [ ] ⚠️ Scrollbars styled consistently (or native — pick one and commit)
- [ ] ⚠️ Icons are consistent style (all outline or all filled, not mixed)

---

## 6. Cleanup — dead code to hunt & purge

Known candidates. Add to this list as the explore agents find more.

- [ ] ⚠️ Search for any remaining "Runway" references (should be zero)
- [ ] ⚠️ Search for any remaining "grok" / "veo" / "luma" references
- [ ] ⚠️ Search for orphaned `window._xxx` functions (defined but never called)
- [ ] ⚠️ Search for DOM ids referenced in JS but not present in HTML
- [ ] ⚠️ Search for DOM ids present in HTML but never referenced in JS
- [ ] ⚠️ Search for API endpoints defined but never called
- [ ] ⚠️ Search for backend handlers defined but never routed
- [ ] ⚠️ Audit stale `.md` files at repo root (AUDIT_MASTER, AUDIT_RESULTS, FINAL_AUDIT, REAUDIT_RESULTS, GENERATION_AUDIT, ROADMAP, ROADMAP_V2) — consolidate or delete
- [ ] ⚠️ Hidden panels with `display:none` — either wire them or remove entirely

---

## 7. Test harness — infrastructure to build

- [ ] ⚠️ `scripts/smoke_golden_path.py` — scripted E2E against live server with fake auth token
- [ ] ⚠️ `scripts/smoke_endpoints.py` — hits every endpoint with minimal payload, asserts 200 or expected error
- [ ] ⚠️ `scripts/smoke_pos_crud.py` — exercises all POS CRUD (characters/costumes/environments/scenes/voices)
- [ ] ⚠️ `scripts/smoke_save_load.py` — save → reset → load roundtrip (similar to `C:/tmp/lumn_full_roundtrip_test.py` but against live server)
- [ ] ⚠️ Playwright walkthrough script for menu bar (clicks each item, asserts modal/nav)
- [ ] ⚠️ `MANUAL_QA.md` — human-only visual checklist
- [ ] ⚠️ CI hook: on commit, run smoke_endpoints + smoke_golden_path
- [ ] ⚠️ Seed script: `scripts/seed_demo_project.py` — creates a fixed demo project with 2 characters, 3 shots, known music for repeatable testing

---

## 8. Deferred — see ROADMAP_V6.md

These are promised features that are **not** shipping in V6. Don't test them. Don't expose UI for them. Pointer only:

- 🚫 TTS / AI voice / narration
- 🚫 Voice cloning
- 🚫 Multi-language dubbing
- 🚫 Real stem separation (Demucs/Spleeter)
- 🚫 3-lane audio (music + voice + SFX)
- 🚫 YouTube Shorts preset
- 🚫 Cinema / DCP export
- 🚫 Album art generation
- 🚫 Spotify Canvas
- 🚫 Banner / header generation
- 🚫 True drag-to-reorder timeline editor
- 🚫 Transition Intelligence verdict-driven assembly

---

## 9. Known issues / bugs (fresh list)

Add here as found. Remove when fixed.

- None currently logged. Next smoke pass will populate this.

---

## 10. Removed (don't re-add)

Short history of what was ripped out and why, so we don't accidentally resurrect it.

- **QR Code / Embed Code / Version History / Storyboard PDF / Best GIFs** — orphan JS functions with no UI callers. Purged this turn. Backend endpoints also removed.
- **Full Save / Full Load** — collapsed into single Save/Load (zip-based by default). Per user decision 2026-04-14.
- **Plain `/api/project/save` + `/api/project/load`** — JSON-only, didn't preserve clips. Dead after Save consolidation.
- **Runway / Veo / Luma / Grok engines** — LUMN is fal.ai only (Kling 3.0 Pro / 2.1 Master / 2.1 Standard).
- **TTS / clone / stems / album art / canvas / banner / YT Shorts / Cinema buttons** — all unwired UI removed per ROADMAP_V6 trim.
- **Restyle / Multi-Angle / Performance scene buttons** — called dead Runway endpoints.
- **3-lane audio UI** — collapsed to 2-lane (music + vocal) until real stem separation ships.
- **Welcome mode picker** — replaced with toolbar toggle.
- **Keyboard shortcuts 6, 7** — stage nav is 1-5 (5 stages, not 7).

---

## Working session ritual

When starting a session with this doc:

1. Boot server, run `scripts/smoke_golden_path.py` (once it exists) — green light to proceed
2. Pick **one** section above (not all at once)
3. Walk each ⚠️ item, exercise it in the UI or via curl, flip to ✅ / 🟡 / 🔴
4. For 🔴 items: either fix immediately if <30 min, or log in "Known issues" with repro
5. At end of session: commit this doc with updated statuses so the next session starts with fresh context
