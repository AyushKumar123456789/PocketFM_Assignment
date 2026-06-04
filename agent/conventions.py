"""Extract project conventions to inject into LLM prompts.

We read:
- Files listed in `RepoConfig.convention_paths` (default: CONTRIBUTING.md).
- `.golangci.yml` if present — surfaces enabled linters.
- The last ~30 commit subjects from `git log` — infers commit-message style.

Output is a single string, capped at ~3000 chars. We do this once per
run and reuse the result via the cached system prompt.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from agent.config import RepoConfig
from agent.tools.shell import run_command


def load_conventions(workspace: Path, cfg: RepoConfig) -> str:
    parts: list[str] = []

    for rel in cfg.convention_paths:
        p = workspace / rel
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            # Trim huge contributing docs — first 1500 chars usually has the
            # PR-formatting and code-style sections.
            parts.append(f"## {rel}\n{text[:1500]}")

    golangci = workspace / ".golangci.yml"
    if not golangci.exists():
        golangci = workspace / ".golangci.yaml"
    if golangci.exists():
        try:
            obj = yaml.safe_load(golangci.read_text(encoding="utf-8", errors="replace"))
            linters = ((obj or {}).get("linters") or {}).get("enable") or []
            if linters:
                parts.append("## Enabled linters (.golangci.yml)\n" + ", ".join(linters))
        except yaml.YAMLError:
            pass

    # Recent commit subjects → style hint.
    r = run_command(["git", "log", "--pretty=%s", "-30"], cwd=workspace, timeout_s=15)
    if r.returncode == 0 and r.stdout.strip():
        parts.append("## Recent commit subjects (style reference)\n" + r.stdout.strip())

    out = "\n\n".join(parts) if parts else "(no conventions files found)"
    return out[:3000]
