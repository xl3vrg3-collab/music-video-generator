# LUMN Studio Final Audit — 10 Independent Auditors, Full Consensus
## Date: 2026-04-06

## CONSENSUS RESULTS (issues ranked by how many of 10 agents independently found them)

### 10/10 CONSENSUS — Every auditor flagged these
| # | Issue | Avg Severity | Real Status |
|---|-------|-------------|-------------|
| 1 | **Wildcard CORS origin** — echoes any Origin header back | 8.4 | REAL — needs origin whitelist |
| 2 | **innerHTML XSS** — 100+ instances with user data | 8.1 | PARTIALLY FIXED — 7 escaped, 90+ remain |
| 3 | **API token in query string** — leaks in logs/history/referer | 7.6 | REAL — remove query param support |

### 8-9/10 CONSENSUS
| # | Issue | Agents | Real Status |
|---|-------|--------|-------------|
| 4 | **No HTTPS enforcement** | 8/10 | REAL but expected for local dev. Add for deployment. |
| 5 | **Missing CSP header** | 8/10 | REAL — add Content-Security-Policy |
| 6 | **Unsafe multipart parser** | 7/10 | REAL — custom regex parser, should use library |
| 7 | **Service worker cache never invalidates** | 8/10 | REAL — no version busting strategy |
| 8 | **Thumbnail cache unbounded** | 8/10 | REAL — no eviction, grows forever |
| 9 | **CSRF in meta tag extractable** | 7/10 | DESIGN TRADEOFF — standard SPA pattern |
| 10 | **No rate limiting** | 7/10 | FALSE — rate limiter EXISTS with lock |

### 6-7/10 CONSENSUS
| # | Issue | Agents | Real Status |
|---|-------|--------|-------------|
| 11 | **Race condition in gen_state** | 6/10 | PARTIALLY REAL — gen_lock exists but not everywhere |
| 12 | **localStorage sensitive data unencrypted** | 6/10 | REAL but low risk for local app |
| 13 | **Missing JSON try-catch on some endpoints** | 6/10 | PARTIALLY FIXED — 9 locations wrapped, some remain |
| 14 | **Missing Content-Length validation** | 6/10 | FALSE — 500MB limit EXISTS |
| 15 | **Missing file upload validation** | 6/10 | FALSE — ALLOWED_EXTENSIONS EXISTS |
| 16 | **Bare except: handlers** | 6/10 | REAL — 20+ bare except clauses |
| 17 | **Path traversal via symlinks** | 6/10 | PARTIALLY FIXED — realpath exists but no symlink check |
| 18 | **Hardcoded FFmpeg Windows path** | 6/10 | REAL — breaks on Linux/Mac |
| 19 | **Missing ARIA/accessibility** | 6/10 | PARTIALLY FIXED — tabs have aria, forms don't |
| 20 | **Weak requirements pinning** | 6/10 | REAL — ranges not exact pins |

### 4-5/10 CONSENSUS
| # | Issue | Agents | Status |
|---|-------|--------|--------|
| 21 | Timeline state sync silent failure | 5/10 | REAL |
| 22 | No error boundaries in timeline render | 5/10 | REAL |
| 23 | Color contrast fails WCAG AA | 5/10 | REAL |
| 24 | Missing keyboard nav on canvas | 5/10 | REAL |
| 25 | No fetch timeout/AbortController | 5/10 | REAL |
| 26 | Thread race in queue processing | 4/10 | PARTIALLY REAL |
| 27 | Trim values can exceed duration | 4/10 | REAL |
| 28 | !important count still high (178) | 4/10 | REAL but cosmetic |
| 29 | 1756 inline styles | 4/10 | REAL but cosmetic |
| 30 | Modal focus trap listener leak | 4/10 | REAL |

## WHAT'S ACTUALLY BROKEN vs FALSE ALARMS

### CONFIRMED FIXES THAT AGENTS MISSED:
- Rate limiting WITH threading.Lock — EXISTS (agents searched wrong lines)
- MIME validation ALLOWED_EXTENSIONS — EXISTS
- Content-Length 500MB limit — EXISTS in _read_body
- X-Content-Type-Options + X-Frame-Options — EXISTS in _send_json/_send_file
- CSRF token validation — EXISTS on all POST/PUT/DELETE
- Plan file locking — EXISTS with _plan_file_lock
- Subprocess timeouts — ALL have timeout=300 or timeout=30
- ETag caching — EXISTS with 304 support
- Path traversal realpath — EXISTS
- Scene index bounds checks — EXIST in 5 functions
- parseInt NaN guards — EXIST in 13 locations

### ACTUALLY NEEDS FIXING (prioritized):
1. **CORS: Replace wildcard with origin whitelist** (10/10 consensus)
2. **Remove API token from query string support** (10/10 consensus)  
3. **Add CSP header** (8/10 consensus)
4. **Fix service worker cache versioning** (8/10 consensus)
5. **Add thumbnail cache eviction (LRU, max 200)** (8/10 consensus)
6. **Escape remaining innerHTML instances** (10/10 consensus, partially done)
7. **Add HTTPS support for deployment** (8/10 consensus)
8. **Replace custom multipart parser** (7/10 consensus)
9. **Add symlink check to path traversal protection** (6/10 consensus)
10. **Fix bare except clauses** (6/10 consensus)
