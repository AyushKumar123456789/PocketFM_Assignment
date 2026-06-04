"""Command-line entrypoint for goagent.

    goagent solve <repo> <issue#>          # run the agent on one issue
    goagent eval <fixtures.yaml>           # run the eval harness
    goagent repos                          # list supported repos

`repo` is a config name (cobra, gin, validator, golangci-lint) — the
agent looks up `configs/<repo>.yaml` to find the clone URL, test command,
and other per-repo settings.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from agent.loop import run as run_pipeline
from agent.config import load_repo_config, list_repo_configs

console = Console()


def _check_prereqs(model: str, *, dry_run: bool = False) -> list[str]:
    """Return a list of human-readable prerequisite problems, empty if all good.

    The required API key depends on the chosen model: gemini-* needs
    GEMINI_API_KEY (or GOOGLE_API_KEY); claude-* needs ANTHROPIC_API_KEY.

    Go is only required when we're going to validate (not in --dry-run mode,
    which stops after the Plan stage).
    """
    problems: list[str] = []
    if model.startswith("gemini-"):
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            problems.append(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add "
                "your key. Free signup (no credit card) at "
                "https://aistudio.google.com/apikey."
            )
    elif model.startswith("claude-"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            problems.append(
                "ANTHROPIC_API_KEY is not set. Either set it (signup at "
                "https://console.anthropic.com/) or switch to a Gemini model "
                "(--model gemini-2.5-flash) with a free key from "
                "https://aistudio.google.com/apikey."
            )
    elif model.startswith("deepseek-ai/"):
        if not os.environ.get("NVIDIA_API_KEY"):
            problems.append(
                "NVIDIA_API_KEY is not set. Required for DeepSeek models hosted "
                "on NVIDIA NIM. Get a key at https://build.nvidia.com/."
            )
    elif model.startswith("groq/"):
        if not os.environ.get("GROQ_API_KEY"):
            problems.append(
                "GROQ_API_KEY is not set. Free key (no credit card) at "
                "https://console.groq.com/keys."
            )
    elif model.startswith("gpt-"):
        if not os.environ.get("OPENAI_API_KEY"):
            problems.append(
                "OPENAI_API_KEY is not set. Required for gpt-* models. "
                "Get a key at https://platform.openai.com/api-keys."
            )
    else:
        problems.append(
            f"Unknown model '{model}'. Use a name starting with "
            f"'gemini-', 'claude-', 'deepseek-ai/', 'groq/', or 'gpt-'."
        )

    from shutil import which
    if not dry_run and which("go") is None:
        problems.append(
            "`go` not found on PATH. Install Go 1.21+ from "
            "https://go.dev/doc/install. (Or pass --dry-run to skip validation.)"
        )
    if which("git") is None:
        problems.append("`git` not found on PATH. Install from https://git-scm.com/.")
    return problems


def cmd_solve(args: argparse.Namespace) -> int:
    model = args.model or os.environ.get("GOAGENT_MODEL", "gemini-2.5-flash")
    problems = _check_prereqs(model, dry_run=args.dry_run)
    if problems:
        for p in problems:
            console.print(f"[red]error:[/red] {p}")
        return 2

    cfg = load_repo_config(args.repo)
    result = run_pipeline(
        repo_config=cfg,
        issue_number=args.issue,
        max_validation_retries=args.max_retries,
        model=model,
        runs_dir=Path(args.runs_dir),
        dry_run=args.dry_run,
    )
    console.print(f"\n[green]done[/green] — run artifacts at {result.run_dir}")
    console.print(f"  patch:  {result.patch_path}")
    console.print(f"  pr.md:  {result.pr_md_path}")
    console.print(f"  trace:  {result.trace_path}")
    return 0 if result.success else 1


def cmd_eval(args: argparse.Namespace) -> int:
    # Import lazily so `goagent --help` doesn't need the eval dependencies.
    from evals.run_eval import run_fixtures

    return run_fixtures(
        fixtures_path=Path(args.fixtures),
        runs_dir=Path(args.runs_dir),
        model=args.model or os.environ.get("GOAGENT_MODEL", "gemini-2.5-flash"),
    )


def cmd_repos(_: argparse.Namespace) -> int:
    for name, cfg in list_repo_configs().items():
        console.print(f"  [cyan]{name}[/cyan] — {cfg.clone_url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="goagent", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", help="Run the agent on one GitHub issue.")
    solve.add_argument("repo", help="Config name, e.g. 'cobra' (see `goagent repos`).")
    solve.add_argument("issue", type=int, help="GitHub issue number on the target repo.")
    solve.add_argument("--max-retries", type=int, default=3,
                       help="Max validate→edit retries (default: 3).")
    solve.add_argument("--model", default=None,
                       help="Override model. Examples: gemini-2.5-flash (free), "
                            "gemini-2.5-pro, claude-sonnet-4-6, claude-opus-4-7. "
                            "Default: gemini-2.5-flash.")
    solve.add_argument("--runs-dir", default="runs", help="Where to write run artifacts.")
    solve.add_argument("--dry-run", action="store_true",
                       help="Skip the Edit/Validate stages; print Plan and exit.")
    solve.set_defaults(func=cmd_solve)

    ev = sub.add_parser("eval", help="Run the eval harness on a fixtures file.")
    ev.add_argument("fixtures", help="Path to fixtures.yaml.")
    ev.add_argument("--runs-dir", default="runs")
    ev.add_argument("--model", default=None)
    ev.set_defaults(func=cmd_eval)

    repos = sub.add_parser("repos", help="List supported repos.")
    repos.set_defaults(func=cmd_repos)

    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # picks up .env if present
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
