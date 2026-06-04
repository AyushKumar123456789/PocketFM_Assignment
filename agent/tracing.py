"""Append-only JSONL tracing for every LLM call and every tool call.

Each run gets its own `trace.jsonl`. Lines are JSON objects with at minimum:

    {"ts": "2026-06-04T12:00:00Z", "kind": "...", "data": {...}}

The trace is the single source of truth for "what did the agent do?" and
is what reviewers will read to understand the framework.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Tracer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate on open — one trace file per run.
        self.path.write_text("", encoding="utf-8")

    def event(self, kind: str, data: dict[str, Any]) -> None:
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "kind": kind,
            "data": data,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")

    # Convenience methods so callers don't sprinkle string literals everywhere.
    def llm_call(self, *, stage: str, model: str, input_tokens: int,
                 output_tokens: int, cached_tokens: int = 0,
                 cost_usd: float = 0.0, prompt: str = "", response: str = "") -> None:
        self.event("llm.call", {
            "stage": stage, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cached_tokens": cached_tokens, "cost_usd": round(cost_usd, 6),
            # Truncate raw text in the trace; full transcripts live in plan.json
            # and pr.md if the caller wants to dump them.
            "prompt_preview": prompt[:400],
            "response_preview": response[:400],
        })

    def tool_call(self, *, name: str, args: dict[str, Any], result_preview: str) -> None:
        self.event("tool.call", {
            "name": name, "args": args, "result_preview": result_preview[:400],
        })
