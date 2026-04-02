# Runway API Reference (Deduplicated)
_Saved 2026-04-02 from docs.dev.runwayml.com/api_

## Key Facts
- API Version Header: `X-Runway-Version: 2024-11-06`
- Auth: `Authorization: Bearer <RUNWAY_API_KEY>`
- Base URL: `https://api.dev.runwayml.com/v1`
- All endpoints support `contentModeration.publicFigureThreshold`: "auto" | "low"
- `text_to_video` has NO image input — pure text only
- `image_to_video` promptImage is ALWAYS the first frame (position: "first")
- `referenceImages` with `tag` + @mention is for `text_to_image` ONLY (not video)
- `video_to_video` (gen4_aleph) supports `references` array with `{type:"image", uri:"..."}` for style/content emulation

## Video Models (Available April 2026)
| Model | Endpoint | Credits/sec |
|-------|----------|-------------|
| gen4.5 | text_to_video, image_to_video | 12 |
| gen4_turbo | image_to_video only | 5 |
| gen3a_turbo | image_to_video only | (legacy) |
| veo3 | text_to_video, image_to_video | 40 |
| veo3.1 | text_to_video, image_to_video | 40 |
| veo3.1_fast | text_to_video, image_to_video | 15 |
| gen4_aleph | video_to_video only | 15 |
| act_two | character_performance only | 5 |

## Image Models
| Model | Credits |
|-------|---------|
| gen4_image | 5-8/image |
| gen4_image_turbo | 2/image |
| gemini_2.5_flash | 5/image |

---

## POST /v1/text_to_video
Generate video from text prompt only.

**Models:** gen4.5, veo3.1, veo3.1_fast, veo3

| Param | Type | Required | Notes |
|-------|------|----------|-------|
| model | string | yes | gen4.5, veo3.1, veo3.1_fast, veo3 |
| promptText | string[1..1000] | yes | |
| ratio | string | yes | "1280:720" or "720:1280" ONLY |
| duration | int[2..10] | yes | |
| seed | int[0..4294967295] | no | |
| contentModeration.publicFigureThreshold | string | no | "auto" or "low" |

---

## POST /v1/image_to_video
Generate video from image (image = first frame).

**Models:** gen4.5, gen4_turbo, gen3a_turbo, veo3.1, veo3.1_fast, veo3

| Param | Type | Required | Notes |
|-------|------|----------|-------|
| model | string | yes | |
| promptText | string[1..1000] | yes | |
| promptImage | string or PromptImages[] | yes | HTTPS URL, runway:// URI, or data:image/* URI. Position is always "first" |
| ratio | string | yes | "1280:720", "720:1280", "1104:832", "960:960", "832:1104", "1584:672" |
| duration | int[2..10] | yes | |
| seed | int[0..4294967295] | no | |
| contentModeration.publicFigureThreshold | string | no | "auto" or "low" |

**PromptImages array format:** `[{uri: "...", position: "first"}]` (exactly 1 item)

---

## POST /v1/video_to_video
Transform existing video with prompt + optional image reference.

**Models:** gen4_aleph ONLY

| Param | Type | Required | Notes |
|-------|------|----------|-------|
| model | string | yes | Must be "gen4_aleph" |
| videoUri | string | yes | HTTPS URL, runway:// URI, or data:video/* |
| promptText | string[1..1000] | yes | |
| seed | int | no | |
| references | ImageReference[] | no | Up to 1. `{type:"image", uri:"..."}` — emulates style/content |
| contentModeration.publicFigureThreshold | string | no | |

---

## POST /v1/text_to_image
Generate images from text + reference images.

**Models:** gen4_image_turbo, gen4_image, gemini_2.5_flash

| Param | Type | Required | Notes |
|-------|------|----------|-------|
| model | string | yes | |
| promptText | string[1..1000] | yes | |
| ratio | string | yes | Many options including 1920:1080, 1280:720, etc. |
| referenceImages | object[1..3] | yes | `{uri:"...", tag:"MyTag"}` — use @MyTag in promptText |
| seed | int | no | |
| contentModeration.publicFigureThreshold | string | no | |

---

## POST /v1/character_performance
Control character facial/body performance from reference video.

**Models:** act_two ONLY

| Param | Type | Required | Notes |
|-------|------|----------|-------|
| model | string | yes | Must be "act_two" |
| character | CharacterImage or CharacterVideo | yes | `{type:"image", uri:"..."}` or `{type:"video", uri:"..."}` |
| reference | CharacterReferenceVideo | yes | `{type:"video", uri:"..."}` — 3-30 seconds |
| seed | int | no | |
| bodyControl | boolean | no | Enable non-facial movements |
| expressionIntensity | int[1..5] | no | Default: 3 |
| ratio | string | no | |
| contentModeration.publicFigureThreshold | string | no | |

---

## POST /v1/sound_effect
**Model:** eleven_text_to_sound_v2
- promptText: string[1..3000]
- duration: number[0.5..30] (optional)
- loop: boolean (default false)

## POST /v1/text_to_speech
**Model:** eleven_multilingual_v2
- promptText: string[1..1000]
- voice: `{type:"runway-preset", presetId:"Leslie"}` (49 presets available)

## POST /v1/speech_to_speech
**Model:** eleven_multilingual_sts_v2
- media: `{type:"audio"|"video", uri:"..."}` 
- voice: `{type:"runway-preset", presetId:"..."}`
- removeBackgroundNoise: boolean

## POST /v1/voice_dubbing
**Model:** eleven_voice_dubbing
- audioUri: string
- targetLang: string (28 languages: en, hi, pt, zh, es, fr, de, ja, ar, ru, ko, id, it, nl, tr, pl, sv, fil, ms, ro, uk, el, cs, da, fi, bg, hr, sk, ta)
- disableVoiceCloning: boolean
- dropBackgroundAudio: boolean
- numSpeakers: integer

## POST /v1/voice_isolation
**Model:** eleven_voice_isolation
- audioUri: string (duration 4.6s - 3600s)

---

## Task Management

### GET /v1/tasks/{id}
Poll no more than every 5 seconds. Statuses: PENDING, THROTTLED, RUNNING, SUCCEEDED, FAILED

### DELETE /v1/tasks/{id}
Cancel running/pending/throttled tasks, or delete completed ones.

---

## Uploads

### POST /v1/uploads
- filename: string[3..255] (valid extension required)
- type: "ephemeral"
- Returns: `{uploadUrl, fields, runwayUri}` — use runwayUri in generation requests

---

## Workflows

### GET /v1/workflows — List published workflows
### POST /v1/workflows/{id} — Run workflow with optional nodeOutputs overrides
### GET /v1/workflows/{id} — Get workflow graph schema
### GET /v1/workflow_invocations/{id} — Poll workflow status

---

## Avatars

### POST /v1/avatars — Create (name, referenceImage, personality, voice)
### GET /v1/avatars — List with pagination
### GET /v1/avatars/{id} — Get details
### PATCH /v1/avatars/{id} — Update
### DELETE /v1/avatars/{id} — Delete
### GET /v1/avatars/{id}/conversations — List conversations
### GET /v1/avatars/{id}/conversations/{conversationId} — Get conversation

Voice presets (avatars): victoria, vincent, clara, drew, skye, max, morgan, felix, mia, marcus, summer, ruby, aurora, jasper, leo, adrian, nina, emma, blake, david, maya, nathan, sam, georgia, petra, adam, zach, violet, roman, luna

---

## Realtime Sessions

### POST /v1/realtime_sessions — Create (model: gwm1_avatars, avatar, tools, maxDuration)
### GET /v1/realtime_sessions/{id} — Get status
### DELETE /v1/realtime_sessions/{id} — Cancel

Supports ClientEventTool and BackendRPCTool with typed parameters.

---

## Knowledge Documents

### POST /v1/documents — Create (name, content up to 200K chars)
### GET /v1/documents — List with pagination  
### GET /v1/documents/{id} — Get with content
### PATCH /v1/documents/{id} — Update name/content
### DELETE /v1/documents/{id} — Delete (removes from all avatars)

---

## Voices

### POST /v1/voices — Create from text description (model: eleven_multilingual_ttv_v2 or eleven_ttv_v3)
### GET /v1/voices — List with pagination
### GET /v1/voices/{id} — Get details
### DELETE /v1/voices/{id} — Delete
### POST /v1/voices/preview — Preview before creating

---

## Organization

### GET /v1/organization — Credit balance + tier info
### POST /v1/organization/usage — Query credit usage by date range (up to 90 days)

Voice presets (TTS/STS): Maya, Arjun, Serene, Bernard, Billy, Mark, Clint, Mabel, Chad, Leslie, Eleanor, Elias, Elliot, Grungle, Brodie, Sandra, Kirk, Kylie, Lara, Lisa, Malachi, Marlene, Martin, Miriam, Monster, Paula, Pip, Rusty, Ragnar, Xylar, Maggie, Jack, Katie, Noah, James, Rina, Ella, Mariah, Frank, Claudia, Niki, Vincent, Kendrick, Myrna, Tom, Wanda, Benjamin, Kiana, Rachel
