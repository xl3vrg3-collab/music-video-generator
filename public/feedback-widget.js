// LUMN beta feedback widget — self-contained floating button + modal.
// Injects on every page that includes this script. Posts to /api/feedback.
// Auth-optional: works for both signed-in users and anon visitors.

(function () {
  if (window.__lumnFeedbackWidgetLoaded) return;
  window.__lumnFeedbackWidgetLoaded = true;

  const css = `
    .lumn-fb-btn {
      position: fixed; bottom: 18px; right: 18px; z-index: 99999;
      width: 44px; height: 44px; border-radius: 50%; border: 0;
      background: linear-gradient(180deg,#5a5aff,#3a3ad8); color:#fff;
      font: 600 18px -apple-system,system-ui,sans-serif; cursor: pointer;
      box-shadow: 0 8px 24px -8px rgba(0,0,0,.5);
      transition: transform .15s, filter .15s;
    }
    .lumn-fb-btn:hover { transform: translateY(-2px); filter: brightness(1.1); }
    .lumn-fb-modal-bg {
      position: fixed; inset: 0; background: rgba(0,0,0,.55);
      z-index: 99998; display: none; align-items: center; justify-content: center;
      backdrop-filter: blur(4px);
    }
    .lumn-fb-modal-bg.show { display: flex; }
    .lumn-fb-modal {
      width: 92%; max-width: 440px;
      background: #14141a; border: 1px solid #24242e; border-radius: 14px;
      padding: 24px; color: #e8e8ec;
      font: 14px/1.5 -apple-system, system-ui, sans-serif;
    }
    .lumn-fb-modal h3 { margin: 0 0 4px; font-size: 17px; }
    .lumn-fb-modal p.sub { margin: 0 0 16px; color: #8a8a96; font-size: 12px; }
    .lumn-fb-modal label {
      display: block; font-size: 11px; color: #8a8a96;
      text-transform: uppercase; letter-spacing: .05em; margin: 12px 0 6px;
    }
    .lumn-fb-modal select, .lumn-fb-modal textarea {
      width: 100%; padding: 10px 12px; background: #0b0b10;
      border: 1px solid #24242e; border-radius: 8px; color: #e8e8ec;
      font: inherit; outline: none; box-sizing: border-box;
    }
    .lumn-fb-modal textarea { min-height: 110px; resize: vertical; }
    .lumn-fb-modal select:focus, .lumn-fb-modal textarea:focus { border-color: #5a5aff; }
    .lumn-fb-modal .row { display: flex; gap: 8px; margin-top: 18px; }
    .lumn-fb-modal button {
      flex: 1; padding: 11px; border: 0; border-radius: 8px;
      font: 600 13px inherit; cursor: pointer;
    }
    .lumn-fb-modal .send {
      background: linear-gradient(180deg,#5a5aff,#3a3ad8); color: #fff;
    }
    .lumn-fb-modal .cancel { background: #1e1e26; color: #b8b8c2; }
    .lumn-fb-toast {
      position: fixed; bottom: 76px; right: 18px; z-index: 99999;
      background: #1a3a1a; border: 1px solid #2d5d2d; color: #aaffaa;
      padding: 10px 14px; border-radius: 8px; font: 13px system-ui;
      opacity: 0; transition: opacity .25s; pointer-events: none;
    }
    .lumn-fb-toast.show { opacity: 1; }
    .lumn-fb-toast.err { background: #3a1a1a; border-color: #5d2d2d; color: #ffaaaa; }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const btn = document.createElement('button');
  btn.className = 'lumn-fb-btn';
  btn.title = 'Report a bug or suggestion';
  btn.textContent = '✉';
  document.body.appendChild(btn);

  const bg = document.createElement('div');
  bg.className = 'lumn-fb-modal-bg';
  bg.innerHTML = `
    <div class="lumn-fb-modal" role="dialog" aria-label="Send feedback">
      <h3>Send feedback</h3>
      <p class="sub">Bugs, weird behavior, ideas — all welcome.</p>
      <label>Type</label>
      <select id="lumnFbCat">
        <option value="bug">Bug — something broke</option>
        <option value="ux">UX — confusing or clunky</option>
        <option value="quality">Output quality — not what I wanted</option>
        <option value="idea">Idea / feature request</option>
        <option value="other">Other</option>
      </select>
      <label>What happened?</label>
      <textarea id="lumnFbMsg" placeholder="Be specific. What did you click, what did you expect, what did you see?"></textarea>
      <div class="row">
        <button class="cancel" id="lumnFbCancel">Cancel</button>
        <button class="send" id="lumnFbSend">Send</button>
      </div>
    </div>
  `;
  document.body.appendChild(bg);

  const toast = document.createElement('div');
  toast.className = 'lumn-fb-toast';
  document.body.appendChild(toast);

  function showToast(msg, isErr) {
    toast.textContent = msg;
    toast.classList.toggle('err', !!isErr);
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2500);
  }

  function open() {
    bg.classList.add('show');
    setTimeout(() => document.getElementById('lumnFbMsg')?.focus(), 50);
  }
  function close() { bg.classList.remove('show'); }

  btn.addEventListener('click', open);
  bg.addEventListener('click', e => { if (e.target === bg) close(); });
  bg.querySelector('#lumnFbCancel').addEventListener('click', close);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && bg.classList.contains('show')) close();
  });

  bg.querySelector('#lumnFbSend').addEventListener('click', async () => {
    const cat = document.getElementById('lumnFbCat').value;
    const msg = document.getElementById('lumnFbMsg').value.trim();
    if (!msg) {
      showToast('Add a description first', true);
      return;
    }
    const sendBtn = bg.querySelector('#lumnFbSend');
    sendBtn.disabled = true;
    sendBtn.textContent = 'Sending…';
    try {
      const r = await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          category: cat,
          message: msg,
          url: window.location.href,
          context: JSON.stringify({
            viewport: `${window.innerWidth}x${window.innerHeight}`,
            ua: navigator.userAgent.slice(0, 200),
          }),
        }),
      });
      if (!r.ok) throw new Error('status ' + r.status);
      document.getElementById('lumnFbMsg').value = '';
      close();
      showToast('Thanks — feedback sent');
    } catch (e) {
      showToast('Failed to send: ' + e.message, true);
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = 'Send';
    }
  });
})();
