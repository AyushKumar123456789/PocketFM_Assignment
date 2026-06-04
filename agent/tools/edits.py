"""Apply structured edit operations to the workspace.

The applier enforces these invariants:
- `find` (for `replace`) and `anchor` (for `insert_*`) must match
  exactly ONCE in the target file. Zero matches and multiple matches both
  raise — this catches the LLM hallucinating non-existent code AND
  catches ambiguous edits that would land in the wrong place.
- `create_file` refuses to overwrite an existing file.
- Paths are workspace-relative and validated via `safe_join`.

`revert_all` restores the workspace to HEAD before each retry, so each
attempt is independent.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from agent.stages.edit import EditOp, EditSet
from agent.tools.fs import safe_join


class EditError(Exception):
    """Raised when a structured edit cannot be applied unambiguously."""


def sanity_check_edits(edits: EditSet) -> None:
    """Reject obviously-broken LLM output before we try to apply it.

    Catches known Gemini-Flash failure modes:
      - long runs of identical characters (the model loops on a token)
      - empty `find`/`anchor` (the model gave up)
      - suspiciously short `find`/`anchor` that won't be unique
      - empty ops list (the model decided not to act — usually wrong here)
    """
    import re

    if not edits.ops:
        raise EditError("editor returned zero ops — nothing to apply")

    for i, op in enumerate(edits.ops):
        target = op.find if op.op == "replace" else op.anchor
        if op.op == "create_file":
            target = op.content
        if not target:
            raise EditError(f"op {i} ({op.op}): empty find/anchor/content")
        # Run of >= 30 identical non-space chars, OR >= 80 identical whitespace
        # chars, both indicate the LLM degenerated.
        if re.search(r"([^\s])\1{29,}", target):
            raise EditError(
                f"op {i} ({op.op}) on {op.file}: degenerate output — 30+ "
                f"consecutive identical characters in find/anchor"
            )
        if re.search(r"(\s)\1{79,}", target):
            raise EditError(
                f"op {i} ({op.op}) on {op.file}: degenerate output — 80+ "
                f"consecutive whitespace chars in find/anchor"
            )
        # Must have at least one non-whitespace char to be a real anchor.
        if op.op in ("replace", "insert_after", "insert_before") and not target.strip():
            raise EditError(
                f"op {i} ({op.op}) on {op.file}: find/anchor is all whitespace"
            )
        if op.op in ("replace", "insert_after", "insert_before") and len(target.strip()) < 8:
            raise EditError(
                f"op {i} ({op.op}) on {op.file}: find/anchor too short "
                f"(needs more context to be unique): {target!r}"
            )


def apply_structured_edits(workspace: Path, edits: EditSet) -> None:
    """Apply each op in order. Raises EditError on the first ambiguous op."""
    for op in edits.ops:
        target = safe_join(workspace, op.file)
        if op.op == "create_file":
            if target.exists():
                raise EditError(f"create_file: {op.file} already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(op.content, encoding="utf-8")
            continue

        if not target.exists():
            raise EditError(f"{op.op}: file does not exist: {op.file}")

        text = target.read_text(encoding="utf-8")

        if op.op == "replace":
            count = text.count(op.find)
            if count == 0:
                raise EditError(f"replace: 'find' not found in {op.file}: {op.find[:80]!r}")
            if count > 1:
                raise EditError(
                    f"replace: 'find' matches {count} times in {op.file}; "
                    f"ambiguous. Use more surrounding context. find={op.find[:80]!r}"
                )
            text = text.replace(op.find, op.replace, 1)

        elif op.op in ("insert_after", "insert_before"):
            count = text.count(op.anchor)
            if count == 0:
                raise EditError(f"{op.op}: 'anchor' not found in {op.file}: {op.anchor[:80]!r}")
            if count > 1:
                raise EditError(
                    f"{op.op}: 'anchor' matches {count} times in {op.file}; ambiguous."
                )
            idx = text.find(op.anchor)
            if op.op == "insert_after":
                idx += len(op.anchor)
            text = text[:idx] + op.content + text[idx:]

        else:
            raise EditError(f"unknown op: {op.op}")

        target.write_text(text, encoding="utf-8")


def revert_all(workspace: Path) -> None:
    """Restore the workspace to its HEAD commit. Used between retry attempts.

    Equivalent to:  git reset --hard HEAD && git clean -fd
    Note: this WILL discard any local edits in the workspace. Workspaces
    live under `workspaces/` and are not meant to hold user work.
    """
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=workspace, check=True,
                   capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=workspace, check=True,
                   capture_output=True)
