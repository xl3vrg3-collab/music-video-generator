# LUMN Studio: Final Build Specification v1.0

**Locked: 2026-04-08** | No contradictions. One authoritative version.

---

## 1. UX Diagnosis

The current workspace fails because it presents every feature on a single scrolling page with no hierarchy, no sequencing, and four competing start buttons. The user sees engine selection before they have written a creative idea. Lock controls and continuity rules appear alongside the master prompt. Preproduction, style profiling, shot pasting, and video rendering all compete for attention simultaneously.

The root cause is additive design -- each feature was appended as a new section without rethinking the overall flow. The fix is a stage-based architecture where each screen answers one question:

| Stage | Question it answers |
|-------|-------------------|
| Brief | What do you want to make? |
| Drafts + References | What does each element look like? |
| Assets | Are the generated stills correct? |
| Scenes | What happens in what order? |
| Shots | How is each moment filmed? |
| Render | Generate the video clips |
| Output | Assemble, mix, and export |

---

## 2. Information Architecture

### 7-Stage Stepper (8 logical stages, 7 top-level views)

```
BRIEF --- DRAFTS & REFS --- ASSETS --- SCENES --- SHOTS --- RENDER --- OUTPUT
  1             2               3          4         5          6          7
```

**Drafts and References are one view with two phases.** They share a single stepper position ("Drafts & Refs") but the view has two internal states: Draft Review and Reference Confirmation. This keeps the reference checkpoint mandatory while avoiding the feeling of two bureaucratic form-fill pages in sequence. The user sees their AI-drafted assets, uploads references inline on the same cards, and proceeds -- one screen, one flow, two concerns handled.

### Authoritative placement of every contested system:

| System | Final Placement | Rationale |
|--------|----------------|-----------|
| **Style Profile / Taste** | **Assets stage**, collapsible panel below the asset grid | Taste informs how stills are generated. It is irrelevant before assets exist and too early in Brief. It belongs right where generation happens. |
| **Continuity Rules** | **Render stage > Advanced** | Continuity enforcement is a render-time constraint, not a creative input. |
| **Creative Controls** (pacing, intensity, arc) | **Shots stage**, collapsible "Director Controls" section | These shape how scenes expand into shots -- they control cinematic rhythm, not render settings. |
| **Director Mode** (paste/import shots) | **Shots stage**, collapsible "Import Shots" section | Manual shot entry is a power-user shot tool, not a project entry point. |
| **Engine / Model / Quality / Seed / Locks** | **Render stage** | All technical generation controls live together at render time. |
| **Music upload / Suno** | **Brief stage** (optional input) AND **Output > Audio tab** (mixing/timing) | Upload/import in Brief because music informs the AI draft. Mix/sync in Output because that is post-render assembly. |
| **Output / Edit / Audio** | **Output stage with 3 internal tabs: Assembly, Audio, Export** | Keeps Output clean. Assembly = timeline/clips/transitions. Audio = music/voice/SFX mixing. Export = format/platform/download. |

---

## 3. Stage-by-Stage Workflow

### Stage 1: Brief

**Purpose:** Capture the creative idea. Let AI draft the full project.

**What the user sees:**
- Project type selector (Music Video / Short Film / Trailer / Cinematic / Brand Film)
- Large master prompt textarea
- Collapsible optional sections:
  - Music & Audio (song upload, Suno import/generate, lyrics)
  - Story Details (storyline, concept notes)
  - Visual Direction (style description, world setting)
  - Reference Photos / Mood Board (general inspiration images)

**What the user does NOT see:** Engine settings, lock controls, continuity rules, taste sliders, creative controls, shot tools.

**Primary CTA:** **"Generate Project Draft"**

**Result:** AI returns structured draft: logline, theme, tone, characters, costumes, environments, props, rough scene list. Auto-advances to Drafts & Refs.

**Feel:** Full page. This is the creative starting point. It should feel expansive and inviting -- a blank canvas with optional depth, not a form.

---

### Stage 2: Drafts & References (one view, two phases)

**Purpose:** Review AI-drafted assets, then confirm visual sources before any image generation.

**Internal structure:** One scrollable view with a phase indicator at the top:

```
  * Review Drafts ---- o Confirm References
```

**Phase 1 -- Review Drafts:**

The user sees text-only cards for every AI-drafted entity, organized by tabs (Characters / Costumes / Environments / Props). Also: a scene draft list at the bottom showing rough scene cards.

Each asset card shows:
- Name, type badge, role
- AI-generated description (editable inline)
- Tags
- Status badge: `Drafted`
- Actions: Edit, Delete

Below the asset cards: a Scene Drafts section showing AI's rough scene breakdown (title, summary, mood, location, involved assets). These are read-only previews -- scene editing happens in the Scenes stage.

**Phase transition:** User clicks **"Confirm References"** (secondary action, advances to Phase 2 within the same view). Or: user scrolls down and reference cards appear below the draft cards, making it feel like one continuous flow.

**Phase 2 -- Confirm References:**

**HARD DESIGN RULE: No asset image is generated until the user has set a visual source for that asset. The system never auto-generates a character image from AI text alone without the user first having the chance to upload a reference photo.**

Each asset card now shows a reference confirmation section:

```
+------------------------------------------+
|  MAYA CHEN -- Protagonist                |
|  "28, athletic build, dark hair..."      |
|                                          |
|  VISUAL SOURCE                           |
|  * AI Only (generate from description)   |
|  o Upload Photo (becomes source of truth)|
|  o Hybrid (photo + AI description)       |
|                                          |
|  [Upload Photo]         Status: Ready    |
|                          [Lock Reference]|
+------------------------------------------+
```

The default mode is NOT pre-selected. The user must explicitly choose a source for each asset. Bulk action: "Set All Remaining to AI Only" for users who want to skip reference upload.

If the user uploads a photo, it appears as a thumbnail on the card. That photo becomes the `@Tag` reference image for all downstream generation of that asset.

**Primary CTA:** **"Generate Stills"** -- activates only when every drafted asset has an explicit visual source mode (`ai_only`, `upload`, or `hybrid`). If the AI draft produced zero entity assets (asset-light project types like lyric video or abstract), this CTA is replaced by **"Proceed to Scenes"** which skips both still generation and the Assets review stage, advancing directly to Scenes. The Assets stage remains reachable via the stepper if the user later wants to add assets manually.

**Feel:** This should feel like a quick checkpoint, not a full page. The drafts and references flow together as one scrollable view. If the user has no photos to upload, they click "Set All to AI Only" and proceed in seconds. If they do have photos, the upload flow is per-card, inline, instant. No modals, no separate pages.

---

### Stage 3: Assets

**Purpose:** Review and approve generated stills / canonical reference sheets.

**What the user sees:**
- Tab bar: Characters / Costumes / Environments / Props
- Grid of asset cards, each showing:
  - Generated still(s) / sheet views
  - The reference source used (AI-only indicator or uploaded photo thumbnail)
  - Status: Generating / Generated / Approved / Locked
  - Actions: Regenerate, Replace (upload replacement), Approve, Lock Asset
- Collapsible **Style Profile** panel (taste sliders, quiz) -- affects generation style
- Batch actions: "Generate Missing", "Approve All Generated"

**Source-of-truth rule:**
- **Approved** assets are the canonical downstream source of truth. Scene composition, anchor generation, and rendering all prefer approved assets.
- **Generated but unapproved** assets are provisional. They can be used downstream, but the system treats them as unstable -- any regeneration may change them.
- **Locked** assets (via "Lock Asset") cannot be accidentally changed or regenerated.

**Lock Reference vs Lock Asset** -- these are separate actions protecting different things:
- **Lock Reference** (Drafts & Refs stage): protects the input source image/mode. Prevents the reference from being swapped or overwritten. Field: `reference_locked`.
- **Lock Asset** (Assets stage): protects the approved generated output. Prevents regeneration, replacement, or status changes. Field: `status = "locked"`.

**Primary CTA:** **"Build Scenes"** -- if at least one asset is approved, advances normally. If no assets are approved but generated assets exist, shows a warning: *"No assets are approved yet. Scenes will use provisional (unapproved) assets that may change if regenerated. Continue anyway?"* The user can dismiss and proceed, or go back to approve. This is a soft warning, not a hard block.

**Feel:** Full page. This is a gallery review. Cards should be visual, with large-ish thumbnails. The Style Profile lives here because adjusting taste sliders directly affects how the next regeneration looks.

---

### Stage 4: Scenes

**Purpose:** Compose scene boards from assets. Define what happens in what order.

**What the user sees:**
- Vertical list of scene cards (reorderable via drag)
- Each card shows:
  - Scene number, title, summary (editable)
  - Mood badge, location tag
  - Involved asset chips (clickable thumbnails of characters/environments used in this scene). Chips use the best available source for each asset: approved assets show a solid green border (canonical), unapproved generated assets show a dashed yellow border (provisional). This makes it visually clear which scenes rely on provisional assets that may change.
  - Scene still (composed from asset refs, or placeholder)
  - Status: Draft / Generated / Approved
  - Actions: Edit, Swap Assets, Regenerate Still, Approve, Delete
- "Add Scene" button
- Storyboard strip at the top (horizontal scroll of scene thumbnails for overview)
- If any scene relies on provisional assets, a subtle banner appears: *"Some scenes use unapproved assets. Approve assets to lock in visual consistency."*

**Primary CTA:** **"Expand to Shots"**

**Feel:** Full page. This is storyboarding. Cards should be wide and visual, showing the scene still alongside the text.

---

### Stage 5: Shots

**Purpose:** Expand scenes into granular, filmable shots. Edit shot-level prompts.

**What the user sees:**
- Shots grouped by parent scene (accordion headers: "Scene 1: The Empty Park -- 3 shots")
- Each shot card shows:
  - Shot label (e.g., 1.1, 1.2)
  - Shot prompt (editable textarea)
  - Camera movement tag (dropdown: push-in, tracking, static, etc.)
  - Duration badge
  - Inherited reference chips (from parent scene's assets)
  - Preview thumbnail (if generated)
  - Status: Draft / Preview Generated / Approved
  - Actions: Edit, Generate Preview, Approve, Delete
- Shot type filter (Close-up / Medium / Wide / Establishing)
- Status filter (Draft / Generated / Approved)
- Stats bar (total shots, previews generated, approved, estimated runtime)
- Collapsible **Director Controls**: pacing, intensity, abstract, arc type
- Collapsible **Import Shots**: paste shot sheet, AI generate shot sheet (Director Mode)

**Secondary toolbar action:** **"Generate Previews"** -- generates still frame previews for all draft shots. Optional. For visual review only. No video.

**Primary CTA:** **"Continue to Render"** -- advances to the Render stage. Does not generate anything. If at least one shot is approved, advances normally. If no shots are approved but shots exist, shows a soft warning: *"No shots are approved yet. Render will use provisional (unapproved) shots that may change if re-expanded. Continue anyway?"* The user can dismiss and proceed.

**Shot approval rule** (mirrors the asset source-of-truth pattern):
- **Approved** shots are the canonical render source. Render uses their prompts, camera tags, and anchor refs as-is.
- **Unapproved** shots (Draft or Preview Generated) are provisional. Render can use them, but they are unstable -- re-expanding scenes may overwrite them.
- Approving a shot signals: "this prompt and framing are final, render it."

**Why this design:** "Generate Previews" is a tool, not a gate. Some users want to see stills before committing to expensive video renders. Others know what they want and skip straight to Render. Both paths are valid. "Continue to Render" is a navigation action, not a generation action. This prevents the user from confusing "queue shot previews" (stills, cheap) with "render final clips" (video, expensive). The generation actions live in their respective contexts: preview stills in Shots, video clips in Render.

**Feel:** Full page. This is the most detail-heavy stage. It should feel like a shot list in a production binder -- dense but organized.

---

### Stage 6: Render

**Purpose:** Configure technical settings. Generate final video clips.

**What the user sees:**
- Render summary at top: X shots queued (Y approved, Z provisional), estimated cost, estimated time
- **Engine & Quality** section:
  - Video Engine select (Gen 4.5, Gen 4 Turbo, Veo 3, Veo 3.1, Luma, Grok)
  - Story AI Model select
  - Quality preset (Draft / Standard / Premium)
- Collapsible **Consistency Controls**:
  - Character lock slider
  - Environment lock slider
  - Style lock slider
  - Seed input
- Collapsible **Continuity Rules**:
  - Rule list + add rule input
- Collapsible **Advanced**:
  - Universal prompt suffix
  - Budget cap
- **Render Queue**: list of shots with per-shot status (queued / rendering / complete / failed / retry)
- Progress bar and cost tracker

**Primary CTA:** **"Render Final Clips"** -- starts video generation for all queued shots. This is the only button in the entire app that starts expensive video generation. There is no ambiguity about what it does or what it costs. The render queue includes both approved and provisional shots. Provisional shots are marked with a dashed yellow indicator in the queue list so the user can see which ones are not yet locked down.

**Feel:** Compact review page, not a sprawling settings form. The defaults should be good enough that most users only need to pick an engine and press Render. Everything else is collapsed.

---

### Stage 7: Output

**Purpose:** Assemble rendered clips into a final video. Mix audio. Export.

**Internal tabs:**

**Tab 1: Assembly**
- Video player (preview of assembled sequence)
- Storyboard strip (draggable clip thumbnails)
- Timeline editor (from current Edit workspace -- drag edges to trim, reorder, zoom)
- Transition controls per cut point (None / Dissolve / Cut / Fade)
- Beat sync button
- Playback controller

**Tab 2: Audio**
- Music track (uploaded or generated via Suno)
- Voice-over controls (TTS, clone)
- SFX list
- Audio ducking / mix controls
- Volume envelopes

**Tab 3: Export**
- Platform presets (YouTube, TikTok, IG Reel, IG Post, Twitter, Cinema, GIF)
- Format / resolution / quality settings
- Watermark controls
- Album art generator
- QR code / embed code
- Thumbnail generator
- Download button

**Primary CTA:** **"Export Video"** (on the Export tab)

**Feel:** Tabbed workspace. Each tab is focused and clean. Assembly is visual. Audio is a mixer. Export is a form. No mega-scroll.

---

## 4. Stage Gates / Readiness Logic

Gates are **soft warnings, never hard blocks.** Every stage is clickable in the stepper at all times. But the primary CTA shows a readiness indicator and warns when prerequisites are missing.

### Flexible gate system:

| Transition | Gate Check | Warning if unmet |
|-----------|-----------|-----------------|
| Brief -> Drafts & Refs | Master prompt is not empty | "Enter a creative prompt to generate your project draft" |
| Drafts & Refs -> Scenes (asset-light skip) | Zero drafted assets exist | No warning. CTA becomes "Proceed to Scenes". Assets stage skipped but remains reachable via stepper. |
| Drafts & Refs -> Assets (normal) | Every drafted asset has an explicit source mode | "Set a visual source for every drafted asset before generating stills" |
| Assets -> Scenes | At least 1 asset exists (generated, approved, or locked) | "Generate at least one asset before building scenes." If assets exist but none are approved: soft warning -- "Scenes will use provisional unapproved assets." |
| Scenes -> Shots | At least 1 scene exists | "Create at least one scene before expanding to shots" |
| Shots -> Render | At least 1 shot exists | "Create at least one shot before rendering." If shots exist but none are approved: soft warning -- "Render will use provisional unapproved shots." |
| Render -> Output | At least 1 clip has been rendered | "Render at least one clip to see output" |

**For asset-light project types** (abstract, lyric video, mood/trailer):
- The AI draft may return zero characters and zero environments -- just style keywords and scene moods
- In this case, the Drafts & Refs stage shows a minimal view: "No entity assets drafted. Your project uses style-driven generation." with a **"Proceed to Scenes"** CTA
- Clicking "Proceed to Scenes" skips both still generation (Drafts & Refs CTA) and the Assets review stage, advancing directly to Scenes
- The Assets stage remains reachable via the stepper if the user later wants to add assets manually
- The stepper shows both Drafts & Refs and Assets as "skippable" (dimmed dot with a dash indicator)

**Project type adaptations:**

| Project Type | Expected Assets | Drafts & Refs behavior |
|-------------|----------------|----------------------|
| Short Film | Characters, costumes, environments, props | Full review + reference upload |
| Music Video | Characters, costumes, environments | Full review + reference upload |
| Trailer | Characters, environments | Quick review, fewer assets |
| Cinematic | Environments, mood | Minimal -- mostly AI-only |
| Brand Film | Characters, props, environments | Full review |
| Lyric Video / Abstract | None or minimal | "Proceed to Scenes" -- skips Assets stage |

---

## 5. Reference-Review System

### Product Rule (non-negotiable):

**The system MUST NOT auto-generate an asset image from AI text alone without first presenting the user with the opportunity to set a visual source for that asset.**

This is enforced by the two-phase Drafts & Refs stage. The "Generate Stills" CTA does not activate until every drafted asset has an explicit visual source mode. The three approved modes are:

- `ai_only` -- user explicitly chose to let AI generate from text
- `upload` -- user uploaded a reference photo
- `hybrid` -- user uploaded a photo AND wants AI to augment

No other modes exist. The key word is **explicit.** No asset defaults to any mode. The user must take an action on each one, even if that action is clicking "Set All to AI Only." If the AI draft produced zero entity assets (asset-light project types), the CTA becomes "Proceed to Scenes" -- skipping both still generation and the Assets review stage.

### Per-asset data model:

```python
{
    "asset_id": "char_001",
    "asset_type": "character",
    "name": "Maya Chen",
    "ai_description": "28, athletic build, dark hair in loose bun...",

    # Reference layer
    "visual_source_mode": None,   # null until user sets it — valid values: "ai_only", "upload", "hybrid"
    "reference_image_path": None, # set if mode is "upload" or "hybrid"
    "reference_locked": False,

    # Generation layer
    "status": "drafted",  # drafted -> source_set -> generating -> generated -> approved -> locked
    "generated_stills": [],
    "hero_image_path": None,
}
```

### Status flow:

```
drafted -> source_set -> generating -> generated -> approved -> locked
                ^                        |
                +-- reject / re-upload --+
```

### Internal state to UI label mapping:

| Internal state | User-facing label | Badge color | Meaning |
|---------------|-------------------|-------------|---------|
| `drafted` | Drafted | Gray | AI text exists, no visual source set |
| `source_set` | Ready | Blue | Visual source mode chosen, awaiting generation |
| `generating` | Generating... | Amber pulse | Still image generation in progress |
| `generated` | Generated | Yellow | Still exists but not reviewed/approved |
| `approved` | Approved | Green | Canonical source of truth for downstream |
| `locked` | Locked | Green + lock icon | Approved and protected from accidental changes (asset lock) |

The UI never displays raw internal state names. Code should reference the internal values; the frontend maps them to labels via this table.

**Note:** `reference_locked` is a separate boolean on the data model, not a generation status. It protects the reference input (Drafts & Refs stage, "Lock Reference" action). The `locked` status above protects the generated output (Assets stage, "Lock Asset" action). Both can be true simultaneously -- a fully locked asset has both its reference input and generated output protected.

### Upload behavior:

1. User clicks upload zone on an asset card (or drags a file onto it)
2. File uploads to `/api/assets/{asset_id}/reference`
3. Server stores at `output/references/{asset_type}/{asset_id}/ref.{ext}`
4. Card shows thumbnail, mode switches to `upload`
5. Status becomes `source_set`
6. If user clicks "Lock Reference": `reference_locked = true`, downstream generation will always use this image. This is separate from "Lock Asset" in the Assets stage, which protects the generated output.
7. During generation, the uploaded image is sent as a Runway `@Tag` reference

### Bulk actions:

- **"Set All to AI Only"** -- sets `visual_source_mode = "ai_only"` for all assets without a mode. Does not override uploads.
- **"Lock All References"** -- sets `reference_locked = true` for every asset that has a source mode set. This protects reference inputs only, not generated outputs.
- **"Upload Batch"** -- multi-file upload. System attempts filename matching (e.g., `maya.jpg` -> "Maya Chen"). Unmatched files shown for manual assignment.

---

## 6. Page/Component Architecture

### Layout:

```
+----------------------------------------------------------+
| HEADER BAR (fixed, 48px)                                 |
| [Logo] [Project v] [Save] [Undo] [Redo]  [$0.00] [G] [M]|
+----------------------------------------------------------+
| STAGE STEPPER (fixed, 56px)                              |
| *Brief -- *Drafts&Refs -- oAssets -- oScenes -- ...      |
| Step 2 of 7 -- Review AI drafts and confirm references   |
+----------------------------------------------------------+
| STATUS BAR (fixed, 28px, compact)                        |
| Assets: 6 | Scenes: 4 | Shots: 12 | ~1:24 | $0.00      |
+----------------------------------+-----------------------+
|                                  |                       |
|  MAIN CONTENT                    |  INSPECTOR (320px)    |
|  (flex:1, scrollable)            |  (contextual, toggle) |
|                                  |                       |
|  One stage view at a time.       |  Shows detail for     |
|                                  |  selected item.       |
|  Max-width: 740px, centered.     |                       |
|                                  |  Hidden when nothing  |
|                                  |  is selected.         |
|                                  |                       |
+----------------------------------+-----------------------+
| BOTTOM ACTION BAR (fixed, 56px)                          |
| [secondary actions]              [<- Back] [Primary CTA->]|
+----------------------------------------------------------+
```

### Inspector behavior:

- **Collapsed by default.** Main content takes full width.
- **Opens when user clicks an entity card** (asset, scene, or shot). Shows full detail editor.
- **Close button** returns to collapsed state.
- **In Render stage:** Inspector shows per-shot render status when a shot is selected.
- **In Output:** Inspector is hidden. Output uses its internal tabs instead.

### Responsive:

- Below 1024px: Inspector becomes a bottom sheet / modal instead of a sidebar.
- Below 768px: Stepper becomes a compact dropdown ("Step 2/7: Drafts & Refs v").

---

## 7. Component Tree

```
App
+-- HeaderBar
|   +-- Logo
|   +-- ProjectDropdown (name, save, load, reset)
|   +-- UndoRedo
|   +-- CostTracker
|   +-- SettingsButton (opens modal: API keys, defaults)
|   +-- ThemeToggle
|
+-- StageStepper
|   +-- StepButton[7] (label, number, state: pending/active/complete/skippable)
|   +-- Connectors[6] (lines between steps)
|   +-- StageDescription (one-line subtitle for active stage)
|
+-- StatusBar
|   +-- StatChip[5] (assets, scenes, shots, runtime, cost)
|   +-- AutoSaveIndicator
|
+-- MainLayout
|   +-- StageContent (one active at a time)
|   |   |
|   |   +-- BriefStage
|   |   |   +-- ProjectTypeSelector
|   |   |   +-- MasterPromptTextarea
|   |   |   +-- OptionalInputsCollapsible
|   |   |   |   +-- MusicUploadSection
|   |   |   |   +-- LyricsInput
|   |   |   |   +-- StorylineInput
|   |   |   |   +-- VisualDirectionInput
|   |   |   |   +-- WorldSettingInput
|   |   |   |   +-- MoodBoardUpload
|   |   |   +-- StageCTA "Generate Project Draft"
|   |   |
|   |   +-- DraftsRefsStage
|   |   |   +-- PhaseIndicator (* Review Drafts --- o Confirm References)
|   |   |   +-- AssetTabBar (Characters | Costumes | Environments | Props)
|   |   |   +-- AssetCardGrid
|   |   |   |   +-- DraftRefCard[n]
|   |   |   |       +-- EntityHeader (name, type badge, role)
|   |   |   |       +-- Description (editable)
|   |   |   |       +-- ReferenceSection (Phase 2)
|   |   |   |       |   +-- SourceModeSelector (AI Only | Upload | Hybrid)
|   |   |   |       |   +-- UploadDropZone
|   |   |   |       |   +-- ReferenceThumbnail
|   |   |   |       |   +-- LockReferenceButton
|   |   |   |       +-- StatusBadge
|   |   |   +-- AssetLightEmptyState ("No entity assets drafted..." + "Proceed to Scenes")
|   |   |   +-- SceneDraftPreview (read-only scene list from AI)
|   |   |   +-- BulkActions ("Set All AI Only" | "Lock All References" | "Upload Batch")
|   |   |   +-- StageCTA "Generate Stills" (normal) / "Proceed to Scenes" (zero assets)
|   |   |
|   |   +-- AssetsStage
|   |   |   +-- AssetTabBar (Characters | Costumes | Environments | Props)
|   |   |   +-- AssetCardGrid
|   |   |   |   +-- AssetCard[n]
|   |   |   |       +-- StillPreview (generated images)
|   |   |   |       +-- ReferenceSourceIndicator
|   |   |   |       +-- StatusBadge
|   |   |   |       +-- Actions (regenerate, replace, approve, lock asset)
|   |   |   +-- BatchActions ("Generate Missing" | "Approve All")
|   |   |   +-- StyleProfileCollapsible (taste sliders, quiz)
|   |   |   +-- StageCTA "Build Scenes"
|   |   |
|   |   +-- ScenesStage
|   |   |   +-- StoryboardStrip (horizontal thumbnail scroll)
|   |   |   +-- SceneCardList (vertical, reorderable)
|   |   |   |   +-- SceneCard[n]
|   |   |   |       +-- SceneStill
|   |   |   |       +-- SceneInfo (title, summary, mood, location)
|   |   |   |       +-- InvolvedAssetChips (green border = approved, dashed yellow = provisional)
|   |   |   |       +-- StatusBadge
|   |   |   |       +-- Actions (edit, swap assets, regenerate still, approve)
|   |   |   +-- ProvisionalAssetBanner (shown if any scene uses unapproved assets)
|   |   |   +-- AddSceneButton
|   |   |   +-- StageCTA "Expand to Shots"
|   |   |
|   |   +-- ShotsStage
|   |   |   +-- ShotFilters (type, status)
|   |   |   +-- ShotStats (total, previews, approved, runtime)
|   |   |   +-- SecondaryToolbar ["Generate Previews" button]
|   |   |   +-- SceneGroupedShotList
|   |   |   |   +-- SceneGroupHeader[n]
|   |   |   |   +-- ShotCard[n]
|   |   |   |       +-- ShotLabel + PromptPreview
|   |   |   |       +-- CameraTag, DurationBadge
|   |   |   |       +-- InheritedRefChips
|   |   |   |       +-- PreviewThumbnail
|   |   |   |       +-- StatusBadge
|   |   |   |       +-- Actions (edit, generate preview, approve)
|   |   |   +-- DirectorControlsCollapsible (pacing, intensity, arc)
|   |   |   +-- ImportShotsCollapsible (paste sheet, AI generate)
|   |   |   +-- StageCTA "Continue to Render"
|   |   |
|   |   +-- RenderStage
|   |   |   +-- RenderSummary (shot count, est. cost, est. time)
|   |   |   +-- EngineSettings (engine, model, quality)
|   |   |   +-- ConsistencyCollapsible (char lock, env lock, style lock, seed)
|   |   |   +-- ContinuityRulesCollapsible
|   |   |   +-- AdvancedCollapsible (universal prompt, budget cap)
|   |   |   +-- RenderQueue (per-shot status list)
|   |   |   +-- StageCTA "Render Final Clips"
|   |   |
|   |   +-- OutputStage
|   |       +-- OutputTabBar (Assembly | Audio | Export)
|   |       +-- AssemblyTab
|   |       |   +-- VideoPlayer
|   |       |   +-- StoryboardStrip (draggable clips)
|   |       |   +-- TimelineEditor
|   |       |   +-- TransitionControls
|   |       |   +-- PlaybackController
|   |       +-- AudioTab
|   |       |   +-- MusicTrack (player, upload, Suno)
|   |       |   +-- VoiceOverControls (TTS, clone)
|   |       |   +-- SFXList
|   |       |   +-- MixerControls (ducking, volume, timing)
|   |       +-- ExportTab
|   |       |   +-- PlatformPresets (YouTube, TikTok, IG, etc.)
|   |       |   +-- FormatSettings
|   |       |   +-- WatermarkControls
|   |       |   +-- ThumbnailGenerator
|   |       |   +-- DownloadButton
|   |       +-- StageCTA "Export Video" (visible on Export tab)
|   |
|   +-- InspectorPanel (320px sidebar, toggled by card selection)
|       +-- InspectorHeader (entity name, type)
|       +-- InspectorBody (full editor for selected entity)
|       +-- InspectorFooter (save, close)
|
+-- BottomActionBar (fixed)
    +-- SecondaryActions (left, context-dependent)
    +-- StageLabel ("Step 3 of 7 -- Assets")
    +-- PrimaryNavigation (<- Back | Primary CTA ->)
```

---

## 8. CTA Hierarchy (LOCKED)

### Primary CTAs (one per stage, bottom-right, prominent):

| Stage | CTA Label | Action | Type | Button Style |
|-------|----------|--------|------|-------------|
| Brief | **Generate Project Draft** | AI extracts assets + drafts scenes | Generate + advance | Gold gradient |
| Drafts & Refs | **Generate Stills** (normal) / **Proceed to Scenes** (zero assets) | Triggers sheet generation for all source-set assets. Asset-light: skips to Scenes. | Generate + advance / Advance | Green gradient |
| Assets | **Build Scenes** | Composes scene boards from approved assets. Soft warning if no assets are approved. | Advance | Cyan |
| Scenes | **Expand to Shots** | AI expands scenes into shot-level prompts | Generate + advance | Cyan |
| Shots | **Continue to Render** | Navigate to Render stage. Soft warning if no shots are approved. | Advance | Amber |
| Render | **Render Final Clips** | Starts video generation for all queued shots (approved + provisional) | Generate | Gold gradient |
| Output | **Export Video** | Stitches, applies audio, and exports | Generate | Green gradient |

### Shots stage secondary action:

| Button | Type | Position | Label | What it does |
|---|---|---|---|---|
| Preview generation | **Secondary** | Top toolbar, next to shot stats | **Generate Previews** | Generates still-frame previews for all draft shots. Optional. For visual review only. No video. |

### CTA pattern:

CTAs alternate between "generate + advance" and "advance only." The expensive generation steps are Brief (AI extraction -- cheap), Drafts & Refs (still generation -- moderate), Scenes (shot expansion -- cheap), and Render (video generation -- expensive). The advance-only steps (Assets, Shots) are review stages where the user inspects and approves before the next generation step.

This rhythm -- **generate, review, generate, review, generate, review, generate, export** -- is the core UX pulse of the app.

### Naming principle for shot generation vs final rendering:

| Term | Meaning | Used in |
|------|---------|---------|
| **Shot Preview** | A still frame generated from the shot prompt. Fast, cheap, for review. | Shots stage |
| **Render / Final Clip** | A 2-10 second video clip generated from the shot + anchor image. Expensive, final. | Render stage |
| **Export** | Stitching all clips + audio + transitions into one deliverable video file. | Output stage |

### Removed buttons:

| Button | Removed from |
|--------|-------------|
| "Start Production Pipeline" | Brief absorbs this |
| "MAKE MY MOVIE" | Brief absorbs this |
| "PLAN VIDEO" | Removed entirely. Brief + Scenes handle planning |
| "Next: Assets ->" | Stepper replaces this |
| "Generate Sheets" (pipeline action) | Drafts & Refs CTA replaces this |
| "Compose Anchors" (pipeline action) | Automated in scene composition |
| "Approve All Sheets" (pipeline action) | Secondary action in Assets |

### Secondary actions per stage:

| Stage | Left side of bottom bar |
|-------|------------------------|
| Brief | Load Template, Import Project |
| Drafts & Refs | Set All AI Only, Upload Batch, Lock All References |
| Assets | Generate Missing, Approve All, Regenerate Selected |
| Scenes | + Add Scene, Reorder |
| Shots | + Add Shot, Import Shots, AI Generate Shots |
| Render | Render Selected Only, Pause Queue |
| Output | Replace Clip, Beat Sync, Stitch |

---

## 9. Migration Map

| Current Section | Current Location | Final Destination | Action |
|---|---|---|---|
| Project Type Selector | Project:3850 | Brief stage, top | **Move** |
| Production Settings (engine, model) | Project:3863 | Render stage > Engine & Quality | **Move** |
| V5 Pipeline Wizard | Project:3900 | -- | **Remove** (Brief stage replaces it) |
| Make My Movie | Project:3966 | -- | **Remove** (Brief stage replaces it) |
| Director Mode | Project:4045 | Shots stage > Import Shots collapsible | **Move** |
| Hidden legacy divs | Project:4109 | -- | **Delete** (dead code) |
| Story panel (song, lyrics, storyline) | Project:4121 | Brief stage (optional inputs) | **Move** |
| Look & World panel | Project:4249 | Brief stage (visual direction / world setting) | **Move** |
| Continuity Rules | Project:4290 | Render stage > Continuity collapsible | **Move** |
| Hidden director refs | Project:4303 | Keep hidden for JS compat | **Keep** |
| Advanced Controls (lock sliders) | Project:4313 | Render stage > Consistency collapsible | **Move** |
| Creative Controls (pacing, intensity, arc) | Project:4360 | Shots stage > Director Controls collapsible | **Move** |
| PLAN VIDEO button | Project:4386 | -- | **Remove** |
| Hidden compat buttons | Project:4392 | Keep hidden for JS compat | **Keep** |
| Scene Plan Grid | Project:4417 | Scenes stage | **Move** |
| Auto Scene Plan | Project:4429 | Scenes stage (merge into scene cards) | **Move** |
| Draft Asset Resolution | Project:4448 | Drafts & Refs stage | **Move** |
| Auto Output | Project:4463 | Output stage | **Move** |
| Next Step CTA | Project:4478 | -- | **Remove** (stepper handles nav) |
| Style Profile / Taste | Assets:4509 | Assets stage > Style Profile collapsible | **Move** |
| Preproduction Packages | Assets:4544 | Drafts & Refs stage (asset management) | **Move** |
| Asset sub-panels (chars, costumes, envs, props) | Assets:4578+ | Assets stage | **Keep** |
| Shots workspace | Shots:4797+ | Shots stage (+ scene grouping headers) | **Keep + enhance** |
| Edit workspace (timeline, storyboard) | Edit:5736+ | Output > Assembly tab | **Move** |
| Audio workspace | Audio:5913+ | Output > Audio tab | **Move** |
| Output workspace (export, presets) | Output:6229+ | Output > Export tab | **Keep + enhance** |

---

## 10. Guided Mode vs Power Mode

### Guided Mode (default):

- 7-stage stepper, one view at a time
- Inspector panel opens on card selection, closed by default
- Advanced settings collapsed by default
- Soft stage gates with readiness indicators
- Single prominent CTA per stage
- Phase indicator in Drafts & Refs
- Clean, minimal, progressive disclosure

### Power Mode (toggle in header):

When enabled (`data-power="true"` on `<body>`):

| Change | Effect |
|--------|--------|
| Stage gates disabled | No readiness warnings, free navigation |
| Inspector always open | 320px panel pinned, shows all settings for selected item |
| Collapsibles auto-expand | Director Controls, Consistency, Continuity, Advanced all open |
| Render settings available everywhere | A compact "Quick Render Config" panel appears in the Inspector on any stage |
| Import Shots always visible | In Shots stage, not collapsed |
| Keyboard shortcuts active | `1-7` jump stages, `G` generate, `A` approve, `L` lock, `Ctrl+Enter` = CTA |
| Batch select enabled | Shift+click to select multiple cards for bulk operations |

### CSS implementation:

```css
.power-only { display: none; }
[data-power="true"] .power-only { display: block; }
[data-power="true"] .guided-only { display: none; }
[data-power="true"] details.advanced-section { open: true; }
[data-power="true"] #inspectorPanel { transform: translateX(0); }
```

Power Mode does not add features. It removes progressive disclosure so experienced users can access everything faster within the same stage architecture.

---

## 11. Implementation Priority (Build Order)

### Priority 1: Stage skeleton (highest impact, enables everything else)

**What:** Replace the 6-tab workspace nav with the 7-stage stepper. Create 7 stage container divs. Implement `_setStage()` switching. Add bottom action bar with stage-specific CTAs.

**Files:** `public/index.html` (HTML structure + CSS + JS)

**Why first:** Every subsequent phase moves content into these containers. The skeleton must exist before anything can be relocated.

### Priority 2: Brief stage (single entry point)

**What:** Build the Brief stage content: project type selector, master prompt textarea, optional inputs collapsible (move song upload, lyrics, storyline, visual direction, world setting into it). Single CTA: "Generate Project Draft" wired to `/api/pipeline/start`.

**Simultaneously remove:** Pipeline Wizard, Make My Movie, and PLAN VIDEO button from the old Project workspace.

### Priority 3: Drafts & References stage (the new checkpoint)

**What:** Build the combined Drafts & Refs view. Two-phase card grid: draft review (editable text cards) -> reference confirmation (source mode picker + upload zone per card). Bulk actions. "Generate Stills" CTA for normal projects. Asset-light empty state with "Proceed to Scenes" CTA when the AI draft produces zero entity assets (skips both generation and Assets stage, advances directly to Scenes).

**Backend work needed:**
- `POST /api/assets/{id}/reference` -- upload reference photo
- `PATCH /api/assets/{id}/source-mode` -- set visual source mode
- `POST /api/assets/bulk-source-mode` -- set all to AI-only
- Reference image storage at `output/references/{type}/{id}/`
- Add `visual_source_mode`, `reference_image_path`, `reference_locked` fields to package model

### Priority 4: Relocate Render settings

**What:** Move engine select, model select, quality select, lock sliders, seed, continuity rules, and budget cap into the Render stage container. Remove them from their current locations. **Creative Controls (pacing, intensity, arc) do NOT move here** -- they stay in the Shots stage (handled in Priority 7).

### Priority 5: Assets stage

**What:** Rehouse the existing asset tab panels (characters, costumes, environments, props) in the Assets stage container. Add Style Profile collapsible. Wire "Build Scenes" CTA to scene composition.

### Priority 6: Scenes stage

**What:** Move scene plan grid into Scenes stage. Add storyboard strip header. Wire "Expand to Shots" to shot expansion logic.

### Priority 7: Shots stage

**What:** Rehouse existing Shots workspace content. Add scene-grouped headers. Move Director Mode and Creative Controls here as collapsibles. "Generate Previews" as secondary toolbar action. "Continue to Render" as primary CTA.

### Priority 8: Output stage with tabs

**What:** Create 3-tab Output: Assembly (timeline from Edit workspace), Audio (from Audio workspace), Export (from Output workspace). Wire "Export Video" CTA.

### Priority 9: Inspector panel

**What:** Add right sidebar that opens on card selection. Populate with detail editors per entity type.

### Priority 10: Power Mode toggle

**What:** Add toggle in header. CSS-driven show/hide of advanced controls.

---

**This specification is final and locked.** All placements are resolved. No contradictions remain between sections. Build sequentially starting with Priority 1.
