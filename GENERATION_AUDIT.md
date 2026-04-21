# LUMN Studio Generation Pipeline Audit — 10 Specialized Auditors
## Date: 2026-04-06 | Total Issues: 200+

## CRITICAL — Core Generation Broken

| # | Issue | Agents | Impact |
|---|-------|--------|--------|
| 1 | **Duplicate reference photo collection** — video_generator.py collects photos twice, second overwrites first | 3/10 | Character refs lost |
| 2 | **Auto-director render missing 50%+ features** — no text overlays, multi-track audio, vocal overlays, speed ramps, color grading, effects, audio ducking, progress callback | 1/10 | Render output incomplete |
| 3 | **prompt_os.py _save_json has NO file locking** — concurrent writes corrupt data | 2/10 | Data corruption |
| 4 | **Props completely omitted from prompt assembly** — assemble_prompt has no props parameter | 1/10 | Props ignored in generation |
| 5 | **Quality level lambda scope violation** — NameError at runtime when sheet has approvedSheet | 1/10 | Crash during generation |
| 6 | **Many API endpoints MISSING** — TTS, voice clone, Suno generate, auto-captions, AI auto-fill, director brain presets, AutoAgent start/stop/status | 3/10 | Features silently fail |
| 7 | **Beat sync infinite loop** — zero bar_duration creates infinite array | 1/10 | Server OOM crash |
| 8 | **Audio ducking filter injection** — user-controlled duck_level interpolated into ffmpeg filter | 1/10 | Command injection |
| 9 | **Seed NOT included in first frame generation** — payload empty | 1/10 | Seeds don't work |
| 10 | **No drag-to-reorder on scene cards** — CSS handle exists, no JS handlers | 1/10 | Feature broken |
| 11 | **clip_url vs clipUrl naming inconsistency** — some code paths set one, check the other | 1/10 | Clips lost/invisible |
| 12 | **Sheet approval endpoint MISSING** — frontend calls /api/pos/sheets/approve, no handler | 2/10 | Approval broken |
| 13 | **Multiple characters per scene only uses FIRST** — pos_chars[0] always | 1/10 | Multi-char scenes broken |
| 14 | **Batch state never cleared on exception** — active=True blocks future batches | 1/10 | Batch generation stuck |
| 15 | **Character description leaks across scenes** — same desc for all scenes in batch | 1/10 | Wrong descriptions |

## HIGH — Data Integrity & API Issues

| # | Issue | Agents |
|---|-------|--------|
| 16 | No cascade delete (character deletion orphans costumes/scenes) | 2/10 |
| 17 | No ID collision detection (8-char UUID) | 2/10 |
| 18 | Inconsistent field naming (referencePhoto vs referenceImagePath) | 2/10 |
| 19 | Photo auto-describe runs EVERY generation (wastes API credits) | 1/10 |
| 20 | Approved sheet URL never persisted to character library | 1/10 |
| 21 | Missing 400 error handling in Runway polling — crashes immediately | 1/10 |
| 22 | Resolution hardcoded 1080p despite 2K/4K claims | 1/10 |
| 23 | Cost tracking formula wrong (only Runway, ignores image gen) | 1/10 |
| 24 | Data URI size warning but no rejection (>5.2MB causes API failure) | 1/10 |
| 25 | Transition mapping bug when clips filtered (off-by-one) | 1/10 |
| 26 | Render payload parameters ignored (format, quality, resolution) | 1/10 |
| 27 | Timeline trim not persisted to server | 1/10 |
| 28 | Rating not persisted to scene object (ephemeral) | 1/10 |
| 29 | Approval state mutated before server confirms | 1/10 |
| 30 | Preview generation doesn't check asset readiness | 1/10 |
| 31 | Director Brain recommendations never used in generation | 1/10 |
| 32 | AutoAgent never initialized — dead code | 1/10 |
| 33 | No smart engine selection — always uses same engine | 1/10 |
| 34 | Costume never rotated — always first costume | 1/10 |
| 35 | Fuzzy character matching unreliable (substring) | 1/10 |

## MEDIUM — Incomplete Features & UX

| # | Issue | Agents |
|---|-------|--------|
| 36 | Prompt truncation at 1500 chars without warning | 1/10 |
| 37 | Variable substitution fails silently for missing entities | 1/10 |
| 38 | Negative prompt not applied in all code paths | 1/10 |
| 39 | Prompt cleaning removes then re-adds "photorealistic" | 1/10 |
| 40 | Shot sheet character detection has excessive false positives | 1/10 |
| 41 | Environment detection creates duplicates | 1/10 |
| 42 | Asset creation sends insufficient data for generation | 1/10 |
| 43 | Timeline scene index mismatch after reorder | 1/10 |
| 44 | Trim constraint allows 80% on each end independently | 1/10 |
| 45 | Waveform struct unpack fails on odd byte counts | 1/10 |
| 46 | Beat sync floating-point comparison never matches | 1/10 |
| 47 | Multiple file uploads overwrite same filename | 1/10 |
| 48 | Visual picker missing for time/weather categories | 1/10 |
| 49 | Stale preview status on network failure | 1/10 |
| 50 | DPR scaling not applied to canvas text metrics | 1/10 |

## FALSE ALARMS (verified these exist)
- import-shots endpoint EXISTS (agent 5 searched wrong section)
- Rate limiting EXISTS with threading.Lock
- CSRF validation EXISTS on all POST/PUT/DELETE
- File upload MIME validation EXISTS
- Subprocess timeouts EXIST on all calls
