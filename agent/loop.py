"""The orchestrator. This is the ONLY place where stage order is defined.

The pipeline:

    Fetch issue ──▶ Index repo ──▶ Retrieve files ──▶ Plan ──┐
                                                              ▼
                                                            Edit
                                                              │
                                              (apply edits)   │
                                                              ▼
                          ┌─── retry (max N) ──── Validate (go build/vet/test) ─── fail
                          │                                   │
                          └───────── edit again ◀─────────────┘
                                                              │
                                                              ▼ pass
                                                          Summarize
                                                              │
                                                              ▼
                                                    patch + pr.md + trace.jsonl

Each stage is a pure-ish function that takes a `Context` and returns
something concrete. Stages that need an LLM accept `llm: LLM`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

from agent.config import RepoConfig
from agent.llm import build_llm
from agent.stages.fetch_issue import fetch_issue
from agent.stages.index_repo import index_repo
from agent.stages.retrieve import retrieve_candidates
from agent.stages.plan import generate_plan
from agent.stages.edit import generate_edits
from agent.stages.validate import validate_changes
from agent.stages.summarize import write_pr
from agent.tools.edits import apply_structured_edits, revert_all
from agent.tools.workspace import ensure_workspace
from agent.tracing import Tracer

console = Console()


@dataclass
class RunResult:
    success: bool
    run_dir: Path
    patch_path: Path
    pr_md_path: Path
    trace_path: Path
    issue_number: int
    repo_slug: str
    files_changed: list[str] = field(default_factory=list)
    validation_passed: bool = False
    attempts: int = 0
    notes: str = ""


def _new_run_dir(runs_dir: Path, repo_name: str, issue_number: int) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = runs_dir / f"{ts}_{repo_name}_{issue_number}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def run(
    *,
    repo_config: RepoConfig,
    issue_number: int,
    max_validation_retries: int = 3,
    model: str = "claude-sonnet-4-6",
    runs_dir: Path = Path("runs"),
    dry_run: bool = False,
) -> RunResult:
    """End-to-end execution of the agent on one issue.

    This function is intentionally linear and easy to read. Branching,
    retries, and error handling live here rather than scattered across
    stages.
    """
    run_dir = _new_run_dir(runs_dir, repo_config.name, issue_number)
    tracer = Tracer(run_dir / "trace.jsonl")
    llm = build_llm(model=model, tracer=tracer)

    started = time.time()
    tracer.event("run.start", {
        "repo": repo_config.slug,
        "issue": issue_number,
        "model": model,
        "max_retries": max_validation_retries,
    })

    # 1) Fetch issue.
    console.print(f"[bold]1/6[/bold] fetching issue #{issue_number} from {repo_config.slug}")
    issue = fetch_issue(repo_config, issue_number)
    (run_dir / "issue.json").write_text(json.dumps(issue.to_dict(), indent=2), encoding="utf-8")
    tracer.event("fetch_issue.done", {"title": issue.title, "labels": issue.labels})

    # 2) Workspace + index.
    console.print(f"[bold]2/6[/bold] preparing workspace and indexing repo")
    workspace = ensure_workspace(repo_config)
    repo_map = index_repo(workspace, repo_config)
    (run_dir / "repo_map.json").write_text(json.dumps(repo_map.to_dict(), indent=2), encoding="utf-8")
    tracer.event("index_repo.done", {"packages": len(repo_map.packages), "files": repo_map.file_count})

    # 3) Retrieve relevant files.
    console.print(f"[bold]3/6[/bold] retrieving candidate files")
    candidates = retrieve_candidates(workspace, repo_map, issue)
    tracer.event("retrieve.done", {"candidates": [c.path for c in candidates]})

    # 4) Plan.
    console.print(f"[bold]4/6[/bold] planning the change")
    plan = generate_plan(llm, issue=issue, repo_map=repo_map, candidates=candidates,
                        repo_config=repo_config, workspace=workspace)
    (run_dir / "plan.json").write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
    tracer.event("plan.done", plan.to_dict())

    if dry_run:
        tracer.event("run.dry_run_exit", {})
        return RunResult(
            success=True, run_dir=run_dir,
            patch_path=run_dir / "changes.patch",
            pr_md_path=run_dir / "pr.md",
            trace_path=run_dir / "trace.jsonl",
            issue_number=issue_number,
            repo_slug=repo_config.slug,
            notes="dry-run: stopped after plan",
        )

    # 5) Edit → Validate loop. EVERY failure mode (LLM parse error,
    # degenerate output, edit-application error, gofmt/vet/build/test
    # failure) goes through the same retry: capture the error, feed it
    # back to the Editor as `prior_error`, try again.
    console.print(f"[bold]5/6[/bold] generating edits and validating")
    last_error: str | None = None
    validation = None
    edits = None
    from agent.tools.edits import EditError, sanity_check_edits, apply_structured_edits, revert_all
    for attempt in range(1, max_validation_retries + 1):
        console.print(f"  attempt {attempt}/{max_validation_retries}")
        revert_all(workspace)  # always start each attempt from clean HEAD
        try:
            edits = generate_edits(
                llm, issue=issue, plan=plan, repo_map=repo_map,
                workspace=workspace, prior_error=last_error,
                repo_config=repo_config,
            )
            sanity_check_edits(edits)
            apply_structured_edits(workspace, edits)
        except (ValueError, EditError) as e:
            # Parse failure, degenerate output, or unapplicable edit.
            err_str = str(e)
            tracer.event("edit.failed", {"attempt": attempt, "error": err_str[:2000]})
            console.print(f"  [yellow]edit failed:[/yellow] {err_str[:200]}")
            last_error = f"Your previous edit attempt could not be applied:\n{err_str[:3000]}"
            continue

        validation = validate_changes(workspace, repo_config, edits)
        tracer.event("validate.done", {
            "attempt": attempt, "passed": validation.passed,
            "build_ok": validation.build_ok, "vet_ok": validation.vet_ok,
            "test_ok": validation.test_ok,
        })
        if validation.passed:
            break
        last_error = validation.error_summary()

    success = bool(validation and validation.passed)

    # 6) Summarize. Best-effort — if the LLM call fails (rate limit, network,
    # malformed output), fall back to a deterministic summary built from the
    # plan + validation result so the reviewer still gets *something* useful.
    console.print(f"[bold]6/6[/bold] writing PR text")
    pr_md_path = run_dir / "pr.md"
    try:
        pr = write_pr(llm, issue=issue, plan=plan, edits=edits, validation=validation,
                      repo_config=repo_config)
        pr_md_path.write_text(f"# {pr.title}\n\n{pr.body}\n", encoding="utf-8")
    except Exception as e:
        tracer.event("summarize.failed", {"error": str(e)[:1000]})
        console.print(f"  [yellow]summarize failed:[/yellow] {str(e)[:200]}")
        # Deterministic fallback so artifacts are still readable.
        files_str = "\n".join(f"- {f}" for f in sorted({op.file for op in (edits.ops if edits else [])})) or "- (no files changed)"
        val = (
            f"- gofmt: {'ok' if validation and validation.fmt_ok else 'fail/skipped'}\n"
            f"- go vet: {'ok' if validation and validation.vet_ok else 'fail/skipped'}\n"
            f"- go build: {'ok' if validation and validation.build_ok else 'fail/skipped'}\n"
            f"- go test: {'ok' if validation and validation.test_ok else 'fail/skipped'}"
        )
        title = f"WIP: address #{issue_number} ({issue.title[:50]})"
        body = (
            f"## Summary\n{plan.hypothesis}\n\n"
            f"## Files changed\n{files_str}\n\n"
            f"## Validation\n{val}\n\n"
            f"## Notes\nGenerated PR text was unavailable (LLM call failed: {str(e)[:200]}). "
            f"This is a deterministic fallback summary built from the agent's Plan output.\n\n"
            f"Closes #{issue_number}"
        )
        pr_md_path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")

    # Write the patch.
    patch_path = run_dir / "changes.patch"
    from agent.tools.shell import git_diff
    patch_path.write_text(git_diff(workspace), encoding="utf-8")

    tracer.event("run.end", {
        "duration_s": round(time.time() - started, 2),
        "success": success,
        "attempts": attempt if validation else 0,
    })

    return RunResult(
        success=success,
        run_dir=run_dir,
        patch_path=patch_path,
        pr_md_path=pr_md_path,
        trace_path=run_dir / "trace.jsonl",
        issue_number=issue_number,
        repo_slug=repo_config.slug,
        files_changed=[e.file for e in (edits.ops if edits else [])],
        validation_passed=success,
        attempts=attempt if validation else 0,
        notes="" if success else "validation failed after retries; see trace for details",
    )
