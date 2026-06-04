"""Validation: gofmt → go vet → go build → go test.

Validation is the agent's correctness signal. If it passes, the change is
plausibly good. If it fails, the captured stderr is fed back to the
editor for a retry.

We try to scope the test command to packages that the edits touched, to
keep the loop fast. A full `go test ./...` is run as a final smoke test
once focused tests pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent.config import RepoConfig
from agent.stages.edit import EditSet
from agent.tools.shell import run_command


@dataclass
class ValidationResult:
    passed: bool
    build_ok: bool = False
    vet_ok: bool = False
    test_ok: bool = False
    fmt_ok: bool = True
    build_output: str = ""
    vet_output: str = ""
    test_output: str = ""
    fmt_output: str = ""

    def error_summary(self) -> str:
        parts: list[str] = []
        if not self.fmt_ok:
            parts.append(f"## gofmt failed\n{self.fmt_output}")
        if not self.vet_ok:
            parts.append(f"## go vet failed\n{self.vet_output}")
        if not self.build_ok:
            parts.append(f"## go build failed\n{self.build_output}")
        if not self.test_ok:
            parts.append(f"## go test failed\n{self.test_output}")
        return "\n\n".join(parts)


def _affected_packages(workspace: Path, edits: EditSet) -> list[str]:
    """Map changed files to Go package selectors. Use `./pkg/...` form."""
    pkgs: set[str] = set()
    for op in edits.ops:
        p = Path(op.file)
        d = p.parent.as_posix() or "."
        pkgs.add(f"./{d}/..." if d != "." else "./...")
    return sorted(pkgs) or ["./..."]


def validate_changes(
    workspace: Path,
    cfg: RepoConfig,
    edits: EditSet,
    *,
    test_timeout_s: int = 180,
) -> ValidationResult:
    result = ValidationResult(passed=False)

    # 1) gofmt -w on each edited file.
    changed_files = sorted({op.file for op in edits.ops})
    if changed_files:
        out = run_command([*cfg.fmt_cmd, *changed_files], cwd=workspace, timeout_s=30)
        result.fmt_ok = out.returncode == 0
        result.fmt_output = out.combined

    # 2) go vet.
    out = run_command(cfg.vet_cmd, cwd=workspace, timeout_s=120)
    result.vet_ok = out.returncode == 0
    result.vet_output = out.combined

    # 3) go build.
    out = run_command(cfg.build_cmd, cwd=workspace, timeout_s=180)
    result.build_ok = out.returncode == 0
    result.build_output = out.combined

    # 4) go test — affected packages first (fast feedback), then full suite.
    if result.build_ok:
        pkgs = _affected_packages(workspace, edits)
        out = run_command([*cfg.test_cmd[:-1], *pkgs] if cfg.test_cmd[-1] == "./..." else cfg.test_cmd,
                          cwd=workspace, timeout_s=test_timeout_s)
        result.test_ok = out.returncode == 0
        result.test_output = out.combined
        # Only run the broad sweep if the focused test passed AND it wasn't already ./...
        if result.test_ok and pkgs != ["./..."]:
            out = run_command(cfg.test_cmd, cwd=workspace, timeout_s=test_timeout_s)
            result.test_ok = out.returncode == 0
            if not result.test_ok:
                result.test_output += "\n\n--- broad-suite sweep also run ---\n" + out.combined

    result.passed = result.fmt_ok and result.vet_ok and result.build_ok and result.test_ok
    return result
