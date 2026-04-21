/**
 * V6 Pipeline UI — Gemini anchors + Kling clips via fal.ai
 * Mounts into the shots workspace as a collapsible panel.
 *
 * API endpoints:
 *   GET  /api/v6/anchors          — list all anchors with candidates
 *   GET  /api/v6/clips            — list all generated clips
 *   POST /api/v6/anchor/generate  — generate anchor via Gemini
 *   POST /api/v6/clip/generate    — generate clip via Kling
 *   POST /api/v6/sonnet/select    — Sonnet picks best candidate
 *   POST /api/v6/sonnet/review    — Sonnet reviews transition pair
 */

(function() {
  'use strict';

  // ─── Camera/lens dropdown options ───
  const CAMERA_ANGLES = [
    'Eye level', 'Low angle', 'Ground level', 'High angle',
    'Bird\'s eye', 'Dutch angle', 'Over-the-shoulder'
  ];
  const LENS_OPTIONS = [
    '24mm wide', '35mm standard', '50mm normal',
    '85mm telephoto', '135mm telephoto', '200mm telephoto'
  ];
  const SHOT_SIZES = [
    'Extreme wide', 'Wide establishing', 'Medium-wide',
    'Medium', 'Medium close-up', 'Close-up', 'Extreme close-up'
  ];
  const CAMERA_MOVEMENTS = [
    'Static / tripod', 'Handheld drift', 'Slow dolly in',
    'Slow dolly out', 'Tracking left', 'Tracking right',
    'Slow push in', 'Crane up', 'Crane down',
    'Pan left', 'Pan right', 'Orbit / arc', 'Whip pan'
  ];
  const DOF_OPTIONS = [
    'Deep (everything sharp)', 'Medium', 'Shallow f/2.8',
    'Very shallow f/1.4', 'Rack focus'
  ];
  // Derive UI defaults (shotSize/lens/dof) from scenes.json cameraAngle +
  // cameraMovement text so shot cards pre-fill coherent with narrative.
  // Tightest match wins: ECU > Close-up > MCU > Medium > Wide > Extreme wide.
  function defaultsFromCameraAngle(raw1, raw2) {
    const t = (String(raw1 || '') + ' ' + String(raw2 || '')).toLowerCase();
    if (/\becu\b|extreme close/.test(t)) {
      return { shotSize: 'Extreme close-up', lens: '135mm telephoto', dof: 'Very shallow f/1.4' };
    }
    if (/\bcu\b|close-up|closeup|\bclose\b/.test(t) && !/\bmcu\b/.test(t)) {
      return { shotSize: 'Close-up', lens: '85mm telephoto', dof: 'Very shallow f/1.4' };
    }
    if (/\bmcu\b|medium close/.test(t)) {
      return { shotSize: 'Medium close-up', lens: '85mm telephoto', dof: 'Shallow f/2.8' };
    }
    if (/\bews\b|extreme wide|establishing wide/.test(t)) {
      return { shotSize: 'Extreme wide', lens: '24mm wide', dof: 'Deep (everything sharp)' };
    }
    if (/\bws\b|wide|establish/.test(t)) {
      return { shotSize: 'Wide establishing', lens: '24mm wide', dof: 'Deep (everything sharp)' };
    }
    if (/medium-wide|\bmws\b/.test(t)) {
      return { shotSize: 'Medium-wide', lens: '35mm standard', dof: 'Medium' };
    }
    if (/medium/.test(t)) {
      return { shotSize: 'Medium', lens: '50mm normal', dof: 'Medium' };
    }
    return { shotSize: 'Medium', lens: '50mm normal', dof: 'Medium' };
  }
  const KLING_TIERS = [
    { value: 'v3_standard', label: 'V3 Standard ($0.084/s)' },
    { value: 'v3_pro', label: 'V3 Pro ($0.112/s)' },
    { value: 'o3_standard', label: 'O3 Standard ($0.084/s)' },
    { value: 'o3_pro', label: 'O3 Pro ($0.392/s)' },
  ];
  const KLING_COST_PER_SEC = {
    v3_standard: 0.084, v3_pro: 0.112,
    o3_standard: 0.084, o3_pro: 0.392,
  };
  function estimateKlingCost(tier, durationSec) {
    return +((KLING_COST_PER_SEC[tier] || 0.084) * (durationSec || 5)).toFixed(3);
  }

  // ─── State ───
  let anchors = [];
  let clips = [];
  let gates = { shots: {}, summary: {} };

  // ─── Helpers ───
  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(k => {
      const v = attrs[k];
      if (v === undefined || v === null || v === false) return;
      if (k === 'style' && typeof v === 'object') {
        Object.assign(e.style, v);
      } else if (k.startsWith('on')) {
        e.addEventListener(k.slice(2), v);
      } else if (v === true) {
        e.setAttribute(k, '');
      } else {
        e.setAttribute(k, v);
      }
    });
    if (children) {
      if (typeof children === 'string') e.innerHTML = children;
      else if (Array.isArray(children)) children.forEach(c => c && e.appendChild(c));
      else e.appendChild(children);
    }
    return e;
  }
  function select(name, options, selected) {
    const s = el('select', { name, style: 'font-size:10px;padding:4px 6px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);' });
    options.forEach(o => {
      const opt = el('option', { value: typeof o === 'object' ? o.value : o }, typeof o === 'object' ? o.label : o);
      if ((typeof o === 'object' ? o.value : o) === selected) opt.selected = true;
      s.appendChild(opt);
    });
    return s;
  }
  async function api(method, path, body) {
    const csrf = document.querySelector('meta[name="csrf-token"]');
    const opts = { method, headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': csrf ? csrf.content : '',
    }};
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      let details = null;
      try { details = await r.json(); if (details && details.error) msg = details.error; } catch (_) {}
      const err = new Error(msg);
      err.status = r.status;
      err.details = details;
      throw err;
    }
    return r.json();
  }

  // Run a long-running generation via the async worker queue + SSE progress.
  // POSTs to `${asyncPath}` (must return {job_id}), then subscribes to
  // /api/jobs/<id>/stream and calls onStage({stage, progress}) on each tick.
  // Resolves with the final result object, or throws on failure.
  async function runJob(asyncPath, body, onStage) {
    const kick = await api('POST', asyncPath, body);
    const jobId = kick && kick.job_id;
    if (!jobId) throw new Error('no job_id from server');
    return new Promise((resolve, reject) => {
      const es = new EventSource(`/api/jobs/${jobId}/stream`);
      let settled = false;
      const done = (fn, v) => { if (settled) return; settled = true; try { es.close(); } catch(_){} fn(v); };
      es.onmessage = (ev) => {
        let j; try { j = JSON.parse(ev.data); } catch (_) { return; }
        if (onStage) { try { onStage(j); } catch (_){} }
        if (j.status === 'done') done(resolve, j.result || j);
        else if (j.status === 'failed') {
          const e = new Error(j.error || 'job failed');
          e.job = j;
          done(reject, e);
        }
      };
      es.onerror = () => {
        // EventSource auto-reconnects; only bail if nothing ever arrived.
        setTimeout(() => { if (!settled) done(reject, new Error('stream lost')); }, 10000);
      };
    });
  }

  // --- V6 feedback toast — non-blocking inline notification ---
  // Used by the pipeline to show assembler/linter/QA results. Auto-dismisses
  // after 8s unless level='error', which stays until clicked.
  function v6Toast(msg, level) {
    level = level || 'info';
    let host = document.getElementById('v6-toast-host');
    if (!host) {
      host = el('div', { id: 'v6-toast-host', style:
        'position:fixed;bottom:16px;right:16px;display:flex;flex-direction:column;gap:6px;z-index:9999;max-width:460px;pointer-events:none;' });
      document.body.appendChild(host);
    }
    const color = level === 'error' ? 'var(--red)' :
                  level === 'warn'  ? 'var(--amber)' :
                  level === 'ok'    ? 'var(--green)' : 'var(--cyan)';
    const card = el('div', {
      style: `background:var(--surface);border:1px solid ${color};border-radius:6px;padding:8px 12px;font-size:11px;color:var(--text);box-shadow:var(--shadow-panel);pointer-events:auto;cursor:pointer;`,
      onclick: () => card.remove(),
    }, msg);
    host.appendChild(card);
    if (level !== 'error') setTimeout(() => card.remove(), 8000);
    return card;
  }
  // Expose for other modules / console debugging.
  window.v6Toast = v6Toast;

  // ─── Shared accessible modal helper (P0-4) ───
  // Wraps any dynamically-built modal with role=dialog, aria-modal, focus trap,
  // ESC handler, backdrop-click close, and auto-focus of the first focusable element.
  // Usage: const ctl = v6Modal({ backdrop, modal, labelText, onClose });
  //        ctl.close() — programmatic close; fires onClose
  function v6Modal({ backdrop, modal, labelText, onClose, initialFocusSelector }) {
    backdrop.setAttribute('role', 'presentation');
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    if (labelText) modal.setAttribute('aria-label', labelText);
    modal.setAttribute('tabindex', '-1');

    const prevFocus = document.activeElement;
    let closed = false;

    function getFocusable() {
      return Array.from(modal.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter(e => e.offsetParent !== null || e === document.activeElement);
    }

    function onKey(e) {
      if (closed) return;
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key !== 'Tab') return;
      const f = getFocusable();
      if (!f.length) { e.preventDefault(); modal.focus(); return; }
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    function onBackdropClick(e) { if (e.target === backdrop) close(); }

    function close() {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKey, true);
      backdrop.removeEventListener('click', onBackdropClick);
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      try { if (prevFocus && prevFocus.focus) prevFocus.focus(); } catch (_) {}
      if (onClose) onClose();
    }

    document.addEventListener('keydown', onKey, true);
    backdrop.addEventListener('click', onBackdropClick);
    document.body.appendChild(backdrop);

    // Defer focus to next tick so layout settles
    setTimeout(() => {
      const explicit = initialFocusSelector ? modal.querySelector(initialFocusSelector) : null;
      const f = getFocusable();
      (explicit || f[0] || modal).focus();
    }, 0);

    return { close };
  }

  // ─── Cost preflight modal ───
  // Returns a Promise<boolean> — true if user confirms, false if cancelled.
  // Remembers "always confirm <threshold>" in localStorage so cheap jobs skip the modal.
  function confirmCost({ title, items, shotId }) {
    return new Promise(resolve => {
      const total = items.reduce((s, it) => s + (it.cost || 0), 0);
      // Auto-approve if under the user's saved skip threshold
      const skipUnder = parseFloat(localStorage.getItem('v6_cost_skip_under') || '0');
      if (skipUnder > 0 && total <= skipUnder) { resolve(true); return; }

      const backdrop = el('div', { style: 'position:fixed;inset:0;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);z-index:9999;display:flex;align-items:center;justify-content:center;' });
      const modal = el('div', { style: 'background:var(--surface);border:1px solid var(--cyan);border-radius:8px;padding:20px;min-width:320px;max-width:440px;box-shadow:0 20px 60px rgba(0,220,255,0.15);' });

      modal.appendChild(el('div', { style: 'font-size:11px;font-weight:700;color:var(--cyan);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px;' }, title || 'Cost Preflight'));
      if (shotId) modal.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);margin-bottom:12px;' }, 'Shot: ' + shotId));

      const list = el('div', { style: 'margin:12px 0;padding:10px;background:var(--surface2);border-radius:4px;' });
      items.forEach(it => {
        const row = el('div', { style: 'display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px;' });
        row.appendChild(el('span', { style: 'color:var(--text);' }, it.label));
        row.appendChild(el('span', { style: 'color:var(--green);font-family:JetBrains Mono,monospace;' }, '$' + (it.cost || 0).toFixed(3)));
        list.appendChild(row);
      });
      const totalRow = el('div', { style: 'display:flex;justify-content:space-between;font-size:13px;font-weight:700;margin-top:8px;padding-top:8px;border-top:1px solid var(--border);' });
      totalRow.appendChild(el('span', {}, 'Total'));
      totalRow.appendChild(el('span', { style: 'color:var(--amber);font-family:JetBrains Mono,monospace;' }, '$' + total.toFixed(3)));
      list.appendChild(totalRow);
      modal.appendChild(list);

      // Skip-under-threshold checkbox
      const skipWrap = el('label', { style: 'display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-dim);margin:8px 0;cursor:pointer;' });
      const skipCb = el('input', { type: 'checkbox' });
      if (skipUnder >= total) skipCb.checked = true;
      skipWrap.appendChild(skipCb);
      skipWrap.appendChild(el('span', {}, `Auto-approve jobs ≤ $${total.toFixed(2)} (don't ask again for this price)`));
      modal.appendChild(skipWrap);

      const btnRow = el('div', { style: 'display:flex;gap:8px;justify-content:flex-end;margin-top:12px;' });
      const cancelBtn = el('button', { class: 'btn btn-small', style: 'font-size:10px;' }, 'Cancel');
      const confirmBtn = el('button', { class: 'btn btn-small', style: 'font-size:10px;border-color:var(--green);color:var(--green);' }, 'Confirm');
      btnRow.appendChild(cancelBtn);
      btnRow.appendChild(confirmBtn);
      modal.appendChild(btnRow);

      backdrop.appendChild(modal);

      let resolvedVal = false;
      const ctl = v6Modal({
        backdrop, modal,
        labelText: (title || 'Cost Preflight') + (shotId ? ' for shot ' + shotId : ''),
        onClose: () => {
          if (skipCb.checked) localStorage.setItem('v6_cost_skip_under', String(total));
          resolve(resolvedVal);
        },
      });
      cancelBtn.addEventListener('click', () => { resolvedVal = false; ctl.close(); });
      confirmBtn.addEventListener('click', () => { resolvedVal = true; ctl.close(); });
      // Enter = confirm
      modal.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); resolvedVal = true; ctl.close(); }
      });
      setTimeout(() => confirmBtn.focus(), 0);
    });
  }

  // ─── Sonnet pick A/B modal ───
  // Shows all candidates side-by-side with Sonnet's pick highlighted.
  // User can accept Sonnet's pick or override by clicking a different candidate.
  function showSonnetPickModal({ shotId, pick, reason, confidence, candidates, scores }) {
    return new Promise(resolve => {
      const backdrop = el('div', { style: 'position:fixed;inset:0;background:rgba(0,0,0,0.85);backdrop-filter:blur(10px);z-index:9999;display:flex;align-items:center;justify-content:center;padding:40px;' });
      const modal = el('div', { style: 'background:var(--surface);border:1px solid var(--violet);border-radius:8px;padding:20px;max-width:960px;width:100%;max-height:92vh;overflow-y:auto;box-shadow:0 20px 60px rgba(180,80,255,0.2);' });

      modal.appendChild(el('div', { style: 'font-size:11px;font-weight:700;color:var(--violet);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px;' }, 'Sonnet Pick — ' + shotId));
      if (confidence != null) {
        modal.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);margin-bottom:4px;' }, 'Confidence: ' + (typeof confidence === 'number' ? (confidence * 100).toFixed(0) + '%' : confidence)));
      }
      if (reason) {
        modal.appendChild(el('div', { style: 'font-size:10px;color:var(--text);margin-bottom:12px;padding:8px;background:var(--surface2);border-left:2px solid var(--violet);border-radius:2px;font-style:italic;' }, reason));
      }

      const grid = el('div', { style: 'display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:12px 0;' });
      let selected = pick;

      const cards = [];
      candidates.forEach((c, i) => {
        const isPick = c === pick || c.endsWith(pick) || pick.endsWith(c.split('/').pop() || '');
        const card = el('div', {
          style: 'position:relative;border:2px solid ' + (isPick ? 'var(--violet)' : 'var(--border)') + ';border-radius:6px;padding:6px;cursor:pointer;transition:all 0.2s;background:var(--surface2);',
        });
        card.addEventListener('click', () => {
          selected = c;
          cards.forEach(cc => cc.style.borderColor = 'var(--border)');
          card.style.borderColor = 'var(--green)';
        });
        card.addEventListener('mouseenter', () => { if (selected !== c) card.style.borderColor = 'var(--cyan)'; });
        card.addEventListener('mouseleave', () => {
          if (selected === c) card.style.borderColor = 'var(--green)';
          else if (isPick && selected === pick) card.style.borderColor = 'var(--violet)';
          else card.style.borderColor = 'var(--border)';
        });

        card.appendChild(el('img', { src: c, style: 'width:100%;border-radius:4px;display:block;' }));
        const labelRow = el('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-top:6px;' });
        labelRow.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);' }, 'Candidate ' + (i + 1)));
        if (isPick) labelRow.appendChild(el('div', { style: 'font-size:8px;color:var(--violet);font-weight:700;', 'aria-label': 'Sonnet pick' }, '★ SONNET PICK'));
        card.appendChild(labelRow);

        // Scores if Sonnet returned per-candidate scores
        const fname = c.split('/').pop();
        const score = scores[fname] || scores[c];
        if (score && typeof score === 'object') {
          const sRow = el('div', { style: 'font-size:8px;color:var(--text-dim);margin-top:4px;display:flex;gap:6px;flex-wrap:wrap;' });
          Object.keys(score).slice(0, 4).forEach(k => {
            sRow.appendChild(el('span', {}, k + ':' + score[k]));
          });
          card.appendChild(sRow);
        }
        cards.push(card);
        grid.appendChild(card);
      });
      modal.appendChild(grid);

      const btnRow = el('div', { style: 'display:flex;gap:8px;justify-content:flex-end;margin-top:12px;padding-top:12px;border-top:1px solid var(--border);' });
      const cancelBtn = el('button', { class: 'btn btn-small', style: 'font-size:10px;' }, 'Cancel');
      const acceptBtn = el('button', { class: 'btn btn-small', style: 'font-size:10px;border-color:var(--green);color:var(--green);' }, 'Accept Selection');
      btnRow.appendChild(cancelBtn);
      btnRow.appendChild(acceptBtn);
      modal.appendChild(btnRow);

      backdrop.appendChild(modal);
      let resolvedVal = null;
      const ctl = v6Modal({
        backdrop, modal,
        labelText: 'Sonnet pick for shot ' + shotId,
        onClose: () => resolve(resolvedVal),
      });
      cancelBtn.addEventListener('click', () => { resolvedVal = null; ctl.close(); });
      acceptBtn.addEventListener('click', async () => {
        try {
          await api('POST', '/api/v6/sonnet/override', { shot_id: shotId, selected });
        } catch (err) {
          console.warn('[V6] Override failed (non-fatal):', err.message);
        }
        resolvedVal = selected;
        ctl.close();
      });
    });
  }

  // ─── Build prompt from structured fields ───
  function buildAnchorPrompt(fields) {
    const parts = [];
    // Scene beat first — grounds Gemini in what's happening before camera specs
    // arrive. Without this, sparse camera-only prompts drift (v8 shot 1a hit
    // 2/3 vs 3/3 after enrichment).
    if (fields.shotDescription) {
      const sd = String(fields.shotDescription).trim();
      parts.push(sd.length > 450 ? sd.slice(0, 447) + '...' : sd);
    }
    if (fields.emotion) parts.push('Emotion: ' + fields.emotion + '.');
    if (fields.narrativeIntent) parts.push('Intent: ' + fields.narrativeIntent + '.');
    const camBits = [];
    if (fields.shotSize) camBits.push(fields.shotSize + ' shot');
    if (fields.lens) camBits.push(fields.lens + ' lens');
    if (fields.cameraAngle) camBits.push(fields.cameraAngle.toLowerCase());
    if (fields.dof) camBits.push(fields.dof);
    if (camBits.length) parts.push(camBits.join(' ') + '.');
    if (fields.subjectAction) parts.push(fields.subjectAction + '.');
    if (fields.environment) parts.push(fields.environment + '.');
    if (fields.lighting) parts.push(fields.lighting + '.');
    return parts.join(' ').replace(/ \./g, '.').replace(/\.\./g, '.').trim();
  }

  function buildVideoPrompt(fields) {
    const parts = [];
    // Suppress the generic camera-move prefix when the Motion field already describes camera motion.
    // Motion is the single camera-sentence source when it starts with a camera-motion verb.
    const CAM_VERBS = /^(static|slow|handheld|pan|tilt|push|pull|dolly|tracking|track|crane|orbit|whip|zoom|rack|jib|steady|push-in|pull-back|pull-out)\b/i;
    const motion = (fields.subjectMotion || '').trim();
    const motionOwnsCamera = motion && CAM_VERBS.test(motion);
    if (fields.cameraMovement && !motionOwnsCamera) {
      parts.push('Camera ' + fields.cameraMovement.toLowerCase().replace('static / tripod', 'on tripod, fixed') + '.');
    }
    if (motion) parts.push(motion + '.');
    if (fields.envMotion) parts.push(fields.envMotion + '.');
    return parts.join(' ').trim();
  }

  // ─── Gate helpers ──────────────────────────────────────────────────
  // Section color mapping — matches refreshSongTiming() colors so the
  // gate strip reads at a glance alongside the song-timing card.
  const SECTION_COLORS = {
    intro: 'var(--violet)',
    outro: 'var(--violet)',
    verse_1: 'var(--cyan)',
    verse_2: 'var(--cyan)',
    verse_3: 'var(--cyan)',
    chorus_1: 'var(--magenta)',
    chorus_2: 'var(--magenta)',
    bridge: 'var(--amber)',
  };

  function gateDot(state, label, tip) {
    // state: 'ok' | 'fail' | 'pending' | 'disabled'
    const color = state === 'ok' ? 'var(--green)'
                : state === 'fail' ? 'var(--red)'
                : state === 'disabled' ? 'var(--text-dim)'
                : 'var(--amber)';
    const glyph = state === 'ok' ? '✓' : state === 'fail' ? '✗' : '○';
    return el('span', {
      title: tip || label,
      style: `display:inline-flex;align-items:center;gap:3px;font-size:9px;color:${color};padding:2px 6px;border:1px solid ${color}40;border-radius:10px;background:${color}10;`,
    }, glyph + ' ' + label);
  }

  function buildGateStrip(shotId) {
    const gate = (gates.shots || {})[shotId];
    const strip = el('div', {
      style: 'display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px;padding:6px 8px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);'
    });
    if (!gate) {
      strip.appendChild(el('span', { style: 'font-size:9px;color:var(--text-dim);' }, 'Gates: not synced — click Sync Gates'));
      return strip;
    }
    // Section badge
    const section = gate.section || '?';
    const secColor = SECTION_COLORS[section] || 'var(--text-dim)';
    strip.appendChild(el('span', {
      style: `font-size:9px;font-weight:700;padding:2px 8px;border-radius:10px;background:${secColor}20;color:${secColor};text-transform:uppercase;letter-spacing:0.5px;`,
    }, section));
    if (gate.start_sec != null) {
      strip.appendChild(el('span', {
        style: 'font-size:9px;color:var(--text-dim);',
      }, `${gate.start_sec.toFixed(1)}–${(gate.end_sec||0).toFixed(1)}s`));
    }
    if (gate.lyric_text) {
      const lyricShort = gate.lyric_text.length > 50 ? gate.lyric_text.slice(0, 50) + '…' : gate.lyric_text;
      strip.appendChild(el('span', {
        style: 'font-size:9px;color:var(--cyan);font-style:italic;',
        title: gate.lyric_text,
      }, '♫ ' + lyricShort));
    }
    // Separator
    strip.appendChild(el('span', { style: 'flex:1;' }));

    // 4 gate dots
    const g = gate.gates || {};
    strip.appendChild(gateDot(g.anchor_generated ? 'ok' : 'pending', 'anchor',
      g.anchor_generated ? 'Anchor PNG on disk' : 'No anchor yet'));
    const auditState = g.audit_passed === true ? 'ok'
                     : g.audit_passed === false ? 'fail' : 'pending';
    const auditTip = g.audit_summary
      ? `Audit: ${g.audit_summary}`
      : (auditState === 'pending' ? 'Click Audit Anchor' : 'No audit data');
    strip.appendChild(gateDot(auditState, 'audit', auditTip));
    strip.appendChild(gateDot(g.clip_rendered ? 'ok' : 'pending', 'clip',
      g.clip_rendered ? 'Clip on disk' : 'Click Generate Clip'));
    const motionState = g.motion_review_passed === true ? 'ok'
                      : g.motion_review_passed === false ? 'fail' : 'pending';
    strip.appendChild(gateDot(motionState, 'motion',
      g.motion_review_notes || 'Watch clip, then mark motion review'));
    strip.appendChild(gateDot(g.signed_off ? 'ok' : 'disabled', 'ready',
      g.signed_off ? `Signed off by ${g.signed_off_by || 'human'}` : 'Sign off after audit + motion pass'));

    return strip;
  }

  async function setShotGate(shotId, gate, value, notes) {
    try {
      await api('POST', '/api/v6/shots/gates/set', {
        project: 'default',
        shot_id: shotId,
        gate,
        value,
        notes: notes || '',
      });
      await refreshGates();
    } catch (err) {
      v6Toast(`Gate update failed: ${err.message}`, 'error');
    }
  }

  function renderGateBatchBar() {
    const host = $('v6GateBatchBar');
    if (!host) return;
    host.innerHTML = '';
    const s = gates.summary || {};
    const total = s.total || 0;
    if (!total) {
      host.appendChild(el('div', {
        style: 'font-size:9px;color:var(--text-dim);padding:6px;',
      }, 'No gates yet — click Sync Gates to derive from song timing.'));
      return;
    }
    const wrap = el('div', {
      style: 'background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;margin-bottom:10px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;',
    });
    wrap.appendChild(el('div', {
      style: 'font-size:10px;font-weight:700;color:var(--cyan);text-transform:uppercase;letter-spacing:1px;',
    }, 'Shot Gates'));

    // Counter pills
    function pill(label, n, color) {
      return el('span', {
        style: `font-size:9px;color:${color};padding:2px 8px;border:1px solid ${color}40;border-radius:10px;background:${color}10;`,
      }, `${label} ${n}/${total}`);
    }
    wrap.appendChild(pill('anchor', s.anchor_generated || 0, 'var(--cyan)'));
    wrap.appendChild(pill('audit✓', s.audit_passed || 0, 'var(--green)'));
    if (s.audit_failed) wrap.appendChild(pill('audit✗', s.audit_failed, 'var(--red)'));
    wrap.appendChild(pill('clip', s.clip_rendered || 0, 'var(--cyan)'));
    wrap.appendChild(pill('motion✓', s.motion_passed || 0, 'var(--green)'));
    wrap.appendChild(pill('ready', s.signed_off || 0, 'var(--magenta)'));

    // Spacer
    wrap.appendChild(el('div', { style: 'flex:1;' }));

    // Actions
    const syncBtn = el('button', {
      class: 'btn btn-small',
      style: 'font-size:9px;',
      title: 'Re-derive section/lyric map from timing.json and reconcile with disk',
      onclick: async () => {
        syncBtn.disabled = true; syncBtn.textContent = 'Syncing…';
        try {
          await api('POST', '/api/v6/shots/gates/sync', { project: 'default' });
          await refreshGates();
          v6Toast('Gates synced with song timing', 'ok');
        } catch (err) { v6Toast(`Sync failed: ${err.message}`, 'error'); }
        finally { syncBtn.disabled = false; syncBtn.textContent = 'Sync Gates'; }
      },
    }, 'Sync Gates');
    wrap.appendChild(syncBtn);

    const auditAllBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--amber);color:var(--amber);font-size:9px;',
      title: `Audit all ${s.anchor_generated || 0} anchors via Sonnet (~$${((s.anchor_generated || 0) * 0.02).toFixed(2)})`,
      onclick: async () => {
        const cost = ((s.anchor_generated || 0) * 0.02).toFixed(2);
        if (!confirm(`Audit all ${s.anchor_generated || 0} anchors for ~$${cost}?`)) return;
        auditAllBtn.disabled = true; auditAllBtn.textContent = 'Auditing…';
        try {
          const res = await api('POST', '/api/v6/shots/gates/audit-all', { project: 'default' });
          const passed = (res.results || []).filter(r => r.pass).length;
          const failed = (res.results || []).filter(r => r.pass === false).length;
          v6Toast(`Audit complete: ${passed} pass / ${failed} fail`, passed > 0 ? 'ok' : 'error');
          await refreshGates();
        } catch (err) { v6Toast(`Audit-all failed: ${err.message}`, 'error'); }
        finally { auditAllBtn.disabled = false; auditAllBtn.textContent = 'Audit All Anchors'; }
      },
    }, 'Audit All Anchors');
    wrap.appendChild(auditAllBtn);

    // Sign-off-all-passing: signs off every shot with audit_passed AND clip_rendered
    const eligibleForSignoff = Object.values(gates.shots || {}).filter(sh => {
      const gg = sh.gates || {};
      return gg.audit_passed === true && gg.clip_rendered && !gg.signed_off;
    });
    const signOffAllBtn = el('button', {
      class: 'btn btn-small',
      style: `border-color:var(--magenta);color:var(--magenta);font-size:9px;${eligibleForSignoff.length ? '' : 'opacity:0.4;pointer-events:none;'}`,
      title: eligibleForSignoff.length
        ? `Sign off ${eligibleForSignoff.length} shots where anchor passed audit and clip is on disk`
        : 'No shots are eligible yet — run audit first',
      onclick: async () => {
        if (!eligibleForSignoff.length) return;
        signOffAllBtn.disabled = true; signOffAllBtn.textContent = 'Signing…';
        try {
          for (const sh of eligibleForSignoff) {
            await api('POST', '/api/v6/shots/gates/set', {
              project: 'default', shot_id: sh.shot_id,
              gate: 'signed_off', value: true, actor: 'human-batch',
            });
          }
          await refreshGates();
          v6Toast(`Signed off ${eligibleForSignoff.length} shots`, 'ok');
        } catch (err) { v6Toast(`Batch sign-off failed: ${err.message}`, 'error'); }
        finally { signOffAllBtn.disabled = false; signOffAllBtn.textContent = 'Sign Off All Passing'; }
      },
    }, `Sign Off All Passing${eligibleForSignoff.length ? ` (${eligibleForSignoff.length})` : ''}`);
    wrap.appendChild(signOffAllBtn);

    const stitchBtn = el('button', {
      class: 'btn btn-small',
      style: `border-color:var(--green);color:var(--green);font-size:9px;${s.signed_off ? '' : 'opacity:0.4;pointer-events:none;'}`,
      title: s.signed_off
        ? `Stitch ${s.signed_off} signed-off shots into final MV`
        : 'Sign off at least one shot first',
      onclick: async () => {
        if (!s.signed_off) return;
        stitchBtn.disabled = true; stitchBtn.textContent = 'Stitching…';
        try {
          const res = await api('POST', '/api/v6/stitch', {
            project: 'default',
            only_signed_off: true,
            audio_path: 'C:/Users/Mathe/Downloads/Lifestream Static.mp3',
            output_name: `tb_v2_gated_${Date.now()}.mp4`,
          });
          v6Toast(`Stitched ${res.clip_count} clips → ${res.output_url}`, 'ok');
          window.open(res.output_url, '_blank');
        } catch (err) { v6Toast(`Stitch failed: ${err.message}`, 'error'); }
        finally { stitchBtn.disabled = false; stitchBtn.textContent = 'Stitch Signed-Off'; }
      },
    }, `Stitch Signed-Off${s.signed_off ? ` (${s.signed_off})` : ''}`);
    wrap.appendChild(stitchBtn);

    const pacingBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--cyan);color:var(--cyan);font-size:9px;',
      title: 'F5: recommend cut density per section (intro/build/climax/outro) from music grid',
      onclick: async () => {
        pacingBtn.disabled = true;
        const orig = pacingBtn.textContent;
        pacingBtn.textContent = 'Planning…';
        try {
          const res = await api('POST', '/api/v6/pacing-arc', { style: 'arc', persist: true });
          const lines = (res.sections || []).map(sec =>
            `  [${sec.index}] ${sec.label.padEnd(11)} ${sec.duration_s}s → ${sec.suggested_cuts} cuts @ ${sec.target_cut_duration_s}s each`
          );
          const body = [
            `Pacing Arc (${res.total_suggested_cuts} cuts across ${res.sections.length} sections)`,
            `Profile: ${res.intensity_profile.join(' → ')}`,
            `Tempo: ${res.tempo_bpm} bpm (${res.bar_s}s/bar)`,
            '',
            ...lines,
            '',
            `${res.recommendation}`,
            res.persisted_path ? `Saved: ${res.persisted_path}` : '',
          ].filter(Boolean).join('\n');
          v6Toast(`F5: ${res.total_suggested_cuts} cuts suggested`, 'ok');
          const pre = document.getElementById('v6-pacing-output') || (function () {
            const p = el('pre', {
              id: 'v6-pacing-output',
              style: 'background:var(--surface);border:1px solid var(--cyan);border-radius:var(--radius);padding:10px;margin:10px 0;color:var(--text);font-size:10px;white-space:pre-wrap;max-height:260px;overflow:auto;',
            });
            host.appendChild(p);
            return p;
          })();
          pre.textContent = body;
        } catch (err) {
          v6Toast(`Pacing failed: ${err.message}`, 'error');
        } finally {
          pacingBtn.disabled = false;
          pacingBtn.textContent = orig;
        }
      },
    }, 'Pacing Arc');
    wrap.appendChild(pacingBtn);

    const planDurBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--cyan);color:var(--cyan);font-size:9px;',
      title: 'Dynamic shot duration planner: map camera/emotion/beat/energy to per-shot 3-15s window',
      onclick: async () => {
        planDurBtn.disabled = true;
        const orig = planDurBtn.textContent;
        planDurBtn.textContent = 'Planning…';
        try {
          const res = await api('POST', '/api/v6/shots/plan-durations', { apply: false });
          const plan = res.plan || [];
          const groups = {};
          for (const p of plan) {
            const key = p.group_id || p.opus_scene_id || '?';
            (groups[key] = groups[key] || []).push(p);
          }
          const lines = [];
          for (const [gid, shots] of Object.entries(groups)) {
            const total = shots.reduce((a, s) => a + (s.duration_s || 0), 0);
            lines.push(`  Scene ${gid}  (${total}s total)`);
            for (const s of shots) {
              const sid = s.opus_shot_id || s.scene_id || '?';
              lines.push(`    ${sid.padEnd(4)} ${String(s.duration_s).padStart(2)}s  ${s.rationale || ''}`);
            }
          }
          const body = [
            `Duration Plan (preview) — ${res.total_shots} shots, ${res.total_seconds}s total`,
            '',
            ...lines,
            '',
            'Review above. Click again with APPLY to bake into scenes.json.',
          ].join('\n');
          const pre = document.getElementById('v6-plandur-output') || (function () {
            const p = el('pre', {
              id: 'v6-plandur-output',
              style: 'background:var(--surface);border:1px solid var(--cyan);border-radius:var(--radius);padding:10px;margin:10px 0;color:var(--text);font-size:10px;white-space:pre-wrap;max-height:320px;overflow:auto;',
            });
            host.appendChild(p);
            return p;
          })();
          pre.textContent = body;
          v6Toast(`Plan: ${res.total_shots} shots, ${res.total_seconds}s total`, 'ok');
          if (confirm(`Apply this plan to scenes.json?\n${res.total_shots} shots, ${res.total_seconds}s total.\nThis will overwrite duration/duration_s fields on each scene.`)) {
            planDurBtn.textContent = 'Applying…';
            const appliedRes = await api('POST', '/api/v6/shots/plan-durations', { apply: true });
            v6Toast(`Applied to ${appliedRes.scenes_path ? 'scenes.json' : '(unknown)'}`, 'ok');
            pre.textContent = body + '\n\nAPPLIED at ' + new Date().toLocaleTimeString();
          }
        } catch (err) {
          v6Toast(`Plan Durations failed: ${err.message}`, 'error');
        } finally {
          planDurBtn.disabled = false;
          planDurBtn.textContent = orig;
        }
      },
    }, 'Plan Durations');
    wrap.appendChild(planDurBtn);

    const directAllBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--magenta);color:var(--magenta);font-size:9px;',
      title: 'Opus director pass on every shot: beat-level acting, motivated camera, shot-specific lighting',
      onclick: async () => {
        const shotCount = (document.querySelectorAll('.v6-shot-card') || []).length;
        const estCost = (shotCount * 0.12).toFixed(2);
        if (!confirm(`Direct all ${shotCount} shots? This rewrites Subject/Lighting/Motion/Env for every shot card via Opus.\n\nEstimated cost: ~$${estCost}\n\nOriginals are preserved under director_v2.original in scenes.json — click a shot's Direct button later to re-run.`)) return;
        directAllBtn.disabled = true;
        const orig = directAllBtn.textContent;
        directAllBtn.textContent = 'Directing all…';
        try {
          const res = await api('POST', '/api/v6/director/direct-all', { apply: true });
          const pre = document.getElementById('v6-direct-output') || (function () {
            const p = el('pre', {
              id: 'v6-direct-output',
              style: 'background:var(--surface);border:1px solid var(--magenta);border-radius:var(--radius);padding:10px;margin:10px 0;color:var(--text);font-size:10px;white-space:pre-wrap;max-height:320px;overflow:auto;',
            });
            host.appendChild(p);
            return p;
          })();
          const lines = (res.results || []).map(r => {
            const p = r.preview || {};
            return r.ok
              ? `  ✓ ${r.shot_id}  Subj="${(p.subjectAction || '').slice(0, 60)}"`
              : `  ✗ ${r.shot_id}  ${r.error || ''}`;
          });
          pre.textContent = [
            `Director Pass — ${res.passed}/${res.total} succeeded (${res.failed} failed)`,
            '',
            ...lines,
          ].join('\n');
          v6Toast(`Direct-all: ${res.passed}/${res.total} rewritten`, res.failed ? 'warn' : 'ok');
          // Re-render cards so the new populated fields show.
          await refreshAnchors();
        } catch (err) {
          const d = err.details || {};
          if (err.status === 402) v6Toast(`Budget blocked: ${d.reason || err.message}`, 'error');
          else v6Toast(`Direct-all failed: ${err.message}`, 'error');
        } finally {
          directAllBtn.disabled = false;
          directAllBtn.textContent = orig;
        }
      },
    }, '🎬 Direct All');
    wrap.appendChild(directAllBtn);

    const varietyBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--amber);color:var(--amber);font-size:9px;',
      title: 'Scan all shots for verb/lighting/camera repetition. No LLM call — pure analysis.',
      onclick: async () => {
        varietyBtn.disabled = true;
        const orig = varietyBtn.textContent;
        varietyBtn.textContent = 'Scanning…';
        try {
          const res = await api('POST', '/api/v6/director/variety-check', {});
          const r = (res.report || {});
          const sum = r.summary || {};
          const fields = r.by_field || {};
          const flagged = r.flagged_shots || [];

          const lines = [];
          lines.push(`Variety Check — ${sum.verdict || '?'} · diversity ${sum.diversity_score ?? '?'} · ${sum.shots_flagged ?? 0}/${sum.shots_total ?? 0} shots flagged`);
          lines.push('');

          for (const [key, data] of Object.entries(fields)) {
            const dupes = data.duplicate_phrases || {};
            const opens = data.opening_repeats || {};
            const over = data.overused_tokens || {};
            const sims = data.similar_pairs || [];
            const parts = [];
            if (Object.keys(dupes).length) {
              parts.push(`  EXACT DUPES (${Object.keys(dupes).length}):`);
              for (const [phr, ids] of Object.entries(dupes)) {
                parts.push(`    "${phr.slice(0, 60)}" × ${ids.length}: ${ids.join(', ')}`);
              }
            }
            if (Object.keys(opens).length) {
              parts.push(`  OPENING REPEATS (${Object.keys(opens).length}):`);
              for (const [phr, ids] of Object.entries(opens)) {
                parts.push(`    "${phr}" × ${ids.length}: ${ids.slice(0, 6).join(', ')}${ids.length > 6 ? '…' : ''}`);
              }
            }
            if (Object.keys(over).length) {
              const topOver = Object.entries(over).sort((a, b) => b[1].length - a[1].length).slice(0, 5);
              parts.push(`  OVERUSED TOKENS (top 5 of ${Object.keys(over).length}):`);
              for (const [tok, ids] of topOver) {
                parts.push(`    "${tok}" × ${ids.length}: ${ids.slice(0, 6).join(', ')}${ids.length > 6 ? '…' : ''}`);
              }
            }
            if (sims.length) {
              parts.push(`  SIMILAR PAIRS (top 5 of ${sims.length}):`);
              for (const p of sims.slice(0, 5)) {
                parts.push(`    ${p.a} ↔ ${p.b}  jaccard=${p.jaccard}`);
              }
            }
            if (parts.length) {
              lines.push(`${data.label || key}  (diversity ${data.diversity_score}):`);
              lines.push(...parts);
              lines.push('');
            }
          }
          if (flagged.length) {
            lines.push(`TOP OFFENDERS (by flag count):`);
            for (const f of flagged.slice(0, 10)) {
              lines.push(`  ${f.shot_id}  score=${f.score}`);
            }
          } else {
            lines.push('No shots flagged — prompts are diverse.');
          }

          const pre = document.getElementById('v6-variety-output') || (function () {
            const p = el('pre', {
              id: 'v6-variety-output',
              style: 'background:var(--surface);border:1px solid var(--amber);border-radius:var(--radius);padding:10px;margin:10px 0;color:var(--text);font-size:10px;white-space:pre-wrap;max-height:400px;overflow:auto;',
            });
            host.appendChild(p);
            return p;
          })();
          pre.textContent = lines.join('\n');
          const lvl = sum.verdict === 'SHIP' ? 'ok' : sum.verdict === 'REVIEW' ? 'warn' : 'error';
          v6Toast(`Variety: ${sum.verdict} · ${sum.shots_flagged}/${sum.shots_total} flagged`, lvl);
        } catch (err) {
          v6Toast(`Variety check failed: ${err.message}`, 'error');
        } finally {
          varietyBtn.disabled = false;
          varietyBtn.textContent = orig;
        }
      },
    }, '🎯 Variety Check');
    wrap.appendChild(varietyBtn);

    const dragBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--amber);color:var(--amber);font-size:9px;',
      title: 'F8: scan rendered clips for phash-similar drag (≥0.92 between 3 frames)',
      onclick: async () => {
        dragBtn.disabled = true;
        const orig = dragBtn.textContent;
        dragBtn.textContent = 'Scanning…';
        try {
          const res = await api('POST', '/api/v6/clips/drag-scan', { signed_off_only: false });
          const records = res.records || [];
          const flagged = records.filter(r => r.is_drag);
          const lines = flagged.map(c =>
            `  ${c.shot_id}  sim=${(c.max_similarity ?? 0).toFixed(3)}`
          );
          const body = [
            `Drag Scan: ${flagged.length} flagged / ${res.total ?? records.length} clips  (${res.elapsed_s ?? '?'}s)`,
            '',
            ...(lines.length ? lines : ['  (no drags detected)']),
            '',
            flagged.length ? 'Cross-check each flagged shot against scenes.json cameraMovement before re-rendering.' : '',
          ].filter(Boolean).join('\n');
          v6Toast(`F8: ${flagged.length} drag(s) flagged`, flagged.length ? 'warn' : 'ok');
          const pre = document.getElementById('v6-drag-output') || (function () {
            const p = el('pre', {
              id: 'v6-drag-output',
              style: 'background:var(--surface);border:1px solid var(--amber);border-radius:var(--radius);padding:10px;margin:10px 0;color:var(--text);font-size:10px;white-space:pre-wrap;max-height:260px;overflow:auto;',
            });
            host.appendChild(p);
            return p;
          })();
          pre.textContent = body;
        } catch (err) {
          v6Toast(`Drag scan failed: ${err.message}`, 'error');
        } finally {
          dragBtn.disabled = false;
          dragBtn.textContent = orig;
        }
      },
    }, 'Drag Scan');
    wrap.appendChild(dragBtn);

    const cutDriftBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--cyan);color:var(--cyan);font-size:9px;',
      title: 'F9: measure how far stitched cuts land from the nearest downbeat',
      onclick: async () => {
        cutDriftBtn.disabled = true;
        const orig = cutDriftBtn.textContent;
        cutDriftBtn.textContent = 'Measuring…';
        try {
          const res = await api('POST', '/api/v6/clips/cut-drift', {});
          const off = (res.off_grid_only || []).slice(0, 20);
          const lines = off.map(r =>
            `  cut ${String(r.cut_idx).padStart(2)} @ ${r.cut_time_s.toFixed(2)}s  Δ${(r.delta_s >= 0 ? '+' : '') + r.delta_s.toFixed(3)}s  (${(r.scene_out || '?').slice(0, 18)} → ${(r.scene_in || '?').slice(0, 18)})`
          );
          const body = [
            `Cut Drift: ${res.off_grid_count}/${res.total_cuts} off-grid (${res.off_grid_pct}%)  max ${res.max_drift_s?.toFixed?.(2)}s  mean ${res.mean_drift_s?.toFixed?.(2)}s`,
            `Threshold: ${res.threshold_s}s   Tempo: ${res.tempo_bpm?.toFixed?.(2)} bpm`,
            '',
            ...(lines.length ? lines : ['  (no off-grid cuts)']),
            '',
            res.recommendation || '',
          ].filter(Boolean).join('\n');
          v6Toast(`F9: ${res.off_grid_pct}% off-grid`, res.off_grid_pct >= 30 ? 'warn' : 'ok');
          const pre = document.getElementById('v6-drift-output') || (function () {
            const p = el('pre', {
              id: 'v6-drift-output',
              style: 'background:var(--surface);border:1px solid var(--cyan);border-radius:var(--radius);padding:10px;margin:10px 0;color:var(--text);font-size:10px;white-space:pre-wrap;max-height:260px;overflow:auto;',
            });
            host.appendChild(p);
            return p;
          })();
          pre.textContent = body;
        } catch (err) {
          v6Toast(`Cut drift failed: ${err.message}`, 'error');
        } finally {
          cutDriftBtn.disabled = false;
          cutDriftBtn.textContent = orig;
        }
      },
    }, 'Cut Drift');
    wrap.appendChild(cutDriftBtn);

    const motionBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--red);color:var(--red);font-size:9px;',
      title: 'Motion Audit: sample 3 frames/clip + Opus vision, flag back-of-head emblem and missing eyes. Gates stitch.',
      onclick: async () => {
        motionBtn.disabled = true;
        const orig = motionBtn.textContent;
        motionBtn.textContent = 'Auditing…';
        try {
          const res = await api('POST', '/api/v6/clips/motion-audit', { sample_count: 3, persist: true });
          const fails = res.fail_ids || [];
          const warns = res.warn_ids || [];
          const records = res.records || [];
          const lines = records
            .filter(r => (r.severity === 'fail' || r.severity === 'warn'))
            .map(r => {
              const frames = (r.frames || []).map(f =>
                `      t=${(f.t ?? 0).toFixed(2)}s  eyes=${f.eyes_visible}  emblem=${f.emblem_location}`
              ).join('\n');
              return `  [${(r.severity || '?').toUpperCase()}] ${r.shot_id}  ${r.summary || ''}\n${frames}`;
            });
          const body = [
            `Motion Audit: ${res.pass ?? 0} pass / ${warns.length} warn / ${fails.length} fail  (${res.elapsed_s ?? '?'}s)`,
            fails.length ? `  FAIL: ${fails.join(', ')}` : '',
            warns.length ? `  WARN: ${warns.join(', ')}` : '',
            '',
            ...(lines.length ? lines : ['  (all clips clean)']),
            '',
            fails.length ? 'Stitch is BLOCKED for fail_ids. Re-render with anti-orbit Kling prompts, then re-audit.' : '',
          ].filter(Boolean).join('\n');
          v6Toast(`Motion: ${fails.length} fail / ${warns.length} warn`,
                  fails.length ? 'error' : (warns.length ? 'warn' : 'ok'));
          const pre = document.getElementById('v6-motion-output') || (function () {
            const p = el('pre', {
              id: 'v6-motion-output',
              style: 'background:var(--surface);border:1px solid var(--red);border-radius:var(--radius);padding:10px;margin:10px 0;color:var(--text);font-size:10px;white-space:pre-wrap;max-height:320px;overflow:auto;',
            });
            host.appendChild(p);
            return p;
          })();
          pre.textContent = body;
        } catch (err) {
          v6Toast(`Motion audit failed: ${err.message}`, 'error');
        } finally {
          motionBtn.disabled = false;
          motionBtn.textContent = orig;
        }
      },
    }, 'Motion Audit');
    wrap.appendChild(motionBtn);

    host.appendChild(wrap);

    // Lyric coverage row — one pill per lyric line, green if ≥1 shot carries
    // it, red if no shot is assigned. Gives a fast read on which lines still
    // need a pickup insert.
    const timing = window._v6LastTiming;
    const lines = (timing?.lyrics?.lines) || [];
    if (lines.length) {
      const coverCounts = new Array(lines.length).fill(0);
      Object.values(gates.shots || {}).forEach(sh => {
        const idx = sh.lyric_line_idx;
        if (idx != null && idx >= 0 && idx < coverCounts.length) coverCounts[idx] += 1;
      });
      const covRow = el('div', {
        style: 'background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;margin-bottom:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;',
      });
      covRow.appendChild(el('div', {
        style: 'font-size:10px;font-weight:700;color:var(--cyan);text-transform:uppercase;letter-spacing:1px;margin-right:6px;',
      }, 'Lyric Coverage'));
      let uncovered = 0;
      lines.forEach((ln, i) => {
        const n = coverCounts[i];
        const covered = n > 0;
        if (!covered) uncovered += 1;
        const col = covered ? 'var(--green)' : 'var(--red)';
        const text = (ln.text || '').slice(0, 32) + ((ln.text || '').length > 32 ? '…' : '');
        covRow.appendChild(el('span', {
          title: `L${i} (${(ln.start||0).toFixed(1)}–${(ln.end||0).toFixed(1)}s): "${ln.text}" — ${n} shot(s)`,
          style: `font-size:9px;color:${col};padding:2px 8px;border:1px solid ${col}40;border-radius:10px;background:${col}10;`,
        }, `L${i}·${n} ${text}`));
      });
      if (uncovered) {
        covRow.appendChild(el('span', {
          style: 'font-size:9px;color:var(--amber);margin-left:auto;',
        }, `⚠ ${uncovered} line(s) without a dedicated shot`));
      }
      host.appendChild(covRow);
    }
  }

  // ─── Shot card builder ───
  function buildShotCard(anchor) {
    const card = el('div', {
      class: 'v6-shot-card',
      'data-v6-shot-id': anchor.shot_id,
      style: 'background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:12px;margin-bottom:12px;'
    });

    // Header — show narrative label (1a Rooftop Violet Sky medium) when
    // the scene is known; fall back to UUID for orphan anchor dirs.
    const header = el('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;' });
    const titleText = anchor.scene_name
      ? anchor.scene_name
      : (anchor.opus_shot_id ? `${anchor.opus_shot_id} · (no scene name)` : anchor.shot_id);
    const titleEl = el('span', { style: 'font-size:12px;font-weight:700;color:var(--cyan);', title: anchor.shot_id }, titleText);
    header.appendChild(titleEl);
    const status = anchor.selected ? 'ready' : 'needs anchor';
    const statusColor = anchor.selected ? 'var(--green)' : 'var(--amber)';
    header.appendChild(el('span', { style: `font-size:9px;padding:2px 8px;border-radius:10px;background:${statusColor}20;color:${statusColor};` }, status));
    card.appendChild(header);

    // Gate strip (section badge + lyric + 5 gate dots)
    card.appendChild(buildGateStrip(anchor.shot_id));

    // Image + controls grid
    const grid = el('div', { style: 'display:grid;grid-template-columns:200px minmax(0, 1fr);gap:12px;' });

    // Left: anchor image
    const imgCol = el('div');
    if (anchor.selected) {
      const img = el('img', {
        src: anchor.selected,
        style: 'width:100%;border-radius:var(--radius);cursor:pointer;',
        title: 'Click to view full size'
      });
      imgCol.appendChild(img);
    } else {
      imgCol.appendChild(el('div', {
        style: 'width:100%;aspect-ratio:16/9;background:var(--surface2);border-radius:var(--radius);display:flex;align-items:center;justify-content:center;color:var(--text-dim);font-size:10px;'
      }, 'No anchor'));
    }

    // Candidates row
    if (anchor.candidates && anchor.candidates.length > 1) {
      const candRow = el('div', { style: 'display:flex;gap:4px;margin-top:4px;' });
      anchor.candidates.forEach((c, i) => {
        const thumb = el('img', {
          src: c,
          style: 'width:48px;height:27px;object-fit:cover;border-radius:3px;cursor:pointer;border:2px solid transparent;',
          title: `Candidate ${i}`,
          onclick: () => {
            // TODO: select this candidate via API
            img && (img.src = c);
          }
        });
        candRow.appendChild(thumb);
      });
      imgCol.appendChild(candRow);
    }

    // End image
    if (anchor.end_image) {
      imgCol.appendChild(el('div', { style: 'font-size:8px;color:var(--text-dim);margin-top:4px;' }, 'End frame:'));
      imgCol.appendChild(el('img', {
        src: anchor.end_image,
        style: 'width:60px;border-radius:3px;margin-top:2px;'
      }));
    }

    grid.appendChild(imgCol);

    // Right: structured input form
    const form = el('div', { style: 'font-size:10px;' });
    const shotId = anchor.shot_id;

    // Row helper
    function formRow(label, input) {
      const row = el('div', { style: 'display:flex;align-items:center;gap:6px;margin-bottom:4px;' });
      row.appendChild(el('label', { style: 'width:80px;color:var(--text-dim);font-size:9px;' }, label));
      if (typeof input === 'string') {
        row.appendChild(el('span', { style: 'color:var(--text);' }, input));
      } else {
        row.appendChild(input);
      }
      return row;
    }

    // Anchor prompt fields — pre-fill from scene cameraAngle so defaults
    // match narrative instead of always landing on SHOT_SIZES[0]=Extreme wide.
    const _scn = scenesByShotId[shotId] || {};
    const _def = defaultsFromCameraAngle(_scn.cameraAngle, _scn.cameraMovement);
    form.appendChild(el('div', { style: 'font-size:9px;font-weight:600;color:var(--amber);margin-bottom:4px;' }, 'ANCHOR PROMPT'));
    form.appendChild(formRow('Shot size', select(`${shotId}_shotSize`, SHOT_SIZES, _def.shotSize)));
    form.appendChild(formRow('Lens', select(`${shotId}_lens`, LENS_OPTIONS, _def.lens)));
    form.appendChild(formRow('Angle', select(`${shotId}_angle`, CAMERA_ANGLES)));
    form.appendChild(formRow('DOF', select(`${shotId}_dof`, DOF_OPTIONS, _def.dof)));

    // P2-14: Camera Card — T-stop, filter, shutter/fps, aspect, side (180° rule)
    const camCard = el('details', { style: 'margin:6px 0;background:rgba(0,229,255,0.03);border:1px solid rgba(0,229,255,0.15);border-radius:var(--radius);padding:4px 8px;' });
    const camSummary = el('summary', { style: 'font-size:9px;font-weight:600;color:var(--cyan);cursor:pointer;padding:3px 0;', 'aria-label': 'Toggle camera card' }, '📷 Camera Card');
    camCard.appendChild(camSummary);

    camCard.appendChild(formRow('T-stop', select(`${shotId}_tstop`, [
      'T1.4','T2.0','T2.8','T4.0','T5.6','T8.0','T11'
    ], 'T2.8')));
    camCard.appendChild(formRow('Filter', select(`${shotId}_filter`, [
      'none','diffusion 1/8','diffusion 1/4','black pro-mist 1/4','black pro-mist 1/2','glimmerglass 1','ND 0.6','polarizer'
    ], 'none')));
    camCard.appendChild(formRow('Shutter', select(`${shotId}_shutter`, [
      '1/24','1/48','1/50','1/60','1/96','1/125','1/250'
    ], '1/48')));
    camCard.appendChild(formRow('FPS', select(`${shotId}_fps`, [
      '23.976','24','25','29.97','30','48','60','120'
    ], '24')));
    camCard.appendChild(formRow('Aspect', select(`${shotId}_aspect`, [
      '2.39:1','2.35:1','1.85:1','16:9','4:3','1:1','9:16'
    ], '2.39:1')));
    // Eye-line side for 180° rule
    const sideSel = select(`${shotId}_side`, [
      { value: 'camera-right', label: 'Subject looks camera-right' },
      { value: 'camera-left',  label: 'Subject looks camera-left' },
      { value: 'to-camera',    label: 'Subject looks to camera' },
      { value: 'away',         label: 'Subject away from camera' },
    ]);
    camCard.appendChild(formRow('Eye-line', sideSel));

    const ruleWarning = el('div', {
      style: 'font-size:9px;color:var(--amber);margin-top:4px;display:none;',
      role: 'alert',
    });
    camCard.appendChild(ruleWarning);

    // 180° rule check: adjacent shots in the same scene that flip eye-line without a cut-in
    function check180Rule() {
      try {
        const allCards = document.querySelectorAll('[data-v6-shot-id]');
        const myIdx = Array.from(allCards).findIndex(c => c.getAttribute('data-v6-shot-id') === shotId);
        if (myIdx <= 0) { ruleWarning.style.display = 'none'; return; }
        const prev = allCards[myIdx - 1];
        const prevSide = prev.querySelector(`[name$="_side"]`)?.value;
        if (prevSide && sideSel.value && prevSide !== 'to-camera' && sideSel.value !== 'to-camera' &&
            prevSide !== 'away' && sideSel.value !== 'away' &&
            prevSide !== sideSel.value) {
          ruleWarning.style.display = 'block';
          ruleWarning.textContent = `⚠ 180° rule: previous shot has eye-line ${prevSide}, this one is ${sideSel.value}. Add a re-establishing wide or OTS between them.`;
        } else {
          ruleWarning.style.display = 'none';
        }
      } catch (_) {}
    }
    sideSel.addEventListener('change', check180Rule);
    setTimeout(check180Rule, 200);

    form.appendChild(camCard);

    const subjectInput = el('input', {
      type: 'text', placeholder: 'Dog standing still, head turned right...',
      style: 'flex:1;font-size:10px;padding:3px 6px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);'
    });
    form.appendChild(formRow('Subject', subjectInput));

    const lightInput = el('input', {
      type: 'text', placeholder: 'Warm dappled light through trees...',
      style: 'flex:1;font-size:10px;padding:3px 6px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);'
    });
    form.appendChild(formRow('Lighting', lightInput));

    // Video prompt fields
    form.appendChild(el('div', { style: 'font-size:9px;font-weight:600;color:var(--green);margin-top:8px;margin-bottom:4px;' }, 'VIDEO PROMPT'));
    form.appendChild(formRow('Camera move', select(`${shotId}_camMove`, CAMERA_MOVEMENTS)));

    const motionInput = el('input', {
      type: 'text', placeholder: 'Dog walks forward, nose low...',
      style: 'flex:1;font-size:10px;padding:3px 6px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);'
    });
    form.appendChild(formRow('Motion', motionInput));

    const envMotionInput = el('input', {
      type: 'text', placeholder: 'Leaves drift, wind moves grass...',
      style: 'flex:1;font-size:10px;padding:3px 6px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);'
    });
    form.appendChild(formRow('Env motion', envMotionInput));

    // Kling config
    form.appendChild(el('div', { style: 'font-size:9px;font-weight:600;color:var(--magenta);margin-top:8px;margin-bottom:4px;' }, 'KLING CONFIG'));
    const tierRow = el('div', { style: 'display:flex;gap:6px;align-items:center;' });
    tierRow.appendChild(formRow('Tier', select(`${shotId}_tier`, KLING_TIERS)));
    form.appendChild(tierRow);

    const plannerDur = Number.isFinite(+anchor?.duration_s) ? +anchor.duration_s : 5;
    const durOpts = [];
    for (let d = 3; d <= 15; d++) {
      const tag = (d === plannerDur) ? ` · ${anchor?.duration_source || 'planner'}` : '';
      durOpts.push({ value: String(d), label: `${d}s${tag}` });
    }
    const durSelect = select(`${shotId}_dur`, durOpts);
    durSelect.value = String(plannerDur);
    const durRationale = anchor?.duration_rationale || '';
    const durRow = formRow('Duration', durSelect);
    if (durRationale) durRow.title = durRationale;
    form.appendChild(durRow);

    // P2-12: Number of parallel video candidates
    const candSelect = select(`${shotId}_numCand`, [
      { value: '1', label: '1 (single)' },
      { value: '2', label: '2 (A/B)' },
      { value: '3', label: '3 (A/B/C)' },
    ]);
    form.appendChild(formRow('Candidates', candSelect));

    // P2-13: Advanced per-shot block (seed / negative / end-frame / ref weight)
    // Collapsed by default. Lives inside the shot form.
    const advWrap = el('details', { style: 'margin-top:6px;border-top:1px dashed var(--border);padding-top:6px;' });
    const advSummary = el('summary', { style: 'font-size:9px;color:var(--text-dim);cursor:pointer;padding:4px 0;', 'aria-label': 'Toggle advanced shot controls' }, '⚙ Advanced (seed, negative, end-frame, ref weight)');
    advWrap.appendChild(advSummary);

    const seedInput = el('input', { type: 'number', name: `${shotId}_seed`, placeholder: '0 = random', style: 'flex:1;font-size:10px;padding:3px 6px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);', 'aria-label': 'Seed' });
    advWrap.appendChild(formRow('Seed', seedInput));

    const negInput = el('textarea', { name: `${shotId}_neg`, rows: '2', placeholder: 'blur, distortion, extra limbs…', style: 'flex:1;font-size:10px;padding:4px 6px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);resize:vertical;', 'aria-label': 'Negative prompt' });
    advWrap.appendChild(formRow('Negative', negInput));

    const endImgInput = el('input', { type: 'text', name: `${shotId}_endImg`, placeholder: 'output/.../end.png', style: 'flex:1;font-size:10px;padding:3px 6px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);', 'aria-label': 'End frame image path' });
    advWrap.appendChild(formRow('End frame', endImgInput));

    const refWeightInput = el('input', { type: 'range', name: `${shotId}_refWeight`, min: '0', max: '1', step: '0.05', value: '0.75', style: 'flex:1;', 'aria-label': 'Reference strength' });
    const refWeightLabel = el('span', { style: 'font-size:9px;color:var(--text-dim);width:28px;' }, '0.75');
    refWeightInput.addEventListener('input', () => { refWeightLabel.textContent = refWeightInput.value; });
    const refRow = el('div', { style: 'display:flex;align-items:center;gap:6px;margin-bottom:4px;' });
    refRow.appendChild(el('label', { style: 'width:80px;color:var(--text-dim);font-size:9px;' }, 'Ref weight'));
    refRow.appendChild(refWeightInput);
    refRow.appendChild(refWeightLabel);
    advWrap.appendChild(refRow);

    form.appendChild(advWrap);

    const cfgInput = el('input', {
      type: 'range', min: '0', max: '1', step: '0.1', value: '0.6',
      style: 'flex:1;',
      title: 'CFG Scale — higher = stricter prompt adherence'
    });
    const cfgLabel = el('span', { style: 'font-size:9px;color:var(--text-dim);width:24px;' }, '0.6');
    cfgInput.addEventListener('input', () => { cfgLabel.textContent = cfgInput.value; });
    const cfgRow = el('div', { style: 'display:flex;align-items:center;gap:6px;margin-bottom:4px;' });
    cfgRow.appendChild(el('label', { style: 'width:80px;color:var(--text-dim);font-size:9px;' }, 'CFG Scale'));
    cfgRow.appendChild(cfgInput);
    cfgRow.appendChild(cfgLabel);
    form.appendChild(cfgRow);

    // Action buttons
    const actions = el('div', { style: 'display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;' });

    const directBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--magenta);color:var(--magenta);font-size:9px;',
      title: 'Opus director rewrite: cinematic beat acting, motivated camera, shot-specific lighting. Overwrites Subject/Lighting/Motion/Env fields.',
      onclick: async () => {
        directBtn.textContent = 'Directing...';
        directBtn.disabled = true;
        try {
          const res = await api('POST', '/api/v6/director/direct-shot', {
            shot_id: shotId,
            apply: true,
          });
          const r = res.result || {};
          if (r.subjectAction) subjectInput.value = r.subjectAction;
          if (r.lighting) lightInput.value = r.lighting;
          if (r.cameraMovement) motionInput.value = r.cameraMovement;
          if (r.envMotion) envMotionInput.value = r.envMotion;
          updatePreview();
          const rat = (r.rationale || '').slice(0, 120);
          v6Toast(`[${shotId}] Directed${rat ? ' — ' + rat : ''}`, 'ok');
        } catch (err) {
          const d = err.details || {};
          if (err.status === 402) v6Toast(`Budget blocked: ${d.reason || err.message}`, 'error');
          else v6Toast(`Direct failed: ${err.message}`, 'error');
          console.error('[V6] Direct error:', err);
        } finally {
          directBtn.textContent = '🎬 Direct';
          directBtn.disabled = false;
        }
      },
    }, '🎬 Direct');

    const genAnchorBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--cyan);color:var(--cyan);font-size:9px;',
      onclick: async () => {
        genAnchorBtn.textContent = 'Generating...';
        genAnchorBtn.disabled = true;
        try {
          const _sceneCtx0 = scenesByShotId[shotId] || {};
          const prompt = buildAnchorPrompt({
            shotDescription: _sceneCtx0.shotDescription,
            emotion: _sceneCtx0.emotion,
            narrativeIntent: _sceneCtx0.narrativeIntent,
            shotSize: document.querySelector(`[name="${shotId}_shotSize"]`)?.value,
            lens: document.querySelector(`[name="${shotId}_lens"]`)?.value,
            cameraAngle: document.querySelector(`[name="${shotId}_angle"]`)?.value,
            dof: document.querySelector(`[name="${shotId}_dof"]`)?.value,
            subjectAction: subjectInput.value,
            lighting: lightInput.value,
          });
          // Project-scoped ref selection. Server resolves character/env/costume
          // from POS via shot_context; we only forward shot-specific env match
          // and reject preproduction/other-project refs. Fixes 2026-04-20
          // Buddy/Owen/Maya cross-project leak — UI can no longer flood the
          // anchor POST with the global /api/v6/references pool.
          // Per-shot ref chips: user can click a chip to exclude that entity.
          const _disabled = shotRefDisabled[shotId] || new Set();
          const refPaths = [];
          const envId = _sceneCtx0.environmentId || '';
          const envDisabled = envId && _disabled.has(`env:${envId}`);
          (references.environment || []).forEach(r => {
            if (!r || !r.path) return;
            if (r.source === 'preproduction') return;
            if (envDisabled) return;
            if (envId && r.name && !r.name.toLowerCase().includes(envId.toLowerCase().slice(0, 8))) {
              // Env cache filename embeds UUID prefix (env_<8char>_<slug>.png);
              // require match to this shot's environmentId.
              return;
            }
            refPaths.push(r.path);
          });
          const charId = _sceneCtx0.characterId || '';
          const costumeId = _sceneCtx0.costumeId || '';
          // excluded_ids: unified list of entity/motif UUIDs the user toggled off.
          // Server-side POS resolver skips these on every lookup (chars, envs,
          // costumes, motifs all share one namespace server-side).
          const excludedIds = Array.from(_disabled).map(k => k.split(':').pop()).filter(Boolean);
          const shotContext = {
            character_ids: (charId && !_disabled.has(`char:${charId}`)) ? [charId] : [],
            env_ids: (envId && !envDisabled) ? [envId] : [],
            costume_ids: (costumeId && !_disabled.has(`costume:${costumeId}`)) ? [costumeId] : [],
            prop_ids: Array.isArray(_sceneCtx0.propIds) ? _sceneCtx0.propIds : [],
            excluded_ids: excludedIds,
          };
          const res = await api('POST', '/api/v6/anchor/generate', {
            shot_id: shotId,
            prompt: prompt,
            reference_image_paths: refPaths.slice(0, 3),
            shot_context: shotContext,
            num_images: 3,
          });
          // Surface assembler + QA + identity gate info from response (#54)
          if (res.injection && res.injection.report) {
            v6Toast(`[${shotId}] ${res.injection.report}`, 'info');
          }
          if (res.qa && !res.qa.error) {
            const pick = res.qa.pick || '?';
            const conf = res.qa.confidence != null ? ` (conf ${res.qa.confidence})` : '';
            const cand = (res.qa.candidates || {})[pick] || {};
            const over = cand.overall != null ? ` overall=${cand.overall}` : '';
            const locked = res.qa.identity_gate_locked;
            let lvl = 'ok';
            if (cand.overall != null && cand.overall < 0.75) lvl = 'warn';
            let msg = `QA pick=${pick}${over}${conf}`;
            if (locked && locked.length) msg += ` — LOCKED: ${locked.join(', ')}`;
            v6Toast(msg, lvl);
          }
          refreshAnchors();
          refreshIdentityGate();
        } catch (err) {
          const d = err.details || {};
          if (err.status === 402) v6Toast(`Budget blocked: ${d.reason || err.message}`, 'error');
          else if (err.status === 428) v6Toast(`Identity gate: ${d.message || err.message}`, 'error');
          else v6Toast(`Anchor failed: ${err.message}`, 'error');
          console.error('[V6] Anchor gen error:', err);
        } finally {
          genAnchorBtn.textContent = 'Generate Anchor';
          genAnchorBtn.disabled = false;
        }
      }
    }, 'Generate Anchor');

    const auditAnchorBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--amber);color:var(--amber);font-size:9px;',
      title: 'Vision-audit this anchor against emblem/duplicate rules',
      onclick: async () => {
        if (!anchor.selected) { alert('No anchor to audit — generate one first'); return; }
        auditAnchorBtn.textContent = 'Auditing...';
        auditAnchorBtn.disabled = true;
        try {
          const res = await api('POST', '/api/v6/anchor/audit', { shot_id: shotId });
          const pass = !!res.pass;
          const vs = res.violations || [];
          const badge = pass ? 'PASS' : `FAIL (${vs.length})`;
          const lvl = pass ? 'ok' : 'error';
          v6Toast(`[${shotId}] Audit ${badge}: ${res.summary || ''}`, lvl);
          if (!pass && vs.length) {
            const lines = vs.map(v => `• [${v.severity}] ${v.code}: ${v.detail}`).join('\n');
            alert(`Anchor audit FAILED for ${shotId}\n\n${lines}\n\nFix: regenerate with stronger anchor_extra or anchor prompt.`);
          }
          await refreshGates();
        } catch (err) {
          v6Toast(`Audit failed: ${err.message}`, 'error');
          console.error('[V6] Audit error:', err);
        } finally {
          auditAnchorBtn.textContent = 'Audit Anchor';
          auditAnchorBtn.disabled = false;
        }
      }
    }, 'Audit Anchor');

    // Gate clip generation on audit_passed — never spend Kling $ on an anchor
    // that hasn't cleared the vision audit. Style conveys the gate: dim + lock
    // icon + tooltip explaining what the user needs to do first.
    const clipGate = (gates.shots || {})[shotId]?.gates || {};
    const auditPassed = clipGate.audit_passed === true;
    const genClipBtn = el('button', {
      class: 'btn btn-small',
      style: `border-color:var(--green);color:var(--green);font-size:9px;${auditPassed ? '' : 'opacity:0.35;cursor:not-allowed;'}`,
      title: auditPassed
        ? 'Render Kling clip from this anchor'
        : (clipGate.anchor_generated ? 'Audit must pass first — click Audit Anchor' : 'Generate and audit anchor first'),
      disabled: auditPassed ? undefined : 'true',
      onclick: async () => {
        if (!anchor.selected) { alert('Generate anchor first'); return; }
        if (!auditPassed) { v6Toast('Audit must pass before rendering clip', 'error'); return; }
        const duration = parseInt(document.querySelector(`[name="${shotId}_dur"]`)?.value || '5');
        const tier = document.querySelector(`[name="${shotId}_tier"]`)?.value || 'v3_standard';
        const numCand = parseInt(document.querySelector(`[name="${shotId}_numCand"]`)?.value || '1');
        const per = estimateKlingCost(tier, duration);
        const items = [];
        for (let ci = 0; ci < numCand; ci++) {
          items.push({ label: `Kling ${tier} · ${duration}s · candidate ${ci + 1}`, cost: per });
        }
        const ok = await confirmCost({
          title: 'Generate Clip — Cost Preflight',
          items,
          shotId,
        });
        if (!ok) return;
        genClipBtn.textContent = 'Generating...';
        genClipBtn.disabled = true;
        try {
          const videoPrompt = buildVideoPrompt({
            cameraMovement: document.querySelector(`[name="${shotId}_camMove"]`)?.value,
            subjectMotion: motionInput.value,
            envMotion: envMotionInput.value,
          });
          const clipBody = {
            shot_id: shotId,
            anchor_path: anchor.selected.replace('/api/v6/anchor-image/', 'output/pipeline/anchors_v6/'),
            prompt: videoPrompt,
            duration: parseInt(document.querySelector(`[name="${shotId}_dur"]`)?.value || '5'),
            tier: document.querySelector(`[name="${shotId}_tier"]`)?.value || 'v3_standard',
            cfg_scale: parseFloat(cfgInput.value),
            num_candidates: numCand,
            seed: parseInt(document.querySelector(`[name="${shotId}_seed"]`)?.value || '0') || undefined,
            negative_prompt: (document.querySelector(`[name="${shotId}_neg"]`)?.value ||
              'blur, distortion, extra limbs, extra legs, face warping, morphing, texture swimming, jitter, flicker, deformation, watermark, text, low quality'),
            end_image_path: document.querySelector(`[name="${shotId}_endImg"]`)?.value || undefined,
            generate_audio: true,
          };
          // Single-candidate clips go through the async worker + SSE progress
          // stream so the user sees staged progress; multi-candidate keeps the
          // sync endpoint since the async runner only handles one clip.
          const clipRes = (numCand === 1)
            ? await runJob('/api/v6/clip/generate_async', clipBody, (j) => {
                const pct = j.progress != null ? ` ${j.progress}%` : '';
                genClipBtn.textContent = `${j.stage || 'working'}${pct}`;
              })
            : await api('POST', '/api/v6/clip/generate', clipBody);
          if (clipRes.injection && clipRes.injection.report) {
            v6Toast(`[${shotId}] ${clipRes.injection.report}`, 'info');
          }
          genClipBtn.textContent = numCand > 1 ? `Queued ${numCand} candidates…` : 'Queued…';
          await refreshGates();
        } catch (err) {
          const d = err.details || {};
          if (err.status === 402) {
            v6Toast(`Budget blocked: ${d.reason || err.message}`, 'error');
          } else if (err.status === 428) {
            v6Toast(`Identity gate blocked: ${d.message || err.message}`, 'error');
          } else if (err.status === 422 && d.lint) {
            const issues = (d.lint.issues || []).map(i => `${i.severity}: ${i.rule}`).join(' · ');
            v6Toast(`Prompt rejected: ${issues}`, 'error');
          } else {
            v6Toast(`Clip failed: ${err.message}`, 'error');
          }
          console.error('[V6] Clip gen error:', err);
          genClipBtn.textContent = 'Generate Clip';
        } finally {
          genClipBtn.disabled = false;
        }
      }
    }, 'Generate Clip');

    const sonnetBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--violet);color:var(--violet);font-size:9px;',
      onclick: async () => {
        sonnetBtn.textContent = 'Reviewing...';
        sonnetBtn.disabled = true;
        try {
          const res = await api('POST', '/api/v6/sonnet/select', {
            shot_id: shotId,
            candidate_paths: (anchor.candidates || []).map(c =>
              c.replace('/api/v6/anchor-image/', 'output/pipeline/anchors_v6/')
            ),
            ref_sheet: 'output/preproduction/pkg_char_c852b9c5/sheet.png',
            shot_info: { title: shotId },
          });
          if (res.pick) {
            await showSonnetPickModal({
              shotId,
              pick: res.pick,
              reason: res.pick_reason || '',
              confidence: res.confidence,
              candidates: anchor.candidates || [],
              scores: res.scores || {},
            });
            refreshAnchors();
          }
        } catch (err) {
          alert('Sonnet select failed: ' + err.message);
          console.error('[V6] Sonnet select error:', err);
        } finally {
          sonnetBtn.textContent = 'Sonnet Select';
          sonnetBtn.disabled = false;
        }
      }
    }, 'Sonnet Select');

    const auditBtn = buildAuditButton(shotId, () => ({
      anchorPrompt: buildAnchorPrompt({
        shotSize: document.querySelector(`[name="${shotId}_shotSize"]`)?.value,
        lens: document.querySelector(`[name="${shotId}_lens"]`)?.value,
        cameraAngle: document.querySelector(`[name="${shotId}_angle"]`)?.value,
        dof: document.querySelector(`[name="${shotId}_dof"]`)?.value,
        subjectAction: subjectInput.value,
        lighting: lightInput.value,
      }),
      videoPrompt: buildVideoPrompt({
        cameraMovement: document.querySelector(`[name="${shotId}_camMove"]`)?.value,
        subjectMotion: motionInput.value,
        envMotion: envMotionInput.value,
      }),
    }));

    const historyBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--text-dim);color:var(--text-dim);font-size:9px;',
      onclick: () => showVersionHistory(shotId),
    }, 'History');

    // Thumb rate buttons — feed TI learning loop with per-shot judgments.
    // rating: 1 = good, -1 = bad, 0 = clear. Asset is whatever's currently
    // selected (anchor if no clip yet). Prompt is the final assembled pair.
    function sendRating(rating) {
      const prompt = buildAnchorPrompt({
        shotSize: document.querySelector(`[name="${shotId}_shotSize"]`)?.value,
        lens: document.querySelector(`[name="${shotId}_lens"]`)?.value,
        cameraAngle: document.querySelector(`[name="${shotId}_angle"]`)?.value,
        dof: document.querySelector(`[name="${shotId}_dof"]`)?.value,
        subjectAction: subjectInput.value,
        lighting: lightInput.value,
      });
      return api('POST', '/api/shot/rate', {
        shot_id: shotId,
        rating,
        asset_path: anchor.selected || '',
        prompt,
        meta: {
          tier: document.querySelector(`[name="${shotId}_tier"]`)?.value,
          duration: parseInt(document.querySelector(`[name="${shotId}_dur"]`)?.value || '5'),
          lens: document.querySelector(`[name="${shotId}_lens"]`)?.value,
        },
      }).then(() => v6Toast(`rated ${rating > 0 ? '👍' : rating < 0 ? '👎' : 'cleared'}`, 'ok'))
        .catch(e => v6Toast(`rate failed: ${e.message}`, 'error'));
    }
    const thumbUpBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--green);color:var(--green);font-size:11px;padding:2px 8px;',
      title: 'Mark this shot as good (feeds TI learning)',
      onclick: () => sendRating(1),
    }, '👍');
    const thumbDownBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--red);color:var(--red);font-size:11px;padding:2px 8px;',
      title: 'Mark this shot as bad (feeds TI learning)',
      onclick: () => sendRating(-1),
    }, '👎');

    // Per-card sign-off: only enabled when audit passed AND clip exists.
    // Final gate before a shot can land in the stitch.
    const so = (gates.shots || {})[shotId]?.gates || {};
    const canSignOff = so.audit_passed === true && so.clip_rendered === true;
    const alreadySigned = so.signed_off === true;
    const signOffBtn = el('button', {
      class: 'btn btn-small',
      style: `border-color:var(--magenta);color:var(--magenta);font-size:9px;${canSignOff || alreadySigned ? '' : 'opacity:0.35;cursor:not-allowed;'}`,
      title: alreadySigned
        ? `Signed off by ${so.signed_off_by || 'human'} — click to unlock`
        : (canSignOff ? 'Mark shot ready for final stitch' : 'Audit + clip must both pass first'),
      disabled: (canSignOff || alreadySigned) ? undefined : 'true',
      onclick: async () => {
        const next = !alreadySigned;
        await setShotGate(shotId, 'signed_off', next);
        v6Toast(`[${shotId}] ${next ? 'signed off' : 'unlocked'}`, next ? 'ok' : 'info');
      },
    }, alreadySigned ? '✓ Signed Off' : 'Sign Off');

    actions.appendChild(directBtn);
    actions.appendChild(genAnchorBtn);
    actions.appendChild(auditBtn);
    actions.appendChild(auditAnchorBtn);
    actions.appendChild(sonnetBtn);
    actions.appendChild(genClipBtn);
    actions.appendChild(signOffBtn);
    actions.appendChild(thumbUpBtn);
    actions.appendChild(thumbDownBtn);
    actions.appendChild(historyBtn);
    form.appendChild(actions);

    // Listen for audit apply events
    window.addEventListener('v6-audit-apply', (e) => {
      if (e.detail.shotId !== shotId) return;
      // Parse revised anchor prompt back into fields is hard — just show in preview
      if (e.detail.video) motionInput.value = e.detail.video;
      updatePreview();
    });

    // Ref chips — what gets attached to this shot (character, env, costume, motifs).
    // Click a chip to exclude. Honored by Generate Anchor + Generate Clip.
    const refChipsWrap = el('div', {
      style: 'margin-top:10px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;',
    });
    refChipsWrap.appendChild(renderRefChips(shotId, scenesByShotId[shotId] || {}, updatePreview));
    form.appendChild(refChipsWrap);

    // P2-11: Prompt preview (glass box) with copy buttons
    const previewWrap = el('details', {
      style: 'margin-top:10px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;',
      open: 'true',
    });
    const previewSummary = el('summary', {
      style: 'padding:6px 10px;cursor:pointer;font-size:10px;font-weight:600;color:var(--cyan);display:flex;justify-content:space-between;align-items:center;',
      'aria-label': 'Final prompts sent to Gemini and Kling',
    });
    previewSummary.appendChild(el('span', {}, 'Final prompts (what gets sent)'));
    const tokenCountSpan = el('span', { style: 'font-size:9px;color:var(--text-dim);font-weight:400;' }, '');
    previewSummary.appendChild(tokenCountSpan);
    previewWrap.appendChild(previewSummary);

    const previewBody = el('div', { style: 'padding:8px 10px 10px 10px;font-family:monospace;font-size:10px;color:var(--text);' });
    const anchorPromptDiv = el('div', { style: 'margin-bottom:6px;' });
    const videoPromptDiv  = el('div');
    previewBody.appendChild(anchorPromptDiv);
    previewBody.appendChild(videoPromptDiv);
    previewWrap.appendChild(previewBody);

    function makePromptRow(label, labelColor, text, onCopy) {
      const row = el('div', { style: 'display:flex;align-items:flex-start;gap:6px;' });
      const lbl = el('div', { style: `min-width:54px;color:${labelColor};font-weight:700;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;padding-top:2px;` }, label);
      const body = el('div', { style: 'flex:1;line-height:1.5;word-break:break-word;', tabindex: '0' }, text || '— (fill fields above) —');
      const copyBtn = el('button', {
        class: 'btn btn-small',
        style: 'font-size:8px;padding:2px 6px;flex-shrink:0;',
        'aria-label': `Copy ${label} prompt`,
        onclick: (e) => {
          e.stopPropagation();
          navigator.clipboard && navigator.clipboard.writeText(text || '').then(() => {
            copyBtn.textContent = '✓';
            setTimeout(() => { copyBtn.textContent = 'Copy'; }, 900);
          });
        },
      }, 'Copy');
      row.appendChild(lbl);
      row.appendChild(body);
      row.appendChild(copyBtn);
      return row;
    }

    function updatePreview() {
      const anchor_p = buildAnchorPrompt({
        shotSize: document.querySelector(`[name="${shotId}_shotSize"]`)?.value,
        lens: document.querySelector(`[name="${shotId}_lens"]`)?.value,
        cameraAngle: document.querySelector(`[name="${shotId}_angle"]`)?.value,
        dof: document.querySelector(`[name="${shotId}_dof"]`)?.value,
        subjectAction: subjectInput.value,
        lighting: lightInput.value,
      });
      const video_p = buildVideoPrompt({
        cameraMovement: document.querySelector(`[name="${shotId}_camMove"]`)?.value,
        subjectMotion: motionInput.value,
        envMotion: envMotionInput.value,
      });
      anchorPromptDiv.innerHTML = '';
      videoPromptDiv.innerHTML = '';
      anchorPromptDiv.appendChild(makePromptRow('Anchor', 'var(--amber)', anchor_p));
      videoPromptDiv.appendChild(makePromptRow('Video',  'var(--green)', video_p));
      // Rough word count (proxy for token budget)
      const words = (anchor_p + ' ' + video_p).split(/\s+/).filter(Boolean).length;
      tokenCountSpan.textContent = `~${words} words`;
    }
    // Update on any input change
    [subjectInput, lightInput, motionInput, envMotionInput].forEach(inp =>
      inp.addEventListener('input', updatePreview)
    );
    // @mention autocomplete — typing @ in any prompt input pops POS-backed
    // suggestions. Picking one inserts @<name> AND enables the matching chip
    // so the ref is guaranteed to reach Gemini.
    const _onAtAttach = () => {
      const newRow = renderRefChips(shotId, scenesByShotId[shotId] || {}, updatePreview);
      refChipsWrap.innerHTML = '';
      refChipsWrap.appendChild(newRow);
      updatePreview();
    };
    [subjectInput, lightInput, motionInput, envMotionInput].forEach(inp =>
      attachAtMention(inp, shotId, _onAtAttach)
    );
    // Also update on select changes
    form.addEventListener('change', updatePreview);
    updatePreview();
    form.appendChild(previewWrap);

    grid.appendChild(form);
    card.appendChild(grid);

    // Clip preview
    const clip = clips.find(c => c.shot_id === anchor.shot_id);
    if (clip) {
      const videoEl = el('video', {
        src: clip.url,
        controls: 'true',
        style: 'width:100%;max-height:200px;margin-top:8px;border-radius:var(--radius);'
      });
      card.appendChild(videoEl);
    }

    return card;
  }

  // ─── Refresh data ───
  async function refreshAnchors() {
    try {
      const data = await api('GET', '/api/v6/anchors');
      anchors = data.anchors || [];
      const clipData = await api('GET', '/api/v6/clips');
      clips = clipData.clips || [];
      try {
        const gateData = await api('GET', '/api/v6/shots/gates?project=default');
        gates = { shots: gateData.shots || {}, summary: gateData.summary || {} };
      } catch (_) { gates = { shots: {}, summary: {} }; }
      try {
        window._v6LastTiming = await api('GET', '/api/v6/song/timing?project=default');
      } catch (_) { /* leave prior cache */ }
      render();
      renderGateBatchBar();
    } catch (err) {
      console.error('[V6] Refresh error:', err);
    }
  }

  async function refreshGates() {
    try {
      const gateData = await api('GET', '/api/v6/shots/gates?project=default');
      gates = { shots: gateData.shots || {}, summary: gateData.summary || {} };
      renderGateBatchBar();
      // Re-render shot cards so per-card gate bars update without a full
      // anchor refetch — cheaper than hammering /api/v6/anchors.
      render();
    } catch (err) {
      console.error('[V6] Gates refresh error:', err);
    }
  }

  // ─── Render ───
  function sceneGroupKey(anchor) {
    const id = (anchor && anchor.opus_shot_id) || '';
    const m = id.match(/^(\d+)/);
    return m ? m[1] : '?';
  }

  function sceneGroupLabel(anchor) {
    const name = (anchor && anchor.scene_name) || '';
    if (!name) return '';
    const stripped = name.replace(/^\d+[a-z]\s+/i, '');
    const parts = stripped.split(/\s+/).filter(Boolean);
    if (parts.length > 1) {
      const last = parts[parts.length - 1].toLowerCase();
      const shotTypes = new Set(['medium', 'close', 'wide', 'ecu', 'cu', 'mcu', 'ws', 'mws', 'ots', 'pov', 'extreme', 'establishing', 'insert', 'cutaway', 'tracking', 'dolly', 'overhead', 'aerial', 'long']);
      if (shotTypes.has(last)) parts.pop();
    }
    return parts.join(' ');
  }

  function render() {
    const container = $('v6ShotGrid');
    if (!container) return;
    container.innerHTML = '';
    if (anchors.length === 0) {
      container.appendChild(el('div', {
        style: 'text-align:center;padding:40px;color:var(--text-dim);font-size:11px;'
      }, 'No V6 anchors yet. Generate anchors to get started.'));
      return;
    }
    let lastGroup = null;
    anchors.forEach(a => {
      const key = sceneGroupKey(a);
      if (key !== lastGroup) {
        const label = sceneGroupLabel(a);
        const header = el('div', {
          style: (lastGroup === null ? 'margin:0 0 10px 0;' : 'margin:20px 0 10px 0;border-top:1px solid var(--border);padding-top:10px;') +
                 'font-size:11px;font-weight:700;color:var(--cyan);letter-spacing:0.08em;text-transform:uppercase;'
        }, label ? `${key} · ${label}` : key);
        container.appendChild(header);
        lastGroup = key;
      }
      container.appendChild(buildShotCard(a));
    });
  }

  // ─── Negative prompt config ───
  function buildGlobalConfig() {
    const panel = el('div', { style: 'margin-bottom:12px;padding:10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);' });
    panel.appendChild(el('div', { style: 'font-size:10px;font-weight:600;color:var(--text);margin-bottom:6px;' }, 'Global Kling Settings'));

    const negInput = el('textarea', {
      id: 'v6GlobalNegPrompt',
      style: 'width:100%;height:40px;font-size:9px;padding:4px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);resize:vertical;',
      placeholder: 'blur, distortion, extra limbs...'
    }, 'blur, distortion, extra limbs, extra legs, face warping, morphing, texture swimming, jitter, flicker, deformation, watermark, text, low quality');

    panel.appendChild(el('label', { style: 'font-size:9px;color:var(--text-dim);' }, 'Negative prompt:'));
    panel.appendChild(negInput);

    const cfgRow = el('div', { style: 'display:flex;gap:12px;margin-top:6px;align-items:center;' });
    cfgRow.appendChild(el('label', { style: 'font-size:9px;color:var(--text-dim);' }, 'Default CFG:'));
    const cfgGlobal = el('input', {
      type: 'range', id: 'v6GlobalCfg', min: '0', max: '1', step: '0.1', value: '0.6',
      style: 'width:100px;'
    });
    const cfgVal = el('span', { style: 'font-size:9px;color:var(--text-dim);' }, '0.6');
    cfgGlobal.addEventListener('input', () => { cfgVal.textContent = cfgGlobal.value; });
    cfgRow.appendChild(cfgGlobal);
    cfgRow.appendChild(cfgVal);

    const audioCheck = el('input', { type: 'checkbox', id: 'v6GlobalAudio', style: 'width:12px;height:12px;' });
    cfgRow.appendChild(el('label', { style: 'font-size:9px;color:var(--text-dim);display:flex;align-items:center;gap:4px;margin-left:16px;' }, [audioCheck, document.createTextNode(' Generate audio')]));

    panel.appendChild(cfgRow);
    return panel;
  }

  // ─── Reference sheets panel ───
  let references = { character: [], environment: [], prop: [], costume: [] };

  // Per-scene context cache: scene.id → { characterId, environmentId, costumeId, propIds }
  // Used by Generate Anchor to force-inject scene-specific entities into the assembler.
  let scenesByShotId = {};

  // POS entity cache for ref-chip rendering (id → full record).
  const posCache = { characters: {}, environments: {}, costumes: {}, references: [] };

  // Per-shot ref disable state: shotId → Set<chipKey> (e.g. "char:<uuid>", "env:<uuid>").
  // Toggled by clicking a chip on the shot card; honored by Generate Anchor / Clip.
  const shotRefDisabled = {};

  // ─── @mention autocomplete ───
  // Typing @ inside a prompt input pops a list of POS entities (characters,
  // environments, costumes, references). Picking one inserts @<name> at the
  // caret AND ensures the matching ref chip is enabled for that shot — so a
  // user who wants "definitely include TB" can type @TB without digging into
  // the chip toggles. Works with the existing shot_context.excluded_ids flow
  // (removing the entity from shotRefDisabled) so POS resolvers fetch the ref.
  // Pull alias tokens off an entity — parenthesized "(TB)" inside the name,
  // shortName, aliases[]. Used so typing @TB matches "Trillion Bear (TB)".
  function _entityAliases(ent) {
    const out = [];
    const name = (ent && ent.name ? String(ent.name) : '').trim();
    if (name) {
      out.push(name);
      const m = name.match(/\(([^)]+)\)/g);
      if (m) for (const p of m) out.push(p.replace(/[()]/g, '').trim());
      const base = name.replace(/\(([^)]+)\)/g, '').trim();
      if (base && base !== name) out.push(base);
    }
    for (const f of ['shortName', 'short_name', 'alias', 'nickname']) {
      if (ent && typeof ent[f] === 'string' && ent[f].trim()) out.push(ent[f].trim());
    }
    const al = (ent && (ent.aliases || ent.altNames)) || [];
    if (Array.isArray(al)) for (const a of al) if (typeof a === 'string' && a.trim()) out.push(a.trim());
    const seen = new Set();
    return out.filter(t => { const k = t.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; });
  }

  function _atMentionCandidates() {
    const items = [];
    for (const c of Object.values(posCache.characters || {})) {
      if (!c || !c.name) continue;
      items.push({
        kind: 'char', id: c.id, name: c.name,
        aliases: _entityAliases(c),
        chipKey: `char:${c.id}`,
        hint: 'Character',
        thumb: _chipThumbForCharacter(c),
      });
    }
    for (const e of Object.values(posCache.environments || {})) {
      if (!e || !e.name) continue;
      items.push({
        kind: 'env', id: e.id, name: e.name,
        aliases: _entityAliases(e),
        chipKey: `env:${e.id}`,
        hint: 'Environment',
        thumb: _chipThumbForEnv(e),
      });
    }
    for (const cs of Object.values(posCache.costumes || {})) {
      if (!cs || !cs.name) continue;
      items.push({
        kind: 'costume', id: cs.id, name: cs.name,
        aliases: _entityAliases(cs),
        chipKey: `costume:${cs.id}`,
        hint: 'Costume',
        thumb: cs.previewImage || cs.collageImage || null,
      });
    }
    for (const r of (posCache.references || [])) {
      if (!r || !r.name) continue;
      items.push({
        kind: 'motif', id: r.id, name: r.name,
        aliases: _entityAliases(r),
        chipKey: `motif:${r.id}`,
        hint: 'Reference',
        thumb: r.previewImage || r.image || null,
      });
    }
    return items;
  }

  function attachAtMention(input, shotId, onAttach) {
    let menuEl = null;
    let activeIdx = 0;
    let currentMatches = [];
    let triggerStart = -1;

    function closeMenu() {
      if (menuEl && menuEl.parentNode) menuEl.parentNode.removeChild(menuEl);
      menuEl = null;
      triggerStart = -1;
      currentMatches = [];
      activeIdx = 0;
    }

    function renderMenu() {
      if (!menuEl) {
        menuEl = el('div', {
          class: 'v6-at-menu',
          style: 'position:absolute;z-index:9999;background:var(--surface);border:1px solid var(--magenta);border-radius:var(--radius);box-shadow:0 6px 18px rgba(0,0,0,0.5);font-size:10px;min-width:220px;max-width:320px;max-height:220px;overflow:auto;padding:4px;',
        });
        document.body.appendChild(menuEl);
      }
      menuEl.innerHTML = '';
      if (!currentMatches.length) {
        menuEl.appendChild(el('div', {
          style: 'padding:6px 8px;color:var(--text-dim);font-style:italic;',
        }, 'No matches'));
        return;
      }
      currentMatches.forEach((m, i) => {
        const row = el('div', {
          class: 'v6-at-item',
          style: `padding:4px 6px;cursor:pointer;border-radius:3px;display:flex;align-items:center;gap:8px;${i === activeIdx ? 'background:var(--magenta);color:#000;' : 'color:var(--text);'}`,
          onmousedown: (ev) => { ev.preventDefault(); select(i); },
          onmouseover: () => { activeIdx = i; renderMenu(); },
        });
        if (m.thumb) {
          row.appendChild(el('img', {
            src: m.thumb,
            style: 'width:28px;height:28px;object-fit:cover;border-radius:3px;border:1px solid rgba(255,255,255,0.15);flex-shrink:0;',
            onerror: function() { this.style.display = 'none'; },
          }));
        } else {
          row.appendChild(el('div', {
            style: `width:28px;height:28px;border-radius:3px;background:rgba(255,255,255,0.06);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:9px;opacity:0.6;${i === activeIdx ? 'color:#000;' : ''}`,
          }, m.kind[0].toUpperCase()));
        }
        const textCol = el('div', { style: 'display:flex;flex-direction:column;flex:1;min-width:0;overflow:hidden;' });
        textCol.appendChild(el('div', { style: 'font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;' }, `@${m.name}`));
        textCol.appendChild(el('div', {
          style: `font-size:9px;opacity:0.7;${i === activeIdx ? 'color:#000;' : ''}`,
        }, m.hint));
        row.appendChild(textCol);
        menuEl.appendChild(row);
      });
    }

    function positionMenu() {
      if (!menuEl) return;
      const rect = input.getBoundingClientRect();
      menuEl.style.left = (rect.left + window.scrollX) + 'px';
      menuEl.style.top  = (rect.bottom + window.scrollY + 2) + 'px';
      menuEl.style.minWidth = Math.max(220, rect.width) + 'px';
    }

    function select(i) {
      const m = currentMatches[i];
      if (!m) { closeMenu(); return; }
      const val = input.value;
      const before = val.slice(0, triggerStart);
      const afterCaret = val.slice(input.selectionEnd);
      const inserted = `@${m.name}`;
      input.value = before + inserted + ' ' + afterCaret;
      const caret = (before + inserted + ' ').length;
      try { input.setSelectionRange(caret, caret); } catch (_) {}
      // Ensure the chip is ENABLED: clear the disabled flag for this key.
      if (shotId && m.chipKey && shotRefDisabled[shotId]) {
        shotRefDisabled[shotId].delete(m.chipKey);
      }
      if (typeof onAttach === 'function') onAttach(m);
      closeMenu();
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    }

    function recomputeMatches() {
      const val = input.value;
      const caret = input.selectionEnd;
      // Walk backwards to find the @ that started this mention.
      let at = -1;
      for (let i = caret - 1; i >= 0 && i >= caret - 40; i--) {
        const ch = val[i];
        if (ch === '@') { at = i; break; }
        if (ch === ' ' || ch === '\n' || ch === '\t') break;
      }
      if (at < 0) { closeMenu(); return; }
      triggerStart = at;
      const query = val.slice(at + 1, caret).toLowerCase().trim();
      const all = _atMentionCandidates();
      // Score: best (lowest) across all alias tokens.
      //  0 = exact alias match
      //  1 = alias starts with query
      //  2 = alias contains query
      //  3 = no match (filtered out)
      const scored = all.map(c => {
        const aliases = (c.aliases && c.aliases.length ? c.aliases : [c.name]);
        let best = 3;
        let bestIdx = 999;
        for (const a of aliases) {
          const al = a.toLowerCase();
          if (!query) { best = Math.min(best, 2); bestIdx = 0; continue; }
          if (al === query) { best = 0; bestIdx = 0; break; }
          const idx = al.indexOf(query);
          if (idx === 0 && best > 1) { best = 1; bestIdx = 0; }
          else if (idx > 0 && best > 2) { best = 2; bestIdx = idx; }
        }
        return { c, score: best, idx: bestIdx };
      });
      const filtered = scored.filter(s => query ? s.score < 3 : true);
      filtered.sort((a, b) => {
        if (a.score !== b.score) return a.score - b.score;
        if (a.idx !== b.idx) return a.idx - b.idx;
        // Prefer characters, then envs, then costumes, then motifs.
        const kindOrder = { char: 0, env: 1, costume: 2, motif: 3 };
        const ka = kindOrder[a.c.kind] ?? 9;
        const kb = kindOrder[b.c.kind] ?? 9;
        if (ka !== kb) return ka - kb;
        return a.c.name.localeCompare(b.c.name);
      });
      currentMatches = filtered.slice(0, 8).map(s => s.c);
      if (activeIdx >= currentMatches.length) activeIdx = 0;
      renderMenu();
      positionMenu();
    }

    input.addEventListener('input', recomputeMatches);
    input.addEventListener('keydown', (ev) => {
      if (!menuEl) return;
      if (ev.key === 'ArrowDown') {
        activeIdx = Math.min(activeIdx + 1, Math.max(0, currentMatches.length - 1));
        renderMenu(); ev.preventDefault();
      } else if (ev.key === 'ArrowUp') {
        activeIdx = Math.max(activeIdx - 1, 0);
        renderMenu(); ev.preventDefault();
      } else if (ev.key === 'Enter' || ev.key === 'Tab') {
        if (currentMatches.length) { select(activeIdx); ev.preventDefault(); }
      } else if (ev.key === 'Escape') {
        closeMenu(); ev.preventDefault();
      }
    });
    input.addEventListener('blur', () => setTimeout(closeMenu, 120));
    window.addEventListener('scroll', positionMenu, true);
  }

  async function refreshPOSForChips() {
    try {
      const [charsRes, envsRes, costumesRes, refsRes] = await Promise.all([
        api('GET', '/api/pos/characters').catch(() => ({ characters: [] })),
        api('GET', '/api/pos/environments').catch(() => ({ environments: [] })),
        api('GET', '/api/pos/costumes').catch(() => ({ costumes: [] })),
        api('GET', '/api/pos/references').catch(() => ({ references: [] })),
      ]);
      const chars = (charsRes && (charsRes.characters || charsRes.items)) || [];
      const envs = (envsRes && (envsRes.environments || envsRes.items)) || [];
      const costumes = (costumesRes && (costumesRes.costumes || costumesRes.items)) || [];
      const refs = (refsRes && (refsRes.references || refsRes.items)) || [];
      posCache.characters = Object.fromEntries(chars.map(c => [c.id, c]));
      posCache.environments = Object.fromEntries(envs.map(e => [e.id, e]));
      posCache.costumes = Object.fromEntries(costumes.map(c => [c.id, c]));
      posCache.references = refs;
    } catch (e) {
      console.warn('[V6] POS chip cache refresh failed:', e && e.message);
    }
  }

  function _chipThumbForCharacter(c) {
    if (!c) return null;
    return c.approvedSheet || c.previewImage ||
           (Array.isArray(c.sheetImages) && c.sheetImages.length ? c.sheetImages[c.sheetImages.length - 1] : null);
  }
  function _chipThumbForEnv(e) {
    if (!e) return null;
    return e.collageImage || e.previewImage ||
           (Array.isArray(e.sheetImages) && e.sheetImages.length ? e.sheetImages[e.sheetImages.length - 1] : null);
  }

  function renderRefChips(shotId, sceneCtx, onChange) {
    const row = el('div', {
      style: 'display:flex;flex-wrap:wrap;gap:4px;align-items:center;padding:6px 10px;background:rgba(0,229,255,0.04);border-bottom:1px solid var(--border);',
      'data-ref-chips-row': shotId,
    });
    row.appendChild(el('span', {
      style: 'font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;margin-right:4px;font-weight:600;'
    }, 'Refs →'));

    if (!shotRefDisabled[shotId]) shotRefDisabled[shotId] = new Set();
    const disabled = shotRefDisabled[shotId];

    function makeChip(key, label, thumbUrl, color, tip) {
      const isDisabled = disabled.has(key);
      const chip = el('span', {
        style: `display:inline-flex;align-items:center;gap:4px;font-size:9px;padding:2px 7px;border-radius:10px;cursor:pointer;transition:opacity 0.15s;` +
               (isDisabled
                 ? 'background:var(--surface2);color:var(--text-dim);text-decoration:line-through;opacity:0.45;border:1px dashed var(--border);'
                 : `background:${color}20;color:${color};border:1px solid ${color}55;`),
        title: (tip || label) + (isDisabled ? ' — excluded (click to include)' : ' — attached (click to exclude)'),
      });
      if (thumbUrl) {
        chip.appendChild(el('img', {
          src: thumbUrl,
          style: 'width:14px;height:14px;border-radius:2px;object-fit:cover;flex-shrink:0;',
        }));
      }
      chip.appendChild(el('span', {}, label));
      chip.onclick = () => {
        if (disabled.has(key)) disabled.delete(key); else disabled.add(key);
        const newRow = renderRefChips(shotId, sceneCtx, onChange);
        row.replaceWith(newRow);
        if (typeof onChange === 'function') onChange();
      };
      return chip;
    }

    const ctx = sceneCtx || {};
    let chipCount = 0;

    if (ctx.characterId) {
      const c = posCache.characters[ctx.characterId];
      const name = c?.name || ctx.characterId.slice(0, 8);
      row.appendChild(makeChip(
        `char:${ctx.characterId}`,
        name,
        _chipThumbForCharacter(c),
        'var(--cyan)',
        'Character sheet — identity lock'
      ));
      chipCount++;
    }
    if (ctx.environmentId) {
      const e = posCache.environments[ctx.environmentId];
      const name = e?.name || ctx.environmentId.slice(0, 8);
      row.appendChild(makeChip(
        `env:${ctx.environmentId}`,
        name,
        _chipThumbForEnv(e),
        'var(--magenta)',
        'Environment collage'
      ));
      chipCount++;
    }
    if (ctx.costumeId) {
      const cs = posCache.costumes[ctx.costumeId];
      const name = cs?.name || 'costume';
      row.appendChild(makeChip(
        `costume:${ctx.costumeId}`,
        name,
        _chipThumbForEnv(cs),
        'var(--amber)',
        'Costume reference'
      ));
      chipCount++;
    }
    // Motif chips: any reference tagged for this scene (by scene id or scene name match)
    const motifs = (posCache.references || []).filter(r => {
      if (!r) return false;
      const tags = Array.isArray(r.tags) ? r.tags.map(t => String(t).toLowerCase()) : [];
      const scenes = Array.isArray(r.sceneIds) ? r.sceneIds : [];
      if (scenes.includes(shotId)) return true;
      // fallback: tag matches env name
      const eName = (posCache.environments[ctx.environmentId]?.name || '').toLowerCase();
      if (eName && tags.some(t => eName.includes(t) || t.includes(eName.split(' ')[0]))) return true;
      return false;
    });
    motifs.slice(0, 3).forEach(m => {
      row.appendChild(makeChip(
        `motif:${m.id}`,
        m.name || 'motif',
        m.previewImage || m.url,
        'var(--violet)',
        'Motif reference'
      ));
      chipCount++;
    });

    if (chipCount === 0) {
      row.appendChild(el('span', { style: 'font-size:9px;color:var(--text-dim);font-style:italic;' }, '(no POS refs for this shot)'));
    }
    return row;
  }

  async function refreshScenes() {
    try {
      const data = await api('GET', '/api/pos/scenes');
      const scenes = (data && (data.scenes || data.items)) || (Array.isArray(data) ? data : []);
      const map = {};
      scenes.forEach(s => {
        if (!s || !s.id) return;
        map[s.id] = {
          characterId: s.characterId || null,
          environmentId: s.environmentId || null,
          costumeId: s.costumeId || null,
          propIds: Array.isArray(s.propIds) ? s.propIds : [],
          shotDescription: s.shotDescription || '',
          emotion: s.emotion || '',
          narrativeIntent: s.narrativeIntent || '',
          cameraAngle: s.cameraAngle || '',
          cameraMovement: s.cameraMovement || '',
        };
      });
      scenesByShotId = map;
    } catch (err) {
      console.warn('[V6] Scenes cache refresh failed:', err.message);
    }
  }

  async function refreshReferences() {
    try {
      const data = await api('GET', '/api/v6/references');
      references = data.references || { character: [], environment: [], prop: [], costume: [] };
      renderRefs();
      // Refs changed → outputs may now be stale
      if (typeof refreshStaleCheck === 'function') refreshStaleCheck();
    } catch (err) {
      console.error('[V6] Ref refresh error:', err);
    }
  }

  function renderRefs() {
    const grid = $('v6RefGrid');
    if (!grid) return;
    grid.innerHTML = '';
    const types = ['character', 'environment', 'prop', 'costume'];
    types.forEach(type => {
      const refs = references[type] || [];
      const col = el('div', { style: 'min-width:120px;' });
      col.appendChild(el('div', { style: 'font-size:9px;font-weight:600;color:var(--cyan);margin-bottom:4px;text-transform:uppercase;' }, type));
      refs.forEach(ref => {
        const isPreprod = ref.source === 'preproduction';
        const thumb = el('div', { style: 'position:relative;margin-bottom:4px;' });
        thumb.appendChild(el('img', {
          src: ref.url,
          style: 'width:100%;border-radius:var(--radius);cursor:pointer;' + (isPreprod ? 'border:1px solid var(--cyan);' : ''),
          title: ref.name + (isPreprod ? ' (from preproduction)' : '')
        }));
        // Source badge
        const badge = el('div', {
          style: 'position:absolute;top:2px;right:2px;font-size:7px;padding:1px 4px;border-radius:2px;text-transform:uppercase;letter-spacing:0.5px;' +
            (isPreprod ? 'background:rgba(0,220,255,0.25);color:var(--cyan);' : 'background:rgba(255,255,255,0.1);color:var(--text-dim);')
        }, isPreprod ? 'PREPROD' : 'UPLOAD');
        thumb.appendChild(badge);
        thumb.appendChild(el('div', { style: 'font-size:8px;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, ref.name));
        if (isPreprod && ref.sheet_count) {
          thumb.appendChild(el('div', { style: 'font-size:7px;color:var(--cyan);opacity:0.7;' }, ref.sheet_count + ' sheet views'));
        }
        col.appendChild(thumb);
      });
      if (refs.length === 0) {
        col.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);padding:8px 0;' }, 'None uploaded'));
      }
      grid.appendChild(col);
    });
  }

  function buildRefUploadPanel() {
    const panel = el('div', { style: 'margin-bottom:12px;padding:10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);' });
    panel.appendChild(el('div', { style: 'font-size:10px;font-weight:600;color:var(--text);margin-bottom:6px;' }, 'Reference Sheets'));
    panel.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);margin-bottom:8px;' }, 'Upload photos for character/environment/prop sheets. These are used as @Tag references during anchor generation.'));

    // Upload row
    const uploadRow = el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;' });

    const typeSelect = select('v6RefType', [
      { value: 'character', label: 'Character' },
      { value: 'environment', label: 'Environment' },
      { value: 'prop', label: 'Prop' },
      { value: 'costume', label: 'Costume' }
    ]);
    uploadRow.appendChild(typeSelect);

    const nameInput = el('input', {
      type: 'text', placeholder: 'Name (e.g. "buddy_golden")',
      style: 'font-size:10px;padding:4px 6px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);width:150px;'
    });
    uploadRow.appendChild(nameInput);

    const fileInput = el('input', {
      type: 'file', accept: 'image/png,image/jpeg,image/webp',
      style: 'font-size:9px;color:var(--text-dim);width:180px;'
    });
    uploadRow.appendChild(fileInput);

    const uploadBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--cyan);color:var(--cyan);font-size:9px;',
      onclick: async () => {
        if (!fileInput.files || !fileInput.files[0]) { alert('Select a file first'); return; }
        uploadBtn.textContent = 'Uploading...';
        uploadBtn.disabled = true;
        try {
          const formData = new FormData();
          formData.append('file', fileInput.files[0]);
          formData.append('type', typeSelect.value);
          formData.append('name', nameInput.value || fileInput.files[0].name.split('.')[0]);
          const csrfMeta = document.querySelector('meta[name="csrf-token"]');
          const res = await fetch('/api/v6/reference/upload', {
            method: 'POST',
            headers: { 'X-CSRF-Token': csrfMeta ? csrfMeta.content : '' },
            body: formData,
          });
          const data = await res.json();
          if (data.ok) {
            fileInput.value = '';
            nameInput.value = '';
            refreshReferences();
          } else {
            alert(data.error || 'Upload failed');
          }
        } catch (err) {
          alert('Upload failed: ' + err.message);
          console.error('[V6] Upload error:', err);
        } finally {
          uploadBtn.textContent = 'Upload';
          uploadBtn.disabled = false;
        }
      }
    }, 'Upload');
    uploadRow.appendChild(uploadBtn);

    panel.appendChild(uploadRow);

    // Ref thumbnails grid
    panel.appendChild(el('div', { id: 'v6RefGrid', style: 'display:grid;grid-template-columns:repeat(4,1fr);gap:8px;' }));

    return panel;
  }

  // ─── Sonnet audit button for shot cards ───
  function buildAuditButton(shotId, getPrompts) {
    const btn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--violet);color:var(--violet);font-size:9px;',
      onclick: async () => {
        const { anchorPrompt, videoPrompt } = getPrompts();
        if (!anchorPrompt && !videoPrompt) { alert('Fill in at least one prompt field'); return; }
        btn.textContent = 'Auditing...';
        btn.disabled = true;
        try {
          const res = await api('POST', '/api/v6/sonnet/audit-prompt', {
            shot_id: shotId,
            anchor_prompt: anchorPrompt,
            video_prompt: videoPrompt,
            shot_context: {}
          });
          if (res.error) { alert('Audit error: ' + res.error); return; }
          showAuditResult(shotId, res);
        } catch (err) {
          alert('Audit failed: ' + err.message);
          console.error('[V6] Audit error:', err);
        } finally {
          btn.textContent = 'Sonnet Audit';
          btn.disabled = false;
        }
      }
    }, 'Sonnet Audit');
    return btn;
  }

  function showAuditResult(shotId, result) {
    // Remove existing
    const existing = document.querySelector('.v6-audit-overlay');
    if (existing) existing.remove();

    const overlay = el('div', {
      class: 'v6-audit-overlay',
      style: 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.85);z-index:9999;display:flex;align-items:center;justify-content:center;',
      onclick: (e) => { if (e.target === overlay) overlay.remove(); }
    });

    const card = el('div', { style: 'background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:20px;max-width:600px;width:90%;max-height:80vh;overflow-y:auto;' });

    card.appendChild(el('div', { style: 'font-size:14px;font-weight:700;color:var(--text);margin-bottom:12px;' }, `Sonnet Audit: ${shotId}`));

    if (result.anchor_prompt_revised) {
      card.appendChild(el('div', { style: 'font-size:9px;font-weight:600;color:var(--amber);margin-bottom:4px;' }, 'REVISED ANCHOR PROMPT'));
      card.appendChild(el('div', { style: 'font-size:10px;color:var(--text);padding:6px;background:var(--surface2);border-radius:var(--radius);margin-bottom:8px;font-family:monospace;' }, result.anchor_prompt_revised));
    }
    if (result.video_prompt_revised) {
      card.appendChild(el('div', { style: 'font-size:9px;font-weight:600;color:var(--green);margin-bottom:4px;' }, 'REVISED VIDEO PROMPT'));
      card.appendChild(el('div', { style: 'font-size:10px;color:var(--text);padding:6px;background:var(--surface2);border-radius:var(--radius);margin-bottom:8px;font-family:monospace;' }, result.video_prompt_revised));
    }
    if (result.changes_made && result.changes_made.length > 0) {
      card.appendChild(el('div', { style: 'font-size:9px;font-weight:600;color:var(--violet);margin-bottom:4px;' }, 'CHANGES MADE'));
      const list = el('ul', { style: 'font-size:9px;color:var(--text-dim);padding-left:16px;margin-bottom:8px;' });
      result.changes_made.forEach(c => list.appendChild(el('li', { style: 'margin-bottom:2px;' }, c)));
      card.appendChild(list);
    }
    if (result.confidence) {
      card.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);' }, `Confidence: ${result.confidence} | Anchor words: ${result.word_count_anchor || '?'} | Video words: ${result.word_count_video || '?'}`));
    }

    // Apply button
    const applyBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--green);color:var(--green);font-size:10px;margin-top:12px;',
      onclick: () => {
        // Dispatch event with revised prompts for the shot card to pick up
        window.dispatchEvent(new CustomEvent('v6-audit-apply', {
          detail: { shotId, anchor: result.anchor_prompt_revised, video: result.video_prompt_revised }
        }));
        overlay.remove();
      }
    }, 'Apply Revisions');
    card.appendChild(applyBtn);

    const closeBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--text-dim);color:var(--text-dim);font-size:10px;margin-top:12px;margin-left:8px;',
      onclick: () => overlay.remove()
    }, 'Dismiss');
    card.appendChild(closeBtn);

    overlay.appendChild(card);
    document.body.appendChild(overlay);
  }

  // ─── Toolbar: template picker, timeline preview, FCPXML export ───
  function buildToolbar() {
    const bar = el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);' });

    const briefBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--magenta);color:var(--magenta);font-size:9px;font-weight:700;',
      onclick: showBriefExpand,
      title: 'Type a one-line idea. Sonnet will expand it into characters, environments, and a shot list.',
    }, '💡 From Idea');

    const templateBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--violet);color:var(--violet);font-size:9px;',
      onclick: showTemplatePicker,
    }, '+ Template');

    const timelineBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--cyan);color:var(--cyan);font-size:9px;',
      onclick: refreshTimelinePreview,
    }, 'Preview Timeline');

    const exportBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--green);color:var(--green);font-size:9px;',
      onclick: () => {
        const csrf = document.querySelector('meta[name="csrf-token"]');
        // Open in new tab — the Content-Disposition header triggers download
        const url = '/api/v6/project/export/fcpxml?_csrf=' + encodeURIComponent(csrf ? csrf.content : '');
        // But CSRF is in header not query — use fetch + blob instead
        fetch('/api/v6/project/export/fcpxml', { headers: { 'X-CSRF-Token': csrf ? csrf.content : '' }})
          .then(r => r.blob())
          .then(blob => {
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'lumn_project.fcpxml';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
          })
          .catch(err => alert('Export failed: ' + err.message));
      },
    }, 'Export FCPXML');

    const staleBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--amber);color:var(--amber);font-size:9px;',
      onclick: refreshStaleCheck,
    }, 'Check Stale');

    bar.appendChild(briefBtn);
    bar.appendChild(templateBtn);
    bar.appendChild(timelineBtn);
    bar.appendChild(exportBtn);
    bar.appendChild(staleBtn);
    return bar;
  }

  // ─── Brief expand modal (#56) ───
  // User types one-line idea → Sonnet returns structured plan → we show the
  // plan for review and, on accept, create preproduction packages automatically.
  async function showBriefExpand() {
    const backdrop = el('div', { style: 'position:fixed;inset:0;background:rgba(0,0,0,0.8);backdrop-filter:blur(10px);z-index:9999;display:flex;align-items:center;justify-content:center;padding:40px;' });
    const modal = el('div', { style: 'background:var(--surface);border:1px solid var(--magenta);border-radius:8px;padding:24px;max-width:720px;width:100%;max-height:88vh;overflow-y:auto;' });

    modal.appendChild(el('div', { style: 'font-size:12px;font-weight:700;color:var(--magenta);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px;' }, 'Start From an Idea'));
    modal.appendChild(el('div', { style: 'font-size:10px;color:var(--text-dim);margin-bottom:16px;line-height:1.5;' },
      'Type a one-line concept. Sonnet will propose characters, environments, and a shot list so you can tweak instead of starting blank. ~$0.04 per expand.'));

    const input = el('textarea', {
      style: 'width:100%;min-height:72px;padding:10px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px;font-family:inherit;resize:vertical;',
      placeholder: 'e.g., A lost golden retriever finds his way home through an autumn park',
    });
    modal.appendChild(input);

    const shotsRow = el('div', { style: 'display:flex;align-items:center;gap:8px;margin-top:10px;' });
    shotsRow.appendChild(el('label', { style: 'font-size:10px;color:var(--text-dim);' }, 'Max shots:'));
    const shotsInput = el('input', { type: 'number', value: '6', min: '2', max: '12',
      style: 'width:60px;padding:4px 6px;background:var(--surface2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;' });
    shotsRow.appendChild(shotsInput);
    modal.appendChild(shotsRow);

    const status = el('div', { style: 'font-size:10px;color:var(--text-dim);margin-top:10px;min-height:14px;' });
    modal.appendChild(status);

    const preview = el('div', { style: 'margin-top:12px;' });
    modal.appendChild(preview);

    const btnRow = el('div', { style: 'display:flex;gap:8px;margin-top:14px;justify-content:flex-end;' });

    const expandBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--magenta);color:var(--magenta);font-size:10px;font-weight:700;',
    }, 'Expand with Sonnet');

    const createBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--green);color:var(--green);font-size:10px;font-weight:700;display:none;',
    }, 'Create Packages');

    const cancelBtn = el('button', { class: 'btn btn-small', style: 'font-size:10px;' }, 'Cancel');

    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(expandBtn);
    btnRow.appendChild(createBtn);
    modal.appendChild(btnRow);

    backdrop.appendChild(modal);
    const ctl = v6Modal({ backdrop, modal, labelText: 'Expand idea into plan' });
    cancelBtn.addEventListener('click', () => ctl.close());

    let currentPlan = null;

    expandBtn.addEventListener('click', async () => {
      const brief = input.value.trim();
      if (!brief) { status.textContent = 'Enter a brief first.'; status.style.color = 'var(--red)'; return; }
      expandBtn.disabled = true;
      expandBtn.textContent = 'Expanding…';
      status.style.color = 'var(--text-dim)';
      status.textContent = 'Calling Sonnet…';
      preview.innerHTML = '';
      try {
        const res = await api('POST', '/api/v6/brief/expand', {
          brief: brief,
          max_shots: parseInt(shotsInput.value, 10) || 6,
        });
        currentPlan = res.plan || {};
        renderPlanPreview(preview, currentPlan);
        createBtn.style.display = '';
        status.textContent = `Got plan: ${(currentPlan.characters || []).length} char, ${(currentPlan.environments || []).length} env, ${(currentPlan.shots || []).length} shots`;
        status.style.color = 'var(--green)';
      } catch (err) {
        const d = err.details || {};
        if (err.status === 402) status.textContent = `Budget blocked: ${d.reason || err.message}`;
        else status.textContent = `Failed: ${err.message}`;
        status.style.color = 'var(--red)';
      } finally {
        expandBtn.disabled = false;
        expandBtn.textContent = 'Expand with Sonnet';
      }
    });

    createBtn.addEventListener('click', async () => {
      if (!currentPlan) return;
      createBtn.disabled = true;
      createBtn.textContent = 'Creating…';
      let created = 0, failed = 0;
      const toCreate = [
        ...((currentPlan.characters || []).map(c => ({ ...c, package_type: 'character' }))),
        ...((currentPlan.environments || []).map(e => ({ ...e, package_type: 'environment' }))),
      ];
      for (const item of toCreate) {
        try {
          await api('POST', '/api/preproduction/package/create', {
            package_type: item.package_type,
            name: item.name,
            description: item.description || '',
            must_keep: item.must_keep || [],
            avoid: item.avoid || [],
            canonical_notes: item.role || '',
          });
          created += 1;
        } catch (err) {
          console.warn('[brief_expand] package create failed:', item.name, err.message);
          failed += 1;
        }
      }
      v6Toast(`Brief expand → ${created} packages created${failed ? `, ${failed} failed` : ''}`,
        failed ? 'warn' : 'ok');
      createBtn.disabled = false;
      createBtn.textContent = 'Create Packages';
      ctl.close();
      if (window._refreshPreproPackages) window._refreshPreproPackages();
    });
  }

  function renderPlanPreview(host, plan) {
    host.innerHTML = '';
    const box = el('div', { style: 'padding:12px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;' });
    if (plan.title) box.appendChild(el('div', { style: 'font-size:13px;font-weight:700;color:var(--text);margin-bottom:2px;' }, plan.title));
    if (plan.logline) box.appendChild(el('div', { style: 'font-size:10px;color:var(--text-dim);margin-bottom:10px;font-style:italic;' }, plan.logline));
    if (plan.tone) box.appendChild(el('div', { style: 'font-size:9px;color:var(--cyan);margin-bottom:10px;text-transform:uppercase;letter-spacing:0.5px;' }, 'Tone: ' + plan.tone));

    if ((plan.characters || []).length) {
      box.appendChild(el('div', { style: 'font-size:10px;font-weight:700;color:var(--text);margin-bottom:4px;margin-top:8px;' }, 'Characters'));
      plan.characters.forEach(c => {
        const row = el('div', { style: 'font-size:10px;color:var(--text);padding:4px 0;border-bottom:1px solid var(--border);' });
        row.appendChild(el('div', { style: 'font-weight:600;' }, c.name + (c.role ? ` · ${c.role}` : '')));
        if (c.description) row.appendChild(el('div', { style: 'color:var(--text-dim);font-size:9px;margin-top:2px;' }, c.description));
        if ((c.must_keep || []).length) row.appendChild(el('div', { style: 'color:var(--green);font-size:9px;' }, 'Keep: ' + c.must_keep.join(', ')));
        box.appendChild(row);
      });
    }
    if ((plan.environments || []).length) {
      box.appendChild(el('div', { style: 'font-size:10px;font-weight:700;color:var(--text);margin-bottom:4px;margin-top:8px;' }, 'Environments'));
      plan.environments.forEach(env => {
        const row = el('div', { style: 'font-size:10px;color:var(--text);padding:4px 0;border-bottom:1px solid var(--border);' });
        row.appendChild(el('div', { style: 'font-weight:600;' }, env.name));
        if (env.description) row.appendChild(el('div', { style: 'color:var(--text-dim);font-size:9px;margin-top:2px;' }, env.description));
        box.appendChild(row);
      });
    }
    if ((plan.shots || []).length) {
      box.appendChild(el('div', { style: 'font-size:10px;font-weight:700;color:var(--text);margin-bottom:4px;margin-top:8px;' }, 'Shot list'));
      plan.shots.forEach(s => {
        const row = el('div', { style: 'font-size:9px;color:var(--text-dim);padding:3px 0;' });
        row.textContent = `${s.shot_id || '?'} · ${s.shot_size || '?'} · ${s.camera || '?'} · ${s.duration_s || '?'}s — ${s.action || s.beat || ''}`;
        box.appendChild(row);
      });
    }
    host.appendChild(box);
  }

  // ─── Template picker modal ───
  async function showTemplatePicker() {
    let data;
    try { data = await api('GET', '/api/templates'); }
    catch (err) { alert('Failed to load templates: ' + err.message); return; }

    const backdrop = el('div', { style: 'position:fixed;inset:0;background:rgba(0,0,0,0.8);backdrop-filter:blur(10px);z-index:9999;display:flex;align-items:center;justify-content:center;padding:40px;' });
    const modal = el('div', { style: 'background:var(--surface);border:1px solid var(--violet);border-radius:8px;padding:24px;max-width:720px;width:100%;max-height:88vh;overflow-y:auto;' });

    modal.appendChild(el('div', { style: 'font-size:12px;font-weight:700;color:var(--violet);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px;' }, 'New Project from Template'));
    modal.appendChild(el('div', { style: 'font-size:10px;color:var(--text-dim);margin-bottom:16px;' }, 'Pick a genre and LUMN will scaffold beat-structured shots you can fill in.'));

    const grid = el('div', { style: 'display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;' });
    (data.templates || []).forEach(t => {
      const card = el('div', { style: 'padding:14px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;cursor:pointer;transition:all 0.2s;' });
      card.addEventListener('mouseenter', () => { card.style.borderColor = 'var(--violet)'; card.style.transform = 'translateY(-1px)'; });
      card.addEventListener('mouseleave', () => { card.style.borderColor = 'var(--border)'; card.style.transform = 'none'; });
      card.appendChild(el('div', { style: 'font-size:12px;font-weight:700;color:var(--text);margin-bottom:4px;' }, t.name));
      card.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);margin-bottom:8px;line-height:1.4;' }, t.description));
      const meta = el('div', { style: 'display:flex;gap:8px;font-size:8px;color:var(--cyan);text-transform:uppercase;letter-spacing:0.5px;' });
      meta.appendChild(el('span', { 'aria-label': 'Target duration' }, '⏱ ' + t.duration_target_sec + 's'));
      meta.appendChild(el('span', { 'aria-label': 'Shot count' }, '⊞ ' + t.shot_count_target + ' shots'));
      card.appendChild(meta);
      card.appendChild(el('div', { style: 'font-size:8px;color:var(--text-dim);margin-top:6px;font-style:italic;' }, t.tone));
      card.addEventListener('click', async () => {
        card.style.opacity = '0.5';
        try {
          const res = await api('POST', '/api/templates/apply', { template_id: t.id });
          alert(`Applied ${res.template}: ${res.shot_count} shots created.`);
          if (modal._v6Close) modal._v6Close();
          refreshAnchors();
          refreshTimelinePreview();
        } catch (err) {
          alert('Failed to apply template: ' + err.message);
          card.style.opacity = '1';
        }
      });
      grid.appendChild(card);
    });
    modal.appendChild(grid);

    const closeBtn = el('button', { class: 'btn btn-small', style: 'margin-top:16px;font-size:10px;', 'aria-label': 'Cancel template picker' }, 'Cancel');
    modal.appendChild(closeBtn);

    backdrop.appendChild(modal);
    const ctl = v6Modal({ backdrop, modal, labelText: 'New project from template' });
    closeBtn.addEventListener('click', () => ctl.close());
    // Re-route "Apply" card clicks through ctl.close instead of raw removeChild
    grid.querySelectorAll('div').forEach(() => {});
    modal._v6Close = ctl.close;
  }

  // ─── Timeline preview strip ───
  // ─── Song Timing (lyrics + real downbeats + sections) ───
  async function refreshSongTiming() {
    const card = $('v6SongTimingCard');
    if (!card) return;
    card.innerHTML = '<div style="font-size:9px;color:var(--text-dim);">Loading song timing...</div>';

    let timing = null;
    try {
      timing = await api('GET', '/api/v6/song/timing');
    } catch (err) {
      // 404 is expected when never analyzed — API throws for non-2xx
      timing = null;
    }

    const wrap = el('div', {
      style: 'background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px;'
    });
    const header = el('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;' });
    header.appendChild(el('div', { style: 'font-size:10px;font-weight:700;color:var(--cyan);text-transform:uppercase;letter-spacing:1px;' }, 'Song Timing'));

    const btnRow = el('div', { style: 'display:flex;gap:6px;' });
    const runBtn = el('button', {
      class: 'btn btn-small',
      style: 'border-color:var(--magenta);color:var(--magenta);font-size:9px;',
      onclick: async () => {
        runBtn.disabled = true;
        runBtn.textContent = 'Analyzing… (~60s)';
        try {
          const res = await api('POST', '/api/v6/song/analyze', { include_lyrics: true });
          console.log('[V6] song/analyze result:', res);
          await refreshSongTiming();
          if (typeof refreshTimelinePreview === 'function') refreshTimelinePreview();
        } catch (err) {
          alert('Song analyze failed: ' + err.message);
          runBtn.disabled = false;
          runBtn.textContent = timing ? 'Re-analyze' : 'Analyze Song';
        }
      },
    }, timing && timing.ok ? 'Re-analyze' : 'Analyze Song');
    btnRow.appendChild(runBtn);
    header.appendChild(btnRow);
    wrap.appendChild(header);

    if (!timing || !timing.ok) {
      wrap.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);' },
        'No timing.json yet. Click "Analyze Song" to run Whisper + beat/section detection.'));
      card.innerHTML = ''; card.appendChild(wrap);
      return;
    }

    // Summary row
    const tempo = timing.tempo || {};
    const lyr = timing.lyrics || {};
    const summary = el('div', { style: 'display:flex;gap:14px;flex-wrap:wrap;font-size:9px;color:var(--text-dim);margin-bottom:6px;' });
    summary.appendChild(el('span', {}, 'BPM: ' + (tempo.bpm || '?')));
    summary.appendChild(el('span', {}, 'Duration: ' + ((timing.source || {}).duration || 0).toFixed(2) + 's'));
    summary.appendChild(el('span', {}, 'Beats: ' + (timing.beats || []).length));
    summary.appendChild(el('span', {}, 'Downbeats: ' + (timing.downbeats || []).length));
    summary.appendChild(el('span', {}, 'Bars: ' + (timing.bars || []).length));
    summary.appendChild(el('span', {}, 'Sections: ' + (timing.sections || []).length));
    summary.appendChild(el('span', {}, 'Lyrics: ' + (lyr.words ? lyr.words.length + ' words / ' + (lyr.lines||[]).length + ' lines' : 'none')));
    wrap.appendChild(summary);

    // Sections band
    if (timing.sections && timing.sections.length) {
      const dur = (timing.source || {}).duration || 1;
      const band = el('div', {
        style: 'position:relative;height:18px;margin-bottom:4px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;',
        title: 'Section map',
      });
      const palette = {
        intro: 'rgba(124,92,255,0.35)',
        outro: 'rgba(124,92,255,0.35)',
        bridge: 'rgba(255,200,80,0.30)',
      };
      const chorusColor = 'rgba(236,73,153,0.40)';
      const verseColor  = 'rgba(0,229,255,0.22)';
      timing.sections.forEach(s => {
        const startPct = (s.start / dur) * 100;
        const widthPct = ((s.end - s.start) / dur) * 100;
        const label = s.label || '';
        let color = palette[label];
        if (!color && label.indexOf('chorus') === 0) color = chorusColor;
        else if (!color && label.indexOf('verse') === 0) color = verseColor;
        else if (!color) color = 'rgba(255,255,255,0.06)';
        const seg = el('div', {
          style: `position:absolute;top:0;bottom:0;left:${startPct}%;width:${widthPct}%;background:${color};border-right:1px solid rgba(255,255,255,0.25);display:flex;align-items:center;justify-content:center;`,
          title: `${label}  ${s.start.toFixed(2)}–${s.end.toFixed(2)}s  energy=${s.energy || 0}`,
        });
        if (widthPct > 6) {
          seg.appendChild(el('div', { style: 'font-size:8px;color:rgba(255,255,255,0.85);text-transform:uppercase;letter-spacing:0.5px;' }, label));
        }
        band.appendChild(seg);
      });
      wrap.appendChild(band);
    }

    // Lyrics preview (first 8 lines)
    if (lyr.lines && lyr.lines.length) {
      const lyricsBox = el('div', {
        style: 'max-height:120px;overflow-y:auto;font-size:9px;color:var(--text);background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);padding:6px;font-family:monospace;',
      });
      lyr.lines.slice(0, 200).forEach(ln => {
        const row = el('div', { style: 'display:flex;gap:8px;padding:1px 0;' });
        row.appendChild(el('span', { style: 'color:var(--text-dim);min-width:90px;' },
          `${ln.start.toFixed(2)}–${ln.end.toFixed(2)}`));
        row.appendChild(el('span', {}, ln.text));
        lyricsBox.appendChild(row);
      });
      wrap.appendChild(lyricsBox);
    } else {
      wrap.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);font-style:italic;' },
        'No lyrics detected (instrumental or whisper unavailable).'));
    }

    card.innerHTML = '';
    card.appendChild(wrap);
  }

  async function refreshTimelinePreview() {
    const strip = $('v6TimelineStrip');
    if (!strip) return;
    strip.innerHTML = '<div style="font-size:9px;color:var(--text-dim);">Loading timeline...</div>';
    let data;
    try { data = await api('GET', '/api/v6/timeline'); }
    catch (err) { strip.innerHTML = '<div style="font-size:9px;color:var(--amber);">Timeline load failed: ' + err.message + '</div>'; return; }

    strip.innerHTML = '';
    const header = el('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;' });
    header.appendChild(el('div', { style: 'font-size:10px;font-weight:700;color:var(--cyan);text-transform:uppercase;letter-spacing:1px;' }, 'Timeline Preview'));
    header.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);' },
      `${data.shot_count} shots · ${data.total_duration_sec.toFixed(1)}s · ${data.generated} ready · ${data.stubs} placeholder`));
    strip.appendChild(header);

    if (!data.track || data.track.length === 0) {
      strip.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);padding:12px;text-align:center;background:var(--surface);border:1px dashed var(--border);border-radius:var(--radius);' },
        'No project shots yet. Use "+ Template" to scaffold one, or generate anchors first.'));
      return;
    }

    // P2-16: Fetch beats and render them as a tick strip above the shot track
    let beatInfo = null;
    try { beatInfo = await api('GET', '/api/v6/audio/beats'); } catch (_) { beatInfo = null; }
    const total = data.total_duration_sec || 1;
    if (beatInfo && beatInfo.has_audio && beatInfo.beats && beatInfo.beats.length) {
      const beatStrip = el('div', {
        style: 'position:relative;height:16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:4px;overflow:hidden;',
        'aria-label': `Beat grid: ${beatInfo.bpm ? Math.round(beatInfo.bpm) + ' BPM' : ''}`,
      });
      const dur = Math.max(total, beatInfo.duration || total);
      const downbeatSet = new Set((beatInfo.downbeats || []).map(t => Math.round(t * 100) / 100));
      beatInfo.beats.forEach(t => {
        const pct = (t / dur) * 100;
        if (pct > 100) return;
        const isDown = downbeatSet.has(Math.round(t * 100) / 100);
        beatStrip.appendChild(el('div', {
          style: `position:absolute;top:${isDown ? 0 : 4}px;bottom:${isDown ? 0 : 4}px;left:${pct}%;width:${isDown ? 2 : 1}px;background:${isDown ? 'var(--magenta)' : 'var(--text-dim)'};opacity:${isDown ? 0.9 : 0.5};`,
          title: `${isDown ? 'Downbeat' : 'Beat'} @ ${t.toFixed(2)}s`,
        }));
      });
      if (beatInfo.bpm) {
        beatStrip.appendChild(el('div', { style: 'position:absolute;right:6px;top:1px;font-size:8px;color:var(--text-dim);font-family:monospace;' }, `${Math.round(beatInfo.bpm)} BPM`));
      }
      strip.appendChild(beatStrip);
    }

    const track = el('div', { style: 'display:flex;gap:2px;overflow-x:auto;padding:6px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);' });
    data.track.forEach(entry => {
      const pct = (entry.duration / total) * 100;
      const minWidth = Math.max(60, pct * 8);
      const statusColor = entry.status === 'ready' ? 'var(--green)'
        : entry.status === 'anchor_only' ? 'var(--amber)' : 'var(--text-dim)';
      const cell = el('div', {
        style: `min-width:${minWidth}px;flex:0 0 auto;background:var(--surface2);border:1px solid ${statusColor};border-radius:4px;padding:6px;cursor:pointer;position:relative;`,
        title: entry.shot_id + ' · ' + entry.duration + 's · ' + entry.status,
      });
      if (entry.clip_url) {
        cell.appendChild(el('video', { src: entry.clip_url, style: 'width:100%;aspect-ratio:16/9;border-radius:2px;object-fit:cover;', muted: 'true', onmouseenter: function() { this.play().catch(()=>{}); }, onmouseleave: function() { this.pause(); this.currentTime = 0; } }));
      } else if (entry.poster_url) {
        cell.appendChild(el('img', { src: entry.poster_url, style: 'width:100%;aspect-ratio:16/9;border-radius:2px;object-fit:cover;opacity:0.7;' }));
      } else {
        cell.appendChild(el('div', { style: 'width:100%;aspect-ratio:16/9;background:var(--surface);border-radius:2px;display:flex;align-items:center;justify-content:center;font-size:9px;color:var(--text-dim);' }, entry.shot_id));
      }
      const label = el('div', { style: 'font-size:8px;color:var(--text);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, entry.shot_id);
      cell.appendChild(label);
      cell.appendChild(el('div', { style: 'font-size:7px;color:' + statusColor + ';margin-top:2px;' }, entry.duration + 's · ' + entry.status));
      track.appendChild(cell);
    });
    strip.appendChild(track);
  }

  // ─── Stale-reference banner ───
  async function refreshStaleCheck() {
    const banner = $('v6StaleBanner');
    if (!banner) return;
    let data;
    try { data = await api('GET', '/api/v6/staleness'); }
    catch (err) { console.warn('[V6] Stale check failed:', err.message); return; }

    banner.innerHTML = '';
    if (!data.stale_count) return;

    const wrap = el('div', { style: 'padding:10px 12px;background:rgba(255,180,60,0.08);border:1px solid rgba(255,180,60,0.4);border-radius:var(--radius);margin-bottom:12px;display:flex;align-items:center;gap:10px;' });
    wrap.setAttribute('role', 'alert');
    wrap.appendChild(el('div', { style: 'font-size:16px;', 'aria-hidden': 'true' }, '⚠'));
    const text = el('div', { style: 'flex:1;' });
    text.appendChild(el('div', { style: 'font-size:10px;font-weight:700;color:var(--amber);' },
      `${data.stale_count} output${data.stale_count === 1 ? '' : 's'} stale — reference "${data.newest_ref_name}" changed after they were generated`));
    const detail = [];
    if (data.stale_anchors.length) detail.push(`${data.stale_anchors.length} anchor${data.stale_anchors.length === 1 ? '' : 's'}`);
    if (data.stale_clips.length) detail.push(`${data.stale_clips.length} clip${data.stale_clips.length === 1 ? '' : 's'}`);
    text.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);margin-top:2px;' }, detail.join(' · ')));
    wrap.appendChild(text);

    const dismissBtn = el('button', { class: 'btn btn-small', style: 'font-size:9px;border-color:var(--amber);color:var(--amber);' }, 'Dismiss');
    dismissBtn.addEventListener('click', () => { banner.innerHTML = ''; });
    wrap.appendChild(dismissBtn);
    banner.appendChild(wrap);
  }

  // ─── Identity gate strip (#55) ───
  // Shows locked character pills with QA scores, unlock button per char.
  // Lock happens automatically on QA-passed anchor gen; this UI is read/unlock.
  async function refreshIdentityGate() {
    const strip = $('v6IdentityGateStrip');
    if (!strip) return;
    let data;
    try { data = await api('GET', '/api/v6/identity-gate'); }
    catch (err) { return; }
    strip.innerHTML = '';
    const chars = data.characters || {};
    const names = Object.keys(chars);
    const wrap = el('div', {
      style: 'padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);display:flex;align-items:center;gap:10px;flex-wrap:wrap;'
    });
    wrap.appendChild(el('div', {
      style: 'font-size:9px;font-weight:700;color:var(--text-dim);letter-spacing:0.5px;text-transform:uppercase;'
    }, 'Identity Gate'));
    if (!names.length) {
      wrap.appendChild(el('div', { style: 'font-size:10px;color:var(--text-dim);' },
        'No characters locked — first anchor generation will auto-lock.'));
    } else {
      names.forEach(name => {
        const entry = chars[name] || {};
        const overall = entry.qa_overall != null ? entry.qa_overall.toFixed(2) : '?';
        const pill = el('div', {
          style: 'display:inline-flex;align-items:center;gap:6px;padding:3px 8px;background:var(--green-dim);border:1px solid var(--green);border-radius:12px;font-size:10px;color:var(--green);'
        });
        pill.appendChild(el('span', { 'aria-hidden': 'true' }, '🔒'));
        pill.appendChild(el('span', { style: 'font-weight:700;' }, name));
        pill.appendChild(el('span', { style: 'opacity:0.7;' }, `QA ${overall}`));
        const unlockBtn = el('button', {
          style: 'background:none;border:none;color:var(--red);cursor:pointer;font-size:10px;padding:0 2px;',
          title: `Unlock ${name}`,
          onclick: async () => {
            if (!confirm(`Unlock ${name}? Downstream clips using this character will be blocked by the identity gate.`)) return;
            try {
              await api('POST', '/api/v6/identity-gate/unlock', { character_name: name });
              v6Toast(`Unlocked ${name}`, 'info');
              refreshIdentityGate();
            } catch (err) { v6Toast(`Unlock failed: ${err.message}`, 'error'); }
          }
        }, '×');
        pill.appendChild(unlockBtn);
        wrap.appendChild(pill);
      });
    }
    strip.appendChild(wrap);
  }

  // ─── Version history modal ───
  async function showVersionHistory(shotId) {
    let data;
    try { data = await api('GET', '/api/v6/clip-versions/' + encodeURIComponent(shotId)); }
    catch (err) { alert('Failed to load versions: ' + err.message); return; }

    const backdrop = el('div', { style: 'position:fixed;inset:0;background:rgba(0,0,0,0.85);backdrop-filter:blur(10px);z-index:9999;display:flex;align-items:center;justify-content:center;padding:40px;' });
    const modal = el('div', { style: 'background:var(--surface);border:1px solid var(--cyan);border-radius:8px;padding:20px;max-width:900px;width:100%;max-height:90vh;overflow-y:auto;' });

    modal.appendChild(el('div', { style: 'font-size:11px;font-weight:700;color:var(--cyan);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px;' }, 'Version History — ' + shotId));
    modal.appendChild(el('div', { style: 'font-size:9px;color:var(--text-dim);margin-bottom:16px;' },
      (data.versions || []).length + ' versions on disk'));

    if (!data.versions || data.versions.length === 0) {
      modal.appendChild(el('div', { style: 'font-size:10px;color:var(--text-dim);padding:20px;text-align:center;' }, 'No versions yet — generate a clip first.'));
    } else {
      const grid = el('div', { style: 'display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;' });
      data.versions.slice().reverse().forEach(v => {
        const card = el('div', { style: 'padding:10px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;' });
        card.appendChild(el('video', { src: v.url, controls: 'true', style: 'width:100%;border-radius:4px;' }));
        const head = el('div', { style: 'display:flex;justify-content:space-between;margin-top:8px;' });
        head.appendChild(el('div', { style: 'font-size:11px;font-weight:700;color:var(--cyan);' }, 'v' + v.version));
        if (v.meta.cost_est) head.appendChild(el('div', { style: 'font-size:9px;color:var(--green);font-family:JetBrains Mono,monospace;' }, '$' + v.meta.cost_est.toFixed(3)));
        card.appendChild(head);
        const m = v.meta || {};
        if (m.tier) card.appendChild(el('div', { style: 'font-size:8px;color:var(--text-dim);margin-top:2px;' }, m.tier + ' · ' + (m.duration || '?') + 's'));
        if (m.ts) card.appendChild(el('div', { style: 'font-size:8px;color:var(--text-dim);' }, new Date(m.ts * 1000).toLocaleString()));
        if (m.prompt) card.appendChild(el('div', { style: 'font-size:8px;color:var(--text);margin-top:6px;padding:6px;background:var(--surface);border-radius:3px;max-height:60px;overflow:hidden;font-style:italic;' }, m.prompt.slice(0, 200)));
        grid.appendChild(card);
      });
      modal.appendChild(grid);
    }

    const closeBtn = el('button', { class: 'btn btn-small', style: 'margin-top:16px;font-size:10px;', 'aria-label': 'Close version history' }, 'Close');
    modal.appendChild(closeBtn);

    backdrop.appendChild(modal);
    const ctl = v6Modal({ backdrop, modal, labelText: 'Version history for ' + shotId });
    closeBtn.addEventListener('click', () => ctl.close());
  }

  // ─── Mount into page ───
  function mount() {
    const target = $('shotsContainer');
    if (!target) return;

    // Find insertion point — after the stats bar
    const statsBar = $('shotsStatsBar');
    const insertAfter = statsBar || target.querySelector('[style*="max-width"]')?.firstChild;

    // P1-9: Promote V6 Pipeline out of <details> — it IS the Shots stage body.
    // Use a plain section with a sticky header instead of collapsible details.
    const panel = el('section', {
      style: 'margin-bottom:16px;',
      'aria-label': 'V6 Pipeline — Shots stage'
    });

    const summaryHdr = el('div', {
      style: 'padding:12px 16px;background:linear-gradient(135deg,rgba(0,229,255,0.08),rgba(124,92,255,0.08));border:1px solid rgba(0,229,255,0.2);border-radius:var(--radius-lg) var(--radius-lg) 0 0;display:flex;align-items:center;gap:10px;'
    });
    summaryHdr.innerHTML = `
      <span style="font-size:14px;" aria-hidden="true">&#127916;</span>
      <div style="flex:1;">
        <div style="font-size:12px;font-weight:700;color:var(--text);">Shot Pipeline <span style="font-size:9px;font-weight:400;color:var(--cyan);margin-left:4px;">Gemini + Kling</span></div>
        <div style="font-size:10px;color:var(--text-dim);">Anchor stills via Gemini, video clips via Kling 3.0. Sonnet QA on every shot.</div>
      </div>
      <button class="btn btn-small" onclick="event.stopPropagation();window._v6Refresh()" style="border-color:var(--cyan);color:var(--cyan);font-size:9px;" aria-label="Refresh shot pipeline">Refresh</button>
    `;
    panel.appendChild(summaryHdr);

    const content = el('div', {
      style: 'padding:16px;background:linear-gradient(135deg,rgba(0,229,255,0.03),rgba(124,92,255,0.03));border:1px solid rgba(0,229,255,0.1);border-top:none;border-radius:0 0 var(--radius-lg) var(--radius-lg);'
    });

    content.appendChild(buildToolbar());
    content.appendChild(el('div', { id: 'v6StaleBanner' }));
    content.appendChild(buildRefUploadPanel());
    content.appendChild(buildGlobalConfig());
    content.appendChild(el('div', { id: 'v6IdentityGateStrip', style: 'margin-bottom:8px;' }));
    content.appendChild(el('div', { id: 'v6SongTimingCard', style: 'margin-bottom:12px;' }));
    content.appendChild(el('div', { id: 'v6TimelineStrip', style: 'margin-bottom:12px;' }));
    content.appendChild(el('div', { id: 'v6GateBatchBar' }));
    content.appendChild(el('div', { id: 'v6ShotGrid' }));
    panel.appendChild(content);

    // Insert into the shots workspace
    if (statsBar && statsBar.parentNode) {
      statsBar.parentNode.insertBefore(panel, statsBar.nextSibling);
    } else {
      target.querySelector('[style*="max-width"]')?.appendChild(panel);
    }

    // Expose refresh + other V6 entry points so they can be invoked
    // programmatically from any tab (via console, global keyboard shortcut,
    // or tests) even when the shots workspace isn't the active view.
    window._v6Refresh = refreshAnchors;
    window._v6ShowBriefExpand = showBriefExpand;
    window._v6RefreshIdentityGate = refreshIdentityGate;

    // Global keyboard shortcut: Ctrl/Cmd + Shift + I → From Idea modal.
    // Reachable from any tab so the feature isn't trapped behind the
    // shots-tab visibility check.
    if (!window._v6IdeaShortcutBound) {
      window._v6IdeaShortcutBound = true;
      document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'I' || e.key === 'i')) {
          e.preventDefault();
          showBriefExpand();
        }
      });
    }

    // Initial load
    refreshAnchors();
    refreshReferences();
    refreshScenes();
    refreshPOSForChips();
    refreshSongTiming();
    refreshTimelinePreview();
    refreshStaleCheck();
    refreshIdentityGate();

    // ─── Autosave: debounced POST of any shot-form changes ───
    let autosaveTimer = null;
    let lastAutosaveHash = '';
    function collectProjectState() {
      const shots = [];
      document.querySelectorAll('[data-v6-shot-id]').forEach(form => {
        const sid = form.getAttribute('data-v6-shot-id');
        const shot = { shot_id: sid };
        form.querySelectorAll('input[name], select[name], textarea[name]').forEach(f => {
          const key = f.getAttribute('name').replace(sid + '_', '');
          shot[key] = f.value;
        });
        shots.push(shot);
      });
      return { shots, _client_ts: Date.now() };
    }
    function scheduleAutosave() {
      if (autosaveTimer) clearTimeout(autosaveTimer);
      autosaveTimer = setTimeout(async () => {
        const state = collectProjectState();
        const hash = JSON.stringify(state.shots);
        if (hash === lastAutosaveHash) return;
        lastAutosaveHash = hash;
        try {
          await api('POST', '/api/v6/project/autosave', state);
          const ind = document.getElementById('v6AutosaveIndicator');
          if (ind) {
            ind.textContent = '✓ saved';
            ind.style.opacity = '1';
            setTimeout(() => { ind.style.opacity = '0.3'; }, 1500);
          }
        } catch (err) {
          console.warn('[V6] Autosave failed:', err.message);
          const ind = document.getElementById('v6AutosaveIndicator');
          if (ind) { ind.textContent = '⚠ save failed'; ind.style.color = 'var(--amber)'; }
        }
      }, 1500);
    }
    document.addEventListener('input', (e) => {
      if (e.target.closest('[data-v6-shot-id]')) scheduleAutosave();
    });
    document.addEventListener('change', (e) => {
      if (e.target.closest('[data-v6-shot-id]')) scheduleAutosave();
    });
    // Mount the indicator in the panel header
    const indWrap = el('div', { id: 'v6AutosaveIndicator', style: 'font-size:9px;color:var(--text-dim);margin-left:8px;opacity:0.3;transition:opacity 0.3s;' }, 'autosave ready');
    const summaryDiv = document.querySelector('#v6PipelinePanel summary div[style*="flex:1"]');
    if (summaryDiv) summaryDiv.appendChild(indWrap);
  }

  // Mount when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    // Delay slightly to let other scripts initialize
    setTimeout(mount, 500);
  }

})();
