"""Scoring functions for the eval harness."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests


GITHUB_API = "https://api.github.com"


@dataclass
class FixtureScore:
    repo: str
    issue: int
    pr: int
    files_jaccard: float
    agent_files: list[str]
    pr_files: list[str]
    tests_passed: bool
    judge_score: int | None = None       # 1-5 from LLM-as-judge
    judge_rationale: str = ""
    notes: str = ""


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_pr_files(owner: str, repo: str, pr: int) -> list[str]:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr}/files?per_page=100"
    r = requests.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    return [f["filename"] for f in r.json()]


def files_jaccard(a: list[str], b: list[str]) -> float:
    A, B = set(a), set(b)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def parse_patch_files(patch_text: str) -> list[str]:
    """Extract changed-file paths from a unified-diff patch."""
    out: set[str] = set()
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)", patch_text, re.MULTILINE):
        out.add(m.group(2))
    return sorted(out)


def judge_with_llm(model: str, issue_title: str, issue_body: str,
                    accepted_pr_diff: str, agent_diff: str) -> tuple[int, str]:
    """Use a separate LLM call to rate root-cause similarity 1-5.

    Returns (score, rationale). Score 5 = same root-cause fix. Score 1 =
    unrelated changes. Uses the same provider abstraction as the agent
    itself, so Gemini and Claude both work.
    """
    from agent.llm import build_llm
    from agent.tracing import Tracer

    # The judge gets its own throwaway tracer.
    import tempfile
    from pathlib import Path
    tracer = Tracer(Path(tempfile.mkstemp(suffix=".jsonl")[1]))
    llm = build_llm(model=model, tracer=tracer)

    system = (
        "You are evaluating whether an AI agent's proposed code change addresses "
        "the same root cause as a human's accepted fix. Output JSON: "
        '{"score": <1-5>, "rationale": "<one paragraph>"}. '
        "5 = clearly the same fix (possibly different style). "
        "3 = touches the right area but the change is different. "
        "1 = unrelated or wrong area."
    )
    user = (
        f"## Issue\n{issue_title}\n\n{issue_body[:2000]}\n\n"
        f"## Accepted human PR diff\n```diff\n{accepted_pr_diff[:6000]}\n```\n\n"
        f"## Agent's proposed diff\n```diff\n{agent_diff[:6000]}\n```\n\n"
        "Output the JSON only."
    )
    resp = llm.complete(
        stage="judge", system=system,
        messages=[{"role": "user", "content": user}],
        max_tokens=512, temperature=0.0,
    )
    import json
    s = resp.text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip().rstrip("`").strip()
    obj = json.loads(s[s.find("{"): s.rfind("}") + 1])
    return int(obj["score"]), obj.get("rationale", "")
