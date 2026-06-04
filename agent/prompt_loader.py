"""Load versioned prompts from `prompts/<name>.md`.

Prompts use `{placeholder}` substitution via `str.format`. Callers pass
the values they want substituted. We do NOT do any other templating —
plain Python format strings keep the prompts grep-able and diffable.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """Return the raw text of `prompts/<name>.md`. Cached after first read."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8")
