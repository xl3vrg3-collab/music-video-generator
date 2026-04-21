"""Build a print-optimized manifesto_print.html from public/manifesto.html.

Strips cinema intro/JS, videos, fixed nav, reveal animations. Forces all
accordions open. Adds @page rules and page-break controls. Produces a clean
document that Edge can print to PDF with high fidelity.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "public", "manifesto.html")
OUT_HTML = os.path.join(REPO, "public", "manifesto_print.html")
OUT_PDF = os.path.join(REPO, "LUMN Manifesto.pdf")

PRINT_CSS = """
/* ═══ PRINT-MODE OVERRIDES ═══ */
@page { size: A4; margin: 18mm 16mm; }
@media print {
  html, body { background: #0b0b0d !important; }
  .wp-section { page-break-inside: avoid; }
  .wp-block, .wp-acc, .wp-stat { page-break-inside: avoid; }
  .wp-section-header { page-break-after: avoid; }
  h2, h3, h4 { page-break-after: avoid; }
  .cover-page { page-break-after: always; }
  .wp-toc { page-break-after: always; }
  a { color: inherit !important; text-decoration: none !important; }
}

/* kill fixed/overflow behaviors that break PDF pagination */
html, body { overflow: visible !important; height: auto !important; }
body.scrollable { overflow: visible !important; }
body::before, body::after { display: none !important; }

/* kill cinema overlay entirely */
#cinema, #skip, #seq-progress, .mark { display: none !important; }

/* sticky nav → static TOC */
.wp-nav { position: static !important; background: transparent !important;
  backdrop-filter: none !important; border: none !important;
  margin: 0 0 24px 0 !important; padding: 0 !important; }
.wp-nav-inner { overflow: visible !important; flex-wrap: wrap !important;
  justify-content: center !important; gap: 6px !important; }
.wp-nav a { border: 1px solid rgba(255,255,255,0.1) !important;
  color: rgba(255,255,255,0.75) !important; }

/* reveal animations → always visible */
.reveal { opacity: 1 !important; transform: none !important; }

/* accordions → open and static */
.wp-acc { max-height: none !important; }
.wp-acc .wp-acc-body { max-height: none !important; overflow: visible !important; }
.wp-acc .arrow { display: none !important; }
.wp-acc-head { cursor: default !important; }
.wp-acc-head:hover { background: transparent !important; }

/* bars pre-filled */
.wp-bar-fill { transition: none !important; }

/* hero — trim the huge min-height so it doesn't eat a whole page */
.wp-hero { min-height: auto !important; padding: 40px 0 24px !important; }

/* hide the proof videos (can't render in PDF); keep grid rows with images */
.wp-proof-card video { display: none !important; }
.wp-proof-card.video-card {
  display: flex !important; align-items: center !important; justify-content: center !important;
  font-size: 9px; letter-spacing: 3px; text-transform: uppercase;
  color: rgba(255,255,255,0.35); font-weight: 400;
}
.wp-proof-card.video-card::before { content: "Kling 3.0 · video clip"; }
.wp-proof-card.video-card .wp-proof-meta { display: none; }

/* COVER PAGE */
.cover-page {
  min-height: 100vh; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center;
  padding: 40px 24px; position: relative;
}
.cover-page .eyebrow {
  font-size: 10px; letter-spacing: 8px; text-transform: uppercase;
  color: rgba(255,255,255,0.45); font-weight: 400; margin-bottom: 32px;
}
.cover-page .cover-wordmark {
  font-size: clamp(72px, 16vw, 180px); letter-spacing: clamp(10px, 3vw, 40px);
  line-height: 1; margin-bottom: 48px;
}
.cover-page .cover-tag {
  font-size: clamp(18px, 3vw, 32px); font-weight: 300;
  letter-spacing: clamp(2px, 0.4vw, 6px); text-transform: uppercase;
  color: rgba(255,255,255,0.85); max-width: 640px; line-height: 1.2;
  margin-bottom: 64px;
}
.cover-page .cover-meta {
  display: flex; gap: 48px; justify-content: center; flex-wrap: wrap;
  margin-top: 32px; padding-top: 32px;
  border-top: 1px solid rgba(255,255,255,0.1); max-width: 720px;
}
.cover-page .cover-meta > div {
  font-size: 9px; letter-spacing: 4px; text-transform: uppercase;
  color: rgba(255,255,255,0.45); font-weight: 400;
}
.cover-page .cover-meta strong {
  display: block; font-weight: 300; font-size: 14px; letter-spacing: 2px;
  color: rgba(255,255,255,0.85); margin-top: 8px;
}
.cover-page .cover-foot {
  position: absolute; bottom: 32px; left: 0; right: 0;
  font-size: 9px; letter-spacing: 6px; text-transform: uppercase;
  color: rgba(255,255,255,0.28);
}

/* Layout showcase pages */
.layout-section-divider {
  min-height: 100vh; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center;
  padding: 40px 24px; page-break-after: always;
}
.layout-divider-eyebrow {
  font-size: 10px; letter-spacing: 8px; text-transform: uppercase;
  color: rgba(255,255,255,0.45); font-weight: 400; margin-bottom: 32px;
}
.layout-divider-title {
  font-family: 'Inter Tight', sans-serif; font-weight: 300;
  font-size: clamp(42px, 8vw, 72px); letter-spacing: clamp(2px, 0.6vw, 8px);
  text-transform: uppercase; color: rgba(255,255,255,0.92); line-height: 1.05;
  margin-bottom: 20px;
}
.layout-divider-sub {
  font-size: 13px; line-height: 1.75; color: rgba(255,255,255,0.55);
  font-weight: 300; letter-spacing: 0.3px; max-width: 520px;
}

.layout-showcase {
  min-height: 100vh; display: flex; flex-direction: column;
  justify-content: center; padding: 48px clamp(24px, 6vw, 80px);
  page-break-after: always; page-break-inside: avoid;
  position: relative;
}
.layout-showcase.layout-dark { background: #060606; }
.layout-showcase.layout-light {
  background: #f4f2ed; color: #1a1a1a;
}
.layout-showcase.layout-light .layout-cap-num,
.layout-showcase.layout-light .layout-cap-label,
.layout-showcase.layout-light .layout-cap-sub { color: rgba(0,0,0,0.72); }
.layout-showcase.layout-light .layout-cap-num { color: rgba(0,0,0,0.38); }
.layout-showcase.layout-light .layout-cap-sub { color: rgba(0,0,0,0.55); }

.layout-caption {
  max-width: 720px; margin: 0 auto 32px; text-align: center;
}
.layout-cap-num {
  font-size: 10px; letter-spacing: 6px; text-transform: uppercase;
  color: rgba(255,255,255,0.38); font-weight: 400; margin-bottom: 14px;
}
.layout-cap-label {
  font-family: 'Inter Tight', sans-serif; font-weight: 300;
  font-size: clamp(22px, 3.4vw, 34px); letter-spacing: clamp(2px, 0.3vw, 5px);
  text-transform: uppercase; color: rgba(255,255,255,0.92); line-height: 1.15;
  margin-bottom: 14px;
}
.layout-cap-sub {
  font-size: 12px; line-height: 1.8; color: rgba(255,255,255,0.55);
  font-weight: 300; letter-spacing: 0.3px; max-width: 640px; margin: 0 auto;
}
.layout-frame {
  max-width: 100%; margin: 0 auto; border-radius: 4px; overflow: hidden;
  border: 1px solid rgba(255,255,255,0.08);
  box-shadow: 0 20px 60px -20px rgba(0,0,0,0.55);
}
.layout-showcase.layout-light .layout-frame {
  border: 1px solid rgba(0,0,0,0.1);
  box-shadow: 0 20px 60px -20px rgba(0,0,0,0.18);
}
.layout-frame img {
  display: block; width: 100%; height: auto; max-height: 70vh;
  object-fit: contain;
}

/* Table of contents page */
.wp-toc {
  max-width: 720px; margin: 0 auto; padding: 80px 24px;
}
.wp-toc h1 {
  font-size: 14px; letter-spacing: 8px; text-transform: uppercase;
  color: rgba(255,255,255,0.55); font-weight: 400; margin-bottom: 48px;
  text-align: center;
}
.wp-toc ol { list-style: none; padding: 0; margin: 0; counter-reset: toc; }
.wp-toc li {
  counter-increment: toc; padding: 14px 0;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  display: flex; align-items: baseline; gap: 20px;
  font-size: 15px; font-weight: 300; color: rgba(255,255,255,0.82);
  letter-spacing: 1px;
}
.wp-toc li::before {
  content: counter(toc, decimal-leading-zero);
  font-size: 10px; letter-spacing: 3px;
  color: rgba(255,255,255,0.38); min-width: 28px;
}
.wp-toc li .toc-title { flex: 1; text-transform: uppercase; letter-spacing: 3px; }
.wp-toc li .toc-sub { font-size: 11px; color: rgba(255,255,255,0.45); font-weight: 300; letter-spacing: 0.5px; text-transform: none; }
"""

COVER_HTML = """
<section class="cover-page">
  <div class="eyebrow">LUMN Studio · Manifesto & Whitepaper</div>
  <div class="lumn-wordmark cover-wordmark">LUMN</div>
  <div class="cover-tag">Build what you see<br>in your head.</div>
  <div class="cover-meta">
    <div>Version<strong>V6</strong></div>
    <div>Pipeline<strong>5 Stages</strong></div>
    <div>Engines<strong>Gemini · Kling · Claude</strong></div>
    <div>Runtime<strong>fal.ai</strong></div>
  </div>
  <div class="cover-foot">LUMN STUDIO · TECHNICAL WHITEPAPER</div>
</section>
"""

TOC_HTML = """
<section class="wp-toc">
  <h1>Contents</h1>
  <ol>
    <li><span class="toc-title">Overview</span><span class="toc-sub">What LUMN is and who it is for</span></li>
    <li><span class="toc-title">The Pipeline</span><span class="toc-sub">Five stages from idea to film</span></li>
    <li><span class="toc-title">Workspace</span><span class="toc-sub">Stage-based interface and Power Mode</span></li>
    <li><span class="toc-title">AI Engines</span><span class="toc-sub">Gemini, Kling, Claude, fal.ai</span></li>
    <li><span class="toc-title">PromptOS &amp; Assets</span><span class="toc-sub">Canonical sheets and composed anchors</span></li>
    <li><span class="toc-title">Cinematic System</span><span class="toc-sub">150+ framing, motion, lighting presets</span></li>
    <li><span class="toc-title">Production Pipeline</span><span class="toc-sub">12-state machine, shot families, anchors</span></li>
    <li><span class="toc-title">Director Brain</span><span class="toc-sub">Learning system and style profile</span></li>
    <li><span class="toc-title">Audio</span><span class="toc-sub">Beat sync, ducking, native scene sound</span></li>
    <li><span class="toc-title">Export</span><span class="toc-sub">Presets, upscaling, beat-synced assembly</span></li>
  </ol>
</section>
"""

# Layout showcase — welcome + home in both themes, one image per page.
LAYOUT_HTML_TEMPLATE = """
<section class="layout-section-divider">
  <div class="layout-divider-eyebrow">PART ONE</div>
  <div class="layout-divider-title">Layout &amp; Interface</div>
  <div class="layout-divider-sub">The studio workspace in both themes.</div>
</section>

<section class="layout-showcase layout-dark">
  <div class="layout-caption">
    <div class="layout-cap-num">01</div>
    <div class="layout-cap-label">Studio &mdash; Dark</div>
    <div class="layout-cap-sub">Brief stage. Five-step progress header, project pill, cost tracker, and the Auto Director panel with project type toggle, idea input, style, and duration.</div>
  </div>
  <div class="layout-frame">
    <img src="file:///{repo}/public/doc_assets/home_ui_dark.png" alt="LUMN studio workspace &mdash; dark theme">
  </div>
</section>

<section class="layout-showcase layout-light">
  <div class="layout-caption">
    <div class="layout-cap-num">02</div>
    <div class="layout-cap-label">Studio &mdash; Light</div>
    <div class="layout-cap-sub">Same stage, inverted palette. Stepper, panels, and bottom action bar preserve their positions. The orange MAKE MY MOVIE CTA holds its chromatic weight across both themes.</div>
  </div>
  <div class="layout-frame">
    <img src="file:///{repo}/public/doc_assets/home_ui_light.png" alt="LUMN studio workspace &mdash; light theme">
  </div>
</section>
"""


def build_print_html() -> str:
    with open(SRC, "r", encoding="utf-8") as f:
        html = f.read()

    # 1) Strip cinema overlay block (div#cinema … /div), skip button, progress bar, mark.
    html = re.sub(
        r'<!-- ═══ TITLE SEQUENCE CINEMA ═══ -->.*?</div>\s*<div id="skip">.*?</div>\s*<div id="seq-progress"></div>',
        '',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(r'<div class="mark">.*?</div>', '', html, count=1, flags=re.DOTALL)

    # 2) Strip the end-of-file <script> … </script> (title sequence + reveal observer).
    html = re.sub(r'<script>\s*\(function\(\).*?\)\(\);\s*</script>', '', html, count=1, flags=re.DOTALL)

    # 3) Tag the 2 video proof cards so CSS can render them as placeholders.
    html = html.replace(
        '<div class="wp-proof-card">\n        <video src="/api/clips/clip_000.mp4"',
        '<div class="wp-proof-card video-card">\n        <video src="/api/clips/clip_000.mp4"',
    )
    html = html.replace(
        '<div class="wp-proof-card">\n        <video src="/api/clips/clip_002.mp4"',
        '<div class="wp-proof-card video-card">\n        <video src="/api/clips/clip_002.mp4"',
    )

    # 4) Rewrite image src paths to absolute file:// URLs so headless Chrome can find them.
    def _abs_image(match: re.Match) -> str:
        rel = match.group(1)
        abs_path = os.path.join(REPO, rel.lstrip("/"))
        return f'src="file:///{abs_path.replace(os.sep, "/")}"'
    html = re.sub(r'src="(/public/[^"]+)"', _abs_image, html)

    # 5) Force body.scrollable from the start + add .visible to every .reveal + .open to every .wp-acc.
    html = html.replace('<body>', '<body class="scrollable">', 1)
    html = re.sub(r'class="(reveal[^"]*)"', r'class="\1 visible"', html)
    html = re.sub(r'class="wp-acc"', 'class="wp-acc open"', html)

    # 6) Inject cover + layout showcase + TOC at the top of <body>.
    layout_html = LAYOUT_HTML_TEMPLATE.format(repo=REPO.replace(os.sep, "/"))
    html = html.replace(
        '<body class="scrollable">',
        '<body class="scrollable">\n' + COVER_HTML + '\n' + layout_html + '\n' + TOC_HTML,
        1,
    )

    # 7) Append print CSS immediately before </head>.
    html = html.replace('</head>', f'<style>{PRINT_CSS}</style>\n</head>', 1)

    # 8) Strip the onclick handlers on accordions (they're all open already).
    html = html.replace('<div class="wp-acc open" onclick="this.classList.toggle(\'open\')">', '<div class="wp-acc open">')

    # 9) Neutralise the footer "ENTER STUDIO" href (it resolves to file:///C:/ in print).
    html = html.replace('<a href="/" class="cta-enter">ENTER STUDIO</a>', '<span class="cta-enter">LUMN STUDIO</span>')

    return html


def render_pdf(html_path: str, pdf_path: str) -> None:
    import tempfile
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not os.path.isfile(edge):
        edge = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    file_url = "file:///" + html_path.replace(os.sep, "/")
    # Fresh profile dir + cache disabled so no browser-side caching of images.
    with tempfile.TemporaryDirectory(prefix="lumn_pdf_") as tmp_profile:
        cmd = [
            edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
            "--hide-scrollbars", "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=10000",
            f"--user-data-dir={tmp_profile}",
            "--disable-http-cache", "--disk-cache-size=1",
            f"--print-to-pdf={pdf_path}", file_url,
        ]
        subprocess.run(cmd, check=True, capture_output=True)


def main() -> None:
    print(f"Reading  {SRC}")
    html = build_print_html()
    print(f"Writing  {OUT_HTML}  ({len(html):,} bytes)")
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Rendering {OUT_PDF}")
    render_pdf(OUT_HTML, OUT_PDF)
    size = os.path.getsize(OUT_PDF)
    print(f"Done. {OUT_PDF}  ({size/1_048_576:.2f} MB)")


if __name__ == "__main__":
    sys.exit(main())
