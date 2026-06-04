"""Sandboxed shell command runner.

`run_command` is the only place we shell out. Centralizing this means:
- We can apply timeouts uniformly.
- We can cap output (long test failures can blow up trace memory).
- We can swap in a container/jail later without touching callers.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


_OUTPUT_CAP = 64 * 1024  # 64KB per stream — plenty for build/test errors


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        if self.stderr and self.stdout:
            return f"{self.stdout}\n\n--- stderr ---\n{self.stderr}"
        return self.stderr or self.stdout


def run_command(cmd: list[str], *, cwd: Path, timeout_s: int = 60,
                env: dict[str, str] | None = None) -> CommandResult:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout_s, env=env,
        )
        return CommandResult(
            returncode=p.returncode,
            stdout=p.stdout[-_OUTPUT_CAP:],
            stderr=p.stderr[-_OUTPUT_CAP:],
        )
    except subprocess.TimeoutExpired as e:
        return CommandResult(
            returncode=124,
            stdout=(e.stdout or b"").decode("utf-8", "replace")[-_OUTPUT_CAP:],
            stderr=f"command timed out after {timeout_s}s: {' '.join(cmd)}",
        )
    except FileNotFoundError as e:
        return CommandResult(returncode=127, stdout="", stderr=str(e))


def git_diff(workspace: Path) -> str:
    """Return the current `git diff` (working tree vs HEAD)."""
    r = run_command(["git", "diff", "HEAD"], cwd=workspace, timeout_s=30)
    return r.stdout


def git_head_sha(workspace: Path) -> str:
    r = run_command(["git", "rev-parse", "HEAD"], cwd=workspace, timeout_s=10)
    return r.stdout.strip() or "HEAD"
