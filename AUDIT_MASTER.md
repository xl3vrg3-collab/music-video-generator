# LUMN Studio Master Audit — 10-Agent Deep Dive + Cross-Review
## Date: 2026-04-06 | Agents: 10 independent + 9 cross-reviewers

---

## CRITICAL (9-10/10) — Must fix before launch

| # | Issue | Agents Found | Category |
|---|-------|-------------|----------|
| 1 | **No authentication/authorization** — any request from any source can control the app | 1 | Security |
| 2 | **CSRF: No tokens on POST endpoints** — external sites can trigger generation/deletion | 3 (A4,A9,R8) | Security |
| 3 | **Scene plan JSON written without file locks** — crash mid-write = data corruption | 3 (A4,A7,R8) | Data Integrity |
| 4 | **innerHTML XSS** — unescaped API error messages + user prompts rendered as HTML | 4 (A1,A4,A9,R1) | Security |
| 5 | **Path traversal in file serving** — no normalization check on clip/reference URLs | 3 (A4,A8,A9) | Security |
| 6 | **Timeline reorder doesn't persist** — drag-reorder in canvas only updates frontend, not backend | 2 (A7,A8) | Data Integrity |
| 7 | **Rate limiter race condition** — global dict without threading.Lock, exploitable for DDoS bypass | 6 (A1,A3,A4,R1,R3,R8) | Security |
| 8 | **Transition schema mismatch** — index.html uses {entry,exit,duration}, timeline.js uses {type,duration} | 1 (A8) | Bug |
| 9 | **Viewport zoom disabled** — `user-scalable=no` blocks accessibility, violates WCAG | 2 (A9,R9) | Accessibility |

## HIGH (7-8/10) — Fix before beta

| # | Issue | Agents Found | Category |
|---|-------|-------------|----------|
| 10 | **File uploads buffered in RAM** — no streaming, OOM on large files | 5 (A1,A3,A4,R1,R3) | Performance |
| 11 | **238+ innerHTML DOM rebuilds** — full reflow on every scene change, blocks 200-500ms | 5 (A1,A3,R1,R3,R8) | Performance |
| 12 | **Missing Content-Security-Policy header** | 3 (A4,A9,R1) | Security |
| 13 | **No MIME type validation on uploads** — can upload executables | 2 (A1,A4) | Security |
| 14 | **Missing Content-Length validation** — attacker can claim 100GB | 2 (A2,A4) | Security |
| 15 | **Subprocess calls without timeout** — hung ffmpeg freezes handler | 2 (A3,A7) | Reliability |
| 16 | **No fetch response.ok checks** — error responses parsed as data | 2 (A1,A3) | Bug |
| 17 | **Uncontrolled polling** — 33+ setIntervals, orphaned intervals leak memory | 4 (A3,R1,R3,R8) | Performance |
| 18 | **Event listener leaks** — 91 adds, 7 removes, modals stack handlers | 3 (A1,A3,R1) | Performance |
| 19 | **Touch targets below 44px** — buttons 28-32px, fails WCAG | 2 (A9,R9) | Accessibility |
| 20 | **No focus traps on modals** — keyboard users can't dismiss or navigate | 3 (A2,A9,R9) | Accessibility |
| 21 | **Canvas timeline ignores theme CSS variables** — hardcoded hex colors | 1 (A5) | Visual |
| 22 | **BUILD PROJECT button undersized** — 11px vs PLAN VIDEO 13px | 6+ (all) | UX |
| 23 | **1,746 inline styles override design system** | 5+ | Code Quality |
| 24 | **199 !important flags** — broken CSS cascade | 5+ | Code Quality |
| 25 | **AI Auto-Fill hidden until photo uploaded** — 6 duplicate buttons across asset types | 4 (A2,R2,R4,R7) | UX |
| 26 | **Sheet approval: 4 slots, tiny buttons, no explanation** | 3+ | UX |
| 27 | **Completion dots 6px, invisible contrast** on dark/light theme | 5+ | UX |
| 28 | **Audio format guidance missing** — no bitrate, size, quality hints | 3+ | UX |
| 29 | **3 conflicting transition systems** — global, per-scene, default. No priority explained | 4 (A8,R6,R8) | UX |
| 30 | **Scene index accessed without bounds check** — crashes on invalid index | 2 (A4,A8) | Bug |
| 31 | **parseInt/parseFloat without NaN checks** — 30+ instances | 2 (A1,A8) | Bug |
| 32 | **Memory leak: append-only globals** — arrays grow unbounded between projects | 3 (A1,A3,R1) | Performance |
| 33 | **Z-index chaos** — values from 101 to 100000 with no hierarchy | 2 (A2,A5) | Code Quality |
| 34 | **Light theme colors clash** — magenta becomes brown, amber becomes muddy | 4+ | Visual |
| 35 | **6 tabs wrap into 2 rows on tablet** (though may be fixed with nowrap override) | 4+ | Responsive |
| 36 | **JSON parsing without error boundary** — corrupted files crash server | 3 (A2,A7,A9) | Reliability |
| 37 | **No network error retry/backoff** — fetch failures are silent and permanent | 2 (A2,A9) | UX |
| 38 | **Stale clip paths after timeline reorder** — mismatched references | 2 (A4,A7) | Data Integrity |
| 39 | **656 querySelector calls, many in loops** — no element caching | 1 (A3) | Performance |
| 40 | **Character form too many fields at once** — 10+ visible before photo upload | 3+ | UX |

## MEDIUM (5-6/10) — Fix in next sprint

| # | Issue | Agents Found | Category |
|---|-------|-------------|----------|
| 41 | Drag-to-reorder has cursor hints but no visible instruction text | Disputed (3 agree, 2 disagree) | UX |
| 42 | Storyboard strip has minimal function (clickable but no editing) | Disputed (3 agree, 2 disagree) | UX |
| 43 | Approval vs generation status in 8px text — hard to read | 4+ | UX |
| 44 | Generate Frame/Clip numbered steps imply mandatory sequence | 3+ | UX |
| 45 | Welcome screen transition timing misaligned | 3+ | UX |
| 46 | Cost tracker overlaps theme toggle on tablets | 3+ | Responsive |
| 47 | Audio timeline confusing dual purpose | 3+ | UX |
| 48 | Disabled render reason hidden in tooltip only | 4+ | UX |
| 49 | Upscale toggle lacks cost/time info | 3+ | UX |
| 50 | TTS voice selection has no preview/descriptions | 3+ | UX |
| 51 | "Add to Edit Timeline" label is cryptic | 3+ | UX |
| 52 | Voice clone presets suggest illegal celebrity use | 3+ | Legal |
| 53 | Costume requires character but no guidance text | 3+ | UX |
| 54 | Shot sheet description text 9px — too small | 4+ | UX |
| 55 | Section labels 10px throughout — barely readable mobile | 4+ | UX |
| 56 | Tab naming asymmetric ("Paste Shot Sheet" vs "AI Write It For Me") | 3+ | UX |
| 57 | PLAN VIDEO button active without song uploaded | 3+ | UX |
| 58 | Text overlay editing — no timeline scrubbing for timing | 3+ | UX |
| 59 | Trim handles invisible until hover | 3+ | UX |
| 60 | Lock Style button label vague | 3+ | UX |
| 61 | Shot type filter duplicates editor dropdown | 3+ | UX |
| 62 | Render errors not persistent, no retry button | 3+ | UX |
| 63 | Render summary missing file size/time estimates | 3+ | UX |
| 64 | Environment "Cinematic Conditions" too technical for beginners | 3+ | UX |
| 65 | Stem separation dead-end UX | 3+ | UX |
| 66 | Voice dubbing missing "Add to Timeline" button | 3+ | UX |
| 67 | File cache 5s TTL too short — thrashes on 3s polling | 3+ | Performance |
| 68 | Suno integration buried/duplicated | 3+ | UX |
| 69 | Focus states inconsistent — some remove outline entirely | 3+ | Accessibility |
| 70 | Animation timing scattered — 0.15s to 0.8s with no system | 3+ | Visual |
| 71 | Glass morphism blur values inconsistent — 8px to 24px | 3+ | Visual |
| 72 | 6+ accent colors with no hierarchy | 4+ | Visual |
| 73 | No ETag/Last-Modified caching on static files | 3+ | Performance |
| 74 | Hardcoded paths break on deployment | 3+ | DevOps |
| 75 | localStorage quota overflow risk — no size check on autosave | 2 | Bug |
| 76 | Image thumbnail cache never evicted — memory leak | 3 (A1,A3,A8) | Performance |
| 77 | ResizeObserver never disconnected | 2 (A7,A3) | Performance |
| 78 | Modal doesn't prevent body scroll | 1 (A1) | UX |
| 79 | Missing CORS headers for cross-origin deployment | 2 (A1,A2) | DevOps |
| 80 | No cleanup of temp files — disk grows unbounded | 1 (A4) | DevOps |

## LOW (3-4/10) — Nice to have

| # | Issue | Category |
|---|-------|----------|
| 81 | Collapsible panel dots too small (5px) but functional | UX |
| 82 | SVG grain overlay nearly invisible at 0.2 opacity | Visual |
| 83 | Spinner animation has no will-change hint | Performance |
| 84 | Font weight 900 used but Inter Tight max is 800 | Visual |
| 85 | Hardcoded English strings — no i18n framework | i18n |
| 86 | Time/number formatting not locale-aware | i18n |
| 87 | No service worker for offline support | DevOps |
| 88 | Division by zero risk in timeline zoom when totalDuration=0 | Bug |
| 89 | Scene duration mismatch — server defaults 8s, timeline defaults 4s | Bug |

---

## DISPUTED ISSUES (reviewers disagreed)

| Issue | Agree | Disagree | Verdict |
|-------|-------|----------|---------|
| Drag-to-reorder undiscoverable | 6 | 3 (has cursor:grab + tooltip) | KEEP — tooltip exists but not prominent enough |
| Storyboard strip orphaned | 5 | 2 (clickable with _posEditScene) | DOWNGRADE — has function but minimal |
| Rate limiter needs lock | 7 | 1 (GIL protects deque ops) | KEEP — global variable race is real |
| Canvas renders every mousemove | 4 | 2 (uses RAF throttle) | DOWNGRADE — throttled but still excessive |
| Tab wrapping on tablet | 5 | 1 (nowrap override exists) | VERIFY — may be fixed already |
| Lookbook still active | 1 (R8) | Needs verification | CHECK — was it fully removed? |

---

## STATS
- Total unique issues: 89+
- Critical (must-fix): 9
- High (pre-beta): 31
- Medium (next sprint): 40
- Low (nice to have): 9
- Agents deployed: 10 independent + 9 cross-reviewers
- Total agent-hours: ~19 audits
- Issues found per agent: 20-36 average
