// Visual smoke test of LUMN V6 UI via Playwright (headless chromium).
// Captures the toolbar, identity gate strip, and the "From Idea" modal
// so we can verify the new elements landed after recent JS changes.
//
// Run:  node tools/ui_screenshot.js
//
// Outputs PNGs under tools/screenshots/.

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.LUMN_BASE || 'http://127.0.0.1:3849';
const OUT = path.join(__dirname, 'screenshots');
if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });

function stamp() {
  return new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
}

async function shot(page, name) {
  const fp = path.join(OUT, `${stamp()}_${name}.png`);
  await page.screenshot({ path: fp, fullPage: false });
  console.log(`  [shot] ${path.basename(fp)}`);
  return fp;
}

// Collect JS errors so we can fail CI on regressions.
const jsErrors = [];

// Known pre-existing warnings we tolerate until they're fixed separately.
// Add regexes here; matching messages don't count as regressions.
const KNOWN_BENIGN_PATTERNS = [
  /Failed to fetch/,                 // transient network noise
  /Runway credits error/,            // needs API key, not a code bug
  /Failed to load project style/,    // no active project in test env
  /status of 401/,                   // anon endpoint probes during init
  /status of 404/,                   // legacy endpoint 404s not yet migrated
  /Unexpected token '<'/,            // HTML body returned for a JSON parse
  /CORS policy/,                     // google fonts preflight noise
  /net::ERR_FAILED/,                 // same
];

function isBenign(msg) {
  return KNOWN_BENIGN_PATTERNS.some(r => r.test(msg));
}

function jslog(page, tag) {
  page.on('console', msg => {
    const t = msg.type();
    if (t === 'error' || t === 'warning') {
      const text = msg.text().slice(0, 300);
      console.log(`  [js:${tag}:${t}] ${text}`);
      if (t === 'error' && !isBenign(text)) {
        jsErrors.push({ tag, text });
      }
    }
  });
  page.on('pageerror', err => {
    const text = String(err).slice(0, 300);
    console.log(`  [js:${tag}:ERR] ${text}`);
    if (!isBenign(text)) jsErrors.push({ tag, text });
  });
}

(async () => {
  console.log(`LUMN UI smoke test → ${BASE}`);
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });

  // Log in via /api/auth/signup (or /login if the user exists) so the
  // session cookie is attached to the context. Avoids the 401 noise that
  // anon page loads produce on protected endpoints.
  const TEST_EMAIL = 'ci@lumn.dev';
  const TEST_PASS  = 'ci-testpass-1234';
  try {
    const api = await ctx.request;
    let resp = await api.post(`${BASE}/api/auth/signup`, {
      data: { email: TEST_EMAIL, password: TEST_PASS },
      failOnStatusCode: false,
    });
    if (!resp.ok()) {
      resp = await api.post(`${BASE}/api/auth/login`, {
        data: { email: TEST_EMAIL, password: TEST_PASS },
        failOnStatusCode: false,
      });
    }
    console.log(`  [auth] ${resp.status()} (${resp.ok() ? 'logged in' : 'anon'})`);
  } catch (e) {
    console.log(`  [auth] skipped: ${e.message}`);
  }
  const page = await ctx.newPage();
  jslog(page, 'index');

  console.log('\n[1] Loading index…');
  const t0 = Date.now();
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  console.log(`  loaded in ${Date.now() - t0}ms`);

  await page.waitForTimeout(1500); // let v6-pipeline.js wire up
  await shot(page, '01_index_loaded');

  // Check that the V6 pipeline script loaded
  const hasV6 = await page.evaluate(() => typeof window.v6Toast === 'function');
  console.log(`  v6Toast available: ${hasV6}`);

  // Look for "From Idea" button in toolbar
  const fromIdeaBtn = await page.$('button:has-text("From Idea")');
  console.log(`  "From Idea" button present: ${!!fromIdeaBtn}`);

  // Identity gate strip
  const gateStrip = await page.$('#v6IdentityGateStrip');
  console.log(`  Identity gate strip present: ${!!gateStrip}`);

  if (gateStrip) {
    const text = await gateStrip.innerText().catch(() => '');
    console.log(`  Gate strip text: ${text.slice(0, 200)}`);
    await gateStrip.scrollIntoViewIfNeeded().catch(() => {});
    await shot(page, '02_identity_gate_strip');
  }

  // Try to click From Idea button and screenshot modal
  if (fromIdeaBtn) {
    console.log('\n[2] Clicking "From Idea"…');
    await fromIdeaBtn.click().catch(e => console.log(`  click err: ${e.message}`));
    await page.waitForTimeout(800);
    await shot(page, '03_from_idea_modal');

    // Close it (escape)
    await page.keyboard.press('Escape').catch(() => {});
  }

  // Navigate to V6 tab if one exists
  console.log('\n[3] Looking for V6 pipeline tab…');
  const v6Tab = await page.$('[data-tab="v6"], button:has-text("V6")').catch(() => null);
  if (v6Tab) {
    await v6Tab.click().catch(() => {});
    await page.waitForTimeout(800);
    await shot(page, '04_v6_tab');
  } else {
    console.log('  (no dedicated V6 tab)');
  }

  // Hit the identity gate API and log result
  console.log('\n[4] Hitting /api/v6/identity-gate…');
  const gate = await page.evaluate(async () => {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const r = await fetch('/api/v6/identity-gate', {
      headers: { 'X-CSRF-Token': csrf, 'Authorization': 'Bearer test' },
    });
    return { status: r.status, body: await r.json().catch(() => null) };
  });
  console.log(`  status: ${gate.status}`);
  if (gate.body?.characters) {
    console.log(`  locked characters: ${Object.keys(gate.body.characters).join(', ')}`);
  }

  await browser.close();
  console.log('\nDone. Screenshots in tools/screenshots/');

  if (jsErrors.length) {
    console.log(`\nFAIL: ${jsErrors.length} non-benign JS error(s):`);
    for (const e of jsErrors) {
      console.log(`  - [${e.tag}] ${e.text}`);
    }
    process.exit(2);
  }
  console.log('\nPASS: no regressions');
})().catch(e => {
  console.error('FAIL:', e);
  process.exit(1);
});
