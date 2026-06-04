"""Workspace management: clone the target repo (or update an existing clone)
into `workspaces/<repo_name>/`.

Each repo lives in its own subdirectory. We always check out the default
branch and `git fetch` on every run, so the workspace is up-to-date.
"""
from __future__ import annotations

from pathlib import Path

from agent.config import RepoConfig
from agent.tools.shell import run_command


WORKSPACES_ROOT = Path(__file__).resolve().parent.parent.parent / "workspaces"


def ensure_workspace(cfg: RepoConfig) -> Path:
    ws = WORKSPACES_ROOT / cfg.name
    if not ws.exists():
        WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
        r = run_command(["git", "clone", "--depth", "50", cfg.clone_url, str(ws)],
                        cwd=WORKSPACES_ROOT, timeout_s=600)
        if r.returncode != 0:
            raise RuntimeError(f"git clone failed: {r.combined}")
    else:
        # Best-effort refresh. We don't fail the run if this errors — maybe
        # the user is offline and just wants to re-run on the cached repo.
        run_command(["git", "fetch", "origin", cfg.default_branch],
                    cwd=ws, timeout_s=120)
        run_command(["git", "checkout", cfg.default_branch], cwd=ws, timeout_s=30)
        run_command(["git", "reset", "--hard", f"origin/{cfg.default_branch}"],
                    cwd=ws, timeout_s=30)
        run_command(["git", "clean", "-fd"], cwd=ws, timeout_s=30)
    return ws
