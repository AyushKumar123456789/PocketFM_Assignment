"""Candidate-file retrieval.

Three signals combined:
1. **Lexical**: ripgrep for issue keywords + identifier-like tokens.
2. **Symbol**: any Symbol from the RepoMap whose name appears in the issue.
3. **Neighbor expansion**: for each hit, pull files that import or are
   imported by the hit file (limited fan-out).

Each candidate gets a score; we return the top N. The Planner LLM gets
the candidates and is free to prune further before the Editor sees them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.stages.fetch_issue import Issue
from agent.stages.index_repo import RepoMap
from agent.tools.search import ripgrep


# Generic stopwords that show up in every Go issue and aren't useful.
_STOP = set("""
a an the and or but if when then is was were be been being have has had
do does did so this that these those for to of in on at by with as it its
i you we they me my our their issue bug error problem fix please thanks
go golang func type var const package main
""".split())

_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
_CAMEL_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]{2,})\b")


@dataclass
class Candidate:
    path: str         # relative to workspace
    score: float
    reasons: list[str] = field(default_factory=list)


def _extract_keywords(text: str) -> tuple[set[str], set[str]]:
    """Return (general_tokens, camel_identifiers)."""
    tokens = {t.lower() for t in _IDENT_RE.findall(text)}
    tokens = {t for t in tokens if t not in _STOP and len(t) > 2}
    camel = set(_CAMEL_RE.findall(text))  # likely Go identifiers
    return tokens, camel


def retrieve_candidates(
    workspace: Path,
    repo_map: RepoMap,
    issue: Issue,
    *,
    top_n: int = 12,
) -> list[Candidate]:
    text = issue.text_blob
    tokens, camel = _extract_keywords(text)

    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}

    # 1) Lexical: ripgrep each camelCase identifier and each token.
    #    CamelCase hits weight higher because they're more likely to be Go symbols.
    for ident in camel:
        hits = ripgrep(workspace, pattern=rf"\b{re.escape(ident)}\b", max_hits=80)
        for h in hits:
            scores[h.path] = scores.get(h.path, 0) + 3.0
            reasons.setdefault(h.path, []).append(f"camel:{ident}@{h.line}")

    # Lowercase tokens — only the longest / most distinctive ones, to avoid
    # blowing the budget on common words.
    distinctive_tokens = sorted([t for t in tokens if len(t) >= 5], key=len, reverse=True)[:20]
    for tok in distinctive_tokens:
        hits = ripgrep(workspace, pattern=rf"\b{re.escape(tok)}\b", max_hits=40,
                       case_insensitive=True)
        for h in hits:
            scores[h.path] = scores.get(h.path, 0) + 0.5
            reasons.setdefault(h.path, []).append(f"tok:{tok}")

    # 2) Symbol map: any RepoMap symbol that matches a camel token.
    for pkg in repo_map.packages:
        for s in pkg.symbols:
            if s.name in camel:
                scores[s.file] = scores.get(s.file, 0) + 5.0
                reasons.setdefault(s.file, []).append(f"sym:{s.kind} {s.name}")

    # De-prioritise vendored / test data / generated.
    for p in list(scores.keys()):
        if "vendor/" in p or "/testdata/" in p or "_gen.go" in p:
            scores[p] *= 0.1

    # Tie-break preference: shorter paths win (top-level files often more relevant).
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], len(kv[0])))

    out: list[Candidate] = []
    for path, score in ranked[:top_n]:
        out.append(Candidate(path=path, score=round(score, 2),
                             reasons=sorted(set(reasons[path]))[:8]))
    return out
