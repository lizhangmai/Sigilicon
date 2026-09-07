"""Presentation-only helpers shared by command-line entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sigilicon.canonical import canonical_json
from sigilicon.paths import discover_project_contract
from sigilicon.project import Project


def emit_json(value: Any) -> None:
    print(canonical_json(value), end="")


def open_cli_project(root: Path | None) -> Project:
    """Open the explicit CLI root or discover the nearest project contract."""

    return Project.open(
        discover_project_contract().parent if root is None else root
    )
