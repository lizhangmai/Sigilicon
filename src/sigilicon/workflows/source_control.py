"""Source-state evidence and clean checks for managed workflows."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from typing import Any

from sigilicon.external_tools import ProcessRequest, managed_process


@dataclass(frozen=True)
class SourceState:
    """Git coordinates and worktree state captured at workflow start."""

    repository_available: bool
    commit: str | None
    working_tree_dirty: bool | None
    changes: tuple[str, ...]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": 1,
            "repository_available": self.repository_available,
            "commit": self.commit,
            "working_tree_dirty": self.working_tree_dirty,
            "changes": list(self.changes),
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


def inspect_source_state(root: Path) -> SourceState:
    """Capture Git identity without imposing a clean-worktree requirement.

    Managed repository workflows normally run inside a Git checkout. Small
    isolated test projects and source-only diagnostic fixtures may not have
    one; those are represented explicitly as unavailable rather than being
    mistaken for a clean checkout.
    """

    environment = os.environ.copy()
    revision = managed_process.run(ProcessRequest(
        argv=("git", "rev-parse", "HEAD"),
        cwd=root,
        environment=environment,
        timeout_seconds=30,
    ))
    if revision.returncode != 0 or not revision.stdout.strip():
        status = managed_process.run(ProcessRequest(
            argv=("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=root,
            environment=environment,
            timeout_seconds=30,
        ))
        status_text = status.stdout
        return SourceState(
            repository_available=False,
            commit=None,
            working_tree_dirty=None,
            changes=tuple(status_text.splitlines()),
            error=(
                f"{revision.stdout}\n{revision.stderr}".strip()
                or "cannot resolve Git HEAD"
            )[-4000:],
        )

    status = managed_process.run(ProcessRequest(
        argv=("git", "status", "--porcelain=v1", "--untracked-files=all"),
        cwd=root,
        environment=environment,
        timeout_seconds=30,
    ))
    if status.returncode != 0:
        raise RuntimeError(
            f"cannot inspect source checkout:\n{status.stdout}{status.stderr}"
        )
    status_text = status.stdout
    return SourceState(
        repository_available=True,
        commit=revision.stdout.strip(),
        working_tree_dirty=bool(status_text.strip()),
        changes=tuple(status_text.splitlines()),
    )


def artifact_source_state(project_root: Path) -> dict[str, Any]:
    """Return Git revisions for the project and this Sigilicon installation.

    Editable installs expose the Sigilicon checkout directly.  Wheels retain
    their package version without pretending that a surrounding project Git
    repository owns the installed package.
    """

    try:
        package_version = version("sigilicon")
    except PackageNotFoundError:
        package_version = "unknown"
    package_root = Path(__file__).resolve().parents[3]
    sigilicon: dict[str, Any] = {"version": package_version}
    if (package_root / "pyproject.toml").is_file() and (
        package_root / ".git"
    ).exists():
        sigilicon.update(inspect_source_state(package_root).as_dict())
    else:
        sigilicon.update(
            {
                "schema": 1,
                "repository_available": False,
                "commit": None,
                "working_tree_dirty": None,
                "changes": [],
            }
        )
    return {
        "project": inspect_source_state(project_root).as_dict(),
        "sigilicon": sigilicon,
    }
