"""Eval harness entrypoint. Invoked via `goagent eval evals/fixtures.yaml`."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from agent.config import load_repo_config
from agent.loop import run as run_pipeline
from evals.scoring import (
    FixtureScore, fetch_pr_files, files_jaccard, judge_with_llm, parse_patch_files,
)


console = Console()


def run_fixtures(*, fixtures_path: Path, runs_dir: Path, model: str) -> int:
    fixtures = yaml.safe_load(fixtures_path.read_text(encoding="utf-8")).get("fixtures", [])
    scored: list[FixtureScore] = []

    for fx in fixtures:
        repo_name, issue_num, pr_num = fx["repo"], int(fx["issue"]), int(fx["pr"])
        if issue_num == 0 or pr_num == 0:
            console.print(f"[yellow]skip[/yellow] {repo_name} placeholder fixture")
            continue
        console.print(f"\n[bold]eval[/bold] {repo_name}#{issue_num} (vs PR #{pr_num})")
        cfg = load_repo_config(repo_name)
        result = run_pipeline(
            repo_config=cfg, issue_number=issue_num,
            model=model, runs_dir=runs_dir,
        )

        try:
            pr_files = fetch_pr_files(cfg.owner, cfg.repo, pr_num)
        except Exception as e:
            console.print(f"  [red]could not fetch PR files: {e}[/red]")
            pr_files = []

        patch_text = result.patch_path.read_text(encoding="utf-8", errors="replace")
        agent_files = parse_patch_files(patch_text)
        score = FixtureScore(
            repo=cfg.slug, issue=issue_num, pr=pr_num,
            files_jaccard=round(files_jaccard(agent_files, pr_files), 3),
            agent_files=agent_files,
            pr_files=pr_files,
            tests_passed=result.validation_passed,
        )

        # LLM-as-judge (best-effort).
        try:
            from agent.tools.shell import run_command
            # Fetch the human PR diff via the GitHub API (.patch).
            import requests
            pr_url = f"https://github.com/{cfg.owner}/{cfg.repo}/pull/{pr_num}.diff"
            pr_diff = requests.get(pr_url, timeout=30).text
            issue_json_path = result.run_dir / "issue.json"
            issue = json.loads(issue_json_path.read_text(encoding="utf-8"))
            j_score, j_rat = judge_with_llm(
                model=model, issue_title=issue["title"], issue_body=issue["body"],
                accepted_pr_diff=pr_diff, agent_diff=patch_text,
            )
            score.judge_score = j_score
            score.judge_rationale = j_rat
        except Exception as e:
            score.notes = f"judge failed: {e}"

        scored.append(score)

    _print_summary(scored)
    return 0 if scored else 1


def _print_summary(scored: list[FixtureScore]) -> None:
    if not scored:
        console.print("[yellow]no fixtures scored[/yellow]")
        return
    t = Table(title="Eval results")
    t.add_column("Repo")
    t.add_column("Issue", justify="right")
    t.add_column("PR", justify="right")
    t.add_column("Files J", justify="right")
    t.add_column("Tests", justify="center")
    t.add_column("Judge", justify="right")
    for s in scored:
        t.add_row(
            s.repo, str(s.issue), str(s.pr),
            f"{s.files_jaccard:.2f}",
            "PASS" if s.tests_passed else "FAIL",
            str(s.judge_score) if s.judge_score is not None else "-",
        )
    console.print(t)
    for s in scored:
        if s.judge_rationale:
            console.print(f"\n[dim]judge {s.repo}#{s.issue}:[/dim] {s.judge_rationale}")
