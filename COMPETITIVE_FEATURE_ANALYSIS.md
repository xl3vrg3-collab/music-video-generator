# LUMN Studio -- Competitive Feature Analysis
## April 2026 (last revised for V6 pipeline)

Research conducted across 19 platforms: CapCut, DaVinci Resolve, Premiere Pro, Final Cut Pro, Descript, Runway, Pika, Kling, Luma Dream Machine, Veo/Google, HailuoAI/Minimax, Synthesia, HeyGen, InVideo AI, Opus Clip, Sora (shutting down), Kaiber, LTX Studio, Stability AI, Freebeat, Neural Frames.

---

## WHAT LUMN HAS TODAY (V6 Baseline)

**Pipeline**: 100% fal.ai for generation. Gemini 3.1 Flash for anchor stills + reference sheets, Kling 3.0 (v3 / o3, standard / pro) for image-to-video. Claude Haiku (default) + Sonnet (hero shots / escalations) for vision QA. FFmpeg for editorial conform.

**Earlier LUMN builds supported Runway (Gen4.5 / Gen4 Turbo), Grok xAI video, Luma Ray2, and Veo 3.1 as selectable engines.** V6 retires the multi-engine model in favor of a single-engine production (no engine mixing) with Kling 3.0. Legacy engine stubs remain in `lib/video_generator.py` for backward compatibility with old project files but are not used by the V6 pipeline.

- PromptOS (POS): character/costume/environment/prop/scene entity registry with approved hero ref per entity
- Preproduction sheets: multi-panel character/costume/environment/prop reference sheets via Gemini 3.1 Flash
- Anchor generation: Gemini edit-mode with up to ~10 reference images per shot, camera-only prompts
- Video generation: Kling 3.0 image-to-video, 3-15s (V3 Standard / O3) or 5-10s (V3 Pro), native character element binding
- 5D transition judge + strategy engine: motivated_cut / direct_animate / end_variants / bridge_frame / regenerate_pair with fallback chain
- 14 structured prompt packs (Gemini + Kling + Claude templates)
- Self-healing learning system: per-shot attempt log, failure clustering, rule updates
- 7-stage stepper workflow: Brief -> Drafts & Refs -> Assets -> Scenes -> Shots -> Render -> Output
- Audio analysis (BPM, beats, energy curve, section detection via librosa)
- Auto scene planning from audio structure (intro/verse/chorus/bridge/outro)
- 19 editorial transition types assigned per cut by the Transition Intelligence system
- Per-shot color grade presets (none/warm/cold/vintage/high-contrast/noir/cyberpunk/sepia)
- Beat-sync cuts aligned to audio beats
- Storyboard generation
- Movie planner with bible (characters, costumes, environments, props, style locks, world rules)
- Prompt assistant (style presets, AI prompt enhancement)
- Audio ducking, audio crossfades, vocal overlay per scene
- Lyrics overlay on video
- Platform export presets (YouTube, Instagram, TikTok)
- GIF export
- Project save/load system
- Cost tracking per project
- Credits sequence generation
- Watermark system
- Thumbnail extraction
- Dark/light theme (cinematic Inter Tight / muted rgba / backdrop-blur aesthetic)
- Web-based UI (localhost, port 3849)

---

## FEATURE GAP ANALYSIS

### TIER 1 -- HIGH IMPACT, REALISTIC TO IMPLEMENT (Priority)

| # | Feature | Description | Who Has It | LUMN Status |
|---|---------|-------------|-----------|-------------|
| 1 | **Native Audio-Visual Generation** | Generate video WITH synchronized audio (dialogue, SFX, ambient) in a single pass instead of separate video + audio overlay | Kling 3.0, Veo 3, Seedance 2.0 | NO -- We overlay audio post-generation. Could integrate Kling 3.0/Veo 3 native audio modes via API |
| 2 | **Lip Sync Video Generation** | Generate characters speaking with accurate lip sync from audio input or text dialogue | Kling 3.0, Veo 3, HeyGen, Synthesia, LTX Studio, Freebeat | NO -- Huge gap. LTX has Audio-to-Video for lip-synced talking. HeyGen/Synthesia are leaders here |
| 3 | **Keyframe Transition (Start/End Frame)** | Upload a start image and end image, AI generates the video transition between them | Pika (Pikaframes), Luma (Ray3), Veo 3.1 | PARTIAL -- We have first/last frame extraction for scene continuity but no start+end frame generation mode |
| 4 | **Multi-Model Intelligent Routing** | AI automatically selects the best generation model per scene based on content type (action=Kling, cinematic=Runway, etc.) | Freebeat, LTX Studio, Neural Frames | NO -- User manually picks engine per scene. Should auto-recommend based on scene content |
| 5 | **Audio Stem Separation + Per-Stem Visual Reactivity** | Separate audio into stems (drums, bass, vocals, melody) and map different visual behaviors to each | Neural Frames, Freebeat | NO -- We analyze energy curve and beats but do not separate stems. librosa or Demucs could do this |
| 6 | **In-Video Post-Gen Editing** | After generating a video clip, modify it with text prompts ("add rain", "change lighting") without regenerating from scratch | Runway (Aleph) | NO -- We regenerate entire clips. Could integrate Aleph API for post-gen edits |
| 7 | **Auto-Captioning / Subtitle Generation** | AI-powered automatic transcription and caption/subtitle overlay with styling | CapCut, Premiere, DaVinci, Descript, Final Cut Pro | PARTIAL -- We have lyrics overlay but no auto-transcription or auto-caption from audio |
| 8 | **AI Voice Clone / Text-to-Speech** | Clone a voice from audio sample, or generate voiceover from text, with natural intonation | Descript (Overdub), Synthesia, HeyGen, InVideo AI | NO -- We have no TTS or voice clone. Could add for narration/voiceover scenes |
| 9 | **Object/Style Swap in Generated Video** | Swap objects, change styles, add elements to existing video using text or reference image | Pika (Pikaswaps, Pikadditions), Runway (Aleph) | NO -- Would need Pika or Aleph API integration |
| 10 | **Performance-Driven Video (Motion Capture)** | Upload a video of a person performing, transfer their expressions/movements to an AI character | Runway (Act-Two), Luma (Ray3 Modify) | NO -- Would be powerful for music video performances |
| 11 | **Multi-Shot Generation** | Generate a coherent sequence with multiple camera cuts in a single generation call | Kling 3.0 (up to 6 cuts) | NO -- We generate one clip per scene then stitch. Kling 3.0 can do multi-shot natively |
| 12 | **Suno/Music AI Integration** | Paste a Suno link, auto-extract audio, analyze structure, generate video | Freebeat | NO -- Easy win. Accept Suno URL, download audio, feed into our pipeline |
| 13 | **Album Cover / Visual Branding Generator** | Generate album artwork, Spotify Canvas loops, social thumbnails matched to video style | Freebeat | NO -- We extract thumbnails but do not generate original artwork matched to style |

### TIER 2 -- MEDIUM IMPACT, MODERATE EFFORT

| # | Feature | Description | Who Has It | LUMN Status |
|---|---------|-------------|-----------|-------------|
| 14 | **AI Green Screen / Background Removal** | Remove or replace video backgrounds without physical green screen | CapCut, Descript, Premiere | NO |
| 15 | **AI Eye Contact Correction** | Adjust gaze in talking-head video so subject appears to look at camera | Descript, HeyGen | NO |
| 16 | **Multi-Language Auto Translation** | Automatically translate and dub video into 27-80+ languages with lip sync | Premiere (27 langs), Synthesia (80+ langs), HeyGen, InVideo AI (50+ langs) | NO |
| 17 | **Clip Trimming / In-Out Points** | Set precise in/out points per generated clip to use only the best portion | CapCut, Premiere, DaVinci, Final Cut Pro, all editors | NO (on roadmap as V2 item 12) |
| 18 | **Text-Based Video Editing** | Edit video by editing a transcript -- delete text to delete video segments | Descript (core feature) | NO -- Novel paradigm, not typical for music video workflow but powerful |
| 19 | **AI Object Masking + Tracking** | Click an object, AI creates mask that tracks it through the scene for effects/color | Premiere (AI Object Mask), DaVinci (Magic Mask), Final Cut Pro | NO |
| 20 | **AI Cinematic Haze / Atmosphere** | Add fog, haze, atmospheric effects using AI depth estimation | DaVinci Resolve 20.2 | NO -- Could add via ffmpeg depth-based effects |
| 21 | **Director Camera Controls** | Precise camera direction (pan, tilt, zoom, orbit, tracking) via UI controls, not just prompt text | Runway (Multi-Motion Brush), Hailuo (Director Model), Kling 3.0 | PARTIAL -- We have camera presets in prompts but no visual camera control UI |
| 22 | **Video Extend / Scene Extension** | Extend a generated clip beyond its initial length by generating continuation | Luma (Extend), Veo 3.1 (Scene Extension), CapCut | NO -- We regenerate at set durations. Could use extend APIs for longer clips |
| 23 | **Virality Score / Content Optimization** | AI scores generated content for social media engagement potential | Opus Clip (AI Virality Score) | NO |
| 24 | **Brand Kit / Template System** | Save brand colors, fonts, logo placement, intro/outro as reusable templates | Opus Clip, InVideo AI, CapCut Pro | PARTIAL -- We have style presets but no full brand kit with logo/font/color persistence |
| 25 | **Real-Time Preview / Proxy Editing** | Low-res preview playback during editing, full-res on export | DaVinci, Premiere, Final Cut Pro | NO (on roadmap as V2 item 35) |
| 26 | **A-Roll / B-Roll Shot Classification** | Auto-classify and balance between main subject shots and cutaway/environment shots | Freebeat, LTX Studio | PARTIAL -- Scene planner varies shots by section but no explicit A-roll/B-roll logic |
| 27 | **Collaborative Editing** | Multiple users editing same project simultaneously | DaVinci (Blackmagic Cloud), Premiere (Frame.io), CapCut Pro | NO -- Single user local app |

### TIER 3 -- NICE TO HAVE / FUTURE VISION

| # | Feature | Description | Who Has It | LUMN Status |
|---|---------|-------------|-----------|-------------|
| 28 | **AI Avatar / Digital Human** | Photorealistic AI-generated spokesperson that speaks and gestures naturally | Synthesia (240+ avatars), HeyGen (Avatar IV) | NO -- Different use case but could add AI presenter for intro/outro |
| 29 | **Interactive Video Agents** | AI avatar pauses and responds to viewer input in real-time | Synthesia (Video Agents, Enterprise) | NO -- Future concept |
| 30 | **4K Native Output** | Native 4K resolution generation and export | Veo 3.1 (native 4K), LTX Studio, CapCut Pro | PARTIAL -- Depends on engine. Our stitching supports it but most gen models output 1080p |
| 31 | **3D / Volumetric Video** | 3D novel view synthesis, 4D generation from video | Stability AI (SV3D, SV4D 2.0), Luma (3D capture) | NO -- Niche but emerging |
| 32 | **Music-Reactive Abstract Visuals** | Deep audio-stem mapping to abstract/generative visual patterns for electronic music | Neural Frames (10+ modulation params, 8 stems) | NO -- Our visuals are scene-based, not parameter-driven abstract |
| 33 | **AI Script Writer / Copilot** | AI writes full video script from minimal input, suggests visual elements | Synthesia (Copilot), InVideo AI | PARTIAL -- Prompt assistant enhances prompts but no full script generation from concept |
| 34 | **Dance/Choreography Video** | AI-generated choreography synced to music | Freebeat (Dance Video mode) | NO |
| 35 | **Lyric Video Specialist Mode** | Dedicated animated lyrics mode with typography and motion design | Freebeat, Kaiber | PARTIAL -- We have lyrics overlay but not animated/styled lyric video mode |
| 36 | **Stock Asset Library Integration** | Search and use millions of stock video/photo/music/SFX assets in-editor | InVideo AI (16M+ assets), Premiere (Adobe Stock), CapCut (12M+ assets) | NO |
| 37 | **Chroma Key / Green Screen** | Real-time chroma key compositing for uploaded footage | CapCut, DaVinci, Premiere, Final Cut | NO |
| 38 | **Audio Mixing Console** | Full multi-track audio mixer with EQ, dynamics, panning, loudness metering | DaVinci (Fairlight), Premiere, Final Cut | NO -- We do basic audio overlay and ducking |
| 39 | **AI Noise Reduction / Voice Enhancement** | AI-powered audio cleanup for uploaded narration/dialogue | Premiere, DaVinci, Descript (Studio Sound) | NO |
| 40 | **Video-to-Video Style Transfer** | Upload existing video, AI re-renders it in a new style while preserving motion | Kaiber (Transform mode), Runway (Aleph), Pika | NO -- Powerful for remixing existing footage |

---

## TOP COMPETITOR KILLER FEATURES (What users love most)

| Platform | Killer Feature | Why Users Love It |
|----------|---------------|-------------------|
| **CapCut** | Auto-captions + one-tap editing | Zero learning curve, viral-ready in minutes |
| **DaVinci Resolve** | Color grading + Fairlight audio | Hollywood-grade tools, free |
| **Premiere Pro** | AI Object Mask + Generative Extend | Edit like magic, extend clips with AI |
| **Descript** | Edit video by editing text transcript | Revolutionary paradigm shift in editing |
| **Runway** | Aleph (in-video editing) + Act-Two (performance transfer) | Post-gen editing without regenerating |
| **Pika** | Pikaframes (start/end keyframes) + Pikaswaps (object swap) | Creative control over transitions and elements |
| **Kling 3.0** | Native audio-visual + multi-shot + 3min videos | Full scenes with dialogue in one generation |
| **Luma** | Ray3 cinematic motion + Modify tool | Most natural camera movement |
| **Veo 3.1** | Native 4K + audio + dialogue lip sync | Highest fidelity all-in-one generation |
| **Freebeat** | Full music-first pipeline with 4-level audio analysis | Only tool purpose-built for music videos with deep audio understanding |
| **Neural Frames** | 8-stem audio reactivity with per-stem visual control | Electronic musicians get visuals that truly match their music |
| **Kaiber** | Audio-reactive animation modes (Flipbook/Motion/Transform) | Artists get stylized, music-synced visuals easily |
| **LTX Studio** | Trained Actors + storyboard-to-video + multi-model | Full film production pipeline in one tool |
| **Synthesia** | 240+ avatars in 160+ languages with gestures | Corporate video creation without cameras |
| **HeyGen** | Avatar IV micro-expressions + phone app | Realistic spokesperson from anywhere |
| **InVideo AI** | Text prompt to complete video with Sora 2 + Veo 3.1 | Zero effort, paste text, get video |
| **Opus Clip** | AI Virality Score + auto-repurposing | Turn one video into 25 social clips instantly |
| **Descript** | Overdub voice clone | Fix spoken mistakes by typing corrections |

---

## PRIORITY RECOMMENDATIONS FOR LUMN STUDIO

### IMMEDIATE (Next Sprint) -- Biggest Bang for Buck

1. **Suno URL Integration** -- Accept Suno links, auto-download and analyze. Trivial to implement, huge UX win for our target users.

2. **Audio Stem Separation** -- Use Demucs or similar to split drums/bass/vocals/melody. Feed stem energy into scene planning for smarter visual-to-audio mapping. Transforms our beat sync from "cuts on beats" to "visuals react to specific instruments."

3. **Multi-Model Auto-Router** -- When user clicks Generate, AI recommends the best engine per scene: Kling for action/dialogue, Runway for cinematic/stylistic, Veo for photorealistic/4K. Can be a simple rules engine initially.

4. **Keyframe Transition Mode** -- Add start+end frame generation via Pika Pikaframes API or Luma Ray3 API. Upload two images, get smooth video transition.

### SHORT-TERM (Next Month)

5. **Aleph Integration for Post-Gen Edits** -- After generating a clip, let users modify it with text prompts via Runway Aleph API. "Add rain", "change to night", "make more dramatic lighting."

6. **Lip Sync / Dialogue Scene Type** -- Add a "Talking" scene type that uses Kling 3.0 or LTX Audio-to-Video to generate lip-synced characters from dialogue audio.

7. **Native Audio Generation Toggle** -- When using Kling 3.0 or Veo 3, offer option to generate with native audio (SFX, ambient, dialogue) instead of silent clips.

8. **In-App Clip Trimming** -- Add in/out point controls per clip. Already on V2 roadmap, should be prioritized since every competitor has this.

### MEDIUM-TERM (Next Quarter)

9. **Performance Transfer** -- Integrate Runway Act-Two: upload webcam video of artist performing, transfer to AI character for authentic music video performances.

10. **AI Voice / TTS for Narration Scenes** -- Add text-to-speech for narration intros/outros. Could use ElevenLabs or similar API.

11. **Enhanced Lyric Video Mode** -- Dedicated animated typography mode for lyric videos with motion design presets (typewriter, kinetic, glitch text).

12. **Visual Branding Package** -- Generate album art, Spotify Canvas, social thumbnails all matched to the video's style palette. One-click release package.

### NOT RECOMMENDED (Wrong direction for LUMN)

- AI Avatars/Spokespersons (Synthesia/HeyGen territory, different product)
- Stock asset library (we generate, not curate)
- Collaborative editing (premature, we are single-user desktop)
- Interactive video agents (enterprise niche)
- Full audio mixing console (not our lane, users have DAWs)

---

## BIGGEST COMPETITIVE THREAT: Freebeat

Freebeat is the closest direct competitor to LUMN Studio. They are purpose-built for music videos with:
- 4-level audio analysis (BPM, beats, bars, song structure) -- comparable to ours
- 90%+ lip sync accuracy
- Multi-model routing (Pika, Kling, Veo, Runway)
- Character consistency across cuts
- Suno integration
- Album cover + Spotify Canvas generation
- Performance and Storytelling modes
- 1 billion seconds of content generated

**Where LUMN beats Freebeat:**
- Our Prompt OS system (character/costume/environment/prop sheets) is more detailed
- Movie planner with full production bible
- Auto Director with workflow presets
- More granular per-scene control
- Self-hosted / local-first (no vendor lock-in)
- Open architecture (user owns everything)

**Where Freebeat beats LUMN:**
- Multi-model auto-routing
- Lip sync
- Suno integration
- Album art generation
- More polished onboarding
- Cloud-based (accessible anywhere)

The gap is closable. Our architecture is more flexible. We need features 1-4 from the immediate list above to achieve parity, then our depth advantages take over.

---

## Sources

### Video Editors
- [CapCut Features 2025](https://www.capcut.com/explore/latest-2025-capcut)
- [DaVinci Resolve 20 Review](https://filmora.wondershare.com/video-editor-review/davinci-resolve-editing-software.html)
- [DaVinci Resolve What's New](https://www.blackmagicdesign.com/products/davinciresolve/whatsnew)
- [Adobe Premiere Pro AI Features 2025](https://blog.adobe.com/en/publish/2025/04/02/introducing-new-ai-powered-features-workflow-enhancements-premiere-pro-after-effects)
- [Adobe Premiere 26 Features](https://blog.adobe.com/en/publish/2026/01/20/new-ai-powered-video-editing-tools-premiere-major-motion-design-upgrades-after-effects)
- [Final Cut Pro AI Features](https://www.editorskeys.com/blogs/news/final-cut-pros-new-ai-features-a-complete-guide-for-faster-editing)
- [Final Cut Pro 12](https://www.redsharknews.com/final-cut-pro-12-creator-studio-integration)
- [Descript Review 2025](https://aitoolanalysis.com/descript-review-2025-text-based-video-editing/)
- [Descript Review 2026](https://www.vidmetoo.com/descript-review/)

### AI Video Generation
- [Runway Gen-4 Turbo Review](https://aitoolsguide.in/runway-gen-4-turbo-review/)
- [Runway Gen-4.5](https://www.datacamp.com/tutorial/runway-gen-4-5)
- [Runway Aleph](https://runwayml.com/research/introducing-runway-aleph)
- [Pika 2.2 Features](https://www.imagine.art/features/pika-2-2)
- [Kling 3.0 Guide](https://kling3.org/blog/kling-3-0-ai-video-generator-complete-guide)
- [Kling 2.6 Audio-Visual](https://ir.kuaishou.com/news-releases/news-release-details/kling-ai-launches-video-26-model-simultaneous-audio-visual)
- [Luma AI Review 2026](https://www.goenhance.ai/blog/luma-ai-review)
- [Luma Ray3 Modify](https://lumalabs.ai/press/luma-ai-announces-ray3-modify)
- [Google Veo 3.1 Features](https://developers.googleblog.com/introducing-veo-3-1-and-new-creative-capabilities-in-the-gemini-api/)
- [Veo 3.1 4K Update](https://wavespeed.ai/blog/posts/google-veo-3-1-4k-update-brings-professional-grade-ai-video-generation/)
- [HailuoAI Hailuo 2.3](https://www.minimax.io/news/minimax-hailuo-23)
- [Synthesia Features](https://www.synthesia.io/features)
- [Synthesia 3.0](https://www.synthesia.io/post/synthesia-3-0-the-next-era-of-video)
- [HeyGen Review 2026](https://bigvu.tv/blog/heygen-ai-avatar-video-generator-complete-review-2026-best-ai-video-generation-tool/)
- [InVideo AI Features](https://ampifire.com/blog/invideo-ai-features-pricing-what-can-this-text-to-video-generator-do/)
- [OpusClip Review](https://www.opus.pro/blog/best-video-repurposing-tools)
- [Sora 2 Guide](https://wavespeed.ai/blog/posts/openai-sora-2-complete-guide-2026/)
- [Stability AI Stable Video](https://stability.ai/stable-video)

### AI Music Video Specialists
- [Freebeat AI](https://freebeat.ai/)
- [Best AI Music Video Generators 2026](https://www.spacewar.com/reports/6_Best_AI_Music_Video_Generators_in_2026_Which_One_Actually_Understands_the_Music_999.html)
- [Neural Frames](https://www.neuralframes.com/ai-music-video-generator)
- [Kaiber Superstudio](https://www.kaiber.ai/superstudio)
- [LTX Studio Features 2026](https://ltx.studio/blog/top-ltx-studio-features)

### AI Film Production
- [LTX Studio AI Movie Maker](https://ltx.studio/platform/ai-movie-maker)
- [AI Video Generators Comparison 2026](https://wavespeed.ai/blog/posts/best-ai-video-generators-2026/)
- [AI Video APIs Guide 2026](https://wavespeed.ai/blog/posts/complete-guide-ai-video-apis-2026/)
- [State of AI Video Feb 2026](https://medium.com/@cliprise/the-state-of-ai-video-generation-in-february-2026-every-major-model-analyzed-6dbfedbe3a5c)
