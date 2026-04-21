# LUMN Studio Re-Audit Results — Post-Fix Verification
## Date: 2026-04-06 | 10 Independent Re-Auditors

## FIXES VERIFIED WORKING
- Viewport zoom enabled (user-scalable removed)
- Touch targets 44px / 36px
- Focus-visible styles implemented
- Modal focus traps on 17 modals
- Completion dots 10px with border
- Aria-labels on 6 workspace tabs
- prefers-reduced-motion CSS exists (line 2696)
- Page Visibility API pauses 3 named timers
- ResizeObserver debounced to 100ms
- Modal escape handler uses named function
- Timer cleanup on workspace switch
- Plan file locking (21 write sites)
- Transition schema consistent (i+1)
- CSRF token generated + injected
- Auth system (CSRF for UI, bearer for API)
- ETag caching with 304 responses
- MIME type validation on uploads
- JSON parse error boundaries (9 locations)
- Path traversal realpath protection
- Subprocess timeouts on all calls
- Font preload (not @import)
- timeline.js deferred
- Blur values standardized (--blur-sm/--blur-lg)
- Animation timing using var(--transition)
- Light theme colors fixed
- Canvas timeline reads theme CSS vars
- Section labels 11px
- BUILD button 13px
- Character form appearance collapsed
- Voice clone disclaimers added

## NEW ISSUES FOUND (Prioritized)

### CRITICAL — Blocks core features
| # | Issue | Agents | Severity |
|---|-------|--------|----------|
| 1 | **Missing /api/auto-director/scenes/reorder endpoint** — timeline drag-reorder silently fails | 2 (A7,A8) | 9 |
| 2 | **Missing /api/auto-director/import-shots endpoint** — re-audit agent claims missing but we added it (VERIFY) | 1 (A8) | 9 |
| 3 | **Missing /api/reference-demos endpoint** — visual picker broken (VERIFY - existed before) | 1 (A8) | 8 |
| 4 | **Timeline trim values not persisted** — only reorder saves, trim lost on refresh | 2 (A1,A7) | 8 |
| 5 | **State mutation before server confirmation** — reorder updates client before server responds | 2 (A7,A1) | 8 |
| 6 | **Focus trap listener leak** — adds keydown per modal open, never removed | 2 (A1,A4) | 7 |
| 7 | **CSRF token in meta tag extractable by XSS** | 2 (A1,A3) | 7 |
| 8 | **Auth bypass: /output/ skips all auth** — attacker can access generated files | 1 (A3) | 9 |
| 9 | **API token logged to console** at startup | 1 (A3) | 7 |
| 10 | **Bearer token in query string** — logged, cached, in browser history | 1 (A3) | 7 |

### HIGH — Should fix
| # | Issue | Agents | Severity |
|---|-------|--------|----------|
| 11 | Missing <label> associations on most form inputs — WCAG 1.3.1 | 1 (A5) | 9 |
| 12 | Divs as buttons without ARIA roles | 1 (A5) | 9 |
| 13 | --text-dim contrast fails WCAG AA | 1 (A5) | 8 |
| 14 | No aria-live on toast/error messages | 1 (A5) | 8 |
| 15 | Wildcard CORS origin (allows any) | 1 (A3) | 8 |
| 16 | Poll timers not paused by visibility API (generation polling) | 1 (A1) | 7 |
| 17 | apiPost/apiGet missing .ok checks | 1 (A1) | 6 |
| 18 | Workspace nav hardcoded colors, not theme-aware | 1 (A6) | 6 |
| 19 | Inline onmouseover/onmouseout instead of CSS :hover | 1 (A6) | 8 |
| 20 | 1756 inline styles + 178 !important (unchanged) | 2 (A4,A6) | 6 |
| 21 | DOM rebuild with 100+ scenes = 2-3MB HTML string | 1 (A10) | 9 |
| 22 | No fetch timeout (AbortController) | 1 (A10) | 6 |
| 23 | 20+ empty .catch(function(){}) blocks | 2 (A4,A9) | 6 |
| 24 | AI Auto-Fill button opacity 0.5 looks disabled | 1 (A2) | 7 |
| 25 | Missing gender/pronouns field in character form | 1 (A2) | 7 |
| 26 | env.example missing LUMN_API_TOKEN | 1 (A9) | 5 |
| 27 | Color theme regex bug in timeline.js | 1 (A9) | 7 |
| 28 | Multiple stacked document keydown listeners | 1 (A4) | 6 |

### MEDIUM — Polish
| # | Issue | Agents | Severity |
|---|-------|--------|----------|
| 29 | Scroll position not restored on back navigation | 1 (A2) | 7 |
| 30 | Voice dropdown 60+ options, no grouping | 1 (A2) | 6 |
| 31 | Lock Scene 1 Style doesn't persist to server | 1 (A8) | 6 |
| 32 | Loop mode settings only in localStorage | 1 (A8) | 5 |
| 33 | Render summary doesn't update on loop mode toggle | 1 (A8) | 5 |
| 34 | Welcome screen references non-existent #wBg element | 1 (A8) | 4 |
| 35 | Service worker cache grows unbounded | 1 (A4) | 5 |
| 36 | Concurrent tab edits = last-write-wins | 1 (A10) | 8 |
| 37 | Autosave only every 60s — data loss window | 1 (A10) | 7 |
| 38 | No browser back button support | 1 (A10) | 6 |

## DISPUTED/FALSE POSITIVES
- "Missing import-shots endpoint" — WE ADDED IT (agent searched wrong part of file)
- "Missing reference-demos endpoint" — EXISTS in server.py
- "Missing ai-autofill endpoint" — EXISTS as /api/ai-autofill or /api/enhance-prompt  
- "No rate limiting" — EXISTS with threading.Lock
- "No security headers" — ADDED X-Content-Type-Options + X-Frame-Options
- "prefers-reduced-motion missing" — EXISTS at line 2696
