# LUMN Studio Roadmap
## Last Updated: 2026-04-06

---

## PHASE 1: One-Click Movie (Next Sprint)
**Goal:** User provides idea + photos + song → LUMN does everything else

### 1.1 "Make My Movie" Button
- Single CTA on Project page
- Chains: AI shot sheet → asset creation → frame gen → clip gen → auto-arrange → music sync → render
- User just reviews and tweaks at any step
- **AI Needed:** LLM for shot sheet writing (have via Grok/Claude), image gen (have via Runway), video gen (have via Runway)

### 1.2 Preview Before Spend
- Free text-only prompt preview before generation
- Cheap $0.01 thumbnail sketch (low-res turbo) before full $0.15 generation
- Cost confirmation dialog: "This will cost ~$X. Proceed?"
- **AI Needed:** Runway gen4_image_turbo for cheap previews (already integrated)

### 1.3 Smart Defaults
- Project type → auto-fills scene count, duration, transitions, framing
- Atmosphere selection → auto-suggests lighting, color grade, camera movement
- Genre presets (dark trap, romance, action, horror, etc.)
- **AI Needed:** None — rule-based logic

---

## PHASE 2: AI Copilot (2-3 weeks)
**Goal:** AI assistant guides user through every step

### 2.1 Inline Suggestions
- "Your character has no photo — upload one for better results"
- "Scene 3 lighting doesn't match Scene 2 — auto-fix?"
- "You have 5 scenes but only 2 clips — generate the rest?"
- Contextual tips based on what workspace you're in
- **AI Needed:** LLM (Claude/Grok) for contextual analysis — need API calls per suggestion

### 2.2 "Fix It For Me" Buttons
- Next to every warning: one-click resolution
- "3 scenes missing clips" → [Generate Missing]
- "Character has no description" → [AI Describe from Photo]
- "Audio not beat-synced" → [Auto Beat Sync]
- **AI Needed:** Existing APIs — just needs UI wiring

### 2.3 AI Scene Enhancement
- User writes basic prompt: "man walks through rain"
- AI enhances: "A lone figure walks through sheets of rain on a neon-lit street, camera slowly pushing in, reflections on wet pavement, melancholy atmosphere, shallow depth of field, 85mm lens"
- **AI Needed:** LLM for prompt enhancement (have via /api/enhance-prompt)

---

## PHASE 3: Polish & Power Features (1 month)

### 3.1 Drag-and-Drop Everything
- Photos → character cards (auto-upload + AI describe)
- Songs → project (auto-upload + beat analysis)
- Videos → scene cards (become clips)
- Scene cards → reorder (fix existing broken drag)
- **AI Needed:** Vision API for photo-to-description (have via Grok vision)

### 3.2 Visual Progress Pipeline
- Replace text tabs with visual flow diagram
- [✓ Story] → [✓ Characters] → [⏳ Generating 3/8] → [ ] Edit
- Click any step to jump. Always visible.
- **AI Needed:** None — pure UI

### 3.3 Undo/Version History
- Ctrl+Z for every action
- Scene version history with restore
- Project snapshots
- **AI Needed:** None — state management

### 3.4 Real Templates
- "Music Video — Dark Trap" with 8 pre-filled scenes
- "Short Film — Romance" with 12 scenes, golden hour
- "Trailer — Action" with fast cuts, bass drops
- Each includes sample prompts user customizes
- **AI Needed:** LLM to generate template variations

---

## PHASE 4: Advanced AI Features (2 months)

### 4.1 Lip Sync (Kling 3.0)
- Generate talking head videos from audio + photo
- Sync lip movements to voiceover/dialogue
- **AI Needed:** Kling 3.0 API (not yet integrated)

### 4.2 Smart Engine Routing
- Auto-recommend best model per scene type
- Close-up character → Runway Gen4.5 (best face consistency)
- Wide landscape → Grok (cheaper, good for environments)
- Fast action → Veo 3.1 (better motion)
- **AI Needed:** Rule-based + LLM for complex scenes

### 4.3 AI Storyboard
- Before committing to video generation, generate storyboard illustrations
- Cheap static images showing composition, framing, mood
- User approves storyboard THEN generates video
- **AI Needed:** Runway text_to_image turbo ($0.01 per frame)

### 4.4 AI Color Grading
- "Make it look like Blade Runner" → AI applies color grade across all scenes
- Style transfer from reference movie screenshots
- **AI Needed:** LLM for color analysis + ffmpeg filters

### 4.5 AI Music Scoring
- Analyze scene moods → generate matching music per section
- Dynamic tempo matching to cut rhythm
- **AI Needed:** Suno API for music generation (partially integrated)

### 4.6 Multi-Character Consistency
- Same character looks the same across ALL scenes
- Character sheet → face lock → seed propagation
- Currently only uses first character per scene (NEEDS FIX)
- **AI Needed:** Runway reference images (have), better prompt engineering

---

## PHASE 5: Platform (3+ months)

### 5.1 Mobile Quick Mode
- 3 screens: Describe → Review (swipe) → Export
- Stripped-down interface for phone
- **AI Needed:** Same backend, simplified frontend

### 5.2 Collaboration
- Multiple users editing same project
- WebSocket for real-time sync
- Role-based access (director, editor, reviewer)
- **AI Needed:** None — infrastructure

### 5.3 Deploy & Scale
- Railway deployment with custom domain
- CDN for generated videos
- Multi-user auth (OAuth)
- Usage billing
- **AI Needed:** None — DevOps

### 5.4 Export to Social
- Direct upload: YouTube, TikTok, Instagram, Twitter
- Auto-format for each platform (aspect ratio, duration limits)
- Thumbnail generation
- **AI Needed:** Image gen for thumbnails

### 5.5 Marketplace
- Share/sell project templates
- Community shot sheets
- Asset packs (characters, environments, music)
- **AI Needed:** None — platform feature

---

## AI REQUIREMENTS SUMMARY

| Feature | AI Model | Status | Cost |
|---------|----------|--------|------|
| Shot sheet writing | Grok/Claude LLM | Have API | ~$0.003/plan |
| Image generation | Runway Gen4.5 | Integrated | ~$0.05/image |
| Video generation | Runway Gen4.5 | Integrated | ~$0.15/clip |
| Cheap preview | Runway Turbo | Integrated | ~$0.01/preview |
| Photo description | Grok Vision | Integrated | ~$0.01/photo |
| Prompt enhancement | Grok/Claude | Have API | ~$0.003/enhance |
| Music generation | Suno | Partial | ~$0.10/song |
| Voice clone | ElevenLabs | Partial | ~$0.30/1000chars |
| TTS | ElevenLabs/Runway | Partial | ~$0.01/sentence |
| Auto-captions | Whisper | Not integrated | Free (local) |
| Lip sync | Kling 3.0 | Not integrated | TBD |
| Beat analysis | librosa | Integrated | Free (local) |
| Smart routing | Rule-based | Not implemented | Free |
| AI copilot suggestions | Claude/Grok | Have API | ~$0.01/suggestion |

**Total AI cost per 30-second video:** ~$2-5 (depends on scene count + quality)
**Most expensive:** Video generation (Runway clips)
**Cheapest:** Beat analysis, smart routing, defaults (free/local)
