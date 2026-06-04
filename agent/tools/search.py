"""Ripgrep wrapper.

Why ripgrep? Fast, gitignore-aware, returns line numbers. We *don't* want
to write our own walker for content search — `rg` is the right tool.

If `rg` is not on PATH, we fall back to a Python implementation that
walks the tree and runs `re.search` per file. Slower but works.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from shutil import which


@dataclass
class Hit:
    path: str   # relative to workspace, posix
    line: int
    text: str


_RG_AVAILABLE: bool | None = None


def _rg_available() -> bool:
    global _RG_AVAILABLE
    if _RG_AVAILABLE is None:
        _RG_AVAILABLE = which("rg") is not None
    return _RG_AVAILABLE


def ripgrep(
    workspace: Path,
    *,
    pattern: str,
    glob: str | None = None,
    case_insensitive: bool = False,
    max_hits: int = 200,
) -> list[Hit]:
    if _rg_available():
        return _rg_search(workspace, pattern, glob, case_insensitive, max_hits)
    return _python_search(workspace, pattern, glob, case_insensitive, max_hits)


def _rg_search(workspace: Path, pattern: str, glob: str | None,
                case_insensitive: bool, max_hits: int) -> list[Hit]:
    cmd = ["rg", "--no-heading", "-n", "--color=never", pattern]
    if case_insensitive:
        cmd.insert(1, "-i")
    if glob:
        cmd.extend(["-g", glob])
    cmd.extend([str(workspace)])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return []
    hits: list[Hit] = []
    for line in out.stdout.splitlines():
        # Format: <path>:<line>:<text>
        try:
            path_part, line_part, text_part = line.split(":", 2)
        except ValueError:
            continue
        try:
            rel = Path(path_part).resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            rel = path_part
        try:
            ln = int(line_part)
        except ValueError:
            continue
        hits.append(Hit(path=rel, line=ln, text=text_part.strip()[:200]))
        if len(hits) >= max_hits:
            break
    return hits


def _python_search(workspace: Path, pattern: str, glob: str | None,
                    case_insensitive: bool, max_hits: int) -> list[Hit]:
    flags = re.IGNORECASE if case_insensitive else 0
    regex = re.compile(pattern, flags)
    hits: list[Hit] = []
    iterable = workspace.rglob(glob) if glob else workspace.rglob("*")
    for path in iterable:
        if not path.is_file():
            continue
        if any(part in {"vendor", ".git", "node_modules"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append(Hit(
                    path=path.relative_to(workspace).as_posix(),
                    line=i,
                    text=line.strip()[:200],
                ))
                if len(hits) >= max_hits:
                    return hits
    return hits
