# ROADMAP V2 — 50 New Items + Tweaks to Existing Features

## STATUS: V1 Roadmap (50 items) — 28 completed, 22 remaining need UI wiring

---

## TWEAKS TO EXISTING FEATURES

### Generation Pipeline
T1. When Runway moderation rejects a prompt, auto-rephrase and retry (strip flagged words, soften language)
T2. Show estimated credit cost BEFORE clicking Generate (per scene based on engine + duration)
T3. Per-scene engine override should show in the scene card as a colored badge (not just dropdown)
T4. Add "Regenerate with different seed" button — same prompt, different result
T5. Progress bar should show actual Runway/Grok polling status with time elapsed
T6. When generation fails, show the EXACT error message in the UI (not generic "failed")
T7. Allow setting aspect ratio per scene (not just global)
T8. Cache generated clips — if prompt hasn't changed, don't regenerate on "Generate All"

### Scene Editor
T9. Scene cards should show a timestamp range (0:00-0:08, 0:08-0:16, etc.) not just duration
T10. Double-click a scene card to expand/collapse (currently only click header)
T11. Show total video duration sum at the bottom of the scene list
T12. Batch select scenes (checkboxes) for bulk delete/regenerate/change engine
T13. Copy/duplicate a scene (clone with same prompt, photo, settings)
T14. Scene numbering should auto-update when reordering
T15. Add scene notes field (internal notes that don't affect generation)

### Photo + Reference System
T16. Thumbnail preview of uploaded photo should be larger (at least 150x150)
T17. Show photo filename under the thumbnail
T18. Allow replacing a photo without deleting the scene
T19. Drag-and-drop photo directly onto a scene card
T20. When photo is uploaded, auto-suggest a prompt based on the image content

---

## NEW ROADMAP V2 — 50 ITEMS

### AI & Smart Features (1-10)
1. **Prompt rewriter** — AI rewrites vague prompts into detailed cinematic descriptions using Grok text API
2. **Scene continuity AI** — after generating scene N, analyze its last frame and use it to inform scene N+1's prompt for visual continuity
3. **Auto-scene generator** — given a song + style, auto-create ALL scenes with varied prompts (fully automated pipeline)
4. **Mood board to prompt** — upload 3-5 inspiration images, AI extracts common visual themes into a style prompt
5. **Prompt A/B testing** — generate same scene with 2 different prompts side by side, pick the winner
6. **Smart duration** — auto-set scene durations based on audio energy (high energy = shorter scenes, low = longer)
7. **Reference video analysis** — upload a music video you like, AI extracts the editing style (cut frequency, camera movements, color palette)
8. **Auto color match** — after generating all clips, analyze color palettes and auto-grade them to match each other
9. **Prompt library** — searchable library of proven prompts categorized by genre/mood/style
10. **Scene suggestion from lyrics** — paste lyrics per scene, AI generates visual prompts from the words

### Editing & Post-Production (11-20)
11. **Waveform timeline** — show audio waveform under the scene cards so you can see beats visually
12. **Trim clips** — set in/out points per clip to use only part of the generated video
13. **Crossfade duration per transition** — currently global, make it per-scene (0.1s to 2.0s)
14. **Fade to/from color** — fade to white, red, or any color (not just black)
15. **Ken Burns on PHOTOS only** — separate button to add gentle motion to still photos (for title cards etc.)
16. **Lower thirds** — pre-designed text overlay templates for artist name, song title
17. **Animated titles** — intro title card with animated text (fade in, typewriter, glitch effect)
18. **Letterbox/cinematic bars** — add black bars top/bottom for widescreen cinematic look
19. **Frame interpolation** — double the framerate of generated clips for smoother motion (24fps → 48fps via ffmpeg minterpolate)
20. **Video stabilization** — apply ffmpeg vidstab to shaky generated clips

### Audio (21-30)
21. **Waveform visualization in scene cards** — show mini audio waveform for the time range of each scene
22. **Auto-detect song sections** — chorus/verse/bridge badges on scene cards based on audio analysis
23. **Audio fade per scene** — individual audio fade in/out per scene during stitch
24. **Silence detection** — auto-mark silent sections and suggest removing or shortening them
25. **Audio preview per scene** — play just the audio for a scene's time range without video
26. **Multi-language subtitle support** — add subtitles in different languages (SRT file import)
27. **Voice-over recording** — record directly in the browser via microphone API
28. **Audio sync check** — after stitching, verify audio and video are in sync (detect drift)
29. **Sound effects library** — built-in SFX (whoosh, impact, transition sounds) to add between scenes
30. **Audio spectrum per scene** — show frequency analysis to help match visuals to audio character

### Export & Distribution (31-40)
31. **Render queue with priority** — queue multiple renders, set priority (high/normal/low)
32. **Auto-thumbnail from best frame** — after stitching, auto-extract the most visually striking frame
33. **Video metadata editor** — set title, artist, album, year, genre in the MP4 metadata
34. **Subtitle burn-in** — burn SRT subtitles directly into the video (not just overlay)
35. **Preview at different qualities** — quick 360p preview vs final 1080p render
36. **Export audio separately** — extract just the audio track from the final video
37. **Chapter markers** — add chapter points at scene boundaries for YouTube chapters
38. **Video compression presets** — output at different file sizes (web optimized, high quality, archive)
39. **Animated GIF preview** — auto-generate a 3-second GIF teaser of the best moment
40. **Social media caption generator** — AI-generated captions/hashtags for each platform

### UI & UX (41-50)
41. **Dark/light mode** — toggle between cyberpunk dark and clean light theme
42. **Drag scene to timeline** — visual drag from card view to a horizontal timeline
43. **Fullscreen scene preview** — click a clip to see it fullscreen with playback controls
44. **Bulk prompt editor** — edit all scene prompts in a single text area (one per line)
45. **Project dashboard** — overview page showing total scenes, duration, cost, generation status
46. **Notification system** — toast notifications for generation complete, errors, auto-save
47. **Search scenes** — filter/search scenes by prompt text, engine, status
48. **Scene grouping** — group scenes into acts/sections (Intro, Verse 1, Chorus, etc.)
49. **Responsive mobile layout** — usable on tablet/phone for reviewing on the go
50. **Plugin system** — allow custom ffmpeg filter plugins that users can add via config
