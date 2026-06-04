"""Edit stage: an LLM call that produces structured edits.

We give the LLM a small tool surface (read_file, list_dir, ripgrep) and a
hard requirement to return its final answer as a JSON list of operations.

Operation schema (one of):

    {"op":"replace","file":"foo.go","find":"<exact text>","replace":"<new text>"}
    {"op":"insert_after","file":"foo.go","anchor":"<exact text>","content":"..."}
    {"op":"insert_before","file":"foo.go","anchor":"<exact text>","content":"..."}
    {"op":"create_file","file":"newfile.go","content":"..."}

`find` and `anchor` must match exactly once in the target file. The
applier (`agent/tools/edits.py`) validates uniqueness before applying.

On retry, we pass `prior_error` containing the build/vet/test output. The
prompt tells the LLM to use that to refine its edits.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config import RepoConfig
from agent.llm import LLM
from agent.prompt_loader import load_prompt
from agent.stages.fetch_issue import Issue
from agent.stages.index_repo import RepoMap
from agent.stages.plan import Plan
from agent.tools.fs import read_file_slice


@dataclass
class EditOp:
    op: str                # "replace" | "insert_after" | "insert_before" | "create_file"
    file: str
    find: str = ""
    anchor: str = ""
    replace: str = ""
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v != ""}


@dataclass
class EditSet:
    ops: list[EditOp] = field(default_factory=list)
    rationale: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rationale": self.rationale,
            "ops": [op.to_dict() for op in self.ops],
        }


def generate_edits(
    llm: LLM,
    *,
    issue: Issue,
    plan: Plan,
    repo_map: RepoMap,
    workspace: Path,
    repo_config: RepoConfig,
    prior_error: str | None = None,
) -> EditSet:
    system = load_prompt("edit").format(
        repo_slug=repo_config.slug,
        repo_map=repo_map.short_summary(max_chars=4000),
    )
    file_context = _gather_file_context(workspace, plan)
    user = _build_user_message(issue, plan, file_context, prior_error)
    resp = llm.complete(
        stage="edit",
        system=system,
        messages=[{"role": "user", "content": user}],
        # Edit can emit long `find`/`replace`/`content` strings, but typical
        # responses are 1-3k tokens. Groq's free tier counts max_tokens against
        # the per-minute budget so we keep this tight; if a real run exceeds
        # this we'll see "truncated JSON" in the trace and can bump it.
        max_tokens=6000,
        temperature=0.0,
    )
    return _parse_edits(resp.text)


def _gather_file_context(workspace: Path, plan: Plan, *, per_file_limit: int = 40_000) -> str:
    """Read the files the planner flagged. Token-budget-aware.

    Default 40k chars/file (~10k tokens). Tuned so a typical 1-2-file
    prompt fits in Claude Sonnet's 30k-input-tokens-per-minute account
    limit with headroom for the system prompt + plan + retries. Most Go
    source files (e.g. cobra's bash_completions.go ~15KB) fit fully.
    Very large files like cobra's command.go (~60KB) still get
    truncated; future improvement is symbol-aware chunking.
    """
    chunks = []
    seen = set()
    for path in plan.files_to_edit + plan.files_to_read_first:
        if path in seen:
            continue
        seen.add(path)
        abs_p = workspace / path
        if not abs_p.exists():
            chunks.append(f"\n## {path}\n(file not found — Planner may be mistaken)\n")
            continue
        text = read_file_slice(abs_p, max_chars=per_file_limit)
        chunks.append(f"\n## {path}\n```go\n{text}\n```")
    return "\n".join(chunks)


def _build_user_message(issue: Issue, plan: Plan, file_context: str,
                         prior_error: str | None) -> str:
    lines = [
        f"# Issue #{issue.number}: {issue.title}",
        "",
        "## Plan",
        json.dumps(plan.to_dict(), indent=2),
        "",
        "## File context (the files the plan named)",
        file_context,
    ]
    if prior_error:
        lines += [
            "",
            "## Validation failed on the previous attempt. Output:",
            "```",
            prior_error[:6000],
            "```",
            "Adjust your edits so this validation passes. Do NOT repeat the same edits.",
        ]
    lines += [
        "",
        "Produce a JSON object: {\"rationale\": \"...\", \"ops\": [ ... ]}. "
        "Each op follows the schema in the system prompt. Output ONLY the JSON object.",
    ]
    return "\n".join(lines)


def _parse_edits(text: str) -> EditSet:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()

    # Fast path: clean JSON.
    try:
        obj = json.loads(s)
        return _coerce_obj(obj, text)
    except json.JSONDecodeError:
        pass

    # Prefer a fenced ```json ... ``` block if one is embedded in prose.
    import re
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", s, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            return _coerce_obj(obj, text)
        except json.JSONDecodeError:
            pass

    # Slow path: walk every `{` and try a brace-balanced, string-aware scan.
    # This is robust to prose containing bash `${var}` patterns or extra text
    # around the JSON. We pick the first candidate that parses successfully.
    last_err: Exception | None = None
    for start in (i for i, c in enumerate(s) if c == "{"):
        # Skip `${...}` shell-style braces in surrounding prose.
        if start > 0 and s[start - 1] == "$":
            continue
        end = _find_matching_brace(s, start)
        if end is None:
            continue
        try:
            obj = json.loads(s[start:end + 1])
            return _coerce_obj(obj, text)
        except json.JSONDecodeError as e:
            last_err = e
            continue

    if last_err is None:
        raise ValueError(f"Editor did not return JSON. Raw response (full):\n{text}")
    raise ValueError(f"Editor JSON malformed ({last_err}). Full raw response:\n{text}")


def _find_matching_brace(s: str, start: int) -> int | None:
    """Return the index of the `}` that closes the `{` at `start`, or None.

    Respects JSON string literals (including backslash escapes) so braces
    inside strings don't move the depth counter.
    """
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
    return None


def _coerce_obj(obj: Any, raw_text: str) -> EditSet:
    if not isinstance(obj, dict):
        raise ValueError(f"Editor returned JSON but not an object. Raw response:\n{raw_text}")
    ops = [EditOp(**o) for o in obj.get("ops", [])]
    return EditSet(ops=ops, rationale=obj.get("rationale", ""), raw_text=raw_text)
