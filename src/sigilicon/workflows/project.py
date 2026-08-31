"""Project composition shared by CLI and external integration entrypoints."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.agentic_execution import agentic_execution_grant_from_json
from sigilicon.domain.config_contracts import read_toml
from sigilicon.domain.repository import Project
from sigilicon.execution import RunStore
from sigilicon.paths import ProjectContext
from sigilicon.workflows.agentic_response import repository_identity
from sigilicon.workflows.agentic_execution import AgenticExecutionInterface
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows.run_read import RunReadInterface


def bind_run_store(project_root: Path | str) -> RunStore:
    """Bind historical run records without loading current owner catalogs."""

    return RunStore(ProjectContext.from_project_root(project_root))


def bind_run_read(project_root: Path | str) -> RunReadInterface:
    """Bind agent-facing historical reads without loading current catalogs."""

    contract = Path(project_root).resolve() / "sigilicon.toml"
    raw = read_toml(contract)
    context = ProjectContext.from_contract(contract, raw)
    if context.project_root != contract.parent:
        raise ValueError("sigilicon.toml declares a different project root")
    manifest_owner = cast(str, raw["owner"])
    return RunReadInterface(
        context,
        repository_identity(manifest_owner, context),
    )


def bind_agentic_read(project_root: Path | str) -> AgenticReadInterface:
    """Bind the read Interface to one explicitly selected project."""

    return AgenticReadInterface.from_project(Project.from_project_root(project_root))


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
    "bind_run_read",
    "bind_run_store",
]
