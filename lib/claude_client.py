"""
Claude API client for LUMN Studio — Opus-everywhere (2026-04-19).

Roles:
  - Opus 4.7: DEFAULT for auditing, prompting, storytelling, vision review
  - Sonnet 4.6: fallback only when explicitly forced (downgrade path)
  - Haiku 4.5: RETIRED from auto-escalation — documented to hallucinate
               (feedback_haiku_bokeh_hallucination.md, feedback_haiku_cant_judge_video.md).
               Constant kept for any legacy callsite that forces it.

All calls return structured JSON. Vision support for image analysis.
Opus wrapper (`call_opus`) supports extended thinking + prompt caching.
"""

import base64
import json
import os
import re
import time

from dotenv import load_dotenv
load_dotenv()

import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPUS_MODEL   = "claude-opus-4-7"
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL  = "claude-haiku-4-5-20251001"  # retired from auto-escalation

DEFAULT_MODEL = OPUS_MODEL  # single switch point — flip here to roll back

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Escalation Rules
# ---------------------------------------------------------------------------

def should_escalate(shot_priority: str = "standard",
                    haiku_score: float = 1.0,
                    failure_types: list = None,
                    attempt_count: int = 0,
                    force_escalate: bool = False) -> bool:
    """Legacy escalation predicate. Opus is now the default — escalation
    mostly means "enable extended thinking". Kept for call-sites that still
    branch on this. Returns True when thinking budget should be enabled.
    """
    if force_escalate:
        return True
    if shot_priority == "hero":
        return True
    if 0.45 <= haiku_score <= 0.65:
        return True
    if failure_types and len(failure_types) >= 2:
        return True
    if attempt_count >= 2:
        return True
    return False


def choose_model(shot_priority: str = "standard",
                 haiku_score: float = None,
                 failure_types: list = None,
                 attempt_count: int = 0,
                 force_model: str = None) -> str:
    """Return the model ID to use. Default = Opus across the board.
    force_model accepts 'opus', 'sonnet', 'haiku' for explicit override.
    """
    if force_model == "opus":
        return OPUS_MODEL
    if force_model == "sonnet":
        return SONNET_MODEL
    if force_model == "haiku":
        return HAIKU_MODEL
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Image Helpers
# ---------------------------------------------------------------------------

def load_image_b64(path: str) -> str:
    """Load an image file as base64 string."""
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


# Claude API rejects images >5MB base64-decoded. Keep a buffer under that.
CLAUDE_IMAGE_MAX_BYTES = 4_500_000


def _downscale_to_fit(image_path: str, max_bytes: int = CLAUDE_IMAGE_MAX_BYTES) -> tuple:
    """If the file is under max_bytes, return (bytes, media_type) as-is.
    Otherwise open it with PIL, progressively downscale + JPEG-compress until
    it fits. Returns (jpeg_bytes, "image/jpeg")."""
    try:
        raw_size = os.path.getsize(image_path)
    except OSError:
        raw_size = 0
    ext = os.path.splitext(image_path)[1].lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")

    if raw_size and raw_size <= max_bytes:
        with open(image_path, "rb") as f:
            return f.read(), media_type

    from PIL import Image
    import io

    img = Image.open(image_path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    w, h = img.size
    scale = 1.0
    for attempt in range(8):
        nw = max(512, int(w * scale))
        nh = max(512, int(h * scale))
        resized = img.resize((nw, nh), Image.LANCZOS) if scale < 1.0 else img
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=88, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            print(f"[claude_client] resized {os.path.basename(image_path)}: {raw_size} → {len(data)} bytes ({nw}x{nh})")
            return data, "image/jpeg"
        scale *= 0.8

    # last resort: aggressive
    resized = img.resize((768, int(768 * h / w)) if w >= h else (int(768 * w / h), 768), Image.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=75, optimize=True)
    data = buf.getvalue()
    print(f"[claude_client] hard-resized {os.path.basename(image_path)}: {raw_size} → {len(data)} bytes (768 fit)")
    return data, "image/jpeg"


def _image_content_block(image_path: str) -> dict:
    """Build a Claude API image content block from a file path.
    Auto-downscales images over ~4.5MB to satisfy the 5MB API limit."""
    data, media_type = _downscale_to_fit(image_path)
    b64 = base64.standard_b64encode(data).decode("utf-8")

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": b64,
        },
    }


# ---------------------------------------------------------------------------
# Core API Calls
# ---------------------------------------------------------------------------

def call_text(prompt: str, system: str = "", model: str = None,
              max_tokens: int = 2048) -> str:
    """Simple text-only Claude call. Defaults to Opus."""
    client = _get_client()
    model = model or DEFAULT_MODEL

    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    start = time.time()
    msg = client.messages.create(**kwargs)
    elapsed = time.time() - start

    text = msg.content[0].text if msg.content else ""
    print(f"[Claude/{model.split('-')[1]}] {len(text)} chars, {elapsed:.1f}s")
    return text


def call_json(prompt: str, system: str = "", model: str = None,
              max_tokens: int = 2048) -> dict:
    """Claude call expecting JSON response. Parses and returns dict."""
    raw = call_text(prompt, system, model, max_tokens)
    return _parse_json_response(raw)


def call_vision(prompt: str, image_paths: list, system: str = "",
                model: str = None, max_tokens: int = 2048) -> str:
    """Claude call with vision (image analysis). Defaults to Opus."""
    client = _get_client()
    model = model or DEFAULT_MODEL

    content = []
    for path in image_paths:
        if os.path.isfile(path):
            content.append(_image_content_block(path))
    content.append({"type": "text", "text": prompt})

    messages = [{"role": "user", "content": content}]
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    start = time.time()
    msg = client.messages.create(**kwargs)
    elapsed = time.time() - start

    text = msg.content[0].text if msg.content else ""
    img_count = len([c for c in content if c.get("type") == "image"])
    print(f"[Claude/{model.split('-')[1]}] vision ({img_count} imgs), "
          f"{len(text)} chars, {elapsed:.1f}s")
    return text


def call_vision_json(prompt: str, image_paths: list, system: str = "",
                     model: str = None, max_tokens: int = 2048) -> dict:
    """Claude vision call expecting JSON response."""
    raw = call_vision(prompt, image_paths, system, model, max_tokens)
    return _parse_json_response(raw)


# ---------------------------------------------------------------------------
# JSON Parsing
# ---------------------------------------------------------------------------

def _parse_json_response(raw: str) -> dict:
    """Parse JSON from Claude response, handling markdown fences."""
    text = raw.strip()

    # Strip markdown JSON fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines if they're fences
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        print(f"[Claude] Failed to parse JSON: {text[:200]}...")
        return {"_raw": raw, "_parse_error": True}


# ---------------------------------------------------------------------------
# Convenience: Transition Judge Call
# ---------------------------------------------------------------------------

def judge_transition_vision(prompt: str, image_paths: list,
                            shot_priority: str = "standard",
                            attempt_count: int = 0,
                            auto_escalate: bool = True) -> dict:
    """Run transition judge on Opus. auto_escalate kept for backwards-compat;
    when True, enables extended thinking for hero/borderline shots.
    """
    enable_thinking = auto_escalate and should_escalate(
        shot_priority, 0.55, None, attempt_count
    )
    if enable_thinking:
        result = call_opus_vision_json(
            prompt, image_paths, thinking_budget=8000, max_tokens=16000
        )
        result["_model_used"] = "opus_thinking"
    else:
        result = call_vision_json(prompt, image_paths, model=OPUS_MODEL)
        result["_model_used"] = "opus"
    return result


# ---------------------------------------------------------------------------
# Convenience: Post-Render Critic Call
# ---------------------------------------------------------------------------

def critique_render_vision(prompt: str, image_paths: list,
                           shot_priority: str = "standard",
                           attempt_count: int = 0) -> dict:
    """Run post-render critic on Opus (with thinking on hero shots)."""
    return judge_transition_vision(prompt, image_paths,
                                   shot_priority, attempt_count)


# ---------------------------------------------------------------------------
# Opus Wrapper (extended thinking + prompt caching)
# ---------------------------------------------------------------------------

def _run_opus(client, kwargs: dict) -> tuple[str, int, int]:
    """Run a messages.create call, streaming when max_tokens exceeds the
    SDK's non-streaming ceiling (currently ~8192 for Opus).

    Returns (text, cache_read_tokens, cache_creation_tokens).
    """
    max_tokens = int(kwargs.get("max_tokens", 0))
    use_stream = max_tokens > 8000

    if not use_stream:
        msg = client.messages.create(**kwargs)
        text = ""
        for block in msg.content or []:
            if getattr(block, "type", None) == "text":
                text += block.text
        usage = getattr(msg, "usage", None)
        return (
            text,
            getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
            getattr(usage, "cache_creation_input_tokens", 0) if usage else 0,
        )

    # Streaming path for long responses.
    text_parts: list[str] = []
    cache_read = 0
    cache_write = 0
    with client.messages.stream(**kwargs) as stream:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta and getattr(delta, "type", "") == "text_delta":
                    text_parts.append(delta.text)
        final = stream.get_final_message()
        usage = getattr(final, "usage", None)
        if usage:
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if not text_parts and getattr(final, "content", None):
            for block in final.content:
                if getattr(block, "type", None) == "text":
                    text_parts.append(block.text)
    return ("".join(text_parts), cache_read, cache_write)


def _budget_to_effort(budget: int) -> str:
    """Map legacy budget_tokens numbers to Opus 4.7 adaptive effort tiers.

    NOTE: "max" reserves heavy internal thinking budget; only use when caller
    has set max_tokens well above the thinking budget (≥ 16000). Otherwise
    the model can burn the full max_tokens on thinking and emit 0 output.
    """
    if budget <= 1500:
        return "low"
    if budget <= 4500:
        return "medium"
    return "high"


def _system_blocks(system: str = "", cached_system: str = "") -> list:
    """Build a system list for prompt caching.

    cached_system gets a cache_control marker — every subsequent call that
    passes the SAME cached_system text reuses the server-side prefix (cheap).
    """
    blocks = []
    if cached_system:
        blocks.append({
            "type": "text",
            "text": cached_system,
            "cache_control": {"type": "ephemeral"},
        })
    if system:
        blocks.append({"type": "text", "text": system})
    return blocks


def _resolve_cached_system(cached_system: str,
                           project: str = None,
                           profile_id: str = None,
                           attach_bible: bool = True) -> str:
    """Merge caller-provided cached_system with the Opus director bible.

    Priority: if `cached_system` is supplied explicitly, it is used as-is.
    Otherwise, when `project` or `profile_id` is supplied (and attach_bible is
    True), hydrate the bible from lib.opus_bible. Soft-fails to empty string.
    """
    if cached_system:
        return cached_system
    if not attach_bible:
        return ""
    if not (project or profile_id):
        return ""
    try:
        from lib.opus_bible import build_bible
        _sys, cached = build_bible(profile_id=profile_id, project_slug=project)
        return cached or ""
    except Exception as e:
        print(f"[Claude/opus] bible load failed ({e}); continuing without")
        return ""


def call_opus(prompt: str,
              system: str = "",
              cached_system: str = "",
              max_tokens: int = 4096,
              thinking_budget: int = 0,
              project: str = None,
              profile_id: str = None,
              attach_bible: bool = True) -> str:
    """Text-only Opus call with optional extended thinking + prompt caching.

    - cached_system: system text to cache (e.g. character bible + style profile).
      Subsequent calls with identical text reuse the cached prefix.
    - thinking_budget: >0 enables extended thinking; must be < max_tokens.

    Returns raw text.
    """
    client = _get_client()
    cached_system = _resolve_cached_system(cached_system, project, profile_id, attach_bible)

    messages = [{"role": "user", "content": prompt}]
    kwargs: dict = {
        "model": OPUS_MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }

    sys_blocks = _system_blocks(system, cached_system)
    if sys_blocks:
        kwargs["system"] = sys_blocks

    if thinking_budget > 0:
        if thinking_budget >= max_tokens:
            max_tokens = thinking_budget + 2048
            kwargs["max_tokens"] = max_tokens
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": _budget_to_effort(thinking_budget)}

    start = time.time()
    text, cached_hits, cached_writes = _run_opus(client, kwargs)
    elapsed = time.time() - start

    thinking_tag = f"+thinking({_budget_to_effort(thinking_budget)})" if thinking_budget > 0 else ""
    bible_tag = f" bible={len(cached_system)//1000}kb" if cached_system else ""
    print(f"[Claude/opus{thinking_tag}]{bible_tag} {len(text)} chars, "
          f"{elapsed:.1f}s, cache_read={cached_hits} cache_write={cached_writes}")
    return text


def call_opus_json(prompt: str,
                   system: str = "",
                   cached_system: str = "",
                   max_tokens: int = 4096,
                   thinking_budget: int = 0,
                   project: str = None,
                   profile_id: str = None,
                   attach_bible: bool = True) -> dict:
    """Opus call expecting JSON response."""
    raw = call_opus(prompt, system, cached_system, max_tokens, thinking_budget,
                    project=project, profile_id=profile_id, attach_bible=attach_bible)
    return _parse_json_response(raw)


def call_opus_vision(prompt: str,
                     image_paths: list,
                     system: str = "",
                     cached_system: str = "",
                     max_tokens: int = 4096,
                     thinking_budget: int = 0,
                     project: str = None,
                     profile_id: str = None,
                     attach_bible: bool = True) -> str:
    """Opus vision call with extended thinking + prompt caching."""
    client = _get_client()
    cached_system = _resolve_cached_system(cached_system, project, profile_id, attach_bible)

    content = []
    for path in image_paths:
        if os.path.isfile(path):
            content.append(_image_content_block(path))
    content.append({"type": "text", "text": prompt})

    messages = [{"role": "user", "content": content}]
    kwargs: dict = {
        "model": OPUS_MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }

    sys_blocks = _system_blocks(system, cached_system)
    if sys_blocks:
        kwargs["system"] = sys_blocks

    if thinking_budget > 0:
        if thinking_budget >= max_tokens:
            max_tokens = thinking_budget + 2048
            kwargs["max_tokens"] = max_tokens
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": _budget_to_effort(thinking_budget)}

    start = time.time()
    text, cached_hits, cached_writes = _run_opus(client, kwargs)
    elapsed = time.time() - start

    img_count = len([c for c in content if c.get("type") == "image"])
    thinking_tag = f"+thinking({_budget_to_effort(thinking_budget)})" if thinking_budget > 0 else ""
    bible_tag = f" bible={len(cached_system)//1000}kb" if cached_system else ""
    print(f"[Claude/opus{thinking_tag}]{bible_tag} vision ({img_count} imgs), "
          f"{len(text)} chars, {elapsed:.1f}s, cache_read={cached_hits} cache_write={cached_writes}")
    return text


def call_opus_vision_json(prompt: str,
                          image_paths: list,
                          system: str = "",
                          cached_system: str = "",
                          max_tokens: int = 4096,
                          thinking_budget: int = 0,
                          project: str = None,
                          profile_id: str = None,
                          attach_bible: bool = True) -> dict:
    """Opus vision call expecting JSON response."""
    raw = call_opus_vision(prompt, image_paths, system, cached_system,
                           max_tokens, thinking_budget,
                           project=project, profile_id=profile_id,
                           attach_bible=attach_bible)
    return _parse_json_response(raw)
