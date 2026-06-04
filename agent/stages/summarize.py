"""PR summarizer: takes the issue, plan, edits, and validation result and
produces a PR title + body.

Output respects conventional commit-ish style (kept lowercase if the
target repo's recent history is lowercase). The body sections are:
  - Summary: 1-3 bullets, what changed and why
  - Closes: #<issue_number>
  - Validation: gofmt/vet/build/test all green (or what failed and why)
  - Notes: optional risks/followups copied from the plan
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.config import RepoConfig
from agent.llm import LLM
from agent.prompt_loader import load_prompt
from agent.stages.edit import EditSet
from agent.stages.fetch_issue import Issue
from agent.stages.plan import Plan
from agent.stages.validate import ValidationResult


@dataclass
class PullRequest:
    title: str
    body: str


def write_pr(
    llm: LLM,
    *,
    issue: Issue,
    plan: Plan,
    edits: EditSet | None,
    validation: ValidationResult | None,
    repo_config: RepoConfig,
) -> PullRequest:
    system = load_prompt("summarize").format(repo_slug=repo_config.slug)

    val_block = "(validation not run)"
    if validation:
        val_block = (
            f"gofmt={'ok' if validation.fmt_ok else 'fail'} "
            f"vet={'ok' if validation.vet_ok else 'fail'} "
            f"build={'ok' if validation.build_ok else 'fail'} "
            f"test={'ok' if validation.test_ok else 'fail'}"
        )

    files = sorted({op.file for op in (edits.ops if edits else [])}) or ["(no files)"]
    user = (
        f"# Issue #{issue.number}: {issue.title}\n"
        f"URL: {issue.url}\n\n"
        f"## Plan hypothesis\n{plan.hypothesis}\n\n"
        f"## Approach\n{plan.approach}\n\n"
        f"## Files changed\n" + "\n".join(f"- {f}" for f in files) + "\n\n"
        f"## Validation\n{val_block}\n\n"
        f"## Rationale (from editor)\n{(edits.rationale if edits else '(none)')}\n\n"
        "Produce JSON: {\"title\": \"...\", \"body\": \"...\"}. "
        "Title under 70 chars. Body uses sections: Summary, Closes, Validation, Notes. "
        "Output ONLY the JSON object."
    )
    resp = llm.complete(
        stage="summarize",
        system=system,
        messages=[{"role": "user", "content": user}],
        max_tokens=1024,
        temperature=0.0,
    )
    return _parse_pr(resp.text, default_close=issue.number)


def _parse_pr(text: str, default_close: int) -> PullRequest:
    import json
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
        l, r = s.find("{"), s.rfind("}")
        obj = json.loads(s[l:r + 1])
    title = obj.get("title", "").strip()
    body = obj.get("body", "").strip()
    if f"#{default_close}" not in body and f"Closes #{default_close}" not in body:
        body += f"\n\nCloses #{default_close}"
    return PullRequest(title=title, body=body)
