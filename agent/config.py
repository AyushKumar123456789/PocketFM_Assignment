"""Per-repo configuration loader.

Configs live in `configs/<name>.yaml`. Each config tells the agent how to
clone, build, test, and lint the target repo, plus where to look for
project conventions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


@dataclass
class RepoConfig:
    name: str                            # e.g. "cobra"
    owner: str                           # e.g. "spf13"
    repo: str                            # e.g. "cobra"
    clone_url: str
    default_branch: str = "main"
    build_cmd: list[str] = field(default_factory=lambda: ["go", "build", "./..."])
    test_cmd: list[str] = field(default_factory=lambda: ["go", "test", "./..."])
    vet_cmd: list[str] = field(default_factory=lambda: ["go", "vet", "./..."])
    fmt_cmd: list[str] = field(default_factory=lambda: ["gofmt", "-w"])
    # Files to read into the conventions prompt.
    convention_paths: list[str] = field(default_factory=lambda: ["CONTRIBUTING.md"])
    # Glob patterns to ignore when building the repo map.
    ignore_globs: list[str] = field(default_factory=lambda: [
        "vendor/**", "**/testdata/**", "**/*_gen.go", "**/zz_*.go",
    ])
    # Optional extra hint shown to the planner.
    project_notes: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def load_repo_config(name: str) -> RepoConfig:
    """Load a single repo config by name."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in CONFIG_DIR.glob("*.yaml")))
        raise FileNotFoundError(
            f"No config for '{name}' at {path}. Available: {available}"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RepoConfig(name=name, **raw)


def list_repo_configs() -> dict[str, RepoConfig]:
    """Load all repo configs from the configs/ directory."""
    out: dict[str, RepoConfig] = {}
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        out[path.stem] = load_repo_config(path.stem)
    return out
