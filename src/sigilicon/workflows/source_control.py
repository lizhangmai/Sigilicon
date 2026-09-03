"""Explicit, tool-bound Git checkout evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sigilicon.execution.model import Resources
from sigilicon.external_tools import (
    ProcessRequest,
    managed_process,
)


@dataclass(frozen=True)
class CheckoutState:
    """Exact revision and worktree state used by a source release."""

    commit: str
    working_tree_dirty: bool
    changes: tuple[str, ...]


def inspect_checkout(root: Path, resources: Resources) -> CheckoutState:
    """Inspect one checkout with the project-configured Git executable."""

    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    with resources.owned_tool("vcs.git") as executable:

        def invoke(*arguments: str):
            return managed_process.run(
                ProcessRequest(
                    argv=(
                        *executable.command,
                        "-c",
                        "core.fsmonitor=false",
                        "-c",
                        "core.hooksPath=/dev/null",
                        *arguments,
                    ),
                    executable=executable.executable,
                    cwd=Path(root).resolve(),
                    environment=environment,
                    timeout_seconds=30,
                    before_spawn=executable.require_visible,
                    pass_fds=(
                        executable.target.fd,
                        executable.target.directory_fd,
                    ),
                )
            )

        revision = invoke("rev-parse", "HEAD")
        if revision.returncode != 0 or not revision.stdout.strip():
            raise RuntimeError(
                f"cannot resolve source commit:\n{revision.stdout}{revision.stderr}"
            )
        status = invoke("status", "--porcelain=v1", "--untracked-files=all")
        if status.returncode != 0:
            raise RuntimeError(
                "cannot inspect source checkout:\n"
                f"{status.stdout}{status.stderr}"
            )
    changes = tuple(status.stdout.splitlines())
    return CheckoutState(
        commit=revision.stdout.strip(),
        working_tree_dirty=bool(changes),
        changes=changes,
    )


__all__ = ["CheckoutState", "inspect_checkout"]
