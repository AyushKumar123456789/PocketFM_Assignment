"""Plan stage: an LLM call that produces a structured plan.

Output shape (JSON, validated):

    {
      "hypothesis": "...",                    # one paragraph: root cause
      "files_to_edit": ["pkg/foo.go", ...],
      "files_to_read_first": ["..."],         # additional context the editor should load
      "test_packages": ["./pkg/foo/..."],     # what to run for validation
      "approach": "...",                      # bullet-y outline of the change
      "risks": ["..."]                        # what could go wrong / breakage to watch for
    }

We keep this stage *cheap*: a single completion, no tools. Tool-using
reasoning happens in the Edit stage where it matters most.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config import RepoConfig
from agent.conventions import load_conventions
from agent.llm import LLM
from agent.stages.fetch_issue import Issue
from agent.stages.index_repo import RepoMap
from agent.stages.retrieve import Candidate
from agent.prompt_loader import load_prompt


@dataclass
class Plan:
    hypothesis: str
    files_to_edit: list[str]
    files_to_read_first: list[str] = field(default_factory=list)
    test_packages: list[str] = field(default_factory=list)
    approach: str = ""
    risks: list[str] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "files_to_edit": self.files_to_edit,
            "files_to_read_first": self.files_to_read_first,
            "test_packages": self.test_packages,
            "approach": self.approach,
            "risks": self.risks,
        }


def generate_plan(
    llm: LLM,
    *,
    issue: Issue,
    repo_map: RepoMap,
    candidates: list[Candidate],
    repo_config: RepoConfig,
    workspace: Path,
) -> Plan:
    conventions = load_conventions(workspace, repo_config)
    system = load_prompt("plan").format(
        repo_slug=repo_config.slug,
        repo_map=repo_map.short_summary(),
        conventions=conventions,
        project_notes=repo_config.project_notes or "(none)",
    )
    user = _build_user_message(issue, candidates)
    resp = llm.complete(
        stage="plan",
        system=system,
        messages=[{"role": "user", "content": user}],
        max_tokens=2048,
        temperature=0.0,
    )
    return _parse_plan(resp.text)


def _build_user_message(issue: Issue, candidates: list[Candidate]) -> str:
    lines = [
        f"# Issue #{issue.number}: {issue.title}",
        f"Labels: {', '.join(issue.labels) or '(none)'}",
        f"URL: {issue.url}",
        "",
        "## Body",
        issue.body or "(empty)",
    ]
    if issue.comments:
        lines.append("\n## Comments")
        for c in issue.comments[:8]:
            user = c.get("user", {}).get("login", "?")
            lines.append(f"\n### @{user}")
            lines.append(c.get("body", ""))
    lines.append("\n## Retrieved candidate files (top-N by lex+symbol score)")
    for c in candidates:
        lines.append(f"  - {c.path}  (score={c.score})  reasons: {', '.join(c.reasons)}")
    lines.append(
        "\nProduce a JSON object matching the schema described in the system prompt. "
        "Output ONLY the JSON object — no prose, no fences."
    )
    return "\n".join(lines)


def _parse_plan(text: str) -> Plan:
    """Best-effort JSON extraction. Tolerant of ```json fences."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        # Last-resort: take everything between the first { and last }.
        l, r = s.find("{"), s.rfind("}")
        if l == -1 or r == -1:
            raise ValueError(
                f"Planner did not return JSON. Raw response (full):\n{text}"
            )
        try:
            obj = json.loads(s[l:r + 1])
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Planner JSON malformed ({e}). Full raw response:\n{text}"
            ) from e
    return Plan(
        hypothesis=obj.get("hypothesis", ""),
        files_to_edit=obj.get("files_to_edit", []),
        files_to_read_first=obj.get("files_to_read_first", []),
        test_packages=obj.get("test_packages", []),
        approach=obj.get("approach", ""),
        risks=obj.get("risks", []),
        raw_text=text,
    )
