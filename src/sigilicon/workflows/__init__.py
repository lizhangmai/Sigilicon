"""Application workflows coordinating pure domain logic and tool adapters."""

from pathlib import Path

from sigilicon.domain.repository import Project


def load_project(project_contract: Path | str) -> Project:
    """Load the canonical Project at the CLI/application composition boundary."""

    return Project.from_file(project_contract)
