"""Source-state evidence and clean checks for managed workflows."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from sigilicon.external_tools import run_process_group


@dataclass(frozen=True)
class SourceState:
    """A reproducible description of the checkout at workflow start."""

    repository_available: bool
    head: str | None
    working_tree_dirty: bool | None
    status: tuple[str, ...]
    status_sha256: str
    tracked_diff_sha256: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": 1,
            "repository_available": self.repository_available,
            "head": self.head,
            "working_tree_dirty": self.working_tree_dirty,
            "status": list(self.status),
            "status_sha256": self.status_sha256,
            "tracked_diff_sha256": self.tracked_diff_sha256,
        }
        if self.error is not None:
            payload["error"] = self.error
        payload["source_state_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return payload


def inspect_source_state(root: Path) -> SourceState:
    """Capture Git identity without imposing a clean-worktree requirement.

    Managed repository workflows normally run inside a Git checkout. Small
    isolated test projects and source-only diagnostic fixtures may not have
    one; those are represented explicitly as unavailable rather than being
    mistaken for a clean checkout.
    """

    environment = os.environ.copy()
    revision = run_process_group(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        env=environment,
        timeout=30,
    )
    if revision.returncode != 0 or not revision.stdout.strip():
        status = run_process_group(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            env=environment,
            timeout=30,
        )
        status_text = status.stdout
        return SourceState(
            repository_available=False,
            head=None,
            working_tree_dirty=None,
            status=tuple(status_text.splitlines()),
            status_sha256=hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
            tracked_diff_sha256=None,
            error=(revision.stdout.strip() or "cannot resolve Git HEAD")[-4000:],
        )

    status = run_process_group(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        env=environment,
        timeout=30,
    )
    if status.returncode != 0:
        raise RuntimeError(f"cannot inspect source checkout:\n{status.stdout}")
    diff = run_process_group(
        ["git", "diff", "--binary", "HEAD"],
        cwd=root,
        env=environment,
        timeout=30,
    )
    if diff.returncode != 0:
        raise RuntimeError(f"cannot fingerprint source diff:\n{diff.stdout}")
    status_text = status.stdout
    return SourceState(
        repository_available=True,
        head=revision.stdout.strip(),
        working_tree_dirty=bool(status_text.strip()),
        status=tuple(status_text.splitlines()),
        status_sha256=hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
        tracked_diff_sha256=hashlib.sha256(diff.stdout.encode("utf-8")).hexdigest(),
    )


def require_clean_source_commit(root: Path) -> str:
    """Return HEAD after proving that a formal qualification checkout is clean."""

    revision = run_process_group(
        ["git", "rev-parse", "HEAD"], cwd=root, env=os.environ.copy(), timeout=30
    )
    if revision.returncode != 0:
        raise RuntimeError(f"cannot resolve source commit:\n{revision.stdout}")
    status = run_process_group(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        env=os.environ.copy(),
        timeout=30,
    )
    if status.returncode != 0:
        raise RuntimeError(f"cannot inspect source checkout:\n{status.stdout}")
    if status.stdout.strip():
        raise RuntimeError(
            "managed qualification requires a clean committed checkout; "
            f"found:\n{status.stdout}"
        )
    return revision.stdout.strip()
