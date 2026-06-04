"""Go symbol lookups.

For v1 we don't ship a Go AST parser. Symbol-level queries are answered
by the RepoMap (built in `agent.stages.index_repo`) plus ripgrep. This
keeps the dependency list small.

If we later want true AST queries (e.g. "find all callers of X"), the
right move is `tree-sitter-go` via the `tree-sitter` Python binding,
which is pure-Python and ships pre-built wheels. The signatures below
are stable so adding the impl is non-breaking.
"""
from __future__ import annotations

import re
from pathlib import Path

from agent.tools.search import Hit, ripgrep


def find_definition(workspace: Path, symbol: str) -> list[Hit]:
    """Return likely-definition sites for `symbol`. Regex-based — not a true AST."""
    pattern = rf"^(?:func\s+(?:\([^)]+\)\s+)?|type\s+|var\s+|const\s+){re.escape(symbol)}\b"
    return ripgrep(workspace, pattern=pattern, glob="*.go", max_hits=20)


def find_references(workspace: Path, symbol: str) -> list[Hit]:
    """Return all call-sites / references to `symbol`."""
    return ripgrep(workspace, pattern=rf"\b{re.escape(symbol)}\b", glob="*.go", max_hits=200)
