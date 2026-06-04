"""Filesystem primitives: read a file, list a directory, glob.

These exist so we have a single point to enforce path-safety (never
escape the workspace) and budget-awareness (cap how many bytes we ever
push into a prompt from one file).
"""
from __future__ import annotations

from pathlib import Path


def read_file_slice(path: Path, *, max_chars: int = 6000,
                    start_line: int | None = None,
                    end_line: int | None = None) -> str:
    """Read a file or a line range. Truncates to max_chars."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if start_line is not None or end_line is not None:
        lines = text.splitlines()
        s = max(0, (start_line or 1) - 1)
        e = min(len(lines), end_line or len(lines))
        text = "\n".join(lines[s:e])
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated)\n"
    return text


def list_dir(path: Path, *, max_entries: int = 200) -> list[str]:
    """List a directory's immediate contents. Returns posix-style names."""
    entries: list[str] = []
    for p in sorted(path.iterdir()):
        suffix = "/" if p.is_dir() else ""
        entries.append(f"{p.name}{suffix}")
        if len(entries) >= max_entries:
            break
    return entries


def safe_join(workspace: Path, rel: str) -> Path:
    """Resolve `rel` relative to `workspace` and refuse paths that escape it."""
    candidate = (workspace / rel).resolve()
    workspace_resolved = workspace.resolve()
    try:
        candidate.relative_to(workspace_resolved)
    except ValueError:
        raise PermissionError(f"path '{rel}' escapes workspace {workspace}")
    return candidate
