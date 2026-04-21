# LUMN Studio — Deep Audit Results (2026-04-06)

## 60 Issues Found by 10-Agent Critic Panel

### TIER 1: CRITICAL (8-9/10 consensus)

| # | Issue | Votes | Source |
|---|-------|-------|--------|
| 1 | Drag-to-reorder on timeline undiscoverable — no cursor hint, no instruction | 9/10 | Edit |
| 2 | Full DOM rebuild on every scene change — blocks main thread 200-500ms | 9/10 | JS Perf |
| 3 | BUILD PROJECT button undersized — 11px vs 13px PLAN VIDEO | 9/10 | Project |
| 4 | Rate limiter has no thread lock — race condition | 8/10 | Server |
| 5 | File uploads buffered entirely in RAM — no streaming | 8/10 | Server |
| 6 | Inline styles override design system — 1,699 instances | 8/10 | CSS |
| 7 | AI Auto-Fill hidden until photo uploaded | 8/10 | Assets |
| 8 | Sheet approval system confusing — 4 slots, tiny buttons | 8/10 | Assets |
| 9 | 6 tabs wrap into 2 rows on tablet | 8/10 | Nav |
| 10 | Completion dots invisible — 6px, no contrast | 8/10 | Nav |
| 11 | Storyboard strip orphaned — zero edit function | 8/10 | Edit |
| 12 | Global vs per-scene transitions conflict — 3 systems | 8/10 | Edit |
| 13 | Shot editor sections undiscoverable — collapsed with opaque names | 8/10 | Shots |
| 14 | Audio format guidance missing | 8/10 | Audio |
| 15 | Voice clone legal warning buried below celebrity presets | 8/10 | Audio |
| 16 | Uncontrolled polling — 33 setIntervals, 7 clearIntervals | 8/10 | JS Perf |
| 17 | Massive string HTML generation — O(n²) for scene cards | 8/10 | JS Perf |

### TIER 2: HIGH (6-7/10 consensus)

| # | Issue | Votes | Source |
|---|-------|-------|--------|
| 18 | Color palette chaos — 6+ accent colors, no hierarchy | 7/10 | CSS |
| 19 | 178 !important flags — broken cascade | 7/10 | CSS |
| 20 | Light theme colors clash — magenta→brown | 7/10 | CSS |
| 21 | No ETag caching on static files | 7/10 | Server |
| 22 | N+1 file checks in scene listing — 1,300+ calls | 7/10 | Server |
| 23 | Event listener leaks — 91 adds, 7 removes | 7/10 | JS Perf |
| 24 | Memory leak: append-only global arrays | 7/10 | JS Perf |
| 25 | Approval vs generation status in 8px text | 7/10 | Shots |
| 26 | Generate steps imply mandatory sequence | 7/10 | Shots |
| 27 | Welcome transition timing misaligned | 7/10 | Nav |
| 28 | Cost tracker overlaps theme toggle on tablets | 7/10 | Nav |
| 29 | Audio timeline confusing dual purpose | 7/10 | Edit |
| 30 | Disabled render reason hidden in tooltip | 7/10 | Output |
| 31 | Upscale toggle lacks cost/time info | 7/10 | Output |
| 32 | TTS voice selection has no previews | 7/10 | Audio |
| 33 | "Add to Edit Timeline" cryptic | 7/10 | Audio |
| 34 | Character form too many fields at once | 7/10 | Assets |
| 35 | Costume requires character but no guidance | 7/10 | Assets |
| 36 | Duplicate AI Auto-Fill buttons | 7/10 | Assets |
| 37 | Shot sheet description text 9px | 8/10 | Project |
| 38 | Section labels 10px throughout | 8/10 | Project |
| 39 | Tab naming asymmetric | 7/10 | Project |
| 40 | PLAN VIDEO active without song | 6/10 | Project |

### TIER 3: MEDIUM (4-6/10 consensus)

| # | Issue | Votes | Source |
|---|-------|-------|--------|
| 41 | Text overlay editing no timeline scrubbing | 6/10 | Edit |
| 42 | Trim handles invisible until hover | 6/10 | Edit |
| 43 | Lock Style button label vague | 6/10 | Shots |
| 44 | Camera emoji picker buttons unclear | 6/10 | Shots |
| 45 | Shot type filter duplicates editor dropdown | 6/10 | Shots |
| 46 | Render errors not persistent, no retry | 6/10 | Output |
| 47 | Render summary missing file size/time | 6/10 | Output |
| 48 | Environment "Cinematic Conditions" too technical | 6/10 | Assets |
| 49 | Voice clone presets suggest illegal use | 6/10 | Audio |
| 50 | Stem separation dead-end UX | 6/10 | Audio |
| 51 | Voice dubbing missing Add to Timeline | 6/10 | Audio |
| 52 | File cache 5s TTL too short | 6/10 | Server |
| 53 | Hardcoded paths break on deployment | 6/10 | Server |
| 54 | Fetch waterfall on initial load | 6/10 | JS Perf |
| 55 | Canvas renders on every mousemove | 6/10 | JS Perf |
| 56 | Suno integration buried/duplicate | 6/10 | Project |
| 57 | Collapsible panel dots too small, no legend | 7/10 | Project |
| 58 | Focus states inconsistent | 6/10 | CSS |
| 59 | Animation timing scattered | 6/10 | CSS |
| 60 | Glass morphism blur values inconsistent | 6/10 | CSS |
