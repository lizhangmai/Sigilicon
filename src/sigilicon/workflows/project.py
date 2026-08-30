"""Project composition shared by CLI and external integration entrypoints."""

from __future__ import annotations

from pathlib import Path

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.agentic_execution import agentic_execution_grant_from_json
from sigilicon.domain.repository import Project
from sigilicon.workflows.agentic_execution import AgenticExecutionInterface
from sigilicon.workflows.agentic_read import AgenticReadInterface


def load_project(project_contract: Path | str) -> Project:
    """Parse one canonical project contract at an application entrypoint."""

    return Project.from_file(project_contract)


def bind_agentic_read(project_root: Path | str) -> AgenticReadInterface:
    """Bind the read Interface to one explicitly selected project."""

    return AgenticReadInterface(Project.from_project_root(project_root))


def bind_agentic_execution(
    project_root: Path | str,
    *,
    grant_contract: Path,
    environment_contract: Path | None = None,
) -> AgenticExecutionInterface:
    """Bind authorized execution without exposing launcher parsing to adapters."""

    return AgenticExecutionInterface(
        bind_agentic_read(project_root),
        grant=agentic_execution_grant_from_json(
            read_nofollow_text(grant_contract.resolve())
        ),
        environment_contract=environment_contract,
    )


__all__ = [
    "bind_agentic_execution",
    "bind_agentic_read",
    "load_project",
]
