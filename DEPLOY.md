# LUMN Studio V6 — Beta Deployment Runbook

This document covers the steps that cannot be automated in-repo but must
be completed before sharing a public beta URL. It assumes the V6
pipeline (Gemini anchors + Kling via fal.ai + Sonnet QA) is working
locally.

Everything in the **"what's already shipped"** section is in the repo.
Everything in the **"what you must do"** section needs an external
account, DNS change, or manual configuration.

---

## 0. What's already shipped (in-repo)

| Concern | Status | Where |
|---|---|---|
| Threaded HTTP server | ✅ | `server.py` (ThreadingHTTPServer) |
| SQLite users/sessions/ledger/rate_limits | ✅ | `lib/db.py` |
| Signup / login / logout / me | ✅ | `/api/auth/*` |
| httpOnly session cookie (`lumn_sid`) | ✅ | `lib/db.py` + server |
| Per-user output namespacing (`u_<id>/`) | ✅ | `lib/db.user_output_root` |
| Per-user credits (ledger) | ✅ | `lib/db.charge_user` |
| Sliding-window rate limits | ✅ | `lib/db.rate_limit_check` |
| Content moderation pre-filter | ✅ | `lib/moderation.py` (returns 451) |
| Request metrics + structured logs | ✅ | `lib/obs.py`, `GET /api/metrics` |
| Input validation | ✅ | `lib/validate.py` on v6 handlers |
| Unit tests (prompt/linter/gate/moderation) | ✅ | `tests/test_v6_core.py` |
| Playwright visual regression | ✅ | `tools/ui_screenshot.js` |
| CI (GitHub Actions) | ✅ | `.github/workflows/ci.yml` |

To sanity-check all of the above:

```bash
python -m unittest tests/test_v6_core.py -v
LUMN_API_TOKEN=test python server.py &
node tools/ui_screenshot.js
curl -H 'Authorization: Bearer test' http://127.0.0.1:3849/api/metrics
```

---

## 1. What you must do (external accounts & infra)

These are **the blockers between you and a public beta URL**. None can
be committed to the repo — each requires logging into a third-party
dashboard or running a one-time command.

### 1.1 fal.ai production key

- [ ] Create a fal.ai account and add a billing method.
- [ ] Generate a production API key (not a personal sandbox key — the
      sandbox aggressively rate-limits).
- [ ] Set a monthly spend cap in the fal.ai dashboard to bound blast
      radius if a credit-leak bug slips through.
- [ ] Copy the key into the hosting provider's secret store as
      `FAL_KEY`.

### 1.2 Anthropic production key

- [ ] Create an Anthropic console org for the beta.
- [ ] Issue a key with a workspace-level monthly limit (suggest $50 for
      beta start).
- [ ] Store as `ANTHROPIC_API_KEY`.

### 1.3 Object storage (S3 or Cloudflare R2)

The generator writes to `output/u_<id>/...` on local disk. That won't
survive a container restart on Fly/Railway, and it won't scale past one
machine. You need a bucket.

- [ ] Create an R2 bucket `lumn-beta` (recommended — no egress fees).
- [ ] Generate an API token with read+write for that bucket only.
- [ ] Decide a retention policy (suggest: 30 days for beta).
- [ ] Set env vars: `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`,
      `S3_SECRET_KEY`, `S3_PUBLIC_BASE_URL`.
- [ ] **Not yet wired in code.** `lib/fal_client.py` currently writes
      to the local `output/` tree. Add an `upload_to_bucket(path)` step
      after each successful anchor/clip and switch the served URL to
      `S3_PUBLIC_BASE_URL/<key>`. Keep the local write as a cache.

### 1.4 Hosting (pick one)

**Option A — Fly.io** (recommended for the beta):
- [ ] `fly launch` from repo root (existing `Dockerfile` is usable).
- [ ] Create a persistent volume for `output/lumn.db` (1 GB is plenty).
- [ ] Mount at `/app/output`.
- [ ] `fly secrets set FAL_KEY=... ANTHROPIC_API_KEY=... LUMN_API_TOKEN=...`
- [ ] Set `LUMN_ADMIN_EMAILS=you@domain.com` so your account gets
      `role=admin` on signup (grants access to `/api/metrics`).

**Option B — Railway** (`railway.json` already present):
- [ ] Same env vars.
- [ ] Railway's ephemeral filesystem means you **must** finish 1.3
      (S3/R2) before going live — otherwise generated anchors disappear
      on every deploy.

### 1.5 Domain + TLS

- [ ] Buy or point a domain (e.g. `lumn.studio`).
- [ ] On Fly: `fly certs add lumn.studio` then CNAME to the fly app.
- [ ] Verify `https://lumn.studio/api/auth/me` returns 401 (not a cert
      error).

### 1.6 Stripe credits (invite-gated beta, skip for closed beta)

Not required for a **closed** beta — you can hand out accounts with
pre-seeded credits via a direct SQL write:

```sql
INSERT INTO ledger (user_id, delta_cents, kind, meta)
VALUES (1, 2000, 'grant', '{"source":"beta_invite"}');
```

For an **open** beta:
- [ ] Create Stripe account, enable test mode.
- [ ] Create a "LUMN Credits" product with three one-time prices
      ($5 / $20 / $50).
- [ ] Add `/api/billing/checkout` and `/api/billing/webhook` handlers.
      **Not yet in repo.** Webhook must verify signature and call
      `lumn_db.charge_user(user_id, +cents, "topup", meta)`.

### 1.7 Error tracking

- [ ] Create a Sentry project (free tier fine for beta).
- [ ] Add `SENTRY_DSN` env var.
- [ ] **Not yet wired.** Add `sentry-sdk` to `requirements.txt` and
      init in `server.py` before `lumn_db.init_db()`. Wrap
      `_do_GET_impl`/`_do_POST_impl` exceptions.

### 1.8 Privacy policy + ToS

Legal, not technical, but required before you can collect email
addresses.

- [ ] Draft a ToS that (a) disclaims output ownership, (b) prohibits
      generating real people or minors, (c) lists disallowed content
      categories matching `lib/moderation.py`.
- [ ] Draft a privacy policy listing what you collect (email, IP, usage
      metrics) and that generations are stored on R2 for 30 days.
- [ ] Link both from the index page footer.
- [ ] Add a signup checkbox: "I agree to the ToS and Privacy Policy".

---

## 2. Pre-launch checklist

Run through this in order the morning you flip the domain live.

- [ ] `python -m unittest tests/test_v6_core.py -v` — all green
- [ ] `node tools/ui_screenshot.js` against the staging URL — no
      non-benign JS errors
- [ ] Signup flow end-to-end: create an account via the web form,
      verify cookie set, verify `/api/auth/me` returns your user
- [ ] Generate one anchor as a fresh user — verify it writes to
      `output/u_<id>/` (or R2 key) **not** `output/shot_test/`
- [ ] Verify budget gate: set user credits to 0 via SQL, try to
      generate, expect 402
- [ ] Verify rate limit: fire 11 anchor requests in a minute, expect
      the 11th to return 429
- [ ] Verify moderation: POST `prompt: "a naked child"` → expect 451
- [ ] `curl /api/metrics` with admin Bearer token — verify by-endpoint
      breakdown populated
- [ ] Tail `fly logs` and trigger one intentional 500 — verify the
      `slow_or_error_request` structured log line appears
- [ ] First real beta user signup — watch metrics + logs live

---

## 3. Known limitations going into beta

Document these in the beta invite email so users don't file bugs for
them:

- Single-machine deployment — **do not** scale to >1 instance until
  `output/` is fully on S3/R2. SQLite is fine on one machine but the
  generated media won't be shared across replicas.
- No email verification on signup. Password reset is also not
  implemented — if a user forgets, manual reset via SQL.
- No project sharing / multi-user projects. Each account is siloed.
- Runway-era features referenced in `README_DEPLOY.md` (old doc) are
  not maintained; the V6 pipeline uses fal.ai only.
- Kling/Gemini region latency can exceed 30s per shot; the UI handles
  this but first-time users often refresh prematurely. Add a toast
  ("generation takes 20-60s, don't refresh") in a future build.

---

## 4. Post-launch (first 48 hours)

- [ ] Watch `/api/metrics` p95 per endpoint. Anything above 30s on
      anchor/generate is a fal.ai region issue, not our bug.
- [ ] Watch `_ERRORS` count by endpoint. A spike on one endpoint
      usually means a bad prompt shape — pull from structured logs.
- [ ] Check daily fal.ai spend dashboard. If it's tracking higher than
      ledger charges, there's a credit-leak bug.
- [ ] Review all 451 moderation hits for false positives — update
      `lib/moderation.py` regex guards.
- [ ] Back up `output/lumn.db` nightly via `fly ssh console -C 'sqlite3
      /app/output/lumn.db .dump' > backup.sql`.
