"""Project construction at the application composition boundary."""

from __future__ import annotations

from pathlib import Path

from sigilicon.domain.repository import Project


def load_project(project_contract: Path | str) -> Project:
    """Load the single canonical project context used by one operation."""

    return Project.from_file(project_contract)
