"""
Prompt Packs — reusable prompt templates for all AI model calls.

Each module exports a `render(**kwargs)` function that interpolates
shot metadata into the prompt template and returns the final string.

Naming convention:
  {model}_{task}.py

Models: gemini, kling, haiku, sonnet
Tasks: start_frame, end_frame, end_variants, bridge_frame, repair_frame,
       direct_motion, bridge_motion, motivated_cut, transition_judge,
       post_render_critic, optimizer, transition_escalation,
       post_render_escalation, optimizer_escalation
"""

import importlib
import os

_PACK_DIR = os.path.dirname(os.path.abspath(__file__))


def render(pack_name: str, **kwargs) -> str:
    """Render a prompt pack by name with variable interpolation.

    Example: render("gemini_start_frame", subject="golden retriever", ...)
    """
    mod = importlib.import_module(f"lib.prompt_packs.{pack_name}")
    return mod.render(**kwargs)


def list_packs() -> list:
    """List all available prompt packs."""
    packs = []
    for f in os.listdir(_PACK_DIR):
        if f.endswith(".py") and f != "__init__.py":
            packs.append(f[:-3])
    return sorted(packs)
